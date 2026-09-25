import copy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from research.autotrade_research.science.registry import (
    ProtocolConflict,
    ProtocolViolation,
    ScientificRegistry,
)


def protocol():
    return {
        "hypothesis": "registered before locked evaluation",
        "strategy": "deterministic baseline",
        "features": ["price_return"],
        "search_space": {"lookback": [5, 10]},
        "train_period": {
            "start": "2025-01-01T00:00:00Z",
            "end": "2025-12-31T23:59:59Z",
        },
        "validation_period": {
            "start": "2026-01-02T00:00:00Z",
            "end": "2026-03-31T23:59:59Z",
        },
        "test_period": {
            "start": "2026-04-02T00:00:00Z",
            "end": "2026-06-30T23:59:59Z",
        },
        "forward_period": {
            "start": "2026-07-02T00:00:00Z",
            "end": "2026-09-30T23:59:59Z",
        },
        "labels": ["net_return"],
        "horizons": ["1d"],
        "purge_embargo": {
            "purge_seconds": 86400,
            "embargo_seconds": 86400,
        },
        "universe": ["AAA"],
        "cost_fill_model": "base-v1",
        "baselines": ["cash", "passive"],
        "primary_metrics": ["net_advantage"],
        "secondary_metrics": ["drawdown"],
        "trial_budget": 3,
        "stopping_rules": "budget or invariant failure",
        "statistical_estimator": "dependence-aware",
        "multiplicity_treatment": "registered correction",
        "minimum_practical_effect": "0.001",
        "risk_constraints": {"max_drawdown": "0.10"},
        "retention_tolerances": {"prior_regime_loss": "0.02"},
        "promotion_rule": "all registered gates",
    }


