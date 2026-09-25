"""Atomic future-routing champion registry with evidence-bound promotion."""
from __future__ import annotations

from contextlib import contextmanager

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from hashlib import sha256
from typing import Mapping
from uuid import UUID
import json
import re
import sqlite3

from autotrade_research.artifacts.store import ArtifactIntegrityError, ArtifactStore
from autotrade_research.io.strict_json import strict_json_loads
from autotrade_research.science.registry import ScientificRegistry


def _time(value: datetime, *, name: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _text(value: str, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    return value.strip()


_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")


def _digest(value: str, *, name: str) -> str:
    normalized = _text(value, name=name).lower()
    if _SHA256.fullmatch(normalized) is None:
        raise ValueError(f"{name} must be a canonical sha256 digest")
    return normalized


def _request_fingerprint(value: dict) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return "sha256:" + sha256(payload.encode("utf-8")).hexdigest()


def _decimal(value, *, name: str, non_negative: bool = False) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError(f"{name} must use exact decimal input")
    try:
        result = value if isinstance(value, Decimal) else Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError(f"{name} must be a finite decimal") from error
    if not result.is_finite():
        raise ValueError(f"{name} must be a finite decimal")
    if non_negative and result < 0:
        raise ValueError(f"{name} cannot be negative")
    return result



@dataclass(frozen=True)
class OnlineEvidenceRef:
    artifact_id: str
    sha256: str

    def __post_init__(self) -> None:
        try:
            artifact_id = str(UUID(self.artifact_id))
        except (ValueError, AttributeError, TypeError) as error:
            raise ValueError("online evidence artifact_id must be a UUID") from error
        digest = _digest(self.sha256, name="online evidence sha256")
        object.__setattr__(self, "artifact_id", artifact_id)
        object.__setattr__(self, "sha256", digest)

    @property
    def token(self) -> str:
        return f"{self.artifact_id}@{self.sha256}"


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
    protocol_id: str
    protocol_hash: str
    evaluation_id: str
    evaluation_result_hash: str

    def __post_init__(self) -> None:
        status = _text(self.evaluation_status, name="evaluation_status").upper()
        if status not in {"PASS", "FAIL", "INCONCLUSIVE"}:
            raise ValueError("invalid evaluation_status")
        if type(self.retention_passed) is not bool or type(self.risk_passed) is not bool:
            raise TypeError("retention_passed and risk_passed must be boolean")
        object.__setattr__(self, "candidate_id", _text(self.candidate_id, name="candidate_id"))
        object.__setattr__(self, "artifact_hash", _digest(self.artifact_hash, name="artifact_hash"))
        object.__setattr__(self, "evidence_id", _text(self.evidence_id, name="evidence_id"))
        object.__setattr__(
            self,
            "evidence_valid_until",
            _time(self.evidence_valid_until, name="evidence_valid_until"),
        )
        object.__setattr__(self, "evaluation_status", status)
        object.__setattr__(
            self,
            "authority_scope_id",
            _text(self.authority_scope_id, name="authority_scope_id"),
        )
        object.__setattr__(self, "protocol_id", _text(self.protocol_id, name="protocol_id"))
        object.__setattr__(self, "protocol_hash", _digest(self.protocol_hash, name="protocol_hash"))
        object.__setattr__(
            self,
            "evaluation_id",
            _text(self.evaluation_id, name="evaluation_id"),
        )
        object.__setattr__(
            self,
            "evaluation_result_hash",
            _digest(self.evaluation_result_hash, name="evaluation_result_hash"),
        )

    @classmethod
    def create(cls, *, candidate_id: str, artifact_hash: str, evidence_id: str,
               evidence_valid_until: datetime, evaluation_status: str,
               retention_passed: bool, risk_passed: bool, authority_scope_id: str,
               protocol_id: str, protocol_hash: str, evaluation_id: str,
               evaluation_result_hash: str) -> "CandidateApproval":
        status = _text(evaluation_status, name="evaluation_status").upper()
        if status not in {"PASS", "FAIL", "INCONCLUSIVE"}:
            raise ValueError("invalid evaluation_status")
        if not isinstance(retention_passed, bool) or not isinstance(risk_passed, bool):
            raise TypeError("retention_passed and risk_passed must be boolean")
        return cls(
            candidate_id=_text(candidate_id, name="candidate_id"),
            artifact_hash=_digest(artifact_hash, name="artifact_hash"),
            evidence_id=_text(evidence_id, name="evidence_id"),
            evidence_valid_until=_time(evidence_valid_until, name="evidence_valid_until"),
            evaluation_status=status,
            retention_passed=retention_passed,
            risk_passed=risk_passed,
            authority_scope_id=_text(authority_scope_id, name="authority_scope_id"),
            protocol_id=_text(protocol_id, name="protocol_id"),
            protocol_hash=_digest(protocol_hash, name="protocol_hash"),
            evaluation_id=_text(evaluation_id, name="evaluation_id"),
            evaluation_result_hash=_digest(
                evaluation_result_hash, name="evaluation_result_hash"
            ),
        )


@dataclass(frozen=True)
class ParameterBound:
    name: str
    minimum: Decimal
    maximum: Decimal

    def __post_init__(self) -> None:
        name = _text(self.name, name="parameter name")
        lower = _decimal(self.minimum, name="minimum")
        upper = _decimal(self.maximum, name="maximum")
        if lower > upper:
            raise ValueError("parameter minimum cannot exceed maximum")
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "minimum", lower)
        object.__setattr__(self, "maximum", upper)

    @classmethod
    def create(cls, *, name: str, minimum, maximum) -> "ParameterBound":
        lower = _decimal(minimum, name="minimum")
        upper = _decimal(maximum, name="maximum")
        if lower > upper:
            raise ValueError("parameter minimum cannot exceed maximum")
        return cls(
            name=_text(name, name="parameter name"),
            minimum=lower,
            maximum=upper,
        )


@dataclass(frozen=True)
class OnlineEnvelope:
    envelope_id: str
    champion_artifact_hash: str
    authority_scope_id: str
    parameter_bounds: tuple[ParameterBound, ...]
    minimum_update_interval_seconds: int
    maximum_update_cost: Decimal
    eligible_label_refs: tuple[str, ...]
    envelope_hash: str

    def __post_init__(self) -> None:
        envelope_id = _text(self.envelope_id, name="envelope_id")
        artifact_hash = _digest(
            self.champion_artifact_hash,
            name="champion_artifact_hash",
        )
        authority_scope = _text(
            self.authority_scope_id,
            name="authority_scope_id",
        )
        if (
            type(self.minimum_update_interval_seconds) is not int
            or self.minimum_update_interval_seconds < 0
        ):
            raise ValueError(
                "minimum_update_interval_seconds must be a non-negative integer"
            )
        if isinstance(self.parameter_bounds, (str, bytes)):
            raise TypeError("parameter_bounds must be a collection")
        raw_bounds = tuple(self.parameter_bounds)
        if not raw_bounds:
            raise ValueError("online envelope requires parameter bounds")
        if any(not isinstance(bound, ParameterBound) for bound in raw_bounds):
            raise TypeError("parameter_bounds must contain ParameterBound")
        bounds = tuple(
            sorted(
                (
                    ParameterBound.create(
                        name=bound.name,
                        minimum=bound.minimum,
                        maximum=bound.maximum,
                    )
                    for bound in raw_bounds
                ),
                key=lambda item: item.name,
            )
        )
        names = tuple(bound.name for bound in bounds)
        if len(set(names)) != len(names):
            raise ValueError("online envelope parameter names must be unique")
        if isinstance(self.eligible_label_refs, (str, bytes)):
            raise TypeError("eligible_label_refs must be a collection")
        labels = tuple(
            sorted(
                _text(item, name="eligible label reference")
                for item in self.eligible_label_refs
            )
        )
        if not labels:
            raise ValueError("online envelope requires eligible labels")
        if len(set(labels)) != len(labels):
            raise ValueError("eligible label references must be unique")
        cost = _decimal(
            self.maximum_update_cost,
            name="maximum_update_cost",
            non_negative=True,
        )
        body = {
            "envelope_id": envelope_id,
            "champion_artifact_hash": artifact_hash,
            "authority_scope_id": authority_scope,
            "parameter_bounds": [
                {
                    "name": bound.name,
                    "minimum": str(bound.minimum),
                    "maximum": str(bound.maximum),
                }
                for bound in bounds
            ],
            "minimum_update_interval_seconds": self.minimum_update_interval_seconds,
            "maximum_update_cost": str(cost),
            "eligible_label_refs": list(labels),
        }
        expected_hash = _request_fingerprint(body)
        provided_hash = _digest(self.envelope_hash, name="envelope_hash")
        if provided_hash != expected_hash:
            raise ValueError("envelope_hash does not match canonical envelope content")
        object.__setattr__(self, "envelope_id", envelope_id)
        object.__setattr__(self, "champion_artifact_hash", artifact_hash)
        object.__setattr__(self, "authority_scope_id", authority_scope)
        object.__setattr__(self, "parameter_bounds", bounds)
        object.__setattr__(self, "maximum_update_cost", cost)
        object.__setattr__(self, "eligible_label_refs", labels)
        object.__setattr__(self, "envelope_hash", provided_hash)

    @classmethod
    def create(
        cls,
        *,
        envelope_id: str,
        champion_artifact_hash: str,
        authority_scope_id: str,
        parameter_bounds,
        minimum_update_interval_seconds: int,
        maximum_update_cost,
        eligible_label_refs,
    ) -> "OnlineEnvelope":
        if (
            not isinstance(minimum_update_interval_seconds, int)
            or isinstance(minimum_update_interval_seconds, bool)
            or minimum_update_interval_seconds < 0
        ):
            raise ValueError(
                "minimum_update_interval_seconds must be a non-negative integer"
            )
        raw_bounds = tuple(parameter_bounds)
        if not raw_bounds:
            raise ValueError("online envelope requires parameter bounds")
        if any(not isinstance(bound, ParameterBound) for bound in raw_bounds):
            raise TypeError("parameter_bounds must contain ParameterBound")
        bounds = tuple(
            ParameterBound.create(
                name=bound.name,
                minimum=bound.minimum,
                maximum=bound.maximum,
            )
            for bound in raw_bounds
        )
        names = tuple(bound.name for bound in bounds)
        if len(set(names)) != len(names):
            raise ValueError("online envelope parameter names must be unique")
        labels = tuple(
            _text(item, name="eligible label reference")
            for item in eligible_label_refs
        )
        if not labels:
            raise ValueError("online envelope requires eligible labels")
        if len(set(labels)) != len(labels):
            raise ValueError("eligible label references must be unique")
        cost = _decimal(
            maximum_update_cost,
            name="maximum_update_cost",
            non_negative=True,
        )
        body = {
            "envelope_id": _text(envelope_id, name="envelope_id"),
            "champion_artifact_hash": _digest(
                champion_artifact_hash,
                name="champion_artifact_hash",
            ),
            "authority_scope_id": _text(
                authority_scope_id,
                name="authority_scope_id",
            ),
            "parameter_bounds": [
                {
                    "name": bound.name,
                    "minimum": str(bound.minimum),
                    "maximum": str(bound.maximum),
                }
                for bound in sorted(bounds, key=lambda item: item.name)
            ],
            "minimum_update_interval_seconds": minimum_update_interval_seconds,
            "maximum_update_cost": str(cost),
            "eligible_label_refs": sorted(labels),
        }
        return cls(
            envelope_id=body["envelope_id"],
            champion_artifact_hash=body["champion_artifact_hash"],
            authority_scope_id=body["authority_scope_id"],
            parameter_bounds=tuple(sorted(bounds, key=lambda item: item.name)),
            minimum_update_interval_seconds=minimum_update_interval_seconds,
            maximum_update_cost=cost,
            eligible_label_refs=tuple(sorted(labels)),
            envelope_hash=_request_fingerprint(body),
        )

    def normalize_updates(self, updates: Mapping[str, object]) -> dict[str, str]:
        if not isinstance(updates, Mapping) or not updates:
            raise ValueError("online update requires parameter values")
        by_name = {bound.name: bound for bound in self.parameter_bounds}
        normalized: dict[str, str] = {}
        for raw_name, raw_value in updates.items():
            name = _text(raw_name, name="update parameter name")
            bound = by_name.get(name)
            if bound is None:
                raise ValueError(
                    f"parameter {name} is outside the approved online envelope"
                )
            value = _decimal(raw_value, name=f"parameter {name}")
            if value < bound.minimum or value > bound.maximum:
                raise ValueError(
                    f"parameter {name} is outside the approved online range"
                )
            normalized[name] = str(value)
        return dict(sorted(normalized.items()))


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
    def __init__(
        self,
        path: str | Path,
        *,
        scientific_registry: ScientificRegistry,
        artifact_store: ArtifactStore | None = None,
    ):
        if not isinstance(scientific_registry, ScientificRegistry):
            raise TypeError("scientific_registry must be ScientificRegistry")
        if artifact_store is not None and not isinstance(artifact_store, ArtifactStore):
            raise TypeError("artifact_store must be ArtifactStore or None")
        self.path = Path(path)
        self.scientific_registry = scientific_registry
        self.artifact_store = artifact_store
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
                CREATE TABLE IF NOT EXISTS online_updates(
                    update_id TEXT PRIMARY KEY,
                    routing_generation INTEGER NOT NULL,
                    champion_artifact_hash TEXT NOT NULL,
                    authority_scope_id TEXT NOT NULL,
                    envelope_id TEXT NOT NULL,
                    envelope_hash TEXT NOT NULL,
                    updates_json TEXT NOT NULL,
                    label_refs_json TEXT NOT NULL,
                    evidence_refs_json TEXT NOT NULL,
                    actual_update_cost TEXT NOT NULL,
                    request_fingerprint TEXT NOT NULL,
                    applied_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_online_updates_envelope
                    ON online_updates(envelope_id, applied_at, update_id);

                CREATE TABLE IF NOT EXISTS promotion_history(
                    generation INTEGER PRIMARY KEY,
                    action TEXT NOT NULL,
                    candidate_id TEXT NOT NULL,
                    artifact_hash TEXT NOT NULL,
                    evidence_id TEXT NOT NULL,
                    authority_scope_id TEXT NOT NULL,
                    existing_position_policy TEXT,
                    request_fingerprint TEXT,
                    created_at TEXT NOT NULL
                );
                INSERT OR IGNORE INTO routing_state(
                    singleton,generation,champion_candidate_id,champion_artifact_hash,
                    authority_scope_id,existing_position_policy,updated_at
                ) VALUES(1,0,NULL,NULL,NULL,NULL,'1970-01-01T00:00:00+00:00');
                """
            )
            columns = {
                row[1]
                for row in con.execute("PRAGMA table_info(promotion_history)").fetchall()
            }
            if "request_fingerprint" not in columns:
                con.execute(
                    "ALTER TABLE promotion_history ADD COLUMN request_fingerprint TEXT"
                )

    @contextmanager
    def _connect(self):
        con = sqlite3.connect(self.path, timeout=30)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        try:
            yield con
            con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

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

    def _validate_approval(self, approval: CandidateApproval, now: datetime) -> None:
        current = _time(now, name="now")
        if approval.evaluation_status != "PASS":
            raise ValueError("candidate evaluation has not passed")
        if not approval.retention_passed:
            raise ValueError("candidate retention gate has not passed")
        if not approval.risk_passed:
            raise ValueError("candidate risk gate has not passed")
        if current >= approval.evidence_valid_until:
            raise ValueError("candidate evidence has expired")
        self.scientific_registry.verify_candidate_promotion_evidence(
            evaluation_id=approval.evaluation_id,
            protocol_id=approval.protocol_id,
            protocol_hash=approval.protocol_hash,
            result_hash=approval.evaluation_result_hash,
            candidate_id=approval.candidate_id,
            artifact_hash=approval.artifact_hash,
            evaluation_status=approval.evaluation_status,
            retention_passed=approval.retention_passed,
            risk_passed=approval.risk_passed,
            authority_scope_id=approval.authority_scope_id,
            evidence_valid_until=approval.evidence_valid_until.isoformat(),
        )

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
        request_fingerprint = _request_fingerprint(
            {
                "action": "PROMOTE",
                "expected_generation": expected_generation,
                "candidate_id": approval.candidate_id,
                "artifact_hash": approval.artifact_hash,
                "evidence_id": approval.evidence_id,
                "evidence_valid_until": approval.evidence_valid_until.isoformat(),
                "evaluation_status": approval.evaluation_status,
                "retention_passed": approval.retention_passed,
                "risk_passed": approval.risk_passed,
                "authority_scope_id": approval.authority_scope_id,
                "protocol_id": approval.protocol_id,
                "protocol_hash": approval.protocol_hash,
                "evaluation_id": approval.evaluation_id,
                "evaluation_result_hash": approval.evaluation_result_hash,
                "open_position_count": open_position_count,
                "existing_position_policy": policy,
            }
        )
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT * FROM routing_state WHERE singleton=1").fetchone()
            current_generation = int(row["generation"])
            if current_generation != expected_generation:
                if current_generation == expected_generation + 1:
                    latest = con.execute(
                        "SELECT * FROM promotion_history WHERE generation=?",
                        (current_generation,),
                    ).fetchone()
                    if (
                        latest is not None
                        and latest["action"] == "PROMOTE"
                        and latest["request_fingerprint"] == request_fingerprint
                    ):
                        return RoutingState(
                            generation=current_generation,
                            champion_candidate_id=row["champion_candidate_id"],
                            champion_artifact_hash=row["champion_artifact_hash"],
                            authority_scope_id=row["authority_scope_id"],
                            existing_position_policy=row["existing_position_policy"],
                        )
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
                    existing_position_policy,request_fingerprint,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (generation,"PROMOTE",approval.candidate_id,approval.artifact_hash,
                 approval.evidence_id,approval.authority_scope_id,policy,
                 request_fingerprint,current_time),
            )
            con.commit()
        return self.state()

    def rollback(self, *, target_generation: int, expected_generation: int, now: datetime,
                 open_position_count: int, existing_position_policy: str | None) -> RoutingState:
        if (
            not isinstance(target_generation, int)
            or isinstance(target_generation, bool)
            or target_generation < 1
        ):
            raise ValueError("target_generation must reference a prior promoted generation")
        if (
            not isinstance(expected_generation, int)
            or isinstance(expected_generation, bool)
            or expected_generation < 0
        ):
            raise ValueError("expected_generation must be non-negative")
        if (
            not isinstance(open_position_count, int)
            or isinstance(open_position_count, bool)
            or open_position_count < 0
        ):
            raise ValueError("open_position_count must be non-negative")
        policy = existing_position_policy.strip() if isinstance(existing_position_policy, str) else None
        if open_position_count > 0 and not policy:
            raise ValueError("rollback with open positions requires an explicit management/exit policy")
        current_time = _time(now, name="now").isoformat()
        request_fingerprint = _request_fingerprint(
            {
                "action": "ROLLBACK",
                "target_generation": target_generation,
                "expected_generation": expected_generation,
                "open_position_count": open_position_count,
                "existing_position_policy": policy,
            }
        )
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            current = con.execute("SELECT * FROM routing_state WHERE singleton=1").fetchone()
            current_generation = int(current["generation"])
            if current_generation != expected_generation:
                if current_generation == expected_generation + 1:
                    latest = con.execute(
                        "SELECT * FROM promotion_history WHERE generation=?",
                        (current_generation,),
                    ).fetchone()
                    if (
                        latest is not None
                        and latest["action"] == "ROLLBACK"
                        and latest["request_fingerprint"] == request_fingerprint
                    ):
                        return RoutingState(
                            generation=current_generation,
                            champion_candidate_id=current["champion_candidate_id"],
                            champion_artifact_hash=current["champion_artifact_hash"],
                            authority_scope_id=current["authority_scope_id"],
                            existing_position_policy=current["existing_position_policy"],
                        )
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
                    existing_position_policy,request_fingerprint,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (generation,"ROLLBACK",target["candidate_id"],target["artifact_hash"],
                 target["evidence_id"],target["authority_scope_id"],policy,
                 request_fingerprint,current_time),
            )
            con.commit()
        return self.state()



    def _verified_artifact(self, ref: OnlineEvidenceRef) -> tuple[dict, bytes]:
        if not isinstance(ref, OnlineEvidenceRef):
            raise TypeError("online evidence reference must be OnlineEvidenceRef")
        if self.artifact_store is None:
            raise ValueError("online update requires an immutable artifact verifier")
        try:
            manifest = self.artifact_store.load_manifest(ref.artifact_id)
            data = self.artifact_store.read_bytes(ref.artifact_id)
        except (FileNotFoundError, ArtifactIntegrityError, OSError, ValueError) as error:
            raise ValueError("online evidence artifact cannot be verified") from error
        if manifest.get("manifest_hash") is None:
            raise ValueError("online evidence manifest lacks integrity binding")
        if manifest.get("sha256") != ref.sha256:
            raise ValueError("online evidence digest does not match immutable artifact")
        return manifest, data

    def _verified_online_decision(
        self,
        ref: OnlineEvidenceRef,
        *,
        kind: str,
        envelope: OnlineEnvelope,
        expected_generation: int,
        applied: datetime,
        label_ref: str | None = None,
    ) -> dict:
        _manifest, data = self._verified_artifact(ref)
        try:
            raw = data.decode("utf-8")
            payload = strict_json_loads(raw)
        except (UnicodeError, ValueError) as error:
            raise ValueError("online decision evidence must be strict UTF-8 JSON") from error
        if type(payload) is not dict:
            raise ValueError("online decision evidence must be an object")
        canonical = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        if canonical != raw:
            raise ValueError("online decision evidence must use canonical JSON bytes")

        common = {
            "kind",
            "champion_artifact_hash",
            "routing_generation",
            "authority_scope_id",
            "envelope_hash",
            "available_at",
        }
        if kind == "DRIFT_GATE":
            expected_keys = common | {"status"}
        elif kind == "STOP_CONDITION":
            expected_keys = common | {"triggered"}
        elif kind == "ONLINE_LABEL":
            expected_keys = common | {"label_ref", "outcome_state"}
        elif kind == "ONLINE_SUPPORT":
            expected_keys = common | {"purpose"}
        else:
            raise ValueError("unsupported online evidence kind")
        if set(payload) != expected_keys or payload.get("kind") != kind:
            raise ValueError(f"{kind} evidence shape is invalid")

        if payload.get("champion_artifact_hash") != envelope.champion_artifact_hash:
            raise ValueError("online evidence is bound to a different champion artifact")
        if payload.get("routing_generation") != expected_generation:
            raise ValueError("online evidence is bound to a different routing generation")
        if payload.get("authority_scope_id") != envelope.authority_scope_id:
            raise ValueError("online evidence is bound to a different authority scope")
        if payload.get("envelope_hash") != envelope.envelope_hash:
            raise ValueError("online evidence is bound to a different online envelope")
        available_raw = payload.get("available_at")
        if not isinstance(available_raw, str):
            raise ValueError("online evidence available_at is required")
        try:
            available = _time(
                datetime.fromisoformat(available_raw.replace("Z", "+00:00")),
                name="online evidence available_at",
            )
        except ValueError as error:
            raise ValueError("online evidence available_at must be timezone-aware ISO-8601") from error
        if available > applied:
            raise ValueError("future online evidence cannot authorize this update")

        if kind == "DRIFT_GATE" and payload.get("status") != "PASS":
            raise ValueError("online update is blocked by the registered drift gate")
        if kind == "STOP_CONDITION" and payload.get("triggered") is not False:
            raise ValueError("online update is blocked by a registered stop condition")
        if kind == "ONLINE_LABEL":
            if payload.get("label_ref") != label_ref:
                raise ValueError("online label evidence identity mismatch")
            if payload.get("outcome_state") != "RECONCILED":
                raise ValueError("online update label outcome is not reconciled/eligible")
        if kind == "ONLINE_SUPPORT":
            _text(payload.get("purpose"), name="online support evidence purpose")
        return payload

    def record_online_update(
        self,
        *,
        envelope: OnlineEnvelope,
        update_id: str,
        expected_generation: int,
        updates: Mapping[str, object],
        label_refs,
        evidence_refs,
        actual_update_cost,
        now: datetime,
        drift_gate_evidence: OnlineEvidenceRef,
        stop_condition_evidence: OnlineEvidenceRef,
        label_evidence_refs: Mapping[str, OnlineEvidenceRef],
    ) -> dict:
        if not isinstance(envelope, OnlineEnvelope):
            raise TypeError("envelope must be OnlineEnvelope")
        envelope = OnlineEnvelope.create(
            envelope_id=envelope.envelope_id,
            champion_artifact_hash=envelope.champion_artifact_hash,
            authority_scope_id=envelope.authority_scope_id,
            parameter_bounds=envelope.parameter_bounds,
            minimum_update_interval_seconds=envelope.minimum_update_interval_seconds,
            maximum_update_cost=envelope.maximum_update_cost,
            eligible_label_refs=envelope.eligible_label_refs,
        )
        update_id = _text(update_id, name="update_id")
        if (
            not isinstance(expected_generation, int)
            or isinstance(expected_generation, bool)
            or expected_generation < 1
        ):
            raise ValueError("expected_generation must be a positive integer")

        normalized_updates = envelope.normalize_updates(updates)
        labels = tuple(_text(item, name="label reference") for item in label_refs)
        if not labels:
            raise ValueError("online update requires label evidence")
        if len(set(labels)) != len(labels):
            raise ValueError("online update label references must be unique")
        if not set(labels).issubset(set(envelope.eligible_label_refs)):
            raise ValueError("online update uses labels outside the approved envelope")

        evidence = tuple(evidence_refs)
        if not evidence:
            raise ValueError("online update requires immutable evidence references")
        if any(not isinstance(item, OnlineEvidenceRef) for item in evidence):
            raise TypeError(
                "online update evidence references must be OnlineEvidenceRef"
            )
        evidence_tokens = tuple(item.token for item in evidence)
        if len(set(evidence_tokens)) != len(evidence_tokens):
            raise ValueError("online update evidence references must be unique")

        if not isinstance(label_evidence_refs, Mapping):
            raise TypeError("label_evidence_refs must be a mapping")
        normalized_label_evidence = dict(label_evidence_refs)
        if set(normalized_label_evidence) != set(labels):
            raise ValueError(
                "every online label must have exactly one immutable evidence artifact"
            )

        cost = _decimal(
            actual_update_cost,
            name="actual_update_cost",
            non_negative=True,
        )
        if cost > envelope.maximum_update_cost:
            raise ValueError("online update exceeds the approved resource budget")
        applied = _time(now, name="now")

        # The decisive continual-learning gates are derived from immutable,
        # causally available evidence bound to this exact routing state. Naked
        # caller booleans are deliberately not part of this API.
        self._verified_online_decision(
            drift_gate_evidence,
            kind="DRIFT_GATE",
            envelope=envelope,
            expected_generation=expected_generation,
            applied=applied,
        )
        self._verified_online_decision(
            stop_condition_evidence,
            kind="STOP_CONDITION",
            envelope=envelope,
            expected_generation=expected_generation,
            applied=applied,
        )
        label_tokens: dict[str, str] = {}
        for label in labels:
            ref = normalized_label_evidence[label]
            if not isinstance(ref, OnlineEvidenceRef):
                raise TypeError("label evidence values must be OnlineEvidenceRef")
            self._verified_online_decision(
                ref,
                kind="ONLINE_LABEL",
                envelope=envelope,
                expected_generation=expected_generation,
                applied=applied,
                label_ref=label,
            )
            label_tokens[label] = ref.token

        for ref in evidence:
            self._verified_online_decision(
                ref,
                kind="ONLINE_SUPPORT",
                envelope=envelope,
                expected_generation=expected_generation,
                applied=applied,
            )

        request_fingerprint = _request_fingerprint(
            {
                "update_id": update_id,
                "expected_generation": expected_generation,
                "envelope_hash": envelope.envelope_hash,
                "updates": normalized_updates,
                "label_refs": sorted(labels),
                "evidence_refs": sorted(evidence_tokens),
                "drift_gate_evidence": drift_gate_evidence.token,
                "stop_condition_evidence": stop_condition_evidence.token,
                "label_evidence_refs": label_tokens,
                "actual_update_cost": str(cost),
            }
        )

        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            existing = con.execute(
                "SELECT * FROM online_updates WHERE update_id=?",
                (update_id,),
            ).fetchone()
            if existing is not None:
                if existing["request_fingerprint"] != request_fingerprint:
                    raise PromotionConflict(
                        "online update identity conflicts with durable request"
                    )
                return dict(existing)

            state = con.execute(
                "SELECT * FROM routing_state WHERE singleton=1"
            ).fetchone()
            if int(state["generation"]) != expected_generation:
                raise PromotionConflict(
                    "routing generation changed before online update"
                )
            if state["champion_artifact_hash"] != envelope.champion_artifact_hash:
                raise ValueError(
                    "online envelope is not bound to the active champion artifact"
                )
            if state["authority_scope_id"] != envelope.authority_scope_id:
                raise ValueError(
                    "online envelope authority scope does not match active champion"
                )

            prior_envelope = con.execute(
                """SELECT envelope_hash FROM online_updates
                   WHERE envelope_id=? LIMIT 1""",
                (envelope.envelope_id,),
            ).fetchone()
            if (
                prior_envelope is not None
                and prior_envelope["envelope_hash"] != envelope.envelope_hash
            ):
                raise ValueError(
                    "envelope_id cannot be reused with different immutable content"
                )

            latest = con.execute(
                """SELECT applied_at FROM online_updates
                   WHERE envelope_id=?
                   ORDER BY applied_at DESC, update_id DESC LIMIT 1""",
                (envelope.envelope_id,),
            ).fetchone()
            if latest is not None:
                prior = datetime.fromisoformat(latest["applied_at"])
                elapsed = (applied - prior.astimezone(timezone.utc)).total_seconds()
                if elapsed < 0:
                    raise ValueError("online update time cannot move backwards")
                if elapsed < envelope.minimum_update_interval_seconds:
                    raise ValueError(
                        "online update violates the approved update frequency"
                    )

            con.execute(
                """INSERT INTO online_updates(
                    update_id,routing_generation,champion_artifact_hash,
                    authority_scope_id,envelope_id,envelope_hash,updates_json,
                    label_refs_json,evidence_refs_json,actual_update_cost,
                    request_fingerprint,applied_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    update_id,
                    expected_generation,
                    envelope.champion_artifact_hash,
                    envelope.authority_scope_id,
                    envelope.envelope_id,
                    envelope.envelope_hash,
                    json.dumps(
                        normalized_updates,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    json.dumps(sorted(labels), separators=(",", ":")),
                    json.dumps(
                        {
                            "supporting": sorted(evidence_tokens),
                            "drift_gate": drift_gate_evidence.token,
                            "stop_condition": stop_condition_evidence.token,
                            "labels": label_tokens,
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    str(cost),
                    request_fingerprint,
                    applied.isoformat(),
                ),
            )
            row = con.execute(
                "SELECT * FROM online_updates WHERE update_id=?",
                (update_id,),
            ).fetchone()
            return dict(row)

    def history(self) -> tuple[dict, ...]:
        with self._connect() as con:
            rows = con.execute("SELECT * FROM promotion_history ORDER BY generation").fetchall()
        return tuple(dict(row) for row in rows)
