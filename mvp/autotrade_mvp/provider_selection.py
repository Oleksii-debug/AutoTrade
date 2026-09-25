"""Deterministic provider crosswalk without implicit fallback or live authority.

This module joins the architectural provider registry, exact-code qualification
evidence and account/instrument capability snapshots. It never sends orders and
never upgrades non-live qualification into live trading authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

from .capabilities import CapabilitySnapshot
from .provider_core import QualificationEvidence, provider_definition


ASSET_FAMILY_COMPATIBILITY = {
    "CRYPTO_SPOT": {
        ("BYBIT", "SPOT"),
        ("KRAKEN", "SPOT"),
        ("WHITEBIT", "SPOT"),
        ("BINANCE", "SPOT"),
        ("ALPACA", "CRYPTO"),
    },
    "CRYPTO_MARGIN": {
        ("BYBIT", "MARGIN"),
        ("KRAKEN", "MARGIN"),
        ("WHITEBIT", "COLLATERAL"),
        ("BINANCE", "MARGIN"),
    },
    "LINEAR_PERPETUAL": {
        ("BYBIT", "LINEAR_DERIVATIVES"),
        ("KRAKEN", "DERIVATIVES"),
        ("WHITEBIT", "FUTURES"),
        ("BINANCE", "USD_M"),
    },
    "INVERSE_PERPETUAL": {
        ("BYBIT", "INVERSE_DERIVATIVES"),
        ("KRAKEN", "DERIVATIVES"),
        ("BINANCE", "COIN_M"),
    },
    "LISTED_FUTURE": {
        ("IBKR", "FUTURES"),
    },
    "EQUITY": {
        ("IBKR", "EQUITIES"),
        ("ALPACA", "EQUITIES"),
    },
    "OPTION": {
        ("BYBIT", "OPTIONS"),
        ("BINANCE", "OPTIONS"),
        ("IBKR", "OPTIONS"),
        ("ALPACA", "OPTIONS"),
    },
    "FX": {
        ("IBKR", "FX"),
    },
}


class ProviderSelectionError(ValueError):
    pass


def _text(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProviderSelectionError(f"{name} is required")
    return value.strip()


def _code_sha(value: str) -> str:
    text = _text(value, "adapter_code_sha")
    if len(text) not in {40, 64}:
        raise ProviderSelectionError("adapter_code_sha must be a 40- or 64-character hex SHA")
    try:
        int(text, 16)
    except ValueError as error:
        raise ProviderSelectionError("adapter_code_sha must be hexadecimal") from error
    if text != text.lower():
        raise ProviderSelectionError("adapter_code_sha must use canonical lowercase hex")
    return text


def _instant(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ProviderSelectionError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class ProviderRouteRequest:
    asset_class: str
    environment: str
    instrument_version: str
    order_type: str
    time_in_force: str
    permission_scope: str
    preferred_provider_id: str | None = None

    def __post_init__(self) -> None:
        asset = _text(self.asset_class, "asset_class").upper()
        if asset not in ASSET_FAMILY_COMPATIBILITY:
            raise ProviderSelectionError("asset_class has no canonical provider crosswalk")
        object.__setattr__(self, "asset_class", asset)
        environment = _text(self.environment, "environment").upper()
        if environment not in {"PAPER", "LIVE"}:
            raise ProviderSelectionError(
                "external provider selection is restricted to PAPER or LIVE"
            )
        object.__setattr__(self, "environment", environment)
        object.__setattr__(
            self, "instrument_version", _text(self.instrument_version, "instrument_version")
        )
        object.__setattr__(self, "order_type", _text(self.order_type, "order_type").upper())
        object.__setattr__(
            self, "time_in_force", _text(self.time_in_force, "time_in_force").upper()
        )
        object.__setattr__(
            self,
            "permission_scope",
            _text(self.permission_scope, "permission_scope").upper(),
        )
        if self.preferred_provider_id is not None:
            object.__setattr__(
                self,
                "preferred_provider_id",
                _text(self.preferred_provider_id, "preferred_provider_id").upper(),
            )


@dataclass(frozen=True)
class ProviderCandidate:
    provider_id: str
    product_family: str
    adapter_code_sha: str
    qualification: QualificationEvidence
    capability: CapabilitySnapshot

    def __post_init__(self) -> None:
        provider = _text(self.provider_id, "provider_id").upper()
        definition = provider_definition(provider)
        family = _text(self.product_family, "product_family")
        if family not in definition.product_families:
            raise ProviderSelectionError("product_family is not declared for provider")
        if self.qualification.provider_id != provider:
            raise ProviderSelectionError("qualification provider does not match candidate")
        if self.qualification.product_family != family:
            raise ProviderSelectionError("qualification product family does not match candidate")
        if self.capability.provider_id.upper() != provider:
            raise ProviderSelectionError("capability provider does not match candidate")
        object.__setattr__(self, "provider_id", provider)
        object.__setattr__(self, "product_family", family)
        object.__setattr__(
            self, "adapter_code_sha", _code_sha(self.adapter_code_sha)
        )


@dataclass(frozen=True)
class CandidateDecision:
    provider_id: str
    product_family: str
    eligible: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class ProviderSelection:
    status: str
    selected: ProviderCandidate | None
    eligible: tuple[ProviderCandidate, ...]
    decisions: tuple[CandidateDecision, ...]


def select_provider(
    request: ProviderRouteRequest,
    candidates: Iterable[ProviderCandidate],
    *,
    at: datetime,
) -> ProviderSelection:
    """Resolve one exact route or fail closed.

    Multiple eligible external providers are intentionally ambiguous unless a
    policy supplies an explicit preferred_provider_id. A LIVE request is never
    admitted by the current non-live qualification evidence type.
    """

    if not isinstance(request, ProviderRouteRequest):
        raise TypeError("request must be ProviderRouteRequest")
    point = _instant(at, "at")
    materialized = tuple(candidates)
    if any(not isinstance(candidate, ProviderCandidate) for candidate in materialized):
        raise TypeError("candidates must contain ProviderCandidate values")
    identities = [(c.provider_id, c.product_family) for c in materialized]
    if len(identities) != len(set(identities)):
        raise ProviderSelectionError("provider/product candidate identities must be unique")

    allowed_pairs = ASSET_FAMILY_COMPATIBILITY[request.asset_class]
    eligible: list[ProviderCandidate] = []
    decisions: list[CandidateDecision] = []

    for candidate in sorted(
        materialized, key=lambda item: (item.provider_id, item.product_family)
    ):
        reasons: list[str] = []
        pair = (candidate.provider_id, candidate.product_family)
        if pair not in allowed_pairs:
            reasons.append("ASSET_PRODUCT_MISMATCH")
        if candidate.qualification.environment.upper() != request.environment:
            reasons.append("QUALIFICATION_ENVIRONMENT_MISMATCH")
        qualification_status = candidate.qualification.status(
            now=point,
            exact_code_sha=candidate.adapter_code_sha,
        )
        if qualification_status != "QUALIFIED_FOR_NONLIVE":
            reasons.append(f"QUALIFICATION_{qualification_status}")
        if request.environment == "LIVE":
            reasons.append("LIVE_QUALIFICATION_NOT_ESTABLISHED")

        capability = candidate.capability
        if capability.environment.upper() != request.environment:
            reasons.append("CAPABILITY_ENVIRONMENT_MISMATCH")
        if capability.instrument_version != request.instrument_version:
            reasons.append("CAPABILITY_INSTRUMENT_MISMATCH")
        if not capability.admits(
            at=point,
            order_type=request.order_type,
            time_in_force=request.time_in_force,
            permission_scope=request.permission_scope,
        ):
            reasons.append("CAPABILITY_DOES_NOT_ADMIT_ACTION")

        unsupported = set(candidate.qualification.unsupported_features)
        requested_features = {
            f"ASSET_CLASS:{request.asset_class}",
            f"ORDER_TYPE:{request.order_type}",
            f"TIF:{request.time_in_force}",
            f"SCOPE:{request.permission_scope}",
        }
        if unsupported & requested_features:
            reasons.append("QUALIFICATION_EXPLICITLY_UNSUPPORTED")

        decision = CandidateDecision(
            provider_id=candidate.provider_id,
            product_family=candidate.product_family,
            eligible=not reasons,
            reasons=tuple(reasons),
        )
        decisions.append(decision)
        if not reasons:
            eligible.append(candidate)

    preferred = request.preferred_provider_id
    if preferred is not None:
        matching = [candidate for candidate in eligible if candidate.provider_id == preferred]
        if len(matching) == 1:
            return ProviderSelection(
                status="SELECTED_BY_EXPLICIT_POLICY",
                selected=matching[0],
                eligible=tuple(eligible),
                decisions=tuple(decisions),
            )
        return ProviderSelection(
            status="NO_ELIGIBLE_PREFERRED_PROVIDER",
            selected=None,
            eligible=tuple(eligible),
            decisions=tuple(decisions),
        )

    if len(eligible) == 1:
        return ProviderSelection(
            status="SELECTED_UNAMBIGUOUS",
            selected=eligible[0],
            eligible=tuple(eligible),
            decisions=tuple(decisions),
        )
    if len(eligible) > 1:
        return ProviderSelection(
            status="AMBIGUOUS_REQUIRES_POLICY",
            selected=None,
            eligible=tuple(eligible),
            decisions=tuple(decisions),
        )
    return ProviderSelection(
        status="NO_ELIGIBLE_PROVIDER",
        selected=None,
        eligible=(),
        decisions=tuple(decisions),
    )
