from __future__ import annotations

import tempfile
import unittest

from mvp.autotrade_mvp.bybit_credential_probe_evidence import (
    BybitCredentialProbeWireResponse,
    bybit_credential_probe_receipt_metadata,
    capture_bybit_credential_probe_evidence,
)
from mvp.autotrade_mvp.provider_core import ProviderCoreError
from mvp.autotrade_mvp.windows_secrets import PersistentCredentialHandle
from mvp.tests.test_bybit_credential_probe_evidence import (
    _probe,
    _register_probe_credential,
)


class _StrSubclass(str):
    pass


class BybitCredentialProbeHandleSnapshotAuthorityTests(unittest.TestCase):
    def test_evidence_detaches_credential_subject_from_source_handle_mutation(self) -> None:
        handle = PersistentCredentialHandle(
            handle_id="bybit-trade-primary",
            account_id="acct-17",
            provider="BYBIT",
            environment="LIVE",
            provider_environment="MAINNET",
            purpose="TRADE",
            generation=7,
        )
        evidence = capture_bybit_credential_probe_evidence(
            credential_handle=handle,
            provider_environment="MAINNET",
            source_uri="https://api.bybit.com/v5/user/query-api",
            product_family="SPOT",
            response_surface="V5_UTA_REST",
            request_timestamp_ms=1791064800123,
            recv_window_ms=5000,
            http_status=200,
            response={"retCode": 10003, "retMsg": "invalid"},
            observed_at="2026-10-03T22:01:02Z",
        )

        object.__setattr__(handle, "generation", 99)
        object.__setattr__(handle, "account_id", "forged-account")
        object.__setattr__(handle, "provider_environment", "DEMO")

        metadata = bybit_credential_probe_receipt_metadata(evidence)
        self.assertIsNot(evidence.credential_handle, handle)
        self.assertEqual(evidence.credential_handle.generation, 7)
        self.assertEqual(evidence.credential_handle.account_id, "acct-17")
        self.assertEqual(evidence.credential_handle.provider_environment, "MAINNET")
        self.assertEqual(metadata["credential_generation"], 7)
        self.assertEqual(metadata["account_id"], "acct-17")
        self.assertEqual(metadata["credential_provider_environment"], "MAINNET")

    def test_wire_callback_cannot_retarget_generation_used_for_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            vault, caller_handle = _register_probe_credential(directory)

            def wire_query(**kwargs):
                del kwargs
                object.__setattr__(caller_handle, "generation", 2)
                object.__setattr__(caller_handle, "account_id", "forged-account")
                object.__setattr__(caller_handle, "environment", "PAPER")
                object.__setattr__(caller_handle, "provider_environment", "DEMO")
                return BybitCredentialProbeWireResponse(
                    http_status=200,
                    response={"retCode": 10003, "retMsg": "invalid"},
                )

            evidence = _probe(
                vault=vault,
                handle=caller_handle,
                wire_query=wire_query,
            )

            self.assertEqual(caller_handle.generation, 2)
            self.assertEqual(evidence.credential_handle.generation, 1)
            self.assertEqual(evidence.credential_handle.account_id, "acct-17")
            self.assertEqual(evidence.credential_handle.environment, "LIVE")
            self.assertEqual(evidence.credential_handle.provider_environment, "MAINNET")
            metadata = bybit_credential_probe_receipt_metadata(evidence)
            self.assertEqual(metadata["credential_generation"], 1)
            self.assertEqual(metadata["account_id"], "acct-17")
            self.assertEqual(metadata["credential_environment"], "LIVE")
            self.assertEqual(metadata["credential_provider_environment"], "MAINNET")

    def test_noncanonical_mutated_handle_state_fails_before_capture(self) -> None:
        handle = PersistentCredentialHandle(
            handle_id="bybit-trade-primary",
            account_id="acct-17",
            provider="BYBIT",
            environment="LIVE",
            provider_environment="MAINNET",
            purpose="TRADE",
            generation=7,
        )
        object.__setattr__(handle, "provider", _StrSubclass("BYBIT"))

        with self.assertRaisesRegex(ProviderCoreError, "provider"):
            capture_bybit_credential_probe_evidence(
                credential_handle=handle,
                provider_environment="MAINNET",
                source_uri="https://api.bybit.com/v5/user/query-api",
                product_family="SPOT",
                response_surface="V5_UTA_REST",
                request_timestamp_ms=1791064800123,
                recv_window_ms=5000,
                http_status=200,
                response={"retCode": 10003},
                observed_at="2026-10-03T22:01:02Z",
            )


if __name__ == "__main__":
    unittest.main()
