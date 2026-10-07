from __future__ import annotations

import pytest

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
