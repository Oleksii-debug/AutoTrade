from dataclasses import replace
from decimal import Decimal

import pytest

from mvp.autotrade_mvp.allocation import AllocationResult, AllocationTarget
from mvp.autotrade_mvp.portfolio_correlation import (
    CorrelationConcentrationError,
    CorrelationConcentrationPolicy,
    CorrelationEvidence,
    assess_correlation_concentration,
    require_correlation_safe_allocation,
)


NOW = "2026-10-04T12:00:00Z"


def _allocation(**notionals: str) -> AllocationResult:
    targets = tuple(
        AllocationTarget(
            symbol=symbol,
            quantity=Decimal("-1") if Decimal(notional) < 0 else Decimal("1"),
            notional=Decimal(notional),
            estimated_cost=Decimal("0"),
        )
        for symbol, notional in notionals.items()
    )
    gross = sum((abs(target.notional) for target in targets), Decimal("0"))
    net = abs(sum((target.notional for target in targets), Decimal("0")))
    return AllocationResult(
        status="ALLOCATED",
        scale=Decimal("1"),
        targets=targets,
        gross_notional=gross,
        net_notional=net,
        estimated_cost=Decimal("0"),
        worst_stress_loss=Decimal("0"),
        cash_required=gross,
        reason="test",
    )


def _e(
    left: str,
    right: str,
    correlation: str,
    *,
    start="2026-10-04T11:00:00Z",
    end="2026-10-04T13:00:00Z",
):
    return CorrelationEvidence(
        left_symbol=left,
        right_symbol=right,
        correlation=Decimal(correlation),
        observed_at=start,
        valid_until=end,
        source_ref="evidence:" + ":".join(sorted((left, right))),
    )


def _policy(cap="100", threshold="0.8"):
    return CorrelationConcentrationPolicy(
        max_correlated_gross_notional=Decimal(cap),
        reinforcing_threshold=Decimal(threshold),
    )


def test_same_direction_positive_correlation_clusters_and_fails_cap():
    assessment = assess_correlation_concentration(
        _allocation(A="60", B="50"),
        (_e("A", "B", "0.9"),),
        _policy(cap="100"),
        decision_time=NOW,
    )
    assert assessment.status == "FAIL"
    assert assessment.components[0].symbols == ("A", "B")
    assert assessment.components[0].gross_notional == Decimal("110")
    assert assessment.economic_edge_status == "NOT_ESTABLISHED"
    assert assessment.grants_trading_authority is False


def test_positive_correlation_opposite_positions_do_not_reinforce():
    assessment = assess_correlation_concentration(
        _allocation(A="60", B="-50"),
        (_e("A", "B", "0.95"),),
        _policy(cap="70"),
        decision_time=NOW,
    )
    assert assessment.status == "PASS"
    assert {component.symbols for component in assessment.components} == {("A",), ("B",)}


def test_negative_correlation_opposite_positions_do_reinforce():
    assessment = assess_correlation_concentration(
        _allocation(A="60", B="-50"),
        (_e("A", "B", "-0.95"),),
        _policy(cap="100"),
        decision_time=NOW,
    )
    assert assessment.status == "FAIL"
    assert assessment.components[0].symbols == ("A", "B")


def test_negative_correlation_same_direction_positions_do_not_reinforce():
    assessment = assess_correlation_concentration(
        _allocation(A="60", B="50"),
        (_e("A", "B", "-0.95"),),
        _policy(cap="70"),
        decision_time=NOW,
    )
    assert assessment.status == "PASS"


def test_pairwise_coverage_is_fail_closed():
    assessment = assess_correlation_concentration(
        _allocation(A="10", B="10", C="10"),
        (_e("A", "B", "0.1"), _e("A", "C", "0.1")),
        _policy(),
        decision_time=NOW,
    )
    assert assessment.status == "INCONCLUSIVE"
    assert assessment.missing_pairs == (("B", "C"),)
    with pytest.raises(CorrelationConcentrationError, match="inconclusive"):
        require_correlation_safe_allocation(
            _allocation(A="10", B="10", C="10"),
            (_e("A", "B", "0.1"), _e("A", "C", "0.1")),
            _policy(),
            decision_time=NOW,
        )


def test_future_or_expired_evidence_is_inconclusive():
    future = assess_correlation_concentration(
        _allocation(A="10", B="10"),
        (
            _e(
                "A",
                "B",
                "0.9",
                start="2026-10-04T12:01:00Z",
                end="2026-10-04T13:00:00Z",
            ),
        ),
        _policy(),
        decision_time=NOW,
    )
    expired = assess_correlation_concentration(
        _allocation(A="10", B="10"),
        (
            _e(
                "A",
                "B",
                "0.9",
                start="2026-10-04T10:00:00Z",
                end="2026-10-04T11:59:59Z",
            ),
        ),
        _policy(),
        decision_time=NOW,
    )
    assert future.status == "INCONCLUSIVE"
    assert expired.status == "INCONCLUSIVE"
    assert future.stale_pairs == (("A", "B"),)
    assert expired.stale_pairs == (("A", "B"),)


