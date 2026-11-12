import numpy as np

from tools.screen_whitebit_cross_sectional_ohlcv import simulate_panel


def _panel():
    return {"markets": ["A_USDT", "B_USDT", "C_USDT"],
        "y": np.asarray([[0.01, -0.02, 0.03]]*20),
        "decision_bar": np.arange(20), "horizon": 2}


def test_panel_selects_only_top_market_and_skips_overlap():
    panel = _panel()
    prediction = np.asarray([[0.001, 0.002, 0.01]]*20)
    result = simulate_panel(prediction, panel, np.arange(20), threshold=0.005,
        round_trip_cost_ratio=0.0026)
    assert 0 < result["trade_count"] < 20
    assert result["market_trade_counts"] == {"C_USDT": result["trade_count"]}
    assert result["winning_trade_count"] == result["trade_count"]


def test_panel_abstains_below_cost_hurdle():
    panel = _panel()
    prediction = np.asarray([[0.001, 0.002, 0.0025]]*20)
    result = simulate_panel(prediction, panel, np.arange(20), threshold=0.0026,
        round_trip_cost_ratio=0.0026)
    assert result["trade_count"] == 0
    assert result["net_return_fraction"] == "0.000000000000"

