import tempfile
import unittest
from types import MappingProxyType

import mvp.autotrade_mvp.bybit_credential_probe_evidence as probe_module
from mvp.autotrade_mvp.bybit_credential_probe_evidence import (
    BybitCredentialProbeEvidence,
    BybitCredentialProbeRawHttpResponse,
    BybitCredentialProbeWireResponse,
    capture_bybit_credential_probe_evidence,
    execute_bybit_credential_probe_wire_query,
)
from mvp.autotrade_mvp.provider_core import ProviderCoreError
from mvp.autotrade_mvp.windows_secrets import PersistentCredentialHandle
from mvp.tests.test_bybit_credential_probe_evidence import (
    _probe,
    _register_probe_credential,
)


class _FakeWireClient:
    def __init__(self, body):
        self.body = body
        self.requests = []

    def send(self, request):
        self.requests.append(request)
        return BybitCredentialProbeRawHttpResponse(
            http_status=200,
            body=self.body,
        )


class _HostileHeaders(dict):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.touched = False

    def _touch(self):
        self.touched = True
        raise AssertionError("hostile header mapping callback executed")

    def __iter__(self):
        return self._touch()

    def items(self):
        return self._touch()

    def keys(self):
        return self._touch()

    def __getitem__(self, key):
        del key
        return self._touch()


def _headers():
    return {
        "Accept": "application/json",
        "X-BAPI-API-KEY": "probe-key",
        "X-BAPI-TIMESTAMP": "1791064800123",
        "X-BAPI-RECV-WINDOW": "5000",
        "X-BAPI-SIGN": "0" * 64,
    }


def _handle():
    return PersistentCredentialHandle(
        handle_id="probe-handle",
        account_id="acct-1",
        provider="BYBIT",
        environment="LIVE",
        purpose="TRADE",
        generation=1,
        provider_environment="MAINNET",
    )


