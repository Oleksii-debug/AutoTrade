from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

from mvp.autotrade_mvp.accounting import (
    book_equity_fill,
    book_external_cash_flow,
)
from mvp.autotrade_mvp.authority import (
    AuthoritativeRiskSnapshot,
    AuthorityPolicy,
    AuthorityService,
)
from mvp.autotrade_mvp.dispatch import GuardedDispatcher
from mvp.autotrade_mvp.durable_order_projection import DurableOrderBookProjection
from mvp.autotrade_mvp.durable_reservations import DurableReservationBook
from mvp.autotrade_mvp.persistence import JournalStore, canonical_json
from mvp.autotrade_mvp.provider_activity_accounting import (
    DurableProviderEconomicBook,
    commit_economic_batch_with_reservation_consumption,
)
from mvp.autotrade_mvp.reconciliation import (
    ProviderFillEvidence,
    ResourceAvailabilityEvidence,
    SnapshotConsistencyEvidence,
    UnknownSubmission,
    reconcile_account,
)
from mvp.autotrade_mvp.reconciliation_journal import (
    record_reconciliation_checkpoint,
)
from mvp.autotrade_mvp.reservations import ReservationBook, ReservationConflict
from mvp.autotrade_mvp.risk import RiskContext, RiskIntent, RiskPolicy
from mvp.autotrade_mvp.simulated_provider import SimulatedProvider
from research.autotrade_research.artifacts.store import ArtifactStore


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
    def resolve_risk(request):
        return AuthoritativeRiskSnapshot(
            context=risk_context(),
            risk_policy=risk_policy(),
            account_id=request.account_id,
            environment=request.environment,
            provider_id=request.provider_id,
            instrument_version=request.instrument_version,
            capability_snapshot_id=request.capability_snapshot_id,
            reconciliation_checkpoint_event_id=request.reconciliation_checkpoint_event_id,
            journal_sequence_cut=request.journal_sequence_cut,
            reservation_version=request.reservation_version,
            reservation_state_digest=request.reservation_state_digest,
            authority_policy_id=request.authority_policy_id,
            authority_policy_version=request.authority_policy_version,
            evaluated_at=request.evaluated_at,
            valid_until="2026-09-24T18:05:00Z",
            evidence_refs={
                dimension: f"test:{dimension.lower()}"
                for dimension in (
                    "PORTFOLIO", "MARKET", "MARGIN", "POLICY",
                    "RECONCILIATION", "CAPABILITY", "BORROW", "STRESS",
                    "FX", "FACTORS", "LIQUIDITY", "LIQUIDATION",
                    "SETTLEMENT", "OPTION_LIFECYCLE", "FUTURES_LIFECYCLE",
                )
            },
        )

    service = AuthorityService(store, risk_authority_resolver=resolve_risk)
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
            journal = JournalStore(f"{directory}/journal.sqlite3")
            economic = DurableProviderEconomicBook(
                journal,
                provider_id="SIMULATED",
                account_id="sim-account",
                environment="SIMULATION",
            )
            economic.append(
                book_external_cash_flow(
                    transaction_id="seed",
                    cause_event_id="deposit",
                    currency="USD",
                    amount="1000",
                )
            )
            bootstrap_outbox = journal.pending_outbox()
            self.assertEqual(
                len(bootstrap_outbox),
                1,
                "durable seed cash flow must publish exactly one bootstrap event",
            )
            journal.mark_outbox_delivered(
                bootstrap_outbox[0]["outbox_id"],
                expected_envelope_hash=bootstrap_outbox[0]["envelope_hash"],
            )

            authority = authority_service(journal)
            artifacts = ArtifactStore(Path(directory) / "artifacts")
            reservations = DurableReservationBook(
                journal,
                environment="SIMULATION",
                account_id="sim-account",
                resolution_artifact_store=artifacts,
            )
            initial_snapshot = provider.account_snapshot(now=NOW)
            availability_result = reconcile_account(
                provider_id="SIMULATED",
                account_id="sim-account",
                environment="SIMULATION",
                local_cash={"USD": "1000"},
                provider_cash={
                    "USD": initial_snapshot["balances"][0]["total"]
                },
                local_positions={},
                provider_positions={},
                local_execution_ids=(),
                provider_fills=(),
                snapshot_consistency=SnapshotConsistencyEvidence(
                    provider_id="SIMULATED",
                    account_id="sim-account",
                    environment="SIMULATION",
                    mode="ATOMIC",
                    query_started_at=NOW,
                    query_completed_at=NOW,
                ),
                coverage_start=NOW,
                coverage_end=NOW,
                pagination_complete=True,
                provider_activity_provider_id="SIMULATED",
                provider_activity_account_id="sim-account",
                resource_availability=ResourceAvailabilityEvidence(
                    provider_id="SIMULATED",
                    account_id="sim-account",
                    environment="SIMULATION",
                    snapshot_id=initial_snapshot["snapshot_id"],
                    query_started_at=NOW,
                    query_completed_at=NOW,
                    valid_until=LATER,
                    available_resources={
                        "CASH:USD": initial_snapshot["balances"][0]["available"]
                    },
                    provider_as_of=initial_snapshot["provider_as_of"],
                    evidence_refs=(
                        f"provider:snapshot:{initial_snapshot['snapshot_id']}",
                    ),
                ),
            )
            availability_checkpoint = record_reconciliation_checkpoint(
                journal,
                reconciliation_id="sim-account:admission-availability",
                result=availability_result,
                observed_at=NOW,
                host_id="sim-host",
                owner_epoch="1",
            )
            for pending in journal.pending_outbox():
                if pending["event_id"] == availability_checkpoint["event_id"]:
                    journal.mark_outbox_delivered(
                        pending["outbox_id"],
                        expected_envelope_hash=pending["envelope_hash"],
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
                reservation_checkpoint_event_id=availability_checkpoint["event_id"],
                reservation_provider_id="SIMULATED",
                reservation_max_age_seconds="60",
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

            orders = DurableOrderBookProjection(
                journal,
                provider_id="SIMULATED",
                account_id="sim-account",
                environment="SIMULATION",
                host_id="sim-host",
                owner_epoch="1",
            )
            orders.create_order(
                event_key="whole-flow-order",
                client_order_id=dispatched.client_order_id,
                instrument=INSTRUMENT,
                side="BUY",
                requested_quantity="2",
                quantity_unit=fill["last_quantity"]["unit"],
                parent_intent_id="intent-1",
                committed_at=NOW,
            )
            orders.sync_submission_attempt(attempt_id=attempt_id)

            fee = fill["fees"][0]
            order_fill = orders.ingest_execution_fill(
                event_key="whole-flow-fill",
                client_order_id=dispatched.client_order_id,
                committed_at=LATER,
                execution_fill={
                    "fill_id": fill["provider_execution_id"],
                    "provider_execution_id": fill["provider_execution_id"],
                    "order_ref": dispatched.client_order_id,
                    "instrument_version": fill["instrument_version"],
                    "side": fill["side"],
                    "last_quantity": fill["last_quantity"],
                    "last_price": fill["last_price"],
                    "trade_time": fill["trade_time"],
                    "receipt_time": LATER,
                    "fees": fill["fees"],
                    "settlement_date": "2026-09-24",
                    "evidence": [],
                },
            )
            self.assertEqual(order_fill.snapshot.state, "FILLED")
            self.assertEqual(order_fill.snapshot.submission_attempt_id, attempt_id)

            commit_economic_batch_with_reservation_consumption(
                economic,
                reservations,
                command_id="fill-financial-commit-1",
                idempotency_key="fill-financial-commit-1",
                reservation_id="reservation-1",
                usage={"CASH:USD": "200.2"},
                transactions=(
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
                    ),
                ),
                committed_at=LATER,
            )
            self.assertEqual(economic.cash("USD"), Decimal("799.8"))
            self.assertEqual(economic.position(INSTRUMENT), Decimal("2"))

            snapshot = provider.account_snapshot(now=LATER)
            provider_fill = ProviderFillEvidence.create(
                provider_id="SIMULATED",
                account_id="sim-account",
                environment="SIMULATION",
                provider_execution_id=fill["provider_execution_id"],
                client_order_id=dispatched.client_order_id,
                instrument=fill["instrument_version"],
                quantity=fill["last_quantity"]["value"],
                price=fill["last_price"],
                fee_amount=fee["amount"],
                fee_currency=fee["currency"],
                trade_time=fill["trade_time"],
            )
            unresolved_submission = UnknownSubmission.create(
                attempt_id=attempt_id,
                intent_id="intent-1",
                provider_id="SIMULATED",
                account_id="sim-account",
                environment="SIMULATION",
                client_order_id=dispatched.client_order_id,
                started_at=NOW,
            )
            reconciled = reconcile_account(
                provider_id="SIMULATED",
                account_id="sim-account",
                environment="SIMULATION",
                local_cash={"USD": economic.cash("USD")},
                provider_cash={"USD": snapshot["balances"][0]["total"]},
                local_positions={INSTRUMENT: economic.position(INSTRUMENT)},
                provider_positions={
                    item["instrument_version"]: item["quantity"]["value"]
                    for item in snapshot["positions"]
                },
                local_execution_ids=[fill["provider_execution_id"]],
                provider_fills=[provider_fill],
                snapshot_consistency=SnapshotConsistencyEvidence(
                    provider_id="SIMULATED",
                    account_id="sim-account",
                    environment="SIMULATION",
                    mode="ATOMIC",
                    query_started_at=LATER,
                    query_completed_at=LATER,
                ),
                unknown_submissions=(unresolved_submission,),
                searched_client_order_ids=(dispatched.client_order_id,),
                coverage_start="2026-09-24T17:00:00Z",
                coverage_end="2026-09-24T19:00:00Z",
                pagination_complete=True,
                provider_activity_provider_id="SIMULATED",
                provider_activity_account_id="sim-account",
            )
            self.assertTrue(reconciled.complete)
            self.assertFalse(reconciled.blocks_new_risk)
            self.assertEqual(
                reconciled.matched_execution_ids,
                (fill["provider_execution_id"],),
            )
            self.assertEqual(reconciled.submission_resolutions[0].outcome, "OBSERVED_EXECUTION")

            checkpoint = record_reconciliation_checkpoint(
                journal,
                reconciliation_id="sim-account:whole-flow",
                result=reconciled,
                observed_at=LATER,
                host_id="sim-host",
                owner_epoch="1",
            )
            resolution_artifact_id = "44444444-4444-4444-8444-444444444444"
            resolution_receipt = {
                "schema_version": 2,
                "evidence_type": "AUTOTRADE_RESERVATION_RESOLUTION",
                "environment": "SIMULATION",
                "account_id": "sim-account",
                "reservation_id": "reservation-1",
                "intent_id": "intent-1",
                "provider": "SIMULATED",
                "attempt_id": attempt_id,
                "outcome": "FILLED",
                "reconciliation_complete": True,
                "reconciliation_event_id": checkpoint["event_id"],
                "reconciliation_payload_hash": checkpoint["payload_hash"],
            }
            resolution_manifest = artifacts.publish_bytes(
                artifact_id=resolution_artifact_id,
                data=canonical_json(resolution_receipt).encode("utf-8"),
                media_type="application/vnd.autotrade.reservation-resolution+json",
                rights={"storage": True, "export": False},
            )
            terminal = reservations.mark_terminal(
                command_id="reservation-terminal-1",
                idempotency_key="reservation-terminal-1",
                reservation_id="reservation-1",
                outcome="FILLED",
                provider="SIMULATED",
                attempt_id=attempt_id,
                resolution_evidence=(
                    f"artifact:{resolution_artifact_id}@{resolution_manifest['sha256']}"
                ),
            )
            self.assertEqual(terminal.state, "FILLED")
            self.assertIsNotNone(terminal.resolution_evidence)
            self.assertEqual(snapshot["open_orders"], [])

            restarted_orders = DurableOrderBookProjection(
                journal,
                provider_id="SIMULATED",
                account_id="sim-account",
                environment="SIMULATION",
                host_id="sim-host-restart",
                owner_epoch="2",
            )
            self.assertEqual(
                restarted_orders.order(dispatched.client_order_id).state,
                "FILLED",
            )
            restarted_reservations = DurableReservationBook(
                journal,
                environment="SIMULATION",
                account_id="sim-account",
                resolution_artifact_store=artifacts,
            )
            self.assertEqual(
                restarted_reservations.get("reservation-1").state,
                "FILLED",
            )
            self.assertEqual(restarted_reservations.total_reserved("CASH:USD"), Decimal("0"))

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
                environment="SIMULATION",
                account_id="sim-account",
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
                restarted._aggregate_id(attempt_id),
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
