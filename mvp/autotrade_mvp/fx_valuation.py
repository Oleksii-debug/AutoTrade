"""Exact, freshness-bound FX valuation primitives.

The module values already-known cash or collateral balances. It has no network
or trading authority and deliberately supports only direct/inverse one-hop
quotes. Missing, stale, future or unroundable evidence produces an uncertain
valuation instead of inventing a conversion rate.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from hashlib import sha256
import re
from typing import Mapping

from .exact_decimal import (
    ExactDecimalError,
    as_fraction as _exact_as_fraction,
    canonical_decimal_text as _exact_canonical_decimal_text,
    exact_sum as _exact_sum,
    round_fraction_to_quantum as _exact_round_fraction_to_quantum,
    terminating_decimal as _exact_terminating_decimal,
)


class FxValuationError(ValueError):
    pass


def _translate_exact(operation, *args, name: str):
    try:
        return operation(*args)
    except ExactDecimalError as error:
        raise FxValuationError(
            f"{name} exceeds the supported exact-decimal resource envelope"
        ) from error


def _as_fraction(value: Decimal, *, name: str) -> Fraction:
    return _translate_exact(_exact_as_fraction, value, name=name)


def _terminating_decimal(value: Fraction, *, name: str) -> Decimal:
    return _translate_exact(_exact_terminating_decimal, value, name=name)


def _round_fraction_to_quantum(
    value: Fraction,
    quantum: Decimal,
    *,
    name: str,
) -> Decimal:
    try:
        return _exact_round_fraction_to_quantum(value, quantum, mode="FLOOR")
    except ExactDecimalError as error:
        raise FxValuationError(
            f"{name} exceeds the supported exact-decimal resource envelope"
        ) from error


def _canonical_decimal_text(value: Decimal, *, name: str) -> str:
    return _translate_exact(_exact_canonical_decimal_text, value, name=name)


def _decimal(value, name: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise FxValuationError(f"{name} must use exact decimal input")
    try:
        result = value if isinstance(value, Decimal) else Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise FxValuationError(f"{name} must be a finite decimal") from error
    if not result.is_finite():
        raise FxValuationError(f"{name} must be a finite decimal")
    if result == 0:
        return Decimal("0")
    _as_fraction(result, name=name)
    return result


def _text(value, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FxValuationError(f"{name} is required")
    return value.strip()


def _currency(value, name: str) -> str:
    currency = _text(value, name).upper()
    if re.fullmatch(r"[A-Z0-9]{2,12}", currency) is None:
        raise FxValuationError(f"{name} must be a canonical currency/asset code")
    return currency


def _instant(value, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise FxValuationError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _digest(value, name: str) -> str:
    digest = _text(value, name)
    if re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None:
        raise FxValuationError(f"{name} must be a canonical SHA-256 digest")
    return digest


def _age_limit(value: timedelta) -> timedelta:
    if not isinstance(value, timedelta) or value < timedelta(0):
        raise FxValuationError("max_age must be a non-negative timedelta")
    return value


def _has_terminating_decimal(value: Fraction) -> bool:
    denominator = value.denominator
    while denominator % 2 == 0:
        denominator //= 2
    while denominator % 5 == 0:
        denominator //= 5
    return denominator == 1


def _optional_terminating_decimal(value: Fraction) -> Decimal | None:
    """Project compatibility rate only when projection itself fits the envelope.

    Exact rational rate identity is authoritative. A compatibility Decimal rate
    must never veto an otherwise bounded final FX conversion.
    """

    if not _has_terminating_decimal(value):
        return None
    try:
        return _exact_terminating_decimal(value)
    except ExactDecimalError:
        return None


@dataclass(frozen=True)
class FxRoundingPolicy:
    """Explicit conservative final-amount boundary for non-terminating FX math."""

    reporting_currency: str
    quantum: Decimal
    version: str = "1"

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "reporting_currency",
            _currency(self.reporting_currency, "reporting_currency"),
        )
        quantum = _decimal(self.quantum, "quantum")
        if quantum <= 0:
            raise FxValuationError("FX rounding quantum must be positive")
        object.__setattr__(self, "quantum", quantum)
        object.__setattr__(self, "version", _text(self.version, "version"))

    @property
    def policy_id(self) -> str:
        payload = "|".join(
            (
                "FX_CONSERVATIVE_FINAL_AMOUNT",
                self.version,
                self.reporting_currency,
                _canonical_decimal_text(self.quantum, name="FX rounding quantum"),
                "FLOOR",
            )
        ).encode("utf-8")
        return "fx-rounding:sha256:" + sha256(payload).hexdigest()


@dataclass(frozen=True)
class FxQuote:
    base_currency: str
    quote_currency: str
    bid: Decimal
    ask: Decimal
    available_at: datetime
    source_id: str
    evidence_sha256: str

    @classmethod
    def create(
        cls,
        *,
        base_currency: str,
        quote_currency: str,
        bid,
        ask,
        available_at: datetime,
        source_id: str,
        evidence_sha256: str,
    ) -> "FxQuote":
        base = _currency(base_currency, "base_currency")
        quote = _currency(quote_currency, "quote_currency")
        if base == quote:
            raise FxValuationError("FX quote currencies must differ")
        bid_value = _decimal(bid, "bid")
        ask_value = _decimal(ask, "ask")
        if bid_value <= 0 or ask_value <= 0:
            raise FxValuationError("FX bid and ask must be positive")
        if bid_value > ask_value:
            raise FxValuationError("FX bid cannot exceed ask")
        return cls(
            base_currency=base,
            quote_currency=quote,
            bid=bid_value,
            ask=ask_value,
            available_at=_instant(available_at, "available_at"),
            source_id=_text(source_id, "source_id"),
            evidence_sha256=_digest(evidence_sha256, "evidence_sha256"),
        )


@dataclass(frozen=True)
class FxValuation:
    source_amount: Decimal
    source_currency: str
    reporting_currency: str
    converted_amount: Decimal | None
    rate_used: Decimal | None
    side: str
    status: str
    available_at: datetime | None
    source_id: str | None
    evidence_sha256: str | None
    haircut: Decimal
    reason: str
    rate_numerator: int | None = None
    rate_denominator: int | None = None
    rounding_policy_id: str | None = None
    rounding_quantum: Decimal | None = None

    @property
    def allocatable(self) -> bool:
        return self.status == "CERTAIN"


@dataclass(frozen=True)
class PortfolioFxValuation:
    reporting_currency: str
    total: Decimal | None
    status: str
    components: tuple[FxValuation, ...]
    reasons: tuple[str, ...]

    @property
    def allocatable(self) -> bool:
        return self.status == "CERTAIN" and self.total is not None


def _unavailable(
    *,
    source_amount: Decimal,
    source: str,
    reporting: str,
    status: str,
    side: str,
    available_at: datetime | None,
    source_id: str | None,
    evidence_sha256: str | None,
    haircut: Decimal,
    reason: str,
    rate_numerator: int | None = None,
    rate_denominator: int | None = None,
) -> FxValuation:
    return FxValuation(
        source_amount=source_amount,
        source_currency=source,
        reporting_currency=reporting,
        converted_amount=None,
        rate_used=None,
        side=side,
        status=status,
        available_at=available_at,
        source_id=source_id,
        evidence_sha256=evidence_sha256,
        haircut=haircut,
        reason=reason,
        rate_numerator=rate_numerator,
        rate_denominator=rate_denominator,
    )


def value_amount(
    amount,
    *,
    source_currency: str,
    reporting_currency: str,
    quote: FxQuote | None,
    as_of: datetime,
    max_age: timedelta,
    haircut=Decimal("0"),
    rounding_policy: FxRoundingPolicy | None = None,
) -> FxValuation:
    """Conservatively value one signed balance at an evidenced point in time.

    Direct finite-decimal arithmetic is exact. Inverse rates remain exact
    rational identities. If the final reporting amount has a non-terminating
    base-10 expansion, a versioned reporting-currency quantum is required and
    the result is rounded toward negative infinity: assets cannot be overstated
    and liabilities cannot be understated.
    """

    source_amount = _decimal(amount, "amount")
    source = _currency(source_currency, "source_currency")
    reporting = _currency(reporting_currency, "reporting_currency")
    point = _instant(as_of, "as_of")
    age_limit = _age_limit(max_age)
    haircut_value = _decimal(haircut, "haircut")
    if haircut_value < 0 or haircut_value >= 1:
        raise FxValuationError("haircut must be in [0, 1)")
    if rounding_policy is not None:
        if not isinstance(rounding_policy, FxRoundingPolicy):
            raise FxValuationError("rounding_policy must be FxRoundingPolicy or None")
        if rounding_policy.reporting_currency != reporting:
            raise FxValuationError("FX rounding policy reporting currency mismatch")

    if source == reporting:
        return FxValuation(
            source_amount=source_amount,
            source_currency=source,
            reporting_currency=reporting,
            converted_amount=source_amount,
            rate_used=Decimal("1"),
            side="IDENTITY",
            status="CERTAIN",
            available_at=point,
            source_id="IDENTITY",
            evidence_sha256=None,
            haircut=Decimal("0"),
            reason="no FX conversion required",
            rate_numerator=1,
            rate_denominator=1,
        )

    if source_amount == 0:
        return FxValuation(
            source_amount=source_amount,
            source_currency=source,
            reporting_currency=reporting,
            converted_amount=Decimal("0"),
            rate_used=None,
            side="ZERO_BALANCE",
            status="CERTAIN",
            available_at=None,
            source_id=None,
            evidence_sha256=None,
            haircut=Decimal("0"),
            reason="zero foreign balance requires no FX rate",
        )

    if quote is None:
        return _unavailable(
            source_amount=source_amount,
            source=source,
            reporting=reporting,
            status="MISSING",
            side="UNAVAILABLE",
            available_at=None,
            source_id=None,
            evidence_sha256=None,
            haircut=haircut_value,
            reason="direct or inverse one-hop FX quote is missing",
        )
    if not isinstance(quote, FxQuote):
        raise FxValuationError("quote must be FxQuote or None")

    if quote.available_at > point:
        return _unavailable(
            source_amount=source_amount,
            source=source,
            reporting=reporting,
            status="FUTURE_EVIDENCE",
            side="UNAVAILABLE",
            available_at=quote.available_at,
            source_id=quote.source_id,
            evidence_sha256=quote.evidence_sha256,
            haircut=haircut_value,
            reason="FX quote was not available at valuation time",
        )
    if point - quote.available_at > age_limit:
        return _unavailable(
            source_amount=source_amount,
            source=source,
            reporting=reporting,
            status="STALE",
            side="UNAVAILABLE",
            available_at=quote.available_at,
            source_id=quote.source_id,
            evidence_sha256=quote.evidence_sha256,
            haircut=haircut_value,
            reason="FX quote exceeds maximum permitted age",
        )

    rate_used: Decimal | None
    if quote.base_currency == source and quote.quote_currency == reporting:
        if source_amount > 0:
            quoted_rate = quote.bid
            side = "BID"
        else:
            quoted_rate = quote.ask
            side = "ASK_FOR_LIABILITY"
        rate_fraction = _as_fraction(quoted_rate, name="FX quoted rate")
        rate_used = quoted_rate
    elif quote.quote_currency == source and quote.base_currency == reporting:
        if source_amount > 0:
            quoted_rate = quote.ask
            side = "INVERSE_ASK"
        else:
            quoted_rate = quote.bid
            side = "INVERSE_BID_FOR_LIABILITY"
        quoted_fraction = _as_fraction(quoted_rate, name="FX quoted rate")
        rate_fraction = Fraction(1, 1) / quoted_fraction
        rate_used = _optional_terminating_decimal(rate_fraction)
    else:
        raise FxValuationError("quote does not connect source and reporting currencies")

    converted_fraction = _as_fraction(source_amount, name="FX source amount") * rate_fraction
    haircut_fraction = _as_fraction(haircut_value, name="FX haircut")
    if converted_fraction > 0:
        converted_fraction *= Fraction(1, 1) - haircut_fraction
    elif converted_fraction < 0:
        converted_fraction *= Fraction(1, 1) + haircut_fraction

    applied_policy: FxRoundingPolicy | None = None
    if _has_terminating_decimal(converted_fraction):
        converted = _terminating_decimal(
            converted_fraction,
            name="FX converted amount",
        )
    else:
        if rounding_policy is None:
            return _unavailable(
                source_amount=source_amount,
                source=source,
                reporting=reporting,
                status="ROUNDING_POLICY_REQUIRED",
                side=side,
                available_at=quote.available_at,
                source_id=quote.source_id,
                evidence_sha256=quote.evidence_sha256,
                haircut=haircut_value,
                reason=(
                    "exact FX result has a non-terminating decimal expansion; "
                    "an explicit reporting-currency rounding policy is required"
                ),
                rate_numerator=rate_fraction.numerator,
                rate_denominator=rate_fraction.denominator,
            )
        converted = _round_fraction_to_quantum(
            converted_fraction,
            rounding_policy.quantum,
            name="FX rounded converted amount",
        )
        applied_policy = rounding_policy

    return FxValuation(
        source_amount=source_amount,
        source_currency=source,
        reporting_currency=reporting,
        converted_amount=converted,
        rate_used=rate_used,
        side=side,
        status="CERTAIN",
        available_at=quote.available_at,
        source_id=quote.source_id,
        evidence_sha256=quote.evidence_sha256,
        haircut=haircut_value,
        reason="fresh evidenced conservative FX valuation",
        rate_numerator=rate_fraction.numerator,
        rate_denominator=rate_fraction.denominator,
        rounding_policy_id=(
            applied_policy.policy_id if applied_policy is not None else None
        ),
        rounding_quantum=(
            applied_policy.quantum if applied_policy is not None else None
        ),
    )


def value_cash_balances(
    balances: Mapping[str, object],
    *,
    reporting_currency: str,
    quotes: Mapping[str, FxQuote],
    as_of: datetime,
    max_age: timedelta,
    haircut=Decimal("0"),
    rounding_policy: FxRoundingPolicy | None = None,
) -> PortfolioFxValuation:
    """Value separated currency balances without ever summing unlike units first."""

    if not isinstance(balances, Mapping):
        raise FxValuationError("balances must be a mapping")
    if not isinstance(quotes, Mapping):
        raise FxValuationError("quotes must be a mapping")
    reporting = _currency(reporting_currency, "reporting_currency")

    components: list[FxValuation] = []
    seen_currencies: set[str] = set()
    for raw_currency in sorted(balances):
        currency = _currency(raw_currency, "balance currency")
        if currency in seen_currencies:
            raise FxValuationError(
                "balances contain duplicate normalized currency codes"
            )
        seen_currencies.add(currency)
        amount = _decimal(balances[raw_currency], f"balance[{currency}]")
        quote = None if currency == reporting else quotes.get(currency)
        component = value_amount(
            amount,
            source_currency=currency,
            reporting_currency=reporting,
            quote=quote,
            as_of=as_of,
            max_age=max_age,
            haircut=haircut,
            rounding_policy=rounding_policy,
        )
        components.append(component)

    uncertain = tuple(
        f"{row.source_currency}: {row.status} - {row.reason}"
        for row in components
        if not row.allocatable
    )
    if uncertain:
        return PortfolioFxValuation(
            reporting_currency=reporting,
            total=None,
            status="UNCERTAIN",
            components=tuple(components),
            reasons=uncertain,
        )

    try:
        total = _exact_sum(
            row.converted_amount
            for row in components
            if row.converted_amount is not None
        )
    except ExactDecimalError as error:
        raise FxValuationError(
            "portfolio FX total exceeds the supported exact-decimal resource envelope"
        ) from error
    return PortfolioFxValuation(
        reporting_currency=reporting,
        total=total,
        status="CERTAIN",
        components=tuple(components),
        reasons=(),
    )
