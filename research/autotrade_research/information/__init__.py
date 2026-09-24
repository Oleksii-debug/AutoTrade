"""Information-source and structured-claim primitives for AutoTrade research."""

from .claims import (
    ClaimDisagreement,
    InformationClaimError,
    InformationObservation,
    RevisionConflict,
    SourceDescriptor,
    StructuredClaim,
    causal_claims,
    disagreements,
    independent_confirmation_count,
)

__all__ = [
    "ClaimDisagreement",
    "InformationClaimError",
    "InformationObservation",
    "RevisionConflict",
    "SourceDescriptor",
    "StructuredClaim",
    "causal_claims",
    "disagreements",
    "independent_confirmation_count",
]
