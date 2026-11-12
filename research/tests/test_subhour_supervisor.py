from datetime import UTC, datetime, timedelta
import json
from pathlib import Path

import pytest

from tools import run_whitebit_course_supervisor as course
from tools import run_whitebit_subhour_supervisor as subhour
from tools.run_whitebit_subhour_supervisor import FROZEN_STRATEGY_ID, configurations


def test_supervisor_uses_explicit_absolute_profit_gate():
    source = Path("tools/run_whitebit_subhour_supervisor.py").read_text(encoding="utf-8")
    assert 'gate_policy="POSITIVE_AFTER_COSTS"' in source


def test_registry_is_small_and_never_exceeds_one_hour():
    values = configurations()
    assert len(values) == 1
    assert {value.maximum_hold_bars for value in values} <= {2, 3, 4}
    assert {value.minimum_hold_bars for value in values} <= {2, 3, 4}
    assert all(value.stop_atr == "1" and value.take_profit_atr == "4"
               for value in values)
    assert all(value.maximum_entries_per_utc_day == 1 for value in values)
    assert all(value.minimum_expected_move_fraction == "0.006" for value in values)
    assert FROZEN_STRATEGY_ID == "SOL_BOLLINGER_RSI_60M_V1"


@pytest.mark.parametrize("supervisor", (course, subhour))
def test_battery_defers_history_without_losing_the_checkpoint(supervisor, tmp_path, monkeypatch):
    now = datetime.now(UTC)
    checkpoint = tmp_path / "checkpoint.json"
    stop = tmp_path / "stop"
    last_result = {"cycle_sha256": "saved"}
    due = (now - timedelta(minutes=1)).isoformat()
    checkpoint.write_text(json.dumps({
        "schema": supervisor.SCHEMA, "status": "COOLDOWN", "cycle_count": 2,
        "next_run_at": due, "last_result": last_result,
    }), encoding="utf-8")
    monkeypatch.setattr(supervisor, "running_on_battery", lambda: True)
    calls = []

    def run(**kwargs):
        calls.append(kwargs)
        return {"cycle_sha256": "new"}

    monkeypatch.setattr(supervisor, "run_cycle", run)
    args = ["--state-dir", str(tmp_path), "--stop-file", str(stop),
            "--defer-on-battery", "--once"]
    assert supervisor.main(args) == 0
    deferred = json.loads(checkpoint.read_bytes())
    assert deferred["status"] == "WAITING_FOR_AC_POWER"
    assert deferred["cycle_count"] == 2
    assert deferred["next_run_at"] == due
    assert deferred["last_result"] == last_result
    assert calls == []

    monkeypatch.setattr(supervisor, "running_on_battery", lambda: False)
    assert supervisor.main(args) == 0
    assert len(calls) == 1
    assert json.loads(checkpoint.read_bytes())["cycle_count"] == 3


def test_managed_subhour_restart_waits_until_persisted_due_time(tmp_path, monkeypatch):
    checkpoint = tmp_path / "checkpoint.json"
    stop = tmp_path / "stop"
    due = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    checkpoint.write_text(json.dumps({
        "schema": subhour.SCHEMA, "status": "RUNNING", "cycle_count": 2,
        "next_run_at": due, "last_result": {"cycle_sha256": "saved"},
    }), encoding="utf-8")
    monkeypatch.setattr(subhour, "sleep", lambda _: stop.touch())
    monkeypatch.setattr(subhour, "run_cycle", lambda **_: pytest.fail("not yet due"))
    assert subhour.main(["--state-dir", str(tmp_path), "--stop-file", str(stop)]) == 0
    saved = json.loads(checkpoint.read_bytes())
    assert saved["status"] == "STOPPED"
    assert saved["cycle_count"] == 2
    assert saved["next_run_at"] == due
