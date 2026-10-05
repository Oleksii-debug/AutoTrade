"""Qualified provider-Q absence semantics for WP-20 account truth.

This module is a sealed refinement of the canonical reconciliation semantics
resolved by :mod:`provider_account_reconciliation_semantics`.  It authenticates
only the provider-Q semantic contract needed by a later #697 negative-proof
resolver.  It deliberately does **not** issue ``CoverageSurfaceEvidence``, decide
snapshot consistency, prove pagination/window coverage, issue an account cut, or
produce ``PROVEN_ABSENT``.

A complete provider absence proof still has to compose the exact acquisition,
provider-origin page set, searched submission identity, pagination/cursor
continuity, retention/window coverage, consistency-horizon authority, and this
qualified semantic contract inside the canonical WP-20 issuer.
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
from .provider_account_reconciliation_semantics import (
    ProviderAccountReconciliationSemanticsError,
    QualifiedProviderAccountReconciliationSemantics,
    require_provider_account_reconciliation_semantics_authority,
    resolve_current_provider_account_reconciliation_semantics,
)


_SCHEMA_VERSION = "1.0.0"
_CLAIM_PREFIX = "ACCOUNT_RECONCILIATION_ABSENCE_RULE:"
_EXCLUSION_MEANING = "COMPLETE_QUALIFIED_WINDOW_EXCLUDES_MATCHING_PROVIDER_FACT"
_REQUIRED_SURFACES = (
    "ACTIVITIES",
    "EXECUTIONS",
    "OPEN_ORDERS",
    "ORDER_HISTORY",
)
_RULE_ID_RE = re.compile(r"^[A-Z][A-Z0-9._:/+-]{0,127}$")
_ENDPOINT_RE = re.compile(r"^/[A-Za-z0-9._~!$&'()*+,;=:@/-]{1,255}$")
_QID_RE = re.compile(r"^provider-qualification:sha256:[0-9a-f]{64}$")
_SCOPE_RE = re.compile(r"^provider-financial-scope:sha256:[0-9a-f]{64}$")
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class ProviderAccountAbsenceSemanticsError(ValueError):
    """Exact current provider Q lacks canonical absence semantics."""


def _surface(value: object) -> str:
    if type(value) is not str or value not in _REQUIRED_SURFACES:
        raise ProviderAccountAbsenceSemanticsError(
            "surface must be one canonical WP-20 absence surface"
        )
    return value


def _endpoint(value: object) -> str:
    if (
        type(value) is not str
        or value.startswith("//")
        or value.endswith("/")
        or "//" in value
        or "/./" in value
        or "/../" in value
        or value.endswith("/.")
        or value.endswith("/..")
        or "://" in value
        or "\\" in value
        or "%" in value
        or _ENDPOINT_RE.fullmatch(value) is None
    ):
        raise ProviderAccountAbsenceSemanticsError(
            "endpoint must be a bounded canonical provider-relative path"
        )
    return value


def _rule_id(value: object, *, name: str) -> str:
    if type(value) is not str or _RULE_ID_RE.fullmatch(value) is None:
        raise ProviderAccountAbsenceSemanticsError(
            f"{name} must be a bounded canonical uppercase identity"
        )
    return value


def _version(value: object) -> int:
    if type(value) is not int or value < 1 or value > 999_999_999:
        raise ProviderAccountAbsenceSemanticsError(
            "semantics_version must be a bounded positive exact integer"
        )
    return value


def _rule_payload(
    *,
    surface: str,
    endpoint: str,
    data_entitlement: str,
    query_scope_rule_id: str,
    pagination_rule_id: str,
    retention_rule_id: str,
    consistency_horizon_rule_id: str,
    semantics_version: int,
) -> dict[str, object]:
    return {
        "schema_version": _SCHEMA_VERSION,
        "surface": _surface(surface),
        "endpoint": _endpoint(endpoint),
        "data_entitlement": _rule_id(
            data_entitlement,
            name="data_entitlement",
        ),
        "query_scope_rule_id": _rule_id(
            query_scope_rule_id,
            name="query_scope_rule_id",
        ),
        "pagination_rule_id": _rule_id(
            pagination_rule_id,
            name="pagination_rule_id",
        ),
        "retention_rule_id": _rule_id(
            retention_rule_id,
            name="retention_rule_id",
        ),
        "consistency_horizon_rule_id": _rule_id(
            consistency_horizon_rule_id,
            name="consistency_horizon_rule_id",
        ),
        "semantics_version": _version(semantics_version),
        "exclusion_meaning": _EXCLUSION_MEANING,
    }


def account_reconciliation_absence_route_semantic(
    *,
    surface: str,
    endpoint: str,
    data_entitlement: str,
    query_scope_rule_id: str,
    pagination_rule_id: str,
    retention_rule_id: str,
    consistency_horizon_rule_id: str,
    semantics_version: int,
) -> dict[str, str]:
    """Construct one source-owned absence rule for a provider-Q campaign.

    The returned text is only campaign material.  It gains authority solely when
    it is inside an authenticated accepted exact-current provider qualification.
    """

    payload = _rule_payload(
        surface=surface,
        endpoint=endpoint,
        data_entitlement=data_entitlement,
        query_scope_rule_id=query_scope_rule_id,
        pagination_rule_id=pagination_rule_id,
        retention_rule_id=retention_rule_id,
        consistency_horizon_rule_id=consistency_horizon_rule_id,
        semantics_version=semantics_version,
    )
    return {
        _CLAIM_PREFIX + payload["surface"]: canonical_json(payload),
    }


def _canonical_route_semantics(qualification: object) -> tuple[dict[str, str], str]:
    raw = getattr(qualification, "route_semantics_json", None)
    if type(raw) is not str or not raw:
        raise ProviderAccountAbsenceSemanticsError(
            "provider Q route semantics are unavailable"
        )
    try:
        decoded = json.loads(raw)
    except (json.JSONDecodeError, RecursionError) as error:
        raise ProviderAccountAbsenceSemanticsError(
            "provider Q route semantics are malformed"
        ) from error
    if (
        type(decoded) is not dict
        or any(type(key) is not str or type(value) is not str for key, value in decoded.items())
        or canonical_json(decoded) != raw
    ):
        raise ProviderAccountAbsenceSemanticsError(
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
        raise ProviderAccountAbsenceSemanticsError(
            "provider Q route semantics do not match qualification identity"
        )
    return decoded, digest


def _parse_rule(*, surface: str, raw: object) -> dict[str, object]:
    expected_surface = _surface(surface)
    if type(raw) is not str or not raw:
        raise ProviderAccountAbsenceSemanticsError(
            f"provider Q absence rule for {expected_surface} is unavailable"
        )
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, RecursionError) as error:
        raise ProviderAccountAbsenceSemanticsError(
            f"provider Q absence rule for {expected_surface} is malformed"
        ) from error
    expected_keys = {
        "schema_version",
        "surface",
        "endpoint",
        "data_entitlement",
        "query_scope_rule_id",
        "pagination_rule_id",
        "retention_rule_id",
        "consistency_horizon_rule_id",
        "semantics_version",
        "exclusion_meaning",
    }
    if (
        type(payload) is not dict
        or set(payload) != expected_keys
        or any(type(key) is not str for key in payload)
        or canonical_json(payload) != raw
    ):
        raise ProviderAccountAbsenceSemanticsError(
            f"provider Q absence rule for {expected_surface} is non-canonical"
        )
    if payload.get("surface") != expected_surface:
        raise ProviderAccountAbsenceSemanticsError(
            "provider Q absence claim key/surface mismatch"
        )
    rebuilt = _rule_payload(
        surface=payload["surface"],
        endpoint=payload["endpoint"],
        data_entitlement=payload["data_entitlement"],
        query_scope_rule_id=payload["query_scope_rule_id"],
        pagination_rule_id=payload["pagination_rule_id"],
        retention_rule_id=payload["retention_rule_id"],
        consistency_horizon_rule_id=payload["consistency_horizon_rule_id"],
        semantics_version=payload["semantics_version"],
    )
    if rebuilt != payload:
        raise ProviderAccountAbsenceSemanticsError(
            f"provider Q absence rule for {expected_surface} is non-canonical"
        )
    return {
        **payload,
        "claim_key": _CLAIM_PREFIX + expected_surface,
        "rule_digest": "sha256:" + sha256(raw.encode("utf-8")).hexdigest(),
    }


@dataclass(frozen=True, slots=True, weakref_slot=True, init=False)
class QualifiedProviderAccountAbsenceSemantics:
    """Sealed exact-current Q semantics component for four-surface absence proof."""

    provider_scope_digest: str
    qualification_id: str
    qualification_route_semantics_digest: str
    reconciliation_semantics_digest: str
    rules_json: str

    def __init__(self, *_args, **_kwargs) -> None:
        raise ProviderAccountAbsenceSemanticsError(
            "absence semantics must come from exact current reconciliation Q"
        )

    @property
    def rules(self) -> tuple[dict[str, object], ...]:
        require_provider_account_absence_semantics_authority(self)
        decoded = json.loads(self.rules_json)
        if type(decoded) is not list:
            raise ProviderAccountAbsenceSemanticsError(
                "absence semantics rule set is non-canonical"
            )
        return tuple(dict(item) for item in decoded)

    def payload(self) -> dict[str, object]:
        require_provider_account_absence_semantics_authority(self)
        return {
            "schema_version": _SCHEMA_VERSION,
            "provider_scope_digest": self.provider_scope_digest,
            "qualification_id": self.qualification_id,
            "qualification_route_semantics_digest":
                self.qualification_route_semantics_digest,
            "reconciliation_semantics_digest": self.reconciliation_semantics_digest,
            "rules": json.loads(self.rules_json),
        }

    @property
    def content_digest(self) -> str:
        return "provider-account-absence-semantics:sha256:" + sha256(
            canonical_json(self.payload()).encode("utf-8")
        ).hexdigest()


def _install_absence_semantics_authority():
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
        "reconciliation_semantics_digest",
        "rules_json",
    )

    def material(value: QualifiedProviderAccountAbsenceSemantics) -> tuple[object, ...]:
        if type(value) is not QualifiedProviderAccountAbsenceSemantics:
            raise ProviderAccountAbsenceSemanticsError(
                "exact QualifiedProviderAccountAbsenceSemantics is required"
            )
        return tuple(getattr(value, name) for name in names)

    def prune() -> None:
        for object_id, state in tuple(states.items()):
            if state[0]() is None:
                states.pop(object_id, None)

    def register(
        value: QualifiedProviderAccountAbsenceSemantics,
        store: JournalStore,
    ) -> None:
        if type(store) is not JournalStore:
            raise ProviderAccountAbsenceSemanticsError(
                "provider absence semantics require exact JournalStore"
            )
        snapshot = material(value)
        store_identity = store.store_identity
        prune()
        current = states.get(id(value))
        if current is not None and current[0]() is not None:
            raise ProviderAccountAbsenceSemanticsError(
                "provider absence semantics identity collision"
            )
        states[id(value)] = (
            weakref.ref(value),
            snapshot,
            store,
            store_identity,
        )

    def require(
        value: QualifiedProviderAccountAbsenceSemantics,
        *,
        qualification_registry: DurableProviderQualificationRegistry | None = None,
        at: datetime | None = None,
    ) -> QualifiedProviderAccountAbsenceSemantics:
        snapshot = material(value)
        prune()
        state = states.get(id(value))
        if state is None or state[0]() is not value:
            raise ProviderAccountAbsenceSemanticsError(
                "provider absence semantics construction authority is unavailable"
            )
        if state[1] != snapshot:
            raise ProviderAccountAbsenceSemanticsError(
                "provider absence semantics changed after Q resolution"
            )
        if type(state[2]) is not JournalStore or state[2].store_identity != state[3]:
            raise ProviderAccountAbsenceSemanticsError(
                "provider absence semantics JournalStore generation changed"
            )
        if qualification_registry is not None:
            if type(qualification_registry) is not DurableProviderQualificationRegistry:
                raise TypeError(
                    "qualification_registry must be exact DurableProviderQualificationRegistry"
                )
            if qualification_registry.store is not state[2]:
                raise ProviderAccountAbsenceSemanticsError(
                    "provider absence semantics and Q registry must share "
                    "the same exact JournalStore generation"
                )
            if at is None:
                raise ProviderAccountAbsenceSemanticsError(
                    "current provider absence semantics require an exact consumption time"
                )
            try:
                current_reconciliation = (
                    resolve_current_provider_account_reconciliation_semantics(
                        qualification_registry=qualification_registry,
                        qualification_id=value.qualification_id,
                        provider_scope_digest=value.provider_scope_digest,
                        at=at,
                    )
                )
            except ProviderAccountReconciliationSemanticsError as error:
                raise ProviderAccountAbsenceSemanticsError(
                    "provider absence semantics Q is not exact current authority"
                ) from error
            if (
                current_reconciliation.content_digest
                != value.reconciliation_semantics_digest
                or current_reconciliation.qualification_route_semantics_digest
                != value.qualification_route_semantics_digest
            ):
                raise ProviderAccountAbsenceSemanticsError(
                    "provider absence semantics no longer match current reconciliation Q"
                )
        elif at is not None:
            raise ProviderAccountAbsenceSemanticsError(
                "consumption time requires the exact qualification registry"
            )
        return value

    return register, require


(
    _register_provider_account_absence_semantics_authority,
    require_provider_account_absence_semantics_authority,
) = _install_absence_semantics_authority()
del _install_absence_semantics_authority


def resolve_current_provider_account_absence_semantics(
    *,
    reconciliation_semantics: QualifiedProviderAccountReconciliationSemantics,
    qualification_registry: DurableProviderQualificationRegistry,
    at: datetime,
) -> QualifiedProviderAccountAbsenceSemantics:
    """Resolve all four exact source-owned absence rules from current provider Q."""

    if type(reconciliation_semantics) is not QualifiedProviderAccountReconciliationSemantics:
        raise TypeError(
            "reconciliation_semantics must be exact "
            "QualifiedProviderAccountReconciliationSemantics"
        )
    if type(qualification_registry) is not DurableProviderQualificationRegistry:
        raise TypeError(
            "qualification_registry must be exact DurableProviderQualificationRegistry"
        )
    try:
        accepted_reconciliation = require_provider_account_reconciliation_semantics_authority(
            reconciliation_semantics,
            qualification_registry=qualification_registry,
        )
        fresh_reconciliation = resolve_current_provider_account_reconciliation_semantics(
            qualification_registry=qualification_registry,
            qualification_id=accepted_reconciliation.qualification_id,
            provider_scope_digest=accepted_reconciliation.provider_scope_digest,
            at=at,
        )
    except ProviderAccountReconciliationSemanticsError as error:
        raise ProviderAccountAbsenceSemanticsError(
            "provider reconciliation semantics are not exact current authority"
        ) from error
    if fresh_reconciliation.content_digest != accepted_reconciliation.content_digest:
        raise ProviderAccountAbsenceSemanticsError(
            "provider reconciliation semantics changed before absence resolution"
        )
    if (
        _QID_RE.fullmatch(accepted_reconciliation.qualification_id) is None
        or _SCOPE_RE.fullmatch(accepted_reconciliation.provider_scope_digest) is None
        or _SHA256_RE.fullmatch(
            accepted_reconciliation.qualification_route_semantics_digest
        )
        is None
    ):
        raise ProviderAccountAbsenceSemanticsError(
            "provider reconciliation semantics identity is non-canonical"
        )
    try:
        qualification = qualification_registry.qualification(
            accepted_reconciliation.qualification_id
        )
    except ProviderQualificationError as error:
        raise ProviderAccountAbsenceSemanticsError(
            "provider absence semantics Q is not durably accepted"
        ) from error
    if (
        qualification.scope.provider_scope.content_digest
        != accepted_reconciliation.provider_scope_digest
    ):
        raise ProviderAccountAbsenceSemanticsError(
            "provider absence semantics Q scope mismatch"
        )
    route_semantics, route_digest = _canonical_route_semantics(qualification)
    if route_digest != accepted_reconciliation.qualification_route_semantics_digest:
        raise ProviderAccountAbsenceSemanticsError(
            "provider absence semantics Q route digest mismatch"
        )

    rules: list[dict[str, object]] = []
    missing: list[str] = []
    for surface in _REQUIRED_SURFACES:
        key = _CLAIM_PREFIX + surface
        raw = route_semantics.get(key)
        if raw is None:
            missing.append(surface)
            continue
        rules.append(_parse_rule(surface=surface, raw=raw))
    if missing:
        raise ProviderAccountAbsenceSemanticsError(
            "provider Q lacks source-owned absence rules for: " + ",".join(missing)
        )
    rules.sort(key=lambda rule: rule["surface"])
    rules_json = canonical_json(rules)

    value = object.__new__(QualifiedProviderAccountAbsenceSemantics)
    object.__setattr__(
        value,
        "provider_scope_digest",
        accepted_reconciliation.provider_scope_digest,
    )
    object.__setattr__(
        value,
        "qualification_id",
        accepted_reconciliation.qualification_id,
    )
    object.__setattr__(
        value,
        "qualification_route_semantics_digest",
        accepted_reconciliation.qualification_route_semantics_digest,
    )
    object.__setattr__(
        value,
        "reconciliation_semantics_digest",
        accepted_reconciliation.content_digest,
    )
    object.__setattr__(value, "rules_json", rules_json)
    _register_provider_account_absence_semantics_authority(
        value,
        qualification_registry.store,
    )
    return value


def require_provider_account_absence_rule(
    value: QualifiedProviderAccountAbsenceSemantics,
    *,
    surface: str,
    endpoint: str,
    data_entitlement: str,
    qualification_registry: DurableProviderQualificationRegistry,
    at: datetime,
) -> dict[str, object]:
    """Return one exact qualified rule; never a coverage/absence verdict."""

    accepted = require_provider_account_absence_semantics_authority(
        value,
        qualification_registry=qualification_registry,
        at=at,
    )
    expected_surface = _surface(surface)
    expected_endpoint = _endpoint(endpoint)
    expected_entitlement = _rule_id(
        data_entitlement,
        name="data_entitlement",
    )
    for rule in accepted.rules:
        if rule["surface"] != expected_surface:
            continue
        if (
            rule["endpoint"] != expected_endpoint
            or rule["data_entitlement"] != expected_entitlement
        ):
            raise ProviderAccountAbsenceSemanticsError(
                "qualified absence rule endpoint/entitlement mismatch"
            )
        return dict(rule)
    raise ProviderAccountAbsenceSemanticsError(
        "qualified absence rule surface is unavailable"
    )
