from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import unittest

from mvp.autotrade_mvp.alpaca import parse_trade_activities
from mvp.autotrade_mvp.capabilities import (
    CapabilityClaim,
    CapabilityRegistry,
    EvidenceVerification,
    derive_capability_snapshot,
)
from mvp.autotrade_mvp.provider_core import (
    Surface,
    prepare_authenticated_read_query,
)
from mvp.autotrade_mvp.provider_transport import (
    ALPACA_ENDPOINT_POLICIES,
    AlpacaAuthenticatedReadSigner,
    AlpacaAuthenticatedReadTransport,
    AuthenticatedReadHttpRequest,
    AuthenticatedReadWireResponse,
    ProviderTransportError,
    ProviderTransportScopeError,
)
from mvp.autotrade_mvp.windows_secrets import PersistentCredentialHandle


NOW = datetime(2026, 10, 1, 1, 0, tzinfo=timezone.utc)
SNAPSHOT_ID = "aaaaaaaa-1111-4111-8111-111111111111"
ARTIFACT_IDS = {
    "DOCUMENTED": "11111111-1111-4111-8111-111111111111",
    "API": "22222222-2222-4222-8222-222222222222",
    "ACCOUNT": "33333333-3333-4333-8333-333333333333",
    "INSTRUMENT": "44444444-4444-4444-8444-444444444444",
}


class RecordingCapabilityRegistry(CapabilityRegistry):
    def __init__(self, events):
        super().__init__()
        self.events = events

    def require_verified(self, **kwargs):
        self.events.append("capability")
        return super().require_verified(**kwargs)


class FakeSecretResolver:
    def __init__(self, events, *, on_resolve=None):
        self.events = events
        self.calls = []
        self.on_resolve = on_resolve

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
        return json.dumps(
            {
                "api_key": "alpaca-key-SECRET",
                "api_secret": "alpaca-secret-SECRET",
            },
            sort_keys=True,
            separators=(",", ":"),
        )


class RecordingWire:
    def __init__(
        self,
        events,
        *,
        body=b'[]',
        http_status=200,
    ):
        self.events = events
        self.body = body
        self.http_status = http_status
        self.requests = []

    def send(self, request):
        self.events.append("wire")
        self.requests.append(request)
        if not isinstance(request, AuthenticatedReadHttpRequest):
            raise AssertionError("Alpaca read transport emitted wrong request type")
        return AuthenticatedReadWireResponse(
            http_status=self.http_status,
            body=self.body,
        )


def read_handle(*, environment="PAPER", account_id="acct-alpaca"):
    return PersistentCredentialHandle(
        handle_id="cred-alpaca-read",
        account_id=account_id,
        provider="ALPACA",
        environment=environment,
        purpose="READ",
        generation=1,
    )


def trade_handle():
    return PersistentCredentialHandle(
        handle_id="cred-alpaca-trade",
        account_id="acct-alpaca",
        provider="ALPACA",
        environment="PAPER",
        purpose="TRADE",
        generation=1,
    )


