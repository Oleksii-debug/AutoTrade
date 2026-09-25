from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest

from mvp.autotrade_mvp.accounting import EconomicBook
from mvp.autotrade_mvp.options import (
    DeliverableLeg,
    OptionContract,
    OptionError,
    book_cash_option_settlement,
    book_physical_option_settlement,
    exercise_gate,
    expiration_cash_settlement,
    expiration_pnl_after_premium,
    interim_multi_leg_reservation,
    intrinsic_value_per_unit,
    physical_exercise_obligation,
    require_holder_exercise_open,
    require_physical_resources,
    OptionRiskEvidence,
    OptionScenarioResult,
    require_current_option_risk,
)


def at(hour: int):
    return datetime(2026, 9, 25, hour, tzinfo=timezone.utc)


class OptionLifecycleTests(unittest.TestCase):
    def _cash_call(self):
        return OptionContract(
            instrument="OPT:CALL",
            right="CALL",
            strike=Decimal("100"),
            multiplier=Decimal("100"),
            settlement_currency="USD",
            settlement_method="CASH",
            exercise_style="EUROPEAN",
            exercise_cutoff=at(19),
            expiry=at(20),
            exercise_opens_at=at(17),
        )

    def _physical(self, right="CALL"):
        return OptionContract(
            instrument=f"OPT:{right}",
            right=right,
            strike=Decimal("50"),
            multiplier=Decimal("100"),
            settlement_currency="USD",
            settlement_method="PHYSICAL",
            exercise_style="AMERICAN",
            exercise_cutoff=at(19),
            expiry=at(20),
            exercise_opens_at=at(17),
            deliverable=(
                DeliverableLeg("SHARES:ADJUSTED", Decimal("150")),
                DeliverableLeg("CASHLIKE:MERGER_RIGHT", Decimal("2.5")),
            ),
            exercise_cash_per_contract=Decimal("5000"),
        )

    def test_call_and_put_intrinsic_values_are_exact(self):
        call = self._cash_call()
        self.assertEqual(intrinsic_value_per_unit(call, "112.5"), Decimal("12.5"))
        self.assertEqual(intrinsic_value_per_unit(call, "90"), Decimal("0"))

        put = OptionContract(
            instrument="OPT:PUT",
            right="PUT",
            strike=Decimal("100"),
            multiplier=Decimal("100"),
            settlement_currency="USD",
            settlement_method="CASH",
            exercise_style="EUROPEAN",
            exercise_cutoff=at(19),
            expiry=at(20),
        )
        self.assertEqual(intrinsic_value_per_unit(put, "80"), Decimal("20"))

    def test_cash_expiry_and_premium_pnl_use_signed_contracts(self):
        call = self._cash_call()
        self.assertEqual(
            expiration_cash_settlement(call, signed_contracts=2, underlying_price="110"),
            Decimal("2000"),
        )
        self.assertEqual(
            expiration_pnl_after_premium(
                call,
                signed_contracts=2,
                premium_per_unit="3",
                underlying_price="110",
            ),
            Decimal("1400"),
        )
        self.assertEqual(
            expiration_pnl_after_premium(
                call,
                signed_contracts=-2,
                premium_per_unit="3",
                underlying_price="110",
            ),
            Decimal("-1400"),
        )

    def test_adjusted_deliverable_rejects_duplicate_asset_identity(self):
        with self.assertRaisesRegex(OptionError, "asset_id values must be unique"):
            OptionContract(
                instrument="OPT:ADJUSTED",
                right="CALL",
                strike=Decimal("50"),
                multiplier=Decimal("100"),
                settlement_currency="USD",
                settlement_method="PHYSICAL",
                exercise_style="AMERICAN",
                expiry=at(20),
                exercise_cutoff=at(19),
                deliverable=(
                    DeliverableLeg("SHARES:ADJUSTED", Decimal("60")),
                    DeliverableLeg("SHARES:ADJUSTED", Decimal("40")),
                ),
                exercise_cash_per_contract=Decimal("5000"),
            )

    def test_adjusted_deliverable_requires_typed_legs(self):
        with self.assertRaisesRegex(OptionError, "DeliverableLeg"):
            OptionContract(
                instrument="OPT:ADJUSTED",
                right="CALL",
                strike=Decimal("50"),
                multiplier=Decimal("100"),
                settlement_currency="USD",
                settlement_method="PHYSICAL",
                exercise_style="AMERICAN",
                expiry=at(20),
                exercise_cutoff=at(19),
                deliverable=(("SHARES:ADJUSTED", Decimal("100")),),
                exercise_cash_per_contract=Decimal("5000"),
            )

    def test_adjusted_deliverable_never_assumes_one_hundred_shares(self):
        call = self._physical("CALL")
        obligation = physical_exercise_obligation(call, signed_contracts=2)
        self.assertEqual(
            obligation.asset_quantities,
            (
                ("SHARES:ADJUSTED", Decimal("300")),
                ("CASHLIKE:MERGER_RIGHT", Decimal("5.0")),
            ),
        )
        self.assertEqual(obligation.settlement_cash, Decimal("-10000"))

    def test_put_and_short_assignment_reverse_physical_direction(self):
        put = self._physical("PUT")
        long_put = physical_exercise_obligation(put, signed_contracts=1)
        self.assertEqual(long_put.asset_quantities[0][1], Decimal("-150"))
        self.assertEqual(long_put.settlement_cash, Decimal("5000"))

        short_put = physical_exercise_obligation(put, signed_contracts=-1)
        self.assertEqual(short_put.asset_quantities[0][1], Decimal("150"))
        self.assertEqual(short_put.settlement_cash, Decimal("-5000"))

    def test_physical_resource_check_fails_closed(self):
        put = self._physical("PUT")
        obligation = physical_exercise_obligation(put, signed_contracts=1)
        with self.assertRaises(OptionError):
            require_physical_resources(
                obligation,
                asset_balances={"SHARES:ADJUSTED": "100"},
                cash_balance="0",
            )
        require_physical_resources(
            obligation,
            asset_balances={
                "SHARES:ADJUSTED": "150",
                "CASHLIKE:MERGER_RIGHT": "2.5",
            },
            cash_balance="0",
        )

    def test_non_atomic_multileg_reserves_all_interim_leg_loss(self):
        self.assertEqual(
            interim_multi_leg_reservation(
                ["100", "60", "25"],
                atomic_package_guaranteed=False,
            ),
            Decimal("185"),
        )
        with self.assertRaises(OptionError):
            interim_multi_leg_reservation(
                ["100", "60"],
                atomic_package_guaranteed=False,
                package_worst_case_loss="20",
            )
        self.assertEqual(
            interim_multi_leg_reservation(
                ["100", "60"],
                atomic_package_guaranteed=True,
                package_worst_case_loss="20",
            ),
            Decimal("20"),
        )

    def test_exercise_cutoff_expiry_and_provider_window_are_hard_gates(self):
        contract = self._cash_call()
        self.assertEqual(exercise_gate(contract, at(16)), "EXERCISE_NOT_YET_OPEN")
        self.assertEqual(exercise_gate(contract, at(18)), "OPEN")
        self.assertEqual(exercise_gate(contract, at(19)), "EXERCISE_WINDOW_CLOSED")
        self.assertEqual(exercise_gate(contract, at(20)), "EXPIRED")
        with self.assertRaises(OptionError):
            require_holder_exercise_open(contract, at(19))

        missing_window = OptionContract(
            instrument="OPT:NO-WINDOW",
            right="CALL",
            strike=Decimal("100"),
            multiplier=Decimal("100"),
            settlement_currency="USD",
            settlement_method="CASH",
            exercise_style="EUROPEAN",
            exercise_cutoff=at(19),
            expiry=at(20),
        )
        self.assertEqual(
            exercise_gate(missing_window, at(18)),
            "PROVIDER_EXERCISE_WINDOW_REQUIRED",
        )

        with self.assertRaisesRegex(OptionError, "exercise_style"):
            OptionContract(
                instrument="OPT:BAD-STYLE",
                right="CALL",
                strike=Decimal("100"),
                multiplier=Decimal("100"),
                settlement_currency="USD",
                settlement_method="CASH",
                exercise_style="BERMUDAN",
                exercise_cutoff=at(19),
                expiry=at(20),
                exercise_opens_at=at(17),
            )

    def test_cash_and_physical_settlements_balance_by_unit(self):
        cash_tx = book_cash_option_settlement(
            transaction_id="option-cash-1",
            cause_event_id="expiry-1",
            settlement_currency="USD",
            amount="2000",
        )
        book = EconomicBook([cash_tx])
        self.assertEqual(book.cash("USD"), Decimal("2000"))

        obligation = physical_exercise_obligation(self._physical("CALL"), signed_contracts=1)
        physical_tx = book_physical_option_settlement(
            transaction_id="option-physical-1",
            cause_event_id="exercise-1",
            obligation=obligation,
        )
        physical_book = EconomicBook([physical_tx])
        self.assertEqual(
            physical_book.position("SHARES:ADJUSTED"),
            Decimal("150"),
        )
        self.assertEqual(physical_book.cash("USD"), Decimal("-5000"))

    def test_float_inputs_are_rejected(self):
        with self.assertRaises(OptionError):
            intrinsic_value_per_unit(self._cash_call(), 101.0)


