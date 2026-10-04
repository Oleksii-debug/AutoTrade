"""Monotonic durable financial-latency samples for WP-65 campaigns.

This module is deliberately not a load generator or scheduler. A campaign owns
which canonical product operation it invokes. This boundary measures one already
predeclared financial event with the process monotonic clock, proves the exact
durable journal event appeared during that operation, and retains the raw timing
sample in the same canonical JournalStore.

A caller cannot supply a clock or latency integer. Reconnect backlog is derived
from the canonical durable outbox at evaluation time; an optional caller value is
only a consistency assertion. Target-host load generation, staleness provenance,
research contention, restart/reconnect orchestration and resource telemetry
remain separate WP-65 work.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from time import perf_counter_ns
from types import FunctionType, MethodType, ModuleType
from typing import Callable, Sequence, TypeVar

from .performance_qualification import RuntimeBudgetDecision, RuntimeBudgetSpec
from .persistence import (
    JournalStore,
    journal_store_authority_scope,
    payload_digest,
    require_exact_journal_store_authority,
)
from .runtime_load_evidence import ExpectedJournalEvent, JournalConservationEvidence
from .runtime_load_plan import (
    DeclaredRuntimeEventPlan,
    evaluate_declared_runtime_budget,
    load_declared_runtime_event_plan,
)


_MEASUREMENT_EVENT_TYPE = "RuntimeQualificationFinancialLatencyMeasured"
_MEASUREMENT_AGGREGATE_TYPE = "runtime_qualification_latency"
_MEASUREMENT_SCHEMA_VERSION = "1.0.0"
_T = TypeVar("_T")


class RuntimeLoadMeasurementError(ValueError):
    """Raised when a monotonic durable measurement cannot be established."""


def _measurement_event_id(plan_id: str, event_id: str) -> str:
    digest = sha256(f"{plan_id}\0{event_id}".encode("utf-8")).hexdigest()
    return f"runtime-qualification-latency-{digest}"


def _non_negative_int(value: object, *, name: str) -> int:
    if type(value) is not int or value < 0:
        raise RuntimeLoadMeasurementError(f"{name} must be a non-negative integer")
    return value


def _expected_for_id(
    plan: DeclaredRuntimeEventPlan,
    event_id: str,
) -> tuple[int, ExpectedJournalEvent]:
    if type(event_id) is not str or not event_id or event_id != event_id.strip():
        raise RuntimeLoadMeasurementError("event_id must be canonical non-empty text")
    for index, expected in enumerate(plan.expected_events):
        if expected.event_id == event_id:
            return index, expected
    raise RuntimeLoadMeasurementError(
        "financial event is not present in the durable pre-run plan"
    )


def _actual_binding(event: dict[str, object]) -> dict[str, object]:
    return {
        "event_id": event.get("event_id"),
        "event_type": event.get("event_type"),
        "aggregate_type": event.get("aggregate_type"),
        "aggregate_id": event.get("aggregate_id"),
        "aggregate_version": event.get("aggregate_version"),
    }


def _require_expected_event(
    event: dict[str, object] | None,
    expected: ExpectedJournalEvent,
    *,
    after_sequence: int,
) -> dict[str, object]:
    if event is None:
        raise RuntimeLoadMeasurementError(
            "canonical operation returned without the predeclared durable financial event"
        )
    if _actual_binding(event) != expected.payload:
        raise RuntimeLoadMeasurementError(
            "durable financial event does not match its predeclared binding"
        )
    sequence = event.get("journal_sequence")
    if type(sequence) is not int or sequence <= after_sequence:
        raise RuntimeLoadMeasurementError(
            "durable financial event was not committed during the measured operation"
        )
    payload_hash = event.get("payload_hash")
    if type(payload_hash) is not str or not payload_hash.startswith("sha256:"):
        raise RuntimeLoadMeasurementError("durable financial event payload hash is invalid")
    return event


@dataclass(frozen=True)
class DurableFinancialLatencySample:
    plan_id: str
    plan_digest: str
    event_id: str
    event_journal_sequence: int
    measurement_event_id: str
    measurement_journal_sequence: int
    event_payload_hash: str
    monotonic_start_ns: int
    monotonic_end_ns: int
    latency_us: int

    @property
    def digest(self) -> str:
        return payload_digest(
            {
                "schema_version": _MEASUREMENT_SCHEMA_VERSION,
                "plan_id": self.plan_id,
                "plan_digest": self.plan_digest,
                "event_id": self.event_id,
                "event_journal_sequence": self.event_journal_sequence,
                "measurement_event_id": self.measurement_event_id,
                "measurement_journal_sequence": self.measurement_journal_sequence,
                "event_payload_hash": self.event_payload_hash,
                "monotonic_start_ns": self.monotonic_start_ns,
                "monotonic_end_ns": self.monotonic_end_ns,
                "latency_us": self.latency_us,
            }
        )


def _decode_measurement(
    *,
    event: dict[str, object],
    plan: DeclaredRuntimeEventPlan,
    expected_index: int,
    expected: ExpectedJournalEvent,
    financial_event: dict[str, object],
) -> DurableFinancialLatencySample:
    expected_measurement_id = _measurement_event_id(plan.plan_id, expected.event_id)
    if event.get("event_id") != expected_measurement_id:
        raise RuntimeLoadMeasurementError("latency measurement event identity conflicts")
    if event.get("event_type") != _MEASUREMENT_EVENT_TYPE:
        raise RuntimeLoadMeasurementError("latency measurement event type conflicts")
    if event.get("aggregate_type") != _MEASUREMENT_AGGREGATE_TYPE:
        raise RuntimeLoadMeasurementError("latency measurement aggregate type conflicts")
    if event.get("aggregate_id") != plan.plan_id:
        raise RuntimeLoadMeasurementError("latency measurement aggregate identity conflicts")
    if event.get("aggregate_version") != expected_index + 1:
        raise RuntimeLoadMeasurementError("latency measurement aggregate order conflicts")

    payload = event.get("payload")
    if type(payload) is not dict:
        raise RuntimeLoadMeasurementError("latency measurement payload is invalid")
    expected_keys = {
        "schema_version",
        "plan_id",
        "plan_digest",
        "spec_digest",
        "expected_event",
        "event_journal_sequence",
        "event_payload_hash",
        "pre_operation_journal_sequence",
        "monotonic_start_ns",
        "monotonic_end_ns",
        "latency_us",
    }
    if set(payload) != expected_keys:
        raise RuntimeLoadMeasurementError("latency measurement payload shape conflicts")
    if payload.get("schema_version") != _MEASUREMENT_SCHEMA_VERSION:
        raise RuntimeLoadMeasurementError("latency measurement schema is unsupported")
    if payload.get("plan_id") != plan.plan_id or payload.get("plan_digest") != plan.digest:
        raise RuntimeLoadMeasurementError("latency measurement plan binding conflicts")
    if payload.get("spec_digest") != plan.spec_digest:
        raise RuntimeLoadMeasurementError("latency measurement budget binding conflicts")
    if payload.get("expected_event") != expected.payload:
        raise RuntimeLoadMeasurementError("latency measurement event binding conflicts")

    event_sequence = financial_event.get("journal_sequence")
    event_payload_hash = financial_event.get("payload_hash")
    if payload.get("event_journal_sequence") != event_sequence:
        raise RuntimeLoadMeasurementError("latency measurement journal cut conflicts")
    if payload.get("event_payload_hash") != event_payload_hash:
        raise RuntimeLoadMeasurementError("latency measurement payload hash binding conflicts")

    pre_sequence = _non_negative_int(
        payload.get("pre_operation_journal_sequence"),
        name="pre_operation_journal_sequence",
    )
    if pre_sequence < plan.declared_journal_sequence:
        raise RuntimeLoadMeasurementError(
            "latency measurement pre-operation cut predates its durable plan"
        )
    if type(event_sequence) is not int or event_sequence <= pre_sequence:
        raise RuntimeLoadMeasurementError(
            "measured financial event does not follow the pre-operation journal cut"
        )
    start_ns = _non_negative_int(
        payload.get("monotonic_start_ns"),
        name="monotonic_start_ns",
    )
    end_ns = _non_negative_int(
        payload.get("monotonic_end_ns"),
        name="monotonic_end_ns",
    )
    if end_ns < start_ns:
        raise RuntimeLoadMeasurementError("monotonic measurement moved backwards")
    latency_us = _non_negative_int(payload.get("latency_us"), name="latency_us")
    if latency_us != (end_ns - start_ns + 999) // 1_000:
        raise RuntimeLoadMeasurementError("latency measurement duration conflicts")

    measurement_sequence = event.get("journal_sequence")
    if type(measurement_sequence) is not int or measurement_sequence <= event_sequence:
        raise RuntimeLoadMeasurementError(
            "latency measurement must be committed after its financial event"
        )
    return DurableFinancialLatencySample(
        plan_id=plan.plan_id,
        plan_digest=plan.digest,
        event_id=expected.event_id,
        event_journal_sequence=event_sequence,
        measurement_event_id=expected_measurement_id,
        measurement_journal_sequence=measurement_sequence,
        event_payload_hash=event_payload_hash,
        monotonic_start_ns=start_ns,
        monotonic_end_ns=end_ns,
        latency_us=latency_us,
    )


def _capture_operation_dependency_graph(values: tuple[object, ...]) -> tuple:
    """Snapshot post-callback dependency identity and Python executable state."""

    function_states: list[tuple] = []
    global_bindings: list[tuple[dict[str, object], str, object]] = []
    builtin_bindings: list[tuple[dict[str, object], str, object]] = []
    module_members: list[tuple[ModuleType, str, object]] = []
    closure_bindings: list[tuple[object, bool, object | None]] = []
    seen_functions: set[int] = set()
    expanded_functions: set[int] = set()
    seen_globals: set[tuple[int, str]] = set()
    seen_builtins: set[tuple[int, str]] = set()
    seen_members: set[tuple[int, str]] = set()

    def is_first_party(function: FunctionType) -> bool:
        module = function.__module__
        return type(module) is str and (
            module == "mvp.autotrade_mvp"
            or module.startswith("mvp.autotrade_mvp.")
        )

    def snapshot_function(value: object) -> FunctionType | None:
        target = value.__func__ if type(value) is MethodType else value
        if type(target) is not FunctionType:
            return None
        if id(target) not in seen_functions:
            seen_functions.add(id(target))
            kwdefaults = target.__kwdefaults__
            function_states.append(
                (
                    target,
                    target.__code__,
                    target.__defaults__,
                    kwdefaults,
                    None if kwdefaults is None else tuple(sorted(kwdefaults.items())),
                )
            )
        return target

    def capture(value: object, *, external_depth: int) -> None:
        target = snapshot_function(value)
        if target is None or id(target) in expanded_functions:
            return
        expanded_functions.add(id(target))
        namespace = target.__globals__
        builtins_namespace = target.__builtins__
        if type(builtins_namespace) is not dict:
            raise RuntimeLoadMeasurementError(
                "measurement dependency function builtins must be an exact dictionary"
            )
        referenced_names = target.__code__.co_names
        for dependency_name in referenced_names:
            if dependency_name in namespace:
                dependency = namespace[dependency_name]
                binding_key = (id(namespace), dependency_name)
                if binding_key not in seen_globals:
                    seen_globals.add(binding_key)
                    global_bindings.append((namespace, dependency_name, dependency))
                if type(dependency) is ModuleType:
                    for member_name in referenced_names:
                        if not hasattr(dependency, member_name):
                            continue
                        member_key = (id(dependency), member_name)
                        if member_key in seen_members:
                            continue
                        seen_members.add(member_key)
                        member = getattr(dependency, member_name)
                        module_members.append((dependency, member_name, member))
                        member_target = snapshot_function(member)
                        if member_target is not None:
                            if is_first_party(member_target):
                                capture(member_target, external_depth=external_depth)
                            elif external_depth > 0:
                                capture(
                                    member_target,
                                    external_depth=external_depth - 1,
                                )
                dependency_target = snapshot_function(dependency)
                if dependency_target is not None:
                    if is_first_party(dependency_target):
                        capture(dependency_target, external_depth=external_depth)
                    elif external_depth > 0:
                        capture(
                            dependency_target,
                            external_depth=external_depth - 1,
                        )
                continue
            if dependency_name in builtins_namespace:
                binding_key = (id(builtins_namespace), dependency_name)
                if binding_key not in seen_builtins:
                    seen_builtins.add(binding_key)
                    dependency = builtins_namespace[dependency_name]
                    builtin_bindings.append(
                        (
                            builtins_namespace,
                            dependency_name,
                            dependency,
                        )
                    )
                    dependency_target = snapshot_function(dependency)
                    if dependency_target is not None:
                        if is_first_party(dependency_target):
                            capture(dependency_target, external_depth=external_depth)
                        elif external_depth > 0:
                            capture(
                                dependency_target,
                                external_depth=external_depth - 1,
                            )
        for cell in target.__closure__ or ():
            try:
                expected_value = cell.cell_contents
            except ValueError:
                closure_bindings.append((cell, False, None))
                continue
            closure_bindings.append((cell, True, expected_value))
            dependency_target = snapshot_function(expected_value)
            if dependency_target is not None:
                if is_first_party(dependency_target):
                    capture(dependency_target, external_depth=external_depth)
                elif external_depth > 0:
                    capture(
                        dependency_target,
                        external_depth=external_depth - 1,
                    )

    for value in values:
        capture(value, external_depth=1)
    return (
        tuple(function_states),
        tuple(global_bindings),
        tuple(builtin_bindings),
        tuple(module_members),
        tuple(closure_bindings),
    )


def measure_declared_financial_operation(
    store: JournalStore,
    spec: RuntimeBudgetSpec,
    *,
    plan_id: str,
    event_id: str,
    operation: Callable[[], _T],
) -> tuple[_T, DurableFinancialLatencySample]:
    """Measure one canonical operation that must durably publish its planned event.

    The operation callable is execution supplied by the campaign; this function
    does not select or schedule work. Measurement uses ``perf_counter_ns``
    directly and retains the raw monotonic endpoints before returning a sample.
    """

    if not callable(operation):
        raise TypeError("operation must be callable")
    # Freeze every direct authority used after caller-controlled product code.
    # The operation runs inside evidence issuance, so validation must happen here
    # before a forged readback/decoder can commit a durable latency sample.
    clock = perf_counter_ns
    type_for = type
    dict_type = dict
    int_type = int
    tuple_for = tuple
    sorted_for = sorted
    getattr_for = getattr
    module_type = ModuleType
    str_for = str
    object_getattribute = object.__getattribute__
    missing = object()
    authority_error_type = ValueError
    error_type = RuntimeLoadMeasurementError
    journal_store_type = JournalStore
    get_event = JournalStore.get_event
    current_journal_sequence = JournalStore.current_journal_sequence
    append_event = JournalStore.append_event
    require_expected_event = _require_expected_event
    actual_binding = _actual_binding
    decode_measurement = _decode_measurement
    measurement_event_id_for = _measurement_event_id
    non_negative_int = _non_negative_int
    payload_digest_for = payload_digest
    datetime_type = datetime
    timezone_type = timezone
    plan_type = DeclaredRuntimeEventPlan
    expected_type = ExpectedJournalEvent
    sample_type = DurableFinancialLatencySample
    sample_init = DurableFinancialLatencySample.__init__
    expected_payload_getter = ExpectedJournalEvent.payload.fget
    plan_digest_getter = DeclaredRuntimeEventPlan.digest.fget
    measurement_event_type = _MEASUREMENT_EVENT_TYPE
    measurement_aggregate_type = _MEASUREMENT_AGGREGATE_TYPE
    measurement_schema_version = _MEASUREMENT_SCHEMA_VERSION
    measurement_sha256 = sha256

    digest_namespace = payload_digest_for.__globals__
    digest_sha256 = digest_namespace.get("sha256")
    canonical_json_for = digest_namespace.get("canonical_json")
    if not callable(digest_sha256) or not callable(canonical_json_for):
        raise error_type("payload digest authority is not canonical")
    canonical_json_namespace = canonical_json_for.__globals__
    json_module = canonical_json_namespace.get("json")
    if type(json_module) is not ModuleType:
        raise error_type("canonical JSON authority is not a module")
    json_encoder_type = json_module.__dict__.get("JSONEncoder")
    if type(json_encoder_type) is not type:
        raise error_type("canonical JSON encoder authority is not an exact class")
    json_encoder_methods = tuple(
        (name, json_encoder_type.__dict__.get(name))
        for name in ("__init__", "default", "encode", "iterencode")
    )
    if any(type(function) is not FunctionType for _name, function in json_encoder_methods):
        raise error_type("canonical JSON encoder executable authority is unavailable")

    plan_digest_namespace = plan_digest_getter.__globals__
    plan_payload_digest = plan_digest_namespace.get("payload_digest")
    plan_schema_version = plan_digest_namespace.get("_PLAN_SCHEMA_VERSION")
    if plan_payload_digest is not payload_digest_for:
        raise error_type("declared plan digest authority is not canonical")

    module_namespace = globals()
    module_bindings = (
        ("perf_counter_ns", clock),
        ("RuntimeLoadMeasurementError", error_type),
        ("JournalStore", journal_store_type),
        ("_require_expected_event", require_expected_event),
        ("_actual_binding", actual_binding),
        ("_decode_measurement", decode_measurement),
        ("_measurement_event_id", measurement_event_id_for),
        ("_non_negative_int", non_negative_int),
        ("payload_digest", payload_digest_for),
        ("datetime", datetime_type),
        ("timezone", timezone_type),
        ("DeclaredRuntimeEventPlan", plan_type),
        ("ExpectedJournalEvent", expected_type),
        ("DurableFinancialLatencySample", sample_type),
        ("_MEASUREMENT_EVENT_TYPE", measurement_event_type),
        ("_MEASUREMENT_AGGREGATE_TYPE", measurement_aggregate_type),
        ("_MEASUREMENT_SCHEMA_VERSION", measurement_schema_version),
        ("sha256", measurement_sha256),
    )
    transitive_bindings = (
        (digest_namespace, "sha256", digest_sha256, "payload_digest.sha256"),
        (
            digest_namespace,
            "canonical_json",
            canonical_json_for,
            "payload_digest.canonical_json",
        ),
        (
            canonical_json_namespace,
            "json",
            json_module,
            "canonical_json.json",
        ),
        (
            json_module.__dict__,
            "JSONEncoder",
            json_encoder_type,
            "canonical_json.json.JSONEncoder",
        ),
        (
            plan_digest_namespace,
            "payload_digest",
            plan_payload_digest,
            "DeclaredRuntimeEventPlan.digest.payload_digest",
        ),
        (
            plan_digest_namespace,
            "_PLAN_SCHEMA_VERSION",
            plan_schema_version,
            "DeclaredRuntimeEventPlan.digest._PLAN_SCHEMA_VERSION",
        ),
    )
    protected_functions = tuple(
        (
            name,
            function,
            function.__code__,
            function.__defaults__,
            function.__kwdefaults__,
            None
            if function.__kwdefaults__ is None
            else tuple(sorted(function.__kwdefaults__.items())),
        )
        for name, function in (
            ("JournalStore.get_event", get_event),
            ("JournalStore.current_journal_sequence", current_journal_sequence),
            ("JournalStore.append_event", append_event),
            ("_require_expected_event", require_expected_event),
            ("_actual_binding", actual_binding),
            ("_decode_measurement", decode_measurement),
            ("_measurement_event_id", measurement_event_id_for),
            ("_non_negative_int", non_negative_int),
            ("payload_digest", payload_digest_for),
            ("payload_digest.canonical_json", canonical_json_for),
            *(
                (f"json.JSONEncoder.{name}", function)
                for name, function in json_encoder_methods
            ),
            ("DurableFinancialLatencySample.__init__", sample_init),
            ("ExpectedJournalEvent.payload", expected_payload_getter),
            ("DeclaredRuntimeEventPlan.digest", plan_digest_getter),
        )
    )
    journal_dependency_names = (
        "__getattribute__",
        "_connect",
        "_require_text",
        "_decode_event_row",
        "_aggregate_version_value",
        "_journal_sequence_value",
    )
    journal_dependencies = tuple(
        (name, getattr(journal_store_type, name)) for name in journal_dependency_names
    )
    journal_schema_version = journal_store_type.SCHEMA_VERSION
    protected_dependency_values = tuple(
        function for _name, function, *_state in protected_functions
    )
    journal_dependency_graph = _capture_operation_dependency_graph(
        protected_dependency_values
        + tuple(value for _name, value in journal_dependencies)
    )
    protected_domain_classes = tuple(
        (
            label,
            class_type,
            tuple(class_type.__dict__),
            tuple(class_type.__dict__.items()),
            tuple(
                (
                    name,
                    value,
                    value.__code__,
                    value.__defaults__,
                    value.__kwdefaults__,
                    None
                    if value.__kwdefaults__ is None
                    else tuple(sorted(value.__kwdefaults__.items())),
                )
                for name, value in class_type.__dict__.items()
                if type_for(value) is FunctionType
            ),
        )
        for label, class_type in (
            ("RuntimeLoadMeasurementError", error_type),
            ("DeclaredRuntimeEventPlan", plan_type),
            ("ExpectedJournalEvent", expected_type),
            ("DurableFinancialLatencySample", sample_type),
        )
    )

    def require_operation_authority() -> None:
        # Verify exact class surfaces first, using mappingproxy access that cannot
        # dispatch callback-installed instance descriptors.  Only after these
        # checks pass is it safe to use the plan/expected/sample classes below.
        for (
            label,
            class_type,
            member_names,
            members,
            class_executables,
        ) in protected_domain_classes:
            current_class_namespace = class_type.__dict__
            if tuple_for(current_class_namespace) != member_names:
                raise authority_error_type(
                    f"measurement {label} class shape changed during financial operation"
                )
            for name, expected_value in members:
                if current_class_namespace.get(name, missing) is not expected_value:
                    raise authority_error_type(
                        f"measurement {label} class authority changed during financial "
                        f"operation: {name}"
                    )
            for (
                name,
                function,
                code,
                defaults,
                kwdefaults,
                kwdefault_items,
            ) in class_executables:
                if (
                    function.__code__ is not code
                    or function.__defaults__ is not defaults
                    or function.__kwdefaults__ is not kwdefaults
                    or (
                        kwdefaults is not None
                        and tuple_for(sorted_for(kwdefaults.items())) != kwdefault_items
                    )
                ):
                    raise authority_error_type(
                        f"measurement {label} executable authority changed during financial "
                        f"operation: {name}"
                    )
        if type_for(plan) is not plan_type:
            raise error_type(
                "measurement durable plan exact class changed during financial operation"
            )
        current_plan_state = object_getattribute(plan, "__dict__")
        if type_for(current_plan_state) is not dict_type:
            raise error_type(
                "measurement durable plan instance state became non-canonical"
            )
        if tuple_for(current_plan_state) != plan_state_names:
            raise error_type(
                "measurement durable plan instance state shape changed during financial operation"
            )
        for name, expected_value in plan_state_snapshot:
            if current_plan_state.get(name, missing) is not expected_value:
                raise error_type(
                    "measurement durable plan instance state changed during financial "
                    f"operation: {name}"
                )
        for planned_event, state_names, state_snapshot in expected_event_state_snapshots:
            if type_for(planned_event) is not expected_type:
                raise error_type(
                    "measurement durable plan event class changed during financial operation"
                )
            current_event_state = object_getattribute(planned_event, "__dict__")
            if type_for(current_event_state) is not dict_type:
                raise error_type(
                    "measurement durable plan event instance state became non-canonical"
                )
            if tuple_for(current_event_state) != state_names:
                raise error_type(
                    "measurement durable plan event instance state shape changed during financial operation"
                )
            for name, expected_value in state_snapshot:
                if current_event_state.get(name, missing) is not expected_value:
                    raise error_type(
                        "measurement durable plan event instance state changed during financial "
                        f"operation: {name}"
                    )
        if declared_expected_events[expected_index] is not expected:
            raise error_type(
                "measurement selected durable plan event identity changed during financial operation"
            )
        if type_for(store) is not journal_store_type:
            raise error_type(
                "measurement JournalStore exact class changed during financial operation"
            )
        current_store_state = object_getattribute(store, "__dict__")
        if type_for(current_store_state) is not dict_type:
            raise error_type(
                "measurement JournalStore instance state became non-canonical"
            )
        if tuple_for(current_store_state) != store_state_names:
            raise error_type(
                "measurement JournalStore instance state shape changed during financial operation"
            )
        for name, expected_value in store_state_snapshot:
            if current_store_state.get(name, missing) is not expected_value:
                raise error_type(
                    "measurement JournalStore instance state changed during financial "
                    f"operation: {name}"
                )
        for value, snapshot, label in (
            (store_identity, selected_identity_snapshot, "selected JournalStore identity"),
            (stored_identity, stored_identity_snapshot, "stored JournalStore identity"),
        ):
            if type_for(value) is not identity_type:
                raise error_type(f"measurement {label} class changed during financial operation")
            current_identity_state = object_getattribute(value, "__dict__")
            if type_for(current_identity_state) is not dict_type:
                raise error_type(f"measurement {label} state became non-canonical")
            if tuple_for(current_identity_state) != identity_state_names:
                raise error_type(f"measurement {label} state shape changed during financial operation")
            for name, expected_value in snapshot:
                if current_identity_state.get(name, missing) is not expected_value:
                    raise error_type(
                        f"measurement {label} state changed during financial operation: {name}"
                    )
        if tuple_for(identity_type.__dict__) != identity_class_member_names:
            raise error_type(
                "measurement JournalStoreIdentity class shape changed during financial operation"
            )
        for name, expected_value in identity_class_members:
            if identity_type.__dict__.get(name, missing) is not expected_value:
                raise error_type(
                    "measurement JournalStoreIdentity class authority changed during "
                    f"financial operation: {name}"
                )
        for (
            name,
            function,
            code,
            defaults,
            kwdefaults,
            kwdefault_items,
        ) in identity_class_executables:
            if (
                function.__code__ is not code
                or function.__defaults__ is not defaults
                or function.__kwdefaults__ is not kwdefaults
                or (
                    kwdefaults is not None
                    and tuple_for(sorted_for(kwdefaults.items())) != kwdefault_items
                )
            ):
                raise error_type(
                    "measurement JournalStoreIdentity executable authority changed during "
                    f"financial operation: {name}"
                )
        for name, expected_value in module_bindings:
            if module_namespace.get(name, missing) is not expected_value:
                raise error_type(
                    f"measurement authority changed during financial operation: {name}"
                )
        for namespace, dependency_name, expected_value, label in transitive_bindings:
            if namespace.get(dependency_name, missing) is not expected_value:
                raise error_type(
                    "measurement transitive authority changed during financial "
                    f"operation: {label}"
                )
        if (
            JournalStore.get_event is not get_event
            or JournalStore.current_journal_sequence is not current_journal_sequence
            or JournalStore.append_event is not append_event
            or DurableFinancialLatencySample.__init__ is not sample_init
            or ExpectedJournalEvent.payload.fget is not expected_payload_getter
            or DeclaredRuntimeEventPlan.digest.fget is not plan_digest_getter
        ):
            raise error_type(
                "measurement class authority changed during financial operation"
            )
        for name, expected_dependency in journal_dependencies:
            if getattr_for(journal_store_type, name, missing) is not expected_dependency:
                raise error_type(
                    "measurement JournalStore dependency changed during financial "
                    f"operation: {name}"
                )
        if journal_store_type.SCHEMA_VERSION is not journal_schema_version:
            raise error_type(
                "measurement JournalStore schema authority changed during financial operation"
            )
        for (
            name,
            function,
            code,
            defaults,
            kwdefaults,
            kwdefault_items,
        ) in protected_functions:
            if (
                function.__code__ is not code
                or function.__defaults__ is not defaults
                or function.__kwdefaults__ is not kwdefaults
                or (
                    kwdefaults is not None
                    and tuple_for(sorted_for(kwdefaults.items())) != kwdefault_items
                )
            ):
                raise error_type(
                    f"measurement executable authority changed during financial operation: {name}"
                )
        (
            graph_functions,
            graph_globals,
            graph_builtins,
            graph_module_members,
            graph_closures,
        ) = journal_dependency_graph
        for function, code, defaults, kwdefaults, kwdefault_items in graph_functions:
            if (
                function.__code__ is not code
                or function.__defaults__ is not defaults
                or function.__kwdefaults__ is not kwdefaults
                or (
                    kwdefaults is not None
                    and tuple_for(sorted_for(kwdefaults.items())) != kwdefault_items
                )
            ):
                raise error_type(
                    "measurement transitive executable authority changed during "
                    f"financial operation: {function.__module__}.{function.__qualname__}"
                )
        for namespace, dependency_name, expected_dependency in graph_globals:
            if namespace.get(dependency_name, missing) is not expected_dependency:
                raise error_type(
                    "measurement transitive global dependency changed during "
                    f"financial operation: {dependency_name}"
                )
        for namespace, dependency_name, expected_dependency in graph_builtins:
            if namespace.get(dependency_name, missing) is not expected_dependency:
                raise error_type(
                    "measurement transitive builtin dependency changed during "
                    f"financial operation: {dependency_name}"
                )
        for module, member_name, expected_member in graph_module_members:
            # A module object can have __class__ reassigned to a ModuleType
            # subclass with caller-defined __getattribute__.  Never dispatch
            # getattr on a post-callback module until its exact builtin module
            # class is re-established.
            if type_for(module) is not module_type:
                raise error_type(
                    "measurement transitive module dependency class changed during "
                    "financial operation"
                )
            if getattr_for(module, member_name, missing) is not expected_member:
                raise error_type(
                    "measurement transitive module dependency changed during financial "
                    f"operation: {module.__name__}.{member_name}"
                )
        for cell, had_value, expected_value in graph_closures:
            try:
                current_value = cell.cell_contents
            except ValueError:
                if had_value:
                    raise error_type(
                        "measurement transitive closure dependency changed during financial operation"
                    )
                continue
            if not had_value or current_value is not expected_value:
                raise error_type(
                    "measurement transitive closure dependency changed during financial operation"
                )

    store_identity = require_exact_journal_store_authority(
        store,
        subject="runtime qualification JournalStore",
    )
    store_state = object_getattribute(store, "__dict__")
    store_state_snapshot = tuple(store_state.items())
    store_state_names = tuple(store_state)
    stored_identity = store_state.get("_store_identity")
    identity_type = type_for(store_identity)
    if type_for(stored_identity) is not identity_type:
        raise error_type("stored JournalStore identity class is not canonical")
    selected_identity_state = object_getattribute(store_identity, "__dict__")
    stored_identity_state = object_getattribute(stored_identity, "__dict__")
    selected_identity_snapshot = tuple(selected_identity_state.items())
    stored_identity_snapshot = tuple(stored_identity_state.items())
    identity_state_names = tuple(selected_identity_state)
    if tuple(stored_identity_state) != identity_state_names:
        raise error_type("stored JournalStore identity state shape is not canonical")
    identity_class_member_names = tuple(identity_type.__dict__)
    identity_class_members = tuple(identity_type.__dict__.items())
    identity_class_executables = tuple(
        (
            name,
            value,
            value.__code__,
            value.__defaults__,
            value.__kwdefaults__,
            None
            if value.__kwdefaults__ is None
            else tuple(sorted(value.__kwdefaults__.items())),
        )
        for name, value in identity_class_members
        if type_for(value) is FunctionType
    )
    # The operation callback is inside the evidence issuance boundary. Hold the
    # exact physical JournalStore generation across pre-cut, operation, event
    # readback and measurement commit so callback code cannot rebind the same
    # Python object to another valid journal and splice two generations.
    with journal_store_authority_scope(store, store_identity):
        plan = load_declared_runtime_event_plan(store, plan_id=plan_id, spec=spec)
        expected_index, expected = _expected_for_id(plan, event_id)
        if type_for(plan) is not plan_type:
            raise error_type("loaded durable plan must use the exact canonical class")
        plan_state = object_getattribute(plan, "__dict__")
        if type_for(plan_state) is not dict_type:
            raise error_type("loaded durable plan instance state must be canonical")
        plan_state_names = tuple_for(plan_state)
        plan_state_snapshot = tuple_for(plan_state.items())
        declared_expected_events = plan.expected_events
        expected_event_state_entries = []
        for planned_event in declared_expected_events:
            if type_for(planned_event) is not expected_type:
                raise error_type("loaded durable plan event must use the exact canonical class")
            planned_event_state = object_getattribute(planned_event, "__dict__")
            if type_for(planned_event_state) is not dict_type:
                raise error_type("loaded durable plan event instance state must be canonical")
            expected_event_state_entries.append(
                (
                    planned_event,
                    tuple_for(planned_event_state),
                    tuple_for(planned_event_state.items()),
                )
            )
        expected_event_state_snapshots = tuple_for(expected_event_state_entries)
        del expected_event_state_entries, planned_event, planned_event_state
        measurement_id = measurement_event_id_for(plan.plan_id, expected.event_id)
        if get_event(store, measurement_id) is not None:
            raise RuntimeLoadMeasurementError("financial latency was already measured")
        if get_event(store, expected.event_id) is not None:
            raise error_type(
                "predeclared financial event already exists before monotonic measurement"
            )

        pre_sequence = current_journal_sequence(store)
        start_ns = clock()
        if get_event(store, expected.event_id) is not None:
            raise error_type(
                "predeclared financial event appeared before monotonic measurement start"
            )

        verifier = require_operation_authority
        verifier_code = verifier.__code__
        verifier_defaults = verifier.__defaults__
        verifier_kwdefaults = verifier.__kwdefaults__
        verifier_closure_snapshot = tuple_for(
            (cell, cell.cell_contents) for cell in verifier.__closure__ or ()
        )
        callback_continuation = (
            verifier,
            verifier_code,
            verifier_defaults,
            verifier_kwdefaults,
            verifier_closure_snapshot,
            get_event,
            store,
            expected,
            require_expected_event,
            pre_sequence,
            clock,
            start_ns,
            type_for,
            int_type,
            error_type,
            measurement_schema_version,
            plan,
            measurement_event_type,
            measurement_aggregate_type,
            measurement_id,
            str_for,
            expected_index,
            payload_digest_for,
            datetime_type,
            timezone_type,
            append_event,
            decode_measurement,
        )
        callback_continuation, result = (callback_continuation, operation())
        (
            require_operation_authority,
            verifier_code,
            verifier_defaults,
            verifier_kwdefaults,
            verifier_closure_snapshot,
            get_event,
            store,
            expected,
            require_expected_event,
            pre_sequence,
            clock,
            start_ns,
            type_for,
            int_type,
            error_type,
            measurement_schema_version,
            plan,
            measurement_event_type,
            measurement_aggregate_type,
            measurement_id,
            str_for,
            expected_index,
            payload_digest_for,
            datetime_type,
            timezone_type,
            append_event,
            decode_measurement,
        ) = callback_continuation
        for cell, expected_value in verifier_closure_snapshot:
            cell.cell_contents = expected_value
        if (
            require_operation_authority.__code__ is not verifier_code
            or require_operation_authority.__defaults__ is not verifier_defaults
            or require_operation_authority.__kwdefaults__ is not verifier_kwdefaults
        ):
            raise error_type(
                "measurement verifier authority changed during financial operation"
            )
        require_operation_authority()
        # Bind the expected durable event before sampling the terminal clock. If the
        # operation returned without publishing it, an unrelated commit racing with
        # end-clock sampling must not be attributed to the measured operation.
        financial_event = require_expected_event(
            get_event(store, expected.event_id),
            expected,
            after_sequence=pre_sequence,
        )
        end_ns = clock()
        if (
            type_for(start_ns) is not int_type
            or type_for(end_ns) is not int_type
            or start_ns < 0
            or end_ns < start_ns
        ):
            raise error_type(
                "system monotonic clock produced an invalid interval"
            )

        latency_us = (end_ns - start_ns + 999) // 1_000
        require_operation_authority()
        payload = {
            "schema_version": measurement_schema_version,
            "plan_id": plan.plan_id,
            "plan_digest": plan.digest,
            "spec_digest": plan.spec_digest,
            "expected_event": expected.payload,
            "event_journal_sequence": financial_event["journal_sequence"],
            "event_payload_hash": financial_event["payload_hash"],
            "pre_operation_journal_sequence": pre_sequence,
            "monotonic_start_ns": start_ns,
            "monotonic_end_ns": end_ns,
            "latency_us": latency_us,
        }
        require_operation_authority()
        append_event(
            store,
            {
                "event_id": measurement_id,
                "event_type": measurement_event_type,
                "aggregate_type": measurement_aggregate_type,
                "aggregate_id": plan.plan_id,
                "aggregate_version": str_for(expected_index + 1),
                "payload": payload,
                "payload_hash": payload_digest_for(payload),
                "committed_at": datetime_type.now(timezone_type.utc)
                .isoformat()
                .replace("+00:00", "Z"),
            },
        )
        measurement_event = get_event(store, measurement_id)
        if measurement_event is None:
            raise error_type("durable latency measurement disappeared")
        require_operation_authority()
        sample = decode_measurement(
            event=measurement_event,
            plan=plan,
            expected_index=expected_index,
            expected=expected,
            financial_event=financial_event,
        )
        return result, sample


def load_declared_financial_latency_samples(
    store: JournalStore,
    spec: RuntimeBudgetSpec,
    *,
    plan_id: str,
) -> tuple[DurableFinancialLatencySample, ...]:
    """Reload the complete ordered latency sample set for one durable plan."""

    store_identity = require_exact_journal_store_authority(
        store,
        subject="runtime qualification JournalStore",
    )
    with journal_store_authority_scope(store, store_identity):
        plan = load_declared_runtime_event_plan(store, plan_id=plan_id, spec=spec)
        samples: list[DurableFinancialLatencySample] = []
        for index, expected in enumerate(plan.expected_events):
            financial_event = JournalStore.get_event(store, expected.event_id)
            financial_event = _require_expected_event(
                financial_event,
                expected,
                after_sequence=plan.declared_journal_sequence,
            )
            measurement_id = _measurement_event_id(plan.plan_id, expected.event_id)
            measurement = JournalStore.get_event(store, measurement_id)
            if measurement is None:
                raise RuntimeLoadMeasurementError(
                    "declared financial event lacks a durable monotonic latency sample"
                )
            samples.append(
                _decode_measurement(
                    event=measurement,
                    plan=plan,
                    expected_index=index,
                    expected=expected,
                    financial_event=financial_event,
                )
            )
        return tuple(samples)


def evaluate_monotonic_declared_runtime_budget(
    spec: RuntimeBudgetSpec,
    store: JournalStore,
    *,
    plan_id: str,
    financial_staleness_us: Sequence[int],
    research_interference_us: Sequence[int],
    financial_staleness_event_ids: Sequence[str] = (),
    reconnect_backlog_remaining: int | None = None,
    declared_duration_us: int | None = None,
    observed_duration_us: int | None = None,
) -> tuple[
    RuntimeBudgetDecision,
    JournalConservationEvidence,
    DeclaredRuntimeEventPlan,
    tuple[DurableFinancialLatencySample, ...],
]:
    """Evaluate using durable latency and durable-outbox reconnect backlog.

    ``reconnect_backlog_remaining`` is compatibility/assertion metadata only.
    When supplied it must equal the canonical JournalStore pending-outbox count;
    the caller cannot turn a non-zero durable backlog into zero.

    Latency identity is reconstructed from the durable samples themselves.
    Staleness remains explicit and anonymous unless the caller supplies its own
    exact event IDs; interference remains a separate evidence dimension. This
    function therefore does not represent terminal WP-65 qualification.
    """

    store_identity = require_exact_journal_store_authority(
        store,
        subject="runtime qualification JournalStore",
    )
    with journal_store_authority_scope(store, store_identity):
        samples = load_declared_financial_latency_samples(store, spec, plan_id=plan_id)
        durable_backlog = JournalStore.pending_outbox_count(store)
        if type(durable_backlog) is not int or durable_backlog < 0:
            raise RuntimeLoadMeasurementError(
                "canonical JournalStore returned an invalid reconnect backlog"
            )
        if reconnect_backlog_remaining is not None:
            asserted_backlog = _non_negative_int(
                reconnect_backlog_remaining,
                name="reconnect_backlog_remaining",
            )
            if asserted_backlog != durable_backlog:
                raise RuntimeLoadMeasurementError(
                    "caller reconnect backlog assertion conflicts with durable outbox"
                )

        decision, evidence, plan = evaluate_declared_runtime_budget(
            spec,
            store,
            plan_id=plan_id,
            financial_latency_us=tuple(sample.latency_us for sample in samples),
            financial_staleness_us=financial_staleness_us,
            research_interference_us=research_interference_us,
            reconnect_backlog_remaining=durable_backlog,
            financial_latency_event_ids=tuple(sample.event_id for sample in samples),
            financial_staleness_event_ids=financial_staleness_event_ids,
            declared_duration_us=declared_duration_us,
            observed_duration_us=observed_duration_us,
        )
        return decision, evidence, plan, samples
