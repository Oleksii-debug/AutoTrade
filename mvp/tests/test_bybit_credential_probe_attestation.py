import unittest

from mvp.autotrade_mvp.bybit_credential_probe_evidence import (
    BybitCredentialProbeRawHttpResponse,
    BybitCredentialProbeWireResponse,
    execute_bybit_credential_probe_wire_query,
)
from mvp.autotrade_mvp.provider_core import ProviderCoreError


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
