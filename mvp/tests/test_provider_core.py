from datetime import datetime, timedelta, timezone, tzinfo
from decimal import Decimal
from hashlib import sha256
import json
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.dispatch import (
    ExactJsonTransportResponse,
    GuardedDispatcher,
    load_submission_response_binding,
)
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.provider_core import (
    ClockGuard,
    PROVIDERS,
    REQUIRED_QUALIFICATION_CASES,
    ProviderCoreError,
    ProviderSubmissionObservation,
    QualificationEvidence,
    QuotaBucket,
    WriteOutcome,
    classify_write_outcome,
    observe_submission_json_response,
    provider_definition,
)


NOW = datetime(2026, 9, 24, 18, tzinfo=timezone.utc)
CODE_SHA = "a" * 40
OTHER_SHA = "b" * 40


class _NoOffsetTZ(tzinfo):
    def utcoffset(self, dt):
        return None

    def dst(self, dt):
        return None


class ProviderCoreTests(unittest.TestCase):

    def _durable_submission_binding(
        self,
        directory,
        *,
        capability_snapshot_ids=("cap-1",),
        instrument_versions=("BTCUSD:v1",),
        raw=b'{ "orderId" : "provider-1" }',
    ):
        store = JournalStore(f"{directory}/journal.sqlite3")
        dispatcher = GuardedDispatcher(
            store,
            environment="PAPER",
            account_id="acct",
            owner_token="owner",
        )
        request = {"symbol": "BTCUSD", "qty": "1"}
        request_text = json.dumps(
            request,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        request_sha = "sha256:" + sha256(request_text.encode("utf-8")).hexdigest()
        scope = {
            "endpoint": "/v5/order/create",
            "prepared_request_sha256": request_sha,
            "capability_snapshot_ids": list(capability_snapshot_ids),
            "instrument_versions": list(instrument_versions),
            "provider_environment": "TESTNET",
        }

        def transport(_client_id, _request, guard):
            guard()
            return ExactJsonTransportResponse(raw)

        outcome = dispatcher.dispatch(
            attempt_id="provider-evidence-a1",
            intent_id="intent-1",
            intent_hash="intent-hash",
            provider="BYBIT",
            request=request,
            now="2026-09-24T18:00:00Z",
            authority_check=lambda _hash, _now: (True, "allowed"),
            transport_send=transport,
            submission_scope=scope,
        )
        self.assertEqual(outcome.status, "SENT")
        return (
            load_submission_response_binding(
                store,
                environment="PAPER",
                account_id="acct",
                attempt_id="provider-evidence-a1",
            ),
            request_sha,
        )

    def test_submission_observation_requires_durable_exact_send_scope(self):
        with TemporaryDirectory() as directory:
            binding, request_sha = self._durable_submission_binding(directory)
            observation = observe_submission_json_response(
                response_binding=binding,
                provider_id="BYBIT",
                endpoint="/v5/order/create",
                prepared_request_sha256=request_sha,
                capability_snapshot_ids=("cap-1",),
                instrument_versions=("BTCUSD:v1",),
                provider_environment="TESTNET",
            )
            self.assertIsInstance(observation, ProviderSubmissionObservation)
            self.assertEqual(observation.payload["orderId"], "provider-1")
            self.assertEqual(observation.response_sha256, binding.response_sha256)
            self.assertEqual(observation.request_sha256, request_sha)
            observation.require_scope(
                provider_id="BYBIT",
                endpoint="/v5/order/create",
                prepared_request_sha256=request_sha,
                capability_snapshot_ids=("cap-1",),
                instrument_versions=("BTCUSD:v1",),
                account_id="acct",
                environment="PAPER",
                provider_environment="TESTNET",
                client_order_id=binding.client_order_id,
            )

    def test_submission_observation_rejects_scope_relabelling(self):
        with TemporaryDirectory() as directory:
            binding, request_sha = self._durable_submission_binding(directory)
            for kwargs in (
                {"provider_id": "ALPACA"},
                {"endpoint": "/other"},
                {"prepared_request_sha256": "sha256:" + "0" * 64},
                {"capability_snapshot_ids": ("cap-2",)},
                {"instrument_versions": ("ETHUSD:v1",)},
                {"provider_environment": "DEMO"},
            ):
                values = {
                    "response_binding": binding,
                    "provider_id": "BYBIT",
                    "endpoint": "/v5/order/create",
                    "prepared_request_sha256": request_sha,
                    "capability_snapshot_ids": ("cap-1",),
                    "instrument_versions": ("BTCUSD:v1",),
                    "provider_environment": "TESTNET",
                }
                values.update(kwargs)
                with self.subTest(kwargs=kwargs), self.assertRaises(ProviderCoreError):
                    observe_submission_json_response(**values)

    def test_submission_observation_cannot_be_constructed_directly(self):
        with TemporaryDirectory() as directory:
            binding, _request_sha = self._durable_submission_binding(directory)
            with self.assertRaisesRegex(
                ProviderCoreError,
                "durable exact response binding",
            ):
                ProviderSubmissionObservation(
                    response_binding=binding,
                    endpoint="/v5/order/create",
                    capability_snapshot_ids=("cap-1",),
                    instrument_versions=("BTCUSD:v1",),
                    evidence_ref="provider-write:sha256:" + "1" * 64,
                    payload={"orderId": "forged"},
                )

    def test_all_six_architectural_provider_targets_exist(self):
        self.assertEqual(
            set(PROVIDERS),
            {"BYBIT", "KRAKEN", "WHITEBIT", "BINANCE", "IBKR", "ALPACA"},
        )
        for provider in PROVIDERS.values():
            self.assertTrue(provider.product_families)
            self.assertTrue(provider.surfaces)

    def test_product_families_are_not_silently_interchangeable(self):
        self.assertIn("OPTIONS", provider_definition("bybit").product_families)
        self.assertIn("USD_M", provider_definition("binance").product_families)
        self.assertNotIn("USD_M", provider_definition("kraken").product_families)

    def test_qualification_is_exact_code_and_time_bound(self):
        evidence = QualificationEvidence(
            provider_id="BYBIT",
            product_family="SPOT",
            environment="TEST",
            adapter_code_sha=CODE_SHA,
            documentation_ref="official-docs-snapshot",
            observed_at=NOW,
            expires_at=NOW + timedelta(days=30),
            passed_cases=REQUIRED_QUALIFICATION_CASES,
        )
        self.assertEqual(
            evidence.status(now=NOW + timedelta(days=1), exact_code_sha=CODE_SHA),
            "QUALIFIED_FOR_NONLIVE",
        )
        self.assertEqual(
            evidence.status(now=NOW + timedelta(days=1), exact_code_sha=OTHER_SHA),
            "CODE_MISMATCH",
        )
        self.assertEqual(
            evidence.status(now=NOW + timedelta(days=31), exact_code_sha=CODE_SHA),
            "EXPIRED",
        )

    def test_qualification_rejects_human_labels_as_exact_code_sha(self):
        with self.assertRaisesRegex(ProviderCoreError, "canonical"):
            QualificationEvidence(
                provider_id="BYBIT",
                product_family="SPOT",
                environment="TEST",
                adapter_code_sha="abc123",
                documentation_ref="official-docs-snapshot",
                observed_at=NOW,
                expires_at=NOW + timedelta(days=1),
                passed_cases=REQUIRED_QUALIFICATION_CASES,
            )

    def test_live_evidence_cannot_be_mislabeled_as_nonlive_qualification(self):
        evidence = QualificationEvidence(
            provider_id="BYBIT",
            product_family="SPOT",
            environment=" live ",
            adapter_code_sha=CODE_SHA,
            documentation_ref="official-live-docs-snapshot",
            observed_at=NOW,
            expires_at=NOW + timedelta(days=1),
            passed_cases=REQUIRED_QUALIFICATION_CASES,
        )
        self.assertEqual(evidence.environment, "LIVE")
        self.assertEqual(
            evidence.status(now=NOW, exact_code_sha=CODE_SHA),
            "LIVE_REQUIRES_BOUNDED_REAL",
        )

    def test_incomplete_qualification_fails_closed(self):
        evidence = QualificationEvidence(
            provider_id="ALPACA",
            product_family="EQUITIES",
            environment="PAPER",
            adapter_code_sha=CODE_SHA,
            documentation_ref="docs",
            observed_at=NOW,
            expires_at=NOW + timedelta(days=1),
            passed_cases=frozenset({"metadata", "authentication"}),
        )
        self.assertEqual(
            evidence.status(now=NOW, exact_code_sha=CODE_SHA),
            "INCOMPLETE",
        )

    def test_research_cannot_consume_recovery_quota(self):
        bucket = QuotaBucket(capacity=Decimal("100"), recovery_reserve=Decimal("20"))
        bucket.acquire("70", purpose="RESEARCH")
        with self.assertRaisesRegex(ProviderCoreError, "reserve"):
            bucket.acquire("15", purpose="RESEARCH")
        bucket.acquire("20", purpose="RECOVERY")
        self.assertEqual(bucket.available(), Decimal("10"))

    def test_clock_skew_blocks_authenticated_send(self):
        guard = ClockGuard(timedelta(seconds=2))
        guard.require_safe(host_time=NOW, provider_time=NOW + timedelta(seconds=2))
        with self.assertRaisesRegex(ProviderCoreError, "clock skew"):
            guard.require_safe(host_time=NOW, provider_time=NOW + timedelta(seconds=3))

    def test_ambiguous_write_is_never_blindly_retried(self):
        unknown = classify_write_outcome(
            transport_started=True,
            provider_acknowledged=False,
            provider_rejected=False,
        )
        self.assertEqual(unknown.status, "UNKNOWN")
        self.assertFalse(unknown.retry_same_economic_action)
        self.assertTrue(unknown.reconciliation_required)

    def test_direct_write_outcome_cannot_bypass_send_state_invariants(self):
        invalid_cases = (
            ("UNKNOWN", True, True),
            ("UNKNOWN", False, False),
            ("NOT_SENT", False, False),
            ("ACKNOWLEDGED", True, False),
            ("REJECTED", False, True),
        )
        for status, retry, reconcile in invalid_cases:
            with self.subTest(status=status), self.assertRaisesRegex(
                ProviderCoreError,
                "send-state invariant",
            ):
                WriteOutcome(status, retry, reconcile)

        with self.assertRaisesRegex(ProviderCoreError, "boolean"):
            WriteOutcome("UNKNOWN", 0, True)

    def test_adapter_code_identity_rejects_noncanonical_whitespace(self):
        with self.assertRaisesRegex(ProviderCoreError, "canonical"):
            QualificationEvidence(
                provider_id="BYBIT",
                product_family="SPOT",
                environment="TEST",
                adapter_code_sha=" " + CODE_SHA,
                documentation_ref="docs",
                observed_at=NOW,
                expires_at=NOW + timedelta(days=1),
                passed_cases=REQUIRED_QUALIFICATION_CASES,
            )

        evidence = QualificationEvidence(
            provider_id="BYBIT",
            product_family="SPOT",
            environment="TEST",
            adapter_code_sha=CODE_SHA,
            documentation_ref="docs",
            observed_at=NOW,
            expires_at=NOW + timedelta(days=1),
            passed_cases=REQUIRED_QUALIFICATION_CASES,
        )
        with self.assertRaisesRegex(ProviderCoreError, "canonical"):
            evidence.status(
                now=NOW + timedelta(hours=1),
                exact_code_sha=CODE_SHA + " ",
            )

    def test_timezone_object_without_utc_offset_fails_closed(self):
        invalid = datetime(2026, 9, 24, 18, tzinfo=_NoOffsetTZ())
        with self.assertRaisesRegex(ProviderCoreError, "timezone-aware"):
            QualificationEvidence(
                provider_id="BYBIT",
                product_family="SPOT",
                environment="TEST",
                adapter_code_sha=CODE_SHA,
                documentation_ref="docs",
                observed_at=invalid,
                expires_at=NOW + timedelta(days=1),
                passed_cases=REQUIRED_QUALIFICATION_CASES,
            )

    def test_only_never_sent_write_is_retryable_as_same_action(self):
        outcome = classify_write_outcome(
            transport_started=False,
            provider_acknowledged=False,
            provider_rejected=False,
        )
        self.assertEqual(outcome.status, "NOT_SENT")
        self.assertTrue(outcome.retry_same_economic_action)
        self.assertFalse(outcome.reconciliation_required)

    def test_invalid_float_quota_is_rejected(self):
        with self.assertRaises(ProviderCoreError):
            QuotaBucket(capacity=100.0, recovery_reserve=10)


if __name__ == "__main__":
    unittest.main()
