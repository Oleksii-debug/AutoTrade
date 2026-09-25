"""Exact futures lifecycle primitives.

Provider-neutral financial math only. This module does not authorize orders,
choose leverage or connect to a venue.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from hashlib import sha256
import json
from typing import Literal

from .accounting import JournalTransaction, posting, validate_transaction
from .instruments import InstrumentVersion


class FuturesError(ValueError):
    pass


_ENVIRONMENTS = frozenset({"REPLAY", "SIMULATION", "PAPER", "LIVE"})


def _decimal(value: Decimal | str | int, name: str, *, positive: bool = False) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise FuturesError(f"{name} must use exact decimal input")
    try:
        result = value if isinstance(value, Decimal) else Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise FuturesError(f"{name} must be a finite decimal") from error
    if not result.is_finite():
        raise FuturesError(f"{name} must be a finite decimal")
    if positive and result <= 0:
        raise FuturesError(f"{name} must be positive")
    return result


def _text(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FuturesError(f"{name} is required")
    return value.strip()


def _utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise FuturesError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _fraction(value: Decimal) -> Fraction:
    sign, digits, exponent = value.as_tuple()
    integer = 0
    for digit in digits:
        integer = integer * 10 + digit
    if sign:
        integer = -integer
    if exponent >= 0:
        return Fraction(integer * (10**exponent), 1)
    return Fraction(integer, 10 ** (-exponent))


def _decimal_identity(value: Decimal) -> str:
    normalized = _decimal(value, "decimal identity")
    if normalized == 0:
        return "0"
    text = format(normalized.normalize(), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


@dataclass(frozen=True)
class FuturesContract:
    instrument: str
    payoff: Literal["LINEAR", "INVERSE"]
    multiplier: Decimal
    quote_currency: str
    settlement_currency: str
    last_trade_at: datetime
    delivery_cutoff: datetime
    expiry: datetime
    settlement_method: Literal["CASH", "PHYSICAL"]
    price_base_currency: str | None = None
    canonical_instrument: InstrumentVersion | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "instrument", _text(self.instrument, "instrument"))
        if self.payoff not in {"LINEAR", "INVERSE"}:
            raise FuturesError("payoff must be LINEAR or INVERSE")
        object.__setattr__(self, "multiplier", _decimal(self.multiplier, "multiplier", positive=True))
        object.__setattr__(self, "quote_currency", _text(self.quote_currency, "quote_currency"))
        object.__setattr__(
            self,
            "settlement_currency",
            _text(self.settlement_currency, "settlement_currency"),
        )
        for name in ("last_trade_at", "delivery_cutoff", "expiry"):
            object.__setattr__(self, name, _utc(getattr(self, name), name))
        if not self.last_trade_at <= self.expiry:
            raise FuturesError("last_trade_at cannot be after expiry")
        if not self.delivery_cutoff <= self.expiry:
            raise FuturesError("delivery_cutoff cannot be after expiry")
        if self.settlement_method not in {"CASH", "PHYSICAL"}:
            raise FuturesError("settlement_method must be CASH or PHYSICAL")
        if (
            self.settlement_method == "PHYSICAL"
            and self.delivery_cutoff > self.last_trade_at
        ):
            raise FuturesError(
                "physical delivery_cutoff cannot be after last_trade_at"
            )
        if self.price_base_currency is not None:
            object.__setattr__(
                self,
                "price_base_currency",
                _text(self.price_base_currency, "price_base_currency"),
            )
        if self.payoff == "INVERSE":
            if self.price_base_currency is None:
                raise FuturesError(
                    "inverse futures require explicit price_base_currency qualification"
                )
            if self.settlement_currency != self.price_base_currency:
                raise FuturesError(
                    "inverse settlement_currency must match the base currency produced by face/price"
                )

        if self.canonical_instrument is not None:
            version = self.canonical_instrument
            if not isinstance(version, InstrumentVersion):
                raise FuturesError("canonical_instrument must be an InstrumentVersion")
            if version.asset_class != "FUTURE":
                raise FuturesError("canonical instrument must have FUTURE asset_class")
            canonical_key = f"{version.instrument_id}@{version.version}"
            if self.instrument != canonical_key:
                raise FuturesError("futures contract identity must match canonical InstrumentVersion")
            if version.payoff != self.payoff:
                raise FuturesError("futures payoff conflicts with canonical InstrumentVersion")
            if version.contract_multiplier != self.multiplier:
                raise FuturesError("futures multiplier conflicts with canonical InstrumentVersion")
            if version.quote_currency != self.quote_currency:
                raise FuturesError("futures quote currency conflicts with canonical InstrumentVersion")
            if version.settlement_currency != self.settlement_currency:
                raise FuturesError(
                    "futures settlement currency conflicts with canonical InstrumentVersion"
                )
            if version.expiry != self.expiry:
                raise FuturesError("futures expiry conflicts with canonical InstrumentVersion")
            if version.last_trade_at != self.last_trade_at:
                raise FuturesError(
                    "futures last_trade_at conflicts with canonical InstrumentVersion"
                )
            if version.delivery_cutoff != self.delivery_cutoff:
                raise FuturesError(
                    "futures delivery_cutoff conflicts with canonical InstrumentVersion"
                )
            if version.settlement_method != self.settlement_method:
                raise FuturesError(
                    "futures settlement_method conflicts with canonical InstrumentVersion"
                )
            if self.payoff == "INVERSE" and version.base_currency != self.price_base_currency:
                raise FuturesError(
                    "inverse price base currency conflicts with canonical InstrumentVersion"
                )

    @classmethod
    def from_instrument_version(cls, version: InstrumentVersion) -> "FuturesContract":
        if not isinstance(version, InstrumentVersion):
            raise FuturesError("canonical InstrumentVersion is required")
        if version.asset_class != "FUTURE":
            raise FuturesError("canonical instrument must have FUTURE asset_class")
        if version.payoff not in {"LINEAR", "INVERSE"}:
            raise FuturesError("canonical future payoff must be LINEAR or INVERSE")
        if (
            version.expiry is None
            or version.last_trade_at is None
            or version.delivery_cutoff is None
            or version.settlement_method not in {"CASH", "PHYSICAL"}
        ):
            raise FuturesError(
                "canonical future requires expiry, last_trade_at, delivery_cutoff and settlement_method"
            )
        return cls(
            instrument=f"{version.instrument_id}@{version.version}",
            payoff=version.payoff,
            multiplier=version.contract_multiplier,
            quote_currency=version.quote_currency,
            settlement_currency=version.settlement_currency,
            last_trade_at=version.last_trade_at,
            delivery_cutoff=version.delivery_cutoff,
            expiry=version.expiry,
            settlement_method=version.settlement_method,
            price_base_currency=(
                version.base_currency if version.payoff == "INVERSE" else None
            ),
            canonical_instrument=version,
        )


@dataclass(frozen=True)
class FuturesSettlementScope:
    """Immutable authority/source scope for one settlement stream."""

    source_id: str
    provider_id: str | None = None
    account_id: str | None = None
    environment: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_id", _text(self.source_id, "source_id"))
        provider_fields = (self.provider_id, self.account_id, self.environment)
        if any(value is not None for value in provider_fields):
            if not all(value is not None for value in provider_fields):
                raise FuturesError(
                    "provider settlement scope requires provider_id, account_id and environment together"
                )
            object.__setattr__(
                self, "provider_id", _text(self.provider_id, "provider_id")
            )
            object.__setattr__(self, "account_id", _text(self.account_id, "account_id"))
            environment = _text(self.environment, "environment").upper()
            if environment not in _ENVIRONMENTS:
                raise FuturesError(
                    "environment must be REPLAY, SIMULATION, PAPER, or LIVE"
                )
            object.__setattr__(self, "environment", environment)


@dataclass(frozen=True)
class FuturesSettlementEvidence:
    """Immutable observation of one stable futures settlement period."""

    settlement_id: str
    observation_id: str
    instrument_id: str
    instrument_version: int
    scope: FuturesSettlementScope
    effective_at: datetime
    sequence: int
    revision: int
    settlement_price: Decimal
    price_currency: str
    settlement_currency: str
    evidence_ref: str | None = None
    supersedes_observation_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "settlement_id", _text(self.settlement_id, "settlement_id")
        )
        object.__setattr__(
            self, "observation_id", _text(self.observation_id, "observation_id")
        )
        object.__setattr__(
            self, "instrument_id", _text(self.instrument_id, "instrument_id")
        )
        if (
            isinstance(self.instrument_version, bool)
            or not isinstance(self.instrument_version, int)
            or self.instrument_version < 1
        ):
            raise FuturesError("instrument_version must be a positive integer")
        if not isinstance(self.scope, FuturesSettlementScope):
            raise FuturesError("settlement scope is required")
        object.__setattr__(
            self, "effective_at", _utc(self.effective_at, "effective_at")
        )
        for name in ("sequence", "revision"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise FuturesError(f"{name} must be a non-negative integer")
        if self.supersedes_observation_id is not None:
            object.__setattr__(
                self,
                "supersedes_observation_id",
                _text(
                    self.supersedes_observation_id,
                    "supersedes_observation_id",
                ),
            )
        if self.revision == 0 and self.supersedes_observation_id is not None:
            raise FuturesError("initial settlement revision cannot supersede an observation")
        if self.revision > 0 and self.supersedes_observation_id is None:
            raise FuturesError(
                "corrected settlement revision requires supersedes_observation_id"
            )
        object.__setattr__(
            self,
            "settlement_price",
            _decimal(self.settlement_price, "settlement_price", positive=True),
        )
        object.__setattr__(
            self, "price_currency", _text(self.price_currency, "price_currency")
        )
        object.__setattr__(
            self,
            "settlement_currency",
            _text(self.settlement_currency, "settlement_currency"),
        )

    @property
    def order_key(self) -> tuple[datetime, int]:
        return self.effective_at, self.sequence


def _require_settlement_contract(
    contract: FuturesContract,
    scope: FuturesSettlementScope,
    evidence: FuturesSettlementEvidence,
) -> None:
    if not isinstance(evidence, FuturesSettlementEvidence):
        raise FuturesError("immutable FuturesSettlementEvidence is required")
    version = contract.canonical_instrument
    if version is None:
        raise FuturesError(
            "settlement economics require canonical InstrumentVersion binding"
        )
    if (
        evidence.instrument_id != version.instrument_id
        or evidence.instrument_version != version.version
    ):
        raise FuturesError("settlement instrument/version does not match state")
    if evidence.scope != scope:
        raise FuturesError("settlement provider/account/environment/source scope mismatch")
    if scope.provider_id is not None and scope.provider_id != version.provider_id:
        raise FuturesError("settlement provider does not match canonical InstrumentVersion")
    if not version.contains(evidence.effective_at):
        raise FuturesError("settlement effective time is outside InstrumentVersion")
    if evidence.price_currency != contract.quote_currency:
        raise FuturesError("settlement price currency does not match contract")
    if evidence.settlement_currency != contract.settlement_currency:
        raise FuturesError("settlement currency does not match contract")


def _validate_settlement_history(
    contract: FuturesContract,
    scope: FuturesSettlementScope,
    history: tuple[FuturesSettlementEvidence, ...],
    last_price: Decimal,
) -> None:
    if not isinstance(scope, FuturesSettlementScope):
        raise FuturesError("settlement_scope is required")
    if not isinstance(history, tuple):
        raise FuturesError("settlement_history must be an immutable tuple")
    seen_observations: set[str] = set()
    latest_by_period: dict[str, FuturesSettlementEvidence] = {}
    previous: FuturesSettlementEvidence | None = None
    for evidence in history:
        _require_settlement_contract(contract, scope, evidence)
        if evidence.observation_id in seen_observations:
            raise FuturesError("settlement history contains duplicate observation identity")
        prior_period = latest_by_period.get(evidence.settlement_id)
        if prior_period is None:
            if evidence.revision != 0 or evidence.supersedes_observation_id is not None:
                raise FuturesError("new settlement period must start at revision 0")
            if previous is not None and evidence.order_key <= previous.order_key:
                raise FuturesError("settlement history is not strictly ordered")
        else:
            if previous is not prior_period:
                raise FuturesError(
                    "correction of a non-latest settlement period is unsupported"
                )
            if evidence.order_key != prior_period.order_key:
                raise FuturesError("settlement correction cannot change period ordering")
            if evidence.revision != prior_period.revision + 1:
                raise FuturesError("settlement correction revision must be consecutive")
            if evidence.supersedes_observation_id != prior_period.observation_id:
                raise FuturesError("settlement correction must supersede latest observation")
        seen_observations.add(evidence.observation_id)
        latest_by_period[evidence.settlement_id] = evidence
        previous = evidence
    if history and history[-1].settlement_price != last_price:
        raise FuturesError("last settlement price does not match settlement history")


def _settlement_duplicate_or_require_new(
    *,
    contract: FuturesContract,
    scope: FuturesSettlementScope,
    history: tuple[FuturesSettlementEvidence, ...],
    evidence: FuturesSettlementEvidence,
) -> Literal["DUPLICATE", "NEW", "CORRECTION"]:
    _require_settlement_contract(contract, scope, evidence)
    latest_same_period: FuturesSettlementEvidence | None = None
    matching_observation: FuturesSettlementEvidence | None = None
    for accepted in history:
        if accepted.settlement_id == evidence.settlement_id:
            latest_same_period = accepted
            if accepted.observation_id == evidence.observation_id:
                matching_observation = accepted

    if matching_observation is not None:
        if matching_observation != evidence:
            raise FuturesError(
                "settlement observation identity conflicts with accepted content"
            )
        if latest_same_period is matching_observation:
            return "DUPLICATE"
        raise FuturesError(
            "settlement revision is stale or conflicts with accepted economics"
        )

    if latest_same_period is not None:
        if evidence.revision <= latest_same_period.revision:
            raise FuturesError(
                "settlement revision is stale or conflicts with accepted economics"
            )
        if history[-1] is not latest_same_period:
            raise FuturesError(
                "correction of a non-latest settlement period is unsupported"
            )
        if evidence.order_key != latest_same_period.order_key:
            raise FuturesError("settlement correction cannot change period ordering")
        if evidence.revision != latest_same_period.revision + 1:
            raise FuturesError("settlement correction revision must be consecutive")
        if evidence.supersedes_observation_id != latest_same_period.observation_id:
            raise FuturesError("settlement correction must supersede latest observation")
        return "CORRECTION"

    if evidence.revision != 0 or evidence.supersedes_observation_id is not None:
        raise FuturesError("new settlement period must start at revision 0")
    if history:
        last = history[-1]
        if evidence.order_key <= last.order_key:
            raise FuturesError(
                "out-of-order settlement cannot rewind variation-margin state"
            )
    return "NEW"


def settlement_identity_digest(evidence: FuturesSettlementEvidence) -> str:
    if not isinstance(evidence, FuturesSettlementEvidence):
        raise FuturesError("immutable FuturesSettlementEvidence is required")
    material = {
        "settlement_id": evidence.settlement_id,
        "observation_id": evidence.observation_id,
        "supersedes_observation_id": evidence.supersedes_observation_id,
        "instrument_id": evidence.instrument_id,
        "instrument_version": evidence.instrument_version,
        "source_id": evidence.scope.source_id,
        "provider_id": evidence.scope.provider_id,
        "account_id": evidence.scope.account_id,
        "environment": evidence.scope.environment,
        "effective_at": evidence.effective_at.isoformat(),
        "sequence": evidence.sequence,
        "revision": evidence.revision,
        "settlement_price": _decimal_identity(evidence.settlement_price),
        "price_currency": evidence.price_currency,
        "settlement_currency": evidence.settlement_currency,
        "evidence_ref": evidence.evidence_ref,
    }
    encoded = json.dumps(
        material,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + sha256(encoded).hexdigest()


@dataclass(frozen=True)
class InverseVariationMarginState:
    """Exact inverse-futures state between explicit settlement boundaries."""

    contract: FuturesContract
    signed_contracts: Decimal
    last_settlement_price: Decimal
    settlement_scope: FuturesSettlementScope
    cumulative_variation_margin: Fraction = Fraction(0, 1)
    settlement_history: tuple[FuturesSettlementEvidence, ...] = ()

    def __post_init__(self) -> None:
        if self.contract.payoff != "INVERSE":
            raise FuturesError("inverse variation-margin state requires INVERSE futures")
        contracts = _decimal(self.signed_contracts, "signed_contracts")
        if contracts == 0:
            raise FuturesError("signed_contracts must be non-zero")
        object.__setattr__(self, "signed_contracts", contracts)
        object.__setattr__(
            self,
            "last_settlement_price",
            _decimal(self.last_settlement_price, "last_settlement_price", positive=True),
        )
        if not isinstance(self.cumulative_variation_margin, Fraction):
            raise FuturesError("cumulative inverse variation margin must be an exact Fraction")
        _validate_settlement_history(
            self.contract,
            self.settlement_scope,
            self.settlement_history,
            self.last_settlement_price,
        )


@dataclass(frozen=True)
class VariationMarginState:
    contract: FuturesContract
    signed_contracts: Decimal
    last_settlement_price: Decimal
    settlement_scope: FuturesSettlementScope
    cumulative_variation_margin: Decimal = Decimal("0")
    settlement_history: tuple[FuturesSettlementEvidence, ...] = ()

    def __post_init__(self) -> None:
        contracts = _decimal(self.signed_contracts, "signed_contracts")
        if contracts == 0:
            raise FuturesError("signed_contracts must be non-zero")
        object.__setattr__(self, "signed_contracts", contracts)
        object.__setattr__(
            self,
            "last_settlement_price",
            _decimal(self.last_settlement_price, "last_settlement_price", positive=True),
        )
        object.__setattr__(
            self,
            "cumulative_variation_margin",
            _decimal(self.cumulative_variation_margin, "cumulative_variation_margin"),
        )
        if self.contract.payoff != "LINEAR":
            raise FuturesError("decimal variation-margin state currently supports LINEAR futures only")
        _validate_settlement_history(
            self.contract,
            self.settlement_scope,
            self.settlement_history,
            self.last_settlement_price,
        )


def linear_futures_pnl(
    *,
    signed_contracts: Decimal | str | int,
    multiplier: Decimal | str | int,
    entry_price: Decimal | str | int,
    exit_price: Decimal | str | int,
) -> Decimal:
    contracts = _decimal(signed_contracts, "signed_contracts")
    contract_multiplier = _decimal(multiplier, "multiplier", positive=True)
    entry = _decimal(entry_price, "entry_price", positive=True)
    exit_value = _decimal(exit_price, "exit_price", positive=True)
    return contracts * contract_multiplier * (exit_value - entry)


def inverse_futures_pnl_exact(
    *,
    signed_contracts: Decimal | str | int,
    contract_quote_value: Decimal | str | int,
    entry_price: Decimal | str | int,
    exit_price: Decimal | str | int,
) -> Fraction:
    """Return exact price-base-currency P&L as a rational number.

    For a contract whose multiplier is a quote-currency face value:
    contracts * face * (1 / entry - 1 / exit).
    """

    contracts = _decimal(signed_contracts, "signed_contracts")
    face = _decimal(contract_quote_value, "contract_quote_value", positive=True)
    entry = _decimal(entry_price, "entry_price", positive=True)
    exit_value = _decimal(exit_price, "exit_price", positive=True)
    return (
        _fraction(contracts)
        * _fraction(face)
        * (Fraction(1, 1) / _fraction(entry) - Fraction(1, 1) / _fraction(exit_value))
    )


def settle_fraction(
    value: Fraction,
    *,
    quantum: Decimal | str,
    rounding: Literal["HALF_EVEN", "DOWN"] = "HALF_EVEN",
) -> Decimal:
    """Round an exact rational to an exact multiple of the settlement quantum."""

    if not isinstance(value, Fraction):
        raise FuturesError("value must be an exact Fraction")
    step = _decimal(quantum, "quantum", positive=True)
    if rounding not in {"HALF_EVEN", "DOWN"}:
        raise FuturesError("unsupported rounding policy")

    units = value / _fraction(step)
    sign = -1 if units < 0 else 1
    numerator = abs(units.numerator)
    denominator = units.denominator
    whole, remainder = divmod(numerator, denominator)

    if rounding == "HALF_EVEN":
        doubled = remainder * 2
        if doubled > denominator or (doubled == denominator and whole % 2 == 1):
            whole += 1

    signed_units = whole * sign
    return step * Decimal(signed_units)


def apply_variation_margin(
    state: VariationMarginState,
    settlement: FuturesSettlementEvidence,
) -> tuple[VariationMarginState, Decimal]:
    """Apply one identity-bound linear settlement exactly once."""

    if not isinstance(state, VariationMarginState):
        raise FuturesError("linear variation-margin state is required")
    disposition = _settlement_duplicate_or_require_new(
        contract=state.contract,
        scope=state.settlement_scope,
        history=state.settlement_history,
        evidence=settlement,
    )
    if disposition == "DUPLICATE":
        return state, Decimal("0")
    amount = linear_futures_pnl(
        signed_contracts=state.signed_contracts,
        multiplier=state.contract.multiplier,
        entry_price=state.last_settlement_price,
        exit_price=settlement.settlement_price,
    )
    return (
        replace(
            state,
            last_settlement_price=settlement.settlement_price,
            cumulative_variation_margin=state.cumulative_variation_margin + amount,
            settlement_history=state.settlement_history + (settlement,),
        ),
        amount,
    )


def apply_inverse_variation_margin(
    state: InverseVariationMarginState,
    settlement: FuturesSettlementEvidence,
) -> tuple[InverseVariationMarginState, Fraction]:
    """Apply one identity-bound inverse settlement without premature rounding."""

    if not isinstance(state, InverseVariationMarginState):
        raise FuturesError("inverse variation-margin state is required")
    disposition = _settlement_duplicate_or_require_new(
        contract=state.contract,
        scope=state.settlement_scope,
        history=state.settlement_history,
        evidence=settlement,
    )
    if disposition == "DUPLICATE":
        return state, Fraction(0, 1)
    amount = inverse_futures_pnl_exact(
        signed_contracts=state.signed_contracts,
        contract_quote_value=state.contract.multiplier,
        entry_price=state.last_settlement_price,
        exit_price=settlement.settlement_price,
    )
    return (
        replace(
            state,
            last_settlement_price=settlement.settlement_price,
            cumulative_variation_margin=state.cumulative_variation_margin + amount,
            settlement_history=state.settlement_history + (settlement,),
        ),
        amount,
    )


def replay_variation_margin(
    opening_state: VariationMarginState,
    settlements: tuple[FuturesSettlementEvidence, ...],
) -> VariationMarginState:
    """Deterministically rebuild linear VM state from immutable settlements."""

    if opening_state.settlement_history:
        raise FuturesError("replay opening state must have empty settlement history")
    state = opening_state
    for evidence in settlements:
        state, _ = apply_variation_margin(state, evidence)
    return state


def replay_inverse_variation_margin(
    opening_state: InverseVariationMarginState,
    settlements: tuple[FuturesSettlementEvidence, ...],
) -> InverseVariationMarginState:
    """Deterministically rebuild inverse VM state from immutable settlements."""

    if opening_state.settlement_history:
        raise FuturesError("replay opening state must have empty settlement history")
    state = opening_state
    for evidence in settlements:
        state, _ = apply_inverse_variation_margin(state, evidence)
    return state


def settle_and_book_inverse_variation_margin(
    *,
    settlement: FuturesSettlementEvidence,
    contract: FuturesContract,
    exact_amount: Fraction,
    settlement_quantum: Decimal | str,
    rounding: Literal["HALF_EVEN", "DOWN"] = "HALF_EVEN",
) -> tuple[Decimal, JournalTransaction | None]:
    """Round only at the explicit settlement boundary and book exact currency truth.

    A sub-quantum amount that rounds to zero creates no artificial zero posting.
    The caller retains the exact rational amount as evidence; the returned Decimal
    is the provider-facing cash settlement amount.
    """

    if not isinstance(contract, FuturesContract) or contract.payoff != "INVERSE":
        raise FuturesError("inverse settlement booking requires an INVERSE futures contract")
    if not isinstance(settlement, FuturesSettlementEvidence):
        raise FuturesError("immutable FuturesSettlementEvidence is required")
    _require_settlement_contract(contract, settlement.scope, settlement)
    if settlement.price_currency != contract.quote_currency:
        raise FuturesError("settlement price currency does not match contract")
    if settlement.settlement_currency != contract.settlement_currency:
        raise FuturesError("settlement currency does not match contract")
    settled = settle_fraction(
        exact_amount,
        quantum=settlement_quantum,
        rounding=rounding,
    )
    if settled == 0:
        return settled, None
    return (
        settled,
        book_variation_margin(
            settlement=settlement,
            amount=settled,
        ),
    )


def unrealized_inverse_after_variation(
    state: InverseVariationMarginState,
    mark_price: Decimal | str | int,
) -> Fraction:
    """Return exact inverse mark P&L from the last settled price."""

    return inverse_futures_pnl_exact(
        signed_contracts=state.signed_contracts,
        contract_quote_value=state.contract.multiplier,
        entry_price=state.last_settlement_price,
        exit_price=mark_price,
    )


def unrealized_after_variation(
    state: VariationMarginState,
    mark_price: Decimal | str | int,
) -> Decimal:
    """Mark only from the last settled price, avoiding double counting."""

    return linear_futures_pnl(
        signed_contracts=state.signed_contracts,
        multiplier=state.contract.multiplier,
        entry_price=state.last_settlement_price,
        exit_price=mark_price,
    )


def book_variation_margin(
    *,
    settlement: FuturesSettlementEvidence,
    amount: Decimal | str | int,
) -> JournalTransaction:
    """Create one deterministic journal identity from accepted settlement evidence."""

    if not isinstance(settlement, FuturesSettlementEvidence):
        raise FuturesError("immutable FuturesSettlementEvidence is required")
    value = _decimal(amount, "amount")
    if value == 0:
        raise FuturesError("variation margin posting must be non-zero")
    currency = settlement.settlement_currency
    digest = settlement_identity_digest(settlement)
    transaction = JournalTransaction(
        transaction_id=f"FUTURES_VM:{digest}",
        cause_event_id=f"FUTURES_SETTLEMENT:{digest}",
        postings=(
            posting(f"CASH:{currency}", currency, value),
            posting(f"FUTURES_VARIATION_PNL:{currency}", currency, -value),
        ),
    )
    validate_transaction(transaction)
    return transaction


def lifecycle_gate(
    contract: FuturesContract,
    at: datetime,
    *,
    physical_delivery_authorized: bool = False,
) -> str:
    """Return a conservative lifecycle state for holding/trading the contract."""

    point = _utc(at, "at")
    if type(physical_delivery_authorized) is not bool:
        raise FuturesError("physical_delivery_authorized must be boolean")
    if point >= contract.expiry:
        return "EXPIRED"
    if point >= contract.last_trade_at:
        return "TRADING_ENDED"
    if (
        contract.settlement_method == "PHYSICAL"
        and not physical_delivery_authorized
        and point >= contract.delivery_cutoff
    ):
        return "DELIVERY_BLOCKED"
    return "OPEN"


def require_open_for_new_exposure(
    contract: FuturesContract,
    at: datetime,
    *,
    physical_delivery_authorized: bool = False,
) -> None:
    state = lifecycle_gate(
        contract,
        at,
        physical_delivery_authorized=physical_delivery_authorized,
    )
    if state != "OPEN":
        raise FuturesError(f"new futures exposure is blocked: {state}")
