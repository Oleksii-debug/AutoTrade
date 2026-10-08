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
from pathlib import Path
import re
import subprocess
from typing import Iterable, Sequence

from control.tools.registry_state import _normalized_scopes, path_covers

SUPPORTED_CHANGE_KINDS = frozenset({"A", "C", "D", "M", "R", "T"})

PROTECTED_SENTINELS = frozenset(
    {
        ".github/workflows/baseline.yml",
        ".github/workflows/contracts.yml",
        ".github/workflows/control-plane.yml",
        ".github/workflows/dotnet-foundation.yml",
        ".github/workflows/futures-qualification.yml",
        ".github/workflows/plan3-recovery-qualification.yml",
        ".github/workflows/plan4-trusted-chronology.yml",
        ".github/workflows/plan5-web-component.yml",
        ".github/workflows/provider-free-product.yml",
        ".github/workflows/section15-portfolio-qualification.yml",
        ".github/workflows/recovery-qualification.yml",
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

PROTECTED_MUTATION_ROOTS = frozenset(
    {
        ".github/workflows/reconvergence-integrity.yml",
        "control/tools/reconvergence_integrity.py",
    }
)

TRUSTED_SCOPE_APPROVAL_MARKER = "AUTOTRADE_RECONVERGENCE_SCOPE_V1"
_SHA40 = re.compile(r"^[0-9a-f]{40}$")


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


def _require_repo_relative_path(value: str, *, name: str) -> str:
    if (
        type(value) is not str
        or not value
        or any(ord(character) in {0, 10, 13, 92} for character in value)
        or value.startswith("/")
        or value.endswith("/")
        or "//" in value
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise ValueError(f"{name} is malformed")
    return value


def parse_trusted_scope_approval(
    body: object,
    *,
    expected_head_sha: str,
) -> tuple[str, ...] | None:
    """Parse one external OWNER-issued exact-head trust-root approval.

    Unmarked comments and records for another well-formed head are unrelated.
    A record targeting the current head is authority-bearing input and therefore
    fails closed when malformed or when it repeats paths.
    """

    if type(body) is not str:
        return None
    lines = body.splitlines()
    if not lines or lines[0] != TRUSTED_SCOPE_APPROVAL_MARKER:
        return None
    if type(expected_head_sha) is not str or not _SHA40.fullmatch(expected_head_sha):
        raise ValueError("expected approval head must be a lowercase 40-hex SHA")
    if len(lines) < 2 or not lines[1].startswith("head: "):
        raise ValueError("trusted scope approval requires one exact head line")
    approved_head = lines[1].removeprefix("head: ")
    if not _SHA40.fullmatch(approved_head):
        raise ValueError("trusted scope approval head must be a lowercase 40-hex SHA")
    if approved_head != expected_head_sha:
        return None
    if len(lines) < 3:
        raise ValueError("trusted scope approval must contain at least one exact path")

    paths: list[str] = []
    for line in lines[2:]:
        if not line.startswith("path: "):
            raise ValueError("trusted scope approval permits only path lines after head")
        paths.append(
            _require_repo_relative_path(
                line.removeprefix("path: "),
                name="approved scope path",
            )
        )
    if len(set(paths)) != len(paths):
        raise ValueError("trusted scope approval must not repeat paths")
    return tuple(paths)


def parse_name_status(lines: Iterable[str]) -> tuple[Change, ...]:
    changes: list[Change] = []
    for raw in lines:
        line = raw.rstrip("\n")
        if not line:
            continue
        parts = line.split("\t")
        status = parts[0]
        kind = status[:1]
        if kind not in SUPPORTED_CHANGE_KINDS:
            raise ValueError(f"Unsupported Git name-status record: {line!r}")
        if not status:
            raise ValueError(f"Malformed Git name-status record: {line!r}")
        if kind in {"R", "C"}:
            score = status[1:]
            if (
                len(score) != 3
                or not score.isdigit()
                or int(score) > 100
            ):
                raise ValueError(f"Malformed rename/copy status: {line!r}")
        elif len(status) != 1:
            raise ValueError(f"Malformed Git name-status record: {line!r}")
        if kind in {"R", "C"}:
            if len(parts) != 3:
                raise ValueError(f"Malformed rename/copy record: {line!r}")
            previous_path = _require_repo_relative_path(
                parts[1],
                name="previous path",
            )
            path = _require_repo_relative_path(parts[2], name="path")
            changes.append(Change(status=status, previous_path=previous_path, path=path))
        else:
            if len(parts) != 2:
                raise ValueError(f"Malformed name-status record: {line!r}")
            path = _require_repo_relative_path(parts[1], name="path")
            changes.append(Change(status=status, path=path))
    return tuple(changes)


def _normalized_exact_paths(paths: Sequence[str] | None) -> frozenset[str]:
    if paths is None:
        return frozenset()
    normalized: set[str] = set()
    for value in paths:
        if (
            type(value) is not str
            or not value
            or any(ord(character) < 32 or ord(character) in {92, 127} for character in value)
            or value.startswith("/")
            or value.endswith("/")
            or "//" in value
            or "*" in value
            or "?" in value
        ):
            raise ValueError("exact protected path authorization is malformed")
        parts = value.split("/")
        if any(part in {"", ".", ".."} for part in parts):
            raise ValueError("exact protected path authorization is malformed")
        normalized.add(value)
    return frozenset(normalized)


def assess_reconvergence(
    *,
    base_paths: Sequence[str],
    changes: Sequence[Change],
    max_deletions: int = 50,
    max_deleted_fraction: float = 0.35,
    protected_sentinels: frozenset[str] = PROTECTED_SENTINELS,
    base_is_ancestor: bool = True,
    allowed_scopes: Sequence[str] | None = None,
    authorized_protected_sentinel_paths: Sequence[str] | None = None,
) -> IntegrityAssessment:
    if max_deletions < 1:
        raise ValueError("max_deletions must be positive")
    if not (0 < max_deleted_fraction <= 1):
        raise ValueError("max_deleted_fraction must be in (0, 1]")
    for change in changes:
        if type(change) is not Change:
            raise TypeError("changes must contain exact Change values")
        if type(change.status) is not str or not change.status:
            raise ValueError("Git change status is malformed")
        kind = change.status[:1]
        if kind not in SUPPORTED_CHANGE_KINDS:
            raise ValueError("unsupported Git change status")
        if kind in {"R", "C"}:
            score = change.status[1:]
            if (
                len(score) != 3
                or not score.isdigit()
                or int(score) > 100
            ):
                raise ValueError("rename/copy Git change status is malformed")
            if change.previous_path is None:
                raise ValueError("rename/copy change requires previous path")
            _require_repo_relative_path(change.previous_path, name="previous path")
        else:
            if len(change.status) != 1:
                raise ValueError("Git change status is malformed")
            if change.previous_path is not None:
                raise ValueError("non-rename/copy change must not have previous path")
        _require_repo_relative_path(change.path, name="changed path")

    authorized_protected_paths = _normalized_exact_paths(
        authorized_protected_sentinel_paths
    )
    if not authorized_protected_paths.issubset(PROTECTED_MUTATION_ROOTS):
        raise ValueError(
            "trust-root authorization must name only canonical executable trust roots"
        )
    if not authorized_protected_paths.issubset(protected_sentinels):
        raise ValueError(
            "trust-root authorization must name active protected sentinels"
        )

    normalized_base = tuple(
        dict.fromkeys(
            _require_repo_relative_path(path, name="base tree path")
            for path in base_paths
        )
    )
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
            and (
                change.previous_path in protected_sentinels
                or change.path in protected_sentinels
            )
            and change.path != change.previous_path
        ):
            protected_damage.add(
                f"{change.previous_path} -> {change.path} (rename)"
            )
        if kind == "T" and change.path in protected_sentinels:
            protected_damage.add(f"{change.path} (type change)")
        if (
            kind == "M"
            and change.path in PROTECTED_MUTATION_ROOTS
            and change.path in protected_sentinels
        ):
            if change.path not in authorized_protected_paths:
                protected_damage.add(f"{change.path} (modification)")
        if kind in {"A", "C"} and change.path in protected_sentinels:
            protected_damage.add(f"{change.path} (addition/copy)")
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
    authorized_protected_sentinel_paths: Sequence[str] | None = None,
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
        authorized_protected_sentinel_paths=authorized_protected_sentinel_paths,
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
            "Trusted externally resolved repository-relative mutation scope. "
            "Repeat to permit multiple scopes; never derive this authority from "
            "PR-authored metadata. When omitted, scope enforcement is disabled."
        ),
    )
    parser.add_argument(
        "--trusted-root-approval",
        action="append",
        default=None,
        help=(
            "Externally approved exact executable trust-root path. This channel is "
            "separate from ordinary mutation scope and must be resolved by trusted "
            "base code from an exact-head OWNER approval record."
        ),
    )
    parser.add_argument(
        "--cwd",
        default=None,
        help="Explicit Git repository/worktree for the public assessment entrypoint.",
    )
    args = parser.parse_args(argv)
    allowed_scopes = args.allowed_scope
    authorized_protected_sentinel_paths = tuple(
        dict.fromkeys(args.trusted_root_approval or ())
    )

    assessment = assess_git_revisions(
        args.base,
        args.head,
        max_deletions=args.max_deletions,
        max_deleted_fraction=args.max_deleted_fraction,
        allowed_scopes=allowed_scopes,
        authorized_protected_sentinel_paths=authorized_protected_sentinel_paths,
        cwd=args.cwd,
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
