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
import stat
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
_CANONICAL_SOL_STATE = "${RUNTIME_ROOT}/whitebit-public-sol-v1-20261111"
_CANONICAL_SOL_STOP = _CANONICAL_SOL_STATE + "/managed.stop.json"
_CANONICAL_SOL_CHECKPOINT = _CANONICAL_SOL_STATE + "/supervisor-state.json"
_ALLOWED_SCRIPTS = frozenset({
    "tools/run_learning_fleet_diagnostics.py",
    "tools/run_goal_directed_attention.py",
    "tools/run_groq_advisory_supervisor.py",
    "tools/run_whitebit_trend_shadow.py",
    "tools/run_whitebit_rolling_alpha_supervisor.py",
    "tools/run_whitebit_opportunity_supervisor.py",
    "tools/run_whitebit_funding_carry_shadow.py",
    "tools/run_whitebit_course_supervisor.py",
    "tools/run_whitebit_flow_research_supervisor.py",
    "tools/run_whitebit_ohlcv_forward_shadow.py",
    "tools/run_whitebit_live_zero_learning.py",
    "tools/run_whitebit_maker_shadow.py",
    "tools/run_whitebit_maker_universe_supervisor.py",
    "tools/run_whitebit_public_supervisor.py",
    "tools/run_whitebit_public_trade_tape.py",
    "tools/run_whitebit_depth_tape.py",
    "tools/run_whitebit_depth_ofi_supervisor.py",
    "tools/run_whitebit_subhour_supervisor.py",
    "tools/run_whitebit_subhour_forward_shadow.py",
    "tools/run_whitebit_trade_flow_supervisor.py",
    "tools/run_whitebit_triangular_shadow.py",
})


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _child_launch() -> tuple[str, int]:
    """Return a detached child interpreter and process creation flags.

    ``pythonw.exe`` deliberately has no Python-level standard streams.  The
    watchdog is installed with pythonw to stay hidden, but its managed workers
    must use the sibling console interpreter because their stdout/stderr are
    durable diagnostic files.
    """
    executable = sys.executable
    if os.name != "nt":
        return executable, 0
    path = Path(executable)
    if path.name.casefold() == "pythonw.exe":
        console = path.with_name("python.exe")
        if not console.is_file():
            raise RuntimeError("watchdog console Python interpreter is unavailable")
        executable = str(console)
    return executable, (
        subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    )


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
        # Windows virus scanners and indexers can briefly open the destination
        # without delete sharing.  A single transient sharing violation must
        # not terminate the owner process and stop every managed worker.
        for attempt in range(8):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                if attempt == 7:
                    raise
                time.sleep(0.025 * (attempt + 1))
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


def _existing_ordinary_directory(path: Path, label: str) -> Path:
    """Admit an existing directory without following redirected ancestors."""
    lexical = path.absolute()
    current = Path(lexical.anchor)
    for component in lexical.parts[1:]:
        current /= component
        is_junction = getattr(current, "is_junction", lambda: False)
        if current.is_symlink() or is_junction():
            raise ValueError(f"{label} must not be redirected")
    if not lexical.exists() or not lexical.is_dir():
        raise ValueError(f"{label} must be an existing ordinary directory")
    return lexical.resolve(strict=True)


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


def _argument_value(arguments: list[str], option: str) -> str:
    values: list[str] = []
    for index, argument in enumerate(arguments):
        if argument == option:
            if index + 1 >= len(arguments) or arguments[index + 1].startswith("--"):
                raise ValueError(f"SOL secondary state requires a value for {option}")
            values.append(arguments[index + 1])
        elif argument.startswith(option + "="):
            values.append(argument.split("=", 1)[1])
    if len(values) != 1 or not values[0]:
        raise ValueError(f"SOL secondary state requires exactly one {option}")
    return values[0]


def _replace_argument_value(
    arguments: list[str], option: str, replacement: str
) -> list[str]:
    rewritten = list(arguments)
    for index, argument in enumerate(rewritten):
        if argument == option:
            rewritten[index + 1] = replacement
        elif argument.startswith(option + "="):
            rewritten[index] = option + "=" + replacement
    return rewritten


