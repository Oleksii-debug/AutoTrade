"""Deterministic three-way adaptive-holding/exit historical experiment."""
import argparse
from datetime import datetime
from pathlib import Path
from time import perf_counter
import json

from research.autotrade_research.strategies.fast_screen import (
    AdaptiveExit, Costs, adaptive_exit_screen,
)
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
    parser.add_argument("--min-cycles", type=int, default=5)
    parser.add_argument("--max-drawdown-fraction", default="0.10")
    parser.add_argument("--output-dir", required=True)
    for field in ("initial_cash", "fee_bps", "spread_bps", "slippage_bps",
                  "capital_fraction", "threshold"):
        parser.add_argument("--" + field.replace("_", "-"),
                            default=getattr(Costs(), field))
    parser.add_argument("--lookback", type=int, default=Costs().lookback)
    parser.add_argument("--exit-lookback", type=int, default=AdaptiveExit().lookback)
    parser.add_argument("--exit-volatility-fraction",
                        default=AdaptiveExit().exit_volatility_fraction)
    parser.add_argument("--min-hold-bars", type=int, default=AdaptiveExit().min_hold_bars)
    parser.add_argument("--max-hold-bars", type=int, default=AdaptiveExit().max_hold_bars)
    args = parser.parse_args(argv)
    started = perf_counter()
    dataset = load_causal_archive(
        Path(args.data_root), symbol=args.symbol, interval=args.interval,
        from_month=args.from_month, through_month=args.through_month,
        availability_policy=BAR_CLOSE_SCENARIO,
    )
    bars = [dict(opened_at=datetime.fromisoformat(event.payload["open_time_utc"]),
                 closed_at=event.event_time,
                 **{name: event.payload[name]
                    for name in ("open", "high", "low", "close", "volume")})
            for event in dataset.events]
    costs = Costs(**{field: getattr(args, field) for field in Costs.__dataclass_fields__})
    adaptive_exit = AdaptiveExit(
        lookback=args.exit_lookback,
        exit_volatility_fraction=args.exit_volatility_fraction,
        min_hold_bars=args.min_hold_bars,
        max_hold_bars=args.max_hold_bars,
    )
    output = Path(args.output_dir)
    result = adaptive_exit_screen(
        bars, train_count=args.train_count, development_count=args.development_count,
        dataset_sha256=dataset.dataset_sha256.removeprefix("sha256:"),
        costs=costs, adaptive_exit=adaptive_exit, min_cycles=args.min_cycles,
        max_drawdown_fraction=args.max_drawdown_fraction,
        checkpoint=stage_checkpoint(output / "stages"),
    )
    source = dict(kind="VERIFIED_ARCHIVE_RETROSPECTIVE_SCENARIO",
                  availability_policy=BAR_CLOSE_SCENARIO, symbol=args.symbol,
                  interval=args.interval, from_month=args.from_month,
                  through_month=args.through_month)
    target = output / (result["config_sha256"] + ".json")
    save_model(target, dict(source=source, result=result))
    print(json.dumps(dict(result_path=str(target), observations=len(bars),
        elapsed_seconds=round(perf_counter()-started, 3),
        evidence_class=result["evidence_class"],
        selected_candidate=result["selected_candidate"],
        training_rejection_reasons=result["training_rejection_reasons"],
        development_rejection_reasons=result["development_rejection_reasons"],
        holdout_consumed=result["holdout_consumed"],
        evaluation=result["evaluation"], result_sha256=result["result_sha256"],
        network_orders=False, paid_model_calls=0)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
