from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import mvp.autotrade_mvp.recovery_dispatch as recovery_dispatch_module
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.recovery import RecoveryController
from mvp.autotrade_mvp.recovery_dispatch import build_recovery_issued_dispatcher


_FORGED_VALIDATE_CALLS: list[tuple[str, int]] = []
_FORGED_SNAPSHOT_CALLS: list[object] = []


def _forged_validate_sender(_self, owner_id: str, owner_epoch: int) -> None:
    _FORGED_VALIDATE_CALLS.append((owner_id, owner_epoch))


def _forged_snapshot(store):
    _FORGED_SNAPSHOT_CALLS.append(store)
    return None


class _HostileText(str):
    calls = 0

    @classmethod
    def reset(cls) -> None:
        cls.calls = 0

    def _called(self):
        type(self).calls += 1
        raise AssertionError("hostile text callback executed")

    def strip(self, *args, **kwargs):
        del args, kwargs
        return self._called()

    def upper(self):
        return self._called()

    def __hash__(self):
        return self._called()

    def __eq__(self, other):
        del other
        return self._called()


class _HostileInt(int):
    calls = 0

    @classmethod
    def reset(cls) -> None:
        cls.calls = 0

    def __lt__(self, other):
        del other
        type(self).calls += 1
        raise AssertionError("hostile integer comparison executed")


