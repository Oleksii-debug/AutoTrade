"""Closed registry for remote text-model providers supported by AutoTrade.

The registry is intentionally data-only.  Hosts and paths are source-owned so
configuration, a model response, or a stored credential cannot redirect an AI
request.  Every entry uses the bearer-authenticated OpenAI chat-completions
wire shape exercised by :mod:`openai_compatible_model_adapter`.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True, slots=True)
class ModelProviderEndpoint:
    provider_id: str
    host: str
    chat_path: str
    models_path: str


_ENDPOINTS = {
    "groq": ModelProviderEndpoint(
        "groq", "api.groq.com", "/openai/v1/chat/completions", "/openai/v1/models",
    ),
    "cerebras": ModelProviderEndpoint(
        "cerebras", "api.cerebras.ai", "/v1/chat/completions", "/v1/models",
    ),
    "deepseek": ModelProviderEndpoint(
        "deepseek", "api.deepseek.com", "/chat/completions", "/models",
    ),
    "openrouter": ModelProviderEndpoint(
        "openrouter", "openrouter.ai", "/api/v1/chat/completions", "/api/v1/models",
    ),
    "nvidia-nim": ModelProviderEndpoint(
        "nvidia-nim", "integrate.api.nvidia.com", "/v1/chat/completions", "/v1/models",
    ),
    "sambanova": ModelProviderEndpoint(
        "sambanova", "api.sambanova.ai", "/v1/chat/completions", "/v1/models",
    ),
}

MODEL_PROVIDER_ENDPOINTS = MappingProxyType(_ENDPOINTS)
SUPPORTED_MODEL_PROVIDERS = tuple(sorted(_ENDPOINTS))
# Mistral keeps its proven dedicated adapter, but shares the credential naming
# scheme and unified hidden-input configuration tool.
SUPPORTED_CREDENTIAL_PROVIDERS = tuple(sorted((*_ENDPOINTS, "mistral")))


def model_provider_endpoint(provider_id: str) -> ModelProviderEndpoint:
    """Return an exact registry entry; arbitrary endpoints are never accepted."""

    if type(provider_id) is not str:
        raise ValueError("AI provider is unsupported")
    try:
        return MODEL_PROVIDER_ENDPOINTS[provider_id]
    except KeyError:
        raise ValueError("AI provider is unsupported") from None


@dataclass(frozen=True, slots=True)
class ModelProviderConfiguration:
    """Explicit remote-provider enablement; the empty/default config is local-only.

    This configuration owns no endpoint, credential, routing priority, retry
    order, or trading authority.  It can only opt named entries from the
    source-owned registry into adapter construction.  The existing
    :class:`model_gateway.RoutingPolicy` remains the sole model router.
    """

    enabled_remote_providers: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        values = self.enabled_remote_providers
        if type(values) is not tuple:
            raise TypeError("enabled remote providers must be a tuple")
        if any(type(value) is not str for value in values):
            raise ValueError("AI provider is unsupported")
        if len(values) != len(set(values)):
            raise ValueError("enabled remote providers must not contain duplicates")
        for value in values:
            model_provider_endpoint(value)
        # Provider order is not fallback or selection authority.
        object.__setattr__(self, "enabled_remote_providers", tuple(sorted(values)))

    def allows(self, provider_id: str) -> bool:
        endpoint = model_provider_endpoint(provider_id)
        return endpoint.provider_id in self.enabled_remote_providers
