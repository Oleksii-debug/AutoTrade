from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import NAMESPACE_URL, uuid5

from research.autotrade_research.data.vintages import HistoricalVintageRegistry
from research.autotrade_research.science.registry import (
    ProtocolViolation,
    ScientificRegistry,
)


BASE = datetime(2026, 10, 1, tzinfo=timezone.utc)


def digest(value: str) -> str:
    return "sha256:" + sha256(value.encode("utf-8")).hexdigest()


def object_digest(value: object) -> str:
    raw = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + sha256(raw).hexdigest()


def protocol(*, trial_budget: int = 2) -> dict:
    return {
        "hypothesis": "locked holdout is frozen before outcome search",
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
        "trial_budget": trial_budget,
        "stopping_rules": "budget only; no canonical early-stop adjudicator",
        "statistical_estimator": "dependence-aware",
        "multiplicity_treatment": "registered correction",
        "minimum_practical_effect": "0.001",
        "risk_constraints": {"max_drawdown": "0.10"},
        "retention_tolerances": {"prior_regime_loss": "0.02"},
        "promotion_rule": "all registered gates",
    }


def vintage_manifest(seed: str = "a") -> dict:
    return {
        "dataset_id": str(uuid5(NAMESPACE_URL, f"wp35-dataset:{seed}")),
        "version": "1",
        "content_hashes": [digest(f"wp35-content:{seed}")],
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
                "artifact_id": str(uuid5(NAMESPACE_URL, f"wp35-evidence:{seed}")),
                "sha256": digest(f"wp35-evidence:{seed}"),
                "observed_at": "2026-06-30T23:59:59Z",
            }
        ],
        "created_at": "2026-07-01T00:00:00Z",
    }


