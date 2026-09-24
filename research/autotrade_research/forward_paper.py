"""Fail-closed evidence evaluation for frozen forward-paper campaigns.

This module does not schedule campaigns, call providers, submit orders, select a
champion or determine economic edge. It only validates recorded forward-paper
evidence against a protocol that was frozen before observations were evaluated.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Mapping, Sequence
import re


class ForwardPaperError(ValueError):
    """Raised when forward-paper evidence is malformed or causally invalid."""


_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_GIT_SHA = re.compile(r"^[0-9a-f]{40}$")


def _text(value: str, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ForwardPaperError(f"{name} is required")
    return value.strip()


def _instant(value: str, *, name: str) -> datetime:
    text = _text(value, name=name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise ForwardPaperError(f"{name} must be an ISO timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ForwardPaperError(f"{name} must include timezone")
    return parsed.astimezone(timezone.utc)


def _hash(value: str, *, name: str) -> str:
    text = _text(value, name=name)
    if _SHA256.fullmatch(text) is None:
        raise ForwardPaperError(f"{name} must be canonical sha256:<64 lowercase hex>")
    return text


def _decimal(value, *, name: str, nonnegative: bool = False) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise ForwardPaperError(f"{name} must use exact decimal input")
    try:
        result = value if isinstance(value, Decimal) else Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ForwardPaperError(f"{name} must be a finite decimal") from error
    if not result.is_finite():
        raise ForwardPaperError(f"{name} must be a finite decimal")
    if nonnegative and result < 0:
        raise ForwardPaperError(f"{name} cannot be negative")
    return result


def _positive_int(value: int, *, name: str, allow_zero: bool = False) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ForwardPaperError(f"{name} must be an integer")
    minimum = 0 if allow_zero else 1
    if value < minimum:
        raise ForwardPaperError(f"{name} must be >= {minimum}")
    return value


def _unique_text(values: Sequence[str], *, name: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise ForwardPaperError(f"{name} must be a sequence")
    normalized = tuple(_text(value, name=name) for value in values)
    if len(set(normalized)) != len(normalized):
        raise ForwardPaperError(f"{name} contains duplicates")
    return normalized


@dataclass(frozen=True)
class ForwardPaperProtocol:
    campaign_id: str
    exact_build_sha: str
    protocol_hash: str
    starts_at: str
    ends_at: str
    minimum_predictions: int
    maximum_decision_latency_ms: int
    required_provider_capabilities: tuple[str, ...]
    required_operational_cases: tuple[str, ...]

    @classmethod
    def create(
        cls,
        *,
        campaign_id: str,
        exact_build_sha: str,
        protocol_hash: str,
        starts_at: str,
        ends_at: str,
        minimum_predictions: int,
        maximum_decision_latency_ms: int,
        required_provider_capabilities: Sequence[str],
        required_operational_cases: Sequence[str],
    ) -> "ForwardPaperProtocol":
        build = _text(exact_build_sha, name="exact_build_sha").lower()
        if _GIT_SHA.fullmatch(build) is None:
            raise ForwardPaperError("exact_build_sha must be a 40-character lowercase git SHA")
        start = _instant(starts_at, name="starts_at")
        end = _instant(ends_at, name="ends_at")
        if end <= start:
            raise ForwardPaperError("ends_at must be after starts_at")
        capabilities = _unique_text(
            required_provider_capabilities,
            name="required_provider_capabilities",
        )
        if not capabilities:
            raise ForwardPaperError("at least one provider capability is required")
        cases = tuple(
            value.upper()
            for value in _unique_text(
                required_operational_cases,
                name="required_operational_cases",
            )
        )
        return cls(
            campaign_id=_text(campaign_id, name="campaign_id"),
            exact_build_sha=build,
            protocol_hash=_hash(protocol_hash, name="protocol_hash"),
            starts_at=starts_at,
            ends_at=ends_at,
            minimum_predictions=_positive_int(
                minimum_predictions,
                name="minimum_predictions",
            ),
            maximum_decision_latency_ms=_positive_int(
                maximum_decision_latency_ms,
                name="maximum_decision_latency_ms",
                allow_zero=True,
            ),
            required_provider_capabilities=capabilities,
            required_operational_cases=cases,
        )


@dataclass(frozen=True)
class SealedPrediction:
    prediction_id: str
    provider_capability: str
    input_hash: str
    proposal_hash: str
    information_cutoff_at: str
    sealed_at: str
    decision_deadline_at: str
    outcome_horizon_end_at: str
    decision_latency_ms: int

    @classmethod
    def create(
        cls,
        *,
        prediction_id: str,
        provider_capability: str,
        input_hash: str,
        proposal_hash: str,
        information_cutoff_at: str,
        sealed_at: str,
        decision_deadline_at: str,
        outcome_horizon_end_at: str,
        decision_latency_ms: int,
    ) -> "SealedPrediction":
        cutoff = _instant(information_cutoff_at, name="information_cutoff_at")
        sealed = _instant(sealed_at, name="sealed_at")
        deadline = _instant(decision_deadline_at, name="decision_deadline_at")
        horizon = _instant(outcome_horizon_end_at, name="outcome_horizon_end_at")
        if cutoff > sealed:
            raise ForwardPaperError("information cutoff cannot be after sealing")
        if horizon <= sealed:
            raise ForwardPaperError("outcome horizon must be after sealing")
        return cls(
            prediction_id=_text(prediction_id, name="prediction_id"),
            provider_capability=_text(
                provider_capability,
                name="provider_capability",
            ),
            input_hash=_hash(input_hash, name="input_hash"),
            proposal_hash=_hash(proposal_hash, name="proposal_hash"),
            information_cutoff_at=information_cutoff_at,
            sealed_at=sealed_at,
            decision_deadline_at=decision_deadline_at,
            outcome_horizon_end_at=outcome_horizon_end_at,
            decision_latency_ms=_positive_int(
                decision_latency_ms,
                name="decision_latency_ms",
                allow_zero=True,
            ),
        )

    @property
    def met_deadline(self) -> bool:
        return _instant(self.sealed_at, name="sealed_at") <= _instant(
            self.decision_deadline_at,
            name="decision_deadline_at",
        )


@dataclass(frozen=True)
class ForwardOutcome:
    prediction_id: str
    outcome_hash: str
    outcome_available_at: str
    evaluated_at: str

    @classmethod
    def create(
        cls,
        *,
        prediction_id: str,
        outcome_hash: str,
        outcome_available_at: str,
        evaluated_at: str,
    ) -> "ForwardOutcome":
        available = _instant(outcome_available_at, name="outcome_available_at")
        evaluated = _instant(evaluated_at, name="evaluated_at")
        if evaluated < available:
            raise ForwardPaperError("evaluation cannot precede outcome availability")
        return cls(
            prediction_id=_text(prediction_id, name="prediction_id"),
            outcome_hash=_hash(outcome_hash, name="outcome_hash"),
            outcome_available_at=outcome_available_at,
            evaluated_at=evaluated_at,
        )


@dataclass(frozen=True)
class OperationalObservation:
    provider_capability: str
    case: str
    observed_at: str
    reconciled: bool

    @classmethod
    def create(
        cls,
        *,
        provider_capability: str,
        case: str,
        observed_at: str,
        reconciled: bool,
    ) -> "OperationalObservation":
        if type(reconciled) is not bool:
            raise ForwardPaperError("reconciled must be boolean")
        _instant(observed_at, name="observed_at")
        return cls(
            provider_capability=_text(
                provider_capability,
                name="provider_capability",
            ),
            case=_text(case, name="case").upper(),
            observed_at=observed_at,
            reconciled=reconciled,
        )


@dataclass(frozen=True)
class ForwardPaperEvidence:
    exact_build_sha: str
    protocol_hash: str
    observed_until: str
    predictions: tuple[SealedPrediction, ...]
    outcomes: tuple[ForwardOutcome, ...]
    operational_observations: tuple[OperationalObservation, ...]
    costs_by_currency: Mapping[str, Decimal]
    costs_complete: bool
    account_reconciliation_complete: bool

    @classmethod
    def create(
        cls,
        *,
        exact_build_sha: str,
        protocol_hash: str,
        observed_until: str,
        predictions: Sequence[SealedPrediction],
        outcomes: Sequence[ForwardOutcome],
        operational_observations: Sequence[OperationalObservation],
        costs_by_currency: Mapping[str, object],
        costs_complete: bool,
        account_reconciliation_complete: bool,
    ) -> "ForwardPaperEvidence":
        build = _text(exact_build_sha, name="exact_build_sha").lower()
        if _GIT_SHA.fullmatch(build) is None:
            raise ForwardPaperError("exact_build_sha must be a 40-character lowercase git SHA")
        _instant(observed_until, name="observed_until")
        if type(costs_complete) is not bool or type(account_reconciliation_complete) is not bool:
            raise ForwardPaperError("completion flags must be boolean")
        if not isinstance(costs_by_currency, Mapping):
            raise TypeError("costs_by_currency must be a mapping")
        costs: dict[str, Decimal] = {}
        for currency, value in costs_by_currency.items():
            code = _text(str(currency), name="cost currency").upper()
            if code in costs:
                raise ForwardPaperError("duplicate cost currency")
            costs[code] = _decimal(value, name=f"cost[{code}]", nonnegative=True)
        return cls(
            exact_build_sha=build,
            protocol_hash=_hash(protocol_hash, name="protocol_hash"),
            observed_until=observed_until,
            predictions=tuple(predictions),
            outcomes=tuple(outcomes),
            operational_observations=tuple(operational_observations),
            costs_by_currency=MappingProxyType(costs),
            costs_complete=costs_complete,
            account_reconciliation_complete=account_reconciliation_complete,
        )


@dataclass(frozen=True)
class ForwardPaperAssessment:
    evidence_status: str
    operational_status: str
    economic_edge_status: str
    reasons: tuple[str, ...]
    prediction_count: int
    evaluated_outcome_count: int

    def __post_init__(self) -> None:
        if self.evidence_status not in {"VALID", "INCONCLUSIVE", "INVALID"}:
            raise ForwardPaperError("unsupported evidence_status")
        if self.operational_status not in {"PASS", "FAIL", "INCONCLUSIVE"}:
            raise ForwardPaperError("unsupported operational_status")
        if self.economic_edge_status != "NOT_ESTABLISHED":
            raise ForwardPaperError(
                "forward-paper mechanics foundation cannot establish economic edge"
            )


def assess_forward_paper(
    protocol: ForwardPaperProtocol,
    evidence: ForwardPaperEvidence,
) -> ForwardPaperAssessment:
    """Validate frozen forward evidence without declaring economic edge."""

    if not isinstance(protocol, ForwardPaperProtocol):
        raise TypeError("protocol must be ForwardPaperProtocol")
    if not isinstance(evidence, ForwardPaperEvidence):
        raise TypeError("evidence must be ForwardPaperEvidence")

    invalid: list[str] = []
    incomplete: list[str] = []
    operational_failures: list[str] = []

    if evidence.exact_build_sha != protocol.exact_build_sha:
        invalid.append("exact_build_sha_mismatch")
    if evidence.protocol_hash != protocol.protocol_hash:
        invalid.append("protocol_hash_mismatch")

    campaign_start = _instant(protocol.starts_at, name="starts_at")
    campaign_end = _instant(protocol.ends_at, name="ends_at")
    observed_until = _instant(evidence.observed_until, name="observed_until")

    prediction_by_id: dict[str, SealedPrediction] = {}
    capability_counts = {
        capability: 0 for capability in protocol.required_provider_capabilities
    }
    for prediction in evidence.predictions:
        if not isinstance(prediction, SealedPrediction):
            invalid.append("invalid_prediction_record")
            continue
        if prediction.prediction_id in prediction_by_id:
            invalid.append("duplicate_prediction_id")
            continue
        prediction_by_id[prediction.prediction_id] = prediction
        sealed = _instant(prediction.sealed_at, name="sealed_at")
        if sealed < campaign_start or sealed > campaign_end:
            invalid.append("prediction_outside_campaign_window")
        if prediction.provider_capability not in capability_counts:
            invalid.append("undeclared_provider_capability")
        else:
            capability_counts[prediction.provider_capability] += 1
        if not prediction.met_deadline:
            operational_failures.append("decision_deadline_missed")
        if prediction.decision_latency_ms > protocol.maximum_decision_latency_ms:
            operational_failures.append("decision_latency_budget_exceeded")

    if len(prediction_by_id) < protocol.minimum_predictions:
        incomplete.append("minimum_prediction_count_not_reached")
    for capability, count in capability_counts.items():
        if count == 0:
            incomplete.append(f"missing_provider_capability:{capability}")

    outcomes_by_prediction: dict[str, ForwardOutcome] = {}
    for outcome in evidence.outcomes:
        if not isinstance(outcome, ForwardOutcome):
            invalid.append("invalid_outcome_record")
            continue
        if outcome.prediction_id in outcomes_by_prediction:
            invalid.append("duplicate_outcome_for_prediction")
            continue
        prediction = prediction_by_id.get(outcome.prediction_id)
        if prediction is None:
            invalid.append("outcome_without_sealed_prediction")
            continue
        outcomes_by_prediction[outcome.prediction_id] = outcome
        available = _instant(outcome.outcome_available_at, name="outcome_available_at")
        horizon = _instant(
            prediction.outcome_horizon_end_at,
            name="outcome_horizon_end_at",
        )
        if available < horizon:
            invalid.append("outcome_available_before_registered_horizon")

    if observed_until < campaign_end:
        incomplete.append("campaign_window_not_finished")
    for prediction_id in prediction_by_id:
        if prediction_id not in outcomes_by_prediction:
            incomplete.append("missing_forward_outcome")

    required_cases = set(protocol.required_operational_cases)
    for capability in protocol.required_provider_capabilities:
        observed_cases = {
            item.case
            for item in evidence.operational_observations
            if isinstance(item, OperationalObservation)
            and item.provider_capability == capability
            and item.reconciled
        }
        for case in sorted(required_cases - observed_cases):
            incomplete.append(f"missing_operational_case:{capability}:{case}")

    for item in evidence.operational_observations:
        if not isinstance(item, OperationalObservation):
            invalid.append("invalid_operational_observation")
            continue
        if item.provider_capability not in capability_counts:
            invalid.append("operational_case_for_undeclared_capability")
        if not item.reconciled:
            operational_failures.append("unreconciled_operational_case")

    if not evidence.costs_complete:
        incomplete.append("actual_costs_incomplete")
    if not evidence.account_reconciliation_complete:
        incomplete.append("account_reconciliation_incomplete")

    invalid_reasons = tuple(dict.fromkeys(invalid))
    incomplete_reasons = tuple(dict.fromkeys(incomplete))
    failure_reasons = tuple(dict.fromkeys(operational_failures))

    if invalid_reasons:
        evidence_status = "INVALID"
        operational_status = "INCONCLUSIVE"
        reasons = invalid_reasons + incomplete_reasons + failure_reasons
    elif incomplete_reasons:
        evidence_status = "INCONCLUSIVE"
        operational_status = "FAIL" if failure_reasons else "INCONCLUSIVE"
        reasons = incomplete_reasons + failure_reasons
    else:
        evidence_status = "VALID"
        operational_status = "FAIL" if failure_reasons else "PASS"
        reasons = failure_reasons

    return ForwardPaperAssessment(
        evidence_status=evidence_status,
        operational_status=operational_status,
        economic_edge_status="NOT_ESTABLISHED",
        reasons=reasons,
        prediction_count=len(prediction_by_id),
        evaluated_outcome_count=len(outcomes_by_prediction),
    )
