from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from autotrade_runtime.artifacts import ArtifactStore
from mvp.autotrade_mvp.performance_qualification import RuntimeBudgetSpec
from mvp.autotrade_mvp.persistence import JournalStore, payload_digest
from mvp.autotrade_mvp.runtime_load_evidence import ExpectedJournalEvent
from mvp.autotrade_mvp.runtime_load_measurement import measure_declared_financial_operation
from mvp.autotrade_mvp.runtime_load_plan import declare_runtime_event_plan
from mvp.autotrade_mvp.runtime_load_research_measurement import (
    ExpectedResearchInterferenceSample,
    declare_research_interference_plan,
    measure_declared_research_interference,
)
from mvp.autotrade_mvp.runtime_target_host_campaign_authority import (
    declare_runtime_target_host_campaign_authority,
)
from mvp.autotrade_mvp.runtime_target_host_measurement import (
    collect_runtime_target_host_measurement,
)
from mvp.autotrade_mvp.runtime_target_host_measurement_publication import (
    RAW_EVIDENCE_KIND,
    RuntimeTargetHostMeasurementPublicationError,
    publish_runtime_target_host_measurement,
)


SHA = "a" * 40
CONFIG = "sha256:" + ("b" * 64)
HOST = "sha256:" + ("c" * 64)
RELEASE_ARTIFACT_ID = "00000000-0000-0000-0000-000000000065"
RELEASE_ARTIFACT_SHA = "sha256:" + ("d" * 64)
MEASUREMENT_ARTIFACT_ID = "00000000-0000-4000-8000-000000000066"


def _spec() -> RuntimeBudgetSpec:
    return RuntimeBudgetSpec(
        scenario_id="target-host-publication",
        release_sha=SHA,
        configuration_hash=CONFIG,
        host_fingerprint=HOST,
        strategy_horizon_us=1_000,
        max_p95_financial_latency_us=500,
        max_financial_staleness_us=500,
        max_research_interference_us=500,
        min_financial_samples=1,
        min_research_samples=1,
    )


def _expected() -> ExpectedJournalEvent:
    return ExpectedJournalEvent(
        event_id="financial-publication-1",
        event_type="RuntimeQualificationFinancialEvent",
        aggregate_type="risk_decision",
        aggregate_id="target-host-publication",
        aggregate_version=1,
    )


def _append_financial(store: JournalStore, expected: ExpectedJournalEvent) -> None:
    payload = {"scenario": "target-host-publication", "event_id": expected.event_id}
    store.append_event(
        {
            "event_id": expected.event_id,
            "event_type": expected.event_type,
            "aggregate_type": expected.aggregate_type,
            "aggregate_id": expected.aggregate_id,
            "aggregate_version": str(expected.aggregate_version),
            "payload": payload,
            "payload_hash": payload_digest(payload),
            "committed_at": "2026-10-05T13:20:00Z",
        }
    )


def _artifact(root: str):
    store = JournalStore(Path(root) / "qualification.sqlite3")
    expected = _expected()
    financial_plan = declare_runtime_event_plan(
        store,
        plan_id="publication-financial-plan",
        spec=_spec(),
        expected_events=(expected,),
    )
    research_plan = declare_research_interference_plan(
        store,
        _spec(),
        plan_id="publication-research-plan",
        financial_plan_id=financial_plan.plan_id,
        expected_samples=(
            ExpectedResearchInterferenceSample("publication-research-1", "cpu"),
        ),
    )
    authority = declare_runtime_target_host_campaign_authority(
        store,
        _spec(),
        authority_id="publication-run",
        financial_plan_id=financial_plan.plan_id,
        release_artifact_id=RELEASE_ARTIFACT_ID,
        release_artifact_sha256=RELEASE_ARTIFACT_SHA,
    )
    with patch(
        "mvp.autotrade_mvp.runtime_load_measurement.perf_counter_ns",
        side_effect=(1_000_000, 1_050_000),
    ):
        measure_declared_financial_operation(
            store,
            _spec(),
            plan_id=financial_plan.plan_id,
            event_id=expected.event_id,
            operation=lambda: _append_financial(store, expected),
        )
    with patch(
        "mvp.autotrade_mvp.runtime_load_research_measurement.perf_counter_ns",
        side_effect=(2_000_000, 2_040_000),
    ):
        measure_declared_research_interference(
            store,
            _spec(),
            plan_id=research_plan.plan_id,
            sample_id="publication-research-1",
            operation=lambda: None,
        )
    artifact = collect_runtime_target_host_measurement(
        store,
        _spec(),
        authority_id=authority.authority_id,
        research_plan_id=research_plan.plan_id,
    )
    return artifact


