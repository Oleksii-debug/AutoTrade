"""Dimensionally qualified WP-63 model-compute component evidence.

The durable model budget owns exact settled amounts.  The durable model-call
orchestrator independently owns the pricing/billing currency bound to that same
request identity.  This module composes those two existing owners at one frozen
JournalStore visibility cut; it does not create a second model-cost ledger.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256
import json
import re

from .ablation_model_compute_evidence import (
    ResolvedAblationModelComputeCostFact,
    reverify_ablation_model_compute_cost_fact,
)
from .exact_decimal import ExactDecimalError, canonical_decimal_text, parse_bounded_exact_decimal
from .model_budget_journal import DurableModelBudget
from .model_call import DurableModelCallOrchestrator
from .persistence import JournalStore, payload_digest


_BUDGET_EVENTS_RAW = DurableModelBudget.__dict__["_events"]
_CALL_EVENTS_RAW = DurableModelCallOrchestrator.__dict__["_events"]
_CALL_AGGREGATE_ID_RAW = DurableModelCallOrchestrator.__dict__["_aggregate_id"]
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_EVIDENCE_KIND = "CANONICAL_MODEL_COMPUTE_COMPONENT"
_COMPONENT = "model_compute"
_PREPARED_FIELDS = frozenset(
    {
        "attempt_id",
        "request_id",
        "request_identity_digest",
        "budget_id",
        "environment",
        "job_id",
        "input_digest",
        "policy_id",
        "pricing_evidence_id",
        "pricing_evidence_digest",
        "pricing_as_of",
        "pricing_valid_until",
        "cost_currency",
        "result_schema_id",
        "fallback_parent_attempt_id",
        "fallback_index",
        "provider_id",
        "model_id",
        "revision",
        "remote",
        "reserved_cost",
        "reservation_context_hash",
    }
)


def _text(value: object, *, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError(f"{name} must be exact canonical non-empty text")
    return value


def _digest(value: object, *, name: str) -> str:
    text = _text(value, name=name)
    if _SHA256.fullmatch(text) is None:
        raise ValueError(f"{name} must be an exact canonical sha256 digest")
    return text


def _positive(value: object, *, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be an exact positive integer")
    return value


def _amount(value: object, *, name: str) -> Decimal:
    try:
        amount = parse_bounded_exact_decimal(value)
    except (ExactDecimalError, TypeError, ValueError) as error:
        raise ValueError(f"{name} must be a bounded exact decimal") from error
    if not amount.is_finite() or amount < 0:
        raise ValueError(f"{name} must be a finite non-negative exact decimal")
    return amount


def _amount_text(value: Decimal) -> str:
    try:
        return canonical_decimal_text(value)
    except ExactDecimalError as error:
        raise ValueError("model-compute amount exceeds exact-decimal authority") from error


def _currency(value: object) -> str:
    text = _text(value, name="cost_currency")
    if text != text.upper() or not text.isalnum():
        raise ValueError("cost_currency must be canonical uppercase alphanumeric text")
    return text


def _assert_owners(
    budget: DurableModelBudget,
    calls: DurableModelCallOrchestrator,
) -> None:
    if type(budget) is not DurableModelBudget:
        raise TypeError("budget must be exact DurableModelBudget")
    if type(calls) is not DurableModelCallOrchestrator:
        raise TypeError("calls must be exact DurableModelCallOrchestrator")
    if DurableModelBudget.__dict__.get("_events") is not _BUDGET_EVENTS_RAW:
        raise ValueError("durable model-budget event reader changed after composition")
    if DurableModelCallOrchestrator.__dict__.get("_events") is not _CALL_EVENTS_RAW:
        raise ValueError("durable model-call event reader changed after composition")
    if (
        DurableModelCallOrchestrator.__dict__.get("_aggregate_id")
        is not _CALL_AGGREGATE_ID_RAW
    ):
        raise ValueError("durable model-call aggregate identity changed after composition")
    call_state = object.__getattribute__(calls, "__dict__")
    if type(call_state) is not dict:
        raise ValueError("durable model-call owner state is not canonical")
    if call_state.get("budget") is not budget:
        raise ValueError("model-call and model-budget authorities are not the same owner graph")
    journal = call_state.get("journal")
    if type(journal) is not JournalStore or journal is not budget.journal:
        raise ValueError("model-call and model-budget authorities do not share one JournalStore")
    shadowed = tuple(
        name for name in ("_events", "_aggregate_id") if name in call_state
    )
    if shadowed:
        raise ValueError(
            "durable model-call owner shadows canonical methods: "
            + ", ".join(shadowed)
        )


def _event_identity(event: dict[str, object]) -> tuple[str, str, str, int, int]:
    payload = event.get("payload")
    if type(payload) is not dict:
        raise ValueError("model-call durable payload is not canonical")
    expected_hash = payload_digest(payload)
    if event.get("payload_hash") != expected_hash:
        raise ValueError("model-call durable payload hash is invalid")
    return (
        _text(event.get("event_id"), name="event_id"),
        _digest(expected_hash, name="payload_hash"),
        _text(event.get("event_type"), name="event_type"),
        _positive(event.get("aggregate_version"), name="aggregate_version"),
        _positive(event.get("journal_sequence"), name="journal_sequence"),
    )


def _visible_call_events(
    calls: DurableModelCallOrchestrator,
    request_id: str,
    visibility: int,
) -> tuple[dict[str, object], ...]:
    events = tuple(
        event
        for event in _CALL_EVENTS_RAW(calls, request_id)
        if _positive(event.get("journal_sequence"), name="journal_sequence") <= visibility
    )
    if not events:
        raise ValueError("model-compute request lacks durable model-call authority")
    for expected_version, event in enumerate(events, start=1):
        if event.get("aggregate_version") != expected_version:
            raise ValueError("model-call visible aggregate prefix is not contiguous")
        _event_identity(event)
    first = events[0]
    if first.get("event_type") != "ModelCallPrepared":
        raise ValueError("model-compute request lacks canonical prepared call authority")
    return events


def _prepared_binding(
    budget: DurableModelBudget,
    calls: DurableModelCallOrchestrator,
    fact: ResolvedAblationModelComputeCostFact,
) -> tuple[str, str, tuple[tuple[str, str, str, int, int], ...]]:
    events = _visible_call_events(calls, fact.request_id, fact.visibility_journal_sequence)
    prepared = events[0]
    payload = prepared.get("payload")
    if type(payload) is not dict or set(payload) != _PREPARED_FIELDS:
        raise ValueError("durable prepared model-call payload shape is not canonical")
    if (
        payload.get("attempt_id") != fact.request_id
        or payload.get("request_id") != fact.request_id
        or payload.get("budget_id") != fact.budget_id
        or payload.get("environment") != fact.environment
    ):
        raise ValueError("prepared model-call scope differs from model-compute fact")
    currency = _currency(payload.get("cost_currency"))
    pricing_digest = _digest(
        payload.get("pricing_evidence_digest"),
        name="pricing_evidence_digest",
    )
    reserved = _amount(payload.get("reserved_cost"), name="reserved_cost")

    visible_budget = tuple(
        event
        for event in _BUDGET_EVENTS_RAW(budget)
        if _positive(event.get("journal_sequence"), name="journal_sequence")
        <= fact.visibility_journal_sequence
    )
    route_events = []
    for event in visible_budget:
        payload_value = event.get("payload")
        if (
            event.get("event_type") == "ModelRouteReserved"
            and type(payload_value) is dict
            and payload_value.get("request_id") == fact.request_id
        ):
            route_events.append(event)
    if len(route_events) != 1:
        raise ValueError("model-compute request lacks unique durable route reservation")
    route_payload = route_events[0]["payload"]
    if _amount(route_payload.get("amount"), name="route reserved amount") != reserved:
        raise ValueError("prepared model-call cost differs from durable route reservation")
    routing_input = route_payload.get("routing_input")
    if type(routing_input) is not dict:
        raise ValueError("durable route input is not canonical")
    context = routing_input.get("reservation_context")
    if type(context) is not dict:
        raise ValueError("model route lacks canonical reservation cost context")
    if payload_digest(context) != payload.get("reservation_context_hash"):
        raise ValueError("prepared model-call reservation context hash mismatch")
    if context.get("cost_currency") != currency:
        raise ValueError("route reservation currency differs from prepared model call")
    if context.get("pricing_evidence_digest") != pricing_digest:
        raise ValueError("route pricing evidence differs from prepared model call")

    # Later observed/billing evidence must remain in the prepared currency.
    for event in events[1:]:
        event_type = event.get("event_type")
        event_payload = event.get("payload")
        if type(event_payload) is not dict:
            raise ValueError("model-call durable payload is not canonical")
        if event_type in {"ModelCallObserved", "ModelBillingEvidenceObserved"}:
            if event_payload.get("cost_currency") != currency:
                raise ValueError("model-call economic evidence changed cost currency")
        if event_type == "ModelBillingEvidenceObserved":
            _digest(event_payload.get("evidence_digest"), name="billing evidence digest")
            _amount(event_payload.get("billed"), name="billed")

    identities = tuple(_event_identity(event) for event in events)
    return currency, pricing_digest, identities


def _material(
    *,
    compute_fact_digest: str,
    request_id: str,
    amount: Decimal,
    value_unit: str,
    pricing_evidence_digest: str,
    visibility_journal_sequence: int,
    model_call_event_identities: tuple[tuple[str, str, str, int, int], ...],
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "evidence_kind": _EVIDENCE_KIND,
        "component_name": _COMPONENT,
        "terminal_cost_component": True,
        "dimensionally_qualified": True,
        "compute_fact_digest": compute_fact_digest,
        "request_id": request_id,
        "amount": _amount_text(amount),
        "value_unit": value_unit,
        "pricing_evidence_digest": pricing_evidence_digest,
        "visibility_journal_sequence": visibility_journal_sequence,
        "model_call_event_identities": [
            list(item) for item in model_call_event_identities
        ],
    }


def _hash(material: dict[str, object]) -> str:
    raw = json.dumps(
        material,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + sha256(raw).hexdigest()


@dataclass(frozen=True)
class ResolvedAblationModelComputeComponentEvidence:
    evidence_kind: str
    component_name: str
    terminal_cost_component: bool
    dimensionally_qualified: bool
    compute_fact_digest: str
    request_id: str
    amount: Decimal
    value_unit: str
    pricing_evidence_digest: str
    visibility_journal_sequence: int
    model_call_event_identities: tuple[tuple[str, str, str, int, int], ...]
    evidence_digest: str

    def __post_init__(self) -> None:
        if self.evidence_kind != _EVIDENCE_KIND or self.component_name != _COMPONENT:
            raise ValueError("model-compute component identity is not canonical")
        if type(self.terminal_cost_component) is not bool or not self.terminal_cost_component:
            raise ValueError("model-compute component must be terminal for its own component")
        if type(self.dimensionally_qualified) is not bool or not self.dimensionally_qualified:
            raise ValueError("model-compute component must be dimensionally qualified")
        _digest(self.compute_fact_digest, name="compute_fact_digest")
        _text(self.request_id, name="request_id")
        _amount_text(self.amount)
        _currency(self.value_unit)
        _digest(self.pricing_evidence_digest, name="pricing_evidence_digest")
        _positive(self.visibility_journal_sequence, name="visibility_journal_sequence")
        if type(self.model_call_event_identities) is not tuple or not self.model_call_event_identities:
            raise ValueError("model-call event identities must be a non-empty exact tuple")
        for item in self.model_call_event_identities:
            if type(item) is not tuple or len(item) != 5:
                raise ValueError("model-call event identity is not canonical")
            _text(item[0], name="event_id")
            _digest(item[1], name="payload_hash")
            _text(item[2], name="event_type")
            _positive(item[3], name="aggregate_version")
            _positive(item[4], name="journal_sequence")
        _digest(self.evidence_digest, name="evidence_digest")
        ResolvedAblationModelComputeComponentEvidence.verify_integrity(self)

    def verify_integrity(self) -> None:
        material = _material(
            compute_fact_digest=self.compute_fact_digest,
            request_id=self.request_id,
            amount=self.amount,
            value_unit=self.value_unit,
            pricing_evidence_digest=self.pricing_evidence_digest,
            visibility_journal_sequence=self.visibility_journal_sequence,
            model_call_event_identities=self.model_call_event_identities,
        )
        if self.evidence_digest != _hash(material):
            raise ValueError(
                "model-compute component digest does not match canonical material"
            )


def resolve_ablation_model_compute_component_evidence(
    budget: DurableModelBudget,
    calls: DurableModelCallOrchestrator,
    fact: ResolvedAblationModelComputeCostFact,
) -> ResolvedAblationModelComputeComponentEvidence:
    """Upgrade a settled budget fact only when canonical call currency is owned."""

    _assert_owners(budget, calls)
    if type(fact) is not ResolvedAblationModelComputeCostFact:
        raise TypeError("fact must be exact ResolvedAblationModelComputeCostFact")
    verified = reverify_ablation_model_compute_cost_fact(budget, fact)
    currency, pricing_digest, identities = _prepared_binding(budget, calls, verified)
    material = _material(
        compute_fact_digest=verified.evidence_digest,
        request_id=verified.request_id,
        amount=verified.amount,
        value_unit=currency,
        pricing_evidence_digest=pricing_digest,
        visibility_journal_sequence=verified.visibility_journal_sequence,
        model_call_event_identities=identities,
    )
    return ResolvedAblationModelComputeComponentEvidence(
        evidence_kind=_EVIDENCE_KIND,
        component_name=_COMPONENT,
        terminal_cost_component=True,
        dimensionally_qualified=True,
        compute_fact_digest=verified.evidence_digest,
        request_id=verified.request_id,
        amount=verified.amount,
        value_unit=currency,
        pricing_evidence_digest=pricing_digest,
        visibility_journal_sequence=verified.visibility_journal_sequence,
        model_call_event_identities=identities,
        evidence_digest=_hash(material),
    )


def reverify_ablation_model_compute_component_evidence(
    budget: DurableModelBudget,
    calls: DurableModelCallOrchestrator,
    fact: ResolvedAblationModelComputeCostFact,
    evidence: ResolvedAblationModelComputeComponentEvidence,
) -> ResolvedAblationModelComputeComponentEvidence:
    if type(evidence) is not ResolvedAblationModelComputeComponentEvidence:
        raise TypeError(
            "evidence must be exact ResolvedAblationModelComputeComponentEvidence"
        )
    ResolvedAblationModelComputeComponentEvidence.verify_integrity(evidence)
    resolved = resolve_ablation_model_compute_component_evidence(budget, calls, fact)
    if resolved != evidence:
        raise ValueError(
            "model-compute component does not match canonical owner evidence"
        )
    return resolved
