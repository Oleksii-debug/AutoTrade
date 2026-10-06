"""Exact current provider-Q reconciliation semantics for WP-20 account truth.

This module does not decide that a snapshot is coherent and does not issue an
account cut.  It only resolves the acquisition model and consistency method
from an authenticated, exact-current provider qualification.  Missing semantic
claims fail closed; caller booleans or caller-created policy objects never gain
authority here.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import re
import weakref

from .durable_provider_qualification import (
    DurableProviderQualificationRegistry,
    ProviderQualificationError,
)
from .persistence import (
    JournalStore,
    canonical_json,
    require_exact_journal_store_authority,
)
from .provider_domain import ProviderFinancialScope
from .provider_qualification_authority import AcceptedProviderQualification
from .provider_qualification_current_scope import ProviderQualificationCurrentScope
from .provider_qualification_identity import ProviderQualificationIdentity


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

# This module consumes provider-Q as financial/reconciliation authority.  Pin
# the installed exact types and methods at import so later public class/module
# rebinding cannot replace the durable Q read/currentness boundary.
_REGISTRY_TYPE = DurableProviderQualificationRegistry
_REGISTRY_QUALIFICATION = DurableProviderQualificationRegistry.qualification
_REGISTRY_REQUIRE_EXACT_CURRENT = (
    DurableProviderQualificationRegistry.require_exact_current
)
_CURRENT_SCOPE_TYPE = ProviderQualificationCurrentScope
_ACCEPTED_Q_TYPE = AcceptedProviderQualification
_QUALIFICATION_IDENTITY_TYPE = ProviderQualificationIdentity
_PROVIDER_SCOPE_TYPE = ProviderFinancialScope
_PROVIDER_SCOPE_PAYLOAD = ProviderFinancialScope.payload
_JOURNAL_STORE_TYPE = JournalStore
_REQUIRE_EXACT_JOURNAL_STORE_AUTHORITY = require_exact_journal_store_authority
_CANONICAL_JSON = canonical_json
_SHA256 = sha256
_JSON_LOADS = json.loads
_JSON_DECODE_ERROR = json.JSONDecodeError
_DATETIME_TYPE = datetime
_TIMEZONE_TYPE = timezone


class ProviderAccountReconciliationSemanticsError(ValueError):
    """Provider Q lacks exact current account-reconciliation semantics."""


def _at(value: object) -> datetime:
    # ``DurableProviderQualificationRegistry.current`` presently normalizes
    # datetimes through ``utcoffset``/``astimezone``.  Do not permit a public
    # WP-20 caller to make that lower authority execute caller-owned tzinfo code.
    if type(value) is not _DATETIME_TYPE or type(value.tzinfo) is not _TIMEZONE_TYPE:
        raise ProviderAccountReconciliationSemanticsError(
            "at must be exact datetime with exact datetime.timezone tzinfo"
        )
    return value


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


def _provider_scope_digest_exact(scope: object) -> str:
    if type(scope) is not _PROVIDER_SCOPE_TYPE:
        raise ProviderAccountReconciliationSemanticsError(
            "provider reconciliation semantics Q provider scope is non-canonical"
        )
    payload = _PROVIDER_SCOPE_PAYLOAD(scope)
    return "provider-financial-scope:sha256:" + _SHA256(
        _CANONICAL_JSON(payload).encode("utf-8")
    ).hexdigest()


def _canonical_route_semantics(qualification: object) -> tuple[dict[str, str], str]:
    if type(qualification) is not _ACCEPTED_Q_TYPE:
        raise ProviderAccountReconciliationSemanticsError(
            "provider Q must be exact AcceptedProviderQualification"
        )
    if type(qualification.identity) is not _QUALIFICATION_IDENTITY_TYPE:
        raise ProviderAccountReconciliationSemanticsError(
            "provider Q identity is non-canonical"
        )
    raw = qualification.route_semantics_json
    if type(raw) is not str or not raw:
        raise ProviderAccountReconciliationSemanticsError(
            "provider Q route semantics are unavailable"
        )
    try:
        decoded = _JSON_LOADS(raw)
    except (_JSON_DECODE_ERROR, RecursionError) as error:
        raise ProviderAccountReconciliationSemanticsError(
            "provider Q route semantics are malformed"
        ) from error
    if (
        type(decoded) is not dict
        or any(type(key) is not str or type(value) is not str for key, value in decoded.items())
        or _CANONICAL_JSON(decoded) != raw
    ):
        raise ProviderAccountReconciliationSemanticsError(
            "provider Q route semantics are non-canonical"
        )
    digest = "sha256:" + _SHA256(raw.encode("utf-8")).hexdigest()
    identity = qualification.identity
    if (
        identity.route_semantics_digest != digest
        or type(qualification.qualification_id) is not str
        or _QID_RE.fullmatch(qualification.qualification_id) is None
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
        _REQUIRE_SEMANTICS_AUTHORITY(self)
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
        _REQUIRE_SEMANTICS_AUTHORITY(self)
        payload = {
            "schema_version": _SCHEMA_VERSION,
            "provider_scope_digest": self.provider_scope_digest,
            "qualification_id": self.qualification_id,
            "qualification_route_semantics_digest":
                self.qualification_route_semantics_digest,
            "acquisition_mode": self.acquisition_mode,
            "consistency_method_id": self.consistency_method_id,
            "consistency_method_version": self.consistency_method_version,
        }
        return "sha256:" + _SHA256(
            _CANONICAL_JSON(payload).encode("utf-8")
        ).hexdigest()


_SEMANTICS_TYPE = QualifiedProviderAccountReconciliationSemantics


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
        if type(value) is not _SEMANTICS_TYPE:
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
        if type(store) is not _JOURNAL_STORE_TYPE:
            raise ProviderAccountReconciliationSemanticsError(
                "provider reconciliation Q requires exact JournalStore"
            )
        snapshot = material(value)
        store_identity = _REQUIRE_EXACT_JOURNAL_STORE_AUTHORITY(
            store,
            subject="provider reconciliation Q journal",
        )
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
            type(state[2]) is not _JOURNAL_STORE_TYPE
            or _REQUIRE_EXACT_JOURNAL_STORE_AUTHORITY(
                state[2],
                subject="provider reconciliation Q journal",
            )
            != state[3]
        ):
            raise ProviderAccountReconciliationSemanticsError(
                "provider reconciliation semantics JournalStore generation changed"
            )
        if qualification_registry is not None:
            if type(qualification_registry) is not _REGISTRY_TYPE:
                raise TypeError(
                    "qualification_registry must be exact DurableProviderQualificationRegistry"
                )
            if qualification_registry.store is not state[2]:
                raise ProviderAccountReconciliationSemanticsError(
                    "provider reconciliation semantics and Q registry must share "
                    "the same exact JournalStore generation"
                )
            if _REQUIRE_EXACT_JOURNAL_STORE_AUTHORITY(
                state[2],
                subject="provider reconciliation Q registry journal",
            ) != state[3]:
                raise ProviderAccountReconciliationSemanticsError(
                    "provider reconciliation semantics Q registry store generation changed"
                )
        return value

    return register, require


(
    _register_provider_account_reconciliation_semantics_authority,
    require_provider_account_reconciliation_semantics_authority,
) = _install_semantics_authority()
del _install_semantics_authority
_REQUIRE_SEMANTICS_AUTHORITY = (
    require_provider_account_reconciliation_semantics_authority
)


def _registry_store_cut(
    registry: object,
) -> tuple[JournalStore, object]:
    if type(registry) is not _REGISTRY_TYPE:
        raise TypeError(
            "qualification_registry must be exact DurableProviderQualificationRegistry"
        )
    store = registry.store
    if type(store) is not _JOURNAL_STORE_TYPE:
        raise ProviderAccountReconciliationSemanticsError(
            "provider reconciliation Q registry store is not canonical"
        )
    identity = _REQUIRE_EXACT_JOURNAL_STORE_AUTHORITY(
        store,
        subject="provider reconciliation Q registry journal",
    )
    return store, identity


def _require_registry_store_cut(
    registry: object,
    store: JournalStore,
    identity: object,
) -> None:
    if type(registry) is not _REGISTRY_TYPE or registry.store is not store:
        raise ProviderAccountReconciliationSemanticsError(
            "provider reconciliation Q registry store changed during resolution"
        )
    if _REQUIRE_EXACT_JOURNAL_STORE_AUTHORITY(
        store,
        subject="provider reconciliation Q registry journal",
    ) != identity:
        raise ProviderAccountReconciliationSemanticsError(
            "provider reconciliation Q registry store generation changed during resolution"
        )


def resolve_current_provider_account_reconciliation_semantics(
    *,
    qualification_registry: DurableProviderQualificationRegistry,
    qualification_id: str,
    provider_scope_digest: str,
    at: datetime,
) -> QualifiedProviderAccountReconciliationSemantics:
    """Resolve exact source-owned account reconciliation semantics from current Q."""

    point = _at(at)
    store, store_identity = _registry_store_cut(qualification_registry)
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
        qualification = _REGISTRY_QUALIFICATION(
            qualification_registry,
            qualification_id,
        )
    except ProviderQualificationError as error:
        raise ProviderAccountReconciliationSemanticsError(
            "provider reconciliation semantics Q is not durably accepted"
        ) from error
    _require_registry_store_cut(
        qualification_registry,
        store,
        store_identity,
    )
    if type(qualification) is not _ACCEPTED_Q_TYPE:
        raise ProviderAccountReconciliationSemanticsError(
            "provider reconciliation semantics Q is not exact accepted authority"
        )
    scope = qualification.scope.provider_scope
    if _provider_scope_digest_exact(scope) != provider_scope_digest:
        raise ProviderAccountReconciliationSemanticsError(
            "provider reconciliation semantics Q scope mismatch"
        )
    current_scope = _CURRENT_SCOPE_TYPE(
        provider_scope=scope,
        product_family=qualification.scope.product_family,
        adapter_source_git_sha=qualification.scope.adapter_source_git_sha,
        packaged_artifact_digest=qualification.scope.packaged_artifact_digest,
        protocol_id=qualification.scope.protocol_id,
        protocol_version=qualification.scope.protocol_version,
    )
    try:
        _REGISTRY_REQUIRE_EXACT_CURRENT(
            qualification_registry,
            scope=current_scope,
            at=point,
            expected_qualification_id=qualification_id,
        )
    except ProviderQualificationError as error:
        raise ProviderAccountReconciliationSemanticsError(
            "provider reconciliation semantics Q is not exact current authority"
        ) from error
    _require_registry_store_cut(
        qualification_registry,
        store,
        store_identity,
    )
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
    value = object.__new__(_SEMANTICS_TYPE)
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
        store,
    )
    return value
