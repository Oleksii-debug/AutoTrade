from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.dispatch import GuardedDispatcher
from mvp.autotrade_mvp.persistence import JournalStore


class ProductionPrepareGuardTests(unittest.TestCase):
    def _store(self, directory: str) -> JournalStore:
        return JournalStore(f"{directory}/journal.sqlite3")

    def test_paper_and_live_missing_prepare_order_block_before_transport(self):
        for environment in ("PAPER", "LIVE"):
            with self.subTest(environment=environment), TemporaryDirectory() as directory:
                store = self._store(directory)
                dispatcher = GuardedDispatcher(
                    store,
                    environment=environment,
                    account_id="acct",
                    owner_token="owner",
                    owner_epoch=1,
                )
                outbound = 0

                def transport(_client_id, _request, final_guard):
                    nonlocal outbound
                    final_guard()
                    outbound += 1
                    return {"provider_order_id": "must-not-happen"}

                outcome = dispatcher.dispatch(
                    attempt_id=f"{environment.lower()}-missing-prepare",
                    intent_id="intent-1",
                    intent_hash="intent-hash",
                    provider="provider",
                    request={"instrument": "TEST", "side": "BUY", "quantity": "1"},
                    now="2026-09-27T18:00:00Z",
                    authority_check=lambda _hash, _now: (True, "allowed"),
                    transport_send=transport,
                    sender_check=lambda _owner, _epoch: None,
                )

                self.assertEqual(outcome.status, "BLOCKED")
                self.assertEqual(outcome.reason, "durable_order_preparation_failed_before_send")
                self.assertEqual(outbound, 0)
                events = store.load_events(
                    "submission_attempt",
                    dispatcher._aggregate_id(f"{environment.lower()}-missing-prepare"),
                )
                self.assertEqual(
                    [event["event_type"] for event in events],
                    ["SubmissionPrepared", "SubmissionBlocked"],
                )
                self.assertIn(
                    "_MissingDurableOrderPreparation",
                    events[-1]["payload"]["reason"],
                )

    def test_valid_paper_prepare_runs_before_transport(self):
        with TemporaryDirectory() as directory:
            store = self._store(directory)
            dispatcher = GuardedDispatcher(
                store,
                environment="PAPER",
                account_id="acct",
                owner_token="owner",
                owner_epoch=1,
            )
            sequence = []

            def prepare_order(*_args):
                sequence.append("prepare")

            def transport(_client_id, _request, final_guard):
                sequence.append("transport")
                final_guard()
                sequence.append("sent")
                return {"provider_order_id": "p-1"}

            outcome = dispatcher.dispatch(
                attempt_id="paper-valid-prepare",
                intent_id="intent-1",
                intent_hash="intent-hash",
                provider="provider",
                request={"instrument": "TEST", "side": "BUY", "quantity": "1"},
                now="2026-09-27T18:00:00Z",
                authority_check=lambda _hash, _now: (True, "allowed"),
                transport_send=transport,
                sender_check=lambda _owner, _epoch: None,
                prepare_order=prepare_order,
            )

            self.assertEqual(outcome.status, "SENT")
            self.assertEqual(sequence, ["prepare", "transport", "sent"])

    def test_simulation_keeps_prepare_order_optional(self):
        with TemporaryDirectory() as directory:
            store = self._store(directory)
            dispatcher = GuardedDispatcher(
                store,
                environment="SIMULATION",
                account_id="acct",
                owner_token="owner",
            )
            outbound = 0

            def transport(_client_id, _request, final_guard):
                nonlocal outbound
                final_guard()
                outbound += 1
                return {"provider_order_id": "sim-1"}

            outcome = dispatcher.dispatch(
                attempt_id="simulation-optional-prepare",
                intent_id="intent-1",
                intent_hash="intent-hash",
                provider="sim",
                request={},
                now="2026-09-27T18:00:00Z",
                authority_check=lambda _hash, _now: (True, "allowed"),
                transport_send=transport,
            )

            self.assertEqual(outcome.status, "SENT")
            self.assertEqual(outbound, 1)


if __name__ == "__main__":
    unittest.main()
