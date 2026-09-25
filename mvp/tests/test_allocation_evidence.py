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
                "price": "10",
                "lot_size": "1",
                "cost_rate": "0.001",
                "capital_requirement_rate": "1",
                "min_notional": "10",
                "fee_floor": "0",
                "max_executable_notional": "500",
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
            for item in (objective, market, capital, stress)
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
            capital_evidence=capital,
            stress_source_evidence=(stress,),
            resolved_evidence=resolved,
            environment=environment,
            decision_time=self.DECISION_TIME,
            policy_version="risk-policy:12",
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


if __name__ == "__main__":
    unittest.main()
