import copy
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import UUID

from research.autotrade_research.data.vintages import HistoricalVintageRegistry
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


def _token_uuid(token):
    return str(UUID(hex=sha256(("uuid:" + token).encode("utf-8")).hexdigest()[:32]))


def _vintage_manifest(token="a"):
    return {
        "dataset_id": _token_uuid("dataset:" + token),
        "version": "1",
        "content_hashes": [
            "sha256:" + sha256(("content:" + token).encode("utf-8")).hexdigest()
        ],
        "instrument_universe_version": "universe:test-v1",
        "calendar_version": "calendar:test-v1",
        "coverage": {
            "from": "2026-01-01T00:00:00Z",
            "to": "2026-06-30T23:59:59Z",
        },
        "availability_policy": {
            "point_in_time": True,
            "no_future_leakage": True,
            "cutoff": "2026-06-30T23:59:59Z",
            "basis": "test-fixture-evidence",
        },
        "revision_policy": {
            "append_only": True,
            "replace_prior_vintages": False,
        },
        "normalization_version": "normalization:test-v1",
        "adjustment_policy": {
            "raw_retained": True,
            "adjusted_available": False,
            "method": "none",
        },
        "rights": {
            "storage": True,
            "research_use": True,
            "redistribution": False,
            "basis": "first-party-test-fixture",
        },
        "missingness_report": {
            "expected_count": 1,
            "observed_count": 1,
            "missing_keys": [],
            "invented_count": 0,
        },
        "source_evidence": [
            {
                "artifact_id": _token_uuid("evidence:" + token),
                "sha256": "sha256:"
                + sha256(("evidence:" + token).encode("utf-8")).hexdigest(),
                "observed_at": "2026-06-30T23:59:59Z",
            }
        ],
        "created_at": "2026-07-01T00:00:00Z",
    }


