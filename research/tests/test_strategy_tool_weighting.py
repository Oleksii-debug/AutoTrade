from dataclasses import replace
from decimal import Decimal
from fractions import Fraction
from types import MappingProxyType
import unittest

from research.autotrade_research.evaluation.gates import GateDecision
from research.autotrade_research.learning.generalization import (
    AssetClassProfile,
    CrossMarketGeneralizationAssessment,
    CrossMarketTrainingProtocol,
    MarketRegimeCell,
)
from research.autotrade_research.strategies.deterministic import StrategyDescriptor
from research.autotrade_research.strategies.tool_weighting import (
    ExactWeight,
    StrategyCellEvidence,
    StrategyToolCell,
    StrategyToolPolicy,
    StrategyToolWeightingError,
    assess_strategy_tools,
    strategy_cell_metrics_digest,
)


BUILD = "a74a02582afd0c6155bbbcf3d07aac3eca65f590"
POPULATION_PROTOCOL = "sha256:" + "9" * 64
METRICS = "sha256:" + "8" * 64


def descriptor(
    family,
    marker,
    *,
    regimes=("bear", "bull"),
    horizon=3600,
    evaluation_marker="d",
):
    return StrategyDescriptor(
        strategy_id=family.lower(),
        version=1,
        family=family,
        feature_schema=f"{family.lower()}-features-v1",
        market_requirements=("CAUSAL_PRICE",),
        minimum_history=20,
        horizon_seconds=horizon,
        decision_schedule="ON_REGISTERED_CUTOFF",
        proposal_semantics="BUY_SELL_HOLD_RESEARCH_PROPOSAL",
        parameter_bounds=(("window", "2", "200"),),
        resource_profile="CPU_LIGHT",
        supported_regimes=regimes,
        source_license="FIRST_PARTY",
        evaluation_protocol_sha256=(
            "sha256:" + evaluation_marker * 64
        ),
        artifact_sha256="sha256:" + marker * 64,
    )


def policy(*, strategies=None, cells=None, threshold="0.01"):
    if strategies is None:
        strategies = (
            descriptor("FIBONACCI", "a"),
            descriptor("MOMENTUM", "b"),
        )
    if cells is None:
        cells = (
            StrategyToolCell("crypto", "bear", 3600),
            StrategyToolCell("crypto", "bull", 3600),
        )
    return StrategyToolPolicy.create(
        policy_id="section21-v1",
        exact_build_sha=BUILD,
        minimum_practical_advantage=threshold,
        registered_strategies=strategies,
        required_cells=cells,
    )


def coverage_protocol(strategy):
    return CrossMarketTrainingProtocol.create(
        protocol_id=f"coverage-{strategy.family.lower()}",
        candidate_hash=strategy.artifact_sha256,
        population_protocol_hash=POPULATION_PROTOCOL,
        exact_build_sha=BUILD,
        asset_profiles=(
            AssetClassProfile.create(
                asset_class="crypto",
                instrument_families=("crypto-spot",),
                specialized_feature_namespaces=("microstructure",),
            ),
            AssetClassProfile.create(
                asset_class="equity",
                instrument_families=("equity-cash",),
                specialized_feature_namespaces=("corporate",),
            ),
        ),
        required_cells=(
            MarketRegimeCell("crypto", "bear"),
            MarketRegimeCell("crypto", "bull"),
            MarketRegimeCell("equity", "bear"),
            MarketRegimeCell("equity", "bull"),
        ),
        min_observations_per_cell=10,
    )


def coverage_assessment(protocol, *, status="PASS"):
    cell_status = "PASS" if status == "PASS" else status
    return CrossMarketGeneralizationAssessment(
        status=status,
        reasons=(
            ("coverage complete",)
            if status == "PASS"
            else ("coverage unresolved",)
        ),
        cell_statuses={
            "crypto::bear": cell_status,
            "crypto::bull": cell_status,
            "equity::bear": cell_status,
            "equity::bull": cell_status,
        },
        protocol_sha256=protocol.digest,
        evidence_sha256="sha256:" + "7" * 64,
    )


