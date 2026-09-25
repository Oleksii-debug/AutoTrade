"""Securities-borrow evidence and durable recall projection.

This is not a second risk, reservation, execution, or reconciliation authority.
It supplies typed provider evidence and a journal-backed recall projection for
the existing AutoTrade authorities. Borrow fees remain in financing/accounting.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
from typing import Mapping
from uuid import NAMESPACE_URL, UUID, uuid5

from .persistence import JournalStore, payload_digest


_ENVIRONMENTS = frozenset({"REPLAY", "SIMULATION", "PAPER", "LIVE"})
_AGGREGATE_TYPE = "securities_borrow_recall"
_RECALL_EVENT = "BorrowRecallObserved"
_RESOLUTION_EVENT = "BorrowRecallResolved"


class BorrowEvidenceError(ValueError):
    pass


class BorrowRecallConflict(BorrowEvidenceError):
    pass


def _text(value: str, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    return value.strip()


def _environment(value: str) -> str:
    normalized = _text(value, name="environment").upper()
    if normalized not in _ENVIRONMENTS:
        raise ValueError("environment must be LIVE, PAPER, REPLAY, or SIMULATION")
    return normalized


def _instrument_id(value: str) -> str:
    try:
        return str(UUID(_text(value, name="instrument_id")))
    except (ValueError, TypeError, AttributeError) as error:
        raise ValueError("instrument_id must be a UUID") from error


def _version(value: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError("instrument_version must be a positive integer")
    return value


def _decimal(value, *, name: str, positive: bool = False) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError(f"{name} must use Decimal, string or integer input")
    try:
        result = value if isinstance(value, Decimal) else Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError(f"{name} must be a finite decimal") from error
    if not result.is_finite() or result < 0 or (positive and result == 0):
        word = "positive" if positive else "non-negative"
        raise ValueError(f"{name} must be a {word} finite decimal")
    return result


def _decimal_text(value: Decimal) -> str:
    if value == 0:
        return "0"
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _instant(value: str, *, name: str) -> str:
    text = _text(value, name=name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{name} must be an ISO timestamp") from error
    if parsed.tzinfo is None:
        raise ValueError(f"{name} must include timezone")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def borrow_resource_key(
    *,
    provider_id: str,
    account_id: str,
    environment: str,
    instrument_id: str,
    instrument_version: int,
) -> str:
    """Canonical borrow resource including provider/account/environment scope."""
    identity = [
        _text(provider_id, name="provider_id").upper(),
        _text(account_id, name="account_id"),
        _environment(environment),
        _instrument_id(instrument_id),
        _version(instrument_version),
    ]
    canonical = json.dumps(identity, ensure_ascii=True, separators=(",", ":"))
    return "BORROW:" + str(
        uuid5(
            NAMESPACE_URL,
            "https://resources.autotrade.local/securities-borrow/" + canonical,
        )
    )


@dataclass(frozen=True)
class BorrowAvailabilityEvidence:
    """Immutable provider proof of TOTAL approved borrow capacity.

    capacity_quantity includes inventory already borrowed. Adapters that know
    only an ambiguous "available" number must fail closed rather than emit this
    evidence. indicative_rate is informational and is never an economic posting.
    """

    provider_id: str
    account_id: str
    environment: str
    instrument_id: str
    instrument_version: int
    locate_id: str
    provider_revision: str
    capacity_quantity: Decimal
    hard_to_borrow: bool
    observed_at: str
    effective_at: str
    expires_at: str
    evidence_ref: str
    indicative_rate: Decimal | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider_id", _text(self.provider_id, name="provider_id").upper())
        object.__setattr__(self, "account_id", _text(self.account_id, name="account_id"))
        object.__setattr__(self, "environment", _environment(self.environment))
        object.__setattr__(self, "instrument_id", _instrument_id(self.instrument_id))
        object.__setattr__(self, "instrument_version", _version(self.instrument_version))
        object.__setattr__(self, "locate_id", _text(self.locate_id, name="locate_id"))
        object.__setattr__(self, "provider_revision", _text(self.provider_revision, name="provider_revision"))
        object.__setattr__(self, "capacity_quantity", _decimal(self.capacity_quantity, name="capacity_quantity"))
        if not isinstance(self.hard_to_borrow, bool):
            raise TypeError("hard_to_borrow must be boolean")
        observed = _instant(self.observed_at, name="observed_at")
        effective = _instant(self.effective_at, name="effective_at")
        expires = _instant(self.expires_at, name="expires_at")
        if _dt(effective) > _dt(observed):
            raise ValueError("effective_at must not be after observed_at")
        if _dt(expires) <= _dt(observed):
            raise ValueError("expires_at must be after observed_at")
        object.__setattr__(self, "observed_at", observed)
        object.__setattr__(self, "effective_at", effective)
        object.__setattr__(self, "expires_at", expires)
        object.__setattr__(self, "evidence_ref", _text(self.evidence_ref, name="evidence_ref"))
        if self.indicative_rate is not None:
            object.__setattr__(
                self,
                "indicative_rate",
                _decimal(self.indicative_rate, name="indicative_rate"),
            )

    @property
    def resource_key(self) -> str:
        return borrow_resource_key(
            provider_id=self.provider_id,
            account_id=self.account_id,
            environment=self.environment,
            instrument_id=self.instrument_id,
            instrument_version=self.instrument_version,
        )

    def resource_detail(self) -> dict[str, str]:
        return {
            "resource_type": "SECURITIES_BORROW",
            "capacity_semantics": "TOTAL_APPROVED_CAPACITY",
            "provider_id": self.provider_id,
            "account_id": self.account_id,
            "environment": self.environment,
            "instrument_id": self.instrument_id,
            "instrument_version": str(self.instrument_version),
            "locate_id": self.locate_id,
            "provider_revision": self.provider_revision,
            "capacity_quantity": _decimal_text(self.capacity_quantity),
            "hard_to_borrow": "true" if self.hard_to_borrow else "false",
            "observed_at": self.observed_at,
            "effective_at": self.effective_at,
            "expires_at": self.expires_at,
            "evidence_ref": self.evidence_ref,
            "indicative_rate": (
                "" if self.indicative_rate is None else _decimal_text(self.indicative_rate)
            ),
        }

    @classmethod
    def from_resource_detail(cls, detail: Mapping[str, object]) -> "BorrowAvailabilityEvidence":
        if not isinstance(detail, Mapping):
            raise TypeError("borrow resource detail must be a mapping")
        if detail.get("resource_type") != "SECURITIES_BORROW":
            raise ValueError("borrow resource detail has invalid resource_type")
        if detail.get("capacity_semantics") != "TOTAL_APPROVED_CAPACITY":
            raise ValueError("borrow evidence must prove TOTAL_APPROVED_CAPACITY")
        hard = detail.get("hard_to_borrow")
        if hard not in {"true", "false"}:
            raise ValueError("hard_to_borrow must use canonical true/false text")
        try:
            version = int(detail.get("instrument_version"))
        except (TypeError, ValueError) as error:
            raise ValueError("instrument_version must be a positive integer") from error
        raw_rate = detail.get("indicative_rate")
        return cls(
            provider_id=detail.get("provider_id"),
            account_id=detail.get("account_id"),
            environment=detail.get("environment"),
            instrument_id=detail.get("instrument_id"),
            instrument_version=version,
            locate_id=detail.get("locate_id"),
            provider_revision=detail.get("provider_revision"),
            capacity_quantity=detail.get("capacity_quantity"),
            hard_to_borrow=(hard == "true"),
            observed_at=detail.get("observed_at"),
            effective_at=detail.get("effective_at"),
            expires_at=detail.get("expires_at"),
            evidence_ref=detail.get("evidence_ref"),
            indicative_rate=(None if raw_rate in {None, ""} else raw_rate),
        )


@dataclass(frozen=True)
class BorrowRecallEvidence:
    recall_id: str
    provider_id: str
    account_id: str
    environment: str
    instrument_id: str
    instrument_version: int
    provider_revision: str
    quantity: Decimal
    observed_at: str
    effective_at: str
    evidence_ref: str
    deadline: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "recall_id", _text(self.recall_id, name="recall_id"))
        object.__setattr__(self, "provider_id", _text(self.provider_id, name="provider_id").upper())
        object.__setattr__(self, "account_id", _text(self.account_id, name="account_id"))
        object.__setattr__(self, "environment", _environment(self.environment))
        object.__setattr__(self, "instrument_id", _instrument_id(self.instrument_id))
        object.__setattr__(self, "instrument_version", _version(self.instrument_version))
        object.__setattr__(self, "provider_revision", _text(self.provider_revision, name="provider_revision"))
        object.__setattr__(self, "quantity", _decimal(self.quantity, name="quantity", positive=True))
        observed = _instant(self.observed_at, name="observed_at")
        effective = _instant(self.effective_at, name="effective_at")
        if _dt(effective) > _dt(observed):
            raise ValueError("recall effective_at must not be after observed_at")
        deadline = None if self.deadline is None else _instant(self.deadline, name="deadline")
        if deadline is not None and _dt(deadline) < _dt(effective):
            raise ValueError("recall deadline must not precede effective_at")
        object.__setattr__(self, "observed_at", observed)
        object.__setattr__(self, "effective_at", effective)
        object.__setattr__(self, "deadline", deadline)
        object.__setattr__(self, "evidence_ref", _text(self.evidence_ref, name="evidence_ref"))

    @property
    def resource_key(self) -> str:
        return borrow_resource_key(
            provider_id=self.provider_id,
            account_id=self.account_id,
            environment=self.environment,
            instrument_id=self.instrument_id,
            instrument_version=self.instrument_version,
        )

    def payload(self) -> dict[str, object]:
        return {
            "recall_id": self.recall_id,
            "provider_id": self.provider_id,
            "account_id": self.account_id,
            "environment": self.environment,
            "instrument_id": self.instrument_id,
            "instrument_version": self.instrument_version,
            "provider_revision": self.provider_revision,
            "quantity": _decimal_text(self.quantity),
            "observed_at": self.observed_at,
            "effective_at": self.effective_at,
            "evidence_ref": self.evidence_ref,
            "deadline": self.deadline,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> "BorrowRecallEvidence":
        return cls(**dict(payload))


@dataclass(frozen=True)
class BorrowRecallResolutionEvidence:
    resolution_id: str
    recall_id: str
    provider_id: str
    account_id: str
    environment: str
    instrument_id: str
    instrument_version: int
    provider_revision: str
    resolved_quantity: Decimal
    observed_at: str
    effective_at: str
    evidence_ref: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "resolution_id", _text(self.resolution_id, name="resolution_id"))
        object.__setattr__(self, "recall_id", _text(self.recall_id, name="recall_id"))
        object.__setattr__(self, "provider_id", _text(self.provider_id, name="provider_id").upper())
        object.__setattr__(self, "account_id", _text(self.account_id, name="account_id"))
        object.__setattr__(self, "environment", _environment(self.environment))
        object.__setattr__(self, "instrument_id", _instrument_id(self.instrument_id))
        object.__setattr__(self, "instrument_version", _version(self.instrument_version))
        object.__setattr__(self, "provider_revision", _text(self.provider_revision, name="provider_revision"))
        object.__setattr__(self, "resolved_quantity", _decimal(self.resolved_quantity, name="resolved_quantity", positive=True))
        observed = _instant(self.observed_at, name="observed_at")
        effective = _instant(self.effective_at, name="effective_at")
        if _dt(effective) > _dt(observed):
            raise ValueError("resolution effective_at must not be after observed_at")
        object.__setattr__(self, "observed_at", observed)
        object.__setattr__(self, "effective_at", effective)
        object.__setattr__(self, "evidence_ref", _text(self.evidence_ref, name="evidence_ref"))

    @property
    def resource_key(self) -> str:
        return borrow_resource_key(
            provider_id=self.provider_id,
            account_id=self.account_id,
            environment=self.environment,
            instrument_id=self.instrument_id,
            instrument_version=self.instrument_version,
        )

    def payload(self) -> dict[str, object]:
        return {
            "resolution_id": self.resolution_id,
            "recall_id": self.recall_id,
            "provider_id": self.provider_id,
            "account_id": self.account_id,
            "environment": self.environment,
            "instrument_id": self.instrument_id,
            "instrument_version": self.instrument_version,
            "provider_revision": self.provider_revision,
            "resolved_quantity": _decimal_text(self.resolved_quantity),
            "observed_at": self.observed_at,
            "effective_at": self.effective_at,
            "evidence_ref": self.evidence_ref,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> "BorrowRecallResolutionEvidence":
        return cls(**dict(payload))


class DurableBorrowRecallProjection:
    """Append-only provider recall state for one canonical borrow resource."""

    def __init__(
        self,
        store: JournalStore,
        *,
        provider_id: str,
        account_id: str,
        environment: str,
        instrument_id: str,
        instrument_version: int,
    ):
        if not isinstance(store, JournalStore):
            raise TypeError("store must be JournalStore")
        self.store = store
        self.provider_id = _text(provider_id, name="provider_id").upper()
        self.account_id = _text(account_id, name="account_id")
        self.environment = _environment(environment)
        self.instrument_id = _instrument_id(instrument_id)
        self.instrument_version = _version(instrument_version)
        self.resource_key = borrow_resource_key(
            provider_id=self.provider_id,
            account_id=self.account_id,
            environment=self.environment,
            instrument_id=self.instrument_id,
            instrument_version=self.instrument_version,
        )
        self.aggregate_id = "borrow-recall:" + str(
            uuid5(NAMESPACE_URL, "https://events.autotrade.local/borrow-recall/" + self.resource_key)
        )
        self._recalls: dict[str, BorrowRecallEvidence] = {}
        self._resolved: dict[str, Decimal] = {}
        self._resolutions: dict[str, BorrowRecallResolutionEvidence] = {}
        self._reload()

    def _scope_matches(self, evidence) -> bool:
        return (
            evidence.provider_id == self.provider_id
            and evidence.account_id == self.account_id
            and evidence.environment == self.environment
            and evidence.instrument_id == self.instrument_id
            and evidence.instrument_version == self.instrument_version
            and evidence.resource_key == self.resource_key
        )

    def _events(self) -> list[dict[str, object]]:
        return self.store.load_events(_AGGREGATE_TYPE, self.aggregate_id)

    def _reload(self) -> None:
        recalls: dict[str, BorrowRecallEvidence] = {}
        resolved: dict[str, Decimal] = {}
        resolutions: dict[str, BorrowRecallResolutionEvidence] = {}
        expected = 1
        for event in self._events():
            if int(event["aggregate_version"]) != expected:
                raise BorrowRecallConflict("borrow recall aggregate versions are not contiguous")
            expected += 1
            payload = event.get("payload")
            if not isinstance(payload, Mapping) or payload_digest(payload) != event.get("payload_hash"):
                raise BorrowRecallConflict("borrow recall event payload is invalid")
            raw = payload.get("evidence")
            if not isinstance(raw, Mapping):
                raise BorrowRecallConflict("borrow recall evidence is missing")
            if event.get("event_type") == _RECALL_EVENT:
                evidence = BorrowRecallEvidence.from_payload(raw)
                if not self._scope_matches(evidence):
                    raise BorrowRecallConflict("borrow recall evidence scope mismatch")
                if evidence.recall_id in recalls:
                    raise BorrowRecallConflict("duplicate borrow recall identity in journal")
                recalls[evidence.recall_id] = evidence
                resolved[evidence.recall_id] = Decimal("0")
            elif event.get("event_type") == _RESOLUTION_EVENT:
                evidence = BorrowRecallResolutionEvidence.from_payload(raw)
                if not self._scope_matches(evidence):
                    raise BorrowRecallConflict("borrow recall resolution scope mismatch")
                if evidence.resolution_id in resolutions:
                    raise BorrowRecallConflict("duplicate borrow recall resolution identity")
                recall = recalls.get(evidence.recall_id)
                if recall is None:
                    raise BorrowRecallConflict("resolution references unknown recall")
                if _dt(evidence.effective_at) < _dt(recall.effective_at):
                    raise BorrowRecallConflict("resolution predates recall")
                after = resolved[evidence.recall_id] + evidence.resolved_quantity
                if after > recall.quantity:
                    raise BorrowRecallConflict("resolution exceeds recalled quantity")
                resolved[evidence.recall_id] = after
                resolutions[evidence.resolution_id] = evidence
            else:
                raise BorrowRecallConflict("unsupported borrow recall event type")
        self._recalls = recalls
        self._resolved = resolved
        self._resolutions = resolutions

    @property
    def version(self) -> int:
        return len(self._events())

    def remaining(self, recall_id: str) -> Decimal:
        rid = _text(recall_id, name="recall_id")
        recall = self._recalls.get(rid)
        if recall is None:
            raise KeyError(rid)
        return recall.quantity - self._resolved.get(rid, Decimal("0"))

    @property
    def active_quantity(self) -> Decimal:
        return sum((self.remaining(rid) for rid in self._recalls), Decimal("0"))

    @property
    def active_recall_ids(self) -> tuple[str, ...]:
        return tuple(sorted(rid for rid in self._recalls if self.remaining(rid) > 0))

    @property
    def active_blocking_resources(self) -> tuple[str, ...]:
        return (self.resource_key,) if self.active_quantity > 0 else ()

    def _append(self, *, event_type: str, identity: str, payload: dict[str, object], committed_at: str) -> None:
        event_id = str(
            uuid5(
                NAMESPACE_URL,
                "https://events.autotrade.local/borrow-recall-event/"
                + self.aggregate_id + "/" + event_type + "/" + identity,
            )
        )
        existing = self.store.get_event(event_id)
        if existing is not None:
            if existing.get("event_type") == event_type and existing.get("payload") == payload:
                self._reload()
                return
            raise BorrowRecallConflict("borrow recall event identity conflicts")
        envelope = {
            "event_id": event_id,
            "event_type": event_type,
            "aggregate_type": _AGGREGATE_TYPE,
            "aggregate_id": self.aggregate_id,
            "aggregate_version": str(self.version + 1),
            "payload": payload,
            "payload_hash": payload_digest(payload),
            "committed_at": committed_at,
        }
        try:
            self.store.append_event(envelope)
        except Exception as error:
            self._reload()
            raise BorrowRecallConflict("borrow recall journal changed concurrently") from error
        self._reload()

    def record_recall(self, evidence: BorrowRecallEvidence) -> Decimal:
        if not isinstance(evidence, BorrowRecallEvidence):
            raise TypeError("evidence must be BorrowRecallEvidence")
        if not self._scope_matches(evidence):
            raise BorrowRecallConflict("borrow recall evidence scope mismatch")
        prior = self._recalls.get(evidence.recall_id)
        if prior is not None:
            if prior != evidence:
                raise BorrowRecallConflict("borrow recall identity has conflicting evidence")
            return self.remaining(evidence.recall_id)
        self._append(
            event_type=_RECALL_EVENT,
            identity=evidence.recall_id,
            payload={"operation": "RECALL", "evidence": evidence.payload()},
            committed_at=evidence.observed_at,
        )
        return self.remaining(evidence.recall_id)

    def resolve_recall(self, evidence: BorrowRecallResolutionEvidence) -> Decimal:
        if not isinstance(evidence, BorrowRecallResolutionEvidence):
            raise TypeError("evidence must be BorrowRecallResolutionEvidence")
        if not self._scope_matches(evidence):
            raise BorrowRecallConflict("borrow recall resolution scope mismatch")
        prior = self._resolutions.get(evidence.resolution_id)
        if prior is not None:
            if prior != evidence:
                raise BorrowRecallConflict("borrow recall resolution identity conflicts")
            return self.remaining(evidence.recall_id)
        recall = self._recalls.get(evidence.recall_id)
        if recall is None:
            raise BorrowRecallConflict("resolution references unknown recall")
        if _dt(evidence.effective_at) < _dt(recall.effective_at):
            raise BorrowRecallConflict("resolution predates recall")
        if evidence.resolved_quantity > self.remaining(evidence.recall_id):
            raise BorrowRecallConflict("resolution exceeds remaining recall obligation")
        self._append(
            event_type=_RESOLUTION_EVENT,
            identity=evidence.resolution_id,
            payload={"operation": "RESOLVE", "evidence": evidence.payload()},
            committed_at=evidence.observed_at,
        )
        return self.remaining(evidence.recall_id)

    def project_equity_state(self, state):
        from .corporate_actions import EquityState

        if not isinstance(state, EquityState):
            raise TypeError("state must be EquityState")
        active = self.active_quantity
        if active > state.borrowed_quantity:
            raise BorrowRecallConflict("provider recall exceeds locally borrowed quantity")
        return replace(state, recalled_quantity=active)
