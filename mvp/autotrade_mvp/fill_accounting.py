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
from datetime import datetime, timezone
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


def _utc_text(value: str, *, name: str) -> str:
    text = _text(value, name=name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{name} must be an ISO timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must include timezone")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _economic_order_key(provider: str, provider_execution_id: str) -> str:
    return (
        f"provider:{_text(provider, name='provider_id').upper()}:"
        f"execution:{_text(provider_execution_id, name='provider_execution_id')}"
    )


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
    observed_at: str | None = None,
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
        economic_effective_at=provider_fill.trade_time,
        economic_order_key=_economic_order_key(
            provider,
            provider_fill.provider_execution_id,
        ),
        observed_at=(
            _utc_text(observed_at, name="observed_at")
            if observed_at is not None
            else None
        ),
    )


def _transaction_matches_provider_fill(
    transaction: JournalTransaction,
    *,
    projected_fill: ProjectedFillEvidence,
    provider_fill: ProviderFillEvidence,
    settlement_currency: str,
    economic_order_key: str,
) -> bool:
    expected = book_equity_fill(
        transaction_id=transaction.transaction_id,
        cause_event_id=transaction.cause_event_id,
        instrument=provider_fill.instrument,
        settlement_currency=settlement_currency,
        side=projected_fill.side,
        quantity=provider_fill.quantity,
        price=provider_fill.price,
        fee=provider_fill.fee_amount,
        fee_currency=provider_fill.fee_currency,
        economic_effective_at=transaction.economic_effective_at,
        economic_order_key=economic_order_key,
        observed_at=transaction.observed_at,
        corrects_transaction_id=transaction.corrects_transaction_id,
    )
    return expected == transaction


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
    correction_observed_at: str,
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
        allow_correction=original_projected_fill.correction_of is not None,
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

    observation = _utc_text(correction_observed_at, name="correction_observed_at")
    order_key = _economic_order_key(
        provider,
        original_provider_fill.provider_execution_id,
    )
    reversed_ids = {
        item.reverses_transaction_id
        for item in book.transactions
        if item.reverses_transaction_id is not None
    }
    active = [
        item
        for item in book.transactions
        if item.economic_order_key == order_key
        and item.reverses_transaction_id is None
        and item.transaction_id not in reversed_ids
    ]
    if len(active) != 1:
        raise AccountingConflict(
            "original provider fill has not been booked as one active economic fact"
        )
    committed_original = active[0]

    if _transaction_matches_provider_fill(
        committed_original,
        projected_fill=corrected_projected_fill,
        provider_fill=corrected_provider_fill,
        settlement_currency=settlement,
        economic_order_key=order_key,
    ):
        corrected_id = committed_original.corrects_transaction_id
        if corrected_id is None:
            raise AccountingConflict(
                "active corrected economics lacks immutable correction lineage"
            )
        prior = next(
            (item for item in book.transactions if item.transaction_id == corrected_id),
            None,
        )
        reversal = next(
            (
                item
                for item in book.transactions
                if item.reverses_transaction_id == corrected_id
            ),
            None,
        )
        if (
            prior is None
            or reversal is None
            or reversal.observed_at != observation
            or committed_original.observed_at != observation
            or not _transaction_matches_provider_fill(
                prior,
                projected_fill=original_projected_fill,
                provider_fill=original_provider_fill,
                settlement_currency=settlement,
                economic_order_key=order_key,
            )
        ):
            raise AccountingConflict(
                "existing correction lineage conflicts with supplied evidence"
            )
        retry_evidence = {
            "schema_version": "1.1.0",
            "provider_id": provider,
            "environment": book.environment,
            "account_id": book.account_id,
            "correction_of": corrected_projected_fill.correction_of,
            "correction_observed_at": observation,
            "original_transaction_id": prior.transaction_id,
            "original": original_evidence,
            "corrected": {
                **corrected_evidence,
                "correction_of": corrected_projected_fill.correction_of,
            },
        }
        retry_digest = payload_digest(retry_evidence).removeprefix("sha256:")
        retry_prefix = (
            f"provider:{provider}:environment:{book.environment}:"
            f"account:{book.account_id}:correction:{retry_digest}"
        )
        if (
            reversal.transaction_id
            != f"provider-fill-correction-reversal:{retry_digest}"
            or reversal.cause_event_id != f"{retry_prefix}:reversal"
            or committed_original.transaction_id
            != f"provider-fill-correction-replacement:{retry_digest}"
            or committed_original.cause_event_id != f"{retry_prefix}:replacement"
        ):
            raise AccountingConflict(
                "existing correction lineage conflicts with immutable correction evidence"
            )
        return reversal, committed_original

    if not _transaction_matches_provider_fill(
        committed_original,
        projected_fill=original_projected_fill,
        provider_fill=original_provider_fill,
        settlement_currency=settlement,
        economic_order_key=order_key,
    ):
        raise AccountingConflict("original provider fill evidence conflicts with ledger")

    correction_evidence = {
        "schema_version": "1.1.0",
        "provider_id": provider,
        "environment": book.environment,
        "account_id": book.account_id,
        "correction_of": corrected_projected_fill.correction_of,
        "correction_observed_at": observation,
        "original_transaction_id": committed_original.transaction_id,
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
        observed_at=observation,
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
        economic_effective_at=committed_original.economic_effective_at,
        economic_order_key=order_key,
        observed_at=observation,
        corrects_transaction_id=committed_original.transaction_id,
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
    observed_at: str | None = None,
) -> bool:
    transaction = build_provider_fill_transaction(
        book=book,
        provider_id=provider_id,
        projected_fill=projected_fill,
        provider_fill=provider_fill,
        expected_instrument=expected_instrument,
        settlement_currency=settlement_currency,
        observed_at=observed_at,
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
    correction_observed_at: str,
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
        correction_observed_at=correction_observed_at,
    )
    return book.append_batch((reversal, replacement))

