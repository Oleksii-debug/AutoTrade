"""Atomic future-routing champion registry with evidence-bound promotion."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import sqlite3


def _time(value: datetime, *, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _text(value: str, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    return value.strip()


@dataclass(frozen=True)
class CandidateApproval:
    candidate_id: str
    artifact_hash: str
    evidence_id: str
    evidence_valid_until: datetime
    evaluation_status: str
    retention_passed: bool
    risk_passed: bool
    authority_scope_id: str

    @classmethod
    def create(cls, *, candidate_id: str, artifact_hash: str, evidence_id: str,
               evidence_valid_until: datetime, evaluation_status: str,
               retention_passed: bool, risk_passed: bool, authority_scope_id: str) -> "CandidateApproval":
        status = _text(evaluation_status, name="evaluation_status").upper()
        if status not in {"PASS", "FAIL", "INCONCLUSIVE"}:
            raise ValueError("invalid evaluation_status")
        if not isinstance(retention_passed, bool) or not isinstance(risk_passed, bool):
            raise TypeError("retention_passed and risk_passed must be boolean")
        return cls(
            candidate_id=_text(candidate_id, name="candidate_id"),
            artifact_hash=_text(artifact_hash, name="artifact_hash"),
            evidence_id=_text(evidence_id, name="evidence_id"),
            evidence_valid_until=_time(evidence_valid_until, name="evidence_valid_until"),
            evaluation_status=status,
            retention_passed=retention_passed,
            risk_passed=risk_passed,
            authority_scope_id=_text(authority_scope_id, name="authority_scope_id"),
        )


@dataclass(frozen=True)
class RoutingState:
    generation: int
    champion_candidate_id: str | None
    champion_artifact_hash: str | None
    authority_scope_id: str | None
    existing_position_policy: str | None


class PromotionConflict(RuntimeError):
    pass


class ChampionRegistry:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as con:
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS routing_state(
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    generation INTEGER NOT NULL,
                    champion_candidate_id TEXT,
                    champion_artifact_hash TEXT,
                    authority_scope_id TEXT,
                    existing_position_policy TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS promotion_history(
                    generation INTEGER PRIMARY KEY,
                    action TEXT NOT NULL,
                    candidate_id TEXT NOT NULL,
                    artifact_hash TEXT NOT NULL,
                    evidence_id TEXT NOT NULL,
                    authority_scope_id TEXT NOT NULL,
                    existing_position_policy TEXT,
                    created_at TEXT NOT NULL
                );
                INSERT OR IGNORE INTO routing_state(
                    singleton,generation,champion_candidate_id,champion_artifact_hash,
                    authority_scope_id,existing_position_policy,updated_at
                ) VALUES(1,0,NULL,NULL,NULL,NULL,'1970-01-01T00:00:00+00:00');
                """
            )

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, timeout=30)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        return con

    def state(self) -> RoutingState:
        with self._connect() as con:
            row = con.execute("SELECT * FROM routing_state WHERE singleton=1").fetchone()
        return RoutingState(
            generation=int(row["generation"]),
            champion_candidate_id=row["champion_candidate_id"],
            champion_artifact_hash=row["champion_artifact_hash"],
            authority_scope_id=row["authority_scope_id"],
            existing_position_policy=row["existing_position_policy"],
        )

    @staticmethod
    def _validate_approval(approval: CandidateApproval, now: datetime) -> None:
        current = _time(now, name="now")
        if approval.evaluation_status != "PASS":
            raise ValueError("candidate evaluation has not passed")
        if not approval.retention_passed:
            raise ValueError("candidate retention gate has not passed")
        if not approval.risk_passed:
            raise ValueError("candidate risk gate has not passed")
        if current > approval.evidence_valid_until:
            raise ValueError("candidate evidence has expired")

    def promote(self, approval: CandidateApproval, *, expected_generation: int, now: datetime,
                open_position_count: int, existing_position_policy: str | None) -> RoutingState:
        self._validate_approval(approval, now)
        if not isinstance(expected_generation, int) or isinstance(expected_generation, bool) or expected_generation < 0:
            raise ValueError("expected_generation must be non-negative")
        if not isinstance(open_position_count, int) or isinstance(open_position_count, bool) or open_position_count < 0:
            raise ValueError("open_position_count must be non-negative")
        policy = existing_position_policy.strip() if isinstance(existing_position_policy, str) else None
        if open_position_count > 0 and not policy:
            raise ValueError("open positions require an explicit compatible management/exit policy")
        current_time = _time(now, name="now").isoformat()
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT * FROM routing_state WHERE singleton=1").fetchone()
            if int(row["generation"]) != expected_generation:
                raise PromotionConflict("routing generation changed before promotion")
            generation = expected_generation + 1
            con.execute(
                """UPDATE routing_state SET generation=?,champion_candidate_id=?,champion_artifact_hash=?,
                    authority_scope_id=?,existing_position_policy=?,updated_at=? WHERE singleton=1""",
                (generation, approval.candidate_id, approval.artifact_hash,
                 approval.authority_scope_id, policy, current_time),
            )
            con.execute(
                """INSERT INTO promotion_history(
                    generation,action,candidate_id,artifact_hash,evidence_id,authority_scope_id,
                    existing_position_policy,created_at
                ) VALUES(?,?,?,?,?,?,?,?)""",
                (generation,"PROMOTE",approval.candidate_id,approval.artifact_hash,
                 approval.evidence_id,approval.authority_scope_id,policy,current_time),
            )
            con.commit()
        return self.state()

    def rollback(self, *, target_generation: int, expected_generation: int, now: datetime,
                 open_position_count: int, existing_position_policy: str | None) -> RoutingState:
        if target_generation < 1:
            raise ValueError("target_generation must reference a prior promoted generation")
        if open_position_count < 0:
            raise ValueError("open_position_count must be non-negative")
        policy = existing_position_policy.strip() if isinstance(existing_position_policy, str) else None
        if open_position_count > 0 and not policy:
            raise ValueError("rollback with open positions requires an explicit management/exit policy")
        current_time = _time(now, name="now").isoformat()
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            current = con.execute("SELECT * FROM routing_state WHERE singleton=1").fetchone()
            if int(current["generation"]) != expected_generation:
                raise PromotionConflict("routing generation changed before rollback")
            target = con.execute(
                "SELECT * FROM promotion_history WHERE generation=? AND action='PROMOTE'",
                (target_generation,),
            ).fetchone()
            if target is None:
                raise KeyError(target_generation)
            generation = expected_generation + 1
            con.execute(
                """UPDATE routing_state SET generation=?,champion_candidate_id=?,champion_artifact_hash=?,
                    authority_scope_id=?,existing_position_policy=?,updated_at=? WHERE singleton=1""",
                (generation,target["candidate_id"],target["artifact_hash"],
                 target["authority_scope_id"],policy,current_time),
            )
            con.execute(
                """INSERT INTO promotion_history(
                    generation,action,candidate_id,artifact_hash,evidence_id,authority_scope_id,
                    existing_position_policy,created_at
                ) VALUES(?,?,?,?,?,?,?,?)""",
                (generation,"ROLLBACK",target["candidate_id"],target["artifact_hash"],
                 target["evidence_id"],target["authority_scope_id"],policy,current_time),
            )
            con.commit()
        return self.state()

    def history(self) -> tuple[dict, ...]:
        with self._connect() as con:
            rows = con.execute("SELECT * FROM promotion_history ORDER BY generation").fetchall()
        return tuple(dict(row) for row in rows)
