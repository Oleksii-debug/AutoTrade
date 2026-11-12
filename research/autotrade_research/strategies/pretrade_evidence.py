"""Fail-closed evidence gate between analysis and any simulated entry.

A directional signal is not an instruction to trade.  The caller must prove a
causal regime, location, trigger, invalidation and after-cost payoff first.
Live-market callers must additionally supply non-conflicting microstructure.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


SCHEMA = "autotrade-pretrade-evidence-gate-v1"


def _decimal(value: object, name: str, *, positive: bool = False) -> Decimal:
    if type(value) is Decimal:
        result = value
    elif type(value) is str:
        try:
            result = Decimal(value)
        except Exception:
            raise ValueError(f"{name} must be an exact decimal") from None
    else:
        raise TypeError(f"{name} must be Decimal or decimal text")
    if not result.is_finite() or (positive and result <= 0):
        raise ValueError(f"{name} is outside its admitted range")
    return result


@dataclass(frozen=True, slots=True)
class PreTradeThesis:
    direction: str
    regime: str
    causal_data_complete: bool
    regime_confirmed: bool
    location_confirmed: bool
    trigger_confirmed: bool
    entry_price: Decimal | str
    invalidation_price: Decimal | str
    target_price: Decimal | str
    round_trip_cost_bps: Decimal | str
    minimum_reward_risk: Decimal | str = Decimal("2")
    minimum_net_reward_cost_multiple: Decimal | str = Decimal("1.5")
    live_market: bool = False
    microstructure_status: str = "NOT_AVAILABLE_HISTORICAL"


def evaluate_pretrade(thesis: PreTradeThesis) -> dict:
    if type(thesis) is not PreTradeThesis:
        raise TypeError("canonical PreTradeThesis required")
    if thesis.direction not in {"LONG", "SHORT"}:
        raise ValueError("direction must be LONG or SHORT")
    if thesis.regime not in {"TREND", "RANGE"}:
        raise ValueError("regime must be TREND or RANGE")
    for name in ("causal_data_complete", "regime_confirmed",
                 "location_confirmed", "trigger_confirmed", "live_market"):
        if type(getattr(thesis, name)) is not bool:
            raise TypeError(f"{name} must be an exact bool")
    if thesis.microstructure_status not in {
            "CONFIRMED", "CONFLICT", "STALE", "NOT_AVAILABLE_HISTORICAL"}:
        raise ValueError("unsupported microstructure status")
    entry = _decimal(thesis.entry_price, "entry_price", positive=True)
    stop = _decimal(thesis.invalidation_price, "invalidation_price", positive=True)
    target = _decimal(thesis.target_price, "target_price", positive=True)
    cost_bps = _decimal(thesis.round_trip_cost_bps, "round_trip_cost_bps")
    minimum_rr = _decimal(thesis.minimum_reward_risk, "minimum_reward_risk", positive=True)
    minimum_cost_multiple = _decimal(
        thesis.minimum_net_reward_cost_multiple,
        "minimum_net_reward_cost_multiple", positive=True)
    if not Decimal(0) <= cost_bps < Decimal(1000):
        raise ValueError("round_trip_cost_bps is outside [0,1000)")
    if thesis.direction == "LONG":
        risk = entry - stop
        reward = target - entry
    else:
        risk = stop - entry
        reward = entry - target
    cost = entry * cost_bps / Decimal(10000)
    net_reward = reward - cost
    total_risk = risk + cost
    gross_rr = reward / risk if risk > 0 else Decimal(0)
    net_rr = net_reward / total_risk if total_risk > 0 else Decimal(0)
    reward_cost_multiple = net_reward / cost if cost > 0 else Decimal("Infinity")

    checks = {
        "CAUSAL_DATA_COMPLETE": thesis.causal_data_complete,
        "REGIME_CONFIRMED": thesis.regime_confirmed,
        "LOCATION_CONFIRMED": thesis.location_confirmed,
        "TRIGGER_CONFIRMED": thesis.trigger_confirmed,
        "INVALIDATION_ORDERED": risk > 0,
        "TARGET_ORDERED": reward > 0,
        "GROSS_REWARD_RISK": risk > 0 and gross_rr >= minimum_rr,
        "POSITIVE_AFTER_COST_REWARD": net_reward > 0,
        "AFTER_COST_REWARD_RISK": total_risk > 0 and net_rr >= minimum_rr,
        "REWARD_COVERS_COST_HURDLE": (
            cost == 0 or reward_cost_multiple >= minimum_cost_multiple),
        "MICROSTRUCTURE_NOT_CONFLICTING": (
            thesis.microstructure_status not in {"CONFLICT", "STALE"}),
        "LIVE_MICROSTRUCTURE_CONFIRMED": (
            not thesis.live_market or thesis.microstructure_status == "CONFIRMED"),
    }
    failures = [name for name, passed in checks.items() if not passed]
    return {
        "schema": SCHEMA,
        "decision": "ENTER" if not failures else "HOLD",
        "failure_reasons": failures,
        "checks": checks,
        "direction": thesis.direction,
        "regime": thesis.regime,
        "gross_reward_risk": format(gross_rr, "f"),
        "after_cost_reward_risk": format(net_rr, "f"),
        "after_cost_reward_fraction": format(net_reward / entry, "f"),
        "round_trip_cost_bps": format(cost_bps, "f"),
        "microstructure_status": thesis.microstructure_status,
        "live_market": thesis.live_market,
    }

