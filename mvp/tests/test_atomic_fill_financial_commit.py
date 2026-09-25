from datetime import date
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.accounting import (
    AccountingConflict,
    book_equity_fill,
)
from mvp.autotrade_mvp.durable_reservations import DurableReservationBook
from mvp.autotrade_mvp.durable_settlement import DurableSettlementBook
from mvp.autotrade_mvp.persistence import JournalStore, canonical_json
from mvp.autotrade_mvp.provider_activity_accounting import (
    DurableProviderEconomicBook,
    commit_economic_batch_with_reservation_consumption,
)
from mvp.autotrade_mvp.reservations import ReservationConflict
from mvp.autotrade_mvp.settlement import (
    SettlementAccountScope,
    SettlementRuleBinding,
    equity_cash_obligation_from_transaction,
)


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


def settlement_book(store: JournalStore) -> DurableSettlementBook:
    return DurableSettlementBook(
        store,
        provider_id=PROVIDER,
        account_id=ACCOUNT,
        environment=ENVIRONMENT,
    )


def settlement_obligation(transaction):
    rule = SettlementRuleBinding(
        rule_id="test-equity-cash",
        rule_version="1",
        scope=SettlementAccountScope(
            provider_id=PROVIDER,
            account_id=ACCOUNT,
            environment=ENVIRONMENT,
        ),
        instrument_version="ABC",
        settlement_currency="USD",
        effective_from=date(2026, 9, 1),
        effective_to=None,
        evidence_refs=("instrument:ABC", "rule:test-equity-cash:1"),
    )
    return equity_cash_obligation_from_transaction(
        transaction,
        obligation_id="settlement-economic-fill-1",
        instrument="ABC",
        settlement_currency="USD",
        settlement_date=date(2026, 9, 26),
        rule_binding=rule,
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
    settlements: DurableSettlementBook | None = None,
):
    economic_transaction = fill_transaction() if transaction is None else transaction
    kwargs = {}
    if settlements is not None:
        kwargs = {
            "settlement_book": settlements,
            "settlement_obligations": (
                settlement_obligation(economic_transaction),
            ),
        }
    return commit_economic_batch_with_reservation_consumption(
        economics,
        reservations,
        command_id="fill-financial-command-1",
        idempotency_key="fill-financial-idempotency-1",
        reservation_id="reservation-1",
        usage={"CASH:USD": usage},
        transactions=(economic_transaction,),
        committed_at="2026-09-25T09:00:02Z",
        **kwargs,
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
                + sha256(
                    canonical_json(
                        [
                            PROVIDER,
                            ACCOUNT,
                            ENVIRONMENT,
                            "fill-financial-idempotency-1",
                        ]
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

    def test_fill_settlement_provenance_commits_and_restarts_atomically(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            reservations = reservation_book(store)
            economics = economic_book(store)
            settlements = settlement_book(store)
            reserve(reservations)

            self.assertTrue(
                commit_fill(
                    economics,
                    reservations,
                    settlements=settlements,
                )
            )
            self.assertEqual(len(settlements.obligations), 1)
            self.assertEqual(
                settlements.obligations[0].source_transaction_id,
                "economic-fill-1",
            )

            reopened_store = JournalStore(path)
            reopened_reservations = reservation_book(reopened_store)
            reopened_economics = economic_book(reopened_store)
            reopened_settlements = settlement_book(reopened_store)
            self.assertEqual(
                reopened_reservations.get("reservation-1").consumed["CASH:USD"],
                Decimal("100"),
            )
            self.assertEqual(len(reopened_economics.transactions), 1)
            self.assertEqual(len(reopened_settlements.obligations), 1)
            projected = reopened_settlements.project(reopened_economics)
            self.assertEqual(
                projected.snapshot("USD").unsettled_payable,
                Decimal("100"),
            )

    def test_atomic_fill_ack_loss_does_not_duplicate_settlement_provenance(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            reservations = reservation_book(store)
            economics = economic_book(store)
            settlements = settlement_book(store)
            reserve(reservations)

            original_commit = store.commit_command
            injected = False

            def lose_ack_after_commit(**kwargs):
                nonlocal injected
                result = original_commit(**kwargs)
                if not injected and result[1]:
                    injected = True
                    raise RuntimeError("injected settlement acknowledgement loss")
                return result

            store.commit_command = lose_ack_after_commit
            try:
                with self.assertRaisesRegex(RuntimeError, "acknowledgement loss"):
                    commit_fill(
                        economics,
                        reservations,
                        settlements=settlements,
                    )
            finally:
                store.commit_command = original_commit

            reopened_store = JournalStore(path)
            reopened_reservations = reservation_book(reopened_store)
            reopened_economics = economic_book(reopened_store)
            reopened_settlements = settlement_book(reopened_store)
            self.assertFalse(
                commit_fill(
                    reopened_economics,
                    reopened_reservations,
                    settlements=reopened_settlements,
                )
            )
            self.assertEqual(len(reopened_economics.transactions), 1)
            self.assertEqual(len(reopened_settlements.obligations), 1)
            self.assertEqual(
                reopened_reservations.get("reservation-1").consumed["CASH:USD"],
                Decimal("100"),
            )

    def test_preexisting_settlement_without_fill_pair_fails_closed(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            reservations = reservation_book(store)
            economics = economic_book(store)
            settlements = settlement_book(store)
            reserve(reservations)
            transaction = fill_transaction()
            obligation = settlement_obligation(transaction)
            self.assertTrue(
                settlements.register_obligations(
                    (obligation,),
                    command_id="legacy-settlement-only",
                    idempotency_key="legacy-settlement-only",
                    committed_at="2026-09-25T09:00:01Z",
                )
            )

            with self.assertRaisesRegex(
                AccountingConflict,
                "partially committed",
            ):
                commit_fill(
                    economics,
                    reservations,
                    transaction=transaction,
                    settlements=settlements,
                )

            self.assertEqual(economics.transactions, ())
            self.assertEqual(
                reservations.get("reservation-1").consumed["CASH:USD"],
                Decimal("0"),
            )
            self.assertEqual(len(settlements.obligations), 1)

    def test_failure_before_three_way_commit_leaves_settlement_unregistered(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            reservations = reservation_book(store)
            economics = economic_book(store)
            settlements = settlement_book(store)
            reserve(reservations)

            original_commit = store.commit_command

            def fail_before_commit(**kwargs):
                raise RuntimeError("injected three-way pre-commit failure")

            store.commit_command = fail_before_commit
            try:
                with self.assertRaisesRegex(RuntimeError, "pre-commit"):
                    commit_fill(
                        economics,
                        reservations,
                        settlements=settlements,
                    )
            finally:
                store.commit_command = original_commit

            reopened_store = JournalStore(path)
            self.assertEqual(economic_book(reopened_store).transactions, ())
            self.assertEqual(
                reservation_book(reopened_store)
                .get("reservation-1")
                .consumed["CASH:USD"],
                Decimal("0"),
            )
            self.assertEqual(settlement_book(reopened_store).obligations, ())

    def test_settlement_coverage_must_include_every_fill_cash_currency(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            reservations = reservation_book(store)
            economics = economic_book(store)
            settlements = settlement_book(store)
            reserve(reservations)
            transaction = book_equity_fill(
                transaction_id="economic-fill-1",
                cause_event_id="provider-execution-1",
                instrument="ABC",
                settlement_currency="USD",
                side="BUY",
                quantity="1",
                price="100",
                fee="1",
                fee_currency="EUR",
                economic_effective_at="2026-09-25T09:00:00Z",
                economic_order_key="provider:PROVIDER-A:execution:provider-execution-1",
                observed_at="2026-09-25T09:00:01Z",
            )
            with self.assertRaisesRegex(
                AccountingConflict,
                "cover every atomic fill cash leg",
            ):
                commit_fill(
                    economics,
                    reservations,
                    transaction=transaction,
                    settlements=settlements,
                )
            self.assertEqual(economics.transactions, ())
            self.assertEqual(settlements.obligations, ())
            self.assertEqual(
                reservations.get("reservation-1").consumed["CASH:USD"],
                Decimal("0"),
            )

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
