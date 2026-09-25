from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.accounting import (
    AccountingConflict,
    book_equity_fill,
)
from mvp.autotrade_mvp.durable_reservations import DurableReservationBook
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.provider_activity_accounting import (
    DurableProviderEconomicBook,
    commit_economic_batch_with_reservation_consumption,
)
from mvp.autotrade_mvp.reservations import ReservationConflict


PROVIDER = "PROVIDER-A"
ACCOUNT = "acct-1"
ENVIRONMENT = "PAPER"


def reservation_book(store: JournalStore) -> DurableReservationBook:
    return DurableReservationBook(
        store,
        environment=ENVIRONMENT,
        account_id=ACCOUNT,
    )


def economic_book(store: JournalStore) -> DurableProviderEconomicBook:
    return DurableProviderEconomicBook(
        store,
        provider_id=PROVIDER,
        account_id=ACCOUNT,
        environment=ENVIRONMENT,
    )


def reserve(book: DurableReservationBook) -> None:
    book.reserve(
        command_id="reserve-command",
        idempotency_key="reserve-idempotency",
        reservation_id="reservation-1",
        intent_id="intent-1",
        requirements={"CASH:USD": "120"},
        available={"CASH:USD": "1000"},
    )


def fill_transaction(*, price: str = "100"):
    return book_equity_fill(
        transaction_id="economic-fill-1",
        cause_event_id="provider-execution-1",
        instrument="ABC",
        settlement_currency="USD",
        side="BUY",
        quantity="1",
        price=price,
        economic_effective_at="2026-09-25T09:00:00Z",
        economic_order_key="provider:PROVIDER-A:execution:provider-execution-1",
        observed_at="2026-09-25T09:00:01Z",
    )


def commit_fill(
    economics: DurableProviderEconomicBook,
    reservations: DurableReservationBook,
    *,
    usage: str = "100",
    transaction=None,
):
    return commit_economic_batch_with_reservation_consumption(
        economics,
        reservations,
        command_id="fill-financial-command-1",
        idempotency_key="fill-financial-idempotency-1",
        reservation_id="reservation-1",
        usage={"CASH:USD": usage},
        transactions=(fill_transaction() if transaction is None else transaction,),
        committed_at="2026-09-25T09:00:02Z",
    )


