from decimal import Decimal
from tempfile import TemporaryDirectory
from threading import Barrier, Lock, Thread
import unittest

from mvp.autotrade_mvp.persistence import JournalStore
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

    def test_same_intent_cannot_receive_two_reservation_ids(self):
        book = ReservationBook()
        book.reserve(
            reservation_id="r1",
            intent_id="i1",
            requirements={"CASH:USD": "10"},
            available={"CASH:USD": "100"},
        )
        with self.assertRaises(ReservationConflict):
            book.reserve(
                reservation_id="r2",
                intent_id="i1",
                requirements={"CASH:USD": "10"},
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


    def test_filled_terminal_cannot_release_unconsumed_remainder(self):
        book = ReservationBook()
        book.reserve(
            reservation_id="r1",
            intent_id="i1",
            requirements={"CASH:USD": "100"},
            available={"CASH:USD": "100"},
        )
        book.consume("r1", {"CASH:USD": "40"})
        with self.assertRaisesRegex(ReservationConflict, "FILLED"):
            book.mark_terminal(
                "r1",
                outcome="FILLED",
                resolution_evidence="provider-filled",
            )
        self.assertEqual(book.total_reserved("CASH:USD"), Decimal("60"))

    def test_rejected_or_absent_cannot_erase_consumed_exposure(self):
        for outcome in ("REJECTED", "PROVEN_ABSENT"):
            with self.subTest(outcome=outcome):
                book = ReservationBook()
                book.reserve(
                    reservation_id="r1",
                    intent_id="i1",
                    requirements={"CASH:USD": "100"},
                    available={"CASH:USD": "100"},
                )
                book.consume("r1", {"CASH:USD": "10"})
                with self.assertRaises(ReservationConflict):
                    book.mark_terminal(
                        "r1",
                        outcome=outcome,
                        resolution_evidence="provider-history",
                    )
                self.assertEqual(book.total_reserved("CASH:USD"), Decimal("90"))



    def test_durable_restart_preserves_working_unknown_and_consumed_exposure(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            book = ReservationBook(store)
            book.reserve(
                reservation_id="r-durable",
                intent_id="i-durable",
                requirements={"CASH:USD": "100"},
                available={"CASH:USD": "1000"},
            )
            partial = book.consume(
                "r-durable",
                {"CASH:USD": "25"},
                operation_id="fill-1",
            )
            self.assertEqual(partial.remaining["CASH:USD"], Decimal("75"))
            book.mark_unknown("r-durable")

            restarted = ReservationBook(store)
            restored = restarted.get("r-durable")
            self.assertEqual(restored.state, "UNKNOWN")
            self.assertEqual(restored.remaining["CASH:USD"], Decimal("75"))
            self.assertEqual(restored.consumed["CASH:USD"], Decimal("25"))
            self.assertEqual(
                restarted.total_reserved("CASH:USD"),
                Decimal("75"),
            )

    def test_durable_consume_requires_operation_id_and_retry_is_exactly_once(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            book = ReservationBook(store)
            book.reserve(
                reservation_id="r1",
                intent_id="i1",
                requirements={"CASH:USD": "100"},
                available={"CASH:USD": "1000"},
            )
            with self.assertRaisesRegex(ValueError, "operation_id"):
                book.consume("r1", {"CASH:USD": "10"})

            first = book.consume(
                "r1",
                {"CASH:USD": "10"},
                operation_id="provider-fill-1",
            )
            second = book.consume(
                "r1",
                {"CASH:USD": "10"},
                operation_id="provider-fill-1",
            )
            self.assertEqual(first, second)
            self.assertEqual(second.consumed["CASH:USD"], Decimal("10"))
            self.assertEqual(book.total_reserved("CASH:USD"), Decimal("90"))

            restarted = ReservationBook(store)
            after_restart = restarted.consume(
                "r1",
                {"CASH:USD": "10"},
                operation_id="provider-fill-1",
            )
            self.assertEqual(after_restart, first)
            self.assertEqual(
                restarted.get("r1").consumed["CASH:USD"],
                Decimal("10"),
            )
            with self.assertRaisesRegex(ReservationConflict, "different"):
                restarted.consume(
                    "r1",
                    {"CASH:USD": "11"},
                    operation_id="provider-fill-1",
                )

    def test_durable_terminal_resolution_survives_restart(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            book = ReservationBook(store)
            book.reserve(
                reservation_id="r-terminal",
                intent_id="i-terminal",
                requirements={"CASH:USD": "50"},
                available={"CASH:USD": "100"},
            )
            terminal = book.mark_terminal(
                "r-terminal",
                outcome="CANCELED",
                resolution_evidence="provider-terminal-cancel",
            )
            self.assertEqual(terminal.state, "CANCELED")

            restarted = ReservationBook(store)
            self.assertEqual(restarted.get("r-terminal"), terminal)
            self.assertEqual(
                restarted.total_reserved("CASH:USD"),
                Decimal("0"),
            )

    def test_two_durable_writers_cannot_both_commit_oversubscribed_cash(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            first = ReservationBook(store)
            second = ReservationBook(store)
            barrier = Barrier(2)
            result_lock = Lock()
            outcomes = []

            def reserve(book, reservation_id, intent_id):
                barrier.wait()
                try:
                    book.reserve(
                        reservation_id=reservation_id,
                        intent_id=intent_id,
                        requirements={"CASH:USD": "70"},
                        available={"CASH:USD": "100"},
                    )
                    outcome = "committed"
                except (ReservationConflict, InsufficientAvailable):
                    outcome = "blocked"
                with result_lock:
                    outcomes.append(outcome)

            threads = (
                Thread(target=reserve, args=(first, "r-a", "i-a")),
                Thread(target=reserve, args=(second, "r-b", "i-b")),
            )
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)
                self.assertFalse(thread.is_alive())

            self.assertEqual(sorted(outcomes), ["blocked", "committed"])
            restarted = ReservationBook(store)
            self.assertEqual(
                restarted.total_reserved("CASH:USD"),
                Decimal("70"),
            )
            self.assertEqual(len(restarted.active()), 1)

    def test_second_durable_writer_observes_committed_reservation_before_admission(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            first = ReservationBook(store)
            second = ReservationBook(store)
            first.reserve(
                reservation_id="r-first",
                intent_id="i-first",
                requirements={"CASH:USD": "70"},
                available={"CASH:USD": "100"},
            )
            with self.assertRaises(InsufficientAvailable):
                second.reserve(
                    reservation_id="r-second",
                    intent_id="i-second",
                    requirements={"CASH:USD": "40"},
                    available={"CASH:USD": "100"},
                )
            self.assertEqual(
                second.total_reserved("CASH:USD"),
                Decimal("70"),
            )


if __name__ == "__main__":
    unittest.main()
