from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Condition, Event, Thread
import unittest

from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.production_financial_host import compose_financial_authority
from mvp.autotrade_mvp.production_host import ProductionHostConfig, ProductionHostRuntime
from mvp.autotrade_mvp.recovery import HostState, RecoveryController
from mvp.autotrade_mvp.recovery_dispatch import build_recovery_issued_dispatcher


class _FenceStub:
    def __init__(self) -> None:
        self.released = False


class ProductionFinancialHostTests(unittest.TestCase):
    def _host(
        self,
        directory: str,
        *,
        host_id: str = "host-a",
        journal: JournalStore | None = None,
    ) -> ProductionHostRuntime:
        journal = journal or JournalStore(Path(directory) / "journal.sqlite3")
        host = object.__new__(ProductionHostRuntime)
        host.config = ProductionHostConfig(
            journal_path=journal.path,
            account_id="acct",
            environment="PAPER",
            host_id=host_id,
            bind_host="127.0.0.1",
            bind_port=19001,
            public_origin="http://127.0.0.1:19001",
        )
        host.journal = journal
        host.store_identity = journal.store_identity
        host._lifecycle_condition = Condition()
        host._serve_state = "IDLE"
        host._instance_fence = _FenceStub()
        return host

    def test_fresh_host_mints_epoch_one_but_remains_recovering_and_cannot_send(self):
        with TemporaryDirectory() as directory:
            host = self._host(directory)
            runtime = compose_financial_authority(host)

            self.assertFalse(runtime.takeover_required)
            self.assertEqual(runtime.recovery_controller.owner.owner_id, "host-a")
            self.assertEqual(runtime.recovery_controller.owner.epoch, 1)
            self.assertIs(runtime.recovery_controller.state, HostState.RECOVERING)
            self.assertEqual(
                runtime.recovery_controller.durable_owner_chain(),
                (runtime.recovery_controller.owner,),
            )

            wire_calls = []

            def transport_send(_client_order_id, _request, final_guard):
                final_guard()
                wire_calls.append("wire")
                return {"status": "accepted"}

            outcome = runtime.financial_dispatcher.dispatch(
                attempt_id="attempt-fresh",
                intent_id="intent-fresh",
                intent_hash="hash-fresh",
                provider="BYBIT",
                request={"symbol": "BTCUSDT"},
                now="2026-10-04T02:00:00Z",
                authority_check=lambda _intent_hash, _now: (True, "allowed"),
                transport_send=transport_send,
                sender_check=lambda *_args: None,
            )

            self.assertEqual(outcome.status, "BLOCKED")
            self.assertIn("sender_fence_rejected:PermissionError", outcome.reason)
            self.assertEqual(wire_calls, [])

    def test_existing_owner_is_not_reused_or_advanced_without_explicit_takeover(self):
        with TemporaryDirectory() as directory:
            journal = JournalStore(Path(directory) / "journal.sqlite3")
            source = RecoveryController(
                owner_store=journal,
                owner_scope="PAPER:acct",
            )
            source_owner = source.start("host-old")
            host = self._host(directory, host_id="host-new", journal=journal)

            runtime = compose_financial_authority(host)

            self.assertTrue(runtime.takeover_required)
            self.assertEqual(runtime.recovery_controller.owner, source_owner)
            self.assertTrue(runtime.recovery_controller.takeover_source_only)
            self.assertIs(runtime.recovery_controller.state, HostState.RECOVERING)
            self.assertEqual(runtime.recovery_controller.durable_owner_chain(), (source_owner,))
            with self.assertRaisesRegex(
                PermissionError,
                "until explicit durable takeover completes",
            ):
                _ = runtime.financial_dispatcher

            # Even if source-owner reconciliation were to make the controller
            # otherwise READY, the takeover-only marker is an independent sender
            # and admission fence. Reissuing the canonical recovery dispatcher
            # directly cannot bypass explicit durable N -> N+1 takeover.
            recovery = runtime.recovery_controller
            recovery.provider_reconciled = True
            recovery.reason_codes.clear()
            recovery.state = HostState.READY
            with self.assertRaisesRegex(
                PermissionError,
                "Takeover source owner cannot regain sender authority",
            ):
                recovery.validate_sender(source_owner.owner_id, source_owner.epoch)
            with self.assertRaisesRegex(
                PermissionError,
                "Takeover source owner cannot regain admission authority",
            ):
                recovery.validate_admission(source_owner.epoch)
            with self.assertRaisesRegex(
                PermissionError,
                "has not advanced the source owner",
            ):
                recovery.activate_takeover_target_recovery()

            issued = build_recovery_issued_dispatcher(
                recovery,
                journal,
                environment="PAPER",
                account_id="acct",
            )
            wire_calls = []

            def forbidden_wire(_client_order_id, _request, final_guard):
                final_guard()
                wire_calls.append("wire")
                return {"status": "must-not-happen"}

            outcome = issued.dispatch(
                attempt_id="source-owner-bypass",
                intent_id="intent-source",
                intent_hash="hash-source",
                provider="BYBIT",
                request={"symbol": "BTCUSDT"},
                now="2026-10-04T02:00:00Z",
                authority_check=lambda _intent_hash, _now: (True, "allowed"),
                transport_send=forbidden_wire,
            )
            self.assertEqual(outcome.status, "BLOCKED")
            self.assertIn("sender_fence_rejected:PermissionError", outcome.reason)
            self.assertEqual(wire_calls, [])

    def test_host_closing_blocks_before_financial_dispatch_side_effects(self):
        with TemporaryDirectory() as directory:
            host = self._host(directory)
            runtime = compose_financial_authority(host)
            host._serve_state = "CLOSING"
            before = JournalStore.load_events_by_aggregate_type(
                host.journal,
                "submission_attempt",
            )
            wire_calls = []

            with self.assertRaisesRegex(
                PermissionError,
                "host lifetime no longer permits financial sends",
            ):
                runtime.financial_dispatcher.dispatch(
                    attempt_id="attempt-closing",
                    intent_id="intent-closing",
                    intent_hash="hash-closing",
                    provider="BYBIT",
                    request={"symbol": "BTCUSDT"},
                    now="2026-10-04T02:00:01Z",
                    authority_check=lambda _intent_hash, _now: (True, "allowed"),
                    transport_send=lambda *_args: wire_calls.append("wire"),
                )

            self.assertEqual(wire_calls, [])
            self.assertEqual(
                JournalStore.load_events_by_aggregate_type(
                    host.journal,
                    "submission_attempt",
                ),
                before,
            )

    def test_host_lifecycle_cut_cannot_cross_inflight_financial_send(self):
        with TemporaryDirectory() as directory:
            host = self._host(directory)
            runtime = compose_financial_authority(host)
            recovery = runtime.recovery_controller
            recovery.state = HostState.READY
            recovery.provider_reconciled = True
            recovery.reason_codes.clear()

            transport_entered = Event()
            release_transport = Event()
            dispatch_done = Event()
            closer_attempting = Event()
            closer_acquired = Event()
            outcome_box = []
            error_box = []

            def transport_send(_client_order_id, _request, final_guard):
                final_guard()
                transport_entered.set()
                if not release_transport.wait(timeout=5):
                    raise AssertionError("test transport release barrier was not opened")
                return {"status": "accepted"}

            def dispatch_worker():
                try:
                    outcome_box.append(
                        runtime.financial_dispatcher.dispatch(
                            attempt_id="attempt-race",
                            intent_id="intent-race",
                            intent_hash="hash-race",
                            provider="BYBIT",
                            request={"symbol": "BTCUSDT"},
                            now="2026-10-04T02:00:02Z",
                            authority_check=lambda _intent_hash, _now: (True, "allowed"),
                            transport_send=transport_send,
                        )
                    )
                except BaseException as error:
                    error_box.append(error)
                finally:
                    dispatch_done.set()

            def closer_worker():
                closer_attempting.set()
                with host._lifecycle_condition:
                    host._serve_state = "CLOSING"
                    closer_acquired.set()

            dispatch_thread = Thread(target=dispatch_worker, daemon=False)
            dispatch_thread.start()
            self.assertTrue(transport_entered.wait(timeout=5))

            closer_thread = Thread(target=closer_worker, daemon=False)
            closer_thread.start()
            self.assertTrue(closer_attempting.wait(timeout=5))
            self.assertFalse(
                closer_acquired.is_set(),
                "teardown crossed the lifecycle cut while provider send was in flight",
            )

            release_transport.set()
            self.assertTrue(dispatch_done.wait(timeout=5))
            dispatch_thread.join(timeout=5)
            closer_thread.join(timeout=5)

            self.assertEqual(error_box, [])
            self.assertEqual(len(outcome_box), 1)
            self.assertEqual(outcome_box[0].status, "SENT")
            self.assertTrue(closer_acquired.is_set())
            self.assertEqual(host._serve_state, "CLOSING")

    def test_released_host_fence_blocks_financial_composition(self):
        with TemporaryDirectory() as directory:
            host = self._host(directory)
            host._instance_fence.released = True
            with self.assertRaisesRegex(
                PermissionError,
                "fence must be active",
            ):
                compose_financial_authority(host)

    def test_host_identity_mutation_invalidates_retained_dispatcher_before_callbacks(self):
        with TemporaryDirectory() as directory:
            host = self._host(directory)
            runtime = compose_financial_authority(host)
            dispatcher = runtime.financial_dispatcher
            authority_calls = []
            wire_calls = []

            object.__setattr__(host.config, "host_id", "host-retargeted")

            with self.assertRaisesRegex(
                PermissionError,
                "identity changed after composition",
            ):
                dispatcher.dispatch(
                    attempt_id="attempt-retarget-config",
                    intent_id="intent-retarget-config",
                    intent_hash="hash-retarget-config",
                    provider="BYBIT",
                    request={"symbol": "BTCUSDT"},
                    now="2026-10-04T02:00:03Z",
                    authority_check=lambda *_args: authority_calls.append("authority"),
                    transport_send=lambda *_args: wire_calls.append("wire"),
                )

            self.assertEqual(authority_calls, [])
            self.assertEqual(wire_calls, [])

    def test_host_journal_retarget_invalidates_retained_dispatcher_before_callbacks(self):
        with TemporaryDirectory() as directory:
            host = self._host(directory)
            runtime = compose_financial_authority(host)
            dispatcher = runtime.financial_dispatcher
            authority_calls = []
            wire_calls = []

            host.journal = JournalStore(Path(directory) / "retargeted.sqlite3")

            with self.assertRaisesRegex(
                PermissionError,
                "journal changed after composition",
            ):
                dispatcher.dispatch(
                    attempt_id="attempt-retarget-journal",
                    intent_id="intent-retarget-journal",
                    intent_hash="hash-retarget-journal",
                    provider="BYBIT",
                    request={"symbol": "BTCUSDT"},
                    now="2026-10-04T02:00:04Z",
                    authority_check=lambda *_args: authority_calls.append("authority"),
                    transport_send=lambda *_args: wire_calls.append("wire"),
                )

            self.assertEqual(authority_calls, [])
            self.assertEqual(wire_calls, [])

    def test_financial_runtime_recovery_controller_reference_is_read_only(self):
        with TemporaryDirectory() as directory:
            host = self._host(directory)
            runtime = compose_financial_authority(host)
            replacement = RecoveryController(
                owner_store=host.journal,
                owner_scope="PAPER:acct",
            )

            with self.assertRaises(AttributeError):
                runtime.recovery_controller = replacement
            self.assertIsNot(runtime.recovery_controller, replacement)

    def test_equivalent_config_object_replacement_fails_closed(self):
        with TemporaryDirectory() as directory:
            host = self._host(directory)
            runtime = compose_financial_authority(host)
            original = host.config
            host.config = ProductionHostConfig(
                journal_path=original.journal_path,
                account_id=original.account_id,
                environment=original.environment,
                host_id=original.host_id,
                bind_host=original.bind_host,
                bind_port=original.bind_port,
                public_origin=original.public_origin,
            )

            with self.assertRaisesRegex(
                PermissionError,
                "config authority changed",
            ):
                _ = runtime.takeover_required


if __name__ == "__main__":
    unittest.main()
