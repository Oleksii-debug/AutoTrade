"""Bounded public Binance archive collector for research, never a trading provider.

Consumes monthly SPOT Kline ZIP + companion CHECKSUM from Binance's documented
public archive. Produces locally held immutable CSV and provenance manifest.
Not a brokerage feed, execution oracle, full order book or profitable backtest.
Do not place private/public market archive output under source control.
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from io import StringIO
import json
import os
from pathlib import Path
import re
from tempfile import NamedTemporaryFile
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, HTTPRedirectHandler
from zipfile import ZipFile, BadZipFile
from io import BytesIO

BASE = "https://data.binance.vision/data/spot/monthly/klines"
DATASET_TERMS_URL = "https://github.com/binance/binance-public-data/blob/master/TERMS_AND_CONDITIONS.md"
DATASET_LICENSE = "CC BY-NC-SA 4.0 + Binance Vision Dataset Terms (NON-COMMERCIAL ONLY)"
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


class _RejectRedirects(HTTPRedirectHandler):
    """Reject redirects before following any untrusted destination."""
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        raise HistoricalArchiveError("public archive redirect denied (host pinned)")


def _fetch(url: str, limit: int) -> bytes:
    if not url.startswith(BASE + "/") or "?" in url or "#" in url:
        raise HistoricalArchiveError("unexpected archive URL")
    req = Request(url, headers={"User-Agent": "AutoTrade-public-history-provenance/1.0"})
    try:
        with build_opener(_RejectRedirects()).open(req, timeout=25) as response:
            if response.status != 200:
                raise HistoricalArchiveError(f"public archive HTTP status {response.status}")
            if response.geturl() != url:
                raise HistoricalArchiveError("archive download changed source URL")
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
        dt = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(microseconds=value * (1000 if factor == 1000 else 1))
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
        # SPOT source changed milliseconds to microseconds on 2025-01-01.
        expected_length = 13 if year < 2025 else 16
        if len(row[0]) != expected_length:
            raise HistoricalArchiveError("publisher timestamp unit mismatches archive month")
        if not row[6].isdigit() or len(row[6]) != expected_length:
            raise HistoricalArchiveError("candle close timestamp unit invalid")
        factor = 1000 if expected_length == 13 else 1_000_000
        if int(row[6]) != int(row[0]) + seconds * factor - 1:
            raise HistoricalArchiveError("candle close time mismatches interval")
        if current.year != year or current.month != mon:
            raise HistoricalArchiveError("archive candle outside requested month")
        if current.minute * 60 + current.second != 0 and interval in ("1h", "1d"):
            raise HistoricalArchiveError("hourly/daily timestamp not aligned")
        if current.second != 0 or current.microsecond != 0:
            raise HistoricalArchiveError("archive time not candle-boundary aligned")
        if int(current.timestamp()) % seconds != 0:
            raise HistoricalArchiveError("candle interval misalignment")
        values = [_decimal(row[n], name, zero_allowed=n == 5)
                  for n, name in ((1, "open"), (2, "high"), (3, "low"),
                                  (4, "close"), (5, "volume"))]
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
    # Coverage must include the entire UTC month; a partial listing is not
    # complete merely because every *observed* pair of rows is contiguous.
    from calendar import monthrange
    start_month = datetime(year, mon, 1, tzinfo=UTC)
    end_month = start_month + timedelta(days=monthrange(year, mon)[1])
    first = datetime.fromisoformat(normalized[0][0].replace("Z", "+00:00"))
    last = datetime.fromisoformat(normalized[-1][0].replace("Z", "+00:00"))
    missing_periods += int((first - start_month).total_seconds()) // seconds
    missing_periods += max(0, int((end_month - last).total_seconds()) // seconds - 1)
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
    for ancestor in (output, output / "spot", output / "spot" / symbol, series):
        if ancestor.is_symlink():
            raise HistoricalArchiveError("dataset directory may not be a symlink")
    series.mkdir(parents=True, exist_ok=True)
    manifest = {k: v for k, v in result.items() if k != "ohlcv_rows"}
    manifest["source_checksum_url"] = url + ".CHECKSUM"
    manifest["source_checksum_sha256"] = sha256(checksum).hexdigest()
    manifest["exchange"] = "BINANCE_SPOT"
    manifest["publisher"] = "Binance Vision public historical data"
    manifest["license"] = DATASET_LICENSE
    manifest["license_terms_url"] = DATASET_TERMS_URL
    manifest["rights_scope"] = "PERSONAL_NON_COMMERCIAL_RESEARCH_SIMULATION_ONLY"
    manifest["downloaded_at_utc"] = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    # The exact publication instant cannot be derived from the archive.
    manifest["published_at_utc"] = None
    manifest["time_zone"] = "UTC"
    manifest["verification"] = {
        "publisher_companion_sha256": "PASS",
        "zip_member_identity_and_size": "PASS",
        "ohlcv_and_timestamp_validation": "PASS",
        "missing_interval_count": result["missing_candle_intervals"],
    }
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
    if target.exists() and not target.is_symlink():
        previous_bytes = target.read_bytes()
        if len(previous_bytes) > 20_000:
            raise HistoricalArchiveError("existing manifest has excessive size")
        try:
            previous = json.loads(previous_bytes)
            recorded = previous.get("downloaded_at_utc")
            parsed = datetime.fromisoformat(recorded.replace("Z", "+00:00"))
            if parsed.tzinfo != UTC or parsed > datetime.now(UTC):
                raise ValueError("invalid prior download time")
        except (ValueError, AttributeError, TypeError, KeyError) as exc:
            raise HistoricalArchiveError("existing provenance manifest malformed") from exc
        manifest["downloaded_at_utc"] = recorded
    manifest_bytes = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
    # Keep accepted bytes immutable; after process loss between the two
    # exclusive writes, repair ONLY the missing file if its peer is identical.
    for file, payload in ((datafile, csv_bytes), (target, manifest_bytes)):
        if file.is_symlink() or series.is_symlink():
            raise HistoricalArchiveError("dataset path must not be a symlink")
        if file.exists() and (not file.is_file() or file.read_bytes() != payload):
            raise HistoricalArchiveError("archive revision conflict; do not silently overwrite accepted data")
    for file, payload in ((datafile, csv_bytes), (target, manifest_bytes)):
        if file.exists():
            continue
        # Complete private file first; atomically publish without overwriting.
        # A crash during write cannot expose a truncated canonical archive.
        temp_name = None
        try:
            with NamedTemporaryFile(mode="wb", prefix=".autotrade-history-",
                                    suffix=".partial", dir=series, delete=False) as stream:
                temp_name = stream.name
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temp_name, file)
            except FileExistsError:
                if file.is_symlink() or not file.is_file() or file.read_bytes() != payload:
                    raise HistoricalArchiveError("concurrent archive revision conflict")
        finally:
            if temp_name is not None:
                Path(temp_name).unlink(missing_ok=True)
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
