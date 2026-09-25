"""Exact, freshness-bound FX valuation primitives.

The module values already-known cash or collateral balances. It has no network
or trading authority and deliberately supports only direct/inverse one-hop
quotes. Missing, stale or future evidence produces an uncertain valuation
instead of inventing a conversion rate.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import re
from typing import Mapping


class FxValuationError(ValueError):
    pass


def _decimal(value, name: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise FxValuationError(f"{name} must use exact decimal input")
    try:
        result = value if isinstance(value, Decimal) else Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise FxValuationError(f"{name} must be a finite decimal") from error
    if not result.is_finite():
        raise FxValuationError(f"{name} must be a finite decimal")
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


def value_amount(
    amount,
    *,
    source_currency: str,
    reporting_currency: str,
    quote: FxQuote | None,
    as_of: datetime,
    max_age: timedelta,
    haircut=Decimal("0"),
) -> FxValuation:
    """Conservatively value one signed balance at an evidenced point in time."""

    source_amount = _decimal(amount, "amount")
    source = _currency(source_currency, "source_currency")
    reporting = _currency(reporting_currency, "reporting_currency")
    point = _instant(as_of, "as_of")
    age_limit = _age_limit(max_age)
    haircut_value = _decimal(haircut, "haircut")
    if haircut_value < 0 or haircut_value >= 1:
        raise FxValuationError("haircut must be in [0, 1)")

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
        return FxValuation(
            source_amount=source_amount,
            source_currency=source,
            reporting_currency=reporting,
            converted_amount=None,
            rate_used=None,
            side="UNAVAILABLE",
            status="MISSING",
            available_at=None,
            source_id=None,
            evidence_sha256=None,
            haircut=haircut_value,
            reason="direct or inverse one-hop FX quote is missing",
        )
    if not isinstance(quote, FxQuote):
        raise FxValuationError("quote must be FxQuote or None")

    if quote.available_at > point:
        return FxValuation(
            source_amount=source_amount,
            source_currency=source,
            reporting_currency=reporting,
            converted_amount=None,
            rate_used=None,
            side="UNAVAILABLE",
            status="FUTURE_EVIDENCE",
            available_at=quote.available_at,
            source_id=quote.source_id,
            evidence_sha256=quote.evidence_sha256,
            haircut=haircut_value,
            reason="FX quote was not available at valuation time",
        )
    if point - quote.available_at > age_limit:
        return FxValuation(
            source_amount=source_amount,
            source_currency=source,
            reporting_currency=reporting,
            converted_amount=None,
            rate_used=None,
            side="UNAVAILABLE",
            status="STALE",
            available_at=quote.available_at,
            source_id=quote.source_id,
            evidence_sha256=quote.evidence_sha256,
            haircut=haircut_value,
            reason="FX quote exceeds maximum permitted age",
        )

    if quote.base_currency == source and quote.quote_currency == reporting:
        if source_amount > 0:
            rate = quote.bid
            side = "BID"
        else:
            rate = quote.ask
            side = "ASK_FOR_LIABILITY"
        converted = source_amount * rate
    elif quote.quote_currency == source and quote.base_currency == reporting:
        if source_amount > 0:
            rate = Decimal("1") / quote.ask
            side = "INVERSE_ASK"
        else:
            rate = Decimal("1") / quote.bid
            side = "INVERSE_BID_FOR_LIABILITY"
        converted = source_amount * rate
    else:
        raise FxValuationError("quote does not connect source and reporting currencies")

    if converted > 0:
        converted *= Decimal("1") - haircut_value
    elif converted < 0:
        converted *= Decimal("1") + haircut_value

    return FxValuation(
        source_amount=source_amount,
        source_currency=source,
        reporting_currency=reporting,
        converted_amount=converted,
        rate_used=rate,
        side=side,
        status="CERTAIN",
        available_at=quote.available_at,
        source_id=quote.source_id,
        evidence_sha256=quote.evidence_sha256,
        haircut=haircut_value,
        reason="fresh evidenced conservative FX valuation",
    )


def value_cash_balances(
    balances: Mapping[str, object],
    *,
    reporting_currency: str,
    quotes: Mapping[str, FxQuote],
    as_of: datetime,
    max_age: timedelta,
    haircut=Decimal("0"),
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

    total = sum(
        (row.converted_amount for row in components if row.converted_amount is not None),
        Decimal("0"),
    )
    return PortfolioFxValuation(
        reporting_currency=reporting,
        total=total,
        status="CERTAIN",
        components=tuple(components),
        reasons=(),
    )
