import copy
import unittest

from mvp.autotrade_mvp.bybit_credential_nonacceptance import (
    BybitCredentialNonAcceptance,
)
from mvp.autotrade_mvp.bybit_credential_probe_evidence import (
    bybit_credential_probe_receipt_metadata,
    capture_bybit_credential_probe_evidence,
)
from mvp.autotrade_mvp.provider_core import ProviderCoreError
from mvp.autotrade_mvp.windows_secrets import PersistentCredentialHandle


def _handle(
    *,
    account_id="acct-17",
    environment="LIVE",
    provider_environment="MAINNET",
    generation=7,
):
    return PersistentCredentialHandle(
        handle_id="bybit-trade-primary",
        account_id=account_id,
        provider="BYBIT",
        environment=environment,
        purpose="TRADE",
        generation=generation,
        provider_environment=provider_environment,
    )


def _capture(
    *,
    handle=None,
    provider_environment="MAINNET",
    source_uri="https://api.bybit.com/v5/user/query-api",
):
    return capture_bybit_credential_probe_evidence(
        credential_handle=handle or _handle(),
        provider_environment=provider_environment,
        source_uri=source_uri,
        product_family="SPOT",
        response_surface="V5_UTA_REST",
        request_timestamp_ms=1791064800123,
        recv_window_ms=5000,
        http_status=200,
        response={"retCode": 10003, "retMsg": "API key is invalid"},
        observed_at="2026-10-03T22:01:02Z",
    )


class BybitCredentialProbeReceiptSnapshotAuthorityTests(unittest.TestCase):
    def test_unchanged_evidence_receipt_uses_issued_state(self):
        evidence = _capture()
        metadata = bybit_credential_probe_receipt_metadata(evidence)
        self.assertEqual(metadata["credential_handle_id"], "bybit-trade-primary")
        self.assertEqual(metadata["account_id"], "acct-17")
        self.assertEqual(metadata["credential_generation"], 7)
        self.assertEqual(metadata["provider_environment"], "MAINNET")
        self.assertEqual(metadata["ret_code"], 10003)
        self.assertEqual(metadata["classification"], "REJECTED_EXACT_DOMAIN")
        self.assertFalse(metadata["send_authority"])
        self.assertFalse(metadata["retirement_authority"])
        self.assertFalse(metadata["takeover_authority"])

    def test_nested_handle_mutation_cannot_retarget_issued_receipt(self):
        evidence = _capture()
        object.__setattr__(evidence.credential_handle, "account_id", "acct-retarget")
        object.__setattr__(evidence.credential_handle, "generation", 99)

        with self.assertRaisesRegex(ProviderCoreError, "mutated after receipt issuance"):
            bybit_credential_probe_receipt_metadata(evidence)

    def test_coherent_domain_and_generation_retarget_cannot_replace_issued_subject(self):
        evidence = _capture()
        object.__setattr__(
            evidence,
            "credential_handle",
            _handle(
                account_id="acct-retarget",
                environment="PAPER",
                provider_environment="DEMO",
                generation=99,
            ),
        )
        object.__setattr__(evidence, "provider_environment", "DEMO")
        object.__setattr__(
            evidence,
            "source_uri",
            "https://api-demo.bybit.com/v5/user/query-api",
        )

        with self.assertRaisesRegex(ProviderCoreError, "mutated after receipt issuance"):
            bybit_credential_probe_receipt_metadata(evidence)

    def test_retcode_and_classification_rewrite_cannot_promote_receipt(self):
        evidence = _capture()
        object.__setattr__(evidence, "ret_code", 0)
        object.__setattr__(evidence, "api_key_echo_confirmed", True)
        object.__setattr__(
            evidence,
            "classification",
            BybitCredentialNonAcceptance.ACCEPTED,
        )

        with self.assertRaisesRegex(ProviderCoreError, "mutated after receipt issuance"):
            bybit_credential_probe_receipt_metadata(evidence)

    def test_copy_without_issuance_identity_cannot_emit_receipt(self):
        evidence = _capture()
        copied = copy.copy(evidence)
        self.assertIsNot(copied, evidence)

        with self.assertRaisesRegex(ProviderCoreError, "issuance snapshot unavailable"):
            bybit_credential_probe_receipt_metadata(copied)

    def test_other_valid_evidence_state_cannot_be_substituted_under_first_identity(self):
        first = _capture()
        second = _capture(handle=_handle(account_id="acct-2", generation=8))

        for name in (
            "credential_handle",
            "provider_environment",
            "source_uri",
            "response_surface",
            "product_family",
            "request_timestamp_ms",
            "recv_window_ms",
            "http_status",
            "ret_code",
            "response_sha256",
            "observed_at",
            "api_key_echo_confirmed",
            "classification",
        ):
            object.__setattr__(first, name, getattr(second, name))

        with self.assertRaisesRegex(ProviderCoreError, "mutated after receipt issuance"):
            bybit_credential_probe_receipt_metadata(first)

    def test_non_exact_mutation_fails_before_polymorphic_state_is_used(self):
        evidence = _capture()

        class HostileText(str):
            def __eq__(self, other):
                raise AssertionError("hostile receipt comparison executed")

        object.__setattr__(evidence, "source_uri", HostileText(evidence.source_uri))
        with self.assertRaisesRegex(ProviderCoreError, "text state mutated"):
            bybit_credential_probe_receipt_metadata(evidence)


if __name__ == "__main__":
    unittest.main()
