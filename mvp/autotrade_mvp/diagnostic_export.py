"""Read-only, bounded export of durable ZERO events and research artifacts.

The report is shareable JSON data. It carries verified journal events, not
browser observations, and never changes the journal or promotes a model.
"""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO, StringIO
import csv
import json
import re
import sqlite3
import stat
from zipfile import ZipFile, ZIP_DEFLATED
from uuid import uuid4

from .diagnostics import redact_diagnostic_value
from .export_boundary import prepare_json_export, PreparedExport
from .persistence import JournalStore
from .runtime_diagnostic_sampler import RuntimeDiagnosticSampler


_EVENT_COLUMNS = (
    "event_id, event_type, aggregate_type, aggregate_id, aggregate_version, "
    "payload_json, payload_hash, committed_at, envelope_json, envelope_hash, "
    "journal_sequence"
)
_MAX_ARTIFACT_BYTES = 1024 * 1024
_MAX_ARTIFACTS = 200
_MAX_GROQ_DATABASE_BYTES = 128 * 1024 * 1024
_MAX_GROQ_MIRROR_BYTES = 16 * 1024 * 1024
_MAX_GROQ_EVENTS = 50_000
_GROQ_SECRET_TEXT = re.compile(
    r"(?i)(?:gsk_|sk-|bearer\s+)[A-Za-z0-9_./+=-]{8,}"
)


@dataclass(frozen=True)
class DiagnosticZipExport:
    filename: str
    data: bytes
    sha256: str


def _json_line(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _zip_bytes(members: dict[str, bytes]) -> bytes:
    buffer = BytesIO()
    with ZipFile(buffer, "w", compression=ZIP_DEFLATED, compresslevel=6) as archive:
        for name, body in sorted(members.items()):
            archive.writestr(name, body)
    return buffer.getvalue()


def _zip_export(
    journal: JournalStore,
    state_dir: Path,
    *,
    source_revision: str,
    filename: str,
    report_kind: str,
    include_legacy_telemetry: bool,
) -> DiagnosticZipExport:
    """Build either the compatibility cut or the technical-free agent cut."""
    members: dict[str, bytes] = {}
    actions, learning, telemetry, trades, portfolio = ([] for _ in range(5))
    events = []
    for event in _events(journal):
        kind = event["event_type"]
        payload = event["payload"]
        if kind in {"RuntimeDiagnosticRecorded", "AgentRuntimeHeartbeatRecorded"}:
            telemetry.append(_json_line(event))
            if not include_legacy_telemetry:
                continue
        events.append(_json_line(event))
        if kind in {"SimulationAgentDecisionRecorded", "AutonomousEpisodeStarted", "RiskDecisionRecorded", "AuthorityAdmissionRecorded"}:
            actions.append(_json_line(event))
        if "Learning" in kind or "Model" in kind or "Research" in kind:
            learning.append(_json_line(event))
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
    if include_legacy_telemetry:
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
        "report_kind": report_kind,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_revision": source_revision,
        "journal_event_count": len(events), "agent_action_count": len(actions),
        "learning_event_count": len(learning),
        "complete_journal": include_legacy_telemetry,
        "complete_agent_evidence": not include_legacy_telemetry,
        "omitted_technical_journal_event_count": (
            0 if include_legacy_telemetry else len(telemetry)
        ),
        "period": {"first_event_at": events and _events_first_time(members["journal-events.jsonl"]),
                   "last_event_at": events and _events_last_time(members["journal-events.jsonl"])},
        "members": {name: {"bytes": len(body), "sha256": sha256(body).hexdigest()}
                    for name, body in members.items()},
        "simulation_only": True,
    }
    if include_legacy_telemetry:
        manifest.update({
            "technical_sample_count": len(telemetry),
            "technical_sampling_complete": False,
            "technical_sampling_note": (
                "Compatibility export only includes technical events that entered "
                "the financial journal; use the independent technical diagnostics export."
            ),
        })
    members["manifest.json"] = json.dumps(manifest, sort_keys=True, ensure_ascii=False).encode("utf-8")
    data = _zip_bytes(members)
    return DiagnosticZipExport(filename, data, sha256(data).hexdigest())


