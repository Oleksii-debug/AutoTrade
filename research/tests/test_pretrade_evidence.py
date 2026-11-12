from decimal import Decimal

from research.autotrade_research.strategies.pretrade_evidence import (
    PreTradeThesis, evaluate_pretrade,
)


def thesis(**changes):
    values = dict(direction="LONG", regime="TREND", causal_data_complete=True,
        regime_confirmed=True, location_confirmed=True, trigger_confirmed=True,
        entry_price="100", invalidation_price="99", target_price="103",
        round_trip_cost_bps="15", minimum_reward_risk="2",
        minimum_net_reward_cost_multiple="1.5")
    values.update(changes)
    return PreTradeThesis(**values)


def test_complete_after_cost_thesis_enters():
    value = evaluate_pretrade(thesis())
    assert value["decision"] == "ENTER"
    assert value["failure_reasons"] == []
    assert Decimal(value["after_cost_reward_risk"]) >= 2


def test_signal_without_regime_or_trigger_is_hold():
    value = evaluate_pretrade(thesis(regime_confirmed=False,
        trigger_confirmed=False))
    assert value["decision"] == "HOLD"
    assert value["failure_reasons"] == ["REGIME_CONFIRMED", "TRIGGER_CONFIRMED"]


def test_gross_profit_that_fails_cost_hurdle_is_hold():
    value = evaluate_pretrade(thesis(invalidation_price="99.95",
        target_price="100.2", round_trip_cost_bps="15"))
    assert value["decision"] == "HOLD"
    assert "AFTER_COST_REWARD_RISK" in value["failure_reasons"]


def test_live_market_requires_current_microstructure_confirmation():
    missing = evaluate_pretrade(thesis(live_market=True))
    conflict = evaluate_pretrade(thesis(live_market=True,
        microstructure_status="CONFLICT"))
    admitted = evaluate_pretrade(thesis(live_market=True,
        microstructure_status="CONFIRMED"))
    assert "LIVE_MICROSTRUCTURE_CONFIRMED" in missing["failure_reasons"]
    assert conflict["decision"] == "HOLD"
    assert admitted["decision"] == "ENTER"

