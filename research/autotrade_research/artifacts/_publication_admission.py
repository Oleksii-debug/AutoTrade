from __future__ import annotations

from hashlib import sha256
import json
import os
import stat
import sys
from typing import Any

from . import _namespace_guard as _guard
from . import _retained_namespace as _retained
from . import _retained_publication as _publication
from . import _retained_publication_hardening as _posix
from . import _windows_retained_publication as _windows
from . import _windows_retained_publication_hardening as _win

_store = _posix._store
_MARKER_VERSION = 1
_PREPARED = "PREPARED"
_COMMITTED = "COMMITTED"


def _marker_name(artifact_id: str, state: str) -> str:
    suffix = "prepared" if state == _PREPARED else "committed"
    return f".{artifact_id}.publication-{suffix}"


def _identity_payload(identity: Any) -> dict[str, Any]:
    if sys.platform == "win32":
        return {
            "platform": "windows",
            "volume_serial": int(identity.dwVolumeSerialNumber),
            "file_index_high": int(identity.nFileIndexHigh),
            "file_index_low": int(identity.nFileIndexLow),
        }
    return {
        "platform": "posix",
        "st_dev": int(identity.st_dev),
        "st_ino": int(identity.st_ino),
    }


def _marker_hash(marker: dict[str, Any]) -> str:
    payload = dict(marker)
    payload.pop("marker_hash", None)
    return sha256(_publication._json_bytes(payload)).hexdigest()


def _marker_payload(
    *,
    state: str,
    manifest: dict[str, Any],
    transaction_id: str,
    prefix_identity: Any,
) -> dict[str, Any]:
    marker = {
        "marker_version": _MARKER_VERSION,
        "state": state,
        "transaction_id": transaction_id,
        "artifact_id": manifest["artifact_id"],
        "manifest_hash": manifest["manifest_hash"],
        "sha256": manifest["sha256"],
        "prefix_identity": _identity_payload(prefix_identity),
    }
    marker["marker_hash"] = _marker_hash(marker)
    return marker


def _write_marker_posix(self, marker: dict[str, Any]) -> None:
    name = _marker_name(marker["artifact_id"], marker["state"])
    payload = _publication._json_bytes(marker)
    temporary_name = None
    descriptor = None
    try:
        temporary_name, descriptor = _publication._open_temp_posix(
            self._retained_manifests_fd,
            prefix="publication-admission",
        )
        _publication._write_all(descriptor, payload)
        _publication._verify_staged_descriptor(
            descriptor,
            expected_digest=sha256(payload).hexdigest(),
            expected_bytes=len(payload),
        )
        os.close(descriptor)
        descriptor = None
        _retained._assert_directory_continuity(
            self,
            ("manifests",),
            "manifests",
        )
        _posix._rename_noreplace_posix(
            self._retained_manifests_fd,
            temporary_name,
            name,
        )
        temporary_name = None
        _publication._sync_directory_fd(self._retained_manifests_fd)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary_name is not None:
            _publication._safe_unlink(
                self._retained_manifests_fd,
                temporary_name,
            )


def _write_marker_windows(self, marker: dict[str, Any]) -> None:
    manifests = _windows._open_mutation_directory(
        self,
        ("manifests",),
        retained_name="manifests",
    )
    descriptor = None
    try:
        payload = _publication._json_bytes(marker)
        _name, descriptor = _windows._create_temp_fd(
            manifests,
            prefix="publication-admission",
        )
        _publication._write_all(descriptor, payload)
        _publication._verify_staged_descriptor(
            descriptor,
            expected_digest=sha256(payload).hexdigest(),
            expected_bytes=len(payload),
        )
        try:
            _windows._publish_temp_fd(
                descriptor,
                target_parent=manifests,
                target_name=_marker_name(marker["artifact_id"], marker["state"]),
                replace=False,
            )
        except OSError as error:
            descriptor = None
            if _win._is_destination_collision(error):
                raise _store.ArtifactConflict(
                    "artifact publication admission marker already exists"
                ) from error
            raise
        else:
            descriptor = None
        _store.sync_parent_directory(
            self._manifest_path(marker["artifact_id"])
        )
    finally:
        if descriptor is not None:
            try:
                _win._delete_fd_on_close(descriptor)
            except BaseException:
                pass
            os.close(descriptor)
        _guard._close_windows_handle(manifests)


