from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Condition, Event, Thread
import unittest

from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.production_financial_host import compose_financial_authority
from mvp.autotrade_mvp.production_host import ProductionHostConfig, ProductionHostRuntime
from mvp.autotrade_mvp.recovery import HostState, RecoveryController


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
            server=object(),
            instance_fence=_FenceStub(),
            admission_gate=object(),
        )

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


if __name__ == "__main__":
    unittest.main()
