from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
import unittest
from unittest.mock import Mock, patch

from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.production_financial_host import (
    HostLifetimeProviderSecretResolver,
    build_production_financial_host,
)
from mvp.autotrade_mvp.production_host import (
    ProductionHostConfig,
    ProductionHostRuntime,
)
from mvp.autotrade_mvp.recovery import HostState, RecoveryController


class _LeaseBoundary:
    def __init__(self) -> None:
        self.calls = []

    @contextmanager
    def lease_for_execution(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        yield "secret"


class ProductionFinancialHostTests(unittest.TestCase):
    def _config(self, root: str, *, host_id: str = "host-a") -> ProductionHostConfig:
        return ProductionHostConfig(
            journal_path=Path(root) / "financial-host.sqlite",
            account_id="account-1",
            environment="PAPER",
            host_id=host_id,
            bind_host="127.0.0.1",
            bind_port=18765,
            public_origin="http://127.0.0.1:18765",
        )

    def _host(self, config: ProductionHostConfig) -> ProductionHostRuntime:
        journal = JournalStore(config.journal_path)
        host = ProductionHostRuntime(
            config=config,
            journal=journal,
            application=Mock(),
            server=Mock(),
            instance_fence=Mock(),
            admission_gate=Mock(),
        )
        host.close = Mock()  # type: ignore[method-assign]
        return host

    def test_builder_claims_first_owner_on_exact_host_journal_scope(self) -> None:
        with TemporaryDirectory() as root:
            config = self._config(root)
            host = self._host(config)

            with patch(
                "mvp.autotrade_mvp.production_financial_host.build_production_host",
                return_value=host,
            ):
                runtime = build_production_financial_host(
                    config,
                    security_boundary=Mock(),
                    principal_resolver=Mock(),
                    snapshot_provider=Mock(),
                )

            self.assertIs(runtime.host, host)
            self.assertIs(runtime.journal, host.journal)
            self.assertEqual(runtime.recovery_controller.owner_scope, "PAPER:account-1")
            self.assertEqual(runtime.recovery_controller.owner, runtime.owner)
            self.assertEqual(runtime.owner.owner_id, "host-a")
            self.assertEqual(runtime.owner.epoch, 1)
            self.assertEqual(runtime.recovery_controller.state, HostState.RECOVERING)
            self.assertTrue(runtime.provider_secret_resolver.accepting)
            self.assertEqual(runtime.provider_secret_resolver.active_leases, 0)
            events = host.journal.load_events("recovery_owner", "PAPER:account-1")
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["payload"]["owner_id"], "host-a")
            self.assertEqual(events[0]["payload"]["owner_epoch"], "1")

    def test_existing_durable_owner_fails_closed_and_releases_host(self) -> None:
        with TemporaryDirectory() as root:
            config = self._config(root, host_id="host-b")
            host = self._host(config)
            prior = RecoveryController(
                owner_store=host.journal,
                owner_scope="PAPER:account-1",
            )
            prior.start("host-a")

            with patch(
                "mvp.autotrade_mvp.production_financial_host.build_production_host",
                return_value=host,
            ):
                with self.assertRaisesRegex(
                    PermissionError,
                    "Existing durable owner requires explicit takeover evidence",
                ):
                    build_production_financial_host(
                        config,
                        security_boundary=Mock(),
                        principal_resolver=Mock(),
                        snapshot_provider=Mock(),
                    )

            host.close.assert_called_once_with()
            events = host.journal.load_events("recovery_owner", "PAPER:account-1")
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["payload"]["owner_id"], "host-a")

    def test_close_drains_commands_then_provider_then_recovery_then_host(self) -> None:
        with TemporaryDirectory() as root:
            config = self._config(root)
            host = self._host(config)
            with patch(
                "mvp.autotrade_mvp.production_financial_host.build_production_host",
                return_value=host,
            ):
                runtime = build_production_financial_host(
                    config,
                    security_boundary=Mock(),
                    principal_resolver=Mock(),
                    snapshot_provider=Mock(),
                )

            order: list[str] = []
            original_drain = runtime.provider_secret_resolver.stop_and_drain
            original_stop = runtime.recovery_controller.stop
            host._admission_gate.stop_and_drain = Mock(  # type: ignore[method-assign]
                side_effect=lambda: order.append("commands")
            )

            def drain_provider() -> None:
                order.append("provider")
                original_drain()

            def stop_recovery() -> None:
                order.append("recovery")
                original_stop()

            runtime.provider_secret_resolver.stop_and_drain = drain_provider  # type: ignore[method-assign]
            runtime.recovery_controller.stop = stop_recovery  # type: ignore[method-assign]
            host.close = Mock(
                side_effect=lambda: (
                    host._admission_gate.stop_and_drain(),
                    host._terminal_finalizer(),
                    order.append("host"),
                )
            )  # type: ignore[method-assign]

            runtime.close()

            self.assertEqual(order, ["commands", "provider", "recovery", "host"])
            self.assertFalse(runtime.provider_secret_resolver.accepting)
            self.assertEqual(runtime.recovery_controller.state, HostState.STOPPED)
            self.assertIsNone(runtime.recovery_controller.owner)
            host.close.assert_called_once_with()

    def test_command_drain_runs_while_provider_authority_is_still_open(self) -> None:
        with TemporaryDirectory() as root:
            config = self._config(root)
            host = self._host(config)
            with patch(
                "mvp.autotrade_mvp.production_financial_host.build_production_host",
                return_value=host,
            ):
                runtime = build_production_financial_host(
                    config,
                    security_boundary=Mock(),
                    principal_resolver=Mock(),
                    snapshot_provider=Mock(),
                )

            observed: list[bool] = []

            def drain_commands() -> None:
                observed.append(runtime.provider_secret_resolver.accepting)

            host._admission_gate.stop_and_drain = Mock(  # type: ignore[method-assign]
                side_effect=drain_commands
            )
            host.close = Mock(
                side_effect=lambda: (
                    host._admission_gate.stop_and_drain(),
                    host._terminal_finalizer(),
                )
            )  # type: ignore[method-assign]
            runtime.close()

            self.assertEqual(observed, [True])
            self.assertFalse(runtime.provider_secret_resolver.accepting)
            host.close.assert_called_once_with()

    def test_active_provider_lease_blocks_fence_teardown_until_lease_exits(self) -> None:
        with TemporaryDirectory() as root:
            config = self._config(root)
            host = self._host(config)
            with patch(
                "mvp.autotrade_mvp.production_financial_host.build_production_host",
                return_value=host,
            ):
                runtime = build_production_financial_host(
                    config,
                    security_boundary=_LeaseBoundary(),
                    principal_resolver=Mock(),
                    snapshot_provider=Mock(),
                )
            host.close = Mock(
                side_effect=lambda: (
                    host._admission_gate.stop_and_drain(),
                    host._terminal_finalizer(),
                )
            )  # type: ignore[method-assign]

            lease_entered = Event()
            allow_lease_exit = Event()
            drain_started = Event()
            close_finished = Event()
            original_drain = runtime.provider_secret_resolver.stop_and_drain

            def drain_provider() -> None:
                drain_started.set()
                original_drain()

            runtime.provider_secret_resolver.stop_and_drain = drain_provider  # type: ignore[method-assign]

            def hold_lease() -> None:
                with runtime.provider_secret_resolver.lease_for_execution(
                    "token",
                    origin="http://127.0.0.1:18765",
                    handle=object(),
                    execution_identity="host-a",
                    provider="BYBIT",
                    provider_environment="TESTNET",
                ):
                    lease_entered.set()
                    allow_lease_exit.wait()

            lease_thread = Thread(target=hold_lease, daemon=False)
            lease_thread.start()
            self.assertTrue(lease_entered.wait(timeout=2))
            self.assertEqual(runtime.provider_secret_resolver.active_leases, 1)

            def close_runtime() -> None:
                runtime.close()
                close_finished.set()

            close_thread = Thread(target=close_runtime, daemon=False)
            close_thread.start()
            self.assertTrue(drain_started.wait(timeout=2))
            self.assertFalse(runtime.provider_secret_resolver.accepting)
            self.assertFalse(close_finished.is_set())
            host.close.assert_not_called()
            self.assertEqual(runtime.recovery_controller.state, HostState.RECOVERING)

            allow_lease_exit.set()
            lease_thread.join(timeout=2)
            close_thread.join(timeout=2)

            self.assertFalse(lease_thread.is_alive())
            self.assertFalse(close_thread.is_alive())
            self.assertTrue(close_finished.is_set())
            host.close.assert_called_once_with()
            self.assertEqual(runtime.recovery_controller.state, HostState.STOPPED)

    def test_resolver_binds_host_scope_and_trade_purpose(self) -> None:
        boundary = _LeaseBoundary()
        resolver = HostLifetimeProviderSecretResolver(
            boundary,
            account_id="account-1",
            environment="PAPER",
        )
        with resolver.lease_for_execution(
            "token",
            origin="http://127.0.0.1:18765",
            handle=object(),
            execution_identity="windows-user",
            provider="BYBIT",
            provider_environment="TESTNET",
        ) as plaintext:
            self.assertEqual(plaintext, "secret")

        self.assertEqual(len(boundary.calls), 1)
        _, kwargs = boundary.calls[0]
        self.assertEqual(kwargs["account_id"], "account-1")
        self.assertEqual(kwargs["environment"], "PAPER")
        self.assertEqual(kwargs["purpose"], "TRADE")
        self.assertEqual(kwargs["execution_identity"], "windows-user")
        self.assertEqual(kwargs["provider"], "BYBIT")
        self.assertEqual(kwargs["provider_environment"], "TESTNET")

        with self.assertRaises(TypeError):
            with resolver.lease_for_execution(
                "token",
                origin="http://127.0.0.1:18765",
                handle=object(),
                execution_identity="windows-user",
                account_id="other-account",
                provider="BYBIT",
                environment="LIVE",
                purpose="READ",
            ):
                self.fail("caller-selected financial scope was accepted")

    def test_closed_resolver_rejects_new_provider_lease(self) -> None:
        resolver = HostLifetimeProviderSecretResolver(
            _LeaseBoundary(),
            account_id="account-1",
            environment="PAPER",
        )
        resolver.stop_and_drain()
        with self.assertRaisesRegex(PermissionError, "credential leases are closed"):
            with resolver.lease_for_execution(
                "token",
                origin="http://127.0.0.1:18765",
                handle=object(),
                execution_identity="host-a",
                account_id="account-1",
                provider="BYBIT",
                environment="PAPER",
                purpose="TRADE",
                provider_environment="TESTNET",
            ):
                self.fail("closed resolver yielded a secret")


if __name__ == "__main__":
    unittest.main()
