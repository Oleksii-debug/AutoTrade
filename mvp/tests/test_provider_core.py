from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest

from mvp.autotrade_mvp.provider_core import (
    ClockGuard,
    PROVIDERS,
    REQUIRED_QUALIFICATION_CASES,
    ProviderCoreError,
    QualificationEvidence,
    QuotaBucket,
    classify_write_outcome,
    provider_definition,
)


NOW = datetime(2026, 9, 24, 18, tzinfo=timezone.utc)


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
            adapter_code_sha="abc123",
            documentation_ref="official-docs-snapshot",
            observed_at=NOW,
            expires_at=NOW + timedelta(days=30),
            passed_cases=REQUIRED_QUALIFICATION_CASES,
        )
        self.assertEqual(
            evidence.status(now=NOW + timedelta(days=1), exact_code_sha="abc123"),
            "QUALIFIED_FOR_NONLIVE",
        )
        self.assertEqual(
            evidence.status(now=NOW + timedelta(days=1), exact_code_sha="other"),
            "CODE_MISMATCH",
        )
        self.assertEqual(
            evidence.status(now=NOW + timedelta(days=31), exact_code_sha="abc123"),
            "EXPIRED",
        )

    def test_incomplete_qualification_fails_closed(self):
        evidence = QualificationEvidence(
            provider_id="ALPACA",
            product_family="EQUITIES",
            environment="PAPER",
            adapter_code_sha="sha",
            documentation_ref="docs",
            observed_at=NOW,
            expires_at=NOW + timedelta(days=1),
            passed_cases=frozenset({"metadata", "authentication"}),
        )
        self.assertEqual(
            evidence.status(now=NOW, exact_code_sha="sha"),
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
