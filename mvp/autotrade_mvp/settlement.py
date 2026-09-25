"""Exact settled/unsettled cash projection for AutoTrade economic evidence.

This module is provider-neutral and performs no networking or order authorization.
It models contractual cash obligations separately from spendable settled cash so
future proceeds cannot be silently reused before their settlement date.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Iterable, Mapping


class SettlementConflict(ValueError):
    """Raised when immutable settlement identity or lifecycle invariants conflict."""


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


def _text(value: str, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    return value.strip()


@dataclass(frozen=True, slots=True)
class SettlementObligation:
    obligation_id: str
    cause_event_id: str
    currency: str
    amount: Decimal
    trade_date: date
    settlement_date: date
    component_id: str = "PRIMARY"

    def __post_init__(self) -> None:
        obligation_id = _text(self.obligation_id, name="obligation_id")
        cause_event_id = _text(self.cause_event_id, name="cause_event_id")
        component_id = _text(self.component_id, name="component_id").upper()
        currency = _text(self.currency, name="currency").upper()
        amount = _decimal(self.amount, name="amount")
        if amount == 0:
            raise ValueError("amount must be non-zero")
        if type(self.trade_date) is not date or type(self.settlement_date) is not date:
            raise TypeError("trade_date and settlement_date must be date values")
        if self.settlement_date < self.trade_date:
            raise ValueError("settlement_date cannot precede trade_date")
        object.__setattr__(self, "obligation_id", obligation_id)
        object.__setattr__(self, "cause_event_id", cause_event_id)
        object.__setattr__(self, "component_id", component_id)
        object.__setattr__(self, "currency", currency)
        object.__setattr__(self, "amount", amount)


@dataclass(frozen=True, slots=True)
class SettlementSnapshot:
    currency: str
    settled_cash: Decimal
    unsettled_receivable: Decimal
    unsettled_payable: Decimal

    @property
    def net_unsettled(self) -> Decimal:
        return self.unsettled_receivable - self.unsettled_payable

    @property
    def economic_cash(self) -> Decimal:
        return self.settled_cash + self.net_unsettled


class SettlementBook:
    """Immutable obligations plus exact spendable-cash projection.

    Positive obligations are receivables, negative obligations are payables.
    They remain outside settled cash until settled explicitly on or after the
    contractual settlement date. Identical retries are idempotent.
    """

    def __init__(
        self,
        *,
        settled_cash: dict[str, Decimal | str | int] | None = None,
        obligations: Iterable[SettlementObligation] = (),
        settled_obligation_evidence: Mapping[str, str] | None = None,
    ) -> None:
        self._settled_cash: dict[str, Decimal] = {}
        for currency, amount in (settled_cash or {}).items():
            unit = _text(currency, name="currency").upper()
            if unit in self._settled_cash:
                raise SettlementConflict(
                    "settled_cash contains duplicate normalized currency codes"
                )
            self._settled_cash[unit] = _decimal(amount, name="settled_cash")
        self._obligations: dict[str, SettlementObligation] = {}
        self._by_cause_component: dict[tuple[str, str], SettlementObligation] = {}
        self._settled_ids: set[str] = set()
        self._settlement_evidence: dict[str, str] = {}
        for obligation in obligations:
            self.add(obligation)
        for obligation_id, evidence_ref in (settled_obligation_evidence or {}).items():
            key = _text(obligation_id, name="settled_obligation_id")
            evidence = _text(evidence_ref, name="settlement_evidence_ref")
            if key not in self._obligations:
                raise SettlementConflict(
                    "settled obligation evidence cannot reference an unknown obligation"
                )
            self._settled_ids.add(key)
            self._settlement_evidence[key] = evidence

    @property
    def obligations(self) -> tuple[SettlementObligation, ...]:
        return tuple(self._obligations.values())

    def add(self, obligation: SettlementObligation) -> bool:
        if not isinstance(obligation, SettlementObligation):
            raise TypeError("obligation must be SettlementObligation")
        existing = self._obligations.get(obligation.obligation_id)
        if existing is not None:
            if existing != obligation:
                raise SettlementConflict(
                    "obligation_id already exists with different economic content"
                )
            return False
        cause_key = (obligation.cause_event_id, obligation.component_id)
        cause_existing = self._by_cause_component.get(cause_key)
        if cause_existing is not None:
            raise SettlementConflict(
                "cause_event_id and component_id were already represented by a different settlement obligation"
            )
        self._obligations[obligation.obligation_id] = obligation
        self._by_cause_component[cause_key] = obligation
        return True

    def is_settled(self, obligation_id: str) -> bool:
        return _text(obligation_id, name="obligation_id") in self._settled_ids

    def settle(
        self,
        obligation_id: str,
        *,
        as_of: date,
        settlement_evidence_ref: str,
    ) -> bool:
        key = _text(obligation_id, name="obligation_id")
        evidence = _text(settlement_evidence_ref, name="settlement_evidence_ref")
        obligation = self._obligations.get(key)
        if obligation is None:
            raise SettlementConflict("Cannot settle an unknown obligation")
        if key in self._settled_ids:
            if self._settlement_evidence[key] != evidence:
                raise SettlementConflict(
                    "settlement retry used different settlement evidence"
                )
            return False
        if as_of < obligation.settlement_date:
            raise SettlementConflict("Cannot settle before contractual settlement date")
        current = self._settled_cash.get(obligation.currency, Decimal("0"))
        self._settled_cash[obligation.currency] = current + obligation.amount
        self._settled_ids.add(key)
        self._settlement_evidence[key] = evidence
        return True

    def settle_due(
        self,
        *,
        as_of: date,
        settlement_evidence: Mapping[str, str],
    ) -> tuple[str, ...]:
        """Settle only due obligations that carry explicit provider/reconciliation evidence.

        Contractual due date alone never makes a receivable spendable.
        """
        if not isinstance(settlement_evidence, Mapping):
            raise TypeError("settlement_evidence must be a mapping")
        settled: list[str] = []
        for obligation in sorted(
            self._obligations.values(),
            key=lambda item: (item.settlement_date, item.obligation_id),
        ):
            evidence_ref = settlement_evidence.get(obligation.obligation_id)
            if (
                obligation.obligation_id not in self._settled_ids
                and obligation.settlement_date <= as_of
                and evidence_ref is not None
            ):
                self.settle(
                    obligation.obligation_id,
                    as_of=as_of,
                    settlement_evidence_ref=evidence_ref,
                )
                settled.append(obligation.obligation_id)
        return tuple(settled)

    def snapshot(self, currency: str) -> SettlementSnapshot:
        unit = _text(currency, name="currency").upper()
        receivable = Decimal("0")
        payable = Decimal("0")
        for obligation in self._obligations.values():
            if obligation.currency != unit or obligation.obligation_id in self._settled_ids:
                continue
            if obligation.amount > 0:
                receivable += obligation.amount
            else:
                payable += -obligation.amount
        return SettlementSnapshot(
            currency=unit,
            settled_cash=self._settled_cash.get(unit, Decimal("0")),
            unsettled_receivable=receivable,
            unsettled_payable=payable,
        )

    def available_to_spend(self, currency: str, *, reserve: Decimal | str | int = 0) -> Decimal:
        snapshot = self.snapshot(currency)
        locked = _decimal(reserve, name="reserve")
        if locked < 0:
            raise ValueError("reserve cannot be negative")
        # Contractual payables consume spendable cash as soon as the economic
        # obligation exists. Receivables remain unavailable until settlement.
        # reserve is an additional caller-owned hold and must not duplicate
        # the same settlement obligation.
        available = snapshot.settled_cash - snapshot.unsettled_payable - locked
        return max(Decimal("0"), available)


def equity_cash_obligation(
    *,
    obligation_id: str,
    cause_event_id: str,
    settlement_currency: str,
    side: str,
    quantity: Decimal | str | int,
    price: Decimal | str | int,
    fee: Decimal | str | int = 0,
    trade_date: date,
    settlement_date: date,
) -> SettlementObligation:
    unit = _text(settlement_currency, name="settlement_currency").upper()
    normalized_side = _text(side, name="side").upper()
    if normalized_side not in {"BUY", "SELL"}:
        raise ValueError("side must be BUY or SELL")
    qty = _decimal(quantity, name="quantity")
    px = _decimal(price, name="price")
    fee_amount = _decimal(fee, name="fee")
    if qty <= 0 or px <= 0 or fee_amount < 0:
        raise ValueError("quantity and price must be positive and fee non-negative")
    gross = qty * px
    amount = -(gross + fee_amount) if normalized_side == "BUY" else gross - fee_amount
    if amount == 0:
        raise ValueError("net settlement amount cannot be zero")
    return SettlementObligation(
        obligation_id=_text(obligation_id, name="obligation_id"),
        cause_event_id=_text(cause_event_id, name="cause_event_id"),
        currency=unit,
        amount=amount,
        trade_date=trade_date,
        settlement_date=settlement_date,
        component_id="PRINCIPAL_AND_SAME_CURRENCY_FEE",
    )
