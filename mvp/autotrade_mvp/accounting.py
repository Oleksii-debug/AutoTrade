"""Exact multi-asset double-entry accounting foundation for the AutoTrade MVP.

This module is deliberately provider-neutral.  It books immutable economic
transactions and projections, but does not authorize or send orders.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
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


def _instant(value: str | None, *, field: str) -> str | None:
    if value is None:
        return None
    text = _name(value, field=field)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{field} must be an ISO timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _instant_value(value: str, *, field: str) -> datetime:
    canonical = _instant(value, field=field)
    assert canonical is not None
    return datetime.fromisoformat(canonical.replace("Z", "+00:00"))


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
    economic_effective_at: str | None = None
    economic_order_key: str | None = None
    observed_at: str | None = None
    corrects_transaction_id: str | None = None


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
        economic_effective_at=_instant(
            transaction.economic_effective_at,
            field="economic_effective_at",
        ),
        economic_order_key=(
            _name(transaction.economic_order_key, field="economic_order_key")
            if transaction.economic_order_key is not None
            else None
        ),
        observed_at=_instant(transaction.observed_at, field="observed_at"),
        corrects_transaction_id=(
            _name(transaction.corrects_transaction_id, field="corrects_transaction_id")
            if transaction.corrects_transaction_id is not None
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
        "schema_version": "1.2.0",
        "transaction_id": normalized.transaction_id,
        "cause_event_id": normalized.cause_event_id,
        "reverses_transaction_id": normalized.reverses_transaction_id,
        "economic_effective_at": normalized.economic_effective_at,
        "economic_order_key": normalized.economic_order_key,
        "observed_at": normalized.observed_at,
        "corrects_transaction_id": normalized.corrects_transaction_id,
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
    effective = _instant(
        transaction.economic_effective_at,
        field="economic_effective_at",
    )
    order_key = (
        _name(transaction.economic_order_key, field="economic_order_key")
        if transaction.economic_order_key is not None
        else None
    )
    if (effective is None) != (order_key is None):
        raise ValueError(
            "economic_effective_at and economic_order_key must be supplied together"
        )
    observed = _instant(transaction.observed_at, field="observed_at")
    if effective is not None and observed is not None:
        if _instant_value(observed, field="observed_at") < _instant_value(
            effective,
            field="economic_effective_at",
        ):
            raise ValueError("observed_at cannot precede economic_effective_at")
    if transaction.corrects_transaction_id is not None:
        corrected_id = _name(
            transaction.corrects_transaction_id,
            field="corrects_transaction_id",
        )
        if corrected_id == transaction.transaction_id:
            raise ValueError("transaction cannot correct itself")
        if transaction.reverses_transaction_id is not None:
            raise ValueError(
                "transaction cannot both reverse and replace corrected economics"
            )
        if effective is None or order_key is None or observed is None:
            raise ValueError(
                "correction replacement requires economic ordering and observation evidence"
            )
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
        self._replacement_by_corrected_id: dict[str, str] = {}
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

        if normalized.corrects_transaction_id is not None:
            corrected_id = normalized.corrects_transaction_id
            corrected = self._by_id.get(corrected_id)
            if corrected is None:
                raise AccountingConflict("Cannot correct an unknown transaction")
            if corrected_id not in self._reversed_transaction_ids:
                raise AccountingConflict(
                    "Correction replacement requires an explicit prior reversal"
                )
            existing_replacement = self._replacement_by_corrected_id.get(corrected_id)
            if existing_replacement not in {None, transaction_id}:
                raise AccountingConflict(
                    "Corrected transaction already has a different replacement"
                )
            if (
                corrected.economic_effective_at != normalized.economic_effective_at
                or corrected.economic_order_key != normalized.economic_order_key
            ):
                raise AccountingConflict(
                    "Correction replacement must preserve economic ordering identity"
                )
            reversal = next(
                (
                    item
                    for item in self._transactions
                    if item.reverses_transaction_id == corrected_id
                ),
                None,
            )
            if reversal is None or reversal.observed_at != normalized.observed_at:
                raise AccountingConflict(
                    "Correction reversal/replacement observation evidence does not match"
                )

        self._by_id[transaction_id] = normalized
        self._by_cause_event_id[cause_event_id] = normalized
        self._transactions.append(normalized)
        if normalized.reverses_transaction_id is not None:
            self._reversed_transaction_ids.add(normalized.reverses_transaction_id)
        if normalized.corrects_transaction_id is not None:
            self._replacement_by_corrected_id[
                normalized.corrects_transaction_id
            ] = transaction_id
        return True

    def append_batch(self, transactions: Iterable[JournalTransaction]) -> bool:
        """Atomically append an immutable batch or leave the live book unchanged.

        Exact replay of a fully committed batch is idempotent. A mixed state where
        only part of the batch already exists fails closed rather than silently
        completing a transaction group whose original atomicity cannot be proven.
        """

        batch = tuple(transactions)
        if not batch:
            raise ValueError("atomic transaction batch must not be empty")

        candidate = EconomicBook(self._transactions)
        outcomes = tuple(candidate.append(transaction) for transaction in batch)
        if any(outcomes) and not all(outcomes):
            raise AccountingConflict(
                "atomic transaction batch is only partially committed"
            )
        if not any(outcomes):
            return False

        self._transactions = candidate._transactions.copy()
        self._by_id = candidate._by_id.copy()
        self._by_cause_event_id = candidate._by_cause_event_id.copy()
        self._reversed_transaction_ids = candidate._reversed_transaction_ids.copy()
        self._replacement_by_corrected_id = (
            candidate._replacement_by_corrected_id.copy()
        )
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

    def append_batch(self, transactions: Iterable[JournalTransaction]) -> bool:
        return self._book.append_batch(transactions)

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
    economic_effective_at: str | None = None,
    economic_order_key: str | None = None,
    observed_at: str | None = None,
    corrects_transaction_id: str | None = None,
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
        economic_effective_at=economic_effective_at,
        economic_order_key=economic_order_key,
        observed_at=observed_at,
        corrects_transaction_id=corrects_transaction_id,
    )
    validate_transaction(transaction)
    return transaction


def book_equity_split_adjustment(
    *,
    transaction_id: str,
    cause_event_id: str,
    instrument: str,
    pre_split_quantity: Decimal | str | int,
    numerator: Decimal | str | int,
    denominator: Decimal | str | int,
    economic_effective_at: str,
    economic_order_key: str,
    observed_at: str,
    corrects_transaction_id: str | None = None,
) -> JournalTransaction:
    """Book a canonical zero-cash equity split quantity transformation.

    The ratio is encoded in the clearing account so a downstream projection can
    independently reconstruct and validate the exact lot transformation from
    immutable journal history. Fractional transformations that cannot be
    represented exactly by the current Decimal lot model fail closed.
    """

    symbol = _name(instrument, field="instrument")
    before = _decimal(pre_split_quantity, name="pre_split_quantity")
    num = _decimal(numerator, name="numerator")
    den = _decimal(denominator, name="denominator")
    if (
        num <= 0
        or den <= 0
        or num != num.to_integral_value()
        or den != den.to_integral_value()
    ):
        raise ValueError("split numerator and denominator must be positive integers")
    if before == 0:
        raise ValueError("split adjustment requires a non-zero pre-split position")

    target = before * num
    after = target / den
    if after * den != target:
        raise AccountingConflict(
            "split quantity cannot be represented exactly by the Decimal position model"
        )
    delta = after - before
    if delta == 0:
        raise ValueError("split adjustment must change position quantity")

    num_text = format(num.quantize(Decimal("1")), "f")
    den_text = format(den.quantize(Decimal("1")), "f")
    transaction = JournalTransaction(
        transaction_id=_name(transaction_id, field="transaction_id"),
        cause_event_id=_name(cause_event_id, field="cause_event_id"),
        postings=(
            posting(f"POSITION:{symbol}", symbol, delta),
            posting(
                f"CORPORATE_ACTION_SPLIT_CLEARING:{symbol}:{num_text}:{den_text}",
                symbol,
                -delta,
            ),
        ),
        economic_effective_at=economic_effective_at,
        economic_order_key=economic_order_key,
        observed_at=observed_at,
        corrects_transaction_id=corrects_transaction_id,
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
    observed_at: str | None = None,
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
        economic_effective_at=original.economic_effective_at,
        economic_order_key=original.economic_order_key,
        observed_at=observed_at,
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


def _canonical_equity_split_terms(
    transaction: JournalTransaction,
    *,
    instrument: str,
) -> tuple[Decimal, Decimal, Decimal] | None:
    """Extract delta and ratio only from canonical split-adjustment shape."""

    symbol = _name(instrument, field="instrument")
    normalized = _normalized_transaction(transaction)
    prefix = f"CORPORATE_ACTION_SPLIT_CLEARING:{symbol}:"
    split_clearing = [
        item
        for item in normalized.postings
        if item.ledger_account.startswith(prefix)
        and item.asset_or_currency == symbol
    ]
    if not split_clearing:
        return None
    position_postings = [
        item
        for item in normalized.postings
        if item.ledger_account == f"POSITION:{symbol}"
        and item.asset_or_currency == symbol
    ]
    if len(position_postings) != 1 or len(split_clearing) != 1:
        raise AccountingConflict(
            "Split projection requires exactly one position and split-clearing posting"
        )
    if len(normalized.postings) != 2:
        raise AccountingConflict(
            "Split projection rejects non-canonical extra postings"
        )
    delta = position_postings[0].signed_amount
    if delta == 0 or split_clearing[0].signed_amount != -delta:
        raise AccountingConflict(
            "Split projection requires an exactly balanced non-zero quantity delta"
        )
    suffix = split_clearing[0].ledger_account[len(prefix):]
    parts = suffix.split(":")
    if len(parts) != 2:
        raise AccountingConflict("Split projection ratio identity is malformed")
    num_text, den_text = parts
    if (
        not num_text.isascii()
        or not den_text.isascii()
        or not num_text.isdigit()
        or not den_text.isdigit()
        or num_text == "0"
        or den_text == "0"
        or (len(num_text) > 1 and num_text.startswith("0"))
        or (len(den_text) > 1 and den_text.startswith("0"))
    ):
        raise AccountingConflict("Split projection ratio identity is not canonical")
    numerator = Decimal(num_text)
    denominator = Decimal(den_text)
    if numerator == denominator:
        raise AccountingConflict("Split projection ratio must change quantity")
    return delta, numerator, denominator


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
    """Project FIFO gross basis/P&L from immutable economic history.

    Fees remain separately expensed by the accounting book. When position-fill
    corrections exist, reversed facts are excluded and active fills are replayed
    by explicit economic effective time plus immutable order key. Missing or
    ambiguous ordering evidence fails closed instead of falling back to append
    order and leaking correction-observation timing into economic chronology.
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

    transactions = tuple(book.transactions)
    by_id = {transaction.transaction_id: transaction for transaction in transactions}
    reversed_ids = {
        transaction.reverses_transaction_id
        for transaction in transactions
        if transaction.reverses_transaction_id is not None
    }
    reversal_ids = {
        transaction.transaction_id
        for transaction in transactions
        if transaction.reverses_transaction_id is not None
    }
    position_corrections: list[JournalTransaction] = []
    split_correction_ids: set[str] = set()
    for transaction in transactions:
        if transaction.reverses_transaction_id is None:
            continue
        corrected_id = transaction.reverses_transaction_id
        corrected = by_id.get(corrected_id)
        if corrected is None:
            raise AccountingConflict(
                "Position correction reversal lacks original transaction"
            )
        split_terms = _canonical_equity_split_terms(
            corrected,
            instrument=symbol,
        )
        fill_terms = _canonical_equity_fill_terms(
            corrected,
            instrument=symbol,
            settlement_currency=settlement,
        )
        if split_terms is not None or fill_terms is not None:
            position_corrections.append(transaction)
        if split_terms is not None:
            split_correction_ids.add(corrected_id)

    has_position_correction = bool(position_corrections)
    if has_position_correction:
        for reversal in position_corrections:
            corrected_id = reversal.reverses_transaction_id
            assert corrected_id is not None
            corrected = by_id[corrected_id]
            replacements = [
                item
                for item in transactions
                if item.corrects_transaction_id == corrected_id
            ]
            if len(replacements) != 1:
                raise AccountingConflict(
                    "Corrected FIFO history requires exactly one explicit replacement lineage"
                )
            replacement = replacements[0]
            if (
                replacement.economic_effective_at
                != corrected.economic_effective_at
                or replacement.economic_order_key
                != corrected.economic_order_key
                or replacement.observed_at != reversal.observed_at
            ):
                raise AccountingConflict(
                    "Corrected FIFO lineage changed economic identity or observation evidence"
                )
            if (
                corrected_id in split_correction_ids
                and _canonical_equity_split_terms(
                    replacement,
                    instrument=symbol,
                )
                is None
            ):
                raise AccountingConflict(
                    "Corrected split history requires one canonical split replacement"
                )

    position_events: list[
        tuple[str, JournalTransaction, tuple[Decimal, ...]]
    ] = []
    has_split = False
    for transaction in transactions:
        if (
            transaction.transaction_id in reversed_ids
            or transaction.transaction_id in reversal_ids
        ):
            continue
        split_terms = _canonical_equity_split_terms(
            transaction,
            instrument=symbol,
        )
        if split_terms is not None:
            has_split = True
            position_events.append(("SPLIT", transaction, split_terms))
            continue
        fill_terms = _canonical_equity_fill_terms(
            transaction,
            instrument=symbol,
            settlement_currency=settlement,
        )
        if fill_terms is not None:
            position_events.append(("FILL", transaction, fill_terms))

    if has_position_correction or has_split:
        ordering: set[tuple[datetime, str]] = set()
        for kind, transaction, _terms in position_events:
            if (
                transaction.economic_effective_at is None
                or transaction.economic_order_key is None
            ):
                raise AccountingConflict(
                    "Corrected/split FIFO history requires economic effective-time "
                    "and immutable order evidence for every position event"
                )
            key = (
                _instant_value(
                    transaction.economic_effective_at,
                    field="economic_effective_at",
                ),
                transaction.economic_order_key,
            )
            if key in ordering:
                raise AccountingConflict(
                    "Corrected/split FIFO history has ambiguous economic ordering"
                )
            ordering.add(key)
        position_events.sort(
            key=lambda item: (
                _instant_value(
                    item[1].economic_effective_at,
                    field="economic_effective_at",
                ),
                item[1].economic_order_key,
                item[1].transaction_id,
            )
        )

    mutable_lots: list[list[Decimal | str]] = []
    realized = Decimal("0")

    for kind, transaction, terms in position_events:
        if kind == "SPLIT":
            delta, numerator, denominator = terms
            current_quantity = sum(
                (
                    lot[0]
                    for lot in mutable_lots
                    if isinstance(lot[0], Decimal)
                ),
                Decimal("0"),
            )
            if current_quantity == 0:
                raise AccountingConflict(
                    "Split adjustment cannot transform an empty projected position"
                )
            target = current_quantity * numerator
            next_quantity = target / denominator
            if next_quantity * denominator != target:
                raise AccountingConflict(
                    "Split projected quantity is not exactly representable"
                )
            if next_quantity - current_quantity != delta:
                raise AccountingConflict(
                    "Split adjustment delta conflicts with projected pre-split quantity"
                )

            for lot in mutable_lots:
                lot_quantity = lot[0]
                lot_price = lot[1]
                assert isinstance(lot_quantity, Decimal)
                assert isinstance(lot_price, Decimal)
                quantity_target = lot_quantity * numerator
                adjusted_quantity = quantity_target / denominator
                if adjusted_quantity * denominator != quantity_target:
                    raise AccountingConflict(
                        "Split lot quantity is not exactly representable"
                    )
                price_target = lot_price * denominator
                adjusted_price = price_target / numerator
                if adjusted_price * numerator != price_target:
                    raise AccountingConflict(
                        "Split lot basis is not exactly representable"
                    )
                lot[0] = adjusted_quantity
                lot[1] = adjusted_price
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
        policy_version=(
            "FIFO_GROSS_CA_SPLIT_V3"
            if has_split
            else (
                "FIFO_GROSS_EFFECTIVE_V2"
                if has_position_correction
                else "FIFO_GROSS_V1"
            )
        ),
    )
