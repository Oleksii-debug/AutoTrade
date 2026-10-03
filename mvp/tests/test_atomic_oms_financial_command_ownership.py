from __future__ import annotations

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
    commit_order_fill_with_reservation_consumption,
)


PROVIDER = "SIMULATED"
ACCOUNT = "atomic-oms-command-owner"
ENVIRONMENT = "SIMULATION"
WHEN = "2026-10-03T19:00:00Z"


def _books(store: JournalStore):
    return (
        DurableOrderBookProjection(
            store,
            provider_id=PROVIDER,
            account_id=ACCOUNT,
            environment=ENVIRONMENT,
            host_id="atomic-oms-owner-host",
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


def _seed(reservations, orders):
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


def _transaction():
    return book_equity_fill(
        transaction_id="economic-fill-1",
        cause_event_id="provider-execution-1",
        instrument="ABC",
        settlement_currency="USD",
        side="BUY",
        quantity="1",
        price="100",
    )


def _atomic_fill(orders, economics, reservations):
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
        transactions=(_transaction(),),
        committed_at=WHEN,
    )


class AtomicOmsCommandOwnershipTests(unittest.TestCase):
    def test_retry_rejects_command_metadata_that_does_not_own_oms_event(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            orders, economics, reservations = _books(store)
            _seed(reservations, orders)
            self.assertTrue(_atomic_fill(orders, economics, reservations))

            original = JournalStore.load_command_event_batch

            def omit_oms_event(selected_store, **kwargs):
                authority = original(selected_store, **kwargs)
                if authority is None or kwargs.get("actor") != "atomic-fill-financial-integration":
                    return authority
                forged = dict(authority)
                forged["events"] = tuple(
                    event
                    for event in authority["events"]
                    if event.get("aggregate_type") != "order_projection_book"
                )
                return forged

            JournalStore.load_command_event_batch = omit_oms_event
            try:
                with self.assertRaisesRegex(
                    AccountingConflict,
                    "without one atomic/recovery command authority",
                ):
                    _atomic_fill(orders, economics, reservations)
            finally:
                JournalStore.load_command_event_batch = original

            self.assertEqual(
                orders.order("order-1").snapshot().filled_quantity,
                Decimal("1"),
            )
            self.assertEqual(economics.position("ABC"), Decimal("1"))
            self.assertEqual(len(economics.transactions), 1)
            self.assertEqual(
                reservations.get("reservation-1").consumed["CASH:USD"],
                Decimal("100"),
            )


if __name__ == "__main__":
    unittest.main()
