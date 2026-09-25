from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

from mvp.autotrade_mvp.bybit_v5 import (
    build_order_payload,
    prepare_order_submission,
    coverage_evidence,
    parse_executions,
    parse_submission_response,
    server_time_from_response,
    validate_auth_timestamp,
)
from mvp.autotrade_mvp.capabilities import (
    CapabilityClaim,
    EvidenceVerification,
    derive_capability_snapshot,
)
from mvp.autotrade_mvp.dispatch import (
    ExactJsonTransportResponse,
    GuardedDispatcher,
    load_submission_response_binding,
    stable_client_order_id,
)
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.provider_core import (
    ProviderCoreError,
    Surface,
    observe_authenticated_json_response,
    observe_submission_json_response,
    prepare_authenticated_read_query,
)


READ_AT = datetime(2026, 9, 24, 20, tzinfo=timezone.utc)


def read_capability(*, account_id="paper-1", environment="PAPER", instrument_version="BTCUSDT@v1"):
    observed_at = READ_AT - timedelta(hours=1)
    claims = tuple(
        CapabilityClaim(
            source=source,
            provider_id="BYBIT",
            account_id=account_id,
            entity_id="bybit-reconciliation",
            environment=environment,
            instrument_version=instrument_version,
            observed_at=observed_at,
            expires_at=READ_AT + timedelta(hours=1),
            supported_order_types=frozenset({"LIMIT", "MARKET"}),
            time_in_force=frozenset({"GTC", "IOC"}),
            permission_scopes=frozenset({"ORDER.READ"}),
            position_mode="NET",
            native_protection=frozenset(),
            rate_limit_policy_id="bybit-read-test",
            data_entitlements=frozenset({"EXECUTIONS"}),
            evidence_ref={
                "artifact_id": str(uuid4()),
                "sha256": "sha256:" + "a" * 64,
                "observed_at": observed_at.isoformat().replace("+00:00", "Z"),
                "source_uri": "https://bybit-exchange.github.io/docs/v5/order/execution",
            },
        )
        for source in ("DOCUMENTED", "API", "ACCOUNT", "INSTRUMENT")
    )
    return derive_capability_snapshot(
        snapshot_id=str(uuid4()),
        claims=claims,
        observed_at=READ_AT,
        evidence_verifier=lambda _claim: EvidenceVerification(valid=True),
    )



def write_capability(
    *,
    family="LINEAR_DERIVATIVES",
    position_mode="HEDGE",
    account_id="bybit-account",
    environment="PAPER",
    instrument_version="BTCUSDT@1",
    expires_at=None,
    permission_scope=None,
):
    scope = permission_scope or {
        "LINEAR_DERIVATIVES": "BYBIT.LINEAR.ORDER.WRITE",
        "INVERSE_DERIVATIVES": "BYBIT.INVERSE.ORDER.WRITE",
    }[family]
    observed_at = READ_AT - timedelta(minutes=1)
    claims = tuple(
        CapabilityClaim(
            source=source,
            provider_id="BYBIT",
            account_id=account_id,
            entity_id="bybit-unified-account",
            environment=environment,
            instrument_version=instrument_version,
            observed_at=observed_at,
            expires_at=expires_at or READ_AT + timedelta(minutes=5),
            supported_order_types=frozenset({"LIMIT", "MARKET"}),
            time_in_force=frozenset({"GTC", "IOC"}),
            permission_scopes=frozenset({scope}),
            position_mode=position_mode,
            native_protection=frozenset(),
            rate_limit_policy_id="bybit-v5-write-test",
            data_entitlements=frozenset(),
            evidence_ref={
                "artifact_id": str(uuid4()),
                "sha256": "sha256:" + "b" * 64,
                "observed_at": observed_at.isoformat().replace("+00:00", "Z"),
                "source_uri": "https://bybit-exchange.github.io/docs/v5/order/create-order",
            },
        )
        for source in ("DOCUMENTED", "API", "ACCOUNT", "INSTRUMENT")
    )
    return derive_capability_snapshot(
        snapshot_id=str(uuid4()),
        claims=claims,
        observed_at=READ_AT,
        evidence_verifier=lambda _claim: EvidenceVerification(valid=True),
    )