def _rewrite_canonical_sol_resources(
    row: dict[str, Any], arguments: list[str], secondary_root: Path
) -> tuple[list[str], str, str]:
    """Redirect only the complete checked-in SOL resource tuple."""
    bindings = (
        _argument_value(arguments, "--state-dir") == _CANONICAL_SOL_STATE,
        _argument_value(arguments, "--stop-file") == _CANONICAL_SOL_STOP,
        row["stop_file"] == _CANONICAL_SOL_STOP,
        row["checkpoint"] == _CANONICAL_SOL_CHECKPOINT,
    )
    if not all(bindings):
        raise ValueError(
            "SOL secondary state requires exact canonical resource paths in all fields"
        )
    state = str(secondary_root)
    stop = str(secondary_root / "managed.stop.json")
    checkpoint = str(secondary_root / "supervisor-state.json")
    rewritten = _replace_argument_value(arguments, "--state-dir", state)
    rewritten = _replace_argument_value(rewritten, "--stop-file", stop)
    return rewritten, stop, checkpoint


def _validate_sol_secondary_binding(
    row: dict[str, Any],
    *,
    arguments: list[str],
    stop_file: Path,
    checkpoint: Path,
    secondary_root: Path,
) -> None:
    if row["script"] != "tools/run_whitebit_public_supervisor.py":
        raise ValueError("SOL secondary state requires the public SOL supervisor")
    if _argument_value(arguments, "--market") != "SOL_USDT":
        raise ValueError("SOL secondary state is restricted to SOL_USDT")
    state_argument = Path(_argument_value(arguments, "--state-dir")).resolve(
        strict=False
    )
    stop_argument = Path(_argument_value(arguments, "--stop-file")).resolve(
        strict=False
    )
    expected_stop = secondary_root / "managed.stop.json"
    expected_checkpoint = secondary_root / "supervisor-state.json"
    if state_argument != secondary_root:
        raise ValueError("SOL --state-dir does not bind the admitted secondary root")
    if stop_argument != expected_stop or stop_file != expected_stop:
        raise ValueError("SOL --stop-file does not bind the admitted secondary root")
    if checkpoint != expected_checkpoint:
        raise ValueError("SOL checkpoint does not bind the actual secondary checkpoint")
    if checkpoint.is_symlink() or not checkpoint.is_file():
        raise ValueError("SOL secondary state requires its actual checkpoint file")


def _disjoint_secondary_root(runtime_root: Path, candidate: Path) -> Path:
    secondary = _existing_ordinary_directory(
        candidate, "SOL secondary state root"
    )
    runtime = runtime_root.resolve(strict=False)
    if secondary == runtime:
        raise ValueError("SOL secondary state root must be distinct from runtime root")
    try:
        secondary.relative_to(runtime)
    except ValueError:
        pass
    else:
        raise ValueError("SOL secondary state root must be disjoint from runtime root")
    try:
        runtime.relative_to(secondary)
    except ValueError:
        pass
    else:
        raise ValueError("SOL secondary state root must be disjoint from runtime root")
    return secondary


