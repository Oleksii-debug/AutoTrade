"""Run a deterministic three-way trend-regime historical experiment."""
import argparse
from datetime import datetime
from pathlib import Path
from time import perf_counter
import json

from research.autotrade_research.strategies.fast_screen import Costs
from research.autotrade_research.strategies.trend_regime import TrendRegime, screen
from tools.historical_causal_replay import BAR_CLOSE_SCENARIO, load_causal_archive
from tools.screen_historical_candidates import stage_checkpoint
from tools.train_historical_agent import save_model


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--symbol", choices=("BTCUSDT", "ETHUSDT"), required=True)
    parser.add_argument("--interval", default="1h", choices=("1h",))
    parser.add_argument("--from-month", default="2024-01")
    parser.add_argument("--through-month", default="2025-01")
    parser.add_argument("--train-count", type=int, required=True)
    parser.add_argument("--development-count", type=int, required=True)
    parser.add_argument("--min-cycles", type=int, default=3)
    parser.add_argument("--max-drawdown-fraction", default="0.20")
    parser.add_argument("--output-dir", required=True)
    for field in ("initial_cash", "fee_bps", "spread_bps", "slippage_bps", "capital_fraction"):
        parser.add_argument("--" + field.replace("_", "-"), default=getattr(Costs(), field))
    parser.add_argument("--fast-lookback", type=int, default=TrendRegime().fast_lookback)
    parser.add_argument("--slow-lookback", type=int, default=TrendRegime().slow_lookback)
    for field in ("entry_threshold", "slow_threshold", "exit_threshold",
                  "trailing_stop_fraction"):
        parser.add_argument("--" + field.replace("_", "-"), default=getattr(TrendRegime(), field))
    parser.add_argument("--min-hold-bars", type=int, default=TrendRegime().min_hold_bars)
    parser.add_argument("--max-hold-bars", type=int, default=TrendRegime().max_hold_bars)
    parser.add_argument("--allow-short", action="store_true")
    parser.add_argument("--short-entry-threshold", default=TrendRegime().short_entry_threshold)
    parser.add_argument("--short-slow-threshold", default=TrendRegime().short_slow_threshold)
    parser.add_argument("--short-carry-bps-per-day",
                        default=TrendRegime().short_carry_bps_per_day)
    args = parser.parse_args(argv)
    started = perf_counter()
    dataset = load_causal_archive(Path(args.data_root), symbol=args.symbol,
        interval=args.interval, from_month=args.from_month, through_month=args.through_month,
        availability_policy=BAR_CLOSE_SCENARIO)
    bars = [dict(opened_at=datetime.fromisoformat(event.payload["open_time_utc"]),
                 closed_at=event.event_time,
                 **{name: event.payload[name] for name in ("open", "high", "low", "close", "volume")})
            for event in dataset.events]
    defaults = Costs()
    costs = Costs(**{field: getattr(args, field, getattr(defaults, field))
                     for field in Costs.__dataclass_fields__})
    configuration = TrendRegime(**{field: getattr(args, field)
                                   for field in TrendRegime.__dataclass_fields__})
    output = Path(args.output_dir)
    result = screen(bars, train_count=args.train_count,
        development_count=args.development_count,
        dataset_sha256=dataset.dataset_sha256.removeprefix("sha256:"), costs=costs,
        configuration=configuration, min_cycles=args.min_cycles,
        max_drawdown_fraction=args.max_drawdown_fraction,
        checkpoint=stage_checkpoint(output / "stages"))
    source = dict(kind="VERIFIED_ARCHIVE_RETROSPECTIVE_SCENARIO",
        availability_policy=BAR_CLOSE_SCENARIO, symbol=args.symbol,
        interval=args.interval, from_month=args.from_month, through_month=args.through_month)
    target = output / (result["config_sha256"] + ".json")
    save_model(target, dict(source=source, result=result))
    print(json.dumps(dict(result_path=str(target), observations=len(bars),
        elapsed_seconds=round(perf_counter()-started, 3),
        selected_candidate=result["selected_candidate"],
        training_rejection_reasons=result["training_rejection_reasons"],
        development_rejection_reasons=result["development_rejection_reasons"],
        holdout_consumed=result["holdout_consumed"], evaluation=result["evaluation"],
        result_sha256=result["result_sha256"], network_orders=False, paid_model_calls=0)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
