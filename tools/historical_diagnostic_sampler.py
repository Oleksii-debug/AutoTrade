"""Process telemetry isolated from the historical trading and learning cut.

One JSONL sample per active second is best effort: missed intervals, write
errors, and restarts are visible in sequence/time fields. The sampler never
enters financial journals and never supplies observations to a strategy.
"""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
from threading import Event, Lock, Thread
import time


class HistoricalDiagnosticSampler:
    def __init__(self, root: Path, *, interval: float = 1.0,
                 max_segment_bytes: int = 16 * 1024 * 1024,
                 monotonic=time.monotonic, clock=lambda: datetime.now(timezone.utc)):
        if not isinstance(root, Path) or root.is_symlink():
            raise ValueError("diagnostic root must be an ordinary path")
        if not 0.1 <= interval <= 60 or max_segment_bytes < 1024:
            raise ValueError("invalid telemetry cadence or segment size")
        self.root = root
        self.interval = interval
        self.max_segment_bytes = max_segment_bytes
        self._monotonic = monotonic
        self._clock = clock
        self._stop = Event()
        self._guard = Lock()
        self._write_guard = Lock()
        self._status = "STARTING"
        self._month = None
        self._sequence = 0
        self._previous_digest = "0" * 64
        self._prior_cpu = time.process_time()
        self._prior_tick = monotonic()
        self._thread: Thread | None = None
        self.error: str | None = None

    def update_phase(self, status: str, month: str | None) -> None:
        with self._guard:
            self._status = status if status in {
                "STARTING", "RUNNING", "WAITING_FOR_COMPLETED_UTC_MONTH",
                "WAITING_FOR_VERIFIED_PUBLIC_ARCHIVE", "CUT_COMPLETED", "STOPPING",
            } else "RUNNING"
            self._month = month if isinstance(month, str) and len(month) == 7 else None

    def _memory_bytes(self) -> int | None:
        try:
            if os.name == "posix" and Path("/proc/self/statm").is_file():
                return int(Path("/proc/self/statm").read_text().split()[1]) * os.sysconf("SC_PAGE_SIZE")
            if os.name == "nt":
                import ctypes
                from ctypes import wintypes
                class Counters(ctypes.Structure):
                    _fields_ = [("cb", wintypes.DWORD), ("pageFaultCount", wintypes.DWORD),
                                ("peakWorkingSetSize", ctypes.c_size_t),
                                ("workingSetSize", ctypes.c_size_t),
                                ("quotaPeakPagedPoolUsage", ctypes.c_size_t),
                                ("quotaPagedPoolUsage", ctypes.c_size_t),
                                ("quotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                                ("quotaNonPagedPoolUsage", ctypes.c_size_t),
                                ("pagefileUsage", ctypes.c_size_t),
                                ("peakPagefileUsage", ctypes.c_size_t)]
                counters = Counters()
                counters.cb = ctypes.sizeof(counters)
                if ctypes.windll.psapi.GetProcessMemoryInfo(
                    ctypes.windll.kernel32.GetCurrentProcess(),
                    ctypes.byref(counters), counters.cb):
                    return int(counters.workingSetSize)
        except (OSError, ValueError, AttributeError):
            pass
        return None

    def _sample(self) -> dict:
        now = self._clock()
        tick = self._monotonic()
        cpu = time.process_time()
        elapsed = max(tick - self._prior_tick, 0.000001)
        cpu_percent = min(100.0 * (cpu - self._prior_cpu) / elapsed, 100.0 * (os.cpu_count() or 1))
        self._prior_tick, self._prior_cpu = tick, cpu
        with self._guard:
            phase, month = self._status, self._month
        free = shutil.disk_usage(self.root).free
        return {"schema_version": "1.0.0", "recorded_at_utc": now.isoformat(),
                "monotonic_tick": tick, "sequence": self._sequence + 1,
                "process_cpu_percent": round(cpu_percent, 2),
                "process_memory_bytes": self._memory_bytes(), "disk_free_bytes": free,
                "supervisor_phase": phase, "next_holdout_month": month,
                "agent_phase": "HISTORICAL_SUPERVISOR", "market_stream": "VERIFIED_MONTHLY_ARCHIVE",
                "api_status": "NOT_CONFIGURED", "queue_depth": None,
                "last_observation_at": None, "last_decision_at": None,
                "last_trade_at": None, "last_learning_update_at": None,
                "previous_sha256": self._previous_digest}

    def _append(self, record: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / "runtime-diagnostics.jsonl"
        if path.is_symlink() or not path.parent.is_dir():
            raise ValueError("diagnostic journal path redirected")
        if path.exists() and path.stat().st_size >= self.max_segment_bytes:
            target = self.root / ("runtime-diagnostics-" + self._clock().strftime("%Y%m%dT%H%M%S")
                                  + "-" + str(time.time_ns()) + ".jsonl")
            os.replace(path, target)
        body = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        with path.open("ab") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        self._sequence = record["sequence"]
        self._previous_digest = sha256(body).hexdigest()

    def _recover_chain(self) -> None:
        expected = 1
        previous = "0" * 64
        segments = sorted(self.root.glob("runtime-diagnostics-*.jsonl"))
        active = self.root / "runtime-diagnostics.jsonl"
        if active.exists() or active.is_symlink():
            segments.append(active)
        for path in segments:
            if path.is_symlink() or not path.is_file():
                raise ValueError("diagnostic segment redirected")
            with path.open("rb") as stream:
                for body in stream:
                    if not body.endswith(b"\n"):
                        raise ValueError("diagnostic segment has an incomplete tail")
                    row = json.loads(body)
                    if (type(row) is not dict or row.get("sequence") != expected
                            or row.get("previous_sha256") != previous):
                        raise ValueError("diagnostic sequence or hash chain broken")
                    previous = sha256(body).hexdigest()
                    expected += 1
        self._sequence = expected - 1
        self._previous_digest = previous

    def sample_once(self) -> None:
        """Take a sample; also usable by focused deterministic tests."""
        with self._write_guard:
            self._append(self._sample())

    def _run(self) -> None:
        try:
            self.sample_once()
            next_tick = self._monotonic() + self.interval
            while not self._stop.is_set():
                if self._stop.wait(max(0, next_tick - self._monotonic())):
                    break
                self.sample_once()
                next_tick += self.interval
                if next_tick <= self._monotonic():
                    next_tick = self._monotonic() + self.interval
        except (OSError, ValueError, TypeError) as exc:
            # Telemetry must not alter a trading decision. Supervisor can
            # inspect the error separately; data after this point is absent.
            self.error = type(exc).__name__ + ": " + str(exc)

    def __enter__(self):
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            self._recover_chain()
        except (OSError, ValueError, TypeError) as exc:
            self.error = type(exc).__name__ + ": " + str(exc)
            return self
        self._thread = Thread(target=self._run, name="autotrade-diagnostics", daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_exc):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(2.0, self.interval + 1.0))
        if self._thread is not None and self._thread.is_alive():
            raise RuntimeError("diagnostic sampler could not stop")
