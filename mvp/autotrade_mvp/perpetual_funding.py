"""Provider-evidenced durable perpetual funding accounting.

This module is an integration authority over existing canonical components:
sealed provider-read evidence, exact perpetual math, JournalStore, and
DurableProviderEconomicBook. It is not a second ledger and grants no order-send
authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from typing import Callable, Mapping
from uuid import NAMESPACE_URL, uuid5

from .accounting import (
    JournalTransaction,
    posting,
    reverse_transaction,
    transaction_digest,
)
from .instruments import InstrumentRegistry, InstrumentVersion
from .perpetuals import (
    FundingConvention,
    MarketSnapshot,
    PerpetualContract,
    PerpetualError,
    funding_cashflow,
)
from .persistence import JournalStore, canonical_json, payload_digest
from .provider_activity_accounting import DurableProviderEconomicBook
from .provider_core import ProviderResponseObservation, Surface


class PerpetualFundingError(ValueError):
    pass


class PerpetualFundingConflict(PerpetualFundingError):
    pass


def _text(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PerpetualFundingError(f"{name} is required")
    return value.strip()


def _decimal(value: Decimal | str | int, name: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise PerpetualFundingError(f"{name} must use exact decimal input")
    try:
        result = value if isinstance(value, Decimal) else Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise PerpetualFundingError(f"{name} must be a finite decimal") from error
    if not result.is_finite():
        raise PerpetualFundingError(f"{name} must be a finite decimal")
    return result


def _utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise PerpetualFundingError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _utc_text(value: datetime) -> str:
    return _utc(value, "timestamp").isoformat().replace("+00:00", "Z")


def _identity(kind: str, *parts: str) -> str:
    digest = sha256(canonical_json(list(parts)).encode("utf-8")).hexdigest()
    return f"{kind}:sha256:{digest}"


@dataclass(frozen=True)
class PerpetualFundingObservation:
    """Provider-normalized funding fact still bound to sealed raw evidence."""

    provider_id: str
    account_id: str
    environment: str
    instrument_id: str
    instrument_version: str
    external_event_id: str
    provider_revision: str
    funding_period_id: str
    effective_at: datetime
    observed_at: datetime
    price_reference_at: datetime
    signed_contracts: Decimal
    funding_rate: Decimal
    mark_price: Decimal
    index_price: Decimal
    price_basis: str
    collateral_currency: str
    positive_rate_effect: str
    raw_evidence_digest: str
    corrects_external_event_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider_id", _text(self.provider_id, "provider_id").upper())
        object.__setattr__(self, "account_id", _text(self.account_id, "account_id"))
        environment = _text(self.environment, "environment").upper()
        if environment not in {"REPLAY", "SIMULATION", "PAPER", "LIVE"}:
            raise PerpetualFundingError("unsupported environment")
        object.__setattr__(self, "environment", environment)
        object.__setattr__(self, "instrument_id", _text(self.instrument_id, "instrument_id"))
        object.__setattr__(
            self, "instrument_version", _text(self.instrument_version, "instrument_version")
        )
        object.__setattr__(
            self, "external_event_id", _text(self.external_event_id, "external_event_id")
        )
        object.__setattr__(
            self, "provider_revision", _text(self.provider_revision, "provider_revision")
        )
        object.__setattr__(
            self, "funding_period_id", _text(self.funding_period_id, "funding_period_id")
        )
        effective = _utc(self.effective_at, "effective_at")
        observed = _utc(self.observed_at, "observed_at")
        if observed < effective:
            raise PerpetualFundingError("observed_at cannot precede funding effective_at")
        price_reference = _utc(
            self.price_reference_at,
            "price_reference_at",
        )
        if price_reference != effective:
            raise PerpetualFundingError(
                "funding mark/index prices must be explicitly bound to the exact funding cut"
            )
        if price_reference > observed:
            raise PerpetualFundingError(
                "funding price reference cannot be observed after provider evidence"
            )
        object.__setattr__(self, "effective_at", effective)
        object.__setattr__(self, "observed_at", observed)
        object.__setattr__(self, "price_reference_at", price_reference)
        object.__setattr__(
            self, "signed_contracts", _decimal(self.signed_contracts, "signed_contracts")
        )
        object.__setattr__(self, "funding_rate", _decimal(self.funding_rate, "funding_rate"))
        object.__setattr__(self, "mark_price", _decimal(self.mark_price, "mark_price"))
        object.__setattr__(self, "index_price", _decimal(self.index_price, "index_price"))
        if self.mark_price <= 0 or self.index_price <= 0:
            raise PerpetualFundingError("funding price evidence must be positive")
        price_basis = _text(self.price_basis, "price_basis").upper()
        if price_basis not in {"MARK", "INDEX"}:
            raise PerpetualFundingError("price_basis must be MARK or INDEX")
        object.__setattr__(self, "price_basis", price_basis)
        collateral_currency = _text(
            self.collateral_currency, "collateral_currency"
        ).upper()
        object.__setattr__(
            self, "collateral_currency", collateral_currency
        )
        effect = _text(self.positive_rate_effect, "positive_rate_effect").upper()
        if effect not in {"LONG_PAYS", "LONG_RECEIVES"}:
            raise PerpetualFundingError(
                "positive_rate_effect must be LONG_PAYS or LONG_RECEIVES"
            )
        object.__setattr__(self, "positive_rate_effect", effect)
        digest = _text(self.raw_evidence_digest, "raw_evidence_digest")
        if not digest.startswith("sha256:") or len(digest) != 71:
            raise PerpetualFundingError("raw_evidence_digest must be canonical SHA-256")
        if self.corrects_external_event_id is not None:
            object.__setattr__(
                self,
                "corrects_external_event_id",
                _text(self.corrects_external_event_id, "corrects_external_event_id"),
            )


def canonical_perpetual_funding_observation(
    observation: PerpetualFundingObservation,
) -> dict[str, object]:
    if not isinstance(observation, PerpetualFundingObservation):
        raise TypeError("observation must be PerpetualFundingObservation")
    return {
        "schema_version": "1.0.0",
        "provider_id": observation.provider_id,
        "account_id": observation.account_id,
        "environment": observation.environment,
        "instrument_id": observation.instrument_id,
        "instrument_version": observation.instrument_version,
        "external_event_id": observation.external_event_id,
        "provider_revision": observation.provider_revision,
        "funding_period_id": observation.funding_period_id,
        "effective_at": _utc_text(observation.effective_at),
        "observed_at": _utc_text(observation.observed_at),
        "price_reference_at": _utc_text(observation.price_reference_at),
        "signed_contracts": format(observation.signed_contracts, "f"),
        "funding_rate": format(observation.funding_rate, "f"),
        "mark_price": format(observation.mark_price, "f"),
        "index_price": format(observation.index_price, "f"),
        "price_basis": observation.price_basis,
        "collateral_currency": observation.collateral_currency,
        "positive_rate_effect": observation.positive_rate_effect,
        "raw_evidence_digest": observation.raw_evidence_digest,
        "corrects_external_event_id": observation.corrects_external_event_id,
    }


FundingEvidenceResolver = Callable[[str], ProviderResponseObservation]


def _canonical_observation_from_sealed_response(
    source: ProviderResponseObservation,
) -> PerpetualFundingObservation:
    """Parse funding economics inside the authority, never through caller code."""

    payload = source.payload
    if not isinstance(payload, Mapping):
        raise PerpetualFundingError(
            "provider funding payload must be a canonical object"
        )
    required = {
        "external_event_id",
        "provider_revision",
        "funding_period_id",
        "effective_at",
        "price_reference_at",
        "instrument_id",
        "signed_contracts",
        "funding_rate",
        "mark_price",
        "index_price",
        "price_basis",
        "collateral_currency",
        "positive_rate_effect",
        "corrects_external_event_id",
    }
    if set(payload) != required:
        raise PerpetualFundingError(
            "provider funding payload shape is not canonical"
        )

    def instant(value: object, name: str) -> datetime:
        if not isinstance(value, str) or not value.endswith("Z"):
            raise PerpetualFundingError(
                f"{name} must be canonical UTC text"
            )
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise PerpetualFundingError(
                f"{name} must be canonical UTC text"
            ) from error
        return _utc(parsed, name)

    return PerpetualFundingObservation(
        provider_id=source.provider_id,
        account_id=source.account_id,
        environment=source.environment,
        instrument_id=_text(payload["instrument_id"], "instrument_id"),
        instrument_version=source.query_binding.instrument_version,
        external_event_id=_text(
            payload["external_event_id"],
            "external_event_id",
        ),
        provider_revision=_text(
            payload["provider_revision"],
            "provider_revision",
        ),
        funding_period_id=_text(
            payload["funding_period_id"],
            "funding_period_id",
        ),
        effective_at=instant(payload["effective_at"], "effective_at"),
        observed_at=instant(source.observed_at, "observed_at"),
        price_reference_at=instant(
            payload["price_reference_at"],
            "price_reference_at",
        ),
        signed_contracts=_decimal(
            payload["signed_contracts"],
            "signed_contracts",
        ),
        funding_rate=_decimal(payload["funding_rate"], "funding_rate"),
        mark_price=_decimal(payload["mark_price"], "mark_price"),
        index_price=_decimal(payload["index_price"], "index_price"),
        price_basis=_text(payload["price_basis"], "price_basis"),
        collateral_currency=_text(
            payload["collateral_currency"], "collateral_currency"
        ),
        positive_rate_effect=_text(
            payload["positive_rate_effect"],
            "positive_rate_effect",
        ),
        raw_evidence_digest=source.response_sha256,
        corrects_external_event_id=(
            None
            if payload["corrects_external_event_id"] is None
            else _text(
                payload["corrects_external_event_id"],
                "corrects_external_event_id",
            )
        ),
    )


@dataclass(frozen=True)
class FundingPositionCut:
    """Causal position proof projected from the canonical durable economic book."""

    instrument: str
    effective_at: str
    evidence_observed_at: str
    position: Decimal
    economic_book_digest: str
    journal_sequence: int
    contributing_transaction_ids: tuple[str, ...]
    contributing_transaction_digests: tuple[str, ...]
    digest: str


@dataclass(frozen=True)
class FundingApplyResult:
    funding_event_id: str
    inserted: bool
    active_transaction_id: str
    reversal_transaction_id: str | None
    cashflow: Decimal
    currency: str


class DurablePerpetualFundingAuthority:
    """Exactly-once provider funding bridge over the canonical economic book."""

    _AGGREGATE_TYPE = "perpetual_funding"
    _EVENT_TYPE = "PerpetualFundingApplied"
    _ACTOR = "perpetual-funding-accounting"

    def __init__(
        self,
        store: JournalStore,
        *,
        economic_book: DurableProviderEconomicBook,
        instrument_registry: InstrumentRegistry,
        evidence_resolver: FundingEvidenceResolver,
        funding_endpoints: frozenset[str],
        permission_scope: str,
    ) -> None:
        if not isinstance(store, JournalStore):
            raise TypeError("store must be JournalStore")
        if not isinstance(economic_book, DurableProviderEconomicBook):
            raise TypeError("economic_book must be DurableProviderEconomicBook")
        if economic_book.store is not store:
            raise ValueError("funding authority and economic book must share one JournalStore")
        if not isinstance(instrument_registry, InstrumentRegistry):
            raise TypeError("instrument_registry must be InstrumentRegistry")
        if not callable(evidence_resolver):
            raise TypeError("evidence_resolver must be callable")
        if not isinstance(funding_endpoints, frozenset) or not funding_endpoints:
            raise TypeError("funding_endpoints must be a non-empty frozenset")
        endpoints = frozenset(_text(value, "funding endpoint") for value in funding_endpoints)
        if any(not value.startswith("/") or "://" in value for value in endpoints):
            raise PerpetualFundingError("funding endpoints must be provider-relative paths")
        self.store = store
        self.economic_book = economic_book
        self.instrument_registry = instrument_registry
        self.evidence_resolver = evidence_resolver
        self.funding_endpoints = endpoints
        self.permission_scope = _text(permission_scope, "permission_scope")
        self.aggregate_id = _identity(
            "perpetual-funding-book",
            economic_book.provider_id,
            economic_book.account_id,
            economic_book.environment,
        )

    def _events(self) -> list[dict[str, object]]:
        return self.store.load_events(self._AGGREGATE_TYPE, self.aggregate_id)

    @staticmethod
    def _payload(event: Mapping[str, object]) -> Mapping[str, object]:
        payload = event.get("payload")
        if not isinstance(payload, Mapping):
            raise PerpetualFundingConflict("durable funding event payload is invalid")
        return payload

    def _observation(
        self, evidence_ref: str
    ) -> tuple[PerpetualFundingObservation, ProviderResponseObservation]:
        reference = _text(evidence_ref, "evidence_ref")
        try:
            source = self.evidence_resolver(reference)
        except Exception as error:
            raise PerpetualFundingError("provider funding evidence could not be resolved") from error
        if not isinstance(source, ProviderResponseObservation):
            raise PerpetualFundingError(
                "provider funding evidence must be a sealed ProviderResponseObservation"
            )
        if source.evidence_ref != reference:
            raise PerpetualFundingError("resolved provider funding evidence identity mismatch")
        endpoint = source.query_binding.endpoint
        if endpoint not in self.funding_endpoints:
            raise PerpetualFundingError("provider funding evidence endpoint is not allowed")
        try:
            source.require_scope(
                provider_id=self.economic_book.provider_id,
                surface=Surface.ACTIVITIES,
                endpoint=endpoint,
                account_id=self.economic_book.account_id,
                environment=self.economic_book.environment,
            )
        except Exception as error:
            raise PerpetualFundingError("provider funding evidence scope mismatch") from error
        if source.query_binding.permission_scope != self.permission_scope:
            raise PerpetualFundingError("provider funding evidence permission scope mismatch")
        observation = _canonical_observation_from_sealed_response(source)
        source_observed = datetime.fromisoformat(
            source.observed_at.replace("Z", "+00:00")
        ).astimezone(timezone.utc)
        if (
            observation.provider_id != source.provider_id
            or observation.account_id != source.account_id
            or observation.environment != source.environment
            or observation.instrument_version != source.query_binding.instrument_version
            or observation.raw_evidence_digest != source.response_sha256
            or observation.observed_at != source_observed
        ):
            raise PerpetualFundingError(
                "normalized funding fact does not match sealed provider evidence"
            )
        return observation, source

    def _contract(
        self,
        observation: PerpetualFundingObservation,
    ) -> tuple[InstrumentVersion, PerpetualContract, str]:
        try:
            version = self.instrument_registry.exact(observation.instrument_version)
            effective = self.instrument_registry.at(
                version.instrument_id,
                observation.effective_at,
            )
        except Exception as error:
            raise PerpetualFundingError(
                "funding instrument version is absent or not effective at the funding cut"
            ) from error
        if effective != version:
            raise PerpetualFundingError(
                "funding evidence is not bound to the exact effective instrument version"
            )
        if version.asset_class != "PERPETUAL":
            raise PerpetualFundingError(
                "funding evidence instrument_version must identify a PERPETUAL"
            )
        if version.provider_id.upper() != observation.provider_id:
            raise PerpetualFundingError(
                "funding instrument provider does not match sealed provider evidence"
            )
        if version.provider_symbol != observation.instrument_id:
            raise PerpetualFundingError(
                "funding provider instrument does not match canonical instrument version"
            )
        if version.payoff not in {"LINEAR", "INVERSE"}:
            raise PerpetualFundingError("perpetual payoff is not canonically qualified")

        contract = PerpetualContract(
            instrument_id=version.provider_symbol,
            settlement_currency=version.settlement_currency,
            collateral_currency=observation.collateral_currency,
            multiplier=version.contract_multiplier,
            payoff=version.payoff,
            face_currency=(
                version.quote_currency if version.payoff == "INVERSE" else None
            ),
            price_quote_currency=(
                version.quote_currency if version.payoff == "INVERSE" else None
            ),
            price_base_currency=(
                version.base_currency if version.payoff == "INVERSE" else None
            ),
        )
        contract_digest = payload_digest(
            {
                "schema_version": "1.0.0",
                "instrument_version": observation.instrument_version,
                "instrument_contract": version.to_contract_dict(),
                "provider_collateral_currency": observation.collateral_currency,
            }
        )
        return version, contract, contract_digest

    def _position_cut(
        self,
        observation: PerpetualFundingObservation,
    ) -> FundingPositionCut:
        """Project position using only durable economics causally known by observation."""

        self.economic_book.refresh()
        book_digest = self.economic_book.audit_digest()
        journal_sequence = self.store.current_journal_sequence()
        instrument = observation.instrument_id
        position_account = f"POSITION:{instrument}"
        position = Decimal("0")
        transaction_ids: list[str] = []
        transaction_digests: list[str] = []

        for transaction in self.economic_book.transactions:
            position_postings = tuple(
                item
                for item in transaction.postings
                if item.ledger_account == position_account
                and item.asset_or_currency == instrument
            )
            if not position_postings:
                continue
            if (
                transaction.economic_effective_at is None
                or transaction.observed_at is None
            ):
                raise PerpetualFundingConflict(
                    "canonical position history lacks causal economic ordering evidence"
                )
            try:
                effective_at = datetime.fromisoformat(
                    transaction.economic_effective_at.replace("Z", "+00:00")
                ).astimezone(timezone.utc)
                observed_at = datetime.fromisoformat(
                    transaction.observed_at.replace("Z", "+00:00")
                ).astimezone(timezone.utc)
            except ValueError as error:
                raise PerpetualFundingConflict(
                    "canonical position history contains invalid causal timestamps"
                ) from error

            if (
                effective_at <= observation.effective_at
                and observed_at <= observation.observed_at
            ):
                position += sum(
                    (item.signed_amount for item in position_postings),
                    Decimal("0"),
                )
                transaction_ids.append(transaction.transaction_id)
                transaction_digests.append(transaction_digest(transaction))

        material = {
            "schema_version": "1.0.0",
            "instrument": instrument,
            "effective_at": _utc_text(observation.effective_at),
            "evidence_observed_at": _utc_text(observation.observed_at),
            "position": format(position, "f"),
            "economic_book_digest": book_digest,
            "journal_sequence": journal_sequence,
            "contributing_transactions": [
                {"transaction_id": transaction_id, "digest": digest}
                for transaction_id, digest in zip(
                    transaction_ids,
                    transaction_digests,
                    strict=True,
                )
            ],
        }
        return FundingPositionCut(
            instrument=instrument,
            effective_at=material["effective_at"],
            evidence_observed_at=material["evidence_observed_at"],
            position=position,
            economic_book_digest=book_digest,
            journal_sequence=journal_sequence,
            contributing_transaction_ids=tuple(transaction_ids),
            contributing_transaction_digests=tuple(transaction_digests),
            digest=payload_digest(material),
        )

    def _transaction(
        self,
        observation: PerpetualFundingObservation,
        contract: PerpetualContract,
        canonical_position: Decimal,
        *,
        cause_event_id: str,
        corrects_transaction_id: str | None = None,
    ) -> tuple[JournalTransaction, Decimal, str]:
        if contract.instrument_id != observation.instrument_id:
            raise PerpetualFundingError("contract instrument does not match funding evidence")
        if contract.payoff != "LINEAR":
            raise PerpetualFundingError(
                "inverse durable funding requires an explicit settlement quantization policy"
            )
        canonical_position = _decimal(canonical_position, "position_at_cut")
        if canonical_position != observation.signed_contracts:
            raise PerpetualFundingConflict(
                "provider funding position does not match canonical position at funding cut"
            )
        snapshot = MarketSnapshot(
            mark_price=observation.mark_price,
            index_price=observation.index_price,
            observed_at=observation.price_reference_at,
            max_age=timedelta(microseconds=1),
            max_mark_index_deviation=Decimal("1"),
        )
        convention = FundingConvention(
            observation.positive_rate_effect, observation.price_basis
        )
        currency, amount = funding_cashflow(
            contract=contract,
            signed_contracts=canonical_position,
            funding_rate=observation.funding_rate,
            snapshot=snapshot,
            convention=convention,
            at=observation.effective_at,
        )
        transaction_id = str(
            uuid5(
                NAMESPACE_URL,
                "https://events.autotrade.local/perpetual-funding-economic/"
                + _identity(
                    "funding",
                    observation.provider_id,
                    observation.account_id,
                    observation.environment,
                    observation.external_event_id,
                ),
            )
        )
        transaction = JournalTransaction(
            transaction_id=transaction_id,
            cause_event_id=cause_event_id,
            postings=(
                posting(f"CASH:{currency}", currency, amount),
                posting(f"FUNDING_PNL:{currency}", currency, -amount),
            ),
            economic_effective_at=_utc_text(observation.effective_at),
            economic_order_key=(
                f"PERPETUAL_FUNDING:{observation.instrument_version}:"
                f"{observation.funding_period_id}"
            ),
            observed_at=_utc_text(observation.observed_at),
            corrects_transaction_id=corrects_transaction_id,
        )
        return transaction, amount, currency

    def apply(self, evidence_ref: str) -> FundingApplyResult:
        observation, source = self._observation(evidence_ref)
        version, contract, contract_digest = self._contract(observation)

        observation_payload = canonical_perpetual_funding_observation(observation)
        observation_digest = payload_digest(observation_payload)
        provider_evidence_payload = {
            "evidence_ref": source.evidence_ref,
            "response_sha256": source.response_sha256,
            "query_digest": source.query_binding.query_digest,
            "endpoint": source.query_binding.endpoint,
            "permission_scope": source.query_binding.permission_scope,
            "capability_snapshot_id": source.query_binding.capability_snapshot_id,
            "instrument_version": source.query_binding.instrument_version,
            "observed_at": source.observed_at,
        }
        provider_evidence_digest = payload_digest(provider_evidence_payload)
        events = self._events()

        same = [
            event
            for event in events
            if self._payload(event).get("external_event_id")
            == observation.external_event_id
        ]
        if same:
            if len(same) != 1:
                raise PerpetualFundingConflict(
                    "external funding identity appears more than once"
                )
            saved = self._payload(same[0])
            if (
                saved.get("observation_digest") != observation_digest
                or saved.get("provider_evidence_digest") != provider_evidence_digest
                or saved.get("instrument_contract_digest") != contract_digest
            ):
                raise PerpetualFundingConflict(
                    "external funding identity was reused with changed evidence"
                )
            active_id = str(saved["active_transaction_id"])
            economic_ids = {
                item.transaction_id for item in self.economic_book.transactions
            }
            if active_id not in economic_ids:
                raise PerpetualFundingConflict(
                    "funding authority event exists without canonical economics"
                )
            reversal_id = (
                None
                if saved.get("reversal_transaction_id") is None
                else str(saved["reversal_transaction_id"])
            )
            if reversal_id is not None and reversal_id not in economic_ids:
                raise PerpetualFundingConflict(
                    "funding correction event exists without canonical reversal"
                )
            return FundingApplyResult(
                funding_event_id=str(same[0]["event_id"]),
                inserted=False,
                active_transaction_id=active_id,
                reversal_transaction_id=reversal_id,
                cashflow=Decimal(str(saved["cashflow"])),
                currency=str(saved["currency"]),
            )

        same_period = [
            event
            for event in events
            if self._payload(event).get("instrument_version")
            == observation.instrument_version
            and self._payload(event).get("funding_period_id")
            == observation.funding_period_id
        ]
        if observation.corrects_external_event_id is None and same_period:
            raise PerpetualFundingConflict(
                "funding period already has a canonical provider event; "
                "a changed provider fact requires explicit correction lineage"
            )

        prior_event = None
        prior_payload = None
        old_transaction = None
        if observation.corrects_external_event_id is not None:
            matches = [
                event
                for event in events
                if self._payload(event).get("external_event_id")
                == observation.corrects_external_event_id
            ]
            if len(matches) != 1:
                raise PerpetualFundingConflict(
                    "funding correction target must identify exactly one prior event"
                )
            if any(
                self._payload(event).get("corrects_external_event_id")
                == observation.corrects_external_event_id
                for event in events
            ):
                raise PerpetualFundingConflict(
                    "funding correction target already has a replacement"
                )
            prior_event = matches[0]
            prior_payload = self._payload(prior_event)
            for key, expected in (
                ("instrument_version", observation.instrument_version),
                ("funding_period_id", observation.funding_period_id),
                ("effective_at", _utc_text(observation.effective_at)),
            ):
                if prior_payload.get(key) != expected:
                    raise PerpetualFundingConflict(
                        "funding correction cannot change immutable period identity"
                    )
            if prior_payload.get("provider_revision") == observation.provider_revision:
                raise PerpetualFundingConflict(
                    "funding correction requires a new provider revision"
                )
            if (
                prior_payload.get("provider_evidence_ref") == source.evidence_ref
                or prior_payload.get("raw_evidence_digest") == observation.raw_evidence_digest
            ):
                raise PerpetualFundingConflict(
                    "funding correction requires fresh provider evidence"
                )
            by_id = {
                item.transaction_id: item for item in self.economic_book.transactions
            }
            old_id = str(prior_payload.get("active_transaction_id"))
            old_transaction = by_id.get(old_id)
            if old_transaction is None:
                raise PerpetualFundingConflict(
                    "prior funding economics are missing from canonical economic book"
                )

        position_cut = self._position_cut(observation)
        if position_cut.position != observation.signed_contracts:
            raise PerpetualFundingConflict(
                "provider funding position does not match canonical position at funding cut"
            )

        event_id = str(
            uuid5(
                NAMESPACE_URL,
                "https://events.autotrade.local/perpetual-funding/"
                + _identity(
                    "funding-event",
                    observation.provider_id,
                    observation.account_id,
                    observation.environment,
                    observation.external_event_id,
                ),
            )
        )
        replacement, amount, currency = self._transaction(
            observation,
            contract,
            position_cut.position,
            cause_event_id=event_id,
            corrects_transaction_id=(
                None if old_transaction is None else old_transaction.transaction_id
            ),
        )
        reversal = None
        batch = (replacement,)
        if old_transaction is not None:
            reversal = reverse_transaction(
                old_transaction,
                transaction_id=str(
                    uuid5(
                        NAMESPACE_URL,
                        "https://events.autotrade.local/perpetual-funding-reversal/"
                        + _identity(
                            "funding-reversal",
                            observation.provider_id,
                            observation.account_id,
                            observation.environment,
                            observation.external_event_id,
                        ),
                    )
                ),
                cause_event_id=event_id + ":reversal",
                observed_at=_utc_text(observation.observed_at),
            )
            batch = (reversal, replacement)

        economic_plan = self.economic_book.prepare_batch_mutation(
            batch, committed_at=_utc_text(observation.observed_at)
        )
        if economic_plan.already_committed:
            raise PerpetualFundingConflict(
                "funding economics exist without the canonical funding authority event"
            )
        if economic_plan.envelope is None:
            raise PerpetualFundingConflict("fresh funding economics lack durable event")
        economic_payload = economic_plan.envelope.get("payload")
        if (
            not isinstance(economic_payload, Mapping)
            or economic_payload.get("previous_book_digest")
            != position_cut.economic_book_digest
        ):
            self.economic_book.refresh()
            raise PerpetualFundingConflict(
                "economic book changed after the canonical funding position cut"
            )

        next_version = 1 if not events else int(events[-1]["aggregate_version"]) + 1
        payload = {
            "schema_version": "1.0.0",
            "provider_id": observation.provider_id,
            "account_id": observation.account_id,
            "environment": observation.environment,
            "instrument_id": observation.instrument_id,
            "instrument_version": observation.instrument_version,
            "collateral_currency": observation.collateral_currency,
            "external_event_id": observation.external_event_id,
            "corrects_external_event_id": observation.corrects_external_event_id,
            "provider_revision": observation.provider_revision,
            "funding_period_id": observation.funding_period_id,
            "effective_at": _utc_text(observation.effective_at),
            "observed_at": _utc_text(observation.observed_at),
            "raw_evidence_digest": observation.raw_evidence_digest,
            "provider_evidence_ref": source.evidence_ref,
            "provider_evidence_digest": provider_evidence_digest,
            "observation_digest": observation_digest,
            "instrument_contract_digest": contract_digest,
            "position_cut": {
                "digest": position_cut.digest,
                "instrument": position_cut.instrument,
                "effective_at": position_cut.effective_at,
                "evidence_observed_at": position_cut.evidence_observed_at,
                "position": format(position_cut.position, "f"),
                "economic_book_digest": position_cut.economic_book_digest,
                "journal_sequence": position_cut.journal_sequence,
                "contributing_transaction_ids": list(
                    position_cut.contributing_transaction_ids
                ),
                "contributing_transaction_digests": list(
                    position_cut.contributing_transaction_digests
                ),
            },
            "active_transaction_id": replacement.transaction_id,
            "reversal_transaction_id": (
                None if reversal is None else reversal.transaction_id
            ),
            "cashflow": format(amount, "f"),
            "currency": currency,
        }
        envelope = {
            "event_id": event_id,
            "event_type": self._EVENT_TYPE,
            "aggregate_type": self._AGGREGATE_TYPE,
            "aggregate_id": self.aggregate_id,
            "aggregate_version": str(next_version),
            "committed_at": _utc_text(observation.observed_at),
            "payload": payload,
            "payload_hash": payload_digest(payload),
        }
        request = {
            "schema_version": "1.0.0",
            "provider_evidence": provider_evidence_payload,
            "observation": observation_payload,
            "instrument_contract_digest": contract_digest,
            "position_cut_digest": position_cut.digest,
        }
        result = {
            "funding_event_id": event_id,
            "active_transaction_id": replacement.transaction_id,
            "reversal_transaction_id": (
                None if reversal is None else reversal.transaction_id
            ),
            "cashflow": format(amount, "f"),
            "currency": currency,
        }
        command_id = str(
            uuid5(
                NAMESPACE_URL,
                "https://commands.autotrade.local/perpetual-funding/"
                + _identity(
                    "funding-command",
                    observation.provider_id,
                    observation.account_id,
                    observation.environment,
                    observation.external_event_id,
                ),
            )
        )
        try:
            _, inserted, _ = self.store.commit_command(
                command_id=command_id,
                actor=self._ACTOR,
                environment=observation.environment,
                idempotency_key=(
                    "perpetual-funding:"
                    + _identity(
                        "idempotency",
                        observation.provider_id,
                        observation.account_id,
                        observation.environment,
                        observation.external_event_id,
                    )
                ),
                request=request,
                result=result,
                state_version=max(next_version, economic_plan.aggregate_version),
                events=[
                    (envelope, None),
                    (economic_plan.envelope, "autotrade.economic.events"),
                ],
            )
        except Exception:
            self.economic_book.refresh()
            raise
        self.economic_book.refresh()
        return FundingApplyResult(
            funding_event_id=event_id,
            inserted=inserted,
            active_transaction_id=replacement.transaction_id,
            reversal_transaction_id=(
                None if reversal is None else reversal.transaction_id
            ),
            cashflow=amount,
            currency=currency,
        )
