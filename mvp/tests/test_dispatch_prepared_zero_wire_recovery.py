from hashlib import sha256
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.dispatch import (
    GuardedDispatcher,
    stable_client_order_id,
    submission_intent_aggregate_id,
)
from mvp.autotrade_mvp.persistence import JournalStore, canonical_json


class PreparedZeroWireRecoveryTests(unittest.TestCase):
    @staticmethod
    def authority(_intent_hash, _now):
        return True, "allowed"

    def _store(self, directory: str) -> JournalStore:
        return JournalStore(f"{directory}/journal.sqlite3")

    def _dispatcher(self, store: JournalStore) -> GuardedDispatcher:
        return GuardedDispatcher(
            store,
            environment="SIMULATION",
            account_id="acct",
            owner_token="owner",
            prepared_lease_seconds=1,
        )

    def _seed_prepared(
        self,
        dispatcher: GuardedDispatcher,
        *,
        attempt_id: str = "prepared-attempt",
        prepared_at: str = "2026-10-05T08:00:00Z",
    ) -> tuple[dict[str, str], str]:
        request = {"qty": "1"}
        request_hash = "sha256:" + sha256(
            canonical_json(request).encode("utf-8")
        ).hexdigest()
        scope_hash = "sha256:" + sha256(
            canonical_json({}).encode("utf-8")
        ).hexdigest()
        client_order_id = stable_client_order_id(
            "sim",
            "economic-intent-1",
            environment="SIMULATION",
            account_id="acct",
        )
        dispatcher._append(
            attempt_id=attempt_id,
            event_type="SubmissionPrepared",
            version=1,
            payload={
                "attempt_id": attempt_id,
                "intent_id": "economic-intent-1",
                "intent_hash": "ih",
                "provider": "sim",
                "request_hash": request_hash,
                "client_order_id": client_order_id,
                "environment": "SIMULATION",
                "account_id": "acct",
                "owner_token": dispatcher.owner_token,
                "owner_epoch": dispatcher.owner_epoch,
                "prepared_at": prepared_at,
                "submission_scope": {},
                "submission_scope_hash": scope_hash,
            },
            now=prepared_at,
        )
        return request, client_order_id

    def _dispatch(
        self,
        dispatcher: GuardedDispatcher,
        *,
        attempt_id: str,
        request: dict[str, str],
        now: str,
        transport,
    ):
        return dispatcher.dispatch(
            attempt_id=attempt_id,
            intent_id="economic-intent-1",
            intent_hash="ih",
            provider="sim",
            request=request,
            now=now,
            authority_check=self.authority,
            transport_send=transport,
        )

    def test_prepared_before_lease_expiry_remains_in_progress_and_zero_wire(self):
        with TemporaryDirectory() as directory:
            store = self._store(directory)
            dispatcher = self._dispatcher(store)
            request, _ = self._seed_prepared(dispatcher)

            result = self._dispatch(
                dispatcher,
                attempt_id="fresh-attempt",
                request=request,
                now="2026-10-05T08:00:00Z",
                transport=lambda *_args: self.fail(
                    "active prepared lease reached provider transport"
                ),
            )

            self.assertEqual(result.status, "IN_PROGRESS")
            self.assertEqual(result.reason, "prepared_owner_lease_active")
            events = dispatcher._events("prepared-attempt")
            self.assertEqual([event["event_type"] for event in events], ["SubmissionPrepared"])
            self.assertEqual(dispatcher._events("fresh-attempt"), [])

    def test_expired_prepared_is_durable_blocked_not_unknown(self):
        with TemporaryDirectory() as directory:
            store = self._store(directory)
            dispatcher = self._dispatcher(store)
            request, _ = self._seed_prepared(dispatcher)

            result = self._dispatch(
                dispatcher,
                attempt_id="prepared-attempt",
                request=request,
                now="2026-10-05T08:00:02Z",
                transport=lambda *_args: self.fail(
                    "expired prepared attempt reached provider transport"
                ),
            )

            self.assertEqual(result.status, "BLOCKED")
            self.assertEqual(result.reason, "prepared_owner_lease_expired_before_send")
            events = dispatcher._events("prepared-attempt")
            self.assertEqual(
                [event["event_type"] for event in events],
                ["SubmissionPrepared", "SubmissionBlocked"],
            )
            self.assertNotIn("SubmissionUnknown", [event["event_type"] for event in events])
            intent_aggregate = submission_intent_aggregate_id(
                provider="sim",
                environment="SIMULATION",
                account_id="acct",
                intent_id="economic-intent-1",
            )
            self.assertEqual(store.load_events("submission_intent", intent_aggregate), [])

    def test_fresh_attempt_can_send_once_after_zero_wire_prepared_is_fenced(self):
        with TemporaryDirectory() as directory:
            store = self._store(directory)
            dispatcher = self._dispatcher(store)
            request, _ = self._seed_prepared(dispatcher, attempt_id="stale-prepared")
            sends = []

            first_recovery = self._dispatch(
                dispatcher,
                attempt_id="fresh-send",
                request=request,
                now="2026-10-05T08:00:02Z",
                transport=lambda *_args: self.fail(
                    "recovery pass reached provider before stale owner was fenced"
                ),
            )
            self.assertEqual(first_recovery.status, "BLOCKED")
            self.assertEqual(
                first_recovery.reason,
                "prepared_owner_lease_expired_before_send",
            )

            def transport(client_order_id, payload, final_guard):
                final_guard()
                sends.append((client_order_id, dict(payload)))
                return {"provider_order_id": "provider-1"}

            sent = self._dispatch(
                dispatcher,
                attempt_id="fresh-send",
                request=request,
                now="2026-10-05T08:00:03Z",
                transport=transport,
            )
            self.assertEqual(sent.status, "SENT")
            self.assertEqual(len(sends), 1)

            stale_replay = self._dispatch(
                dispatcher,
                attempt_id="stale-prepared",
                request=request,
                now="2026-10-05T08:00:04Z",
                transport=lambda *_args: self.fail(
                    "fenced stale prepared attempt reached provider"
                ),
            )
            self.assertEqual(stale_replay.status, "BLOCKED")
            self.assertEqual(len(sends), 1)

            intent_aggregate = submission_intent_aggregate_id(
                provider="sim",
                environment="SIMULATION",
                account_id="acct",
                intent_id="economic-intent-1",
            )
            bindings = store.load_events("submission_intent", intent_aggregate)
            self.assertEqual([event["event_type"] for event in bindings], ["SubmissionIntentBound"])
            self.assertEqual(bindings[0]["payload"]["attempt_id"], "fresh-send")


if __name__ == "__main__":
    unittest.main()
