from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.durable_order_projection import DurableOrderBookProjection
from mvp.autotrade_mvp.durable_reservations import DurableReservationBook
from mvp.autotrade_mvp.fill_accounting import ProjectedFillEvidence
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.provider_activity_accounting import (
    DurableProviderEconomicBook,
    commit_provider_fill_with_reservation_consumption,
)
from mvp.autotrade_mvp.reconciliation import ProviderFillEvidence


PROVIDER = "SIMULATED"
ACCOUNT = "atomic-partial-full-account"
ENVIRONMENT = "SIMULATION"
WHEN = "2026-10-04T20:05:00Z"


def _books(store: JournalStore):
    return (
        DurableOrderBookProjection(
            store,
            provider_id=PROVIDER,
            account_id=ACCOUNT,
            environment=ENVIRONMENT,
            host_id="atomic-partial-full-host",
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


def _fill(index: int):
    fill_id = f"fill-{index}"
    execution_id = f"provider-execution-{index}"
    projected = ProjectedFillEvidence.create(
        fill_id=fill_id,
        provider_execution_id=execution_id,
        intent_id="intent-1",
        client_order_id="order-1",
        side="BUY",
        quantity="1",
        price="100",
    )
    provider = ProviderFillEvidence.create(
        provider_id=PROVIDER,
        account_id=ACCOUNT,
        environment=ENVIRONMENT,
        provider_execution_id=execution_id,
        client_order_id="order-1",
        instrument="ABC",
        quantity="1",
        price="100",
        fee_amount="0",
        fee_currency="USD",
        trade_time=WHEN,
        side="BUY",
    )
    return projected, provider


def _commit(index, orders, economics, reservations):
    projected, provider = _fill(index)
    return commit_provider_fill_with_reservation_consumption(
        economics,
        reservations,
        command_id=f"provider-fill-{index}",
        idempotency_key=f"provider-fill-{index}",
        reservation_id="reservation-1",
        projected_fill=projected,
        provider_fill=provider,
        expected_instrument="ABC",
        settlement_currency="USD",
        observed_at=WHEN,
        committed_at=WHEN,
        order_book=orders,
        order_event_key=f"fill-{index}",
    )


class AtomicPartialToFullReservationTerminalizationTests(unittest.TestCase):
    def test_only_the_fill_that_makes_oms_terminal_marks_reservation_filled(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            orders, economics, reservations = _books(store)
            reservations.reserve(
                command_id="reserve-1",
                idempotency_key="reserve-1",
                reservation_id="reservation-1",
                intent_id="intent-1",
                requirements={"CASH:USD": "220"},
                available={"CASH:USD": "1000"},
            )
            orders.create_order(
                event_key="create-1",
                client_order_id="order-1",
                instrument="ABC",
                side="BUY",
                requested_quantity="2",
                committed_at=WHEN,
            )

            self.assertTrue(_commit(1, orders, economics, reservations))
            first_order = orders.order("order-1").snapshot()
            first_reservation = reservations.get("reservation-1")
            self.assertEqual(first_order.state, "PARTIALLY_FILLED")
            self.assertEqual(first_reservation.state, "WORKING")
            self.assertEqual(
                first_reservation.consumed["CASH:USD"], Decimal("100")
            )
            self.assertEqual(
                first_reservation.remaining["CASH:USD"], Decimal("120")
            )
            self.assertEqual(
                reservations.total_reserved("CASH:USD"), Decimal("120")
            )

            self.assertTrue(_commit(2, orders, economics, reservations))
            second_order = orders.order("order-1").snapshot()
            second_reservation = reservations.get("reservation-1")
            self.assertEqual(second_order.state, "FILLED")
            self.assertEqual(second_order.filled_quantity, Decimal("2"))
            self.assertEqual(second_reservation.state, "FILLED")
            self.assertEqual(
                second_reservation.consumed["CASH:USD"], Decimal("200")
            )
            self.assertEqual(
                second_reservation.remaining["CASH:USD"], Decimal("0")
            )
            self.assertEqual(reservations.total_reserved("CASH:USD"), Decimal("0"))
            self.assertIs(type(second_reservation.resolution_evidence), str)
            self.assertIn("journal:order-fill:", second_reservation.resolution_evidence)

            reopened = JournalStore(path)
            reopened_orders, reopened_economics, reopened_reservations = _books(reopened)
            self.assertEqual(
                reopened_orders.order("order-1").snapshot(), second_order
            )
            self.assertEqual(
                reopened_reservations.get("reservation-1"), second_reservation
            )
            self.assertEqual(reopened_economics.position("ABC"), Decimal("2"))

            self.assertFalse(
                _commit(2, reopened_orders, reopened_economics, reopened_reservations)
            )
            self.assertEqual(
                reopened_reservations.get("reservation-1"), second_reservation
            )


if __name__ == "__main__":
    unittest.main()
