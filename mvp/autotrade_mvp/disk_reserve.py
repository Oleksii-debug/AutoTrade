"""Preallocated emergency disk reserve for fail-closed recovery.

The reserve is not a second journal and never fabricates durability. Its purpose
is to keep explicitly reserved bytes available so a qualified emergency path
can free them after a journal write failure and then record/reconcile the
exceptional condition.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import os
from pathlib import Path
import sys

from autotrade_foundation.local_filesystem import (
    LocalFilesystemQualificationError,
    freeze_local_filesystem_path,
    require_qualified_local_filesystem_path,
)
from autotrade_foundation.windows_namespace import (
    create_windows_regular_file_exclusive,
    require_windows_namespace_component,
    retain_windows_parent_namespace,
    retain_windows_regular_file,
    retain_windows_regular_file_for_delete,
)


class DiskReserveError(ValueError):
    pass


_RESERVE_MAGIC = b"AUTOTRADE_EMERGENCY_DISK_RESERVE_V1\n"


class ReserveReleaseReason(StrEnum):
    JOURNAL_WRITE_FAILURE = "JOURNAL_WRITE_FAILURE"
    RECOVERY_CRITICAL = "RECOVERY_CRITICAL"


@dataclass(frozen=True, slots=True)
class DiskReserveStatus:
    path: str
    expected_bytes: int
    present: bool
    exact_size: bool
    available_for_emergency: bool


class EmergencyDiskReserve:
    """Own exactly one non-secret preallocated reserve file."""

    def __init__(self, path: str | Path, *, reserve_bytes: int) -> None:
        if (
            not isinstance(reserve_bytes, int)
            or isinstance(reserve_bytes, bool)
            or reserve_bytes < len(_RESERVE_MAGIC)
        ):
            raise DiskReserveError(
                "reserve_bytes must be an integer large enough for the reserve header"
            )

        candidate = Path(path)
        if sys.platform == "win32":
            try:
                frozen = Path(freeze_local_filesystem_path(candidate.expanduser()))
                require_qualified_local_filesystem_path(frozen)
                require_windows_namespace_component(
                    frozen.name,
                    subject="emergency reserve file",
                )
            except (LocalFilesystemQualificationError, RuntimeError) as error:
                raise DiskReserveError(
                    "emergency reserve path lacks qualified Windows filesystem authority"
                ) from error
            self.path = frozen
        else:
            self.path = candidate
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.reserve_bytes = reserve_bytes

    def _absent_status(self) -> DiskReserveStatus:
        return DiskReserveStatus(
            path=str(self.path),
            expected_bytes=self.reserve_bytes,
            present=False,
            exact_size=False,
            available_for_emergency=False,
        )

    def _status_from_descriptor(self, descriptor: int) -> DiskReserveStatus:
        stat = os.fstat(descriptor)
        exact = stat.st_size == self.reserve_bytes
        signature_matches = False
        if exact:
            os.lseek(descriptor, 0, os.SEEK_SET)
            signature_matches = os.read(descriptor, len(_RESERVE_MAGIC)) == _RESERVE_MAGIC
        return DiskReserveStatus(
            path=str(self.path),
            expected_bytes=self.reserve_bytes,
            present=True,
            exact_size=exact,
            available_for_emergency=exact and signature_matches,
        )

    def _windows_status(self) -> DiskReserveStatus:
        try:
            with retain_windows_parent_namespace(
                self.path,
                create=False,
            ) as parent_authority:
                with retain_windows_regular_file(
                    parent_authority,
                    target_name=self.path.name,
                    subject="emergency reserve file",
                ) as descriptor:
                    return self._status_from_descriptor(descriptor)
        except FileNotFoundError:
            return self._absent_status()
        except (OSError, RuntimeError, TypeError) as error:
            raise DiskReserveError(
                "emergency reserve Windows namespace authority verification failed"
            ) from error

    def status(self) -> DiskReserveStatus:
        if sys.platform == "win32":
            return self._windows_status()

        if self.path.is_symlink():
            return DiskReserveStatus(
                path=str(self.path),
                expected_bytes=self.reserve_bytes,
                present=True,
                exact_size=False,
                available_for_emergency=False,
            )
        try:
            stat = self.path.stat()
        except FileNotFoundError:
            return self._absent_status()
        exact = self.path.is_file() and stat.st_size == self.reserve_bytes
        signature_matches = False
        if exact:
            try:
                with self.path.open("rb") as stream:
                    signature_matches = stream.read(len(_RESERVE_MAGIC)) == _RESERVE_MAGIC
            except OSError:
                signature_matches = False
        return DiskReserveStatus(
            path=str(self.path),
            expected_bytes=self.reserve_bytes,
            present=True,
            exact_size=exact,
            available_for_emergency=exact and signature_matches,
        )

    def _write_reserve_descriptor(self, descriptor: int) -> None:
        os.lseek(descriptor, 0, os.SEEK_SET)
        header_view = memoryview(_RESERVE_MAGIC)
        header_written = 0
        while header_written < len(header_view):
            count = os.write(descriptor, header_view[header_written:])
            if count <= 0:
                raise OSError("short write while provisioning disk reserve header")
            header_written += count

        remaining = self.reserve_bytes - len(_RESERVE_MAGIC)
        chunk = b"\0" * min(1024 * 1024, max(1, remaining))
        while remaining:
            piece = chunk if remaining >= len(chunk) else chunk[:remaining]
            written = os.write(descriptor, piece)
            if written <= 0:
                raise OSError("short write while provisioning disk reserve")
            remaining -= written
        os.fsync(descriptor)

    def _provision_windows(self) -> DiskReserveStatus:
        try:
            with retain_windows_parent_namespace(
                self.path,
                create=True,
            ) as parent_authority:
                try:
                    with retain_windows_regular_file(
                        parent_authority,
                        target_name=self.path.name,
                        subject="emergency reserve file",
                    ) as descriptor:
                        current = self._status_from_descriptor(descriptor)
                except FileNotFoundError:
                    current = None

                if current is not None:
                    if current.available_for_emergency:
                        return current
                    raise DiskReserveError(
                        "existing emergency reserve path is invalid; refusing to overwrite"
                    )

                try:
                    with create_windows_regular_file_exclusive(
                        parent_authority,
                        target_name=self.path.name,
                        subject="emergency reserve file",
                    ) as descriptor:
                        self._write_reserve_descriptor(descriptor)
                        result = self._status_from_descriptor(descriptor)
                        if not result.available_for_emergency:
                            raise DiskReserveError(
                                "emergency reserve provisioning did not persist exact bytes"
                            )
                        return result
                except FileExistsError as error:
                    raise DiskReserveError(
                        "emergency reserve appeared during provisioning"
                    ) from error
        except DiskReserveError:
            raise
        except (OSError, RuntimeError, TypeError) as error:
            raise DiskReserveError(
                "emergency reserve Windows provisioning authority failed"
            ) from error

    def provision(self) -> DiskReserveStatus:
        if sys.platform == "win32":
            return self._provision_windows()

        current = self.status()
        if current.available_for_emergency:
            return current
        if current.present:
            raise DiskReserveError(
                "existing emergency reserve path is invalid; refusing to overwrite"
            )

        try:
            fd = os.open(
                self.path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except FileExistsError as error:
            raise DiskReserveError(
                "emergency reserve appeared during provisioning"
            ) from error

        complete = False
        try:
            with os.fdopen(fd, "wb", buffering=0) as stream:
                written = stream.write(_RESERVE_MAGIC)
                if written != len(_RESERVE_MAGIC):
                    raise OSError("short write while provisioning disk reserve header")
                remaining = self.reserve_bytes - len(_RESERVE_MAGIC)
                chunk = b"\0" * min(1024 * 1024, max(1, remaining))
                while remaining:
                    piece = chunk if remaining >= len(chunk) else chunk[:remaining]
                    written = stream.write(piece)
                    if written != len(piece):
                        raise OSError("short write while provisioning disk reserve")
                    remaining -= written
                stream.flush()
                os.fsync(stream.fileno())
            complete = True
        finally:
            if not complete:
                try:
                    self.path.unlink()
                except FileNotFoundError:
                    pass
        result = self.status()
        if not result.available_for_emergency:
            raise DiskReserveError("emergency reserve provisioning did not persist exact bytes")
        return result

    def _release_windows(self) -> DiskReserveStatus:
        try:
            with retain_windows_parent_namespace(
                self.path,
                create=False,
            ) as parent_authority:
                with retain_windows_regular_file_for_delete(
                    parent_authority,
                    target_name=self.path.name,
                    subject="emergency reserve file",
                ) as descriptor:
                    current = self._status_from_descriptor(descriptor)
                    if not current.available_for_emergency:
                        raise DiskReserveError(
                            "no verified emergency reserve is available"
                        )
        except FileNotFoundError as error:
            raise DiskReserveError(
                "no verified emergency reserve is available"
            ) from error
        except DiskReserveError:
            raise
        except (OSError, RuntimeError, TypeError) as error:
            raise DiskReserveError(
                "emergency reserve Windows release authority failed"
            ) from error
        return self._absent_status()

    def release_for_emergency(
        self,
        *,
        reason: ReserveReleaseReason,
    ) -> DiskReserveStatus:
        if not isinstance(reason, ReserveReleaseReason):
            raise DiskReserveError("release reason must be explicit")
        if sys.platform == "win32":
            return self._release_windows()

        current = self.status()
        if not current.available_for_emergency:
            raise DiskReserveError("no verified emergency reserve is available")
        self.path.unlink()
        return self.status()

    def restore_after_recovery(self, *, journal_writable: bool) -> DiskReserveStatus:
        if not isinstance(journal_writable, bool):
            raise DiskReserveError("journal_writable must be boolean")
        if not journal_writable:
            raise DiskReserveError(
                "cannot restore reserve while durable journal writes are unhealthy"
            )
        return self.provision()
