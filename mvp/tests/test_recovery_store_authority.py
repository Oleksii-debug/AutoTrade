from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp.dispatch import GuardedDispatcher
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.recovery import HostState, RecoveryController


class RecoveryStoreAuthorityTests(unittest.TestCase):
    def test_controller_rejects_journal_store_subclass(self):
        class ForgedStore(JournalStore):
            pass

        forged = object.__new__(ForgedStore)
        with self.assertRaisesRegex(TypeError, "exact JournalStore"):
            RecoveryController(
                owner_store=forged,
                owner_scope="SIMULATION:acct",
            )

    def test_controller_rejects_instance_method_shadow_at_construction(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            store.load_events = lambda *_args, **_kwargs: []
            with self.assertRaisesRegex(TypeError, "shadow"):
                RecoveryController(
                    owner_store=store,
                    owner_scope="SIMULATION:acct",
                )

    def test_selected_store_shadow_after_construction_fails_before_owner_read(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            recovery = RecoveryController(
                owner_store=store,
                owner_scope="SIMULATION:acct",
            )
            store.load_events = lambda *_args, **_kwargs: []
            with self.assertRaisesRegex(TypeError, "shadow"):
                recovery.durable_owner_chain()

    def test_build_dispatcher_requires_exact_selected_store_generation(self):
        with TemporaryDirectory() as directory:
            selected = JournalStore(f"{directory}/selected.sqlite3")
            foreign = JournalStore(f"{directory}/foreign.sqlite3")
            recovery = RecoveryController(
                owner_store=selected,
                owner_scope="SIMULATION:acct",
            )
            owner = recovery.start("host-a")

            dispatcher = recovery.build_guarded_dispatcher(
                selected,
                environment="SIMULATION",
                account_id="acct",
            )
            self.assertIsInstance(dispatcher, GuardedDispatcher)
            self.assertEqual(dispatcher.owner_token, owner.owner_id)
            self.assertEqual(dispatcher.owner_epoch, owner.epoch)

            with self.assertRaisesRegex(
                PermissionError,
                "selected recovery authority",
            ):
                recovery.build_guarded_dispatcher(
                    foreign,
                    environment="SIMULATION",
                    account_id="acct",
                )

    def test_foreign_reconciliation_scope_fails_before_checkpoint_lookup(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            recovery = RecoveryController(
                owner_store=store,
                owner_scope="SIMULATION:acct",
            )
            recovery.start("host-a")
            with patch(
                "mvp.autotrade_mvp.recovery.load_reconciliation_checkpoint_for_readiness"
            ) as loader:
                with self.assertRaisesRegex(
                    PermissionError,
                    "scope does not match",
                ):
                    recovery.record_reconciliation_checkpoint(
                        reconciliation_id="foreign",
                        provider_id="SIM",
                        account_id="other",
                        environment="SIMULATION",
                    )
                loader.assert_not_called()


    def _mark_ready(self, recovery: RecoveryController) -> None:
        checkpoint = {
            "event_id": "ready-event",
            "payload_hash": "sha256:" + "a" * 64,
            "journal_sequence": 1,
            "payload": {
                "provider_id": "SIM",
                "account_id": "acct",
                "environment": "SIMULATION",
                "complete": True,
                "snapshot_consistent": True,
                "activity_coverage_complete": True,
                "blocking_resources": [],
                "submission_resolutions": [],
            },
        }
        with patch(
            "mvp.autotrade_mvp.recovery.load_reconciliation_checkpoint_for_readiness",
            return_value=checkpoint,
        ):
            recovery.record_reconciliation_checkpoint(
                reconciliation_id="ready",
                provider_id="SIM",
                account_id="acct",
                environment="SIMULATION",
            )
        self.assertEqual(recovery.state, HostState.READY)

    def _append_unknown_after_ready(
        self,
        store: JournalStore,
        recovery: RecoveryController,
        *,
        attempt_id: str,
    ) -> None:
        owner = recovery.owner
        self.assertIsNotNone(owner)
        dispatcher = recovery.build_guarded_dispatcher(
            store,
            environment="SIMULATION",
            account_id="acct",
        )

        def ambiguous(_client_id, _request, final_guard):
            final_guard()
            raise TimeoutError("ambiguous provider result")

        outcome = dispatcher.dispatch(
            attempt_id=attempt_id,
            intent_id="intent-" + attempt_id,
            intent_hash="hash-" + attempt_id,
            provider="sim",
            request={"side": "BUY"},
            now="2026-10-02T05:54:00Z",
            authority_check=lambda _intent_hash, _now: (True, "allowed"),
            transport_send=ambiguous,
            sender_check=recovery.validate_sender,
        )
        self.assertEqual(outcome.status, "UNKNOWN")
        self.assertEqual(recovery.state, HostState.READY)

    def test_sender_validation_rescans_new_durable_unknown(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            recovery = RecoveryController(
                owner_store=store,
                owner_scope="SIMULATION:acct",
            )
            owner = recovery.start("host-a")
            self._mark_ready(recovery)
            self._append_unknown_after_ready(
                store,
                recovery,
                attempt_id="unknown-before-next-sender",
            )

            with self.assertRaisesRegex(PermissionError, "not ready|unresolved"):
                recovery.validate_sender(owner.owner_id, owner.epoch)
            self.assertEqual(recovery.state, HostState.DEGRADED)
            self.assertEqual(
                recovery.unresolved_attempts,
                {"unknown-before-next-sender"},
            )

    def test_admission_validation_rescans_new_durable_unknown(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            recovery = RecoveryController(
                owner_store=store,
                owner_scope="SIMULATION:acct",
            )
            owner = recovery.start("host-a")
            self._mark_ready(recovery)
            self._append_unknown_after_ready(
                store,
                recovery,
                attempt_id="unknown-before-next-admission",
            )

            with self.assertRaisesRegex(PermissionError, "not ready|unresolved"):
                recovery.validate_admission(owner.epoch)
            self.assertEqual(recovery.state, HostState.DEGRADED)
            self.assertEqual(
                recovery.unresolved_attempts,
                {"unknown-before-next-admission"},
            )


if __name__ == "__main__":
    unittest.main()
