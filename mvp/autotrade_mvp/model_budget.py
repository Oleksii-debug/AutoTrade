"""Hard-budget accounting for model and research calls.

This module is intentionally provider-neutral. It separates reserved, incurred,
and estimated-unbilled cost so the runtime can fail closed before exceeding a
user-approved ceiling.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation


def _money(value: Decimal | str | int | float) -> Decimal:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as error:
        raise ValueError("Budget values must be valid decimals") from error
    if not amount.is_finite() or amount < 0:
        raise ValueError("Budget values must be finite and non-negative")
    return amount


@dataclass(frozen=True)
class BudgetSnapshot:
    ceiling: Decimal
    reserved: Decimal
    incurred: Decimal
    estimated_unbilled: Decimal

    @property
    def committed(self) -> Decimal:
        return self.reserved + self.incurred + self.estimated_unbilled

    @property
    def available(self) -> Decimal:
        remaining = self.ceiling - self.committed
        return remaining if remaining > 0 else Decimal("0")


class ModelBudget:
    """Fail-closed budget ledger with explicit reservations and settlements."""

    def __init__(self, ceiling: Decimal | str | int | float):
        self._ceiling = _money(ceiling)
        self._reservations: dict[str, Decimal] = {}
        self._incurred = Decimal("0")
        self._estimated_unbilled = Decimal("0")

    def snapshot(self) -> BudgetSnapshot:
        return BudgetSnapshot(
            ceiling=self._ceiling,
            reserved=sum(self._reservations.values(), Decimal("0")),
            incurred=self._incurred,
            estimated_unbilled=self._estimated_unbilled,
        )

    def reserve(self, request_id: str, maximum_cost: Decimal | str | int | float) -> bool:
        if not isinstance(request_id, str) or not request_id.strip():
            raise ValueError("request_id must be a non-empty string")
        amount = _money(maximum_cost)

        previous = self._reservations.get(request_id)
        if previous is not None:
            if previous != amount:
                raise ValueError("request_id already reserved with a different amount")
            return False

        if amount > self.snapshot().available:
            raise ValueError("budget ceiling would be exceeded")
        self._reservations[request_id] = amount
        return True

    def cancel(self, request_id: str) -> Decimal:
        if request_id not in self._reservations:
            return Decimal("0")
        return self._reservations.pop(request_id)

    def settle(
        self,
        request_id: str,
        *,
        incurred: Decimal | str | int | float,
        estimated_unbilled: Decimal | str | int | float = 0,
    ) -> BudgetSnapshot:
        if request_id not in self._reservations:
            raise ValueError("request_id has no active reservation")
        actual = _money(incurred)
        estimate = _money(estimated_unbilled)
        reserved = self._reservations[request_id]

        if actual + estimate > reserved:
            raise ValueError("settlement exceeds reserved maximum cost")

        del self._reservations[request_id]
        self._incurred += actual
        self._estimated_unbilled += estimate
        if self.snapshot().committed > self._ceiling:
            raise RuntimeError("budget invariant violated")
        return self.snapshot()

    def confirm_billed(
        self,
        amount: Decimal | str | int | float,
        *,
        from_estimated_unbilled: Decimal | str | int | float = 0,
    ) -> BudgetSnapshot:
        billed = _money(amount)
        resolved = _money(from_estimated_unbilled)
        if resolved > self._estimated_unbilled:
            raise ValueError("cannot resolve more estimated cost than is outstanding")
        if billed > resolved:
            extra = billed - resolved
            if extra > self.snapshot().available:
                raise ValueError("confirmed billing would exceed budget ceiling")
        self._estimated_unbilled -= resolved
        self._incurred += billed
        return self.snapshot()
