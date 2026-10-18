from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from decimal import Decimal
from hashlib import sha256
import io
import json
import unittest
from unittest.mock import patch

from autotrade_mvp.model_call import ModelCallBinding
from autotrade_mvp.model_credentials import _target
from autotrade_mvp.model_provider_registry import (
    MODEL_PROVIDER_ENDPOINTS,
    ModelProviderConfiguration,
    SUPPORTED_CREDENTIAL_PROVIDERS,
    SUPPORTED_MODEL_PROVIDERS,
    model_provider_endpoint,
)
from autotrade_mvp.openai_compatible_model_adapter import (
    OpenAICompatibleAdapterError,
    OpenAICompatibleInferenceAdapter,
    OpenAICompatibleModelRegistration,
    OpenAICompatibleSettings,
)


def _settings(provider: str = "groq") -> OpenAICompatibleSettings:
    return OpenAICompatibleSettings(
        provider_id=provider,
        profile="owner",
        model_id="vendor/model-1",
        maximum_input_bytes=128,
        maximum_response_bytes=4096,
        maximum_output_tokens=8,
        input_usd_per_million=Decimal("1"),
        output_usd_per_million=Decimal("2"),
        timeout_seconds=9,
    )


def _binding(settings: OpenAICompatibleSettings) -> ModelCallBinding:
    return ModelCallBinding(
        attempt_id="attempt-1",
        request_id="request-1",
        job_id="job-1",
        input_digest="sha256:" + sha256(b"Observe").hexdigest(),
        provider_id=settings.provider_id,
        model_id=settings.model_id,
        revision=None,
        remote=True,
        reserved_cost=settings.cost_ceiling_usd,
        pricing_evidence_id="pricing-1",
        pricing_evidence_digest="sha256:" + "1" * 64,
        pricing_as_of="2026-10-18T00:00:00Z",
        cost_currency="USD",
        result_schema_id="text-v1",
        fallback_parent_attempt_id=None,
        fallback_index=0,
    )


class ProviderRegistryTests(unittest.TestCase):
    def test_registry_is_closed_and_uses_exact_https_hosts_and_paths(self):
        self.assertEqual(
            set(SUPPORTED_MODEL_PROVIDERS),
            {"groq", "cerebras", "deepseek", "openrouter", "nvidia-nim", "sambanova"},
        )
        self.assertEqual(
            set(SUPPORTED_CREDENTIAL_PROVIDERS),
            set(SUPPORTED_MODEL_PROVIDERS) | {"mistral"},
        )
        expected = {
            "groq": ("api.groq.com", "/openai/v1/chat/completions"),
            "cerebras": ("api.cerebras.ai", "/v1/chat/completions"),
            "deepseek": ("api.deepseek.com", "/chat/completions"),
            "openrouter": ("openrouter.ai", "/api/v1/chat/completions"),
            "nvidia-nim": ("integrate.api.nvidia.com", "/v1/chat/completions"),
            "sambanova": ("api.sambanova.ai", "/v1/chat/completions"),
        }
        for provider, address in expected.items():
            endpoint = model_provider_endpoint(provider)
            self.assertEqual((endpoint.host, endpoint.chat_path), address)
            self.assertTrue(endpoint.models_path.startswith("/"))
        with self.assertRaises(ValueError):
            model_provider_endpoint("https://attacker.example")
        with self.assertRaises(TypeError):
            MODEL_PROVIDER_ENDPOINTS["attacker"] = object()

    def test_remote_registration_is_empty_by_default_and_order_has_no_authority(self):
        self.assertEqual(
            ModelProviderConfiguration().enabled_remote_providers,
            (),
        )
        configured = ModelProviderConfiguration(("sambanova", "groq"))
        self.assertEqual(
            configured.enabled_remote_providers,
            ("groq", "sambanova"),
        )
        self.assertTrue(configured.allows("groq"))
        with self.assertRaises(ValueError):
            ModelProviderConfiguration(("groq", "groq"))
        with self.assertRaises(ValueError):
            ModelProviderConfiguration(("attacker",))

    def test_credential_target_is_provider_and_profile_scoped_with_mistral_compatible(self):
        self.assertEqual(_target("mistral", "owner"), "AutoTrade/AI/v1/mistral/owner")
        self.assertEqual(_target("groq", "owner"), "AutoTrade/AI/v1/groq/owner")
        self.assertNotEqual(_target("groq", "owner"), _target("groq", "research"))
        with self.assertRaises(ValueError):
            _target("unknown", "owner")
        with self.assertRaises(ValueError):
            _target("groq", "../owner")


