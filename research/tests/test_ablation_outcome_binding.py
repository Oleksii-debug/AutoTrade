from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from autotrade_research.evaluation.ablation import CanonicalAblationOutcomeEvidence
from autotrade_research.evaluation.ablation_outcome_binding import (
    BoundReconciledAblationOutcome,
    bind_ablation_outcome_to_reconciled_fact,
)
from autotrade_research.memory.episodes import ExperienceMemory, MemoryIntegrityError
from autotrade_research.memory.reconciled_outcome import resolve_reconciled_outcome_fact


BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)
EPISODE_ID = "00000000-0000-0000-0000-000000000063"
DIGEST = "sha256:" + "a" * 64


def payload() -> dict:
    return {
        "evidence_refs": ["artifact:canonical-reconciled-outcome"],
        "intended_action": {"side": "HOLD"},
        "actual_execution": {"fills": []},
        "outcome": {
            "class": "POSITIVE",
            "label": "realized",
            "label_mature": True,
            "reconciliation_state": "RECONCILED",
            "task_value_fact": "positive",
        },
        "costs": {"USD": "0"},
    }


class AblationOutcomeBindingTests(unittest.TestCase):
    def _memory(self, path: Path) -> ExperienceMemory:
        store = ExperienceMemory(path)
        with patch(
            "autotrade_research.memory.episodes._utc_now",
            return_value=BASE,
        ):
            store.append_episode(
                episode_id=EPISODE_ID,
                decision_time=BASE,
                information_cutoff=BASE,
                task="wp63-ablation",
                regime="historical",
                instrument_family="equity",
                permission_class="research",
                payload=payload(),
            )
        return store

    def _fact(self, store: ExperienceMemory):
        return resolve_reconciled_outcome_fact(
            store,
            episode_id=EPISODE_ID,
            causal_cutoff=BASE + timedelta(days=1),
            granted_permissions={"research"},
            task="wp63-ablation",
            instrument_family="equity",
        )

    def _canonical_outcome(
        self,
        utility_evidence_digest: str,
        *,
        utility: str = "999999",
        outcome_available_utc: datetime = BASE,
        superseded_at_utc: datetime | None = None,
    ) -> CanonicalAblationOutcomeEvidence:
        return CanonicalAblationOutcomeEvidence(
            case_id="case-63",
            variant="FULL",
            population_unit_id=EPISODE_ID,
            utility=Decimal(utility),
            cost=Decimal("999999"),
            outcome_available_utc=outcome_available_utc,
            source_revision="a" * 40,
            utility_evidence_digest=utility_evidence_digest,
            cost_evidence_digest=DIGEST,
            evidence_digest=DIGEST,
            superseded_at_utc=superseded_at_utc,
        )

    def test_matching_digest_binds_fact_without_promoting_artifact_numeric_values(self):
        with TemporaryDirectory() as directory:
            store = self._memory(Path(directory) / "memory.sqlite3")
            fact = self._fact(store)
            outcome = self._canonical_outcome(fact.evidence_digest)

            bound = bind_ablation_outcome_to_reconciled_fact(
                store,
                outcome,
                causal_cutoff=BASE + timedelta(days=1),
                granted_permissions={"research"},
                task="wp63-ablation",
                instrument_family="equity",
            )

            self.assertEqual(bound.reconciled_fact, fact)
            self.assertEqual(bound.population_unit_id, EPISODE_ID)
            self.assertEqual(bound.effective_outcome_available_utc, BASE)
            self.assertFalse(hasattr(bound, "utility"))
            self.assertFalse(hasattr(bound, "cost"))

    def test_artifact_numeric_utility_is_not_the_bound_fact(self):
        with TemporaryDirectory() as directory:
            store = self._memory(Path(directory) / "memory.sqlite3")
            fact = self._fact(store)
            positive = self._canonical_outcome(fact.evidence_digest, utility="1000000")
            negative = self._canonical_outcome(fact.evidence_digest, utility="-1000000")

            positive_bound = bind_ablation_outcome_to_reconciled_fact(
                store,
                positive,
                causal_cutoff=BASE + timedelta(days=1),
                granted_permissions={"research"},
                task="wp63-ablation",
                instrument_family="equity",
            )
            negative_bound = bind_ablation_outcome_to_reconciled_fact(
                store,
                negative,
                causal_cutoff=BASE + timedelta(days=1),
                granted_permissions={"research"},
                task="wp63-ablation",
                instrument_family="equity",
            )

            self.assertEqual(
                positive_bound.reconciled_fact,
                negative_bound.reconciled_fact,
            )

    def test_wrong_utility_evidence_digest_fails_closed(self):
        with TemporaryDirectory() as directory:
            store = self._memory(Path(directory) / "memory.sqlite3")
            outcome = self._canonical_outcome("sha256:" + "b" * 64)

            with self.assertRaisesRegex(
                MemoryIntegrityError,
                "utility evidence digest does not match",
            ):
                bind_ablation_outcome_to_reconciled_fact(
                    store,
                    outcome,
                    causal_cutoff=BASE + timedelta(days=1),
                    granted_permissions={"research"},
                    task="wp63-ablation",
                    instrument_family="equity",
                )

    def test_artifact_cannot_claim_outcome_available_before_memory_fact(self):
        with TemporaryDirectory() as directory:
            store = self._memory(Path(directory) / "memory.sqlite3")
            fact = self._fact(store)
            outcome = self._canonical_outcome(
                fact.evidence_digest,
                outcome_available_utc=BASE - timedelta(microseconds=1),
            )

            with self.assertRaisesRegex(
                MemoryIntegrityError,
                "claims availability before canonical",
            ):
                bind_ablation_outcome_to_reconciled_fact(
                    store,
                    outcome,
                    causal_cutoff=BASE + timedelta(days=1),
                    granted_permissions={"research"},
                    task="wp63-ablation",
                    instrument_family="equity",
                )

    def test_artifact_after_selected_cut_fails_closed(self):
        with TemporaryDirectory() as directory:
            store = self._memory(Path(directory) / "memory.sqlite3")
            fact = self._fact(store)
            outcome = self._canonical_outcome(
                fact.evidence_digest,
                outcome_available_utc=BASE + timedelta(days=2),
            )

            with self.assertRaisesRegex(
                MemoryIntegrityError,
                "not available by selected causal cut",
            ):
                bind_ablation_outcome_to_reconciled_fact(
                    store,
                    outcome,
                    causal_cutoff=BASE + timedelta(days=1),
                    granted_permissions={"research"},
                    task="wp63-ablation",
                    instrument_family="equity",
                )

    def test_superseded_artifact_at_cut_fails_closed(self):
        with TemporaryDirectory() as directory:
            store = self._memory(Path(directory) / "memory.sqlite3")
            fact = self._fact(store)
            outcome = self._canonical_outcome(
                fact.evidence_digest,
                superseded_at_utc=BASE + timedelta(hours=1),
            )

            with self.assertRaisesRegex(
                MemoryIntegrityError,
                "superseded ablation outcome cannot bind",
            ):
                bind_ablation_outcome_to_reconciled_fact(
                    store,
                    outcome,
                    causal_cutoff=BASE + timedelta(days=1),
                    granted_permissions={"research"},
                    task="wp63-ablation",
                    instrument_family="equity",
                )

    def test_causal_cutoff_requires_exact_utc_datetime(self):
        with TemporaryDirectory() as directory:
            store = self._memory(Path(directory) / "memory.sqlite3")
            fact = self._fact(store)
            outcome = self._canonical_outcome(fact.evidence_digest)

            with self.assertRaisesRegex(
                ValueError,
                "causal_cutoff must use exact UTC timezone",
            ):
                bind_ablation_outcome_to_reconciled_fact(
                    store,
                    outcome,
                    causal_cutoff=datetime(2026, 1, 2),
                    granted_permissions={"research"},
                    task="wp63-ablation",
                    instrument_family="equity",
                )

    def test_direct_bound_value_rejects_noncanonical_artifact_digest(self):
        with TemporaryDirectory() as directory:
            store = self._memory(Path(directory) / "memory.sqlite3")
            fact = self._fact(store)
            with self.assertRaisesRegex(
                MemoryIntegrityError,
                "ablation_artifact_digest must be a sha256 digest",
            ):
                BoundReconciledAblationOutcome(
                    case_id="case-63",
                    variant="FULL",
                    population_unit_id=EPISODE_ID,
                    ablation_artifact_digest="caller-digest",
                    reconciled_fact=fact,
                    effective_outcome_available_utc=BASE,
                )

    def test_direct_bound_value_rejects_availability_after_fact_cutoff(self):
        with TemporaryDirectory() as directory:
            store = self._memory(Path(directory) / "memory.sqlite3")
            fact = self._fact(store)
            with self.assertRaisesRegex(
                MemoryIntegrityError,
                "availability cannot follow reconciled fact cutoff",
            ):
                BoundReconciledAblationOutcome(
                    case_id="case-63",
                    variant="FULL",
                    population_unit_id=EPISODE_ID,
                    ablation_artifact_digest=DIGEST,
                    reconciled_fact=fact,
                    effective_outcome_available_utc=BASE + timedelta(days=2),
                )

    def test_memory_subclass_is_rejected_before_virtual_read(self):
        calls: list[str] = []

        class HostileMemory(ExperienceMemory):
            def coverage_population_snapshot(self, *args, **kwargs):
                calls.append("coverage")
                raise AssertionError("hostile memory callback executed")

        with TemporaryDirectory() as directory:
            hostile = HostileMemory(Path(directory) / "memory.sqlite3")
            outcome = self._canonical_outcome(DIGEST)
            with self.assertRaisesRegex(TypeError, "exact ExperienceMemory"):
                bind_ablation_outcome_to_reconciled_fact(
                    hostile,
                    outcome,
                    causal_cutoff=BASE + timedelta(days=1),
                    granted_permissions={"research"},
                )
        self.assertEqual(calls, [])

    def test_instance_shadowed_population_reader_is_not_executed(self):
        calls: list[str] = []
        with TemporaryDirectory() as directory:
            store = self._memory(Path(directory) / "memory.sqlite3")
            fact = self._fact(store)
            outcome = self._canonical_outcome(fact.evidence_digest)

            def hostile(*args, **kwargs):
                calls.append("coverage")
                raise AssertionError("instance population reader executed")

            store.coverage_population_snapshot = hostile
            with self.assertRaisesRegex(
                MemoryIntegrityError,
                "shadows canonical executables",
            ):
                bind_ablation_outcome_to_reconciled_fact(
                    store,
                    outcome,
                    causal_cutoff=BASE + timedelta(days=1),
                    granted_permissions={"research"},
                    task="wp63-ablation",
                    instrument_family="equity",
                )
            self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
