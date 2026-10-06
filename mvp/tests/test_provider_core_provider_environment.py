import unittest
from datetime import datetime, timedelta, timezone

from mvp.autotrade_mvp.capabilities import (
    CapabilityClaim,
    CapabilityRegistry,
    EvidenceVerification,
    derive_capability_snapshot,
)
from mvp.autotrade_mvp.provider_core import (
    ProviderCoreError,
    Surface,
    observe_authenticated_json_response,
    prepare_authenticated_read_query,
    provider_response_observation_projection,
    provider_response_observation_require_scope,
)
from mvp.autotrade_mvp.provider_transport import (
    BYBIT_V5_ENDPOINT_POLICIES,
    BybitV5AuthenticatedReadSigner,
    BybitV5AuthenticatedReadTransport,
    ProviderTransportScopeError,
)
from mvp.autotrade_mvp.windows_secrets import PersistentCredentialHandle


NOW = datetime(2026, 10, 6, 14, 45, tzinfo=timezone.utc)
SNAPSHOT_ID = "71717171-7171-4717-8717-717171717171"
_ARTIFACTS = {
    "DOCUMENTED": "71717171-0001-4001-8001-717171717171",
    "API": "71717171-0002-4002-8002-717171717171",
    "ACCOUNT": "71717171-0003-4003-8003-717171717171",
    "INSTRUMENT": "71717171-0004-4004-8004-717171717171",
}


def bybit_capability(provider_environment: str):
    observed = NOW - timedelta(minutes=1)
    expires = NOW + timedelta(minutes=10)
    claims = tuple(
        CapabilityClaim(
            source=source,
            provider_id="BYBIT",
            account_id="acct-1",
            entity_id="entity-bybit-unified",
            environment="PAPER",
            provider_environment=provider_environment,
            instrument_version="BTCUSDT@1",
            observed_at=observed,
            expires_at=expires,
            supported_order_types=frozenset({"LIMIT"}),
            time_in_force=frozenset({"GTC"}),
            permission_scopes=frozenset({"ORDER.READ"}),
            position_mode="NET",
            native_protection=frozenset(),
            rate_limit_policy_id="bybit-paper-v1",
            data_entitlements=frozenset({"ACCOUNT"}),
            evidence_ref={
                "artifact_id": _ARTIFACTS[source],
                "sha256": "sha256:" + "7" * 64,
                "observed_at": observed.isoformat().replace("+00:00", "Z"),
            },
        )
        for source in ("DOCUMENTED", "API", "ACCOUNT", "INSTRUMENT")
    )
    return derive_capability_snapshot(
        snapshot_id=SNAPSHOT_ID,
        claims=claims,
        observed_at=NOW,
        evidence_verifier=lambda _claim: EvidenceVerification(valid=True),
    )


def binding(provider_environment: str):
    return prepare_authenticated_read_query(
        capability=bybit_capability(provider_environment),
        surface=Surface.AUTHENTICATED_READ,
        endpoint="/v5/account/wallet-balance",
        query={"accountType": "UNIFIED"},
        at=NOW,
        permission_scope="ORDER.READ",
    )


