"""Captured production authority graph for composed WP-65 qualification.

The public composed verifier remains an explicit focused-test seam. Product
admission imports the verifier from this module so rebinding the composed module's
binder, signed verifier, raw campaign reader/parser, budget evaluator, projection
helpers, snapshotters, or result type after import cannot retarget the constructed
production authority path.

This module creates no chronology source, signer/trust root, release authority,
provider/PAPER/LIVE send authority, broker/exchange authority, trading authority,
profitability claim, or economic-edge claim.
"""

from __future__ import annotations

from hashlib import sha256
import json
from types import MappingProxyType

from autotrade_runtime.artifacts import (
    ArtifactIntegrityError,
    ArtifactStore,
    trusted_authenticated_reader,
)

from .performance_qualification import (
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
from .runtime_target_host_composed_qualification import (
    AcceptedComposedRuntimeTargetHostQualification,
    PROJECTION_SCHEMA_VERSION,
    RuntimeTargetHostCompositionError,
    _snapshot_campaign_cut,
    _snapshot_campaign_plan,
    _snapshot_measurement,
    _snapshot_spec,
)
from .runtime_target_host_durable_financial import DurableTargetHostFinancialBinding
from .runtime_target_host_durable_financial_authority import (
    bind_sealed_release_bound_durable_financial_latency_to_target_host_measurement,
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


def _build_durable_plan_matcher(
    *,
    durable_binding_type,
    composition_error_type,
):
    """Build the workload join without mutable composed-module lookups."""

    def require_match(durable_binding, measurement) -> None:
        if type(durable_binding) is not durable_binding_type:
            raise composition_error_type(
                "release-bound durable binder returned non-canonical binding"
            )
        declared_plan_digest = durable_binding.declared_plan_digest
        if declared_plan_digest != measurement.workload_profile_hash:
            raise composition_error_type(
                "durable pre-run plan identity does not match canonical target-host workload identity"
            )

    return require_match


def _build_signed_campaign_matcher(
    *,
    authenticated_reader_factory,
    artifact_integrity_error_type,
    parsed_campaign_type,
    campaign_error_type,
    budget_spec_type,
    budget_evaluator,
    budget_error_type,
    composition_error_type,
    campaign_evidence_kind,
    sha256_factory,
):
    """Capture signed campaign re-read, parse, hash, and budget policy authority."""

    def require_match(
        accepted,
        *,
        evidence_store,
        evidence_root,
        measurement,
        campaign_plan,
        spec=None,
    ):
        artifact_id = accepted.payload_artifact_id_by_kind.get(campaign_evidence_kind)
        expected_sha256 = accepted.payload_sha256_by_kind.get(campaign_evidence_kind)
        if type(artifact_id) is not str or not artifact_id:
            raise composition_error_type(
                f"signed {campaign_evidence_kind} raw payload artifact identity is missing"
            )
        if type(expected_sha256) is not str or not expected_sha256:
            raise composition_error_type(
                f"signed {campaign_evidence_kind} raw payload digest is missing"
            )
        try:
            reader = authenticated_reader_factory(
                evidence_root,
                publication_store=evidence_store,
            )
            _manifest, raw = reader(artifact_id)
        except (
            artifact_integrity_error_type,
            FileNotFoundError,
            OSError,
            TypeError,
            ValueError,
        ) as error:
            raise composition_error_type(
                f"signed {campaign_evidence_kind} raw payload cannot be re-read with integrity"
            ) from error
        if type(raw) is not bytes or not raw:
            raise composition_error_type(
                f"signed {campaign_evidence_kind} raw payload is empty or non-bytes"
            )
        if "sha256:" + sha256_factory(raw).hexdigest() != expected_sha256:
            raise composition_error_type(
                f"signed {campaign_evidence_kind} raw payload changed after canonical verification"
            )

        try:
            parsed = parsed_campaign_type.parse(raw)
        except campaign_error_type as error:
            raise composition_error_type(
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
            raise composition_error_type(
                "signed campaign metric series do not match canonical target-host measurement"
            )
        if observation.declared_duration_us != expected_declared_duration_us:
            raise composition_error_type(
                "signed campaign declared duration does not match canonical campaign plan"
            )
        if observation.observed_duration_us is None:
            raise composition_error_type(
                "signed campaign lacks observed target-host duration"
            )
        if (
            parsed.evidence.journal_sequence_before != measurement.start_journal_sequence
            or parsed.evidence.journal_sequence_after != measurement.end_journal_sequence
        ):
            raise composition_error_type(
                "signed campaign journal cut does not match canonical target-host measurement"
            )
        if (
            parsed.evidence.recovered_event_ids != expected_ids
            or parsed.evidence.recovered_journal_sequences != expected_sequences
        ):
            raise composition_error_type(
                "signed campaign financial identities do not match canonical target-host measurement"
            )

        if spec is None:
            return None
        if type(spec) is not budget_spec_type:
            raise TypeError("spec must be exact RuntimeBudgetSpec")
        try:
            decision = budget_evaluator(spec, observation)
        except budget_error_type as error:
            raise composition_error_type(
                "signed campaign cannot be evaluated by canonical runtime budget policy"
            ) from error
        if decision.status != "PASS" or decision.reasons:
            reasons = ",".join(decision.reasons) if decision.reasons else "none"
            raise composition_error_type(
                "canonical runtime budget decision is not PASS: "
                f"status={decision.status} reasons={reasons}"
            )
        return decision

    return require_match


def _build_projection_digest_builder(
    *,
    projection_schema_version,
    staleness_kind,
    interference_kind,
    resource_kind,
    json_dumps,
    sha256_factory,
    mapping_proxy_factory,
    composition_error_type,
):
    """Build canonical projection digests without mutable module helper lookups."""

    kinds = frozenset({staleness_kind, interference_kind, resource_kind})

    def canonical_json(value: object) -> bytes:
        return json_dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")

    def projection_identity(measurement, *, evidence_kind: str) -> dict[str, object]:
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
            "projection_schema_version": projection_schema_version,
            "release_artifact_id": measurement.release_artifact_id,
            "release_artifact_sha256": measurement.release_artifact_sha256,
            "scenario_id": measurement.scenario_id,
            "source_sha": measurement.source_sha,
            "spec_digest": measurement.spec_digest,
            "start_journal_sequence": measurement.start_journal_sequence,
            "target_host_measurement_digest": measurement.digest,
            "workload_profile_hash": measurement.workload_profile_hash,
        }

    def projection_bytes(measurement, *, evidence_kind: str) -> bytes:
        if type(evidence_kind) is not str or evidence_kind not in kinds:
            raise composition_error_type(
                "evidence_kind must be a canonical target-host measurement projection kind"
            )
        payload = projection_identity(measurement, evidence_kind=evidence_kind)
        if evidence_kind == staleness_kind:
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
        elif evidence_kind == interference_kind:
            payload["basis"] = measurement.research_interference_basis
            payload["samples"] = [
                sample.canonical_payload() for sample in measurement.research_samples
            ]
        else:
            payload["samples"] = [
                sample.canonical_payload() for sample in measurement.resource_samples
            ]
        return canonical_json(payload)

    def digests(measurement):
        return mapping_proxy_factory(
            {
                kind: "sha256:" + sha256_factory(
                    projection_bytes(measurement, evidence_kind=kind)
                ).hexdigest()
                for kind in sorted(kinds)
            }
        )

    return digests


def _build_composed_production_verifier(
    *,
    measurement_snapshotter,
    spec_snapshotter,
    campaign_plan_snapshotter,
    campaign_cut_type,
    campaign_cut_snapshotter,
    durable_binder,
    composition_error_type,
    durable_plan_matcher,
    signed_verifier,
    accepted_type,
    signed_campaign_matcher,
    projection_digest_builder,
    composed_type,
):
    """Build a composed verifier whose direct dependencies are immutable captures."""

    def verify(
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
        measurement_authority = measurement_snapshotter(measurement)
        spec_authority = spec_snapshotter(spec)
        campaign_plan_authority = campaign_plan_snapshotter(campaign_plan)
        campaign_cut_authority = campaign_cut
        if type(campaign_cut_authority) is campaign_cut_type:
            campaign_cut_authority = campaign_cut_snapshotter(campaign_cut_authority)

        durable_binding = durable_binder(
            store=journal_store,
            spec=spec_authority,
            campaign_plan=campaign_plan_authority,
            campaign_cut=campaign_cut_authority,
            declared_plan_id=declared_plan_id,
            measurement=measurement_authority,
            expected_release_artifact_id=expected_release_artifact_id,
            expected_release_artifact_sha256=expected_release_artifact_sha256,
        )
        if (
            durable_binding.target_host_measurement_digest
            != measurement_authority.digest
            or durable_binding.source_sha != measurement_authority.source_sha
            or durable_binding.spec_digest != measurement_authority.spec_digest
        ):
            raise composition_error_type(
                "durable financial binding does not bind canonical target-host measurement"
            )
        durable_plan_matcher(durable_binding, measurement_authority)

        accepted = signed_verifier(
            receipt,
            evidence_store=evidence_store,
            evidence_root=evidence_root,
            expected_source_sha=measurement_authority.source_sha,
            expected_scenario_id=measurement_authority.scenario_id,
            expected_spec_digest=measurement_authority.spec_digest,
            expected_configuration_hash=measurement_authority.configuration_hash,
            expected_host_fingerprint=measurement_authority.host_fingerprint,
            expected_workload_profile_hash=measurement_authority.workload_profile_hash,
            expected_journal_store_identity_digest=(
                measurement_authority.journal_store_identity_digest
            ),
            expected_release_artifact_id=expected_release_artifact_id,
            expected_release_artifact_sha256=expected_release_artifact_sha256,
        )
        if type(accepted) is not accepted_type:
            raise composition_error_type(
                "signed target-host verifier returned non-canonical acceptance"
            )

        signed_campaign_matcher(
            accepted,
            evidence_store=evidence_store,
            evidence_root=evidence_root,
            measurement=measurement_authority,
            campaign_plan=campaign_plan_authority,
            spec=spec_authority,
        )

        projection_digests = projection_digest_builder(measurement_authority)
        for kind, expected_digest in projection_digests.items():
            actual_digest = accepted.payload_sha256_by_kind.get(kind)
            if actual_digest != expected_digest:
                raise composition_error_type(
                    f"signed {kind} raw payload does not bind canonical target-host measurement"
                )

        return composed_type(
            qualification=accepted,
            target_host_measurement_digest=measurement_authority.digest,
            durable_financial_binding_digest=durable_binding.digest,
            projection_sha256_by_kind=projection_digests,
        )

    return verify


_PRODUCTION_DURABLE_PLAN_MATCHER = _build_durable_plan_matcher(
    durable_binding_type=DurableTargetHostFinancialBinding,
    composition_error_type=RuntimeTargetHostCompositionError,
)
_PRODUCTION_SIGNED_CAMPAIGN_MATCHER = _build_signed_campaign_matcher(
    authenticated_reader_factory=trusted_authenticated_reader,
    artifact_integrity_error_type=ArtifactIntegrityError,
    parsed_campaign_type=ParsedRuntimeTargetHostCampaign,
    campaign_error_type=RuntimeTargetHostCampaignError,
    budget_spec_type=RuntimeBudgetSpec,
    budget_evaluator=evaluate_runtime_budget,
    budget_error_type=RuntimeBudgetError,
    composition_error_type=RuntimeTargetHostCompositionError,
    campaign_evidence_kind=CAMPAIGN_EVIDENCE_KIND,
    sha256_factory=sha256,
)
_PRODUCTION_PROJECTION_DIGEST_BUILDER = _build_projection_digest_builder(
    projection_schema_version=PROJECTION_SCHEMA_VERSION,
    staleness_kind=STALENESS_EVIDENCE_KIND,
    interference_kind=INTERFERENCE_EVIDENCE_KIND,
    resource_kind=RESOURCE_EVIDENCE_KIND,
    json_dumps=json.dumps,
    sha256_factory=sha256,
    mapping_proxy_factory=MappingProxyType,
    composition_error_type=RuntimeTargetHostCompositionError,
)

verify_sealed_composed_runtime_target_host_qualification = (
    _build_composed_production_verifier(
        measurement_snapshotter=_snapshot_measurement,
        spec_snapshotter=_snapshot_spec,
        campaign_plan_snapshotter=_snapshot_campaign_plan,
        campaign_cut_type=RuntimeCampaignCut,
        campaign_cut_snapshotter=_snapshot_campaign_cut,
        durable_binder=(
            bind_sealed_release_bound_durable_financial_latency_to_target_host_measurement
        ),
        composition_error_type=RuntimeTargetHostCompositionError,
        durable_plan_matcher=_PRODUCTION_DURABLE_PLAN_MATCHER,
        signed_verifier=verify_runtime_target_host_qualification,
        accepted_type=AcceptedRuntimeTargetHostQualification,
        signed_campaign_matcher=_PRODUCTION_SIGNED_CAMPAIGN_MATCHER,
        projection_digest_builder=_PRODUCTION_PROJECTION_DIGEST_BUILDER,
        composed_type=AcceptedComposedRuntimeTargetHostQualification,
    )
)
