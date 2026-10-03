"""Executable provider-free target-host campaign runner for WP-65.

This module closes the gap between the already durable pre-run event plan,
monotonic financial-operation measurement, raw target-host measurement artifact,
and canonical runtime-budget evaluator. It deliberately does not invent a
financial workload, provider/exchange input, signer, chronology source, release
or trading authority.

The caller supplies the exact product operations named by a *durably declared*
event plan. The runner owns timing and readback: operations cannot supply
latency/staleness integers, and a planned event must actually appear in the exact
JournalStore during its measured operation. Research-pressure callbacks are
measured as explicit host-monotonic intervals; they may exercise CPU/model/disk
pressure but cannot satisfy or hide financial event conservation.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import sys
import threading
import time
from types import FunctionType
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


def _capture_python_function_state(value: object) -> tuple[object, ...] | None:
    """Snapshot mutation-sensitive state for an exact Python function."""

    if type(value) is not FunctionType:
        return None
    return (
        value.__code__,
        value.__defaults__,
        None if value.__kwdefaults__ is None else dict(value.__kwdefaults__),
    )


def _require_callable_binding(
    *,
    label: str,
    current: object,
    expected: object,
    function_state: tuple[object, ...] | None,
    error_prefix: str,
) -> None:
    if current is not expected:
        raise RuntimeTargetHostRunnerError(error_prefix + label)
    if function_state is None:
        return
    code, defaults, kwdefaults = function_state
    function = expected
    if type(function) is not FunctionType:
        raise RuntimeTargetHostRunnerError(error_prefix + label)
    if function.__code__ is not code or function.__defaults__ is not defaults:
        raise RuntimeTargetHostRunnerError(error_prefix + label)
    current_kwdefaults = function.__kwdefaults__
    if kwdefaults is None:
        if current_kwdefaults is not None:
            raise RuntimeTargetHostRunnerError(error_prefix + label)
    elif type(current_kwdefaults) is not dict or current_kwdefaults != kwdefaults:
        raise RuntimeTargetHostRunnerError(error_prefix + label)


def _build_resource_metric_authority_guard():
    """Freeze builtin resource readers across the observation callback interval."""

    error_type = RuntimeTargetHostRunnerError
    journal_store_type = JournalStore
    threading_module = threading
    time_module = time
    require_binding = _require_callable_binding
    require_binding_state = _capture_python_function_state(require_binding)
    bindings = (
        (
            "JournalStore.current_journal_sequence",
            journal_store_type.current_journal_sequence,
            _capture_python_function_state(journal_store_type.current_journal_sequence),
        ),
        (
            "JournalStore.pending_outbox_count",
            journal_store_type.pending_outbox_count,
            _capture_python_function_state(journal_store_type.pending_outbox_count),
        ),
        (
            "threading.active_count",
            threading_module.active_count,
            _capture_python_function_state(threading_module.active_count),
        ),
        (
            "time.process_time_ns",
            time_module.process_time_ns,
            _capture_python_function_state(time_module.process_time_ns),
        ),
    )
    sequence_reader = bindings[0][1]
    pending_reader = bindings[1][1]
    active_count = bindings[2][1]
    process_time_ns = bindings[3][1]

    def require_guard_helper_authority(*, prefix: str) -> None:
        if _require_callable_binding is not require_binding:
            raise error_type(prefix + "_require_callable_binding")
        if require_binding_state is None:
            return
        code, defaults, kwdefaults = require_binding_state
        if (
            require_binding.__code__ is not code
            or require_binding.__defaults__ is not defaults
        ):
            raise error_type(prefix + "_require_callable_binding")
        current_kwdefaults = require_binding.__kwdefaults__
        if kwdefaults is None:
            if current_kwdefaults is not None:
                raise error_type(prefix + "_require_callable_binding")
        elif type(current_kwdefaults) is not dict or current_kwdefaults != kwdefaults:
            raise error_type(prefix + "_require_callable_binding")

    def require_resource_metric_authority() -> None:
        prefix = "resource metric authority changed: "
        require_guard_helper_authority(prefix=prefix)
        if JournalStore is not journal_store_type:
            raise error_type(prefix + "JournalStore")
        if threading is not threading_module:
            raise error_type(prefix + "threading")
        if time is not time_module:
            raise error_type(prefix + "time")
        current_values = (
            journal_store_type.current_journal_sequence,
            journal_store_type.pending_outbox_count,
            threading_module.active_count,
            time_module.process_time_ns,
        )
        for (label, expected, state), current in zip(bindings, current_values):
            require_binding(
                label=label,
                current=current,
                expected=expected,
                function_state=state,
                error_prefix=prefix,
            )

    return (
        require_resource_metric_authority,
        sequence_reader,
        pending_reader,
        active_count,
        process_time_ns,
    )


def _build_runner_callback_authority_guard(*, monotonic_ns: object):
    """Freeze runner authority that caller callbacks must not retarget."""

    error_type = RuntimeTargetHostRunnerError
    namespace = globals()
    journal_store_type = JournalStore
    threading_module = threading
    time_module = time
    require_binding = _require_callable_binding
    require_binding_state = _capture_python_function_state(require_binding)
    names = (
        "FinancialTargetHostSample",
        "JournalStore",
        "MONOTONIC_CLOCK_ID",
        "ParsedRuntimeTargetHostCampaign",
        "RESEARCH_INTERFERENCE_BASIS",
        "ResearchInterferenceSample",
        "ResourceTargetHostSample",
        "RuntimeLoadCampaignEvidence",
        "RuntimeTargetHostRunResult",
        "STALENESS_BASIS",
        "TargetHostMeasurementArtifact",
        "_build_resource_metric_authority_guard",
        "_capture_resource_metrics",
        "collect_runtime_campaign_evidence_from_measurement_artifact",
        "evaluate_runtime_budget",
        "measure_declared_financial_operation",
        "threading",
        "time",
    )
    captured = tuple(
        (name, namespace[name], _capture_python_function_state(namespace[name]))
        for name in names
    )
    resource_bindings = (
        (
            "JournalStore.current_journal_sequence",
            journal_store_type.current_journal_sequence,
            _capture_python_function_state(journal_store_type.current_journal_sequence),
        ),
        (
            "JournalStore.pending_outbox_count",
            journal_store_type.pending_outbox_count,
            _capture_python_function_state(journal_store_type.pending_outbox_count),
        ),
        (
            "threading.active_count",
            threading_module.active_count,
            _capture_python_function_state(threading_module.active_count),
        ),
        (
            "time.process_time_ns",
            time_module.process_time_ns,
            _capture_python_function_state(time_module.process_time_ns),
        ),
    )
    clock_state = _capture_python_function_state(monotonic_ns)

    def require_guard_helper_authority() -> None:
        prefix = "runner callback authority changed: "
        if _require_callable_binding is not require_binding:
            raise error_type(prefix + "_require_callable_binding")
        if require_binding_state is None:
            return
        code, defaults, kwdefaults = require_binding_state
        if (
            require_binding.__code__ is not code
            or require_binding.__defaults__ is not defaults
        ):
            raise error_type(prefix + "_require_callable_binding")
        current_kwdefaults = require_binding.__kwdefaults__
        if kwdefaults is None:
            if current_kwdefaults is not None:
                raise error_type(prefix + "_require_callable_binding")
        elif type(current_kwdefaults) is not dict or current_kwdefaults != kwdefaults:
            raise error_type(prefix + "_require_callable_binding")

    def require_runner_callback_authority() -> None:
        prefix = "runner callback authority changed: "
        require_guard_helper_authority()
        for name, expected, state in captured:
            require_binding(
                label=name,
                current=namespace.get(name),
                expected=expected,
                function_state=state,
                error_prefix=prefix,
            )
        current_resource_values = (
            journal_store_type.current_journal_sequence,
            journal_store_type.pending_outbox_count,
            threading_module.active_count,
            time_module.process_time_ns,
        )
        for (label, expected, state), current in zip(
            resource_bindings,
            current_resource_values,
        ):
            require_binding(
                label=label,
                current=current,
                expected=expected,
                function_state=state,
                error_prefix=prefix,
            )
        require_binding(
            label="time.monotonic_ns",
            current=time_module.monotonic_ns,
            expected=monotonic_ns,
            function_state=clock_state,
            error_prefix=prefix,
        )

    return require_runner_callback_authority


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
    operation_keys = tuple(operations)
    if any(type(key) is not str for key in operation_keys):
        raise RuntimeTargetHostRunnerError("operation keys must be exact strings")
    expected_ids = plan.expected_event_ids
    if set(operation_keys) != set(expected_ids):
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
    """Capture one cut-consistent portable/optional resource observation.

    All JournalStore-derived metrics are bracketed by one start/end sequence
    equality check. A probe is observation-only; a mutation by the probe or an
    unrelated concurrent journal writer anywhere while the sample is being read
    invalidates the sample instead of producing mixed-cut resource evidence.
    Returned optional values must be exact non-negative integers and cannot
    replace runner-owned metric keys.
    """

    (
        require_resource_metric_authority,
        sequence_reader,
        pending_reader,
        active_count,
        process_time_ns,
    ) = _build_resource_metric_authority_guard()
    require_resource_metric_authority()
    sequence_before = sequence_reader(store)
    extras: dict[str, int] = {}
    if resource_probe is not None:
        raw = resource_probe()
        # A caller-controlled observation callback may not retarget the readers
        # that mint canonical builtin resource evidence.
        require_resource_metric_authority()
        if type(raw) is not dict:
            raise RuntimeTargetHostRunnerError(
                "resource_probe must return an exact dict"
            )
        for key, metric in raw.items():
            name = _text(key, name="resource metric")
            extras[name] = _non_negative_int(metric, name=f"resource metric {name}")

    # Keep every observation inside the same journal-sequence bracket. The final
    # sequence read is intentionally last: a write that races pending-outbox or
    # the optional probe invalidates the complete resource sample.
    pending_outbox = pending_reader(store)
    active_threads = active_count()
    process_cpu_time_ns = process_time_ns()
    sequence_after = sequence_reader(store)
    require_resource_metric_authority()
    if sequence_after != sequence_before:
        raise RuntimeTargetHostRunnerError(
            "resource observation crossed a durable JournalStore cut"
        )

    builtins = {
        "active_threads": active_threads,
        "journal_sequence": sequence_after,
        "pending_outbox": pending_outbox,
        "process_cpu_time_ns": process_cpu_time_ns,
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

    The result is diagnostic only. The runner measures the surrounding active
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
    caller supplied. For this internal provider-free runner, financial staleness
    is the age of one declared work item from the same monotonic operation start
    until its durable financial event is observed. It is explicitly not market,
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

    monotonic_ns = time.monotonic_ns
    require_runner_callback_authority = _build_runner_callback_authority_guard(
        monotonic_ns=monotonic_ns
    )
    capture_resource_metrics = _capture_resource_metrics
    resource_sample_type = ResourceTargetHostSample
    research_sample_type = ResearchInterferenceSample
    measure_financial_operation = measure_declared_financial_operation
    financial_sample_type = FinancialTargetHostSample
    measurement_type = TargetHostMeasurementArtifact
    collect_campaign_evidence = collect_runtime_campaign_evidence_from_measurement_artifact
    evaluate_budget = evaluate_runtime_budget
    retained_campaign_type = RuntimeLoadCampaignEvidence
    parsed_campaign_type = ParsedRuntimeTargetHostCampaign
    result_type = RuntimeTargetHostRunResult
    monotonic_clock_id = MONOTONIC_CLOCK_ID
    staleness_basis = STALENESS_BASIS
    research_interference_basis = RESEARCH_INTERFERENCE_BASIS
    end_sequence_reader = JournalStore.current_journal_sequence
    require_runner_callback_authority()

    resource_samples: list[ResourceTargetHostSample] = []
    research_samples: list[ResearchInterferenceSample] = []

    def capture_resource(phase: str) -> None:
        require_runner_callback_authority()
        sample_sequence_before = end_sequence_reader(journal)
        sample_monotonic_ns = monotonic_ns()
        metrics = capture_resource_metrics(journal, resource_probe)
        # The resource timestamp is authority-bearing evidence too. Bracket it
        # with the same durable JournalStore generation/cut as the metrics so a
        # write between clock sampling and metric sampling cannot create a
        # mixed-time/mixed-journal resource observation.
        require_runner_callback_authority()
        sample_sequence_after = end_sequence_reader(journal)
        if (
            sample_sequence_after != sample_sequence_before
            or metrics.get("journal_sequence") != sample_sequence_before
        ):
            raise RuntimeTargetHostRunnerError(
                "resource sample crossed a durable JournalStore cut"
            )
        resource_samples.append(
            resource_sample_type(
                sample_id=f"resource-{len(resource_samples) + 1}",
                monotonic_ns=sample_monotonic_ns,
                phase=phase,
                metrics=metrics,
            )
        )
        require_runner_callback_authority()

    capture_resource("campaign-start")
    for phase, research_operation in research_snapshot:
        require_runner_callback_authority()
        started = monotonic_ns()
        research_operation()
        require_runner_callback_authority()
        ended = monotonic_ns()
        if type(started) is not int or type(ended) is not int or started < 0 or ended < started:
            raise RuntimeTargetHostRunnerError(
                "system monotonic clock produced an invalid research interval"
            )
        research_samples.append(
            research_sample_type(
                sample_id=f"research-{len(research_samples) + 1}",
                phase=phase,
                start_monotonic_ns=started,
                end_monotonic_ns=ended,
            )
        )
        capture_resource(f"after-research:{phase}")

    durable_samples: list[DurableFinancialLatencySample] = []
    for event_id, operation in operation_snapshot:
        require_runner_callback_authority()
        _result, durable_sample = measure_financial_operation(
            journal,
            spec,
            plan_id=declared.plan_id,
            event_id=event_id,
            operation=operation,
        )
        # Product operations are caller-provided callbacks. They may mutate
        # durable state only through their declared operation; they may not
        # retarget the qualification machinery used after the event is observed.
        require_runner_callback_authority()
        durable_samples.append(durable_sample)
        capture_resource(f"after-financial:{event_id}")

    require_runner_callback_authority()
    end_sequence = end_sequence_reader(journal)
    require_runner_callback_authority()
    financial_samples = tuple(
        financial_sample_type(
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
    measurement = measurement_type(
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
        monotonic_clock_id=monotonic_clock_id,
        staleness_basis=staleness_basis,
        research_interference_basis=research_interference_basis,
        financial_samples=financial_samples,
        research_samples=tuple(research_samples),
        resource_samples=tuple(resource_samples),
    )
    require_runner_callback_authority()
    campaign_evidence = collect_campaign_evidence(
        journal=journal,
        spec=spec,
        plan=campaign_plan,
        cut=campaign_cut,
        measurement=measurement,
        expected_release_artifact_id=release_artifact_id,
    )
    require_runner_callback_authority()
    observation = campaign_evidence.to_observation(spec)
    require_runner_callback_authority()
    decision = evaluate_budget(spec, observation)
    require_runner_callback_authority()
    retained_campaign = retained_campaign_type(
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
    retained_campaign = parsed_campaign_type.parse(
        parsed_campaign_type(
            evidence=retained_campaign
        ).canonical_bytes
    ).evidence
    require_runner_callback_authority()
    return result_type(
        declared_plan=declared,
        campaign_plan=campaign_plan,
        campaign_cut=campaign_cut,
        measurement=measurement,
        campaign_evidence=campaign_evidence,
        retained_campaign=retained_campaign,
        inventory=inventory,
        budget_decision=decision,
    )
