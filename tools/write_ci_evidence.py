"""Write non-secret CI evidence only after the verification command succeeded."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys


GIT_SHA = re.compile(r"^[0-9a-f]{40}$")


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"{name} is required")
    return value


def checked_out_sha() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip().lower()


def build_evidence(*, suite: str, command: str) -> dict[str, object]:
    source_sha = _required_env("AUTOTRADE_SOURCE_SHA").lower()
    if GIT_SHA.fullmatch(source_sha) is None:
        raise ValueError("AUTOTRADE_SOURCE_SHA must be an exact Git SHA")
    actual = checked_out_sha()
    if actual != source_sha:
        raise ValueError(
            f"checkout SHA mismatch: expected {source_sha}, actual {actual}"
        )
    event = _required_env("GITHUB_EVENT_NAME")
    if event == "pull_request":
        # PR verification must never silently fall back to GitHub's synthetic
        # merge commit. The workflow supplies pull_request.head.sha explicitly.
        if os.environ.get("AUTOTRADE_PR_HEAD_SHA", "").strip().lower() != source_sha:
            raise ValueError("PR evidence is not bound to pull_request.head.sha")

    return {
        "schema_version": "1.0.0",
        "source_sha": source_sha,
        "checked_out_sha": actual,
        "suite": suite,
        "command": command,
        "result": "PASS",
        "runner_os": _required_env("RUNNER_OS"),
        "python_version": platform.python_version(),
        "github": {
            "event_name": event,
            "run_id": _required_env("GITHUB_RUN_ID"),
            "run_attempt": _required_env("GITHUB_RUN_ATTEMPT"),
            "workflow": _required_env("GITHUB_WORKFLOW"),
        },
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "contains_secrets": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--suite", required=True)
    parser.add_argument("--command", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    evidence = build_evidence(suite=args.suite, command=args.command)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(evidence, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(json.dumps(evidence, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
