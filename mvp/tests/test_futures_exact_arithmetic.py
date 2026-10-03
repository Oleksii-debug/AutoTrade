from datetime import datetime, timezone
from decimal import (
    Decimal,
    ROUND_CEILING,
    ROUND_DOWN,
    ROUND_FLOOR,
    ROUND_HALF_EVEN,
    ROUND_UP,
    localcontext,
)
from fractions import Fraction
import unittest

from mvp.autotrade_mvp.futures import (
    FuturesContract,
    FuturesError,
    FuturesSettlementEvidence,
    FuturesSettlementScope,
    VariationMarginState,
    apply_variation_margin,
    book_variation_margin,
    linear_futures_pnl,
    replay_variation_margin,
    settle_fraction,
    settlement_identity_digest,
)
from mvp.autotrade_mvp.exact_decimal import MAX_SIGNIFICANT_DIGITS
from mvp.autotrade_mvp.instruments import InstrumentVersion


def utc(day: int, hour: int = 0) -> datetime:
    return datetime(2026, 9, day, hour, tzinfo=timezone.utc)


class FuturesExactArithmeticTests(unittest.TestCase):
    def _contract(self, *, multiplier: str = "1") -> FuturesContract:
        version = InstrumentVersion(
            instrument_id="11111111-1111-4111-8111-111111111111",
            version=1,
            provider_id="TEST_CLEARER",
            venue_id="TEST_VENUE",
            provider_symbol="FUT-EXACT-202609",
            asset_class="FUTURE",
            base_currency="TEST",
            quote_currency="USD",
            settlement_currency="USD",
            quantity_unit="CONTRACT",
            contract_multiplier=Decimal(multiplier),
            price_tick=Decimal("0.000000000000000000000000000001"),
            quantity_step=Decimal("1"),
            minimum_quantity=Decimal("1"),
            calendar_id="CONTINUOUS_24_7",
            timezone_id="UTC",
            effective_from=utc(1),
            payoff="LINEAR",
            underlying_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa@1",
            expiry=utc(30, 21),
            last_trade_at=utc(30, 20),
            delivery_cutoff=utc(30, 20),
            settlement_method="CASH",
            margin_model_id="TEST_FUTURES_MARGIN_V1",
        )
        return FuturesContract.from_instrument_version(version)

    def _scope(self) -> FuturesSettlementScope:
        return FuturesSettlementScope(
            source_id="clearing:settlements",
            provider_id="TEST_CLEARER",
            account_id="acct-1",
            environment="PAPER",
        )

    def _settlement(
        self,
        contract: FuturesContract,
        *,
        settlement_id: str,
        price: str,
        sequence: int = 1,
    ) -> FuturesSettlementEvidence:
        version = contract.canonical_instrument
        assert version is not None
        return FuturesSettlementEvidence(
            settlement_id=settlement_id,
            observation_id=f"{settlement_id}:r0",
            instrument_id=version.instrument_id,
            instrument_version=version.version,
            scope=self._scope(),
            effective_at=utc(25, sequence),
            sequence=sequence,
            revision=0,
            settlement_price=Decimal(price),
            price_currency="USD",
            settlement_currency="USD",
        )

    def test_linear_vm_and_cumulative_state_ignore_ambient_decimal_context(self):
        contract = self._contract(
            multiplier="0.000000000000000000000000000001"
        )
        settlement = self._settlement(
            contract,
            settlement_id="tiny-vm",
            price="2",
        )
        expected_amount = Decimal("0.000000000000000000000000000001")
        expected_cumulative = Decimal(
            "1000000000000000000000000000000."
            "000000000000000000000000000001"
        )

        results = []
        for precision, rounding in (
            (6, ROUND_FLOOR),
            (10, ROUND_CEILING),
            (28, ROUND_DOWN),
            (80, ROUND_UP),
            (80, ROUND_HALF_EVEN),
        ):
            with self.subTest(precision=precision, rounding=rounding):
                with localcontext() as context:
                    context.prec = precision
                    context.rounding = rounding
                    opening = VariationMarginState(
                        contract=contract,
                        signed_contracts=Decimal("1"),
                        last_settlement_price=Decimal("1"),
                        settlement_scope=self._scope(),
                        cumulative_variation_margin=Decimal(
                            "1000000000000000000000000000000"
                        ),
                    )
                    state, amount = apply_variation_margin(opening, settlement)
                    self.assertEqual(amount, expected_amount)
                    self.assertEqual(
                        state.cumulative_variation_margin,
                        expected_cumulative,
                    )
                    replayed = replay_variation_margin(
                        opening,
                        (settlement,),
                    )
                    self.assertEqual(replayed, state)
                    duplicate, duplicate_amount = apply_variation_margin(
                        state,
                        settlement,
                    )
                    self.assertIs(duplicate, state)
                    self.assertEqual(duplicate_amount, Decimal("0"))
                    results.append(state)

        self.assertTrue(all(state == results[0] for state in results))

    def test_high_significance_linear_pnl_is_one_exact_value_in_all_contexts(self):
        expected = Decimal(
            "1524157875323883675.019051998750190521"
        )
        actual = []
        for precision, rounding in (
            (6, ROUND_FLOOR),
            (10, ROUND_CEILING),
            (28, ROUND_DOWN),
            (80, ROUND_UP),
            (80, ROUND_HALF_EVEN),
        ):
            with localcontext() as context:
                context.prec = precision
                context.rounding = rounding
                actual.append(
                    linear_futures_pnl(
                        signed_contracts=Decimal(
                            "1234567890123456789"
                        ),
                        multiplier=Decimal("1.234567890123456789"),
                        entry_price=Decimal("1000000.000000000001"),
                        exit_price=Decimal("1000001.000000000001"),
                    )
                )
        self.assertEqual(actual, [expected] * len(actual))

    def test_exact_resource_failure_cannot_advance_linear_vm_state(self):
        contract = self._contract(multiplier="1")
        opening = VariationMarginState(
            contract=contract,
            signed_contracts=Decimal("1"),
            last_settlement_price=Decimal("1"),
            settlement_scope=self._scope(),
            cumulative_variation_margin=Decimal("9" * 256),
        )
        settlement = self._settlement(
            contract,
            settlement_id="overflow-vm",
            price="2",
        )

        with self.assertRaisesRegex(
            FuturesError,
            "shared resource envelope",
        ):
            apply_variation_margin(opening, settlement)

        self.assertEqual(opening.last_settlement_price, Decimal("1"))
        self.assertEqual(opening.settlement_history, ())
        self.assertEqual(
            opening.cumulative_variation_margin,
            Decimal("9" * 256),
        )

    def test_vm_journal_balancing_uses_exact_opposite(self):
        contract = self._contract()
        settlement = self._settlement(
            contract,
            settlement_id="journal-vm",
            price="2",
        )
        amount = Decimal(
            "1234567890123456789012345678.000000000000000000000000001"
        )
        exact_opposite = Decimal(
            "-1234567890123456789012345678.000000000000000000000000001"
        )
        for precision, rounding in (
            (6, ROUND_FLOOR),
            (10, ROUND_CEILING),
            (28, ROUND_DOWN),
            (80, ROUND_UP),
        ):
            with localcontext() as context:
                context.prec = precision
                context.rounding = rounding
                transaction = book_variation_margin(
                    settlement=settlement,
                    amount=amount,
                )
                self.assertEqual(transaction.postings[0].amount, amount)
                self.assertEqual(
                    transaction.postings[1].amount,
                    exact_opposite,
                )

    def test_settlement_quantum_final_multiplication_is_context_independent(self):
        exact = Fraction(123456789012345678901234567890123, 10**30)
        expected = Decimal("123.456789012345678901234567890123")
        outputs = []
        for precision, rounding in (
            (6, ROUND_FLOOR),
            (10, ROUND_CEILING),
            (28, ROUND_DOWN),
            (80, ROUND_UP),
            (80, ROUND_HALF_EVEN),
        ):
            with localcontext() as context:
                context.prec = precision
                context.rounding = rounding
                outputs.append(
                    settle_fraction(
                        exact,
                        quantum=Decimal("0.000000000000000000000000000001"),
                        rounding="DOWN",
                    )
                )
        self.assertEqual(outputs, [expected] * len(outputs))

    def test_arithmetic_repair_does_not_change_settlement_identity(self):
        contract = self._contract()
        settlement = self._settlement(
            contract,
            settlement_id="identity-vm",
            price="1234567890123456789012345678.1",
        )
        identities = set()
        for precision, rounding in (
            (6, ROUND_FLOOR),
            (10, ROUND_CEILING),
            (28, ROUND_DOWN),
            (80, ROUND_UP),
        ):
            with localcontext() as context:
                context.prec = precision
                context.rounding = rounding
                identities.add(settlement_identity_digest(settlement))
        self.assertEqual(len(identities), 1)


    def test_financial_decimal_ingress_rejects_polymorphic_decimal_before_dispatch(self):
        callbacks = []

        class HostileDecimal(Decimal):
            def is_finite(self):
                callbacks.append("is_finite")
                raise AssertionError("virtual decimal method executed")

            def as_tuple(self):
                callbacks.append("as_tuple")
                raise AssertionError("virtual decimal method executed")

        with self.assertRaisesRegex(
            FuturesError,
            "bounded finite exact decimal",
        ):
            linear_futures_pnl(
                signed_contracts=HostileDecimal("1"),
                multiplier=Decimal("1"),
                entry_price=Decimal("100"),
                exit_price=Decimal("101"),
            )
        self.assertEqual(callbacks, [])

    def test_authoritative_vm_state_rejects_oversized_decimal_at_construction(self):
        contract = self._contract()
        oversized = Decimal("9" * (MAX_SIGNIFICANT_DIGITS + 1))

        with self.assertRaisesRegex(
            FuturesError,
            "bounded finite exact decimal",
        ):
            VariationMarginState(
                contract=contract,
                signed_contracts=Decimal("1"),
                last_settlement_price=Decimal("1"),
                settlement_scope=self._scope(),
                cumulative_variation_margin=oversized,
            )


if __name__ == "__main__":
    unittest.main()
