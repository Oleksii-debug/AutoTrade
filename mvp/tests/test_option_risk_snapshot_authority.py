from dataclasses import replace
from datetime import datetime, timedelta, timezone
from tempfile import TemporaryDirectory
import json
import unittest
from unittest.mock import patch
from uuid import uuid4

from autotrade_runtime.artifacts.store import ArtifactIntegrityError, ArtifactStore
from mvp.autotrade_mvp.options import (
    OptionError,
    OptionRiskEvidence,
    OptionScenarioResult,
    option_risk_evidence_metadata,
    option_risk_evidence_payload,
    require_current_option_risk,
)


def _at(hour: int):
    return datetime(2026, 9, 25, hour, tzinfo=timezone.utc)


def _evidence() -> OptionRiskEvidence:
    return OptionRiskEvidence(
        instrument="OPT:CALL",
        model_id="scenario-greeks",
        model_version="1.2.0",
        source_sha="a" * 40,
        input_digest="sha256:" + "b" * 64,
        schema_version=1,
        market_as_of=_at(17),
        calculated_at=_at(18),
        expires_at=_at(19),
        maximum_market_age=timedelta(hours=2),
        delta="0.52",
        gamma="0.03",
        vega="12.5",
        theta="-4.2",
        rho="1.1",
        scenarios=(
            OptionScenarioResult(
                scenario_id="stress",
                underlying_price="80",
                implied_volatility="0.55",
                pnl="-725.25",
            ),
        ),
        tests_run=("scenario-stress",),
        unresolved_limits=(),
    )


def _bind(store: ArtifactStore, evidence: OptionRiskEvidence) -> OptionRiskEvidence:
    data = json.dumps(
        option_risk_evidence_payload(evidence),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    artifact_id = str(uuid4())
    manifest = store.publish_bytes(
        artifact_id=artifact_id,
        data=data,
        media_type="application/json",
        rights={"storage": True, "export": False},
        metadata=option_risk_evidence_metadata(evidence),
    )
    return replace(
        evidence,
        evidence_ref=f"artifact:{artifact_id}@{manifest['sha256']}",
    )


def _validate(evidence: OptionRiskEvidence, store: ArtifactStore) -> None:
    require_current_option_risk(
        evidence,
        instrument="OPT:CALL",
        at=datetime(2026, 9, 25, 18, 30, tzinfo=timezone.utc),
        maximum_calculation_age=timedelta(hours=2),
        maximum_market_age=timedelta(hours=2),
        artifact_store=store,
    )


class OptionRiskSnapshotAuthorityTests(unittest.TestCase):
    def test_artifact_store_subclass_is_rejected_before_virtual_dispatch(self):
        class ForgedStore(ArtifactStore):
            snapshot_called = False

            def read_authenticated_snapshot(self, artifact_id):
                self.snapshot_called = True
                raise AssertionError("subclass method must not run")

        with TemporaryDirectory() as directory:
            forged = ForgedStore(directory)
            with self.assertRaisesRegex(OptionError, "canonical ArtifactStore"):
                _validate(_evidence(), forged)
            self.assertFalse(forged.snapshot_called)

    def test_one_class_qualified_snapshot_is_used(self):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            bound = _bind(store, _evidence())
            original = ArtifactStore.read_authenticated_snapshot
            calls = []

            def counted(instance, artifact_id):
                calls.append((instance, artifact_id))
                return original(instance, artifact_id)

            store.load_manifest = lambda *_a, **_k: (_ for _ in ()).throw(
                AssertionError("split manifest read")
            )
            store.read_bytes = lambda *_a, **_k: (_ for _ in ()).throw(
                AssertionError("split payload read")
            )
            store.read_authenticated_snapshot = lambda *_a, **_k: (_ for _ in ()).throw(
                AssertionError("instance virtual dispatch")
            )
            with patch.object(ArtifactStore, "read_authenticated_snapshot", new=counted):
                _validate(bound, store)

            self.assertEqual(len(calls), 1)

    def test_snapshot_storage_and_integrity_failures_are_fail_closed(self):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            bound = _bind(store, _evidence())
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
                            OptionError,
                            "artifact is missing or corrupt",
                        ):
                            _validate(bound, store)


if __name__ == "__main__":
    unittest.main()
