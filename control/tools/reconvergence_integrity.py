"""Fail-closed guard against malformed reconvergence commits.

The guard detects stale/diverged reconvergence, protected-control damage and
repository-tree destruction. When canonical mutation scopes are supplied, it also
binds every changed path to those scopes. Destructive base-tree accounting covers
not only direct deletions but also rename-away and Git object-type replacement. The resulting candidate tree must also remain case-insensitively unique for Windows-safe checkout. A candidate must descend from the exact
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

PROTECTED_SENTINELS = frozenset(
    {
        ".github/workflows/baseline.yml",
        ".github/workflows/contracts.yml",
        ".github/workflows/control-plane.yml",
        ".github/workflows/dotnet-foundation.yml",
        ".github/workflows/provider-free-product.yml",
        ".github/workflows/futures-qualification.yml",
        ".github/workflows/lean-adoption.yml",
        ".github/workflows/reconvergence-integrity.yml",
        ".github/workflows/recovery-qualification.yml",
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

# Only the two executable reconvergence trust roots require independently
# supplied exact-path authority for ordinary content replacement. Other
# PROTECTED_SENTINELS remain protected against destructive removal/rename/type
# changes without freezing routine controlled metadata evolution.
PROTECTED_MUTATION_ROOTS = frozenset(
    {
        ".github/workflows/reconvergence-integrity.yml",
        "control/tools/reconvergence_integrity.py",
    }
)
_SCOPE_HEAD_RE = re.compile(r"^[0-9a-f]{40}$")
_WINDOWS_FORBIDDEN_COMPONENT_CHARS = frozenset('<>:"|?*')
_WINDOWS_RESERVED_BASENAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
)


def _is_protected_authority(path: str, protected_sentinels: frozenset[str]) -> bool:
    return path in protected_sentinels



@dataclass(frozen=True)
class Change:
    status: str
    path: str
    previous_path: str | None = None


_NAME_STATUS_RE = re.compile(
    r"^(?:A|M|D|T|R(?:100|[0-9]{1,2})|C(?:100|[0-9]{1,2}))$"
)


def _validated_repo_path(value: str, *, name: str) -> str:
    if type(value) is not str or not value:
        raise ValueError(f"{name} must be a non-empty exact string")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"{name} contains a forbidden control character")
    if value.startswith("/") or "\\" in value:
        raise ValueError(f"{name} must be a canonical repository-relative path")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"{name} must be a canonical repository-relative path")
    return value


def _validate_windows_checkout_path(path: str) -> None:
    for component in path.split("/"):
        if component.endswith((" ", ".")):
            raise ValueError(
                f"candidate path is not Windows-checkout-safe: {path}"
            )
        if any(character in _WINDOWS_FORBIDDEN_COMPONENT_CHARS for character in component):
            raise ValueError(
                f"candidate path is not Windows-checkout-safe: {path}"
            )
        basename = component.split(".", 1)[0].upper()
        if basename in _WINDOWS_RESERVED_BASENAMES:
            raise ValueError(
                f"candidate path is not Windows-checkout-safe: {path}"
            )


def _validated_change(change: Change) -> Change:
    if type(change) is not Change:
        raise TypeError("changes must contain exact Change values")
    if type(change.status) is not str or _NAME_STATUS_RE.fullmatch(change.status) is None:
        raise ValueError(f"Unsupported Git name-status code: {change.status!r}")
    kind = change.status[:1]
    path = _validated_repo_path(change.path, name="changed path")
    if kind in {"R", "C"}:
        if change.previous_path is None:
            raise ValueError(f"{kind} change is missing its source path")
        previous = _validated_repo_path(
            change.previous_path,
            name="changed source path",
        )
    else:
        if change.previous_path is not None:
            raise ValueError(
                f"{change.status} change must not carry a previous_path"
            )
        previous = None
    return Change(status=change.status, path=path, previous_path=previous)


@dataclass(frozen=True)
class IntegrityAssessment:
    allowed: bool
    base_is_ancestor: bool
    base_path_count: int
    deletion_count: int
    deletion_fraction: float
    destructive_change_count: int
    destructive_change_fraction: float
    protected_deletions: tuple[str, ...]
    protected_violations: tuple[str, ...]
    scope_violations: tuple[str, ...]
    reasons: tuple[str, ...]


def parse_name_status(lines: Iterable[str]) -> tuple[Change, ...]:
    """Parse line-oriented Git name-status data and validate every record."""

    changes: list[Change] = []
    for raw in lines:
        if type(raw) is not str:
            raise TypeError("name-status lines must be exact strings")
        line = raw.rstrip("\n")
        if not line:
            continue
        parts = line.split("\t")
        status = parts[0]
        kind = status[:1]
        if kind in {"R", "C"}:
            if len(parts) != 3:
                raise ValueError(f"Malformed rename/copy record: {line!r}")
            change = Change(
                status=status,
                previous_path=parts[1],
                path=parts[2],
            )
        else:
            if len(parts) != 2:
                raise ValueError(f"Malformed name-status record: {line!r}")
            change = Change(status=status, path=parts[1])
        changes.append(_validated_change(change))
    return tuple(changes)


def parse_name_status_z(raw: bytes) -> tuple[Change, ...]:
    """Parse NUL-delimited git diff --name-status -z output.

    NUL framing avoids Git's quoted-path ambiguity. Repository paths must still
    be canonical UTF-8 and pass the same validator used for synthetic/library
    Change inputs.
    """

    if type(raw) is not bytes:
        raise TypeError("NUL-delimited name-status input must be exact bytes")
    if not raw:
        return ()
    if not raw.endswith(b"\x00"):
        raise ValueError("Malformed NUL-delimited name-status stream")
    tokens = raw[:-1].split(b"\x00")
    changes: list[Change] = []
    index = 0
    while index < len(tokens):
        try:
            status = tokens[index].decode("ascii")
        except UnicodeDecodeError as error:
            raise ValueError("Git name-status code must be ASCII") from error
        kind = status[:1]
        field_count = 3 if kind in {"R", "C"} else 2
        if index + field_count > len(tokens):
            raise ValueError("Malformed NUL-delimited name-status record")
        try:
            if kind in {"R", "C"}:
                previous = tokens[index + 1].decode("utf-8")
                path = tokens[index + 2].decode("utf-8")
                change = Change(
                    status=status,
                    previous_path=previous,
                    path=path,
                )
            else:
                path = tokens[index + 1].decode("utf-8")
                change = Change(status=status, path=path)
        except UnicodeDecodeError as error:
            raise ValueError("Git paths must be canonical UTF-8") from error
        changes.append(_validated_change(change))
        index += field_count
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
            _validated_repo_path(path, name="base path")
            for path in base_paths
        )
    )
    base_count = len(normalized_base)
    if base_count == 0:
        raise ValueError("base tree must contain at least one tracked path")
    base_set = frozenset(normalized_base)

    validated_changes = tuple(_validated_change(change) for change in changes)
    mutating_sources: dict[str, str] = {}
    for change in validated_changes:
        kind = change.status[:1]
        source = change.previous_path if kind in {"R", "C"} else change.path
        if kind in {"M", "D", "T", "R", "C"} and source not in base_set:
            raise ValueError(
                f"{change.status} change source is absent from the base tree: "
                f"{source}"
            )
        if kind in {"M", "D", "T", "R"}:
            if source is None:
                raise ValueError("mutating change source path identity is missing")
            previous_status = mutating_sources.get(source)
            if previous_status is not None:
                raise ValueError(
                    "base path has multiple mutating Git changes: "
                    f"{source} ({previous_status}, {change.status})"
                )
            mutating_sources[source] = change.status
        if kind == "A" and change.path in base_set:
            raise ValueError(
                f"added path already exists in the base tree: {change.path}"
            )

    removed_base_paths = {
        change.previous_path if change.status[:1] == "R" else change.path
        for change in validated_changes
        if change.status[:1] in {"D", "R"}
    }
    incoming_destinations: set[str] = set()
    for change in validated_changes:
        kind = change.status[:1]
        if kind not in {"A", "R", "C"}:
            continue
        if change.path in incoming_destinations:
            raise ValueError(
                "multiple Git changes target the same candidate path: "
                + change.path
            )
        incoming_destinations.add(change.path)
        if change.path in base_set and change.path not in removed_base_paths:
            raise ValueError(
                "candidate destination already exists in base tree without "
                f"removal: {change.path}"
            )

    candidate_paths = set(base_set)
    for change in validated_changes:
        kind = change.status[:1]
        if kind == "D":
            candidate_paths.discard(change.path)
        elif kind == "R":
            if change.previous_path is None:
                raise ValueError("rename source path identity is missing")
            candidate_paths.discard(change.previous_path)
    for change in validated_changes:
        if change.status[:1] in {"A", "R", "C"}:
            candidate_paths.add(change.path)

    casefold_groups: dict[str, list[str]] = {}
    for path in sorted(candidate_paths):
        _validate_windows_checkout_path(path)
        casefold_groups.setdefault(path.casefold(), []).append(path)
    casefold_collisions = tuple(
        tuple(paths)
        for paths in casefold_groups.values()
        if len(paths) > 1
    )
    if casefold_collisions:
        rendered = "; ".join(
            " <> ".join(paths)
            for paths in casefold_collisions
        )
        raise ValueError(
            "candidate tree contains case-insensitive path collision(s): "
            + rendered
        )

    normalized_scopes: tuple[str, ...] | None = None
    if allowed_scopes is not None:
        normalized_scopes = _normalized_scopes(allowed_scopes)
    exact_trust_root_authorizations = frozenset(normalized_scopes or ())

    deleted = tuple(
        sorted(
            {
                change.path
                for change in validated_changes
                if change.status == "D"
            }
        )
    )
    fraction = len(deleted) / base_count
    destructive_base_paths = tuple(
        sorted(
            {
                (
                    change.previous_path
                    if change.status[:1] == "R"
                    else change.path
                )
                for change in validated_changes
                if change.status[:1] in {"D", "R", "T"}
            }
        )
    )
    destructive_fraction = len(destructive_base_paths) / base_count

    protected_base_paths = frozenset(
        path
        for path in base_set
        if _is_protected_authority(path, protected_sentinels)
    )
    protected = tuple(sorted(set(deleted).intersection(protected_base_paths)))

    protected_damage: set[str] = set(protected)
    for change in validated_changes:
        kind = change.status[:1]
        if (
            kind == "R"
            and change.previous_path in protected_base_paths
            and change.path != change.previous_path
        ):
            protected_damage.add(
                f"{change.previous_path} -> {change.path} (rename)"
            )
        if kind == "T" and change.path in protected_base_paths:
            protected_damage.add(f"{change.path} (type change)")
        if (
            change.status == "M"
            and change.path in PROTECTED_MUTATION_ROOTS
            and change.path not in exact_trust_root_authorizations
        ):
            protected_damage.add(
                f"{change.path} (content change without exact-path authorization)"
            )
        if (
            kind in {"A", "R", "C"}
            and change.path in PROTECTED_MUTATION_ROOTS
            and change.path not in exact_trust_root_authorizations
        ):
            protected_damage.add(
                f"{change.path} (trust-root destination without exact-path authorization)"
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
    if (
        len(destructive_base_paths) >= max_deletions
        and destructive_fraction >= max_deleted_fraction
    ):
        if len(destructive_base_paths) == len(deleted):
            reasons.append(
                "mass base-tree deletion: "
                f"{len(deleted)}/{base_count} paths ({fraction:.1%})"
            )
        else:
            reasons.append(
                "mass destructive base-tree change: "
                f"{len(destructive_base_paths)}/{base_count} paths "
                f"({destructive_fraction:.1%}); direct deletions={len(deleted)}"
            )

    return IntegrityAssessment(
        allowed=not reasons,
        base_is_ancestor=base_is_ancestor,
        base_path_count=base_count,
        deletion_count=len(deleted),
        deletion_fraction=fraction,
        destructive_change_count=len(destructive_base_paths),
        destructive_change_fraction=destructive_fraction,
        protected_deletions=protected,
        protected_violations=protected_violations,
        scope_violations=scope_violations,
        reasons=tuple(reasons),
    )


def _git_paths_z(
    *args: str,
    cwd: str | Path | None = None,
) -> tuple[str, ...]:
    completed = subprocess.run(
        ["git", *args, "-z"],
        cwd=cwd,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    raw = completed.stdout
    if not raw:
        return ()
    if not raw.endswith(b"\x00"):
        raise ValueError("Malformed NUL-delimited Git path stream")
    result: list[str] = []
    for token in raw[:-1].split(b"\x00"):
        try:
            path = token.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ValueError("Git paths must be canonical UTF-8") from error
        result.append(_validated_repo_path(path, name="Git path"))
    return tuple(result)


def _git_name_status(
    base: str,
    head: str,
    *,
    cwd: str | Path | None = None,
) -> tuple[Change, ...]:
    completed = subprocess.run(
        [
            "git",
            "diff",
            "--name-status",
            "--find-renames",
            "-z",
            base,
            head,
        ],
        cwd=cwd,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return parse_name_status_z(completed.stdout)


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


def _git_commit_sha(
    ref: str,
    *,
    cwd: str | Path | None = None,
) -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "--verify", f"{ref}^{{commit}}"],
        cwd=cwd,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    sha = completed.stdout.strip()
    if _SCOPE_HEAD_RE.fullmatch(sha) is None:
        raise ValueError("Git commit identity must be canonical lowercase SHA-1")
    return sha


def assess_git_revisions(
    base: str,
    head: str,
    *,
    max_deletions: int = 50,
    max_deleted_fraction: float = 0.35,
    allowed_scopes: Sequence[str] | None = None,
    allowed_scope_head: str | None = None,
    cwd: str | Path | None = None,
) -> IntegrityAssessment:
    """Assess revisions inside one explicit Git repository/worktree.

    cwd defaults to the current process directory for the CLI workflow. Tests
    and library callers can bind revision identity to another repository. Every
    Git subprocess uses the same repository boundary.
    """

    resolved_head = _git_commit_sha(head, cwd=cwd)
    if allowed_scopes is None:
        if allowed_scope_head is not None:
            raise ValueError("allowed_scope_head requires allowed_scopes")
    else:
        if (
            type(allowed_scope_head) is not str
            or _SCOPE_HEAD_RE.fullmatch(allowed_scope_head) is None
            or allowed_scope_head != resolved_head
        ):
            raise ValueError(
                "protected mutation scope is not bound to the exact candidate head"
            )

    base_is_ancestor = _git_is_ancestor(base, resolved_head, cwd=cwd)
    base_paths = _git_paths_z("ls-tree", "-r", "--name-only", base, cwd=cwd)
    changes = _git_name_status(base, resolved_head, cwd=cwd)
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
        "--repo",
        default=None,
        help=(
            "Optional Git repository/worktree to assess while executing the "
            "trusted guard module from a separate repository root."
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
    parser.add_argument(
        "--allowed-scope-head",
        default=None,
        help=(
            "Exact lowercase candidate commit SHA that the external scope "
            "authorization was issued for. Required whenever --allowed-scope "
            "is supplied; stale-head authorization has zero authority."
        ),
    )
    args = parser.parse_args(argv)
    allowed_scopes = args.allowed_scope

    try:
        assessment = assess_git_revisions(
            args.base,
            args.head,
            max_deletions=args.max_deletions,
            max_deleted_fraction=args.max_deleted_fraction,
            allowed_scopes=allowed_scopes,
            allowed_scope_head=args.allowed_scope_head,
            cwd=args.repo,
        )
    except (ValueError, TypeError, subprocess.CalledProcessError) as error:
        print(f"BLOCKED: {error}")
        return 2
    print(
        "Reconvergence tree guard: "
        f"base_is_ancestor={str(assessment.base_is_ancestor).lower()} "
        f"base_paths={assessment.base_path_count} "
        f"deletions={assessment.deletion_count} "
        f"deleted_fraction={assessment.deletion_fraction:.3f} "
        f"destructive_changes={assessment.destructive_change_count} "
        f"destructive_fraction={assessment.destructive_change_fraction:.3f} "
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
