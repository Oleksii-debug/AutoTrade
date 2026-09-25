"""Exact multi-asset double-entry accounting foundation for the AutoTrade MVP.

This module is deliberately provider-neutral.  It books immutable economic
transactions and projections, but does not authorize or send orders.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Iterable

from .persistence import payload_digest


class AccountingConflict(ValueError):
    """Raised when an immutable transaction identity is reused inconsistently."""


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


def _name(value: str, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is required")
    return value.strip()


def _canonical_decimal(value: Decimal) -> str:
    amount = _decimal(value, name="signed_amount")
    if amount == 0:
        return "0"
    fixed = format(amount, "f")
    if "." in fixed:
        fixed = fixed.rstrip("0").rstrip(".")
    return fixed


@dataclass(frozen=True)
class Posting:
    ledger_account: str
    asset_or_currency: str
    signed_amount: Decimal


@dataclass(frozen=True)
class JournalTransaction:
    transaction_id: str
    cause_event_id: str
    postings: tuple[Posting, ...]
    reverses_transaction_id: str | None = None


def _normalized_transaction(transaction: JournalTransaction) -> JournalTransaction:
    if not isinstance(transaction, JournalTransaction):
        raise TypeError("transaction must be a JournalTransaction")
    return JournalTransaction(
        transaction_id=_name(transaction.transaction_id, field="transaction_id"),
        cause_event_id=_name(transaction.cause_event_id, field="cause_event_id"),
        postings=tuple(
            Posting(
                ledger_account=_name(item.ledger_account, field="ledger_account"),
                asset_or_currency=_name(
                    item.asset_or_currency,
                    field="asset_or_currency",
                ),
                signed_amount=_decimal(item.signed_amount, name="signed_amount"),
            )
            for item in transaction.postings
        ),
        reverses_transaction_id=(
            _name(transaction.reverses_transaction_id, field="reverses_transaction_id")
            if transaction.reverses_transaction_id is not None
            else None
        ),
    )


def posting(ledger_account: str, asset_or_currency: str, signed_amount: Decimal | str | int) -> Posting:
    return Posting(
        ledger_account=_name(ledger_account, field="ledger_account"),
        asset_or_currency=_name(asset_or_currency, field="asset_or_currency"),
        signed_amount=_decimal(signed_amount, name="signed_amount"),
    )


def canonical_transaction(transaction: JournalTransaction) -> dict[str, object]:
    normalized = _normalized_transaction(transaction)
    validate_transaction(normalized)
    return {
        "schema_version": "1.0.0",
        "transaction_id": normalized.transaction_id,
        "cause_event_id": normalized.cause_event_id,
        "reverses_transaction_id": normalized.reverses_transaction_id,
        "postings": [
            {
                "ledger_account": item.ledger_account,
                "asset_or_currency": item.asset_or_currency,
                "signed_amount": _canonical_decimal(item.signed_amount),
            }
            for item in normalized.postings
        ],
    }


def transaction_digest(transaction: JournalTransaction) -> str:
    return payload_digest(canonical_transaction(transaction))


def validate_transaction(transaction: JournalTransaction) -> None:
    _name(transaction.transaction_id, field="transaction_id")
    _name(transaction.cause_event_id, field="cause_event_id")
    if len(transaction.postings) < 2:
        raise ValueError("A journal transaction requires at least two postings")
    totals: dict[str, Decimal] = {}
    for item in transaction.postings:
        _name(item.ledger_account, field="ledger_account")
        asset = _name(item.asset_or_currency, field="asset_or_currency")
        amount = _decimal(item.signed_amount, name="signed_amount")
        totals[asset] = totals.get(asset, Decimal("0")) + amount
    unbalanced = {asset: amount for asset, amount in totals.items() if amount != 0}
    if unbalanced:
        raise ValueError(f"Transaction is not balanced by asset/currency: {unbalanced}")


class EconomicBook:
    """Append-only immutable economic journal with exact projections."""

    def __init__(self, transactions: Iterable[JournalTransaction] = ()):
        self._transactions: list[JournalTransaction] = []
        self._by_id: dict[str, JournalTransaction] = {}
        self._by_cause_event_id: dict[str, JournalTransaction] = {}
        self._reversed_transaction_ids: set[str] = set()
        for transaction in transactions:
            self.append(transaction)

    @property
    def transactions(self) -> tuple[JournalTransaction, ...]:
        return tuple(self._transactions)

    def append(self, transaction: JournalTransaction) -> bool:
        normalized = _normalized_transaction(transaction)
        validate_transaction(normalized)
        transaction_id = normalized.transaction_id
        cause_event_id = normalized.cause_event_id
        existing = self._by_id.get(transaction_id)
        if existing is not None:
            if existing != normalized:
                raise AccountingConflict(
                    "transaction_id was already committed with different economic content"
                )
            return False

        cause_existing = self._by_cause_event_id.get(cause_event_id)
        if cause_existing is not None:
            raise AccountingConflict(
                "cause_event_id was already booked by a different transaction"
            )

        if normalized.reverses_transaction_id is not None:
            original_id = normalized.reverses_transaction_id
            original = self._by_id.get(original_id)
            if original is None:
                raise AccountingConflict("Cannot reverse an unknown transaction")
            if original_id in self._reversed_transaction_ids:
                raise AccountingConflict("Transaction has already been reversed")
            expected = tuple(
                Posting(item.ledger_account, item.asset_or_currency, -item.signed_amount)
                for item in original.postings
            )
            if normalized.postings != expected:
                raise AccountingConflict("A reversal must exactly negate the original postings")
        self._by_id[transaction_id] = normalized
        self._by_cause_event_id[cause_event_id] = normalized
        self._transactions.append(normalized)
        if normalized.reverses_transaction_id is not None:
            self._reversed_transaction_ids.add(normalized.reverses_transaction_id)
        return True

    def balance(self, ledger_account: str, asset_or_currency: str) -> Decimal:
        account = _name(ledger_account, field="ledger_account")
        asset = _name(asset_or_currency, field="asset_or_currency")
        return sum(
            (
                item.signed_amount
                for transaction in self._transactions
                for item in transaction.postings
                if item.ledger_account == account and item.asset_or_currency == asset
            ),
            Decimal("0"),
        )

    def cash(self, currency: str) -> Decimal:
        value = _name(currency, field="currency")
        return self.balance(f"CASH:{value}", value)

    def position(self, instrument: str) -> Decimal:
        value = _name(instrument, field="instrument")
        return self.balance(f"POSITION:{value}", value)

    def fee_expense(self, currency: str) -> Decimal:
        value = _name(currency, field="currency")
        return self.balance(f"FEE_EXPENSE:{value}", value)

    def audit_digest(self) -> str:
        return payload_digest(
            {
                "schema_version": "1.0.0",
                "transactions": [
                    {
                        "transaction_id": _name(
                            transaction.transaction_id,
                            field="transaction_id",
                        ),
                        "digest": transaction_digest(transaction),
                    }
                    for transaction in self._transactions
                ],
            }
        )


class ScopedEconomicBook:
    """Account/environment-bound facade over the canonical EconomicBook."""

    _ENVIRONMENTS = frozenset({"REPLAY", "SIMULATION", "PAPER", "LIVE"})

    def __init__(
        self,
        *,
        environment: str,
        account_id: str,
        transactions: Iterable[JournalTransaction] = (),
    ):
        normalized_environment = _name(environment, field="environment").upper()
        if normalized_environment not in self._ENVIRONMENTS:
            raise ValueError("unsupported environment")
        self.environment = normalized_environment
        self.account_id = _name(account_id, field="account_id")
        self._book = EconomicBook(transactions)

    @property
    def transactions(self) -> tuple[JournalTransaction, ...]:
        return self._book.transactions

    def append(self, transaction: JournalTransaction) -> bool:
        return self._book.append(transaction)

    def balance(self, ledger_account: str, asset_or_currency: str) -> Decimal:
        return self._book.balance(ledger_account, asset_or_currency)

    def cash(self, currency: str) -> Decimal:
        return self._book.cash(currency)

    def position(self, instrument: str) -> Decimal:
        return self._book.position(instrument)

    def fee_expense(self, currency: str) -> Decimal:
        return self._book.fee_expense(currency)

    def audit_digest(self) -> str:
        return payload_digest(
            {
                "schema_version": "1.0.0",
                "environment": self.environment,
                "account_id": self.account_id,
                "economic_book_digest": self._book.audit_digest(),
            }
        )


def book_external_cash_flow(
    *,
    transaction_id: str,
    cause_event_id: str,
    currency: str,
    amount: Decimal | str | int,
) -> JournalTransaction:
    value = _decimal(amount, name="amount")
    if value == 0:
        raise ValueError("External cash flow must be non-zero")
    unit = _name(currency, field="currency")
    transaction = JournalTransaction(
        transaction_id=_name(transaction_id, field="transaction_id"),
        cause_event_id=_name(cause_event_id, field="cause_event_id"),
        postings=(
            posting(f"CASH:{unit}", unit, value),
            posting(f"EXTERNAL_EQUITY:{unit}", unit, -value),
        ),
    )
    validate_transaction(transaction)
    return transaction


def book_equity_fill(
    *,
    transaction_id: str,
    cause_event_id: str,
    instrument: str,
    settlement_currency: str,
    side: str,
    quantity: Decimal | str | int,
    price: Decimal | str | int,
    fee: Decimal | str | int = Decimal("0"),
    fee_currency: str | None = None,
) -> JournalTransaction:
    symbol = _name(instrument, field="instrument")
    settlement = _name(settlement_currency, field="settlement_currency")
    normalized_side = _name(side, field="side").upper()
    if normalized_side not in {"BUY", "SELL"}:
        raise ValueError("side must be BUY or SELL")
    qty = _decimal(quantity, name="quantity")
    unit_price = _decimal(price, name="price")
    fee_amount = _decimal(fee, name="fee")
    if qty <= 0 or unit_price <= 0:
        raise ValueError("quantity and price must be positive")
    signed_quantity = qty if normalized_side == "BUY" else -qty
    trade_cash = -(qty * unit_price) if normalized_side == "BUY" else qty * unit_price

    items = [
        posting(f"POSITION:{symbol}", symbol, signed_quantity),
        posting(f"CLEARING:{symbol}", symbol, -signed_quantity),
        posting(f"CASH:{settlement}", settlement, trade_cash),
        posting(f"CLEARING:{settlement}", settlement, -trade_cash),
    ]
    if fee_amount != 0:
        fee_unit = _name(fee_currency or settlement, field="fee_currency")
        items.extend(
            (
                posting(f"CASH:{fee_unit}", fee_unit, -fee_amount),
                posting(f"FEE_EXPENSE:{fee_unit}", fee_unit, fee_amount),
            )
        )
    transaction = JournalTransaction(
        transaction_id=_name(transaction_id, field="transaction_id"),
        cause_event_id=_name(cause_event_id, field="cause_event_id"),
        postings=tuple(items),
    )
    validate_transaction(transaction)
    return transaction


def book_fx_exchange(
    *,
    transaction_id: str,
    cause_event_id: str,
    sold_currency: str,
    sold_amount: Decimal | str | int,
    bought_currency: str,
    bought_amount: Decimal | str | int,
) -> JournalTransaction:
    sold = _name(sold_currency, field="sold_currency")
    bought = _name(bought_currency, field="bought_currency")
    if sold == bought:
        raise ValueError("FX exchange requires two different currencies")
    sold_value = _decimal(sold_amount, name="sold_amount")
    bought_value = _decimal(bought_amount, name="bought_amount")
    if sold_value <= 0 or bought_value <= 0:
        raise ValueError("FX amounts must be positive")
    transaction = JournalTransaction(
        transaction_id=_name(transaction_id, field="transaction_id"),
        cause_event_id=_name(cause_event_id, field="cause_event_id"),
        postings=(
            posting(f"CASH:{sold}", sold, -sold_value),
            posting(f"FX_CLEARING:{sold}", sold, sold_value),
            posting(f"CASH:{bought}", bought, bought_value),
            posting(f"FX_CLEARING:{bought}", bought, -bought_value),
        ),
    )
    validate_transaction(transaction)
    return transaction


def reverse_transaction(
    original: JournalTransaction,
    *,
    transaction_id: str,
    cause_event_id: str,
) -> JournalTransaction:
    validate_transaction(original)
    transaction = JournalTransaction(
        transaction_id=_name(transaction_id, field="transaction_id"),
        cause_event_id=_name(cause_event_id, field="cause_event_id"),
        postings=tuple(
            Posting(item.ledger_account, item.asset_or_currency, -item.signed_amount)
            for item in original.postings
        ),
        reverses_transaction_id=original.transaction_id,
    )
    validate_transaction(transaction)
    return transaction


@dataclass(frozen=True)
class EquityLot:
    """One open FIFO lot derived from canonical equity-fill postings."""

    quantity: Decimal
    unit_price: Decimal
    transaction_id: str


@dataclass(frozen=True)
class EquityPositionProjection:
    """Derived gross position economics; never an execution or accounting authority."""

    instrument: str
    settlement_currency: str
    quantity: Decimal
    open_cost_basis: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal | None
    mark_price: Decimal | None
    lots: tuple[EquityLot, ...]
    policy_version: str = "FIFO_GROSS_V1"


def _canonical_equity_fill_terms(
    transaction: JournalTransaction,
    *,
    instrument: str,
    settlement_currency: str,
) -> tuple[Decimal, Decimal] | None:
    """Extract quantity and unit price only from canonical book_equity_fill shape."""

    symbol = _name(instrument, field="instrument")
    settlement = _name(settlement_currency, field="settlement_currency")
    if symbol == settlement:
        raise ValueError("instrument and settlement_currency must be distinct")

    normalized = _normalized_transaction(transaction)
    position_postings = [
        item
        for item in normalized.postings
        if item.ledger_account == f"POSITION:{symbol}"
        and item.asset_or_currency == symbol
    ]
    if not position_postings:
        return None
    if len(position_postings) != 1:
        raise AccountingConflict(
            "Position projection requires exactly one canonical position posting"
        )

    quantity = position_postings[0].signed_amount
    if quantity == 0:
        raise AccountingConflict("Position projection cannot infer a zero-quantity fill")

    instrument_clearing = [
        item
        for item in normalized.postings
        if item.ledger_account == f"CLEARING:{symbol}"
        and item.asset_or_currency == symbol
    ]
    settlement_clearing = [
        item
        for item in normalized.postings
        if item.ledger_account == f"CLEARING:{settlement}"
        and item.asset_or_currency == settlement
    ]
    if (
        len(instrument_clearing) != 1
        or instrument_clearing[0].signed_amount != -quantity
        or len(settlement_clearing) != 1
    ):
        raise AccountingConflict(
            "Position projection requires canonical equity-fill clearing postings"
        )

    trade_cash = -settlement_clearing[0].signed_amount
    if trade_cash == 0 or (trade_cash > 0) == (quantity > 0):
        raise AccountingConflict(
            "Position projection requires cash direction opposite to quantity"
        )
    unit_price = abs(trade_cash / quantity)
    if unit_price <= 0 or not unit_price.is_finite():
        raise AccountingConflict("Position projection requires a finite positive price")

    # Clearing legs alone are not evidence that this transaction came from the
    # canonical equity-fill booking path. Reconstruct the only allowed posting
    # shape from the inferred economic terms and compare the complete ordered
    # postings, including trade cash and the optional single fee/rebate pair.
    # This prevents an arbitrary balanced SUSPENSE/ADJUSTMENT leg from being
    # interpreted as fill cash merely because it happened to carry matching
    # clearing amounts.
    fee_postings = [
        item
        for item in normalized.postings
        if item.ledger_account.startswith("FEE_EXPENSE:")
    ]
    if len(fee_postings) > 1:
        raise AccountingConflict(
            "Position projection requires canonical equity-fill fee postings"
        )
    fee_amount = Decimal("0")
    fee_currency: str | None = None
    if fee_postings:
        fee_posting = fee_postings[0]
        fee_currency = fee_posting.asset_or_currency
        if fee_posting.ledger_account != f"FEE_EXPENSE:{fee_currency}":
            raise AccountingConflict(
                "Position projection requires canonical equity-fill fee postings"
            )
        fee_amount = fee_posting.signed_amount

    expected = book_equity_fill(
        transaction_id=normalized.transaction_id,
        cause_event_id=normalized.cause_event_id,
        instrument=symbol,
        settlement_currency=settlement,
        side="BUY" if quantity > 0 else "SELL",
        quantity=abs(quantity),
        price=unit_price,
        fee=fee_amount,
        fee_currency=fee_currency,
    )
    if normalized.postings != expected.postings:
        raise AccountingConflict(
            "Position projection requires complete canonical equity-fill posting shape"
        )
    return quantity, unit_price


def project_equity_position(
    book: EconomicBook,
    *,
    instrument: str,
    settlement_currency: str,
    mark_price: Decimal | str | int | None = None,
) -> EquityPositionProjection:
    """Project FIFO gross basis/P&L from the canonical immutable journal.

    Fees remain separately expensed by the accounting book.  Reversed position
    histories deliberately fail closed because JournalTransaction currently does
    not carry the economic effective-time metadata required to restate FIFO lots
    safely after a retroactive correction.
    """

    if not isinstance(book, EconomicBook):
        raise TypeError("book must be an EconomicBook")
    symbol = _name(instrument, field="instrument")
    settlement = _name(settlement_currency, field="settlement_currency")
    mark = (
        None
        if mark_price is None
        else _decimal(mark_price, name="mark_price")
    )
    if mark is not None and mark <= 0:
        raise ValueError("mark_price must be positive")

    reversed_ids = {
        transaction.reverses_transaction_id
        for transaction in book.transactions
        if transaction.reverses_transaction_id is not None
    }
    reversal_ids = {
        transaction.transaction_id
        for transaction in book.transactions
        if transaction.reverses_transaction_id is not None
    }

    for transaction in book.transactions:
        if (
            transaction.transaction_id in reversed_ids
            or transaction.transaction_id in reversal_ids
        ):
            terms = _canonical_equity_fill_terms(
                transaction,
                instrument=symbol,
                settlement_currency=settlement,
            )
            if terms is not None:
                raise AccountingConflict(
                    "Position projection cannot restate reversed fill history "
                    "without economic effective-time metadata"
                )

    mutable_lots: list[list[Decimal | str]] = []
    realized = Decimal("0")

    for transaction in book.transactions:
        terms = _canonical_equity_fill_terms(
            transaction,
            instrument=symbol,
            settlement_currency=settlement,
        )
        if terms is None:
            continue
        quantity, unit_price = terms
        remaining = quantity

        while (
            remaining != 0
            and mutable_lots
            and (mutable_lots[0][0] > 0) != (remaining > 0)
        ):
            lot_quantity = mutable_lots[0][0]
            lot_price = mutable_lots[0][1]
            assert isinstance(lot_quantity, Decimal)
            assert isinstance(lot_price, Decimal)
            close_quantity = min(abs(remaining), abs(lot_quantity))

            if lot_quantity > 0:
                realized += close_quantity * (unit_price - lot_price)
                lot_quantity -= close_quantity
                remaining += close_quantity
            else:
                realized += close_quantity * (lot_price - unit_price)
                lot_quantity += close_quantity
                remaining -= close_quantity

            if lot_quantity == 0:
                mutable_lots.pop(0)
            else:
                mutable_lots[0][0] = lot_quantity

        if remaining != 0:
            mutable_lots.append(
                [remaining, unit_price, transaction.transaction_id]
            )

    lots = tuple(
        EquityLot(
            quantity=lot[0],
            unit_price=lot[1],
            transaction_id=lot[2],
        )
        for lot in mutable_lots
    )
    quantity = sum((lot.quantity for lot in lots), Decimal("0"))
    open_cost_basis = sum(
        (abs(lot.quantity) * lot.unit_price for lot in lots),
        Decimal("0"),
    )

    unrealized: Decimal | None
    if mark is None:
        unrealized = None
    else:
        unrealized = sum(
            (
                abs(lot.quantity)
                * (
                    (mark - lot.unit_price)
                    if lot.quantity > 0
                    else (lot.unit_price - mark)
                )
                for lot in lots
            ),
            Decimal("0"),
        )

    return EquityPositionProjection(
        instrument=symbol,
        settlement_currency=settlement,
        quantity=quantity,
        open_cost_basis=open_cost_basis,
        realized_pnl=realized,
        unrealized_pnl=unrealized,
        mark_price=mark,
        lots=lots,
    )
