from datetime import datetime, timedelta, timezone, tzinfo
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN, localcontext
from hashlib import sha256
import json
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import autotrade_numeric.exact_decimal as neutral_numeric
import mvp.autotrade_mvp.dispatch as legacy_dispatch
import mvp.autotrade_mvp.provider_core as provider_core_module

from mvp.autotrade_mvp.dispatch import (
    ExactJsonTransportResponse,
    GuardedDispatcher,
    load_submission_response_binding,
)
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.provider_core import (
    ClockGuard,
    PROVIDERS,
    REQUIRED_QUALIFICATION_CASES,
    ProviderCoreError,
    ProviderSubmissionObservation,
    QualificationEvidence,
    QuotaBucket,
    WriteOutcome,
    classify_write_outcome,
    observe_submission_json_response,
    provider_definition,
    provider_submission_observation_projection,
)


NOW = datetime(2026, 9, 24, 18, tzinfo=timezone.utc)
CODE_SHA = "a" * 40
OTHER_SHA = "b" * 40


class _NoOffsetTZ(tzinfo):
    def utcoffset(self, dt):
        return None

    def dst(self, dt):
        return None


class ProviderCoreTests(unittest.TestCase):
    def test_raw_provider_json_keeps_exact_decimals_and_int_identity(self):
        payload = provider_core_module._decode_exact_json(
            b'{"price":65000.10,"fee":0.0100,"negative":-12,'
            b'"negative_zero":-0,"sequence":12345678901234567890}'
        )
        self.assertEqual(
            payload["price"].as_tuple(), Decimal("65000.10").as_tuple()
        )
        self.assertEqual(
            payload["fee"].as_tuple(), Decimal("0.0100").as_tuple()
        )
        self.assertIs(type(payload["sequence"]), int)
        self.assertEqual(payload["sequence"], 12345678901234567890)
        self.assertEqual(payload["negative"], -12)
        self.assertEqual(payload["negative_zero"], 0)

    def test_raw_oversized_provider_tokens_reject_before_decimal_constructor(self):
        invalid_raw = (
            b'{"price":1e256}',
            b'{"price":1e-257}',
            b'{"price":' + b'9' * 257 + b'}',
            b'{"price":0.' + b'0' * 256 + b'1}',
            b'{"sequence":' + b'9' * 257 + b'}',
        )
        for raw in invalid_raw:
            with self.subTest(length=len(raw), prefix=raw[:20]):
                with patch.object(
                    neutral_numeric,
                    "Decimal",
                    side_effect=AssertionError("Decimal constructed before token check"),
                ):
                    with self.assertRaisesRegex(
                        ProviderCoreError, "invalid or oversized exact JSON number"
                    ) as rejected:
                        provider_core_module._decode_exact_json(raw)
                # No provider token is retained in the error's chain.
                # Diagnostic redaction intentionally removes the old parser
                # exception cause while preserving constructor-spy denial.
                self.assertIsNone(rejected.exception.__cause__)
                self.assertIsNone(rejected.exception.__context__)
                self.assertNotIn("999999", str(rejected.exception))

    def test_direct_provider_numeric_fields_share_single_bounded_policy(self):
        accepted = provider_core_module._decimal("65000.10", "price")
        self.assertEqual(
            accepted.as_tuple(), Decimal("65000.10").as_tuple()
        )
        self.assertEqual(
            provider_core_module._decimal("0e-99999999", "quantity"),
            Decimal("0"),
        )
        with self.assertRaisesRegex(ProviderCoreError, "cannot be negative"):
            provider_core_module._decimal("-1", "quantity", non_negative=True)
        for value in (
            "1e256", "1e-257", "9" * 257, float("inf"), True,
            Decimal("Infinity"),
        ):
            with self.subTest(value_type=type(value).__name__):
                with self.assertRaises(ProviderCoreError):
                    provider_core_module._decimal(value, "price")
        class HostileDecimal(Decimal):
            def as_tuple(self):
                raise AssertionError("provider must not dispatch Decimal subclass")
        class HostileString(str):
            def __len__(self):
                raise AssertionError("provider must not dispatch string subclass")
        for value in (HostileDecimal("1.25"), HostileString("1.25")):
            with self.subTest(hostile_type=type(value).__name__):
                with self.assertRaises(ProviderCoreError):
                    provider_core_module._decimal(value, "price")

    def test_submission_observation_reparses_sha_bound_exact_response_bytes(self):
        # Dispatch's transport-only JSON preview may pass through default
        # json.loads floats. The financial observation must use untouched raw
        # journal bytes, keeping scale/trailing-zero identity from Decimal.
        with TemporaryDirectory() as directory:
            binding, request_sha = self._durable_submission_binding(
                directory,
                raw=(
                    b'{"orderId":"provider-1","price":65000.10,'
                    b'"fee":0.0100,"sequence":12345678901234567890}'
                ),
            )
            observation = observe_submission_json_response(
                response_binding=binding,
                provider_id="BYBIT",
                endpoint="/v5/order/create",
                prepared_request_sha256=request_sha,
                capability_snapshot_ids=("cap-1",),
                instrument_versions=("BTCUSD:v1",),
            )
            self.assertEqual(
                observation.payload["price"].as_tuple(),
                Decimal("65000.10").as_tuple(),
            )
            self.assertEqual(
                observation.payload["fee"].as_tuple(),
                Decimal("0.0100").as_tuple(),
            )
            self.assertIs(type(observation.payload["sequence"]), int)
            self.assertEqual(observation.response_sha256, binding.response_sha256)

            with (
                patch.object(
                    ProviderSubmissionObservation,
                    "response_binding",
                    property(lambda _observation: None),
                    create=True,
                ),
                patch.object(
                    ProviderSubmissionObservation,
                    "endpoint",
                    property(lambda _observation: "/forged"),
                    create=True,
                ),
            ):
                sealed = provider_submission_observation_projection(observation)
                self.assertEqual(sealed["provider_id"], "BYBIT")
                self.assertEqual(sealed["endpoint"], "/v5/order/create")
                self.assertEqual(
                    sealed["response_sha256"],
                    binding.response_sha256,
                )
                self.assertEqual(observation.provider_id, "BYBIT")
                observation.require_scope(
                    provider_id="BYBIT",
                    endpoint="/v5/order/create",
                    prepared_request_sha256=request_sha,
                    capability_snapshot_ids=("cap-1",),
                    instrument_versions=("BTCUSD:v1",),
                )

    def test_invalid_response_cannot_bypass_sealed_dispatch_decoder(self):
        with TemporaryDirectory() as directory:
            # A historical weak decoder can no longer be injected to mint a
            # definitive durable response. The post-SEND transport response
            # authority detects the decoder retarget before the response can be
            # consumed as SENT and leaves the attempt reconciliation-required.
            legacy_raw = b'{"orderId":"provider-1","price":1e256}'
            store = JournalStore(f"{directory}/journal.sqlite3")
            dispatcher = GuardedDispatcher(
                store,
                environment="SIMULATION",
                account_id="acct",
                owner_token="owner",
            )
            request = {"symbol": "BTCUSD", "qty": "1"}
            request_text = json.dumps(
                request,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            )
            request_sha = "sha256:" + sha256(request_text.encode("utf-8")).hexdigest()
            scope = {
                "endpoint": "/v5/order/create",
                "prepared_request_sha256": request_sha,
                "capability_snapshot_ids": ["cap-1"],
                "instrument_versions": ["BTCUSD:v1"],
            }

            def transport(_client_id, _request, guard):
                guard()
                return ExactJsonTransportResponse(legacy_raw)

            with patch.object(
                legacy_dispatch,
                "_decode_exact_json_bytes",
                side_effect=lambda raw: json.loads(raw.decode("utf-8")),
            ):
                outcome = dispatcher.dispatch(
                    attempt_id="sealed-decoder-a1",
                    intent_id="intent-1",
                    intent_hash="intent-hash",
                    provider="BYBIT",
                    request=request,
                    now="2026-09-24T18:00:00Z",
                    authority_check=lambda _hash, _now: (True, "allowed"),
                    transport_send=transport,
                    submission_scope=scope,
                )

            self.assertEqual(outcome.status, "UNKNOWN")
            self.assertEqual(outcome.reason, "transport_result_ambiguous")
            events = store.load_events(
                "submission_attempt",
                dispatcher._aggregate_id("sealed-decoder-a1"),
            )
            self.assertEqual(
                [event["event_type"] for event in events],
                ["SubmissionPrepared", "SubmissionSending", "SubmissionUnknown"],
            )
            self.assertEqual(
                events[-1]["payload"]["reason"],
                "transport_exception_after_send_barrier:ValueError",
            )

    def test_oversized_raw_bytes_fail_before_utf8_decode_or_json_materialization(self):
        # A leading invalid UTF-8 byte distinguishes resource-first rejection
        # from a pre-budget raw.decode() allocation/error. The payload is exact
        # bytes and exceeds the one shared hard ceiling by precisely one byte.
        from mvp.autotrade_mvp.provider_response_limits import HARD_MAX_PROVIDER_RESPONSE_BYTES

        raw = b"\xff" + b"x" * HARD_MAX_PROVIDER_RESPONSE_BYTES
        with (
            patch.object(
                provider_core_module.json,
                "loads",
                side_effect=AssertionError("JSON materialized before byte preflight"),
            ),
            patch.object(
                provider_core_module,
                "parse_bounded_json_number_token",
                side_effect=AssertionError("Decimal parsed before byte preflight"),
            ),
        ):
            with self.assertRaisesRegex(
                ProviderCoreError, "resource budget"
            ) as rejected:
                provider_core_module._decode_exact_json(raw)
        self.assertIsNone(rejected.exception.__cause__)
        self.assertIsNone(rejected.exception.__context__)

    def test_json_structural_bound_precedes_recursive_materialization(self):
        # _freeze_json permits 65 nested empty containers (root depth 0).
        at_limit = b"[" * 65 + b"]" * 65
        result = provider_core_module._decode_exact_json(at_limit)
        for _ in range(65):
            self.assertIs(type(result), tuple)
            result = result[0] if result else None
        self.assertIsNone(result)

        # Brackets in strings, including escaped quotes, are not structure.
        text_payload = {
            "note": "[" * 300 + "]" * 300,
            "escaped": chr(92) + '"' + "[[{",
        }
        decoded = provider_core_module._decode_exact_json(
            json.dumps(text_payload).encode("utf-8")
        )
        self.assertEqual(decoded["note"], text_payload["note"])
        self.assertEqual(decoded["escaped"], text_payload["escaped"])

        # 66 nested arrays are valid JSON but beyond the installed budget.
        too_deep = b"[" * 66 + b"]" * 66
        with patch.object(
            provider_core_module.json,
            "loads",
            side_effect=AssertionError("recursive parser was reached"),
        ):
            with self.assertRaisesRegex(
                ProviderCoreError, "maximum JSON depth"
            ):
                provider_core_module._decode_exact_json(too_deep)
            # A scalar at child depth 65 is inadmissible even when the
            # 65-nested empty-container document above is valid.
            with self.assertRaisesRegex(
                ProviderCoreError, "maximum JSON depth"
            ):
                provider_core_module._decode_exact_json(
                    b"[" * 65 + b"0" + b"]" * 65
                )
        # A document exceeding CPython recursion is also rejected before
        # recursive parser invocation, even on retained/replayed bytes.
        much_deeper = b"[" * 4000 + b"]" * 4000
        with self.assertRaisesRegex(
            ProviderCoreError, "maximum JSON depth"
        ) as rejected:
            provider_core_module._decode_exact_json(much_deeper)
        self.assertIsNone(rejected.exception.__cause__)
        self.assertIsNone(rejected.exception.__context__)

    def test_json_decoder_recursion_error_is_redacted(self):
        marker = "AUTOTRADE_SYNTHETIC_UNTRUSTED_PARSER_MARKER"
        with patch.object(
            provider_core_module.json, "loads",
            side_effect=RecursionError(marker),
        ):
            with self.assertRaisesRegex(
                ProviderCoreError, "maximum JSON depth"
            ) as rejected:
                provider_core_module._decode_exact_json(b'{"safe":1}')
        self.assertNotIn(marker, str(rejected.exception))
        self.assertIsNone(rejected.exception.__cause__)
        self.assertIsNone(rejected.exception.__context__)

    def test_provider_raw_json_keeps_duplicate_and_nonfinite_fences(self):
        for invalid in (
            b'{"price":1.25,"price":1.50}',
            b'{"price":NaN}',
            b'{"price":Infinity}',
            b'{"price":-Infinity}',
            b'{"price":1e256,"price":2}',
        ):
            with self.subTest(raw=invalid):
                with self.assertRaises(ProviderCoreError):
                    provider_core_module._decode_exact_json(invalid)


    def test_provider_decode_diagnostics_do_not_retain_untrusted_raw_material(self):
        marker = "AUTOTRADE_SYNTHETIC_SECRET_MARKER_8c07"
        hostile = (
            ('{"' + marker + '":1,"' + marker + '":2}').encode("utf-8"),
            ('{"' + marker + '":"bad",').encode("utf-8"),
            ('{"' + marker + '":"bad-').encode("utf-8") + b"\xff" + b'"}',
        )
        for raw in hostile:
            with self.subTest(raw_prefix=raw[:20]):
                with self.assertRaises(ProviderCoreError) as caught:
                    provider_core_module._decode_exact_json(raw)
                current = caught.exception
                visited = set()
                while current is not None and id(current) not in visited:
                    visited.add(id(current))
                    self.assertNotIn(marker, str(current))
                    self.assertNotIn(marker, repr(current))
                    current = current.__cause__ or current.__context__
                self.assertIsNone(caught.exception.__cause__)
                self.assertIsNone(caught.exception.__context__)


    def _durable_submission_binding(
        self,
        directory,
        *,
        capability_snapshot_ids=("cap-1",),
        instrument_versions=("BTCUSD:v1",),
        raw=b'{ "orderId" : "provider-1" }',
    ):
        store = JournalStore(f"{directory}/journal.sqlite3")
        dispatcher = GuardedDispatcher(
            store,
            environment="SIMULATION",
            account_id="acct",
            owner_token="owner",
        )
        request = {"symbol": "BTCUSD", "qty": "1"}
        request_text = json.dumps(
            request,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        request_sha = "sha256:" + sha256(request_text.encode("utf-8")).hexdigest()
        scope = {
            "endpoint": "/v5/order/create",
            "prepared_request_sha256": request_sha,
            "capability_snapshot_ids": list(capability_snapshot_ids),
            "instrument_versions": list(instrument_versions),
        }

        def transport(_client_id, _request, guard):
            guard()
            return ExactJsonTransportResponse(raw)

        outcome = dispatcher.dispatch(
            attempt_id="provider-evidence-a1",
            intent_id="intent-1",
            intent_hash="intent-hash",
            provider="BYBIT",
            request=request,
            now="2026-09-24T18:00:00Z",
            authority_check=lambda _hash, _now: (True, "allowed"),
            transport_send=transport,
            submission_scope=scope,
        )
        self.assertEqual(outcome.status, "SENT")
        return (
            load_submission_response_binding(
                store,
                environment="SIMULATION",
                account_id="acct",
                attempt_id="provider-evidence-a1",
            ),
            request_sha,
        )

    def test_submission_observation_requires_durable_exact_send_scope(self):
        with TemporaryDirectory() as directory:
            binding, request_sha = self._durable_submission_binding(directory)
            observation = observe_submission_json_response(
                response_binding=binding,
                provider_id="BYBIT",
                endpoint="/v5/order/create",
                prepared_request_sha256=request_sha,
                capability_snapshot_ids=("cap-1",),
                instrument_versions=("BTCUSD:v1",),
            )
            self.assertIsInstance(observation, ProviderSubmissionObservation)
            self.assertEqual(observation.payload["orderId"], "provider-1")
            self.assertEqual(observation.response_sha256, binding.response_sha256)
            self.assertEqual(observation.observed_at, binding.sent_at)
            self.assertEqual(observation.request_sha256, request_sha)
            observation.require_scope(
                provider_id="BYBIT",
                endpoint="/v5/order/create",
                prepared_request_sha256=request_sha,
                capability_snapshot_ids=("cap-1",),
                instrument_versions=("BTCUSD:v1",),
                account_id="acct",
                environment="SIMULATION",
                client_order_id=binding.client_order_id,
            )


    def test_submission_observation_rejects_post_mint_scope_retargeting(self):
        with TemporaryDirectory() as directory:
            binding, request_sha = self._durable_submission_binding(directory)
            observation = observe_submission_json_response(
                response_binding=binding,
                provider_id="BYBIT",
                endpoint="/v5/order/create",
                prepared_request_sha256=request_sha,
                capability_snapshot_ids=("cap-1",),
                instrument_versions=("BTCUSD:v1",),
            )

            object.__setattr__(observation, "endpoint", "/v5/order/cancel")
            with self.assertRaisesRegex(
                ProviderCoreError,
                "provider submission observation authority is unavailable",
            ):
                observation.require_scope(
                    provider_id="BYBIT",
                    endpoint="/v5/order/cancel",
                    prepared_request_sha256=request_sha,
                    capability_snapshot_ids=("cap-1",),
                    instrument_versions=("BTCUSD:v1",),
                    account_id="acct",
                    environment="SIMULATION",
                    client_order_id=binding.client_order_id,
                )
            with self.assertRaisesRegex(
                ProviderCoreError,
                "provider submission observation authority is unavailable",
            ):
                _ = observation.payload

            post_scope = observe_submission_json_response(
                response_binding=binding,
                provider_id="BYBIT",
                endpoint="/v5/order/create",
                prepared_request_sha256=request_sha,
                capability_snapshot_ids=("cap-1",),
                instrument_versions=("BTCUSD:v1",),
            )
            post_scope.require_scope(
                provider_id="BYBIT",
                endpoint="/v5/order/create",
                prepared_request_sha256=request_sha,
                capability_snapshot_ids=("cap-1",),
                instrument_versions=("BTCUSD:v1",),
                account_id="acct",
                environment="SIMULATION",
                client_order_id=binding.client_order_id,
            )
            object.__setattr__(post_scope, "payload", {"orderId": "forged"})
            with self.assertRaisesRegex(
                ProviderCoreError,
                "provider submission observation authority is unavailable",
            ):
                _ = post_scope.payload

    def test_submission_observation_rejects_scope_method_rebinding(self):
        with TemporaryDirectory() as directory:
            binding, request_sha = self._durable_submission_binding(directory)
            observation = observe_submission_json_response(
                response_binding=binding,
                provider_id="BYBIT",
                endpoint="/v5/order/create",
                prepared_request_sha256=request_sha,
                capability_snapshot_ids=("cap-1",),
                instrument_versions=("BTCUSD:v1",),
            )

            with patch.object(
                ProviderSubmissionObservation,
                "require_scope",
                lambda *_args, **_kwargs: None,
            ):
                with self.assertRaisesRegex(
                    ProviderCoreError,
                    "provider submission observation authority is unavailable",
                ):
                    observation.require_scope(
                        provider_id="ALPACA",
                        endpoint="/v2/orders",
                        prepared_request_sha256=request_sha,
                        capability_snapshot_ids=("cap-1",),
                        instrument_versions=("BTCUSD:v1",),
                        account_id="acct",
                        environment="SIMULATION",
                        client_order_id=binding.client_order_id,
                    )
                with self.assertRaisesRegex(
                    ProviderCoreError,
                    "provider submission observation authority is unavailable",
                ):
                    _ = observation.payload

    def test_submission_observation_scope_barrier_rejects_attribute_lookup_rebinding(self):
        with TemporaryDirectory() as directory:
            binding, request_sha = self._durable_submission_binding(directory)
            observation = observe_submission_json_response(
                response_binding=binding,
                provider_id="BYBIT",
                endpoint="/v5/order/create",
                prepared_request_sha256=request_sha,
                capability_snapshot_ids=("cap-1",),
                instrument_versions=("BTCUSD:v1",),
            )

            with patch.object(
                ProviderSubmissionObservation,
                "__getattribute__",
                object.__getattribute__,
            ):
                with self.assertRaisesRegex(
                    ProviderCoreError,
                    "provider submission observation authority is unavailable",
                ):
                    observation.require_scope(
                        provider_id="BYBIT",
                        endpoint="/v5/order/create",
                        prepared_request_sha256=request_sha,
                        capability_snapshot_ids=("cap-1",),
                        instrument_versions=("BTCUSD:v1",),
                        account_id="acct",
                        environment="SIMULATION",
                        client_order_id=binding.client_order_id,
                    )

    def test_submission_observation_rejects_accessor_replacement_before_scope_use(self):
        with TemporaryDirectory() as directory:
            binding, request_sha = self._durable_submission_binding(directory)
            observation = observe_submission_json_response(
                response_binding=binding,
                provider_id="BYBIT",
                endpoint="/v5/order/create",
                prepared_request_sha256=request_sha,
                capability_snapshot_ids=("cap-1",),
                instrument_versions=("BTCUSD:v1",),
            )

            with patch.object(
                ProviderSubmissionObservation,
                "__getattribute__",
                object.__getattribute__,
            ):
                object.__setattr__(observation, "endpoint", "/v5/order/cancel")
                with self.assertRaisesRegex(
                    ProviderCoreError,
                    "provider submission observation authority is unavailable",
                ):
                    observation.require_scope(
                        provider_id="BYBIT",
                        endpoint="/v5/order/cancel",
                        prepared_request_sha256=request_sha,
                        capability_snapshot_ids=("cap-1",),
                        instrument_versions=("BTCUSD:v1",),
                        account_id="acct",
                        environment="SIMULATION",
                        client_order_id=binding.client_order_id,
                    )

    def test_submission_observation_rejects_runtime_binding_projection_rebinding(self):
        with TemporaryDirectory() as directory:
            binding, request_sha = self._durable_submission_binding(directory)
            with patch.object(
                provider_core_module,
                "submission_response_binding_projection",
                side_effect=lambda _binding: {},
            ):
                with self.assertRaisesRegex(
                    ProviderCoreError,
                    "provider submission observation authority is unavailable",
                ):
                    observe_submission_json_response(
                        response_binding=binding,
                        provider_id="BYBIT",
                        endpoint="/v5/order/create",
                        prepared_request_sha256=request_sha,
                        capability_snapshot_ids=("cap-1",),
                        instrument_versions=("BTCUSD:v1",),
                    )

    def test_submission_observation_rejects_scope_relabelling(self):
        with TemporaryDirectory() as directory:
            binding, request_sha = self._durable_submission_binding(directory)
            for kwargs in (
                {"provider_id": "ALPACA"},
                {"endpoint": "/other"},
                {"prepared_request_sha256": "sha256:" + "0" * 64},
                {"capability_snapshot_ids": ("cap-2",)},
                {"instrument_versions": ("ETHUSD:v1",)},
            ):
                values = {
                    "response_binding": binding,
                    "provider_id": "BYBIT",
                    "endpoint": "/v5/order/create",
                    "prepared_request_sha256": request_sha,
                    "capability_snapshot_ids": ("cap-1",),
                    "instrument_versions": ("BTCUSD:v1",),
                }
                values.update(kwargs)
                with self.subTest(kwargs=kwargs), self.assertRaises(ProviderCoreError):
                    observe_submission_json_response(**values)

    def test_submission_observation_cannot_be_constructed_directly(self):
        with TemporaryDirectory() as directory:
            binding, _request_sha = self._durable_submission_binding(directory)
            with self.assertRaisesRegex(
                ProviderCoreError,
                "durable exact response binding",
            ):
                ProviderSubmissionObservation(
                    response_binding=binding,
                    endpoint="/v5/order/create",
                    capability_snapshot_ids=("cap-1",),
                    instrument_versions=("BTCUSD:v1",),
                    evidence_ref="provider-write:sha256:" + "1" * 64,
                    payload={"orderId": "forged"},
                )

    def test_all_six_architectural_provider_targets_exist(self):
        self.assertEqual(
            set(PROVIDERS),
            {"BYBIT", "KRAKEN", "WHITEBIT", "BINANCE", "IBKR", "ALPACA"},
        )
        for provider in PROVIDERS.values():
            self.assertTrue(provider.product_families)
            self.assertTrue(provider.surfaces)

    def test_product_families_are_not_silently_interchangeable(self):
        self.assertIn("OPTIONS", provider_definition("bybit").product_families)
        self.assertIn("USD_M", provider_definition("binance").product_families)
        self.assertNotIn("USD_M", provider_definition("kraken").product_families)

    def test_qualification_is_exact_code_and_time_bound(self):
        evidence = QualificationEvidence(
            provider_id="BYBIT",
            product_family="SPOT",
            environment="TEST",
            adapter_code_sha=CODE_SHA,
            documentation_ref="official-docs-snapshot",
            observed_at=NOW,
            expires_at=NOW + timedelta(days=30),
            passed_cases=REQUIRED_QUALIFICATION_CASES,
        )
        self.assertEqual(
            evidence.status(now=NOW + timedelta(days=1), exact_code_sha=CODE_SHA),
            "QUALIFIED_FOR_NONLIVE",
        )
        self.assertEqual(
            evidence.status(now=NOW + timedelta(days=1), exact_code_sha=OTHER_SHA),
            "CODE_MISMATCH",
        )
        self.assertEqual(
            evidence.status(now=NOW + timedelta(days=31), exact_code_sha=CODE_SHA),
            "EXPIRED",
        )

    def test_qualification_rejects_human_labels_as_exact_code_sha(self):
        with self.assertRaisesRegex(ProviderCoreError, "canonical"):
            QualificationEvidence(
                provider_id="BYBIT",
                product_family="SPOT",
                environment="TEST",
                adapter_code_sha="abc123",
                documentation_ref="official-docs-snapshot",
                observed_at=NOW,
                expires_at=NOW + timedelta(days=1),
                passed_cases=REQUIRED_QUALIFICATION_CASES,
            )

    def test_live_evidence_cannot_be_mislabeled_as_nonlive_qualification(self):
        evidence = QualificationEvidence(
            provider_id="BYBIT",
            product_family="SPOT",
            environment=" live ",
            adapter_code_sha=CODE_SHA,
            documentation_ref="official-live-docs-snapshot",
            observed_at=NOW,
            expires_at=NOW + timedelta(days=1),
            passed_cases=REQUIRED_QUALIFICATION_CASES,
        )
        self.assertEqual(evidence.environment, "LIVE")
        self.assertEqual(
            evidence.status(now=NOW, exact_code_sha=CODE_SHA),
            "LIVE_REQUIRES_BOUNDED_REAL",
        )

    def test_incomplete_qualification_fails_closed(self):
        evidence = QualificationEvidence(
            provider_id="ALPACA",
            product_family="EQUITIES",
            environment="PAPER",
            adapter_code_sha=CODE_SHA,
            documentation_ref="docs",
            observed_at=NOW,
            expires_at=NOW + timedelta(days=1),
            passed_cases=frozenset({"metadata", "authentication"}),
        )
        self.assertEqual(
            evidence.status(now=NOW, exact_code_sha=CODE_SHA),
            "INCOMPLETE",
        )

    def test_research_cannot_consume_recovery_quota(self):
        bucket = QuotaBucket(capacity=Decimal("100"), recovery_reserve=Decimal("20"))
        bucket.acquire("70", purpose="RESEARCH")
        with self.assertRaisesRegex(ProviderCoreError, "reserve"):
            bucket.acquire("15", purpose="RESEARCH")
        bucket.acquire("20", purpose="RECOVERY")
        self.assertEqual(bucket.available(), Decimal("10"))

    def test_quota_purpose_rejects_polymorphic_recovery_impersonation(self):
        callbacks = []

        class Impostor(str):
            def __hash__(self):
                callbacks.append("hash")
                return hash("RECOVERY")

            def __eq__(self, other):
                callbacks.append("eq")
                return True

            def __ne__(self, other):
                callbacks.append("ne")
                return False

        bucket = QuotaBucket(capacity="1", recovery_reserve="0.25")
        for purpose in (Impostor("TRADING"), object()):
            with self.subTest(purpose_type=type(purpose).__name__):
                with self.assertRaisesRegex(
                    ProviderCoreError, "unknown quota purpose"
                ):
                    bucket.acquire("0.9", purpose=purpose)
                self.assertEqual(callbacks, [])
                self.assertEqual(bucket.used, Decimal("0"))
                self.assertEqual(bucket.available(), Decimal("1"))
        with self.assertRaisesRegex(
            ProviderCoreError, "recovery quota reserve"
        ):
            bucket.acquire("0.9", purpose="TRADING")
        self.assertEqual(bucket.used, Decimal("0"))
        bucket.acquire("0.9", purpose="RECOVERY")
        self.assertEqual(bucket.used, Decimal("0.9"))

    def test_quota_recovery_reserve_is_context_independent_and_exact(self):
        tiny = Decimal("1e-30")
        capacity = Decimal("1.000000000000000000000000000001")
        for rounding in (ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN):
            with self.subTest(rounding=rounding), localcontext() as context:
                context.prec = 6
                context.rounding = rounding
                bucket = QuotaBucket(
                    capacity=capacity, recovery_reserve=tiny
                )
                bucket.acquire("1", purpose="TRADING")
                self.assertEqual(bucket.used, Decimal("1"))
                self.assertEqual(bucket.available(), tiny)
                # No ambient rounding may erase the tiny recovery reserve.
                with self.assertRaisesRegex(ProviderCoreError, "reserve"):
                    bucket.acquire(tiny, purpose="RESEARCH")
                self.assertEqual(bucket.used, Decimal("1"))
                bucket.acquire(tiny, purpose="RECOVERY")
                self.assertEqual(bucket.used, capacity)
                self.assertEqual(bucket.available(), Decimal("0"))
                bucket.release(tiny)
                self.assertEqual(bucket.used, Decimal("1"))
                self.assertEqual(bucket.available(), tiny)

    def test_quota_insufficient_capacity_fails_without_partial_mutation(self):
        with localcontext() as context:
            context.prec = 6
            context.rounding = ROUND_FLOOR
            bucket = QuotaBucket(capacity="1", recovery_reserve="1e-30")
            with self.assertRaisesRegex(ProviderCoreError, "reserve"):
                bucket.acquire("1", purpose="TRADING")
            self.assertEqual(bucket.used, Decimal("0"))
            with self.assertRaisesRegex(ProviderCoreError, "exhausted"):
                bucket.acquire("2", purpose="RECOVERY")
            self.assertEqual(bucket.used, Decimal("0"))

    def test_clock_skew_blocks_authenticated_send(self):
        guard = ClockGuard(timedelta(seconds=2))
        guard.require_safe(host_time=NOW, provider_time=NOW + timedelta(seconds=2))
        with self.assertRaisesRegex(ProviderCoreError, "clock skew"):
            guard.require_safe(host_time=NOW, provider_time=NOW + timedelta(seconds=3))

    def test_ambiguous_write_is_never_blindly_retried(self):
        unknown = classify_write_outcome(
            transport_started=True,
            provider_acknowledged=False,
            provider_rejected=False,
        )
        self.assertEqual(unknown.status, "UNKNOWN")
        self.assertFalse(unknown.retry_same_economic_action)
        self.assertTrue(unknown.reconciliation_required)

    def test_direct_write_outcome_cannot_bypass_send_state_invariants(self):
        invalid_cases = (
            ("UNKNOWN", True, True),
            ("UNKNOWN", False, False),
            ("NOT_SENT", False, False),
            ("ACKNOWLEDGED", True, False),
            ("REJECTED", False, True),
        )
        for status, retry, reconcile in invalid_cases:
            with self.subTest(status=status), self.assertRaisesRegex(
                ProviderCoreError,
                "send-state invariant",
            ):
                WriteOutcome(status, retry, reconcile)

        with self.assertRaisesRegex(ProviderCoreError, "boolean"):
            WriteOutcome("UNKNOWN", 0, True)

    def test_adapter_code_identity_rejects_noncanonical_whitespace(self):
        with self.assertRaisesRegex(ProviderCoreError, "canonical"):
            QualificationEvidence(
                provider_id="BYBIT",
                product_family="SPOT",
                environment="TEST",
                adapter_code_sha=" " + CODE_SHA,
                documentation_ref="docs",
                observed_at=NOW,
                expires_at=NOW + timedelta(days=1),
                passed_cases=REQUIRED_QUALIFICATION_CASES,
            )

        evidence = QualificationEvidence(
            provider_id="BYBIT",
            product_family="SPOT",
            environment="TEST",
            adapter_code_sha=CODE_SHA,
            documentation_ref="docs",
            observed_at=NOW,
            expires_at=NOW + timedelta(days=1),
            passed_cases=REQUIRED_QUALIFICATION_CASES,
        )
        with self.assertRaisesRegex(ProviderCoreError, "canonical"):
            evidence.status(
                now=NOW + timedelta(hours=1),
                exact_code_sha=CODE_SHA + " ",
            )

    def test_timezone_object_without_utc_offset_fails_closed(self):
        invalid = datetime(2026, 9, 24, 18, tzinfo=_NoOffsetTZ())
        with self.assertRaisesRegex(ProviderCoreError, "timezone-aware"):
            QualificationEvidence(
                provider_id="BYBIT",
                product_family="SPOT",
                environment="TEST",
                adapter_code_sha=CODE_SHA,
                documentation_ref="docs",
                observed_at=invalid,
                expires_at=NOW + timedelta(days=1),
                passed_cases=REQUIRED_QUALIFICATION_CASES,
            )

    def test_only_never_sent_write_is_retryable_as_same_action(self):
        outcome = classify_write_outcome(
            transport_started=False,
            provider_acknowledged=False,
            provider_rejected=False,
        )
        self.assertEqual(outcome.status, "NOT_SENT")
        self.assertTrue(outcome.retry_same_economic_action)
        self.assertFalse(outcome.reconciliation_required)

    def test_invalid_float_quota_is_rejected(self):
        with self.assertRaises(ProviderCoreError):
            QuotaBucket(capacity=100.0, recovery_reserve=10)


if __name__ == "__main__":
    unittest.main()
