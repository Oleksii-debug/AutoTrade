from datetime import UTC, datetime, timedelta
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from research.autotrade_research.integrations.whitebit_public import PublicDataError
from research.autotrade_research.learning.observed_quote_learning import (
    propose_from_observed_outcomes, review_observed_predictions,
)
from research.autotrade_research.strategies.technical_confluence import TechnicalCosts, simulate
from tools.run_whitebit_subhour_forward_shadow import (
    PROSPECTIVE_WAVE_CYCLES, _bars, _finish_decision, _initial, _publish_decision,
    _midpoint_learning_context, _observe_additional_markets,
    _observe_execution_outcomes, _quote, _record_clock_sample, main,
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
        (8, 1), (2, 0), (1, 0)]
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
    state["balance"] = "1010"
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


def test_execution_outcomes_settle_after_restart_without_virtual_fill():
    start = datetime(2026, 11, 12, 12, tzinfo=UTC)
    state = _initial(int(start.timestamp()) - 900, start)

    def observe(boundary, bid, ask, contiguous=True, delay=3):
        now = boundary + timedelta(seconds=delay)
        quote = {"bid": Decimal(bid), "ask": Decimal(ask),
                 "received_at": now.isoformat(), "source_sha256": str(boundary.timestamp())}
        return _observe_execution_outcomes(state, boundary=boundary, quote=quote,
            now=now, contiguous=contiguous, policy_action="L2_NOT_CONFIRMED")

    assert observe(start, "100", "100.10") == []
    state = json.loads(json.dumps(state))
    assert observe(start + timedelta(minutes=15), "101", "101.10") == []
    due = start + timedelta(minutes=30)
    results = observe(due, "102", "102.10")
    assert len(results) == 1
    outcome = results[0]
    assert outcome["policy_action"] == "L2_NOT_CONFIRMED"
    assert Decimal(outcome["elapsed_seconds"]) == 1800
    assert Decimal("0.004") < Decimal(outcome["outcomes"]["LONG"]["net_equity_return"]) < Decimal("0.005")
    assert Decimal(outcome["outcomes"]["SHORT"]["net_equity_return"]) < 0
    assert (outcome["exit_bid"], outcome["exit_ask"]) == ("102", "102.10")
    for row in outcome["outcomes"].values():
        costs = [Decimal(row[field]) for field in (
            "spread_equity_cost", "slippage_equity_cost", "fee_equity_cost", "funding_equity_cost",
        )]
        assert all(cost > 0 for cost in costs)
        reconciled = Decimal(row["gross_midpoint_equity_return"]) - sum(costs)
        assert abs(reconciled - Decimal(row["net_equity_return"])) < Decimal("1e-28")
    assert observe(due, "103", "103.10") == []
    assert observe(due + timedelta(minutes=30), "103", "103.10", contiguous=False) == []
    assert state["execution_outcomes"]["settled_count"] == 1
    assert state["position"] is None
    assert state["closed_cycles"] == 0
    assert state["balance"] == "1000"


def test_shared_quote_feeds_independent_market_learning(tmp_path):
    start = datetime(2026, 11, 12, 12, tzinfo=UTC)
    db = tmp_path / "depth.sqlite3"
    con = sqlite3.connect(db)
    try:
        con.execute("CREATE TABLE archived_windows(market, window_epoch, features_json)")
        end = (int(start.timestamp()) - 60) // 300 * 300
        for market in ("BTC_PERP", "ETH_PERP"):
            for i in range(20):
                epoch = end - (20 - i) * 300
                feature = {"last_received_at": datetime.fromtimestamp(epoch + 299, UTC).isoformat(),
                           "last_bucket_epoch": epoch + 299, "generation_count": 2 if i == 7 else 1,
                           "close_mid": str(99 + Decimal(i) / 10)}
                con.execute("INSERT INTO archived_windows VALUES(?,?,?)",
                            (market, epoch, json.dumps(feature)))
        con.commit()
    finally:
        con.close()

    calls = []
    def snapshots(now):
        calls.append(now)
        return [SimpleNamespace(market=market, bid="100", ask="100.01",
                    open_interest=f"{index}000", mark_price=f"100.{index}",
                    index_price=f"99.{index}", received_at=now, digest=market)
                for index, market in enumerate(
                    ("SOL_PERP", "BTC_PERP", "ETH_PERP", "NEAR_PERP", "SUI_PERP"))]

    state = _initial(int(start.timestamp()) - 900, start)
    initial_quote = _quote(SimpleNamespace(futures_markets=lambda: snapshots(start)))
    assert len(calls) == 1
    assert set(initial_quote["learning_quotes"]) == {"BTC_PERP", "ETH_PERP"}
    assert set(initial_quote["observed_cost_quotes"]) == {"NEAR_PERP", "SUI_PERP"}
    assert set(initial_quote["point_in_time_futures"]) == {
        "SOL_PERP", "BTC_PERP", "ETH_PERP"}
    for index, market in enumerate(("SOL_PERP", "BTC_PERP", "ETH_PERP")):
        assert initial_quote["point_in_time_futures"][market] == {
            "open_interest": f"{index}000",
            "mark_price": f"100.{index}",
            "index_price": f"99.{index}",
            "received_at": start.isoformat(),
            "source_sha256": market,
        }
    assert "open_interest" not in initial_quote
    assert "mark_price" not in initial_quote
    assert "index_price" not in initial_quote
    cost_quote = initial_quote["observed_cost_quotes"]["NEAR_PERP"]
    assert cost_quote["source_sha256"] == "NEAR_PERP"
    assert Decimal("0.99") < Decimal(cost_quote["spread_bps"]) < Decimal("1")
    assert _midpoint_learning_context(db, "BTC_PERP", initial_quote["learning_quotes"]["BTC_PERP"])
    result = _observe_additional_markets(state, depth_database=db, boundary=start,
        quote=initial_quote, now=start, contiguous=True)
    assert result["BTC_PERP"]["observation"]["entry_quote_sha256"] == "BTC_PERP"
    assert result["ETH_PERP"]["observation"]["learner_proposal"]["status"] == "COLLECTING_OBSERVATIONS"
    state = json.loads(json.dumps(state))
    for minute in (15, 30):
        now = start + timedelta(minutes=minute)
        quote = _quote(SimpleNamespace(futures_markets=lambda: snapshots(now)))
        result = _observe_additional_markets(state, depth_database=db, boundary=now,
            quote=quote, now=now, contiguous=True)
    assert result["BTC_PERP"]["settled"][0]["outcome_id"].startswith("BTC_PERP:")
    assert result["ETH_PERP"]["settled"][0]["outcome_id"].startswith("ETH_PERP:")
    assert state["position"] is None
    assert state["balance"] == "1000"


