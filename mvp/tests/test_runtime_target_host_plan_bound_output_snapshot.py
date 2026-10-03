from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from types import MappingProxyType
import unittest
from unittest.mock import Mock
from uuid import NAMESPACE_URL, uuid5

from mvp.autotrade_mvp import runtime_target_host_plan_bound_qualification as plan_bound
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.runtime_load_plan import declare_runtime_event_plan
from mvp.autotrade_mvp.runtime_target_host_composed_qualification import (
    RuntimeTargetHostCompositionError,
)
from mvp.autotrade_mvp.runtime_target_host_measurement import (
    ResourceTargetHostSample,
    TargetHostMeasurementArtifact,
)
from mvp.tests.test_runtime_target_host_chronology_bound_qualification import (
    RuntimeTargetHostChronologyBoundTests,
)
from mvp.tests.test_runtime_target_host_plan_bound_qualification import (
    RELEASE_ID,
    RELEASE_SHA,
    _expected_event,
    _spec,
)


def _qualification():
    retained = RuntimeTargetHostChronologyBoundTests._qualification()
    accepted = retained.qualification
    evidence_kinds = sorted(plan_bound._REQUIRED_ACCEPTED_EVIDENCE_KINDS)
    provenance_kinds = sorted(plan_bound._PROVENANCE_ACCEPTED_KINDS)

    spare_evidence_chars = iter("56789")
    evidence_digests = {}
    for kind in evidence_kinds:
        if kind == plan_bound.BINDING_EVIDENCE_KIND:
            evidence_digests[kind] = accepted.binding_sha256
        else:
            evidence_digests[kind] = "sha256:" + next(spare_evidence_chars) * 64
    payload_chars = iter("abef0")
    payload_digests = {
        kind: "sha256:" + next(payload_chars) * 64 for kind in provenance_kinds
    }

    object.__setattr__(
        accepted,
        "evidence_sha256_by_kind",
        MappingProxyType(evidence_digests),
    )
    object.__setattr__(
        accepted,
        "payload_artifact_id_by_kind",
        MappingProxyType(
            {
                kind: str(uuid5(NAMESPACE_URL, f"snapshot-payload-{kind}"))
                for kind in provenance_kinds
            }
        ),
    )
    object.__setattr__(
        accepted,
        "payload_sha256_by_kind",
        MappingProxyType(payload_digests),
    )
    object.__setattr__(
        accepted,
        "collector_by_kind",
        MappingProxyType(
            {kind: f"collector-{index}@1.0.0" for index, kind in enumerate(provenance_kinds)}
        ),
    )
    object.__setattr__(
        retained,
        "projection_sha256_by_kind",
        MappingProxyType(
            {
                kind: payload_digests[kind]
                for kind in sorted(plan_bound._PROJECTION_ACCEPTED_KINDS)
            }
        ),
    )
    return retained


def _measurement(spec, *, workload_profile_hash: str) -> TargetHostMeasurementArtifact:
    return TargetHostMeasurementArtifact(
        source_sha=spec.release_sha,
        release_artifact_id=RELEASE_ID,
        release_artifact_sha256=RELEASE_SHA,
        scenario_id=spec.scenario_id,
        spec_digest=spec.digest,
        configuration_hash=spec.configuration_hash,
        host_fingerprint=spec.host_fingerprint,
        workload_profile_hash=workload_profile_hash,
        plan_digest=workload_profile_hash,
        journal_taxonomy_digest="sha256:" + "1" * 64,
        journal_store_identity_digest="sha256:" + "e" * 64,
        start_journal_sequence=0,
        end_journal_sequence=0,
        monotonic_clock_id="python-time.monotonic_ns",
        staleness_basis="same-host-monotonic",
        research_interference_basis="same-host-monotonic",
        financial_samples=(),
        research_samples=(),
        resource_samples=(
            ResourceTargetHostSample(
                sample_id="snapshot-resource-1",
                monotonic_ns=0,
                phase="qualification",
                metrics={"rss_bytes": 1},
            ),
        ),
    )


def _qualification_for_authority(spec, measurement):
    retained = _qualification()
    accepted = retained.qualification
    for field in (
        "source_sha",
        "scenario_id",
        "spec_digest",
        "configuration_hash",
        "host_fingerprint",
        "workload_profile_hash",
        "journal_store_identity_digest",
        "release_artifact_id",
        "release_artifact_sha256",
    ):
        object.__setattr__(accepted, field, getattr(measurement, field))
    object.__setattr__(retained, "target_host_measurement_digest", measurement.digest)
    return retained


