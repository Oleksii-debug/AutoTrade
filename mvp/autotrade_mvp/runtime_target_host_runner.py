"""Executable provider-free target-host campaign runner for WP-65.

This module closes the gap between the already durable pre-run event plan,
monotonic financial-operation measurement, raw target-host measurement artifact,
and canonical runtime-budget evaluator.  It deliberately does not invent a
financial workload, provider/exchange input, signer, chronology source, release
or trading authority.

The caller supplies the exact product operations named by a *durably declared*
event plan.  The runner owns timing and readback: operations cannot supply
latency/staleness integers, and a planned event must actually appear in the exact
JournalStore during its measured operation.  Research-pressure callbacks are
measured as explicit host-monotonic intervals; they may exercise CPU/model/disk
pressure but cannot satisfy or hide financial event conservation.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import sys
import threading
import time
from typing import Callable, Mapping

from .performance_qualification import (
    RuntimeBudgetDecision,
    RuntimeBudgetSpec,
    evaluate_runtime_budget,
)
from .persistence import JournalStore, require_exact_journal_store_authority
from .runtime_load_campaign import RuntimeLoadCampaignEvidence
from .runtime_load_measurement import (
    DurableFinancialLatencySample,
    measure_declared_financial_operation,
)
from .runtime_load_plan import (
    DeclaredRuntimeEventPlan,
    load_declared_runtime_event_plan,
)
from .runtime_load_qualification import (
    RuntimeCampaignCut,
    RuntimeCampaignEvidence,
    RuntimeCampaignPlan,
    begin_runtime_campaign,
)
from .runtime_target_host_campaign import ParsedRuntimeTargetHostCampaign
from .runtime_target_host_inventory import (
    RuntimeTargetHostInventory,
    collect_runtime_target_host_inventory,
)
from .runtime_target_host_measurement import (
    FinancialTargetHostSample,
    MONOTONIC_CLOCK_ID,
    ResearchInterferenceSample,
    ResourceTargetHostSample,
    TargetHostMeasurementArtifact,
    collect_runtime_campaign_evidence_from_measurement_artifact,
)


class RuntimeTargetHostRunnerError(ValueError):
    """Raised when an executable target-host campaign cannot be established."""


STALENESS_BASIS = "declared-work-ready-to-durable-financial-event-monotonic-v1"
RESEARCH_INTERFERENCE_BASIS = "measured-research-pressure-active-interval-monotonic-v1"


def _require_shared_clock_contract() -> None:
    """Require the language-level perf_counter/monotonic single-clock contract."""

    version = sys.version_info
    if (version.major, version.minor) < (3, 13):
        raise RuntimeTargetHostRunnerError(
            "executable target-host runner requires Python 3.13+ because durable "
            "latency perf_counter_ns and target-host monotonic_ns must share one "
            "specified clock"
        )


def _positive_int(value: object, *, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise RuntimeTargetHostRunnerError(f"{name} must be a positive integer")
    return value


def _non_negative_int(value: object, *, name: str) -> int:
    if type(value) is not int or value < 0:
        raise RuntimeTargetHostRunnerError(f"{name} must be a non-negative integer")
    return value


def _text(value: object, *, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise RuntimeTargetHostRunnerError(f"{name} must be canonical non-empty text")
    return value


def _snapshot_operations(
    plan: DeclaredRuntimeEventPlan,
    operations: object,
) -> tuple[tuple[str, Callable[[], object]], ...]:
    if type(operations) is not dict:
        raise RuntimeTargetHostRunnerError(
            "operations must be an exact dict keyed by durable planned event_id"
        )
    expected_ids = plan.expected_event_ids
    if set(operations) != set(expected_ids):
        raise RuntimeTargetHostRunnerError(
            "operation key set must exactly match the durable pre-run event plan"
        )
    result: list[tuple[str, Callable[[], object]]] = []
    for event_id in expected_ids:
        operation = operations[event_id]
        if not callable(operation):
            raise RuntimeTargetHostRunnerError(
                f"operation for {event_id} must be callable"
            )
        result.append((event_id, operation))
    return tuple(result)


def _snapshot_research_operations(
    value: object,
    *,
    minimum: int,
) -> tuple[tuple[str, Callable[[], object]], ...]:
    if type(value) is not tuple:
        raise RuntimeTargetHostRunnerError(
            "research_operations must be an exact tuple"
        )
    result: list[tuple[str, Callable[[], object]]] = []
    for item in value:
        if type(item) is not tuple or len(item) != 2:
            raise RuntimeTargetHostRunnerError(
                "each research operation must be exact (phase, callable) tuple"
            )
        phase = _text(item[0], name="research phase")
        operation = item[1]
        if not callable(operation):
            raise RuntimeTargetHostRunnerError(
                "research operation must be callable"
            )
        result.append((phase, operation))
    if len(result) < minimum:
        raise RuntimeTargetHostRunnerError(
            "research operations do not satisfy the declared minimum sample count"
        )
    return tuple(result)


def _snapshot_resource_probe(value: object) -> Callable[[], Mapping[str, int]] | None:
    if value is None:
        return None
    if not callable(value):
        raise RuntimeTargetHostRunnerError("resource_probe must be callable or None")
    return value


def _capture_resource_metrics(
    store: JournalStore,
    resource_probe: Callable[[], Mapping[str, int]] | None,
) -> dict[str, int]:
    """Capture portable runner metrics plus optional target-host collector metrics.

    A resource probe is observation-only: any JournalStore mutation during the
    callback invalidates the sample.  Returned values must be exact non-negative
    integers and cannot overwrite runner-owned metric keys.
    """

    before = JournalStore.current_journal_sequence(store)
    extras: dict[str, int] = {}
    if resource_probe is not None:
        raw = resource_probe()
        if type(raw) is not dict:
            raise RuntimeTargetHostRunnerError(
                "resource_probe must return an exact dict"
            )
        for key, metric in raw.items():
            name = _text(key, name="resource metric")
            extras[name] = _non_negative_int(metric, name=f"resource metric {name}")
    after = JournalStore.current_journal_sequence(store)
    if after != before:
        raise RuntimeTargetHostRunnerError(
            "resource probe mutated or raced the durable JournalStore"
        )

    builtins = {
        "active_threads": threading.active_count(),
        "journal_sequence": before,
        "pending_outbox": JournalStore.pending_outbox_count(store),
        "process_cpu_time_ns": time.process_time_ns(),
    }
    for key, metric in builtins.items():
        _non_negative_int(metric, name=key)
    collisions = set(extras) & set(builtins)
    if collisions:
        raise RuntimeTargetHostRunnerError(
            "resource_probe cannot replace runner-owned metrics: "
            + ",".join(sorted(collisions))
        )
    return {**builtins, **extras}


def run_cpu_pressure_probe(*, iterations: int = 10_000) -> str:
    """Run a bounded provider-free CPU pressure quantum and return its digest.

    The result is diagnostic only.  The runner measures the surrounding active
    interval; this helper creates no financial/provider state and performs no I/O.
    """

    count = _positive_int(iterations, name="iterations")
    state = b"autotrade-wp65-target-host-pressure-v1"
    for index in range(count):
        state = sha256(state + index.to_bytes(8, "big", signed=False)).digest()
    return state.hex()


@dataclass(frozen=True, slots=True)
class RuntimeTargetHostRunResult:
    declared_plan: DeclaredRuntimeEventPlan
    campaign_plan: RuntimeCampaignPlan
    campaign_cut: RuntimeCampaignCut
    measurement: TargetHostMeasurementArtifact
    campaign_evidence: RuntimeCampaignEvidence
    retained_campaign: RuntimeLoadCampaignEvidence
    inventory: RuntimeTargetHostInventory
    budget_decision: RuntimeBudgetDecision

    @property
    def measurement_bytes(self) -> bytes:
        return self.measurement.canonical_bytes()

    @property
    def retained_campaign_bytes(self) -> bytes:
        return ParsedRuntimeTargetHostCampaign(
            evidence=self.retained_campaign
        ).canonical_bytes

    @property
    def inventory_bytes(self) -> bytes:
        return self.inventory.canonical_bytes()


def run_declared_target_host_campaign(
    *,
    journal: JournalStore,
    spec: RuntimeBudgetSpec,
    declared_plan_id: str,
    release_artifact_id: str,
    release_artifact_sha256: str,
    declared_duration_ms: int,
    operations: dict[str, Callable[[], object]],
    research_operations: tuple[tuple[str, Callable[[], object]], ...],
    resource_probe: Callable[[], Mapping[str, int]] | None = None,
) -> RuntimeTargetHostRunResult:
    """Execute one provider-free, durably predeclared target-host campaign.

    Every financial operation must publish exactly its predeclared durable event;
    latency is issued by ``measure_declared_financial_operation`` and is never
    caller supplied.  For this internal provider-free runner, financial staleness
    is the age of one declared work item from the same monotonic operation start
    until its durable financial event is observed.  It is explicitly not market,
    exchange or provider UTC freshness.
    """

    _require_shared_clock_contract()
    if type(spec) is not RuntimeBudgetSpec:
        raise TypeError("spec must be exact RuntimeBudgetSpec")
    duration_ms = _positive_int(declared_duration_ms, name="declared_duration_ms")
    require_exact_journal_store_authority(
        journal,
        subject="target-host runner JournalStore",
    )
    plan_id = _text(declared_plan_id, name="declared_plan_id")
    declared = load_declared_runtime_event_plan(
        journal,
        plan_id=plan_id,
        spec=spec,
    )
    operation_snapshot = _snapshot_operations(declared, operations)
    research_snapshot = _snapshot_research_operations(
        research_operations,
        minimum=spec.min_research_samples,
    )
    resource_probe = _snapshot_resource_probe(resource_probe)

    inventory = collect_runtime_target_host_inventory(
        expected_host_fingerprint=spec.host_fingerprint,
    )
    aggregate_types = tuple(
        sorted({value.aggregate_type for value in declared.expected_events})
    )
    campaign_plan = RuntimeCampaignPlan.create(
        spec=spec,
        workload_profile_hash=declared.digest,
        declared_duration_ms=duration_ms,
        expected_financial_event_ids=declared.expected_event_ids,
        financial_aggregate_types=aggregate_types,
        release_artifact_sha256=release_artifact_sha256,
    )
    campaign_cut = begin_runtime_campaign(
        journal=journal,
        spec=spec,
        plan=campaign_plan,
    )

    resource_samples: list[ResourceTargetHostSample] = []
    research_samples: list[ResearchInterferenceSample] = []

    def capture_resource(phase: str) -> None:
        resource_samples.append(
            ResourceTargetHostSample(
                sample_id=f"resource-{len(resource_samples) + 1}",
                monotonic_ns=time.monotonic_ns(),
                phase=phase,
                metrics=_capture_resource_metrics(journal, resource_probe),
            )
        )

    capture_resource("campaign-start")
    for phase, research_operation in research_snapshot:
        started = time.monotonic_ns()
        research_operation()
        ended = time.monotonic_ns()
        if type(started) is not int or type(ended) is not int or started < 0 or ended < started:
            raise RuntimeTargetHostRunnerError(
                "system monotonic clock produced an invalid research interval"
            )
        research_samples.append(
            ResearchInterferenceSample(
                sample_id=f"research-{len(research_samples) + 1}",
                phase=phase,
                start_monotonic_ns=started,
                end_monotonic_ns=ended,
            )
        )
        capture_resource(f"after-research:{phase}")

    durable_samples: list[DurableFinancialLatencySample] = []
    for event_id, operation in operation_snapshot:
        _result, durable_sample = measure_declared_financial_operation(
            journal,
            spec,
            plan_id=declared.plan_id,
            event_id=event_id,
            operation=operation,
        )
        durable_samples.append(durable_sample)
        capture_resource(f"after-financial:{event_id}")

    end_sequence = JournalStore.current_journal_sequence(journal)
    financial_samples = tuple(
        FinancialTargetHostSample(
            sample_id=sample.measurement_event_id,
            event_id=sample.event_id,
            journal_sequence=sample.event_journal_sequence,
            latency_start_monotonic_ns=sample.monotonic_start_ns,
            latency_end_monotonic_ns=sample.monotonic_end_ns,
            staleness_source_monotonic_ns=sample.monotonic_start_ns,
            staleness_observed_monotonic_ns=sample.monotonic_end_ns,
        )
        for sample in durable_samples
    )
    measurement = TargetHostMeasurementArtifact(
        source_sha=spec.release_sha,
        release_artifact_id=release_artifact_id,
        release_artifact_sha256=release_artifact_sha256,
        scenario_id=spec.scenario_id,
        spec_digest=spec.digest,
        configuration_hash=spec.configuration_hash,
        host_fingerprint=spec.host_fingerprint,
        workload_profile_hash=declared.digest,
        plan_digest=campaign_plan.digest,
        journal_taxonomy_digest=campaign_plan.journal_taxonomy_digest,
        journal_store_identity_digest=campaign_cut.journal_store_identity_digest,
        start_journal_sequence=campaign_cut.start_journal_sequence,
        end_journal_sequence=end_sequence,
        monotonic_clock_id=MONOTONIC_CLOCK_ID,
        staleness_basis=STALENESS_BASIS,
        research_interference_basis=RESEARCH_INTERFERENCE_BASIS,
        financial_samples=financial_samples,
        research_samples=tuple(research_samples),
        resource_samples=tuple(resource_samples),
    )
    campaign_evidence = collect_runtime_campaign_evidence_from_measurement_artifact(
        journal=journal,
        spec=spec,
        plan=campaign_plan,
        cut=campaign_cut,
        measurement=measurement,
        expected_release_artifact_id=release_artifact_id,
    )
    observation = campaign_evidence.to_observation(spec)
    decision = evaluate_runtime_budget(spec, observation)
    retained_campaign = RuntimeLoadCampaignEvidence(
        observation=observation,
        journal_sequence_before=campaign_evidence.start_journal_sequence,
        journal_sequence_after=campaign_evidence.end_journal_sequence,
        recovered_event_ids=campaign_evidence.recovered_financial_event_ids,
        recovered_journal_sequences=tuple(
            sequence
            for _event_id, _payload_hash, sequence in (
                campaign_evidence.recovered_financial_event_bindings
            )
        ),
        host_identity=dict(inventory.host_identity),
    )
    # Reparse canonical campaign bytes now so the executable runner cannot return
    # a retained document that the terminal WP-65 parser would later reject.
    retained_campaign = ParsedRuntimeTargetHostCampaign.parse(
        ParsedRuntimeTargetHostCampaign(
            evidence=retained_campaign
        ).canonical_bytes
    ).evidence
    return RuntimeTargetHostRunResult(
        declared_plan=declared,
        campaign_plan=campaign_plan,
        campaign_cut=campaign_cut,
        measurement=measurement,
        campaign_evidence=campaign_evidence,
        retained_campaign=retained_campaign,
        inventory=inventory,
        budget_decision=decision,
    )
