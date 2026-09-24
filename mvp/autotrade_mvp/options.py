"""Exact option lifecycle, scenario-risk and model-bound sensitivity primitives.

No valuation model or trade authority is embedded here. Pre-expiry model values
must be supplied with explicit evidence time; deterministic settlement remains
separate from model-dependent Greeks and scenario analysis.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
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
    exercise_opens_at: datetime | None = None

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
        if self.exercise_opens_at is not None:
            opens = _utc(self.exercise_opens_at, "exercise_opens_at")
            if opens > self.exercise_cutoff:
                raise OptionError("exercise_opens_at cannot be after exercise_cutoff")
            object.__setattr__(self, "exercise_opens_at", opens)
        object.__setattr__(self, "deliverable", tuple(self.deliverable))
        if self.settlement_method == "PHYSICAL" and not self.deliverable:
            raise OptionError("physical option requires explicit adjusted deliverable")
        if self.settlement_method == "CASH" and self.deliverable:
            raise OptionError("cash-settled option cannot silently carry physical deliverables")


@dataclass(frozen=True)
class OptionGreekEstimate:
    """Model-bound finite-difference Greeks with explicit evidence time."""

    model_id: str
    observed_at: datetime
    delta: Decimal
    gamma: Decimal
    vega: Decimal | None = None
    theta_per_year: Decimal | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "model_id", _text(self.model_id, "model_id"))
        object.__setattr__(
            self,
            "observed_at",
            _utc(self.observed_at, "observed_at"),
        )
        object.__setattr__(self, "delta", _decimal(self.delta, "delta"))
        object.__setattr__(self, "gamma", _decimal(self.gamma, "gamma"))
        if self.vega is not None:
            object.__setattr__(self, "vega", _decimal(self.vega, "vega"))
        if self.theta_per_year is not None:
            object.__setattr__(
                self,
                "theta_per_year",
                _decimal(self.theta_per_year, "theta_per_year"),
            )

    def require_fresh(self, at: datetime, *, max_age: timedelta) -> None:
        point = _utc(at, "at")
        if not isinstance(max_age, timedelta) or max_age <= timedelta(0):
            raise OptionError("max_age must be positive")
        if point < self.observed_at:
            raise OptionError("Greek estimate cannot come from the future")
        if point - self.observed_at > max_age:
            raise OptionError("Greek estimate is stale")


@dataclass(frozen=True)
class FiniteDifferenceGrid:
    """Explicit same-model valuation grid used to derive local Greeks.

    These values are model outputs rather than authoritative cash or evidence
    of economic edge. The grid is only a deterministic sensitivity boundary.
    """

    model_id: str
    observed_at: datetime
    base_value: Decimal
    spot_down_value: Decimal
    spot_up_value: Decimal
    spot_step: Decimal
    vol_down_value: Decimal | None = None
    vol_up_value: Decimal | None = None
    vol_step: Decimal | None = None
    time_forward_value: Decimal | None = None
    time_step_years: Decimal | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "model_id", _text(self.model_id, "model_id"))
        object.__setattr__(
            self,
            "observed_at",
            _utc(self.observed_at, "observed_at"),
        )
        for name in ("base_value", "spot_down_value", "spot_up_value"):
            value = _decimal(getattr(self, name), name)
            if value < 0:
                raise OptionError(f"{name} cannot be negative")
            object.__setattr__(self, name, value)
        object.__setattr__(
            self,
            "spot_step",
            _decimal(self.spot_step, "spot_step", positive=True),
        )

        vol_values = (self.vol_down_value, self.vol_up_value, self.vol_step)
        if any(value is not None for value in vol_values):
            if not all(value is not None for value in vol_values):
                raise OptionError(
                    "vega grid requires vol_down_value, vol_up_value and vol_step together"
                )
            vol_down = _decimal(self.vol_down_value, "vol_down_value")
            vol_up = _decimal(self.vol_up_value, "vol_up_value")
            if vol_down < 0 or vol_up < 0:
                raise OptionError("volatility scenario option values cannot be negative")
            object.__setattr__(self, "vol_down_value", vol_down)
            object.__setattr__(self, "vol_up_value", vol_up)
            object.__setattr__(
                self,
                "vol_step",
                _decimal(self.vol_step, "vol_step", positive=True),
            )

        time_values = (self.time_forward_value, self.time_step_years)
        if any(value is not None for value in time_values):
            if not all(value is not None for value in time_values):
                raise OptionError(
                    "theta grid requires time_forward_value and time_step_years together"
                )
            forward = _decimal(self.time_forward_value, "time_forward_value")
            if forward < 0:
                raise OptionError("time_forward_value cannot be negative")
            object.__setattr__(self, "time_forward_value", forward)
            object.__setattr__(
                self,
                "time_step_years",
                _decimal(self.time_step_years, "time_step_years", positive=True),
            )


def finite_difference_greeks(grid: FiniteDifferenceGrid) -> OptionGreekEstimate:
    if not isinstance(grid, FiniteDifferenceGrid):
        raise TypeError("grid must be FiniteDifferenceGrid")
    two = Decimal("2")
    delta = (grid.spot_up_value - grid.spot_down_value) / (two * grid.spot_step)
    gamma = (
        grid.spot_up_value - two * grid.base_value + grid.spot_down_value
    ) / (grid.spot_step * grid.spot_step)
    vega = None
    if grid.vol_step is not None:
        vega = (grid.vol_up_value - grid.vol_down_value) / (two * grid.vol_step)
    theta = None
    if grid.time_step_years is not None:
        theta = (grid.time_forward_value - grid.base_value) / grid.time_step_years
    return OptionGreekEstimate(
        model_id=grid.model_id,
        observed_at=grid.observed_at,
        delta=delta,
        gamma=gamma,
        vega=vega,
        theta_per_year=theta,
    )


@dataclass(frozen=True)
class ExpiryScenarioLeg:
    contract: OptionContract
    signed_contracts: Decimal
    premium_per_unit: Decimal
    underlying_price: Decimal

    def __post_init__(self) -> None:
        if not isinstance(self.contract, OptionContract):
            raise TypeError("contract must be OptionContract")
        object.__setattr__(
            self,
            "signed_contracts",
            _decimal(self.signed_contracts, "signed_contracts"),
        )
        premium = _decimal(self.premium_per_unit, "premium_per_unit")
        if premium < 0:
            raise OptionError("premium_per_unit cannot be negative")
        object.__setattr__(self, "premium_per_unit", premium)
        object.__setattr__(
            self,
            "underlying_price",
            _decimal(self.underlying_price, "underlying_price", positive=True),
        )

    @property
    def pnl(self) -> Decimal:
        return expiration_pnl_after_premium(
            self.contract,
            signed_contracts=self.signed_contracts,
            premium_per_unit=self.premium_per_unit,
            underlying_price=self.underlying_price,
        )


def portfolio_expiration_scenario_pnl(
    legs: Iterable[ExpiryScenarioLeg],
) -> Decimal:
    scenario = tuple(legs)
    if not scenario:
        raise OptionError("at least one option scenario leg is required")
    if any(not isinstance(leg, ExpiryScenarioLeg) for leg in scenario):
        raise TypeError("all scenario legs must be ExpiryScenarioLeg")
    return sum((leg.pnl for leg in scenario), Decimal("0"))


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
    if contract.exercise_style == "EUROPEAN" and contract.exercise_opens_at is None:
        return "EXERCISE_SCHEDULE_UNKNOWN"
    if contract.exercise_opens_at is not None and point < contract.exercise_opens_at:
        return "EXERCISE_NOT_YET_OPEN"
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
