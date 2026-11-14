from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
import json

from research.autotrade_research.strategies.two_agent_weekly import Market
from tools.score_whitebit_calendar_forward import (
    ARMS, MARKETS, PROTOCOL_SHA, RATES, aggregate, digest, exit_index, impulse, load_phase,
    phase_clocks, quote_return, synchronized_quotes,
)


def quote(market, at, *, size="10", generation=1, update_id=50):
    return {
        "market": market, "bucket_epoch": int(at.timestamp()), "received_at": at.isoformat(),
        "bid_price": "99", "ask_price": "101", "bid_quantity": size, "ask_quantity": size,
        "generation": generation, "update_id": update_id, "source_sha256": "sha256:" + "1" * 64,
    }


def test_first_synchronized_quote_rejects_undersized_book_without_reselecting():
    boundary = datetime(2026, 11, 15, 17, tzinfo=UTC)
    early = [quote(market, boundary, size="1") for market in MARKETS]
    later = [quote(market, boundary + timedelta(seconds=1)) for market in MARKETS]
    entry = synchronized_quotes(later + early, boundary)
    exit_ = tuple(quote(market, boundary + timedelta(hours=4), update_id=51) for market in MARKETS)
    assert entry == tuple(early)
    assert quote_return(entry, exit_, 1, *RATES["base"][:2], 16) == (None, "INSUFFICIENT_DISPLAYED_SIZE")
    assert quote_return(tuple(later), exit_, 1, *RATES["base"][:2], 16)[0] < 0
    fresh_generation = (*exit_[:2], dict(exit_[2], generation=2))
    assert quote_return(tuple(later), fresh_generation, 1, *RATES["base"][:2], 16) == (
        None, "SOURCE_DISCONTINUITY",
    )


def test_partial_receipt_verifies_and_keeps_missing_triad_unknown(tmp_path):
    day = date(2026, 11, 15)
    clocks = phase_clocks(day, "evening")
    windows = {clock.isoformat(): [] for clock in clocks}
    windows[clocks[0].isoformat()] = [quote(MARKETS[0], clocks[0])]
    packet = {
        "schema": "autotrade-calendar-forward-bbo-window-v1", "study_id": "test",
        "protocol_sha256": PROTOCOL_SHA, "day_utc": day.isoformat(), "phase": "evening",
        "orders": 0, "network_requests": 0, "source": {"table": "execution_bbo"},
        "sealed_at_utc": (clocks[-1] + timedelta(seconds=120)).isoformat(),
        "windows": windows, "window_status": dict.fromkeys(windows, "SOURCE_GAP"),
    }
    packet["document_sha256"] = digest(packet)
    path = tmp_path / "captures" / day.isoformat()
    path.mkdir(parents=True)
    path.joinpath("evening.json").write_text(json.dumps(packet), encoding="utf-8")
    observations, identity = load_phase(tmp_path, {"study_id": "test"}, day, "evening")
    assert identity == packet["document_sha256"]
    assert observations == dict.fromkeys(clocks)


def test_impulse_and_exit_have_the_registered_full_bar_delay():
    count = 29 * 96
    opens = [Decimal(100)] * count
    closes = [Decimal(101)] * count
    sample = 28 * 96
    closes[sample + 2] = Decimal(103)
    entry = sample + 4
    closes[entry] = Decimal(98)
    closes[entry + 1] = Decimal(110)
    markets = tuple(Market(name, opens, closes, frozenset()) for name in MARKETS)
    assert impulse(markets, sample) == 1
    assert exit_index(markets, entry, 1, True, RATES["base"][2], RATES["base"][1]) == entry + 2
    assert exit_index(markets, entry, 1, False, RATES["base"][2], RATES["base"][1]) == entry + 16


def test_missing_trade_never_appears_as_cash_week():
    monday = date(2026, 11, 16)
    days = {monday + timedelta(days=i): dict.fromkeys(ARMS, (Decimal(0), "HOLD")) for i in range(7)}
    days[monday]["evening_stop"] = None, "MISSING_SYNCHRONIZED_QUOTES"
    result = aggregate(days, monday, monday + timedelta(days=7))
    assert result["evening_signal_attempts"] == 1 and result["qualified_evening_baskets"] == 0
    assert result["weekly"]["scored_complete_weeks"] == 0
    assert "INSUFFICIENT_COVERAGE" in result["reasons"]
