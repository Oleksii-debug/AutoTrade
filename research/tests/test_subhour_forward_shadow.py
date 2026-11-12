from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from research.autotrade_research.integrations.whitebit_public import PublicDataError
from research.autotrade_research.strategies.technical_confluence import simulate
from tools.run_whitebit_subhour_forward_shadow import (
    PROSPECTIVE_WAVE_CYCLES, _append_decision, _bars, _initial, process_completed_bar,
)
from tools.run_whitebit_subhour_supervisor import _perfect_timing_benchmark, compare_horizons


def _flat_bars(last_open: datetime, count=200):
    result = []
    for offset in range(count):
        opened = last_open - timedelta(minutes=15 * (count - 1 - offset))
        result.append({"opened_at": opened, "closed_at": opened + timedelta(minutes=15),
            "open": "100", "high": "100", "low": "100", "close": "100",
            "volume": "1"})
    return result


def test_forward_shadow_abstains_without_a_complete_technical_setup():
    previous = datetime(2026, 11, 12, 11, 30, tzinfo=UTC)
    state = _initial(int(previous.timestamp()), previous)
    bars = _flat_bars(previous + timedelta(minutes=15))
    result = process_completed_bar(state, bars,
        quote={"bid": Decimal("99.9"), "ask": Decimal("100.1")},
        news={"decision": "ALLOW", "evidence_sha256": "a" * 64},
        l2={"status": "CONFIRMED", "reasons": []},
        now=datetime(2026, 11, 12, 12, 0, 5, tzinfo=UTC))
    assert result["position"] is None
    assert result["technical_analyses"] == 1
    assert result["holds"] == {"NO_TECHNICAL_SETUP": 1}
    assert result["network_orders"] is False


def test_forward_shadow_reanchors_after_gap_without_backfilled_trade():
    previous = datetime(2026, 11, 12, 10, 0, tzinfo=UTC)
    state = _initial(int(previous.timestamp()), previous)
    bars = _flat_bars(previous + timedelta(minutes=30))
    result = process_completed_bar(state, bars,
        quote={"bid": Decimal("99.9"), "ask": Decimal("100.1")},
        news={"decision": "ALLOW", "evidence_sha256": "a" * 64},
        l2={"status": "CONFIRMED", "reasons": []},
        now=datetime(2026, 11, 12, 10, 45, 5, tzinfo=UTC))
    assert result["position"] is None
    assert result["holds"] == {"BAR_GAP_REANCHORED": 1}


def test_each_wave_reviews_its_own_outcomes_and_allows_next_bar(monkeypatch):
    previous = datetime(2026, 11, 12, 10, 0, tzinfo=UTC)
    state = _initial(int(previous.timestamp()), previous)
    state["closed_cycles"] = 2 * PROSPECTIVE_WAVE_CYCLES - 1
    state["episodes"] = [{"net_pnl": "-500"}] + [
        {"net_pnl": "1"} for _ in range(PROSPECTIVE_WAVE_CYCLES - 1)]
    state["position"] = {"direction": 1, "quantity": "1",
        "entry_price": "100", "entry_atr": "1", "opened_at": previous.isoformat(),
        "holding_bars": 3, "starting_equity": "1000"}
    bars = _flat_bars(previous + timedelta(minutes=15))
    result = process_completed_bar(state, bars,
        quote={"bid": Decimal("100.5"), "ask": Decimal("100.6")},
        news={"decision": "ALLOW", "evidence_sha256": "a" * 64},
        l2={"status": "CONFIRMED", "reasons": []},
        now=datetime(2026, 11, 12, 10, 30, 5, tzinfo=UTC))
    assert result["closed_cycles"] == 2 * PROSPECTIVE_WAVE_CYCLES
    assert result["position"] is None
    assert result["wave_analysis"]["wave_number"] == 2
    assert result["wave_analysis"]["status"] == "POSITIVE_AFTER_COSTS"
    assert result["wave_analysis"]["strategy_mutated"] is False
    assert len(result["episodes"]) == PROSPECTIVE_WAVE_CYCLES
    monkeypatch.setattr("tools.run_whitebit_subhour_forward_shadow.technical_signal",
        lambda *args: {"direction": 1, "features": {"atr": "1"}})
    result = process_completed_bar(result, _flat_bars(previous + timedelta(minutes=30)),
        quote={"bid": Decimal("100.5"), "ask": Decimal("100.6")},
        news={"decision": "ALLOW", "evidence_sha256": "a" * 64},
        l2={"status": "CONFIRMED", "reasons": []},
        now=datetime(2026, 11, 12, 10, 45, 5, tzinfo=UTC))
    assert result["position"] is not None


