from pathlib import Path
from tempfile import TemporaryDirectory, TemporaryFile
from threading import Event, Thread
import unittest
from unittest.mock import Mock, patch

from mvp.autotrade_mvp import host_network, production_host
from mvp.autotrade_mvp.host_network import TransportResponse
from mvp.autotrade_mvp.production_host import (
    ProductionHostConfig,
    ProductionHostRuntime,
    _CommandAdmissionGate,
    _InstanceFence,
    build_production_host,
)


class ProductionHostConfigTests(unittest.TestCase):
    def test_identity_and_listener_contract(self):
        with TemporaryDirectory() as directory:
            path = Path(directory).resolve() / "state" / ".." / "journal.sqlite3"
            config = ProductionHostConfig(
                journal_path=path,
                account_id="paper-account",
                environment="PAPER",
                host_id="host-a",
                bind_host="0.0.0.0",
                bind_port=443,
                public_origin="https://trade.example.com",
            )
            self.assertEqual(config.journal_path, path.resolve(strict=False))
            self.assertEqual(config.bind_host, "0.0.0.0")
            self.assertEqual(config.public_origin, "https://trade.example.com")

        with self.assertRaisesRegex(ValueError, "journal_path must be absolute"):
            ProductionHostConfig(
                journal_path="state/journal.sqlite3",
                account_id="paper-account",
                environment="PAPER",
                host_id="host-a",
                bind_host="127.0.0.1",
                bind_port=8765,
                public_origin="http://127.0.0.1:8765",
            )

    def test_public_origin_port_must_match_listener(self):
        with TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "port must match bind_port"):
                ProductionHostConfig(
                    journal_path=Path(directory).resolve() / "journal.sqlite3",
                    account_id="paper-account",
                    environment="PAPER",
                    host_id="host-a",
                    bind_host="127.0.0.1",
                    bind_port=8765,
                    public_origin="http://127.0.0.1:8766",
                )


class CommandAdmissionGateTests(unittest.TestCase):
    def test_shutdown_rejects_new_commands_and_drains_admitted_dispatch(self):
        entered = Event()
        release = Event()

        def dispatch(**kwargs):
            del kwargs
            entered.set()
            release.wait()
            return TransportResponse(200, "application/json", b"{}")

        application = Mock()
        application.dispatch = dispatch
        gate = _CommandAdmissionGate(application)
        results = []
        request = Thread(
            target=lambda: results.append(
                gate.dispatch(
                    method="POST",
                    target="/api/v1/commands",
                    headers={},
                    body=b"{}",
                )
            )
        )
        request.start()
        self.assertTrue(entered.wait(timeout=1))

        drained = Event()
        closer = Thread(target=lambda: (gate.stop_and_drain(), drained.set()))
        closer.start()
        self.assertFalse(drained.wait(timeout=0.05))
        release.set()
        self.assertTrue(drained.wait(timeout=1))
        request.join(timeout=1)
        closer.join(timeout=1)
        self.assertEqual(results[0].status, 200)

        rejected = gate.dispatch(
            method="POST",
            target="/api/v1/commands",
            headers={},
            body=b"{}",
        )
        self.assertEqual(rejected.status, 503)
        self.assertEqual(rejected.body, b'{"error":"HOST_SHUTTING_DOWN"}')


class InstanceFenceTests(unittest.TestCase):
    def test_release_failure_is_terminal_and_never_claimed_released(self):
        with TemporaryFile() as handle:
            fence = _InstanceFence(path=Path("host.lock"), handle=handle)
            failure = OSError("unlock failed")
            if production_host.os.name == "nt":
                import msvcrt

                target = patch.object(msvcrt, "locking", side_effect=failure)
            else:
                import fcntl

                target = patch.object(fcntl, "flock", side_effect=failure)
            with target:
                with self.assertRaises(OSError) as first:
                    fence.release()
            self.assertIs(first.exception, failure)
            self.assertFalse(fence.released)
            self.assertTrue(handle.closed)
            with self.assertRaises(OSError) as second:
                fence.release()
            self.assertIs(second.exception, failure)


