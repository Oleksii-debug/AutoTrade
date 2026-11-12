import json

import pytest

from tools.run_goal_directed_attention import _flow_research_summary


def _checkpoint(last_result):
    return {
        "schema": "autotrade-whitebit-flow-research-supervisor-v1",
        "status": "RUNNING",
        "updated_at": "2026-11-12T00:00:00+00:00",
        "cycle_count": 1,
        "last_cycle_seconds": "1.5",
        "last_result": last_result,
        "paid_model_calls": 0,
        "network_requests": 0,
        "network_orders": False,
        "trading_authority": False,
        "automatic_promotion": False,
    }


def _base_result():
    return {
        "result_sha256": "a" * 64,
        "volatility_qualified_count": 0,
        "lead_lag_qualified_count": 0,
        "isolated_error_count": 0,
    }


def test_projection_exposes_medium_horizon_counts(tmp_path):
    result = _base_result()
    result.update({
        "medium_horizon_qualified_count": 1,
        "medium_horizon_waiting_count": 12,
        "medium_horizon_rejected_count": 2,
        "medium_horizon_error_count": 0,
    })
    path = tmp_path / "checkpoint.json"
    path.write_text(json.dumps(_checkpoint(result)), encoding="utf-8")

    summary = _flow_research_summary(path)

    assert summary["status"] == "RUNNING"
    assert summary["last_result"]["medium_horizon_qualified_count"] == 1
    assert summary["last_result"]["medium_horizon_waiting_count"] == 12
    assert summary["last_result"]["medium_horizon_rejected_count"] == 2
    assert summary["last_result"]["medium_horizon_error_count"] == 0
    assert summary["network_orders"] is False
    assert summary["trading_authority"] is False
    assert summary["automatic_promotion"] is False


def test_projection_fail_closes_during_legacy_checkpoint_migration(tmp_path):
    path = tmp_path / "checkpoint.json"
    path.write_text(json.dumps(_checkpoint(_base_result())), encoding="utf-8")

    summary = _flow_research_summary(path)

    assert summary["status"] == "MIGRATION_PENDING"
    assert summary["last_result"]["medium_horizon_qualified_count"] is None
    assert summary["automatic_promotion"] is False


def test_projection_rejects_partial_medium_horizon_result(tmp_path):
    result = _base_result()
    result["medium_horizon_qualified_count"] = 0
    path = tmp_path / "checkpoint.json"
    path.write_text(json.dumps(_checkpoint(result)), encoding="utf-8")

    with pytest.raises(ValueError, match="incomplete"):
        _flow_research_summary(path)
