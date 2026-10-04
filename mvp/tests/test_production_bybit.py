from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Condition
import unittest
from unittest.mock import Mock, patch

from mvp.autotrade_mvp.capabilities import CapabilityRegistry
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.production_bybit import (
    ProductionBybitOrderSender,
    build_production_bybit_order_sender,
)
from mvp.autotrade_mvp.production_financial_host import compose_financial_authority
from mvp.autotrade_mvp.production_host import ProductionHostConfig, ProductionHostRuntime
from mvp.autotrade_mvp.provider_transport import (
    BybitV5HttpTransport,
    ProviderTransportScopeError,
)
from mvp.autotrade_mvp.recovery import HostState
from mvp.autotrade_mvp.windows_secrets import PersistentCredentialHandle


_NOW = datetime(2026, 10, 4, 2, 0, tzinfo=timezone.utc)


class _FenceStub:
    def __init__(self) -> None:
        self.released = False


class _LeaseBoundary:
    def __init__(self) -> None:
        self.calls = []

    @contextmanager
    def lease_for_execution(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        yield "secret"


class _ApplicationStub:
    def __init__(self, boundary) -> None:
        self.security_boundary = boundary


class ProductionBybitTests(unittest.TestCase):
    def _runtime(self, root: str, *, environment: str = "PAPER"):
        journal = JournalStore(Path(root) / "financial-host.sqlite")
        host = object.__new__(ProductionHostRuntime)
        host.config = ProductionHostConfig(
            journal_path=journal.path,
            account_id="account-1",
            environment=environment,
            host_id="host-a",
            bind_host="127.0.0.1",
            bind_port=18765,
            public_origin="http://127.0.0.1:18765",
        )
        host.journal = journal
        host.store_identity = journal.store_identity
        host._lifecycle_condition = Condition()
        host._serve_state = "IDLE"
        host._instance_fence = _FenceStub()
        boundary = _LeaseBoundary()
        host.application = _ApplicationStub(boundary)
        return compose_financial_authority(host), boundary

    @staticmethod
    def _handle(
        *,
        environment: str = "PAPER",
        account_id: str = "account-1",
        provider_environment: str = "TESTNET",
    ) -> PersistentCredentialHandle:
        return PersistentCredentialHandle(
            handle_id="cred-bybit-trade",
            account_id=account_id,
            provider="BYBIT",
            environment=environment,
            provider_environment=provider_environment,
            purpose="TRADE",
            generation=1,
        )

    def test_builder_binds_transport_to_exact_host_scope_and_owner(self) -> None:
        with TemporaryDirectory() as root:
            runtime, _boundary = self._runtime(root)
            sender = build_production_bybit_order_sender(
                runtime,
                provider_environment="TESTNET",
                capability_snapshot_id="capability-1",
                capability_registry=CapabilityRegistry(),
                credential_handle=self._handle(),
                session_token="session-1",
                clock_millis=lambda: 1_700_000_000_000,
                clock_utc=lambda: _NOW,
                wire_client=Mock(),
            )

            self.assertIsInstance(sender, ProductionBybitOrderSender)
            self.assertEqual(sender.provider_environment, "TESTNET")
            transport = sender._ProductionBybitOrderSender__transport
            self.assertEqual(transport.account_id, "account-1")
            self.assertEqual(transport.policy.environment, "PAPER")
            self.assertEqual(transport.origin, runtime.config.public_origin)
            self.assertEqual(
                transport.execution_identity,
                runtime.financial_dispatcher.owner.owner_id,
            )

    def test_paper_host_refuses_mainnet_transport(self) -> None:
        with TemporaryDirectory() as root:
            runtime, _boundary = self._runtime(root)
            with self.assertRaisesRegex(
                PermissionError,
                "provider environment does not match production host environment",
            ):
                build_production_bybit_order_sender(
                    runtime,
                    provider_environment="MAINNET",
                    capability_snapshot_id="capability-1",
                    capability_registry=CapabilityRegistry(),
                    credential_handle=self._handle(
                        environment="LIVE",
                        provider_environment="MAINNET",
                    ),
                    session_token="session-1",
                    clock_millis=lambda: 1_700_000_000_000,
                    clock_utc=lambda: _NOW,
                )

    def test_live_host_refuses_testnet_transport(self) -> None:
        with TemporaryDirectory() as root:
            runtime, _boundary = self._runtime(root, environment="LIVE")
            with self.assertRaisesRegex(
                PermissionError,
                "provider environment does not match production host environment",
            ):
                build_production_bybit_order_sender(
                    runtime,
                    provider_environment="TESTNET",
                    capability_snapshot_id="capability-1",
                    capability_registry=CapabilityRegistry(),
                    credential_handle=self._handle(),
                    session_token="session-1",
                    clock_millis=lambda: 1_700_000_000_000,
                    clock_utc=lambda: _NOW,
                )

    def test_transport_secret_adapter_preserves_pinned_trade_scope(self) -> None:
        with TemporaryDirectory() as root:
            runtime, boundary = self._runtime(root)
            recovery = runtime.recovery_controller
            recovery.state = HostState.READY
            recovery.provider_reconciled = True
            recovery.reason_codes.clear()
            sender = build_production_bybit_order_sender(
                runtime,
                provider_environment="TESTNET",
                capability_snapshot_id="capability-1",
                capability_registry=CapabilityRegistry(),
                credential_handle=self._handle(),
                session_token="session-1",
                clock_millis=lambda: 1_700_000_000_000,
                clock_utc=lambda: _NOW,
                wire_client=Mock(),
            )
            transport = sender._ProductionBybitOrderSender__transport
            resolver = transport.secret_resolver

            with resolver.lease_for_execution(
                "session-1",
                origin=runtime.config.public_origin,
                handle=self._handle(),
                execution_identity=runtime.financial_dispatcher.owner.owner_id,
                account_id=runtime.config.account_id,
                provider="BYBIT",
                environment=runtime.config.environment,
                purpose="TRADE",
                provider_environment="TESTNET",
            ) as plaintext:
                self.assertEqual(plaintext, "secret")

            self.assertEqual(len(boundary.calls), 1)
            _, kwargs = boundary.calls[0]
            self.assertEqual(kwargs["account_id"], "account-1")
            self.assertEqual(kwargs["environment"], "PAPER")
            self.assertEqual(kwargs["purpose"], "TRADE")
            self.assertEqual(kwargs["provider"], "BYBIT")

            with self.assertRaisesRegex(
                PermissionError,
                "does not match production host authority",
            ):
                with resolver.lease_for_execution(
                    "session-1",
                    origin=runtime.config.public_origin,
                    handle=self._handle(),
                    execution_identity=runtime.financial_dispatcher.owner.owner_id,
                    account_id="other-account",
                    provider="BYBIT",
                    environment=runtime.config.environment,
                    purpose="TRADE",
                    provider_environment="TESTNET",
                ):
                    self.fail("cross-account credential scope was accepted")

    def test_existing_transport_still_rejects_wrong_credential_account(self) -> None:
        with TemporaryDirectory() as root:
            runtime, _boundary = self._runtime(root)
            with self.assertRaisesRegex(
                ProviderTransportScopeError,
                "credential handle account mismatch",
            ):
                build_production_bybit_order_sender(
                    runtime,
                    provider_environment="TESTNET",
                    capability_snapshot_id="capability-1",
                    capability_registry=CapabilityRegistry(),
                    credential_handle=self._handle(account_id="other-account"),
                    session_token="session-1",
                    clock_millis=lambda: 1_700_000_000_000,
                    clock_utc=lambda: _NOW,
                )

    def test_sender_uses_host_dispatcher_and_fixed_bybit_transport(self) -> None:
        with TemporaryDirectory() as root:
            runtime, _boundary = self._runtime(root)
            recovery = runtime.recovery_controller
            recovery.state = HostState.READY
            recovery.provider_reconciled = True
            recovery.reason_codes.clear()
            sender = build_production_bybit_order_sender(
                runtime,
                provider_environment="TESTNET",
                capability_snapshot_id="capability-1",
                capability_registry=CapabilityRegistry(),
                credential_handle=self._handle(),
                session_token="session-1",
                clock_millis=lambda: 1_700_000_000_000,
                clock_utc=lambda: _NOW,
                wire_client=Mock(),
            )
            wire_calls = []

            def fake_transport(_self, _client_id, _request, final_guard):
                final_guard()
                wire_calls.append("wire")
                return {"provider_order_id": "provider-1"}

            with patch.object(BybitV5HttpTransport, "__call__", fake_transport):
                result = sender.dispatch(
                    attempt_id="attempt-1",
                    intent_id="intent-1",
                    intent_hash="sha256:" + "1" * 64,
                    request={"symbol": "BTCUSDT"},
                    now="2026-10-04T02:00:00Z",
                    authority_check=lambda _hash, _now: (True, "allowed"),
                )

            self.assertEqual(result.status, "SENT")
            self.assertEqual(wire_calls, ["wire"])
            with self.assertRaises(TypeError):
                sender.dispatch(
                    attempt_id="attempt-2",
                    intent_id="intent-2",
                    intent_hash="sha256:" + "2" * 64,
                    request={"symbol": "BTCUSDT"},
                    now="2026-10-04T02:00:01Z",
                    authority_check=lambda _hash, _now: (True, "allowed"),
                    sender_check=lambda *_args: None,
                )


if __name__ == "__main__":
    unittest.main()
