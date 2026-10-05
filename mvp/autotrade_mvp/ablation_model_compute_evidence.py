"""Frozen model-compute cost facts for WP-63.

DurableModelBudget owns exact model-cost amounts, but its current schema does not
own a currency/value unit.  This resolver therefore preserves the exact amount and
frozen journal lineage while explicitly refusing dimensional qualification.  A
terminal ablation cost composite must not treat this fact as USD (or any other
registered value unit) until a canonical unit authority is added.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256
import json
import re

from .model_budget_journal import DurableModelBudget
from .model_gateway import BudgetLedger
from .persistence import (
    JournalStore,
    JournalStoreIdentity,
    payload_digest,
    require_exact_journal_store_identity,
)


_EVENTS_RAW = DurableModelBudget.__dict__["_events"]
_REPLAY_RAW = DurableModelBudget.__dict__["_replay_events"]
_APPLY_RAW = DurableModelBudget.__dict__["_apply"].__func__
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_EVIDENCE_KIND = "MODEL_COMPUTE_COST_FACT"
_BLOCKER = "canonical_model_compute_value_unit_unavailable"


def _text(value: object, *, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError(f"{name} must be exact canonical non-empty text")
    return value


def _digest(value: object, *, name: str) -> str:
    value = _text(value, name=name)
    if _SHA256.fullmatch(value) is None:
        raise ValueError(f"{name} must be an exact canonical sha256 digest")
    return value


def _positive(value: object, *, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be an exact positive integer")
    return value


def _store_material(identity: JournalStoreIdentity) -> dict[str, object]:
    require_exact_journal_store_identity(
        identity,
        subject="ablation model-compute store identity",
    )
    return {
        "canonical_path": identity.canonical_path,
        "filesystem_device": identity.filesystem_device,
        "filesystem_inode": identity.filesystem_inode,
        "identity_source": identity.identity_source,
        "windows_volume_serial": identity.windows_volume_serial,
        "windows_file_index_high": identity.windows_file_index_high,
        "windows_file_index_low": identity.windows_file_index_low,
    }


def _assert_owner(owner: DurableModelBudget) -> dict[str, object]:
    if type(owner) is not DurableModelBudget:
        raise TypeError("owner must be exact DurableModelBudget")
    if DurableModelBudget.__dict__.get("_events") is not _EVENTS_RAW:
        raise ValueError("durable model-budget event reader changed after composition")
    if DurableModelBudget.__dict__.get("_replay_events") is not _REPLAY_RAW:
        raise ValueError("durable model-budget replay changed after composition")
    apply_descriptor = DurableModelBudget.__dict__.get("_apply")
    if type(apply_descriptor) is not staticmethod or apply_descriptor.__func__ is not _APPLY_RAW:
        raise ValueError("durable model-budget apply executable changed after composition")
    state = object.__getattribute__(owner, "__dict__")
    if type(state) is not dict:
        raise ValueError("durable model-budget owner state is not canonical")
    shadowed = tuple(
        sorted(
            name
            for name in state
            if name in DurableModelBudget.__dict__
            and callable(getattr(DurableModelBudget, name, None))
        )
    )
    if shadowed:
        raise ValueError(
            "durable model-budget owner shadows canonical methods: " + ", ".join(shadowed)
        )
    if type(state.get("journal")) is not JournalStore:
        raise ValueError("durable model-budget JournalStore is not canonical")
    _text(state.get("budget_id"), name="budget_id")
    _text(state.get("environment"), name="environment")
    if type(state.get("_ceiling")) is not Decimal:
        raise ValueError("durable model-budget ceiling is not exact Decimal")
    return state


def _visible(events: list[dict[str, object]], visibility: int) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for event in events:
        sequence = _positive(event.get("journal_sequence"), name="journal_sequence")
        if sequence <= visibility:
            result.append(event)
    return result


def _event_identity(event: dict[str, object]) -> tuple[str, str, str, int]:
    payload = event.get("payload")
    if type(payload) is not dict:
        raise ValueError("model-compute durable payload is not canonical")
    expected_hash = payload_digest(payload)
    if event.get("payload_hash") != expected_hash:
        raise ValueError("model-compute durable payload hash is invalid")
    return (
        _text(event.get("event_id"), name="event_id"),
        _digest(expected_hash, name="payload_hash"),
        _text(event.get("event_type"), name="event_type"),
        _positive(event.get("journal_sequence"), name="journal_sequence"),
    )


def _material(
    *,
    store_identity: JournalStoreIdentity,
    budget_id: str,
    environment: str,
    request_id: str,
    aggregate_version: int,
    visibility_journal_sequence: int,
    terminal_journal_sequence: int,
    amount: str,
    event_identities: tuple[tuple[str, str, str, int], ...],
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "evidence_kind": _EVIDENCE_KIND,
        "terminal_cost_component": False,
        "dimensionally_qualified": False,
        "blocking_reason": _BLOCKER,
        "value_unit": None,
        "store_identity": _store_material(store_identity),
        "budget_id": budget_id,
        "environment": environment,
        "request_id": request_id,
        "aggregate_version": aggregate_version,
        "visibility_journal_sequence": visibility_journal_sequence,
        "terminal_journal_sequence": terminal_journal_sequence,
        "amount": amount,
        "event_identities": [list(item) for item in event_identities],
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
class ResolvedAblationModelComputeCostFact:
    evidence_kind: str
    terminal_cost_component: bool
    dimensionally_qualified: bool
    blocking_reason: str
    value_unit: None
    store_identity: JournalStoreIdentity
    budget_id: str
    environment: str
    request_id: str
    aggregate_version: int
    visibility_journal_sequence: int
    terminal_journal_sequence: int
    amount: Decimal
    event_identities: tuple[tuple[str, str, str, int], ...]
    evidence_digest: str

    def __post_init__(self) -> None:
        if self.evidence_kind != _EVIDENCE_KIND:
            raise ValueError("model-compute evidence kind is not canonical")
        if type(self.terminal_cost_component) is not bool or self.terminal_cost_component:
            raise ValueError("model-compute fact cannot claim terminal cost-component authority")
        if type(self.dimensionally_qualified) is not bool or self.dimensionally_qualified:
            raise ValueError("model-compute fact cannot claim a value unit")
        if self.blocking_reason != _BLOCKER or self.value_unit is not None:
            raise ValueError("model-compute dimensional blocker is not canonical")
        _store_material(self.store_identity)
        for name in ("budget_id", "environment", "request_id"):
            _text(getattr(self, name), name=name)
        for name in (
            "aggregate_version", "visibility_journal_sequence", "terminal_journal_sequence"
        ):
            _positive(getattr(self, name), name=name)
        if self.terminal_journal_sequence > self.visibility_journal_sequence:
            raise ValueError("model-compute terminal event follows visibility cut")
        if type(self.amount) is not Decimal or not self.amount.is_finite() or self.amount < 0:
            raise ValueError("model-compute amount must be exact non-negative Decimal")
        if type(self.event_identities) is not tuple or not self.event_identities:
            raise ValueError("model-compute event identities must be a non-empty tuple")
        for item in self.event_identities:
            if type(item) is not tuple or len(item) != 4:
                raise ValueError("model-compute event identity is not canonical")
            _text(item[0], name="event_id")
            _digest(item[1], name="payload_hash")
            _text(item[2], name="event_type")
            _positive(item[3], name="journal_sequence")
        _digest(self.evidence_digest, name="evidence_digest")
        ResolvedAblationModelComputeCostFact.verify_integrity(self)

    def verify_integrity(self) -> None:
        material = _material(
            store_identity=self.store_identity,
            budget_id=self.budget_id,
            environment=self.environment,
            request_id=self.request_id,
            aggregate_version=self.aggregate_version,
            visibility_journal_sequence=self.visibility_journal_sequence,
            terminal_journal_sequence=self.terminal_journal_sequence,
            amount=str(self.amount),
            event_identities=self.event_identities,
        )
        if self.evidence_digest != _hash(material):
            raise ValueError(
                "model-compute evidence digest does not match canonical material"
            )


def resolve_ablation_model_compute_cost_fact(
    owner: DurableModelBudget,
    *,
    request_id: str,
    expected_aggregate_version: int,
    visibility_journal_sequence: int | None = None,
) -> ResolvedAblationModelComputeCostFact:
    """Resolve one settled request at one frozen budget/journal cut."""

    state = _assert_owner(owner)
    request = _text(request_id, name="request_id")
    expected_version = _positive(
        expected_aggregate_version,
        name="expected_aggregate_version",
    )
    journal = state["journal"]
    identity = require_exact_journal_store_identity(
        journal.store_identity,
        subject="ablation model-compute JournalStore",
    )
    current = JournalStore.current_journal_sequence(journal)
    visibility = (
        current
        if visibility_journal_sequence is None
        else _positive(visibility_journal_sequence, name="visibility_journal_sequence")
    )
    if visibility > current:
        raise ValueError("model-compute visibility cut is beyond durable journal")

    visible = _visible(_EVENTS_RAW(owner), visibility)
    if not visible:
        raise ValueError("model budget has no state at visibility cut")
    if visible[-1].get("aggregate_version") != expected_version:
        raise ValueError("requested model-budget version is stale at visibility cut")
    if len(visible) != expected_version:
        raise ValueError("model-budget visible aggregate prefix is not contiguous")
    # Validate the full budget prefix with the canonical owner before selecting a request.
    _REPLAY_RAW(owner, visible)

    initialization = visible[0].get("payload")
    if type(initialization) is not dict:
        raise ValueError("model-budget initialization payload is invalid")
    request_ledger = BudgetLedger(initialization.get("ceiling"))
    relevant: list[dict[str, object]] = []
    settled = False
    for event in visible[1:]:
        payload = event.get("payload")
        if type(payload) is not dict or payload.get("request_id") != request:
            continue
        event_type = event.get("event_type")
        if event_type not in {
            "ModelCostReserved",
            "ModelRouteReserved",
            "ModelCostReleased",
            "ModelCostSettled",
            "ModelUnbilledReconciled",
        }:
            raise ValueError("unsupported model-compute request event")
        _event_identity(event)
        _APPLY_RAW(request_ledger, event)
        relevant.append(event)
        if event_type == "ModelCostSettled":
            settled = True
    if not settled:
        raise ValueError("model-compute request lacks durable settlement")
    snapshot = request_ledger.snapshot()
    if snapshot.reserved != 0:
        raise ValueError("model-compute request still has active reservation")
    if snapshot.estimated_unbilled != 0:
        raise ValueError("model-compute request has unresolved estimated unbilled cost")
    identities = tuple(_event_identity(event) for event in relevant)
    terminal_sequence = identities[-1][3]
    material = _material(
        store_identity=identity,
        budget_id=state["budget_id"],
        environment=state["environment"],
        request_id=request,
        aggregate_version=expected_version,
        visibility_journal_sequence=visibility,
        terminal_journal_sequence=terminal_sequence,
        amount=str(snapshot.incurred),
        event_identities=identities,
    )
    return ResolvedAblationModelComputeCostFact(
        evidence_kind=_EVIDENCE_KIND,
        terminal_cost_component=False,
        dimensionally_qualified=False,
        blocking_reason=_BLOCKER,
        value_unit=None,
        store_identity=identity,
        budget_id=state["budget_id"],
        environment=state["environment"],
        request_id=request,
        aggregate_version=expected_version,
        visibility_journal_sequence=visibility,
        terminal_journal_sequence=terminal_sequence,
        amount=snapshot.incurred,
        event_identities=identities,
        evidence_digest=_hash(material),
    )


def reverify_ablation_model_compute_cost_fact(
    owner: DurableModelBudget,
    evidence: ResolvedAblationModelComputeCostFact,
) -> ResolvedAblationModelComputeCostFact:
    if type(evidence) is not ResolvedAblationModelComputeCostFact:
        raise TypeError("evidence must be exact ResolvedAblationModelComputeCostFact")
    ResolvedAblationModelComputeCostFact.verify_integrity(evidence)
    resolved = resolve_ablation_model_compute_cost_fact(
        owner,
        request_id=evidence.request_id,
        expected_aggregate_version=evidence.aggregate_version,
        visibility_journal_sequence=evidence.visibility_journal_sequence,
    )
    if resolved != evidence:
        raise ValueError("model-compute evidence does not match canonical durable replay")
    return resolved
