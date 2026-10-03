"""Stage reviewed source-controlled AutoTrade components into a product composition.

The staging authority consumes bytes from one exact Git object, not from the
mutable checkout.  It preflights the complete descriptor/composition graph and
all currently-existing destination authority before publishing regular files.
The composition manifest is published last.
"""

from __future__ import annotations

import argparse
from contextlib import ExitStack, contextmanager
import ctypes
from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import stat
import subprocess
import sys
import tempfile
from typing import Iterable
from uuid import uuid4

from autotrade_foundation.windows_namespace import (
    publish_windows_regular_bytes,
    publish_windows_regular_bytes_retained,
    retain_windows_directory_namespace,
    retain_windows_relative_directory_namespace,
    retain_windows_regular_file,
    serialize_windows_directory_publication,
)


ROOT = Path(__file__).resolve().parents[1]
_GIT_OBJECT_ID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_FILE_ATTRIBUTE_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_LINUX_OPENAT2_SYSCALL = 437
_LINUX_OPENAT2_MACHINES = frozenset({"x86_64", "amd64", "aarch64", "arm64"})
_RESOLVE_NO_XDEV = 0x01
_RESOLVE_NO_MAGICLINKS = 0x02
_RESOLVE_NO_SYMLINKS = 0x04
_RESOLVE_BENEATH = 0x08


class _LinuxOpenHow(ctypes.Structure):
    _fields_ = [
        ("flags", ctypes.c_uint64),
        ("mode", ctypes.c_uint64),
        ("resolve", ctypes.c_uint64),
    ]


@dataclass(frozen=True)
class _SourceControlledComponent:
    component_id: str
    kind: str
    path: str


_REQUIRED = (
    _SourceControlledComponent(
        "autotrade-foundation-package",
        "runtime-foundation",
        "autotrade_foundation/__init__.py",
    ),
    _SourceControlledComponent(
        "autotrade-foundation-local-filesystem",
        "runtime-foundation",
        "autotrade_foundation/local_filesystem.py",
    ),
    _SourceControlledComponent(
        "autotrade-foundation-windows-namespace",
        "runtime-foundation",
        "autotrade_foundation/windows_namespace.py",
    ),
    _SourceControlledComponent(
        "autotrade-exact-numeric-package",
        "runtime-numeric",
        "autotrade_numeric/__init__.py",
    ),
    _SourceControlledComponent(
        "autotrade-exact-numeric-common-scalars",
        "runtime-numeric",
        "autotrade_numeric/_generated_common_scalars.py",
    ),
    _SourceControlledComponent(
        "autotrade-exact-numeric-decimal-limits",
        "runtime-numeric",
        "autotrade_numeric/_generated_decimal_limits.py",
    ),
    _SourceControlledComponent(
        "autotrade-exact-numeric-exact-decimal",
        "runtime-numeric",
        "autotrade_numeric/exact_decimal.py",
    ),
)


class FoundationStagingError(ValueError):
    pass


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _is_reparse(info: os.stat_result) -> bool:
    attributes = getattr(info, "st_file_attributes", 0)
    return bool(attributes & _FILE_ATTRIBUTE_REPARSE_POINT)


def _lstat(path: Path, *, label: str) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError as error:
        raise FoundationStagingError(f"cannot inspect {label}: {path}") from error
    if stat.S_ISLNK(info.st_mode) or _is_reparse(info):
        raise FoundationStagingError(f"{label} must not be a symlink/reparse point: {path}")
    return info


def _require_exact_directory(path: Path, *, label: str) -> os.stat_result:
    info = _lstat(path, label=label)
    if not stat.S_ISDIR(info.st_mode):
        raise FoundationStagingError(f"{label} must be an existing directory: {path}")
    return info


def _require_exact_regular_file(
    path: Path,
    *,
    label: str,
    single_link: bool = False,
) -> os.stat_result:
    info = _lstat(path, label=label)
    if not stat.S_ISREG(info.st_mode):
        raise FoundationStagingError(f"{label} must be a regular file: {path}")
    if single_link and getattr(info, "st_nlink", 1) != 1:
        raise FoundationStagingError(f"{label} must not be hardlinked: {path}")
    return info


def _required_posix_open_flag(name: str) -> int:
    """Require the no-follow primitives used by retained POSIX authority."""

    value = getattr(os, name, 0)
    if type(value) is not int or value == 0:
        raise FoundationStagingError(
            f"{name} is required for POSIX retained namespace authority"
        )
    return value


def _require_posix_directory_no_alias(path: Path, *, label: str) -> os.stat_result:
    """Admit one lexical POSIX directory root without resolving ancestor aliases."""

    absolute = Path(os.path.abspath(os.fspath(path)))
    if not absolute.is_absolute():
        raise FoundationStagingError(
            f"{label} must resolve to an absolute lexical path"
        )
    current = Path(absolute.anchor)
    _require_exact_directory(current, label=f"{label} filesystem root")
    final_info: os.stat_result | None = None
    for part in absolute.parts[1:]:
        current = current / part
        info = _lstat(current, label=f"{label} path component")
        if not stat.S_ISDIR(info.st_mode):
            raise FoundationStagingError(
                f"{label} ancestor must be a directory: {current}"
            )
        final_info = info
    if final_info is None:
        final_info = _require_exact_directory(absolute, label=label)
    return final_info


def _require_posix_source_root_no_alias(source_root: Path) -> os.stat_result:
    """Admit the lexical POSIX source root without resolving ancestor aliases away."""

    return _require_posix_directory_no_alias(source_root, label="source_root")

