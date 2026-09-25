from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import NAMESPACE_URL, uuid5

from research.autotrade_research.artifacts.store import ArtifactStore
from research.autotrade_research.jobs import ResearchJobStore
from research.autotrade_research.learning.online import (
    OnlineUpdateEnvelope,
    ParameterRule,
)
from research.autotrade_research.learning.population_coverage import (
    build_population_coverage,
)
from research.autotrade_research.learning.update_producer import (
    OnlineUpdateRuntimeState,
    UpdateProducerConfig,
    produce_bounded_online_update,
    publish_online_update_result,
)
from research.autotrade_research.memory.episodes import ExperienceMemory
from research.autotrade_research.science.registry import ScientificRegistry


SOURCE_SHA = "a" * 40
FEATURE_SCHEMA = "sha256:" + "f" * 64
PROTOCOL_ID = "00000000-0000-0000-0000-000000000901"
CALIBRATION_CUTOFF = datetime(2026, 10, 5, tzinfo=timezone.utc)
UPDATE_CUTOFF = datetime(2026, 10, 10, tzinfo=timezone.utc)
DECISION = datetime(2026, 10, 1, tzinfo=timezone.utc)


def canonical_bytes(value):
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


class ProducerFixture:
    def __init__(self, directory):
        self.root = Path(directory)
        self.correction_times = {}
        self.memory = ExperienceMemory(
            self.root / "memory.sqlite3",
            correction_evidence_resolver=lambda ref: self.correction_times[ref],
        )
        self.artifacts = ArtifactStore(self.root / "artifacts")
        self.jobs = ResearchJobStore(self.root / "jobs.sqlite3")
        self.science = ScientificRegistry(self.root / "science.sqlite3")
        self.protocol_registration = None
        self.reconciliation_evidence = {}
        self.outcome_evidence = {}
        self._episode_counter = 0
        self._physical_evidence_refs = {}

    def publish_physical_evidence(self, identity):
        key = str(identity)
        existing = self._physical_evidence_refs.get(key)
        if existing is not None:
            return existing
        artifact_id = str(
            uuid5(
                NAMESPACE_URL,
                "https://evidence.autotrade.local/physical/" + key,
            )
        )
        payload = canonical_bytes(
            {
                "schema_version": "1.0.0",
                "artifact_kind": "PHYSICAL_OBSERVATION_EVIDENCE",
                "identity": key,
            }
        )
        manifest = self.artifacts.publish_bytes(
            artifact_id=artifact_id,
            data=payload,
            media_type="application/json",
            rights={"storage": True, "export": False},
            source_refs=[],
            metadata={"artifact_kind": "PHYSICAL_OBSERVATION_EVIDENCE"},
        )
        reference = "artifact:" + artifact_id + "@" + manifest["sha256"]
        self._physical_evidence_refs[key] = reference
        return reference

    def resolve_reconciliation_evidence(self, episode_id):
        return self.reconciliation_evidence[episode_id]

    def resolve_outcome_evidence(self, episode_id):
        return self.outcome_evidence[episode_id]

    def bind_outcome_evidence(
        self,
        episode_id,
        *,
        outcome_class,
        label_available_at,
        outcome_horizon_at,
        target,
        label_version="label-v1",
        observed_at=None,
        current_scope=True,
    ):
        observed = observed_at or label_available_at
        material = {
            "episode_id": episode_id,
            "outcome_class": outcome_class,
            "label_version": label_version,
            "label_available_at": label_available_at.isoformat().replace(
                "+00:00", "Z"
            ),
            "outcome_horizon_at": outcome_horizon_at.isoformat().replace(
                "+00:00", "Z"
            ),
            "target": str(target),
            "observed_at": observed.isoformat().replace("+00:00", "Z"),
        }
        self.outcome_evidence[episode_id] = {
            **material,
            "evidence_ref": f"outcome-evidence:{episode_id}",
            "evidence_digest": "sha256:" + sha256(
                canonical_bytes(material)
            ).hexdigest(),
            "current_scope": current_scope,
        }

    def bind_reconciliation_evidence(
        self,
        episode_id,
        *,
        observed_at,
        outcome="OBSERVED_EXECUTION",
    ):
        execution_ids = (
            [f"execution-{episode_id}"]
            if outcome == "OBSERVED_EXECUTION"
            else []
        )
        self.reconciliation_evidence[episode_id] = {
            "episode_id": episode_id,
            "checkpoint_event_id": f"checkpoint-{episode_id}",
            "checkpoint_payload_hash": "sha256:" + "9" * 64,
            "checkpoint_aggregate_id": f"account-reconciliation:{episode_id}",
            "checkpoint_aggregate_version": 1,
            "observed_at": observed_at.isoformat().replace("+00:00", "Z"),
            "provider_id": "TEST",
            "account_id": "paper-account",
            "environment": "PAPER",
            "attempt_id": f"attempt-{episode_id}",
            "intent_id": f"intent-{episode_id}",
            "client_order_id": f"client-{episode_id}",
            "outcome": outcome,
            "evidence_reason": "authority-backed unit-test reconciliation",
            "provider_order_ids": [],
            "provider_execution_ids": execution_ids,
            "current_scope": True,
        }

    def append_learning(
        self,
        *,
        task,
        feature,
        target,
        observation_id,
        label_available_at,
        outcome_horizon_at=None,
        execution_reconciled_at=None,
        episode_id=None,
        decision_time=None,
        evidence_ref=None,
        regime="stable",
        outcome_class="NULL",
        canonical_label_mature=True,
        canonical_reconciliation_state="RECONCILED",
        intended_side="NO_TRADE",
        bind_outcome_evidence=True,
    ):
        self._episode_counter += 1
        decision = decision_time or (
            DECISION + timedelta(minutes=self._episode_counter)
        )
        horizon = outcome_horizon_at or label_available_at
        reconciled = execution_reconciled_at or label_available_at
        physical_evidence = evidence_ref or self.publish_physical_evidence(
            observation_id
        )
        payload = {
            "evidence_refs": [physical_evidence],
            "intended_action": {
                "kind": "NO_TRADE",
                "side": intended_side,
            },
            "actual_execution": {"kind": "NO_TRADE"},
            "outcome": {
                "class": outcome_class,
                "label_mature": canonical_label_mature,
                "reconciliation_state": canonical_reconciliation_state,
            },
            "costs": {"total": "0"},
            "learning": {
                "observation_id": observation_id,
                "label_version": "label-v1",
                "label_available_at": label_available_at.isoformat().replace(
                    "+00:00", "Z"
                ),
                "outcome_horizon_at": horizon.isoformat().replace(
                    "+00:00", "Z"
                ),
                "execution_reconciled_at": reconciled.isoformat().replace(
                    "+00:00", "Z"
                ),
                "features": {"x": str(feature)},
                "target": str(target),
            },
        }
        stored_episode_id = self.memory.append_episode(
            decision_time=decision,
            information_cutoff=decision,
            task=task,
            regime=regime,
            instrument_family="equity",
            permission_class="research",
            payload=payload,
            episode_id=episode_id,
        )[0]
        if bind_outcome_evidence:
            self.bind_outcome_evidence(
                stored_episode_id,
                outcome_class=outcome_class,
                label_available_at=label_available_at,
                outcome_horizon_at=horizon,
                target=target,
            )
        return stored_episode_id

    def seed_calibration(self, values=("0", "1", "2", "3")):
        for index, value in enumerate(values):
            self.append_learning(
                task="calibration",
                feature=value,
                target="0",
                observation_id=f"cal-{index}",
                label_available_at=datetime(
                    2026, 10, 3, 12, index, tzinfo=timezone.utc
                ),
            )

    def seed_update(self, *, feature="1", target="1", observation_id="up-1"):
        return self.append_learning(
            task="update",
            feature=feature,
            target=target,
            observation_id=observation_id,
            label_available_at=datetime(
                2026, 10, 8, tzinfo=timezone.utc
            ),
        )


    def register_protocol(self):
        snapshot = self.memory.coverage_population_snapshot(
            causal_cutoff=CALIBRATION_CUTOFF,
            granted_permissions={"research"},
            task="calibration",
            instrument_family="equity",
        )
        payload = {
            "hypothesis": "bounded update preregistered before candidate publication",
            "strategy": "bounded-linear-gradient-v1",
            "features": ["x"],
            "search_space": {"learning_rate": ["1"]},
            "train_period": {"start": "2024-01-01", "end": "2024-12-31"},
            "validation_period": {"start": "2025-01-01", "end": "2025-06-30"},
            "test_period": {"start": "2025-07-01", "end": "2025-12-31"},
            "forward_period": {"start": "2026-01-01", "end": "2026-06-30"},
            "labels": ["label-v1"],
            "horizons": ["1d"],
            "purge_embargo": {"purge": "1d", "embargo": "1d"},
            "universe": ["equity"],
            "cost_fill_model": "test-cost-v1",
            "baselines": ["zero-update"],
            "primary_metrics": ["bounded-loss"],
            "secondary_metrics": ["drift"],
            "trial_budget": 3,
            "stopping_rules": "fail closed on evidence gaps",
            "statistical_estimator": "deterministic",
            "multiplicity_treatment": "registered",
            "minimum_practical_effect": "0.001",
            "risk_constraints": {"max_update": "bounded"},
            "retention_tolerances": {"prior_regime_loss": "0"},
            "promotion_rule": "independent downstream gates",
            "online_update_registration": {
                "schema_version": "1.0.0",
                "calibration_population_root_hash": snapshot.root_hash,
                "calibration_cutoff": CALIBRATION_CUTOFF.isoformat().replace(
                    "+00:00", "Z"
                ),
                "update_task": "update",
                "calibration_task": "calibration",
                "instrument_family": "equity",
                "permission_classes": ["research"],
                "feature_schema_hash": FEATURE_SCHEMA,
                "label_version": "label-v1",
                "source_sha": SOURCE_SHA,
            },
        }
        self.protocol_registration = self.science.register_protocol(
            payload,
            protocol_id=PROTOCOL_ID,
        )
        return self.protocol_registration

    def publish_checkpoint(self, *, preregister=True):
        if preregister and self.protocol_registration is None:
            self.register_protocol()
        payload = {
            "schema_version": "1.0.0",
            "artifact_kind": "BOUNDED_LINEAR_CHECKPOINT",
            "algorithm_id": "bounded-linear-gradient-v1",
            "algorithm_version": "1.0.0",
            "feature_schema_hash": FEATURE_SCHEMA,
            "label_version": "label-v1",
            "parameters": {"x": "0"},
            "reference_feature_means": {"x": "0"},
        }
        manifest = self.artifacts.publish_bytes(
            artifact_id="00000000-0000-0000-0000-000000000101",
            data=canonical_bytes(payload),
            media_type="application/json",
            rights={"storage": True, "export": False},
            source_refs=[],
            metadata={"artifact_kind": "BOUNDED_LINEAR_CHECKPOINT"},
        )
        return (
            "artifact:00000000-0000-0000-0000-000000000101@"
            + manifest["sha256"]
        )

    def publish_test_evidence(self, *, source_sha=SOURCE_SHA):
        manifest = self.artifacts.publish_bytes(
            artifact_id="00000000-0000-0000-0000-000000000102",
            data=canonical_bytes(
                {
                    "schema_version": "1.0.0",
                    "kind": "producer-tests",
                    "source_sha": source_sha,
                }
            ),
            media_type="application/json",
            rights={"storage": True, "export": False},
            source_refs=[],
            metadata={
                "artifact_kind": "QUALIFICATION_TEST_EVIDENCE",
                "source_sha": source_sha,
            },
        )
        return (
            "artifact:00000000-0000-0000-0000-000000000102@"
            + manifest["sha256"]
        )

    def publish_calibration_evidence(self, *, population_root=None):
        snapshot = self.memory.coverage_population_snapshot(
            causal_cutoff=CALIBRATION_CUTOFF,
            granted_permissions={"research"},
            task="calibration",
            instrument_family="equity",
        )
        root = population_root or snapshot.root_hash
        manifest = self.artifacts.publish_bytes(
            artifact_id="00000000-0000-0000-0000-000000000103",
            data=canonical_bytes(
                {
                    "schema_version": "1.0.0",
                    "kind": "stable-drift-calibration",
                    "population_root_hash": root,
                }
            ),
            media_type="application/json",
            rights={"storage": True, "export": False},
            source_refs=[],
            metadata={
                "artifact_kind": "DRIFT_CALIBRATION_POPULATION",
                "population_root_hash": root,
            },
        )
        return (
            "artifact:00000000-0000-0000-0000-000000000103@"
            + manifest["sha256"]
        )

    def config(
        self,
        test_ref,
        *,
        min_update_episodes=1,
        min_calibration_episodes=4,
        max_compute_units="100",
        source_sha=SOURCE_SHA,
        learning_rate="1",
        protocol_id=PROTOCOL_ID,
    ):
        return UpdateProducerConfig(
            source_sha=source_sha,
            algorithm_version="1.0.0",
            feature_schema_hash=FEATURE_SCHEMA,
            label_version="label-v1",
            learning_rate=Decimal(learning_rate),
            min_update_episodes=min_update_episodes,
            min_calibration_episodes=min_calibration_episodes,
            target_false_alarm_rate=Decimal("0.25"),
            max_compute_units=Decimal(max_compute_units),
            update_task="update",
            calibration_task="calibration",
            test_evidence_refs=(test_ref,),
            instrument_family="equity",
            protocol_id=protocol_id,
        )

    @staticmethod
    def envelope(checkpoint_ref, *, max_step="2", max_compute="100"):
        champion_hash = "sha256:" + checkpoint_ref.rsplit(
            "@sha256:", 1
        )[1]
        return OnlineUpdateEnvelope.create(
            envelope_id="online-envelope-v1",
            champion_artifact_hash=champion_hash,
            parameter_rules=(
                ParameterRule.create(
                    name="x",
                    minimum="-10",
                    maximum="10",
                    max_absolute_step=max_step,
                ),
            ),
            eligible_label_versions=("label-v1",),
            min_seconds_between_updates=0,
            max_updates_per_window=10,
            max_compute_units_per_update=max_compute,
            max_drift_score="100",
        )


    def population_manifest(
        self,
        *,
        checkpoint_ref,
        task,
        cutoff,
        extra_exclusions=None,
        frozen_protocol_hash=None,
    ):
        if frozen_protocol_hash is None:
            if self.protocol_registration is None:
                raise RuntimeError("protocol must be registered before population manifest")
            frozen_protocol_hash = self.protocol_registration.protocol_hash
        snapshot = self.memory.coverage_population_snapshot(
            causal_cutoff=cutoff,
            granted_permissions={"research"},
            task=task,
            instrument_family="equity",
        )
        exclusions = {
            row["episode_id"]: "TOMBSTONED"
            for row in snapshot.rows
            if row["tombstone_lineage"]
        }
        for episode_id, reason in (extra_exclusions or {}).items():
            exclusions[episode_id] = reason
        included = [
            row["episode_id"]
            for row in snapshot.rows
            if row["episode_id"] not in exclusions
        ]
        candidate_hash = (
            "sha256:" + checkpoint_ref.rsplit("@sha256:", 1)[1]
        )
        return build_population_coverage(
            snapshot,
            candidate_hash=candidate_hash,
            frozen_protocol_hash=frozen_protocol_hash,
            input_snapshot_hash=snapshot.root_hash,
            causal_cutoff=cutoff,
            permission_classes=["research"],
            included_episode_ids=included,
            exclusions=exclusions,
            reconciliation_evidence_resolver=self.resolve_reconciliation_evidence,
            outcome_evidence_resolver=self.resolve_outcome_evidence,
            task=task,
            instrument_family="equity",
        )

    def produce(
        self,
        *,
        checkpoint_ref,
        calibration_ref,
        envelope,
        config,
        calibration_cutoff=CALIBRATION_CUTOFF,
        update_cutoff=UPDATE_CUTOFF,
        runtime_state=None,
        granted_permissions=None,
        with_population_manifests=True,
        update_population_manifest=None,
        calibration_population_manifest=None,
    ):
        permissions = granted_permissions or {"research"}
        if with_population_manifests:
            if update_population_manifest is None:
                update_population_manifest = self.population_manifest(
                    checkpoint_ref=checkpoint_ref,
                    task=config.update_task,
                    cutoff=update_cutoff,
                )
            if calibration_population_manifest is None:
                calibration_population_manifest = self.population_manifest(
                    checkpoint_ref=checkpoint_ref,
                    task=config.calibration_task,
                    cutoff=calibration_cutoff,
                )
        return produce_bounded_online_update(
            memory=self.memory,
            artifact_store=self.artifacts,
            checkpoint_ref=checkpoint_ref,
            calibration_evidence_ref=calibration_ref,
            envelope=envelope,
            config=config,
            runtime_state=runtime_state or OnlineUpdateRuntimeState(
                last_update_at=None,
                updates_in_window=0,
            ),
            update_cutoff=update_cutoff,
            calibration_cutoff=calibration_cutoff,
            granted_permissions=permissions,
            update_population_manifest=update_population_manifest,
            calibration_population_manifest=calibration_population_manifest,
            scientific_registry=self.science,
            reconciliation_evidence_resolver=self.resolve_reconciliation_evidence,
            outcome_evidence_resolver=self.resolve_outcome_evidence,
        )


