from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Iterable


class TrustClass(StrEnum):
    TRUSTED_INTERNAL = "TRUSTED_INTERNAL"
    USER_APPROVED = "USER_APPROVED"
    UNTRUSTED_CONTENT = "UNTRUSTED_CONTENT"
    MODEL_OUTPUT = "MODEL_OUTPUT"


class ToolEffect(StrEnum):
    READ_ONLY = "READ_ONLY"
    RESEARCH_NETWORK = "RESEARCH_NETWORK"
    WRITE_NONFINANCIAL = "WRITE_NONFINANCIAL"
    FINANCIAL_PROPOSAL = "FINANCIAL_PROPOSAL"
    FINANCIAL_SEND = "FINANCIAL_SEND"
    CREDENTIAL_ACCESS = "CREDENTIAL_ACCESS"
    SHELL = "SHELL"


@dataclass(frozen=True, slots=True)
class ToolDescriptor:
    tool_id: str
    effect: ToolEffect
    allowed_trust: tuple[TrustClass, ...]

    def __post_init__(self) -> None:
        if not self.tool_id.strip():
            raise ValueError("tool_id is required")
        if not self.allowed_trust:
            raise ValueError("allowed_trust cannot be empty")


@dataclass(frozen=True, slots=True)
class ToolRequest:
    request_id: str
    tool_id: str
    trust_class: TrustClass
    requested_effect: ToolEffect
    authority_claims: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.request_id.strip() or not self.tool_id.strip():
            raise ValueError("request_id and tool_id are required")


@dataclass(frozen=True, slots=True)
class ToolDecision:
    allowed: bool
    reason: str
    granted_effect: ToolEffect | None


_FORBIDDEN_FROM_UNTRUSTED = {
    ToolEffect.FINANCIAL_SEND,
    ToolEffect.CREDENTIAL_ACCESS,
    ToolEffect.SHELL,
}

_FORBIDDEN_FROM_MODEL_OUTPUT = _FORBIDDEN_FROM_UNTRUSTED | {
    ToolEffect.WRITE_NONFINANCIAL,
}


def evaluate_tool_request(
    *,
    request: ToolRequest,
    tools: Iterable[ToolDescriptor],
) -> ToolDecision:
    """Fail-closed tool gate for research text and model output.

    Authority claims embedded in content are ignored. This gate never grants
    financial send authority; such authority belongs to the execution subsystem.
    """
    by_id: dict[str, ToolDescriptor] = {}
    for tool in tools:
        if tool.tool_id in by_id:
            raise ValueError(f"duplicate tool descriptor: {tool.tool_id}")
        by_id[tool.tool_id] = tool

    descriptor = by_id.get(request.tool_id)
    if descriptor is None:
        return ToolDecision(False, "unknown_tool", None)
    if descriptor.effect != request.requested_effect:
        return ToolDecision(False, "effect_mismatch", None)
    if request.trust_class not in descriptor.allowed_trust:
        return ToolDecision(False, "trust_class_not_allowed", None)

    if request.trust_class is TrustClass.UNTRUSTED_CONTENT:
        if request.requested_effect in _FORBIDDEN_FROM_UNTRUSTED:
            return ToolDecision(False, "untrusted_content_cannot_receive_sensitive_tool", None)
    if request.trust_class is TrustClass.MODEL_OUTPUT:
        if request.requested_effect in _FORBIDDEN_FROM_MODEL_OUTPUT:
            return ToolDecision(False, "model_output_cannot_receive_sensitive_tool", None)

    if request.requested_effect is ToolEffect.FINANCIAL_SEND:
        return ToolDecision(False, "financial_send_requires_execution_authority", None)

    return ToolDecision(True, "admitted", descriptor.effect)


def sanitize_untrusted_instructions(text: str) -> str:
    """Return content as inert evidence, never executable instructions."""
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    return text
