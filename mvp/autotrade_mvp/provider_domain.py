"""Pure provider financial-domain identity shared across AutoTrade authorities.

This module creates deterministic content identity only.  A
ProviderFinancialScope is not capability, provider qualification, route
selection, credential, or send authority.  Production callers must obtain the
route-policy identity from the separately accepted route/provider-qualification
composition before using the value at a financial authority boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import re
from types import MappingProxyType
from typing import Mapping


class ProviderDomainError(ValueError):
    """Raised when provider-domain identity is not canonical."""


_SCHEMA_VERSION = 1
_RUNTIME_ENVIRONMENTS = frozenset({"REPLAY", "SIMULATION", "PAPER", "LIVE"})
_BYBIT_PROVIDER_ENVIRONMENTS = frozenset({"MAINNET", "TESTNET", "DEMO"})
_CANONICAL_TOKEN = re.compile(r"^[A-Z0-9._-]{1,64}$")
_ROUTE_POLICY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,255}$")


def _exact_text(value: object, *, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ProviderDomainError(f"{name} must be exact canonical text")
    return value


def _upper_token(value: object, *, name: str) -> str:
    text = _exact_text(value, name=name)
    normalized = text.upper()
    if not _CANONICAL_TOKEN.fullmatch(normalized):
        raise ProviderDomainError(f"{name} is not canonical")
    return normalized


def normalize_provider_environment(
    *,
    provider_id: object,
    environment: object,
    provider_environment: object | None,
) -> str:
    """Normalize provider-domain text without granting route authority.

    BYBIT is the one current shared provider whose runtime PAPER namespace is
    known to contain two distinct provider domains, TESTNET and DEMO.  It must
    therefore always be explicit here.  Other providers may use the runtime
    environment when they have no narrower provider domain; provider-specific
    route policy remains responsible for validating the accepted pairing.
    """

    provider = _upper_token(provider_id, name="provider_id")
    runtime = _upper_token(environment, name="environment")
    if runtime not in _RUNTIME_ENVIRONMENTS:
        raise ProviderDomainError(
            "environment must be REPLAY, SIMULATION, PAPER, or LIVE"
        )

    if provider == "BYBIT":
        if provider_environment is None:
            raise ProviderDomainError(
                "BYBIT requires explicit provider_environment"
            )
        normalized = _upper_token(
            provider_environment,
            name="provider_environment",
        )
        if normalized not in _BYBIT_PROVIDER_ENVIRONMENTS:
            raise ProviderDomainError(
                "BYBIT provider_environment must be MAINNET, TESTNET or DEMO"
            )
        if (
            runtime == "LIVE" and normalized != "MAINNET"
        ) or (
            runtime == "PAPER" and normalized not in {"TESTNET", "DEMO"}
        ):
            raise ProviderDomainError(
                "BYBIT provider_environment does not match runtime environment"
            )
        if runtime not in {"PAPER", "LIVE"}:
            raise ProviderDomainError(
                "BYBIT provider_environment requires PAPER or LIVE runtime"
            )
        return normalized

    if provider_environment is None:
        return runtime
    return _upper_token(
        provider_environment,
        name="provider_environment",
    )


def _route_policy_identity(value: object) -> str:
    text = _exact_text(value, name="route_policy_id")
    if not _ROUTE_POLICY.fullmatch(text):
        raise ProviderDomainError("route_policy_id is not canonical")
    return text


@dataclass(frozen=True, slots=True)
class ProviderFinancialScope:
    """Immutable provider-domain content identity; never authority by itself."""

    provider_id: str
    environment: str
    provider_environment: str
    route_policy_id: str

    def __post_init__(self) -> None:
        provider = _upper_token(self.provider_id, name="provider_id")
        runtime = _upper_token(self.environment, name="environment")
        if runtime not in _RUNTIME_ENVIRONMENTS:
            raise ProviderDomainError(
                "environment must be REPLAY, SIMULATION, PAPER, or LIVE"
            )
        if type(self.provider_environment) is not str:
            raise ProviderDomainError(
                "provider_environment must be exact canonical text"
            )
        provider_environment = normalize_provider_environment(
            provider_id=provider,
            environment=runtime,
            provider_environment=self.provider_environment,
        )
        route_policy = _route_policy_identity(self.route_policy_id)
        object.__setattr__(self, "provider_id", provider)
        object.__setattr__(self, "environment", runtime)
        object.__setattr__(
            self,
            "provider_environment",
            provider_environment,
        )
        object.__setattr__(self, "route_policy_id", route_policy)

    @property
    def payload(self) -> Mapping[str, object]:
        return MappingProxyType(
            {
                "schema_version": _SCHEMA_VERSION,
                "provider_id": self.provider_id,
                "environment": self.environment,
                "provider_environment": self.provider_environment,
                "route_policy_id": self.route_policy_id,
            }
        )

    @property
    def content_sha256(self) -> str:
        encoded = json.dumps(
            dict(self.payload),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
        return "sha256:" + sha256(encoded).hexdigest()

    def to_contract_dict(self) -> dict[str, object]:
        return {
            **dict(self.payload),
            "content_sha256": self.content_sha256,
        }


def provider_financial_scope(
    *,
    provider_id: object,
    environment: object,
    provider_environment: object,
    route_policy_id: object,
) -> ProviderFinancialScope:
    """Build one deterministic scope from explicit decision-relevant identity."""

    if provider_environment is None:
        raise ProviderDomainError(
            "financial scope requires explicit provider_environment"
        )
    return ProviderFinancialScope(
        provider_id=_upper_token(provider_id, name="provider_id"),
        environment=_upper_token(environment, name="environment"),
        provider_environment=_upper_token(
            provider_environment,
            name="provider_environment",
        ),
        route_policy_id=_route_policy_identity(route_policy_id),
    )
