from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest

from mvp.autotrade_mvp.provider_core import (
    BoundReconciliationResponse,
    ClockGuard,
    PreparedReconciliationRead,
    PROVIDERS,
    REQUIRED_QUALIFICATION_CASES,
    ProviderCoreError,
    QualificationEvidence,
    QuotaBucket,
    classify_write_outcome,
    provider_definition,
    require_reconciliation_response,
)


NOW = datetime(2026, 9, 24, 18, tzinfo=timezone.utc)
CODE_SHA = "a" * 40
OTHER_SHA = "b" * 40


class ProviderCoreTests(unittest.TestCase):
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

    def test_reconciliation_response_is_bound_to_prepared_scope_and_bytes(self):
        read = PreparedReconciliationRead.create(
            provider_id="BYBIT",
            account_id="paper-account-1",
            environment="TESTNET",
            surface="EXECUTIONS",
            endpoint="/v5/execution/list",
            request={"category": "spot", "limit": 100},
        )
        source = {"retCode": 0, "result": {"list": []}}
        bound = BoundReconciliationResponse.bind(read, source)
        source["result"]["list"].append({"execId": "forged-after-bind"})
        payload, account_id, environment = require_reconciliation_response(
            bound,
            provider_id="BYBIT",
            surface="EXECUTIONS",
            endpoint="/v5/execution/list",
        )
        self.assertEqual(payload, {"retCode": 0, "result": {"list": []}})
        self.assertEqual(account_id, "paper-account-1")
        self.assertEqual(environment, "TESTNET")
        self.assertTrue(bound.evidence_id.startswith("sha256:"))

    def test_reconciliation_response_cannot_be_relabelled_at_consumer_boundary(self):
        bound = BoundReconciliationResponse.bind(
            PreparedReconciliationRead.create(
                provider_id="ALPACA",
                account_id="account-a",
                environment="PAPER",
                surface="ACTIVITIES",
                endpoint="/v2/account/activities/FILL",
                request={"activity_types": "FILL"},
            ),
            [],
        )
        payload, account_id, environment = require_reconciliation_response(
            bound,
            provider_id="ALPACA",
            surface="ACTIVITIES",
            endpoint="/v2/account/activities/FILL",
        )
        self.assertEqual(payload, [])
        self.assertEqual((account_id, environment), ("account-a", "PAPER"))
        with self.assertRaisesRegex(ProviderCoreError, "provenance scope mismatch"):
            require_reconciliation_response(
                bound,
                provider_id="BYBIT",
                surface="EXECUTIONS",
                endpoint="/v5/execution/list",
            )

    def test_reconciliation_provenance_rejects_binary_float_request_identity(self):
        with self.assertRaisesRegex(ProviderCoreError, "binary float"):
            PreparedReconciliationRead.create(
                provider_id="BINANCE",
                account_id="account-a",
                environment="TESTNET",
                surface="EXECUTIONS",
                endpoint="/api/v3/myTrades",
                request={"limit": 100.0},
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
