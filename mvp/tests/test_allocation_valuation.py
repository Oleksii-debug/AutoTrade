import unittest
from decimal import Decimal

from mvp.autotrade_mvp.allocation import (
    AllocationCandidate,
    AllocationPolicy,
    CandidateEconomicValuation,
    ImmutableAllocationEvidence,
    ObjectiveCandidate,
    allocate_evidence_bound_objective_targets,
    allocate_targets,
)


DECISION = "2026-09-25T18:30:00Z"
OBSERVED = "2026-09-25T18:00:00Z"
VALID = "2026-09-25T19:00:00Z"


def policy(*, cash="1000000"):
    return AllocationPolicy.create(
        cash_available=cash,
        max_gross_notional=cash,
        max_net_notional=cash,
        max_symbol_notional=cash,
        max_total_cost="10000",
        max_stress_loss=cash,
        require_adverse_stress_evidence=False,
    )


def valuation(
    symbol,
    *,
    instrument_version=None,
    payoff_kind="CASH_EQUITY",
    quote="USD",
    settlement=None,
    base="USD",
    multiplier="1",
    fx="1",
    identity=None,
    fx_evidence_id=None,
    observed_at=OBSERVED,
    valid_until=VALID,
):
    return CandidateEconomicValuation.create(
        symbol=symbol,
        instrument_version=instrument_version or f"instrument:{symbol}:v1",
        payoff_kind=payoff_kind,
        quote_currency=quote,
        settlement_currency=settlement or quote,
        base_currency=base,
        contract_multiplier=multiplier,
        fx_rate_to_base=fx,
        valuation_identity=identity or f"valuation:{symbol}:v1",
        fx_evidence_id=fx_evidence_id,
        observed_at=observed_at,
        valid_until=valid_until,
    )


class AllocationEconomicUnitsTests(unittest.TestCase):
    def test_same_currency_equity_remains_numerically_unchanged(self):
        legacy = AllocationCandidate.create(
            symbol="AAA",
            desired_notional="500",
            price="10",
            lot_size="1",
        )
        valued = AllocationCandidate.create(
            symbol="AAA",
            desired_notional="500",
            price="10",
            lot_size="1",
            valuation=valuation("AAA"),
        )
        legacy_result = allocate_targets(
            (legacy,),
            policy(cash="1000"),
            decision_time=DECISION,
        )
        valued_result = allocate_targets(
            (valued,),
            policy(cash="1000"),
            decision_time=DECISION,
        )
        self.assertEqual(legacy_result.targets[0].quantity, Decimal("50"))
        self.assertEqual(valued_result.targets[0].quantity, Decimal("50"))
        self.assertEqual(legacy_result.targets[0].notional, Decimal("500"))
        self.assertEqual(valued_result.targets[0].notional, Decimal("500"))
        self.assertEqual(valued_result.targets[0].base_currency, "USD")

    def test_cross_currency_equity_uses_exact_fx_before_portfolio_aggregation(self):
        candidate = AllocationCandidate.create(
            symbol="EUR_STOCK",
            desired_notional="1200",
            price="100",
            lot_size="1",
            valuation=valuation(
                "EUR_STOCK",
                quote="EUR",
                base="USD",
                fx="1.2",
                fx_evidence_id="fx:EURUSD:20260925T1800",
            ),
        )
        result = allocate_targets(
            (candidate,),
            policy(cash="2000"),
            decision_time=DECISION,
        )
        self.assertEqual(result.status, "ALLOCATED")
        self.assertEqual(result.targets[0].quantity, Decimal("10"))
        self.assertEqual(result.targets[0].notional, Decimal("1200"))
        self.assertEqual(result.gross_notional, Decimal("1200"))
        self.assertEqual(result.targets[0].base_currency, "USD")

    def test_linear_future_includes_contract_multiplier(self):
        candidate = AllocationCandidate.create(
            symbol="ES",
            desired_notional="500000",
            price="5000",
            lot_size="1",
            capital_requirement_rate="0.1",
            valuation=valuation(
                "ES",
                payoff_kind="LINEAR_FUTURE",
                multiplier="50",
            ),
        )
        result = allocate_targets(
            (candidate,),
            policy(cash="1000000"),
            decision_time=DECISION,
        )
        self.assertEqual(result.status, "ALLOCATED")
        self.assertEqual(result.targets[0].quantity, Decimal("2"))
        self.assertEqual(result.targets[0].notional, Decimal("500000"))
        self.assertEqual(result.cash_required, Decimal("50000"))

    def test_inverse_and_option_shortcuts_fail_closed(self):
        for payoff in ("INVERSE_FUTURE", "OPTION"):
            with self.subTest(payoff=payoff):
                candidate = AllocationCandidate.create(
                    symbol=payoff,
                    desired_notional="1000",
                    price="100",
                    lot_size="1",
                    valuation=valuation(payoff, payoff_kind=payoff),
                )
                result = allocate_targets(
                    (candidate,),
                    policy(cash="2000"),
                    decision_time=DECISION,
                )
                self.assertEqual(result.status, "NO_INCREASE_FALLBACK")
                self.assertIn("canonical nonlinear valuation", result.reason)
                self.assertEqual(result.gross_notional, Decimal("0"))

    def test_mixed_portfolio_base_currencies_fail_closed(self):
        usd = AllocationCandidate.create(
            symbol="USD_ASSET",
            desired_notional="100",
            price="10",
            lot_size="1",
            valuation=valuation("USD_ASSET", base="USD"),
        )
        eur = AllocationCandidate.create(
            symbol="EUR_ASSET",
            desired_notional="100",
            price="10",
            lot_size="1",
            valuation=valuation("EUR_ASSET", quote="EUR", base="EUR"),
        )
        result = allocate_targets(
            (usd, eur),
            policy(cash="1000"),
            decision_time=DECISION,
        )
        self.assertEqual(result.status, "NO_INCREASE_FALLBACK")
        self.assertIn("one portfolio base currency", result.reason)

    def test_cross_currency_requires_immutable_fx_identity(self):
        with self.assertRaisesRegex(ValueError, "fx_evidence_id"):
            valuation(
                "EUR_STOCK",
                quote="EUR",
                base="USD",
                fx="1.2",
            )

    def test_binary_float_is_rejected_at_valuation_boundary(self):
        with self.assertRaises(TypeError):
            valuation("AAA", multiplier=1.5)
        with self.assertRaises(TypeError):
            valuation("AAA", fx=1.1)


