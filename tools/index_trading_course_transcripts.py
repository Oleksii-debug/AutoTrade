#!/usr/bin/env python3
"""Create a content-free provenance/topic index for private course transcripts."""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import tempfile


SCHEMA = "autotrade-private-course-transcript-index-v1"
TOPICS = {
    "RISK": r"риск|risk|стоп|stop.?loss|просад|drawdown|позици",
    "DISCIPLINE": r"дисциплин|discipline|overtrad|овертрейд|fomo|revenge|журнал",
    "LIQUIDITY": r"ликвид|liquidity|свип|sweep|equal (?:high|low)",
    "MARKET_STRUCTURE": r"структур|structure|shift|шифт|bos|choch|тренд",
    "POI": r"fair value|фейр|fvg|order.?block|ордер.?блок|rejection.?block",
    "PREMIUM_DISCOUNT": r"premium|discount|премиум|дискаунт|0[,.]5|0[,.]618|0[,.]705|0[,.]786",
    "RANGE": r"консолидац|боковик|range|рейндж|девиац|inside bar",
    "QUASIMODO": r"quasi|квази|квазимод",
    "AUCTION": r"auction|аукцион|value area|\bpoc\b|профил",
}


def build_index(root: Path) -> dict:
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("transcript root must be a directory")
    rows = []
    for path in sorted(root.rglob("*.txt"), key=lambda value: str(value).casefold()):
        if path.is_symlink() or not path.is_file():
            raise ValueError("transcript source must be a regular non-symlink file")
        raw = path.read_bytes()
        if len(raw) > 10_000_000:
            raise ValueError("individual transcript exceeds 10 MB")
        text = raw.decode("utf-8-sig")
        counts = {topic: len(re.findall(pattern, text, flags=re.IGNORECASE))
                  for topic, pattern in TOPICS.items()}
        rows.append({"relative_path": path.relative_to(root).as_posix(),
                     "bytes": len(raw), "sha256": sha256(raw).hexdigest(),
                     "word_count": len(text.split()), "topic_counts": counts,
                     "primary_topics": [name for name, count in sorted(
                         counts.items(), key=lambda item: (-item[1], item[0])) if count][:4]})
    if not rows:
        raise ValueError("no transcript text files found")
    result = {"schema": SCHEMA, "source_root_name": root.name,
              "file_count": len(rows), "total_bytes": sum(row["bytes"] for row in rows),
              "files": rows, "contains_transcript_text": False,
              "purpose": "PRIVATE_RESEARCH_PROVENANCE_AND_TOPIC_ROUTING"}
    body = json.dumps(result, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    result["index_sha256"] = sha256(body.encode()).hexdigest()
    return result


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".course-index-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, sort_keys=True, indent=2, ensure_ascii=False)
            stream.write("\n"); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    result = build_index(args.source_root)
    atomic_json(args.output, result)
    print(json.dumps({key: result[key] for key in (
        "schema", "file_count", "total_bytes", "index_sha256",
        "contains_transcript_text")}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
