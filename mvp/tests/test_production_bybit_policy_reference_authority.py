from __future__ import annotations

import unittest

from mvp.autotrade_mvp import production_bybit
from mvp.autotrade_mvp.provider_transport import BYBIT_V5_ENDPOINT_POLICIES


class _HostilePolicyReference:
    def __init__(self) -> None:
        self.get_calls = 0

    def get(self, *_args, **_kwargs):
        self.get_calls += 1
        raise AssertionError("module policy reference must not be consulted")


class _HostileEnvironment(str):
    equality_calls = 0

    def __eq__(self, other):
        type(self).equality_calls += 1
        raise AssertionError("environment callback must not execute")

    def __hash__(self):
        raise AssertionError("environment hash callback must not execute")


class ProductionBybitPolicyReferenceAuthorityTests(unittest.TestCase):
    def test_canonical_policy_identity_is_code_pinned_for_all_environments(self) -> None:
        expected = {
            "MAINNET": (
                "BYBIT",
                "LIVE",
                "https://api.bybit.com",
                frozenset({"api.bybit.com"}),
                15,
            ),
            "TESTNET": (
                "BYBIT",
                "PAPER",
                "https://api-testnet.bybit.com",
                frozenset({"api-testnet.bybit.com"}),
                15,
            ),
            "DEMO": (
                "BYBIT",
                "PAPER",
                "https://api-demo.bybit.com",
                frozenset({"api-demo.bybit.com"}),
                15,
            ),
        }
        for provider_environment, identity in expected.items():
            with self.subTest(provider_environment=provider_environment):
                self.assertEqual(
                    production_bybit._bybit_policy_identity(
                        BYBIT_V5_ENDPOINT_POLICIES[provider_environment],
                        provider_environment=provider_environment,
                    ),
                    identity,
                )

    def test_module_reference_injection_is_not_consulted(self) -> None:
        hostile_reference = _HostilePolicyReference()
        had_reference = hasattr(production_bybit, "_BYBIT_POLICY_IDENTITIES")
        original_reference = getattr(
            production_bybit,
            "_BYBIT_POLICY_IDENTITIES",
            None,
        )
        try:
            production_bybit._BYBIT_POLICY_IDENTITIES = hostile_reference
            identity = production_bybit._bybit_policy_identity(
                BYBIT_V5_ENDPOINT_POLICIES["TESTNET"],
                provider_environment="TESTNET",
            )
            self.assertEqual(identity[2], "https://api-testnet.bybit.com")
            self.assertEqual(hostile_reference.get_calls, 0)
        finally:
            if had_reference:
                production_bybit._BYBIT_POLICY_IDENTITIES = original_reference
            else:
                del production_bybit._BYBIT_POLICY_IDENTITIES

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
                del production_bybit._BYBIT_POLICY_IDENTITIES

    def test_non_exact_environment_is_rejected_without_callbacks(self) -> None:
        hostile = _HostileEnvironment("TESTNET")
        _HostileEnvironment.equality_calls = 0
        with self.assertRaisesRegex(
            PermissionError,
            "provider environment authority is not canonical",
        ):
            production_bybit._bybit_policy_identity(
                BYBIT_V5_ENDPOINT_POLICIES["TESTNET"],
                provider_environment=hostile,
            )
        self.assertEqual(_HostileEnvironment.equality_calls, 0)


if __name__ == "__main__":
    unittest.main()
