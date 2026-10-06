from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from research.autotrade_research.evaluation.scientific_trial_owner import (
    evaluate_gates_with_scientific_trial_owner,
    gate_profile_subject_digest,
    resolve_scientific_trial_owner,
)
from research.autotrade_research.science.registry import ScientificRegistry
from research.tests.test_evaluation_gates import evidence, profile
from research.tests.test_science_registry import preregister_holdout, protocol


def registered_partial_trial_owner(registry: ScientificRegistry, gate_profile):
    payload = protocol()
    payload["trial_budget"] = 2
    payload["gate_profile_id"] = gate_profile.profile_id
    payload["gate_profile_digest"] = gate_profile_subject_digest(gate_profile)
    registered = registry.register_protocol(payload)
    preregister_holdout(registry, registered.protocol_id)
    registry.record_trial(
        registered.protocol_id,
        status="COMPLETED",
        payload={"candidate": "candidate-1"},
    )
    return registered


class ScientificTrialOwnerCompletenessPolicyTests(unittest.TestCase):
    def test_incomplete_population_is_authoritative_when_profile_does_not_require_completion(self):
        gate_profile = replace(profile(), require_complete_trials=False)
        with TemporaryDirectory() as directory:
            registry = ScientificRegistry(Path(directory) / "science.sqlite3")
            registered_partial_trial_owner(registry, gate_profile)
            gate_evidence = evidence(
                trials_attempted=1,
                trial_log_complete=False,
            )

            owner = resolve_scientific_trial_owner(
                registry=registry,
                profile=gate_profile,
                evidence=gate_evidence,
            )
            decision = evaluate_gates_with_scientific_trial_owner(
                gate_profile,
                gate_evidence,
                scientific_registry=registry,
            )

            self.assertFalse(owner.complete_required)
            self.assertFalse(owner.trial_evidence.complete)
            self.assertTrue(owner.population_matches_gate_evidence)
            self.assertTrue(owner.completion_matches_gate_evidence)
            self.assertTrue(owner.authoritative)
            self.assertEqual(decision.checks["scientific_trial_owner"], "PASS")
            self.assertNotEqual(decision.status, "PASS")

    def test_incomplete_population_is_not_authoritative_when_profile_requires_completion(self):
        gate_profile = replace(profile(), require_complete_trials=True)
        with TemporaryDirectory() as directory:
            registry = ScientificRegistry(Path(directory) / "science.sqlite3")
            registered_partial_trial_owner(registry, gate_profile)
            gate_evidence = evidence(
                trials_attempted=1,
                trial_log_complete=False,
            )

            owner = resolve_scientific_trial_owner(
                registry=registry,
                profile=gate_profile,
                evidence=gate_evidence,
            )
            decision = evaluate_gates_with_scientific_trial_owner(
                gate_profile,
                gate_evidence,
                scientific_registry=registry,
            )

            self.assertTrue(owner.complete_required)
            self.assertFalse(owner.trial_evidence.complete)
            self.assertFalse(owner.authoritative)
            self.assertEqual(decision.checks["scientific_trial_owner"], "FAIL")
            self.assertEqual(decision.status, "FAIL")


if __name__ == "__main__":
    unittest.main()