class UpdateProducerTests(unittest.TestCase):
    def ready_fixture(self, directory, *, max_step="2"):
        fixture = ProducerFixture(directory)
        fixture.seed_calibration()
        fixture.seed_update()
        checkpoint = fixture.publish_checkpoint()
        test_ref = fixture.publish_test_evidence()
        calibration = fixture.publish_calibration_evidence()
        return (
            fixture,
            checkpoint,
            test_ref,
            calibration,
            fixture.envelope(checkpoint, max_step=max_step),
        )

    def test_same_immutable_population_produces_byte_identical_artifact(self):
        with TemporaryDirectory() as directory:
            fixture, checkpoint, test_ref, calibration, envelope = (
                self.ready_fixture(directory)
            )
            config = fixture.config(test_ref)
            first = fixture.produce(
                checkpoint_ref=checkpoint,
                calibration_ref=calibration,
                envelope=envelope,
                config=config,
            )
            second = fixture.produce(
                checkpoint_ref=checkpoint,
                calibration_ref=calibration,
                envelope=envelope,
                config=config,
            )
            self.assertEqual(first.status, "UPDATE_PROPOSED")
            self.assertEqual(first.artifact_bytes, second.artifact_bytes)
            self.assertEqual(first.artifact_digest, second.artifact_digest)
            self.assertEqual(first.proposed_parameters["x"], Decimal("1"))
            self.assertEqual(first.calibrated_threshold, Decimal("2"))
            artifact = json.loads(first.artifact_bytes)
            self.assertEqual(
                artifact["calibration"]["false_alarm_count"],
                1,
            )
            self.assertEqual(
                artifact["calibration"]["false_alarm_denominator"],
                4,
            )
            self.assertEqual(
                artifact["calibration"]["measured_false_alarm_rate"],
                "0.25",
            )
            self.assertFalse(
                artifact["online_gate"]["grants_trading_authority"]
            )
            self.assertEqual(
                artifact["scientific_registration"]["protocol_id"],
                PROTOCOL_ID,
            )
            self.assertEqual(
                artifact["scientific_registration"]["protocol_hash"],
                fixture.protocol_registration.protocol_hash,
            )
            self.assertEqual(
                artifact["producer_config"]["protocol_id"],
                PROTOCOL_ID,
            )

            self.assertEqual(
                artifact["producer_config"]["learning_rate"],
                "1",
            )
            self.assertEqual(
                artifact["producer_config"]["min_update_episodes"],
                1,
            )
            self.assertEqual(
                artifact["online_envelope"]["envelope_id"],
                "online-envelope-v1",
            )
            self.assertEqual(
                artifact["online_envelope"]["parameter_rules"][0],
                {
                    "name": "x",
                    "minimum": "-10",
                    "maximum": "10",
                    "max_absolute_step": "2",
                },
            )
            self.assertEqual(
                artifact["runtime_state"],
                {"last_update_at": None, "updates_in_window": 0},
            )
            self.assertEqual(
                artifact["granted_permissions"],
                ["research"],
            )
            self.assertTrue(
                artifact["producer_config_sha256"].startswith("sha256:")
            )
            self.assertTrue(
                artifact["online_envelope_sha256"].startswith("sha256:")
            )


    def test_scientific_preregistration_is_required_before_update(self):
        with TemporaryDirectory() as directory:
            fixture, checkpoint, test_ref, calibration, envelope = (
                self.ready_fixture(directory)
            )
            produced = fixture.produce(
                checkpoint_ref=checkpoint,
                calibration_ref=calibration,
                envelope=envelope,
                config=fixture.config(test_ref, protocol_id=None),
            )
            self.assertEqual(produced.status, "NO_UPDATE")
            self.assertIsNone(produced.proposed_parameters)
            artifact = json.loads(produced.artifact_bytes)
            self.assertIn(
                "LEARNING.SCIENTIFIC_PREREGISTRATION_REQUIRED",
                artifact["reasons"],
            )
            self.assertIsNone(artifact["scientific_registration"])

    def test_protocol_registered_after_checkpoint_is_too_late(self):
        with TemporaryDirectory() as directory:
            fixture = ProducerFixture(directory)
            fixture.seed_calibration()
            fixture.seed_update()
            checkpoint = fixture.publish_checkpoint(preregister=False)
            fixture.register_protocol()
            test_ref = fixture.publish_test_evidence()
            calibration = fixture.publish_calibration_evidence()
            produced = fixture.produce(
                checkpoint_ref=checkpoint,
                calibration_ref=calibration,
                envelope=fixture.envelope(checkpoint),
                config=fixture.config(test_ref),
            )
            artifact = json.loads(produced.artifact_bytes)
            self.assertEqual(produced.status, "NO_UPDATE")
            self.assertIn(
                "LEARNING.SCIENTIFIC_PREREGISTRATION_LATE",
                artifact["reasons"],
            )
            self.assertIsNone(artifact["scientific_registration"])

    def test_frozen_protocol_rejects_later_calibration_population_change(self):
        with TemporaryDirectory() as directory:
            fixture = ProducerFixture(directory)
            fixture.seed_calibration()
            fixture.register_protocol()
            fixture.append_learning(
                task="calibration",
                feature="4",
                target="0",
                observation_id="post-freeze-calibration",
                label_available_at=datetime(
                    2026, 10, 3, 13, tzinfo=timezone.utc
                ),
            )
            fixture.seed_update()
            checkpoint = fixture.publish_checkpoint()
            test_ref = fixture.publish_test_evidence()
            calibration = fixture.publish_calibration_evidence()
            produced = fixture.produce(
                checkpoint_ref=checkpoint,
                calibration_ref=calibration,
                envelope=fixture.envelope(checkpoint),
                config=fixture.config(test_ref, min_calibration_episodes=5),
            )
            artifact = json.loads(produced.artifact_bytes)
            self.assertEqual(produced.status, "NO_UPDATE")
            self.assertIn(
                "LEARNING.SCIENTIFIC_PREREGISTRATION_SCOPE_MISMATCH",
                artifact["reasons"],
            )
            self.assertIsNone(artifact["scientific_registration"])

    def test_population_manifest_protocol_hash_must_be_registry_issued(self):
        with TemporaryDirectory() as directory:
            fixture, checkpoint, test_ref, calibration, envelope = (
                self.ready_fixture(directory)
            )
            forged = "sha256:" + "d" * 64
            update_manifest = fixture.population_manifest(
                checkpoint_ref=checkpoint,
                task="update",
                cutoff=UPDATE_CUTOFF,
                frozen_protocol_hash=forged,
            )
            calibration_manifest = fixture.population_manifest(
                checkpoint_ref=checkpoint,
                task="calibration",
                cutoff=CALIBRATION_CUTOFF,
                frozen_protocol_hash=forged,
            )
            produced = fixture.produce(
                checkpoint_ref=checkpoint,
                calibration_ref=calibration,
                envelope=envelope,
                config=fixture.config(test_ref),
                update_population_manifest=update_manifest,
                calibration_population_manifest=calibration_manifest,
            )
            artifact = json.loads(produced.artifact_bytes)
            self.assertEqual(produced.status, "NO_UPDATE")
            self.assertIn(
                "LEARNING.SCIENTIFIC_PREREGISTRATION_HASH_MISMATCH",
                artifact["reasons"],
            )
            self.assertIsNone(artifact["scientific_registration"])

    def test_population_authority_is_required_before_any_update_proposal(self):
        with TemporaryDirectory() as directory:
            fixture, checkpoint, test_ref, calibration, envelope = (
                self.ready_fixture(directory)
            )
            produced = fixture.produce(
                checkpoint_ref=checkpoint,
                calibration_ref=calibration,
                envelope=envelope,
                config=fixture.config(test_ref),
                with_population_manifests=False,
            )
            self.assertEqual(produced.status, "NO_UPDATE")
            self.assertIsNone(produced.proposed_parameters)
            artifact = json.loads(produced.artifact_bytes)
            self.assertIn(
                "LEARNING.UPDATE_POPULATION_COVERAGE_REQUIRED",
                artifact["reasons"],
            )
            self.assertIn(
                "LEARNING.CALIBRATION_POPULATION_COVERAGE_REQUIRED",
                artifact["reasons"],
            )
            self.assertIsNone(
                artifact["evidence"]["population_authority"][
                    "update_manifest_digest"
                ]
            )

    def test_valid_episode_cannot_be_omitted_by_favorable_population_manifest(self):
        with TemporaryDirectory() as directory:
            fixture, checkpoint, test_ref, calibration, envelope = (
                self.ready_fixture(directory)
            )
            omitted = fixture.append_learning(
                task="update",
                feature="-9",
                target="-10",
                observation_id="negative-evidence",
                label_available_at=datetime(
                    2026, 10, 8, 1, tzinfo=timezone.utc
                ),
                outcome_class="NEGATIVE",
                intended_side="BUY",
            )
            forged_manifest = fixture.population_manifest(
                checkpoint_ref=checkpoint,
                task="update",
                cutoff=UPDATE_CUTOFF,
                extra_exclusions={
                    omitted: "CALLER_SELECTED_FAVORABLE_SUBSET",
                },
            )
            produced = fixture.produce(
                checkpoint_ref=checkpoint,
                calibration_ref=calibration,
                envelope=envelope,
                config=fixture.config(test_ref),
                update_population_manifest=forged_manifest,
            )
            self.assertEqual(produced.status, "NO_UPDATE")
            self.assertIsNone(produced.proposed_parameters)
            artifact = json.loads(produced.artifact_bytes)
            self.assertIn(
                "LEARNING.UPDATE_POPULATION_COVERAGE_MISMATCH",
                artifact["reasons"],
            )

    def test_caller_payload_maturity_cannot_replace_missing_outcome_authority(self):
        with TemporaryDirectory() as directory:
            fixture, checkpoint, test_ref, calibration, envelope = (
                self.ready_fixture(directory)
            )
            fixture.append_learning(
                task="update",
                feature="2",
                target="1",
                observation_id="caller-claims-mature",
                label_available_at=datetime(
                    2026, 10, 8, 2, tzinfo=timezone.utc
                ),
                canonical_label_mature=True,
                canonical_reconciliation_state="RECONCILED",
                bind_outcome_evidence=False,
            )
            produced = fixture.produce(
                checkpoint_ref=checkpoint,
                calibration_ref=calibration,
                envelope=envelope,
                config=fixture.config(test_ref),
            )
            self.assertEqual(produced.status, "NO_UPDATE")
            self.assertIsNone(produced.proposed_parameters)
            artifact = json.loads(produced.artifact_bytes)
            self.assertIn(
                "OUTCOME_EVIDENCE_UNVERIFIED",
                {
                    reason
                    for _episode_id, reason
                    in artifact["population"]["update_exclusions"]
                },
            )
            self.assertIn(
                "LEARNING.UPDATE_POPULATION_COVERAGE_MISMATCH",
                artifact["reasons"],
            )

    def test_forged_reconciled_trade_without_authority_proof_is_no_update(self):
        with TemporaryDirectory() as directory:
            fixture, checkpoint, test_ref, calibration, envelope = (
                self.ready_fixture(directory)
            )
            forged = fixture.append_learning(
                task="update",
                feature="2",
                target="1",
                observation_id="forged-reconciled-trade",
                label_available_at=datetime(
                    2026, 10, 8, 3, tzinfo=timezone.utc
                ),
                outcome_class="POSITIVE",
                canonical_label_mature=True,
                canonical_reconciliation_state="RECONCILED",
                intended_side="BUY",
            )
            exact_manifest = fixture.population_manifest(
                checkpoint_ref=checkpoint,
                task="update",
                cutoff=UPDATE_CUTOFF,
                extra_exclusions={
                    forged: "RECONCILIATION_EVIDENCE_UNVERIFIED",
                },
            )
            self.assertFalse(exact_manifest.complete)

            produced = fixture.produce(
                checkpoint_ref=checkpoint,
                calibration_ref=calibration,
                envelope=envelope,
                config=fixture.config(test_ref),
                update_population_manifest=exact_manifest,
            )

            self.assertEqual(produced.status, "NO_UPDATE")
            self.assertIsNone(produced.proposed_parameters)
            artifact = json.loads(produced.artifact_bytes)
            self.assertIn(
                "LEARNING.UPDATE_POPULATION_COVERAGE_INCOMPLETE",
                artifact["reasons"],
            )
            self.assertIn(
                [forged, "RECONCILIATION_EVIDENCE_UNVERIFIED"],
                artifact["population"]["update_exclusions"],
            )

    def test_reconciliation_proof_for_different_episode_is_rejected(self):
        with TemporaryDirectory() as directory:
            fixture, checkpoint, test_ref, calibration, envelope = (
                self.ready_fixture(directory)
            )
            episode_id = fixture.append_learning(
                task="update",
                feature="2",
                target="1",
                observation_id="wrong-episode-reconciliation",
                label_available_at=datetime(
                    2026, 10, 8, 4, tzinfo=timezone.utc
                ),
                outcome_class="POSITIVE",
                intended_side="BUY",
            )
            fixture.bind_reconciliation_evidence(
                episode_id,
                observed_at=datetime(
                    2026, 10, 8, 3, 59, tzinfo=timezone.utc
                ),
            )
            fixture.reconciliation_evidence[episode_id]["episode_id"] = (
                "00000000-0000-4000-8000-000000000099"
            )

            produced = fixture.produce(
                checkpoint_ref=checkpoint,
                calibration_ref=calibration,
                envelope=envelope,
                config=fixture.config(test_ref),
            )

            self.assertEqual(produced.status, "NO_UPDATE")
            artifact = json.loads(produced.artifact_bytes)
            self.assertIn(
                [episode_id, "RECONCILIATION_EVIDENCE_INVALID"],
                artifact["population"]["update_exclusions"],
            )

    def test_authority_reconciliation_proof_is_bound_into_learning_row(self):
        with TemporaryDirectory() as directory:
            fixture, checkpoint, test_ref, calibration, envelope = (
                self.ready_fixture(directory)
            )
            episode_id = fixture.append_learning(
                task="update",
                feature="2",
                target="1",
                observation_id="authority-backed-trade",
                label_available_at=datetime(
                    2026, 10, 8, 4, tzinfo=timezone.utc
                ),
                outcome_class="POSITIVE",
                intended_side="BUY",
            )
            fixture.bind_reconciliation_evidence(
                episode_id,
                observed_at=datetime(
                    2026, 10, 8, 3, 59, tzinfo=timezone.utc
                ),
            )

            produced = fixture.produce(
                checkpoint_ref=checkpoint,
                calibration_ref=calibration,
                envelope=envelope,
                config=fixture.config(test_ref),
            )

            self.assertEqual(produced.status, "UPDATE_PROPOSED")
            artifact = json.loads(produced.artifact_bytes)
            row = next(
                item
                for item in artifact["population"]["update_included"]
                if item["episode_id"] == episode_id
            )
            proof = row["reconciliation_evidence"]
            self.assertEqual(proof["status"], "VERIFIED")
            self.assertEqual(
                proof["checkpoint_event_id"],
                f"checkpoint-{episode_id}",
            )
            self.assertEqual(proof["checkpoint_aggregate_version"], 1)
            self.assertEqual(
                proof["observed_at"],
                "2026-10-08T03:59:00+00:00",
            )
            self.assertTrue(proof["current_scope"])

    def test_population_protocol_identity_must_match_across_update_and_calibration(self):
        with TemporaryDirectory() as directory:
            fixture, checkpoint, test_ref, calibration, envelope = (
                self.ready_fixture(directory)
            )
            update_manifest = fixture.population_manifest(
                checkpoint_ref=checkpoint,
                task="update",
                cutoff=UPDATE_CUTOFF,
                frozen_protocol_hash=fixture.protocol_registration.protocol_hash,
            )
            calibration_manifest = fixture.population_manifest(
                checkpoint_ref=checkpoint,
                task="calibration",
                cutoff=CALIBRATION_CUTOFF,
                frozen_protocol_hash="sha256:" + "d" * 64,
            )
            produced = fixture.produce(
                checkpoint_ref=checkpoint,
                calibration_ref=calibration,
                envelope=envelope,
                config=fixture.config(test_ref),
                update_population_manifest=update_manifest,
                calibration_population_manifest=calibration_manifest,
            )
            self.assertEqual(produced.status, "NO_UPDATE")
            artifact = json.loads(produced.artifact_bytes)
            self.assertIn(
                "LEARNING.POPULATION_PROTOCOL_MISMATCH",
                artifact["reasons"],
            )
            self.assertIsNone(
                artifact["evidence"]["population_authority"][
                    "frozen_protocol_hash"
                ]
            )

    def test_early_no_update_still_binds_full_config_envelope_and_runtime(self):
        with TemporaryDirectory() as directory:
            fixture, checkpoint, test_ref, calibration, envelope = (
                self.ready_fixture(directory)
            )
            runtime = OnlineUpdateRuntimeState(
                last_update_at=UPDATE_CUTOFF - timedelta(hours=1),
                updates_in_window=3,
            )
            produced = fixture.produce(
                checkpoint_ref=checkpoint,
                calibration_ref=calibration,
                envelope=envelope,
                config=fixture.config(
                    test_ref,
                    min_update_episodes=2,
                    learning_rate="0.25",
                ),
                runtime_state=runtime,
                granted_permissions={"research"},
            )
            self.assertEqual(produced.status, "NO_UPDATE")
            artifact = json.loads(produced.artifact_bytes)
            self.assertIsNone(artifact["online_gate"])
            self.assertIsNone(artifact["proposal"])
            self.assertEqual(
                artifact["producer_config"]["learning_rate"],
                "0.25",
            )
            self.assertEqual(
                artifact["producer_config"]["min_update_episodes"],
                2,
            )
            self.assertEqual(
                artifact["online_envelope"]["champion_artifact_hash"],
                "sha256:" + checkpoint.rsplit("@sha256:", 1)[1],
            )
            self.assertEqual(
                artifact["runtime_state"]["updates_in_window"],
                3,
            )
            self.assertEqual(
                artifact["runtime_state"]["last_update_at"],
                "2026-10-09T23:00:00Z",
            )
            self.assertIn(
                "LEARNING.INSUFFICIENT_UPDATE_EVIDENCE",
                artifact["reasons"],
            )

    def test_learning_rate_is_part_of_identity_and_changes_proposal(self):
        with TemporaryDirectory() as directory:
            fixture, checkpoint, test_ref, calibration, envelope = (
                self.ready_fixture(directory)
            )
            full = fixture.produce(
                checkpoint_ref=checkpoint,
                calibration_ref=calibration,
                envelope=envelope,
                config=fixture.config(test_ref, learning_rate="1"),
            )
            half = fixture.produce(
                checkpoint_ref=checkpoint,
                calibration_ref=calibration,
                envelope=envelope,
                config=fixture.config(test_ref, learning_rate="0.5"),
            )
            self.assertEqual(full.proposed_parameters["x"], Decimal("1"))
            self.assertEqual(half.proposed_parameters["x"], Decimal("0.5"))
            self.assertNotEqual(full.artifact_digest, half.artifact_digest)
            full_artifact = json.loads(full.artifact_bytes)
            half_artifact = json.loads(half.artifact_bytes)
            self.assertNotEqual(
                full_artifact["producer_config_sha256"],
                half_artifact["producer_config_sha256"],
            )
            self.assertEqual(
                half_artifact["producer_config"]["learning_rate"],
                "0.5",
            )

    def test_physical_evidence_availability_cannot_be_backdated_by_label(self):
        with TemporaryDirectory() as directory:
            fixture, checkpoint, test_ref, calibration, envelope = (
                self.ready_fixture(directory)
            )
            backdated = fixture.append_learning(
                task="update",
                feature="2",
                target="1",
                observation_id="backdated-evidence",
                label_available_at=datetime(
                    2026, 9, 1, tzinfo=timezone.utc
                ),
            )
            exact_manifest = fixture.population_manifest(
                checkpoint_ref=checkpoint,
                task="update",
                cutoff=UPDATE_CUTOFF,
                extra_exclusions={
                    backdated: "PHYSICAL_EVIDENCE_NOT_CAUSALLY_AVAILABLE",
                },
            )
            self.assertFalse(exact_manifest.complete)

            produced = fixture.produce(
                checkpoint_ref=checkpoint,
                calibration_ref=calibration,
                envelope=envelope,
                config=fixture.config(test_ref),
                update_population_manifest=exact_manifest,
            )

            self.assertEqual(produced.status, "NO_UPDATE")
            self.assertIsNone(produced.proposed_parameters)
            artifact = json.loads(produced.artifact_bytes)
            self.assertIn(
                "LEARNING.UPDATE_POPULATION_COVERAGE_INCOMPLETE",
                artifact["reasons"],
            )
            self.assertIn(
                [backdated, "PHYSICAL_EVIDENCE_NOT_CAUSALLY_AVAILABLE"],
                artifact["population"]["update_exclusions"],
            )

    def test_future_label_is_excluded_and_cannot_change_proposal(self):
        with TemporaryDirectory() as directory:
            fixture, checkpoint, test_ref, calibration, envelope = (
                self.ready_fixture(directory)
            )
            config = fixture.config(test_ref)
            baseline = fixture.produce(
                checkpoint_ref=checkpoint,
                calibration_ref=calibration,
                envelope=envelope,
                config=config,
            )
            fixture.append_learning(
                task="update",
                feature="9",
                target="99",
                observation_id="future-label",
                label_available_at=UPDATE_CUTOFF + timedelta(days=1),
                outcome_horizon_at=UPDATE_CUTOFF + timedelta(days=1),
                execution_reconciled_at=UPDATE_CUTOFF + timedelta(days=1),
            )
            with_future = fixture.produce(
                checkpoint_ref=checkpoint,
                calibration_ref=calibration,
                envelope=envelope,
                config=config,
            )
            self.assertEqual(baseline.status, "UPDATE_PROPOSED")
            self.assertEqual(with_future.status, "NO_UPDATE")
            self.assertIsNone(with_future.proposed_parameters)
            artifact = json.loads(with_future.artifact_bytes)
            self.assertIn(
                "LABEL_NOT_CAUSALLY_MATURE",
                {
                    reason
                    for _episode, reason
                    in artifact["population"]["update_exclusions"]
                },
            )
            self.assertIn(
                "LEARNING.UPDATE_POPULATION_COVERAGE_MISMATCH",
                artifact["reasons"],
            )

    def test_exact_matching_manifest_cannot_hide_excluded_unresolved_episode(self):
        with TemporaryDirectory() as directory:
            fixture, checkpoint, test_ref, calibration, envelope = (
                self.ready_fixture(directory)
            )
            config = fixture.config(test_ref)
            unresolved = fixture.append_learning(
                task="update",
                feature="9",
                target="99",
                observation_id="future-pending",
                label_available_at=UPDATE_CUTOFF + timedelta(days=1),
                outcome_horizon_at=UPDATE_CUTOFF + timedelta(days=1),
                execution_reconciled_at=UPDATE_CUTOFF + timedelta(days=1),
                outcome_class="PENDING",
                intended_side="BUY",
                canonical_label_mature=False,
                canonical_reconciliation_state="PENDING",
            )
            exact_manifest = fixture.population_manifest(
                checkpoint_ref=checkpoint,
                task="update",
                cutoff=UPDATE_CUTOFF,
                extra_exclusions={
                    unresolved: "LABEL_NOT_CAUSALLY_MATURE",
                },
            )
            self.assertFalse(exact_manifest.complete)

            produced = fixture.produce(
                checkpoint_ref=checkpoint,
                calibration_ref=calibration,
                envelope=envelope,
                config=config,
                update_population_manifest=exact_manifest,
            )

            self.assertEqual(produced.status, "NO_UPDATE")
            self.assertIsNone(produced.proposed_parameters)
            artifact = json.loads(produced.artifact_bytes)
            self.assertNotIn(
                "LEARNING.UPDATE_POPULATION_COVERAGE_MISMATCH",
                artifact["reasons"],
            )
            self.assertIn(
                "LEARNING.UPDATE_POPULATION_COVERAGE_INCOMPLETE",
                artifact["reasons"],
            )
            self.assertIn(
                [unresolved, "LABEL_NOT_CAUSALLY_MATURE"],
                artifact["population"]["update_exclusions"],
            )

    def test_visible_correction_is_applied_once_and_lineage_is_bound(self):
        with TemporaryDirectory() as directory:
            fixture = ProducerFixture(directory)
            fixture.seed_calibration()
            episode = fixture.append_learning(
                task="update",
                feature="1",
                target="0",
                observation_id="corrected-observation",
                label_available_at=datetime(
                    2026, 10, 8, tzinfo=timezone.utc
                ),
                episode_id="00000000-0000-0000-0000-000000000201",
            )
            correction_time = datetime(
                2026, 10, 9, tzinfo=timezone.utc
            )
            evidence_ref = "artifact:correction-evidence"
            fixture.correction_times[evidence_ref] = correction_time
            fixture.memory.append_correction(
                episode,
                correction_id="00000000-0000-0000-0000-000000000202",
                available_at=correction_time,
                payload={
                    "supersedes_fields": ["learning"],
                    "learning": {
                        "observation_id": "corrected-observation",
                        "label_version": "label-v1",
                        "label_available_at": "2026-10-08T00:00:00Z",
                        "outcome_horizon_at": "2026-10-08T00:00:00Z",
                        "execution_reconciled_at": "2026-10-08T00:00:00Z",
                        "features": {"x": "1"},
                        "target": "1",
                    },
                    "evidence_ref": evidence_ref,
                },
            )
            checkpoint = fixture.publish_checkpoint()
            test_ref = fixture.publish_test_evidence()
            calibration = fixture.publish_calibration_evidence()
            produced = fixture.produce(
                checkpoint_ref=checkpoint,
                calibration_ref=calibration,
                envelope=fixture.envelope(checkpoint),
                config=fixture.config(test_ref),
            )
            self.assertEqual(produced.proposed_parameters["x"], Decimal("1"))
            artifact = json.loads(produced.artifact_bytes)
            rows = artifact["population"]["update_included"]
            self.assertEqual(len(rows), 1)
            self.assertEqual(len(rows[0]["correction_hashes"]), 1)
            self.assertTrue(
                rows[0]["correction_hashes"][0].startswith("sha256:")
            )

    def test_semantic_alias_is_counted_once_and_duplicate_is_explicit(self):
        with TemporaryDirectory() as directory:
            fixture = ProducerFixture(directory)
            fixture.seed_calibration()
            fixture.append_learning(
                task="update",
                feature="1",
                target="1",
                observation_id="same-observation",
                label_available_at=datetime(
                    2026, 10, 8, tzinfo=timezone.utc
                ),
                episode_id="00000000-0000-0000-0000-000000000211",
                decision_time=DECISION,
            )
            fixture.append_learning(
                task="update",
                feature="1",
                target="1",
                observation_id="same-observation",
                label_available_at=datetime(
                    2026, 10, 8, tzinfo=timezone.utc
                ),
                episode_id="00000000-0000-0000-0000-000000000212",
                decision_time=DECISION,
                regime="stable-alias",
            )
            checkpoint = fixture.publish_checkpoint()
            test_ref = fixture.publish_test_evidence()
            calibration = fixture.publish_calibration_evidence()
            produced = fixture.produce(
                checkpoint_ref=checkpoint,
                calibration_ref=calibration,
                envelope=fixture.envelope(checkpoint),
                config=fixture.config(test_ref),
            )
            artifact = json.loads(produced.artifact_bytes)
            self.assertEqual(
                len(artifact["population"]["update_included"]),
                1,
            )
            self.assertEqual(
                len(artifact["population"]["update_alias_groups"][0][1]),
                2,
            )
            self.assertIn(
                "PHYSICAL_DUPLICATE",
                {
                    reason
                    for _episode, reason
                    in artifact["population"]["update_exclusions"]
                },
            )
            self.assertEqual(produced.proposed_parameters["x"], Decimal("1"))

    def test_conflicting_alias_is_durable_no_update_not_fabricated_move(self):
        with TemporaryDirectory() as directory:
            fixture = ProducerFixture(directory)
            fixture.seed_calibration()
            fixture.append_learning(
                task="update",
                feature="1",
                target="1",
                observation_id="conflict",
                label_available_at=datetime(
                    2026, 10, 8, tzinfo=timezone.utc
                ),
                episode_id="00000000-0000-0000-0000-000000000221",
                decision_time=DECISION,
            )
            fixture.append_learning(
                task="update",
                feature="1",
                target="2",
                observation_id="conflict",
                label_available_at=datetime(
                    2026, 10, 8, tzinfo=timezone.utc
                ),
                episode_id="00000000-0000-0000-0000-000000000222",
                decision_time=DECISION + timedelta(minutes=1),
            )
            checkpoint = fixture.publish_checkpoint()
            test_ref = fixture.publish_test_evidence()
            calibration = fixture.publish_calibration_evidence()
            produced = fixture.produce(
                checkpoint_ref=checkpoint,
                calibration_ref=calibration,
                envelope=fixture.envelope(checkpoint),
                config=fixture.config(test_ref),
            )
            self.assertEqual(produced.status, "NO_UPDATE")
            self.assertIsNone(produced.proposed_parameters)
            artifact = json.loads(produced.artifact_bytes)
            self.assertIn(
                "LEARNING.UPDATE_ALIAS_CONFLICT",
                artifact["reasons"],
            )


    def test_same_physical_observation_cannot_cross_calibration_and_update_aliases(self):
        with TemporaryDirectory() as directory:
            fixture = ProducerFixture(directory)
            fixture.seed_calibration(values=("0", "1", "2"))
            shared = fixture.publish_physical_evidence("shared-cross-population")
            physical_time = DECISION + timedelta(minutes=50)
            fixture.append_learning(
                task="calibration",
                feature="3",
                target="0",
                observation_id="calibration-alias",
                label_available_at=datetime(
                    2026, 10, 3, 12, 3, tzinfo=timezone.utc
                ),
                decision_time=physical_time,
                evidence_ref=shared,
            )
            fixture.append_learning(
                task="update",
                feature="3",
                target="1",
                observation_id="update-alias",
                label_available_at=datetime(
                    2026, 10, 8, tzinfo=timezone.utc
                ),
                decision_time=physical_time + timedelta(minutes=7),
                evidence_ref=shared,
            )
            checkpoint = fixture.publish_checkpoint()
            test_ref = fixture.publish_test_evidence()
            calibration = fixture.publish_calibration_evidence()
            produced = fixture.produce(
                checkpoint_ref=checkpoint,
                calibration_ref=calibration,
                envelope=fixture.envelope(checkpoint),
                config=fixture.config(test_ref),
            )
            self.assertEqual(produced.status, "NO_UPDATE")
            self.assertIsNone(produced.proposed_parameters)
            artifact = json.loads(produced.artifact_bytes)
            self.assertIn(
                "LEARNING.CROSS_POPULATION_CONTAMINATION",
                artifact["reasons"],
            )
            overlap = artifact["population"][
                "cross_population_physical_overlap"
            ]
            self.assertEqual(len(overlap), 1)
            self.assertTrue(overlap[0].startswith("sha256:"))

    def test_unregistered_physical_evidence_cannot_define_learning_population(self):
        with TemporaryDirectory() as directory:
            fixture = ProducerFixture(directory)
            fixture.seed_calibration(values=("0", "1", "2"))
            valid = fixture.publish_physical_evidence("registered-update")
            prefix, digest = valid.rsplit("sha256:", 1)
            forged = prefix + "sha256:" + ("0" if digest[0] != "0" else "1") + digest[1:]
            fixture.append_learning(
                task="update",
                feature="4",
                target="1",
                observation_id="forged-physical-ref",
                label_available_at=datetime(
                    2026, 10, 8, tzinfo=timezone.utc
                ),
                evidence_ref=forged,
            )
            checkpoint = fixture.publish_checkpoint()
            test_ref = fixture.publish_test_evidence()
            calibration = fixture.publish_calibration_evidence()
            produced = fixture.produce(
                checkpoint_ref=checkpoint,
                calibration_ref=calibration,
                envelope=fixture.envelope(checkpoint),
                config=fixture.config(test_ref),
            )
            self.assertEqual(produced.status, "NO_UPDATE")
            artifact = json.loads(produced.artifact_bytes)
            self.assertIn(
                "LEARNING.INSUFFICIENT_UPDATE_EVIDENCE",
                artifact["reasons"],
            )
            self.assertIn(
                "LEARNING_SCHEMA_INVALID",
                {
                    reason
                    for _episode, reason
                    in artifact["population"]["update_exclusions"]
                },
            )

    def test_insufficient_evidence_is_first_class_no_update(self):
        with TemporaryDirectory() as directory:
            fixture, checkpoint, test_ref, calibration, envelope = (
                self.ready_fixture(directory)
            )
            produced = fixture.produce(
                checkpoint_ref=checkpoint,
                calibration_ref=calibration,
                envelope=envelope,
                config=fixture.config(
                    test_ref,
                    min_update_episodes=2,
                ),
            )
            self.assertEqual(produced.status, "NO_UPDATE")
            self.assertIsNone(produced.proposed_parameters)
            artifact = json.loads(produced.artifact_bytes)
            self.assertIn(
                "LEARNING.INSUFFICIENT_UPDATE_EVIDENCE",
                artifact["reasons"],
            )

    def test_calibrated_drift_stops_proposal_before_online_gate(self):
        with TemporaryDirectory() as directory:
            fixture = ProducerFixture(directory)
            fixture.seed_calibration(values=("0", "0", "0", "0"))
            fixture.seed_update(feature="1", target="1")
            checkpoint = fixture.publish_checkpoint()
            test_ref = fixture.publish_test_evidence()
            calibration = fixture.publish_calibration_evidence()
            produced = fixture.produce(
                checkpoint_ref=checkpoint,
                calibration_ref=calibration,
                envelope=fixture.envelope(checkpoint),
                config=fixture.config(test_ref),
            )
            self.assertEqual(produced.status, "NO_UPDATE")
            self.assertEqual(produced.calibrated_threshold, Decimal("0"))
            self.assertEqual(produced.drift_score, Decimal("1"))
            self.assertIsNone(produced.proposed_parameters)
            artifact = json.loads(produced.artifact_bytes)
            self.assertIn(
                "LEARNING.CALIBRATED_DRIFT_LIMIT_EXCEEDED",
                artifact["reasons"],
            )

    def test_proposal_outside_existing_step_envelope_is_candidate_required(self):
        with TemporaryDirectory() as directory:
            fixture, checkpoint, test_ref, calibration, _envelope = (
                self.ready_fixture(directory, max_step="0.1")
            )
            produced = fixture.produce(
                checkpoint_ref=checkpoint,
                calibration_ref=calibration,
                envelope=fixture.envelope(
                    checkpoint,
                    max_step="0.1",
                ),
                config=fixture.config(test_ref),
            )
            self.assertEqual(produced.status, "CANDIDATE_REQUIRED")
            self.assertIsNotNone(produced.online_decision)
            self.assertEqual(
                produced.online_decision.status,
                "CANDIDATE_REQUIRED",
            )
            self.assertTrue(
                any(
                    "PARAMETER_STEP_EXCEEDED:x" in reason
                    for reason in produced.online_decision.reasons
                )
            )

    def test_compute_limit_is_no_update_and_records_estimated_usage(self):
        with TemporaryDirectory() as directory:
            fixture, checkpoint, test_ref, calibration, envelope = (
                self.ready_fixture(directory)
            )
            produced = fixture.produce(
                checkpoint_ref=checkpoint,
                calibration_ref=calibration,
                envelope=envelope,
                config=fixture.config(
                    test_ref,
                    max_compute_units="4",
                ),
            )
            self.assertEqual(produced.status, "NO_UPDATE")
            artifact = json.loads(produced.artifact_bytes)
            self.assertEqual(
                artifact["compute"]["estimated_compute_units"],
                "5",
            )
            self.assertIn(
                "LEARNING.PRODUCER_COMPUTE_BUDGET_EXCEEDED",
                artifact["reasons"],
            )

    def test_calibration_cutoff_cannot_touch_update_population(self):
        with TemporaryDirectory() as directory:
            fixture, checkpoint, test_ref, calibration, envelope = (
                self.ready_fixture(directory)
            )
            with self.assertRaisesRegex(
                ValueError,
                "strictly before update cutoff",
            ):
                fixture.produce(
                    checkpoint_ref=checkpoint,
                    calibration_ref=calibration,
                    envelope=envelope,
                    config=fixture.config(test_ref),
                    calibration_cutoff=UPDATE_CUTOFF,
                )

    def test_registered_calibration_population_root_mismatch_fails_closed(self):
        with TemporaryDirectory() as directory:
            fixture = ProducerFixture(directory)
            fixture.seed_calibration()
            fixture.seed_update()
            checkpoint = fixture.publish_checkpoint()
            test_ref = fixture.publish_test_evidence()
            bad_calibration = fixture.publish_calibration_evidence(
                population_root="sha256:" + "0" * 64
            )
            with self.assertRaisesRegex(
                ValueError,
                "does not bind the canonical calibration population",
            ):
                fixture.produce(
                    checkpoint_ref=checkpoint,
                    calibration_ref=bad_calibration,
                    envelope=fixture.envelope(checkpoint),
                    config=fixture.config(test_ref),
                )

    def test_test_evidence_must_bind_exact_source_sha(self):
        with TemporaryDirectory() as directory:
            fixture = ProducerFixture(directory)
            fixture.seed_calibration()
            fixture.seed_update()
            checkpoint = fixture.publish_checkpoint()
            wrong_test = fixture.publish_test_evidence(source_sha="b" * 40)
            calibration = fixture.publish_calibration_evidence()
            with self.assertRaisesRegex(
                ValueError,
                "exact producer source SHA",
            ):
                fixture.produce(
                    checkpoint_ref=checkpoint,
                    calibration_ref=calibration,
                    envelope=fixture.envelope(checkpoint),
                    config=fixture.config(wrong_test),
                )

    def test_publish_reuses_canonical_job_and_artifact_store(self):
        with TemporaryDirectory() as directory:
            fixture, checkpoint, test_ref, calibration, envelope = (
                self.ready_fixture(directory)
            )
            produced = fixture.produce(
                checkpoint_ref=checkpoint,
                calibration_ref=calibration,
                envelope=envelope,
                config=fixture.config(test_ref),
            )
            job, inserted = fixture.jobs.enqueue(
                kind="research.wp38_online_update",
                dedupe_key="wp38:" + produced.artifact_digest,
                input_hashes=[
                    "sha256:" + checkpoint.rsplit("@sha256:", 1)[1]
                ],
                resource_budget={"compute_units": 100},
                lease_requeueable=False,
                job_id="00000000-0000-0000-0000-000000000301",
                now=UPDATE_CUTOFF,
            )
            self.assertTrue(inserted)
            claimed = fixture.jobs.claim(
                "worker-wp38",
                now=UPDATE_CUTOFF,
                lease_seconds=60,
            )
            self.assertEqual(claimed["job_id"], job["job_id"])
            manifest, accepted = publish_online_update_result(
                job_store=fixture.jobs,
                job_id=job["job_id"],
                worker_id="worker-wp38",
                generation=int(claimed["generation"]),
                artifact_store=fixture.artifacts,
                produced=produced,
                now=UPDATE_CUTOFF,
            )
            self.assertTrue(accepted)
            self.assertEqual(
                fixture.artifacts.read_bytes(manifest["artifact_id"]),
                produced.artifact_bytes,
            )
            final = fixture.jobs.get(job["job_id"])
            self.assertEqual(final["state"], "SUCCEEDED")
            self.assertEqual(len(final["output_refs"]), 1)
            self.assertEqual(
                manifest["metadata"]["artifact_kind"],
                "RESEARCH_JOB_RESULT",
            )

    def test_later_correction_is_invisible_before_its_availability(self):
        with TemporaryDirectory() as directory:
            fixture = ProducerFixture(directory)
            fixture.seed_calibration()
            episode = fixture.append_learning(
                task="update",
                feature="1",
                target="1",
                observation_id="late-correction",
                label_available_at=datetime(
                    2026, 10, 8, tzinfo=timezone.utc
                ),
                episode_id="00000000-0000-0000-0000-000000000311",
            )
            future = UPDATE_CUTOFF + timedelta(days=1)
            evidence_ref = "artifact:future-correction"
            fixture.correction_times[evidence_ref] = future
            fixture.memory.append_correction(
                episode,
                correction_id="00000000-0000-0000-0000-000000000312",
                available_at=future,
                payload={
                    "supersedes_fields": ["learning"],
                    "learning": {
                        "observation_id": "late-correction",
                        "label_version": "label-v1",
                        "label_available_at": "2026-10-08T00:00:00Z",
                        "outcome_horizon_at": "2026-10-08T00:00:00Z",
                        "execution_reconciled_at": "2026-10-08T00:00:00Z",
                        "features": {"x": "1"},
                        "target": "9",
                    },
                    "evidence_ref": evidence_ref,
                },
            )
            checkpoint = fixture.publish_checkpoint()
            test_ref = fixture.publish_test_evidence()
            calibration = fixture.publish_calibration_evidence()
            produced = fixture.produce(
                checkpoint_ref=checkpoint,
                calibration_ref=calibration,
                envelope=fixture.envelope(checkpoint),
                config=fixture.config(test_ref),
            )
            self.assertEqual(produced.proposed_parameters["x"], Decimal("1"))
            artifact = json.loads(produced.artifact_bytes)
            self.assertEqual(
                artifact["population"]["update_included"][0][
                    "correction_hashes"
                ],
                [],
            )


if __name__ == "__main__":
    unittest.main()
