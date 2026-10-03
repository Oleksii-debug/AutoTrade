"""Product-facing durable-plan binding for terminal WP-65 qualification.

The lower-level signed target-host verifier accepts exact expected identities so
it can validate independently produced evidence. Product admission must not let a
caller invent workload, JournalStore, release, or chronology authority. Canonical
signed terminal admission therefore requires the composed raw measurement path
plus an accepted RELEASE_RUNTIME trusted chronology cut.

The chronology-free implementation remains private so focused unit tests can
exercise durable-plan fencing with non-canonical test doubles. An exact canonical
SignedQualificationAttestation can never return through that path.

This module creates no measurements, chronology source, signer policy, release
attestation, provider authority, economic edge, or trading authority.
"""

from __future__ import annotations

from copy import copy
import sys
from typing import TYPE_CHECKING

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

if TYPE_CHECKING:
    from .production_host import ProductionHostRuntime
    from .recovery import RecoveryController
    from .runtime_target_host_chronology_bound_qualification import (
        AcceptedChronologyBoundRuntimeTargetHostQualification,
    )
    from .trusted_chronology_cut import TrustedChronologyCut


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


def _canonical_sha256_text(value: object, *, name: str) -> str:
    """Return one inert canonical sha256 identity without invoking caller operators."""

    if type(value) is not str:
        raise RuntimeTargetHostCompositionError(
            f"{name} must remain exact canonical sha256 text"
        )
    if (
        not value.startswith("sha256:")
        or len(value) != 71
        or value != value.lower()
        or any(char not in "0123456789abcdef" for char in value[7:])
    ):
        raise RuntimeTargetHostCompositionError(
            f"{name} must remain exact canonical sha256 text"
        )
    return value


def _snapshot_campaign_cut(value: RuntimeCampaignCut) -> RuntimeCampaignCut:
    """Detach one issued cut before composed code performs caller-visible work."""

    if type(value) is not RuntimeCampaignCut:
        raise TypeError("campaign_cut must be exact RuntimeCampaignCut")
    detached = copy(value)
    if type(detached) is not RuntimeCampaignCut or detached is value:
        raise RuntimeTargetHostCompositionError(
            "campaign_cut could not be detached at terminal authority boundary"
        )
    for field in ("plan_digest", "spec_digest", "journal_store_identity_digest"):
        if type(getattr(detached, field)) is not str:
            raise RuntimeTargetHostCompositionError(
                f"campaign_cut {field} must remain exact inert text"
            )
    for field in ("start_journal_sequence", "started_monotonic_ns"):
        field_value = getattr(detached, field)
        if type(field_value) is not int or field_value < 0:
            raise RuntimeTargetHostCompositionError(
                f"campaign_cut {field} must remain a non-negative integer"
            )
    return detached