def prepare_publication(
    self,
    *,
    manifest: dict[str, Any],
    transaction_id: str,
    prefix_identity: Any,
) -> None:
    marker = _marker_payload(
        state=_PREPARED,
        manifest=manifest,
        transaction_id=transaction_id,
        prefix_identity=prefix_identity,
    )
    if sys.platform == "win32":
        _write_marker_windows(self, marker)
    else:
        _write_marker_posix(self, marker)


def commit_publication(
    self,
    *,
    manifest: dict[str, Any],
    transaction_id: str,
    prefix_identity: Any,
) -> None:
    marker = _marker_payload(
        state=_COMMITTED,
        manifest=manifest,
        transaction_id=transaction_id,
        prefix_identity=prefix_identity,
    )
    if sys.platform == "win32":
        _write_marker_windows(self, marker)
    else:
        _write_marker_posix(self, marker)


def _read_descriptor_bytes(descriptor: int, expected_bytes: int) -> bytes:
    chunks: list[bytes] = []
    copied = 0
    while copied <= expected_bytes:
        chunk = os.read(descriptor, min(1024 * 1024, expected_bytes + 1 - copied))
        if not chunk:
            break
        chunks.append(chunk)
        copied += len(chunk)
    if copied != expected_bytes:
        raise _store.ArtifactIntegrityError(
            "artifact publication admission marker changed during read"
        )
    return b"".join(chunks)


def _read_marker_posix(self, name: str) -> dict[str, Any] | None:
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    if not no_follow or os.open not in getattr(os, "supports_dir_fd", set()):
        raise _store.ArtifactIntegrityError(
            "artifact publication admission lacks descriptor-relative platform support"
        )
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NONBLOCK", 0)
            | no_follow,
            dir_fd=self._retained_manifests_fd,
        )
    except FileNotFoundError:
        return None
    except OSError as error:
        raise _store.ArtifactIntegrityError(
            "artifact publication admission marker could not be opened safely"
        ) from error
    try:
        opened = os.fstat(descriptor)
        self._reject_reparse_point(opened, subject="artifact publication admission marker")
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            raise _store.ArtifactIntegrityError(
                "artifact publication admission marker is not canonical"
            )
        return _decode_marker(_read_descriptor_bytes(descriptor, opened.st_size))
    finally:
        os.close(descriptor)


def _read_marker_windows(self, name: str) -> dict[str, Any] | None:
    parent = getattr(self, "_retained_manifests_handle", None)
    if not parent:
        raise _store.ArtifactIntegrityError(
            "retained manifest namespace handle is unavailable"
        )
    handle = None
    descriptor = None
    try:
        try:
            handle = _guard._nt_open_relative_handle(
                parent,
                name,
                directory=False,
                subject="artifact publication admission marker",
            )
        except FileNotFoundError:
            return None
        descriptor = _guard._windows_file_handle_to_descriptor(handle)
        handle = None
        opened = os.fstat(descriptor)
        self._reject_reparse_point(opened, subject="artifact publication admission marker")
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            raise _store.ArtifactIntegrityError(
                "artifact publication admission marker is not canonical"
            )
        return _decode_marker(_read_descriptor_bytes(descriptor, opened.st_size))
    finally:
        if descriptor is not None:
            os.close(descriptor)
        elif handle is not None:
            _guard._close_windows_handle(handle)


def _read_marker(self, artifact_id: str, state: str) -> dict[str, Any] | None:
    name = _marker_name(artifact_id, state)
    if sys.platform == "win32":
        return _read_marker_windows(self, name)
    return _read_marker_posix(self, name)


