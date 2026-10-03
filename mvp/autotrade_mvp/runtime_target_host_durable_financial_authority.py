"""Captured durable-financial authority for terminal WP-65 qualification.

The public durable-financial functions remain explicit focused-test seams. Product
qualification uses the closures constructed here so rebinding direct measurement,
JournalStore, plan/sample loading, parent-evidence, or durable-mechanics symbols
after import cannot retarget terminal PASS.

These captures close only the direct dependency edges named by each factory. The
captured helpers/loaders and canonical data types retain their own transitive
implementation boundaries, which must be falsified separately.

This module does not create provider truth, chronology, signer policy, release
authority, trading authority, profitability, or economic edge.
"""

from __future__ import annotations

import sys
from uuid import UUID

from .performance_qualification import RuntimeBudgetError, RuntimeBudgetSpec
from .persistence import (
    JournalStore,
    journal_store_authority_scope,
    require_exact_journal_store_authority,
)
from .runtime_load_measurement import (
    DurableFinancialLatencySample,
    load_declared_financial_latency_samples,
)
from .runtime_load_plan import load_declared_runtime_event_plan
from .runtime_load_qualification import RuntimeCampaignCut, RuntimeCampaignPlan
from .runtime_target_host_durable_financial import (
    CLOCK_CONTRACT_ID,
    TARGET_HOST_SHARED_CLOCK_ID,
    DurableTargetHostFinancialBinding,
    RuntimeTargetHostDurableFinancialError,
    _binding_from_durable_sample,
)
from .runtime_target_host_measurement import (
    RuntimeTargetHostMeasurementError,
    TargetHostMeasurementArtifact,
    snapshot_target_host_measurement,
)
from .runtime_target_host_measurement_authority import (
    collect_release_bound_target_host_evidence,
)


