from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

from mvp.autotrade_mvp.bybit_v5 import (
    BYBIT_ORDER_HISTORY_NO_FILL_RETENTION_POLICY_ID,
    build_order_payload,
    prepare_order_submission,
    prepare_order_read_query,
    prepare_next_order_read_query,
    order_history_coverage_from_pages,
    prepare_execution_read_query,
    prepare_next_execution_read_query,
    execution_history_coverage_from_pages,
    prepare_wallet_read_query,
    parse_wallet_snapshot,
    provider_wallet_cash,
    prepare_position_read_query,
    parse_position_page,
    prepare_next_position_read_query,
    provider_position_quantities_from_pages,
    prepare_activity_read_query,
    parse_activity_page,
    prepare_next_activity_read_query,
    activity_coverage_from_pages,
    coverage_evidence,
    parse_execution_page,
    parse_executions,
    parse_order_page,
    working_orders_from_page,
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
from mvp.autotrade_mvp.reconciliation import (
    SnapshotConsistencyEvidence,
    UnknownSubmission,
    reconcile_account,
)


READ_AT = datetime(2026, 9, 24, 20, tzinfo=timezone.utc)


def read_capability(
    *,
    account_id="paper-1",
    environment="PAPER",
    instrument_version="BTCUSDT@v1",
    permission_scopes=("ORDER.READ",),
):
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
    additional_permission_scopes=(),
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
        http_status=200,
        response_bytes=raw,
        observed_at=READ_AT,
        provider_environment="TESTNET",
    )


def parse_test_executions(observation, **kwargs):
    return parse_executions(
        observation,
        provider_environment="TESTNET",
        **kwargs,
    )


def bound_execution_page_response(
    response,
    *,
    category="spot",
    client_order_id=None,
    symbol=None,
    cursor=None,
    limit=100,
    start_time_ms=None,
    end_time_ms=None,
    account_id="paper-1",
    environment="PAPER",
    provider_environment="TESTNET",
    capability=None,
):
    read_scope = (
        capability
        if capability is not None
        else read_capability(
            account_id=account_id,
            environment=environment,
        )
    )
    query = prepare_execution_read_query(
        capability=read_scope,
        at=READ_AT,
        category=category,
        client_order_id=client_order_id,
        symbol=symbol,
        cursor=cursor,
        limit=limit,
        start_time_ms=start_time_ms,
        end_time_ms=end_time_ms,
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
        http_status=200,
        response_bytes=raw,
        observed_at=READ_AT,
        provider_environment=provider_environment,
    )


def bound_order_response(
    response,
    *,
    surface="ORDER_HISTORY",
    category="spot",
    client_order_id=None,
    symbol=None,
    cursor=None,
    limit=50,
    start_time_ms=None,
    end_time_ms=None,
    account_id="paper-1",
    environment="PAPER",
    provider_environment="TESTNET",
    capability=None,
):
    read_scope = (
        capability
        if capability is not None
        else read_capability(
            account_id=account_id,
            environment=environment,
        )
    )
    query = prepare_order_read_query(
        capability=read_scope,
        at=READ_AT,
        surface=surface,
        category=category,
        client_order_id=client_order_id,
        symbol=symbol,
        cursor=cursor,
        limit=limit,
        start_time_ms=start_time_ms,
        end_time_ms=end_time_ms,
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
        http_status=200,
        response_bytes=raw,
        observed_at=READ_AT,
        provider_environment=provider_environment,
    )


def bound_wallet_response(
    response,
    *,
    coins=(),
    account_id="paper-1",
    environment="PAPER",
    provider_environment="TESTNET",
    capability=None,
):
    read_scope = (
        capability
        if capability is not None
        else read_capability(
            account_id=account_id,
            environment=environment,
            permission_scopes=("ACCOUNT.READ",),
        )
    )
    query = prepare_wallet_read_query(
        capability=read_scope,
        at=READ_AT,
        coins=coins,
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
        http_status=200,
        response_bytes=raw,
        observed_at=READ_AT,
        provider_environment=provider_environment,
    )


def bound_position_response(
    response,
    *,
    category="linear",
    symbol="BTCUSDT",
    settle_coin=None,
    base_coin=None,
    cursor=None,
    limit=200,
    account_id="paper-1",
    environment="PAPER",
    provider_environment="TESTNET",
    capability=None,
):
    read_scope = (
        capability
        if capability is not None
        else read_capability(
            account_id=account_id,
            environment=environment,
            permission_scopes=("ACCOUNT.READ",),
        )
    )
    query = prepare_position_read_query(
        capability=read_scope,
        at=READ_AT,
        category=category,
        symbol=symbol,
        settle_coin=settle_coin,
        base_coin=base_coin,
        cursor=cursor,
        limit=limit,
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
        http_status=200,
        response_bytes=raw,
        observed_at=READ_AT,
        provider_environment=provider_environment,
    )


def bound_activity_response(
    response,
    *,
    category=None,
    currency=None,
    activity_type=None,
    cursor=None,
    limit=50,
    start_time_ms=None,
    end_time_ms=None,
    account_id="paper-1",
    environment="PAPER",
    provider_environment="TESTNET",
    capability=None,
):
    read_scope = (
        capability
        if capability is not None
        else read_capability(
            account_id=account_id,
            environment=environment,
            permission_scopes=("ACCOUNT.READ",),
        )
    )
    query = prepare_activity_read_query(
        capability=read_scope,
        at=READ_AT,
        category=category,
        currency=currency,
        activity_type=activity_type,
        cursor=cursor,
        limit=limit,
        start_time_ms=start_time_ms,
        end_time_ms=end_time_ms,
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
        http_status=200,
        response_bytes=raw,
        observed_at=READ_AT,
        provider_environment=provider_environment,
    )