class RecoveryIssuedDispatcherAuthorityIngressTests(unittest.TestCase):
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

    def test_environment_text_subclass_is_rejected_before_callbacks(self):
        with TemporaryDirectory() as directory:
            journal, recovery, _ = self._authority(directory)
            _HostileText.reset()
            with self.assertRaisesRegex(TypeError, "environment must be exact text"):
                build_recovery_issued_dispatcher(
                    recovery,
                    journal,
                    environment=_HostileText("PAPER"),
                    account_id="acct",
                )
            self.assertEqual(_HostileText.calls, 0)

    def test_account_text_subclass_is_rejected_before_callbacks(self):
        with TemporaryDirectory() as directory:
            journal, recovery, _ = self._authority(directory)
            _HostileText.reset()
            with self.assertRaisesRegex(TypeError, "account_id must be exact text"):
                build_recovery_issued_dispatcher(
                    recovery,
                    journal,
                    environment="PAPER",
                    account_id=_HostileText("acct"),
                )
            self.assertEqual(_HostileText.calls, 0)

    def test_prepared_lease_integer_subclass_is_rejected_before_comparison(self):
        with TemporaryDirectory() as directory:
            journal, recovery, _ = self._authority(directory)
            _HostileInt.reset()
            with self.assertRaisesRegex(ValueError, "positive exact integer"):
                build_recovery_issued_dispatcher(
                    recovery,
                    journal,
                    environment="PAPER",
                    account_id="acct",
                    prepared_lease_seconds=_HostileInt(60),
                )
            self.assertEqual(_HostileInt.calls, 0)

    def test_sender_validator_class_rebinding_fails_before_dispatch_callbacks(self):
        with TemporaryDirectory() as directory:
            _journal, _recovery, dispatcher = self._authority(directory)
            original = RecoveryController.validate_sender
            authority_calls = []
            wire_calls = []
            forged_calls = []

            def forged_validate_sender(_self, _owner_id, _owner_epoch):
                forged_calls.append("called")

            RecoveryController.validate_sender = forged_validate_sender
            try:
                with self.assertRaisesRegex(
                    PermissionError,
                    "validator authority changed",
                ):
                    dispatcher.dispatch(
                        attempt_id="attempt-rebind-validator",
                        intent_id="intent-rebind-validator",
                        intent_hash="hash-rebind-validator",
                        provider="BYBIT",
                        request={"symbol": "BTCUSDT"},
                        now="2026-10-04T02:10:00Z",
                        authority_check=lambda *_args: authority_calls.append("authority"),
                        transport_send=lambda *_args: wire_calls.append("wire"),
                    )
            finally:
                RecoveryController.validate_sender = original

            self.assertEqual(forged_calls, [])
            self.assertEqual(authority_calls, [])
            self.assertEqual(wire_calls, [])

    def test_sender_validator_same_object_code_mutation_fails_before_execution(self):
        with TemporaryDirectory() as directory:
            _journal, _recovery, dispatcher = self._authority(directory)
            canonical = recovery_dispatch_module._CANONICAL_VALIDATE_SENDER
            original_code = canonical.__code__
            _FORGED_VALIDATE_CALLS.clear()
            try:
                canonical.__code__ = _forged_validate_sender.__code__
                with self.assertRaisesRegex(
                    PermissionError,
                    "validator code changed",
                ):
                    dispatcher._require_issued_authority()
            finally:
                canonical.__code__ = original_code

            self.assertEqual(_FORGED_VALIDATE_CALLS, [])

    def test_journal_snapshot_binding_replacement_fails_before_forged_reader(self):
        with TemporaryDirectory() as directory:
            _journal, _recovery, dispatcher = self._authority(directory)
            original = recovery_dispatch_module._canonical_journal_authority_snapshot
            forged_calls = []

            def forged_snapshot(_store):
                forged_calls.append("called")
                return dispatcher._RecoveryIssuedDispatcher__store_snapshot

            recovery_dispatch_module._canonical_journal_authority_snapshot = forged_snapshot
            try:
                with self.assertRaisesRegex(
                    PermissionError,
                    "journal snapshot authority changed",
                ):
                    dispatcher._require_issued_authority()
            finally:
                recovery_dispatch_module._canonical_journal_authority_snapshot = original

            self.assertEqual(forged_calls, [])

    def test_journal_snapshot_same_object_code_mutation_fails_before_execution(self):
        with TemporaryDirectory() as directory:
            _journal, _recovery, dispatcher = self._authority(directory)
            canonical = recovery_dispatch_module._CANONICAL_JOURNAL_AUTHORITY_SNAPSHOT
            original_code = canonical.__code__
            _FORGED_SNAPSHOT_CALLS.clear()
            try:
                canonical.__code__ = _forged_snapshot.__code__
                with self.assertRaisesRegex(
                    PermissionError,
                    "snapshot authority code changed",
                ):
                    dispatcher._require_issued_authority()
            finally:
                canonical.__code__ = original_code

            self.assertEqual(_FORGED_SNAPSHOT_CALLS, [])

    def test_dispatch_time_authority_does_not_invoke_rebound_helper_aliases(self):
        with TemporaryDirectory() as directory:
            _journal, _recovery, dispatcher = self._authority(directory)
            original_require = recovery_dispatch_module._require_executable_authority
            original_trusted = recovery_dispatch_module._trusted_journal_authority_snapshot
            helper_calls = []

            def forged_require():
                helper_calls.append("require")

            def forged_trusted(_store):
                helper_calls.append("trusted")
                return None

            recovery_dispatch_module._require_executable_authority = forged_require
            recovery_dispatch_module._trusted_journal_authority_snapshot = forged_trusted
            try:
                dispatcher._require_issued_authority()
            finally:
                recovery_dispatch_module._require_executable_authority = original_require
                recovery_dispatch_module._trusted_journal_authority_snapshot = original_trusted

            self.assertEqual(helper_calls, [])

    def test_permissive_helper_rebinding_cannot_mask_validator_retarget(self):
        with TemporaryDirectory() as directory:
            _journal, _recovery, dispatcher = self._authority(directory)
            original_validator = RecoveryController.validate_sender
            original_require = recovery_dispatch_module._require_executable_authority
            helper_calls = []
            forged_calls = []

            def forged_validator(_self, _owner_id, _owner_epoch):
                forged_calls.append("validator")

            def forged_require():
                helper_calls.append("require")

            RecoveryController.validate_sender = forged_validator
            recovery_dispatch_module._require_executable_authority = forged_require
            try:
                with self.assertRaisesRegex(
                    PermissionError,
                    "validator authority changed",
                ):
                    dispatcher._require_issued_authority()
            finally:
                RecoveryController.validate_sender = original_validator
                recovery_dispatch_module._require_executable_authority = original_require

            self.assertEqual(helper_calls, [])
            self.assertEqual(forged_calls, [])

    def test_permissive_helpers_cannot_mask_journal_reader_retarget(self):
        with TemporaryDirectory() as directory:
            _journal, _recovery, dispatcher = self._authority(directory)
            original_reader = recovery_dispatch_module._canonical_journal_authority_snapshot
            original_require = recovery_dispatch_module._require_executable_authority
            original_trusted = recovery_dispatch_module._trusted_journal_authority_snapshot
            forged_calls = []

            def forged_reader(_store):
                forged_calls.append("reader")
                return None

            def forged_require():
                forged_calls.append("require")

            def forged_trusted(_store):
                forged_calls.append("trusted")
                return None

            recovery_dispatch_module._canonical_journal_authority_snapshot = forged_reader
            recovery_dispatch_module._require_executable_authority = forged_require
            recovery_dispatch_module._trusted_journal_authority_snapshot = forged_trusted
            try:
                with self.assertRaisesRegex(
                    PermissionError,
                    "journal snapshot authority changed",
                ):
                    dispatcher._require_issued_authority()
            finally:
                recovery_dispatch_module._canonical_journal_authority_snapshot = original_reader
                recovery_dispatch_module._require_executable_authority = original_require
                recovery_dispatch_module._trusted_journal_authority_snapshot = original_trusted

            self.assertEqual(forged_calls, [])

    def test_mutated_owner_epoch_boolean_is_not_treated_as_canonical_generation(self):
        with TemporaryDirectory() as directory:
            _journal, recovery, dispatcher = self._authority(directory)
            self.assertIsNotNone(recovery.owner)
            object.__setattr__(recovery.owner, "epoch", True)
            with self.assertRaisesRegex(
                PermissionError,
                "owner epoch is not a positive exact integer",
            ):
                dispatcher._require_issued_authority()


if __name__ == "__main__":
    unittest.main()
