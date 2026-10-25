from dataclasses import replace
import unittest

from research.autotrade_research.evaluation.gates import GateDecision
from research.autotrade_research.evaluation.strategy_tools import (
    StrategyContextDecision,
    StrategyToolCell,
    StrategyToolMatrix,
)
from research.autotrade_research.strategies.deterministic import StrategyDescriptor


SOURCE_SHA = "a" * 40
PROTOCOL_SHA = "sha256:" + "b" * 64
CUT = "2026-10-04T12:00:00Z"


def descriptor(
    strategy_id: str,
    *,
    version: int = 1,
    family: str = "trend",
    horizon_seconds: int = 300,
    protocol_sha: str = PROTOCOL_SHA,
) -> StrategyDescriptor:
    return StrategyDescriptor(
        strategy_id=strategy_id,
        version=version,
        family=family,
        feature_schema="features-v1",
        market_requirements=("causal-bars", "after-cost-economics"),
        minimum_history=20,
        horizon_seconds=horizon_seconds,
        decision_schedule="event-driven",
        proposal_semantics="deterministic-v1",
        parameter_bounds=(("threshold", "0", "1"),),
        resource_profile="bounded-cpu",
        supported_regimes=("bull", "sideways", "stress"),
        source_license="internal",
        evaluation_protocol_sha256=protocol_sha,
        artifact_sha256="sha256:" + "c" * 64,
    )


def gate(
    status: str,
    *,
    bundle_id: str = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    graph_digit: str = "d",
    source_sha: str = SOURCE_SHA,
) -> GateDecision:
    return GateDecision(
        status=status,
        reasons=(f"registered gate {status.lower()}",),
        checks={"context_gate": status},
        provenance={
            "evidence_bundle_id": bundle_id,
            "evidence_graph_digest": "sha256:" + graph_digit * 64,
            "review_source_sha": source_sha,
        },
    )


def cell(
    strategy_id: str,
    status: str,
    *,
    regime: str = "bull",
    asset_class: str = "EQUITY",
    horizon_seconds: int = 300,
    cut: str = CUT,
    protocol_sha: str = PROTOCOL_SHA,
    version: int = 1,
    family: str = "trend",
    bundle_id: str = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    graph_digit: str = "d",
    source_sha: str = SOURCE_SHA,
) -> StrategyToolCell:
    return StrategyToolCell(
        strategy=descriptor(
            strategy_id,
            version=version,
            family=family,
            horizon_seconds=horizon_seconds,
            protocol_sha=protocol_sha,
        ),
        asset_class=asset_class,
        regime_id=regime,
        evaluation_cut=cut,
        gate_decision=gate(
            status,
            bundle_id=bundle_id,
            graph_digit=graph_digit,
            source_sha=source_sha,
        ),
    )


