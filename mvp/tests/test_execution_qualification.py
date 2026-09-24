from decimal import Decimal
import unittest

from mvp.autotrade_mvp.execution_qualification import (
    ExecutionModelQualification,
    ExecutionQualificationError,
    simulate_qualified_execution,
    validate_execution_qualification,
)
from mvp.autotrade_mvp.execution_realism import (
    ExecutionModel,
    LiquidityObservation,
    SimulatedOrder,
)


CALIBRATION = "a" * 64
PROTOCOL = "b" * 64
EVIDENCE = "c" * 64


def model(**overrides):
    values = dict(
        model_version="exec-realism-v1",
        calibration_sha256=CALIBRATION,
        data_fidelity="TOP_OF_BOOK",
        scenario="BASE",
        latency_ms=0,
        fee_rate="0.001",
        minimum_fee="0",
        max_participation="0.25",
        slippage_bps="5",
        impact_bps_at_max_participation="10",
        bar_half_spread_bps="0",
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


def qualification(exec_model, **overrides):
    values = dict(
        qualification_id="q-1",
        asset_class="EQUITY",
        data_fidelity=exec_model.data_fidelity,
        scenario=exec_model.scenario,
        purpose="REPLAY",
        model_fingerprint=exec_model.fingerprint,
        calibration_sha256=exec_model.calibration_sha256,
        protocol_sha256=PROTOCOL,
        evidence_sha256=EVIDENCE,
        instrument_version="ABC@v1",
    )
    values.update(overrides)
    return ExecutionModelQualification(**values)


class ExecutionQualificationTests(unittest.TestCase):
    def test_exact_qualified_model_can_execute_simulation(self):
        exec_model = model()
        result = simulate_qualified_execution(
            order=order(),
            observation=observation(),
            model=exec_model,
            qualification=qualification(exec_model),
            asset_class="EQUITY",
            protocol_sha256=PROTOCOL,
            purpose="REPLAY",
        )
        self.assertEqual(result.status, "FILLED")
        self.assertGreater(result.fill_price, Decimal("101"))

    def test_cost_assumption_change_invalidates_qualification(self):
        qualified = model(slippage_bps="5")
        changed = model(slippage_bps="6")
        with self.assertRaisesRegex(
            ExecutionQualificationError,
            "model_fingerprint",
        ):
            validate_execution_qualification(
                model=changed,
                qualification=qualification(qualified),
                asset_class="EQUITY",
                instrument_version="ABC@v1",
                protocol_sha256=PROTOCOL,
                purpose="REPLAY",
            )

    def test_calibration_change_invalidates_qualification(self):
        qualified = model(calibration_sha256="d" * 64)
        stale = qualification(
            qualified,
            calibration_sha256="a" * 64,
        )
        with self.assertRaisesRegex(
            ExecutionQualificationError,
            "calibration_sha256",
        ):
            validate_execution_qualification(
                model=qualified,
                qualification=stale,
                asset_class="EQUITY",
                instrument_version="ABC@v1",
                protocol_sha256=PROTOCOL,
                purpose="REPLAY",
            )

    def test_asset_class_is_a_qualification_dimension(self):
        exec_model = model()
        with self.assertRaisesRegex(ExecutionQualificationError, "asset_class"):
            validate_execution_qualification(
                model=exec_model,
                qualification=qualification(exec_model, asset_class="SPOT"),
                asset_class="EQUITY",
                instrument_version="ABC@v1",
                protocol_sha256=PROTOCOL,
                purpose="REPLAY",
            )

    def test_data_fidelity_is_a_qualification_dimension(self):
        qualified_model = model(data_fidelity="TOP_OF_BOOK")
        different_model = model(data_fidelity="BOOK")
        with self.assertRaisesRegex(
            ExecutionQualificationError,
            "data_fidelity",
        ):
            validate_execution_qualification(
                model=different_model,
                qualification=qualification(qualified_model),
                asset_class="EQUITY",
                instrument_version="ABC@v1",
                protocol_sha256=PROTOCOL,
                purpose="REPLAY",
            )

    def test_protocol_digest_must_match_frozen_experiment(self):
        exec_model = model()
        with self.assertRaisesRegex(
            ExecutionQualificationError,
            "protocol_sha256",
        ):
            validate_execution_qualification(
                model=exec_model,
                qualification=qualification(exec_model),
                asset_class="EQUITY",
                instrument_version="ABC@v1",
                protocol_sha256="f" * 64,
                purpose="REPLAY",
            )

    def test_instrument_specific_qualification_cannot_cross_instrument(self):
        exec_model = model()
        with self.assertRaisesRegex(
            ExecutionQualificationError,
            "instrument_version",
        ):
            validate_execution_qualification(
                model=exec_model,
                qualification=qualification(
                    exec_model,
                    instrument_version="ABC@v1",
                ),
                asset_class="EQUITY",
                instrument_version="XYZ@v2",
                protocol_sha256=PROTOCOL,
                purpose="REPLAY",
            )

    def test_optimistic_model_cannot_be_qualified_for_promotion(self):
        exec_model = model(
            scenario="OPTIMISTIC",
            scenario_cost_multiplier="0.5",
        )
        with self.assertRaisesRegex(
            ExecutionQualificationError,
            "OPTIMISTIC scenario cannot qualify promotion evidence",
        ):
            qualification(
                exec_model,
                purpose="PROMOTION",
            )

    def test_research_qualification_cannot_be_reused_for_promotion(self):
        exec_model = model()
        with self.assertRaisesRegex(ExecutionQualificationError, "purpose"):
            validate_execution_qualification(
                model=exec_model,
                qualification=qualification(exec_model, purpose="RESEARCH"),
                asset_class="EQUITY",
                instrument_version="ABC@v1",
                protocol_sha256=PROTOCOL,
                purpose="PROMOTION",
            )

    def test_unknown_asset_class_fails_closed(self):
        exec_model = model()
        with self.assertRaisesRegex(
            ExecutionQualificationError,
            "unsupported asset_class",
        ):
            qualification(exec_model, asset_class="MYSTERY")

    def test_invalid_evidence_digest_fails_closed(self):
        exec_model = model()
        with self.assertRaisesRegex(
            ExecutionQualificationError,
            "evidence_sha256 must be a SHA-256 digest",
        ):
            qualification(exec_model, evidence_sha256="not-a-digest")


if __name__ == "__main__":
    unittest.main()
