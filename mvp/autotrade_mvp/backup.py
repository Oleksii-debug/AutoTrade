"""Verified backup and fail-closed restore foundation for AutoTrade.

The backup uses SQLite's backup API for the durable journal, hashes every
payload file, verifies content-addressed artifact objects and restores through
an isolated staging directory. A restored runtime is always marked as requiring
reconciliation before any future trading authority can be considered.
"""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import tempfile
from typing import Any

from .diagnostics import build_diagnostic_snapshot
from .persistence import JournalStore, payload_digest
from .reconciliation_journal import require_current_reconciliation_checkpoint
from .recovery import HostState, OwnerFence, RecoveryController


BACKUP_SCHEMA_VERSION = 1
MANIFEST_NAME = "backup-manifest.json"
MANIFEST_DIGEST_NAME = "backup-manifest.sha256"
RESTORE_MARKER_NAME = "RESTORE_RECONCILIATION_REQUIRED.json"
RESTORE_COMPLETION_PROOF_NAME = "RESTORE_RECONCILIATION_COMPLETE.json"
_SHA256_HEX = frozenset("0123456789abcdef")


class BackupError(RuntimeError):
    """Base error for backup or restore failures."""


class BackupIntegrityError(BackupError):
    """Raised when backup bytes or metadata do not verify."""


class BackupCompatibilityError(BackupError):
    """Raised when a backup uses an unsupported schema."""


def _sha256_bytes(payload: bytes) -> str:
    return sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_json(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _fsync_directory(path: Path) -> None:
    """Persist directory entry changes where the platform exposes that primitive."""

    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        descriptor = os.open(path, flags)
    except OSError:
        if os.name == "posix":
            raise
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory_tree(root: Path) -> None:
    """Flush every directory entry in a staged tree before atomic publication."""

    if root.is_symlink() or not root.is_dir():
        raise BackupIntegrityError(
            f"Durable staging root is not a regular directory: {root.name}"
        )
    directories = [root]
    directories.extend(
        path
        for path in root.rglob("*")
        if path.is_dir() and not path.is_symlink()
    )
    for directory in sorted(
        directories,
        key=lambda item: len(item.relative_to(root).parts),
        reverse=True,
    ):
        _fsync_directory(directory)


def _write_bytes_durable(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _fsync_file(path: Path) -> None:
    """Flush an already-published regular file before publishing its directory tree."""

    if path.is_symlink() or not path.is_file():
        raise BackupIntegrityError(f"Durable payload is not a regular file: {path.name}")
    # Windows' CRT rejects fsync() on a read-only descriptor with EBADF.
    # Open the already-published payload read/write there solely to obtain a
    # flushable descriptor; no bytes are modified. POSIX keeps the narrower
    # read-only descriptor.
    mode = "rb+" if os.name == "nt" else "rb"
    with path.open(mode) as handle:
        os.fsync(handle.fileno())


def _copy_file_durable(source: Path, destination: Path) -> None:
    """Copy payload bytes into staging and durably flush file plus directory entry."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as input_handle, destination.open("wb") as output_handle:
        shutil.copyfileobj(input_handle, output_handle, length=1024 * 1024)
        output_handle.flush()
        os.fsync(output_handle.fileno())
    _fsync_directory(destination.parent)


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False



def _canonical_sha256_ref(value: object, *, name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 71
        or not value.startswith("sha256:")
        or any(character not in _SHA256_HEX for character in value[7:])
    ):
        raise BackupIntegrityError(f"{name} must be a canonical SHA-256 reference")
    return value


def _nonempty_text(value: object, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise BackupIntegrityError(f"{name} must be canonical non-empty text")
    return value


def _utc_text(value: object, *, name: str) -> datetime:
    text = _nonempty_text(value, name=name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise BackupIntegrityError(f"{name} must be an ISO timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise BackupIntegrityError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _recovery_owner_scopes_from_journal(
    journal_path: Path,
) -> tuple[str, ...]:
    """Discover durable recovery-owner scopes without guessing account authority."""

    try:
        connection = sqlite3.connect(str(journal_path), timeout=5)
        rows = connection.execute(
            """
            SELECT DISTINCT aggregate_id
            FROM events
            WHERE aggregate_type = ?
            ORDER BY aggregate_id
            """,
            ("recovery_owner",),
        ).fetchall()
    except (sqlite3.Error, OSError) as error:
        raise BackupIntegrityError(
            "Recovery owner scope evidence is unreadable"
        ) from error
    finally:
        try:
            connection.close()
        except (UnboundLocalError, sqlite3.Error):
            pass

    scopes: list[str] = []
    for row in rows:
        scope = _nonempty_text(row[0], name="recovery owner scope")
        if scope in scopes:
            raise BackupIntegrityError(
                "Recovery owner scope evidence is duplicated"
            )
        scopes.append(scope)
    return tuple(scopes)


def _recovery_owner_chain_from_journal(
    journal_path: Path,
    *,
    owner_scope: str,
) -> tuple[OwnerFence, ...]:
    """Read sender-fence history without mutating the restored SQLite journal."""

    scope = _nonempty_text(owner_scope, name="owner_scope")
    try:
        connection = sqlite3.connect(str(journal_path), timeout=5)
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT event_type, aggregate_version, payload_json, payload_hash
            FROM events
            WHERE aggregate_type = ? AND aggregate_id = ?
            ORDER BY aggregate_version
            """,
            ("recovery_owner", scope),
        ).fetchall()
    except (sqlite3.Error, OSError) as error:
        raise BackupIntegrityError(
            "Recovery owner journal evidence is unreadable"
        ) from error
    finally:
        try:
            connection.close()
        except (UnboundLocalError, sqlite3.Error):
            pass

    chain: list[OwnerFence] = []
    for expected_epoch, row in enumerate(rows, start=1):
        if row["event_type"] != "RecoveryOwnerChanged":
            raise BackupIntegrityError(
                "Recovery owner journal contains unsupported event type"
            )
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, json.JSONDecodeError) as error:
            raise BackupIntegrityError(
                "Recovery owner journal payload is invalid"
            ) from error
        if not isinstance(payload, dict):
            raise BackupIntegrityError(
                "Recovery owner journal payload must be an object"
            )
        if payload_digest(payload) != row["payload_hash"]:
            raise BackupIntegrityError(
                "Recovery owner journal payload hash mismatch"
            )
        owner_id = payload.get("owner_id")
        epoch_raw = payload.get("owner_epoch")
        if not isinstance(owner_id, str) or not owner_id.strip():
            raise BackupIntegrityError(
                "Recovery owner journal owner identity is invalid"
            )
        if (
            not isinstance(epoch_raw, str)
            or not epoch_raw.isdigit()
            or epoch_raw == "0"
            or (len(epoch_raw) > 1 and epoch_raw.startswith("0"))
        ):
            raise BackupIntegrityError(
                "Recovery owner journal epoch is invalid"
            )
        epoch = int(epoch_raw)
        if int(row["aggregate_version"]) != expected_epoch or epoch != expected_epoch:
            raise BackupIntegrityError(
                "Recovery owner journal epoch/version chain is invalid"
            )
        chain.append(OwnerFence(owner_id=owner_id.strip(), epoch=epoch))
    return tuple(chain)


