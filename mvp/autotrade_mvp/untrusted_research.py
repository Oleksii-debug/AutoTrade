"""Fail-closed boundary for untrusted research/model content.

This module is not an authority engine. It enforces a one-way safety property:
external text and model outputs can carry evidence/proposals only. They cannot
mint credentials, tools, execution authority, or redistribution rights.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping as MappingABC
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
import json
from math import isfinite
from types import MappingProxyType
from typing import Iterable, Mapping


class ResearchBoundaryError(ValueError):
    pass


_MAX_INPUT_NODES = 10_000
_MAX_CONTAINER_ITEMS = 2_048
_MAX_STRING_UTF8_BYTES = 1_048_576
_MAX_INTEGER_BITS = 4_096


def _utf8_size(value: str, *, label: str) -> int:
    try:
        return len(value.encode("utf-8"))
    except UnicodeEncodeError as error:
        raise ResearchBoundaryError(
            f"{label} must contain valid UTF-8 text"
        ) from error


class _FrozenDict(MappingABC[str, object]):
    """Read-only mapping with no mutable dict base class."""

    __slots__ = ("_values",)

    def __init__(self, values: Mapping[str, object]) -> None:
        object.__setattr__(
            self,
            "_values",
            MappingProxyType(dict(values)),
        )

    def __getitem__(self, key: str) -> object:
        return self._values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    @staticmethod
    def _blocked(*args, **kwargs):
        raise TypeError("model proposal is immutable")

    __setitem__ = _blocked
    __delitem__ = _blocked
    __setattr__ = _blocked
    __delattr__ = _blocked
    clear = _blocked
    pop = _blocked
    popitem = _blocked
    setdefault = _blocked
    update = _blocked


def _freeze_proposal(
    value: object,
    *,
    depth: int = 0,
    label: str = "model proposal",
    _budget: list[int] | None = None,
) -> object:
    """Freeze JSON-like input while enforcing deterministic structural budgets.

    Depth alone is insufficient for hostile input: a shallow object can still
    contain millions of leaves or one enormous scalar.  The shared mutable
    budget counts every visited node across the whole tree before copying it.
    """

    if _budget is None:
        _budget = [0]
    _budget[0] += 1
    if _budget[0] > _MAX_INPUT_NODES:
        raise ResearchBoundaryError(
            f"{label} exceeds maximum structural node budget"
        )
    if depth > 32:
        raise ResearchBoundaryError(f"{label} exceeds maximum nesting depth")
    if isinstance(value, Mapping):
        if len(value) > _MAX_CONTAINER_ITEMS:
            raise ResearchBoundaryError(
                f"{label} exceeds maximum object width"
            )
        frozen: dict[str, object] = {}
        for key, nested in value.items():
            if not isinstance(key, str):
                raise ResearchBoundaryError(f"{label} object keys must be strings")
            if _utf8_size(key, label=f"{label} object key") > _MAX_STRING_UTF8_BYTES:
                raise ResearchBoundaryError(
                    f"{label} object key exceeds maximum text size"
                )
            frozen[key] = _freeze_proposal(
                nested,
                depth=depth + 1,
                label=label,
                _budget=_budget,
            )
        return _FrozenDict(frozen)
    if isinstance(value, (list, tuple)):
        if len(value) > _MAX_CONTAINER_ITEMS:
            raise ResearchBoundaryError(
                f"{label} exceeds maximum array width"
            )
        return tuple(
            _freeze_proposal(
                item,
                depth=depth + 1,
                label=label,
                _budget=_budget,
            )
            for item in value
        )
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        if _utf8_size(value, label=f"{label} text value") > _MAX_STRING_UTF8_BYTES:
            raise ResearchBoundaryError(
                f"{label} text value exceeds maximum size"
            )
        return value
    if isinstance(value, int):
        if value.bit_length() > _MAX_INTEGER_BITS:
            raise ResearchBoundaryError(
                f"{label} integer exceeds maximum numeric size"
            )
        return value
    if isinstance(value, float):
        if not isfinite(value):
            raise ResearchBoundaryError(
                f"{label} numbers must be finite JSON values"
            )
        return value
    raise ResearchBoundaryError(
        f"{label} values must be JSON-compatible scalars, objects, or arrays"
    )

class ResearchCapability(StrEnum):
    READ_MARKET_EVIDENCE = "READ_MARKET_EVIDENCE"
    READ_RESEARCH_ARTIFACT = "READ_RESEARCH_ARTIFACT"
    COMPUTE_STATISTICS = "COMPUTE_STATISTICS"
    WRITE_RESEARCH_ARTIFACT = "WRITE_RESEARCH_ARTIFACT"


SAFE_RESEARCH_CAPABILITIES = frozenset(ResearchCapability)


class Redistribution(StrEnum):
    FULL = "FULL"
    METADATA_ONLY = "METADATA_ONLY"
    NONE = "NONE"


def _text(value: object, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ResearchBoundaryError(f"{name} is required")
    return value.strip()


def _hash(value: str) -> str:
    return "sha256:" + sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ResearchEvidence:
    evidence_id: str
    source_id: str
    source_revision: str
    content: str
    rights_basis: str
    redistribution: Redistribution
    untrusted_content: bool = True
    permission_effect: str = "NONE"

    def __post_init__(self) -> None:
        for field in (
            "evidence_id",
            "source_id",
            "source_revision",
            "content",
            "rights_basis",
        ):
            object.__setattr__(
                self,
                field,
                _text(getattr(self, field), name=field),
            )
        if self.untrusted_content is not True:
            raise ResearchBoundaryError("research evidence must remain untrusted")
        if self.permission_effect != "NONE":
            raise ResearchBoundaryError("research evidence cannot grant authority")
        if not isinstance(self.redistribution, Redistribution):
            raise ResearchBoundaryError("redistribution must be explicit")


@dataclass(frozen=True, slots=True)
class ResearchToolRequest:
    request_id: str
    tool_name: str
    requested_capabilities: tuple[str, ...]
    arguments: Mapping[str, object]
    evidence_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "request_id", _text(self.request_id, name="request_id"))
        object.__setattr__(self, "tool_name", _text(self.tool_name, name="tool_name"))
        capabilities = tuple(
            _text(item, name="requested_capability").upper()
            for item in self.requested_capabilities
        )
        if len(set(capabilities)) != len(capabilities):
            raise ResearchBoundaryError("requested capabilities must be unique")
        object.__setattr__(self, "requested_capabilities", capabilities)
        if not isinstance(self.arguments, Mapping):
            raise ResearchBoundaryError("arguments must be an object")
        object.__setattr__(
            self,
            "arguments",
            _freeze_proposal(self.arguments, label="research tool arguments"),
        )
        refs = tuple(_text(item, name="evidence_ref") for item in self.evidence_refs)
        object.__setattr__(self, "evidence_refs", refs)


@dataclass(frozen=True, slots=True)
class AdmittedResearchToolRequest:
    request_id: str
    tool_name: str
    capabilities: tuple[ResearchCapability, ...]
    arguments: Mapping[str, object]
    evidence_refs: tuple[str, ...]
    permission_effect: str = "NONE"

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "request_id",
            _text(self.request_id, name="request_id"),
        )
        object.__setattr__(
            self,
            "tool_name",
            _text(self.tool_name, name="tool_name"),
        )
        if isinstance(self.capabilities, (str, bytes)):
            raise ResearchBoundaryError("capabilities must be a collection")
        capabilities = tuple(self.capabilities)
        if not capabilities:
            raise ResearchBoundaryError(
                "admitted request requires at least one research capability"
            )
        if any(not isinstance(item, ResearchCapability) for item in capabilities):
            raise ResearchBoundaryError(
                "admitted capabilities must be ResearchCapability values"
            )
        if len(set(capabilities)) != len(capabilities):
            raise ResearchBoundaryError("admitted capabilities must be unique")
        if not set(capabilities) <= SAFE_RESEARCH_CAPABILITIES:
            raise ResearchBoundaryError(
                "admitted request contains a non-research capability"
            )
        object.__setattr__(self, "capabilities", capabilities)
        if not isinstance(self.arguments, Mapping):
            raise ResearchBoundaryError("arguments must be an object")
        object.__setattr__(
            self,
            "arguments",
            _freeze_proposal(
                self.arguments,
                label="admitted research tool arguments",
            ),
        )
        refs = tuple(
            _text(item, name="evidence_ref")
            for item in self.evidence_refs
        )
        if len(set(refs)) != len(refs):
            raise ResearchBoundaryError("evidence references must be unique")
        object.__setattr__(self, "evidence_refs", refs)
        if self.permission_effect != "NONE":
            raise ResearchBoundaryError(
                "admitted research request cannot grant authority"
            )


class ResearchToolBoundary:
    """Admit only host-configured research tools and non-escalating capabilities."""

    def __init__(self, tool_capabilities: Mapping[str, Iterable[ResearchCapability]]) -> None:
        if not isinstance(tool_capabilities, Mapping):
            raise TypeError("tool_capabilities must be a mapping")
        normalized: dict[str, frozenset[ResearchCapability]] = {}
        for tool_name, capabilities in tool_capabilities.items():
            name = _text(tool_name, name="tool_name")
            if name in normalized:
                raise ResearchBoundaryError(
                    "tool names must be unique after normalization"
                )
            if isinstance(capabilities, (str, bytes)):
                raise ResearchBoundaryError(
                    "tool capabilities must be ResearchCapability values"
                )
            materialized = tuple(capabilities)
            if not materialized:
                raise ResearchBoundaryError("tool capability set cannot be empty")
            if any(
                not isinstance(capability, ResearchCapability)
                for capability in materialized
            ):
                raise ResearchBoundaryError(
                    "tool capabilities must be ResearchCapability values"
                )
            values = frozenset(materialized)
            if len(values) != len(materialized):
                raise ResearchBoundaryError("tool capabilities must be unique")
            if not values <= SAFE_RESEARCH_CAPABILITIES:
                raise ResearchBoundaryError("tool contains a non-research capability")
            normalized[name] = values
        self._tool_capabilities = normalized

    def admit(self, request: ResearchToolRequest) -> AdmittedResearchToolRequest:
        if not isinstance(request, ResearchToolRequest):
            raise TypeError("request must be a ResearchToolRequest")
        allowed = self._tool_capabilities.get(request.tool_name)
        if allowed is None:
            raise PermissionError("research tool is not allowlisted")

        requested: list[ResearchCapability] = []
        for item in request.requested_capabilities:
            try:
                capability = ResearchCapability(item)
            except ValueError as error:
                raise PermissionError(
                    f"requested capability is forbidden: {item}"
                ) from error
            if capability not in allowed:
                raise PermissionError(
                    f"tool is not allowed capability: {capability.value}"
                )
            requested.append(capability)

        return AdmittedResearchToolRequest(
            request_id=request.request_id,
            tool_name=request.tool_name,
            capabilities=tuple(requested),
            arguments=request.arguments,
            evidence_refs=request.evidence_refs,
        )


@dataclass(frozen=True, slots=True)
class ResearchModelResult:
    result_id: str
    proposal: Mapping[str, object]
    evidence_refs: tuple[str, ...]
    requested_capabilities: tuple[str, ...] = ()
    permission_effect: str = "NONE"

    def __post_init__(self) -> None:
        object.__setattr__(self, "result_id", _text(self.result_id, name="result_id"))
        if not isinstance(self.proposal, Mapping):
            raise ResearchBoundaryError("proposal must be an object")
        object.__setattr__(self, "proposal", _freeze_proposal(self.proposal))
        refs = tuple(_text(item, name="evidence_ref") for item in self.evidence_refs)
        if not refs:
            raise ResearchBoundaryError("model result requires evidence references")
        object.__setattr__(self, "evidence_refs", refs)
        capabilities = tuple(
            _text(item, name="requested_capability").upper()
            for item in self.requested_capabilities
        )
        object.__setattr__(self, "requested_capabilities", capabilities)
        if self.permission_effect != "NONE":
            raise ResearchBoundaryError("model result cannot grant authority")


_FORBIDDEN_PRIVILEGED_FIELDS = frozenset(
    {
        "credentials",
        "credential",
        "secret",
        "token",
        "apikey",
        "accesstoken",
        "refreshtoken",
        "password",
        "privatekey",
        "signingkey",
        "authorization",
        "bearertoken",
        "sessioncookie",
        "authoritygrant",
        "toolgrant",
        "tradingauthority",
        "executionauthority",
        "withdrawalauthority",
    }
)
_MAX_PROPOSAL_DEPTH = 32


def _privileged_key(value: str) -> str:
    return "".join(character for character in value.casefold() if character.isalnum())


def _scan_privileged_fields(value: object, *, depth: int = 0) -> frozenset[str]:
    if depth > _MAX_PROPOSAL_DEPTH:
        raise ResearchBoundaryError("model proposal exceeds maximum nesting depth")
    found: set[str] = set()
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if not isinstance(key, str):
                raise ResearchBoundaryError("model proposal object keys must be strings")
            normalized = _privileged_key(key.strip())
            if normalized in _FORBIDDEN_PRIVILEGED_FIELDS:
                found.add(normalized)
            found.update(_scan_privileged_fields(nested, depth=depth + 1))
    elif isinstance(value, (list, tuple)):
        for nested in value:
            found.update(_scan_privileged_fields(nested, depth=depth + 1))
    return frozenset(found)


def validate_model_result(result: ResearchModelResult) -> ResearchModelResult:
    """Reject capability-seeking model output instead of interpreting it as policy."""

    if not isinstance(result, ResearchModelResult):
        raise TypeError("result must be a ResearchModelResult")
    if result.requested_capabilities:
        raise PermissionError(
            "model output cannot request or expand runtime capabilities"
        )
    overlap = _scan_privileged_fields(result.proposal)
    if overlap:
        raise PermissionError(
            "model output contains forbidden privileged fields: "
            + ", ".join(sorted(overlap))
        )
    return result


def export_research_evidence(
    evidence: ResearchEvidence,
) -> dict[str, object]:
    """Return the maximum redistribution-safe representation of evidence."""

    if not isinstance(evidence, ResearchEvidence):
        raise TypeError("evidence must be ResearchEvidence")
    metadata = {
        "evidence_id": evidence.evidence_id,
        "source_id": evidence.source_id,
        "source_revision": evidence.source_revision,
        "rights_basis": evidence.rights_basis,
        "content_sha256": _hash(evidence.content),
        "redistribution": evidence.redistribution.value,
        "untrusted_content": True,
        "permission_effect": "NONE",
    }
    if evidence.redistribution is Redistribution.FULL:
        return {**metadata, "content": evidence.content}
    if evidence.redistribution is Redistribution.METADATA_ONLY:
        return metadata
    return {
        "evidence_id": evidence.evidence_id,
        "source_id": evidence.source_id,
        "source_revision": evidence.source_revision,
        "content_sha256": metadata["content_sha256"],
        "redistribution": evidence.redistribution.value,
        "omitted_reason": "source_rights_forbid_redistribution",
        "untrusted_content": True,
        "permission_effect": "NONE",
    }


def canonical_export_digest(value: Mapping[str, object]) -> str:
    if not isinstance(value, Mapping):
        raise TypeError("value must be a mapping")
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + sha256(encoded).hexdigest()
