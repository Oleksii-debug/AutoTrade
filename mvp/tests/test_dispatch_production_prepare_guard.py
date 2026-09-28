from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.dispatch import GuardedDispatcher
from mvp.autotrade_mvp.durable_order_projection import DurableOrderBookProjection
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.tests._durable_dispatch_test_support import durable_order_preparation


ORDER_PREPARATION_BINDING = {
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
                    order_preparation_binding=ORDER_PREPARATION_BINDING,
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

    def test_paper_and_live_submission_scope_cannot_substitute_for_explicit_order_binding(self):
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
                preparation_calls = 0

                def prepare_order(*_args):
                    nonlocal preparation_calls
                    preparation_calls += 1
                    raise AssertionError(
                        "caller preparation must not run without an independent order binding"
                    )

                def transport(_client_id, _request, final_guard):
                    nonlocal outbound
                    final_guard()
                    outbound += 1
                    return {"provider_order_id": "must-not-happen"}

                outcome = dispatcher.dispatch(
                    attempt_id=f"{environment.lower()}-scope-is-not-order-binding",
                    intent_id="intent-1",
                    intent_hash="intent-hash",
                    provider="provider",
                    request={"instrument": "TEST@1", "side": "BUY", "quantity": "1"},
                    now="2026-09-27T18:00:00Z",
                    authority_check=lambda _hash, _now: (True, "allowed"),
                    transport_send=transport,
                    sender_check=lambda _owner, _epoch: None,
                    submission_scope=ORDER_PREPARATION_BINDING,
                    prepare_order=prepare_order,
                )

                self.assertEqual(outcome.status, "BLOCKED")
                self.assertEqual(
                    outcome.reason,
                    "durable_order_preparation_failed_before_send",
                )
                self.assertEqual(preparation_calls, 0)
                self.assertEqual(outbound, 0)
                events = store.load_events(
                    "submission_attempt",
                    dispatcher._aggregate_id(
                        f"{environment.lower()}-scope-is-not-order-binding"
                    ),
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
                order_preparation_binding=ORDER_PREPARATION_BINDING,
                prepare_order=lambda *_args: None,
            )
            self.assertEqual(outcome.status, "BLOCKED")
            self.assertEqual(outbound, 0)

    def test_attempt_key_bound_to_decoy_order_cannot_prove_target_preparation(self):
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
                orders = DurableOrderBookProjection(
                    store,
                    provider_id="provider",
                    account_id="acct",
                    environment=environment,
                    host_id="owner",
                    owner_epoch="1",
                )
                outbound = 0

                def prepare_order(
                    client_order_id,
                    attempt_id,
                    intent_id,
                    _provider,
                    _request,
                    binding,
                    prepared_at,
                ):
                    # The requested order exists, but under a different key.
                    orders.create_order(
                        event_key=f"unrelated:{attempt_id}",
                        client_order_id=client_order_id,
                        instrument=binding["instrument"],
                        side=binding["side"],
                        requested_quantity=binding["requested_quantity"],
                        quantity_unit=binding["quantity_unit"],
                        origin_intent_id=intent_id,
                        committed_at=prepared_at,
                    )
                    # The expected attempt key exists, but owns a different order.
                    orders.create_order(
                        event_key=f"dispatch-order:{attempt_id}",
                        client_order_id=f"decoy-{client_order_id}",
                        instrument=binding["instrument"],
                        side=binding["side"],
                        requested_quantity=binding["requested_quantity"],
                        quantity_unit=binding["quantity_unit"],
                        origin_intent_id=intent_id,
                        committed_at=prepared_at,
                    )

                def transport(_client_id, _request, final_guard):
                    nonlocal outbound
                    final_guard()
                    outbound += 1
                    return {"provider_order_id": "must-not-happen"}

                outcome = dispatcher.dispatch(
                    attempt_id=f"{environment.lower()}-decoy-prepare",
                    intent_id="intent-1",
                    intent_hash="intent-hash",
                    provider="provider",
                    request={"instrument": "TEST@1", "side": "BUY", "quantity": "1"},
                    now="2026-09-27T18:00:00Z",
                    authority_check=lambda _hash, _now: (True, "allowed"),
                    transport_send=transport,
                    sender_check=lambda _owner, _epoch: None,
                    order_preparation_binding=ORDER_PREPARATION_BINDING,
                    prepare_order=prepare_order,
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
                order_preparation_binding,
                prepared_at,
            ):
                sequence.append("prepare")
                orders.create_order(
                    event_key=f"dispatch-order:{attempt_id}",
                    client_order_id=client_order_id,
                    instrument=order_preparation_binding["instrument"],
                    side=order_preparation_binding["side"],
                    requested_quantity=order_preparation_binding["requested_quantity"],
                    quantity_unit=order_preparation_binding["quantity_unit"],
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
                order_preparation_binding=ORDER_PREPARATION_BINDING,
                prepare_order=prepare_order,
            )

            self.assertEqual(outcome.status, "SENT")
            self.assertEqual(sequence, ["prepare", "transport", "sent"])

    def test_pre_send_block_requires_exact_rearm_before_new_attempt_can_send(self):
        with TemporaryDirectory() as directory:
            store = self._store(directory)
            dispatcher = GuardedDispatcher(
                store,
                environment="PAPER",
                account_id="acct",
                owner_token="owner",
                owner_epoch=1,
            )
            prep = durable_order_preparation(
                dispatcher,
                instrument="TEST@1",
                side="BUY",
                quantity="1",
                quantity_unit="unit:test:share",
            )
            authority_calls = 0
            outbound = 0

            def block_at_final(_intent_hash, _now):
                nonlocal authority_calls
                authority_calls += 1
                return (
                    (True, "allowed")
                    if authority_calls == 1
                    else (False, "local-risk-recheck")
                )

            def transport(client_order_id, _request, final_guard):
                nonlocal outbound
                final_guard()
                outbound += 1
                return {
                    "attempt_id": "first-attempt",
                    "client_order_id": client_order_id,
                    "provider_order_id": "must-not-send",
                    "outcome": "ACCEPTED",
                    "evidence": [],
                }

            first = dispatcher.dispatch(
                attempt_id="first-attempt",
                intent_id="intent-1",
                intent_hash="intent-hash",
                provider="provider",
                request={"instrument": "TEST@1", "side": "BUY", "quantity": "1"},
                now="2026-09-27T18:00:00Z",
                authority_check=block_at_final,
                transport_send=transport,
                sender_check=lambda _owner, _epoch: None,
                **prep,
            )
            self.assertEqual(first.status, "BLOCKED")
            self.assertEqual(outbound, 0)

            orders = DurableOrderBookProjection(
                store,
                provider_id="provider",
                account_id="acct",
                environment="PAPER",
                host_id="owner",
                owner_epoch="1",
            )
            orders.sync_submission_attempt(attempt_id="first-attempt")
            self.assertEqual(
                orders.order(first.client_order_id).state,
                "PRE_SEND_ABORTED",
            )

            old_retry = dispatcher.dispatch(
                attempt_id="first-attempt",
                intent_id="intent-1",
                intent_hash="intent-hash",
                provider="provider",
                request={"instrument": "TEST@1", "side": "BUY", "quantity": "1"},
                now="2026-09-27T18:00:01Z",
                authority_check=lambda _hash, _now: (True, "allowed"),
                transport_send=lambda *_args: self.fail("blocked attempt must never resend"),
                sender_check=lambda _owner, _epoch: None,
                **prep,
            )
            self.assertEqual(old_retry.status, "BLOCKED")
            self.assertEqual(outbound, 0)

            no_rearm = dispatcher.dispatch(
                attempt_id="second-without-rearm",
                intent_id="intent-1",
                intent_hash="intent-hash",
                provider="provider",
                request={"instrument": "TEST@1", "side": "BUY", "quantity": "1"},
                now="2026-09-27T18:00:02Z",
                authority_check=lambda _hash, _now: (True, "allowed"),
                transport_send=lambda *_args: self.fail("missing rearm must block before transport"),
                sender_check=lambda _owner, _epoch: None,
                order_preparation_binding=prep["order_preparation_binding"],
                prepare_order=lambda *_args: None,
            )
            self.assertEqual(no_rearm.status, "BLOCKED")
            self.assertEqual(outbound, 0)

            def successful_transport(client_order_id, _request, final_guard):
                nonlocal outbound
                final_guard()
                outbound += 1
                return {
                    "attempt_id": "third-rearmed",
                    "client_order_id": client_order_id,
                    "provider_order_id": "provider-1",
                    "outcome": "ACCEPTED",
                    "evidence": [],
                }

            rearmed = dispatcher.dispatch(
                attempt_id="third-rearmed",
                intent_id="intent-1",
                intent_hash="intent-hash",
                provider="provider",
                request={"instrument": "TEST@1", "side": "BUY", "quantity": "1"},
                now="2026-09-27T18:00:03Z",
                authority_check=lambda _hash, _now: (True, "allowed"),
                transport_send=successful_transport,
                sender_check=lambda _owner, _epoch: None,
                **prep,
            )
            self.assertEqual(rearmed.status, "SENT")
            self.assertEqual(outbound, 1)

            restarted_orders = DurableOrderBookProjection(
                self._store(directory),
                provider_id="provider",
                account_id="acct",
                environment="PAPER",
                host_id="owner-restarted",
                owner_epoch="2",
            )
            restarted_orders.sync_submission_attempt(attempt_id="third-rearmed")
            snapshot = restarted_orders.order(rearmed.client_order_id).snapshot()
            self.assertEqual(snapshot.state, "WORKING")
            self.assertEqual(snapshot.submission_attempt_id, "third-rearmed")

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
