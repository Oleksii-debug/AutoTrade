from datetime import timezone
import json
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.bybit_v5 import (
    guarded_order_projection,
    prepare_order_submission,
)
from mvp.autotrade_mvp.dispatch import GuardedDispatcher, stable_client_order_id
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.provider_transport import (
    BYBIT_V5_ENDPOINT_POLICIES,
    BybitV5HttpTransport,
    BybitV5Signer,
    ProviderTransportScopeError,
)
from mvp.autotrade_mvp.windows_secrets import PersistentCredentialHandle
from mvp.tests.test_bybit_v5 import READ_AT, write_capability
from mvp.tests.test_provider_transport import FakeSecretResolver, RecordingWire


def trade_handle(*, environment="PAPER", account_id="bybit-account"):
    return PersistentCredentialHandle(
        handle_id="cred-bybit-trade",
        account_id=account_id,
        provider="BYBIT",
        environment=environment,
        purpose="TRADE",
        generation=1,
    )


def prepared(client_order_id="bybit-order-1"):
    capability = write_capability(
        family="LINEAR_DERIVATIVES",
        position_mode="HEDGE",
        account_id="bybit-account",
        environment="PAPER",
        instrument_version="BTCUSDT@1",
    )
    request = prepare_order_submission(
        capability=capability,
        at=READ_AT,
        provider_environment="TESTNET",
        product_family="LINEAR_DERIVATIVES",
        symbol="BTCUSDT",
        side="BUY",
        order_type="LIMIT",
        quantity="0.001",
        client_order_id=client_order_id,
        time_in_force="GTC",
        price="50000",
        reduce_only=False,
        position_idx=1,
    )
    return capability, request