def _read_restore_marker(root: Path) -> dict[str, Any]:
    marker_path = root / RESTORE_MARKER_NAME
    if not marker_path.is_file() or marker_path.is_symlink():
        raise BackupIntegrityError("Restore reconciliation marker is missing")
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise BackupIntegrityError(
            "Restore reconciliation marker is unreadable"
        ) from error
    if not isinstance(marker, dict):
        raise BackupIntegrityError("Restore reconciliation marker is invalid")
    if (
        marker.get("schema_version") != 2
        or marker.get("reason")
        != "RECONCILIATION_AND_OWNERSHIP_FENCING_REQUIRED"
        or marker.get("status")
        not in {"RECONCILIATION_REQUIRED", "RECONCILIATION_COMPLETE"}
    ):
        raise BackupIntegrityError("Restore reconciliation marker is invalid")
    _utc_text(marker.get("restored_at"), name="restored_at")
    _canonical_sha256_ref(
        marker.get("backup_manifest_sha256"),
        name="backup_manifest_sha256",
    )
    _nonempty_text(marker.get("source_owner_scope"), name="source_owner_scope")
    owner_id = marker.get("source_owner_id")
    owner_epoch = marker.get("source_owner_epoch")
    if owner_id is None or owner_epoch is None:
        if owner_id is not None or owner_epoch is not None:
            raise BackupIntegrityError(
                "Restore source owner identity is partially bound"
            )
    else:
        _nonempty_text(owner_id, name="source_owner_id")
        if (
            isinstance(owner_epoch, bool)
            or not isinstance(owner_epoch, int)
            or owner_epoch < 1
        ):
            raise BackupIntegrityError("source_owner_epoch is invalid")
    return marker


def _validate_stored_reconciliation_payload(payload: object) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise BackupIntegrityError("Reconciliation completion proof is invalid")
    required = {
        "provider_id",
        "account_id",
        "environment",
        "complete",
        "snapshot_consistent",
        "snapshot_mode",
        "snapshot_query_started_at",
        "snapshot_query_completed_at",
        "activity_coverage_complete",
        "matched_execution_ids",
        "matched_working_client_order_ids",
        "matched_provider_activity_ids",
        "submission_resolutions",
        "blocking_resources",
    }
    if set(payload) != required:
        raise BackupIntegrityError(
            "Reconciliation completion proof structure is invalid"
        )
    for field in ("provider_id", "account_id", "environment"):
        _nonempty_text(payload[field], name=field)
    if (
        payload["complete"] is not True
        or payload["snapshot_consistent"] is not True
        or payload["activity_coverage_complete"] is not True
        or payload["blocking_resources"] != []
    ):
        raise BackupIntegrityError(
            "Reconciliation completion proof must be a complete non-blocking reconciliation"
        )
    if payload["snapshot_mode"] is not None:
        _nonempty_text(payload["snapshot_mode"], name="snapshot_mode")
    for field in ("snapshot_query_started_at", "snapshot_query_completed_at"):
        if payload[field] is not None:
            _utc_text(payload[field], name=field)

    identity_fields = (
        "matched_execution_ids",
        "matched_working_client_order_ids",
        "matched_provider_activity_ids",
    )
    normalized_ids: dict[str, list[str]] = {}
    for field in identity_fields:
        values = payload[field]
        if not isinstance(values, list):
            raise BackupIntegrityError(f"{field} must be a list")
        normalized: list[str] = []
        for value in values:
            normalized.append(_nonempty_text(value, name=field))
        if len(normalized) != len(set(normalized)):
            raise BackupIntegrityError(f"{field} must be unique")
        normalized_ids[field] = normalized

    resolutions = payload["submission_resolutions"]
    if not isinstance(resolutions, list):
        raise BackupIntegrityError(
            "submission_resolutions must be a list"
        )
    seen_attempts: set[str] = set()
    seen_clients: set[str] = set()
    seen_executions: set[str] = set()
    seen_orders: set[str] = set()
    matched_executions = set(normalized_ids["matched_execution_ids"])
    allowed = {
        "PROVEN_ABSENT",
        "OBSERVED_EXECUTION",
        "OBSERVED_WORKING_ORDER",
    }
    resolution_keys = {
        "attempt_id",
        "intent_id",
        "client_order_id",
        "outcome",
        "evidence_reason",
        "provider_order_ids",
        "provider_execution_ids",
    }
    for item in resolutions:
        if not isinstance(item, dict) or set(item) != resolution_keys:
            raise BackupIntegrityError(
                "submission resolution proof structure is invalid"
            )
        attempt = _nonempty_text(item["attempt_id"], name="attempt_id")
        _nonempty_text(item["intent_id"], name="intent_id")
        client = _nonempty_text(
            item["client_order_id"], name="client_order_id"
        )
        _nonempty_text(item["evidence_reason"], name="evidence_reason")
        if attempt in seen_attempts or client in seen_clients:
            raise BackupIntegrityError(
                "submission resolution identities must be unique"
            )
        seen_attempts.add(attempt)
        seen_clients.add(client)
        outcome = item["outcome"]
        if outcome not in allowed:
            raise BackupIntegrityError(
                "submission resolution outcome is not terminal"
            )
        for field, seen in (
            ("provider_order_ids", seen_orders),
            ("provider_execution_ids", seen_executions),
        ):
            values = item[field]
            if not isinstance(values, list):
                raise BackupIntegrityError(
                    f"{field} must be a list"
                )
            local: set[str] = set()
            for raw in values:
                identity = _nonempty_text(raw, name=field)
                if identity in local or identity in seen:
                    raise BackupIntegrityError(
                        f"{field} identities must be globally unique"
                    )
                local.add(identity)
                seen.add(identity)
        order_ids = item["provider_order_ids"]
        execution_ids = item["provider_execution_ids"]
        if outcome == "PROVEN_ABSENT" and (order_ids or execution_ids):
            raise BackupIntegrityError(
                "PROVEN_ABSENT cannot carry provider identities"
            )
        if outcome == "OBSERVED_EXECUTION":
            if (
                not execution_ids
                or order_ids
                or any(value not in matched_executions for value in execution_ids)
            ):
                raise BackupIntegrityError(
                    "OBSERVED_EXECUTION proof does not match reconciled executions"
                )
        if outcome == "OBSERVED_WORKING_ORDER" and (
            not order_ids or execution_ids
        ):
            raise BackupIntegrityError(
                "OBSERVED_WORKING_ORDER proof is invalid"
            )
    return payload


