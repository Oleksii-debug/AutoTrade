"""Canonical durable aggregate taxonomy for qualification and writer identity.

This module is persistence/domain authority.  Performance qualification consumes
it; performance code must not maintain a second financial-family allowlist.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from types import MappingProxyType
from typing import Mapping


TAXONOMY_VERSION = "1.0.0"
_FINANCIAL = "FINANCIAL"
_CONTROL = "CONTROL"
_SIMULATION = "SIMULATION"
_ALLOWED_VISIBILITY = frozenset({_FINANCIAL, _CONTROL, _SIMULATION})


@dataclass(frozen=True, slots=True)
class DurableAggregateDescriptor:
    aggregate_type: str
    domain_class: str
    qualification_visibility: str
    schema_version: str = "1.0.0"

    def __post_init__(self) -> None:
        for name, value in (
            ("aggregate_type", self.aggregate_type),
            ("domain_class", self.domain_class),
            ("qualification_visibility", self.qualification_visibility),
            ("schema_version", self.schema_version),
        ):
            if type(value) is not str or not value or value != value.strip():
                raise ValueError(f"{name} must be a canonical exact string")
        if self.qualification_visibility not in _ALLOWED_VISIBILITY:
            raise ValueError("unsupported qualification_visibility")

    @property
    def financial_qualification_relevant(self) -> bool:
        return self.qualification_visibility == _FINANCIAL

    def canonical_record(self) -> dict[str, str]:
        return {
            "aggregate_type": self.aggregate_type,
            "domain_class": self.domain_class,
            "qualification_visibility": self.qualification_visibility,
            "schema_version": self.schema_version,
        }


AUTHORITY_STATE = DurableAggregateDescriptor("authority_state", "AUTHORITY", _FINANCIAL)
RISK_DECISION = DurableAggregateDescriptor("risk_decision", "RISK", _FINANCIAL)
FINANCIAL_AUTHORITY = DurableAggregateDescriptor(
    "financial-authority", "AUTHORITY", _FINANCIAL
)
RISK_POLICY_REGISTRY = DurableAggregateDescriptor(
    "risk_policy_registry", "RISK", _FINANCIAL
)
SUBMISSION_ATTEMPT = DurableAggregateDescriptor(
    "submission_attempt", "EXECUTION", _FINANCIAL
)
CAPABILITY_HISTORY = DurableAggregateDescriptor(
    "capability_history", "AUTHORITY", _FINANCIAL
)
ORDER_PROJECTION_BOOK = DurableAggregateDescriptor(
    "order_projection_book", "EXECUTION", _FINANCIAL
)
RESERVATION_BOOK = DurableAggregateDescriptor(
    "reservation_book", "RISK", _FINANCIAL
)
SETTLEMENT_BOOK = DurableAggregateDescriptor(
    "settlement_book", "SETTLEMENT", _FINANCIAL
)
FUTURES_VARIATION_MARGIN = DurableAggregateDescriptor(
    "FUTURES_VARIATION_MARGIN", "DERIVATIVES", _FINANCIAL
)
PROVIDER_FILL_FINANCIAL_BINDING = DurableAggregateDescriptor(
    "provider_fill_financial_binding", "ACCOUNTING", _FINANCIAL
)
PROVIDER_FILL_RESERVATION_CORRECTION_BINDING = DurableAggregateDescriptor(
    "provider_fill_reservation_correction_binding", "ACCOUNTING", _FINANCIAL
)
ECONOMIC_BOOK = DurableAggregateDescriptor("economic_book", "ACCOUNTING", _FINANCIAL)
PROVIDER_ACTIVITY = DurableAggregateDescriptor(
    "provider_activity", "PROVIDER", _FINANCIAL
)
ACCOUNT_RECONCILIATION = DurableAggregateDescriptor(
    "account_reconciliation", "RECONCILIATION", _FINANCIAL
)
CORPORATE_ACTION_EVIDENCE = DurableAggregateDescriptor(
    "corporate_action_evidence", "CORPORATE_ACTION", _FINANCIAL
)
OPTION_LIFECYCLE = DurableAggregateDescriptor(
    "option_lifecycle", "DERIVATIVES", _FINANCIAL
)
SECURITIES_BORROW_RECALL = DurableAggregateDescriptor(
    "securities_borrow_recall", "BORROW", _FINANCIAL
)
RECOVERY_OWNER = DurableAggregateDescriptor("recovery_owner", "RECOVERY", _FINANCIAL)
PROVIDER_NONCE = DurableAggregateDescriptor("provider_nonce", "PROVIDER", _FINANCIAL)
MODEL_BUDGET = DurableAggregateDescriptor("model_budget", "LEARNING", _CONTROL)
HOST_CONTROL = DurableAggregateDescriptor("HOST_CONTROL", "HOST", _CONTROL)
CANONICAL_SIMULATION_SESSION = DurableAggregateDescriptor(
    "canonical_simulation_session", "SIMULATION", _SIMULATION
)

_ALL = (
    AUTHORITY_STATE,
    RISK_DECISION,
    FINANCIAL_AUTHORITY,
    RISK_POLICY_REGISTRY,
    SUBMISSION_ATTEMPT,
    CAPABILITY_HISTORY,
    ORDER_PROJECTION_BOOK,
    RESERVATION_BOOK,
    SETTLEMENT_BOOK,
    FUTURES_VARIATION_MARGIN,
    PROVIDER_FILL_FINANCIAL_BINDING,
    PROVIDER_FILL_RESERVATION_CORRECTION_BINDING,
    ECONOMIC_BOOK,
    PROVIDER_ACTIVITY,
    ACCOUNT_RECONCILIATION,
    CORPORATE_ACTION_EVIDENCE,
    OPTION_LIFECYCLE,
    SECURITIES_BORROW_RECALL,
    RECOVERY_OWNER,
    PROVIDER_NONCE,
    MODEL_BUDGET,
    HOST_CONTROL,
    CANONICAL_SIMULATION_SESSION,
)

if len({item.aggregate_type for item in _ALL}) != len(_ALL):
    raise RuntimeError("durable aggregate taxonomy contains duplicate aggregate types")

_BY_TYPE: Mapping[str, DurableAggregateDescriptor] = MappingProxyType(
    {item.aggregate_type: item for item in _ALL}
)


def descriptor_for_aggregate_type(aggregate_type: str) -> DurableAggregateDescriptor:
    if type(aggregate_type) is not str or not aggregate_type or aggregate_type != aggregate_type.strip():
        raise ValueError("aggregate_type must be a canonical exact string")
    try:
        return _BY_TYPE[aggregate_type]
    except KeyError as error:
        raise ValueError(
            f"unclassified durable aggregate type: {aggregate_type}"
        ) from error


def classify_durable_event(event: Mapping[str, object]) -> DurableAggregateDescriptor:
    if not isinstance(event, Mapping):
        raise TypeError("durable event must be a mapping")
    value = event.get("aggregate_type")
    if type(value) is not str:
        raise ValueError("durable event aggregate_type must be an exact string")
    return descriptor_for_aggregate_type(value)


def taxonomy_records() -> tuple[dict[str, str], ...]:
    return tuple(
        item.canonical_record()
        for item in sorted(_ALL, key=lambda item: item.aggregate_type)
    )


def taxonomy_digest() -> str:
    payload = {
        "taxonomy_version": TAXONOMY_VERSION,
        "descriptors": taxonomy_records(),
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + sha256(encoded).hexdigest()


def financial_qualification_aggregate_types() -> tuple[str, ...]:
    return tuple(
        item.aggregate_type
        for item in sorted(_ALL, key=lambda item: item.aggregate_type)
        if item.financial_qualification_relevant
    )