def _require_posix_source_root_generation(
    source_root: Path,
    baseline: os.stat_result,
) -> None:
    current = _require_posix_source_root_no_alias(source_root)
    if current.st_dev != baseline.st_dev or current.st_ino != baseline.st_ino:
        raise FoundationStagingError(
            "source_root generation changed before staging mutation"
        )


def _path_parts(relative: str) -> tuple[str, ...]:
    pure = PurePosixPath(relative)
    if (
        pure.is_absolute()
        or not pure.parts
        or any(part in {"", ".", ".."} for part in pure.parts)
    ):
        raise FoundationStagingError(f"component path is not canonical: {relative!r}")
    canonical = pure.as_posix()
    if canonical != relative:
        raise FoundationStagingError(f"component path is not canonical: {relative!r}")
    return pure.parts


def _preflight_existing_chain(
    root: Path,
    relative: str,
    *,
    root_label: str,
    leaf_must_be_file: bool | None,
    leaf_single_link: bool = False,
) -> Path:
    """Reject every currently-existing alias/non-directory in a root-relative path."""

    _require_exact_directory(root, label=root_label)
    current = root
    parts = _path_parts(relative)
    for index, part in enumerate(parts):
        current = current / part
        is_leaf = index == len(parts) - 1
        try:
            exists = current.exists() or current.is_symlink()
        except OSError as error:
            raise FoundationStagingError(f"cannot inspect path component: {current}") from error
        if not exists:
            break
        info = _lstat(current, label="path component")
        if is_leaf and leaf_must_be_file is True:
            if not stat.S_ISREG(info.st_mode):
                raise FoundationStagingError(f"path leaf is not a regular file: {current}")
            if leaf_single_link and getattr(info, "st_nlink", 1) != 1:
                raise FoundationStagingError(f"path leaf must not be hardlinked: {current}")
        elif is_leaf and leaf_must_be_file is False:
            if not stat.S_ISDIR(info.st_mode):
                raise FoundationStagingError(f"path leaf is not a directory: {current}")
        elif not stat.S_ISDIR(info.st_mode):
            raise FoundationStagingError(f"path ancestor is not a directory: {current}")
    return root.joinpath(*parts)


def _trusted_git_candidate_paths() -> tuple[Path, ...]:
    """Return fail-closed OS-managed Git locations without consulting PATH."""

    if os.name == "nt":
        return (
            Path(r"C:\\Program Files\\Git\\cmd\\git.exe"),
            Path(r"C:\\Program Files\\Git\\bin\\git.exe"),
        )
    return (Path("/usr/bin/git"), Path("/bin/git"))


def _trusted_git_executable(*, source_root: Path) -> str:
    """Select Git outside the mutable checkout and caller-controlled PATH."""

    try:
        resolved_source_root = source_root.resolve(strict=True)
    except OSError as error:
        raise FoundationStagingError("Git source root is unavailable") from error
    for candidate in _trusted_git_candidate_paths():
        try:
            executable = candidate.resolve(strict=True)
        except OSError:
            continue
        if not executable.is_file():
            continue
        try:
            executable.relative_to(resolved_source_root)
        except ValueError:
            return os.fspath(executable)
        raise FoundationStagingError(
            "Git executable must not originate from the source checkout"
        )
    raise FoundationStagingError(
        "git executable is unavailable at an OS-managed location"
    )


def _trusted_git_environment() -> dict[str, str]:
    """Run exact-object Git reads without ambient process/Git authority."""

    environment = {
        key: value
        for key in ("SYSTEMROOT", "WINDIR", "COMSPEC")
        if (value := os.environ.get(key))
    }
    environment["GIT_CONFIG_NOSYSTEM"] = "1"
    environment["GIT_CONFIG_GLOBAL"] = os.devnull
    environment["GIT_NO_REPLACE_OBJECTS"] = "1"
    environment["LC_ALL"] = "C"
    environment["LANG"] = "C"
    return environment