def _events_first_time(lines: bytes) -> str:
    return json.loads(lines.splitlines()[0])["committed_at"]


def _events_last_time(lines: bytes) -> str:
    return json.loads(lines.splitlines()[-1])["committed_at"]


def runtime_diagnostic_zip(journal: JournalStore, state_dir: Path, *, source_revision: str) -> DiagnosticZipExport:
    """Compatibility export retained for existing local clients.

    New UI clients use :func:`agent_report_zip` and
    :func:`technical_diagnostic_zip` so unlike evidence is never conflated.
    """
    if type(journal) is not JournalStore or not isinstance(state_dir, Path):
        raise TypeError("diagnostic inputs must be canonical journal and path")
    return _zip_export(
        journal,
        state_dir,
        source_revision=source_revision,
        filename="autotrade-diagnostics.zip",
        report_kind="LEGACY_COMBINED_DIAGNOSTICS",
        include_legacy_telemetry=True,
    )


def agent_report_zip(
    journal: JournalStore,
    state_dir: Path,
    *,
    source_revision: str,
    live_learning_status: dict[str, object] | None = None,
    groq_advisory_state_dir: Path | None = None,
) -> DiagnosticZipExport:
    """Export durable agent decisions, learning evidence and outcomes only."""
    if type(journal) is not JournalStore or type(state_dir) is not type(Path()):
        raise TypeError("agent report inputs must be canonical journal and path")
    if (
        groq_advisory_state_dir is not None
        and type(groq_advisory_state_dir) is not type(Path())
    ):
        raise TypeError("Groq advisory state directory must be an exact path")
    report = _zip_export(
        journal,
        state_dir,
        source_revision=source_revision,
        filename="autotrade-agent-report.zip",
        report_kind="AGENT_REPORT",
        include_legacy_telemetry=False,
    )
    if live_learning_status is None and groq_advisory_state_dir is None:
        return report
    if live_learning_status is not None and type(live_learning_status) is not dict:
        raise TypeError("live learning status must be an exact dictionary")
    with ZipFile(BytesIO(report.data), "r") as source:
        members = {name: source.read(name) for name in source.namelist()}
    manifest = json.loads(members["manifest.json"])
    if live_learning_status is not None:
        status = redact_diagnostic_value(live_learning_status)
        members["live-learning/status.json"] = _json_line(status)
        manifest["live_learning_status_included"] = True
    if groq_advisory_state_dir is not None:
        groq_members, groq_summary = _groq_advisory_export_members(
            groq_advisory_state_dir
        )
        members.update(groq_members)
        manifest["groq_advisory"] = groq_summary
    manifest["members"] = {
        name: {"bytes": len(body), "sha256": sha256(body).hexdigest()}
        for name, body in members.items()
        if name != "manifest.json"
    }
    members["manifest.json"] = json.dumps(
        manifest, sort_keys=True, ensure_ascii=False
    ).encode("utf-8")
    data = _zip_bytes(members)
    return DiagnosticZipExport(report.filename, data, sha256(data).hexdigest())