def _verify_declared_plan_runtime_target_host_qualification_without_chronology(
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
    """Apply durable-plan + composed WP-65 fencing without granting terminal status."""

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
        durable_plan_digest = _canonical_sha256_text(
            plan.digest,
            name="durable pre-run plan digest",
        )
        if type(campaign_plan) is RuntimeCampaignPlan:
            campaign_workload = _canonical_sha256_text(
                campaign_plan.workload_profile_hash,
                name="campaign workload identity",
            )
            if campaign_workload != durable_plan_digest:
                raise RuntimeTargetHostCompositionError(
                    "campaign workload identity does not match durable pre-run plan"
                )
        if type(measurement) is TargetHostMeasurementArtifact:
            measurement_workload = _canonical_sha256_text(
                measurement.workload_profile_hash,
                name="measurement workload identity",
            )
            if measurement_workload != durable_plan_digest:
                raise RuntimeTargetHostCompositionError(
                    "measurement workload identity does not match durable pre-run plan"
                )
        if type(campaign_cut) is RuntimeCampaignCut:
            campaign_cut = _snapshot_campaign_cut(campaign_cut)
        accepted = verify_composed_runtime_target_host_qualification(
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

        final_plan = load_declared_runtime_event_plan(
            journal_store,
            plan_id=plan.plan_id,
            spec=spec,
        )
        final_plan_digest = _canonical_sha256_text(
            final_plan.digest,
            name="revalidated durable pre-run plan digest",
        )
        if final_plan_digest != durable_plan_digest:
            raise RuntimeTargetHostCompositionError(
                "durable pre-run plan changed during terminal qualification"
            )
        final_journal_identity = require_exact_journal_store_authority(
            journal_store,
            subject="runtime target-host qualification JournalStore",
        )
        if final_journal_identity != selected_journal_identity:
            raise RuntimeError(
                "journal operation authority changed during terminal qualification"
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
    expected_release_artifact_id: str,
    expected_release_artifact_sha256: str,
    campaign_plan: RuntimeCampaignPlan | None = None,
    campaign_cut: RuntimeCampaignCut | None = None,
    measurement: TargetHostMeasurementArtifact | None = None,
    recovery: RecoveryController | None = None,
    runtime: ProductionHostRuntime | None = None,
    chronology_cut: TrustedChronologyCut | None = None,
) -> (
    AcceptedComposedRuntimeTargetHostQualification
    | AcceptedChronologyBoundRuntimeTargetHostQualification
):
    """Verify product-facing WP-65 admission, requiring chronology for real receipts.

    Exact canonical signed receipts are terminal authority candidates and therefore
    fail closed unless all three RELEASE_RUNTIME chronology inputs are supplied.
    Non-canonical receipt objects can only reach the private lower-level path used
    by focused fencing tests; the canonical signed verifier rejects such objects.
    """

    chronology_values = (recovery, runtime, chronology_cut)
    chronology_supplied = any(value is not None for value in chronology_values)
    if chronology_supplied and any(value is None for value in chronology_values):
        raise RuntimeTargetHostCompositionError(
            "terminal WP-65 qualification requires complete RELEASE_RUNTIME chronology authority"
        )

    if chronology_supplied:
        from .runtime_target_host_chronology_bound_qualification import (
            verify_chronology_bound_runtime_target_host_qualification,
        )

        return verify_chronology_bound_runtime_target_host_qualification(
            receipt,
            evidence_store=evidence_store,
            evidence_root=evidence_root,
            journal_store=journal_store,
            recovery=recovery,
            runtime=runtime,
            chronology_cut=chronology_cut,
            plan_id=plan_id,
            spec=spec,
            expected_release_artifact_id=expected_release_artifact_id,
            expected_release_artifact_sha256=expected_release_artifact_sha256,
            campaign_plan=campaign_plan,
            campaign_cut=campaign_cut,
            measurement=measurement,
        )

    if type(receipt) is SignedQualificationAttestation:
        raise RuntimeTargetHostCompositionError(
            "terminal WP-65 qualification requires accepted RELEASE_RUNTIME chronology authority"
        )

    return _verify_declared_plan_runtime_target_host_qualification_without_chronology(
        receipt,
        evidence_store=evidence_store,
        evidence_root=evidence_root,
        journal_store=journal_store,
        plan_id=plan_id,
        spec=spec,
        expected_release_artifact_id=expected_release_artifact_id,
        expected_release_artifact_sha256=expected_release_artifact_sha256,
        campaign_plan=campaign_plan,
        campaign_cut=campaign_cut,
        measurement=measurement,
    )


# Install the captured production dispatcher before a direct import of this module
# can return the historical lazy-dispatch function. When the terminal facade is
# already importing us through its private implementation, the sys.modules guard
# avoids a circular read of a partially initialized facade; that facade installs
# the same captured dispatcher immediately after its dependencies finish loading.
_TERMINAL_FACADE_MODULE = (
    f"{__package__}.runtime_target_host_chronology_bound_qualification"
)
if _TERMINAL_FACADE_MODULE not in sys.modules:
    from . import runtime_target_host_chronology_bound_qualification as _terminal_bootstrap
