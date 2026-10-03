import math
import unittest

from mvp.autotrade_mvp.bybit_credential_nonacceptance import (
    BybitCredentialNonAcceptance,
)
from mvp.autotrade_mvp.bybit_credential_probe_evidence import (
    BybitCredentialProbeEvidence,
    bybit_credential_probe_receipt_metadata,
    capture_bybit_credential_probe_evidence,
)
from mvp.autotrade_mvp.provider_core import ProviderCoreError
from mvp.autotrade_mvp.windows_secrets import PersistentCredentialHandle


class _StrSubclass(str):
    pass


class _DictSubclass(dict):
    pass


class _IntSubclass(int):
    pass


def _handle(
    *,
    provider="BYBIT",
    environment="LIVE",
    purpose="TRADE",
    generation=7,
    provider_environment=None,
):
    if provider_environment is None:
        provider_environment = "MAINNET" if environment == "LIVE" else "TESTNET"
    return PersistentCredentialHandle(
        handle_id="bybit-trade-primary",
        account_id="acct-17",
        provider=provider,
        environment=environment,
        purpose=purpose,
        generation=generation,
        provider_environment=provider_environment,
    )


def _capture(
    *,
    handle=None,
    provider_environment="MAINNET",
    source_uri="https://api.bybit.com/v5/user/query-api",
    product_family="SPOT",
    response_surface="V5_UTA_REST",
    response=None,
    observed_at="2026-10-03T22:01:02Z",
):
    if handle is None:
        handle = _handle()
    if response is None:
        response = {"retCode": 10003, "retMsg": "API key is invalid"}
    return capture_bybit_credential_probe_evidence(
        credential_handle=handle,
        provider_environment=provider_environment,
        source_uri=source_uri,
        product_family=product_family,
        response_surface=response_surface,
        response=response,
        observed_at=observed_at,
    )


