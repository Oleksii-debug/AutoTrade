from datetime import UTC, datetime, timedelta
import json
from unittest.mock import patch

from tools.run_whitebit_flow_research_supervisor import (
    SCHEMA as FLOW_SCHEMA,
    main as flow_main,
)
from tools.run_whitebit_maker_universe_supervisor import (
    SCHEMA as MAKER_SCHEMA,
    main as maker_main,
)


def _checkpoint(state, schema):
    state.mkdir()
    payload = {
        "schema": schema,
        "status": "STOPPED",
        "cycle_count": 1,
        "next_run_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        "updated_at": datetime.now(UTC).isoformat(),
    }
    (state / "checkpoint.json").write_text(json.dumps(payload), encoding="utf-8")


def test_flow_supervisor_publishes_running_before_future_due_wait(tmp_path):
    state = tmp_path / "flow"
    _checkpoint(state, FLOW_SCHEMA)
    writes = []
    with patch(
        "tools.run_whitebit_flow_research_supervisor.atomic_write_json",
        side_effect=lambda _path, payload: writes.append(dict(payload)),
    ), patch("tools.run_whitebit_flow_research_supervisor._wait", return_value=True):
        assert flow_main([
            "--trade-tape", str(tmp_path / "tape.sqlite3"),
            "--state-dir", str(state), "--market", "BTC_USDT",
            "--market", "ETH_USDT",
            "--stop-file", str(state / "managed.stop.json"),
        ]) == 0
    assert writes[0]["status"] == "RUNNING"
    assert writes[0]["network_requests"] == 0
    assert writes[-1]["status"] == "STOPPED"


def test_maker_supervisor_publishes_running_before_future_due_wait(tmp_path):
    state = tmp_path / "maker"
    _checkpoint(state, MAKER_SCHEMA)
    writes = []
    with patch(
        "tools.run_whitebit_maker_universe_supervisor.atomic_write_json",
        side_effect=lambda _path, payload: writes.append(dict(payload)),
    ), patch("tools.run_whitebit_maker_universe_supervisor._wait", return_value=True):
        assert maker_main([
            "--state-dir", str(state),
            "--stop-file", str(state / "managed.stop.json"),
        ]) == 0
    assert writes[0]["status"] == "RUNNING"
    assert writes[0]["profit_guaranteed"] is False
    assert writes[-1]["status"] == "STOPPED"
