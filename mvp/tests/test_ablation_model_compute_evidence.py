from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.ablation_model_compute_evidence import (
    ResolvedAblationModelComputeCostFact,
    resolve_ablation_model_compute_cost_fact,
    reverify_ablation_model_compute_cost_fact,
)
from mvp.autotrade_mvp.model_budget_journal import DurableModelBudget
from mvp.autotrade_mvp.persistence import JournalStore


class AblationModelComputeCostFactTests(unittest.TestCase):
    def _budget(self, root: Path) -> DurableModelBudget:
        journal = JournalStore(root / "journal.sqlite3")
        return DurableModelBudget(
            journal=journal,
            budget_id="ablation-model-budget",
            ceiling="100",
            environment="SIMULATION",
            clock=lambda: "2026-01-01T00:00:00+00:00",
        )

    def test_settled_request_is_frozen_but_explicitly_lacks_value_unit_authority(self):
        with TemporaryDirectory() as directory:
            budget = self._budget(Path(directory))
            budget.reserve("request-1", "10")
            budget.settle("request-1", incurred="3", estimated_unbilled="0")

            evidence = resolve_ablation_model_compute_cost_fact(
                budget,
                request_id="request-1",
                expected_aggregate_version=3,
            )

            self.assertIsInstance(evidence, ResolvedAblationModelComputeCostFact)
            self.assertEqual(str(evidence.amount), "3")
            self.assertIsNone(evidence.value_unit)
            self.assertFalse(evidence.dimensionally_qualified)
            self.assertFalse(evidence.terminal_cost_component)
            self.assertEqual(
                evidence.blocking_reason,
                "canonical_model_compute_value_unit_unavailable",
            )
            self.assertEqual(len(evidence.event_identities), 2)
            evidence.verify_integrity()

    def test_unresolved_unbilled_cost_is_not_mature_component_fact(self):
        with TemporaryDirectory() as directory:
            budget = self._budget(Path(directory))
            budget.reserve("request-1", "10")
            budget.settle("request-1", incurred="3", estimated_unbilled="2")

            with self.assertRaisesRegex(
                ValueError,
                "unresolved estimated unbilled cost",
            ):
                resolve_ablation_model_compute_cost_fact(
                    budget,
                    request_id="request-1",
                    expected_aggregate_version=3,
                )

    def test_post_cut_billing_does_not_rewrite_frozen_cost_fact(self):
        with TemporaryDirectory() as directory:
            budget = self._budget(Path(directory))
            budget.reserve("request-1", "10")
            budget.settle("request-1", incurred="3", estimated_unbilled="0")
            first = resolve_ablation_model_compute_cost_fact(
                budget,
                request_id="request-1",
                expected_aggregate_version=3,
            )

            budget.reconcile_unbilled(
                billing_id="bill-1",
                request_id="request-1",
                billed="1",
            )

            replayed = reverify_ablation_model_compute_cost_fact(budget, first)
            second = resolve_ablation_model_compute_cost_fact(
                budget,
                request_id="request-1",
                expected_aggregate_version=4,
            )
            self.assertEqual(replayed, first)
            self.assertEqual(str(first.amount), "3")
            self.assertEqual(str(second.amount), "4")
            self.assertNotEqual(first.evidence_digest, second.evidence_digest)

    def test_old_budget_version_is_stale_at_later_visibility(self):
        with TemporaryDirectory() as directory:
            budget = self._budget(Path(directory))
            budget.reserve("request-1", "10")
            budget.settle("request-1", incurred="3", estimated_unbilled="0")
            budget.reserve("request-2", "1")

            with self.assertRaisesRegex(
                ValueError,
                "version is stale at visibility cut",
            ):
                resolve_ablation_model_compute_cost_fact(
                    budget,
                    request_id="request-1",
                    expected_aggregate_version=3,
                )

    def test_digest_tamper_fails_before_replay(self):
        with TemporaryDirectory() as directory:
            budget = self._budget(Path(directory))
            budget.reserve("request-1", "10")
            budget.settle("request-1", incurred="3", estimated_unbilled="0")
            evidence = resolve_ablation_model_compute_cost_fact(
                budget,
                request_id="request-1",
                expected_aggregate_version=3,
            )
            object.__setattr__(evidence, "evidence_digest", "sha256:" + "f" * 64)

            with self.assertRaisesRegex(
                ValueError,
                "evidence digest does not match canonical material",
            ):
                reverify_ablation_model_compute_cost_fact(budget, evidence)

    def test_instance_shadowed_event_reader_is_rejected(self):
        with TemporaryDirectory() as directory:
            budget = self._budget(Path(directory))
            budget.reserve("request-1", "10")
            budget.settle("request-1", incurred="3", estimated_unbilled="0")
            object.__setattr__(budget, "_events", lambda: [])

            with self.assertRaisesRegex(
                ValueError,
                "shadows canonical methods",
            ):
                resolve_ablation_model_compute_cost_fact(
                    budget,
                    request_id="request-1",
                    expected_aggregate_version=3,
                )


if __name__ == "__main__":
    unittest.main()
