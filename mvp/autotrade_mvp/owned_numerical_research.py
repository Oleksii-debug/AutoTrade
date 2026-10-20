"""Opt-in owned process for the existing public collector and ZERO learner.

Starting this helper is an explicit public-network action. Importing it does
nothing. The fixed child reuses existing checkpointing functions and locks;
it never attaches to, stops, or kills an independently launched process.
"""
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from queue import Queue, Empty
import subprocess
import sys
from threading import Event, Thread


class OwnedResearchError(RuntimeError):
    """Bounded lifecycle failure, without untrusted child output."""


def _data_root(value: Path, product_root: Path) -> Path:
    raw = Path(value).absolute()
    for item in (raw, *raw.parents):
        if item.exists() and (item.is_symlink() or getattr(item.lstat(), "st_file_attributes", 0) & 0x400):
            raise OwnedResearchError("Research data path is redirected")
    root = raw.resolve()
    installed = product_root.resolve()
    if root == installed or installed in root.parents:
        raise OwnedResearchError("Research data must remain outside the updated product directory")
    root.mkdir(parents=True, exist_ok=True)
    return root


class OwnedNumericalResearch:
    """Explicit start/stop; successful stop means both owned workers returned.

    A failed graceful stop raises and leaves the owned handle available. The
    caller must abort update or explicitly use terminate_after_failed_stop();
    forced termination never constitutes confirmed graceful checkpointing.
    """
    def __init__(self, data_dir: Path):
        self.product_root = Path(__file__).resolve().parents[2]
        self.data_dir = _data_root(data_dir, self.product_root)
        self._process = None
        self._stop_failed = False
        self._status = "NOT_STARTED"

    def status(self) -> dict:
        status = self._status
        if self._process is not None and self._process.poll() is not None:
            status = "CHECKPOINTED" if self._process.returncode == 0 else "FAILED"
        return {"status": status, "checkpoint_confirmed": status == "CHECKPOINTED",
                "network_orders": False, "model_calls": 0}

    def start(self, *, readiness_timeout: float = 10.0) -> None:
        if self._process is not None:
            raise OwnedResearchError("This companion needs stop/cleanup before restarting")
        command = [sys.executable, "-B", "-m", "mvp.autotrade_mvp.owned_numerical_research",
                   "--child", "--data-dir", str(self.data_dir)]
        child = subprocess.Popen(command, cwd=self.product_root, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
            close_fds=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self._process = child
        self._stop_failed = False
        self._status = "STARTING"
        ready = Queue(maxsize=1)
        def read_ready():
            try:
                ready.put(child.stdout.readline(64))
            except (OSError, ValueError):
                ready.put("")
        Thread(target=read_ready, daemon=True).start()
        try:
            line = ready.get(timeout=readiness_timeout)
        except Empty:
            self._stop_failed = True
            self._status = "FAILED"
            raise OwnedResearchError("Research startup deadline exceeded; checkpoint unconfirmed") from None
        if line != "AUTOTRADE_NUMERICAL_OWNED\n":
            self._stop_failed = True
            self._status = "FAILED"
            raise OwnedResearchError("Research ownership could not be established")
        self._status = "RUNNING"

    def stop(self, *, timeout: float = 20.0) -> dict:
        child = self._process
        if child is None:
            return {"status": "NOT_STARTED", "checkpoint_confirmed": False}
        if child.poll() is None:
            try:
                child.stdin.write("STOP\n")
                child.stdin.flush()
            except (OSError, ValueError):
                pass
        try:
            code = child.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self._stop_failed = True
            self._status = "FAILED"
            raise OwnedResearchError("Research drain failed; checkpoint unconfirmed; update must not proceed") from None
        if code != 0:
            self._stop_failed = True
            self._status = "FAILED"
            raise OwnedResearchError("Research worker failed; checkpoint unconfirmed")
        self._close_pipes()
        self._process = None
        self._status = "CHECKPOINTED"
        return {"status": "STOPPED", "checkpoint_confirmed": True,
                "network_orders": False, "model_calls": 0}

    def terminate_after_failed_stop(self) -> dict:
        if not self._stop_failed:
            raise OwnedResearchError("Forced termination requires a reported lifecycle failure")
        child = self._process
        if child is not None and child.poll() is None:
            child.kill()
            child.wait(timeout=5)
        self._close_pipes()
        self._process = None
        self._status = "FAILED"
        return {"status": "TERMINATED", "checkpoint_confirmed": False}

    def _close_pipes(self):
        if self._process is not None:
            for stream in (self._process.stdin, self._process.stdout):
                if stream is not None:
                    stream.close()


async def _serve_child(data_dir: Path, *, collector=None, learner=None) -> None:
    # Injection exists only at this internal function for offline lifecycle
    # tests. The CLI and parent launch accept no alternate module or command.
    from research.autotrade_research.artifacts.resource_lock import ResourceLock, ResourceLockBusyError
    if collector is None:
        from tools.run_whitebit_public_supervisor import supervise as collector
    if learner is None:
        from tools.run_whitebit_live_zero_learning import run as learner
    root = _data_root(data_dir, Path(__file__).resolve().parents[2])
    collector_dir = root / "whitebit-public-supervisor"
    learner_dir = root / "whitebit-live-zero-learning"
    stop_collector = asyncio.Event()
    stop_learner = Event()
    loop = asyncio.get_running_loop()
    with ResourceLock(root / "owned-numerical-research.lock"):
        # Detect already-running independent workers before readiness or either
        # new worker starts. Actual workers reacquire their own canonical locks
        # for their full lifetimes, so concurrent startup is also fenced.
        collector_dir.mkdir(parents=True, exist_ok=True)
        learner_dir.mkdir(parents=True, exist_ok=True)
        with ResourceLock(collector_dir / "whitebit-public-supervisor.lock"):
            with ResourceLock(learner_dir / "whitebit-live-zero-learning.lock"):
                pass
        def request_stop():
            stop_learner.set()
            loop.call_soon_threadsafe(stop_collector.set)
        def read_control():
            while True:
                command = sys.stdin.readline(64)
                if not command or command.strip() == "STOP":
                    request_stop()
                    return
        Thread(target=read_control, daemon=True).start()
        collector_task = asyncio.create_task(collector(market="BTC_USDT", state_dir=collector_dir,
            stop=stop_collector, install_signals=False))
        learner_task = asyncio.create_task(asyncio.to_thread(learner,
            supervisor_state_dir=collector_dir, state_dir=learner_dir,
            interval_seconds=60.0, stop=stop_learner))
        try:
            admitted = False
            for _ in range(500):
                await asyncio.sleep(0.01)
                if collector_task.done() or learner_task.done():
                    raise OwnedResearchError("Numerical worker exited before admission")
                held = 0
                for path in (collector_dir / "whitebit-public-supervisor.lock",
                             learner_dir / "whitebit-live-zero-learning.lock"):
                    try:
                        with ResourceLock(path):
                            pass
                    except ResourceLockBusyError:
                        held += 1
                if held == 2:
                    admitted = True
                    break
            if not admitted:
                raise OwnedResearchError("Numerical worker admission deadline exceeded")
            print("AUTOTRADE_NUMERICAL_OWNED", flush=True)
            await asyncio.wait((collector_task, learner_task), return_when=asyncio.FIRST_COMPLETED)
        finally:
            stop_collector.set()
            stop_learner.set()
            outcomes = await asyncio.gather(collector_task, learner_task, return_exceptions=True)
        if any(isinstance(outcome, BaseException) for outcome in outcomes):
            raise OwnedResearchError("Owned numerical worker failed")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Owned WhiteBIT public numerical research companion")
    parser.add_argument("--child", action="store_true", required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        asyncio.run(_serve_child(args.data_dir))
        return 0
    except Exception:
        # No paths, credentials, market bodies or raw child exceptions emitted.
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
