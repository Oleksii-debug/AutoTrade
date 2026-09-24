import unittest

from mvp.autotrade_mvp.provider_adapters import (
    ASSET_EQUITY,
    ASSET_OPTION,
    ASSET_SPOT_CRYPTO,
    CanonicalOrder,
    ProviderAdapterError,
    build_unsigned_order_request,
    provider_qualification_matrix,
)


class ProviderAdapterTests(unittest.TestCase):
    def spot_order(self, **changes):
        values = dict(
            client_order_id="client-123",
            asset_family=ASSET_SPOT_CRYPTO,
            symbol="BTCUSDT",
            side="BUY",
            order_type="LIMIT",
            quantity="0.01",
            price="60000",
            time_in_force="GTC",
        )
        values.update(changes)
        return CanonicalOrder(**values)

    def test_bybit_test_request_is_unsigned_and_requires_status_confirmation(self):
        plan = build_unsigned_order_request("bybit", "test", self.spot_order())
        self.assertEqual(plan.url, "https://api-testnet.bybit.com/v5/order/create")
        self.assertEqual(plan.payload["orderLinkId"], "client-123")
        self.assertFalse(plan.contains_secret)
        self.assertTrue(plan.status_confirmation_required)
        self.assertNotIn("apiKey", plan.payload)


    def test_bybit_derivative_category_is_never_guessed(self):
        derivative = self.spot_order(asset_family="PERPETUAL")
        with self.assertRaisesRegex(ProviderAdapterError, "bybit_category"):
            build_unsigned_order_request("bybit", "test", derivative)
        derivative = self.spot_order(
            asset_family="PERPETUAL", metadata={"bybit_category": "linear"}
        )
        plan = build_unsigned_order_request("bybit", "test", derivative)
        self.assertEqual(plan.payload["category"], "linear")

    def test_live_endpoint_is_impossible_without_explicit_allow_live(self):
        with self.assertRaisesRegex(ProviderAdapterError, "explicit allow_live"):
            build_unsigned_order_request("binance", "live", self.spot_order())

    def test_binance_testnet_request_preserves_client_identity(self):
        plan = build_unsigned_order_request("binance", "test", self.spot_order())
        self.assertEqual(plan.url, "https://testnet.binance.vision/api/v3/order")
        self.assertEqual(plan.payload["newClientOrderId"], "client-123")
        self.assertNotIn("signature", plan.payload)
        self.assertNotIn("timestamp", plan.payload)

    def test_kraken_has_no_invented_paper_endpoint(self):
        with self.assertRaisesRegex(ProviderAdapterError, "no evidenced PAPER endpoint"):
            build_unsigned_order_request("kraken", "paper", self.spot_order())

    def test_whitebit_fails_closed_until_exact_v4_path_is_captured(self):
        with self.assertRaisesRegex(ProviderAdapterError, "not evidence-qualified"):
            build_unsigned_order_request(
                "whitebit", "live", self.spot_order(), allow_live=True
            )

    def test_ibkr_requires_account_and_contract_identity(self):
        order = CanonicalOrder(
            client_order_id="ib-1",
            asset_family=ASSET_EQUITY,
            symbol="AAPL",
            side="BUY",
            order_type="LIMIT",
            quantity="2",
            price="200",
            time_in_force="DAY",
        )
        with self.assertRaisesRegex(ProviderAdapterError, "account_id"):
            build_unsigned_order_request("ibkr", "local_gateway", order)

        order = CanonicalOrder(**{**order.__dict__, "account_id": "DU123", "provider_instrument_id": 265598})
        plan = build_unsigned_order_request("ibkr", "local_gateway", order)
        self.assertIn("/iserver/account/DU123/orders", plan.url)
        self.assertEqual(plan.payload["orders"][0]["conid"], 265598)
        self.assertEqual(plan.payload["orders"][0]["cOID"], "ib-1")

    def test_alpaca_paper_request_uses_only_evidenced_surface(self):
        order = CanonicalOrder(
            client_order_id="alp-1",
            asset_family=ASSET_OPTION,
            symbol="AAPL260116C00200000",
            side="BUY",
            order_type="LIMIT",
            quantity="1",
            price="10.25",
            time_in_force="DAY",
        )
        plan = build_unsigned_order_request("alpaca", "paper", order)
        self.assertEqual(plan.url, "https://paper-api.alpaca.markets/v2/orders")
        self.assertEqual(plan.payload["client_order_id"], "alp-1")
        self.assertEqual(plan.payload["limit_price"], "10.25")

    def test_provider_assets_do_not_leak_across_adapters(self):
        equity = CanonicalOrder(
            client_order_id="eq-1",
            asset_family=ASSET_EQUITY,
            symbol="AAPL",
            side="BUY",
            order_type="MARKET",
            quantity="1",
            time_in_force="DAY",
        )
        with self.assertRaisesRegex(ProviderAdapterError, "no qualified mapping"):
            build_unsigned_order_request("bybit", "test", equity)

    def test_generic_reduce_only_is_rejected_when_provider_semantics_do_not_match(self):
        with self.assertRaisesRegex(ProviderAdapterError, "reduce_only"):
            build_unsigned_order_request(
                "binance", "test", self.spot_order(reduce_only=True)
            )

    def test_matrix_never_claims_live_qualification(self):
        matrix = provider_qualification_matrix()
        self.assertEqual(set(matrix), {"ALPACA", "BINANCE", "BYBIT", "IBKR", "KRAKEN", "WHITEBIT"})
        self.assertTrue(all(not row["live_qualified"] for row in matrix.values()))
        self.assertFalse(matrix["WHITEBIT"]["create_order_path_evidenced"])


if __name__ == "__main__":
    unittest.main()
