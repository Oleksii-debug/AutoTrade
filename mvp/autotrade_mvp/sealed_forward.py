"""Durable sealed forward predictions scored only against later reconciled outcomes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import sqlite3
from pathlib import Path
from typing import Any, Mapping


def _instant(value: str, *, name: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{name} must be ISO-8601") from error
    if parsed.tzinfo is None:
        raise ValueError(f"{name} must include timezone")
    return parsed.astimezone(timezone.utc)


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _hash(value: Any) -> str:
    return "sha256:" + sha256(_canonical(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SealedPrediction:
    prediction_id: str
    candidate_hash: str
    input_hash: str
    decision_time: str
    deadline: str
    sealed_at: str
    prediction: Mapping[str, Any]
    prediction_hash: str


@dataclass(frozen=True)
class ReconciledOutcome:
    prediction_id: str
    outcome_available_at: str
    reconciled_at: str
    outcome: Mapping[str, Any]
    outcome_hash: str
    evidence_id: str


class SealedForwardStore:
    def __init__(self, path: str | Path = ":memory:") -> None:
        self._db = sqlite3.connect(str(path))
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS predictions(
            prediction_id TEXT PRIMARY KEY,
            candidate_hash TEXT NOT NULL,
            input_hash TEXT NOT NULL,
            decision_time TEXT NOT NULL,
            deadline TEXT NOT NULL,
            sealed_at TEXT NOT NULL,
            prediction_json TEXT NOT NULL,
            prediction_hash TEXT NOT NULL
            )"""
        )
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS outcomes(
            prediction_id TEXT PRIMARY KEY REFERENCES predictions(prediction_id),
            outcome_available_at TEXT NOT NULL,
            reconciled_at TEXT NOT NULL,
            outcome_json TEXT NOT NULL,
            outcome_hash TEXT NOT NULL,
            evidence_id TEXT NOT NULL
            )"""
        )
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    def seal(
        self,
        *,
        prediction_id: str,
        candidate_hash: str,
        input_hash: str,
        decision_time: str,
        deadline: str,
        sealed_at: str,
        prediction: Mapping[str, Any],
    ) -> SealedPrediction:
        for name, value in (
            ("prediction_id", prediction_id),
            ("candidate_hash", candidate_hash),
            ("input_hash", input_hash),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} is required")
        decision = _instant(decision_time, name="decision_time")
        limit = _instant(deadline, name="deadline")
        sealed = _instant(sealed_at, name="sealed_at")
        if limit < decision:
            raise ValueError("deadline cannot precede decision_time")
        if sealed > limit:
            raise ValueError("prediction was not sealed before deadline")
        if sealed < decision:
            raise ValueError("prediction cannot be sealed before decision_time")
        body = _canonical(dict(prediction))
        digest = _hash(dict(prediction))
        row = (
            prediction_id,
            candidate_hash,
            input_hash,
            decision_time,
            deadline,
            sealed_at,
            body,
            digest,
        )
        try:
            self._db.execute("INSERT INTO predictions VALUES (?,?,?,?,?,?,?,?)", row)
            self._db.commit()
        except sqlite3.IntegrityError:
            existing = self._db.execute(
                "SELECT candidate_hash,input_hash,decision_time,deadline,sealed_at,prediction_json,prediction_hash FROM predictions WHERE prediction_id=?",
                (prediction_id,),
            ).fetchone()
            if existing != row[1:]:
                raise ValueError("prediction identity conflict")
        return SealedPrediction(
            prediction_id,
            candidate_hash,
            input_hash,
            decision_time,
            deadline,
            sealed_at,
            dict(prediction),
            digest,
        )

    def reconcile_outcome(
        self,
        *,
        prediction_id: str,
        outcome_available_at: str,
        reconciled_at: str,
        outcome: Mapping[str, Any],
        evidence_id: str,
    ) -> ReconciledOutcome:
        prediction = self._db.execute(
            "SELECT sealed_at FROM predictions WHERE prediction_id=?", (prediction_id,)
        ).fetchone()
        if prediction is None:
            raise KeyError("unknown prediction_id")
        available = _instant(outcome_available_at, name="outcome_available_at")
        reconciled = _instant(reconciled_at, name="reconciled_at")
        sealed = _instant(prediction[0], name="sealed_at")
        if available <= sealed:
            raise ValueError("outcome must become available after prediction is sealed")
        if reconciled < available:
            raise ValueError("outcome cannot be reconciled before it is available")
        if not isinstance(evidence_id, str) or not evidence_id.strip():
            raise ValueError("outcome evidence_id is required")
        body = _canonical(dict(outcome))
        digest = _hash(dict(outcome))
        row = (prediction_id, outcome_available_at, reconciled_at, body, digest, evidence_id)
        try:
            self._db.execute("INSERT INTO outcomes VALUES (?,?,?,?,?,?)", row)
            self._db.commit()
        except sqlite3.IntegrityError:
            existing = self._db.execute(
                "SELECT prediction_id,outcome_available_at,reconciled_at,outcome_json,outcome_hash,evidence_id FROM outcomes WHERE prediction_id=?",
                (prediction_id,),
            ).fetchone()
            if existing != row:
                raise ValueError("outcome identity conflict")
        return ReconciledOutcome(
            prediction_id,
            outcome_available_at,
            reconciled_at,
            dict(outcome),
            digest,
            evidence_id,
        )

    def paired_record(self, prediction_id: str) -> tuple[SealedPrediction, ReconciledOutcome]:
        row = self._db.execute(
            """SELECT p.prediction_id,p.candidate_hash,p.input_hash,p.decision_time,p.deadline,p.sealed_at,p.prediction_json,p.prediction_hash,
                      o.outcome_available_at,o.reconciled_at,o.outcome_json,o.outcome_hash,o.evidence_id
               FROM predictions p JOIN outcomes o ON p.prediction_id=o.prediction_id
               WHERE p.prediction_id=?""",
            (prediction_id,),
        ).fetchone()
        if row is None:
            raise ValueError("prediction has no reconciled outcome")
        prediction = SealedPrediction(
            row[0], row[1], row[2], row[3], row[4], row[5], json.loads(row[6]), row[7]
        )
        outcome = ReconciledOutcome(
            row[0], row[8], row[9], json.loads(row[10]), row[11], row[12]
        )
        return prediction, outcome

    def audit(self) -> tuple[str, ...]:
        problems: list[str] = []
        rows = self._db.execute(
            "SELECT prediction_id,prediction_json,prediction_hash FROM predictions"
        ).fetchall()
        for prediction_id, body, digest in rows:
            if _hash(json.loads(body)) != digest:
                problems.append(f"prediction_hash_mismatch:{prediction_id}")
        rows = self._db.execute(
            "SELECT prediction_id,outcome_json,outcome_hash FROM outcomes"
        ).fetchall()
        for prediction_id, body, digest in rows:
            if _hash(json.loads(body)) != digest:
                problems.append(f"outcome_hash_mismatch:{prediction_id}")
        return tuple(sorted(problems))
