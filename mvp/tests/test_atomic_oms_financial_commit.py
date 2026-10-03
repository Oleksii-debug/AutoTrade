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
)


PROVIDER = "SIMULATED"
ACCOUNT = "atomic-oms-account"
ENVIRONMENT = "SIMULATION"
WHEN = "2026-10-03T19:00:00Z"


def books(store: JournalStore):
    reservations = DurableReservationBook(
        store,
        environment=ENVIRONMENT,
        account_id=ACCOUNT,
    )
    economics = DurableProviderEconomicBook(
        store,
        provider_id=PROVIDER,
        account_id=ACCOUNT,
        environment=ENVIRONMENT,
    )
    orders = DurableOrderBookProjection(
        store,
        provider_id=PROVIDER,
        account_id=ACCOUNT,
        environment=ENVIRONMENT,
        host_id="atomic-oms-host",
        owner_epoch="1",
    )
    return reservations, economics, orders


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
    )


def atomic_fill(
    economics: DurableProviderEconomicBook,
    reservations: DurableReservationBook,
    orders: DurableOrderBookProjection,
):
    transaction = book_equity_fill(
        transaction_id="economic-fill-1",
        cause_event_id="execution-1",
        instrument="ABC",
        settlement_currency="USD",
        side="BUY",
        quantity="1",
        price="100",
    )
    return commit_economic_batch_with_reservation_consumption(
        economics,
        reservations,
        command_id="atomic-oms-fill-1",
        idempotency_key="atomic-oms-fill-1",
        reservation_id="reservation-1",
        usage={"CASH:USD": "100"},
        transactions=(transaction,),
        committed_at=WHEN,
        order_projection=orders,
        order_fill={
            "event_key": "fill-1",
            "client_order_id": "order-1",
            "fill_id": "execution-1",
            "provider_execution_id": "execution-1",
            "quantity": "1",
            "price": "100",
            "provider_revision": None,
            "evidence_refs": None,
        },
    )


class AtomicOmsFinancialCommitTests(unittest.TestCase):
    def test_oms_economics_and_reservation_restart_together(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            reservations, economics, orders = books(store)
            seed(reservations, orders)

            self.assertTrue(atomic_fill(economics, reservations, orders))
            self.assertEqual(orders.order("order-1").state, "FILLED")
            self.assertEqual(str(economics.position("ABC")), "1")
            self.assertEqual(
                str(reservations.get("reservation-1").consumed["CASH:USD"]),
                "100",
            )

            reopened = JournalStore(path)
            rr, re, ro = books(reopened)
            self.assertEqual(ro.order("order-1").state, "FILLED")
            self.assertEqual(str(re.position("ABC")), "1")
            self.assertEqual(
                str(rr.get("reservation-1").consumed["CASH:USD"]),
                "100",
            )

    def test_failure_before_shared_commit_leaves_all_three_unmutated(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            reservations, economics, orders = books(store)
            seed(reservations, orders)
            original = JournalStore.commit_command

            def fail(selected_store, **kwargs):
                if selected_store is store:
                    raise RuntimeError("injected atomic OMS failure")
                return original(selected_store, **kwargs)

            JournalStore.commit_command = fail
            try:
                with self.assertRaisesRegex(RuntimeError, "atomic OMS failure"):
                    atomic_fill(economics, reservations, orders)
            finally:
                JournalStore.commit_command = original

            reopened = JournalStore(path)
            rr, re, ro = books(reopened)
            self.assertNotEqual(ro.order("order-1").state, "FILLED")
            self.assertEqual(re.transactions, ())
            self.assertEqual(
                str(rr.get("reservation-1").consumed["CASH:USD"]),
                "0",
            )

    def test_ack_loss_retry_is_exactly_once_across_all_three(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            reservations, economics, orders = books(store)
            seed(reservations, orders)
            original = JournalStore.commit_command
            injected = False

            def lose_ack(selected_store, **kwargs):
                nonlocal injected
                result = original(selected_store, **kwargs)
                if selected_store is store and result[1] and not injected:
                    injected = True
                    raise RuntimeError("injected atomic OMS acknowledgement loss")
                return result

            JournalStore.commit_command = lose_ack
            try:
                with self.assertRaisesRegex(RuntimeError, "acknowledgement loss"):
                    atomic_fill(economics, reservations, orders)
            finally:
                JournalStore.commit_command = original

            reopened = JournalStore(path)
            rr, re, ro = books(reopened)
            self.assertFalse(atomic_fill(re, rr, ro))
            self.assertEqual(ro.order("order-1").state, "FILLED")
            self.assertEqual(len(re.transactions), 1)
            self.assertEqual(
                str(rr.get("reservation-1").consumed["CASH:USD"]),
                "100",
            )

    def test_preexisting_oms_fill_without_financial_pair_fails_closed(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            reservations, economics, orders = books(store)
            seed(reservations, orders)
            orders.record_fill(
                event_key="fill-1",
                client_order_id="order-1",
                fill_id="execution-1",
                provider_execution_id="execution-1",
                quantity="1",
                price="100",
                committed_at=WHEN,
            )

            with self.assertRaisesRegex(AccountingConflict, "partially committed"):
                atomic_fill(economics, reservations, orders)
            self.assertEqual(economics.transactions, ())
            self.assertEqual(
                str(reservations.get("reservation-1").consumed["CASH:USD"]),
                "0",
            )


if __name__ == "__main__":
    unittest.main()