def _build_low_level_durable_financial_authority(
    *,
    python_version,
    measurement_type,
    measurement_snapshotter,
    target_clock_id,
    budget_spec_type,
    durable_sample_type,
    durable_error_type,
    require_journal_authority,
    journal_authority_scope,
    plan_loader,
    durable_samples_loader,
    binding_from_sample,
    binding_factory,
    clock_contract_id,
):
    """Build the durable JournalStore/measurement join from captured direct edges."""

    def snapshot_spec(value):
        if type(value) is not budget_spec_type:
            raise TypeError("spec must be exact RuntimeBudgetSpec")
        return budget_spec_type(
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

    def canonical_text(value: object, *, name: str) -> str:
        if type(value) is not str or not value or value != value.strip():
            raise durable_error_type(f"{name} must be canonical non-empty text")
        return value

    def bind(
        store: JournalStore,
        spec: RuntimeBudgetSpec,
        *,
        declared_plan_id: str,
        measurement: TargetHostMeasurementArtifact,
    ) -> DurableTargetHostFinancialBinding:
        if python_version < (3, 13):
            raise durable_error_type(
                "durable latency cannot join target-host campaign cuts before Python 3.13 "
                "because perf_counter and monotonic are not one specified clock"
            )
        if type(measurement) is not measurement_type:
            raise TypeError("measurement must be exact TargetHostMeasurementArtifact")
        measurement_authority = measurement_snapshotter(measurement)
        if measurement_authority.monotonic_clock_id != target_clock_id:
            raise durable_error_type(
                "target-host measurement does not declare canonical time.monotonic_ns authority"
            )

        spec_authority = snapshot_spec(spec)
        plan_id = canonical_text(declared_plan_id, name="declared_plan_id")
        spec_digest = spec_authority.digest
        if measurement_authority.source_sha != spec_authority.release_sha:
            raise durable_error_type(
                "target-host measurement source SHA conflicts with runtime budget"
            )
        if measurement_authority.scenario_id != spec_authority.scenario_id:
            raise durable_error_type(
                "target-host measurement scenario conflicts with runtime budget"
            )
        if measurement_authority.spec_digest != spec_digest:
            raise durable_error_type(
                "target-host measurement spec digest conflicts with runtime budget"
            )
        if (
            measurement_authority.configuration_hash
            != spec_authority.configuration_hash
        ):
            raise durable_error_type(
                "target-host measurement configuration conflicts with runtime budget"
            )
        if measurement_authority.host_fingerprint != spec_authority.host_fingerprint:
            raise durable_error_type(
                "target-host measurement host conflicts with runtime budget"
            )

        store_identity = require_journal_authority(
            store,
            subject="runtime qualification JournalStore",
        )
        with journal_authority_scope(store, store_identity):
            declared_plan = plan_loader(
                store,
                plan_id=plan_id,
                spec=spec_authority,
            )
            if (
                measurement_authority.journal_store_identity_digest
                != declared_plan.store_identity_digest
            ):
                raise durable_error_type(
                    "target-host measurement belongs to another JournalStore generation"
                )
            if (
                declared_plan.declared_journal_sequence
                > measurement_authority.start_journal_sequence
            ):
                raise durable_error_type(
                    "durable runtime event plan was declared after target-host campaign start"
                )
            durable_samples = durable_samples_loader(
                store,
                spec_authority,
                plan_id=plan_id,
            )
            if (
                require_journal_authority(
                    store,
                    subject="runtime qualification JournalStore",
                )
                != store_identity
            ):
                raise durable_error_type(
                    "JournalStore generation changed during durable financial binding"
                )

        declared_plan_digest = declared_plan.digest
        for durable_sample in durable_samples:
            if type(durable_sample) is not durable_sample_type:
                raise TypeError(
                    "durable samples must contain exact DurableFinancialLatencySample"
                )
            if (
                durable_sample.plan_id != declared_plan.plan_id
                or durable_sample.plan_digest != declared_plan_digest
            ):
                raise durable_error_type(
                    "durable latency sample plan identity conflicts with declared plan"
                )

        target_samples = measurement_authority.financial_samples
        if type(target_samples) is not tuple:
            raise durable_error_type(
                "target-host financial samples must remain an exact tuple"
            )
        if len(durable_samples) != len(target_samples):
            raise durable_error_type(
                "target-host financial sample count does not match durable plan"
            )
        expected_event_ids = tuple(
            value.event_id for value in declared_plan.expected_events
        )
        measured_event_ids = tuple(value.event_id for value in target_samples)
        if measured_event_ids != expected_event_ids:
            raise durable_error_type(
                "target-host financial event identities do not match durable plan"
            )

        bindings = []
        for durable_sample, target in zip(
            durable_samples,
            target_samples,
            strict=True,
        ):
            if target.event_id != durable_sample.event_id:
                raise durable_error_type(
                    "target-host financial event ID conflicts with durable latency"
                )
            if target.journal_sequence != durable_sample.event_journal_sequence:
                raise durable_error_type(
                    "target-host financial journal sequence conflicts with durable latency"
                )
            if not (
                measurement_authority.start_journal_sequence
                < durable_sample.event_journal_sequence
                < durable_sample.measurement_journal_sequence
                <= measurement_authority.end_journal_sequence
            ):
                raise durable_error_type(
                    "durable latency dependency lies outside target-host journal cut"
                )
            if (
                target.latency_start_monotonic_ns
                != durable_sample.monotonic_start_ns
                or target.latency_end_monotonic_ns
                != durable_sample.monotonic_end_ns
            ):
                raise durable_error_type(
                    "target-host raw latency endpoints conflict with durable latency"
                )
            start_ns = target.latency_start_monotonic_ns
            end_ns = target.latency_end_monotonic_ns
            if (
                type(start_ns) is not int
                or type(end_ns) is not int
                or start_ns < 0
                or end_ns < start_ns
            ):
                raise durable_error_type(
                    "target-host raw latency endpoints are non-canonical"
                )
            target_latency_us = (end_ns - start_ns + 999) // 1_000
            if target_latency_us != durable_sample.latency_us:
                raise durable_error_type(
                    "target-host latency duration conflicts with durable latency"
                )
            bindings.append(
                binding_from_sample(
                    durable_sample,
                    target_sample_id=target.sample_id,
                )
            )

        return binding_factory(
            target_host_measurement_digest=measurement_authority.digest,
            source_sha=spec_authority.release_sha,
            spec_digest=spec_digest,
            declared_plan_id=declared_plan.plan_id,
            declared_plan_digest=declared_plan_digest,
            clock_contract_id=clock_contract_id,
            bindings=tuple(bindings),
        )

    return bind


bind_sealed_durable_financial_latency_to_target_host_measurement = (
    _build_low_level_durable_financial_authority(
        python_version=(sys.version_info.major, sys.version_info.minor),
        measurement_type=TargetHostMeasurementArtifact,
        measurement_snapshotter=snapshot_target_host_measurement,
        target_clock_id=TARGET_HOST_SHARED_CLOCK_ID,
        budget_spec_type=RuntimeBudgetSpec,
        durable_sample_type=DurableFinancialLatencySample,
        durable_error_type=RuntimeTargetHostDurableFinancialError,
        require_journal_authority=require_exact_journal_store_authority,
        journal_authority_scope=journal_store_authority_scope,
        plan_loader=load_declared_runtime_event_plan,
        durable_samples_loader=load_declared_financial_latency_samples,
        binding_from_sample=_binding_from_durable_sample,
        binding_factory=DurableTargetHostFinancialBinding,
        clock_contract_id=CLOCK_CONTRACT_ID,
    )
)


def _build_release_bound_durable_financial_authority(
    *,
    measurement_type,
    measurement_snapshotter,
    uuid_type,
    durable_error_type,
    parent_collector,
    parent_error_types,
    durable_binder,
):
    """Build one release-bound binder with immutable direct dependency captures."""

    def canonical_uuid(value: object, *, name: str) -> str:
        if type(value) is not str or not value or value != value.strip():
            raise durable_error_type(f"{name} must be a canonical UUID")
        try:
            canonical = str(uuid_type(value))
        except (ValueError, TypeError, AttributeError) as error:
            raise durable_error_type(f"{name} must be a canonical UUID") from error
        if canonical != value:
            raise durable_error_type(f"{name} must be a canonical UUID")
        return canonical

    def canonical_digest(value: object, *, name: str) -> str:
        if (
            type(value) is not str
            or len(value) != 71
            or not value.startswith("sha256:")
            or value != value.lower()
            or any(char not in "0123456789abcdef" for char in value[7:])
        ):
            raise durable_error_type(f"{name} must be canonical sha256:<64 hex>")
        return value

    def bind(
        *,
        store: JournalStore,
        spec: RuntimeBudgetSpec,
        campaign_plan: RuntimeCampaignPlan,
        campaign_cut: RuntimeCampaignCut,
        declared_plan_id: str,
        measurement: TargetHostMeasurementArtifact,
        expected_release_artifact_id: str,
        expected_release_artifact_sha256: str,
    ) -> DurableTargetHostFinancialBinding:
        if type(measurement) is not measurement_type:
            raise TypeError("measurement must be exact TargetHostMeasurementArtifact")
        measurement_authority = measurement_snapshotter(measurement)
        frozen_release_artifact_id = canonical_uuid(
            expected_release_artifact_id,
            name="expected_release_artifact_id",
        )
        frozen_release_artifact_sha256 = canonical_digest(
            expected_release_artifact_sha256,
            name="expected_release_artifact_sha256",
        )
        if measurement_authority.release_artifact_id != frozen_release_artifact_id:
            raise durable_error_type(
                "target-host measurement belongs to another delivered release artifact"
            )
        if (
            measurement_authority.release_artifact_sha256
            != frozen_release_artifact_sha256
        ):
            raise durable_error_type(
                "target-host measurement belongs to another delivered release digest"
            )

        try:
            parent_collector(
                journal=store,
                spec=spec,
                plan=campaign_plan,
                cut=campaign_cut,
                measurement=measurement_authority,
                expected_release_artifact_id=frozen_release_artifact_id,
                expected_release_artifact_sha256=frozen_release_artifact_sha256,
            )
        except parent_error_types as error:
            raise durable_error_type(str(error)) from error

        return durable_binder(
            store,
            spec,
            declared_plan_id=declared_plan_id,
            measurement=measurement_authority,
        )

    return bind


bind_sealed_release_bound_durable_financial_latency_to_target_host_measurement = (
    _build_release_bound_durable_financial_authority(
        measurement_type=TargetHostMeasurementArtifact,
        measurement_snapshotter=snapshot_target_host_measurement,
        uuid_type=UUID,
        durable_error_type=RuntimeTargetHostDurableFinancialError,
        parent_collector=collect_release_bound_target_host_evidence,
        parent_error_types=(RuntimeTargetHostMeasurementError, RuntimeBudgetError),
        durable_binder=(
            bind_sealed_durable_financial_latency_to_target_host_measurement
        ),
    )
)
