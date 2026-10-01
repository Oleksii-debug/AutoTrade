from datetime import timedelta
import unittest

from mvp.autotrade_mvp.provider_core import (
    ProviderCoreError,
    Surface,
    prepare_authenticated_read_query,
)
from mvp.tests.test_capability_provider_environment import NOW, snapshot


class AuthenticatedReadCapabilityDomainGuardTests(unittest.TestCase):
    def test_read_binding_cannot_change_exact_capability_provider_environment(self):
        capability = snapshot(provider_environment="TESTNET")
        exact = prepare_authenticated_read_query(
            capability=capability,
            surface=Surface.AUTHENTICATED_READ,
            endpoint="/account/read",
            query={"kind": "balance"},
            at=NOW + timedelta(seconds=1),
            provider_environment="TESTNET",
        )
        self.assertEqual(exact.provider_environment, "TESTNET")

        with self.assertRaisesRegex(
            ProviderCoreError,
            "provider environment does not match capability",
        ):
            prepare_authenticated_read_query(
                capability=capability,
                surface=Surface.AUTHENTICATED_READ,
                endpoint="/account/read",
                query={"kind": "balance"},
                at=NOW + timedelta(seconds=1),
                provider_environment="DEMO",
            )


if __name__ == "__main__":
    unittest.main()
