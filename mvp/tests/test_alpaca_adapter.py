from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest
from uuid import uuid4

from mvp.autotrade_mvp.alpaca import (
    AlpacaAbsenceEvidence,
    AlpacaAdapterError,
    AlpacaOrderIntent,
    AlpacaPreparedRequest,
    paper_evidence_proves_live_execution_realism,
    coverage_evidence,
    parse_order_observation,
    parse_submission_response,
    parse_trade_activities,
    prepare_order_request,
)
from mvp.autotrade_mvp.capabilities import CapabilitySnapshot


NOW = datetime(2026, 9, 24, 20, tzinfo=timezone.utc)


def capability(
    *,
    order_types=("MARKET", "LIMIT", "STOP", "STOP_LIMIT"),
    tif=("DAY", "GTC", "IOC"),
    account_id="paper-account",
    environment="PAPER",
):
    evidence = {
        "artifact_id": str(uuid4()),
        "sha256": "sha256:" + "d" * 64,
        "observed_at": "2026-09-24T19:00:00Z",
        "source_uri": "https://docs.alpaca.markets/us/reference/postorder",
    }
    return CapabilitySnapshot(
        snapshot_id=str(uuid4()),
        provider_id="ALPACA",
        account_id=account_id,
        entity_id="alpaca",
        environment=environment,
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
    def test_direct_prepared_request_cannot_bypass_scope_or_provenance(self):
        base = {
            "endpoint": "/v2/orders",
            "body": {"symbol": "AAPL", "client_order_id": "at-direct"},
            "account_id": "paper-account",
            "environment": "PAPER",
            "capability_snapshot_id": "cap-1",
            "documentation_refs": ("https://docs.alpaca.markets/orders",),
        }
        request = AlpacaPreparedRequest(**base)
        self.assertEqual(request.environment, "PAPER")
        with self.assertRaisesRegex(AlpacaAdapterError, "endpoint"):
            AlpacaPreparedRequest(**{**base, "endpoint": "/v2/account"})
        with self.assertRaisesRegex(AlpacaAdapterError, "environment"):
            AlpacaPreparedRequest(**{**base, "environment": "SIMULATION"})
        with self.assertRaisesRegex(AlpacaAdapterError, "capability_snapshot_id"):
            AlpacaPreparedRequest(**{**base, "capability_snapshot_id": " "})
        with self.assertRaisesRegex(AlpacaAdapterError, "documentation_refs"):
            AlpacaPreparedRequest(**{**base, "documentation_refs": ()})
        with self.assertRaisesRegex(AlpacaAdapterError, "must be unique"):
            AlpacaPreparedRequest(
                **{
                    **base,
                    "capability_snapshot_ids": ("cap-1", "cap-1"),
                }
            )
        with self.assertRaisesRegex(AlpacaAdapterError, "primary capability_snapshot_id"):
            AlpacaPreparedRequest(
                **{
                    **base,
                    "capability_snapshot_ids": ("other-cap",),
                }
            )

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
            account_id="paper-account",
            environment="PAPER",
            capability=capability(),
            at=NOW,
        )
        self.assertEqual(request.endpoint, "/v2/orders")
        self.assertEqual(request.account_id, "paper-account")
        self.assertEqual(request.environment, "PAPER")
        self.assertEqual(request.body["qty"], "1.25")
        self.assertEqual(request.body["limit_price"], "220.10")
        self.assertNotIn("notional", request.body)

    def test_order_preparation_is_bound_to_exact_account_and_environment(self):
        intent = AlpacaOrderIntent.create(
            instrument_version="AAPL:v1",
            asset_class="EQUITY",
            symbol="AAPL",
            side="BUY",
            order_type="MARKET",
            time_in_force="DAY",
            quantity="1",
        )
        with self.assertRaisesRegex(AlpacaAdapterError, "account"):
            prepare_order_request(
                intent,
                client_order_id="at-wrong-account",
                account_id="other-account",
                environment="PAPER",
                capability=capability(),
                at=NOW,
            )
        with self.assertRaisesRegex(AlpacaAdapterError, "environment"):
            prepare_order_request(
                intent,
                client_order_id="at-wrong-env",
                account_id="paper-account",
                environment="LIVE",
                capability=capability(environment="PAPER"),
                at=NOW,
            )

    def test_crypto_notional_uses_native_crypto_tif(self):
        intent = AlpacaOrderIntent.create(
            instrument_version="BTCUSD:v1",
            asset_class="CRYPTO",
            symbol="BTC/USD",
            side="BUY",
            order_type="MARKET",
            time_in_force="GTC",
            notional="100",
        )
        self.assertEqual(intent.notional, Decimal("100"))

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

    def test_extended_hours_allows_documented_gtc_equity_limit(self):
        allowed = AlpacaOrderIntent.create(
            instrument_version="AAPL:v1",
            asset_class="EQUITY",
            symbol="AAPL",
            side="BUY",
            order_type="LIMIT",
            time_in_force="GTC",
            quantity="1",
            limit_price="220",
            extended_hours=True,
        )
        self.assertTrue(allowed.extended_hours)

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
                account_id="paper-account",
                environment="PAPER",
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

    def test_positive_filled_quantity_requires_average_price(self):
        with self.assertRaisesRegex(AlpacaAdapterError, "filled_avg_price is required"):
            parse_order_observation(
                {
                    "id": "order-1",
                    "client_order_id": "at-order-1",
                    "symbol": "AAPL",
                    "status": "partially_filled",
                    "filled_qty": "0.5",
                    "filled_avg_price": None,
                }
            )
        with self.assertRaisesRegex(AlpacaAdapterError, "filled_avg_price is required"):
            parse_order_observation(
                {
                    "id": "order-1",
                    "client_order_id": "at-order-1",
                    "symbol": "AAPL",
                    "status": "filled",
                    "filled_qty": "1",
                    "filled_avg_price": "",
                }
            )

    def test_missing_filled_quantity_is_not_silently_zero(self):
        with self.assertRaisesRegex(AlpacaAdapterError, "filled_qty is required"):
            parse_order_observation(
                {
                    "id": "order-1",
                    "client_order_id": "at-order-1",
                    "symbol": "AAPL",
                    "status": "new",
                }
            )
        with self.assertRaisesRegex(AlpacaAdapterError, "filled_qty is required"):
            parse_order_observation(
                {
                    "id": "order-1",
                    "client_order_id": "at-order-1",
                    "symbol": "AAPL",
                    "status": "new",
                    "filled_qty": None,
                }
            )

    def test_null_provider_identity_fields_fail_closed_instead_of_stringifying(self):
        base = {
            "id": "order-1",
            "client_order_id": "at-order-1",
            "symbol": "AAPL",
            "status": "new",
            "filled_qty": "0",
        }
        for field in ("id", "client_order_id", "symbol", "status"):
            with self.subTest(field=field):
                payload = dict(base)
                payload[field] = None
                with self.assertRaises((AlpacaAdapterError, ValueError, TypeError)):
                    parse_order_observation(payload)

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
        complete_but_unqualified = AlpacaAbsenceEvidence(
            by_client_order_id_complete=True,
            orders_history_complete=True,
            trade_events_complete=True,
            activities_complete=True,
            consistency_horizon_satisfied=True,
            order_found=False,
        )
        self.assertEqual(complete_but_unqualified.verdict(), "INCONCLUSIVE")
        qualified = AlpacaAbsenceEvidence(
            by_client_order_id_complete=True,
            orders_history_complete=True,
            trade_events_complete=True,
            activities_complete=True,
            consistency_horizon_satisfied=True,
            order_found=False,
            qualified_exclusion_semantics=True,
        )
        self.assertEqual(qualified.verdict(), "PROVEN_ABSENT")


    def test_success_order_response_is_ack_only_not_fill(self):
        order_id = str(uuid4())
        result = parse_submission_response(
            attempt_id=str(uuid4()),
            client_order_id="at-ack-1",
            response={
                "id": order_id,
                "client_order_id": "at-ack-1",
                "status": "filled",
                "filled_qty": "1",
            },
            observed_at="2026-09-24T20:00:00Z",
            environment="PAPER",
        )
        self.assertEqual(result["outcome"], "ACKNOWLEDGED")
        self.assertEqual(result["retry_disposition"], "NEVER")
        self.assertEqual(result["provider_order_id"], order_id)

    def test_transport_ambiguity_is_unknown_and_reconcile_first(self):
        result = parse_submission_response(
            attempt_id=str(uuid4()),
            client_order_id="at-unknown-1",
            response=None,
            observed_at="2026-09-24T20:00:00Z",
            environment="PAPER",
            transport_ambiguous=True,
        )
        self.assertEqual(result["outcome"], "UNKNOWN")
        self.assertEqual(result["retry_disposition"], "RECONCILE_FIRST")
        self.assertIsNone(result["provider_received_at"])
        self.assertEqual(result["observed_at"], "2026-09-24T20:00:00Z")
        self.assertEqual(result["provider_environment"], "PAPER")
        self.assertEqual(result["evidence"], [])

    def test_transport_ambiguity_cannot_claim_provider_response(self):
        with self.assertRaisesRegex(AlpacaAdapterError, "must not fabricate"):
            parse_submission_response(
                attempt_id=str(uuid4()),
                client_order_id="at-unknown-2",
                response={
                    "id": str(uuid4()),
                    "client_order_id": "at-unknown-2",
                },
                observed_at="2026-09-24T20:00:00Z",
                environment="PAPER",
                transport_ambiguous=True,
            )

    def test_trade_activity_requires_order_and_fee_evidence(self):
        order_id = str(uuid4())
        row = {
            "activity_type": "FILL",
            "id": "20190524113406977::fill-1",
            "order_id": order_id,
            "symbol": "AAPL",
            "qty": "1",
            "price": "220.10",
            "transaction_time": "2026-09-24T20:01:00Z",
        }
        with self.assertRaisesRegex(AlpacaAdapterError, "fee evidence"):
            parse_trade_activities(
                [row],
                instrument_versions={"AAPL": "AAPL:v1"},
                client_ids_by_order_id={order_id: "at-ack-1"},
                fees_by_activity_id={},
            )
        fills = parse_trade_activities(
            [row, row],
            instrument_versions={"AAPL": "AAPL:v1"},
            client_ids_by_order_id={order_id: "at-ack-1"},
            fees_by_activity_id={row["id"]: ("0.01", "USD")},
        )
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0].fee_amount, Decimal("0.01"))

    def test_canonical_coverage_defaults_to_unproven_absence(self):
        evidence = coverage_evidence(
            surface="ACTIVITIES",
            coverage_start="2026-09-24T19:00:00Z",
            coverage_end="2026-09-24T21:00:00Z",
            pagination_complete=True,
            consistency_horizon_satisfied=True,
        )
        self.assertFalse(evidence.provider_semantics_exclude_execution)

    def test_paper_is_not_live_execution_realism_proof(self):
        self.assertFalse(paper_evidence_proves_live_execution_realism())