def _groq_advisory_export_members(
    state_dir: Path,
) -> tuple[dict[str, bytes], dict[str, object]]:
    """Project bounded, secret-free advisory evidence from read-only SQLite.

    The raw databases contain prompts and are never added to the archive.  The
    JSONL mirror is validated as a bounded ordinary file, but events are read
    from the SQLite authority so one transaction supplies a coherent snapshot.
    """
    if type(state_dir) is not type(Path()):
        raise TypeError("Groq advisory state directory must be an exact path")
    if not state_dir.is_absolute():
        raise ValueError("Groq advisory state directory must be absolute")
    for ancestor in (state_dir, *state_dir.parents):
        if ancestor.exists() and ancestor.is_symlink():
            raise ValueError("Groq advisory state directory cannot cross a symlink")
    if not state_dir.exists():
        return {}, {"included": False, "status": "NOT_AVAILABLE"}
    info = state_dir.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError("Groq advisory state path must be a directory")

    advisory_path = state_dir / "groq-advisory.sqlite3"
    quota_path = state_dir / "groq-quota.sqlite3"
    mirror_path = state_dir / "groq-advisory-events.jsonl"
    known_paths = (advisory_path, quota_path, mirror_path)
    if not any(path.exists() or path.is_symlink() for path in known_paths):
        return {}, {"included": False, "status": "NOT_AVAILABLE"}
    if not advisory_path.exists() and not advisory_path.is_symlink():
        raise ValueError("Groq advisory database is missing")
    for path in known_paths:
        if not path.exists() and not path.is_symlink():
            continue
        _validate_groq_evidence_file(
            path,
            maximum_bytes=(
                _MAX_GROQ_MIRROR_BYTES
                if path == mirror_path
                else _MAX_GROQ_DATABASE_BYTES
            ),
        )
    for database_path in (advisory_path, quota_path):
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(database_path) + suffix)
            if sidecar.exists() or sidecar.is_symlink():
                _validate_groq_evidence_file(
                    sidecar, maximum_bytes=_MAX_GROQ_DATABASE_BYTES
                )

    events, advisory_summary = _read_groq_advisory_database(advisory_path)
    quota_snapshot = (
        _read_groq_quota_database(quota_path)
        if quota_path.exists()
        else {
            "schema_version": "1.0.0",
            "status": "NOT_AVAILABLE",
            "financial_authority": False,
        }
    )
    event_body = b"".join(_safe_groq_json_line(event) for event in events)
    summary_body = _safe_groq_json_line(advisory_summary)
    quota_body = _safe_groq_json_line(quota_snapshot)
    members = {
        "groq-advisory/events.jsonl": event_body,
        "groq-advisory/summary.json": summary_body,
        "groq-advisory/quota-snapshot.json": quota_body,
    }
    return members, {
        "included": True,
        "status": advisory_summary["status"],
        "event_count": len(events),
        "wave_count": advisory_summary["wave_count"],
        "quota_status": quota_snapshot["status"],
        "financial_authority": False,
        "simulation_only": True,
    }


def _validate_groq_evidence_file(path: Path, *, maximum_bytes: int) -> None:
    if path.is_symlink():
        raise ValueError("Groq advisory evidence cannot be a symlink")
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError("Groq advisory evidence must be an ordinary unaliased file")
    if info.st_size > maximum_bytes:
        raise ValueError("Groq advisory evidence exceeds its export size limit")


def _readonly_sqlite(path: Path):
    return sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1.0)


def _require_columns(
    connection: sqlite3.Connection, table: str, required: set[str]
) -> None:
    columns = {
        row[1] for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
    }
    if not required.issubset(columns):
        raise ValueError("Groq advisory database schema is invalid")


