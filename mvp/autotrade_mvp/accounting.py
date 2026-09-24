"""Exact multi-asset double-entry accounting foundation for the AutoTrade MVP.

This module is deliberately provider-neutral.  It books immutable economic
transactions and projections, but does not authorize or send orders.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Iterable


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


@dataclass(frozen=True)
class Posting:
    ledger_account: str
    asset_or_currency: str
    signed_amount: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "ledger_account",
            _name(self.ledger_account, field="ledger_account"),
        )
        object.__setattr__(
            self,
            "asset_or_currency",
            _name(self.asset_or_currency, field="asset_or_currency"),
        )
        object.__setattr__(
            self,
            "signed_amount",
            _decimal(self.signed_amount, name="signed_amount"),
        )


@dataclass(frozen=True)
class JournalTransaction:
    transaction_id: str
    cause_event_id: str
    postings: tuple[Posting, ...]
    reverses_transaction_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "transaction_id",
            _name(self.transaction_id, field="transaction_id"),
        )
        object.__setattr__(
            self,
            "cause_event_id",
            _name(self.cause_event_id, field="cause_event_id"),
        )
        object.__setattr__(self, "postings", tuple(self.postings))
        if self.reverses_transaction_id is not None:
            object.__setattr__(
                self,
                "reverses_transaction_id",
                _name(
                    self.reverses_transaction_id,
                    field="reverses_transaction_id",
                ),
            )


def posting(ledger_account: str, asset_or_currency: str, signed_amount: Decimal | str | int) -> Posting:
    return Posting(
        ledger_account=_name(ledger_account, field="ledger_account"),
        asset_or_currency=_name(asset_or_currency, field="asset_or_currency"),
        signed_amount=_decimal(signed_amount, name="signed_amount"),
    )


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
        validate_transaction(transaction)
        transaction_id = _name(transaction.transaction_id, field="transaction_id")
        cause_event_id = _name(transaction.cause_event_id, field="cause_event_id")
        existing = self._by_id.get(transaction_id)
        if existing is not None:
            if existing != transaction:
                raise AccountingConflict(
                    "transaction_id was already committed with different economic content"
                )
            return False

        cause_existing = self._by_cause_event_id.get(cause_event_id)
        if cause_existing is not None:
            raise AccountingConflict(
                "cause_event_id was already booked by a different transaction"
            )

        if transaction.reverses_transaction_id is not None:
            original_id = _name(
                transaction.reverses_transaction_id,
                field="reverses_transaction_id",
            )
            original = self._by_id.get(original_id)
            if original is None:
                raise AccountingConflict("Cannot reverse an unknown transaction")
            if original_id in self._reversed_transaction_ids:
                raise AccountingConflict("Transaction has already been reversed")
            expected = tuple(
                Posting(item.ledger_account, item.asset_or_currency, -item.signed_amount)
                for item in original.postings
            )
            if transaction.postings != expected:
                raise AccountingConflict("A reversal must exactly negate the original postings")
        self._by_id[transaction_id] = transaction
        self._by_cause_event_id[cause_event_id] = transaction
        self._transactions.append(transaction)
        if transaction.reverses_transaction_id is not None:
            self._reversed_transaction_ids.add(transaction.reverses_transaction_id)
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
