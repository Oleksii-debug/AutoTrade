from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp import production_host, provider_transport
from mvp.autotrade_mvp.bybit_v5 import guarded_order_projection, prepare_order_submission
from mvp.autotrade_mvp.capabilities import CapabilityRegistry
from mvp.autotrade_mvp.dispatch import stable_client_order_id
from mvp.autotrade_mvp.host_network import AuthenticatedHostApplication
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.production_bybit import build_production_bybit_order_sender
from mvp.autotrade_mvp.production_financial_host import compose_financial_authority
from mvp.autotrade_mvp.production_host import ProductionHostConfig, ProductionHostRuntime
from mvp.autotrade_mvp.provider_transport import (
    BYBIT_V5_ENDPOINT_POLICIES,
    ProviderEndpointPolicy,
)
from mvp.autotrade_mvp.security import SecurityBoundary
from mvp.autotrade_mvp.windows_secrets import PersistentCredentialHandle
from mvp.tests.test_bybit_v5 import READ_AT, write_capability


class _FenceStub:
    def __init__(self) -> None:
        self.released = False


class _RecordingWire:
    def __init__(self) -> None:
        self.requests = []

    def send(self, request):
        self.requests.append(request)
        return b'{"retCode":0,"retMsg":"OK","result":{"orderId":"must-not-send"}}'


class _HostileTimestamp(int):
    comparison_calls = 0

    def __lt__(self, _other):
        type(self).comparison_calls += 1
        raise AssertionError("hostile timestamp comparison executed")

    def __str__(self):
        raise AssertionError("hostile timestamp string conversion executed")


_FORGED_SIGNER_CALLS = []
_FORGED_POLICY_URL_CALLS = []


def _forged_bybit_sign(*args, **kwargs):
    _FORGED_SIGNER_CALLS.append((args, kwargs))
    raise AssertionError("forged Bybit signer executed")


