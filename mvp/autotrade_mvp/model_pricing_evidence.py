"""Independent immutable pricing authority for production model-call routing.

This module does not route models or reserve budget. It converts one retained,
issuer-scoped pricing artifact into the existing PricingEvidenceSnapshot used by
DurableModelCallOrchestrator. ArtifactStore authenticates storage bytes; the
separate trusted receipt authenticates who may price which provider/model scope.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
import re
from types import MappingProxyType
from typing import Iterable
from uuid import UUID

from autotrade_runtime.artifacts import (
    ArtifactIntegrityError,
    ArtifactStore,
    trusted_authenticated_reader,
)
from autotrade_runtime.strict_json import strict_json_loads

from .exact_decimal import (
    ExactDecimalError,
    canonical_decimal_text,
    parse_bounded_exact_decimal,
)
from .model_call import ModelCallSpec, PricingEvidenceSnapshot, PricingQuote
from .model_gateway import ModelDescriptor


MODEL_PRICING_MEDIA_TYPE = (
    "application/vnd.autotrade.model-pricing-evidence+json;version=1"
)
MODEL_PRICING_EVIDENCE_TYPE = "AUTOTRADE_MODEL_PRICING_EVIDENCE"
MODEL_PRICING_SCHEMA_VERSION = 1
REMOTE_PROVIDER_SOURCE = "REMOTE_PROVIDER"
LOCAL_RUNTIME_SOURCE = "LOCAL_RUNTIME"

_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_CURRENCY = re.compile(r"^[A-Z0-9]+$")
_BODY_FIELDS = frozenset(
    {
        "schema_version",
        "evidence_type",
        "issuer",
        "source_class",
        "source_sha256",
        "as_of",
        "valid_until",
        "cost_currency",
        "quotes",
    }
)
_QUOTE_FIELDS = frozenset(
    {"provider_id", "model_id", "revision", "estimated_cost"}
)


class ModelPricingEvidenceError(ValueError):
    """Raised when immutable pricing evidence is not independently authoritative."""


def _text(value: object, *, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ModelPricingEvidenceError(f"{name} must be canonical non-empty text")
    return value


def _digest(value: object, *, name: str) -> str:
    text = _text(value, name=name)
    if _DIGEST.fullmatch(text) is None:
        raise ModelPricingEvidenceError(f"{name} must be canonical sha256:<64-hex>")
    return text


def _uuid(value: object, *, name: str) -> str:
    text = _text(value, name=name)
    try:
        canonical = str(UUID(text))
    except (ValueError, TypeError, AttributeError) as error:
        raise ModelPricingEvidenceError(f"{name} must be UUID") from error
    if canonical != text:
        raise ModelPricingEvidenceError(f"{name} must use canonical UUID text")
    return canonical


def _utc(value: object, *, name: str) -> str:
    from datetime import datetime, timezone

    text = _text(value, name=name)
    if not text.endswith("Z"):
        raise ModelPricingEvidenceError(f"{name} must be canonical UTC text")
    try:
        point = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as error:
        raise ModelPricingEvidenceError(f"{name} must be canonical UTC text") from error
    canonical = point.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if canonical != text:
        raise ModelPricingEvidenceError(f"{name} must be canonical UTC text")
    return text


def _canonical_json_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ModelPricingEvidenceError("pricing evidence is not canonical JSON") from error


def _cost(value: object) -> Decimal:
    if type(value) is not str:
        raise ModelPricingEvidenceError("estimated_cost must be canonical decimal text")
    try:
        parsed = parse_bounded_exact_decimal(value)
        rendered = canonical_decimal_text(parsed)
    except (ExactDecimalError, TypeError, ValueError) as error:
        raise ModelPricingEvidenceError(
            "estimated_cost must be bounded canonical decimal text"
        ) from error
    if parsed < 0 or rendered != value:
        raise ModelPricingEvidenceError(
            "estimated_cost must be non-negative canonical decimal text"
        )
    return parsed


@dataclass(frozen=True, slots=True)
class PricingAuthorityScope:
    provider_id: str
    model_id: str
    revision: str | None
    remote: bool

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "provider_id", _text(self.provider_id, name="provider_id")
        )
        object.__setattr__(
            self, "model_id", _text(self.model_id, name="model_id")
        )
        if self.revision is not None:
            object.__setattr__(
                self, "revision", _text(self.revision, name="revision")
            )
        if type(self.remote) is not bool:
            raise TypeError("remote must be exact bool")

    @property
    def key(self) -> tuple[str, str, str | None]:
        return (self.provider_id, self.model_id, self.revision)


@dataclass(frozen=True, slots=True)
class TrustedPricingArtifactReceipt:
    """Out-of-band issuer trust bound to one immutable pricing object."""

    artifact_id: str
    object_sha256: str
    source_sha256: str
    issuer: str
    source_class: str
    scopes: tuple[PricingAuthorityScope, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "artifact_id", _uuid(self.artifact_id, name="artifact_id")
        )
        object.__setattr__(
            self,
            "object_sha256",
            _digest(self.object_sha256, name="object_sha256"),
        )
        object.__setattr__(
            self,
            "source_sha256",
            _digest(self.source_sha256, name="source_sha256"),
        )
        object.__setattr__(self, "issuer", _text(self.issuer, name="issuer"))
        source_class = _text(self.source_class, name="source_class")
        if source_class not in {REMOTE_PROVIDER_SOURCE, LOCAL_RUNTIME_SOURCE}:
            raise ModelPricingEvidenceError("unsupported pricing source_class")
        object.__setattr__(self, "source_class", source_class)
        if type(self.scopes) is not tuple or not self.scopes:
            raise ModelPricingEvidenceError("trusted pricing receipt requires scopes")
        if any(type(item) is not PricingAuthorityScope for item in self.scopes):
            raise TypeError("trusted pricing scopes must be exact PricingAuthorityScope")
        if len({item.key for item in self.scopes}) != len(self.scopes):
            raise ModelPricingEvidenceError("trusted pricing scopes must be unique")
        expected_remote = source_class == REMOTE_PROVIDER_SOURCE
        if any(item.remote is not expected_remote for item in self.scopes):
            raise ModelPricingEvidenceError(
                "pricing source_class cannot cross remote/local scope"
            )
        object.__setattr__(
            self,
            "scopes",
            tuple(
                sorted(
                    self.scopes,
                    key=lambda item: (
                        item.provider_id,
                        item.model_id,
                        item.revision or "",
                    ),
                )
            ),
        )


class ModelPricingEvidenceAuthority:
    """Callable production PricingEvidenceResolver with a private trusted root."""

    def __init__(
        self,
        *,
        evidence_root: str | Path,
        publication_store: ArtifactStore,
        trusted_receipts: Iterable[TrustedPricingArtifactReceipt],
    ) -> None:
        if type(publication_store) is not ArtifactStore:
            raise TypeError("publication_store must be exact canonical ArtifactStore")
        receipts = tuple(trusted_receipts)
        if not receipts or any(
            type(item) is not TrustedPricingArtifactReceipt for item in receipts
        ):
            raise TypeError(
                "trusted_receipts must contain exact TrustedPricingArtifactReceipt values"
            )
        by_id: dict[str, TrustedPricingArtifactReceipt] = {}
        for receipt in receipts:
            if receipt.artifact_id in by_id:
                raise ModelPricingEvidenceError("duplicate trusted pricing artifact id")
            by_id[receipt.artifact_id] = receipt
        try:
            self._read = trusted_authenticated_reader(
                evidence_root,
                publication_store=publication_store,
            )
        except Exception as error:
            raise ModelPricingEvidenceError(
                "trusted pricing evidence root cannot be established"
            ) from error
        self._receipts = MappingProxyType(by_id)

    def __call__(
        self,
        spec: ModelCallSpec,
        descriptors: tuple[ModelDescriptor, ...],
    ) -> PricingEvidenceSnapshot:
        if type(spec) is not ModelCallSpec:
            raise TypeError("spec must be exact ModelCallSpec")
        if type(descriptors) is not tuple or any(
            type(item) is not ModelDescriptor for item in descriptors
        ):
            raise TypeError("descriptors must be exact ModelDescriptor tuple")

        artifact_id = _uuid(
            spec.pricing_evidence_id,
            name="pricing_evidence_id",
        )
        receipt = self._receipts.get(artifact_id)
        if receipt is None:
            raise ModelPricingEvidenceError(
                "pricing artifact has no independently trusted issuer receipt"
            )

        try:
            manifest, raw = self._read(artifact_id)
        except Exception as error:
            raise ModelPricingEvidenceError(
                "trusted pricing artifact cannot be authenticated"
            ) from error
        return self._resolve_snapshot(
            artifact_id=artifact_id,
            receipt=receipt,
            manifest=manifest,
            raw=raw,
            descriptors=descriptors,
        )

    @staticmethod
    def _resolve_snapshot(
        *,
        artifact_id: str,
        receipt: TrustedPricingArtifactReceipt,
        manifest: object,
        raw: object,
        descriptors: tuple[ModelDescriptor, ...],
    ) -> PricingEvidenceSnapshot:
        if type(manifest) is not dict or type(raw) is not bytes:
            raise ModelPricingEvidenceError("pricing artifact representation is invalid")
        if manifest.get("artifact_id") != artifact_id:
            raise ModelPricingEvidenceError("pricing artifact identity mismatch")
        object_digest = manifest.get("sha256")
        if object_digest != receipt.object_sha256:
            raise ModelPricingEvidenceError("pricing artifact digest is not trusted")
        if object_digest != "sha256:" + sha256(raw).hexdigest():
            raise ModelPricingEvidenceError("pricing artifact object digest mismatch")
        if manifest.get("media_type") != MODEL_PRICING_MEDIA_TYPE:
            raise ModelPricingEvidenceError("pricing artifact media type mismatch")
        rights = manifest.get("rights")
        if type(rights) is not dict or rights.get("storage") is not True:
            raise ModelPricingEvidenceError("pricing artifact lacks storage provenance")

        try:
            decoded = raw.decode("utf-8", errors="strict")
            body = strict_json_loads(decoded)
        except (UnicodeError, TypeError, ValueError) as error:
            raise ModelPricingEvidenceError("pricing artifact JSON is invalid") from error
        if type(body) is not dict or set(body) != _BODY_FIELDS:
            raise ModelPricingEvidenceError("pricing artifact schema is not closed")
        if _canonical_json_bytes(body) != raw:
            raise ModelPricingEvidenceError("pricing artifact bytes are not canonical JSON")

        if body.get("schema_version") != MODEL_PRICING_SCHEMA_VERSION:
            raise ModelPricingEvidenceError("pricing artifact schema_version mismatch")
        if body.get("evidence_type") != MODEL_PRICING_EVIDENCE_TYPE:
            raise ModelPricingEvidenceError("pricing artifact evidence_type mismatch")
        issuer = _text(body.get("issuer"), name="issuer")
        source_class = _text(body.get("source_class"), name="source_class")
        source_sha256 = _digest(body.get("source_sha256"), name="source_sha256")
        as_of = _utc(body.get("as_of"), name="as_of")
        valid_until = _utc(body.get("valid_until"), name="valid_until")
        from datetime import datetime
        if datetime.fromisoformat(valid_until[:-1] + "+00:00") < datetime.fromisoformat(
            as_of[:-1] + "+00:00"
        ):
            raise ModelPricingEvidenceError("pricing evidence validity precedes as_of")
        currency = _text(body.get("cost_currency"), name="cost_currency")
        if _CURRENCY.fullmatch(currency) is None:
            raise ModelPricingEvidenceError("cost_currency must be uppercase alphanumeric")

        if (
            issuer != receipt.issuer
            or source_class != receipt.source_class
            or source_sha256 != receipt.source_sha256
        ):
            raise ModelPricingEvidenceError(
                "pricing artifact issuer/source does not match trusted receipt"
            )

        expected_metadata = {
            "schema_version": MODEL_PRICING_SCHEMA_VERSION,
            "evidence_type": MODEL_PRICING_EVIDENCE_TYPE,
            "issuer": issuer,
            "source_class": source_class,
            "source_sha256": source_sha256,
            "as_of": as_of,
            "valid_until": valid_until,
            "cost_currency": currency,
        }
        if manifest.get("metadata") != expected_metadata:
            raise ModelPricingEvidenceError("pricing artifact metadata scope mismatch")
        if manifest.get("source_refs") != [source_sha256]:
            raise ModelPricingEvidenceError("pricing artifact source digest mismatch")

        raw_quotes = body.get("quotes")
        if type(raw_quotes) is not list or not raw_quotes:
            raise ModelPricingEvidenceError("pricing artifact requires quotes")
        quotes: list[PricingQuote] = []
        quote_scopes: list[PricingAuthorityScope] = []
        seen: set[tuple[str, str, str | None]] = set()
        for value in raw_quotes:
            if type(value) is not dict or set(value) != _QUOTE_FIELDS:
                raise ModelPricingEvidenceError("pricing quote schema is not closed")
            provider_id = _text(value.get("provider_id"), name="provider_id")
            model_id = _text(value.get("model_id"), name="model_id")
            revision_value = value.get("revision")
            revision = (
                None
                if revision_value is None
                else _text(revision_value, name="revision")
            )
            key = (provider_id, model_id, revision)
            if key in seen:
                raise ModelPricingEvidenceError("pricing quote identity is duplicated")
            seen.add(key)
            estimated_cost = _cost(value.get("estimated_cost"))
            remote = source_class == REMOTE_PROVIDER_SOURCE
            if not remote and estimated_cost != Decimal("0"):
                raise ModelPricingEvidenceError(
                    "local runtime pricing must be exact zero cost"
                )
            quotes.append(
                PricingQuote(
                    provider_id=provider_id,
                    model_id=model_id,
                    revision=revision,
                    estimated_cost=estimated_cost,
                )
            )
            quote_scopes.append(
                PricingAuthorityScope(
                    provider_id=provider_id,
                    model_id=model_id,
                    revision=revision,
                    remote=remote,
                )
            )

        sorted_scopes = tuple(
            sorted(
                quote_scopes,
                key=lambda item: (
                    item.provider_id,
                    item.model_id,
                    item.revision or "",
                ),
            )
        )
        if sorted_scopes != receipt.scopes:
            raise ModelPricingEvidenceError(
                "pricing quotes exceed or differ from trusted issuer scope"
            )
        sorted_keys = sorted(
            seen,
            key=lambda item: (item[0], item[1], item[2] or ""),
        )
        if [item.key for item in quotes] != sorted_keys:
            raise ModelPricingEvidenceError("pricing quotes must be canonically sorted")

        scope_by_key = {item.key: item for item in receipt.scopes}
        for descriptor in descriptors:
            scope = scope_by_key.get(
                (descriptor.provider_id, descriptor.model_id, descriptor.revision)
            )
            if scope is None:
                continue
            if descriptor.remote is not scope.remote:
                raise ModelPricingEvidenceError(
                    "descriptor remote/local class conflicts with pricing authority"
                )

        return PricingEvidenceSnapshot(
            evidence_id=artifact_id,
            evidence_digest=receipt.object_sha256,
            as_of=as_of,
            valid_until=valid_until,
            cost_currency=currency,
            quotes=tuple(quotes),
        )

