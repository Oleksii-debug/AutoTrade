import copy
import os
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
        "train_period": {"start": "2024-01-01", "end": "2024-12-31"},
        "validation_period": {"start": "2025-01-01", "end": "2025-06-30"},
        "test_period": {"start": "2025-07-01", "end": "2025-12-31"},
        "forward_period": {"start": "2026-01-01", "end": "2026-06-30"},
        "labels": ["net_return"],
        "horizons": ["1d"],
        "purge_embargo": {"purge": "1d", "embargo": "1d"},
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


def holdout_identity(dataset_digit="a", *, start="2026-01-01", end="2026-06-30", role="LOCKED_FORWARD"):
    return {
        "dataset_digest": "sha256:" + dataset_digit * 64,
        "segment_start": start,
        "segment_end": end,
        "role": role,
    }



def _canonical_for_test(payload):
    import json
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _hash_for_test(payload):
    from hashlib import sha256
    return "sha256:" + sha256(_canonical_for_test(payload).encode("utf-8")).hexdigest()


class ScientificRegistryTests(unittest.TestCase):
    def test_relative_backing_path_is_frozen_at_construction(self):
        original_cwd = Path.cwd()
        with TemporaryDirectory() as directory:
            root = Path(directory)
            primary = root / "primary"
            alternate = root / "alternate"
            primary.mkdir()
            alternate.mkdir()
            try:
                os.chdir(primary)
                store = ScientificRegistry(Path("state") / "science.sqlite3")
                registered = store.register_protocol(protocol())
                frozen_path = store.path

                self.assertTrue(frozen_path.is_absolute())
                self.assertEqual(
                    frozen_path,
                    (primary / "state" / "science.sqlite3").resolve(strict=False),
                )

                os.chdir(alternate)
                reloaded = store.protocol_registration(registered.protocol_id)

                self.assertEqual(reloaded.protocol_hash, registered.protocol_hash)
                self.assertEqual(store.path, frozen_path)
                self.assertFalse((alternate / "state" / "science.sqlite3").exists())
            finally:
                os.chdir(original_cwd)

    def test_backing_path_cannot_be_reassigned_after_construction(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = ScientificRegistry(root / "first.sqlite3")
            original = store.path

            with self.assertRaises(AttributeError):
                store.path = root / "second.sqlite3"
            with self.assertRaises(AttributeError):
                store._path = root / "second.sqlite3"

            self.assertEqual(store.path, original)
            self.assertFalse((root / "second.sqlite3").exists())

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

    def test_protocol_periods_and_purge_embargo_are_machine_checked(self):
        with TemporaryDirectory() as directory:
            store = ScientificRegistry(Path(directory) / "science.sqlite3")

            overlapping = copy.deepcopy(protocol())
            overlapping["validation_period"]["start"] = "2024-12-31"
            with self.assertRaisesRegex(
                ProtocolViolation,
                "train_period must end before validation_period starts",
            ):
                store.register_protocol(overlapping)

            insufficient_gap = copy.deepcopy(protocol())
            insufficient_gap["purge_embargo"] = {
                "purge": "2d",
                "embargo": "2d",
            }
            with self.assertRaisesRegex(
                ProtocolViolation,
                "gap is shorter than the registered purge/embargo",
            ):
                store.register_protocol(insufficient_gap)

            uncovered_horizon = copy.deepcopy(protocol())
            uncovered_horizon["horizons"] = ["2d"]
            with self.assertRaisesRegex(
                ProtocolViolation,
                "purge must cover the longest registered label horizon",
            ):
                store.register_protocol(uncovered_horizon)

    def test_completeness_hash_binds_recorded_trial_outcomes(self):
        with TemporaryDirectory() as directory:
            store = ScientificRegistry(Path(directory) / "science.sqlite3")
            registered = store.register_protocol(protocol())
            before = store.completeness(registered.protocol_id)
            store.record_trial(
                registered.protocol_id,
                status="FAILED",
                payload={"reason": "fit"},
            )
            after = store.completeness(registered.protocol_id)
            self.assertNotEqual(before["trial_log_hash"], after["trial_log_hash"])
            self.assertEqual(after["recorded_trials"], 1)
            self.assertTrue(after["includes_non_successes"])

    def test_locked_evaluation_exposes_exact_immutable_hashes(self):
        with TemporaryDirectory() as directory:
            store = ScientificRegistry(Path(directory) / "science.sqlite3")
            registered = store.register_protocol(protocol())
            row = store.register_evaluation(
                registered.protocol_id,
                holdout_id="holdout-A",
                holdout_identity=holdout_identity(),
                result={"score": "0.1"},
            )
            evidence = store.locked_evaluation(row["evaluation_id"])
            self.assertEqual(evidence.protocol_id, registered.protocol_id)
            self.assertEqual(evidence.protocol_hash, registered.protocol_hash)
            self.assertEqual(evidence.result_hash, row["result_hash"])
            self.assertTrue(evidence.untouched)
            self.assertEqual(evidence.prior_access_count, 0)
            self.assertEqual(evidence.result, {"score": "0.1"})

    def test_triggered_stopping_rule_requires_immutable_artifact_evidence(self):
        with TemporaryDirectory() as directory:
            store = ScientificRegistry(Path(directory) / "science.sqlite3")
            registered = store.register_protocol(protocol())
            with self.assertRaisesRegex(
                ProtocolViolation,
                "immutable artifact evidence",
            ):
                store.register_evaluation(
                    registered.protocol_id,
                    holdout_id="holdout-invalid-stop",
                    holdout_identity=holdout_identity(),
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
                registered.protocol_id,
                holdout_id="holdout-valid-stop",
                holdout_identity=holdout_identity(),
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
            first = store.register_evaluation(p.protocol_id, holdout_id="holdout-A", holdout_identity=holdout_identity(), result={"score": "0.1"})
            self.assertEqual(first["untouched"], 1)
            second = store.register_evaluation(p.protocol_id, holdout_id="holdout-A", holdout_identity=holdout_identity(), result={"score": "0.2"})
            self.assertEqual(second["untouched"], 0)
            self.assertGreaterEqual(second["prior_access_count"], 1)

    def test_manual_holdout_peek_contaminates_locked_evaluation(self):
        with TemporaryDirectory() as directory:
            store = ScientificRegistry(Path(directory) / "science.sqlite3")
            p = store.register_protocol(protocol())
            store.record_holdout_access(p.protocol_id, holdout_id="holdout-A", holdout_identity=holdout_identity(), purpose="manual inspection")
            result = store.register_evaluation(p.protocol_id, holdout_id="holdout-A", holdout_identity=holdout_identity(), result={"score": "0.1"})
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


    def test_trial_completeness_evidence_binds_protocol_and_population(self):
        with TemporaryDirectory() as directory:
            store = ScientificRegistry(Path(directory) / "science.sqlite3")
            registered = store.register_protocol(protocol())

            before = store.trial_completeness_evidence(registered.protocol_id)
            self.assertEqual(before.protocol_id, registered.protocol_id)
            self.assertEqual(before.protocol_hash, registered.protocol_hash)
            self.assertEqual(before.trial_budget, 3)
            self.assertEqual(before.recorded_trials, 0)
            self.assertEqual(before.remaining_trial_budget, 3)
            self.assertFalse(before.complete)
            self.assertEqual(before.statuses, ())
            self.assertTrue(before.digest.startswith("sha256:"))

            store.record_trial(
                registered.protocol_id,
                status="FAILED",
                payload={"reason": "fit"},
            )
            after = store.trial_completeness_evidence(registered.protocol_id)
            self.assertEqual(after.protocol_hash, registered.protocol_hash)
            self.assertEqual(after.recorded_trials, 1)
            self.assertEqual(after.remaining_trial_budget, 2)
            self.assertEqual(after.statuses, (("FAILED", 1),))
            self.assertTrue(after.includes_non_successes)
            self.assertNotEqual(after.trial_log_hash, before.trial_log_hash)
            self.assertNotEqual(after.digest, before.digest)

    def test_trial_completeness_evidence_complete_only_at_full_budget(self):
        with TemporaryDirectory() as directory:
            store = ScientificRegistry(Path(directory) / "science.sqlite3")
            value = protocol()
            value["trial_budget"] = 2
            registered = store.register_protocol(value)
            store.record_trial(
                registered.protocol_id,
                status="COMPLETED",
                payload={"trial": 1},
            )
            self.assertFalse(
                store.trial_completeness_evidence(registered.protocol_id).complete
            )
            store.record_trial(
                registered.protocol_id,
                status="DISCARDED",
                payload={"trial": 2},
            )
            evidence = store.trial_completeness_evidence(registered.protocol_id)
            self.assertTrue(evidence.complete)
            self.assertEqual(evidence.remaining_trial_budget, 0)
            self.assertEqual(
                evidence.statuses,
                (("COMPLETED", 1), ("DISCARDED", 1)),
            )

    def test_trial_completeness_evidence_rejects_corrupt_status(self):
        with TemporaryDirectory() as directory:
            store = ScientificRegistry(Path(directory) / "science.sqlite3")
            registered = store.register_protocol(protocol())
            trial_id = store.record_trial(
                registered.protocol_id,
                status="COMPLETED",
                payload={"trial": 1},
            )
            with store._connect() as con:
                # Exercise corrupted storage below the append-only API.
                # Normal UPDATE remains forbidden by this trigger.
                con.execute("DROP TRIGGER trials_no_update")
                con.execute(
                    "UPDATE trials SET status=? WHERE trial_id=?",
                    ("FORGED", trial_id),
                )
            with self.assertRaisesRegex(
                ProtocolViolation,
                "registered trial status is corrupt",
            ):
                store.trial_completeness_evidence(registered.protocol_id)

    def test_trial_completeness_evidence_rejects_population_above_budget(self):
        with TemporaryDirectory() as directory:
            store = ScientificRegistry(Path(directory) / "science.sqlite3")
            value = protocol()
            value["trial_budget"] = 1
            registered = store.register_protocol(value)
            store.record_trial(
                registered.protocol_id,
                status="COMPLETED",
                payload={"trial": 1},
                trial_id="11111111-1111-4111-8111-111111111111",
            )
            # Simulate storage corruption/non-cooperating mutation rather than
            # using record_trial(), which correctly rejects budget overflow.
            payload = {"trial": 2}
            with store._connect() as con:
                con.execute(
                    """
                    INSERT INTO trials(
                        trial_id,protocol_id,status,payload_hash,payload_json,created_at
                    ) VALUES(?,?,?,?,?,?)
                    """,
                    (
                        "22222222-2222-4222-8222-222222222222",
                        registered.protocol_id,
                        "COMPLETED",
                        _hash_for_test(payload),
                        _canonical_for_test(payload),
                        "2026-10-04T00:00:00+00:00",
                    ),
                )
            with self.assertRaisesRegex(
                ProtocolViolation,
                "population exceeds immutable trial budget",
            ):
                store.trial_completeness_evidence(registered.protocol_id)


if __name__ == "__main__":
    unittest.main()