def _git(*args: str, source_root: Path) -> bytes:
    executable = _trusted_git_executable(source_root=source_root)
    try:
        completed = subprocess.run(
            (executable, *args),
            cwd=source_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=10,
            env=_trusted_git_environment(),
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise FoundationStagingError("git executable is unavailable") from error
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise FoundationStagingError(f"exact Git source lookup failed: {detail or args[0]}")
    return completed.stdout


def _require_git_root(source_root: Path) -> os.stat_result:
    if os.name == "nt":
        baseline = _require_exact_directory(source_root, label="source_root")
    else:
        baseline = _require_posix_source_root_no_alias(source_root)
    top = _git("rev-parse", "--show-toplevel", source_root=source_root)
    try:
        top_path = Path(top.decode("utf-8", errors="strict").strip())
    except UnicodeDecodeError as error:
        raise FoundationStagingError("Git source root is not UTF-8") from error
    try:
        expected = source_root.resolve(strict=True)
        actual = top_path.resolve(strict=True)
    except OSError as error:
        raise FoundationStagingError("cannot resolve Git source root") from error
    if actual != expected:
        raise FoundationStagingError("source_root must be the exact Git worktree root")
    return baseline


def _git_source_bytes(relative: str, *, source_root: Path, source_sha: str) -> bytes:
    if _GIT_OBJECT_ID.fullmatch(source_sha) is None:
        raise FoundationStagingError("composition source_sha must be an exact lowercase Git object id")
    _preflight_existing_chain(
        source_root,
        relative,
        root_label="source_root",
        leaf_must_be_file=True,
    )
    _git("cat-file", "-e", f"{source_sha}^{{commit}}", source_root=source_root)
    return _git(
        "cat-file",
        "blob",
        f"{source_sha}:{relative}",
        source_root=source_root,
    )


def _validate_descriptors(
    descriptors: Iterable[_SourceControlledComponent],
) -> tuple[_SourceControlledComponent, ...]:
    frozen = tuple(descriptors)
    if not frozen:
        raise FoundationStagingError("at least one source-controlled component is required")
    ids: set[str] = set()
    paths: set[str] = set()
    for descriptor in frozen:
        if type(descriptor) is not _SourceControlledComponent:
            raise FoundationStagingError("component descriptor must be canonical")
        if not descriptor.component_id or descriptor.component_id != descriptor.component_id.strip():
            raise FoundationStagingError("component_id must be canonical")
        if not descriptor.kind or descriptor.kind != descriptor.kind.strip():
            raise FoundationStagingError("component kind must be canonical")
        _path_parts(descriptor.path)
        if descriptor.component_id in ids or descriptor.path in paths:
            raise FoundationStagingError("source-controlled component identity is duplicated")
        for other in paths:
            if descriptor.path.startswith(other + "/") or other.startswith(descriptor.path + "/"):
                raise FoundationStagingError("source-controlled component paths overlap")
        ids.add(descriptor.component_id)
        paths.add(descriptor.path)
    return frozen


def _parse_composition(composition_bytes: bytes) -> dict[str, object]:
    try:
        text = composition_bytes.decode("utf-8", errors="strict")
        composition = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise FoundationStagingError("composition manifest is invalid") from error
    if type(composition) is not dict:
        raise FoundationStagingError("composition manifest must be an object")
    source_sha = composition.get("source_sha")
    if type(source_sha) is not str or _GIT_OBJECT_ID.fullmatch(source_sha) is None:
        raise FoundationStagingError("composition source_sha must be an exact lowercase Git object id")
    components = composition.get("components")
    if type(components) is not list:
        raise FoundationStagingError("composition components must be an array")
    return composition


def _composition_index(components: object) -> tuple[dict[str, dict[str, object]], set[str]]:
    if type(components) is not list:
        raise FoundationStagingError("composition components must be an array")
    existing_by_path: dict[str, dict[str, object]] = {}
    existing_ids: set[str] = set()
    for item in components:
        if type(item) is not dict:
            raise FoundationStagingError("composition component must be an object")
        path = item.get("path")
        component_id = item.get("component_id")
        if type(path) is not str or type(component_id) is not str:
            raise FoundationStagingError("composition component identity is invalid")
        _path_parts(path)
        if path in existing_by_path or component_id in existing_ids:
            raise FoundationStagingError("composition component identity is duplicated")
        existing_by_path[path] = item
        existing_ids.add(component_id)
    return existing_by_path, existing_ids


def _same_file_identity(path: Path, baseline: os.stat_result) -> bool:
    try:
        current = path.lstat()
    except OSError:
        return False
    return (
        not stat.S_ISLNK(current.st_mode)
        and not _is_reparse(current)
        and stat.S_ISREG(current.st_mode)
        and current.st_dev == baseline.st_dev
        and current.st_ino == baseline.st_ino
        and current.st_size == baseline.st_size
        and getattr(current, "st_mtime_ns", None) == getattr(baseline, "st_mtime_ns", None)
        and getattr(current, "st_nlink", 1) == 1
    )


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    flags = os.O_RDONLY | _required_posix_open_flag("O_DIRECTORY")
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@contextmanager
def _retained_windows_directory(path: Path, *, label: str):
    """Retain one Windows directory generation through the neutral foundation TCB."""

    if os.name != "nt":
        yield
        return
    try:
        with retain_windows_directory_namespace(path) as authority:
            _require_exact_directory(path, label=label)
            yield authority
    except (OSError, RuntimeError, ValueError) as error:
        raise FoundationStagingError(
            f"{label} cannot be retained as exact Windows namespace authority"
        ) from error


@contextmanager
def _composition_publish_transaction(parent: Path):
    """Serialize one composition update over a stable parent authority.

    POSIX uses the retained directory inode itself as the cooperating-publisher
    lock, so atomic replacement of composition.json cannot move the lock to a
    stale inode. Windows still requires the selected retained-handle namespace
    implementation; this helper deliberately does not pretend pathname checks
    close that native race.
    """

    baseline = (
        _require_exact_directory(parent, label="composition parent")
        if os.name == "nt"
        else _require_posix_directory_no_alias(parent, label="composition parent")
    )
    if os.name == "nt":
        # Keep the composition-parent generation pinned through the complete
        # preflight -> component publication -> manifest-last transaction.
        # Cross-process cooperating-publisher serialization remains a separate
        # blocker until this primitive is neutralized into autotrade_foundation.
        with _retained_windows_directory(
            parent,
            label="composition parent",
        ) as authority:
            with serialize_windows_directory_publication(authority):
                visible = _require_exact_directory(
                    parent,
                    label="composition parent",
                )
                if (
                    visible.st_dev != baseline.st_dev
                    or visible.st_ino != baseline.st_ino
                ):
                    raise FoundationStagingError(
                        "composition parent changed while acquiring publication lock"
                    )
                yield authority
        return

    try:
        import fcntl
    except ImportError as error:
        raise FoundationStagingError(
            "POSIX composition serialization is unavailable"
        ) from error

    flags = os.O_RDONLY | _required_posix_open_flag("O_DIRECTORY") | _required_posix_open_flag("O_NOFOLLOW")
    try:
        descriptor = os.open(parent, flags)
    except OSError as error:
        raise FoundationStagingError(
            "composition parent cannot be retained for publication"
        ) from error
    try:
        retained = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(retained.st_mode)
            or retained.st_dev != baseline.st_dev
            or retained.st_ino != baseline.st_ino
        ):
            raise FoundationStagingError(
                "composition parent changed before publication lock"
            )
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        visible = _require_posix_directory_no_alias(
            parent,
            label="composition parent",
        )
        if visible.st_dev != retained.st_dev or visible.st_ino != retained.st_ino:
            raise FoundationStagingError(
                "composition parent changed while acquiring publication lock"
            )
        yield descriptor
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _read_retained_descriptor(
    descriptor: int,
    *,
    expected: bytes,
    relative: str,
) -> None:
    """Verify exact component bytes through the retained immutable leaf handle."""

    os.lseek(descriptor, 0, os.SEEK_SET)
    observed = bytearray()
    while True:
        chunk = os.read(descriptor, 64 * 1024)
        if not chunk:
            break
        observed.extend(chunk)
    os.lseek(descriptor, 0, os.SEEK_SET)
    if bytes(observed) != expected:
        raise FoundationStagingError(
            f"staged component verification failed: {relative}"
        )


