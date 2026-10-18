from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from hashlib import sha256
from contextlib import redirect_stderr, redirect_stdout
import io
import unittest
from unittest.mock import patch

from autotrade_mvp.mistral_model_adapter import (
    MistralAdapterError, MistralInferenceAdapter, MistralSettings,
)
from autotrade_mvp.model_call import ModelCallBinding
from autotrade_mvp.model_credentials import _target


def _settings() -> MistralSettings:
    return MistralSettings(
        profile="owner", model_id="mistral-small-latest",
        maximum_input_bytes=128, maximum_output_tokens=16,
        input_usd_per_million=Decimal("1"),
        output_usd_per_million=Decimal("2"),
    )


def _binding(settings: MistralSettings) -> ModelCallBinding:
    return ModelCallBinding(
        attempt_id="attempt-1", request_id="request-1", job_id="job-1",
        input_digest="sha256:" + sha256(b"Observe").hexdigest(), provider_id="mistral",
        model_id=settings.model_id, revision=None, remote=True,
        reserved_cost=settings.cost_ceiling_usd,
        pricing_evidence_id="pricing-1", pricing_evidence_digest="sha256:" + "1" * 64,
        pricing_as_of="2026-10-18T00:00:00Z", cost_currency="USD",
        result_schema_id="text-v1", fallback_parent_attempt_id=None,
        fallback_index=0,
    )


class MistralAdapterTests(unittest.TestCase):
    def test_probe_and_inference_use_only_mistral_no_secret_in_result(self):
        calls = []

        def transport(method, path, body, key, timeout):
            calls.append((method, path, body, key, timeout))
            if method == "GET":
                return {"data": [{"id": "mistral-small-latest"}]}
            return {
                "id": "response-1",
                "usage": {"prompt_tokens": 4, "completion_tokens": 2},
                "choices": [{"message": {"content": "HOLD"}}],
            }

        settings = _settings()
        adapter = MistralInferenceAdapter(
            settings,
            key_resolver=lambda *, profile: "private-key-value",
            transport=transport,
        )
        self.assertTrue(adapter.test_connection())
        result = adapter.inference(_binding(settings), lambda: False, prompt="Observe")
        self.assertEqual(result.estimated_unbilled, Decimal("0.000008"))
        self.assertEqual(result.incurred_cost, Decimal(0))
        self.assertEqual(
            result.output,
            {"text": "HOLD", "prompt_tokens": 4, "completion_tokens": 2},
        )
        self.assertNotIn("private-key-value", repr(result))
        self.assertEqual(calls[0][:2], ("GET", "/v1/models"))
        self.assertEqual(calls[1][:2], ("POST", "/v1/chat/completions"))
        self.assertIn(b'"stream":false', calls[1][2])
        self.assertNotIn(b'"tools"', calls[1][2])

    def test_binding_ceiling_and_cancellation_fail_before_network(self):
        settings = _settings()
        calls = []
        adapter = MistralInferenceAdapter(
            settings,
            key_resolver=lambda *, profile: "secret",
            transport=lambda *args: calls.append(args),
        )
        with self.assertRaises(MistralAdapterError):
            adapter.inference(
                replace(_binding(settings), reserved_cost=Decimal("0")),
                lambda: False,
                prompt="Observe",
            )
        with self.assertRaises(MistralAdapterError):
            adapter.inference(_binding(settings), lambda: True, prompt="Observe")
        with self.assertRaises(ValueError):
            adapter.inference(_binding(settings), lambda: False, prompt="x" * 129)
        self.assertEqual(calls, [])

    def test_usage_exceeding_reservation_is_not_accepted_as_valid_observation(self):
        settings = _settings()
        adapter = MistralInferenceAdapter(
            settings,
            key_resolver=lambda *, profile: "secret",
            transport=lambda *args: {
                "usage": {"prompt_tokens": 100_000, "completion_tokens": 100_000},
                "choices": [{"message": {"content": "SELL"}}],
            },
        )
        with self.assertRaisesRegex(MistralAdapterError, "reserved cost"):
            adapter.inference(_binding(settings), lambda: False, prompt="Observe")

    def test_credentials_are_scoped_away_from_broker_vault(self):
        self.assertEqual(_target("mistral", "owner"), "AutoTrade/AI/v1/mistral/owner")
        with self.assertRaises(ValueError):
            _target("binance", "owner")
        with self.assertRaises(ValueError):
            _target("mistral", "../owner")

    def test_secret_bearing_resolver_error_is_redacted(self):
        def bad_resolver(*, profile):
            raise RuntimeError("key=private-key-value")

        adapter = MistralInferenceAdapter(
            _settings(), key_resolver=bad_resolver, transport=lambda *args: {}
        )
        with self.assertRaises(MistralAdapterError) as caught:
            adapter.test_connection()
        self.assertNotIn("private-key-value", str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)

    def test_interactive_setup_does_not_accept_or_echo_key(self):
        from tools import configure_mistral

        recorded = []
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            patch.object(configure_mistral, "getpass", return_value="private-key-value"),
            patch.object(
                configure_mistral,
                "store_model_key",
                side_effect=lambda **kwargs: recorded.append(kwargs),
            ),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            self.assertEqual(configure_mistral.main(["store", "--profile", "owner"]), 0)
            with self.assertRaises(SystemExit) as rejected:
                configure_mistral.main(["store", "--key", "private-key-value"])
        self.assertEqual(recorded, [{"profile": "owner", "key": "private-key-value"}])
        self.assertEqual(rejected.exception.code, 2)
        self.assertNotIn("private-key-value", stdout.getvalue())
        self.assertNotIn("private-key-value", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
