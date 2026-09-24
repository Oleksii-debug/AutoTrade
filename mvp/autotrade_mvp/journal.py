"""Durable SQLite journal/outbox primitives for the AutoTrade MVP.

This is a deliberately small standard-library implementation proving the
transactional invariants required before provider execution is trusted.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from typing import Iterable, Mapping, Sequence


SCHEMA_VERSION = 1


class JournalConflict(ValueError):
    """Raised when a command id is reused with different semantic content."""


@dataclass(frozen=True)
class EventRecord:
    sequence: int
    command_id: str
    event_type: str
    payload: dict


@dataclass(frozen=True)
class OutboxRecord:
    outbox_id: str
    event_sequence: int
    destination: str
    payload: dict
    published_at: str | None


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise ValueError("Journal payload must be finite JSON") from error


def _digest(value: object) -> str:
    return sha256(_canonical_json(value).encode("utf-8")).hexdigest()


class SqliteJournal:
    """Append-only command/event journal with durable transactional outbox."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.execute("PRAGMA synchronous = FULL")
        self._ensure_schema()

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "SqliteJournal":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _ensure_schema(self) -> None:
        current = self._connection.execute("PRAGMA user_version").fetchone()[0]
        if current not in (0, SCHEMA_VERSION):
            raise ValueError(f"Unsupported journal schema version: {current}")
        if current == 0:
            with self._connection:
                self._connection.executescript(
                    """
                    CREATE TABLE commands (
                        command_id TEXT PRIMARY KEY,
                        semantic_hash TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    );

                    CREATE TABLE events (
                        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                        command_id TEXT NOT NULL REFERENCES commands(command_id),
                        event_type TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    );

                    CREATE INDEX events_command_id_idx
                    ON events(command_id, sequence);

                    CREATE TABLE outbox (
                        outbox_id TEXT PRIMARY KEY,
                        event_sequence INTEGER NOT NULL REFERENCES events(sequence),
                        destination TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        published_at TEXT NULL
                    );

                    CREATE INDEX outbox_pending_idx
                    ON outbox(published_at, event_sequence);
                    """
                )
                self._connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    @property
    def schema_version(self) -> int:
        return int(self._connection.execute("PRAGMA user_version").fetchone()[0])

    def append(
        self,
        command_id: str,
        command_payload: Mapping[str, object],
        events: Sequence[Mapping[str, object]],
        *,
        outbox: Sequence[Mapping[str, object]] = (),
    ) -> tuple[int, ...]:
        """Atomically append one command, its events and optional outbox rows.

        Repeating the exact same command is idempotent. Reusing the same
        command id with changed payload/events/outbox is a hard conflict.
        """

        if not command_id or not command_id.strip():
            raise ValueError("command_id is required")
        normalized_events = []
        for item in events:
            event_type = item.get("event_type")
            payload = item.get("payload")
            if not isinstance(event_type, str) or not event_type.strip():
                raise ValueError("Each event requires a non-empty event_type")
            if not isinstance(payload, Mapping):
                raise ValueError("Each event payload must be an object")
            normalized_events.append(
                {"event_type": event_type, "payload": dict(payload)}
            )
        if not normalized_events:
            raise ValueError("At least one event is required")

        normalized_outbox = []
        for item in outbox:
            event_index = item.get("event_index")
            destination = item.get("destination")
            payload = item.get("payload")
            if not isinstance(event_index, int) or not 0 <= event_index < len(normalized_events):
                raise ValueError("Outbox event_index is outside the event list")
            if not isinstance(destination, str) or not destination.strip():
                raise ValueError("Outbox destination is required")
            if not isinstance(payload, Mapping):
                raise ValueError("Outbox payload must be an object")
            normalized_outbox.append(
                {
                    "event_index": event_index,
                    "destination": destination,
                    "payload": dict(payload),
                }
            )

        semantic_request = {
            "command_payload": dict(command_payload),
            "events": normalized_events,
            "outbox": normalized_outbox,
        }
        semantic_hash = _digest(semantic_request)
        command_json = _canonical_json(dict(command_payload))
        event_json = [
            (item["event_type"], _canonical_json(item["payload"]))
            for item in normalized_events
        ]
        outbox_json = [
            (
                item["event_index"],
                item["destination"],
                _canonical_json(item["payload"]),
            )
            for item in normalized_outbox
        ]
        created_at = datetime.now(timezone.utc).isoformat()

        connection = self._connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            existing = connection.execute(
                "SELECT semantic_hash FROM commands WHERE command_id = ?",
                (command_id,),
            ).fetchone()
            if existing is not None:
                if existing["semantic_hash"] != semantic_hash:
                    raise JournalConflict(
                        "command_id was already committed with different content"
                    )
                rows = connection.execute(
                    "SELECT sequence FROM events WHERE command_id = ? ORDER BY sequence",
                    (command_id,),
                ).fetchall()
                connection.commit()
                return tuple(int(row["sequence"]) for row in rows)

            connection.execute(
                """
                INSERT INTO commands(command_id, semantic_hash, payload_json, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (command_id, semantic_hash, command_json, created_at),
            )
            sequences: list[int] = []
            for event_type, payload_json in event_json:
                cursor = connection.execute(
                    """
                    INSERT INTO events(command_id, event_type, payload_json, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (command_id, event_type, payload_json, created_at),
                )
                sequences.append(int(cursor.lastrowid))

            for ordinal, (event_index, destination, payload_json) in enumerate(outbox_json):
                event_sequence = sequences[event_index]
                outbox_id = (
                    "outbox-"
                    + sha256(
                        f"{command_id}:{event_sequence}:{destination}:{ordinal}".encode("utf-8")
                    ).hexdigest()[:24]
                )
                connection.execute(
                    """
                    INSERT INTO outbox(
                        outbox_id, event_sequence, destination, payload_json,
                        created_at, published_at
                    )
                    VALUES (?, ?, ?, ?, ?, NULL)
                    """,
                    (
                        outbox_id,
                        event_sequence,
                        destination,
                        payload_json,
                        created_at,
                    ),
                )
            connection.commit()
            return tuple(sequences)
        except Exception:
            connection.rollback()
            raise

    def read_events(self, *, after_sequence: int = 0) -> list[EventRecord]:
        rows = self._connection.execute(
            """
            SELECT sequence, command_id, event_type, payload_json
            FROM events
            WHERE sequence > ?
            ORDER BY sequence
            """,
            (after_sequence,),
        ).fetchall()
        return [
            EventRecord(
                sequence=int(row["sequence"]),
                command_id=row["command_id"],
                event_type=row["event_type"],
                payload=json.loads(row["payload_json"]),
            )
            for row in rows
        ]

    def pending_outbox(self, *, limit: int = 100) -> list[OutboxRecord]:
        if limit < 1:
            raise ValueError("limit must be positive")
        rows = self._connection.execute(
            """
            SELECT outbox_id, event_sequence, destination, payload_json, published_at
            FROM outbox
            WHERE published_at IS NULL
            ORDER BY event_sequence, outbox_id
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [
            OutboxRecord(
                outbox_id=row["outbox_id"],
                event_sequence=int(row["event_sequence"]),
                destination=row["destination"],
                payload=json.loads(row["payload_json"]),
                published_at=row["published_at"],
            )
            for row in rows
        ]

    def mark_outbox_published(self, outbox_id: str) -> bool:
        if not outbox_id:
            raise ValueError("outbox_id is required")
        published_at = datetime.now(timezone.utc).isoformat()
        with self._connection:
            cursor = self._connection.execute(
                """
                UPDATE outbox
                SET published_at = ?
                WHERE outbox_id = ? AND published_at IS NULL
                """,
                (published_at, outbox_id),
            )
        return cursor.rowcount == 1

    def command_count(self) -> int:
        return int(self._connection.execute("SELECT COUNT(*) FROM commands").fetchone()[0])

    def event_count(self) -> int:
        return int(self._connection.execute("SELECT COUNT(*) FROM events").fetchone()[0])
