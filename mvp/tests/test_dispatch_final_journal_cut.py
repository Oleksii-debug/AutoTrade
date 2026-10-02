import unittest
from tempfile import TemporaryDirectory

from mvp.autotrade_mvp.dispatch import DispatchBlocked, GuardedDispatcher
from mvp.autotrade_mvp.persistence import JournalStore, payload_digest


class SimulatedProcessDeath(BaseException):
    """Abrupt termination outside normal transport exception recovery."""


class DispatchJournalCutTests(unittest.TestCase):
    def test_journal_change_during_final_validation_blocks_before_outbound(self):
        for change_kind in ("revocation", "favorable_resolution"):
            with self.subTest(change_kind=change_kind), TemporaryDirectory() as directory:
                store = JournalStore(f"{directory}/journal.sqlite3")
                dispatcher = GuardedDispatcher(
                    store,
                    environment="SIMULATION",
                    account_id="acct",
                    owner_token="owner",
                )
                authority_calls = 0
                outbound = 0

                def authority(_intent_hash, _now):
                    nonlocal authority_calls
                    authority_calls += 1
                    if authority_calls == 2:
                        payload = {"kind": change_kind}
                        JournalStore.append_event(
                            store,
                            {
                                "event_id": f"authority-change-{change_kind}",
                                "event_type": "AuthorityChanged",
                                "aggregate_type": "authority_oracle",
                                "aggregate_id": change_kind,
                                "aggregate_version": "1",
                                "payload": payload,
                                "payload_hash": payload_digest(payload),
                                "committed_at": "2026-10-01T11:45:00Z",
                            },
                        )
                    return True, "allowed"

                def transport(_client_id, _request, final_guard):
                    nonlocal outbound
                    final_guard()
                    outbound += 1
                    return {"provider_order_id": "must-not-send"}

                result = dispatcher.dispatch(
                    attempt_id=f"journal-cut-{change_kind}",
                    intent_id="intent-1",
                    intent_hash="intent-hash-1",
                    provider="sim",
                    request={"side": "BUY"},
                    now="2026-10-01T11:44:59Z",
                    authority_check=authority,
                    transport_send=transport,
                )
                self.assertEqual(result.status, "BLOCKED")
                self.assertEqual(
                    result.reason,
                    "journal_changed_during_final_send_validation",
                )
                self.assertEqual(outbound, 0)
                events = JournalStore.load_events(
                    store,
                    "submission_attempt",
                    dispatcher._aggregate_id(f"journal-cut-{change_kind}"),
                )
                self.assertEqual(
                    [event["event_type"] for event in events],
                    ["SubmissionPrepared", "SubmissionBlocked"],
                )

    def test_committed_send_barrier_replay_is_not_second_send_permission(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            store = JournalStore(path)
            dispatcher = GuardedDispatcher(
                store,
                environment="SIMULATION",
                account_id="acct",
                owner_token="owner",
            )

            def crash_after_marker(_client_id, _request, final_guard):
                final_guard()
                raise SimulatedProcessDeath("lost reply after durable send marker")

            args = dict(
                attempt_id="barrier-dedupe",
                intent_id="intent-1",
                intent_hash="h1",
                provider="sim",
                request={},
                now="2026-10-01T11:46:00Z",
                authority_check=lambda _hash, _now: (True, "allowed"),
                transport_send=crash_after_marker,
            )
            with self.assertRaises(SimulatedProcessDeath):
                dispatcher.dispatch(**args)

            prepared, sending = JournalStore.load_events(
                store,
                "submission_attempt",
                dispatcher._aggregate_id("barrier-dedupe"),
            )
            before = JournalStore.current_journal_sequence(store)
            with self.assertRaisesRegex(
                DispatchBlocked,
                "send_barrier_already_committed",
            ):
                dispatcher._append(
                    attempt_id="barrier-dedupe",
                    event_type="SubmissionSending",
                    version=2,
                    payload=sending["payload"],
                    now=args["now"],
                    expected_journal_sequence=prepared["journal_sequence"],
                )
            self.assertEqual(JournalStore.current_journal_sequence(store), before)

            restarted = GuardedDispatcher(
                JournalStore(path),
                environment="SIMULATION",
                account_id="acct",
                owner_token="owner-2",
            )
            args["transport_send"] = lambda *_args: self.fail(
                "restart must not resend"
            )
            self.assertEqual(restarted.dispatch(**args).status, "UNKNOWN")


if __name__ == "__main__":
    unittest.main()
