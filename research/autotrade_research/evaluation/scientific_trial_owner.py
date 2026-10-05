"""Canonical ScientificRegistry trial-owner binding for WP-36 gates.

The base gate evaluator intentionally cannot manufacture scientific ownership
from caller-provided booleans. This module resolves one GateProfile to exactly
one preregistered ScientificRegistry protocol by scanning immutable protocol
payloads for a profile id + full profile digest binding. The caller never
supplies which protocol should win after seeing trial outcomes.

This does not create terminal scientific PASS by itself. It adds one
registry-owned trial-population check around the existing gate decision while
the remaining semantic owners stay independently fail-closed.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
from types import MappingProxyType
from typing import Any

from .gates import (
    EvaluationEvidence,
    GateDecision,
    GateProfile,
    _decimal_text,
    _detached_gate_input,
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
_REGISTRY_CRITICAL_INSTANCE_OVERRIDES = frozenset(
    {"_connect", "trial_completeness_evidence"}
)
_REGISTRY_CONNECT_DESCRIPTOR = ScientificRegistry.__dict__["_connect"]
_REGISTRY_PATH_DESCRIPTOR = ScientificRegistry.__dict__["path"]
_REGISTRY_TRIAL_COMPLETENESS_DESCRIPTOR = ScientificRegistry.__dict__[
    "trial_completeness_evidence"
]


def _assert_registry_class_authority_intact() -> None:
    expected = {
        "_connect": _REGISTRY_CONNECT_DESCRIPTOR,
        "path": _REGISTRY_PATH_DESCRIPTOR,
        "trial_completeness_evidence": _REGISTRY_TRIAL_COMPLETENESS_DESCRIPTOR,
    }
    for name, descriptor in expected.items():
        if ScientificRegistry.__dict__.get(name) is not descriptor:
            raise TypeError(
                f"ScientificRegistry class authority descriptor changed: {name}"
            )


def _assert_registry_dispatch_unshadowed(registry: ScientificRegistry) -> None:
    _assert_registry_class_authority_intact()
    if type(registry) is not ScientificRegistry:
        raise TypeError("registry must be exact ScientificRegistry")
    shadowed = _REGISTRY_CRITICAL_INSTANCE_OVERRIDES.intersection(registry.__dict__)
    if shadowed:
        raise TypeError(
            "ScientificRegistry authority methods must not be instance-shadowed: "
            + ", ".join(sorted(shadowed))
        )


def _registry_authority_view(registry: ScientificRegistry) -> ScientificRegistry:
    """Freeze one caller registry onto one pathlib-owned DB path for this cut."""

    _assert_registry_dispatch_unshadowed(registry)
    path = _REGISTRY_PATH_DESCRIPTOR.__get__(registry, ScientificRegistry)
    if type(path) is not type(Path()) or not path.is_absolute():
        raise TypeError(
            "ScientificRegistry path must be a frozen absolute pathlib path"
        )
    authority = object.__new__(ScientificRegistry)
    object.__setattr__(authority, "_path", path)
    return authority


def _canonical_gate_profile_authority_view(profile: GateProfile) -> GateProfile:
    """Detach and revalidate one complete GateProfile value for trust use."""

    if type(profile) is not GateProfile:
        raise TypeError("profile must be exact GateProfile")

    exact_text: dict[str, str] = {}
    for name in (
        "profile_id",
        "primary_baseline_id",
        "selection_correction",
    ):
        value = object.__getattribute__(profile, name)
        if type(value) is not str:
            raise TypeError(f"{name} must remain exact built-in text")
        exact_text[name] = value

    baseline_ids = object.__getattribute__(profile, "baseline_ids")
    required_regimes = object.__getattribute__(profile, "required_regimes")
    for name, values in (
        ("baseline_ids", baseline_ids),
        ("required_regimes", required_regimes),
    ):
        if type(values) is not tuple or any(
            type(value) is not str for value in values
        ):
            raise TypeError(
                f"{name} must remain an exact tuple of built-in text"
            )

    return GateProfile(
        profile_id=exact_text["profile_id"],
        minimum_net_advantage=object.__getattribute__(
            profile, "minimum_net_advantage"
        ),
        max_drawdown=object.__getattribute__(profile, "max_drawdown"),
        max_adverse_cost_loss=object.__getattribute__(
            profile, "max_adverse_cost_loss"
        ),
        min_power=object.__getattribute__(profile, "min_power"),
        primary_baseline_id=exact_text["primary_baseline_id"],
        baseline_ids=baseline_ids,
        selection_correction=exact_text["selection_correction"],
        max_trials=object.__getattribute__(profile, "max_trials"),
        required_regimes=required_regimes,
        require_complete_trials=object.__getattribute__(
            profile, "require_complete_trials"
        ),
        require_causal_audit=object.__getattribute__(
            profile, "require_causal_audit"
        ),
        require_financial_invariants=object.__getattribute__(
            profile, "require_financial_invariants"
        ),
        require_untouched_holdout=object.__getattribute__(
            profile, "require_untouched_holdout"
        ),
        require_walk_forward=object.__getattribute__(
            profile, "require_walk_forward"
        ),
    )


def gate_profile_subject_payload(profile: GateProfile) -> dict[str, object]:
    """Return the complete immutable GateProfile subject payload.

    Every dataclass field that can change evaluation authority is included.
    Sequence order is preserved exactly because GateProfile itself preserves it;
    the subject digest must not silently canonicalize two distinct exact values
    into one identity.

    Frozen dataclasses are not a trust boundary in Python: object.__setattr__
    can still mutate an instance after construction. Revalidate and detach the
    complete profile at the moment the scientific subject is hashed.
    """

    canonical = _canonical_gate_profile_authority_view(profile)
    return {
        "schema_version": "wp36-gate-profile-subject-v1",
        "profile_id": canonical.profile_id,
        "minimum_net_advantage": _decimal_text(canonical.minimum_net_advantage),
        "max_drawdown": _decimal_text(canonical.max_drawdown),
        "max_adverse_cost_loss": _decimal_text(canonical.max_adverse_cost_loss),
        "min_power": _decimal_text(canonical.min_power),
        "primary_baseline_id": canonical.primary_baseline_id,
        "baseline_ids": list(canonical.baseline_ids),
        "selection_correction": canonical.selection_correction,
        "max_trials": canonical.max_trials,
        "required_regimes": list(canonical.required_regimes),
        "require_complete_trials": canonical.require_complete_trials,
        "require_causal_audit": canonical.require_causal_audit,
        "require_financial_invariants": canonical.require_financial_invariants,
        "require_untouched_holdout": canonical.require_untouched_holdout,
        "require_walk_forward": canonical.require_walk_forward,
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
    complete_required: bool

    @property
    def authoritative(self) -> bool:
        return (
            self.population_matches_gate_evidence
            and self.completion_matches_gate_evidence
            and (not self.complete_required or self.trial_evidence.complete)
        )

    @property
    def digest(self) -> str:
        return _hash(
            {
                "binding_digest": self.binding.digest,
                "trial_completeness_digest": self.trial_evidence.digest,
                "population_matches_gate_evidence": self.population_matches_gate_evidence,
                "completion_matches_gate_evidence": self.completion_matches_gate_evidence,
                "complete_required": self.complete_required,
                "authoritative": self.authoritative,
            }
        )


def _registered_protocol_rows(registry: ScientificRegistry) -> tuple[dict[str, Any], ...]:
    authority = _registry_authority_view(registry)
    with _REGISTRY_CONNECT_DESCRIPTOR(authority) as con:
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

    Matching is derived from the full append-only registry population. A
    caller cannot select a favorable protocol after results. Duplicate profile
    bindings, id/digest rebinds and malformed bindings fail closed.
    """

    canonical_profile = _canonical_gate_profile_authority_view(profile)
    profile_id = canonical_profile.profile_id
    profile_digest = gate_profile_subject_digest(canonical_profile)
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
    """Resolve one stable registry-owned protocol + trial-population cut.

    ``BEGIN IMMEDIATE`` holds SQLite's writer reservation while the existing
    canonical protocol-population resolver and #1426 trial snapshot perform
    their read transactions. That prevents a concurrent registration/trial
    writer from committing between the two authority reads and producing a
    mixed profile-owner/trial-population composition.
    """

    authority = _registry_authority_view(registry)
    canonical_profile = _canonical_gate_profile_authority_view(profile)
    canonical_evidence = _detached_gate_input(evidence, EvaluationEvidence)
    with _REGISTRY_CONNECT_DESCRIPTOR(authority) as authority_guard:
        authority_guard.execute("BEGIN IMMEDIATE")
        binding = resolve_gate_profile_protocol_binding(
            registry=authority,
            profile=canonical_profile,
        )
        trial_evidence = _REGISTRY_TRIAL_COMPLETENESS_DESCRIPTOR(
            authority,
            binding.protocol_id,
        )
        if trial_evidence.protocol_hash != binding.protocol_hash:
            raise ProtocolViolation(
                "trial completeness snapshot does not match bound protocol hash"
            )
        if trial_evidence.trial_budget > canonical_profile.max_trials:
            raise ProtocolViolation(
                "bound scientific protocol trial budget exceeds gate profile max_trials"
            )
    population_matches = (
        canonical_evidence.trials_attempted is not None
        and canonical_evidence.trials_attempted == trial_evidence.recorded_trials
    )
    completion_matches = (
        canonical_evidence.trial_log_complete is not None
        and canonical_evidence.trial_log_complete == trial_evidence.complete
    )
    return ScientificTrialOwnerEvidence(
        binding=binding,
        trial_evidence=trial_evidence,
        population_matches_gate_evidence=population_matches,
        completion_matches_gate_evidence=completion_matches,
        complete_required=canonical_profile.require_complete_trials,
    )