class AtomicFillFinancialCommitTests(unittest.TestCase):
    def test_fill_economics_and_reservation_consumption_restart_together(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            reservations = reservation_book(store)
            economics = economic_book(store)
            reserve(reservations)

            self.assertTrue(commit_fill(economics, reservations))
            snapshot = reservations.get("reservation-1")
            self.assertEqual(snapshot.consumed["CASH:USD"], Decimal("100"))
            self.assertEqual(snapshot.remaining["CASH:USD"], Decimal("20"))
            self.assertEqual(economics.position("ABC"), Decimal("1"))

            reopened_store = JournalStore(path)
            reopened_reservations = reservation_book(reopened_store)
            reopened_economics = economic_book(reopened_store)
            reopened_snapshot = reopened_reservations.get("reservation-1")
            self.assertEqual(
                reopened_snapshot.consumed["CASH:USD"],
                Decimal("100"),
            )
            self.assertEqual(
                reopened_snapshot.remaining["CASH:USD"],
                Decimal("20"),
            )
            self.assertEqual(reopened_economics.position("ABC"), Decimal("1"))
            self.assertEqual(len(reopened_economics.transactions), 1)

    def test_failure_before_shared_commit_leaves_neither_projection_mutated(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            reservations = reservation_book(store)
            economics = economic_book(store)
            reserve(reservations)

            original_commit = store.commit_command

            def fail_before_commit(**kwargs):
                raise RuntimeError("injected pre-commit failure")

            store.commit_command = fail_before_commit
            try:
                with self.assertRaisesRegex(RuntimeError, "pre-commit"):
                    commit_fill(economics, reservations)
            finally:
                store.commit_command = original_commit

            reopened_store = JournalStore(path)
            reopened_reservations = reservation_book(reopened_store)
            reopened_economics = economic_book(reopened_store)
            snapshot = reopened_reservations.get("reservation-1")
            self.assertEqual(snapshot.consumed["CASH:USD"], Decimal("0"))
            self.assertEqual(snapshot.remaining["CASH:USD"], Decimal("120"))
            self.assertEqual(reopened_economics.transactions, ())

    def test_ack_loss_after_shared_commit_retries_without_duplicate(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            reservations = reservation_book(store)
            economics = economic_book(store)
            reserve(reservations)

            original_commit = store.commit_command
            injected = False

            def lose_ack_after_commit(**kwargs):
                nonlocal injected
                result = original_commit(**kwargs)
                if not injected and result[1]:
                    injected = True
                    raise RuntimeError("injected acknowledgement loss")
                return result

            store.commit_command = lose_ack_after_commit
            try:
                with self.assertRaisesRegex(RuntimeError, "acknowledgement loss"):
                    commit_fill(economics, reservations)
            finally:
                store.commit_command = original_commit

            reopened_store = JournalStore(path)
            reopened_reservations = reservation_book(reopened_store)
            reopened_economics = economic_book(reopened_store)
            self.assertFalse(
                commit_fill(reopened_economics, reopened_reservations)
            )
            snapshot = reopened_reservations.get("reservation-1")
            self.assertEqual(snapshot.consumed["CASH:USD"], Decimal("100"))
            self.assertEqual(snapshot.remaining["CASH:USD"], Decimal("20"))
            self.assertEqual(reopened_economics.position("ABC"), Decimal("1"))
            self.assertEqual(len(reopened_economics.transactions), 1)

    def test_preexisting_economics_without_reservation_consumption_fails_closed(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            reservations = reservation_book(store)
            economics = economic_book(store)
            reserve(reservations)
            self.assertTrue(economics.append(fill_transaction()))

            with self.assertRaisesRegex(
                AccountingConflict,
                "partially committed",
            ):
                commit_fill(economics, reservations)

            snapshot = reservations.get("reservation-1")
            self.assertEqual(snapshot.consumed["CASH:USD"], Decimal("0"))
            self.assertEqual(snapshot.remaining["CASH:USD"], Decimal("120"))
            self.assertEqual(economics.position("ABC"), Decimal("1"))

    def test_exact_retry_cannot_change_reserved_usage(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            reservations = reservation_book(store)
            economics = economic_book(store)
            reserve(reservations)
            self.assertTrue(commit_fill(economics, reservations))

            with self.assertRaisesRegex(
                ReservationConflict,
                "different reservation request",
            ):
                commit_fill(economics, reservations, usage="101")

            snapshot = reservations.get("reservation-1")
            self.assertEqual(snapshot.consumed["CASH:USD"], Decimal("100"))
            self.assertEqual(snapshot.remaining["CASH:USD"], Decimal("20"))
            self.assertEqual(len(economics.transactions), 1)

    def test_preexisting_reservation_consumption_without_economics_fails_closed(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            reservations = reservation_book(store)
            economics = economic_book(store)
            reserve(reservations)

            component_key = (
                "atomic-fill-reservation:"
                + __import__("hashlib").sha256(
                    __import__("json").dumps(
                        [
                            PROVIDER,
                            ACCOUNT,
                            ENVIRONMENT,
                            "fill-financial-idempotency-1",
                        ],
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
            )
            # Use the public preparation seam to emulate a legacy/crashed
            # partial integration without mutating the economic aggregate.
            plan = reservations.prepare_consume_mutation(
                event_key="partial-reservation-event",
                idempotency_key=component_key,
                reservation_id="reservation-1",
                usage={"CASH:USD": "100"},
                committed_at="2026-09-25T09:00:02Z",
            )
            self.assertIsNotNone(plan.envelope)
            store.commit_command(
                command_id="partial-reservation-only",
                actor="test-partial-financial-state",
                environment=ENVIRONMENT,
                idempotency_key="partial-reservation-only",
                request=plan.request,
                result=plan.snapshot_payload,
                state_version=plan.aggregate_version,
                events=[(plan.envelope, None)],
            )
            reservations.refresh()

            with self.assertRaisesRegex(
                AccountingConflict,
                "partially committed",
            ):
                commit_fill(economics, reservations)

            snapshot = reservations.get("reservation-1")
            self.assertEqual(snapshot.consumed["CASH:USD"], Decimal("100"))
            self.assertEqual(snapshot.remaining["CASH:USD"], Decimal("20"))
            self.assertEqual(economics.transactions, ())

    def test_cross_scope_books_are_rejected_before_mutation(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            reservations = DurableReservationBook(
                store,
                environment=ENVIRONMENT,
                account_id="other-account",
            )
            economics = economic_book(store)
            reservations.reserve(
                command_id="reserve-other",
                idempotency_key="reserve-other",
                reservation_id="reservation-1",
                intent_id="intent-1",
                requirements={"CASH:USD": "120"},
                available={"CASH:USD": "1000"},
            )

            with self.assertRaisesRegex(ValueError, "account/environment"):
                commit_fill(economics, reservations)
            self.assertEqual(economics.transactions, ())
            self.assertEqual(
                reservations.get("reservation-1").remaining["CASH:USD"],
                Decimal("120"),
            )


if __name__ == "__main__":
    unittest.main()
