from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from threading import Event
from tempfile import TemporaryDirectory
import unittest

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
from mvp.autotrade_mvp.dispatch import (
    GuardedDispatcher,
    load_submission_response_binding,
    stable_client_order_id,
)
from mvp.autotrade_mvp.persistence import JournalStore
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
    KRAKEN_SPOT_ENDPOINT_POLICIES,
    KrakenSpotDurableNonceAllocator,
    KrakenSpotHttpTransport,
    KrakenSpotSigner,
    WHITEBIT_ENDPOINT_POLICIES,
    WhiteBitDurableNonceAllocator,
    WhiteBitHttpTransport,
)
from mvp.autotrade_mvp.whitebit import WhiteBitPreparedRequest
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


def whitebit_trade_handle(*, account_id="acct-wb"):
    return PersistentCredentialHandle(
        handle_id="cred-whitebit-trade",
        account_id=account_id,
        provider="WHITEBIT",
        environment="LIVE",
        purpose="TRADE",
        generation=1,
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
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            first = WhiteBitDurableNonceAllocator(
                journal=JournalStore(path),
                account_id="acct-wb",
                environment="LIVE",
                clock_millis=lambda: 1_700_000_000_000,
                clock_utc=lambda: fixed,
            )
            self.assertEqual(first.allocate(), 1_700_000_000_000)

            reopened = WhiteBitDurableNonceAllocator(
                journal=JournalStore(path),
                account_id="acct-wb",
                environment="LIVE",
                clock_millis=lambda: 1_699_999_999_000,
                clock_utc=lambda: fixed + timedelta(seconds=1),
            )
            self.assertEqual(reopened.allocate(), 1_700_000_000_001)
            events = JournalStore(path).load_events(
                "provider_nonce",
                reopened.aggregate_id,
            )
            self.assertEqual(
                [item["payload"]["nonce"] for item in events],
                [1_700_000_000_000, 1_700_000_000_001],
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
                ["quota", "nonce", "resolve", "guard", "wire"],
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
                allocator.aggregate_id,
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
                    allocator.aggregate_id,
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
                wire_client=wire,
            )
            dispatcher = GuardedDispatcher(
                store,
                environment="LIVE",
                account_id="acct-wb",
                owner_token="owner-wb",
            )
            intent_id = "intent-whitebit-e2e"
            client_id = stable_client_order_id(
                "WHITEBIT",
                intent_id,
                environment="LIVE",
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
                sender_check=lambda _owner, _epoch: None,
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
                sender_check=lambda _owner, _epoch: None,
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
                allocator.aggregate_id,
            )
            self.assertEqual(len(nonce_events), 1)


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
            self.assertEqual(first.allocate(), 1_700_000_000_000)

            reopened = KrakenSpotDurableNonceAllocator(
                journal=JournalStore(path),
                account_id="acct-kraken",
                environment="LIVE",
                credential_handle=handle,
                clock_millis=lambda: 1_699_999_999_000,
                clock_utc=lambda: fixed + timedelta(seconds=1),
            )
            self.assertEqual(reopened.allocate(), 1_700_000_000_001)
            events = JournalStore(path).load_events(
                "provider_nonce",
                reopened.aggregate_id,
            )
            self.assertEqual(
                [item["payload"]["provider_id"] for item in events],
                ["KRAKEN", "KRAKEN"],
            )
            self.assertEqual(
                [item["payload"]["nonce"] for item in events],
                [1_700_000_000_000, 1_700_000_000_001],
            )
            self.assertEqual(
                [item["payload"]["credential_handle_id"] for item in events],
                ["cred-kraken-trade", "cred-kraken-trade"],
            )
            self.assertEqual(
                [item["payload"]["credential_generation"] for item in events],
                [1, 1],
            )

    def test_nonce_authority_is_independent_per_handle_and_generation(self):
        fixed = datetime(2026, 9, 26, 0, 0, tzinfo=timezone.utc)
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            handle_a = kraken_trade_handle(handle_id="kraken-a", generation=1)
            handle_b = kraken_trade_handle(handle_id="kraken-b", generation=1)
            handle_a2 = kraken_trade_handle(handle_id="kraken-a", generation=2)

            first_a = KrakenSpotDurableNonceAllocator(
                journal=store,
                account_id="acct-kraken",
                environment="LIVE",
                credential_handle=handle_a,
                clock_millis=lambda: 100,
                clock_utc=lambda: fixed,
            )
            first_b = KrakenSpotDurableNonceAllocator(
                journal=store,
                account_id="acct-kraken",
                environment="LIVE",
                credential_handle=handle_b,
                clock_millis=lambda: 100,
                clock_utc=lambda: fixed,
            )
            rotated_a = KrakenSpotDurableNonceAllocator(
                journal=store,
                account_id="acct-kraken",
                environment="LIVE",
                credential_handle=handle_a2,
                clock_millis=lambda: 100,
                clock_utc=lambda: fixed,
            )
            self.assertEqual(first_a.allocate(), 100)
            self.assertEqual(first_b.allocate(), 100)
            self.assertEqual(rotated_a.allocate(), 100)
            self.assertEqual(
                len({first_a.aggregate_id, first_b.aggregate_id, rotated_a.aggregate_id}),
                3,
            )

            restarted_a = KrakenSpotDurableNonceAllocator(
                journal=store,
                account_id="acct-kraken",
                environment="LIVE",
                credential_handle=handle_a,
                clock_millis=lambda: 50,
                clock_utc=lambda: fixed + timedelta(seconds=1),
            )
            self.assertEqual(restarted_a.allocate(), 101)

    def test_nonce_uint64_boundary_fails_closed_before_persist_or_sign(self):
        maximum = (1 << 64) - 1
        fixed = datetime(2026, 9, 26, 0, 0, tzinfo=timezone.utc)
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
            self.assertEqual(allocator.allocate(), maximum)
            reopened = KrakenSpotDurableNonceAllocator(
                journal=store,
                account_id="acct-kraken",
                environment="LIVE",
                credential_handle=handle,
                clock_millis=lambda: maximum,
                clock_utc=lambda: fixed + timedelta(seconds=1),
            )
            with self.assertRaisesRegex(
                ProviderTransportScopeError,
                "exhausted unsigned 64-bit",
            ):
                reopened.allocate()
            self.assertEqual(
                len(store.load_events("provider_nonce", reopened.aggregate_id)),
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

    def test_transport_orders_quota_nonce_secret_guard_and_one_wire_send(self):
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
                ["quota", "nonce", "resolve", "guard", "wire"],
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

    def test_nonce_send_lock_identity_is_stable_across_allocator_instances(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            handle = kraken_trade_handle()
            first = KrakenSpotDurableNonceAllocator(
                journal=JournalStore(path),
                account_id="acct-kraken",
                environment="LIVE",
                credential_handle=handle,
                clock_millis=lambda: 100,
            )
            second = KrakenSpotDurableNonceAllocator(
                journal=JournalStore(path),
                account_id="acct-kraken",
                environment="LIVE",
                credential_handle=handle,
                clock_millis=lambda: 100,
            )
            rotated = KrakenSpotDurableNonceAllocator(
                journal=JournalStore(path),
                account_id="acct-kraken",
                environment="LIVE",
                credential_handle=PersistentCredentialHandle(
                    handle_id=handle.handle_id,
                    account_id=handle.account_id,
                    provider=handle.provider,
                    environment=handle.environment,
                    purpose=handle.purpose,
                    generation=2,
                ),
                clock_millis=lambda: 100,
            )
            self.assertEqual(first._send_lock_path, second._send_lock_path)
            self.assertNotEqual(first._send_lock_path, rotated._send_lock_path)

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
                environment="LIVE",
                account_id="acct-kraken",
            )
            intent_id = "kraken-live-deadline-intent"
            client_id = stable_client_order_id(
                "KRAKEN",
                intent_id,
                environment="LIVE",
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
                environment="LIVE",
                account_id="acct-kraken",
                attempt_id=kwargs["attempt_id"],
            )
            self.assertEqual(binding.response_bytes, raw)
            self.assertEqual(
                binding.payload,
                {"error": ["EService:Deadline elapsed"], "result": None},
            )
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
                        allocator.aggregate_id,
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
                environment="LIVE",
                account_id="acct-kraken",
            )
            intent_id = "kraken-live-intent-1"
            client_id = stable_client_order_id(
                "KRAKEN",
                intent_id,
                environment="LIVE",
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
                authority_check=lambda _provider, _environment: (True, "allowed"),
                transport_send=transport,
                client_id_max_length=36,
                client_id_format="UUID",
                final_barrier_clock=lambda: now,
            )

            self.assertEqual(outcome.status, "UNKNOWN")
            self.assertEqual(outcome.reason, "transport_result_ambiguous")
            self.assertEqual(events, ["nonce", "resolve", "wire"])
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
                environment="PAPER",
                account_id="acct-1",
                owner_token="owner-1",
            )
            intent_id = "intent-http-reject-1"
            client_id = stable_client_order_id(
                "BINANCE",
                intent_id,
                environment="PAPER",
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
                sender_check=lambda _owner, _epoch: None,
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
                environment="PAPER",
                account_id="acct-1",
                attempt_id="attempt-http-reject-1",
            )
            self.assertEqual(binding.http_status, 400)
            self.assertEqual(binding.response_bytes, body)

            restarted = JournalStore(f"{directory}/journal.sqlite3")
            recovered = load_submission_response_binding(
                restarted,
                environment="PAPER",
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
                environment="PAPER",
                account_id="acct-1",
                owner_token="owner-1",
            )
            intent_id = "intent-transport-1"
            client_id = stable_client_order_id(
                "BINANCE",
                intent_id,
                environment="PAPER",
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
                sender_check=lambda _owner, _epoch: None,
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
                sender_check=lambda _owner, _epoch: None,
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
                environment="PAPER",
                account_id="acct-1",
                owner_token="owner-1",
            )
            intent_id = "intent-quota-1"
            client_id = stable_client_order_id(
                "BINANCE",
                intent_id,
                environment="PAPER",
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
                sender_check=lambda _owner, _epoch: None,
            )
            self.assertEqual(result.status, "BLOCKED")
            self.assertEqual(result.reason, "transport_failed_before_send")
            self.assertEqual(events, ["quota"])
            self.assertEqual(resolver.calls, [])
            self.assertEqual(wire.requests, [])


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


if __name__ == "__main__":
    unittest.main()