class OpenAICompatibleAdapterTests(unittest.TestCase):
    def test_registration_requires_explicit_provider_opt_in(self):
        with self.assertRaisesRegex(
            OpenAICompatibleAdapterError,
            "not enabled",
        ):
            OpenAICompatibleModelRegistration(
                ModelProviderConfiguration(),
                _settings(),
                revision=None,
                latency_ms=100,
                quality_score=Decimal("0.5"),
                key_resolver=lambda **kwargs: "secret",
                transport=lambda *args: {},
            )

    def test_each_provider_uses_only_its_fixed_endpoint_and_returns_bounded_observation(self):
        for provider in SUPPORTED_MODEL_PROVIDERS:
            with self.subTest(provider=provider):
                calls = []

                def transport(*args):
                    calls.append(args)
                    if args[1] == "GET":
                        return {"data": [{"id": "vendor/model-1"}]}
                    return {
                        "id": "response-1",
                        "usage": {"prompt_tokens": 4, "completion_tokens": 2},
                        "choices": [{
                            "finish_reason": "stop",
                            "message": {"content": "HOLD"},
                        }],
                    }

                settings = _settings(provider)
                resolved = []
                adapter = OpenAICompatibleInferenceAdapter(
                    settings,
                    key_resolver=lambda **scope: resolved.append(scope) or "private-key",
                    transport=transport,
                )
                self.assertTrue(adapter.test_connection())
                result = adapter.inference(
                    _binding(settings), lambda: False, prompt="Observe",
                )
                endpoint = model_provider_endpoint(provider)
                self.assertEqual(calls[0][0], endpoint)
                self.assertEqual(calls[0][1:3], ("GET", endpoint.models_path))
                self.assertEqual(calls[1][1:3], ("POST", endpoint.chat_path))
                self.assertEqual(calls[1][5:], (9, 4096))
                payload = json.loads(calls[1][3])
                self.assertEqual(payload["max_tokens"], 8)
                self.assertFalse(payload["stream"])
                self.assertNotIn("tools", payload)
                self.assertNotIn("tool_choice", payload)
                self.assertEqual(
                    resolved,
                    [
                        {"provider": provider, "profile": "owner"},
                        {"provider": provider, "profile": "owner"},
                    ],
                )
                self.assertEqual(result.provider_id, provider)
                self.assertEqual(result.estimated_unbilled, Decimal("0.000008"))
                self.assertEqual(result.incurred_cost, Decimal(0))
                self.assertEqual(result.output["text"], "HOLD")
                self.assertNotIn("private-key", repr(result))

    def test_binding_limits_and_cancellation_fail_before_transport(self):
        settings = _settings()
        calls = []
        adapter = OpenAICompatibleInferenceAdapter(
            settings,
            key_resolver=lambda **kwargs: "secret",
            transport=lambda *args: calls.append(args),
        )
        for binding in (
            replace(_binding(settings), provider_id="deepseek"),
            replace(_binding(settings), remote=False),
            replace(_binding(settings), reserved_cost=Decimal(0)),
            replace(_binding(settings), input_digest="sha256:" + "0" * 64),
        ):
            with self.assertRaises(OpenAICompatibleAdapterError):
                adapter.inference(binding, lambda: False, prompt="Observe")
        with self.assertRaises(OpenAICompatibleAdapterError):
            adapter.inference(_binding(settings), lambda: True, prompt="Observe")
        with self.assertRaises(ValueError):
            adapter.inference(_binding(settings), lambda: False, prompt="x" * 129)
        self.assertEqual(calls, [])

    def test_tool_output_and_usage_outside_limits_are_never_accepted(self):
        settings = _settings()
        responses = (
            {
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                "choices": [{
                    "finish_reason": "tool_calls",
                    "message": {"content": "", "tool_calls": [{"name": "trade"}]},
                }],
            },
            {
                "usage": {"prompt_tokens": 1, "completion_tokens": 9},
                "choices": [{"message": {"content": "HOLD"}}],
            },
            {
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                "choices": [{
                    "message": {
                        "content": "",
                        "function_call": {"name": "trade", "arguments": "{}"},
                    },
                }],
            },
            {
                "usage": {"prompt_tokens": 1_000_000, "completion_tokens": 1},
                "choices": [{"message": {"content": "HOLD"}}],
            },
        )
        for response in responses:
            adapter = OpenAICompatibleInferenceAdapter(
                settings,
                key_resolver=lambda **kwargs: "secret",
                transport=lambda *args, response=response: response,
            )
            with self.assertRaises(OpenAICompatibleAdapterError):
                adapter.inference(_binding(settings), lambda: False, prompt="Observe")

    def test_secret_bearing_callbacks_are_redacted(self):
        def resolver(**kwargs):
            raise RuntimeError("secret=private-key")

        adapter = OpenAICompatibleInferenceAdapter(
            _settings(), key_resolver=resolver, transport=lambda *args: {},
        )
        with self.assertRaises(OpenAICompatibleAdapterError) as caught:
            adapter.test_connection()
        self.assertNotIn("private-key", str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)

        adapter = OpenAICompatibleInferenceAdapter(
            _settings(), key_resolver=lambda **kwargs: "secret", transport=lambda *args: {},
        )
        with self.assertRaises(OpenAICompatibleAdapterError) as caught:
            adapter.inference(
                _binding(_settings()),
                lambda: (_ for _ in ()).throw(RuntimeError("private-key")),
                prompt="Observe",
            )
        self.assertNotIn("private-key", str(caught.exception))

    def test_settings_reject_arbitrary_endpoint_and_unbounded_resources(self):
        with self.assertRaises(ValueError):
            _settings("unknown")
        with self.assertRaises(ValueError):
            replace(_settings(), timeout_seconds=61)
        with self.assertRaises(ValueError):
            replace(_settings(), maximum_response_bytes=2 * 1024 * 1024 + 1)
        with self.assertRaises(ValueError):
            replace(_settings(), maximum_output_tokens=8193)

    def test_hidden_input_configure_tool_does_not_accept_or_echo_key(self):
        from tools import configure_model_provider

        recorded = []
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            patch.object(
                configure_model_provider, "getpass", return_value="private-key-value",
            ),
            patch.object(
                configure_model_provider,
                "store_model_key",
                side_effect=lambda **kwargs: recorded.append(kwargs),
            ),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            self.assertEqual(
                configure_model_provider.main(
                    ["store", "--provider", "groq", "--profile", "owner"],
                ),
                0,
            )
            with self.assertRaises(SystemExit) as rejected:
                configure_model_provider.main(
                    ["store", "--provider", "groq", "--key", "private-key-value"],
                )
        self.assertEqual(
            recorded,
            [{"provider": "groq", "profile": "owner", "key": "private-key-value"}],
        )
        self.assertEqual(rejected.exception.code, 2)
        self.assertNotIn("private-key-value", stdout.getvalue())
        self.assertNotIn("private-key-value", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
