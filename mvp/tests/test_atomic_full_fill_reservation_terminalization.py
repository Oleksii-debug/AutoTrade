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
    commit_provider_fill_bust_with_economic_reversal,
    commit_provider_fill_with_reservation_consumption,
)
from mvp.autotrade_mvp.reconciliation import ProviderFillEvidence
from mvp.autotrade_mvp.reservations import POST_BUST_HOLD_STATE


PROVIDER = "SIMULATED"
ACCOUNT = "atomic-full-fill-account"
ENVIRONMENT = "SIMULATION"
WHEN = "2026-10-04T19:52:00Z"


def books(store: JournalStore):
    return (
        DurableOrderBookProjection(
            store,
            provider_id=PROVIDER,
            account_id=ACCOUNT,
            environment=ENVIRONMENT,
            host_id="atomic-full-fill-host",
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


def evidence():
    return (
        ProjectedFillEvidence.create(
            fill_id="fill-1",
            provider_execution_id="provider-execution-1",
            intent_id="intent-1",
            client_order_id="order-1",
            side="BUY",
            quantity="1",
            price="100",
        ),
        ProviderFillEvidence.create(
            provider_id=PROVIDER,
            account_id=ACCOUNT,
            environment=ENVIRONMENT,
            provider_execution_id="provider-execution-1",
            client_order_id="order-1",
            instrument="ABC",
            quantity="1",
            price="100",
            fee_amount="0",
            fee_currency="USD",
            trade_time=WHEN,
            side="BUY",
        ),
    )


def commit_full_fill(orders, economics, reservations):
    projected, provider = evidence()
    inserted = commit_provider_fill_with_reservation_consumption(
        economics,
        reservations,
        command_id="provider-fill-1",
        idempotency_key="provider-fill-1",
        reservation_id="reservation-1",
        projected_fill=projected,
        provider_fill=provider,
        expected_instrument="ABC",
        settlement_currency="USD",
        observed_at=WHEN,
        committed_at=WHEN,
        order_book=orders,
        order_event_key="fill-1",
    )
    return inserted, projected, provider


class AtomicFullFillReservationTerminalizationTests(unittest.TestCase):
    def test_atomic_full_fill_terminalizes_reservation_and_restart_preserves_cut(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            orders, economics, reservations = books(store)
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

            inserted, projected, provider = commit_full_fill(
                orders, economics, reservations
            )
            self.assertTrue(inserted)
            self.assertEqual(orders.order("order-1").snapshot().state, "FILLED")

            reservation = reservations.get("reservation-1")
            self.assertEqual(reservation.state, "FILLED")
            self.assertEqual(reservation.consumed["CASH:USD"], Decimal("100"))
            self.assertEqual(reservation.remaining["CASH:USD"], Decimal("0"))
            self.assertIs(type(reservation.resolution_evidence), str)
            self.assertTrue(reservation.resolution_evidence)
            self.assertEqual(reservations.total_reserved("CASH:USD"), Decimal("0"))

            reopened = JournalStore(path)
            restarted_orders, restarted_economics, restarted_reservations = books(
                reopened
            )
            restarted = restarted_reservations.get("reservation-1")
            self.assertEqual(restarted, reservation)
            self.assertEqual(
                restarted_orders.order("order-1").snapshot().state,
                "FILLED",
            )
            self.assertEqual(restarted_economics.position("ABC"), Decimal("1"))

            # Exact retry must replay the already-committed atomic cut rather
            # than trying to consume/terminalize a terminal reservation again.
            retried, _, _ = commit_full_fill(
                restarted_orders,
                restarted_economics,
                restarted_reservations,
            )
            self.assertFalse(retried)
            self.assertEqual(
                restarted_reservations.get("reservation-1"), reservation
            )

            # The resulting terminal cut is real production authority: a later
            # provider bust can use it directly, with no synthetic MARK_TERMINAL
            # fixture, and must reconstitute the conservative reservation hold.
            self.assertTrue(
                commit_provider_fill_bust_with_economic_reversal(
                    restarted_economics,
                    restarted_orders,
                    command_id="provider-bust-1",
                    idempotency_key="provider-bust-1",
                    projected_fill=projected,
                    provider_fill=provider,
                    expected_instrument="ABC",
                    settlement_currency="USD",
                    bust_provider_revision="provider-revision-bust-2",
                    bust_observed_at=WHEN,
                    order_event_key="bust-1",
                    reservation_book=restarted_reservations,
                    reservation_id="reservation-1",
                    committed_at=WHEN,
                )
            )
            busted = restarted_reservations.get("reservation-1")
            self.assertEqual(busted.state, POST_BUST_HOLD_STATE)
            self.assertEqual(busted.consumed["CASH:USD"], Decimal("0"))
            self.assertEqual(busted.remaining["CASH:USD"], Decimal("120"))
            self.assertEqual(
                restarted_reservations.total_reserved("CASH:USD"),
                Decimal("120"),
            )


if __name__ == "__main__":
    unittest.main()
