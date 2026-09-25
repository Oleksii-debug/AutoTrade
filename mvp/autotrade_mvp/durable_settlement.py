"""Durable settlement-capital projection for filled economic activity.

This module makes the existing provider-neutral SettlementBook durable through the
canonical JournalStore.  It is not a second ledger: trade-date economics remain
owned by DurableProviderEconomicBook, while this projection answers the narrower
question of which cash is legally/evidentially available for reuse.

The atomic fill integration barrier commits three existing authorities in one
SQLite transaction:
  * reservation consumption,
  * canonical fill economics,
  * pending settlement obligations.

A crash can therefore never consume a reservation and book a sale while losing
the pending receivable that keeps those proceeds unavailable.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import re
from typing import Iterable, Mapping
from uuid import NAMESPACE_URL, UUID, uuid5

from research.autotrade_research.artifacts.store import (
    ArtifactIntegrityError,
    ArtifactStore,
)
from research.autotrade_research.io.strict_json import strict_json_loads

from .accounting import AccountingConflict, JournalTransaction
from .durable_reservations import DurableReservationBook
from .persistence import JournalStore, canonical_json, payload_digest
from .provider_activity_accounting import DurableProviderEconomicBook
from .settlement import (
    SettlementBook,
    SettlementConflict,
    SettlementEvidence,
    SettlementObligation,
)


_ENVIRONMENTS = frozenset({"REPLAY", "SIMULATION", "PAPER", "LIVE"})
_AGGREGATE_TYPE = "settlement_book"
_EVENT_TYPE = "SettlementMutationCommitted"
_ACTOR = "autotrade-settlement-authority"
_INSTRUMENT_VERSION = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}@[1-9][0-9]*$"
)
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
SETTLEMENT_EVIDENCE_MEDIA_TYPE = "application/vnd.autotrade.settlement-evidence+json"
SETTLEMENT_EVIDENCE_TYPE = "AUTOTRADE_SETTLEMENT_EVIDENCE"
SETTLEMENT_EVIDENCE_SCHEMA_VERSION = 1


def _text(value: str, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{name} must be canonical non-empty text")
    return value


def _environment(value: str) -> str:
    environment = _text(value, name="environment").upper()
    if environment not in _ENVIRONMENTS:
        raise ValueError("unsupported environment")
    return environment


def _decimal(value: Decimal | str | int, *, name: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError(f"{name} must use exact decimal input")
    try:
        result = value if isinstance(value, Decimal) else Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError(f"{name} must be a finite decimal") from error
    if not result.is_finite():
        raise ValueError(f"{name} must be a finite decimal")
    return result


def _decimal_text(value: Decimal) -> str:
    value = _decimal(value, name="amount")
    if value == 0:
        return "0"
    rendered = format(value.normalize(), "f")
    return rendered


def _date_text(value: date, *, name: str) -> str:
    if type(value) is not date:
        raise TypeError(f"{name} must be a date")
    return value.isoformat()


def _date_value(value: object, *, name: str) -> date:
    if not isinstance(value, str):
        raise SettlementConflict(f"{name} must be an ISO date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise SettlementConflict(f"{name} must be an ISO date") from error
    if parsed.isoformat() != value:
        raise SettlementConflict(f"{name} must be a canonical ISO date")
    return parsed


def _utc(value: datetime, *, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _utc_value(value: object, *, name: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise SettlementConflict(f"{name} must be a UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise SettlementConflict(f"{name} must be a UTC timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SettlementConflict(f"{name} must include timezone")
    return parsed.astimezone(timezone.utc)


def _instrument_version(value: str) -> str:
    value = _text(value, name="instrument_version")
    if _INSTRUMENT_VERSION.fullmatch(value) is None:
        raise ValueError("instrument_version must be canonical instrument_id@version")
    instrument_id, version = value.split("@", 1)
    try:
        if str(UUID(instrument_id)) != instrument_id:
            raise ValueError
    except (TypeError, ValueError, AttributeError) as error:
        raise ValueError("instrument_version must use canonical UUID identity") from error
    if str(int(version)) != version:
        raise ValueError("instrument_version must use canonical version number")
    return value


def _immutable_ref(value: str, *, name: str) -> str:
    reference = _text(value, name=name)
    if reference.startswith("provider-read:") and _SHA256.fullmatch(reference[len("provider-read:"):]):
        return reference
    marker = "@sha256:"
    if reference.startswith("artifact:") and marker in reference:
        artifact_id, digest = reference[len("artifact:"):].split(marker, 1)
        try:
            artifact_id = str(UUID(artifact_id))
        except (ValueError, TypeError, AttributeError) as error:
            raise ValueError(f"{name} artifact identity must be a UUID") from error
        if len(digest) == 64 and all(ch in "0123456789abcdef" for ch in digest):
            canonical = f"artifact:{artifact_id}@sha256:{digest}"
            if canonical == reference:
                return reference
    raise ValueError(
        f"{name} must be an immutable provider-read or artifact SHA-256 reference"
    )


def _artifact_ref(value: str, *, name: str) -> tuple[str, str, str]:
    reference = _text(value, name=name)
    marker = "@sha256:"
    if not reference.startswith("artifact:") or marker not in reference:
        raise SettlementConflict(
            f"{name} must bind a trusted ArtifactStore UUID and SHA-256 digest"
        )
    artifact_id, digest = reference[len("artifact:"):].split(marker, 1)
    try:
        artifact_id = str(UUID(artifact_id))
    except (ValueError, TypeError, AttributeError) as error:
        raise SettlementConflict(f"{name} artifact identity must be a UUID") from error
    if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
        raise SettlementConflict(f"{name} must use canonical lowercase SHA-256")
    canonical = f"artifact:{artifact_id}@sha256:{digest}"
    if canonical != reference:
        raise SettlementConflict(f"{name} must be canonical")
    return artifact_id, digest, canonical


def _verify_evidence_artifact(
    artifact_store: ArtifactStore,
    *,
    evidence_ref: str,
    expected_receipt: Mapping[str, object],
    expected_metadata: Mapping[str, object],
    name: str,
) -> str:
    if not isinstance(artifact_store, ArtifactStore):
        raise SettlementConflict(
            f"{name} requires trusted ArtifactStore"
        )
    artifact_id, digest, canonical_ref = _artifact_ref(
        evidence_ref, name=f"{name} evidence_ref"
    )
    try:
        manifest = artifact_store.load_manifest(artifact_id)
        manifest_hash = manifest.get("manifest_hash")
        if (
            not isinstance(manifest_hash, str)
            or not manifest_hash.startswith("sha256:")
            or len(manifest_hash) != 71
        ):
            raise ArtifactIntegrityError(
                f"{name} artifact manifest lacks integrity binding"
            )
        if manifest.get("sha256") != f"sha256:{digest}":
            raise ArtifactIntegrityError(
                f"{name} artifact digest does not match manifest"
            )
        if manifest.get("media_type") != SETTLEMENT_EVIDENCE_MEDIA_TYPE:
            raise ArtifactIntegrityError(
                f"{name} artifact has unsupported media type"
            )
        if manifest.get("metadata") != dict(expected_metadata):
            raise ArtifactIntegrityError(
                f"{name} artifact metadata differs from financial scope"
            )
        rights = manifest.get("rights")
        if not isinstance(rights, dict) or rights.get("storage") is not True:
            raise ArtifactIntegrityError(
                f"{name} artifact lacks storage provenance"
            )
        raw = artifact_store.read_bytes(artifact_id)
        parsed = strict_json_loads(raw.decode("utf-8"))
    except (
        ArtifactIntegrityError,
        FileNotFoundError,
        UnicodeError,
        ValueError,
        TypeError,
    ) as error:
        raise SettlementConflict(
            f"{name} artifact verification failed"
        ) from error
    expected = dict(expected_receipt)
    if parsed != expected:
        raise SettlementConflict(
            f"{name} artifact does not match supplied financial semantics"
        )
    if raw != canonical_json(expected).encode("utf-8"):
        raise SettlementConflict(
            f"{name} artifact must use canonical JSON bytes"
        )
    return canonical_ref


def _scope_id(provider_id: str, account_id: str, environment: str) -> str:
    digest = payload_digest(
        {
            "schema_version": "1.0.0",
            "provider_id": provider_id,
            "account_id": account_id,
            "environment": environment,
        }
    ).removeprefix("sha256:")
    return str(uuid5(NAMESPACE_URL, "https://identity.autotrade.local/settlement/" + digest))


def _identity(kind: str, *parts: str) -> str:
    material = canonical_json([kind, *parts])
    return str(uuid5(NAMESPACE_URL, "https://identity.autotrade.local/" + material))


def _opening_payload(values: Mapping[str, Decimal | str | int]) -> dict[str, str]:
    if not isinstance(values, Mapping):
        raise TypeError("opening_settled_cash must be a mapping")
    normalized: dict[str, str] = {}
    for raw_currency, raw_amount in values.items():
        currency = _text(raw_currency, name="opening currency").upper()
        if currency in normalized:
            raise ValueError("duplicate normalized opening currency")
        normalized[currency] = _decimal_text(_decimal(raw_amount, name="opening cash"))
    return dict(sorted(normalized.items()))


@dataclass(frozen=True, slots=True)
class SettlementRuleEvidence:
    """Immutable binding for the expected settlement date of one filled trade.

    The evidence reference is provenance, not an invented global T+N rule.  The
    durable layer binds it to exact provider/account/environment/instrument
    scope and to the resulting contractual dates.
    """

    provider_id: str
    account_id: str
    environment: str
    instrument_version: str
    trade_date: date
    settlement_date: date
    evidence_ref: str

    def __post_init__(self) -> None:
        provider = _text(self.provider_id, name="provider_id").upper()
        account = _text(self.account_id, name="account_id")
        environment = _environment(self.environment)
        instrument = _instrument_version(self.instrument_version)
        trade = self.trade_date
        settlement = self.settlement_date
        if type(trade) is not date or type(settlement) is not date:
            raise TypeError("trade_date and settlement_date must be date values")
        if settlement < trade:
            raise ValueError("settlement_date cannot precede trade_date")
        reference = _immutable_ref(self.evidence_ref, name="settlement rule evidence_ref")
        object.__setattr__(self, "provider_id", provider)
        object.__setattr__(self, "account_id", account)
        object.__setattr__(self, "environment", environment)
        object.__setattr__(self, "instrument_version", instrument)
        object.__setattr__(self, "evidence_ref", reference)


@dataclass(frozen=True)
class PreparedSettlementMutation:
    envelope: dict[str, object] | None
    request: dict[str, object]
    result: dict[str, object]
    aggregate_version: int
    already_committed: bool = False


@dataclass(frozen=True, slots=True)
class AvailableCapitalSnapshot:
    provider_id: str
    account_id: str
    environment: str
    currency: str
    settled_cash: Decimal
    unsettled_receivable: Decimal
    unsettled_payable: Decimal
    reservation_hold: Decimal
    available_cash: Decimal
    settlement_state_digest: str


def _obligation_payload(obligation: SettlementObligation) -> dict[str, object]:
    return {
        "obligation_id": obligation.obligation_id,
        "cause_event_id": obligation.cause_event_id,
        "currency": obligation.currency,
        "amount": _decimal_text(obligation.amount),
        "trade_date": obligation.trade_date.isoformat(),
        "settlement_date": obligation.settlement_date.isoformat(),
        "component_id": obligation.component_id,
    }


def _obligation_from_payload(value: object) -> SettlementObligation:
    if not isinstance(value, Mapping):
        raise SettlementConflict("durable settlement obligation must be an object")
    return SettlementObligation(
        obligation_id=value.get("obligation_id"),
        cause_event_id=value.get("cause_event_id"),
        currency=value.get("currency"),
        amount=value.get("amount"),
        trade_date=_date_value(value.get("trade_date"), name="trade_date"),
        settlement_date=_date_value(value.get("settlement_date"), name="settlement_date"),
        component_id=value.get("component_id"),
    )


def _rule_payload(rule: SettlementRuleEvidence) -> dict[str, object]:
    return {
        "provider_id": rule.provider_id,
        "account_id": rule.account_id,
        "environment": rule.environment,
        "instrument_version": rule.instrument_version,
        "trade_date": rule.trade_date.isoformat(),
        "settlement_date": rule.settlement_date.isoformat(),
        "evidence_ref": rule.evidence_ref,
    }


def _rule_from_payload(value: object) -> SettlementRuleEvidence:
    if not isinstance(value, Mapping):
        raise SettlementConflict("settlement rule payload must be an object")
    return SettlementRuleEvidence(
        provider_id=value.get("provider_id"),
        account_id=value.get("account_id"),
        environment=value.get("environment"),
        instrument_version=value.get("instrument_version"),
        trade_date=_date_value(value.get("trade_date"), name="rule.trade_date"),
        settlement_date=_date_value(
            value.get("settlement_date"), name="rule.settlement_date"
        ),
        evidence_ref=value.get("evidence_ref"),
    )


def settlement_rule_evidence_receipt(
    rule: SettlementRuleEvidence,
) -> dict[str, object]:
    if not isinstance(rule, SettlementRuleEvidence):
        raise TypeError("rule must be SettlementRuleEvidence")
    value = _rule_payload(rule)
    value.pop("evidence_ref", None)
    return {
        "schema_version": SETTLEMENT_EVIDENCE_SCHEMA_VERSION,
        "evidence_type": SETTLEMENT_EVIDENCE_TYPE,
        "observation_kind": "SETTLEMENT_RULE",
        "observation": value,
    }


def settlement_rule_evidence_metadata(
    rule: SettlementRuleEvidence,
) -> dict[str, object]:
    return {
        "evidence_type": SETTLEMENT_EVIDENCE_TYPE,
        "observation_kind": "SETTLEMENT_RULE",
        "provider_id": rule.provider_id,
        "account_id": rule.account_id,
        "environment": rule.environment,
        "instrument_version": rule.instrument_version,
        "trade_date": rule.trade_date.isoformat(),
        "settlement_date": rule.settlement_date.isoformat(),
    }


def verify_settlement_rule_evidence(
    rule: SettlementRuleEvidence,
    artifact_store: ArtifactStore,
) -> str:
    return _verify_evidence_artifact(
        artifact_store,
        evidence_ref=rule.evidence_ref,
        expected_receipt=settlement_rule_evidence_receipt(rule),
        expected_metadata=settlement_rule_evidence_metadata(rule),
        name="settlement rule",
    )


def settlement_completion_evidence_receipt(
    *,
    provider_id: str,
    account_id: str,
    environment: str,
    obligation: SettlementObligation,
    evidence: SettlementEvidence,
) -> dict[str, object]:
    if not isinstance(obligation, SettlementObligation):
        raise TypeError("obligation must be SettlementObligation")
    if not isinstance(evidence, SettlementEvidence):
        raise TypeError("evidence must be SettlementEvidence")
    return {
        "schema_version": SETTLEMENT_EVIDENCE_SCHEMA_VERSION,
        "evidence_type": SETTLEMENT_EVIDENCE_TYPE,
        "observation_kind": "SETTLEMENT_COMPLETION",
        "observation": {
            "provider_id": _text(provider_id, name="provider_id").upper(),
            "account_id": _text(account_id, name="account_id"),
            "environment": _environment(environment),
            "obligation_id": obligation.obligation_id,
            "cause_event_id": obligation.cause_event_id,
            "currency": obligation.currency,
            "signed_amount": _decimal_text(obligation.amount),
            "trade_date": obligation.trade_date.isoformat(),
            "settlement_date": obligation.settlement_date.isoformat(),
            "component_id": obligation.component_id,
            "observed_at": _utc_text(evidence.observed_at),
        },
    }


def settlement_completion_evidence_metadata(
    *,
    provider_id: str,
    account_id: str,
    environment: str,
    obligation: SettlementObligation,
    evidence: SettlementEvidence,
) -> dict[str, object]:
    return {
        "evidence_type": SETTLEMENT_EVIDENCE_TYPE,
        "observation_kind": "SETTLEMENT_COMPLETION",
        "provider_id": _text(provider_id, name="provider_id").upper(),
        "account_id": _text(account_id, name="account_id"),
        "environment": _environment(environment),
        "obligation_id": obligation.obligation_id,
        "cause_event_id": obligation.cause_event_id,
        "currency": obligation.currency,
        "signed_amount": _decimal_text(obligation.amount),
        "settlement_date": obligation.settlement_date.isoformat(),
        "observed_at": _utc_text(evidence.observed_at),
    }


def verify_settlement_completion_evidence(
    *,
    provider_id: str,
    account_id: str,
    environment: str,
    obligation: SettlementObligation,
    evidence: SettlementEvidence,
    artifact_store: ArtifactStore,
) -> str:
    if evidence.obligation_id != obligation.obligation_id:
        raise SettlementConflict(
            "settlement completion evidence identity does not match obligation"
        )
    return _verify_evidence_artifact(
        artifact_store,
        evidence_ref=evidence.evidence_ref,
        expected_receipt=settlement_completion_evidence_receipt(
            provider_id=provider_id,
            account_id=account_id,
            environment=environment,
            obligation=obligation,
            evidence=evidence,
        ),
        expected_metadata=settlement_completion_evidence_metadata(
            provider_id=provider_id,
            account_id=account_id,
            environment=environment,
            obligation=obligation,
            evidence=evidence,
        ),
        name="settlement completion",
    )


def _evidence_payload(evidence: SettlementEvidence) -> dict[str, object]:
    return {
        "obligation_id": evidence.obligation_id,
        "evidence_ref": _immutable_ref(
            evidence.evidence_ref,
            name="settlement evidence_ref",
        ),
        "observed_at": _utc_text(evidence.observed_at),
    }


def _evidence_from_payload(value: object) -> SettlementEvidence:
    if not isinstance(value, Mapping):
        raise SettlementConflict("durable settlement evidence must be an object")
    return SettlementEvidence(
        obligation_id=value.get("obligation_id"),
        evidence_ref=_immutable_ref(
            value.get("evidence_ref"),
            name="settlement evidence_ref",
        ),
        observed_at=_utc_value(value.get("observed_at"), name="observed_at"),
    )


class DurableSettlementBook:
    """Journal-backed projection of pending obligations and spendable settled cash."""

    def __init__(
        self,
        store: JournalStore,
        *,
        provider_id: str,
        account_id: str,
        environment: str,
        opening_settled_cash: Mapping[str, Decimal | str | int],
        evidence_artifact_store: ArtifactStore,
    ) -> None:
        if not isinstance(store, JournalStore):
            raise TypeError("store must be JournalStore")
        self.store = store
        self.provider_id = _text(provider_id, name="provider_id").upper()
        self.account_id = _text(account_id, name="account_id")
        self.environment = _environment(environment)
        if not isinstance(evidence_artifact_store, ArtifactStore):
            raise TypeError("evidence_artifact_store must be trusted ArtifactStore")
        self.evidence_artifact_store = evidence_artifact_store
        self._opening = _opening_payload(opening_settled_cash)
        self._opening_digest = payload_digest(self._opening)
        self.scope_id = _scope_id(self.provider_id, self.account_id, self.environment)
        self._book = SettlementBook()
        self._idempotency: dict[str, tuple[str, dict[str, object]]] = {}
        self._rules: dict[str, dict[str, object]] = {}
        self._reload()

    def _events(self) -> list[dict[str, object]]:
        return self.store.load_events(_AGGREGATE_TYPE, self.scope_id)

    def _new_book(self) -> SettlementBook:
        return SettlementBook(
            settled_cash={currency: Decimal(amount) for currency, amount in self._opening.items()}
        )

    @staticmethod
    def _projection_payload(book: SettlementBook) -> dict[str, object]:
        obligations = sorted(
            (_obligation_payload(item) for item in book.obligations),
            key=lambda item: str(item["obligation_id"]),
        )
        currencies = sorted({str(item["currency"]) for item in obligations})
        currencies.extend(
            currency
            for currency, _amount in book.checkpoint("projection").settled_cash
            if currency not in currencies
        )
        snapshots = {}
        for currency in sorted(set(currencies)):
            item = book.snapshot(currency)
            snapshots[currency] = {
                "settled_cash": _decimal_text(item.settled_cash),
                "unsettled_receivable": _decimal_text(item.unsettled_receivable),
                "unsettled_payable": _decimal_text(item.unsettled_payable),
                "available_cash": _decimal_text(book.available_to_spend(currency)),
            }
        evidence = {
            key: _evidence_payload(value)
            for key, value in sorted(book.settled_obligation_evidence.items())
        }
        return {
            "obligations": obligations,
            "settlement_evidence": evidence,
            "snapshots": snapshots,
        }

    def _replay(
        self,
        events: list[dict[str, object]],
    ) -> tuple[SettlementBook, dict[str, tuple[str, dict[str, object]]], dict[str, dict[str, object]]]:
        book = self._new_book()
        idempotency: dict[str, tuple[str, dict[str, object]]] = {}
        rules: dict[str, dict[str, object]] = {}
        expected_version = 1
        for event in events:
            if event.get("aggregate_version") != expected_version:
                raise SettlementConflict("settlement journal aggregate versions are not contiguous")
            expected_version += 1
            if event.get("event_type") != _EVENT_TYPE:
                raise SettlementConflict("settlement journal contains unsupported event type")
            payload = event.get("payload")
            if not isinstance(payload, Mapping):
                raise SettlementConflict("settlement journal payload must be an object")
            if payload_digest(payload) != event.get("payload_hash"):
                raise SettlementConflict("settlement journal payload hash is invalid")
            if (
                payload.get("provider_id") != self.provider_id
                or payload.get("account_id") != self.account_id
                or payload.get("environment") != self.environment
                or payload.get("opening_settled_cash_digest") != self._opening_digest
            ):
                raise SettlementConflict("settlement journal scope/opening cash binding is invalid")
            idem = payload.get("idempotency_key")
            request = payload.get("request")
            result = payload.get("result")
            if not isinstance(idem, str) or not idem:
                raise SettlementConflict("settlement journal idempotency key is invalid")
            if not isinstance(request, Mapping) or not isinstance(result, Mapping):
                raise SettlementConflict("settlement journal request/result must be objects")
            request_dict = dict(request)
            result_dict = dict(result)
            request_hash = payload_digest(request_dict)
            if payload.get("request_hash") != request_hash:
                raise SettlementConflict("settlement journal request hash is invalid")
            prior = idempotency.get(idem)
            if prior is not None:
                raise SettlementConflict("duplicate settlement idempotency event must not be appended")

            operation = payload.get("operation")
            if operation == "ADD_OBLIGATIONS":
                raw_items = request_dict.get("items")
                if not isinstance(raw_items, list) or not raw_items:
                    raise SettlementConflict("settlement obligation batch must be non-empty")
                for raw_item in raw_items:
                    if not isinstance(raw_item, Mapping):
                        raise SettlementConflict("settlement obligation item must be an object")
                    obligation = _obligation_from_payload(raw_item.get("obligation"))
                    rule = raw_item.get("rule")
                    if not isinstance(rule, Mapping):
                        raise SettlementConflict("settlement rule binding must be an object")
                    if (
                        rule.get("provider_id") != self.provider_id
                        or rule.get("account_id") != self.account_id
                        or rule.get("environment") != self.environment
                        or rule.get("trade_date") != obligation.trade_date.isoformat()
                        or rule.get("settlement_date") != obligation.settlement_date.isoformat()
                    ):
                        raise SettlementConflict("settlement rule scope/date binding is invalid")
                    rule_evidence = _rule_from_payload(rule)
                    verify_settlement_rule_evidence(
                        rule_evidence,
                        self.evidence_artifact_store,
                    )
                    inserted = book.add(obligation)
                    if not inserted:
                        raise SettlementConflict(
                            "durable settlement journal repeats obligation under a new event"
                        )
                    rules[obligation.obligation_id] = dict(rule)
            elif operation == "SETTLE":
                obligation_id = request_dict.get("obligation_id")
                evidence = _evidence_from_payload(request_dict.get("evidence"))
                as_of = _date_value(request_dict.get("as_of"), name="as_of")
                if evidence.obligation_id != obligation_id:
                    raise SettlementConflict("settlement evidence identity mismatch")
                obligation = next(
                    (
                        item
                        for item in book.obligations
                        if item.obligation_id == obligation_id
                    ),
                    None,
                )
                if obligation is None:
                    raise SettlementConflict(
                        "settlement evidence references unknown obligation"
                    )
                verify_settlement_completion_evidence(
                    provider_id=self.provider_id,
                    account_id=self.account_id,
                    environment=self.environment,
                    obligation=obligation,
                    evidence=evidence,
                    artifact_store=self.evidence_artifact_store,
                )
                inserted = book.settle(
                    obligation_id,
                    as_of=as_of,
                    settlement_evidence=evidence,
                )
                if not inserted:
                    raise SettlementConflict(
                        "durable settlement journal repeats settlement under a new event"
                    )
            else:
                raise SettlementConflict("unsupported durable settlement operation")

            actual = self._projection_payload(book)
            if result_dict != actual:
                raise SettlementConflict(
                    "settlement journal result does not match replayed projection"
                )
            idempotency[idem] = (request_hash, result_dict)
        return book, idempotency, rules

    def _reload(self) -> None:
        self._book, self._idempotency, self._rules = self._replay(self._events())

    def refresh(self) -> None:
        self._reload()

    @property
    def obligations(self) -> tuple[SettlementObligation, ...]:
        return self._book.obligations

    def snapshot(self, currency: str):
        return self._book.snapshot(currency)

    def available_to_spend(
        self,
        currency: str,
        *,
        reservation_hold: Decimal | str | int = 0,
    ) -> Decimal:
        return self._book.available_to_spend(currency, reserve=reservation_hold)

    def state_digest(self) -> str:
        return payload_digest(
            {
                "schema_version": "1.0.0",
                "provider_id": self.provider_id,
                "account_id": self.account_id,
                "environment": self.environment,
                "opening_settled_cash_digest": self._opening_digest,
                "projection": self._projection_payload(self._book),
                "rules": {
                    key: value for key, value in sorted(self._rules.items())
                },
            }
        )

    def capital_snapshot(
        self,
        currency: str,
        *,
        reservation_hold: Decimal | str | int = 0,
    ) -> AvailableCapitalSnapshot:
        unit = _text(currency, name="currency").upper()
        hold = _decimal(reservation_hold, name="reservation_hold")
        if hold < 0:
            raise ValueError("reservation_hold cannot be negative")
        snapshot = self._book.snapshot(unit)
        return AvailableCapitalSnapshot(
            provider_id=self.provider_id,
            account_id=self.account_id,
            environment=self.environment,
            currency=unit,
            settled_cash=snapshot.settled_cash,
            unsettled_receivable=snapshot.unsettled_receivable,
            unsettled_payable=snapshot.unsettled_payable,
            reservation_hold=hold,
            available_cash=self._book.available_to_spend(unit, reserve=hold),
            settlement_state_digest=self.state_digest(),
        )

    def _prepare(
        self,
        *,
        operation: str,
        idempotency_key: str,
        event_key: str,
        request: dict[str, object],
        committed_at: str,
    ) -> PreparedSettlementMutation:
        idem = _text(idempotency_key, name="idempotency_key")
        event_key = _text(event_key, name="event_key")
        committed_at = _text(committed_at, name="committed_at")
        events = self._events()
        candidate, idempotency, rules = self._replay(events)
        existing = idempotency.get(idem)
        request_hash = payload_digest(request)
        if existing is not None:
            if existing[0] != request_hash:
                raise SettlementConflict(
                    "idempotency_key was already used for a different settlement request"
                )
            return PreparedSettlementMutation(
                envelope=None,
                request=request,
                result=existing[1],
                aggregate_version=0 if not events else int(events[-1]["aggregate_version"]),
                already_committed=True,
            )

        if operation == "ADD_OBLIGATIONS":
            items = request["items"]
            assert isinstance(items, list)
            for item in items:
                assert isinstance(item, dict)
                obligation = _obligation_from_payload(item["obligation"])
                rule = item["rule"]
                assert isinstance(rule, dict)
                inserted = candidate.add(obligation)
                if not inserted:
                    raise SettlementConflict(
                        "settlement obligation is already committed under another idempotency key"
                    )
                rules[obligation.obligation_id] = rule
        elif operation == "SETTLE":
            evidence = _evidence_from_payload(request["evidence"])
            inserted = candidate.settle(
                request["obligation_id"],
                as_of=_date_value(request["as_of"], name="as_of"),
                settlement_evidence=evidence,
            )
            if not inserted:
                raise SettlementConflict(
                    "settlement is already committed under another idempotency key"
                )
        else:
            raise SettlementConflict("unsupported settlement operation")

        result = self._projection_payload(candidate)
        next_version = 1 if not events else int(events[-1]["aggregate_version"]) + 1
        payload = {
            "schema_version": "1.0.0",
            "provider_id": self.provider_id,
            "account_id": self.account_id,
            "environment": self.environment,
            "opening_settled_cash_digest": self._opening_digest,
            "operation": operation,
            "idempotency_key": idem,
            "request": request,
            "request_hash": request_hash,
            "result": result,
        }
        envelope = {
            "event_id": _identity(
                "settlement-event",
                self.provider_id,
                self.account_id,
                self.environment,
                event_key,
            ),
            "event_type": _EVENT_TYPE,
            "aggregate_type": _AGGREGATE_TYPE,
            "aggregate_id": self.scope_id,
            "aggregate_version": str(next_version),
            "committed_at": committed_at,
            "payload": payload,
            "payload_hash": payload_digest(payload),
        }
        return PreparedSettlementMutation(
            envelope=envelope,
            request=request,
            result=result,
            aggregate_version=next_version,
        )

    def prepare_add_obligations(
        self,
        *,
        idempotency_key: str,
        event_key: str,
        obligations: Iterable[tuple[SettlementObligation, SettlementRuleEvidence]],
        committed_at: str,
    ) -> PreparedSettlementMutation:
        materialized = tuple(obligations)
        if not materialized:
            raise ValueError("at least one settlement obligation is required")
        items: list[dict[str, object]] = []
        identities: set[str] = set()
        for obligation, rule in materialized:
            if not isinstance(obligation, SettlementObligation):
                raise TypeError("obligation must be SettlementObligation")
            if not isinstance(rule, SettlementRuleEvidence):
                raise TypeError("rule evidence must be SettlementRuleEvidence")
            if (
                rule.provider_id != self.provider_id
                or rule.account_id != self.account_id
                or rule.environment != self.environment
            ):
                raise SettlementConflict("settlement rule scope does not match durable book")
            if (
                rule.trade_date != obligation.trade_date
                or rule.settlement_date != obligation.settlement_date
            ):
                raise SettlementConflict("settlement rule dates do not match obligation")
            verify_settlement_rule_evidence(
                rule,
                self.evidence_artifact_store,
            )
            if obligation.obligation_id in identities:
                raise SettlementConflict("duplicate obligation_id in settlement batch")
            identities.add(obligation.obligation_id)
            items.append(
                {
                    "obligation": _obligation_payload(obligation),
                    "rule": _rule_payload(rule),
                }
            )
        items.sort(key=lambda item: str(item["obligation"]["obligation_id"]))
        return self._prepare(
            operation="ADD_OBLIGATIONS",
            idempotency_key=idempotency_key,
            event_key=event_key,
            request={"items": items},
            committed_at=committed_at,
        )

    def settle(
        self,
        *,
        command_id: str,
        idempotency_key: str,
        obligation_id: str,
        as_of: date,
        evidence: SettlementEvidence,
        committed_at: str,
    ) -> bool:
        if not isinstance(evidence, SettlementEvidence):
            raise TypeError("evidence must be SettlementEvidence")
        target = next(
            (
                item
                for item in self._book.obligations
                if item.obligation_id == obligation_id
            ),
            None,
        )
        if target is None:
            raise SettlementConflict("unknown settlement obligation")
        verify_settlement_completion_evidence(
            provider_id=self.provider_id,
            account_id=self.account_id,
            environment=self.environment,
            obligation=target,
            evidence=evidence,
            artifact_store=self.evidence_artifact_store,
        )
        request = {
            "obligation_id": _text(obligation_id, name="obligation_id"),
            "as_of": _date_text(as_of, name="as_of"),
            "evidence": _evidence_payload(evidence),
        }
        plan = self._prepare(
            operation="SETTLE",
            idempotency_key=idempotency_key,
            event_key=command_id,
            request=request,
            committed_at=committed_at,
        )
        if plan.already_committed:
            self._reload()
            return False
        if plan.envelope is None:
            raise SettlementConflict("fresh settlement mutation is missing durable event")
        try:
            _, inserted, _ = self.store.commit_command(
                command_id=_identity(
                    "settlement-command",
                    self.provider_id,
                    self.account_id,
                    self.environment,
                    _text(command_id, name="command_id"),
                ),
                actor=_ACTOR,
                environment=self.environment,
                idempotency_key="settlement:" + _identity(
                    "settlement-idempotency",
                    self.provider_id,
                    self.account_id,
                    self.environment,
                    _text(idempotency_key, name="idempotency_key"),
                ),
                request=plan.request,
                result=plan.result,
                state_version=plan.aggregate_version,
                events=[(plan.envelope, None)],
            )
        except Exception:
            self._reload()
            raise
        self._reload()
        return inserted


def _cash_postings_by_cause(
    transactions: Iterable[JournalTransaction],
) -> dict[tuple[str, str], Decimal]:
    totals: dict[tuple[str, str], Decimal] = {}
    for transaction in transactions:
        if not isinstance(transaction, JournalTransaction):
            raise TypeError("transactions must contain JournalTransaction")
        for item in transaction.postings:
            if item.ledger_account == f"CASH:{item.asset_or_currency}":
                key = (transaction.cause_event_id, item.asset_or_currency.upper())
                totals[key] = totals.get(key, Decimal("0")) + item.signed_amount
    return {key: value for key, value in totals.items() if value != 0}


def _obligations_by_cause(
    obligations: Iterable[tuple[SettlementObligation, SettlementRuleEvidence]],
) -> dict[tuple[str, str], Decimal]:
    totals: dict[tuple[str, str], Decimal] = {}
    for obligation, _rule in obligations:
        key = (obligation.cause_event_id, obligation.currency)
        totals[key] = totals.get(key, Decimal("0")) + obligation.amount
    return {key: value for key, value in totals.items() if value != 0}


def commit_fill_with_settlement_obligations(
    economic_book: DurableProviderEconomicBook,
    reservation_book: DurableReservationBook,
    settlement_book: DurableSettlementBook,
    *,
    command_id: str,
    idempotency_key: str,
    reservation_id: str,
    usage: Mapping[str, object],
    transactions: Iterable[JournalTransaction],
    settlement_obligations: Iterable[
        tuple[SettlementObligation, SettlementRuleEvidence]
    ],
    committed_at: str,
) -> bool:
    """Atomically commit fill economics, reservation consumption and settlement.

    This integration barrier proves persistence atomicity only.  Settlement-rule
    provenance remains represented by immutable SettlementRuleEvidence and must
    be sourced by the caller from the canonical instrument/account provider
    authority; this function never invents a market-wide settlement cycle.
    """

    if not isinstance(economic_book, DurableProviderEconomicBook):
        raise TypeError("economic_book must be DurableProviderEconomicBook")
    if not isinstance(reservation_book, DurableReservationBook):
        raise TypeError("reservation_book must be DurableReservationBook")
    if not isinstance(settlement_book, DurableSettlementBook):
        raise TypeError("settlement_book must be DurableSettlementBook")
    if not (
        economic_book.store is reservation_book.store is settlement_book.store
    ):
        raise ValueError("all financial projections must share one JournalStore")
    if (
        economic_book.provider_id != settlement_book.provider_id
        or economic_book.account_id != settlement_book.account_id
        or economic_book.account_id != reservation_book.account_id
        or economic_book.environment != settlement_book.environment
        or economic_book.environment != reservation_book.environment
    ):
        raise ValueError(
            "economic, reservation and settlement projections must share scope"
        )

    cid = _text(command_id, name="command_id")
    idem = _text(idempotency_key, name="idempotency_key")
    when = _text(committed_at, name="committed_at")
    txs = tuple(transactions)
    obligations = tuple(settlement_obligations)
    if not txs or not obligations:
        raise ValueError("fill integration requires economics and settlement obligations")

    economic_cash = _cash_postings_by_cause(txs)
    contractual_cash = _obligations_by_cause(obligations)
    if economic_cash != contractual_cash:
        raise AccountingConflict(
            "settlement obligations must exactly cover fill cash postings by cause/currency"
        )

    reservation_plan = reservation_book.prepare_consume_mutation(
        event_key=_identity(
            "settlement-fill-reservation-event",
            economic_book.provider_id,
            economic_book.account_id,
            economic_book.environment,
            cid,
        ),
        idempotency_key=_identity(
            "settlement-fill-reservation-idempotency",
            economic_book.provider_id,
            economic_book.account_id,
            economic_book.environment,
            idem,
        ),
        reservation_id=_text(reservation_id, name="reservation_id"),
        usage=usage,
        committed_at=when,
    )
    economic_plan = economic_book.prepare_batch_mutation(txs, committed_at=when)
    settlement_plan = settlement_book.prepare_add_obligations(
        idempotency_key=_identity(
            "settlement-fill-obligation-idempotency",
            economic_book.provider_id,
            economic_book.account_id,
            economic_book.environment,
            idem,
        ),
        event_key=_identity(
            "settlement-fill-obligation-event",
            economic_book.provider_id,
            economic_book.account_id,
            economic_book.environment,
            cid,
        ),
        obligations=obligations,
        committed_at=when,
    )

    committed = (
        reservation_plan.already_committed,
        economic_plan.already_committed,
        settlement_plan.already_committed,
    )
    if any(committed) and not all(committed):
        reservation_book.refresh()
        economic_book.refresh()
        settlement_book.refresh()
        raise AccountingConflict(
            "fill financial state is only partially committed across "
            "reservation/economics/settlement"
        )
    if all(committed):
        reservation_book.refresh()
        economic_book.refresh()
        settlement_book.refresh()
        return False
    if (
        reservation_plan.envelope is None
        or economic_plan.envelope is None
        or settlement_plan.envelope is None
    ):
        raise AccountingConflict("fresh atomic fill plan is missing durable events")

    request = {
        "schema_version": "1.0.0",
        "provider_id": economic_book.provider_id,
        "account_id": economic_book.account_id,
        "environment": economic_book.environment,
        "reservation": reservation_plan.request,
        "economic_batch": economic_plan.request,
        "settlement": settlement_plan.request,
    }
    result = {
        "reservation": reservation_plan.snapshot_payload,
        "economic_batch": economic_plan.result,
        "settlement": settlement_plan.result,
    }
    events = [
        (reservation_plan.envelope, None),
        (economic_plan.envelope, "autotrade.economic.events"),
        (settlement_plan.envelope, None),
    ]
    try:
        _, inserted, _ = economic_book.store.commit_command(
            command_id=_identity(
                "settlement-fill-command",
                economic_book.provider_id,
                economic_book.account_id,
                economic_book.environment,
                cid,
            ),
            actor="atomic-fill-settlement-integration",
            environment=economic_book.environment,
            idempotency_key="atomic-fill-settlement:" + _identity(
                "settlement-fill-command-idempotency",
                economic_book.provider_id,
                economic_book.account_id,
                economic_book.environment,
                idem,
            ),
            request=request,
            result=result,
            state_version=max(
                reservation_plan.aggregate_version,
                economic_plan.aggregate_version,
                settlement_plan.aggregate_version,
            ),
            events=events,
        )
    except Exception:
        reservation_book.refresh()
        economic_book.refresh()
        settlement_book.refresh()
        raise

    reservation_book.refresh()
    economic_book.refresh()
    settlement_book.refresh()
    return inserted
