"""Provider-evidenced durable perpetual funding accounting.

This module is an integration authority over existing canonical components:
sealed provider-read evidence, exact perpetual math, JournalStore, and
DurableProviderEconomicBook. It is not a second ledger and grants no order-send
authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from typing import Callable, Mapping, Sequence
from uuid import NAMESPACE_URL, uuid5

from .accounting import (
    JournalTransaction,
    posting,
    reverse_transaction,
    transaction_digest,
)
from .futures import FuturesError, settle_fraction
from .instruments import InstrumentRegistry, InstrumentVersion
from .perpetuals import (
    FundingConvention,
    MarketSnapshot,
    PerpetualContract,
    PerpetualError,
    funding_cashflow,
    inverse_funding_cashflow_exact,
)
from .persistence import JournalStore, canonical_json, payload_digest
from .provider_activity_accounting import DurableProviderEconomicBook
from .provider_core import ProviderResponseObservation, Surface


class PerpetualFundingError(ValueError):
    pass


class PerpetualFundingConflict(PerpetualFundingError):
    pass


def _text(value: str, name: str) -> str:
    if type(value) is not str or not value.strip():
        raise PerpetualFundingError(f"{name} is required")
    return value.strip()


def _decimal(value: Decimal | str | int, name: str) -> Decimal:
    if type(value) not in {Decimal, str, int}:
        raise PerpetualFundingError(f"{name} must use exact decimal input")
    try:
        result = value if type(value) is Decimal else Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise PerpetualFundingError(f"{name} must be a finite decimal") from error
    if not result.is_finite():
        raise PerpetualFundingError(f"{name} must be a finite decimal")
    return result


def _utc(value: datetime, name: str) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise PerpetualFundingError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _utc_text(value: datetime) -> str:
    return _utc(value, "timestamp").isoformat().replace("+00:00", "Z")


def _identity(kind: str, *parts: str) -> str:
    digest = sha256(canonical_json(list(parts)).encode("utf-8")).hexdigest()
    return f"{kind}:sha256:{digest}"


def _source_event_identity(
    observation: "PerpetualFundingObservation",
    external_event_id: str | None = None,
) -> str:
    """Canonical provider source-event identity.

    Opaque provider event IDs are not assumed account-global. Until a versioned
    provider contract proves a broader uniqueness domain, bind them to the exact
    canonical instrument version inside the already account/provider/environment
    scoped funding aggregate.
    """

    if not isinstance(observation, PerpetualFundingObservation):
        raise TypeError("observation must be PerpetualFundingObservation")
    return _identity(
        "funding-source-event",
        observation.instrument_version,
        observation.external_event_id
        if external_event_id is None
        else _text(external_event_id, "external_event_id"),
    )


def _funding_convention(version: InstrumentVersion) -> FundingConvention:
    """Resolve immutable funding sign/price semantics from the instrument version."""

    if not isinstance(version, InstrumentVersion):
        raise TypeError("version must be InstrumentVersion")
    schedule = version.funding_schedule
    if not isinstance(schedule, Mapping):
        raise PerpetualFundingError(
            "durable funding requires an explicit versioned funding convention"
        )
    price_basis = _text(
        schedule.get("price_basis"),
        "funding_schedule.price_basis",
    ).upper()
    positive_rate_effect = _text(
        schedule.get("positive_rate_effect"),
        "funding_schedule.positive_rate_effect",
    ).upper()
    if price_basis not in {"MARK", "INDEX"}:
        raise PerpetualFundingError(
            "funding_schedule.price_basis must be MARK or INDEX"
        )
    if positive_rate_effect not in {"LONG_PAYS", "LONG_RECEIVES"}:
        raise PerpetualFundingError(
            "funding_schedule.positive_rate_effect must be LONG_PAYS or LONG_RECEIVES"
        )
    return FundingConvention(positive_rate_effect, price_basis)


def _inverse_settlement_policy(
    version: InstrumentVersion,
) -> tuple[Decimal, str]:
    """Resolve the one explicit cash-quantization boundary from versioned contract data."""

    if not isinstance(version, InstrumentVersion) or version.payoff != "INVERSE":
        raise PerpetualFundingError(
            "inverse settlement policy requires an INVERSE instrument version"
        )
    schedule = version.funding_schedule
    if not isinstance(schedule, Mapping):
        raise PerpetualFundingError(
            "inverse durable funding requires an explicit settlement quantization policy"
        )
    quantum_value = schedule.get("settlement_quantum")
    rounding_value = schedule.get("settlement_rounding")
    if quantum_value is None or rounding_value is None:
        raise PerpetualFundingError(
            "inverse durable funding requires an explicit settlement quantization policy"
        )
    quantum = _decimal(quantum_value, "funding_schedule.settlement_quantum")
    if quantum <= 0:
        raise PerpetualFundingError(
            "funding_schedule.settlement_quantum must be positive"
        )
    rounding = _text(
        rounding_value,
        "funding_schedule.settlement_rounding",
    ).upper()
    if rounding not in {"HALF_EVEN", "DOWN"}:
        raise PerpetualFundingError(
            "funding_schedule.settlement_rounding must be HALF_EVEN or DOWN"
        )
    return quantum, rounding


@dataclass(frozen=True)
class PerpetualFundingObservation:
    """Provider-normalized funding fact still bound to sealed raw evidence."""

    provider_id: str
    account_id: str
    environment: str
    instrument_id: str
    instrument_version: str
    external_event_id: str
    provider_revision: str
    funding_period_id: str
    effective_at: datetime