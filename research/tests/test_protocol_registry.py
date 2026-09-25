from __future__ import annotations

import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from research.autotrade_research.science import (
    ProtocolConflict,
    ProtocolViolation,
    ScientificRegistry,
)


def protocol() -> dict:
    return {
        "hypothesis": "candidate improves net utility over baseline",
        "strategy": {"name": "deterministic-baseline"},
        "features": ["return_5m"],
        "search_space": {"threshold": ["0.01", "0.02"]},
        "train_period": {"start": "2024-01-01", "end": "2024-12-31"},
        "validation_period": {"start": "2025-01-01", "end": "2025-06-30"},
        "test_period": {"start": "2025-07-01", "end": "2025-12-31"},
        "forward_period": {"start": "2026-01-01", "end": "2026-06-30"},
        "labels": ["net_return_after_cost"],
        "horizons": ["1d"],
        "purge_embargo": {"purge": "1d", "embargo": "1d"},
        "universe": ["BTC-USD"],
        "cost_fill_model": {"fee_bps": "10"},
        "baselines": ["cash", "existing_champion"],
        "primary_metrics": [{"name": "net_utility", "direction": "max"}],
        "secondary_metrics": [{"name": "drawdown", "direction": "min"}],
        "trial_budget": 2,
        "stopping_rules": {"max_failures": 2},
        "statistical_estimator": {"name": "block_bootstrap"},
        "multiplicity_treatment": {"method": "holm"},
        "minimum_practical_effect": "0.015",
        "risk_constraints": {"max_drawdown": "0.20"},
        "retention_tolerances": {"max_degradation": "0.02"},
        "promotion_rule": {"lower_bound_gt": "0.015"},
    }


