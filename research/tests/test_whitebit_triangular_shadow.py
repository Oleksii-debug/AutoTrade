from datetime import datetime, timedelta, timezone
from decimal import Decimal

from autotrade_research.strategies.triangular_shadow import BookTop, evaluate_cycle


NOW = datetime(2026, 11, 12, 5, tzinfo=timezone.utc)
DIGEST = "sha256:" + "1" * 64


def _book(market, bid, ask, quantity="100000", received_at=NOW):
    return BookTop(
        market=market, bid_price=bid, bid_quantity=quantity,
        ask_price=ask, ask_quantity=quantity, received_at=received_at,
        digest=DIGEST,
    )


def test_cycle_uses_executable_sides_three_fees_and_reserve():
    books = {
        "USDC_USDT": _book("USDC_USDT", "0.9999", "1.0000"),
        "USDC_USD": _book("USDC_USD", "1.0100", "1.0101"),
        "USDT_USD": _book("USDT_USD", "0.9999", "1.0000"),
    }
    result = evaluate_cycle(
        ("USDT", "USDC", "USD", "USDT"), books=books,
        taker_fees={market: "0.001" for market in books}, now=NOW,
        impact_reserve_bps="2",
    )
    assert [leg["side"] for leg in result["legs"]] == ["BUY", "SELL", "BUY"]
    assert Decimal(result["net_after_taker_fees_bps"]) > Decimal("2")
    assert result["qualified_for_shadow_observation"] is True
    assert result["network_orders"] is False
    assert result["trading_authority"] is False
    later = evaluate_cycle(
        ("USDT", "USDC", "USD", "USDT"), books=books,
        taker_fees={market: "0.001" for market in books},
        now=NOW + timedelta(seconds=1), impact_reserve_bps="2",
    )
    assert later["opportunity_key_sha256"] == result["opportunity_key_sha256"]
    assert later["result_sha256"] != result["result_sha256"]


def test_cycle_rejects_insufficient_top_depth_without_partial_fill():
    books = {
        "USDC_USDT": _book("USDC_USDT", "0.9999", "1.0000", quantity="1"),
        "USDC_USD": _book("USDC_USD", "1.0100", "1.0101"),
        "USDT_USD": _book("USDT_USD", "0.9999", "1.0000"),
    }
    result = evaluate_cycle(
        ("USDT", "USDC", "USD", "USDT"), books=books,
        taker_fees={market: "0.001" for market in books}, now=NOW,
    )
    assert result["status"] == "INSUFFICIENT_TOP_DEPTH"
    assert result["qualified_for_shadow_observation"] is False


def test_cycle_rejects_stale_or_skewed_books_even_when_arithmetic_is_positive():
    books = {
        "USDC_USDT": _book(
            "USDC_USDT", "0.9999", "1.0000", received_at=NOW - timedelta(seconds=4),
        ),
        "USDC_USD": _book("USDC_USD", "1.0100", "1.0101"),
        "USDT_USD": _book("USDT_USD", "0.9999", "1.0000"),
    }
    result = evaluate_cycle(
        ("USDT", "USDC", "USD", "USDT"), books=books,
        taker_fees={market: "0.001" for market in books}, now=NOW,
        maximum_age_seconds=3, maximum_skew_seconds=2,
    )
    assert Decimal(result["net_after_reserve_bps"]) > 0
    assert result["qualified_for_shadow_observation"] is False