def load_manifest(path: Path, *, repo_root: Path, runtime_root: Path,
                  state_dir: Path,
                  sol_secondary_state_root: Path | None = None,
                  ) -> tuple[dict[str, Any], str]:
    """Load and validate the closed process manifest."""
    secondary_root = (
        None
        if sol_secondary_state_root is None
        else _disjoint_secondary_root(runtime_root, sol_secondary_state_root)
    )
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
    secondary_sol_admitted = False
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
        if any(type(row[name]) is not str or not row[name]
               for name in ("stop_file", "checkpoint")):
            raise ValueError("watchdog state paths are invalid")
        effective_args = list(args)
        effective_stop = row["stop_file"]
        effective_checkpoint = row["checkpoint"]
        if identifier == "whitebit-public-sol" and secondary_root is not None:
            effective_args, effective_stop, effective_checkpoint = (
                _rewrite_canonical_sol_resources(row, args, secondary_root)
            )
        expanded_args = [
            _expand(
                argument,
                repo_root=repo_root,
                runtime_root=runtime_root,
                state_dir=state_dir,
            )
            for argument in effective_args
        ]
        for index, (argument, expanded) in enumerate(
            zip(effective_args, expanded_args, strict=True)
        ):
            option = argument.split("=", 1)[0].lower()
            if option in _SENSITIVE_OPTIONS:
                raise ValueError("secrets are forbidden in watchdog arguments")
            if (
                index
                and effective_args[index - 1].split("=", 1)[0].lower()
                in _SENSITIVE_OPTIONS
            ):
                raise ValueError("secrets are forbidden in watchdog arguments")
            lowered = argument.lower()
            if lowered.startswith(("gsk_", "sk-")) or "bearer " in lowered:
                raise ValueError("secret-like values are forbidden in watchdog arguments")
            if "${" in expanded:
                raise ValueError("watchdog argument contains an unknown placeholder")
        expanded_stop = Path(_expand(
            effective_stop, repo_root=repo_root, runtime_root=runtime_root,
            state_dir=state_dir))
        expanded_checkpoint = Path(_expand(
            effective_checkpoint, repo_root=repo_root, runtime_root=runtime_root,
            state_dir=state_dir))
        if "${" in str(expanded_stop) or "${" in str(expanded_checkpoint):
            raise ValueError("watchdog state path contains an unknown placeholder")
        if identifier == "whitebit-public-sol" and secondary_root is not None:
            for candidate, label in (
                (expanded_stop, "SOL secondary stop file"),
                (expanded_checkpoint, "SOL secondary checkpoint"),
            ):
                try:
                    raw_info = candidate.lstat()
                except FileNotFoundError:
                    continue
                attributes = getattr(raw_info, "st_file_attributes", 0)
                if candidate.is_symlink() or attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                    raise ValueError(f"{label} must not be a reparse point")
            stop_file = _within(
                secondary_root, expanded_stop, "SOL secondary stop file"
            )
            checkpoint = _within(
                secondary_root, expanded_checkpoint, "SOL secondary checkpoint"
            )
            _validate_sol_secondary_binding(
                row,
                arguments=expanded_args,
                stop_file=stop_file,
                checkpoint=checkpoint,
                secondary_root=secondary_root,
            )
            secondary_sol_admitted = True
        else:
            stop_file = _within(
                runtime_root, expanded_stop, "watchdog stop file"
            )
            checkpoint = _within(
                runtime_root, expanded_checkpoint, "watchdog checkpoint"
            )
        for name in ("restart_limit", "restart_window_seconds"):
            if type(row[name]) is not int or row[name] < 1:
                raise ValueError(f"watchdog {name} is invalid")
        backoff = row["backoff_seconds"]
        if isinstance(backoff, bool) or not isinstance(backoff, (int, float)) or not 0 <= backoff <= 300:
            raise ValueError("watchdog backoff is invalid")
        admitted.append({
            **row,
            "script": str(script),
            "args": expanded_args,
            "stop_file": str(stop_file),
            "checkpoint": str(checkpoint),
        })
    if secondary_root is not None and not secondary_sol_admitted:
        raise ValueError("SOL secondary state root requires a SOL worker")
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
        try:
            executable, flags = _child_launch()
            process = self.popen(
                [executable, "-u", spec["script"], *spec["args"]],
                cwd=str(repo_root),
                env=child_environment,
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                close_fds=True,
                creationflags=flags,
            )
        except BaseException as error:
            stdout.close()
            stderr.close()
            try:
                self.log.write(
                    "PROCESS_START_FAILED",
                    process_id=identifier,
                    error_type=type(error).__name__,
                )
            except OSError:
                pass
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
    parser.add_argument(
        "--sol-secondary-state-root",
        help="Existing ordinary state root used only by whitebit-public-sol",
    )
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    parser.add_argument("--respect-stop-marker", action="store_true",
                        help="For automatic startup, preserve an intentional watchdog.stop.json")
    args = parser.parse_args(argv)
    repo_root = Path(__file__).resolve().parents[1]
    runtime_root = _ordinary_directory(Path(args.runtime_root), "runtime root")
    state_dir = _ordinary_directory(Path(args.state_dir), "watchdog state directory")
    if not 0.1 <= args.poll_seconds <= 60:
        raise SystemExit("poll-seconds must be 0.1..60")
    manifest, digest = load_manifest(Path(args.manifest), repo_root=repo_root,
                                     runtime_root=runtime_root, state_dir=state_dir,
                                     sol_secondary_state_root=(
                                         None
                                         if args.sol_secondary_state_root is None
                                         else Path(args.sol_secondary_state_root)
                                     ))
    stop_file = state_dir / "watchdog.stop.json"
    try:
        with ResourceLock(state_dir / "watchdog.lock"):
            # Decide under the same lock that protects explicit restarts.
            if args.respect_stop_marker:
                if stop_file.exists():
                    print(json.dumps({"schema": STATE_SCHEMA, "phase": "STOP_REQUESTED",
                                      "network_orders": False, "trading_authority": False},
                                     sort_keys=True), flush=True)
                    return 0
            else:
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





