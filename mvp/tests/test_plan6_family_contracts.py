"""Plan 6 / Section 3: static provider-family contract and fail-closed guard.

This checks the existing canonical registry and route compatibility table.
It is not an activation, entitlement, live transport or economic-evidence issuer.
"""
from __future__ import annotations

import unittest

from mvp.autotrade_mvp.provider_core import (
    PROVIDERS,
    ProviderCoreError,
    Surface,
    provider_definition,
)
from mvp.autotrade_mvp.provider_selection import ASSET_FAMILY_COMPATIBILITY


_EXPECTED_FAMILIES = {
    "BYBIT": frozenset({"SPOT", "MARGIN", "LINEAR_DERIVATIVES", "INVERSE_DERIVATIVES", "OPTIONS"}),
    "KRAKEN": frozenset({"SPOT", "MARGIN", "DERIVATIVES"}),
    "WHITEBIT": frozenset({"SPOT", "COLLATERAL", "FUTURES"}),
    "BINANCE": frozenset({"SPOT", "MARGIN", "USD_M", "COIN_M", "OPTIONS"}),
    "IBKR": frozenset({"EQUITIES", "FUTURES", "OPTIONS", "FX", "OTHER_ENTITLED"}),
    "ALPACA": frozenset({"EQUITIES", "CRYPTO", "OPTIONS"}),
}


class Plan6FamilyContractTests(unittest.TestCase):
    def test_all_six_families_match_existing_canonical_registry(self):
        self.assertEqual(set(PROVIDERS), set(_EXPECTED_FAMILIES))
        for provider_id, expected in _EXPECTED_FAMILIES.items():
            with self.subTest(provider=provider_id):
                definition = provider_definition(provider_id)
                self.assertEqual(definition.provider_id, provider_id)
                self.assertEqual(frozenset(definition.product_families), expected)
                self.assertEqual(set(definition.surfaces), set(Surface))
                self.assertTrue(definition.test_environment_note)

    def test_route_compatibility_is_a_subset_of_declared_provider_products(self):
        # Static compatibility is architectural metadata, never a grant of Q/C.
        for asset_class, pairs in ASSET_FAMILY_COMPATIBILITY.items():
            self.assertTrue(pairs, asset_class)
            for provider_id, product_family in pairs:
                with self.subTest(asset=asset_class, provider=provider_id, family=product_family):
                    self.assertIn(product_family, _EXPECTED_FAMILIES[provider_id])

    def test_unregistered_provider_fails_closed(self):
        for provider in ("UNKNOWN_BROKER", "", "BYBIT/OTHER"):
            with self.subTest(provider=provider):
                with self.assertRaises(ProviderCoreError):
                    provider_definition(provider)

    def test_provider_registry_does_not_export_activation_status(self):
        # No synthetic matrix entry may itself claim usable PAPER/LIVE authority.
        for definition in PROVIDERS.values():
            self.assertFalse(hasattr(definition, "is_qualified"))
            self.assertFalse(hasattr(definition, "trading_ready"))
            self.assertFalse(hasattr(definition, "account_activated"))


if __name__ == "__main__":
    unittest.main()
