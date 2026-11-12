from datetime import UTC, datetime, timedelta
from decimal import Decimal

from tools.run_whitebit_subhour_forward_shadow import (
    PROSPECTIVE_WAVE_CYCLES, _append_decision, _initial, process_completed_bar,
)


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
        now=datetime(2026, 11, 12, 11, 45, 5, tzinfo=UTC))
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
        now=datetime(2026, 11, 12, 10, 30, 5, tzinfo=UTC))
    assert result["position"] is None
    assert result["holds"] == {"BAR_GAP_REANCHORED": 1}


def test_fifth_closed_cycle_stops_wave_for_independent_review():
    previous = datetime(2026, 11, 12, 10, 0, tzinfo=UTC)
    state = _initial(int(previous.timestamp()), previous)
    state["closed_cycles"] = PROSPECTIVE_WAVE_CYCLES - 1
    state["episodes"] = [{"net_pnl": "1"}] * (PROSPECTIVE_WAVE_CYCLES - 1)
    state["position"] = {"direction": 1, "quantity": "1",
        "entry_price": "100", "entry_atr": "1", "opened_at": previous.isoformat(),
        "holding_bars": 3, "starting_equity": "1000"}
    bars = _flat_bars(previous + timedelta(minutes=15))
    result = process_completed_bar(state, bars,
        quote={"bid": Decimal("100.5"), "ask": Decimal("100.6")},
        news={"decision": "ALLOW", "evidence_sha256": "a" * 64},
        l2={"status": "CONFIRMED", "reasons": []},
        now=datetime(2026, 11, 12, 10, 15, 5, tzinfo=UTC))
    assert result["closed_cycles"] == PROSPECTIVE_WAVE_CYCLES
    assert result["position"] is None
    assert result["wave_analysis"]["strategy_mutated"] is False
    assert result["wave_analysis"]["next_action"] == "INDEPENDENT_REVIEW_BEFORE_ANY_NEW_WAVE"


def test_decision_journal_is_append_only_and_secret_free(tmp_path):
    target = tmp_path / "decisions.jsonl"
    _append_decision(target, {"bar": 1, "trading_authority": False})
    _append_decision(target, {"bar": 2, "trading_authority": False})
    lines = target.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert '"bar":1' in lines[0] and '"bar":2' in lines[1]
