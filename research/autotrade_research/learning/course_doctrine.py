"""Executable doctrine distilled from the owner's private trading curriculum.

No transcript text is embedded.  The packet records source digests and turns
the course's repeated rules into a deterministic critic that numerical and LLM
agents must satisfy before a setup can enter simulation.
"""
from __future__ import annotations

from decimal import Decimal
from hashlib import sha256
import json


SCHEMA = "autotrade-private-course-doctrine-v1"
SOURCE_DIGESTS = {
    "EXCHANGE_AND_MARKETS": "f62754cf2a256205330aa591f091fa0360d9cc222bfa9eef2f8af0d88674c81d",
    "AUCTION_MARKET_THEORY": "f6232a4a4bc7e04d61ee827550e8a074c8585b7a8927bc95eb938473e0b1aa67",
    "TRADING_STRATEGY": "b0f228f6f8ce351e351484239fe4617706a11ed02eb7bda86b50edbc18c99327",
    "RISK_MANAGEMENT": "ba9145e67b104e2d6b106a7827f518c24b2eb77d830df13085b1515407e6393c",
    "LIQUIDITY": "bc310b11cf38e37be7ca267034585158985a572c11d60f3f1b350fb308bef7eb",
    "TOP_DOWN": "902c2104dab2035c6e4ad1eae9427dfe9a9a8889a942351f868cb71fc269bfe6",
    "FIBONACCI": "58c539732318d55e28bfca731bf59ba45d2271a45a58494abff9fdac114588fe",
    "PRICE_PATTERNS": "4989d7981cdf30677acb09cc71e244a5eb0b8d4d7cdf91d324f4bd913dd3ac66",
    "CONSOLIDATIONS": "2aafdad06d071b538add6db88279ea48c56d57acf8682db83d79a4e26d565172",
    "PROP_AND_TERMINALS": "7d5f0499bce38490ffee55e2c4e8560a72f9daaa399f2dd0d2c0ab87d734d22e",
    "TRADING_PSYCHOLOGY_SUPPORT": "5bce8595a153a622d179a668a6dde427938f70ce305e5b5176932059dd9f8971",
}


def curriculum_packet() -> dict:
    value = {"schema": SCHEMA, "source_digests": SOURCE_DIGESTS,
        "modules": [
            {"id": "MARKET_REGIME", "rules": ["CLASSIFY_TREND_OR_RANGE", "DO_NOT_MIX_REGIME_LOGIC"]},
            {"id": "TOP_DOWN", "rules": ["HTF_CONTEXT_DOMINATES", "HTF_TO_MTF_TO_LTF",
                "ADJACENT_TIMEFRAME_RATIO_AT_MOST_30", "LTF_CONFIRMS_BUT_DOES_NOT_INVENT_THESIS"]},
            {"id": "POI", "rules": ["FVG_OR_ORDER_BLOCK_OR_REJECTION_BLOCK",
                "POI_MUST_ALIGN_WITH_HTF_BIAS"]},
            {"id": "LIQUIDITY", "rules": ["MAP_INTERNAL_AND_EXTERNAL_LIQUIDITY",
                "REQUIRE_SWEEP_AND_REACTION", "TARGET_OPPOSING_LIQUIDITY"]},
            {"id": "PREMIUM_DISCOUNT", "rules": ["LONG_FROM_DISCOUNT", "SHORT_FROM_PREMIUM"]},
            {"id": "TREND_ENTRY", "rules": ["MARKET_SHIFT_AFTER_SWEEP", "RETEST_POI_OR_FVG",
                "FIBONACCI_ONLY_IN_TREND", "OTE_0618_TO_0786_MIDPOINT_0705"]},
            {"id": "RANGE_ENTRY", "rules": ["FIX_RANGE_BOUNDARIES", "USE_HALF_RANGE_EQUILIBRIUM",
                "DEVIATION_MUST_REENTER_AGGRESSIVELY"]},
            {"id": "RISK", "rules": ["RISK_AT_MOST_ONE_PERCENT", "STOP_BEYOND_INVALIDATION",
                "NEVER_WIDEN_STOP", "MINIMUM_TWO_R"]},
            {"id": "SERIES_DISCIPLINE", "rules": ["JUDGE_IDENTICAL_EXECUTION_SERIES",
                "DO_NOT_CHANGE_BEFORE_100_CYCLES", "JOURNAL_EVERY_CYCLE"]},
            {"id": "PROCESS_DISCIPLINE", "rules": ["CHECKLIST_BEFORE_ENTRY",
                "ABSTENTION_IS_VALID", "NO_REVENGE_REENTRY",
                "PAUSE_ANALYZE_AND_COOLDOWN_AFTER_LOSS_BOUND",
                "CAP_TRADES_AND_DAILY_LOSS", "RESUME_THE_SAME_FROZEN_STRATEGY"]},
            {"id": "INDEPENDENT_EVALUATION", "rules": ["LOCK_DEVELOPMENT_AND_FINAL",
                "COUNT_ALL_COSTS", "NO_SELF_PROMOTION"]},
        ], "transcript_text_embedded": False}
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    value["packet_sha256"] = sha256(encoded.encode()).hexdigest()
    return value


