from datetime import datetime, timedelta, timezone
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.durable_jobs import DurableJobStore, JobLease


BASE = datetime(2026, 9, 25, 0, 0, tzinfo=timezone.utc)


def enqueue(store, *, job_id="job", version=1, resource_units=1, max_attempts=3):
    return store.enqueue(
        job_id=job_id,
        version=version,
        dedupe_key=f"dedupe:{job_id}:{version}",
        input_hashes=("sha256:input-a",),
        payload={"candidate": "c1"},
        resource_units=resource_units,
        max_attempts=max_attempts,
    )


def claim(store, *, owner="worker-a", epoch=1, units=10, now=BASE):
    return store.claim(
        owner=owner,
        owner_epoch=epoch,
        max_resource_units=units,
        lease_seconds=10,
        now=now,
    )


class DurableJobStoreTests(unittest.TestCase):
    def test_enqueue_is_idempotent_and_dedupe_identity_is_unique(self):
        with TemporaryDirectory() as directory:
            store = DurableJobStore(f"{directory}/jobs.sqlite3")
            self.assertTrue(enqueue(store))
            self.assertFalse(enqueue(store))
            with self.assertRaisesRegex(ValueError, "dedupe_key"):
                store.enqueue(
                    job_id="other",
                    version=1,
                    dedupe_key="dedupe:job:1",
                    input_hashes=("sha256:input-a",),
                    payload={"candidate": "c1"},
                )

    def test_explicit_queued_claimed_running_lifecycle_and_checkpoint(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/jobs.sqlite3"
            store = DurableJobStore(path)
            enqueue(store)
            self.assertEqual(store.get(job_id="job", version=1)["status"], "QUEUED")
            lease = claim(store)
            self.assertEqual(store.get(job_id="job", version=1)["status"], "CLAIMED")
            store.start(lease, now=BASE + timedelta(seconds=1))
            self.assertEqual(store.get(job_id="job", version=1)["status"], "RUNNING")
            store.checkpoint(
                lease,
                checkpoint={"cursor": 17, "artifact": "sha256:checkpoint"},
                now=BASE + timedelta(seconds=2),
            )
            reopened = DurableJobStore(path)
            self.assertEqual(
                reopened.get(job_id="job", version=1)["checkpoint"],
                {"cursor": 17, "artifact": "sha256:checkpoint"},
            )

    def test_expired_claim_recovers_and_old_owner_epoch_is_fenced(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/jobs.sqlite3"
            store = DurableJobStore(path)
            enqueue(store)
            first = claim(store, owner="worker-a", epoch=4)
            reopened = DurableJobStore(path)
            second = claim(
                reopened,
                owner="worker-b",
                epoch=5,
                now=BASE + timedelta(seconds=11),
            )
            self.assertIsNotNone(second)
            self.assertEqual(second.attempt, 2)
            self.assertGreater(second.lease_token, first.lease_token)
            reopened.start(second, now=BASE + timedelta(seconds=12))
            with self.assertRaisesRegex(ValueError, "stale or expired"):
                reopened.checkpoint(
                    first,
                    checkpoint={"cursor": 99},
                    now=BASE + timedelta(seconds=12),
                )

    def test_result_acceptance_is_single_and_immutable_per_version(self):
        with TemporaryDirectory() as directory:
            store = DurableJobStore(f"{directory}/jobs.sqlite3")
            enqueue(store)
            lease = claim(store)
            store.start(lease, now=BASE + timedelta(seconds=1))
            self.assertTrue(
                store.complete(
                    lease,
                    result={"score": "0.2"},
                    now=BASE + timedelta(seconds=2),
                )
            )
            self.assertFalse(
                store.complete(
                    lease,
                    result={"score": "0.2"},
                    now=BASE + timedelta(seconds=2),
                )
            )
            with self.assertRaisesRegex(ValueError, "different accepted result"):
                store.complete(
                    lease,
                    result={"score": "0.3"},
                    now=BASE + timedelta(seconds=2),
                )

    def test_resource_ceiling_prevents_oversized_claim(self):
        with TemporaryDirectory() as directory:
            store = DurableJobStore(f"{directory}/jobs.sqlite3")
            enqueue(store, resource_units=8)
            self.assertIsNone(claim(store, units=7))
            lease = claim(store, units=8)
            self.assertEqual(lease.resource_units, 8)

    def test_retry_budget_is_durable_and_bounded(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/jobs.sqlite3"
            store = DurableJobStore(path)
            enqueue(store, max_attempts=2)
            first = claim(store)
            self.assertEqual(
                store.fail(
                    first,
                    error="failed before start",
                    retryable=True,
                    now=BASE + timedelta(seconds=1),
                ),
                "QUEUED",
            )
            reopened = DurableJobStore(path)
            second = claim(
                reopened, owner="worker-b", epoch=2, now=BASE + timedelta(seconds=2)
            )
            reopened.start(second, now=BASE + timedelta(seconds=3))
            self.assertEqual(
                reopened.fail(
                    second,
                    error="failed again",
                    retryable=True,
                    now=BASE + timedelta(seconds=4),
                ),
                "FAILED",
            )
            self.assertIsNone(
                claim(reopened, owner="worker-c", epoch=3, now=BASE + timedelta(seconds=5))
            )

    def test_cancel_fences_claimed_worker(self):
        with TemporaryDirectory() as directory:
            store = DurableJobStore(f"{directory}/jobs.sqlite3")
            enqueue(store)
            lease = claim(store)
            self.assertTrue(store.cancel(job_id="job", version=1))
            with self.assertRaisesRegex(ValueError, "stale or expired"):
                store.start(lease, now=BASE + timedelta(seconds=1))
            self.assertEqual(store.get(job_id="job", version=1)["status"], "CANCELLED")

    def test_non_finite_payload_checkpoint_and_result_are_rejected(self):
        with TemporaryDirectory() as directory:
            store = DurableJobStore(f"{directory}/jobs.sqlite3")
            with self.assertRaises(ValueError):
                store.enqueue(
                    job_id="bad",
                    version=1,
                    dedupe_key="bad",
                    input_hashes=("sha256:x",),
                    payload={"x": float("nan")},
                )
            enqueue(store)
            lease = claim(store)
            store.start(lease, now=BASE + timedelta(seconds=1))
            with self.assertRaises(ValueError):
                store.checkpoint(
                    lease,
                    checkpoint={"x": float("inf")},
                    now=BASE + timedelta(seconds=2),
                )
            with self.assertRaises(ValueError):
                store.complete(
                    lease,
                    result={"x": float("inf")},
                    now=BASE + timedelta(seconds=2),
                )

    def test_input_hashes_must_be_explicit_and_unique(self):
        with TemporaryDirectory() as directory:
            store = DurableJobStore(f"{directory}/jobs.sqlite3")
            with self.assertRaisesRegex(ValueError, "non-empty tuple"):
                store.enqueue(
                    job_id="bad",
                    version=1,
                    dedupe_key="bad",
                    input_hashes=(),
                    payload={},
                )
            with self.assertRaisesRegex(ValueError, "duplicates"):
                store.enqueue(
                    job_id="bad",
                    version=1,
                    dedupe_key="bad",
                    input_hashes=("sha256:x", "sha256:x"),
                    payload={},
                )


if __name__ == "__main__":
    unittest.main()