class ProtocolRegistryHardeningTests(unittest.TestCase):
    def test_protocol_identity_is_immutable_and_idempotent(self):
        with TemporaryDirectory() as directory:
            registry = ScientificRegistry(Path(directory) / "science.sqlite3")
            protocol_id = "00000000-0000-0000-0000-000000000001"
            first = registry.register_protocol(protocol(), protocol_id=protocol_id)
            second = registry.register_protocol(protocol(), protocol_id=protocol_id)
            self.assertEqual(first.protocol_hash, second.protocol_hash)

            changed = protocol()
            changed["minimum_practical_effect"] = "0.001"
            with self.assertRaisesRegex(ProtocolConflict, "immutable"):
                registry.register_protocol(changed, protocol_id=protocol_id)

    def test_binary_float_is_rejected_from_frozen_scientific_evidence(self):
        with TemporaryDirectory() as directory:
            registry = ScientificRegistry(Path(directory) / "science.sqlite3")
            bad = protocol()
            bad["minimum_practical_effect"] = 0.015
            with self.assertRaisesRegex(ProtocolViolation, "binary float"):
                registry.register_protocol(bad)

    def test_protocol_rejects_overlapping_or_reversed_causal_periods(self):
        with TemporaryDirectory() as directory:
            registry = ScientificRegistry(Path(directory) / "science.sqlite3")

            overlap = protocol()
            overlap["validation_period"] = {
                "start": "2024-12-31",
                "end": "2025-06-30",
            }
            with self.assertRaisesRegex(ProtocolViolation, "must end before"):
                registry.register_protocol(overlap)

            reversed_period = protocol()
            reversed_period["test_period"] = {
                "start": "2025-12-31",
                "end": "2025-07-01",
            }
            with self.assertRaisesRegex(ProtocolViolation, "start cannot follow end"):
                registry.register_protocol(reversed_period)

            malformed = protocol()
            malformed["forward_period"] = {
                "start": "2026/01/01",
                "end": "2026-06-30",
            }
            with self.assertRaisesRegex(ProtocolViolation, "ISO calendar dates"):
                registry.register_protocol(malformed)

    def test_failed_and_discarded_trials_consume_registered_budget(self):
        with TemporaryDirectory() as directory:
            registry = ScientificRegistry(Path(directory) / "science.sqlite3")
            registered = registry.register_protocol(protocol())
            registry.record_trial(
                registered.protocol_id,
                status="FAILED",
                payload={"reason": "fit_failed"},
            )
            registry.record_trial(
                registered.protocol_id,
                status="DISCARDED",
                payload={"reason": "invalid_candidate"},
            )
            completeness = registry.completeness(registered.protocol_id)
            self.assertEqual(completeness["recorded_trials"], 2)
            self.assertEqual(completeness["remaining_trial_budget"], 0)
            self.assertTrue(completeness["includes_non_successes"])

            with self.assertRaisesRegex(ProtocolViolation, "budget exhausted"):
                registry.record_trial(
                    registered.protocol_id,
                    status="COMPLETED",
                    payload={"metric": "0.02"},
                )

    def test_repeated_holdout_use_cannot_remain_untouched(self):
        with TemporaryDirectory() as directory:
            registry = ScientificRegistry(Path(directory) / "science.sqlite3")
            registered = registry.register_protocol(protocol())
            holdout = "forward-2026-h1"

            first = registry.register_evaluation(
                registered.protocol_id,
                holdout_id=holdout,
                result={"net_utility": "0.020"},
            )
            self.assertEqual(first["prior_access_count"], 0)
            self.assertEqual(first["untouched"], 1)

            second = registry.register_evaluation(
                registered.protocol_id,
                holdout_id=holdout,
                result={"net_utility": "0.021"},
            )
            self.assertEqual(second["prior_access_count"], 1)
            self.assertEqual(second["untouched"], 0)
            self.assertEqual(
                registry.holdout_access_count(registered.protocol_id, holdout), 2
            )

    def test_holdout_access_by_one_protocol_contaminates_other_protocols(self):
        with TemporaryDirectory() as directory:
            registry = ScientificRegistry(Path(directory) / "science.sqlite3")
            first_protocol = registry.register_protocol(protocol())

            second_payload = protocol()
            second_payload["hypothesis"] = "independent candidate over same locked segment"
            second_protocol = registry.register_protocol(second_payload)

            holdout = "forward-2026-h1"
            registry.record_holdout_access(
                first_protocol.protocol_id,
                holdout_id=holdout,
                purpose="candidate-A-inspection",
            )

            evaluation = registry.register_evaluation(
                second_protocol.protocol_id,
                holdout_id=holdout,
                result={"net_utility": "0.019"},
            )
            self.assertEqual(evaluation["prior_access_count"], 1)
            self.assertEqual(evaluation["untouched"], 0)

    def test_manual_holdout_access_contaminates_later_locked_evaluation(self):
        with TemporaryDirectory() as directory:
            registry = ScientificRegistry(Path(directory) / "science.sqlite3")
            registered = registry.register_protocol(protocol())
            holdout = "forward-2026-h1"
            registry.record_holdout_access(
                registered.protocol_id,
                holdout_id=holdout,
                purpose="parameter_selection",
            )
            evaluation = registry.register_evaluation(
                registered.protocol_id,
                holdout_id=holdout,
                result={"net_utility": "0.019"},
            )
            self.assertEqual(evaluation["prior_access_count"], 1)
            self.assertEqual(evaluation["untouched"], 0)

    def test_database_triggers_block_destructive_rewrites(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "science.sqlite3"
            registry = ScientificRegistry(path)
            registered = registry.register_protocol(protocol())
            connection = sqlite3.connect(path)
            try:
                with self.assertRaisesRegex(sqlite3.DatabaseError, "append-only"):
                    connection.execute(
                        "UPDATE protocols SET payload_json='{}' WHERE protocol_id=?",
                        (registered.protocol_id,),
                    )
                with self.assertRaisesRegex(sqlite3.DatabaseError, "append-only"):
                    connection.execute(
                        "DELETE FROM protocols WHERE protocol_id=?",
                        (registered.protocol_id,),
                    )
            finally:
                connection.close()

    def test_registry_state_survives_restart(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "science.sqlite3"
            first = ScientificRegistry(path)
            registered = first.register_protocol(protocol())
            first.record_trial(
                registered.protocol_id,
                status="FAILED",
                payload={"reason": "fit_failed"},
            )

            reopened = ScientificRegistry(path)
            completeness = reopened.completeness(registered.protocol_id)
            self.assertEqual(completeness["recorded_trials"], 1)
            self.assertEqual(completeness["statuses"], {"FAILED": 1})


if __name__ == "__main__":
    unittest.main()
