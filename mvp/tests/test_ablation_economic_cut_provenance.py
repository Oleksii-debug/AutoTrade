from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.ablation_economic_cut_provenance import (
    ResolvedAblationProviderEconomicCutProvenance,
    resolve_ablation_provider_economic_cut_provenance,
    reverify_ablation_provider_economic_cut_provenance,
)
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.provider_activity_accounting import (
    AccountingConflict,
    DurableProviderEconomicBook,
)
from mvp.tests.test_provider_economic_historical_cut import book_cash


class AblationProviderEconomicCutProvenanceTests(unittest.TestCase):
    def _fixture(self, root: Path):
        store = JournalStore(root / "journal.sqlite3")
        book_cash(store, activity_id="cash-1", amount="100")
        owner = DurableProviderEconomicBook(
            store,
            provider_id="ALPACA",
            account_id="cut-account",
            environment="PAPER",
        )
        cut = owner.resolve_historical_cut(1)
        return store, owner, cut

    def test_resolution_retains_exact_terminal_event_provenance(self):
        with TemporaryDirectory() as directory:
            _store, owner, cut = self._fixture(Path(directory))

            evidence = resolve_ablation_provider_economic_cut_provenance(
                owner,
                cut,
                expected_visibility_journal_sequence=cut.visibility_journal_sequence,
            )

            self.assertIsInstance(
                evidence,
                ResolvedAblationProviderEconomicCutProvenance,
            )
            self.assertEqual(evidence.event_id, cut.event_id)
            self.assertEqual(evidence.payload_hash, cut.payload_hash)
            self.assertEqual(evidence.economic_cut_digest, cut.cut_digest)
            self.assertEqual(evidence.resulting_book_digest, cut.resulting_book_digest)
            self.assertEqual(evidence.transaction_digests, cut.transaction_digests)
            self.assertFalse(evidence.terminal_cost_composite)
            self.assertEqual(evidence.evidence_kind, "PROVIDER_ECONOMIC_PREFIX_ONLY")
            self.assertTrue(evidence.provenance_digest.startswith("sha256:"))
            evidence.verify_integrity()

    def test_reverification_replays_same_durable_prefix(self):
        with TemporaryDirectory() as directory:
            store, owner, cut = self._fixture(Path(directory))
            evidence = resolve_ablation_provider_economic_cut_provenance(
                owner,
                cut,
                expected_visibility_journal_sequence=cut.visibility_journal_sequence,
            )
            book_cash(store, activity_id="cash-2", amount="25")

            replayed = reverify_ablation_provider_economic_cut_provenance(
                owner,
                cut,
                expected_visibility_journal_sequence=cut.visibility_journal_sequence,
                evidence=evidence,
            )

            self.assertEqual(replayed, evidence)
            self.assertEqual(replayed.event_id, cut.event_id)
            self.assertEqual(replayed.payload_hash, cut.payload_hash)

    def test_event_identity_tamper_fails_integrity_before_replay(self):
        with TemporaryDirectory() as directory:
            _store, owner, cut = self._fixture(Path(directory))
            evidence = resolve_ablation_provider_economic_cut_provenance(
                owner,
                cut,
                expected_visibility_journal_sequence=cut.visibility_journal_sequence,
            )
            object.__setattr__(evidence, "event_id", "forged-event")

            with self.assertRaisesRegex(
                AccountingConflict,
                "provenance digest does not match canonical material",
            ):
                reverify_ablation_provider_economic_cut_provenance(
                    owner,
                    cut,
                    expected_visibility_journal_sequence=cut.visibility_journal_sequence,
                    evidence=evidence,
                )

    def test_payload_hash_tamper_fails_integrity_before_replay(self):
        with TemporaryDirectory() as directory:
            _store, owner, cut = self._fixture(Path(directory))
            evidence = resolve_ablation_provider_economic_cut_provenance(
                owner,
                cut,
                expected_visibility_journal_sequence=cut.visibility_journal_sequence,
            )
            object.__setattr__(evidence, "payload_hash", "sha256:" + "f" * 64)

            with self.assertRaisesRegex(
                AccountingConflict,
                "provenance digest does not match canonical material",
            ):
                reverify_ablation_provider_economic_cut_provenance(
                    owner,
                    cut,
                    expected_visibility_journal_sequence=cut.visibility_journal_sequence,
                    evidence=evidence,
                )

    def test_transaction_digest_must_remain_canonical_sha256(self):
        with TemporaryDirectory() as directory:
            _store, owner, cut = self._fixture(Path(directory))
            evidence = resolve_ablation_provider_economic_cut_provenance(
                owner,
                cut,
                expected_visibility_journal_sequence=cut.visibility_journal_sequence,
            )
            transaction_id, _digest = evidence.transaction_digests[0]
            object.__setattr__(
                evidence,
                "transaction_digests",
                ((transaction_id, "not-a-digest"),),
            )

            with self.assertRaisesRegex(
                AccountingConflict,
                "transaction_digest must be an exact canonical sha256 digest",
            ):
                reverify_ablation_provider_economic_cut_provenance(
                    owner,
                    cut,
                    expected_visibility_journal_sequence=cut.visibility_journal_sequence,
                    evidence=evidence,
                )

    def test_duplicate_transaction_identity_is_rejected_before_replay(self):
        with TemporaryDirectory() as directory:
            _store, owner, cut = self._fixture(Path(directory))
            evidence = resolve_ablation_provider_economic_cut_provenance(
                owner,
                cut,
                expected_visibility_journal_sequence=cut.visibility_journal_sequence,
            )
            item = evidence.transaction_digests[0]
            object.__setattr__(evidence, "transaction_digests", (item, item))

            with self.assertRaisesRegex(
                AccountingConflict,
                "duplicate transaction_id",
            ):
                reverify_ablation_provider_economic_cut_provenance(
                    owner,
                    cut,
                    expected_visibility_journal_sequence=cut.visibility_journal_sequence,
                    evidence=evidence,
                )

    def test_candidate_cannot_select_later_visibility(self):
        with TemporaryDirectory() as directory:
            store, owner, cut = self._fixture(Path(directory))
            evidence = resolve_ablation_provider_economic_cut_provenance(
                owner,
                cut,
                expected_visibility_journal_sequence=cut.visibility_journal_sequence,
            )
            book_cash(store, activity_id="cash-2", amount="25")
            later_visibility = store.current_journal_sequence()
            self.assertGreater(later_visibility, cut.visibility_journal_sequence)

            with self.assertRaisesRegex(
                AccountingConflict,
                "visibility does not match expected authority",
            ):
                reverify_ablation_provider_economic_cut_provenance(
                    owner,
                    cut,
                    expected_visibility_journal_sequence=later_visibility,
                    evidence=evidence,
                )

    def test_cross_book_reverification_fails_closed(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store, owner, cut = self._fixture(root)
            evidence = resolve_ablation_provider_economic_cut_provenance(
                owner,
                cut,
                expected_visibility_journal_sequence=cut.visibility_journal_sequence,
            )
            book_cash(
                store,
                activity_id="other-cash",
                amount="50",
                account_id="other-account",
            )
            other_owner = DurableProviderEconomicBook(
                store,
                provider_id="ALPACA",
                account_id="other-account",
                environment="PAPER",
            )

            with self.assertRaisesRegex(
                AccountingConflict,
                "scope does not match selected durable book",
            ):
                reverify_ablation_provider_economic_cut_provenance(
                    other_owner,
                    cut,
                    expected_visibility_journal_sequence=cut.visibility_journal_sequence,
                    evidence=evidence,
                )

    def test_instance_shadowed_historical_cut_executable_is_rejected(self):
        with TemporaryDirectory() as directory:
            _store, owner, cut = self._fixture(Path(directory))

            object.__setattr__(
                owner,
                "resolve_historical_cut",
                lambda *args, **kwargs: cut,
            )

            with self.assertRaisesRegex(
                AccountingConflict,
                "historical-cut executable is shadowed",
            ):
                resolve_ablation_provider_economic_cut_provenance(
                    owner,
                    cut,
                    expected_visibility_journal_sequence=cut.visibility_journal_sequence,
                )


if __name__ == "__main__":
    unittest.main()
