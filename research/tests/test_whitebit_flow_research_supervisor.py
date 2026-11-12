from datetime import UTC, datetime
import json
from pathlib import Path
from unittest.mock import patch

from tools.run_whitebit_flow_research_supervisor import execute_cycle, main


def _flow(market):
    return {
        "market": market, "status": "REJECTED",
        "prospective_qualification": False,
        "network_orders": False, "trading_authority": False,
    }


def _lead():
    return {
        "schema": "autotrade-cross-market-flow-experiment-v1",
        "pair_count": 2, "qualified_pair_count": 0,
        "waiting_pair_count": 0, "rejected_pair_count": 2,
        "results": [], "errors": [], "network_orders": False,
        "trading_authority": False, "automatic_promotion": False,
        "paid_model_calls": 0,
    }


def test_cycle_persists_digest_bound_bounded_authority_free_evidence(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    with patch(
        "tools.run_whitebit_flow_research_supervisor.screen_flow_volatility",
        side_effect=lambda _p, market, **_kw: _flow(market),
    ), patch(
        "tools.run_whitebit_flow_research_supervisor.run_lead_lag",
        return_value=_lead(),
    ):
        report = execute_cycle(
            trade_tape=tmp_path / "tape.sqlite3", state_dir=state,
            markets=["BTC_USDT", "ETH_USDT"], minimum_buckets=300,
            round_trip_cost_bps="26", maximum_runs=2,
            now=datetime(2026, 11, 12, tzinfo=UTC),
        )
    assert report["volatility"]["qualified_count"] == 0
    assert report["lead_lag"]["qualified_pair_count"] == 0
    assert report["network_requests"] == 0
    assert report["network_orders"] is False
    assert report["trading_authority"] is False
    assert report["automatic_promotion"] is False
    evidence = state / "runs" / f"{report['result_sha256']}.json"
    assert json.loads(evidence.read_text(encoding="utf-8"))["result_sha256"] == report["result_sha256"]

    # A later run keeps the requested bounded evidence count.
    with patch(
        "tools.run_whitebit_flow_research_supervisor.screen_flow_volatility",
        side_effect=lambda _p, market, **_kw: _flow(market),
    ), patch(
        "tools.run_whitebit_flow_research_supervisor.run_lead_lag",
        return_value=_lead(),
    ):
        execute_cycle(
            trade_tape=tmp_path / "tape.sqlite3", state_dir=state,
            markets=["BTC_USDT", "ETH_USDT"], minimum_buckets=300,
            round_trip_cost_bps="26", maximum_runs=1,
            now=datetime(2026, 11, 12, 1, tzinfo=UTC),
        )
    assert len(list((state / "runs").glob("*.json"))) == 1


def test_once_writes_restart_checkpoint_without_promoting(tmp_path):
    state = tmp_path / "state"
    stop = state / "managed.stop.json"
    with patch(
        "tools.run_whitebit_flow_research_supervisor.execute_cycle",
        return_value={
            "result_sha256": "a" * 64,
            "volatility": {"qualified_count": 0, "errors": []},
            "lead_lag": {"qualified_pair_count": 0, "errors": []},
        },
    ):
        assert main([
            "--trade-tape", str(tmp_path / "tape.sqlite3"),
            "--state-dir", str(state), "--market", "BTC_USDT",
            "--market", "ETH_USDT", "--stop-file", str(stop), "--once",
        ]) == 0
    checkpoint = json.loads((state / "checkpoint.json").read_text(encoding="utf-8"))
    assert checkpoint["status"] == "RUNNING"
    assert checkpoint["cycle_count"] == 1
    assert checkpoint["last_result"]["volatility_qualified_count"] == 0
    assert checkpoint["paid_model_calls"] == 0
    assert checkpoint["network_requests"] == 0
    assert checkpoint["network_orders"] is False
    assert checkpoint["trading_authority"] is False
    assert checkpoint["automatic_promotion"] is False
