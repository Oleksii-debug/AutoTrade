from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.durable_host_api import JournalBackedHostCommandStore
from mvp.autotrade_mvp.persistence import JournalStore


class DurableHostJournalAuthorityTests(unittest.TestCase):
    @staticmethod
    def _host(store: JournalStore) -> JournalBackedHostCommandStore:
        return JournalBackedHostCommandStore(
            store,
            account_id="acct",
            environment="SIMULATION",
            session_validator=lambda _session, _actor, _origin, _action: True,
            request_origin_provider=lambda: "LOCAL_UI",
        )

    def test_constructor_rejects_journal_store_subclass_before_method_dispatch(self):
        class ForgedStore(JournalStore):
            pass

        forged = object.__new__(ForgedStore)
        with self.assertRaisesRegex(TypeError, "exact JournalStore"):
            self._host(forged)

    def test_constructor_rejects_instance_shadowed_journal(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            store.load_events = lambda *_args, **_kwargs: []
            with self.assertRaisesRegex(TypeError, "shadow"):
                self._host(store)

    def test_post_construction_method_shadow_is_rejected_before_host_read(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            host = self._host(store)
            store.load_events = lambda *_args, **_kwargs: []
            with self.assertRaisesRegex(TypeError, "shadow"):
                _ = host.state_version

    def test_post_construction_journal_generation_swap_is_rejected(self):
        with TemporaryDirectory() as directory:
            selected = JournalStore(f"{directory}/selected.sqlite3")
            foreign = JournalStore(f"{directory}/foreign.sqlite3")
            host = self._host(selected)
            host._journal = foreign
            with self.assertRaisesRegex(
                PermissionError,
                "journal authority changed",
            ):
                _ = host.state_version


if __name__ == "__main__":
    unittest.main()