def verified_capability(
    *,
    snapshot_id=SNAPSHOT_ID,
    snapshot_observed_at=NOW,
    permission_scopes=frozenset({"TRADE.READ"}),
    data_entitlements=frozenset({"TRADES"}),
):
    claim_observed = snapshot_observed_at - timedelta(minutes=1)
    expires = snapshot_observed_at + timedelta(minutes=10)
    claims = tuple(
        CapabilityClaim(
            source=source,
            provider_id="ALPACA",
            account_id="acct-alpaca",
            entity_id="entity-alpaca",
            environment="PAPER",
            provider_environment="PAPER",
            instrument_version="AAPL@1",
            observed_at=claim_observed,
            expires_at=expires,
            supported_order_types=frozenset({"LIMIT"}),
            time_in_force=frozenset({"DAY"}),
            permission_scopes=permission_scopes,
            position_mode="NET",
            native_protection=frozenset(),
            rate_limit_policy_id="alpaca-paper-v1",
            data_entitlements=data_entitlements,
            evidence_ref={
                "artifact_id": ARTIFACT_IDS[source],
                "sha256": "sha256:" + "a" * 64,
                "observed_at": claim_observed.isoformat().replace("+00:00", "Z"),
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


def binding(
    *,
    capability=None,
    query=None,
    permission_scope="TRADE.READ",
    surface=Surface.ACTIVITIES,
):
    cap = capability or verified_capability()
    return prepare_authenticated_read_query(
        capability=cap,
        surface=surface,
        endpoint="/v2/account/activities/FILL",
        query=(
            {"direction": "asc", "page_size": "100"}
            if query is None
            else query
        ),
        at=NOW,
        permission_scope=permission_scope,
        provider_environment="PAPER",
    )


class AlpacaAuthenticatedReadTransportTests(unittest.TestCase):
    def make_transport(
        self,
        *,
        events,
        capability=None,
        registry=None,
        resolver=None,
        wire=None,
        clock_utc=None,
        quota_gate=None,
    ):
        cap = capability or verified_capability()
        if registry is None:
            registry = RecordingCapabilityRegistry(events)
            registry.add(cap)
        resolver = resolver or FakeSecretResolver(events)
        wire = wire or RecordingWire(events)
        transport = AlpacaAuthenticatedReadTransport(
            policy=ALPACA_ENDPOINT_POLICIES["PAPER"],
            account_id="acct-alpaca",
            capability_snapshot_id=cap.snapshot_id,
            capability_registry=registry,
            secret_resolver=resolver,
            credential_handle=read_handle(),
            session_token="alpaca-read-session",
            origin="autotrade://execution",
            execution_identity="host-owner",
            clock_utc=clock_utc or (lambda: NOW + timedelta(seconds=1)),
            quota_gate=quota_gate,
            wire_client=wire,
        )
        return transport, resolver, wire

    def test_signer_emits_exact_sorted_query_and_documented_headers(self):
        request = AlpacaAuthenticatedReadSigner.sign(
            policy=ALPACA_ENDPOINT_POLICIES["PAPER"],
            query_binding=binding(
                query={
                    "direction": "asc",
                    "page_size": "100",
                    "page_token": "20260925::aaaaaaaa-1111-4111-8111-111111111111",
                }
            ),
            credential_plaintext=(
                '{"api_key":"key-id","api_secret":"secret-value"}'
            ),
        )
        self.assertEqual(request.method, "GET")
        self.assertEqual(
            request.url,
            "https://paper-api.alpaca.markets/v2/account/activities/FILL?"
            "direction=asc&page_size=100&"
            "page_token=20260925%3A%3Aaaaaaaaa-1111-4111-8111-111111111111",
        )
        self.assertEqual(request.body, b"")
        self.assertEqual(request.headers["APCA-API-KEY-ID"], "key-id")
        self.assertEqual(
            request.headers["APCA-API-SECRET-KEY"],
            "secret-value",
        )

    def test_one_shot_read_rechecks_current_capability_before_secret_and_wire(self):
        events = []
        wire = RecordingWire(
            events,
            body=b'[{"activity_type":"FILL","id":"activity-1"}]',
        )

        def quota(provider, account, environment, purpose):
            events.append("quota")
            self.assertEqual(
                (provider, account, environment, purpose),
                ("ALPACA", "acct-alpaca", "PAPER", "AUTHENTICATED_READ"),
            )

        transport, resolver, _ = self.make_transport(
            events=events,
            wire=wire,
            quota_gate=quota,
        )
        observation = transport(binding())

        self.assertEqual(
            events,
            ["quota", "capability", "resolve", "capability", "wire"],
        )
        self.assertEqual(len(resolver.calls), 1)
        self.assertEqual(resolver.calls[0]["purpose"], "READ")
        self.assertEqual(len(wire.requests), 1)
        request = wire.requests[0]
        self.assertEqual(request.method, "GET")
        self.assertTrue(
            request.url.startswith(
                "https://paper-api.alpaca.markets/"
                "v2/account/activities/FILL?"
            )
        )
        self.assertEqual(observation.provider_id, "ALPACA")
        self.assertEqual(observation.account_id, "acct-alpaca")
        self.assertEqual(observation.environment, "PAPER")
        self.assertEqual(observation.provider_environment, "PAPER")
        self.assertEqual(observation.query_binding, binding())
        self.assertNotIn("SECRET", observation.evidence_ref)

    def test_fill_activity_read_flows_into_existing_alpaca_parser(self):
        events = []
        order_id = "55555555-5555-4555-8555-555555555555"
        body = (
            b'[{"activity_type":"FILL","id":"activity-1",'
            b'"order_id":"55555555-5555-4555-8555-555555555555",'
            b'"symbol":"AAPL","side":"buy","qty":"2.0000",'
            b'"price":"201.2500",'
            b'"transaction_time":"2026-10-01T01:00:00Z"}]'
        )
        transport, _resolver, _wire = self.make_transport(
            events=events,
            wire=RecordingWire(events, body=body),
        )
        observation = transport(binding())
        fills = parse_trade_activities(
            observation,
            instrument_versions={"AAPL": "AAPL@1"},
            client_ids_by_order_id={order_id: "at-alpaca-fill-1"},
            fees_by_activity_id={"activity-1": (Decimal("0.01"), "USD")},
        )
        self.assertEqual(len(fills), 1)
        fill = fills[0]
        self.assertEqual(fill.quantity, Decimal("2.0000"))
        self.assertEqual(fill.price, Decimal("201.2500"))
        self.assertEqual(fill.fee_amount, Decimal("0.01"))
        self.assertEqual(fill.evidence_refs, (observation.evidence_ref,))

    def test_route_and_query_contract_fail_before_secret_or_wire(self):
        invalid_queries = (
            ({}, "page_size"),
            ({"page_size": "0"}, "page_size"),
            ({"page_size": "101"}, "page_size"),
            ({"page_size": "01"}, "page_size"),
            ({"page_size": "+1"}, "page_size"),
            ({"page_size": "100", "direction": "newest"}, "direction"),
            ({"page_size": "100", "order_id": "not-a-uuid"}, "order_id"),
            ({"page_size": "100", "after": "2026-10-01"}, "after"),
            ({"page_size": "100", "date": "2026-02-30"}, "date"),
            ({"page_size": "100", "page_token": "bad token"}, "page_token"),
            ({"page_size": "100", "unknown": "x"}, "unsupported fields"),
        )
        for query, message in invalid_queries:
            with self.subTest(query=query):
                events = []
                transport, resolver, wire = self.make_transport(events=events)
                with self.assertRaisesRegex(
                    ProviderTransportScopeError,
                    message,
                ):
                    transport(binding(query=query))
                self.assertEqual(events, [])
                self.assertEqual(resolver.calls, [])
                self.assertEqual(wire.requests, [])

        for kwargs, message in (
            ({"surface": Surface.AUTHENTICATED_READ}, "surface"),
            ({"permission_scope": "ORDER.READ"}, "permission scope"),
        ):
            with self.subTest(kwargs=kwargs):
                events = []
                cap = verified_capability(
                    permission_scopes=frozenset(
                        {"TRADE.READ", "ORDER.READ"}
                    )
                )
                transport, resolver, wire = self.make_transport(
                    events=events,
                    capability=cap,
                )
                with self.assertRaisesRegex(
                    ProviderTransportScopeError,
                    message,
                ):
                    transport(binding(capability=cap, **kwargs))
                self.assertEqual(events, [])
                self.assertEqual(resolver.calls, [])
                self.assertEqual(wire.requests, [])

    def test_trade_credential_cannot_be_reused_for_read(self):
        events = []
        cap = verified_capability()
        registry = CapabilityRegistry()
        registry.add(cap)
        with self.assertRaisesRegex(
            ProviderTransportScopeError,
            "READ credential handle",
        ):
            AlpacaAuthenticatedReadTransport(
                policy=ALPACA_ENDPOINT_POLICIES["PAPER"],
                account_id="acct-alpaca",
                capability_snapshot_id=cap.snapshot_id,
                capability_registry=registry,
                secret_resolver=FakeSecretResolver(events),
                credential_handle=trade_handle(),
                session_token="alpaca-read-session",
                origin="autotrade://execution",
                execution_identity="host-owner",
                clock_utc=lambda: NOW,
                wire_client=RecordingWire(events),
            )

    def test_expired_current_capability_after_quota_blocks_before_secret_or_wire(self):
        events = []

        def quota(*_args):
            events.append("quota")

        transport, resolver, wire = self.make_transport(
            events=events,
            quota_gate=quota,
            clock_utc=lambda: NOW + timedelta(minutes=11),
        )
        with self.assertRaisesRegex(
            ProviderTransportScopeError,
            "cannot be verified",
        ):
            transport(binding())
        self.assertEqual(events, ["quota", "capability"])
        self.assertEqual(resolver.calls, [])
        self.assertEqual(wire.requests, [])

    def test_newer_capability_after_secret_blocks_wire(self):
        events = []
        initial = verified_capability()
        registry = RecordingCapabilityRegistry(events)
        registry.add(initial)
        replacement = verified_capability(
            snapshot_id="bbbbbbbb-2222-4222-8222-222222222222",
            snapshot_observed_at=NOW + timedelta(milliseconds=500),
        )

        resolver = FakeSecretResolver(
            events,
            on_resolve=lambda: registry.add(replacement),
        )
        transport, resolver, wire = self.make_transport(
            events=events,
            capability=initial,
            registry=registry,
            resolver=resolver,
            clock_utc=lambda: NOW + timedelta(seconds=1),
        )

        with self.assertRaisesRegex(
            ProviderTransportScopeError,
            "no longer valid",
        ):
            transport(binding(capability=initial))
        self.assertEqual(events, ["capability", "resolve", "capability"])
        self.assertEqual(len(resolver.calls), 1)
        self.assertEqual(wire.requests, [])

    def test_unexpected_http_status_never_becomes_provider_observation(self):
        for status in (201, 202, 401, 429, 500):
            with self.subTest(status=status):
                events = []
                transport, resolver, wire = self.make_transport(
                    events=events,
                    wire=RecordingWire(
                        events,
                        body=b'{"message":"not accepted"}',
                        http_status=status,
                    ),
                )
                with self.assertRaisesRegex(
                    ProviderTransportError,
                    "unexpected HTTP status",
                ):
                    transport(binding())
                self.assertEqual(
                    events,
                    ["capability", "resolve", "capability", "wire"],
                )
                self.assertEqual(len(resolver.calls), 1)
                self.assertEqual(len(wire.requests), 1)


if __name__ == "__main__":
    unittest.main()