def _decode_marker(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError) as error:
        raise _store.ArtifactIntegrityError(
            "artifact publication admission marker is invalid"
        ) from error
    if type(value) is not dict:
        raise _store.ArtifactIntegrityError(
            "artifact publication admission marker is invalid"
        )
    required = {
        "marker_version",
        "state",
        "transaction_id",
        "artifact_id",
        "manifest_hash",
        "sha256",
        "prefix_identity",
        "marker_hash",
    }
    if set(value) != required or value.get("marker_version") != _MARKER_VERSION:
        raise _store.ArtifactIntegrityError(
            "artifact publication admission marker contract is invalid"
        )
    if value.get("state") not in {_PREPARED, _COMMITTED}:
        raise _store.ArtifactIntegrityError(
            "artifact publication admission marker state is invalid"
        )
    if not isinstance(value.get("transaction_id"), str) or not value["transaction_id"]:
        raise _store.ArtifactIntegrityError(
            "artifact publication admission transaction identity is invalid"
        )
    if value.get("marker_hash") != _marker_hash(value):
        raise _store.ArtifactIntegrityError(
            "artifact publication admission marker integrity mismatch"
        )
    return value


def _assert_current_prefix_identity(self, manifest: dict[str, Any], expected: dict[str, Any]) -> None:
    digest = manifest["sha256"].removeprefix("sha256:")
    prefix = digest[:2]
    if expected.get("platform") == "windows":
        if sys.platform != "win32":
            raise _store.ArtifactIntegrityError(
                "artifact publication admission platform identity mismatch"
            )
        parent = getattr(self, "_retained_objects_handle", None)
        if not parent:
            raise _store.ArtifactIntegrityError(
                "retained object namespace handle is unavailable"
            )
        handle = _guard._nt_open_relative_handle(
            parent,
            prefix,
            directory=True,
            subject="artifact publication admitted object prefix",
        )
        try:
            current = _guard._windows_handle_information(
                handle,
                subject="artifact publication admitted object prefix",
            )
            current_payload = _identity_payload(current)
        finally:
            _guard._close_windows_handle(handle)
    else:
        descriptor = _guard._open_posix_directory_component(
            self,
            self._retained_objects_fd,
            prefix,
            subject="artifact publication admitted object prefix",
        )
        try:
            current_payload = _identity_payload(os.fstat(descriptor))
        finally:
            os.close(descriptor)
    if current_payload != expected:
        raise _store.ArtifactIntegrityError(
            "artifact publication admitted object generation changed"
        )


def validate_manifest_admission(self, manifest: dict[str, Any]) -> None:
    artifact_id = manifest["artifact_id"]
    prepared = _read_marker(self, artifact_id, _PREPARED)
    committed = _read_marker(self, artifact_id, _COMMITTED)
    if prepared is None and committed is None:
        # Authenticated manifests created before the admission protocol remain
        # readable as legacy committed authority. New publications always create
        # PREPARED before exposing their canonical manifest.
        return
    if prepared is None or committed is None:
        raise _store.ArtifactIntegrityError(
            "artifact manifest publication is incomplete"
        )
    for marker, state in ((prepared, _PREPARED), (committed, _COMMITTED)):
        if (
            marker["state"] != state
            or marker["artifact_id"] != artifact_id
            or marker["manifest_hash"] != manifest.get("manifest_hash")
            or marker["sha256"] != manifest.get("sha256")
        ):
            raise _store.ArtifactIntegrityError(
                "artifact publication admission marker does not match manifest"
            )
    if prepared["transaction_id"] != committed["transaction_id"]:
        raise _store.ArtifactIntegrityError(
            "artifact publication admission transaction mismatch"
        )
    if prepared["prefix_identity"] != committed["prefix_identity"]:
        raise _store.ArtifactIntegrityError(
            "artifact publication admission generation mismatch"
        )
    _assert_current_prefix_identity(
        self,
        manifest,
        committed["prefix_identity"],
    )


def install_publication_admission() -> None:
    artifact_store = _store.ArtifactStore
    if getattr(artifact_store, "_publication_admission", False):
        return
    original_load_manifest_path = artifact_store._load_manifest_path

    def load_manifest_path_with_admission(self, path):
        manifest = original_load_manifest_path(self, path)
        if _store._verify_manifest_integrity(manifest, required=False):
            validate_manifest_admission(self, manifest)
        return manifest

    artifact_store._load_manifest_path = load_manifest_path_with_admission
    artifact_store._publication_admission = True
