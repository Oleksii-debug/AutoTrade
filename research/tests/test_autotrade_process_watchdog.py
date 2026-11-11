from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tools.run_autotrade_process_watchdog import (
    MANIFEST_SCHEMA,
    ProcessWatchdog,
    load_manifest,
)


REPO = Path(__file__).resolve().parents[2]


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
    assert Path(manifest["processes"][0]["stop_file"]).is_relative_to(runtime)

    bad = tmp_path / "bad.json"
    _write_manifest(bad, _row(args=["--api-key", "must-not-appear"]))
    with pytest.raises(ValueError, match="secrets are forbidden"):
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
        "whitebit-public", "learning-primary", "learning-fast",
        "whitebit-public-eth", "learning-eth-fast",
        "whitebit-public-sol", "learning-sol-fast", "goal-attention",
    ]
    serialized = json.dumps(manifest).lower()
    assert "api-key" not in serialized
    assert "password" not in serialized
    assert "secret" not in serialized
