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
from types import MappingProxyType
from typing import TYPE_CHECKING
from uuid import UUID

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
from .runtime_target_host_measurement import (
    RuntimeTargetHostMeasurementError,
    TargetHostMeasurementArtifact,
    snapshot_target_host_measurement,
)
from .runtime_target_host_qualification import (
    AcceptedRuntimeTargetHostQualification,
    BINDING_EVIDENCE_KIND,
    CAMPAIGN_EVIDENCE_KIND,
    HOST_INVENTORY_EVIDENCE_KIND,
    INTERFERENCE_EVIDENCE_KIND,
    RESOURCE_EVIDENCE_KIND,
    STALENESS_EVIDENCE_KIND,
)

if TYPE_CHECKING:
    from .production_host import ProductionHostRuntime
    from .recovery import RecoveryController
    from .runtime_target_host_chronology_bound_qualification import (
        AcceptedChronologyBoundRuntimeTargetHostQualification,
    )
    from .trusted_chronology_cut import TrustedChronologyCut


_PROVENANCE_ACCEPTED_KINDS = frozenset(
    {
        CAMPAIGN_EVIDENCE_KIND,
        STALENESS_EVIDENCE_KIND,
        INTERFERENCE_EVIDENCE_KIND,
        RESOURCE_EVIDENCE_KIND,
        HOST_INVENTORY_EVIDENCE_KIND,
    }
)
_REQUIRED_ACCEPTED_EVIDENCE_KINDS = _PROVENANCE_ACCEPTED_KINDS | {
    BINDING_EVIDENCE_KIND
}
_PROJECTION_ACCEPTED_KINDS = frozenset(
    {
        STALENESS_EVIDENCE_KIND,
        INTERFERENCE_EVIDENCE_KIND,
        RESOURCE_EVIDENCE_KIND,
    }
)


