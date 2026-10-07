"""Bridge frozen lifecycle-cost evidence into canonical WP-32 valuation buckets.

This module does not create a second allocator or valuation authority. It
projects one exact LifecycleCostProfile into the five cost buckets already
required by allocation_valuation.normalize_allocation_valuation.

The projection is conservative: favorable/negative estimated components remain
bound into evidence identity but never reduce a valuation cost rate. It is
decision evidence only, not realized cashflow, profitability, economic-edge,
provider, PAPER or LIVE authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from hashlib import sha256
import json
from types import MappingProxyType
from typing import Mapping

from .exact_decimal import ExactDecimalError, canonical_decimal_text, exact_sum
from .lifecycle_cost import (
    AllocationCostSplit,
    LifecycleCostProfile,
    allocation_cost_split,
)


class LifecycleValuationProjectionError(ValueError):
    """Frozen lifecycle evidence cannot be represented by WP-32 valuation."""


_MAPPING_POLICY_ID = "LIFECYCLE_TO_WP32_COST_BUCKETS_V1"
_BUCKET_ORDER = ("execution", "financing", "funding", "borrow", "fx")
_KIND_TO_BUCKET = {
    "COMMISSION": "execution",
    "EXCHANGE_FEE": "execution",
    "SPREAD": "execution",
    "SLIPPAGE": "execution",
    "TRANSACTION_TAX": "execution",
    "FINANCING": "financing",
    "CARRY": "financing",
    "STORAGE": "financing",
    "FUNDING": "funding",
    "BORROW": "borrow",
    "FX_CONVERSION": "fx",
}


def _snapshot_profile(value: object) -> LifecycleCostProfile:
    if type(value) is not LifecycleCostProfile:
        raise TypeError("profile must be exact LifecycleCostProfile")
    return LifecycleCostProfile(
        profile_id=value.profile_id,
        decision_scope_ref=value.decision_scope_ref,
        instrument_version=value.instrument_version,
        as_of=value.as_of,
        horizon_end=value.horizon_end,
        requirements=value.requirements,
        components=value.components,
    )


def _decimal_text(value: Decimal) -> str:
    try:
        return canonical_decimal_text(value)
    except ExactDecimalError as error:
        raise LifecycleValuationProjectionError(
            "valuation projection exceeds exact-decimal resource envelope"
        ) from error


def _sum(values) -> Decimal:
    try:
        return exact_sum(values)
    except ExactDecimalError as error:
        raise LifecycleValuationProjectionError(
            "valuation projection exceeds exact-decimal resource envelope"
        ) from error


def _bucket_evidence_ref(
    profile: LifecycleCostProfile,
    *,
    bucket: str,
) -> str:
    components = [
        {
            "kind": item.kind,
            "normalized_rate": _decimal_text(item.normalized_rate),
            "evidence_ref": item.evidence_ref,
        }
        for item in profile.components
        if _KIND_TO_BUCKET.get(item.kind) == bucket
    ]
    required_kinds = [
        kind
        for kind in profile.requirements.required_kinds
        if _KIND_TO_BUCKET.get(kind) == bucket
    ]
    payload = {
        "schema_version": "autotrade.lifecycle-valuation-evidence.v1",
        "mapping_policy_id": _MAPPING_POLICY_ID,
        "lifecycle_cost_digest": profile.digest,
        "requirements_ref": profile.requirements.requirements_ref,
        "bucket": bucket,
        "required_kinds": required_kinds,
        "components": components,
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class LifecycleValuationProjection:
    """Exact five-bucket projection consumed by canonical allocation valuation."""

    lifecycle_cost_digest: str
    mapping_policy_id: str
    decision_scope_ref: str
    instrument_version: str
    decision_time: datetime
    horizon_end: datetime
    cost_rate: Decimal
    cost_rate_components: Mapping[str, Decimal]
    cost_evidence_refs: Mapping[str, str]
    component_evidence_refs: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        if self.mapping_policy_id != _MAPPING_POLICY_ID:
            raise LifecycleValuationProjectionError(
                "unsupported lifecycle valuation mapping policy"
            )
        if tuple(self.cost_rate_components) != _BUCKET_ORDER:
            raise LifecycleValuationProjectionError(
                "valuation projection must contain canonical cost buckets in order"
            )
        if tuple(self.cost_evidence_refs) != _BUCKET_ORDER:
            raise LifecycleValuationProjectionError(
                "valuation projection must contain canonical evidence buckets in order"
            )
        values = tuple(self.cost_rate_components[name] for name in _BUCKET_ORDER)
        if any(type(value) is not Decimal or value < 0 for value in values):
            raise LifecycleValuationProjectionError(
                "valuation cost buckets must be exact non-negative Decimal values"
            )
        if _sum(values) != self.cost_rate:
            raise LifecycleValuationProjectionError(
                "valuation cost buckets do not sum to lifecycle cost_rate"
            )
        for name in _BUCKET_ORDER:
            ref = self.cost_evidence_refs[name]
            if (
                type(ref) is not str
                or len(ref) != 71
                or not ref.startswith("sha256:")
                or ref.lower() != ref
                or any(ch not in "0123456789abcdef" for ch in ref[7:])
            ):
                raise LifecycleValuationProjectionError(
                    f"{name} cost evidence ref must be canonical sha256"
                )
        object.__setattr__(
            self,
            "cost_rate_components",
            MappingProxyType(dict(self.cost_rate_components)),
        )
        object.__setattr__(
            self,
            "cost_evidence_refs",
            MappingProxyType(dict(self.cost_evidence_refs)),
        )


def project_lifecycle_cost_to_valuation(
    profile: LifecycleCostProfile,
    *,
    decision_scope_ref: str,
    instrument_version: str,
    decision_time: datetime,
    horizon_end: datetime,
) -> LifecycleValuationProjection:
    """Project one frozen lifecycle profile into current WP-32 cost buckets.

    allocation_cost_split remains the canonical context/freshness gate.
    Every lifecycle kind has one explicit v1 bucket mapping. Signed favorable
    estimates remain included in each bucket evidence digest, but only positive
    rates contribute to the numerical budget. The resulting five rates sum
    exactly to the conservative allocation split, so valuation cannot silently
    understate the cost already presented to allocation.
    """

    snapshot = _snapshot_profile(profile)
    split: AllocationCostSplit = allocation_cost_split(
        snapshot,
        decision_scope_ref=decision_scope_ref,
        instrument_version=instrument_version,
        decision_time=decision_time,
        horizon_end=horizon_end,
    )

    unknown = sorted(
        {item.kind for item in snapshot.components} - set(_KIND_TO_BUCKET)
    )
    if unknown:
        raise LifecycleValuationProjectionError(
            "lifecycle cost kinds lack WP-32 valuation mapping: "
            + ", ".join(unknown)
        )

    rates: dict[str, Decimal] = {}
    refs: dict[str, str] = {}
    for bucket in _BUCKET_ORDER:
        rates[bucket] = _sum(
            item.normalized_rate
            for item in snapshot.components
            if _KIND_TO_BUCKET[item.kind] == bucket
            and item.normalized_rate > 0
        )
        refs[bucket] = _bucket_evidence_ref(snapshot, bucket=bucket)

    if _sum(rates.values()) != split.cost_rate:
        raise LifecycleValuationProjectionError(
            "lifecycle-to-valuation mapping changes conservative total cost"
        )

    return LifecycleValuationProjection(
        lifecycle_cost_digest=split.lifecycle_cost_digest,
        mapping_policy_id=_MAPPING_POLICY_ID,
        decision_scope_ref=split.decision_scope_ref,
        instrument_version=split.instrument_version,
        decision_time=split.decision_time,
        horizon_end=split.horizon_end,
        cost_rate=split.cost_rate,
        cost_rate_components=rates,
        cost_evidence_refs=refs,
        component_evidence_refs=split.component_evidence_refs,
    )
