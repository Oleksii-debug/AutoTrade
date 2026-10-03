"""Product-facing terminal composition for WP-65 target-host qualification.

The lower-level signed target-host verifier authenticates the profile envelopes
and retained payload identities. Product admission additionally has to prove that
those signed assertions describe the exact durable pre-run declaration and the
exact campaign generation that produced the raw target-host measurement.

This adapter therefore composes existing authorities rather than inventing a new
collector or evaluator:

* the immutable ``DeclaredRuntimeEventPlan`` supplies pre-run durable expected
  financial identities;
* ``RuntimeCampaignPlan`` + ``RuntimeCampaignCut`` supply the exact workload,
  campaign-plan digest and JournalStore generation/start cut;
* the already-signed RESOURCE retained payload must be the canonical
  ``TargetHostMeasurementArtifact`` from the #1226 measurement authority; and
* the already-signed CAMPAIGN retained payload must recompute from that raw
  measurement for every overlapping identity/summary field.

This module creates no measurements, trusted chronology, signer policy, release
attestation, provider authority, or trading authority.
"""

from __future__ import annotations

from hashlib import sha256

from autotrade_runtime.artifacts import (
    ArtifactIntegrityError,
    ArtifactStore,
    trusted_authenticated_reader,
)

from .performance_qualification import RuntimeBudgetSpec
from .persistence import (
    JournalStore,
    journal_store_authority_scope,
    require_exact_journal_store_authority,
)
from .qualification_attestation import SignedQualificationAttestation
from .runtime_load_plan import DeclaredRuntimeEventPlan, load_declared_runtime_event_plan
from .runtime_load_qualification import RuntimeCampaignCut, RuntimeCampaignPlan
from .runtime_target_host_campaign import (
    ParsedRuntimeTargetHostCampaign,
    RuntimeTargetHostCampaignError,
)
from .runtime_target_host_measurement import (
    MEASUREMENT_METHOD_ID,
    MEASUREMENT_METHOD_VERSION,
    RuntimeTargetHostMeasurementError,
    TargetHostMeasurementArtifact,
)
from .runtime_target_host_qualification import (
    CAMPAIGN_EVIDENCE_KIND,
    RESOURCE_EVIDENCE_KIND,
    AcceptedRuntimeTargetHostQualification,
    RuntimeTargetHostQualificationError,
    verify_runtime_target_host_qualification,
)


def _snapshot_budget_spec(value: RuntimeBudgetSpec) -> RuntimeBudgetSpec:
    if type(value) is not RuntimeBudgetSpec:
        raise TypeError("spec must be exact RuntimeBudgetSpec")
    return RuntimeBudgetSpec(
        scenario_id=value.scenario_id,
        release_sha=value.release_sha,
        configuration_hash=value.configuration_hash,
        host_fingerprint=value.host_fingerprint,
        strategy_horizon_us=value.strategy_horizon_us,
        max_p95_financial_latency_us=value.max_p95_financial_latency_us,
        max_financial_staleness_us=value.max_financial_staleness_us,
        max_research_interference_us=value.max_research_interference_us,
        min_financial_samples=value.min_financial_samples,
        min_research_samples=value.min_research_samples,
    )


def _snapshot_campaign_plan(value: RuntimeCampaignPlan) -> RuntimeCampaignPlan:
    if type(value) is not RuntimeCampaignPlan:
        raise TypeError("campaign_plan must be exact RuntimeCampaignPlan")
    return RuntimeCampaignPlan(
        scenario_id=value.scenario_id,
        spec_digest=value.spec_digest,
        release_sha=value.release_sha,
        configuration_hash=value.configuration_hash,
        host_fingerprint=value.host_fingerprint,
        workload_profile_hash=value.workload_profile_hash,
        declared_duration_ms=value.declared_duration_ms,
        expected_financial_event_ids=value.expected_financial_event_ids,
        financial_aggregate_types=value.financial_aggregate_types,
        release_artifact_sha256=value.release_artifact_sha256,
        journal_taxonomy_digest=value.journal_taxonomy_digest,
    )


