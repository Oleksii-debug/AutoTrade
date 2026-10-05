"""Source-owned utility projection rule identity for terminal ablation evidence.

This module deliberately does *not* mint utility values.  It owns only the
immutable, source-controlled rule descriptor that a preregistered ablation value
policy may reference.  Actual utility operands must still come from the separate
canonical reconciled-outcome owner at the frozen evaluation cut.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import re
from uuid import UUID, uuid5

from autotrade_research.artifacts.store import ArtifactStore


UTILITY_PROJECTION_MEDIA_TYPE = (
    "application/vnd.autotrade.ablation-value-projection+json"
)
UTILITY_PROJECTION_KIND = "UTILITY"
UTILITY_OWNER_AUTHORITY = "CANONICAL_RECONCILED_OUTCOME"
UTILITY_RULE_ID = "reconciled-outcome-net-value-v1"
UTILITY_RULE_SCHEMA_VERSION = 1

# Fixed source namespace: descriptor identity is a deterministic function of the
# installed rule + value unit, never a caller-selected UUID.
_UTILITY_RULE_NAMESPACE = UUID("28fd8758-7740-5d76-95d7-c703377ce6d0")
_VALUE_UNIT_RE = re.compile(r"[A-Z0-9][A-Z0-9._:-]{0,63}")
_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}")


class UtilityProjectionRuleError(ValueError):
    """Raised when immutable utility-rule evidence is not canonical."""


def _value_unit(value: object) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or value != value.upper()
        or _VALUE_UNIT_RE.fullmatch(value) is None
    ):
        raise UtilityProjectionRuleError(
            "value_unit must be canonical exact uppercase unit text"
        )
    return value


def _canonical_payload(value_unit: str) -> bytes:
    payload = {
        "cost_components": [],
        "owner_authority": UTILITY_OWNER_AUTHORITY,
        "projection_kind": UTILITY_PROJECTION_KIND,
        "rule_id": UTILITY_RULE_ID,
        "schema_version": UTILITY_RULE_SCHEMA_VERSION,
        "value_unit": value_unit,
    }
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _rule_artifact_id(value_unit: str) -> str:
    identity = (
        f"autotrade.utility-projection-rule.v1|{UTILITY_RULE_ID}|{value_unit}"
    )
    return str(uuid5(_UTILITY_RULE_NAMESPACE, identity))


def _digest(data: bytes) -> str:
    return "sha256:" + sha256(data).hexdigest()


@dataclass(frozen=True, slots=True)
class UtilityProjectionRule:
    """Exact source-owned descriptor identity; not utility/economic evidence."""

    value_unit: str
    artifact_id: str
    sha256: str
    payload: bytes

    def __post_init__(self) -> None:
        unit = _value_unit(self.value_unit)
        expected_payload = _canonical_payload(unit)
        expected_id = _rule_artifact_id(unit)
        expected_digest = _digest(expected_payload)
        if type(self.artifact_id) is not str or self.artifact_id != expected_id:
            raise UtilityProjectionRuleError(
                "utility projection artifact identity is not source-owned"
            )
        if type(self.sha256) is not str or self.sha256 != expected_digest:
            raise UtilityProjectionRuleError(
                "utility projection digest is not source-owned"
            )
        if type(self.payload) is not bytes or self.payload != expected_payload:
            raise UtilityProjectionRuleError(
                "utility projection payload is not source-owned"
            )

    @property
    def immutable_reference(self) -> str:
        return f"artifact:{self.artifact_id}@{self.sha256}"


def build_utility_projection_rule(value_unit: str) -> UtilityProjectionRule:
    """Return the only supported source-owned utility descriptor for a unit."""

    unit = _value_unit(value_unit)
    payload = _canonical_payload(unit)
    return UtilityProjectionRule(
        value_unit=unit,
        artifact_id=_rule_artifact_id(unit),
        sha256=_digest(payload),
        payload=payload,
    )


def publish_utility_projection_rule(
    artifact_store: ArtifactStore,
    *,
    value_unit: str,
) -> UtilityProjectionRule:
    """Idempotently publish the deterministic rule descriptor.

    Publication creates no utility value and grants no scientific, provider,
    PAPER/LIVE, risk, or trading authority.  It merely makes the installed rule
    available as an immutable preregistration reference.
    """

    if type(artifact_store) is not ArtifactStore:
        raise TypeError("artifact_store must be exact ArtifactStore")
    rule = build_utility_projection_rule(value_unit)
    manifest = ArtifactStore.publish_bytes(
        artifact_store,
        artifact_id=rule.artifact_id,
        data=rule.payload,
        media_type=UTILITY_PROJECTION_MEDIA_TYPE,
        rights={"storage": True, "export": True},
        source_refs=[],
        metadata={
            "authority": UTILITY_OWNER_AUTHORITY,
            "descriptor_only": True,
            "rule_id": UTILITY_RULE_ID,
        },
    )
    if manifest.get("sha256") != rule.sha256:
        raise UtilityProjectionRuleError(
            "published utility projection digest does not match source rule"
        )
    return rule


def _parse_reference(reference: object) -> tuple[str, str]:
    if type(reference) is not str or not reference.startswith("artifact:"):
        raise UtilityProjectionRuleError(
            "utility projection reference must be immutable artifact text"
        )
    material = reference.removeprefix("artifact:")
    if material.count("@") != 1:
        raise UtilityProjectionRuleError(
            "utility projection reference must bind one digest"
        )
    artifact_id, digest = material.split("@", 1)
    try:
        canonical_id = str(UUID(artifact_id))
    except (TypeError, ValueError, AttributeError) as error:
        raise UtilityProjectionRuleError(
            "utility projection artifact identity is invalid"
        ) from error
    if canonical_id != artifact_id:
        raise UtilityProjectionRuleError(
            "utility projection artifact identity is not canonical"
        )
    if type(digest) is not str or _SHA256_RE.fullmatch(digest) is None:
        raise UtilityProjectionRuleError(
            "utility projection digest is not canonical"
        )
    return artifact_id, digest


def require_source_owned_utility_projection_rule(
    artifact_store: ArtifactStore,
    *,
    reference: str,
    value_unit: str,
) -> UtilityProjectionRule:
    """Resolve a preregistered ref only when it is exactly the installed rule.

    A writable or caller-selected ArtifactStore cannot redefine projection
    semantics: the expected UUID, digest and bytes are reconstructed from source
    constants before any stored descriptor is accepted.
    """

    if type(artifact_store) is not ArtifactStore:
        raise TypeError("artifact_store must be exact ArtifactStore")
    expected = build_utility_projection_rule(value_unit)
    artifact_id, digest = _parse_reference(reference)
    if artifact_id != expected.artifact_id or digest != expected.sha256:
        raise UtilityProjectionRuleError(
            "utility projection reference does not name the installed source rule"
        )

    manifest, data = ArtifactStore.read_authenticated_snapshot(
        artifact_store,
        artifact_id,
    )
    if manifest.get("media_type") != UTILITY_PROJECTION_MEDIA_TYPE:
        raise UtilityProjectionRuleError(
            "utility projection media type is not canonical"
        )
    if manifest.get("sha256") != expected.sha256:
        raise UtilityProjectionRuleError(
            "utility projection manifest digest does not match source rule"
        )
    if data != expected.payload:
        raise UtilityProjectionRuleError(
            "utility projection bytes do not match source rule"
        )
    return expected


__all__ = [
    "UTILITY_OWNER_AUTHORITY",
    "UTILITY_PROJECTION_KIND",
    "UTILITY_PROJECTION_MEDIA_TYPE",
    "UTILITY_RULE_ID",
    "UTILITY_RULE_SCHEMA_VERSION",
    "UtilityProjectionRule",
    "UtilityProjectionRuleError",
    "build_utility_projection_rule",
    "publish_utility_projection_rule",
    "require_source_owned_utility_projection_rule",
]