def test_learner_uses_only_observed_prior_returns_and_scores_whole_hours():
    start = datetime(2026, 11, 12, 12, tzinfo=UTC)
    epoch = int(start.timestamp())
    technical = {"features": {"atr": "1", "close": "99", "band_mid": "100"}}

    def outcome(i, closed, long="0.005", short="-0.006"):
        return {"outcome_id": str(i), "entry_boundary_epoch": epoch + (i - 1) * 1800,
            "exit_boundary_epoch": epoch + i * 1800,
            "exit_quote_received_at": closed.isoformat(), "technical": technical,
            "outcomes": {"LONG": {"net_equity_return": long},
                         "SHORT": {"net_equity_return": short}}}

    history = [outcome(i, start + timedelta(minutes=30 * i)) for i in range(-7, 1)]
    # The same exit boundary can be unavailable if its quote arrived later.
    unavailable = {**outcome(0, start + timedelta(seconds=5), "-1", "1"),
                   "outcome_id": "not-known-yet"}
    proposal = propose_from_observed_outcomes(history + [unavailable],
        entry_epoch=epoch, quote_received_at=start, technical=technical)
    assert proposal["action"] == "LONG"
    assert proposal["training_outcome_count"] == 8
    assert Decimal(proposal["expected_net_equity_returns"]["LONG"]) == Decimal("0.0025")
    assert "not-known-yet" not in proposal["training_outcome_ids"]
    complete = review_observed_predictions([
        {**outcome(1, start + timedelta(minutes=30)), "learner_proposal": proposal},
        {**outcome(2, start + timedelta(hours=1)), "learner_proposal": proposal},
    ])
    assert complete["simulated_direction_choices"] == 2
    assert complete["hourly"]["complete_windows"] == 1
    assert complete["hourly"]["target_hits"] == 1
    assert Decimal(complete["hourly"]["best_fraction"]) == Decimal("0.010025")


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
    checkpoint = tmp_path / "checkpoint.json"
    pending = tmp_path / "pending.json"
    scope = {"schema": "autotrade-whitebit-subhour-forward-shadow-v1",
             "trading_authority": False, "network_orders": False}
    for bar in (1, 2):
        _publish_decision(checkpoint, target, pending, {**scope, "state": {"bar": bar}},
                          {**scope, "bar": bar})
    lines = target.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert '"bar":1' in lines[0] and '"bar":2' in lines[1]
    assert json.loads(checkpoint.read_bytes())["state"]["bar"] == 2
    assert not pending.exists()


