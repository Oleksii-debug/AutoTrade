"""Independent exact economic reference calculations for AutoTrade.

The functions in this module are test oracles for accounting invariants. They
do not estimate strategy edge and they do not place orders. Finite decimal
arithmetic is exact and non-terminating reference results are rounded only at
an explicit, versioned reporting boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from fractions import Fraction
import json
from pathlib import Path
from typing import Any

from .exact_decimal import (
    as_fraction,
    bounded_fraction,
    exact_sum,
    round_fraction_to_quantum,
    terminating_decimal,
)


def _decimal(value: Decimal | str | int, *, name: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError(f"{name} must use Decimal, string or integer input")
    if isinstance(value, Decimal) and type(value) is not Decimal:
        raise TypeError(f"{name} must use Decimal, string or integer input")
    try:
        result = value if type(value) is Decimal else Decimal(value)
    except (InvalidOperation, ValueError, TypeError) as error:
        raise ValueError(f"{name} must be a finite decimal") from error
    if not result.is_finite():
        raise ValueError(f"{name} must be a finite decimal")
    return result


def _non_negative(value: Decimal | str | int, *, name: str) -> Decimal:
    result = _decimal(value, name=name)
    if result < 0:
        raise ValueError(f"{name} must be non-negative")
    return result


def _positive(value: Decimal | str | int, *, name: str) -> Decimal:
    result = _decimal(value, name=name)
    if result <= 0:
        raise ValueError(f"{name} must be positive")
    return result


def _finite(value: Fraction) -> Decimal:
    return terminating_decimal(value)


def _bounded(value: Fraction) -> Fraction:
    return bounded_fraction(value)


def _fadd(left: Fraction, right: Fraction) -> Fraction:
    return _bounded(left + right)


def _fsub(left: Fraction, right: Fraction) -> Fraction:
    return _bounded(left - right)


def _fmul(left: Fraction, right: Fraction) -> Fraction:
    return _bounded(left * right)


def _fdiv(left: Fraction, right: Fraction) -> Fraction:
    if right == 0:
        raise ZeroDivisionError("exact rational divisor must be non-zero")
    return _bounded(left / right)


@dataclass(frozen=True)
class CashRoundTripResult:
    cash: Decimal
    position: Decimal
    gross_realized_pnl: Decimal
    gross_unrealized_pnl: Decimal
    fees: Decimal
    equity: Decimal
    net_pnl: Decimal


def cash_round_trip(
    *,
    start_cash: Decimal | str | int,
    buy_quantity: Decimal | str | int,
    buy_price: Decimal | str | int,
    buy_fee: Decimal | str | int,
    sell_quantity: Decimal | str | int,
    sell_price: Decimal | str | int,
    sell_fee: Decimal | str | int,
    mark_price: Decimal | str | int,
) -> CashRoundTripResult:
    """Calculate the document-04 cash-equity reference convention.

    Fees are expensed separately from inventory cost basis.
    """

    start = _non_negative(start_cash, name="start_cash")
    bought = _positive(buy_quantity, name="buy_quantity")
    buy_px = _positive(buy_price, name="buy_price")
    sold = _non_negative(sell_quantity, name="sell_quantity")
    sell_px = _positive(sell_price, name="sell_price")
    mark = _positive(mark_price, name="mark_price")
    fee_buy = _non_negative(buy_fee, name="buy_fee")
    fee_sell = _non_negative(sell_fee, name="sell_fee")
    if sold > bought:
        raise ValueError("sell_quantity cannot exceed bought inventory in this oracle")

    start_f = as_fraction(start)
    bought_f = as_fraction(bought)
    sold_f = as_fraction(sold)
    buy_px_f = as_fraction(buy_px)
    sell_px_f = as_fraction(sell_px)
    mark_f = as_fraction(mark)
    fee_buy_f = as_fraction(fee_buy)
    fee_sell_f = as_fraction(fee_sell)

    position_f = _fsub(bought_f, sold_f)
    buy_notional_f = _fmul(bought_f, buy_px_f)
    sell_notional_f = _fmul(sold_f, sell_px_f)
    cash_f = _fsub(
        _fadd(
            _fsub(_fsub(start_f, buy_notional_f), fee_buy_f),
            sell_notional_f,
        ),
        fee_sell_f,
    )
    realized_f = _fmul(sold_f, _fsub(sell_px_f, buy_px_f))
    unrealized_f = _fmul(position_f, _fsub(mark_f, buy_px_f))
    fees_f = _fadd(fee_buy_f, fee_sell_f)
    equity_f = _fadd(cash_f, _fmul(position_f, mark_f))
    return CashRoundTripResult(
        cash=_finite(cash_f),
        position=_finite(position_f),
        gross_realized_pnl=_finite(realized_f),
        gross_unrealized_pnl=_finite(unrealized_f),
        fees=_finite(fees_f),
        equity=_finite(equity_f),
        net_pnl=_finite(_fsub(equity_f, start_f)),
    )


def linear_futures_mark_pnl(
    contracts: Decimal | str | int,
    multiplier: Decimal | str | int,
    entry_price: Decimal | str | int,
    mark_price: Decimal | str | int,
) -> Decimal:
    qty = _decimal(contracts, name="contracts")
    mult = _positive(multiplier, name="multiplier")
    entry = _positive(entry_price, name="entry_price")
    mark = _positive(mark_price, name="mark_price")
    price_delta = _fsub(as_fraction(mark), as_fraction(entry))
    return _finite(
        _fmul(
            _fmul(as_fraction(qty), as_fraction(mult)),
            price_delta,
        )
    )


def inverse_futures_pnl_exact(
    contracts: Decimal | str | int,
    contract_value: Decimal | str | int,
    entry_price: Decimal | str | int,
    exit_price: Decimal | str | int,
) -> Fraction:
    """Return the exact rational inverse-contract P&L in settlement units."""

    qty = _decimal(contracts, name="contracts")
    value = _positive(contract_value, name="contract_value")
    entry = _positive(entry_price, name="entry_price")
    exit_value = _positive(exit_price, name="exit_price")
    notional = bounded_fraction(as_fraction(qty) * as_fraction(value))
    entry_inverse = bounded_fraction(Fraction(1, 1) / as_fraction(entry))
    exit_inverse = bounded_fraction(Fraction(1, 1) / as_fraction(exit_value))
    reciprocal_delta = bounded_fraction(entry_inverse - exit_inverse)
    return bounded_fraction(notional * reciprocal_delta)


INVERSE_REFERENCE_QUANTUM = Decimal("0.00000000000000000000000000000000000000000000000001")


def inverse_futures_pnl(
    contracts: Decimal | str | int,
    contract_value: Decimal | str | int,
    entry_price: Decimal | str | int,
    exit_price: Decimal | str | int,
) -> Decimal:
    """Compatibility Decimal view of the exact inverse reference oracle.

    The exact authority is :func:`inverse_futures_pnl_exact`. This Decimal view
    uses an explicit 1e-50 HALF_EVEN reporting quantum and never ambient context.
    """

    return round_fraction_to_quantum(
        inverse_futures_pnl_exact(
            contracts,
            contract_value,
            entry_price,
            exit_price,
        ),
        INVERSE_REFERENCE_QUANTUM,
        mode="HALF_EVEN",
    )


def linear_funding_cashflow(
    notional: Decimal | str | int,
    funding_rate: Decimal | str | int,
    *,
    side: str = "LONG",
) -> Decimal:
    """Calculate the fixture convention where positive funding is paid by longs."""

    value = _non_negative(notional, name="notional")
    rate = _decimal(funding_rate, name="funding_rate")
    normalized_side = side.upper()
    if normalized_side not in {"LONG", "SHORT"}:
        raise ValueError("side must be LONG or SHORT")
    sign = -1 if normalized_side == "LONG" else 1
    return _finite(
        _fmul(
            _fmul(Fraction(sign, 1), as_fraction(value)),
            as_fraction(rate),
        )
    )


@dataclass(frozen=True)
class SplitResult:
    """Exact rational corporate-action split reference result.

    Fractional entitlement policy belongs to the product/accounting boundary,
    not this independent oracle. Quantity and unit basis therefore remain exact
    Fractions even when their base-10 expansions are non-terminating.
    """

    quantity: Fraction
    unit_basis: Fraction
    total_basis: Fraction


@dataclass(frozen=True)
class DecimalSplitResult:
    """Optional Decimal projection for splits whose exact values terminate."""

    quantity: Decimal
    unit_basis: Decimal
    total_basis: Decimal


def apply_split(
    quantity: Decimal | str | int,
    unit_basis: Decimal | str | int,
    *,
    numerator: Decimal | str | int,
    denominator: Decimal | str | int = 1,
) -> SplitResult:
    """Return the exact split reference without inventing entitlement policy."""

    qty = _decimal(quantity, name="quantity")
    basis = _non_negative(unit_basis, name="unit_basis")
    num = _positive(numerator, name="numerator")
    den = _positive(denominator, name="denominator")
    qty_f = as_fraction(qty)
    basis_f = as_fraction(basis)
    total_basis_f = _fmul(abs(qty_f), basis_f)
    scaled_quantity_f = _fmul(qty_f, as_fraction(num))
    new_quantity_f = _fdiv(scaled_quantity_f, as_fraction(den))
    new_unit_basis_f = (
        _fdiv(total_basis_f, abs(new_quantity_f))
        if new_quantity_f
        else Fraction(0, 1)
    )
    return SplitResult(
        quantity=new_quantity_f,
        unit_basis=new_unit_basis_f,
        total_basis=total_basis_f,
    )


def project_split_decimal(result: SplitResult) -> DecimalSplitResult:
    """Project an exact split to Decimal only when all values terminate.

    Non-terminating fractional entitlements intentionally fail closed here.
    Production handling must cross a versioned quantity-quantum,
    fractional-share, or cash-in-lieu policy boundary before projection.
    """

    if not isinstance(result, SplitResult):
        raise TypeError("result must be SplitResult")
    return DecimalSplitResult(
        quantity=_finite(result.quantity),
        unit_basis=_finite(result.unit_basis),
        total_basis=_finite(result.total_basis),
    )


def investment_pnl_excluding_external_flows(
    previous_equity: Decimal | str | int,
    current_equity: Decimal | str | int,
    external_net_flow: Decimal | str | int,
) -> Decimal:
    previous = _decimal(previous_equity, name="previous_equity")
    current = _decimal(current_equity, name="current_equity")
    flow = _decimal(external_net_flow, name="external_net_flow")
    return _finite(
        _fsub(
            _fsub(as_fraction(current), as_fraction(previous)),
            as_fraction(flow),
        )
    )


def corrected_fill_cash_difference(
    quantity: Decimal | str | int,
    original_price: Decimal | str | int,
    corrected_price: Decimal | str | int,
    *,
    side: str = "BUY",
) -> Decimal:
    qty = _positive(quantity, name="quantity")
    original = _positive(original_price, name="original_price")
    corrected = _positive(corrected_price, name="corrected_price")
    normalized_side = side.upper()
    if normalized_side not in {"BUY", "SELL"}:
        raise ValueError("side must be BUY or SELL")
    difference = _fsub(as_fraction(corrected), as_fraction(original))
    unsigned = _fmul(as_fraction(qty), difference)
    signed = -unsigned if normalized_side == "BUY" else unsigned
    return _finite(_bounded(signed))


REPORT_QUANTUM = Decimal("0.00000001")


@dataclass(frozen=True)
class EconomicReport:
    """Evidence-bound economics for one simulated state directory."""

    initial_equity: Decimal
    final_equity: Decimal
    net_pnl: Decimal
    gross_pnl_before_fees: Decimal
    total_fees: Decimal
    turnover: Decimal
    net_return: Decimal
    max_drawdown: Decimal
    effective_fee_rate: Decimal
    break_even_additional_cost: Decimal
    trade_count: int
    ending_position: Decimal
    evidence_count: int
    reconciled: bool
    economic_edge_claim: str
    fill_model: str

    def as_jsonable(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in self.__dict__.items():
            result[key] = str(value) if isinstance(value, Decimal) else value
        return result


def _report_value(value: Decimal | Fraction) -> Decimal:
    rational = value if isinstance(value, Fraction) else as_fraction(value)
    return round_fraction_to_quantum(rational, REPORT_QUANTUM, mode="HALF_EVEN")


def build_economic_report(state_dir: str | Path) -> EconomicReport:
    """Summarize realized simulation economics without claiming strategy edge."""

    root = Path(state_dir)
    checkpoint_path = root / "checkpoint.json"
    evidence_path = root / "learning-evidence.jsonl"
    if not checkpoint_path.is_file() or not evidence_path.is_file():
        raise ValueError("A completed simulated state with evidence is required")
    try:
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        evidence = [
            json.loads(line)
            for line in evidence_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("Economic state is unreadable or corrupt") from error
    if not evidence:
        raise ValueError("At least one evidence record is required")

    initial_equity = _decimal(checkpoint.get("initial_cash"), name="initial_cash")
    final_equity = _decimal(evidence[-1].get("equity"), name="final_equity")
    if initial_equity <= 0:
        raise ValueError("Initial equity must be positive")

    postings = checkpoint.get("postings")
    fills = checkpoint.get("fills")
    if not isinstance(postings, list) or not isinstance(fills, dict):
        raise ValueError("Checkpoint ledger structure is corrupt")

    total_fees = exact_sum(
        _non_negative(row.get("fee"), name="posting fee") for row in postings
    )
    turnover = exact_sum(
        _finite(
            _fmul(
                abs(as_fraction(_decimal(fill.get("quantity"), name="fill quantity"))),
                as_fraction(_decimal(fill.get("price"), name="fill price")),
            )
        )
        for fill in fills.values()
    )
    net_pnl_f = _fsub(as_fraction(final_equity), as_fraction(initial_equity))
    net_pnl = _finite(net_pnl_f)
    gross_pnl = _finite(_fadd(net_pnl_f, as_fraction(total_fees)))
    net_return_f = _fdiv(net_pnl_f, as_fraction(initial_equity))

    peak = initial_equity
    maximum_drawdown_f = Fraction(0, 1)
    reconciled = True
    for row in evidence:
        equity = _decimal(row.get("equity"), name="evidence equity")
        if equity > peak:
            peak = equity
        if peak > 0:
            drawdown_f = _fdiv(
                _fsub(as_fraction(peak), as_fraction(equity)),
                as_fraction(peak),
            )
            maximum_drawdown_f = max(maximum_drawdown_f, drawdown_f)
        reconciled = reconciled and row.get("reconciled") is True

    effective_fee_rate_f = (
        _fdiv(as_fraction(total_fees), as_fraction(turnover))
        if turnover > 0
        else Fraction(0, 1)
    )
    ending_position = exact_sum(
        _decimal(row.get("position_delta"), name="position delta") for row in postings
    )

    return EconomicReport(
        initial_equity=_report_value(initial_equity),
        final_equity=_report_value(final_equity),
        net_pnl=_report_value(net_pnl),
        gross_pnl_before_fees=_report_value(gross_pnl),
        total_fees=_report_value(total_fees),
        turnover=_report_value(turnover),
        net_return=_report_value(net_return_f),
        max_drawdown=_report_value(maximum_drawdown_f),
        effective_fee_rate=_report_value(effective_fee_rate_f),
        break_even_additional_cost=_report_value(max(net_pnl, Decimal("0"))),
        trade_count=len(fills),
        ending_position=_report_value(ending_position),
        evidence_count=len(evidence),
        reconciled=reconciled,
        economic_edge_claim="UNPROVEN_SIMULATION_ONLY",
        fill_model="EXACT_INPUT_PRICE_WITH_EXPLICIT_FEE_NO_SLIPPAGE",
    )
