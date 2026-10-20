"""Authenticate a WhiteBIT historical cut and submit one durable Alpha job.

This is a narrow bridge between ``whitebit_historical_klines.py`` and the
existing durable ``AlphaResearchWorker``.  It never fetches data, reads keys,
or reaches an order route.  Trading costs are explicit hypothetical research
assumptions and are never described as account fees.
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "research"))

from tools.historical_public_klines import HistoricalArchiveError, _reject_symlink_ancestors
from tools.whitebit_historical_klines import INTERVAL_SECONDS, SCHEMA, _canonical_json


MAX_MANIFEST_BYTES = 1_000_000
MAX_CSV_BYTES = 80 * 1024 * 1024
MIN_BARS = 160
MAX_BARS = 4096
UTC = timezone.utc
_DIGEST = re.compile(r"[0-9a-f]{64}")
_COLUMNS = ["open_time_utc", "open", "high", "low", "close", "volume"]
_PAGE_IDENTITY_FIELDS = (
    "index", "start", "end", "source_uri", "received_at_utc", "raw_sha256",
)


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise HistoricalArchiveError("WhiteBIT manifest contains duplicate JSON keys")
        result[key] = value
    return result


def _read_regular(path: Path, *, limit: int, label: str) -> bytes:
    _reject_symlink_ancestors(path.parent)
    if (not isinstance(path, Path) or path.is_symlink() or not path.is_file() or
            not 0 < path.stat().st_size <= limit):
        raise HistoricalArchiveError(f"bounded regular {label} file required")
    data = path.read_bytes()
    if not 0 < len(data) <= limit:
        raise HistoricalArchiveError(f"bounded regular {label} file required")
    return data


def _utc(value: object, *, label: str) -> datetime:
    if type(value) is not str or not value.endswith("Z"):
        raise HistoricalArchiveError(f"{label} must be canonical UTC")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise HistoricalArchiveError(f"{label} must be canonical UTC") from exc
    if parsed.tzinfo != UTC or parsed.isoformat().replace("+00:00", "Z") != value:
        raise HistoricalArchiveError(f"{label} must be canonical UTC")
    return parsed


def _decimal(value: str, *, label: str, positive: bool = True) -> Decimal:
    if type(value) is not str or not 1 <= len(value) <= 80:
        raise HistoricalArchiveError(f"invalid WhiteBIT {label}")
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise HistoricalArchiveError(f"invalid WhiteBIT {label}") from exc
    if not result.is_finite() or (positive and result <= 0) or (not positive and result < 0):
        raise HistoricalArchiveError(f"invalid WhiteBIT {label}")
    return result


def _csv_path(manifest_path: Path) -> Path:
    suffix = ".manifest.json"
    if not manifest_path.name.endswith(suffix):
        raise HistoricalArchiveError("WhiteBIT manifest filename is not canonical")
    return manifest_path.with_name(manifest_path.name[:-len(suffix)] + ".ohlcv.csv")


def _manifest(data: bytes) -> dict:
    try:
        value = json.loads(data, object_pairs_hook=_unique_pairs,
                           parse_constant=lambda _value: (_ for _ in ()).throw(
                               HistoricalArchiveError("non-finite WhiteBIT manifest value")))
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise HistoricalArchiveError("WhiteBIT manifest is invalid JSON") from exc
    if type(value) is not dict:
        raise HistoricalArchiveError("WhiteBIT manifest must be a record")
    return value


def _authenticate_manifest(manifest: dict, csv_bytes: bytes) -> tuple[int, str]:
    required = {
        "schema", "provider", "market", "interval", "start_open_epoch", "end_open_epoch",
        "cutoff_utc", "normalized_csv_sha256", "dataset_identity_sha256", "pages",
        "exchange", "source_kind", "execution_environment", "private_credentials_used",
        "real_orders_enabled", "completed_candles_only", "row_count",
        "missing_candle_intervals", "usable_as_complete_causal_interval",
        "start_open_time_utc", "end_open_time_utc", "canonical_csv_columns",
    }
    if not required.issubset(manifest):
        raise HistoricalArchiveError("WhiteBIT manifest is missing required provenance")
    if any((
        manifest["schema"] != SCHEMA,
        manifest["provider"] != "WHITEBIT",
        manifest["exchange"] != "WHITEBIT_SPOT",
        manifest["source_kind"] != "WHITEBIT_PRODUCTION_PUBLIC_REST_KLINE",
        manifest["execution_environment"] != "SIMULATION_ONLY",
        manifest["private_credentials_used"] is not False,
        manifest["real_orders_enabled"] is not False,
        manifest["completed_candles_only"] is not True,
        manifest["missing_candle_intervals"] != 0,
        manifest["usable_as_complete_causal_interval"] is not True,
        manifest["canonical_csv_columns"] != _COLUMNS,
    )):
        raise HistoricalArchiveError("WhiteBIT manifest does not authorize causal public research")
    interval = manifest["interval"]
    if interval not in INTERVAL_SECONDS:
        raise HistoricalArchiveError("WhiteBIT manifest interval is not fixed width")
    step = INTERVAL_SECONDS[interval]
    market = manifest["market"]
    if type(market) is not str or re.fullmatch(r"[A-Z0-9]{1,20}_[A-Z0-9]{1,20}", market) is None:
        raise HistoricalArchiveError("WhiteBIT manifest market is invalid")
    start, end, count = (manifest[key] for key in
                         ("start_open_epoch", "end_open_epoch", "row_count"))
    if (type(start) is not int or type(end) is not int or type(count) is not int or
            count < MIN_BARS or count > 200_000 or start < 0 or end < start or
            start % step or end % step or (end - start) // step + 1 != count):
        raise HistoricalArchiveError("WhiteBIT manifest range is not complete and contiguous")
    cutoff = _utc(manifest["cutoff_utc"], label="cutoff")
    if end + step > int(cutoff.timestamp()):
        raise HistoricalArchiveError("WhiteBIT manifest includes an unclosed candle")
    digest = manifest["normalized_csv_sha256"]
    identity_digest = manifest["dataset_identity_sha256"]
    if (type(digest) is not str or _DIGEST.fullmatch(digest) is None or
            sha256(csv_bytes).hexdigest() != digest or type(identity_digest) is not str or
            _DIGEST.fullmatch(identity_digest) is None):
        raise HistoricalArchiveError("WhiteBIT normalized dataset hash differs")
    pages = manifest["pages"]
    if type(pages) is not list or not pages:
        raise HistoricalArchiveError("WhiteBIT page provenance is absent")
    page_rows = 0
    identity_pages = []
    next_page_start = start
    for index, page in enumerate(pages):
        if type(page) is not dict or any(field not in page for field in _PAGE_IDENTITY_FIELDS):
            raise HistoricalArchiveError("WhiteBIT page provenance is malformed")
        if (page["index"] != index or type(page.get("row_count")) is not int or
                page["row_count"] <= 0 or page.get("limit") != page["row_count"] or
                page.get("start") != next_page_start or
                page.get("end") != next_page_start + page["row_count"] * step or
                page.get("first_open_time_utc") != _utc_text(next_page_start) or
                page.get("last_open_time_utc") != _utc_text(
                    next_page_start + (page["row_count"] - 1) * step) or
                type(page["raw_sha256"]) is not str or
                _DIGEST.fullmatch(page["raw_sha256"]) is None):
            raise HistoricalArchiveError("WhiteBIT page provenance is not contiguous")
        if _utc(page["received_at_utc"], label="page receipt") < cutoff:
            raise HistoricalArchiveError("WhiteBIT page predates the acquisition cutoff")
        if (type(page["source_uri"]) is not str or
                not page["source_uri"].startswith("https://whitebit.com/api/v1/public/kline?")):
            raise HistoricalArchiveError("WhiteBIT page source is not the public kline route")
        page_rows += page["row_count"]
        next_page_start = page["end"]
        identity_pages.append({field: page[field] for field in _PAGE_IDENTITY_FIELDS})
    if page_rows != count:
        raise HistoricalArchiveError("WhiteBIT page rows differ from dataset rows")
    identity = {
        "schema": manifest["schema"], "provider": manifest["provider"],
        "market": market, "interval": interval, "start_open_epoch": start,
        "end_open_epoch": end, "cutoff_utc": manifest["cutoff_utc"],
        "normalized_csv_sha256": digest, "pages": identity_pages,
    }
    if sha256(_canonical_json(identity)).hexdigest() != identity_digest:
        raise HistoricalArchiveError("WhiteBIT dataset identity hash differs")
    return step, identity_digest


def _utc_text(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, UTC).isoformat().replace("+00:00", "Z")


def _rows(csv_bytes: bytes, *, manifest: dict, step: int) -> list[tuple[int, str]]:
    try:
        text = csv_bytes.decode("utf-8")
    except UnicodeError as exc:
        raise HistoricalArchiveError("WhiteBIT normalized dataset is not UTF-8") from exc
    reader = csv.reader(text.splitlines())
    try:
        header = next(reader)
    except StopIteration as exc:
        raise HistoricalArchiveError("WhiteBIT normalized dataset is empty") from exc
    if header != _COLUMNS:
        raise HistoricalArchiveError("WhiteBIT normalized dataset columns differ")
    result = []
    for index, row in enumerate(reader):
        if len(row) != len(_COLUMNS):
            raise HistoricalArchiveError("WhiteBIT normalized row shape differs")
        opened = _utc(row[0], label="candle open")
        opened_epoch = int(opened.timestamp())
        expected = manifest["start_open_epoch"] + index * step
        if opened_epoch != expected:
            raise HistoricalArchiveError("WhiteBIT normalized candles contain a cadence gap")
        open_value = _decimal(row[1], label="open")
        high = _decimal(row[2], label="high")
        low = _decimal(row[3], label="low")
        close = _decimal(row[4], label="close")
        _decimal(row[5], label="volume", positive=False)
        if high < max(open_value, close) or low > min(open_value, close) or low > high:
            raise HistoricalArchiveError("WhiteBIT normalized OHLC bounds are invalid")
        result.append((opened_epoch, row[4]))
    if (len(result) != manifest["row_count"] or not result or
            result[0][0] != manifest["start_open_epoch"] or
            result[-1][0] != manifest["end_open_epoch"] or
            manifest["start_open_time_utc"] != datetime.fromtimestamp(
                result[0][0], UTC).isoformat().replace("+00:00", "Z") or
            manifest["end_open_time_utc"] != datetime.fromtimestamp(
                result[-1][0], UTC).isoformat().replace("+00:00", "Z")):
        raise HistoricalArchiveError("WhiteBIT normalized rows differ from manifest range")
    return result


def build_alpha_input(
    manifest_path: Path,
    *,
    bars: int = 1000,
    entry_fee_ratio: str = "0.001",
    exit_fee_ratio: str = "0.001",
    spread_bps: str = "4",
    entry_slippage_bps: str = "1",
    exit_slippage_bps: str = "1",
    funding_borrow_bps: str = "0",
    model_cost_quote: str = "0.02",
    notional_quote: str = "1000",
    min_notional_quote: str = "5",
) -> tuple[dict, dict]:
    """Return a deterministic Alpha payload plus authenticated selection facts."""
    if type(bars) is not int or not MIN_BARS <= bars <= MAX_BARS:
        raise HistoricalArchiveError("Alpha history selection requires 160..4096 closes")
    manifest_bytes = _read_regular(manifest_path, limit=MAX_MANIFEST_BYTES, label="manifest")
    csv_path = _csv_path(manifest_path)
    csv_bytes = _read_regular(csv_path, limit=MAX_CSV_BYTES, label="normalized dataset")
    manifest = _manifest(manifest_bytes)
    step, dataset_digest = _authenticate_manifest(manifest, csv_bytes)
    rows = _rows(csv_bytes, manifest=manifest, step=step)
    if bars > len(rows):
        raise HistoricalArchiveError("requested Alpha window exceeds authenticated dataset")
    selected = rows[-bars:]
    interval_ms = step * 1000
    closed = [(opened + step) * 1000 for opened, _close in selected]
    # Fixed-width candle observations become causal only after their close.
    # One millisecond prevents an event at the exact boundary from seeing the
    # just-completed close before the market-data event is admitted.
    available = [value + 1 for value in closed]
    asof_ms = available[-1]
    selection = {
        "dataset_identity_sha256": dataset_digest,
        "first_open_epoch": selected[0][0], "last_open_epoch": selected[-1][0],
        "row_count": bars, "availability_policy": "FIRST_MILLISECOND_AFTER_CANDLE_CLOSE",
    }
    selection_digest = sha256(_canonical_json(selection)).hexdigest()
    quote = manifest["market"].split("_")[-1]
    cost_source = "HYPOTHETICAL_NOT_ACCOUNT_FEES:WHITEBIT_ALPHA_BRIDGE_V1"
    payload = {
        "series": {
            "market": manifest["market"],
            "closes": [close for _opened, close in selected],
            "closed_at_ms": closed,
            "available_at_ms": available,
            "interval_ms": interval_ms,
            "asof_ms": asof_ms,
            "source_ref": "whitebit-public-rest-kline:sha256:" + dataset_digest,
            "snapshot_id": "sha256:" + selection_digest,
            "evidence_class": "RECORDED_PUBLIC",
        },
        "costs": {
            "entry_fee_ratio": entry_fee_ratio,
            "exit_fee_ratio": exit_fee_ratio,
            "spread_bps": spread_bps,
            "entry_slippage_bps": entry_slippage_bps,
            "exit_slippage_bps": exit_slippage_bps,
            "funding_borrow_bps": funding_borrow_bps,
            "model_cost_quote": model_cost_quote,
            "notional_quote": notional_quote,
            "min_notional_quote": min_notional_quote,
            "quote_currency": quote,
            "source_ref": cost_source,
            "observed_at_ms": asof_ms,
            "valid_until_ms": asof_ms + interval_ms,
        },
    }
    # Reuse the canonical cost record.  Series validation above deliberately
    # stays dependency-free so ``--prepare-only`` can authenticate data while
    # native ML wheels are still being installed.
    from research.autotrade_research.integrations.alpha_economics import CostBudget
    CostBudget(**payload["costs"]).validate(asof_ms)
    return payload, selection


def _worker(state_dir: Path):
    from tools.run_whitebit_alpha_research import _durable_worker
    return _durable_worker(state_dir)


def _add_cost_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--entry-fee-ratio", default="0.001")
    parser.add_argument("--exit-fee-ratio", default="0.001")
    parser.add_argument("--spread-bps", default="4")
    parser.add_argument("--entry-slippage-bps", default="1")
    parser.add_argument("--exit-slippage-bps", default="1")
    parser.add_argument("--funding-borrow-bps", default="0")
    parser.add_argument("--model-cost-quote", default="0.02")
    parser.add_argument("--notional-quote", default="1000")
    parser.add_argument("--min-notional-quote", default="5")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Authenticate WhiteBIT public history and run durable offline Alpha research")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--manifest", type=Path)
    source.add_argument("--job-id", help="resume/replay one existing durable Alpha job")
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--bars", type=int, default=1000)
    parser.add_argument("--lookback", type=int, default=24)
    parser.add_argument("--horizon", type=int, default=1)
    parser.add_argument("--steps", type=int, default=32)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--backends", nargs="+", default=["DLINEAR", "XGBOOST", "CATBOOST"])
    parser.add_argument("--prepare-only", action="store_true",
                        help="authenticate and print deterministic input; do not create a job")
    parser.add_argument("--enqueue-only", action="store_true",
                        help="durably enqueue but do not fit models")
    _add_cost_arguments(parser)
    args = parser.parse_args(argv)
    if args.job_id and (args.prepare_only or args.enqueue_only):
        parser.error("--job-id cannot be combined with prepare/enqueue-only")
    if not args.prepare_only and args.state_dir is None:
        parser.error("durable execution requires --state-dir")
    try:
        if args.job_id:
            result = {"job_id": args.job_id, "result": _worker(args.state_dir).run(args.job_id)}
        else:
            cost_names = (
                "entry_fee_ratio", "exit_fee_ratio", "spread_bps", "entry_slippage_bps",
                "exit_slippage_bps", "funding_borrow_bps", "model_cost_quote",
                "notional_quote", "min_notional_quote",
            )
            payload, selection = build_alpha_input(
                args.manifest, bars=args.bars,
                **{name: getattr(args, name) for name in cost_names})
            if args.prepare_only:
                result = {"payload": payload, "selection": selection,
                          "financial_operation_executed": False}
            else:
                worker = _worker(args.state_dir)
                job, created = worker.enqueue(
                    payload, lookback=args.lookback, horizon=args.horizon, steps=args.steps,
                    seed=args.seed, backends=tuple(args.backends))
                result = {"job_id": job["job_id"], "created": created,
                          "selection": selection, "financial_operation_executed": False}
                if not args.enqueue_only:
                    result["result"] = worker.run(job["job_id"])
        print(json.dumps(result, sort_keys=True, allow_nan=False, separators=(",", ":")))
        return 0
    except (HistoricalArchiveError, ValueError, TypeError, KeyError, OSError, ImportError,
            RecursionError, RuntimeError):
        print("WHITEBIT_ALPHA_INPUT_OR_DEPENDENCY_ERROR; no financial operation executed",
              file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
