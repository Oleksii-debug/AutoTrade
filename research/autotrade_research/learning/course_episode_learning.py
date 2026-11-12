"""Bounded post-session learning for the frozen private-course strategy.

The learner consumes only closed TRAIN episodes.  It never touches exchange
credentials, orders, DEVELOPMENT, or FINAL.  After 100 identical executions it
may register one narrow hypothesis: reject confirmations that arrive too long
after the liquidity sweep.  That hypothesis still has to be rerun exactly and
pass later partitions before it can become a candidate.
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from hashlib import sha256
import json


SCHEMA = "autotrade-private-course-episode-learning-v1"


def _summary(rows: list[dict]) -> dict:
    pnl = sum((Decimal(row["pnl_after_costs"]) for row in rows), Decimal(0))
    fees = sum((Decimal(row["fees"]) for row in rows), Decimal(0))
    funding = sum((Decimal(row["funding"]) for row in rows), Decimal(0))
    wins = sum(bool(row["profitable"]) for row in rows)
    return {"cycles": len(rows), "wins": wins, "losses": len(rows) - wins,
        "win_rate": str(Decimal(wins) / Decimal(len(rows))) if rows else None,
        "pnl_after_costs": str(pnl), "fees": str(fees), "funding": str(funding),
        "mean_pnl": str(pnl / Decimal(len(rows))) if rows else None}


def analyze_course_episodes(sources: list[dict], *, minimum_cycles: int = 100) -> dict:
    if minimum_cycles < 20:
        raise ValueError("minimum_cycles must preserve a meaningful series")
    episodes: list[dict] = []
    markets: list[str] = []
    source_hashes: list[str] = []
    for source in sources:
        market = str(source["market"])
        if market in markets:
            raise ValueError(f"duplicate market source: {market}")
        markets.append(market)
        source_hashes.append(str(source["dataset_identity_sha256"]))
        for episode in source["episodes"]:
            episodes.append({**episode, "market": market})

    by_exit: dict[str, list[dict]] = defaultdict(list)
    by_speed = {"FAST_1_4": [], "MEDIUM_5_8": [], "SLOW_9_16": []}
    for episode in episodes:
        by_exit[str(episode["exit_reason"])].append(episode)
        delay = int(episode["sweep_to_shift_bars"])
        bucket = "FAST_1_4" if delay <= 4 else "MEDIUM_5_8" if delay <= 8 else "SLOW_9_16"
        by_speed[bucket].append(episode)

    baseline = _summary(episodes)
    hypothesis = None
    status = "COLLECTING_FROZEN_SERIES"
    if len(episodes) >= minimum_cycles:
        status = "MINIMUM_SERIES_REACHED"
        candidates = []
        for minimum, maximum in ((1, 8), (5, 8)):
            kept = [row for row in episodes
                    if minimum <= int(row["sweep_to_shift_bars"]) <= maximum]
            summary = _summary(kept)
            if len(kept) >= minimum_cycles // 5:
                candidates.append((Decimal(summary["pnl_after_costs"]), minimum, maximum, summary))
        improving = [item for item in candidates
                     if item[0] > 0 and item[0] > Decimal(baseline["pnl_after_costs"])]
        if improving:
            pnl, minimum, maximum, summary = max(improving, key=lambda item: item[0])
            hypothesis = {"kind": "SHIFT_CONFIRMATION_EXPIRY",
                "candidate_minimum_shift_confirmation_bars": minimum,
                "candidate_shift_expiry_bars": maximum,
                "retrospective_training_summary": summary,
                "estimated_training_pnl_delta": str(pnl - Decimal(baseline["pnl_after_costs"])),
                "authority": "REGISTERED_HYPOTHESIS_ONLY",
                "requires_exact_train_rerun": True,
                "requires_locked_development": True,
                "requires_untouched_final": True}
            status = "ONE_AXIS_CANDIDATE_REGISTERED"
        else:
            status = "NO_IMPROVING_ONE_AXIS_CANDIDATE"

    result = {"schema": SCHEMA, "status": status,
        "minimum_cycles": minimum_cycles, "frozen_series_complete": len(episodes) >= minimum_cycles,
        "markets": sorted(markets), "dataset_identity_sha256": sorted(source_hashes),
        "baseline": baseline,
        "exit_reason_diagnostics": {key: _summary(value) for key, value in sorted(by_exit.items())},
        "confirmation_speed_diagnostics": {key: _summary(value) for key, value in by_speed.items()},
        "candidate_hypothesis": hypothesis,
        "source_partition": "TRAIN_ONLY", "development_consumed": False,
        "final_consumed": False, "strategy_changed": False,
        "automatic_promotion": False, "network_orders": False,
        "trading_authority": False}
    encoded = json.dumps(result, sort_keys=True, separators=(",", ":"), allow_nan=False)
    result["learning_checkpoint_sha256"] = sha256(encoded.encode()).hexdigest()
    return result

