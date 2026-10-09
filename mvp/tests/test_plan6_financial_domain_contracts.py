"""Plan-6 Section 4 offline: bind existing provider financial-domain contracts.

The seven rows are discovery/fixture requirements, NOT trading entitlement,
provider-backed financial truth, a second ledger, or source of signed evidence.
The actual authority remains in each existing financial/provider module.
"""
from __future__ import annotations

from importlib import import_module
from inspect import getattr_static
from types import MappingProxyType
import unittest

# Keep every domain on its pre-existing authoritative implementation. This
# intentionally declares no adapter class, account, credential or order sender.
_DOMAIN_COMPONENTS = MappingProxyType({
    "SETTLEMENT": ("durable_settlement", "DurableSettlementBook"),
    "FUTURES": ("futures", "FuturesSettlementEvidence"),
    "PERPETUAL_FUNDING": ("perpetual_funding", "DurablePerpetualFundingAuthority"),
    "SECURITIES_BORROW": ("securities_borrow", "DurableBorrowRecallProjection"),
    "FINANCING": ("durable_financing", "DurableFinancingBook"),
    "CORPORATE_ACTIONS": ("corporate_action_evidence", "DurableCorporateActionEvidenceStore"),
    "OPTION_LIFECYCLE": ("option_lifecycle", "DurableOptionLifecycleAuthority"),
})


class Plan6FinancialDomainContractTests(unittest.TestCase):
    def test_every_financial_domain_consumes_existing_canonical_implementation(self):
        self.assertEqual(len(_DOMAIN_COMPONENTS), 7)
        for name, (module_name, exported_name) in _DOMAIN_COMPONENTS.items():
            with self.subTest(domain=name):
                module = import_module("mvp.autotrade_mvp." + module_name)
                # getattr_static avoids user-defined module/property callbacks.
                definition = getattr_static(module, exported_name)
                self.assertIs(type(definition), type)
                self.assertEqual(definition.__module__, module.__name__)

    def test_contract_inventory_is_read_only_and_non_authorizing(self):
        self.assertIs(type(_DOMAIN_COMPONENTS), MappingProxyType)
        with self.assertRaises(TypeError):
            _DOMAIN_COMPONENTS["PROVIDER_TRADE"] = ("dispatch", "GuardedDispatcher")
        for module_name, export in _DOMAIN_COMPONENTS.values():
            self.assertNotIn(export, {"ProviderCandidate", "FinancialSendAuthority"})
            self.assertNotEqual(module_name, "dispatch")

    def test_financial_provider_consumers_retain_one_ledger_and_no_send_authority(self):
        required = {
            "durable_settlement": "not a second cash ledger",
            "perpetual_funding": "not a second ledger",
            "option_lifecycle": "does not create another ledger",
        }
        for name, statement in required.items():
            with self.subTest(module=name):
                module = import_module("mvp.autotrade_mvp." + name)
                self.assertIn(statement, (module.__doc__ or "").lower())
                self.assertNotIn("send_order", module.__dict__)

    def test_financing_module_is_importable_after_truncation_repair(self):
        # Regression: an earlier provider financing change accidentally retained
        # only an interior substring of the established module. Import must
        # recover the complete trusted source, not simply a class-name stub.
        module = import_module("mvp.autotrade_mvp.durable_financing")
        self.assertTrue(callable(getattr_static(module, "authenticated_financing_event")))
        self.assertTrue(callable(getattr_static(module, "_milliseconds_instant")))
        self.assertIs(type(getattr_static(module, "DurableFinancingBook")), type)


if __name__ == "__main__":
    unittest.main()
