from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp import production_host
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.production_financial_host import compose_financial_authority
from mvp.autotrade_mvp.production_host import ProductionHostConfig, ProductionHostRuntime
from mvp.autotrade_mvp.recovery import HostState, OwnerFence, RecoveryController
from mvp.autotrade_mvp.recovery_dispatch import (
    activate_recovery_takeover_target,
    build_recovery_issued_dispatcher,
    mark_recovery_takeover_source,
)


class _FenceStub:
    def __init__(self) -> None:
        self.released = False


class RecoveryTakeoverSourceSenderFenceTests(unittest.TestCase):
    def _controller(self, directory: str):
        journal = JournalStore(Path(directory) / "journal.sqlite3")
        recovery = RecoveryController(
            owner_store=journal,
            owner_scope="PAPER:acct",
        )
        source = recovery.start("host-old")
        return journal, recovery, source

    def _host(self, journal: JournalStore, *, host_id: str) -> ProductionHostRuntime:
        config = ProductionHostConfig(
            journal_path=journal.path,
            account_id="acct",
            environment="PAPER",
            host_id=host_id,
            bind_host="127.0.0.1",
            bind_port=19091,
            public_origin="http://127.0.0.1:19091",
        )
        return ProductionHostRuntime(
            config=config,
            journal=journal,
            application=object(),
            server=object(),
            instance_fence=_FenceStub(),
            admission_gate=object(),
            issuance_token=production_host._RUNTIME_ISSUANCE_TOKEN,
        )

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

    def test_production_restart_marks_existing_durable_owner_takeover_only(self) -> None:
        with TemporaryDirectory() as directory:
            journal, _old, source = self._controller(directory)
            runtime = compose_financial_authority(
                self._host(journal, host_id="host-new")
            )
            recovery = runtime.recovery_controller

            self.assertTrue(runtime.takeover_required)
            self.assertEqual(recovery.owner, source)
            self.assertIn("takeover_source_only", recovery.reason_codes)

            # Even hostile process-local readiness mutation cannot turn the
            # attached source generation back into an issued product sender.
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

    def test_manual_owner_advance_cannot_activate_sender(self) -> None:
        with TemporaryDirectory() as directory:
            journal, recovery, source = self._controller(directory)
            mark_recovery_takeover_source(recovery, source)
            target = OwnerFence("host-new", source.epoch + 1)

            # A durable owner event alone is not proof of credential revocation,
            # issuer-sealed takeover evidence, or a completed takeover.
            recovery._append_durable_owner(target)
            recovery.owner = target
            with self.assertRaises(TypeError):
                activate_recovery_takeover_target(
                    recovery,
                    source=source,
                    target=target,
                    takeover=None,
                    vault=None,
                )
            self.assertIn("takeover_source_only", recovery.reason_codes)
            with self.assertRaisesRegex(
                PermissionError,
                "takeover source owner cannot receive recovery-issued sender authority",
            ):
                recovery.owner = source
                build_recovery_issued_dispatcher(
                    recovery,
                    journal,
                    environment="PAPER",
                    account_id="acct",
                )

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
                    takeover=None,
                    vault=None,
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
