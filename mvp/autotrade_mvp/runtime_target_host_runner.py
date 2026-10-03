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
from types import FunctionType, MethodType
from typing import Callable, Mapping

from . import runtime_load_measurement as _runtime_load_measurement
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


def _callable_authority_state(value: object) -> tuple[
    object,
    tuple[tuple[FunctionType, object, object, object, tuple[tuple[object, object], ...] | None], ...],
    tuple[tuple[dict[str, object], str, object], ...],
    tuple[tuple[object, bool, object | None], ...],
]:
    """Capture callback-adjacent callable plus first-party transitive dependencies."""

    target = value.__func__ if type(value) is MethodType else value
    if type(target) is not FunctionType:
        return (target, (), (), ())

    function_states: list[
        tuple[
            FunctionType,
            object,
            object,
            object,
            tuple[tuple[object, object], ...] | None,
        ]
    ] = []
    global_bindings: list[tuple[dict[str, object], str, object]] = []
    closure_bindings: list[tuple[object, bool, object | None]] = []
    seen_functions: set[int] = set()

    def capture(function: FunctionType) -> None:
        identity = id(function)
        if identity in seen_functions:
            return
        seen_functions.add(identity)
        kwdefaults = function.__kwdefaults__
        function_states.append(
            (
                function,
                function.__code__,
                function.__defaults__,
                kwdefaults,
                None if kwdefaults is None else tuple(sorted(kwdefaults.items())),
            )
        )
        namespace = function.__globals__
        for dependency_name in function.__code__.co_names:
            if dependency_name not in namespace:
                continue
            expected_dependency = namespace[dependency_name]
            global_bindings.append(
                (namespace, dependency_name, expected_dependency)
            )
            dependency_target = (
                expected_dependency.__func__
                if type(expected_dependency) is MethodType
                else expected_dependency
            )
            if (
                type(dependency_target) is FunctionType
                and (
                    dependency_target.__module__ == "mvp.autotrade_mvp"
                    or dependency_target.__module__.startswith("mvp.autotrade_mvp.")
                )
            ):
                capture(dependency_target)
        for cell in function.__closure__ or ():
            try:
                expected_value = cell.cell_contents
            except ValueError:
                closure_bindings.append((cell, False, None))
                continue
            closure_bindings.append((cell, True, expected_value))
            dependency_target = (
                expected_value.__func__
                if type(expected_value) is MethodType
                else expected_value
            )
            if (
                type(dependency_target) is FunctionType
                and (
                    dependency_target.__module__ == "mvp.autotrade_mvp"
                    or dependency_target.__module__.startswith("mvp.autotrade_mvp.")
                )
            ):
                capture(dependency_target)

    capture(target)
    return (
        target,
        tuple(function_states),
        tuple(global_bindings),
        tuple(closure_bindings),
    )


def _require_callable_authority(
    value: object,
    state: tuple[
        object,
        tuple[tuple[FunctionType, object, object, object, tuple[tuple[object, object], ...] | None], ...],
        tuple[tuple[dict[str, object], str, object], ...],
        tuple[tuple[object, bool, object | None], ...],
    ],
    *,
    name: str,
) -> None:
    target = value.__func__ if type(value) is MethodType else value
    expected_target, function_states, global_bindings, closure_bindings = state
    if target is not expected_target:
        raise RuntimeTargetHostRunnerError(
            f"{name} callable authority changed during campaign callback"
        )
    for function, code, defaults, kwdefaults, kwdefault_items in function_states:
        if (
            function.__code__ is not code
            or function.__defaults__ is not defaults
            or function.__kwdefaults__ is not kwdefaults
            or (
                kwdefaults is not None
                and tuple(sorted(kwdefaults.items())) != kwdefault_items
            )
        ):
            raise RuntimeTargetHostRunnerError(
                f"{name} executable authority changed during campaign callback"
            )
    missing = object()
    for namespace, dependency_name, expected_dependency in global_bindings:
        if namespace.get(dependency_name, missing) is not expected_dependency:
            raise RuntimeTargetHostRunnerError(
                f"{name} global dependency changed during campaign callback: "
                f"{dependency_name}"
            )
    for cell, had_value, expected_value in closure_bindings:
        try:
            current_value = cell.cell_contents
        except ValueError:
            if had_value:
                raise RuntimeTargetHostRunnerError(
                    f"{name} closure dependency changed during campaign callback"
                )
            continue
        if not had_value or current_value is not expected_value:
            raise RuntimeTargetHostRunnerError(
                f"{name} closure dependency changed during campaign callback"
            )

