from __future__ import annotations

from hashlib import sha256
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from autotrade_runtime.artifacts import ArtifactStore
from mvp.autotrade_mvp.performance_qualification import RuntimeLoadObservation
from mvp.autotrade_mvp.runtime_load_campaign import (
    RuntimeLoadCampaignEvidence,
    serialize_runtime_load_campaign_evidence,
)
from mvp.autotrade_mvp.runtime_target_host_inventory import host_identity_fingerprint
from mvp.autotrade_mvp.runtime_target_host_measurement_evidence import (
    COLLECTOR_VERSION,
    INTERFERENCE_COLLECTOR_ID,
    INTERFERENCE_EVIDENCE_TYPE,
    INTERFERENCE_METRIC,
    RESOURCE_COLLECTOR_ID,
    STALENESS_COLLECTOR_ID,
    STALENESS_EVIDENCE_TYPE,
    STALENESS_METRIC,
    RuntimeTargetHostMeasurementEvidenceError,
    RuntimeTargetHostResourceEvidence,
    RuntimeTargetHostTimingEvidence,
    verify_runtime_target_host_measurement_evidence,
)
from mvp.autotrade_mvp.runtime_target_host_qualification import (
    CAMPAIGN_EVIDENCE_KIND,
    HOST_INVENTORY_EVIDENCE_KIND,
    INTERFERENCE_EVIDENCE_KIND,
    RESOURCE_EVIDENCE_KIND,
    STALENESS_EVIDENCE_KIND,
    AcceptedRuntimeTargetHostQualification,
)


SOURCE = "a" * 40
SPEC = "sha256:" + ("b" * 64)
CONFIG = "sha256:" + ("c" * 64)
WORKLOAD = "sha256:" + ("d" * 64)
JOURNAL = "sha256:" + ("e" * 64)
RELEASE_ID = "70000000-0000-4000-8000-000000000001"
RELEASE_SHA = "sha256:" + ("f" * 64)
HOST_IDENTITY = {
    "system": "Windows",
    "release": "11",
    "machine": "AMD64",
    "python_implementation": "CPython",
    "python_version": "3.12.11",
    "cpu_count": 8,
}
HOST = host_identity_fingerprint(HOST_IDENTITY)

_PAYLOAD_IDS = {
    CAMPAIGN_EVIDENCE_KIND: "71000000-0000-4000-8000-000000000001",
    STALENESS_EVIDENCE_KIND: "71000000-0000-4000-8000-000000000002",
    INTERFERENCE_EVIDENCE_KIND: "71000000-0000-4000-8000-000000000003",
    RESOURCE_EVIDENCE_KIND: "71000000-0000-4000-8000-000000000004",
    HOST_INVENTORY_EVIDENCE_KIND: "71000000-0000-4000-8000-000000000005",
}


def _sha(raw: bytes) -> str:
    return "sha256:" + sha256(raw).hexdigest()


def _campaign_raw(
    *,
    expected: int = 2,
    recovered: int = 2,
    staleness=(50, 70),
    interference=(20,),
    backlog: int = 0,
    declared_duration_us: int = 1_000,
    observed_duration_us: int = 900,
) -> bytes:
    observation = RuntimeLoadObservation.create(
        scenario_id="target-host-measured",
        spec_digest=SPEC,
        release_sha=SOURCE,
        configuration_hash=CONFIG,
        host_fingerprint=HOST,
        expected_financial_events=expected,
        recovered_financial_events=recovered,
        financial_latency_us=(100, 200),
        financial_staleness_us=staleness,
        research_interference_us=interference,
        reconnect_backlog_remaining=backlog,
        declared_duration_us=declared_duration_us,
        observed_duration_us=observed_duration_us,
    )
    recovered_ids = tuple(f"financial-{index}" for index in range(1, recovered + 1))
    recovered_sequences = tuple(range(11, 11 + recovered))
    evidence = RuntimeLoadCampaignEvidence(
        observation=observation,
        journal_sequence_before=10,
        journal_sequence_after=10 + recovered,
        recovered_event_ids=recovered_ids,
        recovered_journal_sequences=recovered_sequences,
        host_identity=HOST_IDENTITY,
    )
    return serialize_runtime_load_campaign_evidence(evidence)


