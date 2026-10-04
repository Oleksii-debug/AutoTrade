from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp import production_bybit, production_host
from mvp.autotrade_mvp.capabilities import CapabilityRegistry
from mvp.autotrade_mvp.host_network import AuthenticatedHostApplication
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.production_bybit import _build_production_bybit_order_sender
from mvp.autotrade_mvp.production_financial_host import compose_financial_authority
from mvp.autotrade_mvp.production_host import ProductionHostConfig, ProductionHostRuntime
from mvp.autotrade_mvp.provider_transport import (
    BYBIT_V5_ENDPOINT_POLICIES,
    BybitV5HttpTransport,
)
from mvp.autotrade_mvp.security import SecurityBoundary
from mvp.autotrade_mvp.windows_secrets import PersistentCredentialHandle


_NOW = datetime(2026, 10, 4, 2, 0, tzinfo=timezone.utc)
_POLICY_FORGED_CALLS: list[object] = []
_CREDENTIAL_FORGED_CALLS: list[object] = []
_TRANSPORT_FORGED_CALLS: list[object] = []


def _forged_policy_identity(policy, *, provider_environment):
    _POLICY_FORGED_CALLS.append((policy, provider_environment))
    return (
        "BYBIT",
        "PAPER",
        "https://api-testnet.bybit.com",
        frozenset({"api-testnet.bybit.com"}),
        15,
    )


def _forged_credential_identity(handle):
    _CREDENTIAL_FORGED_CALLS.append(handle)
    return (
        "cred-bybit-trade",
        "account-1",
        "BYBIT",
        "PAPER",
        "TESTNET",
        "TRADE",
        1,
    )


