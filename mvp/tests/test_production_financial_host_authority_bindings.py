from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Condition
import unittest

from mvp.autotrade_mvp import production_host
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.production_financial_host import compose_financial_authority
from mvp.autotrade_mvp.production_host import ProductionHostConfig, ProductionHostRuntime
from mvp.autotrade_mvp.recovery import RecoveryController


class _FenceStub:
    def __init__(self) -> None:
        self.released = False


class ProductionFinancialHostAuthorityBindingTests(unittest.TestCase):
    def _host(self, directory: str) -> ProductionHostRuntime:
        journal = JournalStore(Path(directory) / "journal.sqlite3")
        config = ProductionHostConfig(
            journal_path=journal.path,
            account_id="acct",
            environment="PAPER",
            host_id="host-a",
            bind_host="127.0.0.1",
            bind_port=19081,
            public_origin="http://127.0.0.1:19081",
        )
        return ProductionHostRuntime(
            config=config,
            journal=journal,
            application=object(),
            server=object(),
            instance_fence=_FenceStub(),
            admission_gate=object(),
            issuance_token=production_host._RUNTIME_ISSUANCE_TOKEN,
        )

    def _replace_bound_config(
        self,
        host: ProductionHostRuntime,
        config: ProductionHostConfig,
    ) -> None:
        with production_host._RUNTIME_CONFIG_BINDINGS_LOCK:
            production_host._RUNTIME_CONFIG_BINDINGS[host] = config

    def test_internal_host_identity_retarget_invalidates_dispatcher_before_callbacks(self):
        with TemporaryDirectory() as directory:
            host = self._host(directory)
            runtime = compose_financial_authority(host)
            dispatcher = runtime.financial_dispatcher
            authority_calls = []
            wire_calls = []
            current = host.config
            self._replace_bound_config(
                host,
                ProductionHostConfig(
                    journal_path=current.journal_path,
                    account_id=current.account_id,
                    environment=current.environment,
                    host_id="host-retargeted",
                    bind_host=current.bind_host,
                    bind_port=current.bind_port,
                    public_origin=current.public_origin,
                ),
            )

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
                    now="2026-10-04T02:20:00Z",
                    authority_check=lambda *_args: authority_calls.append("authority"),
                    transport_send=lambda *_args: wire_calls.append("wire"),
                )

            self.assertEqual(authority_calls, [])
            self.assertEqual(wire_calls, [])

    def test_detached_public_config_mutation_does_not_retarget_financial_authority(self):
        with TemporaryDirectory() as directory:
            host = self._host(directory)
            runtime = compose_financial_authority(host)
            exposed = runtime.config
            object.__setattr__(exposed, "host_id", "host-retargeted")
            object.__setattr__(exposed, "environment", "LIVE")
            object.__setattr__(exposed, "account_id", "other")

            self.assertFalse(runtime.takeover_required)
            self.assertEqual(runtime.config.host_id, "host-a")
            self.assertEqual(runtime.config.environment, "PAPER")
            self.assertEqual(runtime.config.account_id, "acct")
            self.assertEqual(runtime.recovery_controller.owner.owner_id, "host-a")

    def test_equivalent_internal_config_replacement_preserves_semantic_authority(self):
        with TemporaryDirectory() as directory:
            host = self._host(directory)
            runtime = compose_financial_authority(host)
            current = host.config
            self._replace_bound_config(
                host,
                ProductionHostConfig(
                    journal_path=current.journal_path,
                    account_id=current.account_id,
                    environment=current.environment,
                    host_id=current.host_id,
                    bind_host=current.bind_host,
                    bind_port=current.bind_port,
                    public_origin=current.public_origin,
                ),
            )

            self.assertFalse(runtime.takeover_required)
            self.assertEqual(runtime.config.host_id, "host-a")

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
                    now="2026-10-04T02:20:01Z",
                    authority_check=lambda *_args: authority_calls.append("authority"),
                    transport_send=lambda *_args: wire_calls.append("wire"),
                )

            self.assertEqual(authority_calls, [])
            self.assertEqual(wire_calls, [])

    def test_recovery_controller_reference_is_read_only(self):
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

    def test_lifecycle_condition_retarget_fails_closed(self):
        with TemporaryDirectory() as directory:
            host = self._host(directory)
            runtime = compose_financial_authority(host)
            dispatcher = runtime.financial_dispatcher
            host._lifecycle_condition = Condition()

            with self.assertRaisesRegex(
                PermissionError,
                "lifecycle authority changed",
            ):
                dispatcher.dispatch(
                    attempt_id="attempt-retarget-lifecycle",
                    intent_id="intent-retarget-lifecycle",
                    intent_hash="hash-retarget-lifecycle",
                    provider="BYBIT",
                    request={"symbol": "BTCUSDT"},
                    now="2026-10-04T02:20:02Z",
                    authority_check=lambda *_args: (True, "allowed"),
                    transport_send=lambda *_args: {"status": "must-not-send"},
                )

    def test_instance_fence_retarget_fails_closed(self):
        with TemporaryDirectory() as directory:
            host = self._host(directory)
            runtime = compose_financial_authority(host)
            dispatcher = runtime.financial_dispatcher
            host._instance_fence = _FenceStub()

            with self.assertRaisesRegex(
                PermissionError,
                "instance-fence authority changed",
            ):
                dispatcher.dispatch(
                    attempt_id="attempt-retarget-fence",
                    intent_id="intent-retarget-fence",
                    intent_hash="hash-retarget-fence",
                    provider="BYBIT",
                    request={"symbol": "BTCUSDT"},
                    now="2026-10-04T02:20:03Z",
                    authority_check=lambda *_args: (True, "allowed"),
                    transport_send=lambda *_args: {"status": "must-not-send"},
                )


if __name__ == "__main__":
    unittest.main()