@contextmanager
def _retained_posix_directory(path: Path, *, label: str):
    """Retain one exact POSIX directory inode without following the leaf."""

    if os.name == "nt":
        yield None
        return
    no_follow = _required_posix_open_flag("O_NOFOLLOW")
    directory_flag = _required_posix_open_flag("O_DIRECTORY")
    if (
        not no_follow
        or not directory_flag
        or os.open not in getattr(os, "supports_dir_fd", set())
    ):
        raise FoundationStagingError(
            f"{label} requires POSIX descriptor-relative no-follow support"
        )
    baseline = _require_posix_directory_no_alias(path, label=label)
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | directory_flag | no_follow,
        )
    except OSError as error:
        raise FoundationStagingError(f"{label} cannot be retained") from error
    try:
        retained = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(retained.st_mode)
            or retained.st_dev != baseline.st_dev
            or retained.st_ino != baseline.st_ino
        ):
            raise FoundationStagingError(f"{label} changed before retention")
        visible = _require_posix_directory_no_alias(path, label=label)
        if visible.st_dev != retained.st_dev or visible.st_ino != retained.st_ino:
            raise FoundationStagingError(f"{label} changed during retention")
        yield descriptor
    finally:
        os.close(descriptor)


@contextmanager
def _retained_posix_relative_directory(
    root_descriptor: int,
    parts: tuple[str, ...],
    *,
    create: bool,
):
    """Open/create one descendant chain relative to a retained POSIX root."""

    no_follow = _required_posix_open_flag("O_NOFOLLOW")
    directory_flag = _required_posix_open_flag("O_DIRECTORY")
    supports = getattr(os, "supports_dir_fd", set())
    if (
        os.open not in supports
        or os.mkdir not in supports
        or not no_follow
        or not directory_flag
    ):
        raise FoundationStagingError(
            "POSIX retained directory publication support is unavailable"
        )
    current = os.dup(root_descriptor)
    try:
        for index, part in enumerate(parts):
            if not part or part in {".", ".."} or "/" in part or "\\" in part:
                raise FoundationStagingError(
                    "POSIX retained directory component is not canonical"
                )
            flags = (
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | directory_flag
                | no_follow
            )
            try:
                child = os.open(part, flags, dir_fd=current)
            except FileNotFoundError:
                if not create:
                    raise FoundationStagingError(
                        "POSIX retained directory component is missing"
                    ) from None
                if index != len(parts) - 1:
                    raise FoundationStagingError(
                        "POSIX missing nested staging parent requires "
                        "pre-existing root-authorized directories"
                    ) from None
                try:
                    os.mkdir(part, 0o700, dir_fd=current)
                except FileExistsError:
                    pass
                except OSError as error:
                    raise FoundationStagingError(
                        "POSIX retained directory component cannot be created"
                    ) from error
                try:
                    child = os.open(part, flags, dir_fd=current)
                except OSError as error:
                    raise FoundationStagingError(
                        "POSIX retained directory component cannot be opened"
                    ) from error
            except OSError as error:
                raise FoundationStagingError(
                    "POSIX retained directory component cannot be opened"
                ) from error
            opened = os.fstat(child)
            if not stat.S_ISDIR(opened.st_mode):
                os.close(child)
                raise FoundationStagingError(
                    "POSIX retained directory component is not a directory"
                )
            os.close(current)
            current = child
        yield current
    finally:
        os.close(current)


def _read_posix_regular_file(
    parent_descriptor: int,
    name: str,
    *,
    subject: str,
) -> tuple[bytes, os.stat_result]:
    no_follow = _required_posix_open_flag("O_NOFOLLOW")
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | no_follow,
            dir_fd=parent_descriptor,
        )
    except OSError as error:
        raise FoundationStagingError(f"{subject} cannot be opened safely") from error
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise FoundationStagingError(f"{subject} must be a regular file")
        if getattr(opened, "st_nlink", 1) != 1:
            raise FoundationStagingError(f"{subject} must not be hardlinked")
        chunks = bytearray()
        while True:
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                break
            chunks.extend(chunk)
        return bytes(chunks), opened
    finally:
        os.close(descriptor)


def _require_posix_relative_directory_matches(
    root_descriptor: int,
    parts: tuple[str, ...],
    retained_descriptor: int,
) -> None:
    with _retained_posix_relative_directory(
        root_descriptor,
        parts,
        create=False,
    ) as visible_descriptor:
        visible = os.fstat(visible_descriptor)
        retained = os.fstat(retained_descriptor)
        if visible.st_dev != retained.st_dev or visible.st_ino != retained.st_ino:
            raise FoundationStagingError(
                "POSIX staging descendant changed during retained publication"
            )


