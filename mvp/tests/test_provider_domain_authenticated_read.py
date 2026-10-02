from datetime import timedelta
import unittest

from mvp.autotrade_mvp.capabilities import CapabilitySnapshot
from mvp.autotrade_mvp.provider_core import (
    AuthenticatedReadQueryBinding,
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


    def test_prepare_rejects_capability_subclass_before_financial_identity_use(self):
        base = read_capability(provider_environment="TESTNET")

        class CapabilitySubclass(CapabilitySnapshot):
            def __post_init__(self, *args, **kwargs):
                pass

        impostor = CapabilitySubclass(
            snapshot_id=base.snapshot_id,
            provider_id=base.provider_id,
            account_id=base.account_id,
            entity_id=base.entity_id,
            environment=base.environment,
            provider_environment=base.provider_environment,
            instrument_version=base.instrument_version,
            observed_at=base.observed_at,
            expires_at=base.expires_at,
            supported_order_types=base.supported_order_types,
            time_in_force=base.time_in_force,
            permission_scopes=base.permission_scopes,
            position_mode=base.position_mode,
            native_protection=base.native_protection,
            rate_limit_policy_id=base.rate_limit_policy_id,
            data_entitlements=base.data_entitlements,
            evidence=base.evidence,
            status=base.status,
            sources=base.sources,
        )
        with self.assertRaisesRegex(TypeError, "exact CapabilitySnapshot"):
            prepare_authenticated_read_query(
                capability=impostor,
                surface=Surface.AUTHENTICATED_READ,
                endpoint="/v5/execution/list",
                query={"category": "spot"},
                at=READ_AT,
                provider_environment="TESTNET",
            )

    def test_response_observation_rejects_query_binding_subclass(self):
        base = prepare_authenticated_read_query(
            capability=read_capability(provider_environment="TESTNET"),
            surface=Surface.AUTHENTICATED_READ,
            endpoint="/v5/execution/list",
            query={"category": "spot"},
            at=READ_AT,
            provider_environment="TESTNET",
        )

        class QuerySubclass(AuthenticatedReadQueryBinding):
            def __post_init__(self, *args, **kwargs):
                pass

        impostor = QuerySubclass(
            provider_id=base.provider_id,
            account_id=base.account_id,
            entity_id=base.entity_id,
            environment=base.environment,
            provider_environment=base.provider_environment,
            capability_snapshot_id=base.capability_snapshot_id,
            instrument_version=base.instrument_version,
            surface=base.surface,
            endpoint=base.endpoint,
            query=base.query,
            prepared_at=base.prepared_at,
            permission_scope=base.permission_scope,
            query_digest=base.query_digest,
        )
        with self.assertRaisesRegex(TypeError, "exact AuthenticatedReadQueryBinding"):
            observe_authenticated_json_response(
                query_binding=impostor,
                http_status=200,
                response_bytes=b'{"list":[]}',
                observed_at=READ_AT + timedelta(seconds=1),
            )


if __name__ == "__main__":
    unittest.main()
