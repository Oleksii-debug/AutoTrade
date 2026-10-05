"""Durable predeclared research-interference measurements for WP-65.

This module removes another caller-authored metric series from the current-main
runtime-load path without claiming terminal target-host qualification. A research
plan is durably declared before outcomes are visible. Each declared pressure
operation is timed with the process monotonic performance clock and its raw
endpoints are retained in the same canonical JournalStore.

Research callbacks are provider-free pressure operations. They may exercise CPU,
model, disk or similar local contention, but they may not mutate the qualification
JournalStore while their interval is sampled. Financial staleness remains a
separate semantic question; the convenience evaluator below uses the explicitly
bounded provider-free basis already established by the historical WP-65 runner:
declared financial work start to durable financial-event observation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from time import perf_counter_ns
from types import FunctionType, ModuleType
from typing import Callable, TypeVar

from .performance_qualification import RuntimeBudgetDecision, RuntimeBudgetSpec
from .persistence import (
    JournalStore,
    journal_store_authority_scope,
    payload_digest,
    require_exact_journal_store_authority,
)
from .runtime_load_evidence import JournalConservationEvidence
from .runtime_load_measurement import (
    DurableFinancialLatencySample,
    _capture_operation_dependency_graph,
    evaluate_monotonic_declared_runtime_budget,
    load_declared_financial_latency_samples,
)
from .runtime_load_plan import (
    DeclaredRuntimeEventPlan,
    load_declared_runtime_event_plan,
)


_RESEARCH_PLAN_EVENT_TYPE = "RuntimeQualificationResearchPlanDeclared"
_RESEARCH_PLAN_AGGREGATE_TYPE = "runtime_qualification_research_plan"
_RESEARCH_PLAN_SCHEMA_VERSION = "1.0.0"
_RESEARCH_SAMPLE_EVENT_TYPE = "RuntimeQualificationResearchInterferenceMeasured"
_RESEARCH_SAMPLE_AGGREGATE_TYPE = "runtime_qualification_research_interference"
_RESEARCH_SAMPLE_SCHEMA_VERSION = "1.0.0"
_T = TypeVar("_T")


class RuntimeLoadResearchMeasurementError(ValueError):
    """Raised when durable research-interference evidence is not canonical."""


def _text(value: object, *, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise RuntimeLoadResearchMeasurementError(
            f"{name} must be canonical non-empty text"
        )
    return value


def _non_negative_int(value: object, *, name: str) -> int:
    if type(value) is not int or value < 0:
        raise RuntimeLoadResearchMeasurementError(
            f"{name} must be a non-negative integer"
        )
    return value


def _research_plan_event_id(plan_id: str) -> str:
    return "runtime-qualification-research-plan-" + sha256(
        plan_id.encode("utf-8")
    ).hexdigest()


def _research_sample_event_id(plan_id: str, sample_id: str) -> str:
    return "runtime-qualification-research-sample-" + sha256(
        f"{plan_id}\0{sample_id}".encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True)
class ExpectedResearchInterferenceSample:
    sample_id: str
    phase: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "sample_id", _text(self.sample_id, name="sample_id"))
        object.__setattr__(self, "phase", _text(self.phase, name="phase"))

    @property
    def payload(self) -> dict[str, str]:
        return {"sample_id": self.sample_id, "phase": self.phase}

    @classmethod
    def from_payload(cls, value: object) -> "ExpectedResearchInterferenceSample":
        if type(value) is not dict or set(value) != {"sample_id", "phase"}:
            raise RuntimeLoadResearchMeasurementError(
                "research sample declaration fields are non-canonical"
            )
        return cls(sample_id=value["sample_id"], phase=value["phase"])


def _snapshot_expected_samples(
    values: object,
) -> tuple[ExpectedResearchInterferenceSample, ...]:
    if type(values) not in {tuple, list}:
        raise RuntimeLoadResearchMeasurementError(
            "expected research samples must be an exact tuple or list"
        )
    result: list[ExpectedResearchInterferenceSample] = []
    seen: set[str] = set()
    for value in values:
        if type(value) is not ExpectedResearchInterferenceSample:
            raise TypeError(
                "expected research samples must contain exact "
                "ExpectedResearchInterferenceSample values"
            )
        sample = ExpectedResearchInterferenceSample.from_payload(value.payload)
        if sample.sample_id in seen:
            raise RuntimeLoadResearchMeasurementError(
                "research sample IDs must be unique"
            )
        seen.add(sample.sample_id)
        result.append(sample)
    if not result:
        raise RuntimeLoadResearchMeasurementError(
            "research measurement plan must declare at least one sample"
        )
    return tuple(result)


@dataclass(frozen=True)
class DeclaredResearchInterferencePlan:
    plan_id: str
    event_id: str
    spec_digest: str
    financial_plan_id: str
    financial_plan_digest: str
    expected_samples: tuple[ExpectedResearchInterferenceSample, ...]
    declared_journal_sequence: int
    payload_hash: str

    @property
    def expected_sample_ids(self) -> tuple[str, ...]:
        return tuple(value.sample_id for value in self.expected_samples)

    @property
    def digest(self) -> str:
        return payload_digest(
            {
                "schema_version": _RESEARCH_PLAN_SCHEMA_VERSION,
                "plan_id": self.plan_id,
                "event_id": self.event_id,
                "spec_digest": self.spec_digest,
                "financial_plan_id": self.financial_plan_id,
                "financial_plan_digest": self.financial_plan_digest,
                "expected_samples": [value.payload for value in self.expected_samples],
                "declared_journal_sequence": self.declared_journal_sequence,
                "payload_hash": self.payload_hash,
            }
        )


@dataclass(frozen=True)
class DurableResearchInterferenceSample:
    plan_id: str
    plan_digest: str
    sample_id: str
    phase: str
    measurement_event_id: str
    measurement_journal_sequence: int
    monotonic_start_ns: int
    monotonic_end_ns: int
    interference_us: int

    @property
    def digest(self) -> str:
        return payload_digest(
            {
                "schema_version": _RESEARCH_SAMPLE_SCHEMA_VERSION,
                "plan_id": self.plan_id,
                "plan_digest": self.plan_digest,
                "sample_id": self.sample_id,
                "phase": self.phase,
                "measurement_event_id": self.measurement_event_id,
                "measurement_journal_sequence": self.measurement_journal_sequence,
                "monotonic_start_ns": self.monotonic_start_ns,
                "monotonic_end_ns": self.monotonic_end_ns,
                "interference_us": self.interference_us,
            }
        )


def _plan_payload(
    *,
    plan_id: str,
    spec: RuntimeBudgetSpec,
    financial_plan: DeclaredRuntimeEventPlan,
    expected_samples: tuple[ExpectedResearchInterferenceSample, ...],
) -> dict[str, object]:
    return {
        "schema_version": _RESEARCH_PLAN_SCHEMA_VERSION,
        "plan_id": plan_id,
        "spec_digest": spec.digest,
        "financial_plan_id": financial_plan.plan_id,
        "financial_plan_digest": financial_plan.digest,
        "expected_samples": [value.payload for value in expected_samples],
    }


def _read_research_plan(
    store: JournalStore,
    spec: RuntimeBudgetSpec,
    *,
    plan_id: str,
) -> DeclaredResearchInterferencePlan:
    pid = _text(plan_id, name="research_plan_id")
    if type(spec) is not RuntimeBudgetSpec:
        raise TypeError("spec must be exact RuntimeBudgetSpec")
    require_exact_journal_store_authority(
        store,
        subject="runtime qualification JournalStore",
    )
    event_id = _research_plan_event_id(pid)
    event = JournalStore.get_event(store, event_id)
    if event is None:
        raise RuntimeLoadResearchMeasurementError(
            "research interference plan is not durably declared"
        )
    if event.get("event_type") != _RESEARCH_PLAN_EVENT_TYPE:
        raise RuntimeLoadResearchMeasurementError("research plan event type conflicts")
    if event.get("aggregate_type") != _RESEARCH_PLAN_AGGREGATE_TYPE:
        raise RuntimeLoadResearchMeasurementError(
            "research plan aggregate type conflicts"
        )
    if event.get("aggregate_id") != pid or event.get("aggregate_version") != 1:
        raise RuntimeLoadResearchMeasurementError(
            "research plan durable identity conflicts"
        )
    payload = event.get("payload")
    expected_keys = {
        "schema_version",
        "plan_id",
        "spec_digest",
        "financial_plan_id",
        "financial_plan_digest",
        "expected_samples",
    }
    if type(payload) is not dict or set(payload) != expected_keys:
        raise RuntimeLoadResearchMeasurementError("research plan payload is non-canonical")
    if payload.get("schema_version") != _RESEARCH_PLAN_SCHEMA_VERSION:
        raise RuntimeLoadResearchMeasurementError("research plan schema is unsupported")
    if payload.get("plan_id") != pid or payload.get("spec_digest") != spec.digest:
        raise RuntimeLoadResearchMeasurementError(
            "research plan budget or identity binding conflicts"
        )
    financial_plan_id = _text(
        payload.get("financial_plan_id"),
        name="financial_plan_id",
    )
    financial_plan = load_declared_runtime_event_plan(
        store,
        plan_id=financial_plan_id,
        spec=spec,
    )
    if payload.get("financial_plan_digest") != financial_plan.digest:
        raise RuntimeLoadResearchMeasurementError(
            "research plan financial-plan binding conflicts"
        )
    raw_samples = payload.get("expected_samples")
    if type(raw_samples) is not list:
        raise RuntimeLoadResearchMeasurementError(
            "research plan expected_samples must be an array"
        )
    samples = _snapshot_expected_samples(
        tuple(ExpectedResearchInterferenceSample.from_payload(value) for value in raw_samples)
    )
    if [value.payload for value in samples] != raw_samples:
        raise RuntimeLoadResearchMeasurementError(
            "research plan sample declarations are non-canonical"
        )
    sequence = event.get("journal_sequence")
    if type(sequence) is not int or sequence <= financial_plan.declared_journal_sequence:
        raise RuntimeLoadResearchMeasurementError(
            "research plan must be durably declared after its financial plan"
        )
    payload_hash = event.get("payload_hash")
    if type(payload_hash) is not str or payload_hash != payload_digest(payload):
        raise RuntimeLoadResearchMeasurementError("research plan payload hash conflicts")
    return DeclaredResearchInterferencePlan(
        plan_id=pid,
        event_id=event_id,
        spec_digest=spec.digest,
        financial_plan_id=financial_plan.plan_id,
        financial_plan_digest=financial_plan.digest,
        expected_samples=samples,
        declared_journal_sequence=sequence,
        payload_hash=payload_hash,
    )


def declare_research_interference_plan(
    store: JournalStore,
    spec: RuntimeBudgetSpec,
    *,
    plan_id: str,
    financial_plan_id: str,
    expected_samples: tuple[ExpectedResearchInterferenceSample, ...],
) -> DeclaredResearchInterferencePlan:
    """Durably predeclare exact research-pressure sample identities and phases."""

    if type(spec) is not RuntimeBudgetSpec:
        raise TypeError("spec must be exact RuntimeBudgetSpec")
    pid = _text(plan_id, name="research_plan_id")
    financial_plan = load_declared_runtime_event_plan(
        store,
        plan_id=_text(financial_plan_id, name="financial_plan_id"),
        spec=spec,
    )
    samples = _snapshot_expected_samples(expected_samples)
    payload = _plan_payload(
        plan_id=pid,
        spec=spec,
        financial_plan=financial_plan,
        expected_samples=samples,
    )
    event_id = _research_plan_event_id(pid)
    existing = JournalStore.get_event(store, event_id)
    if existing is not None:
        loaded = _read_research_plan(store, spec, plan_id=pid)
        if (
            loaded.financial_plan_digest != financial_plan.digest
            or loaded.expected_samples != samples
        ):
            raise RuntimeLoadResearchMeasurementError(
                "research plan identity was already used for different content"
            )
        return loaded
    JournalStore.append_event(
        store,
        {
            "event_id": event_id,
            "event_type": _RESEARCH_PLAN_EVENT_TYPE,
            "aggregate_type": _RESEARCH_PLAN_AGGREGATE_TYPE,
            "aggregate_id": pid,
            "aggregate_version": "1",
            "payload": payload,
            "payload_hash": payload_digest(payload),
            "committed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        },
    )
    return _read_research_plan(store, spec, plan_id=pid)


def load_declared_research_interference_plan(
    store: JournalStore,
    spec: RuntimeBudgetSpec,
    *,
    plan_id: str,
) -> DeclaredResearchInterferencePlan:
    return _read_research_plan(store, spec, plan_id=plan_id)


def _expected_sample(
    plan: DeclaredResearchInterferencePlan,
    sample_id: str,
) -> tuple[int, ExpectedResearchInterferenceSample]:
    sid = _text(sample_id, name="sample_id")
    for index, expected in enumerate(plan.expected_samples):
        if expected.sample_id == sid:
            return index, expected
    raise RuntimeLoadResearchMeasurementError(
        "research sample is not present in the durable pre-run research plan"
    )


def _function_state(function: object) -> tuple[FunctionType, object, object, object, object]:
    if type(function) is not FunctionType:
        raise RuntimeLoadResearchMeasurementError(
            "research measurement executable authority is unavailable"
        )
    kwdefaults = function.__kwdefaults__
    return (
        function,
        function.__code__,
        function.__defaults__,
        kwdefaults,
        None if kwdefaults is None else tuple(sorted(kwdefaults.items())),
    )


def _require_function_state(
    state: tuple[FunctionType, object, object, object, object],
    *,
    label: str,
) -> None:
    function, code, defaults, kwdefaults, kwdefault_items = state
    if (
        function.__code__ is not code
        or function.__defaults__ is not defaults
        or function.__kwdefaults__ is not kwdefaults
        or (
            kwdefaults is not None
            and tuple(sorted(kwdefaults.items())) != kwdefault_items
        )
    ):
        raise RuntimeLoadResearchMeasurementError(
            f"research measurement executable authority changed: {label}"
        )


def _decode_sample(
    *,
    event: dict[str, object],
    plan: DeclaredResearchInterferencePlan,
    expected_index: int,
    expected_sample_id: str,
    expected_phase: str,
) -> DurableResearchInterferenceSample:
    measurement_id = _research_sample_event_id(plan.plan_id, expected_sample_id)
    if (
        event.get("event_id") != measurement_id
        or event.get("event_type") != _RESEARCH_SAMPLE_EVENT_TYPE
        or event.get("aggregate_type") != _RESEARCH_SAMPLE_AGGREGATE_TYPE
        or event.get("aggregate_id") != plan.plan_id
        or event.get("aggregate_version") != expected_index + 1
    ):
        raise RuntimeLoadResearchMeasurementError(
            "research interference measurement durable binding conflicts"
        )
    payload = event.get("payload")
    expected_keys = {
        "schema_version",
        "plan_id",
        "plan_digest",
        "spec_digest",
        "financial_plan_id",
        "financial_plan_digest",
        "sample_id",
        "phase",
        "pre_operation_journal_sequence",
        "monotonic_start_ns",
        "monotonic_end_ns",
        "interference_us",
    }
    if type(payload) is not dict or set(payload) != expected_keys:
        raise RuntimeLoadResearchMeasurementError(
            "research interference measurement payload is non-canonical"
        )
    if payload.get("schema_version") != _RESEARCH_SAMPLE_SCHEMA_VERSION:
        raise RuntimeLoadResearchMeasurementError(
            "research interference measurement schema is unsupported"
        )
    if (
        payload.get("plan_id") != plan.plan_id
        or payload.get("plan_digest") != plan.digest
        or payload.get("spec_digest") != plan.spec_digest
        or payload.get("financial_plan_id") != plan.financial_plan_id
        or payload.get("financial_plan_digest") != plan.financial_plan_digest
        or payload.get("sample_id") != expected_sample_id
        or payload.get("phase") != expected_phase
    ):
        raise RuntimeLoadResearchMeasurementError(
            "research interference measurement provenance binding conflicts"
        )
    pre_sequence = _non_negative_int(
        payload.get("pre_operation_journal_sequence"),
        name="pre_operation_journal_sequence",
    )
    if pre_sequence < plan.declared_journal_sequence:
        raise RuntimeLoadResearchMeasurementError(
            "research interference measurement predates its declaration"
        )
    start_ns = _non_negative_int(payload.get("monotonic_start_ns"), name="monotonic_start_ns")
    end_ns = _non_negative_int(payload.get("monotonic_end_ns"), name="monotonic_end_ns")
    if end_ns < start_ns:
        raise RuntimeLoadResearchMeasurementError(
            "research interference monotonic clock moved backwards"
        )
    interference_us = _non_negative_int(payload.get("interference_us"), name="interference_us")
    if interference_us != (end_ns - start_ns + 999) // 1_000:
        raise RuntimeLoadResearchMeasurementError(
            "research interference duration conflicts with raw endpoints"
        )
    sequence = event.get("journal_sequence")
    if type(sequence) is not int or sequence <= pre_sequence:
        raise RuntimeLoadResearchMeasurementError(
            "research interference measurement lacks durable post-operation order"
        )
    return DurableResearchInterferenceSample(
        plan_id=plan.plan_id,
        plan_digest=plan.digest,
        sample_id=expected_sample_id,
        phase=expected_phase,
        measurement_event_id=measurement_id,
        measurement_journal_sequence=sequence,
        monotonic_start_ns=start_ns,
        monotonic_end_ns=end_ns,
        interference_us=interference_us,
    )


def measure_declared_research_interference(
    store: JournalStore,
    spec: RuntimeBudgetSpec,
    *,
    plan_id: str,
    sample_id: str,
    operation: Callable[[], _T],
) -> tuple[_T, DurableResearchInterferenceSample]:
    """Measure one predeclared provider-free research-pressure operation."""

    if not callable(operation):
        raise TypeError("operation must be callable")
    store_identity = require_exact_journal_store_authority(
        store,
        subject="runtime qualification JournalStore",
    )
    plan = _read_research_plan(store, spec, plan_id=plan_id)
    expected_index, expected = _expected_sample(plan, sample_id)
    expected_sample_id = expected.sample_id
    expected_phase = expected.phase
    plan_id_value = plan.plan_id
    plan_digest_value = plan.digest
    spec_digest_value = plan.spec_digest
    financial_plan_id_value = plan.financial_plan_id
    financial_plan_digest_value = plan.financial_plan_digest
    measurement_id = _research_sample_event_id(plan_id_value, expected_sample_id)

    # Freeze every authority used after caller-controlled research code. Research
    # pressure is intentionally arbitrary same-process work, so the evidence issuer
    # must reject both direct rebinding and same-object executable/state mutation.
    type_for = type
    dict_type = dict
    int_type = int
    tuple_for = tuple
    sorted_for = sorted
    getattr_for = getattr
    object_getattribute = object.__getattribute__
    module_type = ModuleType
    str_for = str
    value_error_type = ValueError
    missing = object()
    error_type = RuntimeLoadResearchMeasurementError
    clock = perf_counter_ns
    store_type = JournalStore
    get_event = JournalStore.get_event
    current_sequence = JournalStore.current_journal_sequence
    append_event = JournalStore.append_event
    decode_sample = _decode_sample
    payload_digest_for = payload_digest
    datetime_type = datetime
    timezone_type = timezone
    plan_type = DeclaredResearchInterferencePlan
    sample_type = DurableResearchInterferenceSample
    sample_init = DurableResearchInterferenceSample.__init__
    plan_digest_getter = DeclaredResearchInterferencePlan.digest.fget
    sample_event_type = _RESEARCH_SAMPLE_EVENT_TYPE
    sample_aggregate_type = _RESEARCH_SAMPLE_AGGREGATE_TYPE
    sample_schema_version = _RESEARCH_SAMPLE_SCHEMA_VERSION
    module_namespace = globals()

    digest_namespace = payload_digest_for.__globals__
    digest_sha256 = digest_namespace.get("sha256")
    canonical_json_for = digest_namespace.get("canonical_json")
    if not callable(digest_sha256) or type_for(canonical_json_for) is not FunctionType:
        raise error_type("research measurement payload digest authority is not canonical")
    canonical_json_namespace = canonical_json_for.__globals__
    json_module = canonical_json_namespace.get("json")
    if type_for(json_module) is not module_type:
        raise error_type("research measurement canonical JSON authority is not a module")
    json_encoder_type = json_module.__dict__.get("JSONEncoder")
    if type_for(json_encoder_type) is not type_for:
        raise error_type("research measurement canonical JSON encoder authority is not exact")
    json_encoder_methods = tuple_for(
        (name, json_encoder_type.__dict__.get(name))
        for name in ("__init__", "default", "encode", "iterencode")
    )
    if any(
        type_for(function) is not FunctionType
        for _name, function in json_encoder_methods
    ):
        raise error_type(
            "research measurement canonical JSON encoder executable authority is unavailable"
        )

    module_bindings = (
        ("perf_counter_ns", clock),
        ("RuntimeLoadResearchMeasurementError", error_type),
        ("JournalStore", store_type),
        ("_decode_sample", decode_sample),
        ("payload_digest", payload_digest_for),
        ("datetime", datetime_type),
        ("timezone", timezone_type),
        ("DeclaredResearchInterferencePlan", plan_type),
        ("DurableResearchInterferenceSample", sample_type),
        ("_RESEARCH_SAMPLE_EVENT_TYPE", sample_event_type),
        ("_RESEARCH_SAMPLE_AGGREGATE_TYPE", sample_aggregate_type),
        ("_RESEARCH_SAMPLE_SCHEMA_VERSION", sample_schema_version),
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
            else tuple(sorted_for(function.__kwdefaults__.items())),
        )
        for name, function in (
            ("JournalStore.get_event", get_event),
            ("JournalStore.current_journal_sequence", current_sequence),
            ("JournalStore.append_event", append_event),
            ("_decode_sample", decode_sample),
            ("payload_digest", payload_digest_for),
            ("payload_digest.canonical_json", canonical_json_for),
            *(
                (f"json.JSONEncoder.{name}", function)
                for name, function in json_encoder_methods
            ),
            ("DeclaredResearchInterferencePlan.digest", plan_digest_getter),
            ("DurableResearchInterferenceSample.__init__", sample_init),
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
        (name, getattr_for(store_type, name)) for name in journal_dependency_names
    )
    journal_schema_version = store_type.SCHEMA_VERSION
    dependency_graph = _capture_operation_dependency_graph(
        tuple(function for _name, function, *_state in protected_functions)
        + tuple(value for _name, value in journal_dependencies)
    )
    protected_domain_classes = tuple(
        (
            label,
            class_type,
            tuple_for(class_type.__dict__),
            tuple_for(class_type.__dict__.items()),
            tuple_for(
                (
                    name,
                    value,
                    value.__code__,
                    value.__defaults__,
                    value.__kwdefaults__,
                    None
                    if value.__kwdefaults__ is None
                    else tuple_for(sorted_for(value.__kwdefaults__.items())),
                )
                for name, value in class_type.__dict__.items()
                if type_for(value) is FunctionType
            ),
        )
        for label, class_type in (
            ("DeclaredResearchInterferencePlan", plan_type),
            ("DurableResearchInterferenceSample", sample_type),
            ("json.JSONEncoder", json_encoder_type),
        )
    )

    store_state = object_getattribute(store, "__dict__")
    if type_for(store_state) is not dict_type:
        raise error_type("research measurement JournalStore instance state is non-canonical")
    store_state_snapshot = tuple_for(store_state.items())
    store_state_names = tuple_for(store_state)
    stored_identity = store_state.get("_store_identity")
    identity_type = type_for(store_identity)
    if type_for(stored_identity) is not identity_type:
        raise error_type("research measurement stored JournalStore identity is non-canonical")
    selected_identity_state = object_getattribute(store_identity, "__dict__")
    stored_identity_state = object_getattribute(stored_identity, "__dict__")
    if (
        type_for(selected_identity_state) is not dict_type
        or type_for(stored_identity_state) is not dict_type
    ):
        raise error_type("research measurement JournalStore identity state is non-canonical")
    selected_identity_snapshot = tuple_for(selected_identity_state.items())
    stored_identity_snapshot = tuple_for(stored_identity_state.items())
    identity_state_names = tuple_for(selected_identity_state)
    if tuple_for(stored_identity_state) != identity_state_names:
        raise error_type("research measurement stored JournalStore identity shape conflicts")
    identity_class_member_names = tuple_for(identity_type.__dict__)
    identity_class_members = tuple_for(identity_type.__dict__.items())
    identity_class_executables = tuple_for(
        (
            name,
            value,
            value.__code__,
            value.__defaults__,
            value.__kwdefaults__,
            None
            if value.__kwdefaults__ is None
            else tuple_for(sorted_for(value.__kwdefaults__.items())),
        )
        for name, value in identity_class_members
        if type_for(value) is FunctionType
    )
    plan_state = object_getattribute(plan, "__dict__")
    if type_for(plan_state) is not dict_type:
        raise error_type("research measurement plan instance state is non-canonical")
    plan_state_snapshot = tuple_for(plan_state.items())
    plan_state_names = tuple_for(plan_state)

    def require_post_callback_authority() -> None:
        for label, class_type, member_names, members, class_executables in protected_domain_classes:
            current_namespace = class_type.__dict__
            if tuple_for(current_namespace) != member_names:
                raise error_type(
                    f"research measurement {label} class shape changed during callback"
                )
            for name, expected_value in members:
                if current_namespace.get(name, missing) is not expected_value:
                    raise error_type(
                        "research measurement class authority changed during callback: "
                        f"{label}.{name}"
                    )
            for name, function, code, defaults, kwdefaults, kwdefault_items in class_executables:
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
                        "research measurement class executable authority changed during callback: "
                        f"{label}.{name}"
                    )

        if type_for(store) is not store_type:
            raise error_type("research measurement JournalStore class changed during callback")
        current_store_state = object_getattribute(store, "__dict__")
        if type_for(current_store_state) is not dict_type:
            raise error_type("research measurement JournalStore state became non-canonical")
        if tuple_for(current_store_state) != store_state_names:
            raise error_type("research measurement JournalStore state shape changed during callback")
        for name, expected_value in store_state_snapshot:
            if current_store_state.get(name, missing) is not expected_value:
                raise error_type(
                    "research measurement JournalStore instance state changed during callback: "
                    f"{name}"
                )

        if type_for(plan) is not plan_type:
            raise error_type("research measurement plan class changed during callback")
        current_plan_state = object_getattribute(plan, "__dict__")
        if type_for(current_plan_state) is not dict_type or tuple_for(current_plan_state) != plan_state_names:
            raise error_type("research measurement plan instance state shape changed during callback")
        for name, expected_value in plan_state_snapshot:
            if current_plan_state.get(name, missing) is not expected_value:
                raise error_type(
                    "research measurement plan instance state changed during callback: "
                    f"{name}"
                )
        for value, snapshot, label in (
            (store_identity, selected_identity_snapshot, "selected JournalStore identity"),
            (stored_identity, stored_identity_snapshot, "stored JournalStore identity"),
        ):
            if type_for(value) is not identity_type:
                raise error_type(f"research measurement {label} class changed during callback")
            current_identity_state = object_getattribute(value, "__dict__")
            if type_for(current_identity_state) is not dict_type:
                raise error_type(f"research measurement {label} state became non-canonical")
            if tuple_for(current_identity_state) != identity_state_names:
                raise error_type(f"research measurement {label} state shape changed during callback")
            for name, expected_value in snapshot:
                if current_identity_state.get(name, missing) is not expected_value:
                    raise error_type(
                        f"research measurement {label} state changed during callback: {name}"
                    )
        if tuple_for(identity_type.__dict__) != identity_class_member_names:
            raise error_type("research measurement JournalStore identity class shape changed during callback")
        for name, expected_value in identity_class_members:
            if identity_type.__dict__.get(name, missing) is not expected_value:
                raise error_type(
                    "research measurement JournalStore identity class authority changed during callback: "
                    f"{name}"
                )
        for name, function, code, defaults, kwdefaults, kwdefault_items in identity_class_executables:
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
                    "research measurement JournalStore identity executable authority changed during callback: "
                    f"{name}"
                )

        for name, expected_value in module_bindings:
            if module_namespace.get(name, missing) is not expected_value:
                raise error_type(
                    f"research measurement authority changed during callback: {name}"
                )
        for namespace, dependency_name, expected_value, label in transitive_bindings:
            if namespace.get(dependency_name, missing) is not expected_value:
                raise error_type(
                    "research measurement transitive authority changed during callback: "
                    f"{label}"
                )
        if (
            store_type.get_event is not get_event
            or store_type.current_journal_sequence is not current_sequence
            or store_type.append_event is not append_event
            or plan_type.digest.fget is not plan_digest_getter
            or sample_type.__init__ is not sample_init
        ):
            raise error_type("research measurement class authority changed during callback")
        for name, expected_dependency in journal_dependencies:
            if getattr_for(store_type, name, missing) is not expected_dependency:
                raise error_type(
                    "research measurement JournalStore dependency changed during callback: "
                    f"{name}"
                )
        if store_type.SCHEMA_VERSION is not journal_schema_version:
            raise error_type("research measurement JournalStore schema authority changed during callback")
        for name, function, code, defaults, kwdefaults, kwdefault_items in protected_functions:
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
                    "research measurement executable authority changed during callback: "
                    f"{name}"
                )

        graph_functions, graph_globals, graph_builtins, graph_module_members, graph_closures = dependency_graph
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
                    "research measurement transitive executable authority changed during callback: "
                    f"{function.__module__}.{function.__qualname__}"
                )
        for namespace, dependency_name, expected_dependency in graph_globals:
            if namespace.get(dependency_name, missing) is not expected_dependency:
                raise error_type(
                    "research measurement transitive global authority changed during callback: "
                    f"{dependency_name}"
                )
        for namespace, dependency_name, expected_dependency in graph_builtins:
            if namespace.get(dependency_name, missing) is not expected_dependency:
                raise error_type(
                    "research measurement transitive builtin authority changed during callback: "
                    f"{dependency_name}"
                )
        for module, member_name, expected_member in graph_module_members:
            if type_for(module) is not module_type:
                raise error_type(
                    "research measurement transitive module class changed during callback"
                )
            if getattr_for(module, member_name, missing) is not expected_member:
                raise error_type(
                    "research measurement transitive module authority changed during callback: "
                    f"{module.__name__}.{member_name}"
                )
        for cell, had_value, expected_value in graph_closures:
            try:
                current_value = cell.cell_contents
            except value_error_type:
                if had_value:
                    raise error_type("research measurement closure authority changed during callback")
                continue
            if not had_value or current_value is not expected_value:
                raise error_type("research measurement closure authority changed during callback")

        # Recompute the plan digest only after every transitive digest/JSON
        # authority used to compute it has been revalidated.  Otherwise a hostile
        # callback can replace canonical_json and turn a TCB mutation into the
        # weaker symptom "plan digest changed".
        if plan.digest != plan_digest_value:
            raise error_type("research measurement plan digest changed during callback")

    with journal_store_authority_scope(store, store_identity):
        if get_event(store, measurement_id) is not None:
            raise error_type("research interference sample was already measured")
        pre_sequence = current_sequence(store)
        start_ns = clock()

        verifier = require_post_callback_authority
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
            clock,
            start_ns,
            type_for,
            int_type,
            error_type,
            current_sequence,
            store,
            pre_sequence,
            sample_schema_version,
            plan_id_value,
            plan_digest_value,
            spec_digest_value,
            financial_plan_id_value,
            financial_plan_digest_value,
            expected_sample_id,
            expected_phase,
            sample_event_type,
            sample_aggregate_type,
            measurement_id,
            str_for,
            expected_index,
            payload_digest_for,
            datetime_type,
            timezone_type,
            append_event,
            get_event,
            decode_sample,
            plan,
        )
        callback_continuation, result = (callback_continuation, operation())
        (
            require_post_callback_authority,
            verifier_code,
            verifier_defaults,
            verifier_kwdefaults,
            verifier_closure_snapshot,
            clock,
            start_ns,
            type_for,
            int_type,
            error_type,
            current_sequence,
            store,
            pre_sequence,
            sample_schema_version,
            plan_id_value,
            plan_digest_value,
            spec_digest_value,
            financial_plan_id_value,
            financial_plan_digest_value,
            expected_sample_id,
            expected_phase,
            sample_event_type,
            sample_aggregate_type,
            measurement_id,
            str_for,
            expected_index,
            payload_digest_for,
            datetime_type,
            timezone_type,
            append_event,
            get_event,
            decode_sample,
            plan,
        ) = callback_continuation
        for cell, expected_value in verifier_closure_snapshot:
            cell.cell_contents = expected_value
        if (
            require_post_callback_authority.__code__ is not verifier_code
            or require_post_callback_authority.__defaults__ is not verifier_defaults
            or require_post_callback_authority.__kwdefaults__ is not verifier_kwdefaults
        ):
            raise error_type("research measurement verifier authority changed during callback")
        require_post_callback_authority()
        end_ns = clock()
        if (
            type_for(start_ns) is not int_type
            or type_for(end_ns) is not int_type
            or start_ns < 0
            or end_ns < start_ns
        ):
            raise error_type("system monotonic clock produced an invalid research interval")
        require_post_callback_authority()
        if current_sequence(store) != pre_sequence:
            raise error_type("research pressure operation mutated the qualification JournalStore")
        interference_us = (end_ns - start_ns + 999) // 1_000
        payload = {
            "schema_version": sample_schema_version,
            "plan_id": plan_id_value,
            "plan_digest": plan_digest_value,
            "spec_digest": spec_digest_value,
            "financial_plan_id": financial_plan_id_value,
            "financial_plan_digest": financial_plan_digest_value,
            "sample_id": expected_sample_id,
            "phase": expected_phase,
            "pre_operation_journal_sequence": pre_sequence,
            "monotonic_start_ns": start_ns,
            "monotonic_end_ns": end_ns,
            "interference_us": interference_us,
        }
        require_post_callback_authority()
        append_event(
            store,
            {
                "event_id": measurement_id,
                "event_type": sample_event_type,
                "aggregate_type": sample_aggregate_type,
                "aggregate_id": plan_id_value,
                "aggregate_version": str_for(expected_index + 1),
                "payload": payload,
                "payload_hash": payload_digest_for(payload),
                "committed_at": datetime_type.now(timezone_type.utc)
                .isoformat()
                .replace("+00:00", "Z"),
            },
        )
        event = get_event(store, measurement_id)
        if event is None:
            raise error_type("durable research interference measurement disappeared")
        require_post_callback_authority()
        sample = decode_sample(
            event=event,
            plan=plan,
            expected_index=expected_index,
            expected_sample_id=expected_sample_id,
            expected_phase=expected_phase,
        )
        return result, sample


def load_declared_research_interference_samples(
    store: JournalStore,
    spec: RuntimeBudgetSpec,
    *,
    plan_id: str,
) -> tuple[DurableResearchInterferenceSample, ...]:
    """Reload every predeclared research sample in declared order."""

    store_identity = require_exact_journal_store_authority(
        store,
        subject="runtime qualification JournalStore",
    )
    with journal_store_authority_scope(store, store_identity):
        plan = _read_research_plan(store, spec, plan_id=plan_id)
        result: list[DurableResearchInterferenceSample] = []
        for index, expected in enumerate(plan.expected_samples):
            event = JournalStore.get_event(
                store,
                _research_sample_event_id(plan.plan_id, expected.sample_id),
            )
            if event is None:
                raise RuntimeLoadResearchMeasurementError(
                    "declared research pressure sample lacks a durable monotonic measurement"
                )
            result.append(
                _decode_sample(
                    event=event,
                    plan=plan,
                    expected_index=index,
                    expected_sample_id=expected.sample_id,
                    expected_phase=expected.phase,
                )
            )
        return tuple(result)


def evaluate_durable_provider_free_runtime_budget(
    spec: RuntimeBudgetSpec,
    store: JournalStore,
    *,
    financial_plan_id: str,
    research_plan_id: str,
) -> tuple[
    RuntimeBudgetDecision,
    JournalConservationEvidence,
    DeclaredRuntimeEventPlan,
    tuple[DurableFinancialLatencySample, ...],
    tuple[DurableResearchInterferenceSample, ...],
]:
    """Evaluate only runner-owned durable metric series on the provider-free basis.

    Financial staleness intentionally uses the same raw monotonic endpoints as
    durable financial latency: declared work start to durable event observation.
    This is *not* provider/exchange market-data freshness. The underlying current-
    main evaluator remains INCONCLUSIVE until terminal target-host provenance is
    reconstructed, so this helper cannot promote the diagnostic result to PASS.
    """

    financial_samples = load_declared_financial_latency_samples(
        store,
        spec,
        plan_id=financial_plan_id,
    )
    research_plan = _read_research_plan(store, spec, plan_id=research_plan_id)
    financial_plan = load_declared_runtime_event_plan(
        store,
        plan_id=financial_plan_id,
        spec=spec,
    )
    if (
        research_plan.financial_plan_id != financial_plan.plan_id
        or research_plan.financial_plan_digest != financial_plan.digest
    ):
        raise RuntimeLoadResearchMeasurementError(
            "research measurements belong to another durable financial plan"
        )
    research_samples = load_declared_research_interference_samples(
        store,
        spec,
        plan_id=research_plan_id,
    )
    decision, evidence, loaded_plan, loaded_financial = (
        evaluate_monotonic_declared_runtime_budget(
            spec,
            store,
            plan_id=financial_plan_id,
            financial_staleness_us=tuple(
                sample.latency_us for sample in financial_samples
            ),
            research_interference_us=tuple(
                sample.interference_us for sample in research_samples
            ),
        )
    )
    if tuple(value.digest for value in loaded_financial) != tuple(
        value.digest for value in financial_samples
    ):
        raise RuntimeLoadResearchMeasurementError(
            "durable financial samples changed during provider-free evaluation"
        )
    return decision, evidence, loaded_plan, loaded_financial, research_samples