def _read_groq_advisory_database(
    path: Path,
) -> tuple[list[dict[str, object]], dict[str, object]]:
    with closing(_readonly_sqlite(path)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        try:
            _require_columns(connection, "advisory_events", {"sequence", "event_json"})
            _require_columns(
                connection,
                "advisory_waves",
                {"wave_id", "status", "created_at", "updated_at"},
            )
            _require_columns(
                connection,
                "advisory_episodes",
                {
                    "wave_id", "ordinal", "role", "profile", "model_id",
                    "status", "prompt_tokens", "completion_tokens", "error_code",
                    "started_at", "completed_at",
                },
            )
            event_count = connection.execute(
                "SELECT COUNT(*) FROM advisory_events"
            ).fetchone()[0]
            if type(event_count) is not int or not 0 <= event_count <= _MAX_GROQ_EVENTS:
                raise ValueError("Groq advisory event count exceeds its export limit")
            event_rows = connection.execute(
                "SELECT sequence,event_json FROM advisory_events ORDER BY sequence"
            ).fetchall()
            wave_counts = {
                row["status"]: row["count"]
                for row in connection.execute(
                    "SELECT status,COUNT(*) AS count FROM advisory_waves GROUP BY status"
                ).fetchall()
            }
            episode_counts = {
                row["status"]: row["count"]
                for row in connection.execute(
                    "SELECT status,COUNT(*) AS count FROM advisory_episodes GROUP BY status"
                ).fetchall()
            }
            recent_waves = [
                dict(row) for row in connection.execute(
                    "SELECT wave_id,status,created_at,updated_at FROM advisory_waves "
                    "ORDER BY updated_at DESC,wave_id DESC LIMIT 100"
                ).fetchall()
            ]
            recent_episodes = [
                dict(row) for row in connection.execute(
                    "SELECT wave_id,ordinal,role,profile,model_id,status,prompt_tokens,"
                    "completion_tokens,error_code,started_at,completed_at "
                    "FROM advisory_episodes ORDER BY rowid DESC LIMIT 400"
                ).fetchall()
            ]
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    events: list[dict[str, object]] = []
    encoded_event_bytes = 0
    for expected, row in enumerate(event_rows, start=1):
        if row["sequence"] != expected:
            raise ValueError("Groq advisory event sequence has a gap")
        if type(row["event_json"]) is not str:
            raise ValueError("Groq advisory event JSON is invalid")
        encoded_event_bytes += len(row["event_json"].encode("utf-8")) + 1
        if encoded_event_bytes > _MAX_GROQ_MIRROR_BYTES:
            raise ValueError("Groq advisory events exceed their export size limit")
        try:
            event = json.loads(row["event_json"])
        except (TypeError, json.JSONDecodeError) as error:
            raise ValueError("Groq advisory event JSON is invalid") from error
        if type(event) is not dict:
            raise ValueError("Groq advisory event must be an exact object")
        events.append({"sequence": expected, **event})
    wave_count = sum(wave_counts.values())
    status = (
        "UNKNOWN"
        if wave_counts.get("UNKNOWN", 0)
        else "RUNNING"
        if wave_counts.get("RUNNING", 0)
        else "COMPLETED"
        if wave_count
        else "EMPTY"
    )
    return events, {
        "schema_version": "1.0.0",
        "status": status,
        "wave_count": wave_count,
        "wave_status_counts": wave_counts,
        "episode_count": sum(episode_counts.values()),
        "episode_status_counts": episode_counts,
        "recent_waves": recent_waves,
        "recent_episodes": recent_episodes,
        "financial_authority": False,
        "trading_authority": False,
        "simulation_only": True,
    }


def _read_groq_quota_database(path: Path) -> dict[str, object]:
    from .groq_account_pool import (
        GROQ_FREE_PLAN_QUOTAS_2026_10_20,
        GroqAccountCompliancePolicy,
    )

    now = datetime.now(timezone.utc)
    minute_window = int(now.timestamp()) // 60 * 60
    day_window = int(now.timestamp()) // 86400 * 86400
    policy = GroqAccountCompliancePolicy()
    with closing(_readonly_sqlite(path)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        try:
            _require_columns(
                connection,
                "quota_reservations",
                {
                    "profile", "model_id", "reserved_tokens", "actual_tokens",
                    "minute_window_start", "day_window_start", "state",
                },
            )
            _require_columns(
                connection,
                "provider_limit_observations",
                {"profile", "model_id", "observation_json"},
            )
            snapshots = []
            for profile in policy.stored_profiles:
                for quota in GROQ_FREE_PLAN_QUOTAS_2026_10_20:
                    minute = connection.execute(
                        "SELECT COUNT(*) AS requests,COALESCE(SUM(CASE WHEN "
                        "state='SETTLED' THEN actual_tokens ELSE reserved_tokens END),0) "
                        "AS tokens FROM quota_reservations WHERE profile=? AND "
                        "model_id=? AND minute_window_start=?",
                        (profile, quota.model_id, minute_window),
                    ).fetchone()
                    day = connection.execute(
                        "SELECT COUNT(*) AS requests,COALESCE(SUM(CASE WHEN "
                        "state='SETTLED' THEN actual_tokens ELSE reserved_tokens END),0) "
                        "AS tokens FROM quota_reservations WHERE profile=? AND "
                        "model_id=? AND day_window_start=?",
                        (profile, quota.model_id, day_window),
                    ).fetchone()
                    provider = connection.execute(
                        "SELECT observation_json FROM provider_limit_observations "
                        "WHERE profile=? AND model_id=? ORDER BY rowid DESC LIMIT 1",
                        (profile, quota.model_id),
                    ).fetchone()
                    provider_observation = None
                    if provider is not None:
                        try:
                            provider_observation = json.loads(
                                provider["observation_json"]
                            )
                        except (TypeError, json.JSONDecodeError) as error:
                            raise ValueError(
                                "Groq provider quota observation is invalid"
                            ) from error
                    snapshots.append({
                        "profile": profile,
                        "model_id": quota.model_id,
                        "minute_requests_remaining": max(
                            0, quota.requests_per_minute - minute["requests"]
                        ),
                        "minute_tokens_remaining": max(
                            0, quota.tokens_per_minute - minute["tokens"]
                        ),
                        "day_requests_remaining": max(
                            0, quota.requests_per_day - day["requests"]
                        ),
                        "day_tokens_remaining": max(
                            0, quota.tokens_per_day - day["tokens"]
                        ),
                        "provider_observation": provider_observation,
                    })
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    return {
        "schema_version": "1.0.0",
        "status": "AVAILABLE",
        "policy": "GROQ_FREE_PLAN_QUOTAS_2026_10_20",
        "observed_at": now.isoformat().replace("+00:00", "Z"),
        "profiles": snapshots,
        "provider_observations_are_advisory": True,
        "financial_authority": False,
    }


def _safe_groq_json_line(value: object) -> bytes:
    redacted = redact_diagnostic_value(value)
    encoded = json.dumps(
        redacted,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    encoded = _GROQ_SECRET_TEXT.sub("[REDACTED]", encoded)
    return (encoded + "\n").encode("utf-8")


def technical_diagnostic_zip(
    sampler: RuntimeDiagnosticSampler,
    *,
    source_revision: str,
) -> DiagnosticZipExport:
    """Export the bounded persistent heartbeat independently of agent evidence."""
    if type(sampler) is not RuntimeDiagnosticSampler:
        raise TypeError("technical diagnostics require the canonical runtime sampler")
    segments = sampler.snapshot()
    if not segments:
        raise ValueError("technical heartbeat has no retained samples")

    members: dict[str, bytes] = {}
    combined: list[bytes] = []
    first_sequence = None
    last_sequence = None
    first_time = None
    last_time = None
    prior_sequence = None
    prior_digest = None
    for name, body in segments:
        members["segments/" + name] = body
        for line in body.splitlines(keepends=True):
            row = json.loads(line)
            sequence = row.get("sequence")
            if type(sequence) is not int or sequence < 1:
                raise ValueError("technical heartbeat sequence is invalid")
            if prior_sequence is not None and (
                sequence != prior_sequence + 1
                or row.get("previous_sha256") != prior_digest
            ):
                raise ValueError("technical heartbeat sequence or hash chain is broken")
            first_sequence = sequence if first_sequence is None else first_sequence
            first_time = row.get("recorded_at_utc") if first_time is None else first_time
            last_sequence = sequence
            last_time = row.get("recorded_at_utc")
            prior_sequence = sequence
            prior_digest = sha256(line).hexdigest()
            combined.append(line)

    members["technical-heartbeats.jsonl"] = b"".join(combined)
    manifest = {
        "schema_version": "1.0.0",
        "scope": "LOCAL_RUNTIME",
        "report_kind": "TECHNICAL_DIAGNOSTICS",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_revision": source_revision,
        "sample_count": len(combined),
        "first_retained_sequence": first_sequence,
        "last_retained_sequence": last_sequence,
        "period": {"first_sample_at": first_time, "last_sample_at": last_time},
        "sampling_interval_seconds": sampler.interval_seconds,
        "retention": {
            "max_segment_bytes": sampler.max_segment_bytes,
            "max_segments": sampler.max_segments,
            "rotating": True,
            "persistent_across_restart": True,
        },
        "sampler_error": redact_diagnostic_value(sampler.error),
        "members": {
            name: {"bytes": len(body), "sha256": sha256(body).hexdigest()}
            for name, body in members.items()
        },
        "financial_authority": False,
        "simulation_only": True,
    }
    members["manifest.json"] = json.dumps(
        manifest,
        sort_keys=True,
        ensure_ascii=False,
    ).encode("utf-8")
    data = _zip_bytes(members)
    return DiagnosticZipExport(
        "autotrade-technical-diagnostics.zip",
        data,
        sha256(data).hexdigest(),
    )


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