class BybitCredentialProbeEvidenceTests(unittest.TestCase):
    def test_mainnet_rejection_binds_exact_trade_generation_without_authority(self):
        evidence = _capture()

        self.assertEqual(evidence.credential_handle.generation, 7)
        self.assertEqual(evidence.credential_handle.environment, "LIVE")
        self.assertEqual(evidence.provider_environment, "MAINNET")
        self.assertEqual(
            evidence.source_uri,
            "https://api.bybit.com/v5/user/query-api",
        )
        self.assertIs(
            evidence.classification,
            BybitCredentialNonAcceptance.REJECTED_EXACT_DOMAIN,
        )
        self.assertFalse(evidence.send_authority)
        self.assertFalse(evidence.retirement_authority)
        self.assertFalse(evidence.takeover_authority)

    def test_testnet_and_demo_require_paper_credential_scope(self):
        cases = (
            ("TESTNET", "https://api-testnet.bybit.com/v5/user/query-api"),
            ("DEMO", "https://api-demo.bybit.com/v5/user/query-api"),
        )
        for provider_environment, source_uri in cases:
            with self.subTest(provider_environment=provider_environment):
                evidence = _capture(
                    handle=_handle(
                        environment="PAPER",
                        provider_environment=provider_environment,
                    ),
                    provider_environment=provider_environment,
                    source_uri=source_uri,
                )
                self.assertEqual(evidence.credential_handle.environment, "PAPER")
                self.assertEqual(
                    evidence.provider_environment,
                    provider_environment,
                )

    def test_provider_environment_must_match_credential_runtime_environment(self):
        with self.assertRaisesRegex(ProviderCoreError, "environment"):
            _capture(
                handle=_handle(environment="LIVE"),
                provider_environment="TESTNET",
                source_uri="https://api-testnet.bybit.com/v5/user/query-api",
            )
        with self.assertRaisesRegex(ProviderCoreError, "environment"):
            _capture(
                handle=_handle(
                    environment="PAPER",
                    provider_environment="TESTNET",
                ),
                provider_environment="MAINNET",
            )

    def test_same_paper_runtime_cannot_alias_testnet_and_demo_domains(self):
        with self.assertRaisesRegex(ProviderCoreError, "provider domain"):
            _capture(
                handle=_handle(
                    environment="PAPER",
                    provider_environment="TESTNET",
                ),
                provider_environment="DEMO",
                source_uri="https://api-demo.bybit.com/v5/user/query-api",
            )
        with self.assertRaisesRegex(ProviderCoreError, "provider domain"):
            _capture(
                handle=_handle(
                    environment="PAPER",
                    provider_environment="DEMO",
                ),
                provider_environment="TESTNET",
                source_uri="https://api-testnet.bybit.com/v5/user/query-api",
            )

    def test_only_exact_bybit_trade_handles_are_admitted(self):
        with self.assertRaisesRegex(ProviderCoreError, "BYBIT"):
            _capture(handle=_handle(provider="KRAKEN"))
        with self.assertRaisesRegex(ProviderCoreError, "TRADE"):
            _capture(handle=_handle(purpose="READ"))

    def test_exact_query_api_origin_is_required(self):
        hostile_or_wrong_sources = (
            "http://api.bybit.com/v5/user/query-api",
            "https://api.bybit.com/v5/user/query-api/",
            "https://api.bybit.com/v5/user/query-api?x=1",
            "https://api.bybit.com/v5/order/create",
            "https://api.bybit.com.evil.example/v5/user/query-api",
            "https://api.bybit.com@evil.example/v5/user/query-api",
            " https://api.bybit.com/v5/user/query-api",
        )
        for source_uri in hostile_or_wrong_sources:
            with self.subTest(source_uri=source_uri), self.assertRaises(
                ProviderCoreError
            ):
                _capture(source_uri=source_uri)

    def test_noncanonical_provider_scope_scalars_fail_closed(self):
        cases = (
            {"provider_environment": "mainnet"},
            {"provider_environment": "MAINNET "},
            {"provider_environment": _StrSubclass("MAINNET")},
            {"source_uri": _StrSubclass("https://api.bybit.com/v5/user/query-api")},
            {"product_family": "spot"},
            {"product_family": _StrSubclass("SPOT")},
            {"response_surface": _StrSubclass("V5_UTA_REST")},
            {"response_surface": "WS_OE_GENERAL"},
        )
        for overrides in cases:
            with self.subTest(overrides=overrides), self.assertRaises(
                ProviderCoreError
            ):
                _capture(**overrides)

    def test_response_must_be_exact_json_object_with_exact_integer_retcode(self):
        bad_responses = (
            _DictSubclass(retCode=10003),
            {},
            {"retCode": "10003"},
            {"retCode": True},
            {"retCode": _IntSubclass(10003)},
            {"retCode": 10003, "bad": object()},
            {"retCode": 10003, "bad": ("tuple", "is-not-json")},
            {"retCode": 10003, "bad": _DictSubclass(value=1)},
            {"retCode": 10003, "bad": {"nested": _IntSubclass(1)}},
            {"retCode": 10003, "bad": {1: "non-text-key"}},
            {"retCode": 10003, "bad": math.nan},
        )
        for response in bad_responses:
            with self.subTest(response=response), self.assertRaises(
                ProviderCoreError
            ):
                _capture(response=response)

    def test_exact_json_lists_and_scalars_remain_digestible(self):
        evidence = _capture(
            response={
                "retCode": 10003,
                "retMsg": "invalid",
                "result": {
                    "flags": [True, False, None],
                    "counts": [0, 1, 2],
                    "ratio": 1.25,
                    "labels": ["a", "b"],
                },
            }
        )
        self.assertRegex(evidence.response_sha256, r"^sha256:[0-9a-f]{64}$")

    def test_success_payload_is_scrubbed_to_digest_and_still_accepted(self):
        raw_api_key = "RAW-API-KEY-MUST-NOT-PERSIST"
        raw_secret = "RAW-SECRET-MUST-NOT-PERSIST"
        response = {
            "retCode": 0,
            "retMsg": "OK",
            "result": {
                "apiKey": raw_api_key,
                "secret": raw_secret,
                "readOnly": 0,
                "permissions": {"ContractTrade": ["Order"]},
            },
        }
        evidence = _capture(response=response)
        metadata = bybit_credential_probe_receipt_metadata(evidence)

        self.assertIs(
            evidence.classification,
            BybitCredentialNonAcceptance.STILL_ACCEPTED,
        )
        self.assertRegex(evidence.response_sha256, r"^sha256:[0-9a-f]{64}$")
        serialized_observation = repr(evidence) + repr(metadata)
        self.assertNotIn(raw_api_key, serialized_observation)
        self.assertNotIn(raw_secret, serialized_observation)
        self.assertNotIn("permissions", serialized_observation)

    def test_digest_is_deterministic_for_equivalent_json_objects(self):
        first = _capture(
            response={
                "retCode": 10003,
                "retMsg": "invalid",
                "result": {"b": 2, "a": 1},
            }
        )
        second = _capture(
            response={
                "result": {"a": 1, "b": 2},
                "retMsg": "invalid",
                "retCode": 10003,
            }
        )
        self.assertEqual(first.response_sha256, second.response_sha256)

    def test_mutating_source_response_after_capture_cannot_change_evidence(self):
        response = {
            "retCode": 10003,
            "retMsg": "invalid",
            "result": {"marker": "before"},
        }
        evidence = _capture(response=response)
        digest = evidence.response_sha256

        response["retCode"] = 0
        response["result"]["marker"] = "after"

        self.assertEqual(evidence.ret_code, 10003)
        self.assertEqual(evidence.response_sha256, digest)
        self.assertIs(
            evidence.classification,
            BybitCredentialNonAcceptance.REJECTED_EXACT_DOMAIN,
        )

    def test_observation_time_is_normalized_to_utc(self):
        evidence = _capture(observed_at="2026-10-04T00:01:02+02:00")
        self.assertEqual(evidence.observed_at, "2026-10-03T22:01:02Z")
        for bad_value in (
            "2026-10-03T22:01:02",
            " 2026-10-03T22:01:02Z",
            _StrSubclass("2026-10-03T22:01:02Z"),
        ):
            with self.subTest(bad_value=bad_value), self.assertRaises(
                ProviderCoreError
            ):
                _capture(observed_at=bad_value)

    def test_direct_construction_rejects_forged_digest_and_retcode_types(self):
        kwargs = dict(
            credential_handle=_handle(),
            provider_environment="MAINNET",
            source_uri="https://api.bybit.com/v5/user/query-api",
            response_surface="V5_UTA_REST",
            product_family="SPOT",
            ret_code=10003,
            response_sha256="sha256:" + "0" * 64,
            observed_at="2026-10-03T22:01:02Z",
        )
        for response_sha256 in (
            "0" * 64,
            "sha256:" + "A" * 64,
            "sha256:" + "0" * 63,
            _StrSubclass("sha256:" + "0" * 64),
        ):
            with self.subTest(response_sha256=response_sha256), self.assertRaises(
                ProviderCoreError
            ):
                BybitCredentialProbeEvidence(
                    **{**kwargs, "response_sha256": response_sha256}
                )
        for ret_code in (True, _IntSubclass(10003)):
            with self.subTest(ret_code=ret_code), self.assertRaises(
                ProviderCoreError
            ):
                BybitCredentialProbeEvidence(**{**kwargs, "ret_code": ret_code})

    def test_receipt_metadata_preserves_generation_and_negative_authority(self):
        evidence = _capture(handle=_handle(generation=23))
        metadata = bybit_credential_probe_receipt_metadata(evidence)

        self.assertEqual(
            metadata,
            {
                "evidence_kind": "BYBIT_CREDENTIAL_PROBE",
                "provider": "BYBIT",
                "provider_environment": "MAINNET",
                "credential_handle_id": "bybit-trade-primary",
                "account_id": "acct-17",
                "credential_environment": "LIVE",
                "credential_provider_environment": "MAINNET",
                "credential_purpose": "TRADE",
                "credential_generation": 23,
                "source_uri": "https://api.bybit.com/v5/user/query-api",
                "response_surface": "V5_UTA_REST",
                "product_family": "SPOT",
                "ret_code": 10003,
                "response_sha256": evidence.response_sha256,
                "observed_at": "2026-10-03T22:01:02Z",
                "classification": "REJECTED_EXACT_DOMAIN",
                "send_authority": False,
                "retirement_authority": False,
                "takeover_authority": False,
            },
        )

    def test_receipt_metadata_rejects_subclass_authority_objects(self):
        class _EvidenceSubclass(BybitCredentialProbeEvidence):
            pass

        evidence = _capture()
        forged = _EvidenceSubclass(
            credential_handle=evidence.credential_handle,
            provider_environment=evidence.provider_environment,
            source_uri=evidence.source_uri,
            response_surface=evidence.response_surface,
            product_family=evidence.product_family,
            ret_code=evidence.ret_code,
            response_sha256=evidence.response_sha256,
            observed_at=evidence.observed_at,
        )
        with self.assertRaises(TypeError):
            bybit_credential_probe_receipt_metadata(forged)


if __name__ == "__main__":
    unittest.main()
