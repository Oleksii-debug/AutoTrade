"""Durable pre-run workload declarations for WP-65 runtime qualification.

A performance campaign cannot prove financial-event conservation when the set of
"expected" events may be chosen after outcomes are visible. This module freezes
an ordered sequence of exact journal bindings in the canonical JournalStore
before the workload runs, then reloads that declaration during evaluation.

The declaration is causal evidence only. It does not create a load generator,
trusted clock, release attestation, provider authority, or performance claim.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from typing import Sequence

from .performance_qualification import RuntimeBudgetDecision, RuntimeBudgetSpec
from .persistence import (
    JournalStore,
    payload_digest,
    require_exact_journal_store_authority,
)
from .runtime_load_evidence import (
    ExpectedJournalEvent,
    JournalConservationEvidence,
    evaluate_journal_backed_runtime_budget,
    snapshot_expected_journal_events,
)
from .store_identity import JournalStoreIdentity


_PLAN_EVENT_TYPE = "RuntimeQualificationPlanDeclared"
_PLAN_AGGREGATE_TYPE = "runtime_qualification_plan"
_PLAN_SCHEMA_VERSION = "1.1.0"
_MAX_JOURNAL_EVENTS = 100_000


class RuntimeLoadPlanError(ValueError):
    """Raised when a durable runtime-load declaration is absent or conflicts."""


def _text(value: object, *, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise RuntimeLoadPlanError(f"{name} must be canonical non-empty text")
    return value


def _journal_event_limit(value: object) -> int:
    if type(value) is not int or value < 1 or value > _MAX_JOURNAL_EVENTS:
        raise RuntimeLoadPlanError(
            f"max_journal_events must be between 1 and {_MAX_JOURNAL_EVENTS}"
        )
    return value


def _validated_spec(value: RuntimeBudgetSpec) -> RuntimeBudgetSpec:
    """Reconstruct the exact budget so post-construction mutation cannot persist."""

    if type(value) is not RuntimeBudgetSpec:
        raise TypeError("spec must be exact RuntimeBudgetSpec")
    return RuntimeBudgetSpec(
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


def _store_identity_digest(identity: JournalStoreIdentity) -> str:
    return payload_digest(
        {
            "canonical_path": identity.canonical_path,
            "filesystem_device": identity.filesystem_device,
            "filesystem_inode": identity.filesystem_inode,
            "identity_source": identity.identity_source,
            "windows_volume_serial": identity.windows_volume_serial,
            "windows_file_index_high": identity.windows_file_index_high,
            "windows_file_index_low": identity.windows_file_index_low,
        }
    )


def _plan_event_id(plan_id: str) -> str:
    digest = sha256(plan_id.encode("utf-8")).hexdigest()
    return f"runtime-qualification-plan-{digest}"


def _plan_payload(
    *,
    plan_id: str,
    spec: RuntimeBudgetSpec,
    expected_events: tuple[ExpectedJournalEvent, ...],
    max_journal_events: int,
    store_identity_digest: str,
) -> dict[str, object]:
    return {
        "schema_version": _PLAN_SCHEMA_VERSION,
        "plan_id": plan_id,
        "scenario_id": spec.scenario_id,
        "spec_digest": spec.digest,
        "release_sha": spec.release_sha,
        "configuration_hash": spec.configuration_hash,
        "host_fingerprint": spec.host_fingerprint,
        "expected_events": [value.payload for value in expected_events],
        "max_journal_events": max_journal_events,
        "store_identity_digest": store_identity_digest,
    }


@dataclass(frozen=True)
class DeclaredRuntimeEventPlan:
    """Read-only projection of one JournalStore-issued pre-run declaration."""

    plan_id: str
    event_id: str
    scenario_id: str
    spec_digest: str
    expected_events: tuple[ExpectedJournalEvent, ...]
    max_journal_events: int
    store_identity_digest: str
    declared_journal_sequence: int
    payload_hash: str

    @property
    def expected_event_ids(self) -> tuple[str, ...]:
        return tuple(value.event_id for value in self.expected_events)

    @property
    def digest(self) -> str:
        return payload_digest(
            {
                "schema_version": _PLAN_SCHEMA_VERSION,
                "plan_id": self.plan_id,
                "event_id": self.event_id,
                "scenario_id": self.scenario_id,
                "spec_digest": self.spec_digest,
                "expected_events": [value.payload for value in self.expected_events],
                "max_journal_events": self.max_journal_events,
                "store_identity_digest": self.store_identity_digest,
                "declared_journal_sequence": self.declared_journal_sequence,
                "payload_hash": self.payload_hash,
            }
        )


def _read_plan(
    store: JournalStore,
    *,
    spec: RuntimeBudgetSpec,
    plan_id: str,
) -> DeclaredRuntimeEventPlan:
    spec = _validated_spec(spec)
    pid = _text(plan_id, name="plan_id")
    identity = require_exact_journal_store_authority(
        store,
        subject="runtime qualification JournalStore",
    )
    event_id = _plan_event_id(pid)
    event = JournalStore.get_event(store, event_id)
    if event is None:
        raise RuntimeLoadPlanError("runtime qualification plan is not durably declared")
    if event.get("event_type") != _PLAN_EVENT_TYPE:
        raise RuntimeLoadPlanError("runtime qualification plan event type conflicts")
    if event.get("aggregate_type") != _PLAN_AGGREGATE_TYPE:
        raise RuntimeLoadPlanError("runtime qualification plan aggregate type conflicts")
    if event.get("aggregate_id") != pid:
        raise RuntimeLoadPlanError("runtime qualification plan aggregate identity conflicts")
    if event.get("aggregate_version") != 1:
        raise RuntimeLoadPlanError("runtime qualification plan must be immutable version 1")

    payload = event.get("payload")
    if type(payload) is not dict:
        raise RuntimeLoadPlanError("runtime qualification plan payload is invalid")
    expected_keys = {
        "schema_version",
        "plan_id",
        "scenario_id",
        "spec_digest",
        "release_sha",
        "configuration_hash",
        "host_fingerprint",
        "expected_events",
        "max_journal_events",
        "store_identity_digest",
    }
    if set(payload) != expected_keys:
        raise RuntimeLoadPlanError("runtime qualification plan payload shape conflicts")
    if payload.get("schema_version") != _PLAN_SCHEMA_VERSION:
        raise RuntimeLoadPlanError("runtime qualification plan schema is unsupported")
    if payload.get("plan_id") != pid:
        raise RuntimeLoadPlanError("runtime qualification plan payload identity conflicts")
    if payload.get("scenario_id") != spec.scenario_id:
        raise RuntimeLoadPlanError("runtime qualification plan scenario conflicts")
    if payload.get("spec_digest") != spec.digest:
        raise RuntimeLoadPlanError("runtime qualification plan budget spec conflicts")
    if payload.get("release_sha") != spec.release_sha:
        raise RuntimeLoadPlanError("runtime qualification plan release conflicts")
    if payload.get("configuration_hash") != spec.configuration_hash:
        raise RuntimeLoadPlanError("runtime qualification plan configuration conflicts")
    if payload.get("host_fingerprint") != spec.host_fingerprint:
        raise RuntimeLoadPlanError("runtime qualification plan host conflicts")

    raw_events = payload.get("expected_events")
    if type(raw_events) is not list:
        raise RuntimeLoadPlanError("runtime qualification plan event bindings are invalid")
    try:
        canonical_events = snapshot_expected_journal_events(
            tuple(ExpectedJournalEvent.from_payload(value) for value in raw_events)
        )
    except (RuntimeError, TypeError, ValueError) as error:
        raise RuntimeLoadPlanError(
            "runtime qualification plan event bindings are invalid"
        ) from error
    if [value.payload for value in canonical_events] != raw_events:
        raise RuntimeLoadPlanError(
            "runtime qualification plan event bindings are non-canonical"
        )
    max_events = _journal_event_limit(payload.get("max_journal_events"))
    actual_store_digest = _store_identity_digest(identity)
    if payload.get("store_identity_digest") != actual_store_digest:
        raise RuntimeLoadPlanError("runtime qualification plan belongs to another journal")

    sequence = event.get("journal_sequence")
    if type(sequence) is not int or sequence <= 0:
        raise RuntimeLoadPlanError("runtime qualification plan lacks durable journal order")
    payload_hash = event.get("payload_hash")
    if type(payload_hash) is not str or payload_hash != payload_digest(payload):
        raise RuntimeLoadPlanError("runtime qualification plan payload hash conflicts")

    return DeclaredRuntimeEventPlan(
        plan_id=pid,
        event_id=event_id,
        scenario_id=spec.scenario_id,
        spec_digest=spec.digest,
        expected_events=canonical_events,
        max_journal_events=max_events,
        store_identity_digest=actual_store_digest,
        declared_journal_sequence=sequence,
        payload_hash=payload_hash,
    )


def declare_runtime_event_plan(
    store: JournalStore,
    *,
    plan_id: str,
    spec: RuntimeBudgetSpec,
    expected_events: Sequence[ExpectedJournalEvent],
    max_journal_events: int = 100_000,
) -> DeclaredRuntimeEventPlan:
    """Durably freeze exact ordered financial-event bindings before a campaign.

    Exact re-declaration of an existing plan is idempotent. Reusing the same
    plan identity with different event/spec/store semantics fails closed.
    """

    spec = _validated_spec(spec)
    pid = _text(plan_id, name="plan_id")
    events = snapshot_expected_journal_events(expected_events)
    limit = _journal_event_limit(max_journal_events)
    identity = require_exact_journal_store_authority(
        store,
        subject="runtime qualification JournalStore",
    )
    store_digest = _store_identity_digest(identity)
    payload = _plan_payload(
        plan_id=pid,
        spec=spec,
        expected_events=events,
        max_journal_events=limit,
        store_identity_digest=store_digest,
    )
    event_id = _plan_event_id(pid)

    existing = JournalStore.get_event(store, event_id)
    if existing is not None:
        loaded = _read_plan(store, spec=spec, plan_id=pid)
        if (
            loaded.expected_events != events
            or loaded.max_journal_events != limit
            or loaded.store_identity_digest != store_digest
        ):
            raise RuntimeLoadPlanError(
                "runtime qualification plan identity was already used for different content"
            )
        return loaded

    envelope = {
        "event_id": event_id,
        "event_type": _PLAN_EVENT_TYPE,
        "aggregate_type": _PLAN_AGGREGATE_TYPE,
        "aggregate_id": pid,
        "aggregate_version": "1",
        "payload": payload,
        "payload_hash": payload_digest(payload),
        # Journal sequence, not this local wall clock, is the causal authority.
        "committed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    try:
        JournalStore.append_event(store, envelope)
    except ValueError:
        # A concurrent identical declaration may have won. Re-read and require
        # exact durable semantics; any genuinely conflicting winner still fails.
        loaded = _read_plan(store, spec=spec, plan_id=pid)
        if (
            loaded.expected_events != events
            or loaded.max_journal_events != limit
            or loaded.store_identity_digest != store_digest
        ):
            raise RuntimeLoadPlanError(
                "runtime qualification plan identity was concurrently reused"
            )
        return loaded
    return _read_plan(store, spec=spec, plan_id=pid)


def load_declared_runtime_event_plan(
    store: JournalStore,
    *,
    plan_id: str,
    spec: RuntimeBudgetSpec,
) -> DeclaredRuntimeEventPlan:
    """Reload one immutable plan from canonical durable authority."""

    return _read_plan(store, spec=spec, plan_id=plan_id)


def evaluate_declared_runtime_budget(
    spec: RuntimeBudgetSpec,
    store: JournalStore,
    *,
    plan_id: str,
    financial_latency_us: Sequence[int],
    financial_staleness_us: Sequence[int],
    research_interference_us: Sequence[int],
    reconnect_backlog_remaining: int,
    financial_latency_event_ids: Sequence[str] = (),
    financial_staleness_event_ids: Sequence[str] = (),
    declared_duration_us: int | None = None,
    observed_duration_us: int | None = None,
) -> tuple[
    RuntimeBudgetDecision,
    JournalConservationEvidence,
    DeclaredRuntimeEventPlan,
]:
    """Evaluate event conservation from a pre-run durable plan and journal tail.

    This API intentionally accepts neither expected event bindings/counts nor a
    start cut. All are reloaded from the immutable declaration issued before
    campaign events, eliminating post-outcome caller selection of those facts.

    Metric series remain caller-supplied at this layer and stay anonymous unless
    explicit event IDs are supplied alongside them. Hard failures are useful
    diagnostics, but a favorable result cannot become qualification PASS until
    those series are recomputed from retained target-host measurement provenance.
    """

    spec = _validated_spec(spec)
    plan = _read_plan(store, spec=spec, plan_id=plan_id)
    decision, evidence = evaluate_journal_backed_runtime_budget(
        spec,
        store,
        start_journal_sequence=plan.declared_journal_sequence,
        expected_events=plan.expected_events,
        financial_latency_us=financial_latency_us,
        financial_staleness_us=financial_staleness_us,
        research_interference_us=research_interference_us,
        reconnect_backlog_remaining=reconnect_backlog_remaining,
        financial_latency_event_ids=financial_latency_event_ids,
        financial_staleness_event_ids=financial_staleness_event_ids,
        declared_duration_us=declared_duration_us,
        observed_duration_us=observed_duration_us,
        max_journal_events=plan.max_journal_events,
    )
    if decision.status == "PASS":
        decision = RuntimeBudgetDecision(
            status="INCONCLUSIVE",
            scenario_id=decision.scenario_id,
            reasons=("unverified_runtime_measurement_provenance",),
            metrics=decision.metrics,
        )
    return decision, evidence, plan
