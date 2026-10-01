from datetime import timedelta
import unittest

from mvp.autotrade_mvp.provider_core import (
    ProviderCoreError,
    Surface,
    observe_authenticated_json_response,
    prepare_authenticated_read_query,
)
from mvp.autotrade_mvp.provider_domain import (
    ProviderDomainError,
    normalize_provider_environment,
)
from mvp.tests.test_bybit_v5 import READ_AT, read_capability
from mvp.tests.test_provider_transport import READ_NOW, verified_read_capability


class ProviderDomainAuthenticatedReadIdentityTests(unittest.TestCase):
    def test_bybit_paper_never_guesses_provider_environment(self):
        with self.assertRaisesRegex(
            ProviderDomainError,
            "requires explicit provider_environment",
        ):
            normalize_provider_environment(
                provider_id="BYBIT",
                environment="PAPER",
                provider_environment=None,
            )
        self.assertEqual(
            normalize_provider_environment(
                provider_id="BYBIT",
                environment="PAPER",
                provider_environment="TESTNET",
            ),
            "TESTNET",
        )
        self.assertEqual(
            normalize_provider_environment(
                provider_id="BYBIT",
                environment="PAPER",
                provider_environment="DEMO",
            ),
            "DEMO",
        )

    def test_same_bybit_query_in_testnet_and_demo_has_distinct_identity(self):
        testnet_capability = read_capability(provider_environment="TESTNET")
        demo_capability = read_capability(provider_environment="DEMO")
        kwargs = dict(
            surface=Surface.AUTHENTICATED_READ,
            endpoint="/v5/execution/list",
            query={"category": "spot", "limit": "100"},
            at=READ_AT,
            permission_scope="ORDER.READ",
        )
        testnet = prepare_authenticated_read_query(
            capability=testnet_capability,
            **kwargs,
            provider_environment="TESTNET",
        )
        demo = prepare_authenticated_read_query(
            capability=demo_capability,
            **kwargs,
            provider_environment="DEMO",
        )
        self.assertNotEqual(testnet.query_digest, demo.query_digest)
        self.assertEqual(testnet.provider_environment, "TESTNET")
        self.assertEqual(demo.provider_environment, "DEMO")
        with self.assertRaisesRegex(
            ProviderCoreError,
            "does not match capability",
        ):
            prepare_authenticated_read_query(
                capability=testnet_capability,
                **kwargs,
                provider_environment="DEMO",
            )
        with self.assertRaisesRegex(
            Exception,
            "provider environment mismatch",
        ):
            testnet.require_scope(
                provider_id="BYBIT",
                surface=Surface.AUTHENTICATED_READ,
                endpoint="/v5/execution/list",
                account_id=testnet_capability.account_id,
                environment="PAPER",
                provider_environment="DEMO",
            )

    def test_non_bybit_default_domain_is_runtime_environment(self):
        capability = verified_read_capability()
        binding = prepare_authenticated_read_query(
            capability=capability,
            surface=Surface.AUTHENTICATED_READ,
            endpoint="/api/v3/account",
            query={"omitZeroBalances": "true"},
            at=READ_NOW,
            permission_scope="ORDER.READ",
        )
        self.assertEqual(binding.provider_environment, "PAPER")
        observation = observe_authenticated_json_response(
            query_binding=binding,
            http_status=200,
            response_bytes=b'{"balances":[]}',
            observed_at=READ_NOW + timedelta(seconds=1),
        )
        self.assertEqual(observation.provider_environment, "PAPER")
        observation.require_scope(
            provider_id="BINANCE",
            surface=Surface.AUTHENTICATED_READ,
            endpoint="/api/v3/account",
            account_id=capability.account_id,
            environment="PAPER",
            provider_environment="PAPER",
        )


if __name__ == "__main__":
    unittest.main()
