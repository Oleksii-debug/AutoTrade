"""Fail-closed cost coverage projected from one canonical economic-book cut.

This module does not invent a complete after-cost number.  It exposes only cost
components whose semantics are unambiguous in the canonical journal and keeps
all other WP-63 components explicitly missing.  FX conversion and non-journal
operating costs remain separate authorities.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256
import json
import re

from mvp.autotrade_mvp.accounting import (
    EconomicBook,
    JournalTransaction,
    ScopedEconomicBook,
    transaction_digest,
)
from mvp.autotrade_mvp.exact_decimal import ExactDecimalError, exact_sum
from mvp.autotrade_mvp.provider_activity_accounting import (
    DurableProviderEconomicBook,
    EconomicBookCut,
)


REQUIRED_ABLATION_COST_COMPONENTS = (
    "commission",
    "spread",
    "slippage",
    "financing",
    "funding",
    "borrow",
    "market_data",
    "model_compute",
    "infrastructure",
    "tax_estimate",
)

# Generic journal account names are not, by themselves, registered WP-63 cost
# classification/projection authority.  In particular:
# - FEE_EXPENSE can include provider/exchange fees and rebates; it is not proof
#   that the registered "commission" component owns that amount.
# - FUNDING_PNL is a signed economic cashflow; a credit must not be silently
#   turned into a non-negative "funding cost".
# - FINANCING_EXPENSE can represent funding, borrow, or other financing scopes.
#
# Dedicated owner-evidence modules preserve those facts and their durable lineage.
# This legacy journal projection therefore keeps them explicit and unresolved
# until a separately registered component classifier/projector composes them.
_JOURNAL_ACCOUNT_COMPONENTS: dict[str, str] = {}
_AMBIGUOUS_COST_ACCOUNT_PREFIXES = (
    "FEE_EXPENSE:",
    "FUNDING_PNL:",
    "FINANCING_EXPENSE:",
)
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")


class CostProjectionError(ValueError):
    pass


def _text(value: object, *, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise CostProjectionError(f"{name} must be exact canonical text")
    return value


def _digest(value: object, *, name: str) -> str:
    text = _text(value, name=name)
    if _DIGEST_RE.fullmatch(text) is None:
        raise CostProjectionError(f"{name} must be canonical SHA-256")
    return text


def _canonical_decimal(value: Decimal) -> str:
    if type(value) is not Decimal or not value.is_finite():
        raise CostProjectionError("cost amount must be exact finite Decimal")
    if value == 0:
        return "0"
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _cut_digest_payload(
    *,
    provider_id: str,
    account_id: str,
    environment: str,
    economic_book_digest: str,
    aggregate_version: int,
    components: tuple["JournalCostComponentEvidence", ...],
    ambiguous_accounts: tuple[str, ...],
    missing_components: tuple[str, ...],
) -> bytes:
    material = {
        "schema_version": 1,
        "provider_id": provider_id,
        "account_id": account_id,
        "environment": environment,
        "economic_book_digest": economic_book_digest,
        "aggregate_version": aggregate_version,
        "components": [item.as_jsonable() for item in components],
        "ambiguous_accounts": list(ambiguous_accounts),
        "missing_components": list(missing_components),
    }
    return json.dumps(
        material,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


@dataclass(frozen=True, slots=True)
class JournalCostComponentEvidence:
    """One native-unit journal component with explicit transaction lineage."""

    component: str
    unit: str
    net_amount: Decimal
    transaction_digests: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.component not in set(_JOURNAL_ACCOUNT_COMPONENTS.values()):
            raise CostProjectionError("component is not journal-classifiable")
        unit = _text(self.unit, name="unit").upper()
        if unit != self.unit:
            raise CostProjectionError("unit must be canonical uppercase text")
        if type(self.net_amount) is not Decimal or not self.net_amount.is_finite():
            raise CostProjectionError("net_amount must be exact finite Decimal")
        if type(self.transaction_digests) is not tuple or not self.transaction_digests:
            raise CostProjectionError(
                "component requires explicit contributing transaction evidence"
            )
        normalized = tuple(
            _digest(value, name="transaction_digest")
            for value in self.transaction_digests
        )
        if normalized != tuple(sorted(set(normalized))):
            raise CostProjectionError(
                "transaction_digests must be unique canonical sorted evidence"
            )

    def as_jsonable(self) -> dict[str, object]:
        return {
            "component": self.component,
            "unit": self.unit,
            "net_amount": _canonical_decimal(self.net_amount),
            "transaction_digests": list(self.transaction_digests),
        }


@dataclass(frozen=True, slots=True)
class CanonicalJournalCostCut:
    """Descriptive partial cost evidence from one immutable economic-book cut."""

    provider_id: str
    account_id: str
    environment: str
    economic_book_digest: str
    aggregate_version: int
    components: tuple[JournalCostComponentEvidence, ...]
    ambiguous_accounts: tuple[str, ...]
    missing_components: tuple[str, ...]
    evidence_digest: str

    def __post_init__(self) -> None:
        provider = _text(self.provider_id, name="provider_id").upper()
        if provider != self.provider_id:
            raise CostProjectionError("provider_id must be canonical uppercase text")
        _text(self.account_id, name="account_id")
        environment = _text(self.environment, name="environment").upper()
        if environment != self.environment or environment not in {
            "REPLAY", "SIMULATION", "PAPER", "LIVE"
        }:
            raise CostProjectionError("environment is not canonical")
        _digest(self.economic_book_digest, name="economic_book_digest")
        if type(self.aggregate_version) is not int or self.aggregate_version < 0:
            raise CostProjectionError("aggregate_version must be non-negative integer")
        if type(self.components) is not tuple:
            raise CostProjectionError("components must be exact immutable tuple")
        ordering = tuple((item.component, item.unit) for item in self.components)
        if ordering != tuple(sorted(set(ordering))):
            raise CostProjectionError("components must be unique and canonically sorted")
        if type(self.ambiguous_accounts) is not tuple or self.ambiguous_accounts != tuple(
            sorted(set(self.ambiguous_accounts))
        ):
            raise CostProjectionError("ambiguous_accounts must be canonical sorted tuple")
        expected_missing = tuple(
            item for item in REQUIRED_ABLATION_COST_COMPONENTS
            if item not in {component.component for component in self.components}
        )
        if self.missing_components != expected_missing:
            raise CostProjectionError("missing component coverage is not canonical")
        expected_digest = "sha256:" + sha256(
            _cut_digest_payload(
                provider_id=self.provider_id,
                account_id=self.account_id,
                environment=self.environment,
                economic_book_digest=self.economic_book_digest,
                aggregate_version=self.aggregate_version,
                components=self.components,
                ambiguous_accounts=self.ambiguous_accounts,
                missing_components=self.missing_components,
            )
        ).hexdigest()
        if self.evidence_digest != expected_digest:
            raise CostProjectionError("cost cut evidence digest is invalid")

    @property
    def complete(self) -> bool:
        return not self.missing_components and not self.ambiguous_accounts


def _recompute_cut_digest(cut: EconomicBookCut) -> str:
    # Rebuild through canonical accounting constructors so hand-edited transaction
    # graphs cannot be accepted merely because a dataclass field says they are valid.
    scoped = ScopedEconomicBook(
        environment=cut.environment,
        account_id=cut.account_id,
        transactions=cut.transactions,
    )
    return scoped.audit_digest()


def project_economic_cut_costs(cut: EconomicBookCut) -> CanonicalJournalCostCut:
    """Project only journal-native cost semantics from an already obtained cut.

    Generic fee/funding/financing account families remain unresolved here because
    journal account identity alone cannot establish the registered WP-63 component
    taxonomy or a signed-cashflow-to-cost projection.  This descriptive function
    does not prove who issued ``cut``.  Authority-bearing callers should use
    :func:`project_provider_journal_costs`, which obtains the cut directly from an
    exact DurableProviderEconomicBook.
    """

    if type(cut) is not EconomicBookCut:
        raise TypeError("cut must be exact EconomicBookCut")
    if _recompute_cut_digest(cut) != cut.book_digest:
        raise CostProjectionError("economic cut digest does not match transactions")

    grouped: dict[tuple[str, str], list[tuple[Decimal, str]]] = {}
    ambiguous_accounts: set[str] = set()
    for transaction in cut.transactions:
        if type(transaction) is not JournalTransaction:
            raise TypeError("economic cut must contain exact JournalTransaction values")
        digest = transaction_digest(transaction)
        for posting in transaction.postings:
            account = posting.ledger_account
            if any(account.startswith(prefix) for prefix in _AMBIGUOUS_COST_ACCOUNT_PREFIXES):
                ambiguous_accounts.add(account)
                continue
            if ":" not in account:
                continue
            family, account_unit = account.split(":", 1)
            component = _JOURNAL_ACCOUNT_COMPONENTS.get(family)
            if component is None:
                continue
            if posting.asset_or_currency != account_unit:
                raise CostProjectionError(
                    "cost account denomination differs from posting unit"
                )
            key = (component, account_unit)
            grouped.setdefault(key, []).append((posting.signed_amount, digest))

    components: list[JournalCostComponentEvidence] = []
    for (component, unit), rows in sorted(grouped.items()):
        try:
            amount = exact_sum(item[0] for item in rows)
        except ExactDecimalError as error:
            raise CostProjectionError(
                "journal cost component exceeds exact-decimal authority"
            ) from error
        components.append(
            JournalCostComponentEvidence(
                component=component,
                unit=unit,
                net_amount=amount,
                transaction_digests=tuple(sorted({item[1] for item in rows})),
            )
        )

    frozen_components = tuple(components)
    missing = tuple(
        item for item in REQUIRED_ABLATION_COST_COMPONENTS
        if item not in {component.component for component in frozen_components}
    )
    ambiguous = tuple(sorted(ambiguous_accounts))
    payload = _cut_digest_payload(
        provider_id=cut.provider_id,
        account_id=cut.account_id,
        environment=cut.environment,
        economic_book_digest=cut.book_digest,
        aggregate_version=cut.aggregate_version,
        components=frozen_components,
        ambiguous_accounts=ambiguous,
        missing_components=missing,
    )
    return CanonicalJournalCostCut(
        provider_id=cut.provider_id,
        account_id=cut.account_id,
        environment=cut.environment,
        economic_book_digest=cut.book_digest,
        aggregate_version=cut.aggregate_version,
        components=frozen_components,
        ambiguous_accounts=ambiguous,
        missing_components=missing,
        evidence_digest="sha256:" + sha256(payload).hexdigest(),
    )


def project_provider_journal_costs(
    economic_book: DurableProviderEconomicBook,
) -> CanonicalJournalCostCut:
    """Obtain and project one cut directly from canonical durable authority."""

    if type(economic_book) is not DurableProviderEconomicBook:
        raise TypeError("economic_book must be exact DurableProviderEconomicBook")
    cut = DurableProviderEconomicBook.read_cut(economic_book)
    return project_economic_cut_costs(cut)


__all__ = [
    "REQUIRED_ABLATION_COST_COMPONENTS",
    "CanonicalJournalCostCut",
    "CostProjectionError",
    "JournalCostComponentEvidence",
    "project_economic_cut_costs",
    "project_provider_journal_costs",
]
