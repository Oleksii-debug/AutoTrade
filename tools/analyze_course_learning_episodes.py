#!/usr/bin/env python3
"""Create a durable TRAIN-only checkpoint from course-strategy episodes."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import tempfile

from research.autotrade_research.learning.course_episode_learning import analyze_course_episodes


def _atomic(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".course-learning-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, sort_keys=True, indent=2); stream.write("\n")
            stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--screen-dir", type=Path, required=True)
    parser.add_argument("--minimum-cycles", type=int, default=100)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    sources = []
    for path in sorted(args.screen_dir.glob("*.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        result = document["result"]
        episodes = result["train"].get("episodes", [])
        if episodes:
            sources.append({"market": document["source"]["market"],
                "dataset_identity_sha256": result["dataset_identity_sha256"],
                "episodes": episodes})
    value = analyze_course_episodes(sources, minimum_cycles=args.minimum_cycles)
    _atomic(args.output, value)
    print(json.dumps({"output": str(args.output), "status": value["status"],
        "cycles": value["baseline"]["cycles"],
        "pnl_after_costs": value["baseline"]["pnl_after_costs"],
        "candidate_hypothesis": value["candidate_hypothesis"],
        "network_orders": False}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

