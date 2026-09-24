from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4

from autotrade_research.artifacts.store import ArtifactStore
from autotrade_research.jobs import (
    JobBudgetError,
    JobConflictError,
    JobLeaseError,
    ResearchJobStore,
)


def digest(text: str) -> str:
    return "sha256:" + sha256(text.encode("utf-8")).hexdigest()


class ResearchJobStoreTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 24, 16, 0, tzinfo=timezone.utc)

    def _enqueue(self, store: ResearchJobStore, key: str = "same"):
        return store.enqueue(
            kind="research.replay",
            dedupe_key=key,
            input_hashes=[digest("dataset")],
            resource_budget={"wall_seconds": 60, "memory_bytes": 1024},
            lease_requeueable=True,
            now=self.now,
        )

    def test_duplicate_submission_is_idempotent_but_conflict_is_rejected(self):
        with TemporaryDirectory() as directory:
            store = ResearchJobStore(Path(directory) / "jobs.sqlite3")
            first, inserted = self._enqueue(store)
            second, inserted_again = self._enqueue(store)
            self.assertTrue(inserted)
            self.assertFalse(inserted_again)
            self.assertEqual(first["job_id"], second["job_id"])
            with self.assertRaises(JobConflictError):
                store.enqueue(
                    kind="research.replay",
                    dedupe_key="same",
                    input_hashes=[digest("different")],
                    resource_budget={"wall_seconds": 60, "memory_bytes": 1024},
                    now=self.now,
                )

    def test_only_one_worker_holds_the_live_lease(self):
        with TemporaryDirectory() as directory:
            store = ResearchJobStore(Path(directory) / "jobs.sqlite3")
            self._enqueue(store)
            first = store.claim("worker-a", now=self.now, lease_seconds=30)
            second = store.claim("worker-b", now=self.now, lease_seconds=30)
            self.assertIsNotNone(first)
            self.assertIsNone(second)
            self.assertEqual(first["owner"], "worker-a")
            self.assertEqual(first["state"], "RUNNING")

    def test_expired_lease_fences_stale_worker_and_requeues(self):
        with TemporaryDirectory() as directory:
            store = ResearchJobStore(Path(directory) / "jobs.sqlite3")
            job, _ = self._enqueue(store)
            first = store.claim("worker-a", now=self.now, lease_seconds=10)
            self.assertEqual(store.requeue_expired(now=self.now + timedelta(seconds=11)), 1)
            second = store.claim("worker-b", now=self.now + timedelta(seconds=11), lease_seconds=30)
            self.assertEqual(second["generation"], "2")
            with self.assertRaises(JobLeaseError):
                store.succeed(
                    job["job_id"],
                    worker_id="worker-a",
                    generation=int(first["generation"]),
                    output_refs=["artifact:stale"],
                    now=self.now + timedelta(seconds=12),
                )
            self.assertTrue(
                store.succeed(
                    job["job_id"],
                    worker_id="worker-b",
                    generation=int(second["generation"]),
                    output_refs=["artifact:accepted"],
                    now=self.now + timedelta(seconds=12),
                )
            )

    def test_expired_non_idempotent_job_is_not_requeued(self):
        with TemporaryDirectory() as directory:
            store = ResearchJobStore(Path(directory) / "jobs.sqlite3")
            job, _ = store.enqueue(
                kind="research.external_annotation",
                dedupe_key="external-side-effect",
                input_hashes=[digest("dataset")],
                resource_budget={"wall_seconds": 60},
                now=self.now,
            )
            claimed = store.claim("worker-a", now=self.now, lease_seconds=10)
            self.assertFalse(claimed["lease_requeueable"])

            self.assertEqual(
                store.requeue_expired(now=self.now + timedelta(seconds=11)),
                0,
            )
            waiting = store.get(job["job_id"])
            self.assertEqual(waiting["state"], "WAITING_EXTERNAL")
            self.assertEqual(waiting["generation"], "2")
            self.assertEqual(
                waiting["error"]["code"],
                "LEASE_EXPIRED_NON_IDEMPOTENT",
            )
            self.assertIsNone(
                store.claim(
                    "worker-b",
                    now=self.now + timedelta(seconds=12),
                    lease_seconds=30,
                )
            )

    def test_v1_migration_defaults_existing_jobs_to_non_requeueable(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "jobs.sqlite3"
            job_id = str(uuid4())
            with sqlite3.connect(path) as connection:
                connection.executescript(
                    """
                    CREATE TABLE schema_migrations(
                        version INTEGER PRIMARY KEY,
                        applied_at TEXT NOT NULL
                    );
                    INSERT INTO schema_migrations(version, applied_at)
                    VALUES(1, '2026-09-24T15:00:00Z');
                    CREATE TABLE jobs (
                        job_id TEXT PRIMARY KEY,
                        kind TEXT NOT NULL,
                        dedupe_key TEXT NOT NULL UNIQUE,
                        input_hashes_json TEXT NOT NULL,
                        state TEXT NOT NULL,
                        generation INTEGER NOT NULL CHECK (generation > 0),
                        attempt INTEGER NOT NULL CHECK (attempt >= 0),
                        owner TEXT,
                        lease_until TEXT,
                        checkpoint_ref TEXT,
                        resource_budget_json TEXT NOT NULL,
                        resource_usage_json TEXT NOT NULL,
                        output_refs_json TEXT NOT NULL,
                        error_json TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    """
                )
                connection.execute(
                    """
                    INSERT INTO jobs(
                        job_id, kind, dedupe_key, input_hashes_json, state, generation,
                        attempt, owner, lease_until, checkpoint_ref, resource_budget_json,
                        resource_usage_json, output_refs_json, error_json, created_at, updated_at
                    ) VALUES(?, 'research.external_annotation', 'legacy', ?, 'RUNNING', 1,
                             1, 'legacy-worker', ?, NULL, ?, '{}', '[]', NULL, ?, ?)
                    """,
                    (
                        job_id,
                        '["' + digest("dataset") + '"]',
                        "2026-09-24T15:59:00Z",
                        '{"wall_seconds":60.0}',
                        "2026-09-24T15:00:00Z",
                        "2026-09-24T15:00:00Z",
                    ),
                )

            store = ResearchJobStore(path)
            migrated = store.get(job_id)
            self.assertFalse(migrated["lease_requeueable"])
            self.assertEqual(store.requeue_expired(now=self.now), 0)
            self.assertEqual(store.get(job_id)["state"], "WAITING_EXTERNAL")

    def test_requeueability_is_part_of_dedupe_identity(self):
        with TemporaryDirectory() as directory:
            store = ResearchJobStore(Path(directory) / "jobs.sqlite3")
            self._enqueue(store, "retry-contract")
            with self.assertRaises(JobConflictError):
                store.enqueue(
                    kind="research.replay",
                    dedupe_key="retry-contract",
                    input_hashes=[digest("dataset")],
                    resource_budget={"wall_seconds": 60, "memory_bytes": 1024},
                    lease_requeueable=False,
                    now=self.now,
                )


    def test_waiting_external_can_requeue_only_with_immutable_proof_not_run(self):
        with TemporaryDirectory() as directory:
            store = ResearchJobStore(Path(directory) / "jobs.sqlite3")
            job, _ = store.enqueue(
                kind="research.external_annotation",
                dedupe_key="prove-not-run",
                input_hashes=[digest("dataset")],
                resource_budget={"wall_seconds": 60},
                now=self.now,
            )
            claimed = store.claim("worker-a", now=self.now, lease_seconds=10)
            old_generation = int(claimed["generation"])
            store.requeue_expired(now=self.now + timedelta(seconds=11))
            waiting = store.get(job["job_id"])
            self.assertEqual(waiting["state"], "WAITING_EXTERNAL")
            self.assertEqual(int(waiting["generation"]), old_generation + 1)

            evidence = (
                "artifact:11111111-1111-4111-8111-111111111111@sha256:"
                + "a" * 64
            )
            self.assertTrue(
                store.resolve_waiting_external(
                    job["job_id"],
                    verdict="PROVEN_NOT_RUN",
                    evidence_ref=evidence,
                    now=self.now + timedelta(seconds=12),
                )
            )
            queued = store.get(job["job_id"])
            self.assertEqual(queued["state"], "QUEUED")
            self.assertEqual(
                queued["external_resolution"]["evidence_ref"],
                evidence,
            )

            reclaimed = store.claim(
                "worker-b",
                now=self.now + timedelta(seconds=13),
                lease_seconds=30,
            )
            self.assertEqual(reclaimed["job_id"], job["job_id"])
            self.assertEqual(int(reclaimed["generation"]), old_generation + 1)
            with self.assertRaises(JobLeaseError):
                store.succeed(
                    job["job_id"],
                    worker_id="worker-a",
                    generation=old_generation,
                    output_refs=["artifact:stale"],
                    now=self.now + timedelta(seconds=14),
                )

    def test_waiting_external_proven_success_is_terminal_and_idempotent(self):
        with TemporaryDirectory() as directory:
            store = ResearchJobStore(Path(directory) / "jobs.sqlite3")
            job, _ = store.enqueue(
                kind="research.external_annotation",
                dedupe_key="prove-success",
                input_hashes=[digest("dataset")],
                resource_budget={"wall_seconds": 60},
                now=self.now,
            )
            store.claim("worker-a", now=self.now, lease_seconds=10)
            store.requeue_expired(now=self.now + timedelta(seconds=11))
            evidence = (
                "artifact:22222222-2222-4222-8222-222222222222@sha256:"
                + "b" * 64
            )
            outputs = ["artifact:accepted-output"]
            resolved_at = self.now + timedelta(seconds=12)
            self.assertTrue(
                store.resolve_waiting_external(
                    job["job_id"],
                    verdict="PROVEN_SUCCEEDED",
                    evidence_ref=evidence,
                    output_refs=outputs,
                    now=resolved_at,
                )
            )
            result = store.get(job["job_id"])
            self.assertEqual(result["state"], "SUCCEEDED")
            self.assertEqual(result["output_refs"], outputs)
            self.assertFalse(
                store.resolve_waiting_external(
                    job["job_id"],
                    verdict="PROVEN_SUCCEEDED",
                    evidence_ref=evidence,
                    output_refs=outputs,
                    now=resolved_at + timedelta(seconds=30),
                )
            )
            with self.assertRaises(JobConflictError):
                store.resolve_waiting_external(
                    job["job_id"],
                    verdict="PROVEN_SUCCEEDED",
                    evidence_ref=evidence,
                    output_refs=["artifact:different-output"],
                    now=resolved_at + timedelta(seconds=31),
                )
            with self.assertRaises(JobConflictError):
                store.resolve_waiting_external(
                    job["job_id"],
                    verdict="PROVEN_SUCCEEDED",
                    evidence_ref=(
                        "artifact:22222222-2222-4222-8222-222222222222@sha256:"
                        + "c" * 64
                    ),
                    output_refs=outputs,
                    now=resolved_at,
                )

    def test_waiting_external_resolution_rejects_weak_evidence_and_output_mismatch(self):
        with TemporaryDirectory() as directory:
            store = ResearchJobStore(Path(directory) / "jobs.sqlite3")
            job, _ = store.enqueue(
                kind="research.external_annotation",
                dedupe_key="bad-resolution",
                input_hashes=[digest("dataset")],
                resource_budget={"wall_seconds": 60},
                now=self.now,
            )
            store.claim("worker-a", now=self.now, lease_seconds=10)
            store.requeue_expired(now=self.now + timedelta(seconds=11))

            with self.assertRaisesRegex(ValueError, "immutable artifact"):
                store.resolve_waiting_external(
                    job["job_id"],
                    verdict="PROVEN_NOT_RUN",
                    evidence_ref="ticket-123",
                    now=self.now + timedelta(seconds=12),
                )
            evidence = (
                "artifact:33333333-3333-4333-8333-333333333333@sha256:"
                + "d" * 64
            )
            with self.assertRaisesRegex(ValueError, "requires output_refs"):
                store.resolve_waiting_external(
                    job["job_id"],
                    verdict="PROVEN_SUCCEEDED",
                    evidence_ref=evidence,
                    now=self.now + timedelta(seconds=12),
                )
            with self.assertRaisesRegex(ValueError, "only for PROVEN_SUCCEEDED"):
                store.resolve_waiting_external(
                    job["job_id"],
                    verdict="PROVEN_FAILED",
                    evidence_ref=evidence,
                    output_refs=["unexpected"],
                    now=self.now + timedelta(seconds=12),
                )

    def test_cancellation_blocks_late_publication(self):
        with TemporaryDirectory() as directory:
            store = ResearchJobStore(Path(directory) / "jobs.sqlite3")
            job, _ = self._enqueue(store)
            claimed = store.claim("worker-a", now=self.now, lease_seconds=30)
            self.assertTrue(store.cancel(job["job_id"], now=self.now + timedelta(seconds=1)))
            with self.assertRaises(JobLeaseError):
                store.succeed(
                    job["job_id"],
                    worker_id="worker-a",
                    generation=int(claimed["generation"]),
                    output_refs=["artifact:late"],
                    now=self.now + timedelta(seconds=2),
                )
            self.assertEqual(store.get(job["job_id"])["state"], "CANCELLED")

    def test_checkpoint_enforces_declared_resource_budget(self):
        with TemporaryDirectory() as directory:
            store = ResearchJobStore(Path(directory) / "jobs.sqlite3")
            job, _ = self._enqueue(store)
            claimed = store.claim("worker-a", now=self.now, lease_seconds=30)
            updated = store.checkpoint(
                job["job_id"],
                worker_id="worker-a",
                generation=int(claimed["generation"]),
                checkpoint_ref="artifact:checkpoint-1",
                resource_usage={"wall_seconds": 20, "memory_bytes": 512},
                now=self.now + timedelta(seconds=1),
            )
            self.assertEqual(updated["checkpoint_ref"], "artifact:checkpoint-1")
            with self.assertRaises(JobBudgetError):
                store.checkpoint(
                    job["job_id"],
                    worker_id="worker-a",
                    generation=int(claimed["generation"]),
                    checkpoint_ref="artifact:checkpoint-2",
                    resource_usage={"wall_seconds": 61, "memory_bytes": 512},
                    now=self.now + timedelta(seconds=2),
                )

    def test_checkpoint_resource_usage_is_monotonic_and_sparse_updates_preserve_totals(self):
        with TemporaryDirectory() as directory:
            store = ResearchJobStore(Path(directory) / "jobs.sqlite3")
            job, _ = self._enqueue(store, "monotonic-usage")
            claimed = store.claim("worker-a", now=self.now, lease_seconds=30)
            generation = int(claimed["generation"])

            first = store.checkpoint(
                job["job_id"],
                worker_id="worker-a",
                generation=generation,
                checkpoint_ref="artifact:checkpoint-1",
                resource_usage={"wall_seconds": 20, "memory_bytes": 512},
                now=self.now + timedelta(seconds=1),
            )
            self.assertEqual(first["resource_usage"]["wall_seconds"], 20.0)
            self.assertEqual(first["resource_usage"]["memory_bytes"], 512.0)

            second = store.checkpoint(
                job["job_id"],
                worker_id="worker-a",
                generation=generation,
                checkpoint_ref="artifact:checkpoint-2",
                resource_usage={"memory_bytes": 768},
                now=self.now + timedelta(seconds=2),
            )
            self.assertEqual(second["resource_usage"]["wall_seconds"], 20.0)
            self.assertEqual(second["resource_usage"]["memory_bytes"], 768.0)

            with self.assertRaises(JobBudgetError):
                store.checkpoint(
                    job["job_id"],
                    worker_id="worker-a",
                    generation=generation,
                    checkpoint_ref="artifact:checkpoint-regression",
                    resource_usage={"wall_seconds": 19},
                    now=self.now + timedelta(seconds=3),
                )
            current = store.get(job["job_id"])
            self.assertEqual(current["resource_usage"]["wall_seconds"], 20.0)
            self.assertEqual(current["resource_usage"]["memory_bytes"], 768.0)

    def test_only_one_result_is_accepted_for_a_generation(self):
        with TemporaryDirectory() as directory:
            store = ResearchJobStore(Path(directory) / "jobs.sqlite3")
            job, _ = self._enqueue(store)
            claimed = store.claim("worker-a", now=self.now, lease_seconds=30)
            generation = int(claimed["generation"])
            self.assertTrue(
                store.succeed(
                    job["job_id"],
                    worker_id="worker-a",
                    generation=generation,
                    output_refs=["artifact:one"],
                    now=self.now + timedelta(seconds=1),
                )
            )
            self.assertFalse(
                store.succeed(
                    job["job_id"],
                    worker_id="worker-a",
                    generation=generation,
                    output_refs=["artifact:one"],
                    now=self.now + timedelta(seconds=2),
                )
            )
            with self.assertRaises(JobConflictError):
                store.succeed(
                    job["job_id"],
                    worker_id="worker-a",
                    generation=generation,
                    output_refs=["artifact:two"],
                    now=self.now + timedelta(seconds=3),
                )

    def test_financial_send_cannot_be_queued_as_generic_job(self):
        with TemporaryDirectory() as directory:
            store = ResearchJobStore(Path(directory) / "jobs.sqlite3")
            with self.assertRaises(ValueError):
                store.enqueue(
                    kind="financial.order_send",
                    dedupe_key="forbidden",
                    input_hashes=[digest("order")],
                    resource_budget={"wall_seconds": 1},
                    now=self.now,
                )

    def test_records_survive_reopen(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "jobs.sqlite3"
            first_store = ResearchJobStore(path)
            job, _ = self._enqueue(first_store, "persisted")
            second_store = ResearchJobStore(path)
            self.assertEqual(second_store.get(job["job_id"])["dedupe_key"], "persisted")

    def test_crash_after_artifact_publish_retries_without_double_acceptance(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            jobs = ResearchJobStore(root / "jobs.sqlite3")
            artifacts = ArtifactStore(root / "artifacts")
            job, _ = self._enqueue(jobs, "artifact-crash")
            claimed = jobs.claim("worker-a", now=self.now, lease_seconds=30)
            generation = int(claimed["generation"])
            rights = {"storage": True, "export": False}

            with patch.object(jobs, "succeed", side_effect=RuntimeError("simulated crash")):
                with self.assertRaises(RuntimeError):
                    jobs.publish_result_bytes(
                        job["job_id"],
                        worker_id="worker-a",
                        generation=generation,
                        artifact_store=artifacts,
                        data=b"candidate-result",
                        media_type="application/octet-stream",
                        rights=rights,
                        now=self.now + timedelta(seconds=1),
                    )

            self.assertEqual(jobs.get(job["job_id"])["state"], "RUNNING")
            self.assertEqual(len(list((root / "artifacts" / "manifests").glob("*.json"))), 1)

            manifest, accepted = jobs.publish_result_bytes(
                job["job_id"],
                worker_id="worker-a",
                generation=generation,
                artifact_store=artifacts,
                data=b"candidate-result",
                media_type="application/octet-stream",
                rights=rights,
                now=self.now + timedelta(seconds=2),
            )
            self.assertTrue(accepted)
            final = jobs.get(job["job_id"])
            self.assertEqual(final["state"], "SUCCEEDED")
            self.assertEqual(len(final["output_refs"]), 1)
            self.assertIn(manifest["artifact_id"], final["output_refs"][0])
            self.assertEqual(len(list((root / "artifacts" / "manifests").glob("*.json"))), 1)

    def test_different_result_bytes_cannot_replace_crash_published_artifact(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            jobs = ResearchJobStore(root / "jobs.sqlite3")
            artifacts = ArtifactStore(root / "artifacts")
            job, _ = self._enqueue(jobs, "artifact-conflict")
            claimed = jobs.claim("worker-a", now=self.now, lease_seconds=30)
            generation = int(claimed["generation"])
            rights = {"storage": True, "export": False}

            with patch.object(jobs, "succeed", side_effect=RuntimeError("simulated crash")):
                with self.assertRaises(RuntimeError):
                    jobs.publish_result_bytes(
                        job["job_id"],
                        worker_id="worker-a",
                        generation=generation,
                        artifact_store=artifacts,
                        data=b"first-result",
                        media_type="application/octet-stream",
                        rights=rights,
                        now=self.now + timedelta(seconds=1),
                    )

            with self.assertRaises(ValueError):
                jobs.publish_result_bytes(
                    job["job_id"],
                    worker_id="worker-a",
                    generation=generation,
                    artifact_store=artifacts,
                    data=b"different-result",
                    media_type="application/octet-stream",
                    rights=rights,
                    now=self.now + timedelta(seconds=2),
                )
            self.assertEqual(jobs.get(job["job_id"])["state"], "RUNNING")



if __name__ == "__main__":
    unittest.main()
