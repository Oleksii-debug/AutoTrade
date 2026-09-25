"""Durable provider-evidenced option lifecycle accounting.

This module binds normalized provider/reference lifecycle observations to the
canonical InstrumentRegistry and DurableProviderEconomicBook.  It deliberately
does not create another ledger, provider adapter, reconciliation authority or
trading authority.

A lifecycle event and its resulting economic transaction batch are committed by
one JournalStore transaction.  Exact retries are idempotent, changed economics
under one external identity fail closed, and corrections use explicit
reversal/replacement lineage.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import re
from typing import Any, Callable, Mapping
from uuid import NAMESPACE_URL, uuid5

from .accounting import JournalTransaction, posting, reverse_transaction
from .instruments import InstrumentRegistry, InstrumentVersion
from .options import (
    DeliverableLeg,
    OptionContract,
    OptionError,
    book_cash_option_settlement,
    book_physical_option_settlement,
    expiration_cash_settlement,
    physical_exercise_obligation,
)
from .persistence import JournalStore, payload_digest
from .provider_activity_accounting import DurableProviderEconomicBook
from .provider_core import ProviderResponseObservation, Surface


_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_ENVIRONMENTS = frozenset({"REPLAY", "SIMULATION", "PAPER", "LIVE"})
_EVENT_KINDS = frozenset({"EXERCISE", "ASSIGNMENT", "EXPIRY"})
_OPTION_LIFECYCLE_PARSER_ID = "autotrade.option-lifecycle.sealed-json"
_OPTION_LIFECYCLE_PARSER_VERSION = "1.0.0"
_OPTION_LIFECYCLE_PARSER_CONTRACT_DIGEST = payload_digest(
    {
        "parser_id": _OPTION_LIFECYCLE_PARSER_ID,
        "parser_version": _OPTION_LIFECYCLE_PARSER_VERSION,
        "source_type": "ProviderResponseObservation",
        "source_surface": "ACTIVITIES",
        "payload_fields": [
            "venue_id",
            "external_event_id",
            "event_kind",
            "signed_contracts",
            "effective_at",
            "provider_revision",
            "underlying_price",
            "corrects_external_event_id",
        ],
        "financial_binding": "EXACT_SEALED_PAYLOAD",
    }
)

OptionLifecycleEvidenceResolver = Callable[[str], ProviderResponseObservation]


class OptionLifecycleError(ValueError):
    """Invalid or unsupported lifecycle evidence."""


class OptionLifecycleConflict(OptionLifecycleError):
    """Immutable lifecycle identity was reused with incompatible evidence."""


def _canonical_observation_from_sealed_response(
    source: ProviderResponseObservation,
) -> "OptionLifecycleObservation":
    """Parse the only admitted canonical lifecycle response shape.

    This parser is part of the financial authority. Callers may resolve sealed
    provider evidence but cannot inject executable normalization logic that
    invents lifecycle economics.
    """

    payload = source.payload
    if not isinstance(payload, Mapping):
        raise OptionLifecycleError(
            "provider lifecycle payload must be a canonical object"
        )
    required = {
        "venue_id",
        "external_event_id",
        "event_kind",
        "signed_contracts",
        "effective_at",
        "provider_revision",
        "underlying_price",
        "corrects_external_event_id",
    }
    if set(payload) != required:
        raise OptionLifecycleError(
            "provider lifecycle payload shape is not canonical"
        )

    def instant(value: object, name: str) -> datetime:
        if not isinstance(value, str) or not value.endswith("Z"):
            raise OptionLifecycleError(
                f"{name} must be canonical UTC text"
            )
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise OptionLifecycleError(
                f"{name} must be canonical UTC text"
            ) from error
        return _utc(parsed, name)

    try:
        observed_at = instant(source.observed_at, "observed_at")
        effective_at = instant(payload["effective_at"], "effective_at")
        signed_contracts = _decimal(
            payload["signed_contracts"],
            "signed_contracts",
        )
        underlying_price = (
            None
            if payload["underlying_price"] is None
            else _decimal(payload["underlying_price"], "underlying_price")
        )
    except (InvalidOperation, TypeError, ValueError) as error:
        raise OptionLifecycleError(
            "provider lifecycle payload contains invalid financial values"
        ) from error

    return OptionLifecycleObservation(
        provider_id=source.provider_id,
        account_id=source.account_id,
        environment=source.environment,
        venue_id=_text(payload["venue_id"], "venue_id"),
        instrument_version=source.query_binding.instrument_version,
        external_event_id=_text(
            payload["external_event_id"],
            "external_event_id",
        ),
        event_kind=_text(payload["event_kind"], "event_kind"),
        signed_contracts=signed_contracts,
        effective_at=effective_at,
        observed_at=observed_at,
        raw_evidence_digest=source.response_sha256,
        provider_revision=_text(
            payload["provider_revision"],
            "provider_revision",
        ),
        underlying_price=underlying_price,
        corrects_external_event_id=(
            None
            if payload["corrects_external_event_id"] is None
            else _text(
                payload["corrects_external_event_id"],
                "corrects_external_event_id",
            )
        ),
    )


def _text(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise OptionLifecycleError(f"{name} must be canonical non-empty text")
    return value


def _decimal(value: Decimal | str | int, name: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise OptionLifecycleError(f"{name} must use exact decimal input")
    try:
        result = value if isinstance(value, Decimal) else Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise OptionLifecycleError(f"{name} must be a finite decimal") from error
    if not result.is_finite():
        raise OptionLifecycleError(f"{name} must be a finite decimal")
    return result


def _utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise OptionLifecycleError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _decimal_text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    if value == 0:
        return "0"
    rendered = format(value.normalize(), "f")
    return rendered


def _identity(kind: str, *parts: str) -> str:
    return str(
        uuid5(
            NAMESPACE_URL,
            "https://identity.autotrade.local/"
            + kind
            + "/"
            + payload_digest(list(parts)).removeprefix("sha256:"),
        )
    )


@dataclass(frozen=True)
class OptionLifecycleObservation:
    """One immutable normalized provider/reference lifecycle fact."""

    provider_id: str
    account_id: str
    environment: str
    venue_id: str
    instrument_version: str
    external_event_id: str
    event_kind: str
    signed_contracts: Decimal
    effective_at: datetime
    observed_at: datetime
    raw_evidence_digest: str
    provider_revision: str
    underlying_price: Decimal | None = None
    corrects_external_event_id: str | None = None

    def __post_init__(self) -> None:
        provider = _text(self.provider_id, "provider_id").upper()
        account = _text(self.account_id, "account_id")
        environment = _text(self.environment, "environment").upper()
        venue = _text(self.venue_id, "venue_id")
        instrument_version = _text(self.instrument_version, "instrument_version")
        external_event_id = _text(self.external_event_id, "external_event_id")
        event_kind = _text(self.event_kind, "event_kind").upper()
        revision = _text(self.provider_revision, "provider_revision")
        if environment not in _ENVIRONMENTS:
            raise OptionLifecycleError("environment is unsupported")
        if event_kind not in _EVENT_KINDS:
            raise OptionLifecycleError(
                "unsupported option lifecycle event; fail closed instead of approximating"
            )
        signed = _decimal(self.signed_contracts, "signed_contracts")
        if signed == 0:
            raise OptionLifecycleError("signed_contracts must be non-zero")
        if event_kind == "EXERCISE" and signed < 0:
            raise OptionLifecycleError("EXERCISE requires positive signed_contracts")
        if event_kind == "ASSIGNMENT" and signed > 0:
            raise OptionLifecycleError("ASSIGNMENT requires negative signed_contracts")
        effective = _utc(self.effective_at, "effective_at")
        observed = _utc(self.observed_at, "observed_at")
        if observed < effective:
            raise OptionLifecycleError("observed_at cannot precede effective_at")
        if not isinstance(self.raw_evidence_digest, str) or _DIGEST.fullmatch(
            self.raw_evidence_digest
        ) is None:
            raise OptionLifecycleError(
                "raw_evidence_digest must be a canonical sha256 digest"
            )
        underlying = self.underlying_price
        if underlying is not None:
            underlying = _decimal(underlying, "underlying_price")
            if underlying <= 0:
                raise OptionLifecycleError("underlying_price must be positive")
        correction = self.corrects_external_event_id
        if correction is not None:
            correction = _text(correction, "corrects_external_event_id")
            if correction == external_event_id:
                raise OptionLifecycleError("lifecycle event cannot correct itself")

        object.__setattr__(self, "provider_id", provider)
        object.__setattr__(self, "account_id", account)
        object.__setattr__(self, "environment", environment)
        object.__setattr__(self, "venue_id", venue)
        object.__setattr__(self, "instrument_version", instrument_version)
        object.__setattr__(self, "external_event_id", external_event_id)
        object.__setattr__(self, "event_kind", event_kind)
        object.__setattr__(self, "signed_contracts", signed)
        object.__setattr__(self, "effective_at", effective)
        object.__setattr__(self, "observed_at", observed)
        object.__setattr__(self, "provider_revision", revision)
        object.__setattr__(self, "underlying_price", underlying)
        object.__setattr__(self, "corrects_external_event_id", correction)


@dataclass(frozen=True)
class OptionLifecycleApplyResult:
    lifecycle_event_id: str
    inserted: bool
    active_transaction_ids: tuple[str, ...]
    reversal_transaction_ids: tuple[str, ...]
    corrected_external_event_id: str | None


def canonical_option_lifecycle_observation(
    observation: OptionLifecycleObservation,
) -> dict[str, Any]:
    if not isinstance(observation, OptionLifecycleObservation):
        raise TypeError("observation must be OptionLifecycleObservation")
    return {
        "schema_version": "1.0.0",
        "provider_id": observation.provider_id,
        "account_id": observation.account_id,
        "environment": observation.environment,
        "venue_id": observation.venue_id,
        "instrument_version": observation.instrument_version,
        "external_event_id": observation.external_event_id,
        "event_kind": observation.event_kind,
        "signed_contracts": _decimal_text(observation.signed_contracts),
        "effective_at": _utc_text(observation.effective_at),
        "observed_at": _utc_text(observation.observed_at),
        "raw_evidence_digest": observation.raw_evidence_digest,
        "provider_revision": observation.provider_revision,
        "underlying_price": _decimal_text(observation.underlying_price),
        "corrects_external_event_id": observation.corrects_external_event_id,
    }


def _standard_physical_exercise_cash(version: InstrumentVersion) -> Decimal:
    """Return strike cash only when standard terms are explicit in registry data.

    The existing option core intentionally forbids inferring adjusted-contract
    exercise cash from strike * multiplier.  InstrumentVersion currently has no
    independent adjusted exercise-cash field, so any non-standard deliverable
    fails closed here instead of silently inventing economics.
    """

    if version.strike is None:
        raise OptionLifecycleError("option strike is missing from instrument version")
    if len(version.deliverable) != 1:
        raise OptionLifecycleError(
            "adjusted physical deliverable requires explicit canonical exercise cash evidence"
        )
    leg = version.deliverable[0]
    if leg.quantity != version.contract_multiplier:
        raise OptionLifecycleError(
            "adjusted physical deliverable requires explicit canonical exercise cash evidence"
        )
    return version.strike * version.contract_multiplier


def _contract_from_version(version: InstrumentVersion) -> OptionContract:
    if version.asset_class != "OPTION":
        raise OptionLifecycleError("instrument_version must identify an OPTION")
    if version.option_right not in {"CALL", "PUT"}:
        raise OptionLifecycleError("instrument option_right is unsupported")
    if version.strike is None or version.expiry is None:
        raise OptionLifecycleError("option strike/expiry are required")
    if version.settlement_method not in {"CASH", "PHYSICAL"}:
        raise OptionLifecycleError(
            "unsupported option settlement method; fail closed instead of approximating"
        )
    if version.exercise_style not in {"AMERICAN", "EUROPEAN"}:
        raise OptionLifecycleError("option exercise_style is unsupported")

    cutoff = version.delivery_cutoff or version.expiry
    if version.settlement_method == "PHYSICAL":
        cash = _standard_physical_exercise_cash(version)
        deliverable = tuple(
            DeliverableLeg(
                asset_id=leg.asset_id,
                quantity_per_contract=leg.quantity,
            )
            for leg in version.deliverable
        )
    else:
        cash = None
        deliverable = ()

    return OptionContract(
        instrument=f"{version.instrument_id}@{version.version}",
        right=version.option_right,
        strike=version.strike,
        multiplier=version.contract_multiplier,
        settlement_currency=version.settlement_currency,
        settlement_method=version.settlement_method,
        exercise_style=version.exercise_style,
        expiry=version.expiry,
        exercise_cutoff=cutoff,
        deliverable=deliverable,
        exercise_opens_at=version.effective_from,
        exercise_cash_per_contract=cash,
    )


def _bind_version(
    registry: InstrumentRegistry,
    observation: OptionLifecycleObservation,
) -> InstrumentVersion:
    if not isinstance(registry, InstrumentRegistry):
        raise TypeError("registry must be InstrumentRegistry")
    version = registry.exact(observation.instrument_version)
    effective = registry.at(version.instrument_id, observation.effective_at)
    if effective != version:
        raise OptionLifecycleError(
            "instrument_version is not the version effective for lifecycle event"
        )
    if version.provider_id.upper() != observation.provider_id:
        raise OptionLifecycleError("provider_id does not match instrument version")
    if version.venue_id != observation.venue_id:
        raise OptionLifecycleError("venue_id does not match instrument version")
    if version.asset_class != "OPTION":
        raise OptionLifecycleError("lifecycle event is not bound to an option")
    return version


def _economic_transaction(
    observation: OptionLifecycleObservation,
    version: InstrumentVersion,
    *,
    cause_event_id: str,
    order_root_external_id: str,
    corrects_transaction_id: str | None = None,
) -> JournalTransaction:
    contract = _contract_from_version(version)
    transaction_id = _identity(
        "option-lifecycle-economic",
        observation.provider_id,
        observation.account_id,
        observation.environment,
        observation.external_event_id,
    )

    # The lifecycle fact consumes the option contract itself.  Retirement is in
    # the same canonical transaction as delivery/cash settlement, so exercise,
    # assignment and even zero-payoff expiry cannot leave expired option
    # inventory economically alive.
    postings = [
        posting(
            f"POSITION:{contract.instrument}",
            contract.instrument,
            -observation.signed_contracts,
        ),
        posting(
            f"CLEARING:{contract.instrument}",
            contract.instrument,
            observation.signed_contracts,
        ),
    ]

    if contract.settlement_method == "PHYSICAL":
        # Physical expiry alone is not evidence that delivery occurred.  It
        # retires only the option inventory. Explicit EXERCISE/ASSIGNMENT adds
        # the delivery obligations.
        if observation.event_kind != "EXPIRY":
            obligation = physical_exercise_obligation(
                contract,
                signed_contracts=observation.signed_contracts,
            )
            base = book_physical_option_settlement(
                transaction_id=transaction_id,
                cause_event_id=cause_event_id,
                obligation=obligation,
            )
            postings.extend(base.postings)
    else:
        if observation.underlying_price is None:
            raise OptionLifecycleError(
                "cash-settled lifecycle event requires provider/reference underlying_price evidence"
            )
        amount = expiration_cash_settlement(
            contract,
            signed_contracts=observation.signed_contracts,
            underlying_price=observation.underlying_price,
        )
        if amount != 0:
            base = book_cash_option_settlement(
                transaction_id=transaction_id,
                cause_event_id=cause_event_id,
                settlement_currency=contract.settlement_currency,
                amount=amount,
            )
            postings.extend(base.postings)

    return JournalTransaction(
        transaction_id=transaction_id,
        cause_event_id=cause_event_id,
        postings=tuple(postings),
        economic_effective_at=_utc_text(observation.effective_at),
        economic_order_key=f"OPTION_LIFECYCLE:{order_root_external_id}",
        observed_at=_utc_text(observation.observed_at),
        corrects_transaction_id=corrects_transaction_id,
    )


def _project_position_after_reversal(
    *,
    economic_book: DurableProviderEconomicBook,
    instrument: str,
    old_active_transactions: tuple[JournalTransaction, ...],
) -> Decimal:
    projected = economic_book.position(instrument)
    for transaction in old_active_transactions:
        matching = [
            item
            for item in transaction.postings
            if item.ledger_account == f"POSITION:{instrument}"
            and item.asset_or_currency == instrument
        ]
        if len(matching) != 1:
            raise OptionLifecycleConflict(
                "prior lifecycle economics do not contain one option-position retirement"
            )
        projected -= matching[0].signed_amount
    return projected


def _require_consumable_option_position(
    *,
    economic_book: DurableProviderEconomicBook,
    observation: OptionLifecycleObservation,
    version: InstrumentVersion,
    old_active_transactions: tuple[JournalTransaction, ...],
) -> None:
    instrument = f"{version.instrument_id}@{version.version}"
    available = _project_position_after_reversal(
        economic_book=economic_book,
        instrument=instrument,
        old_active_transactions=old_active_transactions,
    )
    required = observation.signed_contracts
    if (required > 0 and available < required) or (
        required < 0 and available > required
    ):
        raise OptionLifecycleConflict(
            "provider lifecycle contracts exceed canonical option position"
        )


class DurableOptionLifecycleAuthority:
    """Exactly-once lifecycle-to-economics bridge over canonical authorities."""

    _ACTOR = "option-lifecycle-accounting"
    _AGGREGATE_TYPE = "option_lifecycle"

    def __init__(
        self,
        store: JournalStore,
        *,
        registry: InstrumentRegistry,
        economic_book: DurableProviderEconomicBook,
        evidence_resolver: OptionLifecycleEvidenceResolver,
        lifecycle_endpoints: frozenset[str],
        permission_scope: str,
    ) -> None:
        if not isinstance(store, JournalStore):
            raise TypeError("store must be JournalStore")
        if not isinstance(registry, InstrumentRegistry):
            raise TypeError("registry must be InstrumentRegistry")
        if not isinstance(economic_book, DurableProviderEconomicBook):
            raise TypeError("economic_book must be DurableProviderEconomicBook")
        if economic_book.store is not store:
            raise ValueError("lifecycle and economic authorities must share one JournalStore")
        if not callable(evidence_resolver):
            raise TypeError("evidence_resolver must be callable")
        if not isinstance(lifecycle_endpoints, frozenset) or not lifecycle_endpoints:
            raise TypeError("lifecycle_endpoints must be a non-empty frozenset")
        endpoints = frozenset(
            _text(endpoint, "lifecycle endpoint")
            for endpoint in lifecycle_endpoints
        )
        if any(not endpoint.startswith("/") or "://" in endpoint for endpoint in endpoints):
            raise OptionLifecycleError(
                "lifecycle endpoints must be canonical provider-relative paths"
            )
        scope = _text(permission_scope, "permission_scope")
        self.store = store
        self.registry = registry
        self.economic_book = economic_book
        self.evidence_resolver = evidence_resolver
        self.lifecycle_endpoints = endpoints
        self.permission_scope = scope
        self.aggregate_id = _identity(
            "option-lifecycle-book",
            economic_book.provider_id,
            economic_book.account_id,
            economic_book.environment,
        )

    def _events(self) -> list[dict[str, Any]]:
        return self.store.load_events(self._AGGREGATE_TYPE, self.aggregate_id)

    @staticmethod
    def _payload(event: Mapping[str, Any]) -> Mapping[str, Any]:
        payload = event.get("payload")
        if not isinstance(payload, Mapping):
            raise OptionLifecycleConflict("durable lifecycle event payload is invalid")
        return payload

    def _observation_from_evidence(
        self,
        evidence_ref: str,
    ) -> tuple[OptionLifecycleObservation, ProviderResponseObservation]:
        reference = _text(evidence_ref, "evidence_ref")
        try:
            source = self.evidence_resolver(reference)
        except Exception as error:
            raise OptionLifecycleError(
                "provider lifecycle evidence could not be resolved"
            ) from error
        if not isinstance(source, ProviderResponseObservation):
            raise OptionLifecycleError(
                "provider lifecycle evidence must be a sealed ProviderResponseObservation"
            )
        if source.evidence_ref != reference:
            raise OptionLifecycleError(
                "resolved provider lifecycle evidence identity mismatch"
            )
        endpoint = source.query_binding.endpoint
        if endpoint not in self.lifecycle_endpoints:
            raise OptionLifecycleError(
                "provider lifecycle evidence endpoint is not allowed"
            )
        try:
            source.require_scope(
                provider_id=self.economic_book.provider_id,
                surface=Surface.ACTIVITIES,
                endpoint=endpoint,
                account_id=self.economic_book.account_id,
                environment=self.economic_book.environment,
            )
        except Exception as error:
            raise OptionLifecycleError(
                "provider lifecycle evidence scope mismatch"
            ) from error
        if source.query_binding.permission_scope != self.permission_scope:
            raise OptionLifecycleError(
                "provider lifecycle evidence permission scope mismatch"
            )
        observation = _canonical_observation_from_sealed_response(source)
        observed_at = datetime.fromisoformat(
            source.observed_at.replace("Z", "+00:00")
        ).astimezone(timezone.utc)
        if (
            observation.provider_id != source.provider_id
            or observation.account_id != source.account_id
            or observation.environment != source.environment
            or observation.instrument_version
            != source.query_binding.instrument_version
            or observation.raw_evidence_digest != source.response_sha256
            or observation.observed_at != observed_at
        ):
            raise OptionLifecycleError(
                "normalized lifecycle fact does not match sealed provider evidence"
            )
        return observation, source

    def apply(
        self,
        evidence_ref: str,
    ) -> OptionLifecycleApplyResult:
        observation, provider_evidence = self._observation_from_evidence(evidence_ref)
        if observation.provider_id != self.economic_book.provider_id:
            raise OptionLifecycleError("provider scope does not match economic book")
        if observation.account_id != self.economic_book.account_id:
            raise OptionLifecycleError("account scope does not match economic book")
        if observation.environment != self.economic_book.environment:
            raise OptionLifecycleError("environment scope does not match economic book")

        version = _bind_version(self.registry, observation)
        observation_payload = canonical_option_lifecycle_observation(observation)
        observation_digest = payload_digest(observation_payload)
        instrument_digest = payload_digest(version.to_contract_dict())
        provider_evidence_payload = {
            "evidence_ref": provider_evidence.evidence_ref,
            "response_sha256": provider_evidence.response_sha256,
            "query_digest": provider_evidence.query_binding.query_digest,
            "endpoint": provider_evidence.query_binding.endpoint,
            "permission_scope": provider_evidence.query_binding.permission_scope,
            "capability_snapshot_id": (
                provider_evidence.query_binding.capability_snapshot_id
            ),
            "instrument_version": provider_evidence.query_binding.instrument_version,
            "observed_at": provider_evidence.observed_at,
            "parser_id": _OPTION_LIFECYCLE_PARSER_ID,
            "parser_version": _OPTION_LIFECYCLE_PARSER_VERSION,
            "parser_contract_digest": _OPTION_LIFECYCLE_PARSER_CONTRACT_DIGEST,
        }
        provider_evidence_digest = payload_digest(provider_evidence_payload)
        events = self._events()

        same_identity = [
            event
            for event in events
            if self._payload(event).get("external_event_id")
            == observation.external_event_id
        ]
        if same_identity:
            if len(same_identity) != 1:
                raise OptionLifecycleConflict(
                    "external lifecycle identity appears more than once"
                )
            saved = self._payload(same_identity[0])
            if (
                saved.get("observation_digest") != observation_digest
                or saved.get("instrument_digest") != instrument_digest
                or saved.get("provider_evidence_digest")
                != provider_evidence_digest
            ):
                raise OptionLifecycleConflict(
                    "external lifecycle identity was reused with changed evidence"
                )
            return OptionLifecycleApplyResult(
                lifecycle_event_id=str(same_identity[0]["event_id"]),
                inserted=False,
                active_transaction_ids=tuple(saved.get("active_transaction_ids", ())),
                reversal_transaction_ids=tuple(
                    saved.get("reversal_transaction_ids", ())
                ),
                corrected_external_event_id=saved.get(
                    "corrects_external_event_id"
                ),
            )

        prior_event: Mapping[str, Any] | None = None
        prior_payload: Mapping[str, Any] | None = None
        root_external_id = observation.external_event_id
        old_active_transactions: tuple[JournalTransaction, ...] = ()

        if observation.corrects_external_event_id is not None:
            matches = [
                event
                for event in events
                if self._payload(event).get("external_event_id")
                == observation.corrects_external_event_id
            ]
            if len(matches) != 1:
                raise OptionLifecycleConflict(
                    "correction target must identify exactly one prior lifecycle event"
                )
            if any(
                self._payload(event).get("corrects_external_event_id")
                == observation.corrects_external_event_id
                for event in events
            ):
                raise OptionLifecycleConflict(
                    "correction target already has a replacement; correct the latest event instead"
                )
            prior_event = matches[0]
            prior_payload = self._payload(prior_event)
            if prior_payload.get("instrument_version") != observation.instrument_version:
                raise OptionLifecycleConflict(
                    "correction cannot change instrument version identity"
                )
            if prior_payload.get("event_kind") != observation.event_kind:
                raise OptionLifecycleConflict("correction cannot change lifecycle event kind")
            if prior_payload.get("effective_at") != _utc_text(observation.effective_at):
                raise OptionLifecycleConflict(
                    "correction must preserve original economic effective time"
                )
            if prior_payload.get("observed_at", "") > _utc_text(observation.observed_at):
                raise OptionLifecycleConflict(
                    "correction observation cannot precede prior observation"
                )
            if prior_payload.get("provider_revision") == observation.provider_revision:
                raise OptionLifecycleConflict(
                    "correction requires a new provider revision"
                )
            if (
                prior_payload.get("provider_evidence_ref")
                == provider_evidence.evidence_ref
                or prior_payload.get("raw_evidence_digest")
                == observation.raw_evidence_digest
            ):
                raise OptionLifecycleConflict(
                    "correction requires fresh provider lifecycle evidence"
                )
            root_external_id = str(
                prior_payload.get("order_root_external_id")
                or prior_payload.get("external_event_id")
            )
            active_ids = tuple(prior_payload.get("active_transaction_ids", ()))
            by_id = {item.transaction_id: item for item in self.economic_book.transactions}
            missing = [transaction_id for transaction_id in active_ids if transaction_id not in by_id]
            if missing:
                raise OptionLifecycleConflict(
                    "prior lifecycle economics are missing from canonical economic book"
                )
            old_active_transactions = tuple(by_id[item] for item in active_ids)
            if len(old_active_transactions) > 1:
                raise OptionLifecycleConflict(
                    "unsupported multi-transaction option lifecycle correction"
                )

        _require_consumable_option_position(
            economic_book=self.economic_book,
            observation=observation,
            version=version,
            old_active_transactions=old_active_transactions,
        )

        lifecycle_event_id = _identity(
            "option-lifecycle-event",
            observation.provider_id,
            observation.account_id,
            observation.environment,
            observation.external_event_id,
        )

        reversal_transactions: list[JournalTransaction] = []
        replacement_target: str | None = None
        if old_active_transactions:
            original = old_active_transactions[0]
            reversal_transactions.append(
                reverse_transaction(
                    original,
                    transaction_id=_identity(
                        "option-lifecycle-reversal",
                        observation.provider_id,
                        observation.account_id,
                        observation.environment,
                        observation.external_event_id,
                        original.transaction_id,
                    ),
                    cause_event_id=_identity(
                        "option-lifecycle-reversal-cause",
                        lifecycle_event_id,
                        original.transaction_id,
                    ),
                    observed_at=_utc_text(observation.observed_at),
                )
            )
            replacement_target = original.transaction_id

        replacement_cause_event_id = (
            lifecycle_event_id
            if replacement_target is None
            else _identity(
                "option-lifecycle-replacement-cause",
                lifecycle_event_id,
                replacement_target,
            )
        )
        replacement = _economic_transaction(
            observation,
            version,
            cause_event_id=replacement_cause_event_id,
            order_root_external_id=root_external_id,
            corrects_transaction_id=replacement_target,
        )
        economic_transactions = tuple(reversal_transactions + [replacement])

        plan = (
            self.economic_book.prepare_batch_mutation(
                economic_transactions,
                committed_at=_utc_text(observation.observed_at),
            )
            if economic_transactions
            else None
        )
        if plan is not None and plan.already_committed:
            raise OptionLifecycleConflict(
                "fresh lifecycle identity maps to economics already committed elsewhere"
            )

        next_version = 1 if not events else int(events[-1]["aggregate_version"]) + 1
        active_transaction_ids = (replacement.transaction_id,)
        reversal_transaction_ids = tuple(
            item.transaction_id for item in reversal_transactions
        )
        lifecycle_payload = {
            "schema_version": "1.0.0",
            "provider_id": observation.provider_id,
            "account_id": observation.account_id,
            "environment": observation.environment,
            "venue_id": observation.venue_id,
            "instrument_version": observation.instrument_version,
            "instrument_digest": instrument_digest,
            "external_event_id": observation.external_event_id,
            "event_kind": observation.event_kind,
            "effective_at": _utc_text(observation.effective_at),
            "observed_at": _utc_text(observation.observed_at),
            "provider_revision": observation.provider_revision,
            "raw_evidence_digest": observation.raw_evidence_digest,
            "provider_evidence_ref": provider_evidence.evidence_ref,
            "provider_evidence_digest": provider_evidence_digest,
            "provider_evidence": provider_evidence_payload,
            "observation_digest": observation_digest,
            "corrects_external_event_id": observation.corrects_external_event_id,
            "order_root_external_id": root_external_id,
            "active_transaction_ids": list(active_transaction_ids),
            "reversal_transaction_ids": list(reversal_transaction_ids),
            "economic_batch_digest": plan.batch_digest if plan is not None else None,
        }
        lifecycle_envelope = {
            "event_id": lifecycle_event_id,
            "event_type": "OptionLifecycleApplied",
            "aggregate_type": self._AGGREGATE_TYPE,
            "aggregate_id": self.aggregate_id,
            "aggregate_version": str(next_version),
            "committed_at": _utc_text(observation.observed_at),
            "payload": lifecycle_payload,
            "payload_hash": payload_digest(lifecycle_payload),
        }

        request = {
            "schema_version": "1.0.0",
            "observation": observation_payload,
            "observation_digest": observation_digest,
            "provider_evidence": provider_evidence_payload,
            "provider_evidence_digest": provider_evidence_digest,
            "instrument_digest": instrument_digest,
        }
        result = {
            "lifecycle_event_id": lifecycle_event_id,
            "active_transaction_ids": list(active_transaction_ids),
            "reversal_transaction_ids": list(reversal_transaction_ids),
            "corrected_external_event_id": observation.corrects_external_event_id,
        }
        commit_events: list[tuple[dict[str, Any], str | None]] = [
            (lifecycle_envelope, "autotrade.option.lifecycle")
        ]
        if plan is not None:
            if plan.envelope is None:
                raise OptionLifecycleConflict("fresh economic plan has no durable event")
            commit_events.append((plan.envelope, "autotrade.economic.events"))

        command_id = _identity(
            "option-lifecycle-command",
            observation.provider_id,
            observation.account_id,
            observation.environment,
            observation.external_event_id,
        )
        _, inserted, _ = self.store.commit_command(
            command_id=command_id,
            actor=self._ACTOR,
            environment=observation.environment,
            idempotency_key=(
                f"option-lifecycle:{self.aggregate_id}:{observation.external_event_id}"
            ),
            request=request,
            result=result,
            state_version=next_version,
            events=commit_events,
        )
        self.economic_book.refresh()
        return OptionLifecycleApplyResult(
            lifecycle_event_id=lifecycle_event_id,
            inserted=inserted,
            active_transaction_ids=active_transaction_ids,
            reversal_transaction_ids=reversal_transaction_ids,
            corrected_external_event_id=observation.corrects_external_event_id,
        )