def _linux_openat2_beneath(
    root_descriptor: int,
    relative: str,
    *,
    flags: int,
    mode: int = 0,
    subject: str,
    allow_root_probe: bool = False,
) -> int:
    """Open one path from the retained root with a kernel-enforced beneath policy.

    Descriptor-relative traversal alone is insufficient for mutation: a retained
    descendant directory can be renamed outside the retained root while its fd
    remains valid. Linux openat2 binds the *complete* relative lookup back to the
    retained staging root and rejects symlink/magic-link traversal. Other POSIX
    kernels fail closed instead of silently falling back to stale descendant fds.
    """

    if sys.platform != "linux":
        raise FoundationStagingError(
            f"{subject} requires Linux openat2 beneath/no-symlink authority"
        )
    machine = platform.machine().lower()
    if machine not in _LINUX_OPENAT2_MACHINES:
        raise FoundationStagingError(
            f"{subject} has no reviewed openat2 syscall ABI for {machine or 'unknown'}"
        )
    if type(root_descriptor) is not int or root_descriptor < 0:
        raise FoundationStagingError(f"{subject} root descriptor is invalid")
    if type(allow_root_probe) is not bool:
        raise FoundationStagingError(f"{subject} root-probe flag is non-canonical")
    if type(relative) is not str or not relative or "\\" in relative:
        raise FoundationStagingError(f"{subject} relative path is non-canonical")
    if not (allow_root_probe and relative == "."):
        relative_path = PurePosixPath(relative)
        if (
            relative_path.is_absolute()
            or not relative_path.parts
            or any(part in {"", ".", ".."} for part in relative_path.parts)
        ):
            raise FoundationStagingError(f"{subject} relative path is non-canonical")
    if type(flags) is not int or type(mode) is not int:
        raise FoundationStagingError(f"{subject} open flags/mode are non-canonical")

    libc = ctypes.CDLL(None, use_errno=True)
    try:
        syscall = libc.syscall
    except AttributeError as error:
        raise FoundationStagingError(
            f"{subject} requires a libc syscall entry point for openat2"
        ) from error
    syscall.restype = ctypes.c_long
    how = _LinuxOpenHow(
        flags=flags,
        mode=mode,
        resolve=(
            _RESOLVE_BENEATH
            | _RESOLVE_NO_XDEV
            | _RESOLVE_NO_SYMLINKS
            | _RESOLVE_NO_MAGICLINKS
        ),
    )
    ctypes.set_errno(0)
    descriptor = syscall(
        ctypes.c_long(_LINUX_OPENAT2_SYSCALL),
        ctypes.c_int(root_descriptor),
        ctypes.c_char_p(os.fsencode(relative)),
        ctypes.byref(how),
        ctypes.c_size_t(ctypes.sizeof(how)),
    )
    if descriptor < 0:
        error_number = ctypes.get_errno()
        error = OSError(error_number, os.strerror(error_number))
        raise FoundationStagingError(
            f"{subject} openat2 beneath/no-symlink authorization failed: {relative}"
        ) from error
    return int(descriptor)


def _require_linux_openat2_beneath_authority(root_descriptor: int) -> None:
    """Prove required kernel containment semantics before any staging mutation."""

    descriptor = _linux_openat2_beneath(
        root_descriptor,
        ".",
        flags=(
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | _required_posix_open_flag("O_DIRECTORY")
        ),
        subject="POSIX staging-root capability probe",
        allow_root_probe=True,
    )
    try:
        retained = os.fstat(root_descriptor)
        observed = os.fstat(descriptor)
        if (
            retained.st_dev != observed.st_dev
            or retained.st_ino != observed.st_ino
            or not stat.S_ISDIR(observed.st_mode)
        ):
            raise FoundationStagingError(
                "POSIX staging-root capability probe changed root identity"
            )
    finally:
        os.close(descriptor)


def _read_posix_regular_beneath(
    root_descriptor: int,
    *,
    relative: str,
    subject: str,
) -> tuple[bytes, os.stat_result]:
    descriptor = _linux_openat2_beneath(
        root_descriptor,
        relative,
        flags=os.O_RDONLY | getattr(os, "O_CLOEXEC", 0),
        subject=subject,
    )
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise FoundationStagingError(f"{subject} must be a regular file")
        if getattr(opened, "st_nlink", 1) != 1:
            raise FoundationStagingError(f"{subject} must not be hardlinked")
        chunks = bytearray()
        while True:
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                break
            chunks.extend(chunk)
        return bytes(chunks), opened
    finally:
        os.close(descriptor)


def _sync_posix_parent_beneath(root_descriptor: int, *, relative: str) -> None:
    parent = PurePosixPath(relative).parent
    if str(parent) in {"", "."}:
        descriptor = os.dup(root_descriptor)
    else:
        descriptor = _linux_openat2_beneath(
            root_descriptor,
            parent.as_posix(),
            flags=(
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | _required_posix_open_flag("O_DIRECTORY")
            ),
            subject="published component parent",
        )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_posix_new_regular_beneath(
    root_descriptor: int,
    *,
    data: bytes,
    relative: str,
) -> None:
    """Publish one new leaf through the retained staging root, never a child fd."""

    descriptor = _linux_openat2_beneath(
        root_descriptor,
        relative,
        flags=(
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
        ),
        mode=0o600,
        subject="POSIX staged component publication",
    )
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or getattr(opened, "st_nlink", 1) != 1:
            raise FoundationStagingError(
                f"published component is not one single-link regular file: {relative}"
            )
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short POSIX beneath-authorized component write")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        # A short/failed write deliberately leaves a non-authoritative component
        # leaf rather than attempting pathname cleanup through a possibly raced
        # namespace. composition.json remains unchanged and retry fails closed.
        os.close(descriptor)

    observed, _opened = _read_posix_regular_beneath(
        root_descriptor,
        relative=relative,
        subject="published component",
    )
    if observed != data:
        raise FoundationStagingError(
            f"staged component verification failed: {relative}"
        )
    _sync_posix_parent_beneath(root_descriptor, relative=relative)


