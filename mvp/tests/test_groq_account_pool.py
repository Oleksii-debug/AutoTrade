from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.groq_account_pool import (
    DurableGroqQuotaJournal,
    GROQ_FREE_PLAN_QUOTAS_2026_10_20,
    GroqAccountCompliancePolicy,
    GroqAttemptAlreadyReserved,
    GroqModelQuota,
    GroqQuotaBoundInference,
    GroqQuotaError,
    GroqQuotaExhausted,
    sanitize_groq_rate_limit_headers,
)
from mvp.autotrade_mvp.model_call import ModelCallBinding, ModelCallObservation
from mvp.autotrade_mvp.openai_compatible_model_adapter import (
    OpenAICompatibleInferenceAdapter,
    OpenAICompatibleSettings,
    OpenAICompatibleTransportResponse,
)


NOW = datetime(2026, 10, 20, 10, 0, tzinfo=timezone.utc)
MODELS = (
    "openai/gpt-oss-120b",
    "openai/gpt-oss-20b",
    "qwen/qwen3.8-27b",
)


def quota(model: str, *, rpm=30, tpm=8000, rpd=1000, tpd=200000):
    return GroqModelQuota(model, rpm, tpm, rpd, tpd)


def journal(root: str, *, policy=None, quotas=None):
    return DurableGroqQuotaJournal(
        Path(root) / "groq-quota.db",
        policy=policy or GroqAccountCompliancePolicy(
            active_profiles=("groq-account-1",),
        ),
        quotas=quotas or tuple(quota(model) for model in MODELS),
    )


def binding(attempt: str, model: str = MODELS[0]):
    return ModelCallBinding(
        attempt_id=attempt,
        request_id=attempt,
        job_id="job-1",
        input_digest="sha256:" + "1" * 64,
        provider_id="groq",
        model_id=model,
        revision=None,
        remote=True,
        reserved_cost=Decimal(0),
        pricing_evidence_id="pricing-1",
        pricing_evidence_digest="sha256:" + "2" * 64,
        pricing_as_of="2026-10-20T10:00:00Z",
        cost_currency="USD",
        result_schema_id="text-v1",
        fallback_parent_attempt_id=None,
        fallback_index=0,
    )


def observation(model: str = MODELS[0], *, prompt=4, completion=2):
    return ModelCallObservation(
        provider_id="groq",
        model_id=model,
        revision=None,
        observed_at="2026-10-20T10:00:01Z",
        incurred_cost=Decimal(0),
        estimated_unbilled=Decimal(0),
        output={
            "text": "HOLD",
            "prompt_tokens": prompt,
            "completion_tokens": completion,
        },
        provider_response_id="response-1",
        usage_id="groq-usage-1",
    )


class GroqAccountComplianceTests(unittest.TestCase):
    def test_dated_owner_quota_policy_has_three_independent_model_limits(self):
        self.assertEqual(
            {item.model_id for item in GROQ_FREE_PLAN_QUOTAS_2026_10_20},
            set(MODELS),
        )
        for item in GROQ_FREE_PLAN_QUOTAS_2026_10_20:
            self.assertEqual(
                (
                    item.requests_per_minute,
                    item.tokens_per_minute,
                    item.requests_per_day,
                    item.tokens_per_day,
                ),
                (30, 8000, 1000, 200000),
            )

    def test_default_activates_four_explicit_separately_metered_aliases(self):
        policy = GroqAccountCompliancePolicy()
        self.assertEqual(policy.active_profiles, policy.stored_profiles)
        self.assertEqual(len(policy.stored_profiles), 4)
        self.assertNotIn("gsk_", repr(policy))

    def test_active_profiles_must_be_unique_stored_aliases(self):
        with self.assertRaisesRegex(ValueError, "must be stored"):
            GroqAccountCompliancePolicy(
                stored_profiles=("groq-account-1",),
                active_profiles=("groq-account-2",),
            )
        with self.assertRaisesRegex(ValueError, "nonempty and unique"):
            GroqAccountCompliancePolicy(
                active_profiles=("groq-account-1", "groq-account-1"),
            )
        policy = GroqAccountCompliancePolicy(
            active_profiles=("groq-account-1", "groq-account-2"),
        )
        self.assertEqual(len(policy.active_profiles), 2)


