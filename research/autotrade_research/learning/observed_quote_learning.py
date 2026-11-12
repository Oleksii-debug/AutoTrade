"""Causal, local estimates from prospective SOL quote outcomes."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from ..strategies.technical_confluence import digest, summarize_hour_windows


HORIZON_SECONDS = 1800
MINIMUM_COMPARABLE_OUTCOMES = 8
ZERO_RETURN_PRIOR = 8


def _band_region(technical: dict | None) -> str | None:
    features = (technical or {}).get("features")
    if not features:
        return None
    atr = Decimal(features["atr"])
    if atr <= 0:
        return None
    displacement = (Decimal(features["close"]) - Decimal(features["band_mid"])) / atr
    if displacement < Decimal("-0.5"):
        return "BELOW"
    if displacement > Decimal("0.5"):
        return "ABOVE"
    return "NEAR"


def propose_from_observed_outcomes(
    recent: list[dict], *, entry_epoch: int, quote_received_at: datetime,
    technical: dict | None,
) -> dict | None:
    """Fit one region estimate with outcomes known before the next decision."""
    if entry_epoch % HORIZON_SECONDS:
        return None
    region = _band_region(technical)
    comparable = {}
    if region is not None:
        for outcome in recent:
            if (outcome["entry_boundary_epoch"] % HORIZON_SECONDS == 0
                    and outcome["exit_boundary_epoch"] <= entry_epoch
                    and datetime.fromisoformat(outcome["exit_quote_received_at"]) <= quote_received_at
                    and _band_region(outcome.get("technical")) == region):
                comparable[outcome["outcome_id"]] = outcome
    count = len(comparable)
    means = {
        side: sum((Decimal(row["outcomes"][side]["net_equity_return"])
                   for row in comparable.values()), Decimal(0)) / (count + ZERO_RETURN_PRIOR)
        for side in ("LONG", "SHORT")
    }
    selected = max(means, key=means.get)
    ready = count >= MINIMUM_COMPARABLE_OUTCOMES
    model = {"region": region, "training_outcome_count": count,
             "training_outcome_ids": sorted(comparable),
             "expected_net_equity_returns": {side: str(value) for side, value in means.items()},
             "status": "ESTIMATED" if ready else "COLLECTING_OBSERVATIONS"}
    return {**model, "model_sha256": digest(model),
            "action": selected if ready and means[selected] > 0 else "HOLD"}


def review_observed_predictions(recent: list[dict]) -> dict:
    """Score recorded choices, including flat decisions, on complete UTC hours."""
    marks = {}
    trades = 0
    for outcome in recent:
        decision = outcome.get("learner_proposal")
        if decision is None:
            continue
        action = decision["action"]
        value = Decimal(0)
        if action in ("LONG", "SHORT"):
            value = Decimal(outcome["outcomes"][action]["net_equity_return"])
            trades += 1
        marks[outcome["entry_boundary_epoch"]] = value
    hours = [(1 + value) * (1 + marks[epoch + HORIZON_SECONDS]) - 1
             for epoch, value in sorted(marks.items())
             if epoch % 3600 == 0 and epoch + HORIZON_SECONDS in marks]
    return {"observed_half_hours": len(marks), "simulated_direction_choices": trades,
            "hourly": summarize_hour_windows(hours, 1)}
