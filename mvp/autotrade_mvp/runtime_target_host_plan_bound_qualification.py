"""Product-facing durable-plan binding for terminal WP-65 qualification.

The lower-level signed target-host verifier accepts exact expected identities so
it can validate independently produced evidence. Product admission must not let a
caller invent the workload-profile or JournalStore-generation digests used at
that boundary. This adapter reloads the immutable pre-run declaration from the
canonical JournalStore and derives those identities from durable authority.

This module creates no measurements, trusted chronology, signer policy, release
attestation, provider authority, or trading authority.
"""

from __future__ import annotations

from autotrade_runtime.artifacts import ArtifactStore

from .performance_qualification import RuntimeBudgetSpec
from .persistence import (
    JournalStore,
    journal_store_authority_scope,
    require_exact_journal_store_authority,
)
from .qualification_attestation import SignedQualificationAttestation
from .runtime_load_plan import load_declared_runtime_event_plan
from .runtime_target_host_qualification import (
    AcceptedRuntimeTargetHostQualification,
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
) -> AcceptedRuntimeTargetHostQualification:
    """Verify terminal WP-65 evidence against one durable pre-run workload plan.

    ``workload_profile_hash`` and ``journal_store_identity_digest`` are
    intentionally absent from this API. Both are reloaded from the canonical
    immutable plan, which itself is verified against the supplied exact budget
    spec and current physical JournalStore generation before signed target-host
    evidence is consulted.
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
    return verify_runtime_target_host_qualification(
        receipt,
        evidence_store=evidence_store,
        evidence_root=evidence_root,
        expected_source_sha=spec.release_sha,
        expected_scenario_id=plan.scenario_id,
        expected_spec_digest=plan.spec_digest,
        expected_configuration_hash=spec.configuration_hash,
        expected_host_fingerprint=spec.host_fingerprint,
        expected_workload_profile_hash=plan.digest,
        expected_journal_store_identity_digest=plan.store_identity_digest,
        expected_release_artifact_id=expected_release_artifact_id,
        expected_release_artifact_sha256=expected_release_artifact_sha256,
    )
