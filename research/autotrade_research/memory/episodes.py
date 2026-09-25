"""Append-only cumulative experience memory with causal/permission retrieval."""

from __future__ import annotations

from contextlib import contextmanager

from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from typing import Any
from uuid import UUID, uuid4


def _time(value: datetime, *, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return _time(value, name="time").isoformat()


def _text(value: str, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    return value.strip()


def _identifier(value: str | None = None) -> str:
    return str(UUID(value)) if value is not None else str(uuid4())


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _hash(value: Any) -> str:
    return "sha256:" + sha256(_canonical(value).encode("utf-8")).hexdigest()


class MemoryConflict(ValueError):
    pass


class ExperienceMemory:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as con:
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS episodes(
                    episode_id TEXT PRIMARY KEY,
                    episode_hash TEXT NOT NULL,
                    decision_time TEXT NOT NULL,
                    information_cutoff TEXT NOT NULL,
                    task TEXT NOT NULL,
                    regime TEXT NOT NULL,
                    instrument_family TEXT NOT NULL,
                    permission_class TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS corrections(
                    correction_id TEXT PRIMARY KEY,
                    episode_id TEXT NOT NULL REFERENCES episodes(episode_id),
                    correction_hash TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tombstones(
                    tombstone_id TEXT PRIMARY KEY,
                    episode_id TEXT NOT NULL REFERENCES episodes(episode_id),
                    reason TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_episode_cutoff
                    ON episodes(information_cutoff, task, regime, instrument_family, permission_class);
                CREATE INDEX IF NOT EXISTS idx_corrections_episode
                    ON corrections(episode_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_tombstones_episode
                    ON tombstones(episode_id, created_at);
                """
            )
            correction_columns = {
                row["name"]
                for row in con.execute("PRAGMA table_info(corrections)").fetchall()
            }
            if "available_at" not in correction_columns:
                con.execute("ALTER TABLE corrections ADD COLUMN available_at TEXT")
            con.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_corrections_causal
                    ON corrections(episode_id, available_at, created_at, correction_id)
                """
            )

    @contextmanager
    def _connect(self):
        con = sqlite3.connect(self.path, timeout=30)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA foreign_keys=ON")
        try:
            yield con
            con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    def append_episode(
        self,
        *,
        decision_time: datetime,
        information_cutoff: datetime,
        task: str,
        regime: str,
        instrument_family: str,
        permission_class: str,
        payload: dict[str, Any],
        episode_id: str | None = None,
    ) -> tuple[str, bool]:
        decision = _time(decision_time, name="decision_time")
        cutoff = _time(information_cutoff, name="information_cutoff")
        if cutoff > decision:
            raise ValueError("information cutoff cannot be after decision time")
        if not isinstance(payload, dict) or not payload:
            raise ValueError("episode payload must be a non-empty object")
        required = {"evidence_refs", "intended_action", "actual_execution", "outcome", "costs"}
        missing = sorted(required - set(payload))
        if missing:
            raise ValueError("episode payload missing: " + ", ".join(missing))
        if not isinstance(payload["evidence_refs"], list) or not payload["evidence_refs"]:
            raise ValueError("episode requires evidence references")
        identifier = _identifier(episode_id)
        normalized_task = _text(task, name="task")
        normalized_regime = _text(regime, name="regime")
        normalized_family = _text(instrument_family, name="instrument_family")
        normalized_permission = _text(permission_class, name="permission_class")
        digest = _hash(
            {
                "decision_time": decision.isoformat(),
                "information_cutoff": cutoff.isoformat(),
                "task": normalized_task,
                "regime": normalized_regime,
                "instrument_family": normalized_family,
                "permission_class": normalized_permission,
                "payload": payload,
            }
        )
        canonical = _canonical(payload)
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            existing = con.execute("SELECT * FROM episodes WHERE episode_id=?", (identifier,)).fetchone()
            if existing is not None:
                if existing["episode_hash"] != digest:
                    raise MemoryConflict("episode identity already exists with different content")
                return identifier, False
            duplicate = con.execute("SELECT episode_id FROM episodes WHERE episode_hash=?", (digest,)).fetchone()
            if duplicate is not None:
                return str(duplicate["episode_id"]), False
            con.execute(
                """
                INSERT INTO episodes(
                    episode_id,episode_hash,decision_time,information_cutoff,task,regime,
                    instrument_family,permission_class,payload_json,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    identifier,
                    digest,
                    decision.isoformat(),
                    cutoff.isoformat(),
                    normalized_task,
                    normalized_regime,
                    normalized_family,
                    normalized_permission,
                    canonical,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            con.commit()
        return identifier, True

    def append_correction(
        self,
        episode_id: str,
        *,
        payload: dict[str, Any],
        available_at: datetime | None = None,
        correction_id: str | None = None,
    ) -> tuple[str, bool]:
        episode = _identifier(episode_id)
        requested_availability = (
            _time(available_at, name="available_at")
            if available_at is not None
            else None
        )
        if not isinstance(payload, dict) or not payload:
            raise ValueError("correction payload must be non-empty")
        if "supersedes_fields" not in payload or not isinstance(payload["supersedes_fields"], list):
            raise ValueError("correction must declare supersedes_fields")
        supersedes = tuple(
            _text(field, name="supersedes_field")
            for field in payload["supersedes_fields"]
        )
        if not supersedes:
            raise ValueError("correction supersedes_fields must be non-empty")
        if len(set(supersedes)) != len(supersedes):
            raise ValueError("correction supersedes_fields must be unique")
        missing_fields = tuple(field for field in supersedes if field not in payload)
        if missing_fields:
            raise ValueError(
                "correction must contain every superseded field: "
                + ", ".join(missing_fields)
            )
        if "evidence_ref" not in payload:
            raise ValueError("correction requires evidence_ref")
        evidence_ref = _text(payload["evidence_ref"], name="evidence_ref")
        normalized_payload = dict(payload)
        normalized_payload["supersedes_fields"] = list(supersedes)
        normalized_payload["evidence_ref"] = evidence_ref
        payload = normalized_payload
        identifier = _identifier(correction_id)
        canonical = _canonical(payload)
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            episode_row = con.execute(
                "SELECT decision_time, payload_json FROM episodes WHERE episode_id=?",
                (episode,),
            ).fetchone()
            if episode_row is None:
                raise KeyError(episode)
            episode_decision = datetime.fromisoformat(episode_row["decision_time"]).astimezone(
                timezone.utc
            )
            source_payload = json.loads(episode_row["payload_json"])
            unknown_supersedes = tuple(
                field for field in supersedes if field not in source_payload
            )
            if unknown_supersedes:
                raise MemoryConflict(
                    "correction cannot supersede fields absent from source episode: "
                    + ", ".join(unknown_supersedes)
                )
            correction_metadata = {"supersedes_fields", "evidence_ref"}
            invented_fields = tuple(
                field
                for field in payload
                if field not in correction_metadata and field not in source_payload
            )
            if invented_fields:
                raise MemoryConflict(
                    "correction cannot introduce fields absent from source episode: "
                    + ", ".join(invented_fields)
                )
            existing = con.execute("SELECT * FROM corrections WHERE correction_id=?", (identifier,)).fetchone()
            if existing is not None:
                existing_availability = existing["available_at"] or existing["created_at"]
                effective_availability = (
                    requested_availability.isoformat()
                    if requested_availability is not None
                    else existing_availability
                )
                current_digest = _hash(
                    {
                        "available_at": existing_availability,
                        "payload": payload,
                    }
                )
                legacy_digest = _hash(payload)
                hash_matches = existing["correction_hash"] == current_digest or (
                    existing["available_at"] is None
                    and existing["correction_hash"] == legacy_digest
                )
                same = (
                    existing["episode_id"] == episode
                    and existing["payload_json"] == canonical
                    and hash_matches
                    and existing_availability == effective_availability
                )
                if not same:
                    raise MemoryConflict("correction identity conflict")
                return identifier, False

            availability = requested_availability or datetime.now(timezone.utc)
            if availability < episode_decision:
                raise MemoryConflict(
                    "correction availability cannot precede episode decision_time"
                )
            digest = _hash(
                {
                    "available_at": availability.isoformat(),
                    "payload": payload,
                }
            )
            con.execute(
                """
                INSERT INTO corrections(
                    correction_id,episode_id,correction_hash,payload_json,created_at,available_at
                ) VALUES(?,?,?,?,?,?)
                """,
                (
                    identifier,
                    episode,
                    digest,
                    canonical,
                    datetime.now(timezone.utc).isoformat(),
                    availability.isoformat(),
                ),
            )
            con.commit()
        return identifier, True

    def tombstone(self, episode_id: str, *, reason: str) -> str:
        episode = _identifier(episode_id)
        why = _text(reason, name="reason")
        identifier = _identifier()
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            if con.execute("SELECT 1 FROM episodes WHERE episode_id=?", (episode,)).fetchone() is None:
                raise KeyError(episode)
            con.execute(
                "INSERT INTO tombstones(tombstone_id,episode_id,reason,created_at) VALUES(?,?,?,?)",
                (identifier, episode, why, datetime.now(timezone.utc).isoformat()),
            )
            con.commit()
        return identifier

    @staticmethod
    def _permission_allowed(required: str, granted: set[str]) -> bool:
        return required in granted

    def retrieve(
        self,
        *,
        information_cutoff: datetime,
        granted_permissions: set[str],
        task: str | None = None,
        regime: str | None = None,
        instrument_family: str | None = None,
        include_tombstoned: bool = False,
    ) -> tuple[dict[str, Any], ...]:
        cutoff = _time(information_cutoff, name="information_cutoff")
        if not isinstance(granted_permissions, set):
            raise TypeError("granted_permissions must be a set")
        query = "SELECT * FROM episodes WHERE information_cutoff <= ? AND decision_time <= ?"
        args: list[Any] = [cutoff.isoformat(), cutoff.isoformat()]
        for column, value in (
            ("task", task),
            ("regime", regime),
            ("instrument_family", instrument_family),
        ):
            if value is not None:
                query += f" AND {column}=?"
                args.append(_text(value, name=column))
        query += " ORDER BY decision_time, episode_id"

        results: list[dict[str, Any]] = []
        with self._connect() as con:
            for row in con.execute(query, args).fetchall():
                if not self._permission_allowed(row["permission_class"], granted_permissions):
                    continue
                tombstones = con.execute(
                    "SELECT * FROM tombstones WHERE episode_id=? ORDER BY created_at,tombstone_id",
                    (row["episode_id"],),
                ).fetchall()
                if tombstones and not include_tombstoned:
                    continue
                corrections = con.execute(
                    """
                    SELECT * FROM corrections
                    WHERE episode_id=?
                      AND COALESCE(available_at, created_at) <= ?
                    ORDER BY COALESCE(available_at, created_at), created_at, correction_id
                    """,
                    (row["episode_id"], cutoff.isoformat()),
                ).fetchall()
                results.append(
                    {
                        "episode_id": row["episode_id"],
                        "episode_hash": row["episode_hash"],
                        "decision_time": row["decision_time"],
                        "information_cutoff": row["information_cutoff"],
                        "task": row["task"],
                        "regime": row["regime"],
                        "instrument_family": row["instrument_family"],
                        "permission_class": row["permission_class"],
                        "payload": json.loads(row["payload_json"]),
                        "corrections": [json.loads(item["payload_json"]) for item in corrections],
                        "tombstones": [
                            {"reason": item["reason"], "created_at": item["created_at"]}
                            for item in tombstones
                        ],
                    }
                )
        return tuple(results)

    def source_episode(self, episode_id: str) -> dict[str, Any]:
        identifier = _identifier(episode_id)
        with self._connect() as con:
            row = con.execute("SELECT * FROM episodes WHERE episode_id=?", (identifier,)).fetchone()
            if row is None:
                raise KeyError(identifier)
            return {
                "episode_id": row["episode_id"],
                "episode_hash": row["episode_hash"],
                "payload": json.loads(row["payload_json"]),
                "information_cutoff": row["information_cutoff"],
            }
