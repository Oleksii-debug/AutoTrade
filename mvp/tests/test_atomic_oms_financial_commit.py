from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.accounting import AccountingConflict, book_equity_fill
from mvp.autotrade_mvp.durable_order_projection import DurableOrderBookProjection
from mvp.autotrade_mvp.durable_reservations import DurableReservationBook
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.provider_activity_accounting import (
    DurableProviderEconomicBook,
    commit_economic_batch_with_reservation_consumption,
    commit_order_fill_with_reservation_consumption,
)


PROVIDER = "SIMULATED"
ACCOUNT = "atomic-oms-account"
ENVIRONMENT = "SIMULATION"
WHEN = "2026-10-03T19:00:00Z"


def books(store: JournalStore):
    return (
        DurableOrderBookProjection(
            store,
            provider_id=PROVIDER,
            account_id=ACCOUNT,
            environment=ENVIRONMENT,
            host_id="atomic-oms-host",
            owner_epoch="1",
        ),
        DurableProviderEconomicBook(
            store,
            provider_id=PROVIDER,
            account_id=ACCOUNT,
            environment=ENVIRONMENT,
        ),
        DurableReservationBook(
            store,
            environment=ENVIRONMENT,
            account_id=ACCOUNT,
        ),
    )


def seed(reservations: DurableReservationBook, orders: DurableOrderBookProjection):
    reservations.reserve(
        command_id="reserve-1",
        idempotency_key="reserve-1",
        reservation_id="reservation-1",
        intent_id="intent-1",
        requirements={"CASH:USD": "120"},
        available={"CASH:USD": "1000"},
    )
    orders.create_order(
        event_key="create-1",
        client_order_id="order-1",
        instrument="ABC",
        side="BUY",
        requested_quantity="1",
        committed_at=WHEN,
        parent_intent_id="intent-1",
    )


def transaction(*, cause_event_id: str = "provider-execution-1"):
    return book_equity_fill(
        transaction_id="economic-fill-1",
        cause_event_id=cause_event_id,
        instrument="ABC",
        settlement_currency="USD",
        side="BUY",
        quantity="1",
        price="100",
    )


def atomic_fill(
    orders: DurableOrderBookProjection,
    economics: DurableProviderEconomicBook,
    reservations: DurableReservationBook,
):
    return commit_order_fill_with_reservation_consumption(
        orders,
        economics,
        reservations,
        order_event_key="fill-1",
        client_order_id="order-1",
        fill_id="fill-1",
        provider_execution_id="provider-execution-1",
        quantity="1",
        price="100",
        command_id="atomic-oms-fill-1",
        idempotency_key="atomic-oms-fill-1",
        reservation_id="reservation-1",
        usage={"CASH:USD": "100"},
        transactions=(transaction(),),
        committed_at=WHEN,
    )


