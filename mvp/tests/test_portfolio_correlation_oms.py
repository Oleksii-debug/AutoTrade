from __future__ import annotations

import builtins
from decimal import Decimal
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



def test_changed_open_order_helper_defaults_cannot_hide_durable_open_order():
    with TemporaryDirectory() as directory:
        oms = _oms(directory)
        _create_open_order(oms)
        proposal, evidence, resolver, policy = _passing_inputs()
        helper = correlation_oms._unvalued_open_orders
        original_defaults = helper.__defaults__
        parameter_names = helper.__code__.co_varnames[:helper.__code__.co_argcount]
        default_offset = len(parameter_names) - len(original_defaults)
        changed = list(original_defaults)
        forged_calls = []

        def forged_snapshots(_oms):
            forged_calls.append("forged-snapshots")
            return ()

        for name, replacement in (
            ("_snapshots_getter", forged_snapshots),
            ("_snapshots_getter_code", forged_snapshots.__code__),
        ):
            changed[parameter_names.index(name) - default_offset] = replacement

        helper.__defaults__ = tuple(changed)
        try:
            with pytest.raises(
                CorrelationConcentrationError,
                match="OMS open-exposure resolver defaults changed after binding",
            ):
                require_correlation_safe_proposal_with_oms(
                    proposal, evidence, resolver, policy, oms=oms,
                )
        finally:
            helper.__defaults__ = original_defaults
        assert forged_calls == []


def test_in_place_quantity_parser_defaults_cannot_zero_open_order():
    with TemporaryDirectory() as directory:
        oms = _oms(directory)
        _create_open_order(oms)
        proposal, evidence, resolver, policy = _passing_inputs()
        helper = correlation_oms._exact_open_quantity
        original_defaults = dict(helper.__kwdefaults__)
        forged_calls = []

        def forged_parse(_value):
            forged_calls.append("forged-parser")
            return Decimal("0")

        helper.__kwdefaults__["_parse"] = forged_parse
        helper.__kwdefaults__["_parse_code"] = forged_parse.__code__
        try:
            with pytest.raises(
                CorrelationConcentrationError,
                match="OMS quantity normalizer defaults changed after binding",
            ):
                require_correlation_safe_proposal_with_oms(
                    proposal, evidence, resolver, policy, oms=oms,
                )
        finally:
            helper.__kwdefaults__.clear()
            helper.__kwdefaults__.update(original_defaults)
        assert forged_calls == []


def test_in_place_public_admission_defaults_cannot_replace_oms_guard():
    with TemporaryDirectory() as directory:
        oms = _oms(directory)
        _create_open_order(oms)
        proposal, evidence, resolver, policy = _passing_inputs()
        function = correlation_oms.require_correlation_safe_proposal_with_oms
        original_defaults = dict(function.__kwdefaults__)
        forged_calls = []

        def forged_open_orders(_oms):
            forged_calls.append("forged-open-orders")
            return ()

        function.__kwdefaults__["_open_orders"] = forged_open_orders
        function.__kwdefaults__["_open_orders_code"] = forged_open_orders.__code__
        try:
            with pytest.raises(
                CorrelationConcentrationError,
                match="OMS admission defaults changed after binding",
            ):
                function(proposal, evidence, resolver, policy, oms=oms)
        finally:
            function.__kwdefaults__.clear()
            function.__kwdefaults__.update(original_defaults)
        assert forged_calls == []


def test_explicit_helper_override_cannot_bypass_open_oms_exposure():
    with TemporaryDirectory() as directory:
        oms = _oms(directory)
        _create_open_order(oms)
        proposal, evidence, resolver, policy = _passing_inputs()
        forged_calls = []

        def forged_open_orders(_oms):
            forged_calls.append("forged-open-orders")
            return ()

        with pytest.raises(
            CorrelationConcentrationError,
            match="OMS admission helper override is not authoritative",
        ):
            require_correlation_safe_proposal_with_oms(
                proposal,
                evidence,
                resolver,
                policy,
                oms=oms,
                _open_orders=forged_open_orders,
                _open_orders_code=forged_open_orders.__code__,
            )
        assert forged_calls == []


