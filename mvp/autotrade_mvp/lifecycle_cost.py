"""Frozen provider-neutral lifecycle-cost composition for decision evidence.

The profile composes already-normalized *estimates* for one instrument/horizon.
It is deliberately not realized financial truth: fills, fees, funding, borrow,
financing, settlement and corrections remain owned by their canonical durable
financial authorities.

Signed component rates are retained as evidence because rebates or favorable
funding can be real.  The allocator-facing projection is conservative: negative
components never reduce the non-negative cost budget.  No profitability or
economic-edge claim may be derived from this module alone.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
from uuid import UUID

from .exact_decimal import (
    ExactDecimalError,
    canonical_decimal_text,
    exact_sum,
    parse_bounded_exact_decimal,
)


class LifecycleCostError(ValueError):
    """Raised when lifecycle-cost evidence is incomplete or noncanonical."""


_PHASE_KINDS = {
    "TURNOVER": frozenset(
        {
            "COMMISSION",
            "EXCHANGE_FEE",
            "SPREAD",
            "SLIPPAGE",
            "FX_CONVERSION",
            "TRANSACTION_TAX",
        }
    ),
    "HOLDING": frozenset(
        {
            "FUNDING",
            "BORROW",
            "FINANCING",
            "CARRY",
            "STORAGE",
        }
    ),
}
_ALL_KINDS = frozenset().union(*_PHASE_KINDS.values())


def _text(value: object, *, name: str) -> str:
    if type(value) is not str:
        raise TypeError(f"{name} must be exact text")
    normalized = value.strip()
    if not normalized:
        raise LifecycleCostError(f"{name} is required")
    if value != normalized:
        raise LifecycleCostError(f"{name} must not contain surrounding whitespace")
    return value


def _instrument_version_ref(value: object) -> str:
    """Normalize the repository's canonical UUID@positive-Sequence identity.

    InstrumentVersion.instrument_id is a common-schema UUID and the version is
    the positive subset of common Sequence.  Parse this boundary locally rather
    than accepting provider symbols or arbitrary text as global identity.
    """

    if type(value) is not str:
        raise TypeError("instrument_version must be exact text")
    if not value or value != value.strip():
        raise LifecycleCostError(
            "instrument_version must be canonical instrument-id@version"
        )
    parts = value.split("@")
    if len(parts) != 2:
        raise LifecycleCostError(
            "instrument_version must be canonical instrument-id@version"
        )
    instrument_id, version_text = parts
    try:
        canonical_id = str(UUID(instrument_id))
    except (ValueError, TypeError, AttributeError) as error:
        raise LifecycleCostError(
            "instrument_version must be canonical instrument-id@version"
        ) from error
    if (
        not version_text
        or version_text[0] == "0"
        or any(character not in "0123456789" for character in version_text)
    ):
        raise LifecycleCostError(
            "instrument_version must be canonical instrument-id@version"
        )
    return f"{canonical_id}@{version_text}"


def _decimal(value: object, *, name: str) -> Decimal:
    if type(value) not in {Decimal, str, int}:
        raise TypeError(f"{name} must use exact Decimal, string or integer input")
    try:
        return parse_bounded_exact_decimal(value)
    except ExactDecimalError as error:
        raise LifecycleCostError(
            f"{name} must be a bounded finite decimal"
        ) from error


def _utc(value: object, *, name: str) -> datetime:
    if type(value) is not datetime:
        raise TypeError(f"{name} must be exact datetime")
    if value.tzinfo is None or type(value.tzinfo) is not timezone:
        raise LifecycleCostError(
            f"{name} must use a built-in fixed-offset timezone"
        )
    return value.astimezone(timezone.utc)


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _decimal_text(value: Decimal) -> str:
    try:
        return canonical_decimal_text(value)
    except ExactDecimalError as error:
        raise LifecycleCostError(
            "lifecycle cost exceeds exact-decimal resource envelope"
        ) from error


def _digest(value: object, *, name: str) -> str:
    text = _text(value, name=name)
    if (
        len(text) != 71
        or not text.startswith("sha256:")
        or text.lower() != text
        or any(character not in "0123456789abcdef" for character in text[7:])
    ):
        raise LifecycleCostError(
            f"{name} must be canonical sha256:<64 lowercase hex>"
        )
    return text


def _sum_rates(values) -> Decimal:
    try:
        return exact_sum(values)
    except ExactDecimalError as error:
        raise LifecycleCostError(
            "lifecycle cost arithmetic exceeds exact-decimal resource envelope"
        ) from error


@dataclass(frozen=True)
class LifecycleCostComponent:
    """One full-horizon normalized cost family with evidence validity."""

    component_id: str
    phase: str
    kind: str
    normalized_rate: Decimal
    evidence_ref: str
    observed_at: datetime
    valid_until: datetime

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "component_id",
            _text(self.component_id, name="component_id"),
        )
        phase = _text(self.phase, name="phase").upper()
        if phase not in _PHASE_KINDS:
            raise LifecycleCostError("phase must be TURNOVER or HOLDING")
        kind = _text(self.kind, name="kind").upper()
        if kind not in _PHASE_KINDS[phase]:
            raise LifecycleCostError(
                f"cost kind {kind} is not valid for phase {phase}"
            )
        object.__setattr__(self, "phase", phase)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(
            self,
            "normalized_rate",
            _decimal(self.normalized_rate, name="normalized_rate"),
        )
        object.__setattr__(
            self,
            "evidence_ref",
            _text(self.evidence_ref, name="evidence_ref"),
        )
        observed = _utc(self.observed_at, name="observed_at")
        valid_until = _utc(self.valid_until, name="valid_until")
        if valid_until < observed:
            raise LifecycleCostError("valid_until must not precede observed_at")
        object.__setattr__(self, "observed_at", observed)
        object.__setattr__(self, "valid_until", valid_until)


@dataclass(frozen=True)
class LifecycleCostRequirements:
    """Registered applicability rule for cost families in one use case.

    requirements_ref identifies the external evidence/policy that decided
    which families apply. This DTO binds that decision into the profile digest;
    it does not authenticate the reference by itself.
    """

    requirements_ref: str
    required_kinds: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "requirements_ref",
            _text(self.requirements_ref, name="requirements_ref"),
        )
        if type(self.required_kinds) is not tuple:
            raise TypeError("required_kinds must be an exact tuple")
        if not self.required_kinds:
            raise LifecycleCostError(
                "at least one required cost kind must be declared"
            )
        normalized: list[str] = []
        for value in self.required_kinds:
            kind = _text(value, name="required_cost_kind").upper()
            if kind not in _ALL_KINDS:
                raise LifecycleCostError(f"unsupported required cost kind: {kind}")
            if kind in normalized:
                raise LifecycleCostError("required_kinds must not contain duplicates")
            normalized.append(kind)
        object.__setattr__(self, "required_kinds", tuple(sorted(normalized)))


@dataclass(frozen=True)
class AllocationCostSplit:
    """Conservative allocator projection plus the exact profile binding."""

    cost_rate: Decimal
    turnover_cost_rate: Decimal
    holding_cost_rate: Decimal
    lifecycle_cost_digest: str
    profile_id: str
    decision_scope_ref: str
    requirements_ref: str
    component_evidence_refs: tuple[tuple[str, str], ...]
    instrument_version: str
    decision_time: datetime
    horizon_end: datetime

    def __post_init__(self) -> None:
        turnover = _decimal(self.turnover_cost_rate, name="turnover_cost_rate")
        holding = _decimal(self.holding_cost_rate, name="holding_cost_rate")
        total = _decimal(self.cost_rate, name="cost_rate")
        if turnover < 0 or holding < 0 or total < 0:
            raise LifecycleCostError("allocation cost projection must be non-negative")
        if _sum_rates((turnover, holding)) != total:
            raise LifecycleCostError(
                "turnover and holding rates must sum exactly to cost_rate"
            )
        object.__setattr__(self, "turnover_cost_rate", turnover)
        object.__setattr__(self, "holding_cost_rate", holding)
        object.__setattr__(self, "cost_rate", total)
        object.__setattr__(
            self,
            "lifecycle_cost_digest",
            _digest(self.lifecycle_cost_digest, name="lifecycle_cost_digest"),
        )
        object.__setattr__(self, "profile_id", _text(self.profile_id, name="profile_id"))
        object.__setattr__(
            self,
            "decision_scope_ref",
            _text(self.decision_scope_ref, name="decision_scope_ref"),
        )
        object.__setattr__(
            self,
            "requirements_ref",
            _text(self.requirements_ref, name="requirements_ref"),
        )
        object.__setattr__(
            self,
            "instrument_version",
            _instrument_version_ref(self.instrument_version),
        )
        decision_time = _utc(self.decision_time, name="decision_time")
        horizon_end = _utc(self.horizon_end, name="horizon_end")
        if horizon_end <= decision_time:
            raise LifecycleCostError("horizon_end must be after decision_time")
        object.__setattr__(self, "decision_time", decision_time)
        object.__setattr__(self, "horizon_end", horizon_end)

        if type(self.component_evidence_refs) is not tuple:
            raise TypeError("component_evidence_refs must be an exact tuple")
        normalized_refs: list[tuple[str, str]] = []
        seen_kinds: set[str] = set()
        for entry in self.component_evidence_refs:
            if type(entry) is not tuple or len(entry) != 2:
                raise TypeError(
                    "component_evidence_refs entries must be exact (kind, ref) tuples"
                )
            kind = _text(entry[0], name="component_evidence_kind").upper()
            if kind not in _ALL_KINDS:
                raise LifecycleCostError(
                    f"unsupported component evidence kind: {kind}"
                )
            if kind in seen_kinds:
                raise LifecycleCostError(
                    "component_evidence_refs kinds must be unique"
                )
            seen_kinds.add(kind)
            normalized_refs.append(
                (kind, _text(entry[1], name="component_evidence_ref"))
            )
        if not normalized_refs:
            raise LifecycleCostError("component_evidence_refs must not be empty")
        normalized_refs.sort()
        object.__setattr__(self, "component_evidence_refs", tuple(normalized_refs))


@dataclass(frozen=True)
class LifecycleCostProfile:
    """One immutable cost evidence cut for one instrument and holding horizon."""

    profile_id: str
    decision_scope_ref: str
    instrument_version: str
    as_of: datetime
    horizon_end: datetime
    requirements: LifecycleCostRequirements
    components: tuple[LifecycleCostComponent, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "profile_id", _text(self.profile_id, name="profile_id"))
        object.__setattr__(
            self,
            "decision_scope_ref",
            _text(self.decision_scope_ref, name="decision_scope_ref"),
        )
        object.__setattr__(
            self,
            "instrument_version",
            _instrument_version_ref(self.instrument_version),
        )
        as_of = _utc(self.as_of, name="as_of")
        horizon_end = _utc(self.horizon_end, name="horizon_end")
        if horizon_end <= as_of:
            raise LifecycleCostError("horizon_end must be after as_of")
        object.__setattr__(self, "as_of", as_of)
        object.__setattr__(self, "horizon_end", horizon_end)

        if type(self.requirements) is not LifecycleCostRequirements:
            raise TypeError("requirements must be exact LifecycleCostRequirements")
        requirements = LifecycleCostRequirements(
            requirements_ref=self.requirements.requirements_ref,
            required_kinds=self.requirements.required_kinds,
        )
        object.__setattr__(self, "requirements", requirements)

        if type(self.components) is not tuple:
            raise TypeError("components must be an exact tuple")
        detached: list[LifecycleCostComponent] = []
        ids: set[str] = set()
        kinds: set[str] = set()
        for value in self.components:
            if type(value) is not LifecycleCostComponent:
                raise TypeError("components must contain exact LifecycleCostComponent")
            component = LifecycleCostComponent(
                component_id=value.component_id,
                phase=value.phase,
                kind=value.kind,
                normalized_rate=value.normalized_rate,
                evidence_ref=value.evidence_ref,
                observed_at=value.observed_at,
                valid_until=value.valid_until,
            )
            if component.component_id in ids:
                raise LifecycleCostError("component_id must be unique")
            if component.kind in kinds:
                raise LifecycleCostError(
                    "each lifecycle cost kind must be represented exactly once"
                )
            ids.add(component.component_id)
            kinds.add(component.kind)
            if component.observed_at > as_of:
                raise LifecycleCostError(
                    f"cost component {component.kind} was observed after profile as_of"
                )
            if component.valid_until < horizon_end:
                raise LifecycleCostError(
                    f"cost component {component.kind} does not cover the full horizon"
                )
            detached.append(component)

        missing = set(requirements.required_kinds) - kinds
        if missing:
            raise LifecycleCostError(
                "lifecycle cost profile is missing required kinds: "
                + ", ".join(sorted(missing))
            )
        detached.sort(key=lambda item: (item.phase, item.kind, item.component_id))
        object.__setattr__(self, "components", tuple(detached))

    @property
    def net_expected_rate(self) -> Decimal:
        """Signed evidence sum; favorable rebates/funding remain visible."""

        return _sum_rates(component.normalized_rate for component in self.components)

    def _conservative_phase_rate(self, phase: str) -> Decimal:
        return _sum_rates(
            component.normalized_rate
            for component in self.components
            if component.phase == phase and component.normalized_rate > 0
        )

    @property
    def conservative_turnover_rate(self) -> Decimal:
        return self._conservative_phase_rate("TURNOVER")

    @property
    def conservative_holding_rate(self) -> Decimal:
        return self._conservative_phase_rate("HOLDING")

    @property
    def conservative_decision_rate(self) -> Decimal:
        return _sum_rates(
            (self.conservative_turnover_rate, self.conservative_holding_rate)
        )

    @property
    def digest(self) -> str:
        payload = {
            "profile_id": self.profile_id,
            "decision_scope_ref": self.decision_scope_ref,
            "instrument_version": self.instrument_version,
            "as_of": _utc_text(self.as_of),
            "horizon_end": _utc_text(self.horizon_end),
            "requirements_ref": self.requirements.requirements_ref,
            "required_kinds": list(self.requirements.required_kinds),
            "components": [
                {
                    "component_id": component.component_id,
                    "phase": component.phase,
                    "kind": component.kind,
                    "normalized_rate": _decimal_text(component.normalized_rate),
                    "evidence_ref": component.evidence_ref,
                    "observed_at": _utc_text(component.observed_at),
                    "valid_until": _utc_text(component.valid_until),
                }
                for component in self.components
            ],
        }
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        return "sha256:" + sha256(encoded).hexdigest()


def _detach_profile(value: object) -> LifecycleCostProfile:
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


def allocation_cost_split(
    profile: LifecycleCostProfile,
    *,
    decision_scope_ref: str,
    instrument_version: str,
    decision_time: datetime,
    horizon_end: datetime,
) -> AllocationCostSplit:
    """Project one frozen profile only for its exact allocation context.

    The caller must present the instrument identity, decision cut and forecast
    horizon that will consume the rates.  This prevents a valid profile for a
    different instrument or horizon from being silently reduced to three bare
    Decimal values and reused out of context.

    Negative/rebate components remain in ``net_expected_rate`` but cannot make
    the allocator's cost budget smaller.  The returned split therefore always
    satisfies ``turnover_cost_rate + holding_cost_rate == cost_rate`` without
    relying on ambient Decimal context.
    """

    snapshot = _detach_profile(profile)
    expected_scope = _text(decision_scope_ref, name="decision_scope_ref")
    expected_instrument = _instrument_version_ref(instrument_version)
    expected_decision_time = _utc(decision_time, name="decision_time")
    expected_horizon_end = _utc(horizon_end, name="allocation_horizon_end")
    if expected_scope != snapshot.decision_scope_ref:
        raise LifecycleCostError(
            "lifecycle cost decision scope does not match allocation decision scope"
        )
    if expected_instrument != snapshot.instrument_version:
        raise LifecycleCostError(
            "lifecycle cost instrument does not match allocation instrument"
        )
    if expected_decision_time != snapshot.as_of:
        raise LifecycleCostError(
            "lifecycle cost decision cut does not match allocation decision time"
        )
    if expected_horizon_end != snapshot.horizon_end:
        raise LifecycleCostError(
            "lifecycle cost horizon does not match allocation horizon"
        )

    turnover = snapshot.conservative_turnover_rate
    holding = snapshot.conservative_holding_rate
    total = _sum_rates((turnover, holding))
    return AllocationCostSplit(
        cost_rate=total,
        turnover_cost_rate=turnover,
        holding_cost_rate=holding,
        lifecycle_cost_digest=snapshot.digest,
        profile_id=snapshot.profile_id,
        decision_scope_ref=snapshot.decision_scope_ref,
        requirements_ref=snapshot.requirements.requirements_ref,
        component_evidence_refs=tuple(
            (component.kind, component.evidence_ref)
            for component in snapshot.components
        ),
        instrument_version=snapshot.instrument_version,
        decision_time=snapshot.as_of,
        horizon_end=snapshot.horizon_end,
    )
