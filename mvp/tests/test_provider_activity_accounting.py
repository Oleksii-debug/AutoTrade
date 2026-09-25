from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.persistence import JournalStore, payload_digest
from mvp.autotrade_mvp.provider_activity_accounting import (
    _activity_identity,
    _book_id,
    book_external_provider_cash_activity,
    load_provider_account_economic_book,
)
from mvp.autotrade_mvp.reconciliation import ProviderActivityEvidence


def activity(
    *,
    provider_id="ALPACA",
    account_id="paper-1",
    activity_id="cash-1",
    activity_type="DEPOSIT",
    origin="EXTERNAL",
    currency="USD",
    occurred_at="2026-09-24T18:00:00Z",
    instrument=None,
    client_order_id=None,
    provider_order_id=None,
    provider_execution_id=None,
):
    return ProviderActivityEvidence.create(
        provider_id=provider_id,
        account_id=account_id,
        activity_id=activity_id,
        activity_type=activity_type,
        origin=origin,
        occurred_at=occurred_at,
        currency=currency,
        instrument=instrument,
        client_order_id=client_order_id,
        provider_order_id=provider_order_id,
        provider_execution_id=provider_execution_id,
    )


def paper_activity_identity(**kwargs):
    return _activity_identity(environment="PAPER", **kwargs)


def paper_book_id(**kwargs):
    return _book_id(environment="PAPER", **kwargs)


def book_paper_activity(store, **kwargs):
    return book_external_provider_cash_activity(
        store,
        environment="PAPER",
        **kwargs,
    )


def load_paper_book(store, **kwargs):
    return load_provider_account_economic_book(
        store,
        environment="PAPER",
        **kwargs,
    )


