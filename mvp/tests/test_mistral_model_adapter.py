from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from hashlib import sha256

import pytest

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


def test_probe_and_inference_use_only_mistral_no_secret_in_result():
    calls = []
    def transport(method, path, body, key, timeout):
        calls.append((method, path, body, key, timeout))
        if method == "GET":
            return {"data": [{"id": "mistral-small-latest"}]}
        return {
            "id": "response-1", "usage": {"prompt_tokens": 4, "completion_tokens": 2},
            "choices": [{"message": {"content": "HOLD"}}],
        }
    settings = _settings()
    adapter = MistralInferenceAdapter(settings,
        key_resolver=lambda *, profile: "private-key-value", transport=transport)
    assert adapter.test_connection() is True
    result = adapter.inference(_binding(settings), lambda: False, prompt="Observe")
    assert result.estimated_unbilled == Decimal("0.000008")
    assert result.incurred_cost == Decimal(0)
    assert result.output == {"text": "HOLD", "prompt_tokens": 4, "completion_tokens": 2}
    assert "private-key-value" not in repr(result)
    assert calls[0][:2] == ("GET", "/v1/models")
    assert calls[1][:2] == ("POST", "/v1/chat/completions")
    assert b'"stream":false' in calls[1][2]
    assert b'"tools"' not in calls[1][2]


def test_binding_ceiling_and_cancellation_fail_before_network():
    settings = _settings()
    calls = []
    adapter = MistralInferenceAdapter(settings,
        key_resolver=lambda *, profile: "secret",
        transport=lambda *args: calls.append(args))
    with pytest.raises(MistralAdapterError):
        adapter.inference(replace(_binding(settings), reserved_cost=Decimal("0")),
                          lambda: False, prompt="Observe")
    with pytest.raises(MistralAdapterError):
        adapter.inference(_binding(settings), lambda: True, prompt="Observe")
    with pytest.raises(ValueError):
        adapter.inference(_binding(settings), lambda: False, prompt="x" * 129)
    assert calls == []


def test_usage_exceeding_reservation_is_not_accepted_as_valid_observation():
    settings = _settings()
    adapter = MistralInferenceAdapter(settings,
        key_resolver=lambda *, profile: "secret",
        transport=lambda *args: {
            "usage": {"prompt_tokens": 100_000, "completion_tokens": 100_000},
            "choices": [{"message": {"content": "SELL"}}],
        })
    with pytest.raises(MistralAdapterError, match="reserved cost"):
        adapter.inference(_binding(settings), lambda: False, prompt="Observe")


def test_credentials_are_scoped_away_from_broker_vault_and_nonwindows_fails_closed():
    assert _target("mistral", "owner") == "AutoTrade/AI/v1/mistral/owner"
    with pytest.raises(ValueError):
        _target("binance", "owner")
    with pytest.raises(ValueError):
        _target("mistral", "../owner")


def test_secret_bearing_resolver_error_is_redacted():
    def bad_resolver(*, profile):
        raise RuntimeError("key=private-key-value")
    adapter = MistralInferenceAdapter(_settings(), key_resolver=bad_resolver,
                                      transport=lambda *args: {})
    with pytest.raises(MistralAdapterError) as caught:
        adapter.test_connection()
    assert "private-key-value" not in str(caught.value)
    assert caught.value.__cause__ is None


def test_interactive_setup_does_not_accept_or_echo_key(monkeypatch, capsys):
    from tools import configure_mistral
    recorded = []
    monkeypatch.setattr(configure_mistral, "getpass", lambda prompt: "private-key-value")
    monkeypatch.setattr(configure_mistral, "store_model_key",
                        lambda **kwargs: recorded.append(kwargs))
    assert configure_mistral.main(["store", "--profile", "owner"]) == 0
    assert recorded == [{"profile": "owner", "key": "private-key-value"}]
    assert "private-key-value" not in capsys.readouterr().out
    with pytest.raises(SystemExit) as rejected:
        configure_mistral.main(["store", "--key", "private-key-value"])
    assert rejected.value.code == 2
    assert "private-key-value" not in capsys.readouterr().err
