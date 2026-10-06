"""Non-numeric provider-economic prefix provenance for WP-63 ablation evidence.

This module composes the existing durable provider economic-book owner.  It proves
which exact historical transaction prefix was visible at one independently selected
JournalStore cut.  It deliberately does *not* claim that the prefix is the complete
registered ablation cost composite and never emits a numeric cost or component
allocation.

Terminal WP-63 cost evidence still requires an independently owned projection for
all registered components (commission, spread, slippage, financing, funding,
borrow, market_data, model_compute, infrastructure, tax_estimate), plus valuation
authority when the registered common unit requires conversion.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import re

from .persistence import JournalStoreIdentity, require_exact_journal_store_identity
from .provider_activity_accounting import (
    AccountingConflict,
    DurableProviderEconomicBook,
    ProviderEconomicCut,
    reverify_provider_economic_cut,
)


_REVERIFY_CUT = reverify_provider_economic_cut
_RESOLVE_CUT_RAW = DurableProviderEconomicBook.__dict__["resolve_historical_cut"]
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_EVIDENCE_KIND = "PROVIDER_ECONOMIC_PREFIX_ONLY"


def _text(value: object, *, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise AccountingConflict(f"{name} must be exact canonical non-empty text")
    return value


def _digest(value: object, *, name: str) -> str:
    text = _text(value, name=name)
    if _SHA256.fullmatch(text) is None:
        raise AccountingConflict(f"{name} must be an exact canonical sha256 digest")
    return text


def _store_identity_material(identity: JournalStoreIdentity) -> dict[str, object]:
    require_exact_journal_store_identity(
        identity,
        subject="ablation provider-economic provenance store identity",
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


def _cut_material(cut: ProviderEconomicCut) -> dict[str, object]:
    if type(cut) is not ProviderEconomicCut:
        raise TypeError("cut must be exact ProviderEconomicCut")
    return {
        "schema_version": 1,
        "evidence_kind": _EVIDENCE_KIND,
        "terminal_cost_composite": False,
        "provider_id": cut.provider_id,
        "account_id": cut.account_id,
        "environment": cut.environment,
        "book_id": cut.book_id,
        "aggregate_version": cut.aggregate_version,
        "journal_sequence": cut.journal_sequence,
        "visibility_journal_sequence": cut.visibility_journal_sequence,
        "event_id": cut.event_id,
        "payload_hash": cut.payload_hash,
        "transaction_digests": [list(item) for item in cut.transaction_digests],
        "resulting_book_digest": cut.resulting_book_digest,
        "economic_cut_digest": cut.cut_digest,
        "store_identity": _store_identity_material(cut.store_identity),
    }


def _provenance_digest(material: dict[str, object]) -> str:
    raw = json.dumps(
        material,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + sha256(raw).hexdigest()


def _assert_owner_executables(book: DurableProviderEconomicBook) -> None:
    if type(book) is not DurableProviderEconomicBook:
        raise TypeError("book must be exact DurableProviderEconomicBook")
    if (
        DurableProviderEconomicBook.__dict__.get("resolve_historical_cut")
        is not _RESOLVE_CUT_RAW
    ):
        raise AccountingConflict(
            "durable provider economic historical-cut executable changed after composition"
        )
    state = object.__getattribute__(book, "__dict__")
    if type(state) is not dict:
        raise AccountingConflict("durable provider economic book state is not canonical")
    if "resolve_historical_cut" in state:
        raise AccountingConflict(
            "durable provider economic historical-cut executable is shadowed"
        )


@dataclass(frozen=True)
class ResolvedAblationProviderEconomicCutProvenance:
    """Exact durable economic-prefix identity; explicitly not terminal cost evidence."""

    evidence_kind: str
    terminal_cost_composite: bool
    provider_id: str
    account_id: str
    environment: str
    book_id: str
    aggregate_version: int
    journal_sequence: int
    visibility_journal_sequence: int
    event_id: str
    payload_hash: str
    economic_cut_digest: str
    resulting_book_digest: str
    transaction_digests: tuple[tuple[str, str], ...]
    store_identity: JournalStoreIdentity
    provenance_digest: str

    def __post_init__(self) -> None:
        if self.evidence_kind != _EVIDENCE_KIND:
            raise AccountingConflict("provider economic provenance kind is not canonical")
        if type(self.terminal_cost_composite) is not bool or self.terminal_cost_composite:
            raise AccountingConflict(
                "provider economic prefix cannot claim terminal cost-composite authority"
            )
        for name in ("provider_id", "account_id", "environment", "book_id", "event_id"):
            _text(getattr(self, name), name=name)
        for name in (
            "aggregate_version",
            "journal_sequence",
            "visibility_journal_sequence",
        ):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise AccountingConflict(f"{name} must be an exact positive integer")
        if self.visibility_journal_sequence < self.journal_sequence:
            raise AccountingConflict(
                "visibility_journal_sequence cannot precede terminal economic event"
            )
        _digest(self.payload_hash, name="payload_hash")
        _digest(self.economic_cut_digest, name="economic_cut_digest")
        _digest(self.resulting_book_digest, name="resulting_book_digest")
        if type(self.transaction_digests) is not tuple:
            raise AccountingConflict("transaction_digests must be an exact tuple")
        transaction_ids: list[str] = []
        for item in self.transaction_digests:
            if type(item) is not tuple or len(item) != 2:
                raise AccountingConflict("transaction_digests entry is not canonical")
            transaction_id = _text(item[0], name="transaction_id")
            _digest(item[1], name="transaction_digest")
            transaction_ids.append(transaction_id)
        if len(transaction_ids) != len(set(transaction_ids)):
            raise AccountingConflict("transaction_digests contain duplicate transaction_id")
        _store_identity_material(self.store_identity)
        _digest(self.provenance_digest, name="provenance_digest")
        ResolvedAblationProviderEconomicCutProvenance.verify_integrity(self)

    def verify_integrity(self) -> None:
        material = {
            "schema_version": 1,
            "evidence_kind": self.evidence_kind,
            "terminal_cost_composite": self.terminal_cost_composite,
            "provider_id": self.provider_id,
            "account_id": self.account_id,
            "environment": self.environment,
            "book_id": self.book_id,
            "aggregate_version": self.aggregate_version,
            "journal_sequence": self.journal_sequence,
            "visibility_journal_sequence": self.visibility_journal_sequence,
            "event_id": self.event_id,
            "payload_hash": self.payload_hash,
            "economic_cut_digest": self.economic_cut_digest,
            "resulting_book_digest": self.resulting_book_digest,
            "transaction_digests": [list(item) for item in self.transaction_digests],
            "store_identity": _store_identity_material(self.store_identity),
        }
        if self.provenance_digest != _provenance_digest(material):
            raise AccountingConflict(
                "provider economic provenance digest does not match canonical material"
            )


def resolve_ablation_provider_economic_cut_provenance(
    book: DurableProviderEconomicBook,
    cut: ProviderEconomicCut,
    *,
    expected_visibility_journal_sequence: int,
) -> ResolvedAblationProviderEconomicCutProvenance:
    """Reverify one frozen economic prefix and expose only non-numeric provenance."""

    _assert_owner_executables(book)
    if type(cut) is not ProviderEconomicCut:
        raise TypeError("cut must be exact ProviderEconomicCut")
    if (
        type(expected_visibility_journal_sequence) is not int
        or expected_visibility_journal_sequence <= 0
    ):
        raise ValueError(
            "expected_visibility_journal_sequence must be an exact positive integer"
        )
    replayed = _REVERIFY_CUT(
        book,
        cut,
        expected_visibility_journal_sequence=expected_visibility_journal_sequence,
    )
    _assert_owner_executables(book)
    material = _cut_material(replayed)
    return ResolvedAblationProviderEconomicCutProvenance(
        evidence_kind=_EVIDENCE_KIND,
        terminal_cost_composite=False,
        provider_id=replayed.provider_id,
        account_id=replayed.account_id,
        environment=replayed.environment,
        book_id=replayed.book_id,
        aggregate_version=replayed.aggregate_version,
        journal_sequence=replayed.journal_sequence,
        visibility_journal_sequence=replayed.visibility_journal_sequence,
        event_id=replayed.event_id,
        payload_hash=replayed.payload_hash,
        economic_cut_digest=replayed.cut_digest,
        resulting_book_digest=replayed.resulting_book_digest,
        transaction_digests=replayed.transaction_digests,
        store_identity=replayed.store_identity,
        provenance_digest=_provenance_digest(material),
    )


def reverify_ablation_provider_economic_cut_provenance(
    book: DurableProviderEconomicBook,
    cut: ProviderEconomicCut,
    *,
    expected_visibility_journal_sequence: int,
    evidence: ResolvedAblationProviderEconomicCutProvenance,
) -> ResolvedAblationProviderEconomicCutProvenance:
    """Re-resolve canonical durable truth and require identical prefix provenance."""

    if type(evidence) is not ResolvedAblationProviderEconomicCutProvenance:
        raise TypeError(
            "evidence must be exact ResolvedAblationProviderEconomicCutProvenance"
        )
    ResolvedAblationProviderEconomicCutProvenance.verify_integrity(evidence)
    resolved = resolve_ablation_provider_economic_cut_provenance(
        book,
        cut,
        expected_visibility_journal_sequence=expected_visibility_journal_sequence,
    )
    if resolved != evidence:
        raise AccountingConflict(
            "provider economic provenance does not match canonical durable cut"
        )
    return resolved