class RuntimeTargetHostPlanBoundOutputSnapshotTests(unittest.TestCase):
    def test_snapshot_detaches_composed_and_nested_acceptance(self) -> None:
        retained = _qualification()
        detached = plan_bound._PRODUCTION_ACCEPTANCE_SNAPSHOTTER(retained)

        self.assertIsNot(detached, retained)
        self.assertIsNot(detached.qualification, retained.qualification)
        self.assertEqual(detached.qualification.source_sha, retained.qualification.source_sha)
        self.assertEqual(
            detached.target_host_measurement_digest,
            retained.target_host_measurement_digest,
        )

        object.__setattr__(retained.qualification, "source_sha", "f" * 40)
        object.__setattr__(
            retained,
            "target_host_measurement_digest",
            "sha256:" + "0" * 64,
        )

        self.assertEqual(detached.qualification.source_sha, "a" * 40)
        self.assertEqual(
            detached.target_host_measurement_digest,
            "sha256:" + "3" * 64,
        )

    def test_snapshot_rejects_mutable_mapping_substitution(self) -> None:
        retained = _qualification()
        object.__setattr__(
            retained.qualification,
            "evidence_sha256_by_kind",
            {"RUNTIME_TARGET_HOST_BINDING": "sha256:" + "5" * 64},
        )

        with self.assertRaisesRegex(
            RuntimeTargetHostCompositionError,
            "exact immutable mapping state",
        ):
            plan_bound._PRODUCTION_ACCEPTANCE_SNAPSHOTTER(retained)

    def test_snapshot_rejects_changed_evidence_key_set(self) -> None:
        retained = _qualification()
        object.__setattr__(
            retained.qualification,
            "evidence_sha256_by_kind",
            MappingProxyType({"FORGED": "sha256:" + "5" * 64}),
        )

        with self.assertRaisesRegex(
            RuntimeTargetHostCompositionError,
            "key set changed after canonical verification",
        ):
            plan_bound._PRODUCTION_ACCEPTANCE_SNAPSHOTTER(retained)

    def test_snapshot_rejects_post_pass_noncanonical_authority_text(self) -> None:
        retained = _qualification()
        object.__setattr__(retained.qualification, "source_sha", "F" * 40)

        with self.assertRaisesRegex(
            RuntimeTargetHostCompositionError,
            "lowercase 40-character Git SHA",
        ):
            plan_bound._PRODUCTION_ACCEPTANCE_SNAPSHOTTER(retained)

        retained = _qualification()
        object.__setattr__(retained.qualification, "attestation_digest", "forged")
        with self.assertRaisesRegex(
            RuntimeTargetHostCompositionError,
            "canonical sha256 text",
        ):
            plan_bound._PRODUCTION_ACCEPTANCE_SNAPSHOTTER(retained)

    def test_snapshot_rejects_noncanonical_nested_acceptance_type(self) -> None:
        retained = _qualification()
        object.__setattr__(retained, "qualification", object())

        with self.assertRaisesRegex(
            RuntimeTargetHostCompositionError,
            "non-canonical signed acceptance",
        ):
            plan_bound._PRODUCTION_ACCEPTANCE_SNAPSHOTTER(retained)

    def test_snapshot_detaches_external_mapping_proxy_backing(self) -> None:
        retained = _qualification()
        backing = dict(retained.qualification.evidence_sha256_by_kind)
        selected_kind = sorted(backing)[0]
        original_digest = backing[selected_kind]
        object.__setattr__(
            retained.qualification,
            "evidence_sha256_by_kind",
            MappingProxyType(backing),
        )

        detached = plan_bound._PRODUCTION_ACCEPTANCE_SNAPSHOTTER(retained)
        backing[selected_kind] = "sha256:" + "f" * 64

        self.assertEqual(
            detached.qualification.evidence_sha256_by_kind[selected_kind],
            original_digest,
        )

    def test_verifier_snapshots_before_post_verification_plan_revalidation(self) -> None:
        retained = _qualification()
        composed = Mock(return_value=retained)
        original_loader = plan_bound.load_declared_runtime_event_plan
        load_count = 0

        def mutating_loader(current_store, *, plan_id, spec):
            nonlocal load_count
            load_count += 1
            if load_count == 2:
                object.__setattr__(retained.qualification, "source_sha", "f" * 40)
            return original_loader(current_store, plan_id=plan_id, spec=spec)

        verifier = plan_bound._build_chronology_free_verifier(
            load_declared_plan=mutating_loader,
            verify_composed=composed,
            acceptance_snapshotter=plan_bound._PRODUCTION_ACCEPTANCE_SNAPSHOTTER,
        )

        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            spec = _spec()
            plan = declare_runtime_event_plan(
                store,
                plan_id="snapshot-plan",
                spec=spec,
                expected_events=(_expected_event(),),
            )
            result = verifier(
                object(),
                evidence_store=object(),
                evidence_root=directory,
                journal_store=store,
                plan_id=plan.plan_id,
                spec=spec,
                expected_release_artifact_id=RELEASE_ID,
                expected_release_artifact_sha256=RELEASE_SHA,
                campaign_plan=object(),
                campaign_cut=object(),
                measurement=object(),
            )

        self.assertEqual(load_count, 2)
        self.assertIsNot(result, retained)
        self.assertIsNot(result.qualification, retained.qualification)
        self.assertEqual(result.qualification.source_sha, "a" * 40)
        self.assertEqual(retained.qualification.source_sha, "f" * 40)
        composed.assert_called_once()

    def test_snapshot_rejects_mutable_projection_map_substitution(self) -> None:
        retained = _qualification()
        object.__setattr__(retained, "projection_sha256_by_kind", {})

        with self.assertRaisesRegex(
            RuntimeTargetHostCompositionError,
            "exact immutable mapping state",
        ):
            plan_bound._PRODUCTION_ACCEPTANCE_SNAPSHOTTER(retained)

    def test_snapshot_rejects_changed_projection_key_set(self) -> None:
        retained = _qualification()
        object.__setattr__(
            retained,
            "projection_sha256_by_kind",
            MappingProxyType({"FORGED": "sha256:" + "5" * 64}),
        )

        with self.assertRaisesRegex(
            RuntimeTargetHostCompositionError,
            "key set changed after canonical verification",
        ):
            plan_bound._PRODUCTION_ACCEPTANCE_SNAPSHOTTER(retained)

    def test_binding_validator_accepts_captured_authority_relationships(self) -> None:
        spec = _spec()
        workload = "sha256:" + "4" * 64
        measurement = _measurement(spec, workload_profile_hash=workload)
        detached = plan_bound._PRODUCTION_ACCEPTANCE_SNAPSHOTTER(
            _qualification_for_authority(spec, measurement)
        )

        result = plan_bound._PRODUCTION_ACCEPTANCE_BINDING_VALIDATOR(
            detached,
            spec=spec,
            durable_plan_digest=workload,
            measurement=measurement,
            expected_release_artifact_id=RELEASE_ID,
            expected_release_artifact_sha256=RELEASE_SHA,
        )

        self.assertIs(result, detached)

    def test_binding_validator_rejects_validly_formatted_identity_rebinding(self) -> None:
        spec = _spec()
        workload = "sha256:" + "4" * 64
        measurement = _measurement(spec, workload_profile_hash=workload)
        detached = plan_bound._PRODUCTION_ACCEPTANCE_SNAPSHOTTER(
            _qualification_for_authority(spec, measurement)
        )
        object.__setattr__(detached.qualification, "scenario_id", "forged-scenario")

        with self.assertRaisesRegex(
            RuntimeTargetHostCompositionError,
            "scenario id differs from captured authority",
        ):
            plan_bound._PRODUCTION_ACCEPTANCE_BINDING_VALIDATOR(
                detached,
                spec=spec,
                durable_plan_digest=workload,
                measurement=measurement,
                expected_release_artifact_id=RELEASE_ID,
                expected_release_artifact_sha256=RELEASE_SHA,
            )

    def test_binding_validator_rejects_projection_payload_rebinding(self) -> None:
        spec = _spec()
        workload = "sha256:" + "4" * 64
        measurement = _measurement(spec, workload_profile_hash=workload)
        retained = _qualification_for_authority(spec, measurement)
        projections = dict(retained.projection_sha256_by_kind)
        selected = sorted(plan_bound._PROJECTION_ACCEPTED_KINDS)[0]
        projections[selected] = "sha256:" + "3" * 64
        object.__setattr__(
            retained,
            "projection_sha256_by_kind",
            MappingProxyType(projections),
        )
        detached = plan_bound._PRODUCTION_ACCEPTANCE_SNAPSHOTTER(retained)

        with self.assertRaisesRegex(
            RuntimeTargetHostCompositionError,
            "projection differs from retained payload",
        ):
            plan_bound._PRODUCTION_ACCEPTANCE_BINDING_VALIDATOR(
                detached,
                spec=spec,
                durable_plan_digest=workload,
                measurement=measurement,
                expected_release_artifact_id=RELEASE_ID,
                expected_release_artifact_sha256=RELEASE_SHA,
            )

    def test_binding_validator_rejects_binding_evidence_digest_rebinding(self) -> None:
        spec = _spec()
        workload = "sha256:" + "4" * 64
        measurement = _measurement(spec, workload_profile_hash=workload)
        retained = _qualification_for_authority(spec, measurement)
        evidence = dict(retained.qualification.evidence_sha256_by_kind)
        evidence[plan_bound.BINDING_EVIDENCE_KIND] = "sha256:" + "3" * 64
        object.__setattr__(
            retained.qualification,
            "evidence_sha256_by_kind",
            MappingProxyType(evidence),
        )
        detached = plan_bound._PRODUCTION_ACCEPTANCE_SNAPSHOTTER(retained)

        with self.assertRaisesRegex(
            RuntimeTargetHostCompositionError,
            "binding digest differs from accepted evidence map",
        ):
            plan_bound._PRODUCTION_ACCEPTANCE_BINDING_VALIDATOR(
                detached,
                spec=spec,
                durable_plan_digest=workload,
                measurement=measurement,
                expected_release_artifact_id=RELEASE_ID,
                expected_release_artifact_sha256=RELEASE_SHA,
            )

    def test_binding_validator_rejects_duplicate_payload_digest_authority(self) -> None:
        spec = _spec()
        workload = "sha256:" + "4" * 64
        measurement = _measurement(spec, workload_profile_hash=workload)
        retained = _qualification_for_authority(spec, measurement)
        payload = dict(retained.qualification.payload_sha256_by_kind)
        non_projection = sorted(
            plan_bound._PROVENANCE_ACCEPTED_KINDS - plan_bound._PROJECTION_ACCEPTED_KINDS
        )
        payload[non_projection[1]] = payload[non_projection[0]]
        object.__setattr__(
            retained.qualification,
            "payload_sha256_by_kind",
            MappingProxyType(payload),
        )
        detached = plan_bound._PRODUCTION_ACCEPTANCE_SNAPSHOTTER(retained)

        with self.assertRaisesRegex(
            RuntimeTargetHostCompositionError,
            "payload digests are not independent",
        ):
            plan_bound._PRODUCTION_ACCEPTANCE_BINDING_VALIDATOR(
                detached,
                spec=spec,
                durable_plan_digest=workload,
                measurement=measurement,
                expected_release_artifact_id=RELEASE_ID,
                expected_release_artifact_sha256=RELEASE_SHA,
            )

    def test_production_builder_snapshots_measurement_before_composed_callback(self) -> None:
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            spec = _spec()
            plan = declare_runtime_event_plan(
                store,
                plan_id="measurement-snapshot-plan",
                spec=spec,
                expected_events=(_expected_event(),),
            )
            measurement = _measurement(spec, workload_profile_hash=plan.digest)
            retained = _qualification_for_authority(spec, measurement)

            def mutate_caller_measurement(*_args, **kwargs):
                self.assertIsNot(kwargs["measurement"], measurement)
                object.__setattr__(measurement, "scenario_id", "forged-after-snapshot")
                return retained

            verifier = plan_bound._build_chronology_free_verifier(
                verify_composed=mutate_caller_measurement,
                snapshot_measurement=plan_bound.snapshot_target_host_measurement,
                acceptance_snapshotter=plan_bound._PRODUCTION_ACCEPTANCE_SNAPSHOTTER,
                acceptance_binding_validator=(
                    plan_bound._PRODUCTION_ACCEPTANCE_BINDING_VALIDATOR
                ),
            )
            result = verifier(
                object(),
                evidence_store=object(),
                evidence_root=directory,
                journal_store=store,
                plan_id=plan.plan_id,
                spec=spec,
                expected_release_artifact_id=RELEASE_ID,
                expected_release_artifact_sha256=RELEASE_SHA,
                campaign_plan=object(),
                campaign_cut=object(),
                measurement=measurement,
            )

        self.assertEqual(result.qualification.scenario_id, spec.scenario_id)
        self.assertEqual(measurement.scenario_id, "forged-after-snapshot")


if __name__ == "__main__":
    unittest.main()
