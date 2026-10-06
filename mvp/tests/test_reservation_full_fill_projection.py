from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.durable_reservations import DurableReservationBook
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.reservations import (
    POST_BUST_HOLD_STATE,
    ReservationBook,
    ReservationConflict,
)


class ReservationFullFillProjectionTests(unittest.TestCase):
    def test_consume_and_mark_filled_is_one_terminal_projection_transition(self):
        book = ReservationBook()
        book.reserve(
            reservation_id="reservation-1",
            intent_id="intent-1",
            requirements={"CASH:USD": "120", "FEE:USD": "5"},
            available={"CASH:USD": "1000", "FEE:USD": "100"},
        )

        terminal = book.consume_and_mark_filled(
            "reservation-1",
            {"CASH:USD": "100", "FEE:USD": "2"},
            resolution_evidence="journal:order-filled:event-1@sha256:abc",
        )

        self.assertEqual(terminal.state, "FILLED")
        self.assertEqual(
            terminal.resolution_evidence,
            "journal:order-filled:event-1@sha256:abc",
        )
        self.assertEqual(terminal.consumed["CASH:USD"], Decimal("100"))
        self.assertEqual(terminal.consumed["FEE:USD"], Decimal("2"))
        self.assertEqual(terminal.remaining["CASH:USD"], Decimal("0"))
        self.assertEqual(terminal.remaining["FEE:USD"], Decimal("0"))
        self.assertEqual(book.total_reserved("CASH:USD"), Decimal("0"))
        self.assertEqual(book.total_reserved("FEE:USD"), Decimal("0"))
        self.assertEqual(book.active(), ())

    def test_invalid_terminal_consumption_is_all_or_nothing(self):
        book = ReservationBook()
        before = book.reserve(
            reservation_id="reservation-1",
            intent_id="intent-1",
            requirements={"CASH:USD": "120"},
            available={"CASH:USD": "1000"},
        )

        with self.assertRaisesRegex(
            ReservationConflict,
            "Consumption exceeds remaining reservation",
        ):
            book.consume_and_mark_filled(
                "reservation-1",
                {"CASH:USD": "121"},
                resolution_evidence="journal:order-filled:event-1@sha256:abc",
            )
        self.assertEqual(book.get("reservation-1"), before)
        self.assertEqual(book.total_reserved("CASH:USD"), Decimal("120"))

        with self.assertRaisesRegex(ValueError, "resolution_evidence"):
            book.consume_and_mark_filled(
                "reservation-1",
                {"CASH:USD": "100"},
                resolution_evidence="",
            )
        self.assertEqual(book.get("reservation-1"), before)
        self.assertEqual(book.total_reserved("CASH:USD"), Decimal("120"))

    def test_terminal_projection_cannot_be_applied_twice(self):
        book = ReservationBook()
        book.reserve(
            reservation_id="reservation-1",
            intent_id="intent-1",
            requirements={"CASH:USD": "120"},
            available={"CASH:USD": "1000"},
        )
        book.consume_and_mark_filled(
            "reservation-1",
            {"CASH:USD": "100"},
            resolution_evidence="journal:order-filled:event-1@sha256:abc",
        )

        with self.assertRaisesRegex(
            ReservationConflict,
            "Cannot consume and terminalize a terminal reservation",
        ):
            book.consume_and_mark_filled(
                "reservation-1",
                {"CASH:USD": "1"},
                resolution_evidence="journal:order-filled:event-2@sha256:def",
            )

        terminal = book.get("reservation-1")
        self.assertEqual(terminal.state, "FILLED")
        self.assertEqual(terminal.consumed["CASH:USD"], Decimal("100"))
        self.assertEqual(terminal.remaining["CASH:USD"], Decimal("0"))

    def test_durable_terminal_prepare_replays_historical_consume_without_rewrite(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            book = DurableReservationBook(
                store,
                environment="SIMULATION",
                account_id="account-1",
            )
            book.reserve(
                command_id="reserve-1",
                idempotency_key="reserve-1",
                reservation_id="reservation-1",
                intent_id="intent-1",
                requirements={"CASH:USD": "120"},
                available={"CASH:USD": "1000"},
            )
            book.consume(
                command_id="legacy-consume",
                idempotency_key="legacy-fill-component",
                reservation_id="reservation-1",
                usage={"CASH:USD": "100"},
            )

            plan = book.prepare_consume_and_mark_filled_mutation(
                event_key="new-terminal-attempt",
                idempotency_key="legacy-fill-component",
                reservation_id="reservation-1",
                usage={"CASH:USD": "100"},
                order_fill_event_id="00000000-0000-0000-0000-000000000001",
                order_fill_payload_hash="sha256:" + "1" * 64,
                order_fill_snapshot_digest="sha256:" + "2" * 64,
                order_fill_mutation_hash="sha256:" + "3" * 64,
                order_fill_provider_id="SIMULATED",
                order_fill_client_order_id="order-1",
                order_fill_fill_id="fill-1",
                order_fill_provider_execution_id="execution-1",
                committed_at="2026-10-04T20:10:00Z",
            )

            self.assertTrue(plan.already_committed)
            self.assertIsNone(plan.envelope)
            self.assertEqual(
                plan.request,
                {
                    "reservation_id": "reservation-1",
                    "usage": {"CASH:USD": "100"},
                },
            )
            self.assertEqual(plan.snapshot.state, "WORKING")
            self.assertEqual(
                plan.snapshot.consumed["CASH:USD"],
                Decimal("100"),
            )
            self.assertEqual(
                book.get("reservation-1").remaining["CASH:USD"],
                Decimal("20"),
            )

    def test_durable_terminal_replay_rejects_missing_oms_fill_authority(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            book = DurableReservationBook(
                store,
                environment="SIMULATION",
                account_id="account-1",
            )
            book.reserve(
                command_id="reserve-1",
                idempotency_key="reserve-1",
                reservation_id="reservation-1",
                intent_id="intent-1",
                requirements={"CASH:USD": "120"},
                available={"CASH:USD": "1000"},
            )
            plan = book.prepare_consume_and_mark_filled_mutation(
                event_key="forged-terminal",
                idempotency_key="forged-terminal",
                reservation_id="reservation-1",
                usage={"CASH:USD": "100"},
                order_fill_event_id="00000000-0000-0000-0000-000000000001",
                order_fill_payload_hash="sha256:" + "1" * 64,
                order_fill_snapshot_digest="sha256:" + "2" * 64,
                order_fill_mutation_hash="sha256:" + "3" * 64,
                order_fill_provider_id="SIMULATED",
                order_fill_client_order_id="order-1",
                order_fill_fill_id="fill-1",
                order_fill_provider_execution_id="execution-1",
                committed_at="2026-10-04T20:10:00Z",
            )
            self.assertEqual(plan.snapshot.state, "FILLED")
            self.assertEqual(book.get("reservation-1").state, "WORKING")
            self.assertIsNotNone(plan.envelope)

            store.append_event(plan.envelope)
            with self.assertRaisesRegex(
                ReservationConflict,
                "referenced durable OMS fill event",
            ):
                book.refresh()

    def test_busted_terminal_can_refill_and_terminalize_again(self):
        book = ReservationBook()
        book.reserve(
            reservation_id="reservation-1",
            intent_id="intent-1",
            requirements={"CASH:USD": "120", "FEE:USD": "5"},
            available={"CASH:USD": "1000", "FEE:USD": "100"},
        )
        book.consume_and_mark_filled(
            "reservation-1",
            {"CASH:USD": "100", "FEE:USD": "2"},
            resolution_evidence="journal:order-filled:event-1@sha256:abc",
        )

        reopened = book.restore_consumption(
            "reservation-1",
            {"CASH:USD": "100", "FEE:USD": "2"},
        )
        self.assertEqual(reopened.state, POST_BUST_HOLD_STATE)
        self.assertEqual(reopened.remaining["CASH:USD"], Decimal("120"))
        self.assertEqual(reopened.remaining["FEE:USD"], Decimal("5"))
        self.assertEqual(book.total_reserved("CASH:USD"), Decimal("120"))

        refilled = book.consume_and_mark_filled(
            "reservation-1",
            {"CASH:USD": "101", "FEE:USD": "3"},
            resolution_evidence="journal:order-filled:event-2@sha256:def",
        )
        self.assertEqual(refilled.state, "FILLED")
        self.assertEqual(
            refilled.resolution_evidence,
            "journal:order-filled:event-2@sha256:def",
        )
        self.assertEqual(refilled.consumed["CASH:USD"], Decimal("101"))
        self.assertEqual(refilled.consumed["FEE:USD"], Decimal("3"))
        self.assertEqual(refilled.remaining["CASH:USD"], Decimal("0"))
        self.assertEqual(refilled.remaining["FEE:USD"], Decimal("0"))
        self.assertEqual(book.total_reserved("CASH:USD"), Decimal("0"))
        self.assertEqual(book.total_reserved("FEE:USD"), Decimal("0"))
        self.assertEqual(book.active(), ())


if __name__ == "__main__":
    unittest.main()
