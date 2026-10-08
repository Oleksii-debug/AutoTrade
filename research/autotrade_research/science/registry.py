"""Append-only scientific protocol and holdout-access registry foundation."""

from __future__ import annotations

from contextlib import contextmanager

from dataclasses import dataclass
from datetime import date, datetime, timezone
from hashlib import sha256
import json
import re
from pathlib import Path
import sqlite3
from typing import Any
from uuid import UUID, uuid4

from ..data.vintages import HistoricalDataError, HistoricalVintageRegistry


# Freeze the exact WP-10 class/dispatch surface used by terminal holdout
# preregistration. Exact-type checks alone are not sufficient: Python class
# attributes remain mutable after import and a rebound load/_path method would
# execute inside this authority boundary.
_HISTORICAL_VINTAGE_REGISTRY_CLASS = HistoricalVintageRegistry
_HISTORICAL_VINTAGE_LOAD_DESCRIPTOR = HistoricalVintageRegistry.__dict__["load"]
_HISTORICAL_VINTAGE_PATH_DESCRIPTOR = HistoricalVintageRegistry.__dict__["_path"]


def _assert_historical_vintage_class_authority_intact() -> None:
    if HistoricalVintageRegistry is not _HISTORICAL_VINTAGE_REGISTRY_CLASS:
        raise TypeError("HistoricalVintageRegistry class authority changed")
    expected = {
        "load": _HISTORICAL_VINTAGE_LOAD_DESCRIPTOR,
        "_path": _HISTORICAL_VINTAGE_PATH_DESCRIPTOR,
    }
    for name, descriptor in expected.items():
        if _HISTORICAL_VINTAGE_REGISTRY_CLASS.__dict__.get(name) is not descriptor:
            raise TypeError(
                "HistoricalVintageRegistry class authority descriptor changed: "
                + name
            )


REQUIRED_PROTOCOL_FIELDS = {
    "hypothesis",
    "strategy",
    "features",
    "search_space",
    "train_period",
    "validation_period",
    "test_period",
    "forward_period",
    "labels",
    "horizons",
    "purge_embargo",
    "universe",
    "cost_fill_model",
    "baselines",
    "primary_metrics",
    "secondary_metrics",
    "trial_budget",
    "stopping_rules",
    "statistical_estimator",
    "multiplicity_treatment",
    "minimum_practical_effect",
    "risk_constraints",
    "retention_tolerances",
    "promotion_rule",
}


class ProtocolConflict(ValueError):
    pass


class ProtocolViolation(ValueError):
    pass


def _reject_binary_float(payload: Any, path: str = "$") -> None:
    if isinstance(payload, float):
        raise ProtocolViolation(f"binary float is not permitted in frozen scientific evidence: {path}")
    if isinstance(payload, dict):
        for key, value in payload.items():
            _reject_binary_float(value, f"{path}.{key}")
    elif isinstance(payload, (list, tuple)):
        for index, value in enumerate(payload):
            _reject_binary_float(value, f"{path}[{index}]")


