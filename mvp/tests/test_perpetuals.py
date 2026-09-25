from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
from fractions import Fraction
import unittest

from mvp.autotrade_mvp.perpetuals import (
    CollateralQuote,
    FundingConvention,
    FundingLedger,
    LiquidationSnapshot,
    MarginSnapshot,
    MarketSnapshot,
    PerpetualContract,
    PerpetualError,
    funding_cashflow,
    inverse_funding_cashflow_exact,
    inverse_perpetual_pnl_exact,
    inverse_stressed_loss_exact,
    linear_notional,
    require_liquidation_headroom,
    require_new_risk_capacity,
    stressed_loss,
)


NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)


class PerpetualLifecycleTests(unittest.TestCase):
    def contract(self, payoff="LINEAR"):
        return PerpetualContract(
            instrument_id="BTC-PERP",
            settlement_currency="USDT",
            collateral_currency="USDT",
            multiplier=Decimal("0.001"),
            payoff=payoff,
        )

    def market(self, *, mark="100000", index="100000", age_seconds=0):
        return MarketSnapshot(
            mark_price=Decimal(mark),
            index_price=Decimal(index),
            observed_at=NOW - timedelta(seconds=age_seconds),
            max_age=timedelta(seconds=5),
            max_mark_index_deviation=Decimal("0.01"),
        )

    def test_linear_notional_preserves_signed_exposure(self):
        self.assertEqual(
            linear_notional(signed_contracts="-2", multiplier="0.001", price="100000"),
            Decimal("-200.000"),
        )

    def test_funding_sign_convention_is_explicit(self):
        currency, amount = funding_cashflow(
            contract=self.contract(),
            signed_contracts="2",
            funding_rate="0.001",
            snapshot=self.market(),
            convention=FundingConvention("LONG_PAYS", "MARK"),
            at=NOW,
        )
        self.assertEqual(currency, "USDT")
        self.assertEqual(amount, Decimal("-0.200000"))

    def test_market_deviation_gate_is_independent_of_decimal_context_precision(self):
        market = MarketSnapshot(
            mark_price=Decimal("4"),
            index_price=Decimal("3"),
            observed_at=NOW,
            max_age=timedelta(seconds=5),
            max_mark_index_deviation=Decimal("0.32"),
        )
        with localcontext() as context:
            context.prec = 1
            with self.assertRaisesRegex(PerpetualError, "deviation"):
                market.require_valid(NOW)

    def test_stale_or_future_market_state_fails_closed(self):
        with self.assertRaises(PerpetualError):
            self.market(age_seconds=6).require_valid(NOW)
        with self.assertRaises(PerpetualError):
            self.market().require_valid(NOW - timedelta(seconds=1))

    def test_excessive_mark_index_deviation_fails_closed(self):
        with self.assertRaises(PerpetualError):
            self.market(mark="102000", index="100000").require_valid(NOW)

    def test_inverse_contract_exact_pnl_funding_and_stress(self):
        contract = PerpetualContract(
            instrument_id="BTC-USD-INVERSE-PERP",
            settlement_currency="BTC",
            collateral_currency="BTC",
            multiplier=Decimal("1"),
            payoff="INVERSE",
            face_currency="USD",
            price_quote_currency="USD",
            price_base_currency="BTC",
        )
        self.assertEqual(
            inverse_perpetual_pnl_exact(
                contract=contract,
                signed_contracts="100",
                entry_price="10000",
                exit_price="11000",
            ),
            Fraction(1, 1100),
        )
        currency, funding = inverse_funding_cashflow_exact(
            contract=contract,
            signed_contracts="100",
            funding_rate="0.001",
            snapshot=self.market(mark="10000", index="10000"),
            convention=FundingConvention("LONG_PAYS", "MARK"),
            at=NOW,
        )
        self.assertEqual(currency, "BTC")
        self.assertEqual(funding, Fraction(-1, 100000))
        self.assertEqual(
            inverse_stressed_loss_exact(
                contract=contract,
                signed_contracts="100",
                mark_price="10000",
                adverse_move_fraction="0.1",
            ),
            Fraction(1, 900),
        )
        self.assertEqual(
            inverse_stressed_loss_exact(
                contract=contract,
                signed_contracts="-100",
                mark_price="10000",
                adverse_move_fraction="0.1",
            ),
            Fraction(1, 1100),
        )

    def test_inverse_exact_math_rejects_currency_dimension_mismatch(self):
        contract = PerpetualContract(
            instrument_id="BTC-USD-INVERSE-PERP",
            settlement_currency="BTC",
            collateral_currency="BTC",
            multiplier=Decimal("1"),
            payoff="INVERSE",
            face_currency="USD",
            price_quote_currency="EUR",
            price_base_currency="BTC",
        )
        with self.assertRaisesRegex(PerpetualError, "face_currency must match"):
            inverse_perpetual_pnl_exact(
                contract=contract,
                signed_contracts="100",
                entry_price="10000",
                exit_price="11000",
            )

    def test_inverse_exact_math_rejects_settlement_currency_dimension_mismatch(self):
        contract = PerpetualContract(
            instrument_id="BTC-USD-INVERSE-PERP",
            settlement_currency="USDT",
            collateral_currency="USDT",
            multiplier=Decimal("1"),
            payoff="INVERSE",
            face_currency="USD",
            price_quote_currency="USD",
            price_base_currency="BTC",
        )
        with self.assertRaisesRegex(PerpetualError, "settlement_currency must match"):
            inverse_perpetual_pnl_exact(
                contract=contract,
                signed_contracts="100",
                entry_price="10000",
                exit_price="11000",
            )

    def test_inverse_exact_math_requires_face_currency_evidence(self):
        with self.assertRaises(PerpetualError):
            inverse_perpetual_pnl_exact(
                contract=self.contract("INVERSE"),
                signed_contracts="1",
                entry_price="10000",
                exit_price="11000",
            )

    def test_inverse_contract_needs_provider_specific_qualification(self):
        with self.assertRaises(PerpetualError):
            funding_cashflow(
                contract=self.contract("INVERSE"),
                signed_contracts="1",
                funding_rate="0.001",
                snapshot=self.market(),
                convention=FundingConvention("LONG_PAYS", "MARK"),
                at=NOW,
            )
        with self.assertRaises(PerpetualError):
            stressed_loss(
                contract=self.contract("INVERSE"),
                signed_contracts="1",
                mark_price="100000",
                adverse_move_fraction="0.1",
            )

    def test_liquidation_headroom_requires_fresh_tier_evidence(self):
        liquidation = LiquidationSnapshot(
            side="LONG",
            liquidation_price="90000",
            tier_id="tier-2",
            evidence_ref="provider-margin-tier:rev-7",
            observed_at=NOW,
            max_age=timedelta(seconds=5),
        )
        headroom = require_liquidation_headroom(
            liquidation=liquidation,
            market=self.market(),
            minimum_headroom_fraction="0.08",
            at=NOW,
        )
        self.assertEqual(headroom, Decimal("0.1"))

        with self.assertRaises(PerpetualError):
            require_liquidation_headroom(
                liquidation=liquidation,
                market=self.market(),
                minimum_headroom_fraction="0.11",
                at=NOW,
            )
        with self.assertRaises(PerpetualError):
            require_liquidation_headroom(
                liquidation=liquidation,
                market=self.market(),
                minimum_headroom_fraction="0.08",
                at=NOW + timedelta(seconds=6),
            )

    def test_liquidation_gate_is_independent_of_decimal_context_precision(self):
        liquidation = LiquidationSnapshot(
            side="LONG",
            liquidation_price="2",
            tier_id="precision-tier",
            evidence_ref="provider-margin-tier:precision",
            observed_at=NOW,
            max_age=timedelta(seconds=5),
        )
        market = MarketSnapshot(
            mark_price="3",
            index_price="3",
            observed_at=NOW,
            max_age=timedelta(seconds=5),
            max_mark_index_deviation="0.01",
        )
        with localcontext() as context:
            context.prec = 1
            require_liquidation_headroom(
                liquidation=liquidation,
                market=market,
                minimum_headroom_fraction="0.32",
                at=NOW,
            )
            with self.assertRaisesRegex(PerpetualError, "headroom"):
                require_liquidation_headroom(
                    liquidation=liquidation,
                    market=market,
                    minimum_headroom_fraction="0.34",
                    at=NOW,
                )

    def test_short_liquidation_boundary_has_opposite_direction(self):
        liquidation = LiquidationSnapshot(
            side="SHORT",
            liquidation_price="110000",
            tier_id="tier-short-1",
            evidence_ref="provider-margin-tier:rev-8",
            observed_at=NOW,
            max_age=timedelta(seconds=5),
        )
        self.assertEqual(
            liquidation.headroom_fraction("100000"),
            Decimal("0.1"),
        )
        invalid = LiquidationSnapshot(
            side="SHORT",
            liquidation_price="90000",
            tier_id="bad-tier",
            evidence_ref="provider-margin-tier:bad",
            observed_at=NOW,
            max_age=timedelta(seconds=5),
        )
        with self.assertRaises(PerpetualError):
            invalid.headroom_fraction("100000")

    def test_margin_admission_requires_fresh_sufficient_equity(self):
        margin = MarginSnapshot(
            equity=Decimal("1000"),
            maintenance_requirement=Decimal("400"),
            observed_at=NOW,
            max_age=timedelta(seconds=5),
        )
        require_new_risk_capacity(
            margin=margin,
            market=self.market(),
            stressed_position_loss="300",
            reserve_buffer="100",
            at=NOW,
        )
        with self.assertRaises(PerpetualError):
            require_new_risk_capacity(
                margin=margin,
                market=self.market(),
                stressed_position_loss="500",
                reserve_buffer="100",
                at=NOW,
            )

    def test_collateral_conversion_rejects_stale_quote(self):
        quote = CollateralQuote(
            from_currency="BTC",
            to_currency="USDT",
            rate=Decimal("100000"),
            observed_at=NOW,
            max_age=timedelta(seconds=5),
        )
        self.assertEqual(quote.convert("0.01", NOW), Decimal("1000.00"))
        with self.assertRaises(PerpetualError):
            quote.convert("0.01", NOW + timedelta(seconds=6))

    def test_funding_ledger_is_idempotent_and_conflict_detecting(self):
        ledger = FundingLedger()
        self.assertEqual(
            ledger.apply(event_id="f1", funding_period_id="2026-09-24T12:00Z", instrument_id="BTC-PERP", currency="USDT", amount="-1.25"),
            Decimal("-1.25"),
        )
        self.assertEqual(
            ledger.apply(event_id="f1", funding_period_id="2026-09-24T12:00Z", instrument_id="BTC-PERP", currency="USDT", amount="-1.25"),
            Decimal("-1.25"),
        )
        self.assertEqual(ledger.balance("USDT"), Decimal("-1.25"))
        with self.assertRaises(PerpetualError):
            ledger.apply(event_id="f1", funding_period_id="2026-09-24T12:00Z", instrument_id="BTC-PERP", currency="USDT", amount="-1.30")

    def test_currency_aliases_canonicalize_across_inverse_units_and_funding(self):
        contract = PerpetualContract(
            instrument_id="BTC-USD-INVERSE-PERP",
            settlement_currency=" btc ",
            collateral_currency="BTC",
            multiplier="1",
            payoff="INVERSE",
            face_currency=" usd ",
            price_quote_currency="USD",
            price_base_currency=" btc ",
        )
        self.assertEqual(contract.settlement_currency, "BTC")
        self.assertEqual(contract.face_currency, "USD")
        self.assertEqual(
            inverse_perpetual_pnl_exact(
                contract=contract,
                signed_contracts="100",
                entry_price="10000",
                exit_price="11000",
            ),
            Fraction(1, 1100),
        )

        ledger = FundingLedger()
        ledger.apply(
            event_id="funding-1",
            funding_period_id="2026-09-24T12:00Z",
            instrument_id="BTC-PERP",
            currency=" usdt ",
            amount="-1.25",
        )
        self.assertEqual(ledger.balance("USDT"), Decimal("-1.25"))
        self.assertEqual(ledger.balance(" usdt "), Decimal("-1.25"))

    def test_collateral_quote_currency_aliases_do_not_create_fake_conversion(self):
        with self.assertRaisesRegex(PerpetualError, "currencies must differ"):
            CollateralQuote(
                from_currency=" btc ",
                to_currency="BTC",
                rate="1",
                observed_at=NOW,
                max_age=timedelta(seconds=5),
            )


    def test_binary_float_inputs_are_rejected(self):
        with self.assertRaises(PerpetualError):
            linear_notional(signed_contracts=1.0, multiplier="0.001", price="100000")


    def test_same_funding_period_with_different_event_id_is_not_double_counted(self):
        ledger = FundingLedger()
        first = ledger.apply(
            event_id="provider-event-a",
            funding_period_id="2026-09-24T12:00Z",
            instrument_id="BTC-PERP",
            currency="USDT",
            amount="-1.25",
        )
        duplicate = ledger.apply(
            event_id="provider-event-b",
            funding_period_id="2026-09-24T12:00Z",
            instrument_id="BTC-PERP",
            currency="USDT",
            amount="-1.25",
        )
        self.assertEqual(first, Decimal("-1.25"))
        self.assertEqual(duplicate, Decimal("-1.25"))
        self.assertEqual(ledger.balance("USDT"), Decimal("-1.25"))

    def test_same_funding_period_with_changed_amount_conflicts(self):
        ledger = FundingLedger()
        ledger.apply(
            event_id="provider-event-a",
            funding_period_id="2026-09-24T12:00Z",
            instrument_id="BTC-PERP",
            currency="USDT",
            amount="-1.25",
        )
        with self.assertRaisesRegex(
            PerpetualError, "funding period was reused"
        ):
            ledger.apply(
                event_id="provider-event-b",
                funding_period_id="2026-09-24T12:00Z",
                instrument_id="BTC-PERP",
                currency="USDT",
                amount="-1.30",
            )


if __name__ == "__main__":
    unittest.main()