def test_transitive_reinforcement_forms_one_conservative_component():
    assessment = assess_correlation_concentration(
        _allocation(A="40", B="35", C="30"),
        (
            _e("A", "B", "0.9"),
            _e("A", "C", "0.1"),
            _e("B", "C", "0.9"),
        ),
        _policy(cap="100"),
        decision_time=NOW,
    )
    assert assessment.status == "FAIL"
    assert assessment.components[0].symbols == ("A", "B", "C")
    assert assessment.components[0].gross_notional == Decimal("105")


def test_duplicate_pair_is_rejected_even_if_values_match():
    with pytest.raises(ValueError, match="duplicate"):
        assess_correlation_concentration(
            _allocation(A="10", B="10"),
            (_e("A", "B", "0.5"), _e("B", "A", "0.5")),
            _policy(),
            decision_time=NOW,
        )


def test_unknown_symbol_evidence_is_rejected():
    with pytest.raises(ValueError, match="outside the allocation"):
        assess_correlation_concentration(
            _allocation(A="10", B="10"),
            (_e("A", "C", "0.5"),),
            _policy(),
            decision_time=NOW,
        )


def test_floats_are_not_admitted_at_financial_boundary():
    with pytest.raises(TypeError):
        CorrelationEvidence(
            left_symbol="A",
            right_symbol="B",
            correlation=0.9,
            observed_at="2026-10-04T11:00:00Z",
            valid_until="2026-10-04T13:00:00Z",
            source_ref="evidence",
        )
    with pytest.raises(TypeError):
        CorrelationConcentrationPolicy(max_correlated_gross_notional=100.0)


def test_post_construction_mutation_is_revalidated_at_use():
    evidence = _e("A", "B", "0.9")
    object.__setattr__(evidence, "correlation", Decimal("2"))
    with pytest.raises(ValueError, match="between -1 and 1"):
        assess_correlation_concentration(
            _allocation(A="10", B="10"),
            (evidence,),
            _policy(),
            decision_time=NOW,
        )


def test_subclass_evidence_is_rejected_before_use():
    class Forged(CorrelationEvidence):
        pass

    forged = Forged(
        left_symbol="A",
        right_symbol="B",
        correlation=Decimal("0.9"),
        observed_at="2026-10-04T11:00:00Z",
        valid_until="2026-10-04T13:00:00Z",
        source_ref="forged",
    )
    with pytest.raises(TypeError, match="exact CorrelationEvidence"):
        assess_correlation_concentration(
            _allocation(A="10", B="10"),
            (forged,),
            _policy(),
            decision_time=NOW,
        )


def test_digest_is_order_independent_for_pair_evidence():
    allocation = _allocation(A="10", B="20", C="30")
    first = assess_correlation_concentration(
        allocation,
        (_e("A", "B", "0.2"), _e("A", "C", "0.3"), _e("B", "C", "0.4")),
        _policy(),
        decision_time=NOW,
    )
    second = assess_correlation_concentration(
        allocation,
        (_e("B", "C", "0.4"), _e("C", "A", "0.3"), _e("B", "A", "0.2")),
        _policy(),
        decision_time=NOW,
    )
    assert first.assessment_digest == second.assessment_digest


def test_single_exposure_needs_no_fabricated_pair_evidence():
    allocation = _allocation(A="40")
    assessment = assess_correlation_concentration(
        allocation,
        (),
        _policy(cap="50"),
        decision_time=NOW,
    )
    assert assessment.status == "PASS"
    assert require_correlation_safe_allocation(
        allocation,
        (),
        _policy(cap="50"),
        decision_time=NOW,
    ) is allocation


def test_single_exposure_still_respects_component_cap():
    assessment = assess_correlation_concentration(
        _allocation(A="60"),
        (),
        _policy(cap="50"),
        decision_time=NOW,
    )
    assert assessment.status == "FAIL"


def test_mutated_allocation_status_and_totals_cannot_pass_guard():
    valid = _allocation(A="40", B="-20")
    for changed, message in (
        (replace(valid, status="REJECTED"), "allocated portfolio"),
        (replace(valid, gross_notional=Decimal("1")), "aggregate notionals"),
        (replace(valid, net_notional=Decimal("1")), "aggregate notionals"),
    ):
        with pytest.raises(ValueError, match=message):
            require_correlation_safe_allocation(
                changed, (_e("A", "B", "0.2"),), _policy(), decision_time=NOW,
            )


def test_forged_target_direction_cannot_change_correlation_clustering():
    valid = _allocation(A="60", B="-50")
    forged = replace(valid, targets=(valid.targets[0], replace(valid.targets[1], quantity=Decimal("1"))))
    with pytest.raises(ValueError, match="direction disagree"):
        require_correlation_safe_allocation(
            forged, (_e("A", "B", "-0.95"),), _policy(cap="100"), decision_time=NOW,
        )
