from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.durable_reservations import (
    DurableReservationBook,
    reservation_snapshot_digest,
)
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.reservations import (
    POST_BUST_HOLD_STATE,
    ReservationBook,
    ReservationConflict,
)


class TerminalFillBustReservationConservationTests(unittest.TestCase):
    def test_terminal_filled_bust_restores_full_unresolved_hold_without_relabelling_active(self):
        book = ReservationBook()
        book.reserve(
            reservation_id="reservation-1",
            intent_id="intent-1",
            requirements={"CASH:USD": "120", "BUFFER:USD": "20"},
            available={"CASH:USD": "1000", "BUFFER:USD": "1000"},
        )
        book.consume("reservation-1", {"CASH:USD": "100"})
        terminal = book.mark_terminal(
            "reservation-1",
            outcome="FILLED",
            resolution_evidence="provider-filled-evidence",
        )
        self.assertEqual(terminal.state, "FILLED")
        self.assertEqual(terminal.remaining["CASH:USD"], Decimal("0"))
        self.assertEqual(terminal.remaining["BUFFER:USD"], Decimal("0"))
        self.assertEqual(book.total_reserved("CASH:USD"), Decimal("0"))
        self.assertEqual(book.total_reserved("BUFFER:USD"), Decimal("0"))

        restored = book.restore_consumption(
            "reservation-1",
            {"CASH:USD": "100"},
        )

        self.assertEqual(restored.state, POST_BUST_HOLD_STATE)
        self.assertIsNone(restored.resolution_evidence)
        self.assertEqual(restored.consumed["CASH:USD"], Decimal("0"))
        self.assertEqual(restored.remaining["CASH:USD"], Decimal("120"))
        # Terminalization released unused buffers too. A bust must reconstruct
        # those holds from immutable original minus still-consumed authority.
        self.assertEqual(restored.remaining["BUFFER:USD"], Decimal("20"))
        self.assertEqual(book.total_reserved("CASH:USD"), Decimal("120"))
        self.assertEqual(book.total_reserved("BUFFER:USD"), Decimal("20"))
        self.assertEqual(
            tuple(item.reservation_id for item in book.active()),
            ("reservation-1",),
        )

        with self.assertRaisesRegex(
            ReservationConflict,
            "requires canonical reconciliation before UNKNOWN",
        ):
            book.mark_unknown("reservation-1")

    def test_terminal_cancelled_bust_reverses_consumed_history_without_reholding_capacity(self):
        book = ReservationBook()
        book.reserve(
            reservation_id="reservation-1",
            intent_id="intent-1",
            requirements={"CASH:USD": "120", "BUFFER:USD": "20"},
            available={"CASH:USD": "1000", "BUFFER:USD": "1000"},
        )
        book.consume("reservation-1", {"CASH:USD": "100"})
        cancelled = book.mark_terminal(
            "reservation-1",
            outcome="CANCELED",
            resolution_evidence="provider-cancelled-evidence",
        )
        self.assertEqual(cancelled.state, "CANCELED")
        self.assertEqual(cancelled.remaining["CASH:USD"], Decimal("0"))
        self.assertEqual(book.total_reserved("CASH:USD"), Decimal("0"))

        restored = book.restore_consumption(
            "reservation-1",
            {"CASH:USD": "100"},
        )

        self.assertEqual(restored.state, "CANCELED")
        self.assertEqual(
            restored.resolution_evidence,
            "provider-cancelled-evidence",
        )
        self.assertEqual(restored.consumed["CASH:USD"], Decimal("0"))
        self.assertEqual(restored.remaining["CASH:USD"], Decimal("0"))
        self.assertEqual(restored.remaining["BUFFER:USD"], Decimal("0"))
        self.assertEqual(book.total_reserved("CASH:USD"), Decimal("0"))
        self.assertEqual(book.total_reserved("BUFFER:USD"), Decimal("0"))
        self.assertEqual(book.active(), ())

    def test_rejected_or_proven_absent_cannot_acquire_post_bust_authority(self):
        for outcome in ("REJECTED", "PROVEN_ABSENT"):
            with self.subTest(outcome=outcome):
                book = ReservationBook()
                book.reserve(
                    reservation_id="reservation-1",
                    intent_id="intent-1",
                    requirements={"CASH:USD": "120"},
                    available={"CASH:USD": "1000"},
                )
                book.mark_terminal(
                    "reservation-1",
                    outcome=outcome,
                    resolution_evidence=f"provider-{outcome.lower()}-evidence",
                )
                with self.assertRaisesRegex(
                    ReservationConflict,
                    "Cannot restore consumption on a terminal reservation",
                ):
                    book.restore_consumption(
                        "reservation-1",
                        {"CASH:USD": "1"},
                    )
                self.assertEqual(book.get("reservation-1").state, outcome)
                self.assertEqual(book.total_reserved("CASH:USD"), Decimal("0"))

    def test_durable_terminal_restore_is_prepared_without_mutating_before_atomic_commit(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            book = DurableReservationBook(
                store,
                environment="SIMULATION",
                account_id="terminal-bust-account",
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
                command_id="consume-1",
                idempotency_key="consume-1",
                reservation_id="reservation-1",
                usage={"CASH:USD": "100"},
            )

            # Historical-fixture bridge only. Production terminal release keeps
            # its strict trusted-evidence verifier. Hold this override only while
            # replaying the synthetic legacy FILLED event used to exercise the
            # later prepared bust transition.
            original_verify = DurableReservationBook._verify_resolution_evidence

            def accept_legacy_filled_fixture(selected_book, **kwargs):
                if kwargs.get("resolution_evidence") == "fixture-provider-filled":
                    return "fixture-provider-filled"
                return original_verify(selected_book, **kwargs)

            DurableReservationBook._verify_resolution_evidence = (
                accept_legacy_filled_fixture
            )
            try:
                book._commit(
                    command_id="fixture-filled-terminal",
                    idempotency_key="fixture-filled-terminal",
                    operation="MARK_TERMINAL",
                    request={
                        "reservation_id": "reservation-1",
                        "outcome": "FILLED",
                        "resolution_evidence": "fixture-provider-filled",
                    },
                )
                terminal = book.get("reservation-1")
                self.assertEqual(terminal.state, "FILLED")
                self.assertEqual(book.total_reserved("CASH:USD"), Decimal("0"))

                plan = book.prepare_restore_consumption_mutation(
                    event_key="atomic-bust-reservation-event-1",
                    idempotency_key="atomic-bust-reservation-1",
                    reservation_id="reservation-1",
                    usage={"CASH:USD": "100"},
                    committed_at="2026-10-04T19:30:00Z",
                    expected_snapshot_digest=reservation_snapshot_digest(terminal),
                )

                self.assertFalse(plan.already_committed)
                self.assertIsNotNone(plan.envelope)
                self.assertEqual(plan.snapshot.state, POST_BUST_HOLD_STATE)
                self.assertEqual(
                    plan.snapshot.consumed["CASH:USD"], Decimal("0")
                )
                self.assertEqual(
                    plan.snapshot.remaining["CASH:USD"], Decimal("120")
                )
                # Preparation alone cannot reopen durable authority. The shared
                # OMS/economic bust barrier owns the eventual atomic commit.
                still_terminal = book.get("reservation-1")
                self.assertEqual(still_terminal.state, "FILLED")
                self.assertEqual(
                    still_terminal.remaining["CASH:USD"], Decimal("0")
                )
                self.assertEqual(book.total_reserved("CASH:USD"), Decimal("0"))
            finally:
                DurableReservationBook._verify_resolution_evidence = original_verify


if __name__ == "__main__":
    unittest.main()
