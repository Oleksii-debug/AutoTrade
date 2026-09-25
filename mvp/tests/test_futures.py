from datetime import datetime, timezone
from decimal import Decimal
from fractions import Fraction
import unittest

from mvp.autotrade_mvp.accounting import EconomicBook
from mvp.autotrade_mvp.futures import (
    FuturesContract,
    FuturesSettlementEvidence,
    FuturesSettlementScope,
    InverseVariationMarginState,
    FuturesError,
    VariationMarginState,
    apply_variation_margin,
    book_variation_margin,
    apply_inverse_variation_margin,
    inverse_futures_pnl_exact,
    lifecycle_gate,
    linear_futures_pnl,
    require_open_for_new_exposure,
    replay_inverse_variation_margin,
    replay_variation_margin,
    settle_fraction,
    settle_and_book_inverse_variation_margin,
    unrealized_inverse_after_variation,
    unrealized_after_variation,
)


def utc(day: int, hour: int = 0):
    return datetime(2026, 9, day, hour, tzinfo=timezone.utc)


class FuturesLifecycleTests(unittest.TestCase):
    def _linear_contract(self, settlement_method="CASH"):
        return FuturesContract(
            instrument="FUT:TEST:202609",
            payoff="LINEAR",
            multiplier=Decimal("10"),
            quote_currency="USD",
            settlement_currency="USD",
            last_trade_at=utc(30, 20),
            delivery_cutoff=utc(29, 12),
            expiry=utc(30, 21),
            settlement_method=settlement_method,
        )

    def _scope(self):
        return FuturesSettlementScope(
            source_id="clearing:settlements",
            provider_id="TEST_CLEARER",
            account_id="acct-1",
            environment="PAPER",
        )

    def _settlement(
        self,
        contract,
        settlement_id,
        price,
        *,
        effective_at=None,
        sequence=1,
        revision=0,
        scope=None,
    ):
        return FuturesSettlementEvidence(
            settlement_id=settlement_id,
            instrument=contract.instrument,
            scope=scope or self._scope(),
            effective_at=effective_at or utc(25),
            sequence=sequence,
            revision=revision,
            settlement_price=Decimal(str(price)),
            price_currency=contract.quote_currency,
            settlement_currency=contract.settlement_currency,
        )

    def test_linear_fixture_matches_canonical_example(self):
        pnl = linear_futures_pnl(
            signed_contracts=Decimal("2"),
            multiplier=Decimal("10"),
            entry_price=Decimal("100"),
            exit_price=Decimal("103"),
        )
        self.assertEqual(pnl, Decimal("60"))
        self.assertEqual(
            linear_futures_pnl(
                signed_contracts=Decimal("-2"),
                multiplier=Decimal("10"),
                entry_price=Decimal("100"),
                exit_price=Decimal("103"),
            ),
            Decimal("-60"),
        )

    def test_inverse_fixture_keeps_exact_rational_until_settlement(self):
        pnl = inverse_futures_pnl_exact(
            signed_contracts=100,
            contract_quote_value=1,
            entry_price=10000,
            exit_price=11000,
        )
        self.assertEqual(pnl, Fraction(1, 1100))
        self.assertEqual(
            settle_fraction(pnl, quantum=Decimal("0.00000001")),
            Decimal("0.00090909"),
        )

    def test_settlement_quantum_rounding_uses_actual_step_not_only_decimal_places(self):
        value = Fraction(13, 100)
        self.assertEqual(
            settle_fraction(value, quantum=Decimal("0.05"), rounding="HALF_EVEN"),
            Decimal("0.15"),
        )
        self.assertEqual(
            settle_fraction(value, quantum=Decimal("0.05"), rounding="DOWN"),
            Decimal("0.10"),
        )
        self.assertEqual(
            settle_fraction(-value, quantum=Decimal("0.05"), rounding="DOWN"),
            Decimal("-0.10"),
        )

    def test_half_even_quantum_tie_uses_even_multiple(self):
        self.assertEqual(
            settle_fraction(Fraction(1, 8), quantum=Decimal("0.05"), rounding="HALF_EVEN"),
            Decimal("0.10"),
        )
        self.assertEqual(
            settle_fraction(Fraction(7, 40), quantum=Decimal("0.05"), rounding="HALF_EVEN"),
            Decimal("0.20"),
        )

    def test_variation_margin_is_not_counted_again_as_unrealized(self):
        contract = self._linear_contract()
        state = VariationMarginState(
            contract=contract,
            signed_contracts=Decimal("2"),
            last_settlement_price=Decimal("100"),
            settlement_scope=self._scope(),
        )
        settlement = self._settlement(contract, "linear-s1", "103")
        settled, cash_flow = apply_variation_margin(state, settlement)
        self.assertEqual(cash_flow, Decimal("60"))
        self.assertEqual(settled.cumulative_variation_margin, Decimal("60"))
        self.assertEqual(unrealized_after_variation(settled, Decimal("104")), Decimal("20"))
        self.assertEqual(
            settled.cumulative_variation_margin
            + unrealized_after_variation(settled, Decimal("104")),
            Decimal("80"),
        )

    def test_inverse_contract_rejects_wrong_settlement_currency_dimension(self):
        with self.assertRaisesRegex(FuturesError, "settlement_currency must match"):
            FuturesContract(
                instrument="BTC-USD-INVERSE",
                payoff="INVERSE",
                multiplier=Decimal("1"),
                quote_currency="USD",
                settlement_currency="USDT",
                price_base_currency="BTC",
                last_trade_at=utc(30, 12),
                delivery_cutoff=utc(30, 20),
                expiry=utc(30, 23),
                settlement_method="CASH",
            )

    def test_inverse_variation_margin_remains_exact_until_settlement(self):
        contract = FuturesContract(
            instrument="BTC-USD-INVERSE",
            payoff="INVERSE",
            multiplier=Decimal("1"),
            quote_currency="USD",
            settlement_currency="BTC",
            price_base_currency="BTC",
            last_trade_at=utc(30, 20),
            delivery_cutoff=utc(30, 20),
            expiry=utc(30, 21),
            settlement_method="CASH",
        )
        state = InverseVariationMarginState(
            contract=contract,
            signed_contracts=Decimal("100"),
            last_settlement_price=Decimal("10000"),
            settlement_scope=self._scope(),
        )

        first_settlement = self._settlement(
            contract, "inverse-s1", "11000", sequence=1
        )
        after_first, first = apply_inverse_variation_margin(state, first_settlement)
        self.assertEqual(first, Fraction(1, 1100))
        self.assertEqual(after_first.cumulative_variation_margin, Fraction(1, 1100))

        second_settlement = self._settlement(
            contract, "inverse-s2", "12000", sequence=2
        )
        after_second, second = apply_inverse_variation_margin(
            after_first, second_settlement
        )
        direct = inverse_futures_pnl_exact(
            signed_contracts=100,
            contract_quote_value=1,
            entry_price=10000,
            exit_price=12000,
        )
        self.assertEqual(after_second.cumulative_variation_margin, direct)
        self.assertEqual(
            after_second.cumulative_variation_margin,
            first + second,
        )
        self.assertEqual(
            settle_fraction(
                after_second.cumulative_variation_margin,
                quantum=Decimal("0.00000001"),
            ),
            settle_fraction(direct, quantum=Decimal("0.00000001")),
        )

    def test_inverse_settlement_rounds_once_then_books_settlement_currency(self):
        contract = FuturesContract(
            instrument="BTC-USD-INVERSE",
            payoff="INVERSE",
            multiplier=Decimal("1"),
            quote_currency="USD",
            settlement_currency="BTC",
            price_base_currency="BTC",
            last_trade_at=utc(30, 20),
            delivery_cutoff=utc(30, 20),
            expiry=utc(30, 21),
            settlement_method="CASH",
        )
        exact = Fraction(1, 1100)
        settlement = self._settlement(contract, "inverse-book-1", "11000")
        settled, transaction = settle_and_book_inverse_variation_margin(
            settlement=settlement,
            contract=contract,
            exact_amount=exact,
            settlement_quantum=Decimal("0.00000001"),
        )
        self.assertEqual(settled, Decimal("0.00090909"))
        self.assertIsNotNone(transaction)
        book = EconomicBook([transaction])
        self.assertEqual(book.cash("BTC"), Decimal("0.00090909"))
        self.assertEqual(
            book.balance("FUTURES_VARIATION_PNL:BTC", "BTC"),
            Decimal("-0.00090909"),
        )

    def test_sub_quantum_inverse_settlement_does_not_create_zero_journal_entry(self):
        contract = FuturesContract(
            instrument="BTC-USD-INVERSE",
            payoff="INVERSE",
            multiplier=Decimal("1"),
            quote_currency="USD",
            settlement_currency="BTC",
            price_base_currency="BTC",
            last_trade_at=utc(30, 20),
            delivery_cutoff=utc(30, 20),
            expiry=utc(30, 21),
            settlement_method="CASH",
        )
        settlement = self._settlement(contract, "inverse-book-small", "11000")
        settled, transaction = settle_and_book_inverse_variation_margin(
            settlement=settlement,
            contract=contract,
            exact_amount=Fraction(1, 1_000_000_000),
            settlement_quantum=Decimal("0.00000001"),
        )
        self.assertEqual(settled, Decimal("0.00000000"))
        self.assertIsNone(transaction)

    def test_inverse_unrealized_marks_only_since_last_settlement(self):
        contract = FuturesContract(
            instrument="BTC-USD-INVERSE",
            payoff="INVERSE",
            multiplier=Decimal("1"),
            quote_currency="USD",
            settlement_currency="BTC",
            price_base_currency="BTC",
            last_trade_at=utc(30, 20),
            delivery_cutoff=utc(30, 20),
            expiry=utc(30, 21),
            settlement_method="CASH",
        )
        state = InverseVariationMarginState(
            contract=contract,
            signed_contracts=Decimal("100"),
            last_settlement_price=Decimal("10000"),
            settlement_scope=self._scope(),
        )
        settlement = self._settlement(contract, "inverse-mark-1", "11000")
        settled, _ = apply_inverse_variation_margin(state, settlement)
        unrealized = unrealized_inverse_after_variation(settled, "12000")
        self.assertEqual(
            unrealized,
            inverse_futures_pnl_exact(
                signed_contracts=100,
                contract_quote_value=1,
                entry_price=11000,
                exit_price=12000,
            ),
        )

    def test_inverse_state_rejects_linear_contract_and_decimalized_accumulator(self):
        with self.assertRaisesRegex(FuturesError, "requires INVERSE"):
            InverseVariationMarginState(
                contract=self._linear_contract(),
                signed_contracts=Decimal("1"),
                last_settlement_price=Decimal("100"),
                settlement_scope=self._scope(),
            )

        inverse = FuturesContract(
            instrument="BTC-USD-INVERSE",
            payoff="INVERSE",
            multiplier=Decimal("1"),
            quote_currency="USD",
            settlement_currency="BTC",
            price_base_currency="BTC",
            last_trade_at=utc(30, 20),
            delivery_cutoff=utc(30, 20),
            expiry=utc(30, 21),
            settlement_method="CASH",
        )
        with self.assertRaisesRegex(FuturesError, "exact Fraction"):
            InverseVariationMarginState(
                contract=inverse,
                signed_contracts=Decimal("1"),
                last_settlement_price=Decimal("10000"),
                settlement_scope=self._scope(),
                cumulative_variation_margin=Decimal("0"),
            )

    def test_variation_margin_books_balanced_cash_and_pnl(self):
        contract = self._linear_contract()
        settlement = self._settlement(contract, "linear-book-1", "103")
        transaction = book_variation_margin(
            settlement=settlement,
            amount=Decimal("60"),
        )
        book = EconomicBook([transaction])
        self.assertEqual(book.cash("USD"), Decimal("60"))
        self.assertEqual(
            book.balance("FUTURES_VARIATION_PNL:USD", "USD"),
            Decimal("-60"),
        )

    def test_linear_settlement_duplicate_is_idempotent_and_out_of_order_fails_closed(self):
        contract = self._linear_contract()
        opening = VariationMarginState(
            contract=contract,
            signed_contracts=Decimal("2"),
            last_settlement_price=Decimal("100"),
            settlement_scope=self._scope(),
        )
        s2 = self._settlement(
            contract, "S2", "105", effective_at=utc(25), sequence=2
        )
        after_s2, first = apply_variation_margin(opening, s2)
        self.assertEqual(first, Decimal("100"))

        duplicate_state, duplicate_amount = apply_variation_margin(after_s2, s2)
        self.assertIs(duplicate_state, after_s2)
        self.assertEqual(duplicate_amount, Decimal("0"))

        delayed = self._settlement(
            contract, "S1", "103", effective_at=utc(24), sequence=1
        )
        with self.assertRaisesRegex(FuturesError, "out-of-order"):
            apply_variation_margin(after_s2, delayed)
        self.assertEqual(after_s2.last_settlement_price, Decimal("105"))
        self.assertEqual(after_s2.cumulative_variation_margin, Decimal("100"))

    def test_same_settlement_identity_with_changed_economics_conflicts(self):
        contract = self._linear_contract()
        opening = VariationMarginState(
            contract=contract,
            signed_contracts=Decimal("1"),
            last_settlement_price=Decimal("100"),
            settlement_scope=self._scope(),
        )
        accepted = self._settlement(contract, "same-id", "105", sequence=1)
        state, _ = apply_variation_margin(opening, accepted)
        conflicting = self._settlement(
            contract, "same-id", "106", sequence=1, revision=1
        )
        with self.assertRaisesRegex(FuturesError, "conflicts"):
            apply_variation_margin(state, conflicting)

    def test_equal_effective_time_uses_immutable_sequence_and_rejects_collisions(self):
        contract = self._linear_contract()
        opening = VariationMarginState(
            contract=contract,
            signed_contracts=Decimal("1"),
            last_settlement_price=Decimal("100"),
            settlement_scope=self._scope(),
        )
        first = self._settlement(
            contract, "same-time-1", "101", effective_at=utc(25), sequence=10
        )
        state, _ = apply_variation_margin(opening, first)
        second = self._settlement(
            contract, "same-time-2", "102", effective_at=utc(25), sequence=11
        )
        state, _ = apply_variation_margin(state, second)
        collision = self._settlement(
            contract, "same-time-collision", "103", effective_at=utc(25), sequence=11
        )
        with self.assertRaisesRegex(FuturesError, "out-of-order"):
            apply_variation_margin(state, collision)

    def test_scope_and_instrument_mismatch_fail_before_economic_mutation(self):
        contract = self._linear_contract()
        opening = VariationMarginState(
            contract=contract,
            signed_contracts=Decimal("1"),
            last_settlement_price=Decimal("100"),
            settlement_scope=self._scope(),
        )
        wrong_scope = FuturesSettlementScope(
            source_id="clearing:settlements",
            provider_id="TEST_CLEARER",
            account_id="other-account",
            environment="PAPER",
        )
        mismatched_scope = self._settlement(
            contract, "wrong-scope", "105", scope=wrong_scope
        )
        with self.assertRaisesRegex(FuturesError, "scope mismatch"):
            apply_variation_margin(opening, mismatched_scope)

        wrong_instrument = FuturesSettlementEvidence(
            settlement_id="wrong-instrument",
            instrument="FUT:OTHER:202609",
            scope=self._scope(),
            effective_at=utc(25),
            sequence=1,
            revision=0,
            settlement_price=Decimal("105"),
            price_currency="USD",
            settlement_currency="USD",
        )
        with self.assertRaisesRegex(FuturesError, "instrument/version"):
            apply_variation_margin(opening, wrong_instrument)
        self.assertEqual(opening.cumulative_variation_margin, Decimal("0"))

    def test_linear_replay_reconstructs_identical_state_and_duplicate_after_restart_is_noop(self):
        contract = self._linear_contract()
        opening = VariationMarginState(
            contract=contract,
            signed_contracts=Decimal("2"),
            last_settlement_price=Decimal("100"),
            settlement_scope=self._scope(),
        )
        settlements = (
            self._settlement(contract, "replay-1", "105", sequence=1),
            self._settlement(contract, "replay-2", "110", sequence=2),
        )
        live = opening
        for settlement in settlements:
            live, _ = apply_variation_margin(live, settlement)
        rebuilt = replay_variation_margin(opening, settlements)
        self.assertEqual(rebuilt, live)
        retried, amount = apply_variation_margin(rebuilt, settlements[-1])
        self.assertEqual(retried, rebuilt)
        self.assertEqual(amount, Decimal("0"))
        self.assertEqual(unrealized_after_variation(rebuilt, "111"), Decimal("20"))

    def test_inverse_duplicate_out_of_order_and_replay_remain_exact(self):
        contract = FuturesContract(
            instrument="BTC-USD-INVERSE",
            payoff="INVERSE",
            multiplier=Decimal("1"),
            quote_currency="USD",
            settlement_currency="BTC",
            price_base_currency="BTC",
            last_trade_at=utc(30, 20),
            delivery_cutoff=utc(30, 20),
            expiry=utc(30, 21),
            settlement_method="CASH",
        )
        opening = InverseVariationMarginState(
            contract=contract,
            signed_contracts=Decimal("100"),
            last_settlement_price=Decimal("10000"),
            settlement_scope=self._scope(),
        )
        s1 = self._settlement(contract, "inverse-replay-1", "11000", sequence=1)
        s2 = self._settlement(contract, "inverse-replay-2", "12000", sequence=2)
        state, first = apply_inverse_variation_margin(opening, s1)
        duplicate, duplicate_amount = apply_inverse_variation_margin(state, s1)
        self.assertEqual(duplicate, state)
        self.assertEqual(duplicate_amount, Fraction(0, 1))
        state, second = apply_inverse_variation_margin(state, s2)
        self.assertEqual(
            state.cumulative_variation_margin,
            first + second,
        )
        rebuilt = replay_inverse_variation_margin(opening, (s1, s2))
        self.assertEqual(rebuilt, state)
        delayed = self._settlement(
            contract,
            "inverse-old",
            "10500",
            effective_at=utc(24),
            sequence=0,
        )
        with self.assertRaisesRegex(FuturesError, "out-of-order"):
            apply_inverse_variation_margin(state, delayed)

    def test_settlement_journal_identity_is_deterministic_and_retry_safe(self):
        contract = self._linear_contract()
        settlement = self._settlement(contract, "journal-s1", "105", sequence=1)
        first = book_variation_margin(settlement=settlement, amount=Decimal("50"))
        retry = book_variation_margin(settlement=settlement, amount=Decimal("50"))
        self.assertEqual(first.transaction_id, retry.transaction_id)
        self.assertEqual(first.cause_event_id, retry.cause_event_id)
        self.assertIn("sha256:", first.transaction_id)

    def test_physical_delivery_is_fail_closed_without_explicit_authority(self):
        contract = self._linear_contract(settlement_method="PHYSICAL")
        self.assertEqual(lifecycle_gate(contract, utc(29, 11)), "OPEN")
        self.assertEqual(lifecycle_gate(contract, utc(29, 12)), "DELIVERY_BLOCKED")
        with self.assertRaises(FuturesError):
            require_open_for_new_exposure(contract, utc(29, 12))
        self.assertEqual(
            lifecycle_gate(contract, utc(29, 12), physical_delivery_authorized=True),
            "OPEN",
        )

    def test_last_trade_and_expiry_are_hard_gates(self):
        contract = self._linear_contract()
        self.assertEqual(lifecycle_gate(contract, utc(30, 20)), "TRADING_ENDED")
        self.assertEqual(lifecycle_gate(contract, utc(30, 21)), "EXPIRED")
        with self.assertRaises(FuturesError):
            require_open_for_new_exposure(contract, utc(30, 20))

    def test_float_inputs_are_rejected(self):
        with self.assertRaises(FuturesError):
            linear_futures_pnl(
                signed_contracts=1,
                multiplier=10,
                entry_price=100.0,
                exit_price=101,
            )

    def test_physical_delivery_authority_is_strict_boolean(self):
        contract = self._linear_contract(settlement_method="PHYSICAL")
        for unsafe in (1, "yes", object()):
            with self.subTest(unsafe=unsafe):
                with self.assertRaisesRegex(
                    FuturesError, "physical_delivery_authorized must be boolean"
                ):
                    lifecycle_gate(
                        contract,
                        utc(29, 12),
                        physical_delivery_authorized=unsafe,
                    )

    def test_physical_delivery_cutoff_cannot_follow_last_trade(self):
        with self.assertRaisesRegex(
            FuturesError, "delivery_cutoff cannot be after last_trade_at"
        ):
            FuturesContract(
                instrument="physical-bad-cutoff",
                payoff="LINEAR",
                multiplier=Decimal("1"),
                quote_currency="USD",
                settlement_currency="USD",
                last_trade_at=utc(29, 12),
                delivery_cutoff=utc(30, 12),
                expiry=utc(30, 21),
                settlement_method="PHYSICAL",
            )

    def test_invalid_contract_time_order_is_rejected(self):
        with self.assertRaises(FuturesError):
            FuturesContract(
                instrument="bad",
                payoff="LINEAR",
                multiplier=Decimal("1"),
                quote_currency="USD",
                settlement_currency="USD",
                last_trade_at=utc(30, 22),
                delivery_cutoff=utc(29),
                expiry=utc(30, 21),
                settlement_method="CASH",
            )


if __name__ == "__main__":
    unittest.main()
