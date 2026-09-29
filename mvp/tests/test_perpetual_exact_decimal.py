from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, localcontext
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
    inverse_stressed_loss_exact,
    linear_notional,
    require_liquidation_headroom,
    require_new_risk_capacity,
    stressed_loss,
)


NOW = datetime(2026, 9, 29, 4, 0, tzinfo=timezone.utc)


def linear_contract(multiplier="0.000000001"):
    return PerpetualContract(
        instrument_id="BTC-PERP",
        settlement_currency="USDT",
        collateral_currency="USDT",
        multiplier=multiplier,
        payoff="LINEAR",
    )


def market(mark="1000000000.000000001"):
    return MarketSnapshot(
        mark_price=mark,
        index_price=mark,
        observed_at=NOW,
        max_age=timedelta(seconds=5),
        max_mark_index_deviation="0.01",
    )


class PerpetualExactDecimalTests(unittest.TestCase):
    def test_linear_notional_funding_and_stress_ignore_ambient_context(self):
        observed = []
        contract = linear_contract()
        snapshot = market()
        for precision in (6, 10, 28, 80):
            for rounding in (ROUND_FLOOR, ROUND_CEILING):
                with localcontext() as context:
                    context.prec = precision
                    context.rounding = rounding
                    notional = linear_notional(
                        signed_contracts="123456789.000000001",
                        multiplier=contract.multiplier,
                        price=snapshot.mark_price,
                    )
                    currency, funding = funding_cashflow(
                        contract=contract,
                        signed_contracts="123456789.000000001",
                        funding_rate="0.000000001",
                        snapshot=snapshot,
                        convention=FundingConvention("LONG_PAYS", "MARK"),
                        at=NOW,
                    )
                    loss = stressed_loss(
                        contract=contract,
                        signed_contracts="123456789.000000001",
                        mark_price=snapshot.mark_price,
                        adverse_move_fraction="0.000000001",
                    )
                    observed.append((notional, currency, funding, loss))
        self.assertTrue(all(row == observed[0] for row in observed))
        self.assertEqual(observed[0][1], "USDT")

    def test_collateral_conversion_is_exact_at_low_precision(self):
        quote = CollateralQuote(
            from_currency="BTC",
            to_currency="USDT",
            rate="12345678901234567890.123456789",
            observed_at=NOW,
            max_age=timedelta(seconds=5),
        )
        expected = Decimal("12345678901.234567890123456789")
        for precision in (6, 10, 28, 80):
            with localcontext() as context:
                context.prec = precision
                self.assertEqual(quote.convert("0.000000001", NOW), expected)

    def test_margin_capacity_sum_is_exact_at_threshold(self):
        snapshot = market(mark="1")
        pass_margin = MarginSnapshot(
            equity="1000000000000000000000000000.4",
            maintenance_requirement="1000000000000000000000000000.1",
            observed_at=NOW,
            max_age=timedelta(seconds=5),
        )
        reject_margin = MarginSnapshot(
            equity="1000000000000000000000000000.4",
            maintenance_requirement="1000000000000000000000000000.1",
            observed_at=NOW,
            max_age=timedelta(seconds=5),
        )
        for precision in (6, 10, 28, 80):
            for rounding in (ROUND_FLOOR, ROUND_CEILING):
                with localcontext() as context:
                    context.prec = precision
                    context.rounding = rounding
                    require_new_risk_capacity(
                        margin=pass_margin,
                        market=snapshot,
                        stressed_position_loss="0.1",
                        reserve_buffer="0.1",
                        at=NOW,
                    )
                    with self.assertRaisesRegex(PerpetualError, "insufficient"):
                        require_new_risk_capacity(
                            margin=reject_margin,
                            market=snapshot,
                            stressed_position_loss="0.2",
                            reserve_buffer="0.1",
                            at=NOW,
                        )

    def test_funding_ledger_accumulation_is_exact(self):
        for precision in (6, 10, 28, 80):
            with self.subTest(precision=precision):
                ledger = FundingLedger()
                with localcontext() as context:
                    context.prec = precision
                    ledger.apply(
                        event_id="large",
                        funding_period_id="p1",
                        instrument_id="BTC-PERP",
                        currency="USDT",
                        amount="1234567890123456789012345678.1",
                    )
                    ledger.apply(
                        event_id="small",
                        funding_period_id="p2",
                        instrument_id="BTC-PERP",
                        currency="USDT",
                        amount="0.9",
                    )
                self.assertEqual(
                    ledger.balance("USDT"),
                    Decimal("1234567890123456789012345679"),
                )

    def test_inverse_stress_keeps_exact_rational_exit_price(self):
        contract = PerpetualContract(
            instrument_id="BTC-USD-INVERSE-PERP",
            settlement_currency="BTC",
            collateral_currency="BTC",
            multiplier="1",
            payoff="INVERSE",
            face_currency="USD",
            price_quote_currency="USD",
            price_base_currency="BTC",
        )
        observed = []
        for precision in (3, 6, 28, 80):
            with localcontext() as context:
                context.prec = precision
                observed.append(
                    inverse_stressed_loss_exact(
                        contract=contract,
                        signed_contracts="100",
                        mark_price="10000",
                        adverse_move_fraction="0.1",
                    )
                )
        self.assertTrue(all(value == observed[0] for value in observed))
        self.assertEqual(observed[0], Fraction(1, 900))

    def test_liquidation_headroom_returns_exact_fraction_under_hostile_contexts(self):
        snapshot = MarketSnapshot(
            mark_price="3",
            index_price="3",
            observed_at=NOW,
            max_age=timedelta(seconds=5),
            max_mark_index_deviation="0.01",
        )
        liquidation = LiquidationSnapshot(
            side="LONG",
            liquidation_price="2",
            tier_id="tier-1",
            evidence_ref="liq-evidence",
            observed_at=NOW,
            max_age=timedelta(seconds=5),
        )
        observed = []
        for precision in (3, 6, 28, 80):
            for rounding in (ROUND_FLOOR, ROUND_CEILING):
                with localcontext() as context:
                    context.prec = precision
                    context.rounding = rounding
                    observed.append(
                        require_liquidation_headroom(
                            liquidation=liquidation,
                            market=snapshot,
                            minimum_headroom_fraction="0.3",
                            at=NOW,
                        )
                    )
        self.assertTrue(all(value == Fraction(1, 3) for value in observed))

    def test_resource_envelope_failures_use_perpetual_error(self):
        oversized = "9" * 256
        with self.assertRaisesRegex(PerpetualError, "resource envelope"):
            linear_notional(
                signed_contracts=oversized,
                multiplier="10",
                price="1",
            )

    def test_funding_overflow_does_not_publish_partial_event_or_period(self):
        ledger = FundingLedger()
        boundary = "9" * 256
        ledger.apply(
            event_id="boundary",
            funding_period_id="p-boundary",
            instrument_id="BTC-PERP",
            currency="USDT",
            amount=boundary,
        )
        self.assertEqual(ledger.balance("USDT"), Decimal(boundary))

        with self.assertRaisesRegex(PerpetualError, "resource envelope"):
            ledger.apply(
                event_id="candidate",
                funding_period_id="p-candidate",
                instrument_id="BTC-PERP",
                currency="USDT",
                amount="1",
            )
        self.assertEqual(ledger.balance("USDT"), Decimal(boundary))

        applied = ledger.apply(
            event_id="candidate",
            funding_period_id="p-candidate",
            instrument_id="BTC-PERP",
            currency="USDT",
            amount="-1",
        )
        self.assertEqual(applied, Decimal("-1"))
        self.assertEqual(ledger.balance("USDT"), Decimal(boundary) - Decimal("1"))
        self.assertEqual(
            ledger.apply(
                event_id="candidate",
                funding_period_id="p-candidate",
                instrument_id="BTC-PERP",
                currency="USDT",
                amount="-1",
            ),
            Decimal("-1"),
        )

    def test_extreme_zero_exponent_is_canonicalized_before_fingerprint(self):
        ledger = FundingLedger()
        self.assertEqual(
            ledger.apply(
                event_id="zero",
                funding_period_id="p-zero",
                instrument_id="BTC-PERP",
                currency="USDT",
                amount=Decimal("0E-1000000"),
            ),
            Decimal("0"),
        )
        self.assertEqual(ledger.balance("USDT"), Decimal("0"))
        self.assertEqual(
            ledger.apply(
                event_id="zero",
                funding_period_id="p-zero",
                instrument_id="BTC-PERP",
                currency="USDT",
                amount=Decimal("0"),
            ),
            Decimal("0"),
        )


if __name__ == "__main__":
    unittest.main()
