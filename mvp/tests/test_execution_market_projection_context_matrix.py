from decimal import ROUND_CEILING, ROUND_FLOOR, localcontext
import unittest

from mvp.autotrade_mvp.execution_realism import (
    ExecutionModel,
    ExecutionPriceProjectionPolicy,
    LiquidityObservation,
    SimulatedOrder,
    simulate_execution,
)


CALIBRATION = "a" * 64
INSTRUMENT_BINDING = "c" * 64


def _model() -> ExecutionModel:
    return ExecutionModel.create(
        model_version="exec-realism-v1",
        calibration_sha256=CALIBRATION,
        data_fidelity="TOP_OF_BOOK",
        scenario="BASE",
        latency_ms=0,
        fee_rate="0.001",
        minimum_fee="0",
        max_participation="0.5",
        slippage_bps="5",
        impact_bps_at_max_participation="10",
        bar_half_spread_bps="0",
        scenario_cost_multiplier="1",
        price_projection=ExecutionPriceProjectionPolicy(
            policy_id="ADVERSE_INSTRUMENT_TICK",
            policy_version="1",
            instrument_version="ABC@v1",
            price_quantum="0.01",
            instrument_metadata_binding=INSTRUMENT_BINDING,
        ),
    )


def _order(side: str) -> SimulatedOrder:
    return SimulatedOrder.create(
        order_id=f"context-{side.lower()}",
        instrument_version="ABC@v1",
        side=side,
        order_type="MARKET",
        quantity="10",
        submitted_at="2026-09-24T10:00:00Z",
        lot_size="1",
    )


def _observation() -> LiquidityObservation:
    return LiquidityObservation.create(
        instrument_version="ABC@v1",
        market_time="2026-09-24T10:00:00.200000Z",
        available_at="2026-09-24T10:00:00.250000Z",
        available_volume="30",
        bid="99",
        ask="101",
    )


class MarketProjectionContextMatrixTests(unittest.TestCase):
    def test_market_result_and_evidence_are_identical_across_required_context_matrix(self):
        execution_model = _model()
        observation = _observation()
        contexts = (
            (6, ROUND_FLOOR),
            (10, ROUND_CEILING),
            (28, ROUND_FLOOR),
            (80, ROUND_CEILING),
        )

        for side in ("BUY", "SELL"):
            simulated_order = _order(side)
            results = []
            for precision, rounding in contexts:
                with self.subTest(side=side, precision=precision, rounding=rounding):
                    with localcontext() as context:
                        context.prec = precision
                        context.rounding = rounding
                        results.append(
                            simulate_execution(
                                simulated_order,
                                observation,
                                execution_model,
                            )
                        )

            reference = results[0]
            for result in results[1:]:
                self.assertEqual(result, reference)
            self.assertEqual(reference.model_fingerprint, execution_model.fingerprint)
            self.assertEqual(reference.evidence_available_at, observation.available_at)
            self.assertEqual(reference.trade_time, observation.market_time)


if __name__ == "__main__":
    unittest.main()