def _snapshot_budget_spec(
    value: RuntimeBudgetSpec,
    _spec_type=RuntimeBudgetSpec,
) -> RuntimeBudgetSpec:
    if type(value) is not _spec_type:
        raise TypeError("spec must be exact RuntimeBudgetSpec")
    return _spec_type(
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


def _canonical_sha256_text(
    value: object,
    *,
    name: str,
    _composition_error_type=RuntimeTargetHostCompositionError,
) -> str:
    """Return one inert canonical sha256 identity without invoking caller operators."""

    if type(value) is not str:
        raise _composition_error_type(
            f"{name} must remain exact canonical sha256 text"
        )
    if (
        not value.startswith("sha256:")
        or len(value) != 71
        or value != value.lower()
        or any(char not in "0123456789abcdef" for char in value[7:])
    ):
        raise _composition_error_type(
            f"{name} must remain exact canonical sha256 text"
        )
    return value


def _snapshot_campaign_cut(
    value: RuntimeCampaignCut,
    *,
    _cut_type=RuntimeCampaignCut,
    _copy=copy,
    _composition_error_type=RuntimeTargetHostCompositionError,
) -> RuntimeCampaignCut:
    """Detach one issued cut before composed code performs caller-visible work."""

    if type(value) is not _cut_type:
        raise TypeError("campaign_cut must be exact RuntimeCampaignCut")
    detached = _copy(value)
    if type(detached) is not _cut_type or detached is value:
        raise _composition_error_type(
            "campaign_cut could not be detached at terminal authority boundary"
        )
    for field in ("plan_digest", "spec_digest", "journal_store_identity_digest"):
        if type(getattr(detached, field)) is not str:
            raise _composition_error_type(
                f"campaign_cut {field} must remain exact inert text"
            )
    for field in ("start_journal_sequence", "started_monotonic_ns"):
        field_value = getattr(detached, field)
        if type(field_value) is not int or field_value < 0:
            raise _composition_error_type(
                f"campaign_cut {field} must remain a non-negative integer"
            )
    return detached


def _build_composed_acceptance_snapshotter(
    *,
    composed_type,
    accepted_type,
    composition_error_type,
    mapping_proxy_type,
    required_evidence_kinds,
    provenance_kinds,
    projection_kinds,
):
    """Build a detached canonical snapshotter for composed verifier output."""

    def exact_text(value: object, *, name: str) -> str:
        if type(value) is not str or not value or value != value.strip():
            raise composition_error_type(f"{name} must remain exact non-empty text")
        return value

    def canonical_sha256(value: object, *, name: str) -> str:
        if type(value) is not str or (
            not value.startswith("sha256:")
            or len(value) != 71
            or value != value.lower()
            or any(char not in "0123456789abcdef" for char in value[7:])
        ):
            raise composition_error_type(
                f"{name} must remain exact canonical sha256 text"
            )
        return value

    def canonical_git_sha(value: object, *, name: str) -> str:
        if type(value) is not str or (
            len(value) != 40
            or value != value.lower()
            or any(char not in "0123456789abcdef" for char in value)
        ):
            raise composition_error_type(
                f"{name} must remain exact lowercase 40-character Git SHA"
            )
        return value

    def canonical_uuid(value: object, *, name: str) -> str:
        if type(value) is not str:
            raise composition_error_type(f"{name} must remain exact canonical UUID")
        try:
            parsed = UUID(value)
        except (AttributeError, TypeError, ValueError) as error:
            raise composition_error_type(
                f"{name} must remain exact canonical UUID"
            ) from error
        if str(parsed) != value:
            raise composition_error_type(f"{name} must remain exact canonical UUID")
        return value

    def snapshot_map(
        value: object,
        *,
        name: str,
        value_validator,
        expected_keys,
    ) -> dict[str, str]:
        if type(value) is not mapping_proxy_type:
            raise composition_error_type(
                f"{name} must remain exact immutable mapping state"
            )
        detached = dict(value)
        if frozenset(detached) != expected_keys:
            raise composition_error_type(
                f"{name} key set changed after canonical verification"
            )
        for key, item in detached.items():
            key = exact_text(key, name=f"{name} key")
            value_validator(item, name=f"{name}[{key}]")
        return detached

    def snapshot(value):
        if type(value) is not composed_type:
            raise composition_error_type(
                "composed verifier returned non-canonical accepted qualification"
            )
        accepted = value.qualification
        if type(accepted) is not accepted_type:
            raise composition_error_type(
                "composed verifier returned non-canonical signed acceptance"
            )
        accepted_snapshot = accepted_type(
            attestation_id=canonical_uuid(
                accepted.attestation_id,
                name="accepted target-host attestation_id",
            ),
            attestation_digest=canonical_sha256(
                accepted.attestation_digest,
                name="accepted target-host attestation_digest",
            ),
            source_sha=canonical_git_sha(
                accepted.source_sha,
                name="accepted target-host source_sha",
            ),
            scenario_id=exact_text(
                accepted.scenario_id,
                name="accepted target-host scenario_id",
            ),
            spec_digest=canonical_sha256(
                accepted.spec_digest,
                name="accepted target-host spec_digest",
            ),
            configuration_hash=canonical_sha256(
                accepted.configuration_hash,
                name="accepted target-host configuration_hash",
            ),
            host_fingerprint=canonical_sha256(
                accepted.host_fingerprint,
                name="accepted target-host host_fingerprint",
            ),
            workload_profile_hash=canonical_sha256(
                accepted.workload_profile_hash,
                name="accepted target-host workload_profile_hash",
            ),
            journal_store_identity_digest=canonical_sha256(
                accepted.journal_store_identity_digest,
                name="accepted target-host journal_store_identity_digest",
            ),
            release_artifact_id=canonical_uuid(
                accepted.release_artifact_id,
                name="accepted target-host release_artifact_id",
            ),
            release_artifact_sha256=canonical_sha256(
                accepted.release_artifact_sha256,
                name="accepted target-host release_artifact_sha256",
            ),
            binding_artifact_id=canonical_uuid(
                accepted.binding_artifact_id,
                name="accepted target-host binding_artifact_id",
            ),
            binding_sha256=canonical_sha256(
                accepted.binding_sha256,
                name="accepted target-host binding_sha256",
            ),
            evidence_sha256_by_kind=snapshot_map(
                accepted.evidence_sha256_by_kind,
                name="accepted target-host evidence digest map",
                value_validator=canonical_sha256,
                expected_keys=required_evidence_kinds,
            ),
            payload_artifact_id_by_kind=snapshot_map(
                accepted.payload_artifact_id_by_kind,
                name="accepted target-host payload artifact map",
                value_validator=canonical_uuid,
                expected_keys=provenance_kinds,
            ),
            payload_sha256_by_kind=snapshot_map(
                accepted.payload_sha256_by_kind,
                name="accepted target-host payload digest map",
                value_validator=canonical_sha256,
                expected_keys=provenance_kinds,
            ),
            collector_by_kind=snapshot_map(
                accepted.collector_by_kind,
                name="accepted target-host collector map",
                value_validator=exact_text,
                expected_keys=provenance_kinds,
            ),
        )
        return composed_type(
            qualification=accepted_snapshot,
            target_host_measurement_digest=canonical_sha256(
                value.target_host_measurement_digest,
                name="accepted target-host measurement digest",
            ),
            durable_financial_binding_digest=canonical_sha256(
                value.durable_financial_binding_digest,
                name="accepted durable financial binding digest",
            ),
            projection_sha256_by_kind=snapshot_map(
                value.projection_sha256_by_kind,
                name="accepted target-host projection map",
                value_validator=canonical_sha256,
                expected_keys=projection_kinds,
            ),
        )

    return snapshot


def _build_composed_acceptance_binding_validator(
    *,
    composed_type,
    accepted_type,
    composition_error_type,
    binding_evidence_kind,
    provenance_kinds,
    projection_kinds,
):
    """Preserve lower-verifier relationships after detaching its accepted output."""

    def validate(
        value,
        *,
        spec,
        durable_plan_digest: str,
        measurement,
        expected_release_artifact_id: str,
        expected_release_artifact_sha256: str,
    ):
        if type(value) is not composed_type or type(value.qualification) is not accepted_type:
            raise composition_error_type(
                "detached composed acceptance lost canonical result types"
            )
        accepted = value.qualification
        bindings = (
            ("source SHA", accepted.source_sha, spec.release_sha),
            ("scenario id", accepted.scenario_id, spec.scenario_id),
            ("spec digest", accepted.spec_digest, spec.digest),
            ("configuration hash", accepted.configuration_hash, spec.configuration_hash),
            ("host fingerprint", accepted.host_fingerprint, spec.host_fingerprint),
            ("workload identity", accepted.workload_profile_hash, durable_plan_digest),
            ("release artifact id", accepted.release_artifact_id, expected_release_artifact_id),
            (
                "release artifact digest",
                accepted.release_artifact_sha256,
                expected_release_artifact_sha256,
            ),
            ("measurement source SHA", accepted.source_sha, measurement.source_sha),
            ("measurement scenario id", accepted.scenario_id, measurement.scenario_id),
            ("measurement spec digest", accepted.spec_digest, measurement.spec_digest),
            (
                "measurement configuration hash",
                accepted.configuration_hash,
                measurement.configuration_hash,
            ),
            (
                "measurement host fingerprint",
                accepted.host_fingerprint,
                measurement.host_fingerprint,
            ),
            (
                "measurement workload identity",
                accepted.workload_profile_hash,
                measurement.workload_profile_hash,
            ),
            (
                "measurement JournalStore identity",
                accepted.journal_store_identity_digest,
                measurement.journal_store_identity_digest,
            ),
            (
                "measurement release artifact id",
                accepted.release_artifact_id,
                measurement.release_artifact_id,
            ),
            (
                "measurement release artifact digest",
                accepted.release_artifact_sha256,
                measurement.release_artifact_sha256,
            ),
            (
                "target-host measurement digest",
                value.target_host_measurement_digest,
                measurement.digest,
            ),
        )
        for name, observed, expected in bindings:
            if type(observed) is not str or type(expected) is not str or observed != expected:
                raise composition_error_type(
                    f"detached target-host {name} differs from captured authority"
                )

        evidence_digests = dict(accepted.evidence_sha256_by_kind)
        payload_ids = dict(accepted.payload_artifact_id_by_kind)
        payload_digests = dict(accepted.payload_sha256_by_kind)
        projections = dict(value.projection_sha256_by_kind)
        if accepted.binding_sha256 != evidence_digests[binding_evidence_kind]:
            raise composition_error_type(
                "detached target-host binding digest differs from accepted evidence map"
            )
        for kind in projection_kinds:
            if projections[kind] != payload_digests[kind]:
                raise composition_error_type(
                    f"detached target-host projection differs from retained payload for {kind}"
                )

        payload_id_values = tuple(payload_ids[kind] for kind in provenance_kinds)
        payload_digest_values = tuple(payload_digests[kind] for kind in provenance_kinds)
        if len(set(payload_id_values)) != len(provenance_kinds):
            raise composition_error_type(
                "detached target-host payload artifact identities are not independent"
            )
        if len(set(payload_digest_values)) != len(provenance_kinds):
            raise composition_error_type(
                "detached target-host payload digests are not independent"
            )
        if (
            accepted.release_artifact_id in payload_id_values
            or accepted.binding_artifact_id in payload_id_values
        ):
            raise composition_error_type(
                "detached target-host payload artifact aliases retained authority artifact"
            )
        if accepted.release_artifact_sha256 in payload_digest_values or (
            set(payload_digest_values) & set(evidence_digests.values())
        ):
            raise composition_error_type(
                "detached target-host payload bytes alias retained authority bytes"
            )
        return value

    return validate


_PRODUCTION_ACCEPTANCE_SNAPSHOTTER = _build_composed_acceptance_snapshotter(
    composed_type=AcceptedComposedRuntimeTargetHostQualification,
    accepted_type=AcceptedRuntimeTargetHostQualification,
    composition_error_type=RuntimeTargetHostCompositionError,
    mapping_proxy_type=type(MappingProxyType({})),
    required_evidence_kinds=_REQUIRED_ACCEPTED_EVIDENCE_KINDS,
    provenance_kinds=_PROVENANCE_ACCEPTED_KINDS,
    projection_kinds=_PROJECTION_ACCEPTED_KINDS,
)
_PRODUCTION_ACCEPTANCE_BINDING_VALIDATOR = _build_composed_acceptance_binding_validator(
    composed_type=AcceptedComposedRuntimeTargetHostQualification,
    accepted_type=AcceptedRuntimeTargetHostQualification,
    composition_error_type=RuntimeTargetHostCompositionError,
    binding_evidence_kind=BINDING_EVIDENCE_KIND,
    provenance_kinds=_PROVENANCE_ACCEPTED_KINDS,
    projection_kinds=_PROJECTION_ACCEPTED_KINDS,
)


def _build_chronology_free_verifier(
    *,
    journal_store_type=JournalStore,
    snapshot_budget_spec=_snapshot_budget_spec,
    require_journal_authority=require_exact_journal_store_authority,
    journal_authority_scope=journal_store_authority_scope,
    load_declared_plan=load_declared_runtime_event_plan,
    composition_error_type=RuntimeTargetHostCompositionError,
    canonical_sha256_text=_canonical_sha256_text,
    campaign_plan_type=RuntimeCampaignPlan,
    measurement_type=TargetHostMeasurementArtifact,
    measurement_error_type=RuntimeTargetHostMeasurementError,
    snapshot_measurement=None,
    campaign_cut_type=RuntimeCampaignCut,
    snapshot_campaign_cut=_snapshot_campaign_cut,
    verify_composed=verify_composed_runtime_target_host_qualification,
    acceptance_snapshotter=None,
    acceptance_binding_validator=None,
):
    """Capture the durable-plan/composed authority graph behind one private closure."""

    def verify_declared_plan_runtime_target_host_qualification_without_chronology(
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
        if type(journal_store) is not journal_store_type:
            raise TypeError("journal_store must be exact JournalStore")
        spec = snapshot_budget_spec(spec)

        selected_journal_identity = require_journal_authority(
            journal_store,
            subject="runtime target-host qualification JournalStore",
        )
        with journal_authority_scope(
            journal_store,
            selected_journal_identity,
        ):
            plan = load_declared_plan(
                journal_store,
                plan_id=plan_id,
                spec=spec,
            )
            if campaign_plan is None or campaign_cut is None or measurement is None:
                raise composition_error_type(
                    "terminal WP-65 qualification requires composed target-host measurement authority"
                )
            durable_plan_digest = canonical_sha256_text(
                plan.digest,
                name="durable pre-run plan digest",
            )
            if type(campaign_plan) is campaign_plan_type:
                campaign_workload = canonical_sha256_text(
                    campaign_plan.workload_profile_hash,
                    name="campaign workload identity",
                )
                if campaign_workload != durable_plan_digest:
                    raise composition_error_type(
                        "campaign workload identity does not match durable pre-run plan"
                    )
            if type(measurement) is measurement_type:
                if snapshot_measurement is not None:
                    try:
                        measurement = snapshot_measurement(measurement)
                    except measurement_error_type as error:
                        raise composition_error_type(str(error)) from error
                measurement_workload = canonical_sha256_text(
                    measurement.workload_profile_hash,
                    name="measurement workload identity",
                )
                if measurement_workload != durable_plan_digest:
                    raise composition_error_type(
                        "measurement workload identity does not match durable pre-run plan"
                    )
            if type(campaign_cut) is campaign_cut_type:
                campaign_cut = snapshot_campaign_cut(campaign_cut)
            accepted = verify_composed(
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
            if acceptance_snapshotter is not None:
                accepted = acceptance_snapshotter(accepted)
            if acceptance_binding_validator is not None:
                if type(measurement) is not measurement_type:
                    raise composition_error_type(
                        "terminal WP-65 acceptance binding requires canonical target-host measurement"
                    )
                accepted = acceptance_binding_validator(
                    accepted,
                    spec=spec,
                    durable_plan_digest=durable_plan_digest,
                    measurement=measurement,
                    expected_release_artifact_id=expected_release_artifact_id,
                    expected_release_artifact_sha256=expected_release_artifact_sha256,
                )

            final_plan = load_declared_plan(
                journal_store,
                plan_id=plan.plan_id,
                spec=spec,
            )
            final_plan_digest = canonical_sha256_text(
                final_plan.digest,
                name="revalidated durable pre-run plan digest",
            )
            if final_plan_digest != durable_plan_digest:
                raise composition_error_type(
                    "durable pre-run plan changed during terminal qualification"
                )
            final_journal_identity = require_journal_authority(
                journal_store,
                subject="runtime target-host qualification JournalStore",
            )
            if final_journal_identity != selected_journal_identity:
                raise RuntimeError(
                    "journal operation authority changed during terminal qualification"
                )
            return accepted

    return verify_declared_plan_runtime_target_host_qualification_without_chronology


# Canonical chronology-bound verification captures this closure during module
# initialization. All authority-bearing dependencies are captured here, so later
# module-global rebinding cannot redirect durable plan, JournalStore, measurement,
# composed qualification, accepted-output snapshot, or output-binding authority.
_verify_declared_plan_runtime_target_host_qualification_without_chronology = (
    _build_chronology_free_verifier(
        snapshot_measurement=snapshot_target_host_measurement,
        acceptance_snapshotter=_PRODUCTION_ACCEPTANCE_SNAPSHOTTER,
        acceptance_binding_validator=_PRODUCTION_ACCEPTANCE_BINDING_VALIDATOR,
    )
)


def _terminal_chronology_dispatch_authority():
    """Return one write-once binder and one closure-private terminal dispatcher."""

    verifier = None

    def bind(candidate) -> None:
        nonlocal verifier
        if not callable(candidate):
            raise TypeError("terminal chronology verifier must be callable")
        if verifier is None:
            verifier = candidate
            return
        if verifier is not candidate:
            raise RuntimeError("terminal chronology verifier is already bound")

    def dispatch(*args, **kwargs):
        nonlocal verifier
        if verifier is None:
            # Import for initialization side effects only. The chronology module
            # binds the canonical verifier into this closure exactly once.
            from . import runtime_target_host_chronology_bound_qualification as _chronology_bound

            del _chronology_bound
        selected = verifier
        if selected is None:
            raise RuntimeError("terminal chronology verifier is unavailable")
        return selected(*args, **kwargs)

    return bind, dispatch


_bind_terminal_chronology_verifier, _terminal_chronology_dispatch = (
    _terminal_chronology_dispatch_authority()
)


def _build_product_verifier(
    terminal_chronology_dispatch,
    *,
    signed_receipt_type=SignedQualificationAttestation,
    composition_error_type=RuntimeTargetHostCompositionError,
    chronology_free_verifier=_verify_declared_plan_runtime_target_host_qualification_without_chronology,
):
    """Capture every authority-bearing product dependency before module rebinding."""

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
        """Verify product-facing WP-65 admission, requiring chronology for real receipts."""

        chronology_supplied = (
            recovery is not None or runtime is not None or chronology_cut is not None
        )
        chronology_complete = (
            recovery is not None and runtime is not None and chronology_cut is not None
        )
        if chronology_supplied and not chronology_complete:
            raise composition_error_type(
                "terminal WP-65 qualification requires complete RELEASE_RUNTIME chronology authority"
            )

        if chronology_supplied:
            return terminal_chronology_dispatch(
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

        if type(receipt) is signed_receipt_type:
            raise composition_error_type(
                "terminal WP-65 qualification requires accepted RELEASE_RUNTIME chronology authority"
            )

        return chronology_free_verifier(
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

    return verify_declared_plan_runtime_target_host_qualification


verify_declared_plan_runtime_target_host_qualification = _build_product_verifier(
    _terminal_chronology_dispatch,
    signed_receipt_type=SignedQualificationAttestation,
    composition_error_type=RuntimeTargetHostCompositionError,
    chronology_free_verifier=_verify_declared_plan_runtime_target_host_qualification_without_chronology,
)

# Complete canonical terminal-verifier binding during module import. Leaving this
# until the first product call creates a pre-initialization window where external
# code can invoke the otherwise write-once private binder with a forged verifier.
# Importing here closes that window before this module becomes externally usable.
from . import runtime_target_host_chronology_bound_qualification as _chronology_bound

del _chronology_bound
