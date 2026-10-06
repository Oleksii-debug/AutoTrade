from __future__ import annotations

import pytest

from autotrade_mvp.persistence import JournalStore
from autotrade_mvp.scientific_financial_cut import (
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
