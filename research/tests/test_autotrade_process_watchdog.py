from __future__ import annotations

import json
import io
import os
import subprocess
from pathlib import Path
from contextlib import redirect_stdout
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from tools.run_autotrade_process_watchdog import (
    DiagnosticLog,
    MANIFEST_SCHEMA,
    ProcessWatchdog,
    _atomic,
    _child_launch,
    load_manifest,
    main,
)
from research.autotrade_research.artifacts.resource_lock import ResourceLock


REPO = Path(__file__).resolve().parents[2]


def test_atomic_state_publish_retries_transient_windows_sharing_violation(tmp_path):
    target = tmp_path / "state.json"
    real_replace = os.replace
    calls = 0

    def transient(source, destination):
        nonlocal calls
        calls += 1
        if calls < 3:
            raise PermissionError("temporary Windows sharing violation")
        return real_replace(source, destination)

    with patch("tools.run_autotrade_process_watchdog.os.replace", side_effect=transient), \
            patch("tools.run_autotrade_process_watchdog.time.sleep") as sleep:
        _atomic(target, {"phase": "RUNNING"})

    assert calls == 3
    assert json.loads(target.read_text(encoding="utf-8")) == {"phase": "RUNNING"}
    assert sleep.call_count == 2


def _row(*, restart_limit: int = 2, args: list[str] | None = None) -> dict:
    return {
        "id": "attention",
        "script": "tools/run_goal_directed_attention.py",
        "args": [] if args is None else args,
        "stop_file": "${RUNTIME_ROOT}/attention/managed.stop.json",
        "checkpoint": "${RUNTIME_ROOT}/attention/latest.json",
        "restart_limit": restart_limit,
        "restart_window_seconds": 60,
        "backoff_seconds": 0,
    }


def _write_manifest(path: Path, row: dict) -> None:
    path.write_text(json.dumps({"schema": MANIFEST_SCHEMA, "processes": [row]}),
                    encoding="utf-8")


class FakeProcess:
    next_pid = 100

    def __init__(self) -> None:
        self.pid = FakeProcess.next_pid
        FakeProcess.next_pid += 1
        self.returncode = None

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = -15

    def kill(self):
        self.returncode = -9

    def wait(self, timeout=None):
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


class FakeFactory:
    def __init__(self) -> None:
        self.calls = []
        self.processes = []

    def __call__(self, command, **kwargs):
        process = FakeProcess()
        self.calls.append((command, kwargs))
        self.processes.append(process)
        return process


def _load(tmp_path: Path, row: dict):
    runtime = tmp_path / "runtime"
    state = tmp_path / "watchdog"
    runtime.mkdir()
    state.mkdir()
    manifest_path = tmp_path / "manifest.json"
    _write_manifest(manifest_path, row)
    manifest, digest = load_manifest(
        manifest_path, repo_root=REPO, runtime_root=runtime, state_dir=state
    )
    return runtime, state, manifest, digest


def test_manifest_expands_only_bounded_paths_and_rejects_secrets(tmp_path):
    runtime, _state, manifest, digest = _load(
        tmp_path, _row(args=["--poll-seconds", "15"])
    )
    assert len(digest) == 64
    assert Path(manifest["processes"][0]["stop_file"]).is_relative_to(
        runtime.resolve(strict=False)
    )

    bad = tmp_path / "bad.json"
    _write_manifest(bad, _row(args=["--api-key", "must-not-appear"]))
    with unittest.TestCase().assertRaisesRegex(ValueError, "secrets are forbidden"):
        load_manifest(bad, repo_root=REPO, runtime_root=runtime,
                      state_dir=tmp_path / "watchdog")


def test_watchdog_restarts_then_opens_bounded_crash_circuit(tmp_path):
    _runtime, state, manifest, digest = _load(tmp_path, _row(restart_limit=1))
    factory = FakeFactory()
    watchdog = ProcessWatchdog(
        manifest, manifest_digest=digest, state_dir=state, popen=factory,
        monotonic=lambda: 100.0,
    )
    watchdog.start_all()
    child_environment = factory.calls[0][1]["env"]
    pythonpath = child_environment["PYTHONPATH"].split(os.pathsep)
    assert str(REPO) in pythonpath
    assert str(REPO / "research") in pythonpath
    assert str(REPO / "mvp") in pythonpath
    assert str(REPO / "src") in pythonpath
    factory.processes[0].returncode = 7
    watchdog.check()
    assert len(factory.processes) == 2
    factory.processes[1].returncode = 8
    watchdog.check()
    status = json.loads((state / "watchdog-state.json").read_text(encoding="utf-8"))
    assert status["phase"] == "DEGRADED"
    assert status["processes"][0]["status"] == "BLOCKED"
    assert status["secrets_in_state"] is False
    assert status["network_orders"] is False


