from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import autotrade_research.memory.reconciled_outcome as reconciled_module
from autotrade_research.evaluation.ablation import CanonicalAblationOutcomeEvidence
from autotrade_research.evaluation.ablation_outcome_binding import (
    bind_ablation_outcome_to_reconciled_fact,
)
from autotrade_research.memory.episodes import ExperienceMemory, MemoryIntegrityError
from autotrade_research.memory.reconciled_outcome import (
    ReconciledOutcomeFactEvidence,
    resolve_reconciled_outcome_fact,
    reverify_reconciled_outcome_fact,
)


BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)
EPISODE_ID = "99999999-9999-4999-8999-999999999999"
DIGEST = "sha256:" + "a" * 64


class ReconciledOutcomeExecutableAuthorityTests(unittest.TestCase):
    def _memory(self, path: Path) -> ExperienceMemory:
        memory = ExperienceMemory(path)
        with patch(
            "autotrade_research.memory.episodes._utc_now",
            return_value=BASE,
        ):
            memory.append_episode(
                episode_id=EPISODE_ID,
                decision_time=BASE,
                information_cutoff=BASE,
                task="exec-authority",
                regime="test",
                instrument_family="EQUITY",
                permission_class="RESEARCH",
                payload={
                    "evidence_refs": ["artifact:source"],
                    "intended_action": {"side": "HOLD"},
                    "actual_execution": {"fills": []},
                    "outcome": {
                        "class": "POSITIVE",
                        "label": "observed",
                        "label_mature": True,
                        "reconciliation_state": "RECONCILED",
                    },
                    "costs": {"USD": "0"},
                },
            )
        return memory

    def _fact(self, memory: ExperienceMemory):
        return resolve_reconciled_outcome_fact(
            memory,
            episode_id=EPISODE_ID,
            causal_cutoff=BASE + timedelta(days=1),
            granted_permissions={"RESEARCH"},
            task="exec-authority",
            instrument_family="EQUITY",
        )

    def _canonical_outcome(self, fact) -> CanonicalAblationOutcomeEvidence:
        return CanonicalAblationOutcomeEvidence(
            case_id="case-exec",
            variant="FULL",
            population_unit_id=EPISODE_ID,
            utility=Decimal("999999"),
            cost=Decimal("999999"),
            outcome_available_utc=BASE,
            source_revision="b" * 40,
            utility_evidence_digest=fact.evidence_digest,
            cost_evidence_digest=DIGEST,
            evidence_digest=DIGEST,
        )

    def test_class_level_coverage_rebinding_fails_before_hostile_callback(self):
        calls: list[str] = []
        with TemporaryDirectory() as directory:
            memory = self._memory(Path(directory) / "memory.sqlite3")
            original = ExperienceMemory.__dict__["coverage_population_snapshot"]

            def hostile(self, *args, **kwargs):
                calls.append("coverage")
                raise AssertionError("hostile coverage executed")

            setattr(ExperienceMemory, "coverage_population_snapshot", hostile)
            try:
                with self.assertRaisesRegex(
                    MemoryIntegrityError,
                    "coverage executable changed",
                ):
                    self._fact(memory)
            finally:
                setattr(ExperienceMemory, "coverage_population_snapshot", original)
            self.assertEqual(calls, [])

    def test_class_level_persistence_rebinding_fails_before_hostile_callback(self):
        calls: list[str] = []
        with TemporaryDirectory() as directory:
            memory = self._memory(Path(directory) / "memory.sqlite3")
            original = ExperienceMemory.__dict__["_connect"]

            def hostile(self, *args, **kwargs):
                calls.append("connect")
                raise AssertionError("hostile persistence executed")

            setattr(ExperienceMemory, "_connect", hostile)
            try:
                with self.assertRaisesRegex(
                    MemoryIntegrityError,
                    "persistence executable changed",
                ):
                    self._fact(memory)
            finally:
                setattr(ExperienceMemory, "_connect", original)
            self.assertEqual(calls, [])

    def test_public_fact_integrity_method_rebinding_cannot_retarget_reverification(self):
        calls: list[str] = []
        with TemporaryDirectory() as directory:
            memory = self._memory(Path(directory) / "memory.sqlite3")
            fact = self._fact(memory)
            original = ReconciledOutcomeFactEvidence.__dict__["verify_integrity"]

            def hostile(self):
                calls.append("verify")
                raise AssertionError("hostile public verifier executed")

            setattr(ReconciledOutcomeFactEvidence, "verify_integrity", hostile)
            try:
                reverified = reverify_reconciled_outcome_fact(memory, fact)
            finally:
                setattr(ReconciledOutcomeFactEvidence, "verify_integrity", original)
            self.assertEqual(reverified, fact)
            self.assertEqual(calls, [])

    def test_late_internal_module_globals_cannot_retarget_reconciled_fact_authority(self):
        calls: list[str] = []
        with TemporaryDirectory() as directory:
            memory = self._memory(Path(directory) / "memory.sqlite3")
            expected = self._fact(memory)

            def hostile(*args, **kwargs):
                calls.append("hostile")
                raise AssertionError(
                    "late reconciled-outcome module global must not execute"
                )

            with (
                patch.object(reconciled_module, "_COVERAGE_READ", new=hostile),
                patch.object(reconciled_module, "_COVERAGE_VERIFY", new=hostile),
                patch.object(reconciled_module, "_validate_query", new=hostile),
                patch.object(reconciled_module, "_hash", new=hostile),
                patch.object(
                    reconciled_module,
                    "_assert_memory_authority",
                    new=hostile,
                ),
                patch.object(
                    reconciled_module,
                    "_verify_fact_integrity",
                    new=hostile,
                ),
                patch.object(reconciled_module, "_cutoff", new=hostile),
                patch.object(reconciled_module, "_text", new=hostile),
                patch.object(reconciled_module, "_digest", new=hostile),
                patch.object(reconciled_module, "_freeze", new=hostile),
                patch.object(reconciled_module, "_canonical_value", new=hostile),
                patch.object(reconciled_module, "_fact_identity_payload", new=hostile),
                patch.object(reconciled_module, "ExperienceMemory", new=object),
                patch.object(
                    reconciled_module,
                    "CoveragePopulationSnapshot",
                    new=object,
                ),
                patch.object(reconciled_module, "MemoryIntegrityError", new=RuntimeError),
                patch.object(reconciled_module, "MappingProxyType", new=object),
                patch.object(reconciled_module, "datetime", new=object),
                patch.object(reconciled_module, "timezone", new=object),
                patch.object(reconciled_module, "UUID", new=object),
                patch.object(reconciled_module, "sha256", new=hostile),
            ):
                resolved = resolve_reconciled_outcome_fact(
                    memory,
                    episode_id=EPISODE_ID,
                    causal_cutoff=BASE + timedelta(days=1),
                    granted_permissions={"RESEARCH"},
                    task="exec-authority",
                    instrument_family="EQUITY",
                )
                reverified = reverify_reconciled_outcome_fact(memory, expected)

            self.assertEqual(resolved, expected)
            self.assertEqual(reverified, expected)
            self.assertEqual(calls, [])

    def test_late_module_global_resolver_rebinding_cannot_retarget_bound_bridge(self):
        calls: list[str] = []
        with TemporaryDirectory() as directory:
            memory = self._memory(Path(directory) / "memory.sqlite3")
            fact = self._fact(memory)
            outcome = self._canonical_outcome(fact)
            original = reconciled_module.resolve_reconciled_outcome_fact

            def hostile(*args, **kwargs):
                calls.append("resolve")
                raise AssertionError("late module-global resolver executed")

            reconciled_module.resolve_reconciled_outcome_fact = hostile
            try:
                bound = bind_ablation_outcome_to_reconciled_fact(
                    memory,
                    outcome,
                    causal_cutoff=BASE + timedelta(days=1),
                    granted_permissions={"RESEARCH"},
                    task="exec-authority",
                    instrument_family="EQUITY",
                )
            finally:
                reconciled_module.resolve_reconciled_outcome_fact = original
            self.assertEqual(bound.reconciled_fact, fact)
            self.assertEqual(calls, [])

    def test_bridge_still_never_promotes_artifact_numeric_economics(self):
        with TemporaryDirectory() as directory:
            memory = self._memory(Path(directory) / "memory.sqlite3")
            fact = self._fact(memory)
            bound = bind_ablation_outcome_to_reconciled_fact(
                memory,
                self._canonical_outcome(fact),
                causal_cutoff=BASE + timedelta(days=1),
                granted_permissions={"RESEARCH"},
                task="exec-authority",
                instrument_family="EQUITY",
            )
            self.assertFalse(hasattr(bound, "utility"))
            self.assertFalse(hasattr(bound, "cost"))


if __name__ == "__main__":
    unittest.main()
