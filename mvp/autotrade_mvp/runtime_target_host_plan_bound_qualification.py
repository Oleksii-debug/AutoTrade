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
from .persistence import JournalStore
from .qualification_attestation import SignedQualificationAttestation
from .runtime_load_plan import load_declared_runtime_event_plan
from .runtime_target_host_qualification import (
    AcceptedRuntimeTargetHostQualification,
    verify_runtime_target_host_qualification,
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
    if type(spec) is not RuntimeBudgetSpec:
        raise TypeError("spec must be exact RuntimeBudgetSpec")

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
