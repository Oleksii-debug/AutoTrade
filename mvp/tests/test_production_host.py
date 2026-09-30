from pathlib import Path
from tempfile import TemporaryDirectory
import os
import unittest
from unittest.mock import Mock, patch

from _test_production_host_impl import (
    CommandAdmissionGateTests,
    ProductionHostConfigTests,
    ProductionHostRuntimeTests,
)
from mvp.autotrade_mvp import host_network, production_host
from mvp.autotrade_mvp.host_network import TransportResponse
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.production_host import (
    ProductionHostConfig,
    _InstanceFence,
    _StoreIdentityGate,
    build_production_host,
)


class InstanceFenceTests(unittest.TestCase):
    def test_release_failure_is_terminal_and_never_claimed_released(self):
        failure = OSError("unlock failed")
        lock = Mock()
        lock.path = Path("host.lock")
        lock.release.side_effect = failure
        fence = _InstanceFence(lock)

        with self.assertRaises(OSError) as first:
            fence.release()
        self.assertIs(first.exception, failure)
        self.assertFalse(fence.released)
        lock.release.assert_called_once_with()

        with self.assertRaises(OSError) as second:
            fence.release()
        self.assertIs(second.exception, failure)
        lock.release.assert_called_once_with()


class ProductionHostCompositionTests(unittest.TestCase):
    class DummySecurityBoundary:
        pass

    @staticmethod
    def _config(directory: str, *, journal_name: str = "journal.sqlite3") -> ProductionHostConfig:
        return ProductionHostConfig(
            journal_path=Path(directory).resolve() / journal_name,
            account_id="paper-account",
            environment="PAPER",
            host_id="host-a",
            bind_host="127.0.0.1",
            bind_port=8765,
            public_origin="http://127.0.0.1:8765",
        )

    def _composition_patches(self, *, application=None, server=None):
        application = application or Mock()
        server = server or Mock()
        return (
            patch.object(production_host, "SecurityBoundary", self.DummySecurityBoundary),
            patch.object(host_network, "SecurityBoundary", self.DummySecurityBoundary),
            patch.object(
                production_host,
                "AuthenticatedHostApplication",
                return_value=application,
            ),
            patch.object(
                production_host,
                "AuthenticatedHostServer",
                return_value=server,
            ),
        )

    def test_build_reuses_canonical_components_and_retains_store_identity(self):
        with TemporaryDirectory() as directory:
            config = self._config(directory)
            security = self.DummySecurityBoundary()
            identity = object()
            journal = Mock()
            journal.path = config.journal_path
            journal.store_identity = identity
            application = Mock()
            server = Mock()
            fence = Mock()
            with (
                patch.object(production_host, "SecurityBoundary", self.DummySecurityBoundary),
                patch.object(production_host, "_InstanceFence") as fence_type,
                patch.object(
                    production_host,
                    "JournalStore",
                    return_value=journal,
                ) as journal_factory,
                patch.object(
                    production_host,
                    "AuthenticatedHostApplication",
                    return_value=application,
                ) as app_factory,
                patch.object(
                    production_host,
                    "AuthenticatedHostServer",
                    return_value=server,
                ) as server_factory,
            ):
                fence_type.acquire.return_value = fence
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
                self.assertIs(runtime.store_identity, identity)
                self.assertIs(runtime.application, application)
                self.assertIs(runtime.server, server)
                self.assertFalse(server.daemon_threads)
                self.assertTrue(server.block_on_close)
                self.assertEqual(application.dispatch.__self__.__class__.__name__, "_CommandAdmissionGate")
            finally:
                runtime.close()
            fence.release.assert_called_once_with()

    def test_resource_lock_failure_constructs_no_financial_or_listener_components(self):
        with TemporaryDirectory() as directory:
            config = self._config(directory)
            lock = Mock()
            lock.path = Path(str(config.journal_path) + ".host.lock")
            lock.acquire.side_effect = production_host.ResourceLockBusyError("busy")
            with (
                patch.object(production_host, "SecurityBoundary", self.DummySecurityBoundary),
                patch.object(production_host, "ResourceLock", return_value=lock),
                patch.object(production_host, "JournalStore") as journal_factory,
                patch.object(production_host, "AuthenticatedHostApplication") as app_factory,
                patch.object(production_host, "AuthenticatedHostServer") as server_factory,
            ):
                with self.assertRaisesRegex(RuntimeError, "already owned"):
                    build_production_host(
                        config,
                        security_boundary=self.DummySecurityBoundary(),
                        principal_resolver=Mock(),
                        snapshot_provider=Mock(),
                    )
            journal_factory.assert_not_called()
            app_factory.assert_not_called()
            server_factory.assert_not_called()

    def test_same_instance_is_fenced_and_successor_reuses_same_store_identity(self):
        with TemporaryDirectory() as directory:
            config = self._config(directory)
            patches = self._composition_patches()
            with patches[0], patches[1], patches[2], patches[3]:
                first = build_production_host(
                    config,
                    security_boundary=self.DummySecurityBoundary(),
                    principal_resolver=Mock(),
                    snapshot_provider=Mock(),
                )
                first_identity = first.store_identity
                try:
                    with self.assertRaisesRegex(RuntimeError, "already owned"):
                        build_production_host(
                            config,
                            security_boundary=self.DummySecurityBoundary(),
                            principal_resolver=Mock(),
                            snapshot_provider=Mock(),
                        )
                finally:
                    first.close()

                successor = build_production_host(
                    config,
                    security_boundary=self.DummySecurityBoundary(),
                    principal_resolver=Mock(),
                    snapshot_provider=Mock(),
                )
                try:
                    self.assertEqual(successor.store_identity, first_identity)
                finally:
                    successor.close()

    def test_hardlink_alias_fails_through_journal_identity_before_second_listener(self):
        with TemporaryDirectory() as directory:
            config = self._config(directory)
            application = Mock()
            server_factory = Mock(return_value=Mock())
            with (
                patch.object(production_host, "SecurityBoundary", self.DummySecurityBoundary),
                patch.object(host_network, "SecurityBoundary", self.DummySecurityBoundary),
                patch.object(
                    production_host,
                    "AuthenticatedHostApplication",
                    return_value=application,
                ),
                patch.object(
                    production_host,
                    "AuthenticatedHostServer",
                    server_factory,
                ),
            ):
                first = build_production_host(
                    config,
                    security_boundary=self.DummySecurityBoundary(),
                    principal_resolver=Mock(),
                    snapshot_provider=Mock(),
                )
                alias = Path(directory).resolve() / "journal-alias.sqlite3"
                try:
                    try:
                        os.link(config.journal_path, alias)
                    except OSError as error:
                        self.skipTest(f"hard links unavailable on this runner: {error}")
                    alias_config = self._config(directory, journal_name=alias.name)
                    with self.assertRaisesRegex(RuntimeError, "hard-link|identity"):
                        build_production_host(
                            alias_config,
                            security_boundary=self.DummySecurityBoundary(),
                            principal_resolver=Mock(),
                            snapshot_provider=Mock(),
                        )
                    self.assertEqual(server_factory.call_count, 1)
                finally:
                    if alias.exists():
                        alias.unlink()
                    first.close()

    def test_store_identity_gate_rejects_rebind_before_dispatch(self):
        with TemporaryDirectory() as directory:
            path = Path(directory).resolve() / "journal.sqlite3"
            journal = JournalStore(path)
            application = Mock()
            application.dispatch.return_value = TransportResponse(
                200, "application/json", b"{}"
            )
            gate = _StoreIdentityGate(application, journal)
            backup = path.with_name("journal-old.sqlite3")
            path.replace(backup)
            path.touch()
            with self.assertRaises(RuntimeError):
                gate.dispatch(method="GET", target="/api/v1/state", headers={})
            application.dispatch.assert_not_called()

    def test_store_identity_gate_rejects_rebind_during_dispatch(self):
        with TemporaryDirectory() as directory:
            path = Path(directory).resolve() / "journal.sqlite3"
            journal = JournalStore(path)
            backup = path.with_name("journal-old.sqlite3")
            application = Mock()

            def dispatch(**kwargs):
                del kwargs
                path.replace(backup)
                path.touch()
                return TransportResponse(200, "application/json", b"{}")

            application.dispatch = dispatch
            gate = _StoreIdentityGate(application, journal)
            with self.assertRaises(RuntimeError):
                gate.dispatch(method="GET", target="/api/v1/state", headers={})
            self.assertTrue(backup.exists())

    def test_listener_construction_failure_releases_instance_fence(self):
        with TemporaryDirectory() as directory:
            config = self._config(directory)
            with (
                patch.object(production_host, "SecurityBoundary", self.DummySecurityBoundary),
                patch.object(host_network, "SecurityBoundary", self.DummySecurityBoundary),
                patch.object(
                    production_host,
                    "AuthenticatedHostServer",
                    side_effect=OSError("listener failed"),
                ),
            ):
                with self.assertRaisesRegex(OSError, "listener failed"):
                    build_production_host(
                        config,
                        security_boundary=self.DummySecurityBoundary(),
                        principal_resolver=Mock(),
                        snapshot_provider=Mock(),
                    )
            with (
                patch.object(production_host, "SecurityBoundary", self.DummySecurityBoundary),
                patch.object(host_network, "SecurityBoundary", self.DummySecurityBoundary),
                patch.object(
                    production_host,
                    "AuthenticatedHostServer",
                    return_value=Mock(),
                ),
            ):
                successor = build_production_host(
                    config,
                    security_boundary=self.DummySecurityBoundary(),
                    principal_resolver=Mock(),
                    snapshot_provider=Mock(),
                )
                successor.close()

    def test_https_requires_tls_context(self):
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
            with patch.object(
                production_host,
                "SecurityBoundary",
                self.DummySecurityBoundary,
            ):
                with self.assertRaisesRegex(ValueError, "requires tls_context"):
                    build_production_host(
                        config,
                        security_boundary=self.DummySecurityBoundary(),
                        principal_resolver=Mock(),
                        snapshot_provider=Mock(),
                    )


if __name__ == "__main__":
    unittest.main()
