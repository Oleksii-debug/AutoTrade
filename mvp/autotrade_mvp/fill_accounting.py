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
    reverse_transaction,
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


def _validated_fill_evidence(
    *,
    book: ScopedEconomicBook,
    provider_id: str,
    projected_fill: ProjectedFillEvidence,
    provider_fill: ProviderFillEvidence,
    expected_instrument: str,
    settlement_currency: str,
    allow_correction: bool,
) -> tuple[str, str, str, dict[str, object]]:
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

    if projected_fill.correction_of is not None and not allow_correction:
        raise AccountingConflict(
            "corrected fills require explicit atomic reversal/replacement evidence"
        )
    if allow_correction and projected_fill.provider_revision is None:
        raise AccountingConflict(
            "corrected fill requires immutable provider_revision evidence"
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

    evidence: dict[str, object] = {
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
    return provider, instrument, settlement, evidence


def build_provider_fill_transaction(
    *,
    book: ScopedEconomicBook,
    provider_id: str,
    projected_fill: ProjectedFillEvidence,
    provider_fill: ProviderFillEvidence,
    expected_instrument: str,
    settlement_currency: str,
) -> JournalTransaction:
    provider, _instrument, settlement, evidence = _validated_fill_evidence(
        book=book,
        provider_id=provider_id,
        projected_fill=projected_fill,
        provider_fill=provider_fill,
        expected_instrument=expected_instrument,
        settlement_currency=settlement_currency,
        allow_correction=False,
    )
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


def build_provider_fill_correction_transactions(
    *,
    book: ScopedEconomicBook,
    provider_id: str,
    original_projected_fill: ProjectedFillEvidence,
    original_provider_fill: ProviderFillEvidence,
    corrected_projected_fill: ProjectedFillEvidence,
    corrected_provider_fill: ProviderFillEvidence,
    expected_instrument: str,
    settlement_currency: str,
) -> tuple[JournalTransaction, JournalTransaction]:
    if corrected_projected_fill.correction_of != original_projected_fill.fill_id:
        raise AccountingConflict("corrected fill does not identify the original fill")
    if corrected_projected_fill.fill_id == original_projected_fill.fill_id:
        raise AccountingConflict("corrected fill_id must differ from original fill_id")

    provider, instrument, settlement, original_evidence = _validated_fill_evidence(
        book=book,
        provider_id=provider_id,
        projected_fill=original_projected_fill,
        provider_fill=original_provider_fill,
        expected_instrument=expected_instrument,
        settlement_currency=settlement_currency,
        allow_correction=False,
    )
    corrected_provider, corrected_instrument, corrected_settlement, corrected_evidence = (
        _validated_fill_evidence(
            book=book,
            provider_id=provider_id,
            projected_fill=corrected_projected_fill,
            provider_fill=corrected_provider_fill,
            expected_instrument=expected_instrument,
            settlement_currency=settlement_currency,
            allow_correction=True,
        )
    )
    if (provider, instrument, settlement) != (
        corrected_provider,
        corrected_instrument,
        corrected_settlement,
    ):
        raise AccountingConflict("correction scope does not match original fill scope")
    if corrected_projected_fill.provider_execution_id != original_projected_fill.provider_execution_id:
        raise AccountingConflict("correction provider execution identity changed")
    if corrected_projected_fill.intent_id != original_projected_fill.intent_id:
        raise AccountingConflict("correction intent identity changed")
    if corrected_projected_fill.client_order_id != original_projected_fill.client_order_id:
        raise AccountingConflict("correction client order identity changed")
    if corrected_projected_fill.side != original_projected_fill.side:
        raise AccountingConflict("correction side changed")

    original_transaction = build_provider_fill_transaction(
        book=book,
        provider_id=provider,
        projected_fill=original_projected_fill,
        provider_fill=original_provider_fill,
        expected_instrument=instrument,
        settlement_currency=settlement,
    )
    committed_original = next(
        (
            transaction
            for transaction in book.transactions
            if transaction.transaction_id == original_transaction.transaction_id
        ),
        None,
    )
    if committed_original is None:
        raise AccountingConflict("original provider fill has not been booked")
    if committed_original != original_transaction:
        raise AccountingConflict("original provider fill evidence conflicts with ledger")

    correction_evidence = {
        "schema_version": "1.0.0",
        "provider_id": provider,
        "environment": book.environment,
        "account_id": book.account_id,
        "correction_of": corrected_projected_fill.correction_of,
        "original_transaction_id": original_transaction.transaction_id,
        "original": original_evidence,
        "corrected": {
            **corrected_evidence,
            "correction_of": corrected_projected_fill.correction_of,
        },
    }
    correction_digest = payload_digest(correction_evidence).removeprefix("sha256:")
    cause_prefix = (
        f"provider:{provider}:environment:{book.environment}:"
        f"account:{book.account_id}:correction:{correction_digest}"
    )
    reversal = reverse_transaction(
        committed_original,
        transaction_id=f"provider-fill-correction-reversal:{correction_digest}",
        cause_event_id=f"{cause_prefix}:reversal",
    )
    replacement = book_equity_fill(
        transaction_id=f"provider-fill-correction-replacement:{correction_digest}",
        cause_event_id=f"{cause_prefix}:replacement",
        instrument=corrected_provider_fill.instrument,
        settlement_currency=settlement,
        side=corrected_projected_fill.side,
        quantity=corrected_provider_fill.quantity,
        price=corrected_provider_fill.price,
        fee=corrected_provider_fill.fee_amount,
        fee_currency=corrected_provider_fill.fee_currency,
    )
    return reversal, replacement

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

def book_provider_fill_correction(
    *,
    book: ScopedEconomicBook,
    provider_id: str,
    original_projected_fill: ProjectedFillEvidence,
    original_provider_fill: ProviderFillEvidence,
    corrected_projected_fill: ProjectedFillEvidence,
    corrected_provider_fill: ProviderFillEvidence,
    expected_instrument: str,
    settlement_currency: str,
) -> bool:
    reversal, replacement = build_provider_fill_correction_transactions(
        book=book,
        provider_id=provider_id,
        original_projected_fill=original_projected_fill,
        original_provider_fill=original_provider_fill,
        corrected_projected_fill=corrected_projected_fill,
        corrected_provider_fill=corrected_provider_fill,
        expected_instrument=expected_instrument,
        settlement_currency=settlement_currency,
    )
    return book.append_batch((reversal, replacement))

