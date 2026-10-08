from dataclasses import replace
from decimal import Decimal

import pytest

import mvp.autotrade_mvp.portfolio_correlation as correlation_module
from mvp.autotrade_mvp.allocation import (
    AllocationResult,
    AllocationTarget,
    EvidenceBoundObjectiveAllocationResult,
    ObjectiveAllocationResult,
    _allocation_decision_digest,
)
from mvp.autotrade_mvp.portfolio_correlation import (
    CorrelationConcentrationError,
    CorrelationConcentrationPolicy,
    CorrelationEvidence,
    assess_correlation_concentration,
    require_correlation_safe_proposal,
)

NOW = "2026-10-04T12:00:00Z"


def _proposal(**notionals: str):
    targets = tuple(
        AllocationTarget(
            symbol=symbol,
            quantity=Decimal("-1") if Decimal(notional) < 0 else Decimal("1"),
            notional=Decimal(notional),
            estimated_cost=Decimal("0"),
        )
        for symbol, notional in notionals.items()
    )
    gross = sum((abs(item.notional) for item in targets), Decimal("0"))
    net = abs(sum((item.notional for item in targets), Decimal("0")))
    allocation = AllocationResult(
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
    objective = ObjectiveAllocationResult(
        allocation=allocation,
        selected_symbols=tuple(notionals),
        expected_net_utility=Decimal("0"),
        objective_version="test:v1",
        reason="test",
    )
    evidence_refs = ()
    environment = "SIMULATION"
    policy_version = "policy:1"
    policy_config_digest = "b" * 64
    objective_search_config_digest = "c" * 64
    provider_id = "SIMULATED"
    account_id = "acct:1"
    instrument_versions = tuple((s, f"instrument:{s}:1") for s in notionals)
    capability_snapshot_ids = tuple((s, f"capability:{s}:1") for s in notionals)
    account_snapshot_id = "snapshot:1"
    reconciliation_run_id = "reconciliation:1"
    account_state_version = 1
    reservation_state_version = 1
    reservation_state_digest = "d" * 64
    base_currency = "USD"
    decision_digest = _allocation_decision_digest(
        objective,
        evidence_refs=evidence_refs,
        environment=environment,
        policy_version=policy_version,
        policy_config_digest=policy_config_digest,
        objective_search_config_digest=objective_search_config_digest,
        decision_time=NOW,
        provider_id=provider_id,
        account_id=account_id,
        instrument_versions=instrument_versions,
        capability_snapshot_ids=capability_snapshot_ids,
        account_snapshot_id=account_snapshot_id,
        reconciliation_run_id=reconciliation_run_id,
        account_state_version=account_state_version,
        reservation_state_version=reservation_state_version,
        reservation_state_digest=reservation_state_digest,
        base_currency=base_currency,
    )
    return EvidenceBoundObjectiveAllocationResult(
        objective=objective,
        decision_digest=decision_digest,
        policy_config_digest=policy_config_digest,
        objective_search_config_digest=objective_search_config_digest,
        evidence_refs=evidence_refs,
        environment=environment,
        policy_version=policy_version,
        decision_time=NOW,
        provider_id=provider_id,
        account_id=account_id,
        instrument_versions=instrument_versions,
        capability_snapshot_ids=capability_snapshot_ids,
        account_snapshot_id=account_snapshot_id,
        reconciliation_run_id=reconciliation_run_id,
        account_state_version=account_state_version,
        reservation_state_version=reservation_state_version,
        reservation_state_digest=reservation_state_digest,
        base_currency=base_currency,
    )


def _e(left, right, lower, upper=None, *, observed="2026-10-04T11:00:00Z", valid_until="2026-10-04T13:00:00Z", environment="SIMULATION", suffix=""):
    upper = lower if upper is None else upper
    pair = ":".join(sorted((left, right)))
    return CorrelationEvidence.create(
        evidence_id=f"correlation:{pair}{suffix}",
        environment=environment,
        left_symbol=left,
        right_symbol=right,
        correlation_lower=Decimal(lower),
        correlation_upper=Decimal(upper),
        observed_at=observed,
        valid_until=valid_until,
        sample_start="2026-09-01T00:00:00Z",
        sample_end="2026-10-04T10:00:00Z",
        estimator_id="pearson-shrinkage:v1",
        uncertainty_method="bootstrap-interval:v1",
        source_ref=f"artifact:{pair}:sha256:test",
    )


def _policy(cap="100", threshold="0.8", currency="USD"):
    return CorrelationConcentrationPolicy(
        policy_id="portfolio-policy:1",
        reporting_currency=currency,
        max_correlated_gross_notional=Decimal(cap),
        reinforcing_threshold=Decimal(threshold),
    )


def _assess(proposal, evidence, policy=None):
    return assess_correlation_concentration(
        proposal,
        evidence,
        {item.evidence_id: item for item in evidence},
        _policy() if policy is None else policy,
    )


def test_same_direction_uses_interval_upper_bound_and_fails_cap():
    result = _assess(
        _proposal(A="60", B="50"),
        (_e("A", "B", "0.2", "0.9"),),
        _policy(cap="100"),
    )
    assert result.status == "FAIL"
    assert result.components[0].symbols == ("A", "B")
    assert result.components[0].gross_notional == Decimal("110")
    assert result.economic_edge_status == "NOT_ESTABLISHED"
    assert result.evidence_authority_status == "RESOLVER_NOT_FINANCIAL_AUTHORITY"
    assert result.grants_trading_authority is False


def test_opposite_direction_uses_negated_interval_lower_bound():
    result = _assess(
        _proposal(A="60", B="-50"),
        (_e("A", "B", "-0.9", "0.2"),),
        _policy(cap="100"),
    )
    assert result.status == "FAIL"
    assert result.components[0].symbols == ("A", "B")


def test_sign_specific_nonreinforcing_relationships_remain_separate():
    positive_opposite = _assess(
        _proposal(A="60", B="-50"),
        (_e("A", "B", "0.8", "0.95"),),
        _policy(cap="70"),
    )
    negative_same = _assess(
        _proposal(A="60", B="50"),
        (_e("A", "B", "-0.95", "-0.8"),),
        _policy(cap="70"),
    )
    assert positive_opposite.status == "PASS"
    assert negative_same.status == "PASS"


def test_interval_spanning_both_signs_is_conservative_for_both_directions():
    evidence = (_e("A", "B", "-0.9", "0.9"),)
    assert _assess(_proposal(A="60", B="50"), evidence, _policy(cap="100")).status == "FAIL"
    assert _assess(_proposal(A="60", B="-50"), evidence, _policy(cap="100")).status == "FAIL"


def test_pair_coverage_is_fail_closed_and_non_authorizing():
    proposal = _proposal(A="10", B="10", C="10")
    evidence = (_e("A", "B", "0.1"), _e("A", "C", "0.1"))
    result = _assess(proposal, evidence)
    assert result.status == "INCONCLUSIVE"
    assert result.missing_pairs == (("B", "C"),)
    with pytest.raises(CorrelationConcentrationError, match="inconclusive"):
        require_correlation_safe_proposal(
            proposal, evidence, {item.evidence_id: item for item in evidence}, _policy()
        )


def test_future_and_expired_evidence_are_inconclusive():
    future = _e("A", "B", "0.8", "0.9", observed="2026-10-04T12:01:00Z")
    expired = _e(
        "A", "B", "0.8", "0.9",
        observed="2026-10-04T10:00:00Z", valid_until="2026-10-04T11:59:59Z",
    )
    assert _assess(_proposal(A="10", B="10"), (future,)).stale_pairs == (("A", "B"),)
    assert _assess(_proposal(A="10", B="10"), (expired,)).status == "INCONCLUSIVE"


def test_reinforcement_clusters_transitively_and_conservatively():
    result = _assess(
        _proposal(A="40", B="35", C="30"),
        (
            _e("A", "B", "0.7", "0.9"),
            _e("A", "C", "0.0", "0.2"),
            _e("B", "C", "0.7", "0.9"),
        ),
        _policy(cap="100"),
    )
    assert result.status == "FAIL"
    assert result.components[0].symbols == ("A", "B", "C")
    assert result.components[0].gross_notional == Decimal("105")


def test_duplicate_pair_and_unknown_symbol_are_rejected():
    with pytest.raises(ValueError, match="duplicate"):
        _assess(
            _proposal(A="10", B="10"),
            (_e("A", "B", "0.4", suffix=":1"), _e("B", "A", "0.4", suffix=":2")),
        )
    with pytest.raises(ValueError, match="outside the allocation"):
        _assess(_proposal(A="10", B="10"), (_e("A", "C", "0.4"),))


def test_policy_has_no_hidden_threshold_and_rejects_float_money():
    with pytest.raises(TypeError):
        CorrelationConcentrationPolicy(
            policy_id="p", reporting_currency="USD",
            max_correlated_gross_notional=100.0, reinforcing_threshold=Decimal("0.8")
        )
    with pytest.raises(TypeError):
        CorrelationConcentrationPolicy(
            policy_id="p", reporting_currency="USD",
            max_correlated_gross_notional=Decimal("100")
        )


def test_invalid_uncertainty_interval_and_future_sample_are_rejected():
    with pytest.raises(ValueError, match="correlation interval"):
        _e("A", "B", "0.9", "0.8")
    with pytest.raises(ValueError, match="correlation interval"):
        _e("A", "B", "-1.1", "0.8")
    with pytest.raises(ValueError, match="sample_end"):
        CorrelationEvidence.create(
            evidence_id="e", environment="SIMULATION", left_symbol="A", right_symbol="B",
            correlation_lower=Decimal("0"), correlation_upper=Decimal("0.1"),
            observed_at="2026-10-04T11:00:00Z", valid_until="2026-10-04T13:00:00Z",
            sample_start="2026-10-04T10:00:00Z", sample_end="2026-10-04T12:00:00Z",
            estimator_id="e", uncertainty_method="u", source_ref="s",
        )


def test_post_construction_mutation_and_subclass_fail_content_boundary():
    item = _e("A", "B", "0.7", "0.9")
    object.__setattr__(item, "correlation_upper", Decimal("0.8"))
    with pytest.raises(ValueError, match="digest"):
        _assess(_proposal(A="10", B="10"), (item,))

    class Forged(CorrelationEvidence):
        pass

    base = _e("A", "B", "0.7", "0.9")
    forged = Forged(**base.__dict__)
    with pytest.raises(TypeError, match="exact CorrelationEvidence"):
        _assess(_proposal(A="10", B="10"), (forged,))


def test_resolver_is_required_and_content_must_match():
    proposal = _proposal(A="10", B="10")
    supplied = _e("A", "B", "0.1", "0.2")
    with pytest.raises(ValueError, match="cannot be resolved authoritatively"):
        assess_correlation_concentration(proposal, (supplied,), {}, _policy())
    replacement = _e("A", "B", "0.8", "0.9")
    object.__setattr__(replacement, "evidence_id", supplied.evidence_id)
    with pytest.raises(ValueError):
        assess_correlation_concentration(
            proposal, (supplied,), {supplied.evidence_id: replacement}, _policy()
        )


def test_environment_and_reporting_currency_must_match_proposal():
    with pytest.raises(ValueError, match="environment mismatch"):
        _assess(_proposal(A="10", B="10"), (_e("A", "B", "0.1", environment="PAPER"),))
    with pytest.raises(ValueError, match="reporting_currency"):
        _assess(_proposal(A="10", B="10"), (_e("A", "B", "0.1"),), _policy(currency="EUR"))


def test_assessment_digest_is_pair_order_independent():
    proposal = _proposal(A="10", B="20", C="30")
    evidence = (
        _e("A", "B", "0.1", "0.2"),
        _e("A", "C", "0.2", "0.3"),
        _e("B", "C", "0.3", "0.4"),
    )
    assert _assess(proposal, evidence).assessment_digest == _assess(proposal, tuple(reversed(evidence))).assessment_digest


def test_single_exposure_needs_no_pair_evidence_but_still_obeys_cap():
    proposal = _proposal(A="40")
    assert _assess(proposal, (), _policy(cap="50")).status == "PASS"
    assert require_correlation_safe_proposal(proposal, (), {}, _policy(cap="50")) is proposal
    assert _assess(_proposal(A="60"), (), _policy(cap="50")).status == "FAIL"


def test_forged_allocation_status_totals_and_target_direction_fail_closed():
    proposal = _proposal(A="40", B="-20")
    allocation = proposal.objective.allocation
    for changed, message in (
        (replace(allocation, status="REJECTED"), "allocated portfolio"),
        (replace(allocation, gross_notional=Decimal("1")), "aggregate notionals"),
        (replace(allocation, net_notional=Decimal("1")), "aggregate notionals"),
    ):
        forged = replace(proposal, objective=replace(proposal.objective, allocation=changed))
        with pytest.raises(ValueError, match=message):
            _assess(forged, (_e("A", "B", "0.2"),))
    bad_target = replace(allocation.targets[1], quantity=Decimal("1"))
    forged = replace(
        proposal,
        objective=replace(
            proposal.objective,
            allocation=replace(allocation, targets=(allocation.targets[0], bad_target)),
        ),
    )
    with pytest.raises(ValueError, match="direction disagree"):
        _assess(forged, (_e("A", "B", "-0.95"),))


def test_require_guard_retains_bound_assessment_after_module_rebinding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proposal = _proposal(A="60")

    monkeypatch.setattr(
        correlation_module,
        "assess_correlation_concentration",
        lambda *_args, **_kwargs: None,
    )

    with pytest.raises(CorrelationConcentrationError, match="fail"):
        require_correlation_safe_proposal(
            proposal,
            (),
            {},
            _policy(cap="50"),
        )


def test_require_guard_rejects_in_place_assessment_code_mutation() -> None:
    proposal = _proposal(A="40")
    retained = correlation_module.assess_correlation_concentration
    original_code = retained.__code__

    def forged_assessment(*_args, **_kwargs):
        raise AssertionError("forged correlation assessment executed")

    retained.__code__ = forged_assessment.__code__
    try:
        with pytest.raises(
            CorrelationConcentrationError,
            match="assessment executable changed after binding",
        ):
            require_correlation_safe_proposal(
                proposal,
                (),
                {},
                _policy(cap="50"),
            )
    finally:
        retained.__code__ = original_code


def test_result_class_attribute_dispatch_is_not_used_for_financial_fields():
    proposal = _proposal(A="40")
    result_type = type(proposal)
    touched = []

    def forged_getattribute(self, name):
        touched.append(name)
        raise AssertionError("forged result attribute dispatch executed")

    result_type.__getattribute__ = forged_getattribute
    try:
        result = assess_correlation_concentration(proposal, (), {}, _policy(cap="50"))
    finally:
        del result_type.__getattribute__
    assert result.status == "PASS"
    assert touched == []
