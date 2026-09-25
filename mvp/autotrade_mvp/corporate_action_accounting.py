"""Authoritative provider corporate-action evidence -> durable accounting.

This module is an integration boundary around the existing pure
CorporateActionBook and DurableProviderEconomicBook.  It does not create a
second portfolio ledger or instrument registry.

The first qualified durable economic mapping is CASH_DIVIDEND.  Other corporate
action kinds may be normalized into the pure engine, but financial mutation
fails closed until an explicit canonical accounting mapping is implemented.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import re
from types import MappingProxyType
from typing import Mapping
from uuid import NAMESPACE_URL, uuid5

from .accounting import (
    AccountingConflict,
    JournalTransaction,
    Posting,
    reverse_transaction,
    validate_transaction,
)
from .corporate_actions import CorporateActionBook, CorporateEvent, EquityState, Transition
from .persistence import JournalStore, canonical_json, payload_digest
from .provider_activity_accounting import DurableProviderEconomicBook


_ENVIRONMENTS = frozenset({"REPLAY", "SIMULATION", "PAPER", "LIVE"})
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_SOURCE_AGGREGATE = "corporate_action_source"
_SOURCE_EVENT = "CorporateActionEvidenceAccepted"
_ACTOR = "provider-corporate-action-authority"
_SUPPORTED_DURABLE_KINDS = frozenset({"CASH_DIVIDEND"})


def _text(value: object, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    normalized = value.strip()
    if normalized != value:
        raise ValueError(f"{name} must be canonical text")
    return normalized


def _environment(value: object) -> str:
    normalized = _text(value, name="environment").upper()
    if normalized not in _ENVIRONMENTS:
        raise ValueError("environment must be REPLAY, SIMULATION, PAPER, or LIVE")
    return normalized


def _utc(value: object, *, name: str) -> str:
    text = _text(value, name=name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{name} must be a canonical UTC timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must include timezone")
    canonical = parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if canonical != text:
        raise ValueError(f"{name} must be canonical UTC text")
    return text


def _digest(value: object, *, name: str) -> str:
    text = _text(value, name=name)
    if _SHA256.fullmatch(text) is None:
        raise ValueError(f"{name} must be canonical lowercase sha256:<64 hex>")
    return text


def _identity(kind: str, *parts: str) -> str:
    digest = sha256(canonical_json(list(parts)).encode("utf-8")).hexdigest()
    return f"{kind}:{digest}"


def _event_identity(provider: str, account: str, environment: str, external_id: str) -> str:
    return _identity(
        "corporate-action",
        provider,
        account,
        environment,
        external_id,
    )


@dataclass(frozen=True)
class ProviderCorporateActionEvidence:
    """Immutable normalized provider/reference evidence for one logical action."""

    provider_id: str
    account_id: str
    environment: str
    external_action_id: str
    revision: str
    instrument_id: str
    instrument_version: int
    kind: str
    effective_at: str
    observed_at: str
    payload: Mapping[str, object]
    raw_evidence_sha256: str
    evidence_refs: tuple[str, ...]
    source_sequence: int | None = None
    supersedes_revision: str | None = None
    announcement_at: str | None = None
    record_at: str | None = None
    ex_at: str | None = None
    pay_at: str | None = None
    complete: bool = True

    def __post_init__(self) -> None:
        provider = _text(self.provider_id, name="provider_id").upper()
        account = _text(self.account_id, name="account_id")
        environment = _environment(self.environment)
        external_id = _text(self.external_action_id, name="external_action_id")
        revision = _text(self.revision, name="revision")
        instrument_id = _text(self.instrument_id, name="instrument_id")
        if (
            not isinstance(self.instrument_version, int)
            or isinstance(self.instrument_version, bool)
            or self.instrument_version < 1
        ):
            raise ValueError("instrument_version must be a positive integer")
        kind = _text(self.kind, name="kind").upper()
        effective_at = _utc(self.effective_at, name="effective_at")
        observed_at = _utc(self.observed_at, name="observed_at")
        if datetime.fromisoformat(observed_at.replace("Z", "+00:00")) < datetime.fromisoformat(
            effective_at.replace("Z", "+00:00")
        ):
            raise ValueError("observed_at cannot precede effective_at")
        raw_digest = _digest(self.raw_evidence_sha256, name="raw_evidence_sha256")
        if type(self.complete) is not bool:
            raise TypeError("complete must be boolean")
        if not self.complete:
            raise ValueError("incomplete corporate-action evidence cannot mutate financial state")
        if not isinstance(self.evidence_refs, tuple) or not self.evidence_refs:
            raise ValueError("evidence_refs must be a non-empty tuple")
        refs = tuple(_text(value, name="evidence_ref") for value in self.evidence_refs)
        if len(refs) != len(set(refs)):
            raise ValueError("evidence_refs must be unique")
        if not isinstance(self.payload, Mapping):
            raise TypeError("payload must be a mapping")
        normalized_payload: dict[str, object] = {}
        for raw_key, raw_value in self.payload.items():
            key = _text(raw_key, name="payload key")
            if key in normalized_payload:
                raise ValueError("payload keys must be unique")
            if isinstance(raw_value, (bool, float)) or not isinstance(
                raw_value, (str, int, Decimal)
            ):
                raise TypeError(
                    "corporate-action payload values must use text/integer/Decimal exact input"
                )
            normalized_payload[key] = raw_value
        if self.source_sequence is not None and (
            not isinstance(self.source_sequence, int)
            or isinstance(self.source_sequence, bool)
            or self.source_sequence < 0
        ):
            raise ValueError("source_sequence must be a non-negative integer")
        supersedes = (
            _text(self.supersedes_revision, name="supersedes_revision")
            if self.supersedes_revision is not None
            else None
        )
        lifecycle: dict[str, str | None] = {}
        for field in ("announcement_at", "record_at", "ex_at", "pay_at"):
            value = getattr(self, field)
            lifecycle[field] = _utc(value, name=field) if value is not None else None
        if kind == "CASH_DIVIDEND":
            if lifecycle["ex_at"] is None or lifecycle["pay_at"] is None:
                raise ValueError(
                    "cash dividend requires exact ex_at and pay_at evidence"
                )
            if lifecycle["ex_at"] != effective_at:
                raise ValueError("cash dividend effective_at must equal ex_at")
            if datetime.fromisoformat(lifecycle["pay_at"].replace("Z", "+00:00")) < datetime.fromisoformat(
                effective_at.replace("Z", "+00:00")
            ):
                raise ValueError("cash dividend pay_at cannot precede ex_at")

        object.__setattr__(self, "provider_id", provider)
        object.__setattr__(self, "account_id", account)
        object.__setattr__(self, "environment", environment)
        object.__setattr__(self, "external_action_id", external_id)
        object.__setattr__(self, "revision", revision)
        object.__setattr__(self, "instrument_id", instrument_id)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "effective_at", effective_at)
        object.__setattr__(self, "observed_at", observed_at)
        object.__setattr__(self, "raw_evidence_sha256", raw_digest)
        object.__setattr__(self, "evidence_refs", refs)
        object.__setattr__(self, "payload", MappingProxyType(dict(normalized_payload)))
        object.__setattr__(self, "supersedes_revision", supersedes)
        for field, value in lifecycle.items():
            object.__setattr__(self, field, value)

    @property
    def logical_event_id(self) -> str:
        return _event_identity(
            self.provider_id,
            self.account_id,
            self.environment,
            self.external_action_id,
        )

    def to_corporate_event(self) -> CorporateEvent:
        effective = datetime.fromisoformat(
            self.effective_at.replace("Z", "+00:00")
        )
        return CorporateEvent.create(
            event_id=self.logical_event_id,
            instrument_id=self.instrument_id,
            instrument_version=self.instrument_version,
            kind=self.kind,
            effective_date=effective.date(),
            effective_at=effective,
            source_revision=self.revision,
            source_sequence=self.source_sequence,
            payload=self.payload,
        )


@dataclass(frozen=True)
class CorporateActionCommitResult:
    inserted: bool
    event: CorporateEvent
    transition: Transition
    next_state: EquityState
    source_event_id: str
    transaction_ids: tuple[str, ...]


def _evidence_payload(evidence: ProviderCorporateActionEvidence) -> dict[str, object]:
    event = evidence.to_corporate_event()
    return {
        "schema_version": "1.0.0",
        "provider_id": evidence.provider_id,
        "account_id": evidence.account_id,
        "environment": evidence.environment,
        "external_action_id": evidence.external_action_id,
        "revision": evidence.revision,
        "supersedes_revision": evidence.supersedes_revision,
        "instrument_id": evidence.instrument_id,
        "instrument_version": evidence.instrument_version,
        "kind": evidence.kind,
        "effective_at": evidence.effective_at,
        "observed_at": evidence.observed_at,
        "source_sequence": evidence.source_sequence,
        "announcement_at": evidence.announcement_at,
        "record_at": evidence.record_at,
        "ex_at": evidence.ex_at,
        "pay_at": evidence.pay_at,
        "raw_evidence_sha256": evidence.raw_evidence_sha256,
        "evidence_refs": list(evidence.evidence_refs),
        "complete": True,
        "corporate_event": {
            "event_id": event.event_id,
            "instrument_id": event.instrument_id,
            "instrument_version": event.instrument_version,
            "kind": event.kind,
            "effective_date": event.effective_date.isoformat(),
            "effective_at": evidence.effective_at,
            "source_revision": event.source_revision,
            "source_sequence": event.source_sequence,
            "payload": dict(event.payload),
        },
    }


def _initial_book_state(book: CorporateActionBook) -> tuple[EquityState, object]:
    checkpoint = book.checkpoint("corporate-action-authority-candidate")
    if not checkpoint.records:
        return book.state, book.instrument_version
    first_event, first_transition = checkpoint.records[0]
    versions = tuple(
        value
        for value in book.registry.versions(first_event.instrument_id)
        if value.version == first_event.instrument_version
    )
    if len(versions) != 1:
        raise ValueError("initial corporate-action instrument version is not registered")
    return first_transition.before, versions[0]


def _candidate_book(
    book: CorporateActionBook,
    event: CorporateEvent,
) -> tuple[CorporateActionBook, Transition]:
    if not isinstance(book, CorporateActionBook):
        raise TypeError("corporate_book must be CorporateActionBook")
    current_events = list(book.events)
    replacement_index = next(
        (index for index, value in enumerate(current_events) if value.event_id == event.event_id),
        None,
    )
    if replacement_index is None:
        current_events.append(event)
    else:
        current_events[replacement_index] = event

    initial_state, initial_version = _initial_book_state(book)
    candidate = CorporateActionBook.replay(
        initial_state,
        instrument_version=initial_version,
        registry=book.registry,
        events=tuple(current_events),
    )
    transition = next(
        transition
        for accepted, transition in candidate.checkpoint("candidate").records
        if accepted.event_id == event.event_id
    )
    return candidate, transition


def _source_history(
    store: JournalStore,
    aggregate_id: str,
) -> list[dict[str, object]]:
    history = store.load_events(_SOURCE_AGGREGATE, aggregate_id)
    for expected, item in enumerate(history, start=1):
        if item.get("event_type") != _SOURCE_EVENT:
            raise AccountingConflict("corporate-action source journal has unsupported event")
        if item.get("aggregate_version") != expected:
            raise AccountingConflict("corporate-action source journal is non-contiguous")
        payload = item.get("payload")
        if not isinstance(payload, dict) or payload_digest(payload) != item.get("payload_hash"):
            raise AccountingConflict("corporate-action source journal integrity failed")
    return history


def _validate_revision(
    history: list[dict[str, object]],
    evidence_payload: dict[str, object],
) -> tuple[bool, str | None]:
    revision = evidence_payload["revision"]
    assert isinstance(revision, str)
    for event in history:
        payload = event["payload"]
        assert isinstance(payload, dict)
        if payload.get("revision") == revision:
            if payload != evidence_payload:
                raise AccountingConflict(
                    "corporate-action revision identity conflicts with durable evidence"
                )
            return True, payload.get("revision") if isinstance(payload.get("revision"), str) else None

    if not history:
        if evidence_payload.get("supersedes_revision") is not None:
            raise AccountingConflict("initial corporate-action revision cannot supersede history")
        return False, None

    previous = history[-1]["payload"]
    assert isinstance(previous, dict)
    previous_revision = previous.get("revision")
    if (
        not isinstance(previous_revision, str)
        or evidence_payload.get("supersedes_revision") != previous_revision
    ):
        raise AccountingConflict(
            "corporate-action revision must explicitly supersede the latest durable revision"
        )
    return False, previous_revision


def _logical_order_key(evidence: ProviderCorporateActionEvidence) -> str:
    return _identity(
        "corporate-action-order",
        evidence.provider_id,
        evidence.account_id,
        evidence.environment,
        evidence.external_action_id,
    )


def _transaction_id(
    evidence: ProviderCorporateActionEvidence,
    *,
    suffix: str,
) -> str:
    return _identity(
        "corporate-action-transaction",
        evidence.provider_id,
        evidence.account_id,
        evidence.environment,
        evidence.external_action_id,
        evidence.revision,
        suffix,
    )


def _dividend_transaction(
    evidence: ProviderCorporateActionEvidence,
    transition: Transition,
    *,
    corrects_transaction_id: str | None = None,
    economic_effective_at: str | None = None,
    economic_order_key: str | None = None,
) -> JournalTransaction | None:
    amount = transition.after.unsettled_cash - transition.before.unsettled_cash
    if amount == 0:
        return None
    currency = transition.after.currency
    transaction = JournalTransaction(
        transaction_id=_transaction_id(evidence, suffix="effect"),
        cause_event_id=_identity(
            "corporate-action-cause",
            evidence.logical_event_id,
            evidence.revision,
            "effect",
        ),
        postings=(
            Posting(f"UNSETTLED_CASH:{currency}", currency, amount),
            Posting(f"CORPORATE_ACTION_INCOME:{currency}", currency, -amount),
        ),
        economic_effective_at=economic_effective_at or evidence.effective_at,
        economic_order_key=economic_order_key or _logical_order_key(evidence),
        observed_at=evidence.observed_at,
        corrects_transaction_id=corrects_transaction_id,
    )
    validate_transaction(transaction)
    return transaction


def _active_action_transactions(
    economic_book: DurableProviderEconomicBook,
    order_key: str,
) -> tuple[JournalTransaction, ...]:
    transactions = tuple(economic_book.transactions)
    reversed_ids = {
        item.reverses_transaction_id
        for item in transactions
        if item.reverses_transaction_id is not None
    }
    return tuple(
        item
        for item in transactions
        if item.economic_order_key == order_key
        and item.reverses_transaction_id is None
        and item.transaction_id not in reversed_ids
    )


def _economic_transactions(
    economic_book: DurableProviderEconomicBook,
    evidence: ProviderCorporateActionEvidence,
    transition: Transition,
    *,
    is_revision: bool,
    exact_retry: bool,
) -> tuple[JournalTransaction, ...]:
    if evidence.kind not in _SUPPORTED_DURABLE_KINDS:
        raise AccountingConflict(
            f"{evidence.kind} has no qualified durable corporate-action accounting mapping"
        )
    order_key = _logical_order_key(evidence)
    active = _active_action_transactions(economic_book, order_key)
    replacement = _dividend_transaction(evidence, transition)

    if not is_revision:
        if active and not exact_retry:
            raise AccountingConflict(
                "corporate action already has active economics without source revision history"
            )
        if exact_retry and replacement is None and active:
            raise AccountingConflict(
                "zero-effect corporate-action retry conflicts with active economics"
            )
        return () if replacement is None else (replacement,)

    if exact_retry:
        expected_reversal_id = _transaction_id(evidence, suffix="reversal")
        expected_replacement_id = _transaction_id(evidence, suffix="effect")
        all_transactions = tuple(economic_book.transactions)
        committed_reversal = next(
            (item for item in all_transactions if item.transaction_id == expected_reversal_id),
            None,
        )
        committed_replacement = next(
            (item for item in all_transactions if item.transaction_id == expected_replacement_id),
            None,
        )
        if committed_reversal is None:
            raise AccountingConflict(
                "revised corporate-action evidence exists without its reversal economics"
            )
        original_id = committed_reversal.reverses_transaction_id
        original = next(
            (item for item in all_transactions if item.transaction_id == original_id),
            None,
        )
        if original is None:
            raise AccountingConflict(
                "corporate-action correction reversal lacks original economics"
            )
        rebuilt_reversal = reverse_transaction(
            original,
            transaction_id=expected_reversal_id,
            cause_event_id=committed_reversal.cause_event_id,
            observed_at=evidence.observed_at,
        )
        if rebuilt_reversal != committed_reversal:
            raise AccountingConflict(
                "corporate-action correction reversal conflicts with durable economics"
            )
        if replacement is None:
            if committed_replacement is not None:
                raise AccountingConflict(
                    "zero-effect correction conflicts with durable replacement economics"
                )
            return (rebuilt_reversal,)
        rebuilt_replacement = _dividend_transaction(
            evidence,
            transition,
            corrects_transaction_id=original.transaction_id,
            economic_effective_at=original.economic_effective_at,
            economic_order_key=original.economic_order_key,
        )
        assert rebuilt_replacement is not None
        if committed_replacement is None or committed_replacement != rebuilt_replacement:
            raise AccountingConflict(
                "corporate-action correction replacement conflicts with durable economics"
            )
        return rebuilt_reversal, rebuilt_replacement

    if len(active) > 1:
        raise AccountingConflict("corporate action has ambiguous active economic history")
    if not active:
        return () if replacement is None else (replacement,)

    previous = active[0]
    if previous.economic_effective_at != evidence.effective_at:
        raise AccountingConflict(
            "corporate-action correction cannot change economic effective time"
        )
    reversal = reverse_transaction(
        previous,
        transaction_id=_transaction_id(evidence, suffix="reversal"),
        cause_event_id=_identity(
            "corporate-action-cause",
            evidence.logical_event_id,
            evidence.revision,
            "reversal",
        ),
        observed_at=evidence.observed_at,
    )
    if replacement is None:
        return (reversal,)
    replacement = _dividend_transaction(
        evidence,
        transition,
        corrects_transaction_id=previous.transaction_id,
        economic_effective_at=previous.economic_effective_at,
        economic_order_key=previous.economic_order_key,
    )
    assert replacement is not None
    return reversal, replacement


def commit_provider_corporate_action(
    *,
    store: JournalStore,
    economic_book: DurableProviderEconomicBook,
    corporate_book: CorporateActionBook,
    evidence: ProviderCorporateActionEvidence,
) -> CorporateActionCommitResult:
    """Atomically journal provider evidence and its qualified economic effect."""

    if not isinstance(store, JournalStore):
        raise TypeError("store must be JournalStore")
    if not isinstance(economic_book, DurableProviderEconomicBook):
        raise TypeError("economic_book must be DurableProviderEconomicBook")
    if not isinstance(corporate_book, CorporateActionBook):
        raise TypeError("corporate_book must be CorporateActionBook")
    if not isinstance(evidence, ProviderCorporateActionEvidence):
        raise TypeError(
            "evidence must be ProviderCorporateActionEvidence; caller-authored CorporateEvent is not authority"
        )
    if economic_book.store is not store:
        raise ValueError("corporate-action evidence and economic book must share one JournalStore")
    if (
        economic_book.provider_id != evidence.provider_id
        or economic_book.account_id != evidence.account_id
        or economic_book.environment != evidence.environment
    ):
        raise ValueError("corporate-action evidence scope does not match economic book")

    event = evidence.to_corporate_event()
    candidate, transition = _candidate_book(corporate_book, event)
    aggregate_id = _identity(
        "corporate-action-source",
        evidence.provider_id,
        evidence.account_id,
        evidence.environment,
        evidence.external_action_id,
    )
    history = _source_history(store, aggregate_id)
    source_payload = _evidence_payload(evidence)
    exact_retry, previous_revision = _validate_revision(history, source_payload)
    is_revision = source_payload.get("supersedes_revision") is not None
    transactions = _economic_transactions(
        economic_book,
        evidence,
        transition,
        is_revision=is_revision,
        exact_retry=exact_retry,
    )

    economic_plan = (
        economic_book.prepare_batch_mutation(
            transactions,
            committed_at=evidence.observed_at,
        )
        if transactions
        else None
    )

    if exact_retry:
        if economic_plan is not None and not economic_plan.already_committed:
            raise AccountingConflict(
                "corporate-action source evidence exists without matching economics"
            )
        return CorporateActionCommitResult(
            inserted=False,
            event=event,
            transition=transition,
            next_state=candidate.state,
            source_event_id=str(history[-1]["event_id"]),
            transaction_ids=tuple(item.transaction_id for item in transactions),
        )

    if economic_plan is not None and economic_plan.already_committed:
        raise AccountingConflict(
            "corporate-action economics exist without matching source evidence"
        )

    source_version = len(history) + 1
    source_event_id = str(
        uuid5(
            NAMESPACE_URL,
            "https://events.autotrade.local/corporate-action-source/"
            + canonical_json([aggregate_id, str(source_version), evidence.revision]),
        )
    )
    source_envelope = {
        "event_id": source_event_id,
        "event_type": _SOURCE_EVENT,
        "aggregate_type": _SOURCE_AGGREGATE,
        "aggregate_id": aggregate_id,
        "aggregate_version": str(source_version),
        "committed_at": evidence.observed_at,
        "payload": source_payload,
        "payload_hash": payload_digest(source_payload),
    }
    events: list[tuple[dict[str, object], str | None]] = [
        (source_envelope, None)
    ]
    if economic_plan is not None:
        if economic_plan.envelope is None:
            raise AccountingConflict("fresh corporate-action economics lack durable envelope")
        events.append((economic_plan.envelope, "autotrade.economic.events"))

    request = {
        "schema_version": "1.0.0",
        "source": source_payload,
        "economic_batch": (
            None if economic_plan is None else economic_plan.request
        ),
    }
    result = {
        "source_event_id": source_event_id,
        "revision": evidence.revision,
        "transaction_ids": [item.transaction_id for item in transactions],
        "next_state_digest": payload_digest(
            {
                "symbol": candidate.state.symbol,
                "quantity": str(candidate.state.quantity),
                "total_basis": str(candidate.state.total_basis),
                "settled_cash": str(candidate.state.settled_cash),
                "unsettled_cash": str(candidate.state.unsettled_cash),
                "currency": candidate.state.currency,
            }
        ),
    }
    command_id = str(
        uuid5(
            NAMESPACE_URL,
            "https://commands.autotrade.local/corporate-action/"
            + canonical_json([aggregate_id, evidence.revision]),
        )
    )
    idempotency = _identity(
        "corporate-action-idempotency",
        aggregate_id,
        evidence.revision,
    )
    try:
        _, inserted, _ = store.commit_command(
            command_id=command_id,
            actor=_ACTOR,
            environment=evidence.environment,
            idempotency_key=idempotency,
            request=request,
            result=result,
            state_version=max(
                source_version,
                0 if economic_plan is None else economic_plan.aggregate_version,
            ),
            events=events,
        )
    except Exception:
        economic_book.refresh()
        raise
    economic_book.refresh()
    return CorporateActionCommitResult(
        inserted=inserted,
        event=event,
        transition=transition,
        next_state=candidate.state,
        source_event_id=source_event_id,
        transaction_ids=tuple(item.transaction_id for item in transactions),
    )
