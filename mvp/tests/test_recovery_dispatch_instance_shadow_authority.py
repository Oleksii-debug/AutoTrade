from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.recovery import OwnerFence, RecoveryController
from mvp.autotrade_mvp.recovery_dispatch import build_recovery_issued_dispatcher


_FORGED_CALLS: list[str] = []


def _forged_durable_owner_chain():
    _FORGED_CALLS.append("durable_owner_chain")
    return ()


def _forged_require_current_durable_owner():
    _FORGED_CALLS.append("_require_current_durable_owner")


def _forged_store_append_event(*_args, **_kwargs):
    _FORGED_CALLS.append("JournalStore.append_event")
    return None


def _forged_store_load_events(*_args, **_kwargs):
    _FORGED_CALLS.append("JournalStore.load_events")
    return []


class _HostileScope(str):
    def __eq__(self, other):
        _FORGED_CALLS.append("scope_eq")
        return True


class RecoveryDispatchInstanceShadowAuthorityTests(unittest.TestCase):
    def _recovery(self, directory: str):
        journal = JournalStore(Path(directory) / "journal.sqlite3")
        recovery = RecoveryController(
            owner_store=journal,
            owner_scope="PAPER:acct",
        )
        recovery.start("host-a")
        return journal, recovery

    def test_issuance_rejects_durable_owner_reader_instance_shadow_before_execution(self):
        with TemporaryDirectory() as directory:
            journal, recovery = self._recovery(directory)
            _FORGED_CALLS.clear()
            recovery.durable_owner_chain = _forged_durable_owner_chain

            with self.assertRaisesRegex(
                PermissionError,
                "instance state shadows canonical authority",
            ):
                build_recovery_issued_dispatcher(
                    recovery,
                    journal,
                    environment="PAPER",
                    account_id="acct",
                )

            self.assertEqual(_FORGED_CALLS, [])

    def test_issued_dispatcher_rejects_nested_sender_guard_shadow_before_execution(self):
        with TemporaryDirectory() as directory:
            journal, recovery = self._recovery(directory)
            dispatcher = build_recovery_issued_dispatcher(
                recovery,
                journal,
                environment="PAPER",
                account_id="acct",
            )
            _FORGED_CALLS.clear()
            recovery._require_current_durable_owner = (
                _forged_require_current_durable_owner
            )

            with self.assertRaisesRegex(
                PermissionError,
                "instance state shadows canonical authority",
            ):
                dispatcher._require_issued_authority()

            self.assertEqual(_FORGED_CALLS, [])

    def test_issued_dispatcher_rejects_recovery_store_retarget(self):
        with TemporaryDirectory() as directory:
            journal, recovery = self._recovery(directory)
            dispatcher = build_recovery_issued_dispatcher(
                recovery,
                journal,
                environment="PAPER",
                account_id="acct",
            )
            replacement = JournalStore(Path(directory) / "other.sqlite3")
            recovery._owner_store = replacement

            with self.assertRaisesRegex(
                PermissionError,
                "recovery owner journal binding changed",
            ):
                dispatcher._require_issued_authority()

    def test_issued_dispatcher_rejects_scope_retarget_without_hostile_equality(self):
        with TemporaryDirectory() as directory:
            journal, recovery = self._recovery(directory)
            dispatcher = build_recovery_issued_dispatcher(
                recovery,
                journal,
                environment="PAPER",
                account_id="acct",
            )
            _FORGED_CALLS.clear()
            recovery._owner_scope = _HostileScope("PAPER:acct")

            with self.assertRaisesRegex(
                PermissionError,
                "recovery owner scope binding changed",
            ):
                dispatcher._require_issued_authority()

            self.assertEqual(_FORGED_CALLS, [])

    def test_owner_append_rejects_journal_write_shadow_before_execution(self):
        with TemporaryDirectory() as directory:
            journal, recovery = self._recovery(directory)
            _FORGED_CALLS.clear()
            journal.append_event = _forged_store_append_event

            with self.assertRaisesRegex(
                TypeError,
                "canonical JournalStore instance state is shadowed",
            ):
                recovery._append_durable_owner(OwnerFence("host-b", 2))

            self.assertEqual(_FORGED_CALLS, [])
            del journal.append_event
            self.assertEqual(
                recovery.durable_owner_chain(),
                (OwnerFence("host-a", 1),),
            )

    def test_owner_read_rejects_journal_reader_shadow_before_execution(self):
        with TemporaryDirectory() as directory:
            journal, recovery = self._recovery(directory)
            _FORGED_CALLS.clear()
            journal.load_events = _forged_store_load_events

            with self.assertRaisesRegex(
                TypeError,
                "canonical JournalStore instance state is shadowed",
            ):
                recovery.durable_owner_chain()

            self.assertEqual(_FORGED_CALLS, [])
            del journal.load_events
            self.assertEqual(
                recovery.durable_owner_chain(),
                (OwnerFence("host-a", 1),),
            )


if __name__ == "__main__":
    unittest.main()
