from dataclasses import replace
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import NAMESPACE_URL, uuid5

from autotrade_research.artifacts.store import ArtifactStore
from mvp.autotrade_mvp.execution_oracle import ExecutionOracleError
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
EVIDENCE_BYTES = b"frozen execution qualification evidence v1"
EVIDENCE = sha256(EVIDENCE_BYTES).hexdigest()
ARTIFACT_ID = str(uuid5(NAMESPACE_URL, "autotrade:wp13:execution-evidence"))
DATA_QUALITY_BYTES = b"provider-specific execution data quality profile v1"
DATA_QUALITY = sha256(DATA_QUALITY_BYTES).hexdigest()
DATA_QUALITY_ARTIFACT_ID = str(
    uuid5(NAMESPACE_URL, "autotrade:wp13:data-quality-profile")
)


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
        evidence_artifact_id=ARTIFACT_ID,
        evidence_sha256=EVIDENCE,
        provider_id="SIMULATED",
        provider_environment="PAPER",
        data_quality_artifact_id=DATA_QUALITY_ARTIFACT_ID,
        data_quality_sha256=DATA_QUALITY,
        instrument_version="ABC@v1",
    )
    values.update(overrides)
    return ExecutionModelQualification(**values)


class ExecutionQualificationTests(unittest.TestCase):
    def setUp(self):
        self._temp = TemporaryDirectory()
        self.store = ArtifactStore(Path(self._temp.name) / "artifacts")
        self.store.publish_bytes(
            artifact_id=ARTIFACT_ID,
            data=EVIDENCE_BYTES,
            media_type="application/json",
            rights={"storage": True, "export": False},
            source_refs=["protocol:wp13"],
            metadata={
                "kind": "execution-qualification-evidence",
                "provider_id": "SIMULATED",
                "provider_environment": "PAPER",
                "data_quality_artifact_id": DATA_QUALITY_ARTIFACT_ID,
                "data_quality_sha256": DATA_QUALITY,
            },
        )
        self.store.publish_bytes(
            artifact_id=DATA_QUALITY_ARTIFACT_ID,
            data=DATA_QUALITY_BYTES,
            media_type="application/json",
            rights={"storage": True, "export": False},
            source_refs=["protocol:wp13:data-quality"],
            metadata={
                "kind": "execution-data-quality-profile",
                "provider_id": "SIMULATED",
                "provider_environment": "PAPER",
            },
        )

    def tearDown(self):
        self._temp.cleanup()

    def validation_kwargs(self, exec_model, **overrides):
        values = dict(
            model=exec_model,
            qualification=qualification(exec_model),
            asset_class="EQUITY",
            instrument_version="ABC@v1",
            protocol_sha256=PROTOCOL,
            artifact_store=self.store,
            evidence_artifact_id=ARTIFACT_ID,
            provider_id="SIMULATED",
            provider_environment="PAPER",
            data_quality_artifact_id=DATA_QUALITY_ARTIFACT_ID,
            data_quality_sha256=DATA_QUALITY,
            purpose="REPLAY",
        )
        values.update(overrides)
        return values

    def test_exact_qualified_model_can_execute_simulation(self):
        exec_model = model()
        result = simulate_qualified_execution(
            order=order(),
            observation=observation(),
            model=exec_model,
            qualification=qualification(exec_model),
            asset_class="EQUITY",
            protocol_sha256=PROTOCOL,
            artifact_store=self.store,
            evidence_artifact_id=ARTIFACT_ID,
            provider_id="SIMULATED",
            provider_environment="PAPER",
            data_quality_artifact_id=DATA_QUALITY_ARTIFACT_ID,
            data_quality_sha256=DATA_QUALITY,
            purpose="REPLAY",
        )
        self.assertEqual(result.status, "FILLED")
        self.assertGreater(result.fill_price, Decimal("101"))

    def test_cost_assumption_change_invalidates_qualification(self):
        qualified = model(slippage_bps="5")
        changed = model(slippage_bps="6")
        with self.assertRaisesRegex(ExecutionQualificationError, "model_fingerprint"):
            validate_execution_qualification(
                **self.validation_kwargs(
                    changed,
                    qualification=qualification(qualified),
                )
            )

    def test_calibration_change_invalidates_qualification(self):
        exec_model = model(calibration_sha256="d" * 64)
        stale = qualification(exec_model, calibration_sha256="a" * 64)
        with self.assertRaisesRegex(
            ExecutionQualificationError,
            "calibration_sha256",
        ):
            validate_execution_qualification(
                **self.validation_kwargs(exec_model, qualification=stale)
            )

    def test_asset_class_is_a_qualification_dimension(self):
        exec_model = model()
        with self.assertRaisesRegex(ExecutionQualificationError, "asset_class"):
            validate_execution_qualification(
                **self.validation_kwargs(
                    exec_model,
                    qualification=qualification(exec_model, asset_class="SPOT"),
                )
            )

    def test_provider_identity_is_a_qualification_dimension(self):
        exec_model = model()
        with self.assertRaisesRegex(ExecutionQualificationError, "provider_id"):
            validate_execution_qualification(
                **self.validation_kwargs(
                    exec_model,
                    provider_id="OTHER_PROVIDER",
                )
            )

    def test_verified_artifacts_cannot_be_relabelled_to_another_provider_scope(self):
        exec_model = model()
        relabelled = qualification(
            exec_model,
            provider_id="BYBIT",
            provider_environment="TESTNET",
        )
        with self.assertRaisesRegex(
            ExecutionQualificationError,
            "execution_evidence_provider_scope",
        ):
            validate_execution_qualification(
                **self.validation_kwargs(
                    exec_model,
                    qualification=relabelled,
                    provider_id="BYBIT",
                    provider_environment="TESTNET",
                )
            )

    def test_provider_environment_preserves_provider_specific_identity(self):
        exec_model = model()
        bybit_evidence_id = str(
            uuid5(NAMESPACE_URL, "autotrade:wp13:bybit-testnet:evidence")
        )
        bybit_quality_id = str(
            uuid5(NAMESPACE_URL, "autotrade:wp13:bybit-testnet:data-quality")
        )
        bybit_testnet = qualification(
            exec_model,
            evidence_artifact_id=bybit_evidence_id,
            provider_id="BYBIT",
            provider_environment="testnet",
            data_quality_artifact_id=bybit_quality_id,
        )
        self.store.publish_bytes(
            artifact_id=bybit_evidence_id,
            data=EVIDENCE_BYTES,
            media_type="application/json",
            rights={"storage": True, "export": False},
            source_refs=["protocol:wp13"],
            metadata={
                "kind": "execution-qualification-evidence",
                "provider_id": "BYBIT",
                "provider_environment": "TESTNET",
                "data_quality_artifact_id": bybit_quality_id,
                "data_quality_sha256": DATA_QUALITY,
            },
        )
        self.store.publish_bytes(
            artifact_id=bybit_quality_id,
            data=DATA_QUALITY_BYTES,
            media_type="application/json",
            rights={"storage": True, "export": False},
            source_refs=["protocol:wp13:data-quality"],
            metadata={
                "kind": "execution-data-quality-profile",
                "provider_id": "BYBIT",
                "provider_environment": "TESTNET",
            },
        )
        self.assertEqual(bybit_testnet.provider_environment, "TESTNET")
        validate_execution_qualification(
            **self.validation_kwargs(
                exec_model,
                qualification=bybit_testnet,
                evidence_artifact_id=bybit_evidence_id,
                provider_id="BYBIT",
                provider_environment="TESTNET",
                data_quality_artifact_id=bybit_quality_id,
            )
        )
        with self.assertRaisesRegex(
            ExecutionQualificationError,
            "provider_environment",
        ):
            validate_execution_qualification(
                **self.validation_kwargs(
                    exec_model,
                    qualification=bybit_testnet,
                    evidence_artifact_id=bybit_evidence_id,
                    provider_id="BYBIT",
                    provider_environment="DEMO",
                    data_quality_artifact_id=bybit_quality_id,
                )
            )

    def test_provider_environment_is_a_qualification_dimension(self):
        exec_model = model()
        with self.assertRaisesRegex(
            ExecutionQualificationError,
            "provider_environment",
        ):
            validate_execution_qualification(
                **self.validation_kwargs(
                    exec_model,
                    provider_environment="LIVE",
                )
            )

    def test_data_quality_profile_is_a_qualification_dimension(self):
        exec_model = model()
        with self.assertRaisesRegex(
            ExecutionQualificationError,
            "data_quality_sha256",
        ):
            validate_execution_qualification(
                **self.validation_kwargs(
                    exec_model,
                    data_quality_sha256="c" * 64,
                )
            )

    def test_data_quality_artifact_identity_is_a_qualification_dimension(self):
        exec_model = model()
        alias_id = str(uuid5(NAMESPACE_URL, "autotrade:wp13:data-quality-alias"))
        self.store.publish_bytes(
            artifact_id=alias_id,
            data=DATA_QUALITY_BYTES,
            media_type="application/json",
            rights={"storage": True, "export": False},
        )
        with self.assertRaisesRegex(
            ExecutionQualificationError,
            "data_quality_artifact_id",
        ):
            validate_execution_qualification(
                **self.validation_kwargs(
                    exec_model,
                    data_quality_artifact_id=alias_id,
                )
            )

    def test_data_quality_digest_must_match_resolved_profile_bytes(self):
        exec_model = model()
        with TemporaryDirectory() as directory:
            tampered_store = ArtifactStore(Path(directory) / "artifacts")
            tampered_store.publish_bytes(
                artifact_id=ARTIFACT_ID,
                data=EVIDENCE_BYTES,
                media_type="application/json",
                rights={"storage": True, "export": False},
            )
            tampered_store.publish_bytes(
                artifact_id=DATA_QUALITY_ARTIFACT_ID,
                data=b"different execution data quality profile",
                media_type="application/json",
                rights={"storage": True, "export": False},
            )
            with self.assertRaisesRegex(
                ExecutionQualificationError,
                "data_quality_sha256",
            ):
                validate_execution_qualification(
                    **self.validation_kwargs(
                        exec_model,
                        artifact_store=tampered_store,
                    )
                )

    def test_missing_data_quality_profile_artifact_fails_closed(self):
        exec_model = model()
        with TemporaryDirectory() as directory:
            incomplete_store = ArtifactStore(Path(directory) / "artifacts")
            incomplete_store.publish_bytes(
                artifact_id=ARTIFACT_ID,
                data=EVIDENCE_BYTES,
                media_type="application/json",
                rights={"storage": True, "export": False},
            )
            with self.assertRaisesRegex(
                ExecutionQualificationError,
                "data quality profile artifact cannot be verified",
            ):
                validate_execution_qualification(
                    **self.validation_kwargs(
                        exec_model,
                        artifact_store=incomplete_store,
                    )
                )

    def test_data_fidelity_is_a_qualification_dimension(self):
        qualified_model = model(data_fidelity="TOP_OF_BOOK")
        different_model = model(data_fidelity="BOOK")
        with self.assertRaisesRegex(
            ExecutionQualificationError,
            "data_fidelity",
        ):
            validate_execution_qualification(
                **self.validation_kwargs(
                    different_model,
                    qualification=qualification(qualified_model),
                )
            )

    def test_protocol_digest_must_match_frozen_experiment(self):
        exec_model = model()
        with self.assertRaisesRegex(
            ExecutionQualificationError,
            "protocol_sha256",
        ):
            validate_execution_qualification(
                **self.validation_kwargs(
                    exec_model,
                    protocol_sha256="f" * 64,
                )
            )

    def test_expected_digest_must_match_resolved_immutable_artifact(self):
        exec_model = model()
        stale = qualification(exec_model, evidence_sha256="d" * 64)
        with self.assertRaisesRegex(
            ExecutionQualificationError,
            "evidence_sha256",
        ):
            validate_execution_qualification(
                **self.validation_kwargs(exec_model, qualification=stale)
            )

    def test_different_resolved_bytes_fail_even_when_frozen_digest_is_unchanged(self):
        exec_model = model()
        with TemporaryDirectory() as directory:
            tampered_store = ArtifactStore(Path(directory) / "artifacts")
            tampered_store.publish_bytes(
                artifact_id=ARTIFACT_ID,
                data=b"different immutable evidence bytes",
                media_type="application/json",
                rights={"storage": True, "export": False},
            )
            with self.assertRaisesRegex(
                ExecutionQualificationError,
                "evidence_sha256",
            ):
                validate_execution_qualification(
                    **self.validation_kwargs(
                        exec_model,
                        artifact_store=tampered_store,
                    )
                )

    def test_missing_evidence_artifact_fails_closed(self):
        exec_model = model()
        with TemporaryDirectory() as directory:
            empty_store = ArtifactStore(Path(directory) / "artifacts")
            with self.assertRaisesRegex(
                ExecutionQualificationError,
                "cannot be verified",
            ):
                validate_execution_qualification(
                    **self.validation_kwargs(
                        exec_model,
                        artifact_store=empty_store,
                    )
                )

    def test_artifact_id_alias_cannot_rebind_same_bytes(self):
        exec_model = model()
        alias_id = str(uuid5(NAMESPACE_URL, "autotrade:wp13:alias"))
        self.store.publish_bytes(
            artifact_id=alias_id,
            data=EVIDENCE_BYTES,
            media_type="application/json",
            rights={"storage": True, "export": False},
        )
        with self.assertRaisesRegex(
            ExecutionQualificationError,
            "evidence_artifact_id",
        ):
            validate_execution_qualification(
                **self.validation_kwargs(
                    exec_model,
                    evidence_artifact_id=alias_id,
                )
            )

    def test_instrument_specific_qualification_cannot_cross_instrument(self):
        exec_model = model()
        with self.assertRaisesRegex(
            ExecutionQualificationError,
            "instrument_version",
        ):
            validate_execution_qualification(
                **self.validation_kwargs(
                    exec_model,
                    qualification=qualification(
                        exec_model,
                        instrument_version="ABC@v1",
                    ),
                    instrument_version="XYZ@v2",
                )
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
            qualification(exec_model, purpose="PROMOTION")

    def test_research_qualification_cannot_be_reused_for_promotion(self):
        exec_model = model()
        with self.assertRaisesRegex(ExecutionQualificationError, "purpose"):
            validate_execution_qualification(
                **self.validation_kwargs(
                    exec_model,
                    qualification=qualification(exec_model, purpose="RESEARCH"),
                    purpose="PROMOTION",
                )
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

    def test_invalid_evidence_artifact_id_fails_closed(self):
        exec_model = model()
        with self.assertRaisesRegex(
            ExecutionQualificationError,
            "evidence_artifact_id must be a UUID",
        ):
            qualification(exec_model, evidence_artifact_id="not-a-uuid")

    def test_qualified_path_requires_independent_oracle(self):
        exec_model = model()
        valid = simulate_qualified_execution(
            order=order(),
            observation=observation(),
            model=exec_model,
            qualification=qualification(exec_model),
            asset_class="EQUITY",
            protocol_sha256=PROTOCOL,
            artifact_store=self.store,
            evidence_artifact_id=ARTIFACT_ID,
            provider_id="SIMULATED",
            provider_environment="PAPER",
            data_quality_artifact_id=DATA_QUALITY_ARTIFACT_ID,
            data_quality_sha256=DATA_QUALITY,
            purpose="REPLAY",
        )
        forged = replace(valid, fill_price=Decimal("100"))
        with patch(
            "mvp.autotrade_mvp.execution_qualification.simulate_execution",
            return_value=forged,
        ):
            with self.assertRaisesRegex(ExecutionOracleError, "more favorable"):
                simulate_qualified_execution(
                    order=order(),
                    observation=observation(),
                    model=exec_model,
                    qualification=qualification(exec_model),
                    asset_class="EQUITY",
                    protocol_sha256=PROTOCOL,
                    artifact_store=self.store,
                    evidence_artifact_id=ARTIFACT_ID,
                    provider_id="SIMULATED",
                    provider_environment="PAPER",
                    data_quality_artifact_id=DATA_QUALITY_ARTIFACT_ID,
                    data_quality_sha256=DATA_QUALITY,
                    purpose="REPLAY",
                )


if __name__ == "__main__":
    unittest.main()
