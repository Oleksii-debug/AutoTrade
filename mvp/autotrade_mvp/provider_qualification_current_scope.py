"""Route/currentness scope for accepted provider qualification authority.

Campaign identity is deliberately not part of this lookup scope.  A newer
campaign generation for the same exact provider/product/build/protocol domain
must compete with and explicitly supersede the prior Q instead of escaping into
a different scope.  Campaign id/version remain bound inside the Q content id.
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
_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,127}$")


class ProviderQualificationCurrentScopeError(ValueError):
    pass


def _token(value: object, *, name: str, upper: bool = False) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ProviderQualificationCurrentScopeError(
            f"{name} must be canonical non-empty text"
        )
    result = value.upper() if upper else value
    if _TOKEN_RE.fullmatch(result) is None:
        raise ProviderQualificationCurrentScopeError(f"{name} is not canonical")
    return result


def _digest(value: object, *, name: str) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise ProviderQualificationCurrentScopeError(
            f"{name} must be canonical sha256:<64-hex>"
        )
    return value


@dataclass(frozen=True, slots=True)
class ProviderQualificationCurrentScope:
    provider_scope: ProviderFinancialScope
    product_family: str
    adapter_source_git_sha: str
    packaged_artifact_digest: str
    protocol_id: str
    protocol_version: str

    def __post_init__(self) -> None:
        if type(self.provider_scope) is not ProviderFinancialScope:
            raise ProviderQualificationCurrentScopeError(
                "provider_scope must be exact ProviderFinancialScope"
            )
        object.__setattr__(
            self,
            "product_family",
            _token(self.product_family, name="product_family", upper=True),
        )
        if (
            type(self.adapter_source_git_sha) is not str
            or _GIT_SHA_RE.fullmatch(self.adapter_source_git_sha) is None
        ):
            raise ProviderQualificationCurrentScopeError(
                "adapter_source_git_sha must be canonical lowercase 40-hex Git SHA"
            )
        object.__setattr__(
            self,
            "packaged_artifact_digest",
            _digest(self.packaged_artifact_digest, name="packaged_artifact_digest"),
        )
        object.__setattr__(
            self,
            "protocol_id",
            _token(self.protocol_id, name="protocol_id"),
        )
        object.__setattr__(
            self,
            "protocol_version",
            _token(self.protocol_version, name="protocol_version"),
        )

    def payload(self) -> dict[str, object]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "provider_scope": self.provider_scope.payload(),
            "product_family": self.product_family,
            "adapter_source_git_sha": self.adapter_source_git_sha,
            "packaged_artifact_digest": self.packaged_artifact_digest,
            "protocol_id": self.protocol_id,
            "protocol_version": self.protocol_version,
        }

    @property
    def content_digest(self) -> str:
        return "provider-qualification-current-scope:sha256:" + sha256(
            canonical_json(self.payload()).encode("utf-8")
        ).hexdigest()
