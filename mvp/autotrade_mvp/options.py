"""Exact option expiry, exercise and adjusted-deliverable primitives.

No volatility model, Greeks or trade authority are implied. Pre-expiry value is
model-dependent and deliberately outside this deterministic settlement layer.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Iterable, Literal, Mapping

from .accounting import JournalTransaction, posting, validate_transaction


class OptionError(ValueError):
    pass


def _decimal(value: Decimal | str | int, name: str, *, positive: bool = False) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise OptionError(f"{name} must use exact decimal input")
    try:
        result = value if isinstance(value, Decimal) else Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise OptionError(f"{name} must be a finite decimal") from error
    if not result.is_finite():
        raise OptionError(f"{name} must be a finite decimal")
    if positive and result <= 0:
        raise OptionError(f"{name} must be positive")
    return result


def _text(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OptionError(f"{name} is required")
    return value.strip()


def _utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise OptionError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class DeliverableLeg:
    asset_id: str
    quantity_per_contract: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "asset_id", _text(self.asset_id, "asset_id"))
        object.__setattr__(
            self,
            "quantity_per_contract",
            _decimal(self.quantity_per_contract, "quantity_per_contract", positive=True),
        )


@dataclass(frozen=True)
class OptionContract:
    instrument: str
    right: Literal["CALL", "PUT"]
    strike: Decimal
    multiplier: Decimal
    settlement_currency: str
    settlement_method: Literal["CASH", "PHYSICAL"]
    exercise_style: str
    expiry: datetime
    exercise_cutoff: datetime
    deliverable: tuple[DeliverableLeg, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "instrument", _text(self.instrument, "instrument"))
        if self.right not in {"CALL", "PUT"}:
            raise OptionError("right must be CALL or PUT")
        object.__setattr__(self, "strike", _decimal(self.strike, "strike", positive=True))
        object.__setattr__(
            self,
            "multiplier",
            _decimal(self.multiplier, "multiplier", positive=True),
        )
        object.__setattr__(
            self,
            "settlement_currency",
            _text(self.settlement_currency, "settlement_currency"),
        )
        if self.settlement_method not in {"CASH", "PHYSICAL"}:
            raise OptionError("settlement_method must be CASH or PHYSICAL")
        style = _text(self.exercise_style, "exercise_style").upper()
        if style not in {"AMERICAN", "EUROPEAN"}:
            raise OptionError("exercise_style must be AMERICAN or EUROPEAN")
        object.__setattr__(self, "exercise_style", style)
        object.__setattr__(self, "expiry", _utc(self.expiry, "expiry"))
        object.__setattr__(
            self,
            "exercise_cutoff",
            _utc(self.exercise_cutoff, "exercise_cutoff"),
        )
        if self.exercise_cutoff > self.expiry:
            raise OptionError("exercise_cutoff cannot be after expiry")
        object.__setattr__(self, "deliverable", tuple(self.deliverable))
        if self.settlement_method == "PHYSICAL" and not self.deliverable:
            raise OptionError("physical option requires explicit adjusted deliverable")
        if self.settlement_method == "CASH" and self.deliverable:
            raise OptionError("cash-settled option cannot silently carry physical deliverables")


@dataclass(frozen=True)
class ExerciseObligation:
    asset_quantities: tuple[tuple[str, Decimal], ...]
    settlement_cash: Decimal
    settlement_currency: str


def intrinsic_value_per_unit(
    contract: OptionContract,
    underlying_price: Decimal | str | int,
) -> Decimal:
    price = _decimal(underlying_price, "underlying_price", positive=True)
    if contract.right == "CALL":
        return max(price - contract.strike, Decimal("0"))
    return max(contract.strike - price, Decimal("0"))


def expiration_cash_settlement(
    contract: OptionContract,
    *,
    signed_contracts: Decimal | str | int,
    underlying_price: Decimal | str | int,
) -> Decimal:
    if contract.settlement_method != "CASH":
        raise OptionError("expiration_cash_settlement requires a cash-settled option")
    contracts = _decimal(signed_contracts, "signed_contracts")
    intrinsic = intrinsic_value_per_unit(contract, underlying_price)
    return contracts * contract.multiplier * intrinsic


def expiration_pnl_after_premium(
    contract: OptionContract,
    *,
    signed_contracts: Decimal | str | int,
    premium_per_unit: Decimal | str | int,
    underlying_price: Decimal | str | int,
) -> Decimal:
    contracts = _decimal(signed_contracts, "signed_contracts")
    premium = _decimal(premium_per_unit, "premium_per_unit")
    if premium < 0:
        raise OptionError("premium_per_unit cannot be negative")
    intrinsic = intrinsic_value_per_unit(contract, underlying_price)
    return contracts * contract.multiplier * (intrinsic - premium)


def physical_exercise_obligation(
    contract: OptionContract,
    *,
    signed_contracts: Decimal | str | int,
) -> ExerciseObligation:
    """Map exercise/assignment into exact asset and strike-cash obligations.

    Positive signed_contracts means long-holder exercise. Negative means short
    assignment. CALL receives deliverable and pays strike for a long holder;
    PUT delivers the adjusted deliverable and receives strike.
    """

    if contract.settlement_method != "PHYSICAL":
        raise OptionError("physical exercise requires a physical-settled option")
    contracts = _decimal(signed_contracts, "signed_contracts")
    if contracts == 0:
        raise OptionError("signed_contracts must be non-zero")
    right_direction = Decimal("1") if contract.right == "CALL" else Decimal("-1")
    direction = contracts * right_direction
    assets = tuple(
        (leg.asset_id, direction * leg.quantity_per_contract)
        for leg in contract.deliverable
    )
    cash = -(direction * contract.strike * contract.multiplier)
    return ExerciseObligation(
        asset_quantities=assets,
        settlement_cash=cash,
        settlement_currency=contract.settlement_currency,
    )


def require_physical_resources(
    obligation: ExerciseObligation,
    *,
    asset_balances: Mapping[str, Decimal | str | int],
    cash_balance: Decimal | str | int,
) -> None:
    """Fail closed when a physical obligation needs resources not evidenced."""

    cash = _decimal(cash_balance, "cash_balance")
    if obligation.settlement_cash < 0 and cash < -obligation.settlement_cash:
        raise OptionError("insufficient evidenced cash for physical option obligation")
    for asset_id, quantity in obligation.asset_quantities:
        if quantity >= 0:
            continue
        if asset_id not in asset_balances:
            raise OptionError(f"missing evidenced balance for deliverable asset {asset_id}")
        available = _decimal(asset_balances[asset_id], f"asset balance {asset_id}")
        if available < -quantity:
            raise OptionError(f"insufficient evidenced balance for deliverable asset {asset_id}")


def exercise_gate(contract: OptionContract, at: datetime) -> str:
    point = _utc(at, "at")
    if point >= contract.expiry:
        return "EXPIRED"
    if point >= contract.exercise_cutoff:
        return "EXERCISE_WINDOW_CLOSED"
    if contract.exercise_style == "EUROPEAN":
        # The canonical contract does not encode a provider-specific European
        # exercise-open instant. Do not invent one or treat it as American.
        return "PROVIDER_EXERCISE_WINDOW_REQUIRED"
    return "OPEN"


def require_holder_exercise_open(contract: OptionContract, at: datetime) -> None:
    state = exercise_gate(contract, at)
    if state != "OPEN":
        raise OptionError(f"holder exercise is blocked: {state}")


def interim_multi_leg_reservation(
    leg_worst_case_losses: Iterable[Decimal | str | int],
    *,
    atomic_package_guaranteed: bool,
    package_worst_case_loss: Decimal | str | int | None = None,
) -> Decimal:
    """Reserve interim leg risk unless package atomicity is actually guaranteed."""

    losses = tuple(_decimal(value, "leg worst-case loss") for value in leg_worst_case_losses)
    if not losses:
        raise OptionError("at least one leg loss is required")
    if any(value < 0 for value in losses):
        raise OptionError("worst-case losses cannot be negative")
    if not atomic_package_guaranteed:
        if package_worst_case_loss is not None:
            raise OptionError("package loss cannot override non-atomic interim leg risk")
        return sum(losses, Decimal("0"))
    if package_worst_case_loss is None:
        raise OptionError("atomic package requires an explicitly evidenced package loss")
    package = _decimal(package_worst_case_loss, "package_worst_case_loss")
    if package < 0:
        raise OptionError("package_worst_case_loss cannot be negative")
    return package


def book_cash_option_settlement(
    *,
    transaction_id: str,
    cause_event_id: str,
    settlement_currency: str,
    amount: Decimal | str | int,
) -> JournalTransaction:
    value = _decimal(amount, "amount")
    if value == 0:
        raise OptionError("cash option settlement must be non-zero")
    currency = _text(settlement_currency, "settlement_currency")
    transaction = JournalTransaction(
        transaction_id=_text(transaction_id, "transaction_id"),
        cause_event_id=_text(cause_event_id, "cause_event_id"),
        postings=(
            posting(f"CASH:{currency}", currency, value),
            posting(f"OPTION_SETTLEMENT_PNL:{currency}", currency, -value),
        ),
    )
    validate_transaction(transaction)
    return transaction


def book_physical_option_settlement(
    *,
    transaction_id: str,
    cause_event_id: str,
    obligation: ExerciseObligation,
) -> JournalTransaction:
    postings = []
    for asset_id, quantity in obligation.asset_quantities:
        postings.extend(
            (
                posting(f"POSITION:{asset_id}", asset_id, quantity),
                posting(f"OPTION_DELIVERY_CLEARING:{asset_id}", asset_id, -quantity),
            )
        )
    if obligation.settlement_cash != 0:
        currency = obligation.settlement_currency
        postings.extend(
            (
                posting(f"CASH:{currency}", currency, obligation.settlement_cash),
                posting(
                    f"OPTION_DELIVERY_CLEARING:{currency}",
                    currency,
                    -obligation.settlement_cash,
                ),
            )
        )
    transaction = JournalTransaction(
        transaction_id=_text(transaction_id, "transaction_id"),
        cause_event_id=_text(cause_event_id, "cause_event_id"),
        postings=tuple(postings),
    )
    validate_transaction(transaction)
    return transaction
