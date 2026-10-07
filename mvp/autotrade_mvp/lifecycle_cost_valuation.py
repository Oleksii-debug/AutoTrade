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
from datetime import datetime, timezone
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


def _exact_text(value: object, *, name: str) -> str:
    if type(value) is not str:
        raise TypeError(f"{name} must be exact text")
    if not value or value != value.strip():
        raise LifecycleValuationProjectionError(
            f"{name} must be canonical non-empty text"
        )
    return value


def _canonical_sha256(value: object, *, name: str) -> str:
    text = _exact_text(value, name=name)
    if (
        len(text) != 71
        or not text.startswith("sha256:")
        or text.lower() != text
        or any(character not in "0123456789abcdef" for character in text[7:])
    ):
        raise LifecycleValuationProjectionError(
            f"{name} must be canonical sha256:<64 lowercase hex>"
        )
    return text


def _utc(value: object, *, name: str) -> datetime:
    if type(value) is not datetime:
        raise TypeError(f"{name} must be exact datetime")
    if value.tzinfo is None or type(value.tzinfo) is not timezone:
        raise LifecycleValuationProjectionError(
            f"{name} must use a built-in fixed-offset timezone"
        )
    return value.astimezone(timezone.utc)


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
        object.__setattr__(
            self,
            "lifecycle_cost_digest",
            _canonical_sha256(
                self.lifecycle_cost_digest,
                name="lifecycle_cost_digest",
            ),
        )
        if type(self.mapping_policy_id) is not str:
            raise TypeError("mapping_policy_id must be exact text")
        if self.mapping_policy_id != _MAPPING_POLICY_ID:
            raise LifecycleValuationProjectionError(
                "unsupported lifecycle valuation mapping policy"
            )
        object.__setattr__(
            self,
            "decision_scope_ref",
            _exact_text(self.decision_scope_ref, name="decision_scope_ref"),
        )
        object.__setattr__(
            self,
            "instrument_version",
            _exact_text(self.instrument_version, name="instrument_version"),
        )
        decision_time = _utc(self.decision_time, name="decision_time")
        horizon_end = _utc(self.horizon_end, name="horizon_end")
        if horizon_end <= decision_time:
            raise LifecycleValuationProjectionError(
                "horizon_end must be after decision_time"
            )
        object.__setattr__(self, "decision_time", decision_time)
        object.__setattr__(self, "horizon_end", horizon_end)
        if type(self.cost_rate) is not Decimal:
            raise TypeError("cost_rate must be exact Decimal")
        if self.cost_rate < 0:
            raise LifecycleValuationProjectionError(
                "cost_rate must be non-negative"
            )
        if type(self.cost_rate_components) is not dict:
            raise TypeError("cost_rate_components must be an exact dict")
        if type(self.cost_evidence_refs) is not dict:
            raise TypeError("cost_evidence_refs must be an exact dict")
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
        if type(self.component_evidence_refs) is not tuple:
            raise TypeError("component_evidence_refs must be an exact tuple")
        seen_component_kinds: set[str] = set()
        normalized_component_refs: list[tuple[str, str]] = []
        for entry in self.component_evidence_refs:
            if type(entry) is not tuple or len(entry) != 2:
                raise TypeError(
                    "component_evidence_refs entries must be exact (kind, ref) tuples"
                )
            kind = _exact_text(entry[0], name="component evidence kind")
            evidence_ref = _exact_text(entry[1], name="component evidence ref")
            if kind in seen_component_kinds:
                raise LifecycleValuationProjectionError(
                    "component evidence kinds must be unique"
                )
            seen_component_kinds.add(kind)
            normalized_component_refs.append((kind, evidence_ref))
        object.__setattr__(
            self,
            "component_evidence_refs",
            tuple(normalized_component_refs),
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