def _replace_posix_regular(
    parent_descriptor: int,
    *,
    target_name: str,
    data: bytes,
    subject: str,
) -> None:
    """Replace one authority leaf relative to the already-retained parent."""

    no_follow = _required_posix_open_flag("O_NOFOLLOW")
    temporary_name = f".{target_name}.{uuid4().hex}.tmp"
    try:
        descriptor = os.open(
            temporary_name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | no_follow,
            0o600,
            dir_fd=parent_descriptor,
        )
    except OSError as error:
        raise FoundationStagingError(
            f"{subject} temporary publication failed"
        ) from error
    temporary_exists = True
    try:
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short POSIX retained manifest write")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        try:
            os.replace(
                temporary_name,
                target_name,
                src_dir_fd=parent_descriptor,
                dst_dir_fd=parent_descriptor,
            )
        except (OSError, TypeError) as error:
            raise FoundationStagingError(
                f"{subject} relative replacement failed"
            ) from error
        temporary_exists = False
        observed, _opened = _read_posix_regular_file(
            parent_descriptor,
            target_name,
            subject=subject,
        )
        if observed != data:
            raise FoundationStagingError(f"{subject} verification failed")
        os.fsync(parent_descriptor)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_exists:
            try:
                os.unlink(temporary_name, dir_fd=parent_descriptor)
            except OSError:
                pass


def _atomic_publish_regular(
    path: Path,
    data: bytes,
    *,
    root: Path,
    relative: str,
    windows_parent_authority=None,
    posix_root_authority: int | None = None,
    publication_authority: ExitStack | None = None,
) -> None:
    _preflight_existing_chain(
        root,
        relative,
        root_label="staging",
        leaf_must_be_file=True,
        leaf_single_link=True,
    )
    if os.name == "nt":
        if windows_parent_authority is None or publication_authority is None:
            raise FoundationStagingError(
                "Windows component publication requires retained parent authority"
            )
        try:
            descriptor = publication_authority.enter_context(
                publish_windows_regular_bytes_retained(
                    windows_parent_authority,
                    target_name=path.name,
                    data=data,
                    replace=False,
                )
            )
            _read_retained_descriptor(
                descriptor,
                expected=data,
                relative=relative,
            )
        except FoundationStagingError:
            raise
        except (OSError, RuntimeError, ValueError) as error:
            raise FoundationStagingError(
                f"native retained publication failed: {relative}"
            ) from error
        return

    if posix_root_authority is None:
        raise FoundationStagingError(
            "POSIX component publication requires retained staging-root authority"
        )
    _write_posix_new_regular_beneath(
        posix_root_authority,
        data=data,
        relative=relative,
    )


def _atomic_publish_manifest(
    path: Path,
    data: bytes,
    *,
    baseline: os.stat_result,
    expected_original: bytes | None = None,
    posix_parent_authority: int | None = None,
) -> None:
    if os.name == "nt":
        if not _same_file_identity(path, baseline):
            raise FoundationStagingError("composition manifest changed during staging")
        parent = path.parent
        _require_exact_directory(parent, label="composition parent")
        if not _same_file_identity(path, baseline):
            raise FoundationStagingError("composition manifest changed during staging")
        try:
            with _retained_windows_directory(
                parent,
                label="composition parent",
            ) as authority:
                if not _same_file_identity(path, baseline):
                    raise FoundationStagingError(
                        "composition manifest changed during staging"
                    )
                publish_windows_regular_bytes(
                    authority,
                    target_name=path.name,
                    data=data,
                    replace=True,
                )
        except FoundationStagingError:
            raise
        except (OSError, RuntimeError, ValueError) as error:
            raise FoundationStagingError(
                "native retained composition publication failed"
            ) from error
        _require_exact_regular_file(path, label="composition manifest", single_link=True)
        if path.read_bytes() != data:
            raise FoundationStagingError("composition manifest write verification failed")
        return

    if posix_parent_authority is None:
        raise FoundationStagingError(
            "POSIX composition publication requires retained parent authority"
        )
    if type(expected_original) is not bytes:
        raise FoundationStagingError(
            "POSIX composition publication requires the exact original manifest bytes"
        )
    observed, opened = _read_posix_regular_file(
        posix_parent_authority,
        path.name,
        subject="composition manifest",
    )
    if (
        opened.st_dev != baseline.st_dev
        or opened.st_ino != baseline.st_ino
        or opened.st_size != baseline.st_size
        or observed != expected_original
    ):
        raise FoundationStagingError("composition manifest changed during staging")
    _replace_posix_regular(
        posix_parent_authority,
        target_name=path.name,
        data=data,
        subject="composition manifest",
    )


