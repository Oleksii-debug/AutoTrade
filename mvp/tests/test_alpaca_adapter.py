from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest
from uuid import uuid4

from mvp.autotrade_mvp.alpaca import (
    AlpacaAbsenceEvidence,
    AlpacaAdapterError,
    AlpacaOrderIntent,
    paper_evidence_proves_live_execution_realism,
    parse_order_observation,
    prepare_order_request,
)
from mvp.autotrade_mvp.capabilities import CapabilitySnapshot


NOW = datetime(2026, 9, 24, 20, tzinfo=timezone.utc)


def capability(*, order_types=("MARKET", "LIMIT", "STOP", "STOP_LIMIT"), tif=("DAY", "GTC", "IOC")):
    evidence = {
        "artifact_id": str(uuid4()),
        "sha256": "sha256:" + "d" * 64,
        "observed_at": "2026-09-24T19:00:00Z",
        "source_uri": "https://docs.alpaca.markets/us/reference/postorder",
    }
    return CapabilitySnapshot(
        snapshot_id=str(uuid4()),
        provider_id="ALPACA",
        account_id="paper-account",
        entity_id="alpaca",
        environment="PAPER",
        instrument_version="AAPL:v1",
        observed_at=NOW - timedelta(hours=1),
        expires_at=NOW + timedelta(hours=1),
        supported_order_types=frozenset(order_types),
        time_in_force=frozenset(tif),
        permission_scopes=frozenset({"ORDER_WRITE"}),
        position_mode="NET",
        native_protection=frozenset({"STOP"}),
        rate_limit_policy_id="alpaca-paper-test",
        data_entitlements=frozenset({"ORDERS", "ACTIVITIES"}),
        evidence=(evidence,),
        status="VERIFIED",
        sources=frozenset({"DOCUMENTED", "API", "ACCOUNT", "INSTRUMENT"}),
    )


