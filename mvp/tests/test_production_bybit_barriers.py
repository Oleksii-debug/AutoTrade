from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp import production_host
from mvp.autotrade_mvp.bybit_v5 import guarded_order_projection, prepare_order_submission
from mvp.autotrade_mvp.capabilities import CapabilityRegistry
from mvp.autotrade_mvp.dispatch import stable_client_order_id
from mvp.autotrade_mvp.host_network import AuthenticatedHostApplication
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.production_bybit import _build_production_bybit_order_sender
from mvp.autotrade_mvp.production_financial_host import compose_financial_authority
from mvp.autotrade_mvp.production_host import ProductionHostConfig, ProductionHostRuntime
from mvp.autotrade_mvp.security import SecurityBoundary
from mvp.autotrade_mvp.windows_secrets import PersistentCredentialHandle, ProtectedCredentialVault
from mvp.tests.test_bybit_v5 import READ_AT, write_capability
from mvp.tests.test_security import DeterministicProtector


class _FenceStub:
    def __init__(self) -> None:
        self.released = False


class _RecordingWire:
    def __init__(self) -> None:
        self.requests = []

    def send(self, request):
        self.requests.append(request)
        return b'{"retCode":0,"retMsg":"OK","result":{"orderId":"must-not-send"}}'


class ProductionBybitBarrierTests(unittest.TestCase):
    def _runtime(self, root: str, *, security_boundary=None):
        config = ProductionHostConfig(
            journal_path=Path(root) / "financial-host.sqlite",
            account_id="account-1",
            environment="PAPER",
            host_id="host-a",
            bind_host="127.0.0.1",
            bind_port=18765,
            public_origin="http://127.0.0.1:18765",
        )
        boundary = security_boundary or object.__new__(SecurityBoundary)
        application = object.__new__(AuthenticatedHostApplication)
        application.security_boundary = boundary
        host = ProductionHostRuntime(
            config=config,
            journal=JournalStore(config.journal_path),
            application=application,
            server=object(),
            instance_fence=_FenceStub(),
            admission_gate=object(),
            issuance_token=production_host._RUNTIME_ISSUANCE_TOKEN,
        )
        return compose_financial_authority(host), boundary

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

    def _sender_and_request(self, runtime, wire, *, credential_handle=None, session_token="session-1"):
        capability = write_capability(
            family="LINEAR_DERIVATIVES",
            position_mode="HEDGE",
            account_id="account-1",
            environment="PAPER",
            instrument_version="BTCUSDT@1",
            permission_scope="BYBIT.LINEAR.ORDER.WRITE",
            additional_permission_scopes=("ORDER_WRITE",),
            provider_environment="TESTNET",
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
        sender = _build_production_bybit_order_sender(
            runtime,
            provider_environment="TESTNET",
            capability_snapshot_id=capability.snapshot_id,
            capability_registry=registry,
            credential_handle=credential_handle or self._handle(),
            session_token=session_token,
            clock_millis=lambda: 1_700_000_000_000,
            clock_utc=lambda: READ_AT,
            wire_client=wire,
        )
        return intent_id, sender, guarded_order_projection(request)

    def test_recovering_host_may_sign_but_cannot_cross_durable_sender_fence(self) -> None:
        plaintext = json.dumps(
            {"api_key": "api-key-SECRET", "api_secret": "signing-SECRET"},
            sort_keys=True,
            separators=(",", ":"),
        )
        with TemporaryDirectory() as root:
            boundary = SecurityBoundary(
                allowed_origins={"http://127.0.0.1:18765"},
                credential_vault=ProtectedCredentialVault(
                    Path(root) / "credentials.json", protector=DeterministicProtector(),
                ),
                session_authorizer=lambda _subject, _role, _origin: True,
                now=lambda: 1000.0,
            )
            runtime, _boundary = self._runtime(root, security_boundary=boundary)
            session = boundary.create_session(
                subject="test-owner", role="OWNER", origin=runtime.config.public_origin,
            )
            credential_handle = boundary.register_secret(
                session.token, origin=runtime.config.public_origin,
                owner_identity=runtime.financial_dispatcher.owner.owner_id,
                account_id="account-1", provider="BYBIT", environment="PAPER",
                provider_environment="TESTNET", purpose="TRADE", secret_value=plaintext,
            )
            wire = _RecordingWire()
            intent_id, sender, request = self._sender_and_request(
                runtime, wire, credential_handle=credential_handle, session_token=session.token,
            )
            dispatch_now = READ_AT.isoformat().replace("+00:00", "Z")
            authority_calls = []
            outcome = sender.dispatch(
                attempt_id="attempt-recovering", intent_id=intent_id,
                intent_hash="sha256:" + "3" * 64, request=request,
                now=dispatch_now,
                authority_check=lambda intent_hash, at: (
                    authority_calls.append((intent_hash, at)) or (True, "authorized")
                ),
                final_barrier_clock=lambda: dispatch_now,
            )

            self.assertEqual(outcome.status, "BLOCKED")
            self.assertEqual(outcome.reason, "sender_fence_rejected:PermissionError")
            self.assertEqual(wire.requests, [])
            self.assertEqual(len(authority_calls), 1)
            events = runtime.journal.load_events_by_aggregate_type("submission_attempt")
            event_types = [event["event_type"] for event in events]
            self.assertEqual(event_types, ["SubmissionPrepared", "SubmissionBlocked"])
            self.assertNotIn("SubmissionSending", event_types)

    def test_post_composition_domain_mutation_is_zero_wire_and_zero_secret(self) -> None:
        with TemporaryDirectory() as root:
            runtime, _boundary = self._runtime(root)
            wire = _RecordingWire()
            intent_id, sender, request = self._sender_and_request(runtime, wire)
            transport = sender._ProductionBybitOrderSender__transport
            transport.provider_environment = "DEMO"
            lease_calls = []

            @contextmanager
            def fake_lease(_self, token, **kwargs):
                lease_calls.append((token, kwargs))
                yield "must-not-resolve"

            original = SecurityBoundary.lease_for_execution
            SecurityBoundary.lease_for_execution = fake_lease
            try:
                with self.assertRaisesRegex(
                    PermissionError,
                    "provider environment changed after composition",
                ):
                    sender.dispatch(
                        attempt_id="attempt-retargeted",
                        intent_id=intent_id,
                        intent_hash="sha256:" + "4" * 64,
                        request=request,
                        now=READ_AT.isoformat().replace("+00:00", "Z"),
                        authority_check=lambda _intent_hash, _at: (True, "authorized"),
                    )
            finally:
                SecurityBoundary.lease_for_execution = original

            self.assertEqual(lease_calls, [])
            self.assertEqual(wire.requests, [])
            self.assertEqual(
                runtime.journal.load_events_by_aggregate_type("submission_attempt"),
                [],
            )


if __name__ == "__main__":
    unittest.main()
