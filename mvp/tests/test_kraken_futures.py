from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import unittest
from uuid import uuid4

from mvp.autotrade_mvp.capabilities import (
    CapabilityClaim,
    EvidenceVerification,
    derive_capability_snapshot,
)
from mvp.autotrade_mvp.kraken_futures import (
    KrakenFuturesPreparedRequest,
    build_order_payload,
    coverage_evidence,
    futures_base_url,
    parse_position_executions,
    parse_submission_response,
    prepare_order_request,
)
from mvp.autotrade_mvp.provider_core import (
    ProviderCoreError,
    Surface,
    observe_authenticated_json_response,
    prepare_authenticated_read_query,
)


NOW_DT = datetime(2026, 9, 24, 20, tzinfo=timezone.utc)
NOW = "2026-09-24T20:00:00Z"


def futures_read_capability(*, account_id="paper-1"):
    observed_at = NOW_DT - timedelta(hours=1)
    claims = tuple(
        CapabilityClaim(
            source=source,
            provider_id="KRAKEN",
            account_id=account_id,
            entity_id="futures-api",
            environment="PAPER",
            instrument_version="PI_XBTUSD@v1",
            observed_at=observed_at,
            expires_at=NOW_DT + timedelta(hours=1),
            supported_order_types=frozenset({"MARKET", "LIMIT"}),
            time_in_force=frozenset({"GTC"}),
            permission_scopes=frozenset({"ORDER.READ"}),
            position_mode="NET",
            native_protection=frozenset(),
            rate_limit_policy_id="kraken-futures-paper",
            data_entitlements=frozenset({"EXECUTIONS"}),
            evidence_ref={
                "artifact_id": str(uuid4()),
                "sha256": "sha256:" + "e" * 64,
                "observed_at": observed_at.isoformat().replace("+00:00", "Z"),
            },
        )
        for source in ("DOCUMENTED", "API", "ACCOUNT", "INSTRUMENT")
    )
    return derive_capability_snapshot(
        snapshot_id=str(uuid4()),
        claims=claims,
        observed_at=observed_at,
        evidence_verifier=lambda _claim: EvidenceVerification(valid=True),
    )


def futures_position_observation(payload, *, account_id="paper-1", endpoint="/api/history/v3/positions"):
    binding = prepare_authenticated_read_query(
        capability=futures_read_capability(account_id=account_id),
        surface=Surface.AUTHENTICATED_READ,
        endpoint=endpoint,
        query={},
        at=NOW_DT,
    )
    return observe_authenticated_json_response(
        query_binding=binding,
        response_bytes=json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8"),
        observed_at=NOW_DT,
    )


def futures_write_capability(
    *,
    account_id="futures-account",
    account_environment="PAPER",
    instrument_version="PI_XBTUSD@v1",
):
    observed_at = NOW_DT - timedelta(hours=1)
    claims = tuple(
        CapabilityClaim(
            source=source,
            provider_id="KRAKEN",
            account_id=account_id,
            entity_id="futures-trading",
            environment=account_environment,
            instrument_version=instrument_version,
            observed_at=observed_at,
            expires_at=NOW_DT + timedelta(hours=1),
            supported_order_types=frozenset({"MARKET", "LIMIT"}),
            time_in_force=frozenset({"GTC"}),
            permission_scopes=frozenset({"ORDER_WRITE"}),
            position_mode="NET",
            native_protection=frozenset(),
            rate_limit_policy_id="kraken-futures-write",
            data_entitlements=frozenset(),
            evidence_ref={
                "artifact_id": str(uuid4()),
                "sha256": "sha256:" + "f" * 64,
                "observed_at": observed_at.isoformat().replace("+00:00", "Z"),
            },
        )
        for source in ("DOCUMENTED", "API", "ACCOUNT", "INSTRUMENT")
    )
    return derive_capability_snapshot(
        snapshot_id=str(uuid4()),
        claims=claims,
        observed_at=NOW_DT,
        evidence_verifier=lambda _claim: EvidenceVerification(valid=True),
    )


def prepared_futures_request(
    client_order_id: str,
    *,
    provider_environment="DEMO",
    account_environment="PAPER",
) -> KrakenFuturesPreparedRequest:
    return prepare_order_request(
        capability=futures_write_capability(account_environment=account_environment),
        account_id="futures-account",
        provider_environment=provider_environment,
        instrument_version="PI_XBTUSD@v1",
        at=NOW_DT,
        symbol="PI_XBTUSD",
        side="BUY",
        order_type="MARKET",
        size="1",
        client_order_id=client_order_id,
    )


def futures_response_bytes(payload) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


