"""Read-only, bounded export of durable ZERO events and research artifacts.

The report is shareable JSON data. It carries verified journal events, not
browser observations, and never changes the journal or promotes a model.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO, StringIO
import csv
import json
import stat
from zipfile import ZipFile, ZIP_DEFLATED
from uuid import uuid4

from .diagnostics import redact_diagnostic_value
from .export_boundary import prepare_json_export, PreparedExport
from .persistence import JournalStore


_EVENT_COLUMNS = (
    "event_id, event_type, aggregate_type, aggregate_id, aggregate_version, "
    "payload_json, payload_hash, committed_at, envelope_json, envelope_hash, "
    "journal_sequence"
)
_MAX_ARTIFACT_BYTES = 1024 * 1024
_MAX_ARTIFACTS = 200


@dataclass(frozen=True)
class DiagnosticZipExport:
    filename: str
    data: bytes
    sha256: str


def _json_line(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _zip_export(journal: JournalStore, state_dir: Path, *, source_revision: str) -> DiagnosticZipExport:
    """Export every canonical journal event; derived views carry event identities.

    Technical one-second telemetry is present only if actually recorded. Its
    absence is explicit in the manifest, never filled with synthetic samples.
    """
    members: dict[str, bytes] = {}
    actions, learning, telemetry, trades, portfolio = ([] for _ in range(5))
    events = []
    for event in _events(journal):
        events.append(_json_line(event))
        kind = event["event_type"]
        payload = event["payload"]
        if kind in {"SimulationAgentDecisionRecorded", "AutonomousEpisodeStarted", "RiskDecisionRecorded", "AuthorityAdmissionRecorded"}:
            actions.append(_json_line(event))
        if "Learning" in kind or "Model" in kind or "Research" in kind:
            learning.append(_json_line(event))
        if kind in {"RuntimeDiagnosticRecorded", "AgentRuntimeHeartbeatRecorded"}:
            telemetry.append(_json_line(event))
        if kind == "AutonomousEpisodeFillObserved":
            fill = payload.get("fill", {})
            trades.append((event["journal_sequence"], event["committed_at"],
                fill.get("fill_id", ""), fill.get("provider_execution_id", ""),
                fill.get("price", ""), fill.get("quantity", ""),
                fill.get("side", "")))
        if kind == "AutonomousEpisodeCompleted":
            portfolio.append((event["journal_sequence"], event["committed_at"],
                payload.get("episode", ""), payload.get("cash", ""),
                payload.get("equity", ""), payload.get("position", "")))

    def csv_bytes(header, rows):
        output = StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(header)
        writer.writerows(rows)
        return output.getvalue().encode("utf-8")

    members["journal-events.jsonl"] = b"".join(events)
    members["agent-actions.jsonl"] = b"".join(actions)
    members["learning-history.jsonl"] = b"".join(learning)
    members["runtime-diagnostics.jsonl"] = b"".join(telemetry)
    members["trades.csv"] = csv_bytes(
        ["journal_sequence", "committed_at", "fill_id", "execution_id", "price", "quantity", "side"], trades)
    members["portfolio.csv"] = csv_bytes(
        ["journal_sequence", "committed_at", "episode", "cash", "equity", "position"], portfolio)
    for name in ("checkpoint.json", "learning-evidence.jsonl"):
        path = state_dir / name
        if path.exists() or path.is_symlink():
            members["artifacts/" + name] = _json_line(_artifact(path))
    manifest = {
        "schema_version": "1.0.0", "scope": "LOCAL_SIMULATION",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_revision": source_revision,
        "journal_event_count": len(events), "agent_action_count": len(actions),
        "learning_event_count": len(learning), "technical_sample_count": len(telemetry),
        "complete_journal": True,
        "technical_sampling_complete": False,
        "technical_sampling_note": "This runtime has no guaranteed one-second sampler; only recorded telemetry is exported.",
        "period": {"first_event_at": events and _events_first_time(members["journal-events.jsonl"]),
                   "last_event_at": events and _events_last_time(members["journal-events.jsonl"])},
        "members": {name: {"bytes": len(body), "sha256": sha256(body).hexdigest()}
                    for name, body in members.items()},
        "simulation_only": True,
    }
    members["manifest.json"] = json.dumps(manifest, sort_keys=True, ensure_ascii=False).encode("utf-8")
    buffer = BytesIO()
    with ZipFile(buffer, "w", compression=ZIP_DEFLATED, compresslevel=6) as archive:
        for name, body in sorted(members.items()):
            archive.writestr(name, body)
    data = buffer.getvalue()
    return DiagnosticZipExport("autotrade-diagnostics.zip", data, sha256(data).hexdigest())


def _events_first_time(lines: bytes) -> str:
    return json.loads(lines.splitlines()[0])["committed_at"]


def _events_last_time(lines: bytes) -> str:
    return json.loads(lines.splitlines()[-1])["committed_at"]


def runtime_diagnostic_zip(journal: JournalStore, state_dir: Path, *, source_revision: str) -> DiagnosticZipExport:
    if type(journal) is not JournalStore or not isinstance(state_dir, Path):
        raise TypeError("diagnostic inputs must be canonical journal and path")
    return _zip_export(journal, state_dir, source_revision=source_revision)


def _events(store: JournalStore) -> list[dict]:
    """Read and verify all event rows from one SQLite snapshot in append order."""
    with store._connect() as connection:
        connection.execute("BEGIN")
        try:
            rows = connection.execute(
                "SELECT " + _EVENT_COLUMNS + " FROM events ORDER BY journal_sequence"
            ).fetchall()
            events = [redact_diagnostic_value(store._decode_event_row(row)) for row in rows]
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    sequences = [event["journal_sequence"] for event in events]
    if sequences != list(range(1, len(events) + 1)):
        raise ValueError("journal event sequence has a gap")
    return events


def _artifact(path: Path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError("diagnostic artifact must be an ordinary unaliased file")
    if info.st_size > _MAX_ARTIFACT_BYTES:
        raise ValueError("diagnostic artifact exceeds one-megabyte limit")
    raw = path.read_bytes()
    if len(raw) > _MAX_ARTIFACT_BYTES:
        raise ValueError("diagnostic artifact changed during read")
    if path.suffix == ".jsonl":
        value = [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]
    else:
        value = json.loads(raw.decode("utf-8"))
    return redact_diagnostic_value(value)


def _prepare(payload: dict, filename: str) -> PreparedExport:
    return prepare_json_export(
        export_id=str(uuid4()), filename=filename,
        payload=payload,
        rights={"export": True, "rights_id": "local-owner-diagnostic"},
        source_refs=("durable-journal",),
    )


def runtime_diagnostic_export(journal: JournalStore, state_dir: Path) -> PreparedExport:
    """Export the desktop product's durable events and learning evidence."""
    if type(journal) is not JournalStore or type(state_dir) is not type(Path()):
        raise TypeError("diagnostic inputs must be canonical journal and path")
    events = _events(journal)
    artifacts = {}
    for name in ("checkpoint.json", "learning-evidence.jsonl"):
        path = state_dir / name
        if path.exists() or path.is_symlink():
            artifacts[name] = _artifact(path)
    payload = {
        "schema_version": "1.0.0",
        "scope": "LOCAL_SIMULATION",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "event_count": len(events),
        "events": events,
        "artifacts": artifacts,
        "coverage": "all canonical journal events in one read snapshot; present allowlisted learning artifacts",
        "notes": ["No provider fills or profitability claims are inferred from simulation."],
    }
    return _prepare(payload, "autotrade-diagnostics.json")


