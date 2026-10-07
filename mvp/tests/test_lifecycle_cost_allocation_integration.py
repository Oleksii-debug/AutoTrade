from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest

from mvp.autotrade_mvp.allocation import (
    AllocationCandidate,
    AllocationPolicy,
    allocate_targets,
)
from mvp.autotrade_mvp.authority import AuthoritativeRiskSnapshot
from mvp.autotrade_mvp.lifecycle_cost import (
    LifecycleCostComponent,
    LifecycleCostProfile,
    LifecycleCostRequirements,
    allocation_cost_split,
)
from mvp.autotrade_mvp.simulation_session import _simulation_lifecycle_cost_split
from research.autotrade_research.strategies.deterministic import StrategyEconomicsBinding
from research.autotrade_research.evaluation.gates import (
    EvaluationEvidence,
    GateEvidenceRef,
    GateProfile,
    evaluate_gates,
)
from mvp.tests.test_authority import (
    public_authoritative_risk_snapshot,
    public_risk_authority_request,
)


AS_OF = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)
HORIZON = AS_OF + timedelta(days=30)
VALID_UNTIL = HORIZON + timedelta(days=30)

INSTRUMENT_VERSION = "11111111-1111-4111-8111-111111111111@1"


def split_for_profile(profile):
    return allocation_cost_split(
        profile,
        decision_scope_ref=profile.decision_scope_ref,
        instrument_version=INSTRUMENT_VERSION,
        decision_time=AS_OF,
        horizon_end=HORIZON,
    )



