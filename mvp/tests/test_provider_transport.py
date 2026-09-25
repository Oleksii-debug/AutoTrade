import json
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.dispatch import GuardedDispatcher, stable_client_order_id
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.provider_transport import (
    BINANCE_SPOT_ENDPOINT_POLICIES,
    BinanceSpotHttpTransport,
    BinanceSpotSigner,
    ProviderEndpointPolicy,
    ProviderTransportScopeError,
)
from mvp.autotrade_mvp.windows_secrets import PersistentCredentialHandle


class FakeSecretResolver:
    def __init__(self, events):
        self.events = events
        self.calls = []

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
        return json.dumps(
            {"api_key": "api-key-SECRET", "api_secret": "signing-SECRET"},
            sort_keys=True,
            separators=(",", ":"),
        )


class RecordingWire:
    def __init__(self, events, *, response=b'{"ok":true}', error=None):
        self.events = events
        self.response = response
        self.error = error
        self.requests = []

    def send(self, request):
        self.events.append("wire")
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return self.response


def trade_handle(*, environment="PAPER", account_id="acct-1"):
    return PersistentCredentialHandle(
        handle_id="cred-binance-trade",
        account_id=account_id,
        provider="BINANCE",
        environment=environment,
        purpose="TRADE",
        generation=1,
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


if __name__ == "__main__":
    unittest.main()
