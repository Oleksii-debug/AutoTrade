from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from mvp.autotrade_mvp.bybit_v5 import guarded_order_projection, prepare_order_submission
from mvp.autotrade_mvp.capabilities import CapabilityRegistry
from mvp.autotrade_mvp.dispatch import stable_client_order_id
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.production_bybit import (
    ProductionBybitOrderSender,
    build_production_bybit_order_sender,
)
from mvp.autotrade_mvp.production_financial_host import build_production_financial_host
from mvp.autotrade_mvp.production_host import ProductionHostConfig, ProductionHostRuntime
from mvp.autotrade_mvp.provider_transport import ProviderTransportScopeError
from mvp.autotrade_mvp.recovery import HostState
from mvp.autotrade_mvp.windows_secrets import PersistentCredentialHandle
from mvp.tests.test_bybit_v5 import READ_AT, write_capability


_NOW = datetime(2026, 10, 4, 1, 0, tzinfo=timezone.utc)


class _LeaseBoundary:
    def __init__(self, plaintext: str = "secret") -> None:
        self.calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
        self.plaintext = plaintext

    @contextmanager
    def lease_for_execution(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        yield self.plaintext


class _RecordingWire:
    def __init__(self, response: bytes) -> None:
        self.response = response
        self.requests = []

    def send(self, request):
        self.requests.append(request)
        return self.response


class ProductionBybitTests(unittest.TestCase):
    def _config(self, root: str, *, environment: str = "PAPER") -> ProductionHostConfig:
        return ProductionHostConfig(
            journal_path=Path(root) / "financial-host.sqlite",
            account_id="account-1",
            environment=environment,
            host_id="host-a",
            bind_host="127.0.0.1",
            bind_port=18765,
            public_origin="http://127.0.0.1:18765",
        )

    def _runtime(
        self,
        root: str,
        *,
        environment: str = "PAPER",
        credential_plaintext: str = "secret",
    ):
        config = self._config(root, environment=environment)
        journal = JournalStore(config.journal_path)
        host = ProductionHostRuntime(
            config=config,
            journal=journal,
            application=Mock(),
            server=Mock(),
            instance_fence=Mock(),
            admission_gate=Mock(),
        )
        boundary = _LeaseBoundary(credential_plaintext)
        with patch(
            "mvp.autotrade_mvp.production_financial_host.build_production_host",
            return_value=host,
        ):
            runtime = build_production_financial_host(
                config,
                security_boundary=boundary,
                principal_resolver=Mock(),
                snapshot_provider=Mock(),
            )
        return runtime, boundary

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

    def test_builder_binds_bybit_transport_to_host_account_owner_and_origin(self) -> None:
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
            transport = sender._transport
            self.assertEqual(transport.account_id, "account-1")
            self.assertEqual(transport.policy.environment, "PAPER")
            self.assertEqual(transport.origin, runtime.config.public_origin)
            self.assertEqual(transport.execution_identity, runtime.owner.owner_id)
            self.assertIsNot(transport.secret_resolver, runtime.provider_secret_resolver)

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

    def test_transport_protocol_adapter_preserves_pinned_financial_scope(self) -> None:
        with TemporaryDirectory() as root:
            runtime, boundary = self._runtime(root)
            sender = build_production_bybit_order_sender(
                runtime,
                provider_environment="TESTNET",
                capability_snapshot_id="capability-1",
                capability_registry=CapabilityRegistry(),
                credential_handle=self._handle(),
                session_token="session-1",
                clock_millis=lambda: 1_700_000_000_000,
                clock_utc=lambda: _NOW,
            )
            resolver = sender._transport.secret_resolver

            with resolver.lease_for_execution(
                "session-1",
                origin=runtime.config.public_origin,
                handle=self._handle(),
                execution_identity=runtime.owner.owner_id,
                account_id=runtime.config.account_id,
                provider="BYBIT",
                environment=runtime.config.environment,
                purpose="TRADE",
                provider_environment="TESTNET",
            ) as secret:
                self.assertEqual(secret, "secret")

            self.assertEqual(len(boundary.calls), 1)
            _, kwargs = boundary.calls[0]
            self.assertEqual(kwargs["account_id"], "account-1")
            self.assertEqual(kwargs["environment"], "PAPER")
            self.assertEqual(kwargs["purpose"], "TRADE")
            self.assertEqual(kwargs["execution_identity"], "host-a")
            self.assertEqual(kwargs["provider_environment"], "TESTNET")

            with self.assertRaisesRegex(
                PermissionError,
                "does not match production host authority",
            ):
                with resolver.lease_for_execution(
                    "session-1",
                    origin=runtime.config.public_origin,
                    handle=self._handle(),
                    execution_identity=runtime.owner.owner_id,
                    account_id="other-account",
                    provider="BYBIT",
                    environment=runtime.config.environment,
                    purpose="TRADE",
                    provider_environment="TESTNET",
                ):
                    self.fail("cross-account credential scope was accepted")

    def test_credential_handle_scope_is_still_enforced_by_existing_transport(self) -> None:
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

    def test_sender_dispatch_uses_host_bound_dispatcher_and_fixed_bybit_transport(self) -> None:
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
            )
            marker = object()
            runtime.dispatcher.dispatch = Mock(return_value=marker)  # type: ignore[method-assign]
            authority = Mock(return_value=(True, "allowed"))
            request = {"canonical": "request"}

            result = sender.dispatch(
                attempt_id="attempt-1",
                intent_id="intent-1",
                intent_hash="sha256:" + "1" * 64,
                request=request,
                now="2026-10-04T01:00:00Z",
                authority_check=authority,
            )

            self.assertIs(result, marker)
            kwargs = runtime.dispatcher.dispatch.call_args.kwargs
            self.assertEqual(kwargs["provider"], "BYBIT")
            self.assertIs(kwargs["transport_send"], sender._transport)
            self.assertIs(kwargs["authority_check"], authority)
            self.assertNotIn("sender_check", kwargs)

    def test_end_to_end_guarded_send_reaches_wire_once_and_journals_terminal_result(self) -> None:
        credential = json.dumps(
            {"api_key": "api-key-SECRET", "api_secret": "signing-SECRET"},
            sort_keys=True,
            separators=(",", ":"),
        )
        with TemporaryDirectory() as root:
            runtime, boundary = self._runtime(
                root,
                credential_plaintext=credential,
            )
            capability = write_capability(
                family="LINEAR_DERIVATIVES",
                position_mode="HEDGE",
                account_id="account-1",
                environment="PAPER",
                instrument_version="BTCUSDT@1",
                permission_scope="BYBIT.LINEAR.ORDER.WRITE",
                additional_permission_scopes=("ORDER_WRITE",),
            )
            registry = CapabilityRegistry()
            registry.add(capability)
            intent_id = "intent-e2e"
            client_order_id = stable_client_order_id(
                "BYBIT",
                intent_id,
                environment="PAPER",
                account_id="account-1",
                max_length=36,
                client_id_format="TOKEN",
            )
            prepared = prepare_order_submission(
                capability=capability,
                at=READ_AT,
                provider_environment="TESTNET",
                product_family="LINEAR_DERIVATIVES",
                symbol="BTCUSDT",
                side="BUY",
                order_type="LIMIT",
                quantity="0.001",
                client_order_id=client_order_id,
                time_in_force="GTC",
                price="50000",
                reduce_only=False,
                position_side="LONG",
                position_idx=1,
            )
            wire = _RecordingWire(
                b'{"retCode":0,"retMsg":"OK","result":{"orderId":"provider-1"}}'
            )
            sender = build_production_bybit_order_sender(
                runtime,
                provider_environment="TESTNET",
                capability_snapshot_id=capability.snapshot_id,
                capability_registry=registry,
                credential_handle=self._handle(),
                session_token="session-1",
                clock_millis=lambda: 1_700_000_000_000,
                clock_utc=lambda: READ_AT,
                wire_client=wire,
            )

            # Readiness qualification itself is owned by the reconciliation
            # journal.  This integration test places the controller in the
            # already-qualified state solely to exercise the composed send path.
            runtime.recovery_controller.provider_reconciled = True
            runtime.recovery_controller.reason_codes.clear()
            runtime.recovery_controller.state = HostState.READY
            authority_calls: list[tuple[str, str]] = []

            def authority_check(intent_hash: str, at: str):
                authority_calls.append((intent_hash, at))
                return True, "authorized"

            dispatch_now = READ_AT.isoformat().replace("+00:00", "Z")
            outcome = sender.dispatch(
                attempt_id="attempt-e2e",
                intent_id=intent_id,
                intent_hash="sha256:" + "2" * 64,
                request=guarded_order_projection(prepared),
                now=dispatch_now,
                authority_check=authority_check,
                final_barrier_clock=lambda: dispatch_now,
            )

            self.assertEqual(outcome.status, "SENT")
            self.assertEqual(outcome.reason, "sent_confirmed")
            self.assertEqual(len(wire.requests), 1)
            self.assertEqual(len(boundary.calls), 1)
            self.assertEqual(len(authority_calls), 2)
            events = runtime.journal.load_events(
                "submission_attempt",
                runtime.dispatcher._dispatcher._aggregate_id("attempt-e2e"),
            )
            self.assertEqual(
                [event["event_type"] for event in events],
                ["SubmissionPrepared", "SubmissionSending", "SubmissionSent"],
            )
            self.assertEqual(events[-1]["payload"]["response_encoding"], "utf-8-json")
            self.assertEqual(
                events[-1]["payload"]["response_sha256"],
                outcome.response_sha256 if hasattr(outcome, "response_sha256") else events[-1]["payload"]["response_sha256"],
            )


if __name__ == "__main__":
    unittest.main()