def _require_campaign_and_declaration_alignment(
    *,
    spec: RuntimeBudgetSpec,
    campaign_plan: RuntimeCampaignPlan,
    campaign_cut: RuntimeCampaignCut,
    declared_plan: DeclaredRuntimeEventPlan,
    expected_release_artifact_sha256: str,
) -> None:
    if type(campaign_cut) is not RuntimeCampaignCut:
        raise TypeError("campaign_cut must be exact RuntimeCampaignCut")
    checks = {
        "scenario": (campaign_plan.scenario_id, spec.scenario_id),
        "spec": (campaign_plan.spec_digest, spec.digest),
        "source": (campaign_plan.release_sha, spec.release_sha),
        "configuration": (campaign_plan.configuration_hash, spec.configuration_hash),
        "host": (campaign_plan.host_fingerprint, spec.host_fingerprint),
        "cut spec": (campaign_cut.spec_digest, spec.digest),
        "cut plan": (campaign_cut.plan_digest, campaign_plan.digest),
        "journal generation": (
            campaign_cut.journal_store_identity_digest,
            declared_plan.store_identity_digest,
        ),
        "release artifact": (
            campaign_plan.release_artifact_sha256,
            expected_release_artifact_sha256,
        ),
    }
    mismatches = [name for name, (actual, expected) in checks.items() if actual != expected]
    if mismatches:
        raise RuntimeTargetHostQualificationError(
            "runtime target-host campaign authority conflicts: " + ", ".join(mismatches)
        )
    if declared_plan.declared_journal_sequence > campaign_cut.start_journal_sequence:
        raise RuntimeTargetHostQualificationError(
            "durable runtime event plan was declared after target-host campaign start"
        )
    declared_ids = tuple(value.event_id for value in declared_plan.expected_events)
    if set(declared_ids) != set(campaign_plan.expected_financial_event_ids) or (
        len(declared_ids) != len(campaign_plan.expected_financial_event_ids)
    ):
        raise RuntimeTargetHostQualificationError(
            "campaign expected financial identities conflict with durable declaration"
        )
    declared_aggregate_types = {
        value.aggregate_type for value in declared_plan.expected_events
    }
    if declared_aggregate_types != set(campaign_plan.financial_aggregate_types):
        raise RuntimeTargetHostQualificationError(
            "campaign financial aggregate families conflict with durable declaration"
        )


def _read_accepted_payload(
    reader,
    accepted: AcceptedRuntimeTargetHostQualification,
    *,
    kind: str,
) -> bytes:
    try:
        artifact_id = accepted.payload_artifact_id_by_kind[kind]
        expected_digest = accepted.payload_sha256_by_kind[kind]
    except KeyError as error:
        raise RuntimeTargetHostQualificationError(
            f"accepted target-host qualification lacks retained {kind} payload"
        ) from error
    try:
        _manifest, raw = reader(artifact_id)
    except (ArtifactIntegrityError, FileNotFoundError, OSError) as error:
        raise RuntimeTargetHostQualificationError(
            f"accepted target-host retained {kind} payload is unavailable"
        ) from error
    if type(raw) is not bytes or not raw:
        raise RuntimeTargetHostQualificationError(
            f"accepted target-host retained {kind} payload is empty or non-bytes"
        )
    actual_digest = "sha256:" + sha256(raw).hexdigest()
    if actual_digest != expected_digest:
        raise RuntimeTargetHostQualificationError(
            f"accepted target-host retained {kind} payload digest changed"
        )
    return raw


def _verify_measurement_campaign_composition(
    accepted: AcceptedRuntimeTargetHostQualification,
    *,
    evidence_store: ArtifactStore,
    evidence_root: str,
    spec: RuntimeBudgetSpec,
    campaign_plan: RuntimeCampaignPlan,
    campaign_cut: RuntimeCampaignCut,
    declared_plan: DeclaredRuntimeEventPlan,
    expected_release_artifact_id: str,
) -> AcceptedRuntimeTargetHostQualification:
    if type(accepted) is not AcceptedRuntimeTargetHostQualification:
        raise TypeError(
            "accepted target-host qualification must be exact AcceptedRuntimeTargetHostQualification"
        )
    try:
        reader = trusted_authenticated_reader(
            evidence_root,
            publication_store=evidence_store,
        )
    except (ArtifactIntegrityError, OSError, TypeError, ValueError) as error:
        raise RuntimeTargetHostQualificationError(
            "target-host retained composition authority cannot be bound"
        ) from error

    measurement_raw = _read_accepted_payload(
        reader,
        accepted,
        kind=RESOURCE_EVIDENCE_KIND,
    )
    campaign_raw = _read_accepted_payload(
        reader,
        accepted,
        kind=CAMPAIGN_EVIDENCE_KIND,
    )
    try:
        measurement = TargetHostMeasurementArtifact.parse(measurement_raw)
    except RuntimeTargetHostMeasurementError as error:
        raise RuntimeTargetHostQualificationError(
            "target-host RESOURCE payload is not canonical raw measurement evidence"
        ) from error
    try:
        campaign = ParsedRuntimeTargetHostCampaign.parse(campaign_raw)
    except RuntimeTargetHostCampaignError as error:
        raise RuntimeTargetHostQualificationError(
            "target-host CAMPAIGN payload is not canonical retained campaign evidence"
        ) from error

    expected_measurement_collector = (
        f"{MEASUREMENT_METHOD_ID}@{MEASUREMENT_METHOD_VERSION}"
    )
    if accepted.collector_by_kind.get(RESOURCE_EVIDENCE_KIND) != (
        expected_measurement_collector
    ):
        raise RuntimeTargetHostQualificationError(
            "target-host RESOURCE provenance does not identify canonical measurement method"
        )
    if measurement.release_artifact_id != expected_release_artifact_id:
        raise RuntimeTargetHostQualificationError(
            "retained target-host measurement belongs to another delivered release artifact"
        )
    try:
        measurement.require_campaign_binding(
            spec=spec,
            plan=campaign_plan,
            cut=campaign_cut,
        )
    except RuntimeTargetHostMeasurementError as error:
        raise RuntimeTargetHostQualificationError(str(error)) from error

    if measurement.financial_event_ids != declared_plan.expected_event_ids:
        raise RuntimeTargetHostQualificationError(
            "retained target-host measurement financial identities conflict with durable declaration"
        )

    evidence = campaign.evidence
    observation = evidence.observation
    measured_sequences = tuple(
        sample.journal_sequence for sample in measurement.financial_samples
    )
    mismatches: list[str] = []
    if evidence.journal_sequence_before != measurement.start_journal_sequence:
        mismatches.append("start journal cut")
    if evidence.journal_sequence_after != measurement.end_journal_sequence:
        mismatches.append("end journal cut")
    if evidence.recovered_event_ids != measurement.financial_event_ids:
        mismatches.append("financial event identities")
    if evidence.recovered_journal_sequences != measured_sequences:
        mismatches.append("financial journal sequences")
    if observation.expected_financial_events != len(declared_plan.expected_events):
        mismatches.append("expected financial count")
    if observation.recovered_financial_events != len(measurement.financial_samples):
        mismatches.append("recovered financial count")
    if observation.financial_latency_us != measurement.financial_latency_us:
        mismatches.append("financial latency summary")
    if observation.financial_staleness_us != measurement.financial_staleness_us:
        mismatches.append("financial staleness summary")
    if observation.research_interference_us != measurement.research_interference_us:
        mismatches.append("research interference summary")
    if observation.declared_duration_us != campaign_plan.declared_duration_ms * 1_000:
        mismatches.append("declared duration")
    if mismatches:
        raise RuntimeTargetHostQualificationError(
            "retained target-host campaign conflicts with raw measurement: "
            + ", ".join(mismatches)
        )
    return accepted


