from datetime import datetime, timedelta, timezone
import unittest

from mvp.autotrade_mvp.capabilities import CapabilityClaim, EvidenceVerification, derive_capability_snapshot
from mvp.autotrade_mvp.provider_core import (
    ProviderCoreError,
    Surface,
    observe_authenticated_json_response,
    prepare_authenticated_read_query,
)
from mvp.tests.capability_test_support import fresh_test_admission

NOW = datetime(2026, 10, 3, 18, 0, tzinfo=timezone.utc)
SNAPSHOT = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
SOURCES = ("DOCUMENTED", "API", "ACCOUNT", "INSTRUMENT")


def _claim(source: str, provider_environment: str) -> CapabilityClaim:
    observed = NOW - timedelta(minutes=1)
    return CapabilityClaim(
        source=source,
        provider_id="BYBIT",
        account_id="paper-account",
        entity_id="entity-1",
        environment="PAPER",
        provider_environment=provider_environment,
        instrument_version="BTCUSDT:v1",
        observed_at=observed,
        expires_at=NOW + timedelta(minutes=10),
        supported_order_types=frozenset({"LIMIT"}),
        time_in_force=frozenset({"GTC"}),
        permission_scopes=frozenset({"ORDER.READ"}),
        position_mode="NET",
        native_protection=frozenset(),
        rate_limit_policy_id="bybit-v1",
        data_entitlements=frozenset({"ORDER"}),
        evidence_ref={
            "artifact_id": f"11111111-1111-4111-8111-11111111111{len(source) % 10}",
            "sha256": "sha256:" + "a" * 64,
            "observed_at": observed.isoformat().replace("+00:00", "Z"),
        },
    )


def _snapshot(provider_environment: str, *, admitted: bool):
    snapshot = derive_capability_snapshot(
        snapshot_id=SNAPSHOT,
        claims=tuple(_claim(source, provider_environment) for source in SOURCES),
        observed_at=NOW,
        evidence_verifier=lambda _claim: EvidenceVerification(valid=True),
    )
    return fresh_test_admission(snapshot) if admitted else snapshot


class ProviderReadCapabilityDomainTests(unittest.TestCase):
    def test_descriptive_verified_capability_cannot_prepare_provider_read(self):
        descriptive = _snapshot("TESTNET", admitted=False)
        self.assertEqual(descriptive.status, "VERIFIED")
        with self.assertRaisesRegex(ProviderCoreError, "does not admit"):
            prepare_authenticated_read_query(
                capability=descriptive,
                surface=Surface.AUTHENTICATED_READ,
                endpoint="/v5/order/realtime",
                query={"symbol": "BTCUSDT"},
                at=NOW,
            )

    def test_query_digest_distinguishes_testnet_and_demo(self):
        testnet = prepare_authenticated_read_query(
            capability=_snapshot("TESTNET", admitted=True),
            surface=Surface.AUTHENTICATED_READ,
            endpoint="/v5/order/realtime",
            query={"symbol": "BTCUSDT"},
            at=NOW,
        )
        demo = prepare_authenticated_read_query(
            capability=_snapshot("DEMO", admitted=True),
            surface=Surface.AUTHENTICATED_READ,
            endpoint="/v5/order/realtime",
            query={"symbol": "BTCUSDT"},
            at=NOW,
        )
        self.assertEqual(testnet.provider_environment, "TESTNET")
        self.assertEqual(demo.provider_environment, "DEMO")
        self.assertNotEqual(testnet.query_digest, demo.query_digest)

    def test_observation_scope_rejects_provider_environment_relabelling(self):
        binding = prepare_authenticated_read_query(
            capability=_snapshot("TESTNET", admitted=True),
            surface=Surface.AUTHENTICATED_READ,
            endpoint="/v5/order/realtime",
            query={"symbol": "BTCUSDT"},
            at=NOW,
        )
        observation = observe_authenticated_json_response(
            query_binding=binding,
            http_status=200,
            response_bytes=b'{"retCode":0}',
            observed_at=NOW + timedelta(seconds=1),
        )
        self.assertEqual(observation.provider_environment, "TESTNET")
        observation.require_scope(
            provider_id="BYBIT",
            surface=Surface.AUTHENTICATED_READ,
            endpoint="/v5/order/realtime",
            account_id="paper-account",
            environment="PAPER",
            provider_environment="TESTNET",
        )
        with self.assertRaisesRegex(ProviderCoreError, "provider environment mismatch"):
            observation.require_scope(
                provider_id="BYBIT",
                surface=Surface.AUTHENTICATED_READ,
                endpoint="/v5/order/realtime",
                provider_environment="DEMO",
            )


if __name__ == "__main__":
    unittest.main()
