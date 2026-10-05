from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.ablation_funding_cashflow_evidence import (
    ResolvedAblationFundingCashflowEvidence,
    resolve_ablation_funding_cashflow_evidence,
    reverify_ablation_funding_cashflow_evidence,
)
from mvp.autotrade_mvp.perpetual_funding import PerpetualFundingConflict
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.tests.test_perpetual_funding import sealed_funding, DurablePerpetualFundingAuthorityTests


class AblationFundingCashflowEvidenceTests(unittest.TestCase):
    def _authority(self, root: Path, evidence):
        store = JournalStore(root / "journal.sqlite3")
        helper = DurablePerpetualFundingAuthorityTests("test_arbitrary_funding_normalizer_cannot_be_injected")
        authority, book = helper.authority(store, tuple(evidence))
        return store, authority, book

    def test_provider_authenticated_funding_is_frozen_signed_cashflow_not_cost(self):
        source = sealed_funding()
        with TemporaryDirectory() as directory:
            _store, authority, _book = self._authority(Path(directory), [source])
            applied = authority.apply(source.evidence_ref)

            evidence = resolve_ablation_funding_cashflow_evidence(
                authority,
                expected_aggregate_version=1,
            )

            self.assertIsInstance(evidence, ResolvedAblationFundingCashflowEvidence)
            self.assertEqual(
                evidence.net_cashflow_by_unit,
                ((applied.currency, applied.cashflow),),
            )
            self.assertFalse(evidence.terminal_cost_component)
            self.assertFalse(evidence.cost_projection_ready)
            self.assertEqual(
                evidence.blocking_reason,
                "registered_funding_cost_projection_unavailable",
            )
            self.assertEqual(len(evidence.contributing_transactions), 1)
            evidence.verify_integrity()

    def test_pre_cut_correction_nets_reversal_and_replacement_to_latest_cashflow(self):
        original = sealed_funding(
            external_event_id="funding-1",
            revision="1",
            rate="0.001",
            observed_offset=1,
        )
        correction = sealed_funding(
            external_event_id="funding-2",
            revision="2",
            rate="0.002",
            observed_offset=2,
            corrects="funding-1",
        )
        with TemporaryDirectory() as directory:
            _store, authority, _book = self._authority(
                Path(directory),
                [original, correction],
            )
            authority.apply(original.evidence_ref)
            corrected = authority.apply(correction.evidence_ref)

            evidence = resolve_ablation_funding_cashflow_evidence(
                authority,
                expected_aggregate_version=2,
            )

            self.assertEqual(
                evidence.net_cashflow_by_unit,
                ((corrected.currency, corrected.cashflow),),
            )
            self.assertEqual(len(evidence.contributing_transactions), 3)
            self.assertEqual(len(evidence.funding_event_identities), 2)

    def test_post_cut_correction_does_not_rewrite_previously_frozen_cashflow(self):
        original = sealed_funding(
            external_event_id="funding-1",
            revision="1",
            rate="0.001",
            observed_offset=1,
        )
        correction = sealed_funding(
            external_event_id="funding-2",
            revision="2",
            rate="0.002",
            observed_offset=2,
            corrects="funding-1",
        )
        with TemporaryDirectory() as directory:
            _store, authority, _book = self._authority(
                Path(directory),
                [original, correction],
            )
            first_apply = authority.apply(original.evidence_ref)
            first = resolve_ablation_funding_cashflow_evidence(
                authority,
                expected_aggregate_version=1,
            )
            authority.apply(correction.evidence_ref)

            replayed = reverify_ablation_funding_cashflow_evidence(authority, first)

            self.assertEqual(replayed, first)
            self.assertEqual(
                replayed.net_cashflow_by_unit,
                ((first_apply.currency, first_apply.cashflow),),
            )

    def test_stale_funding_revision_cannot_be_newly_selected_after_correction(self):
        original = sealed_funding(
            external_event_id="funding-1",
            revision="1",
            rate="0.001",
            observed_offset=1,
        )
        correction = sealed_funding(
            external_event_id="funding-2",
            revision="2",
            rate="0.002",
            observed_offset=2,
            corrects="funding-1",
        )
        with TemporaryDirectory() as directory:
            _store, authority, _book = self._authority(
                Path(directory),
                [original, correction],
            )
            authority.apply(original.evidence_ref)
            authority.apply(correction.evidence_ref)

            with self.assertRaisesRegex(
                PerpetualFundingConflict,
                "aggregate version is stale at visibility cut",
            ):
                resolve_ablation_funding_cashflow_evidence(
                    authority,
                    expected_aggregate_version=1,
                )

    def test_digest_tamper_fails_before_owner_replay(self):
        source = sealed_funding()
        with TemporaryDirectory() as directory:
            _store, authority, _book = self._authority(Path(directory), [source])
            authority.apply(source.evidence_ref)
            evidence = resolve_ablation_funding_cashflow_evidence(
                authority,
                expected_aggregate_version=1,
            )
            object.__setattr__(evidence, "evidence_digest", "sha256:" + "f" * 64)

            with self.assertRaisesRegex(
                PerpetualFundingConflict,
                "evidence digest does not match canonical material",
            ):
                reverify_ablation_funding_cashflow_evidence(authority, evidence)

    def test_shadowed_funding_reader_cannot_retarget_owner_evidence(self):
        source = sealed_funding()
        with TemporaryDirectory() as directory:
            _store, authority, _book = self._authority(Path(directory), [source])
            authority.apply(source.evidence_ref)
            object.__setattr__(authority, "_events", lambda: [])

            with self.assertRaisesRegex(
                PerpetualFundingConflict,
                "shadows canonical methods",
            ):
                resolve_ablation_funding_cashflow_evidence(
                    authority,
                    expected_aggregate_version=1,
                )


if __name__ == "__main__":
    unittest.main()