def _staleness_raw(samples=(50, 70)) -> bytes:
    return RuntimeTargetHostTimingEvidence(
        evidence_type=STALENESS_EVIDENCE_TYPE,
        metric=STALENESS_METRIC,
        samples_us=tuple(samples),
    ).canonical_bytes()


def _interference_raw(samples=(20,)) -> bytes:
    return RuntimeTargetHostTimingEvidence(
        evidence_type=INTERFERENCE_EVIDENCE_TYPE,
        metric=INTERFERENCE_METRIC,
        samples_us=tuple(samples),
    ).canonical_bytes()


def _resource_raw(
    *,
    expected: int = 2,
    recovered: int = 2,
    backlog: int = 0,
    declared_duration_us: int = 1_000,
    observed_duration_us: int = 900,
) -> bytes:
    return RuntimeTargetHostResourceEvidence(
        expected_financial_events=expected,
        recovered_financial_events=recovered,
        reconnect_backlog_remaining=backlog,
        declared_duration_us=declared_duration_us,
        observed_duration_us=observed_duration_us,
    ).canonical_bytes()


def _material(
    *,
    campaign_raw: bytes | None = None,
    staleness_raw: bytes | None = None,
    interference_raw: bytes | None = None,
    resource_raw: bytes | None = None,
    collector_overrides=None,
):
    raw_by_kind = {
        CAMPAIGN_EVIDENCE_KIND: campaign_raw or _campaign_raw(),
        STALENESS_EVIDENCE_KIND: staleness_raw or _staleness_raw(),
        INTERFERENCE_EVIDENCE_KIND: interference_raw or _interference_raw(),
        RESOURCE_EVIDENCE_KIND: resource_raw or _resource_raw(),
        HOST_INVENTORY_EVIDENCE_KIND: b"unused-by-semantic-verifier",
    }
    raw_by_id = {
        _PAYLOAD_IDS[kind]: raw
        for kind, raw in raw_by_kind.items()
    }
    collectors = {
        CAMPAIGN_EVIDENCE_KIND: "autotrade-runtime-load-campaign@1.0.0",
        STALENESS_EVIDENCE_KIND: f"{STALENESS_COLLECTOR_ID}@{COLLECTOR_VERSION}",
        INTERFERENCE_EVIDENCE_KIND: f"{INTERFERENCE_COLLECTOR_ID}@{COLLECTOR_VERSION}",
        RESOURCE_EVIDENCE_KIND: f"{RESOURCE_COLLECTOR_ID}@{COLLECTOR_VERSION}",
        HOST_INVENTORY_EVIDENCE_KIND: "autotrade-runtime-target-host-inventory@1.0.0",
    }
    collectors.update(collector_overrides or {})
    accepted = AcceptedRuntimeTargetHostQualification(
        attestation_id="72000000-0000-4000-8000-000000000001",
        attestation_digest="sha256:" + ("1" * 64),
        source_sha=SOURCE,
        scenario_id="target-host-measured",
        spec_digest=SPEC,
        configuration_hash=CONFIG,
        host_fingerprint=HOST,
        workload_profile_hash=WORKLOAD,
        journal_store_identity_digest=JOURNAL,
        release_artifact_id=RELEASE_ID,
        release_artifact_sha256=RELEASE_SHA,
        binding_artifact_id="72000000-0000-4000-8000-000000000002",
        binding_sha256="sha256:" + ("2" * 64),
        evidence_sha256_by_kind={},
        payload_artifact_id_by_kind=dict(_PAYLOAD_IDS),
        payload_sha256_by_kind={
            kind: _sha(raw) for kind, raw in raw_by_kind.items()
        },
        collector_by_kind=collectors,
    )
    return accepted, raw_by_id


