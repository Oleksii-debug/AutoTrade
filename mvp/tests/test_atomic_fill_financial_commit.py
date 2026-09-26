from datetime import date
from decimal import Decimal
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import NAMESPACE_URL, uuid5

from mvp.autotrade_mvp.accounting import (
    AccountingConflict,
    book_equity_fill,
)
from mvp.autotrade_mvp.durable_reservations import DurableReservationBook
from mvp.autotrade_mvp.durable_settlement import (
    DurableSettlementBook,
    SETTLEMENT_EVIDENCE_MEDIA_TYPE,
    settlement_rule_evidence_metadata,
    settlement_rule_evidence_receipt,
)
from mvp.autotrade_mvp.persistence import (
    JournalStore,
    canonical_json,
    payload_digest,
)
from mvp.autotrade_mvp.fill_accounting import (
    ProjectedFillEvidence,
    build_provider_fill_correction_transactions,
    build_provider_fill_financial_plan,
    build_provider_fill_transaction,
)
from mvp.autotrade_mvp.provider_activity_accounting import (
    DurableProviderEconomicBook,
    PreparedProviderFillBinding,
    _legacy_book_id,
    _prepare_provider_fill_correction_binding,
    _provider_fill_binding_aggregate_id,
    _provider_fill_binding_payload,
    _provider_fill_correction_binding_aggregate_id,
    commit_economic_batch_with_reservation_consumption,
    commit_provider_fill_with_reservation_consumption,
)
from mvp.autotrade_mvp.reconciliation import ProviderFillEvidence
from mvp.autotrade_mvp.reservations import ReservationConflict
from research.autotrade_research.artifacts.store import ArtifactStore

from mvp.autotrade_mvp.settlement import (
    SettlementAccountScope,
    SettlementRuleBinding,
    equity_cash_obligation_from_transaction,
)


PROVIDER = "PROVIDER-A"
ACCOUNT = "acct-1"
ENVIRONMENT = "PAPER"


def reservation_book(store: JournalStore) -> DurableReservationBook:
    return DurableReservationBook(
        store,
        environment=ENVIRONMENT,
        account_id=ACCOUNT,
    )


def economic_book(store: JournalStore) -> DurableProviderEconomicBook:
    return DurableProviderEconomicBook(
        store,
        provider_id=PROVIDER,
        account_id=ACCOUNT,
        environment=ENVIRONMENT,
    )


def artifact_store_for(store: JournalStore) -> ArtifactStore:
    return ArtifactStore(store.path.parent / "settlement-evidence")


def settlement_book(store: JournalStore) -> DurableSettlementBook:
    return DurableSettlementBook(
        store,
        provider_id=PROVIDER,
        account_id=ACCOUNT,
        environment=ENVIRONMENT,
        evidence_artifact_store=artifact_store_for(store),
    )


def settlement_obligation(store: JournalStore, transaction):
    rule = SettlementRuleBinding(
        rule_id="test-equity-cash",
        rule_version="1",
        scope=SettlementAccountScope(
            provider_id=PROVIDER,
            account_id=ACCOUNT,
            environment=ENVIRONMENT,
        ),
        instrument_version="ABC",
        settlement_currency="USD",
        effective_from=date(2026, 9, 1),
        effective_to=None,
        evidence_refs=("instrument:ABC", "rule:test-equity-cash:1"),
    )
    receipt = settlement_rule_evidence_receipt(
        rule,
        trade_date=date(2026, 9, 25),
        expected_settlement_date=date(2026, 9, 26),
    )
    artifact_id = str(
        uuid5(
            NAMESPACE_URL,
            "https://evidence.autotrade.local/atomic-settlement-rule/"
            + canonical_json(receipt),
        )
    )
    manifest = artifact_store_for(store).publish_bytes(
        artifact_id=artifact_id,
        data=canonical_json(receipt).encode("utf-8"),
        media_type=SETTLEMENT_EVIDENCE_MEDIA_TYPE,
        rights={"storage": True, "export": False},
        source_refs=["provider-doc:test-settlement-rule"],
        metadata=settlement_rule_evidence_metadata(
            rule,
            trade_date=date(2026, 9, 25),
            expected_settlement_date=date(2026, 9, 26),
        ),
    )
    rule = replace(
        rule,
        evidence_refs=(
            *rule.evidence_refs,
            f"artifact:{artifact_id}@{manifest['sha256']}",
        ),
    )
    return equity_cash_obligation_from_transaction(
        transaction,
        obligation_id="settlement-economic-fill-1",
        instrument="ABC",
        settlement_currency="USD",
        settlement_date=date(2026, 9, 26),
        rule_binding=rule,
    )


def reserve(book: DurableReservationBook) -> None:
    book.reserve(
        command_id="reserve-command",
        idempotency_key="reserve-idempotency",
        reservation_id="reservation-1",
        intent_id="intent-1",
        requirements={"CASH:USD": "120"},
        available={"CASH:USD": "1000"},
    )


def fill_transaction(*, price: str = "100"):
    return book_equity_fill(
        transaction_id="economic-fill-1",
        cause_event_id="provider-execution-1",
        instrument="ABC",
        settlement_currency="USD",
        side="BUY",
        quantity="1",
        price=price,
        economic_effective_at="2026-09-25T09:00:00Z",
        economic_order_key="provider:PROVIDER-A:execution:provider-execution-1",
        observed_at="2026-09-25T09:00:01Z",
    )


def commit_fill(
    economics: DurableProviderEconomicBook,
    reservations: DurableReservationBook,
    *,
    usage: str = "100",
    transaction=None,
    settlements: DurableSettlementBook | None = None,
):
    economic_transaction = fill_transaction() if transaction is None else transaction
    kwargs = {}
    if settlements is not None:
        kwargs = {
            "settlement_book": settlements,
            "settlement_obligations": (
                settlement_obligation(settlements.store, economic_transaction),
            ),
        }
    return commit_economic_batch_with_reservation_consumption(
        economics,
        reservations,
        command_id="fill-financial-command-1",
        idempotency_key="fill-financial-idempotency-1",
        reservation_id="reservation-1",
        usage={"CASH:USD": usage},
        transactions=(economic_transaction,),
        committed_at="2026-09-25T09:00:02Z",
        **kwargs,
    )