@unittest.skipUnless(os.name == "nt", "Windows process creation contract")
def test_windows_children_are_console_isolated_and_pythonw_uses_python(tmp_path):
    pythonw = tmp_path / "pythonw.exe"
    python = tmp_path / "python.exe"
    pythonw.write_bytes(b"")
    python.write_bytes(b"")

    _runtime, state, manifest, digest = _load(tmp_path, _row())
    factory = FakeFactory()
    watchdog = ProcessWatchdog(
        manifest, manifest_digest=digest, state_dir=state, popen=factory
    )
    with patch("tools.run_autotrade_process_watchdog.sys.executable", str(pythonw)):
        watchdog.start_all()
    assert factory.calls[0][0][0] == str(python)
    assert factory.calls[0][1]["creationflags"] == (
        subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    )
    watchdog.stop_all(grace_seconds=0)

    failure_root = tmp_path / "launch-failure"
    failure_root.mkdir()
    _runtime, failure_state, manifest, digest = _load(failure_root, _row())

    def launch_failure(*_args, **_kwargs):
        raise FileNotFoundError("original launch failure")

    watchdog = ProcessWatchdog(
        manifest, manifest_digest=digest, state_dir=failure_state,
        popen=launch_failure,
    )
    with patch.object(
        watchdog.log, "write", side_effect=PermissionError("diagnostic failure")
    ):
        with unittest.TestCase().assertRaisesRegex(
            FileNotFoundError, "original launch failure"
        ):
            watchdog.start_all()


@unittest.skipUnless(os.name == "nt", "Windows process creation contract")
def test_windows_no_window_child_preserves_redirected_stdout_and_stderr(tmp_path):
    stdout_path = tmp_path / "child.stdout.log"
    stderr_path = tmp_path / "child.stderr.log"
    with stdout_path.open("wb", buffering=0) as stdout, \
            stderr_path.open("wb", buffering=0) as stderr:
        executable, flags = _child_launch()
        process = subprocess.Popen(
            [
                executable,
                "-c",
                (
                    "import sys; "
                    "print('watchdog-child-stdout', flush=True); "
                    "print('watchdog-child-stderr', file=sys.stderr, flush=True)"
                ),
            ],
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            close_fds=True,
            creationflags=flags,
        )
        assert process.wait(timeout=15) == 0
    assert stdout_path.read_text(encoding="utf-8").strip() == "watchdog-child-stdout"
    assert stderr_path.read_text(encoding="utf-8").strip() == "watchdog-child-stderr"


def test_watchdog_stop_marker_precedes_forced_termination(tmp_path):
    runtime, state, manifest, digest = _load(tmp_path, _row())
    factory = FakeFactory()
    watchdog = ProcessWatchdog(
        manifest, manifest_digest=digest, state_dir=state, popen=factory
    )
    watchdog.start_all()
    watchdog.stop_all(grace_seconds=0)
    marker = runtime / "attention" / "managed.stop.json"
    assert marker.is_file()
    assert factory.processes[0].returncode == -15
    status = json.loads((state / "watchdog-state.json").read_text(encoding="utf-8"))
    assert status["phase"] == "STOPPED"
    assert status["processes"][0]["status"] == "STOPPED"


def test_checked_in_manifest_is_secret_free_and_admitted(tmp_path):
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    runtime.mkdir()
    state.mkdir()
    manifest, _digest = load_manifest(
        REPO / "packaging/windows/autotrade-runtime-manifest.json",
        repo_root=REPO, runtime_root=runtime, state_dir=state,
    )
    assert [row["id"] for row in manifest["processes"]] == [
        "whitebit-public-sol",
        "whitebit-public-futures-trade-tape",
        "whitebit-course-research",
        "whitebit-depth-tape",
        "whitebit-subhour-screen",
        "whitebit-subhour-forward-shadow",
    ]
    assert "sol-trend-forward-shadow" not in {
        row["id"] for row in manifest["processes"]
    }
    serialized = json.dumps(manifest).lower()
    assert "api-key" not in serialized
    assert "password" not in serialized
    assert "secret" not in serialized
    processes = {row["id"]: row for row in manifest["processes"]}
    tape_args = processes["whitebit-public-futures-trade-tape"]["args"]
    tape_markets = [tape_args[index + 1]
        for index, value in enumerate(tape_args[:-1]) if value == "--market"]
    assert tape_markets == ["BTC_PERP", "ETH_PERP", "SOL_PERP", "NEAR_PERP", "SUI_PERP"]
    assert [
        tape_args[index + 1] for index, value in enumerate(tape_args[:-1])
        if value == "--exact-print-market"
    ] == ["BTC_PERP", "ETH_PERP"]
    depth = processes["whitebit-depth-tape"]
    depth_markets = [
        depth["args"][index + 1]
        for index, value in enumerate(depth["args"][:-1]) if value == "--market"
    ]
    assert depth_markets == tape_markets
    subhour = processes["whitebit-subhour-screen"]
    assert subhour["args"][subhour["args"].index("--bars") + 1] == "6000"
    forward = processes["whitebit-subhour-forward-shadow"]
    assert forward["args"][forward["args"].index("--poll-seconds") + 1] == "30"
    assert Path(forward["script"]).name == "run_whitebit_subhour_forward_shadow.py"


