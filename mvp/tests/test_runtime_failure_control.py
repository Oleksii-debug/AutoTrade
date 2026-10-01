import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp.dispatch import GuardedDispatcher
from mvp.autotrade_mvp.persistence import JournalStore, payload_digest
from mvp.autotrade_mvp.reconciliation_journal import record_reconciliation_checkpoint
from mvp.tests.test_reconciliation_journal import reconciliation
from mvp.autotrade_mvp.recovery import (
    HostState,
    OutboundAttempt,
    RecoveryController,
    SendPhase,
)


class DurableReconciliationAuthorityTests(unittest.TestCase):
    def test_recovery_saved_identity_poison_fails_before_equality(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            controller = RecoveryController(
                owner_store=store,
                owner_scope="PAPER:acct",
            )
            controller.start("host-a")
            touched = []

            class HostileIdentity:
                def __eq__(self, _other):
                    touched.append("eq")
                    return True

                def __ne__(self, _other):
                    touched.append("ne")
                    return False

            controller._owner_store_identity = HostileIdentity()
            with self.assertRaisesRegex(
                TypeError,
                "selected recovery journal identity must be exact JournalStoreIdentity",
            ):
                controller.durable_owner_chain()
            self.assertEqual(touched, [])

    def test_recovery_store_authority_rejects_subclass_and_instance_shadow(self):
        touched = []

        class HostileJournalStore(JournalStore):
            def load_events(self, *_args, **_kwargs):
                touched.append("load")
                raise AssertionError("hostile recovery read executed")

        with TemporaryDirectory() as directory:
            hostile = HostileJournalStore(Path(directory) / "hostile.sqlite3")
            with self.assertRaisesRegex(TypeError, "canonical JournalStore"):
                RecoveryController(
                    owner_store=hostile,
                    owner_scope="PAPER:acct",
                )

            store = JournalStore(Path(directory) / "journal.sqlite3")
            controller = RecoveryController(
                owner_store=store,
                owner_scope="PAPER:acct",
            )
            controller.start("host-a")
            store.__dict__["load_events"] = lambda *_args, **_kwargs: touched.append(
                "shadow"
            )
            with self.assertRaisesRegex(TypeError, "instance state is shadowed"):
                controller.durable_owner_chain()
            del store.__dict__["load_events"]

        self.assertEqual(touched, [])

    def test_recovery_store_generation_mutation_fails_closed(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            controller = RecoveryController(
                owner_store=store,
                owner_scope="PAPER:acct",
            )
            owner = controller.start("host-a")
            other = JournalStore(Path(directory) / "other.sqlite3")
            original_path = store.path
            original_identity = store.store_identity
            store.path = other.path
            store._store_identity = other.store_identity
            with self.assertRaisesRegex(
                PermissionError,
                "recovery journal authority changed",
            ):
                controller.durable_owner_chain()
            store.path = original_path
            store._store_identity = original_identity
            self.assertEqual(controller.durable_owner_chain(), (owner,))

    def test_recovery_factory_binds_dispatcher_to_selected_store_and_scope(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            controller = RecoveryController(
                owner_store=store,
                owner_scope="PAPER:acct",
            )
            owner = controller.start("host-a")
            dispatcher = controller.build_guarded_dispatcher(
                store,
                environment="PAPER",
                account_id="acct",
            )
            self.assertIs(dispatcher.store, store)
            self.assertEqual(dispatcher.owner_token, owner.owner_id)
            self.assertEqual(dispatcher.owner_epoch, owner.epoch)

            with self.assertRaisesRegex(
                PermissionError,
                "scope does not match",
            ):
                controller.build_guarded_dispatcher(
                    store,
                    environment="PAPER",
                    account_id="other-account",
                )

            other = JournalStore(Path(directory) / "other.sqlite3")
            with self.assertRaisesRegex(
                PermissionError,
                "does not match selected recovery authority",
            ):
                controller.build_guarded_dispatcher(
                    other,
                    environment="PAPER",
                    account_id="acct",
                )

    def test_durable_controller_rejects_caller_consistency_boolean(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            controller = RecoveryController(
                owner_store=store,
                owner_scope="PAPER:test-account",
            )
            controller.start("host-a")
            with self.assertRaisesRegex(
                PermissionError,
                "journal-issued reconciliation checkpoint",
            ):
                controller.record_reconciliation(consistent=True)
            self.assertEqual(controller.state, HostState.RECOVERING)
            self.assertFalse(controller.provider_reconciled)

    def test_latest_owner_bound_checkpoint_is_durable_readiness_authority(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            controller = RecoveryController(
                owner_store=store,
                owner_scope="PAPER:test-account",
            )
            owner = controller.start("host-a")
            checkpoint = record_reconciliation_checkpoint(
                store,
                reconciliation_id="runtime-readiness",
                result=reconciliation(),
                observed_at="2026-09-24T19:00:00Z",
                host_id=owner.owner_id,
                owner_epoch=str(owner.epoch),
            )

            evidence = controller.record_reconciliation_checkpoint(
                reconciliation_id="runtime-readiness",
                provider_id="TEST_PROVIDER",
                account_id="test-account",
                environment="PAPER",
            )
            self.assertEqual(controller.state, HostState.READY)
            self.assertTrue(controller.provider_reconciled)
            self.assertEqual(evidence["event_id"], checkpoint["event_id"])
            self.assertEqual(evidence["payload_hash"], checkpoint["payload_hash"])
            self.assertEqual(
                evidence["journal_sequence"],
                checkpoint["journal_sequence"],
            )

            restarted = RecoveryController(
                owner_store=JournalStore(path),
                owner_scope="PAPER:test-account",
            )
            with self.assertRaisesRegex(
                PermissionError,
                "explicit takeover evidence",
            ):
                restarted.start("host-b")
            self.assertIsNone(restarted.owner)
            self.assertEqual(restarted.state, HostState.STOPPED)
            self.assertFalse(restarted.provider_reconciled)
            self.assertEqual(restarted.durable_owner_chain(), (owner,))

    def test_invalid_checkpoint_identity_cannot_mutate_controller_ready(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            controller = RecoveryController(
                owner_store=store,
                owner_scope="PAPER:test-account",
            )
            owner = controller.start("host-a")
            checkpoint = record_reconciliation_checkpoint(
                store,
                reconciliation_id="malformed-readiness",
                result=reconciliation(),
                observed_at="2026-09-24T19:00:00Z",
                host_id=owner.owner_id,
                owner_epoch=str(owner.epoch),
            )
            malformed = dict(checkpoint)
            malformed["event_id"] = ""

            with patch(
                "mvp.autotrade_mvp.recovery."
                "load_reconciliation_checkpoint_for_readiness",
                return_value=malformed,
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "durable identity is invalid",
                ):
                    controller.record_reconciliation_checkpoint(
                        reconciliation_id="malformed-readiness",
                        provider_id="TEST_PROVIDER",
                        account_id="test-account",
                        environment="PAPER",
                    )

            self.assertFalse(controller.provider_reconciled)
            self.assertEqual(controller.state, HostState.RECOVERING)
            self.assertIn(
                "startup_reconciliation_required",
                controller.reason_codes,
            )


class RuntimeRecoveryTests(unittest.TestCase):
    def _record_durable_ready(self, controller, *, reconciliation_id="runtime-readiness"):
        owner = controller.owner
        self.assertIsNotNone(owner)
        store_path = controller.durable_owner_store_path
        self.assertIsNotNone(store_path)
        store = JournalStore(store_path)
        result = reconciliation(
            account_id=controller.owner_scope.split(":", 1)[1],
            environment=controller.owner_scope.split(":", 1)[0],
        )
        record_reconciliation_checkpoint(
            store,
            reconciliation_id=reconciliation_id,
            result=result,
            observed_at="2026-09-24T19:00:00Z",
            host_id=owner.owner_id,
            owner_epoch=str(owner.epoch),
        )
        controller.record_reconciliation_checkpoint(
            reconciliation_id=reconciliation_id,
            provider_id=result.provider_id,
            account_id=result.account_id,
            environment=result.environment,
        )

    def test_direct_paper_dispatcher_cannot_mint_bound_sender_authority(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            controller = RecoveryController(
                owner_store=store,
                owner_scope="PAPER:acct",
            )
            owner = controller.start("host-a")
            self._record_durable_ready(
                controller,
                reconciliation_id="constructor-forgery-ready",
            )
            owner_head = controller._current_durable_owner_head()

            # A caller must not be able to mint the production sender authority
            # merely by passing an allow-all callback + a valid aggregate head to
            # the public GuardedDispatcher constructor.
            with self.assertRaisesRegex(
                PermissionError,
                "bound sender authority must be issued by recovery composition",
            ):
                GuardedDispatcher(
                    store,
                    environment="PAPER",
                    account_id="acct",
                    owner_token=owner.owner_id,
                    owner_epoch=owner.epoch,
                    bound_sender_check=lambda _owner, _epoch: None,
                    bound_sender_preconditions=(owner_head,),
                )

            self.assertEqual(
                JournalStore.load_events_by_aggregate_type(
                    store,
                    "submission_attempt",
                ),
                [],
            )

    def test_recovery_minted_dispatcher_cannot_replace_bound_sender_authority(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            controller = RecoveryController(
                owner_store=store,
                owner_scope="PAPER:acct",
            )
            owner = controller.start("host-a")
            self._record_durable_ready(
                controller,
                reconciliation_id="bound-sender-ready",
            )
            dispatcher = controller.build_guarded_dispatcher(
                store,
                environment="PAPER",
                account_id="acct",
            )
            self.assertEqual(dispatcher.owner_token, owner.owner_id)
            self.assertEqual(dispatcher.owner_epoch, owner.epoch)

            # Make the selected recovery authority non-admitting *after* the
            # dispatcher was minted. A caller-supplied no-op must not replace
            # the authority captured by build_guarded_dispatcher().
            controller.on_lease_expired()
            self.assertEqual(controller.state, HostState.DEGRADED)
            wire_calls = []
            bypass_calls = []

            def forbidden_wire(_client_id, _request, final_guard):
                final_guard()
                wire_calls.append("wire")
                return {"provider_order_id": "must-not-send"}

            outcome = dispatcher.dispatch(
                attempt_id="bound-sender-degraded",
                intent_id="bound-sender-intent",
                intent_hash="bound-sender-intent-hash",
                provider="sim",
                request={"side": "BUY"},
                now="2026-09-30T19:00:00Z",
                authority_check=lambda _intent_hash, _now: (True, "allowed"),
                transport_send=forbidden_wire,
                sender_check=lambda _owner, _epoch: bypass_calls.append("bypass"),
            )
            self.assertEqual(outcome.status, "BLOCKED")
            self.assertEqual(wire_calls, [])
            self.assertEqual(bypass_calls, [])
            submission_events = JournalStore.load_events_by_aggregate_type(
                store,
                "submission_attempt",
            )
            self.assertEqual(
                [event["event_type"] for event in submission_events],
                ["SubmissionPrepared", "SubmissionBlocked"],
            )

    def test_recovery_minted_dispatcher_rejects_post_mint_bound_sender_replacement(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            controller = RecoveryController(
                owner_store=store,
                owner_scope="PAPER:acct",
            )
            controller.start("host-a")
            self._record_durable_ready(
                controller,
                reconciliation_id="bound-sender-post-mint",
            )
            dispatcher = controller.build_guarded_dispatcher(
                store,
                environment="PAPER",
                account_id="acct",
            )

            # The durable owner head is intentionally unchanged.  Only the
            # selected recovery authority becomes non-admitting after mint.
            controller.on_lease_expired()
            self.assertEqual(controller.state, HostState.DEGRADED)

            # A caller holding the dispatcher must not be able to replace the
            # recovery-issued sender authority after construction.
            dispatcher._bound_sender_check = lambda _owner, _epoch: None
            wire_calls = []

            def forbidden_wire(_client_id, _request, final_guard):
                final_guard()
                wire_calls.append("wire")
                return {"provider_order_id": "must-not-send"}

            outcome = dispatcher.dispatch(
                attempt_id="bound-sender-post-mint",
                intent_id="bound-sender-post-mint-intent",
                intent_hash="bound-sender-post-mint-hash",
                provider="sim",
                request={"side": "BUY"},
                now="2026-09-30T19:00:30Z",
                authority_check=lambda _intent_hash, _now: (True, "allowed"),
                transport_send=forbidden_wire,
            )
            self.assertEqual(outcome.status, "BLOCKED")
            self.assertEqual(wire_calls, [])
            submission_events = JournalStore.load_events_by_aggregate_type(
                store,
                "submission_attempt",
            )
            self.assertEqual(
                [event["event_type"] for event in submission_events],
                ["SubmissionPrepared", "SubmissionBlocked"],
            )

    def test_recovery_minted_dispatcher_ignores_per_call_sender_mutation_hook(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            controller = RecoveryController(
                owner_store=store,
                owner_scope="PAPER:acct",
            )
            controller.start("host-a")
            self._record_durable_ready(
                controller,
                reconciliation_id="bound-sender-exclusive",
            )
            dispatcher = controller.build_guarded_dispatcher(
                store,
                environment="PAPER",
                account_id="acct",
            )
            mutation_calls = []
            wire_calls = []

            def caller_sender_hook(_owner, _epoch):
                mutation_calls.append("called")
                controller.on_lease_expired()

            def simulated_wire(_client_id, _request, final_guard):
                final_guard()
                wire_calls.append("wire")
                return {"provider_order_id": "simulated"}

            outcome = dispatcher.dispatch(
                attempt_id="bound-sender-exclusive",
                intent_id="bound-sender-exclusive-intent",
                intent_hash="bound-sender-exclusive-hash",
                provider="sim",
                request={"side": "BUY"},
                now="2026-09-30T19:01:00Z",
                authority_check=lambda _intent_hash, _now: (True, "allowed"),
                transport_send=simulated_wire,
                sender_check=caller_sender_hook,
            )
            self.assertEqual(outcome.status, "SENT")
            self.assertEqual(mutation_calls, [])
            self.assertEqual(wire_calls, ["wire"])
            self.assertEqual(controller.state, HostState.READY)
            submission_events = JournalStore.load_events_by_aggregate_type(
                store,
                "submission_attempt",
            )
            self.assertEqual(
                [event["event_type"] for event in submission_events],
                ["SubmissionPrepared", "SubmissionSending", "SubmissionSent"],
            )

    def test_journal_advance_after_sender_check_before_sending_is_zero_wire(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            controller = RecoveryController(
                owner_store=store,
                owner_scope="PAPER:acct",
            )
            controller.start("host-a")
            self._record_durable_ready(
                controller,
                reconciliation_id="journal-cut-race-ready",
            )
            dispatcher = controller.build_guarded_dispatcher(
                store,
                environment="PAPER",
                account_id="acct",
            )
            authority_calls = []
            wire_calls = []

            def authority_check(_intent_hash, _now):
                authority_calls.append("check")
                if len(authority_calls) == 2:
                    payload = {"status": "UNKNOWN", "source": "concurrent-attempt"}
                    store.append_event(
                        {
                            "event_id": "concurrent-unknown-after-sender-check",
                            "event_type": "SubmissionUnknown",
                            "aggregate_type": "submission_attempt",
                            "aggregate_id": "concurrent-attempt",
                            "aggregate_version": "1",
                            "payload": payload,
                            "payload_hash": payload_digest(payload),
                            "committed_at": "2026-09-30T19:01:59.500000+00:00",
                        }
                    )
                return True, "allowed"

            def forbidden_wire(_client_id, _request, final_guard):
                final_guard()
                wire_calls.append("wire")
                return {"provider_order_id": "must-not-send"}

            outcome = dispatcher.dispatch(
                attempt_id="journal-cut-race",
                intent_id="journal-cut-race-intent",
                intent_hash="journal-cut-race-hash",
                provider="sim",
                request={"side": "BUY"},
                now="2026-09-30T19:01:59Z",
                authority_check=authority_check,
                transport_send=forbidden_wire,
            )

            self.assertEqual(authority_calls, ["check", "check"])
            self.assertEqual(outcome.status, "BLOCKED")
            self.assertEqual(
                outcome.reason,
                "journal_cut_changed_before_sending",
            )
            self.assertEqual(wire_calls, [])
            attempt_events = store.load_events(
                "submission_attempt",
                dispatcher._aggregate_id("journal-cut-race"),
            )
            self.assertEqual(
                [event["event_type"] for event in attempt_events],
                ["SubmissionPrepared", "SubmissionBlocked"],
            )

    def test_owner_advance_between_sender_check_and_sending_append_is_zero_wire(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            controller = RecoveryController(
                owner_store=store,
                owner_scope="PAPER:acct",
            )
            controller.start("host-a")
            self._record_durable_ready(
                controller,
                reconciliation_id="owner-cut-race-ready",
            )
            dispatcher = controller.build_guarded_dispatcher(
                store,
                environment="PAPER",
                account_id="acct",
            )
            authority_calls = []
            wire_calls = []

            def authority_check(_intent_hash, _now):
                authority_calls.append("check")
                if len(authority_calls) == 2:
                    payload = {"owner_id": "host-b", "owner_epoch": "2"}
                    store.append_event(
                        {
                            "event_id": "concurrent-owner-epoch-2",
                            "event_type": "RecoveryOwnerChanged",
                            "aggregate_type": "recovery_owner",
                            "aggregate_id": "PAPER:acct",
                            "aggregate_version": "2",
                            "payload": payload,
                            "payload_hash": payload_digest(payload),
                            "committed_at": "2026-09-30T19:02:00+00:00",
                        }
                    )
                return True, "allowed"

            def forbidden_wire(_client_id, _request, final_guard):
                final_guard()
                wire_calls.append("wire")
                return {"provider_order_id": "must-not-send"}

            outcome = dispatcher.dispatch(
                attempt_id="owner-cut-race",
                intent_id="owner-cut-race-intent",
                intent_hash="owner-cut-race-hash",
                provider="sim",
                request={"side": "BUY"},
                now="2026-09-30T19:01:59Z",
                authority_check=authority_check,
                transport_send=forbidden_wire,
            )

            self.assertEqual(authority_calls, ["check", "check"])
            self.assertEqual(outcome.status, "BLOCKED")
            self.assertEqual(
                outcome.reason,
                "sender_head_changed_before_sending",
            )
            self.assertEqual(wire_calls, [])
            submission_events = JournalStore.load_events_by_aggregate_type(
                store,
                "submission_attempt",
            )
            self.assertEqual(
                [event["event_type"] for event in submission_events],
                ["SubmissionPrepared", "SubmissionBlocked"],
            )
            self.assertNotIn(
                "SubmissionSending",
                [event["event_type"] for event in submission_events],
            )

    def _ready(self):
        controller = RecoveryController()
        owner = controller.start("host-a")
        controller.record_reconciliation(consistent=True)
        self.assertEqual(controller.state, HostState.READY)
        return controller, owner


    def test_runtime_overload_blocks_admission_and_send_fail_closed(self):
        controller, owner = self._ready()
        controller.set_runtime_overloaded(True)

        self.assertEqual(controller.state, HostState.DEGRADED)
        self.assertIn("runtime_overload", controller.reason_codes)
        with self.assertRaises(PermissionError):
            controller.validate_admission(owner.epoch)
        with self.assertRaises(PermissionError):
            controller.validate_sender(owner.owner_id, owner.epoch)

        controller.set_runtime_overloaded(False)
        self.assertEqual(controller.state, HostState.READY)
        self.assertNotIn("runtime_overload", controller.reason_codes)
        controller.validate_admission(owner.epoch)
        controller.validate_sender(owner.owner_id, owner.epoch)

    def test_runtime_overload_cannot_bypass_stronger_durable_barriers(self):
        controller, owner = self._ready()
        controller.set_runtime_overloaded(True)
        controller.set_storage_writable(False)

        self.assertEqual(controller.state, HostState.BLOCKED)
        with self.assertRaisesRegex(PermissionError, "Durable journal is unavailable"):
            controller.validate_admission(owner.epoch)
        with self.assertRaises(PermissionError):
            controller.validate_sender(owner.owner_id, owner.epoch)

        controller.set_runtime_overloaded(False)
        self.assertEqual(controller.state, HostState.BLOCKED)

    def test_runtime_overload_cannot_mask_durable_unknown(self):
        controller, owner = self._ready()
        attempt = OutboundAttempt("overload-unknown", "intent-overload", owner.epoch)
        attempt.persist()
        attempt.mark_send_started("journal:overload-unknown")
        controller.note_unknown_send(attempt)

        controller.set_runtime_overloaded(True)
        self.assertEqual(controller.state, HostState.DEGRADED)
        self.assertIn("runtime_overload", controller.reason_codes)
        self.assertIn("overload-unknown", controller.unresolved_attempts)

        controller.set_runtime_overloaded(False)
        self.assertEqual(controller.state, HostState.DEGRADED)
        self.assertIn("overload-unknown", controller.unresolved_attempts)
        with self.assertRaises(PermissionError):
            controller.validate_sender(owner.owner_id, owner.epoch)

    def test_clearing_runtime_overload_cannot_reuse_invalidated_reconciliation(self):
        controller, owner = self._ready()
        controller.set_runtime_overloaded(True)
        controller.set_clock_trusted(False)
        self.assertEqual(controller.state, HostState.BLOCKED)
        self.assertFalse(controller.provider_reconciled)

        controller.set_clock_trusted(True)
        self.assertEqual(controller.state, HostState.DEGRADED)
        controller.set_runtime_overloaded(False)
        self.assertEqual(controller.state, HostState.RECOVERING)
        with self.assertRaises(PermissionError):
            controller.validate_sender(owner.owner_id, owner.epoch)

        controller.record_reconciliation(consistent=True)
        self.assertEqual(controller.state, HostState.READY)
        controller.validate_sender(owner.owner_id, owner.epoch)

    def test_runtime_overload_state_requires_real_boolean(self):
        controller, _ = self._ready()
        with self.assertRaises(TypeError):
            controller.set_runtime_overloaded(1)

    def test_start_never_reports_ready_before_reconciliation(self):
        controller = RecoveryController()
        controller.start("host-a")
        self.assertEqual(controller.state, HostState.RECOVERING)
        with self.assertRaises(PermissionError):
            controller.validate_admission(1)

    def test_crash_before_send_is_retryable_only_with_new_admission(self):
        attempt = OutboundAttempt("a1", "intent-1", 1)
        attempt.persist()
        self.assertEqual(attempt.phase, SendPhase.DURABLE)
        self.assertEqual(attempt.retry_disposition, "SAFE_WITH_NEW_ADMISSION")

    def test_crash_after_send_before_ack_requires_reconciliation(self):
        controller, owner = self._ready()
        attempt = OutboundAttempt("a1", "intent-1", owner.epoch)
        attempt.persist()
        attempt.mark_send_started("journal:send-started")
        controller.note_unknown_send(attempt)
        self.assertEqual(attempt.retry_disposition, "RECONCILE_FIRST")
        self.assertEqual(controller.state, HostState.DEGRADED)
        with self.assertRaises(PermissionError):
            controller.validate_sender(owner.owner_id, owner.epoch)

    def test_unknown_attempt_can_only_clear_with_external_resolution(self):
        controller, owner = self._ready()
        attempt = OutboundAttempt("a1", "intent-1", owner.epoch)
        attempt.persist()
        attempt.mark_send_started("journal:send-started")
        controller.note_unknown_send(attempt)
        with self.assertRaises(ValueError):
            controller.resolve_attempt(attempt)
        attempt.acknowledge("provider-7", "provider:ack")
        controller.resolve_attempt(attempt)
        controller.record_reconciliation(consistent=True)
        self.assertEqual(controller.state, HostState.READY)
        self.assertEqual(attempt.retry_disposition, "NEVER")

    def test_generic_reconciliation_cannot_erase_known_unknown_attempt(self):
        controller, owner = self._ready()
        attempt = OutboundAttempt("a1", "intent-1", owner.epoch)
        attempt.persist()
        attempt.mark_send_started("journal:send-started")
        controller.note_unknown_send(attempt)

        controller.record_reconciliation(consistent=True)
        self.assertEqual(controller.unresolved_attempts, {"a1"})
        self.assertEqual(controller.state, HostState.DEGRADED)

        controller.record_reconciliation(
            consistent=True,
            uncertainty=["unrelated-provider-gap"],
        )
        self.assertEqual(
            controller.unresolved_attempts,
            {"a1", "unrelated-provider-gap"},
        )
        self.assertEqual(controller.state, HostState.DEGRADED)

        attempt.acknowledge("provider-7", "provider:ack")
        controller.resolve_attempt(attempt)
        self.assertEqual(controller.unresolved_attempts, {"unrelated-provider-gap"})

        controller.record_reconciliation(consistent=True)
        self.assertEqual(controller.unresolved_attempts, set())
        self.assertEqual(controller.state, HostState.READY)

    def test_different_attempt_cannot_clear_sticky_unknown_by_reusing_id(self):
        controller, owner = self._ready()
        original = OutboundAttempt("a1", "intent-original", owner.epoch)
        original.persist()
        original.mark_send_started("journal:original-send")
        controller.note_unknown_send(original)

        forged = OutboundAttempt("a1", "intent-other", owner.epoch)
        forged.persist()
        forged.mark_send_started("journal:forged-send")
        forged.acknowledge("provider-forged", "provider:forged-ack")
        with self.assertRaisesRegex(ValueError, "identity does not match"):
            controller.resolve_attempt(forged)
        self.assertEqual(controller.unresolved_attempts, {"a1"})
        self.assertEqual(controller.state, HostState.DEGRADED)

        same_identity_wrong_evidence = OutboundAttempt(
            "a1",
            "intent-original",
            owner.epoch,
        )
        same_identity_wrong_evidence.persist()
        same_identity_wrong_evidence.mark_send_started("journal:different-send")
        same_identity_wrong_evidence.reject("provider:rejected")
        with self.assertRaisesRegex(ValueError, "original send evidence"):
            controller.resolve_attempt(same_identity_wrong_evidence)
        self.assertEqual(controller.unresolved_attempts, {"a1"})

    def test_attempt_identity_and_reconciliation_uncertainty_are_strict(self):
        for kwargs in (
            {"attempt_id": "", "intent_id": "i1", "owner_epoch": 1},
            {"attempt_id": "a1", "intent_id": "", "owner_epoch": 1},
            {"attempt_id": "a1", "intent_id": "i1", "owner_epoch": True},
            {"attempt_id": "a1", "intent_id": "i1", "owner_epoch": 0},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                OutboundAttempt(**kwargs)

        controller = RecoveryController()
        controller.start("host-a")
        for uncertainty in ((1,), ("",), (None,)):
            with self.subTest(uncertainty=uncertainty), self.assertRaisesRegex(
                ValueError,
                "uncertainty identities",
            ):
                controller.record_reconciliation(
                    consistent=True,
                    uncertainty=uncertainty,
                )
        self.assertEqual(controller.state, HostState.RECOVERING)

    def test_absence_requires_independent_evidence_before_retry(self):
        attempt = OutboundAttempt("a1", "intent-1", 1)
        attempt.persist()
        attempt.mark_send_started("journal:send-started")
        with self.assertRaises(ValueError):
            attempt.prove_absent(["orders:none"])
        with self.assertRaisesRegex(ValueError, "independent evidence"):
            attempt.prove_absent(["orders:none", "orders:none"])
        self.assertEqual(attempt.phase, SendPhase.SENT_UNKNOWN)
        self.assertEqual(attempt.retry_disposition, "RECONCILE_FIRST")
        attempt.prove_absent(["orders:none", "history:none"])
        self.assertEqual(attempt.phase, SendPhase.PROVEN_ABSENT)
        self.assertEqual(attempt.retry_disposition, "SAFE_WITH_NEW_ADMISSION")

    def test_full_disk_blocks_new_financial_admission_until_reconciled_after_restore(self):
        controller, owner = self._ready()
        controller.set_storage_writable(False)
        self.assertEqual(controller.state, HostState.BLOCKED)
        with self.assertRaisesRegex(PermissionError, "Durable journal is unavailable"):
            controller.validate_admission(owner.epoch)
        with self.assertRaisesRegex(PermissionError, "durable journal"):
            controller.record_reconciliation(consistent=True)

        controller.set_storage_writable(True)
        self.assertEqual(controller.state, HostState.RECOVERING)
        with self.assertRaisesRegex(PermissionError, "ready"):
            controller.validate_admission(owner.epoch)

        controller.record_reconciliation(consistent=True)
        self.assertEqual(controller.state, HostState.READY)
        controller.validate_admission(owner.epoch)

    def test_storage_writable_flag_requires_real_boolean(self):
        controller, _ = self._ready()
        with self.assertRaises(TypeError):
            controller.set_storage_writable("true")

    def test_reconciliation_consistency_requires_real_boolean(self):
        controller = RecoveryController()
        controller.start("host-a")
        with self.assertRaisesRegex(TypeError, "consistent"):
            controller.record_reconciliation(consistent="false")
        self.assertEqual(controller.state, HostState.RECOVERING)

    def test_clock_trust_requires_real_boolean_and_cannot_truthiness_bypass_block(self):
        controller, owner = self._ready()
        controller.set_clock_trusted(False)
        self.assertEqual(controller.state, HostState.BLOCKED)
        with self.assertRaisesRegex(TypeError, "trusted"):
            controller.set_clock_trusted("false")
        self.assertEqual(controller.state, HostState.BLOCKED)
        with self.assertRaisesRegex(PermissionError, "Clock is not trusted"):
            controller.validate_admission(owner.epoch)

    def test_owner_transfer_authority_flags_require_real_booleans(self):
        controller, _ = self._ready()
        with self.assertRaisesRegex(TypeError, "old_sender_fenced"):
            controller.transfer_owner(
                new_owner_id="host-b",
                old_sender_fenced="true",
                reconciled=True,
            )
        with self.assertRaisesRegex(TypeError, "reconciled"):
            controller.transfer_owner(
                new_owner_id="host-b",
                old_sender_fenced=True,
                reconciled="true",
            )
        self.assertEqual(controller.owner.owner_id, "host-a")
        self.assertEqual(controller.owner.epoch, 1)

    def test_clock_jump_blocks_until_clock_is_requalified_and_reconciled(self):
        controller, owner = self._ready()
        controller.set_clock_trusted(False)
        self.assertEqual(controller.state, HostState.BLOCKED)
        self.assertFalse(controller.provider_reconciled)
        self.assertIn("clock_requalification_required", controller.reason_codes)
        with self.assertRaises(PermissionError):
            controller.validate_sender(owner.owner_id, owner.epoch)

        controller.set_clock_trusted(True)
        self.assertEqual(controller.state, HostState.RECOVERING)
        self.assertIn("clock_requalification_required", controller.reason_codes)
        with self.assertRaises(PermissionError):
            controller.validate_sender(owner.owner_id, owner.epoch)

        controller.record_reconciliation(consistent=True)
        self.assertEqual(controller.state, HostState.READY)
        self.assertNotIn("clock_requalification_required", controller.reason_codes)
        controller.validate_sender(owner.owner_id, owner.epoch)

    def test_lease_expiry_does_not_create_a_new_sender(self):
        controller, owner = self._ready()
        controller.on_lease_expired()
        self.assertEqual(controller.owner, owner)
        self.assertEqual(controller.state, HostState.DEGRADED)
        controller.record_reconciliation(consistent=True)
        self.assertEqual(controller.state, HostState.DEGRADED)
        with self.assertRaises(PermissionError):
            controller.validate_sender(owner.owner_id, owner.epoch)

    def test_transfer_requires_old_sender_fencing_and_reconciliation(self):
        controller, owner = self._ready()
        with self.assertRaisesRegex(PermissionError, "fenced"):
            controller.transfer_owner(
                new_owner_id="host-b",
                old_sender_fenced=False,
                reconciled=True,
            )
        new_owner = controller.transfer_owner(
            new_owner_id="host-b",
            old_sender_fenced=True,
            reconciled=True,
        )
        self.assertEqual(new_owner.epoch, owner.epoch + 1)
        self.assertEqual(controller.state, HostState.RECOVERING)
        with self.assertRaises(PermissionError):
            controller.validate_sender(owner.owner_id, owner.epoch)

    def test_owner_transfer_cannot_self_assert_reconciliation(self):
        controller, _ = self._ready()
        controller.set_storage_writable(False)
        controller.set_storage_writable(True)
        self.assertFalse(controller.provider_reconciled)
        self.assertEqual(controller.state, HostState.RECOVERING)

        with self.assertRaisesRegex(
            PermissionError,
            "recorded current reconciliation",
        ):
            controller.transfer_owner(
                new_owner_id="host-b",
                old_sender_fenced=True,
                reconciled=True,
            )
        self.assertEqual(controller.owner.owner_id, "host-a")
        self.assertEqual(controller.owner.epoch, 1)

        controller.record_reconciliation(consistent=True)
        transferred = controller.transfer_owner(
            new_owner_id="host-b",
            old_sender_fenced=True,
            reconciled=True,
        )
        self.assertEqual(transferred.owner_id, "host-b")

    def test_new_owner_must_reconcile_again_before_sending(self):
        controller, _ = self._ready()
        new_owner = controller.transfer_owner(
            new_owner_id="host-b",
            old_sender_fenced=True,
            reconciled=True,
        )
        with self.assertRaises(PermissionError):
            controller.validate_sender(new_owner.owner_id, new_owner.epoch)
        controller.record_reconciliation(consistent=True)
        controller.validate_sender(new_owner.owner_id, new_owner.epoch)

    def test_durable_owner_restart_cannot_mint_successor_without_takeover(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            first = RecoveryController(
                owner_store=JournalStore(path),
                owner_scope="PAPER:paper-account",
            )
            owner_one = first.start("host-a")
            self._record_durable_ready(first)
            first.validate_sender(owner_one.owner_id, owner_one.epoch)
            self.assertEqual(owner_one.epoch, 1)

            for candidate in ("host-b", "host-c"):
                restarted = RecoveryController(
                    owner_store=JournalStore(path),
                    owner_scope="PAPER:paper-account",
                )
                with self.subTest(candidate=candidate), self.assertRaisesRegex(
                    PermissionError,
                    "explicit takeover evidence",
                ):
                    restarted.start(candidate)
                self.assertIsNone(restarted.owner)
                self.assertEqual(restarted.state, HostState.STOPPED)
                self.assertEqual(restarted.durable_owner_chain(), (owner_one,))

            first.validate_sender(owner_one.owner_id, owner_one.epoch)
            first.validate_admission(owner_one.epoch)

    def test_durable_owner_scopes_are_independent(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            shared = JournalStore(path)
            paper = RecoveryController(
                owner_store=shared,
                owner_scope="PAPER:acct",
            )
            live = RecoveryController(
                owner_store=shared,
                owner_scope="LIVE:acct",
            )
            paper_owner = paper.start("paper-host")
            live_owner = live.start("live-host")
            self.assertEqual(paper_owner.epoch, 1)
            self.assertEqual(live_owner.epoch, 1)

            self._record_durable_ready(paper)
            with self.assertRaisesRegex(
                PermissionError,
                "independently issued takeover evidence",
            ):
                paper.transfer_owner(
                    new_owner_id="paper-host-2",
                    old_sender_fenced=True,
                    reconciled=True,
                )
            self.assertEqual(paper.durable_owner_chain(), (paper_owner,))
            self._record_durable_ready(live)
            live.validate_sender(live_owner.owner_id, live_owner.epoch)

    def test_durable_transfer_fences_an_observer_of_old_generation(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            first = RecoveryController(
                owner_store=JournalStore(path),
                owner_scope="PAPER:acct",
            )
            owner = first.start("host-a")
            self._record_durable_ready(first)

            stale = RecoveryController(
                owner_store=JournalStore(path),
                owner_scope="PAPER:acct",
            )
            stale.owner = owner
            stale.provider_reconciled = True
            stale.reason_codes.clear()
            stale.state = HostState.READY

            with self.assertRaisesRegex(
                PermissionError,
                "independently issued takeover evidence",
            ):
                first.transfer_owner(
                    new_owner_id="host-b",
                    old_sender_fenced=True,
                    reconciled=True,
                )
            self.assertEqual(first.durable_owner_chain(), (owner,))
            stale.validate_sender(owner.owner_id, owner.epoch)

    def test_owner_identity_is_normalized_before_start_and_transfer(self):
        controller = RecoveryController()
        owner = controller.start(" host-a ")
        self.assertEqual(owner.owner_id, "host-a")
        controller.record_reconciliation(consistent=True)
        with self.assertRaisesRegex(ValueError, "differ"):
            controller.transfer_owner(
                new_owner_id=" host-a ",
                old_sender_fenced=True,
                reconciled=True,
            )

    def test_boolean_cannot_impersonate_owner_epoch_one(self):
        controller, owner = self._ready()
        self.assertEqual(owner.epoch, 1)
        with self.assertRaisesRegex(ValueError, "positive integer"):
            controller.validate_sender(owner.owner_id, True)
        with self.assertRaisesRegex(ValueError, "positive integer"):
            controller.validate_admission(True)

    def test_corrupt_owner_journal_blocks_restart_and_sender_validation(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            controller = RecoveryController(
                owner_store=JournalStore(path),
                owner_scope="PAPER:acct",
            )
            owner = controller.start("host-a")
            self._record_durable_ready(controller)

            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    "UPDATE events SET payload_json = ? "
                    "WHERE aggregate_type = 'recovery_owner'",
                    ('{"owner_epoch":"1","owner_id":"attacker"}',),
                )
                connection.commit()
            finally:
                connection.close()

            with self.assertRaisesRegex(ValueError, "payload hash"):
                controller.validate_sender(owner.owner_id, owner.epoch)
            restarted = RecoveryController(
                owner_store=JournalStore(path),
                owner_scope="PAPER:acct",
            )
            with self.assertRaisesRegex(ValueError, "payload hash"):
                restarted.start("host-b")

    def test_provider_uncertainty_prevents_false_ready(self):
        controller = RecoveryController()
        controller.start("host-a")
        controller.record_reconciliation(
            consistent=True,
            uncertainty=["provider-order-state-unknown"],
        )
        self.assertEqual(controller.state, HostState.DEGRADED)
        self.assertIn("provider_uncertainty", controller.reason_codes)


if __name__ == "__main__":
    unittest.main()
