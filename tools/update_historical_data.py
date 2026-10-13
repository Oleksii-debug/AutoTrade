"""Bounded incremental public-history intake; no daemon, secrets or broker routes.

An operator invokes this finite command manually or from their own scheduler.
Verified cache is reused by default; --recheck-source detects publisher changes
without overwriting accepted history. Every finished update gets an immutable
inventory in the existing private ArtifactStore. Gaps are reported, not filled.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
import time
from urllib.error import HTTPError, URLError
from uuid import NAMESPACE_URL, uuid5

from autotrade_runtime.artifacts import ArtifactStore
from tools.historical_public_klines import (
    HistoricalArchiveError, MAX_CSV_BYTES, MAX_ZIP_BYTES, SYMBOL,
    _fetch, _month_range, _utc_day, _require_past_month,
    _reject_symlink_ancestors, collect_month,
)
from tools.run_historical_zero import _checked_month


def _periods(from_month, through_month, daily_through):
    _require_past_month(through_month)
    result = [(month, None) for month in _month_range(from_month, through_month)]
    if daily_through is not None:
        last = _utc_day(daily_through)
        year, month = map(int, through_month.split("-"))
        first = (datetime(year + 1, 1, 1, tzinfo=timezone.utc) if month == 12
                 else datetime(year, month + 1, 1, tzinfo=timezone.utc))
        days = (last - first).days + 1
        if not 1 <= days <= 35:
            raise HistoricalArchiveError("daily gap must span 1 to 35 days after monthly cut")
        result.extend(((first + timedelta(days=i)).strftime("%Y-%m"),
                       (first + timedelta(days=i)).date().isoformat()) for i in range(days))
    return result


def _usage(root: Path) -> int:
    _reject_symlink_ancestors(root)
    total = 0
    if root.exists():
        for path in root.rglob("*"):
            if path.is_symlink():
                raise HistoricalArchiveError("symlink in historical cache")
            if path.is_file():
                total += path.stat().st_size
    return total


def _transport_failure(exc):
    cause = exc.__cause__
    if isinstance(cause, HTTPError):
        return "MISSING" if cause.code == 404 else "TRANSIENT" if cause.code in (408, 429, 500, 502, 503, 504) else "REJECTED"
    if isinstance(cause, (URLError, TimeoutError)):
        return "TRANSIENT"
    return "REJECTED"


def update_history(
    root: Path, *, from_month: str, through_month: str,
    symbols=("BTCUSDT", "ETHUSDT"), daily_through=None,
    max_disk_bytes=1024 * 1024 * 1024, max_attempts=3,
    recheck_source=False, fetch=_fetch, sleep=time.sleep,
) -> dict:
    if type(max_disk_bytes) is not int or max_disk_bytes < 1024 * 1024:
        raise HistoricalArchiveError("exact disk budget of at least 1 MiB required")
    if type(max_attempts) is not int or not 1 <= max_attempts <= 3:
        raise HistoricalArchiveError("retry budget must be 1..3")
    if type(symbols) not in (tuple, list) or not 1 <= len(symbols) <= 10 or any(
        type(symbol) is not str or SYMBOL.fullmatch(symbol) is None for symbol in symbols
    ) or len(set(symbols)) != len(symbols):
        raise HistoricalArchiveError("invalid or duplicate symbol universe")
    periods = _periods(from_month, through_month, daily_through)
    if len(periods) * len(symbols) > 600:
        raise HistoricalArchiveError("archive count budget exceeded")
    if _usage(root) > max_disk_bytes:
        raise HistoricalArchiveError("existing cache exceeds disk budget; no automatic deletion")
    store = ArtifactStore(root / ".artifact-store")
    entries = []
    for symbol in symbols:
        for month, day in periods:
            period = day or month
            directory = root / "spot" / symbol / "1h"
            if day is not None:
                directory /= "daily"
            manifest_path = directory / f"{period}.manifest.json"
            item = {"symbol": symbol, "period": period, "status": "NOT_CHECKED"}
            try:
                if manifest_path.exists():
                    # Never silently redownload over a corrupt local history.
                    _, _, manifest = _checked_month(root, symbol, "1h", month,
                                                    day=day, artifact_store=store)
                    if not recheck_source:
                        item.update(status="VERIFIED_CACHE", rows=manifest["row_count"],
                                    source_zip_sha256=manifest["source_zip_sha256"])
                        entries.append(item)
                        continue
                # Reserve worst-case source ZIP + two copies of normalized CSV
                # plus manifests before downloading; no delete/evict side effect.
                reserve = MAX_ZIP_BYTES + 2 * MAX_CSV_BYTES + 1024 * 1024
                if _usage(root) + reserve > max_disk_bytes:
                    raise HistoricalArchiveError("insufficient configured disk budget")
                def retrying_fetch(url, limit):
                    for attempt in range(max_attempts):
                        try:
                            return fetch(url, limit)
                        except HistoricalArchiveError as exc:
                            if _transport_failure(exc) != "TRANSIENT" or attempt + 1 == max_attempts:
                                raise
                            sleep(2 ** attempt)
                manifest = collect_month(symbol=symbol, interval="1h", month=month,
                                         day=day, output=root, artifact_store=store,
                                         fetch=retrying_fetch)
                item.update(status="VERIFIED_SOURCE" if manifest["missing_candle_intervals"] == 0 else "GAPPED",
                            rows=manifest["row_count"], gaps=manifest["missing_candle_intervals"],
                            source_zip_sha256=manifest["source_zip_sha256"])
            except HistoricalArchiveError as exc:
                item.update(status=_transport_failure(exc), reason=str(exc))
            entries.append(item)
    report = {"schema": "autotrade-history-update-v1", "simulation_only": True,
              "symbols": list(symbols), "from_month": from_month, "through_month": through_month,
              "daily_through": daily_through, "recheck_source": recheck_source,
              "complete": all(item["status"] in ("VERIFIED_SOURCE", "VERIFIED_CACHE") for item in entries),
              "entries": entries, "completed_at_utc": datetime.now(timezone.utc).isoformat()}
    payload = (json.dumps(report, sort_keys=True, indent=2) + "\n").encode()
    if _usage(root) + len(payload) * 3 + 65536 > max_disk_bytes:
        raise HistoricalArchiveError("insufficient disk budget for immutable inventory")
    identity = sha256(payload).hexdigest()
    artifact = store.publish_bytes(
        artifact_id=str(uuid5(NAMESPACE_URL, "autotrade:historical-inventory:" + identity)),
        data=payload, media_type="application/json",
        rights={"storage": True, "export": False, "rights_id": "private-source-inventory"},
        source_refs=["urn:autotrade:historical-update-v1"],
        metadata={"kind": "HISTORICAL_UPDATE_INVENTORY", "complete": report["complete"]},
    )
    report["inventory_artifact_id"] = artifact["artifact_id"]
    report["inventory_sha256"] = artifact["sha256"]
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="historical-data")
    parser.add_argument("--from-month", default="2024-01")
    parser.add_argument("--through-month", required=True)
    parser.add_argument("--daily-through")
    parser.add_argument("--symbols", default="BTCUSDT,ETHUSDT")
    parser.add_argument("--max-disk-bytes", type=int, default=1024 * 1024 * 1024)
    parser.add_argument("--recheck-source", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = update_history(Path(args.output), from_month=args.from_month,
                                through_month=args.through_month, daily_through=args.daily_through,
                                symbols=args.symbols.split(","), max_disk_bytes=args.max_disk_bytes,
                                recheck_source=args.recheck_source)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result["complete"] else 2
    except (HistoricalArchiveError, OSError, RuntimeError) as exc:
        print(f"Historical update blocked: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
