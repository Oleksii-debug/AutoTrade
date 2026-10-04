from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Condition
import unittest

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
        host = object.__new__(ProductionHostRuntime)
        host.config = ProductionHostConfig(
            journal_path=journal.path,
            account_id="acct",
            environment="PAPER",
            host_id="host-a",
            bind_host="127.0.0.1",
            bind_port=19081,
            public_origin="http://127.0.0.1:19081",
        )
        host.journal = journal
        host.store_identity = journal.store_identity
        host._lifecycle_condition = Condition()
        host._serve_state = "IDLE"
        host._instance_fence = _FenceStub()
        return host

    def test_host_identity_mutation_invalidates_retained_dispatcher_before_callbacks(self):
        with TemporaryDirectory() as directory:
            host = self._host(directory)
            runtime = compose_financial_authority(host)
            dispatcher = runtime.financial_dispatcher
            authority_calls = []
            wire_calls = []

            object.__setattr__(host.config, "host_id", "host-retargeted")

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

    def test_equivalent_config_object_replacement_fails_closed(self):
        with TemporaryDirectory() as directory:
            host = self._host(directory)
            runtime = compose_financial_authority(host)
            original = host.config
            host.config = ProductionHostConfig(
                journal_path=original.journal_path,
                account_id=original.account_id,
                environment=original.environment,
                host_id=original.host_id,
                bind_host=original.bind_host,
                bind_port=original.bind_port,
                public_origin=original.public_origin,
            )

            with self.assertRaisesRegex(
                PermissionError,
                "config authority changed",
            ):
                _ = runtime.takeover_required

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
