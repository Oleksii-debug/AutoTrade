from datetime import UTC, datetime, timedelta
from decimal import Decimal

from tools.run_whitebit_subhour_forward_shadow import (
    _initial, process_completed_bar,
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
