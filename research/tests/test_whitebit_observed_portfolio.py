from datetime import UTC, datetime, timedelta
from decimal import Decimal
import json

from tools.run_whitebit_observed_portfolio import LABEL_POLICY, SOURCE_SCHEMA, advance, new_state


START = datetime(2026, 11, 12, 18, tzinfo=UTC)


def observation(minute, market, expected=".006"):
    at = START + timedelta(minutes=minute)
    return {"entry_boundary_epoch": int(at.timestamp()),
            "quote_received_at": (at + timedelta(seconds=1)).isoformat(),
            "entry_quote_sha256": f"{market}:{minute}",
            "technical": {"features": {"atr": "2", "close": "100", "band_mid": "100"}},
            "learner_proposal": {"status": "ESTIMATED", "action": "LONG",
                "model_sha256": market, "expected_net_equity_returns": {"LONG": expected}}}


def outcome(entry, minute, *, long=".006", short="-.006"):
    at = START + timedelta(minutes=minute)
    return {**entry, "kind": "OBSERVED_FIXED_TIME_EXECUTION_OUTCOME",
            "exit_boundary_epoch": int(at.timestamp()), "exit_quote_sha256": f"exit:{minute}",
            "exit_quote_received_at": (at + timedelta(seconds=1)).isoformat(),
            "outcome_id": f"{entry['entry_quote_sha256']}:{minute}",
            "outcomes": {"LONG": {"net_equity_return": long},
                         "SHORT": {"net_equity_return": short}}}


def apply(state, minute, btc=None, eth=None, btc_labels=(), eth_labels=(), delay=5):
    at = START + timedelta(minutes=minute)
    return advance(state, {"schema": SOURCE_SCHEMA,
        "processed_bar_open_epoch": int(at.timestamp()) - 900,
        "observed_at": (at + timedelta(seconds=2)).isoformat(),
        "additional_market_research": {
            "BTC_PERP": {"observation": btc, "settled": btc_labels},
            "ETH_PERP": {"observation": eth, "settled": eth_labels}}},
        now=at + timedelta(seconds=delay))


def seed_training(state, market, count, *, long, short):
    memory = {"decisions": [], "recent": []}
    state.setdefault("delayed_training", {})[market] = memory
    for index in range(count):
        decision = -60 - 30 * index
        entry = observation(decision + 15, market)
        row = outcome(entry, decision + 45, long=long, short=short)
        row["decision_boundary_epoch"] = int((START + timedelta(minutes=decision)).timestamp())
        memory["recent"].append(row)


def test_portfolio_waits_for_later_quote_and_compounds_complete_hour_across_restart():
    state = new_state(START - timedelta(minutes=1))
    seed_training(state, "BTC_PERP", 8, long=".005", short="-.005")
    seed_training(state, "ETH_PERP", 8, long=".006", short="-.006")
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




def test_portfolio_fits_frozen_features_to_later_entry_without_reusing_source_proposal():
    state = new_state(START - timedelta(minutes=1))
    seed_training(state, "BTC_PERP", 7, long="-.004", short=".003")
    decision = observation(0, "BTC")
    apply(state, 0, btc=decision)
    assert state["queued"]["action"] == "HOLD"
    later = observation(15, "BTC")
    later["technical"]["features"]["close"] = "130"  # execution features differ
    apply(state, 15, btc=later)
    apply(state, 30, btc_labels=[outcome(decision, 30, long=".5")])
    assert len(state["delayed_training"]["BTC_PERP"]["recent"]) == 7

    state = json.loads(json.dumps(state))
    future = outcome(later, 45, long=".5")
    future["exit_quote_received_at"] = (START + timedelta(minutes=45, seconds=3)).isoformat()
    eligible = outcome(later, 45, long="-.01", short=".008")
    apply(state, 45, btc_labels=[future, eligible])
    assert state["recent"][-1]["exit_quote_received_at"] == eligible["exit_quote_received_at"]
    labeled = state["delayed_training"]["BTC_PERP"]["recent"][-1]
    assert labeled["decision_quote_sha256"] == "BTC:0"
    assert labeled["entry_quote_sha256"] == "BTC:15"
    assert labeled["technical"]["features"]["close"] == "100"
    assert labeled["outcomes"]["LONG"]["net_equity_return"] == "-.01"

    apply(state, 60, btc=observation(60, "BTC"))
    assert state["queued"]["action"] == "SHORT"
    assert state["queued"]["label_policy"] == LABEL_POLICY
    assert state["queued"]["training_outcome_count"] == 8



def test_portfolio_finishes_legacy_hold_without_combining_it_with_new_policy():
    state = new_state(START - timedelta(minutes=1))
    apply(state, 0, btc=observation(0, "BTC"))
    state["queued"].pop("label_policy")  # persisted by the previous version
    legacy_entry = observation(15, "BTC")
    apply(state, 15, btc=legacy_entry)
    apply(state, 30, btc=observation(30, "BTC"))
    aligned_entry = observation(45, "BTC")
    apply(state, 45, btc=aligned_entry, btc_labels=[outcome(legacy_entry, 45)])
    apply(state, 75, btc_labels=[outcome(aligned_entry, 75)])
    assert state["settled_count"] == 2
    assert state["evaluation"]["hourly"]["complete_windows"] == 0
    assert state["evaluation"]["legacy_hourly"]["complete_windows"] == 0



def test_two_market_observer_excludes_sol_and_persists_capacity_result():
    state = new_state(START - timedelta(minutes=1))
    seed_training(state, "SOL_PERP", 8, long=".02", short="-.01")
    seed_training(state, "BTC_PERP", 8, long=".01", short="-.01")
    market_pair = ("BTC_PERP", "ETH_PERP")
    checks = []

    def check_capacity(row):
        checks.append(row["market"])
        return False

    def step(minute, btc=None, settled=()):
        at = START + timedelta(minutes=minute)
        event = {
            "schema": SOURCE_SCHEMA, "processed_bar_open_epoch": int(at.timestamp()) - 900,
            "observed_at": (at + timedelta(seconds=2)).isoformat(),
            "execution_observation": observation(minute, "SOL"),
            "additional_market_research": {"BTC_PERP": {"observation": btc, "settled": settled}},
        }
        assert advance(state, event, now=at + timedelta(seconds=3),
                       markets=market_pair, capacity_check=check_capacity)

    step(0, observation(0, "BTC"))
    assert state["queued"]["market"] == "BTC_PERP"
    assert state["queued"]["action"] == "LONG"
    entry = observation(15, "BTC")
    step(15, entry)
    step(45, settled=[outcome(entry, 45)])
    assert checks == ["BTC_PERP"]
    assert state["recent"][-1]["observed_capacity_check"] is False
    assert Decimal(state["recent"][-1]["net_equity_return"]) == Decimal(".006")
    assert not state["delayed_training"]["SOL_PERP"]["decisions"]
