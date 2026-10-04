from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from mvp.autotrade_mvp import production_trading_host as trading_host
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.production_host import ProductionHostConfig
from mvp.autotrade_mvp.recovery import HostState, OwnerFence, RecoveryController


class _Fence:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.released = False
        self.on_release = None
        self.release_calls = 0

    def release(self) -> None:
        self.release_calls += 1
        if self.released:
            return
        if self.on_release is not None:
            self.on_release()
        self.released = True


class _Host:
    def __init__(self, config: ProductionHostConfig, store: JournalStore) -> None:
        self.config = config
        self.journal = store
        self.store_identity = store.store_identity
        self.application = Mock()
        self.server = Mock()
        self._instance_fence = _Fence(Path(str(store.path) + ".host.lock"))
        self.closed = False
        self.shutdown_requested = False
        self.serving = False
        self._terminal_finalizer = None

    def bind_terminal_finalizer(self, finalizer) -> None:
        if self._terminal_finalizer is not None:
            raise RuntimeError("terminal finalizer is already bound")
        self._terminal_finalizer = finalizer

    def close(self) -> None:
        if self.closed:
            return
        if self._terminal_finalizer is not None:
            self._terminal_finalizer()
        self._instance_fence.release()
        self.closed = True
        self.shutdown_requested = True

    def serve_forever(self, *, poll_interval: float = 0.5) -> None:
        del poll_interval
        self.serving = True
        try:
            self.close()
        finally:
            self.serving = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        del exc_type, exc, traceback
        self.close()


