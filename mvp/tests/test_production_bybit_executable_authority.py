from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp import production_host
from mvp.autotrade_mvp.capabilities import CapabilityRegistry
from mvp.autotrade_mvp.host_network import AuthenticatedHostApplication
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.production_bybit import _build_production_bybit_order_sender
from mvp.autotrade_mvp.production_financial_host import compose_financial_authority
from mvp.autotrade_mvp.production_host import ProductionHostConfig, ProductionHostRuntime
from mvp.autotrade_mvp.provider_transport import BybitV5HttpTransport
from mvp.autotrade_mvp.security import SecurityBoundary
from mvp.autotrade_mvp.windows_secrets import PersistentCredentialHandle


_NOW = datetime(2026, 10, 4, 2, 0, tzinfo=timezone.utc)
_TRANSPORT_CODE_CALLS: list[object] = []
_LEASE_CODE_CALLS: list[object] = []
_WIRE_CODE_CALLS: list[object] = []


def _forged_transport_call(self, client_order_id, request, final_guard):
    _TRANSPORT_CODE_CALLS.append((self, client_order_id, request, final_guard))
    raise AssertionError("forged transport executable ran")


def _forged_wire_send(self, request):
    _WIRE_CODE_CALLS.append((self, request))
    raise AssertionError("forged wire executable ran")


def _forged_lease_generator(
    self,
    token,
    *,
    origin,
    handle,
    execution_identity,
    account_id,
    provider,
    environment,
    purpose,
    provider_environment=None,
):
    _LEASE_CODE_CALLS.append(
        (
            self,
            token,
            origin,
            handle,
            execution_identity,
            account_id,
            provider,
            environment,
            purpose,
            provider_environment,
        )
    )
    yield "forged-secret"


class _FenceStub:
    def __init__(self) -> None:
        self.released = False


class _RecordingWire:
    def __init__(self) -> None:
        self.requests = []

    def send(self, request):
        self.requests.append(request)
        return b'{"retCode":0,"retMsg":"OK","result":{}}'


