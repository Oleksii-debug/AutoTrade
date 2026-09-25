from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.accounting import (
    AccountingConflict,
    EconomicBook,
    ScopedEconomicBook,
    book_equity_fill,
    canonical_transaction,
    project_equity_position,
)
from mvp.autotrade_mvp.fill_accounting import (
    ProjectedFillEvidence,
    book_provider_fill,
    book_provider_fill_correction,
)
from mvp.autotrade_mvp.persistence import JournalStore, payload_digest
from mvp.autotrade_mvp.provider_activity_accounting import (
    DurableProviderEconomicBook,
    _book_id,
    _economic_batch_digest,
)
from mvp.autotrade_mvp.reconciliation import ProviderFillEvidence


PROVIDER = "PROVIDER-A"
ACCOUNT = "acct-1"
ENVIRONMENT = "PAPER"


def fill_evidence(
    *,
    fill_id,
    execution_id,
    client_order_id,
    side,
    quantity,
    price,
    trade_time,
    provider_revision=None,
    correction_of=None,
    provider_id=PROVIDER,
):
    projected = ProjectedFillEvidence.create(
        fill_id=fill_id,
        provider_execution_id=execution_id,
        intent_id=f"intent:{execution_id}",
        client_order_id=client_order_id,
        side=side,
        quantity=quantity,
        price=price,
        provider_revision=provider_revision,
        correction_of=correction_of,
    )
    provider = ProviderFillEvidence.create(
        provider_id=provider_id,
        account_id=ACCOUNT,
        environment=ENVIRONMENT,
        provider_execution_id=execution_id,
        client_order_id=client_order_id,
        instrument="ABC",
        quantity=quantity,
        price=price,
        fee_amount="0",
        fee_currency="USD",
        trade_time=trade_time,
    )
    return projected, provider


def root_buy():
    return fill_evidence(
        fill_id="fill-buy",
        execution_id="exec-buy",
        client_order_id="client-buy",
        side="BUY",
        quantity="2",
        price="100",
        trade_time="2026-01-01T10:00:00Z",
    )


def later_sell():
    return fill_evidence(
        fill_id="fill-sell",
        execution_id="exec-sell",
        client_order_id="client-sell",
        side="SELL",
        quantity="1",
        price="110",
        trade_time="2026-01-02T10:00:00Z",
    )


def correction_one():
    return fill_evidence(
        fill_id="fill-buy-r2",
        execution_id="exec-buy",
        client_order_id="client-buy",
        side="BUY",
        quantity="2",
        price="101",
        trade_time="2026-01-01T10:00:00Z",
        provider_revision="revision-2",
        correction_of="fill-buy",
    )


def correction_two():
    return fill_evidence(
        fill_id="fill-buy-r3",
        execution_id="exec-buy",
        client_order_id="client-buy",
        side="BUY",
        quantity="2",
        price="102",
        trade_time="2026-01-01T10:00:00Z",
        provider_revision="revision-3",
        correction_of="fill-buy-r2",
    )


def durable_book(store):
    return DurableProviderEconomicBook(
        store,
        provider_id=PROVIDER,
        account_id=ACCOUNT,
        environment=ENVIRONMENT,
    )


def book_fill(book, evidence, *, observed_at):
    projected, provider = evidence
    return book_provider_fill(
        book=book,
        provider_id=PROVIDER,
        projected_fill=projected,
        provider_fill=provider,
        expected_instrument="ABC",
        settlement_currency="USD",
        observed_at=observed_at,
    )


def correct(book, original, corrected, *, observed_at):
    original_projected, original_provider = original
    corrected_projected, corrected_provider = corrected
    return book_provider_fill_correction(
        book=book,
        provider_id=PROVIDER,
        original_projected_fill=original_projected,
        original_provider_fill=original_provider,
        corrected_projected_fill=corrected_projected,
        corrected_provider_fill=corrected_provider,
        expected_instrument="ABC",
        settlement_currency="USD",
        correction_observed_at=observed_at,
    )


def projection(book):
    return project_equity_position(
        EconomicBook(book.transactions),
        instrument="ABC",
        settlement_currency="USD",
        mark_price="105",
    )


