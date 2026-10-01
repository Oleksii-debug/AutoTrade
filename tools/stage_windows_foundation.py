"""Stage reviewed source-controlled AutoTrade components into a product composition.

The staging authority consumes bytes from one exact Git object, not from the
mutable checkout.  It preflights the complete descriptor/composition graph and
all currently-existing destination authority before publishing regular files.
The composition manifest is published last.
"""

from __future__ import annotations

import argparse
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import tempfile
from typing import Iterable

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


def _git(*args: str, source_root: Path) -> bytes:
    try:
        completed = subprocess.run(
            ("git", "-C", str(source_root), *args),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError as error:
        raise FoundationStagingError("git executable is unavailable") from error
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise FoundationStagingError(f"exact Git source lookup failed: {detail or args[0]}")
    return completed.stdout


def _require_git_root(source_root: Path) -> None:
    _require_exact_directory(source_root, label="source_root")
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
    return _git("show", f"{source_sha}:{relative}", source_root=source_root)


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
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
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

    baseline = _require_exact_directory(parent, label="composition parent")
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
                yield
        return

    try:
        import fcntl
    except ImportError as error:
        raise FoundationStagingError(
            "POSIX composition serialization is unavailable"
        ) from error

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
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
        visible = _require_exact_directory(parent, label="composition parent")
        if visible.st_dev != retained.st_dev or visible.st_ino != retained.st_ino:
            raise FoundationStagingError(
                "composition parent changed while acquiring publication lock"
            )
        yield
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


def _atomic_publish_regular(
    path: Path,
    data: bytes,
    *,
    root: Path,
    relative: str,
    windows_parent_authority=None,
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

    path.parent.mkdir(parents=True, exist_ok=True)
    if PurePosixPath(relative).parent.as_posix() != ".":
        _preflight_existing_chain(
            root,
            str(PurePosixPath(relative).parent),
            root_label="staging",
            leaf_must_be_file=False,
        )
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb", closefd=True) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        _preflight_existing_chain(
            root,
            relative,
            root_label="staging",
            leaf_must_be_file=True,
            leaf_single_link=True,
        )
        os.replace(temporary, path)
        _require_exact_regular_file(path, label="published component", single_link=True)
        if path.read_bytes() != data:
            raise FoundationStagingError(f"staged component verification failed: {relative}")
        _fsync_directory(path.parent)
    except BaseException:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _atomic_publish_manifest(path: Path, data: bytes, *, baseline: os.stat_result) -> None:
    if not _same_file_identity(path, baseline):
        raise FoundationStagingError("composition manifest changed during staging")
    parent = path.parent
    _require_exact_directory(parent, label="composition parent")
    if os.name == "nt":
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

    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb", closefd=True) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if not _same_file_identity(path, baseline):
            raise FoundationStagingError("composition manifest changed during staging")
        os.replace(temporary, path)
        _require_exact_regular_file(path, label="composition manifest", single_link=True)
        if path.read_bytes() != data:
            raise FoundationStagingError("composition manifest write verification failed")
        _fsync_directory(parent)
    except BaseException:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _stage_source_controlled_components_unserialized(
    *,
    staging: Path,
    composition_path: Path,
    source_root: Path,
    descriptors: tuple[_SourceControlledComponent, ...],
    staging_authority=None,
) -> tuple[dict[str, str], ...]:
    """Internal TCB used only with reviewed, module-owned descriptor sets."""

    descriptors = _validate_descriptors(descriptors)
    _require_git_root(source_root)
    _require_exact_directory(staging, label="staging")
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
    composition = _parse_composition(original_composition_bytes)
    source_sha = composition["source_sha"]
    assert isinstance(source_sha, str)
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

    if not _same_file_identity(composition_path, composition_info):
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

    with ExitStack() as publication_authority:
        windows_parents: dict[Path, object] = {}
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

            if not exists:
                _atomic_publish_regular(
                    destination,
                    data,
                    root=staging,
                    relative=descriptor.path,
                )

        if new_composition_bytes != original_composition_bytes:
            _atomic_publish_manifest(
                composition_path,
                new_composition_bytes,
                baseline=composition_info,
            )

    return tuple(expected for _descriptor, _data, expected, _destination, _exists in prepared)


def _stage_source_controlled_components(
    *,
    staging: Path,
    composition_path: Path,
    source_root: Path,
    descriptors: tuple[_SourceControlledComponent, ...],
) -> tuple[dict[str, str], ...]:
    """Run one serialized preflight -> component publish -> manifest commit."""

    with _composition_publish_transaction(composition_path.parent):
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
                staging_authority=staging_authority,
            )


def stage_windows_foundation(
    *,
    staging: Path,
    composition_path: Path,
    source_root: Path = ROOT,
) -> tuple[dict[str, str], ...]:
    """Stage exact committed foundation bytes and bind them into composition authority."""

    return _stage_source_controlled_components(
        staging=staging,
        composition_path=composition_path,
        source_root=source_root,
        descriptors=_REQUIRED,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--staging", required=True, type=Path)
    parser.add_argument("--composition", required=True, type=Path)
    parser.add_argument("--source-root", type=Path, default=ROOT)
    args = parser.parse_args()
    try:
        result = stage_windows_foundation(
            staging=args.staging,
            composition_path=args.composition,
            source_root=args.source_root,
        )
    except FoundationStagingError as error:
        print(str(error), file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