class KrakenFuturesAdapterTests(unittest.TestCase):
    def test_live_and_demo_services_are_explicit(self):
        self.assertEqual(futures_base_url("LIVE"), "https://futures.kraken.com")
        self.assertEqual(futures_base_url("DEMO"), "https://demo-futures.kraken.com")
        with self.assertRaises(ProviderCoreError):
            futures_base_url("SPOT-TESTNET")

    def test_limit_payload_preserves_exact_values_and_reduce_only(self):
        payload = build_order_payload(
            environment="DEMO",
            symbol="PI_XBTUSD",
            side="SELL",
            order_type="LIMIT",
            size=Decimal("2.00"),
            price="70000.00",
            client_order_id="hedge-001",
            reduce_only=True,
        )
        self.assertEqual(payload, {
            "orderType": "lmt",
            "symbol": "PI_XBTUSD",
            "side": "sell",
            "size": "2",
            "cliOrdId": "hedge-001",
            "limitPrice": "70000",
            "reduceOnly": "true",
        })

    def test_unsafe_payload_shapes_fail_closed(self):
        with self.assertRaisesRegex(ProviderCoreError, "ignored"):
            build_order_payload(
                environment="LIVE", symbol="PI_XBTUSD", side="BUY",
                order_type="MARKET", size="1", price="10",
                client_order_id="hedge-002",
            )
        with self.assertRaisesRegex(ProviderCoreError, "exact decimal"):
            build_order_payload(
                environment="LIVE", symbol="PI_XBTUSD", side="BUY",
                order_type="MARKET", size=0.1,
                client_order_id="hedge-003",
            )
        with self.assertRaisesRegex(ProviderCoreError, "client_order_id"):
            build_order_payload(
                environment="LIVE", symbol="PI_XBTUSD", side="BUY",
                order_type="MARKET", size="1",
                client_order_id="this-client-identity-is-not-qualified",
            )

    def test_provider_endpoint_environment_must_match_canonical_account_environment(self):
        with self.assertRaisesRegex(ProviderCoreError, "canonical account environment"):
            prepared_futures_request(
                "bad-demo-live",
                provider_environment="DEMO",
                account_environment="LIVE",
            )
        with self.assertRaisesRegex(ProviderCoreError, "canonical account environment"):
            prepared_futures_request(
                "bad-live-paper",
                provider_environment="LIVE",
                account_environment="PAPER",
            )

    def test_success_is_acknowledgement_not_fill(self):
        for send_status in (
            {"order_id": "provider-order-1", "status": "placed"},
            '{"order_id":"provider-order-1","status":"placed"}',
        ):
            with self.subTest(send_status=send_status):
                result = parse_submission_response(
                    attempt_id=str(uuid4()),
                    prepared_request=prepared_futures_request(
                        "hedge-004",
                        provider_environment="LIVE",
                        account_environment="LIVE",
                    ),
                    observed_at=NOW,
                    response_bytes=futures_response_bytes(
                        {"result": "success", "sendStatus": send_status}
                    ),
                )
                self.assertEqual(result["outcome"], "ACKNOWLEDGED")
                self.assertEqual(result["provider_order_id"], "provider-order-1")
                self.assertEqual(result["retry_disposition"], "NEVER")
                self.assertNotIn("fill", repr(result).lower())

    def test_transport_ambiguity_is_unknown_and_never_blind_retried(self):
        result = parse_submission_response(
            attempt_id=str(uuid4()),
            prepared_request=prepared_futures_request("hedge-005"),
            observed_at=NOW,
            response_bytes=None,
            transport_ambiguous=True,
        )
        self.assertEqual(result["outcome"], "UNKNOWN")
        self.assertEqual(result["retry_disposition"], "RECONCILE_FIRST")
        self.assertEqual(result["evidence"], [])
        self.assertNotIn("provider_received_at", result)
        self.assertNotIn("observed_at", result)

    def test_transport_ambiguity_cannot_claim_response_bytes(self):
        with self.assertRaisesRegex(ProviderCoreError, "response bytes"):
            parse_submission_response(
                attempt_id=str(uuid4()),
                prepared_request=prepared_futures_request("hedge-ambiguous"),
                observed_at=NOW,
                response_bytes=futures_response_bytes({"result": "success"}),
                transport_ambiguous=True,
            )

    def test_explicit_provider_error_is_rejected(self):
        result = parse_submission_response(
            attempt_id=str(uuid4()),
            prepared_request=prepared_futures_request(
                "hedge-006",
                provider_environment="LIVE",
                account_environment="LIVE",
            ),
            observed_at=NOW,
            response_bytes=futures_response_bytes(
                {"result": "error", "error": "insufficientFunds"}
            ),
        )
        self.assertEqual(result["outcome"], "REJECTED")
        self.assertEqual(result["retry_disposition"], "NEVER")

    def test_submission_evidence_binds_exact_request_response_and_attempt(self):
        request = prepared_futures_request("hedge-bind")
        payload = {
            "result": "success",
            "sendStatus": {
                "order_id": "provider-bind",
                "cliOrdId": "hedge-bind",
            },
        }
        raw_a = futures_response_bytes(payload)
        raw_b = json.dumps(payload, indent=1).encode("utf-8")
        attempt = str(uuid4())
        a = parse_submission_response(
            attempt_id=attempt,
            prepared_request=request,
            observed_at=NOW,
            response_bytes=raw_a,
        )
        b = parse_submission_response(
            attempt_id=attempt,
            prepared_request=request,
            observed_at=NOW,
            response_bytes=raw_b,
        )
        other_attempt = parse_submission_response(
            attempt_id=str(uuid4()),
            prepared_request=request,
            observed_at=NOW,
            response_bytes=raw_a,
        )
        self.assertNotEqual(a["evidence"][0]["sha256"], b["evidence"][0]["sha256"])
        self.assertNotEqual(
            a["evidence"][0]["artifact_id"],
            b["evidence"][0]["artifact_id"],
        )
        self.assertNotEqual(
            a["evidence"][0]["artifact_id"],
            other_attempt["evidence"][0]["artifact_id"],
        )

    def test_submission_response_cannot_relabel_guarded_client_identity(self):
        request = prepared_futures_request("hedge-expected")
        with self.assertRaisesRegex(ProviderCoreError, "guarded request"):
            parse_submission_response(
                attempt_id=str(uuid4()),
                prepared_request=request,
                observed_at=NOW,
                response_bytes=futures_response_bytes(
                    {
                        "result": "success",
                        "sendStatus": {
                            "order_id": "provider-mismatch",
                            "cliOrdId": "hedge-other",
                        },
                    }
                ),
            )

    def test_position_history_maps_only_trade_execution_facts(self):
        fills = parse_position_executions(
            futures_position_observation({
                "elements": [
                    {
                        "tradeable": "PI_XBTUSD",
                        "fillTime": 1790280000123,
                        "fee": "1.25",
                        "feeCurrency": "USD",
                        "executionUid": "exec-1",
                        "executionPrice": "65000.10",
                        "executionSize": "0.25",
                        "timestamp": 1790280000123,
                        "updateReason": "trade",
                    },
                    {
                        "tradeable": "PI_XBTUSD",
                        "timestamp": 1790280000456,
                        "updateReason": "fundingRealisation",
                        "realizedFunding": "-0.10",
                    },
                ]
            }),
            instrument_versions={"PI_XBTUSD": "PI_XBTUSD@v1"},
            execution_client_ids={"exec-1": "hedge-007"},
        )
        self.assertEqual(len(fills), 1)
        fill = fills[0]
        self.assertEqual(fill.provider_execution_id, "exec-1")
        self.assertEqual(fill.client_order_id, "hedge-007")
        self.assertEqual(fill.quantity, Decimal("0.25"))
        self.assertEqual(fill.price, Decimal("65000.10"))
        self.assertEqual(fill.fee_amount, Decimal("1.25"))
        self.assertEqual(fill.trade_time, "2026-09-24T20:00:00.123Z")
        self.assertEqual(fill.account_id, "paper-1")
        self.assertEqual(fill.environment, "PAPER")

    def test_position_history_requires_exact_bound_endpoint(self):
        observation = futures_position_observation(
            {"elements": []},
            endpoint="/derivatives/api/v3/fills",
        )
        with self.assertRaisesRegex(ProviderCoreError, "endpoint mismatch"):
            parse_position_executions(
                observation,
                instrument_versions={"PI_XBTUSD": "PI_XBTUSD@v1"},
            )

    def test_conflicting_duplicate_execution_id_fails_closed(self):
        base = {
            "tradeable": "PI_XBTUSD",
            "fillTime": 1790280000123,
            "fee": "1",
            "feeCurrency": "USD",
            "executionUid": "same",
            "executionPrice": "65000",
            "executionSize": "1",
            "timestamp": 1790280000123,
            "updateReason": "trade",
        }
        with self.assertRaisesRegex(ProviderCoreError, "conflicting"):
            parse_position_executions(
                futures_position_observation(
                    {"elements": [base, {**base, "executionSize": "2"}]}
                ),
                instrument_versions={"PI_XBTUSD": "PI_XBTUSD@v1"},
            )

    def test_futures_foundation_cannot_self_assert_absence_semantics(self):
        with self.assertRaisesRegex(
            ProviderCoreError,
            "cannot self-assert provider exclusion semantics",
        ):
            coverage_evidence(
                surface="EXECUTIONS",
                coverage_start="2026-09-24T19:00:00Z",
                coverage_end="2026-09-24T21:00:00Z",
                pagination_complete=True,
                consistency_horizon_satisfied=True,
                qualified_exclusion_semantics=True,
            
                account_id="paper-1",
                environment="PAPER",)

    def test_absence_semantics_default_fail_closed(self):
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
            evidence.proves_absence_for(datetime(2026, 9, 24, 20, tzinfo=timezone.utc))
        )


if __name__ == "__main__":
    unittest.main()
