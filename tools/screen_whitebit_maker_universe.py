#!/usr/bin/env python3
"""One-shot public-only discovery of economically admissible maker shadows."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from pathlib import Path

from research.autotrade_research.artifacts.durable_publish import atomic_write_json
from research.autotrade_research.integrations.whitebit_public import (
    PublicDataError,
    WhiteBitPublicClient,
)
from research.autotrade_research.strategies.maker_universe import (
    SCHEMA,
    evaluate_market,
    rank_candidates,
)


DEFAULT_EXCLUDED = frozenset({"PEPE_USDT", "ENA_USDT"})
STABLE_BASES = frozenset({"USDT", "USDC", "DAI", "FDUSD", "TUSD", "USDE", "PYUSD"})


def _decimal(value: str, name: str) -> Decimal:
    try:
        result = Decimal(value)
    except InvalidOperation:
        raise ValueError(f"{name} is invalid") from None
    if not result.is_finite():
        raise ValueError(f"{name} is invalid")
    return result


def screen(
    *,
    client: WhiteBitPublicClient,
    now: datetime,
    notional_quote: str = "100",
    minimum_quote_volume_24h: str = "100000",
    prefilter_limit: int = 16,
    candidate_limit: int = 4,
    excluded_markets: frozenset[str] = DEFAULT_EXCLUDED,
) -> dict[str, object]:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("maker-universe clock must be timezone-aware")
    if not 1 <= candidate_limit <= prefilter_limit <= 32:
        raise ValueError("maker-universe limits are invalid")
    minimum_volume = _decimal(minimum_quote_volume_24h, "minimum_quote_volume_24h")
    if minimum_volume < 0 or type(excluded_markets) is not frozenset:
        raise ValueError("maker-universe prefilter is invalid")

    instruments = {
        item.market: item for item in client.instruments()
        if item.instrument_type == "spot" and item.quote_asset == "USDT"
        and item.base_asset not in STABLE_BASES and item.trades_enabled
    }
    tickers = []
    for ticker in client.market_activities():
        if ticker.market not in instruments or ticker.is_frozen:
            continue
        volume = _decimal(ticker.quote_volume, "quote_volume")
        if volume >= minimum_volume:
            tickers.append(ticker)
    tickers.sort(key=lambda ticker: (-Decimal(ticker.quote_volume), ticker.market))

    evaluated: list[dict[str, object]] = []
    errors: list[dict[str, str]] = []
    for ticker in tickers[:prefilter_limit]:
        if ticker.market in excluded_markets:
            continue
        try:
            book = client.instrument_orderbook(ticker.market)
            trades = client.recent_trades(ticker.market)
            # The bounded public reads occur after the cycle clock was captured.
            # Advance only to observed receipt times so fresh responses are not
            # falsely rejected as future data while deterministic tests retain
            # an injected lower clock bound.
            evaluation_now = max(now, ticker.received_at, book.received_at, trades.received_at)
            evaluated.append(evaluate_market(
                instruments[ticker.market], ticker, book, trades,
                now=evaluation_now, notional_quote=notional_quote,
            ))
        except (PublicDataError, ValueError) as error:
            errors.append({
                "market": ticker.market,
                "error": type(error).__name__,
                "reason": str(error)[:128],
            })

    candidates = rank_candidates(evaluated, limit=candidate_limit)
    report = {
        "schema": SCHEMA,
        "generated_at": now.astimezone(timezone.utc).isoformat(),
        "prefilter_limit": prefilter_limit,
        "candidate_limit": candidate_limit,
        "minimum_quote_volume_24h": format(minimum_volume, "f"),
        "excluded_prior_failed_markets": sorted(excluded_markets),
        "evaluated_count": len(evaluated),
        "candidate_count": len(candidates),
        "candidates": candidates,
        "evaluated": evaluated,
        "isolated_errors": errors,
        "selection_policy": "SPREAD_MINUS_TWO_MAKER_FEES_MINUS_5BPS_WITH_DEPTH_ACTIVITY_NEUTRAL_FLOW",
        "automatic_promotion": False,
        "research_only": True,
        "network_orders": False,
        "trading_authority": False,
        "paid_model_calls": 0,
        "profit_guaranteed": False,
    }
    report["result_sha256"] = sha256(json.dumps(
        report, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode("ascii")).hexdigest()
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--notional-quote", default="100")
    parser.add_argument("--minimum-quote-volume-24h", default="100000")
    parser.add_argument("--prefilter-limit", type=int, default=16)
    parser.add_argument("--candidate-limit", type=int, default=4)
    parser.add_argument("--exclude-market", action="append", default=[])
    args = parser.parse_args(argv)
    excluded = DEFAULT_EXCLUDED | frozenset(args.exclude_market)
    report = screen(
        client=WhiteBitPublicClient(), now=datetime.now(timezone.utc),
        notional_quote=args.notional_quote,
        minimum_quote_volume_24h=args.minimum_quote_volume_24h,
        prefilter_limit=args.prefilter_limit,
        candidate_limit=args.candidate_limit,
        excluded_markets=excluded,
    )
    args.state_dir.mkdir(parents=True, exist_ok=True)
    if args.state_dir.is_symlink() or not args.state_dir.is_dir():
        raise SystemExit("state-dir must be an ordinary directory")
    atomic_write_json(args.state_dir / "checkpoint.json", report)
    atomic_write_json(args.state_dir / f"run-{report['result_sha256']}.json", report)
    print(json.dumps({
        "schema": report["schema"],
        "result_sha256": report["result_sha256"],
        "evaluated_count": report["evaluated_count"],
        "candidate_count": report["candidate_count"],
        "network_orders": False,
        "trading_authority": False,
    }, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
