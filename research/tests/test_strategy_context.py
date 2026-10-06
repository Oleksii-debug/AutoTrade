from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest

from research.autotrade_research.evaluation.strategy_context import (
    RegisteredStrategyCase,
    StrategyCaseOutcome,
    StrategyContext,
    StrategyFamilyStudy,
    assess_strategy_families,
)


BASE = datetime(2026, 2, 1, 12, tzinfo=timezone.utc)
POP = "sha256:" + "1" * 64
EXEC = "sha256:" + "2" * 64


def contexts():
    return (
        StrategyContext("crypto", "bull", 3600),
        StrategyContext("crypto", "crisis", 3600),
        StrategyContext("equity", "bull", 86400),
        StrategyContext("equity", "crisis", 86400),
    )


def cases():
    rows, index = [], 0
    for context in contexts():
        for family in ("mean_reversion", "trend"):
            index += 1
            decision = BASE + timedelta(days=index)
            rows.append(RegisteredStrategyCase(
                case_id=f"case-{index:02d}",
                strategy_family=family,
                context=context,
                decision_time=decision,
                assignment_time=decision - timedelta(minutes=1),
                input_population_hash=POP,
            ))
    return tuple(rows)


def study(case_rows=None, minimum=1, **overrides):
    values = dict(
        study_id="section21-study",
        registered_at=BASE - timedelta(days=1),
        evaluation_cutoff=BASE + timedelta(days=30),
        economic_unit="USD",
        cases=case_rows or cases(),
        minimum_cases_per_strategy_context=minimum,
    )
    values.update(overrides)
    return StrategyFamilyStudy.create(**values)


def economics(case):
    if case.context.asset_class == "crypto":
        preferred = "trend" if case.context.regime_id == "bull" else "mean_reversion"
    else:
        preferred = "mean_reversion" if case.context.regime_id == "bull" else "trend"
    gross = Decimal("3") if case.strategy_family == preferred else Decimal("1")
    cost = Decimal("0.2")
    return gross, cost, gross - cost


def outcome(case, s, *, family=None, context=None, population=None,
            available=None, gross=None, cost=None, net=None):
    family = family or case.strategy_family
    context = context or case.context
    population = population or case.input_population_hash
    gross0, cost0, net0 = economics(case)
    gross = gross0 if gross is None else gross
    cost = cost0 if cost is None else cost
    net = net0 if net is None else net
    return StrategyCaseOutcome(
        case_id=case.case_id,
        strategy_family=family,
        context=context,
        decision_time=case.decision_time,
        outcome_available_at=available or case.decision_time + timedelta(days=2),
        execution_reconciled_at=case.decision_time + timedelta(days=1),
        input_population_hash=population,
        execution_evidence_hash=EXEC,
        gross_value=gross,
        execution_cost=cost,
        net_value=net,
        economic_unit=s.economic_unit,
    )


def outcomes(s):
    return tuple(outcome(case, s) for case in s.cases)


