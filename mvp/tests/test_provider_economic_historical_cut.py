from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.provider_activity_accounting import (
    AccountingConflict,
    DurableProviderEconomicBook,
    ProviderEconomicCut,
    book_external_provider_cash_activity,
    require_provider_economic_cut,
    reverify_provider_economic_cut,
)
from mvp.tests.test_provider_activity_accounting import activity


def book_cash(
    store: JournalStore,
    *,
    activity_id: str,
    amount: str,
    provider_id: str = "ALPACA",
    account_id: str = "cut-account",
) -> None:
    evidence = activity(
        provider_id=provider_id,
        account_id=account_id,
        activity_id=activity_id,
        signed_amount=amount,
    )
    book_external_provider_cash_activity(
        store,
        provider_id=provider_id,
        account_id=account_id,
        environment="PAPER",
        activity=evidence,
        observed_at="2026-09-24T18:01:00Z",
    )


class ProviderEconomicHistoricalCutTests(unittest.TestCase):
    def test_old_cut_requires_its_frozen_visibility_after_later_economic_append(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            book_cash(store, activity_id="cash-1", amount="100")
            owner = DurableProviderEconomicBook(
                store,
                provider_id="ALPACA",
                account_id="cut-account",
                environment="PAPER",
            )

            first = owner.resolve_historical_cut(1)
            self.assertEqual(first.aggregate_version, 1)
            self.assertEqual(len(first.transaction_digests), 1)

            book_cash(store, activity_id="cash-2", amount="25")
            second = owner.resolve_historical_cut(2)

            with self.assertRaisesRegex(
                AccountingConflict,
                "stale at visibility cut",
            ):
                owner.resolve_historical_cut(
                    1,
                    expected_journal_sequence=first.journal_sequence,
                    expected_event_id=first.event_id,
                )

            first_again = owner.resolve_historical_cut(
                1,
                expected_journal_sequence=first.journal_sequence,
                expected_event_id=first.event_id,
                visibility_journal_sequence=first.visibility_journal_sequence,
            )

            self.assertEqual(first_again, first)
            self.assertEqual(first_again.cut_digest, first.cut_digest)
            self.assertNotEqual(second.cut_digest, first.cut_digest)
            self.assertEqual(len(second.transaction_digests), 2)

    def test_frozen_pre_correction_cut_reverifies_after_later_append(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            book_cash(store, activity_id="cash-1", amount="100")
            owner = DurableProviderEconomicBook(
                store,
                provider_id="ALPACA",
                account_id="cut-account",
                environment="PAPER",
            )
            first = owner.resolve_historical_cut(1)

            book_cash(store, activity_id="cash-2", amount="25")

            replayed = reverify_provider_economic_cut(
                owner,
                first,
                expected_visibility_journal_sequence=first.visibility_journal_sequence,
            )
            self.assertEqual(replayed, first)
            self.assertEqual(
                replayed.visibility_journal_sequence,
                first.visibility_journal_sequence,
            )

    def test_reverification_rejects_candidate_selected_visibility(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            book_cash(store, activity_id="cash-1", amount="100")
            owner = DurableProviderEconomicBook(
                store,
                provider_id="ALPACA",
                account_id="cut-account",
                environment="PAPER",
            )
            first = owner.resolve_historical_cut(1)
            book_cash(store, activity_id="cash-2", amount="25")
            second = owner.resolve_historical_cut(2)

            with self.assertRaisesRegex(
                AccountingConflict,
                "visibility does not match expected authority",
            ):
                reverify_provider_economic_cut(
                    owner,
                    first,
                    expected_visibility_journal_sequence=second.visibility_journal_sequence,
                )

            for invalid in (0, -1, True):
                with self.subTest(expected_visibility=invalid):
                    with self.assertRaises(ValueError):
                        reverify_provider_economic_cut(
                            owner,
                            first,
                            expected_visibility_journal_sequence=invalid,
                        )

    def test_visibility_cut_selects_latest_economic_revision_visible_at_global_cut(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            book_cash(store, activity_id="cash-1", amount="100")
            owner = DurableProviderEconomicBook(
                store,
                provider_id="ALPACA",
                account_id="cut-account",
                environment="PAPER",
            )
            first = owner.resolve_historical_cut(1)
            book_cash(store, activity_id="cash-2", amount="25")
            second = owner.resolve_historical_cut(2)

            at_first = owner.resolve_historical_cut(
                1,
                visibility_journal_sequence=first.journal_sequence,
            )
            self.assertEqual(
                at_first.visibility_journal_sequence,
                first.journal_sequence,
            )
            with self.assertRaisesRegex(
                AccountingConflict,
                "stale at visibility cut",
            ):
                owner.resolve_historical_cut(
                    1,
                    visibility_journal_sequence=second.journal_sequence,
                )
            at_second = owner.resolve_historical_cut(
                2,
                visibility_journal_sequence=second.journal_sequence,
            )
            self.assertEqual(
                at_second.visibility_journal_sequence,
                second.journal_sequence,
            )

    def test_visibility_cut_may_follow_terminal_event_when_other_aggregate_advances_journal(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            book_cash(store, activity_id="cash-1", amount="100")
            owner = DurableProviderEconomicBook(
                store,
                provider_id="ALPACA",
                account_id="cut-account",
                environment="PAPER",
            )
            first = owner.resolve_historical_cut(1)

            book_cash(
                store,
                activity_id="other-account-cash",
                amount="50",
                account_id="other-account",
            )
            global_cut = store.current_journal_sequence()
            self.assertGreater(global_cut, first.journal_sequence)

            visible = owner.resolve_historical_cut(
                1,
                visibility_journal_sequence=global_cut,
            )
            self.assertEqual(visible.journal_sequence, first.journal_sequence)
            self.assertEqual(visible.visibility_journal_sequence, global_cut)
            self.assertNotEqual(visible.cut_digest, first.cut_digest)

    def test_visibility_cut_cannot_claim_future_global_journal_state(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            book_cash(store, activity_id="cash-1", amount="100")
            owner = DurableProviderEconomicBook(
                store,
                provider_id="ALPACA",
                account_id="cut-account",
                environment="PAPER",
            )
            future = store.current_journal_sequence() + 1
            with self.assertRaisesRegex(
                AccountingConflict,
                "visibility cut is beyond durable journal",
            ):
                owner.resolve_historical_cut(
                    1,
                    visibility_journal_sequence=future,
                )

    def test_historical_cut_rejects_shadowed_journal_store_authority(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            book_cash(store, activity_id="cash-1", amount="100")
            owner = DurableProviderEconomicBook(
                store,
                provider_id="ALPACA",
                account_id="cut-account",
                environment="PAPER",
            )
            store.load_events = lambda *args, **kwargs: []
            with self.assertRaisesRegex(TypeError, "shadowed"):
                owner.resolve_historical_cut(1)

    def test_terminal_event_and_journal_sequence_are_exact_fences(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            book_cash(store, activity_id="cash-1", amount="100")
            owner = DurableProviderEconomicBook(
                store,
                provider_id="ALPACA",
                account_id="cut-account",
                environment="PAPER",
            )
            cut = owner.resolve_historical_cut(1)

            with self.assertRaisesRegex(AccountingConflict, "journal sequence mismatch"):
                owner.resolve_historical_cut(
                    1,
                    expected_journal_sequence=cut.journal_sequence + 1,
                )
            with self.assertRaisesRegex(AccountingConflict, "event identity mismatch"):
                owner.resolve_historical_cut(
                    1,
                    expected_event_id=cut.event_id + "-wrong",
                )

    def test_future_or_invalid_cut_is_rejected(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            book_cash(store, activity_id="cash-1", amount="100")
            owner = DurableProviderEconomicBook(
                store,
                provider_id="ALPACA",
                account_id="cut-account",
                environment="PAPER",
            )

            for invalid in (0, -1, True):
                with self.subTest(value=invalid):
                    with self.assertRaises(ValueError):
                        owner.resolve_historical_cut(invalid)
            with self.assertRaisesRegex(AccountingConflict, "beyond durable history"):
                owner.resolve_historical_cut(2)

    def test_same_economics_in_different_store_generation_has_different_cut_identity(self):
        with TemporaryDirectory() as directory:
            first_store = JournalStore(Path(directory) / "first.sqlite3")
            second_store = JournalStore(Path(directory) / "second.sqlite3")
            for store in (first_store, second_store):
                book_cash(store, activity_id="cash-1", amount="100")

            first = DurableProviderEconomicBook(
                first_store,
                provider_id="ALPACA",
                account_id="cut-account",
                environment="PAPER",
            ).resolve_historical_cut(1)
            second = DurableProviderEconomicBook(
                second_store,
                provider_id="ALPACA",
                account_id="cut-account",
                environment="PAPER",
            ).resolve_historical_cut(1)

            self.assertEqual(first.transaction_digests, second.transaction_digests)
            self.assertEqual(first.resulting_book_digest, second.resulting_book_digest)
            self.assertNotEqual(first.store_identity, second.store_identity)
            self.assertNotEqual(first.cut_digest, second.cut_digest)

    def test_bybit_paper_cut_fails_closed_until_exact_provider_domain_is_bound(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            owner = DurableProviderEconomicBook(
                store,
                provider_id="BYBIT",
                account_id="cut-account",
                environment="PAPER",
            )
            with self.assertRaisesRegex(
                AccountingConflict,
                "requires exact provider_environment",
            ):
                owner.resolve_historical_cut(1)

    def test_issued_cut_rejects_post_issuance_field_rewrite(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            book_cash(store, activity_id="cash-1", amount="100")
            cut = DurableProviderEconomicBook(
                store,
                provider_id="ALPACA",
                account_id="cut-account",
                environment="PAPER",
            ).resolve_historical_cut(1)
            self.assertIs(require_provider_economic_cut(cut), cut)

            object.__setattr__(cut, "event_id", cut.event_id + "-forged")
            with self.assertRaisesRegex(
                AccountingConflict,
                "changed after issuance",
            ):
                require_provider_economic_cut(cut)

    def test_object_new_clone_is_not_issued_owner_evidence(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            book_cash(store, activity_id="cash-1", amount="100")
            cut = DurableProviderEconomicBook(
                store,
                provider_id="ALPACA",
                account_id="cut-account",
                environment="PAPER",
            ).resolve_historical_cut(1)

            forged = object.__new__(ProviderEconomicCut)
            for name in (
                "store_identity",
                "provider_id",
                "account_id",
                "environment",
                "book_id",
                "aggregate_version",
                "journal_sequence",
                "visibility_journal_sequence",
                "event_id",
                "payload_hash",
                "transaction_digests",
                "resulting_book_digest",
                "cut_digest",
            ):
                object.__setattr__(forged, name, getattr(cut, name))

            with self.assertRaisesRegex(
                AccountingConflict,
                "not issued by canonical durable replay",
            ):
                require_provider_economic_cut(forged)

    def test_unissued_exact_clone_can_be_reverified_from_durable_truth(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            first_store = JournalStore(path)
            book_cash(first_store, activity_id="cash-1", amount="100")
            issued = DurableProviderEconomicBook(
                first_store,
                provider_id="ALPACA",
                account_id="cut-account",
                environment="PAPER",
            ).resolve_historical_cut(1)

            transported = object.__new__(ProviderEconomicCut)
            for name in (
                "store_identity",
                "provider_id",
                "account_id",
                "environment",
                "book_id",
                "aggregate_version",
                "journal_sequence",
                "visibility_journal_sequence",
                "event_id",
                "payload_hash",
                "transaction_digests",
                "resulting_book_digest",
                "cut_digest",
            ):
                object.__setattr__(transported, name, getattr(issued, name))

            with self.assertRaisesRegex(
                AccountingConflict,
                "not issued by canonical durable replay",
            ):
                require_provider_economic_cut(transported)

            restarted_store = JournalStore(path)
            restarted_book = DurableProviderEconomicBook(
                restarted_store,
                provider_id="ALPACA",
                account_id="cut-account",
                environment="PAPER",
            )
            frozen_visibility = issued.visibility_journal_sequence
            replayed = reverify_provider_economic_cut(
                restarted_book,
                transported,
                expected_visibility_journal_sequence=frozen_visibility,
            )
            self.assertEqual(replayed, issued)
            self.assertIs(require_provider_economic_cut(replayed), replayed)

    def test_durable_reverification_rejects_store_or_content_relabeling(self):
        with TemporaryDirectory() as directory:
            first_store = JournalStore(Path(directory) / "first.sqlite3")
            second_store = JournalStore(Path(directory) / "second.sqlite3")
            for store in (first_store, second_store):
                book_cash(store, activity_id="cash-1", amount="100")

            first_book = DurableProviderEconomicBook(
                first_store,
                provider_id="ALPACA",
                account_id="cut-account",
                environment="PAPER",
            )
            second_book = DurableProviderEconomicBook(
                second_store,
                provider_id="ALPACA",
                account_id="cut-account",
                environment="PAPER",
            )
            cut = first_book.resolve_historical_cut(1)

            with self.assertRaisesRegex(
                AccountingConflict,
                "store identity does not match selected authority",
            ):
                reverify_provider_economic_cut(
                    second_book,
                    cut,
                    expected_visibility_journal_sequence=cut.visibility_journal_sequence,
                )

            object.__setattr__(cut, "cut_digest", "sha256:" + "f" * 64)
            with self.assertRaisesRegex(
                AccountingConflict,
                "does not match canonical durable replay",
            ):
                reverify_provider_economic_cut(
                    first_book,
                    cut,
                    expected_visibility_journal_sequence=cut.visibility_journal_sequence,
                )

    def test_cut_value_cannot_be_publicly_self_minted(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            book_cash(store, activity_id="cash-1", amount="100")
            owner = DurableProviderEconomicBook(
                store,
                provider_id="ALPACA",
                account_id="cut-account",
                environment="PAPER",
            )
            cut = owner.resolve_historical_cut(1)

            with self.assertRaisesRegex(
                AccountingConflict,
                "must come from canonical durable replay",
            ):
                ProviderEconomicCut(
                    store_identity=cut.store_identity,
                    provider_id=cut.provider_id,
                    account_id=cut.account_id,
                    environment=cut.environment,
                    book_id=cut.book_id,
                    aggregate_version=cut.aggregate_version,
                    journal_sequence=cut.journal_sequence,
                    visibility_journal_sequence=cut.visibility_journal_sequence,
                    event_id=cut.event_id,
                    payload_hash=cut.payload_hash,
                    transaction_digests=cut.transaction_digests,
                    resulting_book_digest=cut.resulting_book_digest,
                    cut_digest=cut.cut_digest,
                )


if __name__ == "__main__":
    unittest.main()
