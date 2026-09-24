"""Command-line demo for the safe simulated AutoTrade MVP."""

from dataclasses import asdict
from pathlib import Path
import argparse
import json

from .pipeline import run_multi_episode, run_vertical_slice, verify_replay


def _serialize_result(result) -> dict:
    return {key: str(value) for key, value in asdict(result).items()}


def _parse_episodes(raw_prices: str) -> list[list[str]]:
    episodes = []
    for episode in raw_prices.split(";"):
        values = [value.strip() for value in episode.split(",") if value.strip()]
        if not values:
            raise ValueError("Each episode must contain at least one price")
        episodes.append(values)
    return episodes


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
            "replay_verified": verify_replay(root),
        }
    except (OSError, json.JSONDecodeError, TypeError):
        return {"status": "corrupt"}


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the network-free AutoTrade vertical-slice demo")
    parser.add_argument("--state-dir", default="mvp-state")
    parser.add_argument(
        "--prices",
        default="100,101,102,103",
        help="Comma-separated prices; use semicolons between episodes with --multi-episode",
    )
    parser.add_argument("--status", action="store_true", help="Show the current status")
    parser.add_argument("--multi-episode", action="store_true", help="Run multiple semicolon-separated episodes")
    args = parser.parse_args()

    if args.status:
        print(json.dumps(get_status(args.state_dir), indent=2))
        return 0

    if args.multi_episode:
        results = run_multi_episode(_parse_episodes(args.prices), args.state_dir)
        payload = [_serialize_result(result) for result in results]
    else:
        if ";" in args.prices:
            parser.error("Semicolon-separated episodes require --multi-episode")
        result = run_vertical_slice(args.prices.split(","), args.state_dir)
        payload = _serialize_result(result)

    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
