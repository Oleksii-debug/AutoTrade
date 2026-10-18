"""Private-local authenticated control of the existing historical ZERO campaign.

This is a process/lifecycle adapter only.  The campaign remains the sole owner
of simulation, OMS, risk and accounting.  No provider-order route is exposed.
"""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
import subprocess
import sys
from threading import RLock
from typing import Callable
from uuid import uuid4
from zipfile import ZIP_DEFLATED, ZipFile

from research.autotrade_research.io.strict_json import strict_json_loads


_SCHEMA = "autotrade-historical-host-v1"
_STATUSES = frozenset({"IDLE", "RUNNING", "PAUSED", "STOPPED", "COMPLETED", "FAILED"})


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class HistoricalHostController:
    """One workspace-scoped lifecycle authority for a ZERO campaign process."""

    def __init__(
        self,
        *,
        data_root: Path,
        workspace: Path,
        process_factory: Callable[..., subprocess.Popen] = subprocess.Popen,
        now: Callable[[], str] = _utc_now,
    ) -> None:
        if type(data_root) is not type(Path()) or type(workspace) is not type(Path()):
            raise TypeError("historical roots must be exact Paths")
        self.data_root = data_root.resolve(strict=False)
        self.workspace = workspace.resolve(strict=False)
        self._process_factory = process_factory
        self._now = now
        self._lock = RLock()
        self._process: subprocess.Popen | None = None

    @property
    def _state_path(self) -> Path:
        return self.workspace / "host-state.json"

    @staticmethod
    def _configuration() -> dict[str, object]:
        return {
            "symbol": "BTCUSDT",
            "interval": "1h",
            "training": ["2024-01", "2024-01"],
            "validation": ["2024-02", "2024-02"],
            "holdout": ["2024-03", "2024-03"],
            "episode_limit": 168,
            "virtual_cash_usd": "1000",
            "mode": "ZERO_SIMULATION",
        }

    def _initial_state(self) -> dict[str, object]:
        return {
            "schema": _SCHEMA,
            "generation": 0,
            "status": "IDLE",
            "pid": None,
            "started_at": None,
            "updated_at": self._now(),
            "last_error": None,
            "configuration": self._configuration(),
        }

    def _read_state(self) -> dict[str, object]:
        path = self._state_path
        if not path.exists():
            return self._initial_state()
        if path.is_symlink() or path.stat().st_size > 64 * 1024:
            raise ValueError("historical Host state is redirected or oversized")
        value = strict_json_loads(path.read_text(encoding="utf-8"))
        if type(value) is not dict or value.get("schema") != _SCHEMA:
            raise ValueError("historical Host state schema differs")
        if value.get("status") not in _STATUSES or value.get("configuration") != self._configuration():
            raise ValueError("historical Host state identity differs")
        if type(value.get("generation")) is not int or value["generation"] < 0:
            raise ValueError("historical Host generation is invalid")
        return value

    def _write_state(self, state: dict[str, object]) -> None:
        self.workspace.mkdir(parents=True, exist_ok=True)
        encoded = (json.dumps(state, sort_keys=True, indent=2) + "\n").encode("utf-8")
        temporary = self._state_path.with_name(self._state_path.name + "." + uuid4().hex + ".tmp")
        try:
            with temporary.open("xb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self._state_path)
        finally:
            temporary.unlink(missing_ok=True)

    def _refresh_process(self, state: dict[str, object]) -> dict[str, object]:
        process = self._process
        if process is None or state["status"] != "RUNNING":
            return state
        code = process.poll()
        if code is None:
            return state
        state = dict(state)
        state["status"] = "COMPLETED" if code == 0 else "FAILED"
        state["pid"] = None
        state["updated_at"] = self._now()
        state["last_error"] = None if code == 0 else "historical_campaign_exit_" + str(code)
        self._process = None
        self._write_state(state)
        return state

    def status(self) -> dict[str, object]:
        with self._lock:
            state = self._refresh_process(self._read_state())
            return {
                "status": state["status"],
                "generation": state["generation"],
                "started_at": state["started_at"],
                "updated_at": state["updated_at"],
                "last_error": state["last_error"],
                "configuration": dict(state["configuration"]),
                "trading_authority": False,
                "provider_order_route": "UNAVAILABLE",
            }

    def _command(self) -> list[str]:
        root = Path(__file__).resolve().parents[2]
        script = root / "tools" / "run_historical_campaign.py"
        if not script.is_file():
            raise FileNotFoundError("historical campaign tool is unavailable")
        return [
            sys.executable, str(script),
            "--data-root", str(self.data_root),
            "--workspace", str(self.workspace),
            "--symbol", "BTCUSDT",
            "--train-from", "2024-01", "--train-through", "2024-01",
            "--validate-from", "2024-02", "--validate-through", "2024-02",
            "--holdout-from", "2024-03", "--holdout-through", "2024-03",
            "--episode-limit", "168",
        ]

    def start(self) -> dict[str, object]:
        with self._lock:
            state = self._refresh_process(self._read_state())
            if state["status"] == "RUNNING":
                return self.status()
            if self._process is not None and self._process.poll() is None:
                raise RuntimeError("historical campaign process conflicts with durable state")
            self.workspace.mkdir(parents=True, exist_ok=True)
            stdout = (self.workspace / "host-campaign.stdout.log").open("ab")
            stderr = (self.workspace / "host-campaign.stderr.log").open("ab")
            try:
                process = self._process_factory(
                    self._command(),
                    cwd=str(Path(__file__).resolve().parents[2]),
                    stdin=subprocess.DEVNULL,
                    stdout=stdout,
                    stderr=stderr,
                    creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0),
                )
            finally:
                stdout.close()
                stderr.close()
            self._process = process
            state = dict(state)
            state.update({
                "generation": state["generation"] + 1,
                "status": "RUNNING",
                "pid": process.pid,
                "started_at": self._now(),
                "updated_at": self._now(),
                "last_error": None,
            })
            self._write_state(state)
            return self.status()

    def _terminate(self, target: str) -> dict[str, object]:
        if target not in {"PAUSED", "STOPPED"}:
            raise ValueError("unsupported historical terminal target")
        with self._lock:
            state = self._refresh_process(self._read_state())
            process = self._process
            if state["status"] == "RUNNING":
                if process is None or process.poll() is not None:
                    raise RuntimeError("running historical process is not owned by this Host occurrence")
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=15)
                self._process = None
            state = dict(state)
            state.update({"status": target, "pid": None, "updated_at": self._now(), "last_error": None})
            self._write_state(state)
            return self.status()

    def pause(self) -> dict[str, object]:
        return self._terminate("PAUSED")

    def stop(self) -> dict[str, object]:
        return self._terminate("STOPPED")

    def resume(self) -> dict[str, object]:
        with self._lock:
            if self._read_state()["status"] not in {"PAUSED", "STOPPED", "FAILED"}:
                raise ValueError("historical campaign is not resumable")
        return self.start()

    def _latest_report(self) -> tuple[Path, dict[str, object]] | None:
        reports = self.workspace / "reports"
        if not reports.is_dir() or reports.is_symlink():
            return None
        candidates = sorted(reports.glob("BTCUSDT-*.json"), key=lambda path: path.stat().st_mtime_ns)
        if not candidates:
            return None
        path = candidates[-1]
        if path.is_symlink() or path.stat().st_size > 8 * 1024 * 1024:
            raise ValueError("historical report is redirected or oversized")
        payload = strict_json_loads(path.read_text(encoding="utf-8"))
        if type(payload) is not dict:
            raise ValueError("historical report is malformed")
        return path, payload

    def results(self) -> dict[str, object]:
        with self._lock:
            latest = self._latest_report()
            if latest is None:
                return {"available": False, "report": None, "sha256": None}
            path, report = latest
            return {
                "available": True,
                "report": report,
                "sha256": sha256(path.read_bytes()).hexdigest(),
            }

    def trades(self) -> dict[str, object]:
        """Project bounded fills from the canonical USD-1000 ZERO OMS journal."""
        with self._lock:
            latest = self._latest_report()
            if latest is None:
                return {"available": False, "trades": []}
            _path, report = latest
            periods = report.get("periods")
            symbol = report.get("symbol")
            if type(periods) is not dict or type(periods.get("holdout")) is not list or type(symbol) is not str:
                raise ValueError("historical report scope is malformed")
            holdout = periods["holdout"]
            state_dir = self.workspace / "accounts" / symbol / (holdout[0] + "-" + holdout[1]) / "usd-1000"
            journal_path = state_dir / "journal.sqlite3"
            if not journal_path.is_file():
                return {"available": False, "trades": []}
            from .durable_order_projection import DurableOrderBookProjection
            from .persistence import JournalStore
            projection = DurableOrderBookProjection(
                JournalStore(journal_path), provider_id="SIMULATED",
                account_id="canonical-sim-account", environment="SIMULATION",
                host_id="local-simulation", owner_epoch="1",
            )
            rows = []
            for snapshot in projection.snapshots[-100:]:
                if snapshot.filled_quantity == 0:
                    continue
                rows.append({
                    "time": None,
                    "symbol": symbol,
                    "side": snapshot.side,
                    "quantity": format(snapshot.filled_quantity, "f"),
                    "price_usd": None if snapshot.average_fill_price is None else format(snapshot.average_fill_price, "f"),
                    "status": snapshot.state,
                    "order_id": snapshot.client_order_id,
                })
            return {"available": True, "trades": rows}

    def export_zip(self) -> bytes:
        with self._lock:
            output = BytesIO()
            with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
                archive.writestr("status.json", json.dumps(self.status(), sort_keys=True, indent=2))
                archive.writestr("results.json", json.dumps(self.results(), sort_keys=True, indent=2))
                archive.writestr("trades.json", json.dumps(self.trades(), sort_keys=True, indent=2))
            return output.getvalue()
