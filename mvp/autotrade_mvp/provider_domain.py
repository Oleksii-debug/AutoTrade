"""Shared immutable provider financial-domain identity.

This module defines canonical identity only.  It does not select a route,
authenticate provider evidence, qualify an adapter/build, or issue trading
capability.  Product-owned provider composition supplies the accepted values;
financial consumers retain the resulting scope/digest without independently
normalizing runtime/provider environments.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import re

from .persistence import canonical_json


_SCHEMA_VERSION = "1.0.0"
_TOKEN_RE = re.compile(r"^[A-Z0-9][A-Z0-9_.:-]{0,127}$")
_RUNTIME_ENVIRONMENTS = frozenset({"SIMULATION", "PAPER", "LIVE"})


class ProviderDomainError(ValueError):
    """Raised when provider financial-domain identity is non-canonical."""


def _token(value: object, *, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ProviderDomainError(f"{name} must be canonical non-empty text")
    canonical = value.upper()
    if _TOKEN_RE.fullmatch(canonical) is None:
        raise ProviderDomainError(f"{name} contains unsupported characters or length")
    return canonical


@dataclass(frozen=True, slots=True)
class ProviderFinancialScope:
    """Exact provider/runtime/provider-environment/entity-policy identity."""

    provider_id: str
    runtime_environment: str
    provider_environment: str
    entity_policy_id: str

    def __post_init__(self) -> None:
        provider_id = _token(self.provider_id, name="provider_id")
        runtime_environment = _token(
            self.runtime_environment,
            name="runtime_environment",
        )
        provider_environment = _token(
            self.provider_environment,
            name="provider_environment",
        )
        entity_policy_id = _token(
            self.entity_policy_id,
            name="entity_policy_id",
        )
        if runtime_environment not in _RUNTIME_ENVIRONMENTS:
            raise ProviderDomainError("runtime_environment is unsupported")
        object.__setattr__(self, "provider_id", provider_id)
        object.__setattr__(self, "runtime_environment", runtime_environment)
        object.__setattr__(self, "provider_environment", provider_environment)
        object.__setattr__(self, "entity_policy_id", entity_policy_id)

    @property
    def environment(self) -> str:
        """Compatibility alias for consumers not yet renamed to runtime_environment."""
        return self.runtime_environment

    @property
    def route_policy_id(self) -> str:
        """Compatibility alias for the financially meaningful entity-policy id."""
        return self.entity_policy_id

    def payload(self) -> dict[str, str]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "provider_id": self.provider_id,
            "runtime_environment": self.runtime_environment,
            "provider_environment": self.provider_environment,
            "entity_policy_id": self.entity_policy_id,
        }

    @property
    def content_digest(self) -> str:
        digest = sha256(canonical_json(self.payload()).encode("utf-8")).hexdigest()
        return "provider-financial-scope:sha256:" + digest

    def require_exact(
        self,
        *,
        provider_id: object,
        runtime_environment: object,
        provider_environment: object,
        entity_policy_id: object,
    ) -> None:
        candidate = ProviderFinancialScope(
            provider_id=provider_id,
            runtime_environment=runtime_environment,
            provider_environment=provider_environment,
            entity_policy_id=entity_policy_id,
        )
        if candidate != self:
            raise ProviderDomainError("provider financial scope mismatch")


def provider_financial_scope(
    *,
    provider_id: object,
    environment: object,
    provider_environment: object,
    route_policy_id: object,
) -> ProviderFinancialScope:
    """Compatibility constructor delegating to the single canonical scope type.

    Older consumers named the same financial dimensions ``environment`` and
    ``route_policy_id``.  Keep those spellings as a thin facade only: identity,
    normalization and digesting remain owned exclusively by
    :class:`ProviderFinancialScope`.
    """

    return ProviderFinancialScope(
        provider_id=provider_id,
        runtime_environment=environment,
        provider_environment=provider_environment,
        entity_policy_id=route_policy_id,
    )