def manifest_digest(seed: str = "a") -> str:
    raw = json.dumps(
        vintage_manifest(seed),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + sha256(raw).hexdigest()


def preregister(store: ScientificRegistry, protocol_id: str, *, seed: str = "a"):
    vintages = HistoricalVintageRegistry(store.path.parent / f"vintages-{seed}")
    manifest = vintage_manifest(seed)
    committed = vintages.commit(manifest)
    if committed != manifest_digest(seed):
        raise AssertionError("fixture manifest digest differs from registry digest")
    return store.preregister_locked_holdout(
        protocol_id,
        vintage_registry=vintages,
        dataset_id=manifest["dataset_id"],
        dataset_version=1,
    )


def promotion_result(
    store: ScientificRegistry,
    protocol_id: str,
    *,
    candidate: str,
) -> dict:
    state = store.completeness(protocol_id)
    return {
        "candidate_id": candidate,
        "artifact_hash": digest(candidate),
        "evaluation_status": "PASS",
        "retention_passed": True,
        "risk_passed": True,
        "authority_scope_id": "paper-scope",
        "evidence_valid_until": (BASE + timedelta(days=1)).isoformat(),
        "recorded_trial_count": state["recorded_trials"],
        "trial_budget": state["trial_budget"],
        "trial_log_hash": state["trial_log_hash"],
        "reproducible": True,
        "causal_audit_passed": True,
        "financial_invariants_passed": True,
        "trial_log_complete": True,
    }


class LockedHoldoutPreregistrationTests(unittest.TestCase):
    def test_preregistered_wp10_vintage_round_trips_as_protocol_authority(self):
        with TemporaryDirectory() as directory:
            store = ScientificRegistry(Path(directory) / "science.sqlite3")
            registered = store.register_protocol(protocol())
            holdout = preregister(store, registered.protocol_id)

            reloaded = store.locked_holdout_registration(registered.protocol_id)

            self.assertEqual(reloaded, holdout)
            self.assertEqual(reloaded.dataset_digest, manifest_digest())
            self.assertEqual(reloaded.role, "LOCKED_FORWARD")
            self.assertEqual(reloaded.segment_start, "2026-01-01")
            self.assertEqual(reloaded.segment_end, "2026-06-30")

    def test_preregistration_rejects_rebound_vintage_load_before_dispatch(self):
        with TemporaryDirectory() as directory:
            store = ScientificRegistry(Path(directory) / "science.sqlite3")
            registered = store.register_protocol(protocol())
            vintages = HistoricalVintageRegistry(Path(directory) / "vintages")
            manifest = vintage_manifest("class-load")
            vintages.commit(manifest)
            hostile_called = False

            def hostile_load(*_args, **_kwargs):
                nonlocal hostile_called
                hostile_called = True
                raise AssertionError("hostile class load executed")

            with patch.object(HistoricalVintageRegistry, "load", hostile_load):
                with self.assertRaisesRegex(
                    TypeError,
                    "class authority descriptor changed: load",
                ):
                    store.preregister_locked_holdout(
                        registered.protocol_id,
                        vintage_registry=vintages,
                        dataset_id=manifest["dataset_id"],
                        dataset_version=1,
                    )
            self.assertFalse(hostile_called)

    def test_preregistration_rejects_rebound_vintage_path_before_dispatch(self):
        with TemporaryDirectory() as directory:
            store = ScientificRegistry(Path(directory) / "science.sqlite3")
            registered = store.register_protocol(protocol())
            vintages = HistoricalVintageRegistry(Path(directory) / "vintages")
            manifest = vintage_manifest("class-path")
            vintages.commit(manifest)
            hostile_called = False

            def hostile_path(*_args, **_kwargs):
                nonlocal hostile_called
                hostile_called = True
                raise AssertionError("hostile class path executed")

            with patch.object(HistoricalVintageRegistry, "_path", hostile_path):
                with self.assertRaisesRegex(
                    TypeError,
                    "class authority descriptor changed: _path",
                ):
                    store.preregister_locked_holdout(
                        registered.protocol_id,
                        vintage_registry=vintages,
                        dataset_id=manifest["dataset_id"],
                        dataset_version=1,
                    )
            self.assertFalse(hostile_called)

    def test_preregistration_rejects_registry_class_rebinding(self):
        with TemporaryDirectory() as directory:
            store = ScientificRegistry(Path(directory) / "science.sqlite3")
            registered = store.register_protocol(protocol())
            vintages = HistoricalVintageRegistry(Path(directory) / "vintages")
            manifest = vintage_manifest("class-global")
            vintages.commit(manifest)
            registry_module = __import__(
                "research.autotrade_research.science.registry",
                fromlist=["HistoricalVintageRegistry"],
            )
            with patch.object(
                registry_module,
                "HistoricalVintageRegistry",
                object,
            ):
                with self.assertRaisesRegex(
                    TypeError,
                    "HistoricalVintageRegistry class authority changed",
                ):
                    store.preregister_locked_holdout(
                        registered.protocol_id,
                        vintage_registry=vintages,
                        dataset_id=manifest["dataset_id"],
                        dataset_version=1,
                    )

    def test_trial_search_cannot_start_before_physical_holdout_preregistration(self):
        with TemporaryDirectory() as directory:
            store = ScientificRegistry(Path(directory) / "science.sqlite3")
            registered = store.register_protocol(protocol())

            with self.assertRaisesRegex(
                ProtocolViolation,
                "lacks preregistered physical locked holdout",
            ):
                store.record_trial(
                    registered.protocol_id,
                    status="COMPLETED",
                    payload={
                        "candidate_id": "candidate-a",
                        "artifact_hash": digest("candidate-a"),
                    },
                )
            self.assertEqual(store.completeness(registered.protocol_id)["recorded_trials"], 0)

            locked = preregister(store, registered.protocol_id)
            store.record_trial(
                registered.protocol_id,
                status="COMPLETED",
                payload={
                    "candidate_id": "candidate-a",
                    "artifact_hash": digest("candidate-a"),
                },
            )
            self.assertEqual(
                store.locked_holdout_registration(registered.protocol_id),
                locked,
            )
            self.assertEqual(store.completeness(registered.protocol_id)["recorded_trials"], 1)

    def test_protocol_scoped_holdout_access_requires_frozen_physical_identity(self):
        with TemporaryDirectory() as directory:
            store = ScientificRegistry(Path(directory) / "science.sqlite3")
            registered = store.register_protocol(protocol())
            candidate_identity = {
                "dataset_digest": manifest_digest("a"),
                "segment_start": "2026-01-01",
                "segment_end": "2026-06-30",
                "role": "LOCKED_FORWARD",
            }

            with self.assertRaisesRegex(
                ProtocolViolation,
                "lacks preregistered physical locked holdout",
            ):
                store.record_holdout_access(
                    registered.protocol_id,
                    holdout_id="pre-freeze-peek",
                    holdout_identity=candidate_identity,
                    purpose="manual inspection before preregistration",
                )

            locked = preregister(store, registered.protocol_id)
            with self.assertRaisesRegex(
                ProtocolViolation,
                "must match preregistered physical holdout",
            ):
                store.record_holdout_access(
                    registered.protocol_id,
                    holdout_id="wrong-physical-source",
                    holdout_identity={
                        **candidate_identity,
                        "dataset_digest": manifest_digest("b"),
                    },
                    purpose="wrong source",
                )

            store.record_holdout_access(
                registered.protocol_id,
                holdout_id="frozen-source",
                holdout_identity=locked.identity(),
                purpose="explicit contamination test",
            )
            self.assertEqual(
                store.holdout_access_count(
                    registered.protocol_id,
                    "frozen-source",
                ),
                1,
            )

    def test_preregistered_holdout_rejects_fresh_dataset_selected_after_trials(self):
        with TemporaryDirectory() as directory:
            store = ScientificRegistry(Path(directory) / "science.sqlite3")
            registered = store.register_protocol(protocol(trial_budget=1))
            locked = preregister(store, registered.protocol_id, seed="a")
            store.record_trial(
                registered.protocol_id,
                status="COMPLETED",
                payload={"candidate_id": "candidate-a", "artifact_hash": digest("candidate-a")},
            )

            fresh_identity = {
                "dataset_digest": manifest_digest("b"),
                "segment_start": locked.segment_start,
                "segment_end": locked.segment_end,
                "role": locked.role,
            }
            with self.assertRaisesRegex(
                ProtocolViolation,
                "must match preregistered physical holdout",
            ):
                store.register_evaluation(
                    registered.protocol_id,
                    holdout_id="fresh-B",
                    holdout_identity=fresh_identity,
                    result={"score": "0.99"},
                )

            accepted = store.register_evaluation(
                registered.protocol_id,
                holdout_id="frozen-A",
                holdout_identity=locked.identity(),
                result={"score": "0.10"},
            )
            self.assertEqual(accepted["untouched"], 1)

    def test_legacy_unbound_evaluation_row_remains_permanently_non_promoting(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "science.sqlite3"
            store = ScientificRegistry(path)
            registered = store.register_protocol(protocol(trial_budget=1))
            candidate = "candidate-legacy"
            result = promotion_result(store, registered.protocol_id, candidate=candidate)
            identity = {
                "dataset_digest": digest("legacy-caller-selected"),
                "segment_start": "2026-01-01",
                "segment_end": "2026-06-30",
                "role": "LOCKED_FORWARD",
            }
            identity_json = json.dumps(
                identity,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            )
            identity_hash = object_digest(identity)
            result_json = json.dumps(
                result,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            )
            result_hash = object_digest(result)
            evaluation_id = str(uuid5(NAMESPACE_URL, "legacy-unbound-evaluation"))
            with sqlite3.connect(path) as connection:
                connection.execute(
                    "INSERT INTO holdouts("
                    "holdout_identity_hash,identity_json,created_at"
                    ") VALUES(?,?,?)",
                    (
                        identity_hash,
                        identity_json,
                        "2026-10-01T00:00:00+00:00",
                    ),
                )
                connection.execute(
                    "INSERT INTO evaluations("
                    "evaluation_id,protocol_id,holdout_id,protocol_hash,"
                    "result_hash,result_json,prior_access_count,untouched,"
                    "created_at,holdout_identity_hash"
                    ") VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        evaluation_id,
                        registered.protocol_id,
                        "legacy-late-bound",
                        registered.protocol_hash,
                        result_hash,
                        result_json,
                        0,
                        1,
                        "2026-10-01T00:00:00+00:00",
                        identity_hash,
                    ),
                )
                connection.commit()

            with self.assertRaisesRegex(
                ProtocolViolation,
                "lacks preregistered physical locked holdout",
            ):
                store.verify_candidate_promotion_evidence(
                    evaluation_id=evaluation_id,
                    protocol_id=registered.protocol_id,
                    protocol_hash=registered.protocol_hash,
                    result_hash=result_hash,
                    candidate_id=candidate,
                    artifact_hash=digest(candidate),
                    evaluation_status="PASS",
                    retention_passed=True,
                    risk_passed=True,
                    authority_scope_id="paper-scope",
                    evidence_valid_until=result["evidence_valid_until"],
                )

    def test_caller_early_stop_claim_cannot_unlock_holdout_before_trial_budget(self):
        with TemporaryDirectory() as directory:
            store = ScientificRegistry(Path(directory) / "science.sqlite3")
            registered = store.register_protocol(protocol(trial_budget=2))
            locked = preregister(store, registered.protocol_id)
            candidate = "candidate-early"
            store.record_trial(
                registered.protocol_id,
                status="COMPLETED",
                payload={"candidate_id": candidate, "artifact_hash": digest(candidate)},
            )
            result = promotion_result(store, registered.protocol_id, candidate=candidate)
            result["stopping_rule_triggered"] = True
            result["stopping_rules_hash"] = store.completeness(
                registered.protocol_id
            )["stopping_rules_hash"]
            result["stopping_evidence_ref"] = (
                "artifact:11111111-1111-4111-8111-111111111111@sha256:" + "a" * 64
            )

            with self.assertRaisesRegex(
                ProtocolViolation,
                "exact registered trial budget before evaluation",
            ):
                store.register_evaluation(
                    registered.protocol_id,
                    holdout_id="frozen-A",
                    holdout_identity=locked.identity(),
                    result=result,
                )
            self.assertEqual(
                store.holdout_access_count(registered.protocol_id, "frozen-A"),
                0,
            )

            store.record_trial(
                registered.protocol_id,
                status="FAILED",
                payload={"candidate_id": "failed-search", "reason": "registered"},
            )
            complete_result = promotion_result(
                store,
                registered.protocol_id,
                candidate=candidate,
            )
            row = store.register_evaluation(
                registered.protocol_id,
                holdout_id="frozen-A",
                holdout_identity=locked.identity(),
                result=complete_result,
            )
            self.assertEqual(row["untouched"], 1)


if __name__ == "__main__":
    unittest.main()
