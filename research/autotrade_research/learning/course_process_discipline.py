"""Deterministic process discipline distilled from the supporting course.

The owner's 100 supporting transcripts are primarily about execution process:
planning, patience, journaling, FOMO, revenge trading, overtrading, loss
acceptance and consistency.  Their text is not copied here.  A content-free
digest and topic counts preserve provenance while this module turns the
repeated lessons into a review that can be applied to closed TRAIN episodes.

The review is diagnostic.  It cannot mutate a strategy, consume DEVELOPMENT or
FINAL evidence, call a model, or grant trading authority.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json


SCHEMA = "autotrade-private-course-process-discipline-v1"
SUPPORTING_TRANSCRIPT_INDEX_SHA256 = (
    "5bce8595a153a622d179a668a6dde427938f70ce305e5b5176932059dd9f8971"
)
TOPIC_SIGNAL_COUNTS = {
    "DISCIPLINE": 61,
    "EMOTION": 260,
    "FOMO": 23,
    "JOURNAL": 35,
    "LOSS_OR_DRAWDOWN": 197,
    "OVERTRADING": 11,
    "PAUSE_OR_BREAK": 50,
    "PLAN": 91,
    "REVENGE_TRADING": 62,
    "RISK": 97,
    "STRUCTURE": 29,
    "CHECKLIST": 11,
}

_REQUIRED_JOURNAL_FIELDS = (
    "detected_at", "entered_at", "exited_at", "direction", "entry_price",
    "stop_price", "target_price", "planned_reward_risk", "exit_reason",
    "pnl_after_costs", "return_on_start_equity", "fees", "funding",
    "profitable",
)


def process_curriculum_packet() -> dict:
    """Return a content-free, digest-bound curriculum projection."""
    value = {
        "schema": SCHEMA,
        "source": {
            "file_count": 100,
            "total_bytes": 754_619,
            "index_sha256": SUPPORTING_TRANSCRIPT_INDEX_SHA256,
            "transcript_text_embedded": False,
        },
        "topic_signal_counts": TOPIC_SIGNAL_COUNTS,
        "modules": [
            {"id": "PRE_ENTRY_PLAN", "rules": [
                "NO_ENTRY_WITHOUT_COMPLETE_CHECKLIST",
                "ABSTENTION_IS_A_VALID_DECISION",
            ]},
            {"id": "LOSS_RECOVERY", "rules": [
                "PAUSE_AFTER_CONSECUTIVE_LOSSES",
                "NO_REVENGE_REENTRY",
                "ANALYZE_BEFORE_RESUME",
            ]},
            {"id": "OVERTRADING", "rules": [
                "CAP_TRADES_PER_MARKET_DAY",
                "STOP_AT_DAILY_LOSS_BOUND",
            ]},
            {"id": "JOURNAL", "rules": [
                "RECORD_PLAN_EXECUTION_COSTS_AND_OUTCOME",
                "INCOMPLETE_JOURNAL_FAILS_CLOSED",
            ]},
            {"id": "CONSISTENCY", "rules": [
                "KEEP_THE_FROZEN_STRATEGY_DURING_REVIEW",
                "CHANGE_ONLY_A_PREDECLARED_AXIS_AFTER_EVIDENCE",
            ]},
        ],
    }
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"),
        allow_nan=False)
    value["packet_sha256"] = sha256(encoded.encode()).hexdigest()
    return value


def _timestamp(value: object, field: str) -> datetime:
    if type(value) is not str:
        raise ValueError(f"episode {field} must be text")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"episode {field} must be timezone-aware")
    return parsed


def _decimal(value: object, field: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as error:
        raise ValueError(f"episode {field} must be decimal") from error
    if not parsed.is_finite():
        raise ValueError(f"episode {field} must be finite")
    return parsed


def review_course_episodes(
    sources: list[dict], *, maximum_consecutive_losses: int = 3,
    maximum_trades_per_market_day: int = 4,
    maximum_daily_loss_fraction: Decimal | str = Decimal("0.02"),
) -> dict:
    """Review closed TRAIN episodes under the course's process rules.

    Daily limits are evaluated per market because each historical simulation
    owns an independent equity ledger.  The function reports triggers instead
    of deleting later episodes, so the evidence remains complete and auditable.
    """
    if type(maximum_consecutive_losses) is not int or not 1 <= maximum_consecutive_losses <= 20:
        raise ValueError("maximum consecutive losses must be 1..20")
    if (type(maximum_trades_per_market_day) is not int
            or not 1 <= maximum_trades_per_market_day <= 100):
        raise ValueError("maximum trades per market day must be 1..100")
    daily_loss = _decimal(maximum_daily_loss_fraction, "maximum daily loss")
    if not Decimal("0") < daily_loss <= Decimal("0.20"):
        raise ValueError("maximum daily loss must be within 0..0.20")
    if type(sources) is not list:
        raise TypeError("episode sources must be a list")

    by_market: dict[str, list[dict]] = {}
    incomplete: list[dict] = []
    episode_count = 0
    for source in sources:
        if type(source) is not dict or type(source.get("market")) is not str:
            raise ValueError("each source needs a market")
        market = source["market"]
        if market in by_market:
            raise ValueError(f"duplicate market source: {market}")
        episodes = source.get("episodes")
        if type(episodes) is not list:
            raise ValueError(f"episodes for {market} must be a list")
        normalized = []
        for ordinal, episode in enumerate(episodes, 1):
            if type(episode) is not dict:
                raise ValueError("episode must be an object")
            missing = [name for name in _REQUIRED_JOURNAL_FIELDS if name not in episode]
            if missing:
                incomplete.append({"market": market, "ordinal": ordinal,
                    "missing_fields": missing})
                continue
            entered = _timestamp(episode["entered_at"], "entered_at")
            exited = _timestamp(episode["exited_at"], "exited_at")
            if exited <= entered:
                raise ValueError("episode exit must follow entry")
            pnl = _decimal(episode["pnl_after_costs"], "pnl_after_costs")
            return_fraction = _decimal(
                episode["return_on_start_equity"], "return_on_start_equity")
            if type(episode["profitable"]) is not bool:
                raise ValueError("episode profitable must be boolean")
            if episode["profitable"] != (pnl > 0):
                raise ValueError("episode profitable conflicts with PnL")
            normalized.append({"ordinal": ordinal, "entered_at": entered,
                "exited_at": exited, "pnl": pnl,
                "return_fraction": return_fraction,
                "profitable": episode["profitable"]})
            episode_count += 1
        by_market[market] = sorted(normalized,
            key=lambda item: (item["exited_at"], item["ordinal"]))

    streak_triggers = []
    maximum_observed_streak = 0
    daily: dict[tuple[str, str], dict] = defaultdict(
        lambda: {"trades": 0, "net_return": Decimal("0")})
    for market, episodes in sorted(by_market.items()):
        streak = 0
        trigger_open = False
        for episode in episodes:
            key = (market, episode["exited_at"].date().isoformat())
            daily[key]["trades"] += 1
            daily[key]["net_return"] += episode["return_fraction"]
            if episode["profitable"]:
                streak = 0
                trigger_open = False
            else:
                streak += 1
                maximum_observed_streak = max(maximum_observed_streak, streak)
                if streak >= maximum_consecutive_losses and not trigger_open:
                    streak_triggers.append({"market": market,
                        "observed_at": episode["exited_at"].isoformat(),
                        "consecutive_losses": streak})
                    trigger_open = True

    daily_triggers = []
    for (market, day), value in sorted(daily.items()):
        reasons = []
        if value["trades"] > maximum_trades_per_market_day:
            reasons.append("TRADE_COUNT_BOUND_EXCEEDED")
        if value["net_return"] <= -daily_loss:
            reasons.append("DAILY_LOSS_BOUND_REACHED")
        if reasons:
            daily_triggers.append({"market": market, "utc_day": day,
                "trades": value["trades"],
                "net_return_fraction": str(value["net_return"]),
                "reasons": reasons})

    reasons = []
    if incomplete:
        reasons.append("INCOMPLETE_TRADE_JOURNAL")
    if streak_triggers:
        reasons.append("CONSECUTIVE_LOSS_PAUSE_REQUIRED")
    if daily_triggers:
        reasons.append("DAILY_PROCESS_BOUND_REACHED")
    if not episode_count:
        phase = "COLLECTING"
    elif reasons:
        phase = "PAUSED_ANALYZING"
    else:
        phase = "RUNNING_ELIGIBLE"
    result = {
        "schema": SCHEMA,
        "source_partition": "TRAIN_ONLY",
        "phase": phase,
        "episode_count": episode_count,
        "market_count": len(by_market),
        "journal_complete": not incomplete,
        "incomplete_journal_count": len(incomplete),
        "maximum_observed_consecutive_losses": maximum_observed_streak,
        "loss_streak_pause_events": streak_triggers,
        "daily_process_events": daily_triggers,
        "pause_reasons": reasons,
        "policy": {
            "maximum_consecutive_losses": maximum_consecutive_losses,
            "maximum_trades_per_market_day": maximum_trades_per_market_day,
            "maximum_daily_loss_fraction": str(daily_loss),
        },
        "resume_condition": (
            "REVIEW_CHECKPOINT_WRITTEN_AND_COOLDOWN_ELAPSED"
            if reasons else "NEXT_FROZEN_SERIES"
        ),
        "same_frozen_strategy_required": True,
        "development_consumed": False,
        "final_consumed": False,
        "automatic_strategy_mutation": False,
        "automatic_promotion": False,
        "paid_model_calls": 0,
        "network_orders": False,
        "trading_authority": False,
    }
    encoded = json.dumps(result, sort_keys=True, separators=(",", ":"),
        allow_nan=False)
    result["review_sha256"] = sha256(encoded.encode()).hexdigest()
    return result
