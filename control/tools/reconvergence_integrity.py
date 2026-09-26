"""Fail-closed guard against malformed reconvergence commits.

The guard detects stale/diverged reconvergence, protected-control damage and
repository-tree destruction. When canonical mutation scopes are supplied, it also
binds every changed path to those scopes. A candidate must descend from the exact
base revision supplied by the pull-request event. Protected canonical sentinels
cannot be deleted, renamed away or changed to another Git object type. A PR that
deletes both a material absolute number and a material fraction of the base tree
is blocked.

This directly protects against commits accidentally built from a stale or partial
tree and against small unrelated changes hidden inside otherwise valid work.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
import subprocess
from typing import Iterable, Sequence

from control.tools.registry_state import _normalized_scopes, path_covers


MUTATION_SCOPE_BLOCK_START = "<!-- AUTOTRADE_MUTATION_SCOPE_BEGIN -->"
MUTATION_SCOPE_BLOCK_END = "<!-- AUTOTRADE_MUTATION_SCOPE_END -->"


PROTECTED_SENTINELS = frozenset(
    {
        ".github/workflows/verify.yml",
        ".github/workflows/reconvergence-integrity.yml",
        "AGENTS.md",
        "control/CONSTITUTION.md",
        "control/INDEX.json",
        "control/qualification.json",
        "control/tools/reconvergence_integrity.py",
        "control/work-packages/bank.json",
        "docs/product/PRODUCT_SPEC_CANONICAL.txt",
        "docs/engineering/00_AUTOTRADE_MASTER_ENGINEERING_SPEC.md",
        "requirements-dev.txt",
        "tools/verify.py",
    }
)


@dataclass(frozen=True)
class Change:
    status: str
    path: str
    previous_path: str | None = None


@dataclass(frozen=True)
class IntegrityAssessment:
    allowed: bool
    base_is_ancestor: bool
    base_path_count: int
    deletion_count: int
    deletion_fraction: float
    protected_deletions: tuple[str, ...]
    protected_violations: tuple[str, ...]
    scope_violations: tuple[str, ...]
    reasons: tuple[str, ...]


def parse_name_status(lines: Iterable[str]) -> tuple[Change, ...]:
    changes: list[Change] = []
    for raw in lines:
        line = raw.rstrip("\n")
        if not line:
            continue
        parts = line.split("\t")
        status = parts[0]
        kind = status[:1]
        if kind in {"R", "C"}:
            if len(parts) != 3:
                raise ValueError(f"Malformed rename/copy record: {line!r}")
            changes.append(Change(status=status, previous_path=parts[1], path=parts[2]))
        else:
            if len(parts) != 2:
                raise ValueError(f"Malformed name-status record: {line!r}")
            changes.append(Change(status=status, path=parts[1]))
    return tuple(changes)


def mutation_scopes_from_pull_request_body(body: object) -> tuple[str, ...]:
    """Read the exact declared mutation scope from one pull-request body.

    The declaration is intentionally machine-readable and fail-closed.  Human
    prose such as "Scope:" is not authority for repository mutation.
    """
    if not isinstance(body, str):
        raise ValueError("pull request body must contain a mutation-scope block")
    if body.count(MUTATION_SCOPE_BLOCK_START) != 1:
        raise ValueError("pull request body must contain exactly one mutation-scope start marker")
    if body.count(MUTATION_SCOPE_BLOCK_END) != 1:
        raise ValueError("pull request body must contain exactly one mutation-scope end marker")
    before, remainder = body.split(MUTATION_SCOPE_BLOCK_START, 1)
    payload, after = remainder.split(MUTATION_SCOPE_BLOCK_END, 1)
    del before, after
    raw_lines = payload.splitlines()
    scopes: list[str] = []
    for raw in raw_lines:
        if not raw:
            continue
        if raw != raw.strip():
            raise ValueError("mutation-scope block entries must not contain surrounding whitespace")
        scopes.append(raw)
    return _normalized_scopes(scopes)


def mutation_scopes_from_pull_request_event(path: str | Path) -> tuple[str, ...]:
    """Read a GitHub pull_request event and return its declared mutation scope."""
    event_path = Path(path)
    try:
        payload = json.loads(event_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("pull request event payload is unavailable or invalid") from exc
    if not isinstance(payload, dict):
        raise ValueError("pull request event payload must be an object")
    pull_request = payload.get("pull_request")
    if not isinstance(pull_request, dict):
        raise ValueError("pull request event payload is missing pull_request")
    return mutation_scopes_from_pull_request_body(pull_request.get("body"))


def assess_reconvergence(
    *,
    base_paths: Sequence[str],
    changes: Sequence[Change],
    max_deletions: int = 50,
    max_deleted_fraction: float = 0.35,
    protected_sentinels: frozenset[str] = PROTECTED_SENTINELS,
    base_is_ancestor: bool = True,
    allowed_scopes: Sequence[str] | None = None,
) -> IntegrityAssessment:
    if max_deletions < 1:
        raise ValueError("max_deletions must be positive")
    if not (0 < max_deleted_fraction <= 1):
        raise ValueError("max_deleted_fraction must be in (0, 1]")

    normalized_base = tuple(dict.fromkeys(base_paths))
    base_count = len(normalized_base)
    if base_count == 0:
        raise ValueError("base tree must contain at least one tracked path")

    deleted = tuple(sorted({change.path for change in changes if change.status == "D"}))
    protected = tuple(sorted(set(deleted).intersection(protected_sentinels)))
    fraction = len(deleted) / base_count

    protected_damage: set[str] = set(protected)
    for change in changes:
        kind = change.status[:1]
        if (
            kind == "R"
            and change.previous_path in protected_sentinels
            and change.path != change.previous_path
        ):
            protected_damage.add(
                f"{change.previous_path} -> {change.path} (rename)"
            )
        if kind == "T" and change.path in protected_sentinels:
            protected_damage.add(f"{change.path} (type change)")
    protected_violations = tuple(sorted(protected_damage))

    normalized_scopes: tuple[str, ...] | None = None
    if allowed_scopes is not None:
        normalized_scopes = _normalized_scopes(allowed_scopes)

    scope_damage: set[str] = set()
    if normalized_scopes is not None:
        for change in changes:
            kind = change.status[:1]
            if kind == "R":
                touched = (change.previous_path, change.path)
            elif kind == "C":
                # Copying does not mutate the source path.
                touched = (change.path,)
            else:
                touched = (change.path,)
            for path in touched:
                if path is None:
                    raise ValueError("changed path identity is missing")
                if not any(path_covers(scope, path) for scope in normalized_scopes):
                    scope_damage.add(path)
    scope_violations = tuple(sorted(scope_damage))

    reasons: list[str] = []
    if type(base_is_ancestor) is not bool:
        raise TypeError("base_is_ancestor must be boolean")
    if not base_is_ancestor:
        reasons.append("head is not descended from exact base revision")
    if protected_violations:
        reasons.append(
            "protected canonical sentinel damage: "
            + ", ".join(protected_violations)
        )
    if scope_violations:
        reasons.append(
            "changed paths outside declared mutation scope: "
            + ", ".join(scope_violations)
        )
    if len(deleted) >= max_deletions and fraction >= max_deleted_fraction:
        reasons.append(
            "mass base-tree deletion: "
            f"{len(deleted)}/{base_count} paths ({fraction:.1%})"
        )

    return IntegrityAssessment(
        allowed=not reasons,
        base_is_ancestor=base_is_ancestor,
        base_path_count=base_count,
        deletion_count=len(deleted),
        deletion_fraction=fraction,
        protected_deletions=protected,
        protected_violations=protected_violations,
        scope_violations=scope_violations,
        reasons=tuple(reasons),
    )


def _git_lines(
    *args: str,
    cwd: str | Path | None = None,
) -> tuple[str, ...]:
    completed = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return tuple(completed.stdout.splitlines())


def _git_is_ancestor(
    base: str,
    head: str,
    *,
    cwd: str | Path | None = None,
) -> bool:
    completed = subprocess.run(
        ["git", "merge-base", "--is-ancestor", base, head],
        cwd=cwd,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if completed.returncode == 0:
        return True
    if completed.returncode == 1:
        return False
    raise subprocess.CalledProcessError(
        completed.returncode,
        completed.args,
        output=completed.stdout,
        stderr=completed.stderr,
    )


def assess_git_revisions(
    base: str,
    head: str,
    *,
    max_deletions: int = 50,
    max_deleted_fraction: float = 0.35,
    allowed_scopes: Sequence[str] | None = None,
    cwd: str | Path | None = None,
) -> IntegrityAssessment:
    """Assess revisions inside one explicit Git repository/worktree.

    cwd defaults to the current process directory for the CLI workflow. Tests
    and library callers can bind revision identity to another repository. Every
    Git subprocess uses the same repository boundary.
    """

    base_is_ancestor = _git_is_ancestor(base, head, cwd=cwd)
    base_paths = _git_lines("ls-tree", "-r", "--name-only", base, cwd=cwd)
    changes = parse_name_status(
        _git_lines(
            "diff",
            "--name-status",
            "--find-renames",
            base,
            head,
            cwd=cwd,
        )
    )
    return assess_reconvergence(
        base_paths=base_paths,
        changes=changes,
        max_deletions=max_deletions,
        max_deleted_fraction=max_deleted_fraction,
        base_is_ancestor=base_is_ancestor,
        allowed_scopes=allowed_scopes,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fail closed on malformed reconvergence tree destruction."
    )
    parser.add_argument("--base", required=True, help="Exact base commit SHA/ref")
    parser.add_argument("--head", required=True, help="Exact head commit SHA/ref")
    parser.add_argument("--max-deletions", type=int, default=50)
    parser.add_argument("--max-deleted-fraction", type=float, default=0.35)
    parser.add_argument(
        "--allowed-scope",
        action="append",
        default=None,
        help=(
            "Canonical literal repository-relative mutation scope. Repeat to "
            "permit multiple scopes; when omitted, scope enforcement is disabled."
        ),
    )
    parser.add_argument(
        "--pull-request-event",
        default=None,
        help=(
            "GitHub pull_request event JSON. When supplied, the canonical "
            "machine-readable mutation-scope block in the PR body is required."
        ),
    )
    args = parser.parse_args(argv)
    if args.pull_request_event is not None and args.allowed_scope is not None:
        parser.error("--pull-request-event and --allowed-scope are mutually exclusive")
    allowed_scopes = args.allowed_scope
    if args.pull_request_event is not None:
        try:
            allowed_scopes = mutation_scopes_from_pull_request_event(
                args.pull_request_event
            )
        except ValueError as exc:
            parser.error(str(exc))

    assessment = assess_git_revisions(
        args.base,
        args.head,
        max_deletions=args.max_deletions,
        max_deleted_fraction=args.max_deleted_fraction,
        allowed_scopes=allowed_scopes,
    )
    print(
        "Reconvergence tree guard: "
        f"base_is_ancestor={str(assessment.base_is_ancestor).lower()} "
        f"base_paths={assessment.base_path_count} "
        f"deletions={assessment.deletion_count} "
        f"deleted_fraction={assessment.deletion_fraction:.3f} "
        f"protected_violations={len(assessment.protected_violations)} "
        f"scope_violations={len(assessment.scope_violations)}"
    )
    if assessment.allowed:
        print("Reconvergence tree guard passed.")
        return 0

    for reason in assessment.reasons:
        print(f"BLOCKED: {reason}")
    print(
        "Build reconvergence commits from the full current-main base tree and "
        "verify the exact owned changed-file surface before integration."
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
