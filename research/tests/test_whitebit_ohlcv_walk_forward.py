from datetime import UTC, datetime, timedelta

from tools.run_whitebit_ohlcv_walk_forward import evaluate_windows, window_ranges


def _bars(count=8000):
    start = datetime(2025, 1, 1, tzinfo=UTC)
    return [{"opened_at": start+timedelta(hours=index),
        "closed_at": start+timedelta(hours=index+1), "open": "100",
        "high": "101", "low": "99", "close": "100", "volume": "1000"}
        for index in range(count)]


def test_ranges_are_recent_contiguous_and_nonoverlapping():
    assert window_ranges(8500, window_bars=2000, window_count=4) == [
        (500, 2500), (2500, 4500), (4500, 6500), (6500, 8500)]


def test_every_window_must_qualify_for_robustness_gate():
    calls = []
    def fake_screen(selected, **kwargs):
        index = len(calls); calls.append((selected, kwargs))
        qualified = index != 2
        return {"prospective_qualification": qualified, "final_consumed": qualified,
            "final": {"net_return_fraction": "0.01"} if qualified else None}
    result = evaluate_windows(_bars(), horizon=24, window_bars=2000,
        window_count=4, screen_fn=fake_screen)
    assert len(calls) == 4
    assert result["qualified_window_count"] == 3
    assert result["robustness_gate_passed"] is False
    assert result["windows_non_overlapping"] is True
    assert result["automatic_promotion"] is False

