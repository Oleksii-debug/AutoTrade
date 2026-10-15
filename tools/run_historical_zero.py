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
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from hashlib import sha256
import json
from pathlib import Path
import re

from autotrade_runtime.artifacts import ArtifactIntegrityError, ArtifactStore

from tools.historical_public_klines import (
    BASE, DAILY_BASE, DATASET_LICENSE, DATASET_TERMS_URL,
    HistoricalArchiveError, INTERVAL_SECONDS, SYMBOL, _month_range,
    _reject_symlink_ancestors, _utc_day, _require_past_month,
)
from mvp.autotrade_mvp.simulation_session import run_autonomous_simulation


def _verify_artifact_evidence(
    *,
    store: ArtifactStore,
    manifest: dict,
    normalized_csv: bytes,
) -> None:
    if type(store) is not ArtifactStore:
        raise HistoricalArchiveError("artifact_store must be the canonical ArtifactStore")
    evidence = manifest.get("artifact_evidence")
    roles = {"source_zip", "publisher_checksum", "normalized_ohlcv_csv"}
    if type(evidence) is not dict or set(evidence) != roles:
        raise HistoricalArchiveError("historical manifest lacks canonical artifact evidence")
    expected_digests = {
        "source_zip": "sha256:" + manifest["source_zip_sha256"],
        "publisher_checksum": "sha256:" + manifest["source_checksum_sha256"],
        "normalized_ohlcv_csv": "sha256:" + manifest["normalized_csv_sha256"],
    }
    expected_media = {
        "source_zip": "application/zip",
        "publisher_checksum": "text/plain; charset=us-ascii",
        "normalized_ohlcv_csv": "text/csv; charset=utf-8",
    }
    expected_sources = {
        "source_zip": [manifest["source_url"], manifest["source_checksum_url"], DATASET_TERMS_URL],
        "publisher_checksum": [manifest["source_checksum_url"], manifest["source_url"], DATASET_TERMS_URL],
        "normalized_ohlcv_csv": [manifest["source_url"], manifest["source_checksum_url"], DATASET_TERMS_URL],
    }
    try:
        for role in sorted(roles):
            reference = evidence[role]
            if type(reference) is not dict or set(reference) != {
                "artifact_id", "sha256", "bytes", "media_type", "rights_id",
                "export_permitted",
            }:
                raise HistoricalArchiveError("historical artifact reference is malformed")
            authenticated, data = store.read_authenticated_snapshot(reference["artifact_id"])
            if any((
                reference["sha256"] != expected_digests[role],
                reference["sha256"] != authenticated.get("sha256"),
                reference["bytes"] != authenticated.get("bytes"),
                reference["media_type"] != expected_media[role],
                reference["media_type"] != authenticated.get("media_type"),
                reference["rights_id"] != "binance-vision-noncommercial-research-v1",
                reference["export_permitted"] is not False,
                authenticated.get("rights") != {
                    "storage": True,
                    "export": False,
                    "rights_id": "binance-vision-noncommercial-research-v1",
                },
                authenticated.get("source_refs") != expected_sources[role],
            )):
                raise HistoricalArchiveError("historical artifact evidence conflicts with provenance")
            metadata = authenticated.get("metadata")
            if type(metadata) is not dict or any(metadata.get(key) != expected for key, expected in (
                ("exchange", "BINANCE_SPOT"),
                ("symbol", manifest["symbol"]),
                ("interval", manifest["interval"]),
                ("period", manifest["day"] or manifest["month"]),
                ("license_terms_url", DATASET_TERMS_URL),
                ("rights_scope", "PERSONAL_NON_COMMERCIAL_RESEARCH_SIMULATION_ONLY"),
                ("evidence_kind", role.upper()),
            )):
                raise HistoricalArchiveError("historical artifact metadata conflicts with provenance")
            if role == "normalized_ohlcv_csv" and data != normalized_csv:
                raise HistoricalArchiveError("artifact-store normalized CSV differs from replay bytes")
    except (ArtifactIntegrityError, FileNotFoundError, KeyError, OSError,
            RuntimeError, TypeError, ValueError) as exc:
        if isinstance(exc, HistoricalArchiveError):
            raise
        raise HistoricalArchiveError("historical artifact evidence verification failed") from exc