def _class_member_executables(value: object) -> tuple[FunctionType, ...]:
    if type(value) is FunctionType:
        return (value,)
    if type(value) is staticmethod or type(value) is classmethod:
        function = value.__func__
        return (function,) if type(function) is FunctionType else ()
    if type(value) is property:
        return tuple(
            function
            for function in (value.fget, value.fset, value.fdel)
            if type(function) is FunctionType
        )
    return ()


def _class_authority_state(
    value: type,
) -> tuple[
    tuple[
        str,
        object,
        tuple[
            tuple[FunctionType, tuple],
            ...,
        ],
    ],
    ...,
]:
    if type(value) is not type:
        raise TypeError("class authority must be an exact class")
    return tuple(
        (
            key,
            member,
            tuple(
                (function, _callable_authority_state(function))
                for function in _class_member_executables(member)
            ),
        )
        for key, member in sorted(vars(value).items())
    )


def _class_resolution_authority_state(
    value: type,
    names: tuple[str, ...],
) -> tuple[
    tuple[
        str,
        type,
        object,
        tuple[tuple[FunctionType, tuple], ...],
    ],
    ...,
]:
    """Snapshot raw class-member resolution without invoking descriptors."""

    if type(value) is not type:
        raise TypeError("class-resolution authority must be an exact class")
    if type(names) is not tuple or any(type(name) is not str or not name for name in names):
        raise TypeError("class-resolution authority names must be exact text tuple")
    result = []
    for member_name in names:
        owner = None
        member = None
        for candidate in value.__mro__:
            namespace = vars(candidate)
            if member_name in namespace:
                owner = candidate
                member = namespace[member_name]
                break
        if owner is None:
            raise TypeError(f"class-resolution member is unavailable: {member_name}")
        result.append(
            (
                member_name,
                owner,
                member,
                tuple(
                    (function, _callable_authority_state(function))
                    for function in _class_member_executables(member)
                ),
            )
        )
    return tuple(result)


def _require_class_resolution_authority(
    value: type,
    state: tuple,
    *,
    name: str,
) -> None:
    """Require the same MRO owner, raw descriptor and executable state."""

    if type(value) is not type:
        raise RuntimeTargetHostRunnerError(
            f"{name} class-resolution authority changed during campaign callback"
        )
    missing = object()
    for member_name, expected_owner, expected_member, executables in state:
        current_owner = None
        for candidate in value.__mro__:
            if member_name in vars(candidate):
                current_owner = candidate
                break
        if current_owner is not expected_owner:
            raise RuntimeTargetHostRunnerError(
                f"{name}.{member_name} resolution authority changed during campaign callback"
            )
        namespace = vars(expected_owner)
        if namespace.get(member_name, missing) is not expected_member:
            raise RuntimeTargetHostRunnerError(
                f"{name}.{member_name} class member changed during campaign callback"
            )
        for function, function_state in executables:
            _require_callable_authority(
                function,
                function_state,
                name=f"{name}.{member_name}",
            )


def _require_class_authority(
    value: type,
    state: tuple,
    *,
    name: str,
) -> None:
    if type(value) is not type:
        raise RuntimeTargetHostRunnerError(
            f"{name} class authority changed during campaign callback"
        )
    namespace = vars(value)
    expected_keys = tuple(key for key, _member, _executables in state)
    if tuple(sorted(namespace)) != expected_keys:
        raise RuntimeTargetHostRunnerError(
            f"{name} class namespace changed during campaign callback"
        )
    for key, member, executables in state:
        if namespace[key] is not member:
            raise RuntimeTargetHostRunnerError(
                f"{name}.{key} class member changed during campaign callback"
            )
        for function, function_state in executables:
            _require_callable_authority(
                function,
                function_state,
                name=f"{name}.{key}",
            )