def test_in_place_verifier_lookup_defaults_cannot_hide_open_order():
    with TemporaryDirectory() as directory:
        oms = _oms(directory)
        _create_open_order(oms)
        proposal, evidence, resolver, policy = _passing_inputs()
        verifier = correlation_oms._require_bound_executable
        getter = correlation_oms._OMS_SNAPSHOTS_GETTER
        original_verifier_defaults = dict(verifier.__kwdefaults__)
        original_getter_code = getter.__code__
        forged_calls = []

        def forged_snapshots(_self):
            return ()

        def forged_code_lookup(function, attribute):
            if function is getter and attribute == "__code__":
                forged_calls.append("forged-code-lookup")
                return original_getter_code
            return object.__getattribute__(function, attribute)

        verifier.__kwdefaults__["_get"] = forged_code_lookup
        getter.__code__ = forged_snapshots.__code__
        try:
            with pytest.raises(
                CorrelationConcentrationError,
                match="OMS executable verifier defaults changed after binding",
            ):
                require_correlation_safe_proposal_with_oms(
                    proposal, evidence, resolver, policy, oms=oms,
                )
        finally:
            getter.__code__ = original_getter_code
            verifier.__kwdefaults__.clear()
            verifier.__kwdefaults__.update(original_verifier_defaults)
        assert forged_calls == []


@pytest.mark.parametrize(
    ("provider_id", "account_id", "environment"),
    [
        ("OTHER", "acct:1", "SIMULATION"),
        ("SIMULATED", "acct:other", "SIMULATION"),
        ("SIMULATED", "acct:1", "REPLAY"),
    ],
)
def test_wrong_account_empty_oms_never_authorizes_allocation(
    provider_id: str, account_id: str, environment: str
):
    """An empty OMS from another authority scope must not admit this proposal."""
    with TemporaryDirectory() as directory:
        unrelated_oms = DurableOrderBookProjection(
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
            match="OMS provider/account/environment does not match allocation",
        ):
            require_correlation_safe_proposal_with_oms(
                proposal, evidence, resolver, policy, oms=unrelated_oms
            )


def test_matching_scope_oms_with_open_order_remains_inconclusive():
    """Scope equality must never weaken the existing open exposure barrier."""
    with TemporaryDirectory() as directory:
        oms = _oms(directory)
        _create_open_order(oms)
        proposal, evidence, resolver, policy = _passing_inputs()
        with pytest.raises(
            CorrelationConcentrationError,
            match="canonical OMS has unvalued open exposure",
        ):
            require_correlation_safe_proposal_with_oms(
                proposal, evidence, resolver, policy, oms=oms
            )


@pytest.mark.parametrize(
    ("terminal_outcome", "expected_state"),
    [
        ("confirmed_cancel", "CANCELLED"),
        ("confirmed_expiry", "EXPIRED"),
        ("provider_rejection", "REJECTED"),
    ],
)
def test_terminal_unfilled_remainder_is_not_working_oms_exposure(
    terminal_outcome: str, expected_state: str,
):
    """An order's open_quantity is an unfilled remainder even when terminal."""
    with TemporaryDirectory() as directory:
        oms = _oms(directory)
        _create_open_order(oms)
        if terminal_outcome == "confirmed_cancel":
            oms.request_cancel(
                event_key="cancel-request",
                client_order_id="open-order-1",
                command_id="cancel-command",
                committed_at=NOW,
            )
            with pytest.raises(
                CorrelationConcentrationError,
                match="canonical OMS has unvalued open exposure",
            ):
                proposal, evidence, resolver, policy = _passing_inputs()
                require_correlation_safe_proposal_with_oms(
                    proposal, evidence, resolver, policy, oms=oms,
                )
            oms.confirm_cancel(
                event_key="cancel-confirmation",
                client_order_id="open-order-1",
                committed_at=NOW,
            )
        elif terminal_outcome == "confirmed_expiry":
            oms.confirm_expired(
                event_key="expiry-confirmation",
                client_order_id="open-order-1",
                committed_at=NOW,
            )
        else:
            oms.acknowledge(
                event_key="provider-rejection",
                client_order_id="open-order-1",
                status="REJECTED",
                committed_at=NOW,
            )
        # A fresh durable projection must make the same terminal decision;
        # the guard cannot depend on in-process transient cancellation flags.
        restored = _oms(directory)
        snapshot = restored.snapshots[0]
        assert snapshot.state == expected_state
        assert snapshot.open_quantity > Decimal("0")
        proposal, evidence, resolver, policy = _passing_inputs()
        assert require_correlation_safe_proposal_with_oms(
            proposal, evidence, resolver, policy, oms=restored,
        ) is proposal