def historical_diagnostic_export(workspace: Path) -> PreparedExport:
    """Export every bounded monthly report/model/validation and account journal.

    The user supplies the exact workspace. Unknown files are listed as omitted
    rather than silently asserted to have been examined.
    """
    if type(workspace) is not type(Path()):
        raise TypeError("historical workspace must be an exact path")
    from tools.historical_public_klines import _reject_symlink_ancestors
    _reject_symlink_ancestors(workspace)
    if not workspace.is_dir():
        raise ValueError("historical workspace is missing")
    artifacts, journals, omitted = {}, {}, []
    for directory in ("models", "validation", "reports", "accounts"):
        root = workspace / directory
        if not root.exists():
            continue
        _reject_symlink_ancestors(root)
        if not root.is_dir():
            raise ValueError("historical workspace directory is invalid")
        for path in sorted(root.rglob("*")):
            relative = path.relative_to(workspace).as_posix()
            _reject_symlink_ancestors(path)
            if path.is_dir():
                continue
            if len(artifacts) + len(journals) + len(omitted) >= _MAX_ARTIFACTS:
                raise ValueError("historical workspace exceeds diagnostic file limit")
            if path.name == "journal.sqlite3" and directory == "accounts":
                journals[relative] = _events(JournalStore(path))
            elif path.suffix in (".json", ".jsonl"):
                artifacts[relative] = _artifact(path)
            else:
                omitted.append(relative)
    if not artifacts and not journals:
        raise ValueError("historical workspace has no diagnostic evidence")
    return _prepare({
        "schema_version": "1.0.0",
        "scope": "HISTORICAL_SIMULATION",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "artifacts": artifacts,
        "journals": journals,
        "omitted_files": omitted,
        "coverage": "allowlisted JSON artifacts and all events in account journals at read time",
        "notes": ["Historical retrospective outcomes are simulated and do not establish a profitable strategy."],
    }, "autotrade-historical-diagnostics.json")