def _canonical(payload: Any) -> str:
    _reject_binary_float(payload)
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _hash(payload: Any) -> str:
    return "sha256:" + sha256(_canonical(payload).encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _registered_utc(value: Any, name: str) -> datetime:
    if type(value) is not str or not value or value != value.strip():
        raise ProtocolViolation(f"{name} must be exact timezone-aware ISO text")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ProtocolViolation(
            f"{name} must be exact timezone-aware ISO text"
        ) from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProtocolViolation(f"{name} must include timezone authority")
    return parsed.astimezone(timezone.utc)


def _id(value: str | None = None) -> str:
    return str(UUID(value)) if value is not None else str(uuid4())


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    return value.strip()


def _period(payload: Any, name: str) -> tuple[date, date]:
    if not isinstance(payload, dict) or set(payload) != {"start", "end"}:
        raise ProtocolViolation(f"{name} must contain exactly start and end")
    start_raw = _text(payload.get("start"), f"{name}.start")
    end_raw = _text(payload.get("end"), f"{name}.end")
    try:
        start = date.fromisoformat(start_raw)
        end = date.fromisoformat(end_raw)
    except ValueError as exc:
        raise ProtocolViolation(f"{name} must use canonical YYYY-MM-DD ISO calendar dates") from exc
    if start_raw != start.isoformat() or end_raw != end.isoformat():
        raise ProtocolViolation(f"{name} must use canonical YYYY-MM-DD ISO calendar dates")
    if start > end:
        raise ProtocolViolation(f"{name} start cannot follow end")
    return start, end


_DURATION_RE = re.compile(r"^(0|[1-9][0-9]*)(s|m|h|d)$")
_DURATION_MULTIPLIERS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def _duration_seconds(value: Any, name: str, *, allow_zero: bool = False) -> int:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ProtocolViolation(
            f"{name} must use canonical duration <integer><s|m|h|d>"
        )
    match = _DURATION_RE.fullmatch(value)
    if match is None:
        raise ProtocolViolation(
            f"{name} must use canonical duration <integer><s|m|h|d>"
        )
    amount = int(match.group(1))
    if amount == 0 and not allow_zero:
        raise ProtocolViolation(f"{name} must be positive")
    return amount * _DURATION_MULTIPLIERS[match.group(2)]


def _validate_causal_periods(payload: dict[str, Any]) -> None:
    names = ("train_period", "validation_period", "test_period", "forward_period")
    parsed = [(name, *_period(payload[name], name)) for name in names]
    for (left_name, _left_start, left_end), (right_name, right_start, _right_end) in zip(parsed, parsed[1:]):
        if left_end >= right_start:
            raise ProtocolViolation(
                f"{left_name} must end before {right_name} starts"
            )

    horizons = payload.get("horizons")
    if not isinstance(horizons, list) or not horizons:
        raise ProtocolViolation("horizons must be a non-empty list")
    horizon_seconds = [
        _duration_seconds(value, f"horizons[{index}]")
        for index, value in enumerate(horizons)
    ]
    longest_horizon = max(horizon_seconds)

    purge_embargo = payload.get("purge_embargo")
    if not isinstance(purge_embargo, dict) or set(purge_embargo) != {"purge", "embargo"}:
        raise ProtocolViolation(
            "purge_embargo must contain exactly purge and embargo"
        )
    purge_seconds = _duration_seconds(
        purge_embargo["purge"],
        "purge_embargo.purge",
        allow_zero=True,
    )
    embargo_seconds = _duration_seconds(
        purge_embargo["embargo"],
        "purge_embargo.embargo",
        allow_zero=True,
    )
    if purge_seconds < longest_horizon:
        raise ProtocolViolation(
            "purge must cover the longest registered label horizon"
        )
    if embargo_seconds < longest_horizon:
        raise ProtocolViolation(
            "embargo must cover the longest registered dependency horizon"
        )

    required_gap_seconds = max(longest_horizon, purge_seconds, embargo_seconds)
    for (left_name, _left_start, left_end), (right_name, right_start, _right_end) in zip(parsed, parsed[1:]):
        actual_gap_seconds = (right_start - left_end).days * 86400
        if actual_gap_seconds < required_gap_seconds:
            raise ProtocolViolation(
                f"{left_name} to {right_name} gap is shorter than the "
                "registered purge/embargo dependency horizon"
            )


_HOLDOUT_IDENTITY_FIELDS = {
    "dataset_digest",
    "segment_start",
    "segment_end",
    "role",
}
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def _holdout_identity(payload: Any) -> tuple[str, str]:
    """Return immutable holdout identity hash and canonical identity bytes.

    Display aliases are deliberately excluded.  Contamination follows the
    physical/versioned evidence segment: dataset digest + exact causal window +
    protocol role.
    """

    if not isinstance(payload, dict) or set(payload) != _HOLDOUT_IDENTITY_FIELDS:
        raise ProtocolViolation(
            "holdout_identity must contain exactly dataset_digest, "
            "segment_start, segment_end and role"
        )
    dataset_digest = _text(payload.get("dataset_digest"), "holdout_identity.dataset_digest")
    if _SHA256_RE.fullmatch(dataset_digest) is None:
        raise ProtocolViolation("holdout_identity.dataset_digest must be canonical sha256")
    role = _text(payload.get("role"), "holdout_identity.role").upper()
    start_raw = _text(payload.get("segment_start"), "holdout_identity.segment_start")
    end_raw = _text(payload.get("segment_end"), "holdout_identity.segment_end")
    try:
        start = date.fromisoformat(start_raw)
        end = date.fromisoformat(end_raw)
    except ValueError as exc:
        raise ProtocolViolation(
            "holdout_identity segment bounds must use canonical YYYY-MM-DD ISO calendar dates"
        ) from exc
    if start_raw != start.isoformat() or end_raw != end.isoformat():
        raise ProtocolViolation(
            "holdout_identity segment bounds must use canonical YYYY-MM-DD ISO calendar dates"
        )
    if start > end:
        raise ProtocolViolation("holdout_identity segment_start cannot follow segment_end")
    normalized = {
        "dataset_digest": dataset_digest,
        "segment_start": start.isoformat(),
        "segment_end": end.isoformat(),
        "role": role,
    }
    canonical = _canonical(normalized)
    return _hash(normalized), canonical


def _immutable_artifact_ref(value: Any, name: str) -> str:
    reference = _text(value, name)
    prefix = "artifact:"
    marker = "@sha256:"
    if not reference.startswith(prefix) or marker not in reference:
        raise ValueError(
            f"{name} must bind an immutable artifact and SHA-256 digest"
        )
    artifact_id, digest = reference[len(prefix):].split(marker, 1)
    try:
        canonical_id = str(UUID(artifact_id))
    except (ValueError, TypeError, AttributeError) as error:
        raise ValueError(f"{name} artifact id must be a UUID") from error
    if artifact_id != canonical_id:
        raise ValueError(f"{name} artifact id must use canonical UUID text")
    if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
        raise ValueError(
            f"{name} must use a canonical lowercase SHA-256 digest"
        )
    return reference


def _registered_protocol_payload(row: sqlite3.Row) -> dict[str, Any]:
    """Decode and revalidate one stored protocol row before using its authority."""

    try:
        payload = json.loads(row["payload_json"])
    except (json.JSONDecodeError, TypeError) as error:
        raise ProtocolViolation("registered protocol payload is corrupt") from error
    if (
        type(payload) is not dict
        or _canonical(payload) != row["payload_json"]
        or _hash(payload) != row["protocol_hash"]
    ):
        raise ProtocolViolation("registered protocol integrity mismatch")
    return payload


def _registered_trial_payload(row: sqlite3.Row) -> dict[str, Any]:
    """Decode and revalidate one stored terminal trial before authority use."""

    status = row["status"]
    if type(status) is not str or status not in {
        "COMPLETED",
        "FAILED",
        "DISCARDED",
        "CANCELLED",
    }:
        raise ProtocolViolation("registered trial status is corrupt")
    try:
        payload = json.loads(row["payload_json"])
    except (json.JSONDecodeError, TypeError) as error:
        raise ProtocolViolation("registered trial payload is corrupt") from error
    if (
        type(payload) is not dict
        or not payload
        or _canonical(payload) != row["payload_json"]
        or _hash(payload) != row["payload_hash"]
    ):
        raise ProtocolViolation("registered trial payload integrity mismatch")
    return payload


def _locked_holdout_binding_hash(
    *,
    protocol_id: str,
    dataset_id: str,
    dataset_version: int,
    dataset_digest: str,
    holdout_identity_hash: str,
    created_at: str,
) -> str:
    return _hash(
        {
            "protocol_id": protocol_id,
            "dataset_id": dataset_id,
            "dataset_version": dataset_version,
            "dataset_digest": dataset_digest,
            "holdout_identity_hash": holdout_identity_hash,
            "created_at": created_at,
        }
    )


@dataclass(frozen=True)
class ProtocolRegistration:
    protocol_id: str
    protocol_hash: str
    created_at: str


@dataclass(frozen=True)
class LockedHoldoutRegistration:
    """Append-only protocol binding to one physical WP-10 forward holdout."""

    protocol_id: str
    dataset_id: str
    dataset_version: int
    dataset_digest: str
    holdout_identity_hash: str
    segment_start: str
    segment_end: str
    role: str
    created_at: str

    def identity(self) -> dict[str, str]:
        return {
            "dataset_digest": self.dataset_digest,
            "segment_start": self.segment_start,
            "segment_end": self.segment_end,
            "role": self.role,
        }


@dataclass(frozen=True)
class TrialCompletenessEvidence:
    """One immutable ScientificRegistry trial-population snapshot.

    This value is descriptive evidence, not bearer authority: downstream
    qualification must obtain it from the exact canonical ScientificRegistry at
    use time rather than trusting a caller-constructed instance.
    """

    protocol_id: str
    protocol_hash: str
    trial_budget: int
    recorded_trials: int
    remaining_trial_budget: int
    trial_log_hash: str
    stopping_rules_hash: str
    statuses: tuple[tuple[str, int], ...]
    includes_non_successes: bool

    @property
    def complete(self) -> bool:
        return (
            self.recorded_trials == self.trial_budget
            and self.remaining_trial_budget == 0
        )

    @property
    def digest(self) -> str:
        return _hash(
            {
                "protocol_id": self.protocol_id,
                "protocol_hash": self.protocol_hash,
                "trial_budget": self.trial_budget,
                "recorded_trials": self.recorded_trials,
                "remaining_trial_budget": self.remaining_trial_budget,
                "trial_log_hash": self.trial_log_hash,
                "stopping_rules_hash": self.stopping_rules_hash,
                "statuses": [[name, count] for name, count in self.statuses],
                "includes_non_successes": self.includes_non_successes,
            }
        )


@dataclass(frozen=True)
class LockedEvaluationEvidence:
    evaluation_id: str
    protocol_id: str
    holdout_id: str
    holdout_identity_hash: str
    protocol_hash: str
    result_hash: str
    prior_access_count: int
    untouched: bool
    result: dict[str, Any]
    created_at: str


class ScientificRegistry:
    def __init__(self, path: str | Path):
        if type(path) is str:
            candidate = Path(path)
        elif type(path) is type(Path()):
            candidate = path
        else:
            raise TypeError("path must be exact string or pathlib path")
        # Freeze caller-relative/CWD-sensitive authority at construction.  Every
        # later SQLite connection must target the same lexical backing path even
        # if another component changes the process working directory.
        self._path = candidate.resolve(strict=False)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def __setattr__(self, name: str, value: Any) -> None:
        if name in {"path", "_path"} and "_path" in vars(self):
            raise AttributeError("ScientificRegistry backing path is immutable")
        object.__setattr__(self, name, value)

    @property
    def path(self) -> Path:
        """Frozen backing path selected at construction."""

        return self._path

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

    def _init(self) -> None:
        with self._connect() as con:
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS protocols(
                    protocol_id TEXT PRIMARY KEY,
                    protocol_hash TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS trials(
                    trial_id TEXT PRIMARY KEY,
                    protocol_id TEXT NOT NULL REFERENCES protocols(protocol_id),
                    status TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS holdout_access(
                    access_id TEXT PRIMARY KEY,
                    protocol_id TEXT NOT NULL REFERENCES protocols(protocol_id),
                    holdout_id TEXT NOT NULL,
                    purpose TEXT NOT NULL,
                    accessed_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS evaluations(
                    evaluation_id TEXT PRIMARY KEY,
                    protocol_id TEXT NOT NULL REFERENCES protocols(protocol_id),
                    holdout_id TEXT NOT NULL,
                    protocol_hash TEXT NOT NULL,
                    result_hash TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    prior_access_count INTEGER NOT NULL,
                    untouched INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS holdouts(
                    holdout_identity_hash TEXT PRIMARY KEY,
                    identity_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS holdout_aliases(
                    holdout_id TEXT PRIMARY KEY,
                    holdout_identity_hash TEXT NOT NULL REFERENCES holdouts(holdout_identity_hash),
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS protocol_locked_holdouts(
                    protocol_id TEXT PRIMARY KEY REFERENCES protocols(protocol_id),
                    dataset_id TEXT NOT NULL,
                    dataset_version INTEGER NOT NULL,
                    dataset_digest TEXT NOT NULL,
                    holdout_identity_hash TEXT NOT NULL REFERENCES holdouts(holdout_identity_hash),
                    created_at TEXT NOT NULL,
                    binding_hash TEXT NOT NULL
                );

                CREATE TRIGGER IF NOT EXISTS protocols_no_update
                BEFORE UPDATE ON protocols BEGIN
                    SELECT RAISE(ABORT, 'protocols are append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS protocols_no_delete
                BEFORE DELETE ON protocols BEGIN
                    SELECT RAISE(ABORT, 'protocols are append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS trials_no_update
                BEFORE UPDATE ON trials BEGIN
                    SELECT RAISE(ABORT, 'trials are append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS trials_no_delete
                BEFORE DELETE ON trials BEGIN
                    SELECT RAISE(ABORT, 'trials are append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS holdout_access_no_update
                BEFORE UPDATE ON holdout_access BEGIN
                    SELECT RAISE(ABORT, 'holdout access is append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS holdout_access_no_delete
                BEFORE DELETE ON holdout_access BEGIN
                    SELECT RAISE(ABORT, 'holdout access is append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS evaluations_no_update
                BEFORE UPDATE ON evaluations BEGIN
                    SELECT RAISE(ABORT, 'evaluations are append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS evaluations_no_delete
                BEFORE DELETE ON evaluations BEGIN
                    SELECT RAISE(ABORT, 'evaluations are append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS holdouts_no_update
                BEFORE UPDATE ON holdouts BEGIN
                    SELECT RAISE(ABORT, 'holdout identities are append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS holdouts_no_delete
                BEFORE DELETE ON holdouts BEGIN
                    SELECT RAISE(ABORT, 'holdout identities are append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS holdout_aliases_no_update
                BEFORE UPDATE ON holdout_aliases BEGIN
                    SELECT RAISE(ABORT, 'holdout aliases are append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS holdout_aliases_no_delete
                BEFORE DELETE ON holdout_aliases BEGIN
                    SELECT RAISE(ABORT, 'holdout aliases are append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS protocol_locked_holdouts_no_update
                BEFORE UPDATE ON protocol_locked_holdouts BEGIN
                    SELECT RAISE(ABORT, 'protocol locked holdouts are append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS protocol_locked_holdouts_no_delete
                BEFORE DELETE ON protocol_locked_holdouts BEGIN
                    SELECT RAISE(ABORT, 'protocol locked holdouts are append-only');
                END;
                """
            )
            access_columns = {
                row["name"] for row in con.execute("PRAGMA table_info(holdout_access)")
            }
            if "holdout_identity_hash" not in access_columns:
                con.execute(
                    "ALTER TABLE holdout_access ADD COLUMN holdout_identity_hash TEXT"
                )
            evaluation_columns = {
                row["name"] for row in con.execute("PRAGMA table_info(evaluations)")
            }
            if "holdout_identity_hash" not in evaluation_columns:
                con.execute(
                    "ALTER TABLE evaluations ADD COLUMN holdout_identity_hash TEXT"
                )

    def register_protocol(self, payload: dict[str, Any], *, protocol_id: str | None = None) -> ProtocolRegistration:
        if not isinstance(payload, dict):
            raise TypeError("protocol payload must be an object")
        missing = sorted(REQUIRED_PROTOCOL_FIELDS - set(payload))
        if missing:
            raise ProtocolViolation("missing required protocol fields: " + ", ".join(missing))
        empty = sorted(
            name
            for name in REQUIRED_PROTOCOL_FIELDS
            if payload.get(name) is None
            or (isinstance(payload.get(name), str) and not payload[name].strip())
            or (isinstance(payload.get(name), (list, tuple, dict, set)) and not payload[name])
        )
        if empty:
            raise ProtocolViolation("required protocol fields cannot be empty: " + ", ".join(empty))
        if not isinstance(payload.get("trial_budget"), int) or isinstance(payload.get("trial_budget"), bool) or payload["trial_budget"] < 1:
            raise ProtocolViolation("trial_budget must be a positive integer")
        _validate_causal_periods(payload)
        identifier = _id(protocol_id)
        canonical = _canonical(payload)
        digest = _hash(payload)
        created = _now()
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT * FROM protocols WHERE protocol_id=?", (identifier,)).fetchone()
            if row is not None:
                if row["protocol_hash"] != digest or row["payload_json"] != canonical:
                    raise ProtocolConflict("registered protocol is immutable")
                return ProtocolRegistration(identifier, row["protocol_hash"], row["created_at"])
            con.execute(
                "INSERT INTO protocols(protocol_id,protocol_hash,payload_json,created_at) VALUES(?,?,?,?)",
                (identifier, digest, canonical, created),
            )
            con.commit()
        return ProtocolRegistration(identifier, digest, created)

    def protocol_registration(self, protocol_id: str) -> ProtocolRegistration:
        """Load and integrity-check one append-only protocol registration."""

        protocol = _id(protocol_id)
        with self._connect() as con:
            row = con.execute(
                "SELECT protocol_hash,payload_json,created_at FROM protocols WHERE protocol_id=?",
                (protocol,),
            ).fetchone()
        if row is None:
            raise KeyError(protocol)
        try:
            payload = json.loads(row["payload_json"])
        except json.JSONDecodeError as error:
            raise ProtocolViolation("registered protocol payload is corrupt") from error
        if (
            not isinstance(payload, dict)
            or _canonical(payload) != row["payload_json"]
            or _hash(payload) != row["protocol_hash"]
        ):
            raise ProtocolViolation("registered protocol integrity mismatch")
        return ProtocolRegistration(
            protocol_id=protocol,
            protocol_hash=row["protocol_hash"],
            created_at=row["created_at"],
        )

    @staticmethod
    def _registered_locked_holdout(
        con: sqlite3.Connection,
        *,
        protocol_id: str,
        protocol_payload: dict[str, Any],
    ) -> LockedHoldoutRegistration:
        row = con.execute(
            "SELECT * FROM protocol_locked_holdouts WHERE protocol_id=?",
            (protocol_id,),
        ).fetchone()
        if row is None:
            raise ProtocolViolation(
                "protocol lacks preregistered physical locked holdout"
            )
        raw_dataset_id = row["dataset_id"]
        if type(raw_dataset_id) is not str:
            raise ProtocolViolation(
                "preregistered locked holdout dataset identity is corrupt"
            )
        try:
            dataset_id = str(UUID(raw_dataset_id))
        except (ValueError, TypeError, AttributeError) as error:
            raise ProtocolViolation(
                "preregistered locked holdout dataset identity is corrupt"
            ) from error
        if dataset_id != raw_dataset_id:
            raise ProtocolViolation(
                "preregistered locked holdout dataset identity is noncanonical"
            )
        dataset_version = row["dataset_version"]
        if type(dataset_version) is not int or dataset_version < 1:
            raise ProtocolViolation(
                "preregistered locked holdout dataset version is corrupt"
            )
        dataset_digest = row["dataset_digest"]
        identity_hash = row["holdout_identity_hash"]
        if (
            type(dataset_digest) is not str
            or _SHA256_RE.fullmatch(dataset_digest) is None
            or type(identity_hash) is not str
            or _SHA256_RE.fullmatch(identity_hash) is None
        ):
            raise ProtocolViolation(
                "preregistered locked holdout digest identity is corrupt"
            )
        binding_hash = row["binding_hash"]
        expected_binding_hash = _locked_holdout_binding_hash(
            protocol_id=protocol_id,
            dataset_id=dataset_id,
            dataset_version=dataset_version,
            dataset_digest=dataset_digest,
            holdout_identity_hash=identity_hash,
            created_at=row["created_at"],
        )
        if (
            type(binding_hash) is not str
            or _SHA256_RE.fullmatch(binding_hash) is None
            or binding_hash != expected_binding_hash
        ):
            raise ProtocolViolation(
                "preregistered locked holdout binding integrity mismatch"
            )
        identity_row = con.execute(
            "SELECT identity_json FROM holdouts WHERE holdout_identity_hash=?",
            (identity_hash,),
        ).fetchone()
        if identity_row is None:
            raise ProtocolViolation(
                "preregistered locked holdout physical identity is missing"
            )
        try:
            identity_payload = json.loads(identity_row["identity_json"])
        except (json.JSONDecodeError, TypeError) as error:
            raise ProtocolViolation(
                "preregistered locked holdout physical identity is corrupt"
            ) from error
        canonical_hash, canonical_json = _holdout_identity(identity_payload)
        if (
            canonical_hash != identity_hash
            or canonical_json != identity_row["identity_json"]
            or identity_payload["dataset_digest"] != dataset_digest
        ):
            raise ProtocolViolation(
                "preregistered locked holdout physical identity integrity mismatch"
            )
        forward_start, forward_end = _period(
            protocol_payload["forward_period"],
            "forward_period",
        )
        if (
            identity_payload["role"] != "LOCKED_FORWARD"
            or identity_payload["segment_start"] != forward_start.isoformat()
            or identity_payload["segment_end"] != forward_end.isoformat()
        ):
            raise ProtocolViolation(
                "preregistered locked holdout does not match protocol forward authority"
            )
        registered_at = _registered_utc(
            row["created_at"],
            "preregistered locked holdout created_at",
        )
        first_trial = con.execute(
            "SELECT created_at FROM trials WHERE protocol_id=? "
            "ORDER BY created_at,trial_id LIMIT 1",
            (protocol_id,),
        ).fetchone()
        if first_trial is not None:
            first_trial_at = _registered_utc(
                first_trial["created_at"],
                "registered trial created_at",
            )
            if registered_at > first_trial_at:
                raise ProtocolViolation(
                    "locked holdout must be preregistered before the first trial"
                )
        return LockedHoldoutRegistration(
            protocol_id=protocol_id,
            dataset_id=dataset_id,
            dataset_version=dataset_version,
            dataset_digest=dataset_digest,
            holdout_identity_hash=identity_hash,
            segment_start=identity_payload["segment_start"],
            segment_end=identity_payload["segment_end"],
            role=identity_payload["role"],
            created_at=row["created_at"],
        )

    def preregister_locked_holdout(
        self,
        protocol_id: str,
        *,
        vintage_registry: HistoricalVintageRegistry,
        dataset_id: str,
        dataset_version: int,
    ) -> LockedHoldoutRegistration:
        """Bind one exact WP-10 historical vintage before scientific activity."""

        protocol = _id(protocol_id)
        _assert_historical_vintage_class_authority_intact()
        if type(vintage_registry) is not _HISTORICAL_VINTAGE_REGISTRY_CLASS:
            raise TypeError(
                "vintage_registry must be exact HistoricalVintageRegistry"
            )
        vintage_state = object.__getattribute__(vintage_registry, "__dict__")
        if (
            type(vintage_state) is not dict
            or set(vintage_state) != {"root"}
            or type(vintage_state["root"]) is not type(Path())
        ):
            raise TypeError(
                "vintage_registry has unexpected mutable instance state"
            )
        # Snapshot one absolute root before any dataset lookup. A relative Path
        # would otherwise be reinterpreted if process CWD changed between
        # registry construction and scientific preregistration.
        vintage_root = vintage_state["root"].resolve()
        if type(dataset_id) is not str:
            raise ProtocolViolation(
                "locked holdout dataset_id must be a canonical UUID"
            )
        try:
            canonical_dataset_id = str(UUID(dataset_id))
        except (ValueError, TypeError, AttributeError) as error:
            raise ProtocolViolation(
                "locked holdout dataset_id must be a canonical UUID"
            ) from error
        if canonical_dataset_id != dataset_id:
            raise ProtocolViolation(
                "locked holdout dataset_id must be a canonical UUID"
            )
        if type(dataset_version) is not int or dataset_version < 1:
            raise ProtocolViolation(
                "locked holdout dataset_version must be a positive integer"
            )
        try:
            _assert_historical_vintage_class_authority_intact()
            # Build a detached exact authority view without dispatching the
            # caller instance or the mutable class constructor. Pin _path on
            # the detached instance to the descriptor captured at import so
            # load() cannot be redirected through instance/class rebinding.
            authoritative_vintages = object.__new__(
                _HISTORICAL_VINTAGE_REGISTRY_CLASS
            )
            object.__setattr__(authoritative_vintages, "root", vintage_root)
            object.__setattr__(
                authoritative_vintages,
                "_path",
                lambda dataset, version: _HISTORICAL_VINTAGE_PATH_DESCRIPTOR(
                    authoritative_vintages,
                    dataset,
                    version,
                ),
            )
            vintage_manifest = _HISTORICAL_VINTAGE_LOAD_DESCRIPTOR(
                authoritative_vintages,
                canonical_dataset_id,
                dataset_version,
            )
            _assert_historical_vintage_class_authority_intact()
            dataset_digest = _hash(vintage_manifest)
        except (HistoricalDataError, FileNotFoundError, OSError, ValueError) as error:
            raise ProtocolViolation(
                "locked holdout historical vintage is unavailable or invalid"
            ) from error

        created = _now()
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            protocol_row = con.execute(
                "SELECT * FROM protocols WHERE protocol_id=?",
                (protocol,),
            ).fetchone()
            if protocol_row is None:
                raise KeyError(protocol)
            protocol_payload = _registered_protocol_payload(protocol_row)
            forward_start, forward_end = _period(
                protocol_payload["forward_period"],
                "forward_period",
            )
            coverage = vintage_manifest.get("coverage")
            if (
                type(coverage) is not dict
                or "from" not in coverage
                or "to" not in coverage
            ):
                raise ProtocolViolation(
                    "locked holdout historical vintage lacks coverage authority"
                )
            coverage_start = _registered_utc(
                coverage["from"],
                "locked holdout vintage coverage.from",
            )
            coverage_end = _registered_utc(
                coverage["to"],
                "locked holdout vintage coverage.to",
            )
            required_start = datetime(
                forward_start.year,
                forward_start.month,
                forward_start.day,
                tzinfo=timezone.utc,
            )
            required_end = datetime(
                forward_end.year,
                forward_end.month,
                forward_end.day,
                23,
                59,
                59,
                tzinfo=timezone.utc,
            )
            if coverage_start > required_start or coverage_end < required_end:
                raise ProtocolViolation(
                    "locked holdout historical vintage does not cover protocol forward_period"
                )
            availability = vintage_manifest.get("availability_policy")
            if type(availability) is not dict:
                raise ProtocolViolation(
                    "locked holdout historical vintage lacks availability authority"
                )
            availability_cutoff = _registered_utc(
                availability.get("cutoff"),
                "locked holdout vintage availability cutoff",
            )
            if availability_cutoff < required_end:
                raise ProtocolViolation(
                    "locked holdout historical vintage availability cutoff "
                    "does not cover protocol forward_period"
                )
            identity = {
                "dataset_digest": dataset_digest,
                "segment_start": forward_start.isoformat(),
                "segment_end": forward_end.isoformat(),
                "role": "LOCKED_FORWARD",
            }
            identity_hash, identity_json = _holdout_identity(identity)
            existing = con.execute(
                "SELECT * FROM protocol_locked_holdouts WHERE protocol_id=?",
                (protocol,),
            ).fetchone()
            if existing is not None:
                if (
                    existing["dataset_id"] != canonical_dataset_id
                    or existing["dataset_version"] != dataset_version
                    or existing["dataset_digest"] != dataset_digest
                    or existing["holdout_identity_hash"] != identity_hash
                ):
                    raise ProtocolConflict(
                        "protocol locked holdout is immutable"
                    )
                return self._registered_locked_holdout(
                    con,
                    protocol_id=protocol,
                    protocol_payload=protocol_payload,
                )

            prior_activity = sum(
                int(
                    con.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE protocol_id=?",
                        (protocol,),
                    ).fetchone()[0]
                )
                for table in ("trials", "evaluations", "holdout_access")
            )
            if prior_activity != 0:
                raise ProtocolViolation(
                    "locked holdout must be preregistered before the first "
                    "trial, evaluation, or holdout access"
                )
            holdout_row = con.execute(
                "SELECT identity_json FROM holdouts WHERE holdout_identity_hash=?",
                (identity_hash,),
            ).fetchone()
            if holdout_row is None:
                con.execute(
                    "INSERT INTO holdouts("
                    "holdout_identity_hash,identity_json,created_at"
                    ") VALUES(?,?,?)",
                    (identity_hash, identity_json, created),
                )
            elif holdout_row["identity_json"] != identity_json:
                raise ProtocolConflict(
                    "holdout identity hash was reused inconsistently"
                )
            binding_hash = _locked_holdout_binding_hash(
                protocol_id=protocol,
                dataset_id=canonical_dataset_id,
                dataset_version=dataset_version,
                dataset_digest=dataset_digest,
                holdout_identity_hash=identity_hash,
                created_at=created,
            )
            con.execute(
                "INSERT INTO protocol_locked_holdouts("
                "protocol_id,dataset_id,dataset_version,dataset_digest,"
                "holdout_identity_hash,created_at,binding_hash"
                ") VALUES(?,?,?,?,?,?,?)",
                (
                    protocol,
                    canonical_dataset_id,
                    dataset_version,
                    dataset_digest,
                    identity_hash,
                    created,
                    binding_hash,
                ),
            )
            return self._registered_locked_holdout(
                con,
                protocol_id=protocol,
                protocol_payload=protocol_payload,
            )

    def locked_holdout_registration(
        self,
        protocol_id: str,
    ) -> LockedHoldoutRegistration:
        protocol = _id(protocol_id)
        with self._connect() as con:
            protocol_row = con.execute(
                "SELECT * FROM protocols WHERE protocol_id=?",
                (protocol,),
            ).fetchone()
            if protocol_row is None:
                raise KeyError(protocol)
            protocol_payload = _registered_protocol_payload(protocol_row)
            return self._registered_locked_holdout(
                con,
                protocol_id=protocol,
                protocol_payload=protocol_payload,
            )

    def record_trial(
        self,
        protocol_id: str,
        *,
        status: str,
        payload: dict[str, Any],
        trial_id: str | None = None,
    ) -> str:
        protocol = _id(protocol_id)
        normalized = _text(status, "status").upper()
        if normalized not in {"COMPLETED", "FAILED", "DISCARDED", "CANCELLED"}:
            raise ProtocolViolation("trial status is invalid")
        if not isinstance(payload, dict) or not payload:
            raise ProtocolViolation("trial payload must be a non-empty object")
        identifier = _id(trial_id)
        canonical = _canonical(payload)
        digest = _hash(payload)
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            owner = con.execute(
                "SELECT * FROM protocols WHERE protocol_id=?",
                (protocol,),
            ).fetchone()
            if owner is None:
                raise KeyError(protocol)
            protocol_payload = _registered_protocol_payload(owner)
            # Trial search is admitted only after the exact physical forward
            # holdout is frozen.  Otherwise the search can observe outcomes
            # first and select the terminal evidence source later.
            self._registered_locked_holdout(
                con,
                protocol_id=protocol,
                protocol_payload=protocol_payload,
            )
            existing = con.execute("SELECT * FROM trials WHERE trial_id=?", (identifier,)).fetchone()
            if existing is not None:
                if (
                    existing["protocol_id"] != protocol
                    or existing["status"] != normalized
                    or existing["payload_hash"] != digest
                    or existing["payload_json"] != canonical
                ):
                    raise ProtocolConflict("trial identity was reused inconsistently")
                return identifier
            budget = protocol_payload["trial_budget"]
            used = con.execute("SELECT COUNT(*) FROM trials WHERE protocol_id=?", (protocol,)).fetchone()[0]
            if used >= budget:
                raise ProtocolViolation("registered trial budget exhausted")
            con.execute(
                "INSERT INTO trials(trial_id,protocol_id,status,payload_hash,payload_json,created_at) VALUES(?,?,?,?,?,?)",
                (identifier, protocol, normalized, digest, canonical, _now()),
            )
            con.commit()
        return identifier

    @staticmethod
    def _bind_holdout_identity(
        con: sqlite3.Connection,
        *,
        holdout_id: str,
        holdout_identity: dict[str, Any],
    ) -> str:
        identity_hash, identity_json = _holdout_identity(holdout_identity)
        row = con.execute(
            "SELECT identity_json FROM holdouts WHERE holdout_identity_hash=?",
            (identity_hash,),
        ).fetchone()
        if row is None:
            con.execute(
                "INSERT INTO holdouts(holdout_identity_hash,identity_json,created_at) "
                "VALUES(?,?,?)",
                (identity_hash, identity_json, _now()),
            )
        elif row["identity_json"] != identity_json:
            raise ProtocolConflict("holdout identity hash was reused inconsistently")

        alias = con.execute(
            "SELECT holdout_identity_hash FROM holdout_aliases WHERE holdout_id=?",
            (holdout_id,),
        ).fetchone()
        if alias is None:
            con.execute(
                "INSERT INTO holdout_aliases(holdout_id,holdout_identity_hash,created_at) "
                "VALUES(?,?,?)",
                (holdout_id, identity_hash, _now()),
            )
        elif alias["holdout_identity_hash"] != identity_hash:
            raise ProtocolConflict("holdout alias cannot be rebound to different evidence")
        return identity_hash

    def record_holdout_access(
        self,
        protocol_id: str,
        *,
        holdout_id: str,
        holdout_identity: dict[str, Any],
        purpose: str,
    ) -> str:
        protocol = _id(protocol_id)
        holdout = _text(holdout_id, "holdout_id")
        why = _text(purpose, "purpose")
        access_id = _id()
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            protocol_row = con.execute(
                "SELECT * FROM protocols WHERE protocol_id=?",
                (protocol,),
            ).fetchone()
            if protocol_row is None:
                raise KeyError(protocol)
            protocol_payload = _registered_protocol_payload(protocol_row)
            supplied_identity_hash, _ = _holdout_identity(holdout_identity)
            locked_holdout = self._registered_locked_holdout(
                con,
                protocol_id=protocol,
                protocol_payload=protocol_payload,
            )
            if supplied_identity_hash != locked_holdout.holdout_identity_hash:
                raise ProtocolViolation(
                    "holdout access identity must match preregistered physical holdout"
                )
            identity_hash = self._bind_holdout_identity(
                con,
                holdout_id=holdout,
                holdout_identity=locked_holdout.identity(),
            )
            con.execute(
                "INSERT INTO holdout_access("
                "access_id,protocol_id,holdout_id,purpose,accessed_at,holdout_identity_hash"
                ") VALUES(?,?,?,?,?,?)",
                (access_id, protocol, holdout, why, _now(), identity_hash),
            )
            con.commit()
        return access_id

    def holdout_access_count(self, protocol_id: str, holdout_id: str) -> int:
        protocol = _id(protocol_id)
        holdout = _text(holdout_id, "holdout_id")
        with self._connect() as con:
            return int(
                con.execute(
                    "SELECT COUNT(*) FROM holdout_access WHERE protocol_id=? AND holdout_id=?",
                    (protocol, holdout),
                ).fetchone()[0]
            )

    def register_evaluation(
        self,
        protocol_id: str,
        *,
        holdout_id: str,
        holdout_identity: dict[str, Any],
        result: dict[str, Any],
        evaluation_id: str | None = None,
    ) -> dict[str, Any]:
        protocol = _id(protocol_id)
        holdout = _text(holdout_id, "holdout_id")
        if not isinstance(result, dict) or not result:
            raise ProtocolViolation("evaluation result must be a non-empty object")
        if result.get("stopping_rule_triggered") is True:
            try:
                _immutable_artifact_ref(
                    result.get("stopping_evidence_ref"),
                    "stopping_evidence_ref",
                )
            except ValueError as error:
                raise ProtocolViolation(
                    "triggered stopping rule requires immutable artifact evidence"
                ) from error
        identifier = _id(evaluation_id)
        canonical = _canonical(result)
        result_hash = _hash(result)
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            p = con.execute("SELECT * FROM protocols WHERE protocol_id=?", (protocol,)).fetchone()
            if p is None:
                raise KeyError(protocol)
            protocol_payload = _registered_protocol_payload(p)
            locked_holdout = self._registered_locked_holdout(
                con,
                protocol_id=protocol,
                protocol_payload=protocol_payload,
            )
            supplied_identity_hash, _ = _holdout_identity(holdout_identity)
            if supplied_identity_hash != locked_holdout.holdout_identity_hash:
                raise ProtocolViolation(
                    "locked evaluation holdout identity must match "
                    "preregistered physical holdout"
                )

            # The physical holdout remains inaccessible until every registered
            # trial slot is terminal.  Caller-authored early-stop fields are
            # metadata only until a separate canonical adjudicator exists.
            trial_budget = protocol_payload["trial_budget"]
            trial_rows = con.execute(
                """
                SELECT trial_id,status,payload_hash,payload_json
                FROM trials
                WHERE protocol_id=?
                ORDER BY trial_id
                """,
                (protocol,),
            ).fetchall()
            for trial in trial_rows:
                _registered_trial_payload(trial)
            if len(trial_rows) != trial_budget:
                raise ProtocolViolation(
                    "locked holdout requires exact registered trial budget "
                    "before evaluation; no canonical early-stop adjudicator is installed"
                )

            identity_hash = self._bind_holdout_identity(
                con,
                holdout_id=holdout,
                holdout_identity=locked_holdout.identity(),
            )
            forward_start, forward_end = _period(
                protocol_payload["forward_period"],
                "forward_period",
            )
            identity_payload = json.loads(
                con.execute(
                    "SELECT identity_json FROM holdouts WHERE holdout_identity_hash=?",
                    (identity_hash,),
                ).fetchone()["identity_json"]
            )
            if identity_payload["role"] != "LOCKED_FORWARD":
                raise ProtocolViolation(
                    "locked evaluation holdout role must be LOCKED_FORWARD"
                )
            if (
                identity_payload["segment_start"] != forward_start.isoformat()
                or identity_payload["segment_end"] != forward_end.isoformat()
            ):
                raise ProtocolViolation(
                    "locked evaluation holdout segment must match registered forward_period"
                )
            existing = con.execute("SELECT * FROM evaluations WHERE evaluation_id=?", (identifier,)).fetchone()
            if existing is not None:
                if (
                    existing["protocol_id"] != protocol
                    or existing["holdout_id"] != holdout
                    or existing["holdout_identity_hash"] != identity_hash
                    or existing["result_hash"] != result_hash
                ):
                    raise ProtocolConflict("evaluation identity was reused inconsistently")
                return dict(existing)
            # Contamination follows immutable evidence identity globally, not
            # a protocol-local or caller-chosen display alias. Legacy alias-only
            # rows under the same label are conservatively included.
            prior_access = int(
                con.execute(
                    "SELECT COUNT(*) FROM holdout_access "
                    "WHERE holdout_identity_hash=? "
                    "OR (holdout_identity_hash IS NULL AND holdout_id=?)",
                    (identity_hash, holdout),
                ).fetchone()[0]
            )
            untouched = 1 if prior_access == 0 else 0
            con.execute(
                "INSERT INTO evaluations("
                "evaluation_id,protocol_id,holdout_id,protocol_hash,result_hash,result_json,"
                "prior_access_count,untouched,created_at,holdout_identity_hash"
                ") VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    identifier,
                    protocol,
                    holdout,
                    p["protocol_hash"],
                    result_hash,
                    canonical,
                    prior_access,
                    untouched,
                    _now(),
                    identity_hash,
                ),
            )
            con.execute(
                "INSERT INTO holdout_access("
                "access_id,protocol_id,holdout_id,purpose,accessed_at,holdout_identity_hash"
                ") VALUES(?,?,?,?,?,?)",
                (_id(), protocol, holdout, "LOCKED_EVALUATION", _now(), identity_hash),
            )
            con.commit()
            row = con.execute("SELECT * FROM evaluations WHERE evaluation_id=?", (identifier,)).fetchone()
            return dict(row)

    def locked_evaluation(self, evaluation_id: str) -> LockedEvaluationEvidence:
        identifier = _id(evaluation_id)
        with self._connect() as con:
            row = con.execute(
                "SELECT * FROM evaluations WHERE evaluation_id=?",
                (identifier,),
            ).fetchone()
            if row is None:
                raise KeyError(identifier)
            identity_hash = row["holdout_identity_hash"]
            if (
                not isinstance(identity_hash, str)
                or _SHA256_RE.fullmatch(identity_hash) is None
            ):
                raise ProtocolViolation(
                    "locked evaluation lacks immutable holdout identity"
                )
            identity_row = con.execute(
                "SELECT identity_json FROM holdouts WHERE holdout_identity_hash=?",
                (identity_hash,),
            ).fetchone()
        if identity_row is None:
            raise ProtocolViolation(
                "locked evaluation holdout identity is not registered"
            )
        try:
            identity_payload = json.loads(identity_row["identity_json"])
            canonical_identity_hash, canonical_identity_json = _holdout_identity(
                identity_payload
            )
        except (json.JSONDecodeError, ProtocolViolation, ValueError) as error:
            raise ProtocolViolation(
                "locked evaluation holdout identity is corrupt"
            ) from error
        if (
            canonical_identity_hash != identity_hash
            or canonical_identity_json != identity_row["identity_json"]
        ):
            raise ProtocolViolation(
                "locked evaluation holdout identity integrity mismatch"
            )
        try:
            result = json.loads(row["result_json"])
        except json.JSONDecodeError as error:
            raise ProtocolViolation("locked evaluation result is corrupt") from error
        if not isinstance(result, dict) or not result:
            raise ProtocolViolation("locked evaluation result is invalid")
        if (
            _canonical(result) != row["result_json"]
            or _hash(result) != row["result_hash"]
        ):
            raise ProtocolViolation("locked evaluation result hash mismatch")
        return LockedEvaluationEvidence(
            evaluation_id=row["evaluation_id"],
            protocol_id=row["protocol_id"],
            holdout_id=row["holdout_id"],
            holdout_identity_hash=identity_hash,
            protocol_hash=row["protocol_hash"],
            result_hash=row["result_hash"],
            prior_access_count=int(row["prior_access_count"]),
            untouched=bool(row["untouched"]),
            result=result,
            created_at=row["created_at"],
        )

    @contextmanager
    def candidate_promotion_evidence_guard(
        self,
        *,
        evaluation_id: str,
        protocol_id: str,
        protocol_hash: str,
        result_hash: str,
        candidate_id: str,
        artifact_hash: str,
        evaluation_status: str,
        retention_passed: bool,
        risk_passed: bool,
        authority_scope_id: str,
        evidence_valid_until: str,
    ):
        """Hold scientific writer authority through a dependent promotion commit.

        A preflight validation is not a commit fence. Another process could append
        holdout access or trial state after the untouched/complete checks and before
        ChampionRegistry durably changes routing. BEGIN IMMEDIATE serializes
        ScientificRegistry writers while ordinary WAL readers remain available.
        The caller must keep this context open through its dependent durable commit.
        """

        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            evidence = self.verify_candidate_promotion_evidence(
                evaluation_id=evaluation_id,
                protocol_id=protocol_id,
                protocol_hash=protocol_hash,
                result_hash=result_hash,
                candidate_id=candidate_id,
                artifact_hash=artifact_hash,
                evaluation_status=evaluation_status,
                retention_passed=retention_passed,
                risk_passed=risk_passed,
                authority_scope_id=authority_scope_id,
                evidence_valid_until=evidence_valid_until,
            )
            yield evidence

    def verify_candidate_promotion_evidence(
        self,
        *,
        evaluation_id: str,
        protocol_id: str,
        protocol_hash: str,
        result_hash: str,
        candidate_id: str,
        artifact_hash: str,
        evaluation_status: str,
        retention_passed: bool,
        risk_passed: bool,
        authority_scope_id: str,
        evidence_valid_until: str,
    ) -> LockedEvaluationEvidence:
        evidence = self.locked_evaluation(evaluation_id)
        expected_protocol = _id(protocol_id)
        if evidence.protocol_id != expected_protocol:
            raise ProtocolViolation(
                "candidate approval protocol_id does not match locked evaluation"
            )
        if evidence.protocol_hash != _text(protocol_hash, "protocol_hash"):
            raise ProtocolViolation(
                "candidate approval protocol_hash does not match locked evaluation"
            )
        if evidence.result_hash != _text(result_hash, "result_hash"):
            raise ProtocolViolation(
                "candidate approval result_hash does not match locked evaluation"
            )
        if not evidence.untouched or evidence.prior_access_count != 0:
            raise ProtocolViolation(
                "candidate promotion requires an untouched locked holdout evaluation"
            )

        # Terminal promotion authority requires a physical/versioned holdout
        # frozen before scientific activity. Legacy diagnostic evaluations that
        # selected a holdout only at evaluation time are permanently
        # non-promoting.
        with self._connect() as con:
            protocol_row = con.execute(
                "SELECT * FROM protocols WHERE protocol_id=?",
                (expected_protocol,),
            ).fetchone()
            if protocol_row is None:
                raise KeyError(expected_protocol)
            protocol_payload = _registered_protocol_payload(protocol_row)
            locked_holdout = self._registered_locked_holdout(
                con,
                protocol_id=expected_protocol,
                protocol_payload=protocol_payload,
            )
            if evidence.holdout_identity_hash != locked_holdout.holdout_identity_hash:
                raise ProtocolViolation(
                    "candidate promotion holdout differs from preregistered physical authority"
                )

            # Contamination belongs to the immutable physical holdout identity,
            # not a protocol-local display alias. Legacy alias-only accesses are
            # counted conservatively so a migration cannot manufacture an
            # untouched holdout.
            current_access_count = int(
                con.execute(
                    "SELECT COUNT(*) FROM holdout_access "
                    "WHERE holdout_identity_hash=? "
                    "OR (holdout_identity_hash IS NULL AND holdout_id=?)",
                    (evidence.holdout_identity_hash, evidence.holdout_id),
                ).fetchone()[0]
            )
        if current_access_count != 1:
            raise ProtocolViolation(
                "candidate promotion requires holdout to remain untouched after locked evaluation"
            )

        trial_state = self.completeness(expected_protocol)
        if trial_state["recorded_trials"] < 1:
            raise ProtocolViolation(
                "candidate promotion requires at least one registered trial"
            )
        with self._connect() as con:
            trial_rows = con.execute(
                "SELECT status,payload_hash,payload_json FROM trials "
                "WHERE protocol_id=?",
                (expected_protocol,),
            ).fetchall()
        candidate_trial_found = False
        for row in trial_rows:
            payload = _registered_trial_payload(row)
            if row["status"] != "COMPLETED":
                continue
            if (
                payload.get("candidate_id") == candidate_id
                and payload.get("artifact_hash") == artifact_hash
            ):
                candidate_trial_found = True
        if not candidate_trial_found:
            raise ProtocolViolation(
                "candidate promotion requires a completed registered trial "
                "bound to candidate_id and artifact_hash"
            )

        result = evidence.result
        required = {
            "candidate_id": _text(candidate_id, "candidate_id"),
            "artifact_hash": _text(artifact_hash, "artifact_hash"),
            "evaluation_status": _text(
                evaluation_status, "evaluation_status"
            ).upper(),
            "retention_passed": retention_passed,
            "risk_passed": risk_passed,
            "authority_scope_id": _text(
                authority_scope_id, "authority_scope_id"
            ),
            "evidence_valid_until": _text(
                evidence_valid_until, "evidence_valid_until"
            ),
            "recorded_trial_count": trial_state["recorded_trials"],
            "trial_budget": trial_state["trial_budget"],
            "trial_log_hash": trial_state["trial_log_hash"],
        }
        for field in ("retention_passed", "risk_passed"):
            if type(required[field]) is not bool:
                raise TypeError(f"{field} must be boolean")
        for field, expected in required.items():
            observed = result.get(field)
            if field == "evaluation_status" and isinstance(observed, str):
                observed = observed.upper()
            if observed != expected:
                raise ProtocolViolation(
                    f"candidate approval {field} does not match locked evaluation result"
                )

        for required_true in (
            "reproducible",
            "causal_audit_passed",
            "financial_invariants_passed",
            "trial_log_complete",
        ):
            if result.get(required_true) is not True:
                raise ProtocolViolation(
                    f"locked evaluation does not prove {required_true}"
                )
        if trial_state["remaining_trial_budget"] > 0:
            raise ProtocolViolation(
                "candidate promotion requires exact registered trial-budget exhaustion; "
                "no canonical early-stopping adjudicator is installed"
            )
        return evidence

    def trial_completeness_evidence(
        self,
        protocol_id: str,
    ) -> TrialCompletenessEvidence:
        """Read protocol + complete registered trial population from one DB cut."""

        protocol = _id(protocol_id)
        with self._connect() as con:
            # An explicit read transaction pins both queries to one WAL snapshot.
            # Without this, a concurrent record_trial() could commit between the
            # protocol and trial reads and produce a mixed authority cut.
            con.execute("BEGIN")
            p = con.execute(
                """
                SELECT protocol_hash,payload_json
                FROM protocols
                WHERE protocol_id=?
                """,
                (protocol,),
            ).fetchone()
            if p is None:
                raise KeyError(protocol)
            trial_rows = con.execute(
                """
                SELECT trial_id,status,payload_hash,payload_json
                FROM trials
                WHERE protocol_id=?
                ORDER BY trial_id
                """,
                (protocol,),
            ).fetchall()

        try:
            protocol_payload = json.loads(p["payload_json"])
        except json.JSONDecodeError as error:
            raise ProtocolViolation("registered protocol payload is corrupt") from error
        if (
            not isinstance(protocol_payload, dict)
            or _canonical(protocol_payload) != p["payload_json"]
            or _hash(protocol_payload) != p["protocol_hash"]
        ):
            raise ProtocolViolation("registered protocol integrity mismatch")
        budget = protocol_payload.get("trial_budget")
        if type(budget) is not int or budget < 1:
            raise ProtocolViolation("registered trial budget is invalid")
        if "stopping_rules" not in protocol_payload:
            raise ProtocolViolation("registered stopping rules are missing")

        log: list[dict[str, str]] = []
        counts: dict[str, int] = {}
        for row in trial_rows:
            _registered_trial_payload(row)
            status = row["status"]
            log.append(
                {
                    "trial_id": row["trial_id"],
                    "status": status,
                    "payload_hash": row["payload_hash"],
                }
            )
            counts[status] = counts.get(status, 0) + 1

        total = len(trial_rows)
        if total > budget:
            raise ProtocolViolation(
                "registered trial population exceeds immutable trial budget"
            )
        statuses = tuple(sorted(counts.items()))
        return TrialCompletenessEvidence(
            protocol_id=protocol,
            protocol_hash=p["protocol_hash"],
            trial_budget=budget,
            recorded_trials=total,
            remaining_trial_budget=budget - total,
            trial_log_hash=_hash(log),
            stopping_rules_hash=_hash(protocol_payload["stopping_rules"]),
            statuses=statuses,
            includes_non_successes=any(
                counts.get(value, 0)
                for value in ("FAILED", "DISCARDED", "CANCELLED")
            ),
        )

    def completeness(self, protocol_id: str) -> dict[str, Any]:
        evidence = self.trial_completeness_evidence(protocol_id)
        return {
            "trial_budget": evidence.trial_budget,
            "recorded_trials": evidence.recorded_trials,
            "remaining_trial_budget": evidence.remaining_trial_budget,
            "trial_log_hash": evidence.trial_log_hash,
            "stopping_rules_hash": evidence.stopping_rules_hash,
            "statuses": dict(evidence.statuses),
            "includes_non_successes": evidence.includes_non_successes,
        }
