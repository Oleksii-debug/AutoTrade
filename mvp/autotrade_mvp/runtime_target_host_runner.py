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
    provided_ids = tuple(operations)
    if any(type(event_id) is not str for event_id in provided_ids):
        raise RuntimeTargetHostRunnerError(
            "operation keys must be exact built-in str event ids"
        )
    expected_ids = plan.expected_event_ids
    if set(provided_ids) != set(expected_ids):
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


def _build_callback_authority_guard() -> Callable[[], None]:
    """Freeze executable measurement authority across caller callbacks.

    Campaign callbacks are workload, not measurement authority. Capture exact
    clock/resource/JournalStore bindings plus the reachable Python-function graph
    used by ``measure_declared_financial_operation`` before the first callback,
    then recheck it immediately after every caller-controlled operation. This
    prevents both rebinding and same-object ``__code__``/defaults/closure mutation
    from becoming post-callback qualification evidence.
    """

    runner_namespace = globals()
    time_module = time
    threading_module = threading
    journal_type = JournalStore
    measurement_function = measure_declared_financial_operation
    error_type = RuntimeTargetHostRunnerError
    missing = object()

    def capture_function_state(function: object, label: str):
        if type(function) is not FunctionType:
            return None
        return (
            label,
            function,
            function.__code__,
            function.__defaults__,
            None if function.__kwdefaults__ is None else dict(function.__kwdefaults__),
            tuple(
                (cell, cell.cell_contents)
                for cell in (function.__closure__ or ())
            ),
        )

    expected_runner_bindings = (
        ("time", time_module, "runner time module"),
        ("threading", threading_module, "runner threading module"),
        ("JournalStore", journal_type, "JournalStore type"),
        (
            "measure_declared_financial_operation",
            measurement_function,
            "durable financial measurement function",
        ),
        (
            "_capture_resource_metrics",
            _capture_resource_metrics,
            "runner resource collector",
        ),
        (
            "collect_runtime_campaign_evidence_from_measurement_artifact",
            collect_runtime_campaign_evidence_from_measurement_artifact,
            "runtime campaign evidence collector",
        ),
        (
            "evaluate_runtime_budget",
            evaluate_runtime_budget,
            "runtime budget evaluator",
        ),
    )
    expected_time_bindings = (
        ("monotonic_ns", time_module.__dict__.get("monotonic_ns")),
        ("process_time_ns", time_module.__dict__.get("process_time_ns")),
    )
    expected_threading_binding = threading_module.__dict__.get("active_count")
    expected_journal_bindings = tuple(
        (name, getattr(journal_type, name, None))
        for name in (
            "append_event",
            "current_journal_sequence",
            "get_event",
            "pending_outbox_count",
        )
    )
    expected_class_bindings = (
        (
            "FinancialTargetHostSample.__init__",
            FinancialTargetHostSample,
            "__init__",
            FinancialTargetHostSample.__dict__.get("__init__"),
        ),
        (
            "ResearchInterferenceSample.__init__",
            ResearchInterferenceSample,
            "__init__",
            ResearchInterferenceSample.__dict__.get("__init__"),
        ),
        (
            "ResourceTargetHostSample.__init__",
            ResourceTargetHostSample,
            "__init__",
            ResourceTargetHostSample.__dict__.get("__init__"),
        ),
        (
            "TargetHostMeasurementArtifact.__init__",
            TargetHostMeasurementArtifact,
            "__init__",
            TargetHostMeasurementArtifact.__dict__.get("__init__"),
        ),
        (
            "RuntimeLoadCampaignEvidence.__init__",
            RuntimeLoadCampaignEvidence,
            "__init__",
            RuntimeLoadCampaignEvidence.__dict__.get("__init__"),
        ),
        (
            "RuntimeCampaignEvidence.to_observation",
            RuntimeCampaignEvidence,
            "to_observation",
            RuntimeCampaignEvidence.__dict__.get("to_observation"),
        ),
        (
            "ParsedRuntimeTargetHostCampaign.__init__",
            ParsedRuntimeTargetHostCampaign,
            "__init__",
            ParsedRuntimeTargetHostCampaign.__dict__.get("__init__"),
        ),
        (
            "ParsedRuntimeTargetHostCampaign.parse",
            ParsedRuntimeTargetHostCampaign,
            "parse",
            ParsedRuntimeTargetHostCampaign.__dict__.get("parse"),
        ),
    )

    measurement_namespace = measurement_function.__globals__
    measurement_module = measurement_function.__module__
    measurement_bindings: list[tuple[dict[str, object], str, object, str]] = []
    function_states: list[tuple[object, ...]] = []
    seen_functions: set[int] = set()
    seen_bindings: set[tuple[int, str]] = set()

    def capture_function_graph(function: object, label: str, *, recurse: bool) -> None:
        state = capture_function_state(function, label)
        if state is None:
            return
        identity = id(function)
        if identity in seen_functions:
            return
        seen_functions.add(identity)
        function_states.append(state)
        if not recurse:
            return
        namespace = function.__globals__
        for name in function.__code__.co_names:
            if name not in namespace:
                continue
            expected = namespace[name]
            binding_key = (id(namespace), name)
            if binding_key not in seen_bindings:
                seen_bindings.add(binding_key)
                measurement_bindings.append(
                    (namespace, name, expected, function.__module__ + "." + name)
                )
            if type(expected) is FunctionType:
                capture_function_graph(
                    expected,
                    function.__module__ + "." + name,
                    recurse=expected.__module__ == measurement_module,
                )

    capture_function_graph(
        measurement_function,
        "runtime_load_measurement.measure_declared_financial_operation",
        recurse=True,
    )
    for name, expected, label in expected_runner_bindings:
        capture_function_graph(expected, label, recurse=False)
    for name, expected in expected_time_bindings:
        capture_function_graph(expected, "time." + name, recurse=False)
    capture_function_graph(
        expected_threading_binding,
        "threading.active_count",
        recurse=False,
    )
    for name, expected in expected_journal_bindings:
        capture_function_graph(
            expected,
            "JournalStore." + name,
            recurse=False,
        )
    for label, _owner, _name, expected in expected_class_bindings:
        executable = expected
        if isinstance(expected, (classmethod, staticmethod)):
            executable = expected.__func__
        capture_function_graph(executable, label, recurse=False)

    if any(value is None for _name, value in expected_time_bindings):
        raise error_type("target-host clock authority is unavailable")
    if expected_threading_binding is None:
        raise error_type("target-host measurement authority is unavailable")
    if any(value is None for _name, value in expected_journal_bindings):
        raise error_type(
            "target-host JournalStore measurement authority is unavailable"
        )
    if any(expected is None for _label, _owner, _name, expected in expected_class_bindings):
        raise error_type("target-host evidence authority is unavailable")

    frozen_measurement_bindings = tuple(measurement_bindings)
    frozen_function_states = tuple(function_states)

    def require_callback_authority() -> None:
        for name, expected, label in expected_runner_bindings:
            if runner_namespace.get(name) is not expected:
                raise error_type(
                    "callback changed target-host measurement authority: " + label
                )
        for name, expected in expected_time_bindings:
            if time_module.__dict__.get(name) is not expected:
                raise error_type(
                    "callback changed target-host measurement authority: time." + name
                )
        if threading_module.__dict__.get("active_count") is not expected_threading_binding:
            raise error_type(
                "callback changed target-host measurement authority: "
                "threading.active_count"
            )
        for name, expected in expected_journal_bindings:
            current = getattr(journal_type, name, None)
            if current is not expected:
                raise error_type(
                    "callback changed target-host measurement authority: "
                    + "JournalStore."
                    + name
                )
        for label, owner, name, expected in expected_class_bindings:
            if owner.__dict__.get(name) is not expected:
                raise error_type(
                    "callback changed target-host measurement authority: " + label
                )
        for namespace, name, expected, label in frozen_measurement_bindings:
            if namespace.get(name, missing) is not expected:
                raise error_type(
                    "callback changed target-host measurement authority: " + label
                )
        for (
            label,
            function,
            expected_code,
            expected_defaults,
            expected_kwdefaults,
            expected_closure,
        ) in frozen_function_states:
            if function.__code__ is not expected_code:
                raise error_type(
                    "callback changed target-host executable authority: "
                    + str(label)
                )
            if function.__defaults__ is not expected_defaults:
                raise error_type(
                    "callback changed target-host executable defaults: "
                    + str(label)
                )
            current_kwdefaults = function.__kwdefaults__
            if expected_kwdefaults is None:
                if current_kwdefaults is not None:
                    raise error_type(
                        "callback changed target-host executable defaults: "
                        + str(label)
                    )
            elif (
                type(current_kwdefaults) is not dict
                or current_kwdefaults != expected_kwdefaults
            ):
                raise error_type(
                    "callback changed target-host executable defaults: "
                    + str(label)
                )
            current_closure = function.__closure__ or ()
            if len(current_closure) != len(expected_closure):
                raise error_type(
                    "callback changed target-host executable closure: "
                    + str(label)
                )
            for current_cell, (expected_cell, expected_value) in zip(
                current_closure,
                expected_closure,
            ):
                if (
                    current_cell is not expected_cell
                    or current_cell.cell_contents is not expected_value
                ):
                    raise error_type(
                        "callback changed target-host executable closure: "
                        + str(label)
                    )

    return require_callback_authority


