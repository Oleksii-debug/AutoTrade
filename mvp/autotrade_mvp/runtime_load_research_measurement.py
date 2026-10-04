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
from types import FunctionType
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

    clock = perf_counter_ns
    store_type = JournalStore
    get_event = JournalStore.get_event
    current_sequence = JournalStore.current_journal_sequence
    append_event = JournalStore.append_event
    get_event_state = _function_state(get_event)
    current_sequence_state = _function_state(current_sequence)
    append_event_state = _function_state(append_event)
    module_namespace = globals()

    def require_post_callback_authority() -> None:
        if module_namespace.get("perf_counter_ns") is not clock:
            raise RuntimeLoadResearchMeasurementError(
                "research measurement clock authority changed during callback"
            )
        if module_namespace.get("JournalStore") is not store_type:
            raise RuntimeLoadResearchMeasurementError(
                "research measurement JournalStore authority changed during callback"
            )
        if (
            JournalStore.get_event is not get_event
            or JournalStore.current_journal_sequence is not current_sequence
            or JournalStore.append_event is not append_event
        ):
            raise RuntimeLoadResearchMeasurementError(
                "research measurement JournalStore method authority changed during callback"
            )
        _require_function_state(get_event_state, label="JournalStore.get_event")
        _require_function_state(
            current_sequence_state,
            label="JournalStore.current_journal_sequence",
        )
        _require_function_state(append_event_state, label="JournalStore.append_event")

    with journal_store_authority_scope(store, store_identity):
        if get_event(store, measurement_id) is not None:
            raise RuntimeLoadResearchMeasurementError(
                "research interference sample was already measured"
            )
        pre_sequence = current_sequence(store)
        start_ns = clock()
        result = operation()
        require_post_callback_authority()
        end_ns = clock()
        if (
            type(start_ns) is not int
            or type(end_ns) is not int
            or start_ns < 0
            or end_ns < start_ns
        ):
            raise RuntimeLoadResearchMeasurementError(
                "system monotonic clock produced an invalid research interval"
            )
        if current_sequence(store) != pre_sequence:
            raise RuntimeLoadResearchMeasurementError(
                "research pressure operation mutated the qualification JournalStore"
            )
        interference_us = (end_ns - start_ns + 999) // 1_000
        payload = {
            "schema_version": _RESEARCH_SAMPLE_SCHEMA_VERSION,
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
        append_event(
            store,
            {
                "event_id": measurement_id,
                "event_type": _RESEARCH_SAMPLE_EVENT_TYPE,
                "aggregate_type": _RESEARCH_SAMPLE_AGGREGATE_TYPE,
                "aggregate_id": plan_id_value,
                "aggregate_version": str(expected_index + 1),
                "payload": payload,
                "payload_hash": payload_digest(payload),
                "committed_at": datetime.now(timezone.utc)
                .isoformat()
                .replace("+00:00", "Z"),
            },
        )
        event = get_event(store, measurement_id)
        if event is None:
            raise RuntimeLoadResearchMeasurementError(
                "durable research interference measurement disappeared"
            )
        sample = _decode_sample(
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
