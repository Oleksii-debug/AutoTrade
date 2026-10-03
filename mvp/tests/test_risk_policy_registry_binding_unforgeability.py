from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import weakref

from mvp.autotrade_mvp import risk_policy_authority as authority
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.risk_policy_authority import (
    DurableRiskPolicyRegistry,
    RiskPolicyAuthorityError,
)


class RiskPolicyRegistryBindingUnforgeabilityTests(unittest.TestCase):
    def test_reinitialization_cannot_retarget_original_policy_history(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            selected = JournalStore(root / "selected.sqlite3")
            replacement = JournalStore(root / "replacement.sqlite3")
            registry = DurableRiskPolicyRegistry(selected)

            with self.assertRaisesRegex(
                RiskPolicyAuthorityError,
                "already initialized|already established",
            ):
                registry.__init__(replacement)

            bound_store, _bound_identity = registry._journal_store_authority()
            self.assertIs(bound_store, selected)
            self.assertIs(registry.store, selected)

    def test_imported_registry_binding_map_cannot_retarget_policy_history(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            selected = JournalStore(root / "selected.sqlite3")
            replacement = JournalStore(root / "replacement.sqlite3")
            registry = DurableRiskPolicyRegistry(selected)
            replacement_identity = authority._canonical_journal_authority_snapshot(
                replacement
            )

            # Visible diagnostics and the module-global binding map are all
            # caller-writable Python state. Retargeting them together must not
            # redefine which durable policy history the registry originally chose.
            registry.store = replacement
            registry._journal_store_identity = replacement_identity
            authority._RISK_POLICY_REGISTRY_BINDINGS[id(registry)] = (
                weakref.ref(registry),
                weakref.ref(replacement),
                replacement_identity,
            )

            with self.assertRaises(RiskPolicyAuthorityError):
                registry._journal_store_authority()

            # Restoring only the caller-visible diagnostics must recover the
            # original selected store even while the imported global map remains
            # forged; the authoritative composition lives in closure-owned state.
            selected_identity = authority._canonical_journal_authority_snapshot(
                selected
            )
            registry.store = selected
            registry._journal_store_identity = selected_identity
            bound_store, bound_identity = registry._journal_store_authority()
            self.assertIs(bound_store, selected)
            self.assertEqual(bound_identity, selected_identity)


if __name__ == "__main__":
    unittest.main()