def submission_write_capability(
    *,
    account_id="bybit-account",
    environment="LIVE",
    instrument_version="BTCUSDT@v1",
):
    observed_at = READ_AT - timedelta(hours=1)
    claims = tuple(
        CapabilityClaim(
            source=source,
            provider_id="BYBIT",
            account_id=account_id,
            entity_id="bybit-order",
            environment=environment,
            instrument_version=instrument_version,
            observed_at=observed_at,
            expires_at=READ_AT + timedelta(hours=1),
            supported_order_types=frozenset({"LIMIT", "MARKET"}),
            time_in_force=frozenset({"GTC", "IOC", "FOK", "POST_ONLY"}),
            permission_scopes=frozenset({"ORDER_WRITE"}),
            position_mode="NET",
            native_protection=frozenset(),
            rate_limit_policy_id="bybit-write-test",
            data_entitlements=frozenset({"ORDERS"}),
            evidence_ref={
                "artifact_id": str(uuid4()),
                "sha256": "sha256:" + "c" * 64,
                "observed_at": observed_at.isoformat().replace("+00:00", "Z"),
                "source_uri": "https://bybit-exchange.github.io/docs/v5/order/create-order",
            },
        )
        for source in ("DOCUMENTED", "API", "ACCOUNT", "INSTRUMENT")
    )
    return derive_capability_snapshot(
        snapshot_id=str(uuid4()),
        claims=claims,
        observed_at=READ_AT,
        evidence_verifier=lambda _claim: EvidenceVerification(valid=True),
    )


