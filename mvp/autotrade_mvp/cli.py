"""Command-line demo for the safe simulated AutoTrade MVP."""

from dataclasses import asdict
from decimal import Decimal
import argparse
import json
from pathlib import Path
import sqlite3
import sys

from .accessibility import format_accessible_status
from .economics import build_economic_report
from .pipeline import run_multi_episode, run_vertical_slice, verify_replay
from .simulation_session import run_canonical_simulation
from .simulation_status import inspect_canonical_simulation, SimulationStateChanging
from research.autotrade_research.io.strict_json import strict_json_loads


def _jsonable_result(result) -> dict:
    payload = {}
    for key, value in asdict(result).items():
        payload[key] = str(value) if isinstance(value, Decimal) else value
    return payload


def get_status(state_dir: str) -> dict:
    """Get the current status of the AutoTrade MVP."""
    canonical = _read_canonical_state(state_dir)
    if canonical is not None:
        return canonical["status"]
    return _legacy_status(state_dir)


def _read_canonical_state(state_dir: str) -> dict | None:
    try:
        return inspect_canonical_simulation(state_dir)
    except SimulationStateChanging:
        state = "busy"
    except (OSError, sqlite3.Error, RuntimeError, KeyError, TypeError, ValueError, ArithmeticError):
        state = "corrupt"
    return {"status": {"status": state, "state_format": "canonical_journal"},
            "economic_report": None}


def _legacy_status(state_dir: str) -> dict:
    root = Path(state_dir)
    checkpoint_path = root / "checkpoint.json"
    evidence_path = root / "learning-evidence.jsonl"
    if not checkpoint_path.exists():
        return {"status": "not_started"}
    try:
        checkpoint = strict_json_loads(checkpoint_path.read_text(encoding="utf-8"))
        if not isinstance(checkpoint, dict):
            return {"status": "corrupt"}
        evidence_count = len(evidence_path.read_text(encoding="utf-8").splitlines()) if evidence_path.exists() else 0
        replay_verified = verify_replay(root)
        return {
            "status": "running" if replay_verified else "needs_recovery",
            "symbol": checkpoint.get("symbol"),
            "initial_cash": checkpoint.get("initial_cash"),
            "postings": checkpoint.get("postings", []),
            "fills": checkpoint.get("fills", {}),
            "evidence_count": evidence_count,
            "replay_verified": replay_verified,
        }
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError, ArithmeticError):
        return {"status": "corrupt"}


def _parse_episodes(raw: str) -> list[list[str]]:
    episodes = []
    for episode in raw.split(";"):
        prices = [item.strip() for item in episode.split(",") if item.strip()]
        if prices:
            episodes.append(prices)
    if not episodes:
        raise ValueError("At least one episode with one price is required")
    return episodes


def get_economic_report(state_dir: str) -> dict:
    canonical = inspect_canonical_simulation(state_dir)
    if canonical is not None:
        if canonical["economic_report"] is None:
            raise ValueError("simulation requires reconciliation before an economic report")
        return canonical["economic_report"]
    return build_economic_report(state_dir).as_jsonable()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the network-free AutoTrade vertical-slice demo")
    parser.add_argument("--state-dir", default="mvp-state")
    parser.add_argument("--prices", default="100,101,102,103")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--status", action="store_true", help="Show the current status")
    modes.add_argument("--accessible-status", action="store_true", help="Show a stable plain-text status for keyboard and screen-reader use")
    modes.add_argument("--economic-report", action="store_true", help="Show evidence-bound simulated economics")
    modes.add_argument("--history", action="store_true", help="Read bounded canonical journal history without event payloads")
    parser.add_argument("--history-limit", type=int, default=100, help="History rows, from 1 to 1000 (default 100)")
    modes.add_argument("--multi-episode", action="store_true", help="Run semicolon-separated legacy episodes")
    modes.add_argument("--canonical-simulation", action="store_true", help="Run one journal-backed canonical simulated episode")
    parser.add_argument("--episode-id", default="episode-1", help="Stable canonical simulation episode identity")
    parser.add_argument("--at", help="Optional ISO timestamp for deterministic simulation evidence")
    parser.add_argument("--fault-after-send", action="store_true", help="Inject an ambiguous simulated send, which will never be retried")
    args = parser.parse_args(argv)
    if not 1 <= args.history_limit <= 1000:
        parser.error("--history-limit must be between 1 and 1000")
    if (args.at is not None or args.fault_after_send) and not args.canonical_simulation:
        parser.error("--at and --fault-after-send require --canonical-simulation")
    try:
        return _execute(args)
    except SimulationStateChanging:
        print("AutoTrade: state changed during reading; try the read again.", file=sys.stderr)
        return 2
    except (OSError, sqlite3.Error, RuntimeError, KeyError, TypeError, ValueError, ArithmeticError):
        # Corrupt state and transport errors can contain retained data. Keep
        # the operator error stable, payload-free and free of stack traces.
        print("AutoTrade: unable to complete this command. Check inputs and state; unresolved sends require reconciliation.", file=sys.stderr)
        return 2


def _execute(args) -> int:
    if args.canonical_simulation:
        if any((Path(args.state_dir) / name).exists()
               for name in ("checkpoint.json", "learning-evidence.jsonl")):
            raise ValueError("legacy state requires a separate canonical simulation directory")
        result = run_canonical_simulation(
            args.prices.split(","), args.state_dir, episode_id=args.episode_id,
            now=args.at, fault_after_send=args.fault_after_send,
        )
        print(json.dumps(result, indent=2))
        return 0
    if args.status:
        status = get_status(args.state_dir)
        print(json.dumps(status, indent=2))
        return 2 if status["status"] in {"corrupt", "busy"} else 0
    if args.accessible_status:
        canonical = _read_canonical_state(args.state_dir)
        status = canonical["status"] if canonical else _legacy_status(args.state_dir)
        economic_report = canonical["economic_report"] if canonical else None
        if canonical is None and status.get("status") in {"running", "needs_recovery"}:
            try:
                economic_report = build_economic_report(args.state_dir).as_jsonable()
            except ValueError:
                economic_report = None
        print(format_accessible_status(status, economic_report))
        return 2 if status["status"] in {"corrupt", "busy"} else 0
    if args.economic_report:
        print(json.dumps(get_economic_report(args.state_dir), indent=2))
        return 0
    if args.history:
        canonical = inspect_canonical_simulation(args.state_dir, history_limit=args.history_limit)
        if canonical is None:
            raise ValueError("canonical journal history is unavailable")
        print(json.dumps({"journal_sequence": canonical["status"]["journal_sequence"],
                          "events": canonical["status"]["history"]}, indent=2))
        return 0
    if inspect_canonical_simulation(args.state_dir) is not None:
        raise ValueError("a canonical journal cannot be used for a legacy execution")
    if args.multi_episode:
        results = run_multi_episode(_parse_episodes(args.prices), args.state_dir)
        print(json.dumps([_jsonable_result(item) for item in results], indent=2))
    else:
        result = run_vertical_slice(args.prices.split(","), args.state_dir)
        print(json.dumps(_jsonable_result(result), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
