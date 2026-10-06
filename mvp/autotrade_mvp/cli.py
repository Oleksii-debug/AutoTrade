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
from .pipeline import (
    CHECKPOINT_SCHEMA_VERSION,
    run_multi_episode,
    run_vertical_slice,
    verify_replay,
)
from .simulation_session import run_canonical_simulation, run_autonomous_simulation
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
        if evidence_path.exists() or (root / "journal.sqlite3").exists():
            return {"status": "needs_recovery", "replay_verified": False}
        return {"status": "not_started"}
    try:
        checkpoint = strict_json_loads(checkpoint_path.read_text(encoding="utf-8"))
        if not isinstance(checkpoint, dict):
            return {"status": "corrupt"}
        schema_version = checkpoint.get("schema_version")
        if type(schema_version) is not int:
            return {"status": "corrupt"}
        evidence_count = len(evidence_path.read_text(encoding="utf-8").splitlines()) if evidence_path.exists() else 0
        replay_verified = verify_replay(root)
        common = {
            "symbol": checkpoint.get("symbol"),
            "initial_cash": checkpoint.get("initial_cash"),
            "postings": checkpoint.get("postings", []),
            "fills": checkpoint.get("fills", {}),
            "evidence_count": evidence_count,
            "replay_verified": replay_verified,
        }
        if schema_version == 1:
            return {
                "status": "needs_recovery",
                "resume_compatibility": "migration_required",
                **common,
            }
        if schema_version != CHECKPOINT_SCHEMA_VERSION:
            return {"status": "corrupt"}
        return {
            "status": "running" if replay_verified else "needs_recovery",
            "resume_compatibility": "current",
            **common,
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
    root = Path(state_dir)
    paths = (root / "checkpoint.json", root / "learning-evidence.jsonl")
    before = tuple(path.read_bytes() for path in paths)
    strict_json_loads(before[0].decode("utf-8"))
    for line in before[1].decode("utf-8").splitlines():
        strict_json_loads(line)
    if not verify_replay(root):
        raise ValueError("legacy economic evidence does not match its checkpoint")
    report = build_economic_report(root).as_jsonable()
    if tuple(path.read_bytes() for path in paths) != before:
        raise SimulationStateChanging("legacy state changed during economic read")
    return report


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
    modes.add_argument("--autonomous-simulation", action="store_true", help="Run/resume a frozen provider-free ZERO price stream through canonical risk, OMS and accounting")
    parser.add_argument("--stop-after-episodes", type=int, help="Pause the autonomous stream after this many observations")
    parser.add_argument("--fault-episode", type=int, help="Inject ambiguous internal send at this frozen episode")
    parser.add_argument("--emergency-episode", type=int, help="Enter frozen emergency NO_TRADE state at this episode")
    parser.add_argument("--execution-profile", choices=("IMMEDIATE", "TWO_EQUAL_PARTIALS"), default="IMMEDIATE",
                        help="Frozen ZERO execution: one full fill or two equal partial fills")
    parser.add_argument("--target-quantity", default="1", help="Exact frozen ZERO target quantity in instrument units")
    parser.add_argument("--episode-id", default="episode-1", help="Stable canonical simulation episode identity")
    parser.add_argument("--at", help="Optional ISO timestamp for deterministic simulation evidence")
    parser.add_argument("--fault-after-send", action="store_true", help="Inject an ambiguous simulated send, which will never be retried")
    args = parser.parse_args(argv)
    if not 1 <= args.history_limit <= 1000:
        parser.error("--history-limit must be between 1 and 1000")
    if args.autonomous_simulation and args.at is None:
        parser.error("--autonomous-simulation requires --at to freeze deterministic chronology")
    if args.at is not None and not (args.canonical_simulation or args.autonomous_simulation):
        parser.error("--at requires canonical or autonomous simulation")
    if args.fault_after_send and not args.canonical_simulation:
        parser.error("--fault-after-send requires --canonical-simulation")
    if any(value is not None for value in (args.stop_after_episodes, args.fault_episode, args.emergency_episode)) and not args.autonomous_simulation:
        parser.error("episode controls require --autonomous-simulation")
    if (args.execution_profile != "IMMEDIATE" or args.target_quantity != "1") and not args.autonomous_simulation:
        parser.error("execution controls require --autonomous-simulation")
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
    if args.autonomous_simulation:
        result = run_autonomous_simulation(args.prices.split(","), args.state_dir,
            run_id=args.episode_id, now=args.at, stop_after_episodes=args.stop_after_episodes,
            fault_at_episode=args.fault_episode, emergency_at_episode=args.emergency_episode,
            execution_profile=args.execution_profile, target_quantity=args.target_quantity)
        print(json.dumps(result, indent=2))
        return 2 if result["status"] == "UNKNOWN" else 0
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
        if canonical is None and status.get("status") == "running":
            try:
                economic_report = get_economic_report(args.state_dir)
            except (OSError, ValueError):
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
