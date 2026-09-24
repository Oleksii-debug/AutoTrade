from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest
from uuid import uuid4

from mvp.autotrade_mvp.alpaca_v2 import (
    build_order_payload,
    coverage_evidence,
    parse_submission_response,
    parse_trade_activities,
)
from mvp.autotrade_mvp.capabilities import CapabilitySnapshot
from mvp.autotrade_mvp.provider_core import ProviderCoreError


NOW = datetime(2026, 9, 24, 20, tzinfo=timezone.utc)


def capability(*, instrument="AAPL@v1", order_types=None, tif=None):
    return CapabilitySnapshot(
        snapshot_id=str(uuid4()),
        provider_id="ALPACA",
        account_id="paper-1",
        entity_id="entity-1",
        environment="PAPER",
        instrument_version=instrument,
        observed_at=NOW - timedelta(minutes=5),
        expires_at=NOW + timedelta(minutes=5),
        supported_order_types=frozenset(
            order_types or {"MARKET", "LIMIT", "STOP", "STOP_LIMIT"}
        ),
        time_in_force=frozenset(tif or {"DAY", "GTC", "IOC"}),
        permission_scopes=frozenset({"ORDER_WRITE"}),
        position_mode="NET",
        native_protection=frozenset(),
        rate_limit_policy_id="alpaca-paper-recorded",
        data_entitlements=frozenset({"ORDERS", "ACTIVITIES"}),
        evidence=(
            {
                "artifact_id": str(uuid4()),
                "sha256": "sha256:" + "a" * 64,
                "observed_at": "2026-09-24T19:55:00Z",
            },
        ),
        status="VERIFIED",
        sources=frozenset({"DOCUMENTED", "API", "ACCOUNT", "INSTRUMENT"}),
    )


