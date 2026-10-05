"""Frozen canonical financing-component evidence for WP-63 cost composition.

This module does not create a second financing ledger and does not claim a complete
ablation cost.  It composes the existing ``DurableFinancingBook`` revision owner
with the existing historical ``ProviderEconomicCut`` owner at one exact global
JournalStore visibility cut.  The result is one re-verifiable financing component
whose numeric charge is owned by the financing revision authority and whose
postings are proven present in the same historical provider-economic prefix.
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
    _revision_book_digest,
)
from .financing import FinancingEvent
from .persistence import (
    JournalStore,
    JournalStoreIdentity,
    require_exact_journal_store_identity,
)
from .provider_activity_accounting import (
    DurableProviderEconomicBook,
    ProviderEconomicCut,
    reverify_provider_economic_cut,
)


_FINANCING_EVENTS_RAW = DurableFinancingBook.__dict__["_events"]
_FINANCING_REPLAY_RAW = DurableFinancingBook.__dict__["_book_from_durable_events"]
_FINANCING_ECONOMIC_TX_RAW = DurableFinancingBook.__dict__["_economic_transaction"]
_ECONOMIC_EVENTS_RAW = DurableProviderEconomicBook.__dict__["_events"]
_RESOLVE_ECONOMIC_CUT_RAW = DurableProviderEconomicBook.__dict__["resolve_historical_cut"]
_REVERIFY_ECONOMIC_CUT = reverify_provider_economic_cut
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_EVIDENCE_KIND = "CANONICAL_FINANCING_COMPONENT"


def _text(value: object, *, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise FinancingConflict(f"{name} must be exact canonical non-empty text")
    return value


def _digest(value: object, *, name: str) -> str:
    text = _text(value, name=name)
    if _SHA256.fullmatch(text) is None:
        raise FinancingConflict(f"{name} must be an exact canonical sha256 digest")
    return text


def _positive_int(value: object, *, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise FinancingConflict(f"{name} must be an exact positive integer")
    return value


def _store_identity_material(identity: JournalStoreIdentity) -> dict[str, object]:
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
    economic_book = state.get("economic_book")
    if type(store) is not JournalStore:
        raise FinancingConflict("durable financing JournalStore is not canonical")
    if type(economic_book) is not DurableProviderEconomicBook:
        raise FinancingConflict("durable financing economic owner is not canonical")
    if economic_book.store is not store:
        # Existing constructor permits a distinct JournalStore object only when it
        # resolves to the same backing generation.  For authority composition we
        # deliberately require the exact selected object so reads cannot split.
        raise FinancingConflict(
            "ablation financing evidence requires one exact JournalStore object"
        )
    if (
        state.get("provider_id") != economic_book.provider_id
        or state.get("account_id") != economic_book.account_id
        or state.get("environment") != economic_book.environment
    ):
        raise FinancingConflict(
            "durable financing scope does not match provider economic authority"
        )
    if DurableProviderEconomicBook.__dict__.get("_events") is not _ECONOMIC_EVENTS_RAW:
        raise FinancingConflict(
            "provider economic event reader changed after financing composition"
        )
    if (
        DurableProviderEconomicBook.__dict__.get("resolve_historical_cut")
        is not _RESOLVE_ECONOMIC_CUT_RAW
    ):
        raise FinancingConflict(
            "provider economic historical-cut executable changed after financing composition"
        )


def _canonical_material(
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
    provider_economic_cut_digest: str,
    provider_economic_resulting_book_digest: str,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "evidence_kind": _EVIDENCE_KIND,
        "terminal_cost_composite": False,
        "store_identity": _store_identity_material(store_identity),
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


def _evidence_digest(material: dict[str, object]) -> str:
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
    provider_economic_cut_digest: str
    provider_economic_resulting_book_digest: str
    evidence_digest: str

    def __post_init__(self) -> None:
        if self.evidence_kind != _EVIDENCE_KIND:
            raise FinancingConflict("financing evidence kind is not canonical")
        if type(self.terminal_cost_composite) is not bool or self.terminal_cost_composite:
            raise FinancingConflict(
                "financing component cannot claim terminal cost-composite authority"
            )
        _store_identity_material(self.store_identity)
        for name in (
            "provider_id",
            "account_id",
            "environment",
            "charge_id",
            "aggregate_id",
            "event_id",
            "unit",
        ):
            _text(getattr(self, name), name=name)
        for name in (
            "aggregate_version",
            "journal_sequence",
            "visibility_journal_sequence",
            "revision",
        ):
            _positive_int(getattr(self, name), name=name)
        if self.journal_sequence > self.visibility_journal_sequence:
            raise FinancingConflict(
                "financing event cannot follow its frozen visibility cut"
            )
        if type(self.current_final_charge) is not Decimal:
            raise FinancingConflict("current_final_charge must be exact Decimal")
        if not self.current_final_charge.is_finite() or self.current_final_charge < 0:
            raise FinancingConflict(
                "current_final_charge must be finite and non-negative"
            )
        _digest(self.payload_hash, name="payload_hash")
        _digest(self.revision_book_digest, name="revision_book_digest")
        _digest(self.provider_economic_cut_digest, name="provider_economic_cut_digest")
        _digest(
            self.provider_economic_resulting_book_digest,
            name="provider_economic_resulting_book_digest",
        )
        if type(self.contributing_transactions) is not tuple:
            raise FinancingConflict("contributing_transactions must be an exact tuple")
        transaction_ids: list[str] = []
        for item in self.contributing_transactions:
            if type(item) is not tuple or len(item) != 2:
                raise FinancingConflict(
                    "contributing financing transaction entry is not canonical"
                )
            transaction_ids.append(_text(item[0], name="transaction_id"))
            _digest(item[1], name="transaction_digest")
        if len(transaction_ids) != len(set(transaction_ids)):
            raise FinancingConflict(
                "contributing financing transactions contain duplicate identity"
            )
        _digest(self.evidence_digest, name="evidence_digest")
        ResolvedAblationFinancingComponentEvidence.verify_integrity(self)

    def verify_integrity(self) -> None:
        material = _canonical_material(
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
            provider_economic_resulting_book_digest=(
                self.provider_economic_resulting_book_digest
            ),
        )
        if self.evidence_digest != _evidence_digest(material):
            raise FinancingConflict(
                "financing component evidence digest does not match canonical material"
            )


def _visible_prefix(events: list[dict[str, object]], visibility: int) -> list[dict[str, object]]:
    visible: list[dict[str, object]] = []
    for event in events:
        sequence = event.get("journal_sequence")
        _positive_int(sequence, name="journal_sequence")
        if sequence <= visibility:
            visible.append(event)
    return visible


def resolve_ablation_financing_component_evidence(
    owner: DurableFinancingBook,
    *,
    charge_id: str,
    expected_aggregate_version: int,
    visibility_journal_sequence: int | None = None,
) -> ResolvedAblationFinancingComponentEvidence:
    """Resolve the latest financing revision visible at one exact durable cut."""

    _assert_owner(owner)
    charge = _text(charge_id, name="charge_id")
    expected_version = _positive_int(
        expected_aggregate_version,
        name="expected_aggregate_version",
    )
    store = object.__getattribute__(owner, "store")
    identity = require_exact_journal_store_identity(
        store.store_identity,
        subject="ablation financing evidence JournalStore",
    )
    current_sequence = JournalStore.current_journal_sequence(store)
    if visibility_journal_sequence is None:
        visibility = current_sequence
    else:
        visibility = _positive_int(
            visibility_journal_sequence,
            name="visibility_journal_sequence",
        )
        if visibility > current_sequence:
            raise FinancingConflict(
                "financing visibility cut is beyond durable journal"
            )

    events = _FINANCING_EVENTS_RAW(owner, charge)
    visible = _visible_prefix(events, visibility)
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
    if latest.revision != expected_version:
        raise FinancingConflict(
            "financing revision identity does not match durable aggregate version"
        )
    final_charge = book.final_charge(charge)
    if type(final_charge) is not Decimal:
        raise FinancingConflict("financing final charge is not canonical Decimal")

    aggregate_id = owner._aggregate_id(charge)
    contributions: list[tuple[str, str]] = []
    replay_history: list[FinancingEvent] = []
    from .financing import FinancingRevisionBook
    from .durable_financing import _record_exact

    for durable in visible:
        payload = durable.get("payload")
        if type(payload) is not dict:
            raise FinancingConflict("financing durable payload is not canonical")
        candidate = FinancingEvent.create(
            charge_id=payload.get("charge_id"),
            revision=payload.get("revision"),
            kind=payload.get("kind"),
            effective_at=payload.get("effective_at"),
            available_at=payload.get("available_at"),
            unit=payload.get("unit"),
            amount=payload.get("amount"),
            source_account=payload.get("source_account"),
            evidence_ref=payload.get("evidence_ref"),
        )
        partial = FinancingRevisionBook(replay_history)
        update = _record_exact(partial, candidate)
        replay_history = list(partial.events)
        if update.economic_delta != 0:
            event_id = _text(durable.get("event_id"), name="financing event_id")
            transaction = _FINANCING_ECONOMIC_TX_RAW(
                owner,
                aggregate_id=aggregate_id,
                event_id=event_id,
                event=candidate,
                economic_delta=update.economic_delta,
            )
            contributions.append(
                (transaction.transaction_id, transaction_digest(transaction))
            )

    economic_book = object.__getattribute__(owner, "economic_book")
    economic_events = _ECONOMIC_EVENTS_RAW(economic_book)
    economic_visible = _visible_prefix(economic_events, visibility)
    if contributions and not economic_visible:
        raise FinancingConflict(
            "financing component lacks provider economics at visibility cut"
        )
    if economic_visible:
        economic_version = economic_visible[-1].get("aggregate_version")
        _positive_int(economic_version, name="economic aggregate_version")
        economic_cut = _RESOLVE_ECONOMIC_CUT_RAW(
            economic_book,
            economic_version,
            visibility_journal_sequence=visibility,
        )
        if type(economic_cut) is not ProviderEconomicCut:
            raise FinancingConflict("provider economic historical cut is not canonical")
        economic_index = dict(economic_cut.transaction_digests)
        for transaction_id, digest in contributions:
            if economic_index.get(transaction_id) != digest:
                raise FinancingConflict(
                    "financing transaction is absent from frozen provider economic cut"
                )
    else:
        raise FinancingConflict(
            "financing component requires a provider economic cut at visibility"
        )

    terminal_sequence = _positive_int(
        terminal.get("journal_sequence"),
        name="terminal journal_sequence",
    )
    event_id = _text(terminal.get("event_id"), name="terminal event_id")
    payload_hash = _digest(terminal.get("payload_hash"), name="terminal payload_hash")
    material = _canonical_material(
        store_identity=identity,
        provider_id=owner.provider_id,
        account_id=owner.account_id,
        environment=owner.environment,
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
        revision_book_digest=_revision_book_digest(list(book.events)),
        contributing_transactions=tuple(contributions),
        provider_economic_cut_digest=economic_cut.cut_digest,
        provider_economic_resulting_book_digest=economic_cut.resulting_book_digest,
    )
    return ResolvedAblationFinancingComponentEvidence(
        evidence_kind=_EVIDENCE_KIND,
        terminal_cost_composite=False,
        store_identity=identity,
        provider_id=owner.provider_id,
        account_id=owner.account_id,
        environment=owner.environment,
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
        revision_book_digest=_revision_book_digest(list(book.events)),
        contributing_transactions=tuple(contributions),
        provider_economic_cut_digest=economic_cut.cut_digest,
        provider_economic_resulting_book_digest=economic_cut.resulting_book_digest,
        evidence_digest=_evidence_digest(material),
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
