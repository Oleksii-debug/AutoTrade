from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.dispatch import GuardedDispatcher
from mvp.autotrade_mvp.durable_order_projection import DurableOrderBookProjection
from mvp.autotrade_mvp.persistence import JournalStore


ORDER_SCOPE = {
    "instrument": "TEST@1",
    "side": "BUY",
    "requested_quantity": "1",
    "quantity_unit": "unit:test:share",
}


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
                    request={"instrument": "TEST@1", "side": "BUY", "quantity": "1"},
                    now="2026-09-27T18:00:00Z",
                    authority_check=lambda _hash, _now: (True, "allowed"),
                    transport_send=transport,
                    sender_check=lambda _owner, _epoch: None,
                    submission_scope=ORDER_SCOPE,
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

    def test_noop_prepare_order_cannot_satisfy_production_proof(self):
        with TemporaryDirectory() as directory:
            store = self._store(directory)
            dispatcher = GuardedDispatcher(
                store,
                environment="PAPER",
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
                attempt_id="paper-noop-prepare",
                intent_id="intent-1",
                intent_hash="intent-hash",
                provider="provider",
                request={"instrument": "TEST@1", "side": "BUY", "quantity": "1"},
                now="2026-09-27T18:00:00Z",
                authority_check=lambda _hash, _now: (True, "allowed"),
                transport_send=transport,
                sender_check=lambda _owner, _epoch: None,
                submission_scope=ORDER_SCOPE,
                prepare_order=lambda *_args: None,
            )
            self.assertEqual(outcome.status, "BLOCKED")
            self.assertEqual(outbound, 0)

    def test_valid_paper_prepare_is_verified_before_transport(self):
        with TemporaryDirectory() as directory:
            store = self._store(directory)
            dispatcher = GuardedDispatcher(
                store,
                environment="PAPER",
                account_id="acct",
                owner_token="owner",
                owner_epoch=1,
            )
            orders = DurableOrderBookProjection(
                store,
                provider_id="provider",
                account_id="acct",
                environment="PAPER",
                host_id="owner",
                owner_epoch="1",
            )
            sequence = []

            def prepare_order(
                client_order_id,
                attempt_id,
                intent_id,
                _provider,
                _request,
                scope,
                prepared_at,
            ):
                sequence.append("prepare")
                orders.create_order(
                    event_key=f"dispatch-order:{attempt_id}",
                    client_order_id=client_order_id,
                    instrument=scope["instrument"],
                    side=scope["side"],
                    requested_quantity=scope["requested_quantity"],
                    quantity_unit=scope["quantity_unit"],
                    origin_intent_id=intent_id,
                    committed_at=prepared_at,
                )

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
                request={"instrument": "TEST@1", "side": "BUY", "quantity": "1"},
                now="2026-09-27T18:00:00Z",
                authority_check=lambda _hash, _now: (True, "allowed"),
                transport_send=transport,
                sender_check=lambda _owner, _epoch: None,
                submission_scope=ORDER_SCOPE,
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