class RuntimeTargetHostMeasurementEvidenceTests(unittest.TestCase):
    def _verify(self, accepted, raw_by_id):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(f"{directory}/store")

            def reader(artifact_id):
                if artifact_id not in raw_by_id:
                    raise FileNotFoundError(artifact_id)
                return {}, raw_by_id[artifact_id]

            with patch(
                "mvp.autotrade_mvp.runtime_target_host_measurement_evidence."
                "trusted_authenticated_reader",
                return_value=reader,
            ):
                return verify_runtime_target_host_measurement_evidence(
                    accepted,
                    evidence_store=store,
                    evidence_root=directory,
                )

    def test_matching_retained_measurements_reconcile_to_campaign(self) -> None:
        accepted, raw_by_id = _material()
        self.assertIs(self._verify(accepted, raw_by_id), accepted)

    def test_staleness_series_must_equal_campaign_observation(self) -> None:
        accepted, raw_by_id = _material(staleness_raw=_staleness_raw((50, 71)))
        with self.assertRaisesRegex(
            RuntimeTargetHostMeasurementEvidenceError,
            "staleness samples conflict",
        ):
            self._verify(accepted, raw_by_id)

    def test_interference_series_must_equal_campaign_observation(self) -> None:
        accepted, raw_by_id = _material(
            interference_raw=_interference_raw((21,)),
        )
        with self.assertRaisesRegex(
            RuntimeTargetHostMeasurementEvidenceError,
            "interference samples conflict",
        ):
            self._verify(accepted, raw_by_id)

    def test_resource_pressure_facts_must_equal_campaign_observation(self) -> None:
        accepted, raw_by_id = _material(
            resource_raw=_resource_raw(observed_duration_us=901),
        )
        with self.assertRaisesRegex(
            RuntimeTargetHostMeasurementEvidenceError,
            "resource/pressure evidence conflicts",
        ):
            self._verify(accepted, raw_by_id)

    def test_noncanonical_measurement_bytes_are_rejected(self) -> None:
        accepted, raw_by_id = _material(
            staleness_raw=_staleness_raw() + b"\n",
        )
        with self.assertRaisesRegex(
            RuntimeTargetHostMeasurementEvidenceError,
            "bytes are not canonical JSON",
        ):
            self._verify(accepted, raw_by_id)

    def test_measurement_collector_identity_is_not_caller_selectable(self) -> None:
        accepted, raw_by_id = _material(
            collector_overrides={
                INTERFERENCE_EVIDENCE_KIND: "caller-selected@9.9.9",
            }
        )
        with self.assertRaisesRegex(
            RuntimeTargetHostMeasurementEvidenceError,
            "collector is non-canonical.*INTERFERENCE",
        ):
            self._verify(accepted, raw_by_id)

    def test_terminal_semantics_reject_financial_event_loss(self) -> None:
        campaign = _campaign_raw(expected=2, recovered=1)
        resources = _resource_raw(expected=2, recovered=1)
        accepted, raw_by_id = _material(
            campaign_raw=campaign,
            resource_raw=resources,
        )
        with self.assertRaisesRegex(
            RuntimeTargetHostMeasurementEvidenceError,
            "financial event loss",
        ):
            self._verify(accepted, raw_by_id)

    def test_terminal_semantics_reject_reconnect_backlog(self) -> None:
        campaign = _campaign_raw(backlog=1)
        resources = _resource_raw(backlog=1)
        accepted, raw_by_id = _material(
            campaign_raw=campaign,
            resource_raw=resources,
        )
        with self.assertRaisesRegex(
            RuntimeTargetHostMeasurementEvidenceError,
            "reconnect backlog",
        ):
            self._verify(accepted, raw_by_id)

    def test_retained_measurement_digest_is_rechecked(self) -> None:
        accepted, raw_by_id = _material()
        raw_by_id[_PAYLOAD_IDS[RESOURCE_EVIDENCE_KIND]] = b"tampered"
        with self.assertRaisesRegex(
            RuntimeTargetHostMeasurementEvidenceError,
            "payload digest conflicts.*RESOURCES",
        ):
            self._verify(accepted, raw_by_id)


if __name__ == "__main__":
    unittest.main()
