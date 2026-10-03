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
    # Freeze the exact clock callable before caller-controlled product code runs.
    # Rebinding the module global during the operation cannot change the terminal
    # sample; attempted replacement is rejected before latency evidence commits.
    clock = perf_counter_ns
    store_identity = require_exact_journal_store_authority(
        store,
        subject="runtime qualification JournalStore",
    )
    # The operation callback is inside the evidence issuance boundary. Hold the
    # exact physical JournalStore generation across pre-cut, operation, event
    # readback and measurement commit so callback code cannot rebind the same
    # Python object to another valid journal and splice two generations.
    with journal_store_authority_scope(store, store_identity):
        plan = load_declared_runtime_event_plan(store, plan_id=plan_id, spec=spec)
        expected_index, expected = _expected_for_id(plan, event_id)
        measurement_id = _measurement_event_id(plan.plan_id, expected.event_id)
        if JournalStore.get_event(store, measurement_id) is not None:
            raise RuntimeLoadMeasurementError("financial latency was already measured")
        if JournalStore.get_event(store, expected.event_id) is not None:
            raise RuntimeLoadMeasurementError(
                "predeclared financial event already exists before monotonic measurement"
            )

        pre_sequence = JournalStore.current_journal_sequence(store)
        start_ns = clock()
        if JournalStore.get_event(store, expected.event_id) is not None:
            raise RuntimeLoadMeasurementError(
                "predeclared financial event appeared before monotonic measurement start"
            )
        result = operation()
        if perf_counter_ns is not clock:
            raise RuntimeLoadMeasurementError(
                "financial latency clock authority changed during measured operation"
            )
        # Bind the expected durable event before sampling the terminal clock. If the
        # operation returned without publishing it, an unrelated commit racing with
        # end-clock sampling must not be attributed to the measured operation.
        financial_event = _require_expected_event(
            JournalStore.get_event(store, expected.event_id),
            expected,
            after_sequence=pre_sequence,
        )
        end_ns = clock()
        if (
            type(start_ns) is not int
            or type(end_ns) is not int
            or start_ns < 0
            or end_ns < start_ns
        ):
            raise RuntimeLoadMeasurementError(
                "system monotonic clock produced an invalid interval"
            )

        latency_us = (end_ns - start_ns + 999) // 1_000
        payload = {
            "schema_version": _MEASUREMENT_SCHEMA_VERSION,
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
        JournalStore.append_event(
            store,
            {
                "event_id": measurement_id,
                "event_type": _MEASUREMENT_EVENT_TYPE,
                "aggregate_type": _MEASUREMENT_AGGREGATE_TYPE,
                "aggregate_id": plan.plan_id,
                "aggregate_version": str(expected_index + 1),
                "payload": payload,
                "payload_hash": payload_digest(payload),
                "committed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            },
        )
        measurement_event = JournalStore.get_event(store, measurement_id)
        if measurement_event is None:
            raise RuntimeLoadMeasurementError("durable latency measurement disappeared")
        sample = _decode_measurement(
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

    Staleness/interference remain explicit inputs until their own raw target-host
    collectors are implemented; this function therefore does not represent
    terminal WP-65 qualification.
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
            declared_duration_us=declared_duration_us,
            observed_duration_us=observed_duration_us,
        )
        return decision, evidence, plan, samples
