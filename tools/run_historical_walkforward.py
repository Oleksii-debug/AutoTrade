"""Bounded monthly research loop over the canonical historical campaign.

Each month freezes a newly trained model, validates on the previous month,
and runs the next untouched month in two isolated simulation accounts. Once
that month is over, a later cut may learn from it. This is an internal
retrospective walk-forward scenario, not point-in-time publication evidence or
automatic strategy promotion to provider trading.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Callable

from tools.run_historical_campaign import _month, campaign


def _shift(month: str, delta: int) -> str:
    year, number = _month(month)
    absolute = year * 12 + number - 1 + delta
    return f"{absolute // 12:04d}-{absolute % 12 + 1:02d}"


def walk_forward(*, data_root: Path, workspace: Path, symbol: str,
                 train_from: str, first_holdout: str, last_holdout: str,
                 max_cycles: int, episode_limit: int | None = None,
                 progress: Callable[[dict], None] | None = None) -> dict:
    if type(max_cycles) is not int or not 1 <= max_cycles <= 120:
        raise ValueError("max_cycles must be in 1..120")
    if _month(first_holdout) > _month(last_holdout):
        raise ValueError("first holdout exceeds last holdout")
    if _month(train_from) > _month(_shift(first_holdout, -2)):
        raise ValueError("training must precede an independent validation month")
    current = datetime.now(timezone.utc).strftime("%Y-%m")
    if _month(last_holdout) >= _month(current):
        raise ValueError("holdouts must be completed UTC months")
    cuts = []
    month = first_holdout
    while _month(month) <= _month(last_holdout) and len(cuts) < max_cycles:
        validation = _shift(month, -1)
        training_end = _shift(month, -2)
        result = campaign(
            data_root=data_root, workspace=workspace, symbol=symbol,
            train_from=train_from, train_through=training_end,
            validate_from=validation, validate_through=validation,
            holdout_from=month, holdout_through=month,
            episode_limit=episode_limit,
        )
        cut = {"holdout_month": month, "training_through": training_end,
                     "model_sha256": result["model_sha256"],
                     "selected_strategy_parameters": result["selected_strategy_parameters"],
                     "closed_training_outcomes": result["closed_training_outcomes"],
                     "validation_report_sha256": result["validation_report_sha256"],
                     "accounts": result["accounts"]}
        cuts.append(cut)
        if progress is not None:
            progress(cut)
        month = _shift(month, 1)
    return {
        "status": "INTERNAL_RETROSPECTIVE_WALK_FORWARD_NOT_PROFITABILITY_EVIDENCE",
        "symbol": symbol, "completed_cuts": len(cuts),
        "next_holdout_month": month if _month(month) <= _month(last_holdout) else None,
        "cuts": cuts,
    }


def _text_progress(cut: dict) -> None:
    print(f"Simulation month {cut['holdout_month']}; trained through "
          f"{cut['training_through']}; model {cut['model_sha256']}; "
          f"parameters {json.dumps(cut['selected_strategy_parameters'], sort_keys=True)}; "
          f"closed simulation outcomes {cut['closed_training_outcomes']}.", flush=True)
    for budget, account in sorted(cut["accounts"].items(), key=lambda item: int(item[0])):
        print(f"Virtual USD {budget}: {account['status']}; "
              f"episodes {account['completed_episodes']}; "
              f"cash USD {account['cash_usd']}; "
              f"simulated marked P&L USD {account['agent_mark_pnl_usd']}; "
              f"maximum marked drawdown {account['agent_max_mark_drawdown_fraction']}; "
              f"hypothetical buy-and-hold P&L USD "
              f"{account['buy_hold_liquidation_scenario_pnl_usd']}.", flush=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Run bounded monthly historical simulation and learning cuts")
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--symbol", choices=("BTCUSDT", "ETHUSDT"), required=True)
    parser.add_argument("--train-from", default="2024-01")
    parser.add_argument("--first-holdout", required=True)
    parser.add_argument("--last-holdout", required=True)
    parser.add_argument("--max-cycles", type=int, default=1)
    parser.add_argument("--episode-limit", type=int)
    parser.add_argument("--text", action="store_true", help="Print screen-reader friendly progress after each cut")
    args = parser.parse_args(argv)
    try:
        result = walk_forward(
            data_root=Path(args.data_root), workspace=Path(args.workspace),
            symbol=args.symbol, train_from=args.train_from,
            first_holdout=args.first_holdout, last_holdout=args.last_holdout,
            max_cycles=args.max_cycles, episode_limit=args.episode_limit,
            progress=_text_progress if args.text else None,
        )
        if args.text:
            print(f"Completed {result['completed_cuts']} retrospective simulation cuts. "
                  f"Next holdout: {result['next_holdout_month'] or 'none in requested range'}. "
                  "These are estimated historical simulations, not real fills or proven profit.")
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, TypeError, ArithmeticError, KeyError) as exc:
        print(f"AutoTrade walk-forward blocked: {type(exc).__name__}: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
