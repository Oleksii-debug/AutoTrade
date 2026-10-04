from __future__ import annotations

import unittest

from mvp.autotrade_mvp import production_bybit
from mvp.autotrade_mvp.provider_transport import BYBIT_V5_ENDPOINT_POLICIES


class ProductionBybitPolicyReferenceAuthorityTests(unittest.TestCase):
    def test_coherent_policy_and_reference_retarget_is_rejected(self) -> None:
        policy = BYBIT_V5_ENDPOINT_POLICIES["TESTNET"]
        original_base_url = policy.base_url
        original_allowed_hosts = policy.allowed_hosts
        had_reference = hasattr(production_bybit, "_BYBIT_POLICY_IDENTITIES")
        original_reference = getattr(
            production_bybit,
            "_BYBIT_POLICY_IDENTITIES",
            None,
        )

        try:
            object.__setattr__(policy, "base_url", "https://evil.example")
            object.__setattr__(
                policy,
                "allowed_hosts",
                frozenset({"evil.example"}),
            )
            production_bybit._BYBIT_POLICY_IDENTITIES = {
                "TESTNET": (
                    "BYBIT",
                    "PAPER",
                    "https://evil.example",
                    frozenset({"evil.example"}),
                    15,
                )
            }

            with self.assertRaisesRegex(
                PermissionError,
                "provider policy values changed",
            ):
                production_bybit._bybit_policy_identity(
                    policy,
                    provider_environment="TESTNET",
                )
        finally:
            object.__setattr__(policy, "base_url", original_base_url)
            object.__setattr__(policy, "allowed_hosts", original_allowed_hosts)
            if had_reference:
                production_bybit._BYBIT_POLICY_IDENTITIES = original_reference
            else:
                try:
                    del production_bybit._BYBIT_POLICY_IDENTITIES
                except AttributeError:
                    pass


if __name__ == "__main__":
    unittest.main()
