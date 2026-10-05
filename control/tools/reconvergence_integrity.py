"""Fail-closed guard against malformed reconvergence commits.

The guard detects stale/diverged reconvergence, protected-control damage and
repository-tree destruction. When canonical mutation scopes are supplied, it also
binds every changed path to those scopes. A candidate must descend from the exact
base revision supplied by the pull-request event and, when a target branch is
supplied, that event base must still equal the live remote branch tip. Protected
canonical sentinels cannot be deleted, renamed away, type-changed, added, copied
over, or ordinarily modified unless the exact sentinel path has separate trusted
authorization. A PR that deletes both a material absolute number and a material
fraction of the base tree is blocked.

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
    base_matches_remote_tip: bool
    base_path_count: int
    deletion_count: int
    deletion_fraction: float
    protected_deletions: tuple[str, ...]
    protected_violations: tuple[str, ...]
    scope_violations: tuple[str, ...]
    reasons: tuple[str, ...]


def _validated_status(status: str) -> str:
    if type(status) is not str or not status:
        raise ValueError("Git name-status code must be a non-empty string")
    kind = status[:1]
    if kind in {"R", "C"}:
        score = status[1:]
        if not score.isdigit() or not (0 <= int(score) <= 100):
            raise ValueError(f"Malformed rename/copy status: {status!r}")
        return kind
    if status not in {"A", "D", "M", "T"}:
        raise ValueError(f"Unsupported Git name-status code: {status!r}")
    return kind


def _validated_repo_path(path: str) -> str:
    if type(path) is not str or not path:
        raise ValueError("changed path must be a non-empty string")
    if "\x00" in path or "\n" in path or "\r" in path or "\t" in path:
        raise ValueError(f"changed path contains a control separator: {path!r}")
    if "\\" in path or path.startswith("/") or path.endswith("/"):
        raise ValueError(f"changed path is not canonical repository-relative form: {path!r}")
    parts = path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"changed path contains a non-canonical segment: {path!r}")
    return path


def parse_name_status(lines: Iterable[str]) -> tuple[Change, ...]:
    changes: list[Change] = []
    for raw in lines:
        if type(raw) is not str:
            raise TypeError("Git name-status lines must be strings")
        line = raw.rstrip("\n")
        if not line:
            continue
        parts = line.split("\t")
        status = parts[0]
        kind = _validated_status(status)
        if kind in {"R", "C"}:
            if len(parts) != 3:
                raise ValueError(f"Malformed rename/copy record: {line!r}")
            previous_path = _validated_repo_path(parts[1])
            path = _validated_repo_path(parts[2])
            changes.append(Change(status=status, previous_path=previous_path, path=path))
        else:
            if len(parts) != 2:
                raise ValueError(f"Malformed name-status record: {line!r}")
            changes.append(Change(status=status, path=_validated_repo_path(parts[1])))
    return tuple(changes)


def parse_name_status_z(raw: bytes) -> tuple[Change, ...]:
    """Parse NUL-delimited Git name-status output without pathname quoting ambiguity."""
    if type(raw) is not bytes:
        raise TypeError("NUL-delimited Git name-status output must be bytes")
    if not raw:
        return ()
    fields = raw.split(b"\x00")
    if fields[-1] != b"":
        raise ValueError("NUL-delimited Git name-status output is truncated")
    fields = fields[:-1]
    changes: list[Change] = []
    index = 0
    while index < len(fields):
        try:
            status = fields[index].decode("ascii", "strict")
        except UnicodeDecodeError as exc:
            raise ValueError("Git name-status code is not ASCII") from exc
        index += 1
        kind = _validated_status(status)
        path_count = 2 if kind in {"R", "C"} else 1
        if index + path_count > len(fields):
            raise ValueError("NUL-delimited Git name-status record is truncated")
        try:
            decoded = [fields[index + offset].decode("utf-8", "strict") for offset in range(path_count)]
        except UnicodeDecodeError as exc:
            raise ValueError("changed path is not valid UTF-8") from exc
        index += path_count
        if kind in {"R", "C"}:
            changes.append(Change(status=status, previous_path=_validated_repo_path(decoded[0]), path=_validated_repo_path(decoded[1])))
        else:
            changes.append(Change(status=status, path=_validated_repo_path(decoded[0])))
    return tuple(changes)


def _normalized_protected_authorizations(
    values: Sequence[str] | None,
    *,
    protected_sentinels: frozenset[str],
) -> frozenset[str]:
    if values is None:
        return frozenset()
    authorized: set[str] = set()
    for value in values:
        path = _validated_repo_path(value)
        if path not in protected_sentinels:
            raise ValueError(
                "protected-sentinel authorization must name one exact protected path: "
                f"{path!r}"
            )
        authorized.add(path)
    return frozenset(authorized)


def assess_reconvergence(
    *,
    base_paths: Sequence[str],
    changes: Sequence[Change],
    max_deletions: int = 50,
    max_deleted_fraction: float = 0.35,
    protected_sentinels: frozenset[str] = PROTECTED_SENTINELS,
    base_is_ancestor: bool = True,
    base_matches_remote_tip: bool = True,
    allowed_scopes: Sequence[str] | None = None,
    allowed_protected_sentinels: Sequence[str] | None = None,
) -> IntegrityAssessment:
    if max_deletions < 1:
        raise ValueError("max_deletions must be positive")
    if not (0 < max_deleted_fraction <= 1):
        raise ValueError("max_deleted_fraction must be in (0, 1]")

    if type(base_matches_remote_tip) is not bool:
        raise TypeError("base_matches_remote_tip must be boolean")

    normalized_base = tuple(dict.fromkeys(base_paths))
    base_count = len(normalized_base)
    if base_count == 0:
        raise ValueError("base tree must contain at least one tracked path")

    deleted = tuple(sorted({change.path for change in changes if change.status == "D"}))
    protected = tuple(sorted(set(deleted).intersection(protected_sentinels)))
    fraction = len(deleted) / base_count

    authorized_protected = _normalized_protected_authorizations(
        allowed_protected_sentinels,
        protected_sentinels=protected_sentinels,
    )
    protected_damage: set[str] = set(protected)
    for change in changes:
        kind = _validated_status(change.status)
        if kind in {"M", "A"} and change.path in protected_sentinels:
            if change.path not in authorized_protected:
                action = "modified" if kind == "M" else "added"
                protected_damage.add(f"{change.path} ({action})")
        if kind == "C" and change.path in protected_sentinels:
            if change.path not in authorized_protected:
                protected_damage.add(f"{change.path} (copy destination)")
        if (
            kind == "R"
            and (
                change.previous_path in protected_sentinels
                or change.path in protected_sentinels
            )
            and change.path != change.previous_path
        ):
            protected_damage.add(f"{change.previous_path} -> {change.path} (rename)")
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
    if not base_matches_remote_tip:
        reasons.append("event base revision is not current remote target tip")
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
        base_matches_remote_tip=base_matches_remote_tip,
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


def _git_name_status(
    base: str,
    head: str,
    *,
    cwd: str | Path | None = None,
) -> tuple[Change, ...]:
    completed = subprocess.run(
        ["git", "diff", "--name-status", "-z", "--find-renames", base, head],
        cwd=cwd,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return parse_name_status_z(completed.stdout)


def _git_remote_branch_tip(
    remote: str,
    branch: str,
    *,
    cwd: str | Path | None = None,
) -> str:
    if type(remote) is not str or not remote:
        raise ValueError("remote must be a non-empty string")
    if type(branch) is not str or not branch:
        raise ValueError("base_ref must be a non-empty string")
    ref = f"refs/heads/{branch}"
    checked = subprocess.run(
        ["git", "check-ref-format", ref],
        cwd=cwd,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if checked.returncode != 0:
        raise ValueError(f"base_ref is not a canonical branch ref: {branch!r}")
    completed = subprocess.run(
        ["git", "ls-remote", "--heads", remote, ref],
        cwd=cwd,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    lines = completed.stdout.splitlines()
    if len(lines) != 1:
        raise RuntimeError(f"expected exactly one remote target ref for {ref!r}, got {len(lines)}")
    fields = lines[0].split("\t")
    if len(fields) != 2 or fields[1] != ref or not fields[0]:
        raise RuntimeError("malformed git ls-remote target-branch response")
    return fields[0]


def _git_resolve_commit(
    revision: str,
    *,
    cwd: str | Path | None = None,
) -> str:
    lines = _git_lines("rev-parse", "--verify", f"{revision}^{{commit}}", cwd=cwd)
    if len(lines) != 1 or not lines[0]:
        raise RuntimeError(f"could not resolve one exact commit for {revision!r}")
    return lines[0]


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
    allowed_protected_sentinels: Sequence[str] | None = None,
    base_ref: str | None = None,
    remote: str = "origin",
    cwd: str | Path | None = None,
) -> IntegrityAssessment:
    """Assess revisions inside one explicit Git repository/worktree.

    cwd defaults to the current process directory for the CLI workflow. Tests
    and library callers can bind revision identity to another repository. Every
    Git subprocess uses the same repository boundary.
    """

    base_is_ancestor = _git_is_ancestor(base, head, cwd=cwd)
    base_matches_remote_tip = True
    if base_ref is not None:
        base_matches_remote_tip = (
            _git_remote_branch_tip(remote, base_ref, cwd=cwd)
            == _git_resolve_commit(base, cwd=cwd)
        )
    base_paths = _git_lines("ls-tree", "-r", "--name-only", base, cwd=cwd)
    changes = _git_name_status(base, head, cwd=cwd)
    return assess_reconvergence(
        base_paths=base_paths,
        changes=changes,
        max_deletions=max_deletions,
        max_deleted_fraction=max_deleted_fraction,
        base_is_ancestor=base_is_ancestor,
        base_matches_remote_tip=base_matches_remote_tip,
        allowed_scopes=allowed_scopes,
        allowed_protected_sentinels=allowed_protected_sentinels,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fail closed on malformed reconvergence tree destruction."
    )
    parser.add_argument("--base", required=True, help="Exact base commit SHA/ref")
    parser.add_argument("--head", required=True, help="Exact head commit SHA/ref")
    parser.add_argument(
        "--base-ref",
        default=None,
        help=(
            "Trusted target branch name. When supplied, its live remote tip must "
            "still equal --base before candidate evaluation."
        ),
    )
    parser.add_argument(
        "--remote",
        default="origin",
        help="Git remote used only to resolve the trusted target branch tip.",
    )
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
        "--allow-protected-sentinel",
        action="append",
        default=None,
        help=(
            "Trusted externally issued exact protected path authorization. "
            "Repeat for additional exact sentinels; directory scopes and "
            "candidate-authored metadata are never accepted here."
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
        allowed_protected_sentinels=args.allow_protected_sentinel,
        base_ref=args.base_ref,
        remote=args.remote,
    )
    print(
        "Reconvergence tree guard: "
        f"base_is_ancestor={str(assessment.base_is_ancestor).lower()} "
        f"base_matches_remote_tip={str(assessment.base_matches_remote_tip).lower()} "
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