def _checked_month(
    root: Path,
    symbol: str,
    interval: str,
    month: str,
    *,
    artifact_store: ArtifactStore | None = None,
    day: str | None = None,
    include_ohlcv: bool = False,
):
    """Validate one frozen UTC period; daily and monthly paths share authority."""
    if type(symbol) is not str or SYMBOL.fullmatch(symbol) is None or interval not in INTERVAL_SECONDS:
        raise HistoricalArchiveError("invalid instrument selection")
    if day is None:
        _require_past_month(month)
    elif _utc_day(day).strftime("%Y-%m") != month:
        raise HistoricalArchiveError("daily period does not match month")
    period = day or month
    base = root / "spot" / symbol / interval
    if day is not None:
        base = base / "daily"
    # Verify the complete lexical ancestor chain, including ancestors ABOVE
    # data-root; a symlink above root must never redirect trusted replay.
    _reject_symlink_ancestors(base)
    manifest_file = base / (period + ".manifest.json")
    rows_file = base / (period + ".ohlcv.csv")
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
    # A normalized CSV digest alone does not authenticate a locally mutable
    # provenance manifest. Reject origin/right-scope relabeling before replay.
    source_base = DAILY_BASE if day is not None else BASE
    canonical_zip_url = f"{source_base}/{symbol}/{interval}/{symbol}-{interval}-{period}.zip"
    if any(manifest.get(key) != expected for key, expected in (
        ("day", day),
        ("period_granularity", "DAILY" if day is not None else "MONTHLY"),
        ("exchange", "BINANCE_SPOT"),
        ("source_url", canonical_zip_url),
        ("source_checksum_url", canonical_zip_url + ".CHECKSUM"),
        ("license", DATASET_LICENSE),
        ("license_terms_url", DATASET_TERMS_URL),
        ("rights_scope", "PERSONAL_NON_COMMERCIAL_RESEARCH_SIMULATION_ONLY"),
    )):
        raise HistoricalArchiveError("source provenance metadata conflicts with selected public archive")
    for field in ("source_zip_sha256", "source_csv_sha256", "source_checksum_sha256"):
        value = manifest.get(field)
        if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
            raise HistoricalArchiveError(f"source provenance digest invalid: {field}")
    if not manifest.get("usable_as_complete_causal_interval") or manifest.get("missing_candle_intervals") != 0:
        raise HistoricalArchiveError("historical source has missing intervals; fail closed")
    if sha256(raw).hexdigest() != manifest.get("normalized_csv_sha256"):
        raise HistoricalArchiveError("normalized historical archive digest mismatch")
    if artifact_store is not None:
        _verify_artifact_evidence(
            store=artifact_store,
            manifest=manifest,
            normalized_csv=raw,
        )
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
    candles = []
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
        candles.append(tuple(row[key] for key in ("open", "high", "low", "close", "volume")))
        if len(prices) > 10000:
            raise HistoricalArchiveError("one canonical ZERO run is capped at 10000 observations")
    if len(prices) != manifest.get("row_count"):
        raise HistoricalArchiveError("candle rows differ from verified manifest")
    if not prices or dates[0].isoformat().replace("+00:00", "Z") != manifest.get("start_open_time_utc") or dates[-1].isoformat().replace("+00:00", "Z") != manifest.get("end_open_time_utc"):
        raise HistoricalArchiveError("time range conflicts with manifest")
    step = INTERVAL_SECONDS[interval]
    if any(int((b-a).total_seconds()) != step for a,b in zip(dates, dates[1:])):
        raise HistoricalArchiveError("internal timestamp gap/reorder")
    # Recalculate UTC-calendar completeness from actual rows, not from
    # self-reported manifest fields. A truncated first/last bar and a locally
    # recomputed CSV hash must never silently qualify a partial month.
    from calendar import monthrange
    year, month_number = map(int, month.split("-"))
    expected_first = (_utc_day(day) if day is not None else
                      datetime(year, month_number, 1, tzinfo=timezone.utc))
    expected_bars = (1 if day is not None else monthrange(year, month_number)[1]) * 86400 // step
    expected_last = expected_first + timedelta(seconds=step * (expected_bars - 1))
    if len(dates) != expected_bars or dates[0] != expected_first or dates[-1] != expected_last:
        raise HistoricalArchiveError("historical coverage does not include full UTC month")
    return dates, (tuple(candles) if include_ohlcv else prices), manifest


