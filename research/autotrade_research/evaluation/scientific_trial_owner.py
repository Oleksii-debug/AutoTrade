"""Canonical ScientificRegistry trial-owner binding for WP-36 gates.

The base gate evaluator intentionally cannot manufacture scientific ownership
from caller-provided booleans.  This module resolves one GateProfile to exactly
one preregistered ScientificRegistry protocol by scanning immutable protocol
payloads for a profile id + profile digest binding.  The caller never supplies
which protocol should win after seeing trial outcomes.

This does not create terminal scientific PASS by itself.  It adds one
registry-owned trial-population check around the existing gate decision while
the remaining semantic owners stay independently fail-closed.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from types import MappingProxyType
from typing import Any

from .gates import (
    EvaluationEvidence,
    GateDecision,
    GateProfile,
    _decimal_text,
    evaluate_gates,
)
from ..science.registry import (
    ProtocolViolation,
    ScientificRegistry,
    TrialCompletenessEvidence,
    _SHA256_RE,
    _canonical,
    _hash,
)


_GATE_PROFILE_ID_FIELD = "gate_profile_id"
_GATE_PROFILE_DIGEST_FIELD = "gate_profile_digest"


def gate_profile_subject_payload(profile: GateProfile) -> dict[str, object]:
    """Return the complete immutable GateProfile subject payload."""

    if type(profile) is not GateProfile:
        raise TypeError("profile must be exact GateProfile")
    return {
        "schema_version": "wp36-gate-profile-subject-v1",
        "profile_id": profile.profile_id,
        "minimum_net_advantage": _decimal_text(profile.minimum_net_advantage),
        "max_drawdown": _decimal_text(profile.max_drawdown),
        "max_adverse_cost_loss": _decimal_text(profile.max_adverse_cost_loss),
        "min_power": _decimal_text(profile.min_power),
        "primary_baseline_id": profile.primary_baseline_id,
        "baseline_ids": list(profile.baseline_ids),
        "selection_correction": profile.selection_correction,
        "max_trials": profile.max_trials,
        "required_regimes": sorted(profile.required_regimes),
        "require_complete_trials": profile.require_complete_trials,
        "require_causal_audit": profile.require_causal_audit,
        "require_financial_invariants": profile.require_financial_invariants,
    }


def gate_profile_subject_digest(profile: GateProfile) -> str:
    payload = gate_profile_subject_payload(profile)
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + sha256(encoded).hexdigest()


@dataclass(frozen=True)
class GateProfileProtocolBinding:
    profile_id: str
    profile_digest: str
    protocol_id: str
    protocol_hash: str

    @property
    def digest(self) -> str:
        return _hash(
            {
                "profile_id": self.profile_id,
                "profile_digest": self.profile_digest,
                "protocol_id": self.protocol_id,
                "protocol_hash": self.protocol_hash,
            }
        )


@dataclass(frozen=True)
class ScientificTrialOwnerEvidence:
    binding: GateProfileProtocolBinding
    trial_evidence: TrialCompletenessEvidence
    population_matches_gate_evidence: bool
    completion_matches_gate_evidence: bool

    @property
    def authoritative(self) -> bool:
        return (
            self.population_matches_gate_evidence
            and self.completion_matches_gate_evidence
            and self.trial_evidence.complete
        )

    @property
    def digest(self) -> str:
        return _hash(
            {
                "binding_digest": self.binding.digest,
                "trial_completeness_digest": self.trial_evidence.digest,
                "population_matches_gate_evidence": self.population_matches_gate_evidence,
                "completion_matches_gate_evidence": self.completion_matches_gate_evidence,
                "authoritative": self.authoritative,
            }
        )


def _registered_protocol_rows(registry: ScientificRegistry) -> tuple[dict[str, Any], ...]:
    if type(registry) is not ScientificRegistry:
        raise TypeError("registry must be exact ScientificRegistry")
    with registry._connect() as con:
        con.execute("BEGIN")
        rows = con.execute(
            "SELECT protocol_id,protocol_hash,payload_json "
            "FROM protocols ORDER BY protocol_id"
        ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        try:
            payload = json.loads(row["payload_json"])
        except json.JSONDecodeError as error:
            raise ProtocolViolation("registered protocol payload is corrupt") from error
        if (
            type(payload) is not dict
            or _canonical(payload) != row["payload_json"]
            or _hash(payload) != row["protocol_hash"]
        ):
            raise ProtocolViolation("registered protocol integrity mismatch")
        result.append(
            {
                "protocol_id": row["protocol_id"],
                "protocol_hash": row["protocol_hash"],
                "payload": payload,
            }
        )
    return tuple(result)


def resolve_gate_profile_protocol_binding(
    *,
    registry: ScientificRegistry,
    profile: GateProfile,
) -> GateProfileProtocolBinding:
    """Resolve exactly one immutable protocol owner for one exact GateProfile.

    Matching is derived from the full append-only registry population.  A
    caller cannot select a favorable protocol after results.  Duplicate profile
    bindings, id/digest rebinds and malformed bindings fail closed.
    """

    profile_id = profile.profile_id
    profile_digest = gate_profile_subject_digest(profile)
    candidates: list[GateProfileProtocolBinding] = []
    for row in _registered_protocol_rows(registry):
        payload = row["payload"]
        bound_id = payload.get(_GATE_PROFILE_ID_FIELD)
        bound_digest = payload.get(_GATE_PROFILE_DIGEST_FIELD)
        binding_present = (
            _GATE_PROFILE_ID_FIELD in payload
            or _GATE_PROFILE_DIGEST_FIELD in payload
        )
        if not binding_present:
            continue
        if type(bound_id) is not str or not bound_id or bound_id != bound_id.strip():
            raise ProtocolViolation(
                "registered gate-profile binding has invalid gate_profile_id"
            )
        if (
            type(bound_digest) is not str
            or _SHA256_RE.fullmatch(bound_digest) is None
        ):
            raise ProtocolViolation(
                "registered gate-profile binding has invalid gate_profile_digest"
            )
        if bound_id == profile_id and bound_digest != profile_digest:
            raise ProtocolViolation(
                "gate profile id is rebound to a different immutable profile digest"
            )
        if bound_digest == profile_digest and bound_id != profile_id:
            raise ProtocolViolation(
                "gate profile digest is rebound under a different profile id"
            )
        if bound_id != profile_id:
            continue
        candidates.append(
            GateProfileProtocolBinding(
                profile_id=profile_id,
                profile_digest=profile_digest,
                protocol_id=row["protocol_id"],
                protocol_hash=row["protocol_hash"],
            )
        )
    if not candidates:
        raise KeyError(profile_id)
    if len(candidates) != 1:
        raise ProtocolViolation(
            "gate profile is bound to multiple immutable scientific protocols"
        )
    return candidates[0]


def resolve_scientific_trial_owner(
    *,
    registry: ScientificRegistry,
    profile: GateProfile,
    evidence: EvaluationEvidence,
) -> ScientificTrialOwnerEvidence:
    """Resolve one registry-owned trial population against gate observations."""

    if type(evidence) is not EvaluationEvidence:
        raise TypeError("evidence must be exact EvaluationEvidence")
    binding = resolve_gate_profile_protocol_binding(
        registry=registry,
        profile=profile,
    )
    trial_evidence = registry.trial_completeness_evidence(binding.protocol_id)
    if trial_evidence.protocol_hash != binding.protocol_hash:
        raise ProtocolViolation(
            "trial completeness snapshot does not match bound protocol hash"
        )
    if trial_evidence.trial_budget > profile.max_trials:
        raise ProtocolViolation(
            "bound scientific protocol trial budget exceeds gate profile max_trials"
        )
    population_matches = (
        evidence.trials_attempted is not None
        and evidence.trials_attempted == trial_evidence.recorded_trials
    )
    completion_matches = (
        evidence.trial_log_complete is not None
        and evidence.trial_log_complete == trial_evidence.complete
    )
    return ScientificTrialOwnerEvidence(
        binding=binding,
        trial_evidence=trial_evidence,
        population_matches_gate_evidence=population_matches,
        completion_matches_gate_evidence=completion_matches,
    )


def evaluate_gates_with_scientific_trial_owner(
    profile: GateProfile,
    evidence: EvaluationEvidence,
    *,
    scientific_registry: ScientificRegistry,
    **gate_kwargs: object,
) -> GateDecision:
    """Evaluate gates plus canonical registry-owned trial completeness.

    Missing owner identity is INCONCLUSIVE.  Conflicting/corrupt identity,
    population mismatch, incomplete required trials or a gate/profile budget
    contradiction are FAIL.  Existing gate FAIL remains FAIL and existing
    semantic-owner INCONCLUSIVE remains unavailable for terminal PASS.
    """

    base = evaluate_gates(profile, evidence, **gate_kwargs)
    checks = dict(base.checks)
    provenance = dict(base.provenance or {})
    reasons = list(base.reasons)
    try:
        owner = resolve_scientific_trial_owner(
            registry=scientific_registry,
            profile=profile,
            evidence=evidence,
        )
    except KeyError:
        checks["scientific_trial_owner"] = "INCONCLUSIVE"
        reasons.append(
            "gate profile has no unique preregistered ScientificRegistry protocol owner"
        )
        status = "FAIL" if base.status == "FAIL" else "INCONCLUSIVE"
    except ProtocolViolation as error:
        checks["scientific_trial_owner"] = "FAIL"
        reasons.append(str(error))
        status = "FAIL"
    else:
        provenance.update(
            {
                "gate_profile_subject_digest": owner.binding.profile_digest,
                "scientific_protocol_id": owner.binding.protocol_id,
                "scientific_protocol_hash": owner.binding.protocol_hash,
                "trial_completeness_digest": owner.trial_evidence.digest,
                "scientific_trial_owner_digest": owner.digest,
            }
        )
        if not owner.population_matches_gate_evidence:
            checks["scientific_trial_owner"] = "FAIL"
            reasons.append(
                "gate trials_attempted does not match registry-owned trial population"
            )
            status = "FAIL"
        elif not owner.completion_matches_gate_evidence:
            checks["scientific_trial_owner"] = "FAIL"
            reasons.append(
                "gate trial_log_complete does not match registry-owned completeness"
            )
            status = "FAIL"
        elif profile.require_complete_trials and not owner.trial_evidence.complete:
            checks["scientific_trial_owner"] = "FAIL"
            reasons.append(
                "bound scientific protocol has not exhausted its immutable trial budget"
            )
            status = "FAIL"
        else:
            checks["scientific_trial_owner"] = "PASS"
            status = base.status
    return GateDecision(
        status=status,
        reasons=tuple(reasons),
        checks=MappingProxyType(checks),
        provenance=MappingProxyType(provenance) if provenance else None,
    )
