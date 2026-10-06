"""Independent immutable billing authority for production model-call reconciliation.

This module does not route models, invoke inference, or own budget state. It
converts one retained issuer-scoped billing artifact into the BillingEvidence
accepted by DurableModelCallOrchestrator. ArtifactStore authenticates retained
bytes; a separate trusted receipt authenticates who may bill one exact durable
model-call scope.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
import re
from types import MappingProxyType
from typing import Iterable, Mapping
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
from .model_call import BillingEvidence


MODEL_BILLING_MEDIA_TYPE = (
    "application/vnd.autotrade.model-billing-evidence+json;version=1"
)
MODEL_BILLING_EVIDENCE_TYPE = "AUTOTRADE_MODEL_BILLING_EVIDENCE"
MODEL_BILLING_SCHEMA_VERSION = 1
REMOTE_PROVIDER_BILLING_SOURCE = "REMOTE_PROVIDER_BILLING"
LOCAL_RUNTIME_METER_SOURCE = "LOCAL_RUNTIME_METER"

_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_CURRENCY = re.compile(r"^[A-Z0-9]+$")
_TERMINAL_STATES = frozenset({"OBSERVED", "UNKNOWN"})
_BODY_FIELDS = frozenset(
    {
        "schema_version",
        "evidence_type",
        "issuer",
        "source_class",
        "source_sha256",
        "attempt_id",
        "billing_id",
        "provider_id",
        "model_id",
        "revision",
        "cost_currency",
        "pricing_evidence_digest",
        "terminal_state",
        "provider_request_id",
        "provider_response_id",
        "usage_id",
        "observation_digest",
        "billed",
        "observed_at",
    }
)
_SCOPE_FIELDS = frozenset(
    {
        "attempt_id",
        "request_id",
        "request_identity_digest",
        "budget_id",
        "environment",
        "provider_id",
        "model_id",
        "revision",
        "remote",
        "pricing_evidence_id",
        "pricing_evidence_digest",
        "cost_currency",
        "terminal_state",
        "provider_request_id",
        "provider_response_id",
        "usage_id",
        "observation_digest",
        "observation_evidence_id",
        "observation_evidence_digest",
        "observation_evidence_issuer",
    }
)


class ModelBillingEvidenceError(ValueError):
    """Raised when retained billing evidence is not independently authoritative."""


def _text(value: object, *, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ModelBillingEvidenceError(f"{name} must be canonical non-empty text")
    return value


def _optional_text(value: object, *, name: str) -> str | None:
    if value is None:
        return None
    return _text(value, name=name)


def _digest(value: object, *, name: str) -> str:
    text = _text(value, name=name)
    if _DIGEST.fullmatch(text) is None:
        raise ModelBillingEvidenceError(f"{name} must be canonical sha256:<64-hex>")
    return text


def _uuid(value: object, *, name: str) -> str:
    text = _text(value, name=name)
    try:
        canonical = str(UUID(text))
    except (ValueError, TypeError, AttributeError) as error:
        raise ModelBillingEvidenceError(f"{name} must be UUID") from error
    if canonical != text:
        raise ModelBillingEvidenceError(f"{name} must use canonical UUID text")
    return canonical


def _utc(value: object, *, name: str) -> str:
    from datetime import datetime, timezone

    text = _text(value, name=name)
    if not text.endswith("Z"):
        raise ModelBillingEvidenceError(f"{name} must be canonical UTC text")
    try:
        point = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as error:
        raise ModelBillingEvidenceError(f"{name} must be canonical UTC text") from error
    canonical = point.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if canonical != text:
        raise ModelBillingEvidenceError(f"{name} must be canonical UTC text")
    return text


def _currency(value: object) -> str:
    value = _text(value, name="cost_currency")
    if _CURRENCY.fullmatch(value) is None:
        raise ModelBillingEvidenceError(
            "cost_currency must be uppercase alphanumeric text"
        )
    return value


def _amount(value: object) -> Decimal:
    if type(value) is not str:
        raise ModelBillingEvidenceError("billed must be canonical decimal text")
    try:
        parsed = parse_bounded_exact_decimal(value)
        rendered = canonical_decimal_text(parsed)
    except (ExactDecimalError, TypeError, ValueError) as error:
        raise ModelBillingEvidenceError(
            "billed must be bounded canonical decimal text"
        ) from error
    if parsed < 0 or rendered != value:
        raise ModelBillingEvidenceError(
            "billed must be non-negative canonical decimal text"
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
        raise ModelBillingEvidenceError(
            "billing evidence is not canonical JSON"
        ) from error


@dataclass(frozen=True, slots=True)
class TrustedBillingArtifactReceipt:
    """Out-of-band issuer trust for one immutable invoice/usage line."""

    artifact_id: str
    object_sha256: str
    source_sha256: str
    issuer: str
    source_class: str
    attempt_id: str
    billing_id: str
    provider_id: str
    model_id: str
    revision: str | None
    cost_currency: str
    pricing_evidence_digest: str
    terminal_state: str
    provider_request_id: str | None = None
    provider_response_id: str | None = None
    usage_id: str | None = None
    observation_digest: str | None = None

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
            "billing_id",
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
        object.__setattr__(self, "cost_currency", _currency(self.cost_currency))
        object.__setattr__(
            self,
            "pricing_evidence_digest",
            _digest(
                self.pricing_evidence_digest,
                name="pricing_evidence_digest",
            ),
        )
        source_class = _text(self.source_class, name="source_class")
        if source_class not in {
            REMOTE_PROVIDER_BILLING_SOURCE,
            LOCAL_RUNTIME_METER_SOURCE,
        }:
            raise ModelBillingEvidenceError("unsupported billing source_class")
        object.__setattr__(self, "source_class", source_class)
        terminal_state = _text(self.terminal_state, name="terminal_state")
        if terminal_state not in _TERMINAL_STATES:
            raise ModelBillingEvidenceError("unsupported billing terminal_state")
        object.__setattr__(self, "terminal_state", terminal_state)
        for field_name in (
            "provider_request_id",
            "provider_response_id",
            "usage_id",
        ):
            object.__setattr__(
                self,
                field_name,
                _optional_text(getattr(self, field_name), name=field_name),
            )
        if self.observation_digest is not None:
            object.__setattr__(
                self,
                "observation_digest",
                _digest(self.observation_digest, name="observation_digest"),
            )
        if terminal_state == "UNKNOWN":
            if any(
                value is not None
                for value in (
                    self.provider_request_id,
                    self.provider_response_id,
                    self.usage_id,
                    self.observation_digest,
                )
            ):
                raise ModelBillingEvidenceError(
                    "UNKNOWN billing receipt cannot claim unobserved response lineage"
                )
        elif self.observation_digest is None:
            raise ModelBillingEvidenceError(
                "OBSERVED billing receipt requires observation_digest"
            )
        if (
            source_class == LOCAL_RUNTIME_METER_SOURCE
            and (
                self.provider_request_id is not None
                or self.provider_response_id is not None
            )
        ):
            raise ModelBillingEvidenceError(
                "local runtime billing receipt cannot claim remote provider identities"
            )


class ModelBillingEvidenceAuthority:
    """Callable production BillingEvidenceResolver with a private trusted root."""

    def __init__(
        self,
        *,
        evidence_root: str | Path,
        publication_store: ArtifactStore,
        trusted_receipts: Iterable[TrustedBillingArtifactReceipt],
    ) -> None:
        if type(publication_store) is not ArtifactStore:
            raise TypeError("publication_store must be exact canonical ArtifactStore")
        receipts = tuple(trusted_receipts)
        if not receipts or any(
            type(item) is not TrustedBillingArtifactReceipt for item in receipts
        ):
            raise TypeError(
                "trusted_receipts must contain exact TrustedBillingArtifactReceipt values"
            )
        by_billing_id: dict[str, TrustedBillingArtifactReceipt] = {}
        for receipt in receipts:
            if receipt.billing_id in by_billing_id:
                raise ModelBillingEvidenceError(
                    "duplicate trusted billing identity"
                )
            by_billing_id[receipt.billing_id] = receipt
        try:
            self._read = trusted_authenticated_reader(
                evidence_root,
                publication_store=publication_store,
            )
        except Exception as error:
            raise ModelBillingEvidenceError(
                "trusted billing evidence root cannot be established"
            ) from error
        self._receipts = MappingProxyType(by_billing_id)

    def __call__(
        self,
        attempt_id: str,
        billing_id: str,
        scope: Mapping[str, object],
    ) -> BillingEvidence:
        attempt = _text(attempt_id, name="attempt_id")
        billing = _text(billing_id, name="billing_id")
        if not isinstance(scope, Mapping) or set(scope) != _SCOPE_FIELDS:
            raise ModelBillingEvidenceError(
                "billing resolver scope is not the closed durable scope"
            )
        receipt = self._receipts.get(billing)
        if receipt is None:
            raise ModelBillingEvidenceError(
                "billing identity has no independently trusted issuer receipt"
            )
        if receipt.attempt_id != attempt:
            raise ModelBillingEvidenceError(
                "billing receipt does not match durable attempt"
            )
        self._validate_scope(receipt, scope)
        try:
            manifest, raw = self._read(receipt.artifact_id)
        except Exception as error:
            raise ModelBillingEvidenceError(
                "trusted billing artifact cannot be authenticated"
            ) from error
        return self._resolve(
            receipt=receipt,
            manifest=manifest,
            raw=raw,
            scope=scope,
        )

    @staticmethod
    def _validate_scope(
        receipt: TrustedBillingArtifactReceipt,
        scope: Mapping[str, object],
    ) -> None:
        attempt = _text(scope.get("attempt_id"), name="attempt_id")
        provider_id = _text(scope.get("provider_id"), name="provider_id")
        model_id = _text(scope.get("model_id"), name="model_id")
        revision = _optional_text(scope.get("revision"), name="revision")
        currency = _currency(scope.get("cost_currency"))
        pricing_digest = _digest(
            scope.get("pricing_evidence_digest"),
            name="pricing_evidence_digest",
        )
        terminal_state = _text(scope.get("terminal_state"), name="terminal_state")
        remote = scope.get("remote")
        if type(remote) is not bool:
            raise ModelBillingEvidenceError("billing scope remote must be exact bool")
        provider_request_id = _optional_text(
            scope.get("provider_request_id"), name="provider_request_id"
        )
        provider_response_id = _optional_text(
            scope.get("provider_response_id"), name="provider_response_id"
        )
        usage_id = _optional_text(scope.get("usage_id"), name="usage_id")
        observation_value = scope.get("observation_digest")
        observation_digest = (
            None
            if observation_value is None
            else _digest(observation_value, name="observation_digest")
        )
        expected_remote = receipt.source_class == REMOTE_PROVIDER_BILLING_SOURCE
        if remote is not expected_remote:
            raise ModelBillingEvidenceError(
                "billing source_class cannot cross remote/local scope"
            )
        if (
            receipt.attempt_id != attempt
            or receipt.provider_id != provider_id
            or receipt.model_id != model_id
            or receipt.revision != revision
            or receipt.cost_currency != currency
            or receipt.pricing_evidence_digest != pricing_digest
            or receipt.terminal_state != terminal_state
            or receipt.provider_request_id != provider_request_id
            or receipt.provider_response_id != provider_response_id
            or receipt.usage_id != usage_id
            or receipt.observation_digest != observation_digest
        ):
            raise ModelBillingEvidenceError(
                "trusted billing receipt does not match durable model-call scope"
            )

    @staticmethod
    def _resolve(
        *,
        receipt: TrustedBillingArtifactReceipt,
        manifest: object,
        raw: object,
        scope: Mapping[str, object],
    ) -> BillingEvidence:
        if type(manifest) is not dict or type(raw) is not bytes:
            raise ModelBillingEvidenceError(
                "billing artifact representation is invalid"
            )
        if manifest.get("artifact_id") != receipt.artifact_id:
            raise ModelBillingEvidenceError("billing artifact identity mismatch")
        object_digest = manifest.get("sha256")
        if object_digest != receipt.object_sha256:
            raise ModelBillingEvidenceError("billing artifact digest is not trusted")
        if object_digest != "sha256:" + sha256(raw).hexdigest():
            raise ModelBillingEvidenceError(
                "billing artifact object digest mismatch"
            )
        if manifest.get("media_type") != MODEL_BILLING_MEDIA_TYPE:
            raise ModelBillingEvidenceError("billing artifact media type mismatch")
        rights = manifest.get("rights")
        if type(rights) is not dict or rights.get("storage") is not True:
            raise ModelBillingEvidenceError(
                "billing artifact lacks storage provenance"
            )

        try:
            decoded = raw.decode("utf-8", errors="strict")
            body = strict_json_loads(decoded)
        except (UnicodeError, TypeError, ValueError) as error:
            raise ModelBillingEvidenceError(
                "billing artifact JSON is invalid"
            ) from error
        if type(body) is not dict or set(body) != _BODY_FIELDS:
            raise ModelBillingEvidenceError("billing artifact schema is not closed")
        if _canonical_json_bytes(body) != raw:
            raise ModelBillingEvidenceError(
                "billing artifact bytes are not canonical JSON"
            )
        if body.get("schema_version") != MODEL_BILLING_SCHEMA_VERSION:
            raise ModelBillingEvidenceError(
                "billing artifact schema_version mismatch"
            )
        if body.get("evidence_type") != MODEL_BILLING_EVIDENCE_TYPE:
            raise ModelBillingEvidenceError(
                "billing artifact evidence_type mismatch"
            )

        issuer = _text(body.get("issuer"), name="issuer")
        source_class = _text(body.get("source_class"), name="source_class")
        source_sha256 = _digest(body.get("source_sha256"), name="source_sha256")
        attempt_id = _text(body.get("attempt_id"), name="attempt_id")
        billing_id = _text(body.get("billing_id"), name="billing_id")
        provider_id = _text(body.get("provider_id"), name="provider_id")
        model_id = _text(body.get("model_id"), name="model_id")
        revision = _optional_text(body.get("revision"), name="revision")
        currency = _currency(body.get("cost_currency"))
        pricing_digest = _digest(
            body.get("pricing_evidence_digest"),
            name="pricing_evidence_digest",
        )
        terminal_state = _text(body.get("terminal_state"), name="terminal_state")
        if terminal_state not in _TERMINAL_STATES:
            raise ModelBillingEvidenceError(
                "billing artifact terminal_state is invalid"
            )
        provider_request_id = _optional_text(
            body.get("provider_request_id"), name="provider_request_id"
        )
        provider_response_id = _optional_text(
            body.get("provider_response_id"), name="provider_response_id"
        )
        usage_id = _optional_text(body.get("usage_id"), name="usage_id")
        observation_value = body.get("observation_digest")
        observation_digest = (
            None
            if observation_value is None
            else _digest(observation_value, name="observation_digest")
        )
        billed = _amount(body.get("billed"))
        observed_at = _utc(body.get("observed_at"), name="observed_at")

        if (
            issuer != receipt.issuer
            or source_class != receipt.source_class
            or source_sha256 != receipt.source_sha256
            or attempt_id != receipt.attempt_id
            or billing_id != receipt.billing_id
            or provider_id != receipt.provider_id
            or model_id != receipt.model_id
            or revision != receipt.revision
            or currency != receipt.cost_currency
            or pricing_digest != receipt.pricing_evidence_digest
            or terminal_state != receipt.terminal_state
            or provider_request_id != receipt.provider_request_id
            or provider_response_id != receipt.provider_response_id
            or usage_id != receipt.usage_id
            or observation_digest != receipt.observation_digest
        ):
            raise ModelBillingEvidenceError(
                "billing artifact does not match trusted issuer receipt"
            )
        if source_class == LOCAL_RUNTIME_METER_SOURCE and billed != Decimal("0"):
            raise ModelBillingEvidenceError(
                "local runtime billing authority may only attest exact zero external cost"
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
                "billing_id",
                "provider_id",
                "model_id",
                "revision",
                "cost_currency",
                "pricing_evidence_digest",
                "terminal_state",
                "provider_request_id",
                "provider_response_id",
                "usage_id",
                "observation_digest",
                "billed",
                "observed_at",
            )
        }
        if manifest.get("metadata") != expected_metadata:
            raise ModelBillingEvidenceError(
                "billing artifact metadata scope mismatch"
            )
        if manifest.get("source_refs") != [source_sha256]:
            raise ModelBillingEvidenceError(
                "billing artifact source digest mismatch"
            )

        # Re-check the durable scope after parsing retained evidence so neither
        # artifact metadata nor receipt selection can loosen the call identity.
        ModelBillingEvidenceAuthority._validate_scope(receipt, scope)

        return BillingEvidence(
            attempt_id=attempt_id,
            billing_id=billing_id,
            provider_id=provider_id,
            model_id=model_id,
            revision=revision,
            billed=billed,
            cost_currency=currency,
            observed_at=observed_at,
            evidence_id=receipt.artifact_id,
            evidence_digest=receipt.object_sha256,
            issuer=issuer,
        )
