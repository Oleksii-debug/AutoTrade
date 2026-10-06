"""Exact current provider-Q reconciliation semantics for WP-20 account truth.

This module does not decide that a snapshot is coherent and does not issue an
account cut.  It only resolves the acquisition model and consistency method
from an authenticated, exact-current provider qualification.  Missing semantic
claims fail closed; caller booleans or caller-created policy objects never gain
authority here.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import json
import re
import weakref

from .durable_provider_qualification import (
    DurableProviderQualificationRegistry,
    ProviderQualificationError,
)
from .persistence import JournalStore, canonical_json
from .provider_qualification_current_scope import ProviderQualificationCurrentScope


_SCHEMA_VERSION = "1.0.0"
_KEY_SCHEMA = "ACCOUNT_RECONCILIATION_SCHEMA_VERSION"
_KEY_ACQUISITION = "ACCOUNT_RECONCILIATION_ACQUISITION_MODE"
_KEY_METHOD = "ACCOUNT_RECONCILIATION_CONSISTENCY_METHOD_ID"
_KEY_METHOD_VERSION = "ACCOUNT_RECONCILIATION_CONSISTENCY_METHOD_VERSION"
_ALLOWED_ACQUISITION_MODES = frozenset(
    {
        "PROVIDER_NATIVE_GENERATION",
        "SERIALIZED_ACQUISITION_GENERATION",
    }
)
_METHOD_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_VERSION_RE = re.compile(r"^[1-9][0-9]{0,8}$")
_QID_RE = re.compile(r"^provider-qualification:sha256:[0-9a-f]{64}$")
_SCOPE_RE = re.compile(r"^provider-financial-scope:sha256:[0-9a-f]{64}$")


class ProviderAccountReconciliationSemanticsError(ValueError):
    """Provider Q lacks exact current account-reconciliation semantics."""


def account_reconciliation_route_semantics(
    *,
    acquisition_mode: str,
    consistency_method_id: str,
    consistency_method_version: int,
) -> dict[str, str]:
    """Canonical semantic claims for an authenticated provider-Q campaign.

    This helper constructs claim text only.  It does not authenticate or accept
    a provider qualification and therefore is not account-truth authority.
    """

    if type(acquisition_mode) is not str or acquisition_mode not in _ALLOWED_ACQUISITION_MODES:
        raise ProviderAccountReconciliationSemanticsError(
            "acquisition_mode is not a supported exact reconciliation model"
        )
    if (
        type(consistency_method_id) is not str
        or _METHOD_RE.fullmatch(consistency_method_id) is None
    ):
        raise ProviderAccountReconciliationSemanticsError(
            "consistency_method_id must be canonical bounded uppercase identity"
        )
    if (
        type(consistency_method_version) is not int
        or consistency_method_version < 1
        or consistency_method_version > 999_999_999
    ):
        raise ProviderAccountReconciliationSemanticsError(
            "consistency_method_version must be a bounded positive exact integer"
        )
    return {
        _KEY_SCHEMA: _SCHEMA_VERSION,
        _KEY_ACQUISITION: acquisition_mode,
        _KEY_METHOD: consistency_method_id,
        _KEY_METHOD_VERSION: str(consistency_method_version),
    }


def _canonical_route_semantics(qualification: object) -> tuple[dict[str, str], str]:
    raw = getattr(qualification, "route_semantics_json", None)
    if type(raw) is not str or not raw:
        raise ProviderAccountReconciliationSemanticsError(
            "provider Q route semantics are unavailable"
        )
    try:
        decoded = json.loads(raw)
    except (json.JSONDecodeError, RecursionError) as error:
        raise ProviderAccountReconciliationSemanticsError(
            "provider Q route semantics are malformed"
        ) from error
    if (
        type(decoded) is not dict
        or any(type(key) is not str or type(value) is not str for key, value in decoded.items())
        or canonical_json(decoded) != raw
    ):
        raise ProviderAccountReconciliationSemanticsError(
            "provider Q route semantics are non-canonical"
        )
    digest = "sha256:" + sha256(raw.encode("utf-8")).hexdigest()
    identity = getattr(qualification, "identity", None)
    if (
        identity is None
        or getattr(identity, "route_semantics_digest", None) != digest
        or getattr(identity, "content_digest", None)
        != getattr(qualification, "qualification_id", None)
    ):
        raise ProviderAccountReconciliationSemanticsError(
            "provider Q route semantics do not match qualification identity"
        )
    return decoded, digest


@dataclass(frozen=True, slots=True, weakref_slot=True, init=False)
class QualifiedProviderAccountReconciliationSemantics:
    provider_scope_digest: str
    qualification_id: str
    qualification_route_semantics_digest: str
    acquisition_mode: str
    consistency_method_id: str
    consistency_method_version: int

    def __init__(self, *_args, **_kwargs) -> None:
        raise ProviderAccountReconciliationSemanticsError(
            "reconciliation semantics must come from exact current provider Q"
        )

    def payload(self) -> dict[str, object]:
        require_provider_account_reconciliation_semantics_authority(self)
        return {
            "schema_version": _SCHEMA_VERSION,
            "provider_scope_digest": self.provider_scope_digest,
            "qualification_id": self.qualification_id,
            "qualification_route_semantics_digest":
                self.qualification_route_semantics_digest,
            "acquisition_mode": self.acquisition_mode,
            "consistency_method_id": self.consistency_method_id,
            "consistency_method_version": self.consistency_method_version,
        }

    @property
    def content_digest(self) -> str:
        return "sha256:" + sha256(
            canonical_json(self.payload()).encode("utf-8")
        ).hexdigest()


def _install_semantics_authority():
    states: dict[
        int,
        tuple[
            weakref.ReferenceType,
            tuple[object, ...],
            JournalStore,
            object,
        ],
    ] = {}
    names = (
        "provider_scope_digest",
        "qualification_id",
        "qualification_route_semantics_digest",
        "acquisition_mode",
        "consistency_method_id",
        "consistency_method_version",
    )

    def material(value: QualifiedProviderAccountReconciliationSemantics) -> tuple[object, ...]:
        if type(value) is not QualifiedProviderAccountReconciliationSemantics:
            raise ProviderAccountReconciliationSemanticsError(
                "exact QualifiedProviderAccountReconciliationSemantics is required"
            )
        return tuple(getattr(value, name) for name in names)

    def prune() -> None:
        for object_id, state in tuple(states.items()):
            if state[0]() is None:
                states.pop(object_id, None)

    def register(
        value: QualifiedProviderAccountReconciliationSemantics,
        store: JournalStore,
    ) -> None:
        if type(store) is not JournalStore:
            raise ProviderAccountReconciliationSemanticsError(
                "provider reconciliation Q requires exact JournalStore"
            )
        snapshot = material(value)
        store_identity = store.store_identity
        prune()
        current = states.get(id(value))
        if current is not None and current[0]() is not None:
            raise ProviderAccountReconciliationSemanticsError(
                "provider reconciliation semantics identity collision"
            )
        states[id(value)] = (
            weakref.ref(value),
            snapshot,
            store,
            store_identity,
        )

    def require(
        value: QualifiedProviderAccountReconciliationSemantics,
        *,
        qualification_registry: DurableProviderQualificationRegistry | None = None,
    ) -> QualifiedProviderAccountReconciliationSemantics:
        snapshot = material(value)
        prune()
        state = states.get(id(value))
        if state is None or state[0]() is not value:
            raise ProviderAccountReconciliationSemanticsError(
                "provider reconciliation semantics construction authority is unavailable"
            )
        if state[1] != snapshot:
            raise ProviderAccountReconciliationSemanticsError(
                "provider reconciliation semantics changed after Q resolution"
            )
        if (
            type(state[2]) is not JournalStore
            or state[2].store_identity != state[3]
        ):
            raise ProviderAccountReconciliationSemanticsError(
                "provider reconciliation semantics JournalStore generation changed"
            )
        if qualification_registry is not None:
            if type(qualification_registry) is not DurableProviderQualificationRegistry:
                raise TypeError(
                    "qualification_registry must be exact DurableProviderQualificationRegistry"
                )
            if qualification_registry.store is not state[2]:
                raise ProviderAccountReconciliationSemanticsError(
                    "provider reconciliation semantics and Q registry must share "
                    "the same exact JournalStore generation"
                )
        return value

    return register, require


(
    _register_provider_account_reconciliation_semantics_authority,
    require_provider_account_reconciliation_semantics_authority,
) = _install_semantics_authority()
del _install_semantics_authority


def resolve_current_provider_account_reconciliation_semantics(
    *,
    qualification_registry: DurableProviderQualificationRegistry,
    qualification_id: str,
    provider_scope_digest: str,
    at: datetime,
) -> QualifiedProviderAccountReconciliationSemantics:
    """Resolve exact source-owned account reconciliation semantics from current Q."""

    if type(qualification_registry) is not DurableProviderQualificationRegistry:
        raise TypeError(
            "qualification_registry must be exact DurableProviderQualificationRegistry"
        )
    if type(qualification_id) is not str or _QID_RE.fullmatch(qualification_id) is None:
        raise ProviderAccountReconciliationSemanticsError(
            "qualification_id is non-canonical"
        )
    if (
        type(provider_scope_digest) is not str
        or _SCOPE_RE.fullmatch(provider_scope_digest) is None
    ):
        raise ProviderAccountReconciliationSemanticsError(
            "provider_scope_digest is non-canonical"
        )
    try:
        qualification = qualification_registry.qualification(qualification_id)
    except ProviderQualificationError as error:
        raise ProviderAccountReconciliationSemanticsError(
            "provider reconciliation semantics Q is not durably accepted"
        ) from error
    scope = qualification.scope.provider_scope
    if scope.content_digest != provider_scope_digest:
        raise ProviderAccountReconciliationSemanticsError(
            "provider reconciliation semantics Q scope mismatch"
        )
    current_scope = ProviderQualificationCurrentScope(
        provider_scope=scope,
        product_family=qualification.scope.product_family,
        adapter_source_git_sha=qualification.scope.adapter_source_git_sha,
        packaged_artifact_digest=qualification.scope.packaged_artifact_digest,
        protocol_id=qualification.scope.protocol_id,
        protocol_version=qualification.scope.protocol_version,
    )
    try:
        qualification_registry.require_exact_current(
            scope=current_scope,
            at=at,
            expected_qualification_id=qualification_id,
        )
    except ProviderQualificationError as error:
        raise ProviderAccountReconciliationSemanticsError(
            "provider reconciliation semantics Q is not exact current authority"
        ) from error
    route_semantics, route_digest = _canonical_route_semantics(qualification)
    required = {
        _KEY_SCHEMA,
        _KEY_ACQUISITION,
        _KEY_METHOD,
        _KEY_METHOD_VERSION,
    }
    missing = sorted(required - set(route_semantics))
    if missing:
        raise ProviderAccountReconciliationSemanticsError(
            "provider Q lacks source-owned account reconciliation semantics: "
            + ",".join(missing)
        )
    if route_semantics[_KEY_SCHEMA] != _SCHEMA_VERSION:
        raise ProviderAccountReconciliationSemanticsError(
            "provider Q account reconciliation schema is unsupported"
        )
    version_text = route_semantics[_KEY_METHOD_VERSION]
    if type(version_text) is not str or _VERSION_RE.fullmatch(version_text) is None:
        raise ProviderAccountReconciliationSemanticsError(
            "provider Q consistency_method_version is non-canonical"
        )
    version = int(version_text)
    claims = account_reconciliation_route_semantics(
        acquisition_mode=route_semantics[_KEY_ACQUISITION],
        consistency_method_id=route_semantics[_KEY_METHOD],
        consistency_method_version=version,
    )
    if any(route_semantics.get(key) != value for key, value in claims.items()):
        raise ProviderAccountReconciliationSemanticsError(
            "provider Q account reconciliation semantics are non-canonical"
        )
    value = object.__new__(QualifiedProviderAccountReconciliationSemantics)
    object.__setattr__(value, "provider_scope_digest", provider_scope_digest)
    object.__setattr__(value, "qualification_id", qualification_id)
    object.__setattr__(
        value,
        "qualification_route_semantics_digest",
        route_digest,
    )
    object.__setattr__(value, "acquisition_mode", claims[_KEY_ACQUISITION])
    object.__setattr__(value, "consistency_method_id", claims[_KEY_METHOD])
    object.__setattr__(
        value,
        "consistency_method_version",
        version,
    )
    _register_provider_account_reconciliation_semantics_authority(
        value,
        qualification_registry.store,
    )
    return value
