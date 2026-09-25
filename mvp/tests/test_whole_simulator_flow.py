from decimal import Decimal
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

from mvp.autotrade_mvp.accounting import (
    EconomicBook,
    book_equity_fill,
    book_external_cash_flow,
)
from mvp.autotrade_mvp.authority import AuthorityPolicy, AuthorityService
from mvp.autotrade_mvp.dispatch import GuardedDispatcher
from mvp.autotrade_mvp.durable_reservations import DurableReservationBook
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.reconciliation import ProviderFillEvidence, reconcile_account
from mvp.autotrade_mvp.reservations import ReservationBook
from mvp.autotrade_mvp.risk import RiskContext, RiskIntent, RiskPolicy
from mvp.autotrade_mvp.simulated_provider import SimulatedProvider


NOW = "2026-09-24T18:00:00Z"
LATER = "2026-09-24T18:01:00Z"
INSTRUMENT = "ABC@1"
AUTHORITY_INSTRUMENT_ID = "33333333-3333-4333-8333-333333333333"
CAPABILITY_SNAPSHOT_ID = "sim-capability-snapshot-1"


def risk_policy() -> RiskPolicy:
    return RiskPolicy.create(
        max_abs_position="10",
        max_single_notional="1000",
        max_gross_leverage="2",
        max_net_leverage="2",
        max_daily_loss="500",
        max_drawdown_fraction="0.20",
        max_data_age_seconds="5",
        max_fx_age_seconds="60",
        min_margin_headroom="0.20",
        max_stress_loss="500",
    )


def risk_context() -> RiskContext:
    return RiskContext.create(
        state_version=1,
        equity="1000",
        positions={},
        marks={INSTRUMENT: "100"},
        reserved_position_delta={},
        daily_pnl="0",
        drawdown_fraction="0",
        market_data_age_seconds="1",
        fx_age_seconds={"USD": "1"},
        margin_headroom="1",
        capability_allowed=True,
        borrow_available=True,
        stress_scenarios=({INSTRUMENT: "-0.25"},),
    )


def authority_service(store: JournalStore) -> AuthorityService:
    service = AuthorityService(store)
    service.register_policy(
        AuthorityPolicy.create(
            policy_id="sim-policy",
            account_id="sim-account",
            environments={"SIMULATION"},
            instruments={(AUTHORITY_INSTRUMENT_ID, 1)},
            actions={"ORDER.SUBMIT"},
            max_notional="1000",
            expires_at="2026-09-25T00:00:00Z",
            autonomous=True,
            protection_only=False,
            version=1,
        )
    )
    return service


