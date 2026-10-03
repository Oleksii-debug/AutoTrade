from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from io import BytesIO
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler
import subprocess
import sys
from threading import Event
from tempfile import TemporaryDirectory
import unittest
from contextlib import contextmanager

from mvp.autotrade_mvp.capabilities import (
    CapabilityClaim,
    CapabilityRegistry,
    EvidenceVerification,
    derive_capability_snapshot,
)
from mvp.autotrade_mvp.alpaca import (
    AlpacaOrderIntent,
    guarded_order_projection as alpaca_guarded_order_projection,
    prepare_order_request as prepare_alpaca_order_request,
)
from mvp.autotrade_mvp.binance_spot import parse_account_trades
from mvp.autotrade_mvp.kraken_spot import parse_trade_history
from mvp.autotrade_mvp.dispatch import (
    GuardedDispatcher,
    load_submission_response_binding,
    stable_client_order_id,
)
from mvp.autotrade_mvp.persistence import JournalStore, payload_digest
from mvp.autotrade_mvp.provider_core import (
    Surface,
    observe_authenticated_json_response,
    prepare_authenticated_read_query,
)
from mvp.autotrade_mvp.provider_transport import (
    ALPACA_ENDPOINT_POLICIES,
    BINANCE_SPOT_ENDPOINT_POLICIES,
    AuthenticatedReadHttpRequest,
    AuthenticatedReadWireResponse,
    AlpacaTradingHttpTransport,
    BinanceSpotAuthenticatedReadSigner,
    BinanceSpotAuthenticatedReadTransport,
    BinanceSpotHttpTransport,
    BinanceSpotSigner,
    ProviderEndpointPolicy,
    ProviderTransportError,
    ProviderTransportScopeError,
    TradingWireResponse,
    SignedHttpRequest,
    UrllibJsonWireClient,
    _exact_trading_response,
    _binance_exact_trading_response,
    KRAKEN_FUTURES_ENDPOINT_POLICIES,
    KRAKEN_SPOT_ENDPOINT_POLICIES,
    KrakenFuturesSigner,
    KrakenSpotAuthenticatedReadSigner,
    KrakenSpotAuthenticatedReadTransport,
    KrakenSpotDurableNonceAllocator,
    KrakenSpotHttpTransport,
    KrakenSpotSigner,
    WHITEBIT_ENDPOINT_POLICIES,
    WhiteBitDurableNonceAllocator,
    WhiteBitHttpTransport,
    _DurableProviderNonceAllocator,
)
from mvp.autotrade_mvp.whitebit import WhiteBitPreparedRequest
from mvp.autotrade_mvp.provider_response_limits import (
    HARD_MAX_PROVIDER_RESPONSE_BYTES,
    DEFAULT_MAX_PROVIDER_RESPONSE_BYTES,
    require_provider_response_bytes,
    require_provider_json_depth,
    MAX_PROVIDER_JSON_DEPTH,
)
from mvp.autotrade_mvp.windows_secrets import PersistentCredentialHandle


class FakeSecretResolver:
    def __init__(
        self,
        events,
        *,
        on_resolve=None,
        credential_plaintext=None,
    ):
        self.events = events
        self.calls = []
        self.on_resolve = on_resolve
        self.credential_plaintext = credential_plaintext
        self.lease_active = False
        self.lease_enters = 0
        self.lease_exits = 0

    @contextmanager
    def lease_for_execution(
        self,
        token,
        *,
        origin,
        handle,
        execution_identity,
        account_id,
        provider,
        environment,
        purpose,
        provider_environment=None,
    ):
        if self.lease_active:
            raise AssertionError("credential lease must not be re-entered")
        plaintext = self.resolve_for_execution(
            token,
            origin=origin,
            handle=handle,
            execution_identity=execution_identity,
            account_id=account_id,
            provider=provider,
            environment=environment,
            purpose=purpose,
            provider_environment=provider_environment,
        )
        self.lease_active = True
        self.lease_enters += 1
        try:
            yield plaintext
        finally:
            self.lease_active = False
            self.lease_exits += 1

    def resolve_for_execution(
        self,
        token,
        *,
        origin,
        handle,
        execution_identity,
        account_id,
        provider,
        environment,
        purpose,
        provider_environment=None,
    ):
        self.events.append("resolve")
        self.calls.append(
            {
                "token": token,
                "origin": origin,
                "handle": handle,
                "execution_identity": execution_identity,
                "account_id": account_id,
                "provider": provider,
                "environment": environment,
                "provider_environment": provider_environment,
                "purpose": purpose,
            }
        )
        if self.on_resolve is not None:
            self.on_resolve()
        if self.credential_plaintext is not None:
            return self.credential_plaintext
        return json.dumps(
            {"api_key": "api-key-SECRET", "api_secret": "signing-SECRET"},
            sort_keys=True,
            separators=(",", ":"),
        )


class RecordingWire:
    def __init__(
        self,
        events,
        *,
        response=b'{"ok":true}',
        error=None,
        http_status=200,
    ):
        self.events = events
        self.response = response
        self.error = error
        self.http_status = http_status
        self.requests = []

    def send(self, request):
        self.events.append("wire")
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        if isinstance(request, AuthenticatedReadHttpRequest):
            return AuthenticatedReadWireResponse(
                http_status=self.http_status,
                body=self.response,
            )
        return TradingWireResponse(
            http_status=self.http_status,
            body=self.response,
        )



class LeaseAssertingWire(RecordingWire):
    def __init__(self, events, resolver, **kwargs):
        super().__init__(events, **kwargs)
        self.resolver = resolver

    def send(self, request):
        if not self.resolver.lease_active:
            raise AssertionError("wire send escaped credential generation lease")
        self.events.append("wire-lease-active")
        return super().send(request)


def trade_handle(*, environment="PAPER", account_id="acct-1"):
    return PersistentCredentialHandle(
        handle_id="cred-binance-trade",
        account_id=account_id,
        provider="BINANCE",
        environment=environment,
        purpose="TRADE",
        generation=1,
    )


def alpaca_trade_handle(*, environment="PAPER", account_id="acct-alpaca"):
    return PersistentCredentialHandle(
        handle_id="cred-alpaca-trade",
        account_id=account_id,
        provider="ALPACA",
        environment=environment,
        purpose="TRADE",
        generation=1,
    )


ALPACA_NOW = datetime(2026, 9, 25, 10, 0, tzinfo=timezone.utc)
ALPACA_SNAPSHOT_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
_ALPACA_ARTIFACT_IDS = {
    "DOCUMENTED": "51111111-1111-4111-8111-111111111111",
    "API": "52222222-2222-4222-8222-222222222222",
    "ACCOUNT": "53333333-3333-4333-8333-333333333333",
    "INSTRUMENT": "54444444-4444-4444-8444-444444444444",
}


def verified_alpaca_capability():
    observed = ALPACA_NOW - timedelta(minutes=1)
    expires = ALPACA_NOW + timedelta(minutes=10)
    claims = tuple(
        CapabilityClaim(
            source=source,
            provider_id="ALPACA",
            account_id="acct-alpaca",
            entity_id="entity-alpaca",
            environment="PAPER",
            instrument_version="AAPL@1",
            observed_at=observed,
            expires_at=expires,
            supported_order_types=frozenset({"LIMIT"}),
            time_in_force=frozenset({"DAY"}),
            permission_scopes=frozenset({"ORDER_WRITE"}),
            position_mode="NET",
            native_protection=frozenset(),
            rate_limit_policy_id="alpaca-paper-v1",
            data_entitlements=frozenset({"ORDERS"}),
            evidence_ref={
                "artifact_id": _ALPACA_ARTIFACT_IDS[source],
                "sha256": "sha256:" + "b" * 64,
                "observed_at": observed.isoformat().replace("+00:00", "Z"),
            },
        )
        for source in ("DOCUMENTED", "API", "ACCOUNT", "INSTRUMENT")
    )
    return derive_capability_snapshot(
        snapshot_id=ALPACA_SNAPSHOT_ID,
        claims=claims,
        observed_at=ALPACA_NOW,
        evidence_verifier=lambda _claim: EvidenceVerification(valid=True),
    )


def alpaca_prepared_request(client_order_id="at-alpaca-1"):
    intent = AlpacaOrderIntent.create(
        instrument_version="AAPL@1",
        asset_class="EQUITY",
        symbol="AAPL",
        side="BUY",
        order_type="LIMIT",
        time_in_force="DAY",
        quantity=Decimal("1"),
        limit_price=Decimal("200.25"),
    )
    return prepare_alpaca_order_request(
        intent,
        client_order_id=client_order_id,
        account_id="acct-alpaca",
        environment="PAPER",
        capability=verified_alpaca_capability(),
        at=ALPACA_NOW,
    )


def whitebit_trade_handle(
    *,
    account_id="acct-wb",
    handle_id="cred-whitebit-trade",
    generation=1,
):
    return PersistentCredentialHandle(
        handle_id=handle_id,
        account_id=account_id,
        provider="WHITEBIT",
        environment="LIVE",
        purpose="TRADE",
        generation=generation,
    )


def whitebit_prepared_request(
    client_order_id,
    *,
    capability_snapshot_id="wb-cap-1",
):
    return {
        "endpoint": "/api/v4/order/new",
        "body": {
            "market": "BTC_USDT",
            "side": "buy",
            "amount": "0.001",
            "price": "50000",
            "clientOrderId": client_order_id,
            "postOnly": False,
        },
        "capability_snapshot_id": capability_snapshot_id,
    }


def kraken_trade_handle(
    *,
    account_id="acct-kraken",
    handle_id="cred-kraken-trade",
    generation=1,
):
    return PersistentCredentialHandle(
        handle_id=handle_id,
        account_id=account_id,
        provider="KRAKEN",
        environment="LIVE",
        purpose="TRADE",
        generation=generation,
    )


def kraken_prepared_request(
    client_order_id,
    *,
    capability_snapshot_id="kraken-cap-1",
    time_in_force="GTC",
):
    return {
        "endpoint": "/0/private/AddOrder",
        "body": {
            "pair": "XBTUSD",
            "type": "buy",
            "ordertype": "limit",
            "volume": "1.25",
            "price": "37500",
            "cl_ord_id": client_order_id,
            "timeinforce": time_in_force,
        },
        "capability_snapshot_id": capability_snapshot_id,
    }


READ_NOW = datetime(2026, 9, 25, 10, 0, tzinfo=timezone.utc)
READ_SNAPSHOT_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
_READ_ARTIFACT_IDS = {
    "DOCUMENTED": "11111111-1111-4111-8111-111111111111",
    "API": "22222222-2222-4222-8222-222222222222",
    "ACCOUNT": "33333333-3333-4333-8333-333333333333",
    "INSTRUMENT": "44444444-4444-4444-8444-444444444444",
}


def read_handle(*, environment="PAPER", account_id="acct-1"):
    return PersistentCredentialHandle(
        handle_id="cred-binance-read",
        account_id=account_id,
        provider="BINANCE",
        environment=environment,
        purpose="READ",
        generation=1,
    )


def verified_read_capability(
    *,
    snapshot_id=READ_SNAPSHOT_ID,
    snapshot_observed_at=READ_NOW,
    permission_scopes=frozenset({"ORDER.READ"}),
    data_entitlements=frozenset({"ACCOUNT"}),
):
    observed = snapshot_observed_at - timedelta(minutes=1)
    expires = snapshot_observed_at + timedelta(minutes=10)
    claims = tuple(
        CapabilityClaim(
            source=source,
            provider_id="BINANCE",
            account_id="acct-1",
            entity_id="entity-1",
            environment="PAPER",
            instrument_version="BTCUSDT@1",
            observed_at=observed,
            expires_at=expires,
            supported_order_types=frozenset({"LIMIT"}),
            time_in_force=frozenset({"GTC"}),
            permission_scopes=permission_scopes,
            position_mode="NET",
            native_protection=frozenset(),
            rate_limit_policy_id="binance-test-v1",
            data_entitlements=data_entitlements,
            evidence_ref={
                "artifact_id": _READ_ARTIFACT_IDS[source],
                "sha256": "sha256:" + "a" * 64,
                "observed_at": observed.isoformat().replace("+00:00", "Z"),
            },
        )
        for source in ("DOCUMENTED", "API", "ACCOUNT", "INSTRUMENT")
    )
    return derive_capability_snapshot(
        snapshot_id=snapshot_id,
        claims=claims,
        observed_at=snapshot_observed_at,
        evidence_verifier=lambda _claim: EvidenceVerification(valid=True),
    )


class RecordingCapabilityRegistry(CapabilityRegistry):
    def __init__(self, events):
        super().__init__()
        self.events = events

    def require_verified(self, **kwargs):
        self.events.append("capability")
        return super().require_verified(**kwargs)


def authenticated_read_binding(
    *,
    endpoint="/api/v3/account",
    query=None,
    capability=None,
    permission_scope="ORDER.READ",
    surface=Surface.AUTHENTICATED_READ,
):
    return prepare_authenticated_read_query(
        capability=capability or verified_read_capability(),
        surface=surface,
        endpoint=endpoint,
        query={"omitZeroBalances": "true"} if query is None else query,
        at=READ_NOW,
        permission_scope=permission_scope,
    )


KRAKEN_READ_NOW = datetime(2026, 9, 26, 1, 0, tzinfo=timezone.utc)
KRAKEN_READ_SNAPSHOT_ID = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
_KRAKEN_READ_ARTIFACT_IDS = {
    "DOCUMENTED": "61111111-1111-4111-8111-111111111111",
    "API": "62222222-2222-4222-8222-222222222222",
    "ACCOUNT": "63333333-3333-4333-8333-333333333333",
    "INSTRUMENT": "64444444-4444-4444-8444-444444444444",
}


def kraken_read_handle(
    *,
    account_id="acct-kraken",
    handle_id="cred-kraken-read",
    generation=1,
):
    return PersistentCredentialHandle(
        handle_id=handle_id,
        account_id=account_id,
        provider="KRAKEN",
        environment="LIVE",
        purpose="READ",
        generation=generation,
    )


def verified_kraken_read_capability(
    *,
    snapshot_id=KRAKEN_READ_SNAPSHOT_ID,
    snapshot_observed_at=KRAKEN_READ_NOW,
    permission_scopes=frozenset({"ORDER.READ"}),
    data_entitlements=frozenset({"ORDERS"}),
):
    observed = snapshot_observed_at - timedelta(minutes=1)
    expires = snapshot_observed_at + timedelta(minutes=10)
    claims = tuple(
        CapabilityClaim(
            source=source,
            provider_id="KRAKEN",
            account_id="acct-kraken",
            entity_id="entity-kraken-spot",
            environment="LIVE",
            instrument_version="XBTUSD@1",
            observed_at=observed,
            expires_at=expires,
            supported_order_types=frozenset({"LIMIT"}),
            time_in_force=frozenset({"GTC", "IOC"}),
            permission_scopes=permission_scopes,
            position_mode="NET",
            native_protection=frozenset(),
            rate_limit_policy_id="kraken-spot-live-v1",
            data_entitlements=data_entitlements,
            evidence_ref={
                "artifact_id": _KRAKEN_READ_ARTIFACT_IDS[source],
                "sha256": "sha256:" + "c" * 64,
                "observed_at": observed.isoformat().replace("+00:00", "Z"),
            },
        )
        for source in ("DOCUMENTED", "API", "ACCOUNT", "INSTRUMENT")
    )
    return derive_capability_snapshot(
        snapshot_id=snapshot_id,
        claims=claims,
        observed_at=snapshot_observed_at,
        evidence_verifier=lambda _claim: EvidenceVerification(valid=True),
    )


def kraken_authenticated_read_binding(
    *,
    endpoint="/0/private/OpenOrders",
    query=None,
    capability=None,
    permission_scope="ORDER.READ",
):
    return prepare_authenticated_read_query(
        capability=capability or verified_kraken_read_capability(),
        surface=Surface.ACTIVITIES,
        endpoint=endpoint,
        query={} if query is None else query,
        at=KRAKEN_READ_NOW,
        permission_scope=permission_scope,
    )


def prepared_request(client_order_id, *, capability_snapshot_id="cap-1"):
    return {
        "endpoint": "/api/v3/order",
        "body": {
            "symbol": "BTCUSDT",
            "side": "BUY",
            "type": "LIMIT",
            "quantity": "0.001",
            "price": "50000",
            "timeInForce": "GTC",
            "newClientOrderId": client_order_id,
            "newOrderRespType": "ACK",
        },
        "capability_snapshot_id": capability_snapshot_id,
    }


class AlpacaProviderTransportTests(unittest.TestCase):
    def make_transport(self, *, events, wire=None, quota_gate=None):
        resolver = FakeSecretResolver(events)
        transport = AlpacaTradingHttpTransport(
            policy=ALPACA_ENDPOINT_POLICIES["PAPER"],
            account_id="acct-alpaca",
            capability_snapshot_id=ALPACA_SNAPSHOT_ID,
            secret_resolver=resolver,
            credential_handle=alpaca_trade_handle(),
            session_token="session-token",
            origin="https://localhost",
            execution_identity="host-owner",
            quota_gate=quota_gate,
            wire_client=wire or RecordingWire(events),
        )
        return transport, resolver

    def test_alpaca_paper_transport_binds_exact_scope_and_guarded_send(self):
        events = []
        wire = RecordingWire(events, response=b'{"id":"order-1","status":"accepted"}')

        def quota(provider, account, environment, purpose):
            events.append("quota")
            self.assertEqual(
                (provider, account, environment, purpose),
                ("ALPACA", "acct-alpaca", "PAPER", "ORDER_WRITE"),
            )

        transport, resolver = self.make_transport(
            events=events,
            wire=wire,
            quota_gate=quota,
        )
        prepared = alpaca_prepared_request()
        projection = alpaca_guarded_order_projection(prepared)
        response = transport(
            "at-alpaca-1",
            projection,
            lambda: events.append("guard"),
        )

        self.assertEqual(response.payload["id"], "order-1")
        self.assertEqual(events, ["quota", "resolve", "guard", "wire"])
        self.assertEqual(len(resolver.calls), 1)
        self.assertEqual(len(wire.requests), 1)
        request = wire.requests[0]
        self.assertEqual(
            request.url,
            "https://paper-api.alpaca.markets/v2/orders",
        )
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.headers["APCA-API-KEY-ID"], "api-key-SECRET")
        self.assertEqual(
            request.headers["APCA-API-SECRET-KEY"],
            "signing-SECRET",
        )
        self.assertEqual(
            json.loads(request.body.decode("utf-8")),
            dict(prepared.body),
        )

    def test_alpaca_final_guard_failure_has_zero_outbound_requests(self):
        events = []
        wire = RecordingWire(events)
        transport, resolver = self.make_transport(events=events, wire=wire)

        def blocked():
            events.append("guard")
            raise PermissionError("authority revoked")

        with self.assertRaisesRegex(PermissionError, "authority revoked"):
            transport(
                "at-alpaca-1",
                alpaca_guarded_order_projection(alpaca_prepared_request()),
                blocked,
            )
        self.assertEqual(events, ["resolve", "guard"])
        self.assertEqual(len(resolver.calls), 1)
        self.assertEqual(wire.requests, [])

    def test_alpaca_scope_and_digest_mismatch_fail_before_secret_or_wire(self):
        mutations = (
            ("account_id", "other-account", "account mismatch"),
            ("environment", "LIVE", "environment mismatch"),
            ("capability_snapshot_id", "other-cap", "capability snapshot mismatch"),
            ("body_sha256", "sha256:" + "0" * 64, "body digest mismatch"),
        )
        for field, value, message in mutations:
            with self.subTest(field=field):
                events = []
                wire = RecordingWire(events)
                transport, resolver = self.make_transport(events=events, wire=wire)
                request = dict(
                    alpaca_guarded_order_projection(alpaca_prepared_request())
                )
                request[field] = value
                if field == "capability_snapshot_id":
                    request["capability_snapshot_ids"] = [value]
                with self.assertRaisesRegex(
                    ProviderTransportScopeError,
                    message,
                ):
                    transport(
                        "at-alpaca-1",
                        request,
                        lambda: events.append("guard"),
                    )
                self.assertEqual(events, [])
                self.assertEqual(resolver.calls, [])
                self.assertEqual(wire.requests, [])

    def test_alpaca_rejects_binary_float_before_secret_or_wire(self):
        events = []
        wire = RecordingWire(events)
        transport, resolver = self.make_transport(events=events, wire=wire)
        request = dict(
            alpaca_guarded_order_projection(alpaca_prepared_request())
        )
        request["body"] = dict(request["body"])
        request["body"]["limit_price"] = 200.25
        with self.assertRaisesRegex(
            ProviderTransportScopeError,
            "binary floating",
        ):
            transport(
                "at-alpaca-1",
                request,
                lambda: events.append("guard"),
            )
        self.assertEqual(events, [])
        self.assertEqual(resolver.calls, [])
        self.assertEqual(wire.requests, [])

    def test_alpaca_wrong_scoped_trade_handle_is_rejected(self):
        events = []
        with self.assertRaisesRegex(
            ProviderTransportScopeError,
            "provider/environment/purpose mismatch",
        ):
            AlpacaTradingHttpTransport(
                policy=ALPACA_ENDPOINT_POLICIES["PAPER"],
                account_id="acct-alpaca",
                capability_snapshot_id=ALPACA_SNAPSHOT_ID,
                secret_resolver=FakeSecretResolver(events),
                credential_handle=alpaca_trade_handle(environment="LIVE"),
                session_token="session-token",
                origin="https://localhost",
                execution_identity="host-owner",
                wire_client=RecordingWire(events),
            )


