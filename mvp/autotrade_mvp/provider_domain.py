"""Cycle-free provider-domain normalization shared by capability and provider authority."""

from __future__ import annotations


class ProviderDomainError(ValueError):
    """Raised when runtime/provider environment identity is not canonical."""


_RUNTIME_ENVIRONMENTS = frozenset({"REPLAY", "SIMULATION", "PAPER", "LIVE"})
_BYBIT_PROVIDER_ENVIRONMENTS = frozenset({"MAINNET", "TESTNET", "DEMO"})


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value.strip():
        raise ProviderDomainError(f"{name} is required")
    return value.strip()


def normalize_provider_environment(
    *, provider_id: str, environment: str, provider_environment: str | None,
) -> str:
    """Return exact provider-domain identity without granting endpoint authority."""
    provider = _text(provider_id, "provider_id").upper()
    runtime_environment = _text(environment, "environment").upper()
    if runtime_environment not in _RUNTIME_ENVIRONMENTS:
        raise ProviderDomainError("environment must be REPLAY, SIMULATION, PAPER, or LIVE")
    if provider == "BYBIT":
        if provider_environment is None:
            raise ProviderDomainError("BYBIT requires explicit provider_environment")
        normalized = _text(provider_environment, "provider_environment").upper()
        if normalized not in _BYBIT_PROVIDER_ENVIRONMENTS:
            raise ProviderDomainError("BYBIT provider_environment must be MAINNET, TESTNET or DEMO")
        if (runtime_environment == "LIVE" and normalized != "MAINNET") or (
            runtime_environment == "PAPER" and normalized not in {"TESTNET", "DEMO"}
        ):
            raise ProviderDomainError("BYBIT provider_environment does not match runtime environment")
        return normalized
    normalized = runtime_environment if provider_environment is None else _text(provider_environment, "provider_environment").upper()
    if len(normalized) > 64 or any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for c in normalized):
        raise ProviderDomainError("provider_environment is not canonical")
    return normalized