from mvp.autotrade_mvp.alpaca import (
    AlpacaMlegLeg,
    AlpacaMlegOrderIntent,
    prepare_mleg_order_request,
)


def mleg_capability(instrument_version, *, account_id="paper-account", environment="PAPER"):
    base = capability(order_types=("MARKET", "LIMIT"), tif=("DAY",))
    return CapabilitySnapshot(
        snapshot_id=str(uuid4()),
        provider_id="ALPACA",
        account_id=account_id,
        entity_id="alpaca",
        environment=environment,
        instrument_version=instrument_version,
        observed_at=base.observed_at,
        expires_at=base.expires_at,
        supported_order_types=base.supported_order_types,
        time_in_force=base.time_in_force,
        permission_scopes=base.permission_scopes,
        position_mode=base.position_mode,
        native_protection=base.native_protection,
        rate_limit_policy_id=base.rate_limit_policy_id,
        data_entitlements=base.data_entitlements,
        evidence=base.evidence,
        status=base.status,
        sources=base.sources,
    )


class AlpacaMlegFoundationTests(unittest.TestCase):
    def leg(self, symbol, ratio, side, position_intent):
        return AlpacaMlegLeg.create(
            instrument_version=f"{symbol}:v1",
            underlying_version="AAPL:v1",
            symbol=symbol,
            ratio_quantity=ratio,
            side=side,
            position_intent=position_intent,
        )

    def test_limit_spread_keeps_parent_and_leg_identity(self):
        first = self.leg(
            "AAPL261218C00200000", "1", "BUY", "buy_to_open"
        )
        second = self.leg(
            "AAPL261218C00210000", "1", "SELL", "sell_to_open"
        )
        intent = AlpacaMlegOrderIntent.create(
            underlying_version="AAPL:v1",
            quantity="2",
            order_type="LIMIT",
            time_in_force="DAY",
            limit_price="-0.60",
            legs=(first, second),
        )
        request = prepare_mleg_order_request(
            intent,
            client_order_id="at-mleg-1",
            capabilities={
                first.instrument_version: mleg_capability(first.instrument_version),
                second.instrument_version: mleg_capability(second.instrument_version),
            },
            at=NOW,
        )
        self.assertEqual(request.body["order_class"], "mleg")
        self.assertEqual(request.account_id, "paper-account")
        self.assertEqual(request.environment, "PAPER")
        self.assertEqual(
            request.capability_snapshot_ids,
            tuple(
                capabilities[leg.instrument_version].snapshot_id
                for leg in intent.legs
            ),
        )
        self.assertEqual(request.body["qty"], "2")
        self.assertEqual(request.body["limit_price"], "-0.60")
        self.assertNotIn("symbol", request.body)
        self.assertNotIn("side", request.body)
        self.assertEqual(request.body["legs"][0]["ratio_qty"], "1")
        self.assertEqual(
            request.body["legs"][1]["position_intent"], "sell_to_open"
        )

    def test_market_has_no_parent_limit_price(self):
        first = self.leg(
            "AAPL261218P00200000", 1, "BUY", "buy_to_open"
        )
        second = self.leg(
            "AAPL261218P00190000", 1, "SELL", "sell_to_open"
        )
        intent = AlpacaMlegOrderIntent.create(
            underlying_version="AAPL:v1",
            quantity=1,
            order_type="MARKET",
            time_in_force="DAY",
            legs=(first, second),
        )
        request = prepare_mleg_order_request(
            intent,
            client_order_id="at-mleg-market",
            capabilities={
                first.instrument_version: mleg_capability(first.instrument_version),
                second.instrument_version: mleg_capability(second.instrument_version),
            },
            at=NOW,
        )
        self.assertNotIn("limit_price", request.body)

    def test_mleg_requires_two_to_four_legs(self):
        first = self.leg(
            "AAPL261218C00200000", 1, "BUY", "buy_to_open"
        )
        with self.assertRaisesRegex(AlpacaAdapterError, "2 to 4"):
            AlpacaMlegOrderIntent.create(
                underlying_version="AAPL:v1",
                quantity=1,
                order_type="MARKET",
                time_in_force="DAY",
                legs=(first,),
            )
        with self.assertRaisesRegex(AlpacaAdapterError, "2 to 4"):
            AlpacaMlegOrderIntent.create(
                underlying_version="AAPL:v1",
                quantity=1,
                order_type="MARKET",
                time_in_force="DAY",
                legs=(first, first, first, first, first),
            )

    def test_duplicate_leg_identity_must_use_ratio_quantity(self):
        first = self.leg(
            "AAPL261218C00200000", 1, "BUY", "buy_to_open"
        )
        with self.assertRaisesRegex(AlpacaAdapterError, "instrument versions"):
            AlpacaMlegOrderIntent.create(
                underlying_version="AAPL:v1",
                quantity=1,
                order_type="LIMIT",
                time_in_force="DAY",
                limit_price="1",
                legs=(first, first),
            )

        duplicate_symbol = AlpacaMlegLeg.create(
            instrument_version="AAPL261218C00200000:v2",
            underlying_version="AAPL:v1",
            symbol="AAPL261218C00200000",
            ratio_quantity=1,
            side="SELL",
            position_intent="sell_to_open",
        )
        with self.assertRaisesRegex(AlpacaAdapterError, "provider symbols"):
            AlpacaMlegOrderIntent.create(
                underlying_version="AAPL:v1",
                quantity=1,
                order_type="LIMIT",
                time_in_force="DAY",
                limit_price="1",
                legs=(first, duplicate_symbol),
            )

    def test_ratio_quantities_must_be_whole_positive_and_reduced(self):
        with self.assertRaisesRegex(AlpacaAdapterError, "whole"):
            self.leg("AAPL261218C00200000", "1.5", "BUY", "buy_to_open")
        first = self.leg(
            "AAPL261218C00200000", 4, "BUY", "buy_to_open"
        )
        second = self.leg(
            "AAPL261218C00210000", 2, "SELL", "sell_to_open"
        )
        with self.assertRaisesRegex(AlpacaAdapterError, "simplest"):
            AlpacaMlegOrderIntent.create(
                underlying_version="AAPL:v1",
                quantity=1,
                order_type="LIMIT",
                time_in_force="DAY",
                limit_price="1",
                legs=(first, second),
            )

    def test_position_intent_must_match_leg_side(self):
        with self.assertRaisesRegex(AlpacaAdapterError, "BUY"):
            self.leg(
                "AAPL261218C00200000",
                1,
                "SELL",
                "buy_to_open",
            )

    def test_underlying_mismatch_fails_closed(self):
        first = self.leg(
            "AAPL261218C00200000", 1, "BUY", "buy_to_open"
        )
        second = AlpacaMlegLeg.create(
            instrument_version="MSFT261218C00500000:v1",
            underlying_version="MSFT:v1",
            symbol="MSFT261218C00500000",
            ratio_quantity=1,
            side="SELL",
            position_intent="sell_to_open",
        )
        with self.assertRaisesRegex(AlpacaAdapterError, "underlying"):
            AlpacaMlegOrderIntent.create(
                underlying_version="AAPL:v1",
                quantity=1,
                order_type="LIMIT",
                time_in_force="DAY",
                limit_price="1",
                legs=(first, second),
            )

    def test_limit_credit_and_debit_are_exact_but_zero_is_rejected(self):
        first = self.leg(
            "AAPL261218C00200000", 1, "BUY", "buy_to_open"
        )
        second = self.leg(
            "AAPL261218C00210000", 1, "SELL", "sell_to_open"
        )
        credit = AlpacaMlegOrderIntent.create(
            underlying_version="AAPL:v1",
            quantity=1,
            order_type="LIMIT",
            time_in_force="DAY",
            limit_price="-1.2500",
            legs=(first, second),
        )
        self.assertEqual(credit.limit_price, Decimal("-1.2500"))
        with self.assertRaisesRegex(AlpacaAdapterError, "non-zero"):
            AlpacaMlegOrderIntent.create(
                underlying_version="AAPL:v1",
                quantity=1,
                order_type="LIMIT",
                time_in_force="DAY",
                limit_price="0",
                legs=(first, second),
            )
        with self.assertRaises(AlpacaAdapterError):
            AlpacaMlegOrderIntent.create(
                underlying_version="AAPL:v1",
                quantity=1,
                order_type="LIMIT",
                time_in_force="DAY",
                limit_price=1.25,
                legs=(first, second),
            )

    def test_day_only_is_explicit_until_mleg_gtc_is_qualified(self):
        first = self.leg(
            "AAPL261218C00200000", 1, "BUY", "buy_to_open"
        )
        second = self.leg(
            "AAPL261218C00210000", 1, "SELL", "sell_to_open"
        )
        with self.assertRaisesRegex(AlpacaAdapterError, "DAY"):
            AlpacaMlegOrderIntent.create(
                underlying_version="AAPL:v1",
                quantity=1,
                order_type="LIMIT",
                time_in_force="GTC",
                limit_price="1",
                legs=(first, second),
            )

    def test_every_leg_needs_same_account_exact_capability(self):
        first = self.leg(
            "AAPL261218C00200000", 1, "BUY", "buy_to_open"
        )
        second = self.leg(
            "AAPL261218C00210000", 1, "SELL", "sell_to_open"
        )
        intent = AlpacaMlegOrderIntent.create(
            underlying_version="AAPL:v1",
            quantity=1,
            order_type="LIMIT",
            time_in_force="DAY",
            limit_price="1",
            legs=(first, second),
        )
        with self.assertRaisesRegex(AlpacaAdapterError, "missing exact capability"):
            prepare_mleg_order_request(
                intent,
                client_order_id="at-mleg-missing",
                capabilities={
                    first.instrument_version: mleg_capability(first.instrument_version)
                },
                at=NOW,
            )
        with self.assertRaisesRegex(AlpacaAdapterError, "same account"):
            prepare_mleg_order_request(
                intent,
                client_order_id="at-mleg-cross-account",
                capabilities={
                    first.instrument_version: mleg_capability(
                        first.instrument_version, account_id="a"
                    ),
                    second.instrument_version: mleg_capability(
                        second.instrument_version, account_id="b"
                    ),
                },
                at=NOW,
            )


if __name__ == "__main__":
    unittest.main()
