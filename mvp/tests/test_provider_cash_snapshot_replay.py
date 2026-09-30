from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.provider_activity_accounting import (
    AccountingConflict,
    book_external_provider_cash_activity,
    load_provider_account_economic_book,
)
from mvp.autotrade_mvp.reconciliation import ProviderActivityEvidence


def provider_cash_evidence(*, account_id: str, activity_id: str) -> ProviderActivityEvidence:
    return ProviderActivityEvidence.create(
        provider_id="IBKR",
        account_id=account_id,
        environment="PAPER",
        activity_id=activity_id,
        activity_type="DEPOSIT",
        origin="EXTERNAL",
        occurred_at="2026-09-24T18:00:00Z",
        currency="USD",
        signed_amount="100",
    )


def book_cash(
    store: JournalStore,
    *,
    account_id: str,
    activity_id: str,
):
    return book_external_provider_cash_activity(
        store,
        provider_id="IBKR",
        account_id=account_id,
        environment="PAPER",
        activity=provider_cash_evidence(
            account_id=account_id,
            activity_id=activity_id,
        ),
        observed_at="2026-09-24T18:02:00Z",
    )


class ProviderCashSnapshotReplayTests(unittest.TestCase):
    def _seed_command(
        self,
        directory: str,
        *,
        account_id: str,
        activity_id: str,
    ):
        source = JournalStore(Path(directory) / "source.sqlite3")
        with patch.object(
            source,
            "commit_command",
            wraps=source.commit_command,
        ) as source_commit:
            transaction, inserted = book_cash(
                source,
                account_id=account_id,
                activity_id=activity_id,
            )
        self.assertTrue(inserted)
        self.assertIsNotNone(source_commit.call_args)
        return transaction, dict(source_commit.call_args.kwargs)

    def test_competing_commit_after_absent_snapshot_replays_from_one_held_batch(self):
        with TemporaryDirectory() as directory:
            account_id = "acct-snapshot-race"
            activity_id = "dep-snapshot-race"
            expected_transaction, seed = self._seed_command(
                directory,
                account_id=account_id,
                activity_id=activity_id,
            )
            target = JournalStore(Path(directory) / "target.sqlite3")
            original_snapshot = target.load_command_event_batch
            snapshot_calls = 0

            def absent_then_competing_commit(**kwargs):
                nonlocal snapshot_calls
                snapshot_calls += 1
                snapshot = original_snapshot(**kwargs)
                if snapshot_calls == 1:
                    self.assertIsNone(snapshot)
                    _, inserted, _ = target.commit_command(**seed)
                    self.assertTrue(inserted)
                    return None
                return snapshot

            with patch.object(
                target,
                "load_command_event_batch",
                side_effect=absent_then_competing_commit,
            ):
                transaction, inserted = book_cash(
                    target,
                    account_id=account_id,
                    activity_id=activity_id,
                )

            self.assertFalse(inserted)
            self.assertEqual(transaction, expected_transaction)
            self.assertGreaterEqual(snapshot_calls, 2)
            self.assertEqual(target.current_journal_sequence(), 2)
            self.assertEqual(
                load_provider_account_economic_book(
                    target,
                    provider_id="IBKR",
                    account_id=account_id,
                    environment="PAPER",
                ).transactions,
                (expected_transaction,),
            )

    def test_wrong_economic_semantic_owner_never_becomes_valid_cash_authority(self):
        with TemporaryDirectory() as directory:
            account_id = "acct-owner-mismatch"
            activity_id = "dep-owner-mismatch"
            _transaction, seed = self._seed_command(
                directory,
                account_id=account_id,
                activity_id=activity_id,
            )
            original_events = tuple(seed["events"])

            for field, value in (
                ("event_type", "OtherEconomicEvent"),
                ("aggregate_type", "other_economic_book"),
                ("aggregate_id", "economic-book:forged-owner"),
            ):
                with self.subTest(field=field):
                    forged = JournalStore(
                        Path(directory) / f"forged-{field}.sqlite3"
                    )
                    forged_events = []
                    for envelope, topic in original_events:
                        candidate = dict(envelope)
                        if candidate["event_type"] == "EconomicTransactionBooked":
                            candidate[field] = value
                        forged_events.append((candidate, topic))

                    _, forged_inserted, _ = forged.commit_command(
                        **{**seed, "events": forged_events}
                    )
                    self.assertTrue(forged_inserted)
                    self.assertEqual(forged.current_journal_sequence(), 2)

                    if field == "event_type":
                        with self.assertRaisesRegex(
                            AccountingConflict,
                            "unsupported durable event type",
                        ):
                            load_provider_account_economic_book(
                                forged,
                                provider_id="IBKR",
                                account_id=account_id,
                                environment="PAPER",
                            )
                    else:
                        self.assertEqual(
                            load_provider_account_economic_book(
                                forged,
                                provider_id="IBKR",
                                account_id=account_id,
                                environment="PAPER",
                            ).transactions,
                            (),
                        )

                    with self.assertRaisesRegex(
                        AccountingConflict,
                        "invalid durable semantic owner",
                    ):
                        book_cash(
                            forged,
                            account_id=account_id,
                            activity_id=activity_id,
                        )

                    self.assertEqual(forged.current_journal_sequence(), 2)
                    if field == "event_type":
                        with self.assertRaisesRegex(
                            AccountingConflict,
                            "unsupported durable event type",
                        ):
                            load_provider_account_economic_book(
                                forged,
                                provider_id="IBKR",
                                account_id=account_id,
                                environment="PAPER",
                            )
                    else:
                        self.assertEqual(
                            load_provider_account_economic_book(
                                forged,
                                provider_id="IBKR",
                                account_id=account_id,
                                environment="PAPER",
                            ).transactions,
                            (),
                        )


if __name__ == "__main__":
    unittest.main()
