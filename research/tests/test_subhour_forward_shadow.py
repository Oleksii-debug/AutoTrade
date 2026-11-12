from datetime import UTC, datetime, timedelta
from decimal import Decimal
import json
from types import SimpleNamespace

import pytest

from research.autotrade_research.integrations.whitebit_public import PublicDataError
from research.autotrade_research.strategies.technical_confluence import TechnicalCosts, simulate
from tools.run_whitebit_subhour_forward_shadow import (
    PROSPECTIVE_WAVE_CYCLES, _append_decision, _bars, _initial, _record_clock_sample, main,
    process_completed_bar, protect_open_position,
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


def test_clock_returns_measure_elapsed_windows_without_extrapolation():
    bars = _flat_bars(datetime(2026, 11, 12, 7, 45, tzinfo=UTC), count=32)
    bars[3].update(high="102.04", close="102.04")
    for bar in bars[4:]:
        bar.update(open="102.04", close="102.04", high="102.04", low="102.04")
    costs = TechnicalCosts(capital_fraction="1", fee_bps_per_fill="0",
                          spread_bps="0", slippage_bps_per_fill="0",
                          funding_bps_per_day="0")
    hold = simulate(bars, candidate="HOLD_LONG", costs=costs, report_hourly_returns=True)
    clocks = hold["clock_window_returns"]
    assert [(clocks[h]["complete_windows"], clocks[h]["target_hits"]) for h in ("1", "4", "8")] == [
        (8, 1), (2, 1), (1, 0)]
    assert Decimal(clocks["1"]["median_fraction"]) == 0
    assert Decimal(clocks["8"]["best_fraction"]) == Decimal("0.0204")
    assert hold["one_hour_with_exposure"]["complete_windows"] == 8

    cash = simulate(bars[1:], candidate="CASH", report_hourly_returns=True)
    assert cash["clock_window_returns"]["1"]["flat_windows"] == 7
    assert cash["clock_window_returns"]["4"]["complete_windows"] == 1
    assert cash["clock_window_returns"]["8"]["complete_windows"] == 0
    assert cash["one_hour_with_exposure"]["complete_windows"] == 0


def test_shadow_clock_resumes_and_does_not_measure_through_a_data_gap():
    start = datetime(2026, 11, 12, 0, 0, tzinfo=UTC)
    state = _initial(int(start.timestamp()), start)
    quote = {"bid": Decimal("99.9"), "ask": Decimal("100.1"), "source_sha256": "book"}
    assert _record_clock_sample(state, boundary=start, quote=quote,
                               now=start, contiguous=True)["equity"] == "1000"
    state = json.loads(json.dumps(state))
    state["balance"] = "1005.10"
    hour = start + timedelta(hours=1)
    _record_clock_sample(state, boundary=hour, quote=quote, now=hour, contiguous=True)
    assert state["clock_metrics"]["windows"]["1"]["target_hits"] == 1
    assert _record_clock_sample(state, boundary=hour, quote=quote,
                               now=hour, contiguous=True) is None
    gap = hour + timedelta(minutes=30)
    assert _record_clock_sample(state, boundary=gap, quote=quote,
                               now=gap, contiguous=False) is None
    later = start + timedelta(hours=4)
    _record_clock_sample(state, boundary=later, quote=quote, now=later, contiguous=True)
    assert state["clock_metrics"]["windows"]["4"]["complete_windows"] == 0


def test_shadow_clock_marks_an_open_short_after_exit_costs():
    start = datetime(2026, 11, 12, 0, 0, tzinfo=UTC)
    state = _initial(int(start.timestamp()), start)
    state["position"] = {"quantity": "-1", "entry_price": "100",
                         "opened_at": start.isoformat()}
    hour = start + timedelta(hours=1)
    sample = _record_clock_sample(state, boundary=hour,
        quote={"bid": Decimal("98"), "ask": Decimal("99")},
        now=hour, contiguous=True)
    assert Decimal("1000") < Decimal(sample["equity"]) < Decimal("1001")
    assert state["balance"] == "1000"


@pytest.mark.parametrize(("direction", "bid", "ask"), [(1, "97", "97.1"), (-1, "102.9", "103")])
def test_shadow_protects_from_executable_quote_before_the_minimum_hold(direction, bid, ask):
    start = datetime(2026, 11, 12, 0, 0, tzinfo=UTC)
    state = _initial(int(start.timestamp()), start)
    state["position"] = {"direction": direction, "quantity": str(direction),
        "entry_price": "100", "entry_atr": "1", "starting_equity": "1000",
        "opened_at": start.isoformat()}
    now = start + timedelta(minutes=5)
    quote = {"bid": Decimal(bid), "ask": Decimal(ask), "source_sha256": "observed",
             "received_at": now.isoformat()}
    assert protect_open_position(state, quote, now) == "OBSERVED_ATR_PROTECTIVE_STOP"
    assert state["position"] is None
    assert state["last_episode"]["holding_seconds"] == 300
    assert state["last_episode"]["exit_quote_sha256"] == "observed"
    assert Decimal(state["net_pnl"]) < Decimal("-3")


def test_shadow_hold_deadline_requires_a_fresh_quote():
    start = datetime(2026, 11, 12, 0, 0, tzinfo=UTC)
    state = _initial(int(start.timestamp()), start)
    state["position"] = {"direction": 1, "quantity": "1", "entry_price": "100",
                         "entry_atr": "1", "opened_at": start.isoformat()}
    due = start + timedelta(hours=1)
    quote = {"bid": Decimal("99.9"), "ask": Decimal("100.1"),
             "received_at": start.isoformat()}
    with pytest.raises(PublicDataError, match="STALE_PROTECTIVE_QUOTE"):
        protect_open_position(state, quote, due)
    assert state["position"] is not None
    quote["received_at"] = due.isoformat()
    assert protect_open_position(state, quote, due) == "ELAPSED_HOLD_LIMIT_EXIT"
    assert state["last_episode"]["holding_seconds"] == 3600


def test_main_persists_protective_exit_without_candles_or_news(tmp_path, monkeypatch):
    started = datetime.now(UTC) - timedelta(minutes=5)
    state = _initial(int(started.timestamp()), started)
    state["position"] = {"direction": 1, "quantity": "1", "entry_price": "100",
                         "entry_atr": "1", "opened_at": started.isoformat()}
    checkpoint = tmp_path / "checkpoint.json"
    checkpoint.write_text(json.dumps({"state": state}), encoding="utf-8")
    module = "tools.run_whitebit_subhour_forward_shadow"
    monkeypatch.setattr(f"{module}._quote", lambda _: {
        "bid": Decimal("97"), "ask": Decimal("97.1"), "source_sha256": "book",
        "received_at": datetime.now(UTC).isoformat(),
    })
    monkeypatch.setattr(f"{module}._bars", lambda *args: pytest.fail("protective exit must run first"))
    monkeypatch.setattr(f"{module}.collect_market_context",
                        lambda *args: pytest.fail("protective exit does not need news"))
    assert main(["--state-dir", str(tmp_path), "--depth-database", str(tmp_path / "depth"),
                 "--stop-file", str(tmp_path / "stop"), "--once"]) == 0
    saved = json.loads(checkpoint.read_bytes())
    record = json.loads((tmp_path / "decisions.jsonl").read_bytes())
    assert saved["state"]["position"] is None
    assert record["episode"] == saved["state"]["last_episode"]
    assert record["event"] == "PROTECTIVE_EXIT"


def test_decision_journal_is_append_only_and_secret_free(tmp_path):
    target = tmp_path / "decisions.jsonl"
    _append_decision(target, {"bar": 1, "trading_authority": False})
    _append_decision(target, {"bar": 2, "trading_authority": False})
    lines = target.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert '"bar":1' in lines[0] and '"bar":2' in lines[1]
