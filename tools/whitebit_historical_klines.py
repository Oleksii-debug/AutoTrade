"""Durable credential-free WhiteBIT historical candle acquisition.

The collector reuses the strict production-public WhiteBIT parser and writes the
same normalized OHLCV column contract as AutoTrade's existing historical
learner.  WhiteBIT REST responses have a different trust model from the signed
Binance archive, so their manifests remain source-specific and are never
relabeled as Binance evidence.

Only explicitly bounded, completed fixed-width candles are accepted.  Page
envelopes are immutable and make an interrupted run resumable without fetching
already accepted pages again.  This module has no private API or order path.
"""
from __future__ import annotations

import argparse
import base64
import csv
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from io import StringIO
import json
import os
from pathlib import Path
import sys
from tempfile import NamedTemporaryFile
import time
from typing import Callable

# Keep direct checkout invocation (``python tools/...``) independent of the
# caller's working directory and PYTHONPATH.  Packaged/module invocation keeps
# the same imports, and no user-controlled path is added.
ROOT = Path(__file__).resolve().parents[1]
for dependency_root in (ROOT, ROOT / "research"):
    if str(dependency_root) not in sys.path:
        sys.path.insert(0, str(dependency_root))

from autotrade_research.integrations.whitebit_public import (
    ACTIVITY_KLINE_ADAPTER_VERSION,
    INTERVAL_SECONDS,
    MAX_BYTES,
    MAX_CANDLES,
    SYMBOL,
    PublicDataError,
    PublicKlines,
    WhiteBitPublicClient,
    _kline_query,
    _public_url,
    parse_klines,
    plan_kline_windows,
)
from tools.historical_public_klines import HistoricalArchiveError, _reject_symlink_ancestors


SCHEMA = "autotrade-whitebit-public-klines-v1"
CHECKPOINT_SCHEMA = "autotrade-whitebit-kline-checkpoint-v1"
PAGE_SCHEMA = "autotrade-whitebit-kline-page-v1"
MAX_PAGES = 400
MAX_ROWS = MAX_PAGES * MAX_CANDLES
MAX_NORMALIZED_BYTES = 80 * 1024 * 1024
RETRIABLE_ERRORS = frozenset({
    "PUBLIC_API_NETWORK_UNAVAILABLE",
    "PUBLIC_API_RATE_LIMIT",
    "PUBLIC_API_HTTP_ERROR",
    "PUBLIC_API_NON_SUCCESS",
})
UTC = timezone.utc