def _manifest_digest(token="a"):
    raw = json.dumps(
        _vintage_manifest(token),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + sha256(raw).hexdigest()


def holdout_identity(dataset_digit="a", *, start="2026-01-01", end="2026-06-30", role="LOCKED_FORWARD"):
    return {
        "dataset_digest": _manifest_digest(dataset_digit),
        "segment_start": start,
        "segment_end": end,
        "role": role,
    }


def preregister_holdout(store, protocol_id, *, dataset_digit="a"):
    registry = HistoricalVintageRegistry(store.path.parent / "historical-vintages")
    manifest = _vintage_manifest(dataset_digit)
    committed_digest = registry.commit(manifest)
    if committed_digest != _manifest_digest(dataset_digit):
        raise AssertionError("test vintage digest drifted from canonical manifest")
    return store.preregister_locked_holdout(
        protocol_id,
        vintage_registry=registry,
        dataset_id=manifest["dataset_id"],
        dataset_version=1,
    )


def exhaust_trials(store, protocol_id):
    try:
        store.locked_holdout_registration(protocol_id)
    except ProtocolViolation as error:
        if "lacks preregistered physical locked holdout" not in str(error):
            raise
        preregister_holdout(store, protocol_id)
    state = store.completeness(protocol_id)
    for index in range(state["remaining_trial_budget"]):
        store.record_trial(
            protocol_id,
            status="COMPLETED",
            payload={"trial_index": index, "result": "registered"},
        )



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
            preregister_holdout(store, registered.protocol_id)
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
            preregister_holdout(store, registered.protocol_id)

            with self.assertRaisesRegex(
                ProtocolViolation,
                "exact registered trial budget",
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

            # The rejected attempt must not consume or contaminate the exact
            # physical holdout that was frozen before trial 1.
            exhaust_trials(store, registered.protocol_id)
            admitted = store.register_evaluation(
                registered.protocol_id,
                holdout_id="holdout-premature",
                holdout_identity=holdout_identity(),
                result={"score": "0.1"},
            )
            self.assertEqual(admitted["prior_access_count"], 0)
            self.assertEqual(admitted["untouched"], 1)

    def test_all_caller_asserted_early_stop_forms_fail_before_holdout_access(self):
        with TemporaryDirectory() as directory:
            store = ScientificRegistry(Path(directory) / "science.sqlite3")
            registered = store.register_protocol(protocol())
            preregister_holdout(store, registered.protocol_id)
            rules_hash = store.completeness(
                registered.protocol_id
            )["stopping_rules_hash"]
            canonical_ref = (
                "artifact:11111111-1111-4111-8111-111111111111@sha256:"
                + "a" * 64
            )

            variants = (
                {"score": "0.1"},
                {"stopping_rule_triggered": True},
                {
                    "stopping_rule_triggered": True,
                    "stopping_rules_hash": rules_hash,
                    "stopping_evidence_ref": canonical_ref,
                },
                {
                    "stopping_rule_triggered": True,
                    "stopping_rules_hash": "sha256:" + "f" * 64,
                    "stopping_evidence_ref": canonical_ref,
                },
            )
            for index, result in enumerate(variants):
                holdout_id = f"early-stop-disabled-{index}"
                with self.subTest(result=result):
                    with self.assertRaisesRegex(
                        ProtocolViolation,
                        "early stopping and oversized trial populations are not",
                    ):
                        store.register_evaluation(
                            registered.protocol_id,
                            holdout_id=holdout_id,
                            holdout_identity=holdout_identity(),
                            result=result,
                        )
                    self.assertEqual(
                        store.holdout_access_count(
                            registered.protocol_id,
                            holdout_id,
                        ),
                        0,
                    )
                    self.assertEqual(
                        store.completeness(
                            registered.protocol_id
                        )["recorded_trials"],
                        0,
                    )

    def test_oversized_trial_population_cannot_unlock_holdout(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "science.sqlite3"
            store = ScientificRegistry(path)
            value = protocol()
            value["trial_budget"] = 1
            registered = store.register_protocol(value)
            preregister_holdout(store, registered.protocol_id)
            store.record_trial(
                registered.protocol_id,
                status="COMPLETED",
                payload={"trial_index": 0, "result": "registered"},
                trial_id="11111111-1111-4111-8111-111111111111",
            )

            extra_payload = {"trial_index": 1, "result": "legacy-extra"}
            extra_json = json.dumps(
                extra_payload,
                sort_keys=True,
                separators=(",", ":"),
            )
            extra_hash = "sha256:" + sha256(extra_json.encode("utf-8")).hexdigest()
            import sqlite3
            with sqlite3.connect(path) as connection:
                connection.execute(
                    "INSERT INTO trials("
                    "trial_id,protocol_id,status,payload_hash,payload_json,created_at"
                    ") VALUES(?,?,?,?,?,?)",
                    (
                        "22222222-2222-4222-8222-222222222222",
                        registered.protocol_id,
                        "COMPLETED",
                        extra_hash,
                        extra_json,
                        "2026-09-28T12:00:00Z",
                    ),
                )
                connection.commit()

            with self.assertRaisesRegex(
                ProtocolViolation,
                "exceeds preregistered trial budget",
            ):
                store.completeness(registered.protocol_id)

            with self.assertRaisesRegex(
                ProtocolViolation,
                "exact registered trial budget",
            ):
                store.register_evaluation(
                    registered.protocol_id,
                    holdout_id="holdout-oversized-trials",
                    holdout_identity=holdout_identity(),
                    result={"score": "0.1"},
                )
            self.assertEqual(
                store.holdout_access_count(
                    registered.protocol_id,
                    "holdout-oversized-trials",
                ),
                0,
            )

    def test_corrupt_trial_population_cannot_unlock_holdout(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "science.sqlite3"
            store = ScientificRegistry(path)
            value = protocol()
            value["trial_budget"] = 1
            registered = store.register_protocol(value)
            preregister_holdout(store, registered.protocol_id)
            store.record_trial(
                registered.protocol_id,
                status="COMPLETED",
                payload={"trial_index": 0, "result": "registered"},
                trial_id="11111111-1111-4111-8111-111111111111",
            )

            import sqlite3
            with sqlite3.connect(path) as connection:
                connection.execute("DROP TRIGGER trials_no_update")
                connection.execute(
                    "UPDATE trials SET payload_json=? WHERE trial_id=?",
                    (
                        '{"result":"tampered","trial_index":0}',
                        "11111111-1111-4111-8111-111111111111",
                    ),
                )
                connection.commit()

            with self.assertRaisesRegex(
                ProtocolViolation,
                "trial population integrity mismatch",
            ):
                store.register_evaluation(
                    registered.protocol_id,
                    holdout_id="holdout-corrupt-trials",
                    holdout_identity=holdout_identity(),
                    result={"score": "0.1"},
                )

            self.assertEqual(
                store.holdout_access_count(
                    registered.protocol_id,
                    "holdout-corrupt-trials",
                ),
                0,
            )

    def test_nonterminal_legacy_trial_cannot_count_toward_completeness(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "science.sqlite3"
            store = ScientificRegistry(path)
            value = protocol()
            value["trial_budget"] = 1
            registered = store.register_protocol(value)
            preregister_holdout(store, registered.protocol_id)
            store.record_trial(
                registered.protocol_id,
                status="COMPLETED",
                payload={"trial_index": 0, "result": "registered"},
                trial_id="11111111-1111-4111-8111-111111111111",
            )

            import sqlite3
            with sqlite3.connect(path) as connection:
                connection.execute("DROP TRIGGER trials_no_update")
                cursor = connection.execute(
                    "UPDATE trials SET status=? WHERE trial_id=?",
                    (
                        "RUNNING",
                        "11111111-1111-4111-8111-111111111111",
                    ),
                )
                self.assertEqual(cursor.rowcount, 1)
                connection.commit()

            with self.assertRaisesRegex(
                ProtocolViolation,
                "trial population integrity mismatch",
            ):
                store.completeness(registered.protocol_id)

    def test_full_trial_closure_allows_locked_evaluation_without_early_stop_authority(self):
        with TemporaryDirectory() as directory:
            store = ScientificRegistry(Path(directory) / "science.sqlite3")
            registered = store.register_protocol(protocol())
            exhaust_trials(store, registered.protocol_id)
            row = store.register_evaluation(
                registered.protocol_id,
                holdout_id="closed-budget-holdout",
                holdout_identity=holdout_identity(),
                result={"score": "0.1"},
            )
            self.assertEqual(row["prior_access_count"], 0)
            self.assertEqual(row["untouched"], 1)
            self.assertEqual(
                store.holdout_access_count(
                    registered.protocol_id,
                    "closed-budget-holdout",
                ),
                1,
            )

    def test_failed_and_discarded_trials_are_preserved(self):
        with TemporaryDirectory() as directory:
            store = ScientificRegistry(Path(directory) / "science.sqlite3")
            p = store.register_protocol(protocol())
            preregister_holdout(store, p.protocol_id)
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
            preregister_holdout(store, p.protocol_id)
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
            preregister_holdout(first, p.protocol_id)
            first.record_trial(p.protocol_id, status="CANCELLED", payload={"reason": "budget"})
            second = ScientificRegistry(path)
            self.assertEqual(second.completeness(p.protocol_id)["statuses"]["CANCELLED"], 1)


if __name__ == "__main__":
    unittest.main()
