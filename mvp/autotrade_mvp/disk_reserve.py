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


class DiskReserveError(ValueError):
    pass


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
            or reserve_bytes <= 0
        ):
            raise DiskReserveError("reserve_bytes must be a positive integer")
        self.path = Path(path)
        self.reserve_bytes = reserve_bytes
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def status(self) -> DiskReserveStatus:
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
            return DiskReserveStatus(
                path=str(self.path),
                expected_bytes=self.reserve_bytes,
                present=False,
                exact_size=False,
                available_for_emergency=False,
            )
        exact = self.path.is_file() and stat.st_size == self.reserve_bytes
        return DiskReserveStatus(
            path=str(self.path),
            expected_bytes=self.reserve_bytes,
            present=True,
            exact_size=exact,
            available_for_emergency=exact,
        )

    def provision(self) -> DiskReserveStatus:
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
                chunk = b"\\0" * min(1024 * 1024, self.reserve_bytes)
                remaining = self.reserve_bytes
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

    def release_for_emergency(
        self,
        *,
        reason: ReserveReleaseReason,
    ) -> DiskReserveStatus:
        if not isinstance(reason, ReserveReleaseReason):
            raise DiskReserveError("release reason must be explicit")
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
