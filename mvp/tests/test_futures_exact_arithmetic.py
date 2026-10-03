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

    def test_vm_containers_reject_polymorphic_retained_authorities(self):
        contract_calls = []

        class HostileContract(FuturesContract):
            def __getattribute__(self, name):
                if name in {"payoff", "multiplier", "canonical_instrument"}:
                    contract_calls.append(name)
                return super().__getattribute__(name)

        base = self._contract()
        hostile_contract = HostileContract(
            instrument=base.instrument,
            payoff=base.payoff,
            multiplier=base.multiplier,
            quote_currency=base.quote_currency,
            settlement_currency=base.settlement_currency,
            last_trade_at=base.last_trade_at,
            delivery_cutoff=base.delivery_cutoff,
            expiry=base.expiry,
            settlement_method=base.settlement_method,
            price_base_currency=base.price_base_currency,
            canonical_instrument=base.canonical_instrument,
        )
        contract_calls.clear()
        with self.assertRaisesRegex(FuturesError, "exact FuturesContract"):
            VariationMarginState(
                contract=hostile_contract,
                signed_contracts=Decimal("1"),
                last_settlement_price=Decimal("1"),
                settlement_scope=self._scope(),
            )
        self.assertEqual(contract_calls, [])

        scope_calls = []

        class HostileScope(FuturesSettlementScope):
            def __getattribute__(self, name):
                if name in {"source_id", "provider_id", "account_id", "environment"}:
                    scope_calls.append(name)
                return super().__getattribute__(name)

        scope = self._scope()
        hostile_scope = HostileScope(
            source_id=scope.source_id,
            provider_id=scope.provider_id,
            account_id=scope.account_id,
            environment=scope.environment,
        )
        scope_calls.clear()
        version = base.canonical_instrument
        assert version is not None
        with self.assertRaisesRegex(FuturesError, "exact FuturesSettlementScope"):
            FuturesSettlementEvidence(
                settlement_id="hostile-scope",
                observation_id="hostile-scope:r0",
                instrument_id=version.instrument_id,
                instrument_version=version.version,
                scope=hostile_scope,
                effective_at=utc(25, 1),
                sequence=1,
                revision=0,
                settlement_price=Decimal("2"),
                price_currency="USD",
                settlement_currency="USD",
            )
        self.assertEqual(scope_calls, [])

    def test_vm_authority_rejects_polymorphic_state_and_settlement_before_dispatch(self):
        contract = self._contract()
        settlement = self._settlement(
            contract,
            settlement_id="authority-vm",
            price="2",
        )
        state_calls = []

        class HostileState(VariationMarginState):
            def __getattribute__(self, name):
                if name in {
                    "contract",
                    "settlement_scope",
                    "settlement_history",
                    "signed_contracts",
                    "last_settlement_price",
                    "cumulative_variation_margin",
                }:
                    state_calls.append(name)
                return super().__getattribute__(name)

        hostile_state = HostileState(
            contract=contract,
            signed_contracts=Decimal("1"),
            last_settlement_price=Decimal("1"),
            settlement_scope=self._scope(),
        )
        state_calls.clear()
        with self.assertRaisesRegex(FuturesError, "linear variation-margin state"):
            apply_variation_margin(hostile_state, settlement)
        self.assertEqual(state_calls, [])

        evidence_calls = []

        class HostileSettlement(FuturesSettlementEvidence):
            def __getattribute__(self, name):
                if name in {
                    "settlement_id",
                    "observation_id",
                    "instrument_id",
                    "instrument_version",
                    "scope",
                    "settlement_price",
                }:
                    evidence_calls.append(name)
                return super().__getattribute__(name)

        hostile_settlement = HostileSettlement(
            settlement_id=settlement.settlement_id,
            observation_id=settlement.observation_id,
            instrument_id=settlement.instrument_id,
            instrument_version=settlement.instrument_version,
            scope=settlement.scope,
            effective_at=settlement.effective_at,
            sequence=settlement.sequence,
            revision=settlement.revision,
            settlement_price=settlement.settlement_price,
            price_currency=settlement.price_currency,
            settlement_currency=settlement.settlement_currency,
        )
        evidence_calls.clear()
        opening = VariationMarginState(
            contract=contract,
            signed_contracts=Decimal("1"),
            last_settlement_price=Decimal("1"),
            settlement_scope=self._scope(),
        )
        with self.assertRaisesRegex(FuturesError, "immutable FuturesSettlementEvidence"):
            apply_variation_margin(opening, hostile_settlement)
        self.assertEqual(evidence_calls, [])

    def test_settlement_rounding_policy_rejects_polymorphic_string(self):
        calls = []

        class HostileRounding(str):
            def __hash__(self):
                calls.append("__hash__")
                return super().__hash__()

            def __eq__(self, other):
                calls.append("__eq__")
                return True

        with self.assertRaisesRegex(FuturesError, "unsupported rounding policy"):
            settle_fraction(
                Fraction(3, 2),
                quantum=Decimal("1"),
                rounding=HostileRounding("DOWN"),
            )
        self.assertEqual(calls, [])

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


    def test_canonical_instrument_subclass_is_rejected_before_metadata_dispatch(self):
        base_contract = self._contract()
        version = base_contract.canonical_instrument
        assert version is not None
        calls = []

        class HostileInstrumentVersion(InstrumentVersion):
            def __getattribute__(self, name):
                if name in {
                    "asset_class",
                    "payoff",
                    "instrument_id",
                    "version",
                    "contract_multiplier",
                    "quote_currency",
                    "settlement_currency",
                    "expiry",
                    "last_trade_at",
                    "delivery_cutoff",
                    "settlement_method",
                    "base_currency",
                }:
                    calls.append(name)
                return super().__getattribute__(name)

        hostile = HostileInstrumentVersion(**version.__dict__)
        calls.clear()

        with self.assertRaisesRegex(
            FuturesError,
            "exact canonical InstrumentVersion",
        ):
            FuturesContract.from_instrument_version(hostile)

        self.assertEqual(calls, [])

    def test_contract_text_subclass_is_rejected_before_string_dispatch(self):
        base = self._contract()
        calls = []

        class HostileText(str):
            def strip(self, *args, **kwargs):
                calls.append("strip")
                raise AssertionError("polymorphic strip must not run")

            def __hash__(self):
                calls.append("hash")
                raise AssertionError("polymorphic hash must not run")

            def __eq__(self, other):
                calls.append("eq")
                raise AssertionError("polymorphic comparison must not run")

        with self.assertRaisesRegex(FuturesError, "exact non-empty text"):
            FuturesContract(
                instrument=base.instrument,
                payoff=HostileText("LINEAR"),
                multiplier=base.multiplier,
                quote_currency=base.quote_currency,
                settlement_currency=base.settlement_currency,
                last_trade_at=base.last_trade_at,
                delivery_cutoff=base.delivery_cutoff,
                expiry=base.expiry,
                settlement_method=base.settlement_method,
            )

        self.assertEqual(calls, [])

    def test_contract_datetime_subclass_is_rejected_before_time_dispatch(self):
        base = self._contract()
        calls = []

        class HostileDatetime(datetime):
            def astimezone(self, *args, **kwargs):
                calls.append("astimezone")
                raise AssertionError("polymorphic astimezone must not run")

        hostile = HostileDatetime(
            2026,
            9,
            30,
            20,
            tzinfo=timezone.utc,
        )

        with self.assertRaisesRegex(
            FuturesError,
            "exact timezone-aware datetime",
        ):
            FuturesContract(
                instrument=base.instrument,
                payoff=base.payoff,
                multiplier=base.multiplier,
                quote_currency=base.quote_currency,
                settlement_currency=base.settlement_currency,
                last_trade_at=hostile,
                delivery_cutoff=base.delivery_cutoff,
                expiry=base.expiry,
                settlement_method=base.settlement_method,
            )

        self.assertEqual(calls, [])



if __name__ == "__main__":
    unittest.main()