class AlpacaAdapterTests(unittest.TestCase):
    def test_equity_limit_request_preserves_decimal_strings(self):
        intent = AlpacaOrderIntent.create(
            instrument_version="AAPL:v1",
            asset_class="EQUITY",
            symbol="aapl",
            side="buy",
            order_type="limit",
            time_in_force="day",
            quantity="1.25",
            limit_price="220.10",
        )
        request = prepare_order_request(
            intent,
            client_order_id="at-equity-1",
            capability=capability(),
            at=NOW,
        )
        self.assertEqual(request.endpoint, "/v2/orders")
        self.assertEqual(request.body["qty"], "1.25")
        self.assertEqual(request.body["limit_price"], "220.10")
        self.assertNotIn("notional", request.body)

    def test_crypto_notional_and_quantity_are_mutually_exclusive(self):
        with self.assertRaisesRegex(AlpacaAdapterError, "exactly one"):
            AlpacaOrderIntent.create(
                instrument_version="BTCUSD:v1",
                asset_class="CRYPTO",
                symbol="BTC/USD",
                side="BUY",
                order_type="MARKET",
                time_in_force="GTC",
                quantity="0.01",
                notional="100",
            )

    def test_crypto_stop_requires_stop_limit_shape(self):
        intent = AlpacaOrderIntent.create(
            instrument_version="BTCUSD:v1",
            asset_class="CRYPTO",
            symbol="BTC/USD",
            side="SELL",
            order_type="STOP_LIMIT",
            time_in_force="GTC",
            quantity="0.01",
            limit_price="55000",
            stop_price="56000",
        )
        self.assertEqual(intent.order_type, "STOP_LIMIT")
        with self.assertRaises(AlpacaAdapterError):
            AlpacaOrderIntent.create(
                instrument_version="BTCUSD:v1",
                asset_class="CRYPTO",
                symbol="BTC/USD",
                side="SELL",
                order_type="STOP",
                time_in_force="GTC",
                quantity="0.01",
                stop_price="56000",
            )

    def test_option_quantity_is_whole_and_notional_is_forbidden(self):
        intent = AlpacaOrderIntent.create(
            instrument_version="AAPL_OPT:v1",
            asset_class="OPTION",
            symbol="AAPL261218C00250000",
            side="BUY",
            order_type="LIMIT",
            time_in_force="GTC",
            quantity="2",
            limit_price="5.25",
            position_intent="buy_to_open",
        )
        self.assertEqual(intent.quantity, Decimal("2"))
        with self.assertRaisesRegex(AlpacaAdapterError, "whole"):
            AlpacaOrderIntent.create(
                instrument_version="AAPL_OPT:v1",
                asset_class="OPTION",
                symbol="AAPL261218C00250000",
                side="BUY",
                order_type="LIMIT",
                time_in_force="DAY",
                quantity="1.5",
                limit_price="5.25",
            )
        with self.assertRaisesRegex(AlpacaAdapterError, "notional"):
            AlpacaOrderIntent.create(
                instrument_version="AAPL_OPT:v1",
                asset_class="OPTION",
                symbol="AAPL261218C00250000",
                side="BUY",
                order_type="MARKET",
                time_in_force="DAY",
                notional="500",
            )

    def test_extended_hours_is_conservative_day_equity_limit_only(self):
        allowed = AlpacaOrderIntent.create(
            instrument_version="AAPL:v1",
            asset_class="EQUITY",
            symbol="AAPL",
            side="BUY",
            order_type="LIMIT",
            time_in_force="DAY",
            quantity="1",
            limit_price="220",
            extended_hours=True,
        )
        self.assertTrue(allowed.extended_hours)
        with self.assertRaisesRegex(AlpacaAdapterError, "extended_hours"):
            AlpacaOrderIntent.create(
                instrument_version="AAPL:v1",
                asset_class="EQUITY",
                symbol="AAPL",
                side="BUY",
                order_type="MARKET",
                time_in_force="DAY",
                quantity="1",
                extended_hours=True,
            )

    def test_binary_float_money_is_rejected(self):
        with self.assertRaises(AlpacaAdapterError):
            AlpacaOrderIntent.create(
                instrument_version="AAPL:v1",
                asset_class="EQUITY",
                symbol="AAPL",
                side="BUY",
                order_type="MARKET",
                time_in_force="DAY",
                quantity=1.0,
            )

    def test_capability_evidence_controls_admission(self):
        intent = AlpacaOrderIntent.create(
            instrument_version="AAPL:v1",
            asset_class="EQUITY",
            symbol="AAPL",
            side="BUY",
            order_type="LIMIT",
            time_in_force="DAY",
            quantity="1",
            limit_price="220",
        )
        with self.assertRaisesRegex(AlpacaAdapterError, "capability"):
            prepare_order_request(
                intent,
                client_order_id="at-order-1",
                capability=capability(order_types=("MARKET",)),
                at=NOW,
            )

    def test_order_observation_is_not_unique_fill_evidence(self):
        observed = parse_order_observation(
            {
                "id": "order-1",
                "client_order_id": "at-order-1",
                "symbol": "AAPL",
                "status": "filled",
                "filled_qty": "1",
                "filled_avg_price": "220.10",
            }
        )
        self.assertEqual(observed.filled_quantity, Decimal("1"))
        self.assertFalse(observed.proves_economic_fill)

    def test_average_fill_without_quantity_fails_closed(self):
        with self.assertRaisesRegex(AlpacaAdapterError, "filled_avg_price"):
            parse_order_observation(
                {
                    "id": "order-1",
                    "client_order_id": "at-order-1",
                    "symbol": "AAPL",
                    "status": "new",
                    "filled_qty": "0",
                    "filled_avg_price": "220.10",
                }
            )

    def test_absence_needs_orders_trade_events_activities_and_horizon(self):
        partial = AlpacaAbsenceEvidence(
            by_client_order_id_complete=True,
            orders_history_complete=True,
            trade_events_complete=True,
            activities_complete=False,
            consistency_horizon_satisfied=True,
            order_found=False,
        )
        self.assertEqual(partial.verdict(), "INCONCLUSIVE")
        complete = AlpacaAbsenceEvidence(
            by_client_order_id_complete=True,
            orders_history_complete=True,
            trade_events_complete=True,
            activities_complete=True,
            consistency_horizon_satisfied=True,
            order_found=False,
        )
        self.assertEqual(complete.verdict(), "PROVEN_ABSENT")

    def test_paper_is_not_live_execution_realism_proof(self):
        self.assertFalse(paper_evidence_proves_live_execution_realism())


if __name__ == "__main__":
    unittest.main()