class BybitV5AdapterTests(unittest.TestCase):
    def test_execution_parser_requires_exact_provider_environment_scope(self):
        response = {
            "retCode": 0,
            "result": {
                "list": [{
                    "execId": "scope-provider-env",
                    "orderLinkId": "",
                    "symbol": "BTCUSDT",
                    "side": "Buy",
                    "execQty": "1",
                    "execPrice": "10",
                    "execFee": "0",
                    "feeCurrency": "USDT",
                    "execTime": "1790280000000",
                }]
            },
        }
        observation = bound_execution_response(response)
        with self.assertRaisesRegex(
            ProviderCoreError,
            "requires expected provider_environment",
        ):
            parse_executions(
                observation,
                instrument_versions={"BTCUSDT": "BTCUSDT@v1"},
            )
        with self.assertRaisesRegex(
            ProviderCoreError,
            "provider-environment mismatch",
        ):
            parse_executions(
                observation,
                provider_environment="DEMO",
                instrument_versions={"BTCUSDT": "BTCUSDT@v1"},
            )
        fill = parse_executions(
            observation,
            provider_environment="TESTNET",
            instrument_versions={"BTCUSDT": "BTCUSDT@v1"},
        )[0]
        self.assertEqual(fill.provider_environment, "TESTNET")

    def test_authenticated_read_requires_explicit_provider_environment(self):
        capability = read_capability()
        query = prepare_authenticated_read_query(
            capability=capability,
            surface=Surface.AUTHENTICATED_READ,
            endpoint="/v5/execution/list",
            query={"category": "spot", "limit": "100"},
            at=READ_AT,
            permission_scope="ORDER.READ",
        )
        with self.assertRaisesRegex(
            ProviderCoreError,
            "requires explicit provider_environment",
        ):
            observe_authenticated_json_response(
                query_binding=query,
                http_status=200,
                response_bytes=b'{"retCode":0,"result":{"list":[]}}',
                observed_at=READ_AT,
            )
        with self.assertRaisesRegex(
            ProviderCoreError,
            "must be MAINNET, TESTNET or DEMO",
        ):
            observe_authenticated_json_response(
                query_binding=query,
                http_status=200,
                response_bytes=b'{"retCode":0,"result":{"list":[]}}',
                observed_at=READ_AT,
                provider_environment="PAPER",
            )

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
        fills = parse_test_executions(
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
            parse_test_executions(
                bound_execution_response(
                    {"retCode": 0, "result": {"list": [base]}}
                ),
                instrument_versions={"BTCUSDT": "BTCUSDT@v1"},
            )

        documented = dict(base, side="Sell")
        observation = bound_execution_response(
            {"retCode": 0, "result": {"list": [documented]}}
        )
        fill = parse_test_executions(
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
        fills = parse_test_executions(
            observation,
            instrument_versions={"BTCUSDT": "BTCUSDT@v1"},
        )
        self.assertEqual((fills[0].account_id, fills[0].environment), ("account-a", "PAPER"))
        self.assertEqual(observation.query_binding.account_id, "account-a")

    def test_documented_linear_execution_requires_qualified_fee_currency(self):
        response = {"retCode": 0, "result": {"category": "linear", "list": [{
            "execId": "e0cbe81d-0f18-5866-9415-cf319b5dab3b", "orderLinkId": "",
            "symbol": "ETHPERP", "side": "Buy", "execQty": "0.1", "execPrice": "1190.15",
            "execFee": "0.071409", "feeCurrency": "", "extraFees": "",
            "execTime": "1672282722429",
        }]}}
        evidence = bound_execution_response(response, instrument_version="ETHPERP@v1")
        with self.assertRaisesRegex(ProviderCoreError, "fee currency is unresolved"):
            parse_test_executions(evidence, instrument_versions={"ETHPERP": "ETHPERP@v1"})
        fills = parse_test_executions(
            evidence,
            instrument_versions={"ETHPERP": "ETHPERP@v1"},
            qualified_fee_currencies={"ETHPERP@v1": "USDT"},
        )
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0].fee_amount, Decimal("0.071409"))
        self.assertEqual(fills[0].fee_currency, "USDT")

    def test_nonempty_extra_fees_cannot_silently_disappear(self):
        response = {"retCode": 0, "result": {"category": "spot", "list": [{
            "execId": "exec-extra-fee", "orderLinkId": "", "symbol": "BTCUSDT", "side": "Buy",
            "execQty": "0.01", "execPrice": "65000", "execFee": "0.5",
            "feeCurrency": "USDT",
            "extraFees": '[{"feeType":"tax","subFeeType":"regional"}]',
            "execTime": "1790280000000",
        }]}}
        with self.assertRaisesRegex(ProviderCoreError, "extraFees"):
            parse_test_executions(
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
                fills = parse_test_executions(
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
            parse_test_executions(
                bound_execution_response(conflict),
                instrument_versions={"BTCUSDT": "BTCUSDT@v1"},
            )
        unknown = {"retCode": 0, "result": {"list": [{
            "execId": "x", "orderLinkId": "", "symbol": "UNKNOWN", "side": "Buy", "execQty": "1",
            "execPrice": "10", "execFee": "0", "feeCurrency": "USD",
            "execTime": "1790280000000",
        }]}}
        with self.assertRaisesRegex(ProviderCoreError, "unmapped"):
            parse_test_executions(bound_execution_response(unknown), instrument_versions={})

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
            environment="PAPER",
            provider_environment="TESTNET",)
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
            environment="PAPER",
            provider_environment="TESTNET",)
        self.assertTrue(
            qualified.proves_absence_for(
                datetime(2026, 9, 24, 20, tzinfo=timezone.utc)
            )
        )


    def test_order_history_query_binds_exact_cursor_client_id_and_window(self):
        binding = prepare_order_read_query(
            capability=read_capability(),
            at=READ_AT,
            surface="ORDER_HISTORY",
            category="SPOT",
            client_order_id="client_123",
            cursor="opaque%3Dcursor",
            limit=50,
            start_time_ms=1790193600000,
            end_time_ms=1790280000000,
        )
        self.assertEqual(binding.endpoint, "/v5/order/history")
        self.assertEqual(binding.query["category"], "spot")
        self.assertEqual(binding.query["orderLinkId"], "client_123")
        self.assertEqual(binding.query["cursor"], "opaque%3Dcursor")
        self.assertEqual(binding.query["limit"], "50")
        self.assertEqual(binding.query["startTime"], "1790193600000")
        self.assertEqual(binding.query["endTime"], "1790280000000")

    def test_order_history_rejects_provider_window_over_seven_days(self):
        with self.assertRaisesRegex(ProviderCoreError, "seven days"):
            prepare_order_read_query(
                capability=read_capability(),
                at=READ_AT,
                surface="ORDER_HISTORY",
                category="spot",
                start_time_ms=0,
                end_time_ms=(7 * 24 * 60 * 60 * 1000) + 1,
            )

    def test_realtime_order_query_rejects_history_window(self):
        with self.assertRaisesRegex(ProviderCoreError, "does not accept history"):
            prepare_order_read_query(
                capability=read_capability(),
                at=READ_AT,
                surface="OPEN_ORDERS",
                category="spot",
                start_time_ms=1790193600000,
            )

    def test_order_page_preserves_exact_scope_state_and_cursor(self):
        observation = bound_order_response(
            {
                "retCode": 0,
                "retMsg": "OK",
                "result": {
                    "category": "spot",
                    "nextPageCursor": "cursor%3Dnext%26page%3D2",
                    "list": [
                        {
                            "orderId": "provider-order-1",
                            "orderLinkId": "client_123",
                            "symbol": "BTCUSDT",
                            "orderStatus": "Filled",
                            "leavesQty": "0",
                            "createdTime": "1790279999000",
                            "updatedTime": "1790280000000",
                        }
                    ],
                },
                "time": 1790280000000,
            },
            client_order_id="client_123",
        )
        page = parse_order_page(observation)
        self.assertEqual(page.surface, "ORDER_HISTORY")
        self.assertEqual(page.category, "spot")
        self.assertEqual(page.environment, "PAPER")
        self.assertEqual(page.provider_environment, "TESTNET")
        self.assertEqual(page.next_cursor, "cursor%3Dnext%26page%3D2")
        self.assertFalse(page.pagination_complete)
        self.assertEqual(len(page.orders), 1)
        order = page.orders[0]
        self.assertEqual(order.provider_order_id, "provider-order-1")
        self.assertEqual(order.client_order_id, "client_123")
        self.assertEqual(order.order_status, "Filled")
        self.assertEqual(order.created_at, "2026-09-24T19:59:59Z")
        self.assertEqual(order.updated_at, "2026-09-24T20:00:00Z")
        self.assertEqual(order.evidence_ref, observation.evidence_ref)

    def test_order_page_empty_cursor_is_terminal_but_not_absence_proof(self):
        page = parse_order_page(
            bound_order_response(
                {
                    "retCode": 0,
                    "result": {
                        "category": "spot",
                        "nextPageCursor": "",
                        "list": [],
                    },
                }
            )
        )
        self.assertTrue(page.pagination_complete)
        self.assertEqual(page.orders, ())

    def test_order_page_rejects_category_or_client_identity_mismatch(self):
        with self.assertRaisesRegex(ProviderCoreError, "category"):
            parse_order_page(
                bound_order_response(
                    {
                        "retCode": 0,
                        "result": {
                            "category": "linear",
                            "nextPageCursor": "",
                            "list": [],
                        },
                    }
                )
            )

        with self.assertRaisesRegex(ProviderCoreError, "orderLinkId"):
            parse_order_page(
                bound_order_response(
                    {
                        "retCode": 0,
                        "result": {
                            "category": "spot",
                            "nextPageCursor": "",
                            "list": [
                                {
                                    "orderId": "provider-order-wrong-link",
                                    "orderLinkId": "other_client",
                                    "symbol": "BTCUSDT",
                                    "orderStatus": "New",
                                    "leavesQty": "1",
                                    "createdTime": "1790279999000",
                                    "updatedTime": "1790280000000",
                                }
                            ],
                        },
                    },
                    client_order_id="expected_client",
                )
            )

    def test_order_pages_enforce_effective_symbol_filter(self):
        row = {
            "orderId": "provider-order-symbol",
            "orderLinkId": "",
            "symbol": "ETHUSDT",
            "orderStatus": "New",
            "leavesQty": "1",
            "createdTime": "1790279999000",
            "updatedTime": "1790280000000",
        }
        for surface in ("OPEN_ORDERS", "ORDER_HISTORY"):
            with self.subTest(surface=surface), self.assertRaisesRegex(
                ProviderCoreError,
                "symbol response does not match",
            ):
                parse_order_page(
                    bound_order_response(
                        {
                            "retCode": 0,
                            "result": {
                                "category": "spot",
                                "nextPageCursor": "",
                                "list": [row],
                            },
                        },
                        surface=surface,
                        symbol="BTCUSDT",
                    )
                )

        identity_row = dict(row)
        identity_row["orderLinkId"] = "client_effective"
        page = parse_order_page(
            bound_order_response(
                {
                    "retCode": 0,
                    "result": {
                        "category": "spot",
                        "nextPageCursor": "",
                        "list": [identity_row],
                    },
                },
                client_order_id="client_effective",
                symbol="BTCUSDT",
            )
        )
        self.assertEqual(page.orders[0].symbol, "ETHUSDT")

    def test_order_page_rejects_conflicting_duplicate_order_identity(self):
        with self.assertRaisesRegex(ProviderCoreError, "conflicting state"):
            parse_order_page(
                bound_order_response(
                    {
                        "retCode": 0,
                        "result": {
                            "category": "spot",
                            "nextPageCursor": "",
                            "list": [
                                {
                                    "orderId": "same-order",
                                    "orderLinkId": "",
                                    "symbol": "BTCUSDT",
                                    "orderStatus": "New",
                                    "leavesQty": "1",
                                    "createdTime": "1790279999000",
                                    "updatedTime": "1790280000000",
                                },
                                {
                                    "orderId": "same-order",
                                    "orderLinkId": "",
                                    "symbol": "BTCUSDT",
                                    "orderStatus": "Cancelled",
                                    "leavesQty": "1",
                                    "createdTime": "1790279999000",
                                    "updatedTime": "1790280000000",
                                },
                            ],
                        },
                    }
                )
            )

    def test_order_page_rejects_non_order_endpoint_provenance(self):
        with self.assertRaisesRegex(ProviderCoreError, "unsupported exact"):
            parse_order_page(
                bound_execution_response(
                    {"retCode": 0, "result": {"list": []}}
                )
            )


    def test_next_order_page_preserves_exact_base_query_and_capability_scope(self):
        capability = read_capability()
        first = bound_order_response(
            {
                "retCode": 0,
                "result": {
                    "category": "spot",
                    "nextPageCursor": "cursor-next",
                    "list": [],
                },
            },
            client_order_id="client_123",
            start_time_ms=1790193600000,
            end_time_ms=1790280000000,
            capability=capability,
        )
        binding = prepare_next_order_read_query(
            observation=first,
            capability=capability,
            at=READ_AT,
        )
        self.assertIsNotNone(binding)
        self.assertEqual(binding.query["cursor"], "cursor-next")
        self.assertEqual(binding.query["orderLinkId"], "client_123")
        self.assertEqual(binding.query["startTime"], "1790193600000")
        self.assertEqual(binding.query["endTime"], "1790280000000")

        terminal = bound_order_response(
            {
                "retCode": 0,
                "result": {
                    "category": "spot",
                    "nextPageCursor": "",
                    "list": [],
                },
            },
            start_time_ms=1790193600000,
            end_time_ms=1790280000000,
        )
        self.assertIsNone(
            prepare_next_order_read_query(
                observation=terminal,
                capability=read_capability(),
                at=READ_AT,
            )
        )

    def test_order_history_coverage_requires_contiguous_complete_cursor_chain(self):
        common = {
            "start_time_ms": 1790193600000,
            "end_time_ms": 1790280000000,
        }
        capability = read_capability()
        first = bound_order_response(
            {
                "retCode": 0,
                "result": {
                    "category": "spot",
                    "nextPageCursor": "page-2",
                    "list": [],
                },
            },
            capability=capability,
            **common,
        )
        second = bound_order_response(
            {
                "retCode": 0,
                "result": {
                    "category": "spot",
                    "nextPageCursor": "",
                    "list": [],
                },
            },
            cursor="page-2",
            capability=capability,
            **common,
        )
        coverage = order_history_coverage_from_pages(
            (first, second),
            consistency_horizon_satisfied=True,
        )
        self.assertTrue(coverage.pagination_complete)
        self.assertTrue(coverage.consistency_horizon_satisfied)
        self.assertFalse(coverage.provider_semantics_exclude_execution)
        self.assertEqual(coverage.surface, "ORDER_HISTORY")
        self.assertEqual(coverage.provider_environment, "TESTNET")
        self.assertEqual(coverage.coverage_start, "2026-09-23T20:00:00.000Z")
        self.assertEqual(coverage.coverage_end, "2026-09-24T20:00:00.000Z")

    def test_order_history_exclusion_requires_exact_retention_policy(self):
        observation = bound_order_response(
            {
                "retCode": 0,
                "result": {
                    "category": "spot",
                    "nextPageCursor": "",
                    "list": [],
                },
            },
            start_time_ms=1790193600000,
            end_time_ms=1790280000000,
        )
        with self.assertRaisesRegex(
            ProviderCoreError,
            "require the exact qualified retention policy",
        ):
            order_history_coverage_from_pages(
                (observation,),
                consistency_horizon_satisfied=True,
                qualified_exclusion_semantics=True,
            )
        with self.assertRaisesRegex(
            ProviderCoreError,
            "unknown Bybit order-history retention qualification policy",
        ):
            order_history_coverage_from_pages(
                (observation,),
                consistency_horizon_satisfied=True,
                qualified_exclusion_semantics=True,
                retention_policy_id="BYBIT_V5_ORDER_HISTORY_UNVERSIONED",
            )

    def test_order_history_absence_semantics_stop_at_no_fill_retention_boundary(self):
        def terminal_history(*, start_time_ms, end_time_ms):
            observation = bound_order_response(
                {
                    "retCode": 0,
                    "result": {
                        "category": "spot",
                        "nextPageCursor": "",
                        "list": [],
                    },
                },
                start_time_ms=start_time_ms,
                end_time_ms=end_time_ms,
            )
            return order_history_coverage_from_pages(
                (observation,),
                consistency_horizon_satisfied=True,
                qualified_exclusion_semantics=True,
                retention_policy_id=(
                    BYBIT_ORDER_HISTORY_NO_FILL_RETENTION_POLICY_ID
                ),
            )

        exact_boundary = terminal_history(
            start_time_ms=1790193600000,
            end_time_ms=1790280000000,
        )
        self.assertTrue(exact_boundary.pagination_complete)
        self.assertTrue(exact_boundary.provider_semantics_exclude_execution)

        one_millisecond_too_old = terminal_history(
            start_time_ms=1790193599999,
            end_time_ms=1790280000000,
        )
        self.assertTrue(one_millisecond_too_old.pagination_complete)
        self.assertFalse(
            one_millisecond_too_old.provider_semantics_exclude_execution
        )

        older_than_seven_days = terminal_history(
            start_time_ms=1789588800000,
            end_time_ms=1789675200000,
        )
        self.assertTrue(older_than_seven_days.pagination_complete)
        self.assertFalse(
            older_than_seven_days.provider_semantics_exclude_execution
        )

        future_end = terminal_history(
            start_time_ms=1790276400000,
            end_time_ms=1790280000001,
        )
        self.assertTrue(future_end.pagination_complete)
        self.assertFalse(future_end.provider_semantics_exclude_execution)

    def test_offline_recovery_past_no_fill_retention_stays_unknown(self):
        observation = bound_order_response(
            {
                "retCode": 0,
                "result": {
                    "category": "spot",
                    "nextPageCursor": "",
                    "list": [],
                },
            },
            start_time_ms=1790190000000,
            end_time_ms=1790280000000,
            client_order_id="client_retention_expired",
        )
        order_history = order_history_coverage_from_pages(
            (observation,),
            consistency_horizon_satisfied=True,
            qualified_exclusion_semantics=True,
            retention_policy_id=BYBIT_ORDER_HISTORY_NO_FILL_RETENTION_POLICY_ID,
        )
        self.assertFalse(order_history.provider_semantics_exclude_execution)

        common = {
            "account_id": "paper-1",
            "environment": "PAPER",
            "provider_environment": "TESTNET",
            "coverage_start": "2026-09-23T19:00:00Z",
            "coverage_end": "2026-09-24T20:00:00Z",
            "pagination_complete": True,
            "consistency_horizon_satisfied": True,
            "qualified_exclusion_semantics": True,
        }
        absence_coverage = (
            coverage_evidence(surface="OPEN_ORDERS", **common),
            order_history,
            coverage_evidence(surface="EXECUTIONS", **common),
            coverage_evidence(surface="ACTIVITIES", **common),
        )
        unknown = UnknownSubmission.create(
            attempt_id="attempt-retention-expired",
            intent_id="intent-retention-expired",
            provider_id="BYBIT",
            account_id="paper-1",
            environment="PAPER",
            provider_environment="TESTNET",
            client_order_id="client_retention_expired",
            started_at="2026-09-23T19:00:00Z",
        )
        result = reconcile_account(
            provider_id="BYBIT",
            account_id="paper-1",
            environment="PAPER",
            provider_environment="TESTNET",
            local_cash={},
            provider_cash={},
            local_positions={},
            provider_positions={},
            local_execution_ids=(),
            provider_fills=(),
            snapshot_consistency=SnapshotConsistencyEvidence(
                provider_id="BYBIT",
                account_id="paper-1",
                environment="PAPER",
                provider_environment="TESTNET",
                mode="ATOMIC",
                query_started_at="2026-09-24T19:59:00Z",
                query_completed_at="2026-09-24T20:00:00Z",
            ),
            unknown_submissions=(unknown,),
            searched_client_order_ids=("client_retention_expired",),
            coverage_start="2026-09-23T19:00:00Z",
            coverage_end="2026-09-24T20:00:00Z",
            pagination_complete=True,
            absence_coverage=absence_coverage,
        )
        resolution = result.submission_resolutions[0]
        self.assertEqual(resolution.outcome, "UNKNOWN")
        self.assertEqual(
            resolution.evidence_reason,
            "absence_surface_evidence_incomplete",
        )
        self.assertTrue(result.blocks_new_risk)

    def test_order_history_coverage_rejects_mixed_capability_snapshots(self):
        first_capability = read_capability()
        second_capability = read_capability()
        self.assertNotEqual(
            first_capability.snapshot_id,
            second_capability.snapshot_id,
        )
        common = {
            "start_time_ms": 1790193600000,
            "end_time_ms": 1790280000000,
        }
        first = bound_order_response(
            {
                "retCode": 0,
                "result": {
                    "category": "spot",
                    "nextPageCursor": "page-2",
                    "list": [],
                },
            },
            capability=first_capability,
            **common,
        )
        second = bound_order_response(
            {
                "retCode": 0,
                "result": {
                    "category": "spot",
                    "nextPageCursor": "",
                    "list": [],
                },
            },
            cursor="page-2",
            capability=second_capability,
            **common,
        )
        with self.assertRaisesRegex(
            ProviderCoreError,
            "authenticated-read capability scope",
        ):
            order_history_coverage_from_pages(
                (first, second),
                consistency_horizon_satisfied=True,
            )

    def test_order_history_coverage_rejects_skipped_or_partial_cursor_chain(self):
        common = {
            "start_time_ms": 1790193600000,
            "end_time_ms": 1790280000000,
        }
        capability = read_capability()
        first = bound_order_response(
            {
                "retCode": 0,
                "result": {
                    "category": "spot",
                    "nextPageCursor": "page-2",
                    "list": [],
                },
            },
            capability=capability,
            **common,
        )
        wrong_second = bound_order_response(
            {
                "retCode": 0,
                "result": {
                    "category": "spot",
                    "nextPageCursor": "",
                    "list": [],
                },
            },
            cursor="page-3",
            capability=capability,
            **common,
        )
        with self.assertRaisesRegex(ProviderCoreError, "cursor chain"):
            order_history_coverage_from_pages(
                (first, wrong_second),
                consistency_horizon_satisfied=True,
            )

        with self.assertRaisesRegex(ProviderCoreError, "incomplete"):
            order_history_coverage_from_pages(
                (first,),
                consistency_horizon_satisfied=True,
            )

    def test_order_history_coverage_rejects_mid_chain_start_and_implicit_window(self):
        mid_chain = bound_order_response(
            {
                "retCode": 0,
                "result": {
                    "category": "spot",
                    "nextPageCursor": "",
                    "list": [],
                },
            },
            cursor="page-2",
            start_time_ms=1790193600000,
            end_time_ms=1790280000000,
        )
        with self.assertRaisesRegex(ProviderCoreError, "first page"):
            order_history_coverage_from_pages(
                (mid_chain,),
                consistency_horizon_satisfied=True,
            )

        implicit = bound_order_response(
            {
                "retCode": 0,
                "result": {
                    "category": "spot",
                    "nextPageCursor": "",
                    "list": [],
                },
            }
        )
        with self.assertRaisesRegex(ProviderCoreError, "explicit startTime"):
            order_history_coverage_from_pages(
                (implicit,),
                consistency_horizon_satisfied=True,
            )


    def test_execution_read_query_binds_exact_identity_window_and_cursor(self):
        binding = prepare_execution_read_query(
            capability=read_capability(),
            at=READ_AT,
            category="SPOT",
            client_order_id="client_exec",
            symbol="BTCUSDT",
            cursor="exec%3Acursor",
            limit=100,
            start_time_ms=1790193600000,
            end_time_ms=1790280000000,
        )
        self.assertEqual(binding.endpoint, "/v5/execution/list")
        self.assertEqual(binding.query["category"], "spot")
        self.assertEqual(binding.query["orderLinkId"], "client_exec")
        self.assertEqual(binding.query["symbol"], "BTCUSDT")
        self.assertEqual(binding.query["cursor"], "exec%3Acursor")
        self.assertEqual(binding.query["limit"], "100")
        with self.assertRaisesRegex(ProviderCoreError, "seven days"):
            prepare_execution_read_query(
                capability=read_capability(),
                at=READ_AT,
                category="spot",
                start_time_ms=0,
                end_time_ms=(7 * 24 * 60 * 60 * 1000) + 1,
            )

    def test_execution_page_preserves_fill_provenance_and_exact_cursor(self):
        observation = bound_execution_page_response(
            {
                "retCode": 0,
                "result": {
                    "category": "spot",
                    "nextPageCursor": "exec-next",
                    "list": [
                        {
                            "execId": "exec-page-1",
                            "orderLinkId": "client_exec",
                            "symbol": "BTCUSDT",
                            "side": "Buy",
                            "execQty": "0.5",
                            "execPrice": "65000",
                            "execFee": "1.25",
                            "feeCurrency": "USDT",
                            "execTime": "1790280000000",
                        }
                    ],
                },
            },
            client_order_id="client_exec",
            start_time_ms=1790193600000,
            end_time_ms=1790280000000,
        )
        page = parse_execution_page(
            observation,
            provider_environment="TESTNET",
            instrument_versions={"BTCUSDT": "BTCUSDT@v1"},
        )
        self.assertEqual(page.category, "spot")
        self.assertEqual(page.provider_environment, "TESTNET")
        self.assertEqual(page.next_cursor, "exec-next")
        self.assertFalse(page.pagination_complete)
        self.assertEqual(len(page.fills), 1)
        self.assertEqual(page.fills[0].provider_execution_id, "exec-page-1")
        self.assertEqual(page.fills[0].evidence_refs, (observation.evidence_ref,))

    def test_execution_page_rejects_category_or_client_identity_mismatch(self):
        base_row = {
            "execId": "exec-mismatch",
            "orderLinkId": "wrong_client",
            "symbol": "BTCUSDT",
            "side": "Buy",
            "execQty": "1",
            "execPrice": "10",
            "execFee": "0",
            "feeCurrency": "USDT",
            "execTime": "1790280000000",
        }
        with self.assertRaisesRegex(ProviderCoreError, "category"):
            parse_execution_page(
                bound_execution_page_response(
                    {
                        "retCode": 0,
                        "result": {
                            "category": "linear",
                            "nextPageCursor": "",
                            "list": [],
                        },
                    }
                ),
                provider_environment="TESTNET",
                instrument_versions={"BTCUSDT": "BTCUSDT@v1"},
            )
        with self.assertRaisesRegex(ProviderCoreError, "orderLinkId"):
            parse_execution_page(
                bound_execution_page_response(
                    {
                        "retCode": 0,
                        "result": {
                            "category": "spot",
                            "nextPageCursor": "",
                            "list": [base_row],
                        },
                    },
                    client_order_id="expected_client",
                ),
                provider_environment="TESTNET",
                instrument_versions={"BTCUSDT": "BTCUSDT@v1"},
            )

    def test_execution_page_enforces_effective_symbol_filter(self):
        row = {
            "execId": "exec-symbol-filter",
            "orderLinkId": "",
            "symbol": "ETHUSDT",
            "side": "Buy",
            "execQty": "1",
            "execPrice": "10",
            "execFee": "0",
            "feeCurrency": "USDT",
            "execTime": "1790280000000",
        }
        with self.assertRaisesRegex(
            ProviderCoreError,
            "symbol response does not match",
        ):
            parse_execution_page(
                bound_execution_page_response(
                    {
                        "retCode": 0,
                        "result": {
                            "category": "spot",
                            "nextPageCursor": "",
                            "list": [row],
                        },
                    },
                    symbol="BTCUSDT",
                ),
                provider_environment="TESTNET",
                instrument_versions={"ETHUSDT": "ETHUSDT@v1"},
            )

        identity_row = dict(row)
        identity_row["orderLinkId"] = "client_exec_effective"
        page = parse_execution_page(
            bound_execution_page_response(
                {
                    "retCode": 0,
                    "result": {
                        "category": "spot",
                        "nextPageCursor": "",
                        "list": [identity_row],
                    },
                },
                client_order_id="client_exec_effective",
                symbol="BTCUSDT",
            ),
            provider_environment="TESTNET",
            instrument_versions={"ETHUSDT": "ETHUSDT@v1"},
        )
        self.assertEqual(page.fills[0].instrument, "ETHUSDT@v1")

    def test_next_execution_page_preserves_exact_capability_and_query(self):
        capability = read_capability()
        first = bound_execution_page_response(
            {
                "retCode": 0,
                "result": {
                    "category": "spot",
                    "nextPageCursor": "exec-page-2",
                    "list": [],
                },
            },
            client_order_id="client_exec",
            start_time_ms=1790193600000,
            end_time_ms=1790280000000,
            capability=capability,
        )
        next_binding = prepare_next_execution_read_query(
            observation=first,
            capability=capability,
            provider_environment="TESTNET",
            instrument_versions={"BTCUSDT": "BTCUSDT@v1"},
            at=READ_AT,
        )
        self.assertIsNotNone(next_binding)
        self.assertEqual(next_binding.query["cursor"], "exec-page-2")
        self.assertEqual(next_binding.query["orderLinkId"], "client_exec")
        self.assertEqual(next_binding.query["startTime"], "1790193600000")
        self.assertEqual(next_binding.query["endTime"], "1790280000000")

    def test_execution_history_coverage_is_derived_from_complete_cursor_chain(self):
        common = {
            "start_time_ms": 1790193600000,
            "end_time_ms": 1790280000000,
        }
        capability = read_capability()
        first = bound_execution_page_response(
            {
                "retCode": 0,
                "result": {
                    "category": "spot",
                    "nextPageCursor": "exec-page-2",
                    "list": [
                        {
                            "execId": "exec-cover-1",
                            "orderLinkId": "",
                            "symbol": "BTCUSDT",
                            "side": "Buy",
                            "execQty": "1",
                            "execPrice": "10",
                            "execFee": "0",
                            "feeCurrency": "USDT",
                            "execTime": "1790279999000",
                        }
                    ],
                },
            },
            capability=capability,
            **common,
        )
        second = bound_execution_page_response(
            {
                "retCode": 0,
                "result": {
                    "category": "spot",
                    "nextPageCursor": "",
                    "list": [
                        {
                            "execId": "exec-cover-2",
                            "orderLinkId": "",
                            "symbol": "BTCUSDT",
                            "side": "Sell",
                            "execQty": "1",
                            "execPrice": "11",
                            "execFee": "0",
                            "feeCurrency": "USDT",
                            "execTime": "1790279998000",
                        }
                    ],
                },
            },
            cursor="exec-page-2",
            capability=capability,
            **common,
        )
        coverage = execution_history_coverage_from_pages(
            (first, second),
            provider_environment="TESTNET",
            instrument_versions={"BTCUSDT": "BTCUSDT@v1"},
            consistency_horizon_satisfied=True,
        )
        self.assertTrue(coverage.pagination_complete)
        self.assertFalse(coverage.provider_semantics_exclude_execution)
        self.assertEqual(coverage.provider_environment, "TESTNET")
        self.assertEqual(coverage.coverage_start, "2026-09-23T20:00:00.000Z")
        self.assertEqual(coverage.coverage_end, "2026-09-24T20:00:00.000Z")

        with self.assertRaisesRegex(ProviderCoreError, "incomplete"):
            execution_history_coverage_from_pages(
                (first,),
                provider_environment="TESTNET",
                instrument_versions={"BTCUSDT": "BTCUSDT@v1"},
                consistency_horizon_satisfied=True,
            )

    def test_execution_history_coverage_rejects_mixed_capability_snapshots(self):
        first_capability = read_capability()
        second_capability = read_capability()
        self.assertNotEqual(
            first_capability.snapshot_id,
            second_capability.snapshot_id,
        )
        common = {
            "start_time_ms": 1790193600000,
            "end_time_ms": 1790280000000,
        }
        first = bound_execution_page_response(
            {
                "retCode": 0,
                "result": {
                    "category": "spot",
                    "nextPageCursor": "exec-page-2",
                    "list": [],
                },
            },
            capability=first_capability,
            **common,
        )
        second = bound_execution_page_response(
            {
                "retCode": 0,
                "result": {
                    "category": "spot",
                    "nextPageCursor": "",
                    "list": [],
                },
            },
            cursor="exec-page-2",
            capability=second_capability,
            **common,
        )
        with self.assertRaisesRegex(
            ProviderCoreError,
            "authenticated-read capability scope",
        ):
            execution_history_coverage_from_pages(
                (first, second),
                provider_environment="TESTNET",
                instrument_versions={"BTCUSDT": "BTCUSDT@v1"},
                consistency_horizon_satisfied=True,
            )


    def test_realtime_linear_query_requires_documented_symbol_scope(self):
        with self.assertRaisesRegex(ProviderCoreError, "requires symbol"):
            prepare_order_read_query(
                capability=read_capability(),
                at=READ_AT,
                surface="OPEN_ORDERS",
                category="linear",
            )
        binding = prepare_order_read_query(
            capability=read_capability(),
            at=READ_AT,
            surface="OPEN_ORDERS",
            category="linear",
            symbol="BTCUSDT",
        )
        self.assertEqual(binding.query["symbol"], "BTCUSDT")
        self.assertEqual(binding.query["openOnly"], "0")

    def test_open_order_page_projects_documented_working_states_into_reconciliation(self):
        page = parse_order_page(
            bound_order_response(
                {
                    "retCode": 0,
                    "result": {
                        "category": "spot",
                        "nextPageCursor": "",
                        "list": [
                            {
                                "orderId": "working-new",
                                "orderLinkId": "client-new",
                                "symbol": "BTCUSDT",
                                "orderStatus": "New",
                                "leavesQty": "2.5",
                                "createdTime": "1790279999000",
                                "updatedTime": "1790280000000",
                            },
                            {
                                "orderId": "working-partial",
                                "orderLinkId": "client-partial",
                                "symbol": "BTCUSDT",
                                "orderStatus": "PartiallyFilled",
                                "leavesQty": "0.25",
                                "createdTime": "1790279999000",
                                "updatedTime": "1790280000000",
                            },
                            {
                                "orderId": "working-trigger",
                                "orderLinkId": "",
                                "symbol": "BTCUSDT",
                                "orderStatus": "Untriggered",
                                "leavesQty": "1",
                                "createdTime": "1790279999000",
                                "updatedTime": "1790280000000",
                            },
                            {
                                "orderId": "terminal-filled",
                                "orderLinkId": "client-filled",
                                "symbol": "BTCUSDT",
                                "orderStatus": "Filled",
                                "leavesQty": "0",
                                "createdTime": "1790279999000",
                                "updatedTime": "1790280000000",
                            },
                        ],
                    },
                },
                surface="OPEN_ORDERS",
            )
        )
        working = working_orders_from_page(
            page,
            instrument_versions={"BTCUSDT": "BTCUSDT@v1"},
        )
        self.assertEqual(
            tuple(item.provider_order_id for item in working),
            ("working-new", "working-partial", "working-trigger"),
        )
        self.assertEqual(
            tuple(item.remaining_quantity for item in working),
            (Decimal("2.5"), Decimal("0.25"), Decimal("1")),
        )
        self.assertTrue(all(item.provider_id == "BYBIT" for item in working))
        self.assertTrue(all(item.provider_environment == "TESTNET" for item in working))
        self.assertTrue(all(item.instrument == "BTCUSDT@v1" for item in working))

    def test_working_order_projection_rejects_history_unknown_symbol_and_invalid_open_remainder(self):
        history = parse_order_page(
            bound_order_response(
                {
                    "retCode": 0,
                    "result": {
                        "category": "spot",
                        "nextPageCursor": "",
                        "list": [],
                    },
                }
            )
        )
        with self.assertRaisesRegex(ProviderCoreError, "OPEN_ORDERS"):
            working_orders_from_page(
                history,
                instrument_versions={"BTCUSDT": "BTCUSDT@v1"},
            )

        open_page = parse_order_page(
            bound_order_response(
                {
                    "retCode": 0,
                    "result": {
                        "category": "spot",
                        "nextPageCursor": "",
                        "list": [
                            {
                                "orderId": "working-unmapped",
                                "orderLinkId": "",
                                "symbol": "UNKNOWN",
                                "orderStatus": "New",
                                "leavesQty": "1",
                                "createdTime": "1790279999000",
                                "updatedTime": "1790280000000",
                            }
                        ],
                    },
                },
                surface="OPEN_ORDERS",
            )
        )
        with self.assertRaisesRegex(ProviderCoreError, "unmapped"):
            working_orders_from_page(open_page, instrument_versions={})

        with self.assertRaisesRegex(ProviderCoreError, "positive remaining"):
            parse_order_page(
                bound_order_response(
                    {
                        "retCode": 0,
                        "result": {
                            "category": "spot",
                            "nextPageCursor": "",
                            "list": [
                                {
                                    "orderId": "invalid-open-zero",
                                    "orderLinkId": "",
                                    "symbol": "BTCUSDT",
                                    "orderStatus": "New",
                                    "leavesQty": "0",
                                    "createdTime": "1790279999000",
                                    "updatedTime": "1790280000000",
                                }
                            ],
                        },
                    },
                    surface="OPEN_ORDERS",
                )
            )

    def test_unknown_future_order_status_fails_closed(self):
        with self.assertRaisesRegex(ProviderCoreError, "unsupported Bybit order status"):
            parse_order_page(
                bound_order_response(
                    {
                        "retCode": 0,
                        "result": {
                            "category": "spot",
                            "nextPageCursor": "",
                            "list": [
                                {
                                    "orderId": "future-status",
                                    "orderLinkId": "",
                                    "symbol": "BTCUSDT",
                                    "orderStatus": "FutureProviderStatus",
                                    "leavesQty": "1",
                                    "createdTime": "1790279999000",
                                    "updatedTime": "1790280000000",
                                }
                            ],
                        },
                    },
                    surface="OPEN_ORDERS",
                )
            )


    def test_exact_bybit_working_page_resolves_unknown_send_without_retry(self):
        page = parse_order_page(
            bound_order_response(
                {
                    "retCode": 0,
                    "result": {
                        "category": "spot",
                        "nextPageCursor": "",
                        "list": [
                            {
                                "orderId": "provider-unknown-resolved",
                                "orderLinkId": "client_unknown_resolved",
                                "symbol": "BTCUSDT",
                                "orderStatus": "New",
                                "leavesQty": "0.4",
                                "createdTime": "1790279999000",
                                "updatedTime": "1790280000000",
                            }
                        ],
                    },
                },
                surface="OPEN_ORDERS",
                client_order_id="client_unknown_resolved",
            )
        )
        working = working_orders_from_page(
            page,
            instrument_versions={"BTCUSDT": "BTCUSDT@v1"},
        )
        unknown = UnknownSubmission.create(
            attempt_id="attempt-bybit-unknown",
            intent_id="intent-bybit-unknown",
            provider_id="BYBIT",
            account_id="paper-1",
            environment="PAPER",
            provider_environment="TESTNET",
            client_order_id="client_unknown_resolved",
            started_at="2026-09-24T19:59:00Z",
        )
        result = reconcile_account(
            provider_id="BYBIT",
            account_id="paper-1",
            environment="PAPER",
            provider_environment="TESTNET",
            local_cash={},
            provider_cash={},
            local_positions={},
            provider_positions={},
            local_execution_ids=(),
            provider_fills=(),
            local_working_client_order_ids=("client_unknown_resolved",),
            provider_working_orders=working,
            snapshot_consistency=SnapshotConsistencyEvidence(
                provider_id="BYBIT",
                account_id="paper-1",
                environment="PAPER",
                provider_environment="TESTNET",
                mode="ATOMIC",
                query_started_at="2026-09-24T20:00:00Z",
                query_completed_at="2026-09-24T20:00:00Z",
            ),
            unknown_submissions=(unknown,),
            searched_client_order_ids=("client_unknown_resolved",),
            coverage_start="2026-09-24T19:59:00Z",
            coverage_end="2026-09-24T20:01:00Z",
            pagination_complete=True,
        )
        resolution = result.submission_resolutions[0]
        self.assertEqual(resolution.outcome, "OBSERVED_WORKING_ORDER")
        self.assertEqual(
            resolution.provider_order_ids,
            ("provider-unknown-resolved",),
        )
        self.assertTrue(result.complete)
        self.assertFalse(result.blocks_new_risk)


    def test_wallet_query_and_snapshot_preserve_unified_scope(self):
        capability = read_capability(permission_scopes=("ACCOUNT.READ",))
        binding = prepare_wallet_read_query(
            capability=capability,
            at=READ_AT,
            coins=("USDT", "BTC"),
        )
        self.assertEqual(binding.endpoint, "/v5/account/wallet-balance")
        self.assertEqual(binding.permission_scope, "ACCOUNT.READ")
        self.assertEqual(binding.query["accountType"], "UNIFIED")
        self.assertEqual(binding.query["coin"], "USDT,BTC")

        snapshot = parse_wallet_snapshot(
            bound_wallet_response(
                {
                    "retCode": 0,
                    "result": {
                        "list": [
                            {
                                "accountType": "UNIFIED",
                                "coin": [
                                    {
                                        "coin": "USDT",
                                        "walletBalance": "100.25",
                                        "equity": "100.25",
                                        "borrowAmount": "0",
                                        "spotBorrow": "0",
                                        "locked": "2.5",
                                        "unrealisedPnl": "0",
                                    },
                                    {
                                        "coin": "BTC",
                                        "walletBalance": "0.01",
                                        "equity": "0.01",
                                        "borrowAmount": "0",
                                        "spotBorrow": "0",
                                        "locked": "0",
                                        "unrealisedPnl": "0",
                                    },
                                ],
                            }
                        ]
                    },
                },
                coins=("USDT", "BTC"),
                capability=capability,
            )
        )
        self.assertEqual(snapshot.account_type, "UNIFIED")
        self.assertEqual(snapshot.provider_environment, "TESTNET")
        self.assertEqual(
            dict(provider_wallet_cash(snapshot)),
            {"BTC": Decimal("0.01"), "USDT": Decimal("100.25")},
        )
        self.assertEqual(snapshot.coins[1].locked, Decimal("2.5"))

    def test_wallet_snapshot_rejects_out_of_scope_coin_and_unrepresented_liability(self):
        with self.assertRaisesRegex(ProviderCoreError, "outside exact query scope"):
            parse_wallet_snapshot(
                bound_wallet_response(
                    {
                        "retCode": 0,
                        "result": {
                            "list": [
                                {
                                    "accountType": "UNIFIED",
                                    "coin": [
                                        {
                                            "coin": "BTC",
                                            "walletBalance": "1",
                                            "equity": "1",
                                            "borrowAmount": "0",
                                            "spotBorrow": "0",
                                            "locked": "0",
                                            "unrealisedPnl": "0",
                                        }
                                    ],
                                }
                            ]
                        },
                    },
                    coins=("USDT",),
                )
            )

        borrowed = parse_wallet_snapshot(
            bound_wallet_response(
                {
                    "retCode": 0,
                    "result": {
                        "list": [
                            {
                                "accountType": "UNIFIED",
                                "coin": [
                                    {
                                        "coin": "USDT",
                                        "walletBalance": "100",
                                        "equity": "90",
                                        "borrowAmount": "10",
                                        "spotBorrow": "4",
                                        "locked": "0",
                                        "unrealisedPnl": "0",
                                    }
                                ],
                            }
                        ]
                    },
                }
            )
        )
        with self.assertRaisesRegex(ProviderCoreError, "liabilities"):
            provider_wallet_cash(borrowed)

        with self.assertRaisesRegex(ProviderCoreError, "spotBorrow"):
            parse_wallet_snapshot(
                bound_wallet_response(
                    {
                        "retCode": 0,
                        "result": {
                            "list": [
                                {
                                    "accountType": "UNIFIED",
                                    "coin": [
                                        {
                                            "coin": "USDT",
                                            "walletBalance": "100",
                                            "equity": "100",
                                            "borrowAmount": "1",
                                            "spotBorrow": "2",
                                            "locked": "0",
                                            "unrealisedPnl": "0",
                                        }
                                    ],
                                }
                            ]
                        },
                    }
                )
            )

    def test_position_query_requires_bounded_documented_scope(self):
        account_capability = read_capability(
            permission_scopes=("ACCOUNT.READ",)
        )
        with self.assertRaisesRegex(ProviderCoreError, "symbol or settleCoin"):
            prepare_position_read_query(
                capability=account_capability,
                at=READ_AT,
                category="linear",
                symbol=None,
                settle_coin=None,
            )
        with self.assertRaisesRegex(ProviderCoreError, "cannot exceed 200"):
            prepare_position_read_query(
                capability=account_capability,
                at=READ_AT,
                category="linear",
                symbol="BTCUSDT",
                limit=201,
            )
        option = prepare_position_read_query(
            capability=account_capability,
            at=READ_AT,
            category="option",
            symbol=None,
            base_coin="BTC",
        )
        self.assertEqual(option.query["baseCoin"], "BTC")

    def test_position_page_enforces_exact_symbol_and_provider_environment(self):
        page = parse_position_page(
            bound_position_response(
                {
                    "retCode": 0,
                    "result": {
                        "category": "linear",
                        "nextPageCursor": "",
                        "list": [
                            {
                                "symbol": "BTCUSDT",
                                "positionIdx": 0,
                                "side": "Buy",
                                "size": "2",
                                "positionStatus": "Normal",
                                "updatedTime": "1790280000000",
                                "seq": "42",
                            }
                        ],
                    },
                }
            )
        )
        self.assertTrue(page.pagination_complete)
        self.assertEqual(page.positions[0].side, "BUY")
        self.assertEqual(page.positions[0].size, Decimal("2"))
        self.assertEqual(page.positions[0].sequence, 42)
        self.assertEqual(
            page.positions[0].updated_at,
            "2026-09-24T20:00:00.000Z",
        )

        with self.assertRaisesRegex(ProviderCoreError, "symbol does not match"):
            parse_position_page(
                bound_position_response(
                    {
                        "retCode": 0,
                        "result": {
                            "category": "linear",
                            "nextPageCursor": "",
                            "list": [
                                {
                                    "symbol": "ETHUSDT",
                                    "positionIdx": 0,
                                    "side": "Buy",
                                    "size": "1",
                                    "positionStatus": "Normal",
                                    "updatedTime": "1790280000000",
                                    "seq": "43",
                                }
                            ],
                        },
                    },
                    symbol="BTCUSDT",
                )
            )

    def test_position_projection_requires_complete_one_way_cursor_chain(self):
        common = dict(
            category="linear",
            symbol=None,
            settle_coin="USDT",
        )
        first = bound_position_response(
            {
                "retCode": 0,
                "result": {
                    "category": "linear",
                    "nextPageCursor": "position-page-2",
                    "list": [
                        {
                            "symbol": "BTCUSDT",
                            "positionIdx": 0,
                            "side": "Buy",
                            "size": "2",
                            "positionStatus": "Normal",
                            "updatedTime": "1790279999000",
                            "seq": "50",
                        }
                    ],
                },
            },
            **common,
        )
        second = bound_position_response(
            {
                "retCode": 0,
                "result": {
                    "category": "linear",
                    "nextPageCursor": "",
                    "list": [
                        {
                            "symbol": "ETHUSDT",
                            "positionIdx": 0,
                            "side": "Sell",
                            "size": "3",
                            "positionStatus": "Normal",
                            "updatedTime": "1790280000000",
                            "seq": "51",
                        }
                    ],
                },
            },
            cursor="position-page-2",
            **common,
        )
        positions = provider_position_quantities_from_pages(
            (first, second),
            instrument_versions={
                "BTCUSDT": "BTCUSDT@v1",
                "ETHUSDT": "ETHUSDT@v1",
            },
        )
        self.assertEqual(
            dict(positions),
            {
                "BTCUSDT@v1": Decimal("2"),
                "ETHUSDT@v1": Decimal("-3"),
            },
        )
        with self.assertRaisesRegex(ProviderCoreError, "incomplete"):
            provider_position_quantities_from_pages(
                (first,),
                instrument_versions={"BTCUSDT": "BTCUSDT@v1"},
            )

    def test_position_projection_rejects_mixed_capability_snapshots(self):
        first_capability = read_capability(permission_scopes=("ACCOUNT.READ",))
        second_capability = read_capability(permission_scopes=("ACCOUNT.READ",))
        first = bound_position_response(
            {
                "retCode": 0,
                "result": {
                    "category": "linear",
                    "nextPageCursor": "position-page-2",
                    "list": [],
                },
            },
            capability=first_capability,
        )
        second = bound_position_response(
            {
                "retCode": 0,
                "result": {
                    "category": "linear",
                    "nextPageCursor": "",
                    "list": [],
                },
            },
            cursor="position-page-2",
            capability=second_capability,
        )

        with self.assertRaisesRegex(
            ProviderCoreError,
            "authenticated-read capability scope",
        ):
            provider_position_quantities_from_pages(
                (first, second),
                instrument_versions={},
            )

    def test_position_projection_fails_closed_for_hedge_or_liquidation_state(self):
        hedge = bound_position_response(
            {
                "retCode": 0,
                "result": {
                    "category": "linear",
                    "nextPageCursor": "",
                    "list": [
                        {
                            "symbol": "BTCUSDT",
                            "positionIdx": 1,
                            "side": "Buy",
                            "size": "2",
                            "positionStatus": "Normal",
                            "updatedTime": "1790280000000",
                            "seq": "60",
                        }
                    ],
                },
            }
        )
        with self.assertRaisesRegex(ProviderCoreError, "hedge-mode"):
            provider_position_quantities_from_pages(
                (hedge,),
                instrument_versions={"BTCUSDT": "BTCUSDT@v1"},
            )

        liquidation = bound_position_response(
            {
                "retCode": 0,
                "result": {
                    "category": "linear",
                    "nextPageCursor": "",
                    "list": [
                        {
                            "symbol": "BTCUSDT",
                            "positionIdx": 0,
                            "side": "Buy",
                            "size": "2",
                            "positionStatus": "Liq",
                            "updatedTime": "1790280000000",
                            "seq": "61",
                        }
                    ],
                },
            }
        )
        with self.assertRaisesRegex(ProviderCoreError, "Liq/Adl"):
            provider_position_quantities_from_pages(
                (liquidation,),
                instrument_versions={"BTCUSDT": "BTCUSDT@v1"},
            )

    def test_position_next_page_preserves_exact_capability_and_query(self):
        capability = read_capability(permission_scopes=("ACCOUNT.READ",))
        first = bound_position_response(
            {
                "retCode": 0,
                "result": {
                    "category": "linear",
                    "nextPageCursor": "position-next",
                    "list": [],
                },
            },
            symbol=None,
            settle_coin="USDT",
            capability=capability,
        )
        binding = prepare_next_position_read_query(
            observation=first,
            capability=capability,
            at=READ_AT,
        )
        self.assertIsNotNone(binding)
        self.assertEqual(binding.query["cursor"], "position-next")
        self.assertEqual(binding.query["settleCoin"], "USDT")

    def test_bybit_account_surfaces_feed_existing_reconciliation_without_new_authority(self):
        wallet = parse_wallet_snapshot(
            bound_wallet_response(
                {
                    "retCode": 0,
                    "result": {
                        "list": [
                            {
                                "accountType": "UNIFIED",
                                "coin": [
                                    {
                                        "coin": "USDT",
                                        "walletBalance": "100",
                                        "equity": "100",
                                        "borrowAmount": "0",
                                        "spotBorrow": "0",
                                        "locked": "0",
                                        "unrealisedPnl": "0",
                                    }
                                ],
                            }
                        ]
                    },
                }
            )
        )
        position_observation = bound_position_response(
            {
                "retCode": 0,
                "result": {
                    "category": "linear",
                    "nextPageCursor": "",
                    "list": [
                        {
                            "symbol": "BTCUSDT",
                            "positionIdx": 0,
                            "side": "Buy",
                            "size": "2",
                            "positionStatus": "Normal",
                            "updatedTime": "1790280000000",
                            "seq": "70",
                        }
                    ],
                },
            }
        )
        provider_positions = provider_position_quantities_from_pages(
            (position_observation,),
            instrument_versions={"BTCUSDT": "BTCUSDT@v1"},
        )
        result = reconcile_account(
            provider_id="BYBIT",
            account_id="paper-1",
            environment="PAPER",
            provider_environment="TESTNET",
            local_cash={"USDT": "100"},
            provider_cash=provider_wallet_cash(wallet),
            local_positions={"BTCUSDT@v1": "2"},
            provider_positions=provider_positions,
            local_execution_ids=(),
            provider_fills=(),
            snapshot_consistency=SnapshotConsistencyEvidence(
                provider_id="BYBIT",
                account_id="paper-1",
                environment="PAPER",
                provider_environment="TESTNET",
                mode="COMPOSED",
                query_started_at="2026-09-24T19:59:59Z",
                query_completed_at="2026-09-24T20:00:01Z",
                buffered_stream_events=True,
                replay_complete=True,
                sequence_gap_detected=False,
            ),
            coverage_start="2026-09-24T19:59:00Z",
            coverage_end="2026-09-24T20:01:00Z",
            pagination_complete=True,
        )
        self.assertTrue(result.complete)
        self.assertTrue(result.snapshot_consistent)
        self.assertEqual(dict(result.provider_cash), {"USDT": Decimal("100")})
        self.assertEqual(
            dict(result.provider_positions),
            {"BTCUSDT@v1": Decimal("2")},
        )


    def test_activity_query_binds_unified_scope_window_filter_and_cursor(self):
        binding = prepare_activity_read_query(
            capability=read_capability(permission_scopes=("ACCOUNT.READ",)),
            at=READ_AT,
            category="linear",
            currency="USDT",
            activity_type="SETTLEMENT",
            cursor="activity%3A2",
            limit=50,
            start_time_ms=1790193600000,
            end_time_ms=1790280000000,
        )
        self.assertEqual(binding.endpoint, "/v5/account/transaction-log")
        self.assertEqual(binding.permission_scope, "ACCOUNT.READ")
        self.assertEqual(binding.query["accountType"], "UNIFIED")
        self.assertEqual(binding.query["category"], "linear")
        self.assertEqual(binding.query["currency"], "USDT")
        self.assertEqual(binding.query["type"], "SETTLEMENT")
        self.assertEqual(binding.query["cursor"], "activity%3A2")
        with self.assertRaisesRegex(ProviderCoreError, "seven days"):
            prepare_activity_read_query(
                capability=read_capability(
                    permission_scopes=("ACCOUNT.READ",)
                ),
                at=READ_AT,
                start_time_ms=0,
                end_time_ms=(7 * 24 * 60 * 60 * 1000) + 1,
            )

    def test_activity_page_preserves_cash_delta_and_unknown_origin(self):
        page = parse_activity_page(
            bound_activity_response(
                {
                    "retCode": 0,
                    "result": {
                        "nextPageCursor": "activity-next",
                        "list": [
                            {
                                "id": "activity-1",
                                "symbol": "BTCUSDT",
                                "category": "linear",
                                "side": "Buy",
                                "transactionTime": "1790280000000",
                                "type": "SETTLEMENT",
                                "qty": "1",
                                "size": "1",
                                "currency": "USDT",
                                "tradePrice": "65000",
                                "funding": "-0.25",
                                "fee": "0",
                                "cashFlow": "0",
                                "change": "-0.25",
                                "cashBalance": "99.75",
                                "feeRate": "0.0001",
                                "bonusChange": "",
                                "tradeId": "trade-1",
                                "orderId": "provider-order-1",
                                "orderLinkId": "client-1",
                                "extraFees": "",
                                "displayType": "funding",
                            }
                        ],
                    },
                },
                category="linear",
                currency="USDT",
                activity_type="SETTLEMENT",
            ),
            instrument_versions={"BTCUSDT": "BTCUSDT@v1"},
        )
        self.assertFalse(page.pagination_complete)
        self.assertEqual(page.next_cursor, "activity-next")
        self.assertEqual(len(page.activities), 1)
        activity = page.activities[0]
        self.assertEqual(activity.activity_id, "activity-1")
        self.assertEqual(activity.activity_type, "SETTLEMENT")
        self.assertEqual(activity.origin, "UNKNOWN")
        self.assertEqual(activity.instrument, "BTCUSDT@v1")
        self.assertEqual(activity.currency, "USDT")
        self.assertEqual(activity.client_order_id, "client-1")
        self.assertEqual(activity.provider_order_id, "provider-order-1")
        self.assertEqual(activity.provider_execution_id, "trade-1")
        self.assertEqual(activity.signed_amount, Decimal("-0.25"))
        self.assertEqual(activity.occurred_at, "2026-09-24T20:00:00Z")

    def test_activity_page_rejects_exact_filter_or_instrument_mismatch(self):
        base_row = {
            "id": "activity-mismatch",
            "symbol": "BTCUSDT",
            "category": "linear",
            "side": "None",
            "transactionTime": "1790280000000",
            "type": "SETTLEMENT",
            "qty": "0",
            "size": "0",
            "currency": "USDT",
            "tradePrice": "0",
            "funding": "0",
            "fee": "0",
            "cashFlow": "1",
            "change": "1",
            "cashBalance": "101",
            "feeRate": "0",
            "bonusChange": "",
            "tradeId": "",
            "orderId": "",
            "orderLinkId": "",
            "extraFees": "",
            "displayType": "",
        }
        with self.assertRaisesRegex(ProviderCoreError, "currency"):
            parse_activity_page(
                bound_activity_response(
                    {
                        "retCode": 0,
                        "result": {
                            "nextPageCursor": "",
                            "list": [base_row],
                        },
                    },
                    currency="BTC",
                ),
                instrument_versions={"BTCUSDT": "BTCUSDT@v1"},
            )
        with self.assertRaisesRegex(ProviderCoreError, "unmapped"):
            parse_activity_page(
                bound_activity_response(
                    {
                        "retCode": 0,
                        "result": {
                            "nextPageCursor": "",
                            "list": [base_row],
                        },
                    }
                ),
                instrument_versions={},
            )

    def test_activity_coverage_requires_complete_contiguous_explicit_window(self):
        common = {
            "start_time_ms": 1790193600000,
            "end_time_ms": 1790280000000,
        }
        capability = read_capability(permission_scopes=("ACCOUNT.READ",))
        first = bound_activity_response(
            {
                "retCode": 0,
                "result": {
                    "nextPageCursor": "activity-page-2",
                    "list": [],
                },
            },
            capability=capability,
            **common,
        )
        second = bound_activity_response(
            {
                "retCode": 0,
                "result": {
                    "nextPageCursor": "",
                    "list": [],
                },
            },
            cursor="activity-page-2",
            capability=capability,
            **common,
        )
        coverage = activity_coverage_from_pages(
            (first, second),
            instrument_versions={},
            consistency_horizon_satisfied=True,
        )
        self.assertEqual(coverage.surface, "ACTIVITIES")
        self.assertTrue(coverage.pagination_complete)
        self.assertFalse(coverage.provider_semantics_exclude_execution)
        self.assertEqual(coverage.coverage_start, "2026-09-23T20:00:00.000Z")
        self.assertEqual(coverage.coverage_end, "2026-09-24T20:00:00.000Z")

        with self.assertRaisesRegex(ProviderCoreError, "incomplete"):
            activity_coverage_from_pages(
                (first,),
                instrument_versions={},
                consistency_horizon_satisfied=True,
            )

    def test_activity_coverage_rejects_mixed_capability_snapshots(self):
        first_capability = read_capability(
            permission_scopes=("ACCOUNT.READ",)
        )
        second_capability = read_capability(
            permission_scopes=("ACCOUNT.READ",)
        )
        self.assertNotEqual(
            first_capability.snapshot_id,
            second_capability.snapshot_id,
        )
        common = {
            "start_time_ms": 1790193600000,
            "end_time_ms": 1790280000000,
        }
        first = bound_activity_response(
            {
                "retCode": 0,
                "result": {
                    "nextPageCursor": "activity-page-2",
                    "list": [],
                },
            },
            capability=first_capability,
            **common,
        )
        second = bound_activity_response(
            {
                "retCode": 0,
                "result": {
                    "nextPageCursor": "",
                    "list": [],
                },
            },
            cursor="activity-page-2",
            capability=second_capability,
            **common,
        )
        with self.assertRaisesRegex(
            ProviderCoreError,
            "authenticated-read capability scope",
        ):
            activity_coverage_from_pages(
                (first, second),
                instrument_versions={},
                consistency_horizon_satisfied=True,
            )

    def test_activity_next_page_preserves_exact_capability_scope(self):
        capability = read_capability(permission_scopes=("ACCOUNT.READ",))
        first = bound_activity_response(
            {
                "retCode": 0,
                "result": {
                    "nextPageCursor": "activity-next",
                    "list": [],
                },
            },
            start_time_ms=1790193600000,
            end_time_ms=1790280000000,
            capability=capability,
        )
        binding = prepare_next_activity_read_query(
            observation=first,
            capability=capability,
            instrument_versions={},
            at=READ_AT,
        )
        self.assertIsNotNone(binding)
        self.assertEqual(binding.query["cursor"], "activity-next")
        self.assertEqual(binding.query["accountType"], "UNIFIED")
        self.assertEqual(binding.query["startTime"], "1790193600000")
        self.assertEqual(binding.query["endTime"], "1790280000000")

    def test_unmatched_bybit_activity_blocks_reconciliation_as_unknown_origin(self):
        page = parse_activity_page(
            bound_activity_response(
                {
                    "retCode": 0,
                    "result": {
                        "nextPageCursor": "",
                        "list": [
                            {
                                "id": "manual-or-external-cash-change",
                                "symbol": "",
                                "category": "spot",
                                "side": "None",
                                "transactionTime": "1790280000000",
                                "type": "TRANSFER_IN",
                                "qty": "0",
                                "size": "0",
                                "currency": "USDT",
                                "tradePrice": "0",
                                "funding": "0",
                                "fee": "0",
                                "cashFlow": "10",
                                "change": "10",
                                "cashBalance": "110",
                                "feeRate": "0",
                                "bonusChange": "",
                                "tradeId": "",
                                "orderId": "",
                                "orderLinkId": "",
                                "extraFees": "",
                                "displayType": "",
                            }
                        ],
                    },
                }
            ),
            instrument_versions={},
        )
        coverage = coverage_evidence(
            account_id="paper-1",
            environment="PAPER",
            provider_environment="TESTNET",
            surface="ACTIVITIES",
            coverage_start="2026-09-24T19:59:00Z",
            coverage_end="2026-09-24T20:01:00Z",
            pagination_complete=True,
            consistency_horizon_satisfied=True,
        )
        result = reconcile_account(
            provider_id="BYBIT",
            account_id="paper-1",
            environment="PAPER",
            provider_environment="TESTNET",
            local_cash={"USDT": "110"},
            provider_cash={"USDT": "110"},
            local_positions={},
            provider_positions={},
            local_execution_ids=(),
            provider_fills=(),
            provider_activities=page.activities,
            provider_activity_provider_id="BYBIT",
            provider_activity_account_id="paper-1",
            activity_coverage=coverage,
            require_activity_reconciliation=True,
            coverage_start="2026-09-24T19:59:00Z",
            coverage_end="2026-09-24T20:01:00Z",
            pagination_complete=True,
        )
        self.assertIn(
            "manual-or-external-cash-change",
            result.manual_or_external_activity_ids,
        )
        self.assertTrue(result.blocks_new_risk)


if __name__ == "__main__":
    unittest.main()