class AtomicOmsFinancialCommitTests(unittest.TestCase):
    def assert_complete(self, orders, economics, reservations):
        snapshot = orders.order("order-1").snapshot()
        self.assertEqual(snapshot.state, "FILLED")
        self.assertEqual(snapshot.filled_quantity, Decimal("1"))
        self.assertEqual(snapshot.fill_count, 1)
        self.assertEqual(economics.position("ABC"), Decimal("1"))
        self.assertEqual(len(economics.transactions), 1)
        reservation = reservations.get("reservation-1")
        self.assertEqual(reservation.consumed["CASH:USD"], Decimal("100"))
        self.assertEqual(reservation.remaining["CASH:USD"], Decimal("20"))

    def test_restart_replays_one_atomic_effect(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            orders, economics, reservations = books(store)
            seed(reservations, orders)

            self.assertTrue(atomic_fill(orders, economics, reservations))
            self.assert_complete(orders, economics, reservations)

            reopened = JournalStore(path)
            ro, re, rr = books(reopened)
            self.assertFalse(atomic_fill(ro, re, rr))
            self.assert_complete(ro, re, rr)

    def test_precommit_failure_leaves_all_three_unmutated(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            orders, economics, reservations = books(store)
            seed(reservations, orders)
            original = JournalStore.commit_command

            def fail(selected_store, **kwargs):
                if selected_store is store:
                    raise RuntimeError("injected atomic OMS failure")
                return original(selected_store, **kwargs)

            JournalStore.commit_command = fail
            try:
                with self.assertRaisesRegex(RuntimeError, "atomic OMS failure"):
                    atomic_fill(orders, economics, reservations)
            finally:
                JournalStore.commit_command = original

            ro, re, rr = books(JournalStore(path))
            self.assertEqual(ro.order("order-1").snapshot().filled_quantity, Decimal("0"))
            self.assertEqual(re.transactions, ())
            self.assertEqual(rr.get("reservation-1").consumed["CASH:USD"], Decimal("0"))

    def test_ack_loss_retry_is_exactly_once(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            orders, economics, reservations = books(store)
            seed(reservations, orders)
            original = JournalStore.commit_command
            injected = False

            def lose_ack(selected_store, **kwargs):
                nonlocal injected
                result = original(selected_store, **kwargs)
                if selected_store is store and result[1] and not injected:
                    injected = True
                    raise RuntimeError("injected acknowledgement loss")
                return result

            JournalStore.commit_command = lose_ack
            try:
                with self.assertRaisesRegex(RuntimeError, "acknowledgement loss"):
                    atomic_fill(orders, economics, reservations)
            finally:
                JournalStore.commit_command = original

            ro, re, rr = books(JournalStore(path))
            self.assertFalse(atomic_fill(ro, re, rr))
            self.assert_complete(ro, re, rr)

    def test_legacy_oms_only_split_recovers_missing_finance(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            orders, economics, reservations = books(store)
            seed(reservations, orders)
            self.assertTrue(
                orders.record_fill(
                    event_key="fill-1",
                    client_order_id="order-1",
                    fill_id="fill-1",
                    provider_execution_id="provider-execution-1",
                    quantity="1",
                    price="100",
                    committed_at=WHEN,
                ).inserted
            )

            self.assertTrue(atomic_fill(orders, economics, reservations))
            self.assert_complete(orders, economics, reservations)
            self.assertFalse(atomic_fill(orders, economics, reservations))

    def test_finance_only_split_fails_closed(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            orders, economics, reservations = books(store)
            seed(reservations, orders)
            self.assertTrue(
                commit_economic_batch_with_reservation_consumption(
                    economics,
                    reservations,
                    command_id="atomic-oms-fill-1",
                    idempotency_key="atomic-oms-fill-1",
                    reservation_id="reservation-1",
                    usage={"CASH:USD": "100"},
                    transactions=(transaction(),),
                    committed_at=WHEN,
                )
            )

            with self.assertRaisesRegex(
                AccountingConflict,
                "financial fill is committed without the matching OMS fill",
            ):
                atomic_fill(orders, economics, reservations)

    def test_legacy_fully_split_state_requires_recovery_command_authority(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            orders, economics, reservations = books(store)
            seed(reservations, orders)
            self.assertTrue(
                commit_economic_batch_with_reservation_consumption(
                    economics,
                    reservations,
                    command_id="atomic-oms-fill-1",
                    idempotency_key="atomic-oms-fill-1",
                    reservation_id="reservation-1",
                    usage={"CASH:USD": "100"},
                    transactions=(transaction(),),
                    committed_at=WHEN,
                )
            )
            orders.record_fill(
                event_key="fill-1",
                client_order_id="order-1",
                fill_id="fill-1",
                provider_execution_id="provider-execution-1",
                quantity="1",
                price="100",
                committed_at=WHEN,
            )

            with self.assertRaisesRegex(
                AccountingConflict,
                "without one atomic/recovery command authority",
            ):
                atomic_fill(orders, economics, reservations)

    def test_non_oms_financial_retry_keeps_legacy_request_shape(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            orders, economics, reservations = books(store)
            seed(reservations, orders)
            kwargs = dict(
                command_id="finance-only-compatible",
                idempotency_key="finance-only-compatible",
                reservation_id="reservation-1",
                usage={"CASH:USD": "100"},
                transactions=(transaction(),),
                committed_at=WHEN,
            )
            self.assertTrue(
                commit_economic_batch_with_reservation_consumption(
                    economics,
                    reservations,
                    **kwargs,
                )
            )
            self.assertFalse(
                commit_economic_batch_with_reservation_consumption(
                    economics,
                    reservations,
                    **kwargs,
                )
            )

    def test_provider_execution_must_bind_economic_cause(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            orders, economics, reservations = books(store)
            seed(reservations, orders)
            with self.assertRaisesRegex(
                AccountingConflict,
                "provider execution is absent",
            ):
                commit_order_fill_with_reservation_consumption(
                    orders,
                    economics,
                    reservations,
                    order_event_key="fill-1",
                    client_order_id="order-1",
                    fill_id="fill-1",
                    provider_execution_id="provider-execution-1",
                    quantity="1",
                    price="100",
                    command_id="cause-mismatch",
                    idempotency_key="cause-mismatch",
                    reservation_id="reservation-1",
                    usage={"CASH:USD": "100"},
                    transactions=(transaction(cause_event_id="different-execution"),),
                    committed_at=WHEN,
                )
            self.assertEqual(
                orders.order("order-1").snapshot().filled_quantity,
                Decimal("0"),
            )
            self.assertEqual(economics.transactions, ())
            self.assertEqual(
                reservations.get("reservation-1").consumed["CASH:USD"],
                Decimal("0"),
            )

    def test_competing_oms_writer_fences_shared_commit(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            orders, economics, reservations = books(store)
            seed(reservations, orders)
            original_prepare = DurableProviderEconomicBook.prepare_batch_mutation
            injected = False

            def race_order_writer(selected_book, transactions, **kwargs):
                nonlocal injected
                if selected_book is economics and not injected:
                    injected = True
                    orders.request_cancel(
                        event_key="racing-cancel",
                        client_order_id="order-1",
                        command_id="racing-cancel-command",
                        committed_at=WHEN,
                    )
                return original_prepare(selected_book, transactions, **kwargs)

            DurableProviderEconomicBook.prepare_batch_mutation = race_order_writer
            try:
                with self.assertRaisesRegex(ValueError, "aggregate_version"):
                    atomic_fill(orders, economics, reservations)
            finally:
                DurableProviderEconomicBook.prepare_batch_mutation = original_prepare

            ro, re, rr = books(JournalStore(path))
            self.assertTrue(ro.order("order-1").cancel_requested)
            self.assertNotEqual(ro.order("order-1").state, "FILLED")
            self.assertEqual(re.transactions, ())
            self.assertEqual(
                rr.get("reservation-1").consumed["CASH:USD"],
                Decimal("0"),
            )

    def test_order_projection_must_share_physical_journal_generation(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            financial_store = JournalStore(root / "financial.sqlite3")
            order_store = JournalStore(root / "orders.sqlite3")
            reservations = DurableReservationBook(
                financial_store,
                environment=ENVIRONMENT,
                account_id=ACCOUNT,
            )
            economics = DurableProviderEconomicBook(
                financial_store,
                provider_id=PROVIDER,
                account_id=ACCOUNT,
                environment=ENVIRONMENT,
            )
            reservations.reserve(
                command_id="reserve-1",
                idempotency_key="reserve-1",
                reservation_id="reservation-1",
                intent_id="intent-1",
                requirements={"CASH:USD": "120"},
                available={"CASH:USD": "1000"},
            )
            orders = DurableOrderBookProjection(
                order_store,
                provider_id=PROVIDER,
                account_id=ACCOUNT,
                environment=ENVIRONMENT,
                host_id="atomic-oms-host",
                owner_epoch="1",
            )
            orders.create_order(
                event_key="create-1",
                client_order_id="order-1",
                instrument="ABC",
                side="BUY",
                requested_quantity="1",
                committed_at=WHEN,
                parent_intent_id="intent-1",
            )

            with self.assertRaisesRegex(
                (AccountingConflict, RuntimeError, ValueError),
                "JournalStore|generation|backing",
            ):
                atomic_fill(orders, economics, reservations)

            self.assertNotEqual(orders.order("order-1").state, "FILLED")
            self.assertEqual(economics.transactions, ())
            self.assertEqual(
                reservations.get("reservation-1").consumed["CASH:USD"],
                Decimal("0"),
            )



if __name__ == "__main__":
    unittest.main()