class AlpacaV2AdapterTests(unittest.TestCase):
    def test_equity_limit_order_uses_exact_quantity_and_existing_capability(self):
        payload = build_order_payload(
            capability=capability(),
            at=NOW,
            instrument_version="AAPL@v1",
            asset_class="EQUITIES",
            symbol="AAPL",
            side="BUY",
            order_type="LIMIT",
            quantity="1.2500",
            limit_price="250.1000",
            time_in_force="DAY",
            client_order_id="at-equity-1",
        )
        self.assertEqual(payload["qty"], "1.25")
        self.assertEqual(payload["limit_price"], "250.1")
        self.assertEqual(payload["time_in_force"], "day")
        self.assertNotIn("notional", payload)

    def test_extended_hours_is_bounded_to_equity_limit_day_or_gtc(self):
        payload = build_order_payload(
            capability=capability(),
            at=NOW,
            instrument_version="AAPL@v1",
            asset_class="EQUITIES",
            symbol="AAPL",
            side="BUY",
            order_type="LIMIT",
            quantity="1",
            limit_price="250",
            time_in_force="GTC",
            client_order_id="at-extended",
            extended_hours=True,
        )
        self.assertTrue(payload["extended_hours"])

        with self.assertRaisesRegex(ProviderCoreError, "extended hours"):
            build_order_payload(
                capability=capability(),
                at=NOW,
                instrument_version="AAPL@v1",
                asset_class="EQUITIES",
                symbol="AAPL",
                side="BUY",
                order_type="MARKET",
                quantity="1",
                time_in_force="DAY",
                client_order_id="bad-extended",
                extended_hours=True,
            )

    def test_crypto_and_options_have_distinct_fail_closed_rules(self):
        crypto = build_order_payload(
            capability=capability(
                instrument="BTC/USD@v1",
                order_types={"MARKET", "LIMIT", "STOP_LIMIT"},
                tif={"GTC", "IOC"},
            ),
            at=NOW,
            instrument_version="BTC/USD@v1",
            asset_class="CRYPTO",
            symbol="BTC/USD",
            side="BUY",
            order_type="MARKET",
            quantity="0.0001",
            time_in_force="GTC",
            client_order_id="crypto-1",
        )
        self.assertEqual(crypto["qty"], "0.0001")

        with self.assertRaisesRegex(ProviderCoreError, "whole contracts"):
            build_order_payload(
                capability=capability(
                    instrument="AAPL261218C00200000@v1",
                    tif={"DAY", "GTC"},
                ),
                at=NOW,
                instrument_version="AAPL261218C00200000@v1",
                asset_class="OPTIONS",
                symbol="AAPL261218C00200000",
                side="BUY",
                order_type="LIMIT",
                quantity="1.5",
                limit_price="5",
                time_in_force="DAY",
                client_order_id="option-fraction",
            )

    def test_prices_and_binary_float_economics_fail_closed(self):
        with self.assertRaisesRegex(ProviderCoreError, "limit_price is required"):
            build_order_payload(
                capability=capability(),
                at=NOW,
                instrument_version="AAPL@v1",
                asset_class="EQUITIES",
                symbol="AAPL",
                side="BUY",
                order_type="LIMIT",
                quantity="1",
                time_in_force="DAY",
                client_order_id="missing-price",
            )
        with self.assertRaisesRegex(ProviderCoreError, "exact decimal"):
            build_order_payload(
                capability=capability(),
                at=NOW,
                instrument_version="AAPL@v1",
                asset_class="EQUITIES",
                symbol="AAPL",
                side="BUY",
                order_type="MARKET",
                quantity=1.5,
                time_in_force="DAY",
                client_order_id="float-qty",
            )

    def test_capability_identity_is_not_bypassed(self):
        with self.assertRaisesRegex(ProviderCoreError, "another provider"):
            other = capability()
            object.__setattr__(other, "provider_id", "OTHER")
            build_order_payload(
                capability=other,
                at=NOW,
                instrument_version="AAPL@v1",
                asset_class="EQUITIES",
                symbol="AAPL",
                side="BUY",
                order_type="MARKET",
                quantity="1",
                time_in_force="DAY",
                client_order_id="wrong-provider",
            )

    def test_order_response_is_acknowledgement_even_when_status_says_filled(self):
        attempt = str(uuid4())
        order_id = str(uuid4())
        result = parse_submission_response(
            attempt_id=attempt,
            client_order_id="client-1",
            response={
                "id": order_id,
                "client_order_id": "client-1",
                "status": "filled",
                "filled_qty": "1",
                "filled_avg_price": "200",
            },
            observed_at="2026-09-24T20:00:00Z",
            environment="PAPER",
        )
        self.assertEqual(result["outcome"], "ACKNOWLEDGED")
        self.assertEqual(result["retry_disposition"], "NEVER")
        self.assertNotIn("fill", {key.lower() for key in result})
        self.assertEqual(result["provider_order_id"], order_id)

    def test_order_response_must_echo_exact_client_identity(self):
        with self.assertRaisesRegex(ProviderCoreError, "does not match"):
            parse_submission_response(
                attempt_id=str(uuid4()),
                client_order_id="expected",
                response={
                    "id": str(uuid4()),
                    "client_order_id": "other",
                },
                observed_at="2026-09-24T20:00:00Z",
                environment="PAPER",
            )

    def test_fill_activity_requires_fee_and_order_identity_evidence(self):
        order_id = str(uuid4())
        activity = {
            "activity_type": "FILL",
            "id": "20190524113406977::fill-1",
            "order_id": order_id,
            "symbol": "AAPL",
            "qty": "1",
            "price": "250.25",
            "side": "buy",
            "transaction_time": "2026-09-24T20:01:00Z",
        }
        with self.assertRaisesRegex(ProviderCoreError, "missing fee evidence"):
            parse_trade_activities(
                [activity],
                instrument_versions={"AAPL": "AAPL@v1"},
                client_ids_by_order_id={order_id: "client-1"},
                fees_by_activity_id={},
            )
        with self.assertRaisesRegex(ProviderCoreError, "order-to-client"):
            parse_trade_activities(
                [activity],
                instrument_versions={"AAPL": "AAPL@v1"},
                client_ids_by_order_id={},
                fees_by_activity_id={
                    activity["id"]: ("0.01", "USD"),
                },
            )

        fills = parse_trade_activities(
            [activity, activity],
            instrument_versions={"AAPL": "AAPL@v1"},
            client_ids_by_order_id={order_id: "client-1"},
            fees_by_activity_id={
                activity["id"]: ("0.01", "USD"),
            },
        )
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0].quantity, Decimal("1"))
        self.assertEqual(fills[0].fee_amount, Decimal("0.01"))
        self.assertEqual(fills[0].client_order_id, "client-1")

    def test_absence_semantics_remain_unproven_until_qualified(self):
        evidence = coverage_evidence(
            surface="ACTIVITIES",
            coverage_start="2026-09-24T19:00:00Z",
            coverage_end="2026-09-24T21:00:00Z",
            pagination_complete=True,
            consistency_horizon_satisfied=True,
        )
        self.assertFalse(evidence.provider_semantics_exclude_execution)


if __name__ == "__main__":
    unittest.main()
