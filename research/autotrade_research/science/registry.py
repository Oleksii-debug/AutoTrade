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
                if existing["protocol_id"] != protocol or existing["status"] != normalized or existing["payload_hash"] != digest:
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
                "SELECT status, payload_json FROM trials WHERE protocol_id=?",
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
        return evidence

    def completeness(self, protocol_id: str) -> dict[str, Any]:
        protocol = _id(protocol_id)
        with self._connect() as con:
            p = con.execute("SELECT payload_json FROM protocols WHERE protocol_id=?", (protocol,)).fetchone()
            if p is None:
                raise KeyError(protocol)
            budget = json.loads(p["payload_json"])["trial_budget"]
            rows = con.execute(
                "SELECT status, COUNT(*) AS n FROM trials WHERE protocol_id=? GROUP BY status",
                (protocol,),
            ).fetchall()
        counts = {row["status"]: int(row["n"]) for row in rows}
        total = sum(counts.values())
        return {
            "trial_budget": budget,
            "recorded_trials": total,
            "remaining_trial_budget": budget - total,
            "statuses": counts,
            "includes_non_successes": any(counts.get(x, 0) for x in ("FAILED", "DISCARDED", "CANCELLED")),
        }
