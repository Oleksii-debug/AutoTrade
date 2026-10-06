from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.dispatch import GuardedDispatcher
from mvp.autotrade_mvp.persistence import JournalStore


class PreparedRecoveryRaceTests(unittest.TestCase):
    @staticmethod
    def authority(_intent_hash, _now):
        return True, "allowed"

    def test_expired_recovery_fences_late_original_final_guard_before_wire(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            original = GuardedDispatcher(
                store,
                environment="SIMULATION",
                account_id="acct",
                owner_token="original-owner",
                prepared_lease_seconds=1,
            )
            recovery = GuardedDispatcher(
                store,
                environment="SIMULATION",
                account_id="acct",
                owner_token="recovery-owner",
                prepared_lease_seconds=1,
            )
            wire_sends = []
            recovery_results = []

            def original_transport(client_order_id, _request, final_guard):
                # The first dispatcher has already durably prepared but has not
                # crossed its final send barrier. Simulate another process
                # recovering the exact attempt after the lease expires.
                recovered = recovery.dispatch(
                    attempt_id="racy-attempt",
                    intent_id="economic-intent-race",
                    intent_hash="ih",
                    provider="sim",
                    request={"qty": "1"},
                    now="2026-10-05T08:00:02Z",
                    authority_check=self.authority,
                    transport_send=lambda *_args: self.fail(
                        "recovery of existing Prepared reached provider"
                    ),
                )
                recovery_results.append(recovered)

                # A conforming provider wrapper sends only after this returns.
                # The stale guard must fail because recovery already committed
                # SubmissionBlocked at aggregate version 2.
                final_guard()
                wire_sends.append(client_order_id)
                return {"provider_order_id": "must-not-exist"}

            result = original.dispatch(
                attempt_id="racy-attempt",
                intent_id="economic-intent-race",
                intent_hash="ih",
                provider="sim",
                request={"qty": "1"},
                now="2026-10-05T08:00:00Z",
                authority_check=self.authority,
                transport_send=original_transport,
            )

            self.assertEqual(len(recovery_results), 1)
            self.assertEqual(recovery_results[0].status, "BLOCKED")
            self.assertEqual(
                recovery_results[0].reason,
                "prepared_owner_lease_expired_before_send",
            )
            self.assertEqual(result.status, "BLOCKED")
            self.assertEqual(wire_sends, [])

            events = original._events("racy-attempt")
            self.assertEqual(
                [event["event_type"] for event in events],
                ["SubmissionPrepared", "SubmissionBlocked"],
            )
            self.assertNotIn("SubmissionSending", [event["event_type"] for event in events])
            self.assertNotIn("SubmissionUnknown", [event["event_type"] for event in events])


if __name__ == "__main__":
    unittest.main()
