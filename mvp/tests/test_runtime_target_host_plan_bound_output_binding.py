from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from types import MappingProxyType
import unittest
from uuid import NAMESPACE_URL, uuid5

from mvp.autotrade_mvp import runtime_target_host_plan_bound_qualification as plan_bound
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.runtime_load_plan import declare_runtime_event_plan
from mvp.autotrade_mvp.runtime_target_host_composed_qualification import (
    AcceptedComposedRuntimeTargetHostQualification,
    RuntimeTargetHostCompositionError,
)
from mvp.autotrade_mvp.runtime_target_host_qualification import (
    AcceptedRuntimeTargetHostQualification,
)
from mvp.tests.test_runtime_target_host_plan_bound_qualification import (
    RELEASE_ID,
    RELEASE_SHA,
    _expected_event,
    _spec,
)


def _digest(label: str) -> str:
    return "sha256:" + sha256(label.encode("utf-8")).hexdigest()


def _artifact_id(label: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"wp65-output-binding:{label}"))


def _qualification(spec, *, workload_profile_hash: str):
    evidence_digests = {
        kind: _digest(f"evidence:{kind}")
        for kind in sorted(plan_bound._REQUIRED_ACCEPTED_EVIDENCE_KINDS)
    }
    payload_ids = {
        kind: _artifact_id(f"payload:{kind}")
        for kind in sorted(plan_bound._PROVENANCE_ACCEPTED_KINDS)
    }
    payload_digests = {
        kind: _digest(f"payload:{kind}")
        for kind in sorted(plan_bound._PROVENANCE_ACCEPTED_KINDS)
    }
    collectors = {
        kind: f"collector-{index}@1.0.0"
        for index, kind in enumerate(sorted(plan_bound._PROVENANCE_ACCEPTED_KINDS))
    }
    accepted = AcceptedRuntimeTargetHostQualification(
        attestation_id=_artifact_id("attestation"),
        attestation_digest=_digest("attestation"),
        source_sha=spec.release_sha,
        scenario_id=spec.scenario_id,
        spec_digest=spec.digest,
        configuration_hash=spec.configuration_hash,
        host_fingerprint=spec.host_fingerprint,
        workload_profile_hash=workload_profile_hash,
        journal_store_identity_digest=_digest("journal-store"),
        release_artifact_id=RELEASE_ID,
        release_artifact_sha256=RELEASE_SHA,
        binding_artifact_id=_artifact_id("binding"),
        binding_sha256=evidence_digests[plan_bound.BINDING_EVIDENCE_KIND],
        evidence_sha256_by_kind=evidence_digests,
        payload_artifact_id_by_kind=payload_ids,
        payload_sha256_by_kind=payload_digests,
        collector_by_kind=collectors,
    )
    return AcceptedComposedRuntimeTargetHostQualification(
        qualification=accepted,
        target_host_measurement_digest=_digest("measurement"),
        durable_financial_binding_digest=_digest("durable-financial-binding"),
        projection_sha256_by_kind={
            kind: payload_digests[kind]
            for kind in sorted(plan_bound._PROJECTION_ACCEPTED_KINDS)
        },
    )


def _detached(spec, *, workload_profile_hash: str):
    return plan_bound._PRODUCTION_ACCEPTANCE_SNAPSHOTTER(
        _qualification(spec, workload_profile_hash=workload_profile_hash)
    )


def _validate(value, spec, *, workload_profile_hash: str):
    return plan_bound._PRODUCTION_ACCEPTANCE_BINDING_VALIDATOR(
        value,
        expected_source_sha=spec.release_sha,
        expected_scenario_id=spec.scenario_id,
        expected_spec_digest=spec.digest,
        expected_configuration_hash=spec.configuration_hash,
        expected_host_fingerprint=spec.host_fingerprint,
        expected_workload_profile_hash=workload_profile_hash,
        expected_release_artifact_id=RELEASE_ID,
        expected_release_artifact_sha256=RELEASE_SHA,
    )


