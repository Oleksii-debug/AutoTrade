from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest import TestCase
from unittest.mock import patch

from research.autotrade_research.strategies import course_quasimodo, course_range_deviation
from research.autotrade_research.strategies.technical_confluence import TechnicalCosts


class CourseHourlyExecutionTests(TestCase):
    def test_both_course_engines_use_adverse_open_through_stop(self):
        now = datetime(2026, 1, 1, tzinfo=UTC)
        costs = TechnicalCosts(fee_bps_per_fill="0", spread_bps="0",
                               slippage_bps_per_fill="0", funding_bps_per_day="0")
        for direction, gap in ((1, "95"), (-1, "105")):
            for engine in (course_quasimodo, course_range_deviation):
                with self.subTest(direction=direction, engine=engine.__name__):
                    values = [
                        ("100", "100.5", "99.5", "100"),
                        ("100", "100.5", "99.5", "100"),
                        ("100", "100.5", "99.5", "100"),
                        (gap, str(max(Decimal(gap), Decimal("100"))),
                         str(min(Decimal(gap), Decimal("100"))), gap),
                    ]
                    bars = [
                        {"opened_at": now + timedelta(minutes=i * 15),
                         "closed_at": now + timedelta(minutes=(i + 1) * 15),
                         "open": opening, "high": high, "low": low, "close": close,
                         "volume": "1"}
                        for i, (opening, high, low, close) in enumerate(values)
                    ]
                    setup = {"direction": direction, "shift_level": str(100 - direction),
                             "target": str(100 + 5 * direction), "age": 0,
                             "detected_at": bars[0]["closed_at"].isoformat()}
                    if engine is course_quasimodo:
                        setup.update(sweep_extreme=str(100 - direction), atr="1",
                                     fvg_lower="99", fvg_upper="101", top_down_verified=False)
                        # The sweep extreme and shift level must bracket the close
                        # without allowing the confirmation candle to invalidate it.
                        detector = "detect_sweep"
                        config = engine.CourseConfiguration(stop_buffer_atr="0")
                    else:
                        setup["stop"] = str(100 - direction)
                        detector = "detect_deviation"
                        config = engine.RangeDeviationConfiguration(
                            minimum_shift_confirmation_bars=1)
                    with patch.object(engine, detector,
                                      side_effect=[setup, None, None, None]):
                        result = engine.simulate(bars, costs=costs, configuration=config,
                                                 report_hourly_returns=True)
                    self.assertEqual(result["closed_cycles"], 1)
                    self.assertEqual(Decimal(result["episodes"][0]["exit_price"]), Decimal(gap))
                    self.assertEqual(Decimal(result["net_pnl"]), Decimal("-50"))
                    hourly = result["clock_window_returns"]["1"]
                    self.assertEqual(hourly["complete_windows"], 1)
                    self.assertEqual(Decimal(hourly["worst_fraction"]), Decimal("-0.05"))