def _capture_resource_metrics(
    store: JournalStore,
    resource_probe: Callable[[], Mapping[str, int]] | None,
    *,
    require_callback_authority: Callable[[], None] | None = None,
) -> dict[str, int]:
    """Capture one cut-consistent portable/optional resource observation.

    All JournalStore-derived metrics are bracketed by one start/end sequence
    equality check. A probe is observation-only; a mutation by the probe or an
    unrelated concurrent journal writer anywhere while the sample is being read
    invalidates the sample instead of producing mixed-cut resource evidence.
    Returned optional values must be exact non-negative integers and cannot
    replace runner-owned metric keys.

    When ``require_callback_authority`` is supplied by the executable campaign,
    the optional probe cannot retarget the runner's clock/resource/JournalStore
    authorities before built-in metrics are read.
    """

    sequence_before = JournalStore.current_journal_sequence(store)
    extras: dict[str, int] = {}
    if resource_probe is not None:
        raw = resource_probe()
        if require_callback_authority is not None:
            require_callback_authority()
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
    pending_outbox = JournalStore.pending_outbox_count(store)
    active_threads = threading.active_count()
    process_cpu_time_ns = time.process_time_ns()
    sequence_after = JournalStore.current_journal_sequence(store)
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
    require_callback_authority = _build_callback_authority_guard()

    resource_samples: list[ResourceTargetHostSample] = []
    research_samples: list[ResearchInterferenceSample] = []

    def capture_resource(phase: str) -> None:
        resource_samples.append(
            ResourceTargetHostSample(
                sample_id=f"resource-{len(resource_samples) + 1}",
                monotonic_ns=time.monotonic_ns(),
                phase=phase,
                metrics=_capture_resource_metrics(
                    journal,
                    resource_probe,
                    require_callback_authority=require_callback_authority,
                ),
            )
        )

    capture_resource("campaign-start")
    for phase, research_operation in research_snapshot:
        started = time.monotonic_ns()
        research_operation()
        require_callback_authority()
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
        def guarded_operation(
            operation: Callable[[], object] = operation,
        ) -> object:
            result = operation()
            require_callback_authority()
            return result

        _result, durable_sample = measure_declared_financial_operation(
            journal,
            spec,
            plan_id=declared.plan_id,
            event_id=event_id,
            operation=guarded_operation,
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
