"""Command-line demo for the safe simulated AutoTrade MVP."""

from dataclasses import asdict
from pathlib import Path
import argparse
import json

from .pipeline import run_multi_episode, run_vertical_slice


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


def _result_payload(result) -> dict:
    return {key: str(value) for key, value in asdict(result).items()}


def _parse_episodes(value: str) -> list[list[str]]:
    episodes = []
    for raw_episode in value.split(";"):
        prices = [item.strip() for item in raw_episode.split(",") if item.strip()]
        if prices:
            episodes.append(prices)
    if not episodes:
        raise ValueError("At least one episode is required")
    return episodes


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the network-free AutoTrade vertical-slice demo")
    parser.add_argument("--state-dir", default="mvp-state")
    parser.add_argument("--prices", default="100,101,102,103")
    parser.add_argument("--status", action="store_true", help="Show the current status")
    parser.add_argument(
        "--multi-episode",
        action="store_true",
        help="Run semicolon-separated price episodes, with comma-separated prices inside each episode",
    )
    args = parser.parse_args()
    if args.status:
        print(json.dumps(get_status(args.state_dir), indent=2))
        return 0
    if args.multi_episode:
        results = run_multi_episode(_parse_episodes(args.prices), args.state_dir)
        print(json.dumps([_result_payload(item) for item in results], indent=2))
        return 0
    result = run_vertical_slice(args.prices.split(","), args.state_dir)
    print(json.dumps(_result_payload(result), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