class ProviderActivityAccountingTests(unittest.TestCase):
    def test_environment_is_part_of_durable_provider_activity_identity(self):
        paper = paper_activity_identity(
            provider_id="ALPACA",
            account_id="shared-account",
            activity_id="shared-id",
        )
        live = _activity_identity(
            provider_id="ALPACA",
            account_id="shared-account",
            environment="LIVE",
            activity_id="shared-id",
        )
        self.assertNotEqual(paper, live)
        self.assertNotEqual(
            paper_book_id(provider_id="ALPACA", account_id="shared-account"),
            _book_id(
                provider_id="ALPACA",
                account_id="shared-account",
                environment="LIVE",
            ),
        )

    def test_bridge_requires_canonical_environment_scope(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            with self.assertRaisesRegex(ValueError, "environment must be"):
                book_external_provider_cash_activity(
                    store,
                    provider_id="ALPACA",
                    account_id="paper-1",
                    environment="UNKNOWN",
                    activity=activity(),
                    amount="1",
                    observed_at="2026-09-24T18:01:00Z",
                )

    def test_external_deposit_is_atomically_booked_once(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            transaction, inserted = book_paper_activity(
                store,
                provider_id="ALPACA",
                account_id="paper-1",
                activity=activity(),
                amount="250.50",
                observed_at="2026-09-24T18:01:00Z",
            )

            self.assertTrue(inserted)
            activity_identity = paper_activity_identity(
                provider_id="ALPACA",
                account_id="paper-1",
                activity_id="cash-1",
            )
            book_identity = paper_book_id(provider_id="ALPACA", account_id="paper-1")
            self.assertEqual(
                transaction.cause_event_id,
                f"provider-activity:{activity_identity}",
            )
            events = store.load_events("provider_activity", activity_identity)
            economics = store.load_events("economic_book", book_identity)
            self.assertEqual(len(events), 1)
            self.assertEqual(len(economics), 1)
            self.assertEqual(events[0]["event_type"], "ProviderActivityImported")
            self.assertEqual(economics[0]["event_type"], "EconomicTransactionBooked")

            book = load_paper_book(
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
            first, inserted = book_paper_activity(
                store,
                provider_id="IBKR",
                account_id="acct-7",
                activity=activity(provider_id="IBKR", account_id="acct-7", activity_id="dep-7"),
                amount="100",
                observed_at="2026-09-24T18:02:00Z",
            )
            self.assertTrue(inserted)

            reopened = JournalStore(path)
            second, replay_inserted = book_paper_activity(
                reopened,
                provider_id="IBKR",
                account_id="acct-7",
                activity=activity(provider_id="IBKR", account_id="acct-7", activity_id="dep-7"),
                amount=Decimal("100.00"),
                observed_at="2026-09-24T18:02:00+00:00",
            )
            self.assertFalse(replay_inserted)
            self.assertEqual(first, second)
            self.assertEqual(
                len(
                    reopened.load_events(
                        "economic_book",
                        paper_book_id(provider_id="IBKR", account_id="acct-7"),
                    )
                ),
                1,
            )
            book = load_paper_book(
                reopened,
                provider_id="IBKR",
                account_id="acct-7",
            )
            self.assertEqual(book.cash("USD"), Decimal("100"))

    def test_same_activity_repolled_later_is_idempotent(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            evidence = activity(provider_id="IBKR", account_id="acct-repoll", activity_id="dep-repoll")
            first, inserted = book_paper_activity(
                store,
                provider_id="IBKR",
                account_id="acct-repoll",
                activity=evidence,
                amount="100",
                observed_at="2026-09-24T18:02:00Z",
            )
            self.assertTrue(inserted)

            second, replay_inserted = book_paper_activity(
                store,
                provider_id="IBKR",
                account_id="acct-repoll",
                activity=evidence,
                amount="100.00",
                observed_at="2026-09-24T18:12:00Z",
            )
            self.assertFalse(replay_inserted)
            self.assertEqual(first, second)
            self.assertEqual(
                len(
                    store.load_events(
                        "economic_book",
                        paper_book_id(
                            provider_id="IBKR",
                            account_id="acct-repoll",
                        ),
                    )
                ),
                1,
            )
            imported = store.load_events(
                "provider_activity",
                paper_activity_identity(
                    provider_id="IBKR",
                    account_id="acct-repoll",
                    activity_id="dep-repoll",
                ),
            )
            self.assertEqual(len(imported), 1)
            self.assertEqual(
                imported[0]["payload"]["observed_at"],
                "2026-09-24T18:02:00Z",
            )

    def test_same_activity_reobserved_later_is_idempotent(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            evidence = activity(provider_id="IBKR", account_id="acct-9", activity_id="dep-reobserved")
            first, inserted = book_paper_activity(
                store,
                provider_id="IBKR",
                account_id="acct-9",
                activity=evidence,
                amount="100",
                observed_at="2026-09-24T18:02:00Z",
            )
            self.assertTrue(inserted)

            reopened = JournalStore(path)
            second, replay_inserted = book_paper_activity(
                reopened,
                provider_id="IBKR",
                account_id="acct-9",
                activity=evidence,
                amount="100.00",
                observed_at="2026-09-24T18:12:00Z",
            )
            self.assertFalse(replay_inserted)
            self.assertEqual(first, second)
            self.assertEqual(
                load_paper_book(
                    reopened,
                    provider_id="IBKR",
                    account_id="acct-9",
                ).cash("USD"),
                Decimal("100"),
            )

    def test_same_identity_with_changed_amount_fails_closed(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            kwargs = dict(
                provider_id="KRAKEN",
                account_id="acct",
                activity=activity(provider_id="KRAKEN", account_id="acct", activity_id="dep-conflict"),
                observed_at="2026-09-24T18:03:00Z",
            )
            book_paper_activity(store, amount="10", **kwargs)
            with self.assertRaisesRegex(ValueError, "idempotency_key"):
                book_paper_activity(store, amount="11", **kwargs)

            book = load_paper_book(
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
                provider_id="BINANCE",
                account_id="acct",
                activity_id="wd-1",
                activity_type="WITHDRAWAL",
            )
            transaction, inserted = book_paper_activity(
                store,
                provider_id="BINANCE",
                account_id="acct",
                activity=withdrawal,
                amount="-25.125",
                observed_at="2026-09-24T18:04:00Z",
            )
            self.assertTrue(inserted)
            book = load_paper_book(
                store,
                provider_id="BINANCE",
                account_id="acct",
            )
            self.assertEqual(book.cash("USD"), Decimal("-25.125"))
            self.assertEqual(transaction.postings[0].signed_amount, Decimal("-25.125"))

            with self.assertRaisesRegex(ValueError, "WITHDRAWAL amount"):
                book_paper_activity(
                    store,
                    provider_id="BINANCE",
                    account_id="acct",
                    activity=activity(
                        provider_id="BINANCE",
                        account_id="acct",
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
                book_paper_activity(
                    store,
                    provider_id="ALPACA",
                    account_id="acct",
                    activity=activity(provider_id="ALPACA", account_id="acct", activity_id="dep-negative"),
                    amount="-1",
                    observed_at="2026-09-24T18:05:00Z",
                )
            with self.assertRaises(TypeError):
                book_paper_activity(
                    store,
                    provider_id="ALPACA",
                    account_id="acct",
                    activity=activity(provider_id="ALPACA", account_id="acct", activity_id="dep-float"),
                    amount=1.25,
                    observed_at="2026-09-24T18:05:00Z",
                )
            self.assertEqual(
                store.load_events(
                    "economic_book",
                    paper_book_id(provider_id="ALPACA", account_id="acct"),
                ),
                [],
            )

    def test_external_cash_flow_rejects_trading_linkage_instead_of_dropping_it(self):
        with TemporaryDirectory() as directory:
            for field, value in (
                ("instrument", "BTCUSD"),
                ("client_order_id", "client-7"),
                ("provider_order_id", "provider-7"),
                ("provider_execution_id", "execution-7"),
            ):
                with self.subTest(field=field):
                    store = JournalStore(
                        Path(directory) / f"{field}.sqlite3"
                    )
                    with self.assertRaisesRegex(
                        ValueError,
                        "must not discard trading linkage",
                    ):
                        book_paper_activity(
                            store,
                            provider_id="ALPACA",
                            account_id="acct",
                            activity=activity(
                                provider_id="ALPACA",
                                account_id="acct",
                                activity_id=f"linked-{field}",
                                **{field: value},
                            ),
                            amount="5",
                            observed_at="2026-09-24T18:06:00Z",
                        )
                    self.assertEqual(
                        store.load_events(
                            "economic_book",
                            paper_book_id(provider_id="ALPACA", account_id="acct"),
                        ),
                        [],
                    )

    def test_unknown_autotrade_and_unqualified_activity_types_are_not_booked(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            for candidate in (
                activity(provider_id="BYBIT", account_id="acct", activity_id="unknown", origin="UNKNOWN"),
                activity(provider_id="BYBIT", account_id="acct", activity_id="auto", origin="AUTOTRADE"),
            ):
                with self.assertRaisesRegex(ValueError, "MANUAL or EXTERNAL"):
                    book_paper_activity(
                        store,
                        provider_id="BYBIT",
                        account_id="acct",
                        activity=candidate,
                        amount="5",
                        observed_at="2026-09-24T18:06:00Z",
                    )

            with self.assertRaisesRegex(ValueError, "not qualified"):
                book_paper_activity(
                    store,
                    provider_id="BYBIT",
                    account_id="acct",
                    activity=activity(
                        provider_id="BYBIT",
                        account_id="acct",
                        activity_id="adjustment",
                        activity_type="CASH_ADJUSTMENT",
                    ),
                    amount="5",
                    observed_at="2026-09-24T18:06:00Z",
                )
            self.assertEqual(
                store.load_events(
                    "economic_book",
                    paper_book_id(provider_id="BYBIT", account_id="acct"),
                ),
                [],
            )

    def test_currency_and_evidence_time_are_required(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            with self.assertRaisesRegex(ValueError, "requires currency"):
                book_paper_activity(
                    store,
                    provider_id="IBKR",
                    account_id="acct",
                    activity=activity(provider_id="IBKR", account_id="acct", activity_id="no-currency", currency=None),
                    amount="5",
                    observed_at="2026-09-24T18:07:00Z",
                )
            with self.assertRaisesRegex(ValueError, "must not precede"):
                book_paper_activity(
                    store,
                    provider_id="IBKR",
                    account_id="acct",
                    activity=activity(
                        provider_id="IBKR",
                        account_id="acct",
                        activity_id="future",
                        occurred_at="2026-09-24T19:00:00Z",
                    ),
                    amount="5",
                    observed_at="2026-09-24T18:07:00Z",
                )

    def test_opaque_identifier_delimiters_cannot_alias_durable_identity(self):
        first_activity = paper_activity_identity(
            provider_id="ALPACA",
            account_id="a/b",
            activity_id="c",
        )
        second_activity = paper_activity_identity(
            provider_id="ALPACA",
            account_id="a",
            activity_id="b/c",
        )
        self.assertNotEqual(first_activity, second_activity)

        first_book = paper_book_id(provider_id="ALPACA", account_id="a/b")
        second_book = paper_book_id(provider_id="ALPACA/A", account_id="b")
        self.assertNotEqual(first_book, second_book)

    def test_provider_activity_evidence_cannot_cross_account_or_provider(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            evidence = activity(
                provider_id="ALPACA",
                account_id="a",
                activity_id="shared-id",
            )
            _, inserted = book_paper_activity(
                store,
                provider_id="ALPACA",
                account_id="a",
                activity=evidence,
                amount="10",
                observed_at="2026-09-24T18:08:00Z",
            )
            self.assertTrue(inserted)

            with self.assertRaisesRegex(ValueError, "account_id mismatch"):
                book_paper_activity(
                    store,
                    provider_id="ALPACA",
                    account_id="b",
                    activity=evidence,
                    amount="20",
                    observed_at="2026-09-24T18:08:00Z",
                )
            with self.assertRaisesRegex(ValueError, "provider_id mismatch"):
                book_paper_activity(
                    store,
                    provider_id="IBKR",
                    account_id="a",
                    activity=evidence,
                    amount="20",
                    observed_at="2026-09-24T18:08:00Z",
                )

            second_evidence = activity(
                provider_id="ALPACA",
                account_id="b",
                activity_id="shared-id",
            )
            _, inserted = book_paper_activity(
                store,
                provider_id="ALPACA",
                account_id="b",
                activity=second_evidence,
                amount="20",
                observed_at="2026-09-24T18:08:00Z",
            )
            self.assertTrue(inserted)
            self.assertEqual(
                load_paper_book(
                    store, provider_id="ALPACA", account_id="a"
                ).cash("USD"),
                Decimal("10"),
            )
            self.assertEqual(
                load_paper_book(
                    store, provider_id="ALPACA", account_id="b"
                ).cash("USD"),
                Decimal("20"),
            )

    def test_multiple_events_rebuild_in_order_after_restart(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            book_paper_activity(
                store,
                provider_id="WHITEBIT",
                account_id="acct",
                activity=activity(provider_id="WHITEBIT", account_id="acct", activity_id="dep"),
                amount="100",
                observed_at="2026-09-24T18:09:00Z",
            )
            book_paper_activity(
                store,
                provider_id="WHITEBIT",
                account_id="acct",
                activity=activity(
                    provider_id="WHITEBIT",
                    account_id="acct",
                    activity_id="wd",
                    activity_type="WITHDRAWAL",
                    occurred_at="2026-09-24T18:10:00Z",
                ),
                amount="-40",
                observed_at="2026-09-24T18:11:00Z",
            )

            reopened = JournalStore(path)
            book = load_paper_book(
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
                    "aggregate_id": paper_book_id(provider_id="ALPACA", account_id="acct"),
                    "aggregate_version": 1,
                    "committed_at": "2026-09-24T18:12:00Z",
                    "payload": payload,
                    "payload_hash": payload_digest(payload),
                }
            )
            with self.assertRaisesRegex(ValueError, "unsupported durable event"):
                load_paper_book(
                    store,
                    provider_id="ALPACA",
                    account_id="acct",
                )


if __name__ == "__main__":
    unittest.main()
