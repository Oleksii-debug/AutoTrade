import unittest

from mvp.autotrade_mvp.provider_domain import (
    ProviderDomainError,
    normalize_provider_environment,
)


class _HostileText(str):
    def strip(self):
        raise AssertionError("hostile string callback executed")

    def upper(self):
        raise AssertionError("hostile string callback executed")


class ProviderDomainTests(unittest.TestCase):
    def test_bybit_paper_requires_explicit_testnet_or_demo(self):
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

    def test_bybit_live_accepts_only_mainnet(self):
        self.assertEqual(
            normalize_provider_environment(
                provider_id="BYBIT",
                environment="LIVE",
                provider_environment="MAINNET",
            ),
            "MAINNET",
        )
        for domain in ("TESTNET", "DEMO"):
            with self.subTest(domain=domain), self.assertRaisesRegex(
                ProviderDomainError,
                "does not match runtime environment",
            ):
                normalize_provider_environment(
                    provider_id="BYBIT",
                    environment="LIVE",
                    provider_environment=domain,
                )

    def test_bybit_testnet_and_demo_never_alias(self):
        testnet = normalize_provider_environment(
            provider_id="BYBIT",
            environment="PAPER",
            provider_environment="TESTNET",
        )
        demo = normalize_provider_environment(
            provider_id="BYBIT",
            environment="PAPER",
            provider_environment="DEMO",
        )
        self.assertNotEqual(testnet, demo)

    def test_other_provider_defaults_only_to_runtime_environment(self):
        self.assertEqual(
            normalize_provider_environment(
                provider_id="KRAKEN",
                environment="LIVE",
                provider_environment=None,
            ),
            "LIVE",
        )
        self.assertEqual(
            normalize_provider_environment(
                provider_id="KRAKEN",
                environment="PAPER",
                provider_environment="SANDBOX-1",
            ),
            "SANDBOX-1",
        )

    def test_noncanonical_runtime_or_provider_domain_fails_closed(self):
        for environment in ("", "PROD", "STAGING"):
            with self.subTest(environment=environment), self.assertRaises(ProviderDomainError):
                normalize_provider_environment(
                    provider_id="KRAKEN",
                    environment=environment,
                    provider_environment=None,
                )
        for domain in ("bad domain", "A/B", "x" * 65):
            with self.subTest(domain=domain), self.assertRaises(ProviderDomainError):
                normalize_provider_environment(
                    provider_id="KRAKEN",
                    environment="PAPER",
                    provider_environment=domain,
                )

    def test_hostile_string_subclasses_are_rejected_before_callbacks(self):
        for field, kwargs in (
            (
                "provider_id",
                dict(
                    provider_id=_HostileText("BYBIT"),
                    environment="PAPER",
                    provider_environment="TESTNET",
                ),
            ),
            (
                "environment",
                dict(
                    provider_id="BYBIT",
                    environment=_HostileText("PAPER"),
                    provider_environment="TESTNET",
                ),
            ),
            (
                "provider_environment",
                dict(
                    provider_id="BYBIT",
                    environment="PAPER",
                    provider_environment=_HostileText("TESTNET"),
                ),
            ),
        ):
            with self.subTest(field=field), self.assertRaises(ProviderDomainError):
                normalize_provider_environment(**kwargs)


if __name__ == "__main__":
    unittest.main()