class RuntimeTargetHostMeasurementPublicationTests(unittest.TestCase):
    def test_publication_retains_exact_raw_measurement_bytes_and_bindings(self):
        with tempfile.TemporaryDirectory() as root:
            artifact = _artifact(root)
            evidence_store = ArtifactStore(Path(root) / "evidence")

            published = publish_runtime_target_host_measurement(
                evidence_store,
                artifact_id=MEASUREMENT_ARTIFACT_ID,
                artifact=artifact,
            )

            manifest, raw = evidence_store.read_authenticated_snapshot(
                MEASUREMENT_ARTIFACT_ID
            )
            self.assertEqual(raw, artifact.canonical_bytes())
            self.assertEqual(published.payload_sha256, artifact.digest)
            self.assertEqual(manifest["sha256"], artifact.digest)
            self.assertEqual(manifest["media_type"], "application/json")
            self.assertEqual(manifest["source_refs"], [f"git:{SHA}"])
            self.assertEqual(
                manifest["metadata"],
                {
                    "evidence_kind": RAW_EVIDENCE_KIND,
                    "schema_version": artifact.schema_version,
                    "authority_id": artifact.authority_id,
                    "authority_digest": artifact.authority_digest,
                    "release_artifact_id": artifact.release_artifact_id,
                    "release_artifact_sha256": artifact.release_artifact_sha256,
                    "store_identity_digest": artifact.store_identity_digest,
                    "journal_taxonomy_digest": artifact.journal_taxonomy_digest,
                    "resource_evidence_status": "NOT_COLLECTED",
                },
            )
            self.assertEqual(published.authority_id, artifact.authority_id)
            self.assertEqual(published.release_artifact_id, RELEASE_ARTIFACT_ID)
            self.assertEqual(published.resource_evidence_status, "NOT_COLLECTED")

    def test_exact_publication_is_idempotent(self):
        with tempfile.TemporaryDirectory() as root:
            artifact = _artifact(root)
            evidence_store = ArtifactStore(Path(root) / "evidence")
            first = publish_runtime_target_host_measurement(
                evidence_store,
                artifact_id=MEASUREMENT_ARTIFACT_ID,
                artifact=artifact,
            )
            second = publish_runtime_target_host_measurement(
                evidence_store,
                artifact_id=MEASUREMENT_ARTIFACT_ID,
                artifact=artifact,
            )
            self.assertEqual(first, second)

    def test_noncanonical_artifact_id_fails_before_publication(self):
        with tempfile.TemporaryDirectory() as root:
            artifact = _artifact(root)
            evidence_store = ArtifactStore(Path(root) / "evidence")
            with self.assertRaisesRegex(
                RuntimeTargetHostMeasurementPublicationError,
                "canonical UUID",
            ):
                publish_runtime_target_host_measurement(
                    evidence_store,
                    artifact_id="AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA",
                    artifact=artifact,
                )
            self.assertEqual(list(evidence_store.manifests.iterdir()), [])

    def test_publication_requires_exact_store_and_collector_issued_artifact(self):
        with self.assertRaisesRegex(TypeError, "exact ArtifactStore"):
            publish_runtime_target_host_measurement(
                object(),
                artifact_id=MEASUREMENT_ARTIFACT_ID,
                artifact=object(),
            )
        with tempfile.TemporaryDirectory() as root:
            evidence_store = ArtifactStore(Path(root) / "evidence")
            with self.assertRaisesRegex(
                TypeError,
                "exact RuntimeTargetHostMeasurementArtifact",
            ):
                publish_runtime_target_host_measurement(
                    evidence_store,
                    artifact_id=MEASUREMENT_ARTIFACT_ID,
                    artifact=object(),
                )


if __name__ == "__main__":
    unittest.main()
