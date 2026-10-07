"""Fail-closed OMS composition for the WP-32 correlation guard.

The canonical durable OMS owns lifecycle/open-quantity truth, but its order
snapshot deliberately does not claim an authority-bearing base-currency order
price/notional.  Correlation admission must therefore not ignore open orders or
invent monetary exposure from quantity alone.  This composition blocks while
any exact durable OMS snapshot has non-zero open quantity and delegates to the
existing correlation guard only when that ambiguity is absent.

This module is a safety prerequisite only.  It does not add another allocator,
OMS, valuation engine, correlation model, financial admission issuer, or
trading authority.  Full Product Specification section 25 still requires an
authenticated valuation cut for working/UNKNOWN order exposure.
"""
from __future__ import annotations

from decimal import Decimal

from .durable_order_projection import (
    DurableOrderBookProjection,
    require_exact_order_projection_authority,
)
from .exact_decimal import ExactDecimalError, parse_bounded_exact_decimal
from .order_projection import OrderSnapshot
from .portfolio_correlation import (
    CorrelationConcentrationError,
    require_correlation_safe_proposal,
)


# Capture the installed authority/use primitives once.  Later rebinding of the
# public class property/module aliases must not make an open order disappear
# from a financial safety decision.
_OMS_TYPE = DurableOrderBookProjection
_SNAPSHOT_TYPE = OrderSnapshot
_REQUIRE_OMS_AUTHORITY = require_exact_order_projection_authority
_OMS_SNAPSHOTS_GETTER = DurableOrderBookProjection.snapshots.fget
_BASE_CORRELATION_GUARD = require_correlation_safe_proposal
_GET = object.__getattribute__

if _OMS_SNAPSHOTS_GETTER is None:  # pragma: no cover - import-time invariant
    raise RuntimeError("DurableOrderBookProjection.snapshots lost its getter")

_REQUIRE_OMS_AUTHORITY_CODE = _GET(_REQUIRE_OMS_AUTHORITY, "__code__")
_OMS_SNAPSHOTS_GETTER_CODE = _GET(_OMS_SNAPSHOTS_GETTER, "__code__")
_BASE_CORRELATION_GUARD_CODE = _GET(_BASE_CORRELATION_GUARD, "__code__")


def _require_bound_executable(function, expected_code, *, name: str) -> None:
    if _GET(function, "__code__") is not expected_code:
        raise CorrelationConcentrationError(
            f"{name} executable changed after binding"
        )


def _exact_text(value: object, *, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise CorrelationConcentrationError(
            f"canonical OMS {name} must be exact non-empty text"
        )
    return value


def _exact_open_quantity(value: object) -> Decimal:
    if type(value) not in {Decimal, str, int} or type(value) is bool:
        raise CorrelationConcentrationError(
            "canonical OMS open_quantity must use exact Decimal, string or integer"
        )
    try:
        amount = parse_bounded_exact_decimal(value)
    except (ExactDecimalError, TypeError, ValueError) as error:
        raise CorrelationConcentrationError(
            "canonical OMS open_quantity is outside exact decimal authority"
        ) from error
    if amount < 0:
        raise CorrelationConcentrationError(
            "canonical OMS open_quantity cannot be negative"
        )
    return amount


def _unvalued_open_orders(
    oms: DurableOrderBookProjection,
) -> tuple[tuple[str, str, str, Decimal], ...]:
    """Return exact open OMS rows that lack a canonical monetary valuation cut."""

    if type(oms) is not _OMS_TYPE:
        raise TypeError("oms must be exact DurableOrderBookProjection")
    _require_bound_executable(
        _REQUIRE_OMS_AUTHORITY,
        _REQUIRE_OMS_AUTHORITY_CODE,
        name="canonical OMS authority verifier",
    )
    _require_bound_executable(
        _OMS_SNAPSHOTS_GETTER,
        _OMS_SNAPSHOTS_GETTER_CODE,
        name="canonical OMS snapshots getter",
    )
    _REQUIRE_OMS_AUTHORITY(oms)
    snapshots = _OMS_SNAPSHOTS_GETTER(oms)
    if type(snapshots) is not tuple:
        raise CorrelationConcentrationError(
            "canonical OMS snapshots must be an exact tuple"
        )

    blockers: list[tuple[str, str, str, Decimal]] = []
    for snapshot in snapshots:
        if type(snapshot) is not _SNAPSHOT_TYPE:
            raise CorrelationConcentrationError(
                "canonical OMS snapshots must contain exact OrderSnapshot values"
            )
        order_id = _exact_text(
            _GET(snapshot, "client_order_id"),
            name="client_order_id",
        )
        instrument = _exact_text(
            _GET(snapshot, "instrument"),
            name="instrument",
        )
        state = _exact_text(_GET(snapshot, "state"), name="state")
        open_quantity = _exact_open_quantity(_GET(snapshot, "open_quantity"))
        if open_quantity != 0:
            blockers.append((order_id, instrument, state, open_quantity))

    return tuple(sorted(blockers, key=lambda item: (item[1], item[0])))


def require_correlation_safe_proposal_with_oms(
    result,
    evidence,
    resolved_evidence,
    policy,
    *,
    oms: DurableOrderBookProjection,
):
    """Require no hidden open-order exposure before the existing correlation gate.

    A non-zero OMS open quantity is not converted into money here because the
    canonical OMS snapshot has no authority-bearing order-price/base-currency
    valuation cut.  Treating quantity as notional, using last fill price, or
    asking the caller for an estimate would manufacture financial authority.
    """

    _require_bound_executable(
        _BASE_CORRELATION_GUARD,
        _BASE_CORRELATION_GUARD_CODE,
        name="base correlation guard",
    )
    blockers = _unvalued_open_orders(oms)
    if blockers:
        detail = ", ".join(
            f"{instrument}/{order_id}:{state}:open={open_quantity}"
            for order_id, instrument, state, open_quantity in blockers
        )
        raise CorrelationConcentrationError(
            "correlation concentration inconclusive: canonical OMS has unvalued "
            "open exposure; an authenticated base-currency working/UNKNOWN "
            f"valuation cut is required before admission ({detail})"
        )
    return _BASE_CORRELATION_GUARD(
        result,
        evidence,
        resolved_evidence,
        policy,
    )


__all__ = ["require_correlation_safe_proposal_with_oms"]
