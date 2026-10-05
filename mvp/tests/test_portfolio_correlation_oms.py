from __future__ import annotations

from tempfile import TemporaryDirectory
from unittest.mock import patch

import pytest

from mvp.autotrade_mvp.durable_order_projection import DurableOrderBookProjection
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.portfolio_correlation import CorrelationConcentrationError
from mvp.autotrade_mvp.portfolio_correlation_oms import (
    require_correlation_safe_proposal_with_oms,
)
from mvp.tests.test_portfolio_correlation import NOW, _e, _policy, _proposal


def _oms(directory: str) -> DurableOrderBookProjection:
    return DurableOrderBookProjection(
        JournalStore(f"{directory}/journal.sqlite3"),
        provider_id="SIMULATED",
        account_id="acct:1",
        environment="SIMULATION",
        host_id="host-1",
        owner_epoch="epoch-1",
    )


def _passing_inputs():
    proposal = _proposal(A="10", B="10")
    evidence = (_e("A", "B", "0.1", "0.2"),)
    resolver = {item.evidence_id: item for item in evidence}
    return proposal, evidence, resolver, _policy(cap="100")


def _create_open_order(oms: DurableOrderBookProjection) -> None:
    oms.create_order(
        event_key="create-open-order",
        client_order_id="open-order-1",
        instrument="A",
        side="BUY",
        requested_quantity="1",
        committed_at=NOW,
    )


def test_open_durable_order_blocks_otherwise_passing_correlation_admission():
    with TemporaryDirectory() as directory:
        oms = _oms(directory)
        _create_open_order(oms)
        proposal, evidence, resolver, policy = _passing_inputs()

        with pytest.raises(
            CorrelationConcentrationError,
            match="canonical OMS has unvalued open exposure",
        ):
            require_correlation_safe_proposal_with_oms(
                proposal,
                evidence,
                resolver,
                policy,
                oms=oms,
            )


def test_empty_canonical_oms_delegates_to_existing_correlation_guard():
    with TemporaryDirectory() as directory:
        oms = _oms(directory)
        proposal, evidence, resolver, policy = _passing_inputs()

        returned = require_correlation_safe_proposal_with_oms(
            proposal,
            evidence,
            resolver,
            policy,
            oms=oms,
        )

        assert returned is proposal


def test_public_snapshots_rebinding_cannot_hide_open_order():
    with TemporaryDirectory() as directory:
        oms = _oms(directory)
        _create_open_order(oms)
        proposal, evidence, resolver, policy = _passing_inputs()
        public_calls: list[str] = []

        def forged_snapshots(_self):
            public_calls.append("snapshots")
            return ()

        with patch.object(
            DurableOrderBookProjection,
            "snapshots",
            new=property(forged_snapshots),
        ):
            with pytest.raises(
                CorrelationConcentrationError,
                match="canonical OMS has unvalued open exposure",
            ):
                require_correlation_safe_proposal_with_oms(
                    proposal,
                    evidence,
                    resolver,
                    policy,
                    oms=oms,
                )

        assert public_calls == []


def test_empty_oms_does_not_turn_missing_pair_evidence_into_authority():
    with TemporaryDirectory() as directory:
        oms = _oms(directory)
        proposal = _proposal(A="10", B="10")

        with pytest.raises(CorrelationConcentrationError, match="inconclusive"):
            require_correlation_safe_proposal_with_oms(
                proposal,
                (),
                {},
                _policy(cap="100"),
                oms=oms,
            )


def test_noncanonical_oms_object_is_rejected_before_correlation_admission():
    proposal, evidence, resolver, policy = _passing_inputs()

    with pytest.raises(TypeError, match="exact DurableOrderBookProjection"):
        require_correlation_safe_proposal_with_oms(
            proposal,
            evidence,
            resolver,
            policy,
            oms=object(),
        )