class BybitV5SharedTransportTests(unittest.TestCase):
    def make_transport(
        self,
        *,
        capability_snapshot_id,
        events,
        wire=None,
        quota_gate=None,
        provider_environment="TESTNET",
        policy=None,
    ):
        resolver = FakeSecretResolver(events)
        transport = BybitV5HttpTransport(
            policy=(
                BYBIT_V5_ENDPOINT_POLICIES[provider_environment]
                if policy is None
                else policy
            ),
            provider_environment=provider_environment,
            account_id="bybit-account",
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

    def test_bybit_signer_has_fixed_exact_vector(self):
        signed = BybitV5Signer.sign(
            policy=BYBIT_V5_ENDPOINT_POLICIES["TESTNET"],
            endpoint="/v5/order/create",
            body={
                "category": "linear",
                "symbol": "BTCUSDT",
                "side": "Buy",
                "orderType": "Limit",
                "qty": "0.001",
                "timeInForce": "GTC",
                "orderLinkId": "bybit-order-1",
                "reduceOnly": False,
                "positionIdx": 1,
                "price": "50000",
            },
            credential_plaintext=json.dumps(
                {
                    "api_key": "api-key-SECRET",
                    "api_secret": "signing-SECRET",
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            timestamp_ms=1700000000000,
            recv_window_ms=5000,
        )
        self.assertEqual(
            signed.url,
            "https://api-testnet.bybit.com/v5/order/create",
        )
        self.assertEqual(
            signed.body,
            (
                b'{"category":"linear","orderLinkId":"bybit-order-1",'
                b'"orderType":"Limit","positionIdx":1,"price":"50000",'
                b'"qty":"0.001","reduceOnly":false,"side":"Buy",'
                b'"symbol":"BTCUSDT","timeInForce":"GTC"}'
            ),
        )
        self.assertEqual(
            signed.headers["X-BAPI-SIGN"],
            "7f1226046b432e0743cfe648e5d403e357895c7eec8d7d922a61153a6e243e8c",
        )
        self.assertEqual(signed.headers["X-BAPI-TIMESTAMP"], "1700000000000")
        self.assertEqual(signed.headers["X-BAPI-RECV-WINDOW"], "5000")

    def test_testnet_transport_binds_scope_quota_guard_and_one_send(self):
        capability, request = prepared()
        events = []
        wire = RecordingWire(
            events,
            response=b'{"retCode":0,"retMsg":"OK","result":{"orderId":"provider-1","orderLinkId":"bybit-order-1"}}',
        )

        def quota(provider, account, environment, purpose):
            events.append("quota")
            self.assertEqual(
                (provider, account, environment, purpose),
                ("BYBIT", "bybit-account", "PAPER", "ORDER_WRITE"),
            )

        transport, resolver = self.make_transport(
            capability_snapshot_id=capability.snapshot_id,
            events=events,
            wire=wire,
            quota_gate=quota,
        )
        response = transport(
            "bybit-order-1",
            guarded_order_projection(request),
            lambda: events.append("guard"),
        )

        self.assertEqual(response.payload["retCode"], 0)
        self.assertEqual(events, ["quota", "resolve", "guard", "wire"])
        self.assertEqual(len(resolver.calls), 1)
        self.assertEqual(len(wire.requests), 1)
        outbound = wire.requests[0]
        self.assertEqual(
            outbound.url,
            "https://api-testnet.bybit.com/v5/order/create",
        )
        self.assertEqual(outbound.headers["X-BAPI-API-KEY"], "api-key-SECRET")
        self.assertEqual(json.loads(outbound.body), dict(request.body))

    def test_exact_provider_environment_policy_cannot_cross_testnet_and_demo(self):
        capability, _request = prepared()
        with self.assertRaisesRegex(
            ProviderTransportScopeError,
            "exact provider environment",
        ):
            self.make_transport(
                capability_snapshot_id=capability.snapshot_id,
                events=[],
                provider_environment="TESTNET",
                policy=BYBIT_V5_ENDPOINT_POLICIES["DEMO"],
            )

    def test_scope_digest_and_provider_environment_fail_before_secret_or_wire(self):
        capability, request = prepared()
        mutations = (
            ("account_id", "other", "account mismatch"),
            ("environment", "LIVE", "environment mismatch"),
            (
                "provider_environment",
                "DEMO",
                "provider environment mismatch",
            ),
            (
                "capability_snapshot_id",
                "other-cap",
                "capability snapshot mismatch",
            ),
            ("body_sha256", "sha256:" + "0" * 64, "body digest mismatch"),
        )
        for field, value, pattern in mutations:
            with self.subTest(field=field):
                events = []
                wire = RecordingWire(events)
                transport, resolver = self.make_transport(
                    capability_snapshot_id=capability.snapshot_id,
                    events=events,
                    wire=wire,
                )
                projected = dict(guarded_order_projection(request))
                projected[field] = value
                if field == "capability_snapshot_id":
                    projected["capability_snapshot_ids"] = [value]
                with self.assertRaisesRegex(
                    ProviderTransportScopeError,
                    pattern,
                ):
                    transport(
                        "bybit-order-1",
                        projected,
                        lambda: events.append("guard"),
                    )
                self.assertEqual(events, [])
                self.assertEqual(resolver.calls, [])
                self.assertEqual(wire.requests, [])

    def test_final_guard_failure_has_zero_outbound_requests(self):
        capability, request = prepared()
        events = []
        wire = RecordingWire(events)
        transport, resolver = self.make_transport(
            capability_snapshot_id=capability.snapshot_id,
            events=events,
            wire=wire,
        )

        def blocked():
            events.append("guard")
            raise PermissionError("authority revoked")

        with self.assertRaisesRegex(PermissionError, "authority revoked"):
            transport(
                "bybit-order-1",
                guarded_order_projection(request),
                blocked,
            )
        self.assertEqual(events, ["resolve", "guard"])
        self.assertEqual(len(resolver.calls), 1)
        self.assertEqual(wire.requests, [])

    def test_quota_failure_has_zero_secret_resolution_and_zero_outbound(self):
        capability, request = prepared()
        events = []
        wire = RecordingWire(events)

        def quota(*_args):
            events.append("quota")
            raise RuntimeError("quota unavailable")

        transport, resolver = self.make_transport(
            capability_snapshot_id=capability.snapshot_id,
            events=events,
            wire=wire,
            quota_gate=quota,
        )
        with self.assertRaisesRegex(RuntimeError, "quota unavailable"):
            transport(
                "bybit-order-1",
                guarded_order_projection(request),
                lambda: events.append("guard"),
            )
        self.assertEqual(events, ["quota"])
        self.assertEqual(resolver.calls, [])
        self.assertEqual(wire.requests, [])

    def test_dispatcher_marks_post_barrier_loss_unknown_and_never_resends(self):
        with TemporaryDirectory() as directory:
            events = []
            wire = RecordingWire(
                events,
                error=TimeoutError("response lost after possible send"),
            )
            store = JournalStore(f"{directory}/journal.sqlite3")
            dispatcher = GuardedDispatcher(
                store,
                environment="PAPER",
                account_id="bybit-account",
                owner_token="owner-bybit",
            )
            intent_id = "intent-bybit-e2e"
            client_id = stable_client_order_id(
                "BYBIT",
                intent_id,
                environment="PAPER",
                account_id="bybit-account",
            )
            capability, request = prepared(client_id)
            transport, _resolver = self.make_transport(
                capability_snapshot_id=capability.snapshot_id,
                events=events,
                wire=wire,
            )
            projected = guarded_order_projection(request)

            result = dispatcher.dispatch(
                attempt_id="attempt-bybit-e2e",
                intent_id=intent_id,
                intent_hash="intent-hash-bybit",
                provider="BYBIT",
                request=projected,
                now="2026-09-25T10:00:00Z",
                authority_check=lambda _hash, _now: (True, "allowed"),
                transport_send=transport,
                sender_check=lambda _owner, _epoch: None,
                final_barrier_clock=lambda: "2026-09-25T10:00:01Z",
                submission_scope={
                    "capability_snapshot_id": capability.snapshot_id,
                    "provider": "BYBIT",
                    "account_id": "bybit-account",
                    "environment": "PAPER",
                    "provider_environment": "TESTNET",
                },
            )
            self.assertEqual(result.status, "UNKNOWN")
            self.assertEqual(result.reason, "transport_result_ambiguous")
            self.assertEqual(events.count("wire"), 1)

            repeated = dispatcher.dispatch(
                attempt_id="attempt-bybit-e2e",
                intent_id=intent_id,
                intent_hash="intent-hash-bybit",
                provider="BYBIT",
                request=projected,
                now="2026-09-25T10:00:02Z",
                authority_check=lambda _hash, _now: (True, "allowed"),
                transport_send=transport,
                sender_check=lambda _owner, _epoch: None,
                submission_scope={
                    "capability_snapshot_id": capability.snapshot_id,
                    "provider": "BYBIT",
                    "account_id": "bybit-account",
                    "environment": "PAPER",
                    "provider_environment": "TESTNET",
                },
            )
            self.assertEqual(repeated.status, "UNKNOWN")
            self.assertEqual(events.count("wire"), 1)

            durable = store.load_events(
                "submission_attempt",
                dispatcher._aggregate_id("attempt-bybit-e2e"),
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
            self.assertNotIn("X-BAPI-SIGN", durable_text)


if __name__ == "__main__":
    unittest.main()
