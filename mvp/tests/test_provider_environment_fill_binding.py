import unittest

from mvp.autotrade_mvp._provider_activity_accounting_impl import (
    _provider_fill_binding_aggregate_id,
    _provider_fill_binding_payload,
)
from mvp.autotrade_mvp.reconciliation import ProviderFillEvidence


def provider_fill(provider: str, environment: str, provider_environment: str):
    return ProviderFillEvidence.create(
        provider_id=provider,
        account_id="acct-1",
        environment=environment,
        provider_environment=provider_environment,
        provider_execution_id="exec-1",
        client_order_id="client-1",
        instrument="BTCUSDT",
        quantity="1",
        price="100",
        fee_amount="0",
        fee_currency="USDT",
        trade_time="2026-09-24T18:00:00Z",
        side="BUY",
        evidence_refs=("provider-read:sha256:" + "1" * 64,),
    )


class ProviderEnvironmentFillBindingTests(unittest.TestCase):
    def test_testnet_and_demo_fill_bindings_have_disjoint_identity(self):
        testnet = _provider_fill_binding_aggregate_id(
            provider_id="BYBIT",
            account_id="acct-1",
            environment="PAPER",
            provider_environment="TESTNET",
            provider_execution_id="exec-1",
        )
        demo = _provider_fill_binding_aggregate_id(
            provider_id="BYBIT",
            account_id="acct-1",
            environment="PAPER",
            provider_environment="DEMO",
            provider_execution_id="exec-1",
        )
        self.assertNotEqual(testnet, demo)

    def test_narrow_provider_domain_is_part_of_fill_binding_payload(self):
        payload = _provider_fill_binding_payload(
            provider_fill("BYBIT", "PAPER", "TESTNET")
        )
        self.assertEqual(payload["provider_environment"], "TESTNET")

    def test_non_narrow_provider_domain_preserves_legacy_payload_shape(self):
        payload = _provider_fill_binding_payload(
            provider_fill("KRAKEN", "PAPER", "PAPER")
        )
        self.assertNotIn("provider_environment", payload)


if __name__ == "__main__":
    unittest.main()
