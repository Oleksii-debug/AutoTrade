"""Append-only cumulative experience memory with causal/permission retrieval."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass

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
    if not isinstance(value, str):
        raise TypeError(f"{name} must be text")
    if not value.strip():
        raise ValueError(f"{name} is required")
    return value.strip()


def _identifier(value: str | None = None) -> str:
    return str(UUID(value)) if value is not None else str(uuid4())


def _reject_binary_float(value: Any, *, path: str = "$") -> None:
    if isinstance(value, float):
        raise TypeError(
            f"binary float is not permitted in immutable experience evidence: {path}"
        )
    if isinstance(value, dict):
        for key, item in value.items():
            _reject_binary_float(item, path=f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_binary_float(item, path=f"{path}[{index}]")


def _canonical(value: Any) -> str:
    _reject_binary_float(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _hash(value: Any) -> str:
    return "sha256:" + sha256(_canonical(value).encode("utf-8")).hexdigest()


class MemoryConflict(ValueError):
    pass


class MemoryIntegrityError(MemoryConflict):
    """Persisted memory bytes no longer match their immutable identity."""


def _stored_time(value: Any, *, name: str) -> datetime:
    if not isinstance(value, str):
        raise MemoryIntegrityError(f"{name} must be stored as text")
    try:
        parsed = datetime.fromisoformat(value)
        normalized = _time(parsed, name=name).isoformat()
    except (TypeError, ValueError) as error:
        raise MemoryIntegrityError(f"{name} is not a valid stored timestamp") from error
    if normalized != value:
        raise MemoryIntegrityError(f"{name} is not canonical UTC time")
    return parsed.astimezone(timezone.utc)


def _stored_text(value: Any, *, name: str) -> str:
    try:
        normalized = _text(value, name=name)
    except (TypeError, ValueError) as error:
        raise MemoryIntegrityError(f"{name} is not valid stored text") from error
    if normalized != value:
        raise MemoryIntegrityError(f"{name} is not canonical stored text")
    return normalized


def _stored_json(value: Any, *, name: str) -> Any:
    if not isinstance(value, str):
        raise MemoryIntegrityError(f"{name} must be stored as JSON text")
    try:
        parsed = json.loads(value)
        canonical = _canonical(parsed)
    except (json.JSONDecodeError, TypeError, ValueError) as error:
        raise MemoryIntegrityError(f"{name} is not valid canonical JSON") from error
    if canonical != value:
        raise MemoryIntegrityError(f"{name} is not canonical JSON")
    return parsed


@dataclass(frozen=True)
class CoveragePopulationSnapshot:
    """Immutable identity for one canonical qualification-facing memory query."""

    causal_cutoff: str
    permission_classes: tuple[str, ...]
    task: str | None
    instrument_family: str | None
    rows: tuple[dict[str, Any], ...]
    eligible_count: int
    root_hash: str

    def __post_init__(self) -> None:
        cutoff = _stored_time(self.causal_cutoff, name="coverage causal_cutoff")
        if cutoff.isoformat() != self.causal_cutoff:
            raise MemoryIntegrityError("coverage causal_cutoff is not canonical")
        if not isinstance(self.permission_classes, tuple) or not self.permission_classes:
            raise MemoryIntegrityError("coverage permission_classes must be a non-empty tuple")
        normalized_permissions = tuple(
            _stored_text(value, name="coverage permission_class")
            for value in self.permission_classes
        )
        if normalized_permissions != self.permission_classes:
            raise MemoryIntegrityError("coverage permission_classes are not canonical")
        if tuple(sorted(set(normalized_permissions))) != normalized_permissions:
            raise MemoryIntegrityError("coverage permission_classes must be sorted and unique")
        if self.task is not None and _stored_text(self.task, name="coverage task") != self.task:
            raise MemoryIntegrityError("coverage task is not canonical")
        if (
            self.instrument_family is not None
            and _stored_text(
                self.instrument_family,
                name="coverage instrument_family",
            )
            != self.instrument_family
        ):
            raise MemoryIntegrityError("coverage instrument_family is not canonical")
        # Detach the snapshot from mutable caller-owned row objects at creation.
        # The stored rows remain ordinary JSON mappings for compatibility, so every
        # qualification consumer must still re-verify the root through verified_rows().
        object.__setattr__(self, "rows", self.verified_rows())

    def verified_rows(self) -> tuple[dict[str, Any], ...]:
        """Recompute the canonical population root and return detached verified rows.

        A frozen dataclass does not make nested dict/list values immutable.  This
        boundary therefore canonicalizes a fresh copy and verifies the root every
        time a qualification consumer asks for population rows.  Post-construction
        top-level or nested mutation can never be consumed under the stale root.
        """

        if not isinstance(self.rows, tuple):
            raise MemoryIntegrityError("coverage rows must be an immutable tuple")
        try:
            copied = json.loads(_canonical(self.rows))
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise MemoryIntegrityError("coverage rows are not canonical JSON") from error
        if not isinstance(copied, list) or any(not isinstance(row, dict) for row in copied):
            raise MemoryIntegrityError("coverage rows must contain canonical mappings")
        rows = tuple(copied)
        if (
            not isinstance(self.eligible_count, int)
            or isinstance(self.eligible_count, bool)
            or self.eligible_count < 0
            or self.eligible_count != len(rows)
        ):
            raise MemoryIntegrityError("coverage eligible_count does not match rows")
        episode_ids = tuple(row.get("episode_id") for row in rows)
        if any(not isinstance(value, str) or not value for value in episode_ids):
            raise MemoryIntegrityError("coverage rows require episode identities")
        if len(set(episode_ids)) != len(episode_ids):
            raise MemoryIntegrityError("coverage rows contain duplicate episode identities")
        expected = _hash(
            {
                "schema_version": 1,
                "causal_cutoff": self.causal_cutoff,
                "permission_classes": self.permission_classes,
                "task": self.task,
                "instrument_family": self.instrument_family,
                "eligible_count": self.eligible_count,
                "rows": rows,
            }
        )
        if self.root_hash != expected:
            raise MemoryIntegrityError(
                "coverage population root does not match canonical ExperienceMemory snapshot"
            )
        return rows


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
            tombstone_columns = {
                row["name"]
                for row in con.execute("PRAGMA table_info(tombstones)").fetchall()
            }
            if "tombstone_hash" not in tombstone_columns:
                # Deliberately nullable: pre-migration tombstones are not silently
                # relabelled as cryptographically witnessed history.
                con.execute("ALTER TABLE tombstones ADD COLUMN tombstone_hash TEXT")

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

    @staticmethod
    def _verified_episode(row: sqlite3.Row) -> dict[str, Any]:
        decision = _stored_time(row["decision_time"], name="decision_time")
        cutoff = _stored_time(row["information_cutoff"], name="information_cutoff")
        if cutoff > decision:
            raise MemoryIntegrityError(
                "persisted information cutoff cannot be after decision time"
            )
        task = _stored_text(row["task"], name="task")
        regime = _stored_text(row["regime"], name="regime")
        family = _stored_text(row["instrument_family"], name="instrument_family")
        permission = _stored_text(row["permission_class"], name="permission_class")
        payload = _stored_json(row["payload_json"], name="episode payload")
        if not isinstance(payload, dict) or not payload:
            raise MemoryIntegrityError("episode payload must be a non-empty object")
        required = {
            "evidence_refs",
            "intended_action",
            "actual_execution",
            "outcome",
            "costs",
        }
        if required - set(payload):
            raise MemoryIntegrityError("episode payload is missing required fields")
        if not isinstance(payload["evidence_refs"], list) or not payload["evidence_refs"]:
            raise MemoryIntegrityError("episode evidence references are invalid")
        expected = _hash(
            {
                "decision_time": row["decision_time"],
                "information_cutoff": row["information_cutoff"],
                "task": task,
                "regime": regime,
                "instrument_family": family,
                "permission_class": permission,
                "payload": payload,
            }
        )
        if row["episode_hash"] != expected:
            raise MemoryIntegrityError("episode integrity mismatch")
        return {
            "decision": decision,
            "cutoff": cutoff,
            "task": task,
            "regime": regime,
            "instrument_family": family,
            "permission_class": permission,
            "payload": payload,
        }

    @staticmethod
    def _verified_correction(
        row: sqlite3.Row,
        *,
        episode_id: str,
    ) -> tuple[dict[str, Any], datetime]:
        if row["episode_id"] != episode_id:
            raise MemoryIntegrityError("correction episode binding mismatch")
        payload = _stored_json(row["payload_json"], name="correction payload")
        if not isinstance(payload, dict) or not payload:
            raise MemoryIntegrityError("correction payload must be a non-empty object")
        created = _stored_time(row["created_at"], name="correction created_at")
        if row["available_at"] is None:
            # Pre-causal rows used a payload-only checksum and therefore cannot
            # cryptographically prove their original episode parent. Preserve
            # the bytes for explicit migration/audit, but never present them as
            # trusted causal correction evidence.
            availability = created
            legacy_expected = _hash(payload)
            if row["correction_hash"] == legacy_expected:
                raise MemoryIntegrityError(
                    "legacy correction lacks episode-bound integrity; explicit recovery is required"
                )
            raise MemoryIntegrityError("correction integrity mismatch")

        availability = _stored_time(
            row["available_at"],
            name="correction available_at",
        )
        expected = _hash(
            {
                "correction_id": row["correction_id"],
                "episode_id": row["episode_id"],
                "available_at": row["available_at"],
                "payload": payload,
            }
        )
        if row["correction_hash"] != expected:
            raise MemoryIntegrityError("correction integrity mismatch")
        return payload, availability

    @staticmethod
    def _verified_tombstone(
        row: sqlite3.Row,
        *,
        episode_id: str,
    ) -> dict[str, str]:
        if row["episode_id"] != episode_id:
            raise MemoryIntegrityError("tombstone episode binding mismatch")
        if row["tombstone_hash"] is None:
            raise MemoryIntegrityError(
                "legacy tombstone lacks integrity identity; explicit recovery is required"
            )
        reason = _stored_text(row["reason"], name="tombstone reason")
        created = _stored_time(row["created_at"], name="tombstone created_at")
        expected = _hash(
            {
                "episode_id": episode_id,
                "reason": reason,
                "created_at": row["created_at"],
            }
        )
        if row["tombstone_hash"] != expected:
            raise MemoryIntegrityError("tombstone integrity mismatch")
        return {"reason": reason, "created_at": created.isoformat()}

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
                "SELECT * FROM episodes WHERE episode_id=?",
                (episode,),
            ).fetchone()
            if episode_row is None:
                raise KeyError(episode)
            verified_episode = self._verified_episode(episode_row)
            episode_decision = verified_episode["decision"]
            source_payload = verified_episode["payload"]
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
                        "correction_id": identifier,
                        "episode_id": episode,
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
                    "correction_id": identifier,
                    "episode_id": episode,
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
        created_at = datetime.now(timezone.utc).isoformat()
        digest = _hash(
            {
                "episode_id": episode,
                "reason": why,
                "created_at": created_at,
            }
        )
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            source = con.execute(
                "SELECT * FROM episodes WHERE episode_id=?",
                (episode,),
            ).fetchone()
            if source is None:
                raise KeyError(episode)
            self._verified_episode(source)
            con.execute(
                """
                INSERT INTO tombstones(
                    tombstone_id,episode_id,reason,created_at,tombstone_hash
                ) VALUES(?,?,?,?,?)
                """,
                (identifier, episode, why, created_at, digest),
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
        if type(include_tombstoned) is not bool:
            raise TypeError("include_tombstoned must be boolean")
        normalized_permissions: set[str] = set()
        for permission in granted_permissions:
            normalized = _text(permission, name="granted_permission")
            if normalized != permission:
                raise ValueError("granted_permissions must contain canonical text")
            normalized_permissions.add(normalized)
        normalized_task = None if task is None else _text(task, name="task")
        normalized_regime = None if regime is None else _text(regime, name="regime")
        normalized_family = (
            None
            if instrument_family is None
            else _text(instrument_family, name="instrument_family")
        )

        results: list[dict[str, Any]] = []
        with self._connect() as con:
            # Verify immutable rows before applying metadata filters. A raw DB edit
            # must not be able to hide a record merely by moving task/regime/time.
            rows = con.execute("SELECT * FROM episodes ORDER BY episode_id").fetchall()
            for row in rows:
                verified = self._verified_episode(row)
                if verified["cutoff"] > cutoff or verified["decision"] > cutoff:
                    continue
                if normalized_task is not None and verified["task"] != normalized_task:
                    continue
                if normalized_regime is not None and verified["regime"] != normalized_regime:
                    continue
                if (
                    normalized_family is not None
                    and verified["instrument_family"] != normalized_family
                ):
                    continue
                if not self._permission_allowed(
                    verified["permission_class"],
                    normalized_permissions,
                ):
                    continue

                tombstone_rows = con.execute(
                    "SELECT * FROM tombstones WHERE episode_id=? ORDER BY created_at,tombstone_id",
                    (row["episode_id"],),
                ).fetchall()
                tombstones = [
                    self._verified_tombstone(item, episode_id=row["episode_id"])
                    for item in tombstone_rows
                ]
                if tombstones and not include_tombstoned:
                    continue

                correction_rows = con.execute(
                    """
                    SELECT * FROM corrections
                    WHERE episode_id=?
                    ORDER BY COALESCE(available_at, created_at), created_at, correction_id
                    """,
                    (row["episode_id"],),
                ).fetchall()
                visible_corrections: list[dict[str, Any]] = []
                for correction_row in correction_rows:
                    correction, available = self._verified_correction(
                        correction_row,
                        episode_id=row["episode_id"],
                    )
                    if available <= cutoff:
                        visible_corrections.append(correction)

                results.append(
                    {
                        "episode_id": row["episode_id"],
                        "episode_hash": row["episode_hash"],
                        "decision_time": row["decision_time"],
                        "information_cutoff": row["information_cutoff"],
                        "task": verified["task"],
                        "regime": verified["regime"],
                        "instrument_family": verified["instrument_family"],
                        "permission_class": verified["permission_class"],
                        "payload": verified["payload"],
                        "corrections": visible_corrections,
                        "tombstones": tombstones,
                    }
                )
        results.sort(key=lambda item: (item["decision_time"], item["episode_id"]))
        return tuple(results)

    def source_episode(
        self,
        episode_id: str,
        *,
        information_cutoff: datetime,
        granted_permissions: set[str],
        include_tombstoned: bool = False,
    ) -> dict[str, Any]:
        """Read one immutable source episode without bypassing retrieval authority."""
        identifier = _identifier(episode_id)
        cutoff = _time(information_cutoff, name="information_cutoff")
        if not isinstance(granted_permissions, set):
            raise TypeError("granted_permissions must be a set")
        if type(include_tombstoned) is not bool:
            raise TypeError("include_tombstoned must be boolean")
        normalized_permissions: set[str] = set()
        for permission in granted_permissions:
            normalized = _text(permission, name="granted_permission")
            if normalized != permission:
                raise ValueError("granted_permissions must contain canonical text")
            normalized_permissions.add(normalized)

        with self._connect() as con:
            row = con.execute(
                "SELECT * FROM episodes WHERE episode_id=?",
                (identifier,),
            ).fetchone()
            if row is None:
                raise KeyError(identifier)
            verified = self._verified_episode(row)
            if verified["cutoff"] > cutoff or verified["decision"] > cutoff:
                raise PermissionError("episode is not causally available at information_cutoff")
            if not self._permission_allowed(
                verified["permission_class"],
                normalized_permissions,
            ):
                raise PermissionError("episode permission is not granted")

            tombstone_rows = con.execute(
                "SELECT * FROM tombstones WHERE episode_id=? ORDER BY created_at,tombstone_id",
                (identifier,),
            ).fetchall()
            tombstones = [
                self._verified_tombstone(item, episode_id=identifier)
                for item in tombstone_rows
            ]
            if tombstones and not include_tombstoned:
                raise PermissionError("episode is tombstoned")

            return {
                "episode_id": row["episode_id"],
                "episode_hash": row["episode_hash"],
                "payload": verified["payload"],
                "information_cutoff": row["information_cutoff"],
                "permission_class": verified["permission_class"],
                "tombstones": tombstones,
            }

    def coverage_population_snapshot(
        self,
        *,
        causal_cutoff: datetime,
        granted_permissions: set[str],
        task: str | None = None,
        instrument_family: str | None = None,
    ) -> CoveragePopulationSnapshot:
        """Freeze the complete canonical population and its exact query identity."""

        rows = self.coverage_population(
            causal_cutoff=causal_cutoff,
            granted_permissions=granted_permissions,
            task=task,
            instrument_family=instrument_family,
        )
        cutoff = _iso(causal_cutoff)
        permissions = tuple(
            sorted(
                _text(value, name="granted_permission")
                for value in granted_permissions
            )
        )
        normalized_task = None if task is None else _text(task, name="task")
        normalized_family = (
            None
            if instrument_family is None
            else _text(instrument_family, name="instrument_family")
        )
        payload = {
            "schema_version": 1,
            "causal_cutoff": cutoff,
            "permission_classes": permissions,
            "task": normalized_task,
            "instrument_family": normalized_family,
            "eligible_count": len(rows),
            "rows": rows,
        }
        return CoveragePopulationSnapshot(
            causal_cutoff=cutoff,
            permission_classes=permissions,
            task=normalized_task,
            instrument_family=normalized_family,
            rows=rows,
            eligible_count=len(rows),
            root_hash=_hash(payload),
        )

    def coverage_population(
        self,
        *,
        causal_cutoff: datetime,
        granted_permissions: set[str],
        task: str | None = None,
        instrument_family: str | None = None,
    ) -> tuple[dict[str, Any], ...]:
        """Return verified, reproducible episode facts available at a causal cutoff.

        This projection is intentionally qualification-facing.  Unlike normal
        retrieval, it keeps tombstoned episodes visible as population facts so a
        scientific manifest must account for them explicitly instead of silently
        shrinking the eligible population.  Corrections and tombstones that first
        become available after the cutoff cannot rewrite the historical snapshot.
        """

        cutoff = _time(causal_cutoff, name="causal_cutoff")
        if not isinstance(granted_permissions, set):
            raise TypeError("granted_permissions must be a set")
        normalized_permissions: set[str] = set()
        for permission in granted_permissions:
            normalized = _text(permission, name="granted_permission")
            if normalized != permission:
                raise ValueError("granted_permissions must contain canonical text")
            normalized_permissions.add(normalized)
        if not normalized_permissions:
            raise ValueError("granted_permissions must not be empty")
        normalized_task = None if task is None else _text(task, name="task")
        normalized_family = (
            None
            if instrument_family is None
            else _text(instrument_family, name="instrument_family")
        )

        population: list[dict[str, Any]] = []
        with self._connect() as con:
            rows = con.execute("SELECT * FROM episodes ORDER BY episode_id").fetchall()
            for row in rows:
                verified = self._verified_episode(row)
                created = _stored_time(row["created_at"], name="episode created_at")
                if (
                    verified["cutoff"] > cutoff
                    or verified["decision"] > cutoff
                    or created > cutoff
                ):
                    continue
                if normalized_task is not None and verified["task"] != normalized_task:
                    continue
                if (
                    normalized_family is not None
                    and verified["instrument_family"] != normalized_family
                ):
                    continue
                if not self._permission_allowed(
                    verified["permission_class"],
                    normalized_permissions,
                ):
                    continue

                effective_payload = json.loads(_canonical(verified["payload"]))
                correction_lineage: list[dict[str, Any]] = []
                correction_rows = con.execute(
                    """
                    SELECT * FROM corrections
                    WHERE episode_id=?
                    ORDER BY COALESCE(available_at, created_at), created_at, correction_id
                    """,
                    (row["episode_id"],),
                ).fetchall()
                for correction_row in correction_rows:
                    correction, available = self._verified_correction(
                        correction_row,
                        episode_id=row["episode_id"],
                    )
                    if available > cutoff:
                        continue
                    supersedes = correction["supersedes_fields"]
                    for field in supersedes:
                        effective_payload[field] = correction[field]
                    correction_lineage.append(
                        {
                            "correction_id": correction_row["correction_id"],
                            "correction_hash": correction_row["correction_hash"],
                            "available_at": available.isoformat(),
                            "evidence_ref": correction["evidence_ref"],
                            "supersedes_fields": tuple(supersedes),
                        }
                    )

                tombstone_lineage: list[dict[str, Any]] = []
                tombstone_rows = con.execute(
                    """
                    SELECT * FROM tombstones
                    WHERE episode_id=?
                    ORDER BY created_at,tombstone_id
                    """,
                    (row["episode_id"],),
                ).fetchall()
                for tombstone_row in tombstone_rows:
                    tombstone = self._verified_tombstone(
                        tombstone_row,
                        episode_id=row["episode_id"],
                    )
                    tombstone_time = _stored_time(
                        tombstone_row["created_at"],
                        name="tombstone created_at",
                    )
                    if tombstone_time > cutoff:
                        continue
                    tombstone_lineage.append(
                        {
                            "tombstone_id": tombstone_row["tombstone_id"],
                            "tombstone_hash": tombstone_row["tombstone_hash"],
                            "reason": tombstone["reason"],
                            "created_at": tombstone_time.isoformat(),
                        }
                    )

                population.append(
                    {
                        "episode_id": row["episode_id"],
                        "episode_hash": row["episode_hash"],
                        "decision_time": row["decision_time"],
                        "information_cutoff": row["information_cutoff"],
                        "created_at": row["created_at"],
                        "task": verified["task"],
                        "regime": verified["regime"],
                        "instrument_family": verified["instrument_family"],
                        "permission_class": verified["permission_class"],
                        "effective_payload": effective_payload,
                        "correction_lineage": tuple(correction_lineage),
                        "tombstone_lineage": tuple(tombstone_lineage),
                    }
                )
        population.sort(
            key=lambda item: (item["decision_time"], item["episode_id"])
        )
        return tuple(population)