class ProviderFillBindingEnvironmentTests(unittest.TestCase):
    def test_bybit_correction_binding_identity_is_provider_environment_scoped(self):
        testnet_id = _provider_fill_correction_binding_aggregate_id(
            provider_id="BYBIT",
            account_id="bybit-account",
            environment="PAPER",
            provider_environment="TESTNET",
            provider_execution_id="execution-1",
        )
        demo_id = _provider_fill_correction_binding_aggregate_id(
            provider_id="BYBIT",
            account_id="bybit-account",
            environment="PAPER",
            provider_environment="DEMO",
            provider_execution_id="execution-1",
        )
        self.assertNotEqual(testnet_id, demo_id)
        with self.assertRaisesRegex(
            AccountingConflict,
            "explicit provider_environment",
        ):
            _provider_fill_correction_binding_aggregate_id(
                provider_id="BYBIT",
                account_id="bybit-account",
                environment="PAPER",
                provider_execution_id="execution-1",
            )

    def test_bybit_testnet_correction_recovers_exact_initial_binding(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            economics = DurableProviderEconomicBook(
                store,
                provider_id="BYBIT",
                account_id="bybit-account",
                environment="PAPER",
                provider_environment="TESTNET",
            )
            reservations = DurableReservationBook(
                store,
                environment="PAPER",
                account_id="bybit-account",
            )
            reservations.reserve(
                command_id="reserve-bybit-testnet",
                idempotency_key="reserve-bybit-testnet",
                reservation_id="bybit-reservation",
                intent_id="bybit-intent",
                requirements={"CASH:USDT": "150"},
                available={"CASH:USDT": "1000"},
            )
            original_projected = ProjectedFillEvidence.create(
                fill_id="bybit-fill-1",
                provider_execution_id="bybit-execution-1",
                intent_id="bybit-intent",
                client_order_id="bybit-client-1",
                side="BUY",
                quantity="1",
                price="100",
            )
            original_provider = ProviderFillEvidence.create(
                provider_id="BYBIT",
                account_id="bybit-account",
                environment="PAPER",
                provider_environment="TESTNET",
                provider_execution_id="bybit-execution-1",
                client_order_id="bybit-client-1",
                instrument="BTCUSDT",
                quantity="1",
                price="100",
                fee_amount="0",
                fee_currency="USDT",
                trade_time="2026-09-25T09:00:00Z",
                side="BUY",
            )
            self.assertTrue(
                commit_provider_fill_with_reservation_consumption(
                    economics,
                    reservations,
                    command_id="initial-bybit-fill",
                    idempotency_key="initial-bybit-fill",
                    reservation_id="bybit-reservation",
                    projected_fill=original_projected,
                    provider_fill=original_provider,
                    expected_instrument="BTCUSDT",
                    settlement_currency="USDT",
                    committed_at="2026-09-25T09:00:01Z",
                )
            )
            corrected_projected = ProjectedFillEvidence.create(
                fill_id="bybit-fill-1-r2",
                provider_execution_id="bybit-execution-1",
                intent_id="bybit-intent",
                client_order_id="bybit-client-1",
                side="BUY",
                quantity="1",
                price="110",
                provider_revision="r2",
                correction_of="bybit-fill-1",
            )
            corrected_provider = ProviderFillEvidence.create(
                provider_id="BYBIT",
                account_id="bybit-account",
                environment="PAPER",
                provider_environment="TESTNET",
                provider_execution_id="bybit-execution-1",
                client_order_id="bybit-client-1",
                instrument="BTCUSDT",
                quantity="1",
                price="110",
                fee_amount="0",
                fee_currency="USDT",
                trade_time="2026-09-25T09:00:00Z",
                side="BUY",
            )
            _, replacement = build_provider_fill_correction_transactions(
                book=economics,
                provider_id="BYBIT",
                original_projected_fill=original_projected,
                original_provider_fill=original_provider,
                corrected_projected_fill=corrected_projected,
                corrected_provider_fill=corrected_provider,
                expected_instrument="BTCUSDT",
                settlement_currency="USDT",
                correction_observed_at="2026-09-25T09:00:02Z",
            )
            binding = _prepare_provider_fill_correction_binding(
                economics,
                reservations,
                reservation_id="bybit-reservation",
                original_projected_fill=original_projected,
                original_provider_fill=original_provider,
                corrected_projected_fill=corrected_projected,
                corrected_provider_fill=corrected_provider,
                replacement=replacement,
                asset_family="CASH_EQUITY",
                committed_at="2026-09-25T09:00:03Z",
            )
            self.assertEqual(binding.request["provider_environment"], "TESTNET")
            self.assertEqual(
                binding.aggregate_id,
                _provider_fill_correction_binding_aggregate_id(
                    provider_id="BYBIT",
                    account_id="bybit-account",
                    environment="PAPER",
                    provider_environment="TESTNET",
                    provider_execution_id="bybit-execution-1",
                ),
            )
            self.assertNotEqual(
                binding.aggregate_id,
                _provider_fill_correction_binding_aggregate_id(
                    provider_id="BYBIT",
                    account_id="bybit-account",
                    environment="PAPER",
                    provider_environment="DEMO",
                    provider_execution_id="bybit-execution-1",
                ),
            )

    def test_bybit_binding_preserves_exact_provider_environment(self):
        testnet_fill = ProviderFillEvidence.create(
            provider_id="BYBIT",
            account_id="bybit-account",
            environment="PAPER",
            provider_environment="TESTNET",
            provider_execution_id="execution-1",
            client_order_id="client-1",
            instrument="BTCUSDT",
            quantity="1",
            price="100",
            fee_currency="USDT",
            trade_time="2026-09-25T09:00:00Z",
            side="BUY",
        )
        demo_fill = replace(testnet_fill, provider_environment="DEMO")

        testnet_payload = _provider_fill_binding_payload(testnet_fill)
        self.assertEqual(testnet_payload["provider_environment"], "TESTNET")
        self.assertNotEqual(
            _provider_fill_binding_aggregate_id(
                provider_id="BYBIT",
                account_id="bybit-account",
                environment="PAPER",
                provider_environment="TESTNET",
                provider_execution_id="execution-1",
            ),
            _provider_fill_binding_aggregate_id(
                provider_id="BYBIT",
                account_id="bybit-account",
                environment="PAPER",
                provider_environment="DEMO",
                provider_execution_id="execution-1",
            ),
        )
        self.assertEqual(
            _provider_fill_binding_payload(demo_fill)["provider_environment"],
            "DEMO",
        )

    def test_generic_binding_keeps_runtime_environment_identity(self):
        fill = ProviderFillEvidence.create(
            provider_id="PROVIDER-A",
            account_id=ACCOUNT,
            environment="PAPER",
            provider_execution_id="execution-1",
            client_order_id="client-1",
            instrument="ABC",
            quantity="1",
            price="100",
            fee_currency="USD",
            trade_time="2026-09-25T09:00:00Z",
            side="BUY",
        )
        self.assertNotIn(
            "provider_environment",
            _provider_fill_binding_payload(fill),
        )
        self.assertEqual(
            _provider_fill_binding_aggregate_id(
                provider_id="PROVIDER-A",
                account_id=ACCOUNT,
                environment="PAPER",
                provider_execution_id="execution-1",
            ),
            _provider_fill_binding_aggregate_id(
                provider_id="PROVIDER-A",
                account_id=ACCOUNT,
                environment="PAPER",
                provider_environment="PAPER",
                provider_execution_id="execution-1",
            ),
        )


    def test_bybit_economic_book_requires_exact_provider_environment(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            with self.assertRaisesRegex(
                AccountingConflict,
                "requires explicit provider_environment",
            ):
                DurableProviderEconomicBook(
                    store,
                    provider_id="BYBIT",
                    account_id="bybit-account",
                    environment="PAPER",
                )

            for runtime_environment, provider_environment in (
                ("PAPER", "MAINNET"),
                ("LIVE", "TESTNET"),
                ("LIVE", "DEMO"),
            ):
                with self.subTest(
                    runtime_environment=runtime_environment,
                    provider_environment=provider_environment,
                ):
                    with self.assertRaisesRegex(
                        AccountingConflict,
                        "does not match runtime environment",
                    ):
                        DurableProviderEconomicBook(
                            store,
                            provider_id="BYBIT",
                            account_id="bybit-account",
                            environment=runtime_environment,
                            provider_environment=provider_environment,
                        )

            live_book = DurableProviderEconomicBook(
                store,
                provider_id="BYBIT",
                account_id="bybit-live",
                environment="LIVE",
                provider_environment="MAINNET",
            )
            self.assertEqual(live_book.provider_environment, "MAINNET")

    def test_atomic_barrier_rejects_cross_provider_environment_binding(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            economics = DurableProviderEconomicBook(
                store,
                provider_id="BYBIT",
                account_id="bybit-account",
                environment="PAPER",
                provider_environment="TESTNET",
            )
            reservations = DurableReservationBook(
                store,
                environment="PAPER",
                account_id="bybit-account",
            )
            binding = PreparedProviderFillBinding(
                aggregate_id="demo-binding",
                envelope=None,
                request={
                    "provider_id": "BYBIT",
                    "account_id": "bybit-account",
                    "environment": "PAPER",
                    "provider_environment": "DEMO",
                    "reservation_id": "reservation-1",
                },
                result={},
                aggregate_version=1,
            )

            with self.assertRaisesRegex(
                AccountingConflict,
                "provider fill financial binding scope does not match atomic fill",
            ):
                commit_economic_batch_with_reservation_consumption(
                    economics,
                    reservations,
                    command_id="cross-provider-environment",
                    idempotency_key="cross-provider-environment",
                    reservation_id="reservation-1",
                    usage={},
                    transactions=(fill_transaction(),),
                    provider_fill_binding=binding,
                )

            self.assertEqual(economics.transactions, ())
            self.assertEqual(
                store.load_events("economic_book", economics.book_id),
                [],
            )

    def test_bybit_testnet_and_demo_have_distinct_economic_truth(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            testnet_book = DurableProviderEconomicBook(
                store,
                provider_id="BYBIT",
                account_id="bybit-account",
                environment="PAPER",
                provider_environment="TESTNET",
            )
            demo_book = DurableProviderEconomicBook(
                store,
                provider_id="BYBIT",
                account_id="bybit-account",
                environment="PAPER",
                provider_environment="DEMO",
            )
            projected = ProjectedFillEvidence.create(
                fill_id="fill-1",
                provider_execution_id="execution-1",
                intent_id="intent-1",
                client_order_id="client-1",
                side="BUY",
                quantity="1",
                price="100",
            )
            testnet_fill = ProviderFillEvidence.create(
                provider_id="BYBIT",
                account_id="bybit-account",
                environment="PAPER",
                provider_environment="TESTNET",
                provider_execution_id="execution-1",
                client_order_id="client-1",
                instrument="BTCUSDT",
                quantity="1",
                price="100",
                fee_currency="USDT",
                trade_time="2026-09-25T09:00:00Z",
                side="BUY",
            )
            demo_fill = replace(testnet_fill, provider_environment="DEMO")

            testnet_transaction = build_provider_fill_transaction(
                book=testnet_book,
                provider_id="BYBIT",
                projected_fill=projected,
                provider_fill=testnet_fill,
                expected_instrument="BTCUSDT",
                settlement_currency="USDT",
                observed_at="2026-09-25T09:00:01Z",
            )
            demo_transaction = build_provider_fill_transaction(
                book=demo_book,
                provider_id="BYBIT",
                projected_fill=projected,
                provider_fill=demo_fill,
                expected_instrument="BTCUSDT",
                settlement_currency="USDT",
                observed_at="2026-09-25T09:00:01Z",
            )

            self.assertNotEqual(testnet_book.book_id, demo_book.book_id)
            self.assertNotEqual(
                testnet_transaction.transaction_id,
                demo_transaction.transaction_id,
            )
            self.assertNotEqual(
                testnet_transaction.cause_event_id,
                demo_transaction.cause_event_id,
            )
            self.assertNotEqual(
                testnet_transaction.economic_order_key,
                demo_transaction.economic_order_key,
            )

            testnet_plan = testnet_book.prepare_batch_mutation(
                (testnet_transaction,),
                committed_at="2026-09-25T09:00:02Z",
            )
            demo_plan = demo_book.prepare_batch_mutation(
                (demo_transaction,),
                committed_at="2026-09-25T09:00:02Z",
            )
            self.assertNotEqual(testnet_plan.batch_digest, demo_plan.batch_digest)
            self.assertNotEqual(
                testnet_plan.envelope["event_id"],
                demo_plan.envelope["event_id"],
            )
            self.assertEqual(
                testnet_plan.request["provider_environment"], "TESTNET"
            )
            self.assertEqual(demo_plan.request["provider_environment"], "DEMO")

            self.assertTrue(testnet_book.append(testnet_transaction))
            self.assertTrue(demo_book.append(demo_transaction))
            self.assertFalse(testnet_book.append(testnet_transaction))
            self.assertFalse(demo_book.append(demo_transaction))

    def test_legacy_bybit_paper_economic_state_fails_closed_before_rekey(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            legacy_book_id = _legacy_book_id(
                provider_id="BYBIT",
                account_id="bybit-account",
                environment="PAPER",
            )
            legacy_payload = {"legacy_scope": "BYBIT/PAPER"}
            store.append_event(
                {
                    "event_id": "legacy-bybit-economic-event",
                    "event_type": "EconomicTransactionBatchBooked",
                    "aggregate_type": "economic_book",
                    "aggregate_id": legacy_book_id,
                    "aggregate_version": "1",
                    "payload": legacy_payload,
                    "payload_hash": payload_digest(legacy_payload),
                    "committed_at": "2026-09-25T08:59:59Z",
                }
            )

            for provider_environment in ("TESTNET", "DEMO"):
                with self.subTest(provider_environment=provider_environment):
                    with self.assertRaisesRegex(
                        AccountingConflict,
                        "legacy ambiguous provider economic-book state",
                    ):
                        DurableProviderEconomicBook(
                            store,
                            provider_id="BYBIT",
                            account_id="bybit-account",
                            environment="PAPER",
                            provider_environment=provider_environment,
                        )


class AtomicFillFinancialCommitTests(unittest.TestCase):
    def test_fill_economics_and_reservation_consumption_restart_together(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            reservations = reservation_book(store)
            economics = economic_book(store)
            reserve(reservations)

            self.assertTrue(commit_fill(economics, reservations))
            snapshot = reservations.get("reservation-1")
            self.assertEqual(snapshot.consumed["CASH:USD"], Decimal("100"))
            self.assertEqual(snapshot.remaining["CASH:USD"], Decimal("20"))
            self.assertEqual(economics.position("ABC"), Decimal("1"))

            reopened_store = JournalStore(path)
            reopened_reservations = reservation_book(reopened_store)
            reopened_economics = economic_book(reopened_store)
            reopened_snapshot = reopened_reservations.get("reservation-1")
            self.assertEqual(
                reopened_snapshot.consumed["CASH:USD"],
                Decimal("100"),
            )
            self.assertEqual(
                reopened_snapshot.remaining["CASH:USD"],
                Decimal("20"),
            )
            self.assertEqual(reopened_economics.position("ABC"), Decimal("1"))
            self.assertEqual(len(reopened_economics.transactions), 1)

    def test_failure_before_shared_commit_leaves_neither_projection_mutated(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            reservations = reservation_book(store)
            economics = economic_book(store)
            reserve(reservations)

            original_commit = store.commit_command

            def fail_before_commit(**kwargs):
                raise RuntimeError("injected pre-commit failure")

            store.commit_command = fail_before_commit
            try:
                with self.assertRaisesRegex(RuntimeError, "pre-commit"):
                    commit_fill(economics, reservations)
            finally:
                store.commit_command = original_commit

            reopened_store = JournalStore(path)
            reopened_reservations = reservation_book(reopened_store)
            reopened_economics = economic_book(reopened_store)
            snapshot = reopened_reservations.get("reservation-1")
            self.assertEqual(snapshot.consumed["CASH:USD"], Decimal("0"))
            self.assertEqual(snapshot.remaining["CASH:USD"], Decimal("120"))
            self.assertEqual(reopened_economics.transactions, ())

    def test_ack_loss_after_shared_commit_retries_without_duplicate(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            reservations = reservation_book(store)
            economics = economic_book(store)
            reserve(reservations)

            original_commit = store.commit_command
            injected = False

            def lose_ack_after_commit(**kwargs):
                nonlocal injected
                result = original_commit(**kwargs)
                if not injected and result[1]:
                    injected = True
                    raise RuntimeError("injected acknowledgement loss")
                return result

            store.commit_command = lose_ack_after_commit
            try:
                with self.assertRaisesRegex(RuntimeError, "acknowledgement loss"):
                    commit_fill(economics, reservations)
            finally:
                store.commit_command = original_commit

            reopened_store = JournalStore(path)
            reopened_reservations = reservation_book(reopened_store)
            reopened_economics = economic_book(reopened_store)
            self.assertFalse(
                commit_fill(reopened_economics, reopened_reservations)
            )
            snapshot = reopened_reservations.get("reservation-1")
            self.assertEqual(snapshot.consumed["CASH:USD"], Decimal("100"))
            self.assertEqual(snapshot.remaining["CASH:USD"], Decimal("20"))
            self.assertEqual(reopened_economics.position("ABC"), Decimal("1"))
            self.assertEqual(len(reopened_economics.transactions), 1)

    def test_preexisting_economics_without_reservation_consumption_fails_closed(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            reservations = reservation_book(store)
            economics = economic_book(store)
            reserve(reservations)
            self.assertTrue(economics.append(fill_transaction()))

            with self.assertRaisesRegex(
                AccountingConflict,
                "partially committed",
            ):
                commit_fill(economics, reservations)

            snapshot = reservations.get("reservation-1")
            self.assertEqual(snapshot.consumed["CASH:USD"], Decimal("0"))
            self.assertEqual(snapshot.remaining["CASH:USD"], Decimal("120"))
            self.assertEqual(economics.position("ABC"), Decimal("1"))

    def test_exact_retry_cannot_change_reserved_usage(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            reservations = reservation_book(store)
            economics = economic_book(store)
            reserve(reservations)
            self.assertTrue(commit_fill(economics, reservations))

            with self.assertRaisesRegex(
                ReservationConflict,
                "different reservation request",
            ):
                commit_fill(economics, reservations, usage="101")

            snapshot = reservations.get("reservation-1")
            self.assertEqual(snapshot.consumed["CASH:USD"], Decimal("100"))
            self.assertEqual(snapshot.remaining["CASH:USD"], Decimal("20"))
            self.assertEqual(len(economics.transactions), 1)

    def test_preexisting_reservation_consumption_without_economics_fails_closed(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            reservations = reservation_book(store)
            economics = economic_book(store)
            reserve(reservations)

            component_key = (
                "atomic-fill-reservation:"
                + sha256(
                    canonical_json(
                        [
                            PROVIDER,
                            ACCOUNT,
                            ENVIRONMENT,
                            "fill-financial-idempotency-1",
                        ]
                    ).encode("utf-8")
                ).hexdigest()
            )
            # Use the public preparation seam to emulate a legacy/crashed
            # partial integration without mutating the economic aggregate.
            plan = reservations.prepare_consume_mutation(
                event_key="partial-reservation-event",
                idempotency_key=component_key,
                reservation_id="reservation-1",
                usage={"CASH:USD": "100"},
                committed_at="2026-09-25T09:00:02Z",
            )
            self.assertIsNotNone(plan.envelope)
            store.commit_command(
                command_id="partial-reservation-only",
                actor="test-partial-financial-state",
                environment=ENVIRONMENT,
                idempotency_key="partial-reservation-only",
                request=plan.request,
                result=plan.snapshot_payload,
                state_version=plan.aggregate_version,
                events=[(plan.envelope, None)],
            )
            reservations.refresh()

            with self.assertRaisesRegex(
                AccountingConflict,
                "partially committed",
            ):
                commit_fill(economics, reservations)

            snapshot = reservations.get("reservation-1")
            self.assertEqual(snapshot.consumed["CASH:USD"], Decimal("100"))
            self.assertEqual(snapshot.remaining["CASH:USD"], Decimal("20"))
            self.assertEqual(economics.transactions, ())

    def test_fill_settlement_provenance_commits_and_restarts_atomically(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            reservations = reservation_book(store)
            economics = economic_book(store)
            settlements = settlement_book(store)
            reserve(reservations)

            self.assertTrue(
                commit_fill(
                    economics,
                    reservations,
                    settlements=settlements,
                )
            )
            self.assertEqual(len(settlements.obligations), 1)
            self.assertEqual(
                settlements.obligations[0].source_transaction_id,
                "economic-fill-1",
            )

            reopened_store = JournalStore(path)
            reopened_reservations = reservation_book(reopened_store)
            reopened_economics = economic_book(reopened_store)
            reopened_settlements = settlement_book(reopened_store)
            self.assertEqual(
                reopened_reservations.get("reservation-1").consumed["CASH:USD"],
                Decimal("100"),
            )
            self.assertEqual(len(reopened_economics.transactions), 1)
            self.assertEqual(len(reopened_settlements.obligations), 1)
            projected = reopened_settlements.project(reopened_economics)
            self.assertEqual(
                projected.snapshot("USD").unsettled_payable,
                Decimal("100"),
            )

    def test_atomic_fill_ack_loss_does_not_duplicate_settlement_provenance(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            reservations = reservation_book(store)
            economics = economic_book(store)
            settlements = settlement_book(store)
            reserve(reservations)

            original_commit = store.commit_command
            injected = False

            def lose_ack_after_commit(**kwargs):
                nonlocal injected
                result = original_commit(**kwargs)
                if not injected and result[1]:
                    injected = True
                    raise RuntimeError("injected settlement acknowledgement loss")
                return result

            store.commit_command = lose_ack_after_commit
            try:
                with self.assertRaisesRegex(RuntimeError, "acknowledgement loss"):
                    commit_fill(
                        economics,
                        reservations,
                        settlements=settlements,
                    )
            finally:
                store.commit_command = original_commit

            reopened_store = JournalStore(path)
            reopened_reservations = reservation_book(reopened_store)
            reopened_economics = economic_book(reopened_store)
            reopened_settlements = settlement_book(reopened_store)
            self.assertFalse(
                commit_fill(
                    reopened_economics,
                    reopened_reservations,
                    settlements=reopened_settlements,
                )
            )
            self.assertEqual(len(reopened_economics.transactions), 1)
            self.assertEqual(len(reopened_settlements.obligations), 1)
            self.assertEqual(
                reopened_reservations.get("reservation-1").consumed["CASH:USD"],
                Decimal("100"),
            )

    def test_preexisting_settlement_without_fill_pair_fails_closed(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            reservations = reservation_book(store)
            economics = economic_book(store)
            settlements = settlement_book(store)
            reserve(reservations)
            transaction = fill_transaction()
            obligation = settlement_obligation(store, transaction)
            self.assertTrue(
                settlements.register_obligations(
                    (obligation,),
                    command_id="legacy-settlement-only",
                    idempotency_key="legacy-settlement-only",
                    committed_at="2026-09-25T09:00:01Z",
                )
            )

            with self.assertRaisesRegex(
                AccountingConflict,
                "partially committed",
            ):
                commit_fill(
                    economics,
                    reservations,
                    transaction=transaction,
                    settlements=settlements,
                )

            self.assertEqual(economics.transactions, ())
            self.assertEqual(
                reservations.get("reservation-1").consumed["CASH:USD"],
                Decimal("0"),
            )
            self.assertEqual(len(settlements.obligations), 1)

    def test_failure_before_three_way_commit_leaves_settlement_unregistered(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            reservations = reservation_book(store)
            economics = economic_book(store)
            settlements = settlement_book(store)
            reserve(reservations)

            original_commit = store.commit_command

            def fail_before_commit(**kwargs):
                raise RuntimeError("injected three-way pre-commit failure")

            store.commit_command = fail_before_commit
            try:
                with self.assertRaisesRegex(RuntimeError, "pre-commit"):
                    commit_fill(
                        economics,
                        reservations,
                        settlements=settlements,
                    )
            finally:
                store.commit_command = original_commit

            reopened_store = JournalStore(path)
            self.assertEqual(economic_book(reopened_store).transactions, ())
            self.assertEqual(
                reservation_book(reopened_store)
                .get("reservation-1")
                .consumed["CASH:USD"],
                Decimal("0"),
            )
            self.assertEqual(settlement_book(reopened_store).obligations, ())

    def test_settlement_coverage_must_include_every_fill_cash_currency(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            reservations = reservation_book(store)
            economics = economic_book(store)
            settlements = settlement_book(store)
            reserve(reservations)
            transaction = book_equity_fill(
                transaction_id="economic-fill-1",
                cause_event_id="provider-execution-1",
                instrument="ABC",
                settlement_currency="USD",
                side="BUY",
                quantity="1",
                price="100",
                fee="1",
                fee_currency="EUR",
                economic_effective_at="2026-09-25T09:00:00Z",
                economic_order_key="provider:PROVIDER-A:execution:provider-execution-1",
                observed_at="2026-09-25T09:00:01Z",
            )
            with self.assertRaisesRegex(
                AccountingConflict,
                "cover every atomic fill cash leg",
            ):
                commit_fill(
                    economics,
                    reservations,
                    transaction=transaction,
                    settlements=settlements,
                )
            self.assertEqual(economics.transactions, ())
            self.assertEqual(settlements.obligations, ())
            self.assertEqual(
                reservations.get("reservation-1").consumed["CASH:USD"],
                Decimal("0"),
            )

    def test_cross_scope_books_are_rejected_before_mutation(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            reservations = DurableReservationBook(
                store,
                environment=ENVIRONMENT,
                account_id="other-account",
            )
            economics = economic_book(store)
            reservations.reserve(
                command_id="reserve-other",
                idempotency_key="reserve-other",
                reservation_id="reservation-1",
                intent_id="intent-1",
                requirements={"CASH:USD": "120"},
                available={"CASH:USD": "1000"},
            )

            with self.assertRaisesRegex(ValueError, "account/environment"):
                commit_fill(economics, reservations)
            self.assertEqual(economics.transactions, ())
            self.assertEqual(
                reservations.get("reservation-1").remaining["CASH:USD"],
                Decimal("120"),
            )


class EvidenceDerivedFillConsumptionTests(unittest.TestCase):
    def projected_fill(
        self,
        *,
        side="BUY",
        quantity="1",
        price="100",
        fill_id="fill-1",
        provider_execution_id="provider-execution-1",
    ):
        return ProjectedFillEvidence.create(
            fill_id=fill_id,
            provider_execution_id=provider_execution_id,
            intent_id="intent-1",
            client_order_id="client-order-1",
            side=side,
            quantity=quantity,
            price=price,
        )

    def provider_fill(
        self,
        *,
        side="BUY",
        quantity="1",
        price="100",
        fee_amount="0",
        fee_currency="USD",
        position_side=None,
        provider_execution_id="provider-execution-1",
        evidence_refs=("provider-fill:test",),
    ):
        return ProviderFillEvidence.create(
            provider_id=PROVIDER,
            account_id=ACCOUNT,
            environment=ENVIRONMENT,
            provider_execution_id=provider_execution_id,
            client_order_id="client-order-1",
            instrument="ABC",
            quantity=quantity,
            price=price,
            fee_amount=fee_amount,
            fee_currency=fee_currency,
            trade_time="2026-09-25T09:00:00Z",
            side=side,
            position_side=position_side,
            evidence_refs=evidence_refs,
        )

    def commit_evidenced_fill(
        self,
        economics,
        reservations,
        *,
        projected=None,
        provider=None,
        asset_family="CASH_EQUITY",
        command_id="evidence-fill-command-1",
        idempotency_key="evidence-fill-idempotency-1",
    ):
        return commit_provider_fill_with_reservation_consumption(
            economics,
            reservations,
            command_id=command_id,
            idempotency_key=idempotency_key,
            reservation_id="reservation-1",
            projected_fill=(
                self.projected_fill() if projected is None else projected
            ),
            provider_fill=(
                self.provider_fill() if provider is None else provider
            ),
            expected_instrument="ABC",
            settlement_currency="USD",
            asset_family=asset_family,
            observed_at="2026-09-25T09:00:01Z",
            committed_at="2026-09-25T09:00:02Z",
        )

    def test_usage_is_derived_from_provider_fill_not_caller_input(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            reservations = reservation_book(store)
            economics = economic_book(store)
            reserve(reservations)

            self.assertTrue(self.commit_evidenced_fill(economics, reservations))
            snapshot = reservations.get("reservation-1")
            self.assertEqual(snapshot.consumed["CASH:USD"], Decimal("100"))
            self.assertEqual(snapshot.remaining["CASH:USD"], Decimal("20"))
            self.assertEqual(economics.position("ABC"), Decimal("1"))
            bindings = store.load_events_by_aggregate_type(
                "provider_fill_financial_binding"
            )
            self.assertEqual(len(bindings), 1)
            binding = bindings[0]["payload"]["request"]
            self.assertEqual(binding["reservation_id"], "reservation-1")
            self.assertEqual(binding["intent_id"], "intent-1")
            self.assertEqual(
                binding["provider_execution_id"], "provider-execution-1"
            )
            self.assertEqual(binding["fill_id"], "fill-1")
            self.assertEqual(binding["derived_usage"], {"CASH:USD": "100"})
            self.assertTrue(binding["reservation_cut_digest"].startswith("sha256:"))
            self.assertTrue(binding["plan_digest"].startswith("sha256:"))
            self.assertTrue(binding["transaction_digest"].startswith("sha256:"))
            self.assertTrue(binding["projected_fill_digest"].startswith("sha256:"))
            self.assertTrue(binding["provider_fill_digest"].startswith("sha256:"))
            self.assertEqual(
                binding["provider_fill"]["evidence_refs"],
                ["provider-fill:test"],
            )
            self.assertEqual(binding["schema_version"], "1.1.0")
            self.assertEqual(binding["projected_fill"]["position_side"], None)
            self.assertEqual(binding["projected_fill"]["position_effect"], None)
            self.assertEqual(binding["provider_fill"]["side"], "BUY")
            self.assertEqual(binding["provider_fill"]["position_side"], None)
            self.assertEqual(binding["provider_fill"]["position_effect"], None)
            self.assertEqual(binding["provider_fill"]["quantity"], "1")

    def test_provider_evidence_retargeting_conflicts_with_existing_fill_binding(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            reservations = reservation_book(store)
            economics = economic_book(store)
            reserve(reservations)

            self.commit_evidenced_fill(economics, reservations)
            changed_provider = self.provider_fill(
                evidence_refs=("provider-fill:retargeted",),
            )

            with self.assertRaisesRegex(
                AccountingConflict,
                "different financial binding",
            ):
                self.commit_evidenced_fill(
                    economics,
                    reservations,
                    provider=changed_provider,
                    command_id="evidence-fill-command-retarget",
                    idempotency_key="evidence-fill-idempotency-retarget",
                )

            self.assertEqual(len(economics.transactions), 1)
            self.assertEqual(
                len(
                    store.load_events_by_aggregate_type(
                        "provider_fill_financial_binding"
                    )
                ),
                1,
            )
            snapshot = reservations.get("reservation-1")
            self.assertEqual(snapshot.consumed["CASH:USD"], Decimal("100"))
            self.assertEqual(snapshot.remaining["CASH:USD"], Decimal("20"))

    def test_equivalent_decimal_exponents_share_reservation_cut_identity(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            reservations = reservation_book(store)
            economics = economic_book(store)
            reserve(reservations)

            durable_snapshot = reservations.get("reservation-1")
            equivalent_snapshot = replace(
                durable_snapshot,
                original={"CASH:USD": Decimal("120.0")},
                remaining={"CASH:USD": Decimal("120.00")},
                consumed={"CASH:USD": Decimal("0.000")},
            )
            projected = self.projected_fill(
                quantity="0.5",
                fill_id="fill-equivalent-cut",
                provider_execution_id="provider-execution-equivalent-cut",
            )
            provider = self.provider_fill(
                quantity="0.5",
                provider_execution_id="provider-execution-equivalent-cut",
            )
            plan = build_provider_fill_financial_plan(
                book=economics,
                provider_id=PROVIDER,
                projected_fill=projected,
                provider_fill=provider,
                expected_instrument="ABC",
                settlement_currency="USD",
                reservation_snapshot=equivalent_snapshot,
                observed_at="2026-09-25T09:00:01Z",
            )

            prepared = reservations.prepare_consume_mutation(
                event_key="equivalent-cut-event",
                idempotency_key="equivalent-cut-idempotency",
                reservation_id="reservation-1",
                usage=plan.usage,
                committed_at="2026-09-25T09:00:02Z",
                expected_snapshot_digest=plan.reservation_cut_digest,
            )
            self.assertFalse(prepared.already_committed)
            self.assertEqual(
                prepared.snapshot.consumed["CASH:USD"],
                Decimal("50"),
            )
            self.assertEqual(
                prepared.snapshot.remaining["CASH:USD"],
                Decimal("70"),
            )

    def test_stale_financial_plan_is_fenced_before_atomic_mutation(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            reservations = reservation_book(store)
            economics = economic_book(store)
            reserve(reservations)

            projected = self.projected_fill(
                quantity="0.5",
                fill_id="fill-stale",
                provider_execution_id="provider-execution-stale",
            )
            provider = self.provider_fill(
                quantity="0.5",
                provider_execution_id="provider-execution-stale",
            )
            plan = build_provider_fill_financial_plan(
                book=economics,
                provider_id=PROVIDER,
                projected_fill=projected,
                provider_fill=provider,
                expected_instrument="ABC",
                settlement_currency="USD",
                reservation_snapshot=reservations.get("reservation-1"),
                observed_at="2026-09-25T09:00:01Z",
            )

            reservations.consume(
                command_id="competing-consume-command",
                idempotency_key="competing-consume-idempotency",
                reservation_id="reservation-1",
                usage={"CASH:USD": "1"},
            )
            with self.assertRaisesRegex(
                ReservationConflict,
                "snapshot changed after provider fill plan derivation",
            ):
                commit_economic_batch_with_reservation_consumption(
                    economics,
                    reservations,
                    command_id="stale-plan-command",
                    idempotency_key="stale-plan-idempotency",
                    reservation_id="reservation-1",
                    usage=plan.usage,
                    transactions=(plan.transaction,),
                    reservation_expected_snapshot_digest=plan.reservation_cut_digest,
                    committed_at="2026-09-25T09:00:02Z",
                )

            self.assertEqual(economics.transactions, ())
            snapshot = reservations.get("reservation-1")
            self.assertEqual(snapshot.consumed["CASH:USD"], Decimal("1"))
            self.assertEqual(snapshot.remaining["CASH:USD"], Decimal("119"))

    def test_binding_is_not_left_behind_when_atomic_commit_fails(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            reservations = reservation_book(store)
            economics = economic_book(store)
            reserve(reservations)

            original_commit = store.commit_command

            def fail_before_commit(**kwargs):
                raise RuntimeError("injected provider-fill binding failure")

            store.commit_command = fail_before_commit
            try:
                with self.assertRaisesRegex(
                    RuntimeError, "provider-fill binding failure"
                ):
                    self.commit_evidenced_fill(economics, reservations)
            finally:
                store.commit_command = original_commit

            self.assertEqual(
                store.load_events_by_aggregate_type(
                    "provider_fill_financial_binding"
                ),
                [],
            )
            self.assertEqual(economics.transactions, ())
            snapshot = reservations.get("reservation-1")
            self.assertEqual(snapshot.consumed["CASH:USD"], Decimal("0"))
            self.assertEqual(snapshot.remaining["CASH:USD"], Decimal("120"))

    def test_two_partial_fills_accumulate_exact_reservation_consumption(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            reservations = reservation_book(store)
            economics = economic_book(store)
            reserve(reservations)

            first_projected = self.projected_fill(
                quantity="0.4",
                fill_id="fill-partial-1",
                provider_execution_id="provider-execution-partial-1",
            )
            first_provider = self.provider_fill(
                quantity="0.4",
                provider_execution_id="provider-execution-partial-1",
            )
            second_projected = self.projected_fill(
                quantity="0.6",
                fill_id="fill-partial-2",
                provider_execution_id="provider-execution-partial-2",
            )
            second_provider = self.provider_fill(
                quantity="0.6",
                provider_execution_id="provider-execution-partial-2",
            )

            self.assertTrue(
                self.commit_evidenced_fill(
                    economics,
                    reservations,
                    projected=first_projected,
                    provider=first_provider,
                    command_id="partial-command-1",
                    idempotency_key="partial-idempotency-1",
                )
            )
            self.assertTrue(
                self.commit_evidenced_fill(
                    economics,
                    reservations,
                    projected=second_projected,
                    provider=second_provider,
                    command_id="partial-command-2",
                    idempotency_key="partial-idempotency-2",
                )
            )

            snapshot = reservations.get("reservation-1")
            self.assertEqual(snapshot.consumed["CASH:USD"], Decimal("100.0"))
            self.assertEqual(snapshot.remaining["CASH:USD"], Decimal("20.0"))
            self.assertEqual(economics.position("ABC"), Decimal("1.0"))
            self.assertEqual(len(economics.transactions), 2)
            bindings = store.load_events_by_aggregate_type(
                "provider_fill_financial_binding"
            )
            self.assertEqual(len(bindings), 2)
            self.assertEqual(
                {
                    event["payload"]["request"]["provider_execution_id"]
                    for event in bindings
                },
                {
                    "provider-execution-partial-1",
                    "provider-execution-partial-2",
                },
            )
            self.assertEqual(
                {
                    event["payload"]["request"]["derived_usage"]["CASH:USD"]
                    for event in bindings
                },
                {"40", "60"},
            )
            self.assertEqual(
                len({event["aggregate_id"] for event in bindings}),
                2,
            )

    def test_fill_cannot_consume_beyond_admitted_reservation(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            reservations = reservation_book(store)
            economics = economic_book(store)
            reserve(reservations)

            with self.assertRaisesRegex(
                AccountingConflict,
                "exceeds admitted reservation",
            ):
                self.commit_evidenced_fill(
                    economics,
                    reservations,
                    projected=self.projected_fill(quantity="2"),
                    provider=self.provider_fill(quantity="2"),
                )

            self.assertEqual(economics.transactions, ())
            snapshot = reservations.get("reservation-1")
            self.assertEqual(snapshot.consumed["CASH:USD"], Decimal("0"))
            self.assertEqual(snapshot.remaining["CASH:USD"], Decimal("120"))

    def test_positive_settlement_fee_consumes_same_reserved_cash(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            reservations = reservation_book(store)
            economics = economic_book(store)
            reserve(reservations)

            self.assertTrue(
                self.commit_evidenced_fill(
                    economics,
                    reservations,
                    provider=self.provider_fill(fee_amount="1"),
                )
            )
            snapshot = reservations.get("reservation-1")
            self.assertEqual(snapshot.consumed["CASH:USD"], Decimal("101"))
            self.assertEqual(snapshot.remaining["CASH:USD"], Decimal("19"))

    def test_third_currency_fee_requires_admitted_resource(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            reservations = reservation_book(store)
            economics = economic_book(store)
            reserve(reservations)

            with self.assertRaisesRegex(
                AccountingConflict,
                "unreserved resource CASH:EUR",
            ):
                self.commit_evidenced_fill(
                    economics,
                    reservations,
                    provider=self.provider_fill(
                        fee_amount="1",
                        fee_currency="EUR",
                    ),
                )
            self.assertEqual(economics.transactions, ())
            self.assertEqual(
                reservations.get("reservation-1").consumed["CASH:USD"],
                Decimal("0"),
            )

    def test_negative_fee_never_releases_or_creates_authority(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            reservations = reservation_book(store)
            economics = economic_book(store)
            reserve(reservations)

            self.assertTrue(
                self.commit_evidenced_fill(
                    economics,
                    reservations,
                    provider=self.provider_fill(fee_amount="-2"),
                )
            )
            snapshot = reservations.get("reservation-1")
            self.assertEqual(snapshot.consumed["CASH:USD"], Decimal("100"))
            self.assertEqual(snapshot.remaining["CASH:USD"], Decimal("20"))

    def test_provider_side_mismatch_fails_before_any_mutation(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            reservations = reservation_book(store)
            economics = economic_book(store)
            reserve(reservations)

            with self.assertRaisesRegex(
                AccountingConflict,
                "side does not match projection",
            ):
                self.commit_evidenced_fill(
                    economics,
                    reservations,
                    provider=self.provider_fill(side="SELL"),
                )
            self.assertEqual(economics.transactions, ())
            self.assertEqual(
                reservations.get("reservation-1").consumed["CASH:USD"],
                Decimal("0"),
            )

    def test_unsupported_sell_and_derivative_mapping_fail_closed(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            reservations = reservation_book(store)
            economics = economic_book(store)
            reserve(reservations)

            with self.assertRaisesRegex(
                AccountingConflict,
                "qualified only for BUY",
            ):
                self.commit_evidenced_fill(
                    economics,
                    reservations,
                    projected=self.projected_fill(side="SELL"),
                    provider=self.provider_fill(side="SELL"),
                )
            with self.assertRaisesRegex(
                AccountingConflict,
                "not qualified for this asset family",
            ):
                self.commit_evidenced_fill(
                    economics,
                    reservations,
                    asset_family="FUTURES",
                )
            self.assertEqual(economics.transactions, ())

    def test_same_caller_idempotency_rejects_changed_fill_plan(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            reservations = reservation_book(store)
            economics = economic_book(store)
            reserve(reservations)

            first_projected = self.projected_fill(
                quantity="0.4",
                fill_id="fill-idempotency-1",
                provider_execution_id="provider-execution-idempotency-1",
            )
            first_provider = self.provider_fill(
                quantity="0.4",
                provider_execution_id="provider-execution-idempotency-1",
            )
            self.assertTrue(
                self.commit_evidenced_fill(
                    economics,
                    reservations,
                    projected=first_projected,
                    provider=first_provider,
                    command_id="idempotency-command-1",
                    idempotency_key="shared-fill-idempotency",
                )
            )

            changed_projected = self.projected_fill(
                quantity="0.5",
                fill_id="fill-idempotency-2",
                provider_execution_id="provider-execution-idempotency-2",
            )
            changed_provider = self.provider_fill(
                quantity="0.5",
                provider_execution_id="provider-execution-idempotency-2",
            )
            with self.assertRaisesRegex(
                ReservationConflict,
                "idempotency_key was already used for a different reservation request",
            ):
                self.commit_evidenced_fill(
                    economics,
                    reservations,
                    projected=changed_projected,
                    provider=changed_provider,
                    command_id="idempotency-command-2",
                    idempotency_key="shared-fill-idempotency",
                )

            snapshot = reservations.get("reservation-1")
            self.assertEqual(snapshot.consumed["CASH:USD"], Decimal("40.0"))
            self.assertEqual(snapshot.remaining["CASH:USD"], Decimal("80.0"))
            self.assertEqual(economics.position("ABC"), Decimal("0.4"))
            self.assertEqual(len(economics.transactions), 1)

    def test_exact_retry_after_restart_is_idempotent_across_both_aggregates(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            reservations = reservation_book(store)
            economics = economic_book(store)
            reserve(reservations)

            self.assertTrue(self.commit_evidenced_fill(economics, reservations))

            reopened = JournalStore(path)
            reopened_reservations = reservation_book(reopened)
            reopened_economics = economic_book(reopened)
            self.assertFalse(
                self.commit_evidenced_fill(
                    reopened_economics,
                    reopened_reservations,
                )
            )
            snapshot = reopened_reservations.get("reservation-1")
            self.assertEqual(snapshot.consumed["CASH:USD"], Decimal("100"))
            self.assertEqual(snapshot.remaining["CASH:USD"], Decimal("20"))
            self.assertEqual(len(reopened_economics.transactions), 1)
            bindings = reopened.load_events_by_aggregate_type(
                "provider_fill_financial_binding"
            )
            self.assertEqual(len(bindings), 1)
            self.assertEqual(
                bindings[0]["payload"]["request"]["provider_execution_id"],
                "provider-execution-1",
            )



if __name__ == "__main__":
    unittest.main()