class EvidenceBoundValuationTests(unittest.TestCase):
    def evidence(self, evidence_id, kind, payload, *, observed_at=OBSERVED, valid_until=VALID):
        return ImmutableAllocationEvidence.create(
            evidence_id=evidence_id,
            kind=kind,
            environment="SIMULATION",
            schema_version="1.0.0",
            observed_at=observed_at,
            valid_until=valid_until,
            payload=payload,
        )

    def bundle(self, *, fx="1.2", multiplier="1", payoff="CASH_EQUITY", valuation_valid_until=VALID):
        symbol = "AAA"
        instrument = "instrument:AAA:v3"
        candidate_valuation = valuation(
            symbol,
            instrument_version=instrument,
            payoff_kind=payoff,
            quote="EUR",
            settlement="EUR",
            base="USD",
            multiplier=multiplier,
            fx=fx,
            fx_evidence_id="fx:EURUSD:v7",
            identity="valuation:AAA:v7",
            valid_until=valuation_valid_until,
        )
        candidate = ObjectiveCandidate.create(
            symbol=symbol,
            desired_notional="1200",
            price="100",
            lot_size="1",
            expected_return_rate="0.10",
            risk_penalty_rate="0.01",
            cost_rate="0.004",
            capital_requirement_rate="1",
            min_notional="0",
            fee_floor="2",
            max_executable_notional="1200",
            valuation=candidate_valuation,
        )
        objective = self.evidence(
            "objective:AAA:v1",
            "OBJECTIVE",
            {
                "symbol": symbol,
                "candidate_id": "candidate:AAA:v1",
                "proposal_id": "proposal:AAA:v1",
                "strategy_version": "strategy:v1",
                "protocol_digest": "1" * 64,
                "input_snapshot_digest": "2" * 64,
                "information_cutoff": "2026-09-25T18:20:00Z",
                "desired_notional": "1200",
                "expected_return_rate": "0.10",
                "risk_penalty_rate": "0.01",
            },
        )
        market = self.evidence(
            "market:AAA:v1",
            "MARKET_CONSTRAINT",
            {
                "symbol": symbol,
                "instrument_version": instrument,
                "provider_id": "SIMULATED",
                "account_id": "acct:1",
                "capability_snapshot_id": "capability:1",
                "source_as_of": "2026-09-25T18:15:00Z",
                "price": "100",
                "lot_size": "1",
                "cost_rate": "0.004",
                "capital_requirement_rate": "1",
                "min_notional": "0",
                "fee_floor": "2",
                "max_executable_notional": "1200",
            },
        )
        capital = self.evidence(
            "capital:acct:v1",
            "CAPITAL_STATE",
            {
                "provider_id": "SIMULATED",
                "account_id": "acct:1",
                "account_snapshot_id": "snapshot:1",
                "reconciliation_run_id": "reconciliation:1",
                "account_state_version": 1,
                "reservation_state_version": 2,
                "reservation_state_digest": "3" * 64,
                "cash_available": "2000",
                "base_currency": "USD",
            },
        )
        stress = self.evidence(
            "stress:AAA:v1",
            "STRESS_SCENARIO",
            {
                "name": "down",
                "shocks": {symbol: "-0.10"},
                "instrument_versions": {symbol: instrument},
            },
        )
        valuation_record = self.evidence(
            f"valuation:AAA:evidence:{fx}:{multiplier}:{payoff}",
            "VALUATION",
            {
                "symbol": symbol,
                "instrument_version": instrument,
                "payoff_kind": payoff,
                "quote_currency": "EUR",
                "settlement_currency": "EUR",
                "base_currency": "USD",
                "contract_multiplier": multiplier,
                "fx_rate_to_base": fx,
                "valuation_identity": "valuation:AAA:v7",
                "fx_evidence_id": "fx:EURUSD:v7",
                "price": "100",
                "desired_notional_currency": "USD",
                "execution_cost_rate": "0.001",
                "financing_cost_rate": "0.001",
                "funding_cost_rate": "0.001",
                "borrow_cost_rate": "0.001",
                "fee_floor_base": "2",
            },
            valid_until=valuation_valid_until,
        )
        resolved = {
            item.evidence_id: item
            for item in (objective, market, capital, stress, valuation_record)
        }
        return candidate, objective, market, capital, stress, valuation_record, resolved

    def allocate(self, bundle):
        candidate, objective, market, capital, stress, valuation_record, resolved = bundle
        return allocate_evidence_bound_objective_targets(
            (candidate,),
            policy(cash="2000"),
            objective_evidence={"AAA": objective},
            market_evidence={"AAA": market},
            capital_evidence=capital,
            stress_source_evidence=(stress,),
            valuation_evidence={"AAA": valuation_record},
            resolved_evidence=resolved,
            environment="SIMULATION",
            decision_time=DECISION,
            policy_version="risk-policy:v1",
        )

    def test_evidence_bound_fx_and_multiplier_enter_target_and_digest(self):
        fx_a = self.allocate(self.bundle(fx="1.2"))
        fx_b = self.allocate(self.bundle(fx="1.1"))
        self.assertEqual(fx_a.base_currency, "USD")
        self.assertEqual(fx_a.objective.allocation.targets[0].base_currency, "USD")
        self.assertNotEqual(fx_a.decision_digest, fx_b.decision_digest)
        self.assertNotEqual(
            fx_a.objective.allocation.targets[0].quantity,
            fx_b.objective.allocation.targets[0].quantity,
        )

        future_a = self.allocate(self.bundle(multiplier="5", payoff="LINEAR_FUTURE"))
        future_b = self.allocate(self.bundle(multiplier="10", payoff="LINEAR_FUTURE"))
        self.assertNotEqual(future_a.decision_digest, future_b.decision_digest)

    def test_missing_valuation_evidence_fails_before_allocation(self):
        candidate, objective, market, capital, stress, valuation_record, resolved = self.bundle()
        with self.assertRaisesRegex(ValueError, "valuation evidence"):
            allocate_evidence_bound_objective_targets(
                (candidate,),
                policy(cash="2000"),
                objective_evidence={"AAA": objective},
                market_evidence={"AAA": market},
                capital_evidence=capital,
                stress_source_evidence=(stress,),
                resolved_evidence=resolved,
                environment="SIMULATION",
                decision_time=DECISION,
                policy_version="risk-policy:v1",
            )

    def test_stale_valuation_evidence_fails_closed(self):
        bundle = self.bundle(valuation_valid_until="2026-09-25T18:10:00Z")
        with self.assertRaisesRegex(ValueError, "stale|observable"):
            self.allocate(bundle)

    def test_nonlinear_evidence_is_rejected_not_approximated(self):
        with self.assertRaisesRegex(ValueError, "not qualified"):
            self.allocate(self.bundle(payoff="OPTION"))


if __name__ == "__main__":
    unittest.main()