class WhiteBitProviderTransportTests(unittest.TestCase):
    def test_durable_nonce_survives_restart_and_clock_regression(self):
        fixed = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)
        provider_key = "api-key-SECRET"
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            first = WhiteBitDurableNonceAllocator(
                journal=JournalStore(path),
                account_id="acct-wb",
                environment="LIVE",
                clock_millis=lambda: 1_700_000_000_000,
                clock_utc=lambda: fixed,
            )
            self.assertEqual(
                first.for_provider_api_key(provider_key).allocate(),
                1_700_000_000_000,
            )

            reopened = WhiteBitDurableNonceAllocator(
                journal=JournalStore(path),
                account_id="acct-wb",
                environment="LIVE",
                clock_millis=lambda: 1_699_999_999_000,
                clock_utc=lambda: fixed + timedelta(seconds=1),
            )
            self.assertEqual(
                reopened.for_provider_api_key(provider_key).allocate(),
                1_700_000_000_001,
            )
            aggregate_id = reopened.aggregate_id_for_provider_api_key(provider_key)
            events = JournalStore(path).load_events(
                "provider_nonce",
                aggregate_id,
            )
            self.assertEqual(
                [item["payload"]["nonce"] for item in events],
                [1_700_000_000_000, 1_700_000_000_001],
            )
            fingerprint = reopened.provider_api_key_fingerprint(provider_key)
            self.assertEqual(
                {item["payload"]["provider_api_key_fingerprint"] for item in events},
                {fingerprint},
            )
            self.assertNotIn(provider_key, json.dumps(events, sort_keys=True))

    def test_provider_key_nonce_domain_inherits_legacy_account_high_water(self):
        fixed = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            journal = JournalStore(path)
            legacy = _DurableProviderNonceAllocator(
                provider_id="WHITEBIT",
                display_name="WhiteBIT",
                journal=journal,
                account_id="acct-wb",
                environment="LIVE",
                clock_millis=lambda: 1_700_000_000_100,
                clock_utc=lambda: fixed,
            )
            self.assertEqual(legacy.allocate(), 1_700_000_000_100)

            migrated = WhiteBitDurableNonceAllocator(
                journal=JournalStore(path),
                account_id="acct-wb",
                environment="LIVE",
                clock_millis=lambda: 1_699_999_000_000,
                clock_utc=lambda: fixed + timedelta(seconds=1),
            )
            keyed = migrated.for_provider_api_key("api-key-SECRET")
            self.assertEqual(keyed.allocate(), 1_700_000_000_101)
            self.assertEqual(migrated.legacy_nonce_floor, 1_700_000_000_100)

    def test_provider_key_nonce_domain_rejects_cross_account_aliasing(self):
        fixed = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)
        provider_key = "shared-provider-key"
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            first = WhiteBitDurableNonceAllocator(
                journal=JournalStore(path),
                account_id="acct-wb-a",
                environment="LIVE",
                clock_millis=lambda: 1_700_000_000_000,
                clock_utc=lambda: fixed,
            )
            second = WhiteBitDurableNonceAllocator(
                journal=JournalStore(path),
                account_id="acct-wb-b",
                environment="LIVE",
                clock_millis=lambda: 1_700_000_000_001,
                clock_utc=lambda: fixed + timedelta(milliseconds=1),
            )
            self.assertEqual(
                first.aggregate_id_for_provider_api_key(provider_key),
                second.aggregate_id_for_provider_api_key(provider_key),
            )
            first.for_provider_api_key(provider_key).allocate()
            with self.assertRaisesRegex(
                ProviderTransportError,
                "scope does not match allocator",
            ):
                second.for_provider_api_key(provider_key).allocate()

    def test_whitebit_live_transport_requires_quota_gate(self):
        events = []
        fixed = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)
        with TemporaryDirectory() as directory:
            journal = JournalStore(f"{directory}/journal.sqlite3")
            allocator = WhiteBitDurableNonceAllocator(
                journal=journal,
                account_id="acct-wb",
                environment="LIVE",
                clock_millis=lambda: events.append("nonce") or 1_700_000_000_000,
                clock_utc=lambda: fixed,
            )
            with self.assertRaisesRegex(
                TypeError,
                "quota_gate must be callable for WhiteBIT LIVE transport",
            ):
                WhiteBitHttpTransport(
                    policy=WHITEBIT_ENDPOINT_POLICIES["LIVE"],
                    account_id="acct-wb",
                    capability_snapshot_id="wb-cap-1",
                    secret_resolver=FakeSecretResolver(events),
                    credential_handle=whitebit_trade_handle(),
                    session_token="session-1",
                    origin="autotrade://execution",
                    execution_identity="sender-1",
                    nonce_allocator=allocator,
                    wire_client=RecordingWire(events),
                )
            self.assertEqual(events, [])
            self.assertEqual(
                journal.load_events(
                    "provider_nonce",
                    allocator.aggregate_id_for_provider_api_key("api-key-SECRET"),
                ),
                [],
            )

    def test_whitebit_transport_has_one_guarded_send_after_durable_nonce(self):
        events = []
        fixed = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)
        with TemporaryDirectory() as directory:
            allocator = WhiteBitDurableNonceAllocator(
                journal=JournalStore(f"{directory}/journal.sqlite3"),
                account_id="acct-wb",
                environment="LIVE",
                clock_millis=lambda: events.append("nonce") or 1_700_000_000_000,
                clock_utc=lambda: fixed,
            )
            resolver = FakeSecretResolver(events)
            wire = RecordingWire(events, response=b'{"orderId":"123"}')
            transport = WhiteBitHttpTransport(
                policy=WHITEBIT_ENDPOINT_POLICIES["LIVE"],
                account_id="acct-wb",
                capability_snapshot_id="wb-cap-1",
                secret_resolver=resolver,
                credential_handle=whitebit_trade_handle(),
                session_token="session-1",
                origin="autotrade://execution",
                execution_identity="sender-1",
                nonce_allocator=allocator,
                quota_gate=lambda *_args: events.append("quota"),
                wire_client=wire,
            )

            client_id = "at-whitebit-1"
            response = transport(
                client_id,
                whitebit_prepared_request(client_id),
                lambda: events.append("guard"),
            )

            self.assertEqual(
                events,
                ["quota", "resolve", "nonce", "guard", "wire"],
            )
            self.assertEqual(response.payload, {"orderId": "123"})
            self.assertEqual(len(wire.requests), 1)
            signed = wire.requests[0]
            self.assertEqual(
                signed.url,
                "https://whitebit.com/api/v4/order/new",
            )
            body = json.loads(signed.body)
            self.assertEqual(body["clientOrderId"], client_id)
            self.assertEqual(body["request"], "/api/v4/order/new")
            self.assertEqual(body["nonce"], 1_700_000_000_000)
            self.assertIn("X-TXC-SIGNATURE", signed.headers)

    def test_whitebit_transport_rejects_self_asserted_credential_boundary(self):
        calls = []
        with TemporaryDirectory() as directory:
            allocator = WhiteBitDurableNonceAllocator(
                journal=JournalStore(f"{directory}/journal.sqlite3"),
                account_id="acct-wb",
                environment="LIVE",
                clock_millis=lambda: calls.append("nonce") or 1_700_000_000_000,
                clock_utc=lambda: datetime(
                    2026, 9, 25, 12, 0, tzinfo=timezone.utc
                ),
            )
            with self.assertRaisesRegex(TypeError, "credential_boundary"):
                WhiteBitHttpTransport(
                    policy=WHITEBIT_ENDPOINT_POLICIES["LIVE"],
                    account_id="acct-wb",
                    capability_snapshot_id="wb-cap-1",
                    secret_resolver=FakeSecretResolver(calls),
                    credential_handle=whitebit_trade_handle(),
                    credential_boundary=object(),
                    session_token="session-1",
                    origin="autotrade://execution",
                    execution_identity="sender-1",
                    nonce_allocator=allocator,
                    quota_gate=lambda *_args: None,
                    wire_client=RecordingWire(calls),
                )
            self.assertEqual(calls, [])

    def test_whitebit_same_provider_key_shares_nonce_domain_across_handle_generations(self):
        fixed = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)
        events = []
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            wire = RecordingWire(events, response=b'{"orderId":"123"}')
            first_allocator = WhiteBitDurableNonceAllocator(
                journal=JournalStore(path),
                account_id="acct-wb",
                environment="LIVE",
                clock_millis=lambda: 1_700_000_000_000,
                clock_utc=lambda: fixed,
            )
            second_allocator = WhiteBitDurableNonceAllocator(
                journal=JournalStore(path),
                account_id="acct-wb",
                environment="LIVE",
                clock_millis=lambda: 1_700_000_000_000,
                clock_utc=lambda: fixed + timedelta(milliseconds=1),
            )
            first = WhiteBitHttpTransport(
                policy=WHITEBIT_ENDPOINT_POLICIES["LIVE"],
                account_id="acct-wb",
                capability_snapshot_id="wb-cap-1",
                secret_resolver=FakeSecretResolver(events),
                credential_handle=whitebit_trade_handle(
                    handle_id="cred-whitebit-a",
                    generation=1,
                ),
                session_token="session-1",
                origin="autotrade://execution",
                execution_identity="sender-1",
                nonce_allocator=first_allocator,
                quota_gate=lambda *_args: None,
                wire_client=wire,
            )
            second = WhiteBitHttpTransport(
                policy=WHITEBIT_ENDPOINT_POLICIES["LIVE"],
                account_id="acct-wb",
                capability_snapshot_id="wb-cap-1",
                secret_resolver=FakeSecretResolver(events),
                credential_handle=whitebit_trade_handle(
                    handle_id="cred-whitebit-b",
                    generation=2,
                ),
                session_token="session-2",
                origin="autotrade://execution",
                execution_identity="sender-2",
                nonce_allocator=second_allocator,
                quota_gate=lambda *_args: None,
                wire_client=wire,
            )

            first(
                "at-whitebit-generation-one",
                whitebit_prepared_request("at-whitebit-generation-one"),
                lambda: None,
            )
            second(
                "at-whitebit-generation-two",
                whitebit_prepared_request("at-whitebit-generation-two"),
                lambda: None,
            )
            nonces = [json.loads(request.body)["nonce"] for request in wire.requests]
            self.assertEqual(
                nonces,
                [1_700_000_000_000, 1_700_000_000_001],
            )
            self.assertEqual(
                first_allocator.aggregate_id_for_provider_api_key("api-key-SECRET"),
                second_allocator.aggregate_id_for_provider_api_key("api-key-SECRET"),
            )

    def test_whitebit_concurrent_sends_cannot_overtake_nonce_order(self):
        fixed = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)
        first_wire_entered = Event()
        release_first_wire = Event()
        second_started = Event()
        second_wire_entered = Event()
        wire_calls = []

        class BlockingWhiteBitWire(RecordingWire):
            def send(self, request):
                self.events.append("wire")
                self.requests.append(request)
                wire_calls.append(json.loads(request.body)["nonce"])
                if len(self.requests) == 1:
                    first_wire_entered.set()
                    if not release_first_wire.wait(2):
                        raise TimeoutError("WhiteBIT test wire release timed out")
                else:
                    second_wire_entered.set()
                return TradingWireResponse(
                    http_status=200,
                    body=b'{"orderId":"123"}',
                )

        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            events = []
            wire = BlockingWhiteBitWire(events)
            first_allocator = WhiteBitDurableNonceAllocator(
                journal=JournalStore(path),
                account_id="acct-wb",
                environment="LIVE",
                clock_millis=lambda: 1_700_000_000_000,
                clock_utc=lambda: fixed,
            )
            second_allocator = WhiteBitDurableNonceAllocator(
                journal=JournalStore(path),
                account_id="acct-wb",
                environment="LIVE",
                clock_millis=lambda: 1_700_000_000_000,
                clock_utc=lambda: fixed + timedelta(milliseconds=1),
            )

            def make_transport(allocator, sender):
                return WhiteBitHttpTransport(
                    policy=WHITEBIT_ENDPOINT_POLICIES["LIVE"],
                    account_id="acct-wb",
                    capability_snapshot_id="wb-cap-1",
                    secret_resolver=FakeSecretResolver(events),
                    credential_handle=whitebit_trade_handle(),
                        session_token="session-1",
                    origin="autotrade://execution",
                    execution_identity=sender,
                    nonce_allocator=allocator,
                    quota_gate=lambda *_args: None,
                    wire_client=wire,
                )

            first = make_transport(first_allocator, "sender-1")
            second = make_transport(second_allocator, "sender-2")

            def run_first():
                return first(
                    "at-whitebit-first",
                    whitebit_prepared_request("at-whitebit-first"),
                    lambda: None,
                )

            def run_second():
                second_started.set()
                return second(
                    "at-whitebit-second",
                    whitebit_prepared_request("at-whitebit-second"),
                    lambda: None,
                )

            with ThreadPoolExecutor(max_workers=2) as pool:
                first_future = pool.submit(run_first)
                self.assertTrue(first_wire_entered.wait(2))
                second_future = pool.submit(run_second)
                self.assertTrue(second_started.wait(2))
                self.assertFalse(second_wire_entered.wait(0.1))
                self.assertEqual(wire_calls, [1_700_000_000_000])
                release_first_wire.set()
                first_future.result(timeout=2)
                second_future.result(timeout=2)

            self.assertTrue(second_wire_entered.is_set())
            self.assertEqual(
                wire_calls,
                [1_700_000_000_000, 1_700_000_000_001],
            )

    def test_whitebit_final_guard_failure_never_reaches_wire(self):
        fixed = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)
        events = []
        with TemporaryDirectory() as directory:
            journal = JournalStore(f"{directory}/journal.sqlite3")
            allocator = WhiteBitDurableNonceAllocator(
                journal=journal,
                account_id="acct-wb",
                environment="LIVE",
                clock_millis=lambda: 1_700_000_000_000,
                clock_utc=lambda: fixed,
            )
            wire = RecordingWire(events)
            transport = WhiteBitHttpTransport(
                policy=WHITEBIT_ENDPOINT_POLICIES["LIVE"],
                account_id="acct-wb",
                capability_snapshot_id="wb-cap-1",
                secret_resolver=FakeSecretResolver(events),
                credential_handle=whitebit_trade_handle(),
                session_token="session-1",
                origin="autotrade://execution",
                execution_identity="sender-1",
                nonce_allocator=allocator,
                quota_gate=lambda *_args: None,
                wire_client=wire,
            )
            client_id = "at-whitebit-guard"
            with self.assertRaisesRegex(RuntimeError, "revoked"):
                transport(
                    client_id,
                    whitebit_prepared_request(client_id),
                    lambda: (_ for _ in ()).throw(RuntimeError("revoked")),
                )

            self.assertEqual(wire.requests, [])
            nonce_events = journal.load_events(
                "provider_nonce",
                allocator.aggregate_id_for_provider_api_key("api-key-SECRET"),
            )
            self.assertEqual(len(nonce_events), 1)

    def test_whitebit_rejects_non_order_private_endpoint_before_authority_work(self):
        fixed = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)
        calls = []
        with TemporaryDirectory() as directory:
            allocator = WhiteBitDurableNonceAllocator(
                journal=JournalStore(f"{directory}/journal.sqlite3"),
                account_id="acct-wb",
                environment="LIVE",
                clock_millis=lambda: calls.append("nonce") or 1_700_000_000_000,
                clock_utc=lambda: fixed,
            )
            transport = WhiteBitHttpTransport(
                policy=WHITEBIT_ENDPOINT_POLICIES["LIVE"],
                account_id="acct-wb",
                capability_snapshot_id="wb-cap-1",
                secret_resolver=FakeSecretResolver(calls),
                credential_handle=whitebit_trade_handle(),
                session_token="session-1",
                origin="autotrade://execution",
                execution_identity="sender-1",
                nonce_allocator=allocator,
                quota_gate=lambda *_args: calls.append("quota"),
                wire_client=RecordingWire(calls),
            )
            request = whitebit_prepared_request("at-whitebit-scope")
            request["endpoint"] = "/api/v4/main-account/withdraw"
            with self.assertRaisesRegex(
                ProviderTransportScopeError,
                "canonical order path",
            ):
                transport(
                    "at-whitebit-scope",
                    request,
                    lambda: calls.append("guard"),
                )
            self.assertEqual(calls, [])
            self.assertEqual(
                JournalStore(f"{directory}/journal.sqlite3").load_events(
                    "provider_nonce",
                    allocator.aggregate_id_for_provider_api_key("api-key-SECRET"),
                ),
                [],
            )

    def test_whitebit_rejects_transport_owned_auth_fields_before_allocation(self):
        fixed = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)
        calls = []
        with TemporaryDirectory() as directory:
            allocator = WhiteBitDurableNonceAllocator(
                journal=JournalStore(f"{directory}/journal.sqlite3"),
                account_id="acct-wb",
                environment="LIVE",
                clock_millis=lambda: calls.append("nonce") or 1_700_000_000_000,
                clock_utc=lambda: fixed,
            )
            transport = WhiteBitHttpTransport(
                policy=WHITEBIT_ENDPOINT_POLICIES["LIVE"],
                account_id="acct-wb",
                capability_snapshot_id="wb-cap-1",
                secret_resolver=FakeSecretResolver(calls),
                credential_handle=whitebit_trade_handle(),
                session_token="session-1",
                origin="autotrade://execution",
                execution_identity="sender-1",
                nonce_allocator=allocator,
                quota_gate=lambda *_args: None,
                wire_client=RecordingWire(calls),
            )
            request = whitebit_prepared_request("at-whitebit-auth")
            request["body"]["nonce"] = 7
            with self.assertRaisesRegex(
                ProviderTransportScopeError,
                "transport-owned authentication fields",
            ):
                transport(
                    "at-whitebit-auth",
                    request,
                    lambda: calls.append("guard"),
                )
            self.assertEqual(calls, [])


    def test_whitebit_prepared_request_flows_through_dispatcher_and_ambiguity_never_retries(self):
        with TemporaryDirectory() as directory:
            events = []
            store = JournalStore(f"{directory}/journal.sqlite3")
            fixed = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)
            allocator = WhiteBitDurableNonceAllocator(
                journal=store,
                account_id="acct-wb",
                environment="LIVE",
                clock_millis=lambda: events.append("nonce") or 1_700_000_000_000,
                clock_utc=lambda: fixed,
            )
            wire = RecordingWire(
                events,
                error=TimeoutError("response lost after possible WhiteBIT send"),
            )
            transport = WhiteBitHttpTransport(
                policy=WHITEBIT_ENDPOINT_POLICIES["LIVE"],
                account_id="acct-wb",
                capability_snapshot_id="wb-cap-1",
                secret_resolver=FakeSecretResolver(events),
                credential_handle=whitebit_trade_handle(),
                session_token="session-1",
                origin="autotrade://execution",
                execution_identity="sender-1",
                nonce_allocator=allocator,
                quota_gate=lambda *_args: None,
                wire_client=wire,
            )
            dispatcher = GuardedDispatcher(
                store,
                environment="SIMULATION",
                account_id="acct-wb",
                owner_token="owner-wb",
            )
            intent_id = "intent-whitebit-e2e"
            client_id = stable_client_order_id(
                "WHITEBIT",
                intent_id,
                environment="SIMULATION",
                account_id="acct-wb",
                max_length=32,
                client_id_format="TOKEN",
            )
            # This fixture exercises the real projection shape emitted by the
            # existing WhiteBIT adapter without fabricating another dispatcher.
            actual = WhiteBitPreparedRequest(
                endpoint="/api/v4/order/new",
                body={
                    "market": "BTC_USDT",
                    "side": "buy",
                    "amount": "0.001",
                    "price": "50000",
                    "clientOrderId": client_id,
                    "postOnly": False,
                },
                account_id="acct-wb",
                environment="LIVE",
                capability_snapshot_id="wb-cap-1",
                documentation_refs=("https://docs.whitebit.com/api-reference/overview",),
            )

            result = dispatcher.dispatch(
                attempt_id="attempt-whitebit-e2e",
                intent_id=intent_id,
                intent_hash="intent-hash-whitebit",
                provider="WHITEBIT",
                request=actual.to_guarded_dispatch_request(),
                now="2026-09-25T12:00:00Z",
                authority_check=lambda _hash, _now: (True, "allowed"),
                transport_send=transport,
                final_barrier_clock=lambda: "2026-09-25T12:00:01Z",
                submission_scope={
                    "capability_snapshot_id": "wb-cap-1",
                    "provider": "WHITEBIT",
                    "account_id": "acct-wb",
                    "environment": "LIVE",
                },
            )
            self.assertEqual(result.status, "UNKNOWN")
            self.assertEqual(result.reason, "transport_result_ambiguous")
            self.assertEqual(events.count("wire"), 1)

            repeated = dispatcher.dispatch(
                attempt_id="attempt-whitebit-e2e",
                intent_id=intent_id,
                intent_hash="intent-hash-whitebit",
                provider="WHITEBIT",
                request=actual.to_guarded_dispatch_request(),
                now="2026-09-25T12:00:02Z",
                authority_check=lambda _hash, _now: (True, "allowed"),
                transport_send=transport,
                submission_scope={
                    "capability_snapshot_id": "wb-cap-1",
                    "provider": "WHITEBIT",
                    "account_id": "acct-wb",
                    "environment": "LIVE",
                },
            )
            self.assertEqual(repeated.status, "UNKNOWN")
            self.assertEqual(events.count("wire"), 1)
            nonce_events = store.load_events(
                "provider_nonce",
                allocator.aggregate_id_for_provider_api_key("api-key-SECRET"),
            )
            self.assertEqual(len(nonce_events), 1)


    def test_whitebit_ambiguous_http_write_status_requires_reconciliation(self):
        for status, expected_reason in (
            (408, "whitebit_ambiguous_write_timeout"),
            (429, "whitebit_ambiguous_write_rate_limit"),
            (503, "whitebit_ambiguous_write"),
        ):
            with self.subTest(status=status), TemporaryDirectory() as directory:
                events = []
                allocator = WhiteBitDurableNonceAllocator(
                    journal=JournalStore(f"{directory}/journal.sqlite3"),
                    account_id="acct-wb",
                    environment="LIVE",
                    clock_millis=lambda: 1_700_000_000_000,
                    clock_utc=lambda: datetime(
                        2026, 9, 25, 12, 0, tzinfo=timezone.utc
                    ),
                )
                wire = RecordingWire(
                    events,
                    response=json.dumps(
                        {"code": status, "message": "ambiguous provider response"},
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8"),
                    http_status=status,
                )
                transport = WhiteBitHttpTransport(
                    policy=WHITEBIT_ENDPOINT_POLICIES["LIVE"],
                    account_id="acct-wb",
                    capability_snapshot_id="wb-cap-1",
                    secret_resolver=FakeSecretResolver(events),
                    credential_handle=whitebit_trade_handle(),
                    session_token="session-1",
                    origin="autotrade://execution",
                    execution_identity="sender-1",
                    nonce_allocator=allocator,
                    quota_gate=lambda *_args: None,
                    wire_client=wire,
                )
                response = transport(
                    "at-whitebit-status",
                    whitebit_prepared_request("at-whitebit-status"),
                    lambda: None,
                )
                self.assertEqual(response.http_status, status)
                self.assertTrue(response.requires_reconciliation)
                self.assertEqual(response.ambiguity_reason, expected_reason)
                self.assertEqual(len(wire.requests), 1)

    def test_whitebit_ambiguous_http_status_is_durable_unknown_and_never_retried(self):
        for status, expected_reason in (
            (408, "whitebit_ambiguous_write_timeout"),
            (429, "whitebit_ambiguous_write_rate_limit"),
            (503, "whitebit_ambiguous_write"),
        ):
            with self.subTest(status=status), TemporaryDirectory() as directory:
                events = []
                store = JournalStore(f"{directory}/journal.sqlite3")
                allocator = WhiteBitDurableNonceAllocator(
                    journal=store,
                    account_id="acct-wb",
                    environment="LIVE",
                    clock_millis=lambda: events.append("nonce") or 1_700_000_000_000,
                    clock_utc=lambda: datetime(
                        2026, 9, 25, 12, 0, tzinfo=timezone.utc
                    ),
                )
                raw = json.dumps(
                    {"code": status, "message": "ambiguous provider response"},
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
                wire = RecordingWire(
                    events,
                    response=raw,
                    http_status=status,
                )
                transport = WhiteBitHttpTransport(
                    policy=WHITEBIT_ENDPOINT_POLICIES["LIVE"],
                    account_id="acct-wb",
                    capability_snapshot_id="wb-cap-1",
                    secret_resolver=FakeSecretResolver(events),
                    credential_handle=whitebit_trade_handle(),
                    session_token="session-1",
                    origin="autotrade://execution",
                    execution_identity="sender-1",
                    nonce_allocator=allocator,
                    quota_gate=lambda *_args: None,
                    wire_client=wire,
                )
                dispatcher = GuardedDispatcher(
                    store,
                    environment="SIMULATION",
                    account_id="acct-wb",
                    owner_token="owner-wb",
                )
                intent_id = f"intent-whitebit-http-{status}"
                attempt_id = f"attempt-whitebit-http-{status}"
                client_id = stable_client_order_id(
                    "WHITEBIT",
                    intent_id,
                    environment="SIMULATION",
                    account_id="acct-wb",
                    max_length=32,
                    client_id_format="TOKEN",
                )
                actual = WhiteBitPreparedRequest(
                    endpoint="/api/v4/order/new",
                    body={
                        "market": "BTC_USDT",
                        "side": "buy",
                        "amount": "0.001",
                        "price": "50000",
                        "clientOrderId": client_id,
                        "postOnly": False,
                    },
                    account_id="acct-wb",
                    environment="LIVE",
                    capability_snapshot_id="wb-cap-1",
                    documentation_refs=(
                        "https://docs.whitebit.com/api-reference/overview",
                    ),
                )
                kwargs = {
                    "attempt_id": attempt_id,
                    "intent_id": intent_id,
                    "intent_hash": f"intent-hash-whitebit-{status}",
                    "provider": "WHITEBIT",
                    "request": actual.to_guarded_dispatch_request(),
                    "now": "2026-09-25T12:00:00Z",
                    "authority_check": lambda _hash, _now: (True, "allowed"),
                    "transport_send": transport,
                    "final_barrier_clock": lambda: "2026-09-25T12:00:01Z",
                    "submission_scope": {
                        "capability_snapshot_id": "wb-cap-1",
                        "provider": "WHITEBIT",
                        "account_id": "acct-wb",
                        "environment": "LIVE",
                    },
                }
                result = dispatcher.dispatch(**kwargs)
                self.assertEqual(result.status, "UNKNOWN")
                self.assertEqual(result.reason, expected_reason)
                self.assertEqual(events.count("wire"), 1)

                binding = load_submission_response_binding(
                    store,
                    environment="SIMULATION",
                    account_id="acct-wb",
                    attempt_id=attempt_id,
                )
                self.assertEqual(binding.response_bytes, raw)
                terminal = store.load_events(
                    "submission_attempt",
                    binding.aggregate_id,
                )[-1]
                self.assertEqual(terminal["event_type"], "SubmissionUnknown")
                self.assertEqual(
                    terminal["payload"]["retry_disposition"],
                    "RECONCILE_FIRST",
                )
                self.assertEqual(terminal["payload"]["http_status"], status)

                repeated = dispatcher.dispatch(**kwargs)
                self.assertEqual(repeated.status, "UNKNOWN")
                self.assertEqual(repeated.reason, expected_reason)
                self.assertEqual(events.count("wire"), 1)

    def test_whitebit_definitive_client_rejection_does_not_claim_ambiguity(self):
        events = []
        with TemporaryDirectory() as directory:
            allocator = WhiteBitDurableNonceAllocator(
                journal=JournalStore(f"{directory}/journal.sqlite3"),
                account_id="acct-wb",
                environment="LIVE",
                clock_millis=lambda: 1_700_000_000_000,
                clock_utc=lambda: datetime(
                    2026, 9, 25, 12, 0, tzinfo=timezone.utc
                ),
            )
            transport = WhiteBitHttpTransport(
                policy=WHITEBIT_ENDPOINT_POLICIES["LIVE"],
                account_id="acct-wb",
                capability_snapshot_id="wb-cap-1",
                secret_resolver=FakeSecretResolver(events),
                credential_handle=whitebit_trade_handle(),
                session_token="session-1",
                origin="autotrade://execution",
                execution_identity="sender-1",
                nonce_allocator=allocator,
                quota_gate=lambda *_args: None,
                wire_client=RecordingWire(
                    events,
                    response=b'{"code":400,"message":"validation failed"}',
                    http_status=400,
                ),
            )
            response = transport(
                "at-whitebit-400",
                whitebit_prepared_request("at-whitebit-400"),
                lambda: None,
            )
            self.assertEqual(response.http_status, 400)
            self.assertFalse(response.requires_reconciliation)
            self.assertIsNone(response.ambiguity_reason)


class KrakenSpotProviderTransportTests(unittest.TestCase):
    SYNTHETIC_SECRET = "bm90LWEtcmVhbC1zZWNyZXQtdGVzdC12ZWN0b3I="

    def credential_plaintext(self):
        return json.dumps(
            {
                "api_key": "kraken-test-key",
                "api_secret": self.SYNTHETIC_SECRET,
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    def make_transport(
        self,
        *,
        journal,
        events,
        wire=None,
        quota_gate=None,
        nonce_clock=None,
        credential_handle=None,
    ):
        handle = credential_handle or kraken_trade_handle()
        allocator = KrakenSpotDurableNonceAllocator(
            journal=journal,
            account_id="acct-kraken",
            environment="LIVE",
            credential_handle=handle,
            clock_millis=nonce_clock
            or (lambda: events.append("nonce") or 1_700_000_000_000),
            clock_utc=lambda: datetime(
                2026,
                9,
                26,
                0,
                0,
                tzinfo=timezone.utc,
            ),
        )
        resolver = FakeSecretResolver(
            events,
            credential_plaintext=self.credential_plaintext(),
        )
        transport = KrakenSpotHttpTransport(
            policy=KRAKEN_SPOT_ENDPOINT_POLICIES["LIVE"],
            account_id="acct-kraken",
            capability_snapshot_id="kraken-cap-1",
            secret_resolver=resolver,
            credential_handle=handle,
            session_token="session-kraken",
            origin="autotrade://execution",
            execution_identity="sender-kraken",
            nonce_allocator=allocator,
            quota_gate=quota_gate,
            wire_client=wire or RecordingWire(events),
        )
        return transport, resolver, allocator

    def test_nonce_allocator_rejects_credential_handle_subclass(self):
        base = kraken_trade_handle()

        class HostileHandle(PersistentCredentialHandle):
            pass

        forged = HostileHandle(
            base.handle_id,
            base.account_id,
            base.provider,
            base.environment,
            base.purpose,
            base.generation,
            base.provider_environment,
        )
        with TemporaryDirectory() as directory:
            with self.assertRaisesRegex(TypeError, "credential_handle"):
                KrakenSpotDurableNonceAllocator(
                    journal=JournalStore(f"{directory}/journal.sqlite3"),
                    account_id="acct-kraken",
                    environment="LIVE",
                    credential_handle=forged,
                    clock_millis=lambda: 100,
                )

    def test_signer_matches_independent_synthetic_auth_vector(self):
        request = KrakenSpotSigner.sign(
            policy=KRAKEN_SPOT_ENDPOINT_POLICIES["LIVE"],
            endpoint="/0/private/AddOrder",
            body={
                "ordertype": "limit",
                "pair": "XBTUSD",
                "price": "37500",
                "type": "buy",
                "volume": "1.25",
            },
            credential_plaintext=self.credential_plaintext(),
            nonce=1_616_492_376_594,
        )

        self.assertEqual(
            request.body,
            b"nonce=1616492376594&ordertype=limit&pair=XBTUSD&price=37500&type=buy&volume=1.25",
        )
        self.assertEqual(
            request.headers["API-Sign"],
            "MFLsFjtEUp8B8kVw9TYV/03VU9NoOHPX9lc5tMErSJMSsg7aOZlo0JC6IdO+tqoZGc8yyti21qakVVdxPla/Bw==",
        )
        self.assertEqual(
            request.url,
            "https://api.kraken.com/0/private/AddOrder",
        )


    def test_durable_nonce_survives_restart_and_clock_regression(self):
        fixed = datetime(2026, 9, 26, 0, 0, tzinfo=timezone.utc)
        provider_api_key = "kraken-test-key"
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            handle = kraken_trade_handle()
            first = KrakenSpotDurableNonceAllocator(
                journal=JournalStore(path),
                account_id="acct-kraken",
                environment="LIVE",
                credential_handle=handle,
                clock_millis=lambda: 1_700_000_000_000,
                clock_utc=lambda: fixed,
            )
            first_domain = first.for_provider_api_key(provider_api_key)
            self.assertEqual(first_domain.allocate(), 1_700_000_000_000)

            reopened = KrakenSpotDurableNonceAllocator(
                journal=JournalStore(path),
                account_id="acct-kraken",
                environment="LIVE",
                credential_handle=handle,
                clock_millis=lambda: 1_699_999_999_000,
                clock_utc=lambda: fixed + timedelta(seconds=1),
            )
            reopened_domain = reopened.for_provider_api_key(provider_api_key)
            self.assertEqual(reopened_domain.allocate(), 1_700_000_000_001)
            events = JournalStore(path).load_events(
                "provider_nonce",
                reopened_domain.aggregate_id,
            )
            self.assertEqual(
                [item["payload"]["provider_id"] for item in events],
                ["KRAKEN", "KRAKEN"],
            )
            self.assertEqual(
                [item["payload"]["nonce"] for item in events],
                [1_700_000_000_000, 1_700_000_000_001],
            )
            fingerprint = reopened.provider_api_key_fingerprint(provider_api_key)
            self.assertEqual(
                [
                    item["payload"]["provider_api_key_fingerprint"]
                    for item in events
                ],
                [fingerprint, fingerprint],
            )
            for item in events:
                self.assertNotIn("credential_handle_id", item["payload"])
                self.assertNotIn("credential_generation", item["payload"])
            self.assertNotIn(provider_api_key, json.dumps(events, sort_keys=True))

    def test_nonce_authority_is_shared_by_provider_key_across_handles_and_generations(self):
        fixed = datetime(2026, 9, 26, 0, 0, tzinfo=timezone.utc)
        provider_api_key = "shared-kraken-key"
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            trade_handle = kraken_trade_handle(
                handle_id="kraken-trade-a",
                generation=1,
            )
            read_handle = PersistentCredentialHandle(
                handle_id="kraken-read-b",
                account_id="acct-kraken",
                provider="KRAKEN",
                environment="LIVE",
                purpose="READ",
                generation=1,
            )
            rotated_trade_handle = kraken_trade_handle(
                handle_id="kraken-trade-a",
                generation=2,
            )

            trade = KrakenSpotDurableNonceAllocator(
                journal=store,
                account_id="acct-kraken",
                environment="LIVE",
                credential_handle=trade_handle,
                clock_millis=lambda: 100,
                clock_utc=lambda: fixed,
            )
            read = KrakenSpotDurableNonceAllocator(
                journal=store,
                account_id="acct-kraken",
                environment="LIVE",
                credential_handle=read_handle,
                clock_millis=lambda: 100,
                clock_utc=lambda: fixed,
            )
            rotated_trade = KrakenSpotDurableNonceAllocator(
                journal=store,
                account_id="acct-kraken",
                environment="LIVE",
                credential_handle=rotated_trade_handle,
                clock_millis=lambda: 100,
                clock_utc=lambda: fixed,
            )

            trade_domain = trade.for_provider_api_key(provider_api_key)
            read_domain = read.for_provider_api_key(provider_api_key)
            rotated_domain = rotated_trade.for_provider_api_key(provider_api_key)
            self.assertEqual(trade_domain.allocate(), 100)
            self.assertEqual(read_domain.allocate(), 101)
            self.assertEqual(rotated_domain.allocate(), 102)
            self.assertEqual(
                {
                    trade_domain.aggregate_id,
                    read_domain.aggregate_id,
                    rotated_domain.aggregate_id,
                },
                {trade_domain.aggregate_id},
            )

            restarted_read = KrakenSpotDurableNonceAllocator(
                journal=store,
                account_id="acct-kraken",
                environment="LIVE",
                credential_handle=read_handle,
                clock_millis=lambda: 50,
                clock_utc=lambda: fixed + timedelta(seconds=1),
            ).for_provider_api_key(provider_api_key)
            self.assertEqual(restarted_read.allocate(), 103)

            distinct_key_domain = trade.for_provider_api_key(
                "different-kraken-key"
            )
            self.assertEqual(distinct_key_domain.allocate(), 100)
            self.assertNotEqual(
                distinct_key_domain.aggregate_id,
                trade_domain.aggregate_id,
            )

    def test_provider_key_nonce_domain_inherits_legacy_handle_high_water(self):
        fixed = datetime(2026, 9, 26, 0, 0, tzinfo=timezone.utc)
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            legacy_trade = _DurableProviderNonceAllocator(
                provider_id="KRAKEN",
                display_name="Kraken Spot",
                journal=store,
                account_id="acct-kraken",
                environment="LIVE",
                clock_millis=lambda: 700,
                clock_utc=lambda: fixed,
                scope_fields={
                    "credential_handle_id": "legacy-trade",
                    "credential_generation": 1,
                },
                max_nonce=(1 << 64) - 1,
                nonce_domain_name="unsigned 64-bit",
            )
            legacy_rotated = _DurableProviderNonceAllocator(
                provider_id="KRAKEN",
                display_name="Kraken Spot",
                journal=store,
                account_id="acct-kraken",
                environment="LIVE",
                clock_millis=lambda: 900,
                clock_utc=lambda: fixed + timedelta(milliseconds=1),
                scope_fields={
                    "credential_handle_id": "legacy-trade",
                    "credential_generation": 2,
                },
                max_nonce=(1 << 64) - 1,
                nonce_domain_name="unsigned 64-bit",
            )
            legacy_read = _DurableProviderNonceAllocator(
                provider_id="KRAKEN",
                display_name="Kraken Spot",
                journal=store,
                account_id="acct-kraken",
                environment="LIVE",
                clock_millis=lambda: 800,
                clock_utc=lambda: fixed + timedelta(milliseconds=2),
                scope_fields={
                    "credential_handle_id": "legacy-read",
                    "credential_generation": 1,
                },
                max_nonce=(1 << 64) - 1,
                nonce_domain_name="unsigned 64-bit",
            )
            self.assertEqual(legacy_trade.allocate(), 700)
            self.assertEqual(legacy_rotated.allocate(), 900)
            self.assertEqual(legacy_read.allocate(), 800)

            manager = KrakenSpotDurableNonceAllocator(
                journal=store,
                account_id="acct-kraken",
                environment="LIVE",
                credential_handle=kraken_trade_handle(),
                clock_millis=lambda: 100,
                clock_utc=lambda: fixed + timedelta(seconds=1),
            )
            self.assertEqual(manager.legacy_nonce_floor, 900)
            domain = manager.for_provider_api_key("shared-kraken-key")
            self.assertEqual(domain.allocate(), 901)

            restarted = KrakenSpotDurableNonceAllocator(
                journal=store,
                account_id="acct-kraken",
                environment="LIVE",
                credential_handle=PersistentCredentialHandle(
                    handle_id="restarted-read",
                    account_id="acct-kraken",
                    provider="KRAKEN",
                    environment="LIVE",
                    purpose="READ",
                    generation=1,
                ),
                clock_millis=lambda: 50,
                clock_utc=lambda: fixed + timedelta(seconds=2),
            )
            self.assertEqual(restarted.legacy_nonce_floor, 900)
            restarted_domain = restarted.for_provider_api_key(
                "shared-kraken-key"
            )
            self.assertEqual(restarted_domain.allocate(), 902)


    def test_provider_key_nonce_migration_rejects_corrupt_legacy_history(self):
        fixed = datetime(2026, 9, 26, 0, 0, tzinfo=timezone.utc)

        def legacy_payload(nonce):
            return {
                "provider_id": "KRAKEN",
                "account_id": "acct-kraken",
                "environment": "LIVE",
                "nonce": nonce,
                "credential_handle_id": "legacy-trade",
                "credential_generation": 1,
            }

        def append_event(
            store,
            *,
            aggregate_id,
            aggregate_version,
            nonce,
            event_type="ProviderNonceAllocated",
            event_id,
        ):
            payload = legacy_payload(nonce)
            store.append_event(
                {
                    "event_id": event_id,
                    "event_type": event_type,
                    "aggregate_type": "provider_nonce",
                    "aggregate_id": aggregate_id,
                    "aggregate_version": str(aggregate_version),
                    "payload": payload,
                    "payload_hash": payload_digest(payload),
                    "committed_at": (
                        fixed + timedelta(milliseconds=aggregate_version)
                    ).isoformat().replace("+00:00", "Z"),
                }
            )

        for corruption, expected in (
            ("unexpected_event_type", "unexpected event type"),
            ("nonce_regression", "not strictly monotonic"),
            ("aggregate_identity", "aggregate identity is invalid"),
        ):
            with self.subTest(corruption=corruption), TemporaryDirectory() as directory:
                store = JournalStore(f"{directory}/journal.sqlite3")
                legacy = _DurableProviderNonceAllocator(
                    provider_id="KRAKEN",
                    display_name="Kraken Spot",
                    journal=store,
                    account_id="acct-kraken",
                    environment="LIVE",
                    clock_millis=lambda: 700,
                    clock_utc=lambda: fixed,
                    scope_fields={
                        "credential_handle_id": "legacy-trade",
                        "credential_generation": 1,
                    },
                    max_nonce=(1 << 64) - 1,
                    nonce_domain_name="unsigned 64-bit",
                )

                if corruption == "aggregate_identity":
                    append_event(
                        store,
                        aggregate_id="KRAKEN:" + "0" * 64,
                        aggregate_version=1,
                        nonce=700,
                        event_id="legacy-wrong-aggregate",
                    )
                else:
                    self.assertEqual(legacy.allocate(), 700)
                    append_event(
                        store,
                        aggregate_id=legacy.aggregate_id,
                        aggregate_version=2,
                        nonce=699 if corruption == "nonce_regression" else 701,
                        event_type=(
                            "UnexpectedNonceEvent"
                            if corruption == "unexpected_event_type"
                            else "ProviderNonceAllocated"
                        ),
                        event_id="legacy-corrupt-" + corruption,
                    )

                with self.assertRaisesRegex(ProviderTransportError, expected):
                    KrakenSpotDurableNonceAllocator(
                        journal=store,
                        account_id="acct-kraken",
                        environment="LIVE",
                        credential_handle=kraken_trade_handle(),
                        clock_millis=lambda: 100,
                        clock_utc=lambda: fixed + timedelta(seconds=1),
                    )

    def test_nonce_uint64_boundary_fails_closed_before_persist_or_sign(self):
        maximum = (1 << 64) - 1
        fixed = datetime(2026, 9, 26, 0, 0, tzinfo=timezone.utc)
        provider_api_key = "kraken-test-key"
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            handle = kraken_trade_handle()
            allocator = KrakenSpotDurableNonceAllocator(
                journal=store,
                account_id="acct-kraken",
                environment="LIVE",
                credential_handle=handle,
                clock_millis=lambda: maximum,
                clock_utc=lambda: fixed,
            )
            domain = allocator.for_provider_api_key(provider_api_key)
            self.assertEqual(domain.allocate(), maximum)
            reopened = KrakenSpotDurableNonceAllocator(
                journal=store,
                account_id="acct-kraken",
                environment="LIVE",
                credential_handle=handle,
                clock_millis=lambda: maximum,
                clock_utc=lambda: fixed + timedelta(seconds=1),
            )
            reopened_domain = reopened.for_provider_api_key(provider_api_key)
            with self.assertRaisesRegex(
                ProviderTransportScopeError,
                "exhausted unsigned 64-bit",
            ):
                reopened_domain.allocate()
            self.assertEqual(
                len(
                    store.load_events(
                        "provider_nonce",
                        reopened_domain.aggregate_id,
                    )
                ),
                1,
            )
        with self.assertRaisesRegex(
            ProviderTransportScopeError,
            "unsigned 64-bit",
        ):
            KrakenSpotSigner.sign(
                policy=KRAKEN_SPOT_ENDPOINT_POLICIES["LIVE"],
                endpoint="/0/private/AddOrder",
                body={},
                credential_plaintext=self.credential_plaintext(),
                nonce=maximum + 1,
            )
    def test_transport_rejects_nonce_allocator_from_stale_credential_generation(self):
        with TemporaryDirectory() as directory:
            events = []
            stale = kraken_trade_handle(generation=1)
            current = kraken_trade_handle(generation=2)
            allocator = KrakenSpotDurableNonceAllocator(
                journal=JournalStore(f"{directory}/journal.sqlite3"),
                account_id="acct-kraken",
                environment="LIVE",
                credential_handle=stale,
                clock_millis=lambda: 100,
            )
            with self.assertRaisesRegex(
                ProviderTransportScopeError,
                "credential/account/environment scope mismatch",
            ):
                KrakenSpotHttpTransport(
                    policy=KRAKEN_SPOT_ENDPOINT_POLICIES["LIVE"],
                    account_id="acct-kraken",
                    capability_snapshot_id="kraken-cap-1",
                    secret_resolver=FakeSecretResolver(
                        events,
                        credential_plaintext=self.credential_plaintext(),
                    ),
                    credential_handle=current,
                    session_token="session-kraken",
                    origin="autotrade://execution",
                    execution_identity="sender-kraken",
                    nonce_allocator=allocator,
                    wire_client=RecordingWire(events),
                )
            self.assertEqual(events, [])


    def test_transport_orders_quota_secret_nonce_guard_and_one_wire_send(self):
        events = []
        with TemporaryDirectory() as directory:
            wire = RecordingWire(
                events,
                response=b'{"error":[],"result":{"txid":["O-1"]}}',
            )
            transport, resolver, _allocator = self.make_transport(
                journal=JournalStore(f"{directory}/journal.sqlite3"),
                events=events,
                wire=wire,
                quota_gate=lambda provider, account, environment, purpose: (
                    events.append("quota"),
                    self.assertEqual(
                        (provider, account, environment, purpose),
                        ("KRAKEN", "acct-kraken", "LIVE", "ORDER_WRITE"),
                    ),
                )[-1],
            )
            client_id = "at-kraken-1"
            response = transport(
                client_id,
                kraken_prepared_request(client_id),
                lambda: events.append("guard"),
            )

            self.assertEqual(
                events,
                ["quota", "resolve", "nonce", "guard", "wire"],
            )
            self.assertEqual(
                response.payload,
                {"error": [], "result": {"txid": ["O-1"]}},
            )
            self.assertEqual(len(resolver.calls), 1)
            self.assertEqual(len(wire.requests), 1)
            signed = wire.requests[0]
            self.assertEqual(
                signed.url,
                "https://api.kraken.com/0/private/AddOrder",
            )
            self.assertEqual(signed.headers["API-Key"], "kraken-test-key")
            self.assertNotIn("not-a-real-secret-test-vector", repr(signed))
            self.assertIn(b"nonce=1700000000000", signed.body)
            self.assertIn(b"cl_ord_id=at-kraken-1", signed.body)
            self.assertIn(b"timeinforce=GTC", signed.body)
    def test_ioc_is_emitted_with_exact_provider_case(self):
        events = []
        with TemporaryDirectory() as directory:
            wire = RecordingWire(
                events,
                response=b'{"error":[],"result":{"txid":["O-IOC"]}}',
            )
            transport, _resolver, _allocator = self.make_transport(
                journal=JournalStore(f"{directory}/journal.sqlite3"),
                events=events,
                wire=wire,
            )
            transport(
                "kraken-ioc",
                kraken_prepared_request("kraken-ioc", time_in_force="IOC"),
                lambda: events.append("guard"),
            )
            self.assertIn(b"timeinforce=IOC", wire.requests[0].body)

    def test_lowercase_tif_fails_before_nonce_secret_and_wire(self):
        events = []
        with TemporaryDirectory() as directory:
            wire = RecordingWire(events)
            transport, resolver, _allocator = self.make_transport(
                journal=JournalStore(f"{directory}/journal.sqlite3"),
                events=events,
                wire=wire,
            )
            with self.assertRaisesRegex(
                ProviderTransportScopeError,
                "timeinforce must be GTC or IOC",
            ):
                transport(
                    "kraken-tif",
                    kraken_prepared_request("kraken-tif", time_in_force="gtc"),
                    lambda: events.append("guard"),
                )
            self.assertEqual(events, [])
            self.assertEqual(resolver.calls, [])
            self.assertEqual(wire.requests, [])


    def test_nonce_send_lock_identity_is_provider_key_scoped_across_handles(self):
        provider_api_key = "shared-kraken-key"
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            trade_handle = kraken_trade_handle()
            read_handle = PersistentCredentialHandle(
                handle_id="cred-kraken-read-other",
                account_id=trade_handle.account_id,
                provider=trade_handle.provider,
                environment=trade_handle.environment,
                purpose="READ",
                generation=1,
            )
            rotated_trade = PersistentCredentialHandle(
                handle_id=trade_handle.handle_id,
                account_id=trade_handle.account_id,
                provider=trade_handle.provider,
                environment=trade_handle.environment,
                purpose=trade_handle.purpose,
                generation=2,
            )
            trade = KrakenSpotDurableNonceAllocator(
                journal=JournalStore(path),
                account_id="acct-kraken",
                environment="LIVE",
                credential_handle=trade_handle,
                clock_millis=lambda: 100,
            )
            read = KrakenSpotDurableNonceAllocator(
                journal=JournalStore(path),
                account_id="acct-kraken",
                environment="LIVE",
                credential_handle=read_handle,
                clock_millis=lambda: 100,
            )
            rotated = KrakenSpotDurableNonceAllocator(
                journal=JournalStore(path),
                account_id="acct-kraken",
                environment="LIVE",
                credential_handle=rotated_trade,
                clock_millis=lambda: 100,
            )
            paths = {
                trade.send_lock_path_for_provider_api_key(provider_api_key),
                read.send_lock_path_for_provider_api_key(provider_api_key),
                rotated.send_lock_path_for_provider_api_key(provider_api_key),
            }
            self.assertEqual(len(paths), 1)
            self.assertNotIn(
                trade.send_lock_path_for_provider_api_key(
                    "different-kraken-key"
                ),
                paths,
            )

    def test_cross_handle_nonce_send_lock_serializes_same_provider_key(self):
        provider_api_key = "shared-kraken-key"
        first_entered = Event()
        second_entered = Event()
        release_first = Event()

        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            trade = KrakenSpotDurableNonceAllocator(
                journal=store,
                account_id="acct-kraken",
                environment="LIVE",
                credential_handle=kraken_trade_handle(),
                clock_millis=lambda: 100,
            )
            read = KrakenSpotDurableNonceAllocator(
                journal=store,
                account_id="acct-kraken",
                environment="LIVE",
                credential_handle=PersistentCredentialHandle(
                    handle_id="cred-kraken-read-other",
                    account_id="acct-kraken",
                    provider="KRAKEN",
                    environment="LIVE",
                    purpose="READ",
                    generation=1,
                ),
                clock_millis=lambda: 100,
            )
            trade_domain = trade.for_provider_api_key(provider_api_key)
            read_domain = read.for_provider_api_key(provider_api_key)

            def hold_first():
                with trade_domain.serialized_send():
                    first_entered.set()
                    if not release_first.wait(2):
                        raise TimeoutError("test nonce lock release timed out")

            def enter_second():
                if not first_entered.wait(2):
                    raise TimeoutError("first nonce lock was not acquired")
                with read_domain.serialized_send():
                    second_entered.set()

            with ThreadPoolExecutor(max_workers=2) as pool:
                first_future = pool.submit(hold_first)
                second_future = pool.submit(enter_second)
                self.assertTrue(first_entered.wait(2))
                self.assertFalse(second_entered.wait(0.1))
                release_first.set()
                first_future.result(timeout=2)
                second_future.result(timeout=2)
                self.assertTrue(second_entered.is_set())

    def test_nonce_send_lock_is_enforced_across_process_boundary(self):
        probe = r"""
import os
import sys

path = sys.argv[1]
with open(path, "a+b") as stream:
    stream.seek(0, os.SEEK_END)
    if stream.tell() == 0:
        stream.write(b"0")
        stream.flush()
    stream.seek(0)
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(
                stream.fileno(),
                fcntl.LOCK_EX | fcntl.LOCK_NB,
            )
    except OSError:
        print("BUSY")
    else:
        stream.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        print("FREE")
"""

        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            allocator = KrakenSpotDurableNonceAllocator(
                journal=JournalStore(path),
                account_id="acct-kraken",
                environment="LIVE",
                credential_handle=kraken_trade_handle(),
                clock_millis=lambda: 100,
            )
            nonce_domain = allocator.for_provider_api_key(
                "kraken-test-key"
            )

            with nonce_domain.serialized_send():
                blocked = subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        probe,
                        str(nonce_domain._send_lock_path),
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                self.assertEqual(blocked.stdout.strip(), "BUSY")

            released = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    probe,
                    str(nonce_domain._send_lock_path),
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=5,
            )
            self.assertEqual(released.stdout.strip(), "FREE")
    def test_same_credential_concurrent_sends_serialize_nonce_through_wire(self):
        events = []
        first_wire_entered = Event()
        release_first_wire = Event()
        second_started = Event()

        class BlockingWire(RecordingWire):
            def send(self, request):
                self.events.append("wire")
                self.requests.append(request)
                if len(self.requests) == 1:
                    first_wire_entered.set()
                    if not release_first_wire.wait(2):
                        raise TimeoutError("test wire release timed out")
                return TradingWireResponse(
                    http_status=200,
                    body=b'{"error":[],"result":{"txid":["O-1"]}}',
                )

        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            wire = BlockingWire(events)
            handle = kraken_trade_handle()
            first, _resolver1, _allocator1 = self.make_transport(
                journal=store,
                events=events,
                wire=wire,
                credential_handle=handle,
            )
            second, _resolver2, _allocator2 = self.make_transport(
                journal=store,
                events=events,
                wire=wire,
                credential_handle=handle,
            )

            def run_second():
                second_started.set()
                return second(
                    "kraken-two",
                    kraken_prepared_request("kraken-two"),
                    lambda: events.append("guard-two"),
                )

            with ThreadPoolExecutor(max_workers=2) as pool:
                first_future = pool.submit(
                    first,
                    "kraken-one",
                    kraken_prepared_request("kraken-one"),
                    lambda: events.append("guard-one"),
                )
                self.assertTrue(first_wire_entered.wait(1))
                second_future = pool.submit(run_second)
                self.assertTrue(second_started.wait(1))
                self.assertEqual(events.count("nonce"), 1)
                self.assertEqual(len(wire.requests), 1)
                release_first_wire.set()
                first_future.result(timeout=2)
                second_future.result(timeout=2)

            self.assertEqual(events.count("nonce"), 2)
            self.assertEqual(len(wire.requests), 2)
            self.assertIn(b"nonce=1700000000000", wire.requests[0].body)
            self.assertIn(b"nonce=1700000000001", wire.requests[1].body)

    def test_scope_or_forged_auth_fields_fail_before_secret_and_wire(self):
        for mutation, message in (
            (("capability_snapshot_id", "wrong-cap"), "capability snapshot mismatch"),
            (("cl_ord_id", "other-id"), "client order identity mismatch"),
            (("nonce", "1"), "not canonical"),
        ):
            with self.subTest(mutation=mutation[0]):
                events = []
                with TemporaryDirectory() as directory:
                    wire = RecordingWire(events)
                    transport, resolver, _allocator = self.make_transport(
                        journal=JournalStore(f"{directory}/journal.sqlite3"),
                        events=events,
                        wire=wire,
                    )
                    request = kraken_prepared_request("at-kraken-1")
                    request = {
                        "endpoint": request["endpoint"],
                        "body": dict(request["body"]),
                        "capability_snapshot_id": request["capability_snapshot_id"],
                    }
                    field, value = mutation
                    if field == "capability_snapshot_id":
                        request[field] = value
                    else:
                        request["body"][field] = value
                    with self.assertRaisesRegex(
                        ProviderTransportScopeError,
                        message,
                    ):
                        transport(
                            "at-kraken-1",
                            request,
                            lambda: events.append("guard"),
                        )
                    self.assertEqual(events, [])
                    self.assertEqual(resolver.calls, [])
                    self.assertEqual(wire.requests, [])

    def test_deadline_elapsed_exact_response_is_durable_unknown_without_resend(self):
        events = []
        now = "2026-09-26T00:00:00Z"
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            raw = b'{"error":["EService:Deadline elapsed"],"result":null}'
            wire = RecordingWire(events, response=raw)
            transport, resolver, allocator = self.make_transport(
                journal=store,
                events=events,
                wire=wire,
            )
            dispatcher = GuardedDispatcher(
                store,
                environment="SIMULATION",
                account_id="acct-kraken",
            )
            intent_id = "kraken-live-deadline-intent"
            client_id = stable_client_order_id(
                "KRAKEN",
                intent_id,
                environment="SIMULATION",
                account_id="acct-kraken",
                max_length=36,
                client_id_format="UUID",
            )
            kwargs = {
                "attempt_id": "22222222-2222-4222-8222-222222222222",
                "intent_id": intent_id,
                "intent_hash": "sha256:" + "2" * 64,
                "provider": "KRAKEN",
                "request": kraken_prepared_request(client_id),
                "now": now,
                "authority_check": lambda _provider, _environment: (
                    True,
                    "allowed",
                ),
                "transport_send": transport,
                "client_id_max_length": 36,
                "client_id_format": "UUID",
                "final_barrier_clock": lambda: now,
            }

            outcome = dispatcher.dispatch(**kwargs)
            self.assertEqual(outcome.status, "UNKNOWN")
            self.assertEqual(outcome.reason, "kraken_spot_deadline_elapsed")
            self.assertEqual(len(wire.requests), 1)

            binding = load_submission_response_binding(
                store,
                environment="SIMULATION",
                account_id="acct-kraken",
                attempt_id=kwargs["attempt_id"],
            )
            self.assertEqual(binding.response_bytes, raw)
            self.assertEqual(
                binding.payload["error"],
                ("EService:Deadline elapsed",),
            )
            self.assertIsNone(binding.payload["result"])
            terminal = store.load_events(
                "submission_attempt",
                binding.aggregate_id,
            )[-1]
            self.assertEqual(terminal["event_type"], "SubmissionUnknown")
            self.assertEqual(
                terminal["payload"]["response_sha256"],
                binding.response_sha256,
            )

            events_before_restart = list(events)
            repeated = dispatcher.dispatch(**kwargs)
            self.assertEqual(repeated.status, "UNKNOWN")
            self.assertEqual(
                repeated.reason,
                "kraken_spot_deadline_elapsed",
            )
            self.assertEqual(events, events_before_restart)
            self.assertEqual(len(wire.requests), 1)
            self.assertEqual(len(resolver.calls), 1)
            self.assertEqual(
                len(
                    store.load_events(
                        "provider_nonce",
                        allocator.aggregate_id_for_provider_api_key(
                            "kraken-test-key"
                        ),
                    )
                ),
                1,
            )

    def test_post_barrier_wire_failure_is_unknown_without_retry(self):
        events = []
        now = "2026-09-26T00:00:00Z"
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            wire = RecordingWire(
                events,
                error=TimeoutError("ambiguous provider timeout"),
            )
            transport, resolver, _allocator = self.make_transport(
                journal=store,
                events=events,
                wire=wire,
            )
            dispatcher = GuardedDispatcher(
                store,
                environment="SIMULATION",
                account_id="acct-kraken",
            )
            intent_id = "kraken-live-intent-1"
            client_id = stable_client_order_id(
                "KRAKEN",
                intent_id,
                environment="SIMULATION",
                account_id="acct-kraken",
                max_length=36,
                client_id_format="UUID",
            )
            outcome = dispatcher.dispatch(
                attempt_id="11111111-1111-4111-8111-111111111111",
                intent_id=intent_id,
                intent_hash="sha256:" + "1" * 64,
                provider="KRAKEN",
                request=kraken_prepared_request(client_id),
                now=now,
                authority_check=lambda _hash, _now: (True, "allowed"),
                transport_send=transport,
                client_id_max_length=36,
                client_id_format="UUID",
                final_barrier_clock=lambda: now,
            )

            self.assertEqual(outcome.status, "UNKNOWN")
            self.assertEqual(outcome.reason, "transport_result_ambiguous")
            self.assertEqual(events, ["resolve", "nonce", "wire"])
            self.assertEqual(len(resolver.calls), 1)
            self.assertEqual(len(wire.requests), 1)


class ProviderTransportTests(unittest.TestCase):
    def test_binance_signer_has_fixed_exact_vector(self):
        signed = BinanceSpotSigner.sign(
            policy=BINANCE_SPOT_ENDPOINT_POLICIES["PAPER"],
            endpoint="/api/v3/order",
            body={
                "symbol": "BTCUSDT",
                "side": "BUY",
                "type": "LIMIT",
                "quantity": "0.001",
                "price": "50000",
                "timeInForce": "GTC",
                "newClientOrderId": "at-test",
                "newOrderRespType": "ACK",
            },
            credential_plaintext='{"api_key":"key","api_secret":"secret"}',
            timestamp_ms=1700000000000,
            recv_window_ms=5000,
        )
        self.assertEqual(signed.method, "POST")
        self.assertEqual(
            signed.url,
            "https://testnet.binance.vision/api/v3/order",
        )
        expected_unsigned = (
            "newClientOrderId=at-test&newOrderRespType=ACK&price=50000&"
            "quantity=0.001&recvWindow=5000&side=BUY&symbol=BTCUSDT&"
            "timeInForce=GTC&timestamp=1700000000000&type=LIMIT"
        )
        self.assertEqual(
            signed.body,
            (
                expected_unsigned
                + "&signature="
                + "5adc47a515932e840bf7a85cfec228ba8c3b3c71d7e894e1f4a975a98389b21d"
            ).encode("ascii"),
        )
        self.assertEqual(signed.headers["X-MBX-APIKEY"], "key")

    def test_environment_host_policy_fails_closed(self):
        with self.assertRaisesRegex(
            ProviderTransportScopeError,
            "outside the explicit allowlist",
        ):
            ProviderEndpointPolicy(
                provider_id="BINANCE",
                environment="PAPER",
                base_url="https://api.binance.com",
                allowed_hosts=frozenset({"testnet.binance.vision"}),
            )
        with self.assertRaisesRegex(
            ProviderTransportScopeError,
            "origin-only HTTPS",
        ):
            ProviderEndpointPolicy(
                provider_id="BINANCE",
                environment="LIVE",
                base_url="http://api.binance.com",
                allowed_hosts=frozenset({"api.binance.com"}),
            )

    def make_transport(
        self,
        *,
        events,
        wire=None,
        quota_gate=None,
        capability_snapshot_id="cap-1",
    ):
        resolver = FakeSecretResolver(events)
        transport = BinanceSpotHttpTransport(
            policy=BINANCE_SPOT_ENDPOINT_POLICIES["PAPER"],
            account_id="acct-1",
            capability_snapshot_id=capability_snapshot_id,
            secret_resolver=resolver,
            credential_handle=trade_handle(),
            session_token="session-token",
            origin="https://localhost",
            execution_identity="host-owner",
            clock_millis=lambda: 1700000000000,
            quota_gate=quota_gate,
            wire_client=wire or RecordingWire(events),
        )
        return transport, resolver

    def test_binance_transport_rejects_credential_handle_subclass(self):
        base = trade_handle()

        class HostileHandle(PersistentCredentialHandle):
            pass

        forged = HostileHandle(
            base.handle_id,
            base.account_id,
            base.provider,
            base.environment,
            base.purpose,
            base.generation,
            base.provider_environment,
        )
        events = []
        with self.assertRaisesRegex(TypeError, "credential_handle"):
            BinanceSpotHttpTransport(
                policy=BINANCE_SPOT_ENDPOINT_POLICIES["PAPER"],
                account_id="acct-1",
                capability_snapshot_id="cap-1",
                secret_resolver=FakeSecretResolver(events),
                credential_handle=forged,
                session_token="session-token",
                origin="https://localhost",
                execution_identity="host-owner",
                clock_millis=lambda: 1700000000000,
                wire_client=RecordingWire(events),
            )
        self.assertEqual(events, [])

    def test_quota_gate_secret_sign_guard_wire_order_is_exact(self):
        events = []
        wire = RecordingWire(events)

        def quota(provider, account, environment, purpose):
            events.append("quota")
            self.assertEqual(
                (provider, account, environment, purpose),
                ("BINANCE", "acct-1", "PAPER", "ORDER_WRITE"),
            )

        transport, resolver = self.make_transport(
            events=events,
            wire=wire,
            quota_gate=quota,
        )
        client_id = "at-client-1"

        def final_guard():
            events.append("guard")

        response = transport(
            client_id,
            prepared_request(client_id),
            final_guard,
        )
        self.assertEqual(response.payload["ok"], True)
        self.assertEqual(response.http_status, 200)
        self.assertEqual(events, ["quota", "resolve", "guard", "wire"])
        self.assertEqual(len(resolver.calls), 1)
        self.assertEqual(resolver.calls[0]["purpose"], "TRADE")
        self.assertEqual(resolver.calls[0]["environment"], "PAPER")
        self.assertEqual(len(wire.requests), 1)
        self.assertIn(
            b"newClientOrderId=at-client-1",
            wire.requests[0].body,
        )

    def test_trade_credential_generation_lease_covers_final_guard_and_wire(self):
        events = []
        resolver = FakeSecretResolver(events)
        wire = LeaseAssertingWire(events, resolver)
        transport = BinanceSpotHttpTransport(
            policy=BINANCE_SPOT_ENDPOINT_POLICIES["PAPER"],
            account_id="acct-1",
            capability_snapshot_id="cap-1",
            secret_resolver=resolver,
            credential_handle=trade_handle(),
            session_token="session-token",
            origin="https://localhost",
            execution_identity="host-owner",
            clock_millis=lambda: 1700000000000,
            quota_gate=None,
            wire_client=wire,
        )
        client_id = "at-lease-trade"

        def final_guard():
            self.assertTrue(
                resolver.lease_active,
                "final guard must execute inside exact credential generation lease",
            )
            events.append("guard-lease-active")

        response = transport(
            client_id,
            prepared_request(client_id),
            final_guard,
        )

        self.assertEqual(response.http_status, 200)
        self.assertFalse(resolver.lease_active)
        self.assertEqual(resolver.lease_enters, 1)
        self.assertEqual(resolver.lease_exits, 1)
        self.assertEqual(
            events,
            [
                "resolve",
                "guard-lease-active",
                "wire-lease-active",
                "wire",
            ],
        )

    def test_capability_mismatch_rejects_before_secret_guard_or_wire(self):
        events = []
        wire = RecordingWire(events)
        transport, resolver = self.make_transport(events=events, wire=wire)
        with self.assertRaisesRegex(
            ProviderTransportScopeError,
            "capability snapshot mismatch",
        ):
            transport(
                "at-client-1",
                prepared_request(
                    "at-client-1",
                    capability_snapshot_id="other-cap",
                ),
                lambda: events.append("guard"),
            )
        self.assertEqual(events, [])
        self.assertEqual(resolver.calls, [])
        self.assertEqual(wire.requests, [])

    def test_client_identity_mismatch_rejects_before_secret_or_wire(self):
        events = []
        wire = RecordingWire(events)
        transport, resolver = self.make_transport(events=events, wire=wire)
        with self.assertRaisesRegex(
            ProviderTransportScopeError,
            "client order identity mismatch",
        ):
            transport(
                "at-client-1",
                prepared_request("at-client-OTHER"),
                lambda: events.append("guard"),
            )
        self.assertEqual(events, [])
        self.assertEqual(resolver.calls, [])
        self.assertEqual(wire.requests, [])

    def test_prepared_request_text_is_not_silently_normalized_before_signing(self):
        events = []
        wire = RecordingWire(events)
        transport, resolver = self.make_transport(events=events, wire=wire)

        request = prepared_request("at-client-1")
        request["endpoint"] = " /api/v3/order "
        with self.assertRaisesRegex(
            ProviderTransportScopeError,
            "endpoint must be canonical text",
        ):
            transport(
                "at-client-1",
                request,
                lambda: events.append("guard"),
            )

        request = prepared_request("at-client-1")
        request["body"][" symbol"] = request["body"].pop("symbol")
        with self.assertRaisesRegex(
            ProviderTransportScopeError,
            "order parameter must be canonical text",
        ):
            transport(
                "at-client-1",
                request,
                lambda: events.append("guard"),
            )

        self.assertEqual(events, [])
        self.assertEqual(resolver.calls, [])
        self.assertEqual(wire.requests, [])

    def test_signer_rejects_noncanonical_key_instead_of_rebinding_signed_bytes(self):
        body = prepared_request("at-client-1")["body"].copy()
        body[" symbol"] = body.pop("symbol")
        with self.assertRaisesRegex(
            ProviderTransportScopeError,
            "order parameter must be canonical text",
        ):
            BinanceSpotSigner.sign(
                policy=BINANCE_SPOT_ENDPOINT_POLICIES["PAPER"],
                endpoint="/api/v3/order",
                body=body,
                credential_plaintext='{"api_key":"key","api_secret":"secret"}',
                timestamp_ms=1700000000000,
            )

    def test_quota_failure_is_before_secret_and_before_final_guard(self):
        events = []
        wire = RecordingWire(events)

        def quota(*_args):
            events.append("quota")
            raise RuntimeError("quota unavailable")

        transport, resolver = self.make_transport(
            events=events,
            wire=wire,
            quota_gate=quota,
        )
        with self.assertRaisesRegex(RuntimeError, "quota unavailable"):
            transport(
                "at-client-1",
                prepared_request("at-client-1"),
                lambda: events.append("guard"),
            )
        self.assertEqual(events, ["quota"])
        self.assertEqual(resolver.calls, [])
        self.assertEqual(wire.requests, [])

    def test_wrong_scoped_trade_handle_is_rejected_at_construction(self):
        events = []
        with self.assertRaisesRegex(
            ProviderTransportScopeError,
            "provider/environment/purpose mismatch",
        ):
            BinanceSpotHttpTransport(
                policy=BINANCE_SPOT_ENDPOINT_POLICIES["PAPER"],
                account_id="acct-1",
                capability_snapshot_id="cap-1",
                secret_resolver=FakeSecretResolver(events),
                credential_handle=trade_handle(environment="LIVE"),
                session_token="session-token",
                origin="https://localhost",
                execution_identity="host-owner",
                clock_millis=lambda: 1700000000000,
                wire_client=RecordingWire(events),
            )

    def test_binance_5xx_and_backend_timeout_are_unknown_not_definitive(self):
        cases = (
            (503, b'{"code":-1000,"msg":"backend failure"}',
             "binance_spot_http_5xx_execution_unknown"),
            (200, b'{"code":-1007,"msg":"Timeout waiting for response"}',
             "binance_spot_backend_timeout_execution_unknown"),
        )
        for status, body, reason in cases:
            with self.subTest(status=status, body=body):
                exact = _binance_exact_trading_response(
                    TradingWireResponse(http_status=status, body=body)
                )
                self.assertTrue(exact.requires_reconciliation)
                self.assertEqual(exact.ambiguity_reason, reason)
                self.assertEqual(exact.http_status, status)
                self.assertEqual(exact.response_bytes, body)
        definite = _binance_exact_trading_response(
            TradingWireResponse(http_status=400, body=b'{"code":-1013,"msg":"filter"}')
        )
        self.assertFalse(definite.requires_reconciliation)
        self.assertEqual(definite.http_status, 400)
        successful = _binance_exact_trading_response(
            TradingWireResponse(http_status=200, body=b'{"orderId":123}')
        )
        self.assertFalse(successful.requires_reconciliation)
        self.assertEqual(successful.http_status, 200)

    def test_binance_ambiguous_http_after_send_is_durable_unknown_without_retry(self):
        cases = (
            (503, b'{"code":-1000,"msg":"server"}',
             "binance_spot_http_5xx_execution_unknown"),
            (200, b'{"code":-1007,"msg":"timeout"}',
             "binance_spot_backend_timeout_execution_unknown"),
        )
        for status, body, reason in cases:
            with self.subTest(status=status), TemporaryDirectory() as directory:
                events = []
                wire = RecordingWire(events, response=body, http_status=status)
                transport, _ = self.make_transport(events=events, wire=wire)
                store = JournalStore(f"{directory}/journal.sqlite3")
                dispatcher = GuardedDispatcher(
                    store,
                    environment="SIMULATION",
                    account_id="acct-1",
                    owner_token="owner-1",
                )
                intent_id = f"intent-binance-ambiguous-{status}"
                attempt_id = f"attempt-binance-ambiguous-{status}"
                client_id = stable_client_order_id(
                    "BINANCE", intent_id,
                    environment="SIMULATION", account_id="acct-1",
                )
                args = dict(
                    attempt_id=attempt_id,
                    intent_id=intent_id,
                    intent_hash=f"intent-binance-ambiguous-hash-{status}",
                    provider="BINANCE",
                    request=prepared_request(client_id),
                    now="2026-09-25T10:00:00Z",
                    authority_check=lambda _hash, _now: (True, "allowed"),
                    transport_send=transport,
                    final_barrier_clock=lambda: "2026-09-25T10:00:01Z",
                    submission_scope={
                        "capability_snapshot_id": "cap-1",
                        "provider": "BINANCE",
                        "account_id": "acct-1",
                        "environment": "PAPER",
                    },
                )
                outcome = dispatcher.dispatch(**args)
                self.assertEqual(outcome.status, "UNKNOWN")
                self.assertEqual(outcome.reason, reason)
                self.assertEqual(len(wire.requests), 1)
                aggregate_id = dispatcher._aggregate_id(attempt_id)
                events_saved = store.load_events("submission_attempt", aggregate_id)
                self.assertEqual(
                    [event["event_type"] for event in events_saved],
                    ["SubmissionPrepared", "SubmissionSending", "SubmissionUnknown"],
                )
                terminal = events_saved[-1]["payload"]
                self.assertEqual(terminal["http_status"], status)
                self.assertEqual(terminal["response_text"], body.decode("utf-8"))
                self.assertEqual(terminal["reason"], reason)
                self.assertEqual(terminal["retry_disposition"], "RECONCILE_FIRST")
                self.assertNotIn("response", terminal)
                restarted = GuardedDispatcher(
                    JournalStore(f"{directory}/journal.sqlite3"),
                    environment="SIMULATION",
                    account_id="acct-1",
                    owner_token="owner-1",
                )
                repeated = restarted.dispatch(
                    **{**args, "transport_send": lambda *_: self.fail("blind resend")}
                )
                self.assertEqual(repeated.status, "UNKNOWN")
                self.assertEqual(len(wire.requests), 1)

    def test_definitive_http_rejection_is_durable_response_not_unknown(self):
        with TemporaryDirectory() as directory:
            events = []
            body = b'{"code":-1013,"msg":"Filter failure: LOT_SIZE"}'
            wire = RecordingWire(
                events,
                response=body,
                http_status=400,
            )
            transport, _resolver = self.make_transport(
                events=events,
                wire=wire,
            )
            store = JournalStore(f"{directory}/journal.sqlite3")
            dispatcher = GuardedDispatcher(
                store,
                environment="SIMULATION",
                account_id="acct-1",
                owner_token="owner-1",
            )
            intent_id = "intent-http-reject-1"
            client_id = stable_client_order_id(
                "BINANCE",
                intent_id,
                environment="SIMULATION",
                account_id="acct-1",
            )
            result = dispatcher.dispatch(
                attempt_id="attempt-http-reject-1",
                intent_id=intent_id,
                intent_hash="intent-http-reject-hash",
                provider="BINANCE",
                request=prepared_request(client_id),
                now="2026-09-25T10:00:00Z",
                authority_check=lambda _hash, _now: (True, "allowed"),
                transport_send=transport,
                final_barrier_clock=lambda: "2026-09-25T10:00:01Z",
                submission_scope={
                    "capability_snapshot_id": "cap-1",
                    "provider": "BINANCE",
                    "account_id": "acct-1",
                    "environment": "PAPER",
                },
            )
            self.assertEqual(result.status, "SENT")
            self.assertEqual(result.response["code"], -1013)
            self.assertEqual(events.count("wire"), 1)

            binding = load_submission_response_binding(
                store,
                environment="SIMULATION",
                account_id="acct-1",
                attempt_id="attempt-http-reject-1",
            )
            self.assertEqual(binding.http_status, 400)
            self.assertEqual(binding.response_bytes, body)

            restarted = JournalStore(f"{directory}/journal.sqlite3")
            recovered = load_submission_response_binding(
                restarted,
                environment="SIMULATION",
                account_id="acct-1",
                attempt_id="attempt-http-reject-1",
            )
            self.assertEqual(recovered.http_status, 400)
            self.assertEqual(recovered.response_sha256, binding.response_sha256)

    def test_dispatcher_marks_post_barrier_response_loss_unknown_and_never_retries(self):
        with TemporaryDirectory() as directory:
            events = []
            wire = RecordingWire(
                events,
                error=TimeoutError("response lost after possible send"),
            )
            transport, _resolver = self.make_transport(
                events=events,
                wire=wire,
            )
            store = JournalStore(f"{directory}/journal.sqlite3")
            dispatcher = GuardedDispatcher(
                store,
                environment="SIMULATION",
                account_id="acct-1",
                owner_token="owner-1",
            )
            intent_id = "intent-transport-1"
            client_id = stable_client_order_id(
                "BINANCE",
                intent_id,
                environment="SIMULATION",
                account_id="acct-1",
            )
            request = prepared_request(client_id)

            result = dispatcher.dispatch(
                attempt_id="attempt-transport-1",
                intent_id=intent_id,
                intent_hash="intent-hash-1",
                provider="BINANCE",
                request=request,
                now="2026-09-25T10:00:00Z",
                authority_check=lambda _hash, _now: (True, "allowed"),
                transport_send=transport,
                final_barrier_clock=lambda: "2026-09-25T10:00:01Z",
                submission_scope={
                    "capability_snapshot_id": "cap-1",
                    "provider": "BINANCE",
                    "account_id": "acct-1",
                    "environment": "PAPER",
                },
            )
            self.assertEqual(result.status, "UNKNOWN")
            self.assertEqual(result.reason, "transport_result_ambiguous")
            self.assertEqual(events.count("wire"), 1)

            durable = store.load_events(
                "submission_attempt",
                dispatcher._aggregate_id("attempt-transport-1"),
            )
            self.assertEqual(
                [event["event_type"] for event in durable],
                [
                    "SubmissionPrepared",
                    "SubmissionSending",
                    "SubmissionUnknown",
                ],
            )
            durable_text = json.dumps(durable, sort_keys=True)
            self.assertNotIn("api-key-SECRET", durable_text)
            self.assertNotIn("signing-SECRET", durable_text)
            self.assertNotIn("signature=", durable_text)

            # The same attempt is recovery-only: it must not execute wire I/O again.
            repeated = dispatcher.dispatch(
                attempt_id="attempt-transport-1",
                intent_id=intent_id,
                intent_hash="intent-hash-1",
                provider="BINANCE",
                request=request,
                now="2026-09-25T10:00:02Z",
                authority_check=lambda _hash, _now: (True, "allowed"),
                transport_send=transport,
                submission_scope={
                    "capability_snapshot_id": "cap-1",
                    "provider": "BINANCE",
                    "account_id": "acct-1",
                    "environment": "PAPER",
                },
            )
            self.assertEqual(repeated.status, "UNKNOWN")
            self.assertEqual(events.count("wire"), 1)

    def test_dispatcher_quota_outage_is_blocked_with_zero_outbound(self):
        with TemporaryDirectory() as directory:
            events = []
            wire = RecordingWire(events)

            def quota(*_args):
                events.append("quota")
                raise RuntimeError("quota unavailable")

            transport, resolver = self.make_transport(
                events=events,
                wire=wire,
                quota_gate=quota,
            )
            store = JournalStore(f"{directory}/journal.sqlite3")
            dispatcher = GuardedDispatcher(
                store,
                environment="SIMULATION",
                account_id="acct-1",
                owner_token="owner-1",
            )
            intent_id = "intent-quota-1"
            client_id = stable_client_order_id(
                "BINANCE",
                intent_id,
                environment="SIMULATION",
                account_id="acct-1",
            )
            result = dispatcher.dispatch(
                attempt_id="attempt-quota-1",
                intent_id=intent_id,
                intent_hash="intent-hash-q",
                provider="BINANCE",
                request=prepared_request(client_id),
                now="2026-09-25T10:00:00Z",
                authority_check=lambda _hash, _now: (True, "allowed"),
                transport_send=transport,
            )
            self.assertEqual(result.status, "BLOCKED")
            self.assertEqual(result.reason, "transport_failed_before_send")
            self.assertEqual(events, ["quota"])
            self.assertEqual(resolver.calls, [])
            self.assertEqual(wire.requests, [])


    def test_oversized_real_http_body_after_send_is_durable_unknown_and_never_resends(self):
        class OversizeStream:
            status = 200
            def __enter__(self):
                return self
            def __exit__(self, *_):
                return False
            def read(self, size):
                self.read_sizes.append(size)
                return b"x" * size
            def __init__(self):
                self.read_sizes = []

        class OversizeWire:
            def __init__(self):
                self.calls = 0
                self.streams = []
            def send(self, request):
                self.calls += 1
                client = UrllibJsonWireClient(max_response_bytes=8)
                stream = OversizeStream()
                self.streams.append(stream)
                class FakeOpener:
                    def open(self, *_args, **_kwargs):
                        return stream
                client._opener = FakeOpener()
                return client.send(request)

        with TemporaryDirectory() as directory:
            events = []
            wire = OversizeWire()
            transport, _resolver = self.make_transport(events=events, wire=wire)
            store = JournalStore(f"{directory}/journal.sqlite3")
            dispatcher = GuardedDispatcher(
                store,
                environment="SIMULATION",
                account_id="acct-1",
                owner_token="owner-1",
            )
            intent_id = "intent-http-byte-budget"
            client_id = stable_client_order_id(
                "BINANCE", intent_id, environment="SIMULATION", account_id="acct-1"
            )
            arguments = dict(
                attempt_id="attempt-http-byte-budget",
                intent_id=intent_id,
                intent_hash="intent-http-byte-budget-hash",
                provider="BINANCE",
                request=prepared_request(client_id),
                now="2026-09-25T10:00:00Z",
                authority_check=lambda _hash, _now: (True, "allowed"),
                transport_send=transport,
                final_barrier_clock=lambda: "2026-09-25T10:00:01Z",
                submission_scope={
                    "capability_snapshot_id": "cap-1",
                    "provider": "BINANCE",
                    "account_id": "acct-1",
                    "environment": "PAPER",
                },
            )
            result = dispatcher.dispatch(**arguments)
            self.assertEqual(result.status, "UNKNOWN")
            self.assertEqual(wire.calls, 1)
            self.assertEqual(wire.streams[0].read_sizes, [9])
            self.assertEqual(
                [x["event_type"] for x in dispatcher._events("attempt-http-byte-budget")],
                ["SubmissionPrepared", "SubmissionSending", "SubmissionUnknown"],
            )
            repeated = dispatcher.dispatch(**arguments)
            self.assertEqual(repeated.status, "UNKNOWN")
            self.assertEqual(wire.calls, 1)



    def test_trade_transport_rejects_legacy_resolve_only_secret_resolver(self):
        class ResolveOnlySecretResolver:
            def resolve_for_execution(self, *_args, **_kwargs):
                return '{"api_key":"legacy","api_secret":"legacy"}'

        with self.assertRaisesRegex(
            TypeError,
            "secret_resolver must implement lease_for_execution",
        ):
            BinanceSpotHttpTransport(
                policy=BINANCE_SPOT_ENDPOINT_POLICIES["PAPER"],
                account_id="acct-1",
                capability_snapshot_id="cap-1",
                secret_resolver=ResolveOnlySecretResolver(),
                credential_handle=trade_handle(),
                session_token="session-token",
                origin="https://localhost",
                execution_identity="host-owner",
                clock_millis=lambda: 1700000000000,
                wire_client=RecordingWire([]),
            )


class AuthenticatedReadTransportTests(unittest.TestCase):
    def make_read_transport(
        self,
        *,
        events,
        wire=None,
        quota_gate=None,
        capability=None,
        capability_registry=None,
        secret_resolver=None,
        clock_utc=None,
    ):
        resolver = secret_resolver or FakeSecretResolver(events)
        final_capability = capability or verified_read_capability()

        if capability_registry is None:
            capability_registry = RecordingCapabilityRegistry(events)
            capability_registry.add(final_capability)

        transport = BinanceSpotAuthenticatedReadTransport(
            policy=BINANCE_SPOT_ENDPOINT_POLICIES["PAPER"],
            account_id="acct-1",
            capability_snapshot_id=READ_SNAPSHOT_ID,
            capability_registry=capability_registry,
            secret_resolver=resolver,
            credential_handle=read_handle(),
            session_token="read-session-token",
            origin="https://localhost",
            execution_identity="host-owner",
            clock_millis=lambda: 1700000000000,
            clock_utc=clock_utc or (lambda: READ_NOW + timedelta(seconds=1)),
            quota_gate=quota_gate,
            wire_client=wire or RecordingWire(events),
        )
        return transport, resolver

    def test_authenticated_read_signer_has_fixed_exact_vector(self):
        request = BinanceSpotAuthenticatedReadSigner.sign(
            policy=BINANCE_SPOT_ENDPOINT_POLICIES["PAPER"],
            query_binding=authenticated_read_binding(),
            credential_plaintext='{"api_key":"key","api_secret":"secret"}',
            timestamp_ms=1700000000000,
            recv_window_ms=5000,
        )
        self.assertEqual(
            request.url,
            "https://testnet.binance.vision/api/v3/account?"
            "omitZeroBalances=true&recvWindow=5000&timestamp=1700000000000&"
            "signature=5091638040086d5386ccd440209d33de457b747b3595a67f1dc82e0352a99249",
        )
        self.assertEqual(request.headers["X-MBX-APIKEY"], "key")
        self.assertNotIn("secret", request.url.lower())

    def test_read_transport_binds_scope_exact_bytes_and_call_order(self):
        events = []
        wire = RecordingWire(events, response=b'{"balances":[{"asset":"USD"}]}')

        def quota(provider, account, environment, purpose):
            events.append("quota")
            self.assertEqual(
                (provider, account, environment, purpose),
                ("BINANCE", "acct-1", "PAPER", "AUTHENTICATED_READ"),
            )

        transport, resolver = self.make_read_transport(
            events=events,
            wire=wire,
            quota_gate=quota,
        )
        binding = authenticated_read_binding()
        observation = transport(binding)

        self.assertEqual(events, ["quota", "capability", "resolve", "capability", "wire"])
        self.assertEqual(len(wire.requests), 1)
        self.assertEqual(len(resolver.calls), 1)
        self.assertEqual(resolver.calls[0]["purpose"], "READ")
        self.assertEqual(observation.provider_id, "BINANCE")
        self.assertEqual(observation.account_id, "acct-1")
        self.assertEqual(observation.environment, "PAPER")
        self.assertEqual(observation.query_binding, binding)
        self.assertTrue(observation.response_sha256.startswith("sha256:"))
        self.assertTrue(observation.evidence_ref.startswith("provider-read:sha256:"))
        self.assertEqual(observation.payload["balances"][0]["asset"], "USD")
        self.assertNotIn("SECRET", observation.evidence_ref)

    def test_read_credential_generation_lease_covers_final_currentness_and_wire(self):
        events = []
        resolver = FakeSecretResolver(events)
        wire = LeaseAssertingWire(
            events,
            resolver,
            response=b'{"balances":[{"asset":"USD"}]}',
        )
        transport, _ = self.make_read_transport(
            events=events,
            wire=wire,
            secret_resolver=resolver,
        )

        observation = transport(authenticated_read_binding())

        self.assertEqual(observation.provider_id, "BINANCE")
        self.assertFalse(resolver.lease_active)
        self.assertEqual(resolver.lease_enters, 1)
        self.assertEqual(resolver.lease_exits, 1)
        self.assertEqual(
            events,
            [
                "capability",
                "resolve",
                "capability",
                "wire-lease-active",
                "wire",
            ],
        )

    def test_credential_generation_lease_releases_after_wire_failure(self):
        events = []
        resolver = FakeSecretResolver(events)
        wire = LeaseAssertingWire(
            events,
            resolver,
            error=OSError("synthetic post-barrier failure"),
        )
        transport = BinanceSpotHttpTransport(
            policy=BINANCE_SPOT_ENDPOINT_POLICIES["PAPER"],
            account_id="acct-1",
            capability_snapshot_id="cap-1",
            secret_resolver=resolver,
            credential_handle=trade_handle(),
            session_token="session-token",
            origin="https://localhost",
            execution_identity="host-owner",
            clock_millis=lambda: 1700000000000,
            quota_gate=None,
            wire_client=wire,
        )
        client_id = "at-lease-failure"

        with self.assertRaisesRegex(OSError, "synthetic post-barrier failure"):
            transport(
                client_id,
                prepared_request(client_id),
                lambda: None,
            )

        self.assertFalse(resolver.lease_active)
        self.assertEqual(resolver.lease_enters, 1)
        self.assertEqual(resolver.lease_exits, 1)
        self.assertEqual(events[-1], "wire")

    def test_my_trades_activity_transport_flows_into_fill_parser(self):
        events = []
        capability = verified_read_capability(
            permission_scopes=frozenset({"TRADE.READ"}),
            data_entitlements=frozenset({"TRADES"}),
        )
        body = (
            b'[{"symbol":"BTCUSDT","id":7,"orderId":42,'
            b'"price":"100.2500","qty":"0.2000","commission":"0.0010",'
            b'"commissionAsset":"BNB","isBuyer":true,"time":1790272800123}]'
        )
        transport, _resolver = self.make_read_transport(
            events=events,
            capability=capability,
            wire=RecordingWire(events, response=body, http_status=200),
        )
        binding = authenticated_read_binding(
            endpoint="/api/v3/myTrades",
            query={"symbol": "BTCUSDT"},
            capability=capability,
            permission_scope="TRADE.READ",
            surface=Surface.ACTIVITIES,
        )
        observation = transport(binding)
        fills = parse_account_trades(
            observation,
            instrument_versions={"BTCUSDT": "BTCUSDT@1"},
            client_ids_by_order_id={42: "at-fill-42"},
        )
        self.assertEqual(len(fills), 1)
        fill = fills[0]
        self.assertEqual(fill.quantity, Decimal("0.2000"))
        self.assertEqual(fill.price, Decimal("100.2500"))
        self.assertEqual(fill.fee_amount, Decimal("0.0010"))
        self.assertEqual(fill.evidence_refs, (observation.evidence_ref,))

        wrong_binding = authenticated_read_binding(
            endpoint="/api/v3/myTrades",
            query={"symbol": "BTCUSDT"},
            capability=capability,
            permission_scope="TRADE.READ",
            surface=Surface.AUTHENTICATED_READ,
        )
        with self.assertRaisesRegex(
            ProviderTransportScopeError,
            "surface does not match",
        ):
            transport(wrong_binding)

    def test_read_scope_mismatch_rejects_before_secret_or_wire(self):
        events = []
        wire = RecordingWire(events)
        transport, resolver = self.make_read_transport(events=events, wire=wire)
        other_binding = prepare_authenticated_read_query(
            capability=derive_capability_snapshot(
                snapshot_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                claims=tuple(
                    CapabilityClaim(
                        source=source,
                        provider_id="BINANCE",
                        account_id="other-account",
                        entity_id="entity-1",
                        environment="PAPER",
                        instrument_version="BTCUSDT@1",
                        observed_at=READ_NOW - timedelta(minutes=1),
                        expires_at=READ_NOW + timedelta(minutes=10),
                        supported_order_types=frozenset({"LIMIT"}),
                        time_in_force=frozenset({"GTC"}),
                        permission_scopes=frozenset({"ORDER.READ"}),
                        position_mode="NET",
                        native_protection=frozenset(),
                        rate_limit_policy_id="binance-test-v1",
                        data_entitlements=frozenset({"ACCOUNT"}),
                        evidence_ref={
                            "artifact_id": _READ_ARTIFACT_IDS[source],
                            "sha256": "sha256:" + "b" * 64,
                            "observed_at": (
                                READ_NOW - timedelta(minutes=1)
                            ).isoformat().replace("+00:00", "Z"),
                        },
                    )
                    for source in ("DOCUMENTED", "API", "ACCOUNT", "INSTRUMENT")
                ),
                observed_at=READ_NOW,
                evidence_verifier=lambda _claim: EvidenceVerification(valid=True),
            ),
            surface=Surface.AUTHENTICATED_READ,
            endpoint="/api/v3/account",
            query=None,
            at=READ_NOW,
            permission_scope="ORDER.READ",
        )
        with self.assertRaisesRegex(
            ProviderTransportScopeError,
            "query scope mismatch",
        ):
            transport(other_binding)
        self.assertEqual(events, [])
        self.assertEqual(resolver.calls, [])

    def test_trade_credential_cannot_be_reused_for_authenticated_read(self):
        with self.assertRaisesRegex(
            ProviderTransportScopeError,
            "READ credential handle",
        ):
            BinanceSpotAuthenticatedReadTransport(
                policy=BINANCE_SPOT_ENDPOINT_POLICIES["PAPER"],
                account_id="acct-1",
                capability_snapshot_id=READ_SNAPSHOT_ID,
                capability_registry=CapabilityRegistry(),
                secret_resolver=FakeSecretResolver([]),
                credential_handle=trade_handle(),
                session_token="read-session-token",
                origin="https://localhost",
                execution_identity="host-owner",
                clock_millis=lambda: 1700000000000,
                clock_utc=lambda: READ_NOW,
            )

    def test_final_capability_expiry_after_quota_blocks_before_secret_or_wire(self):
        events = []
        wire = RecordingWire(events)
        capability = verified_read_capability()

        def quota(*_args):
            events.append("quota")

        transport, resolver = self.make_read_transport(
            events=events,
            wire=wire,
            quota_gate=quota,
            capability=capability,
            clock_utc=lambda: READ_NOW + timedelta(minutes=11),
        )
        with self.assertRaisesRegex(
            ProviderTransportScopeError,
            "cannot be verified",
        ):
            transport(authenticated_read_binding(capability=capability))
        self.assertEqual(events, ["quota", "capability"])
        self.assertEqual(resolver.calls, [])
        self.assertEqual(wire.requests, [])

    def test_newer_capability_supersedes_prepared_snapshot_before_secret_or_wire(self):
        events = []
        wire = RecordingWire(events)
        capability = verified_read_capability()
        registry = RecordingCapabilityRegistry(events)
        registry.add(capability)
        registry.add(
            verified_read_capability(
                snapshot_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                snapshot_observed_at=READ_NOW + timedelta(milliseconds=500),
                data_entitlements=frozenset({"MARKET_DATA"}),
            )
        )

        transport, resolver = self.make_read_transport(
            events=events,
            wire=wire,
            capability=capability,
            capability_registry=registry,
            clock_utc=lambda: READ_NOW + timedelta(seconds=1),
        )
        with self.assertRaisesRegex(
            ProviderTransportScopeError,
            "no longer valid",
        ):
            transport(authenticated_read_binding(capability=capability))
        self.assertEqual(events, ["capability"])
        self.assertEqual(resolver.calls, [])
        self.assertEqual(wire.requests, [])

    def test_supersession_after_secret_resolution_blocks_final_wire_send(self):
        events = []
        wire = RecordingWire(events)
        capability = verified_read_capability()
        registry = RecordingCapabilityRegistry(events)
        registry.add(capability)
        replacement = verified_read_capability(
            snapshot_id="cccccccc-cccc-4ccc-8ccc-cccccccccccc",
            snapshot_observed_at=READ_NOW + timedelta(milliseconds=500),
            data_entitlements=frozenset({"MARKET_DATA"}),
        )

        def supersede():
            registry.add(replacement)

        secret_resolver = FakeSecretResolver(events, on_resolve=supersede)
        transport, secret_resolver = self.make_read_transport(
            events=events,
            wire=wire,
            capability=capability,
            capability_registry=registry,
            secret_resolver=secret_resolver,
            clock_utc=lambda: READ_NOW + timedelta(seconds=1),
        )
        with self.assertRaisesRegex(
            ProviderTransportScopeError,
            "no longer valid",
        ):
            transport(authenticated_read_binding(capability=capability))
        self.assertEqual(events, ["capability", "resolve", "capability"])
        self.assertEqual(len(secret_resolver.calls), 1)
        self.assertEqual(wire.requests, [])

    def test_unsupported_authenticated_endpoint_fails_before_secret_or_wire(self):
        events = []
        wire = RecordingWire(events)
        transport, resolver = self.make_read_transport(events=events, wire=wire)
        binding = authenticated_read_binding(endpoint="/api/v3/order")
        with self.assertRaisesRegex(
            ProviderTransportScopeError,
            "endpoint is not explicitly allowed",
        ):
            transport(binding)
        self.assertEqual(events, [])
        self.assertEqual(resolver.calls, [])
        self.assertEqual(wire.requests, [])

    def test_non_write_mutating_scope_name_cannot_cross_read_endpoint_policy(self):
        events = []
        wire = RecordingWire(events)
        capability = verified_read_capability(
            permission_scopes=frozenset({"ORDER.PLACE"}),
        )
        binding = authenticated_read_binding(
            capability=capability,
            permission_scope="ORDER.PLACE",
        )
        transport, resolver = self.make_read_transport(
            events=events,
            wire=wire,
            capability=capability,
        )
        with self.assertRaisesRegex(
            ProviderTransportScopeError,
            "permission scope does not match",
        ):
            transport(binding)
        self.assertEqual(events, [])
        self.assertEqual(resolver.calls, [])
        self.assertEqual(wire.requests, [])

    def test_required_data_entitlement_is_revalidated_before_secret_or_wire(self):
        events = []
        wire = RecordingWire(events)
        capability = verified_read_capability(
            data_entitlements=frozenset({"MARKET_DATA"}),
        )
        transport, resolver = self.make_read_transport(
            events=events,
            wire=wire,
            capability=capability,
        )
        with self.assertRaisesRegex(
            ProviderTransportScopeError,
            "no longer valid",
        ):
            transport(authenticated_read_binding(capability=capability))
        self.assertEqual(events, ["capability"])
        self.assertEqual(resolver.calls, [])
        self.assertEqual(wire.requests, [])

    def test_unexpected_http_status_is_never_promoted_to_provider_state(self):
        body = b'{"balances":[{"asset":"USD"}]}'
        for status in (201, 202, 206, 401, 429, 500):
            events = []
            wire = RecordingWire(events, response=body, http_status=status)
            transport, resolver = self.make_read_transport(
                events=events,
                wire=wire,
            )
            with self.subTest(status=status), self.assertRaisesRegex(
                ProviderTransportError,
                "unexpected HTTP status",
            ):
                transport(authenticated_read_binding())
            self.assertEqual(events, ["capability", "resolve", "capability", "wire"])
            self.assertEqual(len(resolver.calls), 1)
            self.assertEqual(len(wire.requests), 1)

    def test_http_status_is_bound_into_read_evidence_identity(self):
        body = b'{"balances":[{"asset":"USD"}]}'
        binding = authenticated_read_binding()
        observations = [
            observe_authenticated_json_response(
                query_binding=binding,
                http_status=status,
                response_bytes=body,
                observed_at=READ_NOW + timedelta(seconds=1),
            )
            for status in (200, 201)
        ]
        self.assertEqual(
            observations[0].response_sha256,
            observations[1].response_sha256,
        )
        self.assertNotEqual(
            observations[0].evidence_ref,
            observations[1].evidence_ref,
        )

    def test_authenticated_read_preserves_exact_decimal_json_numbers(self):
        events = []
        body = (
            b'{"small":0.1,"precise":1234567890.12345678901234567890,'
            b'"exponent":1e-18,"negative":-42.5000,"integer":7}'
        )
        transport, _resolver = self.make_read_transport(
            events=events,
            wire=RecordingWire(events, response=body),
        )
        observation = transport(authenticated_read_binding())

        self.assertIsInstance(observation.payload["small"], Decimal)
        self.assertEqual(observation.payload["small"], Decimal("0.1"))
        self.assertEqual(
            observation.payload["precise"],
            Decimal("1234567890.12345678901234567890"),
        )
        self.assertEqual(observation.payload["exponent"], Decimal("1e-18"))
        self.assertEqual(observation.payload["negative"], Decimal("-42.5000"))
        self.assertEqual(observation.payload["integer"], 7)
        self.assertNotIsInstance(observation.payload["small"], float)

    def test_authenticated_read_rejects_non_finite_json_numbers(self):
        for token in (b"NaN", b"Infinity", b"-Infinity"):
            events = []
            body = b'{"value":' + token + b"}"
            transport, _resolver = self.make_read_transport(
                events=events,
                wire=RecordingWire(events, response=body),
            )
            with self.subTest(token=token), self.assertRaisesRegex(
                ValueError,
                "non-finite JSON constant",
            ):
                transport(authenticated_read_binding())

    def test_malformed_provider_json_is_not_retried(self):
        events = []
        wire = RecordingWire(events, response=b'{"duplicate":1,"duplicate":2}')
        transport, resolver = self.make_read_transport(events=events, wire=wire)
        with self.assertRaisesRegex(
            ValueError,
            "duplicate JSON key",
        ):
            transport(authenticated_read_binding())
        self.assertEqual(events, ["capability", "resolve", "capability", "wire"])
        self.assertEqual(len(wire.requests), 1)
        self.assertEqual(len(resolver.calls), 1)



class KrakenSpotAuthenticatedReadTransportTests(unittest.TestCase):
    @staticmethod
    def credential_plaintext():
        return json.dumps(
            {"api_key": "key", "api_secret": "c2VjcmV0"},
            sort_keys=True,
            separators=(",", ":"),
        )

    def make_transport(
        self,
        *,
        directory,
        events,
        wire=None,
        quota_gate=None,
        capability=None,
        capability_registry=None,
        secret_resolver=None,
        clock_utc=None,
        credential_handle=None,
        nonce_clock=None,
    ):
        handle = credential_handle or kraken_read_handle()
        final_capability = capability or verified_kraken_read_capability()
        if capability_registry is None:
            capability_registry = RecordingCapabilityRegistry(events)
            capability_registry.add(final_capability)
        resolver = secret_resolver or FakeSecretResolver(
            events,
            credential_plaintext=self.credential_plaintext(),
        )
        allocator = KrakenSpotDurableNonceAllocator(
            journal=JournalStore(f"{directory}/journal.sqlite3"),
            account_id="acct-kraken",
            environment="LIVE",
            credential_handle=handle,
            clock_millis=nonce_clock
            or (lambda: events.append("nonce") or 1_700_000_000_000),
            clock_utc=lambda: KRAKEN_READ_NOW,
        )
        transport = KrakenSpotAuthenticatedReadTransport(
            policy=KRAKEN_SPOT_ENDPOINT_POLICIES["LIVE"],
            account_id="acct-kraken",
            capability_snapshot_id=final_capability.snapshot_id,
            capability_registry=capability_registry,
            secret_resolver=resolver,
            credential_handle=handle,
            session_token="kraken-read-session",
            origin="autotrade://execution",
            execution_identity="host-owner",
            nonce_allocator=allocator,
            clock_utc=clock_utc
            or (lambda: KRAKEN_READ_NOW + timedelta(seconds=1)),
            quota_gate=quota_gate,
            wire_client=wire or RecordingWire(events),
        )
        return transport, resolver, allocator

    def test_private_read_signer_has_fixed_exact_hmac_vector(self):
        request = KrakenSpotAuthenticatedReadSigner.sign(
            policy=KRAKEN_SPOT_ENDPOINT_POLICIES["LIVE"],
            query_binding=kraken_authenticated_read_binding(
                query={"trades": "true"}
            ),
            credential_plaintext=self.credential_plaintext(),
            nonce=1_616_492_376_594,
        )
        self.assertEqual(request.method, "POST")
        self.assertEqual(
            request.url,
            "https://api.kraken.com/0/private/OpenOrders",
        )
        self.assertEqual(
            request.body,
            b"nonce=1616492376594&trades=true",
        )
        self.assertEqual(request.headers["API-Key"], "key")
        self.assertEqual(
            request.headers["API-Sign"],
            "QVIsPGuJdkFIcNbtLozLyl6gEqYmiq6fKBcHnKuxJZ0H7G25wyC7qdD+"
            "dgW7wOHGCdK6rWoU9eE3AyNdkQuQIw==",
        )
        self.assertNotIn("c2VjcmV0", request.body.decode("ascii"))
        self.assertNotIn("c2VjcmV0", request.url)


    def test_open_orders_read_binds_scope_nonce_capability_and_exact_bytes(self):
        events = []
        wire = RecordingWire(
            events,
            response=b'{"error":[],"result":{"open":{}}}',
            http_status=200,
        )

        def quota(provider, account, environment, purpose):
            events.append("quota")
            self.assertEqual(
                (provider, account, environment, purpose),
                ("KRAKEN", "acct-kraken", "LIVE", "AUTHENTICATED_READ"),
            )

        with TemporaryDirectory() as directory:
            transport, resolver, allocator = self.make_transport(
                directory=directory,
                events=events,
                wire=wire,
                quota_gate=quota,
            )
            binding = kraken_authenticated_read_binding(
                query={"trades": "true"}
            )
            observation = transport(binding)

            self.assertEqual(
                events,
                ["quota", "capability", "resolve", "nonce", "capability", "wire"],
            )
            self.assertEqual(len(resolver.calls), 1)
            self.assertEqual(resolver.calls[0]["purpose"], "READ")
            self.assertEqual(len(wire.requests), 1)
            request = wire.requests[0]
            self.assertIsInstance(request, AuthenticatedReadHttpRequest)
            self.assertEqual(request.method, "POST")
            self.assertEqual(
                request.url,
                "https://api.kraken.com/0/private/OpenOrders",
            )
            self.assertEqual(
                request.body,
                b"nonce=1700000000000&trades=true",
            )
            self.assertEqual(observation.provider_id, "KRAKEN")
            self.assertEqual(observation.account_id, "acct-kraken")
            self.assertEqual(observation.environment, "LIVE")
            self.assertEqual(observation.query_binding, binding)
            self.assertEqual(observation.payload["error"], ())
            self.assertTrue(observation.evidence_ref.startswith("provider-read:sha256:"))
            nonce_events = allocator.journal.load_events(
                "provider_nonce",
                allocator.aggregate_id_for_provider_api_key("key"),
            )
            self.assertEqual(len(nonce_events), 1)
            fingerprint = allocator.provider_api_key_fingerprint("key")
            self.assertEqual(
                nonce_events[0]["payload"]["provider_api_key_fingerprint"],
                fingerprint,
            )
            self.assertNotIn("credential_handle_id", nonce_events[0]["payload"])
            self.assertNotIn("credential_generation", nonce_events[0]["payload"])
            self.assertNotIn('"key"', json.dumps(nonce_events, sort_keys=True))
    def test_trades_history_pagination_query_flows_into_existing_fill_parser(self):
        events = []
        capability = verified_kraken_read_capability(
            permission_scopes=frozenset({"TRADE.READ"}),
            data_entitlements=frozenset({"TRADES"}),
        )
        body = (
            b'{"error":[],"result":{"trades":{"T-1":{'
            b'"pair":"XBTUSD","ordertxid":"O-1","type":"buy",'
            b'"vol":"0.2500","price":"40000.00","fee":"2.50",'
            b'"time":"1790384400.000000"}}}}'
        )
        with TemporaryDirectory() as directory:
            transport, _resolver, _allocator = self.make_transport(
                directory=directory,
                events=events,
                capability=capability,
                wire=RecordingWire(events, response=body, http_status=200),
            )
            binding = kraken_authenticated_read_binding(
                endpoint="/0/private/TradesHistory",
                query={"ofs": "50"},
                capability=capability,
                permission_scope="TRADE.READ",
            )
            observation = transport(binding)
            fills = parse_trade_history(
                observation,
                instrument_versions={"XBTUSD": "XBTUSD@1"},
                client_ids_by_provider_order={"O-1": "kraken-trade-1"},
                fee_currency_by_pair={"XBTUSD": "USD"},
            )
            request = transport.wire_client.requests[0]

        self.assertIn(b"nonce=1700000000000", request.body)
        self.assertIn(b"ofs=50", request.body)
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0].side, "BUY")
        self.assertEqual(fills[0].quantity, Decimal("0.2500"))
        self.assertEqual(fills[0].price, Decimal("40000.00"))
        self.assertEqual(fills[0].fee_amount, Decimal("2.50"))
        self.assertEqual(fills[0].evidence_refs, (observation.evidence_ref,))

    def test_query_orders_read_is_bounded_and_uses_existing_read_authority(self):
        events = []
        capability = verified_kraken_read_capability(
            permission_scopes=frozenset({"ORDER.READ"}),
            data_entitlements=frozenset({"ORDERS"}),
        )
        wire = RecordingWire(
            events,
            response=b'{"error":[],"result":{"O-ONE":{},"O-TWO":{}}}',
            http_status=200,
        )
        with TemporaryDirectory() as directory:
            transport, _resolver, _allocator = self.make_transport(
                directory=directory,
                events=events,
                capability=capability,
                wire=wire,
            )
            binding = kraken_authenticated_read_binding(
                endpoint="/0/private/QueryOrders",
                query={
                    "txid": "O-ONE,O-TWO",
                    "trades": "false",
                    "consolidate_taker": "true",
                },
                capability=capability,
                permission_scope="ORDER.READ",
            )
            observation = transport(binding)
            request = wire.requests[0]

        self.assertEqual(
            request.url,
            "https://api.kraken.com/0/private/QueryOrders",
        )
        self.assertIn(b"txid=O-ONE%2CO-TWO", request.body)
        self.assertIn(b"trades=false", request.body)
        self.assertEqual(set(observation.payload["result"]), {"O-ONE", "O-TWO"})
        self.assertEqual(observation.query_binding, binding)

    def test_invalid_private_read_query_fails_before_quota_nonce_secret_or_wire(self):
        cases = (
            (
                "/0/private/OpenOrders",
                {"unexpected": "1"},
                "ORDER.READ",
                frozenset({"ORDERS"}),
                "unsupported fields",
            ),
            (
                "/0/private/OpenOrders",
                {"trades": "True"},
                "ORDER.READ",
                frozenset({"ORDERS"}),
                "canonical boolean text",
            ),
            (
                "/0/private/OpenOrders",
                {"userref": str(1 << 31)},
                "ORDER.READ",
                frozenset({"ORDERS"}),
                "outside the documented range",
            ),
            (
                "/0/private/QueryOrders",
                {},
                "ORDER.READ",
                frozenset({"ORDERS"}),
                "requires txid",
            ),
            (
                "/0/private/QueryOrders",
                {"txid": ",".join(f"O-{index}" for index in range(51))},
                "ORDER.READ",
                frozenset({"ORDERS"}),
                "1..50 unique order ids",
            ),
            (
                "/0/private/QueryOrders",
                {"txid": "O-ONE,O-ONE"},
                "ORDER.READ",
                frozenset({"ORDERS"}),
                "1..50 unique order ids",
            ),
            (
                "/0/private/QueryOrders",
                {"txid": "O-ONE, O-TWO"},
                "ORDER.READ",
                frozenset({"ORDERS"}),
                "1..50 unique order ids",
            ),
            (
                "/0/private/ClosedOrders",
                {"end": ""},
                "ORDER.READ",
                frozenset({"ORDERS"}),
                "canonical Unix time or provider id",
            ),
            (
                "/0/private/TradesHistory",
                {"end": "001"},
                "TRADE.READ",
                frozenset({"TRADES"}),
                "canonical integer text",
            ),
            (
                "/0/private/Ledgers",
                {"end": "not a boundary"},
                "ACCOUNT.READ",
                frozenset({"ACTIVITIES"}),
                "canonical Unix time or provider id",
            ),
            (
                "/0/private/TradesHistory",
                {"ofs": "-1"},
                "TRADE.READ",
                frozenset({"TRADES"}),
                "canonical integer text",
            ),
            (
                "/0/private/TradesHistory",
                {"limit": "101"},
                "TRADE.READ",
                frozenset({"TRADES"}),
                "outside the documented range",
            ),
            (
                "/0/private/ClosedOrders",
                {"closetime": "created"},
                "ORDER.READ",
                frozenset({"ORDERS"}),
                "outside the documented enum",
            ),
            (
                "/0/private/Ledgers",
                {"type": "mystery"},
                "ACCOUNT.READ",
                frozenset({"ACTIVITIES"}),
                "outside the documented enum",
            ),
        )
        for endpoint, query, permission, entitlements, message in cases:
            with self.subTest(endpoint=endpoint, query=query):
                events = []
                capability = verified_kraken_read_capability(
                    permission_scopes=frozenset({permission}),
                    data_entitlements=entitlements,
                )
                with TemporaryDirectory() as directory:
                    wire = RecordingWire(events)
                    transport, resolver, allocator = self.make_transport(
                        directory=directory,
                        events=events,
                        wire=wire,
                        capability=capability,
                        quota_gate=lambda *_args: events.append("quota"),
                    )
                    binding = kraken_authenticated_read_binding(
                        endpoint=endpoint,
                        query=query,
                        capability=capability,
                        permission_scope=permission,
                    )
                    with self.assertRaisesRegex(
                        ProviderTransportScopeError,
                        message,
                    ):
                        transport(binding)
                    self.assertEqual(events, [])
                    self.assertEqual(resolver.calls, [])
                    self.assertEqual(wire.requests, [])
                    self.assertEqual(
                        allocator.journal.load_events(
                            "provider_nonce",
                            allocator.aggregate_id_for_provider_api_key("key"),
                        ),
                        [],
                    )

    def test_unsupported_private_endpoint_fails_before_nonce_secret_or_wire(self):
        events = []
        with TemporaryDirectory() as directory:
            wire = RecordingWire(events)
            transport, resolver, allocator = self.make_transport(
                directory=directory,
                events=events,
                wire=wire,
            )
            binding = kraken_authenticated_read_binding(
                endpoint="/0/private/AddOrder",
            )
            with self.assertRaisesRegex(
                ProviderTransportScopeError,
                "endpoint is not explicitly allowed",
            ):
                transport(binding)
            self.assertEqual(events, [])
            self.assertEqual(resolver.calls, [])
            self.assertEqual(wire.requests, [])
            self.assertEqual(
                allocator.journal.load_events(
                    "provider_nonce",
                    allocator.aggregate_id_for_provider_api_key("key"),
                ),
                [],
            )


    def test_read_capability_supersession_after_secret_blocks_wire(self):
        events = []
        capability = verified_kraken_read_capability()
        registry = RecordingCapabilityRegistry(events)
        registry.add(capability)
        replacement = verified_kraken_read_capability(
            snapshot_id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
            snapshot_observed_at=KRAKEN_READ_NOW + timedelta(milliseconds=500),
            data_entitlements=frozenset({"TRADES"}),
        )

        def supersede():
            registry.add(replacement)

        resolver = FakeSecretResolver(
            events,
            on_resolve=supersede,
            credential_plaintext=self.credential_plaintext(),
        )
        with TemporaryDirectory() as directory:
            wire = RecordingWire(events)
            transport, resolver, allocator = self.make_transport(
                directory=directory,
                events=events,
                wire=wire,
                capability=capability,
                capability_registry=registry,
                secret_resolver=resolver,
            )
            with self.assertRaisesRegex(
                ProviderTransportScopeError,
                "no longer valid",
            ):
                transport(
                    kraken_authenticated_read_binding(
                        capability=capability,
                    )
                )

            self.assertEqual(
                events,
                ["capability", "resolve", "nonce", "capability"],
            )
            self.assertEqual(len(resolver.calls), 1)
            self.assertEqual(wire.requests, [])
            self.assertEqual(
                len(
                    allocator.journal.load_events(
                        "provider_nonce",
                        allocator.aggregate_id_for_provider_api_key("key"),
                    )
                ),
                1,
            )
    def test_trade_credential_cannot_be_reused_for_private_read(self):
        events = []
        handle = kraken_trade_handle()
        with TemporaryDirectory() as directory:
            allocator = KrakenSpotDurableNonceAllocator(
                journal=JournalStore(f"{directory}/journal.sqlite3"),
                account_id="acct-kraken",
                environment="LIVE",
                credential_handle=handle,
                clock_millis=lambda: 100,
            )
            with self.assertRaisesRegex(
                ProviderTransportScopeError,
                "READ credential handle",
            ):
                KrakenSpotAuthenticatedReadTransport(
                    policy=KRAKEN_SPOT_ENDPOINT_POLICIES["LIVE"],
                    account_id="acct-kraken",
                    capability_snapshot_id=KRAKEN_READ_SNAPSHOT_ID,
                    capability_registry=CapabilityRegistry(),
                    secret_resolver=FakeSecretResolver(events),
                    credential_handle=handle,
                    session_token="kraken-read-session",
                    origin="autotrade://execution",
                    execution_identity="host-owner",
                    nonce_allocator=allocator,
                    clock_utc=lambda: KRAKEN_READ_NOW,
                )

    def test_unexpected_private_read_http_status_never_becomes_provider_state(self):
        for status in (201, 202, 401, 429, 500):
            events = []
            with TemporaryDirectory() as directory:
                wire = RecordingWire(
                    events,
                    response=b'{"error":["EAPI:Rate limit exceeded"]}',
                    http_status=status,
                )
                transport, resolver, _allocator = self.make_transport(
                    directory=directory,
                    events=events,
                    wire=wire,
                )
                with self.subTest(status=status), self.assertRaisesRegex(
                    ProviderTransportError,
                    "unexpected HTTP status",
                ):
                    transport(kraken_authenticated_read_binding())
                self.assertEqual(
                    events,
                    ["capability", "resolve", "nonce", "capability", "wire"],
                )
                self.assertEqual(len(resolver.calls), 1)
                self.assertEqual(len(wire.requests), 1)


class KrakenFuturesSigningPrimitiveTests(unittest.TestCase):
    def sign(self, **overrides):
        arguments = dict(
            policy=KRAKEN_FUTURES_ENDPOINT_POLICIES["LIVE"],
            provider_environment="LIVE",
            endpoint="/derivatives/api/v3/sendorder",
            body={"orderType": "mkt", "symbol": "PF_XBTUSD", "side": "buy",
                  "size": "1", "cliOrdId": "client-1"},
            credential_plaintext=self.credential_plaintext(),
            nonce=1,
        )
        arguments.update(overrides)
        return KrakenFuturesSigner.sign(**arguments)

    def test_nonce_subclass_is_rejected_without_executing_numeric_callbacks(self):
        class HostileNonce(int):
            def __le__(self, other):
                raise AssertionError("untrusted nonce comparison executed")

            def __str__(self):
                raise AssertionError("untrusted nonce rendering executed")

        with self.assertRaises(ProviderTransportScopeError):
            self.sign(nonce=HostileNonce(1))

    def test_policy_subclass_cannot_supply_equality_or_destination(self):
        class HostilePolicy(ProviderEndpointPolicy):
            def __eq__(self, other):
                raise AssertionError("untrusted policy equality executed")

            def absolute_url(self, endpoint):
                raise AssertionError("untrusted destination callback executed")

        policy = HostilePolicy("KRAKEN", "LIVE", "https://futures.kraken.com",
                               frozenset({"futures.kraken.com"}))
        with self.assertRaises(ProviderTransportScopeError):
            self.sign(policy=policy)

    def test_equal_policy_instance_cannot_retarget_approved_destination(self):
        policy = ProviderEndpointPolicy("KRAKEN", "LIVE", "https://futures.kraken.com",
                                        frozenset({"futures.kraken.com"}))
        object.__setattr__(policy, "absolute_url", lambda endpoint: "https://attacker.test/order")
        request = self.sign(policy=policy)
        self.assertTrue(request.url.startswith("https://futures.kraken.com/derivatives/api/v3/sendorder?"))

    def test_unbounded_size_is_rejected_before_provider_decimal_construction(self):
        body = {"orderType": "mkt", "symbol": "PF_XBTUSD", "side": "buy",
                "size": "1e999999999", "cliOrdId": "client-1"}
        with patch("mvp.autotrade_mvp.provider_transport.Decimal",
                   side_effect=AssertionError("unbounded Decimal construction")):
            with self.assertRaises(ProviderTransportScopeError):
                self.sign(body=body)

    def test_noncanonical_base64_pad_bits_are_rejected(self):
        credential = json.dumps({"api_key": "key", "api_secret": "Zh=="})
        with self.assertRaises(ProviderTransportScopeError):
            self.sign(credential_plaintext=credential)

    @staticmethod
    def credential_plaintext():
        return json.dumps(
            {
                "api_key": "kraken-futures-key",
                "api_secret": "dGVzdC1mdXR1cmVzLXNlY3JldA==",
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    def test_signer_matches_url_encoded_derivatives_vector(self):
        request = KrakenFuturesSigner.sign(
            policy=KRAKEN_FUTURES_ENDPOINT_POLICIES["LIVE"],
            provider_environment="LIVE",
            endpoint="/derivatives/api/v3/sendorder",
            body={
                "orderType": "lmt",
                "symbol": "PF_XBTUSD",
                "side": "buy",
                "size": "1",
                "cliOrdId": "client+id",
                "limitPrice": "60000",
                "reduceOnly": "true",
            },
            credential_plaintext=self.credential_plaintext(),
            nonce=1_415_957_147_987,
        )
        exact_query = (
            "cliOrdId=client%2Bid&limitPrice=60000&orderType=lmt&"
            "reduceOnly=true&side=buy&size=1&symbol=PF_XBTUSD"
        )
        self.assertEqual(
            request.url,
            "https://futures.kraken.com/derivatives/api/v3/sendorder?"
            + exact_query,
        )
        self.assertEqual(request.body, b"")
        self.assertEqual(request.headers["APIKey"], "kraken-futures-key")
        self.assertEqual(request.headers["Nonce"], "1415957147987")
        self.assertEqual(
            request.headers["Authent"],
            "Dx7wkm8YwJNpIwhewi4P97983BAGM3iy5iLzUS1kGNPnWQUH6e21X4NjvXUj1FPNfhvEx39t8ZgzSoK4vj5K2Q==",
        )
        self.assertNotIn("Content-Type", request.headers)
        self.assertNotIn("test-futures-secret", repr(request))
        self.assertNotIn("dGVzdC1mdXR1cmVzLXNlY3JldA==", repr(request))

    def test_signer_rejects_scope_payload_nonce_and_secret_ambiguity(self):
        valid_body = {
            "orderType": "mkt",
            "symbol": "PF_XBTUSD",
            "side": "buy",
            "size": "1",
            "cliOrdId": "client-1",
        }
        with self.assertRaisesRegex(
            ProviderTransportScopeError,
            "policy does not match exact provider environment",
        ):
            KrakenFuturesSigner.sign(
                policy=KRAKEN_FUTURES_ENDPOINT_POLICIES["LIVE"],
                provider_environment="DEMO",
                endpoint="/derivatives/api/v3/sendorder",
                body=valid_body,
                credential_plaintext=self.credential_plaintext(),
                nonce=1,
            )
        for bad_nonce in (True, 0, -1, (1 << 64), "1"):
            with self.subTest(nonce=repr(bad_nonce)), self.assertRaisesRegex(
                ProviderTransportScopeError,
                "unsigned 64-bit positive integer",
            ):
                KrakenFuturesSigner.sign(
                    policy=KRAKEN_FUTURES_ENDPOINT_POLICIES["LIVE"],
                    provider_environment="LIVE",
                    endpoint="/derivatives/api/v3/sendorder",
                    body=valid_body,
                    credential_plaintext=self.credential_plaintext(),
                    nonce=bad_nonce,
                )
        with self.assertRaisesRegex(
            ProviderTransportScopeError,
            "client order id must be canonical text",
        ):
            KrakenFuturesSigner.sign(
                policy=KRAKEN_FUTURES_ENDPOINT_POLICIES["LIVE"],
                provider_environment="LIVE",
                endpoint="/derivatives/api/v3/sendorder",
                body={**valid_body, "cliOrdId": " client-1 "},
                credential_plaintext=self.credential_plaintext(),
                nonce=1,
            )
        with self.assertRaisesRegex(
            ProviderTransportScopeError,
            "canonical base64",
        ):
            KrakenFuturesSigner.sign(
                policy=KRAKEN_FUTURES_ENDPOINT_POLICIES["LIVE"],
                provider_environment="LIVE",
                endpoint="/derivatives/api/v3/sendorder",
                body=valid_body,
                credential_plaintext='{"api_key":"key","api_secret":"not-base64!"}',
                nonce=1,
            )


class SharedProviderWireResponseBudgetTests(unittest.TestCase):
    """Network-free bounded I/O tests for the selected shared wire client."""

    @staticmethod
    def request():
        return SignedHttpRequest(
            method="POST",
            url="https://api.example.test/v1/order",
            headers={"Content-Type": "application/json"},
            body=b"{}",
            timeout_seconds=2,
        )

    def test_production_client_disables_ambient_process_os_proxy_discovery(self):
        with patch("mvp.autotrade_mvp.provider_transport.build_opener") as factory:
            UrllibJsonWireClient()
            handlers = factory.call_args.args
            selected = [x for x in handlers if type(x) is ProxyHandler]
            self.assertEqual(len(selected), 1)
            self.assertEqual(selected[0].proxies, {})
        self.assertEqual(DEFAULT_MAX_PROVIDER_RESPONSE_BYTES, 8 * 1024 * 1024)

    def test_signed_write_requires_exactly_one_payload_channel(self):
        body_request = SignedHttpRequest(
            method="POST",
            url="https://api.example.test/v1/order",
            headers={"Content-Type": "application/json"},
            body=b"{}",
            timeout_seconds=2,
        )
        self.assertEqual(body_request.body, b"{}")

        query_request = SignedHttpRequest(
            method="POST",
            url="https://api.example.test/v1/order?symbol=PI_XBTUSD&size=1",
            headers={"APIKey": "synthetic"},
            body=b"",
            timeout_seconds=2,
        )
        self.assertEqual(query_request.body, b"")

        with self.assertRaisesRegex(
            ProviderTransportScopeError,
            "exactly one payload channel",
        ):
            SignedHttpRequest(
                method="POST",
                url="https://api.example.test/v1/order?symbol=PI_XBTUSD",
                headers={"Content-Type": "application/json"},
                body=b"{}",
                timeout_seconds=2,
            )
        with self.assertRaisesRegex(
            ProviderTransportScopeError,
            "exactly one payload channel",
        ):
            SignedHttpRequest(
                method="POST",
                url="https://api.example.test/v1/order",
                headers={"APIKey": "synthetic"},
                body=b"",
                timeout_seconds=2,
            )

    def test_query_only_signed_write_preserves_exact_url_and_sends_no_body(self):
        class Stream(BytesIO):
            status = 200

            def __init__(self):
                super().__init__(b'{"result":"success"}')
                self.read_sizes = []

            def read(self, size=-1):
                self.read_sizes.append(size)
                return super().read(size)

        captured = []
        stream = Stream()

        class Opener:
            def open(self, request, *, timeout):
                captured.append((request, timeout))
                return stream

        exact_url = (
            "https://api.example.test/derivatives/api/v3/sendorder?"
            "orderType=mkt&symbol=PI_XBTUSD&side=buy&size=1"
        )
        request = SignedHttpRequest(
            method="POST",
            url=exact_url,
            headers={"APIKey": "synthetic", "Authent": "synthetic-signature"},
            body=b"",
            timeout_seconds=2,
        )
        client = UrllibJsonWireClient(max_response_bytes=128)
        client._opener = Opener()

        response = client.send(request)

        self.assertEqual(len(captured), 1)
        outbound, timeout = captured[0]
        self.assertEqual(outbound.full_url, exact_url)
        self.assertEqual(outbound.get_method(), "POST")
        self.assertIsNone(outbound.data)
        self.assertEqual(timeout, 2)
        self.assertEqual(type(response), TradingWireResponse)
        self.assertEqual(response.http_status, 200)
        self.assertEqual(response.body, b'{"result":"success"}')
        self.assertEqual(stream.read_sizes, [129])

    def test_config_exacts_and_shared_nonpolymorphic_byte_limits(self):
        class NumericSubtype(int):
            pass
        for bad in (0, True, "8", NumericSubtype(8), HARD_MAX_PROVIDER_RESPONSE_BYTES + 1):
            with self.subTest(bad=repr(bad)), self.assertRaises((ValueError, TypeError)):
                UrllibJsonWireClient(max_response_bytes=bad)
        with self.assertRaisesRegex(ValueError, "byte budget"):
            require_provider_response_bytes(b"x" * 9, max_bytes=8)
        self.assertEqual(require_provider_response_bytes(b"x" * 8, max_bytes=8), b"x" * 8)
        self.assertEqual(UrllibJsonWireClient(max_response_bytes=8).max_response_bytes, 8)


    def test_mutated_response_budget_is_revalidated_before_read_and_snapshotted(self):
        class Stream:
            status = 200

            def __init__(self, client, body, mutate_to):
                self.client = client
                self.body = body
                self.mutate_to = mutate_to
                self.sizes = []

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def read(self, size=-1):
                self.sizes.append(size)
                self.client.max_response_bytes = self.mutate_to
                return self.body[:size]

        class Opener:
            def __init__(self, stream):
                self.stream = stream
                self.calls = 0

            def open(self, *_args, **_kwargs):
                self.calls += 1
                return self.stream

        client = UrllibJsonWireClient(max_response_bytes=8)
        stream = Stream(client, b"x" * 8, 8)
        opener = Opener(stream)
        client._opener = opener
        client.max_response_bytes = HARD_MAX_PROVIDER_RESPONSE_BYTES + 1
        with self.assertRaisesRegex(ProviderTransportScopeError, "byte budget"):
            client.send(self.request())
        self.assertEqual(opener.calls, 0)
        self.assertEqual(stream.sizes, [])

        # A concurrent field mutation cannot widen the captured four-byte limit.
        client = UrllibJsonWireClient(max_response_bytes=4)
        stream = Stream(client, b"x" * 5, 8)
        opener = Opener(stream)
        client._opener = opener
        with self.assertRaisesRegex(ProviderTransportError, "oversized"):
            client.send(self.request())
        self.assertEqual(opener.calls, 1)
        self.assertEqual(stream.sizes, [5])

    def test_http_error_uses_same_captured_budget_when_live_field_mutates_during_read(self):
        class MutatingBytesIO(BytesIO):
            def __init__(self, client, data, mutate_to):
                super().__init__(data)
                self.client = client
                self.mutate_to = mutate_to
                self.read_sizes = []

            def read(self, size=-1):
                self.read_sizes.append(size)
                self.client.max_response_bytes = self.mutate_to
                return super().read(size)

        class FailingOpener:
            def __init__(self, client):
                self.stream = MutatingBytesIO(client, b"x" * 5, 8)
                self.error = HTTPError(
                    "https://api.example.test/v1/order",
                    429,
                    "error",
                    {},
                    self.stream,
                )
                self.calls = 0

            def open(self, *_args, **_kwargs):
                self.calls += 1
                raise self.error

        client = UrllibJsonWireClient(max_response_bytes=4)
        opener = FailingOpener(client)
        client._opener = opener
        with self.assertRaisesRegex(ProviderTransportError, "oversized"):
            client.send(self.request())
        self.assertEqual(opener.calls, 1)
        self.assertEqual(opener.stream.read_sizes, [5])
        self.assertEqual(client.max_response_bytes, 8)

    def test_http_success_reads_at_most_limit_plus_one_and_rejects_overlimit(self):
        class Stream:
            def __init__(self, body):
                self.body = body
                self.status = 200
                self.sizes = []
            def __enter__(self):
                return self
            def __exit__(self, *_):
                return False
            def read(self, size=-1):
                self.sizes.append(size)
                return self.body[:size]
        class Opener:
            def __init__(self, stream):
                self.stream = stream
                self.calls = 0
            def open(self, *_args, **_kwargs):
                self.calls += 1
                return self.stream
        for body, ok in ((b"x" * 8, True), (b"x" * 9, False)):
            with self.subTest(ok=ok):
                client = UrllibJsonWireClient(max_response_bytes=8)
                stream = Stream(body)
                client._opener = Opener(stream)
                if ok:
                    result = client.send(self.request())
                    self.assertEqual(result.body, body)
                else:
                    with self.assertRaisesRegex(ProviderTransportError, "oversized"):
                        client.send(self.request())
                self.assertEqual(stream.sizes, [9])
                self.assertEqual(client._opener.calls, 1)

    def test_pure_json_structure_gate_prevents_parser_recursion_before_allocation(self):
        inside_quoted_string = json.dumps(
            {"message": "[" * 150 + "\\\"" + "]" * 150},
            separators=(",", ":"),
        ).encode("utf-8")
        self.assertIs(require_provider_json_depth(inside_quoted_string), inside_quoted_string)
        at_limit = b"[" * MAX_PROVIDER_JSON_DEPTH + b"0" + b"]" * MAX_PROVIDER_JSON_DEPTH
        one_over = b"[" * (MAX_PROVIDER_JSON_DEPTH + 1) + b"0" + b"]" * (MAX_PROVIDER_JSON_DEPTH + 1)
        self.assertEqual(require_provider_json_depth(at_limit), at_limit)
        # Preserve the existing provider_core._freeze_json(depth=0) geometry:
        # 65 *empty* nested containers finish at depth 64, while placing a
        # primitive inside the deepest one would recurse to forbidden depth 65.
        empty_65 = b"[" * 65 + b"]" * 65
        empty_66 = b"[" * 66 + b"]" * 66
        self.assertEqual(require_provider_json_depth(empty_65), empty_65)
        with self.assertRaisesRegex(ValueError, "structural depth"):
            require_provider_json_depth(empty_66)
        with self.assertRaisesRegex(ValueError, "structural depth"):
            require_provider_json_depth(one_over)
        with self.assertRaisesRegex(ValueError, "structural depth"):
            require_provider_json_depth(at_limit, max_depth=63)
        with self.assertRaisesRegex(ValueError, "structural depth budget is invalid"):
            require_provider_json_depth(at_limit, max_depth=True)
        with self.assertRaisesRegex(ValueError, "invalid structural nesting"):
            require_provider_json_depth(b"}")
        with self.assertRaises(ValueError):
            require_provider_json_depth(b"x" * (HARD_MAX_PROVIDER_RESPONSE_BYTES + 1))


    def test_http_error_body_uses_same_limit_and_redirect_never_reads_body(self):
        class TrackingBytesIO(BytesIO):
            def __init__(self, data):
                super().__init__(data)
                self.read_sizes = []
            def read(self, size=-1):
                self.read_sizes.append(size)
                return super().read(size)
        class FailingOpener:
            def __init__(self, status, body):
                self.stream = TrackingBytesIO(body)
                self.error = HTTPError("https://api.example.test/v1/order", status, "error", {}, self.stream)
                self.calls = 0
            def open(self, *_args, **_kwargs):
                self.calls += 1
                raise self.error
        for status, body, ok in ((429, b"x" * 8, True), (429, b"x" * 9, False), (302, b"x" * 9, False)):
            with self.subTest(status=status, ok=ok):
                client = UrllibJsonWireClient(max_response_bytes=8)
                client._opener = FailingOpener(status, body)
                if ok:
                    result = client.send(self.request())
                    self.assertEqual(result.http_status, 429)
                    self.assertEqual(result.body, body)
                else:
                    with self.assertRaises(ProviderTransportError):
                        client.send(self.request())
                self.assertEqual(client._opener.calls, 1)
                self.assertEqual(client._opener.stream.read_sizes, [] if status == 302 else [9])

    def test_injected_typed_and_raw_wire_values_cannot_exceed_absolute_ceiling(self):
        too_large = b"x" * (HARD_MAX_PROVIDER_RESPONSE_BYTES + 1)
        for response in (TradingWireResponse, AuthenticatedReadWireResponse):
            with self.subTest(contract=response.__name__), self.assertRaises(ProviderTransportError):
                response(http_status=200, body=too_large)
        with self.assertRaises(ProviderTransportError):
            _exact_trading_response(too_large)


    def test_bybit_default_urllib_typed_response_is_preserved(self):
        # Reuse the real Bybit preparation/capability/signer, replacing only
        # network I/O with deterministic in-memory HTTP status/bytes.
        from mvp.tests.test_bybit_transport import (
            BybitV5SharedTransportTests, prepared,
        )
        from mvp.autotrade_mvp.bybit_v5 import guarded_order_projection

        class BodyStream(BytesIO):
            def __init__(self, body):
                super().__init__(body)
                self.status = 200
                self.read_sizes = []
            def read(self, size=-1):
                self.read_sizes.append(size)
                return super().read(size)

        for status, body in (
            (200, b'{"retCode":0,"retMsg":"OK","result":{"orderId":"provider-1","orderLinkId":"bybit-order-1"}}'),
            (429, b'{"retCode":10006,"retMsg":"rate limit","result":{}}'),
        ):
            with self.subTest(status=status):
                events = []
                stream = BodyStream(body)
                calls = []

                class FakeOpener:
                    def open(self, request, *, timeout):
                        calls.append((request, timeout))
                        if status == 429:
                            raise HTTPError(request.full_url, status, "rate limit", {}, stream)
                        return stream

                client = UrllibJsonWireClient(max_response_bytes=256)
                client._opener = FakeOpener()
                capability, prepared_request = prepared()
                transport, resolver = BybitV5SharedTransportTests().make_transport(
                    capability=capability, events=events, wire=client,
                )
                exact = transport(
                    "bybit-order-1",
                    guarded_order_projection(prepared_request),
                    lambda: events.append("guard"),
                )
                self.assertEqual(type(exact.response_bytes), bytes)
                self.assertEqual(exact.response_bytes, body)
                self.assertEqual(exact.http_status, status)
                self.assertEqual(exact.payload["retCode"], 0 if status == 200 else 10006)
                self.assertEqual(stream.read_sizes, [257])
                self.assertEqual(len(calls), 1)
                self.assertEqual(len(resolver.calls), 1)
                self.assertEqual(events, [
                    "capability", "resolve", "capability", "guard",
                ])

    def test_typed_wire_status_rejects_int_subclasses_before_comparison_callbacks(self):
        # Exact response CLASS is not enough: a malicious subclass nested
        # in http_status can override comparisons during construction.
        callbacks = []

        class SpoofedHttpStatus(int):
            def __lt__(self, _other):
                callbacks.append("lt")
                return False

            def __gt__(self, _other):
                callbacks.append("gt")
                return False

        for response_type in (
            TradingWireResponse,
            AuthenticatedReadWireResponse,
        ):
            with self.subTest(response_type=response_type.__name__):
                with self.assertRaisesRegex(
                    ProviderTransportScopeError,
                    "HTTP status must be an integer",
                ):
                    response_type(http_status=SpoofedHttpStatus(777), body=b"{}")
                self.assertEqual(callbacks, [])
                accepted = response_type(http_status=200, body=b"{}")
                self.assertIs(type(accepted.http_status), int)

    def test_exact_trading_adapter_revalidates_nested_status_after_construction(self):
        # Even an exact frozen dataclass can be mutated by object.__setattr__.
        # Never trust a past __post_init__ decision at post-SEND evidence time.
        callbacks = []

        class MutatedStatus(int):
            def __lt__(self, _other):
                callbacks.append("lt")
                raise AssertionError("hostile comparator executed")

            def __gt__(self, _other):
                callbacks.append("gt")
                raise AssertionError("hostile comparator executed")

        typed = TradingWireResponse(http_status=200, body=b"{}")
        object.__setattr__(typed, "http_status", MutatedStatus(777))
        with self.assertRaisesRegex(
            ProviderTransportError, "invalid trading HTTP response status",
        ):
            _exact_trading_response(typed)
        self.assertEqual(callbacks, [])

        for bad in (True, 99, 600, "200"):
            with self.subTest(bad=repr(bad)):
                object.__setattr__(typed, "http_status", bad)
                with self.assertRaisesRegex(
                    ProviderTransportError, "invalid trading HTTP response status",
                ):
                    _exact_trading_response(typed)

        object.__setattr__(typed, "http_status", 503)
        legitimate = _exact_trading_response(typed)
        self.assertEqual(legitimate.http_status, 503)
        self.assertEqual(legitimate.response_bytes, b"{}")

    def test_trading_wire_subclass_cannot_impersonate_post_send_exact_status(self):
        callbacks = []
        class HostileTradingWireResponse(TradingWireResponse):
            def __getattribute__(self, name):
                if name in ("body", "http_status"):
                    callbacks.append(name)
                    raise AssertionError("untrusted virtual getter ran after send")
                return object.__getattribute__(self, name)

        forged = object.__new__(HostileTradingWireResponse)
        with self.assertRaisesRegex(ProviderTransportError, "unsupported response contract"):
            _exact_trading_response(forged)
        self.assertEqual(callbacks, [])

    def test_signed_http_error_context_is_detached_after_redacted_outcome(self):
        secret = "SYNTHETIC_SIGNED_QUERY_OR_KEY_NO_LOGGING"
        sensitive_url = "https://api.example.test/read?signature=" + secret
        signed_read = AuthenticatedReadHttpRequest(
            url=sensitive_url,
            headers={"X-API-KEY": secret},
            timeout_seconds=2,
        )

        class SensitiveStream(BytesIO):
            def __init__(self, body, *, fail=False):
                super().__init__(body)
                self.fail = fail
                self.read_sizes = []
            def read(self, size=-1):
                self.read_sizes.append(size)
                if self.fail:
                    raise OSError(secret)
                return super().read(size)

        for status, body, read_fail, expected, size_calls in (
            (302, b"redirect-not-read", False, "redirect is prohibited", []),
            (429, b"x" * 9, False, "invalid or oversized", [9]),
            (500, b"x" * 3, True, "error body unavailable", [9]),
        ):
            with self.subTest(status=status):
                client = UrllibJsonWireClient(max_response_bytes=8)
                stream = SensitiveStream(body, fail=read_fail)
                source_error = HTTPError(
                    sensitive_url, status, "SECRET-BEARING HTTP ERROR",
                    {"X-API-KEY": secret}, stream,
                )
                class FailingOpener:
                    def open(self, *_args, **_kwargs):
                        raise source_error
                client._opener = FailingOpener()
                with self.assertRaisesRegex(
                    ProviderTransportError, expected,
                ) as caught:
                    client.send(signed_read)
                self.assertEqual(stream.read_sizes, size_calls)
                graph = [caught.exception]
                visited = set()
                while graph:
                    current = graph.pop()
                    if id(current) in visited:
                        continue
                    visited.add(id(current))
                    self.assertNotIsInstance(current, HTTPError)
                    self.assertNotIn(secret, str(current))
                    self.assertNotIn(secret, repr(current))
                    if current.__cause__ is not None:
                        graph.append(current.__cause__)
                    if current.__context__ is not None:
                        graph.append(current.__context__)

        class BrokenDnsOpener:
            def open(self, *_args, **_kwargs):
                raise URLError(secret)
        client = UrllibJsonWireClient(max_response_bytes=8)
        client._opener = BrokenDnsOpener()
        with self.assertRaisesRegex(
            ProviderTransportError, "transport response unavailable"
        ) as caught:
            client.send(signed_read)
        self.assertIsNone(caught.exception.__cause__)
        self.assertIsNone(caught.exception.__context__)
        self.assertNotIn(secret, repr(caught.exception))

    def test_http_error_hostile_status_cannot_dispatch_comparison_or_leak_context(self):
        observed = []
        secret = "SYNTHETIC_HOSTILE_HTTP_STATUS_SECRET"
        class HostileCode(int):
            def __int__(self):
                observed.append("int")
                raise AssertionError(secret)
            def __le__(self, _):
                observed.append("le")
                raise AssertionError(secret)
        for status in (True, "503", HostileCode(503)):
            with self.subTest(status=repr(status)):
                client = UrllibJsonWireClient(max_response_bytes=8)
                error = HTTPError(
                    "https://api.example.test/?signature=" + secret,
                    status, "synthetic", {}, BytesIO(b"ok"),
                )
                class FailingOpener:
                    def open(self, *_args, **_kwargs):
                        raise error
                client._opener = FailingOpener()
                with self.assertRaisesRegex(
                    ProviderTransportError, "error status invalid",
                ) as caught:
                    client.send(self.request())
                self.assertIsNone(caught.exception.__cause__)
                self.assertIsNone(caught.exception.__context__)
                self.assertNotIn(secret, repr(caught.exception))
        self.assertEqual(observed, [])

    def test_authenticated_http_error_keeps_exact_status_and_budget(self):
        class Body(BytesIO):
            def __init__(self):
                super().__init__(b"12345678")
                self.calls = []
            def read(self, size=-1):
                self.calls.append(size)
                return super().read(size)
        client = UrllibJsonWireClient(max_response_bytes=8)
        body = Body()
        class FailingOpener:
            def open(self, request, *_args, **_kwargs):
                raise HTTPError(request.full_url, 429, "rate limit", {}, body)
        client._opener = FailingOpener()
        exact = client.send(AuthenticatedReadHttpRequest(
            url="https://api.example.test/read?signature=fake",
            headers={"X-API-KEY": "synthetic"}, timeout_seconds=2,
        ))
        self.assertIs(type(exact), AuthenticatedReadWireResponse)
        self.assertEqual(exact.http_status, 429)
        self.assertEqual(exact.body, b"12345678")
        self.assertEqual(body.calls, [9])


if __name__ == "__main__":
    unittest.main()
