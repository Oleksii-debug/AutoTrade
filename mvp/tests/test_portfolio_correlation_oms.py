from __future__ import annotations

import builtins
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pytest

from mvp.autotrade_mvp.durable_order_projection import DurableOrderBookProjection
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.portfolio_correlation import CorrelationConcentrationError
import mvp.autotrade_mvp.portfolio_correlation_oms as correlation_oms
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


@pytest.mark.parametrize(
    ("provider_id", "account_id", "environment"),
    (
        ("OTHER", "acct:1", "SIMULATION"),
        ("SIMULATED", "acct:other", "SIMULATION"),
        ("SIMULATED", "acct:1", "REPLAY"),
    ),
)
def test_empty_oms_scope_mismatch_cannot_hide_proposal_open_exposure_domain(
    provider_id: str,
    account_id: str,
    environment: str,
):
    with TemporaryDirectory() as directory:
        oms = DurableOrderBookProjection(
            JournalStore(f"{directory}/journal.sqlite3"),
            provider_id=provider_id,
            account_id=account_id,
            environment=environment,
            host_id="host-1",
            owner_epoch="epoch-1",
        )
        proposal, evidence, resolver, policy = _passing_inputs()

        with pytest.raises(
            CorrelationConcentrationError,
            match="provider/account/environment scope does not match",
        ):
            require_correlation_safe_proposal_with_oms(
                proposal,
                evidence,
                resolver,
                policy,
                oms=oms,
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

def test_in_place_snapshots_getter_code_mutation_fails_closed():
    with TemporaryDirectory() as directory:
        oms = _oms(directory)
        _create_open_order(oms)
        proposal, evidence, resolver, policy = _passing_inputs()

        original_code = correlation_oms._OMS_SNAPSHOTS_GETTER.__code__

        def forged_snapshots(_self):
            return ()

        correlation_oms._OMS_SNAPSHOTS_GETTER.__code__ = forged_snapshots.__code__
        try:
            with pytest.raises(
                CorrelationConcentrationError,
                match="canonical OMS snapshots getter executable changed after binding",
            ):
                require_correlation_safe_proposal_with_oms(
                    proposal,
                    evidence,
                    resolver,
                    policy,
                    oms=oms,
                )
        finally:
            correlation_oms._OMS_SNAPSHOTS_GETTER.__code__ = original_code


def test_in_place_oms_authority_verifier_code_mutation_fails_closed():
    with TemporaryDirectory() as directory:
        oms = _oms(directory)
        proposal, evidence, resolver, policy = _passing_inputs()

        original_code = correlation_oms._REQUIRE_OMS_AUTHORITY.__code__

        def forged_authority(_oms):
            return None

        correlation_oms._REQUIRE_OMS_AUTHORITY.__code__ = forged_authority.__code__
        try:
            with pytest.raises(
                CorrelationConcentrationError,
                match="canonical OMS authority verifier executable changed after binding",
            ):
                require_correlation_safe_proposal_with_oms(
                    proposal,
                    evidence,
                    resolver,
                    policy,
                    oms=oms,
                )
        finally:
            correlation_oms._REQUIRE_OMS_AUTHORITY.__code__ = original_code


def test_in_place_base_correlation_guard_code_mutation_fails_closed():
    with TemporaryDirectory() as directory:
        oms = _oms(directory)
        proposal, evidence, resolver, policy = _passing_inputs()

        original_code = correlation_oms._BASE_CORRELATION_GUARD.__code__

        def forged_guard(_result, _evidence, _resolved_evidence, _policy):
            return _result

        correlation_oms._BASE_CORRELATION_GUARD.__code__ = forged_guard.__code__
        try:
            with pytest.raises(
                CorrelationConcentrationError,
                match="base correlation guard executable changed after binding",
            ):
                require_correlation_safe_proposal_with_oms(
                    proposal,
                    evidence,
                    resolver,
                    policy,
                    oms=oms,
                )
        finally:
            correlation_oms._BASE_CORRELATION_GUARD.__code__ = original_code

def test_builtin_sorted_rebinding_cannot_hide_open_order():
    with TemporaryDirectory() as directory:
        oms = _oms(directory)
        _create_open_order(oms)
        proposal, evidence, resolver, policy = _passing_inputs()
        calls: list[str] = []
        original_sorted = builtins.sorted

        def forged_sorted(*_args, **_kwargs):
            calls.append("sorted")
            return []

        caught = None
        builtins.sorted = forged_sorted
        try:
            try:
                require_correlation_safe_proposal_with_oms(
                    proposal,
                    evidence,
                    resolver,
                    policy,
                    oms=oms,
                )
            except CorrelationConcentrationError as error:
                caught = error
        finally:
            builtins.sorted = original_sorted

        assert caught is not None
        assert "canonical OMS has unvalued open exposure" in str(caught)
        assert calls == []


def test_public_open_exposure_helper_rebinding_cannot_hide_open_order():
    with TemporaryDirectory() as directory:
        oms = _oms(directory)
        _create_open_order(oms)
        proposal, evidence, resolver, policy = _passing_inputs()

        with patch.object(correlation_oms, "_unvalued_open_orders", lambda _oms: ()):
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


def test_public_open_quantity_helper_rebinding_cannot_hide_open_order():
    with TemporaryDirectory() as directory:
        oms = _oms(directory)
        _create_open_order(oms)
        proposal, evidence, resolver, policy = _passing_inputs()

        with patch.object(correlation_oms, "_exact_open_quantity", lambda _value: 0):
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


def test_in_place_open_exposure_helper_code_mutation_fails_closed():
    with TemporaryDirectory() as directory:
        oms = _oms(directory)
        proposal, evidence, resolver, policy = _passing_inputs()
        original_code = correlation_oms._unvalued_open_orders.__code__

        def forged_open_orders(_oms):
            return ()

        correlation_oms._unvalued_open_orders.__code__ = forged_open_orders.__code__
        try:
            with pytest.raises(
                CorrelationConcentrationError,
                match="OMS open-exposure resolver executable changed after binding",
            ):
                require_correlation_safe_proposal_with_oms(
                    proposal,
                    evidence,
                    resolver,
                    policy,
                    oms=oms,
                )
        finally:
            correlation_oms._unvalued_open_orders.__code__ = original_code