class StrategyToolsTests(unittest.TestCase):
    def test_same_strategy_can_pass_one_regime_and_fail_another(self):
        strategy = descriptor("fibonacci")
        bull = StrategyToolCell(
            strategy=strategy,
            asset_class="EQUITY",
            regime_id="bull",
            evaluation_cut=CUT,
            gate_decision=gate("PASS"),
        )
        sideways = StrategyToolCell(
            strategy=strategy,
            asset_class="EQUITY",
            regime_id="sideways",
            evaluation_cut=CUT,
            gate_decision=gate(
                "FAIL",
                bundle_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                graph_digit="e",
            ),
        )
        matrix = StrategyToolMatrix((sideways, bull))

        bull_decision = matrix.decision_for_context(
            asset_class="EQUITY",
            regime_id="bull",
            horizon_seconds=300,
        )
        sideways_decision = matrix.decision_for_context(
            asset_class="EQUITY",
            regime_id="sideways",
            horizon_seconds=300,
        )

        self.assertEqual(bull_decision.status, "ONE_ELIGIBLE")
        self.assertEqual(
            bull_decision.eligible_fingerprints,
            (strategy.fingerprint,),
        )
        self.assertEqual(sideways_decision.status, "NO_ELIGIBLE")
        self.assertEqual(
            sideways_decision.failed_fingerprints,
            (strategy.fingerprint,),
        )

    def test_multiple_passing_tools_do_not_invent_a_winner_or_weight(self):
        matrix = StrategyToolMatrix(
            (
                cell("trend-a", "PASS"),
                cell(
                    "mean-reversion-b",
                    "PASS",
                    family="mean-reversion",
                    bundle_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                    graph_digit="e",
                ),
            )
        )

        decision = matrix.decision_for_context(
            asset_class="EQUITY",
            regime_id="bull",
            horizon_seconds=300,
        )

        self.assertEqual(decision.status, "MULTIPLE_ELIGIBLE")
        self.assertEqual(len(decision.eligible_fingerprints), 2)
        self.assertFalse(decision.grants_trading_authority)
        self.assertEqual(decision.economic_edge_status, "NOT_ESTABLISHED")

    def test_inconclusive_candidate_blocks_context_selection(self):
        matrix = StrategyToolMatrix(
            (
                cell("known-pass", "PASS"),
                cell(
                    "unresolved",
                    "INCONCLUSIVE",
                    bundle_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                    graph_digit="e",
                ),
            )
        )

        decision = matrix.decision_for_context(
            asset_class="EQUITY",
            regime_id="bull",
            horizon_seconds=300,
        )

        self.assertEqual(decision.status, "INCONCLUSIVE")
        self.assertEqual(len(decision.eligible_fingerprints), 1)
        self.assertEqual(len(decision.inconclusive_fingerprints), 1)

    def test_missing_context_is_inconclusive_not_no_trade_proof(self):
        matrix = StrategyToolMatrix((cell("trend", "PASS"),))
        decision = matrix.decision_for_context(
            asset_class="OPTION",
            regime_id="bull",
            horizon_seconds=300,
        )
        self.assertEqual(decision.status, "INCONCLUSIVE")
        self.assertEqual(decision.eligible_fingerprints, ())
        self.assertEqual(decision.failed_fingerprints, ())

    def test_all_registered_failures_produce_no_eligible_tool(self):
        matrix = StrategyToolMatrix(
            (
                cell("trend", "FAIL"),
                cell(
                    "breakout",
                    "FAIL",
                    bundle_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                    graph_digit="e",
                ),
            )
        )
        decision = matrix.decision_for_context(
            asset_class="EQUITY",
            regime_id="bull",
            horizon_seconds=300,
        )
        self.assertEqual(decision.status, "NO_ELIGIBLE")
        self.assertEqual(len(decision.failed_fingerprints), 2)

    def test_horizon_is_part_of_context_not_a_global_strategy_label(self):
        matrix = StrategyToolMatrix(
            (
                cell("fast-trend", "PASS", horizon_seconds=60),
                cell(
                    "slow-trend",
                    "PASS",
                    horizon_seconds=3600,
                    bundle_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                    graph_digit="e",
                ),
            )
        )
        fast = matrix.decision_for_context(
            asset_class="EQUITY",
            regime_id="bull",
            horizon_seconds=60,
        )
        slow = matrix.decision_for_context(
            asset_class="EQUITY",
            regime_id="bull",
            horizon_seconds=3600,
        )
        self.assertEqual(fast.status, "ONE_ELIGIBLE")
        self.assertEqual(slow.status, "ONE_ELIGIBLE")
        self.assertNotEqual(fast.eligible_fingerprints, slow.eligible_fingerprints)

    def test_matrix_rejects_apples_to_oranges_protocols(self):
        with self.assertRaisesRegex(ValueError, "not comparable"):
            StrategyToolMatrix(
                (
                    cell("trend", "PASS"),
                    cell(
                        "breakout",
                        "PASS",
                        protocol_sha="sha256:" + "9" * 64,
                        bundle_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                        graph_digit="e",
                    ),
                )
            )

    def test_matrix_rejects_mixed_review_source_sha(self):
        with self.assertRaisesRegex(ValueError, "not comparable"):
            StrategyToolMatrix(
                (
                    cell("trend", "PASS"),
                    cell(
                        "breakout",
                        "PASS",
                        source_sha="f" * 40,
                        bundle_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                        graph_digit="e",
                    ),
                )
            )

    def test_matrix_rejects_mixed_evaluation_cut(self):
        with self.assertRaisesRegex(ValueError, "not comparable"):
            StrategyToolMatrix(
                (
                    cell("trend", "PASS"),
                    cell(
                        "breakout",
                        "PASS",
                        cut="2026-10-04T12:01:00Z",
                        bundle_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                        graph_digit="e",
                    ),
                )
            )

    def test_verified_gate_provenance_is_required(self):
        decision = GateDecision(
            status="PASS",
            reasons=("unit-only object",),
            checks={"context_gate": "PASS"},
        )
        with self.assertRaisesRegex(ValueError, "evidence_bundle_id"):
            StrategyToolCell(
                strategy=descriptor("trend"),
                asset_class="EQUITY",
                regime_id="bull",
                evaluation_cut=CUT,
                gate_decision=decision,
            )

    def test_duplicate_strategy_context_cell_is_rejected(self):
        original = cell("trend", "PASS")
        duplicate = cell(
            "trend",
            "PASS",
            bundle_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
            graph_digit="e",
        )
        with self.assertRaisesRegex(ValueError, "duplicate"):
            StrategyToolMatrix((original, duplicate))

    def test_one_strategy_version_cannot_have_two_fingerprints(self):
        first = cell("trend", "PASS", family="trend")
        second = cell(
            "trend",
            "PASS",
            family="breakout",
            regime="sideways",
            bundle_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
            graph_digit="e",
        )
        with self.assertRaisesRegex(ValueError, "multiple strategy fingerprints"):
            StrategyToolMatrix((first, second))

    def test_matrix_digest_is_order_independent(self):
        first = cell("trend", "PASS")
        second = cell(
            "breakout",
            "FAIL",
            bundle_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
            graph_digit="e",
        )
        self.assertEqual(
            StrategyToolMatrix((first, second)).matrix_digest,
            StrategyToolMatrix((second, first)).matrix_digest,
        )

    def test_post_construction_mutation_is_revalidated_at_matrix_boundary(self):
        value = cell("trend", "PASS")
        object.__setattr__(value, "asset_class", " EQUITY")
        with self.assertRaisesRegex(ValueError, "asset_class"):
            StrategyToolMatrix((value,))

    def test_mutated_gate_storage_cannot_dispatch_through_untrusted_mapping(self):
        decision = gate("PASS")
        object.__setattr__(decision, "checks", {"context_gate": "PASS"})
        with self.assertRaisesRegex(TypeError, "immutable canonical storage"):
            StrategyToolCell(
                strategy=descriptor("trend"),
                asset_class="EQUITY",
                regime_id="bull",
                evaluation_cut=CUT,
                gate_decision=decision,
            )

    def test_gate_and_strategy_subclasses_are_not_authority_inputs(self):
        class GateSubclass(GateDecision):
            pass

        class StrategySubclass(StrategyDescriptor):
            pass

        subclass_gate = GateSubclass(
            status="PASS",
            reasons=("pass",),
            checks={"context_gate": "PASS"},
            provenance={
                "evidence_bundle_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                "evidence_graph_digest": "sha256:" + "d" * 64,
                "review_source_sha": SOURCE_SHA,
            },
        )
        with self.assertRaisesRegex(TypeError, "exact GateDecision"):
            StrategyToolCell(
                strategy=descriptor("trend"),
                asset_class="EQUITY",
                regime_id="bull",
                evaluation_cut=CUT,
                gate_decision=subclass_gate,
            )

        base = descriptor("trend")
        subclass_strategy = StrategySubclass(**base.__dict__)
        with self.assertRaisesRegex(TypeError, "exact StrategyDescriptor"):
            StrategyToolCell(
                strategy=subclass_strategy,
                asset_class="EQUITY",
                regime_id="bull",
                evaluation_cut=CUT,
                gate_decision=gate("PASS"),
            )

    def test_context_decision_cannot_be_forged_into_financial_authority(self):
        digest = "sha256:" + "1" * 64
        with self.assertRaisesRegex(ValueError, "cannot grant"):
            StrategyContextDecision(
                status="NO_ELIGIBLE",
                asset_class="EQUITY",
                regime_id="bull",
                horizon_seconds=300,
                eligible_fingerprints=(),
                failed_fingerprints=(),
                inconclusive_fingerprints=(),
                matrix_digest=digest,
                context_digest=digest,
                grants_trading_authority=True,
            )

    def test_context_digest_changes_when_regime_result_changes(self):
        strategy = descriptor("trend")
        matrix = StrategyToolMatrix(
            (
                StrategyToolCell(
                    strategy=strategy,
                    asset_class="EQUITY",
                    regime_id="bull",
                    evaluation_cut=CUT,
                    gate_decision=gate("PASS"),
                ),
                StrategyToolCell(
                    strategy=strategy,
                    asset_class="EQUITY",
                    regime_id="sideways",
                    evaluation_cut=CUT,
                    gate_decision=gate(
                        "FAIL",
                        bundle_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                        graph_digit="e",
                    ),
                ),
            )
        )
        bull = matrix.decision_for_context(
            asset_class="EQUITY", regime_id="bull", horizon_seconds=300
        )
        sideways = matrix.decision_for_context(
            asset_class="EQUITY", regime_id="sideways", horizon_seconds=300
        )
        self.assertNotEqual(bull.context_digest, sideways.context_digest)


if __name__ == "__main__":
    unittest.main()
