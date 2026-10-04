"""Fail-closed guard against malformed reconvergence commits.

The guard detects stale/diverged reconvergence, protected-control damage and
repository-tree destruction. When canonical mutation scopes are supplied, it also
binds every changed path to those scopes. A candidate must descend from the exact
base revision supplied by the pull-request event. Protected canonical sentinels cannot be deleted, renamed away or changed to
another Git object type. Ordinary content modification of a protected trust root
is also blocked unless the caller supplies that exact repository-relative path
through the independently trusted mutation scope. Directory-wide scope never
authorizes a trust-root edit. A PR that deletes both a material absolute number
and a material fraction of the base tree is blocked.

This directly protects against commits accidentally built from a stale or partial
tree and against small unrelated changes hidden inside otherwise valid work.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
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


_SUPPORTED_SIMPLE_STATUSES = frozenset({"A", "D", "M", "T"})
_SUPPORTED_SCORED_STATUS_KINDS = frozenset({"R", "C"})


def _canonical_repo_path(value: object, *, name: str) -> str:
    if type(value) is not str or not value:
        raise ValueError(f"{name} must be non-empty exact text")
    if value != value.strip():
        raise ValueError(f"{name} must be canonical repository-relative text")
    if (
        value.startswith("/")
        or "\\" in value
        or ":" in value
        or any(char in value for char in "*?[]\x00\n\r\t")
    ):
        raise ValueError(f"{name} must be a literal repository-relative path")
    if any(part in {"", ".", ".."} for part in value.split("/")):
        raise ValueError(f"{name} must not contain empty/dot path segments")
    return value


def _status_kind(value: object) -> str:
    if type(value) is not str or not value:
        raise ValueError("Git name-status must be non-empty exact text")
    if value in _SUPPORTED_SIMPLE_STATUSES:
        return value
    kind = value[:1]
    if kind in _SUPPORTED_SCORED_STATUS_KINDS:
        score = value[1:]
        if (
            not score
            or not score.isascii()
            or not score.isdigit()
            or len(score) > 3
            or int(score) > 100
        ):
            raise ValueError(f"Malformed scored Git name-status: {value!r}")
        return kind
    raise ValueError(f"Unsupported Git name-status: {value!r}")


def _canonical_change(value: object) -> Change:
    if type(value) is not Change:
        raise TypeError("changes must contain exact Change values")
    kind = _status_kind(value.status)
    path = _canonical_repo_path(value.path, name="changed path")
    previous = value.previous_path
    if kind in _SUPPORTED_SCORED_STATUS_KINDS:
        if previous is None:
            raise ValueError(f"{kind} status requires a source path")
        previous = _canonical_repo_path(previous, name="changed source path")
    elif previous is not None:
        raise ValueError(f"{kind} status must not carry a source path")
    return Change(status=value.status, path=path, previous_path=previous)


def parse_name_status(lines: Iterable[str]) -> tuple[Change, ...]:
    changes: list[Change] = []
    for raw in lines:
        if type(raw) is not str:
            raise TypeError("Git name-status records must be exact text")
        if "\x00" in raw:
            raise ValueError("Git name-status record must not contain NUL")
        line = raw[:-1] if raw.endswith("\n") else raw
        if "\n" in line or "\r" in line:
            raise ValueError("Git name-status record contains embedded newline")
        if not line:
            continue
        parts = line.split("\t")
        status = parts[0]
        kind = _status_kind(status)
        if kind in _SUPPORTED_SCORED_STATUS_KINDS:
            if len(parts) != 3:
                raise ValueError(f"Malformed rename/copy record: {line!r}")
            change = Change(
                status=status,
                previous_path=_canonical_repo_path(
                    parts[1],
                    name="changed source path",
                ),
                path=_canonical_repo_path(parts[2], name="changed path"),
            )
        else:
            if len(parts) != 2:
                raise ValueError(f"Malformed name-status record: {line!r}")
            change = Change(
                status=status,
                path=_canonical_repo_path(parts[1], name="changed path"),
            )
        changes.append(_canonical_change(change))
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

    normalized_base = tuple(
        dict.fromkeys(
            _canonical_repo_path(path, name="base tree path")
            for path in base_paths
        )
    )
    normalized_changes = tuple(_canonical_change(change) for change in changes)
    base_count = len(normalized_base)
    if base_count == 0:
        raise ValueError("base tree must contain at least one tracked path")

    normalized_scopes: tuple[str, ...] | None = None
    if allowed_scopes is not None:
        normalized_scopes = _normalized_scopes(allowed_scopes)
    exact_scope_authority = frozenset(
        scope.casefold() for scope in (normalized_scopes or ())
    )

    protected_by_casefold: dict[str, str] = {}
    for sentinel in protected_sentinels:
        canonical = _canonical_repo_path(
            sentinel,
            name="protected sentinel",
        )
        folded = canonical.casefold()
        existing = protected_by_casefold.get(folded)
        if existing is not None and existing != canonical:
            raise ValueError("protected sentinels collide case-insensitively")
        protected_by_casefold[folded] = canonical

    deleted = tuple(
        sorted(
            {
                change.path
                for change in normalized_changes
                if _status_kind(change.status) == "D"
            }
        )
    )
    protected = tuple(
        sorted(
            protected_by_casefold[path.casefold()]
            for path in deleted
            if path.casefold() in protected_by_casefold
        )
    )
    fraction = len(deleted) / base_count

    protected_damage: set[str] = set(protected)
    for change in normalized_changes:
        kind = _status_kind(change.status)
        path_key = change.path.casefold()
        destination_sentinel = protected_by_casefold.get(path_key)
        source_sentinel = (
            None
            if change.previous_path is None
            else protected_by_casefold.get(change.previous_path.casefold())
        )
        if (
            kind == "R"
            and source_sentinel is not None
            and change.path != change.previous_path
        ):
            protected_damage.add(
                f"{source_sentinel} -> {change.path} (rename)"
            )
        if (
            kind == "R"
            and destination_sentinel is not None
            and source_sentinel != destination_sentinel
            and destination_sentinel.casefold() not in exact_scope_authority
        ):
            protected_damage.add(
                f"{change.previous_path} -> {destination_sentinel} "
                "(rename into trust root without exact-path authorization)"
            )
        if kind == "T" and destination_sentinel is not None:
            protected_damage.add(f"{destination_sentinel} (type change)")
        if (
            kind in {"A", "M", "C"}
            and destination_sentinel is not None
            and destination_sentinel.casefold() not in exact_scope_authority
        ):
            protected_damage.add(
                f"{destination_sentinel} "
                "(content change without exact-path authorization)"
            )
    protected_violations = tuple(sorted(protected_damage))

    scope_damage: set[str] = set()
    if normalized_scopes is not None:
        for change in normalized_changes:
            kind = _status_kind(change.status)
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
            "Trusted externally resolved repository-relative mutation scope. "
            "Repeat to permit multiple scopes; never derive this authority from "
            "PR-authored metadata. When omitted, scope enforcement is disabled."
        ),
    )
    args = parser.parse_args(argv)
    allowed_scopes = args.allowed_scope

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