class LifecycleCostAllocationIntegrationTests(unittest.TestCase):
    def test_zero_simulator_consumes_frozen_complete_lifecycle_cost_projection(self):
        split = _simulation_lifecycle_cost_split(
            decision_scope_ref="simulation:decision:test",
            decision_time=AS_OF,
            horizon_end=HORIZON,
        )

        self.assertEqual(split.turnover_cost_rate, Decimal("0.001"))
        self.assertEqual(split.holding_cost_rate, Decimal("0"))
        self.assertEqual(split.cost_rate, Decimal("0.001"))
        self.assertEqual(
            split.requirements_ref,
            "simulation:lifecycle-cost-requirements:v1",
        )
        self.assertEqual(
            {kind for kind, _ref in split.component_evidence_refs},
            {
                "COMMISSION",
                "EXCHANGE_FEE",
                "SPREAD",
                "SLIPPAGE",
                "FX_CONVERSION",
                "TRANSACTION_TAX",
                "FUNDING",
                "BORROW",
                "FINANCING",
                "CARRY",
                "STORAGE",
            },
        )

    def test_same_lifecycle_digest_is_admissible_as_research_cost_dimension(self):
        split = split_for_profile(self.profile())
        capacity_digest = "sha256:" + "2" * 64
        binding = StrategyEconomicsBinding(
            strategy_fingerprint="sha256:" + "3" * 64,
            strategy_configuration_fingerprint="sha256:" + "4" * 64,
            instrument_version=INSTRUMENT_VERSION,
            information_cutoff=AS_OF,
            decision_time=AS_OF,
            horizon_seconds=int((HORIZON - AS_OF).total_seconds()),
            expiry=HORIZON,
            available_at=AS_OF,
            input_manifest_refs=("sha256:" + "5" * 64,),
            gross_return_distribution_sha256="sha256:" + "6" * 64,
            after_cost_return_distribution_sha256="sha256:" + "7" * 64,
            after_cost_lower_bound="0.01",
            execution_model_fingerprint="sha256:" + "8" * 64,
            execution_calibration_sha256="sha256:" + "9" * 64,
            execution_fidelity="SIMULATION",
            capacity_assessment_sha256=capacity_digest,
            max_feasible_quantity="2",
            lot_size="1",
            required_evidence_dimensions=("COST", "CAPACITY"),
            dimension_evidence=(
                ("COST", split.lifecycle_cost_digest),
                ("CAPACITY", capacity_digest),
            ),
            status="QUALIFIED",
        )

        self.assertEqual(
            dict(binding.dimension_evidence)["COST"],
            split.lifecycle_cost_digest,
        )
        self.assertEqual(binding.missing_dimensions, ())

    def test_authoritative_risk_identity_binds_exact_lifecycle_cost_digest(self):
        split = split_for_profile(self.profile())
        base = public_authoritative_risk_snapshot(public_risk_authority_request())
        refs = dict(base.evidence_refs.items())
        refs["COST"] = split.lifecycle_cost_digest
        values = dict(vars(base))
        values["evidence_refs"] = refs

        bound = AuthoritativeRiskSnapshot(**values)

        self.assertEqual(
            dict(bound.evidence_refs.items())["COST"],
            split.lifecycle_cost_digest,
        )
        self.assertNotEqual(bound.snapshot_id, base.snapshot_id)

    def test_lifecycle_cost_digest_reaches_science_but_cannot_self_issue_pass(self):
        split = split_for_profile(self.profile())
        profile = GateProfile.create(
            profile_id="section17-science-profile",
            minimum_net_advantage="0.01",
            max_drawdown="0.50",
            max_adverse_cost_loss="1",
            min_power="0.50",
            primary_baseline_id="NO_TRADE",
            baseline_ids=("NO_TRADE",),
            selection_correction="bonferroni",
            max_trials=1,
            required_regimes=("BASE",),
        )
        evidence = EvaluationEvidence.create(
            registered_profile_id=profile.profile_id,
            profile_unchanged_after_results=True,
            reproducible=True,
            causal_audit_passed=True,
            financial_invariants_passed=True,
            trial_log_complete=True,
            dependence_aware_lower_bound="0.10",
            estimated_power="0.90",
            net_advantage="0.20",
            drawdown="0.01",
            adverse_cost_loss="0",
            retention_passed=True,
            baseline_advantages={"NO_TRADE": "0.20"},
            selection_correction_applied="bonferroni",
            trials_attempted=1,
            regime_coverage=frozenset({"BASE"}),
            untouched_holdout_passed=True,
            valid_sequential_evaluation_passed=False,
            walk_forward_passed=True,
            evidence_refs={
                "cost": GateEvidenceRef(
                    artifact_id="11111111-1111-4111-8111-111111111111",
                    sha256=split.lifecycle_cost_digest,
                ),
            },
        )

        self.assertEqual(
            evidence.evidence_refs["cost"].sha256,
            split.lifecycle_cost_digest,
        )
        decision = evaluate_gates(profile, evidence)
        self.assertEqual(decision.checks["cost_stress"], "PASS")
        self.assertEqual(decision.status, "INCONCLUSIVE")
        self.assertEqual(decision.checks["evidence_bundle"], "INCONCLUSIVE")
        self.assertEqual(decision.checks["semantic_owner_evidence"], "INCONCLUSIVE")

    def component(self, component_id, phase, kind, rate):
        return LifecycleCostComponent(
            component_id=component_id,
            phase=phase,
            kind=kind,
            normalized_rate=rate,
            evidence_ref=f"evidence:{component_id}",
            observed_at=AS_OF - timedelta(minutes=1),
            valid_until=VALID_UNTIL,
        )

    def profile(self):
        return LifecycleCostProfile(
            profile_id="gold-fund-costs",
            decision_scope_ref="decision-scope:test:v1",
            instrument_version=INSTRUMENT_VERSION,
            as_of=AS_OF,
            horizon_end=HORIZON,
            requirements=LifecycleCostRequirements(
                requirements_ref="requirements:test:v1",
                required_kinds=("COMMISSION", "SPREAD", "FUNDING", "BORROW")
            ),
            components=(
                self.component("commission", "TURNOVER", "COMMISSION", "0.001"),
                self.component("spread", "TURNOVER", "SPREAD", "0.002"),
                self.component("funding", "HOLDING", "FUNDING", "0.003"),
                self.component("borrow", "HOLDING", "BORROW", "0.004"),
            ),
        )

    def policy(self):
        return AllocationPolicy.create(
            cash_available="1000",
            max_gross_notional="1000",
            max_net_notional="1000",
            max_symbol_notional="1000",
            max_total_cost="10",
            max_stress_loss="1000",
            require_adverse_stress_evidence=False,
            require_fresh_stress_evidence=False,
        )

    def test_allocator_charges_turnover_on_delta_and_holding_on_final_exposure(self):
        split = split_for_profile(self.profile())
        candidate = AllocationCandidate.create(
            symbol="GOLD-FUND",
            desired_notional="200",
            price="100",
            lot_size="1",
            current_quantity="1",
            cost_rate=split.cost_rate,
            turnover_cost_rate=split.turnover_cost_rate,
            holding_cost_rate=split.holding_cost_rate,
        )

        result = allocate_targets((candidate,), self.policy())

        self.assertEqual(result.status, "ALLOCATED")
        self.assertEqual(result.turnover_notional, Decimal("100"))
        self.assertEqual(result.gross_notional, Decimal("200"))
        # 100 delta * .003 turnover + 200 final exposure * .007 holding.
        self.assertEqual(result.estimated_cost, Decimal("1.700"))
        self.assertEqual(result.targets[0].estimated_cost, Decimal("1.700"))
        self.assertEqual(result.targets[0].quantity, Decimal("2"))
        self.assertEqual(result.targets[0].notional, Decimal("200"))
        self.assertEqual(result.cash_required, Decimal("201.700"))

    def test_rebate_evidence_cannot_reduce_allocator_decision_cost(self):
        profile = LifecycleCostProfile(
            profile_id="rebate-profile",
            decision_scope_ref="decision-scope:test:v1",
            instrument_version=INSTRUMENT_VERSION,
            as_of=AS_OF,
            horizon_end=HORIZON,
            requirements=LifecycleCostRequirements(
                requirements_ref="requirements:test:v1",
                required_kinds=("COMMISSION", "SPREAD", "FUNDING")
            ),
            components=(
                self.component("rebate", "TURNOVER", "COMMISSION", "-0.005"),
                self.component("spread", "TURNOVER", "SPREAD", "0.002"),
                self.component("funding", "HOLDING", "FUNDING", "-0.003"),
            ),
        )
        split = split_for_profile(profile)
        candidate = AllocationCandidate.create(
            symbol="GOLD-FUND",
            desired_notional="200",
            price="100",
            lot_size="1",
            current_quantity="1",
            cost_rate=split.cost_rate,
            turnover_cost_rate=split.turnover_cost_rate,
            holding_cost_rate=split.holding_cost_rate,
        )

        result = allocate_targets((candidate,), self.policy())

        self.assertEqual(profile.net_expected_rate, Decimal("-0.006"))
        self.assertEqual(split.turnover_cost_rate, Decimal("0.002"))
        self.assertEqual(split.holding_cost_rate, Decimal("0"))
        self.assertEqual(result.estimated_cost, Decimal("0.200"))
        self.assertGreaterEqual(result.estimated_cost, Decimal("0"))


if __name__ == "__main__":
    unittest.main()