def load_verified_months(
    root: Path,
    symbol: str,
    interval: str,
    from_month: str,
    through_month: str,
    *,
    artifact_store: ArtifactStore | None = None,
):
    if SYMBOL.fullmatch(symbol) is None or interval not in INTERVAL_SECONDS:
        raise HistoricalArchiveError("invalid instrument selection")
    all_dates, all_prices, digests = [], [], []
    step = INTERVAL_SECONDS[interval]
    months = list(_month_range(from_month, through_month))
    for month in months:
        dates, prices, manifest = _checked_month(
            root,
            symbol,
            interval,
            month,
            artifact_store=artifact_store,
        )
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
    parser.add_argument(
        "--artifact-store",
        default=None,
        help=("canonical evidence store; defaults to "
              "<data-root>/.artifact-store and is mandatory for CLI replay"),
    )
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--interval", choices=tuple(INTERVAL_SECONDS), default="1h")
    parser.add_argument("--from-month", default="2024-01")
    parser.add_argument("--through-month", required=True)
    parser.add_argument("--state-dir", default=None,
                        help="separate durable directory per virtual account; defaults are budget-isolated")
    parser.add_argument("--run-id", default="research-2024")
    parser.add_argument("--virtual-cash", choices=("200", "1000"), default="1000")
    parser.add_argument("--stop-after-episodes", type=int)
    args = parser.parse_args(argv)
    try:
        data_root = Path(args.data_root)
        artifact_root = (Path(args.artifact_store) if args.artifact_store is not None
                         else data_root / ".artifact-store")
        _reject_symlink_ancestors(artifact_root)
        dates, prices, digest = load_verified_months(
            data_root,
            args.symbol,
            args.interval,
            args.from_month,
            args.through_month,
            artifact_store=ArtifactStore(artifact_root),
        )
        # The canonical simulator gets a complete immutable price cut for
        # restart/recovery, but its strategy receives only prices[:episode].
        # Observation time represents the completed bar, never its open.
        first_close = dates[0] + timedelta(seconds=INTERVAL_SECONDS[args.interval])
        # The first CLOSED candle is known at first_close. Freeze a bounded,
        # affordable SPOT quantity from that observation alone; no future
        # bars, result labels or hindsight portfolio figures are consulted.
        # Existing canonical ZERO risk/OMS/accounting still own every fill.
        first_closed_price = Decimal(prices[0])
        research_notional = Decimal(args.virtual_cash) * Decimal("0.25")
        research_target = (research_notional / first_closed_price).quantize(
            Decimal("0.00000001"), rounding=ROUND_DOWN
        )
        if research_target <= 0:
            raise HistoricalArchiveError("first closed price exceeds bounded fractional research quantity")
        state_dir = args.state_dir or (
            "historical-zero-state" if args.virtual_cash == "1000"
            else "historical-zero-state-200"
        )
        result = run_autonomous_simulation(
            prices, state_dir, run_id=args.run_id + "-" + digest[:16],
            now=first_close.isoformat().replace("+00:00", "Z"),
            observation_interval_seconds=INTERVAL_SECONDS[args.interval],
            stop_after_episodes=args.stop_after_episodes,
            instrument_profile="FRACTIONAL_SPOT_RESEARCH",
            target_quantity=format(research_target, "f"),
            virtual_cash=args.virtual_cash,
        )
        print(json.dumps({
            "label": "INTERNAL_CANDLE_CLOSE_SAME_PRICE_EXPERIMENT_NOT_PERFORMANCE_EVIDENCE",
            "source_market": "BINANCE_HISTORICAL_SPOT_CANDLES",
            "original_symbol": args.symbol,
            "historical_start_utc": dates[0].isoformat().replace("+00:00", "Z"),
            "historical_end_utc": dates[-1].isoformat().replace("+00:00", "Z"),
            "observation_count": len(prices),
            "dataset_identity_sha256": digest,
            "frozen_fractional_spot_target_quantity": format(research_target, "f"),
            "target_sizing_basis": (
                f"USD {research_notional} (25% of selected virtual cash) / first completed "
                "candle close, rounded down to 0.00000001; remaining virtual cash stays unspent"
            ),
            "virtual_cash_usd": args.virtual_cash,
            "important_limitations": [
                "Existing agent is currently a frozen moving-average strategy; this run does not prove learning.",
                "Existing engine executes at observed mark, not qualified next-bar open/spread/slippage.",
                "OHLCV has no order-book or tick-level execution truth.",
                f"Virtual {args.virtual_cash} USD, zero network orders. Do not infer profitability from these fills.",
            ],
            "simulation": result,
        }, indent=2))
        return 2 if result.get("status") == "UNKNOWN" else 0
    except (HistoricalArchiveError, OSError, RuntimeError, ValueError, TypeError, ArithmeticError) as exc:
        print(f"AutoTrade historical ZERO experiment blocked: {type(exc).__name__}: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