class ProductionBybitExecutableAuthorityTests(unittest.TestCase):
    def _runtime(self, root: str):
        journal = JournalStore(Path(root) / "financial-host.sqlite")
        config = ProductionHostConfig(
            journal_path=journal.path,
            account_id="account-1",
            environment="PAPER",
            host_id="host-a",
            bind_host="127.0.0.1",
            bind_port=18767,
            public_origin="http://127.0.0.1:18767",
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

    def _sender(self, runtime, *, wire=None):
        return _build_production_bybit_order_sender(
            runtime,
            provider_environment="TESTNET",
            capability_snapshot_id="capability-1",
            capability_registry=CapabilityRegistry(),
            credential_handle=self._handle(),
            session_token="session-1",
            clock_millis=lambda: 1_700_000_000_000,
            clock_utc=lambda: _NOW,
            wire_client=wire,
        )

    def test_capability_clock_code_mutation_is_zero_callback_zero_wire(self):
        with TemporaryDirectory() as root:
            runtime = self._runtime(root)
            wire = _RecordingWire()
            sender = self._sender(runtime, wire=wire)
            transport = sender._ProductionBybitOrderSender__transport
            clock = transport.clock_utc
            original_code = clock.__code__

            def forged_clock():
                return _NOW

            authority_calls = []
            try:
                clock.__code__ = forged_clock.__code__
                with self.assertRaisesRegex(
                    PermissionError,
                    "capability clock authority code changed",
                ):
                    sender.dispatch(
                        attempt_id="attempt-clock-code-retarget",
                        intent_id="intent-clock-code-retarget",
                        intent_hash="sha256:" + "5" * 64,
                        request={"symbol": "BTCUSDT"},
                        now="2026-10-04T01:59:59Z",
                        authority_check=lambda *_args: (
                            authority_calls.append("called")
                            or (True, "authorized")
                        ),
                    )
            finally:
                clock.__code__ = original_code

            self.assertEqual(authority_calls, [])
            self.assertEqual(wire.requests, [])

    def test_capability_clock_retarget_is_zero_callback_zero_wire(self):
        with TemporaryDirectory() as root:
            runtime = self._runtime(root)
            wire = _RecordingWire()
            sender = self._sender(runtime, wire=wire)
            transport = sender._ProductionBybitOrderSender__transport
            transport.clock_utc = lambda: _NOW

            authority_calls = []
            with self.assertRaisesRegex(
                PermissionError,
                "capability clock authority changed",
            ):
                sender.dispatch(
                    attempt_id="attempt-clock-retarget",
                    intent_id="intent-clock-retarget",
                    intent_hash="sha256:" + "6" * 64,
                    request={"symbol": "BTCUSDT"},
                    now="2026-10-04T02:00:00Z",
                    authority_check=lambda *_args: (
                        authority_calls.append("called")
                        or (True, "authorized")
                    ),
                )

            self.assertEqual(authority_calls, [])
            self.assertEqual(wire.requests, [])

    def test_transport_class_rebind_is_zero_callback_zero_wire(self):
        with TemporaryDirectory() as root:
            runtime = self._runtime(root)
            wire = _RecordingWire()
            sender = self._sender(runtime, wire=wire)
            original = BybitV5HttpTransport.__call__
            forged_calls = []

            def forged(*args, **kwargs):
                forged_calls.append((args, kwargs))
                raise AssertionError("forged transport executable ran")

            BybitV5HttpTransport.__call__ = forged
            try:
                with self.assertRaisesRegex(
                    PermissionError,
                    "transport executable authority changed",
                ):
                    sender.dispatch(
                        attempt_id="attempt-transport-rebind",
                        intent_id="intent-transport-rebind",
                        intent_hash="sha256:" + "7" * 64,
                        request={"symbol": "BTCUSDT"},
                        now="2026-10-04T02:00:00Z",
                        authority_check=lambda *_args: self.fail(
                            "financial callback ran after transport retarget"
                        ),
                    )
            finally:
                BybitV5HttpTransport.__call__ = original

            self.assertEqual(forged_calls, [])
            self.assertEqual(wire.requests, [])

    def test_transport_same_function_code_mutation_is_zero_callback_zero_wire(self):
        with TemporaryDirectory() as root:
            runtime = self._runtime(root)
            wire = _RecordingWire()
            sender = self._sender(runtime, wire=wire)
            transport_call = BybitV5HttpTransport.__call__
            original_code = transport_call.__code__
            _TRANSPORT_CODE_CALLS.clear()
            try:
                transport_call.__code__ = _forged_transport_call.__code__
                with self.assertRaisesRegex(
                    PermissionError,
                    "transport executable authority code changed",
                ):
                    sender.dispatch(
                        attempt_id="attempt-transport-code",
                        intent_id="intent-transport-code",
                        intent_hash="sha256:" + "8" * 64,
                        request={"symbol": "BTCUSDT"},
                        now="2026-10-04T02:00:01Z",
                        authority_check=lambda *_args: self.fail(
                            "financial callback ran after transport code retarget"
                        ),
                    )
            finally:
                transport_call.__code__ = original_code

            self.assertEqual(_TRANSPORT_CODE_CALLS, [])
            self.assertEqual(wire.requests, [])

    def test_wire_client_replacement_is_zero_callback_zero_wire(self):
        with TemporaryDirectory() as root:
            runtime = self._runtime(root)
            wire = _RecordingWire()
            replacement = _RecordingWire()
            sender = self._sender(runtime, wire=wire)
            transport = sender._ProductionBybitOrderSender__transport
            transport.wire_client = replacement

            with self.assertRaisesRegex(
                PermissionError,
                "wire client authority changed",
            ):
                sender.dispatch(
                    attempt_id="attempt-wire-client",
                    intent_id="intent-wire-client",
                    intent_hash="sha256:" + "9" * 64,
                    request={"symbol": "BTCUSDT"},
                    now="2026-10-04T02:00:02Z",
                    authority_check=lambda *_args: self.fail(
                        "financial callback ran after wire-client retarget"
                    ),
                )

            self.assertEqual(wire.requests, [])
            self.assertEqual(replacement.requests, [])

    def test_wire_send_class_rebind_is_zero_callback_zero_wire(self):
        with TemporaryDirectory() as root:
            runtime = self._runtime(root)
            wire = _RecordingWire()
            sender = self._sender(runtime, wire=wire)
            original = _RecordingWire.send
            forged_calls = []

            def forged(*args, **kwargs):
                forged_calls.append((args, kwargs))
                raise AssertionError("forged wire send ran")

            _RecordingWire.send = forged
            try:
                with self.assertRaisesRegex(
                    PermissionError,
                    "wire send authority changed",
                ):
                    sender.dispatch(
                        attempt_id="attempt-wire-rebind",
                        intent_id="intent-wire-rebind",
                        intent_hash="sha256:" + "a" * 64,
                        request={"symbol": "BTCUSDT"},
                        now="2026-10-04T02:00:03Z",
                        authority_check=lambda *_args: self.fail(
                            "financial callback ran after wire-send retarget"
                        ),
                    )
            finally:
                _RecordingWire.send = original

            self.assertEqual(forged_calls, [])
            self.assertEqual(wire.requests, [])

    def test_wire_send_same_function_code_mutation_is_zero_callback_zero_wire(self):
        with TemporaryDirectory() as root:
            runtime = self._runtime(root)
            wire = _RecordingWire()
            sender = self._sender(runtime, wire=wire)
            wire_send = _RecordingWire.send
            original_code = wire_send.__code__
            _WIRE_CODE_CALLS.clear()
            try:
                wire_send.__code__ = _forged_wire_send.__code__
                with self.assertRaisesRegex(
                    PermissionError,
                    "wire send authority code changed",
                ):
                    sender.dispatch(
                        attempt_id="attempt-wire-code",
                        intent_id="intent-wire-code",
                        intent_hash="sha256:" + "b" * 64,
                        request={"symbol": "BTCUSDT"},
                        now="2026-10-04T02:00:04Z",
                        authority_check=lambda *_args: self.fail(
                            "financial callback ran after wire-send code retarget"
                        ),
                    )
            finally:
                wire_send.__code__ = original_code

            self.assertEqual(_WIRE_CODE_CALLS, [])
            self.assertEqual(wire.requests, [])

    def test_security_lease_wrapper_rebind_fails_before_secret_access(self):
        with TemporaryDirectory() as root:
            runtime = self._runtime(root)
            sender = self._sender(runtime)
            transport = sender._ProductionBybitOrderSender__transport
            resolver = transport.secret_resolver
            original = SecurityBoundary.lease_for_execution
            forged_calls = []

            def forged(*args, **kwargs):
                forged_calls.append((args, kwargs))
                raise AssertionError("forged credential lease ran")

            SecurityBoundary.lease_for_execution = forged
            try:
                with self.assertRaisesRegex(
                    PermissionError,
                    "credential lease authority changed",
                ):
                    resolver._require_runtime_authority()
            finally:
                SecurityBoundary.lease_for_execution = original

            self.assertEqual(forged_calls, [])

    def test_security_lease_underlying_generator_code_mutation_is_rejected(self):
        with TemporaryDirectory() as root:
            runtime = self._runtime(root)
            sender = self._sender(runtime)
            transport = sender._ProductionBybitOrderSender__transport
            resolver = transport.secret_resolver
            lease_wrapper = SecurityBoundary.lease_for_execution
            lease_generator = lease_wrapper.__wrapped__
            original_code = lease_generator.__code__
            _LEASE_CODE_CALLS.clear()
            try:
                lease_generator.__code__ = _forged_lease_generator.__code__
                with self.assertRaisesRegex(
                    PermissionError,
                    "credential lease implementation code changed",
                ):
                    resolver._require_runtime_authority()
            finally:
                lease_generator.__code__ = original_code

            self.assertEqual(_LEASE_CODE_CALLS, [])


if __name__ == "__main__":
    unittest.main()