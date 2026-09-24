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
        "horizons": [86400],
        "purge_embargo": {"purge_seconds": 86400, "embargo_seconds": 86400},
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

    def test_free_text_periods_are_rejected(self):
        with TemporaryDirectory() as directory:
            store = ScientificRegistry(Path(directory) / "science.sqlite3")
            value = protocol()
            value["train_period"] = "t0-t1"
            with self.assertRaisesRegex(ProtocolViolation, "train_period"):
                store.register_protocol(value)

    def test_overlapping_temporal_windows_are_rejected(self):
        with TemporaryDirectory() as directory:
            store = ScientificRegistry(Path(directory) / "science.sqlite3")
            value = protocol()
            value["validation_period"]["start"] = "2025-12-01T00:00:00Z"
            with self.assertRaisesRegex(ProtocolViolation, "overlaps"):
                store.register_protocol(value)

    def test_reversed_period_is_rejected(self):
        with TemporaryDirectory() as directory:
            store = ScientificRegistry(Path(directory) / "science.sqlite3")
            value = protocol()
            value["test_period"] = {
                "start": "2026-06-01T00:00:00Z",
                "end": "2026-05-01T00:00:00Z",
            }
            with self.assertRaisesRegex(ProtocolViolation, "must precede"):
                store.register_protocol(value)

    def test_naive_or_non_utc_period_is_rejected(self):
        with TemporaryDirectory() as directory:
            store = ScientificRegistry(Path(directory) / "science.sqlite3")
            value = protocol()
            value["forward_period"]["start"] = "2026-07-02T00:00:00"
            with self.assertRaisesRegex(ProtocolViolation, "UTC"):
                store.register_protocol(value)

    def test_purge_must_cover_longest_registered_label_horizon(self):
        with TemporaryDirectory() as directory:
            store = ScientificRegistry(Path(directory) / "science.sqlite3")
            value = protocol()
            value["horizons"] = [86400, 172800]
            value["purge_embargo"]["purge_seconds"] = 86400
            with self.assertRaisesRegex(ProtocolViolation, "longest"):
                store.register_protocol(value)

    def test_registered_purge_and_embargo_require_real_inter_period_gap(self):
        with TemporaryDirectory() as directory:
            store = ScientificRegistry(Path(directory) / "science.sqlite3")
            value = protocol()
            value["validation_period"]["start"] = "2026-01-01T00:00:00Z"
            with self.assertRaisesRegex(ProtocolViolation, "gap is shorter"):
                store.register_protocol(value)

    def test_embargo_must_cover_longest_registered_dependency_horizon(self):
        with TemporaryDirectory() as directory:
            store = ScientificRegistry(Path(directory) / "science.sqlite3")
            value = protocol()
            value["horizons"] = [86400, 172800]
            value["purge_embargo"]["purge_seconds"] = 172800
            value["purge_embargo"]["embargo_seconds"] = 86400
            with self.assertRaisesRegex(ProtocolViolation, "embargo_seconds"):
                store.register_protocol(value)

    def test_boolean_or_negative_temporal_controls_fail_closed(self):
        with TemporaryDirectory() as directory:
            store = ScientificRegistry(Path(directory) / "science.sqlite3")
            for invalid in (True, -1):
                with self.subTest(invalid=invalid):
                    value = protocol()
                    value["purge_embargo"]["embargo_seconds"] = invalid
                    with self.assertRaisesRegex(ProtocolViolation, "embargo_seconds"):
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