def gate(
    status="PASS",
    *,
    provenance=True,
    source=BUILD,
    checks=None,
    metrics_sha256=None,
):
    if checks is None:
        if status == "PASS":
            checks = {"semantic_owner_evidence": "PASS"}
        elif status == "FAIL":
            checks = {"net_advantage": "FAIL"}
        else:
            checks = {"semantic_owner_evidence": "INCONCLUSIVE"}
    values = {}
    if provenance:
        values = {
            "evidence_bundle_id": "bundle-1",
            "evidence_graph_digest": "sha256:" + "6" * 64,
            "review_source_sha": source,
        }
        if metrics_sha256 is not None:
            values["strategy_comparison_metrics_sha256"] = metrics_sha256
    return GateDecision(
        status=status,
        reasons=("scientific result",),
        checks=checks,
        provenance=values,
    )


def evidence(
    strategy,
    regime,
    lower,
    *,
    net=None,
    gate_status="PASS",
    gate_value=None,
    coverage_status="PASS",
    costs_complete=True,
    protocol=None,
    assessment=None,
    cell=None,
):
    protocol = protocol or coverage_protocol(strategy)
    assessment = assessment or coverage_assessment(
        protocol,
        status=coverage_status,
    )
    if cell is None:
        cell = StrategyToolCell("crypto", regime, 3600)
    net_value = lower if net is None else net
    metrics_sha256 = strategy_cell_metrics_digest(
        cell=cell,
        strategy_fingerprint=strategy.fingerprint,
        after_cost_net_advantage=net_value,
        dependence_aware_lower_bound=lower,
        costs_complete=costs_complete,
    )
    if gate_value is None:
        gate_value = gate(
            gate_status,
            metrics_sha256=metrics_sha256,
        )
    elif (
        type(gate_value.checks) is MappingProxyType
        and type(gate_value.provenance) is MappingProxyType
        and gate_value.provenance
    ):
        provenance = dict(gate_value.provenance)
        provenance.setdefault(
            "strategy_comparison_metrics_sha256",
            metrics_sha256,
        )
        gate_value = GateDecision(
            status=gate_value.status,
            reasons=gate_value.reasons,
            checks=dict(gate_value.checks),
            provenance=provenance,
        )
    return StrategyCellEvidence(
        cell=cell,
        strategy_fingerprint=strategy.fingerprint,
        coverage_protocol=protocol,
        coverage_assessment=assessment,
        scientific_gate=gate_value,
        after_cost_net_advantage=net_value,
        dependence_aware_lower_bound=lower,
        costs_complete=costs_complete,
        metrics_sha256=metrics_sha256,
    )


def full_evidence(p):
    by_family = {
        item.family: item
        for item in p.registered_strategies
    }
    fib = by_family["FIBONACCI"]
    momentum = by_family["MOMENTUM"]
    return (
        evidence(fib, "bear", "0.005", net="0.008"),
        evidence(momentum, "bear", "0.03", net="0.04"),
        evidence(fib, "bull", "0.04", net="0.05"),
        evidence(momentum, "bull", "0.02", net="0.03"),
    )


