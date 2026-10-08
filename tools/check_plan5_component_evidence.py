"""Strict Plan-5 cross-job CI receipt consistency, NEVER terminal approval.

Receipts are produced by the existing tools/write_ci_evidence.py on separate
OS/browser jobs. This checks exact-run structural binding only. It does NOT
authenticate GitHub's API, sign evidence, issue DONE or approve a release.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import re

_SHA = re.compile(r"^[0-9a-f]{40}$")
_POSITIVE_INTEGER = re.compile(r"^[1-9][0-9]*$")
_EXPECTED = {
    "source": ("plan5-terminal-source.json", "plan5-terminal-source-and-recovery", "Linux"),
    "browser": ("plan5-terminal-browser.json", "plan5-terminal-keyboard-browser", "Linux"),
    "desktop": ("plan5-terminal-desktop.json", "plan5-terminal-desktop-native", "Windows"),
}
_ROOT_FIELDS = frozenset({
    "schema_version", "source_sha", "checked_out_sha", "suite", "command",
    "result", "runner_os", "python_version", "github", "generated_at",
    "contains_secrets",
})
_GITHUB_FIELDS = frozenset({
    "event_name", "run_id", "run_attempt", "workflow",
})


def _pairs_unique(pairs):
    output = {}
    for key, value in pairs:
        if key in output:
            raise ValueError("duplicate CI evidence JSON field: " + key)
        output[key] = value
    return output


def _read_receipt(root: Path, group: str, filename: str):
    parent = root / group
    path = parent / filename
    if parent.is_symlink() or path.is_symlink():
        raise ValueError("CI receipt path is a symlink")
    if not path.is_file():
        raise ValueError("missing required CI receipt: " + group)
    if path.stat().st_size > 16384:
        raise ValueError("CI receipt exceeds bounded source envelope")
    try:
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs_unique)
    except (OSError, UnicodeError, ValueError) as error:
        raise ValueError("CI receipt is not unambiguous UTF-8 JSON: " + group) from error
    if type(value) is not dict or frozenset(value) != _ROOT_FIELDS:
        raise ValueError("CI receipt fields differ from canonical writer: " + group)
    return value


def inspect_component_receipts(
    evidence_dir: Path, *,
    source_sha: str, run_id: str, run_attempt: str, event_name: str,
) -> dict:
    if (
        type(source_sha) is not str or _SHA.fullmatch(source_sha) is None
        or type(run_id) is not str or _POSITIVE_INTEGER.fullmatch(run_id) is None
        or type(run_attempt) is not str or _POSITIVE_INTEGER.fullmatch(run_attempt) is None
        or event_name not in ("pull_request", "workflow_dispatch")
    ):
        raise ValueError("expected component identity is invalid")
    if type(evidence_dir) is not Path or not evidence_dir.is_dir() or evidence_dir.is_symlink():
        raise ValueError("CI evidence root must be a regular directory")
    received = []
    for group, (filename, suite, runner) in _EXPECTED.items():
        record = _read_receipt(evidence_dir, group, filename)
        github = record["github"]
        if type(github) is not dict or frozenset(github) != _GITHUB_FIELDS:
            raise ValueError("CI receipt GitHub metadata is invalid: " + group)
        if (
            record["schema_version"] != "1.0.0"
            or record["source_sha"] != source_sha
            or record["checked_out_sha"] != source_sha
            or record["suite"] != suite
            or type(record["command"]) is not str or not record["command"].strip()
            or record["result"] != "PASS"
            or record["runner_os"] != runner
            or type(record["python_version"]) is not str or not record["python_version"]
            or record["contains_secrets"] is not False
            or github["event_name"] != event_name
            or github["run_id"] != run_id
            or github["run_attempt"] != run_attempt
            or github["workflow"] != "plan5-terminal-component-qualification"
        ):
            raise ValueError("CI receipt does not match exact run/suite/source: " + group)
        try:
            value = record["generated_at"]
            if type(value) is not str:
                raise ValueError("non-text time")
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise ValueError("naive time")
        except ValueError as error:
            raise ValueError("CI receipt timestamp is invalid: " + group) from error
        received.append(suite)
    return {
        "schema_version": "1.0.0",
        "source_sha": source_sha,
        "run_id": run_id,
        "run_attempt": run_attempt,
        "evidence_class": "CI_STRUCTURAL_CONSISTENCY_ONLY",
        "suite_names": received,
        "terminal_done": False,
        "release_qualified": False,
        "independent_GitHub_API_authentication": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-dir", required=True, type=Path)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-attempt", required=True)
    parser.add_argument("--event", required=True)
    args = parser.parse_args()
    checked = inspect_component_receipts(
        args.evidence_dir,
        source_sha=args.source_sha,
        run_id=args.run_id,
        run_attempt=args.run_attempt,
        event_name=args.event,
    )
    print(json.dumps(checked, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
