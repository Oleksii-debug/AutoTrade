from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
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
