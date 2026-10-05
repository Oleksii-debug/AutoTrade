from datetime import timedelta
import unittest

from mvp.autotrade_mvp.provider_core import (
    Surface,
    prepare_authenticated_read_query,
)
from mvp.autotrade_mvp.provider_negative_result_query_scope import (
    CanonicalNegativeResultQueryScope,
    ProviderNegativeResultQueryScopeError,
    require_canonical_negative_result_query_scope,
)
from mvp.tests.test_durable_capabilities import verified
from mvp.tests.test_provider_selection import NOW


CLIENT_ORDER_ID = "autotrade-crash-ambiguity-1"
CATEGORY = "spot"


class ProviderNegativeResultQueryScopeTests(unittest.TestCase):
    def binding(self, endpoint: str, query: dict[str, str]):
        capability = verified(
            "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            NOW - timedelta(minutes=1),
            provider_id="BYBIT",
            provider_environment="TESTNET",
        )
        return prepare_authenticated_read_query(
            capability=capability,
            surface=Surface.AUTHENTICATED_READ,
            endpoint=endpoint,
            query=query,
            at=NOW,
            permission_scope="ORDER.READ",
        )

    def authorize(self, endpoint: str, query: dict[str, str]):
        return require_canonical_negative_result_query_scope(
            query_binding=self.binding(endpoint, query),
            client_order_id=CLIENT_ORDER_ID,
            submission_at=NOW,
        )

    def test_realtime_exact_client_identity_is_canonical_scope(self):
        authority = self.authorize(
            "/v5/order/realtime",
            {"category": CATEGORY, "orderLinkId": CLIENT_ORDER_ID},
        )
        self.assertEqual(authority.endpoint, "/v5/order/realtime")
        self.assertEqual(authority.client_order_id, CLIENT_ORDER_ID)
        self.assertEqual(authority.category, CATEGORY)
        self.assertEqual(authority.selector_semantics, "EXACT_CLIENT_ORDER_ID")
        self.assertIsNone(authority.coverage_start_ms)
        self.assertIsNone(authority.coverage_end_ms)
        self.assertTrue(
            authority.evidence_ref.startswith(
                "canonical-negative-result-query:sha256:"
            )
        )

    def test_realtime_rejects_extra_filter_or_pagination(self):
        for extra in (
            {"symbol": "BTCUSDT"},
            {"openOnly": "1"},
            {"limit": "50"},
            {"cursor": "opaque"},
            {"orderId": "provider-order-id"},
        ):
            query = {
                "category": CATEGORY,
                "orderLinkId": CLIENT_ORDER_ID,
                **extra,
            }
            with self.subTest(extra=extra), self.assertRaisesRegex(
                ProviderNegativeResultQueryScopeError,
                "filters or pagination",
            ):
                self.authorize("/v5/order/realtime", query)

    def test_execution_exact_client_identity_is_canonical_scope(self):
        authority = self.authorize(
            "/v5/execution/list",
            {"category": "linear", "orderLinkId": CLIENT_ORDER_ID},
        )
        self.assertEqual(authority.endpoint, "/v5/execution/list")
        self.assertEqual(authority.selector_semantics, "EXACT_CLIENT_ORDER_ID")

    def test_order_history_requires_explicit_window_covering_submission(self):
        submitted_ms = int(NOW.timestamp() * 1000)
        start_ms = submitted_ms - 60_000
        end_ms = submitted_ms + 60_000
        authority = self.authorize(
            "/v5/order/history",
            {
                "category": CATEGORY,
                "orderLinkId": CLIENT_ORDER_ID,
                "startTime": str(start_ms),
                "endTime": str(end_ms),
            },
        )
        self.assertEqual(
            authority.selector_semantics,
            "EXACT_CLIENT_ORDER_ID_WITH_SUBMISSION_WINDOW",
        )
        self.assertEqual(authority.coverage_start_ms, start_ms)
        self.assertEqual(authority.coverage_end_ms, end_ms)

    def test_order_history_rejects_window_that_misses_submission(self):
        submitted_ms = int(NOW.timestamp() * 1000)
        with self.assertRaisesRegex(
            ProviderNegativeResultQueryScopeError,
            "does not cover submission instant",
        ):
            self.authorize(
                "/v5/order/history",
                {
                    "category": CATEGORY,
                    "orderLinkId": CLIENT_ORDER_ID,
                    "startTime": str(submitted_ms + 1),
                    "endTime": str(submitted_ms + 60_000),
                },
            )

    def test_order_history_rejects_window_over_seven_days(self):
        submitted_ms = int(NOW.timestamp() * 1000)
        with self.assertRaisesRegex(
            ProviderNegativeResultQueryScopeError,
            "must not exceed seven days",
        ):
            self.authorize(
                "/v5/order/history",
                {
                    "category": CATEGORY,
                    "orderLinkId": CLIENT_ORDER_ID,
                    "startTime": str(submitted_ms - 1),
                    "endTime": str(submitted_ms + 7 * 24 * 60 * 60 * 1000),
                },
            )

    def test_wrong_client_identity_is_rejected(self):
        with self.assertRaisesRegex(
            ProviderNegativeResultQueryScopeError,
            "not bound to target client order identity",
        ):
            require_canonical_negative_result_query_scope(
                query_binding=self.binding(
                    "/v5/order/realtime",
                    {"category": CATEGORY, "orderLinkId": "different-order"},
                ),
                client_order_id=CLIENT_ORDER_ID,
                submission_at=NOW,
            )

    def test_transaction_log_requires_separate_complete_window_authority(self):
        submitted_ms = int(NOW.timestamp() * 1000)
        with self.assertRaisesRegex(
            ProviderNegativeResultQueryScopeError,
            "not directly identity-selectable",
        ):
            self.authorize(
                "/v5/account/transaction-log",
                {
                    "category": CATEGORY,
                    "startTime": str(submitted_ms - 60_000),
                    "endTime": str(submitted_ms + 60_000),
                },
            )

    def test_constructor_is_sealed(self):
        with self.assertRaisesRegex(
            ProviderNegativeResultQueryScopeError,
            "canonical authenticated-read authority",
        ):
            CanonicalNegativeResultQueryScope(provider_id="BYBIT")


if __name__ == "__main__":
    unittest.main()
