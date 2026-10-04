from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from mvp.autotrade_mvp.bybit_v5 import guarded_order_projection, prepare_order_submission
from mvp.autotrade_mvp.capabilities import CapabilityRegistry
from mvp.autotrade_mvp.dispatch import stable_client_order_id
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.production_bybit import build_production_bybit_order_sender
from mvp.autotrade_mvp.production_financial_host import build_production_financial_host
from mvp.autotrade_mvp.production_host import ProductionHostConfig, ProductionHostRuntime
from mvp.autotrade_mvp.windows_secrets import PersistentCredentialHandle
from mvp.tests.test_bybit_v5 import READ_AT, write_capability


class _LeaseBoundary:
    def __init__(self) -> None:
        self.calls = []
        self.plaintext = json.dumps(
            {"api_key": "api-key-SECRET", "api_secret": "signing-SECRET"},
            sort_keys=True,
            separators=(",", ":"),
        )

    @contextmanager
    def lease_for_execution(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        yield self.plaintext


class _RecordingWire:
    def __init__(self) -> None:
        self.requests = []

    def send(self, request):
        self.requests.append(request)
        return b'{"retCode":0,"retMsg":"OK","result":{"orderId":"must-not-send"}}'


class ProductionBybitBarrierTests(unittest.TestCase):
    def _runtime(self, root: str):
        config = ProductionHostConfig(
            journal_path=Path(root) / "financial-host.sqlite",
            account_id="account-1",
            environment="PAPER",
            host_id="host-a",
            bind_host="127.0.0.1",
            bind_port=18765,
            public_origin="http://127.0.0.1:18765",
        )
        host = ProductionHostRuntime(
            config=config,
            journal=JournalStore(config.journal_path),
            application=Mock(),
            server=Mock(),
            instance_fence=Mock(),
            admission_gate=Mock(),
        )
        boundary = _LeaseBoundary()
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
    def _handle() -> PersistentCredentialHandle:
        return PersistentCredentialHandle(
            handle_id="cred-bybit-trade",
            account_id="account-1",
            provider="BYBIT",
            environment="PAPER",
            provider_environment="TESTNET",
            purpose="TRADE",
            generation=1,
        )

    def _sender_and_request(self, runtime, wire):
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
        intent_id = "intent-barrier"
        client_order_id = stable_client_order_id(
            "BYBIT",
            intent_id,
            environment="PAPER",
            account_id="account-1",
            max_length=36,
            client_id_format="TOKEN",
        )
        request = prepare_order_submission(
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
        return intent_id, sender, guarded_order_projection(request)

    def test_recovering_host_can_resolve_secret_but_never_cross_sender_fence(self) -> None:
        with TemporaryDirectory() as root:
            runtime, boundary = self._runtime(root)
            wire = _RecordingWire()
            intent_id, sender, request = self._sender_and_request(runtime, wire)
            dispatch_now = READ_AT.isoformat().replace("+00:00", "Z")
            authority_calls = []

            outcome = sender.dispatch(
                attempt_id="attempt-recovering",
                intent_id=intent_id,
                intent_hash="sha256:" + "3" * 64,
                request=request,
                now=dispatch_now,
                authority_check=lambda intent_hash, at: (
                    authority_calls.append((intent_hash, at)) or (True, "authorized")
                ),
                final_barrier_clock=lambda: dispatch_now,
            )

            self.assertEqual(outcome.status, "BLOCKED")
            self.assertEqual(outcome.reason, "sender_fence_rejected:PermissionError")
            self.assertEqual(len(boundary.calls), 1)
            self.assertEqual(len(wire.requests), 0)
            self.assertEqual(len(authority_calls), 1)
            events = runtime.journal.load_events(
                "submission_attempt",
                runtime.dispatcher._dispatcher._aggregate_id("attempt-recovering"),
            )
            self.assertEqual(
                [event["event_type"] for event in events],
                ["SubmissionPrepared", "SubmissionBlocked"],
            )
            self.assertNotIn(
                "SubmissionSending",
                [event["event_type"] for event in events],
            )

    def test_closed_dispatch_admission_blocks_before_secret_or_wire(self) -> None:
        with TemporaryDirectory() as root:
            runtime, boundary = self._runtime(root)
            wire = _RecordingWire()
            intent_id, sender, request = self._sender_and_request(runtime, wire)
            runtime.dispatcher.stop_and_drain()
            dispatch_now = READ_AT.isoformat().replace("+00:00", "Z")

            with self.assertRaisesRegex(
                PermissionError,
                "provider dispatch is closed",
            ):
                sender.dispatch(
                    attempt_id="attempt-after-dispatch-close",
                    intent_id=intent_id,
                    intent_hash="sha256:" + "4" * 64,
                    request=request,
                    now=dispatch_now,
                    authority_check=lambda _intent_hash, _at: (True, "authorized"),
                    final_barrier_clock=lambda: dispatch_now,
                )

            self.assertEqual(boundary.calls, [])
            self.assertEqual(wire.requests, [])
            self.assertEqual(
                runtime.journal.load_events(
                    "submission_attempt",
                    runtime.dispatcher._dispatcher._aggregate_id(
                        "attempt-after-dispatch-close"
                    ),
                ),
                [],
            )


if __name__ == "__main__":
    unittest.main()
