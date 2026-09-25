from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest
from uuid import uuid4

from mvp.autotrade_mvp.capabilities import CapabilitySnapshot
from mvp.autotrade_mvp.kraken_spot import (
    KrakenSpotAbsenceEvidence,
    KrakenSpotAdapterError,
    KrakenSpotOrderIntent,
    coverage_evidence,
    derivatives_supported_by_this_module,
    parse_trade_history,
    parse_spot_submission_response,
    prepare_spot_order_request,
    validate_spot_client_order_id,
)


NOW = datetime(2026, 9, 24, 20, tzinfo=timezone.utc)


def capability(*, order_types=("MARKET", "LIMIT"), tif=("GTC", "IOC")):
    evidence = {
        "artifact_id": str(uuid4()),
        "sha256": "sha256:" + "b" * 64,
        "observed_at": "2026-09-24T19:00:00Z",
        "source_uri": "https://www.kraken.com/features/trading-api",
    }
    return CapabilitySnapshot(
        snapshot_id=str(uuid4()),
        provider_id="KRAKEN",
        account_id="spot-account",
        entity_id="kraken-spot",
        environment="PAPER",
        instrument_version="XBTUSD:v1",
        observed_at=NOW - timedelta(hours=1),
        expires_at=NOW + timedelta(hours=1),
        supported_order_types=frozenset(order_types),
        time_in_force=frozenset(tif),
        permission_scopes=frozenset({"ORDER_WRITE"}),
        position_mode="CASH",
        native_protection=frozenset(),
        rate_limit_policy_id="kraken-spot-test",
        data_entitlements=frozenset({"ORDERS", "TRADES", "LEDGERS"}),
        evidence=(evidence,),
        status="VERIFIED",
        sources=frozenset({"DOCUMENTED", "API", "ACCOUNT", "INSTRUMENT"}),
    )


