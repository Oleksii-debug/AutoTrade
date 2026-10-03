"""Compose signed WP-65 qualification with canonical raw target-host measurement truth.

The signed target-host profile authenticates retained payloads but intentionally
owns no target-host measurement collector.  The raw measurement lineage owns
recomputable financial staleness, research-interference and resource samples but
owns no signer.  This adapter joins those authorities without letting either
side self-assert the other.

Three domain-separated canonical projections are derived from one validated
``TargetHostMeasurementArtifact``.  A signed PASS is composition-eligible only
when the already authenticated raw payload digest for each measurement family is
exactly the digest of its projection.  The projections are intentionally
separate payloads so the signed profile's raw-payload independence invariant is
preserved.

This module does not create a signer, trust root, release authority, budget
evaluator, provider/PAPER/LIVE authority, profitability claim, economic edge or
trading authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from types import MappingProxyType
from typing import Mapping

from autotrade_runtime.artifacts import ArtifactStore

from .performance_qualification import RuntimeBudgetSpec
from .persistence import JournalStore
from .qualification_attestation import SignedQualificationAttestation
from .runtime_load_qualification import RuntimeCampaignCut, RuntimeCampaignPlan
from .runtime_target_host_durable_financial import (
    DurableTargetHostFinancialBinding,
    bind_release_bound_durable_financial_latency_to_target_host_measurement,
)
from .runtime_target_host_measurement import TargetHostMeasurementArtifact
from .runtime_target_host_qualification import (
    AcceptedRuntimeTargetHostQualification,
    INTERFERENCE_EVIDENCE_KIND,
    RESOURCE_EVIDENCE_KIND,
    STALENESS_EVIDENCE_KIND,
    verify_runtime_target_host_qualification,
)


PROJECTION_SCHEMA_VERSION = "1.0.0"
_MEASUREMENT_PROJECTION_KINDS = frozenset(
    {
        STALENESS_EVIDENCE_KIND,
        INTERFERENCE_EVIDENCE_KIND,
        RESOURCE_EVIDENCE_KIND,
    }
)


class RuntimeTargetHostCompositionError(ValueError):
    """Raised when signed and raw target-host authorities do not compose."""


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _sha256(raw: bytes) -> str:
    return "sha256:" + sha256(raw).hexdigest()


def _snapshot_measurement(
    measurement: TargetHostMeasurementArtifact,
) -> TargetHostMeasurementArtifact:
    if type(measurement) is not TargetHostMeasurementArtifact:
        raise TypeError("measurement must be exact TargetHostMeasurementArtifact")
    return TargetHostMeasurementArtifact.parse(measurement.canonical_bytes())


def _projection_identity(
    measurement: TargetHostMeasurementArtifact,
    *,
    evidence_kind: str,
) -> dict[str, object]:
    return {
        "configuration_hash": measurement.configuration_hash,
        "end_journal_sequence": measurement.end_journal_sequence,
        "evidence_kind": evidence_kind,
        "host_fingerprint": measurement.host_fingerprint,
        "journal_store_identity_digest": measurement.journal_store_identity_digest,
        "journal_taxonomy_digest": measurement.journal_taxonomy_digest,
        "measurement_method_id": measurement.measurement_method_id,
        "measurement_method_version": measurement.measurement_method_version,
        "monotonic_clock_id": measurement.monotonic_clock_id,
        "plan_digest": measurement.plan_digest,
        "projection_schema_version": PROJECTION_SCHEMA_VERSION,
        "release_artifact_id": measurement.release_artifact_id,
        "release_artifact_sha256": measurement.release_artifact_sha256,
        "scenario_id": measurement.scenario_id,
        "source_sha": measurement.source_sha,
        "spec_digest": measurement.spec_digest,
        "start_journal_sequence": measurement.start_journal_sequence,
        "target_host_measurement_digest": measurement.digest,
        "workload_profile_hash": measurement.workload_profile_hash,
    }


def target_host_measurement_projection_bytes(
    measurement: TargetHostMeasurementArtifact,
    *,
    evidence_kind: str,
) -> bytes:
    """Return the canonical signed-profile raw payload for one measurement family."""

    measurement = _snapshot_measurement(measurement)
    if type(evidence_kind) is not str or evidence_kind not in _MEASUREMENT_PROJECTION_KINDS:
        raise RuntimeTargetHostCompositionError(
            "evidence_kind must be a canonical target-host measurement projection kind"
        )

    payload = _projection_identity(measurement, evidence_kind=evidence_kind)
    if evidence_kind == STALENESS_EVIDENCE_KIND:
        payload["basis"] = measurement.staleness_basis
        payload["samples"] = [
            {
                "event_id": sample.event_id,
                "journal_sequence": sample.journal_sequence,
                "sample_id": sample.sample_id,
                "staleness_observed_monotonic_ns": sample.staleness_observed_monotonic_ns,
                "staleness_source_monotonic_ns": sample.staleness_source_monotonic_ns,
                "staleness_us": sample.staleness_us,
            }
            for sample in measurement.financial_samples
        ]
    elif evidence_kind == INTERFERENCE_EVIDENCE_KIND:
        payload["basis"] = measurement.research_interference_basis
        payload["samples"] = [
            sample.canonical_payload() for sample in measurement.research_samples
        ]
    else:
        payload["samples"] = [
            sample.canonical_payload() for sample in measurement.resource_samples
        ]
    return _canonical_json(payload)


def target_host_measurement_projection_digests(
    measurement: TargetHostMeasurementArtifact,
) -> Mapping[str, str]:
    """Return immutable expected raw-payload digests for the three signed families."""

    measurement = _snapshot_measurement(measurement)
    return MappingProxyType(
        {
            kind: _sha256(
                target_host_measurement_projection_bytes(
                    measurement,
                    evidence_kind=kind,
                )
            )
            for kind in sorted(_MEASUREMENT_PROJECTION_KINDS)
        }
    )


@dataclass(frozen=True, slots=True)
class AcceptedComposedRuntimeTargetHostQualification:
    """Terminal composition result retaining both independent authority digests."""

    qualification: AcceptedRuntimeTargetHostQualification
    target_host_measurement_digest: str
    durable_financial_binding_digest: str
    projection_sha256_by_kind: Mapping[str, str]

    def __post_init__(self) -> None:
        if type(self.qualification) is not AcceptedRuntimeTargetHostQualification:
            raise TypeError(
                "qualification must be exact AcceptedRuntimeTargetHostQualification"
            )
        object.__setattr__(
            self,
            "projection_sha256_by_kind",
            MappingProxyType(dict(self.projection_sha256_by_kind)),
        )


def verify_composed_runtime_target_host_qualification(
    receipt: SignedQualificationAttestation,
    *,
    evidence_store: ArtifactStore,
    evidence_root: str,
    journal_store: JournalStore,
    spec: RuntimeBudgetSpec,
    campaign_plan: RuntimeCampaignPlan,
    campaign_cut: RuntimeCampaignCut,
    declared_plan_id: str,
    measurement: TargetHostMeasurementArtifact,
    expected_release_artifact_id: str,
    expected_release_artifact_sha256: str,
) -> AcceptedComposedRuntimeTargetHostQualification:
    """Join canonical signed PASS with the exact validated raw measurement lineage.

    The durable/terminal measurement authority is evaluated first.  Only that
    snapshotted measurement identity is then supplied as the signed profile's
    expected source/scenario/spec/config/host/workload/store/release identity.
    Finally, the three authenticated raw payloads must be the domain-separated
    canonical projections of the same measurement artifact.
    """

    measurement = _snapshot_measurement(measurement)
    durable_binding: DurableTargetHostFinancialBinding = (
        bind_release_bound_durable_financial_latency_to_target_host_measurement(
            store=journal_store,
            spec=spec,
            campaign_plan=campaign_plan,
            campaign_cut=campaign_cut,
            declared_plan_id=declared_plan_id,
            measurement=measurement,
            expected_release_artifact_id=expected_release_artifact_id,
            expected_release_artifact_sha256=expected_release_artifact_sha256,
        )
    )

    accepted = verify_runtime_target_host_qualification(
        receipt,
        evidence_store=evidence_store,
        evidence_root=evidence_root,
        expected_source_sha=measurement.source_sha,
        expected_scenario_id=measurement.scenario_id,
        expected_spec_digest=measurement.spec_digest,
        expected_configuration_hash=measurement.configuration_hash,
        expected_host_fingerprint=measurement.host_fingerprint,
        expected_workload_profile_hash=measurement.workload_profile_hash,
        expected_journal_store_identity_digest=measurement.journal_store_identity_digest,
        expected_release_artifact_id=expected_release_artifact_id,
        expected_release_artifact_sha256=expected_release_artifact_sha256,
    )
    if type(accepted) is not AcceptedRuntimeTargetHostQualification:
        raise RuntimeTargetHostCompositionError(
            "signed target-host verifier returned non-canonical acceptance"
        )

    projection_digests = target_host_measurement_projection_digests(measurement)
    for kind, expected_digest in projection_digests.items():
        actual_digest = accepted.payload_sha256_by_kind.get(kind)
        if actual_digest != expected_digest:
            raise RuntimeTargetHostCompositionError(
                f"signed {kind} raw payload does not bind canonical target-host measurement"
            )

    return AcceptedComposedRuntimeTargetHostQualification(
        qualification=accepted,
        target_host_measurement_digest=measurement.digest,
        durable_financial_binding_digest=durable_binding.digest,
        projection_sha256_by_kind=projection_digests,
    )