def evaluate_gates_with_scientific_trial_owner(
    profile: GateProfile,
    evidence: EvaluationEvidence,
    *,
    scientific_registry: ScientificRegistry,
    **gate_kwargs: object,
) -> GateDecision:
    """Evaluate gates plus canonical registry-owned trial completeness.

    Missing owner identity is INCONCLUSIVE. Conflicting/corrupt identity,
    population mismatch, incomplete required trials or a gate/profile budget
    contradiction are FAIL. Existing gate FAIL remains FAIL and existing
    semantic-owner INCONCLUSIVE remains unavailable for terminal PASS.
    """

    canonical_profile = _canonical_gate_profile_authority_view(profile)
    canonical_evidence = _detached_gate_input(evidence, EvaluationEvidence)
    _assert_registry_dispatch_unshadowed(scientific_registry)
    base = evaluate_gates(canonical_profile, canonical_evidence, **gate_kwargs)
    checks = dict(base.checks)
    provenance = dict(base.provenance or {})
    reasons = list(base.reasons)
    try:
        owner = resolve_scientific_trial_owner(
            registry=scientific_registry,
            profile=canonical_profile,
            evidence=canonical_evidence,
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
        elif owner.complete_required and not owner.trial_evidence.complete:
            checks["scientific_trial_owner"] = "FAIL"
            reasons.append(
                "bound scientific protocol has not exhausted its immutable trial budget"
            )
            status = "FAIL"
        elif not owner.authoritative:
            checks["scientific_trial_owner"] = "FAIL"
            reasons.append("scientific trial owner evidence is not authoritative")
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