class StrategyContextTests(unittest.TestCase):
    def test_assignment_must_be_known_by_decision(self):
        with self.assertRaisesRegex(ValueError, "after decision_time"):
            RegisteredStrategyCase(
                "bad", "trend", contexts()[0], BASE,
                BASE + timedelta(seconds=1), POP,
            )

    def test_study_requires_more_than_one_strategy_family(self):
        rows = tuple(c for c in cases() if c.strategy_family == "trend")
        with self.assertRaisesRegex(ValueError, "at least two strategy families"):
            study(case_rows=rows)

    def test_study_registration_precedes_first_decision(self):
        with self.assertRaisesRegex(ValueError, "registered before"):
            study(registered_at=BASE + timedelta(days=2))

    def test_hostile_case_is_rejected_before_property_access(self):
        touched = {"count": 0}
        class Hostile:
            @property
            def case_id(self):
                touched["count"] += 1
                raise AssertionError("callback")
        with self.assertRaisesRegex(TypeError, "exact RegisteredStrategyCase"):
            StrategyFamilyStudy.create(
                study_id="bad", registered_at=BASE,
                evaluation_cutoff=BASE + timedelta(days=10),
                economic_unit="USD", cases=(Hostile(),),
            )
        self.assertEqual(touched["count"], 0)

    def test_binary_float_economics_fail_closed(self):
        s = study()
        c = s.cases[0]
        with self.assertRaisesRegex(TypeError, "Decimal, string, or integer"):
            outcome(c, s, gross=1.0)

    def test_net_is_exact_gross_less_execution_cost(self):
        s = study()
        c = s.cases[0]
        with self.assertRaisesRegex(ValueError, "gross_value - execution_cost"):
            outcome(c, s, gross="1", cost="0.2", net="0.9")

    def test_complete_study_has_contextual_not_global_winners(self):
        s = study()
        result = assess_strategy_families(s, outcomes(s))
        self.assertEqual(result.status, "COVERAGE_OK")
        winners = dict(result.descriptive_best_by_context)
        self.assertEqual(winners["crypto::bull::3600"], ("trend",))
        self.assertEqual(winners["crypto::crisis::3600"], ("mean_reversion",))
        self.assertEqual(winners["equity::bull::86400"], ("mean_reversion",))
        self.assertEqual(winners["equity::crisis::86400"], ("trend",))
        self.assertFalse(hasattr(result, "global_winner"))

    def test_coverage_cannot_establish_authority(self):
        result = assess_strategy_families(study(), outcomes(study()))
        self.assertEqual(result.economic_edge_status, "UNPROVEN")
        self.assertEqual(result.registration_authority, "NOT_ESTABLISHED")
        self.assertEqual(result.regime_routing_authority, "NOT_ESTABLISHED")
        self.assertEqual(result.promotion_authority, "NOT_ESTABLISHED")
        self.assertFalse(result.grants_trading_authority)

    def test_missing_registered_case_cannot_be_cherry_picked(self):
        s = study()
        with self.assertRaisesRegex(ValueError, "every registered"):
            assess_strategy_families(s, outcomes(s)[:-1])

    def test_posthoc_strategy_change_is_rejected(self):
        s = study()
        rows = list(outcomes(s))
        rows[0] = outcome(s.cases[0], s, family="posthoc_winner")
        with self.assertRaisesRegex(ValueError, "registered strategy assignment changed"):
            assess_strategy_families(s, tuple(rows))

    def test_posthoc_context_change_is_rejected(self):
        s = study()
        rows = list(outcomes(s))
        rows[0] = outcome(
            s.cases[0], s,
            context=StrategyContext("crypto", "sideways", 3600),
        )
        with self.assertRaisesRegex(ValueError, "registered strategy assignment changed"):
            assess_strategy_families(s, tuple(rows))

    def test_population_identity_change_is_rejected(self):
        s = study()
        rows = list(outcomes(s))
        rows[0] = outcome(
            s.cases[0], s,
            population="sha256:" + "9" * 64,
        )
        with self.assertRaisesRegex(ValueError, "registered strategy assignment changed"):
            assess_strategy_families(s, tuple(rows))

    def test_late_outcome_is_rejected(self):
        s = study()
        rows = list(outcomes(s))
        rows[0] = outcome(
            s.cases[0], s,
            available=s.evaluation_cutoff + timedelta(seconds=1),
        )
        with self.assertRaisesRegex(ValueError, "not available"):
            assess_strategy_families(s, tuple(rows))

    def test_insufficient_per_context_sample_is_inconclusive(self):
        s = study(minimum=2)
        result = assess_strategy_families(s, outcomes(s))
        self.assertEqual(result.status, "INCONCLUSIVE")
        self.assertEqual(result.descriptive_best_by_context, ())
        self.assertTrue(all(
            r.startswith("LEARNING.STRATEGY_CONTEXT_SAMPLE_INSUFFICIENT:")
            for r in result.reasons
        ))

    def test_input_order_does_not_change_evidence(self):
        s = study()
        rows = outcomes(s)
        first = assess_strategy_families(s, rows)
        second = assess_strategy_families(s, tuple(reversed(rows)))
        self.assertEqual(first, second)

    def test_mutated_study_is_revalidated_at_use(self):
        s = study()
        rows = outcomes(s)
        object.__setattr__(s, "economic_unit", "EUR")
        with self.assertRaisesRegex(ValueError, "study_hash"):
            assess_strategy_families(s, rows)

    def test_mutated_outcome_is_revalidated_at_use(self):
        s = study()
        rows = list(outcomes(s))
        object.__setattr__(rows[0], "net_value", Decimal("999"))
        with self.assertRaisesRegex(ValueError, "net_value"):
            assess_strategy_families(s, tuple(rows))



    def test_semantically_valid_economic_mutation_breaks_stored_evidence_hash(self):
        s = study()
        rows = list(outcomes(s))
        object.__setattr__(
            rows[0], "gross_value", rows[0].gross_value + Decimal("100")
        )
        object.__setattr__(
            rows[0], "net_value", rows[0].net_value + Decimal("100")
        )
        with self.assertRaisesRegex(ValueError, "evidence_hash"):
            assess_strategy_families(s, tuple(rows))

    def test_semantically_valid_registered_case_mutation_breaks_study_hash(self):
        s = study()
        rows = outcomes(s)
        object.__setattr__(
            s.cases[0], "strategy_family", "posthoc_winner"
        )
        with self.assertRaisesRegex(ValueError, "study_hash"):
            assess_strategy_families(s, rows)


if __name__ == "__main__":
    unittest.main()
