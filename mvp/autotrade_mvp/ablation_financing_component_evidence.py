"""Frozen canonical financing-component evidence for WP-63 cost composition.

The resolver composes the existing DurableFinancingBook revision owner with the
existing ProviderEconomicCut owner at one exact JournalStore visibility cut.  It
never creates a second ledger and never claims a complete ablation cost.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256
import json
import re

from .accounting import transaction_digest
from .durable_financing import (
    DurableFinancingBook,
    FinancingConflict,
    _decimal_text,
    _event_from_payload,
    _record_exact,
    _revision_book_digest,
)
from .financing import FinancingEvent, FinancingRevisionBook
from .persistence import (
    JournalStore,
    JournalStoreIdentity,
    require_exact_journal_store_identity,
)
from .provider_activity_accounting import DurableProviderEconomicBook, ProviderEconomicCut


_FINANCING_EVENTS_RAW = DurableFinancingBook.__dict__["_events"]
_FINANCING_REPLAY_RAW = DurableFinancingBook.__dict__["_book_from_durable_events"]
_FINANCING_ECONOMIC_TX_RAW = DurableFinancingBook.__dict__["_economic_transaction"]
_FINANCING_AGGREGATE_ID_RAW = DurableFinancingBook.__dict__["_aggregate_id"]
_ECONOMIC_EVENTS_RAW = DurableProviderEconomicBook.__dict__["_events"]
_RESOLVE_ECONOMIC_CUT_RAW = DurableProviderEconomicBook.__dict__["resolve_historical_cut"]
_EVENT_FROM_PAYLOAD = _event_from_payload
_RECORD_EXACT = _record_exact
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_EVIDENCE_KIND = "CANONICAL_FINANCING_COMPONENT"


def _text(value: object, *, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise FinancingConflict(f"{name} must be exact canonical non-empty text")
    return value


def _digest(value: object, *, name: str) -> str:
    value = _text(value, name=name)
    if _SHA256.fullmatch(value) is None:
        raise FinancingConflict(f"{name} must be an exact canonical sha256 digest")
    return value


def _positive(value: object, *, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise FinancingConflict(f"{name} must be an exact positive integer")
    return value


def _store_material(identity: JournalStoreIdentity) -> dict[str, object]:
    require_exact_journal_store_identity(
        identity,
        subject="ablation financing evidence store identity",
    )
    return {
        "canonical_path": identity.canonical_path,
        "filesystem_device": identity.filesystem_device,
        "filesystem_inode": identity.filesystem_inode,
        "identity_source": identity.identity_source,
        "windows_volume_serial": identity.windows_volume_serial,
        "windows_file_index_high": identity.windows_file_index_high,
        "windows_file_index_low": identity.windows_file_index_low,
    }


def _assert_owner(owner: DurableFinancingBook) -> None:
    if type(owner) is not DurableFinancingBook:
        raise TypeError("owner must be exact DurableFinancingBook")
    for name, expected in (
        ("_events", _FINANCING_EVENTS_RAW),
        ("_book_from_durable_events", _FINANCING_REPLAY_RAW),
        ("_economic_transaction", _FINANCING_ECONOMIC_TX_RAW),
        ("_aggregate_id", _FINANCING_AGGREGATE_ID_RAW),
    ):
        if DurableFinancingBook.__dict__.get(name) is not expected:
            raise FinancingConflict(
                f"durable financing {name} executable changed after composition"
            )
    state = object.__getattribute__(owner, "__dict__")
    if type(state) is not dict:
        raise FinancingConflict("durable financing owner state is not canonical")
    shadowed = tuple(
        sorted(
            name
            for name in state
            if name in DurableFinancingBook.__dict__
            and callable(DurableFinancingBook.__dict__.get(name))
        )
    )
    if shadowed:
        raise FinancingConflict(
            "durable financing owner shadows canonical methods: " + ", ".join(shadowed)
        )
    store = state.get("store")
    economic = state.get("economic_book")
    if type(store) is not JournalStore or type(economic) is not DurableProviderEconomicBook:
        raise FinancingConflict("durable financing authorities are not canonical")
    if economic.store is not store:
        raise FinancingConflict(
            "ablation financing evidence requires one exact JournalStore object"
        )
    if (
        state.get("provider_id") != economic.provider_id
        or state.get("account_id") != economic.account_id
        or state.get("environment") != economic.environment
    ):
        raise FinancingConflict(
            "durable financing scope does not match provider economic authority"
        )
    if DurableProviderEconomicBook.__dict__.get("_events") is not _ECONOMIC_EVENTS_RAW:
        raise FinancingConflict("provider economic event reader changed after composition")
    if (
        DurableProviderEconomicBook.__dict__.get("resolve_historical_cut")
        is not _RESOLVE_ECONOMIC_CUT_RAW
    ):
        raise FinancingConflict(
            "provider economic historical-cut executable changed after composition"
        )


def _material(
    *,
    store_identity: JournalStoreIdentity,
    provider_id: str,
    account_id: str,
    environment: str,
    charge_id: str,
    aggregate_id: str,
    aggregate_version: int,
    journal_sequence: int,
    visibility_journal_sequence: int,
    event_id: str,
    payload_hash: str,
    revision: int,
    unit: str,
    current_final_charge: str,
    revision_book_digest: str,
    contributing_transactions: tuple[tuple[str, str], ...],
    provider_economic_cut_digest: str | None,
    provider_economic_resulting_book_digest: str | None,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "evidence_kind": _EVIDENCE_KIND,
        "terminal_cost_composite": False,
        "store_identity": _store_material(store_identity),
        "provider_id": provider_id,
        "account_id": account_id,
        "environment": environment,
        "charge_id": charge_id,
        "aggregate_id": aggregate_id,
        "aggregate_version": aggregate_version,
        "journal_sequence": journal_sequence,
        "visibility_journal_sequence": visibility_journal_sequence,
        "event_id": event_id,
        "payload_hash": payload_hash,
        "revision": revision,
        "unit": unit,
        "current_final_charge": current_final_charge,
        "revision_book_digest": revision_book_digest,
        "contributing_transactions": [list(item) for item in contributing_transactions],
        "provider_economic_cut_digest": provider_economic_cut_digest,
        "provider_economic_resulting_book_digest": provider_economic_resulting_book_digest,
    }


def _hash(material: dict[str, object]) -> str:
    raw = json.dumps(
        material,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + sha256(raw).hexdigest()


@dataclass(frozen=True)
class ResolvedAblationFinancingComponentEvidence:
    evidence_kind: str
    terminal_cost_composite: bool
    store_identity: JournalStoreIdentity
    provider_id: str
    account_id: str
    environment: str
    charge_id: str
    aggregate_id: str
    aggregate_version: int
    journal_sequence: int
    visibility_journal_sequence: int
    event_id: str
    payload_hash: str
    revision: int
    unit: str
    current_final_charge: Decimal
    revision_book_digest: str
    contributing_transactions: tuple[tuple[str, str], ...]
    provider_economic_cut_digest: str | None
    provider_economic_resulting_book_digest: str | None
    evidence_digest: str

    def __post_init__(self) -> None:
        if self.evidence_kind != _EVIDENCE_KIND:
            raise FinancingConflict("financing evidence kind is not canonical")
        if type(self.terminal_cost_composite) is not bool or self.terminal_cost_composite:
            raise FinancingConflict(
                "financing component cannot claim terminal cost-composite authority"
            )
        _store_material(self.store_identity)
        for name in (
            "provider_id", "account_id", "environment", "charge_id",
            "aggregate_id", "event_id", "unit",
        ):
            _text(getattr(self, name), name=name)
        for name in (
            "aggregate_version", "journal_sequence",
            "visibility_journal_sequence", "revision",
        ):
            _positive(getattr(self, name), name=name)
        if self.journal_sequence > self.visibility_journal_sequence:
            raise FinancingConflict("financing event follows its frozen visibility cut")
        if type(self.current_final_charge) is not Decimal:
            raise FinancingConflict("current_final_charge must be exact Decimal")
        if not self.current_final_charge.is_finite() or self.current_final_charge < 0:
            raise FinancingConflict("current_final_charge must be finite and non-negative")
        _digest(self.payload_hash, name="payload_hash")
        _digest(self.revision_book_digest, name="revision_book_digest")
        if type(self.contributing_transactions) is not tuple:
            raise FinancingConflict("contributing_transactions must be an exact tuple")
        ids: list[str] = []
        for item in self.contributing_transactions:
            if type(item) is not tuple or len(item) != 2:
                raise FinancingConflict("contributing transaction entry is not canonical")
            ids.append(_text(item[0], name="transaction_id"))
            _digest(item[1], name="transaction_digest")
        if len(ids) != len(set(ids)):
            raise FinancingConflict("contributing transactions contain duplicate identity")
        if self.contributing_transactions:
            _digest(self.provider_economic_cut_digest, name="provider_economic_cut_digest")
            _digest(
                self.provider_economic_resulting_book_digest,
                name="provider_economic_resulting_book_digest",
            )
        elif (
            self.provider_economic_cut_digest is not None
            or self.provider_economic_resulting_book_digest is not None
        ):
            raise FinancingConflict(
                "zero-posting financing evidence cannot claim unrelated economic-cut authority"
            )
        _digest(self.evidence_digest, name="evidence_digest")
        ResolvedAblationFinancingComponentEvidence.verify_integrity(self)

    def verify_integrity(self) -> None:
        material = _material(
            store_identity=self.store_identity,
            provider_id=self.provider_id,
            account_id=self.account_id,
            environment=self.environment,
            charge_id=self.charge_id,
            aggregate_id=self.aggregate_id,
            aggregate_version=self.aggregate_version,
            journal_sequence=self.journal_sequence,
            visibility_journal_sequence=self.visibility_journal_sequence,
            event_id=self.event_id,
            payload_hash=self.payload_hash,
            revision=self.revision,
            unit=self.unit,
            current_final_charge=_decimal_text(self.current_final_charge),
            revision_book_digest=self.revision_book_digest,
            contributing_transactions=self.contributing_transactions,
            provider_economic_cut_digest=self.provider_economic_cut_digest,
            provider_economic_resulting_book_digest=self.provider_economic_resulting_book_digest,
        )
        if self.evidence_digest != _hash(material):
            raise FinancingConflict(
                "financing component evidence digest does not match canonical material"
            )


def _visible(events: list[dict[str, object]], visibility: int) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for event in events:
        sequence = _positive(event.get("journal_sequence"), name="journal_sequence")
        if sequence <= visibility:
            result.append(event)
    return result


def resolve_ablation_financing_component_evidence(
    owner: DurableFinancingBook,
    *,
    charge_id: str,
    expected_aggregate_version: int,
    visibility_journal_sequence: int | None = None,
) -> ResolvedAblationFinancingComponentEvidence:
    """Resolve the latest FINAL financing revision visible at one durable cut."""

    _assert_owner(owner)
    charge = _text(charge_id, name="charge_id")
    expected_version = _positive(
        expected_aggregate_version,
        name="expected_aggregate_version",
    )
    state = object.__getattribute__(owner, "__dict__")
    store = state["store"]
    economic_book = state["economic_book"]
    identity = require_exact_journal_store_identity(
        store.store_identity,
        subject="ablation financing evidence JournalStore",
    )
    current = JournalStore.current_journal_sequence(store)
    visibility = (
        current
        if visibility_journal_sequence is None
        else _positive(visibility_journal_sequence, name="visibility_journal_sequence")
    )
    if visibility > current:
        raise FinancingConflict("financing visibility cut is beyond durable journal")

    visible = _visible(_FINANCING_EVENTS_RAW(owner, charge), visibility)
    if not visible:
        raise FinancingConflict("financing charge has no state at visibility cut")
    terminal = visible[-1]
    if terminal.get("aggregate_version") != expected_version:
        raise FinancingConflict(
            "requested financing aggregate version is stale at visibility cut"
        )
    if len(visible) != expected_version:
        raise FinancingConflict("financing visible aggregate prefix is not contiguous")

    book = _FINANCING_REPLAY_RAW(owner, charge, visible)
    latest = book.latest(charge)
    if type(latest) is not FinancingEvent:
        raise FinancingConflict("financing visible prefix has no canonical latest event")
    if latest.kind != "FINAL":
        raise FinancingConflict(
            "financing component is not mature FINAL evidence at visibility cut"
        )

    aggregate_id = _FINANCING_AGGREGATE_ID_RAW(owner, charge)
    contributions: list[tuple[str, str]] = []
    replay_history: list[FinancingEvent] = []
    final_charge = Decimal("0")
    for durable in visible:
        payload = durable.get("payload")
        if type(payload) is not dict:
            raise FinancingConflict("financing durable payload is not canonical")
        candidate = _EVENT_FROM_PAYLOAD(payload)
        partial = FinancingRevisionBook(replay_history)
        update = _RECORD_EXACT(partial, candidate)
        replay_history = list(partial.events)
        final_charge = update.current_final_charge
        if update.economic_delta != 0:
            durable_event_id = _text(
                durable.get("event_id"),
                name="financing event_id",
            )
            transaction = _FINANCING_ECONOMIC_TX_RAW(
                owner,
                aggregate_id=aggregate_id,
                event_id=durable_event_id,
                event=candidate,
                economic_delta=update.economic_delta,
            )
            contributions.append(
                (transaction.transaction_id, transaction_digest(transaction))
            )

    economic_cut_digest: str | None = None
    economic_book_digest: str | None = None
    if contributions:
        economic_visible = _visible(_ECONOMIC_EVENTS_RAW(economic_book), visibility)
        if not economic_visible:
            raise FinancingConflict(
                "financing component lacks provider economics at visibility cut"
            )
        economic_version = _positive(
            economic_visible[-1].get("aggregate_version"),
            name="economic aggregate_version",
        )
        economic_cut = _RESOLVE_ECONOMIC_CUT_RAW(
            economic_book,
            economic_version,
            visibility_journal_sequence=visibility,
        )
        if type(economic_cut) is not ProviderEconomicCut:
            raise FinancingConflict("provider economic historical cut is not canonical")
        index = dict(economic_cut.transaction_digests)
        for transaction_id, digest in contributions:
            if index.get(transaction_id) != digest:
                raise FinancingConflict(
                    "financing transaction is absent from frozen provider economic cut"
                )
        economic_cut_digest = economic_cut.cut_digest
        economic_book_digest = economic_cut.resulting_book_digest

    terminal_sequence = _positive(
        terminal.get("journal_sequence"),
        name="terminal journal_sequence",
    )
    event_id = _text(terminal.get("event_id"), name="terminal event_id")
    payload_hash = _digest(terminal.get("payload_hash"), name="terminal payload_hash")
    revision_digest = _revision_book_digest(list(book.events))
    material = _material(
        store_identity=identity,
        provider_id=state["provider_id"],
        account_id=state["account_id"],
        environment=state["environment"],
        charge_id=charge,
        aggregate_id=aggregate_id,
        aggregate_version=expected_version,
        journal_sequence=terminal_sequence,
        visibility_journal_sequence=visibility,
        event_id=event_id,
        payload_hash=payload_hash,
        revision=latest.revision,
        unit=latest.unit,
        current_final_charge=_decimal_text(final_charge),
        revision_book_digest=revision_digest,
        contributing_transactions=tuple(contributions),
        provider_economic_cut_digest=economic_cut_digest,
        provider_economic_resulting_book_digest=economic_book_digest,
    )
    return ResolvedAblationFinancingComponentEvidence(
        evidence_kind=_EVIDENCE_KIND,
        terminal_cost_composite=False,
        store_identity=identity,
        provider_id=state["provider_id"],
        account_id=state["account_id"],
        environment=state["environment"],
        charge_id=charge,
        aggregate_id=aggregate_id,
        aggregate_version=expected_version,
        journal_sequence=terminal_sequence,
        visibility_journal_sequence=visibility,
        event_id=event_id,
        payload_hash=payload_hash,
        revision=latest.revision,
        unit=latest.unit,
        current_final_charge=final_charge,
        revision_book_digest=revision_digest,
        contributing_transactions=tuple(contributions),
        provider_economic_cut_digest=economic_cut_digest,
        provider_economic_resulting_book_digest=economic_book_digest,
        evidence_digest=_hash(material),
    )


def reverify_ablation_financing_component_evidence(
    owner: DurableFinancingBook,
    evidence: ResolvedAblationFinancingComponentEvidence,
) -> ResolvedAblationFinancingComponentEvidence:
    if type(evidence) is not ResolvedAblationFinancingComponentEvidence:
        raise TypeError(
            "evidence must be exact ResolvedAblationFinancingComponentEvidence"
        )
    ResolvedAblationFinancingComponentEvidence.verify_integrity(evidence)
    resolved = resolve_ablation_financing_component_evidence(
        owner,
        charge_id=evidence.charge_id,
        expected_aggregate_version=evidence.aggregate_version,
        visibility_journal_sequence=evidence.visibility_journal_sequence,
    )
    if resolved != evidence:
        raise FinancingConflict(
            "financing component evidence does not match canonical durable replay"
        )
    return resolved
