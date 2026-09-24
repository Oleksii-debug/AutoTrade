from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.persistence import JournalStore, payload_digest
from mvp.autotrade_mvp.provider_activity_accounting import (
    book_external_provider_cash_activity,
    load_provider_account_economic_book,
)
from mvp.autotrade_mvp.reconciliation import ProviderActivityEvidence


def activity(
    *,
    activity_id="cash-1",
    activity_type="DEPOSIT",
    origin="EXTERNAL",
    currency="USD",
    occurred_at="2026-09-24T18:00:00Z",
):
    return ProviderActivityEvidence.create(
        activity_id=activity_id,
        activity_type=activity_type,
        origin=origin,
        occurred_at=occurred_at,
        currency=currency,
    )


class ProviderActivityAccountingTests(unittest.TestCase):
    def test_external_deposit_is_atomically_booked_once(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            transaction, inserted = book_external_provider_cash_activity(
                store,
                provider_id="ALPACA",
                account_id="paper-1",
                activity=activity(),
                amount="250.50",
                observed_at="2026-09-24T18:01:00Z",
            )

            self.assertTrue(inserted)
            self.assertEqual(transaction.cause_event_id, "provider-activity:ALPACA/paper-1/cash-1")
            events = store.load_events("provider_activity", "ALPACA/paper-1/cash-1")
            economics = store.load_events("economic_book", "ALPACA/paper-1")
            self.assertEqual(len(events), 1)
            self.assertEqual(len(economics), 1)
            self.assertEqual(events[0]["event_type"], "ProviderActivityImported")
            self.assertEqual(economics[0]["event_type"], "EconomicTransactionBooked")

            book = load_provider_account_economic_book(
                store,
                provider_id="ALPACA",
                account_id="paper-1",
            )
            self.assertEqual(book.cash("USD"), Decimal("250.5"))
            self.assertEqual(
                book.balance("EXTERNAL_EQUITY:USD", "USD"),
                Decimal("-250.5"),
            )

    def test_exact_retry_after_restart_is_noop(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            first, inserted = book_external_provider_cash_activity(
                store,
                provider_id="IBKR",
                account_id="acct-7",
                activity=activity(activity_id="dep-7"),
                amount="100",
                observed_at="2026-09-24T18:02:00Z",
            )
            self.assertTrue(inserted)

            reopened = JournalStore(path)
            second, replay_inserted = book_external_provider_cash_activity(
                reopened,
                provider_id="IBKR",
                account_id="acct-7",
                activity=activity(activity_id="dep-7"),
                amount=Decimal("100.00"),
                observed_at="2026-09-24T18:02:00+00:00",
            )
            self.assertFalse(replay_inserted)
            self.assertEqual(first, second)
            self.assertEqual(
                len(reopened.load_events("economic_book", "IBKR/acct-7")),
                1,
            )
            book = load_provider_account_economic_book(
                reopened,
                provider_id="IBKR",
                account_id="acct-7",
            )
            self.assertEqual(book.cash("USD"), Decimal("100"))

    def test_same_identity_with_changed_amount_fails_closed(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            kwargs = dict(
                provider_id="KRAKEN",
                account_id="acct",
                activity=activity(activity_id="dep-conflict"),
                observed_at="2026-09-24T18:03:00Z",
            )
            book_external_provider_cash_activity(store, amount="10", **kwargs)
            with self.assertRaisesRegex(ValueError, "idempotency_key"):
                book_external_provider_cash_activity(store, amount="11", **kwargs)

            book = load_provider_account_economic_book(
                store,
                provider_id="KRAKEN",
                account_id="acct",
            )
            self.assertEqual(book.cash("USD"), Decimal("10"))
            self.assertEqual(len(book.transactions), 1)

    def test_withdrawal_requires_negative_amount_and_books_exactly(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            withdrawal = activity(
                activity_id="wd-1",
                activity_type="WITHDRAWAL",
            )
            transaction, inserted = book_external_provider_cash_activity(
                store,
                provider_id="BINANCE",
                account_id="acct",
                activity=withdrawal,
                amount="-25.125",
                observed_at="2026-09-24T18:04:00Z",
            )
            self.assertTrue(inserted)
            book = load_provider_account_economic_book(
                store,
                provider_id="BINANCE",
                account_id="acct",
            )
            self.assertEqual(book.cash("USD"), Decimal("-25.125"))
            self.assertEqual(transaction.postings[0].signed_amount, Decimal("-25.125"))

            with self.assertRaisesRegex(ValueError, "WITHDRAWAL amount"):
                book_external_provider_cash_activity(
                    store,
                    provider_id="BINANCE",
                    account_id="acct",
                    activity=activity(
                        activity_id="wd-2",
                        activity_type="WITHDRAWAL",
                    ),
                    amount="1",
                    observed_at="2026-09-24T18:04:00Z",
                )

    def test_deposit_rejects_nonpositive_and_binary_float(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            with self.assertRaisesRegex(ValueError, "DEPOSIT amount"):
                book_external_provider_cash_activity(
                    store,
                    provider_id="ALPACA",
                    account_id="acct",
                    activity=activity(activity_id="dep-negative"),
                    amount="-1",
                    observed_at="2026-09-24T18:05:00Z",
                )
            with self.assertRaises(TypeError):
                book_external_provider_cash_activity(
                    store,
                    provider_id="ALPACA",
                    account_id="acct",
                    activity=activity(activity_id="dep-float"),
                    amount=1.25,
                    observed_at="2026-09-24T18:05:00Z",
                )
            self.assertEqual(store.load_events("economic_book", "ALPACA/acct"), [])

    def test_unknown_autotrade_and_unqualified_activity_types_are_not_booked(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            for candidate in (
                activity(activity_id="unknown", origin="UNKNOWN"),
                activity(activity_id="auto", origin="AUTOTRADE"),
            ):
                with self.assertRaisesRegex(ValueError, "MANUAL or EXTERNAL"):
                    book_external_provider_cash_activity(
                        store,
                        provider_id="BYBIT",
                        account_id="acct",
                        activity=candidate,
                        amount="5",
                        observed_at="2026-09-24T18:06:00Z",
                    )

            with self.assertRaisesRegex(ValueError, "not qualified"):
                book_external_provider_cash_activity(
                    store,
                    provider_id="BYBIT",
                    account_id="acct",
                    activity=activity(
                        activity_id="adjustment",
                        activity_type="CASH_ADJUSTMENT",
                    ),
                    amount="5",
                    observed_at="2026-09-24T18:06:00Z",
                )
            self.assertEqual(store.load_events("economic_book", "BYBIT/acct"), [])

    def test_currency_and_evidence_time_are_required(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            with self.assertRaisesRegex(ValueError, "requires currency"):
                book_external_provider_cash_activity(
                    store,
                    provider_id="IBKR",
                    account_id="acct",
                    activity=activity(activity_id="no-currency", currency=None),
                    amount="5",
                    observed_at="2026-09-24T18:07:00Z",
                )
            with self.assertRaisesRegex(ValueError, "must not precede"):
                book_external_provider_cash_activity(
                    store,
                    provider_id="IBKR",
                    account_id="acct",
                    activity=activity(
                        activity_id="future",
                        occurred_at="2026-09-24T19:00:00Z",
                    ),
                    amount="5",
                    observed_at="2026-09-24T18:07:00Z",
                )

    def test_same_provider_activity_id_is_scoped_by_account(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            evidence = activity(activity_id="shared-id")
            for account, amount in (("a", "10"), ("b", "20")):
                _, inserted = book_external_provider_cash_activity(
                    store,
                    provider_id="ALPACA",
                    account_id=account,
                    activity=evidence,
                    amount=amount,
                    observed_at="2026-09-24T18:08:00Z",
                )
                self.assertTrue(inserted)

            self.assertEqual(
                load_provider_account_economic_book(
                    store, provider_id="ALPACA", account_id="a"
                ).cash("USD"),
                Decimal("10"),
            )
            self.assertEqual(
                load_provider_account_economic_book(
                    store, provider_id="ALPACA", account_id="b"
                ).cash("USD"),
                Decimal("20"),
            )

    def test_multiple_events_rebuild_in_order_after_restart(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            book_external_provider_cash_activity(
                store,
                provider_id="WHITEBIT",
                account_id="acct",
                activity=activity(activity_id="dep"),
                amount="100",
                observed_at="2026-09-24T18:09:00Z",
            )
            book_external_provider_cash_activity(
                store,
                provider_id="WHITEBIT",
                account_id="acct",
                activity=activity(
                    activity_id="wd",
                    activity_type="WITHDRAWAL",
                    occurred_at="2026-09-24T18:10:00Z",
                ),
                amount="-40",
                observed_at="2026-09-24T18:11:00Z",
            )

            reopened = JournalStore(path)
            book = load_provider_account_economic_book(
                reopened,
                provider_id="WHITEBIT",
                account_id="acct",
            )
            self.assertEqual(book.cash("USD"), Decimal("60"))
            self.assertEqual(len(book.transactions), 2)

    def test_unrecognized_durable_economic_event_fails_closed(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            payload = {"unexpected": True}
            store.append_event(
                {
                    "event_id": "bad-economic-event",
                    "event_type": "UnexpectedEconomicEvent",
                    "aggregate_type": "economic_book",
                    "aggregate_id": "ALPACA/acct",
                    "aggregate_version": 1,
                    "committed_at": "2026-09-24T18:12:00Z",
                    "payload": payload,
                    "payload_hash": payload_digest(payload),
                }
            )
            with self.assertRaisesRegex(ValueError, "unsupported durable event"):
                load_provider_account_economic_book(
                    store,
                    provider_id="ALPACA",
                    account_id="acct",
                )


if __name__ == "__main__":
    unittest.main()
