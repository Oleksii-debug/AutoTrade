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
from dataclasses import dataclass
import json
from pathlib import Path
import subprocess
from typing import Iterable, Sequence

from control.tools.registry_state import _normalized_scopes, path_covers

PROTECTED_SENTINELS = frozenset(
    {
        ".github/workflows/baseline.yml",
        ".github/workflows/contracts.yml",
        ".github/workflows/control-plane.yml",
        ".github/workflows/dotnet-foundation.yml",
        ".github/workflows/futures-qualification.yml",
        ".github/workflows/lean-adoption.yml",
        ".github/workflows/reconvergence-integrity.yml",
        ".github/workflows/research-primitives.yml",
        ".github/workflows/science-qualification.yml",
        ".github/workflows/verify.yml",
        ".github/workflows/zero-model-qualification.yml",
        "AGENTS.md",
        "control/CONSTITUTION.md",
        "control/INDEX.json",
        "control/qualification.json",
        "control/work-packages/bank.json",
        "control/tools/reconvergence_integrity.py",
        "control/tools/registry_state.py",
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


_GIT_OBJECT_HEX = frozenset("0123456789abcdef")


def _exact_git_object_id(value: str, *, field: str) -> str:
    if (
        type(value) is not str
        or len(value) not in {40, 64}
        or value != value.lower()
        or any(character not in _GIT_OBJECT_HEX for character in value)
    ):
        raise ValueError(f"{field} must be an exact lowercase Git object id")
    return value


def reconvergence_evidence(
    *,
    base_sha: str,
    head_sha: str,
    assessment: IntegrityAssessment,
    max_deletions: int,
    max_deleted_fraction: float,
    scope_enforced: bool,
) -> dict[str, object]:
    """Machine-readable exact-revision evidence from the trusted-base guard."""

    if type(assessment) is not IntegrityAssessment:
        raise TypeError("assessment must be IntegrityAssessment")
    if type(max_deletions) is not int or max_deletions < 1:
        raise ValueError("max_deletions must be a positive exact integer")
    if (
        type(max_deleted_fraction) is not float
        or not (0.0 < max_deleted_fraction <= 1.0)
    ):
        raise ValueError("max_deleted_fraction must be an exact float in (0, 1]")
    if type(scope_enforced) is not bool:
        raise TypeError("scope_enforced must be bool")
    base_sha = _exact_git_object_id(base_sha, field="base_sha")
    head_sha = _exact_git_object_id(head_sha, field="head_sha")
    unresolved_limits = (
        []
        if scope_enforced
        else [
            "mutation_scope_not_enforced_without_trusted_external_scope"
        ]
    )
    return {
        "schema_version": "1.0.0",
        "source_sha": head_sha,
        "trusted_guard_source_sha": base_sha,
        "base_sha": base_sha,
        "head_sha": head_sha,
        "input_schema_version": "git-tree-reconvergence/v1",
        "result": "PASS" if assessment.allowed else "FAIL",
        "policy": {
            "max_deletions": max_deletions,
            "max_deleted_fraction": max_deleted_fraction,
            "scope_enforced": scope_enforced,
        },
        "checks_run": [
            "exact-base-ancestry",
            "protected-sentinel-integrity",
            "mass-base-tree-deletion",
            *(
                ["trusted-mutation-scope"]
                if scope_enforced
                else []
            ),
        ],
        "unresolved_limits": unresolved_limits,
        "base_is_ancestor": assessment.base_is_ancestor,
        "base_path_count": assessment.base_path_count,
        "deletion_count": assessment.deletion_count,
        "deletion_fraction": assessment.deletion_fraction,
        "protected_deletions": list(assessment.protected_deletions),
        "protected_violations": list(assessment.protected_violations),
        "scope_violations": list(assessment.scope_violations),
        "reasons": list(assessment.reasons),
        "contains_secrets": False,
    }


def write_reconvergence_evidence(
    output: Path,
    *,
    base_sha: str,
    head_sha: str,
    assessment: IntegrityAssessment,
    max_deletions: int,
    max_deleted_fraction: float,
    scope_enforced: bool,
) -> None:
    evidence = reconvergence_evidence(
        base_sha=base_sha,
        head_sha=head_sha,
        assessment=assessment,
        max_deletions=max_deletions,
        max_deleted_fraction=max_deleted_fraction,
        scope_enforced=scope_enforced,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(evidence, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


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
            changes.append(
                Change(status=status, previous_path=parts[1], path=parts[2])
            )
        else:
            if len(parts) != 2:
                raise ValueError(f"Malformed name-status record: {line!r}")
            changes.append(Change(status=status, path=parts[1]))
    return tuple(changes)


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


def _git_commit_id(
    revision: str,
    *,
    cwd: str | Path | None = None,
) -> str:
    lines = _git_lines("rev-parse", "--verify", f"{revision}^{{commit}}", cwd=cwd)
    if len(lines) != 1:
        raise ValueError("Git revision did not resolve to exactly one commit")
    return _exact_git_object_id(lines[0], field="resolved revision")


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
        "--evidence-output",
        type=Path,
        default=None,
        help=(
            "Optional machine-readable evidence path. The trusted-base guard "
            "writes exact resolved base/head identities and PASS/FAIL before exit."
        ),
    )
    parser.add_argument(
        "--allowed-scope",
        action="append",
        default=None,
        help=(
            "Trusted externally resolved repository-relative mutation scope. "
            "Repeat to permit multiple scopes; never derive this authority from "
            "PR-authored metadata. When omitted, scope enforcement is disabled."
        ),
    )
    args = parser.parse_args(argv)
    allowed_scopes = args.allowed_scope

    base_sha = _git_commit_id(args.base)
    head_sha = _git_commit_id(args.head)
    assessment = assess_git_revisions(
        base_sha,
        head_sha,
        max_deletions=args.max_deletions,
        max_deleted_fraction=args.max_deleted_fraction,
        allowed_scopes=allowed_scopes,
    )
    if args.evidence_output is not None:
        write_reconvergence_evidence(
            args.evidence_output,
            base_sha=base_sha,
            head_sha=head_sha,
            assessment=assessment,
            max_deletions=args.max_deletions,
            max_deleted_fraction=args.max_deleted_fraction,
            scope_enforced=allowed_scopes is not None,
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
