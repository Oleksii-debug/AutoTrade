from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, localcontext
import unittest

from mvp.autotrade_mvp import risk as risk_module
from mvp.autotrade_mvp.risk import (
    RiskContext,
    RiskIntent,
    RiskPolicy,
    RISK_ARITHMETIC_POLICY_ID,
    bind_risk_decision,
    evaluate_risk,
    risk_decision_fingerprint,
    tail_scenario_set_digest,
)


def policy(**overrides):
    values = dict(
        max_abs_position="9999999999999999999999999999",
        max_single_notional="9999999999999999999999999999",
        max_gross_leverage="1",
        max_net_leverage="1",
        max_daily_loss="9999999999999999999999999999",
        max_drawdown_fraction="1",
        max_data_age_seconds="5",
        max_fx_age_seconds="60",
        min_margin_headroom="0",
        max_stress_loss="9999999999999999999999999999",
    )
    values.update(overrides)
    return RiskPolicy.create(**values)


def exact_context():
    expected = "1234567890123456789012345679"
    return RiskContext.create(
        state_version=7,
        equity=expected,
        positions={"ABC": "1234567890123456789012345678.1"},
        marks={"ABC": "1"},
        reserved_position_delta={"ABC": "0.8"},
        daily_pnl="-0.1",
        drawdown_fraction="0",
        market_data_age_seconds="0",
        fx_age_seconds={},
        margin_headroom="1",
        capability_allowed=True,
        borrow_available=True,
        stress_scenarios=({"ABC": "-1"},),
        equivalent_exposure_per_unit={"ABC": "1"},
        instrument_types={"ABC": "GENERIC"},
    )


