from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from autotrade_runtime.artifacts import ArtifactStore
from mvp.autotrade_mvp.runtime_target_host_resource_evidence import (
    RuntimeTargetHostResourceEvidence,
    RuntimeTargetHostResourceEvidenceError,
    RuntimeTargetHostResourceSnapshot,
    capture_runtime_target_host_resource_snapshot,
    issue_runtime_target_host_resource_evidence,
    publish_runtime_target_host_resource_evidence,
    run_declared_target_host_campaign_with_resources,
)
from mvp.autotrade_mvp.runtime_target_host_runner import RuntimeTargetHostRunResult


SOURCE_SHA = "a" * 40
AUTHORITY_DIGEST = "sha256:" + ("1" * 64)
RELEASE_ARTIFACT_ID = "00000000-0000-4000-8000-000000000071"
RELEASE_ARTIFACT_SHA = "sha256:" + ("2" * 64)
INVENTORY_ARTIFACT_ID = "00000000-0000-4000-8000-000000000072"
INVENTORY_DIGEST = "sha256:" + ("3" * 64)
MEASUREMENT_ARTIFACT_ID = "00000000-0000-4000-8000-000000000073"
MEASUREMENT_DIGEST = "sha256:" + ("4" * 64)
RUN_RECEIPT_ARTIFACT_ID = "00000000-0000-4000-8000-000000000074"
RUN_RECEIPT_DIGEST = "sha256:" + ("5" * 64)
RESOURCE_ARTIFACT_ID = "00000000-0000-4000-8000-000000000075"
HOST = "sha256:" + ("6" * 64)
SPEC_DIGEST = "sha256:" + ("7" * 64)


def _snapshot(
    *,
    monotonic_ns: int,
    process_cpu_ns: int,
    peak_rss_bytes: int = 100_000,
    io_read_bytes: int = 1_000,
    io_write_bytes: int = 2_000,
    disk_total_bytes: int = 10_000_000,
    disk_free_bytes: int = 8_000_000,
    thread_count: int = 3,
) -> RuntimeTargetHostResourceSnapshot:
    return RuntimeTargetHostResourceSnapshot(
        monotonic_ns=monotonic_ns,
        process_cpu_ns=process_cpu_ns,
        peak_rss_bytes=peak_rss_bytes,
        io_read_bytes=io_read_bytes,
        io_write_bytes=io_write_bytes,
        disk_total_bytes=disk_total_bytes,
        disk_free_bytes=disk_free_bytes,
        thread_count=thread_count,
    )


def _run_result() -> RuntimeTargetHostRunResult:
    authority = SimpleNamespace(
        authority_id="resource-authority",
        digest=AUTHORITY_DIGEST,
        source_sha=SOURCE_SHA,
        release_artifact_id=RELEASE_ARTIFACT_ID,
        release_artifact_sha256=RELEASE_ARTIFACT_SHA,
        scenario_id="resource-campaign",
        spec_digest=SPEC_DIGEST,
        host_fingerprint=HOST,
    )
    measurement = SimpleNamespace(
        digest=MEASUREMENT_DIGEST,
        reconnect_backlog_remaining=0,
    )
    published_inventory = SimpleNamespace(
        artifact_id=INVENTORY_ARTIFACT_ID,
        payload_sha256=INVENTORY_DIGEST,
    )
    published_measurement = SimpleNamespace(
        artifact_id=MEASUREMENT_ARTIFACT_ID,
        payload_sha256=MEASUREMENT_DIGEST,
    )
    receipt = SimpleNamespace(digest=RUN_RECEIPT_DIGEST)
    return RuntimeTargetHostRunResult(
        authority=authority,
        research_plan=object(),
        measurement=measurement,
        published_inventory=published_inventory,
        published_measurement=published_measurement,
        run_receipt=receipt,
        run_receipt_artifact_id=RUN_RECEIPT_ARTIFACT_ID,
        run_receipt_payload_sha256=RUN_RECEIPT_DIGEST,
    )


