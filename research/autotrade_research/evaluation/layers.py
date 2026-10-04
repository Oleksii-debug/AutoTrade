"""Three-layer evaluation composition for product Section 19.

This module does not evaluate strategies, run replays, score paper campaigns, or
establish economic edge.  It composes the existing WP-36 scientific gate result
and WP-57 forward-paper result into three explicitly distinct evidence classes:

A. blinded market replay;
B. causal information replay;
C. live forward paper.

The boundary is deliberately fail-closed: a result from one layer cannot stand
in for another, historical PASS cannot substitute for forward evidence, and a
three-layer PASS is still not an economic-edge claim.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import re
from types import MappingProxyType
from typing import Mapping, Sequence

from .gates import GateDecision
from ..forward_paper import (
    ForwardOutcome,
    ForwardPaperAssessment,
    ForwardPaperEvidence,
    ForwardPaperProtocol,
    OperationalObservation,
    PaperDecisionEconomics,
    SealedPrediction,
    assess_forward_paper,
)

BLINDED_MARKET_REPLAY = "BLINDED_MARKET_REPLAY"
CAUSAL_INFORMATION_REPLAY = "CAUSAL_INFORMATION_REPLAY"
FORWARD_PAPER = "FORWARD_PAPER"

_LAYER_ORDER = (
    BLINDED_MARKET_REPLAY,
    CAUSAL_INFORMATION_REPLAY,
    FORWARD_PAPER,
)
_HISTORICAL_LAYERS = frozenset(
    {BLINDED_MARKET_REPLAY, CAUSAL_INFORMATION_REPLAY}
)
_LAYER_EVIDENCE_CLASS = {
    BLINDED_MARKET_REPLAY: "HISTORICAL_BLINDED_MARKET",
    CAUSAL_INFORMATION_REPLAY: "HISTORICAL_CAUSAL_INFORMATION",
    FORWARD_PAPER: "LIVE_FORWARD_PAPER",
}
_LAYER_INFORMATION_MODE = {
    BLINDED_MARKET_REPLAY: "CAUSAL_MARKET_ONLY_MASKED_IDENTITY_CALENDAR",
    CAUSAL_INFORMATION_REPLAY: "CAUSAL_MARKET_PLUS_ANONYMIZED_INFORMATION",
    FORWARD_PAPER: "LIVE_REAL_IDENTITIES_CURRENT_INFORMATION",
}
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_ALLOWED_STATUS = frozenset({"PASS", "FAIL", "INCONCLUSIVE"})


class EvaluationLayersError(ValueError):
    """Raised when three-layer evidence is malformed or semantically mixed."""


def _text(value: str, *, name: str) -> str:
    if type(value) is not str or value != value.strip() or not value:
        raise EvaluationLayersError(f"{name} must be non-empty exact text")
    return value


def _sha(value: str, *, name: str) -> str:
    value = _text(value, name=name)
    if _SHA256.fullmatch(value) is None:
        raise EvaluationLayersError(
            f"{name} must be canonical sha256:<64 lowercase hex>"
        )
    return value


def _git_sha(value: str, *, name: str) -> str:
    value = _text(value, name=name)
    if _GIT_SHA.fullmatch(value) is None:
        raise EvaluationLayersError(
            f"{name} must be a 40-character lowercase git SHA"
        )
    return value


def _instant(value: str, *, name: str) -> str:
    value = _text(value, name=name)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise EvaluationLayersError(
            f"{name} must be timezone-aware ISO text"
        ) from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise EvaluationLayersError(f"{name} must include timezone")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical(value: object, *, path: str = "value") -> object:
    if value is None or type(value) in {str, bool, int}:
        return value
    if type(value) in {dict, MappingProxyType}:
        result: dict[str, object] = {}
        for raw_key, raw_value in value.items():
            if type(raw_key) is not str or not raw_key:
                raise TypeError(f"{path} mapping keys must be non-empty exact strings")
            if raw_key in result:
                raise EvaluationLayersError(f"{path} contains duplicate keys")
            result[raw_key] = _canonical(raw_value, path=f"{path}.{raw_key}")
        return {key: result[key] for key in sorted(result)}
    if type(value) in {tuple, list}:
        return [
            _canonical(item, path=f"{path}[]")
            for item in value
        ]
    raise TypeError(
        f"{path} contains unsupported authority-bearing type "
        f"{type(value).__name__}"
    )


def _digest(value: object) -> str:
    payload = json.dumps(
        _canonical(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + sha256(payload).hexdigest()


def _gate_payload(decision: GateDecision) -> dict[str, object]:
    if type(decision) is not GateDecision:
        raise TypeError("historical layer requires exact GateDecision")
    if (
        type(decision.status) is not str
        or type(decision.reasons) is not tuple
        or type(decision.checks) is not MappingProxyType
        or type(decision.provenance) is not MappingProxyType
    ):
        raise TypeError("historical GateDecision has noncanonical retained fields")
    clean = GateDecision(
        status=decision.status,
        reasons=tuple(decision.reasons),
        checks=dict(decision.checks),
        provenance=(
            None
            if decision.provenance is None
            else dict(decision.provenance)
        ),
    )
    return {
        "status": clean.status,
        "reasons": tuple(clean.reasons),
        "checks": dict(clean.checks),
        "provenance": dict(clean.provenance),
    }


def _detached_forward_inputs(
    protocol: ForwardPaperProtocol,
    evidence: ForwardPaperEvidence,
    assessment: ForwardPaperAssessment,
) -> tuple[ForwardPaperProtocol, ForwardPaperEvidence, ForwardPaperAssessment]:
    if type(protocol) is not ForwardPaperProtocol:
        raise TypeError("forward layer requires exact ForwardPaperProtocol")
    if type(evidence) is not ForwardPaperEvidence:
        raise TypeError("forward layer requires exact ForwardPaperEvidence")
    if type(assessment) is not ForwardPaperAssessment:
        raise TypeError("forward layer requires exact ForwardPaperAssessment")
    if (
        type(protocol.required_provider_capabilities) is not tuple
        or type(protocol.required_operational_cases) is not tuple
        or type(protocol.required_regimes) is not tuple
        or type(protocol.required_simulation_limitations) is not tuple
        or type(evidence.predictions) is not tuple
        or type(evidence.outcomes) is not tuple
        or type(evidence.operational_observations) is not tuple
        or type(evidence.paper_economics) is not tuple
        or type(evidence.simulation_limitations) is not tuple
        or type(evidence.costs_by_currency) is not MappingProxyType
        or type(assessment.reasons) is not tuple
    ):
        raise TypeError("forward layer has noncanonical retained containers")

    clean_protocol = ForwardPaperProtocol(
        campaign_id=protocol.campaign_id,
        exact_build_sha=protocol.exact_build_sha,
        protocol_hash=protocol.protocol_hash,
        registered_at=protocol.registered_at,
        starts_at=protocol.starts_at,
        ends_at=protocol.ends_at,
        minimum_predictions=protocol.minimum_predictions,
        maximum_decision_latency_ms=protocol.maximum_decision_latency_ms,
        required_provider_capabilities=tuple(protocol.required_provider_capabilities),
        required_operational_cases=tuple(protocol.required_operational_cases),
        required_regimes=tuple(protocol.required_regimes),
        minimum_decision_units_per_regime=protocol.minimum_decision_units_per_regime,
        required_simulation_limitations=tuple(protocol.required_simulation_limitations),
        reporting_currency=protocol.reporting_currency,
        maximum_drawdown=protocol.maximum_drawdown,
        evaluation_profile_hash=protocol.evaluation_profile_hash,
    )

    clean_predictions = []
    for item in evidence.predictions:
        if type(item) is not SealedPrediction:
            raise TypeError(
                "forward evidence predictions must contain exact SealedPrediction"
            )
        clean_predictions.append(
            SealedPrediction(
                prediction_id=item.prediction_id,
                provider_capability=item.provider_capability,
                input_hash=item.input_hash,
                proposal_hash=item.proposal_hash,
                information_cutoff_at=item.information_cutoff_at,
                sealed_at=item.sealed_at,
                decision_deadline_at=item.decision_deadline_at,
                outcome_horizon_end_at=item.outcome_horizon_end_at,
                decision_latency_ms=item.decision_latency_ms,
                regime=item.regime,
                dependence_unit_id=item.dependence_unit_id,
            )
        )

    clean_outcomes = []
    for item in evidence.outcomes:
        if type(item) is not ForwardOutcome:
            raise TypeError(
                "forward evidence outcomes must contain exact ForwardOutcome"
            )
        clean_outcomes.append(
            ForwardOutcome(
                prediction_id=item.prediction_id,
                outcome_hash=item.outcome_hash,
                outcome_available_at=item.outcome_available_at,
                evaluated_at=item.evaluated_at,
            )
        )

    clean_observations = []
    for item in evidence.operational_observations:
        if type(item) is not OperationalObservation:
            raise TypeError(
                "forward evidence operational observations must contain "
                "exact OperationalObservation"
            )
        clean_observations.append(
            OperationalObservation(
                provider_capability=item.provider_capability,
                case=item.case,
                observed_at=item.observed_at,
                reconciled=item.reconciled,
            )
        )

    clean_economics = []
    for item in evidence.paper_economics:
        if type(item) is not PaperDecisionEconomics:
            raise TypeError(
                "forward evidence economics must contain exact PaperDecisionEconomics"
            )
        clean_economics.append(
            PaperDecisionEconomics(
                prediction_id=item.prediction_id,
                currency=item.currency,
                sequence=item.sequence,
                realized_at=item.realized_at,
                gross_pnl=item.gross_pnl,
                fees=item.fees,
                spread_cost=item.spread_cost,
                slippage_cost=item.slippage_cost,
                net_pnl=item.net_pnl,
                equity_before=item.equity_before,
                equity_after=item.equity_after,
                peak_equity_before=item.peak_equity_before,
            )
        )

    clean_evidence = ForwardPaperEvidence(
        exact_build_sha=evidence.exact_build_sha,
        protocol_hash=evidence.protocol_hash,
        observed_until=evidence.observed_until,
        predictions=tuple(clean_predictions),
        outcomes=tuple(clean_outcomes),
        operational_observations=tuple(clean_observations),
        costs_by_currency=dict(evidence.costs_by_currency),
        costs_complete=evidence.costs_complete,
        account_reconciliation_complete=evidence.account_reconciliation_complete,
        paper_economics=tuple(clean_economics),
        simulation_limitations=tuple(evidence.simulation_limitations),
        evaluation_profile_hash=evidence.evaluation_profile_hash,
    )
    clean_assessment = ForwardPaperAssessment(
        evidence_status=assessment.evidence_status,
        operational_status=assessment.operational_status,
        economic_edge_status=assessment.economic_edge_status,
        reasons=tuple(assessment.reasons),
        prediction_count=assessment.prediction_count,
        evaluated_outcome_count=assessment.evaluated_outcome_count,
    )
    return clean_protocol, clean_evidence, clean_assessment


def _forward_assessment_payload(
    assessment: ForwardPaperAssessment,
) -> dict[str, object]:
    if type(assessment) is not ForwardPaperAssessment:
        raise TypeError("forward layer requires exact ForwardPaperAssessment")
    return {
        "evidence_status": assessment.evidence_status,
        "operational_status": assessment.operational_status,
        "economic_edge_status": assessment.economic_edge_status,
        "reasons": tuple(assessment.reasons),
        "prediction_count": assessment.prediction_count,
        "evaluated_outcome_count": assessment.evaluated_outcome_count,
    }


def _forward_evidence_payload(evidence: ForwardPaperEvidence) -> dict[str, object]:
    if type(evidence) is not ForwardPaperEvidence:
        raise TypeError("forward layer requires exact ForwardPaperEvidence")
    return {
        "exact_build_sha": evidence.exact_build_sha,
        "protocol_hash": evidence.protocol_hash,
        "observed_until": evidence.observed_until,
        "predictions": tuple(
            {
                "prediction_id": item.prediction_id,
                "provider_capability": item.provider_capability,
                "input_hash": item.input_hash,
                "proposal_hash": item.proposal_hash,
                "information_cutoff_at": item.information_cutoff_at,
                "sealed_at": item.sealed_at,
                "decision_deadline_at": item.decision_deadline_at,
                "outcome_horizon_end_at": item.outcome_horizon_end_at,
                "decision_latency_ms": item.decision_latency_ms,
                "regime": item.regime,
                "dependence_unit_id": item.dependence_unit_id,
            }
            for item in evidence.predictions
        ),
        "outcomes": tuple(
            {
                "prediction_id": item.prediction_id,
                "outcome_hash": item.outcome_hash,
                "outcome_available_at": item.outcome_available_at,
                "evaluated_at": item.evaluated_at,
            }
            for item in evidence.outcomes
        ),
        "operational_observations": tuple(
            {
                "provider_capability": item.provider_capability,
                "case": item.case,
                "observed_at": item.observed_at,
                "reconciled": item.reconciled,
            }
            for item in evidence.operational_observations
        ),
        "costs_by_currency": {
            key: str(value)
            for key, value in sorted(evidence.costs_by_currency.items())
        },
        "paper_economics": tuple(
            {
                "prediction_id": item.prediction_id,
                "currency": item.currency,
                "sequence": item.sequence,
                "realized_at": item.realized_at,
                "gross_pnl": str(item.gross_pnl),
                "fees": str(item.fees),
                "spread_cost": str(item.spread_cost),
                "slippage_cost": str(item.slippage_cost),
                "net_pnl": str(item.net_pnl),
                "equity_before": str(item.equity_before),
                "equity_after": str(item.equity_after),
                "peak_equity_before": str(item.peak_equity_before),
            }
            for item in evidence.paper_economics
        ),
        "simulation_limitations": tuple(evidence.simulation_limitations),
        "evaluation_profile_hash": evidence.evaluation_profile_hash,
        "costs_complete": evidence.costs_complete,
        "account_reconciliation_complete": evidence.account_reconciliation_complete,
    }


@dataclass(frozen=True, slots=True)
class EvaluationLayerReceipt:
    """One immutable, typed result in the three-layer evidence chain."""

    layer: str
    candidate_id: str
    exact_build_sha: str
    candidate_selected_at: str
    protocol_sha256: str
    evidence_sha256: str
    authority_result_sha256: str
    status: str
    evidence_class: str
    information_mode: str
    forward_events_unavailable_at_selection: bool
    model_training_cutoff_uncertainty: str
    economic_edge_status: str = "NOT_ESTABLISHED"

    def __post_init__(self) -> None:
        layer = _text(self.layer, name="layer")
        if layer not in _LAYER_ORDER:
            raise EvaluationLayersError(f"unsupported evaluation layer: {layer}")
        object.__setattr__(self, "layer", layer)
        object.__setattr__(
            self,
            "candidate_id",
            _text(self.candidate_id, name="candidate_id"),
        )
        object.__setattr__(
            self,
            "exact_build_sha",
            _git_sha(self.exact_build_sha, name="exact_build_sha"),
        )
        object.__setattr__(
            self,
            "candidate_selected_at",
            _instant(self.candidate_selected_at, name="candidate_selected_at"),
        )
        object.__setattr__(
            self,
            "protocol_sha256",
            _sha(self.protocol_sha256, name="protocol_sha256"),
        )
        object.__setattr__(
            self,
            "evidence_sha256",
            _sha(self.evidence_sha256, name="evidence_sha256"),
        )
        object.__setattr__(
            self,
            "authority_result_sha256",
            _sha(
                self.authority_result_sha256,
                name="authority_result_sha256",
            ),
        )
        status = _text(self.status, name="status")
        if status not in _ALLOWED_STATUS:
            raise EvaluationLayersError(
                "status must be PASS, FAIL or INCONCLUSIVE"
            )
        object.__setattr__(self, "status", status)

        expected_class = _LAYER_EVIDENCE_CLASS[layer]
        evidence_class = _text(self.evidence_class, name="evidence_class")
        if evidence_class != expected_class:
            raise EvaluationLayersError(
                f"{layer} requires evidence_class {expected_class}"
            )
        object.__setattr__(self, "evidence_class", evidence_class)

        expected_mode = _LAYER_INFORMATION_MODE[layer]
        information_mode = _text(self.information_mode, name="information_mode")
        if information_mode != expected_mode:
            raise EvaluationLayersError(
                f"{layer} requires information_mode {expected_mode}"
            )
        object.__setattr__(self, "information_mode", information_mode)

        if type(self.forward_events_unavailable_at_selection) is not bool:
            raise TypeError(
                "forward_events_unavailable_at_selection must be boolean"
            )
        expected_forward = layer == FORWARD_PAPER
        if self.forward_events_unavailable_at_selection is not expected_forward:
            raise EvaluationLayersError(
                f"{layer} has the wrong forward-event evidence class"
            )

        uncertainty = _text(
            self.model_training_cutoff_uncertainty,
            name="model_training_cutoff_uncertainty",
        )
        if layer in _HISTORICAL_LAYERS and uncertainty == "N/A_FORWARD_EVENTS":
            raise EvaluationLayersError(
                "historical layers must record model training-cutoff uncertainty"
            )
        if layer == FORWARD_PAPER and uncertainty != "N/A_FORWARD_EVENTS":
            raise EvaluationLayersError(
                "forward layer must use N/A_FORWARD_EVENTS for historical "
                "training-cutoff uncertainty"
            )
        object.__setattr__(
            self,
            "model_training_cutoff_uncertainty",
            uncertainty,
        )

        if self.economic_edge_status != "NOT_ESTABLISHED":
            raise EvaluationLayersError(
                "three-layer mechanics cannot establish economic edge"
            )

    @property
    def digest(self) -> str:
        return _digest(
            {
                "schema_version": 1,
                "layer": self.layer,
                "candidate_id": self.candidate_id,
                "exact_build_sha": self.exact_build_sha,
                "candidate_selected_at": self.candidate_selected_at,
                "protocol_sha256": self.protocol_sha256,
                "evidence_sha256": self.evidence_sha256,
                "authority_result_sha256": self.authority_result_sha256,
                "status": self.status,
                "evidence_class": self.evidence_class,
                "information_mode": self.information_mode,
                "forward_events_unavailable_at_selection":
                    self.forward_events_unavailable_at_selection,
                "model_training_cutoff_uncertainty":
                    self.model_training_cutoff_uncertainty,
                "economic_edge_status": self.economic_edge_status,
            }
        )

    @classmethod
    def from_historical_gate(
        cls,
        *,
        layer: str,
        candidate_id: str,
        exact_build_sha: str,
        candidate_selected_at: str,
        protocol_sha256: str,
        evidence_sha256: str,
        decision: GateDecision,
        model_training_cutoff_uncertainty: str,
    ) -> "EvaluationLayerReceipt":
        layer = _text(layer, name="layer")
        if layer not in _HISTORICAL_LAYERS:
            raise EvaluationLayersError(
                "historical GateDecision can only populate layer A or B"
            )
        payload = _gate_payload(decision)
        check_statuses = tuple(payload["checks"].values())
        derived_status = (
            "FAIL"
            if "FAIL" in check_statuses
            else (
                "INCONCLUSIVE"
                if "INCONCLUSIVE" in check_statuses
                else "PASS"
            )
        )
        if decision.status != derived_status:
            raise EvaluationLayersError(
                "GateDecision status contradicts its own check statuses"
            )

        # A bare GateDecision is a public value, not independent scientific
        # authority.  WP-36 issue #1116 remains open on current main: callers
        # can self-publish internally consistent PASS material without an
        # issuer-owned authenticated evidence graph.  Preserve negative FAIL
        # evidence, but never promote an unissued historical PASS through this
        # composition.  Once WP-36 exposes a non-caller-forgeable accepted
        # result, this boundary must consume that authority rather than infer it
        # from provenance fields supplied by the caller.
        receipt_status = (
            "INCONCLUSIVE" if decision.status == "PASS" else decision.status
        )
        return cls(
            layer=layer,
            candidate_id=candidate_id,
            exact_build_sha=exact_build_sha,
            candidate_selected_at=candidate_selected_at,
            protocol_sha256=protocol_sha256,
            evidence_sha256=evidence_sha256,
            authority_result_sha256=_digest(payload),
            status=receipt_status,
            evidence_class=_LAYER_EVIDENCE_CLASS[layer],
            information_mode=_LAYER_INFORMATION_MODE[layer],
            forward_events_unavailable_at_selection=False,
            model_training_cutoff_uncertainty=model_training_cutoff_uncertainty,
        )

    @classmethod
    def from_forward_paper(
        cls,
        *,
        candidate_id: str,
        candidate_selected_at: str,
        protocol: ForwardPaperProtocol,
        evidence: ForwardPaperEvidence,
        assessment: ForwardPaperAssessment,
    ) -> "EvaluationLayerReceipt":
        protocol, evidence, assessment = _detached_forward_inputs(
            protocol,
            evidence,
            assessment,
        )
        recomputed = assess_forward_paper(protocol, evidence)
        if recomputed != assessment:
            raise EvaluationLayersError(
                "forward assessment does not match canonical recomputation"
            )

        selected = _instant(candidate_selected_at, name="candidate_selected_at")
        registered = _instant(protocol.registered_at, name="registered_at")
        if selected > registered:
            raise EvaluationLayersError(
                "candidate must be selected before the forward protocol is registered"
            )
        if evidence.exact_build_sha != protocol.exact_build_sha:
            raise EvaluationLayersError(
                "forward evidence build differs from frozen protocol build"
            )
        if evidence.protocol_hash != protocol.protocol_hash:
            raise EvaluationLayersError(
                "forward evidence protocol differs from frozen protocol"
            )

        if assessment.evidence_status == "INVALID":
            status = "FAIL"
        elif assessment.operational_status == "FAIL":
            status = "FAIL"
        elif (
            assessment.evidence_status == "VALID"
            and assessment.operational_status == "PASS"
        ):
            status = "PASS"
        else:
            status = "INCONCLUSIVE"

        return cls(
            layer=FORWARD_PAPER,
            candidate_id=candidate_id,
            exact_build_sha=protocol.exact_build_sha,
            candidate_selected_at=selected,
            protocol_sha256=protocol.protocol_hash,
            evidence_sha256=_digest(_forward_evidence_payload(evidence)),
            authority_result_sha256=_digest(
                _forward_assessment_payload(assessment)
            ),
            status=status,
            evidence_class=_LAYER_EVIDENCE_CLASS[FORWARD_PAPER],
            information_mode=_LAYER_INFORMATION_MODE[FORWARD_PAPER],
            forward_events_unavailable_at_selection=True,
            model_training_cutoff_uncertainty="N/A_FORWARD_EVENTS",
        )


def _detached_receipt(value: EvaluationLayerReceipt) -> EvaluationLayerReceipt:
    if type(value) is not EvaluationLayerReceipt:
        raise TypeError("layers must contain exact EvaluationLayerReceipt")
    return EvaluationLayerReceipt(
        layer=value.layer,
        candidate_id=value.candidate_id,
        exact_build_sha=value.exact_build_sha,
        candidate_selected_at=value.candidate_selected_at,
        protocol_sha256=value.protocol_sha256,
        evidence_sha256=value.evidence_sha256,
        authority_result_sha256=value.authority_result_sha256,
        status=value.status,
        evidence_class=value.evidence_class,
        information_mode=value.information_mode,
        forward_events_unavailable_at_selection=
            value.forward_events_unavailable_at_selection,
        model_training_cutoff_uncertainty=
            value.model_training_cutoff_uncertainty,
        economic_edge_status=value.economic_edge_status,
    )


@dataclass(frozen=True, slots=True)
class ThreeLayerEvaluation:
    """Fail-closed composition of exactly one A, B and C result."""

    candidate_id: str
    exact_build_sha: str
    candidate_selected_at: str
    layers: tuple[EvaluationLayerReceipt, ...]

    def __post_init__(self) -> None:
        candidate_id = _text(self.candidate_id, name="candidate_id")
        exact_build_sha = _git_sha(
            self.exact_build_sha,
            name="exact_build_sha",
        )
        selected = _instant(
            self.candidate_selected_at,
            name="candidate_selected_at",
        )
        if type(self.layers) is not tuple:
            raise TypeError("layers must be an exact tuple")
        if len(self.layers) != 3:
            raise EvaluationLayersError(
                "three-layer evaluation requires exactly three receipts"
            )

        receipts = tuple(_detached_receipt(item) for item in self.layers)
        by_layer: dict[str, EvaluationLayerReceipt] = {}
        for receipt in receipts:
            if receipt.layer in by_layer:
                raise EvaluationLayersError(
                    f"duplicate evaluation layer: {receipt.layer}"
                )
            by_layer[receipt.layer] = receipt
            if receipt.candidate_id != candidate_id:
                raise EvaluationLayersError(
                    "all layers must bind the same candidate_id"
                )
            if receipt.exact_build_sha != exact_build_sha:
                raise EvaluationLayersError(
                    "all layers must bind the same exact build"
                )
            if receipt.candidate_selected_at != selected:
                raise EvaluationLayersError(
                    "all layers must bind the same candidate selection cut"
                )

        if set(by_layer) != set(_LAYER_ORDER):
            raise EvaluationLayersError(
                "layers must contain blinded market, causal information and "
                "forward paper exactly once"
            )

        protocol_ids = {item.protocol_sha256 for item in receipts}
        evidence_ids = {item.evidence_sha256 for item in receipts}
        result_ids = {item.authority_result_sha256 for item in receipts}
        if len(protocol_ids) != 3:
            raise EvaluationLayersError(
                "each evaluation layer requires an independent protocol identity"
            )
        if len(evidence_ids) != 3:
            raise EvaluationLayersError(
                "one evidence bundle cannot substitute for another layer"
            )
        if len(result_ids) != 3:
            raise EvaluationLayersError(
                "one authority result cannot substitute for another layer"
            )

        ordered = tuple(by_layer[name] for name in _LAYER_ORDER)
        object.__setattr__(self, "candidate_id", candidate_id)
        object.__setattr__(self, "exact_build_sha", exact_build_sha)
        object.__setattr__(self, "candidate_selected_at", selected)
        object.__setattr__(self, "layers", ordered)

    @property
    def layer_statuses(self) -> Mapping[str, str]:
        return MappingProxyType(
            {item.layer: item.status for item in self.layers}
        )

    @property
    def historical_status(self) -> str:
        statuses = tuple(
            item.status
            for item in self.layers
            if item.layer in _HISTORICAL_LAYERS
        )
        if "FAIL" in statuses:
            return "FAIL"
        if "INCONCLUSIVE" in statuses:
            return "INCONCLUSIVE"
        return "PASS"

    @property
    def forward_status(self) -> str:
        return next(
            item.status for item in self.layers if item.layer == FORWARD_PAPER
        )

    @property
    def evaluation_status(self) -> str:
        statuses = tuple(item.status for item in self.layers)
        if "FAIL" in statuses:
            return "FAIL"
        if "INCONCLUSIVE" in statuses:
            return "INCONCLUSIVE"
        return "PASS"

    @property
    def all_three_layers_passed(self) -> bool:
        return self.evaluation_status == "PASS"

    @property
    def forward_required_for_complete_evaluation(self) -> bool:
        return True

    @property
    def economic_edge_status(self) -> str:
        return "NOT_ESTABLISHED"

    @property
    def digest(self) -> str:
        return _digest(
            {
                "schema_version": 1,
                "candidate_id": self.candidate_id,
                "exact_build_sha": self.exact_build_sha,
                "candidate_selected_at": self.candidate_selected_at,
                "layers": tuple(item.digest for item in self.layers),
                "evaluation_status": self.evaluation_status,
                "economic_edge_status": self.economic_edge_status,
            }
        )


def compose_three_layer_evaluation(
    *,
    candidate_id: str,
    exact_build_sha: str,
    candidate_selected_at: str,
    layers: Sequence[EvaluationLayerReceipt],
) -> ThreeLayerEvaluation:
    """Compose exactly A+B+C without allowing cross-layer substitution."""

    if isinstance(layers, (str, bytes)):
        raise TypeError("layers must be a sequence of receipts")
    return ThreeLayerEvaluation(
        candidate_id=candidate_id,
        exact_build_sha=exact_build_sha,
        candidate_selected_at=candidate_selected_at,
        layers=tuple(layers),
    )
