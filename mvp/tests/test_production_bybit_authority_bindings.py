from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp import production_host
from mvp.autotrade_mvp.capabilities import CapabilityRegistry
from mvp.autotrade_mvp.host_network import AuthenticatedHostApplication
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.production_bybit import build_production_bybit_order_sender
from mvp.autotrade_mvp.production_financial_host import compose_financial_authority
from mvp.autotrade_mvp.production_host import ProductionHostConfig, ProductionHostRuntime
from mvp.autotrade_mvp.provider_transport import BYBIT_V5_ENDPOINT_POLICIES
from mvp.autotrade_mvp.security import SecurityBoundary
from mvp.autotrade_mvp.windows_secrets import PersistentCredentialHandle


_NOW = datetime(2026, 10, 4, 2, 0, tzinfo=timezone.utc)


class _FenceStub:
    def __init__(self) -> None:
        self.released = False


class _RecordingWire:
    def __init__(self) -> None:
        self.requests = []

    def send(self, request):
        self.requests.append(request)
        return b'{"retCode":0,"retMsg":"OK","result":{}}'


class ProductionBybitAuthorityBindingTests(unittest.TestCase):
    def _runtime(self, root: str):
        journal = JournalStore(Path(root) / "financial-host.sqlite")
        config = ProductionHostConfig(
            journal_path=journal.path,
            account_id="account-1",
            environment="PAPER",
            host_id="host-a",
            bind_host="127.0.0.1",
            bind_port=18766,
            public_origin="http://127.0.0.1:18766",
        )
        boundary = object.__new__(SecurityBoundary)
        application = object.__new__(AuthenticatedHostApplication)
        application.security_boundary = boundary
        host = ProductionHostRuntime(
            config=config,
            journal=journal,
            application=application,
            server=object(),
            instance_fence=_FenceStub(),
            admission_gate=object(),
            issuance_token=production_host._RUNTIME_ISSUANCE_TOKEN,
        )
        return compose_financial_authority(host)

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

    def _sender(self, runtime, *, handle=None, wire=None):
        return build_production_bybit_order_sender(
            runtime,
            provider_environment="TESTNET",
            capability_snapshot_id="capability-1",
            capability_registry=CapabilityRegistry(),
            credential_handle=handle or self._handle(),
            session_token="session-1",
            clock_millis=lambda: 1_700_000_000_000,
            clock_utc=lambda: _NOW,
            wire_client=wire,
        )

    def test_same_canonical_policy_object_cannot_be_retargeted_to_another_https_host(self):
        with TemporaryDirectory() as root:
            runtime = self._runtime(root)
            wire = _RecordingWire()
            sender = self._sender(runtime, wire=wire)
            policy = BYBIT_V5_ENDPOINT_POLICIES["TESTNET"]
            original_base_url = policy.base_url
            original_allowed_hosts = policy.allowed_hosts
            try:
                object.__setattr__(policy, "base_url", "https://evil.example")
                object.__setattr__(policy, "allowed_hosts", frozenset({"evil.example"}))
                with self.assertRaisesRegex(
                    PermissionError,
                    "provider policy values changed",
                ):
                    sender.dispatch(
                        attempt_id="attempt-policy-retarget",
                        intent_id="intent-policy-retarget",
                        intent_hash="sha256:" + "1" * 64,
                        request={"symbol": "BTCUSDT"},
                        now="2026-10-04T02:00:00Z",
                        authority_check=lambda *_args: (True, "allowed"),
                    )
            finally:
                object.__setattr__(policy, "base_url", original_base_url)
                object.__setattr__(policy, "allowed_hosts", original_allowed_hosts)

            self.assertEqual(wire.requests, [])

    def test_policy_retarget_before_sender_construction_is_rejected(self):
        with TemporaryDirectory() as root:
            runtime = self._runtime(root)
            policy = BYBIT_V5_ENDPOINT_POLICIES["TESTNET"]
            original_base_url = policy.base_url
            original_allowed_hosts = policy.allowed_hosts
            try:
                object.__setattr__(policy, "base_url", "https://evil.example")
                object.__setattr__(policy, "allowed_hosts", frozenset({"evil.example"}))
                with self.assertRaisesRegex(
                    PermissionError,
                    "provider policy values changed",
                ):
                    self._sender(runtime)
            finally:
                object.__setattr__(policy, "base_url", original_base_url)
                object.__setattr__(policy, "allowed_hosts", original_allowed_hosts)

    def test_credential_handle_id_mutation_fails_before_financial_callbacks(self):
        with TemporaryDirectory() as root:
            runtime = self._runtime(root)
            handle = self._handle()
            wire = _RecordingWire()
            sender = self._sender(runtime, handle=handle, wire=wire)
            object.__setattr__(handle, "handle_id", "other-credential")

            with self.assertRaisesRegex(
                PermissionError,
                "credential handle changed after composition",
            ):
                sender.dispatch(
                    attempt_id="attempt-credential-retarget",
                    intent_id="intent-credential-retarget",
                    intent_hash="sha256:" + "2" * 64,
                    request={"symbol": "BTCUSDT"},
                    now="2026-10-04T02:00:01Z",
                    authority_check=lambda *_args: self.fail(
                        "authority callback ran after credential retarget"
                    ),
                )

            self.assertEqual(wire.requests, [])

    def test_credential_generation_boolean_is_not_generation_one(self):
        with TemporaryDirectory() as root:
            runtime = self._runtime(root)
            handle = self._handle()
            sender = self._sender(runtime, handle=handle)
            object.__setattr__(handle, "generation", True)

            with self.assertRaisesRegex(
                PermissionError,
                "credential generation authority changed",
            ):
                sender._require_send_authority()

    def test_credential_provider_domain_mutation_is_rejected_by_resolver_too(self):
        with TemporaryDirectory() as root:
            runtime = self._runtime(root)
            handle = self._handle()
            sender = self._sender(runtime, handle=handle)
            transport = sender._ProductionBybitOrderSender__transport
            resolver = transport.secret_resolver
            object.__setattr__(handle, "provider_environment", "DEMO")

            with self.assertRaisesRegex(
                PermissionError,
                "credential handle changed after composition",
            ):
                resolver._require_runtime_authority()


if __name__ == "__main__":
    unittest.main()
