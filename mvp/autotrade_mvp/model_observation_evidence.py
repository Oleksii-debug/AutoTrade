"""Independent response/usage authority for production model-call observations.

This module does not invoke a model or own routing/budget state. It authenticates
one retained remote-provider response or local-runtime metering artifact and
converts it into the existing ModelObservationEvidence consumed by the durable
model-call lifecycle.
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
    ArtifactStore,
    trusted_authenticated_reader,
)
from autotrade_runtime.strict_json import strict_json_loads

from .exact_decimal import (
    ExactDecimalError,
    canonical_decimal_text,
    parse_bounded_exact_decimal,
)
from .model_call import (
    ModelCallBinding,
    ModelCallObservation,
    ModelObservationEvidence,
)
from .persistence import payload_digest


MODEL_OBSERVATION_MEDIA_TYPE = (
    "application/vnd.autotrade.model-observation-evidence+json;version=1"
)
MODEL_OBSERVATION_EVIDENCE_TYPE = "AUTOTRADE_MODEL_OBSERVATION_EVIDENCE"
MODEL_OBSERVATION_SCHEMA_VERSION = 1
REMOTE_PROVIDER_RESPONSE_SOURCE = "REMOTE_PROVIDER_RESPONSE"
LOCAL_RUNTIME_METER_SOURCE = "LOCAL_RUNTIME_METER"

_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_CURRENCY = re.compile(r"^[A-Z0-9]+$")
_BODY_FIELDS = frozenset(
    {
        "schema_version",
        "evidence_type",
        "issuer",
        "source_class",
        "source_sha256",
        "attempt_id",
        "provider_id",
        "model_id",
        "revision",
        "pricing_evidence_digest",
        "cost_currency",
        "observed_at",
        "incurred_cost",
        "estimated_unbilled",
        "output_digest",
        "provider_request_id",
        "provider_response_id",
        "usage_id",
        "billing_id",
        "observation_digest",
    }
)


class ModelObservationEvidenceError(ValueError):
    """Raised when response/usage evidence is not independently authoritative."""


def _text(value: object, *, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ModelObservationEvidenceError(
            f"{name} must be canonical non-empty text"
        )
    return value


def _optional_text(value: object, *, name: str) -> str | None:
    if value is None:
        return None
    return _text(value, name=name)


def _digest(value: object, *, name: str) -> str:
    text = _text(value, name=name)
    if _DIGEST.fullmatch(text) is None:
        raise ModelObservationEvidenceError(
            f"{name} must be canonical sha256:<64-hex>"
        )
    return text


def _uuid(value: object, *, name: str) -> str:
    text = _text(value, name=name)
    try:
        canonical = str(UUID(text))
    except (ValueError, TypeError, AttributeError) as error:
        raise ModelObservationEvidenceError(f"{name} must be UUID") from error
    if canonical != text:
        raise ModelObservationEvidenceError(
            f"{name} must use canonical UUID text"
        )
    return canonical


def _utc(value: object, *, name: str) -> str:
    from datetime import datetime, timezone

    text = _text(value, name=name)
    if not text.endswith("Z"):
        raise ModelObservationEvidenceError(
            f"{name} must be canonical UTC text"
        )
    try:
        point = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as error:
        raise ModelObservationEvidenceError(
            f"{name} must be canonical UTC text"
        ) from error
    canonical = point.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if canonical != text:
        raise ModelObservationEvidenceError(
            f"{name} must be canonical UTC text"
        )
    return text


def _currency(value: object) -> str:
    text = _text(value, name="cost_currency")
    if _CURRENCY.fullmatch(text) is None:
        raise ModelObservationEvidenceError(
            "cost_currency must be uppercase alphanumeric text"
        )
    return text


def _amount(value: object, *, name: str) -> Decimal:
    if type(value) is not str:
        raise ModelObservationEvidenceError(
            f"{name} must be canonical decimal text"
        )
    try:
        parsed = parse_bounded_exact_decimal(value)
        rendered = canonical_decimal_text(parsed)
    except (ExactDecimalError, TypeError, ValueError) as error:
        raise ModelObservationEvidenceError(
            f"{name} must be bounded canonical decimal text"
        ) from error
    if parsed < 0 or rendered != value:
        raise ModelObservationEvidenceError(
            f"{name} must be non-negative canonical decimal text"
        )
    return parsed


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
        raise ModelObservationEvidenceError(
            "observation evidence is not canonical JSON"
        ) from error


def _observation_material(
    observation: ModelCallObservation,
    binding: ModelCallBinding,
) -> dict[str, object]:
    return {
        "attempt_id": binding.attempt_id,
        "provider_id": observation.provider_id,
        "model_id": observation.model_id,
        "revision": observation.revision,
        "observed_at": observation.observed_at,
        "incurred_cost": str(observation.incurred_cost),
        "estimated_unbilled": str(observation.estimated_unbilled),
        "output_digest": payload_digest(observation.output),
        "provider_request_id": observation.provider_request_id,
        "provider_response_id": observation.provider_response_id,
        "usage_id": observation.usage_id,
        "billing_id": observation.billing_id,
    }


@dataclass(frozen=True, slots=True)
class TrustedObservationArtifactReceipt:
    """Out-of-band trust for one immutable response/usage evidence object."""

    artifact_id: str
    object_sha256: str
    source_sha256: str
    issuer: str
    source_class: str
    attempt_id: str
    provider_id: str
    model_id: str
    revision: str | None
    pricing_evidence_digest: str
    cost_currency: str
    provider_request_id: str | None
    provider_response_id: str | None
    usage_id: str | None
    billing_id: str | None

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
        for field_name in (
            "issuer",
            "attempt_id",
            "provider_id",
            "model_id",
        ):
            object.__setattr__(
                self,
                field_name,
                _text(getattr(self, field_name), name=field_name),
            )
        if self.revision is not None:
            object.__setattr__(
                self, "revision", _text(self.revision, name="revision")
            )
        object.__setattr__(
            self,
            "pricing_evidence_digest",
            _digest(
                self.pricing_evidence_digest,
                name="pricing_evidence_digest",
            ),
        )
        object.__setattr__(self, "cost_currency", _currency(self.cost_currency))
        source_class = _text(self.source_class, name="source_class")
        if source_class not in {
            REMOTE_PROVIDER_RESPONSE_SOURCE,
            LOCAL_RUNTIME_METER_SOURCE,
        }:
            raise ModelObservationEvidenceError(
                "unsupported observation source_class"
            )
        object.__setattr__(self, "source_class", source_class)
        for field_name in (
            "provider_request_id",
            "provider_response_id",
            "usage_id",
            "billing_id",
        ):
            object.__setattr__(
                self,
                field_name,
                _optional_text(getattr(self, field_name), name=field_name),
            )
        if source_class == REMOTE_PROVIDER_RESPONSE_SOURCE and (
            self.provider_request_id is None
            or self.provider_response_id is None
        ):
            raise ModelObservationEvidenceError(
                "remote provider observation receipt requires request/response identities"
            )
        if source_class == LOCAL_RUNTIME_METER_SOURCE and (
            self.provider_request_id is not None
            or self.provider_response_id is not None
        ):
            raise ModelObservationEvidenceError(
                "local runtime observation receipt cannot claim remote provider identities"
            )


class ModelObservationEvidenceAuthority:
    """Callable ObservationEvidenceResolver with one private trusted evidence root."""

    def __init__(
        self,
        *,
        evidence_root: str | Path,
        publication_store: ArtifactStore,
        trusted_receipts: Iterable[TrustedObservationArtifactReceipt],
    ) -> None:
        if type(publication_store) is not ArtifactStore:
            raise TypeError(
                "publication_store must be exact canonical ArtifactStore"
            )
        receipts = tuple(trusted_receipts)
        if not receipts or any(
            type(item) is not TrustedObservationArtifactReceipt
            for item in receipts
        ):
            raise TypeError(
                "trusted_receipts must contain exact TrustedObservationArtifactReceipt values"
            )
        by_attempt: dict[str, TrustedObservationArtifactReceipt] = {}
        for receipt in receipts:
            if receipt.attempt_id in by_attempt:
                raise ModelObservationEvidenceError(
                    "duplicate trusted observation attempt"
                )
            by_attempt[receipt.attempt_id] = receipt
        try:
            self._read = trusted_authenticated_reader(
                evidence_root,
                publication_store=publication_store,
            )
        except Exception as error:
            raise ModelObservationEvidenceError(
                "trusted observation evidence root cannot be established"
            ) from error
        self._receipts = MappingProxyType(by_attempt)

    def __call__(
        self,
        observation: ModelCallObservation,
        binding: ModelCallBinding,
    ) -> ModelObservationEvidence:
        if type(observation) is not ModelCallObservation:
            raise TypeError("observation must be exact ModelCallObservation")
        if type(binding) is not ModelCallBinding:
            raise TypeError("binding must be exact ModelCallBinding")

        receipt = self._receipts.get(binding.attempt_id)
        if receipt is None:
            raise ModelObservationEvidenceError(
                "model attempt has no independently trusted observation receipt"
            )
        self._validate_binding(receipt, observation, binding)
        try:
            manifest, raw = self._read(receipt.artifact_id)
        except Exception as error:
            raise ModelObservationEvidenceError(
                "trusted observation artifact cannot be authenticated"
            ) from error
        return self._resolve(
            receipt=receipt,
            manifest=manifest,
            raw=raw,
            observation=observation,
            binding=binding,
        )

    @staticmethod
    def _validate_binding(
        receipt: TrustedObservationArtifactReceipt,
        observation: ModelCallObservation,
        binding: ModelCallBinding,
    ) -> None:
        expected_remote = (
            receipt.source_class == REMOTE_PROVIDER_RESPONSE_SOURCE
        )
        if binding.remote is not expected_remote:
            raise ModelObservationEvidenceError(
                "observation source_class cannot cross remote/local route"
            )
        if (
            receipt.attempt_id != binding.attempt_id
            or receipt.provider_id != binding.provider_id
            or receipt.model_id != binding.model_id
            or receipt.revision != binding.revision
            or receipt.pricing_evidence_digest
            != binding.pricing_evidence_digest
            or receipt.cost_currency != binding.cost_currency
            or observation.provider_id != binding.provider_id
            or observation.model_id != binding.model_id
            or observation.revision != binding.revision
            or receipt.provider_request_id
            != observation.provider_request_id
            or receipt.provider_response_id
            != observation.provider_response_id
            or receipt.usage_id != observation.usage_id
            or receipt.billing_id != observation.billing_id
        ):
            raise ModelObservationEvidenceError(
                "trusted observation receipt does not match model-call binding"
            )

    @staticmethod
    def _resolve(
        *,
        receipt: TrustedObservationArtifactReceipt,
        manifest: object,
        raw: object,
        observation: ModelCallObservation,
        binding: ModelCallBinding,
    ) -> ModelObservationEvidence:
        if type(manifest) is not dict or type(raw) is not bytes:
            raise ModelObservationEvidenceError(
                "observation artifact representation is invalid"
            )
        if manifest.get("artifact_id") != receipt.artifact_id:
            raise ModelObservationEvidenceError(
                "observation artifact identity mismatch"
            )
        object_digest = manifest.get("sha256")
        if object_digest != receipt.object_sha256:
            raise ModelObservationEvidenceError(
                "observation artifact digest is not trusted"
            )
        if object_digest != "sha256:" + sha256(raw).hexdigest():
            raise ModelObservationEvidenceError(
                "observation artifact object digest mismatch"
            )
        if manifest.get("media_type") != MODEL_OBSERVATION_MEDIA_TYPE:
            raise ModelObservationEvidenceError(
                "observation artifact media type mismatch"
            )
        rights = manifest.get("rights")
        if type(rights) is not dict or rights.get("storage") is not True:
            raise ModelObservationEvidenceError(
                "observation artifact lacks storage provenance"
            )

        try:
            decoded = raw.decode("utf-8", errors="strict")
            body = strict_json_loads(decoded)
        except (UnicodeError, TypeError, ValueError) as error:
            raise ModelObservationEvidenceError(
                "observation artifact JSON is invalid"
            ) from error
        if type(body) is not dict or set(body) != _BODY_FIELDS:
            raise ModelObservationEvidenceError(
                "observation artifact schema is not closed"
            )
        if _canonical_json_bytes(body) != raw:
            raise ModelObservationEvidenceError(
                "observation artifact bytes are not canonical JSON"
            )
        if body.get("schema_version") != MODEL_OBSERVATION_SCHEMA_VERSION:
            raise ModelObservationEvidenceError(
                "observation artifact schema_version mismatch"
            )
        if body.get("evidence_type") != MODEL_OBSERVATION_EVIDENCE_TYPE:
            raise ModelObservationEvidenceError(
                "observation artifact evidence_type mismatch"
            )

        issuer = _text(body.get("issuer"), name="issuer")
        source_class = _text(body.get("source_class"), name="source_class")
        source_sha256 = _digest(
            body.get("source_sha256"), name="source_sha256"
        )
        attempt_id = _text(body.get("attempt_id"), name="attempt_id")
        provider_id = _text(body.get("provider_id"), name="provider_id")
        model_id = _text(body.get("model_id"), name="model_id")
        revision = _optional_text(body.get("revision"), name="revision")
        pricing_digest = _digest(
            body.get("pricing_evidence_digest"),
            name="pricing_evidence_digest",
        )
        currency = _currency(body.get("cost_currency"))
        observed_at = _utc(body.get("observed_at"), name="observed_at")
        incurred = _amount(body.get("incurred_cost"), name="incurred_cost")
        unbilled = _amount(
            body.get("estimated_unbilled"),
            name="estimated_unbilled",
        )
        output_digest = _digest(
            body.get("output_digest"), name="output_digest"
        )
        provider_request_id = _optional_text(
            body.get("provider_request_id"), name="provider_request_id"
        )
        provider_response_id = _optional_text(
            body.get("provider_response_id"), name="provider_response_id"
        )
        usage_id = _optional_text(body.get("usage_id"), name="usage_id")
        billing_id = _optional_text(
            body.get("billing_id"), name="billing_id"
        )
        observation_digest = _digest(
            body.get("observation_digest"),
            name="observation_digest",
        )

        if (
            issuer != receipt.issuer
            or source_class != receipt.source_class
            or source_sha256 != receipt.source_sha256
            or attempt_id != receipt.attempt_id
            or provider_id != receipt.provider_id
            or model_id != receipt.model_id
            or revision != receipt.revision
            or pricing_digest != receipt.pricing_evidence_digest
            or currency != receipt.cost_currency
            or provider_request_id != receipt.provider_request_id
            or provider_response_id != receipt.provider_response_id
            or usage_id != receipt.usage_id
            or billing_id != receipt.billing_id
        ):
            raise ModelObservationEvidenceError(
                "observation artifact does not match trusted issuer receipt"
            )

        material = _observation_material(observation, binding)
        expected_digest = payload_digest(material)
        if (
            observed_at != observation.observed_at
            or incurred != observation.incurred_cost
            or unbilled != observation.estimated_unbilled
            or output_digest != payload_digest(observation.output)
            or observation_digest != expected_digest
        ):
            raise ModelObservationEvidenceError(
                "observation artifact does not bind exact response/usage semantics"
            )

        expected_metadata = {
            key: body[key]
            for key in (
                "schema_version",
                "evidence_type",
                "issuer",
                "source_class",
                "source_sha256",
                "attempt_id",
                "provider_id",
                "model_id",
                "revision",
                "pricing_evidence_digest",
                "cost_currency",
                "observed_at",
                "incurred_cost",
                "estimated_unbilled",
                "output_digest",
                "provider_request_id",
                "provider_response_id",
                "usage_id",
                "billing_id",
                "observation_digest",
            )
        }
        if manifest.get("metadata") != expected_metadata:
            raise ModelObservationEvidenceError(
                "observation artifact metadata scope mismatch"
            )
        if manifest.get("source_refs") != [source_sha256]:
            raise ModelObservationEvidenceError(
                "observation artifact source digest mismatch"
            )

        ModelObservationEvidenceAuthority._validate_binding(
            receipt,
            observation,
            binding,
        )
        return ModelObservationEvidence(
            attempt_id=binding.attempt_id,
            evidence_id=receipt.artifact_id,
            evidence_digest=receipt.object_sha256,
            issuer=issuer,
            observation_digest=expected_digest,
        )
