"""Fail-closed reviewer of Section-8 same-run CI receipts, never a signing authority."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re

SHA = re.compile(r"^[0-9a-f]{40}$")
WORKFLOW = "Plan 3 whole-stack runtime recovery qualification"
EXPECTED = {
    ("plan3-whole-stack-section8", "Linux"): "qualify_plan3_stack.py --suite all",
    ("plan3-whole-stack-section8", "Windows"): "qualify_plan3_stack.py --suite all",
    ("plan3-native-host-section8", "Windows"): "Host.Process Release executable suite",
}
KEYS = {
    "schema_version", "source_sha", "checked_out_sha", "suite", "command",
    "result", "runner_os", "python_version", "github", "generated_at",
    "contains_secrets",
}
GITHUB_KEYS = {"event_name", "run_id", "run_attempt", "workflow"}


def verify(directory: Path, *, source: str, run_id: str,
           attempt: str, event: str) -> None:
    if SHA.fullmatch(source) is None:
        raise ValueError("invalid exact-source SHA")
    if not directory.is_dir() or directory.is_symlink():
        raise ValueError("missing or symlinked receipts directory")
    paths = sorted(directory.iterdir())
    if len(paths) != len(EXPECTED):
        raise ValueError("incomplete or surplus receipts")
    seen = set()
    for path in paths:
        if path.suffix != ".json" or path.is_symlink() or not path.is_file():
            raise ValueError("invalid receipt file")
        record = json.loads(path.read_text(encoding="utf-8"))
        if type(record) is not dict or set(record) != KEYS:
            raise ValueError("unexpected receipt schema")
        github = record["github"]
        if type(github) is not dict or set(github) != GITHUB_KEYS:
            raise ValueError("unexpected workflow identity schema")
        key = (record["suite"], record["runner_os"])
        if key not in EXPECTED or key in seen:
            raise ValueError("duplicate or wrong suite/OS")
        if (record["schema_version"] != "1.0.0"
            or record["source_sha"] != source
            or record["checked_out_sha"] != source
            or record["result"] != "PASS"
            or record["command"] != EXPECTED[key]
            or record["python_version"] != "3.12.10"
            or record["contains_secrets"] is not False
            or github["workflow"] != WORKFLOW
            or str(github["run_id"]) != run_id
            or str(github["run_attempt"]) != attempt
            or github["event_name"] != event):
            raise ValueError("receipt source/authority mismatch")
        seen.add(key)
    if seen != set(EXPECTED):
        raise ValueError("required independent campaigns missing")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", required=True, type=Path)
    parser.add_argument("--source", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--attempt", required=True)
    parser.add_argument("--event", required=True)
    args = parser.parse_args()
    try:
        verify(args.directory, source=args.source, run_id=args.run_id,
               attempt=args.attempt, event=args.event)
    except (ValueError, TypeError, KeyError, OSError, UnicodeError,
            json.JSONDecodeError) as exc:
        print(f"PLAN3_RECEIPTS_REJECTED: {type(exc).__name__}: {exc}")
        return 1
    print("PLAN3_RECEIPTS_ACCEPTED: 2 OS Python + Windows native Host, exact same source/run/attempt")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
