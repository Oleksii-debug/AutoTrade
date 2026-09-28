"""Independent exact economic reference calculations for AutoTrade.

The functions in this module are test oracles for accounting invariants. They
do not estimate strategy edge and they do not place orders.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, localcontext
import json
from pathlib import Path
from typing import Any


def _decimal(value: Decimal | str | int, *, name: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError(f"{name} must use Decimal, string or integer input")
    try:
        result = value if isinstance(value, Decimal) else Decimal(value)
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

    position = bought - sold
    cash = start - bought * buy_px - fee_buy + sold * sell_px - fee_sell
    realized = sold * (sell_px - buy_px)
    unrealized = position * (mark - buy_px)
    fees = fee_buy + fee_sell
    equity = cash + position * mark
    return CashRoundTripResult(
        cash=cash,
        position=position,
        gross_realized_pnl=realized,
        gross_unrealized_pnl=unrealized,
        fees=fees,
        equity=equity,
        net_pnl=equity - start,
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
    return qty * mult * (mark - entry)


def inverse_futures_pnl(
    contracts: Decimal | str | int,
    contract_value: Decimal | str | int,
    entry_price: Decimal | str | int,
    exit_price: Decimal | str | int,
) -> Decimal:
    """Return settlement-currency P&L using high-precision decimal arithmetic."""

    qty = _decimal(contracts, name="contracts")
    value = _positive(contract_value, name="contract_value")
    entry = _positive(entry_price, name="entry_price")
    exit_value = _positive(exit_price, name="exit_price")
    with localcontext() as context:
        context.prec = 50
        return +(qty * value * ((Decimal(1) / entry) - (Decimal(1) / exit_value)))


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
    signed = Decimal("-1") if normalized_side == "LONG" else Decimal("1")
    return signed * value * rate


@dataclass(frozen=True)
class SplitResult:
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
    qty = _decimal(quantity, name="quantity")
    basis = _non_negative(unit_basis, name="unit_basis")
    num = _positive(numerator, name="numerator")
    den = _positive(denominator, name="denominator")
    total_basis = abs(qty) * basis
    new_quantity = qty * num / den
    new_unit_basis = (
        total_basis / abs(new_quantity)
        if new_quantity
        else Decimal("0")
    )
    return SplitResult(
        quantity=new_quantity,
        unit_basis=new_unit_basis,
        total_basis=total_basis,
    )


def investment_pnl_excluding_external_flows(
    previous_equity: Decimal | str | int,
    current_equity: Decimal | str | int,
    external_net_flow: Decimal | str | int,
) -> Decimal:
    previous = _decimal(previous_equity, name="previous_equity")
    current = _decimal(current_equity, name="current_equity")
    flow = _decimal(external_net_flow, name="external_net_flow")
    return current - previous - flow


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
    price_difference = corrected - original
    return -qty * price_difference if normalized_side == "BUY" else qty * price_difference


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


def _report_value(value: Decimal) -> Decimal:
    return value.quantize(REPORT_QUANTUM)


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

    total_fees = sum(
        (_non_negative(row.get("fee"), name="posting fee") for row in postings),
        Decimal("0"),
    )
    turnover = sum(
        (
            abs(
                _decimal(fill.get("quantity"), name="fill quantity")
                * _decimal(fill.get("price"), name="fill price")
            )
            for fill in fills.values()
        ),
        Decimal("0"),
    )
    net_pnl = final_equity - initial_equity
    gross_pnl = net_pnl + total_fees
    net_return = net_pnl / initial_equity

    peak = initial_equity
    maximum_drawdown = Decimal("0")
    reconciled = True
    for row in evidence:
        equity = _decimal(row.get("equity"), name="evidence equity")
        if equity > peak:
            peak = equity
        if peak > 0:
            drawdown = (peak - equity) / peak
            maximum_drawdown = max(maximum_drawdown, drawdown)
        reconciled = reconciled and row.get("reconciled") is True

    effective_fee_rate = total_fees / turnover if turnover > 0 else Decimal("0")
    ending_position = sum(
        (_decimal(row.get("position_delta"), name="position delta") for row in postings),
        Decimal("0"),
    )

    return EconomicReport(
        initial_equity=_report_value(initial_equity),
        final_equity=_report_value(final_equity),
        net_pnl=_report_value(net_pnl),
        gross_pnl_before_fees=_report_value(gross_pnl),
        total_fees=_report_value(total_fees),
        turnover=_report_value(turnover),
        net_return=_report_value(net_return),
        max_drawdown=_report_value(maximum_drawdown),
        effective_fee_rate=_report_value(effective_fee_rate),
        break_even_additional_cost=_report_value(max(net_pnl, Decimal("0"))),
        trade_count=len(fills),
        ending_position=_report_value(ending_position),
        evidence_count=len(evidence),
        reconciled=reconciled,
        economic_edge_claim="UNPROVEN_SIMULATION_ONLY",
        fill_model="EXACT_INPUT_PRICE_WITH_EXPLICIT_FEE_NO_SLIPPAGE",
    )