class OptionRiskEvidenceTests(unittest.TestCase):
    def test_adjusted_physical_contract_requires_explicit_exercise_cash(self):
        with self.assertRaisesRegex(OptionError, "exercise_cash_per_contract"):
            OptionContract(
                instrument="OPT:ADJUSTED",
                right="CALL",
                strike=Decimal("50"),
                multiplier=Decimal("100"),
                settlement_currency="USD",
                settlement_method="PHYSICAL",
                exercise_style="AMERICAN",
                exercise_cutoff=at(19),
                expiry=at(20),
                deliverable=(DeliverableLeg("SHARES:ADJUSTED", Decimal("150")),),
            )

    def test_adjusted_deliverable_does_not_rewrite_strike_cash_by_multiplier(self):
        contract = OptionContract(
            instrument="OPT:ADJUSTED",
            right="CALL",
            strike=Decimal("50"),
            multiplier=Decimal("100"),
            settlement_currency="USD",
            settlement_method="PHYSICAL",
            exercise_style="AMERICAN",
            exercise_cutoff=at(19),
            expiry=at(20),
            deliverable=(DeliverableLeg("SHARES:ADJUSTED", Decimal("150")),),
            exercise_cash_per_contract=Decimal("5000"),
        )
        obligation = physical_exercise_obligation(contract, signed_contracts=1)
        self.assertEqual(
            obligation.asset_quantities,
            (("SHARES:ADJUSTED", Decimal("150")),),
        )
        self.assertEqual(obligation.settlement_cash, Decimal("-5000"))

    def _risk(self):
        return OptionRiskEvidence(
            instrument="OPT:CALL",
            model_id="scenario-greeks",
            model_version="1.2.0",
            source_sha="a" * 40,
            input_digest="sha256:" + "b" * 64,
            schema_version=1,
            market_as_of=at(17),
            calculated_at=at(18),
            expires_at=at(19),
            maximum_market_age=timedelta(hours=2),
            delta="0.52",
            gamma="0.03",
            vega="12.5",
            theta="-4.2",
            rho="1.1",
            scenarios=(
                OptionScenarioResult(
                    scenario_id="spot-down-vol-up",
                    underlying_price="80",
                    implied_volatility="0.55",
                    pnl="-725.25",
                ),
                OptionScenarioResult(
                    scenario_id="spot-up-vol-flat",
                    underlying_price="120",
                    implied_volatility="0.30",
                    pnl="410.10",
                ),
            ),
        )

    def test_greeks_are_versioned_estimates_and_stress_is_explicit(self):
        evidence = self._risk()
        self.assertEqual(evidence.delta, Decimal("0.52"))
        self.assertEqual(evidence.worst_scenario_loss, Decimal("725.25"))
        require_current_option_risk(
            evidence,
            instrument="OPT:CALL",
            at=datetime(2026, 9, 25, 18, 30, tzinfo=timezone.utc),
        )

    def test_stress_scenarios_allow_zero_underlying_but_reject_negative_price(self):
        zero = OptionScenarioResult(
            scenario_id="total-loss-boundary",
            underlying_price="0",
            implied_volatility="1.25",
            pnl="-1000",
        )
        self.assertEqual(zero.underlying_price, Decimal("0"))
        with self.assertRaisesRegex(OptionError, "underlying_price cannot be negative"):
            OptionScenarioResult(
                scenario_id="invalid-negative",
                underlying_price="-0.01",
                implied_volatility="1.25",
                pnl="-1000",
            )

    def test_missing_scenarios_and_float_inputs_fail_closed(self):
        with self.assertRaisesRegex(OptionError, "scenario stress"):
            OptionRiskEvidence(
                instrument="OPT:CALL",
                model_id="model",
                model_version="1",
                source_sha="a" * 40,
                input_digest="sha256:" + "b" * 64,
                schema_version=1,
                market_as_of=at(17),
                calculated_at=at(18),
                expires_at=at(19),
                maximum_market_age=timedelta(hours=2),
                delta="0",
                gamma="0",
                vega="0",
                theta="0",
                rho="0",
                scenarios=(),
            )
        with self.assertRaises(OptionError):
            OptionScenarioResult(
                scenario_id="bad",
                underlying_price=100.0,
                implied_volatility="0.3",
                pnl="-1",
            )

    def test_duplicate_scenarios_and_bad_provenance_fail_closed(self):
        scenario = OptionScenarioResult(
            scenario_id="same",
            underlying_price="100",
            implied_volatility="0.3",
            pnl="-1",
        )
        with self.assertRaisesRegex(OptionError, "unique"):
            OptionRiskEvidence(
                instrument="OPT:CALL",
                model_id="model",
                model_version="1",
                source_sha="a" * 40,
                input_digest="sha256:" + "b" * 64,
                schema_version=1,
                market_as_of=at(17),
                calculated_at=at(18),
                expires_at=at(19),
                maximum_market_age=timedelta(hours=2),
                delta="0",
                gamma="0",
                vega="0",
                theta="0",
                rho="0",
                scenarios=(scenario, scenario),
            )
        with self.assertRaisesRegex(OptionError, "source_sha"):
            OptionRiskEvidence(
                instrument="OPT:CALL",
                model_id="model",
                model_version="1",
                source_sha="NOT_A_SHA",
                input_digest="sha256:" + "b" * 64,
                schema_version=1,
                market_as_of=at(17),
                calculated_at=at(18),
                expires_at=at(19),
                maximum_market_age=timedelta(hours=2),
                delta="0",
                gamma="0",
                vega="0",
                theta="0",
                rho="0",
                scenarios=(scenario,),
            )

        with self.assertRaisesRegex(OptionError, "40-character"):
            OptionRiskEvidence(
                instrument="OPT:CALL",
                model_id="model",
                model_version="1",
                source_sha="c" * 64,
                input_digest="sha256:" + "b" * 64,
                schema_version=1,
                market_as_of=at(17),
                calculated_at=at(18),
                expires_at=at(19),
                maximum_market_age=timedelta(hours=2),
                delta="0",
                gamma="0",
                vega="0",
                theta="0",
                rho="0",
                scenarios=(scenario,),
            )

    def test_future_stale_and_cross_instrument_evidence_are_blocked(self):
        evidence = self._risk()
        with self.assertRaisesRegex(OptionError, "future"):
            require_current_option_risk(
                evidence,
                instrument="OPT:CALL",
                at=at(17),
            )
        with self.assertRaisesRegex(OptionError, "stale"):
            require_current_option_risk(
                evidence,
                instrument="OPT:CALL",
                at=at(19),
            )
        with self.assertRaisesRegex(OptionError, "another instrument"):
            require_current_option_risk(
                evidence,
                instrument="OPT:PUT",
                at=datetime(2026, 9, 25, 18, 30, tzinfo=timezone.utc),
            )


    def test_market_snapshot_age_expires_independently_of_calculation_expiry(self):
        evidence = OptionRiskEvidence(
            instrument="OPT:CALL",
            model_id="scenario-greeks",
            model_version="1.2.0",
            source_sha="a" * 40,
            input_digest="sha256:" + "b" * 64,
            schema_version=1,
            market_as_of=at(17),
            calculated_at=datetime(2026, 9, 25, 17, 30, tzinfo=timezone.utc),
            expires_at=datetime(2026, 9, 25, 21, 0, tzinfo=timezone.utc),
            maximum_market_age=timedelta(hours=2),
            delta="0.52",
            gamma="0.03",
            vega="12.5",
            theta="-4.2",
            rho="1.1",
            scenarios=(
                OptionScenarioResult(
                    scenario_id="stress",
                    underlying_price="80",
                    implied_volatility="0.55",
                    pnl="-725.25",
                ),
            ),
        )
        require_current_option_risk(
            evidence,
            instrument="OPT:CALL",
            at=datetime(2026, 9, 25, 18, 59, tzinfo=timezone.utc),
        )
        with self.assertRaisesRegex(OptionError, "market evidence is stale"):
            require_current_option_risk(
                evidence,
                instrument="OPT:CALL",
                at=datetime(2026, 9, 25, 19, 1, tzinfo=timezone.utc),
            )

    def test_market_snapshot_cannot_be_stale_when_risk_is_calculated(self):
        with self.assertRaisesRegex(OptionError, "stale at calculation"):
            OptionRiskEvidence(
                instrument="OPT:CALL",
                model_id="model",
                model_version="1",
                source_sha="a" * 40,
                input_digest="sha256:" + "b" * 64,
                schema_version=1,
                market_as_of=at(17),
                calculated_at=at(18),
                expires_at=at(20),
                maximum_market_age=timedelta(minutes=30),
                delta="0",
                gamma="0",
                vega="0",
                theta="0",
                rho="0",
                scenarios=(
                    OptionScenarioResult(
                        scenario_id="stress",
                        underlying_price="100",
                        implied_volatility="0.3",
                        pnl="-1",
                    ),
                ),
            )


if __name__ == "__main__":
    unittest.main()
