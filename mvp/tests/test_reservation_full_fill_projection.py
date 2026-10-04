from decimal import Decimal
import unittest

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
