from __future__ import annotations

from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from research.autotrade_research.evaluation.scientific_trial_owner import (
    gate_profile_subject_digest,
    resolve_scientific_trial_owner,
)
from research.autotrade_research.science.registry import ScientificRegistry
from research.tests.test_evaluation_gates import evidence, profile
from research.tests.test_science_registry import protocol


class ScientificTrialOwnerStableCutTests(unittest.TestCase):
    def test_owner_cut_pins_one_registry_path_even_if_caller_instance_moves(self) -> None:
        gate_profile = profile()
        with TemporaryDirectory() as directory:
            first = ScientificRegistry(Path(directory) / "science-a.sqlite3")
            second = ScientificRegistry(Path(directory) / "science-b.sqlite3")
            payload = protocol()
            payload["trial_budget"] = 1
            payload["gate_profile_id"] = gate_profile.profile_id
            payload["gate_profile_digest"] = gate_profile_subject_digest(gate_profile)
            registered = first.register_protocol(payload)
            first.record_trial(
                registered.protocol_id,
                status="COMPLETED",
                payload={"candidate": "candidate-1"},
            )

            canonical_trial_snapshot = ScientificRegistry.trial_completeness_evidence
            original_path = first.path
            caller_was_moved = False

            def move_caller_then_read(authority, protocol_id: str):
                nonlocal caller_was_moved
                first.path = second.path
                caller_was_moved = True
                self.assertEqual(authority.path, original_path)
                return canonical_trial_snapshot(authority, protocol_id)

            with patch.object(
                ScientificRegistry,
                "trial_completeness_evidence",
                new=move_caller_then_read,
            ):
                owner = resolve_scientific_trial_owner(
                    registry=first,
                    profile=gate_profile,
                    evidence=evidence(trials_attempted=1, trial_log_complete=True),
                )

            self.assertTrue(caller_was_moved)
            self.assertEqual(owner.binding.protocol_id, registered.protocol_id)
            self.assertEqual(owner.trial_evidence.recorded_trials, 1)
            self.assertTrue(owner.authoritative)

    def test_owner_composition_holds_writer_reservation_across_trial_snapshot(self) -> None:
        gate_profile = profile()
        with TemporaryDirectory() as directory:
            registry = ScientificRegistry(Path(directory) / "science.sqlite3")
            payload = protocol()
            payload["trial_budget"] = 1
            payload["gate_profile_id"] = gate_profile.profile_id
            payload["gate_profile_digest"] = gate_profile_subject_digest(gate_profile)
            registered = registry.register_protocol(payload)
            registry.record_trial(
                registered.protocol_id,
                status="COMPLETED",
                payload={"candidate": "candidate-1"},
            )

            canonical_trial_snapshot = ScientificRegistry.trial_completeness_evidence
            writer_was_blocked = False

            def guarded_trial_snapshot(authority, protocol_id: str):
                nonlocal writer_was_blocked
                competing = sqlite3.connect(authority.path, timeout=0)
                try:
                    with self.assertRaises(sqlite3.OperationalError):
                        competing.execute("BEGIN IMMEDIATE")
                    writer_was_blocked = True
                finally:
                    competing.close()
                return canonical_trial_snapshot(authority, protocol_id)

            with patch.object(
                ScientificRegistry,
                "trial_completeness_evidence",
                new=guarded_trial_snapshot,
            ):
                owner = resolve_scientific_trial_owner(
                    registry=registry,
                    profile=gate_profile,
                    evidence=evidence(trials_attempted=1, trial_log_complete=True),
                )

            self.assertTrue(writer_was_blocked)
            self.assertEqual(owner.binding.protocol_id, registered.protocol_id)
            self.assertEqual(owner.trial_evidence.recorded_trials, 1)
            self.assertTrue(owner.authoritative)


if __name__ == "__main__":
    unittest.main()
