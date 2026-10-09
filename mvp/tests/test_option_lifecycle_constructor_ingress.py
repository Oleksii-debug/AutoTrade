from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.instruments import InstrumentRegistry
from mvp.autotrade_mvp.option_lifecycle import DurableOptionLifecycleAuthority
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.provider_activity_accounting import DurableProviderEconomicBook


class TrapRegistry(InstrumentRegistry):
    pass


class TrapFrozenSet(frozenset):
    iter_calls = 0

    def __iter__(self):
        type(self).iter_calls += 1
        raise AssertionError("lifecycle endpoint iteration must not execute")


class TrapText(str):
    strip_calls = 0

    def strip(self, *args, **kwargs):
        type(self).strip_calls += 1
        raise AssertionError("caller text methods must not execute")


class DurableOptionLifecycleConstructorIngressTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = JournalStore(self.directory.name + "/journal.sqlite3")
        self.registry = InstrumentRegistry()
        self.book = DurableProviderEconomicBook(
            self.store,
            provider_id="TEST_PROVIDER",
            account_id="paper-1",
            environment="SIMULATION",
        )
        TrapFrozenSet.iter_calls = 0
        TrapText.strip_calls = 0

    def authority(
        self,
        *,
        registry=None,
        lifecycle_endpoints=None,
        permission_scope="ACCOUNT.READ",
    ):
        return DurableOptionLifecycleAuthority(
            self.store,
            registry=self.registry if registry is None else registry,
            economic_book=self.book,
            evidence_resolver=lambda _reference: None,
            lifecycle_endpoints=(
                frozenset({"/v5/account/option-lifecycle"})
                if lifecycle_endpoints is None
                else lifecycle_endpoints
            ),
            permission_scope=permission_scope,
        )

    def test_registry_subclass_is_rejected_at_constructor_boundary(self):
        with self.assertRaisesRegex(TypeError, "exact InstrumentRegistry"):
            self.authority(registry=TrapRegistry())

    def test_frozenset_subclass_is_rejected_without_iteration(self):
        endpoints = TrapFrozenSet({"/v5/account/option-lifecycle"})

        with self.assertRaisesRegex(TypeError, "exact non-empty frozenset"):
            self.authority(lifecycle_endpoints=endpoints)

        self.assertEqual(TrapFrozenSet.iter_calls, 0)

    def test_endpoint_text_subclass_is_rejected_without_strip(self):
        endpoint = TrapText("/v5/account/option-lifecycle")
        endpoints = frozenset({endpoint})

        with self.assertRaisesRegex(TypeError, "contain exact strings"):
            self.authority(lifecycle_endpoints=endpoints)

        self.assertEqual(TrapText.strip_calls, 0)

    def test_permission_scope_text_subclass_is_rejected_without_strip(self):
        permission_scope = TrapText("ACCOUNT.READ")

        with self.assertRaisesRegex(TypeError, "permission_scope must be exact str"):
            self.authority(permission_scope=permission_scope)

        self.assertEqual(TrapText.strip_calls, 0)


if __name__ == "__main__":
    unittest.main()
