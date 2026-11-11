"""Durable, secret-free supervisor for AutoTrade background processes.

The watchdog deliberately accepts only repository-relative Python scripts and a
closed JSON manifest.  Credentials are resolved by the child at runtime from
the secure credential store; they are never accepted in the manifest, command
line, state file, or diagnostic log.
"""
from __future__ import annotations

import argparse
from collections import deque
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[1]
for dependency_root in (ROOT, ROOT / "research", ROOT / "mvp", ROOT / "src"):
    if str(dependency_root) not in sys.path:
        sys.path.insert(0, str(dependency_root))

from research.autotrade_research.artifacts.resource_lock import (
    ResourceLock,
    ResourceLockBusyError,
)


MANIFEST_SCHEMA = "autotrade-process-watchdog-manifest-v1"
STATE_SCHEMA = "autotrade-process-watchdog-state-v1"
_SENSITIVE_OPTIONS = frozenset({
    "--access-token", "--api-key", "--apikey", "--authorization",
    "--client-secret", "--credential", "--key", "--password",
    "--private-key", "--secret", "--token",
})
_SAFE_ID = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-_")
_ALLOWED_SCRIPTS = frozenset({
    "tools/run_learning_fleet_diagnostics.py",
    "tools/run_goal_directed_attention.py",
    "tools/run_groq_advisory_supervisor.py",
    "tools/run_whitebit_trend_shadow.py",
    "tools/run_whitebit_live_zero_learning.py",
    "tools/run_whitebit_public_supervisor.py",
})


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")


