"""Opt-in Mistral transport for the existing durable model-call boundary.

This adapter has no trading tools or financial authority. Its token charge is
estimated from observed usage; independently authenticated billing and pricing
remain requirements of model_production_composition before production calls.
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


_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_HOST = "api.mistral.ai"
_MAX_RESPONSE = 1024 * 1024


class MistralAdapterError(RuntimeError):
    """Provider failure with no response body, secret or prompt in the message."""


@dataclass(frozen=True, slots=True)
class MistralSettings:
    profile: str
    model_id: str
    maximum_input_bytes: int
    maximum_output_tokens: int
    input_usd_per_million: Decimal
    output_usd_per_million: Decimal
    timeout_seconds: int = 20

    def __post_init__(self) -> None:
        for name in ("profile", "model_id"):
            value = getattr(self, name)
            if type(value) is not str or not _MODEL.fullmatch(value):
                raise ValueError(f"{name} is invalid")
        for name, minimum, maximum in (
            ("maximum_input_bytes", 1, 250_000),
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
        return (
            self.maximum_input_bytes * self.input_usd_per_million
            + self.maximum_output_tokens * self.output_usd_per_million
        ) / Decimal(1_000_000)


Transport = Callable[[str, str, bytes | None, str, int], dict]


def _transport(method: str, path: str, body: bytes | None,
               key: str, timeout: int) -> dict:
    # A direct HTTPS connection deliberately does not follow redirects.
    connection = http.client.HTTPSConnection(
        _HOST, timeout=timeout, context=ssl.create_default_context(),
    )
    try:
        connection.request(method, path, body=body, headers={
            "Authorization": "Bearer " + key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        })
        response = connection.getresponse()
        if response.status != 200:
            raise MistralAdapterError("Mistral returned HTTP " + str(response.status))
        raw = response.read(_MAX_RESPONSE + 1)
        if len(raw) > _MAX_RESPONSE:
            raise MistralAdapterError("Mistral response exceeds size limit")
        value = json.loads(raw)
        if type(value) is not dict:
            raise MistralAdapterError("Mistral response shape is invalid")
        return value
    except MistralAdapterError:
        raise
    except (OSError, UnicodeError, ValueError, http.client.HTTPException) as error:
        raise MistralAdapterError("Mistral request failed") from None
    finally:
        connection.close()


class MistralInferenceAdapter:
    def __init__(self, settings: MistralSettings, *,
                 key_resolver: Callable[..., str] = resolve_model_key,
                 transport: Transport = _transport) -> None:
        if type(settings) is not MistralSettings or not callable(key_resolver) or not callable(transport):
            raise TypeError("Mistral adapter configuration is invalid")
        self.settings = settings
        self._key_resolver = key_resolver
        self._transport = transport

    def _key(self) -> str:
        try:
            key = self._key_resolver(profile=self.settings.profile)
        except Exception:
            raise MistralAdapterError("AI credential is unavailable") from None
        if type(key) is not str or not key or "\r" in key or "\n" in key:
            raise MistralAdapterError("AI credential is invalid")
        return key

    def test_connection(self) -> bool:
        """Read-only authentication check; never submits a trading decision."""
        response = self._transport("GET", "/v1/models", None,
                                   self._key(),
                                   self.settings.timeout_seconds)
        data = response.get("data")
        if type(data) is not list:
            raise MistralAdapterError("Mistral model catalog is invalid")
        return any(type(model) is dict and model.get("id") == self.settings.model_id
                   for model in data)

    def inference(self, binding: ModelCallBinding,
                  cancelled: Callable[[], bool], *, prompt: str) -> ModelCallObservation:
        """Invoke only under a durable STARTED binding from model_call.py."""
        if type(binding) is not ModelCallBinding or not callable(cancelled):
            raise TypeError("durable model binding is required")
        settings = self.settings
        if (binding.provider_id != "mistral" or binding.model_id != settings.model_id
            or binding.remote is not True or binding.cost_currency != "USD"
            or binding.reserved_cost < settings.cost_ceiling_usd):
            raise MistralAdapterError("model binding or reserved ceiling is invalid")
        if type(prompt) is not str or not prompt or len(prompt.encode("utf-8")) > settings.maximum_input_bytes:
            raise ValueError("model prompt exceeds configured input limit")
        if binding.input_digest != "sha256:" + sha256(prompt.encode("utf-8")).hexdigest():
            raise MistralAdapterError("model input digest differs from durable binding")
        if cancelled():
            raise MistralAdapterError("model call cancelled")
        request = json.dumps({
            "model": settings.model_id,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": settings.maximum_output_tokens,
            "stream": False,
        }, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        response = self._transport("POST", "/v1/chat/completions", request,
                                   self._key(),
                                   settings.timeout_seconds)
        usage = response.get("usage")
        if type(usage) is not dict:
            raise MistralAdapterError("Mistral token usage is missing")
        input_tokens = usage.get("prompt_tokens")
        output_tokens = usage.get("completion_tokens")
        if (type(input_tokens) is not int or type(output_tokens) is not int
            or input_tokens < 0 or output_tokens < 0):
            raise MistralAdapterError("Mistral token usage is invalid")
        cost = (input_tokens * settings.input_usd_per_million
                + output_tokens * settings.output_usd_per_million) / Decimal(1_000_000)
        if cost > binding.reserved_cost:
            raise MistralAdapterError("Mistral usage exceeded reserved cost")
        choices = response.get("choices")
        if type(choices) is not list or len(choices) != 1 or type(choices[0]) is not dict:
            raise MistralAdapterError("Mistral choices are invalid")
        message = choices[0].get("message")
        if type(message) is not dict or type(message.get("content")) is not str:
            raise MistralAdapterError("Mistral content is invalid")
        response_id = response.get("id")
        if response_id is not None and (
            type(response_id) is not str or not _MODEL.fullmatch(response_id)
        ):
            raise MistralAdapterError("Mistral response identity is invalid")
        # The remote response can never grant tools, execution or risk authority.
        usage_identity = sha256(json.dumps({
            "attempt_id": binding.attempt_id, "prompt_tokens": input_tokens,
            "completion_tokens": output_tokens, "estimated_usd": str(cost),
        }, sort_keys=True).encode("utf-8")).hexdigest()
        return ModelCallObservation(
            provider_id="mistral", model_id=settings.model_id,
            revision=binding.revision,
            observed_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            incurred_cost=Decimal(0), estimated_unbilled=cost,
            output={"text": message["content"], "prompt_tokens": input_tokens,
                    "completion_tokens": output_tokens},
            provider_response_id=response_id,
            usage_id="mistral-usage-sha256:" + usage_identity,
        )
