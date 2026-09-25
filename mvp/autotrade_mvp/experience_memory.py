from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any


OUTCOMES = {"POSITIVE", "NEGATIVE", "NULL", "UNKNOWN", "PENDING"}


@dataclass(frozen=True, slots=True)
class ExperienceEpisode:
    episode_id: str
    decision_id: str
    evidence_cutoff: str
    evidence_refs: tuple[str, ...]
    action_ref: str | None
    execution_ref: str | None
    outcome_class: str
    outcome_payload: Any
    source_version: str
    superseded_by: str | None
    tombstoned: bool


class ExperienceMemoryStore:
    """Append-only experience truth with correction and tombstone lineage.

    Derived indexes may be rebuilt from this store. Earlier episodes are never
    overwritten; corrections append a successor. Negative and null outcomes are
    first-class evidence and cannot be silently omitted by filtered retrieval.
    """

    SCHEMA_VERSION = 1

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            yield connection
        finally:
            connection.close()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)

    @staticmethod
    def _canonical_cutoff(value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("evidence_cutoff must be non-empty text")
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as error:
            raise ValueError("evidence_cutoff must be ISO-8601") from error
        if parsed.tzinfo is None:
            raise ValueError("evidence_cutoff must be timezone-aware")
        return parsed.astimezone(timezone.utc).isoformat()

    @classmethod
    def _evidence_refs(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if not isinstance(values, tuple) or not values:
            raise ValueError("evidence_refs must be a non-empty tuple")
        normalized = tuple(cls._require_text(value, "evidence_ref") for value in values)
        if len(set(normalized)) != len(normalized):
            raise ValueError("evidence_refs cannot contain duplicates")
        return normalized

    @staticmethod
    def _require_text(value: Any, name: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must be non-empty text")
        return value.strip()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS experience_schema "
                "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            current = connection.execute(
                "SELECT MAX(version) FROM experience_schema"
            ).fetchone()[0]
            if current is not None and current > self.SCHEMA_VERSION:
                connection.rollback()
                raise ValueError("experience schema is newer than this runtime")
            if current is None:
                connection.executescript(
                    """
                    CREATE TABLE experiences (
                        episode_id TEXT PRIMARY KEY,
                        decision_id TEXT NOT NULL,
                        evidence_cutoff TEXT NOT NULL,
                        evidence_refs_json TEXT NOT NULL,
                        action_ref TEXT,
                        execution_ref TEXT,
                        outcome_class TEXT NOT NULL CHECK (
                            outcome_class IN ('POSITIVE','NEGATIVE','NULL','UNKNOWN','PENDING')
                        ),
                        outcome_json TEXT NOT NULL,
                        source_version TEXT NOT NULL,
                        supersedes_episode_id TEXT UNIQUE,
                        created_at TEXT NOT NULL,
                        FOREIGN KEY(supersedes_episode_id)
                            REFERENCES experiences(episode_id) ON DELETE RESTRICT
                    );

                    CREATE TABLE experience_tombstones (
                        episode_id TEXT PRIMARY KEY,
                        reason TEXT NOT NULL,
                        tombstoned_at TEXT NOT NULL,
                        FOREIGN KEY(episode_id)
                            REFERENCES experiences(episode_id) ON DELETE RESTRICT
                    );

                    CREATE TRIGGER experiences_immutable_update
                    BEFORE UPDATE ON experiences
                    BEGIN
                        SELECT RAISE(ABORT, 'experience truth is immutable');
                    END;

                    CREATE TRIGGER experiences_immutable_delete
                    BEFORE DELETE ON experiences
                    BEGIN
                        SELECT RAISE(ABORT, 'experience truth is immutable');
                    END;

                    CREATE TRIGGER experience_tombstones_immutable_update
                    BEFORE UPDATE ON experience_tombstones
                    BEGIN
                        SELECT RAISE(ABORT, 'experience tombstone is immutable');
                    END;

                    CREATE TRIGGER experience_tombstones_immutable_delete
                    BEFORE DELETE ON experience_tombstones
                    BEGIN
                        SELECT RAISE(ABORT, 'experience tombstone is immutable');
                    END;

                    CREATE INDEX idx_experience_decision
                        ON experiences(decision_id, created_at, episode_id);

                    CREATE INDEX idx_experience_outcome
                        ON experiences(outcome_class, created_at, episode_id);
                    """
                )
                connection.execute(
                    "INSERT INTO experience_schema(version, applied_at) VALUES (?, ?)",
                    (self.SCHEMA_VERSION, self._now()),
                )
            connection.commit()

    def append(
        self,
        *,
        episode_id: str,
        decision_id: str,
        evidence_cutoff: str,
        evidence_refs: tuple[str, ...],
        action_ref: str | None,
        execution_ref: str | None,
        outcome_class: str,
        outcome_payload: Any,
        source_version: str,
        supersedes_episode_id: str | None = None,
    ) -> bool:
        episode_id = self._require_text(episode_id, "episode_id")
        decision_id = self._require_text(decision_id, "decision_id")
        canonical_cutoff = self._canonical_cutoff(evidence_cutoff)
        normalized_refs = self._evidence_refs(evidence_refs)
        source_version = self._require_text(source_version, "source_version")
        if supersedes_episode_id is not None:
            supersedes_episode_id = self._require_text(
                supersedes_episode_id, "supersedes_episode_id"
            )
        if action_ref is not None:
            action_ref = self._require_text(action_ref, "action_ref")
        if execution_ref is not None:
            execution_ref = self._require_text(execution_ref, "execution_ref")
        if outcome_class not in OUTCOMES:
            raise ValueError("invalid outcome_class")
        refs_json = self._json(normalized_refs)
        payload_json = self._json(outcome_payload)

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM experiences WHERE episode_id = ?", (episode_id,)
            ).fetchone()
            if existing is not None:
                exact = (
                    existing["decision_id"] == decision_id
                    and existing["evidence_cutoff"] == canonical_cutoff
                    and existing["evidence_refs_json"] == refs_json
                    and existing["action_ref"] == action_ref
                    and existing["execution_ref"] == execution_ref
                    and existing["outcome_class"] == outcome_class
                    and existing["outcome_json"] == payload_json
                    and existing["source_version"] == source_version
                    and existing["supersedes_episode_id"] == supersedes_episode_id
                )
                connection.commit()
                if not exact:
                    raise ValueError("episode_id conflicts with existing immutable truth")
                return False

            if supersedes_episode_id is not None:
                prior = connection.execute(
                    "SELECT decision_id FROM experiences WHERE episode_id = ?",
                    (supersedes_episode_id,),
                ).fetchone()
                if prior is None:
                    connection.rollback()
                    raise ValueError("superseded episode does not exist")
                if prior["decision_id"] != decision_id:
                    connection.rollback()
                    raise ValueError("correction must retain decision lineage")

            connection.execute(
                """
                INSERT INTO experiences(
                    episode_id, decision_id, evidence_cutoff, evidence_refs_json,
                    action_ref, execution_ref, outcome_class, outcome_json,
                    source_version, supersedes_episode_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    episode_id,
                    decision_id,
                    canonical_cutoff,
                    refs_json,
                    action_ref,
                    execution_ref,
                    outcome_class,
                    payload_json,
                    source_version,
                    supersedes_episode_id,
                    self._now(),
                ),
            )
            connection.commit()
        return True

    def tombstone(self, episode_id: str, *, reason: str) -> bool:
        episode_id = self._require_text(episode_id, "episode_id")
        reason = self._require_text(reason, "reason")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute(
                "SELECT 1 FROM experiences WHERE episode_id = ?", (episode_id,)
            ).fetchone() is None:
                connection.rollback()
                raise KeyError(episode_id)
            existing = connection.execute(
                "SELECT reason FROM experience_tombstones WHERE episode_id = ?",
                (episode_id,),
            ).fetchone()
            if existing is not None:
                connection.commit()
                if existing["reason"] != reason:
                    raise ValueError("tombstone reason conflicts with immutable record")
                return False
            connection.execute(
                "INSERT INTO experience_tombstones(episode_id, reason, tombstoned_at) "
                "VALUES (?, ?, ?)",
                (episode_id, reason, self._now()),
            )
            connection.commit()
        return True

    def _row_to_episode(self, row: sqlite3.Row) -> ExperienceEpisode:
        return ExperienceEpisode(
            episode_id=row["episode_id"],
            decision_id=row["decision_id"],
            evidence_cutoff=row["evidence_cutoff"],
            evidence_refs=tuple(json.loads(row["evidence_refs_json"])),
            action_ref=row["action_ref"],
            execution_ref=row["execution_ref"],
            outcome_class=row["outcome_class"],
            outcome_payload=json.loads(row["outcome_json"]),
            source_version=row["source_version"],
            superseded_by=row["superseded_by"],
            tombstoned=bool(row["tombstoned"]),
        )

    def get(self, episode_id: str) -> ExperienceEpisode:
        episode_id = self._require_text(episode_id, "episode_id")
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT e.*,
                       successor.episode_id AS superseded_by,
                       CASE WHEN t.episode_id IS NULL THEN 0 ELSE 1 END AS tombstoned
                FROM experiences e
                LEFT JOIN experiences successor
                  ON successor.supersedes_episode_id = e.episode_id
                LEFT JOIN experience_tombstones t
                  ON t.episode_id = e.episode_id
                WHERE e.episode_id = ?
                """,
                (episode_id,),
            ).fetchone()
        if row is None:
            raise KeyError(episode_id)
        return self._row_to_episode(row)

    def retrieve(
        self,
        *,
        outcome_classes: tuple[str, ...] | None = None,
        include_tombstoned: bool = False,
        latest_lineage_only: bool = True,
        information_cutoff: str | None = None,
        limit: int = 100,
    ) -> list[ExperienceEpisode]:
        if not isinstance(limit, int) or isinstance(limit, bool) or not (1 <= limit <= 1000):
            raise ValueError("limit must be between 1 and 1000")
        classes = OUTCOMES if outcome_classes is None else set(outcome_classes)
        if not classes or not classes <= OUTCOMES:
            raise ValueError("invalid outcome class filter")

        placeholders = ",".join("?" for _ in sorted(classes))
        where = [f"e.outcome_class IN ({placeholders})"]
        params: list[Any] = list(sorted(classes))
        if not include_tombstoned:
            where.append("t.episode_id IS NULL")
        canonical_information_cutoff = (
            None
            if information_cutoff is None
            else self._canonical_cutoff(information_cutoff)
        )
        if canonical_information_cutoff is not None:
            where.append("e.evidence_cutoff <= ?")
            params.append(canonical_information_cutoff)
        if latest_lineage_only:
            if canonical_information_cutoff is None:
                where.append("successor.episode_id IS NULL")
            else:
                where.append(
                    "(successor.episode_id IS NULL OR successor.evidence_cutoff > ?)"
                )
                params.append(canonical_information_cutoff)

        sql = f"""
            SELECT e.*,
                   successor.episode_id AS superseded_by,
                   CASE WHEN t.episode_id IS NULL THEN 0 ELSE 1 END AS tombstoned
            FROM experiences e
            LEFT JOIN experiences successor
              ON successor.supersedes_episode_id = e.episode_id
            LEFT JOIN experience_tombstones t
              ON t.episode_id = e.episode_id
            WHERE {' AND '.join(where)}
            ORDER BY e.created_at, e.episode_id
            LIMIT ?
        """
        params.append(limit)
        with self._connect() as connection:
            rows = connection.execute(sql, tuple(params)).fetchall()
        return [self._row_to_episode(row) for row in rows]

    def outcome_counts(self, *, include_tombstoned: bool = False) -> dict[str, int]:
        episodes = self.retrieve(
            include_tombstoned=include_tombstoned,
            latest_lineage_only=True,
            limit=1000,
        )
        result = {name: 0 for name in sorted(OUTCOMES)}
        for episode in episodes:
            result[episode.outcome_class] += 1
        return result
