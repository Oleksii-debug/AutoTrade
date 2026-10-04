from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import mvp.autotrade_mvp.recovery_dispatch as recovery_dispatch_module
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.recovery import RecoveryController
from mvp.autotrade_mvp.recovery_dispatch import build_recovery_issued_dispatcher


_FORGED_RECOVERY_CALLS: list[tuple[object, str, str]] = []
_FORGED_RECONCILIATION_CALLS: list[object] = []
_FORGED_DIGEST_CALLS: list[object] = []


def _forged_recover_uncertainty(
    self,
    *,
    environment: str,
    account_id: str,
):
    _FORGED_RECOVERY_CALLS.append((self, environment, account_id))
    return ()


def _forged_reconciliation_reader(
    store,
    *,
    provider_id: str,
    account_id: str,
    environment: str,
):
    _FORGED_RECONCILIATION_CALLS.append(
        (store, provider_id, account_id, environment)
    )
    return None


def _forged_payload_digest(value):
    _FORGED_DIGEST_CALLS.append(value)
    return "sha256:" + ("0" * 64)


class RecoveryDispatchDurableReadinessAuthorityTests(unittest.TestCase):
    def _authority(self, directory: str):
        journal = JournalStore(Path(directory) / "journal.sqlite3")
        recovery = RecoveryController(
            owner_store=journal,
            owner_scope="PAPER:acct",
        )
        recovery.start("host-a")
        dispatcher = build_recovery_issued_dispatcher(
            recovery,
            journal,
            environment="PAPER",
            account_id="acct",
        )
        return journal, recovery, dispatcher

    def test_recovery_uncertainty_class_rebinding_fails_before_forged_code(self):
        with TemporaryDirectory() as directory:
            _journal, _recovery, dispatcher = self._authority(directory)
            original = RecoveryController.recover_durable_submission_uncertainty
            _FORGED_RECOVERY_CALLS.clear()
            RecoveryController.recover_durable_submission_uncertainty = (
                _forged_recover_uncertainty
            )
            try:
                with self.assertRaisesRegex(
                    PermissionError,
                    "recovery uncertainty authority changed",
                ):
                    dispatcher._require_issued_authority()
            finally:
                RecoveryController.recover_durable_submission_uncertainty = original

            self.assertEqual(_FORGED_RECOVERY_CALLS, [])

    def test_recovery_uncertainty_same_object_code_mutation_fails_before_execution(self):
        with TemporaryDirectory() as directory:
            _journal, _recovery, dispatcher = self._authority(directory)
            canonical = recovery_dispatch_module._CANONICAL_RECOVER_DURABLE_UNCERTAINTY
            original_code = canonical.__code__
            _FORGED_RECOVERY_CALLS.clear()
            try:
                canonical.__code__ = _forged_recover_uncertainty.__code__
                with self.assertRaisesRegex(
                    PermissionError,
                    "recovery uncertainty authority code changed",
                ):
                    dispatcher._require_issued_authority()
            finally:
                canonical.__code__ = original_code

            self.assertEqual(_FORGED_RECOVERY_CALLS, [])

    def test_reconciliation_reader_rebinding_fails_before_forged_code(self):
        with TemporaryDirectory() as directory:
            _journal, _recovery, dispatcher = self._authority(directory)
            original = (
                recovery_dispatch_module.load_latest_reconciliation_checkpoint_for_scope
            )
            _FORGED_RECONCILIATION_CALLS.clear()
            recovery_dispatch_module.load_latest_reconciliation_checkpoint_for_scope = (
                _forged_reconciliation_reader
            )
            try:
                with self.assertRaisesRegex(
                    PermissionError,
                    "reconciliation reader authority changed",
                ):
                    dispatcher._require_issued_authority()
            finally:
                recovery_dispatch_module.load_latest_reconciliation_checkpoint_for_scope = (
                    original
                )

            self.assertEqual(_FORGED_RECONCILIATION_CALLS, [])

    def test_reconciliation_reader_same_object_code_mutation_fails_before_execution(self):
        with TemporaryDirectory() as directory:
            _journal, _recovery, dispatcher = self._authority(directory)
            canonical = recovery_dispatch_module._CANONICAL_LOAD_LATEST_RECONCILIATION
            original_code = canonical.__code__
            _FORGED_RECONCILIATION_CALLS.clear()
            try:
                canonical.__code__ = _forged_reconciliation_reader.__code__
                with self.assertRaisesRegex(
                    PermissionError,
                    "reconciliation reader authority code changed",
                ):
                    dispatcher._require_issued_authority()
            finally:
                canonical.__code__ = original_code

            self.assertEqual(_FORGED_RECONCILIATION_CALLS, [])

    def test_payload_digest_rebinding_fails_before_forged_code(self):
        with TemporaryDirectory() as directory:
            _journal, _recovery, dispatcher = self._authority(directory)
            original = recovery_dispatch_module.payload_digest
            _FORGED_DIGEST_CALLS.clear()
            recovery_dispatch_module.payload_digest = _forged_payload_digest
            try:
                with self.assertRaisesRegex(
                    PermissionError,
                    "payload digest authority changed",
                ):
                    dispatcher._require_issued_authority()
            finally:
                recovery_dispatch_module.payload_digest = original

            self.assertEqual(_FORGED_DIGEST_CALLS, [])

    def test_payload_digest_same_object_code_mutation_fails_before_execution(self):
        with TemporaryDirectory() as directory:
            _journal, _recovery, dispatcher = self._authority(directory)
            canonical = recovery_dispatch_module._CANONICAL_PAYLOAD_DIGEST
            original_code = canonical.__code__
            _FORGED_DIGEST_CALLS.clear()
            try:
                canonical.__code__ = _forged_payload_digest.__code__
                with self.assertRaisesRegex(
                    PermissionError,
                    "payload digest authority code changed",
                ):
                    dispatcher._require_issued_authority()
            finally:
                canonical.__code__ = original_code

            self.assertEqual(_FORGED_DIGEST_CALLS, [])


if __name__ == "__main__":
    unittest.main()