class KrakenSpotAdapterTests(unittest.TestCase):
    def test_direct_prepared_request_fails_closed_on_scope_and_provenance(self):
        base = {
            "endpoint": "/0/private/AddOrder",
            "body": {
                "pair": "XBTUSD",
                "cl_ord_id": "at-direct",
            },
            "account_id": "spot-account",
            "environment": "PAPER",
            "capability_snapshot_id": "cap-1",
            "documentation_refs": ("https://docs.kraken.com/order",),
        }
        request = KrakenSpotPreparedRequest(**base)
        self.assertEqual(request.environment, "PAPER")
        with self.assertRaisesRegex(KrakenSpotAdapterError, "endpoint"):
            KrakenSpotPreparedRequest(**{**base, "endpoint": "/0/private/Withdraw"})
        with self.assertRaisesRegex(KrakenSpotAdapterError, "environment"):
            KrakenSpotPreparedRequest(**{**base, "environment": "STAGING"})
        with self.assertRaisesRegex(KrakenSpotAdapterError, "capability_snapshot_id"):
            KrakenSpotPreparedRequest(**{**base, "capability_snapshot_id": " "})
        with self.assertRaisesRegex(KrakenSpotAdapterError, "documentation_refs"):
            KrakenSpotPreparedRequest(**{**base, "documentation_refs": ()})
        with self.assertRaisesRegex(KrakenSpotAdapterError, "client_order_id"):
            KrakenSpotPreparedRequest(
                **{**base, "body": {"pair": "XBTUSD", "cl_ord_id": ""}}
            )

    def test_limit_request_preserves_exact_decimal_and_has_no_nonce(self):
        intent = KrakenSpotOrderIntent.create(
            instrument_version="XBTUSD:v1",
            pair="xbtusd",
            side="buy",
            order_type="limit",
            volume="0.0100",
            price="60000.25",
        )
        request = prepare_spot_order_request(
            intent,
            client_order_id="at-0123456789abcd",
            account_id="spot-account",
            environment="PAPER",
            capability=capability(),
            at=NOW,
        )
        self.assertEqual(request.endpoint, "/0/private/AddOrder")
        self.assertEqual(request.account_id, "spot-account")
        self.assertEqual(request.environment, "PAPER")
        self.assertEqual(request.body["volume"], "0.0100")
        self.assertEqual(request.body["price"], "60000.25")
        self.assertEqual(request.body["cl_ord_id"], "at-0123456789abcd")
        self.assertNotIn("nonce", request.body)
        self.assertNotIn("deadline", request.body)

    def test_prepared_order_is_bound_to_exact_account_and_environment(self):
        intent = KrakenSpotOrderIntent.create(
            instrument_version="XBTUSD:v1",
            pair="XBTUSD",
            side="BUY",
            order_type="MARKET",
            volume="0.01",
            time_in_force="IOC",
        )
        with self.assertRaisesRegex(KrakenSpotAdapterError, "account"):
            prepare_spot_order_request(
                intent,
                client_order_id="at-bind-account",
                account_id="other-account",
                environment="PAPER",
                capability=capability(),
                at=NOW,
            )
        with self.assertRaisesRegex(KrakenSpotAdapterError, "environment"):
            prepare_spot_order_request(
                intent,
                client_order_id="at-bind-env",
                account_id="spot-account",
                environment="LIVE",
                capability=capability(),
                at=NOW,
            )

    def test_dispatcher_style_client_id_must_fit_free_text_limit(self):
        self.assertEqual(validate_spot_client_order_id("at-0123456789abcd"), "at-0123456789abcd")
        with self.assertRaises(KrakenSpotAdapterError):
            validate_spot_client_order_id("at-" + "a" * 30)

    def test_uuid_client_id_is_supported(self):
        value = "6d1b345e-2821-40e2-ad83-4ecb18a06876"
        self.assertEqual(validate_spot_client_order_id(value), value)

    def test_binary_float_money_is_rejected(self):
        with self.assertRaises(KrakenSpotAdapterError):
            KrakenSpotOrderIntent.create(
                instrument_version="XBTUSD:v1",
                pair="XBTUSD",
                side="BUY",
                order_type="LIMIT",
                volume=0.01,
                price="60000",
            )

    def test_post_only_ioc_conflict_fails_before_send(self):
        with self.assertRaisesRegex(KrakenSpotAdapterError, "mutually exclusive"):
            KrakenSpotOrderIntent.create(
                instrument_version="XBTUSD:v1",
                pair="XBTUSD",
                side="BUY",
                order_type="LIMIT",
                volume="0.01",
                price="60000",
                time_in_force="IOC",
                post_only=True,
            )

    def test_capability_mismatch_fails_closed(self):
        intent = KrakenSpotOrderIntent.create(
            instrument_version="XBTUSD:v1",
            pair="XBTUSD",
            side="BUY",
            order_type="LIMIT",
            volume="0.01",
            price="60000",
        )
        with self.assertRaisesRegex(KrakenSpotAdapterError, "capability"):
            prepare_spot_order_request(
                intent,
                client_order_id="at-order-1",
                account_id="spot-account",
                environment="PAPER",
                capability=capability(order_types=("MARKET",)),
                at=NOW,
            )

    def _parse_submission(self, payload, **overrides):
        values = {
            "attempt_id": str(uuid4()),
            "client_order_id": "at-order-1",
            "environment": "LIVE",
            "observed_at": "2026-09-24T20:00:00Z",
            "source_uri": "https://api.kraken.com/0/private/AddOrder",
            "payload": payload,
        }
        values.update(overrides)
        return parse_spot_submission_response(**values)

    def test_add_order_acknowledgement_never_proves_fill(self):
        result = self._parse_submission(
            {
                "error": [],
                "result": {
                    "descr": {"order": "buy 0.01000000 XBTUSD @ limit 60000.0"},
                    "txid": ["OABC-D123-E456"],
                },
            }
        )
        self.assertEqual(result["outcome"], "ACKNOWLEDGED")
        self.assertEqual(result["provider_order_ids"], ("OABC-D123-E456",))
        self.assertEqual(result["retry_disposition"], "NEVER")
        self.assertNotIn("fill", repr(result).lower())
        self.assertEqual(
            result["evidence"][0]["source_uri"],
            "https://api.kraken.com/0/private/AddOrder",
        )
        self.assertEqual(result["evidence"][0]["provider_environment"], "LIVE")

    def test_provider_error_is_canonical_rejection_not_exception_or_success(self):
        result = self._parse_submission(
            {"error": ["EOrder:Insufficient funds"], "result": None}
        )
        self.assertEqual(result["outcome"], "REJECTED")
        self.assertEqual(result["retry_disposition"], "NEVER")

    def test_transport_ambiguity_is_unknown_and_reconcile_first(self):
        result = self._parse_submission(
            None,
            transport_ambiguous=True,
        )
        self.assertEqual(result["outcome"], "UNKNOWN")
        self.assertEqual(result["retry_disposition"], "RECONCILE_FIRST")
        self.assertEqual(result["evidence"], [])
        self.assertIsNone(result["provider_received_at"])
        self.assertEqual(result["observed_at"], "2026-09-24T20:00:00Z")

        with self.assertRaisesRegex(KrakenSpotAdapterError, "must not fabricate"):
            self._parse_submission(
                {"error": [], "result": {"txid": ["OABC-D123-E456"]}},
                transport_ambiguous=True,
            )

    def test_submission_provenance_requires_exact_https_add_order_endpoint(self):
        with self.assertRaisesRegex(KrakenSpotAdapterError, "source_uri"):
            self._parse_submission(
                {"error": [], "result": {"txid": ["OABC-D123-E456"]}},
                source_uri="http://example.invalid/0/private/AddOrder",
            )

    def test_submission_provenance_rejects_spoofed_host_and_unqualified_environment(self):
        payload = {"error": [], "result": {"txid": ["OABC-D123-E456"]}}
        with self.assertRaisesRegex(KrakenSpotAdapterError, "source_uri"):
            self._parse_submission(
                payload,
                source_uri="https://example.invalid/0/private/AddOrder",
            )
        with self.assertRaisesRegex(KrakenSpotAdapterError, "qualified only for LIVE"):
            self._parse_submission(payload, environment="PAPER")

    def test_empty_txid_fails_closed(self):
        with self.assertRaisesRegex(KrakenSpotAdapterError, "transaction ids"):
            self._parse_submission({"error": [], "result": {"txid": []}})

    def test_incomplete_search_never_proves_absence(self):
        evidence = KrakenSpotAbsenceEvidence(
            order_found=False,
            open_orders_complete=True,
            closed_orders_complete=True,
            trades_complete=False,
            ledgers_complete=False,
            consistency_horizon_satisfied=True,
        )
        self.assertEqual(evidence.verdict(), "INCONCLUSIVE")

    def test_complete_surfaces_do_not_self_qualify_provider_absence(self):
        evidence = KrakenSpotAbsenceEvidence(
            order_found=False,
            open_orders_complete=True,
            closed_orders_complete=True,
            trades_complete=True,
            ledgers_complete=True,
            consistency_horizon_satisfied=True,
        )
        self.assertEqual(evidence.verdict(), "INCONCLUSIVE")
        with self.assertRaisesRegex(
            KrakenSpotAdapterError,
            "cannot self-assert provider exclusion semantics",
        ):
            KrakenSpotAbsenceEvidence(
                order_found=False,
                open_orders_complete=True,
                closed_orders_complete=True,
                trades_complete=True,
                ledgers_complete=True,
                consistency_horizon_satisfied=True,
                qualified_exclusion_semantics=True,
            )

    def test_derivatives_are_not_silently_claimed_by_spot_module(self):
        self.assertFalse(derivatives_supported_by_this_module())


    def test_trade_history_uses_trade_id_as_unique_fill_identity(self):
        fills = parse_trade_history(
            {
                "error": [],
                "result": {
                    "count": 1,
                    "trades": {
                        "T-EXEC-1": {
                            "ordertxid": "OABC-D123-E456",
                            "pair": "XXBTZUSD",
                            "time": "1790280001.123456",
                            "price": "60000.25",
                            "vol": "0.0100",
                            "fee": "0.20",
                        }
                    },
                },
            },
            instrument_versions={"XXBTZUSD": "XBTUSD:v1"},
            client_ids_by_provider_order={"OABC-D123-E456": "at-order-1"},
            fee_currency_by_pair={"XXBTZUSD": "USD"},
        )
        self.assertEqual(len(fills), 1)
        fill = fills[0]
        self.assertEqual(fill.provider_execution_id, "T-EXEC-1")
        self.assertEqual(fill.client_order_id, "at-order-1")
        self.assertEqual(fill.instrument, "XBTUSD:v1")
        self.assertEqual(fill.quantity, Decimal("0.0100"))
        self.assertEqual(fill.price, Decimal("60000.25"))
        self.assertEqual(fill.fee_amount, Decimal("0.20"))
        self.assertEqual(fill.fee_currency, "USD")
        self.assertEqual(fill.trade_time, "2026-09-24T20:00:01.123456Z")

    def test_trade_history_refuses_to_guess_instrument_or_fee_currency(self):
        response = {
            "error": [],
            "result": {
                "trades": {
                    "T-EXEC-1": {
                        "ordertxid": "OABC-D123-E456",
                        "pair": "XXBTZUSD",
                        "time": "1790280001",
                        "price": "60000",
                        "vol": "0.01",
                        "fee": "0.2",
                    }
                }
            },
        }
        with self.assertRaisesRegex(KrakenSpotAdapterError, "unmapped Kraken pair"):
            parse_trade_history(
                response,
                instrument_versions={},
                client_ids_by_provider_order={},
                fee_currency_by_pair={"XXBTZUSD": "USD"},
            )
        with self.assertRaisesRegex(KrakenSpotAdapterError, "fee currency"):
            parse_trade_history(
                response,
                instrument_versions={"XXBTZUSD": "XBTUSD:v1"},
                client_ids_by_provider_order={},
                fee_currency_by_pair={},
            )

    def test_trade_timestamp_rejects_binary_float_and_sub_microsecond_precision(self):
        base = {
            "error": [],
            "result": {
                "trades": {
                    "T-EXEC-1": {
                        "ordertxid": "OABC-D123-E456",
                        "pair": "XXBTZUSD",
                        "time": 1790280001.25,
                        "price": "60000",
                        "vol": "0.01",
                        "fee": "0.2",
                    }
                }
            },
        }
        with self.assertRaisesRegex(KrakenSpotAdapterError, "exact decimal"):
            parse_trade_history(
                base,
                instrument_versions={"XXBTZUSD": "XBTUSD:v1"},
                client_ids_by_provider_order={},
                fee_currency_by_pair={"XXBTZUSD": "USD"},
            )
        base["result"]["trades"]["T-EXEC-1"]["time"] = "1790280001.1234567"
        with self.assertRaisesRegex(KrakenSpotAdapterError, "microsecond"):
            parse_trade_history(
                base,
                instrument_versions={"XXBTZUSD": "XBTUSD:v1"},
                client_ids_by_provider_order={},
                fee_currency_by_pair={"XXBTZUSD": "USD"},
            )

    def test_coverage_defaults_to_non_authoritative_absence(self):
        coverage = coverage_evidence(
            surface="executions",
            coverage_start="2026-09-24T19:00:00Z",
            coverage_end="2026-09-24T21:00:00Z",
            pagination_complete=True,
            consistency_horizon_satisfied=True,
        )
        self.assertEqual(coverage.surface, "EXECUTIONS")
        self.assertFalse(coverage.provider_semantics_exclude_execution)
        self.assertFalse(coverage.proves_absence_for(NOW))
        with self.assertRaisesRegex(
            KrakenSpotAdapterError,
            "cannot self-assert provider exclusion semantics",
        ):
            coverage_evidence(
                surface="executions",
                coverage_start="2026-09-24T19:00:00Z",
                coverage_end="2026-09-24T21:00:00Z",
                pagination_complete=True,
                consistency_horizon_satisfied=True,
                qualified_exclusion_semantics=True,
            )



if __name__ == "__main__":
    unittest.main()