class StrategyToolWeightingTests(unittest.TestCase):
    def test_same_strategy_family_can_have_different_regime_weight(self):
        p = policy()
        result = assess_strategy_tools(p, full_evidence(p))
        by_family = {
            item.family: item.fingerprint
            for item in p.registered_strategies
        }
        fib = by_family["FIBONACCI"]
        momentum = by_family["MOMENTUM"]

        self.assertEqual(result.status, "PASS")
        self.assertEqual(
            result.weights["crypto::bull::3600"][fib],
            ExactWeight(3, 4),
        )
        self.assertEqual(
            result.weights["crypto::bull::3600"][momentum],
            ExactWeight(1, 4),
        )
        self.assertEqual(
            result.weights["crypto::bear::3600"][fib],
            ExactWeight.zero(),
        )
        self.assertEqual(
            result.weights["crypto::bear::3600"][momentum],
            ExactWeight(1, 1),
        )

    def test_weights_are_exact_rationals_and_sum_to_one_without_rounding(self):
        p = policy()
        result = assess_strategy_tools(p, full_evidence(p))
        for cell, values in result.weights.items():
            nonzero = [
                value for value in values.values()
                if value.numerator
            ]
            if not nonzero:
                continue
            total = sum(
                (
                    Fraction(value.numerator, value.denominator)
                    for value in values.values()
                ),
                Fraction(0, 1),
            )
            self.assertEqual(total, Fraction(1, 1), cell)

    def test_popularity_is_not_a_policy_or_evidence_input(self):
        p = policy()
        row = full_evidence(p)[0]
        self.assertFalse(hasattr(p, "popularity"))
        self.assertFalse(hasattr(row, "popularity"))
        self.assertFalse(hasattr(row, "followers"))
        self.assertFalse(hasattr(row, "brand"))

    def test_missing_registered_candidate_is_inconclusive_and_zeroes_cell(self):
        p = policy()
        momentum = next(
            item.fingerprint
            for item in p.registered_strategies
            if item.family == "MOMENTUM"
        )
        rows = [
            row
            for row in full_evidence(p)
            if not (
                row.cell.regime == "bull"
                and row.strategy_fingerprint == momentum
            )
        ]
        result = assess_strategy_tools(p, rows)
        self.assertEqual(result.status, "INCONCLUSIVE")
        self.assertEqual(
            result.cell_statuses["crypto::bull::3600"],
            "INCONCLUSIVE",
        )
        self.assertTrue(
            all(
                weight == ExactWeight.zero()
                for weight
                in result.weights["crypto::bull::3600"].values()
            )
        )

    def test_inconclusive_section20_coverage_blocks_partial_weight(self):
        p = policy()
        rows = list(full_evidence(p))
        fib = next(
            item for item in p.registered_strategies
            if item.family == "FIBONACCI"
        )
        rows[0] = evidence(
            fib,
            "bear",
            "0.02",
            coverage_status="INCONCLUSIVE",
        )
        result = assess_strategy_tools(p, rows)
        self.assertEqual(
            result.cell_statuses["crypto::bear::3600"],
            "INCONCLUSIVE",
        )
        self.assertTrue(
            all(
                weight.numerator == 0
                for weight
                in result.weights["crypto::bear::3600"].values()
            )
        )

    def test_scientific_inconclusive_blocks_partial_weight(self):
        p = policy()
        rows = list(full_evidence(p))
        strategy = next(
            item for item in p.registered_strategies
            if item.family == "MOMENTUM"
        )
        rows[-1] = evidence(
            strategy,
            "bull",
            "0.02",
            gate_status="INCONCLUSIVE",
        )
        result = assess_strategy_tools(p, rows)
        self.assertEqual(
            result.cell_statuses["crypto::bull::3600"],
            "INCONCLUSIVE",
        )
        self.assertTrue(
            all(
                weight.numerator == 0
                for weight
                in result.weights["crypto::bull::3600"].values()
            )
        )

    def test_scientific_fail_is_retained_as_zero_not_hidden(self):
        p = policy()
        rows = list(full_evidence(p))
        fib = next(
            item for item in p.registered_strategies
            if item.family == "FIBONACCI"
        )
        rows[2] = evidence(
            fib,
            "bull",
            "0.05",
            gate_status="FAIL",
        )
        result = assess_strategy_tools(p, rows)
        self.assertEqual(
            result.cell_statuses["crypto::bull::3600"],
            "PASS",
        )
        self.assertEqual(
            result.dispositions["crypto::bull::3600"][fib.fingerprint],
            "EVIDENCED_ZERO",
        )
        self.assertEqual(
            result.weights["crypto::bull::3600"][fib.fingerprint],
            ExactWeight.zero(),
        )

    def test_scientific_pass_cannot_contradict_its_checks(self):
        p = policy()
        rows = list(full_evidence(p))
        strategy = next(
            item for item in p.registered_strategies
            if item.family == "MOMENTUM"
        )
        rows[-1] = evidence(
            strategy,
            "bull",
            "0.02",
            gate_value=gate(
                "PASS",
                checks={"causality": "FAIL"},
            ),
        )
        result = assess_strategy_tools(p, rows)
        self.assertEqual(
            result.cell_statuses["crypto::bull::3600"],
            "FAIL",
        )

    def test_scientific_pass_without_reviewed_provenance_is_inconclusive(self):
        p = policy()
        rows = list(full_evidence(p))
        strategy = next(
            item for item in p.registered_strategies
            if item.family == "MOMENTUM"
        )
        rows[-1] = evidence(
            strategy,
            "bull",
            "0.02",
            gate_value=gate("PASS", provenance=False),
        )
        result = assess_strategy_tools(p, rows)
        self.assertEqual(
            result.cell_statuses["crypto::bull::3600"],
            "INCONCLUSIVE",
        )

    def test_scientific_review_source_must_match_exact_build(self):
        p = policy()
        rows = list(full_evidence(p))
        strategy = next(
            item for item in p.registered_strategies
            if item.family == "MOMENTUM"
        )
        rows[-1] = evidence(
            strategy,
            "bull",
            "0.02",
            gate_value=gate("PASS", source="f" * 40),
        )
        result = assess_strategy_tools(p, rows)
        self.assertEqual(
            result.cell_statuses["crypto::bull::3600"],
            "FAIL",
        )

    def test_coverage_candidate_must_be_registered_strategy_artifact(self):
        p = policy()
        rows = list(full_evidence(p))
        strategy = next(
            item for item in p.registered_strategies
            if item.family == "FIBONACCI"
        )
        wrong = descriptor("OTHER", "c")
        protocol = coverage_protocol(wrong)
        rows[0] = evidence(
            strategy,
            "bear",
            "0.02",
            protocol=protocol,
            assessment=coverage_assessment(protocol),
        )
        result = assess_strategy_tools(p, rows)
        self.assertEqual(
            result.cell_statuses["crypto::bear::3600"],
            "FAIL",
        )

    def test_coverage_assessment_must_bind_supplied_protocol(self):
        p = policy()
        rows = list(full_evidence(p))
        strategy = next(
            item for item in p.registered_strategies
            if item.family == "FIBONACCI"
        )
        protocol = coverage_protocol(strategy)
        assessment = coverage_assessment(protocol)
        object.__setattr__(
            assessment,
            "protocol_sha256",
            "sha256:" + "5" * 64,
        )
        rows[0] = evidence(
            strategy,
            "bear",
            "0.02",
            protocol=protocol,
            assessment=assessment,
        )
        result = assess_strategy_tools(p, rows)
        self.assertEqual(
            result.cell_statuses["crypto::bear::3600"],
            "FAIL",
        )

    def test_lower_bound_above_point_estimate_is_invalid(self):
        p = policy()
        rows = list(full_evidence(p))
        strategy = next(
            item for item in p.registered_strategies
            if item.family == "MOMENTUM"
        )
        rows[-1] = evidence(
            strategy,
            "bull",
            "0.04",
            net="0.03",
        )
        result = assess_strategy_tools(p, rows)
        self.assertEqual(
            result.cell_statuses["crypto::bull::3600"],
            "FAIL",
        )

    def test_complete_no_edge_cell_is_pass_with_zero_weights(self):
        p = policy(threshold="0.10")
        result = assess_strategy_tools(p, full_evidence(p))
        self.assertEqual(result.status, "PASS")
        for values in result.weights.values():
            self.assertTrue(
                all(
                    value == ExactWeight.zero()
                    for value in values.values()
                )
            )
        self.assertTrue(
            any(
                "no registered strategy cleared" in reason
                for reason in result.reasons
            )
        )

    def test_binary_float_metrics_are_rejected(self):
        p = policy()
        strategy = p.registered_strategies[0]
        protocol = coverage_protocol(strategy)
        with self.assertRaises(TypeError):
            StrategyCellEvidence(
                cell=StrategyToolCell("crypto", "bull", 3600),
                strategy_fingerprint=strategy.fingerprint,
                coverage_protocol=protocol,
                coverage_assessment=coverage_assessment(protocol),
                scientific_gate=gate(),
                after_cost_net_advantage=0.03,
                dependence_aware_lower_bound="0.02",
                costs_complete=True,
                metrics_sha256=METRICS,
            )

    def test_duplicate_strategy_cell_evidence_is_rejected(self):
        p = policy()
        rows = list(full_evidence(p))
        rows.append(rows[0])
        with self.assertRaisesRegex(
            StrategyToolWeightingError,
            "duplicate strategy/cell",
        ):
            assess_strategy_tools(p, rows)

    def test_unregistered_extra_cell_fails_closed(self):
        p = policy()
        rows = list(full_evidence(p))
        strategy = p.registered_strategies[0]
        rows.append(
            evidence(
                strategy,
                "bull",
                "0.03",
                cell=StrategyToolCell(
                    "equity",
                    "bull",
                    3600,
                ),
            )
        )
        result = assess_strategy_tools(p, rows)
        self.assertEqual(result.status, "FAIL")
        self.assertTrue(
            all(
                status == "FAIL"
                for status in result.cell_statuses.values()
            )
        )
        self.assertTrue(
            all(
                weight.numerator == 0
                for values in result.weights.values()
                for weight in values.values()
            )
        )

    def test_policy_allows_one_evidence_bound_strategy_tool(self):
        only = descriptor("MOMENTUM", "a")
        p = policy(
            strategies=(only,),
            cells=(StrategyToolCell("crypto", "bull", 3600),),
        )
        row = evidence(only, "bull", "0.03", net="0.04")
        result = assess_strategy_tools(p, (row,))
        self.assertEqual(result.status, "PASS")
        self.assertEqual(
            result.weights["crypto::bull::3600"][only.fingerprint],
            ExactWeight(1, 1),
        )

    def test_policy_rejects_cell_without_any_eligible_strategy(self):
        first = descriptor("FIBONACCI", "a", horizon=60)
        second = descriptor("MOMENTUM", "b", horizon=60)
        with self.assertRaisesRegex(
            StrategyToolWeightingError,
            "at least one registered strategy",
        ):
            policy(
                strategies=(first, second),
                cells=(
                    StrategyToolCell(
                        "crypto",
                        "bull",
                        3600,
                    ),
                ),
            )

    def test_evidence_order_does_not_change_identity_or_weights(self):
        p = policy()
        rows = full_evidence(p)
        first = assess_strategy_tools(p, rows)
        second = assess_strategy_tools(p, tuple(reversed(rows)))
        self.assertEqual(first.evidence_sha256, second.evidence_sha256)
        self.assertEqual(
            {
                cell: {
                    key: value.text
                    for key, value in values.items()
                }
                for cell, values in first.weights.items()
            },
            {
                cell: {
                    key: value.text
                    for key, value in values.items()
                }
                for cell, values in second.weights.items()
            },
        )

    def test_policy_digest_changes_when_practical_effect_changes(self):
        p1 = policy(threshold="0.01")
        p2 = policy(threshold="0.02")
        self.assertNotEqual(p1.digest, p2.digest)

    def test_strategy_artifact_change_changes_policy_digest(self):
        base = (
            descriptor("FIBONACCI", "a"),
            descriptor("MOMENTUM", "b"),
        )
        changed = (
            descriptor("FIBONACCI", "c"),
            descriptor("MOMENTUM", "b"),
        )
        self.assertNotEqual(
            policy(strategies=base).digest,
            policy(strategies=changed).digest,
        )

    def test_result_never_grants_any_execution_authority(self):
        p = policy()
        result = assess_strategy_tools(p, full_evidence(p))
        self.assertEqual(
            result.economic_edge_status,
            "NOT_ESTABLISHED",
        )
        self.assertFalse(result.routing_authority_granted)
        self.assertFalse(result.promotion_authority_granted)
        self.assertFalse(result.trading_authority_granted)

    def test_exact_weight_must_be_reduced(self):
        with self.assertRaisesRegex(
            StrategyToolWeightingError,
            "reduced",
        ):
            ExactWeight(2, 4)

    def test_incomplete_costs_fail_closed(self):
        p = policy()
        rows = list(full_evidence(p))
        strategy = next(
            item for item in p.registered_strategies
            if item.family == "FIBONACCI"
        )
        rows[2] = evidence(
            strategy,
            "bull",
            "0.04",
            costs_complete=False,
        )
        result = assess_strategy_tools(p, rows)
        self.assertEqual(
            result.cell_statuses["crypto::bull::3600"],
            "INCONCLUSIVE",
        )
        self.assertTrue(
            all(
                weight.numerator == 0
                for weight
                in result.weights["crypto::bull::3600"].values()
            )
        )


    def test_mutated_section20_trading_authority_cannot_be_cleaned(self):
        p = policy()
        strategy = p.registered_strategies[0]
        protocol = coverage_protocol(strategy)
        assessment = coverage_assessment(protocol)
        object.__setattr__(
            assessment,
            "grants_trading_authority",
            True,
        )
        with self.assertRaisesRegex(
            ValueError,
            "cannot grant trading authority",
        ):
            evidence(
                strategy,
                "bull",
                "0.03",
                protocol=protocol,
                assessment=assessment,
            )

    def test_mutated_section20_strategy_comparison_claim_cannot_be_cleaned(self):
        p = policy()
        strategy = p.registered_strategies[0]
        protocol = coverage_protocol(strategy)
        assessment = coverage_assessment(protocol)
        object.__setattr__(
            assessment,
            "strategy_comparison_status",
            "ESTABLISHED",
        )
        with self.assertRaisesRegex(
            ValueError,
            "strategy_comparison_status must remain NOT_ESTABLISHED",
        ):
            evidence(
                strategy,
                "bull",
                "0.03",
                protocol=protocol,
                assessment=assessment,
            )

    def test_hostile_evidence_sequence_is_rejected_before_iteration(self):
        p = policy()
        touched = {"count": 0}

        class HostileSequence:
            def __iter__(self):
                touched["count"] += 1
                raise AssertionError("hostile iterator executed")

        with self.assertRaisesRegex(TypeError, "exact tuple or list"):
            assess_strategy_tools(p, HostileSequence())
        self.assertEqual(touched["count"], 0)

    def test_hostile_policy_sequence_is_rejected_before_iteration(self):
        touched = {"count": 0}

        class HostileSequence:
            def __iter__(self):
                touched["count"] += 1
                raise AssertionError("hostile iterator executed")

        with self.assertRaisesRegex(TypeError, "exact tuple or list"):
            StrategyToolPolicy.create(
                policy_id="section21-hostile",
                exact_build_sha=BUILD,
                minimum_practical_advantage="0.01",
                registered_strategies=HostileSequence(),
                required_cells=(
                    StrategyToolCell("crypto", "bull", 3600),
                ),
            )
        self.assertEqual(touched["count"], 0)

    def test_mutated_gate_mapping_is_rejected_before_callbacks(self):
        p = policy()
        strategy = p.registered_strategies[0]
        touched = {"count": 0}

        class HostileDict(dict):
            def items(self):
                touched["count"] += 1
                raise AssertionError("hostile mapping executed")

        scientific_gate = gate()
        object.__setattr__(
            scientific_gate,
            "checks",
            HostileDict({"semantic_owner_evidence": "PASS"}),
        )
        with self.assertRaisesRegex(
            TypeError,
            "checks must remain an exact dict or mapping proxy",
        ):
            evidence(
                strategy,
                "bull",
                "0.03",
                gate_value=scientific_gate,
            )
        self.assertEqual(touched["count"], 0)


    def test_protocol_integrity_gate_failure_is_invalid_not_strategy_loss(self):
        p = policy()
        rows = list(full_evidence(p))
        strategy = next(
            item for item in p.registered_strategies
            if item.family == "FIBONACCI"
        )
        rows[2] = evidence(
            strategy,
            "bull",
            "0.05",
            gate_value=gate(
                "FAIL",
                checks={"causality": "FAIL"},
            ),
        )
        result = assess_strategy_tools(p, rows)
        self.assertEqual(
            result.cell_statuses["crypto::bull::3600"],
            "FAIL",
        )
        self.assertEqual(
            result.dispositions["crypto::bull::3600"][
                strategy.fingerprint
            ],
            "INVALID",
        )
        self.assertEqual(
            result.weights["crypto::bull::3600"][
                strategy.fingerprint
            ],
            ExactWeight.zero(),
        )

    def test_terminal_loss_without_reviewed_provenance_is_inconclusive(self):
        p = policy()
        rows = list(full_evidence(p))
        strategy = next(
            item for item in p.registered_strategies
            if item.family == "FIBONACCI"
        )
        rows[2] = evidence(
            strategy,
            "bull",
            "0.05",
            gate_value=gate(
                "FAIL",
                provenance=False,
                checks={"net_advantage": "FAIL"},
            ),
        )
        result = assess_strategy_tools(p, rows)
        self.assertEqual(
            result.cell_statuses["crypto::bull::3600"],
            "INCONCLUSIVE",
        )
        self.assertEqual(
            result.dispositions["crypto::bull::3600"][
                strategy.fingerprint
            ],
            "INCONCLUSIVE",
        )


    def test_mutated_coverage_mapping_is_rejected_before_callbacks(self):
        p = policy()
        strategy = p.registered_strategies[0]
        protocol = coverage_protocol(strategy)
        assessment = coverage_assessment(protocol)
        touched = {"count": 0}

        class HostileDict(dict):
            def items(self):
                touched["count"] += 1
                raise AssertionError("hostile mapping executed")

        object.__setattr__(
            assessment,
            "cell_statuses",
            HostileDict({"crypto::bull": "PASS"}),
        )
        with self.assertRaisesRegex(
            TypeError,
            "cell_statuses must remain an exact",
        ):
            evidence(
                strategy,
                "bull",
                "0.03",
                protocol=protocol,
                assessment=assessment,
            )
        self.assertEqual(touched["count"], 0)


    def test_policy_rejects_metrics_from_different_evaluation_protocols(self):
        first = descriptor(
            "FIBONACCI",
            "a",
            evaluation_marker="d",
        )
        second = descriptor(
            "MOMENTUM",
            "b",
            evaluation_marker="e",
        )
        with self.assertRaisesRegex(
            StrategyToolWeightingError,
            "share one registered evaluation protocol",
        ):
            policy(strategies=(first, second))


    def test_scientific_review_cannot_bind_different_score_metrics(self):
        p = policy()
        rows = list(full_evidence(p))
        strategy = next(
            item for item in p.registered_strategies
            if item.family == "MOMENTUM"
        )
        rows[-1] = evidence(
            strategy,
            "bull",
            "0.02",
            gate_value=gate(
                "PASS",
                metrics_sha256="sha256:" + "f" * 64,
            ),
        )
        result = assess_strategy_tools(p, rows)
        self.assertEqual(
            result.cell_statuses["crypto::bull::3600"],
            "FAIL",
        )

    def test_metrics_digest_must_match_exact_score_values(self):
        p = policy()
        strategy = p.registered_strategies[0]
        protocol = coverage_protocol(strategy)
        with self.assertRaisesRegex(
            StrategyToolWeightingError,
            "does not bind the exact score-bearing values",
        ):
            StrategyCellEvidence(
                cell=StrategyToolCell("crypto", "bull", 3600),
                strategy_fingerprint=strategy.fingerprint,
                coverage_protocol=protocol,
                coverage_assessment=coverage_assessment(protocol),
                scientific_gate=gate(),
                after_cost_net_advantage="0.03",
                dependence_aware_lower_bound="0.02",
                costs_complete=True,
                metrics_sha256=METRICS,
            )


if __name__ == "__main__":
    unittest.main()