class BybitCredentialProbeAttestationTests(unittest.TestCase):
    def test_caller_flag_cannot_forge_success_wire_attestation(self):
        response = {
            "retCode": 0,
            "retMsg": "OK",
            "result": {"apiKey": "probe-key", "secret": ""},
        }
        with self.assertRaisesRegex(ProviderCoreError, "provider-derived"):
            BybitCredentialProbeWireResponse(
                http_status=200,
                response=response,
                api_key_echo_confirmed=True,
            )

    def test_caller_cannot_forge_success_durable_evidence(self):
        with self.assertRaisesRegex(ProviderCoreError, "provider-derived"):
            BybitCredentialProbeEvidence(
                credential_handle=_handle(),
                provider_environment="MAINNET",
                source_uri="https://api.bybit.com/v5/user/query-api",
                response_surface="V5_UTA_REST",
                product_family="SPOT",
                request_timestamp_ms=1791064800123,
                recv_window_ms=5000,
                http_status=200,
                ret_code=0,
                response_sha256="sha256:" + "0" * 64,
                observed_at="2030-01-01T00:00:00Z",
                api_key_echo_confirmed=True,
            )

    def test_direct_capture_cannot_forge_success_durable_evidence(self):
        response = {
            "retCode": 0,
            "retMsg": "OK",
            "result": {"apiKey": "self-authored-key", "secret": ""},
        }
        with self.assertRaisesRegex(ProviderCoreError, "provider-derived"):
            capture_bybit_credential_probe_evidence(
                credential_handle=_handle(),
                provider_environment="MAINNET",
                source_uri="https://api.bybit.com/v5/user/query-api",
                product_family="SPOT",
                response_surface="V5_UTA_REST",
                request_timestamp_ms=1791064800123,
                recv_window_ms=5000,
                http_status=200,
                response=response,
                observed_at="2030-01-01T00:00:00Z",
                api_key_echo_confirmed=True,
            )

    def test_mutated_negative_wire_cannot_be_promoted_to_accepted_evidence(self):
        forged = BybitCredentialProbeWireResponse(
            http_status=200,
            response={"retCode": 10003, "retMsg": "invalid"},
        )
        object.__setattr__(forged, "ret_code", 0)
        object.__setattr__(forged, "api_key_echo_confirmed", True)

        with tempfile.TemporaryDirectory() as directory:
            vault, handle = _register_probe_credential(directory)

            def wire_query(**kwargs):
                del kwargs
                return forged

            with self.assertRaisesRegex(ProviderCoreError, "provider-derived attestation"):
                _probe(
                    vault=vault,
                    handle=handle,
                    wire_query=wire_query,
                )

    def test_injected_wire_client_cannot_mint_success_attestation(self):
        client = _FakeWireClient(
            b'{"retCode":0,"retMsg":"OK","result":{"apiKey":"probe-key","secret":""}}'
        )
        with self.assertRaisesRegex(ProviderCoreError, "direct production transport"):
            execute_bybit_credential_probe_wire_query(
                source_uri="https://api.bybit.com/v5/user/query-api",
                headers=_headers(),
                timeout_seconds=15,
                wire_client=client,
            )
        self.assertEqual(len(client.requests), 1)

    def test_direct_transport_class_or_send_retarget_cannot_mint_attestation(self):
        body = (
            b'{"retCode":0,"retMsg":"OK","result":'
            b'{"apiKey":"probe-key","secret":""}}'
        )

        class ForgedDirectClient:
            def send(self, request):
                del request
                return BybitCredentialProbeRawHttpResponse(
                    http_status=200,
                    body=body,
                )

        original_type = probe_module.BybitCredentialProbeUrllibClient
        original_send = original_type.send
        try:
            probe_module.BybitCredentialProbeUrllibClient = ForgedDirectClient
            with self.assertRaisesRegex(
                ProviderCoreError,
                "transport type authority",
            ):
                execute_bybit_credential_probe_wire_query(
                    source_uri="https://api.bybit.com/v5/user/query-api",
                    headers=_headers(),
                    timeout_seconds=15,
                )
        finally:
            probe_module.BybitCredentialProbeUrllibClient = original_type

        def forged_send(self, request):
            del self, request
            return BybitCredentialProbeRawHttpResponse(
                http_status=200,
                body=body,
            )

        try:
            original_type.send = forged_send
            with self.assertRaisesRegex(
                ProviderCoreError,
                "transport send authority",
            ):
                execute_bybit_credential_probe_wire_query(
                    source_uri="https://api.bybit.com/v5/user/query-api",
                    headers=_headers(),
                    timeout_seconds=15,
                )
        finally:
            original_type.send = original_send

    def test_injected_wire_client_can_exercise_nonattested_rejection_path(self):
        client = _FakeWireClient(
            b'{"retCode":10003,"retMsg":"API key is invalid"}'
        )
        result = execute_bybit_credential_probe_wire_query(
            source_uri="https://api.bybit.com/v5/user/query-api",
            headers=_headers(),
            timeout_seconds=15,
            wire_client=client,
        )
        self.assertEqual(len(client.requests), 1)
        self.assertEqual(result.ret_code, 10003)
        self.assertFalse(result.api_key_echo_confirmed)

    def test_polymorphic_header_mapping_is_rejected_before_callbacks(self):
        hostile = _HostileHeaders(_headers())
        client = _FakeWireClient(b'{"retCode":10003}')
        with self.assertRaisesRegex(ProviderCoreError, "exact built-in"):
            execute_bybit_credential_probe_wire_query(
                source_uri="https://api.bybit.com/v5/user/query-api",
                headers=hostile,
                timeout_seconds=15,
                wire_client=client,
            )
        self.assertFalse(hostile.touched)
        self.assertEqual(client.requests, [])

    def test_mappingproxy_over_hostile_headers_is_rejected_before_callbacks(self):
        hostile = _HostileHeaders(_headers())
        proxy = MappingProxyType(hostile)
        client = _FakeWireClient(b'{"retCode":10003}')
        with self.assertRaisesRegex(ProviderCoreError, "exact built-in"):
            execute_bybit_credential_probe_wire_query(
                source_uri="https://api.bybit.com/v5/user/query-api",
                headers=proxy,
                timeout_seconds=15,
                wire_client=client,
            )
        self.assertFalse(hostile.touched)
        self.assertEqual(client.requests, [])

    def test_wire_boundary_rejects_success_for_different_api_key(self):
        client = _FakeWireClient(
            b'{"retCode":0,"retMsg":"OK","result":{"apiKey":"other-key","secret":""}}'
        )
        with self.assertRaisesRegex(ProviderCoreError, "does not match"):
            execute_bybit_credential_probe_wire_query(
                source_uri="https://api.bybit.com/v5/user/query-api",
                headers=_headers(),
                timeout_seconds=15,
                wire_client=client,
            )


if __name__ == "__main__":
    unittest.main()
