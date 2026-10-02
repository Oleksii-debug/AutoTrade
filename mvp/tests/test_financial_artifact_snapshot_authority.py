from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import NAMESPACE_URL, uuid5

from autotrade_runtime.artifacts.store import ArtifactIntegrityError, ArtifactStore
from mvp.autotrade_mvp.execution_qualification import (
    ExecutionModelQualification,
    ExecutionQualificationError,
    validate_execution_qualification,
)
from mvp.autotrade_mvp.execution_realism import ExecutionModel
from mvp.autotrade_mvp.perpetual_margin import (
    PerpetualMarginError,
    _verify_immutable_artifact,
)


class FinancialArtifactSnapshotAuthorityTests(unittest.TestCase):
    def test_perpetual_margin_rejects_store_subclass_before_virtual_dispatch(self):
        class ForgedStore(ArtifactStore):
            snapshot_called = False

            def read_authenticated_snapshot(self, artifact_id):
                self.snapshot_called = True
                raise AssertionError("subclass method must not run")

        with TemporaryDirectory() as directory:
            forged = ForgedStore(Path(directory) / "artifacts")
            with self.assertRaisesRegex(PerpetualMarginError, "canonical ArtifactStore"):
                _verify_immutable_artifact(
                    forged,
                    artifact_id=str(uuid5(NAMESPACE_URL, "autotrade:margin:test")),
                    expected_payload={},
                    expected_metadata={},
                )
            self.assertFalse(forged.snapshot_called)

    def test_perpetual_margin_uses_one_class_qualified_snapshot(self):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory) / "artifacts")
            artifact_id = str(uuid5(NAMESPACE_URL, "autotrade:margin:snapshot"))
            expected_payload = {"kind": "margin-test", "value": "1"}
            import json
            data = json.dumps(
                expected_payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
            store.publish_bytes(
                artifact_id=artifact_id,
                data=data,
                media_type="application/json",
                rights={"storage": True, "export": False},
                metadata={"kind": "margin-test"},
            )
            original = ArtifactStore.read_authenticated_snapshot
            calls = []

            def counted(instance, requested_id):
                calls.append(requested_id)
                return original(instance, requested_id)

            store.load_manifest = lambda *_a, **_k: (_ for _ in ()).throw(
                AssertionError("split manifest read")
            )
            store.read_bytes = lambda *_a, **_k: (_ for _ in ()).throw(
                AssertionError("split payload read")
            )
            with patch.object(ArtifactStore, "read_authenticated_snapshot", new=counted):
                _verify_immutable_artifact(
                    store,
                    artifact_id=artifact_id,
                    expected_payload=expected_payload,
                    expected_metadata={"kind": "margin-test"},
                )
            self.assertEqual(calls, [artifact_id])

    def test_perpetual_margin_snapshot_failure_is_fail_closed(self):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory) / "artifacts")
            artifact_id = str(uuid5(NAMESPACE_URL, "autotrade:margin:failure"))
            with patch.object(
                ArtifactStore,
                "read_authenticated_snapshot",
                side_effect=OSError("simulated storage race"),
            ):
                with self.assertRaisesRegex(PerpetualMarginError, "missing or corrupt"):
                    _verify_immutable_artifact(
                        store,
                        artifact_id=artifact_id,
                        expected_payload={},
                        expected_metadata={},
                    )

    def _execution_model(self):
        return ExecutionModel.create(
            model_version="exec-realism-v1",
            calibration_sha256="a" * 64,
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

    def _execution_qualification(self, model, artifact_id, digest):
        return ExecutionModelQualification(
            qualification_id="q-1",
            asset_class="EQUITY",
            data_fidelity=model.data_fidelity,
            scenario=model.scenario,
            purpose="REPLAY",
            model_fingerprint=model.fingerprint,
            calibration_sha256=model.calibration_sha256,
            protocol_sha256="b" * 64,
            evidence_artifact_id=artifact_id,
            evidence_sha256=digest,
            instrument_version="ABC@v1",
        )

    def test_execution_qualification_rejects_store_subclass(self):
        class ForgedStore(ArtifactStore):
            snapshot_called = False

            def read_authenticated_snapshot(self, artifact_id):
                self.snapshot_called = True
                raise AssertionError("subclass method must not run")

        with TemporaryDirectory() as directory:
            forged = ForgedStore(Path(directory) / "artifacts")
            model = self._execution_model()
            artifact_id = str(uuid5(NAMESPACE_URL, "autotrade:exec:forged"))
            qualification = self._execution_qualification(
                model,
                artifact_id,
                "0" * 64,
            )
            with self.assertRaisesRegex(TypeError, "canonical ArtifactStore"):
                validate_execution_qualification(
                    model=model,
                    qualification=qualification,
                    asset_class="EQUITY",
                    instrument_version="ABC@v1",
                    protocol_sha256="b" * 64,
                    artifact_store=forged,
                    evidence_artifact_id=artifact_id,
                    purpose="REPLAY",
                )
            self.assertFalse(forged.snapshot_called)

    def test_execution_qualification_uses_authenticated_snapshot(self):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory) / "artifacts")
            model = self._execution_model()
            artifact_id = str(uuid5(NAMESPACE_URL, "autotrade:exec:snapshot"))
            data = b"execution evidence"
            manifest = store.publish_bytes(
                artifact_id=artifact_id,
                data=data,
                media_type="application/json",
                rights={"storage": True, "export": False},
            )
            qualification = self._execution_qualification(
                model,
                artifact_id,
                sha256(data).hexdigest(),
            )
            original = ArtifactStore.read_authenticated_snapshot
            calls = []

            def counted(instance, requested_id):
                calls.append(requested_id)
                return original(instance, requested_id)

            store.load_manifest = lambda *_a, **_k: (_ for _ in ()).throw(
                AssertionError("split manifest read")
            )
            store.read_bytes = lambda *_a, **_k: (_ for _ in ()).throw(
                AssertionError("split payload read")
            )
            with patch.object(ArtifactStore, "read_authenticated_snapshot", new=counted):
                validate_execution_qualification(
                    model=model,
                    qualification=qualification,
                    asset_class="EQUITY",
                    instrument_version="ABC@v1",
                    protocol_sha256="b" * 64,
                    artifact_store=store,
                    evidence_artifact_id=artifact_id,
                    purpose="REPLAY",
                )
            self.assertEqual(calls, [artifact_id])

    def test_execution_qualification_snapshot_failure_is_fail_closed(self):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory) / "artifacts")
            model = self._execution_model()
            artifact_id = str(uuid5(NAMESPACE_URL, "autotrade:exec:failure"))
            qualification = self._execution_qualification(
                model,
                artifact_id,
                "0" * 64,
            )
            for failure in (
                OSError("simulated storage race"),
                ArtifactIntegrityError("simulated integrity failure"),
            ):
                with self.subTest(failure=type(failure).__name__):
                    with patch.object(
                        ArtifactStore,
                        "read_authenticated_snapshot",
                        side_effect=failure,
                    ):
                        with self.assertRaisesRegex(
                            ExecutionQualificationError,
                            "cannot be verified",
                        ):
                            validate_execution_qualification(
                                model=model,
                                qualification=qualification,
                                asset_class="EQUITY",
                                instrument_version="ABC@v1",
                                protocol_sha256="b" * 64,
                                artifact_store=store,
                                evidence_artifact_id=artifact_id,
                                purpose="REPLAY",
                            )


if __name__ == "__main__":
    unittest.main()