def _atomic(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".watchdog-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(_canonical(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _ordinary_directory(path: Path, label: str) -> Path:
    resolved = path.resolve(strict=False)
    if path.exists() and path.is_symlink():
        raise ValueError(f"{label} must not be redirected")
    resolved.mkdir(parents=True, exist_ok=True)
    if resolved.is_symlink() or not resolved.is_dir():
        raise ValueError(f"{label} must be an ordinary directory")
    return resolved


def _within(root: Path, candidate: Path, label: str) -> Path:
    # Windows can hand TemporaryDirectory (and other callers) an 8.3 root
    # while resolving a descendant expands that same root to its long form.
    # Compare two equally canonical paths so the containment gate does not
    # reject an identical authority root solely because of that spelling.
    canonical_root = root.resolve(strict=False)
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(canonical_root)
    except ValueError as exc:
        raise ValueError(f"{label} escapes its authority root") from exc
    return resolved


def _expand(value: str, *, repo_root: Path, runtime_root: Path,
            state_dir: Path) -> str:
    return (value.replace("${REPO_ROOT}", str(repo_root))
                 .replace("${RUNTIME_ROOT}", str(runtime_root))
                 .replace("${WATCHDOG_STATE}", str(state_dir)))


def load_manifest(path: Path, *, repo_root: Path, runtime_root: Path,
                  state_dir: Path) -> tuple[dict[str, Any], str]:
    """Load and validate the closed process manifest."""
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 256_000:
        raise ValueError("watchdog manifest is missing, redirected, or unbounded")
    raw = path.read_bytes()
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("watchdog manifest is malformed") from exc
    if type(document) is not dict or set(document) != {"schema", "processes"}:
        raise ValueError("watchdog manifest has unsupported fields")
    if document["schema"] != MANIFEST_SCHEMA or type(document["processes"]) is not list:
        raise ValueError("watchdog manifest schema is unsupported")
    if not 1 <= len(document["processes"]) <= 32:
        raise ValueError("watchdog manifest process count is invalid")
    admitted: list[dict[str, Any]] = []
    identifiers: set[str] = set()
    for row in document["processes"]:
        required = {
            "id", "script", "args", "stop_file", "checkpoint",
            "restart_limit", "restart_window_seconds", "backoff_seconds",
        }
        if type(row) is not dict or set(row) != required:
            raise ValueError("watchdog process has unsupported fields")
        identifier = row["id"]
        if (type(identifier) is not str or not 1 <= len(identifier) <= 64
                or any(character not in _SAFE_ID for character in identifier)):
            raise ValueError("watchdog process id is invalid")
        if identifier in identifiers:
            raise ValueError("watchdog process id is duplicated")
        identifiers.add(identifier)
        script_text = row["script"]
        if type(script_text) is not str or Path(script_text).is_absolute():
            raise ValueError("watchdog script must be repository-relative")
        if Path(script_text).as_posix() not in _ALLOWED_SCRIPTS:
            raise ValueError("watchdog script is outside the admitted runtime set")
        script = _within(repo_root, repo_root / script_text, "watchdog script")
        if script.suffix.lower() != ".py" or not script.is_file() or script.is_symlink():
            raise ValueError("watchdog script must be an ordinary Python file")
        args = row["args"]
        if (type(args) is not list or len(args) > 64
                or any(type(argument) is not str or len(argument) > 4096 for argument in args)):
            raise ValueError("watchdog process arguments are invalid")
        for index, argument in enumerate(args):
            option = argument.split("=", 1)[0].lower()
            if option in _SENSITIVE_OPTIONS:
                raise ValueError("secrets are forbidden in watchdog arguments")
            if index and args[index - 1].split("=", 1)[0].lower() in _SENSITIVE_OPTIONS:
                raise ValueError("secrets are forbidden in watchdog arguments")
            lowered = argument.lower()
            if lowered.startswith(("gsk_", "sk-")) or "bearer " in lowered:
                raise ValueError("secret-like values are forbidden in watchdog arguments")
            expanded = _expand(argument, repo_root=repo_root,
                               runtime_root=runtime_root, state_dir=state_dir)
            if "${" in expanded:
                raise ValueError("watchdog argument contains an unknown placeholder")
        if any(type(row[name]) is not str or not row[name]
               for name in ("stop_file", "checkpoint")):
            raise ValueError("watchdog state paths are invalid")
        stop_file = _within(runtime_root, Path(_expand(
            row["stop_file"], repo_root=repo_root, runtime_root=runtime_root,
            state_dir=state_dir)), "watchdog stop file")
        checkpoint = _within(runtime_root, Path(_expand(
            row["checkpoint"], repo_root=repo_root, runtime_root=runtime_root,
            state_dir=state_dir)), "watchdog checkpoint")
        for name in ("restart_limit", "restart_window_seconds"):
            if type(row[name]) is not int or row[name] < 1:
                raise ValueError(f"watchdog {name} is invalid")
        backoff = row["backoff_seconds"]
        if isinstance(backoff, bool) or not isinstance(backoff, (int, float)) or not 0 <= backoff <= 300:
            raise ValueError("watchdog backoff is invalid")
        admitted.append({
            **row,
            "script": str(script),
            "args": [_expand(argument, repo_root=repo_root, runtime_root=runtime_root,
                             state_dir=state_dir) for argument in args],
            "stop_file": str(stop_file),
            "checkpoint": str(checkpoint),
        })
    return {"schema": MANIFEST_SCHEMA, "processes": admitted}, sha256(raw).hexdigest()


class DiagnosticLog:
    def __init__(self, path: Path, maximum_bytes: int = 1_000_000,
                 archives: int = 4) -> None:
        self.path = path
        self.maximum_bytes = maximum_bytes
        self.archives = archives

    def write(self, event: str, **fields: object) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and self.path.stat().st_size >= self.maximum_bytes:
            oldest = self.path.with_suffix(f".jsonl.{self.archives}")
            oldest.unlink(missing_ok=True)
            for index in range(self.archives - 1, 0, -1):
                source = self.path.with_suffix(f".jsonl.{index}")
                if source.exists():
                    os.replace(source, self.path.with_suffix(f".jsonl.{index + 1}"))
            os.replace(self.path, self.path.with_suffix(".jsonl.1"))
        record = {"timestamp": _utc(), "event": event, **fields}
        with self.path.open("ab") as stream:
            stream.write(_canonical(record))
            stream.flush()


class ProcessWatchdog:
    def __init__(self, manifest: dict[str, Any], *, manifest_digest: str,
                 state_dir: Path, popen: Callable[..., subprocess.Popen] = subprocess.Popen,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        self.manifest = manifest
        self.manifest_digest = manifest_digest
        self.state_dir = _ordinary_directory(state_dir, "watchdog state directory")
        self.popen = popen
        self.monotonic = monotonic
        self.children: dict[str, subprocess.Popen] = {}
        self.streams: dict[str, tuple[Any, Any]] = {}
        self.restarts: dict[str, deque[float]] = {
            row["id"]: deque() for row in manifest["processes"]
        }
        self.started_at: dict[str, str] = {}
        self.phase = "STARTING"
        self.stop_requested = False
        self.blocked: dict[str, int] = {}
        self.log = DiagnosticLog(self.state_dir / "watchdog-diagnostics.jsonl")

    def _publish(self) -> None:
        rows = []
        for spec in self.manifest["processes"]:
            identifier = spec["id"]
            process = self.children.get(identifier)
            rows.append({
                "id": identifier,
                "pid": None if process is None else process.pid,
                "status": ("BLOCKED" if identifier in self.blocked else
                           "STOPPED" if self.phase == "STOPPED" else
                           "STOPPED" if process is None else
                           "RUNNING" if process.poll() is None else "EXITED"),
                "started_at": self.started_at.get(identifier),
                "restart_count_in_window": len(self.restarts[identifier]),
                "checkpoint": spec["checkpoint"],
                "checkpoint_present": Path(spec["checkpoint"]).is_file(),
                "stdout_log": str(self.state_dir / "logs" / f"{identifier}.stdout.log"),
                "stderr_log": str(self.state_dir / "logs" / f"{identifier}.stderr.log"),
            })
        _atomic(self.state_dir / "watchdog-state.json", {
            "schema": STATE_SCHEMA,
            "phase": self.phase,
            "updated_at": _utc(),
            "manifest_sha256": self.manifest_digest,
            "processes": rows,
            "secrets_in_state": False,
            "network_orders": False,
            "trading_authority": False,
        })

    def _start(self, spec: dict[str, Any], *, restart: bool) -> None:
        identifier = spec["id"]
        Path(spec["stop_file"]).unlink(missing_ok=True)
        repo_root = Path(spec["script"]).parents[1]
        child_environment = os.environ.copy()
        import_roots = [
            str(repo_root),
            str(repo_root / "research"),
            str(repo_root / "mvp"),
            str(repo_root / "src"),
        ]
        inherited_pythonpath = child_environment.get("PYTHONPATH")
        if inherited_pythonpath:
            import_roots.append(inherited_pythonpath)
        child_environment["PYTHONPATH"] = os.pathsep.join(import_roots)
        logs = self.state_dir / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        stdout = (logs / f"{identifier}.stdout.log").open("ab", buffering=0)
        stderr = (logs / f"{identifier}.stderr.log").open("ab", buffering=0)
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        try:
            process = self.popen(
                [sys.executable, "-u", spec["script"], *spec["args"]],
                cwd=str(repo_root),
                env=child_environment,
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                close_fds=True,
                creationflags=flags,
            )
        except BaseException:
            stdout.close()
            stderr.close()
            raise
        self.children[identifier] = process
        self.streams[identifier] = (stdout, stderr)
        self.started_at[identifier] = _utc()
        self.log.write("PROCESS_RESTARTED" if restart else "PROCESS_STARTED",
                       process_id=identifier, pid=process.pid)

    def start_all(self) -> None:
        for spec in self.manifest["processes"]:
            self._start(spec, restart=False)
        self.phase = "RUNNING"
        self._publish()
        self.log.write("WATCHDOG_RUNNING", process_count=len(self.children))

    def check(self) -> None:
        now = self.monotonic()
        for spec in self.manifest["processes"]:
            identifier = spec["id"]
            process = self.children.get(identifier)
            if process is None or process.poll() is None or identifier in self.blocked:
                continue
            exit_code = process.returncode
            for stream in self.streams.pop(identifier, ()):
                stream.close()
            history = self.restarts[identifier]
            while history and now - history[0] > spec["restart_window_seconds"]:
                history.popleft()
            if len(history) >= spec["restart_limit"]:
                self.blocked[identifier] = exit_code
                self.phase = "DEGRADED"
                self.log.write("PROCESS_CIRCUIT_OPEN", process_id=identifier,
                               exit_code=exit_code)
                continue
            history.append(now)
            self.log.write("PROCESS_EXITED", process_id=identifier, exit_code=exit_code)
            if spec["backoff_seconds"]:
                time.sleep(float(spec["backoff_seconds"]))
            self._start(spec, restart=True)
        self._publish()

    def stop_all(self, grace_seconds: float = 30.0) -> None:
        self.phase = "STOPPING"
        self.stop_requested = True
        for spec in self.manifest["processes"]:
            if spec["id"] in self.children and self.children[spec["id"]].poll() is None:
                _atomic(Path(spec["stop_file"]), {
                    "schema": "autotrade-managed-stop-v1", "requested_at": _utc(),
                    "reason": "watchdog shutdown",
                })
        deadline = self.monotonic() + grace_seconds
        while self.monotonic() < deadline:
            if all(process.poll() is not None for process in self.children.values()):
                break
            time.sleep(0.1)
        for identifier, process in self.children.items():
            if process.poll() is None:
                process.terminate()
                self.log.write("PROCESS_FORCE_TERMINATED", process_id=identifier)
        for process in self.children.values():
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
        for streams in self.streams.values():
            for stream in streams:
                stream.close()
        self.phase = "STOPPED"
        self._publish()
        self.log.write("WATCHDOG_STOPPED")

    def run(self, *, poll_seconds: float = 1.0, stop_file: Path | None = None) -> int:
        try:
            self.start_all()
            while not self.stop_requested:
                if stop_file is not None and stop_file.exists():
                    break
                self.check()
                time.sleep(poll_seconds)
        finally:
            if self.children:
                self.stop_all()
        return 2 if self.blocked else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--runtime-root", required=True)
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    args = parser.parse_args(argv)
    repo_root = Path(__file__).resolve().parents[1]
    runtime_root = _ordinary_directory(Path(args.runtime_root), "runtime root")
    state_dir = _ordinary_directory(Path(args.state_dir), "watchdog state directory")
    if not 0.1 <= args.poll_seconds <= 60:
        raise SystemExit("poll-seconds must be 0.1..60")
    manifest, digest = load_manifest(Path(args.manifest), repo_root=repo_root,
                                     runtime_root=runtime_root, state_dir=state_dir)
    stop_file = state_dir / "watchdog.stop.json"
    try:
        with ResourceLock(state_dir / "watchdog.lock"):
            # Only the lock owner may clear a previous terminal stop request.
            # A duplicate startup must not consume the request intended for
            # the process that already owns the runtime.
            stop_file.unlink(missing_ok=True)
            watchdog = ProcessWatchdog(
                manifest, manifest_digest=digest, state_dir=state_dir
            )
            previous: dict[signal.Signals, Any] = {}
            for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
                signum = getattr(signal, name, None)
                if signum is not None:
                    previous[signum] = signal.getsignal(signum)
                    signal.signal(
                        signum,
                        lambda *_args: setattr(watchdog, "stop_requested", True),
                    )
            try:
                return watchdog.run(
                    poll_seconds=args.poll_seconds, stop_file=stop_file
                )
            finally:
                for signum, handler in previous.items():
                    signal.signal(signum, handler)
    except ResourceLockBusyError:
        print(json.dumps({
            "schema": STATE_SCHEMA,
            "phase": "ALREADY_RUNNING",
            "state_dir": str(state_dir),
            "network_orders": False,
            "trading_authority": False,
        }, sort_keys=True), flush=True)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