class DurableGroqQuotaJournalTests(unittest.TestCase):
    def test_three_model_quotas_are_independent_on_one_active_profile(self):
        with TemporaryDirectory() as root:
            book = journal(root)
            for index, model in enumerate(MODELS):
                reservation = book.reserve(
                    attempt_id=f"attempt-{index}",
                    model_id=model,
                    maximum_input_tokens=100,
                    maximum_output_tokens=100,
                    now=NOW,
                )
                self.assertEqual(reservation.profile, "groq-account-1")
                snapshot = book.snapshot(
                    profile="groq-account-1", model_id=model, now=NOW,
                )
                self.assertEqual(snapshot.minute_requests_remaining, 29)
                self.assertEqual(snapshot.day_tokens_remaining, 199800)

    def test_minute_and_day_capacity_reset_in_separate_windows(self):
        limits = (quota(MODELS[0], rpm=1, tpm=10, rpd=2, tpd=20),)
        with TemporaryDirectory() as root:
            book = journal(root, quotas=limits)
            book.reserve(
                attempt_id="attempt-a", model_id=MODELS[0],
                maximum_input_tokens=5, maximum_output_tokens=5, now=NOW,
            )
            with self.assertRaises(GroqQuotaExhausted):
                book.reserve(
                    attempt_id="attempt-b", model_id=MODELS[0],
                    maximum_input_tokens=5, maximum_output_tokens=5, now=NOW,
                )
            book.reserve(
                attempt_id="attempt-b", model_id=MODELS[0],
                maximum_input_tokens=5, maximum_output_tokens=5,
                now=NOW + timedelta(minutes=1),
            )
            with self.assertRaises(GroqQuotaExhausted):
                book.reserve(
                    attempt_id="attempt-c", model_id=MODELS[0],
                    maximum_input_tokens=1, maximum_output_tokens=1,
                    now=NOW + timedelta(minutes=2),
                )
            book.reserve(
                attempt_id="attempt-c", model_id=MODELS[0],
                maximum_input_tokens=1, maximum_output_tokens=1,
                now=NOW + timedelta(days=1),
            )

    def test_settlement_releases_unused_reservation_and_survives_restart(self):
        limits = (quota(MODELS[0], rpm=3, tpm=10, rpd=3, tpd=10),)
        with TemporaryDirectory() as root:
            book = journal(root, quotas=limits)
            book.reserve(
                attempt_id="attempt-a", model_id=MODELS[0],
                maximum_input_tokens=4, maximum_output_tokens=4, now=NOW,
            )
            book.settle(
                attempt_id="attempt-a", prompt_tokens=1,
                completion_tokens=1, now=NOW,
            )
            reopened = journal(root, quotas=limits)
            snapshot = reopened.snapshot(
                profile="groq-account-1", model_id=MODELS[0], now=NOW,
            )
            self.assertEqual(snapshot.minute_tokens_remaining, 8)
            with self.assertRaises(GroqAttemptAlreadyReserved):
                reopened.reserve(
                    attempt_id="attempt-a", model_id=MODELS[0],
                    maximum_input_tokens=4, maximum_output_tokens=4, now=NOW,
                )
            reopened.settle(
                attempt_id="attempt-a", prompt_tokens=1,
                completion_tokens=1, now=NOW,
            )

    def test_concurrent_reservations_cannot_overbook(self):
        limits = (quota(MODELS[0], rpm=3, tpm=30, rpd=3, tpd=30),)
        with TemporaryDirectory() as root:
            book = journal(root, quotas=limits)

            def reserve(index):
                try:
                    book.reserve(
                        attempt_id=f"attempt-{index}", model_id=MODELS[0],
                        maximum_input_tokens=5, maximum_output_tokens=5, now=NOW,
                    )
                    return True
                except GroqQuotaExhausted:
                    return False

            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(reserve, range(12)))
            self.assertEqual(sum(results), 3)
            snapshot = book.snapshot(
                profile="groq-account-1", model_id=MODELS[0], now=NOW,
            )
            self.assertEqual(snapshot.minute_requests_remaining, 0)
            self.assertEqual(snapshot.minute_tokens_remaining, 0)

    def test_concurrent_same_attempt_has_one_durable_pre_send_winner(self):
        with TemporaryDirectory() as root:
            book = journal(root)

            def reserve(_index):
                try:
                    book.reserve(
                        attempt_id="same-attempt", model_id=MODELS[0],
                        maximum_input_tokens=5, maximum_output_tokens=5, now=NOW,
                    )
                    return "winner"
                except GroqAttemptAlreadyReserved:
                    return "existing"

            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(reserve, range(8)))
            self.assertEqual(results.count("winner"), 1)
            self.assertEqual(results.count("existing"), 7)
            snapshot = book.snapshot(
                profile="groq-account-1", model_id=MODELS[0], now=NOW,
            )
            self.assertEqual(snapshot.minute_requests_remaining, 29)
            self.assertEqual(snapshot.minute_tokens_remaining, 7990)

    def test_pool_rotates_across_independently_metered_profiles(self):
        policy = GroqAccountCompliancePolicy(
            active_profiles=("groq-account-1", "groq-account-2"),
        )
        limits = (quota(MODELS[0], rpm=1, tpm=10, rpd=1, tpd=10),)
        with TemporaryDirectory() as root:
            book = journal(root, policy=policy, quotas=limits)
            first = book.reserve(
                attempt_id="attempt-a", model_id=MODELS[0],
                maximum_input_tokens=5, maximum_output_tokens=5, now=NOW,
            )
            second = book.reserve(
                attempt_id="attempt-b", model_id=MODELS[0],
                maximum_input_tokens=5, maximum_output_tokens=5, now=NOW,
            )
            self.assertEqual(first.profile, "groq-account-1")
            self.assertEqual(second.profile, "groq-account-2")

    def test_owner_default_uses_all_four_without_cross_profile_overrun(self):
        limits = (quota(MODELS[0], rpm=1, tpm=10, rpd=1, tpd=10),)
        with TemporaryDirectory() as root:
            book = journal(
                root,
                policy=GroqAccountCompliancePolicy(),
                quotas=limits,
            )
            selected = [
                book.reserve(
                    attempt_id=f"attempt-{index}", model_id=MODELS[0],
                    maximum_input_tokens=5, maximum_output_tokens=5, now=NOW,
                ).profile
                for index in range(4)
            ]
            self.assertEqual(
                selected,
                [f"groq-account-{index}" for index in range(1, 5)],
            )
            with self.assertRaises(GroqQuotaExhausted):
                book.reserve(
                    attempt_id="attempt-fifth", model_id=MODELS[0],
                    maximum_input_tokens=1, maximum_output_tokens=1, now=NOW,
                )

    def test_durable_configuration_conflict_fails_closed(self):
        with TemporaryDirectory() as root:
            journal(root)
            with self.assertRaisesRegex(GroqQuotaError, "configuration conflicts"):
                journal(root, quotas=(quota(MODELS[0], tpd=199999),))

    def test_provider_headers_are_sanitized_diagnostic_not_admission_authority(self):
        with TemporaryDirectory() as root:
            book = journal(root)
            book.reserve(
                attempt_id="attempt-headers", model_id=MODELS[0],
                maximum_input_tokens=10, maximum_output_tokens=10, now=NOW,
            )
            observed = book.record_provider_headers(
                attempt_id="attempt-headers",
                headers={
                    "X-RateLimit-Limit-Requests": "1000",
                    "x-ratelimit-remaining-requests": "999",
                    "x-ratelimit-reset-requests": "2m59.56s",
                    "x-ratelimit-limit-tokens": "200000",
                    "x-ratelimit-remaining-tokens": "199980",
                    "x-ratelimit-reset-tokens": "23h59m",
                    "authorization": "Bearer gsk_must-be-discarded",
                    "x-ratelimit-secret": "gsk_must-be-discarded",
                },
                observed_at=NOW,
            )
            self.assertEqual(observed.remaining_tokens, 199980)
            snapshot = book.snapshot(
                profile="groq-account-1", model_id=MODELS[0], now=NOW,
            )
            self.assertEqual(snapshot.day_tokens_remaining, 199980)
            self.assertEqual(snapshot.provider_observation.remaining_tokens, 199980)
            self.assertNotIn(b"gsk_", Path(book.path).read_bytes())

            # A provider-reported zero is diagnostic only: local capacity remains.
            isolated = sanitize_groq_rate_limit_headers(
                {"x-ratelimit-remaining-tokens": "0"}, observed_at=NOW,
            )
            self.assertEqual(isolated.remaining_tokens, 0)
            self.assertEqual(snapshot.minute_requests_remaining, 29)