def verify_declared_plan_runtime_target_host_qualification(
    receipt: SignedQualificationAttestation,
    *,
    evidence_store: ArtifactStore,
    evidence_root: str,
    journal_store: JournalStore,
    plan_id: str,
    spec: RuntimeBudgetSpec,
    campaign_plan: RuntimeCampaignPlan,
    campaign_cut: RuntimeCampaignCut,
    expected_release_artifact_id: str,
    expected_release_artifact_sha256: str,
) -> AcceptedRuntimeTargetHostQualification:
    """Verify terminal WP-65 evidence against durable + campaign authority.

    Workload identity is derived from the exact ``RuntimeCampaignPlan`` that is
    bound into the JournalStore-issued campaign cut. It is deliberately **not**
    derived from ``DeclaredRuntimeEventPlan.digest``: that digest identifies the
    pre-run expected-event declaration, not the workload profile.

    The lower signed verifier authenticates the target-host evidence graph. This
    facade then requires the signed RESOURCE payload to be the canonical raw
    target-host measurement and cross-binds the signed CAMPAIGN payload to it.
    """

    if type(journal_store) is not JournalStore:
        raise TypeError("journal_store must be exact JournalStore")
    spec = _snapshot_budget_spec(spec)
    campaign_plan = _snapshot_campaign_plan(campaign_plan)
    if type(campaign_cut) is not RuntimeCampaignCut:
        raise TypeError("campaign_cut must be exact RuntimeCampaignCut")

    selected_journal_identity = require_exact_journal_store_authority(
        journal_store,
        subject="runtime target-host qualification JournalStore",
    )
    with journal_store_authority_scope(
        journal_store,
        selected_journal_identity,
    ):
        declared_plan = load_declared_runtime_event_plan(
            journal_store,
            plan_id=plan_id,
            spec=spec,
        )

    _require_campaign_and_declaration_alignment(
        spec=spec,
        campaign_plan=campaign_plan,
        campaign_cut=campaign_cut,
        declared_plan=declared_plan,
        expected_release_artifact_sha256=expected_release_artifact_sha256,
    )

    accepted = verify_runtime_target_host_qualification(
        receipt,
        evidence_store=evidence_store,
        evidence_root=evidence_root,
        expected_source_sha=spec.release_sha,
        expected_scenario_id=declared_plan.scenario_id,
        expected_spec_digest=declared_plan.spec_digest,
        expected_configuration_hash=spec.configuration_hash,
        expected_host_fingerprint=spec.host_fingerprint,
        expected_workload_profile_hash=campaign_plan.workload_profile_hash,
        expected_journal_store_identity_digest=(
            campaign_cut.journal_store_identity_digest
        ),
        expected_release_artifact_id=expected_release_artifact_id,
        expected_release_artifact_sha256=expected_release_artifact_sha256,
    )
    return _verify_measurement_campaign_composition(
        accepted,
        evidence_store=evidence_store,
        evidence_root=evidence_root,
        spec=spec,
        campaign_plan=campaign_plan,
        campaign_cut=campaign_cut,
        declared_plan=declared_plan,
        expected_release_artifact_id=expected_release_artifact_id,
    )
