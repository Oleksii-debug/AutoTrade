from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import mvp.autotrade_mvp.recovery_dispatch as recovery_dispatch_module
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.recovery import RecoveryController
from mvp.autotrade_mvp.recovery_dispatch import build_recovery_issued_dispatcher


_FORGED_EFFECTFUL_CALLS: list[object] = []


def _forged_effectful_reader(store, *, environment: str, account_id: str) -> int:
    _FORGED_EFFECTFUL_CALLS.append((store, environment, account_id))
    return 0


class RecoveryDispatchEffectfulReaderAuthorityTests(unittest.TestCase):
    def _dispatcher(self, directory: str):
        journal = JournalStore(Path(directory) / "journal.sqlite3")
        recovery = RecoveryController(
            owner_store=journal,
            owner_scope="PAPER:acct",
        )
        recovery.start("host-a")
        return build_recovery_issued_dispatcher(
            recovery,
            journal,
            environment="PAPER",
            account_id="acct",
        )

    def test_effectful_reader_rebinding_fails_before_forged_code(self):
        with TemporaryDirectory() as directory:
            dispatcher = self._dispatcher(directory)
            original = recovery_dispatch_module._latest_effectful_submission_sequence
            _FORGED_EFFECTFUL_CALLS.clear()
            recovery_dispatch_module._latest_effectful_submission_sequence = (
                _forged_effectful_reader
            )
            try:
                with self.assertRaisesRegex(
                    PermissionError,
                    "effectful submission reader authority changed",
                ):
                    dispatcher._require_issued_authority()
            finally:
                recovery_dispatch_module._latest_effectful_submission_sequence = original

            self.assertEqual(_FORGED_EFFECTFUL_CALLS, [])

    def test_effectful_reader_same_object_code_mutation_fails_before_execution(self):
        with TemporaryDirectory() as directory:
            dispatcher = self._dispatcher(directory)
            canonical = (
                recovery_dispatch_module._CANONICAL_LATEST_EFFECTFUL_SUBMISSION_SEQUENCE
            )
            original_code = canonical.__code__
            _FORGED_EFFECTFUL_CALLS.clear()
            try:
                canonical.__code__ = _forged_effectful_reader.__code__
                with self.assertRaisesRegex(
                    PermissionError,
                    "effectful submission reader authority code changed",
                ):
                    dispatcher._require_issued_authority()
            finally:
                canonical.__code__ = original_code

            self.assertEqual(_FORGED_EFFECTFUL_CALLS, [])


if __name__ == "__main__":
    unittest.main()
