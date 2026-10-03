from decimal import Decimal, Inexact, Rounded, ROUND_CEILING, ROUND_FLOOR, localcontext
from fractions import Fraction
from unittest.mock import patch
import unittest
import mvp.autotrade_mvp.allocation as allocation_module

from mvp.autotrade_mvp.fx_valuation import FxRoundingPolicy
from mvp.autotrade_mvp.allocation import (
    AllocationPolicy,
    ImmutableAllocationEvidence,
    ObjectiveCandidate,
    allocate_evidence_bound_objective_targets,
    revalidate_evidence_bound_allocation,
)


class EvidenceBoundAllocationTests(unittest.TestCase):
    DECISION_TIME = "2026-09-25T18:30:00Z"
    OBSERVED_AT = "2026-09-25T18:00:00Z"
    VALID_UNTIL = "2026-09-25T19:00:00Z"
    FORECAST_HORIZON_END = "2026-09-26T18:30:00Z"

    def policy(self, *, cash_available="1000"):
        return AllocationPolicy.create(
            cash_available=cash_available,
            max_gross_notional="1000",
            max_net_notional="1000",
            max_symbol_notional="1000",
            max_total_cost="50",
            max_stress_loss="500",
            max_turnover_notional="1000",
            require_adverse_stress_evidence=True,
            require_fresh_stress_evidence=True,
        )

    def candidate(self, *, expected_return_rate="0.10"):
        return ObjectiveCandidate.create(
            symbol="AAA",
            desired_notional="500",
            price="10",
            lot_size="1",
            expected_return_rate=expected_return_rate,
            risk_penalty_rate="0.01",
            cost_rate="0.001",
            capital_requirement_rate="1",
            min_notional="10",
            fee_floor="0",
            max_executable_notional="500",
        )

    def evidence(self, *, evidence_id, kind, payload, environment="SIMULATION",
                 observed_at=None, valid_until=None):
        return ImmutableAllocationEvidence.create(
            evidence_id=evidence_id,
            kind=kind,
            environment=environment,
            schema_version="1.0.0",
            observed_at=observed_at or self.OBSERVED_AT,
            valid_until=valid_until or self.VALID_UNTIL,
            payload=payload,
        )

    def bundle(self, *, objective_rate="0.10", capital_cash="1000",
               environment="SIMULATION", market_valid_until=None,
               capital_provider="SIMULATED"):
        objective = self.evidence(
            evidence_id="objective:aaa:v1",
            kind="OBJECTIVE",
            environment=environment,
            payload={
                "symbol": "AAA",
                "candidate_id": "candidate:aaa:v1",
                "proposal_id": "proposal:aaa:v11",
                "strategy_version": "strategy:v7",
                "protocol_digest": "1" * 64,
                "input_snapshot_digest": "2" * 64,
                "information_cutoff": "2026-09-25T18:20:00Z",
                "forecast_horizon_end": self.FORECAST_HORIZON_END,
                "desired_notional": "500",
                "desired_notional_currency": "USD",
                "expected_return_rate": objective_rate,
                "risk_penalty_rate": "0.01",
            },
        )
        market = self.evidence(
            evidence_id="market:aaa:v1",
            kind="MARKET_CONSTRAINT",
            environment=environment,
            valid_until=market_valid_until,
            payload={
                "symbol": "AAA",
                "instrument_version": "instrument:aaa:v3",
                "provider_id": "SIMULATED",
                "account_id": "acct:paper:1",
                "capability_snapshot_id": "capability:1",
                "source_as_of": "2026-09-25T18:15:00Z",
                "asset_class": "CASH_EQUITY",
                "payoff": "LINEAR",
                "quantity_unit": "SHARE",
                "contract_multiplier": "1",
                "quote_currency": "USD",
                "settlement_currency": "USD",
                "monetary_constraint_currency": "USD",
                "price": "10",
                "lot_size": "1",
                "cost_rate": "0.001",
                "capital_requirement_rate": "1",
                "min_notional": "10",
                "fee_floor": "0",
                "max_executable_notional": "500",
            },
        )
        valuation = self.evidence(
            evidence_id="valuation:aaa:v1",
            kind="VALUATION",
            environment=environment,
            payload={
                "symbol": "AAA",
                "instrument_version": "instrument:aaa:v3",
                "capability_snapshot_id": "capability:1",
                "asset_class": "CASH_EQUITY",
                "payoff": "LINEAR",
                "quantity_unit": "SHARE",
                "contract_multiplier": "1",
                "quote_currency": "USD",
                "settlement_currency": "USD",
                "source_price": "10",
                "portfolio_base_currency": "USD",
                "source_monetary_currency": "USD",
                "desired_notional_currency": "USD",
                "desired_notional_base": "500",
                "fx_rate": "1",
                "fx_source_id": "IDENTITY",
                "unit_base_notional": "10",
                "capital_requirement_rate": "1",
                "min_notional_base": "10",
                "fee_floor_base": "0",
                "max_executable_notional_base": "500",
                "payoff_identity": "linear:cash-equity:v1",
                "holding_cost_horizon_end": self.FORECAST_HORIZON_END,
                "cost_rate_components": {
                    "execution": "0.001", "financing": "0", "funding": "0", "borrow": "0", "fx": "0"
                },
                "cost_evidence_refs": {
                    "execution": "execution-cost:aaa:v1",
                    "financing": "financing:none:aaa:v1",
                    "funding": "funding:none:aaa:v1",
                    "borrow": "borrow:none:aaa:v1",
                    "fx": "fx:identity:usd:v1",
                },
            },
        )
        capital = self.evidence(
            evidence_id="capital:acct:1:v5",
            kind="CAPITAL_STATE",
            environment=environment,
            payload={
                "provider_id": capital_provider,
                "account_id": "acct:paper:1",
                "account_snapshot_id": "snapshot:acct:1:v5",
                "reconciliation_run_id": "reconciliation:acct:1:v5",
                "account_state_version": 5,
                "reservation_state_version": 9,
                "reservation_state_digest": "3" * 64,
                "cash_available": capital_cash,
                "base_currency": "USD",
                "positions_complete": True,
                "position_quantities": {"AAA": "0"},
            },
        )
        stress = self.evidence(
            evidence_id="stress:gap:v2",
            kind="STRESS_SCENARIO",
            environment=environment,
            payload={
                "name": "gap_down",
                "shocks": {"AAA": "-0.20"},
                "instrument_versions": {"AAA": "instrument:aaa:v3"},
            },
        )
        resolved = {
            item.evidence_id: item
            for item in (objective, market, valuation, capital, stress)
        }
        return objective, market, capital, stress, resolved

    def allocate(self, *, candidate=None, policy=None, bundle=None, environment="SIMULATION"):
        candidate = candidate or self.candidate()
        policy = policy or self.policy()
        objective, market, capital, stress, resolved = bundle or self.bundle(
            environment=environment
        )
        return allocate_evidence_bound_objective_targets(
            (candidate,),
            policy,
            objective_evidence={"AAA": objective},
            market_evidence={"AAA": market},
            valuation_evidence={"AAA": resolved["valuation:aaa:v1"]},
            capital_evidence=capital,
            stress_source_evidence=(stress,),
            resolved_evidence=resolved,
            environment=environment,
            decision_time=self.DECISION_TIME,
            policy_version="risk-policy:12",
        )


    def test_objective_forecast_horizon_must_extend_beyond_decision_time(self):
        objective, market, capital, stress, resolved = self.bundle()
        objective = self.evidence(
            evidence_id=objective.evidence_id,
            kind=objective.kind,
            payload={
                **objective.payload,
                "forecast_horizon_end": self.DECISION_TIME,
            },
        )
        resolved = {
            **resolved,
            objective.evidence_id: objective,
        }

        with self.assertRaisesRegex(
            ValueError,
            "forecast_horizon_end must be after decision_time",
        ):
            self.allocate(
                bundle=(objective, market, capital, stress, resolved),
            )

    def test_holding_cost_horizon_must_match_objective_forecast_horizon(self):
        objective, market, capital, stress, resolved = self.bundle()
        valuation = resolved["valuation:aaa:v1"]
        valuation = self.evidence(
            evidence_id=valuation.evidence_id,
            kind=valuation.kind,
            payload={
                **valuation.payload,
                "holding_cost_horizon_end": "2026-09-27T18:30:00Z",
            },
        )
        resolved = {
            **resolved,
            valuation.evidence_id: valuation,
        }

        with self.assertRaisesRegex(
            ValueError,
            "holding-cost horizon does not match objective forecast horizon",
        ):
            self.allocate(
                bundle=(objective, market, capital, stress, resolved),
            )

    def test_matched_horizon_financing_cost_is_charged_to_net_utility(self):
        objective, market, capital, stress, resolved = self.bundle()
        market = self.evidence(
            evidence_id=market.evidence_id,
            kind=market.kind,
            payload={
                **market.payload,
                "cost_rate": "0.011",
            },
        )
        valuation = resolved["valuation:aaa:v1"]
        valuation = self.evidence(
            evidence_id=valuation.evidence_id,
            kind=valuation.kind,
            payload={
                **valuation.payload,
                "cost_rate_components": {
                    "execution": "0.001",
                    "financing": "0.01",
                    "funding": "0",
                    "borrow": "0",
                    "fx": "0",
                },
                "cost_evidence_refs": {
                    "execution": "execution-cost:aaa:v1",
                    "financing": "financing:aaa:24h:v1",
                    "funding": "funding:none:aaa:v1",
                    "borrow": "borrow:none:aaa:v1",
                    "fx": "fx:identity:usd:v1",
                },
            },
        )
        resolved = {
            objective.evidence_id: objective,
            market.evidence_id: market,
            valuation.evidence_id: valuation,
            capital.evidence_id: capital,
            stress.evidence_id: stress,
        }
        candidate = self.candidate()
        candidate = ObjectiveCandidate.create(
            symbol=candidate.candidate.symbol,
            desired_notional=candidate.candidate.desired_notional,
            price=candidate.candidate.price,
            lot_size=candidate.candidate.lot_size,
            expected_return_rate=candidate.expected_return_rate,
            risk_penalty_rate=candidate.risk_penalty_rate,
            cost_rate="0.011",
            capital_requirement_rate=candidate.candidate.capital_requirement_rate,
            min_notional=candidate.candidate.min_notional,
            fee_floor=candidate.candidate.fee_floor,
            max_executable_notional=candidate.candidate.max_executable_notional,
        )

        result = self.allocate(
            candidate=candidate,
            bundle=(objective, market, capital, stress, resolved),
        )

        target = result.objective.allocation.targets[0]
        self.assertEqual(target.estimated_cost, Decimal("5.500"))
        self.assertEqual(result.objective.expected_net_utility, Decimal("39.500"))

    def cross_currency_bundle(self, *, omit_desired_currency=False):
        objective, market, capital, stress, resolved = self.bundle()
        objective_payload = dict(objective.payload)
        objective_payload.update({
            "desired_notional": "100",
            "desired_notional_currency": "EUR",
        })
        if omit_desired_currency:
            objective_payload.pop("desired_notional_currency")
        objective = self.evidence(
            evidence_id=objective.evidence_id,
            kind="OBJECTIVE",
            payload=objective_payload,
        )

        market_payload = dict(market.payload)
        market_payload.update({
            "quote_currency": "EUR",
            "settlement_currency": "EUR",
            "price": "10",
            "min_notional": "10",
            "fee_floor": "2",
            "max_executable_notional": "100",
            "monetary_constraint_currency": "EUR",
        })
        market = self.evidence(
            evidence_id=market.evidence_id,
            kind="MARKET_CONSTRAINT",
            payload=market_payload,
        )

        valuation = resolved["valuation:aaa:v1"]
        valuation_payload = dict(valuation.payload)
        valuation_payload.update({
            "quote_currency": "EUR",
            "settlement_currency": "EUR",
            "source_price": "10",
            "portfolio_base_currency": "USD",
            "fx_rate": "1.20",
            "fx_source_id": "fx:eurusd:allocation:v1",
            "fx_quote": {
                "base_currency": "EUR",
                "quote_currency": "USD",
                "bid": "1.20",
                "ask": "1.20",
                "available_at": "2026-09-25T18:29:30Z",
                "source_id": "fx:eurusd:allocation:v1",
                "evidence_sha256": "sha256:" + "a" * 64,
                "max_age_seconds": 60,
                "haircut": "0",
            },
            "fx_evidence_sha256": "sha256:" + "a" * 64,
            "unit_base_notional": "12.0",
            "source_monetary_currency": "EUR",
            "desired_notional_currency": "EUR",
            "desired_notional_base": "120",
            "min_notional_base": "12",
            "fee_floor_base": "2.4",
            "max_executable_notional_base": "120",
            "cost_evidence_refs": {
                "execution": "execution-cost:aaa:v1",
                "financing": "financing:none:aaa:v1",
                "funding": "funding:none:aaa:v1",
                "borrow": "borrow:none:aaa:v1",
                "fx": "fx:eurusd:allocation:v1",
            },
        })
        valuation = self.evidence(
            evidence_id=valuation.evidence_id,
            kind="VALUATION",
            payload=valuation_payload,
        )
        resolved = {
            objective.evidence_id: objective,
            market.evidence_id: market,
            valuation.evidence_id: valuation,
            capital.evidence_id: capital,
            stress.evidence_id: stress,
        }
        return objective, market, capital, stress, resolved

    def test_cross_currency_normalizes_desired_min_fee_and_executable_cap(self):
        candidate = ObjectiveCandidate.create(
            symbol="AAA",
            desired_notional="100",
            price="10",
            lot_size="1",
            expected_return_rate="0.10",
            risk_penalty_rate="0.01",
            cost_rate="0.001",
            capital_requirement_rate="1",
            min_notional="10",
            fee_floor="2",
            max_executable_notional="100",
        )
        result = self.allocate(
            candidate=candidate,
            bundle=self.cross_currency_bundle(),
        )
        target = result.objective.allocation.targets[0]
        self.assertEqual(target.quantity, 10)
        self.assertEqual(target.notional, 120)
        self.assertEqual(target.estimated_cost, Decimal("2.4"))
        self.assertEqual(result.objective.allocation.cash_required, Decimal("122.4"))

    def test_direct_fx_spread_uses_role_specific_asset_and_liability_sides(self):
        objective, market, capital, stress, resolved = self.cross_currency_bundle()

        objective_payload = dict(objective.payload)
        objective_payload.update({
            "desired_notional": "-100",
            "expected_return_rate": "0.10",
        })
        objective = self.evidence(
            evidence_id=objective.evidence_id,
            kind="OBJECTIVE",
            payload=objective_payload,
        )

        market_payload = dict(market.payload)
        market_payload.update({
            "lot_size": "0.1",
            "min_notional": "1",
            "fee_floor": "1",
            "max_executable_notional": "200",
        })
        market = self.evidence(
            evidence_id=market.evidence_id,
            kind="MARKET_CONSTRAINT",
            payload=market_payload,
        )

        valuation = resolved["valuation:aaa:v1"]
        valuation_payload = dict(valuation.payload)
        quote_payload = dict(valuation_payload["fx_quote"])
        quote_payload.update({"bid": "1.10", "ask": "1.20"})
        valuation_payload.update({
            "fx_rate": "1.10",
            "fx_quote": quote_payload,
            "unit_base_notional": "11",
            "desired_notional_base": "-120",
            "min_notional_base": "1.2",
            "fee_floor_base": "1.2",
            "max_executable_notional_base": "220",
        })
        valuation = self.evidence(
            evidence_id=valuation.evidence_id,
            kind="VALUATION",
            payload=valuation_payload,
        )
        resolved = {
            objective.evidence_id: objective,
            market.evidence_id: market,
            valuation.evidence_id: valuation,
            capital.evidence_id: capital,
            stress.evidence_id: stress,
        }
        bundle = (objective, market, capital, stress, resolved)
        candidate = ObjectiveCandidate.create(
            symbol="AAA",
            desired_notional="-100",
            price="10",
            lot_size="0.1",
            expected_return_rate="0.10",
            risk_penalty_rate="0.01",
            cost_rate="0.001",
            capital_requirement_rate="1",
            min_notional="1",
            fee_floor="1",
            max_executable_notional="200",
        )

        stress_payload = dict(stress.payload)
        stress_payload["shocks"] = {"AAA": "0.20"}
        stress = self.evidence(
            evidence_id=stress.evidence_id,
            kind="STRESS_SCENARIO",
            payload=stress_payload,
        )
        resolved[stress.evidence_id] = stress
        bundle = (objective, market, capital, stress, resolved)

        with patch.object(
            allocation_module,
            "allocate_objective_targets",
            wraps=allocation_module.allocate_objective_targets,
        ) as allocate:
            result = self.allocate(candidate=candidate, bundle=bundle)
            normalized = allocate.call_args.args[0][0].candidate

        self.assertEqual(normalized.price, Decimal("12"))
        self.assertEqual(
            allocation_module._round_quantity(
                normalized.desired_notional,
                normalized.price,
                normalized.lot_size,
            ),
            Decimal("-10"),
        )
        self.assertEqual(result.objective.allocation.status, "ALLOCATED")
        self.assertEqual(result.objective.allocation.targets[0].quantity, Decimal("-10"))
        self.assertEqual(result.objective.allocation.targets[0].notional, Decimal("-120"))
        self.assertEqual(
            (
                normalized.desired_notional,
                normalized.min_notional,
                normalized.fee_floor,
                normalized.max_executable_notional,
            ),
            (
                Decimal("-120"),
                Decimal("1.2"),
                Decimal("1.2"),
                Decimal("220"),
            ),
        )

    def test_cross_currency_spread_reversal_fails_closed_before_linear_allocation(self):
        objective, market, capital, stress, resolved = self.cross_currency_bundle()

        objective_payload = dict(objective.payload)
        objective_payload.update({
            "desired_notional": "-100",
            "expected_return_rate": "0.10",
        })
        objective = self.evidence(
            evidence_id=objective.evidence_id,
            kind="OBJECTIVE",
            payload=objective_payload,
        )

        valuation = resolved["valuation:aaa:v1"]
        valuation_payload = dict(valuation.payload)
        quote_payload = dict(valuation_payload["fx_quote"])
        quote_payload.update({"bid": "1.10", "ask": "1.20"})
        valuation_payload.update({
            "fx_rate": "1.10",
            "fx_quote": quote_payload,
            "unit_base_notional": "11",
            "desired_notional_base": "-120",
            "max_executable_notional_base": "110",
        })
        valuation = self.evidence(
            evidence_id=valuation.evidence_id,
            kind="VALUATION",
            payload=valuation_payload,
        )

        capital_payload = dict(capital.payload)
        capital_payload["position_quantities"] = {"AAA": "10"}
        capital = self.evidence(
            evidence_id=capital.evidence_id,
            kind="CAPITAL_STATE",
            payload=capital_payload,
        )
        resolved = {
            objective.evidence_id: objective,
            market.evidence_id: market,
            valuation.evidence_id: valuation,
            capital.evidence_id: capital,
            stress.evidence_id: stress,
        }
        candidate = ObjectiveCandidate.create(
            symbol="AAA",
            desired_notional="-100",
            price="10",
            lot_size="1",
            expected_return_rate="0.10",
            risk_penalty_rate="0.01",
            cost_rate="0.001",
            capital_requirement_rate="1",
            min_notional="10",
            fee_floor="2",
            max_executable_notional="100",
        )

        with self.assertRaisesRegex(ValueError, "piecewise side-aware allocation"):
            self.allocate(
                candidate=candidate,
                bundle=(objective, market, capital, stress, resolved),
            )

    def test_cross_currency_financial_normalization_ignores_ambient_decimal_context(self):
        objective, market, capital, stress, resolved = self.cross_currency_bundle()

        market_payload = dict(market.payload)
        market_payload["cost_rate"] = "0.0010000001"
        market = self.evidence(
            evidence_id=market.evidence_id,
            kind="MARKET_CONSTRAINT",
            payload=market_payload,
        )

        valuation = resolved["valuation:aaa:v1"]
        valuation_payload = dict(valuation.payload)
        quote_payload = dict(valuation_payload["fx_quote"])
        quote_payload.update({
            "bid": "1.2000000001",
            "ask": "1.2000000001",
        })
        valuation_payload.update({
            "fx_rate": "1.2000000001",
            "fx_quote": quote_payload,
            "unit_base_notional": "12.000000001",
            "desired_notional_base": "120.00000001",
            "min_notional_base": "12.000000001",
            "fee_floor_base": "2.4000000002",
            "max_executable_notional_base": "120.00000001",
            "cost_rate_components": {
                "execution": "0.00100000005",
                "financing": "0",
                "funding": "0",
                "borrow": "0",
                "fx": "0.00000000005",
            },
        })
        valuation = self.evidence(
            evidence_id=valuation.evidence_id,
            kind="VALUATION",
            payload=valuation_payload,
        )
        resolved = {
            objective.evidence_id: objective,
            market.evidence_id: market,
            valuation.evidence_id: valuation,
            capital.evidence_id: capital,
            stress.evidence_id: stress,
        }
        bundle = (objective, market, capital, stress, resolved)
        candidate = ObjectiveCandidate.create(
            symbol="AAA",
            desired_notional="100",
            price="10",
            lot_size="1",
            expected_return_rate="0.10",
            risk_penalty_rate="0.01",
            cost_rate="0.0010000001",
            capital_requirement_rate="1",
            min_notional="10",
            fee_floor="2",
            max_executable_notional="100",
        )

        def snapshot():
            result = self.allocate(candidate=candidate, bundle=bundle)
            target = result.objective.allocation.targets[0]
            return (
                result.objective.allocation.status,
                target.quantity,
                target.notional,
                target.estimated_cost,
                result.objective.allocation.cash_required,
                result.objective.selected_symbols,
                result.objective.expected_net_utility,
            )

        baseline = snapshot()
        self.assertEqual(baseline[0], "ALLOCATED")
        self.assertEqual(baseline[2], Decimal("120.00000001"))
        self.assertEqual(baseline[3], Decimal("2.4000000002"))
        self.assertEqual(
            baseline[4],
            Decimal("122.4000000102"),
        )
        for precision in (6, 10, 28, 80):
            for rounding in (ROUND_FLOOR, ROUND_CEILING):
                with localcontext() as context:
                    context.prec = precision
                    context.rounding = rounding
                    self.assertEqual(snapshot(), baseline)

    def inverse_fx_bundle(self):
        objective, market, capital, stress, resolved = self.bundle()
        digest = "sha256:" + "b" * 64
        policy = FxRoundingPolicy(
            reporting_currency="EUR",
            quantum=Decimal("0.01"),
        )

        objective_payload = dict(objective.payload)
        objective_payload["desired_notional"] = "100"
        objective = self.evidence(
            evidence_id=objective.evidence_id,
            kind="OBJECTIVE",
            payload=objective_payload,
        )

        market_payload = dict(market.payload)
        market_payload.update({
            "price": "110.02",
            "lot_size": "0.0001",
            "min_notional": "0",
            "fee_floor": "0",
            "max_executable_notional": "100",
        })
        market = self.evidence(
            evidence_id=market.evidence_id,
            kind="MARKET_CONSTRAINT",
            payload=market_payload,
        )

        valuation = resolved["valuation:aaa:v1"]
        valuation_payload = dict(valuation.payload)
        valuation_payload.update({
            "source_price": "110.02",
            "portfolio_base_currency": "EUR",
            "fx_rate": None,
            "fx_rate_numerator": 5000,
            "fx_rate_denominator": 5501,
            "fx_source_id": "fx:eurusd:venue:v8",
            "fx_quote": {
                "base_currency": "EUR",
                "quote_currency": "USD",
                "bid": "1.1000",
                "ask": "1.1002",
                "available_at": "2026-09-25T18:29:30Z",
                "source_id": "fx:eurusd:venue:v8",
                "evidence_sha256": digest,
                "max_age_seconds": 60,
                "haircut": "0",
            },
            "fx_evidence_sha256": digest,
            "fx_rounding_policy_id": policy.policy_id,
            "fx_rounding_quantum": "0.01",
            "unit_base_notional": "100",
            "desired_notional_base": "90.89",
            "min_notional_base": "0",
            "fee_floor_base": "0",
            "max_executable_notional_base": "90.89",
            "cost_evidence_refs": {
                "execution": "execution-cost:aaa:v1",
                "financing": "financing:none:aaa:v1",
                "funding": "funding:none:aaa:v1",
                "borrow": "borrow:none:aaa:v1",
                "fx": "fx:eurusd:venue:v8",
            },
        })
        valuation = self.evidence(
            evidence_id=valuation.evidence_id,
            kind="VALUATION",
            payload=valuation_payload,
        )

        capital_payload = dict(capital.payload)
        capital_payload["base_currency"] = "EUR"
        capital = self.evidence(
            evidence_id=capital.evidence_id,
            kind="CAPITAL_STATE",
            payload=capital_payload,
        )

        resolved = {
            objective.evidence_id: objective,
            market.evidence_id: market,
            valuation.evidence_id: valuation,
            capital.evidence_id: capital,
            stress.evidence_id: stress,
        }
        bundle = (objective, market, capital, stress, resolved)
        candidate = ObjectiveCandidate.create(
            symbol="AAA",
            desired_notional="100",
            price="110.02",
            lot_size="0.0001",
            expected_return_rate="0.10",
            risk_penalty_rate="0.01",
            cost_rate="0.001",
            capital_requirement_rate="1",
            min_notional="0",
            fee_floor="0",
            max_executable_notional="100",
        )

        return candidate, bundle

    def test_inverse_fx_exact_identity_reaches_allocator_without_fabricated_rate(self):
        candidate, bundle = self.inverse_fx_bundle()

        def snapshot():
            result = self.allocate(candidate=candidate, bundle=bundle)
            target = result.objective.allocation.targets[0]
            return (
                result.objective.allocation.status,
                target.quantity,
                target.notional,
                target.estimated_cost,
                result.objective.allocation.cash_required,
                result.objective.selected_symbols,
                result.objective.expected_net_utility,
            )

        baseline = snapshot()
        self.assertEqual(baseline[0], "ALLOCATED")
        self.assertEqual(baseline[1], Decimal("0.9089"))
        self.assertEqual(baseline[2], Decimal("90.89"))
        self.assertEqual(baseline[3], Decimal("0.09089"))
        self.assertEqual(baseline[4], Decimal("90.98089"))
        self.assertEqual(baseline[5], ("AAA",))
        self.assertEqual(baseline[6], Decimal("8.08921"))
        for precision in (6, 10, 28, 80):
            for rounding in (ROUND_FLOOR, ROUND_CEILING):
                with localcontext() as context:
                    context.prec = precision
                    context.rounding = rounding
                    self.assertEqual(snapshot(), baseline)

    def test_evidence_bound_allocation_rejects_rounded_unit_fx_before_source_cap_can_be_breached(self):
        objective, market, capital, stress, resolved = self.bundle()
        digest = "sha256:" + "d" * 64
        policy = FxRoundingPolicy(
            reporting_currency="EUR",
            quantum=Decimal("0.01"),
        )

        objective = self.evidence(
            evidence_id=objective.evidence_id,
            kind="OBJECTIVE",
            payload={
                **objective.payload,
                "desired_notional": "3",
                "desired_notional_currency": "USD",
            },
        )
        market = self.evidence(
            evidence_id=market.evidence_id,
            kind="MARKET_CONSTRAINT",
            payload={
                **market.payload,
                "quote_currency": "USD",
                "settlement_currency": "USD",
                "monetary_constraint_currency": "USD",
                "price": "1",
                "min_notional": "0",
                "fee_floor": "0",
                "max_executable_notional": "0.99",
            },
        )
        valuation = resolved["valuation:aaa:v1"]
        valuation = self.evidence(
            evidence_id=valuation.evidence_id,
            kind="VALUATION",
            payload={
                **valuation.payload,
                "quote_currency": "USD",
                "settlement_currency": "USD",
                "source_price": "1",
                "portfolio_base_currency": "EUR",
                "source_monetary_currency": "USD",
                "desired_notional_currency": "USD",
                "desired_notional_base": "1",
                "fx_rate": None,
                "fx_rate_numerator": 1,
                "fx_rate_denominator": 3,
                "fx_source_id": "fx:eurusd:venue:one-third",
                "fx_quote": {
                    "base_currency": "EUR",
                    "quote_currency": "USD",
                    "bid": "3",
                    "ask": "3",
                    "available_at": "2026-09-25T18:29:30Z",
                    "source_id": "fx:eurusd:venue:one-third",
                    "evidence_sha256": digest,
                    "max_age_seconds": 60,
                    "haircut": "0",
                },
                "fx_evidence_sha256": digest,
                "fx_rounding_policy_id": policy.policy_id,
                "fx_rounding_quantum": "0.01",
                "unit_base_notional": "0.33",
                "capital_requirement_rate": "1",
                "min_notional_base": "0",
                "fee_floor_base": "0",
                "max_executable_notional_base": "0.33",
                "cost_evidence_refs": {
                    "execution": "execution-cost:aaa:v1",
                    "financing": "financing:none:aaa:v1",
                    "funding": "funding:none:aaa:v1",
                    "borrow": "borrow:none:aaa:v1",
                    "fx": "fx:eurusd:venue:one-third",
                },
            },
        )
        capital = self.evidence(
            evidence_id=capital.evidence_id,
            kind="CAPITAL_STATE",
            payload={
                **capital.payload,
                "base_currency": "EUR",
            },
        )
        resolved = {
            objective.evidence_id: objective,
            market.evidence_id: market,
            valuation.evidence_id: valuation,
            capital.evidence_id: capital,
            stress.evidence_id: stress,
        }
        candidate = ObjectiveCandidate.create(
            symbol="AAA",
            desired_notional="3",
            price="1",
            lot_size="1",
            expected_return_rate="0.10",
            risk_penalty_rate="0.01",
            cost_rate="0.001",
            capital_requirement_rate="1",
            min_notional="0",
            fee_floor="0",
            max_executable_notional="0.99",
        )

        with self.assertRaisesRegex(
            ValueError,
            "unit FX conversion must terminate exactly",
        ):
            self.allocate(
                candidate=candidate,
                bundle=(objective, market, capital, stress, resolved),
            )

    def test_inverse_fx_projects_each_monetary_purpose_conservatively(self):
        _, bundle = self.inverse_fx_bundle()
        objective, market, capital, stress, resolved = bundle
        updates = {
            objective.evidence_id: {"desired_notional": "-100", "expected_return_rate": "-0.10"},
            market.evidence_id: {"min_notional": "1", "fee_floor": "1"},
            "valuation:aaa:v1": {"desired_notional_base": "-90.90",
                "min_notional_base": "0.91", "fee_floor_base": "0.91"},
        }
        resolved = {key: self.evidence(evidence_id=value.evidence_id, kind=value.kind,
                    payload={**value.payload, **updates.get(key, {})})
                    for key, value in resolved.items()}
        bundle = (resolved[objective.evidence_id], resolved[market.evidence_id],
                  resolved[capital.evidence_id], resolved[stress.evidence_id], resolved)
        candidate = ObjectiveCandidate.create(symbol="AAA", desired_notional="-100",
            price="110.02", lot_size="0.0001", expected_return_rate="-0.10",
            risk_penalty_rate="0.01", cost_rate="0.001", capital_requirement_rate="1",
            min_notional="1", fee_floor="1", max_executable_notional="100")
        asset_rate = Fraction(5000, 5501)
        liability_rate = Fraction(10, 11)
        baseline = None
        for precision in (6, 10, 28, 80):
            for rounding in (ROUND_FLOOR, ROUND_CEILING):
                with self.subTest(precision=precision, rounding=rounding), localcontext() as context:
                    context.prec = precision
                    context.rounding = rounding
                    context.traps[Inexact] = True
                    context.traps[Rounded] = True
                    with patch.object(allocation_module, "allocate_objective_targets",
                            wraps=allocation_module.allocate_objective_targets) as allocate:
                        self.allocate(candidate=candidate, bundle=bundle)
                        normalized = allocate.call_args.args[0][0].candidate
                    values = (normalized.desired_notional, normalized.min_notional,
                              normalized.fee_floor, normalized.max_executable_notional)
                    self.assertGreaterEqual(Fraction(values[0]), -100 * liability_rate)
                    self.assertGreaterEqual(Fraction(values[1]), liability_rate)
                    self.assertGreaterEqual(Fraction(values[2]), liability_rate)
                    self.assertLessEqual(Fraction(values[3]), 100 * asset_rate)
                    self.assertEqual(values, (Decimal("-90.90"), Decimal("0.91"),
                                             Decimal("0.91"), Decimal("90.89")))
                    if baseline is None:
                        baseline = values
                    self.assertEqual(values, baseline)

    def test_horizon_binding_algorithm_is_bound_into_policy_identity(self):
        bundle = self.bundle()
        original = self.allocate(bundle=bundle)
        with patch.object(
            allocation_module,
            "_OBJECTIVE_HORIZON_BINDING_ALGORITHM",
            "test-future-horizon-binding-v2",
            create=True,
        ):
            changed = self.allocate(bundle=bundle)
        self.assertNotEqual(original.policy_config_digest, changed.policy_config_digest)
        self.assertNotEqual(original.decision_digest, changed.decision_digest)

    def test_fx_projection_algorithm_is_bound_into_policy_identity(self):
        candidate, bundle = self.inverse_fx_bundle()
        original = self.allocate(candidate=candidate, bundle=bundle)
        with patch.object(allocation_module, "_ALLOCATION_FX_PROJECTION_ALGORITHM",
                          "test-future-projection-v2", create=True):
            changed = self.allocate(candidate=candidate, bundle=bundle)
        self.assertNotEqual(original.policy_config_digest, changed.policy_config_digest)
        self.assertNotEqual(original.decision_digest, changed.decision_digest)

    def test_fx_projection_keeps_terminating_values_exact_and_rejects_unknown_purpose(self):
        purposes = ("TARGET_NOTIONAL", "MIN_NOTIONAL", "FEE_FLOOR", "MAX_EXECUTABLE_NOTIONAL")
        for purpose in purposes:
            converted = allocation_module._exact_fx_monetary_conversion(
                Decimal("19.25"),
                asset_rate_numerator=2,
                asset_rate_denominator=5,
                liability_rate_numerator=2,
                liability_rate_denominator=5,
                rounding_quantum=Decimal("1"),
                purpose=purpose,
            )
            self.assertEqual(converted, Decimal("7.7"))
        with patch.object(allocation_module, "_shared_as_fraction",
                          side_effect=AssertionError("arithmetic before purpose admission")):
            with self.assertRaisesRegex(ValueError, "purpose is invalid"):
                allocation_module._exact_fx_monetary_conversion(
                    Decimal("1"),
                    asset_rate_numerator=1,
                    asset_rate_denominator=3,
                    liability_rate_numerator=1,
                    liability_rate_denominator=2,
                    rounding_quantum=Decimal("0.01"),
                    purpose="UNRECOGNIZED",
                )

    def test_cross_currency_rejects_unbound_desired_notional_currency(self):
        candidate = ObjectiveCandidate.create(
            symbol="AAA",
            desired_notional="100",
            price="10",
            lot_size="1",
            expected_return_rate="0.10",
            risk_penalty_rate="0.01",
            cost_rate="0.001",
            capital_requirement_rate="1",
            min_notional="10",
            fee_floor="2",
            max_executable_notional="100",
        )
        with self.assertRaisesRegex(ValueError, "desired_notional_currency"):
            self.allocate(
                candidate=candidate,
                bundle=self.cross_currency_bundle(
                    omit_desired_currency=True,
                ),
            )

    def test_same_currency_requires_explicit_monetary_unit_declarations(self):
        objective, market, capital, stress, resolved = self.bundle()
        objective_payload = dict(objective.payload)
        objective_payload.pop("desired_notional_currency")
        objective = self.evidence(
            evidence_id=objective.evidence_id,
            kind="OBJECTIVE",
            payload=objective_payload,
        )
        resolved = dict(resolved)
        resolved[objective.evidence_id] = objective
        with self.assertRaisesRegex(ValueError, "desired_notional_currency"):
            self.allocate(
                bundle=(objective, market, capital, stress, resolved),
            )

    def test_same_currency_rejects_mismatched_monetary_unit_declarations(self):
        objective, market, capital, stress, resolved = self.bundle()
        market_payload = dict(market.payload)
        market_payload["monetary_constraint_currency"] = "EUR"
        market = self.evidence(
            evidence_id=market.evidence_id,
            kind="MARKET_CONSTRAINT",
            payload=market_payload,
        )
        resolved = dict(resolved)
        resolved[market.evidence_id] = market
        with self.assertRaisesRegex(ValueError, "quote-currency units"):
            self.allocate(
                bundle=(objective, market, capital, stress, resolved),
            )

    def test_same_currency_rejects_non_unit_identity_fx_evidence(self):
        objective, market, capital, stress, resolved = self.bundle()
        valuation = resolved["valuation:aaa:v1"]
        valuation_payload = dict(valuation.payload)
        valuation_payload["fx_rate"] = "1.01"
        valuation = self.evidence(
            evidence_id=valuation.evidence_id,
            kind="VALUATION",
            payload=valuation_payload,
        )
        resolved = dict(resolved)
        resolved[valuation.evidence_id] = valuation
        with self.assertRaisesRegex(ValueError, "must use rate 1 and IDENTITY source"):
            self.allocate(
                bundle=(objective, market, capital, stress, resolved),
            )

    def test_bound_allocation_is_deterministic_and_revalidates(self):
        bundle = self.bundle()
        result = self.allocate(bundle=bundle)
        self.assertEqual(result.objective.allocation.status, "ALLOCATED")
        self.assertEqual(result.objective.selected_symbols, ("AAA",))
        self.assertEqual(len(result.decision_digest), 64)
        self.assertTrue(
            revalidate_evidence_bound_allocation(
                result,
                resolved_evidence=bundle[-1],
                environment="SIMULATION",
                as_of="2026-09-25T18:40:00Z",
                current_policy_version="risk-policy:12",
                current_policy=self.policy(),
                current_max_candidate_sets=64,
                current_provider_id="SIMULATED",
                current_instrument_versions={"AAA": "instrument:aaa:v3"},
                current_capability_snapshot_ids={"AAA": "capability:1"},
                current_account_id="acct:paper:1",
                current_account_snapshot_id="snapshot:acct:1:v5",
                current_reconciliation_run_id="reconciliation:acct:1:v5",
                current_account_state_version=5,
                current_reservation_state_version=9,
                current_reservation_state_digest="3" * 64,
            )
        )
        repeated = self.allocate(bundle=bundle)
        self.assertEqual(result.decision_digest, repeated.decision_digest)

    def test_revalidation_rejects_forecast_after_its_economic_horizon(self):
        objective, market, capital, stress, resolved = self.bundle()
        horizon_end = "2026-09-25T18:45:00Z"
        objective = self.evidence(
            evidence_id=objective.evidence_id,
            kind=objective.kind,
            payload={
                **objective.payload,
                "forecast_horizon_end": horizon_end,
            },
        )
        valuation = resolved["valuation:aaa:v1"]
        valuation = self.evidence(
            evidence_id=valuation.evidence_id,
            kind=valuation.kind,
            payload={
                **valuation.payload,
                "holding_cost_horizon_end": horizon_end,
            },
        )
        resolved = {
            objective.evidence_id: objective,
            market.evidence_id: market,
            valuation.evidence_id: valuation,
            capital.evidence_id: capital,
            stress.evidence_id: stress,
        }
        bundle = (objective, market, capital, stress, resolved)
        result = self.allocate(bundle=bundle)

        with self.assertRaisesRegex(
            ValueError,
            "forecast horizon expired before admission",
        ):
            revalidate_evidence_bound_allocation(
                result,
                resolved_evidence=resolved,
                environment="SIMULATION",
                as_of="2026-09-25T18:50:00Z",
                current_policy_version="risk-policy:12",
                current_policy=self.policy(),
                current_max_candidate_sets=64,
                current_provider_id="SIMULATED",
                current_instrument_versions={"AAA": "instrument:aaa:v3"},
                current_capability_snapshot_ids={"AAA": "capability:1"},
                current_account_id="acct:paper:1",
                current_account_snapshot_id="snapshot:acct:1:v5",
                current_reconciliation_run_id="reconciliation:acct:1:v5",
                current_account_state_version=5,
                current_reservation_state_version=9,
                current_reservation_state_digest="3" * 64,
            )

    def test_same_evidence_identity_cannot_hide_changed_expected_return(self):
        bundle = self.bundle(objective_rate="0.10")
        with self.assertRaisesRegex(ValueError, "expected_return_rate mismatch"):
            self.allocate(
                candidate=self.candidate(expected_return_rate="0.50"),
                bundle=bundle,
            )

    def test_inflated_cash_available_is_rejected_against_account_truth(self):
        bundle = self.bundle(capital_cash="1000")
        with self.assertRaisesRegex(ValueError, "cash_available"):
            self.allocate(
                policy=self.policy(cash_available="5000"),
                bundle=bundle,
            )

    def test_stress_payload_cannot_change_without_digest_change(self):
        _, _, _, stress, _ = self.bundle()
        altered_payload = dict(stress.payload)
        altered_payload["shocks"] = {"AAA": "-0.01"}
        with self.assertRaisesRegex(ValueError, "digest does not match"):
            ImmutableAllocationEvidence(
                evidence_id=stress.evidence_id,
                kind=stress.kind,
                environment=stress.environment,
                schema_version=stress.schema_version,
                observed_at=stress.observed_at,
                valid_until=stress.valid_until,
                payload=altered_payload,
                digest=stress.digest,
            )

    def test_nested_evidence_payload_is_immutable(self):
        _, _, _, stress, _ = self.bundle()
        with self.assertRaises(TypeError):
            stress.payload["shocks"]["AAA"] = "-0.01"

    def test_stale_market_evidence_rejects_increased_exposure(self):
        bundle = self.bundle(market_valid_until="2026-09-25T18:10:00Z")
        with self.assertRaisesRegex(ValueError, "stale or not yet observable"):
            self.allocate(bundle=bundle)

    def test_simulation_evidence_cannot_cross_into_live(self):
        objective, market, capital, stress, resolved = self.bundle(
            environment="SIMULATION"
        )
        with self.assertRaisesRegex(ValueError, "environment mismatch"):
            allocate_evidence_bound_objective_targets(
                (self.candidate(),),
                self.policy(),
                objective_evidence={"AAA": objective},
                market_evidence={"AAA": market},
                valuation_evidence={"AAA": resolved["valuation:aaa:v1"]},
                capital_evidence=capital,
                stress_source_evidence=(stress,),
                resolved_evidence=resolved,
                environment="LIVE",
                decision_time=self.DECISION_TIME,
                policy_version="risk-policy:12",
            )

    def test_authoritative_resolver_rejects_same_id_with_new_content(self):
        objective, market, capital, stress, resolved = self.bundle()
        altered = self.evidence(
            evidence_id=objective.evidence_id,
            kind="OBJECTIVE",
            payload={
                **dict(objective.payload),
                "expected_return_rate": "0.90",
            },
        )
        resolved = dict(resolved)
        resolved[objective.evidence_id] = altered
        with self.assertRaisesRegex(ValueError, "digest does not match authoritative"):
            allocate_evidence_bound_objective_targets(
                (self.candidate(),),
                self.policy(),
                objective_evidence={"AAA": objective},
                market_evidence={"AAA": market},
                valuation_evidence={"AAA": resolved["valuation:aaa:v1"]},
                capital_evidence=capital,
                stress_source_evidence=(stress,),
                resolved_evidence=resolved,
                environment="SIMULATION",
                decision_time=self.DECISION_TIME,
                policy_version="risk-policy:12",
            )

    def test_account_or_reservation_version_advance_invalidates_proposal(self):
        bundle = self.bundle()
        result = self.allocate(bundle=bundle)
        with self.assertRaisesRegex(ValueError, "account state version advanced"):
            revalidate_evidence_bound_allocation(
                result,
                resolved_evidence=bundle[-1],
                environment="SIMULATION",
                as_of="2026-09-25T18:40:00Z",
                current_policy_version="risk-policy:12",
                current_policy=self.policy(),
                current_max_candidate_sets=64,
                current_provider_id="SIMULATED",
                current_instrument_versions={"AAA": "instrument:aaa:v3"},
                current_capability_snapshot_ids={"AAA": "capability:1"},
                current_account_id="acct:paper:1",
                current_account_snapshot_id="snapshot:acct:1:v5",
                current_reconciliation_run_id="reconciliation:acct:1:v5",
                current_account_state_version=6,
                current_reservation_state_version=9,
                current_reservation_state_digest="3" * 64,
            )
        with self.assertRaisesRegex(ValueError, "reservation state version advanced"):
            revalidate_evidence_bound_allocation(
                result,
                resolved_evidence=bundle[-1],
                environment="SIMULATION",
                as_of="2026-09-25T18:40:00Z",
                current_policy_version="risk-policy:12",
                current_policy=self.policy(),
                current_max_candidate_sets=64,
                current_provider_id="SIMULATED",
                current_instrument_versions={"AAA": "instrument:aaa:v3"},
                current_capability_snapshot_ids={"AAA": "capability:1"},
                current_account_id="acct:paper:1",
                current_account_snapshot_id="snapshot:acct:1:v5",
                current_reconciliation_run_id="reconciliation:acct:1:v5",
                current_account_state_version=5,
                current_reservation_state_version=10,
                current_reservation_state_digest="3" * 64,
            )

    def test_policy_reconciliation_and_reservation_digest_changes_invalidate(self):
        bundle = self.bundle()
        result = self.allocate(bundle=bundle)
        common = {
            "result": result,
            "resolved_evidence": bundle[-1],
            "environment": "SIMULATION",
            "as_of": "2026-09-25T18:40:00Z",
            "current_policy": self.policy(),
            "current_max_candidate_sets": 64,
            "current_provider_id": "SIMULATED",
            "current_instrument_versions": {"AAA": "instrument:aaa:v3"},
            "current_capability_snapshot_ids": {"AAA": "capability:1"},
            "current_account_id": "acct:paper:1",
            "current_account_snapshot_id": "snapshot:acct:1:v5",
            "current_account_state_version": 5,
            "current_reservation_state_version": 9,
        }
        with self.assertRaisesRegex(ValueError, "policy version changed"):
            revalidate_evidence_bound_allocation(
                **common,
                current_policy_version="risk-policy:13",
                current_reconciliation_run_id="reconciliation:acct:1:v5",
                current_reservation_state_digest="3" * 64,
            )
        with self.assertRaisesRegex(ValueError, "reconciliation identity advanced"):
            revalidate_evidence_bound_allocation(
                **common,
                current_policy_version="risk-policy:12",
                current_reconciliation_run_id="reconciliation:acct:1:v6",
                current_reservation_state_digest="3" * 64,
            )
        with self.assertRaisesRegex(ValueError, "reservation state digest changed"):
            revalidate_evidence_bound_allocation(
                **common,
                current_policy_version="risk-policy:12",
                current_reconciliation_run_id="reconciliation:acct:1:v5",
                current_reservation_state_digest="4" * 64,
            )

    def test_portfolio_candidates_must_share_one_forecast_horizon(self):
        objective_a, market_a, capital, stress, resolved = self.bundle()
        valuation_a = resolved["valuation:aaa:v1"]
        horizon_b = "2026-09-27T18:30:00Z"

        objective_b = self.evidence(
            evidence_id="objective:bbb:v1",
            kind="OBJECTIVE",
            payload={
                **objective_a.payload,
                "symbol": "BBB",
                "candidate_id": "candidate:bbb:v1",
                "proposal_id": "proposal:bbb:v1",
                "forecast_horizon_end": horizon_b,
            },
        )
        market_b = self.evidence(
            evidence_id="market:bbb:v1",
            kind="MARKET_CONSTRAINT",
            payload={
                **market_a.payload,
                "symbol": "BBB",
                "instrument_version": "instrument:bbb:v1",
                "capability_snapshot_id": "capability:2",
            },
        )
        valuation_b = self.evidence(
            evidence_id="valuation:bbb:v1",
            kind="VALUATION",
            payload={
                **valuation_a.payload,
                "symbol": "BBB",
                "instrument_version": "instrument:bbb:v1",
                "capability_snapshot_id": "capability:2",
                "holding_cost_horizon_end": horizon_b,
            },
        )
        capital = self.evidence(
            evidence_id=capital.evidence_id,
            kind="CAPITAL_STATE",
            payload={
                **capital.payload,
                "position_quantities": {"AAA": "0", "BBB": "0"},
            },
        )
        stress = self.evidence(
            evidence_id=stress.evidence_id,
            kind="STRESS_SCENARIO",
            payload={
                **stress.payload,
                "shocks": {"AAA": "-0.20", "BBB": "-0.20"},
                "instrument_versions": {
                    "AAA": "instrument:aaa:v3",
                    "BBB": "instrument:bbb:v1",
                },
            },
        )
        resolved = {
            objective_a.evidence_id: objective_a,
            market_a.evidence_id: market_a,
            valuation_a.evidence_id: valuation_a,
            objective_b.evidence_id: objective_b,
            market_b.evidence_id: market_b,
            valuation_b.evidence_id: valuation_b,
            capital.evidence_id: capital,
            stress.evidence_id: stress,
        }
        candidate_b = ObjectiveCandidate.create(
            symbol="BBB",
            desired_notional="500",
            price="10",
            lot_size="1",
            expected_return_rate="0.10",
            risk_penalty_rate="0.01",
            cost_rate="0.001",
            capital_requirement_rate="1",
            min_notional="10",
            fee_floor="0",
            max_executable_notional="500",
        )

        with self.assertRaisesRegex(
            ValueError,
            "objective candidates must share one forecast horizon",
        ):
            allocate_evidence_bound_objective_targets(
                (self.candidate(), candidate_b),
                self.policy(),
                objective_evidence={"AAA": objective_a, "BBB": objective_b},
                market_evidence={"AAA": market_a, "BBB": market_b},
                valuation_evidence={"AAA": valuation_a, "BBB": valuation_b},
                capital_evidence=capital,
                stress_source_evidence=(stress,),
                resolved_evidence=resolved,
                environment="SIMULATION",
                decision_time=self.DECISION_TIME,
                policy_version="risk-policy:12",
            )

    def test_evidence_bound_stress_coverage_ignores_only_zero_unchanged_symbols(self):
        objective_a, market_a, capital, stress, resolved = self.bundle()
        valuation_a = resolved["valuation:aaa:v1"]

        objective_b_payload = dict(objective_a.payload)
        objective_b_payload.update({
            "symbol": "BBB",
            "candidate_id": "candidate:bbb:v1",
            "proposal_id": "proposal:bbb:v1",
            "desired_notional": "0",
            "expected_return_rate": "0",
            "risk_penalty_rate": "0",
        })
        objective_b = self.evidence(
            evidence_id="objective:bbb:v1",
            kind="OBJECTIVE",
            payload=objective_b_payload,
        )

        market_b_payload = dict(market_a.payload)
        market_b_payload.update({
            "symbol": "BBB",
            "instrument_version": "instrument:bbb:v1",
            "capability_snapshot_id": "capability:2",
        })
        market_b = self.evidence(
            evidence_id="market:bbb:v1",
            kind="MARKET_CONSTRAINT",
            payload=market_b_payload,
        )

        valuation_b_payload = dict(valuation_a.payload)
        valuation_b_payload.update({
            "symbol": "BBB",
            "instrument_version": "instrument:bbb:v1",
            "capability_snapshot_id": "capability:2",
            "desired_notional_base": "0",
        })
        valuation_b = self.evidence(
            evidence_id="valuation:bbb:v1",
            kind="VALUATION",
            payload=valuation_b_payload,
        )

        def capital_with_bbb(quantity):
            payload = dict(capital.payload)
            payload["position_quantities"] = {"AAA": "0", "BBB": quantity}
            return self.evidence(
                evidence_id=capital.evidence_id,
                kind="CAPITAL_STATE",
                payload=payload,
            )

        candidate_b = ObjectiveCandidate.create(
            symbol="BBB",
            desired_notional="0",
            price="10",
            lot_size="1",
            expected_return_rate="0",
            risk_penalty_rate="0",
            cost_rate="0.001",
            capital_requirement_rate="1",
            min_notional="10",
            fee_floor="0",
            max_executable_notional="500",
        )

        zero_capital = capital_with_bbb("0")
        zero_resolved = {
            **resolved,
            objective_b.evidence_id: objective_b,
            market_b.evidence_id: market_b,
            valuation_b.evidence_id: valuation_b,
            zero_capital.evidence_id: zero_capital,
        }
        result = allocate_evidence_bound_objective_targets(
            (self.candidate(), candidate_b),
            self.policy(),
            objective_evidence={"AAA": objective_a, "BBB": objective_b},
            market_evidence={"AAA": market_a, "BBB": market_b},
            valuation_evidence={"AAA": valuation_a, "BBB": valuation_b},
            capital_evidence=zero_capital,
            stress_source_evidence=(stress,),
            resolved_evidence=zero_resolved,
            environment="SIMULATION",
            decision_time=self.DECISION_TIME,
            policy_version="risk-policy:12",
        )
        self.assertEqual(result.objective.allocation.status, "ALLOCATED")
        self.assertEqual(result.objective.selected_symbols, ("AAA",))

        exposed_capital = capital_with_bbb("1")
        exposed_resolved = {
            **zero_resolved,
            exposed_capital.evidence_id: exposed_capital,
        }
        with self.assertRaisesRegex(
            ValueError,
            "stress evidence must cover every current/requested exposure symbol",
        ):
            allocate_evidence_bound_objective_targets(
                (self.candidate(), candidate_b),
                self.policy(),
                objective_evidence={"AAA": objective_a, "BBB": objective_b},
                market_evidence={"AAA": market_a, "BBB": market_b},
                valuation_evidence={"AAA": valuation_a, "BBB": valuation_b},
                capital_evidence=exposed_capital,
                stress_source_evidence=(stress,),
                resolved_evidence=exposed_resolved,
                environment="SIMULATION",
                decision_time=self.DECISION_TIME,
                policy_version="risk-policy:12",
            )

    def test_same_policy_version_cannot_hide_policy_configuration_change(self):
        bundle = self.bundle()
        result = self.allocate(bundle=bundle)
        changed_policy = AllocationPolicy.create(
            cash_available="1000",
            max_gross_notional="1000",
            max_net_notional="1000",
            max_symbol_notional="1000",
            max_total_cost="50",
            max_stress_loss="500",
            max_turnover_notional="1000",
            require_adverse_stress_evidence=True,
            require_fresh_stress_evidence=True,
            max_execution_states=9999,
        )
        with self.assertRaisesRegex(ValueError, "policy configuration changed"):
            revalidate_evidence_bound_allocation(
                result,
                resolved_evidence=bundle[-1],
                environment="SIMULATION",
                as_of="2026-09-25T18:40:00Z",
                current_policy_version="risk-policy:12",
                current_policy=changed_policy,
                current_max_candidate_sets=64,
                current_provider_id="SIMULATED",
                current_instrument_versions={"AAA": "instrument:aaa:v3"},
                current_capability_snapshot_ids={"AAA": "capability:1"},
                current_account_id="acct:paper:1",
                current_account_snapshot_id="snapshot:acct:1:v5",
                current_reconciliation_run_id="reconciliation:acct:1:v5",
                current_account_state_version=5,
                current_reservation_state_version=9,
                current_reservation_state_digest="3" * 64,
            )

    def test_same_policy_version_cannot_hide_objective_search_budget_change(self):
        bundle = self.bundle()
        result = self.allocate(bundle=bundle)
        with self.assertRaisesRegex(ValueError, "objective search configuration changed"):
            revalidate_evidence_bound_allocation(
                result,
                resolved_evidence=bundle[-1],
                environment="SIMULATION",
                as_of="2026-09-25T18:40:00Z",
                current_policy_version="risk-policy:12",
                current_policy=self.policy(),
                current_max_candidate_sets=65,
                current_provider_id="SIMULATED",
                current_instrument_versions={"AAA": "instrument:aaa:v3"},
                current_capability_snapshot_ids={"AAA": "capability:1"},
                current_account_id="acct:paper:1",
                current_account_snapshot_id="snapshot:acct:1:v5",
                current_reconciliation_run_id="reconciliation:acct:1:v5",
                current_account_state_version=5,
                current_reservation_state_version=9,
                current_reservation_state_digest="3" * 64,
            )

    def test_provider_capability_and_instrument_scope_are_current_at_admission(self):
        with self.assertRaisesRegex(ValueError, "capital evidence provider_id"):
            self.allocate(bundle=self.bundle(capital_provider="OTHER_PROVIDER"))

        bundle = self.bundle()
        result = self.allocate(bundle=bundle)
        common = {
            "result": result,
            "resolved_evidence": bundle[-1],
            "environment": "SIMULATION",
            "as_of": "2026-09-25T18:40:00Z",
            "current_policy_version": "risk-policy:12",
            "current_policy": self.policy(),
            "current_max_candidate_sets": 64,
            "current_provider_id": "SIMULATED",
            "current_instrument_versions": {"AAA": "instrument:aaa:v3"},
            "current_capability_snapshot_ids": {"AAA": "capability:1"},
            "current_account_id": "acct:paper:1",
            "current_account_snapshot_id": "snapshot:acct:1:v5",
            "current_reconciliation_run_id": "reconciliation:acct:1:v5",
            "current_account_state_version": 5,
            "current_reservation_state_version": 9,
            "current_reservation_state_digest": "3" * 64,
        }
        with self.assertRaisesRegex(ValueError, "provider identity changed"):
            revalidate_evidence_bound_allocation(
                **{**common, "current_provider_id": "OTHER_PROVIDER"}
            )
        with self.assertRaisesRegex(ValueError, "instrument version scope changed"):
            revalidate_evidence_bound_allocation(
                **{
                    **common,
                    "current_instrument_versions": {"AAA": "instrument:aaa:v4"},
                }
            )
        with self.assertRaisesRegex(ValueError, "capability snapshot scope changed"):
            revalidate_evidence_bound_allocation(
                **{
                    **common,
                    "current_capability_snapshot_ids": {"AAA": "capability:2"},
                }
            )

    def test_revalidation_cannot_move_before_proposal_time(self):
        bundle = self.bundle()
        result = self.allocate(bundle=bundle)
        with self.assertRaisesRegex(ValueError, "precedes proposal decision_time"):
            revalidate_evidence_bound_allocation(
                result,
                resolved_evidence=bundle[-1],
                environment="SIMULATION",
                as_of="2026-09-25T18:29:59Z",
                current_policy_version="risk-policy:12",
                current_policy=self.policy(),
                current_max_candidate_sets=64,
                current_provider_id="SIMULATED",
                current_instrument_versions={"AAA": "instrument:aaa:v3"},
                current_capability_snapshot_ids={"AAA": "capability:1"},
                current_account_id="acct:paper:1",
                current_account_snapshot_id="snapshot:acct:1:v5",
                current_reconciliation_run_id="reconciliation:acct:1:v5",
                current_account_state_version=5,
                current_reservation_state_version=9,
                current_reservation_state_digest="3" * 64,
            )

    def test_evidence_expiry_before_admission_invalidates_proposal(self):
        bundle = self.bundle()
        result = self.allocate(bundle=bundle)
        with self.assertRaisesRegex(ValueError, "stale at admission"):
            revalidate_evidence_bound_allocation(
                result,
                resolved_evidence=bundle[-1],
                environment="SIMULATION",
                as_of="2026-09-25T19:01:00Z",
                current_policy_version="risk-policy:12",
                current_policy=self.policy(),
                current_max_candidate_sets=64,
                current_provider_id="SIMULATED",
                current_instrument_versions={"AAA": "instrument:aaa:v3"},
                current_capability_snapshot_ids={"AAA": "capability:1"},
                current_account_id="acct:paper:1",
                current_account_snapshot_id="snapshot:acct:1:v5",
                current_reconciliation_run_id="reconciliation:acct:1:v5",
                current_account_state_version=5,
                current_reservation_state_version=9,
                current_reservation_state_digest="3" * 64,
            )


    def test_valuation_evidence_identity_is_part_of_decision_digest(self):
        bundle = self.bundle()
        first = self.allocate(bundle=bundle)
        objective, market, capital, stress, resolved = bundle
        original = resolved["valuation:aaa:v1"]
        payload = dict(original.payload)
        cost_refs = dict(payload["cost_evidence_refs"])
        cost_refs["execution"] = "execution-cost:aaa:v2"
        payload["cost_evidence_refs"] = cost_refs
        changed = self.evidence(
            evidence_id="valuation:aaa:v2",
            kind="VALUATION",
            payload=payload,
        )
        changed_resolved = {
            key: value
            for key, value in resolved.items()
            if key != original.evidence_id
        }
        changed_resolved[changed.evidence_id] = changed
        second = allocate_evidence_bound_objective_targets(
            (self.candidate(),),
            self.policy(),
            objective_evidence={"AAA": objective},
            market_evidence={"AAA": market},
            valuation_evidence={"AAA": changed},
            capital_evidence=capital,
            stress_source_evidence=(stress,),
            resolved_evidence=changed_resolved,
            environment="SIMULATION",
            decision_time=self.DECISION_TIME,
            policy_version="risk-policy:12",
        )
        self.assertNotEqual(first.decision_digest, second.decision_digest)
        self.assertEqual(first.objective.allocation, second.objective.allocation)

    def test_valuation_evidence_must_still_be_fresh_at_admission(self):
        objective, market, capital, stress, resolved = self.bundle()
        original = resolved["valuation:aaa:v1"]
        expiring = self.evidence(
            evidence_id=original.evidence_id,
            kind="VALUATION",
            payload=original.payload,
            valid_until="2026-09-25T18:35:00Z",
        )
        expiring_resolved = dict(resolved)
        expiring_resolved[expiring.evidence_id] = expiring
        result = allocate_evidence_bound_objective_targets(
            (self.candidate(),),
            self.policy(),
            objective_evidence={"AAA": objective},
            market_evidence={"AAA": market},
            valuation_evidence={"AAA": expiring},
            capital_evidence=capital,
            stress_source_evidence=(stress,),
            resolved_evidence=expiring_resolved,
            environment="SIMULATION",
            decision_time=self.DECISION_TIME,
            policy_version="risk-policy:12",
        )
        with self.assertRaisesRegex(ValueError, "stale at admission"):
            revalidate_evidence_bound_allocation(
                result,
                resolved_evidence=expiring_resolved,
                environment="SIMULATION",
                as_of="2026-09-25T18:40:00Z",
                current_policy_version="risk-policy:12",
                current_policy=self.policy(),
                current_max_candidate_sets=64,
                current_provider_id="SIMULATED",
                current_instrument_versions={"AAA": "instrument:aaa:v3"},
                current_capability_snapshot_ids={"AAA": "capability:1"},
                current_account_id="acct:paper:1",
                current_account_snapshot_id="snapshot:acct:1:v5",
                current_reconciliation_run_id="reconciliation:acct:1:v5",
                current_account_state_version=5,
                current_reservation_state_version=9,
                current_reservation_state_digest="3" * 64,
            )


if __name__ == "__main__":
    unittest.main()
