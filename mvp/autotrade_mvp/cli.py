"""Command-line demo for the safe simulated AutoTrade MVP."""

from dataclasses import asdict
import argparse
import json
from pathlib import Path

from .pipeline import run_vertical_slice, run_multi_episode


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the network-free AutoTrade vertical-slice demo")
    parser.add_argument("--state-dir", default="mvp-state")
    parser.add_argument("--prices", default="100,101,102,103")
    parser.add_argument("--status", action="store_true", help="Show the current status")
    parser.add_argument("--multi-episode", action="store_true", help="Run multiple episodes")
    args = parser.parse_args()
    if args.status:
        status = get_status(args.state_dir)
        print(json.dumps(status, indent=2))
        return 0
    if args.multi_episode:
        episodes = [args.prices.split(",")]
        result = run_multi_episode(episodes, args.state_dir)
    else:
        result = run_vertical_slice(args.prices.split(","), args.state_dir)
    print(json.dumps({key: str(value) for key, value in asdict(result).items()}, indent=2))
    return 0


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
            "status": "running",
            "symbol": checkpoint.get("symbol"),
            "initial_cash": checkpoint.get("initial_cash"),
            "postings": checkpoint.get("postings", []),
            "fills": checkpoint.get("fills", {}),
            "evidence_count": evidence_count,
        }
    except (OSError, json.JSONDecodeError):
        return {"status": "corrupt"}


if __name__ == "__main__":
    raise SystemExit(main())
