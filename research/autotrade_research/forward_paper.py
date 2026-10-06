"""Fail-closed evidence evaluation for frozen forward-paper campaigns.

This module does not schedule campaigns, call providers, submit orders, select a
champion or determine economic edge. It only validates recorded forward-paper
evidence against a protocol that was frozen before observations were evaluated.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from types import MappingProxyType
from typing import Mapping, Sequence
from hashlib import sha256
import json
import re


class ForwardPaperError(ValueError):
    """Raised when forward-paper evidence is malformed or causally invalid."""


_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_MAPPING_PROXY_TYPE = type(MappingProxyType({}))


def _text(value: str, *, name: str) -> str:
    if type(value) is not str or not value.strip():
        raise ForwardPaperError(f"{name} is required")
    return value.strip()


def _instant(value: str, *, name: str) -> datetime:
    text = _text(value, name=name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise ForwardPaperError(f"{name} must be an ISO timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ForwardPaperError(f"{name} must include timezone")
    return parsed.astimezone(timezone.utc)


def _hash(value: str, *, name: str) -> str:
    text = _text(value, name=name)
    if _SHA256.fullmatch(text) is None:
        raise ForwardPaperError(f"{name} must be canonical sha256:<64 lowercase hex>")
    return text

def _git_sha(value: str, *, name: str) -> str:
    text = _text(value, name=name)
    if _GIT_SHA.fullmatch(text) is None:
        raise ForwardPaperError(
            f"{name} must be a 40-character lowercase git SHA"
        )
    return text



def forward_paper_protocol_hash(
    *,
    campaign_id: str,
    exact_build_sha: str,
    registered_at: str,
    starts_at: str,
    ends_at: str,
    minimum_predictions: int,
    maximum_decision_latency_ms: int,
    required_provider_capabilities: Sequence[str],
    required_operational_cases: Sequence[str],
    required_regimes: Sequence[str] = (),
    minimum_decision_units_per_regime: int = 1,
    required_simulation_limitations: Sequence[str] = (),
    reporting_currency: str = "USD",
    maximum_drawdown: object | None = None,
    evaluation_profile_hash: str | None = None,
) -> str:
    """Canonical semantic identity of a frozen forward-paper protocol."""

    campaign = _text(campaign_id, name="campaign_id")
    build = _git_sha(exact_build_sha, name="exact_build_sha")
    registered = _instant(registered_at, name="registered_at")
    start = _instant(starts_at, name="starts_at")
    end = _instant(ends_at, name="ends_at")
    minimum = _positive_int(minimum_predictions, name="minimum_predictions")
    latency = _positive_int(
        maximum_decision_latency_ms,
        name="maximum_decision_latency_ms",
        allow_zero=True,
    )
    capabilities = tuple(
        sorted(
            _unique_text(
                required_provider_capabilities,
                name="required_provider_capabilities",
            )
        )
    )
    if not capabilities:
        raise ForwardPaperError("at least one provider capability is required")
    raw_cases = _unique_text(
        required_operational_cases,
        name="required_operational_cases",
    )
    cases = tuple(sorted(value.upper() for value in raw_cases))
    if len(set(cases)) != len(cases):
        raise ForwardPaperError(
            "required_operational_cases contains case-insensitive duplicates"
        )
    raw_regimes = _unique_text(required_regimes, name="required_regimes")
    regimes = tuple(sorted(value.upper() for value in raw_regimes))
    if len(set(regimes)) != len(regimes):
        raise ForwardPaperError("required_regimes contains case-insensitive duplicates")
    if "UNSPECIFIED" in regimes:
        raise ForwardPaperError("UNSPECIFIED cannot be a required regime")
    unit_minimum = _positive_int(
        minimum_decision_units_per_regime,
        name="minimum_decision_units_per_regime",
    )
    raw_limitations = _unique_text(
        required_simulation_limitations,
        name="required_simulation_limitations",
    )
    limitations = tuple(sorted(value.upper() for value in raw_limitations))
    if len(set(limitations)) != len(limitations):
        raise ForwardPaperError(
            "required_simulation_limitations contains case-insensitive duplicates"
        )
    if "UNSPECIFIED" in limitations:
        raise ForwardPaperError(
            "UNSPECIFIED cannot be a required simulation limitation"
        )
    currency = _text(reporting_currency, name="reporting_currency").upper()
    drawdown = (
        None
        if maximum_drawdown is None
        else _decimal(maximum_drawdown, name="maximum_drawdown", nonnegative=True)
    )
    evaluation_profile = (
        None
        if evaluation_profile_hash is None
        else _hash(evaluation_profile_hash, name="evaluation_profile_hash")
    )
    extended = bool(
        regimes
        or limitations
        or drawdown is not None
        or unit_minimum != 1
        or currency != "USD"
        or evaluation_profile is not None
    )
    payload = {
        "schema_version": 2 if extended else 1,
        "campaign_id": campaign,
        "exact_build_sha": build,
        "registered_at": registered.isoformat(),
        "starts_at": start.isoformat(),
        "ends_at": end.isoformat(),
        "minimum_predictions": minimum,
        "maximum_decision_latency_ms": latency,
        "required_provider_capabilities": list(capabilities),
        "required_operational_cases": list(cases),
    }
    if extended:
        payload.update(
            {
                "required_regimes": list(regimes),
                "minimum_decision_units_per_regime": unit_minimum,
                "required_simulation_limitations": list(limitations),
                "reporting_currency": currency,
                "maximum_drawdown": (
                    None if drawdown is None else _canonical_decimal_text(drawdown)
                ),
                "evaluation_profile_hash": evaluation_profile,
            }
        )
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + sha256(encoded).hexdigest()


def _decimal(value, *, name: str, nonnegative: bool = False) -> Decimal:
    if type(value) not in (str, int, Decimal):
        raise ForwardPaperError(f"{name} must use exact decimal input")
    try:
        result = value if type(value) is Decimal else Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ForwardPaperError(f"{name} must be a finite decimal") from error
    if not result.is_finite():
        raise ForwardPaperError(f"{name} must be a finite decimal")
    if nonnegative and result < 0:
        raise ForwardPaperError(f"{name} cannot be negative")
    return result


def _canonical_decimal_text(value: Decimal) -> str:
    """Render one exact Decimal without representation-only trailing zeros."""

    if type(value) is not Decimal or not value.is_finite():
        raise ForwardPaperError("canonical decimal must be finite Decimal")
    sign, digits_tuple, exponent = value.as_tuple()
    digits = list(digits_tuple)
    while digits and digits[-1] == 0:
        digits.pop()
        exponent += 1
    if not digits:
        return "0"
    coefficient = "".join(str(digit) for digit in digits)
    if exponent >= 0:
        rendered = coefficient + ("0" * exponent)
    else:
        split = len(coefficient) + exponent
        if split > 0:
            rendered = coefficient[:split] + "." + coefficient[split:]
        else:
            rendered = "0." + ("0" * (-split)) + coefficient
    return ("-" if sign else "") + rendered


def _positive_int(value: int, *, name: str, allow_zero: bool = False) -> int:
    if type(value) is not int:
        raise ForwardPaperError(f"{name} must be an integer")
    minimum = 0 if allow_zero else 1
    if value < minimum:
        raise ForwardPaperError(f"{name} must be >= {minimum}")
    return value


def _unique_text(values: Sequence[str], *, name: str) -> tuple[str, ...]:
    if type(values) not in (list, tuple):
        raise ForwardPaperError(f"{name} must be a plain list or tuple")
    normalized = tuple(_text(value, name=name) for value in values)
    if len(set(normalized)) != len(normalized):
        raise ForwardPaperError(f"{name} contains duplicates")
    return normalized


def _plain_records(values: object, *, name: str) -> tuple[object, ...]:
    if type(values) not in (list, tuple):
        raise ForwardPaperError(f"{name} must be a plain list or tuple")
    return tuple(values)


def _is_plain_mapping(value: object) -> bool:
    return type(value) is dict


@dataclass(frozen=True)
class ForwardPaperProtocol:
    campaign_id: str
    exact_build_sha: str
    protocol_hash: str
    registered_at: str
    starts_at: str
    ends_at: str
    minimum_predictions: int
    maximum_decision_latency_ms: int
    required_provider_capabilities: tuple[str, ...]
    required_operational_cases: tuple[str, ...]
    required_regimes: tuple[str, ...] = ()
    minimum_decision_units_per_regime: int = 1
    required_simulation_limitations: tuple[str, ...] = ()
    reporting_currency: str = "USD"
    maximum_drawdown: Decimal | None = None
    evaluation_profile_hash: str | None = None

    def __post_init__(self) -> None:
        build = _git_sha(self.exact_build_sha, name="exact_build_sha")
        registered = _instant(self.registered_at, name="registered_at")
        start = _instant(self.starts_at, name="starts_at")
        end = _instant(self.ends_at, name="ends_at")
        if registered >= start:
            raise ForwardPaperError(
                "registered_at must be strictly before campaign starts_at"
            )
        if end <= start:
            raise ForwardPaperError("ends_at must be after starts_at")
        capabilities = _unique_text(
            self.required_provider_capabilities,
            name="required_provider_capabilities",
        )
        if not capabilities:
            raise ForwardPaperError(
                "at least one provider capability is required"
            )
        raw_cases = _unique_text(
            self.required_operational_cases,
            name="required_operational_cases",
        )
        cases = tuple(value.upper() for value in raw_cases)
        if len(set(cases)) != len(cases):
            raise ForwardPaperError(
                "required_operational_cases contains case-insensitive duplicates"
            )
        raw_regimes = _unique_text(self.required_regimes, name="required_regimes")
        regimes = tuple(value.upper() for value in raw_regimes)
        if len(set(regimes)) != len(regimes):
            raise ForwardPaperError("required_regimes contains case-insensitive duplicates")
        if "UNSPECIFIED" in regimes:
            raise ForwardPaperError("UNSPECIFIED cannot be a required regime")
        unit_minimum = _positive_int(
            self.minimum_decision_units_per_regime,
            name="minimum_decision_units_per_regime",
        )
        raw_limitations = _unique_text(
            self.required_simulation_limitations,
            name="required_simulation_limitations",
        )
        limitations = tuple(value.upper() for value in raw_limitations)
        if len(set(limitations)) != len(limitations):
            raise ForwardPaperError(
                "required_simulation_limitations contains case-insensitive duplicates"
            )
        if "UNSPECIFIED" in limitations:
            raise ForwardPaperError(
                "UNSPECIFIED cannot be a required simulation limitation"
            )
        currency = _text(self.reporting_currency, name="reporting_currency").upper()
        drawdown = (
            None
            if self.maximum_drawdown is None
            else _decimal(
                self.maximum_drawdown,
                name="maximum_drawdown",
                nonnegative=True,
            )
        )
        evaluation_profile = (
            None
            if self.evaluation_profile_hash is None
            else _hash(
                self.evaluation_profile_hash,
                name="evaluation_profile_hash",
            )
        )
        object.__setattr__(
            self,
            "campaign_id",
            _text(self.campaign_id, name="campaign_id"),
        )
        object.__setattr__(self, "exact_build_sha", build)
        provided_hash = _hash(self.protocol_hash, name="protocol_hash")
        expected_hash = forward_paper_protocol_hash(
            campaign_id=self.campaign_id,
            exact_build_sha=build,
            registered_at=self.registered_at,
            starts_at=self.starts_at,
            ends_at=self.ends_at,
            minimum_predictions=self.minimum_predictions,
            maximum_decision_latency_ms=self.maximum_decision_latency_ms,
            required_provider_capabilities=capabilities,
            required_operational_cases=cases,
            required_regimes=regimes,
            minimum_decision_units_per_regime=unit_minimum,
            required_simulation_limitations=limitations,
            reporting_currency=currency,
            maximum_drawdown=drawdown,
            evaluation_profile_hash=evaluation_profile,
        )
        if provided_hash != expected_hash:
            raise ForwardPaperError(
                "protocol_hash does not match canonical frozen protocol content"
            )
        object.__setattr__(self, "protocol_hash", provided_hash)
        object.__setattr__(
            self,
            "minimum_predictions",
            _positive_int(
                self.minimum_predictions,
                name="minimum_predictions",
            ),
        )
        object.__setattr__(
            self,
            "maximum_decision_latency_ms",
            _positive_int(
                self.maximum_decision_latency_ms,
                name="maximum_decision_latency_ms",
                allow_zero=True,
            ),
        )
        object.__setattr__(
            self,
            "required_provider_capabilities",
            capabilities,
        )
        object.__setattr__(self, "required_operational_cases", cases)
        object.__setattr__(self, "required_regimes", regimes)
        object.__setattr__(
            self,
            "minimum_decision_units_per_regime",
            unit_minimum,
        )
        object.__setattr__(
            self,
            "required_simulation_limitations",
            limitations,
        )
        object.__setattr__(self, "reporting_currency", currency)
        object.__setattr__(self, "maximum_drawdown", drawdown)
        object.__setattr__(
            self,
            "evaluation_profile_hash",
            evaluation_profile,
        )

    @classmethod
    def create(
        cls,
        *,
        campaign_id: str,
        exact_build_sha: str,
        protocol_hash: str,
        registered_at: str,
        starts_at: str,
        ends_at: str,
        minimum_predictions: int,
        maximum_decision_latency_ms: int,
        required_provider_capabilities: Sequence[str],
        required_operational_cases: Sequence[str],
        required_regimes: Sequence[str] = (),
        minimum_decision_units_per_regime: int = 1,
        required_simulation_limitations: Sequence[str] = (),
        reporting_currency: str = "USD",
        maximum_drawdown: object | None = None,
        evaluation_profile_hash: str | None = None,
    ) -> "ForwardPaperProtocol":
        build = _git_sha(exact_build_sha, name="exact_build_sha")
        registered = _instant(registered_at, name="registered_at")
        start = _instant(starts_at, name="starts_at")
        end = _instant(ends_at, name="ends_at")
        if registered >= start:
            raise ForwardPaperError(
                "registered_at must be strictly before campaign starts_at"
            )
        if end <= start:
            raise ForwardPaperError("ends_at must be after starts_at")
        capabilities = _unique_text(
            required_provider_capabilities,
            name="required_provider_capabilities",
        )
        if not capabilities:
            raise ForwardPaperError("at least one provider capability is required")
        raw_cases = _unique_text(
            required_operational_cases,
            name="required_operational_cases",
        )
        cases = tuple(value.upper() for value in raw_cases)
        if len(set(cases)) != len(cases):
            raise ForwardPaperError(
                "required_operational_cases contains case-insensitive duplicates"
            )
        raw_regimes = _unique_text(required_regimes, name="required_regimes")
        regimes = tuple(value.upper() for value in raw_regimes)
        if len(set(regimes)) != len(regimes):
            raise ForwardPaperError("required_regimes contains case-insensitive duplicates")
        if "UNSPECIFIED" in regimes:
            raise ForwardPaperError("UNSPECIFIED cannot be a required regime")
        unit_minimum = _positive_int(
            minimum_decision_units_per_regime,
            name="minimum_decision_units_per_regime",
        )
        raw_limitations = _unique_text(
            required_simulation_limitations,
            name="required_simulation_limitations",
        )
        limitations = tuple(value.upper() for value in raw_limitations)
        if len(set(limitations)) != len(limitations):
            raise ForwardPaperError(
                "required_simulation_limitations contains case-insensitive duplicates"
            )
        if "UNSPECIFIED" in limitations:
            raise ForwardPaperError(
                "UNSPECIFIED cannot be a required simulation limitation"
            )
        currency = _text(reporting_currency, name="reporting_currency").upper()
        drawdown = (
            None
            if maximum_drawdown is None
            else _decimal(maximum_drawdown, name="maximum_drawdown", nonnegative=True)
        )
        evaluation_profile = (
            None
            if evaluation_profile_hash is None
            else _hash(evaluation_profile_hash, name="evaluation_profile_hash")
        )
        return cls(
            campaign_id=_text(campaign_id, name="campaign_id"),
            exact_build_sha=build,
            protocol_hash=_hash(protocol_hash, name="protocol_hash"),
            registered_at=registered_at,
            starts_at=starts_at,
            ends_at=ends_at,
            minimum_predictions=_positive_int(
                minimum_predictions,
                name="minimum_predictions",
            ),
            maximum_decision_latency_ms=_positive_int(
                maximum_decision_latency_ms,
                name="maximum_decision_latency_ms",
                allow_zero=True,
            ),
            required_provider_capabilities=capabilities,
            required_operational_cases=cases,
            required_regimes=regimes,
            minimum_decision_units_per_regime=unit_minimum,
            required_simulation_limitations=limitations,
            reporting_currency=currency,
            maximum_drawdown=drawdown,
            evaluation_profile_hash=evaluation_profile,
        )


@dataclass(frozen=True)
class SealedPrediction:
    prediction_id: str
    provider_capability: str
    input_hash: str
    proposal_hash: str
    information_cutoff_at: str
    sealed_at: str
    decision_deadline_at: str
    outcome_horizon_end_at: str
    decision_latency_ms: int
    regime: str = "UNSPECIFIED"
    dependence_unit_id: str = "UNSPECIFIED"

    def __post_init__(self) -> None:
        cutoff = _instant(
            self.information_cutoff_at,
            name="information_cutoff_at",
        )
        sealed = _instant(self.sealed_at, name="sealed_at")
        _instant(self.decision_deadline_at, name="decision_deadline_at")
        horizon = _instant(
            self.outcome_horizon_end_at,
            name="outcome_horizon_end_at",
        )
        if cutoff > sealed:
            raise ForwardPaperError(
                "information cutoff cannot be after sealing"
            )
        if horizon <= sealed:
            raise ForwardPaperError("outcome horizon must be after sealing")
        object.__setattr__(
            self,
            "prediction_id",
            _text(self.prediction_id, name="prediction_id"),
        )
        object.__setattr__(
            self,
            "provider_capability",
            _text(self.provider_capability, name="provider_capability"),
        )
        object.__setattr__(
            self,
            "input_hash",
            _hash(self.input_hash, name="input_hash"),
        )
        object.__setattr__(
            self,
            "proposal_hash",
            _hash(self.proposal_hash, name="proposal_hash"),
        )
        object.__setattr__(
            self,
            "decision_latency_ms",
            _positive_int(
                self.decision_latency_ms,
                name="decision_latency_ms",
                allow_zero=True,
            ),
        )
        object.__setattr__(
            self,
            "regime",
            _text(self.regime, name="regime").upper(),
        )
        object.__setattr__(
            self,
            "dependence_unit_id",
            _text(self.dependence_unit_id, name="dependence_unit_id"),
        )

    @classmethod
    def create(
        cls,
        *,
        prediction_id: str,
        provider_capability: str,
        input_hash: str,
        proposal_hash: str,
        information_cutoff_at: str,
        sealed_at: str,
        decision_deadline_at: str,
        outcome_horizon_end_at: str,
        decision_latency_ms: int,
        regime: str = "UNSPECIFIED",
        dependence_unit_id: str = "UNSPECIFIED",
    ) -> "SealedPrediction":
        cutoff = _instant(information_cutoff_at, name="information_cutoff_at")
        sealed = _instant(sealed_at, name="sealed_at")
        deadline = _instant(decision_deadline_at, name="decision_deadline_at")
        horizon = _instant(outcome_horizon_end_at, name="outcome_horizon_end_at")
        if cutoff > sealed:
            raise ForwardPaperError("information cutoff cannot be after sealing")
        if horizon <= sealed:
            raise ForwardPaperError("outcome horizon must be after sealing")
        return cls(
            prediction_id=_text(prediction_id, name="prediction_id"),
            provider_capability=_text(
                provider_capability,
                name="provider_capability",
            ),
            input_hash=_hash(input_hash, name="input_hash"),
            proposal_hash=_hash(proposal_hash, name="proposal_hash"),
            information_cutoff_at=information_cutoff_at,
            sealed_at=sealed_at,
            decision_deadline_at=decision_deadline_at,
            outcome_horizon_end_at=outcome_horizon_end_at,
            decision_latency_ms=_positive_int(
                decision_latency_ms,
                name="decision_latency_ms",
                allow_zero=True,
            ),
            regime=_text(regime, name="regime").upper(),
            dependence_unit_id=_text(dependence_unit_id, name="dependence_unit_id"),
        )

    @property
    def met_deadline(self) -> bool:
        return _instant(self.sealed_at, name="sealed_at") <= _instant(
            self.decision_deadline_at,
            name="decision_deadline_at",
        )


@dataclass(frozen=True)
class ForwardOutcome:
    prediction_id: str
    outcome_hash: str
    outcome_available_at: str
    evaluated_at: str

    def __post_init__(self) -> None:
        available = _instant(
            self.outcome_available_at,
            name="outcome_available_at",
        )
        evaluated = _instant(self.evaluated_at, name="evaluated_at")
        if evaluated < available:
            raise ForwardPaperError(
                "evaluation cannot precede outcome availability"
            )
        object.__setattr__(
            self,
            "prediction_id",
            _text(self.prediction_id, name="prediction_id"),
        )
        object.__setattr__(
            self,
            "outcome_hash",
            _hash(self.outcome_hash, name="outcome_hash"),
        )

    @classmethod
    def create(
        cls,
        *,
        prediction_id: str,
        outcome_hash: str,
        outcome_available_at: str,
        evaluated_at: str,
    ) -> "ForwardOutcome":
        available = _instant(outcome_available_at, name="outcome_available_at")
        evaluated = _instant(evaluated_at, name="evaluated_at")
        if evaluated < available:
            raise ForwardPaperError("evaluation cannot precede outcome availability")
        return cls(
            prediction_id=_text(prediction_id, name="prediction_id"),
            outcome_hash=_hash(outcome_hash, name="outcome_hash"),
            outcome_available_at=outcome_available_at,
            evaluated_at=evaluated_at,
        )


@dataclass(frozen=True)
class OperationalObservation:
    provider_capability: str
    case: str
    observed_at: str
    reconciled: bool

    def __post_init__(self) -> None:
        if type(self.reconciled) is not bool:
            raise ForwardPaperError("reconciled must be boolean")
        _instant(self.observed_at, name="observed_at")
        object.__setattr__(
            self,
            "provider_capability",
            _text(self.provider_capability, name="provider_capability"),
        )
        object.__setattr__(
            self,
            "case",
            _text(self.case, name="case").upper(),
        )

    @classmethod
    def create(
        cls,
        *,
        provider_capability: str,
        case: str,
        observed_at: str,
        reconciled: bool,
    ) -> "OperationalObservation":
        if type(reconciled) is not bool:
            raise ForwardPaperError("reconciled must be boolean")
        _instant(observed_at, name="observed_at")
        return cls(
            provider_capability=_text(
                provider_capability,
                name="provider_capability",
            ),
            case=_text(case, name="case").upper(),
            observed_at=observed_at,
            reconciled=reconciled,
        )


@dataclass(frozen=True)
class PaperDecisionEconomics:
    prediction_id: str
    currency: str
    sequence: int
    realized_at: str
    gross_pnl: Decimal
    fees: Decimal
    spread_cost: Decimal
    slippage_cost: Decimal
    net_pnl: Decimal
    equity_before: Decimal
    equity_after: Decimal
    peak_equity_before: Decimal

    def __post_init__(self) -> None:
        prediction_id = _text(self.prediction_id, name="prediction_id")
        currency = _text(self.currency, name="currency").upper()
        sequence = _positive_int(self.sequence, name="sequence")
        _instant(self.realized_at, name="realized_at")
        gross = _decimal(self.gross_pnl, name="gross_pnl")
        fees = _decimal(self.fees, name="fees", nonnegative=True)
        spread = _decimal(self.spread_cost, name="spread_cost", nonnegative=True)
        slippage = _decimal(
            self.slippage_cost,
            name="slippage_cost",
            nonnegative=True,
        )
        net = _decimal(self.net_pnl, name="net_pnl")
        equity_before = _decimal(self.equity_before, name="equity_before")
        equity_after = _decimal(self.equity_after, name="equity_after")
        peak = _decimal(self.peak_equity_before, name="peak_equity_before")
        if (
            Fraction(net)
            != Fraction(gross) - Fraction(fees) - Fraction(spread) - Fraction(slippage)
        ):
            raise ForwardPaperError(
                "net_pnl must equal gross_pnl minus fees, spread_cost and slippage_cost"
            )
        if Fraction(equity_after) != Fraction(equity_before) + Fraction(net):
            raise ForwardPaperError("equity_after must equal equity_before plus net_pnl")
        if Fraction(peak) < Fraction(equity_before):
            raise ForwardPaperError(
                "peak_equity_before cannot be below equity_before"
            )
        object.__setattr__(self, "prediction_id", prediction_id)
        object.__setattr__(self, "currency", currency)
        object.__setattr__(self, "sequence", sequence)
        object.__setattr__(self, "gross_pnl", gross)
        object.__setattr__(self, "fees", fees)
        object.__setattr__(self, "spread_cost", spread)
        object.__setattr__(self, "slippage_cost", slippage)
        object.__setattr__(self, "net_pnl", net)
        object.__setattr__(self, "equity_before", equity_before)
        object.__setattr__(self, "equity_after", equity_after)
        object.__setattr__(self, "peak_equity_before", peak)

    @classmethod
    def create(
        cls,
        *,
        prediction_id: str,
        currency: str,
        sequence: int,
        realized_at: str,
        gross_pnl: object,
        fees: object,
        spread_cost: object,
        slippage_cost: object,
        net_pnl: object,
        equity_before: object,
        equity_after: object,
        peak_equity_before: object,
    ) -> "PaperDecisionEconomics":
        return cls(
            prediction_id=prediction_id,
            currency=currency,
            sequence=_positive_int(sequence, name="sequence"),
            realized_at=realized_at,
            gross_pnl=_decimal(gross_pnl, name="gross_pnl"),
            fees=_decimal(fees, name="fees", nonnegative=True),
            spread_cost=_decimal(spread_cost, name="spread_cost", nonnegative=True),
            slippage_cost=_decimal(
                slippage_cost,
                name="slippage_cost",
                nonnegative=True,
            ),
            net_pnl=_decimal(net_pnl, name="net_pnl"),
            equity_before=_decimal(equity_before, name="equity_before"),
            equity_after=_decimal(equity_after, name="equity_after"),
            peak_equity_before=_decimal(
                peak_equity_before,
                name="peak_equity_before",
            ),
        )


@dataclass(frozen=True)
class ForwardPaperEvidence:
    exact_build_sha: str
    protocol_hash: str
    observed_until: str
    predictions: tuple[SealedPrediction, ...]
    outcomes: tuple[ForwardOutcome, ...]
    operational_observations: tuple[OperationalObservation, ...]
    costs_by_currency: Mapping[str, Decimal]
    costs_complete: bool
    account_reconciliation_complete: bool
    paper_economics: tuple[PaperDecisionEconomics, ...] = ()
    simulation_limitations: tuple[str, ...] = ()
    evaluation_profile_hash: str | None = None

    def __post_init__(self) -> None:
        build = _git_sha(self.exact_build_sha, name="exact_build_sha")
        protocol_hash = _hash(self.protocol_hash, name="protocol_hash")
        _instant(self.observed_until, name="observed_until")
        if (
            type(self.costs_complete) is not bool
            or type(self.account_reconciliation_complete) is not bool
        ):
            raise ForwardPaperError("completion flags must be boolean")
        if not _is_plain_mapping(self.costs_by_currency):
            raise TypeError("costs_by_currency must be a plain mapping")
        costs: dict[str, Decimal] = {}
        for currency, value in self.costs_by_currency.items():
            if not isinstance(currency, str):
                raise TypeError("cost currency keys must be strings")
            code = _text(currency, name="cost currency").upper()
            if code in costs:
                raise ForwardPaperError("duplicate cost currency")
            costs[code] = _decimal(
                value,
                name=f"cost[{code}]",
                nonnegative=True,
            )
        object.__setattr__(self, "exact_build_sha", build)
        object.__setattr__(self, "protocol_hash", protocol_hash)
        object.__setattr__(
            self,
            "predictions",
            _plain_records(self.predictions, name="predictions"),
        )
        object.__setattr__(
            self,
            "outcomes",
            _plain_records(self.outcomes, name="outcomes"),
        )
        object.__setattr__(
            self,
            "operational_observations",
            _plain_records(
                self.operational_observations,
                name="operational_observations",
            ),
        )
        object.__setattr__(
            self,
            "costs_by_currency",
            MappingProxyType(costs),
        )
        object.__setattr__(
            self,
            "paper_economics",
            _plain_records(self.paper_economics, name="paper_economics"),
        )
        raw_limitations = _unique_text(
            self.simulation_limitations,
            name="simulation_limitations",
        )
        limitations = tuple(value.upper() for value in raw_limitations)
        if len(set(limitations)) != len(limitations):
            raise ForwardPaperError(
                "simulation_limitations contains case-insensitive duplicates"
            )
        object.__setattr__(self, "simulation_limitations", limitations)
        profile = (
            None
            if self.evaluation_profile_hash is None
            else _hash(
                self.evaluation_profile_hash,
                name="evaluation_profile_hash",
            )
        )
        object.__setattr__(self, "evaluation_profile_hash", profile)

    @classmethod
    def create(
        cls,
        *,
        exact_build_sha: str,
        protocol_hash: str,
        observed_until: str,
        predictions: Sequence[SealedPrediction],
        outcomes: Sequence[ForwardOutcome],
        operational_observations: Sequence[OperationalObservation],
        costs_by_currency: Mapping[str, object],
        costs_complete: bool,
        account_reconciliation_complete: bool,
        paper_economics: Sequence[PaperDecisionEconomics] = (),
        simulation_limitations: Sequence[str] = (),
        evaluation_profile_hash: str | None = None,
    ) -> "ForwardPaperEvidence":
        build = _git_sha(exact_build_sha, name="exact_build_sha")
        _instant(observed_until, name="observed_until")
        if type(costs_complete) is not bool or type(account_reconciliation_complete) is not bool:
            raise ForwardPaperError("completion flags must be boolean")
        if not _is_plain_mapping(costs_by_currency):
            raise TypeError("costs_by_currency must be a plain mapping")
        costs: dict[str, Decimal] = {}
        for currency, value in costs_by_currency.items():
            if not isinstance(currency, str):
                raise TypeError("cost currency keys must be strings")
            code = _text(currency, name="cost currency").upper()
            if code in costs:
                raise ForwardPaperError("duplicate cost currency")
            costs[code] = _decimal(value, name=f"cost[{code}]", nonnegative=True)
        return cls(
            exact_build_sha=build,
            protocol_hash=_hash(protocol_hash, name="protocol_hash"),
            observed_until=observed_until,
            predictions=_plain_records(predictions, name="predictions"),
            outcomes=_plain_records(outcomes, name="outcomes"),
            operational_observations=_plain_records(
                operational_observations,
                name="operational_observations",
            ),
            costs_by_currency=costs,
            costs_complete=costs_complete,
            account_reconciliation_complete=account_reconciliation_complete,
            paper_economics=_plain_records(
                paper_economics,
                name="paper_economics",
            ),
            simulation_limitations=_unique_text(
                simulation_limitations,
                name="simulation_limitations",
            ),
            evaluation_profile_hash=evaluation_profile_hash,
        )


@dataclass(frozen=True)
class ForwardPaperAssessment:
    evidence_status: str
    operational_status: str
    economic_edge_status: str
    reasons: tuple[str, ...]
    prediction_count: int
    evaluated_outcome_count: int

    def __post_init__(self) -> None:
        if self.evidence_status not in {"VALID", "INCONCLUSIVE", "INVALID"}:
            raise ForwardPaperError("unsupported evidence_status")
        if self.operational_status not in {"PASS", "FAIL", "INCONCLUSIVE"}:
            raise ForwardPaperError("unsupported operational_status")
        if self.economic_edge_status != "NOT_ESTABLISHED":
            raise ForwardPaperError(
                "forward-paper mechanics foundation cannot establish economic edge"
            )


def assess_forward_paper(
    protocol: ForwardPaperProtocol,
    evidence: ForwardPaperEvidence,
) -> ForwardPaperAssessment:
    """Validate frozen forward evidence without declaring economic edge."""

    if type(protocol) is not ForwardPaperProtocol:
        raise TypeError("protocol must be exact ForwardPaperProtocol")
    if type(evidence) is not ForwardPaperEvidence:
        raise TypeError("evidence must be exact ForwardPaperEvidence")

    # Frozen dataclasses are convenience immutability, not a trust boundary:
    # object.__setattr__ can still alter an instance after construction. Re-admit
    # the complete top-level protocol/evidence through canonical constructors
    # before evaluating any caller-retained object.
    protocol = ForwardPaperProtocol.create(
        campaign_id=protocol.campaign_id,
        exact_build_sha=protocol.exact_build_sha,
        protocol_hash=protocol.protocol_hash,
        registered_at=protocol.registered_at,
        starts_at=protocol.starts_at,
        ends_at=protocol.ends_at,
        minimum_predictions=protocol.minimum_predictions,
        maximum_decision_latency_ms=protocol.maximum_decision_latency_ms,
        required_provider_capabilities=protocol.required_provider_capabilities,
        required_operational_cases=protocol.required_operational_cases,
        required_regimes=protocol.required_regimes,
        minimum_decision_units_per_regime=protocol.minimum_decision_units_per_regime,
        required_simulation_limitations=protocol.required_simulation_limitations,
        reporting_currency=protocol.reporting_currency,
        maximum_drawdown=protocol.maximum_drawdown,
        evaluation_profile_hash=protocol.evaluation_profile_hash,
    )
    evidence = ForwardPaperEvidence.create(
        exact_build_sha=evidence.exact_build_sha,
        protocol_hash=evidence.protocol_hash,
        observed_until=evidence.observed_until,
        predictions=evidence.predictions,
        outcomes=evidence.outcomes,
        operational_observations=evidence.operational_observations,
        costs_by_currency=evidence.costs_by_currency,
        costs_complete=evidence.costs_complete,
        account_reconciliation_complete=evidence.account_reconciliation_complete,
        paper_economics=evidence.paper_economics,
        simulation_limitations=evidence.simulation_limitations,
        evaluation_profile_hash=evidence.evaluation_profile_hash,
    )

    invalid: list[str] = []
    incomplete: list[str] = []
    operational_failures: list[str] = []

    if evidence.exact_build_sha != protocol.exact_build_sha:
        invalid.append("exact_build_sha_mismatch")
    if evidence.protocol_hash != protocol.protocol_hash:
        invalid.append("protocol_hash_mismatch")

    campaign_start = _instant(protocol.starts_at, name="starts_at")
    campaign_end = _instant(protocol.ends_at, name="ends_at")
    observed_until = _instant(evidence.observed_until, name="observed_until")

    if not protocol.required_regimes:
        incomplete.append("regime_coverage_not_registered")
    if not protocol.required_simulation_limitations:
        incomplete.append("simulation_limitations_not_registered")
    if protocol.maximum_drawdown is None:
        incomplete.append("maximum_drawdown_not_registered")
    if protocol.evaluation_profile_hash is None:
        incomplete.append("evaluation_profile_not_registered")
    elif evidence.evaluation_profile_hash is None:
        incomplete.append("evaluation_profile_evidence_missing")
    elif evidence.evaluation_profile_hash != protocol.evaluation_profile_hash:
        invalid.append("evaluation_profile_hash_mismatch")

    prediction_by_id: dict[str, SealedPrediction] = {}
    capability_counts = {
        capability: 0 for capability in protocol.required_provider_capabilities
    }
    regime_decision_units: dict[str, set[str]] = {
        regime: set() for regime in protocol.required_regimes
    }
    dependence_unit_regime: dict[str, str] = {}
    for prediction in evidence.predictions:
        if type(prediction) is not SealedPrediction:
            invalid.append("invalid_prediction_record")
            continue
        try:
            prediction = SealedPrediction.create(
                prediction_id=prediction.prediction_id,
                provider_capability=prediction.provider_capability,
                input_hash=prediction.input_hash,
                proposal_hash=prediction.proposal_hash,
                information_cutoff_at=prediction.information_cutoff_at,
                sealed_at=prediction.sealed_at,
                decision_deadline_at=prediction.decision_deadline_at,
                outcome_horizon_end_at=prediction.outcome_horizon_end_at,
                decision_latency_ms=prediction.decision_latency_ms,
                regime=prediction.regime,
                dependence_unit_id=prediction.dependence_unit_id,
            )
        except (ForwardPaperError, TypeError):
            invalid.append("invalid_prediction_record")
            continue
        if prediction.prediction_id in prediction_by_id:
            invalid.append("duplicate_prediction_id")
            continue
        prediction_by_id[prediction.prediction_id] = prediction
        sealed = _instant(prediction.sealed_at, name="sealed_at")
        if sealed < campaign_start or sealed > campaign_end:
            invalid.append("prediction_outside_campaign_window")
        if sealed > observed_until:
            invalid.append("prediction_after_observed_until")
        if prediction.provider_capability not in capability_counts:
            invalid.append("undeclared_provider_capability")
        else:
            capability_counts[prediction.provider_capability] += 1
        if protocol.required_regimes:
            if prediction.regime not in regime_decision_units:
                invalid.append("undeclared_regime")
            else:
                if prediction.dependence_unit_id == "UNSPECIFIED":
                    invalid.append("dependence_unit_id_unspecified")
                else:
                    prior_regime = dependence_unit_regime.get(
                        prediction.dependence_unit_id
                    )
                    if (
                        prior_regime is not None
                        and prior_regime != prediction.regime
                    ):
                        invalid.append("dependence_unit_id_regime_conflict")
                    else:
                        dependence_unit_regime[
                            prediction.dependence_unit_id
                        ] = prediction.regime
                        regime_decision_units[prediction.regime].add(
                            prediction.dependence_unit_id
                        )
        if not prediction.met_deadline:
            operational_failures.append("decision_deadline_missed")
        if prediction.decision_latency_ms > protocol.maximum_decision_latency_ms:
            operational_failures.append("decision_latency_budget_exceeded")

    if len(prediction_by_id) < protocol.minimum_predictions:
        incomplete.append("minimum_prediction_count_not_reached")
    for capability, count in capability_counts.items():
        if count == 0:
            incomplete.append(f"missing_provider_capability:{capability}")
    for regime, keys in regime_decision_units.items():
        if len(keys) < protocol.minimum_decision_units_per_regime:
            incomplete.append(
                f"minimum_decision_units_not_reached:{regime}"
            )

    outcomes_by_prediction: dict[str, ForwardOutcome] = {}
    for outcome in evidence.outcomes:
        if type(outcome) is not ForwardOutcome:
            invalid.append("invalid_outcome_record")
            continue
        try:
            outcome = ForwardOutcome.create(
                prediction_id=outcome.prediction_id,
                outcome_hash=outcome.outcome_hash,
                outcome_available_at=outcome.outcome_available_at,
                evaluated_at=outcome.evaluated_at,
            )
        except (ForwardPaperError, TypeError):
            invalid.append("invalid_outcome_record")
            continue
        if outcome.prediction_id in outcomes_by_prediction:
            invalid.append("duplicate_outcome_for_prediction")
            continue
        prediction = prediction_by_id.get(outcome.prediction_id)
        if prediction is None:
            invalid.append("outcome_without_sealed_prediction")
            continue
        outcomes_by_prediction[outcome.prediction_id] = outcome
        available = _instant(outcome.outcome_available_at, name="outcome_available_at")
        horizon = _instant(
            prediction.outcome_horizon_end_at,
            name="outcome_horizon_end_at",
        )
        if available < horizon:
            invalid.append("outcome_available_before_registered_horizon")
        evaluated = _instant(outcome.evaluated_at, name="evaluated_at")
        if available > observed_until or evaluated > observed_until:
            invalid.append("outcome_after_observed_until")

    if observed_until < campaign_end:
        incomplete.append("campaign_window_not_finished")
    for prediction_id in prediction_by_id:
        if prediction_id not in outcomes_by_prediction:
            incomplete.append("missing_forward_outcome")

    admitted_operational_observations: list[OperationalObservation] = []
    for item in evidence.operational_observations:
        if type(item) is not OperationalObservation:
            invalid.append("invalid_operational_observation")
            continue
        try:
            item = OperationalObservation.create(
                provider_capability=item.provider_capability,
                case=item.case,
                observed_at=item.observed_at,
                reconciled=item.reconciled,
            )
        except (ForwardPaperError, TypeError):
            invalid.append("invalid_operational_observation")
            continue
        admitted_operational_observations.append(item)
        if item.provider_capability not in capability_counts:
            invalid.append("operational_case_for_undeclared_capability")
        observed_at = _instant(item.observed_at, name="observed_at")
        if observed_at < campaign_start or observed_at > campaign_end:
            invalid.append("operational_case_outside_campaign_window")
        if observed_at > observed_until:
            invalid.append("operational_case_after_observed_until")
        if not item.reconciled:
            operational_failures.append("unreconciled_operational_case")

    required_cases = set(protocol.required_operational_cases)
    for capability in protocol.required_provider_capabilities:
        observed_cases = {
            item.case
            for item in admitted_operational_observations
            if item.provider_capability == capability and item.reconciled
        }
        for case in sorted(required_cases - observed_cases):
            incomplete.append(f"missing_operational_case:{capability}:{case}")

    economics_by_prediction: dict[str, PaperDecisionEconomics] = {}
    economics_by_sequence: dict[int, PaperDecisionEconomics] = {}
    for item in evidence.paper_economics:
        if type(item) is not PaperDecisionEconomics:
            invalid.append("invalid_paper_economics_record")
            continue
        try:
            item = PaperDecisionEconomics.create(
                prediction_id=item.prediction_id,
                currency=item.currency,
                sequence=item.sequence,
                realized_at=item.realized_at,
                gross_pnl=item.gross_pnl,
                fees=item.fees,
                spread_cost=item.spread_cost,
                slippage_cost=item.slippage_cost,
                net_pnl=item.net_pnl,
                equity_before=item.equity_before,
                equity_after=item.equity_after,
                peak_equity_before=item.peak_equity_before,
            )
        except (ForwardPaperError, TypeError):
            invalid.append("invalid_paper_economics_record")
            continue
        if item.prediction_id in economics_by_prediction:
            invalid.append("duplicate_paper_economics_for_prediction")
            continue
        if item.prediction_id not in prediction_by_id:
            invalid.append("paper_economics_without_sealed_prediction")
            continue
        if item.sequence in economics_by_sequence:
            invalid.append("duplicate_paper_economics_sequence")
            continue
        economics_by_prediction[item.prediction_id] = item
        economics_by_sequence[item.sequence] = item
        if item.currency != protocol.reporting_currency:
            invalid.append("paper_economics_currency_mismatch")
        realized = _instant(item.realized_at, name="realized_at")
        if realized < campaign_start or realized > observed_until:
            invalid.append("paper_economics_outside_observed_window")
        outcome = outcomes_by_prediction.get(item.prediction_id)
        if (
            outcome is not None
            and realized
            < _instant(outcome.outcome_available_at, name="outcome_available_at")
        ):
            invalid.append("paper_economics_before_outcome_available")

    ordered_economics = [
        economics_by_sequence[key] for key in sorted(economics_by_sequence)
    ]
    if ordered_economics and sorted(economics_by_sequence) != list(
        range(1, len(ordered_economics) + 1)
    ):
        invalid.append("paper_economics_sequence_gap")
    previous: PaperDecisionEconomics | None = None
    for item in ordered_economics:
        if previous is not None and _instant(
            item.realized_at,
            name="realized_at",
        ) < _instant(previous.realized_at, name="realized_at"):
            invalid.append("paper_economics_time_regression")
        if previous is None:
            if Fraction(item.peak_equity_before) != Fraction(item.equity_before):
                invalid.append("paper_equity_opening_peak_mismatch")
        else:
            if Fraction(item.equity_before) != Fraction(previous.equity_after):
                invalid.append("paper_equity_chain_break")
            expected_peak = max(
                Fraction(previous.peak_equity_before),
                Fraction(previous.equity_after),
            )
            if Fraction(item.peak_equity_before) != expected_peak:
                invalid.append("paper_peak_equity_chain_break")
        if (
            protocol.maximum_drawdown is not None
            and max(
                Fraction(0),
                Fraction(item.peak_equity_before) - Fraction(item.equity_after),
            )
            > Fraction(protocol.maximum_drawdown)
        ):
            operational_failures.append("maximum_drawdown_exceeded")
        previous = item

    for prediction_id in prediction_by_id:
        if prediction_id not in economics_by_prediction:
            incomplete.append("missing_paper_economics")

    observed_limitations = set(evidence.simulation_limitations)
    required_limitations = set(protocol.required_simulation_limitations)
    for limitation in protocol.required_simulation_limitations:
        if limitation not in observed_limitations:
            incomplete.append(f"missing_simulation_limitation:{limitation}")
    for limitation in sorted(observed_limitations - required_limitations):
        incomplete.append(f"unregistered_simulation_limitation:{limitation}")

    execution_cost = sum(
        (
            Fraction(item.fees)
            + Fraction(item.spread_cost)
            + Fraction(item.slippage_cost)
        )
        for item in economics_by_prediction.values()
    )
    reporting_ledger_cost = evidence.costs_by_currency.get(protocol.reporting_currency)
    if economics_by_prediction and reporting_ledger_cost is None:
        incomplete.append("reporting_currency_cost_ledger_missing")
    elif (
        reporting_ledger_cost is not None
        and Fraction(reporting_ledger_cost) < execution_cost
    ):
        invalid.append("paper_execution_costs_exceed_cost_ledger")

    if not evidence.costs_complete:
        incomplete.append("actual_costs_incomplete")
    elif prediction_by_id and not evidence.costs_by_currency:
        incomplete.append("actual_cost_ledger_empty")
    if not evidence.account_reconciliation_complete:
        incomplete.append("account_reconciliation_incomplete")

    invalid_reasons = tuple(dict.fromkeys(invalid))
    incomplete_reasons = tuple(dict.fromkeys(incomplete))
    failure_reasons = tuple(dict.fromkeys(operational_failures))

    if invalid_reasons:
        evidence_status = "INVALID"
        operational_status = "INCONCLUSIVE"
        reasons = invalid_reasons + incomplete_reasons + failure_reasons
    elif incomplete_reasons:
        evidence_status = "INCONCLUSIVE"
        operational_status = "FAIL" if failure_reasons else "INCONCLUSIVE"
        reasons = incomplete_reasons + failure_reasons
    else:
        evidence_status = "VALID"
        operational_status = "FAIL" if failure_reasons else "PASS"
        reasons = failure_reasons

    return ForwardPaperAssessment(
        evidence_status=evidence_status,
        operational_status=operational_status,
        economic_edge_status="NOT_ESTABLISHED",
        reasons=reasons,
        prediction_count=len(prediction_by_id),
        evaluated_outcome_count=len(outcomes_by_prediction),
    )
