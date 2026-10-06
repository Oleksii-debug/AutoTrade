"""Stable read-only binding between scientific qualification and financial truth.

This module does not calculate profitability, financial invariants, or scientific
metrics. It binds one already-qualified scientific subject to one exact,
current JournalStore/reconciliation cut so downstream scientific owners cannot
quietly mix facts from different financial snapshots.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

from .persistence import (
    JournalStore,
    journal_store_authority_scope,
    payload_digest,
    require_exact_journal_store_authority,
)
from .reconciliation_journal import (
    load_latest_reconciliation_checkpoint_for_scope,
    require_current_reconciliation_checkpoint,
)


class FinancialCutUnavailable(LookupError):
    """Raised when the requested authoritative financial cut does not exist yet."""


class FinancialCutConflict(RuntimeError):
    """Raised when the selected financial authority changes or contradicts itself."""


def _text(value: object, *, name: str) -> str:
    if type(value) is not str:
        raise TypeError(f"{name} must be exact built-in text")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{name} is required")
    return normalized


def _sha256(value: object, *, name: str) -> str:
    text = _text(value, name=name)
    if (
        len(text) != 71
        or not text.startswith("sha256:")
        or any(ch not in "0123456789abcdef" for ch in text[7:])
    ):
        raise ValueError(f"{name} must be a canonical SHA-256 digest")
    return text


def _exact_snapshot(value: object, *, name: str) -> dict[str, Any]:
    if type(value) is not dict:
        raise FinancialCutConflict(f"{name} must be an exact dictionary snapshot")
    if any(type(key) is not str for key in value):
        raise FinancialCutConflict(f"{name} keys must be exact text")
    return dict(value)


def _frozen_journal_snapshot(value: object, *, name: str) -> Mapping[str, Any]:
    state = _exact_snapshot(value, name=name)
    counts = state.get("counts")
    if type(counts) is not dict:
        raise FinancialCutConflict(f"{name}.counts must be an exact dictionary")
    if any(
        type(key) is not str or type(count) is not int or count < 0
        for key, count in counts.items()
    ):
        raise FinancialCutConflict(
            f"{name}.counts must contain exact text and non-negative integers"
        )
    state["counts"] = MappingProxyType(dict(counts))
    return MappingProxyType(state)


@dataclass(frozen=True)
class ScientificFinancialCut:
    """Immutable identity of one stable local financial/reconciliation cut.

    A stable cut is provenance needed by the financial/metrics semantic owner;
    it is not itself evidence that economics are valid or profitable.
    """

    scientific_protocol_id: str
    gate_profile_digest: str
    provider_id: str
    account_id: str
    environment: str
    reconciliation_event_id: str
    journal_sequence: int
    journal_state_digest: str
    reconciliation_checkpoint_digest: str
    cut_digest: str
    journal_state: Mapping[str, Any]

    def __post_init__(self) -> None:
        for field in (
            "scientific_protocol_id",
            "provider_id",
            "account_id",
            "environment",
            "reconciliation_event_id",
        ):
            object.__setattr__(self, field, _text(getattr(self, field), name=field))
        object.__setattr__(
            self,
            "gate_profile_digest",
            _sha256(self.gate_profile_digest, name="gate_profile_digest"),
        )
        object.__setattr__(
            self,
            "journal_state_digest",
            _sha256(self.journal_state_digest, name="journal_state_digest"),
        )
        object.__setattr__(
            self,
            "reconciliation_checkpoint_digest",
            _sha256(
                self.reconciliation_checkpoint_digest,
                name="reconciliation_checkpoint_digest",
            ),
        )
        object.__setattr__(
            self,
            "cut_digest",
            _sha256(self.cut_digest, name="cut_digest"),
        )
        if type(self.journal_sequence) is not int or self.journal_sequence < 0:
            raise ValueError("journal_sequence must be a non-negative integer")
        state = _frozen_journal_snapshot(self.journal_state, name="journal_state")
        object.__setattr__(self, "journal_state", state)


def capture_current_scientific_financial_cut(
    store: JournalStore,
    *,
    scientific_protocol_id: str,
    gate_profile_digest: str,
    provider_id: str,
    account_id: str,
    environment: str,
    reconciliation_event_id: str,
) -> ScientificFinancialCut:
    """Bind one scientific subject to one current, non-racing financial cut.

    The selected JournalStore authority is held while both the whole-store cut
    and the reconciliation checkpoint are read. A second whole-store read must
    be equivalent; otherwise the operation fails closed instead of constructing
    evidence across two financial moments.

    The reconciliation helper is the existing authority for deciding whether
    the selected checkpoint is current for the provider/account/environment.
    Missing current reconciliation is unavailable evidence; stale, mismatched,
    or corrupt reconciliation is a conflict.
    """

    protocol_id = _text(scientific_protocol_id, name="scientific_protocol_id")
    profile_digest = _sha256(gate_profile_digest, name="gate_profile_digest")
    provider = _text(provider_id, name="provider_id").upper()
    account = _text(account_id, name="account_id")
    env = _text(environment, name="environment").upper()
    checkpoint_id = _text(
        reconciliation_event_id,
        name="reconciliation_event_id",
    )

    identity = require_exact_journal_store_authority(
        store,
        subject="scientific financial JournalStore",
    )
    with journal_store_authority_scope(store, identity):
        before = _exact_snapshot(
            store.whole_store_state_cut(),
            name="journal_state_before",
        )
        try:
            latest = load_latest_reconciliation_checkpoint_for_scope(
                store,
                provider_id=provider,
                account_id=account,
                environment=env,
            )
            checkpoint = (
                None
                if latest is None
                else require_current_reconciliation_checkpoint(
                    store,
                    checkpoint_event_id=checkpoint_id,
                    provider_id=provider,
                    account_id=account,
                    environment=env,
                )
            )
        except (TypeError, ValueError) as error:
            raise FinancialCutConflict(
                "reconciliation checkpoint is not authoritative for the requested scope"
            ) from error
        after = _exact_snapshot(
            store.whole_store_state_cut(),
            name="journal_state_after",
        )

    if before != after:
        raise FinancialCutConflict(
            "financial journal changed while the scientific financial cut was captured"
        )
    if checkpoint is None:
        raise FinancialCutUnavailable(
            "current reconciliation checkpoint is unavailable"
        )
    if type(checkpoint) is not dict:
        raise FinancialCutConflict(
            "reconciliation checkpoint must be an exact dictionary"
        )
    if any(type(key) is not str for key in checkpoint):
        raise FinancialCutConflict(
            "reconciliation checkpoint keys must be exact text"
        )

    sequence = before.get("journal_sequence")
    if type(sequence) is not int or sequence < 0:
        raise FinancialCutConflict(
            "whole-store financial cut lacks a canonical journal_sequence"
        )

    journal_digest = payload_digest(before)
    reconciliation_digest = payload_digest(checkpoint)
    cut_payload = {
        "schema": "autotrade.scientific-financial-cut.v1",
        "scientific_protocol_id": protocol_id,
        "gate_profile_digest": profile_digest,
        "provider_id": provider,
        "account_id": account,
        "environment": env,
        "reconciliation_event_id": checkpoint_id,
        "journal_sequence": sequence,
        "journal_state_digest": journal_digest,
        "reconciliation_checkpoint_digest": reconciliation_digest,
    }
    return ScientificFinancialCut(
        scientific_protocol_id=protocol_id,
        gate_profile_digest=profile_digest,
        provider_id=provider,
        account_id=account,
        environment=env,
        reconciliation_event_id=checkpoint_id,
        journal_sequence=sequence,
        journal_state_digest=journal_digest,
        reconciliation_checkpoint_digest=reconciliation_digest,
        cut_digest=payload_digest(cut_payload),
        journal_state=before,
    )
