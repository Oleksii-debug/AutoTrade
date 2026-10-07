"""Fail-closed OMS composition for the WP-32 correlation guard.

The canonical durable OMS owns lifecycle/open-quantity truth, but its order
snapshot deliberately does not claim an authority-bearing base-currency order
price/notional. Correlation admission must therefore not ignore open orders or
invent monetary exposure from quantity alone. This composition blocks while
any exact durable OMS snapshot has non-zero open quantity and delegates to the
existing correlation guard only when that ambiguity is absent.

This module is a safety prerequisite only. It does not add another allocator,
OMS, valuation engine, correlation model, financial admission issuer, or
trading authority. Full Product Specification section 25 still requires an
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


def _require_bound_executable(
    function,
    expected_code,
    *,
    name: str,
    _get=_GET,
    _error_type=CorrelationConcentrationError,
) -> None:
    if _get(function, "__code__") is not expected_code:
        raise _error_type(f"{name} executable changed after binding")


def _exact_text(
    value: object,
    *,
    name: str,
    _type=type,
    _str_type=str,
    _strip=str.strip,
    _error_type=CorrelationConcentrationError,
) -> str:
    if _type(value) is not _str_type:
        raise _error_type(f"canonical OMS {name} must be exact non-empty text")
    normalized = _strip(value)
    if not normalized or normalized != value:
        raise _error_type(f"canonical OMS {name} must be exact non-empty text")
    return normalized


def _exact_open_quantity(
    value: object,
    *,
    _type=type,
    _decimal_type=Decimal,
    _str_type=str,
    _int_type=int,
    _bool_type=bool,
    _parse=parse_bounded_exact_decimal,
    _parse_code=getattr(parse_bounded_exact_decimal, "__code__", None),
    _get=_GET,
    _parse_errors=(ExactDecimalError, TypeError, ValueError),
    _error_type=CorrelationConcentrationError,
) -> Decimal:
    if _get(_parse, "__code__") is not _parse_code:
        raise _error_type("canonical OMS decimal parser executable changed after binding")
    if _type(value) not in {_decimal_type, _str_type, _int_type} or _type(value) is _bool_type:
        raise _error_type(
            "canonical OMS open_quantity must use exact Decimal, string or integer"
        )
    try:
        amount = _parse(value)
    except _parse_errors as error:
        raise _error_type(
            "canonical OMS open_quantity is outside exact decimal authority"
        ) from error
    if amount < 0:
        raise _error_type("canonical OMS open_quantity cannot be negative")
    return amount


def _unvalued_open_orders(
    oms: DurableOrderBookProjection,
    _oms_type=_OMS_TYPE,
    _snapshot_type=_SNAPSHOT_TYPE,
    _require_oms=_REQUIRE_OMS_AUTHORITY,
    _require_oms_code=_REQUIRE_OMS_AUTHORITY_CODE,
    _snapshots_getter=_OMS_SNAPSHOTS_GETTER,
    _snapshots_getter_code=_OMS_SNAPSHOTS_GETTER_CODE,
    _bound_check=_require_bound_executable,
    _bound_check_code=_GET(_require_bound_executable, "__code__"),
    _text=_exact_text,
    _text_code=_GET(_exact_text, "__code__"),
    _open_quantity=_exact_open_quantity,
    _open_quantity_code=_GET(_exact_open_quantity, "__code__"),
    _get=_GET,
    _type=type,
    _tuple=tuple,
    _sorted=sorted,
    _error_type=CorrelationConcentrationError,
) -> tuple[tuple[str, str, str, Decimal], ...]:
    """Return exact open OMS rows that lack a canonical monetary valuation cut."""

    for function, expected_code, name in (
        (_bound_check, _bound_check_code, "OMS executable verifier"),
        (_text, _text_code, "OMS text normalizer"),
        (_open_quantity, _open_quantity_code, "OMS quantity normalizer"),
    ):
        if _get(function, "__code__") is not expected_code:
            raise _error_type(f"{name} executable changed after binding")

    if _type(oms) is not _oms_type:
        raise TypeError("oms must be exact DurableOrderBookProjection")
    _bound_check(
        _require_oms,
        _require_oms_code,
        name="canonical OMS authority verifier",
    )
    _bound_check(
        _snapshots_getter,
        _snapshots_getter_code,
        name="canonical OMS snapshots getter",
    )
    _require_oms(oms)
    snapshots = _snapshots_getter(oms)
    if _type(snapshots) is not _tuple:
        raise _error_type("canonical OMS snapshots must be an exact tuple")

    blockers = []
    for snapshot in snapshots:
        if _type(snapshot) is not _snapshot_type:
            raise _error_type(
                "canonical OMS snapshots must contain exact OrderSnapshot values"
            )
        order_id = _text(
            _get(snapshot, "client_order_id"),
            name="client_order_id",
        )
        instrument = _text(
            _get(snapshot, "instrument"),
            name="instrument",
        )
        state = _text(_get(snapshot, "state"), name="state")
        open_quantity = _open_quantity(_get(snapshot, "open_quantity"))
        if open_quantity != 0:
            blockers.append((order_id, instrument, state, open_quantity))

    return _tuple(_sorted(blockers, key=lambda item: (item[1], item[0])))


def require_correlation_safe_proposal_with_oms(
    result,
    evidence,
    resolved_evidence,
    policy,
    *,
    oms: DurableOrderBookProjection,
    _bound_check=_require_bound_executable,
    _bound_check_code=_GET(_require_bound_executable, "__code__"),
    _open_orders=_unvalued_open_orders,
    _open_orders_code=_GET(_unvalued_open_orders, "__code__"),
    _base_guard=_BASE_CORRELATION_GUARD,
    _base_guard_code=_BASE_CORRELATION_GUARD_CODE,
    _get=_GET,
    _error_type=CorrelationConcentrationError,
):
    """Require no hidden open-order exposure before the correlation gate.

    A non-zero OMS open quantity is not converted into money here because the
    canonical OMS snapshot has no authority-bearing order-price/base-currency
    valuation cut. Treating quantity as notional, using last fill price, or
    asking the caller for an estimate would manufacture financial authority.
    """

    for function, expected_code, name in (
        (_bound_check, _bound_check_code, "OMS executable verifier"),
        (_open_orders, _open_orders_code, "canonical OMS open-exposure helper"),
        (_base_guard, _base_guard_code, "base correlation guard"),
    ):
        if _get(function, "__code__") is not expected_code:
            raise _error_type(f"{name} executable changed after binding")

    blockers = _open_orders(oms)
    if blockers:
        detail = ", ".join(
            f"{instrument}/{order_id}:{state}:open={open_quantity}"
            for order_id, instrument, state, open_quantity in blockers
        )
        raise _error_type(
            "correlation concentration inconclusive: canonical OMS has unvalued "
            "open exposure; an authenticated base-currency working/UNKNOWN "
            f"valuation cut is required before admission ({detail})"
        )
    return _base_guard(
        result,
        evidence,
        resolved_evidence,
        policy,
    )


__all__ = ["require_correlation_safe_proposal_with_oms"]
