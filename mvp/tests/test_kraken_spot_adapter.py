from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest
from uuid import uuid4

from mvp.autotrade_mvp.capabilities import CapabilitySnapshot
from mvp.autotrade_mvp.kraken_spot import (
    KrakenSpotAbsenceEvidence,
    KrakenSpotAdapterError,
    KrakenSpotOrderIntent,
    derivatives_supported_by_this_module,
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
            capability=capability(),
            at=NOW,
        )
        self.assertEqual(request.endpoint, "/0/private/AddOrder")
        self.assertEqual(request.body["volume"], "0.0100")
        self.assertEqual(request.body["price"], "60000.25")
        self.assertEqual(request.body["cl_ord_id"], "at-0123456789abcd")
        self.assertNotIn("nonce", request.body)
        self.assertNotIn("deadline", request.body)

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
                capability=capability(order_types=("MARKET",)),
                at=NOW,
            )

    def test_add_order_acknowledgement_never_proves_fill(self):
        ack = parse_spot_submission_response(
            {
                "error": [],
                "result": {
                    "descr": {"order": "buy 0.01000000 XBTUSD @ limit 60000.0"},
                    "txid": ["OABC-D123-E456"],
                },
            }
        )
        self.assertEqual(ack.provider_order_ids, ("OABC-D123-E456",))
        self.assertFalse(ack.proves_fill)

    def test_provider_error_is_rejection_not_success(self):
        with self.assertRaisesRegex(KrakenSpotAdapterError, "rejected"):
            parse_spot_submission_response(
                {"error": ["EOrder:Insufficient funds"], "result": None}
            )

    def test_empty_txid_fails_closed(self):
        with self.assertRaisesRegex(KrakenSpotAdapterError, "transaction ids"):
            parse_spot_submission_response({"error": [], "result": {"txid": []}})

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

    def test_absence_requires_order_trade_ledger_and_horizon_coverage(self):
        evidence = KrakenSpotAbsenceEvidence(
            order_found=False,
            open_orders_complete=True,
            closed_orders_complete=True,
            trades_complete=True,
            ledgers_complete=True,
            consistency_horizon_satisfied=True,
        )
        self.assertEqual(evidence.verdict(), "PROVEN_ABSENT")

    def test_derivatives_are_not_silently_claimed_by_spot_module(self):
        self.assertFalse(derivatives_supported_by_this_module())


if __name__ == "__main__":
    unittest.main()