class RuntimeTargetHostPlanBoundOutputBindingTests(unittest.TestCase):
    def test_validator_accepts_canonical_detached_relationships(self) -> None:
        spec = _spec()
        workload = _digest("durable-plan")
        detached = _detached(spec, workload_profile_hash=workload)

        self.assertIs(_validate(detached, spec, workload_profile_hash=workload), detached)

    def test_validator_rejects_validly_formatted_identity_rebinding(self) -> None:
        spec = _spec()
        workload = _digest("durable-plan")
        detached = _detached(spec, workload_profile_hash=workload)
        object.__setattr__(
            detached.qualification,
            "scenario_id",
            "different-valid-scenario",
        )

        with self.assertRaisesRegex(
            RuntimeTargetHostCompositionError,
            "scenario id differs from captured authority",
        ):
            _validate(detached, spec, workload_profile_hash=workload)

    def test_validator_rejects_noncanonical_post_pass_identity(self) -> None:
        spec = _spec()
        workload = _digest("durable-plan")
        detached = _detached(spec, workload_profile_hash=workload)
        object.__setattr__(detached.qualification, "source_sha", "F" * 40)

        with self.assertRaisesRegex(
            RuntimeTargetHostCompositionError,
            "lowercase 40-character Git SHA",
        ):
            _validate(detached, spec, workload_profile_hash=workload)

    def test_validator_rejects_changed_evidence_key_set(self) -> None:
        spec = _spec()
        workload = _digest("durable-plan")
        detached = _detached(spec, workload_profile_hash=workload)
        object.__setattr__(
            detached.qualification,
            "evidence_sha256_by_kind",
            MappingProxyType({"FORGED": _digest("forged")}),
        )

        with self.assertRaisesRegex(
            RuntimeTargetHostCompositionError,
            "key set changed after canonical verification",
        ):
            _validate(detached, spec, workload_profile_hash=workload)

    def test_validator_rejects_binding_evidence_rebinding(self) -> None:
        spec = _spec()
        workload = _digest("durable-plan")
        detached = _detached(spec, workload_profile_hash=workload)
        evidence = dict(detached.qualification.evidence_sha256_by_kind)
        evidence[plan_bound.BINDING_EVIDENCE_KIND] = _digest("forged-binding")
        object.__setattr__(
            detached.qualification,
            "evidence_sha256_by_kind",
            MappingProxyType(evidence),
        )

        with self.assertRaisesRegex(
            RuntimeTargetHostCompositionError,
            "binding digest differs from accepted evidence map",
        ):
            _validate(detached, spec, workload_profile_hash=workload)

    def test_validator_rejects_projection_payload_rebinding(self) -> None:
        spec = _spec()
        workload = _digest("durable-plan")
        detached = _detached(spec, workload_profile_hash=workload)
        projections = dict(detached.projection_sha256_by_kind)
        selected = sorted(plan_bound._PROJECTION_ACCEPTED_KINDS)[0]
        projections[selected] = _digest("forged-projection")
        object.__setattr__(
            detached,
            "projection_sha256_by_kind",
            MappingProxyType(projections),
        )

        with self.assertRaisesRegex(
            RuntimeTargetHostCompositionError,
            "projection differs from retained payload",
        ):
            _validate(detached, spec, workload_profile_hash=workload)

    def test_validator_rejects_duplicate_payload_digest_authority(self) -> None:
        spec = _spec()
        workload = _digest("durable-plan")
        detached = _detached(spec, workload_profile_hash=workload)
        payloads = dict(detached.qualification.payload_sha256_by_kind)
        non_projection = sorted(
            plan_bound._PROVENANCE_ACCEPTED_KINDS
            - plan_bound._PROJECTION_ACCEPTED_KINDS
        )
        payloads[non_projection[1]] = payloads[non_projection[0]]
        object.__setattr__(
            detached.qualification,
            "payload_sha256_by_kind",
            MappingProxyType(payloads),
        )

        with self.assertRaisesRegex(
            RuntimeTargetHostCompositionError,
            "payload digests are not independent",
        ):
            _validate(detached, spec, workload_profile_hash=workload)

    def test_validator_rejects_payload_alias_with_top_level_evidence(self) -> None:
        spec = _spec()
        workload = _digest("durable-plan")
        detached = _detached(spec, workload_profile_hash=workload)
        payloads = dict(detached.qualification.payload_sha256_by_kind)
        selected_payload = sorted(
            plan_bound._PROVENANCE_ACCEPTED_KINDS
            - plan_bound._PROJECTION_ACCEPTED_KINDS
        )[0]
        selected_evidence = sorted(plan_bound._REQUIRED_ACCEPTED_EVIDENCE_KINDS)[0]
        payloads[selected_payload] = detached.qualification.evidence_sha256_by_kind[
            selected_evidence
        ]
        object.__setattr__(
            detached.qualification,
            "payload_sha256_by_kind",
            MappingProxyType(payloads),
        )

        with self.assertRaisesRegex(
            RuntimeTargetHostCompositionError,
            "payload bytes alias retained authority bytes",
        ):
            _validate(detached, spec, workload_profile_hash=workload)

    def test_production_like_builder_rejects_valid_composed_rebinding(self) -> None:
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            spec = _spec()
            plan = declare_runtime_event_plan(
                store,
                plan_id="output-binding-plan",
                spec=spec,
                expected_events=(_expected_event(),),
            )
            retained = _qualification(spec, workload_profile_hash=plan.digest)
            object.__setattr__(
                retained.qualification,
                "configuration_hash",
                _digest("different-valid-configuration"),
            )
            verifier = plan_bound._build_chronology_free_verifier(
                verify_composed=lambda *_args, **_kwargs: retained,
                acceptance_snapshotter=plan_bound._PRODUCTION_ACCEPTANCE_SNAPSHOTTER,
                acceptance_binding_validator=(
                    plan_bound._PRODUCTION_ACCEPTANCE_BINDING_VALIDATOR
                ),
            )

            with self.assertRaisesRegex(
                RuntimeTargetHostCompositionError,
                "configuration hash differs from captured authority",
            ):
                verifier(
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

    def test_lower_callback_cannot_rebind_frozen_spec_authority(self) -> None:
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            caller_spec = _spec()
            original_configuration = caller_spec.configuration_hash
            plan = declare_runtime_event_plan(
                store,
                plan_id="lower-mutates-spec-plan",
                spec=caller_spec,
                expected_events=(_expected_event(),),
            )
            retained = _qualification(
                caller_spec,
                workload_profile_hash=plan.digest,
            )
            forged_configuration = _digest("lower-mutated-configuration")

            def mutating_lower(*_args, **kwargs):
                lower_spec = kwargs["spec"]
                self.assertIsNot(lower_spec, caller_spec)
                object.__setattr__(
                    lower_spec,
                    "configuration_hash",
                    forged_configuration,
                )
                object.__setattr__(
                    retained.qualification,
                    "configuration_hash",
                    forged_configuration,
                )
                object.__setattr__(
                    retained.qualification,
                    "spec_digest",
                    lower_spec.digest,
                )
                return retained

            verifier = plan_bound._build_chronology_free_verifier(
                verify_composed=mutating_lower,
                acceptance_snapshotter=plan_bound._PRODUCTION_ACCEPTANCE_SNAPSHOTTER,
                acceptance_binding_validator=(
                    plan_bound._PRODUCTION_ACCEPTANCE_BINDING_VALIDATOR
                ),
            )

            with self.assertRaisesRegex(
                RuntimeTargetHostCompositionError,
                "spec digest differs from captured authority",
            ):
                verifier(
                    object(),
                    evidence_store=object(),
                    evidence_root=directory,
                    journal_store=store,
                    plan_id=plan.plan_id,
                    spec=caller_spec,
                    expected_release_artifact_id=RELEASE_ID,
                    expected_release_artifact_sha256=RELEASE_SHA,
                    campaign_plan=object(),
                    campaign_cut=object(),
                    measurement=object(),
                )

            self.assertEqual(caller_spec.configuration_hash, original_configuration)


if __name__ == "__main__":
    unittest.main()
