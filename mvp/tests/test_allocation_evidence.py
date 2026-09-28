from decimal import Decimal
import unittest

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
        with self.assertRaisesRegex(ValueError, "must use unit FX"):
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
