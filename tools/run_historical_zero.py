"""Research-only historical candle adapter for the existing AutoTrade ZERO engine.

No broker credentials, no network orders. This is an integration experiment:
the underlying moving-average simulation currently marks and executes at the
same observed candle close. It is NOT a valid next-bar historical profitability
test. Plan 10 Section 3 must qualify independent decision/fill timing.
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from pathlib import Path

from tools.historical_public_klines import (
    HistoricalArchiveError, INTERVAL_SECONDS, SYMBOL, _month_range,
)
from mvp.autotrade_mvp.simulation_session import run_autonomous_simulation


def _checked_month(root: Path, symbol: str, interval: str, month: str):
    base = root / "spot" / symbol / interval
    manifest_file = base / (month + ".manifest.json")
    rows_file = base / (month + ".ohlcv.csv")
    for f in (manifest_file, rows_file):
        if f.is_symlink() or not f.is_file():
            raise HistoricalArchiveError(f"missing/nonregular verified market archive for {month}")
    mraw = manifest_file.read_bytes()
    raw = rows_file.read_bytes()
    if len(mraw) > 20_000 or len(raw) > 80 * 1024 * 1024:
        raise HistoricalArchiveError("unbounded historical input")
    try:
        manifest = json.loads(mraw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HistoricalArchiveError("invalid source provenance manifest") from exc
    if type(manifest) is not dict or any(manifest.get(key) != expected for key, expected in (
        ("schema", "autotrade-spot-monthly-public-klines-v1"),
        ("symbol", symbol), ("interval", interval), ("month", month),
        ("market", "BINANCE_PUBLIC_ARCHIVE_SPOT_NOT_TRADING_PROVIDER"),
    )):
        raise HistoricalArchiveError("source identity conflicts with requested archive")
    if not manifest.get("usable_as_complete_causal_interval") or manifest.get("missing_candle_intervals") != 0:
        raise HistoricalArchiveError("historical source has missing intervals; fail closed")
    if sha256(raw).hexdigest() != manifest.get("normalized_csv_sha256"):
        raise HistoricalArchiveError("normalized historical archive digest mismatch")
    if not isinstance(manifest.get("source_zip_sha256"), str) or len(manifest["source_zip_sha256"]) != 64:
        raise HistoricalArchiveError("missing original archive identity")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HistoricalArchiveError("normalized CSV is not UTF-8") from exc
    from io import StringIO
    reader = csv.DictReader(StringIO(text))
    if reader.fieldnames != ["open_time_utc", "open", "high", "low", "close", "volume"]:
        raise HistoricalArchiveError("historical CSV header differs from canonical")
    prices = []
    dates = []
    for row in reader:
        if any(type(v) is not str for v in row.values()):
            raise HistoricalArchiveError("historical CSV row malformed")
        try:
            point = datetime.fromisoformat(row["open_time_utc"].replace("Z", "+00:00"))
            if point.tzinfo != timezone.utc:
                raise ValueError("timezone mismatch")
            for n in ("open", "high", "low", "close", "volume"):
                v = Decimal(row[n])
                if not v.is_finite() or v < 0 or (n != "volume" and v == 0):
                    raise ValueError("nonpositive OHLC or nonfinite amount")
            low, high = Decimal(row["low"]), Decimal(row["high"])
            if low > min(Decimal(row["open"]), Decimal(row["close"])) or high < max(Decimal(row["open"]), Decimal(row["close"])):
                raise ValueError("invalid OHLC order")
        except (InvalidOperation, TypeError, ValueError, KeyError) as exc:
            raise HistoricalArchiveError("malformed candle in normalized archive") from exc
        dates.append(point)
        prices.append(row["close"])
        if len(prices) > 10000:
            raise HistoricalArchiveError("one canonical ZERO run is capped at 10000 observations")
    if len(prices) != manifest.get("row_count"):
        raise HistoricalArchiveError("candle rows differ from verified manifest")
    if not prices or dates[0].isoformat().replace("+00:00", "Z") != manifest.get("start_open_time_utc") or dates[-1].isoformat().replace("+00:00", "Z") != manifest.get("end_open_time_utc"):
        raise HistoricalArchiveError("time range conflicts with manifest")
    step = INTERVAL_SECONDS[interval]
    if any(int((b-a).total_seconds()) != step for a,b in zip(dates, dates[1:])):
        raise HistoricalArchiveError("internal timestamp gap/reorder")
    return dates, prices, manifest


def load_verified_months(root: Path, symbol: str, interval: str, from_month: str, through_month: str):
    if SYMBOL.fullmatch(symbol) is None or interval not in INTERVAL_SECONDS:
        raise HistoricalArchiveError("invalid instrument selection")
    all_dates, all_prices, digests = [], [], []
    step = INTERVAL_SECONDS[interval]
    months = list(_month_range(from_month, through_month))
    for month in months:
        dates, prices, manifest = _checked_month(root, symbol, interval, month)
        if all_dates and dates[0] - all_dates[-1] != timedelta(seconds=step):
            raise HistoricalArchiveError("missing UTC time between adjacent monthly sources")
        all_dates.extend(dates)
        all_prices.extend(prices)
        digests.append({"month": month, "source_zip_sha256": manifest["source_zip_sha256"],
                        "normalized_csv_sha256": manifest["normalized_csv_sha256"]})
        if len(all_prices) > 10000:
            raise HistoricalArchiveError("over 10000 bars; use shorter separate *non-continuous* research runs")
    digest = sha256(json.dumps(
        {"market": "spot", "symbol": symbol, "interval": interval, "sources": digests,
         "observation_count": len(all_prices)}, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()
    return all_dates, all_prices, digest


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Run a frozen, unqualified candle-close ZERO experiment")
    parser.add_argument("--data-root", default="historical-data")
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--interval", choices=tuple(INTERVAL_SECONDS), default="1h")
    parser.add_argument("--from-month", default="2024-01")
    parser.add_argument("--through-month", required=True)
    parser.add_argument("--state-dir", default="historical-zero-state")
    parser.add_argument("--run-id", default="research-2024")
    parser.add_argument("--stop-after-episodes", type=int)
    args = parser.parse_args(argv)
    try:
        dates, prices, digest = load_verified_months(
            Path(args.data_root), args.symbol, args.interval, args.from_month, args.through_month
        )
        # The canonical simulator gets a complete immutable price cut for
        # restart/recovery, but its strategy receives only prices[:episode].
        # Observation time represents the completed bar, never its open.
        first_close = dates[0] + timedelta(seconds=INTERVAL_SECONDS[args.interval])
        result = run_autonomous_simulation(
            prices, args.state_dir, run_id=args.run_id + "-" + digest[:16],
            now=first_close.isoformat().replace("+00:00", "Z"),
            observation_interval_seconds=INTERVAL_SECONDS[args.interval],
            stop_after_episodes=args.stop_after_episodes,
        )
        print(json.dumps({
            "label": "INTERNAL_CANDLE_CLOSE_SAME_PRICE_EXPERIMENT_NOT_PERFORMANCE_EVIDENCE",
            "source_market": "BINANCE_HISTORICAL_SPOT_CANDLES",
            "original_symbol": args.symbol,
            "historical_start_utc": dates[0].isoformat().replace("+00:00", "Z"),
            "historical_end_utc": dates[-1].isoformat().replace("+00:00", "Z"),
            "observation_count": len(prices),
            "dataset_identity_sha256": digest,
            "important_limitations": [
                "Existing agent is currently a frozen moving-average strategy; this run does not prove learning.",
                "Existing engine executes at observed mark, not qualified next-bar open/spread/slippage.",
                "OHLCV has no order-book or tick-level execution truth.",
                "Virtual 1000 USD, zero network orders. Do not infer profitability from these fills.",
            ],
            "simulation": result,
        }, indent=2))
        return 2 if result.get("status") == "UNKNOWN" else 0
    except (HistoricalArchiveError, OSError, RuntimeError, ValueError, TypeError, ArithmeticError) as exc:
        print(f"AutoTrade historical ZERO experiment blocked: {type(exc).__name__}: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
