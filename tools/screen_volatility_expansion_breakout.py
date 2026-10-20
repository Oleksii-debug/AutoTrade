#!/usr/bin/env python3
"""Run the deterministic BTC volatility-expansion ZERO research screen."""
from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
from time import perf_counter
from uuid import uuid4

from research.autotrade_research.strategies.volatility_expansion_breakout import (
    CheckpointStore,
    ExecutionCosts,
    GateConfiguration,
    StrategyConfiguration,
    screen,
)
from tools.historical_causal_replay import BAR_CLOSE_SCENARIO, load_causal_archive


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    temporary = path.with_suffix(f".{os.getpid()}.{uuid4().hex}.tmp")
    with temporary.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _bars(dataset) -> list[dict]:
    return [
        {
            "opened_at": datetime.fromisoformat(event.payload["open_time_utc"]),
            "closed_at": event.event_time,
            **{
                name: event.payload[name]
                for name in ("open", "high", "low", "close", "volume")
            },
        }
        for event in dataset.events
    ]


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--data-root", required=True)
    result.add_argument("--symbol", default="BTCUSDT", choices=("BTCUSDT",))
    result.add_argument("--interval", default="1h", choices=("1h",))
    result.add_argument("--from-month", default="2024-01")
    result.add_argument("--through-month", default="2025-01")
    result.add_argument("--train-count", type=int, required=True)
    result.add_argument("--development-count", type=int, required=True)
    result.add_argument("--output-dir", required=True)
    for field, definition in ExecutionCosts.__dataclass_fields__.items():
        result.add_argument("--" + field.replace("_", "-"), default=definition.default)
    for field, definition in StrategyConfiguration.__dataclass_fields__.items():
        argument = "--" + field.replace("_", "-")
        value_type = int if type(definition.default) is int else str
        result.add_argument(argument, type=value_type, default=definition.default)
    result.add_argument(
        "--minimum-closed-cycles", type=int,
        default=GateConfiguration().minimum_closed_cycles,
    )
    result.add_argument(
        "--maximum-drawdown-fraction",
        default=GateConfiguration().maximum_drawdown_fraction,
    )
    result.add_argument(
        "--allow-underperform-hold", action="store_true",
        help="Do not require the candidate to beat buy-and-hold on each gate.",
    )
    return result


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    started = perf_counter()
    dataset = load_causal_archive(
        Path(args.data_root), symbol=args.symbol, interval=args.interval,
        from_month=args.from_month, through_month=args.through_month,
        availability_policy=BAR_CLOSE_SCENARIO,
    )
    bars = _bars(dataset)
    costs = ExecutionCosts(**{
        field: getattr(args, field) for field in ExecutionCosts.__dataclass_fields__
    })
    strategy = StrategyConfiguration(**{
        field: getattr(args, field) for field in StrategyConfiguration.__dataclass_fields__
    })
    gate = GateConfiguration(
        minimum_closed_cycles=args.minimum_closed_cycles,
        maximum_drawdown_fraction=args.maximum_drawdown_fraction,
        require_beats_hold=not args.allow_underperform_hold,
    )
    output = Path(args.output_dir)
    result = screen(
        bars, train_count=args.train_count,
        development_count=args.development_count,
        dataset_sha256=dataset.dataset_sha256.removeprefix("sha256:"),
        costs=costs, strategy=strategy, gate=gate,
        checkpoint=CheckpointStore(output / "stages"),
    )
    source = {
        "kind": "VERIFIED_ARCHIVE_RETROSPECTIVE_SCENARIO",
        "availability_policy": BAR_CLOSE_SCENARIO,
        "symbol": args.symbol,
        "execution_compatibility": "WHITEBIT_SPOT_BTC_USDT_LONG_ONLY",
        "archive_venue_is_not_execution_venue": True,
        "interval": args.interval,
        "from_month": args.from_month,
        "through_month": args.through_month,
        "dataset_sha256": dataset.dataset_sha256,
        "network_orders": False,
        "paid_model_calls": 0,
    }
    artifact = {"source": source, "result": result}
    target = output / f"{result['config_sha256']}.json"
    _atomic_json(target, artifact)
    print(json.dumps({
        "result_path": str(target),
        "observations": len(bars),
        "elapsed_seconds": round(perf_counter() - started, 3),
        "phase": result["phase"],
        "training_rejection_reasons": result["training_rejection_reasons"],
        "development_consumed": result["development_consumed"],
        "final_consumed": result["final_consumed"],
        "result_sha256": result["result_sha256"],
        "network_orders": False,
        "paid_model_calls": 0,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
