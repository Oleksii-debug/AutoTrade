from __future__ import annotations

import pytest

from autotrade_mvp.persistence import JournalStore
from autotrade_mvp.reconciliation import SnapshotConsistencyEvidence, reconcile_account
from autotrade_mvp.reconciliation_journal import record_reconciliation_checkpoint
from autotrade_mvp.scientific_financial_cut import (
    FinancialCutConflict,
    FinancialCutUnavailable,
    capture_current_scientific_financial_cut,
)


_SHA = "sha256:" + "1" * 64


class HostileText(str):
    calls = 0

    def strip(self, *args, **kwargs):
        type(self).calls += 1
        raise AssertionError("hostile text callback executed")


def _capture(store, **overrides):
    values = {
        "scientific_protocol_id": "protocol-1",
        "gate_profile_digest": _SHA,
        "provider_id": "BYBIT",
        "account_id": "account-1",
        "environment": "TESTNET",
        "reconciliation_event_id": "missing-checkpoint",
    }
    values.update(overrides)
    return capture_current_scientific_financial_cut(store, **values)


def _record_checkpoint(
    store: JournalStore,
    *,
    reconciliation_id: str = "financial-cut-checkpoint",
    provider_id: str = "BYBIT",
    account_id: str = "account-1",
    environment: str = "PAPER",
):
    started = "2026-10-06T08:00:00Z"
    completed = "2026-10-06T08:01:00Z"
    snapshot = SnapshotConsistencyEvidence(
        provider_id=provider_id,
        account_id=account_id,
        environment=environment,
        mode="ATOMIC",
        query_started_at=started,
        query_completed_at=completed,
    )
    result = reconcile_account(
        provider_id=provider_id,
        account_id=account_id,
        environment=environment,
        local_cash={"USD": "1000"},
        provider_cash={"USD": "1000"},
        local_positions={},
        provider_positions={},
        local_execution_ids=[],
        provider_fills=[],
        snapshot_consistency=snapshot,
        coverage_start=started,
        coverage_end=completed,
        pagination_complete=True,
        provider_activity_provider_id=provider_id,
        provider_activity_account_id=account_id,
    )
    return record_reconciliation_checkpoint(
        store,
        reconciliation_id=reconciliation_id,
        result=result,
        observed_at=completed,
        host_id="financial-cut-test-host",
        owner_epoch="financial-cut-test-epoch",
    )


def test_rejects_polymorphic_text_before_financial_authority_dispatch(tmp_path):
    HostileText.calls = 0
    store = JournalStore(tmp_path / "journal.db")
    with pytest.raises(TypeError, match="exact built-in text"):
        _capture(store, provider_id=HostileText("BYBIT"))
    assert HostileText.calls == 0


def test_rejects_non_journal_store_before_constructing_cut():
    with pytest.raises(TypeError):
        _capture(object())


def test_missing_current_reconciliation_is_unavailable_not_financial_pass(tmp_path):
    store = JournalStore(tmp_path / "journal.db")
    with pytest.raises(FinancialCutUnavailable, match="unavailable"):
        _capture(store)


def test_profile_binding_requires_canonical_sha256_before_store_dispatch(tmp_path):
    store = JournalStore(tmp_path / "journal.db")
    with pytest.raises(ValueError, match="canonical SHA-256"):
        _capture(store, gate_profile_digest="not-a-digest")


def test_scope_aliases_resolve_to_one_canonical_cut_identity(tmp_path):
    store = JournalStore(tmp_path / "journal.db")
    checkpoint = _record_checkpoint(store)
    exact = _capture(
        store,
        provider_id="BYBIT",
        environment="PAPER",
        reconciliation_event_id=checkpoint["event_id"],
    )
    alias = _capture(
        store,
        provider_id="bybit",
        environment="paper",
        reconciliation_event_id=checkpoint["event_id"],
    )
    assert exact.provider_id == alias.provider_id == "BYBIT"
    assert exact.environment == alias.environment == "PAPER"
    assert exact.cut_digest == alias.cut_digest


def test_returned_journal_state_is_deeply_immutable(tmp_path):
    store = JournalStore(tmp_path / "journal.db")
    checkpoint = _record_checkpoint(store)
    cut = _capture(
        store,
        provider_id="BYBIT",
        environment="PAPER",
        reconciliation_event_id=checkpoint["event_id"],
    )
    digest = cut.cut_digest
    counts = cut.journal_state["counts"]
    with pytest.raises(TypeError):
        counts["events"] = counts["events"] + 1
    assert cut.cut_digest == digest


def test_superseded_checkpoint_is_conflict_not_unavailable(tmp_path):
    store = JournalStore(tmp_path / "journal.db")
    first = _record_checkpoint(store, reconciliation_id="cut-1")
    _record_checkpoint(store, reconciliation_id="cut-2")
    with pytest.raises(FinancialCutConflict, match="not authoritative"):
        _capture(
            store,
            provider_id="BYBIT",
            environment="PAPER",
            reconciliation_event_id=first["event_id"],
        )
