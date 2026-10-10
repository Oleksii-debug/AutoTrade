"""Bounded public Binance archive collector for research, never a trading provider.

Consumes monthly SPOT Kline ZIP + companion CHECKSUM from Binance's documented
public archive. Produces locally held immutable CSV and provenance manifest.
Not a brokerage feed, execution oracle, full order book or profitable backtest.
Do not place private/public market archive output under source control.
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from io import StringIO
import json
import os
from pathlib import Path
import re
from tempfile import NamedTemporaryFile
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zipfile import ZipFile, BadZipFile
from io import BytesIO

BASE = "https://data.binance.vision/data/spot/monthly/klines"
SYMBOL = re.compile(r"^[A-Z0-9]{5,20}$")
MONTH = re.compile(r"^(20\d{2})-(0[1-9]|1[0-2])$")
INTERVAL_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "1d": 86400}
MAX_ZIP_BYTES = 20 * 1024 * 1024
MAX_CSV_BYTES = 80 * 1024 * 1024
MAX_ROWS = 100_000
UTC = timezone.utc


class HistoricalArchiveError(ValueError):
    """Untrusted archive, data, chronology or provenance is invalid."""


def _utc_month(value: str) -> tuple[int, int]:
    m = MONTH.fullmatch(value) if type(value) is str else None
    if m is None:
        raise HistoricalArchiveError("month must be YYYY-MM")
    year, month = int(m[1]), int(m[2])
    if not 2024 <= year <= 2100:
        raise HistoricalArchiveError("requested month outside approved 2024+ window")
    return year, month


def _month_range(start: str, end: str):
    year, month = _utc_month(start)
    last_year, last_month = _utc_month(end)
    if (last_year, last_month) < (year, month):
        raise HistoricalArchiveError("through-month precedes from-month")
    while (year, month) <= (last_year, last_month):
        yield f"{year:04d}-{month:02d}"
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)


def _fetch(url: str, limit: int) -> bytes:
    if not url.startswith(BASE + "/") or "?" in url or "#" in url:
        raise HistoricalArchiveError("unexpected archive URL")
    req = Request(url, headers={"User-Agent": "AutoTrade-public-history-provenance/1.0"})
    try:
        with urlopen(req, timeout=25) as response:
            if response.status != 200:
                raise HistoricalArchiveError(f"public archive HTTP status {response.status}")
            if not response.geturl().startswith("https://"):
                raise HistoricalArchiveError("archive download must retain HTTPS")
            raw = response.read(limit + 1)
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise HistoricalArchiveError(f"public archive unavailable: {type(exc).__name__}") from exc
    if not raw or len(raw) > limit:
        raise HistoricalArchiveError("public archive empty or exceeds configured size budget")
    return raw


def _decimal(value: str, name: str, *, zero_allowed: bool) -> Decimal:
    if type(value) is not str or len(value) > 80:
        raise HistoricalArchiveError(f"{name} must be a bounded decimal")
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise HistoricalArchiveError(f"{name} is not decimal") from exc
    if not number.is_finite() or number < 0 or (not zero_allowed and number == 0):
        raise HistoricalArchiveError(f"{name} must be finite and positive")
    return number


def _timestamp(raw: str) -> datetime:
    if type(raw) is not str or not raw.isdigit() or len(raw) not in (13, 16):
        raise HistoricalArchiveError("archive timestamp must use 13-digit milliseconds or 16-digit microseconds")
    value = int(raw)
    factor = 1000 if len(raw) == 13 else 1_000_000
    try:
        dt = datetime.fromtimestamp(value / factor, tz=UTC)
    except (OverflowError, OSError, ValueError) as exc:
        raise HistoricalArchiveError("timestamp out of datetime bounds") from exc
    if dt.year < 2024 or dt.year > 2100:
        raise HistoricalArchiveError("archive timestamp not in 2024+ support")
    return dt


def _validated_rows(data: bytes, *, month: str, interval: str) -> tuple[list[tuple[str, str, str, str, str, str]], int]:
    if not data or len(data) > MAX_CSV_BYTES:
        raise HistoricalArchiveError("CSV size budget exceeded")
    try:
        payload = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise HistoricalArchiveError("archive CSV not UTF-8") from exc
    year, mon = _utc_month(month)
    seconds = INTERVAL_SECONDS[interval]
    normalized = []
    prev = None
    missing_periods = 0
    for row in csv.reader(StringIO(payload, newline="")):
        if len(row) != 12:
            raise HistoricalArchiveError("spot Kline archive row must have exactly 12 fields")
        current = _timestamp(row[0])
        if current.year != year or current.month != mon:
            raise HistoricalArchiveError("archive candle outside requested month")
        if current.minute * 60 + current.second != 0 and interval in ("1h", "1d"):
            raise HistoricalArchiveError("hourly/daily timestamp not aligned")
        if current.second != 0 or current.microsecond != 0:
            raise HistoricalArchiveError("archive time not candle-boundary aligned")
        if int(current.timestamp()) % seconds != 0:
            raise HistoricalArchiveError("candle interval misalignment")
        values = [_decimal(row[n], name, zero_allowed=n == 5)
                  for n, name in zip(range(1, 7), ("open", "high", "low", "close", "volume", "unused"))][:5]
        opening, high, low, closing, volume = values
        if low > min(opening, closing) or high < max(opening, closing) or low > high:
            raise HistoricalArchiveError("impossible OHLC relation")
        if prev is not None:
            delta = int((current - prev).total_seconds())
            if delta <= 0 or delta % seconds:
                raise HistoricalArchiveError("duplicate, reversed or non-cadence candle")
            if delta > seconds:
                missing_periods += delta // seconds - 1
        normalized.append((
            current.isoformat().replace("+00:00", "Z"),
            str(opening), str(high), str(low), str(closing), str(volume),
        ))
        prev = current
        if len(normalized) > MAX_ROWS:
            raise HistoricalArchiveError("too many candles")
    if not normalized:
        raise HistoricalArchiveError("empty candle archive")
    return normalized, missing_periods


def verify_archive(raw: bytes, checksum: bytes, *, symbol: str, interval: str, month: str) -> dict:
    if type(symbol) is not str or SYMBOL.fullmatch(symbol) is None:
        raise HistoricalArchiveError("symbol must be uppercase bounded pair")
    if interval not in INTERVAL_SECONDS:
        raise HistoricalArchiveError("unsupported candle interval")
    _utc_month(month)
    filename = f"{symbol}-{interval}-{month}.zip"
    try:
        check = checksum.decode("ascii").strip()
    except UnicodeDecodeError as exc:
        raise HistoricalArchiveError("CHECKSUM must be ASCII") from exc
    parts = check.split()
    if len(parts) != 2 or not re.fullmatch(r"[a-fA-F0-9]{64}", parts[0]) or parts[1].lstrip("*") != filename:
        raise HistoricalArchiveError("malformed or mismatched companion CHECKSUM")
    digest = sha256(raw).hexdigest()
    if digest.lower() != parts[0].lower():
        raise HistoricalArchiveError("archive SHA-256 does not match publisher CHECKSUM")
    if len(raw) > MAX_ZIP_BYTES:
        raise HistoricalArchiveError("ZIP size exceeds budget")
    try:
        with ZipFile(BytesIO(raw)) as z:
            entries = z.infolist()
            if len(entries) != 1 or entries[0].filename != filename[:-4] + ".csv":
                raise HistoricalArchiveError("ZIP must have exactly its expected CSV, no path or extra entries")
            if entries[0].file_size > MAX_CSV_BYTES or entries[0].compress_size == 0:
                raise HistoricalArchiveError("unbounded archive member")
            if entries[0].file_size > max(1, entries[0].compress_size) * 200:
                raise HistoricalArchiveError("archive compression ratio unsafe")
            data = z.read(entries[0])
    except (BadZipFile, OSError, RuntimeError) as exc:
        raise HistoricalArchiveError("invalid ZIP") from exc
    rows, gaps = _validated_rows(data, month=month, interval=interval)
    return {
        "schema": "autotrade-spot-monthly-public-klines-v1",
        "market": "BINANCE_PUBLIC_ARCHIVE_SPOT_NOT_TRADING_PROVIDER",
        "symbol": symbol, "interval": interval, "month": month,
        "source_url": f"{BASE}/{symbol}/{interval}/{filename}",
        "source_zip_sha256": digest,
        "source_csv_sha256": sha256(data).hexdigest(),
        "row_count": len(rows), "missing_candle_intervals": gaps,
        "usable_as_complete_causal_interval": gaps == 0,
        "start_open_time_utc": rows[0][0], "end_open_time_utc": rows[-1][0],
        "ohlcv_rows": rows,
    }


def collect_month(*, symbol: str, interval: str, month: str, output: Path, fetch=_fetch) -> dict:
    if type(symbol) is not str or SYMBOL.fullmatch(symbol) is None or interval not in INTERVAL_SECONDS:
        raise HistoricalArchiveError("invalid public archive selection")
    _utc_month(month)
    filename = f"{symbol}-{interval}-{month}.zip"
    url = f"{BASE}/{symbol}/{interval}/{filename}"
    raw = fetch(url, MAX_ZIP_BYTES)
    checksum = fetch(url + ".CHECKSUM", 4096)
    result = verify_archive(raw, checksum, symbol=symbol, interval=interval, month=month)
    series = output / "spot" / symbol / interval
    series.mkdir(parents=True, exist_ok=True)
    manifest = {k: v for k, v in result.items() if k != "ohlcv_rows"}
    manifest["source_checksum_url"] = url + ".CHECKSUM"
    manifest["untrusted_source_notice"] = "Historical candles only, no live quotes, spread, ticks, order book or fill proof"
    target = series / (month + ".manifest.json")
    datafile = series / (month + ".ohlcv.csv")
    lines = [["open_time_utc", "open", "high", "low", "close", "volume"], *result["ohlcv_rows"]]
    from io import StringIO as _IO
    stream = _IO()
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerows(lines)
    csv_bytes = stream.getvalue().encode("utf-8")
    manifest["normalized_csv_sha256"] = sha256(csv_bytes).hexdigest()
    manifest_bytes = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if target.exists() or datafile.exists():
        if target.is_file() and datafile.is_file() and target.read_bytes() == manifest_bytes and datafile.read_bytes() == csv_bytes:
            return manifest
        raise HistoricalArchiveError("archive revision conflict; do not silently overwrite accepted data")
    # Exclusive creation: fail closed on collision, never delete previously accepted data.
    for file, payload in ((datafile, csv_bytes), (target, manifest_bytes)):
        with file.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    return manifest


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Fetch and verify bounded monthly public historical spot candles")
    parser.add_argument("--symbols", default="BTCUSDT,ETHUSDT")
    parser.add_argument("--interval", choices=tuple(INTERVAL_SECONDS), default="1h")
    parser.add_argument("--from-month", default="2024-01")
    parser.add_argument("--through-month", required=True,
                        help="last fully published month YYYY-MM; current incomplete month is not an accepted monthly archive")
    parser.add_argument("--output", default="historical-data")
    args = parser.parse_args(argv)
    try:
        symbols = args.symbols.split(",")
        if len(symbols) != len(set(symbols)) or not 1 <= len(symbols) <= 10:
            raise HistoricalArchiveError("duplicate or oversized symbol selection")
        months = list(_month_range(args.from_month, args.through_month))
        if len(months) * len(symbols) > 600:
            raise HistoricalArchiveError("explicit dataset size budget exceeded")
        for sym in symbols:
            if SYMBOL.fullmatch(sym) is None:
                raise HistoricalArchiveError("invalid symbol")
            for month in months:
                result = collect_month(symbol=sym, interval=args.interval, month=month, output=Path(args.output))
                print(f"{sym} {month}: {result['row_count']} rows; gaps={result['missing_candle_intervals']}; source_sha256={result['source_zip_sha256']}")
        return 0
    except (HistoricalArchiveError, OSError) as exc:
        print(f"AutoTrade historical public data: blocked — {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
