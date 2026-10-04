from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.production_financial_host import (
    build_production_financial_host,
)
from mvp.autotrade_mvp.production_host import (
    ProductionHostConfig,
    ProductionHostRuntime,
)
from mvp.autotrade_mvp.recovery import HostState, RecoveryController


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

    def test_close_stops_recovery_before_host_fence_teardown(self) -> None:
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
            original_stop = runtime.recovery_controller.stop

            def stop_recovery() -> None:
                order.append("recovery")
                original_stop()

            runtime.recovery_controller.stop = stop_recovery  # type: ignore[method-assign]
            host.close = Mock(side_effect=lambda: order.append("host"))  # type: ignore[method-assign]

            runtime.close()

            self.assertEqual(order, ["recovery", "host"])
            self.assertEqual(runtime.recovery_controller.state, HostState.STOPPED)
            self.assertIsNone(runtime.recovery_controller.owner)
            host.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
