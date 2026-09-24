from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest

from mvp.autotrade_mvp.perpetuals import (
    CollateralQuote,
    FundingConvention,
    FundingLedger,
    MarginSnapshot,
    MarketSnapshot,
    PerpetualContract,
    PerpetualError,
    funding_cashflow,
    linear_notional,
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

    def test_stale_or_future_market_state_fails_closed(self):
        with self.assertRaises(PerpetualError):
            self.market(age_seconds=6).require_valid(NOW)
        with self.assertRaises(PerpetualError):
            self.market().require_valid(NOW - timedelta(seconds=1))

    def test_excessive_mark_index_deviation_fails_closed(self):
        with self.assertRaises(PerpetualError):
            self.market(mark="102000", index="100000").require_valid(NOW)

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
            ledger.apply(event_id="f1", instrument_id="BTC-PERP", currency="USDT", amount="-1.25"),
            Decimal("-1.25"),
        )
        self.assertEqual(
            ledger.apply(event_id="f1", instrument_id="BTC-PERP", currency="USDT", amount="-1.25"),
            Decimal("-1.25"),
        )
        self.assertEqual(ledger.balance("USDT"), Decimal("-1.25"))
        with self.assertRaises(PerpetualError):
            ledger.apply(event_id="f1", instrument_id="BTC-PERP", currency="USDT", amount="-1.30")

    def test_binary_float_inputs_are_rejected(self):
        with self.assertRaises(PerpetualError):
            linear_notional(signed_contracts=1.0, multiplier="0.001", price="100000")


if __name__ == "__main__":
    unittest.main()
