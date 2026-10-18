"""Deterministic Plan10 execution scenarios; synthetic OHLCV is not fill evidence."""
from datetime import datetime, timezone
from decimal import Decimal
import unittest

from mvp.autotrade_mvp.execution_realism import (
    ExecutionModel,
    ExecutionRealismError,
    SimulatedOrder,
)
from mvp.autotrade_mvp.instruments import InstrumentVersion
from tools.historical_execution_scenarios import (
    conservative_bar_model,
    evaluate_completed_next_bar,
)


CALIBRATION = "a" * 64
INSTRUMENT_ID = "11111111-1111-4111-8111-111111111111"
INSTRUMENT = f"{INSTRUMENT_ID}@1"


def instrument():
    return InstrumentVersion(
        instrument_id=INSTRUMENT_ID,
        version=1,
        provider_id="internal-simulation",
        venue_id="historical-research",
        provider_symbol="BTCUSDT",
        asset_class="CRYPTO_SPOT",
        base_currency="BTC",
        quote_currency="USD",
        settlement_currency="USD",
        quantity_unit="BTC",
        contract_multiplier="1",
        price_tick="0.01",
        quantity_step="1",
        minimum_quantity="1",
        calendar_id="CONTINUOUS_24_7",
        timezone_id="UTC",
        effective_from=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )


def model(**overrides):
    values = dict(
        model_version="plan10-historical-adverse-v1",
        calibration_sha256=CALIBRATION,
        instrument=instrument(),
        fee_rate="0.001",
        maximum_participation="0.25",
        slippage_bps="10",
        half_spread_bps="5",
        latency_ms=0,
    )
    values.update(overrides)
    return conservative_bar_model(**values)


def order(**overrides):
    values = dict(
        order_id="historical-order-1",
        instrument_version=INSTRUMENT,
        side="BUY",
        order_type="MARKET",
        quantity="10",
        submitted_at="2024-01-01T01:00:00Z",
        lot_size="1",
    )
    values.update(overrides)
    return SimulatedOrder.create(**values)


def evaluate(simulated_order=None, execution_model=None, **overrides):
    values = dict(
        as_of="2024-01-01T02:00:01Z",
        interval_start="2024-01-01T01:00:00Z",
        interval_end="2024-01-01T02:00:00Z",
        available_at="2024-01-01T02:00:01Z",
        open_price="100",
        high_price="102",
        low_price="99",
        close_price="101",
        base_volume="100",
    )
    values.update(overrides)
    return evaluate_completed_next_bar(
        simulated_order or order(), execution_model or model(), **values
    )


class HistoricalExecutionScenariosTests(unittest.TestCase):
    def test_next_bar_buy_applies_adverse_spread_slippage_and_exact_fee(self):
        result = evaluate()

        self.assertEqual(result.status, "FILLED")
        self.assertEqual(result.filled_quantity, Decimal("10"))
        # BUY uses adverse bar high 102 plus 5 bps half-spread and 10 bps
        # slippage, rounded adversely to the canonical 0.01 price tick.
        self.assertEqual(result.fill_price, Decimal("102.16"))
        self.assertEqual(result.fee, Decimal("1.02160"))
        self.assertEqual(result.trade_time, "2024-01-01T02:00:00Z")
        self.assertIn("intrabar queue", " ".join(result.warnings))

    def test_sell_costs_are_adverse_and_distinct_from_close_mark(self):
        result = evaluate(order(side="SELL"))

        self.assertEqual(result.status, "FILLED")
        self.assertEqual(result.fill_price, Decimal("98.85"))
        self.assertLess(result.fill_price, Decimal("101"))
        self.assertEqual(result.fee, Decimal("0.98850"))

    def test_volume_participation_produces_deterministic_partial_fill(self):
        first = evaluate(base_volume="20")
        second = evaluate(base_volume="20")

        self.assertEqual(first, second)
        self.assertEqual(first.status, "PARTIAL")
        self.assertEqual(first.filled_quantity, Decimal("5"))
        self.assertEqual(first.fill_price, Decimal("102.16"))
        self.assertEqual(first.fee, Decimal("0.51080"))

    def test_insufficient_liquidity_declines_fill_without_money_mutation(self):
        result = evaluate(base_volume="3")

        self.assertEqual(result.status, "NO_FILL")
        self.assertEqual(result.filled_quantity, Decimal("0"))
        self.assertIsNone(result.fill_price)
        self.assertEqual(result.fee, Decimal("0"))
        self.assertIn("below one lot", result.reason)

    def test_latency_declines_ambiguous_bar_instead_of_using_prearrival_volume(self):
        result = evaluate(execution_model=model(latency_ms=1))

        self.assertEqual(result.status, "AMBIGUOUS_NO_FILL")
        self.assertEqual(result.filled_quantity, Decimal("0"))
        self.assertIsNone(result.fill_price)
        self.assertEqual(result.fee, Decimal("0"))
        self.assertIn("before venue arrival", result.reason)

    def test_future_or_non_next_bar_fails_closed_before_execution(self):
        cases = (
            ({"as_of": "2024-01-01T02:00:00Z"}, "future historical bar"),
            ({"as_of": "2024-01-01T02:00:02Z"}, "stale historical execution bar"),
            ({"interval_start": "2024-01-01T02:00:00Z",
              "interval_end": "2024-01-01T03:00:00Z",
              "available_at": "2024-01-01T03:00:01Z",
              "as_of": "2024-01-01T03:00:01Z"}, "immediately follow"),
            ({"high_price": "100", "close_price": "101"}, "OHLC ordering"),
        )
        for changes, message in cases:
            with self.subTest(changes=changes):
                with self.assertRaisesRegex(ExecutionRealismError, message):
                    evaluate(**changes)

    def test_non_adverse_or_non_bar_model_cannot_enter_historical_path(self):
        base = model()
        for changes in (
            {"scenario": "BASE"},
            {"data_fidelity": "TOP_OF_BOOK"},
        ):
            values = dict(base.__dict__)
            values.update(changes)
            unqualified = ExecutionModel(**values)
            with self.subTest(changes=changes):
                with self.assertRaisesRegex(ExecutionRealismError, "adverse BAR"):
                    evaluate(execution_model=unqualified)

    def test_binary_float_and_negative_volume_fail_closed(self):
        for volume, error in ((1.0, TypeError), ("-1", ExecutionRealismError)):
            with self.subTest(volume=volume):
                with self.assertRaises(error):
                    evaluate(base_volume=volume)


if __name__ == "__main__":
    unittest.main()