class ProductionTradingHostTests(unittest.TestCase):
    @staticmethod
    def _config(root: Path, *, environment: str = "PAPER") -> ProductionHostConfig:
        return ProductionHostConfig(
            journal_path=root / "journal.sqlite3",
            account_id="acct",
            environment=environment,
            host_id="host-process",
            bind_host="127.0.0.1",
            bind_port=8765,
            public_origin="http://127.0.0.1:8765",
        )

    def _build_with_fake_host(
        self,
        root: Path,
        *,
        environment: str = "PAPER",
        recovery_owner_id: str = "owner-a",
        takeover=None,
    ):
        config = self._config(root, environment=environment)
        store = JournalStore(config.journal_path)
        host = _Host(config, store)
        patcher = patch.object(
            trading_host,
            "build_production_host",
            return_value=host,
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        runtime = trading_host.build_production_trading_host(
            config,
            security_boundary=Mock(),
            principal_resolver=Mock(),
            snapshot_provider=Mock(),
            recovery_owner_id=recovery_owner_id,
            takeover=takeover,
        )
        return runtime, host, store

    def test_paper_binds_exact_host_store_and_blocks_wire_before_reconciliation(self):
        with TemporaryDirectory() as directory:
            runtime, host, store = self._build_with_fake_host(Path(directory))
            self.addCleanup(runtime.close)

            self.assertIs(runtime.journal, store)
            self.assertIs(vars(runtime.recovery)["_owner_store"], store)
            self.assertIs(vars(runtime.dispatcher)["_store"], store)
            self.assertEqual(runtime.recovery.owner_scope, "PAPER:acct")
            self.assertEqual(runtime.dispatcher.owner_id, "owner-a")
            self.assertEqual(runtime.dispatcher.owner_epoch, 1)
            self.assertEqual(runtime.recovery.state, HostState.RECOVERING)

            wire_calls = []

            def transport(client_order_id, request, final_guard):
                del request
                final_guard()
                wire_calls.append(client_order_id)
                return {"status": "accepted"}

            outcome = runtime.dispatcher.dispatch(
                attempt_id="startup-block",
                intent_id="intent-1",
                intent_hash="hash-1",
                provider="SIMULATED",
                request={},
                now="2026-10-04T03:00:00Z",
                authority_check=lambda _hash, _now: (True, "allowed"),
                transport_send=transport,
            )
            self.assertEqual(outcome.status, "BLOCKED")
            self.assertEqual(wire_calls, [])
            self.assertFalse(host._instance_fence.released)

    def test_close_revokes_retained_dispatcher_before_process_fence_release(self):
        with TemporaryDirectory() as directory:
            runtime, host, _store = self._build_with_fake_host(Path(directory))
            retained_dispatcher = runtime.dispatcher
            original_fence = host._instance_fence._fence
            controller = runtime.recovery
            original_fence.on_release = lambda: self.assertIsNone(controller.owner)

            runtime.close()

            self.assertTrue(runtime.closed)
            self.assertTrue(original_fence.released)
            self.assertEqual(original_fence.release_calls, 1)
            self.assertIsNone(controller.owner)
            self.assertEqual(controller.state, HostState.STOPPED)

            wire_calls = []

            def transport(client_order_id, request, final_guard):
                del request
                final_guard()
                wire_calls.append(client_order_id)
                return {"status": "accepted"}

            outcome = retained_dispatcher.dispatch(
                attempt_id="after-close",
                intent_id="intent-2",
                intent_hash="hash-2",
                provider="SIMULATED",
                request={},
                now="2026-10-04T03:01:00Z",
                authority_check=lambda _hash, _now: (True, "allowed"),
                transport_send=transport,
            )
            self.assertEqual(outcome.status, "BLOCKED")
            self.assertEqual(wire_calls, [])


    def test_close_revokes_dispatcher_before_later_host_cleanup_failure(self):
        with TemporaryDirectory() as directory:
            runtime, host, _store = self._build_with_fake_host(Path(directory))
            retained_dispatcher = runtime.dispatcher
            controller = runtime.recovery
            original_finalizer = host._terminal_finalizer
            self.assertIsNotNone(original_finalizer)

            def fail_after_finalizer():
                original_finalizer()
                raise RuntimeError("listener close failed")

            host.close = fail_after_finalizer

            with self.assertRaisesRegex(RuntimeError, "listener close failed"):
                runtime.close()

            self.assertIsNone(controller.owner)
            self.assertEqual(controller.state, HostState.STOPPED)
            outcome = retained_dispatcher.dispatch(
                attempt_id="after-failed-close",
                intent_id="intent-failed-close",
                intent_hash="sha256:" + "5" * 64,
                provider="SIMULATED",
                request={},
                now="2026-10-04T03:02:00Z",
                authority_check=lambda _hash, _at: (True, "allowed"),
                transport_send=lambda *_args, **_kwargs: self.fail(
                    "revoked dispatcher reached transport"
                ),
            )
            self.assertEqual(outcome.status, "BLOCKED")


    def test_existing_durable_owner_without_takeover_fails_and_releases_host_fence(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._config(root)
            store = JournalStore(config.journal_path)
            prior = RecoveryController(
                owner_store=store,
                owner_scope="PAPER:acct",
            )
            prior.start("owner-old")
            prior.stop()
            host = _Host(config, store)
            original_fence = host._instance_fence

            with patch.object(
                trading_host,
                "build_production_host",
                return_value=host,
            ):
                with self.assertRaisesRegex(
                    PermissionError,
                    "requires explicit takeover inputs",
                ):
                    trading_host.build_production_trading_host(
                        config,
                        security_boundary=Mock(),
                        principal_resolver=Mock(),
                        snapshot_provider=Mock(),
                        recovery_owner_id="owner-new",
                    )

            self.assertTrue(host.closed)
            self.assertTrue(original_fence.released)
            self.assertEqual(original_fence.release_calls, 1)
            verifier = RecoveryController(
                owner_store=store,
                owner_scope="PAPER:acct",
            )
            self.assertEqual(
                [(owner.owner_id, owner.epoch) for owner in verifier.durable_owner_chain()],
                [("owner-old", 1)],
            )

    def test_clean_restart_stages_durable_source_only_for_immediate_takeover(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._config(root)
            store = JournalStore(config.journal_path)
            prior = RecoveryController(
                owner_store=store,
                owner_scope="PAPER:acct",
            )
            prior.start("owner-old")
            prior.stop()
            host = _Host(config, store)
            takeover = trading_host.DurableTakeoverInputs(
                vault=Mock(),
                handle=Mock(),
                execution_identity="windows-user",
                reconciliation_id="reconcile-1",
                provider_id="SIMULATED",
            )
            observed = {}

            def perform_takeover(controller, *, new_owner_id, **kwargs):
                observed["controller"] = controller
                observed["store"] = vars(controller)["_owner_store"]
                observed["source"] = controller.owner
                observed["state"] = controller.state
                observed["new_owner_id"] = new_owner_id
                observed["kwargs"] = kwargs
                controller.owner = OwnerFence(new_owner_id, 2)
                controller.provider_reconciled = False
                controller.reason_codes = {"startup_reconciliation_required"}
                controller.state = HostState.RECOVERING
                return Mock()

            with (
                patch.object(
                    trading_host,
                    "build_production_host",
                    return_value=host,
                ),
                patch.object(
                    trading_host,
                    "execute_durable_takeover",
                    side_effect=perform_takeover,
                ) as takeover_call,
            ):
                runtime = trading_host.build_production_trading_host(
                    config,
                    security_boundary=Mock(),
                    principal_resolver=Mock(),
                    snapshot_provider=Mock(),
                    recovery_owner_id="owner-new",
                    takeover=takeover,
                )
            try:
                self.assertEqual(observed["source"], OwnerFence("owner-old", 1))
                self.assertEqual(observed["state"], HostState.RECOVERING)
                self.assertIs(observed["store"], store)
                self.assertEqual(observed["new_owner_id"], "owner-new")
                self.assertEqual(runtime.dispatcher.owner_id, "owner-new")
                self.assertEqual(runtime.dispatcher.owner_epoch, 2)
                self.assertIs(runtime.recovery, observed["controller"])
                takeover_call.assert_called_once()
            finally:
                runtime.close()

    def test_simulation_exposes_no_external_recovery_dispatcher(self):
        with TemporaryDirectory() as directory:
            runtime, host, store = self._build_with_fake_host(
                Path(directory),
                environment="SIMULATION",
            )
            try:
                self.assertIs(runtime.journal, store)
                self.assertIsNone(runtime.recovery)
                self.assertIsNone(runtime.dispatcher)
                self.assertIs(runtime.host, host)
            finally:
                runtime.close()


if __name__ == "__main__":
    unittest.main()
