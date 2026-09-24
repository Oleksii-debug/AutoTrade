"""Evidence gate for forward paper/testnet operation.

The evaluator consumes externally produced observations.  It does not connect to a
provider and it never treats backtests or simulations as forward evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from typing import Iterable


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


def _decimal(value, *, name: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError(f"{name} requires exact decimal input")
    try:
        result = value if isinstance(value, Decimal) else Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError(f"{name} must be decimal") from error
    if not result.is_finite():
        raise ValueError(f"{name} must be finite")
    return result


@dataclass(frozen=True)
class ForwardPaperEvidence:
    run_id: str
    source_revision: str
    provider_id: str
    environment: str
    account_fingerprint: str
    started_at: str
    ended_at: str
    decision_count: int
    submitted_order_count: int
    reconciled_order_count: int
    unknown_submission_count: int
    unresolved_reconciliation_count: int
    market_data_gap_count: int
    restart_recovery_count: int
    restart_recovery_failures: int
    realized_pnl: str
    explicit_costs: str
    max_drawdown: str
    evidence_ids: tuple[str, ...]

    def normalized(self) -> "ForwardPaperEvidence":
        if not self.run_id.strip() or not self.source_revision.strip():
            raise ValueError("run_id and source_revision are required")
        if not self.provider_id.strip() or not self.account_fingerprint.strip():
            raise ValueError("provider/account evidence is required")
        env = self.environment.strip().upper()
        if env not in {"PAPER", "TEST", "DEMO"}:
            raise ValueError("forward evidence must be PAPER, TEST or DEMO")
        start = _instant(self.started_at, name="started_at")
        end = _instant(self.ended_at, name="ended_at")
        if end <= start:
            raise ValueError("ended_at must be after started_at")
        counts = (
            self.decision_count,
            self.submitted_order_count,
            self.reconciled_order_count,
            self.unknown_submission_count,
            self.unresolved_reconciliation_count,
            self.market_data_gap_count,
            self.restart_recovery_count,
            self.restart_recovery_failures,
        )
        if any(isinstance(item, bool) or not isinstance(item, int) or item < 0 for item in counts):
            raise ValueError("counts must be non-negative integers")
        if self.reconciled_order_count > self.submitted_order_count:
            raise ValueError("reconciled orders cannot exceed submitted orders")
        if self.restart_recovery_failures > self.restart_recovery_count:
            raise ValueError("restart failures cannot exceed restart attempts")
        _decimal(self.realized_pnl, name="realized_pnl")
        costs = _decimal(self.explicit_costs, name="explicit_costs")
        drawdown = _decimal(self.max_drawdown, name="max_drawdown")
        if costs < 0 or drawdown < 0:
            raise ValueError("costs/drawdown cannot be negative")
        if not self.evidence_ids or any(not item.strip() for item in self.evidence_ids):
            raise ValueError("durable evidence_ids are required")
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ValueError("evidence_ids must be unique")
        return self

    def duration_hours(self) -> Decimal:
        start = _instant(self.started_at, name="started_at")
        end = _instant(self.ended_at, name="ended_at")
        seconds = Decimal(str((end - start).total_seconds()))
        return seconds / Decimal("3600")

    def fingerprint(self) -> str:
        self.normalized()
        payload = {
            key: getattr(self, key)
            for key in self.__dataclass_fields__
        }
        payload["evidence_ids"] = list(self.evidence_ids)
        return sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()


@dataclass(frozen=True)
class ForwardRequirements:
    min_duration_hours: Decimal | str | int
    min_decisions: int
    min_submitted_orders: int
    require_restart_exercise: bool = True

    def __post_init__(self) -> None:
        if _decimal(self.min_duration_hours, name="min_duration_hours") <= 0:
            raise ValueError("min_duration_hours must be positive")
        if self.min_decisions <= 0 or self.min_submitted_orders <= 0:
            raise ValueError("minimum counts must be positive")


@dataclass(frozen=True)
class ForwardQualification:
    passed: bool
    reasons: tuple[str, ...]
    evidence_fingerprint: str


def qualify_forward_paper(
    evidence: ForwardPaperEvidence,
    requirements: ForwardRequirements,
) -> ForwardQualification:
    evidence.normalized()
    reasons: list[str] = []
    if evidence.duration_hours() < _decimal(requirements.min_duration_hours, name="min_duration_hours"):
        reasons.append("insufficient_forward_duration")
    if evidence.decision_count < requirements.min_decisions:
        reasons.append("insufficient_decision_count")
    if evidence.submitted_order_count < requirements.min_submitted_orders:
        reasons.append("insufficient_submitted_orders")
    if evidence.reconciled_order_count != evidence.submitted_order_count:
        reasons.append("not_all_orders_reconciled")
    if evidence.unknown_submission_count:
        reasons.append("unknown_submissions_present")
    if evidence.unresolved_reconciliation_count:
        reasons.append("unresolved_reconciliation_present")
    if evidence.market_data_gap_count:
        reasons.append("market_data_gaps_present")
    if evidence.restart_recovery_failures:
        reasons.append("restart_recovery_failure")
    if requirements.require_restart_exercise and evidence.restart_recovery_count == 0:
        reasons.append("restart_recovery_not_exercised")
    return ForwardQualification(
        passed=not reasons,
        reasons=tuple(reasons),
        evidence_fingerprint=evidence.fingerprint(),
    )
