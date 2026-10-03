"""Compose signed WP-65 qualification with canonical raw target-host measurement truth.

The signed target-host profile authenticates retained payloads but intentionally
owns no target-host measurement collector. The raw measurement lineage owns
recomputable financial staleness, research-interference and resource samples but
owns no signer. This adapter joins those authorities without letting either side
self-assert the other.

Three domain-separated canonical projections are derived from one validated
``TargetHostMeasurementArtifact``. A signed PASS is composition-eligible only
when the already authenticated raw payload digest for each measurement family is
exactly the digest of its projection. The signed campaign payload is separately
parsed and required to equal the terminal ``RuntimeCampaignEvidence`` collected
from the same JournalStore cut, including latency/staleness/interference series,
reconnect backlog and observed duration.

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

from autotrade_runtime.artifacts import (
    ArtifactIntegrityError,
    ArtifactStore,
    trusted_authenticated_reader,
)

from .performance_qualification import RuntimeBudgetSpec
from .persistence import JournalStore
from .qualification_attestation import SignedQualificationAttestation
from .runtime_load_qualification import (
    RuntimeCampaignCut,
    RuntimeCampaignEvidence,
    RuntimeCampaignPlan,
)
from .runtime_target_host_campaign import (
    ParsedRuntimeTargetHostCampaign,
    RuntimeTargetHostCampaignError,
)
from .runtime_target_host_durable_financial import (
    DurableTargetHostFinancialBinding,
    bind_durable_financial_latency_to_target_host_measurement,
)
from .runtime_target_host_measurement import TargetHostMeasurementArtifact
from .runtime_target_host_measurement_authority import (
    collect_release_bound_target_host_evidence,
)
from .runtime_target_host_qualification import (
    AcceptedRuntimeTargetHostQualification,
    CAMPAIGN_EVIDENCE_KIND,
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


def _snapshot_spec(spec: RuntimeBudgetSpec) -> RuntimeBudgetSpec:
    if type(spec) is not RuntimeBudgetSpec:
        raise TypeError("spec must be exact RuntimeBudgetSpec")
    return RuntimeBudgetSpec(
        scenario_id=spec.scenario_id,
        release_sha=spec.release_sha,
        configuration_hash=spec.configuration_hash,
        host_fingerprint=spec.host_fingerprint,
        strategy_horizon_us=spec.strategy_horizon_us,
        max_p95_financial_latency_us=spec.max_p95_financial_latency_us,
        max_financial_staleness_us=spec.max_financial_staleness_us,
        max_research_interference_us=spec.max_research_interference_us,
        min_financial_samples=spec.min_financial_samples,
        min_research_samples=spec.min_research_samples,
    )


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


def _read_accepted_raw_payload(
    accepted: AcceptedRuntimeTargetHostQualification,
    *,
    evidence_store: ArtifactStore,
    evidence_root: str,
    evidence_kind: str,
) -> bytes:
    artifact_id = accepted.payload_artifact_id_by_kind.get(evidence_kind)
    expected_sha256 = accepted.payload_sha256_by_kind.get(evidence_kind)
    if type(artifact_id) is not str or not artifact_id:
        raise RuntimeTargetHostCompositionError(
            f"signed {evidence_kind} raw payload artifact identity is missing"
        )
    if type(expected_sha256) is not str or not expected_sha256:
        raise RuntimeTargetHostCompositionError(
            f"signed {evidence_kind} raw payload digest is missing"
        )
    try:
        reader = trusted_authenticated_reader(
            evidence_root,
            publication_store=evidence_store,
        )
        _manifest, raw = reader(artifact_id)
    except (ArtifactIntegrityError, FileNotFoundError, OSError, TypeError, ValueError) as error:
        raise RuntimeTargetHostCompositionError(
            f"signed {evidence_kind} raw payload cannot be re-read with integrity"
        ) from error
    if type(raw) is not bytes or not raw:
        raise RuntimeTargetHostCompositionError(
            f"signed {evidence_kind} raw payload is empty or non-bytes"
        )
    if _sha256(raw) != expected_sha256:
        raise RuntimeTargetHostCompositionError(
            f"signed {evidence_kind} raw payload changed after canonical verification"
        )
    return raw


def _require_signed_campaign_match(
    accepted: AcceptedRuntimeTargetHostQualification,
    *,
    evidence_store: ArtifactStore,
    evidence_root: str,
    campaign_evidence: RuntimeCampaignEvidence,
    spec: RuntimeBudgetSpec,
) -> None:
    raw = _read_accepted_raw_payload(
        accepted,
        evidence_store=evidence_store,
        evidence_root=evidence_root,
        evidence_kind=CAMPAIGN_EVIDENCE_KIND,
    )
    try:
        parsed = ParsedRuntimeTargetHostCampaign.parse(raw)
    except RuntimeTargetHostCampaignError as error:
        raise RuntimeTargetHostCompositionError(
            "signed campaign raw payload is not canonical target-host campaign evidence"
        ) from error

    expected_observation = campaign_evidence.to_observation(spec)
    recovered_ids = campaign_evidence.recovered_financial_event_ids
    recovered_sequences = tuple(
        sequence
        for _event_id, _payload_hash, sequence
        in campaign_evidence.recovered_financial_event_bindings
    )
    if parsed.evidence.observation != expected_observation:
        raise RuntimeTargetHostCompositionError(
            "signed campaign observation does not match terminal JournalStore evidence"
        )
    if (
        parsed.evidence.journal_sequence_before != campaign_evidence.start_journal_sequence
        or parsed.evidence.journal_sequence_after != campaign_evidence.end_journal_sequence
    ):
        raise RuntimeTargetHostCompositionError(
            "signed campaign journal cut does not match terminal JournalStore evidence"
        )
    if (
        parsed.evidence.recovered_event_ids != recovered_ids
        or parsed.evidence.recovered_journal_sequences != recovered_sequences
    ):
        raise RuntimeTargetHostCompositionError(
            "signed campaign financial identities do not match terminal JournalStore evidence"
        )


@dataclass(frozen=True, slots=True)
class AcceptedComposedRuntimeTargetHostQualification:
    """Terminal composition result retaining both independent authority digests."""

    qualification: AcceptedRuntimeTargetHostQualification
    target_host_measurement_digest: str
    durable_financial_binding_digest: str
    campaign_evidence_digest: str
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
    """Join canonical signed PASS with the exact validated raw measurement lineage."""

    measurement = _snapshot_measurement(measurement)
    spec = _snapshot_spec(spec)

    # Keep the exact terminal campaign evidence instead of discarding it at the
    # durable-financial facade. This lets the signed campaign payload be compared
    # to the same frozen JournalStore cut that admitted the raw measurement.
    campaign_evidence = collect_release_bound_target_host_evidence(
        journal=journal_store,
        spec=spec,
        plan=campaign_plan,
        cut=campaign_cut,
        measurement=measurement,
        expected_release_artifact_id=expected_release_artifact_id,
        expected_release_artifact_sha256=expected_release_artifact_sha256,
    )
    durable_binding: DurableTargetHostFinancialBinding = (
        bind_durable_financial_latency_to_target_host_measurement(
            journal_store,
            spec,
            declared_plan_id=declared_plan_id,
            measurement=measurement,
        )
    )
    if (
        durable_binding.target_host_measurement_digest != measurement.digest
        or durable_binding.source_sha != measurement.source_sha
        or durable_binding.spec_digest != measurement.spec_digest
    ):
        raise RuntimeTargetHostCompositionError(
            "durable financial binding does not bind canonical target-host measurement"
        )
    if campaign_evidence.resource_evidence_hash != measurement.digest:
        raise RuntimeTargetHostCompositionError(
            "terminal campaign evidence does not bind canonical target-host measurement"
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

    _require_signed_campaign_match(
        accepted,
        evidence_store=evidence_store,
        evidence_root=evidence_root,
        campaign_evidence=campaign_evidence,
        spec=spec,
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
        campaign_evidence_digest=campaign_evidence.digest,
        projection_sha256_by_kind=projection_digests,
    )
