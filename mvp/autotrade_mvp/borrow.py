"""Securities-borrow evidence and recall lifecycle.

This module owns provider borrow inventory evidence and its durable projection.
It deliberately does not own order submission, risk policy, reservation storage,
or financing-fee economics. Borrow capacity is exposed as one canonical resource
identity so the existing DurableReservationBook can consume it atomically with
the rest of a financial admission.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from typing import Mapping, Sequence
from uuid import NAMESPACE_URL, UUID, uuid5

from .persistence import JournalStore, canonical_json, payload_digest
from .risk import RiskContext, RiskIntent


_ENVIRONMENTS = frozenset({"REPLAY", "SIMULATION", "PAPER", "LIVE"})
_AVAILABILITY_STATES = frozenset({"AVAILABLE", "HARD_TO_BORROW", "UNAVAILABLE"})
_AGGREGATE_TYPE = "borrow_lifecycle"


def _text(value: str, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    return value.strip()


def _environment(value: str) -> str:
    normalized = _text(value, name="environment").upper()
    if normalized not in _ENVIRONMENTS:
        raise ValueError("environment must be REPLAY, SIMULATION, PAPER, or LIVE")
    return normalized


def _instant(value: str, *, name: str) -> str:
    text = _text(value, name=name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{name} must be an ISO timestamp") from error
    if parsed.tzinfo is None:
        raise ValueError(f"{name} must include timezone")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _instant_value(value: str, *, name: str) -> datetime:
    return datetime.fromisoformat(_instant(value, name=name).replace("Z", "+00:00"))


def _decimal(value, *, name: str, allow_zero: bool = True) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError(f"{name} must use Decimal, string or integer input")
    try:
        result = value if isinstance(value, Decimal) else Decimal(value)
    except (InvalidOperation, ValueError, TypeError) as error:
        raise ValueError(f"{name} must be a finite decimal") from error
    if not result.is_finite() or result < 0 or (result == 0 and not allow_zero):
        raise ValueError(
            f"{name} must be {'non-negative' if allow_zero else 'positive'}"
        )
    return result


def _decimal_text(value: Decimal) -> str:
    if value == 0:
        return "0"
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered


def _refs(values: Sequence[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise TypeError("evidence_refs must be a sequence")
    result: list[str] = []
    for value in values:
        reference = _text(value, name="evidence_ref")
        if reference in result:
            raise ValueError("evidence_refs must be unique")
        result.append(reference)
    if not result:
        raise ValueError("at least one provider evidence reference is required")
    return tuple(result)


@dataclass(frozen=True)
class BorrowResourceIdentity:
    provider_id: str
    account_id: str
    environment: str
    instrument_id: str
    instrument_version: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "provider_id", _text(self.provider_id, name="provider_id").upper()
        )
        object.__setattr__(self, "account_id", _text(self.account_id, name="account_id"))
        object.__setattr__(self, "environment", _environment(self.environment))
        try:
            instrument_id = str(UUID(_text(self.instrument_id, name="instrument_id")))
        except ValueError as error:
            raise ValueError("instrument_id must be a UUID") from error
        object.__setattr__(self, "instrument_id", instrument_id)
        if (
            not isinstance(self.instrument_version, int)
            or isinstance(self.instrument_version, bool)
            or self.instrument_version < 1
        ):
            raise ValueError("instrument_version must be a positive integer")

    @property
    def resource_key(self) -> str:
        payload = {
            "kind": "SECURITIES_BORROW",
            "provider_id": self.provider_id,
            "account_id": self.account_id,
            "environment": self.environment,
            "instrument_id": self.instrument_id,
            "instrument_version": self.instrument_version,
        }
        digest = sha256(canonical_json(payload).encode("utf-8")).hexdigest()
        return "BORROW:sha256:" + digest

    def payload(self) -> dict[str, object]:
        return {
            "provider_id": self.provider_id,
            "account_id": self.account_id,
            "environment": self.environment,
            "instrument_id": self.instrument_id,
            "instrument_version": self.instrument_version,
            "resource_key": self.resource_key,
        }


@dataclass(frozen=True)
class BorrowLocateEvidence:
    resource: BorrowResourceIdentity
    locate_id: str
    provider_revision: str
    availability_state: str
    available_quantity: Decimal
    observed_at: str
    effective_at: str
    valid_until: str
    evidence_refs: tuple[str, ...]
    indicative_rate: Decimal | None = None
    rate_unit: str | None = None
    provider_as_of: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.resource, BorrowResourceIdentity):
            raise TypeError("resource must be BorrowResourceIdentity")
        object.__setattr__(self, "locate_id", _text(self.locate_id, name="locate_id"))
        object.__setattr__(
            self,
            "provider_revision",
            _text(self.provider_revision, name="provider_revision"),
        )
        state = _text(self.availability_state, name="availability_state").upper()
        if state not in _AVAILABILITY_STATES:
            raise ValueError(
                "availability_state must be AVAILABLE, HARD_TO_BORROW, or UNAVAILABLE"
            )
        quantity = _decimal(self.available_quantity, name="available_quantity")
        if state == "UNAVAILABLE" and quantity != 0:
            raise ValueError("UNAVAILABLE locate must expose zero available quantity")
        object.__setattr__(self, "availability_state", state)
        object.__setattr__(self, "available_quantity", quantity)
        observed = _instant(self.observed_at, name="observed_at")
        effective = _instant(self.effective_at, name="effective_at")
        valid = _instant(self.valid_until, name="valid_until")
        if _instant_value(effective, name="effective_at") > _instant_value(
            observed, name="observed_at"
        ):
            raise ValueError("effective_at cannot be after observed_at")
        if _instant_value(valid, name="valid_until") <= _instant_value(
            observed, name="observed_at"
        ):
            raise ValueError("valid_until must be after observed_at")
        object.__setattr__(self, "observed_at", observed)
        object.__setattr__(self, "effective_at", effective)
        object.__setattr__(self, "valid_until", valid)
        object.__setattr__(self, "evidence_refs", _refs(self.evidence_refs))
        if self.provider_as_of is not None:
            object.__setattr__(
                self,
                "provider_as_of",
                _instant(self.provider_as_of, name="provider_as_of"),
            )
        if self.indicative_rate is None:
            if self.rate_unit is not None:
                raise ValueError("rate_unit requires indicative_rate")
        else:
            object.__setattr__(
                self,
                "indicative_rate",
                _decimal(self.indicative_rate, name="indicative_rate"),
            )
            object.__setattr__(
                self, "rate_unit", _text(self.rate_unit, name="rate_unit").upper()
            )

    def assert_fresh(self, *, now: str) -> None:
        current = _instant_value(now, name="now")
        if current < _instant_value(self.effective_at, name="effective_at"):
            raise ValueError("borrow locate is not yet effective")
        if current >= _instant_value(self.valid_until, name="valid_until"):
            raise ValueError("borrow locate is expired")
        if self.availability_state == "UNAVAILABLE":
            raise ValueError("borrow locate is unavailable")

    def payload(self) -> dict[str, object]:
        return {
            "resource": self.resource.payload(),
            "locate_id": self.locate_id,
            "provider_revision": self.provider_revision,
            "availability_state": self.availability_state,
            "available_quantity": _decimal_text(self.available_quantity),
            "observed_at": self.observed_at,
            "effective_at": self.effective_at,
            "valid_until": self.valid_until,
            "provider_as_of": self.provider_as_of,
            "evidence_refs": list(self.evidence_refs),
            "indicative_rate": (
                None
                if self.indicative_rate is None
                else _decimal_text(self.indicative_rate)
            ),
            "rate_unit": self.rate_unit,
        }


@dataclass(frozen=True)
class BorrowLoanEvidence:
    """Provider truth for the currently outstanding borrowed share quantity.

    This is distinct from locate availability: a provider may report remaining
    capacity for new borrow while an existing loan is already outstanding.
    """

    resource: BorrowResourceIdentity
    provider_revision: str
    borrowed_quantity: Decimal
    observed_at: str
    effective_at: str
    valid_until: str
    evidence_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.resource, BorrowResourceIdentity):
            raise TypeError("resource must be BorrowResourceIdentity")
        object.__setattr__(
            self,
            "provider_revision",
            _text(self.provider_revision, name="provider_revision"),
        )
        object.__setattr__(
            self,
            "borrowed_quantity",
            _decimal(self.borrowed_quantity, name="borrowed_quantity"),
        )
        observed = _instant(self.observed_at, name="observed_at")
        effective = _instant(self.effective_at, name="effective_at")
        valid = _instant(self.valid_until, name="valid_until")
        if _instant_value(effective, name="effective_at") > _instant_value(
            observed, name="observed_at"
        ):
            raise ValueError("loan effective_at cannot be after observed_at")
        if _instant_value(valid, name="valid_until") <= _instant_value(
            observed, name="observed_at"
        ):
            raise ValueError("loan valid_until must be after observed_at")
        object.__setattr__(self, "observed_at", observed)
        object.__setattr__(self, "effective_at", effective)
        object.__setattr__(self, "valid_until", valid)
        object.__setattr__(self, "evidence_refs", _refs(self.evidence_refs))

    def assert_fresh(self, *, now: str) -> None:
        current = _instant_value(now, name="now")
        if current < _instant_value(self.effective_at, name="effective_at"):
            raise ValueError("borrow loan evidence is not yet effective")
        if current >= _instant_value(self.valid_until, name="valid_until"):
            raise ValueError("borrow loan evidence is expired")

    def payload(self) -> dict[str, object]:
        return {
            "resource": self.resource.payload(),
            "provider_revision": self.provider_revision,
            "borrowed_quantity": _decimal_text(self.borrowed_quantity),
            "observed_at": self.observed_at,
            "effective_at": self.effective_at,
            "valid_until": self.valid_until,
            "evidence_refs": list(self.evidence_refs),
        }


@dataclass(frozen=True)
class BorrowRecallEvidence:
    resource: BorrowResourceIdentity
    recall_id: str
    provider_revision: str
    recalled_quantity: Decimal
    observed_at: str
    effective_at: str
    evidence_refs: tuple[str, ...]
    deadline: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.resource, BorrowResourceIdentity):
            raise TypeError("resource must be BorrowResourceIdentity")
        object.__setattr__(self, "recall_id", _text(self.recall_id, name="recall_id"))
        object.__setattr__(
            self,
            "provider_revision",
            _text(self.provider_revision, name="provider_revision"),
        )
        object.__setattr__(
            self,
            "recalled_quantity",
            _decimal(self.recalled_quantity, name="recalled_quantity", allow_zero=False),
        )
        observed = _instant(self.observed_at, name="observed_at")
        effective = _instant(self.effective_at, name="effective_at")
        if _instant_value(effective, name="effective_at") > _instant_value(
            observed, name="observed_at"
        ):
            raise ValueError("recall effective_at cannot be after observed_at")
        object.__setattr__(self, "observed_at", observed)
        object.__setattr__(self, "effective_at", effective)
        if self.deadline is not None:
            deadline = _instant(self.deadline, name="deadline")
            if _instant_value(deadline, name="deadline") < _instant_value(
                effective, name="effective_at"
            ):
                raise ValueError("recall deadline cannot precede effective_at")
            object.__setattr__(self, "deadline", deadline)
        object.__setattr__(self, "evidence_refs", _refs(self.evidence_refs))

    def payload(self) -> dict[str, object]:
        return {
            "resource": self.resource.payload(),
            "recall_id": self.recall_id,
            "provider_revision": self.provider_revision,
            "recalled_quantity": _decimal_text(self.recalled_quantity),
            "observed_at": self.observed_at,
            "effective_at": self.effective_at,
            "deadline": self.deadline,
            "evidence_refs": list(self.evidence_refs),
        }


@dataclass(frozen=True)
class BorrowRecallResolutionEvidence:
    resource: BorrowResourceIdentity
    recall_id: str
    provider_revision: str
    resolved_quantity: Decimal
    observed_at: str
    evidence_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.resource, BorrowResourceIdentity):
            raise TypeError("resource must be BorrowResourceIdentity")
        object.__setattr__(self, "recall_id", _text(self.recall_id, name="recall_id"))
        object.__setattr__(
            self,
            "provider_revision",
            _text(self.provider_revision, name="provider_revision"),
        )
        object.__setattr__(
            self,
            "resolved_quantity",
            _decimal(self.resolved_quantity, name="resolved_quantity", allow_zero=False),
        )
        object.__setattr__(
            self, "observed_at", _instant(self.observed_at, name="observed_at")
        )
        object.__setattr__(self, "evidence_refs", _refs(self.evidence_refs))

    def payload(self) -> dict[str, object]:
        return {
            "resource": self.resource.payload(),
            "recall_id": self.recall_id,
            "provider_revision": self.provider_revision,
            "resolved_quantity": _decimal_text(self.resolved_quantity),
            "observed_at": self.observed_at,
            "evidence_refs": list(self.evidence_refs),
        }


@dataclass(frozen=True)
class BorrowLifecycleState:
    resource: BorrowResourceIdentity
    latest_locate: BorrowLocateEvidence | None
    latest_loan: BorrowLoanEvidence | None
    active_recalls: Mapping[str, Decimal]

    @property
    def active_recall_quantity(self) -> Decimal:
        return sum(self.active_recalls.values(), Decimal("0"))

    @property
    def blocks_new_short(self) -> bool:
        return self.active_recall_quantity > 0


def incremental_short_borrow_quantity(
    intent: RiskIntent,
    context: RiskContext,
) -> Decimal:
    """Return only newly-created short quantity after existing reservations."""

    if not isinstance(intent, RiskIntent):
        raise TypeError("intent must be RiskIntent")
    if not isinstance(context, RiskContext):
        raise TypeError("context must be RiskContext")
    if intent.instrument_type != "EQUITY":
        return Decimal("0")
    current = context.positions.get(intent.symbol, Decimal("0"))
    reserved = context.reserved_position_delta.get(intent.symbol, Decimal("0"))
    base_position = current + reserved
    signed = intent.quantity if intent.side == "BUY" else -intent.quantity
    resulting = base_position + signed
    short_before = max(-base_position, Decimal("0"))
    short_after = max(-resulting, Decimal("0"))
    return max(short_after - short_before, Decimal("0"))


def local_short_quantity(context: RiskContext, *, symbol: str) -> Decimal:
    if not isinstance(context, RiskContext):
        raise TypeError("context must be RiskContext")
    name = _text(symbol, name="symbol")
    return max(-context.positions.get(name, Decimal("0")), Decimal("0"))


def validate_borrow_account_truth(
    state: BorrowLifecycleState,
    context: RiskContext,
    *,
    symbol: str,
    now: str,
) -> Decimal:
    """Verify provider loan truth against the current filled local short.

    WORKING/UNKNOWN future short exposure is not part of provider loan quantity
    yet; it remains conservatively consumed by DurableReservationBook.
    """

    if not isinstance(state, BorrowLifecycleState):
        raise TypeError("state must be BorrowLifecycleState")
    current_short = local_short_quantity(context, symbol=symbol)
    loan = state.latest_loan
    if loan is None:
        if current_short != 0:
            raise ValueError(
                "provider borrow loan evidence is required for an existing short"
            )
        return current_short
    loan.assert_fresh(now=now)
    if loan.borrowed_quantity != current_short:
        raise ValueError(
            "provider borrowed quantity does not match current local short exposure"
        )
    return current_short


def borrow_reservation_requirement(
    resource: BorrowResourceIdentity,
    intent: RiskIntent,
    context: RiskContext,
) -> dict[str, str]:
    if not isinstance(resource, BorrowResourceIdentity):
        raise TypeError("resource must be BorrowResourceIdentity")
    quantity = incremental_short_borrow_quantity(intent, context)
    if quantity == 0:
        return {}
    return {resource.resource_key: _decimal_text(quantity)}


def locate_capacity(
    evidence: BorrowLocateEvidence,
    *,
    now: str,
    active_recall_quantity=0,
) -> dict[str, str]:
    if not isinstance(evidence, BorrowLocateEvidence):
        raise TypeError("evidence must be BorrowLocateEvidence")
    evidence.assert_fresh(now=now)
    recalled = _decimal(active_recall_quantity, name="active_recall_quantity")
    available = max(evidence.available_quantity - recalled, Decimal("0"))
    return {evidence.resource.resource_key: _decimal_text(available)}


def validated_borrow_capacity(
    state: BorrowLifecycleState,
    context: RiskContext,
    *,
    symbol: str,
    now: str,
) -> dict[str, str]:
    """Return remaining new-borrow capacity only after account truth validates."""

    if not isinstance(state, BorrowLifecycleState):
        raise TypeError("state must be BorrowLifecycleState")
    if state.latest_locate is None:
        raise ValueError("fresh provider borrow locate evidence is required")
    validate_borrow_account_truth(state, context, symbol=symbol, now=now)
    if state.blocks_new_short:
        raise ValueError("active provider borrow recall blocks new short exposure")
    return locate_capacity(state.latest_locate, now=now)


class BorrowLifecycleJournal:
    """Restart-safe provider-evidence projection for one borrow resource."""

    def __init__(self, store: JournalStore, resource: BorrowResourceIdentity):
        if not isinstance(store, JournalStore):
            raise TypeError("store must be JournalStore")
        if not isinstance(resource, BorrowResourceIdentity):
            raise TypeError("resource must be BorrowResourceIdentity")
        self.store = store
        self.resource = resource
        self.aggregate_id = "borrow-lifecycle:" + str(
            uuid5(
                NAMESPACE_URL,
                "https://events.autotrade.local/borrow/" + resource.resource_key,
            )
        )

    def _events(self) -> list[dict[str, object]]:
        return self.store.load_events(_AGGREGATE_TYPE, self.aggregate_id)

    def _resource_from_payload(self, payload: Mapping[str, object]) -> BorrowResourceIdentity:
        raw = payload.get("resource")
        if not isinstance(raw, Mapping):
            raise ValueError("borrow event resource is required")
        resource = BorrowResourceIdentity(
            provider_id=raw.get("provider_id"),
            account_id=raw.get("account_id"),
            environment=raw.get("environment"),
            instrument_id=raw.get("instrument_id"),
            instrument_version=raw.get("instrument_version"),
        )
        if resource != self.resource or raw.get("resource_key") != resource.resource_key:
            raise ValueError("borrow event resource scope mismatch")
        return resource

    def _locate_from_payload(self, payload: Mapping[str, object]) -> BorrowLocateEvidence:
        resource = self._resource_from_payload(payload)
        return BorrowLocateEvidence(
            resource=resource,
            locate_id=payload.get("locate_id"),
            provider_revision=payload.get("provider_revision"),
            availability_state=payload.get("availability_state"),
            available_quantity=payload.get("available_quantity"),
            observed_at=payload.get("observed_at"),
            effective_at=payload.get("effective_at"),
            valid_until=payload.get("valid_until"),
            provider_as_of=payload.get("provider_as_of"),
            evidence_refs=tuple(payload.get("evidence_refs") or ()),
            indicative_rate=payload.get("indicative_rate"),
            rate_unit=payload.get("rate_unit"),
        )

    def _loan_from_payload(self, payload: Mapping[str, object]) -> BorrowLoanEvidence:
        resource = self._resource_from_payload(payload)
        return BorrowLoanEvidence(
            resource=resource,
            provider_revision=payload.get("provider_revision"),
            borrowed_quantity=payload.get("borrowed_quantity"),
            observed_at=payload.get("observed_at"),
            effective_at=payload.get("effective_at"),
            valid_until=payload.get("valid_until"),
            evidence_refs=tuple(payload.get("evidence_refs") or ()),
        )

    def _recall_from_payload(self, payload: Mapping[str, object]) -> BorrowRecallEvidence:
        resource = self._resource_from_payload(payload)
        return BorrowRecallEvidence(
            resource=resource,
            recall_id=payload.get("recall_id"),
            provider_revision=payload.get("provider_revision"),
            recalled_quantity=payload.get("recalled_quantity"),
            observed_at=payload.get("observed_at"),
            effective_at=payload.get("effective_at"),
            deadline=payload.get("deadline"),
            evidence_refs=tuple(payload.get("evidence_refs") or ()),
        )

    def _resolution_from_payload(
        self, payload: Mapping[str, object]
    ) -> BorrowRecallResolutionEvidence:
        resource = self._resource_from_payload(payload)
        return BorrowRecallResolutionEvidence(
            resource=resource,
            recall_id=payload.get("recall_id"),
            provider_revision=payload.get("provider_revision"),
            resolved_quantity=payload.get("resolved_quantity"),
            observed_at=payload.get("observed_at"),
            evidence_refs=tuple(payload.get("evidence_refs") or ()),
        )

    def state(self, *, through_version: int | None = None) -> BorrowLifecycleState:
        if through_version is not None and (
            not isinstance(through_version, int)
            or isinstance(through_version, bool)
            or through_version < 0
        ):
            raise ValueError("through_version must be a non-negative integer")
        latest_locate: BorrowLocateEvidence | None = None
        latest_loan: BorrowLoanEvidence | None = None
        recalls: dict[str, Decimal] = {}
        identities: dict[tuple[str, str, str], str] = {}
        locate_observed: datetime | None = None
        loan_observed: datetime | None = None
        recall_observed: dict[str, datetime] = {}

        events = self._events()
        if through_version is not None:
            if through_version > len(events):
                raise ValueError("borrow journal cut exceeds durable aggregate version")
            events = events[:through_version]
        for event in events:
            payload = event.get("payload")
            if not isinstance(payload, Mapping):
                raise ValueError("borrow journal payload is required")
            if event.get("payload_hash") != payload_digest(payload):
                raise ValueError("borrow journal payload hash mismatch")
            kind = _text(event.get("event_type"), name="event_type")
            if kind == "BorrowLocateObserved":
                item = self._locate_from_payload(payload)
                key = (kind, item.locate_id, item.provider_revision)
                digest = payload_digest(payload)
                if key in identities and identities[key] != digest:
                    raise ValueError("conflicting borrow locate revision")
                identities[key] = digest
                observed = _instant_value(item.observed_at, name="observed_at")
                if locate_observed is None or observed >= locate_observed:
                    latest_locate = item
                    locate_observed = observed
            elif kind == "BorrowLoanObserved":
                item = self._loan_from_payload(payload)
                key = (kind, self.resource.resource_key, item.provider_revision)
                digest = payload_digest(payload)
                if key in identities and identities[key] != digest:
                    raise ValueError("conflicting borrow loan revision")
                identities[key] = digest
                observed = _instant_value(item.observed_at, name="observed_at")
                if loan_observed is None or observed >= loan_observed:
                    latest_loan = item
                    loan_observed = observed
            elif kind == "BorrowRecallObserved":
                item = self._recall_from_payload(payload)
                key = (kind, item.recall_id, item.provider_revision)
                digest = payload_digest(payload)
                if key in identities and identities[key] != digest:
                    raise ValueError("conflicting borrow recall revision")
                identities[key] = digest
                observed = _instant_value(item.observed_at, name="observed_at")
                previous_observed = recall_observed.get(item.recall_id)
                if previous_observed is None or observed >= previous_observed:
                    recalls[item.recall_id] = item.recalled_quantity
                    recall_observed[item.recall_id] = observed
            elif kind == "BorrowRecallReduced":
                item = self._resolution_from_payload(payload)
                key = (kind, item.recall_id, item.provider_revision)
                digest = payload_digest(payload)
                if key in identities and identities[key] != digest:
                    raise ValueError("conflicting borrow recall resolution revision")
                identities[key] = digest
                if item.recall_id not in recalls:
                    raise ValueError("recall resolution has no active provider recall")
                resolution_observed = _instant_value(
                    item.observed_at,
                    name="observed_at",
                )
                if resolution_observed < recall_observed[item.recall_id]:
                    raise ValueError(
                        "recall resolution predates the active provider recall revision"
                    )
                remaining = recalls[item.recall_id] - item.resolved_quantity
                if remaining < 0:
                    raise ValueError("recall resolution exceeds active recalled quantity")
                if remaining == 0:
                    del recalls[item.recall_id]
                else:
                    recalls[item.recall_id] = remaining
            else:
                raise ValueError(f"unsupported borrow journal event: {kind}")

        return BorrowLifecycleState(
            resource=self.resource,
            latest_locate=latest_locate,
            latest_loan=latest_loan,
            active_recalls=dict(sorted(recalls.items())),
        )

    def authority_snapshot(
        self,
        context: RiskContext,
        *,
        symbol: str,
        now: str,
    ) -> dict[str, object]:
        """Bind admission to one immutable borrow-journal cut."""

        events = self._events()
        state = self.state()
        capacity = validated_borrow_capacity(
            state,
            context,
            symbol=symbol,
            now=now,
        )
        if state.latest_locate is None:
            raise ValueError("fresh provider borrow locate evidence is required")
        local_short = local_short_quantity(context, symbol=symbol)

        def matching_event(event_type: str, payload: Mapping[str, object]):
            digest = payload_digest(payload)
            matches = [
                event
                for event in events
                if event.get("event_type") == event_type
                and event.get("payload_hash") == digest
            ]
            if len(matches) != 1:
                raise ValueError(
                    f"borrow {event_type} evidence is missing or ambiguous"
                )
            return matches[0]

        locate_event = matching_event(
            "BorrowLocateObserved",
            state.latest_locate.payload(),
        )
        loan_event = (
            None
            if state.latest_loan is None
            else matching_event("BorrowLoanObserved", state.latest_loan.payload())
        )
        cut = len(events)
        return {
            "aggregate_id": self.aggregate_id,
            "aggregate_version": cut,
            "resource": self.resource.payload(),
            "local_short_quantity": _decimal_text(local_short),
            "active_recall_quantity": _decimal_text(
                state.active_recall_quantity
            ),
            "availability": capacity,
            "locate_event_id": locate_event.get("event_id"),
            "locate_payload_hash": locate_event.get("payload_hash"),
            "loan_event_id": (
                None if loan_event is None else loan_event.get("event_id")
            ),
            "loan_payload_hash": (
                None if loan_event is None else loan_event.get("payload_hash")
            ),
        }

    def validate_authority_snapshot(
        self,
        snapshot: Mapping[str, object],
        *,
        now: str,
    ) -> dict[str, str]:
        """Rebuild and verify the exact historical borrow cut used at admission."""

        if not isinstance(snapshot, Mapping):
            raise TypeError("borrow authority snapshot must be a mapping")
        if snapshot.get("aggregate_id") != self.aggregate_id:
            raise ValueError("borrow authority aggregate identity mismatch")
        version = snapshot.get("aggregate_version")
        if (
            not isinstance(version, int)
            or isinstance(version, bool)
            or version < 1
        ):
            raise ValueError(
                "borrow authority aggregate_version must be a positive integer"
            )
        raw_resource = snapshot.get("resource")
        if not isinstance(raw_resource, Mapping):
            raise ValueError("borrow authority resource identity is required")
        if raw_resource != self.resource.payload():
            raise ValueError("borrow authority resource identity mismatch")
        state = self.state(through_version=version)
        if state.latest_locate is None:
            raise ValueError("borrow authority cut lacks locate evidence")
        state.latest_locate.assert_fresh(now=now)
        if state.blocks_new_short:
            raise ValueError("borrow authority cut contains an active recall")

        local_short = _decimal(
            snapshot.get("local_short_quantity"),
            name="local_short_quantity",
        )
        if state.latest_loan is None:
            if local_short != 0:
                raise ValueError(
                    "borrow authority cut lacks provider loan evidence"
                )
        else:
            state.latest_loan.assert_fresh(now=now)
            if state.latest_loan.borrowed_quantity != local_short:
                raise ValueError(
                    "borrow authority loan quantity differs from bound local short"
                )

        events = self._events()[:version]
        by_id = {
            event.get("event_id"): event
            for event in events
            if isinstance(event.get("event_id"), str)
        }
        locate_event = by_id.get(snapshot.get("locate_event_id"))
        if (
            locate_event is None
            or locate_event.get("event_type") != "BorrowLocateObserved"
            or locate_event.get("payload_hash")
            != snapshot.get("locate_payload_hash")
            or locate_event.get("payload_hash")
            != payload_digest(state.latest_locate.payload())
        ):
            raise ValueError("borrow locate authority evidence mismatch")

        if state.latest_loan is None:
            if (
                snapshot.get("loan_event_id") is not None
                or snapshot.get("loan_payload_hash") is not None
            ):
                raise ValueError("unexpected borrow loan authority evidence")
        else:
            loan_event = by_id.get(snapshot.get("loan_event_id"))
            if (
                loan_event is None
                or loan_event.get("event_type") != "BorrowLoanObserved"
                or loan_event.get("payload_hash")
                != snapshot.get("loan_payload_hash")
                or loan_event.get("payload_hash")
                != payload_digest(state.latest_loan.payload())
            ):
                raise ValueError("borrow loan authority evidence mismatch")

        expected = locate_capacity(state.latest_locate, now=now)
        if snapshot.get("active_recall_quantity") != "0":
            raise ValueError("borrow authority snapshot must bind zero active recall")
        if snapshot.get("availability") != expected:
            raise ValueError("borrow authority capacity mismatch")
        return expected

    def current_capacity_for_bound_short(
        self,
        snapshot: Mapping[str, object],
        *,
        now: str,
    ) -> dict[str, str]:
        """Validate current provider truth before dispatch of an admitted short."""

        if not isinstance(snapshot, Mapping):
            raise TypeError("borrow authority snapshot must be a mapping")
        raw_resource = snapshot.get("resource")
        if not isinstance(raw_resource, Mapping) or raw_resource != self.resource.payload():
            raise ValueError("borrow authority resource identity mismatch")
        local_short = _decimal(
            snapshot.get("local_short_quantity"),
            name="local_short_quantity",
        )
        state = self.state()
        if state.latest_locate is None:
            raise ValueError("current provider borrow locate evidence is missing")
        state.latest_locate.assert_fresh(now=now)
        if state.blocks_new_short:
            raise ValueError("active provider borrow recall blocks dispatch")
        if state.latest_loan is None:
            if local_short != 0:
                raise ValueError("current provider borrow loan evidence is missing")
        else:
            state.latest_loan.assert_fresh(now=now)
            if state.latest_loan.borrowed_quantity != local_short:
                raise ValueError(
                    "current provider borrowed quantity differs from bound local short"
                )
        return locate_capacity(state.latest_locate, now=now)

    def current_blocks_new_short(self) -> bool:
        """Current dispatch-time recall fence; no historical reinterpretation."""

        return self.state().blocks_new_short

    def _append(self, *, event_type: str, payload: dict[str, object], observed_at: str):
        digest = payload_digest(payload)
        identity_fields = {
            "BorrowLocateObserved": ("locate_id", "provider_revision"),
            "BorrowLoanObserved": ("provider_revision",),
            "BorrowRecallObserved": ("recall_id", "provider_revision"),
            "BorrowRecallReduced": ("recall_id", "provider_revision"),
        }[event_type]
        identity = tuple(payload[field] for field in identity_fields)
        for existing in self._events():
            if existing.get("event_type") != event_type:
                continue
            existing_payload = existing.get("payload")
            if not isinstance(existing_payload, Mapping):
                raise ValueError("borrow journal payload is required")
            if tuple(existing_payload.get(field) for field in identity_fields) == identity:
                if existing.get("payload_hash") != digest:
                    raise ValueError("provider evidence revision conflicts with durable history")
                return existing

        # Validate the candidate against the existing projection before append.
        if event_type == "BorrowRecallReduced":
            current = self.state()
            recall_id = _text(payload.get("recall_id"), name="recall_id")
            if recall_id not in current.active_recalls:
                raise ValueError("recall resolution has no active provider recall")
            resolved = _decimal(
                payload.get("resolved_quantity"),
                name="resolved_quantity",
                allow_zero=False,
            )
            if resolved > current.active_recalls[recall_id]:
                raise ValueError("recall resolution exceeds active recalled quantity")

        version = self.store.next_aggregate_version(_AGGREGATE_TYPE, self.aggregate_id)
        event_id = str(
            uuid5(
                NAMESPACE_URL,
                "https://events.autotrade.local/borrow-event/"
                + canonical_json([self.aggregate_id, version, event_type, digest]),
            )
        )
        instant = _instant(observed_at, name="observed_at")
        envelope = {
            "event_id": event_id,
            "event_type": event_type,
            "schema_version": "1.0.0",
            "aggregate_type": _AGGREGATE_TYPE,
            "aggregate_id": self.aggregate_id,
            "aggregate_version": str(version),
            "environment": self.resource.environment,
            "occurred_at": instant,
            "observed_at": instant,
            "committed_at": instant,
            "correlation_id": event_id,
            "causation_id": None,
            "payload": payload,
            "payload_hash": digest,
            "evidence_refs": list(payload.get("evidence_refs") or ()),
        }
        self.store.append_event(envelope)
        event = self.store.get_event(event_id)
        if event is None:
            raise RuntimeError("borrow lifecycle event was not persisted")
        # Force replay now so malformed transitions cannot hide until restart.
        self.state()
        return event

    def record_locate(self, evidence: BorrowLocateEvidence):
        if not isinstance(evidence, BorrowLocateEvidence):
            raise TypeError("evidence must be BorrowLocateEvidence")
        if evidence.resource != self.resource:
            raise ValueError("borrow locate resource scope mismatch")
        return self._append(
            event_type="BorrowLocateObserved",
            payload=evidence.payload(),
            observed_at=evidence.observed_at,
        )

    def record_loan(self, evidence: BorrowLoanEvidence):
        if not isinstance(evidence, BorrowLoanEvidence):
            raise TypeError("evidence must be BorrowLoanEvidence")
        if evidence.resource != self.resource:
            raise ValueError("borrow loan resource scope mismatch")
        return self._append(
            event_type="BorrowLoanObserved",
            payload=evidence.payload(),
            observed_at=evidence.observed_at,
        )

    def record_recall(self, evidence: BorrowRecallEvidence):
        if not isinstance(evidence, BorrowRecallEvidence):
            raise TypeError("evidence must be BorrowRecallEvidence")
        if evidence.resource != self.resource:
            raise ValueError("borrow recall resource scope mismatch")
        return self._append(
            event_type="BorrowRecallObserved",
            payload=evidence.payload(),
            observed_at=evidence.observed_at,
        )

    def record_recall_resolution(self, evidence: BorrowRecallResolutionEvidence):
        """Reduce recall only from affirmative provider evidence.

        There is intentionally no ACK/UNKNOWN/timeout shortcut here. An
        ambiguous close attempt must stay represented by the execution and
        reservation authorities and cannot erase a provider recall.
        """

        if not isinstance(evidence, BorrowRecallResolutionEvidence):
            raise TypeError("evidence must be BorrowRecallResolutionEvidence")
        if evidence.resource != self.resource:
            raise ValueError("borrow recall resolution resource scope mismatch")
        return self._append(
            event_type="BorrowRecallReduced",
            payload=evidence.payload(),
            observed_at=evidence.observed_at,
        )
