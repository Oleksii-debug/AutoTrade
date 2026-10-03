import unittest

from mvp.autotrade_mvp.bybit_credential_probe_evidence import (
    BybitCredentialProbeEvidence,
    BybitCredentialProbeRawHttpResponse,
    BybitCredentialProbeWireResponse,
    capture_bybit_credential_probe_evidence,
    execute_bybit_credential_probe_wire_query,
)
from mvp.autotrade_mvp.provider_core import ProviderCoreError
from mvp.autotrade_mvp.windows_secrets import PersistentCredentialHandle


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

    def test_wire_boundary_attests_success_after_exact_provider_echo(self):
        client = _FakeWireClient(
            b'{"retCode":0,"retMsg":"OK","result":{"apiKey":"probe-key","secret":""}}'
        )
        result = execute_bybit_credential_probe_wire_query(
            source_uri="https://api.bybit.com/v5/user/query-api",
            headers=_headers(),
            timeout_seconds=15,
            wire_client=client,
        )
        self.assertEqual(len(client.requests), 1)
        self.assertEqual(result.ret_code, 0)
        self.assertTrue(result.api_key_echo_confirmed)
        self.assertNotIn("probe-key", repr(result))

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
