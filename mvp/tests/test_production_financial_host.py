from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Condition, Event, Thread
import unittest
from unittest.mock import Mock

from mvp.autotrade_mvp import production_host
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.production_financial_host import compose_financial_authority
from mvp.autotrade_mvp.production_host import ProductionHostConfig, ProductionHostRuntime
from mvp.autotrade_mvp.recovery import HostState, RecoveryController
from mvp.autotrade_mvp.reconciliation_journal import record_reconciliation_checkpoint
from mvp.autotrade_mvp.recovery_dispatch import build_recovery_issued_dispatcher
from mvp.tests.test_reconciliation_journal import reconciliation


class _FenceStub:
    def __init__(self) -> None:
        self.released = False

    def release(self) -> None:
        self.released = True


class ProductionFinancialHostTests(unittest.TestCase):
    def _host(
        self,
        directory: str,
        *,
        host_id: str = "host-a",
        journal: JournalStore | None = None,
    ) -> ProductionHostRuntime:
        journal = journal or JournalStore(Path(directory) / "journal.sqlite3")
        config = ProductionHostConfig(
            journal_path=journal.path,
            account_id="acct",
            environment="PAPER",
            host_id=host_id,
            bind_host="127.0.0.1",
            bind_port=19001,
            public_origin="http://127.0.0.1:19001",
        )
        return ProductionHostRuntime(
            config=config,
            journal=journal,
            application=object(),
            server=Mock(),
            instance_fence=_FenceStub(),
            admission_gate=Mock(),
            issuance_token=production_host._RUNTIME_ISSUANCE_TOKEN,
        )

    def _mark_ready(
        self,
        runtime,
        *,
        provider_id: str = "BYBIT",
        reconciliation_id: str = "production-host-ready",
    ) -> None:
        recovery = runtime.recovery_controller
        owner = recovery.owner
        self.assertIsNotNone(owner)
        result = reconciliation(
            provider_id=provider_id,
            account_id=runtime.config.account_id,
            environment=runtime.config.environment,
            provider_environment="TESTNET" if provider_id == "BYBIT" else None,
        )
        record_reconciliation_checkpoint(
            runtime.journal,
            reconciliation_id=reconciliation_id,
            result=result,
            observed_at="2026-10-04T01:59:59Z",
            host_id=owner.owner_id,
            owner_epoch=str(owner.epoch),
        )
        recovery.record_reconciliation_checkpoint(
            reconciliation_id=reconciliation_id,
            provider_id=provider_id,
            account_id=runtime.config.account_id,
            environment=runtime.config.environment,
            provider_environment="TESTNET" if provider_id == "BYBIT" else None,
        )
        self.assertIs(recovery.state, HostState.READY)

    def test_exposed_config_mutation_cannot_retarget_financial_scope(self):
        with TemporaryDirectory() as directory:
            host = self._host(directory)
            exposed = host.config
            object.__setattr__(exposed, "environment", "LIVE")
            object.__setattr__(exposed, "account_id", "other-account")
            object.__setattr__(exposed, "host_id", "host-forged")

            runtime = compose_financial_authority(host)

            self.assertEqual(host.config.environment, "PAPER")
            self.assertEqual(host.config.account_id, "acct")
            self.assertEqual(host.config.host_id, "host-a")
            self.assertEqual(runtime.config.environment, "PAPER")
            self.assertEqual(runtime.config.account_id, "acct")
            self.assertEqual(runtime.recovery_controller.owner_scope, "PAPER:acct")
            self.assertEqual(runtime.recovery_controller.owner.owner_id, "host-a")

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
            self.assertIs(runtime.recovery_controller.state, HostState.RECOVERING)
            self.assertEqual(runtime.recovery_controller.durable_owner_chain(), (source_owner,))
            with self.assertRaisesRegex(
                PermissionError,
                "until explicit durable takeover completes",
            ):
                _ = runtime.financial_dispatcher

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
            self._mark_ready(runtime)

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

    def test_listener_failure_observes_recovery_revoked_before_host_cleanup(self):
        with TemporaryDirectory() as directory:
            host = self._host(directory)
            runtime = compose_financial_authority(host)
            recovery = runtime.recovery_controller
            self._mark_ready(runtime)
            retained = build_recovery_issued_dispatcher(
                recovery,
                host.journal,
                environment="PAPER",
                account_id="acct",
            )
            observed = {}
            wire_calls = []

            def transport_send(_client_order_id, _request, final_guard):
                final_guard()
                wire_calls.append("wire")
                return {"status": "accepted"}

            def fail_listener():
                observed["owner"] = recovery.owner
                observed["state"] = recovery.state
                outcome = retained.dispatch(
                    attempt_id="retained-during-listener-close",
                    intent_id="intent-retained-close",
                    intent_hash="sha256:" + "d" * 64,
                    provider="BYBIT",
                    request={"symbol": "BTCUSDT"},
                    now="2026-10-04T04:30:00Z",
                    authority_check=lambda _intent_hash, _now: (True, "allowed"),
                    transport_send=transport_send,
                    final_barrier_clock=lambda: "2026-10-04T04:30:00Z",
                )
                observed["dispatch"] = outcome
                raise RuntimeError("listener close failed")

            host.server.server_close.side_effect = fail_listener

            with self.assertRaisesRegex(RuntimeError, "listener close failed"):
                runtime.close()

            self.assertIsNone(observed["owner"])
            self.assertIs(observed["state"], HostState.STOPPED)
            self.assertEqual(observed["dispatch"].status, "BLOCKED")
            self.assertEqual(wire_calls, [])
            self.assertFalse(host._instance_fence.released)

    def test_released_host_fence_blocks_financial_composition(self):
        with TemporaryDirectory() as directory:
            host = self._host(directory)
            host._instance_fence.released = True
            with self.assertRaisesRegex(
                PermissionError,
                "fence must be active",
            ):
                compose_financial_authority(host)


if __name__ == "__main__":
    unittest.main()