class GroqQuotaBoundInferenceTests(unittest.TestCase):
    def test_real_adapter_envelope_flows_sanitized_headers_into_quota_snapshot(self):
        with TemporaryDirectory() as root:
            book = journal(root)
            prompt = "Observe"
            settings = OpenAICompatibleSettings(
                provider_id="groq",
                profile="groq-account-1",
                model_id=MODELS[0],
                maximum_input_bytes=128,
                maximum_response_bytes=4096,
                maximum_output_tokens=8,
                input_usd_per_million=Decimal(0),
                output_usd_per_million=Decimal(0),
            )
            adapter = OpenAICompatibleInferenceAdapter(
                settings,
                key_resolver=lambda **_scope: "test-only-key",
                transport=lambda *_args: OpenAICompatibleTransportResponse(
                    {
                        "id": "response-1",
                        "usage": {"prompt_tokens": 4, "completion_tokens": 2},
                        "choices": [{
                            "finish_reason": "stop",
                            "message": {"content": "HOLD"},
                        }],
                    },
                    {
                        "x-ratelimit-remaining-requests": "999",
                        "x-ratelimit-remaining-tokens": "199994",
                        "x-ratelimit-reset-tokens": "23h59m",
                    },
                ),
            )
            wrapper = GroqQuotaBoundInference(
                journal=book,
                model_id=MODELS[0],
                prompt=prompt,
                maximum_output_tokens=8,
                calls_by_profile={
                    "groq-account-1": lambda selected, cancelled: adapter.inference(
                        selected, cancelled, prompt=prompt,
                    )
                },
                clock=lambda: NOW,
            )
            selected = replace(
                binding("attempt-real-envelope"),
                input_digest="sha256:" + sha256(prompt.encode()).hexdigest(),
            )
            result = wrapper(selected, lambda: False)
            self.assertEqual(
                result.output["provider_rate_limits"]["remaining_tokens"], 199994,
            )
            snapshot = book.snapshot(
                profile="groq-account-1", model_id=MODELS[0], now=NOW,
            )
            self.assertEqual(snapshot.provider_observation.remaining_requests, 999)

    def test_success_settles_actual_usage_and_calls_once(self):
        with TemporaryDirectory() as root:
            book = journal(root)
            calls = []
            wrapper = GroqQuotaBoundInference(
                journal=book,
                model_id=MODELS[0],
                prompt="Observe",
                maximum_output_tokens=8,
                calls_by_profile={
                    "groq-account-1": lambda selected, cancelled: (
                        calls.append(selected.attempt_id) or observation()
                    ),
                },
                clock=lambda: NOW,
            )
            result = wrapper(binding("attempt-success"), lambda: False)
            self.assertEqual(result.output["text"], "HOLD")
            self.assertEqual(calls, ["attempt-success"])
            snapshot = book.snapshot(
                profile="groq-account-1", model_id=MODELS[0], now=NOW,
            )
            self.assertEqual(snapshot.minute_tokens_remaining, 7994)

    def test_failure_is_ambiguous_and_same_attempt_is_never_retried(self):
        with TemporaryDirectory() as root:
            book = journal(root)
            calls = []

            def fail(selected, cancelled):
                calls.append(selected.attempt_id)
                raise RuntimeError("transport failed")

            wrapper = GroqQuotaBoundInference(
                journal=book,
                model_id=MODELS[0],
                prompt="Observe",
                maximum_output_tokens=8,
                calls_by_profile={"groq-account-1": fail},
                clock=lambda: NOW,
            )
            with self.assertRaisesRegex(RuntimeError, "transport failed"):
                wrapper(binding("attempt-failed"), lambda: False)
            with self.assertRaises(GroqAttemptAlreadyReserved):
                wrapper(binding("attempt-failed"), lambda: False)
            self.assertEqual(calls, ["attempt-failed"])
            snapshot = book.snapshot(
                profile="groq-account-1", model_id=MODELS[0], now=NOW,
            )
            self.assertEqual(snapshot.minute_tokens_remaining, 7985)

    def test_no_secret_value_is_persisted(self):
        with TemporaryDirectory() as root:
            book = journal(root)
            secret = "gsk_test-secret-must-not-persist"
            wrapper = GroqQuotaBoundInference(
                journal=book,
                model_id=MODELS[0],
                prompt="Observe",
                maximum_output_tokens=8,
                calls_by_profile={
                    "groq-account-1": lambda *_args: observation(),
                },
                clock=lambda: NOW,
            )
            wrapper(binding("attempt-secret-check"), lambda: False)
            rendered = Path(book.path).read_bytes()
            self.assertNotIn(secret.encode(), rendered)
            self.assertNotIn(b"gsk_", rendered)

    def test_raw_response_headers_are_removed_from_returned_observation(self):
        with TemporaryDirectory() as root:
            book = journal(root)

            def call(*_args):
                result = observation()
                result.output["provider_rate_limit_headers"] = {
                    "x-ratelimit-remaining-tokens": "199994",
                    "authorization": "Bearer gsk_not-returned",
                }
                return result

            wrapper = GroqQuotaBoundInference(
                journal=book,
                model_id=MODELS[0],
                prompt="Observe",
                maximum_output_tokens=8,
                calls_by_profile={"groq-account-1": call},
                clock=lambda: NOW,
            )
            result = wrapper(binding("attempt-header-output"), lambda: False)
            self.assertNotIn("provider_rate_limit_headers", result.output)
            self.assertEqual(
                result.output["provider_rate_limits"]["remaining_tokens"], 199994,
            )
            self.assertFalse(result.output["provider_rate_limits"]["authoritative"])
            self.assertNotIn("gsk_", repr(result.output))


if __name__ == "__main__":
    unittest.main()
