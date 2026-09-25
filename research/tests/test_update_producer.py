from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from research.autotrade_research.artifacts.store import ArtifactStore
from research.autotrade_research.jobs import ResearchJobStore
from research.autotrade_research.learning.online import (
    OnlineUpdateEnvelope,
    ParameterRule,
)
from research.autotrade_research.learning.update_producer import (
    OnlineUpdateRuntimeState,
    UpdateProducerConfig,
    produce_bounded_online_update,
    publish_online_update_result,
)
from research.autotrade_research.memory.episodes import ExperienceMemory


SOURCE_SHA = "a" * 40
FEATURE_SCHEMA = "sha256:" + "f" * 64
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
        self._episode_counter = 0

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
        evidence_refs=None,
    ):
        self._episode_counter += 1
        decision = decision_time or (
            DECISION + timedelta(minutes=self._episode_counter)
        )
        horizon = outcome_horizon_at or label_available_at
        reconciled = execution_reconciled_at or label_available_at
        refs = (
            ["artifact:episode-evidence"]
            if evidence_refs is None
            else list(evidence_refs)
        )
        payload = {
            "evidence_refs": refs,
            "intended_action": {"kind": "NO_TRADE"},
            "actual_execution": {"kind": "NO_TRADE"},
            "outcome": {"class": "NULL"},
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
        return self.memory.append_episode(
            decision_time=decision,
            information_cutoff=decision,
            task=task,
            regime="stable",
            instrument_family="equity",
            permission_class="research",
            payload=payload,
            episode_id=episode_id,
        )[0]

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

    def publish_checkpoint(self):
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
    ):
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
            granted_permissions=granted_permissions or {"research"},
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
            self.assertEqual(
                baseline.proposed_parameters,
                with_future.proposed_parameters,
            )
            artifact = json.loads(with_future.artifact_bytes)
            self.assertIn(
                "LABEL_NOT_CAUSALLY_MATURE",
                {
                    reason
                    for _episode, reason
                    in artifact["population"]["update_exclusions"]
                },
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
                "ALIAS_DUPLICATE",
                {
                    reason
                    for _episode, reason
                    in artifact["population"]["update_exclusions"]
                },
            )
            self.assertEqual(produced.proposed_parameters["x"], Decimal("1"))

    def test_cross_population_aliases_of_same_physical_observation_fail_closed(self):
        with TemporaryDirectory() as directory:
            fixture = ProducerFixture(directory)
            shared_decision = DECISION
            shared_refs = ("provider-evidence:shared-physical-observation",)
            shared_label = datetime(2026, 10, 3, tzinfo=timezone.utc)

            fixture.append_learning(
                task="calibration",
                feature="1",
                target="1",
                observation_id="calibration-alias",
                label_available_at=shared_label,
                episode_id="00000000-0000-0000-0000-000000000241",
                decision_time=shared_decision,
                evidence_refs=shared_refs,
            )
            fixture.append_learning(
                task="update",
                feature="1",
                target="1",
                observation_id="update-alias",
                label_available_at=shared_label,
                episode_id="00000000-0000-0000-0000-000000000242",
                decision_time=shared_decision,
                evidence_refs=shared_refs,
            )

            checkpoint = fixture.publish_checkpoint()
            test_ref = fixture.publish_test_evidence()
            calibration = fixture.publish_calibration_evidence()
            produced = fixture.produce(
                checkpoint_ref=checkpoint,
                calibration_ref=calibration,
                envelope=fixture.envelope(checkpoint),
                config=fixture.config(
                    test_ref,
                    min_calibration_episodes=1,
                ),
            )

            self.assertEqual(produced.status, "NO_UPDATE")
            self.assertIsNone(produced.proposed_parameters)
            artifact = json.loads(produced.artifact_bytes)
            self.assertIn(
                "LEARNING.CROSS_POPULATION_PHYSICAL_OVERLAP",
                artifact["reasons"],
            )
            overlap = artifact["population"][
                "cross_population_physical_overlap"
            ]
            self.assertEqual(len(overlap), 1)
            update_row = artifact["population"]["update_included"][0]
            calibration_row = artifact["population"]["calibration_included"][0]
            self.assertNotEqual(
                update_row["observation_id"],
                calibration_row["observation_id"],
            )
            self.assertEqual(
                update_row["physical_observation_id"],
                calibration_row["physical_observation_id"],
            )
            self.assertEqual(
                overlap,
                [update_row["physical_observation_id"]],
            )

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
