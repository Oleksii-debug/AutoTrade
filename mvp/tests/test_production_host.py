from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from mvp.autotrade_mvp import production_host
from mvp.autotrade_mvp.production_host import (
    ProductionHostConfig,
    ProductionHostRuntime,
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

    def test_listener_identity_must_match_public_origin(self):
        with TemporaryDirectory() as directory:
            path = Path(directory).resolve() / "journal.sqlite3"
            with self.assertRaisesRegex(ValueError, "host must match bind_host"):
                ProductionHostConfig(
                    journal_path=path,
                    account_id="paper-account",
                    environment="PAPER",
                    host_id="host-a",
                    bind_host="127.0.0.1",
                    bind_port=8765,
                    public_origin="http://localhost:8765",
                )
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

    def test_canonical_runtime_identity_is_retained(self):
        with TemporaryDirectory() as directory:
            path = Path(directory).resolve() / "journal.sqlite3"
            config = ProductionHostConfig(
                journal_path=path,
                account_id="paper-account",
                environment="PAPER",
                host_id="host-a",
                bind_host="127.0.0.1",
                bind_port=8765,
                public_origin="http://127.0.0.1:8765",
            )
            self.assertEqual(config.journal_path, path)
            self.assertEqual(config.account_id, "paper-account")
            self.assertEqual(config.environment, "PAPER")
            self.assertEqual(config.public_origin, "http://127.0.0.1:8765")


class ProductionHostCompositionTests(unittest.TestCase):
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
            application = object()
            server = object()

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

    def test_pre_serve_shutdown_is_idempotent_and_never_waits_for_serve_loop(self):
        server = Mock()
        runtime = ProductionHostRuntime(
            config=Mock(),
            journal=Mock(),
            application=Mock(),
            server=server,
        )
        runtime.close()
        runtime.close()
        server.shutdown.assert_not_called()
        server.server_close.assert_called_once_with()
        self.assertTrue(runtime.closed)
        with self.assertRaisesRegex(RuntimeError, "runtime is closed"):
            runtime.serve_forever()

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
                bind_host="example.com",
                bind_port=443,
                public_origin="https://example.com",
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
