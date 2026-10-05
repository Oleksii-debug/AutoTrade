"""Preregistered cost-projection provenance for WP-63 terminal composition.

This module authenticates only the immutable rule identity and exact component
coverage selected by the registered ablation value policy.  It deliberately does
not mint a numeric cost and does not turn a provider economic prefix into the
complete cost composite.  A later terminal resolver must compose this rule with
canonical owner evidence for every required component at the frozen causal cut.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import re
from uuid import UUID

import autotrade_research.evaluation.ablation as _ablation
from autotrade_research.artifacts.store import ArtifactStore
from autotrade_research.evaluation.ablation import AblationQualificationAuthority
from autotrade_research.io.strict_json import strict_json_loads
from autotrade_research.memory.episodes import MemoryIntegrityError
from autotrade_research.science.registry import ScientificRegistry


_RESOLVE_POLICY_CONTEXT = _ablation._registered_policy_context
_VALUE_POLICY = ScientificRegistry.ablation_value_policy
_VALUE_POLICY_RAW = ScientificRegistry.__dict__["ablation_value_policy"]
_REGISTRY_CONNECT_RAW = ScientificRegistry.__dict__["_connect"]
_ARTIFACT_READ = ArtifactStore.read_authenticated_snapshot
_ARTIFACT_READ_RAW = ArtifactStore.__dict__["read_authenticated_snapshot"]
_STRICT_JSON_LOADS = strict_json_loads
_PROJECTION_MEDIA_TYPE = "application/vnd.autotrade.ablation-value-projection+json"
_EXPECTED_OWNER = "CANONICAL_ABLATION_COST_COMPOSITE"
_EXPECTED_RULE = "complete-after-cost-attribution-v1"
_REQUIRED_COMPONENTS = (
    "commission",
    "spread",
    "slippage",
    "financing",
    "funding",
    "borrow",
    "market_data",
    "model_compute",
    "infrastructure",
    "tax_estimate",
)
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_REQUIRED_DESCRIPTOR_FIELDS = {
    "schema_version",
    "projection_kind",
    "value_unit",
    "owner_authority",
    "rule_id",
    "cost_components",
}


def _digest(value: object, *, name: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise MemoryIntegrityError(
            f"{name} must be an exact canonical sha256 digest"
        )
    return value


def _text(value: object, *, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise MemoryIntegrityError(f"{name} must be exact canonical non-empty text")
    return value


def _projection_ref(reference: object) -> tuple[str, str]:
    value = _text(reference, name="cost_projection_ref")
    if not value.startswith("artifact:") or value.count("@") != 1:
        raise MemoryIntegrityError(
            "cost_projection_ref must be one immutable artifact reference"
        )
    artifact_id, digest = value.removeprefix("artifact:").split("@", 1)
    try:
        canonical_id = str(UUID(artifact_id))
    except (TypeError, ValueError, AttributeError) as error:
        raise MemoryIntegrityError(
            "cost_projection_ref artifact identity is invalid"
        ) from error
    if canonical_id != artifact_id:
        raise MemoryIntegrityError(
            "cost_projection_ref artifact identity is not canonical"
        )
    return artifact_id, _digest(digest, name="cost_projection_ref digest")


def _assert_executables() -> None:
    if ScientificRegistry.__dict__.get("ablation_value_policy") is not _VALUE_POLICY_RAW:
        raise MemoryIntegrityError(
            "scientific registry value-policy executable changed after composition"
        )
    if ScientificRegistry.__dict__.get("_connect") is not _REGISTRY_CONNECT_RAW:
        raise MemoryIntegrityError(
            "scientific registry persistence executable changed after composition"
        )
    if ArtifactStore.__dict__.get("read_authenticated_snapshot") is not _ARTIFACT_READ_RAW:
        raise MemoryIntegrityError(
            "artifact authenticated-read executable changed after composition"
        )


def _descriptor(
    artifact_store: ArtifactStore,
    reference: object,
    *,
    value_unit: str,
) -> tuple[str, str, str, str, tuple[str, ...]]:
    artifact_id, expected_digest = _projection_ref(reference)
    manifest, data = _ARTIFACT_READ(artifact_store, artifact_id)
    if type(manifest) is not dict or type(data) is not bytes:
        raise MemoryIntegrityError(
            "cost projection read returned non-canonical snapshot values"
        )
    if manifest.get("sha256") != expected_digest:
        raise MemoryIntegrityError("registered cost projection digest mismatch")
    if manifest.get("media_type") != _PROJECTION_MEDIA_TYPE:
        raise MemoryIntegrityError(
            "registered cost projection media type is not qualified"
        )
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise MemoryIntegrityError(
            "registered cost projection must be UTF-8 JSON"
        ) from error
    try:
        payload = _STRICT_JSON_LOADS(text)
    except ValueError as error:
        raise MemoryIntegrityError(
            "registered cost projection JSON is invalid"
        ) from error
    if type(payload) is not dict or set(payload) != _REQUIRED_DESCRIPTOR_FIELDS:
        raise MemoryIntegrityError(
            "registered cost projection schema is not canonical"
        )
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    if canonical != text:
        raise MemoryIntegrityError(
            "registered cost projection JSON must be canonical"
        )
    if payload.get("schema_version") != 1:
        raise MemoryIntegrityError(
            "registered cost projection schema version is unsupported"
        )
    if payload.get("projection_kind") != "COST":
        raise MemoryIntegrityError("registered cost projection kind mismatch")
    if payload.get("value_unit") != value_unit:
        raise MemoryIntegrityError("registered cost projection value unit mismatch")
    owner = _text(payload.get("owner_authority"), name="cost owner_authority")
    if owner != _EXPECTED_OWNER:
        raise MemoryIntegrityError(
            "registered cost projection owner authority mismatch"
        )
    rule = _text(payload.get("rule_id"), name="cost rule_id")
    if rule != _EXPECTED_RULE:
        raise MemoryIntegrityError("registered cost projection rule is unsupported")
    raw_components = payload.get("cost_components")
    if type(raw_components) is not list or any(
        type(item) is not str or not item or item != item.strip()
        for item in raw_components
    ):
        raise MemoryIntegrityError(
            "registered cost projection component coverage is invalid"
        )
    components = tuple(raw_components)
    if components != _REQUIRED_COMPONENTS:
        raise MemoryIntegrityError(
            "registered cost projection component coverage is incomplete"
        )
    return artifact_id, expected_digest, owner, rule, components


def _binding_material(
    *,
    protocol_digest: str,
    value_unit: str,
    projection_artifact_id: str,
    projection_artifact_digest: str,
    owner_authority: str,
    rule_id: str,
    cost_components: tuple[str, ...],
    fx_valuation_ref: str | None,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "protocol_digest": protocol_digest,
        "value_unit": value_unit,
        "projection_artifact_id": projection_artifact_id,
        "projection_artifact_digest": projection_artifact_digest,
        "owner_authority": owner_authority,
        "rule_id": rule_id,
        "cost_components": list(cost_components),
        "fx_valuation_ref": fx_valuation_ref,
    }


def _binding_digest(material: dict[str, object]) -> str:
    raw = json.dumps(
        material,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + sha256(raw).hexdigest()


@dataclass(frozen=True)
class ResolvedAblationCostRuleProvenance:
    """Exact preregistered cost rule identity; never a numeric cost operand."""

    protocol_digest: str
    value_unit: str
    projection_artifact_id: str
    projection_artifact_digest: str
    owner_authority: str
    rule_id: str
    cost_components: tuple[str, ...]
    fx_valuation_ref: str | None
    binding_digest: str

    def __post_init__(self) -> None:
        _digest(self.protocol_digest, name="protocol_digest")
        _text(self.value_unit, name="value_unit")
        try:
            if str(UUID(self.projection_artifact_id)) != self.projection_artifact_id:
                raise ValueError
        except (TypeError, ValueError, AttributeError) as error:
            raise MemoryIntegrityError(
                "projection_artifact_id must be a canonical UUID"
            ) from error
        _digest(
            self.projection_artifact_digest,
            name="projection_artifact_digest",
        )
        if self.owner_authority != _EXPECTED_OWNER:
            raise MemoryIntegrityError("cost owner authority is not canonical")
        if self.rule_id != _EXPECTED_RULE:
            raise MemoryIntegrityError("cost rule identity is not canonical")
        if type(self.cost_components) is not tuple:
            raise MemoryIntegrityError("cost_components must be an exact tuple")
        if self.cost_components != _REQUIRED_COMPONENTS:
            raise MemoryIntegrityError(
                "cost rule component coverage is not canonical"
            )
        if self.fx_valuation_ref is not None:
            _text(self.fx_valuation_ref, name="fx_valuation_ref")
        _digest(self.binding_digest, name="binding_digest")
        ResolvedAblationCostRuleProvenance.verify_integrity(self)

    def verify_integrity(self) -> None:
        material = _binding_material(
            protocol_digest=self.protocol_digest,
            value_unit=self.value_unit,
            projection_artifact_id=self.projection_artifact_id,
            projection_artifact_digest=self.projection_artifact_digest,
            owner_authority=self.owner_authority,
            rule_id=self.rule_id,
            cost_components=self.cost_components,
            fx_valuation_ref=self.fx_valuation_ref,
        )
        if self.binding_digest != _binding_digest(material):
            raise MemoryIntegrityError(
                "cost rule provenance digest does not match canonical material"
            )


def resolve_ablation_cost_rule_provenance(
    authority: AblationQualificationAuthority,
) -> ResolvedAblationCostRuleProvenance:
    """Authenticate the preregistered complete-cost projection rule only."""

    if type(authority) is not AblationQualificationAuthority:
        raise TypeError("authority must be exact AblationQualificationAuthority")
    _assert_executables()
    (
        registry,
        _memory,
        artifacts,
        protocol_id,
        protocol_hash,
        _source_revision,
        _causal_cutoff,
        _permission_classes,
        _task,
        _instrument_family,
    ) = _RESOLVE_POLICY_CONTEXT(authority)
    policy = _VALUE_POLICY(registry, protocol_id)
    if policy.protocol_id != protocol_id or policy.protocol_hash != protocol_hash:
        raise MemoryIntegrityError(
            "registered cost policy does not match qualification binding"
        )
    value_unit = _text(policy.value_unit, name="value_unit")
    artifact_id, artifact_digest, owner, rule, components = _descriptor(
        artifacts,
        policy.cost_projection_ref,
        value_unit=value_unit,
    )
    fx_ref = policy.fx_valuation_ref
    if fx_ref is not None:
        _text(fx_ref, name="fx_valuation_ref")
    post_context = _RESOLVE_POLICY_CONTEXT(authority)
    if (
        post_context[0] is not registry
        or post_context[2] is not artifacts
        or post_context[3] != protocol_id
        or post_context[4] != protocol_hash
    ):
        raise MemoryIntegrityError(
            "ablation authority binding changed during cost rule resolution"
        )
    material = _binding_material(
        protocol_digest=protocol_hash,
        value_unit=value_unit,
        projection_artifact_id=artifact_id,
        projection_artifact_digest=artifact_digest,
        owner_authority=owner,
        rule_id=rule,
        cost_components=components,
        fx_valuation_ref=fx_ref,
    )
    return ResolvedAblationCostRuleProvenance(
        protocol_digest=protocol_hash,
        value_unit=value_unit,
        projection_artifact_id=artifact_id,
        projection_artifact_digest=artifact_digest,
        owner_authority=owner,
        rule_id=rule,
        cost_components=components,
        fx_valuation_ref=fx_ref,
        binding_digest=_binding_digest(material),
    )


def reverify_ablation_cost_rule_provenance(
    authority: AblationQualificationAuthority,
    evidence: ResolvedAblationCostRuleProvenance,
) -> ResolvedAblationCostRuleProvenance:
    if type(evidence) is not ResolvedAblationCostRuleProvenance:
        raise TypeError(
            "evidence must be exact ResolvedAblationCostRuleProvenance"
        )
    ResolvedAblationCostRuleProvenance.verify_integrity(evidence)
    resolved = resolve_ablation_cost_rule_provenance(authority)
    if resolved != evidence:
        raise MemoryIntegrityError(
            "cost rule provenance does not match canonical authority evidence"
        )
    return resolved
