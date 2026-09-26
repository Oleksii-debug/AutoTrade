import copy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from research.autotrade_research.artifacts.store import ArtifactStore
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


def exhaust_trials(store, protocol_id):
    state = store.completeness(protocol_id)
    for index in range(state["remaining_trial_budget"]):
        store.record_trial(
            protocol_id,
            status="COMPLETED",
            payload={"trial_index": index, "result": "registered"},
        )


def publish_stopping_evidence(
    artifact_store,
    *,
    protocol_id,
    stopping_rules_hash,
    artifact_id="11111111-1111-4111-8111-111111111111",
    data=b"registered stopping-rule evidence",
    metadata_overrides=None,
):
    metadata = {
        "kind": "stopping-rule-evidence",
        "protocol_id": protocol_id,
        "stopping_rules_hash": stopping_rules_hash,
    }
    metadata.update(metadata_overrides or {})
    manifest = artifact_store.publish_bytes(
        artifact_id=artifact_id,
        data=data,
        media_type="application/json",
        rights={"storage": True, "export": False},
        source_refs=["science:registered-stopping-rule"],
        metadata=metadata,
    )
    return "artifact:" + artifact_id + "@" + manifest["sha256"]


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
            exhaust_trials(store, registered.protocol_id)
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

    def test_premature_locked_evaluation_does_not_burn_holdout(self):
        with TemporaryDirectory() as directory:
            store = ScientificRegistry(Path(directory) / "science.sqlite3")
            registered = store.register_protocol(protocol())

            with self.assertRaisesRegex(
                ProtocolViolation,
                "trial budget is exhausted",
            ):
                store.register_evaluation(
                    registered.protocol_id,
                    holdout_id="holdout-premature",
                    holdout_identity=holdout_identity(),
                    result={"score": "looks-good"},
                )

            self.assertEqual(
                store.holdout_access_count(
                    registered.protocol_id,
                    "holdout-premature",
                ),
                0,
            )
            self.assertEqual(
                store.completeness(registered.protocol_id)["recorded_trials"],
                0,
            )

    def test_early_stop_must_bind_exact_registered_stopping_rules(self):
        with TemporaryDirectory() as directory:
            store = ScientificRegistry(Path(directory) / "science.sqlite3")
            registered = store.register_protocol(protocol())
            canonical_ref = (
                "artifact:22222222-2222-4222-8222-222222222222@sha256:"
                + "b" * 64
            )
            with self.assertRaisesRegex(
                ProtocolViolation,
                "registered stopping rules",
            ):
                store.register_evaluation(
                    registered.protocol_id,
                    holdout_id="holdout-wrong-stop",
                    holdout_identity=holdout_identity(),
                    result={
                        "stopping_rule_triggered": True,
                        "stopping_rules_hash": "sha256:" + "f" * 64,
                        "stopping_evidence_ref": canonical_ref,
                    },
                )
            self.assertEqual(
                store.holdout_access_count(
                    registered.protocol_id,
                    "holdout-wrong-stop",
                ),
                0,
            )

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

            artifact_store = ArtifactStore(Path(directory) / "artifacts")
            store = ScientificRegistry(
                Path(directory) / "science.sqlite3",
                artifact_store=artifact_store,
            )
            registered = store.register_protocol(protocol())
            stopping_rules_hash = store.completeness(
                registered.protocol_id
            )["stopping_rules_hash"]
            canonical_ref = publish_stopping_evidence(
                artifact_store,
                protocol_id=registered.protocol_id,
                stopping_rules_hash=stopping_rules_hash,
            )
            row = store.register_evaluation(
                registered.protocol_id,
                holdout_id="holdout-valid-stop",
                holdout_identity=holdout_identity(),
                result={
                    "stopping_rule_triggered": True,
                    "stopping_rules_hash": stopping_rules_hash,
                    "stopping_evidence_ref": canonical_ref,
                },
            )
            locked = store.locked_evaluation(row["evaluation_id"])
            self.assertEqual(
                locked.result["stopping_evidence_ref"],
                canonical_ref,
            )

    def test_early_stop_requires_resolvable_stopping_evidence(self):
        with TemporaryDirectory() as directory:
            artifact_store = ArtifactStore(Path(directory) / "artifacts")
            store = ScientificRegistry(
                Path(directory) / "science.sqlite3",
                artifact_store=artifact_store,
            )
            registered = store.register_protocol(protocol())
            stopping_rules_hash = store.completeness(
                registered.protocol_id
            )["stopping_rules_hash"]
            missing_ref = (
                "artifact:33333333-3333-4333-8333-333333333333@sha256:"
                + "c" * 64
            )
            with self.assertRaisesRegex(
                ProtocolViolation,
                "cannot be verified",
            ):
                store.register_evaluation(
                    registered.protocol_id,
                    holdout_id="holdout-missing-stop",
                    holdout_identity=holdout_identity(),
                    result={
                        "stopping_rule_triggered": True,
                        "stopping_rules_hash": stopping_rules_hash,
                        "stopping_evidence_ref": missing_ref,
                    },
                )
            self.assertEqual(
                store.holdout_access_count(
                    registered.protocol_id,
                    "holdout-missing-stop",
                ),
                0,
            )

    def test_early_stop_rejects_stopping_evidence_digest_mismatch(self):
        with TemporaryDirectory() as directory:
            artifact_store = ArtifactStore(Path(directory) / "artifacts")
            store = ScientificRegistry(
                Path(directory) / "science.sqlite3",
                artifact_store=artifact_store,
            )
            registered = store.register_protocol(protocol())
            stopping_rules_hash = store.completeness(
                registered.protocol_id
            )["stopping_rules_hash"]
            canonical_ref = publish_stopping_evidence(
                artifact_store,
                protocol_id=registered.protocol_id,
                stopping_rules_hash=stopping_rules_hash,
                artifact_id="44444444-4444-4444-8444-444444444444",
            )
            wrong_ref = canonical_ref.rsplit(":", 1)[0] + ":" + "d" * 64
            with self.assertRaisesRegex(
                ProtocolViolation,
                "digest mismatch",
            ):
                store.register_evaluation(
                    registered.protocol_id,
                    holdout_id="holdout-wrong-digest",
                    holdout_identity=holdout_identity(),
                    result={
                        "stopping_rule_triggered": True,
                        "stopping_rules_hash": stopping_rules_hash,
                        "stopping_evidence_ref": wrong_ref,
                    },
                )
            self.assertEqual(
                store.holdout_access_count(
                    registered.protocol_id,
                    "holdout-wrong-digest",
                ),
                0,
            )

    def test_early_stop_evidence_must_bind_protocol_and_stopping_rules(self):
        with TemporaryDirectory() as directory:
            artifact_store = ArtifactStore(Path(directory) / "artifacts")
            store = ScientificRegistry(
                Path(directory) / "science.sqlite3",
                artifact_store=artifact_store,
            )
            registered = store.register_protocol(protocol())
            stopping_rules_hash = store.completeness(
                registered.protocol_id
            )["stopping_rules_hash"]
            ref = publish_stopping_evidence(
                artifact_store,
                protocol_id="00000000-0000-4000-8000-000000000099",
                stopping_rules_hash=stopping_rules_hash,
                artifact_id="55555555-5555-4555-8555-555555555555",
            )
            with self.assertRaisesRegex(
                ProtocolViolation,
                "not bound to the registered protocol",
            ):
                store.register_evaluation(
                    registered.protocol_id,
                    holdout_id="holdout-wrong-binding",
                    holdout_identity=holdout_identity(),
                    result={
                        "stopping_rule_triggered": True,
                        "stopping_rules_hash": stopping_rules_hash,
                        "stopping_evidence_ref": ref,
                    },
                )
            self.assertEqual(
                store.holdout_access_count(
                    registered.protocol_id,
                    "holdout-wrong-binding",
                ),
                0,
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
            exhaust_trials(store, p.protocol_id)
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
            exhaust_trials(store, p.protocol_id)
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


if __name__ == "__main__":
    unittest.main()
