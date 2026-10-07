from __future__ import annotations

import pytest

import mvp.autotrade_mvp.portfolio_correlation as correlation_module
from mvp.autotrade_mvp.portfolio_correlation import assess_correlation_concentration
from mvp.tests.test_portfolio_correlation import _e, _policy, _proposal


def test_post_construction_allocation_decision_digest_mutation_fails_closed() -> None:
    proposal = _proposal(A="10", B="10")
    evidence = (_e("A", "B", "0.1", "0.2"),)

    original_digest = proposal.decision_digest
    object.__setattr__(proposal, "decision_digest", "f" * 64)
    assert proposal.decision_digest != original_digest

    with pytest.raises(ValueError, match="allocation.*(digest|integrity)|decision_digest"):
        assess_correlation_concentration(
            proposal,
            evidence,
            {item.evidence_id: item for item in evidence},
            _policy(),
        )



@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("policy_version", "policy:2"),
        ("account_snapshot_id", "snapshot:2"),
        ("reservation_state_version", 2),
        ("reservation_state_digest", "e" * 64),
    ),
)
def test_post_construction_bound_allocation_field_mutation_fails_closed(
    field: str,
    value: object,
) -> None:
    proposal = _proposal(A="10", B="10")
    evidence = (_e("A", "B", "0.1", "0.2"),)
    object.__setattr__(proposal, field, value)

    with pytest.raises(ValueError, match="decision_digest"):
        assess_correlation_concentration(
            proposal,
            evidence,
            {item.evidence_id: item for item in evidence},
            _policy(),
        )


def test_post_construction_nested_objective_mutation_fails_closed() -> None:
    proposal = _proposal(A="10", B="10")
    evidence = (_e("A", "B", "0.1", "0.2"),)
    object.__setattr__(proposal.objective, "objective_version", "forged:v2")

    with pytest.raises(ValueError, match="decision_digest"):
        assess_correlation_concentration(
            proposal,
            evidence,
            {item.evidence_id: item for item in evidence},
            _policy(),
        )


def test_module_rebinding_cannot_replace_bound_allocation_digest_verifier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proposal = _proposal(A="10", B="10")
    evidence = (_e("A", "B", "0.1", "0.2"),)
    object.__setattr__(proposal, "policy_version", "forged-policy")
    monkeypatch.setattr(
        correlation_module,
        "_allocation_decision_digest",
        lambda *args, **kwargs: proposal.decision_digest,
    )

    with pytest.raises(ValueError, match="decision_digest"):
        assess_correlation_concentration(
            proposal,
            evidence,
            {item.evidence_id: item for item in evidence},
            _policy(),
        )
