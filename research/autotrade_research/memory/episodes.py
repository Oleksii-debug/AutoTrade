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
        digest = _hash(
            {
                "decision_time": decision.isoformat(),
                "information_cutoff": cutoff.isoformat(),
                "task": task,
                "regime": regime,
                "instrument_family": instrument_family,
                "permission_class": permission_class,
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
                    _text(task, name="task"),
                    _text(regime, name="regime"),
                    _text(instrument_family, name="instrument_family"),
                    _text(permission_class, name="permission_class"),
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
        correction_id: str | None = None,
    ) -> tuple[str, bool]:
        episode = _identifier(episode_id)
        if not isinstance(payload, dict) or not payload:
            raise ValueError("correction payload must be non-empty")
        if "supersedes_fields" not in payload or not isinstance(payload["supersedes_fields"], list):
            raise ValueError("correction must declare supersedes_fields")
        identifier = _identifier(correction_id)
        digest = _hash(payload)
        canonical = _canonical(payload)
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            if con.execute("SELECT 1 FROM episodes WHERE episode_id=?", (episode,)).fetchone() is None:
                raise KeyError(episode)
            existing = con.execute("SELECT * FROM corrections WHERE correction_id=?", (identifier,)).fetchone()
            if existing is not None:
                if existing["episode_id"] != episode or existing["correction_hash"] != digest:
                    raise MemoryConflict("correction identity conflict")
                return identifier, False
            con.execute(
                "INSERT INTO corrections(correction_id,episode_id,correction_hash,payload_json,created_at) VALUES(?,?,?,?,?)",
                (identifier, episode, digest, canonical, datetime.now(timezone.utc).isoformat()),
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
                    "SELECT * FROM corrections WHERE episode_id=? ORDER BY created_at,correction_id",
                    (row["episode_id"],),
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