class ScientificRegistryTests(unittest.TestCase):
    def test_protocol_windows_are_machine_checked_for_causal_order(self):
        with TemporaryDirectory() as directory:
            registry = ScientificRegistry(Path(directory) / "science.sqlite3")

            opaque = protocol()
            opaque["train_period"] = "t0-t1"
            with self.assertRaisesRegex(ProtocolViolation, "train_period must be an object"):
                registry.register_protocol(opaque)

            reversed_window = protocol()
            reversed_window["validation_period"] = {
                "start": "2026-03-31T23:59:59Z",
                "end": "2026-01-02T00:00:00Z",
            }
            with self.assertRaisesRegex(ProtocolViolation, "strictly before end"):
                registry.register_protocol(reversed_window)

            overlapping = protocol()
            overlapping["validation_period"] = {
                "start": "2025-12-31T23:00:00Z",
                "end": "2026-03-31T23:59:59Z",
            }
            with self.assertRaisesRegex(
                ProtocolViolation,
                "train_period must end strictly before validation_period",
            ):
                registry.register_protocol(overlapping)

            short_gap = protocol()
            short_gap["validation_period"] = {
                "start": "2026-01-01T12:00:00Z",
                "end": "2026-03-31T23:59:59Z",
            }
            with self.assertRaisesRegex(
                ProtocolViolation,
                "gap is shorter than registered purge/embargo",
            ):
                registry.register_protocol(short_gap)

            invalid_exclusion = protocol()
            invalid_exclusion["purge_embargo"] = {
                "purge_seconds": True,
                "embargo_seconds": 86400,
            }
            with self.assertRaisesRegex(
                ProtocolViolation,
                "purge_embargo.purge_seconds must be a non-negative integer",
            ):
                registry.register_protocol(invalid_exclusion)

    def test_protocol_is_immutable_after_registration(self):
        with TemporaryDirectory() as directory:
            store = ScientificRegistry(Path(directory) / "science.sqlite3")
            identifier = "00000000-0000-0000-0000-000000000001"
            first = store.register_protocol(protocol(), protocol_id=identifier)
            again = store.register_protocol(protocol(), protocol_id=identifier)
            self.assertEqual(first.protocol_hash, again.protocol_hash)
            changed = copy.deepcopy(protocol())
            changed["minimum_practical_effect"] = "0.0001"
            with self.assertRaises(ProtocolConflict):
                store.register_protocol(changed, protocol_id=identifier)

    def test_missing_registration_fields_fail_closed(self):
        with TemporaryDirectory() as directory:
            store = ScientificRegistry(Path(directory) / "science.sqlite3")
            value = protocol()
            del value["promotion_rule"]
            with self.assertRaises(ProtocolViolation):
                store.register_protocol(value)

    def test_present_but_empty_required_field_fails_closed(self):
        with TemporaryDirectory() as directory:
            store = ScientificRegistry(Path(directory) / "science.sqlite3")
            value = protocol()
            value["primary_metrics"] = []
            with self.assertRaisesRegex(ProtocolViolation, "cannot be empty"):
                store.register_protocol(value)

    def test_failed_and_discarded_trials_are_preserved(self):
        with TemporaryDirectory() as directory:
            store = ScientificRegistry(Path(directory) / "science.sqlite3")
            p = store.register_protocol(protocol())
            store.record_trial(p.protocol_id, status="FAILED", payload={"reason": "fit"})
            store.record_trial(p.protocol_id, status="DISCARDED", payload={"reason": "constraint"})
            state = store.completeness(p.protocol_id)
            self.assertEqual(state["recorded_trials"], 2)
            self.assertTrue(state["includes_non_successes"])
            self.assertEqual(state["remaining_trial_budget"], 1)

    def test_completeness_hash_binds_all_recorded_trial_outcomes(self):
        with TemporaryDirectory() as directory:
            store = ScientificRegistry(Path(directory) / "science.sqlite3")
            p = store.register_protocol(protocol())
            before = store.completeness(p.protocol_id)
            store.record_trial(
                p.protocol_id,
                status="FAILED",
                payload={"reason": "fit"},
            )
            after = store.completeness(p.protocol_id)
            self.assertNotEqual(before["trial_log_hash"], after["trial_log_hash"])
            self.assertEqual(after["recorded_trials"], 1)
            self.assertTrue(after["includes_non_successes"])

    def test_trial_budget_cannot_be_silently_exceeded(self):
        with TemporaryDirectory() as directory:
            store = ScientificRegistry(Path(directory) / "science.sqlite3")
            value = protocol()
            value["trial_budget"] = 1
            p = store.register_protocol(value)
            store.record_trial(p.protocol_id, status="COMPLETED", payload={"x": 1})
            with self.assertRaises(ProtocolViolation):
                store.record_trial(p.protocol_id, status="COMPLETED", payload={"x": 2})

    def test_first_locked_evaluation_is_untouched_then_repeat_is_contaminated(self):
        with TemporaryDirectory() as directory:
            store = ScientificRegistry(Path(directory) / "science.sqlite3")
            p = store.register_protocol(protocol())
            first = store.register_evaluation(p.protocol_id, holdout_id="holdout-A", result={"score": "0.1"})
            self.assertEqual(first["untouched"], 1)
            second = store.register_evaluation(p.protocol_id, holdout_id="holdout-A", result={"score": "0.2"})
            self.assertEqual(second["untouched"], 0)
            self.assertGreaterEqual(second["prior_access_count"], 1)

    def test_manual_holdout_peek_contaminates_locked_evaluation(self):
        with TemporaryDirectory() as directory:
            store = ScientificRegistry(Path(directory) / "science.sqlite3")
            p = store.register_protocol(protocol())
            store.record_holdout_access(p.protocol_id, holdout_id="holdout-A", purpose="manual inspection")
            result = store.register_evaluation(p.protocol_id, holdout_id="holdout-A", result={"score": "0.1"})
            self.assertEqual(result["untouched"], 0)
            self.assertEqual(result["prior_access_count"], 1)

    def test_locked_evaluation_exposes_exact_immutable_hashes(self):
        with TemporaryDirectory() as directory:
            store = ScientificRegistry(Path(directory) / "science.sqlite3")
            p = store.register_protocol(protocol())
            row = store.register_evaluation(
                p.protocol_id,
                holdout_id="holdout-A",
                result={"score": "0.1"},
            )
            evidence = store.locked_evaluation(row["evaluation_id"])
            self.assertEqual(evidence.protocol_id, p.protocol_id)
            self.assertEqual(evidence.protocol_hash, p.protocol_hash)
            self.assertEqual(evidence.result_hash, row["result_hash"])
            self.assertTrue(evidence.untouched)
            self.assertEqual(evidence.prior_access_count, 0)
            self.assertEqual(evidence.result, {"score": "0.1"})

    def test_triggered_stopping_rule_requires_immutable_artifact_evidence(self):
        with TemporaryDirectory() as directory:
            store = ScientificRegistry(Path(directory) / "science.sqlite3")
            p = store.register_protocol(protocol())
            with self.assertRaisesRegex(
                ProtocolViolation,
                "immutable artifact evidence",
            ):
                store.register_evaluation(
                    p.protocol_id,
                    holdout_id="holdout-early-stop",
                    result={
                        "stopping_rule_triggered": True,
                        "stopping_evidence_ref": "ticket-123",
                    },
                )

            canonical_ref = (
                "artifact:11111111-1111-4111-8111-111111111111@sha256:"
                + "a" * 64
            )
            row = store.register_evaluation(
                p.protocol_id,
                holdout_id="holdout-early-stop-valid",
                result={
                    "stopping_rule_triggered": True,
                    "stopping_evidence_ref": canonical_ref,
                },
            )
            locked = store.locked_evaluation(row["evaluation_id"])
            self.assertEqual(
                locked.result["stopping_evidence_ref"],
                canonical_ref,
            )

    def test_records_survive_reopen(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "science.sqlite3"
            first = ScientificRegistry(path)
            p = first.register_protocol(protocol())
            first.record_trial(p.protocol_id, status="CANCELLED", payload={"reason": "budget"})
            second = ScientificRegistry(path)
            self.assertEqual(second.completeness(p.protocol_id)["statuses"]["CANCELLED"], 1)


if __name__ == "__main__":
    unittest.main()
