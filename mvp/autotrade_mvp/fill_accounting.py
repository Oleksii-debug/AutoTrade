"""Fail-closed bridge from reconciliable provider fills to canonical accounting.

This module owns no ledger, provider transport, order state, or reconciliation
authority. It only requires independent order-projection and provider-fill
evidence to agree before delegating to the canonical accounting book.
"""

from __future__ import annotations

from .accounting import (
    AccountingConflict,
    JournalTransaction,
    ScopedEconomicBook,
    book_equity_fill,
)
from .orders import FillObservation
from .persistence import payload_digest
from .reconciliation import ProviderFillEvidence


def _text(value: str, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    return value.strip()


def build_provider_fill_transaction(
    *,
    book: ScopedEconomicBook,
    provider_id: str,
    projected_fill: FillObservation,
    provider_fill: ProviderFillEvidence,
    expected_instrument: str,
    settlement_currency: str,
) -> JournalTransaction:
    if not isinstance(book, ScopedEconomicBook):
        raise TypeError("book must be ScopedEconomicBook")
    if not isinstance(projected_fill, FillObservation):
        raise TypeError("projected_fill must be FillObservation, not an acknowledgement")
    if not isinstance(provider_fill, ProviderFillEvidence):
        raise TypeError("provider_fill must be ProviderFillEvidence")

    provider = _text(provider_id, name="provider_id")
    instrument = _text(expected_instrument, name="expected_instrument")
    settlement = _text(settlement_currency, name="settlement_currency").upper()

    if projected_fill.correction_of is not None:
        raise AccountingConflict(
            "corrected fills require explicit atomic reversal/replacement evidence"
        )
    if projected_fill.provider_execution_id != provider_fill.provider_execution_id:
        raise AccountingConflict("provider execution identity does not match projection")
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
        "client_order_id": provider_fill.client_order_id,
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
    projected_fill: FillObservation,
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
