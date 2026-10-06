from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import mvp.autotrade_mvp.recovery_dispatch as recovery_dispatch_module
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.recovery import OwnerFence, RecoveryController
from mvp.autotrade_mvp.recovery_dispatch import (
    activate_recovery_takeover_target,
    build_recovery_issued_dispatcher,
    mark_recovery_takeover_source,
)


_FORGED_READER_CALLS: list[object] = []


def _forged_takeover_source_reader(recovery):
    _FORGED_READER_CALLS.append(recovery)
    return None


class RecoveryTakeoverHelperAuthorityTests(unittest.TestCase):
    def _controller(self, root: str):
        store = JournalStore(Path(root) / "takeover-helper.sqlite")
        recovery = RecoveryController(
            owner_store=store,
            owner_scope="PAPER:acct",
        )
        source = recovery.start("host-old")
        return store, recovery, source

    def test_rebound_helper_cannot_hide_source_from_existing_dispatcher(self) -> None:
        with TemporaryDirectory() as root:
            store, recovery, source = self._controller(root)
            dispatcher = build_recovery_issued_dispatcher(
                recovery,
                store,
                environment="PAPER",
                account_id="acct",
            )
            mark_recovery_takeover_source(recovery, source)
            original = recovery_dispatch_module._takeover_source_owner
            forged_calls = []

            def forged_reader(_recovery):
                forged_calls.append("called")
                return None

            recovery_dispatch_module._takeover_source_owner = forged_reader
            try:
                with self.assertRaisesRegex(
                    PermissionError,
                    "takeover source owner cannot retain recovery-issued sender authority",
                ):
                    dispatcher._require_issued_authority()
            finally:
                recovery_dispatch_module._takeover_source_owner = original

            self.assertEqual(forged_calls, [])

    def test_same_helper_code_mutation_cannot_hide_source_marker(self) -> None:
        with TemporaryDirectory() as root:
            store, recovery, source = self._controller(root)
            dispatcher = build_recovery_issued_dispatcher(
                recovery,
                store,
                environment="PAPER",
                account_id="acct",
            )
            mark_recovery_takeover_source(recovery, source)
            canonical = recovery_dispatch_module._takeover_source_owner
            original_code = canonical.__code__
            _FORGED_READER_CALLS.clear()
            try:
                canonical.__code__ = _forged_takeover_source_reader.__code__
                with self.assertRaisesRegex(
                    PermissionError,
                    "takeover source owner cannot retain recovery-issued sender authority",
                ):
                    dispatcher._require_issued_authority()
            finally:
                canonical.__code__ = original_code

            self.assertEqual(_FORGED_READER_CALLS, [])

    def test_marker_key_global_rebinding_cannot_hide_source_from_issuance(self) -> None:
        with TemporaryDirectory() as root:
            store, recovery, source = self._controller(root)
            mark_recovery_takeover_source(recovery, source)
            original_key = recovery_dispatch_module._TAKEOVER_SOURCE_ATTR
            recovery_dispatch_module._TAKEOVER_SOURCE_ATTR = "_forged_takeover_key"
            try:
                with self.assertRaisesRegex(
                    PermissionError,
                    "takeover source owner cannot receive recovery-issued sender authority",
                ):
                    build_recovery_issued_dispatcher(
                        recovery,
                        store,
                        environment="PAPER",
                        account_id="acct",
                    )
            finally:
                recovery_dispatch_module._TAKEOVER_SOURCE_ATTR = original_key

    def test_mark_does_not_trust_rebound_takeover_reader(self) -> None:
        with TemporaryDirectory() as root:
            store, recovery, source = self._controller(root)
            original = recovery_dispatch_module._takeover_source_owner
            forged_calls = []

            def forged_reader(_recovery):
                forged_calls.append("called")
                return None

            recovery_dispatch_module._takeover_source_owner = forged_reader
            try:
                self.assertEqual(mark_recovery_takeover_source(recovery, source), source)
                with self.assertRaisesRegex(
                    PermissionError,
                    "takeover source owner cannot receive recovery-issued sender authority",
                ):
                    build_recovery_issued_dispatcher(
                        recovery,
                        store,
                        environment="PAPER",
                        account_id="acct",
                    )
            finally:
                recovery_dispatch_module._takeover_source_owner = original

            self.assertEqual(forged_calls, [])

    def test_activation_does_not_trust_rebound_takeover_reader(self) -> None:
        with TemporaryDirectory() as root:
            store, recovery, source = self._controller(root)
            mark_recovery_takeover_source(recovery, source)
            target = OwnerFence("host-new", source.epoch + 1)
            recovery._append_durable_owner(target)
            recovery.owner = target
            original = recovery_dispatch_module._takeover_source_owner
            forged_calls = []

            def forged_reader(_recovery):
                forged_calls.append("called")
                return source

            recovery_dispatch_module._takeover_source_owner = forged_reader
            try:
                self.assertEqual(
                    activate_recovery_takeover_target(
                        recovery,
                        source=source,
                        target=target,
                    ),
                    target,
                )
                dispatcher = build_recovery_issued_dispatcher(
                    recovery,
                    store,
                    environment="PAPER",
                    account_id="acct",
                )
                self.assertEqual(dispatcher.owner, target)
            finally:
                recovery_dispatch_module._takeover_source_owner = original

            self.assertEqual(forged_calls, [])


if __name__ == "__main__":
    unittest.main()
