"""Exact option expiry, exercise and adjusted-deliverable primitives.

No volatility model, Greeks or trade authority are implied. Pre-expiry value is
model-dependent and deliberately outside this deterministic settlement layer.
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
    exercise_cash_per_contract: Decimal | None = None

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
        object.__setattr__(self, "exercise_style", _text(self.exercise_style, "exercise_style"))
        object.__setattr__(self, "expiry", _utc(self.expiry, "expiry"))
        object.__setattr__(
            self,
            "exercise_cutoff",
            _utc(self.exercise_cutoff, "exercise_cutoff"),
        )
        if self.exercise_cutoff > self.expiry:
            raise OptionError("exercise_cutoff cannot be after expiry")
        deliverable = tuple(self.deliverable)
        if any(not isinstance(leg, DeliverableLeg) for leg in deliverable):
            raise OptionError("deliverable must contain DeliverableLeg values")
        asset_ids = [leg.asset_id for leg in deliverable]
        if len(asset_ids) != len(set(asset_ids)):
            raise OptionError("deliverable asset_id values must be unique")
        object.__setattr__(self, "deliverable", deliverable)
        if self.settlement_method == "PHYSICAL":
            if not self.deliverable:
                raise OptionError("physical option requires explicit adjusted deliverable")
            if self.exercise_cash_per_contract is None:
                raise OptionError(
                    "physical option requires explicit exercise_cash_per_contract"
                )
            object.__setattr__(
                self,
                "exercise_cash_per_contract",
                _decimal(
                    self.exercise_cash_per_contract,
                    "exercise_cash_per_contract",
                    positive=True,
                ),
            )
        else:
            if self.deliverable:
                raise OptionError(
                    "cash-settled option cannot silently carry physical deliverables"
                )
            if self.exercise_cash_per_contract is not None:
                raise OptionError(
                    "cash-settled option cannot carry physical exercise cash"
                )


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
    if contract.exercise_cash_per_contract is None:
        raise OptionError("physical exercise cash is not evidenced")
    cash = -(direction * contract.exercise_cash_per_contract)
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


def _source_sha(value: str) -> str:
    text = _text(value, "source_sha")
    if len(text) not in {40, 64} or any(ch not in "0123456789abcdef" for ch in text):
        raise OptionError("source_sha must be a canonical lowercase Git digest")
    return text


def _input_digest(value: str) -> str:
    text = _text(value, "input_digest")
    if (
        not text.startswith("sha256:")
        or len(text) != 71
        or any(ch not in "0123456789abcdef" for ch in text[7:])
    ):
        raise OptionError("input_digest must be sha256:<64 lowercase hex>")
    return text


@dataclass(frozen=True)
class OptionScenarioResult:
    """One deterministic stress evaluation, not a probability forecast."""

    scenario_id: str
    underlying_price: Decimal
    implied_volatility: Decimal
    pnl: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "scenario_id", _text(self.scenario_id, "scenario_id"))
        object.__setattr__(
            self,
            "underlying_price",
            _decimal(self.underlying_price, "underlying_price", positive=True),
        )
        volatility = _decimal(self.implied_volatility, "implied_volatility")
        if volatility < 0:
            raise OptionError("implied_volatility cannot be negative")
        object.__setattr__(self, "implied_volatility", volatility)
        object.__setattr__(self, "pnl", _decimal(self.pnl, "scenario pnl"))


@dataclass(frozen=True)
class OptionRiskEvidence:
    """Versioned model evidence for Greeks plus explicit scenario stress.

    Greeks are estimates tied to a model and market timestamp. They do not grant
    trading authority and do not replace scenario stress.
    """

    instrument: str
    model_id: str
    model_version: str
    source_sha: str
    input_digest: str
    schema_version: int
    market_as_of: datetime
    calculated_at: datetime
    expires_at: datetime
    maximum_market_age: timedelta
    delta: Decimal
    gamma: Decimal
    vega: Decimal
    theta: Decimal
    rho: Decimal
    scenarios: tuple[OptionScenarioResult, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "instrument", _text(self.instrument, "instrument"))
        object.__setattr__(self, "model_id", _text(self.model_id, "model_id"))
        object.__setattr__(
            self, "model_version", _text(self.model_version, "model_version")
        )
        object.__setattr__(self, "source_sha", _source_sha(self.source_sha))
        object.__setattr__(self, "input_digest", _input_digest(self.input_digest))
        if type(self.schema_version) is not int or self.schema_version <= 0:
            raise OptionError("schema_version must be a positive integer")

        market = _utc(self.market_as_of, "market_as_of")
        calculated = _utc(self.calculated_at, "calculated_at")
        expires = _utc(self.expires_at, "expires_at")
        if market > calculated:
            raise OptionError("market_as_of cannot be after calculated_at")
        if expires <= calculated:
            raise OptionError("risk evidence must expire after calculation")
        if (
            not isinstance(self.maximum_market_age, timedelta)
            or self.maximum_market_age <= timedelta(0)
        ):
            raise OptionError("maximum_market_age must be a positive timedelta")
        if calculated - market > self.maximum_market_age:
            raise OptionError("market evidence is stale at calculation")
        object.__setattr__(self, "market_as_of", market)
        object.__setattr__(self, "calculated_at", calculated)
        object.__setattr__(self, "expires_at", expires)

        for field in ("delta", "gamma", "vega", "theta", "rho"):
            object.__setattr__(
                self, field, _decimal(getattr(self, field), field)
            )

        scenarios = tuple(self.scenarios)
        if not scenarios:
            raise OptionError("option risk evidence requires scenario stress")
        if any(not isinstance(item, OptionScenarioResult) for item in scenarios):
            raise OptionError("scenarios must contain OptionScenarioResult values")
        ids = [item.scenario_id for item in scenarios]
        if len(ids) != len(set(ids)):
            raise OptionError("scenario_id values must be unique")
        object.__setattr__(self, "scenarios", scenarios)

    @property
    def worst_scenario_loss(self) -> Decimal:
        return max(
            (max(-scenario.pnl, Decimal("0")) for scenario in self.scenarios),
            default=Decimal("0"),
        )


def require_current_option_risk(
    evidence: OptionRiskEvidence,
    *,
    instrument: str,
    at: datetime,
) -> None:
    if not isinstance(evidence, OptionRiskEvidence):
        raise TypeError("evidence must be OptionRiskEvidence")
    expected = _text(instrument, "instrument")
    if evidence.instrument != expected:
        raise OptionError("option risk evidence belongs to another instrument")
    point = _utc(at, "at")
    if point < evidence.calculated_at:
        raise OptionError("option risk evidence is from the future")
    if point >= evidence.expires_at:
        raise OptionError("option risk evidence is stale")
    if point - evidence.market_as_of > evidence.maximum_market_age:
        raise OptionError("option risk market evidence is stale")