def criticize_setup(proposal: dict) -> list[str]:
    """Return stable course-rule violations for a proposed simulated setup."""
    violations: list[str] = []
    regime = proposal.get("regime")
    direction = proposal.get("direction")
    if regime not in {"TREND", "RANGE"}: violations.append("REGIME_NOT_CLASSIFIED")
    if direction not in {"LONG", "SHORT"}: violations.append("DIRECTION_NOT_EXACT")
    frames = proposal.get("timeframes")
    if (not isinstance(frames, list) or len(frames) != 3
            or any(type(value) is not int or value <= 0 for value in frames)):
        violations.append("HTF_MTF_LTF_LADDER_MISSING")
    elif frames[0] / frames[1] > 30 or frames[1] / frames[2] > 30:
        violations.append("TIMEFRAME_RATIO_TOO_LARGE")
    if proposal.get("htf_bias") != direction: violations.append("HTF_DIRECTION_MISMATCH")
    if proposal.get("mtf_poi") not in {"FVG", "ORDER_BLOCK", "REJECTION_BLOCK"}:
        violations.append("MTF_POI_MISSING")
    if proposal.get("ltf_trigger") not in {"LIQUIDITY_SWEEP_MARKET_SHIFT", "QUASIMODO", "V_SHAPE"}:
        violations.append("LTF_CONFIRMATION_MISSING")
    location = proposal.get("location")
    if direction == "LONG" and location != "DISCOUNT": violations.append("LONG_NOT_IN_DISCOUNT")
    if direction == "SHORT" and location != "PREMIUM": violations.append("SHORT_NOT_IN_PREMIUM")
    try:
        risk = Decimal(str(proposal.get("risk_fraction")))
        reward_risk = Decimal(str(proposal.get("reward_risk")))
    except Exception:
        violations.append("RISK_NUMBERS_INVALID")
    else:
        if risk <= 0 or risk > Decimal("0.01"): violations.append("RISK_EXCEEDS_ONE_PERCENT")
        if reward_risk < Decimal("2"): violations.append("REWARD_RISK_BELOW_TWO")
    if proposal.get("stop_beyond_invalidation") is not True:
        violations.append("STOP_NOT_BEYOND_INVALIDATION")
    if proposal.get("stop_widening_allowed") is not False:
        violations.append("STOP_WIDENING_NOT_FORBIDDEN")
    if regime == "TREND" and proposal.get("fibonacci_used") is True:
        ote = proposal.get("fibonacci_entry")
        if ote is None or not Decimal("0.618") <= Decimal(str(ote)) <= Decimal("0.786"):
            violations.append("TREND_FIBONACCI_OUTSIDE_OTE")
    if regime == "RANGE" and proposal.get("fibonacci_used") is True:
        violations.append("FIBONACCI_USED_IN_RANGE")
    return violations