def _forged_transport_call(self, client_order_id, request, final_guard):
    _TRANSPORT_FORGED_CALLS.append((self, client_order_id, request, final_guard))
    raise AssertionError("forged Bybit transport executable ran")


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
        return _build_production_bybit_order_sender(
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

    def test_policy_mutation_cannot_be_masked_by_permissive_identity_helper(self):
        with TemporaryDirectory() as root:
            runtime = self._runtime(root)
            wire = _RecordingWire()
            sender = self._sender(runtime, wire=wire)
            policy = BYBIT_V5_ENDPOINT_POLICIES["TESTNET"]
            original_base_url = policy.base_url
            original_allowed_hosts = policy.allowed_hosts
            original_reader = production_bybit._bybit_policy_identity
            forged_calls = []

            def forged_reader(_policy, *, provider_environment):
                forged_calls.append(provider_environment)
                return sender._ProductionBybitOrderSender__policy_identity

            try:
                object.__setattr__(policy, "base_url", "https://evil.example")
                object.__setattr__(policy, "allowed_hosts", frozenset({"evil.example"}))
                production_bybit._bybit_policy_identity = forged_reader
                with self.assertRaisesRegex(
                    PermissionError,
                    "policy identity authority changed",
                ):
                    sender._require_send_authority()
            finally:
                production_bybit._bybit_policy_identity = original_reader
                object.__setattr__(policy, "base_url", original_base_url)
                object.__setattr__(policy, "allowed_hosts", original_allowed_hosts)

            self.assertEqual(forged_calls, [])
            self.assertEqual(wire.requests, [])

    def test_policy_identity_same_object_code_mutation_fails_before_execution(self):
        with TemporaryDirectory() as root:
            runtime = self._runtime(root)
            sender = self._sender(runtime)
            canonical = production_bybit._bybit_policy_identity
            original_code = canonical.__code__
            _POLICY_FORGED_CALLS.clear()
            try:
                canonical.__code__ = _forged_policy_identity.__code__
                with self.assertRaisesRegex(
                    PermissionError,
                    "policy identity authority code changed",
                ):
                    sender._require_send_authority()
            finally:
                canonical.__code__ = original_code

            self.assertEqual(_POLICY_FORGED_CALLS, [])

    def test_policy_registry_global_rebinding_is_rejected(self):
        with TemporaryDirectory() as root:
            runtime = self._runtime(root)
            sender = self._sender(runtime)
            original_registry = production_bybit.BYBIT_V5_ENDPOINT_POLICIES
            replacement_registry = dict(original_registry)
            production_bybit.BYBIT_V5_ENDPOINT_POLICIES = replacement_registry
            try:
                with self.assertRaisesRegex(
                    PermissionError,
                    "policy registry authority changed",
                ):
                    sender._require_send_authority()
            finally:
                production_bybit.BYBIT_V5_ENDPOINT_POLICIES = original_registry

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

    def test_credential_mutation_cannot_be_masked_by_permissive_identity_helper(self):
        with TemporaryDirectory() as root:
            runtime = self._runtime(root)
            handle = self._handle()
            sender = self._sender(runtime, handle=handle)
            original_reader = production_bybit._credential_identity
            forged_calls = []

            def forged_reader(_handle):
                forged_calls.append("called")
                return sender._ProductionBybitOrderSender__credential_identity

            object.__setattr__(handle, "handle_id", "other-credential")
            production_bybit._credential_identity = forged_reader
            try:
                with self.assertRaisesRegex(
                    PermissionError,
                    "credential identity authority changed",
                ):
                    sender._require_send_authority()
            finally:
                production_bybit._credential_identity = original_reader

            self.assertEqual(forged_calls, [])

    def test_credential_identity_same_object_code_mutation_fails_before_execution(self):
        with TemporaryDirectory() as root:
            runtime = self._runtime(root)
            sender = self._sender(runtime)
            canonical = production_bybit._credential_identity
            original_code = canonical.__code__
            _CREDENTIAL_FORGED_CALLS.clear()
            try:
                canonical.__code__ = _forged_credential_identity.__code__
                with self.assertRaisesRegex(
                    PermissionError,
                    "credential identity authority code changed",
                ):
                    sender._require_send_authority()
            finally:
                canonical.__code__ = original_code

            self.assertEqual(_CREDENTIAL_FORGED_CALLS, [])

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

    def test_resolver_credential_mutation_cannot_be_masked_by_helper_rebind(self):
        with TemporaryDirectory() as root:
            runtime = self._runtime(root)
            handle = self._handle()
            sender = self._sender(runtime, handle=handle)
            transport = sender._ProductionBybitOrderSender__transport
            resolver = transport.secret_resolver
            original_reader = production_bybit._credential_identity
            forged_calls = []

            def forged_reader(_handle):
                forged_calls.append("called")
                return resolver._ProductionBybitSecretResolver__credential_identity

            object.__setattr__(handle, "provider_environment", "DEMO")
            production_bybit._credential_identity = forged_reader
            try:
                with self.assertRaisesRegex(
                    PermissionError,
                    "credential identity authority changed",
                ):
                    resolver._require_runtime_authority()
            finally:
                production_bybit._credential_identity = original_reader

            self.assertEqual(forged_calls, [])

    def test_transport_class_rebinding_fails_before_financial_callbacks_or_wire(self):
        with TemporaryDirectory() as root:
            runtime = self._runtime(root)
            wire = _RecordingWire()
            sender = self._sender(runtime, wire=wire)
            original_call = BybitV5HttpTransport.__call__
            forged_calls = []

            def forged_call(*args, **kwargs):
                forged_calls.append((args, kwargs))
                raise AssertionError("forged transport executable ran")

            BybitV5HttpTransport.__call__ = forged_call
            try:
                with self.assertRaisesRegex(
                    PermissionError,
                    "transport executable authority changed",
                ):
                    sender.dispatch(
                        attempt_id="attempt-transport-rebind",
                        intent_id="intent-transport-rebind",
                        intent_hash="sha256:" + "3" * 64,
                        request={"symbol": "BTCUSDT"},
                        now="2026-10-04T02:00:02Z",
                        authority_check=lambda *_args: self.fail(
                            "authority callback ran after transport executable retarget"
                        ),
                    )
            finally:
                BybitV5HttpTransport.__call__ = original_call

            self.assertEqual(forged_calls, [])
            self.assertEqual(wire.requests, [])

    def test_transport_same_object_code_mutation_fails_before_execution(self):
        with TemporaryDirectory() as root:
            runtime = self._runtime(root)
            wire = _RecordingWire()
            sender = self._sender(runtime, wire=wire)
            canonical = BybitV5HttpTransport.__call__
            original_code = canonical.__code__
            _TRANSPORT_FORGED_CALLS.clear()
            try:
                canonical.__code__ = _forged_transport_call.__code__
                with self.assertRaisesRegex(
                    PermissionError,
                    "transport executable authority code changed",
                ):
                    sender.dispatch(
                        attempt_id="attempt-transport-code",
                        intent_id="intent-transport-code",
                        intent_hash="sha256:" + "4" * 64,
                        request={"symbol": "BTCUSDT"},
                        now="2026-10-04T02:00:03Z",
                        authority_check=lambda *_args: self.fail(
                            "authority callback ran after transport code mutation"
                        ),
                    )
            finally:
                canonical.__code__ = original_code

            self.assertEqual(_TRANSPORT_FORGED_CALLS, [])
            self.assertEqual(wire.requests, [])

    def test_security_boundary_lease_rebinding_is_rejected_before_secret_access(self):
        with TemporaryDirectory() as root:
            runtime = self._runtime(root)
            sender = self._sender(runtime)
            transport = sender._ProductionBybitOrderSender__transport
            resolver = transport.secret_resolver
            original_lease = SecurityBoundary.lease_for_execution
            forged_calls = []

            def forged_lease(*args, **kwargs):
                forged_calls.append((args, kwargs))
                raise AssertionError("forged credential lease ran")

            SecurityBoundary.lease_for_execution = forged_lease
            try:
                with self.assertRaisesRegex(
                    PermissionError,
                    "credential lease authority changed",
                ):
                    resolver._require_runtime_authority()
            finally:
                SecurityBoundary.lease_for_execution = original_lease

            self.assertEqual(forged_calls, [])


if __name__ == "__main__":
    unittest.main()