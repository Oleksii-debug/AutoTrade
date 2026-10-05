"""Frozen financing-attribution facts for WP-63 cost composition.

DurableFinancingBook is an accounting owner, not a preregistered ablation cost
classifier.  Its durable charge stream can represent economically distinct
concepts (for example account-scoped borrow liabilities and provider funding
charges).  This module therefore binds the canonical scope/source-account facts
for one already-resolved financing charge while deliberately refusing to relabel
that charge as the registered ``financing``, ``funding`` or ``borrow`` cost
component.

A terminal cost composer must combine these facts with an independently
preregistered attribution rule.  Until that rule exists the evidence remains a
non-numeric, fail-closed classification prerequisite and grants no terminal cost,
scientific PASS, trading or release authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import re

import mvp.autotrade_mvp.ablation_financing_component_evidence as _component_evidence
from .ablation_financing_component_evidence import (
    ResolvedAblationFinancingComponentEvidence,
)
from .durable_financing import DurableFinancingBook
from .financing import FinancingConflict


_FINANCING_EVENTS_RAW = DurableFinancingBook.__dict__["_events"]
_REVERIFY_COMPONENT = _component_evidence.reverify_ablation_financing_component_evidence
_REVERIFY_COMPONENT_RAW = _component_evidence.__dict__[
    "reverify_ablation_financing_component_evidence"
]
_EVIDENCE_KIND = "CANONICAL_FINANCING_ATTRIBUTION_FACTS"
_ATTRIBUTION_STATUS = "UNATTRIBUTED"
_ATTRIBUTION_BLOCKER = "registered_financing_cost_component_attribution_unavailable"
_ALLOWED_SCOPE_TYPES = {"ACCOUNT", "INSTRUMENT"}
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")


def _text(value: object, *, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise FinancingConflict(f"{name} must be exact canonical non-empty text")
    return value


def _digest(value: object, *, name: str) -> str:
    value = _text(value, name=name)
    if _SHA256.fullmatch(value) is None:
        raise FinancingConflict(f"{name} must be an exact canonical sha256 digest")
    return value


def _positive(value: object, *, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise FinancingConflict(f"{name} must be an exact positive integer")
    return value


def _material(
    *,
    financing_evidence_digest: str,
    charge_id: str,
    aggregate_version: int,
    visibility_journal_sequence: int,
    source_account: str,
    charge_scope_type: str,
    charge_scope_id: str,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "evidence_kind": _EVIDENCE_KIND,
        "terminal_cost_component": False,
        "registered_cost_component": None,
        "attribution_status": _ATTRIBUTION_STATUS,
        "attribution_blocker": _ATTRIBUTION_BLOCKER,
        "financing_evidence_digest": financing_evidence_digest,
        "charge_id": charge_id,
        "aggregate_version": aggregate_version,
        "visibility_journal_sequence": visibility_journal_sequence,
        "source_account": source_account,
        "charge_scope_type": charge_scope_type,
        "charge_scope_id": charge_scope_id,
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


def _assert_owner(owner: DurableFinancingBook) -> None:
    if type(owner) is not DurableFinancingBook:
        raise TypeError("owner must be exact DurableFinancingBook")
    if DurableFinancingBook.__dict__.get("_events") is not _FINANCING_EVENTS_RAW:
        raise FinancingConflict(
            "durable financing event reader changed after attribution composition"
        )
    if (
        _component_evidence.__dict__.get(
            "reverify_ablation_financing_component_evidence"
        )
        is not _REVERIFY_COMPONENT_RAW
    ):
        raise FinancingConflict(
            "financing component reverification executable changed after attribution composition"
        )
    state = object.__getattribute__(owner, "__dict__")
    if type(state) is not dict:
        raise FinancingConflict("durable financing owner state is not canonical")
    if "_events" in state:
        raise FinancingConflict("durable financing owner shadows canonical event reader")


@dataclass(frozen=True)
class ResolvedAblationFinancingAttributionEvidence:
    """Canonical durable charge scope facts without a cost-component claim."""

    evidence_kind: str
    terminal_cost_component: bool
    registered_cost_component: None
    attribution_status: str
    attribution_blocker: str
    financing_evidence_digest: str
    charge_id: str
    aggregate_version: int
    visibility_journal_sequence: int
    source_account: str
    charge_scope_type: str
    charge_scope_id: str
    evidence_digest: str

    def __post_init__(self) -> None:
        if self.evidence_kind != _EVIDENCE_KIND:
            raise FinancingConflict("financing attribution evidence kind is not canonical")
        if type(self.terminal_cost_component) is not bool or self.terminal_cost_component:
            raise FinancingConflict(
                "unattributed financing facts cannot claim terminal cost-component authority"
            )
        if self.registered_cost_component is not None:
            raise FinancingConflict(
                "unattributed financing facts cannot name a registered cost component"
            )
        if self.attribution_status != _ATTRIBUTION_STATUS:
            raise FinancingConflict("financing attribution status is not canonical")
        if self.attribution_blocker != _ATTRIBUTION_BLOCKER:
            raise FinancingConflict("financing attribution blocker is not canonical")
        _digest(self.financing_evidence_digest, name="financing_evidence_digest")
        _text(self.charge_id, name="charge_id")
        _positive(self.aggregate_version, name="aggregate_version")
        _positive(
            self.visibility_journal_sequence,
            name="visibility_journal_sequence",
        )
        _text(self.source_account, name="source_account")
        scope_type = _text(self.charge_scope_type, name="charge_scope_type")
        if scope_type not in _ALLOWED_SCOPE_TYPES:
            raise FinancingConflict("financing charge scope type is not canonical")
        _text(self.charge_scope_id, name="charge_scope_id")
        _digest(self.evidence_digest, name="evidence_digest")
        ResolvedAblationFinancingAttributionEvidence.verify_integrity(self)

    def verify_integrity(self) -> None:
        material = _material(
            financing_evidence_digest=self.financing_evidence_digest,
            charge_id=self.charge_id,
            aggregate_version=self.aggregate_version,
            visibility_journal_sequence=self.visibility_journal_sequence,
            source_account=self.source_account,
            charge_scope_type=self.charge_scope_type,
            charge_scope_id=self.charge_scope_id,
        )
        if self.evidence_digest != _hash(material):
            raise FinancingConflict(
                "financing attribution evidence digest does not match canonical material"
            )


def resolve_ablation_financing_attribution_evidence(
    owner: DurableFinancingBook,
    financing_evidence: ResolvedAblationFinancingComponentEvidence,
) -> ResolvedAblationFinancingAttributionEvidence:
    """Bind durable scope facts while refusing unregistered component attribution."""

    _assert_owner(owner)
    if type(financing_evidence) is not ResolvedAblationFinancingComponentEvidence:
        raise TypeError(
            "financing_evidence must be exact ResolvedAblationFinancingComponentEvidence"
        )
    ResolvedAblationFinancingComponentEvidence.verify_integrity(financing_evidence)
    verified = _REVERIFY_COMPONENT(owner, financing_evidence)
    if verified != financing_evidence:
        raise FinancingConflict(
            "financing attribution input changed during canonical reverification"
        )

    events = _FINANCING_EVENTS_RAW(owner, financing_evidence.charge_id)
    if type(events) is not list:
        raise FinancingConflict("durable financing event reader returned noncanonical history")
    visible: list[dict[str, object]] = []
    for event in events:
        if type(event) is not dict:
            raise FinancingConflict("durable financing history contains noncanonical event")
        sequence = _positive(event.get("journal_sequence"), name="journal_sequence")
        if sequence <= financing_evidence.visibility_journal_sequence:
            visible.append(event)
    if len(visible) != financing_evidence.aggregate_version:
        raise FinancingConflict(
            "financing attribution history does not match frozen aggregate version"
        )
    if not visible:
        raise FinancingConflict("financing attribution history is empty")

    canonical_scope: tuple[str, str, str] | None = None
    for durable in visible:
        payload = durable.get("payload")
        if type(payload) is not dict:
            raise FinancingConflict("financing durable payload is not canonical")
        if payload.get("charge_id") != financing_evidence.charge_id:
            raise FinancingConflict("financing durable charge identity changed")
        if payload.get("unit") != financing_evidence.unit:
            raise FinancingConflict("financing durable charge unit changed")
        scope_type = _text(payload.get("charge_scope_type"), name="charge_scope_type")
        if scope_type not in _ALLOWED_SCOPE_TYPES:
            raise FinancingConflict("financing charge scope type is not canonical")
        scope = (
            _text(payload.get("source_account"), name="source_account"),
            scope_type,
            _text(payload.get("charge_scope_id"), name="charge_scope_id"),
        )
        if canonical_scope is None:
            canonical_scope = scope
        elif scope != canonical_scope:
            raise FinancingConflict(
                "financing revision history changed source-account or charge scope"
            )

    terminal = visible[-1]
    if terminal.get("event_id") != financing_evidence.event_id:
        raise FinancingConflict("financing attribution terminal event identity mismatch")
    if terminal.get("payload_hash") != financing_evidence.payload_hash:
        raise FinancingConflict("financing attribution terminal payload digest mismatch")
    if canonical_scope is None:
        raise FinancingConflict("financing attribution scope could not be resolved")
    source_account, charge_scope_type, charge_scope_id = canonical_scope
    material = _material(
        financing_evidence_digest=financing_evidence.evidence_digest,
        charge_id=financing_evidence.charge_id,
        aggregate_version=financing_evidence.aggregate_version,
        visibility_journal_sequence=financing_evidence.visibility_journal_sequence,
        source_account=source_account,
        charge_scope_type=charge_scope_type,
        charge_scope_id=charge_scope_id,
    )
    return ResolvedAblationFinancingAttributionEvidence(
        evidence_kind=_EVIDENCE_KIND,
        terminal_cost_component=False,
        registered_cost_component=None,
        attribution_status=_ATTRIBUTION_STATUS,
        attribution_blocker=_ATTRIBUTION_BLOCKER,
        financing_evidence_digest=financing_evidence.evidence_digest,
        charge_id=financing_evidence.charge_id,
        aggregate_version=financing_evidence.aggregate_version,
        visibility_journal_sequence=financing_evidence.visibility_journal_sequence,
        source_account=source_account,
        charge_scope_type=charge_scope_type,
        charge_scope_id=charge_scope_id,
        evidence_digest=_hash(material),
    )


def reverify_ablation_financing_attribution_evidence(
    owner: DurableFinancingBook,
    financing_evidence: ResolvedAblationFinancingComponentEvidence,
    evidence: ResolvedAblationFinancingAttributionEvidence,
) -> ResolvedAblationFinancingAttributionEvidence:
    if type(evidence) is not ResolvedAblationFinancingAttributionEvidence:
        raise TypeError(
            "evidence must be exact ResolvedAblationFinancingAttributionEvidence"
        )
    ResolvedAblationFinancingAttributionEvidence.verify_integrity(evidence)
    resolved = resolve_ablation_financing_attribution_evidence(
        owner,
        financing_evidence,
    )
    if resolved != evidence:
        raise FinancingConflict(
            "financing attribution evidence does not match canonical durable replay"
        )
    return resolved
