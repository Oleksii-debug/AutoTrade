"""Product-facing durable-plan binding for terminal WP-65 qualification.

The lower-level signed target-host verifier accepts exact expected identities so
it can validate independently produced evidence. Product admission must not let a
caller invent the workload-profile or JournalStore-generation digests used at
that boundary. This adapter reloads the immutable pre-run declaration from the
canonical JournalStore and then requires the composed signed + raw measurement
authority. A legacy signed PASS alone is intentionally insufficient.

This module creates no measurements, trusted chronology, signer policy, release
attestation, provider authority, or trading authority.
"""

from __future__ import annotations

from copy import copy

from autotrade_runtime.artifacts import ArtifactStore

from .performance_qualification import RuntimeBudgetSpec
from .persistence import (
    JournalStore,
    journal_store_authority_scope,
    require_exact_journal_store_authority,
)
from .qualification_attestation import SignedQualificationAttestation
from .runtime_load_plan import load_declared_runtime_event_plan
from .runtime_load_qualification import RuntimeCampaignCut, RuntimeCampaignPlan
from .runtime_target_host_composed_qualification import (
    AcceptedComposedRuntimeTargetHostQualification,
    RuntimeTargetHostCompositionError,
    verify_composed_runtime_target_host_qualification,
)
from .runtime_target_host_measurement import TargetHostMeasurementArtifact


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


def _snapshot_campaign_cut(value: RuntimeCampaignCut) -> RuntimeCampaignCut:
    """Detach one issued cut before composed code performs any caller-visible work."""

    if type(value) is not RuntimeCampaignCut:
        raise TypeError("campaign_cut must be exact RuntimeCampaignCut")
    for field in ("plan_digest", "spec_digest", "journal_store_identity_digest"):
        if type(getattr(value, field)) is not str:
            raise RuntimeTargetHostCompositionError(
                f"campaign_cut {field} must remain exact inert text"
            )
    for field in ("start_journal_sequence", "started_monotonic_ns"):
        field_value = getattr(value, field)
        if type(field_value) is not int or field_value < 0:
            raise RuntimeTargetHostCompositionError(
                f"campaign_cut {field} must remain a non-negative integer"
            )
    detached = copy(value)
    if type(detached) is not RuntimeCampaignCut or detached is value:
        raise RuntimeTargetHostCompositionError(
            "campaign_cut could not be detached at terminal authority boundary"
        )
    return detached


def verify_declared_plan_runtime_target_host_qualification(
    receipt: SignedQualificationAttestation,
    *,
    evidence_store: ArtifactStore,
    evidence_root: str,
    journal_store: JournalStore,
    plan_id: str,
    spec: RuntimeBudgetSpec,
    expected_release_artifact_id: str,
    expected_release_artifact_sha256: str,
    campaign_plan: RuntimeCampaignPlan | None = None,
    campaign_cut: RuntimeCampaignCut | None = None,
    measurement: TargetHostMeasurementArtifact | None = None,
) -> AcceptedComposedRuntimeTargetHostQualification:
    """Verify terminal WP-65 evidence through the composed raw/signed authority.

    The durable pre-run plan remains the source of the canonical plan identity.
    Legacy callers that provide only a signed receipt are failed closed: terminal
    acceptance additionally requires the exact campaign plan/cut and canonical
    ``TargetHostMeasurementArtifact`` so the #1228 composition authority can bind
    signed payloads to durable raw financial evidence.
    """

    if type(journal_store) is not JournalStore:
        raise TypeError("journal_store must be exact JournalStore")
    spec = _snapshot_budget_spec(spec)

    selected_journal_identity = require_exact_journal_store_authority(
        journal_store,
        subject="runtime target-host qualification JournalStore",
    )
    with journal_store_authority_scope(
        journal_store,
        selected_journal_identity,
    ):
        plan = load_declared_runtime_event_plan(
            journal_store,
            plan_id=plan_id,
            spec=spec,
        )
        if campaign_plan is None or campaign_cut is None or measurement is None:
            raise RuntimeTargetHostCompositionError(
                "terminal WP-65 qualification requires composed target-host measurement authority"
            )
        # ``RuntimeCampaignCut`` is a frozen issued object but Python callers can
        # still abuse ``object.__setattr__``. Detach its inert scalar state before
        # the composed verifier reaches measurement/campaign-window prechecks.
        # Invalid non-cut inputs remain delegated to the composed authority's
        # existing exact-type checks rather than becoming accepted here.
        if type(campaign_cut) is RuntimeCampaignCut:
            campaign_cut = _snapshot_campaign_cut(campaign_cut)
        return verify_composed_runtime_target_host_qualification(
            receipt,
            evidence_store=evidence_store,
            evidence_root=evidence_root,
            journal_store=journal_store,
            spec=spec,
            campaign_plan=campaign_plan,
            campaign_cut=campaign_cut,
            declared_plan_id=plan.plan_id,
            measurement=measurement,
            expected_release_artifact_id=expected_release_artifact_id,
            expected_release_artifact_sha256=expected_release_artifact_sha256,
        )
