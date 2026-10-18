"""Fail-closed read model for the autonomous WhiteBIT public supervisor.

This module only reads the supervisor's already-published public files.  It
does not import or start the collector and exposes no credential, account,
order, transfer, or provider-write surface.
"""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import stat
from typing import Callable

from research.autotrade_research.io.strict_json import strict_json_loads


_SUPERVISOR_SCHEMA = "autotrade-whitebit-public-supervisor-v1"
_STATUS_SCHEMA = "autotrade-whitebit-public-status-v1"
_ACTIVE_DIAGNOSTIC = "whitebit-public-diagnostics.jsonl"
_MAX_STATUS_BYTES = 64 * 1024
_MAX_ACTIVE_DIAGNOSTIC_BYTES = 512 * 1024
_COHERENT_READ_ATTEMPTS = 3
_MAX_HEARTBEAT_AGE_SECONDS = 90.0
_STATE_FIELDS = frozenset({
    "schema", "market", "phase", "checkpointed_at", "observation_count",
    "latest_evidence_ref", "public_data_only", "trading_authority",
})
_LATEST_FIELDS = frozenset({
    "schema", "market", "venue", "bid_price", "bid_quantity", "ask_price",
    "ask_quantity", "source_event_at", "available_at", "published_at",
    "update_id", "stream_generation", "raw_evidence_ref", "public_data_only",
    "trading_authority",
})
_EVIDENCE_FIELDS = frozenset({
    "artifact_id", "sha256", "source_uri", "observed_at", "rights_id",
})
_DIAGNOSTIC_FIELDS = frozenset({
    "schema", "recorded_at_utc", "sequence", "previous_sha256", "event",
    "market", "phase", "connection_code", "attempt", "generation",
    "observation_count", "last_observed_at", "public_data_only",
    "trading_authority",
})
_PHASES = frozenset({"STARTING", "RUNNING", "STOPPING", "STOPPED", "FAILED"})
_EVENTS = frozenset({"HEARTBEAT", "TRANSPORT", "OBSERVATION", "CHECKPOINT"})
_CONNECTION_CODES = frozenset({
    "CONNECTING", "SUBSCRIBED", "CONNECTION_LOST", "SCHEMA_REJECTED",
    "RECONNECT_WAIT", "STOPPED",
})