def _reconciliation_completion_summary(
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Validate the economic completeness copied from a journal-issued checkpoint."""

    for field in (
        "unexpected_execution_ids",
        "missing_local_execution_ids",
        "unexpected_working_provider_order_ids",
        "missing_local_working_client_order_ids",
        "unexpected_provider_activity_ids",
        "missing_local_provider_activity_ids",
        "manual_or_external_activity_ids",
    ):
        value = payload.get(field)
        if not isinstance(value, list):
            raise BackupIntegrityError(f"{field} must be a list")
        if value:
            raise BackupError(
                "Restore completion requires a complete non-blocking reconciliation"
            )
    for field in (
        "cash_differences",
        "position_differences",
        "borrow_differences",
    ):
        value = payload.get(field)
        if not isinstance(value, dict):
            raise BackupIntegrityError(f"{field} must be an object")
        if value:
            raise BackupError(
                "Restore completion requires a complete non-blocking reconciliation"
            )

    snapshot = payload.get("snapshot")
    if snapshot is None:
        snapshot_mode = None
        snapshot_started = None
        snapshot_completed = None
    else:
        if not isinstance(snapshot, dict) or set(snapshot) != {
            "mode",
            "query_started_at",
            "query_completed_at",
        }:
            raise BackupIntegrityError(
                "Reconciliation checkpoint snapshot structure is invalid"
            )
        snapshot_mode = snapshot["mode"]
        snapshot_started = snapshot["query_started_at"]
        snapshot_completed = snapshot["query_completed_at"]

    summary = {
        "provider_id": payload.get("provider_id"),
        "account_id": payload.get("account_id"),
        "environment": payload.get("environment"),
        "complete": payload.get("complete"),
        "snapshot_consistent": payload.get("snapshot_consistent"),
        "snapshot_mode": snapshot_mode,
        "snapshot_query_started_at": snapshot_started,
        "snapshot_query_completed_at": snapshot_completed,
        "activity_coverage_complete": payload.get("activity_coverage_complete"),
        "matched_execution_ids": payload.get("matched_execution_ids"),
        "matched_working_client_order_ids": payload.get(
            "matched_working_client_order_ids"
        ),
        "matched_provider_activity_ids": payload.get(
            "matched_provider_activity_ids"
        ),
        "submission_resolutions": payload.get("submission_resolutions"),
        "blocking_resources": payload.get("blocking_resources"),
    }
    return _validate_stored_reconciliation_payload(summary)


def _restore_reconciliation_checkpoint_proof(
    store: JournalStore,
    *,
    checkpoint_event_id: str,
    current_owner: OwnerFence,
) -> dict[str, Any]:
    """Load restore authority only from the current owner-bound journal checkpoint."""

    if not isinstance(store, JournalStore):
        raise TypeError("store must be JournalStore")
    if not isinstance(current_owner, OwnerFence):
        raise TypeError("current_owner must be OwnerFence")
    event_id = _nonempty_text(
        checkpoint_event_id,
        name="reconciliation_checkpoint_event_id",
    )
    checkpoint = store.get_event(event_id)
    if checkpoint is None:
        raise BackupError("Restore reconciliation checkpoint does not exist")
    if (
        checkpoint.get("event_type") != "AccountReconciled"
        or checkpoint.get("aggregate_type") != "account_reconciliation"
    ):
        raise BackupIntegrityError(
            "Restore reconciliation checkpoint has invalid event authority"
        )
    payload = checkpoint.get("payload")
    if not isinstance(payload, dict):
        raise BackupIntegrityError(
            "Restore reconciliation checkpoint payload is invalid"
        )

    provider_id = _nonempty_text(payload.get("provider_id"), name="provider_id")
    account_id = _nonempty_text(payload.get("account_id"), name="account_id")
    environment = _nonempty_text(payload.get("environment"), name="environment")
    current = require_current_reconciliation_checkpoint(
        store,
        checkpoint_event_id=event_id,
        provider_id=provider_id,
        account_id=account_id,
        environment=environment,
    )
    if current.get("event_id") != event_id:
        raise BackupIntegrityError(
            "Restore reconciliation checkpoint identity changed while loading"
        )

    checkpoint_owner = payload.get("checkpoint_owner")
    if (
        not isinstance(checkpoint_owner, dict)
        or set(checkpoint_owner) != {"host_id", "owner_epoch"}
        or checkpoint_owner.get("host_id") != current_owner.owner_id
        or checkpoint_owner.get("owner_epoch") != str(current_owner.epoch)
        or checkpoint.get("host_id") != current_owner.owner_id
        or checkpoint.get("owner_epoch") != str(current_owner.epoch)
    ):
        raise BackupError(
            "Restore reconciliation checkpoint is not bound to current recovery owner"
        )

    payload_hash = _canonical_sha256_ref(
        checkpoint.get("payload_hash"),
        name="reconciliation checkpoint payload_hash",
    )
    if payload_hash != payload_digest(payload):
        raise BackupIntegrityError(
            "Restore reconciliation checkpoint payload hash mismatch"
        )
    journal_sequence = checkpoint.get("journal_sequence")
    aggregate_version = checkpoint.get("aggregate_version")
    aggregate_id = checkpoint.get("aggregate_id")
    if (
        type(journal_sequence) is not int
        or journal_sequence <= 0
        or type(aggregate_version) is not int
        or aggregate_version <= 0
        or not isinstance(aggregate_id, str)
        or not aggregate_id
    ):
        raise BackupIntegrityError(
            "Restore reconciliation checkpoint durable identity is invalid"
        )
    _utc_text(payload.get("observed_at"), name="reconciliation observed_at")
    summary = _reconciliation_completion_summary(payload)
    return {
        "checkpoint_event_id": event_id,
        "checkpoint_payload_hash": payload_hash,
        "checkpoint_journal_sequence": journal_sequence,
        "checkpoint_aggregate_id": aggregate_id,
        "checkpoint_aggregate_version": aggregate_version,
        "checkpoint_owner_id": current_owner.owner_id,
        "checkpoint_owner_epoch": current_owner.epoch,
        "provider_id": provider_id,
        "account_id": account_id,
        "environment": environment,
        "summary": summary,
    }


def _safe_relative_path(raw: str) -> Path:
    if not isinstance(raw, str) or not raw:
        raise BackupIntegrityError("Backup manifest path is invalid")
    if "\\" in raw or "\x00" in raw:
        raise BackupIntegrityError("Backup manifest path must use canonical POSIX separators")
    if len(raw) >= 2 and raw[0].isalpha() and raw[1] == ":":
        raise BackupIntegrityError("Backup manifest path must not use Windows drive notation")
    pure = PurePosixPath(raw)
    if (
        pure.is_absolute()
        or pure.as_posix() != raw
        or any(part in {"", ".", ".."} for part in pure.parts)
    ):
        raise BackupIntegrityError("Backup manifest path is unsafe")
    return Path(*pure.parts)


def _valid_sha256_digest(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and value == value.lower()
        and all(character in _SHA256_HEX for character in value)
    )


def _expected_kind(path: str) -> str:
    parts = PurePosixPath(path).parts
    if parts == ("state", "journal.sqlite3"):
        return "sqlite-journal"
    if parts in {
        ("state", "checkpoint.json"),
        ("state", "learning-evidence.jsonl"),
    }:
        return "runtime-state"
    if len(parts) >= 3 and parts[:2] == ("state", "order-intents") and parts[-1].endswith(".json"):
        return "order-intent"
    if (
        len(parts) == 5
        and parts[:3] == ("artifacts", "objects", "sha256")
        and len(parts[3]) == 2
        and _valid_sha256_digest(parts[4])
        and parts[3] == parts[4][:2]
    ):
        return "artifact-object"
    if (
        len(parts) == 5
        and parts[:3] == ("artifacts", "manifests", "sha256")
        and len(parts[3]) == 2
        and parts[4].endswith(".json")
        and _valid_sha256_digest(parts[4][:-5])
        and parts[3] == parts[4][:-5][:2]
    ):
        return "artifact-manifest"
    raise BackupIntegrityError(f"Backup payload path is outside the canonical inventory: {path}")


def _copy_stable_file(source: Path, destination: Path) -> tuple[str, int]:
    if source.is_symlink() or not source.is_file():
        raise BackupError(f"Backup source is not a regular file: {source.name}")
    before = _sha256_file(source)
    payload = source.read_bytes()
    digest = _sha256_bytes(payload)
    after = _sha256_file(source)
    if before != digest or after != digest:
        raise BackupError(f"Backup source changed while being copied: {source.name}")
    _write_bytes_durable(destination, payload)
    return digest, len(payload)


def _sqlite_schema_version(
    path: Path,
    *,
    classify_future_source_as_compatibility: bool = False,
) -> int:
    if not path.is_file():
        raise BackupError("Durable journal is missing")
    uri = path.resolve().as_uri() + "?mode=ro"
    try:
        with closing(sqlite3.connect(uri, uri=True)) as connection:
            rows = connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
    except sqlite3.Error as error:
        raise BackupIntegrityError(
            "Durable journal schema evidence cannot be read"
        ) from error
    if not rows:
        raise BackupCompatibilityError("Durable journal has no schema migration record")
    try:
        versions = [int(row[0]) for row in rows]
    except (TypeError, ValueError) as error:
        raise BackupIntegrityError(
            "Durable journal schema migration history is invalid"
        ) from error
    latest = versions[-1]
    current_prefix = [
        version for version in versions
        if version <= JournalStore.SCHEMA_VERSION
    ]
    expected_prefix = list(
        range(1, min(latest, JournalStore.SCHEMA_VERSION) + 1)
    )
    if current_prefix != expected_prefix:
        raise BackupIntegrityError(
            "Durable journal schema migration history is not contiguous"
        )
    if (
        latest > JournalStore.SCHEMA_VERSION
        and classify_future_source_as_compatibility
    ):
        raise BackupCompatibilityError(
            f"Unsupported journal schema version: {latest}"
        )
    if versions != list(range(1, latest + 1)):
        raise BackupIntegrityError(
            "Durable journal schema migration history is not contiguous"
        )
    if latest > JournalStore.SCHEMA_VERSION:
        raise BackupCompatibilityError(
            f"Unsupported journal schema version: {latest}"
        )
    return latest


def _backup_sqlite(source: Path, destination: Path) -> tuple[str, int, int]:
    schema_version = _sqlite_schema_version(
        source,
        classify_future_source_as_compatibility=True,
    )
    if schema_version != JournalStore.SCHEMA_VERSION:
        raise BackupCompatibilityError(
            f"Unsupported journal schema version: {schema_version}"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_uri = source.resolve().as_uri() + "?mode=ro"
    try:
        with closing(sqlite3.connect(source_uri, uri=True)) as source_db:
            with closing(sqlite3.connect(destination)) as destination_db:
                source_db.backup(destination_db)
                destination_db.commit()
                # SQLite backup preserves the source journal mode. A portable
                # backup bundle must be a self-contained database, not a main
                # file whose latest pages live in undeclared WAL sidecars.
                destination_db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                journal_mode = destination_db.execute(
                    "PRAGMA journal_mode=DELETE"
                ).fetchone()[0]
                if str(journal_mode).lower() != "delete":
                    raise BackupIntegrityError(
                        "SQLite backup could not be finalized as a standalone snapshot"
                    )
                destination_db.commit()
    except sqlite3.Error as error:
        raise BackupError("SQLite backup failed") from error
    copied_schema = _sqlite_schema_version(destination)
    if copied_schema != schema_version:
        raise BackupIntegrityError("SQLite backup changed the journal schema")
    _fsync_file(destination)
    _fsync_directory(destination.parent)
    return _sha256_file(destination), destination.stat().st_size, schema_version


def _entry(path: str, digest: str, size: int, kind: str) -> dict[str, Any]:
    return {
        "path": path,
        "sha256": f"sha256:{digest}",
        "size_bytes": size,
        "kind": kind,
    }


def _artifact_source_files(root: Path) -> list[tuple[Path, str]]:
    if not root.is_dir():
        raise BackupError("Artifact store root is missing")
    files: list[tuple[Path, str]] = []
    objects_root = root / "objects" / "sha256"
    manifests_root = root / "manifests" / "sha256"
    if objects_root.exists():
        for path in sorted(objects_root.rglob("*")):
            if path.is_file():
                files.append((path, "artifact-object"))
    if manifests_root.exists():
        for path in sorted(manifests_root.rglob("*.json")):
            if path.is_file():
                files.append((path, "artifact-manifest"))
    return files


def _validate_artifact_source(root: Path) -> None:
    manifests_root = root / "manifests" / "sha256"
    objects_root = root / "objects" / "sha256"
    if objects_root.exists():
        for path in objects_root.rglob("*"):
            if not path.is_file():
                continue
            digest = path.name
            if (
                not _valid_sha256_digest(digest)
                or path.parent.name != digest[:2]
                or path.parent.parent != objects_root
                or _sha256_file(path) != digest
            ):
                raise BackupIntegrityError("Artifact object is not content-addressed correctly")
    if manifests_root.exists():
        for path in manifests_root.rglob("*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                digest = payload["digest"]
            except (OSError, json.JSONDecodeError, KeyError, TypeError) as error:
                raise BackupIntegrityError("Artifact manifest is unreadable") from error
            declared_size = payload.get("size_bytes")
            if (
                payload.get("algorithm") != "sha256"
                or not _valid_sha256_digest(digest)
                or path.stem != digest
                or path.parent.name != digest[:2]
                or path.parent.parent != manifests_root
                or isinstance(declared_size, bool)
                or not isinstance(declared_size, int)
                or declared_size < 0
            ):
                raise BackupIntegrityError("Artifact manifest digest identity is invalid")
            object_path = objects_root / digest[:2] / digest
            if (
                not object_path.is_file()
                or _sha256_file(object_path) != digest
                or object_path.stat().st_size != declared_size
            ):
                raise BackupIntegrityError("Artifact manifest references a missing or corrupt object")


def _verify_runtime_trace_consistency(state_root: Path) -> None:
    checkpoint = state_root / "checkpoint.json"
    learning_evidence = state_root / "learning-evidence.jsonl"
    journal = state_root / "journal.sqlite3"
    present = (checkpoint.is_file(), learning_evidence.is_file())
    if present == (False, False):
        return
    if present != (True, True):
        raise BackupIntegrityError(
            "Runtime consistency evidence is partial; checkpoint and learning evidence "
            "must be captured together or both be absent"
        )
    if not journal.is_file():
        raise BackupIntegrityError(
            "Runtime consistency verification requires journal"
        )
    try:
        with tempfile.TemporaryDirectory(
            prefix=".autotrade-backup-verify-",
        ) as verification_directory:
            verification_state = Path(verification_directory) / "state"
            verification_state.mkdir()
            for name in (
                "journal.sqlite3",
                "checkpoint.json",
                "learning-evidence.jsonl",
            ):
                # Trace verification is byte-oriented.  Keep this copy path
                # distinct from restore publication so restore fault injection
                # cannot be consumed by preflight verification.
                shutil.copyfile(state_root / name, verification_state / name)
            build_diagnostic_snapshot(verification_state)
    except BackupIntegrityError:
        raise
    except Exception as error:
        raise BackupIntegrityError(
            "Runtime state, journal and evidence are not one consistent snapshot"
        ) from error


def create_backup(
    state_dir: str | Path,
    artifact_root: str | Path,
    destination: str | Path,
) -> Path:
    """Create an atomic verified backup bundle.

    The runtime state may continue to exist while the SQLite backup API copies
    the journal. Mutable non-database state is copied with before/after digest
    checks; if it changes during capture, the backup fails rather than silently
    mixing versions.
    """

    state = Path(state_dir)
    artifacts = Path(artifact_root)
    target = Path(destination)
    if target.exists():
        raise BackupError("Backup destination already exists")
    if not state.is_dir():
        raise BackupError("Runtime state directory is missing")
    if _inside(target, state) or _inside(target, artifacts):
        raise BackupError("Backup destination must be outside source directories")
    _validate_artifact_source(artifacts)

    target.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".autotrade-backup-", dir=target.parent))
    entries: list[dict[str, Any]] = []
    source_rechecks: list[tuple[Path, str]] = []
    try:
        journal_source = state / "journal.sqlite3"
        journal_target = stage / "state" / "journal.sqlite3"
        journal_digest, journal_size, journal_schema = _backup_sqlite(
            journal_source, journal_target
        )
        entries.append(
            _entry(
                "state/journal.sqlite3",
                journal_digest,
                journal_size,
                "sqlite-journal",
            )
        )

        for source in [
            state / "checkpoint.json",
            state / "learning-evidence.jsonl",
        ]:
            if source.exists():
                relative = Path("state") / source.name
                digest, size = _copy_stable_file(source, stage / relative)
                entries.append(_entry(relative.as_posix(), digest, size, "runtime-state"))
                source_rechecks.append((source, digest))

        intents_root = state / "order-intents"
        if intents_root.exists():
            for source in sorted(intents_root.rglob("*.json")):
                relative = Path("state") / source.relative_to(state)
                digest, size = _copy_stable_file(source, stage / relative)
                entries.append(_entry(relative.as_posix(), digest, size, "order-intent"))
                source_rechecks.append((source, digest))

        for source, kind in _artifact_source_files(artifacts):
            relative = Path("artifacts") / source.relative_to(artifacts)
            digest, size = _copy_stable_file(source, stage / relative)
            entries.append(_entry(relative.as_posix(), digest, size, kind))
            source_rechecks.append((source, digest))

        for source, expected_digest in source_rechecks:
            if not source.is_file() or _sha256_file(source) != expected_digest:
                raise BackupError("Source changed before backup commit")

        checkpoint_present = (stage / "state" / "checkpoint.json").is_file()
        learning_evidence_present = (
            stage / "state" / "learning-evidence.jsonl"
        ).is_file()
        _verify_runtime_trace_consistency(stage / "state")
        runtime_consistency_check = (
            "DURABLE_TRACE_RECONSTRUCTION"
            if checkpoint_present and learning_evidence_present
            else "JOURNAL_ONLY"
        )

        manifest = {
            "schema_version": BACKUP_SCHEMA_VERSION,
            "created_at": _utc_now(),
            "journal_schema_version": journal_schema,
            "reconciliation_required_after_restore": True,
            "runtime_consistency_check": runtime_consistency_check,
            "files": sorted(entries, key=lambda item: item["path"]),
        }
        manifest_bytes = _canonical_json(manifest)
        _write_bytes_durable(stage / MANIFEST_NAME, manifest_bytes)
        manifest_digest = _sha256_bytes(manifest_bytes)
        _write_bytes_durable(
            stage / MANIFEST_DIGEST_NAME,
            f"sha256:{manifest_digest}\n".encode("ascii"),
        )
        verify_backup(stage)
        _fsync_directory_tree(stage)
        os.replace(stage, target)
        _fsync_directory(target.parent)
        return target
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise


def verify_backup(backup_root: str | Path) -> dict[str, Any]:
    """Verify the backup manifest, every payload byte and compatibility gates."""

    root = Path(backup_root)
    manifest_path = root / MANIFEST_NAME
    digest_path = root / MANIFEST_DIGEST_NAME
    try:
        manifest_bytes = manifest_path.read_bytes()
        expected_manifest_digest = digest_path.read_text(encoding="ascii").strip()
        manifest = json.loads(manifest_bytes)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise BackupIntegrityError("Backup manifest evidence is unreadable") from error

    actual_manifest_digest = f"sha256:{_sha256_bytes(manifest_bytes)}"
    if expected_manifest_digest != actual_manifest_digest:
        raise BackupIntegrityError("Backup manifest digest does not match")

    if not isinstance(manifest, dict) or manifest.get("schema_version") != BACKUP_SCHEMA_VERSION:
        raise BackupCompatibilityError("Unsupported backup schema version")
    if manifest.get("journal_schema_version") != JournalStore.SCHEMA_VERSION:
        raise BackupCompatibilityError("Unsupported backed-up journal schema version")
    if manifest.get("reconciliation_required_after_restore") is not True:
        raise BackupIntegrityError("Restore reconciliation gate is missing")
    entries = manifest.get("files")
    if not isinstance(entries, list) or not entries:
        raise BackupIntegrityError("Backup file inventory is empty")

    expected_paths: set[str] = set()
    for item in entries:
        if not isinstance(item, dict) or set(item) != {
            "path",
            "sha256",
            "size_bytes",
            "kind",
        }:
            raise BackupIntegrityError("Backup file inventory entry is invalid")
        relative = _safe_relative_path(item["path"])
        normalized = relative.as_posix()
        if normalized in expected_paths:
            raise BackupIntegrityError("Backup file inventory contains duplicates")
        expected_kind = _expected_kind(normalized)
        if item["kind"] != expected_kind:
            raise BackupIntegrityError(
                f"Backup payload kind does not match canonical path: {normalized}"
            )
        expected_paths.add(normalized)
        path = root / relative
        if path.is_symlink() or not path.is_file():
            raise BackupIntegrityError(f"Backup payload is missing: {normalized}")
        digest = item["sha256"]
        if (
            not isinstance(digest, str)
            or not digest.startswith("sha256:")
            or digest != f"sha256:{_sha256_file(path)}"
        ):
            raise BackupIntegrityError(f"Backup payload digest mismatch: {normalized}")
        size_bytes = item["size_bytes"]
        if (
            isinstance(size_bytes, bool)
            or not isinstance(size_bytes, int)
            or size_bytes < 0
            or size_bytes != path.stat().st_size
        ):
            raise BackupIntegrityError(f"Backup payload size mismatch: {normalized}")
        if normalized.startswith("artifacts/objects/sha256/"):
            object_digest = path.name.lower()
            if item["sha256"] != f"sha256:{object_digest}":
                raise BackupIntegrityError("Content-addressed artifact object identity mismatch")

    checkpoint_present = "state/checkpoint.json" in expected_paths
    learning_evidence_present = "state/learning-evidence.jsonl" in expected_paths
    if checkpoint_present != learning_evidence_present:
        raise BackupIntegrityError(
            "Backup runtime consistency evidence is partial"
        )
    expected_consistency_check = (
        "DURABLE_TRACE_RECONSTRUCTION"
        if checkpoint_present
        else "JOURNAL_ONLY"
    )
    if manifest.get("runtime_consistency_check") != expected_consistency_check:
        raise BackupIntegrityError(
            "Runtime consistency claim does not match the backup inventory"
        )

    observed_paths = {
        relative
        for path in root.rglob("*")
        if path.is_file()
        for relative in (path.relative_to(root).as_posix(),)
        if relative not in {MANIFEST_NAME, MANIFEST_DIGEST_NAME}
    }
    if observed_paths != expected_paths:
        raise BackupIntegrityError(
            "Backup contains untracked or missing payload files; "
            f"extra={sorted(observed_paths - expected_paths)}; "
            f"missing={sorted(expected_paths - observed_paths)}"
        )

    journal_relative = "state/journal.sqlite3"
    if journal_relative not in expected_paths:
        raise BackupIntegrityError("Backed-up journal is missing")
    if _sqlite_schema_version(root / journal_relative) != JournalStore.SCHEMA_VERSION:
        raise BackupCompatibilityError("Backed-up journal schema is incompatible")
    if expected_consistency_check == "DURABLE_TRACE_RECONSTRUCTION":
        _verify_runtime_trace_consistency(root / "state")

    artifact_manifests = [
        root / _safe_relative_path(item["path"])
        for item in entries
        if item["kind"] == "artifact-manifest"
    ]
    for path in artifact_manifests:
        try:
            artifact_manifest = json.loads(path.read_text(encoding="utf-8"))
            digest = artifact_manifest["digest"]
        except (OSError, json.JSONDecodeError, KeyError, TypeError) as error:
            raise BackupIntegrityError("Backed-up artifact manifest is invalid") from error
        declared_size = artifact_manifest.get("size_bytes")
        expected_manifest_relative = (
            f"artifacts/manifests/sha256/{digest[:2]}/{digest}.json"
            if _valid_sha256_digest(digest)
            else ""
        )
        actual_manifest_relative = path.relative_to(root).as_posix()
        if (
            artifact_manifest.get("algorithm") != "sha256"
            or not _valid_sha256_digest(digest)
            or actual_manifest_relative != expected_manifest_relative
            or isinstance(declared_size, bool)
            or not isinstance(declared_size, int)
            or declared_size < 0
        ):
            raise BackupIntegrityError("Backed-up artifact manifest identity is invalid")
        object_relative = f"artifacts/objects/sha256/{digest[:2]}/{digest}"
        if object_relative not in expected_paths:
            raise BackupIntegrityError("Backed-up artifact manifest has no matching object")
        object_path = root / _safe_relative_path(object_relative)
        if _sha256_file(object_path) != digest or object_path.stat().st_size != declared_size:
            raise BackupIntegrityError("Backed-up artifact manifest does not match its object")

    return manifest


def restore_backup(backup_root: str | Path, destination_root: str | Path) -> Path:
    """Restore through staging and leave a mandatory reconciliation marker."""

    backup = Path(backup_root)
    manifest = verify_backup(backup)
    destination = Path(destination_root)
    if destination.exists():
        raise BackupError("Restore destination already exists")
    if _inside(destination, backup):
        raise BackupError("Restore destination must be outside the backup bundle")
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".autotrade-restore-", dir=destination.parent))
    try:
        for item in manifest["files"]:
            relative = _safe_relative_path(item["path"])
            source = backup / relative
            target = stage / relative
            _copy_file_durable(source, target)
            if _sha256_file(target) != item["sha256"].removeprefix("sha256:"):
                raise BackupIntegrityError("Restored payload digest mismatch")

        journal_path = stage / "state" / "journal.sqlite3"
        owner_scopes = _recovery_owner_scopes_from_journal(journal_path)
        if len(owner_scopes) > 1:
            raise BackupIntegrityError(
                "Restore journal has multiple recovery owner scopes; "
                "explicit scoped restore is required"
            )
        owner_scope = owner_scopes[0] if owner_scopes else "default"
        owner_chain = _recovery_owner_chain_from_journal(
            journal_path,
            owner_scope=owner_scope,
        )
        source_owner = owner_chain[-1] if owner_chain else None
        marker = {
            "schema_version": 2,
            "status": "RECONCILIATION_REQUIRED",
            "restored_at": _utc_now(),
            "reason": "RECONCILIATION_AND_OWNERSHIP_FENCING_REQUIRED",
            "backup_manifest_sha256": (backup / MANIFEST_DIGEST_NAME)
            .read_text(encoding="ascii")
            .strip(),
            "source_owner_scope": owner_scope,
            "source_owner_id": (
                source_owner.owner_id if source_owner is not None else None
            ),
            "source_owner_epoch": (
                source_owner.epoch if source_owner is not None else None
            ),
        }
        _write_bytes_durable(stage / RESTORE_MARKER_NAME, _canonical_json(marker))
        _fsync_directory_tree(stage)
        os.replace(stage, destination)
        _fsync_directory(destination.parent)
        return destination
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise



def complete_restore_reconciliation(
    destination_root: str | Path,
    *,
    controller: RecoveryController,
    reconciliation_checkpoint_event_id: str,
    completed_at: str,
) -> dict[str, Any]:
    """Durably clear a restore gate from journal-issued reconciliation and fence evidence.

    Reconciliation authority is recovered from the exact current AccountReconciled
    event for the current recovery owner. Callers cannot supply a copied
    ReconciliationResult. Sender-fence issuer authenticity is validated separately
    by the fencing evidence boundary.
    """

    root = Path(destination_root)
    marker = _read_restore_marker(root)
    if marker["status"] == "RECONCILIATION_COMPLETE":
        if restore_requires_reconciliation(root):
            raise BackupIntegrityError(
                "Existing restore completion proof is invalid"
            )
        proof_path = root / RESTORE_COMPLETION_PROOF_NAME
        return json.loads(proof_path.read_text(encoding="utf-8"))

    if not isinstance(controller, RecoveryController):
        raise TypeError("controller must be RecoveryController")
    completed = _utc_text(completed_at, name="completed_at")
    restored = _utc_text(marker["restored_at"], name="restored_at")
    if completed < restored:
        raise BackupError("Restore completion cannot precede restore")

    source_owner_id = marker.get("source_owner_id")
    source_owner_epoch = marker.get("source_owner_epoch")
    if source_owner_id is None or source_owner_epoch is None:
        raise BackupError(
            "Restore completion requires a durable source owner fence"
        )
    expected_journal = (root / "state" / "journal.sqlite3").resolve(
        strict=False
    )
    durable_path = controller.durable_owner_store_path
    if (
        durable_path is None
        or durable_path.resolve(strict=False) != expected_journal
        or controller.owner_scope != marker["source_owner_scope"]
    ):
        raise BackupError(
            "Recovery controller is not bound to the restored journal and owner scope"
        )

    chain = controller.durable_owner_chain()
    if (
        not chain
        or controller.owner is None
        or controller.owner != chain[-1]
        or source_owner_epoch > len(chain)
        or chain[source_owner_epoch - 1]
        != OwnerFence(source_owner_id, source_owner_epoch)
    ):
        raise BackupError(
            "Recovery controller owner chain does not descend from restored owner"
        )
    current_owner = chain[-1]
    if current_owner.epoch <= source_owner_epoch:
        raise BackupError(
            "Restore completion requires a new durably fenced sender owner"
        )
    if (
        controller.state is not HostState.READY
        or not controller.provider_reconciled
        or controller.unresolved_attempts
        or not controller.storage_writable
        or not controller.clock_trusted
    ):
        raise BackupError(
            "Recovery controller is not READY with reconciled durable state"
        )

    reconciliation_proof = _restore_reconciliation_checkpoint_proof(
        JournalStore(expected_journal),
        checkpoint_event_id=reconciliation_checkpoint_event_id,
        current_owner=current_owner,
    )

    transition_chain = chain[source_owner_epoch - 1 :]
    if len(transition_chain) < 2:
        raise BackupError(
            "Restore completion requires a post-restore sender-fence transition"
        )

    evidence: list[dict[str, Any]] = []
    for index, new_owner in enumerate(transition_chain[1:]):
        item = controller.durable_sender_fence_proof(new_owner.epoch)
        old_owner = transition_chain[index]
        if (
            item["old_owner_id"] != old_owner.owner_id
            or item["old_owner_epoch"] != old_owner.epoch
            or item["new_owner_id"] != new_owner.owner_id
            or item["new_owner_epoch"] != new_owner.epoch
            or item["fence_context_digest"]
            != marker["backup_manifest_sha256"]
        ):
            raise BackupError(
                "Journal-issued sender fence is not bound to this restore transition"
            )
        evidence.append(item)

    proof = {
        "schema_version": 3,
        "backup_manifest_sha256": marker["backup_manifest_sha256"],
        "restored_at": marker["restored_at"],
        "completed_at": completed.isoformat().replace("+00:00", "Z"),
        "owner_scope": marker["source_owner_scope"],
        "source_owner_id": source_owner_id,
        "source_owner_epoch": source_owner_epoch,
        "current_owner_id": current_owner.owner_id,
        "current_owner_epoch": current_owner.epoch,
        "sender_fence_events": evidence,
        "reconciliation": reconciliation_proof,
    }
    proof_bytes = _canonical_json(proof)
    proof_hash = "sha256:" + _sha256_bytes(proof_bytes)
    _write_bytes_durable(
        root / RESTORE_COMPLETION_PROOF_NAME,
        proof_bytes,
    )
    completed_marker = {
        **marker,
        "status": "RECONCILIATION_COMPLETE",
        "completed_at": proof["completed_at"],
        "completion_proof_sha256": proof_hash,
        "current_owner_id": current_owner.owner_id,
        "current_owner_epoch": current_owner.epoch,
    }
    _write_bytes_durable(
        root / RESTORE_MARKER_NAME,
        _canonical_json(completed_marker),
    )
    return proof


def restore_requires_reconciliation(destination_root: str | Path) -> bool:
    """Return False only while all durable completion identities still verify."""

    root = Path(destination_root)
    try:
        marker = _read_restore_marker(root)
    except BackupIntegrityError:
        return True
    if marker["status"] != "RECONCILIATION_COMPLETE":
        return True

    try:
        expected_proof = _canonical_sha256_ref(
            marker.get("completion_proof_sha256"),
            name="completion_proof_sha256",
        )
        current_owner_id = _nonempty_text(
            marker.get("current_owner_id"),
            name="current_owner_id",
        )
        current_owner_epoch = marker.get("current_owner_epoch")
        if (
            isinstance(current_owner_epoch, bool)
            or not isinstance(current_owner_epoch, int)
            or current_owner_epoch < 1
        ):
            return True
        proof_path = root / RESTORE_COMPLETION_PROOF_NAME
        if proof_path.is_symlink() or not proof_path.is_file():
            return True
        proof_bytes = proof_path.read_bytes()
        if expected_proof != "sha256:" + _sha256_bytes(proof_bytes):
            return True
        proof = json.loads(proof_bytes)
        expected_keys = {
            "schema_version",
            "backup_manifest_sha256",
            "restored_at",
            "completed_at",
            "owner_scope",
            "source_owner_id",
            "source_owner_epoch",
            "current_owner_id",
            "current_owner_epoch",
            "sender_fence_events",
            "reconciliation",
        }
        if not isinstance(proof, dict) or set(proof) != expected_keys:
            return True
        if (
            proof["schema_version"] != 3
            or proof["backup_manifest_sha256"]
            != marker["backup_manifest_sha256"]
            or proof["restored_at"] != marker["restored_at"]
            or proof["completed_at"] != marker.get("completed_at")
            or proof["owner_scope"] != marker["source_owner_scope"]
            or proof["source_owner_id"] != marker["source_owner_id"]
            or proof["source_owner_epoch"] != marker["source_owner_epoch"]
            or proof["current_owner_id"] != current_owner_id
            or proof["current_owner_epoch"] != current_owner_epoch
        ):
            return True
        completed = _utc_text(proof["completed_at"], name="completed_at")
        restored = _utc_text(proof["restored_at"], name="restored_at")
        if completed < restored:
            return True

        chain = _recovery_owner_chain_from_journal(
            root / "state" / "journal.sqlite3",
            owner_scope=proof["owner_scope"],
        )
        source_epoch = proof["source_owner_epoch"]
        current_owner = OwnerFence(current_owner_id, current_owner_epoch)
        if (
            not chain
            or source_epoch > len(chain)
            or chain[source_epoch - 1]
            != OwnerFence(proof["source_owner_id"], source_epoch)
            or chain[-1] != current_owner
        ):
            return True

        stored_reconciliation = proof["reconciliation"]
        if not isinstance(stored_reconciliation, dict):
            return True
        checkpoint_event_id = stored_reconciliation.get("checkpoint_event_id")
        loaded_reconciliation = _restore_reconciliation_checkpoint_proof(
            JournalStore(root / "state" / "journal.sqlite3"),
            checkpoint_event_id=checkpoint_event_id,
            current_owner=current_owner,
        )
        if loaded_reconciliation != stored_reconciliation:
            return True

        transition_chain = chain[source_epoch - 1 :]
        stored_evidence = proof["sender_fence_events"]
        if (
            not isinstance(stored_evidence, list)
            or len(stored_evidence) != len(transition_chain) - 1
            or not stored_evidence
        ):
            return True
        fence_reader = RecoveryController(
            owner_store=JournalStore(root / "state" / "journal.sqlite3"),
            owner_scope=proof["owner_scope"],
        )
        seen_event_ids: set[str] = set()
        for index, stored in enumerate(stored_evidence):
            if not isinstance(stored, dict):
                return True
            new_owner = transition_chain[index + 1]
            loaded = fence_reader.durable_sender_fence_proof(new_owner.epoch)
            event_id = loaded.get("event_id")
            if not isinstance(event_id, str) or event_id in seen_event_ids:
                return True
            seen_event_ids.add(event_id)
            if loaded != stored:
                return True
            old_owner = transition_chain[index]
            if (
                loaded["old_owner_id"] != old_owner.owner_id
                or loaded["old_owner_epoch"] != old_owner.epoch
                or loaded["new_owner_id"] != new_owner.owner_id
                or loaded["new_owner_epoch"] != new_owner.epoch
                or loaded["fence_context_digest"]
                != proof["backup_manifest_sha256"]
            ):
                return True
    except (
        BackupError,
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        ValueError,
        TypeError,
        sqlite3.Error,
    ):
        return True
    return False
