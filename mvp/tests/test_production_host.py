from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
import unittest
from unittest.mock import Mock, patch

from mvp.autotrade_mvp import production_host
from mvp.autotrade_mvp.host_network import TransportResponse
from mvp.autotrade_mvp.production_host import (
    ProductionHostConfig,
    ProductionHostRuntime,
    _CommandAdmissionGate,
    build_production_host,
)


class ProductionHostConfigTests(unittest.TestCase):
    def test_requires_absolute_journal_identity(self):
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

    def test_listener_port_must_match_public_origin(self):
        with TemporaryDirectory() as directory:
            path = Path(directory).resolve() / "journal.sqlite3"
            with self.assertRaisesRegex(ValueError, "port must match bind_port"):
                ProductionHostConfig(
                    journal_path=path,
                    account_id="paper-account",
                    environment="PAPER",
                    host_id="host-a",
                    bind_host="127.0.0.1",
                    bind_port=8765,
                    public_origin="http://127.0.0.1:8766",
                )

    def test_tls_listener_bind_and_authenticated_public_host_are_distinct(self):
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
            self.assertEqual(config.bind_host, "0.0.0.0")
            self.assertEqual(config.public_origin, "https://trade.example.com")

    def test_canonical_runtime_identity_is_retained(self):
        with TemporaryDirectory() as directory:
            path = Path(directory).resolve() / "state" / ".." / "journal.sqlite3"
            config = ProductionHostConfig(
                journal_path=path,
                account_id="paper-account",
                environment="PAPER",
                host_id="host-a",
                bind_host="127.0.0.1",
                bind_port=8765,
                public_origin="http://127.0.0.1:8765",
            )
            self.assertEqual(config.journal_path, path.resolve(strict=False))
            self.assertEqual(config.account_id, "paper-account")
            self.assertEqual(config.environment, "PAPER")
            self.assertEqual(config.public_origin, "http://127.0.0.1:8765")


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
        result = []
        request = Thread(
            target=lambda: result.append(
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
        self.assertFalse(request.is_alive())
        self.assertFalse(closer.is_alive())
        self.assertEqual(result[0].status, 200)

        rejected = gate.dispatch(
            method="POST",
            target="/api/v1/commands",
            headers={},
            body=b"{}",
        )
        self.assertEqual(rejected.status, 503)
        self.assertEqual(rejected.body, b'{"error":"HOST_SHUTTING_DOWN"}')


class ProductionHostCompositionTests(unittest.TestCase):
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

    def test_build_reuses_canonical_journal_application_and_server(self):
        class DummySecurityBoundary:
            pass

        with TemporaryDirectory() as directory:
            config = ProductionHostConfig(
                journal_path=Path(directory).resolve() / "journal.sqlite3",
                account_id="paper-account",
                environment="PAPER",
                host_id="host-a",
                bind_host="127.0.0.1",
                bind_port=8765,
                public_origin="http://127.0.0.1:8765",
            )
            security = DummySecurityBoundary()
            principal_resolver = Mock(name="principal_resolver")
            snapshot_provider = Mock(name="snapshot_provider")
            journal = object()
            application = Mock()
            server = Mock()

            journal_factory = Mock(return_value=journal)
            application_factory = Mock(return_value=application)
            server_factory = Mock(return_value=server)
            with (
                patch.object(production_host, "SecurityBoundary", DummySecurityBoundary),
                patch.object(production_host, "JournalStore", journal_factory),
                patch.object(
                    production_host,
                    "AuthenticatedHostApplication",
                    application_factory,
                ),
                patch.object(
                    production_host,
                    "AuthenticatedHostServer",
                    server_factory,
                ),
            ):
                runtime = build_production_host(
                    config,
                    security_boundary=security,
                    principal_resolver=principal_resolver,
                    snapshot_provider=snapshot_provider,
                )

            try:
                journal_factory.assert_called_once_with(config.journal_path)
                application_factory.assert_called_once_with(
                    journal,
                    security_boundary=security,
                    account_id="paper-account",
                    environment="PAPER",
                    host_id="host-a",
                    public_origin="http://127.0.0.1:8765",
                    principal_resolver=principal_resolver,
                    snapshot_provider=snapshot_provider,
                    now=None,
                )
                server_factory.assert_called_once_with(
                    ("127.0.0.1", 8765),
                    application,
                    tls_context=None,
                )
                self.assertIs(runtime.journal, journal)
                self.assertIs(runtime.application, application)
                self.assertIs(runtime.server, server)
                self.assertFalse(server.daemon_threads)
                self.assertTrue(server.block_on_close)
            finally:
                runtime.close()

    def test_same_durable_instance_is_process_fenced_until_close(self):
        class DummySecurityBoundary:
            pass

        with TemporaryDirectory() as directory:
            config = ProductionHostConfig(
                journal_path=Path(directory).resolve() / "journal.sqlite3",
                account_id="paper-account",
                environment="PAPER",
                host_id="host-a",
                bind_host="127.0.0.1",
                bind_port=8765,
                public_origin="http://127.0.0.1:8765",
            )
            server_factory = Mock(side_effect=lambda *args, **kwargs: Mock())
            with (
                patch.object(production_host, "SecurityBoundary", DummySecurityBoundary),
                patch.object(
                    production_host,
                    "AuthenticatedHostServer",
                    server_factory,
                ),
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

                successor = build_production_host(
                    config,
                    security_boundary=DummySecurityBoundary(),
                    principal_resolver=lambda headers, origin: None,
                    snapshot_provider=lambda state, principal: {},
                )
                successor.close()

    def test_construction_failure_releases_instance_fence(self):
        class DummySecurityBoundary:
            pass

        with TemporaryDirectory() as directory:
            config = ProductionHostConfig(
                journal_path=Path(directory).resolve() / "journal.sqlite3",
                account_id="paper-account",
                environment="PAPER",
                host_id="host-a",
                bind_host="127.0.0.1",
                bind_port=8765,
                public_origin="http://127.0.0.1:8765",
            )
            with (
                patch.object(production_host, "SecurityBoundary", DummySecurityBoundary),
                patch.object(
                    production_host,
                    "AuthenticatedHostServer",
                    side_effect=OSError("listener failed"),
                ),
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
                patch.object(
                    production_host,
                    "AuthenticatedHostServer",
                    return_value=Mock(),
                ),
            ):
                runtime = build_production_host(
                    config,
                    security_boundary=DummySecurityBoundary(),
                    principal_resolver=lambda headers, origin: None,
                    snapshot_provider=lambda state, principal: {},
                )
                runtime.close()

    def test_pre_serve_shutdown_is_idempotent_and_never_waits_for_serve_loop(self):
        server = Mock()
        fence = Mock()
        gate = Mock()
        runtime = self._runtime(server=server, fence=fence, gate=gate)
        runtime.close()
        runtime.close()
        gate.stop_and_drain.assert_called_once_with()
        server.shutdown.assert_not_called()
        server.server_close.assert_called_once_with()
        fence.release.assert_called_once_with()
        self.assertTrue(runtime.closed)
        with self.assertRaisesRegex(RuntimeError, "runtime is closed"):
            runtime.serve_forever()

    def test_close_during_starting_returns_without_shutdown_and_cancels_serve(self):
        server = Mock()
        fence = Mock()
        gate = Mock()
        runtime = self._runtime(server=server, fence=fence, gate=gate)
        admitted = Event()
        release = Event()

        def pause_before_server_entry():
            admitted.set()
            release.wait()

        runtime._serve_entry_hook = pause_before_server_entry
        serve_thread = Thread(target=runtime.serve_forever)
        serve_thread.start()
        self.assertTrue(admitted.wait(timeout=1))

        close_thread = Thread(target=runtime.close)
        close_thread.start()
        close_thread.join(timeout=1)
        self.assertFalse(close_thread.is_alive())
        server.shutdown.assert_not_called()
        server.server_close.assert_called_once_with()
        fence.release.assert_called_once_with()

        release.set()
        serve_thread.join(timeout=1)
        self.assertFalse(serve_thread.is_alive())
        server.serve_forever.assert_not_called()
        self.assertTrue(runtime.closed)

    def test_active_serve_shutdown_waits_for_server_loop_before_listener_close(self):
        entered = Event()
        stop = Event()
        ordering = []
        server = Mock()
        gate = Mock()
        fence = Mock()

        def serve_forever(*, poll_interval):
            self.assertGreater(poll_interval, 0)
            entered.set()
            stop.wait()
            ordering.append("serve-exited")

        def shutdown():
            ordering.append("shutdown")
            stop.set()

        server.serve_forever.side_effect = serve_forever
        server.shutdown.side_effect = shutdown
        server.server_close.side_effect = lambda: ordering.append("listener-closed")
        fence.release.side_effect = lambda: ordering.append("fence-released")
        runtime = self._runtime(server=server, fence=fence, gate=gate)
        caller = Thread(target=runtime.serve_forever)
        caller.start()
        self.assertTrue(entered.wait(timeout=1))

        runtime.close()
        caller.join(timeout=1)
        self.assertFalse(caller.is_alive())
        self.assertEqual(
            ordering,
            ["shutdown", "serve-exited", "listener-closed", "fence-released"],
        )

    def test_fence_is_released_after_listener_close(self):
        ordering = []
        server = Mock()
        fence = Mock()
        server.server_close.side_effect = lambda: ordering.append("listener-closed")
        fence.release.side_effect = lambda: ordering.append("fence-released")
        runtime = self._runtime(server=server, fence=fence)
        runtime.close()
        self.assertEqual(ordering, ["listener-closed", "fence-released"])

    def test_https_requires_tls_and_tls_requires_https(self):
        class DummySecurityBoundary:
            pass

        with TemporaryDirectory() as directory:
            path = Path(directory).resolve() / "journal.sqlite3"
            https_config = ProductionHostConfig(
                journal_path=path,
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
                        https_config,
                        security_boundary=DummySecurityBoundary(),
                        principal_resolver=lambda headers, origin: None,
                        snapshot_provider=lambda state, principal: {},
                    )


if __name__ == "__main__":
    unittest.main()