def _market(value: object) -> str:
    if (
        type(value) is not str
        or not 3 <= len(value) <= 32
        or value != value.upper()
        or "_" not in value
        or any(character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_" for character in value)
    ):
        raise ValueError("WhiteBIT public market is not canonical")
    return value


def _utc(value: object, *, name: str) -> tuple[str, datetime]:
    if type(value) is not str or not value:
        raise ValueError(f"{name} must be an exact timestamp")
    token = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(token)
    except ValueError as error:
        raise ValueError(f"{name} must be an ISO UTC timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError(f"{name} must be UTC")
    normalized = parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return normalized, parsed.astimezone(timezone.utc)


def _ordinary_bytes(path: Path, *, maximum: int) -> bytes:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError("status source must be an ordinary unaliased file")
    if info.st_size <= 0 or info.st_size > maximum:
        raise ValueError("status source size is outside the bounded read budget")
    body = path.read_bytes()
    if len(body) != info.st_size:
        raise ValueError("status source changed during read")
    return body


def _strict_object(path: Path, *, fields: frozenset[str]) -> dict:
    body = _ordinary_bytes(path, maximum=_MAX_STATUS_BYTES)
    value = strict_json_loads(body.decode("utf-8"))
    if type(value) is not dict or frozenset(value) != fields:
        raise ValueError("status source fields are not canonical")
    return value


def _public_boundary(value: dict) -> None:
    if (
        value.get("schema") != _SUPERVISOR_SCHEMA
        or value.get("public_data_only") is not True
        or value.get("trading_authority") is not False
    ):
        raise ValueError("WhiteBIT public-only boundary is not canonical")


def _latest_diagnostic(root: Path) -> tuple[dict, str]:
    """Validate only the bounded active segment used by the hot status path.

    Rotated history remains collector diagnostic evidence; the Host snapshot
    never rescans it. Adjacent rows in the active segment still prove sequence
    and hash continuity, while the stable-read retry below detects rotation or
    append races around latest-market.json.
    """
    if root.is_symlink() or not root.is_dir():
        raise ValueError("diagnostic root must be an ordinary directory")
    body = _ordinary_bytes(
        root / _ACTIVE_DIAGNOSTIC,
        maximum=_MAX_ACTIVE_DIAGNOSTIC_BYTES,
    )
    if not body.endswith(b"\n"):
        raise ValueError("diagnostic heartbeat evidence is incomplete")
    prior_sequence: int | None = None
    prior_digest: str | None = None
    latest: dict | None = None
    for raw in body.splitlines(keepends=True):
        row = strict_json_loads(raw[:-1].decode("utf-8"))
        if type(row) is not dict or frozenset(row) != _DIAGNOSTIC_FIELDS:
            raise ValueError("diagnostic heartbeat fields are not canonical")
        _public_boundary(row)
        sequence = row["sequence"]
        if type(sequence) is not int or sequence < 1:
            raise ValueError("diagnostic heartbeat sequence is invalid")
        if prior_sequence is not None and (
            sequence != prior_sequence + 1
            or row["previous_sha256"] != prior_digest
        ):
            raise ValueError("diagnostic heartbeat chain is broken")
        if (
            row["event"] not in _EVENTS
            or row["phase"] not in _PHASES
            or _market(row["market"]) != row["market"]
            or row["connection_code"] not in _CONNECTION_CODES
            or any(type(row[name]) is not int or row[name] < 0 for name in (
                "attempt", "generation", "observation_count"
            ))
        ):
            raise ValueError("diagnostic heartbeat values are invalid")
        _utc(row["recorded_at_utc"], name="heartbeat recorded_at_utc")
        if row["last_observed_at"] is not None:
            _utc(row["last_observed_at"], name="heartbeat last_observed_at")
        prior_sequence = sequence
        prior_digest = sha256(raw).hexdigest()
        latest = row
    if latest is None or prior_digest is None:
        raise ValueError("WhiteBIT heartbeat is empty")
    return latest, prior_digest


def _validated_state(root: Path) -> dict:
    value = _strict_object(root / "supervisor-state.json", fields=_STATE_FIELDS)
    _public_boundary(value)
    if (
        _market(value["market"]) != value["market"]
        or value["phase"] not in _PHASES
        or type(value["observation_count"]) is not int
        or value["observation_count"] < 0
    ):
        raise ValueError("supervisor checkpoint values are invalid")
    _utc(value["checkpointed_at"], name="supervisor checkpointed_at")
    return value


def _validated_latest(root: Path) -> dict:
    value = _strict_object(root / "latest-market.json", fields=_LATEST_FIELDS)
    _public_boundary(value)
    evidence = value["raw_evidence_ref"]
    if type(evidence) is not dict or frozenset(evidence) != _EVIDENCE_FIELDS:
        raise ValueError("latest public evidence reference is not canonical")
    for field in (
        "bid_price", "bid_quantity", "ask_price", "ask_quantity",
    ):
        if type(value[field]) is not str or not value[field]:
            raise ValueError("latest public market value is invalid")
    if _market(value["market"]) != value["market"] or value["venue"] != "WHITEBIT_SPOT":
        raise ValueError("latest public market identity is invalid")
    if (
        type(value["update_id"]) is not int
        or value["update_id"] < 0
        or type(value["stream_generation"]) is not int
        or value["stream_generation"] < 1
    ):
        raise ValueError("latest public stream identity is invalid")
    for field in ("source_event_at", "available_at", "published_at"):
        _utc(value[field], name="latest " + field)
    if (
        type(evidence["artifact_id"]) is not str
        or type(evidence["sha256"]) is not str
        or not evidence["sha256"].startswith("sha256:")
        or type(evidence["source_uri"]) is not str
        or not evidence["source_uri"].startswith("wss://wss.whitebit.com/")
        or type(evidence["rights_id"]) is not str
        or not evidence["rights_id"]
    ):
        raise ValueError("latest public evidence reference is invalid")
    _utc(evidence["observed_at"], name="latest evidence observed_at")
    return value


def _blocked_status(as_of: str, reason: str) -> dict[str, object]:
    return {
        "schema": _STATUS_SCHEMA,
        "health": {
            "component": "WHITEBIT_PUBLIC_SUPERVISOR",
            "status": "BLOCKED",
            "as_of": as_of,
            "reason_codes": [reason],
        },
        "heartbeat": None,
        "latest": None,
        "public_data_only": True,
        "trading_authority": False,
    }


def read_whitebit_public_status(
    state_dir: str | Path,
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> dict[str, object]:
    """Return a bounded, secret-free supervisor projection, never authority."""

    observed_now = clock()
    if type(observed_now) is not datetime or observed_now.tzinfo is None:
        raise ValueError("status clock must return a timezone-aware datetime")
    now = observed_now.astimezone(timezone.utc)
    as_of = now.isoformat().replace("+00:00", "Z")
    try:
        if type(state_dir) not in (str, type(Path())):
            raise TypeError("WhiteBIT state path must be exact text or Path")
        root = Path(state_dir).resolve(strict=False)
        if root.is_symlink() or not root.is_dir():
            raise ValueError("WhiteBIT supervisor state directory is unavailable")
        state = None
        latest = None
        heartbeat = None
        saw_incoherent_cut = False
        for _attempt in range(_COHERENT_READ_ATTEMPTS):
            try:
                candidate_state = _validated_state(root)
                first, first_digest = _latest_diagnostic(root / "diagnostics")
                candidate_latest = (
                    _validated_latest(root)
                    if first["observation_count"] > 0
                    else None
                )
                second, second_digest = _latest_diagnostic(root / "diagnostics")
            except (OSError, UnicodeDecodeError, TypeError, ValueError):
                continue
            if (
                first["sequence"] == second["sequence"]
                and first_digest == second_digest
            ):
                state = candidate_state
                latest = candidate_latest
                heartbeat = second
                break
            saw_incoherent_cut = True
        if state is None or heartbeat is None:
            return _blocked_status(
                as_of,
                "whitebit_public_status_changing"
                if saw_incoherent_cut
                else "whitebit_public_status_unavailable",
            )
        heartbeat_text, heartbeat_at = _utc(
            heartbeat["recorded_at_utc"], name="heartbeat recorded_at_utc"
        )
        if heartbeat["market"] != state["market"]:
            raise ValueError("WhiteBIT supervisor market identity changed")

        latest_projection = None
        if latest is not None:
            available_at, _ = _utc(latest["available_at"], name="latest available_at")
            if (
                latest["market"] != heartbeat["market"]
                or latest["stream_generation"] != heartbeat["generation"]
                or heartbeat["last_observed_at"] is None
                or _utc(heartbeat["last_observed_at"], name="heartbeat last_observed_at")[0]
                    != available_at
            ):
                raise ValueError("latest public observation is not bound to heartbeat")
            latest_projection = {
                "market": latest["market"],
                "venue": latest["venue"],
                "available_at": available_at,
                "update_id": latest["update_id"],
                "stream_generation": latest["stream_generation"],
            }

        age = (now - heartbeat_at).total_seconds()
        reasons: list[str] = []
        phase = heartbeat["phase"]
        connection = heartbeat["connection_code"]
        if age < 0 or age > _MAX_HEARTBEAT_AGE_SECONDS:
            health = "DEGRADED"
            reasons.append("whitebit_public_heartbeat_stale")
        elif phase == "FAILED":
            health = "BLOCKED"
            reasons.append("whitebit_public_supervisor_failed")
        elif phase in {"STOPPING", "STOPPED"}:
            health = "STOPPED"
            reasons.append("whitebit_public_supervisor_stopped")
        elif phase == "STARTING":
            health = "RECOVERING"
            reasons.append("whitebit_public_supervisor_starting")
        elif connection != "SUBSCRIBED":
            health = "DEGRADED"
            reasons.append("whitebit_public_connection_unavailable")
        elif latest_projection is None:
            health = "DEGRADED"
            reasons.append("whitebit_public_latest_unavailable")
        else:
            health = "READY"

        return {
            "schema": _STATUS_SCHEMA,
            "health": {
                "component": "WHITEBIT_PUBLIC_SUPERVISOR",
                "status": health,
                "as_of": heartbeat_text,
                "reason_codes": reasons,
            },
            "heartbeat": {
                "as_of": heartbeat_text,
                "sequence": heartbeat["sequence"],
                "phase": phase,
                "connection_code": connection,
                "observation_count": heartbeat["observation_count"],
            },
            "latest": latest_projection,
            "public_data_only": True,
            "trading_authority": False,
        }
    except (OSError, UnicodeDecodeError, TypeError, ValueError):
        return _blocked_status(as_of, "whitebit_public_status_unavailable")
