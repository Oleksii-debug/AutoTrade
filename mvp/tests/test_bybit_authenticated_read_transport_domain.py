from datetime import timezone
import unittest

from mvp.autotrade_mvp.capabilities import CapabilityRegistry
from mvp.autotrade_mvp.provider_core import Surface, prepare_authenticated_read_query
from mvp.autotrade_mvp.provider_transport import (
    AuthenticatedReadEndpointRule,
    BYBIT_V5_ENDPOINT_POLICIES,
    BybitV5AuthenticatedReadTransport,
)
from mvp.tests.test_bybit_v5 import READ_AT, read_capability


class BybitAuthenticatedReadTransportDomainTests(unittest.TestCase):
    def test_current_capability_lookup_uses_exact_provider_environment(self):
        capability = read_capability(provider_environment="TESTNET")
        registry = CapabilityRegistry()
        registry.add(capability)
        binding = prepare_authenticated_read_query(
            capability=capability,
            surface=Surface.AUTHENTICATED_READ,
            endpoint="/v5/execution/list",
            query={"category": "spot", "limit": "100"},
            at=READ_AT,
            permission_scope="ORDER.READ",
            provider_environment="TESTNET",
        )
        transport = object.__new__(BybitV5AuthenticatedReadTransport)
        transport.policy = BYBIT_V5_ENDPOINT_POLICIES["TESTNET"]
        transport.provider_environment = "TESTNET"
        transport.account_id = capability.account_id
        transport.capability_snapshot_id = capability.snapshot_id
        transport.capability_registry = registry
        transport.clock_utc = lambda: READ_AT.astimezone(timezone.utc)
        rule = AuthenticatedReadEndpointRule(
            surface=Surface.AUTHENTICATED_READ,
            permission_scope="ORDER.READ",
            data_entitlement="EXECUTIONS",
            success_statuses=frozenset({200}),
        )

        current = transport._require_current_capability(binding, rule)

        self.assertIs(current, capability)
        self.assertEqual(current.provider_environment, "TESTNET")


if __name__ == "__main__":
    unittest.main()
