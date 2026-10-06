from datetime import datetime, timedelta, timezone, tzinfo
from decimal import Decimal
import json
from tempfile import TemporaryDirectory
from types import MappingProxyType
import unittest
from unittest.mock import patch
from uuid import uuid4

import mvp.autotrade_mvp.bybit_v5 as bybit_v5_module
from mvp.autotrade_mvp.bybit_v5 import (
    BybitPreparedSubmission,
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
    ProviderSubmissionObservation,
    Surface,
    observe_authenticated_json_response,
    observe_submission_json_response,
    prepare_authenticated_read_query,
)
from mvp.tests.capability_test_support import fresh_test_admission


READ_AT = datetime(2026, 9, 24, 20, tzinfo=timezone.utc)


def read_capability(
    *,
    account_id="paper-1",
    environment="PAPER",
    instrument_version="BTCUSDT@v1",
    provider_environment=None,
    permission_scopes=("ORDER.READ",),
    at=READ_AT,
):
    observed_at = at - timedelta(hours=1)
    claims = tuple(
        CapabilityClaim(
            source=source,
            provider_id="BYBIT",
            account_id=account_id,
            entity_id="bybit-reconciliation",
            environment=environment,
            provider_environment=provider_environment or ("MAINNET" if environment == "LIVE" else "TESTNET"),
            instrument_version=instrument_version,
            observed_at=observed_at,
            expires_at=at + timedelta(hours=1),
            supported_order_types=frozenset({"LIMIT", "MARKET"}),
            time_in_force=frozenset({"GTC", "IOC"}),
            permission_scopes=frozenset(permission_scopes),
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
    return fresh_test_admission(derive_capability_snapshot(
        snapshot_id=str(uuid4()),
        claims=claims,
        observed_at=at,
        evidence_verifier=lambda _claim: EvidenceVerification(valid=True),
    ))



def write_capability(
    *,
    family="LINEAR_DERIVATIVES",
    position_mode="HEDGE",
    account_id="bybit-account",
    environment="PAPER",
    instrument_version="BTCUSDT@1",
    expires_at=None,
    permission_scope=None,
    additional_permission_scopes=(),
    provider_environment="DEMO",
):
    scope = permission_scope or {
        "LINEAR_DERIVATIVES": "BYBIT.LINEAR.ORDER.WRITE",
        "INVERSE_DERIVATIVES": "BYBIT.INVERSE.ORDER.WRITE",
    }[family]
    scopes = frozenset({scope, *additional_permission_scopes})
    observed_at = READ_AT - timedelta(minutes=1)
    claims = tuple(
        CapabilityClaim(
            source=source,
            provider_id="BYBIT",
            account_id=account_id,
            entity_id="bybit-unified-account",
            environment=environment,
            provider_environment=provider_environment,
            instrument_version=instrument_version,
            observed_at=observed_at,
            expires_at=expires_at or READ_AT + timedelta(minutes=5),
            supported_order_types=frozenset({"LIMIT", "MARKET"}),
            time_in_force=frozenset({"GTC", "IOC"}),
            permission_scopes=scopes,
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
    return fresh_test_admission(derive_capability_snapshot(
        snapshot_id=str(uuid4()),
        claims=claims,
        observed_at=READ_AT,
        evidence_verifier=lambda _claim: EvidenceVerification(valid=True),
    ))

def submission_write_capability(
    *,
    account_id="bybit-account",
    environment="LIVE",
    instrument_version="BTCUSDT@v1",
    provider_environment=None,
):
    observed_at = READ_AT - timedelta(hours=1)
    claims = tuple(
        CapabilityClaim(
            source=source,
            provider_id="BYBIT",
            account_id=account_id,
            entity_id="bybit-order",
            environment=environment,
            provider_environment=provider_environment or ("MAINNET" if environment == "LIVE" else "TESTNET"),
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
    return fresh_test_admission(derive_capability_snapshot(
        snapshot_id=str(uuid4()),
        claims=claims,
        observed_at=READ_AT,
        evidence_verifier=lambda _claim: EvidenceVerification(valid=True),
    ))


def bound_execution_response(
    response,
    *,
    account_id="paper-1",
    environment="PAPER",
    instrument_version="BTCUSDT@v1",
    query_category="spot",
    permission_scope="ORDER.READ",
    ensure_response_category=True,
    ensure_trade_exec_type=True,
    query_overrides=None,
    capability=None,
    at=READ_AT,
):
    query_values = {"category": query_category, "limit": "100"}
    if query_overrides is not None:
        if type(query_overrides) is not dict:
            raise TypeError("query_overrides must be an exact dict")
        query_values.update(query_overrides)
    capability = capability or read_capability(
        account_id=account_id,
        environment=environment,
        instrument_version=instrument_version,
        permission_scopes=(permission_scope,),
        at=at,
    )
    query = prepare_authenticated_read_query(
        capability=capability,
        surface=Surface.AUTHENTICATED_READ,
        endpoint="/v5/execution/list",
        query=query_values,
        at=at,
        permission_scope=permission_scope,
    )
    if type(response) is dict:
        result = response.get("result")
        if type(result) is dict:
            rows = result.get("list")
            needs_category = ensure_response_category and "category" not in result
            needs_exec_type = (
                ensure_trade_exec_type
                and type(rows) is list
                and any(type(row) is dict and "execType" not in row for row in rows)
            )
            if needs_category or needs_exec_type:
                response = dict.copy(response)
                result = dict.copy(result)
                if needs_category:
                    result["category"] = query_category
                if needs_exec_type:
                    result["list"] = [
                        (
                            dict(row, execType="Trade")
                            if type(row) is dict and "execType" not in row
                            else row
                        )
                        for row in rows
                    ]
                response["result"] = result
    raw = json.dumps(
        response,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return observe_authenticated_json_response(
        query_binding=query,
        http_status=200,
        response_bytes=raw,
        observed_at=at,
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

    def test_spot_payload_rejects_ignored_derivative_leg_controls(self):
        base = {
            "product_family": "SPOT",
            "symbol": "BTCUSDT",
            "side": "BUY",
            "order_type": "MARKET",
            "quantity": "0.01",
            "client_order_id": "spot-no-derivative-leg",
            "time_in_force": "IOC",
        }
        for name, value in (("position_side", "LONG"), ("position_idx", 1)):
            with self.subTest(name=name):
                with self.assertRaisesRegex(
                    ProviderCoreError,
                    "only supported for derivative orders",
                ):
                    build_order_payload(**base, **{name: value})

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
        submission_scope_provider_environment=None,
        submission_scope_capability_snapshot_id=None,
        http_status=200,
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
            provider_environment=provider_environment,
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
        if isinstance(result, dict):
            if result.get("orderLinkId") == "__CLIENT__":
                result["orderLinkId"] = client_id
            elif result.get("orderLinkId") == "__CLIENT_PADDED__":
                result["orderLinkId"] = f" {client_id} "
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
                    ExactJsonTransportResponse(
                        raw,
                        http_status=http_status,
                    ),
                )[1],
                sender_check=lambda _owner, _epoch: None,
                submission_scope={
                    "provider_id": "BYBIT",
                    "account_id": account_id,
                    "environment": runtime_environment,
                    "provider_environment": (
                        submission_scope_provider_environment
                        if submission_scope_provider_environment is not None
                        else provider_environment
                    ),
                    "capability_snapshot_id": (
                        submission_scope_capability_snapshot_id
                        if submission_scope_capability_snapshot_id is not None
                        else prepared.capability_snapshot_id
                    ),
                    "endpoint": prepared.endpoint,
                    "prepared_request_sha256": prepared.body_sha256,
                    "capability_snapshot_ids": list(
                        prepared.capability_snapshot_ids
                    ),
                    "instrument_versions": list(prepared.instrument_versions),
                    "provider_route_qualification_id": (
                        "provider-qualification:sha256:" + "7" * 64
                    ),
                    "provider_route_capability_snapshot_id": (
                        submission_scope_capability_snapshot_id
                        if submission_scope_capability_snapshot_id is not None
                        else prepared.capability_snapshot_id
                    ),
                    "provider_route_decision_journal_sequence_cut": 41,
                    "provider_route_provider_environment": provider_environment,
                    "provider_route_adapter_code_sha": "adapter-code-sha",
                    "provider_route_packaged_artifact_digest": (
                        "sha256:" + "8" * 64
                    ),
                    "provider_route_protocol_id": "bybit-v5",
                    "provider_route_protocol_version": "1",
                    "provider_route_entity_policy_id": "linear-order-v1",
                    "provider_route_entity_id": "BTCUSDT",
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

    def test_success_response_rejects_noncanonical_provider_order_id(self):
        attempt, prepared, observation = self._durable_write_observation(
            {
                "retCode": 0,
                "retMsg": "OK",
                "result": {
                    "orderId": " provider-123 ",
                    "orderLinkId": "__CLIENT__",
                },
            },
            intent_id="bybit-noncanonical-provider-order-id",
        )
        with self.assertRaisesRegex(
            ProviderCoreError,
            "orderId response must be canonical exact text",
        ):
            parse_submission_response(
                attempt_id=attempt,
                prepared_request=prepared,
                observation=observation,
            )

    def test_success_response_rejects_noncanonical_echoed_client_order_id(self):
        attempt, prepared, observation = self._durable_write_observation(
            {
                "retCode": 0,
                "retMsg": "OK",
                "result": {
                    "orderId": "provider-123",
                    "orderLinkId": "__CLIENT_PADDED__",
                },
            },
            intent_id="bybit-noncanonical-client-order-id",
        )
        with self.assertRaisesRegex(
            ProviderCoreError,
            "orderLinkId response must be canonical exact text",
        ):
            parse_submission_response(
                attempt_id=attempt,
                prepared_request=prepared,
                observation=observation,
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

    def test_submission_response_rejects_durable_provider_environment_scope_mismatch(self):
        attempt, prepared, observation = self._durable_write_observation(
            {
                "retCode": 0,
                "retMsg": "OK",
                "result": {
                    "orderId": "provider-domain-mismatch",
                    "orderLinkId": "__CLIENT__",
                },
            },
            provider_environment="TESTNET",
            submission_scope_provider_environment="DEMO",
            intent_id="bybit-provider-domain-mismatch-demo",
        )
        with self.assertRaisesRegex(
            ProviderCoreError,
            "provider-write provenance scope mismatch",
        ):
            parse_submission_response(
                attempt_id=attempt,
                prepared_request=prepared,
                observation=observation,
            )

    def test_submission_observation_rejects_partial_financial_route_scope(self):
        prepared, response_binding = self._durable_write_response_binding(
            {"retCode": 0, "retMsg": "OK", "result": {}},
            provider_environment="TESTNET",
            intent_id="bybit-partial-route-scope",
        )
        partial_scope = dict(response_binding.submission_scope)
        partial_scope.pop("provider_route_protocol_version")
        rebound = replace(
            response_binding,
            submission_scope=partial_scope,
            submission_scope_hash=payload_digest(partial_scope),
        )
        with self.assertRaisesRegex(
            ProviderCoreError,
            "financial route submission scope is incomplete",
        ):
            observe_submission_json_response(
                prepared,
                rebound,
                provider_id="BYBIT",
            )

    def test_submission_observation_rejects_unknown_scope_axis(self):
        prepared, response_binding = self._durable_write_response_binding(
            {"retCode": 0, "retMsg": "OK", "result": {}},
            provider_environment="TESTNET",
            intent_id="bybit-unknown-scope-axis",
        )
        expanded_scope = dict(response_binding.submission_scope)
        expanded_scope["caller_extension"] = "forged"
        rebound = replace(
            response_binding,
            submission_scope=expanded_scope,
            submission_scope_hash=payload_digest(expanded_scope),
        )
        with self.assertRaisesRegex(
            ProviderCoreError,
            "unknown authority axes",
        ):
            observe_submission_json_response(
                prepared,
                rebound,
                provider_id="BYBIT",
            )

    def test_submission_observation_rejects_route_capability_retarget(self):
        with self.assertRaisesRegex(
            ProviderCoreError,
            "capability scope does not match prepared request",
        ):
            self._durable_write_observation(
                {
                    "retCode": 0,
                    "retMsg": "OK",
                    "result": {
                        "orderId": "provider-capability-retarget",
                        "orderLinkId": "__CLIENT__",
                    },
                },
                provider_environment="TESTNET",
                submission_scope_capability_snapshot_id="other-capability",
                intent_id="bybit-route-capability-retarget",
            )

    def test_submission_response_rejects_missing_http_status_sent_binding(self):
        attempt, prepared, observation = self._durable_write_observation(
            {
                "retCode": 0,
                "retMsg": "OK",
                "result": {
                    "orderId": "missing-http-status",
                    "orderLinkId": "__CLIENT__",
                },
            },
            http_status=None,
            intent_id="bybit-missing-http-status",
        )
        with self.assertRaisesRegex(
            ProviderCoreError,
            "requires successful HTTP status",
        ):
            parse_submission_response(
                attempt_id=attempt,
                prepared_request=prepared,
                observation=observation,
            )

    def test_submission_response_rejects_non_2xx_sent_binding(self):
        attempt, prepared, observation = self._durable_write_observation(
            {
                "retCode": 0,
                "retMsg": "OK",
                "result": {
                    "orderId": "contradictory-http-status",
                    "orderLinkId": "__CLIENT__",
                },
            },
            http_status=500,
            intent_id="bybit-contradictory-http-status",
        )
        with self.assertRaisesRegex(
            ProviderCoreError,
            "requires successful HTTP status",
        ):
            parse_submission_response(
                attempt_id=attempt,
                prepared_request=prepared,
                observation=observation,
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

    def test_raw_option_payload_fails_closed_without_option_semantics(self):
        with self.assertRaisesRegex(
            ProviderCoreError,
            "dedicated option semantics",
        ):
            build_order_payload(
                product_family="OPTIONS",
                symbol="BTC-30OCT26-100000-C",
                side="BUY",
                order_type="LIMIT",
                quantity="0.1",
                price="100",
                client_order_id="option-payload-unqualified",
                time_in_force="GTC",
            )

    def test_canonical_preparation_rejects_cross_provider_environment_capability(self):
        capability = submission_write_capability(
            environment="PAPER",
            provider_environment="TESTNET",
        )
        with self.assertRaisesRegex(
            ProviderCoreError,
            "capability provider environment does not match target",
        ):
            prepare_order_submission(
                capability=capability,
                at=READ_AT,
                provider_environment="DEMO",
                product_family="SPOT",
                symbol="BTCUSDT",
                side="BUY",
                order_type="LIMIT",
                quantity="0.01",
                price="100",
                client_order_id="domain-mismatch-rejected",
                time_in_force="GTC",
            )

    def test_derivative_payload_rejects_cross_provider_environment_capability(self):
        capability = write_capability(provider_environment="TESTNET")
        with self.assertRaisesRegex(
            ProviderCoreError,
            "capability provider environment does not match target",
        ):
            build_order_payload(
                product_family="LINEAR_DERIVATIVES",
                symbol="BTCUSDT",
                side="BUY",
                order_type="LIMIT",
                quantity="0.01",
                price="100",
                client_order_id="derivative-domain-mismatch",
                time_in_force="GTC",
                position_side="LONG",
                capability=capability,
                capability_at=READ_AT,
                account_id=capability.account_id,
                instrument_version=capability.instrument_version,
                provider_environment="DEMO",
            )

    def test_canonical_preparation_rejects_executable_tzinfo_before_callback(self):
        capability = submission_write_capability(
            environment="PAPER",
            provider_environment="DEMO",
        )
        callbacks = []

        class ExecutableTimezone(tzinfo):
            def utcoffset(self, _dt):
                callbacks.append("utcoffset")
                return timedelta(0)

            def dst(self, _dt):
                callbacks.append("dst")
                return timedelta(0)

            def tzname(self, _dt):
                callbacks.append("tzname")
                return "forged"

        at = datetime(2026, 9, 24, 20, tzinfo=ExecutableTimezone())
        with self.assertRaisesRegex(
            ProviderCoreError,
            "exact stdlib timezone",
        ):
            prepare_order_submission(
                capability=capability,
                at=at,
                provider_environment="DEMO",
                product_family="LINEAR_DERIVATIVES",
                symbol="BTCUSDT",
                side="BUY",
                order_type="LIMIT",
                quantity="0.01",
                price="100",
                client_order_id="tzinfo-authority-required",
                time_in_force="GTC",
            )
        self.assertEqual(callbacks, [])

    def test_canonical_preparation_refuses_margin_without_borrow_authority(self):
        capability = submission_write_capability(
            environment="PAPER",
            provider_environment="DEMO",
        )
        with self.assertRaisesRegex(
            ProviderCoreError,
            "dedicated spot-margin borrow/collateral authority",
        ):
            prepare_order_submission(
                capability=capability,
                at=READ_AT,
                provider_environment="DEMO",
                product_family="MARGIN",
                symbol="BTCUSDT",
                side="BUY",
                order_type="LIMIT",
                quantity="0.01",
                price="100",
                client_order_id="margin-authority-required",
                time_in_force="GTC",
            )

    def test_canonical_preparation_refuses_options_without_payoff_authority(self):
        capability = submission_write_capability(
            environment="PAPER",
            provider_environment="DEMO",
        )
        with self.assertRaisesRegex(
            ProviderCoreError,
            "dedicated option capability/payoff authority",
        ):
            prepare_order_submission(
                capability=capability,
                at=READ_AT,
                provider_environment="DEMO",
                product_family="OPTIONS",
                symbol="BTC-30OCT26-100000-C",
                side="BUY",
                order_type="LIMIT",
                quantity="0.1",
                price="100",
                client_order_id="option-authority-required",
                time_in_force="GTC",
            )

    def test_prepared_issuer_runtime_shadowing_fails_before_callback(self):
        capability = submission_write_capability()
        callbacks = []

        def forged(*_args, **_kwargs):
            callbacks.append(True)
            return None

        for name in (
            "type",
            "id",
            "tuple",
            "getattr",
            "isinstance",
            "bool",
            "int",
            "float",
            "str",
            "Decimal",
            "format",
            "Mapping",
            "json",
            "sha256",
            "TypeError",
            "ValueError",
            "InvalidOperation",
            "CapabilityError",
            "_decimal",
            "_CLIENT_ID",
            "dict",
            "len",
            "object",
            "AttributeError",
            "MappingProxyType",
            "ProviderCoreError",
            "datetime",
            "timezone",
            "CapabilitySnapshot",
            "BybitPreparedSubmission",
            "_CATEGORY_BY_FAMILY",
            "_TIME_IN_FORCE",
            "_REST_BASE_BY_ENVIRONMENT",
            "_RUNTIME_ENVIRONMENT_BY_PROVIDER_ENVIRONMENT",
            "_DERIVATIVE_ORDER_SCOPE_BY_FAMILY",
            "_CAPABILITY_ENVIRONMENT_BY_PROVIDER_ENVIRONMENT",
            "BYBIT_DOCUMENTED_ENDPOINTS",
            "_BYBIT_PREPARED_SUBMISSION_TOKEN",
        ):
            with self.subTest(name=name):
                with patch.object(
                    bybit_v5_module,
                    name,
                    forged,
                    create=True,
                ):
                    with self.assertRaisesRegex(
                        ProviderCoreError,
                        "prepared submission authority changed",
                    ):
                        bybit_v5_module.prepare_order_submission(
                            capability=capability,
                            at=READ_AT,
                            provider_environment="MAINNET",
                            product_family="SPOT",
                            symbol="BTCUSDT",
                            side="BUY",
                            order_type="MARKET",
                            quantity="0.01",
                            client_order_id=f"shadow-{name.lower()}",
                            time_in_force="IOC",
                        )
                self.assertEqual(callbacks, [])

    def test_preparation_rejects_executable_scalar_inputs_before_callback(self):
        capability = submission_write_capability()
        callbacks = []

        class HostileStr(str):
            def strip(self):
                callbacks.append("strip")
                raise AssertionError("hostile str callback executed")

            def upper(self):
                callbacks.append("upper")
                raise AssertionError("hostile str callback executed")

        base = {
            "capability": capability,
            "at": READ_AT,
            "provider_environment": "MAINNET",
            "product_family": "SPOT",
            "symbol": "BTCUSDT",
            "side": "BUY",
            "order_type": "MARKET",
            "quantity": "0.01",
            "client_order_id": "scalar-ingress",
            "time_in_force": "IOC",
        }
        for name in (
            "provider_environment",
            "product_family",
            "symbol",
            "side",
            "order_type",
            "client_order_id",
            "time_in_force",
        ):
            with self.subTest(name=name):
                arguments = dict(base)
                arguments[name] = HostileStr(arguments[name])
                with self.assertRaisesRegex(
                    ProviderCoreError,
                    f"{name} must be exact str",
                ):
                    prepare_order_submission(**arguments)
                self.assertEqual(callbacks, [])

        class HostileNumber:
            def __str__(self):
                callbacks.append("__str__")
                raise AssertionError("hostile number callback executed")

        for name in ("quantity", "price"):
            with self.subTest(name=name):
                arguments = dict(base)
                if name == "price":
                    arguments["order_type"] = "LIMIT"
                    arguments["time_in_force"] = "GTC"
                arguments[name] = HostileNumber()
                with self.assertRaisesRegex(
                    ProviderCoreError,
                    f"{name} must be exact str, Decimal or int",
                ):
                    prepare_order_submission(**arguments)
                self.assertEqual(callbacks, [])

        with self.assertRaisesRegex(
            ProviderCoreError,
            "reduce_only must be exact bool",
        ):
            prepare_order_submission(**base, reduce_only=1)
        with self.assertRaisesRegex(
            ProviderCoreError,
            "position_side must be exact str or None",
        ):
            prepare_order_submission(**base, position_side=HostileStr("NET"))
        with self.assertRaisesRegex(
            ProviderCoreError,
            "position_idx must be exact int or None",
        ):
            prepare_order_submission(**base, position_idx=True)
        self.assertEqual(callbacks, [])

    def test_prepared_issuer_rejects_json_helper_rebinding_before_callback(self):
        capability = submission_write_capability()
        callbacks = []

        def forged(*_args, **_kwargs):
            callbacks.append(True)
            raise AssertionError("rebound json helper executed")

        for name in ("dumps", "loads"):
            with self.subTest(name=name):
                with patch.object(bybit_v5_module.json, name, forged):
                    with self.assertRaisesRegex(
                        ProviderCoreError,
                        "prepared submission authority changed",
                    ):
                        prepare_order_submission(
                            capability=capability,
                            at=READ_AT,
                            provider_environment="MAINNET",
                            product_family="SPOT",
                            symbol="BTCUSDT",
                            side="BUY",
                            order_type="MARKET",
                            quantity="0.01",
                            client_order_id=f"json-{name}-rebound",
                            time_in_force="IOC",
                        )
                self.assertEqual(callbacks, [])

    def test_prepared_issuer_rejects_in_place_routing_authority_mutation(self):
        capability = submission_write_capability()
        cases = (
            (bybit_v5_module._CATEGORY_BY_FAMILY, "SPOT", "linear"),
            (bybit_v5_module._TIME_IN_FORCE, "IOC", "GTC"),
            (
                bybit_v5_module._REST_BASE_BY_ENVIRONMENT,
                "MAINNET",
                "https://attacker.invalid",
            ),
            (
                bybit_v5_module._RUNTIME_ENVIRONMENT_BY_PROVIDER_ENVIRONMENT,
                "MAINNET",
                "PAPER",
            ),
            (
                bybit_v5_module._DERIVATIVE_ORDER_SCOPE_BY_FAMILY,
                "LINEAR_DERIVATIVES",
                "ORDER_WRITE",
            ),
            (
                bybit_v5_module._CAPABILITY_ENVIRONMENT_BY_PROVIDER_ENVIRONMENT,
                "MAINNET",
                "PAPER",
            ),
            (
                bybit_v5_module.BYBIT_DOCUMENTED_ENDPOINTS,
                "PLACE_ORDER",
                "/v5/order/cancel",
            ),
        )
        for authority, key, forged_value in cases:
            with self.subTest(key=key, forged_value=forged_value):
                original = authority[key]
                authority[key] = forged_value
                try:
                    with self.assertRaisesRegex(
                        ProviderCoreError,
                        "prepared submission authority changed",
                    ):
                        bybit_v5_module.prepare_order_submission(
                            capability=capability,
                            at=READ_AT,
                            provider_environment="MAINNET",
                            product_family="SPOT",
                            symbol="BTCUSDT",
                            side="BUY",
                            order_type="MARKET",
                            quantity="0.01",
                            client_order_id="in-place-authority-mutation",
                            time_in_force="IOC",
                        )
                finally:
                    authority[key] = original

    def test_guarded_projection_rejects_runtime_shadowing_before_callback(self):
        prepared = prepare_order_submission(
            capability=submission_write_capability(),
            at=READ_AT,
            provider_environment="MAINNET",
            product_family="SPOT",
            symbol="BTCUSDT",
            side="BUY",
            order_type="MARKET",
            quantity="0.01",
            client_order_id="projection-shadow",
            time_in_force="IOC",
        )
        callbacks = []

        def forged(*_args, **_kwargs):
            callbacks.append(True)
            return None

        for name in (
            "require_canonical_bybit_prepared_submission",
            "BybitPreparedSubmission",
            "ProviderCoreError",
            "object",
            "dict",
            "MappingProxyType",
            "getattr",
        ):
            with self.subTest(name=name):
                with patch.object(
                    bybit_v5_module,
                    name,
                    forged,
                    create=True,
                ):
                    with self.assertRaisesRegex(
                        ProviderCoreError,
                        "guarded projection authority changed",
                    ):
                        bybit_v5_module.guarded_order_projection(prepared)
                self.assertEqual(callbacks, [])

        with patch.object(
            BybitPreparedSubmission,
            "__getattribute__",
            forged,
        ):
            projected = bybit_v5_module.guarded_order_projection(prepared)
        self.assertEqual(callbacks, [])
        self.assertEqual(projected["endpoint"], "/v5/order/create")
        self.assertEqual(projected["body"]["category"], "spot")
        self.assertEqual(
            projected["capability_snapshot_ids"],
            [prepared.capability_snapshot_id],
        )
        self.assertEqual(
            projected["instrument_versions"],
            [prepared.instrument_version],
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

    def test_submission_response_rejects_unissued_exact_prepared_clone(self):
        issued = prepare_order_submission(
            capability=submission_write_capability(),
            at=READ_AT,
            provider_environment="MAINNET",
            product_family="SPOT",
            symbol="BTCUSDT",
            side="BUY",
            order_type="MARKET",
            quantity="0.01",
            client_order_id=stable_client_order_id(
                "BYBIT",
                "bybit-unissued-response",
                environment="LIVE",
                account_id="bybit-account",
            ),
            time_in_force="IOC",
        )
        forged = object.__new__(BybitPreparedSubmission)
        for name in (
            "endpoint",
            "body",
            "account_id",
            "environment",
            "provider_environment",
            "capability_snapshot_id",
            "entity_id",
            "instrument_version",
            "body_sha256",
        ):
            object.__setattr__(
                forged,
                name,
                object.__getattribute__(issued, name),
            )
        with self.assertRaisesRegex(
            ProviderCoreError,
            "prepared submission authority changed",
        ):
            parse_submission_response(
                attempt_id=str(uuid4()),
                prepared_request=forged,
                observation=None,
                transport_ambiguous=True,
            )

    def test_submission_response_rejects_provider_environment_retarget(self):
        attempt, prepared, observation = self._durable_write_observation(
            {
                "retCode": 0,
                "result": {
                    "orderId": "provider-env-retarget",
                    "orderLinkId": "__CLIENT__",
                },
            }
        )
        object.__setattr__(prepared, "provider_environment", "TESTNET")
        with self.assertRaisesRegex(
            ProviderCoreError,
            "prepared submission authority changed",
        ):
            parse_submission_response(
                attempt_id=attempt,
                prepared_request=prepared,
                observation=observation,
            )

    def test_submission_response_ignores_prepared_getattribute_callback(self):
        attempt, prepared, observation = self._durable_write_observation(
            {
                "retCode": 0,
                "result": {
                    "orderId": "provider-prepared-callback",
                    "orderLinkId": "__CLIENT__",
                },
            }
        )
        callbacks = []

        def forged(*_args, **_kwargs):
            callbacks.append(True)
            raise AssertionError("prepared virtual callback executed")

        with patch.object(BybitPreparedSubmission, "__getattribute__", forged):
            result = parse_submission_response(
                attempt_id=attempt,
                prepared_request=prepared,
                observation=observation,
            )
        self.assertEqual(callbacks, [])
        self.assertEqual(result["outcome"], "ACKNOWLEDGED")
        self.assertEqual(result["provider_order_id"], "provider-prepared-callback")

    def test_submission_response_rejects_rebound_prepared_projection_without_callback(self):
        prepared = prepare_order_submission(
            capability=submission_write_capability(),
            at=READ_AT,
            provider_environment="MAINNET",
            product_family="SPOT",
            symbol="BTCUSDT",
            side="BUY",
            order_type="MARKET",
            quantity="0.01",
            client_order_id="prepared-projection-rebound",
            time_in_force="IOC",
        )
        callbacks = []

        def forged(*_args, **_kwargs):
            callbacks.append(True)
            return {}

        with patch.object(
            bybit_v5_module,
            "guarded_order_projection",
            forged,
        ) as rebound:
            with self.assertRaisesRegex(
                ProviderCoreError,
                "submission response parser authority changed",
            ):
                parse_submission_response(
                    attempt_id=str(uuid4()),
                    prepared_request=prepared,
                    observation=None,
                    transport_ambiguous=True,
                )
        rebound.assert_not_called()
        self.assertEqual(callbacks, [])

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

    def test_submission_consumer_rejects_unregistered_exact_clone(self):
        attempt, prepared, _observation = self._durable_write_observation(
            {
                "retCode": 0,
                "result": {
                    "orderId": "provider-clone",
                    "orderLinkId": "__CLIENT__",
                },
            }
        )
        forged = object.__new__(ProviderSubmissionObservation)
        with self.assertRaisesRegex(
            ProviderCoreError,
            "provider submission observation authority is unavailable",
        ):
            parse_submission_response(
                attempt_id=attempt,
                prepared_request=prepared,
                observation=forged,
            )

    def test_submission_consumer_rejects_rebound_projection_alias_before_forgery(self):
        attempt, prepared, _observation = self._durable_write_observation(
            {
                "retCode": 0,
                "result": {
                    "orderId": "provider-alias-forgery",
                    "orderLinkId": "__CLIENT__",
                },
            }
        )
        forged = object.__new__(ProviderSubmissionObservation)
        with patch.object(
            bybit_v5_module,
            "provider_submission_observation_projection",
            return_value={
                "attempt_id": attempt,
                "provider_id": "BYBIT",
                "endpoint": prepared.endpoint,
                "request_sha256": prepared.body_sha256,
                "capability_snapshot_ids": prepared.capability_snapshot_ids,
                "instrument_versions": prepared.instrument_versions,
                "account_id": prepared.account_id,
                "environment": prepared.environment,
                "client_order_id": prepared.body["orderLinkId"],
                "evidence_ref": "provider-write:sha256:" + "0" * 64,
                "response_sha256": "sha256:" + "0" * 64,
                "sent_at": "2026-09-24T20:00:00Z",
                "payload": {
                    "retCode": 0,
                    "result": {
                        "orderId": "forged",
                        "orderLinkId": prepared.body["orderLinkId"],
                    },
                },
            },
        ):
            with self.assertRaisesRegex(
                ProviderCoreError,
                "submission response parser authority changed",
            ):
                parse_submission_response(
                    attempt_id=attempt,
                    prepared_request=prepared,
                    observation=forged,
                )

    def test_submission_parser_rejects_uuid5_code_mutation_before_execution(self):
        attempt, prepared, observation = self._durable_write_observation(
            {
                "retCode": 0,
                "result": {
                    "orderId": "provider-uuid5-code-fence",
                    "orderLinkId": "__CLIENT__",
                },
            }
        )

        def forged_uuid5(_namespace, _name):
            raise AssertionError("mutated uuid5 code executed")

        with patch.object(
            bybit_v5_module.uuid5,
            "__code__",
            forged_uuid5.__code__,
        ):
            with self.assertRaisesRegex(
                ProviderCoreError,
                "submission response parser authority changed",
            ):
                parse_submission_response(
                    attempt_id=attempt,
                    prepared_request=prepared,
                    observation=observation,
                )

    def test_submission_consumer_rejects_subclass_before_virtual_callback(self):
        attempt, prepared, _observation = self._durable_write_observation(
            {
                "retCode": 0,
                "result": {
                    "orderId": "provider-subclass",
                    "orderLinkId": "__CLIENT__",
                },
            }
        )

        class HostileObservation(ProviderSubmissionObservation):
            def __getattribute__(self, _name):
                raise AssertionError(
                    "virtual callback executed before authority verification"
                )

        forged = object.__new__(HostileObservation)
        with self.assertRaisesRegex(
            TypeError,
            "durable ProviderSubmissionObservation",
        ):
            parse_submission_response(
                attempt_id=attempt,
                prepared_request=prepared,
                observation=forged,
            )

    def test_submission_consumer_rejects_rebound_require_scope_without_callback(self):
        attempt, prepared, observation = self._durable_write_observation(
            {
                "retCode": 0,
                "retMsg": "OK",
                "result": {
                    "orderId": "provider-no-virtual-scope",
                    "orderLinkId": "__CLIENT__",
                },
                "time": 1790280000123,
            }
        )
        with patch.object(
            ProviderSubmissionObservation,
            "require_scope",
            side_effect=AssertionError(
                "rebindable require_scope callback must not execute"
            ),
        ) as rebound:
            with self.assertRaisesRegex(
                ProviderCoreError,
                "provider submission observation authority is unavailable",
            ):
                parse_submission_response(
                    attempt_id=attempt,
                    prepared_request=prepared,
                    observation=observation,
                )
        rebound.assert_not_called()

    def test_submission_consumer_rejects_post_mint_payload_retargeting(self):
        attempt, prepared, observation = self._durable_write_observation(
            {
                "retCode": 0,
                "result": {
                    "orderId": "provider-original",
                    "orderLinkId": "__CLIENT__",
                },
            }
        )
        object.__setattr__(
            observation,
            "payload",
            {
                "retCode": 0,
                "result": {
                    "orderId": "provider-forged",
                    "orderLinkId": "__CLIENT__",
                },
            },
        )
        with self.assertRaisesRegex(
            ProviderCoreError,
            "provider submission observation authority is unavailable",
        ):
            parse_submission_response(
                attempt_id=attempt,
                prepared_request=prepared,
                observation=observation,
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

    def test_unclassified_nonzero_response_remains_reconciliation_first(self):
        attempt, prepared, observation = self._durable_write_observation(
            {
                "retCode": 199999,
                "retMsg": "unclassified provider response",
                "result": {},
                "retExtInfo": {},
                "time": 1790280000123,
            },
            intent_id="bybit-unclassified-response",
        )
        result = parse_submission_response(
            attempt_id=attempt,
            prepared_request=prepared,
            observation=observation,
        )
        self.assertEqual(result["outcome"], "UNKNOWN")
        self.assertEqual(result["reason_code"], "BYBIT_199999")
        self.assertEqual(result["retry_disposition"], "RECONCILE_FIRST")

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
                    "execId": "exec-1", "orderLinkId": "client-1", "symbol": "BTCUSDT", "side": "Buy",
                    "execQty": "0.25", "execPrice": "65000.10", "execFee": "1.23",
                    "feeCurrency": "USDT", "execTime": "1790280000123",
                },
                {
                    "execId": "exec-1", "orderLinkId": "client-1", "symbol": "BTCUSDT", "side": "Buy",
                    "execQty": "0.25", "execPrice": "65000.10", "execFee": "1.23",
                    "feeCurrency": "USDT", "execTime": "1790280000123",
                },
            ]},
            "time": 1790280001000,
        }
        observation = bound_execution_response(response)
        fills = parse_executions(
            observation,
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
        self.assertEqual(fill.side, "BUY")
        self.assertIsNone(fill.position_side)
        self.assertEqual(fill.evidence_refs, (observation.evidence_ref,))

    def test_execution_consumer_rejects_observation_subclass_before_virtual_callback(self):
        canonical = bound_execution_response(
            {
                "retCode": 0,
                "result": {
                    "list": [
                        {
                            "execId": "exec-hostile-observation",
                            "orderLinkId": "",
                            "symbol": "BTCUSDT",
                            "side": "Buy",
                            "execQty": "0.01",
                            "execPrice": "65000",
                            "execFee": "0",
                            "feeCurrency": "USDT",
                            "execTime": "1790280000000",
                        }
                    ]
                },
            }
        )
        callbacks = []

        class HostileObservation(type(canonical)):
            def __getattribute__(self, name):
                callbacks.append(name)
                raise AssertionError(
                    "virtual provider-response callback executed before exact-type fence"
                )

        forged = object.__new__(HostileObservation)
        with self.assertRaisesRegex(
            TypeError,
            "exact ProviderResponseObservation",
        ):
            parse_executions(
                forged,
                instrument_versions={"BTCUSDT": "BTCUSDT@v1"},
            )
        self.assertEqual(callbacks, [])

    def test_execution_parser_uses_sealed_observation_projection(self):
        observation = bound_execution_response(
            {
                "retCode": 0,
                "result": {
                    "list": [
                        {
                            "execId": "exec-sealed-projection",
                            "orderLinkId": "",
                            "symbol": "BTCUSDT",
                            "side": "Buy",
                            "execQty": "0.01",
                            "execPrice": "65000",
                            "execFee": "0",
                            "feeCurrency": "USDT",
                            "execTime": "1790280000000",
                        }
                    ]
                },
            }
        )
        callbacks = []

        def forged_scope(*_args, **_kwargs):
            callbacks.append("require_scope")
            raise AssertionError("public scope method executed")

        with patch.object(type(observation), "require_scope", forged_scope), patch.object(
            type(observation.query_binding),
            "require_scope",
            forged_scope,
        ):
            fills = parse_executions(
                observation,
                instrument_versions={"BTCUSDT": "BTCUSDT@v1"},
            )
        self.assertEqual(callbacks, [])
        self.assertEqual(fills[0].provider_execution_id, "exec-sealed-projection")
        self.assertEqual(fills[0].evidence_refs, (observation.evidence_ref,))

    def test_execution_instrument_version_must_match_authenticated_read_capability(self):
        observation = bound_execution_response(
            {
                "retCode": 0,
                "result": {
                    "list": [
                        {
                            "execId": "exec-instrument-authority",
                            "orderLinkId": "",
                            "symbol": "BTCUSDT",
                            "side": "Buy",
                            "execQty": "0.01",
                            "execPrice": "65000",
                            "execFee": "0",
                            "feeCurrency": "USDT",
                            "execTime": "1790280000000",
                        }
                    ]
                },
            },
            instrument_version="BTCUSDT@v1",
        )
        with self.assertRaisesRegex(
            ProviderCoreError,
            "does not match authenticated read capability",
        ):
            parse_executions(
                observation,
                instrument_versions={"BTCUSDT": "BTCUSDT@v2"},
            )

    def test_execution_read_scope_and_response_category_are_bound(self):
        row = {
            "execId": "exec-scope-category",
            "orderLinkId": "",
            "symbol": "BTCUSDT",
            "side": "Buy",
            "execQty": "0.01",
            "execPrice": "65000",
            "execFee": "0",
            "feeCurrency": "USDT",
            "execTime": "1790280000000",
        }

        wrong_permission = bound_execution_response(
            {"retCode": 0, "result": {"list": [row]}},
            permission_scope="ACCOUNT.READ",
        )
        with self.assertRaisesRegex(
            ProviderCoreError,
            "ORDER.READ permission scope",
        ):
            parse_executions(
                wrong_permission,
                instrument_versions={"BTCUSDT": "BTCUSDT@v1"},
            )

        wrong_query_category = bound_execution_response(
            {"retCode": 0, "result": {"list": [row]}},
            query_category="SPOT",
        )
        with self.assertRaisesRegex(
            ProviderCoreError,
            "exact documented category",
        ):
            parse_executions(
                wrong_query_category,
                instrument_versions={"BTCUSDT": "BTCUSDT@v1"},
            )

        mismatched_response = bound_execution_response(
            {
                "retCode": 0,
                "result": {"category": "linear", "list": [row]},
            },
            query_category="spot",
        )
        with self.assertRaisesRegex(
            ProviderCoreError,
            "response category must match exact queried category",
        ):
            parse_executions(
                mismatched_response,
                instrument_versions={"BTCUSDT": "BTCUSDT@v1"},
            )

        missing_response_category = bound_execution_response(
            {"retCode": 0, "result": {"list": [row]}},
            ensure_response_category=False,
        )
        with self.assertRaisesRegex(
            ProviderCoreError,
            "response category must match exact queried category",
        ):
            parse_executions(
                missing_response_category,
                instrument_versions={"BTCUSDT": "BTCUSDT@v1"},
            )

    def test_execution_rows_match_exact_active_query_filter(self):
        row = {
            "execId": "exec-query-filter",
            "orderId": "provider-order-1",
            "orderLinkId": "client-filter-1",
            "symbol": "BTCUSDT",
            "side": "Buy",
            "execQty": "0.01",
            "execPrice": "65000",
            "execFee": "0",
            "feeCurrency": "USDT",
            "execTime": "1790280000000",
        }

        for query_overrides, message in (
            ({"symbol": "ETHUSDT"}, "exact symbol query"),
            ({"orderLinkId": "client-filter-2"}, "exact orderLinkId query"),
            ({"orderId": "provider-order-2"}, "exact orderId query"),
        ):
            with self.subTest(query_overrides=query_overrides):
                observation = bound_execution_response(
                    {"retCode": 0, "result": {"list": [row]}},
                    query_overrides=query_overrides,
                )
                with self.assertRaisesRegex(ProviderCoreError, message):
                    parse_executions(
                        observation,
                        instrument_versions={"BTCUSDT": "BTCUSDT@v1"},
                    )

        ambiguous = bound_execution_response(
            {"retCode": 0, "result": {"list": [row]}},
            query_overrides={
                "orderId": "provider-order-1",
                "orderLinkId": "client-filter-1",
            },
        )
        with self.assertRaisesRegex(
            ProviderCoreError,
            "must not combine provider selectors",
        ):
            parse_executions(
                ambiguous,
                instrument_versions={"BTCUSDT": "BTCUSDT@v1"},
            )

        base_coin_only = bound_execution_response(
            {"retCode": 0, "result": {"list": [row]}},
            query_overrides={"baseCoin": "BTC"},
        )
        with self.assertRaisesRegex(
            ProviderCoreError,
            "baseCoin-filtered execution rows require qualified",
        ):
            parse_executions(
                base_coin_only,
                instrument_versions={"BTCUSDT": "BTCUSDT@v1"},
            )

    def test_execution_rows_require_exact_trade_exec_type(self):
        row = {
            "execId": "exec-type-guard",
            "orderId": "provider-order-type-guard",
            "orderLinkId": "",
            "symbol": "BTCUSDT",
            "side": "Buy",
            "execQty": "0.01",
            "execPrice": "65000",
            "execFee": "0",
            "feeCurrency": "USDT",
            "execTime": "1790280000000",
        }

        missing = bound_execution_response(
            {"retCode": 0, "result": {"list": [row]}},
            ensure_trade_exec_type=False,
        )
        with self.assertRaisesRegex(
            ProviderCoreError,
            "row execType to be exact Trade",
        ):
            parse_executions(
                missing,
                instrument_versions={"BTCUSDT": "BTCUSDT@v1"},
            )

        for non_trade_type in (
            "AdlTrade",
            "Funding",
            "BustTrade",
            "Delivery",
            "Settle",
            "BlockTrade",
            "MovePosition",
            "FutureSpread",
            "CorporateAction",
            "UNKNOWN",
        ):
            with self.subTest(execType=non_trade_type):
                response = bound_execution_response(
                    {
                        "retCode": 0,
                        "result": {
                            "list": [dict(row, execType=non_trade_type)]
                        },
                    }
                )
                with self.assertRaisesRegex(
                    ProviderCoreError,
                    "row execType to be exact Trade",
                ):
                    parse_executions(
                        response,
                        instrument_versions={"BTCUSDT": "BTCUSDT@v1"},
                    )

        non_trade_query = bound_execution_response(
            {
                "retCode": 0,
                "result": {"list": [dict(row, execType="Funding")]},
            },
            query_overrides={"execType": "Funding"},
        )
        with self.assertRaisesRegex(
            ProviderCoreError,
            "query execType to be exact Trade",
        ):
            parse_executions(
                non_trade_query,
                instrument_versions={"BTCUSDT": "BTCUSDT@v1"},
            )

        exact_trade = bound_execution_response(
            {
                "retCode": 0,
                "result": {"list": [dict(row, execType="Trade")]},
            },
            query_overrides={"execType": "Trade"},
        )
        fills = parse_executions(
            exact_trade,
            instrument_versions={"BTCUSDT": "BTCUSDT@v1"},
        )
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0].provider_execution_id, "exec-type-guard")

    def test_execution_success_code_requires_exact_json_integer(self):
        row = {
            "execId": "exec-ret-code-type",
            "orderLinkId": "",
            "symbol": "BTCUSDT",
            "side": "Buy",
            "execQty": "0.01",
            "execPrice": "65000",
            "execFee": "0",
            "feeCurrency": "USDT",
            "execTime": "1790280000000",
        }
        for malformed in ("0", False):
            with self.subTest(retCode=malformed):
                with self.assertRaisesRegex(
                    ProviderCoreError,
                    "retCode must be exact integer",
                ):
                    parse_executions(
                        bound_execution_response(
                            {"retCode": malformed, "result": {"list": [row]}}
                        ),
                        instrument_versions={"BTCUSDT": "BTCUSDT@v1"},
                    )

    def test_execution_direction_is_evidenced_without_inventing_hedge_leg(self):
        base = {
            "execId": "exec-direction",
            "orderLinkId": "",
            "symbol": "BTCUSDT",
            "execQty": "1",
            "execPrice": "10",
            "execFee": "0",
            "feeCurrency": "USDT",
            "execTime": "1790280000000",
        }
        with self.assertRaisesRegex(ProviderCoreError, "side"):
            parse_executions(
                bound_execution_response(
                    {"retCode": 0, "result": {"list": [base]}}
                ),
                instrument_versions={"BTCUSDT": "BTCUSDT@v1"},
            )

        documented = dict(base, side="Sell")
        observation = bound_execution_response(
            {"retCode": 0, "result": {"list": [documented]}}
        )
        fill = parse_executions(
            observation,
            instrument_versions={"BTCUSDT": "BTCUSDT@v1"},
        )[0]
        self.assertEqual(fill.side, "SELL")
        self.assertIsNone(fill.position_side)
        self.assertIsNone(fill.position_effect)
        self.assertEqual(fill.evidence_refs, (observation.evidence_ref,))

    def test_execution_scope_is_derived_from_prepared_read_not_parser_labels(self):
        response = {"retCode": 0, "result": {"list": [{
            "execId": "scope-1", "orderLinkId": "", "symbol": "BTCUSDT", "side": "Buy",
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

    def test_execution_rejects_noncanonical_present_fee_currency(self):
        base = {
            "execId": "exec-fee-currency-canonical",
            "orderLinkId": "",
            "symbol": "BTCUSDT",
            "side": "Buy",
            "execQty": "0.01",
            "execPrice": "65000",
            "execFee": "0.5",
            "execTime": "1790280000000",
        }
        for malformed in (" USDT ", "usdt", 123):
            with self.subTest(feeCurrency=malformed):
                row = dict(base, feeCurrency=malformed)
                with self.assertRaisesRegex(
                    ProviderCoreError,
                    "fee currency must be canonical exact text",
                ):
                    parse_executions(
                        bound_execution_response(
                            {"retCode": 0, "result": {"list": [row]}}
                        ),
                        instrument_versions={"BTCUSDT": "BTCUSDT@v1"},
                    )

    def test_execution_metadata_inputs_require_exact_dict_snapshots(self):
        row = {
            "execId": "exec-metadata-snapshot",
            "orderLinkId": "",
            "symbol": "BTCUSDT",
            "side": "Buy",
            "execQty": "0.01",
            "execPrice": "65000",
            "execFee": "0.5",
            "feeCurrency": "USDT",
            "execTime": "1790280000000",
        }
        observation = bound_execution_response(
            {"retCode": 0, "result": {"list": [row]}}
        )
        callbacks = []

        class HostileDict(dict):
            def __getitem__(self, key):
                callbacks.append(("getitem", key))
                raise AssertionError("mapping callback executed")

            def __iter__(self):
                callbacks.append(("iter", None))
                raise AssertionError("mapping callback executed")

        with self.assertRaisesRegex(
            ProviderCoreError,
            "instrument_versions must be an exact dict snapshot",
        ):
            parse_executions(
                observation,
                instrument_versions=HostileDict(
                    {"BTCUSDT": "BTCUSDT@v1"}
                ),
            )
        self.assertEqual(callbacks, [])

        immutable_instruments = MappingProxyType(
            {"BTCUSDT": "BTCUSDT@v1"}
        )
        fills = parse_executions(
            observation,
            instrument_versions=immutable_instruments,
        )
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0].instrument, "BTCUSDT@v1")
        self.assertEqual(fills[0].fee_currency, "USDT")

        with self.assertRaisesRegex(
            ProviderCoreError,
            "instrument_versions keys and values must be exact text",
        ):
            parse_executions(
                observation,
                instrument_versions=MappingProxyType(
                    {1: "BTCUSDT@v1"}
                ),
            )

        with self.assertRaisesRegex(
            ProviderCoreError,
            "instrument version must be canonical exact text",
        ):
            parse_executions(
                observation,
                instrument_versions={"BTCUSDT": " BTCUSDT@v1 "},
            )

    def test_execution_rejects_noncanonical_provider_identity_text(self):
        base = {
            "execId": "exec-identity-canonical",
            "orderLinkId": "client-identity-canonical",
            "symbol": "BTCUSDT",
            "side": "Buy",
            "execQty": "0.01",
            "execPrice": "65000",
            "execFee": "0.5",
            "feeCurrency": "USDT",
            "execTime": "1790280000000",
        }
        cases = (
            ("execId", " exec-identity-canonical ", "execution id"),
            ("execId", 123, "execution id"),
            ("symbol", " BTCUSDT ", "execution symbol"),
            ("symbol", 123, "execution symbol"),
            ("orderLinkId", " client-identity-canonical ", "orderLinkId"),
            ("orderLinkId", 123, "orderLinkId"),
            ("side", "BUY", "execution side"),
            ("side", " Buy ", "execution side"),
            ("side", 123, "execution side"),
        )
        for field, malformed, message in cases:
            with self.subTest(field=field, value=malformed):
                row = dict(base, **{field: malformed})
                with self.assertRaisesRegex(ProviderCoreError, message):
                    parse_executions(
                        bound_execution_response(
                            {"retCode": 0, "result": {"list": [row]}}
                        ),
                        instrument_versions={"BTCUSDT": "BTCUSDT@v1"},
                    )

    def test_execution_rejects_noncanonical_economic_field_types(self):
        base = {
            "execId": "exec-economics-canonical",
            "orderLinkId": "",
            "symbol": "BTCUSDT",
            "side": "Buy",
            "execQty": "0.01",
            "execPrice": "65000.10",
            "execFee": "0.5",
            "feeCurrency": "USDT",
            "execTime": "1790280000000",
        }
        cases = (
            ("execQty", " 0.01 "),
            ("execQty", 1),
            ("execPrice", " 65000.10 "),
            ("execPrice", 65000),
            ("execFee", " 0.5 "),
            ("execFee", 1),
            ("execTime", " 1790280000000 "),
            ("execTime", 1790280000000),
        )
        for field, malformed in cases:
            with self.subTest(field=field, value=malformed):
                row = dict(base, **{field: malformed})
                with self.assertRaisesRegex(ProviderCoreError, field):
                    parse_executions(
                        bound_execution_response(
                            {"retCode": 0, "result": {"list": [row]}}
                        ),
                        instrument_versions={"BTCUSDT": "BTCUSDT@v1"},
                    )

    def test_execution_numeric_fields_use_bounded_financial_envelope(self):
        base = {
            "execId": "exec-bounded-economics",
            "orderLinkId": "",
            "symbol": "BTCUSDT",
            "side": "Buy",
            "execQty": "0.01",
            "execPrice": "65000.10",
            "execFee": "0.5",
            "feeCurrency": "USDT",
            "execTime": "1790280000000",
        }
        oversized_decimal = "1" * 260
        for field in ("execQty", "execPrice", "execFee"):
            with self.subTest(field=field):
                row = dict(base, **{field: oversized_decimal})
                with self.assertRaisesRegex(
                    ProviderCoreError,
                    "exact numeric envelope",
                ):
                    parse_executions(
                        bound_execution_response(
                            {"retCode": 0, "result": {"list": [row]}}
                        ),
                        instrument_versions={"BTCUSDT": "BTCUSDT@v1"},
                    )

        for malformed_time, message in (
            ("1" * 260, "bounded integer string"),
            ("999999999999999999999999999999999999", "supported UTC range"),
        ):
            with self.subTest(execTime=malformed_time):
                row = dict(base, execTime=malformed_time)
                with self.assertRaisesRegex(ProviderCoreError, message):
                    parse_executions(
                        bound_execution_response(
                            {"retCode": 0, "result": {"list": [row]}}
                        ),
                        instrument_versions={"BTCUSDT": "BTCUSDT@v1"},
                    )

    def test_documented_linear_execution_fails_closed_without_qualified_authority(self):
        response = {"retCode": 0, "result": {"category": "linear", "list": [{
            "execId": "e0cbe81d-0f18-5866-9415-cf319b5dab3b", "orderLinkId": "",
            "symbol": "ETHPERP", "side": "Buy", "execQty": "0.1", "execPrice": "1190.15",
            "execFee": "0.071409", "feeCurrency": "", "extraFees": "",
            "execTime": "1672282722429",
        }]}}
        evidence = bound_execution_response(
            response,
            instrument_version="ETHPERP@v1",
            query_category="linear",
        )
        with self.assertRaisesRegex(ProviderCoreError, "fee currency is unresolved"):
            parse_executions(evidence, instrument_versions={"ETHPERP": "ETHPERP@v1"})
        with self.assertRaises(TypeError):
            parse_executions(
                evidence,
                instrument_versions={"ETHPERP": "ETHPERP@v1"},
                qualified_fee_currencies={"ETHPERP@v1": "USDT"},
            )

    def test_nonempty_extra_fees_cannot_silently_disappear(self):
        response = {"retCode": 0, "result": {"category": "spot", "list": [{
            "execId": "exec-extra-fee", "orderLinkId": "", "symbol": "BTCUSDT", "side": "Buy",
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
                    "symbol": "BTCUSDT", "side": "Buy", "execQty": "0.01", "execPrice": "65000",
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
            {"execId": "same", "orderLinkId": "", "symbol": "BTCUSDT", "side": "Buy", "execQty": "1", "execPrice": "10", "execFee": "0", "feeCurrency": "USDT", "execTime": "1790280000000"},
            {"execId": "same", "orderLinkId": "", "symbol": "BTCUSDT", "side": "Buy", "execQty": "2", "execPrice": "10", "execFee": "0", "feeCurrency": "USDT", "execTime": "1790280000000"},
        ]}}
        with self.assertRaisesRegex(ProviderCoreError, "conflicting"):
            parse_executions(
                bound_execution_response(conflict),
                instrument_versions={"BTCUSDT": "BTCUSDT@v1"},
            )
        unknown = {"retCode": 0, "result": {"list": [{
            "execId": "x", "orderLinkId": "", "symbol": "UNKNOWN", "side": "Buy", "execQty": "1",
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

    def test_absence_semantics_are_fail_closed_until_canonical_authority_exists(self):
        evidence = coverage_evidence(
            surface="EXECUTIONS",
            coverage_start="2026-09-24T19:00:00Z",
            coverage_end="2026-09-24T21:00:00Z",
            pagination_complete=True,
            consistency_horizon_satisfied=True,
            account_id="paper-1",
            environment="PAPER",
        )
        self.assertFalse(evidence.provider_semantics_exclude_execution)
        self.assertFalse(
            evidence.proves_absence_for(
                datetime(2026, 9, 24, 20, tzinfo=timezone.utc)
            )
        )

        with self.assertRaisesRegex(
            ProviderCoreError,
            "qualification|authority|exclusion semantics",
        ):
            coverage_evidence(
                surface="EXECUTIONS",
                coverage_start="2026-09-24T19:00:00Z",
                coverage_end="2026-09-24T21:00:00Z",
                pagination_complete=True,
                consistency_horizon_satisfied=True,
                qualified_exclusion_semantics=True,
                account_id="paper-1",
                environment="PAPER",
            )


if __name__ == "__main__":
    unittest.main()
