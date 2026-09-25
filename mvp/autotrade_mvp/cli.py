"""Command-line demo for the safe simulated AutoTrade MVP."""

from dataclasses import asdict
from decimal import Decimal
import argparse
import json
from pathlib import Path

from .accessibility import format_accessible_status
from .economics import build_economic_report
from .pipeline import run_multi_episode, run_vertical_slice, verify_replay


def _jsonable_result(result) -> dict:
    payload = {}
    for key, value in asdict(result).items():
        payload[key] = str(value) if isinstance(value, Decimal) else value
    return payload


def get_status(state_dir: str) -> dict:
    """Get the current status of the AutoTrade MVP."""
    root = Path(state_dir)
    checkpoint_path = root / "checkpoint.json"
    evidence_path = root / "learning-evidence.jsonl"
    if not checkpoint_path.exists():
        return {"status": "not_started"}
    try:
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        evidence_count = len(evidence_path.read_text(encoding="utf-8").splitlines()) if evidence_path.exists() else 0
        return {
            "status": "running" if verify_replay(root) else "needs_recovery",
            "symbol": checkpoint.get("symbol"),
            "initial_cash": checkpoint.get("initial_cash"),
            "postings": checkpoint.get("postings", []),
            "fills": checkpoint.get("fills", {}),
            "evidence_count": evidence_count,
            "replay_verified": verify_replay(root),
        }
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the network-free AutoTrade vertical-slice demo")
    parser.add_argument("--state-dir", default="mvp-state")
    parser.add_argument("--prices", default="100,101,102,103")
    parser.add_argument("--status", action="store_true", help="Show the current status")
    parser.add_argument("--accessible-status", action="store_true", help="Show a stable plain-text status for keyboard and screen-reader use")
    parser.add_argument("--economic-report", action="store_true", help="Show evidence-bound simulated economics")
    parser.add_argument("--multi-episode", action="store_true", help="Run semicolon-separated episodes")
    args = parser.parse_args()
    if args.status:
        print(json.dumps(get_status(args.state_dir), indent=2))
        return 0
    if args.accessible_status:
        status = get_status(args.state_dir)
        economic_report = None
        if status.get("status") in {"running", "needs_recovery"}:
            try:
                economic_report = build_economic_report(args.state_dir).as_jsonable()
            except ValueError:
                economic_report = None
        print(format_accessible_status(status, economic_report))
        return 0
    if args.economic_report:
        print(json.dumps(build_economic_report(args.state_dir).as_jsonable(), indent=2))
        return 0
    if args.multi_episode:
        results = run_multi_episode(_parse_episodes(args.prices), args.state_dir)
        print(json.dumps([_jsonable_result(item) for item in results], indent=2))
    else:
        result = run_vertical_slice(args.prices.split(","), args.state_dir)
        print(json.dumps(_jsonable_result(result), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