def _stage_source_controlled_components_unserialized(
    *,
    staging: Path,
    composition_path: Path,
    source_root: Path,
    descriptors: tuple[_SourceControlledComponent, ...],
    expected_source_sha: str | None = None,
    staging_authority=None,
    posix_staging_authority: int | None = None,
    posix_composition_authority: int | None = None,
) -> tuple[dict[str, str], ...]:
    """Internal TCB used only with reviewed, module-owned descriptor sets."""

    descriptors = _validate_descriptors(descriptors)
    source_root_baseline = _require_git_root(source_root)
    _require_exact_directory(staging, label="staging")
    if os.name == "nt":
        composition_info = _require_exact_regular_file(
            composition_path,
            label="composition manifest",
            single_link=True,
        )
        try:
            original_composition_bytes = composition_path.read_bytes()
        except OSError as error:
            raise FoundationStagingError("composition manifest cannot be read") from error
        if not _same_file_identity(composition_path, composition_info):
            raise FoundationStagingError("composition manifest changed while reading")
    else:
        if posix_composition_authority is None:
            raise FoundationStagingError(
                "POSIX composition preflight requires retained parent authority"
            )
        original_composition_bytes, composition_info = _read_posix_regular_file(
            posix_composition_authority,
            composition_path.name,
            subject="composition manifest",
        )
    composition = _parse_composition(original_composition_bytes)
    source_sha = composition["source_sha"]
    assert isinstance(source_sha, str)
    if expected_source_sha is not None:
        if (
            type(expected_source_sha) is not str
            or _GIT_OBJECT_ID.fullmatch(expected_source_sha) is None
        ):
            raise FoundationStagingError(
                "expected_source_sha must be an exact lowercase Git object id"
            )
        if source_sha != expected_source_sha:
            raise FoundationStagingError(
                "composition source_sha does not match expected source_sha"
            )
    existing_by_path, existing_ids = _composition_index(composition.get("components"))

    prepared: list[tuple[_SourceControlledComponent, bytes, dict[str, str], Path, bool]] = []
    for descriptor in descriptors:
        data = _git_source_bytes(
            descriptor.path,
            source_root=source_root,
            source_sha=source_sha,
        )
        digest = "sha256:" + sha256(data).hexdigest()
        expected = {
            "component_id": descriptor.component_id,
            "kind": descriptor.kind,
            "path": descriptor.path,
            "version": "source-controlled",
            "sha256": digest,
        }
        existing = existing_by_path.get(descriptor.path)
        if existing is not None and existing != expected:
            raise FoundationStagingError(
                f"composition has conflicting source-controlled component: {descriptor.path}"
            )
        if existing is None and descriptor.component_id in existing_ids:
            raise FoundationStagingError(
                f"composition component_id conflicts: {descriptor.component_id}"
            )
        destination = _preflight_existing_chain(
            staging,
            descriptor.path,
            root_label="staging",
            leaf_must_be_file=True,
            leaf_single_link=True,
        )
        exists = destination.exists()
        if exists:
            try:
                current = destination.read_bytes()
            except OSError as error:
                raise FoundationStagingError(
                    f"cannot read existing staged component: {descriptor.path}"
                ) from error
            if current != data:
                raise FoundationStagingError(
                    f"staging destination has conflicting bytes: {descriptor.path}"
                )
        prepared.append((descriptor, data, expected, destination, exists))

    components = composition["components"]
    assert isinstance(components, list)
    for descriptor, _data, expected, _destination, _exists in prepared:
        if descriptor.path not in existing_by_path:
            components.append(expected)
            existing_by_path[descriptor.path] = expected
            existing_ids.add(descriptor.component_id)
    components.sort(key=lambda item: str(item.get("path", "")))
    new_composition_bytes = _canonical_json(composition)

    if os.name == "nt":
        if not _same_file_identity(composition_path, composition_info):
            raise FoundationStagingError("composition manifest changed after preflight")
    else:
        assert posix_composition_authority is not None
        observed_composition, observed_info = _read_posix_regular_file(
            posix_composition_authority,
            composition_path.name,
            subject="composition manifest",
        )
        if (
            observed_info.st_dev != composition_info.st_dev
            or observed_info.st_ino != composition_info.st_ino
            or observed_info.st_size != composition_info.st_size
            or observed_composition != original_composition_bytes
        ):
            raise FoundationStagingError("composition manifest changed after preflight")
    for descriptor, data, _expected, destination, exists in prepared:
        _preflight_existing_chain(
            staging,
            descriptor.path,
            root_label="staging",
            leaf_must_be_file=True,
            leaf_single_link=True,
        )
        if exists and destination.read_bytes() != data:
            raise FoundationStagingError(
                f"staging destination changed after preflight: {descriptor.path}"
            )

    if os.name != "nt":
        _require_posix_source_root_generation(
            source_root,
            source_root_baseline,
        )
        if posix_staging_authority is None:
            raise FoundationStagingError(
                "POSIX staging requires retained root authority"
            )
        _require_linux_openat2_beneath_authority(posix_staging_authority)

    with ExitStack() as publication_authority:
        windows_parents: dict[Path, object] = {}
        posix_parents: dict[Path, int] = {}
        if os.name == "nt":
            if staging_authority is None:
                raise FoundationStagingError(
                    "Windows staging requires retained root authority"
                )
            component_parents = {
                destination.parent
                for _descriptor, _data, _expected, destination, _exists in prepared
            }
            for parent in sorted(
                component_parents,
                key=lambda item: (len(item.parts), str(item)),
            ):
                try:
                    relative_parent = parent.relative_to(staging)
                except ValueError as error:
                    raise FoundationStagingError(
                        "component parent escaped staging root"
                    ) from error
                try:
                    authority = publication_authority.enter_context(
                        retain_windows_relative_directory_namespace(
                            staging_authority,
                            tuple(relative_parent.parts),
                            create=True,
                        )
                    )
                except (OSError, RuntimeError, TypeError, ValueError) as error:
                    raise FoundationStagingError(
                        "staging component parent cannot be retained/created "
                        "relative to the staging authority"
                    ) from error
                windows_parents[parent] = authority
        else:
            if posix_staging_authority is None:
                raise FoundationStagingError(
                    "POSIX staging requires retained root authority"
                )
            component_parents = {
                destination.parent
                for _descriptor, _data, _expected, destination, _exists in prepared
            }
            for parent in sorted(
                component_parents,
                key=lambda item: (len(item.parts), str(item)),
            ):
                try:
                    relative_parent = parent.relative_to(staging)
                except ValueError as error:
                    raise FoundationStagingError(
                        "component parent escaped staging root"
                    ) from error
                authority = publication_authority.enter_context(
                    _retained_posix_relative_directory(
                        posix_staging_authority,
                        tuple(relative_parent.parts),
                        create=True,
                    )
                )
                posix_parents[parent] = authority

        if os.name != "nt":
            if posix_staging_authority is None:
                raise FoundationStagingError(
                    "POSIX staging authority disappeared before component publication"
                )
            # Detect a retained descendant that was detached/replaced while the
            # complete parent set was being acquired. This is a zero-component-
            # write guard; the root-authorized openat2 seam below remains the
            # final mutation authority for races after this validation.
            for parent, retained_descriptor in posix_parents.items():
                relative_parent = parent.relative_to(staging)
                _require_posix_relative_directory_matches(
                    posix_staging_authority,
                    tuple(relative_parent.parts),
                    retained_descriptor,
                )

        for descriptor, data, _expected, destination, exists in prepared:
            if os.name == "nt":
                parent_authority = windows_parents.get(destination.parent)
                if parent_authority is None:
                    raise FoundationStagingError(
                        f"missing retained component parent: {descriptor.path}"
                    )
                if exists:
                    try:
                        retained_descriptor = publication_authority.enter_context(
                            retain_windows_regular_file(
                                parent_authority,
                                target_name=destination.name,
                                subject="staged source-controlled component",
                            )
                        )
                        _read_retained_descriptor(
                            retained_descriptor,
                            expected=data,
                            relative=descriptor.path,
                        )
                    except FoundationStagingError:
                        raise
                    except (OSError, RuntimeError, TypeError, ValueError) as error:
                        raise FoundationStagingError(
                            f"existing staged component cannot be retained: {descriptor.path}"
                        ) from error
                else:
                    _atomic_publish_regular(
                        destination,
                        data,
                        root=staging,
                        relative=descriptor.path,
                        windows_parent_authority=parent_authority,
                        publication_authority=publication_authority,
                    )
                continue

            parent_authority = posix_parents.get(destination.parent)
            if parent_authority is None:
                raise FoundationStagingError(
                    f"missing retained POSIX component parent: {descriptor.path}"
                )
            if exists:
                observed, _opened = _read_posix_regular_file(
                    parent_authority,
                    destination.name,
                    subject="existing staged component",
                )
                if observed != data:
                    raise FoundationStagingError(
                        f"staging destination changed after preflight: {descriptor.path}"
                    )
            else:
                _atomic_publish_regular(
                    destination,
                    data,
                    root=staging,
                    relative=descriptor.path,
                    posix_root_authority=posix_staging_authority,
                )

        if os.name != "nt":
            if posix_staging_authority is None:
                raise FoundationStagingError(
                    "POSIX staging authority disappeared before manifest commit"
                )
            for parent, retained_descriptor in posix_parents.items():
                relative_parent = parent.relative_to(staging)
                _require_posix_relative_directory_matches(
                    posix_staging_authority,
                    tuple(relative_parent.parts),
                    retained_descriptor,
                )

        if new_composition_bytes != original_composition_bytes:
            if os.name == "nt":
                _atomic_publish_manifest(
                    composition_path,
                    new_composition_bytes,
                    baseline=composition_info,
                )
            else:
                _atomic_publish_manifest(
                    composition_path,
                    new_composition_bytes,
                    baseline=composition_info,
                    expected_original=original_composition_bytes,
                    posix_parent_authority=posix_composition_authority,
                )

    return tuple(expected for _descriptor, _data, expected, _destination, _exists in prepared)


