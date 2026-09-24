from decimal import Decimal
import unittest

from mvp.autotrade_mvp.reservations import (
    InsufficientAvailable,
    ReservationBook,
    ReservationConflict,
)


class ReservationFoundationTests(unittest.TestCase):
    def test_two_concurrent_intents_cannot_double_spend_cash(self):
        book = ReservationBook()
        book.reserve(
            reservation_id="r1",
            intent_id="i1",
            requirements={"CASH:USD": "70"},
            available={"CASH:USD": "100"},
        )
        with self.assertRaises(InsufficientAvailable):
            book.reserve(
                reservation_id="r2",
                intent_id="i2",
                requirements={"CASH:USD": "40"},
                available={"CASH:USD": "100"},
            )
        self.assertEqual(book.total_reserved("CASH:USD"), Decimal("70"))

    def test_partial_fill_reduces_reservation_and_cancel_releases_remainder(self):
        book = ReservationBook()
        book.reserve(
            reservation_id="r1",
            intent_id="i1",
            requirements={"CASH:USD": "100"},
            available={"CASH:USD": "1000"},
        )
        partial = book.consume("r1", {"CASH:USD": "40"})
        self.assertEqual(partial.remaining["CASH:USD"], Decimal("60"))
        self.assertEqual(partial.consumed["CASH:USD"], Decimal("40"))
        self.assertEqual(book.total_reserved("CASH:USD"), Decimal("60"))
        terminal = book.mark_terminal(
            "r1",
            outcome="CANCELED",
            resolution_evidence="provider-order-terminal-cancel",
        )
        self.assertEqual(terminal.remaining["CASH:USD"], Decimal("0"))
        self.assertEqual(book.total_reserved("CASH:USD"), Decimal("0"))

    def test_unknown_send_keeps_remaining_exposure_reserved(self):
        book = ReservationBook()
        book.reserve(
            reservation_id="r1",
            intent_id="i1",
            requirements={"CASH:USD": "100"},
            available={"CASH:USD": "1000"},
        )
        book.consume("r1", {"CASH:USD": "20"})
        unknown = book.mark_unknown("r1")
        self.assertEqual(unknown.state, "UNKNOWN")
        self.assertEqual(book.total_reserved("CASH:USD"), Decimal("80"))
        with self.assertRaises(ValueError):
            book.mark_terminal("r1", outcome="PROVEN_ABSENT", resolution_evidence="")

    def test_unknown_can_release_only_after_evidenced_terminal_resolution(self):
        book = ReservationBook()
        book.reserve(
            reservation_id="r1",
            intent_id="i1",
            requirements={"CASH:USD": "100"},
            available={"CASH:USD": "1000"},
        )
        book.mark_unknown("r1")
        resolved = book.mark_terminal(
            "r1",
            outcome="PROVEN_ABSENT",
            resolution_evidence="complete-provider-history-window",
        )
        self.assertEqual(resolved.state, "PROVEN_ABSENT")
        self.assertEqual(book.total_reserved("CASH:USD"), Decimal("0"))

    def test_overlapping_replacement_counts_old_reservation_until_terminal(self):
        book = ReservationBook()
        book.reserve(
            reservation_id="old",
            intent_id="i-old",
            requirements={"CASH:USD": "80"},
            available={"CASH:USD": "100"},
        )
        with self.assertRaises(InsufficientAvailable):
            book.reserve(
                reservation_id="new",
                intent_id="i-new",
                requirements={"CASH:USD": "80"},
                available={"CASH:USD": "100"},
            )
        book.mark_terminal(
            "old",
            outcome="CANCELED",
            resolution_evidence="provider-confirmed-cancel",
        )
        replacement = book.reserve(
            reservation_id="new",
            intent_id="i-new",
            requirements={"CASH:USD": "80"},
            available={"CASH:USD": "100"},
        )
        self.assertEqual(replacement.state, "WORKING")

    def test_same_reservation_is_idempotent_but_changed_content_conflicts(self):
        book = ReservationBook()
        first = book.reserve(
            reservation_id="r1",
            intent_id="i1",
            requirements={"CASH:USD": "10"},
            available={"CASH:USD": "100"},
        )
        second = book.reserve(
            reservation_id="r1",
            intent_id="i1",
            requirements={"CASH:USD": "10"},
            available={"CASH:USD": "100"},
        )
        self.assertEqual(first, second)
        with self.assertRaises(ReservationConflict):
            book.reserve(
                reservation_id="r1",
                intent_id="i1",
                requirements={"CASH:USD": "11"},
                available={"CASH:USD": "100"},
            )

    def test_consumption_cannot_exceed_or_invent_reserved_resource(self):
        book = ReservationBook()
        book.reserve(
            reservation_id="r1",
            intent_id="i1",
            requirements={"CASH:USD": "10"},
            available={"CASH:USD": "100"},
        )
        with self.assertRaises(ReservationConflict):
            book.consume("r1", {"CASH:USD": "11"})
        with self.assertRaises(ReservationConflict):
            book.consume("r1", {"POSITION:ABC": "1"})

    def test_returned_snapshot_cannot_mutate_book_state(self):
        book = ReservationBook()
        snapshot = book.reserve(
            reservation_id="r1",
            intent_id="i1",
            requirements={"CASH:USD": "10"},
            available={"CASH:USD": "100"},
        )
        with self.assertRaises(TypeError):
            snapshot.remaining["CASH:USD"] = Decimal("0")
        self.assertEqual(book.total_reserved("CASH:USD"), Decimal("10"))

    def test_binary_float_inputs_fail_closed(self):
        book = ReservationBook()
        with self.assertRaises(TypeError):
            book.reserve(
                reservation_id="r1",
                intent_id="i1",
                requirements={"CASH:USD": 10.5},
                available={"CASH:USD": "100"},
            )


if __name__ == "__main__":
    unittest.main()
