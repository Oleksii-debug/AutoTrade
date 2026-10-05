"""Frozen provider fee-expense evidence for WP-63 cost composition.

This module composes the existing :class:`DurableProviderEconomicBook` historical
cut authority.  It does not invent a commission taxonomy: provider-booked
``FEE_EXPENSE`` is retained as provider fee expense until a separately registered
classification rule proves which portion, if any, is the ablation ``commission``
component.

The resolver is deliberately multi-unit.  It never converts currencies, never
selects an FX rate, and never claims a complete cost composite.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256
import json
import re

from .accounting import AccountingConflict, JournalTransaction, transaction_digest
from .exact_decimal import ExactDecimalError, canonical_decimal_text, exact_sum
from .provider_activity_accounting import (
    DurableProviderEconomicBook,
    EconomicBookCut,
    ProviderEconomicCut,
)


_READ_HISTORICAL_CUT_RAW = DurableProviderEconomicBook.__dict__["read_historical_cut"]
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_EVIDENCE_KIND = "CANONICAL_PROVIDER_FEE_EXPENSE_COMPONENT"
_CLASSIFICATION_BLOCKER = "registered_commission_classification_unavailable"


def _text(value: object, *, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise AccountingConflict(f"{name} must be exact canonical non-empty text")
    return value


def _digest(value: object, *, name: str) -> str:
    text = _text(value, name=name)
    if _SHA256.fullmatch(text) is None:
        raise AccountingConflict(f"{name} must be an exact canonical sha256 digest")
    return text


def _positive_int(value: object, *, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise AccountingConflict(f"{name} must be an exact positive integer")
    return value


def _decimal_text(value: Decimal) -> str:
    if type(value) is not Decimal or not value.is_finite():
        raise AccountingConflict("provider fee amount must be an exact finite Decimal")
    try:
        return canonical_decimal_text(value)
    except ExactDecimalError as error:
        raise AccountingConflict(
            "provider fee amount exceeds exact-decimal resource authority"
        ) from error


def _assert_owner(owner: DurableProviderEconomicBook) -> None:
    if type(owner) is not DurableProviderEconomicBook:
        raise TypeError("owner must be exact DurableProviderEconomicBook")
    if (
        DurableProviderEconomicBook.__dict__.get("read_historical_cut")
        is not _READ_HISTORICAL_CUT_RAW
    ):
        raise AccountingConflict(
            "provider economic historical-read executable changed after composition"
        )
    state = object.__getattribute__(owner, "__dict__")
    if type(state) is not dict:
        raise AccountingConflict("provider economic owner state is not canonical")
    if "read_historical_cut" in state:
        raise AccountingConflict(
            "provider economic owner shadows canonical historical-read executable"
        )


def _canonical_fee_facts(
    historical: EconomicBookCut,
    cut: ProviderEconomicCut,
) -> tuple[tuple[tuple[str, Decimal], ...], tuple[tuple[str, str], ...]]:
    if type(historical) is not EconomicBookCut:
        raise TypeError("historical must be exact EconomicBookCut")
    if type(cut) is not ProviderEconomicCut:
        raise TypeError("cut must be exact ProviderEconomicCut")
    if (
        historical.provider_id != cut.provider_id
        or historical.account_id != cut.account_id
        or historical.environment != cut.environment
        or historical.aggregate_version != cut.aggregate_version
        or historical.book_digest != cut.resulting_book_digest
    ):
        raise AccountingConflict(
            "historical economic projection does not match provider cut"
        )

    all_transaction_digests = tuple(
        (transaction.transaction_id, transaction_digest(transaction))
        for transaction in historical.transactions
    )
    if all_transaction_digests != cut.transaction_digests:
        raise AccountingConflict(
            "historical economic projection transaction identity differs from provider cut"
        )

    amounts: dict[str, list[Decimal]] = {}
    contributors: list[tuple[str, str]] = []
    for transaction in historical.transactions:
        if type(transaction) is not JournalTransaction:
            raise AccountingConflict(
                "historical economic projection contains non-canonical transaction"
            )
        transaction_has_fee = False
        for posting in transaction.postings:
            account = posting.ledger_account
            if not account.startswith("FEE_EXPENSE:"):
                continue
            unit = _text(posting.asset_or_currency, name="fee asset_or_currency")
            if account != f"FEE_EXPENSE:{unit}":
                raise AccountingConflict(
                    "provider fee posting account does not match its exact value unit"
                )
            if type(posting.signed_amount) is not Decimal or not posting.signed_amount.is_finite():
                raise AccountingConflict(
                    "provider fee posting amount is not an exact finite Decimal"
                )
            amounts.setdefault(unit, []).append(posting.signed_amount)
            transaction_has_fee = True
        if transaction_has_fee:
            contributors.append(
                (transaction.transaction_id, transaction_digest(transaction))
            )

    fee_by_unit: list[tuple[str, Decimal]] = []
    for unit in sorted(amounts):
        try:
            amount = exact_sum(amounts[unit])
        except ExactDecimalError as error:
            raise AccountingConflict(
                "provider fee expense exceeds exact-decimal resource authority"
            ) from error
        fee_by_unit.append((unit, amount))
    return tuple(fee_by_unit), tuple(contributors)


def _material(
    *,
    provider_id: str,
    account_id: str,
    environment: str,
    aggregate_version: int,
    journal_sequence: int,
    visibility_journal_sequence: int,
    provider_economic_cut_digest: str,
    provider_economic_book_digest: str,
    fee_expense_by_unit: tuple[tuple[str, Decimal], ...],
    contributing_transactions: tuple[tuple[str, str], ...],
    terminal_cost_composite: bool,
    commission_classified: bool,
    classification_blocker: str,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "evidence_kind": _EVIDENCE_KIND,
        "provider_id": provider_id,
        "account_id": account_id,
        "environment": environment,
        "aggregate_version": aggregate_version,
        "journal_sequence": journal_sequence,
        "visibility_journal_sequence": visibility_journal_sequence,
        "provider_economic_cut_digest": provider_economic_cut_digest,
        "provider_economic_book_digest": provider_economic_book_digest,
        "fee_expense_by_unit": [
            [unit, _decimal_text(amount)] for unit, amount in fee_expense_by_unit
        ],
        "contributing_transactions": [
            list(item) for item in contributing_transactions
        ],
        "terminal_cost_composite": terminal_cost_composite,
        "commission_classified": commission_classified,
        "classification_blocker": classification_blocker,
    }


def _evidence_digest(material: dict[str, object]) -> str:
    raw = json.dumps(
        material,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + sha256(raw).hexdigest()


@dataclass(frozen=True)
class ResolvedAblationProviderFeeExpenseEvidence:
    """Re-verifiable provider fee expense at one frozen provider-economic cut."""

    evidence_kind: str
    provider_id: str
    account_id: str
    environment: str
    aggregate_version: int
    journal_sequence: int
    visibility_journal_sequence: int
    provider_economic_cut_digest: str
    provider_economic_book_digest: str
    fee_expense_by_unit: tuple[tuple[str, Decimal], ...]
    contributing_transactions: tuple[tuple[str, str], ...]
    terminal_cost_composite: bool
    commission_classified: bool
    classification_blocker: str
    evidence_digest: str

    def __post_init__(self) -> None:
        if self.evidence_kind != _EVIDENCE_KIND:
            raise AccountingConflict("provider fee evidence kind is not canonical")
        for name in ("provider_id", "account_id", "environment"):
            _text(getattr(self, name), name=name)
        for name in (
            "aggregate_version",
            "journal_sequence",
            "visibility_journal_sequence",
        ):
            _positive_int(getattr(self, name), name=name)
        _digest(self.provider_economic_cut_digest, name="provider_economic_cut_digest")
        _digest(self.provider_economic_book_digest, name="provider_economic_book_digest")
        _digest(self.evidence_digest, name="evidence_digest")
        if type(self.fee_expense_by_unit) is not tuple:
            raise AccountingConflict("fee_expense_by_unit must be an exact tuple")
        units: list[str] = []
        for item in self.fee_expense_by_unit:
            if type(item) is not tuple or len(item) != 2:
                raise AccountingConflict("provider fee facts must be exact pairs")
            unit, amount = item
            units.append(_text(unit, name="fee unit"))
            _decimal_text(amount)
        if units != sorted(units) or len(units) != len(set(units)):
            raise AccountingConflict("provider fee units must be unique canonical order")
        if type(self.contributing_transactions) is not tuple:
            raise AccountingConflict("contributing_transactions must be an exact tuple")
        transaction_ids: list[str] = []
        for item in self.contributing_transactions:
            if type(item) is not tuple or len(item) != 2:
                raise AccountingConflict("contributing transaction must be an exact pair")
            transaction_id, digest = item
            transaction_ids.append(_text(transaction_id, name="transaction_id"))
            _digest(digest, name="transaction_digest")
        if len(transaction_ids) != len(set(transaction_ids)):
            raise AccountingConflict("contributing transaction identities must be unique")
        if type(self.terminal_cost_composite) is not bool or self.terminal_cost_composite:
            raise AccountingConflict("provider fee evidence cannot be terminal cost composite")
        if type(self.commission_classified) is not bool or self.commission_classified:
            raise AccountingConflict(
                "provider fee evidence cannot self-classify as commission"
            )
        if self.classification_blocker != _CLASSIFICATION_BLOCKER:
            raise AccountingConflict("provider fee classification blocker is not canonical")
        ResolvedAblationProviderFeeExpenseEvidence.verify_integrity(self)

    def verify_integrity(self) -> None:
        material = _material(
            provider_id=self.provider_id,
            account_id=self.account_id,
            environment=self.environment,
            aggregate_version=self.aggregate_version,
            journal_sequence=self.journal_sequence,
            visibility_journal_sequence=self.visibility_journal_sequence,
            provider_economic_cut_digest=self.provider_economic_cut_digest,
            provider_economic_book_digest=self.provider_economic_book_digest,
            fee_expense_by_unit=self.fee_expense_by_unit,
            contributing_transactions=self.contributing_transactions,
            terminal_cost_composite=self.terminal_cost_composite,
            commission_classified=self.commission_classified,
            classification_blocker=self.classification_blocker,
        )
        if self.evidence_digest != _evidence_digest(material):
            raise AccountingConflict(
                "provider fee evidence digest does not match canonical material"
            )


def resolve_ablation_provider_fee_expense_evidence(
    owner: DurableProviderEconomicBook,
    cut: ProviderEconomicCut,
    *,
    expected_visibility_journal_sequence: int,
) -> ResolvedAblationProviderFeeExpenseEvidence:
    """Resolve provider-booked fee expense without inventing commission semantics."""

    _assert_owner(owner)
    if type(cut) is not ProviderEconomicCut:
        raise TypeError("cut must be exact ProviderEconomicCut")
    expected_visibility = _positive_int(
        expected_visibility_journal_sequence,
        name="expected_visibility_journal_sequence",
    )
    historical = _READ_HISTORICAL_CUT_RAW(
        owner,
        cut,
        expected_visibility_journal_sequence=expected_visibility,
    )
    fee_by_unit, contributors = _canonical_fee_facts(historical, cut)
    material = _material(
        provider_id=cut.provider_id,
        account_id=cut.account_id,
        environment=cut.environment,
        aggregate_version=cut.aggregate_version,
        journal_sequence=cut.journal_sequence,
        visibility_journal_sequence=cut.visibility_journal_sequence,
        provider_economic_cut_digest=cut.cut_digest,
        provider_economic_book_digest=historical.book_digest,
        fee_expense_by_unit=fee_by_unit,
        contributing_transactions=contributors,
        terminal_cost_composite=False,
        commission_classified=False,
        classification_blocker=_CLASSIFICATION_BLOCKER,
    )
    return ResolvedAblationProviderFeeExpenseEvidence(
        evidence_kind=_EVIDENCE_KIND,
        provider_id=cut.provider_id,
        account_id=cut.account_id,
        environment=cut.environment,
        aggregate_version=cut.aggregate_version,
        journal_sequence=cut.journal_sequence,
        visibility_journal_sequence=cut.visibility_journal_sequence,
        provider_economic_cut_digest=cut.cut_digest,
        provider_economic_book_digest=historical.book_digest,
        fee_expense_by_unit=fee_by_unit,
        contributing_transactions=contributors,
        terminal_cost_composite=False,
        commission_classified=False,
        classification_blocker=_CLASSIFICATION_BLOCKER,
        evidence_digest=_evidence_digest(material),
    )


def reverify_ablation_provider_fee_expense_evidence(
    owner: DurableProviderEconomicBook,
    cut: ProviderEconomicCut,
    evidence: ResolvedAblationProviderFeeExpenseEvidence,
    *,
    expected_visibility_journal_sequence: int,
) -> ResolvedAblationProviderFeeExpenseEvidence:
    if type(evidence) is not ResolvedAblationProviderFeeExpenseEvidence:
        raise TypeError(
            "evidence must be exact ResolvedAblationProviderFeeExpenseEvidence"
        )
    ResolvedAblationProviderFeeExpenseEvidence.verify_integrity(evidence)
    resolved = resolve_ablation_provider_fee_expense_evidence(
        owner,
        cut,
        expected_visibility_journal_sequence=expected_visibility_journal_sequence,
    )
    if resolved != evidence:
        raise AccountingConflict(
            "provider fee evidence does not match canonical historical owner"
        )
    return resolved