def _stage_source_controlled_components(
    *,
    staging: Path,
    composition_path: Path,
    source_root: Path,
    descriptors: tuple[_SourceControlledComponent, ...],
    expected_source_sha: str | None = None,
) -> tuple[dict[str, str], ...]:
    """Run one serialized preflight -> component publish -> manifest commit."""

    with _composition_publish_transaction(
        composition_path.parent
    ) as composition_authority:
        if os.name == "nt":
            with _retained_windows_directory(
                source_root,
                label="source_root",
            ), _retained_windows_directory(
                staging,
                label="staging",
            ) as staging_authority:
                return _stage_source_controlled_components_unserialized(
                    staging=staging,
                    composition_path=composition_path,
                    source_root=source_root,
                    descriptors=descriptors,
                    expected_source_sha=expected_source_sha,
                    staging_authority=staging_authority,
                )

        with _retained_posix_directory(
            source_root,
            label="source_root",
        ), _retained_posix_directory(
            staging,
            label="staging",
        ) as posix_staging_authority:
            return _stage_source_controlled_components_unserialized(
                staging=staging,
                composition_path=composition_path,
                source_root=source_root,
                descriptors=descriptors,
                expected_source_sha=expected_source_sha,
                posix_staging_authority=posix_staging_authority,
                posix_composition_authority=composition_authority,
            )


def stage_windows_foundation(
    *,
    staging: Path,
    composition_path: Path,
    source_root: Path = ROOT,
    expected_source_sha: str | None = None,
) -> tuple[dict[str, str], ...]:
    """Stage exact committed foundation bytes and bind them into composition authority."""

    return _stage_source_controlled_components(
        staging=staging,
        composition_path=composition_path,
        source_root=source_root,
        descriptors=_REQUIRED,
        expected_source_sha=expected_source_sha,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--staging", required=True, type=Path)
    parser.add_argument("--composition", required=True, type=Path)
    parser.add_argument("--source-root", type=Path, default=ROOT)
    parser.add_argument("--expected-source-sha")
    args = parser.parse_args()
    try:
        result = stage_windows_foundation(
            staging=args.staging,
            composition_path=args.composition,
            source_root=args.source_root,
            expected_source_sha=args.expected_source_sha,
        )
    except FoundationStagingError as error:
        print(str(error), file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
