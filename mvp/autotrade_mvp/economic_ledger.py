"""Exact, append-only economic ledger foundation for AutoTrade.

The ledger is deliberately unit-aware and authority-neutral. It never converts
currencies, guesses prices, or grants trading permission. Every transaction must
balance independently for each economic unit it touches.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256
import json
import re
from typing import Iterable


_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_ENVIRONMENTS = frozenset({"REPLAY", "SIMULATION", "PAPER", "LIVE"})


def _text(value: str, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    return value.strip()


def _decimal(value: Decimal, *, name: str) -> Decimal:
    if not isinstance(value, Decimal):
        raise TypeError(f"{name} must be Decimal")
    if not value.is_finite():
        raise ValueError(f"{name} must be finite")
    return value


def _digest(value: str, *, name: str) -> str:
    normalized = _text(value, name=name)
    if _SHA256.fullmatch(normalized) is None:
        raise ValueError(f"{name} must be canonical sha256:<64 lowercase hex>")
    return normalized


@dataclass(frozen=True)
class LedgerEntry:
    book: str
    unit: str
    amount: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "book", _text(self.book, name="book"))
        object.__setattr__(self, "unit", _text(self.unit, name="unit"))
        amount = _decimal(self.amount, name="amount")
        if amount == Decimal("0"):
            raise ValueError("zero ledger entries are not allowed")
        object.__setattr__(self, "amount", amount)

    def canonical(self) -> dict[str, str]:
        return {
            "book": self.book,
            "unit": self.unit,
            "amount": format(self.amount, "f"),
        }


@dataclass(frozen=True)
class LedgerTransaction:
    transaction_id: str
    event_id: str
    environment: str
    account_id: str
    evidence_digest: str
    entries: tuple[LedgerEntry, ...]
    reversal_of: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "transaction_id",
            _text(self.transaction_id, name="transaction_id"),
        )
        object.__setattr__(self, "event_id", _text(self.event_id, name="event_id"))
        environment = _text(self.environment, name="environment").upper()
        if environment not in _ENVIRONMENTS:
            raise ValueError("unsupported environment")
        object.__setattr__(self, "environment", environment)
        object.__setattr__(
            self,
            "account_id",
            _text(self.account_id, name="account_id"),
        )
        object.__setattr__(
            self,
            "evidence_digest",
            _digest(self.evidence_digest, name="evidence_digest"),
        )
        if not isinstance(self.entries, tuple) or len(self.entries) < 2:
            raise ValueError("entries must be a tuple with at least two postings")
        if not all(isinstance(entry, LedgerEntry) for entry in self.entries):
            raise ValueError("entries must contain LedgerEntry values")
        if self.reversal_of is not None:
            object.__setattr__(
                self,
                "reversal_of",
                _text(self.reversal_of, name="reversal_of"),
            )
            if self.reversal_of == self.transaction_id:
                raise ValueError("a transaction cannot reverse itself")

        totals: dict[str, Decimal] = {}
        for entry in self.entries:
            totals[entry.unit] = totals.get(entry.unit, Decimal("0")) + entry.amount
        unbalanced = {
            unit: amount
            for unit, amount in totals.items()
            if amount != Decimal("0")
        }
        if unbalanced:
            details = ", ".join(
                f"{unit}={format(amount, 'f')}"
                for unit, amount in sorted(unbalanced.items())
            )
            raise ValueError(f"transaction is not balanced by unit: {details}")

    def canonical(self) -> dict[str, object]:
        return {
            "schema_version": "1.0.0",
            "transaction_id": self.transaction_id,
            "event_id": self.event_id,
            "environment": self.environment,
            "account_id": self.account_id,
            "evidence_digest": self.evidence_digest,
            "reversal_of": self.reversal_of,
            "entries": [entry.canonical() for entry in self.entries],
        }

    def digest(self) -> str:
        encoded = json.dumps(
            self.canonical(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        return "sha256:" + sha256(encoded.encode("utf-8")).hexdigest()


class EconomicLedger:
    """In-memory append-only projection with fail-closed identity semantics."""

    def __init__(self) -> None:
        self._transactions: list[LedgerTransaction] = []
        self._by_id: dict[str, LedgerTransaction] = {}
        self._reversed_by: dict[str, str] = {}
        self._balances: dict[tuple[str, str, str, str], Decimal] = {}

    @property
    def transactions(self) -> tuple[LedgerTransaction, ...]:
        return tuple(self._transactions)

    def append(self, transaction: LedgerTransaction) -> bool:
        if not isinstance(transaction, LedgerTransaction):
            raise TypeError("transaction must be LedgerTransaction")
        existing = self._by_id.get(transaction.transaction_id)
        if existing is not None:
            if existing != transaction:
                raise ValueError("transaction identity conflict")
            return False

        if transaction.reversal_of is not None:
            original = self._by_id.get(transaction.reversal_of)
            if original is None:
                raise ValueError("reversal target does not exist")
            if transaction.reversal_of in self._reversed_by:
                raise ValueError("transaction already has a reversal")
            self._validate_exact_reversal(original, transaction)

        for entry in transaction.entries:
            key = (
                transaction.environment,
                transaction.account_id,
                entry.book,
                entry.unit,
            )
            self._balances[key] = self._balances.get(key, Decimal("0")) + entry.amount

        self._transactions.append(transaction)
        self._by_id[transaction.transaction_id] = transaction
        if transaction.reversal_of is not None:
            self._reversed_by[transaction.reversal_of] = transaction.transaction_id
        return True

    @staticmethod
    def _validate_exact_reversal(
        original: LedgerTransaction,
        reversal: LedgerTransaction,
    ) -> None:
        if reversal.environment != original.environment:
            raise ValueError("reversal environment differs from original")
        if reversal.account_id != original.account_id:
            raise ValueError("reversal account differs from original")
        expected = sorted(
            (entry.book, entry.unit, -entry.amount)
            for entry in original.entries
        )
        actual = sorted(
            (entry.book, entry.unit, entry.amount)
            for entry in reversal.entries
        )
        if actual != expected:
            raise ValueError("reversal must exactly negate original postings")

    def reverse(
        self,
        *,
        transaction_id: str,
        reversal_id: str,
        event_id: str,
        evidence_digest: str,
    ) -> LedgerTransaction:
        original_id = _text(transaction_id, name="transaction_id")
        original = self._by_id.get(original_id)
        if original is None:
            raise ValueError("reversal target does not exist")
        reversal = LedgerTransaction(
            transaction_id=_text(reversal_id, name="reversal_id"),
            event_id=_text(event_id, name="event_id"),
            environment=original.environment,
            account_id=original.account_id,
            evidence_digest=evidence_digest,
            reversal_of=original.transaction_id,
            entries=tuple(
                LedgerEntry(
                    book=entry.book,
                    unit=entry.unit,
                    amount=-entry.amount,
                )
                for entry in original.entries
            ),
        )
        self.append(reversal)
        return reversal

    def balance(
        self,
        *,
        environment: str,
        account_id: str,
        book: str,
        unit: str,
    ) -> Decimal:
        key = (
            _text(environment, name="environment").upper(),
            _text(account_id, name="account_id"),
            _text(book, name="book"),
            _text(unit, name="unit"),
        )
        return self._balances.get(key, Decimal("0"))

    def projection(self) -> dict[tuple[str, str, str, str], Decimal]:
        return {
            key: value
            for key, value in sorted(self._balances.items())
            if value != Decimal("0")
        }

    def audit_digest(self) -> str:
        payload = {
            "schema_version": "1.0.0",
            "transactions": [
                {
                    "transaction_id": transaction.transaction_id,
                    "digest": transaction.digest(),
                }
                for transaction in self._transactions
            ],
        }
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        return "sha256:" + sha256(encoded.encode("utf-8")).hexdigest()


def transaction(
    *,
    transaction_id: str,
    event_id: str,
    environment: str,
    account_id: str,
    evidence_digest: str,
    entries: Iterable[LedgerEntry],
) -> LedgerTransaction:
    return LedgerTransaction(
        transaction_id=transaction_id,
        event_id=event_id,
        environment=environment,
        account_id=account_id,
        evidence_digest=evidence_digest,
        entries=tuple(entries),
    )
