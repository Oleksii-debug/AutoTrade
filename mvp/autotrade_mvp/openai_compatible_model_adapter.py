"""Dependency-free, fail-closed adapter for approved OpenAI-compatible APIs.

This is an opt-in transport behind the existing durable model-call boundary.
It has no tools, trading authority, URL override, retry loop, or implicit
fallback.  ZERO/local routing remains the product default.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import http.client
import json
import re
import ssl
from typing import Callable

from .exact_decimal import parse_bounded_exact_decimal
from .model_call import ModelCallBinding, ModelCallObservation
from .model_credentials import resolve_model_key
from .model_provider_registry import (
    ModelProviderConfiguration,
    ModelProviderEndpoint,
    model_provider_endpoint,
)
from .model_gateway import ModelDescriptor


_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,191}\Z")
_PROFILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


class OpenAICompatibleAdapterError(RuntimeError):
    """Sanitized provider failure; response bodies and prompts are omitted."""


@dataclass(frozen=True, slots=True)
class OpenAICompatibleSettings:
    provider_id: str
    profile: str
    model_id: str
    maximum_input_bytes: int
    maximum_response_bytes: int
    maximum_output_tokens: int
    input_usd_per_million: Decimal
    output_usd_per_million: Decimal
    timeout_seconds: int = 20

    def __post_init__(self) -> None:
        endpoint = model_provider_endpoint(self.provider_id)
        object.__setattr__(self, "provider_id", endpoint.provider_id)
        if type(self.profile) is not str or not _PROFILE.fullmatch(self.profile):
            raise ValueError("profile is invalid")
        if type(self.model_id) is not str or not _MODEL_ID.fullmatch(self.model_id):
            raise ValueError("model_id is invalid")
        for name, minimum, maximum in (
            ("maximum_input_bytes", 1, 250_000),
            ("maximum_response_bytes", 1, 2 * 1024 * 1024),
            ("maximum_output_tokens", 1, 8192),
            ("timeout_seconds", 1, 60),
        ):
            value = getattr(self, name)
            if type(value) is not int or not minimum <= value <= maximum:
                raise ValueError(f"{name} is outside the allowed range")
        for name in ("input_usd_per_million", "output_usd_per_million"):
            value = parse_bounded_exact_decimal(getattr(self, name))
            if value < 0:
                raise ValueError(f"{name} must be nonnegative")
            object.__setattr__(self, name, value)

    @property
    def cost_ceiling_usd(self) -> Decimal:
        # One UTF-8 input byte per input token is a deliberately conservative
        # bound.  The actual provider usage is still checked after the call.
        return (
            self.maximum_input_bytes * self.input_usd_per_million
            + self.maximum_output_tokens * self.output_usd_per_million
        ) / Decimal(1_000_000)


@dataclass(frozen=True, slots=True)
class OpenAICompatibleTransportResponse:
    """Optional transport envelope for bounded non-authoritative diagnostics."""

    payload: dict
    rate_limit_headers: dict[str, str]

    def __post_init__(self) -> None:
        if type(self.payload) is not dict or type(self.rate_limit_headers) is not dict:
            raise TypeError("AI provider transport response is invalid")
        allowed = {
            "x-ratelimit-limit-requests",
            "x-ratelimit-remaining-requests",
            "x-ratelimit-reset-requests",
            "x-ratelimit-limit-tokens",
            "x-ratelimit-remaining-tokens",
            "x-ratelimit-reset-tokens",
        }
        sanitized: dict[str, str] = {}
        for name, value in self.rate_limit_headers.items():
            if type(name) is not str or type(value) is not str:
                raise TypeError("AI provider rate-limit header is invalid")
            canonical = name.lower()
            if canonical not in allowed or len(value) > 64 or not value.isascii():
                raise ValueError("AI provider rate-limit header is invalid")
            if canonical in sanitized:
                raise ValueError("AI provider rate-limit header is duplicated")
            sanitized[canonical] = value
        object.__setattr__(self, "payload", dict(self.payload))
        object.__setattr__(self, "rate_limit_headers", sanitized)


Transport = Callable[
    [ModelProviderEndpoint, str, str, bytes | None, str, int, int],
    dict | OpenAICompatibleTransportResponse,
]


def _transport(
    endpoint: ModelProviderEndpoint,
    method: str,
    path: str,
    body: bytes | None,
    key: str,
    timeout: int,
    maximum_response_bytes: int,
) -> dict | OpenAICompatibleTransportResponse:
    if type(endpoint) is not ModelProviderEndpoint:
        raise OpenAICompatibleAdapterError("AI provider endpoint is invalid")
    allowed_paths = (endpoint.chat_path, endpoint.models_path)
    if method not in ("GET", "POST") or path not in allowed_paths:
        raise OpenAICompatibleAdapterError("AI provider request is outside its allowlist")
    connection = http.client.HTTPSConnection(
        endpoint.host,
        timeout=timeout,
        context=ssl.create_default_context(),
    )
    try:
        connection.request(
            method,
            path,
            body=body,
            headers={
                "Authorization": "Bearer " + key,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        response = connection.getresponse()
        if response.status != 200:
            raise OpenAICompatibleAdapterError(
                f"AI provider returned HTTP {response.status}"
            )
        raw = response.read(maximum_response_bytes + 1)
        if len(raw) > maximum_response_bytes:
            raise OpenAICompatibleAdapterError("AI provider response exceeds size limit")
        value = json.loads(raw)
        if type(value) is not dict:
            raise OpenAICompatibleAdapterError("AI provider response shape is invalid")
        rate_limit_headers: dict[str, str] = {}
        if endpoint.provider_id == "groq":
            allowed = {
                "x-ratelimit-limit-requests",
                "x-ratelimit-remaining-requests",
                "x-ratelimit-reset-requests",
                "x-ratelimit-limit-tokens",
                "x-ratelimit-remaining-tokens",
                "x-ratelimit-reset-tokens",
            }
            for name, header_value in response.getheaders():
                canonical = name.lower()
                if (
                    canonical in allowed
                    and canonical not in rate_limit_headers
                    and len(header_value) <= 64
                    and header_value.isascii()
                ):
                    rate_limit_headers[canonical] = header_value
        if rate_limit_headers:
            return OpenAICompatibleTransportResponse(value, rate_limit_headers)
        return value
    except OpenAICompatibleAdapterError:
        raise
    except (OSError, UnicodeError, ValueError, http.client.HTTPException):
        raise OpenAICompatibleAdapterError("AI provider request failed") from None
    finally:
        connection.close()


class OpenAICompatibleInferenceAdapter:
    def __init__(
        self,
        settings: OpenAICompatibleSettings,
        *,
        key_resolver: Callable[..., str] = resolve_model_key,
        transport: Transport = _transport,
    ) -> None:
        if (
            type(settings) is not OpenAICompatibleSettings
            or not callable(key_resolver)
            or not callable(transport)
        ):
            raise TypeError("OpenAI-compatible adapter configuration is invalid")
        self.settings = settings
        self.endpoint = model_provider_endpoint(settings.provider_id)
        self._key_resolver = key_resolver
        self._transport = transport

    def _key(self) -> str:
        try:
            key = self._key_resolver(
                provider=self.settings.provider_id,
                profile=self.settings.profile,
            )
        except Exception:
            raise OpenAICompatibleAdapterError("AI credential is unavailable") from None
        if (
            type(key) is not str
            or not key
            or len(key) > 2500
            or "\x00" in key
            or "\r" in key
            or "\n" in key
        ):
            raise OpenAICompatibleAdapterError("AI credential is invalid")
        return key

    @staticmethod
    def _cancelled(cancelled: Callable[[], bool]) -> bool:
        try:
            result = cancelled()
        except Exception:
            raise OpenAICompatibleAdapterError("model cancellation check failed") from None
        if type(result) is not bool:
            raise OpenAICompatibleAdapterError("model cancellation check is invalid")
        return result

    def test_connection(self) -> bool:
        """Read-only catalog probe; never sends a prompt or trading decision."""

        settings = self.settings
        transported = self._transport(
            self.endpoint,
            "GET",
            self.endpoint.models_path,
            None,
            self._key(),
            settings.timeout_seconds,
            settings.maximum_response_bytes,
        )
        response = (
            transported.payload
            if type(transported) is OpenAICompatibleTransportResponse
            else transported
        )
        if type(response) is not dict:
            raise OpenAICompatibleAdapterError("AI provider response shape is invalid")
        data = response.get("data")
        if type(data) is not list:
            raise OpenAICompatibleAdapterError("AI provider model catalog is invalid")
        return any(
            type(model) is dict and model.get("id") == settings.model_id
            for model in data
        )

    def inference(
        self,
        binding: ModelCallBinding,
        cancelled: Callable[[], bool],
        *,
        prompt: str,
    ) -> ModelCallObservation:
        """Invoke only with the durable STARTED binding from ``model_call``."""

        if type(binding) is not ModelCallBinding or not callable(cancelled):
            raise TypeError("durable model binding is required")
        settings = self.settings
        if (
            binding.provider_id != settings.provider_id
            or binding.model_id != settings.model_id
            or binding.remote is not True
            or binding.cost_currency != "USD"
            or binding.reserved_cost < settings.cost_ceiling_usd
        ):
            raise OpenAICompatibleAdapterError(
                "model binding or reserved ceiling is invalid"
            )
        if type(prompt) is not str or not prompt:
            raise ValueError("model prompt is invalid")
        encoded_prompt = prompt.encode("utf-8")
        if len(encoded_prompt) > settings.maximum_input_bytes:
            raise ValueError("model prompt exceeds configured input limit")
        if binding.input_digest != "sha256:" + sha256(encoded_prompt).hexdigest():
            raise OpenAICompatibleAdapterError(
                "model input digest differs from durable binding"
            )
        if self._cancelled(cancelled):
            raise OpenAICompatibleAdapterError("model call cancelled")

        request = json.dumps(
            {
                "model": settings.model_id,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": settings.maximum_output_tokens,
                "stream": False,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        transported = self._transport(
            self.endpoint,
            "POST",
            self.endpoint.chat_path,
            request,
            self._key(),
            settings.timeout_seconds,
            settings.maximum_response_bytes,
        )
        if type(transported) is OpenAICompatibleTransportResponse:
            response = transported.payload
            rate_limit_headers = transported.rate_limit_headers
        else:
            response = transported
            rate_limit_headers = {}
        if type(response) is not dict:
            raise OpenAICompatibleAdapterError("AI provider response shape is invalid")
        if self._cancelled(cancelled):
            raise OpenAICompatibleAdapterError("model call cancelled")

        usage = response.get("usage")
        if type(usage) is not dict:
            raise OpenAICompatibleAdapterError("AI provider token usage is missing")
        input_tokens = usage.get("prompt_tokens")
        output_tokens = usage.get("completion_tokens")
        if (
            type(input_tokens) is not int
            or type(output_tokens) is not int
            or input_tokens < 0
            or output_tokens < 0
            or output_tokens > settings.maximum_output_tokens
        ):
            raise OpenAICompatibleAdapterError("AI provider token usage is invalid")
        cost = (
            input_tokens * settings.input_usd_per_million
            + output_tokens * settings.output_usd_per_million
        ) / Decimal(1_000_000)
        if cost > binding.reserved_cost:
            raise OpenAICompatibleAdapterError(
                "AI provider usage exceeded reserved cost"
            )

        choices = response.get("choices")
        if type(choices) is not list or len(choices) != 1 or type(choices[0]) is not dict:
            raise OpenAICompatibleAdapterError("AI provider choices are invalid")
        choice = choices[0]
        message = choice.get("message")
        if (
            type(message) is not dict
            or type(message.get("content")) is not str
            or message.get("tool_calls") not in (None, [])
            or message.get("function_call") is not None
            or choice.get("finish_reason") == "tool_calls"
        ):
            raise OpenAICompatibleAdapterError(
                "AI provider returned unsupported executable content"
            )
        response_id = response.get("id")
        if response_id is not None and (
            type(response_id) is not str or not _MODEL_ID.fullmatch(response_id)
        ):
            raise OpenAICompatibleAdapterError("AI provider response identity is invalid")

        usage_identity = sha256(
            json.dumps(
                {
                    "attempt_id": binding.attempt_id,
                    "provider_id": settings.provider_id,
                    "prompt_tokens": input_tokens,
                    "completion_tokens": output_tokens,
                    "estimated_usd": str(cost),
                },
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        output = {
            "text": message["content"],
            "prompt_tokens": input_tokens,
            "completion_tokens": output_tokens,
        }
        if settings.provider_id == "groq" and rate_limit_headers:
            output["provider_rate_limit_headers"] = dict(rate_limit_headers)
        return ModelCallObservation(
            provider_id=settings.provider_id,
            model_id=settings.model_id,
            revision=binding.revision,
            observed_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            incurred_cost=Decimal(0),
            estimated_unbilled=cost,
            output=output,
            provider_response_id=response_id,
            usage_id=f"{settings.provider_id}-usage-sha256:{usage_identity}",
        )


class _RegisteredPromptInferenceCall:
    """Prompt-bound callback with the exact signature used by ``model_call``."""

    __slots__ = ("_adapter", "_prompt")

    def __init__(
        self,
        adapter: OpenAICompatibleInferenceAdapter,
        prompt: str,
    ) -> None:
        if type(adapter) is not OpenAICompatibleInferenceAdapter:
            raise TypeError("registered inference adapter is invalid")
        settings = adapter.settings
        if type(prompt) is not str or not prompt:
            raise ValueError("model prompt is invalid")
        if len(prompt.encode("utf-8")) > settings.maximum_input_bytes:
            raise ValueError("model prompt exceeds configured input limit")
        self._adapter = adapter
        self._prompt = prompt

    def __call__(
        self,
        binding: ModelCallBinding,
        cancelled: Callable[[], bool],
    ) -> ModelCallObservation:
        return self._adapter.inference(binding, cancelled, prompt=self._prompt)


class OpenAICompatibleModelRegistration:
    """Opt-in bridge from the source registry to the canonical model lifecycle.

    The registration supplies a conservative descriptor to ``model_gateway``
    and a prompt-bound callback to ``DurableModelCallOrchestrator.execute``. It
    deliberately owns neither policy selection nor retry/fallback ordering.
    """

    __slots__ = ("settings", "descriptor", "_adapter")

    def __init__(
        self,
        configuration: ModelProviderConfiguration,
        settings: OpenAICompatibleSettings,
        *,
        revision: str | None,
        latency_ms: int,
        quality_score: Decimal,
        key_resolver: Callable[..., str] = resolve_model_key,
        transport: Transport = _transport,
    ) -> None:
        if type(configuration) is not ModelProviderConfiguration:
            raise TypeError("model provider configuration is invalid")
        if type(settings) is not OpenAICompatibleSettings:
            raise TypeError("OpenAI-compatible adapter configuration is invalid")
        if not configuration.allows(settings.provider_id):
            raise OpenAICompatibleAdapterError(
                "AI provider is not enabled by model configuration"
            )
        adapter = OpenAICompatibleInferenceAdapter(
            settings,
            key_resolver=key_resolver,
            transport=transport,
        )
        descriptor = ModelDescriptor(
            model_id=settings.model_id,
            provider_id=settings.provider_id,
            revision=revision,
            remote=True,
            estimated_cost=settings.cost_ceiling_usd,
            latency_ms=latency_ms,
            quality_score=quality_score,
        )
        self.settings = settings
        self.descriptor = descriptor
        self._adapter = adapter

    def inference_call(self, *, prompt: str) -> Callable[
        [ModelCallBinding, Callable[[], bool]], ModelCallObservation
    ]:
        """Bind advisory input without exposing a secret or execution tool."""

        return _RegisteredPromptInferenceCall(self._adapter, prompt)
