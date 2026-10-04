"""Bridge canonical InstrumentVersion facts into thesis implementation screening.

The bridge snapshots an already-registered instrument version into the
proposal-only ``ImplementationCandidate`` model.  It does not make registry
metadata, route evidence, or screening output into trading authority.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from .instruments import InstrumentVersion
from .thesis_implementation import (
    ImplementationCandidate,
    MarketThesis,
    ThesisImplementationError,
)


def _exact_text(value: object, *, name: str) -> str:
    if type(value) is not str:
        raise TypeError(f"instrument {name} must be exact text")
    if not value:
        raise ThesisImplementationError(f"instrument {name} is required")
    return value


def _exact_datetime(value: object, *, name: str) -> datetime:
    if type(value) is not datetime:
        raise TypeError(f"instrument {name} must be exact datetime")
    if value.tzinfo is None or type(value.tzinfo) is not timezone:
        raise ThesisImplementationError(
            f"instrument {name} must use a built-in fixed-offset timezone"
        )
    return value.astimezone(timezone.utc)


def _thesis_snapshot(value: object) -> MarketThesis:
    if type(value) is not MarketThesis:
        raise TypeError("thesis must be exact MarketThesis")
    return MarketThesis(
        thesis_id=value.thesis_id,
        subject=value.subject,
        direction=value.direction,
        as_of=value.as_of,
        horizon_end=value.horizon_end,
        required_notional=value.required_notional,
    )


def candidate_from_instrument_version(
    *,
    thesis: MarketThesis,
    instrument: InstrumentVersion,
    candidate_id: str,
    exposure_subject: str,
    exposure_direction: str,
    route_id: str,
    legal: bool,
    technically_available: bool,
    route_qualified: bool,
    fee_rate: Decimal | str | int,
    spread_rate: Decimal | str | int,
    holding_cost_rate: Decimal | str | int,
    leverage_ratio: Decimal | str | int,
    liquidation_risk: Decimal | str | int,
    liquidity_capacity: Decimal | str | int,
) -> ImplementationCandidate:
    """Create one detached proposal candidate from canonical instrument facts.

    The caller must state the economic subject mapping explicitly because a
    fund, future, option, spot asset, or other instrument can have semantics
    that are not inferable from a provider symbol alone.  Subject equality is
    only a screening consistency check; it is not proof of economic exposure.
    """

    thesis_snapshot = _thesis_snapshot(thesis)
    if type(instrument) is not InstrumentVersion:
        raise TypeError("instrument must be exact InstrumentVersion")

    subject = _exact_text(exposure_subject, name="exposure_subject")
    if subject != thesis_snapshot.subject:
        raise ThesisImplementationError(
            "instrument exposure_subject does not match thesis subject"
        )

    instrument_id = _exact_text(instrument.instrument_id, name="instrument_id")
    if type(instrument.version) is not int or instrument.version < 1:
        raise ThesisImplementationError("instrument version must be a positive exact integer")
    provider_id = _exact_text(instrument.provider_id, name="provider_id")
    asset_class = _exact_text(instrument.asset_class, name="asset_class")
    status = _exact_text(instrument.status, name="status")
    if status != "ACTIVE":
        raise ThesisImplementationError("instrument version is not ACTIVE")

    effective_from = _exact_datetime(
        instrument.effective_from,
        name="effective_from",
    )
    effective_to = (
        _exact_datetime(instrument.effective_to, name="effective_to")
        if instrument.effective_to is not None
        else None
    )
    if thesis_snapshot.as_of < effective_from or (
        effective_to is not None and thesis_snapshot.as_of >= effective_to
    ):
        raise ThesisImplementationError(
            "instrument version is not effective at thesis as_of"
        )

    terminal_times: list[datetime] = []
    for field_name in ("effective_to", "last_trade_at", "expiry", "delivery_cutoff"):
        value = getattr(instrument, field_name)
        if value is not None:
            terminal_times.append(_exact_datetime(value, name=field_name))
    tradable_until = min(terminal_times) if terminal_times else None

    # Snapshot only the canonical identity facts needed by the screening
    # boundary.  Later mutation of the source InstrumentVersion object cannot
    # retarget the returned candidate.
    return ImplementationCandidate(
        candidate_id=candidate_id,
        thesis_id=thesis_snapshot.thesis_id,
        instrument_version=f"{instrument_id}@{instrument.version}",
        provider_id=provider_id,
        asset_class=asset_class,
        exposure_direction=exposure_direction,
        route_id=route_id,
        legal=legal,
        technically_available=technically_available,
        route_qualified=route_qualified,
        fee_rate=fee_rate,
        spread_rate=spread_rate,
        holding_cost_rate=holding_cost_rate,
        leverage_ratio=leverage_ratio,
        liquidation_risk=liquidation_risk,
        liquidity_capacity=liquidity_capacity,
        tradable_until=tradable_until,
    )