class ProductionHostRuntimeTests(unittest.TestCase):
    @staticmethod
    def _runtime(*, server=None, gate=None, fence=None):
        return ProductionHostRuntime(
            config=Mock(),
            journal=Mock(),
            application=Mock(),
            server=server or Mock(),
            instance_fence=fence or Mock(),
            admission_gate=gate or Mock(),
        )

    def test_pre_serve_close_is_terminal_idempotent_and_ordered(self):
        ordering = []
        server = Mock()
        gate = Mock()
        fence = Mock()
        gate.stop_and_drain.side_effect = lambda: ordering.append("drained")
        server.server_close.side_effect = lambda: ordering.append("listener-closed")
        fence.release.side_effect = lambda: ordering.append("fence-released")
        runtime = self._runtime(server=server, gate=gate, fence=fence)
        runtime.close()
        runtime.close()
        self.assertTrue(runtime.closed)
        self.assertEqual(ordering, ["drained", "listener-closed", "fence-released"])
        server.shutdown.assert_not_called()
        with self.assertRaisesRegex(RuntimeError, "closed"):
            runtime.serve_forever()

    def _assert_close_waits_for_pre_io_hook(self, hook_name):
        server = Mock()
        gate = Mock()
        fence = Mock()
        runtime = self._runtime(server=server, gate=gate, fence=fence)
        entered = Event()
        release = Event()

        def pause():
            entered.set()
            release.wait()

        setattr(runtime, hook_name, pause)
        serve_errors = []

        def serve():
            try:
                runtime.serve_forever()
            except BaseException as exc:
                serve_errors.append(exc)

        caller = Thread(target=serve)
        caller.start()
        self.assertTrue(entered.wait(timeout=1))
        close_done = Event()
        closer = Thread(target=lambda: (runtime.close(), close_done.set()))
        closer.start()
        self.assertFalse(close_done.wait(timeout=0.05))
        release.set()
        closer.join(timeout=1)
        caller.join(timeout=1)
        self.assertFalse(closer.is_alive())
        self.assertFalse(caller.is_alive())
        self.assertEqual(serve_errors, [])
        server.handle_request.assert_not_called()
        server.server_close.assert_called_once_with()
        fence.release.assert_called_once_with()
        self.assertTrue(runtime.closed)

    def test_close_during_starting_joins_worker_before_terminal_close(self):
        self._assert_close_waits_for_pre_io_hook("_serve_entry_hook")

    def test_close_during_entering_joins_worker_before_terminal_close(self):
        self._assert_close_waits_for_pre_io_hook("_serve_loop_entry_hook")

    def test_concurrent_close_callers_share_one_terminal_success(self):
        drain_entered = Event()
        drain_release = Event()
        gate = Mock()
        gate.stop_and_drain.side_effect = lambda: (drain_entered.set(), drain_release.wait())
        server = Mock()
        fence = Mock()
        runtime = self._runtime(server=server, gate=gate, fence=fence)
        second_done = Event()
        first = Thread(target=runtime.close)
        second = Thread(target=lambda: (runtime.close(), second_done.set()))
        first.start()
        self.assertTrue(drain_entered.wait(timeout=1))
        second.start()
        self.assertFalse(second_done.wait(timeout=0.05))
        drain_release.set()
        first.join(timeout=1)
        second.join(timeout=1)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertTrue(runtime.closed)
        gate.stop_and_drain.assert_called_once_with()
        server.server_close.assert_called_once_with()
        fence.release.assert_called_once_with()

    def test_concurrent_close_callers_share_same_terminal_failure(self):
        entered = Event()
        release = Event()
        failure = OSError("listener close failed")
        server = Mock()

        def fail_close():
            entered.set()
            release.wait()
            raise failure

        server.server_close.side_effect = fail_close
        fence = Mock()
        runtime = self._runtime(server=server, fence=fence)
        errors = []

        def close_and_record():
            try:
                runtime.close()
            except BaseException as exc:
                errors.append(exc)

        first = Thread(target=close_and_record)
        second = Thread(target=close_and_record)
        first.start()
        self.assertTrue(entered.wait(timeout=1))
        second.start()
        release.set()
        first.join(timeout=1)
        second.join(timeout=1)
        self.assertEqual(len(errors), 2)
        self.assertIs(errors[0], failure)
        self.assertIs(errors[1], failure)
        fence.release.assert_not_called()
        self.assertFalse(runtime.closed)
        self.assertTrue(runtime.shutdown_requested)
        with self.assertRaises(OSError) as later:
            runtime.close()
        self.assertIs(later.exception, failure)

    def test_active_serving_close_joins_request_loop_before_listener_and_fence(self):
        entered = Event()
        ordering = []
        server = Mock()
        gate = Mock()
        fence = Mock()

        def handle_request():
            if not entered.is_set():
                ordering.append("request-loop-entered")
                entered.set()

        server.handle_request.side_effect = handle_request
        runtime = self._runtime(server=server, gate=gate, fence=fence)

        def listener_close():
            worker = runtime._serve_thread
            self.assertIsNotNone(worker)
            self.assertFalse(worker.is_alive())
            ordering.append("listener-closed")

        server.server_close.side_effect = listener_close
        fence.release.side_effect = lambda: ordering.append("fence-released")
        caller = Thread(target=runtime.serve_forever)
        caller.start()
        self.assertTrue(entered.wait(timeout=1))
        self.assertTrue(runtime.serving)
        runtime.close()
        caller.join(timeout=1)
        self.assertFalse(caller.is_alive())
        server.shutdown.assert_not_called()
        self.assertEqual(ordering[-2:], ["listener-closed", "fence-released"])
        self.assertTrue(runtime.closed)

    def test_abnormal_serve_exit_terminally_tears_down_and_is_not_restartable(self):
        entered = Event()
        failure = OSError("request loop failed")
        ordering = []
        server = Mock()

        def fail_request():
            entered.set()
            raise failure

        server.handle_request.side_effect = fail_request
        server.server_close.side_effect = lambda: ordering.append("listener-closed")
        gate = Mock()
        fence = Mock()
        fence.release.side_effect = lambda: ordering.append("fence-released")
        runtime = self._runtime(server=server, gate=gate, fence=fence)
        errors = []

        def serve():
            try:
                runtime.serve_forever()
            except BaseException as exc:
                errors.append(exc)

        caller = Thread(target=serve)
        caller.start()
        self.assertTrue(entered.wait(timeout=1))
        caller.join(timeout=1)
        self.assertFalse(caller.is_alive())
        self.assertEqual(errors, [failure])
        gate.stop_and_drain.assert_called_once_with()
        self.assertEqual(ordering, ["listener-closed", "fence-released"])
        self.assertFalse(runtime.closed)
        self.assertTrue(runtime.shutdown_requested)
        with self.assertRaises(OSError) as retry:
            runtime.serve_forever()
        self.assertIs(retry.exception, failure)
        with self.assertRaises(OSError) as later_close:
            runtime.close()
        self.assertIs(later_close.exception, failure)

    def test_serve_poll_interval_is_finite_positive(self):
        runtime = self._runtime()
        for value in (0, -1, float("inf"), float("nan")):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "finite positive"):
                    runtime.serve_forever(poll_interval=value)
        with self.assertRaisesRegex(TypeError, "finite positive"):
            runtime.serve_forever(poll_interval=True)