def bound_execution_response(
    response,
    *,
    account_id="paper-1",
    environment="PAPER",
    instrument_version="BTCUSDT@v1",
):
    query = prepare_authenticated_read_query(
        capability=read_capability(
            account_id=account_id,
            environment=environment,
            instrument_version=instrument_version,
        ),
        surface=Surface.AUTHENTICATED_READ,
        endpoint="/v5/execution/list",
        query={"category": "spot", "limit": "100"},
        at=READ_AT,
        permission_scope="ORDER.READ",
    )
    raw = json.dumps(
        response,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return observe_authenticated_json_response(
        query_binding=query,
        response_bytes=raw,
        observed_at=READ_AT,
    )


class BybitV5AdapterTests(unittest.TestCase):
    def test_spot_market_quantity_is_explicitly_base_coin(self):
        payload = build_order_payload(
            product_family="SPOT",
            symbol="BTCUSDT",
            side="BUY",
            order_type="MARKET",
            quantity="0.0100",
            client_order_id="order_1",
            time_in_force="IOC",
        )
        self.assertEqual(payload["category"], "spot")
        self.assertEqual(payload["qty"], "0.01")
        self.assertEqual(payload["marketUnit"], "baseCoin")
        self.assertEqual(payload["isLeverage"], 0)
        self.assertNotIn("price", payload)

    def test_margin_is_explicit_and_limit_price_is_exact(self):
        payload = build_order_payload(
            product_family="MARGIN",
            symbol="ETHUSDT",
            side="SELL",
            order_type="LIMIT",
            quantity=Decimal("2.500"),
            price="3456.7000",
            client_order_id="margin-1",
            time_in_force="POST_ONLY",
        )
        self.assertEqual(payload["isLeverage"], 1)
        self.assertEqual(payload["qty"], "2.5")
        self.assertEqual(payload["price"], "3456.7")
        self.assertEqual(payload["timeInForce"], "PostOnly")

    def test_derivative_scope_maps_reduce_only_and_verified_hedge_mode(self):
        payload = build_order_payload(
            product_family="LINEAR_DERIVATIVES",
            symbol="BTCUSDT",
            side="SELL",
            order_type="LIMIT",
            quantity="1",
            price="70000",
            client_order_id="reduce-1",
            time_in_force="GTC",
            reduce_only=True,
            position_side="LONG",
            capability=write_capability(position_mode="HEDGE"),
            capability_at=READ_AT,
            account_id="bybit-account",
            instrument_version="BTCUSDT@1",
            provider_environment="DEMO",
        )
        self.assertEqual(payload["category"], "linear")
        self.assertTrue(payload["reduceOnly"])
        self.assertEqual(payload["positionIdx"], 1)

    def test_derivative_order_requires_verified_capability_context(self):
        with self.assertRaisesRegex(ProviderCoreError, "capability context"):
            build_order_payload(
                product_family="LINEAR_DERIVATIVES",
                symbol="BTCUSDT",
                side="BUY",
                order_type="LIMIT",
                quantity="1",
                price="70000",
                client_order_id="mode-required",
                time_in_force="GTC",
            )

        one_way = build_order_payload(
            product_family="INVERSE_DERIVATIVES",
            symbol="BTCUSD",
            side="BUY",
            order_type="LIMIT",
            quantity="1",
            price="70000",
            client_order_id="mode-one-way",
            time_in_force="GTC",
            capability=write_capability(
                family="INVERSE_DERIVATIVES",
                position_mode="ONE_WAY",
                instrument_version="BTCUSD@1",
            ),
            capability_at=READ_AT,
            account_id="bybit-account",
            instrument_version="BTCUSD@1",
            provider_environment="DEMO",
        )
        self.assertEqual(one_way["positionIdx"], 0)

    def test_derivative_position_index_cannot_conflict_with_verified_mode(self):
        with self.assertRaisesRegex(ProviderCoreError, "conflicts"):
            build_order_payload(
                product_family="LINEAR_DERIVATIVES",
                symbol="BTCUSDT",
                side="SELL",
                order_type="LIMIT",
                quantity="1",
                price="70000",
                client_order_id="wrong-index",
                time_in_force="GTC",
                position_side="SHORT",
                position_idx=1,
                capability=write_capability(position_mode="HEDGE"),
                capability_at=READ_AT,
                account_id="bybit-account",
                instrument_version="BTCUSDT@1",
                provider_environment="DEMO",
            )

    def test_derivative_capability_is_bound_to_scope_identity_and_time(self):
        common = dict(
            product_family="LINEAR_DERIVATIVES",
            symbol="BTCUSDT",
            side="BUY",
            order_type="LIMIT",
            quantity="1",
            price="70000",
            time_in_force="GTC",
            capability_at=READ_AT,
            instrument_version="BTCUSDT@1",
            provider_environment="DEMO",
            position_side="LONG",
        )
        with self.assertRaisesRegex(ProviderCoreError, "account"):
            build_order_payload(
                **common,
                client_order_id="wrong-account",
                capability=write_capability(),
                account_id="other-account",
            )
        with self.assertRaisesRegex(ProviderCoreError, "instrument"):
            build_order_payload(
                **{**common, "instrument_version": "ETHUSDT@1"},
                client_order_id="wrong-instrument",
                capability=write_capability(),
                account_id="bybit-account",
            )
        with self.assertRaisesRegex(ProviderCoreError, "environment"):
            build_order_payload(
                **{**common, "provider_environment": "MAINNET"},
                client_order_id="wrong-environment",
                capability=write_capability(),
                account_id="bybit-account",
            )
        with self.assertRaisesRegex(ProviderCoreError, "not admitted"):
            build_order_payload(
                **common,
                client_order_id="wrong-category-scope",
                capability=write_capability(
                    permission_scope="BYBIT.INVERSE.ORDER.WRITE"
                ),
                account_id="bybit-account",
            )
        with self.assertRaisesRegex(ProviderCoreError, "not admitted"):
            build_order_payload(
                **common,
                client_order_id="expired-mode",
                capability=write_capability(
                    expires_at=READ_AT - timedelta(seconds=1)
                ),
                account_id="bybit-account",
            )

    def test_hedge_mode_target_leg_drives_open_and_reduce_only_index(self):
        capability = write_capability(position_mode="HEDGE")
        cases = (
            ("BUY", False, "LONG", 1),
            ("SELL", False, "SHORT", 2),
            ("SELL", True, "LONG", 1),
            ("BUY", True, "SHORT", 2),
        )
        for side, reduce_only, position_side, expected in cases:
            with self.subTest(
                side=side,
                reduce_only=reduce_only,
                position_side=position_side,
            ):
                payload = build_order_payload(
                    product_family="LINEAR_DERIVATIVES",
                    symbol="BTCUSDT",
                    side=side,
                    order_type="LIMIT",
                    quantity="1",
                    price="70000",
                    client_order_id=f"hedge-{side.lower()}-{position_side.lower()}",
                    time_in_force="GTC",
                    reduce_only=reduce_only,
                    position_side=position_side,
                    capability=capability,
                    capability_at=READ_AT,
                    account_id="bybit-account",
                    instrument_version="BTCUSDT@1",
                    provider_environment="DEMO",
                )
                self.assertEqual(payload["positionIdx"], expected)

    def test_hedge_mode_requires_explicit_compatible_target_leg(self):
        capability = write_capability(position_mode="HEDGE")
        common = dict(
            product_family="LINEAR_DERIVATIVES",
            symbol="BTCUSDT",
            order_type="LIMIT",
            quantity="1",
            price="70000",
            time_in_force="GTC",
            capability=capability,
            capability_at=READ_AT,
            account_id="bybit-account",
            instrument_version="BTCUSDT@1",
            provider_environment="DEMO",
        )
        with self.assertRaisesRegex(ProviderCoreError, "target position_side"):
            build_order_payload(
                **common,
                side="BUY",
                client_order_id="missing-leg",
            )
        with self.assertRaisesRegex(ProviderCoreError, "inconsistent"):
            build_order_payload(
                **common,
                side="SELL",
                position_side="LONG",
                client_order_id="open-long-wrong-side",
            )
        with self.assertRaisesRegex(ProviderCoreError, "inconsistent"):
            build_order_payload(
                **common,
                side="BUY",
                reduce_only=True,
                position_side="LONG",
                client_order_id="reduce-long-wrong-side",
            )

    def test_unknown_provider_position_mode_fails_closed(self):
        with self.assertRaisesRegex(ProviderCoreError, "ONE_WAY or HEDGE"):
            build_order_payload(
                product_family="LINEAR_DERIVATIVES",
                symbol="BTCUSDT",
                side="BUY",
                order_type="LIMIT",
                quantity="1",
                price="70000",
                client_order_id="bad-mode",
                time_in_force="GTC",
                position_side="LONG",
                capability=write_capability(position_mode="NET"),
                capability_at=READ_AT,
                account_id="bybit-account",
                instrument_version="BTCUSDT@1",
                provider_environment="DEMO",
            )

    def test_unsafe_or_ambiguous_request_shapes_fail_closed(self):
        common = dict(
            product_family="SPOT",
            symbol="BTCUSDT",
            side="BUY",
            client_order_id="safe-id",
        )
        with self.assertRaisesRegex(ProviderCoreError, "IOC"):
            build_order_payload(
                **common,
                order_type="MARKET",
                quantity="1",
                time_in_force="GTC",
            )
        with self.assertRaisesRegex(ProviderCoreError, "ignored"):
            build_order_payload(
                **common,
                order_type="MARKET",
                quantity="1",
                price="1",
                time_in_force="IOC",
            )
        with self.assertRaisesRegex(ProviderCoreError, "exact decimal"):
            build_order_payload(
                **common,
                order_type="LIMIT",
                quantity=0.1,
                price="1",
                time_in_force="GTC",
            )
        with self.assertRaisesRegex(ProviderCoreError, "1-36"):
            build_order_payload(
                **{**common, "client_order_id": "bad id"},
                order_type="LIMIT",
                quantity="1",
                price="1",
                time_in_force="GTC",
            )

    def _durable_write_observation(
        self,
        response,
        *,
        provider_environment="MAINNET",
        intent_id="bybit-write-intent",
        attempt_id=None,
    ):
        runtime_environment = (
            "LIVE" if provider_environment == "MAINNET" else "PAPER"
        )
        account_id = "bybit-account"
        attempt = attempt_id or str(uuid4())
        client_id = stable_client_order_id(
            "BYBIT",
            intent_id,
            environment=runtime_environment,
            account_id=account_id,
        )
        capability = submission_write_capability(
            account_id=account_id,
            environment=runtime_environment,
        )
        prepared = prepare_order_submission(
            capability=capability,
            at=READ_AT,
            provider_environment=provider_environment,
            product_family="SPOT",
            symbol="BTCUSDT",
            side="BUY",
            order_type="MARKET",
            quantity="0.01",
            client_order_id=client_id,
            time_in_force="IOC",
        )
        payload = json.loads(json.dumps(response))
        result = payload.get("result")
        if isinstance(result, dict) and result.get("orderLinkId") == "__CLIENT__":
            result["orderLinkId"] = client_id
        raw = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            dispatcher = GuardedDispatcher(
                store,
                environment=runtime_environment,
                account_id=account_id,
                owner_token="owner",
            )
            outcome = dispatcher.dispatch(
                attempt_id=attempt,
                intent_id=intent_id,
                intent_hash="bybit-intent-hash",
                provider="BYBIT",
                request=prepared.body,
                now="2026-09-24T20:00:00Z",
                authority_check=lambda _hash, _now: (True, "allowed"),
                transport_send=lambda _cid, _request, guard: (
                    guard(),
                    ExactJsonTransportResponse(raw),
                )[1],
                sender_check=lambda _owner, _epoch: None,
                submission_scope={
                    "endpoint": prepared.endpoint,
                    "prepared_request_sha256": prepared.body_sha256,
                    "capability_snapshot_ids": list(
                        prepared.capability_snapshot_ids
                    ),
                    "instrument_versions": list(prepared.instrument_versions),
                },
            )
            self.assertEqual(outcome.status, "SENT")
            binding = load_submission_response_binding(
                store,
                environment=runtime_environment,
                account_id=account_id,
                attempt_id=attempt,
            )
            observation = observe_submission_json_response(
                response_binding=binding,
                provider_id="BYBIT",
                endpoint=prepared.endpoint,
                prepared_request_sha256=prepared.body_sha256,
                capability_snapshot_ids=prepared.capability_snapshot_ids,
                instrument_versions=prepared.instrument_versions,
            )
        return attempt, prepared, observation

    def test_success_response_is_acknowledgement_not_fill(self):
        attempt, prepared, observation = self._durable_write_observation(
            {
                "retCode": 0,
                "retMsg": "OK",
                "result": {
                    "orderId": "provider-123",
                    "orderLinkId": "__CLIENT__",
                },
                "retExtInfo": {},
                "time": 1790280000123,
            }
        )
        result = parse_submission_response(
            attempt_id=attempt,
            prepared_request=prepared,
            observation=observation,
        )
        self.assertEqual(result["outcome"], "ACKNOWLEDGED")
        self.assertEqual(result["retry_disposition"], "NEVER")
        self.assertEqual(result["provider_order_id"], "provider-123")
        self.assertNotIn("fill", repr(result).lower())
        self.assertEqual(
            result["evidence"][0]["sha256"],
            observation.response_sha256,
        )

    def test_response_evidence_is_bound_to_provider_environment(self):
        base = {
            "retCode": 0,
            "retMsg": "OK",
            "result": {
                "orderId": "provider-env",
                "orderLinkId": "__CLIENT__",
            },
            "time": 1790280000123,
        }
        expected = {
            "MAINNET": "https://api.bybit.com/v5/order/create",
            "TESTNET": "https://api-testnet.bybit.com/v5/order/create",
            "DEMO": "https://api-demo.bybit.com/v5/order/create",
        }
        for provider_environment, source_uri in expected.items():
            with self.subTest(provider_environment=provider_environment):
                attempt, prepared, observation = self._durable_write_observation(
                    base,
                    provider_environment=provider_environment,
                    intent_id=f"bybit-{provider_environment.lower()}",
                )
                result = parse_submission_response(
                    attempt_id=attempt,
                    prepared_request=prepared,
                    observation=observation,
                )
                self.assertEqual(
                    result["evidence"][0]["source_uri"],
                    source_uri,
                )

    def test_journal_observation_time_and_provider_time_are_distinct(self):
        attempt, prepared, observation = self._durable_write_observation(
            {
                "retCode": 0,
                "retMsg": "OK",
                "result": {
                    "orderId": "provider-time",
                    "orderLinkId": "__CLIENT__",
                },
                "time": 1790280000123,
            }
        )
        result = parse_submission_response(
            attempt_id=attempt,
            prepared_request=prepared,
            observation=observation,
        )
        self.assertEqual(
            result["provider_received_at"],
            "2026-09-24T20:00:00.123Z",
        )
        self.assertEqual(
            result["evidence"][0]["observed_at"],
            "2026-09-24T20:00:00Z",
        )

    def test_transport_loss_after_possible_write_is_unknown(self):
        client_id = stable_client_order_id(
            "BYBIT",
            "bybit-unknown",
            environment="LIVE",
            account_id="bybit-account",
        )
        prepared = prepare_order_submission(
            capability=submission_write_capability(),
            at=READ_AT,
            provider_environment="MAINNET",
            product_family="SPOT",
            symbol="BTCUSDT",
            side="BUY",
            order_type="MARKET",
            quantity="0.01",
            client_order_id=client_id,
            time_in_force="IOC",
        )
        result = parse_submission_response(
            attempt_id=str(uuid4()),
            prepared_request=prepared,
            observation=None,
            transport_ambiguous=True,
        )
        self.assertEqual(result["outcome"], "UNKNOWN")
        self.assertEqual(result["retry_disposition"], "RECONCILE_FIRST")
        self.assertEqual(result["reason_code"], "BYBIT_TRANSPORT_AMBIGUOUS")
        self.assertEqual(result["evidence"], [])

    def test_transport_ambiguity_requires_boolean_flag(self):
        client_id = stable_client_order_id(
            "BYBIT",
            "bybit-ambiguous-bool",
            environment="LIVE",
            account_id="bybit-account",
        )
        prepared = prepare_order_submission(
            capability=submission_write_capability(),
            at=READ_AT,
            provider_environment="MAINNET",
            product_family="SPOT",
            symbol="BTCUSDT",
            side="BUY",
            order_type="MARKET",
            quantity="0.01",
            client_order_id=client_id,
            time_in_force="IOC",
        )
        with self.assertRaisesRegex(ProviderCoreError, "must be boolean"):
            parse_submission_response(
                attempt_id=str(uuid4()),
                prepared_request=prepared,
                observation=None,
                transport_ambiguous=1,
            )

    def test_transport_ambiguity_cannot_coexist_with_provider_response(self):
        attempt, prepared, observation = self._durable_write_observation(
            {
                "retCode": 0,
                "result": {
                    "orderId": "provider-contradiction",
                    "orderLinkId": "__CLIENT__",
                },
            }
        )
        with self.assertRaisesRegex(ProviderCoreError, "authoritative response"):
            parse_submission_response(
                attempt_id=attempt,
                prepared_request=prepared,
                observation=observation,
                transport_ambiguous=True,
            )

    def test_missing_exact_observation_is_not_authoritative(self):
        client_id = stable_client_order_id(
            "BYBIT",
            "bybit-missing",
            environment="LIVE",
            account_id="bybit-account",
        )
        prepared = prepare_order_submission(
            capability=submission_write_capability(),
            at=READ_AT,
            provider_environment="MAINNET",
            product_family="SPOT",
            symbol="BTCUSDT",
            side="BUY",
            order_type="MARKET",
            quantity="0.01",
            client_order_id=client_id,
            time_in_force="IOC",
        )
        for invalid in (None, {"retCode": 0}):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(
                TypeError,
                "durable ProviderSubmissionObservation",
            ):
                parse_submission_response(
                    attempt_id=str(uuid4()),
                    prepared_request=prepared,
                    observation=invalid,
                )

    def test_ambiguous_bybit_codes_require_reconciliation(self):
        for code in (429, 10000, 10014, 10016):
            with self.subTest(code=code):
                attempt, prepared, observation = self._durable_write_observation(
                    {
                        "retCode": code,
                        "retMsg": "ambiguous",
                        "result": {},
                        "retExtInfo": {},
                        "time": 1790280000123,
                    },
                    intent_id=f"bybit-ambiguous-{code}",
                )
                result = parse_submission_response(
                    attempt_id=attempt,
                    prepared_request=prepared,
                    observation=observation,
                )
                self.assertEqual(result["outcome"], "UNKNOWN")
                self.assertEqual(
                    result["retry_disposition"],
                    "RECONCILE_FIRST",
                )
                self.assertEqual(
                    result["evidence"][0]["sha256"],
                    observation.response_sha256,
                )

    def test_explicit_parameter_error_is_rejected(self):
        attempt, prepared, observation = self._durable_write_observation(
            {
                "retCode": 10001,
                "retMsg": "parameter error",
                "result": {},
                "retExtInfo": {},
                "time": 1790280000123,
            }
        )
        result = parse_submission_response(
            attempt_id=attempt,
            prepared_request=prepared,
            observation=observation,
        )
        self.assertEqual(result["outcome"], "REJECTED")
        self.assertEqual(result["retry_disposition"], "NEVER")

    def test_success_response_must_echo_exact_client_identity(self):
        attempt, prepared, observation = self._durable_write_observation(
            {
                "retCode": 0,
                "retMsg": "OK",
                "result": {
                    "orderId": "provider-1",
                    "orderLinkId": "other",
                },
                "time": 1790280000123,
            }
        )
        with self.assertRaisesRegex(ProviderCoreError, "does not match"):
            parse_submission_response(
                attempt_id=attempt,
                prepared_request=prepared,
                observation=observation,
            )

    def test_execution_rows_preserve_provider_identity_and_exact_economics(self):
        response = {
            "retCode": 0,
            "retMsg": "OK",
            "result": {"list": [
                {
                    "execId": "exec-1", "orderLinkId": "client-1", "symbol": "BTCUSDT",
                    "execQty": "0.25", "execPrice": "65000.10", "execFee": "1.23",
                    "feeCurrency": "USDT", "execTime": "1790280000123",
                },
                {
                    "execId": "exec-1", "orderLinkId": "client-1", "symbol": "BTCUSDT",
                    "execQty": "0.25", "execPrice": "65000.10", "execFee": "1.23",
                    "feeCurrency": "USDT", "execTime": "1790280000123",
                },
            ]},
            "time": 1790280001000,
        }
        fills = parse_executions(
            bound_execution_response(response),
            instrument_versions={"BTCUSDT": "BTCUSDT@v1"},
        )
        self.assertEqual(len(fills), 1)
        fill = fills[0]
        self.assertEqual((fill.account_id, fill.environment), ("paper-1", "PAPER"))
        self.assertEqual(fill.provider_execution_id, "exec-1")
        self.assertEqual(fill.instrument, "BTCUSDT@v1")
        self.assertEqual(fill.quantity, Decimal("0.25"))
        self.assertEqual(fill.price, Decimal("65000.10"))
        self.assertEqual(fill.fee_amount, Decimal("1.23"))
        self.assertEqual(fill.trade_time, "2026-09-24T20:00:00.123Z")

    def test_execution_scope_is_derived_from_prepared_read_not_parser_labels(self):
        response = {"retCode": 0, "result": {"list": [{
            "execId": "scope-1", "orderLinkId": "", "symbol": "BTCUSDT",
            "execQty": "1", "execPrice": "10", "execFee": "0",
            "feeCurrency": "USDT", "execTime": "1790280000000",
        }]}}
        observation = bound_execution_response(response, account_id="account-a")
        fills = parse_executions(
            observation,
            instrument_versions={"BTCUSDT": "BTCUSDT@v1"},
        )
        self.assertEqual((fills[0].account_id, fills[0].environment), ("account-a", "PAPER"))
        self.assertEqual(observation.query_binding.account_id, "account-a")

    def test_documented_linear_execution_requires_qualified_fee_currency(self):
        response = {"retCode": 0, "result": {"category": "linear", "list": [{
            "execId": "e0cbe81d-0f18-5866-9415-cf319b5dab3b", "orderLinkId": "",
            "symbol": "ETHPERP", "execQty": "0.1", "execPrice": "1190.15",
            "execFee": "0.071409", "feeCurrency": "", "extraFees": "",
            "execTime": "1672282722429",
        }]}}
        evidence = bound_execution_response(response, instrument_version="ETHPERP@v1")
        with self.assertRaisesRegex(ProviderCoreError, "fee currency is unresolved"):
            parse_executions(evidence, instrument_versions={"ETHPERP": "ETHPERP@v1"})
        fills = parse_executions(
            evidence,
            instrument_versions={"ETHPERP": "ETHPERP@v1"},
            qualified_fee_currencies={"ETHPERP@v1": "USDT"},
        )
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0].fee_amount, Decimal("0.071409"))
        self.assertEqual(fills[0].fee_currency, "USDT")

    def test_nonempty_extra_fees_cannot_silently_disappear(self):
        response = {"retCode": 0, "result": {"category": "spot", "list": [{
            "execId": "exec-extra-fee", "orderLinkId": "", "symbol": "BTCUSDT",
            "execQty": "0.01", "execPrice": "65000", "execFee": "0.5",
            "feeCurrency": "USDT",
            "extraFees": '[{"feeType":"tax","subFeeType":"regional"}]',
            "execTime": "1790280000000",
        }]}}
        with self.assertRaisesRegex(ProviderCoreError, "extraFees"):
            parse_executions(
                bound_execution_response(response),
                instrument_versions={"BTCUSDT": "BTCUSDT@v1"},
            )

    def test_empty_extra_fee_shapes_remain_economically_complete(self):
        for extra_fees in (None, "", [], {}):
            with self.subTest(extra_fees=extra_fees):
                row = {
                    "execId": f"exec-{repr(extra_fees)}", "orderLinkId": "",
                    "symbol": "BTCUSDT", "execQty": "0.01", "execPrice": "65000",
                    "execFee": "0.5", "feeCurrency": "USDT", "execTime": "1790280000000",
                }
                if extra_fees is not None:
                    row["extraFees"] = extra_fees
                fills = parse_executions(
                    bound_execution_response({"retCode": 0, "result": {"list": [row]}}),
                    instrument_versions={"BTCUSDT": "BTCUSDT@v1"},
                )
                self.assertEqual(fills[0].fee_currency, "USDT")

    def test_execution_conflict_and_unknown_symbol_fail_closed(self):
        conflict = {"retCode": 0, "result": {"list": [
            {"execId": "same", "orderLinkId": "", "symbol": "BTCUSDT", "execQty": "1", "execPrice": "10", "execFee": "0", "feeCurrency": "USDT", "execTime": "1790280000000"},
            {"execId": "same", "orderLinkId": "", "symbol": "BTCUSDT", "execQty": "2", "execPrice": "10", "execFee": "0", "feeCurrency": "USDT", "execTime": "1790280000000"},
        ]}}
        with self.assertRaisesRegex(ProviderCoreError, "conflicting"):
            parse_executions(
                bound_execution_response(conflict),
                instrument_versions={"BTCUSDT": "BTCUSDT@v1"},
            )
        unknown = {"retCode": 0, "result": {"list": [{
            "execId": "x", "orderLinkId": "", "symbol": "UNKNOWN", "execQty": "1",
            "execPrice": "10", "execFee": "0", "feeCurrency": "USD",
            "execTime": "1790280000000",
        }]}}
        with self.assertRaisesRegex(ProviderCoreError, "unmapped"):
            parse_executions(bound_execution_response(unknown), instrument_versions={})

    def test_auth_timestamp_window_matches_documented_boundaries(self):
        server = 1_000_000
        validate_auth_timestamp(
            request_timestamp_ms=995_000,
            server_time_ms=server,
            recv_window_ms=5000,
        )
        validate_auth_timestamp(
            request_timestamp_ms=1_000_999,
            server_time_ms=server,
            recv_window_ms=5000,
        )
        with self.assertRaisesRegex(ProviderCoreError, "authentication window"):
            validate_auth_timestamp(
                request_timestamp_ms=994_999,
                server_time_ms=server,
                recv_window_ms=5000,
            )
        with self.assertRaisesRegex(ProviderCoreError, "authentication window"):
            validate_auth_timestamp(
                request_timestamp_ms=1_001_000,
                server_time_ms=server,
                recv_window_ms=5000,
            )

    def test_server_time_uses_provider_nanosecond_value_without_float_rounding(self):
        result = server_time_from_response(
            {
                "retCode": 0,
                "result": {
                    "timeSecond": "1790280000",
                    "timeNano": "1790280000123456789",
                },
            }
        )
        self.assertEqual(result, "2026-09-24T20:00:00.123456Z")

    def test_absence_semantics_are_fail_closed_until_explicitly_qualified(self):
        evidence = coverage_evidence(
            surface="EXECUTIONS",
            coverage_start="2026-09-24T19:00:00Z",
            coverage_end="2026-09-24T21:00:00Z",
            pagination_complete=True,
            consistency_horizon_satisfied=True,
        
            account_id="paper-1",
            environment="PAPER",)
        self.assertFalse(evidence.provider_semantics_exclude_execution)
        self.assertFalse(
            evidence.proves_absence_for(
                datetime(2026, 9, 24, 20, tzinfo=timezone.utc)
            )
        )

        qualified = coverage_evidence(
            surface="EXECUTIONS",
            coverage_start="2026-09-24T19:00:00Z",
            coverage_end="2026-09-24T21:00:00Z",
            pagination_complete=True,
            consistency_horizon_satisfied=True,
            qualified_exclusion_semantics=True,
        
            account_id="paper-1",
            environment="PAPER",)
        self.assertTrue(
            qualified.proves_absence_for(
                datetime(2026, 9, 24, 20, tzinfo=timezone.utc)
            )
        )


if __name__ == "__main__":
    unittest.main()
