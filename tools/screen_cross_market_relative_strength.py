#!/usr/bin/env python3
"""Run the deterministic BTC/ETH relative-strength ZERO research screen."""
from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
from time import perf_counter
from uuid import uuid4

from research.autotrade_research.strategies.cross_market_relative_strength import (
    CheckpointStore,
    ExecutionCosts,
    GateConfiguration,
    StrategyConfiguration,
    digest,
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
            "open": event.payload["open"],
            "close": event.payload["close"],
        }
        for event in dataset.events
    ]


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    result.add_argument("--data-root", required=True)
    result.add_argument("--from-month", default="2024-01")
    result.add_argument("--through-month", default="2025-01")
    result.add_argument("--interval", default="1h")
    result.add_argument("--train-count", type=int, required=True)
    result.add_argument("--development-count", type=int, required=True)
    result.add_argument("--output-dir", required=True)
    for field, definition in ExecutionCosts.__dataclass_fields__.items():
        result.add_argument("--" + field.replace("_", "-"), default=definition.default)
    for field, definition in StrategyConfiguration.__dataclass_fields__.items():
        argument = "--" + field.replace("_", "-")
        value_type = int if definition.type == "int" or type(definition.default) is int else str
        result.add_argument(argument, type=value_type, default=definition.default)
    for field, definition in GateConfiguration.__dataclass_fields__.items():
        argument = "--" + field.replace("_", "-")
        value_type = int if definition.type == "int" or type(definition.default) is int else str
        result.add_argument(argument, type=value_type, default=definition.default)
    return result


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    started = perf_counter()
    root = Path(args.data_root)
    btc_dataset = load_causal_archive(
        root, symbol="BTCUSDT", interval=args.interval,
        from_month=args.from_month, through_month=args.through_month,
        availability_policy=BAR_CLOSE_SCENARIO,
    )
    eth_dataset = load_causal_archive(
        root, symbol="ETHUSDT", interval=args.interval,
        from_month=args.from_month, through_month=args.through_month,
        availability_policy=BAR_CLOSE_SCENARIO,
    )
    btc_bars, eth_bars = _bars(btc_dataset), _bars(eth_dataset)
    source = {
        "kind": "VERIFIED_ALIGNED_ARCHIVES_RETROSPECTIVE_SCENARIO",
        "btc_dataset_sha256": btc_dataset.dataset_sha256,
        "eth_dataset_sha256": eth_dataset.dataset_sha256,
        "symbols": ["BTCUSDT", "ETHUSDT"],
        "interval": args.interval,
        "from_month": args.from_month,
        "through_month": args.through_month,
        "network_orders": False,
    }
    dataset_sha256 = digest(source)
    costs = ExecutionCosts(**{
        field: getattr(args, field) for field in ExecutionCosts.__dataclass_fields__
    })
    strategy = StrategyConfiguration(**{
        field: getattr(args, field) for field in StrategyConfiguration.__dataclass_fields__
    })
    gate = GateConfiguration(**{
        field: getattr(args, field) for field in GateConfiguration.__dataclass_fields__
    })
    output = Path(args.output_dir)
    result = screen(
        btc_bars, eth_bars,
        train_count=args.train_count,
        development_count=args.development_count,
        dataset_sha256=dataset_sha256,
        costs=costs,
        strategy=strategy,
        gate=gate,
        checkpoint=CheckpointStore(output / "stages"),
    )
    artifact = {"source": source, "result": result}
    target = output / f"{result['config_sha256']}.json"
    _atomic_json(target, artifact)
    print(json.dumps({
        "result_path": str(target),
        "observations": len(btc_bars),
        "elapsed_seconds": round(perf_counter() - started, 3),
        "phase": result["phase"],
        "training_rejection_reasons": result["training_rejection_reasons"],
        "development_rejection_reasons": result["development_rejection_reasons"],
        "final_consumed": result["final_consumed"],
        "result_sha256": result["result_sha256"],
        "network_orders": False,
        "paid_model_calls": 0,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
