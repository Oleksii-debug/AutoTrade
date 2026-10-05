from datetime import datetime, timezone
from decimal import Decimal, Inexact, Rounded, ROUND_DOWN, localcontext
import unittest

from mvp.autotrade_mvp.thesis_implementation import (
    ImplementationCandidate,
    ImplementationDecision,
    ImplementationPolicy,
    MarketThesis,
    ThesisImplementationError,
    select_implementation,
)


AS_OF = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)
HORIZON = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)
AFTER_HORIZON = datetime(2026, 12, 4, 12, 0, tzinfo=timezone.utc)
BEFORE_HORIZON = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)


class ThesisImplementationTests(unittest.TestCase):
    def thesis(self, **overrides):
        values = dict(
            thesis_id="gold-down-1",
            subject="GOLD",
            direction="SHORT",
            as_of=AS_OF,
            horizon_end=HORIZON,
            required_notional="10000",
            notional_currency="USD",
        )
        values.update(overrides)
        return MarketThesis(**values)

    def policy(self, **overrides):
        values = dict(
            max_total_cost_rate="0.02",
            max_leverage_ratio="3",
            max_liquidation_risk="0.20",
            min_liquidity_capacity="10000",
            permitted_asset_classes=("FUND", "FUTURE", "OPTION"),
            require_qualified_route=True,
        )
        values.update(overrides)
        return ImplementationPolicy(**values)

    def candidate(self, candidate_id, **overrides):
        values = dict(
            candidate_id=candidate_id,
            thesis_id="gold-down-1",
            instrument_version="11111111-1111-4111-8111-111111111111@1",
            provider_id="SIMULATED",
            asset_class="FUTURE",
            exposure_direction="SHORT",
            route_id=f"route-{candidate_id}",
            legal=True,
            technically_available=True,
            route_qualified=True,
            fee_rate="0.001",
            spread_rate="0.001",
            holding_cost_rate="0.001",
            leverage_ratio="2",
            liquidation_risk="0.10",
            liquidity_capacity="50000",
            liquidity_currency="USD",
            tradable_until=AFTER_HORIZON,
        )
        values.update(overrides)
        return ImplementationCandidate(**values)

    def test_unique_pareto_winner_is_selected_without_weighted_score(self):
        winner = self.candidate(
            "future",
            fee_rate="0.0005",
            spread_rate="0.0005",
            holding_cost_rate="0.0005",
            leverage_ratio="1.5",
            liquidation_risk="0.05",
            liquidity_capacity="75000",
        )
        dominated = self.candidate(
            "option",
            asset_class="OPTION",
            fee_rate="0.001",
            spread_rate="0.002",
            holding_cost_rate="0.003",
            leverage_ratio="2",
            liquidation_risk="0.10",
            liquidity_capacity="50000",
        )

        decision = select_implementation(
            thesis=self.thesis(),
            policy=self.policy(),
            candidates=(dominated, winner),
        )

        self.assertEqual(decision.status, "SELECTED")
        self.assertEqual(decision.selected_candidate_id, "future")
        self.assertEqual(decision.pareto_frontier_ids, ("future",))
        self.assertEqual(decision.feasible_candidate_ids, ("future", "option"))

    def test_tradeoff_stays_ambiguous_instead_of_inventing_utility_weights(self):
        low_cost = self.candidate(
            "low-cost",
            fee_rate="0.0002",
            spread_rate="0.0002",
            holding_cost_rate="0.0002",
            leverage_ratio="2.5",
            liquidation_risk="0.12",
            liquidity_capacity="30000",
        )
        low_risk = self.candidate(
            "low-risk",
            asset_class="FUND",
            fee_rate="0.003",
            spread_rate="0.002",
            holding_cost_rate="0.001",
            leverage_ratio="1",
            liquidation_risk="0.01",
            liquidity_capacity="80000",
        )

        decision = select_implementation(
            thesis=self.thesis(),
            policy=self.policy(),
            candidates=[low_cost, low_risk],
        )

        self.assertEqual(decision.status, "AMBIGUOUS")
        self.assertIsNone(decision.selected_candidate_id)
        self.assertEqual(decision.pareto_frontier_ids, ("low-cost", "low-risk"))

    def test_no_trade_is_first_class_when_every_expression_is_infeasible(self):
        candidates = (
            self.candidate("illegal", legal=False),
            self.candidate("offline", technically_available=False),
            self.candidate("route", route_qualified=False),
            self.candidate("expiring", tradable_until=BEFORE_HORIZON),
            self.candidate("thin", liquidity_capacity="9999"),
            self.candidate("expensive", fee_rate="0.03"),
            self.candidate("levered", leverage_ratio="4"),
            self.candidate("liquidation", liquidation_risk="0.21"),
        )

        decision = select_implementation(
            thesis=self.thesis(),
            policy=self.policy(),
            candidates=candidates,
        )

        self.assertEqual(decision.status, "NO_TRADE")
        self.assertIsNone(decision.selected_candidate_id)
        self.assertEqual(decision.feasible_candidate_ids, ())
        self.assertIn("LEGAL_RESTRICTION", decision.rejected_reasons["illegal"])
        self.assertIn("TECHNICALLY_UNAVAILABLE", decision.rejected_reasons["offline"])
        self.assertIn("ROUTE_NOT_QUALIFIED", decision.rejected_reasons["route"])
        self.assertIn("HORIZON_NOT_COVERED", decision.rejected_reasons["expiring"])
        self.assertIn("INSUFFICIENT_LIQUIDITY_CAPACITY", decision.rejected_reasons["thin"])
        self.assertIn("TOTAL_COST_LIMIT", decision.rejected_reasons["expensive"])
        self.assertIn("LEVERAGE_LIMIT", decision.rejected_reasons["levered"])
        self.assertIn("LIQUIDATION_RISK_LIMIT", decision.rejected_reasons["liquidation"])

    def test_wrong_thesis_or_exposure_direction_cannot_be_selected(self):
        wrong_thesis = self.candidate("foreign", thesis_id="oil-down-1")
        wrong_direction = self.candidate("long", exposure_direction="LONG")

        decision = select_implementation(
            thesis=self.thesis(),
            policy=self.policy(),
            candidates=(wrong_thesis, wrong_direction),
        )

        self.assertEqual(decision.status, "NO_TRADE")
        self.assertEqual(
            decision.rejected_reasons["foreign"],
            ("THESIS_ID_MISMATCH",),
        )
        self.assertEqual(
            decision.rejected_reasons["long"],
            ("EXPOSURE_DIRECTION_MISMATCH",),
        )

    def test_asset_class_policy_is_explicit_and_fail_closed(self):
        crypto = self.candidate("crypto", asset_class="CRYPTO_SPOT")
        decision = select_implementation(
            thesis=self.thesis(),
            policy=self.policy(),
            candidates=(crypto,),
        )
        self.assertEqual(decision.status, "NO_TRADE")
        self.assertEqual(
            decision.rejected_reasons["crypto"],
            ("ASSET_CLASS_NOT_PERMITTED",),
        )

    def test_route_requirement_can_only_be_relaxed_explicitly(self):
        diagnostic = self.candidate("diagnostic", route_qualified=False)
        strict = select_implementation(
            thesis=self.thesis(),
            policy=self.policy(),
            candidates=(diagnostic,),
        )
        relaxed = select_implementation(
            thesis=self.thesis(),
            policy=self.policy(require_qualified_route=False),
            candidates=(diagnostic,),
        )
        self.assertEqual(strict.status, "NO_TRADE")
        self.assertEqual(relaxed.status, "SELECTED")
        self.assertEqual(relaxed.selected_candidate_id, "diagnostic")

    def test_cost_math_is_independent_of_ambient_decimal_context(self):
        candidate = self.candidate(
            "exact",
            fee_rate="0.00000000000000000001",
            spread_rate="0.00000000000000000002",
            holding_cost_rate="0.00000000000000000003",
        )
        values = []
        for precision in (1, 80):
            with localcontext() as context:
                context.prec = precision
                context.rounding = ROUND_DOWN
                context.traps[Inexact] = True
                context.traps[Rounded] = True
                values.append(candidate.total_cost_rate)
        self.assertEqual(values, [Decimal("0.00000000000000000006")] * 2)

    def test_duplicate_candidate_identity_fails_instead_of_tie_breaking(self):
        with self.assertRaisesRegex(ThesisImplementationError, "candidate_id must be unique"):
            select_implementation(
                thesis=self.thesis(),
                policy=self.policy(),
                candidates=(self.candidate("same"), self.candidate("same")),
            )

    def test_hostile_text_subclass_is_rejected_before_string_callbacks(self):
        calls = []

        class HostileText(str):
            def strip(self, *args, **kwargs):
                calls.append("strip")
                raise AssertionError("hostile strip executed")

            def upper(self):
                calls.append("upper")
                raise AssertionError("hostile upper executed")

        with self.assertRaises(TypeError):
            self.candidate("hostile", provider_id=HostileText("SIMULATED"))
        self.assertEqual(calls, [])

    def test_hostile_bool_subclass_and_float_numeric_ingress_fail_closed(self):
        class BoolLike(int):
            def __bool__(self):
                raise AssertionError("hostile bool executed")

        with self.assertRaises(TypeError):
            self.candidate("bool", legal=BoolLike(1))
        with self.assertRaises(TypeError):
            self.candidate("float", fee_rate=0.001)

    def test_datetime_subclass_and_custom_tzinfo_are_not_temporal_authority(self):
        class HostileDatetime(datetime):
            def astimezone(self, *args, **kwargs):
                raise AssertionError("hostile datetime executed")

        with self.assertRaises(TypeError):
            self.thesis(as_of=HostileDatetime(2026, 10, 4, 12, tzinfo=timezone.utc))

    def test_candidate_subclass_is_rejected_before_candidate_field_reads(self):
        class DerivedCandidate(ImplementationCandidate):
            pass

        derived = DerivedCandidate(**{
            "candidate_id": "derived",
            "thesis_id": "gold-down-1",
            "instrument_version": "22222222-2222-4222-8222-222222222222@1",
            "provider_id": "SIMULATED",
            "asset_class": "FUTURE",
            "exposure_direction": "SHORT",
            "route_id": "route-derived",
            "legal": True,
            "technically_available": True,
            "route_qualified": True,
            "fee_rate": Decimal("0.001"),
            "spread_rate": Decimal("0.001"),
            "holding_cost_rate": Decimal("0.001"),
            "leverage_ratio": Decimal("2"),
            "liquidation_risk": Decimal("0.10"),
            "liquidity_capacity": Decimal("50000"),
            "liquidity_currency": "USD",
            "tradable_until": AFTER_HORIZON,
        })
        with self.assertRaises(TypeError):
            select_implementation(
                thesis=self.thesis(),
                policy=self.policy(),
                candidates=(derived,),
            )

    def test_result_rejection_mapping_is_detached_and_read_only(self):
        decision = select_implementation(
            thesis=self.thesis(),
            policy=self.policy(),
            candidates=(self.candidate("bad", legal=False),),
        )
        with self.assertRaises(TypeError):
            decision.rejected_reasons["bad"] = ("FORGED",)



    def test_liquidity_capacity_never_compares_across_currencies(self):
        decision = select_implementation(
            thesis=self.thesis(notional_currency="USD"),
            policy=self.policy(),
            candidates=(
                self.candidate(
                    "eur-capacity",
                    liquidity_capacity="999999999",
                    liquidity_currency="EUR",
                ),
            ),
        )
        self.assertEqual(decision.status, "NO_TRADE")
        self.assertEqual(
            decision.rejected_reasons["eur-capacity"],
            ("LIQUIDITY_CURRENCY_MISMATCH",),
        )

    def test_instrument_version_identity_must_be_canonical(self):
        invalid = (
            "not-a-uuid@1",
            "11111111-1111-4111-8111-111111111111@0",
            "11111111-1111-4111-8111-111111111111@01",
            " 11111111-1111-4111-8111-111111111111@1",
            "11111111-1111-4111-8111-11111111111A@1",
        )
        for value in invalid:
            with self.subTest(value=value), self.assertRaisesRegex(
                ThesisImplementationError,
                "canonical instrument_id@version",
            ):
                self.candidate("bad-ref", instrument_version=value)

    def test_provider_identity_is_not_case_normalized(self):
        candidate = self.candidate("provider-case", provider_id="Provider-X")
        self.assertEqual(candidate.provider_id, "Provider-X")

    def test_zero_leverage_is_not_a_valid_screening_quantity(self):
        with self.assertRaisesRegex(
            ThesisImplementationError,
            "max_leverage_ratio must be positive",
        ):
            self.policy(max_leverage_ratio="0")
        with self.assertRaisesRegex(
            ThesisImplementationError,
            "leverage_ratio must be positive",
        ):
            self.candidate("zero-leverage", leverage_ratio="0")

    def test_terminal_cut_must_be_strictly_after_horizon(self):
        decision = select_implementation(
            thesis=self.thesis(),
            policy=self.policy(),
            candidates=(self.candidate("at-cut", tradable_until=HORIZON),),
        )
        self.assertEqual(decision.status, "NO_TRADE")
        self.assertEqual(
            decision.rejected_reasons["at-cut"],
            ("HORIZON_NOT_COVERED",),
        )

    def test_decision_constructor_rejects_internally_inconsistent_states(self):
        with self.assertRaisesRegex(
            ThesisImplementationError,
            "SELECTED requires one selected feasible Pareto candidate",
        ):
            ImplementationDecision(
                thesis_id="gold-down-1",
                status="SELECTED",
                selected_candidate_id=None,
                feasible_candidate_ids=("a",),
                pareto_frontier_ids=("a",),
                rejected_reasons={},
            )

        with self.assertRaisesRegex(
            ThesisImplementationError,
            "AMBIGUOUS requires at least two Pareto candidates",
        ):
            ImplementationDecision(
                thesis_id="gold-down-1",
                status="AMBIGUOUS",
                selected_candidate_id=None,
                feasible_candidate_ids=("a",),
                pareto_frontier_ids=("a",),
                rejected_reasons={},
            )

        with self.assertRaisesRegex(
            ThesisImplementationError,
            "NO_TRADE cannot contain selected, feasible, or Pareto candidates",
        ):
            ImplementationDecision(
                thesis_id="gold-down-1",
                status="NO_TRADE",
                selected_candidate_id=None,
                feasible_candidate_ids=("a",),
                pareto_frontier_ids=(),
                rejected_reasons={},
            )

        with self.assertRaisesRegex(
            ThesisImplementationError,
            "a candidate cannot be both feasible and rejected",
        ):
            ImplementationDecision(
                thesis_id="gold-down-1",
                status="SELECTED",
                selected_candidate_id="a",
                feasible_candidate_ids=("a",),
                pareto_frontier_ids=("a",),
                rejected_reasons={"a": ("LEGAL_RESTRICTION",)},
            )

    def test_decision_collections_require_exact_immutable_shapes(self):
        with self.assertRaisesRegex(TypeError, "feasible_candidate_ids"):
            ImplementationDecision(
                thesis_id="gold-down-1",
                status="NO_TRADE",
                selected_candidate_id=None,
                feasible_candidate_ids=[],
                pareto_frontier_ids=(),
                rejected_reasons={},
            )

if __name__ == "__main__":
    unittest.main()
