from datetime import datetime, timezone
from decimal import Decimal
import re
import unittest
from uuid import uuid4

from mvp.autotrade_mvp.kraken_spot import (
    build_spot_order_payload,
    classify_transport_failure,
    coverage_evidence,
    kraken_client_order_id,
    next_nonce,
    parse_submission_response,
    parse_trade_history,
    spot_rest_base_url,
)
from mvp.autotrade_mvp.provider_core import ProviderCoreError


class KrakenSpotAdapterTests(unittest.TestCase):
    def test_client_order_identity_is_deterministic_and_provider_bounded(self):
        client_id = str(uuid4())
        first = kraken_client_order_id(client_id)
        second = kraken_client_order_id(client_id.upper())
        self.assertEqual(first, second)
        self.assertEqual(len(first), 18)
        self.assertRegex(first, r"^AT[A-Za-z0-9_-]{16}$")

    def test_nonce_is_strictly_monotonic_even_when_clock_repeats_or_regresses(self):
        self.assertEqual(
            next_nonce(previous_nonce=1000, observed_time_ms=1000), 1001
        )
        self.assertEqual(
            next_nonce(previous_nonce=1001, observed_time_ms=900), 1002
        )
        self.assertEqual(
            next_nonce(previous_nonce=1002, observed_time_ms=2000), 2000
        )
        with self.assertRaisesRegex(ProviderCoreError, "integer"):
            next_nonce(previous_nonce=True, observed_time_ms=2000)

    def test_spot_live_environment_does_not_invent_derivatives_demo_parity(self):
        self.assertEqual(spot_rest_base_url("live"), "https://api.kraken.com")
        for environment in ("demo", "test", "paper"):
            with self.subTest(environment=environment):
                with self.assertRaisesRegex(ProviderCoreError, "separate API family"):
                    spot_rest_base_url(environment)

    def test_market_and_limit_payloads_keep_exact_quantity_and_identity(self):
        client_id = str(uuid4())
        market = build_spot_order_payload(
            pair="XBTUSD",
            side="BUY",
            order_type="MARKET",
            volume=Decimal("0.0100"),
            client_order_id=client_id,
            time_in_force="IOC",
        )
        self.assertEqual(market["volume"], "0.01")
        self.assertEqual(market["cl_ord_id"], kraken_client_order_id(client_id))
        self.assertNotIn("price", market)

        limit = build_spot_order_payload(
            pair="ETHUSD",
            side="SELL",
            order_type="LIMIT",
            volume="2.500",
            price="3456.7000",
            client_order_id=client_id,
            leverage="2:1",
        )
        self.assertEqual(limit["volume"], "2.5")
        self.assertEqual(limit["price"], "3456.7")
        self.assertEqual(limit["leverage"], "2:1")

    def test_unsafe_or_ambiguous_order_shapes_fail_closed(self):
        client_id = str(uuid4())
        common = dict(
            pair="XBTUSD",
            side="BUY",
            client_order_id=client_id,
        )
        with self.assertRaisesRegex(ProviderCoreError, "ignored limit price"):
            build_spot_order_payload(
                **common,
                order_type="MARKET",
                volume="1",
                price="1",
            )
        with self.assertRaisesRegex(ProviderCoreError, "limit price"):
            build_spot_order_payload(
                **common,
                order_type="LIMIT",
                volume="1",
            )
        with self.assertRaisesRegex(ProviderCoreError, "exact decimal"):
            build_spot_order_payload(
                **common,
                order_type="LIMIT",
                volume=0.1,
                price="10",
            )
        with self.assertRaisesRegex(ProviderCoreError, "MARKET or LIMIT"):
            build_spot_order_payload(
                **common,
                order_type="STOP_LOSS",
                volume="1",
            )

    def test_successful_add_order_is_acknowledgement_not_fill(self):
        attempt = str(uuid4())
        client_id = str(uuid4())
        result = parse_submission_response(
            attempt_id=attempt,
            client_order_id=client_id,
            observed_at="2026-09-24T20:00:00Z",
            response={
                "error": [],
                "result": {
                    "descr": {"order": "buy 0.1 XBTUSD @ market"},
                    "txid": ["OABCDE-12345-XYZ789"],
                },
            },
        )
        self.assertEqual(result["outcome"], "ACKNOWLEDGED")
        self.assertEqual(result["retry_disposition"], "NEVER")
        self.assertEqual(result["provider_order_id"], "OABCDE-12345-XYZ789")
        self.assertEqual(
            result["provider_client_order_id"],
            kraken_client_order_id(client_id),
        )
        self.assertNotIn("fill", repr(result).lower())

    def test_explicit_provider_error_is_rejected(self):
        result = parse_submission_response(
            attempt_id=str(uuid4()),
            client_order_id=str(uuid4()),
            observed_at="2026-09-24T20:00:00Z",
            response={
                "error": ["EOrder:Insufficient funds"],
                "result": {},
            },
        )
        self.assertEqual(result["outcome"], "REJECTED")
        self.assertEqual(result["retry_disposition"], "NEVER")
        self.assertIn("Insufficient funds", result["reason_code"])

    def test_transport_failure_after_send_is_unknown_and_never_blindly_retried(self):
        result = classify_transport_failure(
            attempt_id=str(uuid4()),
            client_order_id=str(uuid4()),
            send_started=True,
            observed_at="2026-09-24T20:00:01Z",
            reason_code="TIMEOUT_AFTER_SEND",
        )
        self.assertEqual(result["outcome"], "UNKNOWN")
        self.assertEqual(result["retry_disposition"], "RECONCILE_FIRST")

        not_sent = classify_transport_failure(
            attempt_id=str(uuid4()),
            client_order_id=str(uuid4()),
            send_started=False,
            observed_at="2026-09-24T20:00:01Z",
            reason_code="AUTHORITY_REVOKED_BEFORE_SEND",
        )
        self.assertEqual(not_sent["outcome"], "NOT_SENT")

    def test_trade_history_preserves_execution_identity_and_exact_economics(self):
        client_id = str(uuid4())
        response = {
            "error": [],
            "result": {
                "trades": {
                    "TAAAAA-BBBBB-CCCCCC": {
                        "ordertxid": "OORDER-1",
                        "pair": "XBTUSD",
                        "time": "1790280000.123456",
                        "type": "buy",
                        "ordertype": "limit",
                        "price": "65000.10",
                        "cost": "16250.025",
                        "fee": "4.8750075",
                        "vol": "0.25",
                    }
                }
            },
        }
        fills = parse_trade_history(
            response,
            instrument_versions={"XBTUSD": "XBTUSD@v1"},
            client_ids_by_provider_order={"OORDER-1": client_id},
            fee_currency_by_pair={"XBTUSD": "USD"},
        )
        self.assertEqual(len(fills), 1)
        fill = fills[0]
        self.assertEqual(fill.provider_execution_id, "TAAAAA-BBBBB-CCCCCC")
        self.assertEqual(fill.client_order_id, client_id)
        self.assertEqual(fill.instrument, "XBTUSD@v1")
        self.assertEqual(fill.quantity, Decimal("0.25"))
        self.assertEqual(fill.price, Decimal("65000.10"))
        self.assertEqual(fill.fee_amount, Decimal("4.8750075"))
        self.assertEqual(fill.trade_time, "2026-09-24T20:00:00.123456Z")

    def test_trade_history_refuses_to_guess_missing_canonical_context(self):
        response = {
            "error": [],
            "result": {
                "trades": {
                    "T1": {
                        "ordertxid": "O1",
                        "pair": "XBTUSD",
                        "time": "1790280000",
                        "price": "65000",
                        "fee": "1",
                        "vol": "0.1",
                    }
                }
            },
        }
        with self.assertRaisesRegex(ProviderCoreError, "unmapped Kraken pair"):
            parse_trade_history(
                response,
                instrument_versions={},
                client_ids_by_provider_order={},
                fee_currency_by_pair={"XBTUSD": "USD"},
            )
        with self.assertRaisesRegex(ProviderCoreError, "fee currency"):
            parse_trade_history(
                response,
                instrument_versions={"XBTUSD": "XBTUSD@v1"},
                client_ids_by_provider_order={},
                fee_currency_by_pair={},
            )

    def test_trade_timestamp_and_money_reject_binary_float_inputs(self):
        response = {
            "error": [],
            "result": {
                "trades": {
                    "T1": {
                        "ordertxid": "O1",
                        "pair": "XBTUSD",
                        "time": 1790280000.5,
                        "price": "65000",
                        "fee": "1",
                        "vol": "0.1",
                    }
                }
            },
        }
        with self.assertRaisesRegex(ProviderCoreError, "exact decimal"):
            parse_trade_history(
                response,
                instrument_versions={"XBTUSD": "XBTUSD@v1"},
                client_ids_by_provider_order={},
                fee_currency_by_pair={"XBTUSD": "USD"},
            )

    def test_absence_coverage_is_fail_closed_until_explicitly_qualified(self):
        evidence = coverage_evidence(
            surface="EXECUTIONS",
            coverage_start="2026-09-24T19:00:00Z",
            coverage_end="2026-09-24T21:00:00Z",
            pagination_complete=True,
            consistency_horizon_satisfied=True,
        )
        self.assertFalse(evidence.provider_semantics_exclude_execution)
        self.assertFalse(
            evidence.proves_absence_for(
                datetime(2026, 9, 24, 20, tzinfo=timezone.utc)
            )
        )


if __name__ == "__main__":
    unittest.main()
