"""Fail-closed guard against malformed reconvergence commits.

The guard detects stale/diverged reconvergence, protected-control damage and
repository-tree destruction. When canonical mutation scopes are supplied, it also
binds every changed path to those scopes. A candidate must descend from the exact
base revision supplied by the pull-request event. Every mutation of a protected
canonical sentinel requires separately supplied exact-path authorization;
directory mutation scopes never authorize protected trust-root edits. A PR that
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

TRUSTED_SCOPE_APPROVAL_MARKER = "AUTOTRADE_RECONVERGENCE_SCOPE_V1"
_SHA40 = re.compile(r"^[0-9a-f]{40}$")
_SCORED_CHANGE_STATUS = re.compile(r"^[RC](?:100|0[0-9]{2})$")

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
        "control/__init__.py",
        "control/tools/__init__.py",
        "control/tools/reconvergence_integrity.py",
        "control/tools/registry_state.py",
        "docs/product/PRODUCT_SPEC_CANONICAL.txt",
        "docs/engineering/00_AUTOTRADE_MASTER_ENGINEERING_SPEC.md",
        "Directory.Build.props",
        "Directory.Build.targets",
        "global.json",
        "requirements-dev.txt",
        "contracts/fixtures/common-scalars.corpus.json",
        "tests/Contracts.DotNet/Contracts.DotNet.csproj",
        "tests/Contracts.DotNet/Program.cs",
        "tests/Desktop.Client/Desktop.Client.csproj",
        "tests/Desktop.Client/Program.cs",
        "tools/baseline.py",
        "tools/build_provenance_manifest.py",
        "tools/check_nvda_qualification.py",
        "tools/verify.py",
        "tools/write_ci_evidence.py",
    }
)

BOOTSTRAP_TRUST_ROOTS = frozenset(
    {
        "control/__init__.py",
        "control/tools/__init__.py",
        "control/tools/reconvergence_integrity.py",
        "control/tools/registry_state.py",
    }
)

WORKFLOW_AUTHORITY_ROOTS = frozenset(
    path
    for path in PROTECTED_SENTINELS
    if path.startswith(".github/workflows/")
)

INTEGRATION_HARNESS_ROOTS = frozenset(
    {
        "Directory.Build.props",
        "Directory.Build.targets",
        "global.json",
        "requirements-dev.txt",
        "contracts/fixtures/common-scalars.corpus.json",
        "tests/Contracts.DotNet/Contracts.DotNet.csproj",
        "tests/Contracts.DotNet/Program.cs",
        "tests/Desktop.Client/Desktop.Client.csproj",
        "tests/Desktop.Client/Program.cs",
        "tools/baseline.py",
        "tools/build_provenance_manifest.py",
        "tools/check_nvda_qualification.py",
        "tools/verify.py",
        "tools/write_ci_evidence.py",
    }
)

MUTATION_AUTHORITY_ROOTS = (
    BOOTSTRAP_TRUST_ROOTS
    | WORKFLOW_AUTHORITY_ROOTS
    | INTEGRATION_HARNESS_ROOTS
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


_SIMPLE_CHANGE_STATUSES = frozenset({"A", "D", "M", "T"})


def _is_workflow_authority_path(path: str) -> bool:
    return path.startswith(".github/workflows/") and path.endswith((".yml", ".yaml"))


def _validate_repository_path(value: str, *, field: str) -> str:
    if type(value) is not str:
        raise TypeError(f"{field} must be an exact string")
    if not value:
        raise ValueError(f"{field} must not be empty")
    if any(character in value for character in ("\x00", "\n", "\r", "\t")):
        raise ValueError(f"{field} contains a forbidden control character")
    if "\\" in value or ":" in value:
        raise ValueError(f"{field} must use canonical repository POSIX separators")
    if value.startswith("/") or value.endswith("/"):
        raise ValueError(f"{field} must be a repository-relative file path")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"{field} is not a canonical repository-relative path")
    return value


def _validate_change(change: Change) -> Change:
    if type(change) is not Change:
        raise TypeError("changes must contain exact Change values")
    if type(change.status) is not str or not change.status:
        raise ValueError("change status must be non-empty exact text")

    status = change.status
    kind = status[:1]
    if status in _SIMPLE_CHANGE_STATUSES:
        if change.previous_path is not None:
            raise ValueError(f"{status} change must not carry previous_path")
    elif _SCORED_CHANGE_STATUS.fullmatch(status):
        if change.previous_path is None:
            raise ValueError(f"{status} change requires previous_path")
        _validate_repository_path(change.previous_path, field="previous_path")
    else:
        raise ValueError(f"unsupported or unmerged change status: {status!r}")

    _validate_repository_path(change.path, field="path")
    return change


def _normalized_protected_authorizations(
    values: Sequence[str] | None,
    *,
    protected_sentinels: frozenset[str],
) -> frozenset[str]:
    if values is None:
        return frozenset()
    normalized: set[str] = set()
    for raw in values:
        path = _validate_repository_path(raw, field="authorized protected path")
        if (
            path not in MUTATION_AUTHORITY_ROOTS
            and not _is_workflow_authority_path(path)
        ):
            raise ValueError(
                "protected-path authorization must name one exact executable "
                f"trust root or workflow authority: {path!r}"
            )
        normalized.add(path)
    return frozenset(normalized)


def parse_trusted_scope_approval(
    body: object,
    *,
    expected_head_sha: str,
) -> tuple[str, ...] | None:
    """Parse one OWNER-issued exact-head protected-path approval record.

    Unrelated comments and otherwise valid records for an older head are ignored.
    A marked record for the current exact head is authority-shaped input and must
    be fully canonical; malformed records fail closed.
    """

    if type(body) is not str:
        return None
    lines = body.splitlines()
    if not lines or lines[0] != TRUSTED_SCOPE_APPROVAL_MARKER:
        return None
    if not _SHA40.fullmatch(expected_head_sha):
        raise ValueError("expected approval head must be lowercase 40-hex Git SHA")
    if len(lines) < 2 or not lines[1].startswith("head: "):
        raise ValueError("trusted scope approval requires one exact head line")
    approved_head = lines[1].removeprefix("head: ")
    if not _SHA40.fullmatch(approved_head):
        raise ValueError("trusted scope approval head must be lowercase 40-hex Git SHA")
    if approved_head != expected_head_sha:
        return None
    if len(lines) < 3:
        raise ValueError("trusted scope approval requires at least one exact path")

    paths: list[str] = []
    for line in lines[2:]:
        if not line.startswith("path: "):
            raise ValueError("trusted scope approval permits only exact path lines")
        path = _validate_repository_path(
            line.removeprefix("path: "),
            field="approved protected path",
        )
        paths.append(path)
    if len(set(paths)) != len(paths):
        raise ValueError("trusted scope approval must not repeat paths")
    return tuple(paths)


def parse_name_status(lines: Iterable[str]) -> tuple[Change, ...]:
    changes: list[Change] = []
    for raw in lines:
        if type(raw) is not str:
            raise TypeError("name-status records must be exact strings")
        if "\x00" in raw:
            raise ValueError("name-status record contains NUL")
        line = raw.rstrip("\n")
        if not line:
            continue
        parts = line.split("\t")
        status = parts[0]
        kind = status[:1]
        if kind in {"R", "C"}:
            if len(parts) != 3:
                raise ValueError(f"Malformed rename/copy record: {line!r}")
            change = Change(status=status, previous_path=parts[1], path=parts[2])
        else:
            if len(parts) != 2:
                raise ValueError(f"Malformed name-status record: {line!r}")
            change = Change(status=status, path=parts[1])
        changes.append(_validate_change(change))
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
    authorized_protected_paths: Sequence[str] | None = None,
) -> IntegrityAssessment:
    if max_deletions < 1:
        raise ValueError("max_deletions must be positive")
    if not (0 < max_deleted_fraction <= 1):
        raise ValueError("max_deleted_fraction must be in (0, 1]")

    normalized_base = tuple(
        dict.fromkeys(
            _validate_repository_path(path, field="base path") for path in base_paths
        )
    )
    validated_changes = tuple(_validate_change(change) for change in changes)
    base_count = len(normalized_base)
    if base_count == 0:
        raise ValueError("base tree must contain at least one tracked path")

    deleted = tuple(
        sorted({change.path for change in validated_changes if change.status == "D"})
    )
    protected = tuple(sorted(set(deleted).intersection(protected_sentinels)))
    fraction = len(deleted) / base_count
    protected_authorizations = _normalized_protected_authorizations(
        authorized_protected_paths,
        protected_sentinels=protected_sentinels,
    )

    protected_damage: set[str] = set()
    for change in validated_changes:
        kind = change.status[:1]
        if kind == "R":
            if (
                change.previous_path in protected_sentinels
                and change.previous_path not in protected_authorizations
            ):
                protected_damage.add(
                    f"{change.previous_path} -> {change.path} (rename)"
                )
            if (
                change.path in protected_sentinels
                and change.path != change.previous_path
                and change.path not in protected_authorizations
            ):
                protected_damage.add(f"{change.path} (rename destination)")
            continue
        if kind == "C":
            if (
                change.path in protected_sentinels
                and change.path not in protected_authorizations
            ):
                protected_damage.add(f"{change.path} (copy destination)")
            continue
        if (
            change.path not in protected_sentinels
            or change.path in protected_authorizations
        ):
            continue
        if kind == "D":
            protected_damage.add(change.path)
        elif kind == "T":
            protected_damage.add(f"{change.path} (type change)")
        elif kind == "M":
            protected_damage.add(f"{change.path} (modified)")
        elif kind == "A":
            protected_damage.add(f"{change.path} (added)")
    protected_violations = tuple(sorted(protected_damage))

    normalized_scopes: tuple[str, ...] | None = None
    if allowed_scopes is not None:
        normalized_scopes = _normalized_scopes(allowed_scopes)

    scope_damage: set[str] = set()
    if normalized_scopes is not None:
        for change in validated_changes:
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
    authorized_protected_paths: Sequence[str] | None = None,
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
        authorized_protected_paths=authorized_protected_paths,
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
        "--allow-protected-path",
        action="append",
        default=None,
        help=(
            "Trusted independently supplied exact protected-sentinel path. "
            "Directory scopes never authorize protected trust-root mutation. "
            "Repeat only for each exact protected path intentionally changed."
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
        authorized_protected_paths=args.allow_protected_path,
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