class RuntimeTargetHostResourceEvidenceTests(unittest.TestCase):
    def test_snapshot_collects_process_io_memory_disk_without_wall_clock(self):
        with tempfile.TemporaryDirectory() as root:
            with (
                patch(
                    "mvp.autotrade_mvp.runtime_target_host_resource_evidence.perf_counter_ns",
                    return_value=11_000,
                ),
                patch(
                    "mvp.autotrade_mvp.runtime_target_host_resource_evidence.process_time_ns",
                    return_value=7_000,
                ),
                patch(
                    "mvp.autotrade_mvp.runtime_target_host_resource_evidence._peak_rss_bytes",
                    return_value=123_456,
                ),
                patch(
                    "mvp.autotrade_mvp.runtime_target_host_resource_evidence._process_io_bytes",
                    return_value=(1_234, 5_678),
                ),
                patch(
                    "mvp.autotrade_mvp.runtime_target_host_resource_evidence.active_count",
                    return_value=4,
                ),
                patch(
                    "mvp.autotrade_mvp.runtime_target_host_resource_evidence.shutil.disk_usage",
                    return_value=SimpleNamespace(
                        total=50_000,
                        used=20_000,
                        free=30_000,
                    ),
                ),
            ):
                observed = capture_runtime_target_host_resource_snapshot(
                    evidence_root=Path(root)
                )

        self.assertEqual(observed.monotonic_ns, 11_000)
        self.assertEqual(observed.process_cpu_ns, 7_000)
        self.assertEqual(observed.peak_rss_bytes, 123_456)
        self.assertEqual(observed.io_read_bytes, 1_234)
        self.assertEqual(observed.io_write_bytes, 5_678)
        self.assertEqual(observed.disk_total_bytes, 50_000)
        self.assertEqual(observed.disk_free_bytes, 30_000)
        self.assertEqual(observed.thread_count, 4)
        self.assertNotIn("observed_at", observed.payload)
        self.assertNotIn("timestamp", observed.payload)

    def test_public_constructor_cannot_mint_resource_evidence(self):
        before = _snapshot(monotonic_ns=1, process_cpu_ns=1)
        after = _snapshot(monotonic_ns=2, process_cpu_ns=2)
        with self.assertRaisesRegex(
            RuntimeTargetHostResourceEvidenceError,
            "canonical issuer",
        ):
            RuntimeTargetHostResourceEvidence(
                authority_id="authority",
                authority_digest=AUTHORITY_DIGEST,
                source_sha=SOURCE_SHA,
                release_artifact_id=RELEASE_ARTIFACT_ID,
                release_artifact_sha256=RELEASE_ARTIFACT_SHA,
                scenario_id="scenario",
                spec_digest=SPEC_DIGEST,
                host_fingerprint=HOST,
                inventory_artifact_id=INVENTORY_ARTIFACT_ID,
                inventory_payload_sha256=INVENTORY_DIGEST,
                measurement_artifact_id=MEASUREMENT_ARTIFACT_ID,
                measurement_payload_sha256=MEASUREMENT_DIGEST,
                run_receipt_artifact_id=RUN_RECEIPT_ARTIFACT_ID,
                run_receipt_payload_sha256=RUN_RECEIPT_DIGEST,
                reconnect_backlog_remaining=0,
                before=before,
                after=after,
            )

    def test_issued_evidence_binds_exact_retained_run_chain_and_stays_nonterminal(self):
        before = _snapshot(monotonic_ns=10_000, process_cpu_ns=2_000)
        after = _snapshot(
            monotonic_ns=25_000,
            process_cpu_ns=9_000,
            peak_rss_bytes=150_000,
            io_read_bytes=1_600,
            io_write_bytes=3_200,
            disk_free_bytes=7_999_000,
            thread_count=5,
        )
        evidence = issue_runtime_target_host_resource_evidence(
            _run_result(),
            before=before,
            after=after,
        )

        self.assertEqual(evidence.authority_digest, AUTHORITY_DIGEST)
        self.assertEqual(evidence.inventory_artifact_id, INVENTORY_ARTIFACT_ID)
        self.assertEqual(evidence.measurement_payload_sha256, MEASUREMENT_DIGEST)
        self.assertEqual(evidence.run_receipt_payload_sha256, RUN_RECEIPT_DIGEST)
        self.assertEqual(evidence.elapsed_monotonic_ns, 15_000)
        self.assertEqual(evidence.process_cpu_delta_ns, 7_000)
        self.assertEqual(evidence.io_read_delta_bytes, 600)
        self.assertEqual(evidence.io_write_delta_bytes, 1_200)
        self.assertEqual(evidence.disk_free_delta_bytes, -1_000)
        self.assertEqual(evidence.resource_evidence_status, "COLLECTED_PROCESS_DISK_V1")
        self.assertFalse(evidence.terminal_qualification_eligible)
        payload = evidence.canonical_payload()
        self.assertEqual(
            payload["unclosed_authorities"],
            [
                "independent_chronology",
                "provider_source_clock_freshness",
                "queue_backlog_high_water",
                "signed_terminal_qualification",
            ],
        )
        self.assertNotIn("observed_at", payload)

    def test_counter_rollback_fails_closed_through_canonical_issuer(self):
        before = _snapshot(monotonic_ns=10_000, process_cpu_ns=2_000)
        after = _snapshot(monotonic_ns=9_999, process_cpu_ns=2_000)
        with self.assertRaisesRegex(
            RuntimeTargetHostResourceEvidenceError,
            "monotonic cut moved backwards",
        ):
            issue_runtime_target_host_resource_evidence(
                _run_result(),
                before=before,
                after=after,
            )

    def test_mismatched_retained_measurement_digest_cannot_issue_resource_evidence(self):
        run = _run_result()
        run.published_measurement.payload_sha256 = "sha256:" + ("8" * 64)
        with self.assertRaisesRegex(
            RuntimeTargetHostResourceEvidenceError,
            "measurement digest does not match",
        ):
            issue_runtime_target_host_resource_evidence(
                run,
                before=_snapshot(monotonic_ns=1, process_cpu_ns=1),
                after=_snapshot(monotonic_ns=2, process_cpu_ns=2),
            )

    def test_publication_retains_exact_canonical_resource_bytes(self):
        evidence = issue_runtime_target_host_resource_evidence(
            _run_result(),
            before=_snapshot(monotonic_ns=1, process_cpu_ns=1),
            after=_snapshot(
                monotonic_ns=2,
                process_cpu_ns=2,
                peak_rss_bytes=100_001,
                io_read_bytes=1_001,
                io_write_bytes=2_001,
                disk_free_bytes=7_999_999,
            ),
        )
        with tempfile.TemporaryDirectory() as root:
            store = ArtifactStore(Path(root) / "evidence")
            published = publish_runtime_target_host_resource_evidence(
                store,
                artifact_id=RESOURCE_ARTIFACT_ID,
                evidence=evidence,
            )
            manifest, raw = store.read_authenticated_snapshot(RESOURCE_ARTIFACT_ID)

        self.assertEqual(published.payload_sha256, evidence.digest)
        self.assertEqual(raw, evidence.canonical_bytes())
        self.assertEqual(manifest["sha256"], evidence.digest)
        self.assertEqual(
            manifest["metadata"]["resource_evidence_status"],
            "COLLECTED_PROCESS_DISK_V1",
        )
        self.assertFalse(manifest["metadata"]["terminal_qualification_eligible"])

    def test_wrapper_composes_existing_runner_and_retains_fourth_resource_artifact(self):
        run = _run_result()
        before = _snapshot(monotonic_ns=100, process_cpu_ns=10)
        after = _snapshot(
            monotonic_ns=200,
            process_cpu_ns=40,
            peak_rss_bytes=101_000,
            io_read_bytes=1_100,
            io_write_bytes=2_200,
            disk_free_bytes=7_999_500,
        )
        with tempfile.TemporaryDirectory() as root:
            store = ArtifactStore(Path(root) / "evidence")
            with (
                patch(
                    "mvp.autotrade_mvp.runtime_target_host_resource_evidence.capture_runtime_target_host_resource_snapshot",
                    side_effect=(before, after),
                ) as capture,
                patch(
                    "mvp.autotrade_mvp.runtime_target_host_resource_evidence.run_declared_target_host_campaign",
                    return_value=run,
                ) as runner,
            ):
                result = run_declared_target_host_campaign_with_resources(
                    journal=object(),
                    evidence_store=store,
                    spec=object(),
                    authority_id="resource-authority",
                    research_plan_id="resource-research-plan",
                    financial_operations={},
                    research_operations={},
                    inventory_artifact_id=INVENTORY_ARTIFACT_ID,
                    measurement_artifact_id=MEASUREMENT_ARTIFACT_ID,
                    run_receipt_artifact_id=RUN_RECEIPT_ARTIFACT_ID,
                    resource_artifact_id=RESOURCE_ARTIFACT_ID,
                )
            manifest, raw = store.read_authenticated_snapshot(RESOURCE_ARTIFACT_ID)

        self.assertEqual(capture.call_count, 2)
        runner.assert_called_once()
        self.assertIs(result.run, run)
        self.assertFalse(result.terminal_qualification_eligible)
        self.assertEqual(
            result.published_resource_evidence.artifact_id,
            RESOURCE_ARTIFACT_ID,
        )
        self.assertEqual(raw, result.resource_evidence.canonical_bytes())
        self.assertEqual(
            manifest["sha256"],
            result.published_resource_evidence.payload_sha256,
        )

    def test_wrapper_rejects_resource_artifact_identity_collision_before_run(self):
        with tempfile.TemporaryDirectory() as root:
            store = ArtifactStore(Path(root) / "evidence")
            with patch(
                "mvp.autotrade_mvp.runtime_target_host_resource_evidence.run_declared_target_host_campaign"
            ) as runner:
                with self.assertRaisesRegex(
                    RuntimeTargetHostResourceEvidenceError,
                    "must be distinct",
                ):
                    run_declared_target_host_campaign_with_resources(
                        journal=object(),
                        evidence_store=store,
                        spec=object(),
                        authority_id="resource-authority",
                        research_plan_id="resource-research-plan",
                        financial_operations={},
                        research_operations={},
                        inventory_artifact_id=INVENTORY_ARTIFACT_ID,
                        measurement_artifact_id=MEASUREMENT_ARTIFACT_ID,
                        run_receipt_artifact_id=RUN_RECEIPT_ARTIFACT_ID,
                        resource_artifact_id=RUN_RECEIPT_ARTIFACT_ID,
                    )
            runner.assert_not_called()


if __name__ == "__main__":
    unittest.main()
