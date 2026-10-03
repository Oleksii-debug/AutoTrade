from datetime import datetime, timezone
import json
import math
import tempfile
import unittest
from pathlib import Path

from mvp.autotrade_mvp.bybit_credential_nonacceptance import (
    BybitCredentialNonAcceptance,
)
from mvp.autotrade_mvp.bybit_credential_probe_evidence import (
    BybitCredentialProbeEvidence,
    BybitCredentialProbeHttpRequest,
    BybitCredentialProbeRawHttpResponse,
    BybitCredentialProbeWireResponse,
    bybit_credential_probe_receipt_metadata,
    capture_bybit_credential_probe_evidence,
    execute_bybit_credential_probe_wire_query,
    probe_bybit_credential_with_shared_wire,
    probe_bybit_credential_with_vault,
)
from mvp.autotrade_mvp.provider_core import ProviderCoreError
from mvp.autotrade_mvp.windows_secrets import (
    PersistentCredentialHandle,
    ProtectedCredentialVault,
)


class _StrSubclass(str):
    pass


class _DictSubclass(dict):
    pass


class _IntSubclass(int):
    pass


class _PassthroughProtector:
    def protect(self, plaintext, *, entropy):
        del entropy
        return plaintext

    def unprotect(self, ciphertext, *, entropy):
        del entropy
        return ciphertext


class _FakeProbeWireClient:
    def __init__(self, response):
        self.response = response
        self.requests = []

    def send(self, request):
        self.requests.append(request)
        return self.response


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
    request_timestamp_ms=1791064800123,
    recv_window_ms=5000,
    http_status=200,
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
        request_timestamp_ms=request_timestamp_ms,
        recv_window_ms=recv_window_ms,
        http_status=http_status,
        response=response,
        observed_at=observed_at,
    )


def _credential_secret(*, api_key="probe-key", api_secret="probe-secret"):
    return json.dumps(
        {"api_key": api_key, "api_secret": api_secret},
        sort_keys=True,
        separators=(",", ":"),
    )


def _register_probe_credential(
    directory,
    *,
    provider_environment="MAINNET",
    environment="LIVE",
    secret_value=None,
):
    vault = ProtectedCredentialVault(
        Path(directory) / "credentials.json",
        protector=_PassthroughProtector(),
    )
    handle = vault.register(
        owner_identity="operator-1",
        account_id="acct-17",
        provider="BYBIT",
        environment=environment,
        provider_environment=provider_environment,
        purpose="TRADE",
        secret_value=secret_value or _credential_secret(),
        handle_id="bybit-trade-primary",
    )
    return vault, handle


def _probe(
    *,
    vault,
    handle,
    wire_query,
    product_family="SPOT",
    execution_identity="operator-1",
    clock_millis=lambda: 1791064800123,
    clock_utc=lambda: datetime(2026, 10, 3, 22, 1, 2, tzinfo=timezone.utc),
    recv_window_ms=5000,
):
    return probe_bybit_credential_with_vault(
        vault=vault,
        credential_handle=handle,
        execution_identity=execution_identity,
        product_family=product_family,
        wire_query=wire_query,
        clock_millis=clock_millis,
        clock_utc=clock_utc,
        recv_window_ms=recv_window_ms,
    )


