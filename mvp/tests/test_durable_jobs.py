from datetime import datetime, timedelta, timezone
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.durable_jobs import DurableJobStore


BASE = datetime(2026, 9, 25, 0, 0, tzinfo=timezone.utc)


class DurableJobStoreTests(unittest.TestCase):
    def test_enqueue_is_idempotent_but_conflicting_identity_fails_closed(self):
        with TemporaryDirectory() as directory:
            store = DurableJobStore(f"{directory}/jobs.sqlite3")
            self.assertTrue(store.enqueue(job_id="learn", version=1, payload={"dataset": "d1"}))
            self.assertFalse(store.enqueue(job_id="learn", version=1, payload={"dataset": "d1"}))
            with self.assertRaisesRegex(ValueError, "conflicts"):
                store.enqueue(job_id="learn", version=1, payload={"dataset": "d2"})

    def test_claim_restart_and_expired_lease_recover_without_duplicate_acceptance(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/jobs.sqlite3"
            store = DurableJobStore(path)
            store.enqueue(job_id="research", version=1, payload={"candidate": "c1"})
            first = store.claim(owner="worker-a", lease_seconds=10, now=BASE)
            self.assertIsNotNone(first)
            self.assertEqual(first.attempt, 1)

            reopened = DurableJobStore(path)
            second = reopened.claim(owner="worker-b", lease_seconds=10, now=BASE + timedelta(seconds=11))
            self.assertIsNotNone(second)
            self.assertEqual(second.attempt, 2)
            self.assertGreater(second.lease_token, first.lease_token)

            with self.assertRaisesRegex(ValueError, "stale or expired"):
                reopened.complete(first, result={"score": "0.1"}, now=BASE + timedelta(seconds=12))

            self.assertTrue(
                reopened.complete(second, result={"score": "0.2"}, now=BASE + timedelta(seconds=12))
            )
            self.assertFalse(
                reopened.complete(second, result={"score": "0.2"}, now=BASE + timedelta(seconds=12))
            )
            with self.assertRaisesRegex(ValueError, "different accepted result"):
                reopened.complete(second, result={"score": "0.3"}, now=BASE + timedelta(seconds=12))

            state = reopened.get(job_id="research", version=1)
            self.assertEqual(state["status"], "SUCCEEDED")
            self.assertEqual(state["result"], {"score": "0.2"})

    def test_failure_retries_are_bounded_and_terminal_after_budget(self):
        with TemporaryDirectory() as directory:
            store = DurableJobStore(f"{directory}/jobs.sqlite3")
            store.enqueue(job_id="paper", version=3, payload={}, max_attempts=2)

            first = store.claim(owner="w", now=BASE)
            self.assertEqual(
                store.fail(first, error="transient", retryable=True, now=BASE + timedelta(seconds=1)),
                "PENDING",
            )
            second = store.claim(owner="w", now=BASE + timedelta(seconds=2))
            self.assertEqual(second.attempt, 2)
            self.assertEqual(
                store.fail(second, error="still failing", retryable=True, now=BASE + timedelta(seconds=3)),
                "FAILED",
            )
            self.assertIsNone(store.claim(owner="w2", now=BASE + timedelta(seconds=4)))

    def test_heartbeat_requires_current_fencing_token(self):
        with TemporaryDirectory() as directory:
            store = DurableJobStore(f"{directory}/jobs.sqlite3")
            store.enqueue(job_id="memory", version=1, payload={})
            first = store.claim(owner="worker-a", lease_seconds=5, now=BASE)
            second = store.claim(owner="worker-b", lease_seconds=5, now=BASE + timedelta(seconds=6))
            with self.assertRaisesRegex(ValueError, "stale or expired"):
                store.heartbeat(first, now=BASE + timedelta(seconds=7))
            renewed = store.heartbeat(second, lease_seconds=20, now=BASE + timedelta(seconds=7))
            self.assertEqual(renewed.lease_token, second.lease_token)
            self.assertGreater(
                datetime.fromisoformat(renewed.lease_expires_at),
                datetime.fromisoformat(second.lease_expires_at),
            )

    def test_cancel_fences_inflight_worker(self):
        with TemporaryDirectory() as directory:
            store = DurableJobStore(f"{directory}/jobs.sqlite3")
            store.enqueue(job_id="agent", version=2, payload={})
            lease = store.claim(owner="worker", now=BASE)
            self.assertTrue(store.cancel(job_id="agent", version=2))
            with self.assertRaisesRegex(ValueError, "stale or expired"):
                store.complete(lease, result={"ok": True}, now=BASE + timedelta(seconds=1))
            self.assertEqual(store.get(job_id="agent", version=2)["status"], "CANCELLED")

    def test_non_finite_payload_and_result_are_rejected(self):
        with TemporaryDirectory() as directory:
            store = DurableJobStore(f"{directory}/jobs.sqlite3")
            with self.assertRaises(ValueError):
                store.enqueue(job_id="bad", version=1, payload={"x": float("nan")})
            store.enqueue(job_id="ok", version=1, payload={})
            lease = store.claim(owner="worker", now=BASE)
            with self.assertRaises(ValueError):
                store.complete(lease, result={"x": float("inf")}, now=BASE + timedelta(seconds=1))


if __name__ == "__main__":
    unittest.main()