def _forged_policy_absolute_url(self, endpoint):
    _FORGED_POLICY_URL_CALLS.append((self, endpoint))
    raise AssertionError("forged policy URL authority executed")


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
        boundary = object.__new__(SecurityBoundary)
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

    def _sender_and_request(
        self,
        runtime,
        wire,
        *,
        credential_handle=None,
        quota_gate=None,
        clock_millis=None,
        clock_utc=None,
        recv_window_ms=5000,
    ):
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
            credential_handle=credential_handle or self._handle(),
            session_token="session-1",
            clock_millis=clock_millis or (lambda: 1_700_000_000_000),
            clock_utc=clock_utc or (lambda: READ_AT),
            quota_gate=quota_gate,
            wire_client=wire,
            recv_window_ms=recv_window_ms,
        )
        return intent_id, sender, guarded_order_projection(request)

    def test_callback_time_policy_retarget_is_rejected_before_signing(self) -> None:
        plaintext = json.dumps(
            {"api_key": "api-key-SECRET", "api_secret": "signing-SECRET"},
            sort_keys=True,
            separators=(",", ":"),
        )
        with TemporaryDirectory() as root:
            runtime, _boundary = self._runtime(root)
            wire = _RecordingWire()
            policy = BYBIT_V5_ENDPOINT_POLICIES["TESTNET"]
            original_base_url = policy.base_url
            original_allowed_hosts = policy.allowed_hosts
            clock_utc_calls = []
            lease_calls = []
            final_guard_calls = []

            def quota_gate(*_args):
                object.__setattr__(policy, "base_url", "https://evil.example")
                object.__setattr__(
                    policy,
                    "allowed_hosts",
                    frozenset({"evil.example"}),
                )

            def clock_utc():
                clock_utc_calls.append("called")
                if len(clock_utc_calls) >= 2:
                    object.__setattr__(policy, "base_url", original_base_url)
                    object.__setattr__(
                        policy,
                        "allowed_hosts",
                        original_allowed_hosts,
                    )
                return READ_AT

            @contextmanager
            def fake_lease(_self, token, **kwargs):
                lease_calls.append((token, kwargs))
                yield plaintext

            original_lease = SecurityBoundary.lease_for_execution
            SecurityBoundary.lease_for_execution = fake_lease
            try:
                intent_id, sender, request = self._sender_and_request(
                    runtime,
                    wire,
                    quota_gate=quota_gate,
                    clock_utc=clock_utc,
                )
                client_order_id = stable_client_order_id(
                    "BYBIT",
                    intent_id,
                    environment="PAPER",
                    account_id="account-1",
                    max_length=36,
                    client_id_format="TOKEN",
                )
                with self.assertRaisesRegex(
                    PermissionError,
                    "provider policy values changed",
                ):
                    sender._transport_send(
                        client_order_id,
                        request,
                        lambda: final_guard_calls.append("called"),
                    )
            finally:
                SecurityBoundary.lease_for_execution = original_lease
                object.__setattr__(policy, "base_url", original_base_url)
                object.__setattr__(
                    policy,
                    "allowed_hosts",
                    original_allowed_hosts,
                )

            self.assertEqual(len(lease_calls), 1)
            self.assertEqual(final_guard_calls, [])
            self.assertEqual(wire.requests, [])

    def test_callback_time_policy_object_replacement_is_rejected_before_signing(self) -> None:
        plaintext = json.dumps(
            {"api_key": "api-key-SECRET", "api_secret": "signing-SECRET"},
            sort_keys=True,
            separators=(",", ":"),
        )
        with TemporaryDirectory() as root:
            runtime, _boundary = self._runtime(root)
            wire = _RecordingWire()
            replacement = ProviderEndpointPolicy(
                provider_id="BYBIT",
                environment="PAPER",
                base_url="https://evil.example",
                allowed_hosts=frozenset({"evil.example"}),
            )
            transport_holder = {}

            def quota_gate(*_args):
                transport_holder["transport"].policy = replacement

            canonical_policy = BYBIT_V5_ENDPOINT_POLICIES["TESTNET"]
            transport = None

            @contextmanager
            def fake_lease(_self, _token, **_kwargs):
                yield plaintext

            original_lease = SecurityBoundary.lease_for_execution
            SecurityBoundary.lease_for_execution = fake_lease
            try:
                intent_id, sender, request = self._sender_and_request(
                    runtime,
                    wire,
                    quota_gate=quota_gate,
                )
                transport = sender._ProductionBybitOrderSender__transport
                transport_holder["transport"] = transport
                client_order_id = stable_client_order_id(
                    "BYBIT",
                    intent_id,
                    environment="PAPER",
                    account_id="account-1",
                    max_length=36,
                    client_id_format="TOKEN",
                )
                with self.assertRaisesRegex(
                    PermissionError,
                    "transport policy authority changed before signing",
                ):
                    sender._transport_send(
                        client_order_id,
                        request,
                        lambda: self.fail("final guard ran after policy replacement"),
                    )
            finally:
                SecurityBoundary.lease_for_execution = original_lease
                if transport is not None:
                    transport.policy = canonical_policy

            self.assertEqual(wire.requests, [])

    def test_callback_time_recv_window_mutation_is_rejected_before_signing(self) -> None:
        plaintext = json.dumps(
            {"api_key": "api-key-SECRET", "api_secret": "signing-SECRET"},
            sort_keys=True,
            separators=(",", ":"),
        )
        with TemporaryDirectory() as root:
            runtime, _boundary = self._runtime(root)
            wire = _RecordingWire()
            transport_holder = {}

            def quota_gate(*_args):
                transport_holder["transport"].recv_window_ms = 6000

            transport = None

            @contextmanager
            def fake_lease(_self, _token, **_kwargs):
                yield plaintext

            original_lease = SecurityBoundary.lease_for_execution
            SecurityBoundary.lease_for_execution = fake_lease
            try:
                intent_id, sender, request = self._sender_and_request(
                    runtime,
                    wire,
                    quota_gate=quota_gate,
                    recv_window_ms=5000,
                )
                transport = sender._ProductionBybitOrderSender__transport
                transport_holder["transport"] = transport
                client_order_id = stable_client_order_id(
                    "BYBIT",
                    intent_id,
                    environment="PAPER",
                    account_id="account-1",
                    max_length=36,
                    client_id_format="TOKEN",
                )
                with self.assertRaisesRegex(
                    PermissionError,
                    "receive-window authority changed before signing",
                ):
                    sender._transport_send(
                        client_order_id,
                        request,
                        lambda: self.fail("final guard ran after recv-window mutation"),
                    )
            finally:
                SecurityBoundary.lease_for_execution = original_lease
                if transport is not None:
                    transport.recv_window_ms = 5000

            self.assertEqual(wire.requests, [])

    def test_hostile_timestamp_subclass_is_rejected_without_callbacks(self) -> None:
        plaintext = json.dumps(
            {"api_key": "api-key-SECRET", "api_secret": "signing-SECRET"},
            sort_keys=True,
            separators=(",", ":"),
        )
        with TemporaryDirectory() as root:
            runtime, _boundary = self._runtime(root)
            wire = _RecordingWire()
            _HostileTimestamp.comparison_calls = 0

            @contextmanager
            def fake_lease(_self, _token, **_kwargs):
                yield plaintext

            original_lease = SecurityBoundary.lease_for_execution
            SecurityBoundary.lease_for_execution = fake_lease
            try:
                intent_id, sender, request = self._sender_and_request(
                    runtime,
                    wire,
                    clock_millis=lambda: _HostileTimestamp(1_700_000_000_000),
                )
                client_order_id = stable_client_order_id(
                    "BYBIT",
                    intent_id,
                    environment="PAPER",
                    account_id="account-1",
                    max_length=36,
                    client_id_format="TOKEN",
                )
                with self.assertRaisesRegex(
                    ValueError,
                    "clock_millis must return an exact non-negative integer",
                ):
                    sender._transport_send(
                        client_order_id,
                        request,
                        lambda: self.fail("final guard ran after hostile timestamp"),
                    )
            finally:
                SecurityBoundary.lease_for_execution = original_lease

            self.assertEqual(_HostileTimestamp.comparison_calls, 0)
            self.assertEqual(wire.requests, [])

    def test_callback_time_signer_rebind_is_rejected_before_execution(self) -> None:
        plaintext = json.dumps(
            {"api_key": "api-key-SECRET", "api_secret": "signing-SECRET"},
            sort_keys=True,
            separators=(",", ":"),
        )
        with TemporaryDirectory() as root:
            runtime, _boundary = self._runtime(root)
            wire = _RecordingWire()
            original_signer = provider_transport.BybitV5Signer.sign
            _FORGED_SIGNER_CALLS.clear()

            def quota_gate(*_args):
                provider_transport.BybitV5Signer.sign = staticmethod(_forged_bybit_sign)

            @contextmanager
            def fake_lease(_self, _token, **_kwargs):
                yield plaintext

            original_lease = SecurityBoundary.lease_for_execution
            SecurityBoundary.lease_for_execution = fake_lease
            try:
                intent_id, sender, request = self._sender_and_request(
                    runtime,
                    wire,
                    quota_gate=quota_gate,
                )
                client_order_id = stable_client_order_id(
                    "BYBIT",
                    intent_id,
                    environment="PAPER",
                    account_id="account-1",
                    max_length=36,
                    client_id_format="TOKEN",
                )
                with self.assertRaisesRegex(
                    PermissionError,
                    "signer authority changed before signing",
                ):
                    sender._transport_send(
                        client_order_id,
                        request,
                        lambda: self.fail("final guard ran after signer rebind"),
                    )
            finally:
                SecurityBoundary.lease_for_execution = original_lease
                provider_transport.BybitV5Signer.sign = staticmethod(original_signer)

            self.assertEqual(_FORGED_SIGNER_CALLS, [])
            self.assertEqual(wire.requests, [])

    def test_callback_time_signer_code_mutation_is_rejected_before_execution(self) -> None:
        plaintext = json.dumps(
            {"api_key": "api-key-SECRET", "api_secret": "signing-SECRET"},
            sort_keys=True,
            separators=(",", ":"),
        )
        with TemporaryDirectory() as root:
            runtime, _boundary = self._runtime(root)
            wire = _RecordingWire()
            signer = provider_transport.BybitV5Signer.sign
            original_code = signer.__code__
            _FORGED_SIGNER_CALLS.clear()

            def quota_gate(*_args):
                signer.__code__ = _forged_bybit_sign.__code__

            @contextmanager
            def fake_lease(_self, _token, **_kwargs):
                yield plaintext

            original_lease = SecurityBoundary.lease_for_execution
            SecurityBoundary.lease_for_execution = fake_lease
            try:
                intent_id, sender, request = self._sender_and_request(
                    runtime,
                    wire,
                    quota_gate=quota_gate,
                )
                client_order_id = stable_client_order_id(
                    "BYBIT",
                    intent_id,
                    environment="PAPER",
                    account_id="account-1",
                    max_length=36,
                    client_id_format="TOKEN",
                )
                with self.assertRaisesRegex(
                    PermissionError,
                    "signer authority code changed before signing",
                ):
                    sender._transport_send(
                        client_order_id,
                        request,
                        lambda: self.fail("final guard ran after signer code mutation"),
                    )
            finally:
                signer.__code__ = original_code
                SecurityBoundary.lease_for_execution = original_lease

            self.assertEqual(_FORGED_SIGNER_CALLS, [])
            self.assertEqual(wire.requests, [])

    def test_callback_time_policy_url_rebind_is_rejected_before_execution(self) -> None:
        plaintext = json.dumps(
            {"api_key": "api-key-SECRET", "api_secret": "signing-SECRET"},
            sort_keys=True,
            separators=(",", ":"),
        )
        with TemporaryDirectory() as root:
            runtime, _boundary = self._runtime(root)
            wire = _RecordingWire()
            original_url_reader = ProviderEndpointPolicy.absolute_url
            _FORGED_POLICY_URL_CALLS.clear()

            def quota_gate(*_args):
                ProviderEndpointPolicy.absolute_url = _forged_policy_absolute_url

            @contextmanager
            def fake_lease(_self, _token, **_kwargs):
                yield plaintext

            original_lease = SecurityBoundary.lease_for_execution
            SecurityBoundary.lease_for_execution = fake_lease
            try:
                intent_id, sender, request = self._sender_and_request(
                    runtime,
                    wire,
                    quota_gate=quota_gate,
                )
                client_order_id = stable_client_order_id(
                    "BYBIT",
                    intent_id,
                    environment="PAPER",
                    account_id="account-1",
                    max_length=36,
                    client_id_format="TOKEN",
                )
                with self.assertRaisesRegex(
                    PermissionError,
                    "policy URL authority changed before signing",
                ):
                    sender._transport_send(
                        client_order_id,
                        request,
                        lambda: self.fail("final guard ran after URL authority rebind"),
                    )
            finally:
                ProviderEndpointPolicy.absolute_url = original_url_reader
                SecurityBoundary.lease_for_execution = original_lease

            self.assertEqual(_FORGED_POLICY_URL_CALLS, [])
            self.assertEqual(wire.requests, [])

    def test_post_sign_callback_authority_mutation_is_rechecked_before_wire(self) -> None:
        plaintext = json.dumps(
            {"api_key": "api-key-SECRET", "api_secret": "signing-SECRET"},
            sort_keys=True,
            separators=(",", ":"),
        )
        with TemporaryDirectory() as root:
            runtime, _boundary = self._runtime(root)
            wire = _RecordingWire()
            handle = self._handle()
            clock_utc_calls = []
            lease_calls = []
            final_guard_calls = []

            def clock_utc():
                clock_utc_calls.append("called")
                if len(clock_utc_calls) == 2:
                    object.__setattr__(handle, "generation", 2)
                return READ_AT

            @contextmanager
            def fake_lease(_self, token, **kwargs):
                lease_calls.append((token, kwargs))
                yield plaintext

            original_lease = SecurityBoundary.lease_for_execution
            SecurityBoundary.lease_for_execution = fake_lease
            try:
                intent_id, sender, request = self._sender_and_request(
                    runtime,
                    wire,
                    credential_handle=handle,
                    clock_utc=clock_utc,
                )
                client_order_id = stable_client_order_id(
                    "BYBIT",
                    intent_id,
                    environment="PAPER",
                    account_id="account-1",
                    max_length=36,
                    client_id_format="TOKEN",
                )
                with self.assertRaisesRegex(
                    PermissionError,
                    "credential handle changed after composition",
                ):
                    sender._transport_send(
                        client_order_id,
                        request,
                        lambda: final_guard_calls.append("called"),
                    )
            finally:
                SecurityBoundary.lease_for_execution = original_lease

            self.assertEqual(len(lease_calls), 1)
            self.assertEqual(final_guard_calls, ["called"])
            self.assertEqual(wire.requests, [])

    def test_recovering_host_may_sign_but_cannot_cross_durable_sender_fence(self) -> None:
        plaintext = json.dumps(
            {"api_key": "api-key-SECRET", "api_secret": "signing-SECRET"},
            sort_keys=True,
            separators=(",", ":"),
        )
        with TemporaryDirectory() as root:
            runtime, _boundary = self._runtime(root)
            wire = _RecordingWire()
            lease_calls = []

            @contextmanager
            def fake_lease(_self, token, **kwargs):
                lease_calls.append((token, kwargs))
                yield plaintext

            original = SecurityBoundary.lease_for_execution
            SecurityBoundary.lease_for_execution = fake_lease
            try:
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
                        authority_calls.append((intent_hash, at))
                        or (True, "authorized")
                    ),
                    final_barrier_clock=lambda: dispatch_now,
                )
            finally:
                SecurityBoundary.lease_for_execution = original

            self.assertEqual(outcome.status, "BLOCKED")
            self.assertEqual(outcome.reason, "sender_fence_rejected:PermissionError")
            self.assertEqual(len(lease_calls), 1)
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
