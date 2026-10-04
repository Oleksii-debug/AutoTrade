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


PROVIDER = "SIMULATED"
ACCOUNT = "late-cancelled-bust-account"
ENVIRONMENT = "SIMULATION"
WHEN = "2026-10-04T19:40:00Z"


class LateCancelledFillBustConservationTests(unittest.TestCase):
    def test_late_bust_after_confirmed_cancel_reverses_fill_without_reholding_terminal_order(self):
        """A late bust must not confuse terminal order truth with fill economics.

        The provider-confirmed cancellation still owns the order remainder after
        the earlier partial fill is busted. Therefore the OMS/economic reversal
        must commit, but the already-cancelled reservation must remain released:
        there is no live order exposure whose capacity should be re-held.
        """

        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            orders = DurableOrderBookProjection(
                store,
                provider_id=PROVIDER,
                account_id=ACCOUNT,
                environment=ENVIRONMENT,
                host_id="late-cancelled-bust-host",
                owner_epoch="1",
            )
            economics = DurableProviderEconomicBook(
                store,
                provider_id=PROVIDER,
                account_id=ACCOUNT,
                environment=ENVIRONMENT,
            )
            reservations = DurableReservationBook(
                store,
                environment=ENVIRONMENT,
                account_id=ACCOUNT,
            )

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
            projected = ProjectedFillEvidence.create(
                fill_id="fill-1",
                provider_execution_id="provider-execution-1",
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
                provider_execution_id="provider-execution-1",
                client_order_id="order-1",
                instrument="ABC",
                quantity="1",
                price="100",
                fee_amount="0",
                fee_currency="USD",
                trade_time=WHEN,
                side="BUY",
            )
            self.assertTrue(
                commit_provider_fill_with_reservation_consumption(
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
            )
            self.assertEqual(
                orders.order("order-1").snapshot().state,
                "PARTIALLY_FILLED",
            )
            self.assertEqual(economics.position("ABC"), Decimal("1"))

            orders.confirm_cancel(
                event_key="cancel-confirmed-1",
                client_order_id="order-1",
                committed_at=WHEN,
            )
            self.assertEqual(
                orders.order("order-1").snapshot().state,
                "PARTIALLY_FILLED_CANCELLED",
            )

            # Isolate legacy cancellation state-machine behavior; this synthetic
            # receipt is never accepted by the production evidence verifier.
            original_verify = DurableReservationBook._verify_resolution_evidence

            def accept_cancelled_fixture(selected_book, **kwargs):
                if kwargs.get("resolution_evidence") == "fixture-provider-cancelled":
                    return "fixture-provider-cancelled"
                return original_verify(selected_book, **kwargs)

            DurableReservationBook._verify_resolution_evidence = accept_cancelled_fixture
            try:
                cancelled = reservations._commit(
                    command_id="fixture-cancel-terminal",
                    idempotency_key="fixture-cancel-terminal",
                    operation="MARK_TERMINAL",
                    request={
                        "reservation_id": "reservation-1",
                        "outcome": "CANCELED",
                        "resolution_evidence": "fixture-provider-cancelled",
                    },
                )
                self.assertEqual(cancelled.state, "CANCELED")
                self.assertEqual(reservations.total_reserved("CASH:USD"), Decimal("0"))

                self.assertTrue(
                    commit_provider_fill_bust_with_economic_reversal(
                        economics,
                        orders,
                        command_id="provider-bust-1",
                        idempotency_key="provider-bust-1",
                        projected_fill=projected,
                        provider_fill=provider,
                        expected_instrument="ABC",
                        settlement_currency="USD",
                        bust_provider_revision="provider-revision-bust-2",
                        bust_observed_at=WHEN,
                        order_event_key="bust-1",
                        reservation_book=reservations,
                        reservation_id="reservation-1",
                        committed_at=WHEN,
                    )
                )

                reopened = JournalStore(Path(directory) / "journal.sqlite3")
                reopened_orders = DurableOrderBookProjection(
                    reopened,
                    provider_id=PROVIDER,
                    account_id=ACCOUNT,
                    environment=ENVIRONMENT,
                    host_id="late-cancelled-bust-host",
                    owner_epoch="1",
                )
                reopened_economics = DurableProviderEconomicBook(
                    reopened,
                    provider_id=PROVIDER,
                    account_id=ACCOUNT,
                    environment=ENVIRONMENT,
                )
                reopened_reservations = DurableReservationBook(
                    reopened,
                    environment=ENVIRONMENT,
                    account_id=ACCOUNT,
                )

                snapshot = reopened_orders.order("order-1").snapshot()
                self.assertEqual(snapshot.state, "CANCELLED")
                self.assertEqual(snapshot.filled_quantity, Decimal("0"))
                self.assertEqual(snapshot.fill_count, 0)
                self.assertEqual(reopened_economics.position("ABC"), Decimal("0"))
                self.assertEqual(len(reopened_economics.transactions), 2)
                reservation = reopened_reservations.get("reservation-1")
                self.assertEqual(reservation.state, "CANCELED")
                self.assertEqual(reservation.remaining["CASH:USD"], Decimal("0"))
                self.assertEqual(
                    reopened_reservations.total_reserved("CASH:USD"),
                    Decimal("0"),
                )
            finally:
                DurableReservationBook._verify_resolution_evidence = original_verify


if __name__ == "__main__":
    unittest.main()
