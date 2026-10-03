from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.authority import AuthorityConflict, AuthorityService
from mvp.autotrade_mvp.persistence import JournalStore


class AuthorityServiceStoreUnforgeabilityTests(unittest.TestCase):
    def test_journal_store_subclass_cannot_enter_authority_replay(self):
        with TemporaryDirectory() as directory:
            calls = []

            class HostileStore(JournalStore):
                def load_events(self, *_args, **_kwargs):
                    calls.append("load_events")
                    raise AssertionError("hostile JournalStore callback executed")

            store = HostileStore(Path(directory) / "hostile.sqlite3")
            with self.assertRaises(TypeError):
                AuthorityService(store)
            self.assertEqual(calls, [])

    def test_caller_cannot_retarget_authority_history_after_construction(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            selected = JournalStore(root / "selected.sqlite3")
            replacement = JournalStore(root / "replacement.sqlite3")
            service = AuthorityService(selected)

            service.store = replacement

            with self.assertRaises(AuthorityConflict):
                service._restore_journal()

    def test_store_instance_method_shadow_is_rejected_before_callback(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "selected.sqlite3")
            calls = []

            def hostile(*_args, **_kwargs):
                calls.append("load_events")
                raise AssertionError("instance shadow callback executed")

            vars(store)["load_events"] = hostile
            with self.assertRaises((AuthorityConflict, TypeError)):
                AuthorityService(store)
            self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