def _capture_resource_metrics(
    store: JournalStore,
    resource_probe: Callable[[], Mapping[str, int]] | None,
    *,
    current_journal_sequence: Callable[[JournalStore], int] | None = None,
    pending_outbox_count: Callable[[JournalStore], int] | None = None,
    active_count: Callable[[], int] | None = None,
    process_time_ns: Callable[[], int] | None = None,
    authority_check: Callable[[], None] | None = None,
) -> dict[str, int]:
    """Capture one cut-consistent portable/optional resource observation.

    All JournalStore-derived metrics are bracketed by one start/end sequence
    equality check. A probe is observation-only; a mutation by the probe or an
    unrelated concurrent journal writer anywhere while the sample is being read
    invalidates the sample instead of producing mixed-cut resource evidence.
    Returned optional values must be exact non-negative integers and cannot
    replace runner-owned metric keys.
    """

    if current_journal_sequence is None:
        current_journal_sequence = JournalStore.current_journal_sequence
    if pending_outbox_count is None:
        pending_outbox_count = JournalStore.pending_outbox_count
    if active_count is None:
        active_count = threading.active_count
    if process_time_ns is None:
        process_time_ns = time.process_time_ns

    if authority_check is not None:
        authority_check()
    sequence_before = current_journal_sequence(store)
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
        if authority_check is not None:
            authority_check()

    # Keep every observation inside the same journal-sequence bracket. The final
    # sequence read is intentionally last: a write that races pending-outbox or
    # the optional probe invalidates the complete resource sample.
    pending_outbox = pending_outbox_count(store)
    active_threads = active_count()
    process_cpu_time_ns = process_time_ns()
    sequence_after = current_journal_sequence(store)
    if authority_check is not None:
        authority_check()
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

    # Detach the authority-bearing budget from the caller-owned object before
    # any caller callback runs. Frozen dataclasses can still be mutated through
    # object.__setattr__, so evaluating against the original instance would let
    # a callback widen its own qualification thresholds after the campaign cut.
    input_spec = spec
    budget_spec_fields = (
        "scenario_id",
        "release_sha",
        "configuration_hash",
        "host_fingerprint",
        "strategy_horizon_us",
        "max_p95_financial_latency_us",
        "max_financial_staleness_us",
        "max_research_interference_us",
        "min_financial_samples",
        "min_research_samples",
    )
    input_spec_dict = input_spec.__dict__
    if type(input_spec_dict) is not dict:
        raise RuntimeTargetHostRunnerError(
            "input RuntimeBudgetSpec instance state is non-canonical"
        )
    input_spec_keys = tuple(input_spec_dict)
    if (
        any(type(key) is not str for key in input_spec_keys)
        or len(input_spec_keys) != len(budget_spec_fields)
        or set(input_spec_keys) != set(budget_spec_fields)
    ):
        raise RuntimeTargetHostRunnerError(
            "input RuntimeBudgetSpec instance state is non-canonical"
        )
    input_spec_state = tuple(
        (name, input_spec_dict[name]) for name in budget_spec_fields
    )
    spec = RuntimeBudgetSpec(
        **{name: value for name, value in input_spec_state}
    )
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

    # Freeze every authority callable used after a caller-supplied callback. A
    # callback may exercise product/research code, but it cannot replace clocks,
    # durable-store readers, evidence collection, evaluation, or terminal parse.
    monotonic_ns = time.monotonic_ns
    process_time_ns = time.process_time_ns
    active_count = threading.active_count
    financial_clock = _runtime_load_measurement.perf_counter_ns
    journal_store_type = JournalStore
    journal_store_mro = JournalStore.__mro__
    journal_store_resolution_state = _class_resolution_authority_state(
        JournalStore,
        (
            "SCHEMA_VERSION",
            "_connect",
            "_connect_windows",
            "_require_text",
            "_now",
            "_aggregate_version_value",
            "_journal_sequence_value",
            "_decode_event_row",
            "get_event",
            "append_event",
            "current_journal_sequence",
            "pending_outbox_count",
            "load_events_after_journal_sequence",
        ),
    )
    current_journal_sequence = JournalStore.current_journal_sequence
    pending_outbox_count = JournalStore.pending_outbox_count
    measure_financial = measure_declared_financial_operation
    capture_resource_metrics = _capture_resource_metrics
    collect_campaign_evidence = (
        collect_runtime_campaign_evidence_from_measurement_artifact
    )
    evaluate_budget = evaluate_runtime_budget
    parsed_campaign_type = ParsedRuntimeTargetHostCampaign
    parse_campaign = ParsedRuntimeTargetHostCampaign.parse
    measurement_type = TargetHostMeasurementArtifact
    financial_sample_type = FinancialTargetHostSample
    research_sample_type = ResearchInterferenceSample
    resource_sample_type = ResourceTargetHostSample
    retained_campaign_type = RuntimeLoadCampaignEvidence
    run_result_type = RuntimeTargetHostRunResult
    budget_spec_type = RuntimeBudgetSpec
    budget_decision_type = RuntimeBudgetDecision
    declared_plan_type = DeclaredRuntimeEventPlan
    campaign_plan_type = RuntimeCampaignPlan
    campaign_cut_type = RuntimeCampaignCut
    campaign_evidence_type = RuntimeCampaignEvidence
    durable_sample_type = DurableFinancialLatencySample
    inventory_type = RuntimeTargetHostInventory
    error_type = RuntimeTargetHostRunnerError
    monotonic_clock_id = MONOTONIC_CLOCK_ID
    staleness_basis = STALENESS_BASIS
    research_interference_basis = RESEARCH_INTERFERENCE_BASIS

    require_callable_authority_helper = _require_callable_authority
    require_class_authority_helper = _require_class_authority
    require_class_resolution_authority_helper = _require_class_resolution_authority
    require_callable_kwdefaults = require_callable_authority_helper.__kwdefaults__
    require_class_kwdefaults = require_class_authority_helper.__kwdefaults__
    require_class_resolution_kwdefaults = (
        require_class_resolution_authority_helper.__kwdefaults__
    )
    require_callable_helper_state = (
        require_callable_authority_helper.__code__,
        require_callable_authority_helper.__defaults__,
        require_callable_kwdefaults,
        None
        if require_callable_kwdefaults is None
        else tuple(sorted(require_callable_kwdefaults.items())),
    )
    require_class_helper_state = (
        require_class_authority_helper.__code__,
        require_class_authority_helper.__defaults__,
        require_class_kwdefaults,
        None
        if require_class_kwdefaults is None
        else tuple(sorted(require_class_kwdefaults.items())),
    )
    require_class_resolution_helper_state = (
        require_class_resolution_authority_helper.__code__,
        require_class_resolution_authority_helper.__defaults__,
        require_class_resolution_kwdefaults,
        None
        if require_class_resolution_kwdefaults is None
        else tuple(sorted(require_class_resolution_kwdefaults.items())),
    )

    class_states = tuple(
        (name, value, _class_authority_state(value))
        for name, value in (
            ("RuntimeBudgetSpec", budget_spec_type),
            ("RuntimeBudgetDecision", budget_decision_type),
            ("DeclaredRuntimeEventPlan", declared_plan_type),
            ("RuntimeCampaignPlan", campaign_plan_type),
            ("RuntimeCampaignCut", campaign_cut_type),
            ("RuntimeCampaignEvidence", campaign_evidence_type),
            ("DurableFinancialLatencySample", durable_sample_type),
            ("RuntimeTargetHostInventory", inventory_type),
            ("ParsedRuntimeTargetHostCampaign", parsed_campaign_type),
            ("TargetHostMeasurementArtifact", measurement_type),
            ("FinancialTargetHostSample", financial_sample_type),
            ("ResearchInterferenceSample", research_sample_type),
            ("ResourceTargetHostSample", resource_sample_type),
            ("RuntimeLoadCampaignEvidence", retained_campaign_type),
            ("RuntimeTargetHostRunResult", run_result_type),
        )
    )

    callable_states = (
        ("runner monotonic clock", lambda: time.monotonic_ns, _callable_authority_state(monotonic_ns)),
        ("runner process CPU clock", lambda: time.process_time_ns, _callable_authority_state(process_time_ns)),
        ("runner thread counter", lambda: threading.active_count, _callable_authority_state(active_count)),
        ("financial latency clock", lambda: _runtime_load_measurement.perf_counter_ns, _callable_authority_state(financial_clock)),
        ("JournalStore current-sequence reader", lambda: JournalStore.current_journal_sequence, _callable_authority_state(current_journal_sequence)),
        ("JournalStore pending-outbox reader", lambda: JournalStore.pending_outbox_count, _callable_authority_state(pending_outbox_count)),
        ("financial measurement authority", lambda: measure_declared_financial_operation, _callable_authority_state(measure_financial)),
        ("resource capture authority", lambda: _capture_resource_metrics, _callable_authority_state(capture_resource_metrics)),
        ("campaign evidence collector", lambda: collect_runtime_campaign_evidence_from_measurement_artifact, _callable_authority_state(collect_campaign_evidence)),
        ("budget evaluator", lambda: evaluate_runtime_budget, _callable_authority_state(evaluate_budget)),
        ("terminal campaign parser", lambda: ParsedRuntimeTargetHostCampaign.parse, _callable_authority_state(parse_campaign)),
    )

    def require_guard_helper_authority() -> None:
        if RuntimeTargetHostRunnerError is not error_type:
            raise error_type(
                "runner callback guard helper authority changed: "
                "RuntimeTargetHostRunnerError"
            )
        helper_checks = (
            (
                "_require_callable_authority",
                _require_callable_authority,
                require_callable_authority_helper,
                require_callable_helper_state,
            ),
            (
                "_require_class_authority",
                _require_class_authority,
                require_class_authority_helper,
                require_class_helper_state,
            ),
            (
                "_require_class_resolution_authority",
                _require_class_resolution_authority,
                require_class_resolution_authority_helper,
                require_class_resolution_helper_state,
            ),
        )
        for name, current, expected, state in helper_checks:
            if current is not expected:
                raise error_type(
                    f"runner callback guard helper authority changed: {name}"
                )
            code, defaults, kwdefaults, kwdefault_items = state
            if (
                expected.__code__ is not code
                or expected.__defaults__ is not defaults
                or expected.__kwdefaults__ is not kwdefaults
                or (
                    kwdefaults is not None
                    and tuple(sorted(kwdefaults.items())) != kwdefault_items
                )
            ):
                raise error_type(
                    f"runner callback guard helper executable authority changed: {name}"
                )

    def require_input_spec_authority() -> None:
        if type(input_spec) is not budget_spec_type:
            raise error_type(
                "input RuntimeBudgetSpec authority changed during campaign callback"
            )
        current_state = input_spec.__dict__
        if type(current_state) is not dict:
            raise error_type(
                "input RuntimeBudgetSpec state changed during campaign callback"
            )
        current_keys = tuple(current_state)
        if (
            any(type(key) is not str for key in current_keys)
            or len(current_keys) != len(budget_spec_fields)
            or set(current_keys) != set(budget_spec_fields)
        ):
            raise error_type(
                "input RuntimeBudgetSpec state changed during campaign callback"
            )
        missing = object()
        for name, expected in input_spec_state:
            current = current_state.get(name, missing)
            if type(current) is not type(expected) or current != expected:
                raise error_type(
                    "input RuntimeBudgetSpec state changed during campaign callback: "
                    + name
                )

    def require_callback_authority() -> None:
        require_guard_helper_authority()
        require_input_spec_authority()
        if JournalStore is not journal_store_type:
            raise error_type(
                "JournalStore authority changed during campaign callback"
            )
        if JournalStore.__mro__ != journal_store_mro:
            raise error_type(
                "JournalStore MRO authority changed during campaign callback"
            )
        require_class_resolution_authority_helper(
            JournalStore,
            journal_store_resolution_state,
            name="JournalStore",
        )
        if MONOTONIC_CLOCK_ID is not monotonic_clock_id:
            raise error_type(
                "monotonic clock identity changed during campaign callback"
            )
        if STALENESS_BASIS is not staleness_basis:
            raise error_type(
                "staleness basis changed during campaign callback"
            )
        if RESEARCH_INTERFERENCE_BASIS is not research_interference_basis:
            raise error_type(
                "research interference basis changed during campaign callback"
            )
        class_bindings = (
            ("budget spec type", RuntimeBudgetSpec, budget_spec_type),
            ("budget decision type", RuntimeBudgetDecision, budget_decision_type),
            ("declared plan type", DeclaredRuntimeEventPlan, declared_plan_type),
            ("campaign plan type", RuntimeCampaignPlan, campaign_plan_type),
            ("campaign cut type", RuntimeCampaignCut, campaign_cut_type),
            ("campaign evidence type", RuntimeCampaignEvidence, campaign_evidence_type),
            ("durable sample type", DurableFinancialLatencySample, durable_sample_type),
            ("inventory type", RuntimeTargetHostInventory, inventory_type),
            ("terminal campaign type", ParsedRuntimeTargetHostCampaign, parsed_campaign_type),
            ("measurement type", TargetHostMeasurementArtifact, measurement_type),
            ("financial sample type", FinancialTargetHostSample, financial_sample_type),
            ("research sample type", ResearchInterferenceSample, research_sample_type),
            ("resource sample type", ResourceTargetHostSample, resource_sample_type),
            ("retained campaign type", RuntimeLoadCampaignEvidence, retained_campaign_type),
            ("run result type", RuntimeTargetHostRunResult, run_result_type),
        )
        for name, current, expected in class_bindings:
            if current is not expected:
                raise RuntimeTargetHostRunnerError(
                    f"{name} authority changed during campaign callback"
                )
        for name, resolve, state in callable_states:
            require_callable_authority_helper(resolve(), state, name=name)
        for name, value, state in class_states:
            require_class_authority_helper(value, state, name=name)

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
        require_callback_authority()
        sample_sequence_before = current_journal_sequence(journal)
        sample_monotonic_ns = monotonic_ns()
        metrics = capture_resource_metrics(
            journal,
            resource_probe,
            current_journal_sequence=current_journal_sequence,
            pending_outbox_count=pending_outbox_count,
            active_count=active_count,
            process_time_ns=process_time_ns,
            authority_check=require_callback_authority,
        )
        require_callback_authority()
        sample_sequence_after = current_journal_sequence(journal)
        if (
            sample_sequence_after != sample_sequence_before
            or metrics.get("journal_sequence") != sample_sequence_before
        ):
            raise error_type(
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
        require_callback_authority()

    capture_resource("campaign-start")
    for phase, research_operation in research_snapshot:
        require_callback_authority()
        started = monotonic_ns()
        research_operation()
        require_callback_authority()
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
        require_callback_authority()
        _result, durable_sample = measure_financial(
            journal,
            spec,
            plan_id=declared.plan_id,
            event_id=event_id,
            operation=operation,
        )
        require_callback_authority()
        durable_samples.append(durable_sample)
        capture_resource(f"after-financial:{event_id}")

    require_callback_authority()
    end_sequence = current_journal_sequence(journal)
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
    require_callback_authority()
    campaign_evidence = collect_campaign_evidence(
        journal=journal,
        spec=spec,
        plan=campaign_plan,
        cut=campaign_cut,
        measurement=measurement,
        expected_release_artifact_id=release_artifact_id,
    )
    require_callback_authority()
    observation = campaign_evidence.to_observation(spec)
    require_callback_authority()
    decision = evaluate_budget(spec, observation)
    require_callback_authority()
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
    require_callback_authority()
    retained_campaign = parse_campaign(
        parsed_campaign_type(
            evidence=retained_campaign
        ).canonical_bytes
    ).evidence
    require_callback_authority()
    return run_result_type(
        declared_plan=declared,
        campaign_plan=campaign_plan,
        campaign_cut=campaign_cut,
        measurement=measurement,
        campaign_evidence=campaign_evidence,
        retained_campaign=retained_campaign,
        inventory=inventory,
        budget_decision=decision,
    )