class AuthenticatedReadProviderEnvironmentTests(unittest.TestCase):
    def test_bybit_testnet_and_demo_are_distinct_query_identities(self):
        testnet = binding("TESTNET")
        demo = binding("DEMO")

        self.assertEqual(testnet.provider_environment, "TESTNET")
        self.assertEqual(demo.provider_environment, "DEMO")
        self.assertEqual(testnet.capability_snapshot_id, demo.capability_snapshot_id)
        self.assertEqual(testnet.query, demo.query)
        self.assertEqual(testnet.prepared_at, demo.prepared_at)
        self.assertNotEqual(testnet.query_digest, demo.query_digest)

        testnet.require_scope(
            provider_id="BYBIT",
            account_id="acct-1",
            environment="PAPER",
            provider_environment="TESTNET",
            surface=Surface.AUTHENTICATED_READ,
            endpoint="/v5/account/wallet-balance",
        )
        with self.assertRaisesRegex(
            ProviderCoreError,
            "provider environment mismatch",
        ):
            testnet.require_scope(
                provider_id="BYBIT",
                account_id="acct-1",
                environment="PAPER",
                provider_environment="DEMO",
                surface=Surface.AUTHENTICATED_READ,
                endpoint="/v5/account/wallet-balance",
            )

    def test_response_projection_preserves_provider_environment(self):
        query = binding("TESTNET")
        response = observe_authenticated_json_response(
            query_binding=query,
            http_status=200,
            response_bytes=b'{"retCode":0,"result":{}}',
            observed_at=NOW + timedelta(seconds=1),
        )

        projection = provider_response_observation_projection(response)
        self.assertEqual(projection["provider_environment"], "TESTNET")
        self.assertEqual(response.provider_environment, "TESTNET")

        exact = provider_response_observation_require_scope(
            response,
            provider_id="BYBIT",
            account_id="acct-1",
            environment="PAPER",
            provider_environment="TESTNET",
            surface=Surface.AUTHENTICATED_READ,
            endpoint="/v5/account/wallet-balance",
        )
        self.assertIs(exact["query_binding"], query)
        with self.assertRaisesRegex(
            ProviderCoreError,
            "provider environment mismatch",
        ):
            provider_response_observation_require_scope(
                response,
                provider_id="BYBIT",
                account_id="acct-1",
                environment="PAPER",
                provider_environment="DEMO",
                surface=Surface.AUTHENTICATED_READ,
                endpoint="/v5/account/wallet-balance",
            )

    def test_bybit_signer_rejects_policy_from_another_provider_domain(self):
        query = binding("TESTNET")
        with self.assertRaisesRegex(
            ProviderTransportScopeError,
            "provider environment mismatch",
        ):
            BybitV5AuthenticatedReadSigner.sign(
                policy=BYBIT_V5_ENDPOINT_POLICIES["DEMO"],
                query_binding=query,
                credential_plaintext='{"api_key":"test-key","api_secret":"test-secret"}',
                timestamp_ms=1_700_000_000_000,
            )

    def test_bybit_transport_rejects_wrong_domain_before_secret_or_network(self):
        class NoSecret:
            def lease_for_execution(self, *_args, **_kwargs):
                raise AssertionError("secret resolver must not be reached")

        transport = BybitV5AuthenticatedReadTransport(
            policy=BYBIT_V5_ENDPOINT_POLICIES["TESTNET"],
            provider_environment="TESTNET",
            account_id="acct-1",
            capability_snapshot_id=SNAPSHOT_ID,
            capability_registry=CapabilityRegistry(),
            secret_resolver=NoSecret(),
            credential_handle=PersistentCredentialHandle(
                handle_id="bybit-read-testnet",
                account_id="acct-1",
                provider="BYBIT",
                environment="PAPER",
                provider_environment="TESTNET",
                purpose="READ",
                generation=1,
            ),
            session_token="session",
            origin="https://localhost",
            execution_identity="provider-domain-test",
            clock_millis=lambda: 1_700_000_000_000,
            clock_utc=lambda: NOW,
        )
        with self.assertRaisesRegex(
            ProviderTransportScopeError,
            "query scope mismatch",
        ):
            transport(binding("DEMO"))


    def test_provider_environment_mutation_after_mint_is_detected(self):
        query = binding("TESTNET")
        object.__setattr__(query, "provider_environment", "DEMO")

        with self.assertRaisesRegex(
            ProviderCoreError,
            "changed after preparation",
        ):
            query.require_scope(
                provider_id="BYBIT",
                provider_environment="DEMO",
                surface=Surface.AUTHENTICATED_READ,
                endpoint="/v5/account/wallet-balance",
            )
        with self.assertRaisesRegex(
            ProviderCoreError,
            "changed after preparation",
        ):
            observe_authenticated_json_response(
                query_binding=query,
                http_status=200,
                response_bytes=b'{"retCode":0}',
                observed_at=NOW + timedelta(seconds=1),
            )


    def test_bybit_transport_checks_binding_authority_before_field_comparisons(self):
        class TrapText(str):
            def __eq__(self, _other):
                raise AssertionError("binding equality executed before authority check")

            def __ne__(self, _other):
                raise AssertionError("binding inequality executed before authority check")

        class NoSecret:
            def lease_for_execution(self, *_args, **_kwargs):
                raise AssertionError("secret resolver must not be reached")

        class NoWire:
            def send(self, _request):
                raise AssertionError("network must not be reached")

        query = binding("TESTNET")
        object.__setattr__(
            query,
            "provider_environment",
            TrapText("TESTNET"),
        )
        transport = BybitV5AuthenticatedReadTransport(
            policy=BYBIT_V5_ENDPOINT_POLICIES["TESTNET"],
            provider_environment="TESTNET",
            account_id="acct-1",
            capability_snapshot_id=SNAPSHOT_ID,
            capability_registry=CapabilityRegistry(),
            secret_resolver=NoSecret(),
            credential_handle=PersistentCredentialHandle(
                handle_id="bybit-read-testnet-authority",
                account_id="acct-1",
                provider="BYBIT",
                environment="PAPER",
                provider_environment="TESTNET",
                purpose="READ",
                generation=1,
            ),
            session_token="session",
            origin="https://localhost",
            execution_identity="provider-domain-authority-test",
            clock_millis=lambda: 1_700_000_000_000,
            clock_utc=lambda: NOW,
            wire_client=NoWire(),
        )

        with self.assertRaisesRegex(
            ProviderCoreError,
            "changed after preparation",
        ):
            transport(query)


if __name__ == "__main__":
    unittest.main()
