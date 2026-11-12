from datetime import datetime, timedelta, timezone
from decimal import Decimal

from research.autotrade_research.integrations.whitebit_public import (
    PublicBook, PublicInstrument, PublicTrade, PublicTradeBatch,
)
from research.autotrade_research.strategies.maker_shadow import (
    advance_evidence_wave, evaluate_quote, open_quote, quote_admission,
)


NOW = datetime(2026, 11, 12, tzinfo=timezone.utc)


def instrument():
    return PublicInstrument(
        market="PEPE_USDT", base_asset="PEPE", quote_asset="USDT",
        instrument_type="SPOT", maker_fee_rate="0.001", taker_fee_rate="0.001",
        minimum_quantity="1", minimum_notional="1", tick_size="0.00000001",
        step_size="1", trades_enabled=True,
    )


def book(*, bid="0.00000400", ask="0.00000402", queue="1000000", at=NOW):
    return PublicBook(
        market="PEPE_USDT", source_time=at, received_at=at,
        bids=((bid, queue),), asks=((ask, queue),), raw=b"book",
        source_uri="https://whitebit.com/api/v4/public/orderbook/PEPE_USDT?limit=100&level=0",
    )


def batch(*trades):
    return PublicTradeBatch(
        market="PEPE_USDT", received_at=NOW + timedelta(seconds=30),
        trades=tuple(trades), raw=b"trades",
        source_uri="https://whitebit.com/api/v4/public/trades/PEPE_USDT",
    )


def trade(trade_id, *, side, price, quantity, seconds=10):
    return PublicTrade(
        market="PEPE_USDT", trade_id=trade_id, price=price,
        base_quantity=quantity,
        quote_quantity=format(Decimal(price) * Decimal(quantity), "f"),
        trade_at=NOW + timedelta(seconds=seconds), side=side, rpi=False,
    )


def test_both_sides_require_queue_ahead_and_settle_after_costs():
    quote = open_quote(book(), instrument(), batch(), notional_quote="4")
    assert evaluate_quote(
        quote, book(at=NOW + timedelta(seconds=10)),
        batch(
            trade(2, side="sell", price="0.00000400", quantity="2500000"),
            trade(3, side="buy", price="0.00000402", quantity="2500000"),
        ), now=NOW + timedelta(seconds=10),
    )["outcome"] == "BOTH_SIDES_FILLED"


def test_one_sided_fill_is_forced_out_with_taker_fee_and_impact():
    quote = open_quote(book(), instrument(), batch(), notional_quote="4")
    result = evaluate_quote(
        quote,
        book(bid="0.00000395", ask="0.00000397", at=NOW + timedelta(seconds=61)),
        batch(trade(2, side="sell", price="0.00000400", quantity="2500000")),
        now=NOW + timedelta(seconds=61), maximum_age_seconds=60,
    )
    assert result["outcome"] == "BID_ONLY_FORCED_EXIT"
    assert Decimal(result["net_pnl_quote"]) < 0
    assert result["network_orders"] is False
    assert result["trading_authority"] is False


def test_insufficient_queue_volume_remains_open_before_expiry():
    quote = open_quote(book(), instrument(), batch(), notional_quote="4")
    result = evaluate_quote(
        quote, book(at=NOW + timedelta(seconds=10)),
        batch(trade(2, side="sell", price="0.00000400", quantity="100")),
        now=NOW + timedelta(seconds=10),
    )
    assert result["status"] == "OPEN"
    assert result["bid_filled"] is False


def test_negative_discovery_wave_stops_new_research_actions():
    state = {"research_phase": "DISCOVERY"}
    for _ in range(4):
        state = advance_evidence_wave(
            state, net_pnl_quote="-0.1", outcome="BID_ONLY_FORCED_EXIT",
            target_cycles=4,
        )
    assert state["research_phase"] == "RESEARCH_FILTERED"
    assert state["latest_wave_analysis"]["passed_after_cost_gate"] is False
    assert state["latest_wave_analysis"]["automatic_promotion"] is False


def test_positive_discovery_requires_separate_positive_evaluation_wave():
    state = {"research_phase": "DISCOVERY"}
    for _ in range(4):
        state = advance_evidence_wave(
            state, net_pnl_quote="0.1", outcome="BOTH_SIDES_FILLED",
            target_cycles=4,
        )
    assert state["research_phase"] == "EVALUATING"
    for _ in range(4):
        state = advance_evidence_wave(
            state, net_pnl_quote="0.1", outcome="BOTH_SIDES_FILLED",
            target_cycles=4,
        )
    assert state["research_phase"] == "QUALIFIED_SHADOW"
    assert state["latest_wave_analysis"]["automatic_promotion"] is False


def test_filtered_wave_rejects_directional_flow_and_insufficient_spread():
    directional = quote_admission(
        book(), instrument(), batch(
            trade(2, side="buy", price="0.00000402", quantity="5000000"),
        ),
    )
    assert directional["admitted"] is False
    assert directional["reason"] == "DIRECTIONAL_FLOW"
    neutral_but_too_narrow = quote_admission(
        book(bid="0.00000400", ask="0.000004005"), instrument(), batch(),
    )
    assert neutral_but_too_narrow["admitted"] is False
    assert neutral_but_too_narrow["reason"] == "SPREAD_BELOW_HURDLE"


def test_failed_filtered_wave_exhausts_the_single_adaptive_trial():
    state = {"research_phase": "RESEARCH_FILTERED"}
    for _ in range(4):
        state = advance_evidence_wave(
            state, net_pnl_quote="-0.1", outcome="ASK_ONLY_FORCED_EXIT",
            target_cycles=4,
        )
    assert state["research_phase"] == "RESEARCH_ONLY"