@pytest.mark.parametrize("written", ["none", "partial", "complete"])
def test_interrupted_decision_restores_original_evidence_and_state(tmp_path, written):
    checkpoint = tmp_path / "checkpoint.json"
    journal = tmp_path / "decisions.jsonl"
    pending = tmp_path / "pending.json"
    scope = {"schema": "autotrade-whitebit-subhour-forward-shadow-v1",
             "trading_authority": False, "network_orders": False}
    event = {**scope, "bar": 2, "quote_sha256": "original-quote"}
    value = {**scope, "state": {"bar": 2, "balance": "999.81"}}
    previous = b'{"bar":1}\n'
    payload = (json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n").encode()
    suffix = {"none": b"", "partial": payload[:25], "complete": payload}[written]
    journal.write_bytes(previous + suffix)
    checkpoint.write_text('{"state":{"bar":1}}', encoding="utf-8")
    pending.write_text(json.dumps({"checkpoint": value, "event": event, "offset": len(previous)}),
                       encoding="utf-8")

    _finish_decision(checkpoint, journal, pending)

    assert journal.read_bytes() == previous + payload
    assert json.loads(checkpoint.read_bytes()) == value
    assert not pending.exists()


def test_main_records_shared_portfolio_in_existing_checkpoint(tmp_path, monkeypatch):
    boundary = datetime(2026, 11, 12, 18, tzinfo=UTC)
    now = boundary + timedelta(seconds=3)

    class FixedClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    state = _initial(int(boundary.timestamp()) - 1800, boundary - timedelta(minutes=30))
    checkpoint = tmp_path / "checkpoint.json"
    checkpoint.write_text(json.dumps({"state": state}), encoding="utf-8")
    module = "tools.run_whitebit_subhour_forward_shadow"
    journal = tmp_path / "decisions.jsonl"
    previous = b'{"bar":1}\n'
    journal.write_bytes(previous)
    monkeypatch.setattr(f"{module}.MAX_DECISION_JOURNAL_BYTES", len(previous))
    monkeypatch.setattr(f"{module}.datetime", FixedClock)
    monkeypatch.setattr(f"{module}.WhiteBitPublicClient", lambda: object())
    monkeypatch.setattr(f"{module}._bars", lambda *_: _flat_bars(boundary - timedelta(minutes=15)))
    monkeypatch.setattr(f"{module}.collect_market_context",
                        lambda: {"decision": "ALLOW", "evidence_sha256": "news"})
    calls = []

    cost_quote = {"bid": "2.5", "ask": "2.51", "spread_bps": "39.92015968",
                  "received_at": now.isoformat(), "source_sha256": "near-book"}

    def quote(_):
        assert not journal.exists()
        assert journal.with_suffix(".jsonl.1").read_bytes() == previous
        calls.append(now)
        return {"bid": Decimal("99.99"), "ask": Decimal("100.01"),
                "source_sha256": "new-market-quote", "received_at": now.isoformat(),
                "observed_cost_quotes": {"NEAR_PERP": cost_quote}}

    monkeypatch.setattr(f"{module}._quote", quote)
    assert main(["--state-dir", str(tmp_path), "--depth-database", str(tmp_path / "depth"),
                 "--stop-file", str(tmp_path / "stop"), "--once"]) == 0
    saved = json.loads(checkpoint.read_bytes())
    record = json.loads((tmp_path / "decisions.jsonl").read_bytes())
    portfolio = saved["state"]["portfolio_observer"]
    assert len(calls) == 1
    assert record["observed_cost_quotes"] == {"NEAR_PERP": cost_quote}
    assert portfolio["queued"] == record["portfolio_observer"]["queued"]
    assert portfolio["queued"]["action"] == "HOLD"
    assert portfolio["queued"]["planned_entry_epoch"] == int(boundary.timestamp()) + 900
    assert portfolio["pending"] is None
    assert saved["state"]["balance"] == "1000"


def test_portfolio_checkpoint_schema_error_does_not_mutate_shadow_state():
    from tools.run_whitebit_subhour_forward_shadow import _record_portfolio_observation
    now = datetime(2026, 11, 12, 18, tzinfo=UTC)
    state = _initial(int(now.timestamp()) - 900, now)
    state["portfolio_observer"] = "invalid"
    before = json.loads(json.dumps(state))
    assert _record_portfolio_observation(state, {"observed_at": now.isoformat()}, now=now) == {
        "error_class": "PORTFOLIO_CHECKPOINT_SCHEMA_MISMATCH"}
    assert state == before



def test_btc_eth_portfolio_migration_reuses_its_checkpoint():
    from tools.run_whitebit_subhour_forward_shadow import _record_portfolio_observation
    from tools.run_whitebit_observed_portfolio import new_state

    at = datetime(2026, 11, 12, 18, 0, 2, tzinfo=UTC)
    epoch = int(at.timestamp()) - 2
    checkpoint = {"portfolio_observer": new_state(at - timedelta(minutes=30)),
                  "btc_eth_portfolio_observer": new_state(at - timedelta(minutes=15))}
    checkpoint["btc_eth_portfolio_observer"]["settled_count"] = 7
    event = {"schema": "autotrade-whitebit-subhour-forward-shadow-v1",
             "processed_bar_open_epoch": epoch - 900, "observed_at": at.isoformat(),
             "additional_market_research": {"BTC_PERP": {"observation": {
                 "entry_boundary_epoch": epoch,
                 "quote_received_at": (at - timedelta(seconds=1)).isoformat(),
                 "entry_quote_sha256": "btc",
             }}}}
    report = _record_portfolio_observation(checkpoint, event, now=at,
                                           depth_database=Path("unused.sqlite3"))
    assert report["settled_count"] == 7
    assert report["queued"]["market"] == "BTC_PERP"
    assert checkpoint["portfolio_observer"]["last_boundary_epoch"] is None