class RiskExactArithmeticTests(unittest.TestCase):
    def test_risk_domain_subclass_is_rejected_before_virtual_reads(self):
        base = RiskIntent.create(
            symbol="ABC",
            side="BUY",
            quantity="1",
            price="1",
            expected_state_version=7,
        )

        class HostileRiskIntent(RiskIntent):
            reads = 0

            def __getattribute__(self, name):
                if name in {"symbol", "side", "quantity", "price"}:
                    type(self).reads += 1
                    raise AssertionError("hostile risk semantic read")
                return super().__getattribute__(name)

        hostile = HostileRiskIntent(**vars(base))
        with self.assertRaisesRegex(TypeError, "exact RiskIntent"):
            evaluate_risk(hostile, exact_context(), policy())
        self.assertEqual(HostileRiskIntent.reads, 0)

    def test_context_state_version_subclass_is_rejected_before_comparison(self):
        class HostileInt(int):
            comparisons = 0

            def __lt__(self, other):
                type(self).comparisons += 1
                raise AssertionError("hostile state_version comparison")

            def __eq__(self, other):
                type(self).comparisons += 1
                raise AssertionError("hostile state_version equality")

        hostile = HostileInt(7)
        with self.assertRaisesRegex(
            ValueError,
            "state_version must be a non-negative integer",
        ):
            RiskContext.create(
                state_version=hostile,
                equity="1000",
                positions={},
                marks={"ABC": "1"},
                reserved_position_delta={},
                daily_pnl="0",
                drawdown_fraction="0",
                market_data_age_seconds="0",
                fx_age_seconds={},
                margin_headroom="1",
                capability_allowed=True,
                borrow_available=True,
                stress_scenarios=({"ABC": "0"},),
            )
        self.assertEqual(HostileInt.comparisons, 0)

    def test_context_rejects_mapping_subclass_before_truthiness_or_items(self):
        touched = []

        class HostileDict(dict):
            def __bool__(self):
                touched.append("bool")
                raise AssertionError("hostile mapping truthiness")

            def items(self):
                touched.append("items")
                raise AssertionError("hostile mapping items")

        hostile = HostileDict({"ABC": "1"})
        touched.clear()

        with self.assertRaisesRegex(
            TypeError,
            "reserved_position_delta must be an exact dict",
        ):
            RiskContext.create(
                state_version=7,
                equity="1000",
                positions={},
                marks={"ABC": "1"},
                reserved_position_delta=hostile,
                daily_pnl="0",
                drawdown_fraction="0",
                market_data_age_seconds="0",
                fx_age_seconds={},
                margin_headroom="1",
                capability_allowed=True,
                borrow_available=True,
                stress_scenarios=({"ABC": "0"},),
            )

        self.assertEqual(touched, [])

    def test_context_rejects_sequence_subclass_before_iteration(self):
        touched = []

        class HostileList(list):
            def __iter__(self):
                touched.append("iter")
                raise AssertionError("hostile sequence iteration")

        hostile = HostileList([{"ABC": "0"}])
        touched.clear()

        with self.assertRaisesRegex(
            TypeError,
            "stress_scenarios must be an exact list or tuple",
        ):
            RiskContext.create(
                state_version=7,
                equity="1000",
                positions={},
                marks={"ABC": "1"},
                reserved_position_delta={},
                daily_pnl="0",
                drawdown_fraction="0",
                market_data_age_seconds="0",
                fx_age_seconds={},
                margin_headroom="1",
                capability_allowed=True,
                borrow_available=True,
                stress_scenarios=hostile,
            )

        self.assertEqual(touched, [])

    def test_direct_policy_reseal_rejects_hostile_digest_tuple_before_iteration(self):
        touched = []

        class HostileTuple(tuple):
            def __iter__(self):
                touched.append("iter")
                raise AssertionError("hostile policy digest iteration")

        configured = policy()
        hostile_pairs = HostileTuple(
            (("base", "sha256:" + "0" * 64),)
        )
        forged = RiskPolicy(
            **{
                **vars(configured),
                "required_stress_scenario_labels": ("base",),
                "required_stress_scenario_digests": hostile_pairs,
            }
        )
        intent = RiskIntent.create(
            symbol="ABC",
            side="BUY",
            quantity="0.1",
            price="1",
            expected_state_version=7,
        )
        touched.clear()

        with self.assertRaisesRegex(
            TypeError,
            "required_stress_scenario_digests must be a canonical exact tuple",
        ):
            evaluate_risk(intent, exact_context(), forged)

        self.assertEqual(touched, [])

    def test_exact_list_inputs_are_snapshotted_without_relaxing_container_boundary(self):
        context = RiskContext.create(
            state_version=7,
            equity="1000",
            positions={},
            marks={"ABC": "1"},
            reserved_position_delta={},
            daily_pnl="0",
            drawdown_fraction="0",
            market_data_age_seconds="0",
            fx_age_seconds={},
            margin_headroom="1",
            capability_allowed=True,
            borrow_available=True,
            stress_scenarios=[{"ABC": "0"}],
            stress_scenario_labels=["base"],
            tail_scenarios=[{"ABC": "0"}],
        )
        configured = policy(allowed_actions=["TRADE", "HEDGE"])

        self.assertEqual(context.stress_scenarios, ({"ABC": Decimal("0")},))
        self.assertEqual(context.stress_scenario_labels, ("base",))
        self.assertEqual(context.tail_scenarios, ({"ABC": Decimal("0")},))
        self.assertEqual(configured.allowed_actions, ("TRADE", "HEDGE"))

    def test_hostile_text_scalar_is_rejected_before_normalization(self):
        class HostileText(str):
            calls = 0

            def strip(self, *args, **kwargs):
                type(self).calls += 1
                raise AssertionError("hostile strip")

            def upper(self):
                type(self).calls += 1
                raise AssertionError("hostile upper")

        hostile = HostileText("ABC")
        with self.assertRaisesRegex(ValueError, "symbol is required"):
            RiskIntent.create(
                symbol=hostile,
                side="BUY",
                quantity="1",
                price="1",
                expected_state_version=7,
            )
        self.assertEqual(HostileText.calls, 0)

    def test_binding_rejects_polymorphic_decision_and_text_before_dispatch(self):
        raw = evaluate_risk(
            RiskIntent.create(
                symbol="ABC",
                side="BUY",
                quantity="0.1",
                price="1",
                expected_state_version=7,
            ),
            exact_context(),
            policy(),
        )

        class HostileDecision(type(raw)):
            reads = 0

            def __getattribute__(self, name):
                if name == "arithmetic_policy_id":
                    type(self).reads += 1
                    raise AssertionError("hostile decision read")
                return super().__getattribute__(name)

        hostile_decision = HostileDecision(**vars(raw))
        with self.assertRaisesRegex(TypeError, "exact RiskDecision"):
            bind_risk_decision(
                hostile_decision,
                intent_hash="intent",
                state_version=7,
                policy_version=1,
                reservation_version=0,
                reservation_requirements={"CASH:USD": "1"},
                capability_snapshot_id="capability",
                evaluated_at="2026-10-03T20:00:00+00:00",
                valid_until="2026-10-03T20:01:00+00:00",
            )
        self.assertEqual(HostileDecision.reads, 0)

        class HostileText(str):
            calls = 0

            def strip(self, *args, **kwargs):
                type(self).calls += 1
                raise AssertionError("hostile strip")

        hostile_text = HostileText("intent")
        with self.assertRaisesRegex(ValueError, "intent_hash is required"):
            bind_risk_decision(
                raw,
                intent_hash=hostile_text,
                state_version=7,
                policy_version=1,
                reservation_version=0,
                reservation_requirements={"CASH:USD": "1"},
                capability_snapshot_id="capability",
                evaluated_at="2026-10-03T20:00:00+00:00",
                valid_until="2026-10-03T20:01:00+00:00",
            )
        self.assertEqual(HostileText.calls, 0)

    def test_input_fingerprint_binds_arithmetic_policy_identity(self):
        intent = RiskIntent.create(
            symbol="ABC",
            side="BUY",
            quantity="0.1",
            price="1",
            expected_state_version=7,
        )
        context = exact_context()
        configured = policy()
        baseline = evaluate_risk(intent, context, configured)
        original_policy_id = risk_module.RISK_ARITHMETIC_POLICY_ID
        alternate_policy_id = original_policy_id + "-TEST"
        try:
            risk_module.RISK_ARITHMETIC_POLICY_ID = alternate_policy_id
            alternate = risk_module.evaluate_risk(intent, context, configured)
        finally:
            risk_module.RISK_ARITHMETIC_POLICY_ID = original_policy_id

        self.assertEqual(baseline.arithmetic_policy_id, original_policy_id)
        self.assertEqual(alternate.arithmetic_policy_id, alternate_policy_id)
        self.assertNotEqual(
            baseline.input_fingerprint,
            alternate.input_fingerprint,
        )

    def test_high_significance_financial_path_is_context_independent(self):
        intent = RiskIntent.create(
            symbol="ABC",
            side="BUY",
            quantity="0.1",
            price="1",
            expected_state_version=7,
        )
        fingerprints = set()
        decision_fingerprints = set()
        snapshots = set()
        for precision in (6, 10, 28, 80):
            for rounding in (ROUND_FLOOR, ROUND_CEILING):
                with self.subTest(precision=precision, rounding=rounding):
                    with localcontext() as decimal_context:
                        decimal_context.prec = precision
                        decimal_context.rounding = rounding
                        decision = evaluate_risk(intent, exact_context(), policy())
                    self.assertTrue(decision.admitted)
                    self.assertEqual(
                        decision.resulting_position,
                        Decimal("1234567890123456789012345679"),
                    )
                    self.assertEqual(
                        decision.worst_stress_loss,
                        Decimal("1234567890123456789012345679"),
                    )
                    self.assertEqual(decision.gross_leverage, Decimal("1"))
                    self.assertEqual(decision.net_leverage, Decimal("1"))
                    fingerprints.add(decision.input_fingerprint)
                    decision_fingerprints.add(
                        risk_decision_fingerprint(decision)
                    )
                    snapshots.add(
                        tuple(
                            (rule.rule, rule.passed, rule.observed, rule.limit)
                            for rule in decision.rules
                        )
                    )
        self.assertEqual(len(fingerprints), 1)
        self.assertEqual(len(decision_fingerprints), 1)
        self.assertEqual(len(snapshots), 1)

    def test_exact_resource_overflow_fails_before_risk_authority(self):
        oversized = "9" * 129
        with self.assertRaisesRegex(
            ValueError,
            "bounded finite decimal",
        ):
            RiskContext.create(
                state_version=7,
                equity="1000",
                positions={"ABC": oversized},
                marks={"ABC": "1"},
                daily_pnl="0",
                drawdown_fraction="0",
                market_data_age_seconds="0",
                fx_age_seconds={},
                margin_headroom="1",
                capability_allowed=True,
                borrow_available=True,
            )

    def test_polymorphic_decimal_is_rejected_before_virtual_dispatch(self):
        class HostileDecimal(Decimal):
            finite_reads = 0

            def is_finite(self):
                type(self).finite_reads += 1
                raise AssertionError("hostile Decimal method was dispatched")

        hostile = HostileDecimal("1")
        with self.assertRaisesRegex(
            TypeError,
            "quantity must use Decimal",
        ):
            RiskIntent.create(
                symbol="ABC",
                side="BUY",
                quantity=hostile,
                price="1",
                expected_state_version=7,
            )
        self.assertEqual(HostileDecimal.finite_reads, 0)

    def test_intermediate_notional_overflow_fails_closed(self):
        large = "1" + "0" * 64
        wide_limit = "9" * 128
        intent = RiskIntent.create(
            symbol="ABC",
            side="BUY",
            quantity=large,
            price=large,
            expected_state_version=7,
        )
        configured = policy(
            max_abs_position=wide_limit,
            max_single_notional=wide_limit,
            max_daily_loss=wide_limit,
            max_stress_loss=wide_limit,
        )
        with self.assertRaisesRegex(
            ValueError,
            "risk product exceeds the exact arithmetic resource envelope",
        ):
            evaluate_risk(
                intent,
                RiskContext.create(
                    state_version=7,
                    equity=wide_limit,
                    positions={},
                    marks={"ABC": large},
                    reserved_position_delta={},
                    daily_pnl="0",
                    drawdown_fraction="0",
                    market_data_age_seconds="0",
                    fx_age_seconds={},
                    margin_headroom="1",
                    capability_allowed=True,
                    borrow_available=True,
                    stress_scenarios=({"ABC": "0"},),
                ),
                configured,
            )

    def test_nonterminating_leverage_verdict_uses_exact_ratio(self):
        intent = RiskIntent.create(
            symbol="ABC",
            side="BUY",
            quantity="1",
            price="1",
            expected_state_version=7,
        )
        configured = policy(
            max_gross_leverage="0.3333331",
            max_net_leverage="0.3333331",
        )
        fingerprints = set()
        observed = set()
        for precision in (6, 10, 28, 80):
            for rounding in (ROUND_FLOOR, ROUND_CEILING):
                with self.subTest(precision=precision, rounding=rounding):
                    with localcontext() as decimal_context:
                        decimal_context.prec = precision
                        decimal_context.rounding = rounding
                        decision = evaluate_risk(
                            intent,
                            RiskContext.create(
                                state_version=7,
                                equity="3",
                                positions={},
                                marks={"ABC": "1"},
                                reserved_position_delta={},
                                daily_pnl="0",
                                drawdown_fraction="0",
                                market_data_age_seconds="0",
                                fx_age_seconds={},
                                margin_headroom="1",
                                capability_allowed=True,
                                borrow_available=True,
                                stress_scenarios=({"ABC": "-1"},),
                            ),
                            configured,
                        )
                    gross_rule = next(
                        rule
                        for rule in decision.rules
                        if rule.rule == "gross_leverage"
                    )
                    net_rule = next(
                        rule
                        for rule in decision.rules
                        if rule.rule == "net_leverage"
                    )
                    self.assertFalse(gross_rule.passed)
                    self.assertFalse(net_rule.passed)
                    self.assertFalse(decision.admitted)
                    self.assertEqual(
                        decision.gross_leverage,
                        Decimal("0.333333333333333334"),
                    )
                    self.assertEqual(
                        gross_rule.observed,
                        "0.333333333333333334",
                    )
                    self.assertEqual(
                        decision.arithmetic_policy_id,
                        RISK_ARITHMETIC_POLICY_ID,
                    )
                    fingerprints.add(risk_decision_fingerprint(decision))
                    observed.add(
                        (
                            decision.gross_leverage,
                            gross_rule.observed,
                            gross_rule.limit,
                        )
                    )
        self.assertEqual(len(fingerprints), 1)
        self.assertEqual(len(observed), 1)

    def test_expected_shortfall_one_third_rejects_exactly(self):
        tail = (
            {"ABC": "-1"},
            {"ABC": "0"},
            {"ABC": "0"},
        )
        configured = policy(
            max_expected_shortfall="0.3333331",
            expected_shortfall_tail_fraction="1",
            required_tail_scenario_set_digest=tail_scenario_set_digest(tail),
        )
        intent = RiskIntent.create(
            symbol="ABC",
            side="BUY",
            quantity="1",
            price="1",
            expected_state_version=7,
        )
        results = set()
        for precision in (6, 10, 28, 80):
            for rounding in (ROUND_FLOOR, ROUND_CEILING):
                with localcontext() as decimal_context:
                    decimal_context.prec = precision
                    decimal_context.rounding = rounding
                    decision = evaluate_risk(
                        intent,
                        RiskContext.create(
                            state_version=7,
                            equity="10",
                            positions={},
                            marks={"ABC": "1"},
                            reserved_position_delta={},
                            daily_pnl="0",
                            drawdown_fraction="0",
                            market_data_age_seconds="0",
                            fx_age_seconds={},
                            margin_headroom="1",
                            capability_allowed=True,
                            borrow_available=True,
                            stress_scenarios=({"ABC": "-1"},),
                            tail_scenarios=tail,
                        ),
                        configured,
                    )
                rule = next(
                    item
                    for item in decision.rules
                    if item.rule == "expected_shortfall"
                )
                self.assertFalse(rule.passed)
                self.assertFalse(decision.admitted)
                self.assertEqual(
                    rule.observed,
                    "0.333333333333333334",
                )
                self.assertEqual(
                    decision.arithmetic_policy_id,
                    RISK_ARITHMETIC_POLICY_ID,
                )
                results.add(
                    (
                        rule.observed,
                        rule.limit,
                        risk_decision_fingerprint(decision),
                    )
                )
        self.assertEqual(len(results), 1)

    def test_concentration_and_participation_compare_exact_one_third(self):
        intent = RiskIntent.create(
            symbol="ABC",
            side="BUY",
            quantity="1",
            price="1",
            expected_state_version=7,
        )
        configured = policy(
            max_asset_concentration_fraction="0.3333331",
            max_order_participation_fraction="0.3333331",
        )
        fingerprints = set()
        for precision in (6, 10, 28, 80):
            for rounding in (ROUND_FLOOR, ROUND_CEILING):
                with localcontext() as decimal_context:
                    decimal_context.prec = precision
                    decimal_context.rounding = rounding
                    decision = evaluate_risk(
                        intent,
                        RiskContext.create(
                            state_version=7,
                            equity="100",
                            positions={"XYZ": "1", "QRS": "1"},
                            marks={"ABC": "1", "XYZ": "1", "QRS": "1"},
                            reserved_position_delta={},
                            daily_pnl="0",
                            drawdown_fraction="0",
                            market_data_age_seconds="0",
                            fx_age_seconds={},
                            margin_headroom="1",
                            capability_allowed=True,
                            borrow_available=True,
                            stress_scenarios=(
                                {"ABC": "0", "XYZ": "0", "QRS": "0"},
                            ),
                            asset_buckets={
                                "ABC": "asset-a",
                                "XYZ": "asset-b",
                                "QRS": "asset-c",
                            },
                            liquidity_capacity={"ABC": "3"},
                            instrument_types={
                                "XYZ": "GENERIC",
                                "QRS": "GENERIC",
                            },
                        ),
                        configured,
                    )
                failed = {
                    rule.rule
                    for rule in decision.rules
                    if not rule.passed
                }
                self.assertIn("asset_concentration", failed)
                self.assertIn("liquidity_participation", failed)
                self.assertFalse(decision.admitted)
                fingerprints.add(risk_decision_fingerprint(decision))
        self.assertEqual(len(fingerprints), 1)

    def test_tail_count_exact_ceiling_does_not_round_to_wrong_sample(self):
        tail = (
            {"ABC": "-9"},
            {"ABC": "-6"},
            {"ABC": "0"},
        )
        configured = policy(
            max_expected_shortfall="8",
            expected_shortfall_tail_fraction=(
                "0.3333333333333333333333333334"
            ),
            required_tail_scenario_set_digest=tail_scenario_set_digest(tail),
        )
        intent = RiskIntent.create(
            symbol="ABC",
            side="BUY",
            quantity="1",
            price="1",
            expected_state_version=7,
        )
        outcomes = set()
        for precision in (6, 10, 28, 80):
            for rounding in (ROUND_FLOOR, ROUND_CEILING):
                with localcontext() as decimal_context:
                    decimal_context.prec = precision
                    decimal_context.rounding = rounding
                    decision = evaluate_risk(
                        intent,
                        RiskContext.create(
                            state_version=7,
                            equity="10",
                            positions={},
                            marks={"ABC": "1"},
                            reserved_position_delta={},
                            daily_pnl="0",
                            drawdown_fraction="0",
                            market_data_age_seconds="0",
                            fx_age_seconds={},
                            margin_headroom="1",
                            capability_allowed=True,
                            borrow_available=True,
                            stress_scenarios=({"ABC": "-9"},),
                            tail_scenarios=tail,
                        ),
                        configured,
                    )
                rule = next(
                    item
                    for item in decision.rules
                    if item.rule == "expected_shortfall"
                )
                self.assertTrue(rule.passed)
                self.assertEqual(rule.observed, "7.5")
                self.assertTrue(decision.admitted)
                outcomes.add(risk_decision_fingerprint(decision))
        self.assertEqual(len(outcomes), 1)


if __name__ == "__main__":
    unittest.main()
