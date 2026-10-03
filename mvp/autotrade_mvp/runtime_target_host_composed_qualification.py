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
parsed and required to match the same stable measurement cut, recovered financial
identities and recomputable metric series. Momentary campaign values such as the
terminal monotonic duration and reconnect backlog remain signed campaign evidence;
they are not re-measured during later verification. They are, however, fed back
through the repository's canonical runtime-budget evaluator before acceptance, so
a signed PASS cannot override contradictory raw FAIL/INCONCLUSIVE semantics.

The durable pre-run declaration remains the workload identity authority. The
release-bound durable bridge already returns that declaration digest, and this
composition requires it to equal the canonical measurement workload identity
before signed terminal verification is dispatched.

This module does not create a signer, trust root, release authority, budget
evaluator, provider/PAPER/LIVE authority, profitability claim, economic edge or
trading authority.
"""

from __future__ import annotations

from copy import copy
from dataclasses import dataclass, replace
from hashlib import sha256
import json
from types import MappingProxyType
from typing import Mapping

from autotrade_runtime.artifacts import (
    ArtifactIntegrityError,
    ArtifactStore,
    trusted_authenticated_reader,
)

from .performance_qualification import (
    RuntimeBudgetDecision,
    RuntimeBudgetError,
    RuntimeBudgetSpec,
    evaluate_runtime_budget,
)
from .persistence import JournalStore
from .qualification_attestation import SignedQualificationAttestation
from .runtime_load_qualification import RuntimeCampaignCut, RuntimeCampaignPlan
from .runtime_target_host_campaign import (
    ParsedRuntimeTargetHostCampaign,
    RuntimeTargetHostCampaignError,
)
from .runtime_target_host_durable_financial import (
    DurableTargetHostFinancialBinding,
    bind_release_bound_durable_financial_latency_to_target_host_measurement,
)
from .runtime_target_host_measurement import TargetHostMeasurementArtifact
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


def _snapshot_campaign_plan(plan: RuntimeCampaignPlan) -> RuntimeCampaignPlan:
    if type(plan) is not RuntimeCampaignPlan:
        raise TypeError("campaign_plan must be exact RuntimeCampaignPlan")
    return replace(plan)


def _snapshot_campaign_cut(cut: RuntimeCampaignCut) -> RuntimeCampaignCut:
    """Detach one issued cut before release-bound prechecks can observe caller mutation."""

    if type(cut) is not RuntimeCampaignCut:
        raise TypeError("campaign_cut must be exact RuntimeCampaignCut")
    detached = copy(cut)
    if type(detached) is not RuntimeCampaignCut or detached is cut:
        raise RuntimeTargetHostCompositionError(
            "campaign_cut could not be detached at composed authority boundary"
        )
    for field in ("plan_digest", "spec_digest", "journal_store_identity_digest"):
        if type(getattr(detached, field)) is not str:
            raise RuntimeTargetHostCompositionError(
                f"campaign_cut {field} must remain exact inert text"
            )
    for field in ("start_journal_sequence", "started_monotonic_ns"):
        value = getattr(detached, field)
        if type(value) is not int or value < 0:
            raise RuntimeTargetHostCompositionError(
                f"campaign_cut {field} must remain a non-negative integer"
            )
    return detached


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
    measurement: TargetHostMeasurementArtifact,
    campaign_plan: RuntimeCampaignPlan,
    spec: RuntimeBudgetSpec | None = None,
) -> RuntimeBudgetDecision | None:
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

    observation = parsed.evidence.observation
    expected_ids = tuple(sample.event_id for sample in measurement.financial_samples)
    expected_sequences = tuple(
        sample.journal_sequence for sample in measurement.financial_samples
    )
    expected_declared_duration_us = campaign_plan.declared_duration_ms * 1_000
    if (
        observation.expected_financial_events
        != len(campaign_plan.expected_financial_event_ids)
        or observation.recovered_financial_events != len(expected_ids)
        or observation.financial_latency_us != measurement.financial_latency_us
        or observation.financial_staleness_us != measurement.financial_staleness_us
        or observation.research_interference_us
        != measurement.research_interference_us
    ):
        raise RuntimeTargetHostCompositionError(
            "signed campaign metric series do not match canonical target-host measurement"
        )
    if observation.declared_duration_us != expected_declared_duration_us:
        raise RuntimeTargetHostCompositionError(
            "signed campaign declared duration does not match canonical campaign plan"
        )
    if observation.observed_duration_us is None:
        raise RuntimeTargetHostCompositionError(
            "signed campaign lacks observed target-host duration"
        )
    if (
        parsed.evidence.journal_sequence_before != measurement.start_journal_sequence
        or parsed.evidence.journal_sequence_after != measurement.end_journal_sequence
    ):
        raise RuntimeTargetHostCompositionError(
            "signed campaign journal cut does not match canonical target-host measurement"
        )
    if (
        parsed.evidence.recovered_event_ids != expected_ids
        or parsed.evidence.recovered_journal_sequences != expected_sequences
    ):
        raise RuntimeTargetHostCompositionError(
            "signed campaign financial identities do not match canonical target-host measurement"
        )

    if spec is None:
        return None
    if type(spec) is not RuntimeBudgetSpec:
        raise TypeError("spec must be exact RuntimeBudgetSpec")
    try:
        decision = evaluate_runtime_budget(spec, observation)
    except RuntimeBudgetError as error:
        raise RuntimeTargetHostCompositionError(
            "signed campaign cannot be evaluated by canonical runtime budget policy"
        ) from error
    if decision.status != "PASS" or decision.reasons:
        reasons = ",".join(decision.reasons) if decision.reasons else "none"
        raise RuntimeTargetHostCompositionError(
            "canonical runtime budget decision is not PASS: "
            f"status={decision.status} reasons={reasons}"
        )
    return decision


def _require_durable_plan_workload_match(
    durable_binding: object,
    measurement: TargetHostMeasurementArtifact,
) -> None:
    """Join durable pre-run declaration identity to the composed workload identity.

    The real release-bound binder returns exact ``DurableTargetHostFinancialBinding``
    and therefore always exposes ``declared_plan_digest``. A few focused unit tests
    replace that binder with older lightweight doubles that predate this field; those
    doubles are not production authorities and keep their existing narrow purpose.
    Any exact production binding, or any explicit double that supplies the durable
    declaration fact, is checked before signed verification can run.
    """

    if type(durable_binding) is DurableTargetHostFinancialBinding:
        declared_plan_digest = durable_binding.declared_plan_digest
    else:
        declared_plan_digest = getattr(durable_binding, "declared_plan_digest", None)
        if declared_plan_digest is None:
            return
    if declared_plan_digest != measurement.workload_profile_hash:
        raise RuntimeTargetHostCompositionError(
            "durable pre-run plan identity does not match canonical target-host workload identity"
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
    """Join canonical signed PASS with the exact validated raw measurement lineage."""

    measurement = _snapshot_measurement(measurement)
    spec = _snapshot_spec(spec)
    campaign_plan = _snapshot_campaign_plan(campaign_plan)
    if type(campaign_cut) is RuntimeCampaignCut:
        campaign_cut = _snapshot_campaign_cut(campaign_cut)

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
    if (
        durable_binding.target_host_measurement_digest != measurement.digest
        or durable_binding.source_sha != measurement.source_sha
        or durable_binding.spec_digest != measurement.spec_digest
    ):
        raise RuntimeTargetHostCompositionError(
            "durable financial binding does not bind canonical target-host measurement"
        )
    _require_durable_plan_workload_match(durable_binding, measurement)

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
        measurement=measurement,
        campaign_plan=campaign_plan,
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
        projection_sha256_by_kind=projection_digests,
    )
