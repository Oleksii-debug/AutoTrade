"""Fail-closed provider×asset lifecycle qualification crosswalk for WP-61.

This module does not implement provider transport or financial lifecycle math. It
derives advertised product families from the canonical provider registry and
checks whether exact-build integration evidence covers the lifecycle semantics
required for those families. Passing this gate never grants trading authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Iterable, Mapping

from .provider_core import PROVIDERS, provider_definition


class CrosswalkError(ValueError):
    pass


class Lifecycle(StrEnum):
    FUTURES = "FUTURES"
    PERPETUAL = "PERPETUAL"
    OPTIONS = "OPTIONS"
    CORPORATE = "CORPORATE"


_COMMON_CASES = frozenset({"reconciliation"})
_LIFECYCLE_CASES: Mapping[Lifecycle, frozenset[str]] = {
    Lifecycle.FUTURES: frozenset({"expiry", "cross_currency_fees"}),
    Lifecycle.PERPETUAL: frozenset({"funding", "cross_currency_fees"}),
    Lifecycle.OPTIONS: frozenset(
        {"expiry", "assignment", "adjusted_contracts", "cross_currency_fees"}
    ),
    Lifecycle.CORPORATE: frozenset({"adjusted_contracts", "cross_currency_fees"}),
}

# This is the integration crosswalk itself, not a second provider registry.
# Keys must already exist in provider_core.PROVIDERS; validation below enforces it.
_PROVIDER_FAMILY_LIFECYCLES: Mapping[
    tuple[str, str], tuple[Lifecycle, ...]
] = {
    ("BYBIT", "LINEAR_DERIVATIVES"): (Lifecycle.FUTURES, Lifecycle.PERPETUAL),
    ("BYBIT", "INVERSE_DERIVATIVES"): (Lifecycle.FUTURES, Lifecycle.PERPETUAL),
    ("BYBIT", "OPTIONS"): (Lifecycle.OPTIONS,),
    ("KRAKEN", "DERIVATIVES"): (Lifecycle.FUTURES, Lifecycle.PERPETUAL),
    ("WHITEBIT", "FUTURES"): (Lifecycle.FUTURES, Lifecycle.PERPETUAL),
    ("BINANCE", "USD_M"): (Lifecycle.FUTURES, Lifecycle.PERPETUAL),
    ("BINANCE", "COIN_M"): (Lifecycle.FUTURES, Lifecycle.PERPETUAL),
    ("BINANCE", "OPTIONS"): (Lifecycle.OPTIONS,),
    ("IBKR", "FUTURES"): (Lifecycle.FUTURES,),
    ("IBKR", "OPTIONS"): (Lifecycle.OPTIONS,),
    ("IBKR", "EQUITIES"): (Lifecycle.CORPORATE,),
    ("ALPACA", "EQUITIES"): (Lifecycle.CORPORATE,),
    ("ALPACA", "OPTIONS"): (Lifecycle.OPTIONS,),
}


def _text(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CrosswalkError(f"{name} is required")
    return value.strip()


def _sha(value: str, name: str) -> str:
    normalized = _text(value, name)
    if len(normalized) != 40 or any(ch not in "0123456789abcdef" for ch in normalized):
        raise CrosswalkError(f"{name} must be a 40-character lowercase Git SHA")
    return normalized


@dataclass(frozen=True, order=True)
class CrosswalkKey:
    provider_id: str
    product_family: str
    lifecycle: Lifecycle

    def __post_init__(self) -> None:
        provider = _text(self.provider_id, "provider_id").upper()
        family = _text(self.product_family, "product_family")
        definition = provider_definition(provider)
        if family not in definition.product_families:
            raise CrosswalkError("product family is not advertised by provider registry")
        if not isinstance(self.lifecycle, Lifecycle):
            raise CrosswalkError("lifecycle must be a Lifecycle")
        allowed = _PROVIDER_FAMILY_LIFECYCLES.get((provider, family), ())
        if self.lifecycle not in allowed:
            raise CrosswalkError("lifecycle is not declared for provider product family")
        object.__setattr__(self, "provider_id", provider)
        object.__setattr__(self, "product_family", family)


@dataclass(frozen=True)
class LifecycleEvidence:
    key: CrosswalkKey
    source_sha: str
    adapter_sha: str
    cases: frozenset[str]
    reconciliation_complete: bool
    economic_units_exact: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_sha", _sha(self.source_sha, "source_sha"))
        object.__setattr__(self, "adapter_sha", _sha(self.adapter_sha, "adapter_sha"))
        normalized_cases = frozenset(_text(case, "case") for case in self.cases)
        object.__setattr__(self, "cases", normalized_cases)
        if not isinstance(self.reconciliation_complete, bool):
            raise CrosswalkError("reconciliation_complete must be boolean")
        if not isinstance(self.economic_units_exact, bool):
            raise CrosswalkError("economic_units_exact must be boolean")


@dataclass(frozen=True)
class CrosswalkVerdict:
    status: str
    missing_keys: tuple[CrosswalkKey, ...]
    invalid_keys: tuple[CrosswalkKey, ...]
    trading_authority_granted: bool = False


def advertised_lifecycle_keys() -> tuple[CrosswalkKey, ...]:
    """Return every lifecycle combination explicitly advertised by the crosswalk.

    Importantly, this function validates every pair against provider_core so the
    crosswalk cannot silently advertise a provider family absent from the
    canonical provider registry.
    """

    keys: list[CrosswalkKey] = []
    for (provider_id, product_family), lifecycles in _PROVIDER_FAMILY_LIFECYCLES.items():
        definition = PROVIDERS.get(provider_id)
        if definition is None or product_family not in definition.product_families:
            raise CrosswalkError("crosswalk drifted from canonical provider registry")
        for lifecycle in lifecycles:
            keys.append(CrosswalkKey(provider_id, product_family, lifecycle))
    return tuple(sorted(keys))


def required_cases(lifecycle: Lifecycle) -> frozenset[str]:
    if not isinstance(lifecycle, Lifecycle):
        raise CrosswalkError("lifecycle must be a Lifecycle")
    return _COMMON_CASES | _LIFECYCLE_CASES[lifecycle]


def qualify_asset_provider_crosswalk(
    evidence: Iterable[LifecycleEvidence],
    *,
    exact_source_sha: str,
    exact_adapter_shas: Mapping[tuple[str, str], str],
) -> CrosswalkVerdict:
    """Fail closed unless every advertised combination has exact complete evidence."""

    source_sha = _sha(exact_source_sha, "exact_source_sha")
    expected = advertised_lifecycle_keys()
    expected_set = set(expected)

    by_key: dict[CrosswalkKey, LifecycleEvidence] = {}
    invalid: set[CrosswalkKey] = set()

    for item in evidence:
        if not isinstance(item, LifecycleEvidence):
            raise CrosswalkError("evidence entries must be LifecycleEvidence")
        if item.key not in expected_set:
            raise CrosswalkError("evidence contains a non-advertised lifecycle combination")
        if item.key in by_key:
            raise CrosswalkError("duplicate lifecycle evidence key")
        by_key[item.key] = item

        expected_adapter = exact_adapter_shas.get(
            (item.key.provider_id, item.key.product_family)
        )
        if expected_adapter is None:
            invalid.add(item.key)
            continue
        expected_adapter = _sha(expected_adapter, "exact adapter sha")
        missing_cases = required_cases(item.key.lifecycle) - item.cases
        if (
            item.source_sha != source_sha
            or item.adapter_sha != expected_adapter
            or missing_cases
            or not item.reconciliation_complete
            or not item.economic_units_exact
        ):
            invalid.add(item.key)

    missing = tuple(sorted(expected_set - set(by_key)))
    invalid_keys = tuple(sorted(invalid))

    status = "PASS"
    if missing or invalid_keys:
        status = "INCOMPLETE"

    return CrosswalkVerdict(
        status=status,
        missing_keys=missing,
        invalid_keys=invalid_keys,
        trading_authority_granted=False,
    )