def _canonical_json(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _utc_text(value: datetime) -> str:
    if type(value) is not datetime or value.tzinfo != UTC:
        raise HistoricalArchiveError("WhiteBIT acquisition requires an exact UTC timestamp")
    return value.isoformat().replace("+00:00", "Z")


def _parse_utc_text(value: object) -> datetime:
    if type(value) is not str or not value.endswith("Z"):
        raise HistoricalArchiveError("WhiteBIT provenance timestamp is not canonical UTC")
    try:
        result = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise HistoricalArchiveError("WhiteBIT provenance timestamp is invalid") from exc
    if result.tzinfo != UTC or _utc_text(result) != value:
        raise HistoricalArchiveError("WhiteBIT provenance timestamp is not canonical UTC")
    return result


def _validate_range(*, start: int, end: int, interval: str, cutoff: datetime) -> int:
    if interval not in INTERVAL_SECONDS:
        raise HistoricalArchiveError("WhiteBIT history requires a fixed-width interval")
    if type(start) is not int or type(end) is not int or start < 0 or end <= start:
        raise HistoricalArchiveError("WhiteBIT history range is invalid")
    if type(cutoff) is not datetime or cutoff.tzinfo != UTC:
        raise HistoricalArchiveError("WhiteBIT acquisition cutoff must be UTC")
    step = INTERVAL_SECONDS[interval]
    if start % step or end % step:
        raise HistoricalArchiveError("WhiteBIT history range must align to candle opens")
    latest_complete_open = int(cutoff.timestamp()) // step * step - step
    if end > latest_complete_open:
        raise HistoricalArchiveError("WhiteBIT history accepts completed candles only")
    count = (end - start) // step + 1
    if count > MAX_ROWS:
        raise HistoricalArchiveError("WhiteBIT history row budget exceeded")
    return count


def _atomic_replace(path: Path, payload: bytes) -> None:
    _reject_symlink_ancestors(path.parent)
    path.parent.mkdir(parents=True, exist_ok=True)
    _reject_symlink_ancestors(path.parent)
    if path.is_symlink():
        raise HistoricalArchiveError("WhiteBIT dataset path must not be a symlink")
    temporary = None
    try:
        with NamedTemporaryFile("wb", prefix=".whitebit-history-", suffix=".partial",
                                dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _write_immutable(path: Path, payload: bytes) -> None:
    if path.exists():
        if path.is_symlink() or not path.is_file() or path.read_bytes() != payload:
            raise HistoricalArchiveError("WhiteBIT immutable dataset revision conflict")
        return
    _atomic_replace(path, payload)


def _page_envelope(index: int, batch: PublicKlines, *, start: int, end: int, limit: int) -> bytes:
    raw_digest = sha256(batch.raw).hexdigest()
    return _canonical_json({
        "schema": PAGE_SCHEMA,
        "index": index,
        "start": start,
        "end": end,
        "limit": limit,
        "source_uri": batch.source_uri,
        "received_at_utc": _utc_text(batch.received_at),
        "raw_sha256": raw_digest,
        "raw_base64": base64.b64encode(batch.raw).decode("ascii"),
    })


def _load_page(path: Path, *, index: int, market: str, interval: str,
               start: int, end: int, limit: int) -> PublicKlines:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_BYTES * 2:
        raise HistoricalArchiveError("WhiteBIT checkpoint page is missing or unbounded")
    try:
        value = json.loads(path.read_bytes())
        if type(value) is not dict or set(value) != {
            "schema", "index", "start", "end", "limit", "source_uri",
            "received_at_utc", "raw_sha256", "raw_base64",
        }:
            raise ValueError("page shape")
        if any((
            value["schema"] != PAGE_SCHEMA,
            value["index"] != index,
            value["start"] != start,
            value["end"] != end,
            value["limit"] != limit,
        )):
            raise ValueError("page identity")
        received_at = _parse_utc_text(value["received_at_utc"])
        raw = base64.b64decode(value["raw_base64"], validate=True)
        if not 0 < len(raw) <= MAX_BYTES or sha256(raw).hexdigest() != value["raw_sha256"]:
            raise ValueError("page digest")
        # WhiteBIT documents that when ``limit`` is supplied the server derives
        # start as ``end - limit * interval``.  Do not also send ``start``: it
        # is not an inclusive candle-open filter under that API contract.
        expected_uri = _public_url(
            "kline", _kline_query(market, interval, limit, end=end))
        if value["source_uri"] != expected_uri:
            raise ValueError("page source")
        return parse_klines(
            raw, market, interval, received_at,
            source_uri=expected_uri, requested_limit=limit,
            end=end, training=True,
        )
    except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError,
            PublicDataError) as exc:
        raise HistoricalArchiveError("WhiteBIT checkpoint page failed authentication") from exc


def _validate_page(batch: PublicKlines, *, start: int, interval: str, count: int,
                   cutoff: datetime) -> None:
    step = INTERVAL_SECONDS[interval]
    expected = tuple(start + offset * step for offset in range(count))
    actual = tuple(int(candle.open_time.timestamp()) for candle in batch.candles)
    if (batch.received_at < cutoff or actual != expected
            or any(not candle.complete for candle in batch.candles)):
        raise HistoricalArchiveError("WhiteBIT historical page is incomplete or has a cadence gap")


def _request_page(
    client: WhiteBitPublicClient,
    *, market: str, interval: str, start: int, end: int, limit: int,
    max_attempts: int, sleeper: Callable[[float], None],
) -> PublicKlines:
    for attempt in range(1, max_attempts + 1):
        try:
            return client.klines(
                market, interval=interval, end=end,
                limit=limit, training=True,
            )
        except PublicDataError as exc:
            if str(exc) not in RETRIABLE_ERRORS or attempt == max_attempts:
                raise HistoricalArchiveError(
                    f"WhiteBIT public history request failed: {str(exc)}"
                ) from exc
            sleeper(min(2.0, 0.25 * (2 ** (attempt - 1))))
    raise AssertionError("bounded retry loop exhausted")


def _checkpoint_identity(*, market: str, interval: str, start: int, end: int,
                         page_limit: int, cutoff: datetime) -> dict[str, object]:
    return {
        "schema": CHECKPOINT_SCHEMA,
        "provider": "WHITEBIT",
        "market": market,
        "interval": interval,
        "start_open_epoch": start,
        "end_open_epoch": end,
        "page_limit": page_limit,
        "cutoff_utc": _utc_text(cutoff),
        "adapter_version": ACTIVITY_KLINE_ADAPTER_VERSION,
        "execution_environment": "SIMULATION_ONLY",
    }


def collect_whitebit_history(
    *,
    market: str,
    interval: str,
    start: int,
    end: int,
    output: Path,
    client: WhiteBitPublicClient | None = None,
    page_limit: int = MAX_CANDLES,
    max_attempts: int = 3,
    now: datetime | None = None,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, object]:
    """Acquire a deterministic completed-candle cut and return its manifest."""
    if not isinstance(output, Path):
        raise HistoricalArchiveError("WhiteBIT output must be a Path")
    if type(market) is not str or SYMBOL.fullmatch(market) is None:
        raise HistoricalArchiveError("WhiteBIT market is invalid")
    if type(page_limit) is not int or not 1 <= page_limit <= MAX_CANDLES:
        raise HistoricalArchiveError("WhiteBIT page limit is invalid")
    if type(max_attempts) is not int or not 1 <= max_attempts <= 3:
        raise HistoricalArchiveError("WhiteBIT retry budget must be 1..3 attempts")
    if client is None:
        client = WhiteBitPublicClient()
    if type(client) is not WhiteBitPublicClient and not callable(getattr(client, "klines", None)):
        raise HistoricalArchiveError("WhiteBIT public client is invalid")

    current = now or datetime.now(UTC)
    _validate_range(start=start, end=end, interval=interval, cutoff=current)
    pages = plan_kline_windows(start=start, end=end, interval=interval, page_limit=page_limit)
    if not 1 <= len(pages) <= MAX_PAGES:
        raise HistoricalArchiveError("WhiteBIT pagination budget exceeded")

    dataset_id = f"{start}-{end}"
    series = output / "spot" / market / interval
    work = series / ".whitebit-acquisition" / dataset_id
    checkpoint_path = work / "checkpoint.json"
    _reject_symlink_ancestors(work)
    work.mkdir(parents=True, exist_ok=True)
    _reject_symlink_ancestors(work)

    if checkpoint_path.exists():
        if checkpoint_path.is_symlink() or checkpoint_path.stat().st_size > 200_000:
            raise HistoricalArchiveError("WhiteBIT checkpoint is redirected or unbounded")
        try:
            checkpoint = json.loads(checkpoint_path.read_bytes())
            cutoff = _parse_utc_text(checkpoint["cutoff_utc"])
        except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
            raise HistoricalArchiveError("WhiteBIT checkpoint is malformed") from exc
        expected_identity = _checkpoint_identity(
            market=market, interval=interval, start=start, end=end,
            page_limit=page_limit, cutoff=cutoff,
        )
        if any(checkpoint.get(key) != value for key, value in expected_identity.items()):
            raise HistoricalArchiveError("WhiteBIT checkpoint identity conflicts with request")
        if type(checkpoint.get("pages")) is not list:
            raise HistoricalArchiveError("WhiteBIT checkpoint page index is malformed")
        if current < cutoff:
            raise HistoricalArchiveError("WhiteBIT acquisition clock moved backward")
    else:
        cutoff = current
        checkpoint = {
            **_checkpoint_identity(
                market=market, interval=interval, start=start, end=end,
                page_limit=page_limit, cutoff=cutoff,
            ),
            "pages": [],
            "complete": False,
        }
        _atomic_replace(checkpoint_path, _canonical_json(checkpoint))
    _validate_range(start=start, end=end, interval=interval, cutoff=cutoff)

    accepted: list[PublicKlines] = []
    page_records: list[dict[str, object]] = []
    for index, window in enumerate(pages):
        # The shared planner's final ``end`` may equal a single candle's open.
        # WhiteBIT requires end > start, so bind every request to the complete
        # inclusive candle span instead of the last open timestamp alone.
        query_end = window.start + window.limit * INTERVAL_SECONDS[interval]
        page_path = work / "pages" / f"{index:06d}.json"
        if page_path.exists():
            batch = _load_page(
                page_path, index=index, market=market, interval=interval,
                start=window.start, end=query_end, limit=window.limit,
            )
        else:
            batch = _request_page(
                client, market=market, interval=interval,
                start=window.start, end=query_end, limit=window.limit,
                max_attempts=max_attempts, sleeper=sleeper,
            )
            _write_immutable(
                page_path,
                _page_envelope(index, batch, start=window.start,
                               end=query_end, limit=window.limit),
            )
        _validate_page(
            batch, start=window.start, interval=interval,
            count=window.limit, cutoff=cutoff,
        )
        accepted.append(batch)
        page_records.append({
            "index": index,
            "start": window.start,
            "end": query_end,
            "limit": window.limit,
            "source_uri": batch.source_uri,
            "received_at_utc": _utc_text(batch.received_at),
            "raw_sha256": sha256(batch.raw).hexdigest(),
            "row_count": len(batch.candles),
            "first_open_time_utc": _utc_text(batch.candles[0].open_time),
            "last_open_time_utc": _utc_text(batch.candles[-1].open_time),
        })
        checkpoint["pages"] = page_records
        checkpoint["complete"] = False
        _atomic_replace(checkpoint_path, _canonical_json(checkpoint))

    candles = tuple(candle for batch in accepted for candle in batch.candles)
    expected_rows = _validate_range(start=start, end=end, interval=interval, cutoff=cutoff)
    if len(candles) != expected_rows:
        raise HistoricalArchiveError("WhiteBIT dataset row count conflicts with requested range")
    stream = StringIO()
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(["open_time_utc", "open", "high", "low", "close", "volume"])
    for candle in candles:
        writer.writerow([
            _utc_text(candle.open_time), candle.open, candle.high,
            candle.low, candle.close, candle.base_volume,
        ])
    csv_bytes = stream.getvalue().encode("utf-8")
    if len(csv_bytes) > MAX_NORMALIZED_BYTES:
        raise HistoricalArchiveError("WhiteBIT normalized dataset exceeds byte budget")
    csv_digest = sha256(csv_bytes).hexdigest()
    identity = {
        "schema": SCHEMA,
        "provider": "WHITEBIT",
        "market": market,
        "interval": interval,
        "start_open_epoch": start,
        "end_open_epoch": end,
        "cutoff_utc": _utc_text(cutoff),
        "normalized_csv_sha256": csv_digest,
        "pages": [
            {key: page[key] for key in (
                "index", "start", "end", "source_uri", "received_at_utc", "raw_sha256",
            )}
            for page in page_records
        ],
    }
    dataset_digest = sha256(_canonical_json(identity)).hexdigest()
    manifest = {
        **identity,
        "dataset_identity_sha256": dataset_digest,
        "exchange": "WHITEBIT_SPOT",
        "source_kind": "WHITEBIT_PRODUCTION_PUBLIC_REST_KLINE",
        "adapter_version": ACTIVITY_KLINE_ADAPTER_VERSION,
        "execution_environment": "SIMULATION_ONLY",
        "private_credentials_used": False,
        "real_orders_enabled": False,
        "completed_candles_only": True,
        "pagination_semantics": "LIMIT_DERIVES_START_FROM_END",
        "cutoff_utc": _utc_text(cutoff),
        "row_count": len(candles),
        "missing_candle_intervals": 0,
        "usable_as_complete_causal_interval": True,
        "start_open_time_utc": _utc_text(candles[0].open_time),
        "end_open_time_utc": _utc_text(candles[-1].open_time),
        "canonical_csv_columns": ["open_time_utc", "open", "high", "low", "close", "volume"],
        "pages": page_records,
        "trust_boundary": (
            "WhiteBIT REST capture; not Binance signed-archive evidence and not fill/order evidence"
        ),
    }
    manifest_bytes = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
    csv_path = series / f"{dataset_id}.ohlcv.csv"
    manifest_path = series / f"{dataset_id}.manifest.json"
    _write_immutable(csv_path, csv_bytes)
    _write_immutable(manifest_path, manifest_bytes)
    checkpoint["pages"] = page_records
    checkpoint["complete"] = True
    checkpoint["dataset_identity_sha256"] = dataset_digest
    checkpoint["normalized_csv_sha256"] = csv_digest
    _atomic_replace(checkpoint_path, _canonical_json(checkpoint))
    return manifest


def _epoch_argument(value: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("expected UTC epoch seconds") from exc


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Acquire completed WhiteBIT public candles for simulation-only research"
    )
    parser.add_argument("--market", default="BTC_USDT")
    parser.add_argument("--interval", choices=tuple(INTERVAL_SECONDS), default="1h")
    parser.add_argument("--start", type=_epoch_argument, required=True,
                        help="aligned first candle-open UTC epoch seconds")
    parser.add_argument("--end", type=_epoch_argument, required=True,
                        help="aligned last completed candle-open UTC epoch seconds")
    parser.add_argument("--output", type=Path, default=Path("historical-data"))
    parser.add_argument("--page-limit", type=int, default=MAX_CANDLES)
    parser.add_argument("--max-attempts", type=int, default=3)
    args = parser.parse_args(argv)
    try:
        result = collect_whitebit_history(
            market=args.market, interval=args.interval,
            start=args.start, end=args.end, output=args.output,
            page_limit=args.page_limit, max_attempts=args.max_attempts,
        )
    except (HistoricalArchiveError, OSError) as exc:
        print(f"WhiteBIT historical acquisition blocked: {exc}")
        return 2
    print(json.dumps({
        "market": result["market"],
        "interval": result["interval"],
        "row_count": result["row_count"],
        "dataset_identity_sha256": result["dataset_identity_sha256"],
        "execution_environment": result["execution_environment"],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