class DurableFillAccountingTests(unittest.TestCase):
    def test_provider_fill_is_durable_and_exact_restart_retry_is_noop(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            book = durable_book(store)
            buy = root_buy()

            self.assertTrue(
                book_fill(
                    book,
                    buy,
                    observed_at="2026-01-01T10:00:01Z",
                )
            )
            reopened = durable_book(JournalStore(path))
            self.assertEqual(reopened.position("ABC"), Decimal("2"))
            self.assertFalse(
                book_fill(
                    reopened,
                    buy,
                    observed_at="2026-01-01T10:00:01Z",
                )
            )
            events = reopened.store.load_events(
                "economic_book",
                _book_id(
                    provider_id=PROVIDER,
                    account_id=ACCOUNT,
                    environment=ENVIRONMENT,
                ),
            )
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["event_type"], "EconomicTransactionBatchBooked")

    def test_correction_batch_reopens_with_effective_time_fifo_restatement(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            book = durable_book(JournalStore(path))
            buy = root_buy()
            sell = later_sell()
            corrected = correction_one()
            self.assertTrue(
                book_fill(book, buy, observed_at="2026-01-01T10:00:01Z")
            )
            self.assertTrue(
                book_fill(book, sell, observed_at="2026-01-02T10:00:01Z")
            )
            self.assertTrue(
                correct(
                    book,
                    buy,
                    corrected,
                    observed_at="2026-01-03T10:00:00Z",
                )
            )

            reopened = durable_book(JournalStore(path))
            state = projection(reopened)
            self.assertEqual(state.quantity, Decimal("1"))
            self.assertEqual(state.open_cost_basis, Decimal("101"))
            self.assertEqual(state.realized_pnl, Decimal("9"))
            self.assertEqual(state.unrealized_pnl, Decimal("4"))
            self.assertEqual(len(reopened.transactions), 4)
            self.assertFalse(
                correct(
                    reopened,
                    buy,
                    corrected,
                    observed_at="2026-01-03T10:00:00Z",
                )
            )

    def test_failure_before_durable_commit_leaves_neither_correction_member(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            book = durable_book(store)
            buy = root_buy()
            sell = later_sell()
            corrected = correction_one()
            book_fill(book, buy, observed_at="2026-01-01T10:00:01Z")
            book_fill(book, sell, observed_at="2026-01-02T10:00:01Z")
            before = tuple(book.transactions)

            original_commit = store.commit_command
            def fail_before_commit(**kwargs):
                raise RuntimeError("injected pre-commit failure")
            store.commit_command = fail_before_commit
            try:
                with self.assertRaisesRegex(RuntimeError, "pre-commit"):
                    correct(
                        book,
                        buy,
                        corrected,
                        observed_at="2026-01-03T10:00:00Z",
                    )
            finally:
                store.commit_command = original_commit

            reopened = durable_book(JournalStore(path))
            self.assertEqual(reopened.transactions, before)
            state = projection(reopened)
            self.assertEqual(state.open_cost_basis, Decimal("100"))
            self.assertEqual(state.realized_pnl, Decimal("10"))

    def test_post_commit_ack_loss_replays_committed_correction_without_duplicate(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            book = durable_book(store)
            buy = root_buy()
            sell = later_sell()
            corrected = correction_one()
            book_fill(book, buy, observed_at="2026-01-01T10:00:01Z")
            book_fill(book, sell, observed_at="2026-01-02T10:00:01Z")

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
                    correct(
                        book,
                        buy,
                        corrected,
                        observed_at="2026-01-03T10:00:00Z",
                    )
            finally:
                store.commit_command = original_commit

            reopened = durable_book(JournalStore(path))
            self.assertEqual(projection(reopened).realized_pnl, Decimal("9"))
            self.assertFalse(
                correct(
                    reopened,
                    buy,
                    corrected,
                    observed_at="2026-01-03T10:00:00Z",
                )
            )
            events = reopened.store.load_events(
                "economic_book",
                reopened.book_id,
            )
            self.assertEqual(len(events), 3)
            self.assertEqual(len(reopened.transactions), 4)

    def test_second_correction_after_restart_targets_durable_active_head(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            book = durable_book(JournalStore(path))
            buy = root_buy()
            sell = later_sell()
            corrected_one = correction_one()
            corrected_two = correction_two()
            book_fill(book, buy, observed_at="2026-01-01T10:00:01Z")
            book_fill(book, sell, observed_at="2026-01-02T10:00:01Z")
            correct(
                book,
                buy,
                corrected_one,
                observed_at="2026-01-03T10:00:00Z",
            )

            reopened = durable_book(JournalStore(path))
            self.assertTrue(
                correct(
                    reopened,
                    corrected_one,
                    corrected_two,
                    observed_at="2026-01-04T10:00:00Z",
                )
            )
            final = durable_book(JournalStore(path))
            state = projection(final)
            self.assertEqual(state.quantity, Decimal("1"))
            self.assertEqual(state.open_cost_basis, Decimal("102"))
            self.assertEqual(state.realized_pnl, Decimal("8"))
            self.assertEqual(state.unrealized_pnl, Decimal("3"))
            self.assertFalse(
                correct(
                    final,
                    corrected_one,
                    corrected_two,
                    observed_at="2026-01-04T10:00:00Z",
                )
            )

    def test_durable_book_rejects_cross_provider_fill_before_mutation(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            book = durable_book(store)
            projected, provider = fill_evidence(
                fill_id="foreign-fill",
                execution_id="foreign-exec",
                client_order_id="foreign-client",
                side="BUY",
                quantity="1",
                price="10",
                trade_time="2026-01-01T10:00:00Z",
                provider_id="PROVIDER-B",
            )
            with self.assertRaisesRegex(
                AccountingConflict,
                "durable economic book",
            ):
                book_provider_fill(
                    book=book,
                    provider_id="PROVIDER-B",
                    projected_fill=projected,
                    provider_fill=provider,
                    expected_instrument="ABC",
                    settlement_currency="USD",
                    observed_at="2026-01-01T10:00:01Z",
                )
            self.assertEqual(book.transactions, ())

    def test_mixed_durable_batch_is_corruption_not_auto_completed(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            book = durable_book(store)
            buy = root_buy()
            book_fill(book, buy, observed_at="2026-01-01T10:00:01Z")
            existing = book.transactions[0]
            uncommitted = book_equity_fill(
                transaction_id="manual-sell",
                cause_event_id="manual-sell-cause",
                instrument="ABC",
                settlement_currency="USD",
                side="SELL",
                quantity="1",
                price="110",
                economic_effective_at="2026-01-02T10:00:00Z",
                economic_order_key="provider:PROVIDER-A:execution:manual-sell",
                observed_at="2026-01-02T10:00:01Z",
            )
            batch = (existing, uncommitted)
            tx_payloads = [canonical_transaction(item) for item in batch]
            batch_digest = _economic_batch_digest(
                provider_id=PROVIDER,
                account_id=ACCOUNT,
                environment=ENVIRONMENT,
                transactions=batch,
            )
            payload = {
                "schema_version": "1.0.0",
                "provider_id": PROVIDER,
                "account_id": ACCOUNT,
                "environment": ENVIRONMENT,
                "batch_digest": batch_digest,
                "previous_book_digest": book.audit_digest(),
                "resulting_book_digest": "sha256:" + "0" * 64,
                "transactions": tx_payloads,
            }
            store.append_event(
                {
                    "event_id": "corrupt-mixed-economic-batch",
                    "event_type": "EconomicTransactionBatchBooked",
                    "aggregate_type": "economic_book",
                    "aggregate_id": book.book_id,
                    "aggregate_version": "2",
                    "committed_at": "2026-01-02T10:00:02Z",
                    "payload": payload,
                    "payload_hash": payload_digest(payload),
                }
            )
            with self.assertRaisesRegex(
                AccountingConflict,
                "partially committed",
            ):
                durable_book(JournalStore(path))


if __name__ == "__main__":
    unittest.main()