def _wire_headers():
    return {
        "Accept": "application/json",
        "X-BAPI-API-KEY": "probe-key",
        "X-BAPI-TIMESTAMP": "1791064800123",
        "X-BAPI-RECV-WINDOW": "5000",
        "X-BAPI-SIGN": "0" * 64,
    }


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
        self.assertEqual(evidence.request_timestamp_ms, 1791064800123)
        self.assertEqual(evidence.recv_window_ms, 5000)
        self.assertEqual(evidence.http_status, 200)
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

    def test_request_window_and_http_status_are_exact_receipt_fields(self):
        bad_cases = (
            {"request_timestamp_ms": True},
            {"request_timestamp_ms": -1},
            {"request_timestamp_ms": _IntSubclass(1)},
            {"recv_window_ms": True},
            {"recv_window_ms": 0},
            {"recv_window_ms": 60001},
            {"recv_window_ms": _IntSubclass(5000)},
            {"http_status": True},
            {"http_status": 201},
            {"http_status": _IntSubclass(200)},
        )
        for overrides in bad_cases:
            with self.subTest(overrides=overrides), self.assertRaises(
                ProviderCoreError
            ):
                _capture(**overrides)

    def test_response_must_be_exact_finite_json_with_integer_retcode(self):
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
            {"retCode": 10003, "bad": math.inf},
            {"retCode": 10003, "bad": -math.inf},
        )
        for response in bad_responses:
            with self.subTest(response=response), self.assertRaises(
                ProviderCoreError
            ):
                _capture(response=response)

    def test_exact_json_lists_and_finite_scalars_remain_digestible(self):
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
            request_timestamp_ms=1791064800123,
            recv_window_ms=5000,
            http_status=200,
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
                "request_timestamp_ms": 1791064800123,
                "recv_window_ms": 5000,
                "http_status": 200,
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
            request_timestamp_ms=evidence.request_timestamp_ms,
            recv_window_ms=evidence.recv_window_ms,
            http_status=evidence.http_status,
            ret_code=evidence.ret_code,
            response_sha256=evidence.response_sha256,
            observed_at=evidence.observed_at,
        )
        with self.assertRaises(TypeError):
            bybit_credential_probe_receipt_metadata(forged)

    def test_vault_probe_binds_live_generation_and_never_exposes_api_secret(self):
        with tempfile.TemporaryDirectory() as directory:
            vault, handle = _register_probe_credential(directory)
            calls = []

            def wire_query(*, source_uri, headers, timeout_seconds):
                calls.append((source_uri, dict(headers), timeout_seconds))
                self.assertEqual(
                    source_uri,
                    "https://api.bybit.com/v5/user/query-api",
                )
                self.assertEqual(headers["X-BAPI-API-KEY"], "probe-key")
                self.assertEqual(headers["X-BAPI-TIMESTAMP"], "1791064800123")
                self.assertEqual(headers["X-BAPI-RECV-WINDOW"], "5000")
                self.assertRegex(headers["X-BAPI-SIGN"], r"^[0-9a-f]{64}$")
                self.assertNotIn("probe-secret", repr(headers))
                return BybitCredentialProbeWireResponse(
                    http_status=200,
                    response={"retCode": 10003, "retMsg": "API key is invalid"},
                )

            evidence = _probe(
                vault=vault,
                handle=handle,
                wire_query=wire_query,
            )

            self.assertEqual(len(calls), 1)
            self.assertEqual(evidence.credential_handle, handle)
            self.assertEqual(evidence.credential_handle.generation, 1)
            self.assertEqual(evidence.request_timestamp_ms, 1791064800123)
            self.assertEqual(evidence.recv_window_ms, 5000)
            self.assertEqual(evidence.http_status, 200)
            self.assertIs(
                evidence.classification,
                BybitCredentialNonAcceptance.REJECTED_EXACT_DOMAIN,
            )
            self.assertNotIn("probe-secret", repr(evidence))
            self.assertNotIn(
                "probe-secret",
                repr(bybit_credential_probe_receipt_metadata(evidence)),
            )

    def test_vault_probe_derives_testnet_and_demo_origins_from_handle(self):
        cases = (
            ("TESTNET", "https://api-testnet.bybit.com/v5/user/query-api"),
            ("DEMO", "https://api-demo.bybit.com/v5/user/query-api"),
        )
        for provider_environment, expected_uri in cases:
            with self.subTest(provider_environment=provider_environment):
                with tempfile.TemporaryDirectory() as directory:
                    vault, handle = _register_probe_credential(
                        directory,
                        provider_environment=provider_environment,
                        environment="PAPER",
                    )
                    seen = []

                    def wire_query(*, source_uri, headers, timeout_seconds):
                        del headers, timeout_seconds
                        seen.append(source_uri)
                        return BybitCredentialProbeWireResponse(
                            http_status=200,
                            response={"retCode": 0, "retMsg": "OK"},
                        )

                    evidence = _probe(
                        vault=vault,
                        handle=handle,
                        wire_query=wire_query,
                    )
                    self.assertEqual(seen, [expected_uri])
                    self.assertEqual(
                        evidence.provider_environment,
                        provider_environment,
                    )
                    self.assertEqual(evidence.source_uri, expected_uri)

    def test_stale_generation_fails_before_wire_query(self):
        with tempfile.TemporaryDirectory() as directory:
            vault, old_handle = _register_probe_credential(directory)
            current_handle = vault.rotate(
                old_handle,
                execution_identity="operator-1",
                new_secret_value=_credential_secret(
                    api_key="generation-2-key",
                    api_secret="generation-2-secret",
                ),
            )
            calls = []

            def wire_query(**kwargs):
                calls.append(kwargs)
                return BybitCredentialProbeWireResponse(
                    http_status=200,
                    response={"retCode": 0},
                )

            with self.assertRaisesRegex(PermissionError, "stale"):
                _probe(
                    vault=vault,
                    handle=old_handle,
                    wire_query=wire_query,
                )
            self.assertEqual(calls, [])

            evidence = _probe(
                vault=vault,
                handle=current_handle,
                wire_query=wire_query,
            )
            self.assertEqual(evidence.credential_handle.generation, 2)
            self.assertEqual(len(calls), 1)
            self.assertEqual(
                calls[0]["headers"]["X-BAPI-API-KEY"],
                "generation-2-key",
            )

    def test_wrong_execution_identity_fails_before_wire_query(self):
        with tempfile.TemporaryDirectory() as directory:
            vault, handle = _register_probe_credential(directory)
            calls = []

            def wire_query(**kwargs):
                calls.append(kwargs)
                return BybitCredentialProbeWireResponse(
                    http_status=200,
                    response={"retCode": 0},
                )

            with self.assertRaisesRegex(PermissionError, "identity"):
                _probe(
                    vault=vault,
                    handle=handle,
                    execution_identity="other-operator",
                    wire_query=wire_query,
                )
            self.assertEqual(calls, [])

    def test_invalid_credential_material_fails_before_wire_query(self):
        with tempfile.TemporaryDirectory() as directory:
            vault, handle = _register_probe_credential(
                directory,
                secret_value='{"api_key":"key-only"}',
            )
            calls = []

            def wire_query(**kwargs):
                calls.append(kwargs)
                return BybitCredentialProbeWireResponse(
                    http_status=200,
                    response={"retCode": 0},
                )

            with self.assertRaisesRegex(ProviderCoreError, "material"):
                _probe(
                    vault=vault,
                    handle=handle,
                    wire_query=wire_query,
                )
            self.assertEqual(calls, [])

    def test_non_200_http_result_is_unknown_not_rejection_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            vault, handle = _register_probe_credential(directory)

            def wire_query(**kwargs):
                del kwargs
                return BybitCredentialProbeWireResponse(
                    http_status=401,
                    response={"retCode": 10003, "retMsg": "invalid"},
                )

            with self.assertRaisesRegex(ProviderCoreError, "non-200"):
                _probe(
                    vault=vault,
                    handle=handle,
                    wire_query=wire_query,
                )

    def test_probe_rejects_untyped_wire_result_and_subclass(self):
        with tempfile.TemporaryDirectory() as directory:
            vault, handle = _register_probe_credential(directory)

            def plain_dict_wire(**kwargs):
                del kwargs
                return {"retCode": 10003}

            with self.assertRaisesRegex(TypeError, "WireResponse"):
                _probe(
                    vault=vault,
                    handle=handle,
                    wire_query=plain_dict_wire,
                )

            class _WireSubclass(BybitCredentialProbeWireResponse):
                pass

            def subclass_wire(**kwargs):
                del kwargs
                return _WireSubclass(
                    http_status=200,
                    response={"retCode": 10003},
                )

            with self.assertRaisesRegex(TypeError, "WireResponse"):
                _probe(
                    vault=vault,
                    handle=handle,
                    wire_query=subclass_wire,
                )

    def test_probe_clocks_and_recv_window_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            vault, handle = _register_probe_credential(directory)
            calls = []

            def wire_query(**kwargs):
                calls.append(kwargs)
                return BybitCredentialProbeWireResponse(
                    http_status=200,
                    response={"retCode": 10003},
                )

            for bad_timestamp in (True, -1, _IntSubclass(1), "1"):
                with self.subTest(bad_timestamp=bad_timestamp), self.assertRaises(
                    ProviderCoreError
                ):
                    _probe(
                        vault=vault,
                        handle=handle,
                        wire_query=wire_query,
                        clock_millis=lambda value=bad_timestamp: value,
                    )
            self.assertEqual(calls, [])

            for bad_window in (True, 0, 60001, _IntSubclass(5000)):
                with self.subTest(bad_window=bad_window), self.assertRaises(
                    ProviderCoreError
                ):
                    _probe(
                        vault=vault,
                        handle=handle,
                        wire_query=wire_query,
                        recv_window_ms=bad_window,
                    )
            self.assertEqual(calls, [])

            with self.assertRaisesRegex(ProviderCoreError, "clock_utc"):
                _probe(
                    vault=vault,
                    handle=handle,
                    wire_query=wire_query,
                    clock_utc=lambda: datetime(2026, 10, 3, 22, 1, 2),
                )
            self.assertEqual(len(calls), 1)

    def test_wire_response_rejects_non_exact_json_before_evidence_capture(self):
        bad = (
            (True, {"retCode": 0}),
            (_IntSubclass(200), {"retCode": 0}),
            (99, {"retCode": 0}),
            (600, {"retCode": 0}),
            (200, _DictSubclass(retCode=0)),
            (200, {"retCode": 0, "bad": (1, 2)}),
            (200, {"retCode": 0, "bad": math.inf}),
        )
        for http_status, response in bad:
            with self.subTest(
                http_status=http_status,
                response=response,
            ), self.assertRaises(ProviderCoreError):
                BybitCredentialProbeWireResponse(
                    http_status=http_status,
                    response=response,
                )

    def test_shared_wire_probe_builds_exact_empty_query_get_request(self):
        with tempfile.TemporaryDirectory() as directory:
            vault, handle = _register_probe_credential(directory)
            client = _FakeProbeWireClient(
                BybitCredentialProbeRawHttpResponse(
                    http_status=200,
                    body=b'{"retCode":10003,"retMsg":"API key is invalid"}',
                )
            )

            evidence = probe_bybit_credential_with_shared_wire(
                vault=vault,
                credential_handle=handle,
                execution_identity="operator-1",
                product_family="SPOT",
                clock_millis=lambda: 1791064800123,
                clock_utc=lambda: datetime(
                    2026,
                    10,
                    3,
                    22,
                    1,
                    2,
                    tzinfo=timezone.utc,
                ),
                wire_client=client,
            )

            self.assertEqual(len(client.requests), 1)
            request = client.requests[0]
            self.assertIs(type(request), BybitCredentialProbeHttpRequest)
            self.assertEqual(
                request.url,
                "https://api.bybit.com/v5/user/query-api",
            )
            self.assertNotIn("?", request.url)
            self.assertEqual(request.headers["X-BAPI-API-KEY"], "probe-key")
            self.assertNotIn("probe-secret", repr(request.headers))
            self.assertIs(
                evidence.classification,
                BybitCredentialNonAcceptance.REJECTED_EXACT_DOMAIN,
            )

    def test_shared_wire_adapter_rejects_origin_escape_before_send(self):
        client = _FakeProbeWireClient(
            BybitCredentialProbeRawHttpResponse(
                http_status=200,
                body=b'{"retCode":0}',
            )
        )
        hostile = (
            "https://evil.example/v5/user/query-api",
            "https://api.bybit.com.evil.example/v5/user/query-api",
            "http://api.bybit.com/v5/user/query-api",
            "https://api.bybit.com/v5/order/create",
        )
        for source_uri in hostile:
            with self.subTest(source_uri=source_uri), self.assertRaises(
                ProviderCoreError
            ):
                execute_bybit_credential_probe_wire_query(
                    source_uri=source_uri,
                    headers=_wire_headers(),
                    timeout_seconds=15,
                    wire_client=client,
                )
        self.assertEqual(client.requests, [])

    def test_shared_wire_adapter_rejects_header_shape_before_send(self):
        client = _FakeProbeWireClient(
            BybitCredentialProbeRawHttpResponse(
                http_status=200,
                body=b'{"retCode":0}',
            )
        )
        bad_headers = []
        missing = _wire_headers()
        del missing["X-BAPI-SIGN"]
        bad_headers.append(missing)
        extra = _wire_headers()
        extra["Authorization"] = "unexpected"
        bad_headers.append(extra)
        bad_signature = _wire_headers()
        bad_signature["X-BAPI-SIGN"] = "A" * 64
        bad_headers.append(bad_signature)
        bad_window = _wire_headers()
        bad_window["X-BAPI-RECV-WINDOW"] = "05000"
        bad_headers.append(bad_window)
        newline = _wire_headers()
        newline["X-BAPI-API-KEY"] = "key\r\nInjected: yes"
        bad_headers.append(newline)

        for headers in bad_headers:
            with self.subTest(headers=headers), self.assertRaises(
                ProviderCoreError
            ):
                execute_bybit_credential_probe_wire_query(
                    source_uri="https://api.bybit.com/v5/user/query-api",
                    headers=headers,
                    timeout_seconds=15,
                    wire_client=client,
                )
        self.assertEqual(client.requests, [])

    def test_shared_wire_json_parser_rejects_ambiguous_nonfinite_or_non_object_payload(self):
        bodies = (
            b'{"retCode":0,"retCode":10003}',
            b'[]',
            b'{"retCode":NaN}',
            b'{"retCode":0,"ratio":1e9999}',
            b'not-json',
            b'\xff',
        )
        for body in bodies:
            with self.subTest(body=body):
                client = _FakeProbeWireClient(
                    BybitCredentialProbeRawHttpResponse(
                        http_status=200,
                        body=body,
                    )
                )
                with self.assertRaises(ProviderCoreError):
                    execute_bybit_credential_probe_wire_query(
                        source_uri="https://api.bybit.com/v5/user/query-api",
                        headers=_wire_headers(),
                        timeout_seconds=15,
                        wire_client=client,
                    )
                self.assertEqual(len(client.requests), 1)

    def test_shared_wire_requires_typed_status_preserving_response(self):
        class UntypedClient:
            def send(self, request):
                del request
                return b'{"retCode":0}'

        with self.assertRaisesRegex(TypeError, "RawHttpResponse"):
            execute_bybit_credential_probe_wire_query(
                source_uri="https://api.bybit.com/v5/user/query-api",
                headers=_wire_headers(),
                timeout_seconds=15,
                wire_client=UntypedClient(),
            )

    def test_shared_wire_non_200_stays_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            vault, handle = _register_probe_credential(directory)
            client = _FakeProbeWireClient(
                BybitCredentialProbeRawHttpResponse(
                    http_status=401,
                    body=b'{"retCode":10003,"retMsg":"invalid"}',
                )
            )
            with self.assertRaisesRegex(ProviderCoreError, "non-200"):
                probe_bybit_credential_with_shared_wire(
                    vault=vault,
                    credential_handle=handle,
                    execution_identity="operator-1",
                    product_family="SPOT",
                    clock_millis=lambda: 1791064800123,
                    clock_utc=lambda: datetime.now(timezone.utc),
                    wire_client=client,
                )
            self.assertEqual(len(client.requests), 1)

    def test_wire_exception_produces_no_evidence_and_propagates(self):
        with tempfile.TemporaryDirectory() as directory:
            vault, handle = _register_probe_credential(directory)

            class ExpectedWireFailure(RuntimeError):
                pass

            def wire_query(**kwargs):
                del kwargs
                raise ExpectedWireFailure("network unavailable")

            with self.assertRaises(ExpectedWireFailure):
                _probe(
                    vault=vault,
                    handle=handle,
                    wire_query=wire_query,
                )


if __name__ == "__main__":
    unittest.main()
