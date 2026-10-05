from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from autotrade_runtime.artifacts import ArtifactStore
from mvp.autotrade_mvp.persistence import JournalStore, payload_digest
from mvp.autotrade_mvp.runtime_target_host_resource_evidence import (
    RuntimeTargetHostResourceEvidenceError,
    run_declared_target_host_campaign_with_resources,
)
from mvp.tests.test_runtime_target_host_resource_evidence import (
    INVENTORY_ARTIFACT_ID,
    MEASUREMENT_ARTIFACT_ID,
    RESOURCE_ARTIFACT_ID,
    RUN_RECEIPT_ARTIFACT_ID,
    _run_result,
    _snapshot,
)


def _outbox_event(event_id: str, aggregate_id: str) -> dict[str, object]:
    payload = {"kind": "resource-backlog", "event_id": event_id}
    return {
        "event_id": event_id,
        "event_type": "ResourceBacklogObserved",
        "aggregate_type": "resource-backlog-test",
        "aggregate_id": aggregate_id,
        "aggregate_version": "1",
        "payload": payload,
        "payload_hash": payload_digest(payload),
        "committed_at": "2026-10-05T18:00:00+00:00",
    }


def _run_wrapper(
    *,
    journal: JournalStore,
    store: ArtifactStore,
):
    return run_declared_target_host_campaign_with_resources(
        journal=journal,
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


class RuntimeTargetHostResourceBacklogHighWaterTests(unittest.TestCase):
    def test_wrapper_binds_exact_backlog_peak_and_transition_window(self):
        run = _run_result()
        run.measurement.reconnect_backlog_remaining = 1
        before = _snapshot(monotonic_ns=100, process_cpu_ns=10)
        after = _snapshot(
            monotonic_ns=200,
            process_cpu_ns=20,
            peak_rss_bytes=100_100,
            io_read_bytes=1_100,
            io_write_bytes=2_100,
            disk_free_bytes=7_999_000,
        )

        with tempfile.TemporaryDirectory() as root:
            journal = JournalStore(Path(root) / "journal.sqlite3")
            store = ArtifactStore(Path(root) / "evidence")

            def runner(**_kwargs):
                journal.append_event(
                    _outbox_event("evt-backlog-a", "a"),
                    outbox_topic="events",
                )
                journal.append_event(
                    _outbox_event("evt-backlog-b", "b"),
                    outbox_topic="events",
                )
                first = journal.pending_outbox()[0]
                self.assertTrue(
                    journal.mark_outbox_delivered(
                        first["outbox_id"],
                        expected_envelope_hash=first["envelope_hash"],
                    )
                )
                return run

            with (
                patch(
                    "mvp.autotrade_mvp.runtime_target_host_resource_evidence.capture_runtime_target_host_resource_snapshot",
                    side_effect=(before, after),
                ),
                patch(
                    "mvp.autotrade_mvp.runtime_target_host_resource_evidence.run_declared_target_host_campaign",
                    side_effect=runner,
                ),
            ):
                result = _run_wrapper(journal=journal, store=store)

            evidence = result.resource_evidence
            self.assertEqual(evidence.outbox_transition_start_sequence, 0)
            self.assertEqual(evidence.outbox_transition_end_sequence, 3)
            self.assertEqual(evidence.outbox_backlog_start_pending_count, 0)
            self.assertEqual(evidence.outbox_backlog_end_pending_count, 1)
            self.assertEqual(evidence.queue_backlog_high_water, 2)
            self.assertEqual(evidence.reconnect_backlog_remaining, 1)
            self.assertNotIn(
                "queue_backlog_high_water",
                evidence.canonical_payload()["unclosed_authorities"],
            )
            self.assertEqual(
                evidence.canonical_payload()["outbox_backlog"],
                {
                    "transition_start_sequence": 0,
                    "transition_end_sequence": 3,
                    "start_pending_count": 0,
                    "end_pending_count": 1,
                    "high_water": 2,
                },
            )

    def test_wrapper_refuses_backlog_end_that_disagrees_with_measurement(self):
        run = _run_result()
        run.measurement.reconnect_backlog_remaining = 0
        before = _snapshot(monotonic_ns=100, process_cpu_ns=10)
        after = _snapshot(monotonic_ns=200, process_cpu_ns=20)

        with tempfile.TemporaryDirectory() as root:
            journal = JournalStore(Path(root) / "journal.sqlite3")
            store = ArtifactStore(Path(root) / "evidence")

            def runner(**_kwargs):
                journal.append_event(
                    _outbox_event("evt-backlog-mismatch", "mismatch"),
                    outbox_topic="events",
                )
                return run

            with (
                patch(
                    "mvp.autotrade_mvp.runtime_target_host_resource_evidence.capture_runtime_target_host_resource_snapshot",
                    side_effect=(before, after),
                ),
                patch(
                    "mvp.autotrade_mvp.runtime_target_host_resource_evidence.run_declared_target_host_campaign",
                    side_effect=runner,
                ),
            ):
                with self.assertRaisesRegex(
                    RuntimeTargetHostResourceEvidenceError,
                    "outbox backlog end does not match reconnect backlog authority",
                ):
                    _run_wrapper(journal=journal, store=store)

            with self.assertRaises(FileNotFoundError):
                store.read_authenticated_snapshot(RESOURCE_ARTIFACT_ID)

    def test_wrapper_rejects_preentry_backlog_method_replacement(self):
        run = _run_result()
        before = _snapshot(monotonic_ns=100, process_cpu_ns=10)
        after = _snapshot(monotonic_ns=200, process_cpu_ns=20)

        def forged_cut(_self):
            return {"transition_sequence": 0, "pending_count": 0}

        with tempfile.TemporaryDirectory() as root:
            journal = JournalStore(Path(root) / "journal.sqlite3")
            store = ArtifactStore(Path(root) / "evidence")
            with (
                patch.object(JournalStore, "outbox_backlog_cut", forged_cut),
                patch(
                    "mvp.autotrade_mvp.runtime_target_host_resource_evidence.capture_runtime_target_host_resource_snapshot",
                    side_effect=(before, after),
                ) as capture,
                patch(
                    "mvp.autotrade_mvp.runtime_target_host_resource_evidence.run_declared_target_host_campaign",
                    return_value=run,
                ) as runner,
            ):
                with self.assertRaisesRegex(
                    RuntimeTargetHostResourceEvidenceError,
                    "JournalStore backlog authority changed before target-host run",
                ):
                    _run_wrapper(journal=journal, store=store)

            capture.assert_not_called()
            runner.assert_not_called()
            with self.assertRaises(FileNotFoundError):
                store.read_authenticated_snapshot(RESOURCE_ARTIFACT_ID)


if __name__ == "__main__":
    unittest.main()