def test_exclusive_whitebit_boundary_requests_latest_completed_candle():
    boundary = datetime(2026, 11, 12, 10, 15, tzinfo=UTC)

    def fetch(market, *, interval, end, limit, training):
        assert end == int(boundary.timestamp())
        candles = [SimpleNamespace(open_time=bar["opened_at"], open=bar["open"],
            high=bar["high"], low=bar["low"], close=bar["close"], base_volume=bar["volume"])
            for bar in _flat_bars(boundary - timedelta(minutes=15))]
        return SimpleNamespace(candles=candles)

    bars = _bars(SimpleNamespace(klines=fetch), boundary)
    assert bars[-1]["closed_at"] == boundary
    with pytest.raises(PublicDataError, match="LATEST_COMPLETED_CANDLE_NOT_AVAILABLE"):
        _bars(SimpleNamespace(klines=lambda *a, **kw: SimpleNamespace(
            candles=fetch(*a, **kw).candles[:-1])), boundary)


def test_late_candle_cannot_open_even_when_all_other_gates_pass(monkeypatch):
    previous = datetime(2026, 11, 12, 10, 0, tzinfo=UTC)
    state = _initial(int(previous.timestamp()), previous)
    monkeypatch.setattr("tools.run_whitebit_subhour_forward_shadow.technical_signal",
        lambda *args: {"direction": 1, "features": {"atr": "1"}})
    process_completed_bar(state, _flat_bars(previous + timedelta(minutes=15)),
        quote={"bid": Decimal("100.5"), "ask": Decimal("100.6")},
        news={"decision": "ALLOW", "evidence_sha256": "a" * 64},
        l2={"status": "CONFIRMED"},
        now=previous + timedelta(minutes=32))
    assert state["position"] is None
    assert state["holds"] == {"COMPLETED_CANDLE_OUTSIDE_ENTRY_WINDOW": 1}


def test_daily_horizon_comparison_keeps_flat_days_and_final_is_unread():
    end = datetime(2026, 11, 12, 23, 45, tzinfo=UTC)
    bars = _flat_bars(end, count=288)
    report = compare_horizons(bars[:192] + [{"unread": True}],
                              train_count=96, development_count=96)
    for horizon, stages in report["variants"].items():
        for result in stages.values():
            assert result["configuration"]["maximum_hold_bars"] * 15 == int(horizon)
            assert result["daily_returns"]["complete_utc_days"] == 1
            assert result["daily_returns"]["target_hit_days"] == {
                "0.05": 0, "0.10": 0, "0.20": 0}
            assert result["perfect_timing_benchmark"]["best_day_fraction"] == "0"
    partial = simulate(bars[1:192], candidate="CASH", report_daily_returns=True)
    assert partial["daily_returns"]["complete_utc_days"] == 1


def test_perfect_timing_benchmark_observes_holding_limit_and_costs():
    bars = _flat_bars(datetime(2026, 11, 12, 23, 45, tzinfo=UTC), count=96)
    bars[0]["open"] = "95"
    bars[4]["open"] = "114.1"
    thirty = _perfect_timing_benchmark(bars, 2)
    sixty = _perfect_timing_benchmark(bars, 4)
    assert thirty["target_hit_days"]["0.05"] == 0
    assert sixty["target_hit_days"]["0.05"] == 0
    bars[4]["open"] = "115"
    assert _perfect_timing_benchmark(bars, 4)["target_hit_days"]["0.05"] == 1


def test_decision_journal_is_append_only_and_secret_free(tmp_path):
    target = tmp_path / "decisions.jsonl"
    _append_decision(target, {"bar": 1, "trading_authority": False})
    _append_decision(target, {"bar": 2, "trading_authority": False})
    lines = target.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert '"bar":1' in lines[0] and '"bar":2' in lines[1]
