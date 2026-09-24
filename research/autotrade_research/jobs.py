"""Durable, bounded research-job coordination.

This store is intentionally restricted to research work. It cannot submit or
retry financial orders. Lease generation fences stale workers, and final result
publication is compare-and-swap on the current generation.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from typing import Any
from uuid import UUID, uuid4, uuid5

from .artifacts.store import ArtifactStore


FINAL_STATES = {"SUCCEEDED", "FAILED", "CANCELLED"}
ALLOWED_STATES = {"QUEUED", "RUNNING", "WAITING_EXTERNAL", *FINAL_STATES}


class JobError(RuntimeError):
    pass


class JobConflictError(JobError):
    pass


class JobLeaseError(JobError):
    pass


class JobBudgetError(JobError):
    pass


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return _utc(value).isoformat().replace("+00:00", "Z")


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _require_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty text")
    return value.strip()



def _require_immutable_artifact_ref(value: Any, name: str) -> str:
    reference = _require_text(value, name)
    prefix = "artifact:"
    marker = "@sha256:"
    if not reference.startswith(prefix) or marker not in reference:
        raise ValueError(f"{name} must bind an immutable artifact and SHA-256 digest")
    artifact_id, digest = reference[len(prefix):].split(marker, 1)
    try:
        UUID(artifact_id)
    except (ValueError, TypeError, AttributeError) as error:
        raise ValueError(f"{name} artifact id must be a UUID") from error
    if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
        raise ValueError(f"{name} must use a canonical lowercase SHA-256 digest")
    return reference


def _verify_artifact_ref(
    artifact_store: ArtifactStore,
    reference: str,
) -> bool:
    if not isinstance(artifact_store, ArtifactStore):
        raise TypeError("artifact_store must be ArtifactStore")
    normalized = _require_immutable_artifact_ref(reference, "artifact_ref")
    artifact_id, digest = normalized[len("artifact:"):].split("@sha256:", 1)
    expected_hash = "sha256:" + digest
    try:
        manifest = artifact_store.load_manifest(artifact_id)
        payload = artifact_store.read_bytes(artifact_id)
    except (FileNotFoundError, UnicodeError, ValueError, TypeError):
        return False
    manifest_hash = manifest.get("manifest_hash")
    return (
        manifest.get("sha256") == expected_hash
        and "sha256:" + sha256(payload).hexdigest() == expected_hash
        and isinstance(manifest_hash, str)
        and len(manifest_hash) == 71
        and manifest_hash.startswith("sha256:")
        and all(ch in "0123456789abcdef" for ch in manifest_hash[7:])
    )


def _verify_external_resolution_artifact(
    *,
    artifact_store: ArtifactStore,
    evidence_ref: str,
    job_id: str,
    generation: int,
    verdict: str,
) -> bool:
    if not _verify_artifact_ref(artifact_store, evidence_ref):
        return False
    reference = _require_immutable_artifact_ref(evidence_ref, "evidence_ref")
    artifact_id = reference[len("artifact:"):].split("@sha256:", 1)[0]
    try:
        manifest = artifact_store.load_manifest(artifact_id)
        payload = artifact_store.read_bytes(artifact_id)
        proof = json.loads(payload.decode("utf-8"))
    except (FileNotFoundError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
        return False
    if manifest.get("media_type") != "application/json":
        return False
    expected = {
        "artifact_kind": "RESEARCH_JOB_EXTERNAL_RESOLUTION",
        "job_id": job_id,
        "generation": generation,
        "verdict": verdict,
    }
    metadata = manifest.get("metadata")
    if not isinstance(metadata, dict) or any(
        metadata.get(key) != value for key, value in expected.items()
    ):
        return False
    if not isinstance(proof, dict) or any(
        proof.get(key) != value for key, value in expected.items()
    ):
        return False
    return proof.get("schema_version") == "1.0.0"

def _require_research_kind(kind: str) -> str:
    normalized = _require_text(kind, "kind")
    if not normalized.lower().startswith("research."):
        raise ValueError("durable job kind must use the research.* namespace")
    return normalized


def _validate_hashes(input_hashes: list[str]) -> list[str]:
    if not isinstance(input_hashes, list) or not input_hashes:
        raise ValueError("input_hashes must be a non-empty list")
    result: list[str] = []
    for digest in input_hashes:
        value = _require_text(digest, "input hash")
        if not value.startswith("sha256:") or len(value) != 71:
            raise ValueError("input hash must be a sha256 digest")
        hex_part = value[7:]
        if any(ch not in "0123456789abcdef" for ch in hex_part):
            raise ValueError("input hash must be lowercase hexadecimal")
        result.append(value)
    return result


def _validate_budget(budget: dict[str, int | float]) -> dict[str, float]:
    if not isinstance(budget, dict) or not budget:
        raise ValueError("resource_budget must be a non-empty object")
    normalized: dict[str, float] = {}
    for name, raw in budget.items():
        key = _require_text(name, "budget key")
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ValueError("resource budget values must be numeric")
        value = float(raw)
        if value < 0 or value == float("inf") or value != value:
            raise ValueError("resource budget values must be finite and non-negative")
        normalized[key] = value
    return normalized


class ResearchJobStore:
    SCHEMA_VERSION = 3

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            versions = [row[0] for row in connection.execute("SELECT version FROM schema_migrations")]
            if any(version > self.SCHEMA_VERSION for version in versions):
                connection.rollback()
                raise JobError("job database schema is newer than this runtime")
            if 1 not in versions:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS jobs (
                        job_id TEXT PRIMARY KEY,
                        kind TEXT NOT NULL,
                        dedupe_key TEXT NOT NULL UNIQUE,
                        input_hashes_json TEXT NOT NULL,
                        state TEXT NOT NULL,
                        generation INTEGER NOT NULL CHECK (generation > 0),
                        attempt INTEGER NOT NULL CHECK (attempt >= 0),
                        owner TEXT,
                        lease_until TEXT,
                        checkpoint_ref TEXT,
                        resource_budget_json TEXT NOT NULL,
                        resource_usage_json TEXT NOT NULL,
                        output_refs_json TEXT NOT NULL,
                        error_json TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_jobs_claim
                        ON jobs(state, lease_until, created_at, job_id);
                    """
                )
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (1, _iso(datetime.now(timezone.utc))),
                )
                versions.append(1)
            if 2 not in versions:
                columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info(jobs)").fetchall()
                }
                if "lease_requeueable" not in columns:
                    connection.execute(
                        "ALTER TABLE jobs ADD COLUMN lease_requeueable INTEGER NOT NULL DEFAULT 0 "
                        "CHECK (lease_requeueable IN (0, 1))"
                    )
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (2, _iso(datetime.now(timezone.utc))),
                )
                versions.append(2)
            if 3 not in versions:
                columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info(jobs)").fetchall()
                }
                if "external_resolution_json" not in columns:
                    connection.execute(
                        "ALTER TABLE jobs ADD COLUMN external_resolution_json TEXT"
                    )
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (3, _iso(datetime.now(timezone.utc))),
                )
            connection.commit()

    @staticmethod
    def _row_record(row: sqlite3.Row) -> dict[str, Any]:
        record = {
            "job_id": row["job_id"],
            "kind": row["kind"],
            "input_hashes": json.loads(row["input_hashes_json"]),
            "dedupe_key": row["dedupe_key"],
            "state": row["state"],
            "generation": str(row["generation"]),
            "attempt": row["attempt"],
            "resource_budget": json.loads(row["resource_budget_json"]),
            "resource_usage": json.loads(row["resource_usage_json"]),
            "lease_requeueable": bool(row["lease_requeueable"]),
            "output_refs": json.loads(row["output_refs_json"]),
        }
        if row["owner"] is not None:
            record["owner"] = row["owner"]
        if row["lease_until"] is not None:
            record["lease_until"] = row["lease_until"]
        if row["checkpoint_ref"] is not None:
            record["checkpoint_ref"] = row["checkpoint_ref"]
        if row["error_json"] is not None:
            record["error"] = json.loads(row["error_json"])
        if "external_resolution_json" in row.keys() and row["external_resolution_json"] is not None:
            record["external_resolution"] = json.loads(row["external_resolution_json"])
        return record

    def enqueue(
        self,
        *,
        kind: str,
        dedupe_key: str,
        input_hashes: list[str],
        resource_budget: dict[str, int | float],
        lease_requeueable: bool = False,
        job_id: str | None = None,
        now: datetime | None = None,
    ) -> tuple[dict[str, Any], bool]:
        job_kind = _require_research_kind(kind)
        key = _require_text(dedupe_key, "dedupe_key")
        hashes = _validate_hashes(input_hashes)
        budget = _validate_budget(resource_budget)
        if not isinstance(lease_requeueable, bool):
            raise ValueError("lease_requeueable must be boolean")
        current = _utc(now or datetime.now(timezone.utc))
        identifier = str(uuid4()) if job_id is None else str(UUID(_require_text(job_id, "job_id")))

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute("SELECT * FROM jobs WHERE dedupe_key = ?", (key,)).fetchone()
            if existing is not None:
                same = (
                    existing["kind"] == job_kind
                    and json.loads(existing["input_hashes_json"]) == hashes
                    and json.loads(existing["resource_budget_json"]) == budget
                    and bool(existing["lease_requeueable"]) is lease_requeueable
                )
                if not same:
                    connection.rollback()
                    raise JobConflictError("dedupe_key conflicts with another research job")
                connection.commit()
                return self._row_record(existing), False

            connection.execute(
                """
                INSERT INTO jobs(
                    job_id, kind, dedupe_key, input_hashes_json, state, generation,
                    attempt, owner, lease_until, checkpoint_ref, resource_budget_json,
                    resource_usage_json, output_refs_json, error_json, created_at, updated_at,
                    lease_requeueable
                ) VALUES (?, ?, ?, ?, 'QUEUED', 1, 0, NULL, NULL, NULL, ?, '{}', '[]', NULL, ?, ?, ?)
                """,
                (
                    identifier,
                    job_kind,
                    key,
                    _json(hashes),
                    _json(budget),
                    _iso(current),
                    _iso(current),
                    1 if lease_requeueable else 0,
                ),
            )
            row = connection.execute("SELECT * FROM jobs WHERE job_id = ?", (identifier,)).fetchone()
            connection.commit()
        return self._row_record(row), True

    def get(self, job_id: str) -> dict[str, Any]:
        identifier = str(UUID(_require_text(job_id, "job_id")))
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE job_id = ?", (identifier,)).fetchone()
        if row is None:
            raise KeyError(identifier)
        return self._row_record(row)

    def claim(
        self,
        worker_id: str,
        *,
        now: datetime | None = None,
        lease_seconds: int = 60,
    ) -> dict[str, Any] | None:
        worker = _require_text(worker_id, "worker_id")
        if not isinstance(lease_seconds, int) or isinstance(lease_seconds, bool) or lease_seconds < 1:
            raise ValueError("lease_seconds must be a positive integer")
        current = _utc(now or datetime.now(timezone.utc))
        lease_until = current + timedelta(seconds=lease_seconds)

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM jobs
                WHERE state = 'QUEUED'
                ORDER BY created_at, job_id
                LIMIT 1
                """
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            connection.execute(
                """
                UPDATE jobs
                SET state='RUNNING', owner=?, lease_until=?, attempt=attempt+1, updated_at=?
                WHERE job_id=? AND state='QUEUED'
                """,
                (worker, _iso(lease_until), _iso(current), row["job_id"]),
            )
            claimed = connection.execute("SELECT * FROM jobs WHERE job_id = ?", (row["job_id"],)).fetchone()
            connection.commit()
        return self._row_record(claimed)

    def requeue_expired(self, *, now: datetime | None = None) -> int:
        """Requeue only jobs whose enqueue contract explicitly permits retry.

        An expired lease does not prove that a non-idempotent external research
        effect did not happen. Such jobs move to WAITING_EXTERNAL and require
        evidence or an explicit recovery decision instead of being retried.
        """

        current = _utc(now or datetime.now(timezone.utc))
        requeued = 0
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT job_id, lease_requeueable FROM jobs
                WHERE state='RUNNING' AND lease_until IS NOT NULL AND lease_until <= ?
                """,
                (_iso(current),),
            ).fetchall()
            for row in rows:
                if bool(row["lease_requeueable"]):
                    connection.execute(
                        """
                        UPDATE jobs
                        SET state='QUEUED', owner=NULL, lease_until=NULL,
                            generation=generation+1, updated_at=?
                        WHERE job_id=? AND state='RUNNING'
                        """,
                        (_iso(current), row["job_id"]),
                    )
                    requeued += 1
                else:
                    error = _json(
                        {
                            "code": "LEASE_EXPIRED_NON_IDEMPOTENT",
                            "message": (
                                "Lease expired without proof that retry is safe; "
                                "external outcome requires reconciliation"
                            ),
                        }
                    )
                    connection.execute(
                        """
                        UPDATE jobs
                        SET state='WAITING_EXTERNAL', owner=NULL, lease_until=NULL,
                            generation=generation+1, error_json=?, updated_at=?
                        WHERE job_id=? AND state='RUNNING'
                        """,
                        (error, _iso(current), row["job_id"]),
                    )
            connection.commit()
        return requeued


    def resolve_waiting_external(
        self,
        job_id: str,
        *,
        generation: int,
        verdict: str,
        evidence_ref: str,
        artifact_store: ArtifactStore | None = None,
        output_refs: list[str] | None = None,
        now: datetime | None = None,
    ) -> bool:
        """Resolve an ambiguous non-idempotent lease only from immutable evidence.

        PROVEN_NOT_RUN requeues the already-fenced generation. PROVEN_SUCCEEDED
        accepts explicit output references. PROVEN_FAILED terminates the job.
        This is a research-job recovery boundary and cannot submit financial work.
        """

        identifier = str(UUID(_require_text(job_id, "job_id")))
        if not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
            raise ValueError("generation must be a positive integer")
        normalized_verdict = _require_text(verdict, "verdict").upper()
        allowed = {"PROVEN_NOT_RUN", "PROVEN_SUCCEEDED", "PROVEN_FAILED"}
        if normalized_verdict not in allowed:
            raise ValueError("unsupported external-resolution verdict")
        evidence = _require_immutable_artifact_ref(evidence_ref, "evidence_ref")
        if output_refs is not None and not isinstance(output_refs, list):
            raise ValueError("output_refs must be a list when provided")
        outputs = [] if output_refs is None else [
            _require_immutable_artifact_ref(value, "output_ref")
            for value in output_refs
        ]
        if normalized_verdict == "PROVEN_SUCCEEDED" and not outputs:
            raise ValueError("PROVEN_SUCCEEDED requires output_refs")
        if normalized_verdict != "PROVEN_SUCCEEDED" and outputs:
            raise ValueError("output_refs are valid only for PROVEN_SUCCEEDED")

        current = _utc(now or datetime.now(timezone.utc))
        semantic_resolution = {
            "generation": generation,
            "verdict": normalized_verdict,
            "evidence_ref": evidence,
            "output_refs": outputs,
        }
        resolution = {
            **semantic_resolution,
            "resolved_at": _iso(current),
        }
        encoded_resolution = _json(resolution)

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM jobs WHERE job_id = ?",
                (identifier,),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise KeyError(identifier)
            if int(row["generation"]) != generation:
                connection.rollback()
                raise JobLeaseError("external resolution generation is stale")

            existing_resolution = row["external_resolution_json"]
            if row["state"] != "WAITING_EXTERNAL":
                if existing_resolution is not None:
                    prior = json.loads(existing_resolution)
                    prior_semantic = {
                        "generation": prior.get("generation"),
                        "verdict": prior.get("verdict"),
                        "evidence_ref": prior.get("evidence_ref"),
                        "output_refs": prior.get("output_refs", []),
                    }
                    if prior_semantic == semantic_resolution:
                        connection.commit()
                        return False
                connection.rollback()
                raise JobConflictError(
                    "job is not waiting for the supplied external resolution"
                )

            if artifact_store is None or not _verify_external_resolution_artifact(
                artifact_store=artifact_store,
                evidence_ref=evidence,
                job_id=identifier,
                generation=generation,
                verdict=normalized_verdict,
            ):
                connection.rollback()
                raise JobConflictError(
                    "external resolution requires matching immutable artifact evidence"
                )
            if normalized_verdict == "PROVEN_SUCCEEDED" and any(
                not _verify_artifact_ref(artifact_store, output_ref)
                for output_ref in outputs
            ):
                connection.rollback()
                raise JobConflictError(
                    "external success requires verified immutable output artifacts"
                )

            if normalized_verdict == "PROVEN_NOT_RUN":
                state = "QUEUED"
                error_json = None
                output_json = "[]"
            elif normalized_verdict == "PROVEN_SUCCEEDED":
                state = "SUCCEEDED"
                error_json = None
                output_json = _json(outputs)
            else:
                state = "FAILED"
                error_json = _json(
                    {
                        "code": "EXTERNAL_OUTCOME_PROVEN_FAILED",
                        "message": "Immutable recovery evidence proves external research work failed",
                    }
                )
                output_json = "[]"

            connection.execute(
                """
                UPDATE jobs
                SET state=?, owner=NULL, lease_until=NULL, error_json=?,
                    output_refs_json=?, external_resolution_json=?, updated_at=?
                WHERE job_id=? AND state='WAITING_EXTERNAL'
                """,
                (
                    state,
                    error_json,
                    output_json,
                    encoded_resolution,
                    _iso(current),
                    identifier,
                ),
            )
            connection.commit()
        return True

    def renew(
        self,
        job_id: str,
        *,
        worker_id: str,
        generation: int,
        now: datetime | None = None,
        lease_seconds: int = 60,
    ) -> dict[str, Any]:
        identifier = str(UUID(_require_text(job_id, "job_id")))
        worker = _require_text(worker_id, "worker_id")
        if not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
            raise ValueError("generation must be a positive integer")
        if not isinstance(lease_seconds, int) or isinstance(lease_seconds, bool) or lease_seconds < 1:
            raise ValueError("lease_seconds must be a positive integer")
        current = _utc(now or datetime.now(timezone.utc))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM jobs WHERE job_id = ?", (identifier,)).fetchone()
            self._require_live_lease(row, worker, generation, current)
            connection.execute(
                "UPDATE jobs SET lease_until=?, updated_at=? WHERE job_id=?",
                (_iso(current + timedelta(seconds=lease_seconds)), _iso(current), identifier),
            )
            updated = connection.execute("SELECT * FROM jobs WHERE job_id = ?", (identifier,)).fetchone()
            connection.commit()
        return self._row_record(updated)

    @staticmethod
    def _require_live_lease(
        row: sqlite3.Row | None,
        worker: str,
        generation: int,
        now: datetime,
    ) -> None:
        if row is None:
            raise KeyError("job")
        if row["state"] != "RUNNING":
            raise JobLeaseError("job is not running")
        if row["owner"] != worker or row["generation"] != generation:
            raise JobLeaseError("worker does not own the current job generation")
        if row["lease_until"] is None or _parse(row["lease_until"]) <= now:
            raise JobLeaseError("job lease has expired")

    def checkpoint(
        self,
        job_id: str,
        *,
        worker_id: str,
        generation: int,
        checkpoint_ref: str,
        resource_usage: dict[str, int | float],
        now: datetime | None = None,
    ) -> dict[str, Any]:
        identifier = str(UUID(_require_text(job_id, "job_id")))
        worker = _require_text(worker_id, "worker_id")
        checkpoint = _require_text(checkpoint_ref, "checkpoint_ref")
        usage = _validate_budget(resource_usage)
        current = _utc(now or datetime.now(timezone.utc))

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM jobs WHERE job_id = ?", (identifier,)).fetchone()
            self._require_live_lease(row, worker, generation, current)
            budget = json.loads(row["resource_budget_json"])
            previous_usage = json.loads(row["resource_usage_json"])
            merged_usage = dict(previous_usage)
            for key, value in usage.items():
                if key not in budget or value > float(budget[key]):
                    connection.rollback()
                    raise JobBudgetError(f"resource budget exceeded or undeclared: {key}")
                previous_value = float(previous_usage.get(key, 0.0))
                if value < previous_value:
                    connection.rollback()
                    raise JobBudgetError(f"resource usage cannot decrease: {key}")
                merged_usage[key] = value
            connection.execute(
                """
                UPDATE jobs SET checkpoint_ref=?, resource_usage_json=?, updated_at=?
                WHERE job_id=?
                """,
                (checkpoint, _json(merged_usage), _iso(current), identifier),
            )
            updated = connection.execute("SELECT * FROM jobs WHERE job_id = ?", (identifier,)).fetchone()
            connection.commit()
        return self._row_record(updated)

    def cancel(self, job_id: str, *, now: datetime | None = None) -> bool:
        identifier = str(UUID(_require_text(job_id, "job_id")))
        current = _utc(now or datetime.now(timezone.utc))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT state FROM jobs WHERE job_id = ?", (identifier,)).fetchone()
            if row is None:
                connection.rollback()
                raise KeyError(identifier)
            if row["state"] in FINAL_STATES:
                connection.commit()
                return False
            connection.execute(
                """
                UPDATE jobs
                SET state='CANCELLED', owner=NULL, lease_until=NULL, updated_at=?
                WHERE job_id=?
                """,
                (_iso(current), identifier),
            )
            connection.commit()
        return True

    def succeed(
        self,
        job_id: str,
        *,
        worker_id: str,
        generation: int,
        output_refs: list[str],
        now: datetime | None = None,
    ) -> bool:
        identifier = str(UUID(_require_text(job_id, "job_id")))
        worker = _require_text(worker_id, "worker_id")
        if not isinstance(output_refs, list) or not output_refs:
            raise ValueError("output_refs must be a non-empty list")
        outputs = [_require_text(value, "output_ref") for value in output_refs]
        current = _utc(now or datetime.now(timezone.utc))

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM jobs WHERE job_id = ?", (identifier,)).fetchone()
            if row is None:
                connection.rollback()
                raise KeyError(identifier)
            if row["state"] == "SUCCEEDED":
                existing = json.loads(row["output_refs_json"])
                if existing == outputs and row["generation"] == generation:
                    connection.commit()
                    return False
                connection.rollback()
                raise JobConflictError("job already published a different accepted result")
            self._require_live_lease(row, worker, generation, current)
            connection.execute(
                """
                UPDATE jobs
                SET state='SUCCEEDED', output_refs_json=?, owner=NULL, lease_until=NULL, updated_at=?
                WHERE job_id=? AND state='RUNNING' AND generation=?
                """,
                (_json(outputs), _iso(current), identifier, generation),
            )
            connection.commit()
        return True


    def publish_result_bytes(
        self,
        job_id: str,
        *,
        worker_id: str,
        generation: int,
        artifact_store: ArtifactStore,
        data: bytes,
        media_type: str,
        rights: dict[str, Any],
        slot: str = "primary",
        now: datetime | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Publish one deterministic result artifact and accept it exactly once.

        Artifact publication happens before the job compare-and-swap. A crash
        between those steps can leave immutable evidence, but it cannot create
        an accepted result. Retrying the same generation and bytes is
        idempotent; different bytes conflict on the deterministic artifact ID.
        """

        identifier = str(UUID(_require_text(job_id, "job_id")))
        worker = _require_text(worker_id, "worker_id")
        result_slot = _require_text(slot, "slot")
        if not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
            raise ValueError("generation must be a positive integer")
        if not isinstance(data, bytes):
            raise TypeError("data must be bytes")
        current = _utc(now or datetime.now(timezone.utc))

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM jobs WHERE job_id = ?", (identifier,)).fetchone()
            self._require_live_lease(row, worker, generation, current)
            source_refs = json.loads(row["input_hashes_json"])
            kind = row["kind"]
            connection.commit()

        artifact_id = str(uuid5(UUID(identifier), f"generation:{generation}:slot:{result_slot}"))
        manifest = artifact_store.publish_bytes(
            artifact_id=artifact_id,
            data=data,
            media_type=media_type,
            rights=rights,
            source_refs=source_refs,
            metadata={
                "job_id": identifier,
                "job_generation": generation,
                "job_kind": kind,
                "slot": result_slot,
            },
        )
        output_ref = f"artifact:{artifact_id}@{manifest['sha256']}"
        accepted = self.succeed(
            identifier,
            worker_id=worker,
            generation=generation,
            output_refs=[output_ref],
            now=now,
        )
        return manifest, accepted

    def fail(
        self,
        job_id: str,
        *,
        worker_id: str,
        generation: int,
        error: dict[str, Any],
        now: datetime | None = None,
    ) -> None:
        identifier = str(UUID(_require_text(job_id, "job_id")))
        worker = _require_text(worker_id, "worker_id")
        if not isinstance(error, dict) or not error:
            raise ValueError("error must be a non-empty object")
        current = _utc(now or datetime.now(timezone.utc))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM jobs WHERE job_id = ?", (identifier,)).fetchone()
            self._require_live_lease(row, worker, generation, current)
            connection.execute(
                """
                UPDATE jobs
                SET state='FAILED', error_json=?, owner=NULL, lease_until=NULL, updated_at=?
                WHERE job_id=?
                """,
                (_json(error), _iso(current), identifier),
            )
            connection.commit()
