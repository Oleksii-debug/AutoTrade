"""Fail-closed guard against malformed reconvergence commits.

The guard detects stale/diverged reconvergence, protected-control damage and
repository-tree destruction. When canonical mutation scopes are supplied, it also
binds every changed path to those scopes. A candidate must descend from the exact
base revision supplied by the pull-request event. Protected canonical sentinels
cannot be deleted, renamed away or changed to another Git object type. Checked-in
workflow authorities, their integration harness roots, and the executable
reconvergence guard reject ordinary content modification unless an independently
supplied trusted scope names that exact path. Untrusted creation of an additional
workflow authority is rejected as well. A PR that deletes or renames away both a
material absolute number and a material fraction of base-tree paths is blocked;
copies do not count as source disappearance.

This directly protects against commits accidentally built from a stale or partial
tree, candidate-controlled rewrites/spoofs of integration check authorities, and
small unrelated changes hidden inside otherwise valid work.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
import re
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
        "tools/baseline.py",
        "tools/build_provenance_manifest.py",
        "tools/check_nvda_qualification.py",
        "tools/verify.py",
        "tools/write_ci_evidence.py",
    }
)

SELF_PROTECTING_TRUST_ROOTS = frozenset(
    {
        ".github/workflows/reconvergence-integrity.yml",
        "control/tools/reconvergence_integrity.py",
    }
)

WORKFLOW_AUTHORITY_ROOTS = frozenset(
    path
    for path in PROTECTED_SENTINELS
    if path.startswith(".github/workflows/")
)

INTEGRATION_HARNESS_ROOTS = frozenset(
    {
        "requirements-dev.txt",
        "tools/baseline.py",
        "tools/build_provenance_manifest.py",
        "tools/check_nvda_qualification.py",
        "tools/verify.py",
        "tools/write_ci_evidence.py",
    }
)

_SIMPLE_STATUS = frozenset({"A", "D", "M", "T"})
_SCORED_STATUS = re.compile(r"^[RC](?:100|0[0-9]{2})$")


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


def _validate_changed_path(value: object, *, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be non-empty text")
    if (
        value.startswith("/")
        or "\\" in value
        or ":" in value
        or any(ch in value for ch in ("\x00", "\n", "\r"))
    ):
        raise ValueError(f"{name} must be a canonical repository-relative path")
    if any(part in {"", ".", ".."} for part in value.split("/")):
        raise ValueError(f"{name} must not contain empty/dot path segments")
    return value


def _validated_change(change: Change) -> Change:
    if not isinstance(change, Change):
        raise TypeError("changes must contain Change values")
    if not isinstance(change.status, str):
        raise ValueError("Git name-status must be text")

    if change.status in _SIMPLE_STATUS:
        if change.previous_path is not None:
            raise ValueError(
                f"{change.status} change must not carry a previous path"
            )
    elif _SCORED_STATUS.fullmatch(change.status):
        if change.previous_path is None:
            raise ValueError(
                f"{change.status} change must carry a previous path"
            )
        _validate_changed_path(change.previous_path, name="previous changed path")
    else:
        raise ValueError(f"Unsupported or malformed Git name-status: {change.status!r}")

    _validate_changed_path(change.path, name="changed path")
    return change


def parse_name_status(lines: Iterable[str]) -> tuple[Change, ...]:
    changes: list[Change] = []
    for raw in lines:
        line = raw.rstrip("\n")
        if not line:
            continue
        parts = line.split("\t")
        status = parts[0]
        if _SCORED_STATUS.fullmatch(status):
            if len(parts) != 3:
                raise ValueError(f"Malformed rename/copy record: {line!r}")
            change = Change(status=status, previous_path=parts[1], path=parts[2])
        else:
            if len(parts) != 2:
                raise ValueError(f"Malformed name-status record: {line!r}")
            change = Change(status=status, path=parts[1])
        changes.append(_validated_change(change))
    return tuple(changes)


def _is_workflow_authority_path(path: str) -> bool:
    return path.startswith(".github/workflows/") and path.endswith((".yml", ".yaml"))


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
    base_path_set = frozenset(normalized_base)

    validated_changes = tuple(_validated_change(change) for change in changes)

    normalized_scopes: tuple[str, ...] | None = None
    if allowed_scopes is not None:
        normalized_scopes = _normalized_scopes(allowed_scopes)

    def exactly_authorized(path: str) -> bool:
        return normalized_scopes is not None and path in normalized_scopes

    disappeared_paths: set[str] = set()
    direct_deletions: set[str] = set()
    for change in validated_changes:
        kind = change.status[:1]
        if kind == "D" and change.path in base_path_set:
            direct_deletions.add(change.path)
            disappeared_paths.add(change.path)
        elif (
            kind == "R"
            and change.previous_path is not None
            and change.previous_path in base_path_set
            and change.previous_path != change.path
        ):
            # Git rename classification must not let a large base-tree path
            # disappearance evade the same destruction fence as explicit D.
            disappeared_paths.add(change.previous_path)
        # C* intentionally does not remove its source path.

    disappeared = tuple(sorted(disappeared_paths))
    protected = tuple(sorted(direct_deletions.intersection(protected_sentinels)))
    fraction = len(disappeared) / base_count

    protected_damage: set[str] = set(protected)
    for change in validated_changes:
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
        if (
            kind == "M"
            and (
                change.path in SELF_PROTECTING_TRUST_ROOTS
                or change.path in WORKFLOW_AUTHORITY_ROOTS
                or change.path in INTEGRATION_HARNESS_ROOTS
            )
            and not exactly_authorized(change.path)
        ):
            protected_damage.add(
                f"{change.path} (unauthorized trust-root modification)"
            )
        if (
            kind in {"A", "R", "C"}
            and _is_workflow_authority_path(change.path)
            and change.path not in base_path_set
            and not exactly_authorized(change.path)
        ):
            protected_damage.add(
                f"{change.path} (unauthorized workflow-authority creation)"
            )
    protected_violations = tuple(sorted(protected_damage))

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
    if len(disappeared) >= max_deletions and fraction >= max_deleted_fraction:
        reasons.append(
            "mass base-tree deletion/rename-away: "
            f"{len(disappeared)}/{base_count} paths ({fraction:.1%})"
        )

    return IntegrityAssessment(
        allowed=not reasons,
        base_is_ancestor=base_is_ancestor,
        base_path_count=base_count,
        deletion_count=len(disappeared),
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