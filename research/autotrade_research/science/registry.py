"""Append-only scientific protocol and holdout-access registry foundation."""

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


def _canonical(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _hash(payload: Any) -> str:
    return "sha256:" + sha256(_canonical(payload).encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _id(value: str | None = None) -> str:
    return str(UUID(value)) if value is not None else str(uuid4())


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    return value.strip()


def _protocol_instant(value: Any, name: str) -> datetime:
    text = _text(value, name)
    if not text.endswith("Z"):
        raise ProtocolViolation(f"{name} must be a canonical UTC instant ending in Z")
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as error:
        raise ProtocolViolation(f"{name} must be an ISO-8601 UTC instant") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProtocolViolation(f"{name} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _protocol_period(value: Any, name: str) -> tuple[datetime, datetime]:
    if type(value) is not dict or set(value) != {"start", "end"}:
        raise ProtocolViolation(
            f"{name} must be an object with exactly start and end"
        )
    start = _protocol_instant(value["start"], f"{name}.start")
    end = _protocol_instant(value["end"], f"{name}.end")
    if start >= end:
        raise ProtocolViolation(f"{name} must have start strictly before end")
    return start, end


def _nonnegative_seconds(value: Any, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ProtocolViolation(f"{name} must be a non-negative integer")
    return value


def _validate_causal_protocol_windows(payload: dict[str, Any]) -> None:
    periods = [
        ("train_period", _protocol_period(payload["train_period"], "train_period")),
        (
            "validation_period",
            _protocol_period(payload["validation_period"], "validation_period"),
        ),
        ("test_period", _protocol_period(payload["test_period"], "test_period")),
        (
            "forward_period",
            _protocol_period(payload["forward_period"], "forward_period"),
        ),
    ]
    exclusion = payload["purge_embargo"]
    if type(exclusion) is not dict or set(exclusion) != {
        "purge_seconds",
        "embargo_seconds",
    }:
        raise ProtocolViolation(
            "purge_embargo must contain exactly purge_seconds and embargo_seconds"
        )
    purge = _nonnegative_seconds(
        exclusion["purge_seconds"], "purge_embargo.purge_seconds"
    )
    embargo = _nonnegative_seconds(
        exclusion["embargo_seconds"], "purge_embargo.embargo_seconds"
    )
    # Periods are closed evidence windows.  Each adjacent split therefore needs
    # a strict temporal separation.  Purge and embargo are two constraints on
    # that same excluded boundary interval; the boundary must satisfy both,
    # hence the minimum gap is their maximum rather than caller-chosen prose.
    minimum_gap_seconds = max(purge, embargo)
    for (left_name, (_left_start, left_end)), (
        right_name,
        (right_start, _right_end),
    ) in zip(periods, periods[1:]):
        if left_end >= right_start:
            raise ProtocolViolation(
                f"{left_name} must end strictly before {right_name} starts"
            )
        gap_seconds = (right_start - left_end).total_seconds()
        if gap_seconds < minimum_gap_seconds:
            raise ProtocolViolation(
                f"{left_name}->{right_name} gap is shorter than registered "
                "purge/embargo requirement"
            )


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


@dataclass(frozen=True)
class ProtocolRegistration:
    protocol_id: str
    protocol_hash: str
    created_at: str


@dataclass(frozen=True)
class LockedEvaluationEvidence:
    evaluation_id: str
    protocol_id: str
    holdout_id: str
    protocol_hash: str
    result_hash: str
    prior_access_count: int
    untouched: bool
    result: dict[str, Any]
    created_at: str


class ScientificRegistry:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

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
                """
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
        _validate_causal_protocol_windows(payload)
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
            owner = con.execute("SELECT payload_json FROM protocols WHERE protocol_id=?", (protocol,)).fetchone()
            if owner is None:
                raise KeyError(protocol)
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
            budget = json.loads(owner["payload_json"])["trial_budget"]
            used = con.execute("SELECT COUNT(*) FROM trials WHERE protocol_id=?", (protocol,)).fetchone()[0]
            if used >= budget:
                raise ProtocolViolation("registered trial budget exhausted")
            con.execute(
                "INSERT INTO trials(trial_id,protocol_id,status,payload_hash,payload_json,created_at) VALUES(?,?,?,?,?,?)",
                (identifier, protocol, normalized, digest, canonical, _now()),
            )
            con.commit()
        return identifier

    def record_holdout_access(self, protocol_id: str, *, holdout_id: str, purpose: str) -> str:
        protocol = _id(protocol_id)
        holdout = _text(holdout_id, "holdout_id")
        why = _text(purpose, "purpose")
        access_id = _id()
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            if con.execute("SELECT 1 FROM protocols WHERE protocol_id=?", (protocol,)).fetchone() is None:
                raise KeyError(protocol)
            con.execute(
                "INSERT INTO holdout_access(access_id,protocol_id,holdout_id,purpose,accessed_at) VALUES(?,?,?,?,?)",
                (access_id, protocol, holdout, why, _now()),
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
            existing = con.execute("SELECT * FROM evaluations WHERE evaluation_id=?", (identifier,)).fetchone()
            if existing is not None:
                if existing["protocol_id"] != protocol or existing["holdout_id"] != holdout or existing["result_hash"] != result_hash:
                    raise ProtocolConflict("evaluation identity was reused inconsistently")
                return dict(existing)
            prior_access = int(
                con.execute(
                    "SELECT COUNT(*) FROM holdout_access WHERE protocol_id=? AND holdout_id=?",
                    (protocol, holdout),
                ).fetchone()[0]
            )
            untouched = 1 if prior_access == 0 else 0
            con.execute(
                "INSERT INTO evaluations(evaluation_id,protocol_id,holdout_id,protocol_hash,result_hash,result_json,prior_access_count,untouched,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (identifier, protocol, holdout, p["protocol_hash"], result_hash, canonical, prior_access, untouched, _now()),
            )
            con.execute(
                "INSERT INTO holdout_access(access_id,protocol_id,holdout_id,purpose,accessed_at) VALUES(?,?,?,?,?)",
                (_id(), protocol, holdout, "LOCKED_EVALUATION", _now()),
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
        try:
            result = json.loads(row["result_json"])
        except json.JSONDecodeError as error:
            raise ProtocolViolation("locked evaluation result is corrupt") from error
        if not isinstance(result, dict) or not result:
            raise ProtocolViolation("locked evaluation result is invalid")
        if _hash(result) != row["result_hash"]:
            raise ProtocolViolation("locked evaluation result hash mismatch")
        return LockedEvaluationEvidence(
            evaluation_id=row["evaluation_id"],
            protocol_id=row["protocol_id"],
            holdout_id=row["holdout_id"],
            protocol_hash=row["protocol_hash"],
            result_hash=row["result_hash"],
            prior_access_count=int(row["prior_access_count"]),
            untouched=bool(row["untouched"]),
            result=result,
            created_at=row["created_at"],
        )

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
            raise ProtocolViolation("candidate approval protocol_id does not match locked evaluation")
        if evidence.protocol_hash != _text(protocol_hash, "protocol_hash"):
            raise ProtocolViolation("candidate approval protocol_hash does not match locked evaluation")
        if evidence.result_hash != _text(result_hash, "result_hash"):
            raise ProtocolViolation("candidate approval result_hash does not match locked evaluation")
        if not evidence.untouched or evidence.prior_access_count != 0:
            raise ProtocolViolation("candidate promotion requires an untouched locked holdout evaluation")
        current_access_count = self.holdout_access_count(
            evidence.protocol_id,
            evidence.holdout_id,
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
                "SELECT status, payload_hash, payload_json FROM trials WHERE protocol_id=?",
                (expected_protocol,),
            ).fetchall()
        candidate_trial_found = False
        for row in trial_rows:
            if row["status"] != "COMPLETED":
                continue
            try:
                payload = json.loads(row["payload_json"])
            except json.JSONDecodeError as error:
                raise ProtocolViolation("registered trial payload is corrupt") from error
            if not isinstance(payload, dict):
                raise ProtocolViolation("registered trial payload is invalid")
            if (
                _canonical(payload) != row["payload_json"]
                or _hash(payload) != row["payload_hash"]
            ):
                raise ProtocolViolation(
                    "registered trial payload integrity mismatch"
                )
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
            "evaluation_status": _text(evaluation_status, "evaluation_status").upper(),
            "retention_passed": retention_passed,
            "risk_passed": risk_passed,
            "authority_scope_id": _text(authority_scope_id, "authority_scope_id"),
            "evidence_valid_until": _text(evidence_valid_until, "evidence_valid_until"),
            "recorded_trial_count": trial_state["recorded_trials"],
            "trial_budget": trial_state["trial_budget"],
            "trial_log_hash": trial_state["trial_log_hash"],
        }
        for field in ("retention_passed", "risk_passed"):
            if not isinstance(required[field], bool):
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
            if result.get("stopping_rule_triggered") is not True:
                raise ProtocolViolation(
                    "candidate promotion before trial-budget exhaustion requires "
                    "an explicitly triggered registered stopping rule"
                )
            if result.get("stopping_rules_hash") != trial_state["stopping_rules_hash"]:
                raise ProtocolViolation(
                    "early-stop evidence is not bound to the registered stopping rules"
                )
            try:
                _immutable_artifact_ref(
                    result.get("stopping_evidence_ref"),
                    "stopping_evidence_ref",
                )
            except ValueError as error:
                raise ProtocolViolation(
                    "early-stop promotion requires immutable stopping evidence"
                ) from error
        return evidence

    def completeness(self, protocol_id: str) -> dict[str, Any]:
        protocol = _id(protocol_id)
        with self._connect() as con:
            p = con.execute(
                "SELECT payload_json FROM protocols WHERE protocol_id=?",
                (protocol,),
            ).fetchone()
            if p is None:
                raise KeyError(protocol)
            protocol_payload = json.loads(p["payload_json"])
            budget = protocol_payload["trial_budget"]
            trial_rows = con.execute(
                """
                SELECT trial_id,status,payload_hash,payload_json
                FROM trials
                WHERE protocol_id=?
                ORDER BY trial_id
                """,
                (protocol,),
            ).fetchall()
        log = []
        for row in trial_rows:
            try:
                payload = json.loads(row["payload_json"])
            except json.JSONDecodeError as error:
                raise ProtocolViolation("registered trial payload is corrupt") from error
            if (
                not isinstance(payload, dict)
                or _canonical(payload) != row["payload_json"]
                or _hash(payload) != row["payload_hash"]
            ):
                raise ProtocolViolation(
                    "registered trial payload integrity mismatch"
                )
            log.append(
                {
                    "trial_id": row["trial_id"],
                    "status": row["status"],
                    "payload_hash": row["payload_hash"],
                }
            )
        counts: dict[str, int] = {}
        for row in trial_rows:
            counts[row["status"]] = counts.get(row["status"], 0) + 1
        total = len(trial_rows)
        return {
            "trial_budget": budget,
            "recorded_trials": total,
            "remaining_trial_budget": budget - total,
            "trial_log_hash": _hash(log),
            "stopping_rules_hash": _hash(protocol_payload["stopping_rules"]),
            "statuses": counts,
            "includes_non_successes": any(
                counts.get(x, 0)
                for x in ("FAILED", "DISCARDED", "CANCELLED")
            ),
        }
