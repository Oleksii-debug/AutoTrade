"""Fail-closed bridge from reconciliable provider fills to canonical accounting.

This module owns no ledger, provider transport, order state, or reconciliation
authority. It consumes a small immutable projection-evidence DTO plus independent
provider-fill evidence before delegating to the canonical accounting book.

The DTO deliberately does not import either order projection implementation.
That keeps accounting downstream of the single WP-19 lifecycle authority without
turning a particular in-memory projection module into an accounting dependency.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from .accounting import (
    AccountingConflict,
    JournalTransaction,
    ScopedEconomicBook,
    book_equity_fill,
)
from .persistence import payload_digest
from .reconciliation import ProviderFillEvidence


def _text(value: str, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    return value.strip()


def _decimal(value, *, name: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError(f"{name} must use Decimal, string or integer input")
    try:
        result = value if isinstance(value, Decimal) else Decimal(value)
    except (InvalidOperation, ValueError, TypeError) as error:
        raise ValueError(f"{name} must be a finite decimal") from error
    if not result.is_finite():
        raise ValueError(f"{name} must be a finite decimal")
    return result


@dataclass(frozen=True)
class ProjectedFillEvidence:
    """Immutable economic fact emitted by the canonical order projection boundary."""

    fill_id: str
    provider_execution_id: str
    intent_id: str
    client_order_id: str | None
    side: str
    quantity: Decimal
    price: Decimal
    provider_revision: str | None = None
    correction_of: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "fill_id", _text(self.fill_id, name="fill_id"))
        object.__setattr__(
            self,
            "provider_execution_id",
            _text(self.provider_execution_id, name="provider_execution_id"),
        )
        object.__setattr__(self, "intent_id", _text(self.intent_id, name="intent_id"))
        object.__setattr__(
            self,
            "client_order_id",
            (_text(self.client_order_id, name="client_order_id") if self.client_order_id is not None else None),
        )
        normalized_side = _text(self.side, name="side").upper()
        if normalized_side not in {"BUY", "SELL"}:
            raise ValueError("side must be BUY or SELL")
        object.__setattr__(self, "side", normalized_side)
        quantity = _decimal(self.quantity, name="quantity")
        price = _decimal(self.price, name="price")
        if quantity <= 0 or price <= 0:
            raise ValueError("quantity and price must be positive")
        object.__setattr__(self, "quantity", quantity)
        object.__setattr__(self, "price", price)
        object.__setattr__(
            self,
            "provider_revision",
            (
                _text(self.provider_revision, name="provider_revision")
                if self.provider_revision is not None
                else None
            ),
        )
        object.__setattr__(
            self,
            "correction_of",
            (
                _text(self.correction_of, name="correction_of")
                if self.correction_of is not None
                else None
            ),
        )

    @classmethod
    def create(
        cls,
        *,
        fill_id: str,
        provider_execution_id: str,
        intent_id: str,
        client_order_id: str | None,
        side: str,
        quantity,
        price,
        provider_revision: str | None = None,
        correction_of: str | None = None,
    ) -> "ProjectedFillEvidence":
        return cls(
            fill_id=fill_id,
            provider_execution_id=provider_execution_id,
            intent_id=intent_id,
            client_order_id=client_order_id,
            side=side,
            quantity=_decimal(quantity, name="quantity"),
            price=_decimal(price, name="price"),
            provider_revision=provider_revision,
            correction_of=correction_of,
        )


def build_provider_fill_transaction(
    *,
    book: ScopedEconomicBook,
    provider_id: str,
    projected_fill: ProjectedFillEvidence,
    provider_fill: ProviderFillEvidence,
    expected_instrument: str,
    settlement_currency: str,
) -> JournalTransaction:
    if not isinstance(book, ScopedEconomicBook):
        raise TypeError("book must be ScopedEconomicBook")
    if not isinstance(projected_fill, ProjectedFillEvidence):
        raise TypeError(
            "projected_fill must be ProjectedFillEvidence, not an acknowledgement"
        )
    if not isinstance(provider_fill, ProviderFillEvidence):
        raise TypeError("provider_fill must be ProviderFillEvidence")

    provider = _text(provider_id, name="provider_id").upper()
    instrument = _text(expected_instrument, name="expected_instrument")
    settlement = _text(settlement_currency, name="settlement_currency").upper()

    if projected_fill.correction_of is not None:
        raise AccountingConflict(
            "corrected fills require explicit atomic reversal/replacement evidence"
        )
    if projected_fill.provider_execution_id != provider_fill.provider_execution_id:
        raise AccountingConflict("provider execution identity does not match projection")
    if (
        provider_fill.client_order_id is not None
        and projected_fill.client_order_id != provider_fill.client_order_id
    ):
        raise AccountingConflict("provider client order identity does not match projection")
    if projected_fill.quantity != provider_fill.quantity:
        raise AccountingConflict("provider fill quantity does not match projection")
    if projected_fill.price != provider_fill.price:
        raise AccountingConflict("provider fill price does not match projection")
    if provider_fill.instrument != instrument:
        raise AccountingConflict("provider instrument does not match expected instrument")

    evidence = {
        "schema_version": "1.0.0",
        "provider_id": provider,
        "environment": book.environment,
        "account_id": book.account_id,
        "provider_execution_id": provider_fill.provider_execution_id,
        "client_order_id": projected_fill.client_order_id,
        "fill_id": projected_fill.fill_id,
        "intent_id": projected_fill.intent_id,
        "side": projected_fill.side,
        "instrument": provider_fill.instrument,
        "quantity": format(provider_fill.quantity, "f"),
        "price": format(provider_fill.price, "f"),
        "fee_amount": format(provider_fill.fee_amount, "f"),
        "fee_currency": provider_fill.fee_currency,
        "trade_time": provider_fill.trade_time,
        "provider_revision": projected_fill.provider_revision,
    }
    evidence_digest = payload_digest(evidence)
    transaction_id = "provider-fill:" + evidence_digest.removeprefix("sha256:")
    cause_event_id = (
        f"provider:{provider}:environment:{book.environment}:"
        f"account:{book.account_id}:execution:{provider_fill.provider_execution_id}"
    )
    return book_equity_fill(
        transaction_id=transaction_id,
        cause_event_id=cause_event_id,
        instrument=provider_fill.instrument,
        settlement_currency=settlement,
        side=projected_fill.side,
        quantity=provider_fill.quantity,
        price=provider_fill.price,
        fee=provider_fill.fee_amount,
        fee_currency=provider_fill.fee_currency,
    )


def book_provider_fill(
    *,
    book: ScopedEconomicBook,
    provider_id: str,
    projected_fill: ProjectedFillEvidence,
    provider_fill: ProviderFillEvidence,
    expected_instrument: str,
    settlement_currency: str,
) -> bool:
    transaction = build_provider_fill_transaction(
        book=book,
        provider_id=provider_id,
        projected_fill=projected_fill,
        provider_fill=provider_fill,
        expected_instrument=expected_instrument,
        settlement_currency=settlement_currency,
    )
    return book.append(transaction)
