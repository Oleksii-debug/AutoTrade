"""Versioned domain taxonomy for durable JournalStore aggregate families.

The durable journal contains financial/economic facts, financial-control facts,
and product-control facts. Runtime qualification must never let a campaign caller
decide after the fact which durable families count as financial. This module is
the persistence/domain classification authority used by qualification readers.

Unknown aggregate types fail closed. Adding a new production JournalStore writer
therefore requires an explicit descriptor and changes the taxonomy digest bound
to frozen qualification evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from types import MappingProxyType
from typing import Mapping


TAXONOMY_SCHEMA_VERSION = "1.0.0"

FINANCIAL = "FINANCIAL"
FINANCIAL_CONTROL = "FINANCIAL_CONTROL"
NON_FINANCIAL = "NON_FINANCIAL"
_ALLOWED_DOMAIN_CLASSIFICATIONS = frozenset(
    {FINANCIAL, FINANCIAL_CONTROL, NON_FINANCIAL}
)

QUALIFICATION_FINANCIAL = "FINANCIAL"
QUALIFICATION_NON_FINANCIAL = "NON_FINANCIAL"
_ALLOWED_QUALIFICATION_VISIBILITY = frozenset(
    {QUALIFICATION_FINANCIAL, QUALIFICATION_NON_FINANCIAL}
)


class JournalTaxonomyError(ValueError):
    """Raised when durable aggregate classification is absent or malformed."""


def _text(value: object, *, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise JournalTaxonomyError(f"{name} must be canonical non-empty text")
    return value


@dataclass(frozen=True)
class JournalAggregateDescriptor:
    aggregate_type: str
    domain_classification: str
    qualification_visibility: str

    def __post_init__(self) -> None:
        aggregate_type = _text(self.aggregate_type, name="aggregate_type")
        domain = _text(self.domain_classification, name="domain_classification")
        visibility = _text(
            self.qualification_visibility,
            name="qualification_visibility",
        )
        if domain not in _ALLOWED_DOMAIN_CLASSIFICATIONS:
            raise JournalTaxonomyError("unsupported durable domain classification")
        if visibility not in _ALLOWED_QUALIFICATION_VISIBILITY:
            raise JournalTaxonomyError("unsupported qualification visibility")
        if domain == NON_FINANCIAL and visibility != QUALIFICATION_NON_FINANCIAL:
            raise JournalTaxonomyError(
                "non-financial aggregate cannot be qualification-financial"
            )
        object.__setattr__(self, "aggregate_type", aggregate_type)
        object.__setattr__(self, "domain_classification", domain)
        object.__setattr__(self, "qualification_visibility", visibility)

    @property
    def payload(self) -> dict[str, str]:
        return {
            "aggregate_type": self.aggregate_type,
            "domain_classification": self.domain_classification,
            "qualification_visibility": self.qualification_visibility,
        }

    @property
    def is_financial_for_qualification(self) -> bool:
        return self.qualification_visibility == QUALIFICATION_FINANCIAL


def _descriptor(
    aggregate_type: str,
    domain_classification: str,
    qualification_visibility: str,
) -> JournalAggregateDescriptor:
    return JournalAggregateDescriptor(
        aggregate_type=aggregate_type,
        domain_classification=domain_classification,
        qualification_visibility=qualification_visibility,
    )


# This registry is intentionally explicit. These aggregate families are emitted
# by current production JournalStore writers (inventory traced in #613) plus the
# durable runtime-qualification evidence families. Financial-control families
# remain qualification-visible: silently ignoring authority/risk/submission
# activity during a financial load campaign would make event conservation false.
_DESCRIPTORS = (
    _descriptor("FUTURES_VARIATION_MARGIN", FINANCIAL, QUALIFICATION_FINANCIAL),
    _descriptor("HOST_CONTROL", NON_FINANCIAL, QUALIFICATION_NON_FINANCIAL),
    _descriptor("account_reconciliation", FINANCIAL_CONTROL, QUALIFICATION_FINANCIAL),
    _descriptor("alpaca_option_lifecycle_obligation", FINANCIAL, QUALIFICATION_FINANCIAL),
    _descriptor("authority_state", FINANCIAL_CONTROL, QUALIFICATION_FINANCIAL),
    _descriptor("canonical_simulation_session", NON_FINANCIAL, QUALIFICATION_NON_FINANCIAL),
    _descriptor("capability_history", FINANCIAL_CONTROL, QUALIFICATION_FINANCIAL),
    _descriptor("corporate_action_evidence", FINANCIAL, QUALIFICATION_FINANCIAL),
    _descriptor("economic_book", FINANCIAL, QUALIFICATION_FINANCIAL),
    _descriptor("financial-authority", FINANCIAL_CONTROL, QUALIFICATION_FINANCIAL),
    _descriptor("model_budget", NON_FINANCIAL, QUALIFICATION_NON_FINANCIAL),
    _descriptor("option_lifecycle", FINANCIAL, QUALIFICATION_FINANCIAL),
    _descriptor("order_projection_book", FINANCIAL_CONTROL, QUALIFICATION_FINANCIAL),
    _descriptor("provider_activity", FINANCIAL, QUALIFICATION_FINANCIAL),
    _descriptor("provider_fill_financial_binding", FINANCIAL, QUALIFICATION_FINANCIAL),
    _descriptor(
        "provider_fill_reservation_correction_binding",
        FINANCIAL,
        QUALIFICATION_FINANCIAL,
    ),
    _descriptor("provider_nonce", FINANCIAL_CONTROL, QUALIFICATION_FINANCIAL),
    _descriptor("production_host_runtime", NON_FINANCIAL, QUALIFICATION_NON_FINANCIAL),
    _descriptor("trusted_chronology", NON_FINANCIAL, QUALIFICATION_NON_FINANCIAL),
    _descriptor("recovery_clock_incident", FINANCIAL_CONTROL, QUALIFICATION_FINANCIAL),
    _descriptor("recovery_owner", FINANCIAL_CONTROL, QUALIFICATION_FINANCIAL),
    _descriptor("reservation_book", FINANCIAL, QUALIFICATION_FINANCIAL),
    _descriptor("risk_decision", FINANCIAL_CONTROL, QUALIFICATION_FINANCIAL),
    _descriptor("risk_policy_registry", FINANCIAL_CONTROL, QUALIFICATION_FINANCIAL),
    _descriptor("runtime_qualification_latency", NON_FINANCIAL, QUALIFICATION_NON_FINANCIAL),
    _descriptor("runtime_qualification_plan", NON_FINANCIAL, QUALIFICATION_NON_FINANCIAL),
    _descriptor("securities_borrow_recall", FINANCIAL, QUALIFICATION_FINANCIAL),
    _descriptor("settlement_book", FINANCIAL, QUALIFICATION_FINANCIAL),
    _descriptor("simulation_portfolio", NON_FINANCIAL, QUALIFICATION_NON_FINANCIAL),
    _descriptor("submission_attempt", FINANCIAL_CONTROL, QUALIFICATION_FINANCIAL),
)

if len({item.aggregate_type for item in _DESCRIPTORS}) != len(_DESCRIPTORS):
    raise RuntimeError("durable journal taxonomy contains duplicate aggregate types")

_BY_AGGREGATE_TYPE: Mapping[str, JournalAggregateDescriptor] = MappingProxyType(
    {item.aggregate_type: item for item in _DESCRIPTORS}
)


def taxonomy_payload() -> dict[str, object]:
    """Return the canonical immutable material whose digest is release-bound."""

    return {
        "schema_version": TAXONOMY_SCHEMA_VERSION,
        "descriptors": [
            item.payload
            for item in sorted(_DESCRIPTORS, key=lambda value: value.aggregate_type)
        ],
    }


def taxonomy_digest() -> str:
    encoded = json.dumps(
        taxonomy_payload(),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + sha256(encoded).hexdigest()


def descriptors() -> Mapping[str, JournalAggregateDescriptor]:
    return _BY_AGGREGATE_TYPE


def require_journal_aggregate_descriptor(
    aggregate_type: object,
) -> JournalAggregateDescriptor:
    key = _text(aggregate_type, name="aggregate_type")
    descriptor = _BY_AGGREGATE_TYPE.get(key)
    if descriptor is None:
        raise JournalTaxonomyError(
            f"durable aggregate type is unclassified: {key}"
        )
    return descriptor


def is_financial_for_qualification(aggregate_type: object) -> bool:
    return require_journal_aggregate_descriptor(
        aggregate_type
    ).is_financial_for_qualification