class WholeSimulatorFlowTests(unittest.TestCase):
    def test_risk_authority_reservation_dispatch_fill_accounting_and_reconciliation(self):
        with TemporaryDirectory() as directory:
            provider = SimulatedProvider(
                account_id="sim-account",
                initial_cash="1000",
                fee_rate="0.001",
            )
            economic = EconomicBook()
            economic.append(
                book_external_cash_flow(
                    transaction_id="seed",
                    cause_event_id="deposit",
                    currency="USD",
                    amount="1000",
                )
            )

            journal = JournalStore(f"{directory}/journal.sqlite3")
            authority = authority_service(journal)
            reservations = DurableReservationBook(
                journal,
                environment="SIMULATION",
                account_id="sim-account",
                resolution_evidence_verifier=lambda _reference: True,
            )
            intent = RiskIntent.create(
                symbol=INSTRUMENT,
                side="BUY",
                quantity="2",
                price="100",
                expected_state_version=1,
            )
            intent_hash = "sha256:" + "a" * 64
            admission = authority.admit(
                command_id="financial-command-1",
                idempotency_key="financial-command-1",
                admission_id="admission-1",
                policy_id="sim-policy",
                intent_id="intent-1",
                intent_hash=intent_hash,
                account_id="sim-account",
                environment="SIMULATION",
                instrument_id=AUTHORITY_INSTRUMENT_ID,
                instrument_version=1,
                action="ORDER.SUBMIT",
                notional="200",
                capability_snapshot_id=CAPABILITY_SNAPSHOT_ID,
                risk_intent=intent,
                risk_context=risk_context(),
                risk_policy=risk_policy(),
                risk_valid_until="2026-09-24T18:05:00Z",
                reservation_book=reservations,
                reservation_id="reservation-1",
                reservation_requirements={"CASH:USD": "200.2"},
                reservation_available={"CASH:USD": "1000"},
                now=NOW,
            )
            self.assertEqual(admission.outcome, "ADMITTED")
            self.assertEqual(
                reservations.total_reserved("CASH:USD"), Decimal("200.2")
            )
            self.assertEqual(len(journal.pending_outbox()), 1)

            def final_authority_check(candidate_hash, current_time):
                return authority.dispatch_allowed(
                    "admission-1",
                    intent_hash=candidate_hash,
                    account_id="sim-account",
                    environment="SIMULATION",
                    instrument_id=AUTHORITY_INSTRUMENT_ID,
                    instrument_version=1,
                    action="ORDER.SUBMIT",
                    now=current_time,
                    capability_snapshot_id=CAPABILITY_SNAPSHOT_ID,
                )

            attempt_id = str(uuid4())
            dispatcher = GuardedDispatcher(
                journal,
                environment="SIMULATION",
                account_id="sim-account",
                owner_token="sim-owner",
            )
            dispatched = dispatcher.dispatch(
                attempt_id=attempt_id,
                intent_id="intent-1",
                intent_hash=intent_hash,
                provider="simulated",
                request={
                    "attempt_id": attempt_id,
                    "instrument_version": INSTRUMENT,
                    "side": "BUY",
                    "quantity": "2",
                    "price": "100",
                    "now": NOW,
                },
                now=NOW,
                authority_check=final_authority_check,
                transport_send=provider.transport_send,
            )
            self.assertEqual(dispatched.status, "SENT")
            self.assertEqual(dispatched.response["outcome"], "ACKNOWLEDGED")
            self.assertEqual(provider.outbound_request_count, 1)

            fill = provider.activity_fills()[0]
            fee = fill["fees"][0]
            reservations.consume(
                command_id="reservation-consume-1",
                idempotency_key="reservation-consume-1",
                reservation_id="reservation-1",
                usage={"CASH:USD": "200.2"},
            )
            terminal = reservations.mark_terminal(
                command_id="reservation-terminal-1",
                idempotency_key="reservation-terminal-1",
                reservation_id="reservation-1",
                outcome="FILLED",
                resolution_evidence=(
                    "artifact:44444444-4444-4444-8444-444444444444@sha256:"
                    + "f" * 64
                ),
            )
            self.assertEqual(terminal.state, "FILLED")
            self.assertEqual(reservations.total_reserved("CASH:USD"), Decimal("0"))

            economic.append(
                book_equity_fill(
                    transaction_id="economic-fill-1",
                    cause_event_id=fill["provider_execution_id"],
                    instrument=fill["instrument_version"],
                    settlement_currency="USD",
                    side=fill["side"],
                    quantity=fill["last_quantity"]["value"],
                    price=fill["last_price"],
                    fee=fee["amount"],
                    fee_currency=fee["currency"],
                )
            )
            self.assertEqual(economic.cash("USD"), Decimal("799.8"))
            self.assertEqual(economic.position(INSTRUMENT), Decimal("2"))

            snapshot = provider.account_snapshot(now=LATER)
            provider_fill = ProviderFillEvidence.create(
                provider_execution_id=fill["provider_execution_id"],
                client_order_id=dispatched.client_order_id,
                instrument=fill["instrument_version"],
                quantity=fill["last_quantity"]["value"],
                price=fill["last_price"],
                fee_amount=fee["amount"],
                fee_currency=fee["currency"],
                trade_time=fill["trade_time"],
            )
            reconciled = reconcile_account(
                local_cash={"USD": economic.cash("USD")},
                provider_cash={"USD": snapshot["balances"][0]["total"]},
                local_positions={INSTRUMENT: economic.position(INSTRUMENT)},
                provider_positions={
                    item["instrument_version"]: item["quantity"]["value"]
                    for item in snapshot["positions"]
                },
                local_execution_ids=[fill["provider_execution_id"]],
                provider_fills=[provider_fill],
                coverage_start="2026-09-24T17:00:00Z",
                coverage_end="2026-09-24T19:00:00Z",
                pagination_complete=True,
            )
            self.assertTrue(reconciled.complete)
            self.assertFalse(reconciled.blocks_new_risk)
            self.assertEqual(reconciled.matched_execution_ids, (fill["provider_execution_id"],))
            self.assertEqual(snapshot["open_orders"], [])

    def test_acknowledgement_without_fill_keeps_reservation_and_working_order_truth(self):
        with TemporaryDirectory() as directory:
            provider = SimulatedProvider(
                account_id="sim-account",
                initial_cash="1000",
                fee_rate="0.001",
            )
            reservations = ReservationBook()
            reservations.reserve(
                reservation_id="reservation-ack",
                intent_id="intent-ack",
                requirements={"CASH:USD": "100.1"},
                available={"CASH:USD": "1000"},
            )
            attempt_id = str(uuid4())
            dispatcher = GuardedDispatcher(
                JournalStore(f"{directory}/journal.sqlite3"),
                environment="SIMULATION",
                account_id="sim-account",
                owner_token="sim-owner",
            )
            dispatched = dispatcher.dispatch(
                attempt_id=attempt_id,
                intent_id="intent-ack",
                intent_hash="sha256:" + "b" * 64,
                provider="simulated",
                request={
                    "attempt_id": attempt_id,
                    "instrument_version": INSTRUMENT,
                    "side": "BUY",
                    "quantity": "1",
                    "price": "100",
                    "now": NOW,
                    "fill_immediately": False,
                },
                now=NOW,
                authority_check=lambda _intent_hash, _now: (True, "allowed"),
                transport_send=provider.transport_send,
            )

            self.assertEqual(dispatched.status, "SENT")
            self.assertEqual(dispatched.response["outcome"], "ACKNOWLEDGED")
            self.assertEqual(provider.activity_fills(), ())
            self.assertEqual(
                reservations.total_reserved("CASH:USD"),
                Decimal("100.1"),
            )
            snapshot = provider.account_snapshot(now=LATER)
            self.assertEqual(snapshot["balances"][0]["total"], "1000")
            self.assertEqual(snapshot["positions"], [])
            self.assertEqual(len(snapshot["open_orders"]), 1)
            self.assertEqual(
                snapshot["open_orders"][0]["client_order_id"],
                dispatched.client_order_id,
            )


    def test_same_attempt_is_idempotent_across_later_receipt_time(self):
        provider = SimulatedProvider(account_id="sim-account", initial_cash="1000")
        attempt_id = str(uuid4())
        first = provider.submit_order(
            attempt_id=attempt_id,
            client_order_id="stable-retry",
            instrument_version=INSTRUMENT,
            side="BUY",
            quantity="1",
            price="100",
            now=NOW,
            fill_immediately=False,
        )
        second = provider.submit_order(
            attempt_id=attempt_id,
            client_order_id="stable-retry",
            instrument_version=INSTRUMENT,
            side="BUY",
            quantity="1",
            price="100",
            now=LATER,
            fill_immediately=False,
        )

        self.assertEqual(first["provider_order_id"], second["provider_order_id"])
        self.assertEqual(first["attempt_id"], second["attempt_id"])
        self.assertEqual(len(provider.orders), 1)
        self.assertEqual(provider.orders["stable-retry"].submitted_at, NOW)
        self.assertEqual(provider.activity_fills(), ())

    def test_provider_outputs_canonical_utc_timestamps(self):
        provider = SimulatedProvider(account_id="sim-account", initial_cash="1000")
        attempt_id = str(uuid4())
        submitted = provider.submit_order(
            attempt_id=attempt_id,
            client_order_id="timezone-order",
            instrument_version=INSTRUMENT,
            side="BUY",
            quantity="1",
            price="100",
            now="2026-09-24T20:00:00+02:00",
            fill_immediately=False,
        )
        self.assertEqual(submitted["provider_received_at"], NOW)

        snapshot = provider.account_snapshot(now="2026-09-24T20:01:00+02:00")
        self.assertEqual(snapshot["provider_as_of"], LATER)
        self.assertEqual(snapshot["query_started_at"], LATER)
        self.assertEqual(snapshot["balances"][0]["as_of"], LATER)

        queried = provider.query_order(
            client_order_id="timezone-order",
            coverage_start="2026-09-24T19:00:00+02:00",
            coverage_end="2026-09-24T21:00:00+02:00",
            pagination_complete=True,
            now="2026-09-24T20:01:00+02:00",
        )
        self.assertEqual(queried["time_window"]["start"], "2026-09-24T17:00:00Z")
        self.assertEqual(queried["time_window"]["end"], "2026-09-24T19:00:00Z")
        self.assertEqual(queried["consistency_horizon"], "2026-09-24T19:00:00Z")
        self.assertEqual(provider.health(now="2026-09-24T20:01:00+02:00")["as_of"], LATER)

    def test_timeout_after_send_keeps_unknown_reservation_and_never_blindly_retries(self):
        with TemporaryDirectory() as directory:
            provider = SimulatedProvider(
                account_id="sim-account",
                initial_cash="1000",
                fee_rate="0.001",
            )
            reservations = ReservationBook()
            reservations.reserve(
                reservation_id="reservation-unknown",
                intent_id="intent-unknown",
                requirements={"CASH:USD": "100.1"},
                available={"CASH:USD": "1000"},
            )
            attempt_id = str(uuid4())
            dispatcher = GuardedDispatcher(
                JournalStore(f"{directory}/journal.sqlite3"),
                owner_token="sim-owner",
            )

            def timeout_after_send(client_order_id, request, final_guard):
                final_guard()
                provider.outbound_request_count += 1
                provider.submit_order(
                    attempt_id=request["attempt_id"],
                    client_order_id=client_order_id,
                    instrument_version=request["instrument_version"],
                    side=request["side"],
                    quantity=request["quantity"],
                    price=request["price"],
                    now=request["now"],
                    fill_immediately=False,
                )
                raise TimeoutError("simulated timeout after provider acceptance")

            request = {
                "attempt_id": attempt_id,
                "instrument_version": INSTRUMENT,
                "side": "BUY",
                "quantity": "1",
                "price": "100",
                "now": NOW,
                "fill_immediately": False,
            }
            first = dispatcher.dispatch(
                attempt_id=attempt_id,
                intent_id="intent-unknown",
                intent_hash="sha256:" + "c" * 64,
                provider="simulated",
                request=request,
                now=NOW,
                authority_check=lambda _intent_hash, _now: (True, "allowed"),
                transport_send=timeout_after_send,
            )
            self.assertEqual(first.status, "UNKNOWN")
            self.assertEqual(provider.outbound_request_count, 1)
            self.assertEqual(
                reservations.total_reserved("CASH:USD"),
                Decimal("100.1"),
            )
            snapshot = provider.account_snapshot(now=LATER)
            self.assertEqual(len(snapshot["open_orders"]), 1)

            def forbidden_retry(*_args, **_kwargs):
                self.fail("UNKNOWN attempt must not be sent again")

            recovered = dispatcher.dispatch(
                attempt_id=attempt_id,
                intent_id="intent-unknown",
                intent_hash="sha256:" + "c" * 64,
                provider="simulated",
                request=request,
                now=LATER,
                authority_check=lambda _intent_hash, _now: (True, "allowed"),
                transport_send=forbidden_retry,
            )
            self.assertEqual(recovered.status, "UNKNOWN")
            self.assertEqual(provider.outbound_request_count, 1)
            self.assertEqual(
                reservations.total_reserved("CASH:USD"),
                Decimal("100.1"),
            )


    def test_process_death_after_outbound_send_recovers_unknown_without_second_send(self):
        with TemporaryDirectory() as directory:
            journal_path = f"{directory}/journal.sqlite3"
            provider = SimulatedProvider(
                account_id="sim-account",
                initial_cash="1000",
                fee_rate="0.001",
            )
            attempt_id = str(uuid4())
            request = {
                "attempt_id": attempt_id,
                "instrument_version": INSTRUMENT,
                "side": "BUY",
                "quantity": "1",
                "price": "100",
                "now": NOW,
                "fill_immediately": False,
            }
            first_process = GuardedDispatcher(
                JournalStore(journal_path),
                environment="SIMULATION",
                account_id="sim-account",
                owner_token="process-one",
            )

            def crash_after_provider_accepts(client_order_id, outbound, final_guard):
                final_guard()
                provider.outbound_request_count += 1
                provider.submit_order(
                    attempt_id=outbound["attempt_id"],
                    client_order_id=client_order_id,
                    instrument_version=outbound["instrument_version"],
                    side=outbound["side"],
                    quantity=outbound["quantity"],
                    price=outbound["price"],
                    now=outbound["now"],
                    fill_immediately=False,
                )
                raise KeyboardInterrupt("simulated process death after outbound send")

            with self.assertRaises(KeyboardInterrupt):
                first_process.dispatch(
                    attempt_id=attempt_id,
                    intent_id="intent-crash",
                    intent_hash="sha256:" + "d" * 64,
                    provider="simulated",
                    request=request,
                    now=NOW,
                    authority_check=lambda _intent_hash, _now: (True, "allowed"),
                    transport_send=crash_after_provider_accepts,
                )

            self.assertEqual(provider.outbound_request_count, 1)
            self.assertEqual(len(provider.account_snapshot(now=LATER)["open_orders"]), 1)

            restarted = GuardedDispatcher(
                JournalStore(journal_path),
                environment="SIMULATION",
                account_id="sim-account",
                owner_token="process-two",
            )

            def forbidden_retry(*_args, **_kwargs):
                self.fail("restart must reconcile UNKNOWN before any second outbound send")

            recovered = restarted.dispatch(
                attempt_id=attempt_id,
                intent_id="intent-crash",
                intent_hash="sha256:" + "d" * 64,
                provider="simulated",
                request=request,
                now=LATER,
                authority_check=lambda _intent_hash, _now: (True, "allowed"),
                transport_send=forbidden_retry,
            )
            self.assertEqual(recovered.status, "UNKNOWN")
            self.assertEqual(
                recovered.reason,
                "recovered_after_send_barrier_without_terminal_result",
            )
            self.assertEqual(provider.outbound_request_count, 1)

            events = JournalStore(journal_path).load_events(
                "submission_attempt",
                attempt_id,
            )
            self.assertEqual(
                [event["event_type"] for event in events],
                ["SubmissionPrepared", "SubmissionSending", "SubmissionUnknown"],
            )


    def test_stable_client_order_id_dedupes_across_distinct_local_attempts(self):
        provider = SimulatedProvider(account_id="sim-account", initial_cash="1000")
        first_attempt = str(uuid4())
        second_attempt = str(uuid4())

        first = provider.submit_order(
            attempt_id=first_attempt,
            client_order_id="stable-economic-intent",
            instrument_version=INSTRUMENT,
            side="BUY",
            quantity="1",
            price="100",
            now=NOW,
            fill_immediately=False,
        )
        second = provider.submit_order(
            attempt_id=second_attempt,
            client_order_id="stable-economic-intent",
            instrument_version=INSTRUMENT,
            side="BUY",
            quantity="1",
            price="100",
            now=LATER,
            fill_immediately=True,
        )

        self.assertEqual(first["provider_order_id"], second["provider_order_id"])
        self.assertEqual(first["attempt_id"], first_attempt)
        self.assertEqual(second["attempt_id"], second_attempt)
        self.assertEqual(len(provider.orders), 1)
        self.assertEqual(len(provider._attempts), 2)
        self.assertEqual(provider.activity_fills(), ())
        self.assertEqual(provider.cash, Decimal("1000"))

    def test_stable_client_order_id_rejects_changed_economics_across_attempts(self):
        provider = SimulatedProvider(account_id="sim-account", initial_cash="1000")
        provider.submit_order(
            attempt_id=str(uuid4()),
            client_order_id="stable-economic-intent-conflict",
            instrument_version=INSTRUMENT,
            side="BUY",
            quantity="1",
            price="100",
            now=NOW,
            fill_immediately=False,
        )
        with self.assertRaisesRegex(ValueError, "client_order_id"):
            provider.submit_order(
                attempt_id=str(uuid4()),
                client_order_id="stable-economic-intent-conflict",
                instrument_version=INSTRUMENT,
                side="BUY",
                quantity="2",
                price="100",
                now=LATER,
                fill_immediately=False,
            )
        self.assertEqual(len(provider.orders), 1)
        self.assertEqual(provider.activity_fills(), ())


if __name__ == "__main__":
    unittest.main()
