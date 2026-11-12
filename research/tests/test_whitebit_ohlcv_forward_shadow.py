from datetime import UTC, datetime, timedelta
from decimal import Decimal

import json

from tools.run_whitebit_ohlcv_forward_shadow import apply_decision, initial_state, main


NOW = datetime(2026, 11, 12, 3, tzinfo=UTC)


def test_shadow_enters_once_and_exits_after_horizon_with_costs():
    state = initial_state(market="SOL_USDT", model_sha256="a"*64,
        threshold="0.005", horizon=12, initial_cash="1000")
    entered = apply_decision(state, prediction=Decimal("0.006"),
        decision_closed_at=NOW, best_bid=Decimal("99.9"), best_ask=Decimal("100"),
        fee_bps=Decimal("10"), slippage_bps=Decimal("1"))
    assert entered["last_action"] == "ENTER_LONG"
    assert Decimal(entered["asset_quantity"]) > 0
    duplicate = apply_decision(entered, prediction=Decimal("0.9"),
        decision_closed_at=NOW, best_bid=Decimal("110"), best_ask=Decimal("111"),
        fee_bps=Decimal("10"), slippage_bps=Decimal("1"))
    assert duplicate == entered
    held = apply_decision(entered, prediction=Decimal("0"),
        decision_closed_at=NOW+timedelta(hours=11), best_bid=Decimal("105"),
        best_ask=Decimal("106"), fee_bps=Decimal("10"), slippage_bps=Decimal("1"))
    assert held["last_action"] == "HOLD"
    exited = apply_decision(held, prediction=Decimal("0"),
        decision_closed_at=NOW+timedelta(hours=12), best_bid=Decimal("105"),
        best_ask=Decimal("106"), fee_bps=Decimal("10"), slippage_bps=Decimal("1"))
    assert exited["last_action"] == "EXIT_LONG"
    assert exited["completed_cycles"] == 1
    assert exited["winning_cycles"] == 1
    assert Decimal(exited["realized_net_pnl_quote"]) > 0


def test_below_threshold_remains_cash():
    state = initial_state(market="SOL_USDT", model_sha256="b"*64,
        threshold="0.005", horizon=24, initial_cash="1000")
    result = apply_decision(state, prediction=Decimal("0.0049"),
        decision_closed_at=NOW, best_bid=Decimal("100"), best_ask=Decimal("100.1"),
        fee_bps=Decimal("10"), slippage_bps=Decimal("1"))
    assert result["last_action"] == "HOLD"
    assert result["cash_quote"] == "1000"
    assert result["network_orders"] is False


def test_missing_model_can_wait_without_restart_loop(tmp_path, capsys):
    result = main(["--model-artifact", str(tmp_path/"missing-model.json"),
        "--model-sha256", "c"*64, "--threshold", "0.005",
        "--horizon", "24", "--state-dir", str(tmp_path/"state"),
        "--poll-seconds", "10", "--wait-for-artifacts", "--once"])
    assert result == 0
    state = json.loads((tmp_path/"state"/"checkpoint.json").read_text(encoding="utf-8"))
    assert state["status"] == "WAITING_FOR_QUALIFIED_ARTIFACTS"
    assert state["network_orders"] is False
    assert json.loads(capsys.readouterr().out)["status"] == "WAITING_FOR_QUALIFIED_ARTIFACTS"


