from __future__ import annotations

from dataclasses import fields, replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from research.autotrade_research.evaluation.gates import GateProfile
from research.autotrade_research.evaluation.scientific_trial_owner import (
    gate_profile_subject_digest,
    gate_profile_subject_payload,
    resolve_gate_profile_protocol_binding,
)
from research.autotrade_research.science.registry import (
    ProtocolViolation,
    ScientificRegistry,
)
from research.tests.test_evaluation_gates import profile
from research.tests.test_science_registry import protocol


class ScientificTrialOwnerProfileDigestTests(unittest.TestCase):
    def test_subject_payload_covers_every_gate_profile_field(self) -> None:
        gate_profile = profile()
        payload = gate_profile_subject_payload(gate_profile)
        self.assertEqual(
            set(payload) - {"schema_version"},
            {field.name for field in fields(GateProfile)},
        )

    def test_each_boolean_authority_field_changes_profile_digest(self) -> None:
        gate_profile = profile()
        original = gate_profile_subject_digest(gate_profile)
        for field_name in (
            "require_complete_trials",
            "require_causal_audit",
            "require_financial_invariants",
            "require_untouched_holdout",
            "require_walk_forward",
        ):
            with self.subTest(field_name=field_name):
                mutated = replace(
                    gate_profile,
                    **{field_name: not getattr(gate_profile, field_name)},
                )
                self.assertNotEqual(
                    gate_profile_subject_digest(mutated),
                    original,
                )

    def test_sequence_order_is_part_of_exact_profile_subject(self) -> None:
        gate_profile = profile()
        reordered_baselines = replace(
            gate_profile,
            baseline_ids=tuple(reversed(gate_profile.baseline_ids)),
        )
        reordered_regimes = replace(
            gate_profile,
            required_regimes=tuple(reversed(gate_profile.required_regimes)),
        )
        original = gate_profile_subject_digest(gate_profile)
        self.assertNotEqual(gate_profile_subject_digest(reordered_baselines), original)
        self.assertNotEqual(gate_profile_subject_digest(reordered_regimes), original)

    def test_in_place_profile_mutation_after_registration_breaks_owner_binding(self) -> None:
        gate_profile = profile()
        with TemporaryDirectory() as directory:
            registry = ScientificRegistry(Path(directory) / "science.sqlite3")
            payload = protocol()
            payload["gate_profile_id"] = gate_profile.profile_id
            payload["gate_profile_digest"] = gate_profile_subject_digest(gate_profile)
            registry.register_protocol(payload)

            object.__setattr__(
                gate_profile,
                "require_untouched_holdout",
                not gate_profile.require_untouched_holdout,
            )

            with self.assertRaisesRegex(
                ProtocolViolation,
                "rebound to a different immutable profile digest",
            ):
                resolve_gate_profile_protocol_binding(
                    registry=registry,
                    profile=gate_profile,
                )


if __name__ == "__main__":
    unittest.main()
