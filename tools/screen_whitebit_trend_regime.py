"""Screen the causal trend-regime family on authenticated WhiteBIT candles."""
import argparse
import csv
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import perf_counter
import json

from research.autotrade_research.strategies.fast_screen import Costs
from research.autotrade_research.strategies.trend_regime import TrendRegime, screen
from tools.run_whitebit_historical_alpha import (
    MAX_CSV_BYTES, MAX_MANIFEST_BYTES, _COLUMNS, _authenticate_manifest,
    _csv_path, _manifest, _read_regular, _rows,
)
from tools.screen_historical_candidates import stage_checkpoint
from tools.train_historical_agent import save_model


def authenticated_bars(manifest_path: Path):
    """Return exact OHLCV bars only after the frozen WhiteBIT cut authenticates."""
    manifest_bytes = _read_regular(
        manifest_path, limit=MAX_MANIFEST_BYTES, label="manifest")
    csv_path = _csv_path(manifest_path)
    csv_bytes = _read_regular(
        csv_path, limit=MAX_CSV_BYTES, label="normalized dataset")
    manifest = _manifest(manifest_bytes)
    step, dataset_digest = _authenticate_manifest(manifest, csv_bytes)
    validated = _rows(csv_bytes, manifest=manifest, step=step)
    reader = csv.reader(csv_bytes.decode("utf-8").splitlines())
    if next(reader) != _COLUMNS:
        raise ValueError("authenticated WhiteBIT columns changed during decode")
    result = []
    for (opened_epoch, validated_close), row in zip(validated, reader, strict=True):
        if row[4] != validated_close:
            raise ValueError("authenticated WhiteBIT close changed during decode")
        opened_at = datetime.fromtimestamp(opened_epoch, UTC)
        result.append(dict(opened_at=opened_at,
            closed_at=opened_at+timedelta(seconds=step), open=row[1], high=row[2],
            low=row[3], close=row[4], volume=row[5]))
    return result, dataset_digest, manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--train-count", type=int, required=True)
    parser.add_argument("--development-count", type=int, required=True)
    parser.add_argument("--min-cycles", type=int, default=3)
    parser.add_argument("--max-drawdown-fraction", default="0.25")
    parser.add_argument("--output-dir", type=Path, required=True)
    for field in ("initial_cash", "fee_bps", "spread_bps", "slippage_bps", "capital_fraction"):
        parser.add_argument("--" + field.replace("_", "-"), default=getattr(Costs(), field))
    parser.add_argument("--fast-lookback", type=int, default=TrendRegime().fast_lookback)
    parser.add_argument("--slow-lookback", type=int, default=TrendRegime().slow_lookback)
    for field in ("entry_threshold", "slow_threshold", "exit_threshold",
                  "trailing_stop_fraction", "short_entry_threshold",
                  "short_slow_threshold", "short_carry_bps_per_day"):
        parser.add_argument("--" + field.replace("_", "-"), default=getattr(TrendRegime(), field))
    parser.add_argument("--min-hold-bars", type=int, default=TrendRegime().min_hold_bars)
    parser.add_argument("--max-hold-bars", type=int, default=TrendRegime().max_hold_bars)
    parser.add_argument("--allow-short", action="store_true")
    args = parser.parse_args(argv)
    started = perf_counter()
    bars, dataset_digest, manifest = authenticated_bars(args.manifest)
    defaults = Costs()
    costs = Costs(**{field: getattr(args, field, getattr(defaults, field))
                     for field in Costs.__dataclass_fields__})
    configuration = TrendRegime(**{field: getattr(args, field)
                                   for field in TrendRegime.__dataclass_fields__})
    result = screen(bars, train_count=args.train_count,
        development_count=args.development_count, dataset_sha256=dataset_digest,
        costs=costs, configuration=configuration, min_cycles=args.min_cycles,
        max_drawdown_fraction=args.max_drawdown_fraction,
        checkpoint=stage_checkpoint(args.output_dir / "stages"))
    source = dict(kind="AUTHENTICATED_WHITEBIT_PUBLIC_REST_HISTORY",
        market=manifest["market"], interval=manifest["interval"],
        start_open_time_utc=manifest["start_open_time_utc"],
        end_open_time_utc=manifest["end_open_time_utc"],
        dataset_identity_sha256=dataset_digest)
    target = args.output_dir / (result["config_sha256"] + ".json")
    save_model(target, dict(source=source, result=result))
    print(json.dumps(dict(result_path=str(target), observations=len(bars),
        elapsed_seconds=round(perf_counter()-started, 3),
        selected_candidate=result["selected_candidate"],
        training_rejection_reasons=result["training_rejection_reasons"],
        development_rejection_reasons=result["development_rejection_reasons"],
        final_rejection_reasons=result["final_rejection_reasons"],
        prospective_qualification=result["prospective_qualification"],
        holdout_consumed=result["holdout_consumed"], evaluation=result["evaluation"],
        result_sha256=result["result_sha256"], network_orders=False, paid_model_calls=0)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
