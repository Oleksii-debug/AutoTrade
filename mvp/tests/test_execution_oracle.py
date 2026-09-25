from dataclasses import replace
from decimal import Decimal
import unittest

from mvp.autotrade_mvp.execution_oracle import (
    ExecutionOracleError,
    assert_conservative_execution,
)
from mvp.autotrade_mvp.execution_realism import (
    ExecutionModel,
    LiquidityObservation,
    SimulatedOrder,
    simulate_execution,
)


CALIBRATION = "a" * 64


def model(**overrides):
    values = dict(
        model_version="exec-realism-v1",
        calibration_sha256=CALIBRATION,
        data_fidelity="TOP_OF_BOOK",
        scenario="BASE",
        latency_ms=100,
        fee_rate="0.001",
        minimum_fee="0",
        max_participation="0.25",
        slippage_bps="5",
        impact_bps_at_max_participation="10",
        scenario_cost_multiplier="1",
    )
    values.update(overrides)
    return ExecutionModel.create(**values)


def order(**overrides):
    values = dict(
        order_id="sim-1",
        instrument_version="ABC@v1",
        side="BUY",
        order_type="MARKET",
        quantity="10",
        submitted_at="2026-09-24T10:00:00Z",
        lot_size="1",
    )
    values.update(overrides)
    return SimulatedOrder.create(**values)


def observation(**overrides):
    values = dict(
        instrument_version="ABC@v1",
        market_time="2026-09-24T10:00:00.200000Z",
        available_at="2026-09-24T10:00:00.250000Z",
        available_volume="100",
        bid="99",
        ask="101",
    )
    values.update(overrides)
    return LiquidityObservation.create(**values)


class ExecutionOracleTests(unittest.TestCase):
    def test_existing_conservative_market_fill_passes_independent_oracle(self):
        o = order()
        q = observation()
        m = model()
        result = simulate_execution(o, q, m)
        assert_conservative_execution(order=o, observation=q, model=m, result=result)

    def test_oracle_rejects_quantity_above_participation_capacity(self):
        o = order(quantity="20")
        q = observation(available_volume="20")
        m = model(max_participation="0.25")
        result = simulate_execution(o, q, m)
        bad = replace(result, filled_quantity=Decimal("6"))
        with self.assertRaisesRegex(ExecutionOracleError, "participation capacity"):
            assert_conservative_execution(order=o, observation=q, model=m, result=bad)

    def test_oracle_rejects_market_buy_better_than_ask(self):
        o, q, m = order(), observation(), model()
        result = simulate_execution(o, q, m)
        bad = replace(result, fill_price=Decimal("100"))
        with self.assertRaisesRegex(ExecutionOracleError, "more favorable"):
            assert_conservative_execution(order=o, observation=q, model=m, result=bad)

    def test_oracle_rejects_market_sell_better_than_bid(self):
        o = order(side="SELL")
        q = observation()
        m = model()
        result = simulate_execution(o, q, m)
        bad = replace(result, fill_price=Decimal("100"))
        with self.assertRaisesRegex(ExecutionOracleError, "more favorable"):
            assert_conservative_execution(order=o, observation=q, model=m, result=bad)

    def test_oracle_rejects_fee_below_model_minimum(self):
        o, q = order(quantity="1"), observation()
        m = model(fee_rate="0", minimum_fee="2.50")
        result = simulate_execution(o, q, m)
        bad = replace(result, fee=Decimal("2.49"))
        with self.assertRaisesRegex(ExecutionOracleError, "fee is below"):
            assert_conservative_execution(order=o, observation=q, model=m, result=bad)

    def test_oracle_rejects_filled_result_at_or_before_arrival(self):
        o = order()
        q = observation(
            market_time="2026-09-24T10:00:00.100000Z",
            available_at="2026-09-24T10:00:00.150000Z",
        )
        m = model(latency_ms=100)
        waiting = simulate_execution(o, q, m)
        forged = replace(
            waiting,
            status="FILLED",
            filled_quantity=Decimal("1"),
            fill_price=Decimal("101"),
            fee=Decimal("0.101"),
            trade_time=q.market_time,
        )
        with self.assertRaisesRegex(ExecutionOracleError, "same or earlier"):
            assert_conservative_execution(order=o, observation=q, model=m, result=forged)

    def test_oracle_rejects_zero_fill_with_economic_posting_fields(self):
        o = order(order_type="LIMIT", limit_price="90")
        q, m = observation(), model()
        result = simulate_execution(o, q, m)
        bad = replace(result, fee=Decimal("1"))
        with self.assertRaisesRegex(ExecutionOracleError, "zero fill cannot carry fee"):
            assert_conservative_execution(order=o, observation=q, model=m, result=bad)

    def test_oracle_rejects_stale_model_fingerprint(self):
        o, q = order(), observation()
        m = model(slippage_bps="5")
        result = simulate_execution(o, q, m)
        changed = model(slippage_bps="6")
        with self.assertRaisesRegex(ExecutionOracleError, "model fingerprint"):
            assert_conservative_execution(
                order=o,
                observation=q,
                model=changed,
                result=result,
            )


if __name__ == "__main__":
    unittest.main()