def test_automatic_watchdog_start_preserves_an_intentional_stop(tmp_path):
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    runtime.mkdir()
    state.mkdir()
    stop_file = state / "watchdog.stop.json"
    stop_file.write_text("owner stop request", encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"
    _write_manifest(manifest_path, _row())

    args = ["--manifest", str(manifest_path), "--runtime-root", str(runtime),
            "--state-dir", str(state)]
    captured = io.StringIO()
    with patch("tools.run_autotrade_process_watchdog.ProcessWatchdog") as runner:
        with redirect_stdout(captured):
            assert main([*args, "--respect-stop-marker"]) == 0
        runner.assert_not_called()
        assert json.loads(captured.getvalue())["phase"] == "STOP_REQUESTED"
        assert stop_file.read_text(encoding="utf-8") == "owner stop request"

        runner.return_value.run.return_value = 0
        assert main(args) == 0
        runner.return_value.run.assert_called_once()
        assert not stop_file.exists()


def test_duplicate_watchdog_does_not_start_or_consume_owner_stop(tmp_path):
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    runtime.mkdir()
    state.mkdir()
    stop_file = state / "watchdog.stop.json"
    stop_file.write_text("owner stop request", encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"
    _write_manifest(manifest_path, _row())

    captured = io.StringIO()
    with redirect_stdout(captured):
        with ResourceLock(state / "watchdog.lock"):
            result = main([
                "--manifest", str(manifest_path),
                "--runtime-root", str(runtime),
                "--state-dir", str(state),
            ])

    assert result == 0
    assert stop_file.read_text(encoding="utf-8") == "owner stop request"
    output = json.loads(captured.getvalue())
    assert output["phase"] == "ALREADY_RUNNING"
    assert output["network_orders"] is False


def test_watchdog_diagnostic_log_rotation_is_bounded_and_readable(tmp_path):
    path = tmp_path / "watchdog-diagnostics.jsonl"
    diagnostic = DiagnosticLog(path, maximum_bytes=128, archives=2)

    for sequence in range(10):
        diagnostic.write("VALIDATION_HEARTBEAT", sequence=sequence,
                         payload="x" * 80)

    segments = sorted(tmp_path.glob("watchdog-diagnostics.jsonl*"))
    assert {item.name for item in segments} == {
        "watchdog-diagnostics.jsonl",
        "watchdog-diagnostics.jsonl.1",
        "watchdog-diagnostics.jsonl.2",
    }
    for segment in segments:
        for line in segment.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            assert record["event"] == "VALIDATION_HEARTBEAT"


class ProcessWatchdogUnittestTests(unittest.TestCase):
    """Run the watchdog regressions under the repository's unittest CI runner."""

    def _run_with_temp(self, test):
        with TemporaryDirectory() as raw:
            test(Path(raw))

    def test_manifest_contract(self):
        self._run_with_temp(test_manifest_expands_only_bounded_paths_and_rejects_secrets)

    def test_atomic_publish_retry(self):
        self._run_with_temp(test_atomic_state_publish_retries_transient_windows_sharing_violation)

    def test_bounded_crash_circuit(self):
        self._run_with_temp(test_watchdog_restarts_then_opens_bounded_crash_circuit)

    def test_windows_creation_contract(self):
        self._run_with_temp(
            test_windows_children_are_console_isolated_and_pythonw_uses_python
        )

    @unittest.skipUnless(os.name == "nt", "Windows process creation contract")
    def test_real_windows_redirected_child(self):
        self._run_with_temp(
            test_windows_no_window_child_preserves_redirected_stdout_and_stderr
        )

    def test_stop_marker_order(self):
        self._run_with_temp(test_watchdog_stop_marker_precedes_forced_termination)

    def test_checked_in_manifest(self):
        self._run_with_temp(test_checked_in_manifest_is_secret_free_and_admitted)

    def test_duplicate_owner_lock(self):
        self._run_with_temp(test_duplicate_watchdog_does_not_start_or_consume_owner_stop)

    def test_diagnostic_rotation(self):
        self._run_with_temp(test_watchdog_diagnostic_log_rotation_is_bounded_and_readable)