def test_a_terminal_order_cannot_mask_a_second_working_order():
    with TemporaryDirectory() as directory:
        oms = _oms(directory)
        _create_open_order(oms)
        oms.confirm_cancel(
            event_key="cancel-first",
            client_order_id="open-order-1",
            committed_at=NOW,
        )
        oms.create_order(
            event_key="create-second",
            client_order_id="open-order-2",
            instrument="B",
            side="SELL",
            requested_quantity="2",
            committed_at=NOW,
        )
        states = {snapshot.client_order_id: snapshot.state for snapshot in oms.snapshots}
        assert states == {"open-order-1": "CANCELLED", "open-order-2": "PENDING"}
        proposal, evidence, resolver, policy = _passing_inputs()
        with pytest.raises(
            CorrelationConcentrationError,
            match="canonical OMS has unvalued open exposure",
        ):
            require_correlation_safe_proposal_with_oms(
                proposal, evidence, resolver, policy, oms=_oms(directory),
            )


def test_rejected_cancel_retains_unvalued_open_oms_exposure():
    with TemporaryDirectory() as directory:
        oms = _oms(directory)
        _create_open_order(oms)
        oms.request_cancel(
            event_key="cancel-request",
            client_order_id="open-order-1",
            command_id="cancel-command",
            committed_at=NOW,
        )
        oms.reject_cancel(
            event_key="cancel-rejection",
            client_order_id="open-order-1",
            command_id="cancel-command",
            reason_code="STILL_WORKING",
            committed_at=NOW,
        )
        assert oms.snapshots[0].state == "PENDING"
        proposal, evidence, resolver, policy = _passing_inputs()
        with pytest.raises(
            CorrelationConcentrationError,
            match="canonical OMS has unvalued open exposure",
        ):
            require_correlation_safe_proposal_with_oms(
                proposal, evidence, resolver, policy, oms=oms,
            )


def test_partially_filled_cancelled_order_requires_fill_reconciliation():
    """Terminal remainder is safe to release only when no fills need accounting."""
    with TemporaryDirectory() as directory:
        oms = _oms(directory)
        _create_open_order(oms)
        oms.record_fill(
            event_key="partial-fill",
            client_order_id="open-order-1",
            fill_id="partial-f1",
            provider_execution_id="execution-f1",
            quantity="0.25",
            price="100",
            committed_at=NOW,
        )
        oms.request_cancel(
            event_key="cancel-request",
            client_order_id="open-order-1",
            command_id="cancel-command",
            committed_at=NOW,
        )
        oms.confirm_cancel(
            event_key="cancel-confirmation",
            client_order_id="open-order-1",
            committed_at=NOW,
        )
        fresh_oms = _oms(directory)
        snapshot = fresh_oms.snapshots[0]
        assert snapshot.state == "PARTIALLY_FILLED_CANCELLED"
        assert snapshot.filled_quantity == Decimal("0.25")
        assert snapshot.open_quantity == Decimal("0.75")
        proposal, evidence, resolver, policy = _passing_inputs()
        with pytest.raises(
            CorrelationConcentrationError,
            match="canonical OMS has unvalued open exposure",
        ):
            require_correlation_safe_proposal_with_oms(
                proposal, evidence, resolver, policy, oms=fresh_oms,
            )
