from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from research.autotrade_research.evaluation.scientific_trial_owner import (
    evaluate_gates_with_scientific_trial_owner,
    gate_profile_subject_digest,
    resolve_gate_profile_protocol_binding,
    resolve_scientific_trial_owner,
)
from research.autotrade_research.science.registry import (
    ProtocolViolation,
    ScientificRegistry,
)
from research.tests.test_evaluation_gates import evidence, profile
from research.tests.test_science_registry import protocol


def bound_protocol(gate_profile, *, trial_budget: int = 12):
    value = deepcopy(protocol())
    value["trial_budget"] = trial_budget
    value["gate_profile_id"] = gate_profile.profile_id
    value["gate_profile_digest"] = gate_profile_subject_digest(gate_profile)
    return value


def fill_trials(registry: ScientificRegistry, protocol_id: str, count: int) -> None:
    for index in range(count):
        registry.record_trial(
            protocol_id,
            status="COMPLETED",
            payload={"candidate": f"candidate-{index}"},
        )


class ScientificTrialOwnerTests(unittest.TestCase):
    def test_unique_preregistered_profile_binding_resolves_without_protocol_id_input(self):
        gate_profile = profile()
        with TemporaryDirectory() as directory:
            registry = ScientificRegistry(Path(directory) / "science.sqlite3")
            registered = registry.register_protocol(bound_protocol(gate_profile))

            binding = resolve_gate_profile_protocol_binding(
                registry=registry,
                profile=gate_profile,
            )

            self.assertEqual(binding.protocol_id, registered.protocol_id)
            self.assertEqual(binding.protocol_hash, registered.protocol_hash)
            self.assertEqual(binding.profile_id, gate_profile.profile_id)
            self.assertEqual(
                binding.profile_digest,
                gate_profile_subject_digest(gate_profile),
            )

    def test_duplicate_preregistered_bindings_fail_closed_instead_of_cherry_picking(self):
        gate_profile = profile()
        with TemporaryDirectory() as directory:
            registry = ScientificRegistry(Path(directory) / "science.sqlite3")
            registry.register_protocol(bound_protocol(gate_profile))
            second = bound_protocol(gate_profile)
            second["hypothesis"] = "same gate profile, second preregistered protocol"
            registry.register_protocol(second)

            with self.assertRaisesRegex(
                ProtocolViolation,
                "multiple immutable scientific protocols",
            ):
                resolve_gate_profile_protocol_binding(
                    registry=registry,
                    profile=gate_profile,
                )

    def test_profile_id_rebound_to_different_profile_digest_fails_closed(self):
        gate_profile = profile()
        with TemporaryDirectory() as directory:
            registry = ScientificRegistry(Path(directory) / "science.sqlite3")
            value = bound_protocol(gate_profile)
            value["gate_profile_digest"] = "sha256:" + "f" * 64
            registry.register_protocol(value)

            with self.assertRaisesRegex(
                ProtocolViolation,
                "rebound to a different immutable profile digest",
            ):
                resolve_gate_profile_protocol_binding(
                    registry=registry,
                    profile=gate_profile,
                )

    def test_protocol_budget_cannot_exceed_bound_gate_max_trials(self):
        gate_profile = profile()
        with TemporaryDirectory() as directory:
            registry = ScientificRegistry(Path(directory) / "science.sqlite3")
            registry.register_protocol(
                bound_protocol(gate_profile, trial_budget=gate_profile.max_trials + 1)
            )

            with self.assertRaisesRegex(
                ProtocolViolation,
                "trial budget exceeds gate profile max_trials",
            ):
                resolve_scientific_trial_owner(
                    registry=registry,
                    profile=gate_profile,
                    evidence=evidence(trials_attempted=0, trial_log_complete=False),
                )

    def test_registry_population_must_match_gate_trial_count(self):
        gate_profile = profile()
        with TemporaryDirectory() as directory:
            registry = ScientificRegistry(Path(directory) / "science.sqlite3")
            registered = registry.register_protocol(
                bound_protocol(gate_profile, trial_budget=3)
            )
            fill_trials(registry, registered.protocol_id, 2)

            owner = resolve_scientific_trial_owner(
                registry=registry,
                profile=gate_profile,
                evidence=evidence(trials_attempted=1, trial_log_complete=False),
            )

            self.assertEqual(owner.trial_evidence.recorded_trials, 2)
            self.assertFalse(owner.population_matches_gate_evidence)
            self.assertTrue(owner.completion_matches_gate_evidence)
            self.assertFalse(owner.authoritative)

    def test_complete_registry_population_can_match_gate_evidence(self):
        gate_profile = profile()
        with TemporaryDirectory() as directory:
            registry = ScientificRegistry(Path(directory) / "science.sqlite3")
            registered = registry.register_protocol(
                bound_protocol(gate_profile, trial_budget=3)
            )
            fill_trials(registry, registered.protocol_id, 3)

            owner = resolve_scientific_trial_owner(
                registry=registry,
                profile=gate_profile,
                evidence=evidence(trials_attempted=3, trial_log_complete=True),
            )

            self.assertTrue(owner.trial_evidence.complete)
            self.assertTrue(owner.population_matches_gate_evidence)
            self.assertTrue(owner.completion_matches_gate_evidence)
            self.assertTrue(owner.authoritative)
            self.assertTrue(owner.digest.startswith("sha256:"))

    def test_registry_connect_instance_shadow_cannot_replace_owner_authority(self):
        gate_profile = profile()
        with TemporaryDirectory() as directory:
            registry = ScientificRegistry(Path(directory) / "science.sqlite3")
            registry.register_protocol(bound_protocol(gate_profile))
            state = {"called": False}

            def hostile_connect(*_args, **_kwargs):
                state["called"] = True
                raise AssertionError("caller-owned registry connection override executed")

            registry._connect = hostile_connect
            with self.assertRaisesRegex(
                TypeError,
                "authority methods must not be instance-shadowed",
            ):
                resolve_gate_profile_protocol_binding(
                    registry=registry,
                    profile=gate_profile,
                )
            self.assertFalse(state["called"])

    def test_registry_trial_evidence_instance_shadow_cannot_replace_owner_authority(self):
        gate_profile = profile()
        with TemporaryDirectory() as directory:
            registry = ScientificRegistry(Path(directory) / "science.sqlite3")
            registered = registry.register_protocol(
                bound_protocol(gate_profile, trial_budget=1)
            )
            fill_trials(registry, registered.protocol_id, 1)
            state = {"called": False}

            def hostile_trial_evidence(*_args, **_kwargs):
                state["called"] = True
                raise AssertionError("caller-owned trial evidence override executed")

            registry.trial_completeness_evidence = hostile_trial_evidence
            with self.assertRaisesRegex(
                TypeError,
                "authority methods must not be instance-shadowed",
            ):
                resolve_scientific_trial_owner(
                    registry=registry,
                    profile=gate_profile,
                    evidence=evidence(trials_attempted=1, trial_log_complete=True),
                )
            self.assertFalse(state["called"])

    def test_wrapper_rejects_registry_shadow_before_base_gate_evaluation(self):
        gate_profile = profile()
        with TemporaryDirectory() as directory:
            registry = ScientificRegistry(Path(directory) / "science.sqlite3")
            registry._connect = lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("hostile connect executed")
            )
            with self.assertRaisesRegex(
                TypeError,
                "authority methods must not be instance-shadowed",
            ):
                evaluate_gates_with_scientific_trial_owner(
                    gate_profile,
                    evidence(),
                    scientific_registry=registry,
                )

    def test_wrapper_reports_missing_registry_owner_as_inconclusive(self):
        gate_profile = profile()
        with TemporaryDirectory() as directory:
            registry = ScientificRegistry(Path(directory) / "science.sqlite3")

            decision = evaluate_gates_with_scientific_trial_owner(
                gate_profile,
                evidence(),
                scientific_registry=registry,
            )

            self.assertEqual(
                decision.checks["scientific_trial_owner"],
                "INCONCLUSIVE",
            )
            self.assertNotEqual(decision.status, "PASS")

    def test_wrapper_fails_duplicate_owner_even_when_caller_evidence_looks_good(self):
        gate_profile = profile()
        with TemporaryDirectory() as directory:
            registry = ScientificRegistry(Path(directory) / "science.sqlite3")
            registry.register_protocol(bound_protocol(gate_profile, trial_budget=3))
            second = bound_protocol(gate_profile, trial_budget=3)
            second["hypothesis"] = "second profile-bound protocol"
            registry.register_protocol(second)

            decision = evaluate_gates_with_scientific_trial_owner(
                gate_profile,
                evidence(trials_attempted=3, trial_log_complete=True),
                scientific_registry=registry,
            )

            self.assertEqual(decision.status, "FAIL")
            self.assertEqual(decision.checks["scientific_trial_owner"], "FAIL")


if __name__ == "__main__":
    unittest.main()
