from datetime import UTC, datetime, timedelta
from decimal import Decimal
import json

from tools.run_whitebit_observed_portfolio import SOURCE_SCHEMA, advance, new_state


START = datetime(2026, 11, 12, 18, tzinfo=UTC)


def observation(minute, market, expected=".006"):
    at = START + timedelta(minutes=minute)
    return {"entry_boundary_epoch": int(at.timestamp()),
            "quote_received_at": (at + timedelta(seconds=1)).isoformat(),
            "entry_quote_sha256": f"{market}:{minute}",
            "learner_proposal": {"status": "ESTIMATED", "action": "LONG",
                "model_sha256": market, "expected_net_equity_returns": {"LONG": expected}}}


def outcome(entry, minute):
    at = START + timedelta(minutes=minute)
    return {**entry, "kind": "OBSERVED_FIXED_TIME_EXECUTION_OUTCOME",
            "exit_boundary_epoch": int(at.timestamp()), "exit_quote_sha256": f"exit:{minute}",
            "exit_quote_received_at": (at + timedelta(seconds=1)).isoformat(),
            "outcomes": {"LONG": {"net_equity_return": ".006"}}}


def apply(state, minute, btc=None, eth=None, btc_labels=(), eth_labels=(), delay=5):
    at = START + timedelta(minutes=minute)
    return advance(state, {"schema": SOURCE_SCHEMA,
        "processed_bar_open_epoch": int(at.timestamp()) - 900,
        "observed_at": (at + timedelta(seconds=2)).isoformat(),
        "additional_market_research": {
            "BTC_PERP": {"observation": btc, "settled": btc_labels},
            "ETH_PERP": {"observation": eth, "settled": eth_labels}}},
        now=at + timedelta(seconds=delay))


def test_portfolio_waits_for_later_quote_and_compounds_complete_hour_across_restart():
    state = new_state(START - timedelta(minutes=1))
    apply(state, 0, btc=observation(0, "BTC", ".005"), eth=observation(0, "ETH"))
    assert state["queued"]["market"] == "ETH_PERP"
    assert state["pending"] is None

    state = json.loads(json.dumps(state))
    entry_eth = observation(15, "ETH")
    apply(state, 15, eth=entry_eth)
    assert state["pending"]["entry_quote_sha256"] == "ETH:15"
    apply(state, 30, btc=observation(30, "BTC", ".007"))
    assert state["pending"]["market"] == "ETH_PERP"
    assert state["queued"]["market"] == "BTC_PERP"

    state = json.loads(json.dumps(state))
    entry_btc = observation(45, "BTC")
    apply(state, 45, btc=entry_btc, eth_labels=[outcome(entry_eth, 45)])
    assert state["settled_count"] == 1
    assert state["pending"]["market"] == "BTC_PERP"
    apply(state, 75, btc_labels=[outcome(entry_btc, 75)])
    assert state["settled_count"] == 2
    assert state["evaluation"]["hourly"]["target_hits"] == 1
    assert Decimal(state["evaluation"]["compound_complete_hour_return"]) == Decimal(".012036")
    assert not apply(state, 75, btc_labels=[outcome(entry_btc, 75)])


def test_portfolio_excludes_late_decisions_same_quotes_and_missing_outcomes():
    state = new_state(START - timedelta(minutes=1))
    apply(state, 0, btc=observation(0, "BTC"), delay=120)
    assert state["queued"] is None

    state = new_state(START - timedelta(minutes=1))
    apply(state, 0, btc=observation(0, "BTC"))
    same_quote = {**observation(15, "BTC"), "entry_quote_sha256": "BTC:0"}
    apply(state, 15, btc=same_quote)
    assert state["pending"] is None
    assert state["unmatched_count"] == 1

    apply(state, 30, btc=observation(30, "BTC"))
    apply(state, 45, btc=observation(45, "BTC"))
    apply(state, 75)
    assert state["unmatched_count"] == 2
    assert state["settled_count"] == 0
    assert "evaluation" not in state

