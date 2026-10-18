"""Bounded durable technical heartbeat for the local product runtime.

The sampler is deliberately outside the financial journal.  Its records are
best-effort operational evidence only: they cannot authorize a command or be
consumed as a market observation.  Segment rotation bounds retained data while
the sequence and hash link make gaps and restarts visible.
"""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import stat
from threading import Event, Lock, Thread, active_count
import time

from .diagnostics import redact_diagnostic_value


_ACTIVE_NAME = "runtime-diagnostics.jsonl"
_SEGMENT_PREFIX = "runtime-diagnostics-"
_SEGMENT_SUFFIX = ".jsonl"


def _json_line(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


class RuntimeDiagnosticSampler:
    """Persist one technical liveness sample at approximately one-second cadence."""

    def __init__(
        self,
        root: Path,
        *,
        interval_seconds: float = 1.0,
        max_segment_bytes: int = 512 * 1024,
        max_segments: int = 8,
        monotonic=time.monotonic,
        clock=lambda: datetime.now(timezone.utc),
    ) -> None:
        if type(root) is not type(Path()):
            raise TypeError("diagnostic root must be an exact Path")
        if root.exists() and (root.is_symlink() or not root.is_dir()):
            raise ValueError("diagnostic root must be an ordinary directory")
        if (
            isinstance(interval_seconds, bool)
            or not isinstance(interval_seconds, (int, float))
            or not 0.1 <= float(interval_seconds) <= 60.0
        ):
            raise ValueError("diagnostic interval must be between 0.1 and 60 seconds")
        if type(max_segment_bytes) is not int or max_segment_bytes < 1024:
            raise ValueError("diagnostic segment size must be at least 1024 bytes")
        if type(max_segments) is not int or not 2 <= max_segments <= 128:
            raise ValueError("diagnostic segment count must be between 2 and 128")
        if not callable(monotonic) or not callable(clock):
            raise TypeError("diagnostic clocks must be callable")

        self.root = root
        self.interval_seconds = float(interval_seconds)
        self.max_segment_bytes = max_segment_bytes
        self.max_segments = max_segments
        self._monotonic = monotonic
        self._clock = clock
        self._stop = Event()
        self._write_guard = Lock()
        self._thread: Thread | None = None
        self._sequence = 0
        self._previous_digest = "0" * 64
        self._prior_cpu = time.process_time()
        self._prior_tick = float(monotonic())
        self.error: str | None = None

    @property
    def active_path(self) -> Path:
        return self.root / _ACTIVE_NAME

    def _rotated_paths(self) -> list[Path]:
        paths = []
        for path in self.root.glob(_SEGMENT_PREFIX + "*" + _SEGMENT_SUFFIX):
            middle = path.name[len(_SEGMENT_PREFIX):-len(_SEGMENT_SUFFIX)]
            if len(middle) != 20 or not middle.isdigit():
                raise ValueError("unexpected diagnostic segment name")
            paths.append(path)
        return sorted(paths)

    @staticmethod
    def _ordinary_file(path: Path) -> None:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("diagnostic segment must be an ordinary unaliased file")

    def _memory_bytes(self) -> int | None:
        try:
            if os.name == "posix" and Path("/proc/self/statm").is_file():
                resident_pages = int(Path("/proc/self/statm").read_text().split()[1])
                return resident_pages * int(os.sysconf("SC_PAGE_SIZE"))
            if os.name == "nt":
                import ctypes
                from ctypes import wintypes

                class Counters(ctypes.Structure):
                    _fields_ = [
                        ("cb", wintypes.DWORD),
                        ("page_fault_count", wintypes.DWORD),
                        ("peak_working_set_size", ctypes.c_size_t),
                        ("working_set_size", ctypes.c_size_t),
                        ("quota_peak_paged_pool_usage", ctypes.c_size_t),
                        ("quota_paged_pool_usage", ctypes.c_size_t),
                        ("quota_peak_non_paged_pool_usage", ctypes.c_size_t),
                        ("quota_non_paged_pool_usage", ctypes.c_size_t),
                        ("pagefile_usage", ctypes.c_size_t),
                        ("peak_pagefile_usage", ctypes.c_size_t),
                    ]

                counters = Counters()
                counters.cb = ctypes.sizeof(counters)
                if ctypes.windll.psapi.GetProcessMemoryInfo(
                    ctypes.windll.kernel32.GetCurrentProcess(),
                    ctypes.byref(counters),
                    counters.cb,
                ):
                    return int(counters.working_set_size)
        except (AttributeError, OSError, ValueError):
            pass
        return None

    def _sample(self) -> dict:
        now = self._clock()
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise ValueError("diagnostic clock must return a timezone-aware datetime")
        tick = float(self._monotonic())
        cpu = time.process_time()
        elapsed = max(tick - self._prior_tick, 0.000001)
        cpu_percent = max(
            0.0,
            min(
                100.0 * (cpu - self._prior_cpu) / elapsed,
                100.0 * (os.cpu_count() or 1),
            ),
        )
        self._prior_tick, self._prior_cpu = tick, cpu
        free_bytes = shutil.disk_usage(self.root).free
        return {
            "schema_version": "1.0.0",
            "recorded_at_utc": now.astimezone(timezone.utc).isoformat(),
            "sequence": self._sequence + 1,
            "previous_sha256": self._previous_digest,
            "runtime_phase": "RUNNING",
            "heartbeat_interval_seconds": self.interval_seconds,
            "process_cpu_percent": round(cpu_percent, 2),
            "process_memory_bytes": self._memory_bytes(),
            "process_thread_count": active_count(),
            "disk_free_bytes": free_bytes,
            "financial_authority": False,
            "simulation_only": True,
        }

    def _rotate_if_needed(self, body_size: int) -> None:
        active = self.active_path
        if active.exists() or active.is_symlink():
            self._ordinary_file(active)
            if active.stat().st_size + body_size > self.max_segment_bytes:
                target = self.root / (
                    _SEGMENT_PREFIX + f"{self._sequence:020d}" + _SEGMENT_SUFFIX
                )
                if target.exists() or target.is_symlink():
                    raise ValueError("diagnostic rotation target already exists")
                os.replace(active, target)

        rotated = self._rotated_paths()
        while len(rotated) > self.max_segments - 1:
            oldest = rotated.pop(0)
            self._ordinary_file(oldest)
            oldest.unlink()

    def _append(self, record: dict) -> None:
        safe_record = redact_diagnostic_value(record)
        body = _json_line(safe_record)
        if len(body) > self.max_segment_bytes:
            raise ValueError("one diagnostic sample exceeds the segment bound")
        self._rotate_if_needed(len(body))
        active = self.active_path
        if active.exists() or active.is_symlink():
            self._ordinary_file(active)
        with active.open("ab") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        self._sequence = safe_record["sequence"]
        self._previous_digest = sha256(body).hexdigest()

    def _available_paths(self) -> list[Path]:
        paths = self._rotated_paths()
        if self.active_path.exists() or self.active_path.is_symlink():
            paths.append(self.active_path)
        return paths

    def _recover(self) -> None:
        paths = self._available_paths()
        if len(paths) > self.max_segments:
            raise ValueError("diagnostic segment count exceeds configured bound")
        prior_sequence = None
        prior_digest = None
        for path in paths:
            self._ordinary_file(path)
            with path.open("rb") as stream:
                for body in stream:
                    if not body.endswith(b"\n"):
                        raise ValueError("diagnostic segment has an incomplete tail")
                    row = json.loads(body)
                    if type(row) is not dict or type(row.get("sequence")) is not int:
                        raise ValueError("diagnostic sample is invalid")
                    if prior_sequence is not None and (
                        row["sequence"] != prior_sequence + 1
                        or row.get("previous_sha256") != prior_digest
                    ):
                        raise ValueError("diagnostic sequence or hash chain is broken")
                    prior_sequence = row["sequence"]
                    prior_digest = sha256(body).hexdigest()
        if prior_sequence is not None:
            self._sequence = prior_sequence
            self._previous_digest = prior_digest

    def sample_once(self) -> None:
        """Persist one sample; exposed for deterministic qualification tests."""
        with self._write_guard:
            self._append(self._sample())

    def snapshot(self) -> tuple[tuple[str, bytes], ...]:
        """Return one stable, redacted copy of every retained segment."""
        with self._write_guard:
            result = []
            for path in self._available_paths():
                self._ordinary_file(path)
                before = path.stat()
                safe_lines = []
                with path.open("rb") as stream:
                    for line in stream:
                        if not line.endswith(b"\n"):
                            raise ValueError("diagnostic segment has an incomplete tail")
                        safe_lines.append(_json_line(redact_diagnostic_value(json.loads(line))))
                after = path.stat()
                if (
                    before.st_size != after.st_size
                    or before.st_mtime_ns != after.st_mtime_ns
                    or before.st_ino != after.st_ino
                ):
                    raise ValueError("diagnostic segment changed during export")
                result.append((path.name, b"".join(safe_lines)))
            return tuple(result)

    def _run(self) -> None:
        next_tick = float(self._monotonic()) + self.interval_seconds
        while not self._stop.is_set():
            delay = max(0.0, next_tick - float(self._monotonic()))
            if self._stop.wait(delay):
                return
            try:
                self.sample_once()
            except (OSError, TypeError, ValueError) as error:
                self.error = type(error).__name__ + ": " + str(error)
                return
            next_tick += self.interval_seconds
            if next_tick <= float(self._monotonic()):
                next_tick = float(self._monotonic()) + self.interval_seconds

    def start(self) -> None:
        with self._write_guard:
            if self._thread is not None:
                raise RuntimeError("diagnostic sampler is already started")
            self.root.mkdir(parents=True, exist_ok=True)
            if self.root.is_symlink() or not self.root.is_dir():
                raise ValueError("diagnostic root must be an ordinary directory")
            self._recover()
            self._append(self._sample())
            self._stop.clear()
            self._thread = Thread(
                target=self._run,
                name="autotrade-runtime-diagnostics",
                daemon=False,
            )
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=max(2.0, self.interval_seconds + 1.0))
            if thread.is_alive():
                raise RuntimeError("diagnostic sampler could not stop")
