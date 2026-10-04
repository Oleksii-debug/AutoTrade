from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.recovery import HostState, OwnerFence, RecoveryController
from mvp.autotrade_mvp.recovery_dispatch import (
    activate_recovery_takeover_target,
    build_recovery_issued_dispatcher,
    mark_recovery_takeover_source,
)


class RecoveryTakeoverSourceSenderFenceTests(unittest.TestCase):
    def _controller(self, directory: str):
        journal = JournalStore(Path(directory) / "journal.sqlite3")
        recovery = RecoveryController(
            owner_store=journal,
            owner_scope="PAPER:acct",
        )
        source = recovery.start("host-old")
        return journal, recovery, source

    def test_takeover_source_cannot_regain_sender_after_forced_ready_state(self) -> None:
        with TemporaryDirectory() as directory:
            journal, recovery, source = self._controller(directory)
            mark_recovery_takeover_source(recovery, source)

            # Source reconciliation/readiness is not sender ownership transfer.
            recovery.provider_reconciled = True
            recovery.reason_codes.clear()
            recovery.state = HostState.READY

            with self.assertRaisesRegex(
                PermissionError,
                "takeover source owner cannot receive recovery-issued sender authority",
            ):
                build_recovery_issued_dispatcher(
                    recovery,
                    journal,
                    environment="PAPER",
                    account_id="acct",
                )

    def test_takeover_source_marker_must_match_current_durable_tail(self) -> None:
        with TemporaryDirectory() as directory:
            _journal, recovery, source = self._controller(directory)
            forged = OwnerFence("forged", source.epoch)
            with self.assertRaisesRegex(
                PermissionError,
                "not the attached recovery owner",
            ):
                mark_recovery_takeover_source(recovery, forged)

    def test_exact_next_durable_generation_releases_issuance_fence(self) -> None:
        with TemporaryDirectory() as directory:
            journal, recovery, source = self._controller(directory)
            mark_recovery_takeover_source(recovery, source)
            target = OwnerFence("host-new", source.epoch + 1)

            recovery._append_durable_owner(target)
            recovery.owner = target
            activated = activate_recovery_takeover_target(
                recovery,
                source=source,
                target=target,
            )

            self.assertEqual(activated, target)
            self.assertIs(recovery.state, HostState.RECOVERING)
            self.assertFalse(recovery.provider_reconciled)
            self.assertNotIn("takeover_source_only", recovery.reason_codes)
            self.assertEqual(recovery.durable_owner_chain()[-1], target)

            dispatcher = build_recovery_issued_dispatcher(
                recovery,
                journal,
                environment="PAPER",
                account_id="acct",
            )
            self.assertEqual(dispatcher.owner, target)

    def test_activation_rejects_skipped_generation_without_clearing_source_fence(self) -> None:
        with TemporaryDirectory() as directory:
            journal, recovery, source = self._controller(directory)
            mark_recovery_takeover_source(recovery, source)
            skipped = OwnerFence("host-new", source.epoch + 2)
            recovery.owner = skipped

            with self.assertRaisesRegex(
                PermissionError,
                "not the next owner generation",
            ):
                activate_recovery_takeover_target(
                    recovery,
                    source=source,
                    target=skipped,
                )

            recovery.owner = source
            with self.assertRaisesRegex(
                PermissionError,
                "takeover source owner cannot receive recovery-issued sender authority",
            ):
                build_recovery_issued_dispatcher(
                    recovery,
                    journal,
                    environment="PAPER",
                    account_id="acct",
                )


if __name__ == "__main__":
    unittest.main()