class ProductionHostCompositionTests(unittest.TestCase):
    @staticmethod
    def _config(directory: str) -> ProductionHostConfig:
        return ProductionHostConfig(
            journal_path=Path(directory).resolve() / "journal.sqlite3",
            account_id="paper-account",
            environment="PAPER",
            host_id="host-a",
            bind_host="127.0.0.1",
            bind_port=8765,
            public_origin="http://127.0.0.1:8765",
        )

    def test_build_reuses_canonical_components(self):
        class DummySecurityBoundary:
            pass

        with TemporaryDirectory() as directory:
            config = self._config(directory)
            security = DummySecurityBoundary()
            journal = object()
            application = Mock()
            server = Mock()
            with (
                patch.object(production_host, "SecurityBoundary", DummySecurityBoundary),
                patch.object(production_host, "JournalStore", return_value=journal) as journal_factory,
                patch.object(production_host, "AuthenticatedHostApplication", return_value=application) as app_factory,
                patch.object(production_host, "AuthenticatedHostServer", return_value=server) as server_factory,
            ):
                runtime = build_production_host(
                    config,
                    security_boundary=security,
                    principal_resolver=Mock(),
                    snapshot_provider=Mock(),
                )
            try:
                journal_factory.assert_called_once_with(config.journal_path)
                app_factory.assert_called_once()
                server_factory.assert_called_once()
                self.assertIs(runtime.journal, journal)
                self.assertIs(runtime.application, application)
                self.assertIs(runtime.server, server)
                self.assertFalse(server.daemon_threads)
                self.assertTrue(server.block_on_close)
            finally:
                runtime.close()

    def test_same_instance_is_fenced_and_construction_failure_releases_fence(self):
        class DummySecurityBoundary:
            pass

        with TemporaryDirectory() as directory:
            config = self._config(directory)
            with (
                patch.object(production_host, "SecurityBoundary", DummySecurityBoundary),
                patch.object(host_network, "SecurityBoundary", DummySecurityBoundary),
                patch.object(production_host, "AuthenticatedHostServer", side_effect=lambda *a, **k: Mock()),
            ):
                first = build_production_host(
                    config,
                    security_boundary=DummySecurityBoundary(),
                    principal_resolver=lambda headers, origin: None,
                    snapshot_provider=lambda state, principal: {},
                )
                try:
                    with self.assertRaisesRegex(RuntimeError, "already owned"):
                        build_production_host(
                            config,
                            security_boundary=DummySecurityBoundary(),
                            principal_resolver=lambda headers, origin: None,
                            snapshot_provider=lambda state, principal: {},
                        )
                finally:
                    first.close()

    def test_listener_construction_failure_releases_instance_fence(self):
        class DummySecurityBoundary:
            pass

        with TemporaryDirectory() as directory:
            config = self._config(directory)
            with (
                patch.object(production_host, "SecurityBoundary", DummySecurityBoundary),
                patch.object(host_network, "SecurityBoundary", DummySecurityBoundary),
                patch.object(production_host, "AuthenticatedHostServer", side_effect=OSError("listener failed")),
            ):
                with self.assertRaisesRegex(OSError, "listener failed"):
                    build_production_host(
                        config,
                        security_boundary=DummySecurityBoundary(),
                        principal_resolver=lambda headers, origin: None,
                        snapshot_provider=lambda state, principal: {},
                    )
            with (
                patch.object(production_host, "SecurityBoundary", DummySecurityBoundary),
                patch.object(host_network, "SecurityBoundary", DummySecurityBoundary),
                patch.object(production_host, "AuthenticatedHostServer", return_value=Mock()),
            ):
                successor = build_production_host(
                    config,
                    security_boundary=DummySecurityBoundary(),
                    principal_resolver=lambda headers, origin: None,
                    snapshot_provider=lambda state, principal: {},
                )
                successor.close()

    def test_https_requires_tls_context(self):
        class DummySecurityBoundary:
            pass

        with TemporaryDirectory() as directory:
            config = ProductionHostConfig(
                journal_path=Path(directory).resolve() / "journal.sqlite3",
                account_id="paper-account",
                environment="PAPER",
                host_id="host-a",
                bind_host="0.0.0.0",
                bind_port=443,
                public_origin="https://trade.example.com",
            )
            with patch.object(production_host, "SecurityBoundary", DummySecurityBoundary):
                with self.assertRaisesRegex(ValueError, "requires tls_context"):
                    build_production_host(
                        config,
                        security_boundary=DummySecurityBoundary(),
                        principal_resolver=lambda headers, origin: None,
                        snapshot_provider=lambda state, principal: {},
                    )


if __name__ == "__main__":
    unittest.main()
