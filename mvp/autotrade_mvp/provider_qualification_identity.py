"""Pure immutable content identity for one provider qualification result.

This module is intentionally not the #1082 accepted-Q issuer or current index.
A caller can construct this value, therefore construction is never proof that a
campaign ran or that an attestation/trust policy was accepted.  The canonical
qualification authority must derive and seal this exact identity from trusted
attestation + authenticated campaign evidence before production consumers use it.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import re

from .persistence import canonical_json
from .provider_domain import ProviderFinancialScope


_SCHEMA_VERSION = "1.0.0"
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class ProviderQualificationIdentityError(ValueError):
    """Raised when qualification content identity is non-canonical."""


def _text(value: object, *, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ProviderQualificationIdentityError(
            f"{name} must be canonical non-empty text"
        )
    return value


def _digest(value: object, *, name: str) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise ProviderQualificationIdentityError(
            f"{name} must be canonical lowercase sha256:<64-hex>"
        )
    return value


def _positive_int(value: object, *, name: str) -> int:
    if type(value) is not int or value < 1:
        raise ProviderQualificationIdentityError(
            f"{name} must be a positive exact integer"
        )
    return value


@dataclass(frozen=True, slots=True)
class ProviderQualificationIdentity:
    """Content identity that an accepted #1082 qualification record must seal."""

    provider_scope: ProviderFinancialScope
    product_family: str
    adapter_source_git_sha: str
    packaged_artifact_digest: str
    campaign_id: str
    campaign_version: int
    required_case_policy_digest: str
    result_set_digest: str
    route_semantics_digest: str
    documentation_revision_digest: str
    evidence_set_digest: str
    attestation_digest: str
    trust_policy_digest: str
    issuer_identity_digest: str
    verifier_identity_digest: str

    def __post_init__(self) -> None:
        if type(self.provider_scope) is not ProviderFinancialScope:
            raise ProviderQualificationIdentityError(
                "provider_scope must be exact ProviderFinancialScope"
            )
        object.__setattr__(
            self,
            "product_family",
            _text(self.product_family, name="product_family").upper(),
        )
        if (
            type(self.adapter_source_git_sha) is not str
            or _GIT_SHA_RE.fullmatch(self.adapter_source_git_sha) is None
        ):
            raise ProviderQualificationIdentityError(
                "adapter_source_git_sha must be canonical lowercase 40-hex Git SHA"
            )
        object.__setattr__(
            self,
            "campaign_id",
            _text(self.campaign_id, name="campaign_id"),
        )
        object.__setattr__(
            self,
            "campaign_version",
            _positive_int(self.campaign_version, name="campaign_version"),
        )
        for name in (
            "packaged_artifact_digest",
            "required_case_policy_digest",
            "result_set_digest",
            "route_semantics_digest",
            "documentation_revision_digest",
            "evidence_set_digest",
            "attestation_digest",
            "trust_policy_digest",
            "issuer_identity_digest",
            "verifier_identity_digest",
        ):
            object.__setattr__(self, name, _digest(getattr(self, name), name=name))

    def payload(self) -> dict[str, object]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "provider_scope": self.provider_scope.payload(),
            "provider_scope_digest": self.provider_scope.content_digest,
            "product_family": self.product_family,
            "adapter_source_git_sha": self.adapter_source_git_sha,
            "packaged_artifact_digest": self.packaged_artifact_digest,
            "campaign_id": self.campaign_id,
            "campaign_version": self.campaign_version,
            "required_case_policy_digest": self.required_case_policy_digest,
            "result_set_digest": self.result_set_digest,
            "route_semantics_digest": self.route_semantics_digest,
            "documentation_revision_digest": self.documentation_revision_digest,
            "evidence_set_digest": self.evidence_set_digest,
            "attestation_digest": self.attestation_digest,
            "trust_policy_digest": self.trust_policy_digest,
            "issuer_identity_digest": self.issuer_identity_digest,
            "verifier_identity_digest": self.verifier_identity_digest,
        }

    @property
    def content_digest(self) -> str:
        digest = sha256(canonical_json(self.payload()).encode("utf-8")).hexdigest()
        return "provider-qualification:sha256:" + digest
