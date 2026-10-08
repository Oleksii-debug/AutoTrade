import unittest

from mvp.autotrade_mvp.provider_domain import (
    ProviderDomainError,
    ProviderFinancialScope,
)


class ProviderFinancialScopeTests(unittest.TestCase):
    def test_same_runtime_different_provider_environment_is_distinct_financial_scope(self):
        testnet = ProviderFinancialScope(
            provider_id="BYBIT",
            runtime_environment="PAPER",
            provider_environment="TESTNET",
            entity_policy_id="LINEAR_ORDER_V1",
        )
        demo = ProviderFinancialScope(
            provider_id="BYBIT",
            runtime_environment="PAPER",
            provider_environment="DEMO",
            entity_policy_id="LINEAR_ORDER_V1",
        )

        self.assertNotEqual(testnet, demo)
        self.assertNotEqual(testnet.content_digest, demo.content_digest)

    def test_financial_entity_policy_participates_in_identity(self):
        linear = ProviderFinancialScope(
            provider_id="BYBIT",
            runtime_environment="PAPER",
            provider_environment="TESTNET",
            entity_policy_id="LINEAR_ORDER_V1",
        )
        inverse = ProviderFinancialScope(
            provider_id="BYBIT",
            runtime_environment="PAPER",
            provider_environment="TESTNET",
            entity_policy_id="INVERSE_ORDER_V1",
        )
        self.assertNotEqual(linear.content_digest, inverse.content_digest)

    def test_payload_and_digest_are_versioned_and_deterministic(self):
        scope = ProviderFinancialScope(
            provider_id="bybit",
            runtime_environment="paper",
            provider_environment="testnet",
            entity_policy_id="linear_order_v1",
        )
        same = ProviderFinancialScope(
            provider_id="BYBIT",
            runtime_environment="PAPER",
            provider_environment="TESTNET",
            entity_policy_id="LINEAR_ORDER_V1",
        )

        self.assertEqual(scope, same)
        self.assertEqual(scope.payload()["schema_version"], "1.0.0")
        self.assertEqual(scope.content_digest, same.content_digest)
        self.assertTrue(
            scope.content_digest.startswith("provider-financial-scope:sha256:")
        )

    def test_runtime_environment_is_not_provider_environment(self):
        scope = ProviderFinancialScope(
            provider_id="BYBIT",
            runtime_environment="PAPER",
            provider_environment="TESTNET",
            entity_policy_id="LINEAR_ORDER_V1",
        )
        with self.assertRaises(ProviderDomainError):
            scope.require_exact(
                provider_id="BYBIT",
                runtime_environment="PAPER",
                provider_environment="PAPER",
                entity_policy_id="LINEAR_ORDER_V1",
            )

    def test_exact_scope_match_reuses_one_normalizer(self):
        scope = ProviderFinancialScope(
            provider_id="BYBIT",
            runtime_environment="PAPER",
            provider_environment="TESTNET",
            entity_policy_id="LINEAR_ORDER_V1",
        )
        scope.require_exact(
            provider_id="bybit",
            runtime_environment="paper",
            provider_environment="testnet",
            entity_policy_id="linear_order_v1",
        )

    def test_bybit_financial_scope_rejects_crossed_paper_and_live_domains(self):
        for environment, provider_environment in (
            ("PAPER", "MAINNET"),
            ("LIVE", "TESTNET"),
            ("LIVE", "DEMO"),
        ):
            with self.subTest(environment=environment, provider_environment=provider_environment):
                with self.assertRaises(ProviderDomainError):
                    ProviderFinancialScope(
                        provider_id="BYBIT",
                        runtime_environment=environment,
                        provider_environment=provider_environment,
                        entity_policy_id="LINEAR_ORDER_V1",
                    )

    def test_bybit_financial_scope_accepts_canonical_separate_domains(self):
        for environment, provider_environment in (
            ("PAPER", "TESTNET"),
            ("PAPER", "DEMO"),
            ("LIVE", "MAINNET"),
        ):
            with self.subTest(environment=environment, provider_environment=provider_environment):
                scope = ProviderFinancialScope(
                    provider_id="BYBIT",
                    runtime_environment=environment,
                    provider_environment=provider_environment,
                    entity_policy_id="LINEAR_ORDER_V1",
                )
                self.assertEqual(scope.runtime_environment, environment)
                self.assertEqual(scope.provider_environment, provider_environment)

    def test_non_builtin_or_invalid_tokens_fail_closed(self):
        class Text(str):
            pass

        with self.assertRaises(ProviderDomainError):
            ProviderFinancialScope(
                provider_id=Text("BYBIT"),
                runtime_environment="PAPER",
                provider_environment="TESTNET",
                entity_policy_id="LINEAR_ORDER_V1",
            )
        with self.assertRaises(ProviderDomainError):
            ProviderFinancialScope(
                provider_id="BYBIT",
                runtime_environment="PRODUCTION",
                provider_environment="LIVE",
                entity_policy_id="LINEAR_ORDER_V1",
            )
        with self.assertRaises(ProviderDomainError):
            ProviderFinancialScope(
                provider_id="BYBIT",
                runtime_environment="PAPER",
                provider_environment="TEST NET",
                entity_policy_id="LINEAR_ORDER_V1",
            )


if __name__ == "__main__":
    unittest.main()
