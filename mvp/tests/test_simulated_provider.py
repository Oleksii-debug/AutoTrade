from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

from mvp.autotrade_mvp.dispatch import GuardedDispatcher, stable_client_order_id
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.simulated_provider import (
    SimulatedProvider,
    SimulatedProviderConflict,
)


class SimulatedProviderTests(unittest.TestCase):
    def test_submission_fill_and_snapshot_are_deterministic(self):
        provider = SimulatedProvider(initial_cash="1000", fee_rate="0.001")
        attempt_id = str(uuid4())
        first = provider.submit_order(
            attempt_id=attempt_id,
            client_order_id="client-1",
            instrument_version="ABC@1",
            side="BUY",
            quantity="2",
            price="100",
            now="2026-09-24T18:00:00Z",
        )
        second = provider.submit_order(
            attempt_id=attempt_id,
            client_order_id="client-1",
            instrument_version="ABC@1",
            side="BUY",
            quantity="2",
            price="100",
            now="2026-09-24T18:00:00Z",
        )
        self.assertEqual(first, second)
        self.assertEqual(len(provider.activity_fills()), 1)
        snapshot = provider.account_snapshot(now="2026-09-24T18:01:00Z")
        self.assertEqual(snapshot["balances"][0]["total"], "799.8")
        self.assertEqual(snapshot["positions"][0]["quantity"]["value"], "2")

    def test_changed_attempt_or_client_identity_conflicts(self):
        provider = SimulatedProvider()
        attempt_id = str(uuid4())
        provider.submit_order(
            attempt_id=attempt_id,
            client_order_id="client-1",
            instrument_version="ABC@1",
            side="BUY",
            quantity="1",
            price="100",
            now="2026-09-24T18:00:00Z",
        )
        with self.assertRaises(SimulatedProviderConflict):
            provider.submit_order(
                attempt_id=attempt_id,
                client_order_id="client-1",
                instrument_version="ABC@1",
                side="BUY",
                quantity="2",
                price="100",
                now="2026-09-24T18:00:00Z",
            )

    def test_query_order_never_claims_absence_with_incomplete_pagination(self):
        provider = SimulatedProvider()
        result = provider.query_order(
            client_order_id="missing",
            coverage_start="2026-09-24T17:00:00Z",
            coverage_end="2026-09-24T19:00:00Z",
            pagination_complete=False,
            now="2026-09-24T19:00:00Z",
        )
        self.assertEqual(result["verdict"], "INCONCLUSIVE")
        complete = provider.query_order(
            client_order_id="missing",
            coverage_start="2026-09-24T17:00:00Z",
            coverage_end="2026-09-24T19:00:00Z",
            pagination_complete=True,
            now="2026-09-24T19:00:00Z",
        )
        self.assertEqual(complete["verdict"], "PROVEN_ABSENT")

    def test_dispatcher_calls_provider_once_after_final_guard(self):
        with TemporaryDirectory() as directory:
            provider = SimulatedProvider()
            dispatcher = GuardedDispatcher(
                JournalStore(f"{directory}/journal.sqlite3"),
                environment="SIMULATION",
                account_id="sim-account",
                owner_token="test-owner",
            )
            checks = 0

            def authority(intent_hash, now):
                nonlocal checks
                checks += 1
                return True, "allowed"

            attempt_id = str(uuid4())
            result = dispatcher.dispatch(
                attempt_id=attempt_id,
                intent_id=str(uuid4()),
                intent_hash="sha256:" + "1" * 64,
                provider="simulated",
                request={
                    "attempt_id": attempt_id,
                    "instrument_version": "ABC@1",
                    "side": "BUY",
                    "quantity": "1",
                    "price": "100",
                    "now": "2026-09-24T18:00:00Z",
                },
                now="2026-09-24T18:00:00Z",
                authority_check=authority,
                transport_send=provider.transport_send,
            )
            self.assertEqual(result.status, "SENT")
            self.assertEqual(provider.outbound_request_count, 1)
            self.assertEqual(checks, 2)

    def test_provider_rejects_binary_float_economics(self):
        provider = SimulatedProvider()
        with self.assertRaises(TypeError):
            provider.submit_order(
                attempt_id=str(uuid4()),
                client_order_id="client-1",
                instrument_version="ABC@1",
                side="BUY",
                quantity=1.0,
                price="100",
                now="2026-09-24T18:00:00Z",
            )


    def test_outage_before_final_guard_is_blocked_without_outbound_send(self):
        with TemporaryDirectory() as directory:
            intent_id = str(uuid4())
            client_order_id = stable_client_order_id(
                "simulated",
                intent_id,
                environment="SIMULATION",
                account_id="sim-account",
            )
            provider = SimulatedProvider(
                transport_faults={client_order_id: "BEFORE_SEND_OUTAGE"}
            )
            dispatcher = GuardedDispatcher(
                JournalStore(f"{directory}/journal.sqlite3"),
                environment="SIMULATION",
                account_id="sim-account",
                owner_token="test-owner",
            )
            result = dispatcher.dispatch(
                attempt_id=str(uuid4()),
                intent_id=intent_id,
                intent_hash="sha256:" + "2" * 64,
                provider="simulated",
                request={
                    "attempt_id": str(uuid4()),
                    "instrument_version": "ABC@1",
                    "side": "BUY",
                    "quantity": "1",
                    "price": "100",
                    "now": "2026-09-24T18:00:00Z",
                },
                now="2026-09-24T18:00:00Z",
                authority_check=lambda *_: (True, "allowed"),
                transport_send=provider.transport_send,
            )
            self.assertEqual(result.status, "BLOCKED")
            self.assertEqual(provider.outbound_request_count, 0)
            self.assertEqual(provider.activity_fills(), ())

    def test_lost_response_after_provider_acceptance_becomes_unknown_and_never_resends(self):
        with TemporaryDirectory() as directory:
            intent_id = str(uuid4())
            attempt_id = str(uuid4())
            client_order_id = stable_client_order_id(
                "simulated",
                intent_id,
                environment="SIMULATION",
                account_id="sim-account",
            )
            provider = SimulatedProvider(
                transport_faults={client_order_id: "AFTER_ACCEPT_RESPONSE_LOST"}
            )
            dispatcher = GuardedDispatcher(
                JournalStore(f"{directory}/journal.sqlite3"),
                environment="SIMULATION",
                account_id="sim-account",
                owner_token="test-owner",
            )
            request = {
                "attempt_id": attempt_id,
                "instrument_version": "ABC@1",
                "side": "BUY",
                "quantity": "1",
                "price": "100",
                "now": "2026-09-24T18:00:00Z",
            }
            result = dispatcher.dispatch(
                attempt_id=attempt_id,
                intent_id=intent_id,
                intent_hash="sha256:" + "3" * 64,
                provider="simulated",
                request=request,
                now="2026-09-24T18:00:00Z",
                authority_check=lambda *_: (True, "allowed"),
                transport_send=provider.transport_send,
            )
            self.assertEqual(result.status, "UNKNOWN")
            self.assertEqual(provider.outbound_request_count, 1)
            self.assertEqual(len(provider.activity_fills()), 1)

            found = provider.query_order(
                client_order_id=client_order_id,
                coverage_start="2026-09-24T17:00:00Z",
                coverage_end="2026-09-24T19:00:00Z",
                pagination_complete=True,
                now="2026-09-24T19:00:00Z",
            )
            self.assertEqual(found["verdict"], "FOUND")
            replay = dispatcher.dispatch(
                attempt_id=attempt_id,
                intent_id=intent_id,
                intent_hash="sha256:" + "3" * 64,
                provider="simulated",
                request=request,
                now="2026-09-24T19:00:00Z",
                authority_check=lambda *_: (True, "allowed"),
                transport_send=provider.transport_send,
            )
            self.assertEqual(replay.status, "UNKNOWN")
            self.assertEqual(provider.outbound_request_count, 1)


if __name__ == "__main__":
    unittest.main()
