"""Issuer-protected serialized-readback provider account cut for WP-20.

The repository already has :class:`ProviderAccountCutIdentity`, deliberately a
public immutable value and not an authority.  This module is the bounded issuer
for the currently qualified BYBIT-style
``SERIALIZED_ACQUISITION_GENERATION + SERIALIZED_SNAPSHOT_READBACK v1`` path.

The cut binds the exact current acquisition, source-owned Q consistency method,
current provider-origin binding set, and exact four-surface searched coverage
window.  It does not decide semantic absence, consistency-horizon expiry,
PROVEN_ABSENT, reservation release, or PAPER/LIVE authorization.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import json
import weakref

from .durable_provider_qualification import DurableProviderQualificationRegistry
from .persistence import canonical_json
from .provider_account_acquisition import (
    DurableProviderAccountAcquisitionAuthority,
    ProviderAccountAcquisitionError,
    SerializedProviderAccountAcquisition,
)
from .provider_account_coverage_set import (
    ProviderAccountCoverageSetError,
    ProviderAccountRequiredSurfaceCoverageSet,
    require_current_provider_account_coverage_set_authority,
)
from .provider_account_cut import ProviderAccountCutIdentity
from .provider_account_origin_set import (
    ProviderAccountOriginBindingSet,
    ProviderAccountOriginSetError,
    require_current_provider_account_origin_set_authority,
)
from .provider_account_reconciliation_semantics import (
    ProviderAccountReconciliationSemanticsError,
    QualifiedProviderAccountReconciliationSemantics,
    resolve_current_provider_account_reconciliation_semantics,
)

_SCHEMA_VERSION = "1.0.0"
_SUPPORTED_ACQUISITION_MODE = "SERIALIZED_ACQUISITION_GENERATION"
_SUPPORTED_CONSISTENCY_METHOD = "SERIALIZED_SNAPSHOT_READBACK"
_SUPPORTED_CONSISTENCY_VERSION = 1


class AcceptedProviderAccountCutError(ValueError):
    """Exact current accepted provider-account cut authority is unavailable."""


def _at(value: object) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise AcceptedProviderAccountCutError(
            "at must be exact timezone-aware datetime"
        )
    return value


def _coverage_window_digest(
    coverage_set: ProviderAccountRequiredSurfaceCoverageSet,
) -> str:
    material = {
        "schema_version": _SCHEMA_VERSION,
        "required_coverage_set_digest": coverage_set.content_digest,
        "surfaces": json.loads(coverage_set.surfaces_json),
    }
    return "sha256:" + sha256(
        canonical_json(material).encode("utf-8")
    ).hexdigest()


def _derive_identity(
    *,
    account_acquisition_authority: DurableProviderAccountAcquisitionAuthority,
    account_acquisition: SerializedProviderAccountAcquisition,
    origin_set: ProviderAccountOriginBindingSet,
    coverage_set: ProviderAccountRequiredSurfaceCoverageSet,
    qualification_registry: DurableProviderQualificationRegistry,
    at: datetime,
) -> tuple[ProviderAccountCutIdentity, QualifiedProviderAccountReconciliationSemantics]:
    if type(account_acquisition_authority) is not DurableProviderAccountAcquisitionAuthority:
        raise TypeError(
            "account_acquisition_authority must be exact "
            "DurableProviderAccountAcquisitionAuthority"
        )
    if type(account_acquisition) is not SerializedProviderAccountAcquisition:
        raise TypeError(
            "account_acquisition must be exact SerializedProviderAccountAcquisition"
        )
    if type(origin_set) is not ProviderAccountOriginBindingSet:
        raise TypeError("origin_set must be exact ProviderAccountOriginBindingSet")
    if type(coverage_set) is not ProviderAccountRequiredSurfaceCoverageSet:
        raise TypeError(
            "coverage_set must be exact ProviderAccountRequiredSurfaceCoverageSet"
        )
    if type(qualification_registry) is not DurableProviderQualificationRegistry:
        raise TypeError(
            "qualification_registry must be exact DurableProviderQualificationRegistry"
        )
    point = _at(at)
    try:
        current_acquisition = account_acquisition_authority.require_current(
            account_acquisition
        )
        require_current_provider_account_origin_set_authority(
            origin_set,
            at=point,
        )
        require_current_provider_account_coverage_set_authority(
            coverage_set,
            at=point,
        )
    except (
        ProviderAccountAcquisitionError,
        ProviderAccountOriginSetError,
        ProviderAccountCoverageSetError,
    ) as error:
        raise AcceptedProviderAccountCutError(
            "account cut sources are not exact current authority"
        ) from error

    try:
        semantics = resolve_current_provider_account_reconciliation_semantics(
            qualification_registry=qualification_registry,
            qualification_id=origin_set.qualification_id,
            provider_scope_digest=origin_set.provider_scope_digest,
            at=point,
        )
    except ProviderAccountReconciliationSemanticsError as error:
        raise AcceptedProviderAccountCutError(
            "account cut reconciliation semantics are not exact current Q"
        ) from error

    if (
        semantics.acquisition_mode != _SUPPORTED_ACQUISITION_MODE
        or semantics.consistency_method_id != _SUPPORTED_CONSISTENCY_METHOD
        or semantics.consistency_method_version != _SUPPORTED_CONSISTENCY_VERSION
    ):
        raise AcceptedProviderAccountCutError(
            "qualified account consistency method is unsupported by serialized-readback issuer"
        )
    if (
        current_acquisition.provider_scope.content_digest
        != origin_set.provider_scope_digest
        or current_acquisition.account_id != origin_set.account_id
        or current_acquisition.acquisition_id != origin_set.acquisition_id
        or current_acquisition.acquisition_generation
        != origin_set.acquisition_generation
        or current_acquisition.acquisition_journal_sequence_cut
        != origin_set.acquisition_journal_sequence_cut
        or coverage_set.provider_scope_digest != origin_set.provider_scope_digest
        or coverage_set.account_id != origin_set.account_id
        or coverage_set.qualification_id != origin_set.qualification_id
        or coverage_set.acquisition_id != origin_set.acquisition_id
        or coverage_set.acquisition_generation != origin_set.acquisition_generation
        or coverage_set.origin_set_digest != origin_set.content_digest
        or semantics.qualification_id != origin_set.qualification_id
        or semantics.provider_scope_digest != origin_set.provider_scope_digest
        or semantics.qualification_route_semantics_digest
        != origin_set.qualification_route_semantics_digest
    ):
        raise AcceptedProviderAccountCutError(
            "account cut sources do not share one acquisition/Q identity"
        )

    identity = ProviderAccountCutIdentity(
        provider_scope=current_acquisition.provider_scope,
        account_id=current_acquisition.account_id,
        acquisition_mode=semantics.acquisition_mode,
        acquisition_id=current_acquisition.acquisition_id,
        acquisition_generation=current_acquisition.acquisition_generation,
        acquisition_journal_sequence_cut=
            current_acquisition.acquisition_journal_sequence_cut,
        qualification_identity_digest=origin_set.qualification_id,
        consistency_method_id=semantics.consistency_method_id,
        consistency_method_version=semantics.consistency_method_version,
        origin_binding_set_digest=origin_set.content_digest,
        stream_binding_set_digest=None,
        backfill_binding_set_digest=None,
        coverage_window_digest=_coverage_window_digest(coverage_set),
        provider_native_generation_token=None,
    )
    return identity, semantics


@dataclass(frozen=True, slots=True, weakref_slot=True, init=False)
class AcceptedProviderAccountCut:
    """Sealed current authority around the canonical account-cut identity value."""

    identity: ProviderAccountCutIdentity
    reconciliation_semantics_digest: str
    origin_set_digest: str
    required_coverage_set_digest: str

    def __init__(self, *_args, **_kwargs) -> None:
        raise AcceptedProviderAccountCutError(
            "accepted provider account cut must come from canonical issuer"
        )

    def payload(self) -> dict[str, object]:
        require_accepted_provider_account_cut_authority(self)
        return {
            "schema_version": _SCHEMA_VERSION,
            "identity": self.identity.payload(),
            "identity_digest": self.identity.content_digest,
            "reconciliation_semantics_digest": self.reconciliation_semantics_digest,
            "origin_set_digest": self.origin_set_digest,
            "required_coverage_set_digest": self.required_coverage_set_digest,
        }

    @property
    def content_digest(self) -> str:
        return "accepted-provider-account-cut:sha256:" + sha256(
            canonical_json(self.payload()).encode("utf-8")
        ).hexdigest()


def _install_accepted_cut_authority():
    states: dict[
        int,
        tuple[
            weakref.ReferenceType,
            tuple[object, ...],
            DurableProviderAccountAcquisitionAuthority,
            SerializedProviderAccountAcquisition,
            weakref.ReferenceType,
            weakref.ReferenceType,
            DurableProviderQualificationRegistry,
        ],
    ] = {}

    def material(value: AcceptedProviderAccountCut) -> tuple[object, ...]:
        if type(value) is not AcceptedProviderAccountCut:
            raise AcceptedProviderAccountCutError(
                "exact AcceptedProviderAccountCut is required"
            )
        return (
            value.identity,
            value.identity.content_digest,
            value.reconciliation_semantics_digest,
            value.origin_set_digest,
            value.required_coverage_set_digest,
        )

    def prune() -> None:
        for object_id, state in tuple(states.items()):
            if state[0]() is None:
                states.pop(object_id, None)

    def register(
        value: AcceptedProviderAccountCut,
        *,
        account_acquisition_authority: DurableProviderAccountAcquisitionAuthority,
        account_acquisition: SerializedProviderAccountAcquisition,
        origin_set: ProviderAccountOriginBindingSet,
        coverage_set: ProviderAccountRequiredSurfaceCoverageSet,
        qualification_registry: DurableProviderQualificationRegistry,
    ) -> None:
        prune()
        if id(value) in states and states[id(value)][0]() is not None:
            raise AcceptedProviderAccountCutError(
                "accepted account-cut authority identity collision"
            )
        states[id(value)] = (
            weakref.ref(value),
            material(value),
            account_acquisition_authority,
            account_acquisition,
            weakref.ref(origin_set),
            weakref.ref(coverage_set),
            qualification_registry,
        )

    def require(value: AcceptedProviderAccountCut) -> AcceptedProviderAccountCut:
        snapshot = material(value)
        prune()
        state = states.get(id(value))
        if state is None or state[0]() is not value:
            raise AcceptedProviderAccountCutError(
                "accepted account-cut construction authority is unavailable"
            )
        if state[1] != snapshot:
            raise AcceptedProviderAccountCutError(
                "accepted account-cut changed after issuance"
            )
        return value

    def require_current(
        value: AcceptedProviderAccountCut,
        *,
        at: datetime,
    ) -> AcceptedProviderAccountCut:
        accepted = require(value)
        state = states.get(id(accepted))
        assert state is not None
        origin_set = state[4]()
        coverage_set = state[5]()
        if origin_set is None or coverage_set is None:
            raise AcceptedProviderAccountCutError(
                "accepted account-cut source authority is unavailable"
            )
        identity, semantics = _derive_identity(
            account_acquisition_authority=state[2],
            account_acquisition=state[3],
            origin_set=origin_set,
            coverage_set=coverage_set,
            qualification_registry=state[6],
            at=_at(at),
        )
        if (
            identity != accepted.identity
            or semantics.content_digest != accepted.reconciliation_semantics_digest
            or origin_set.content_digest != accepted.origin_set_digest
            or coverage_set.content_digest != accepted.required_coverage_set_digest
        ):
            raise AcceptedProviderAccountCutError(
                "accepted account-cut no longer matches current source authority"
            )
        return accepted

    return register, require, require_current


(
    _register_accepted_provider_account_cut_authority,
    require_accepted_provider_account_cut_authority,
    require_current_accepted_provider_account_cut_authority,
) = _install_accepted_cut_authority()
del _install_accepted_cut_authority


def issue_accepted_serialized_readback_account_cut(
    *,
    account_acquisition_authority: DurableProviderAccountAcquisitionAuthority,
    account_acquisition: SerializedProviderAccountAcquisition,
    origin_set: ProviderAccountOriginBindingSet,
    coverage_set: ProviderAccountRequiredSurfaceCoverageSet,
    qualification_registry: DurableProviderQualificationRegistry,
    at: datetime,
) -> AcceptedProviderAccountCut:
    """Issue current account cut from exact sources and Q-selected readback method."""

    identity, semantics = _derive_identity(
        account_acquisition_authority=account_acquisition_authority,
        account_acquisition=account_acquisition,
        origin_set=origin_set,
        coverage_set=coverage_set,
        qualification_registry=qualification_registry,
        at=_at(at),
    )
    value = object.__new__(AcceptedProviderAccountCut)
    object.__setattr__(value, "identity", identity)
    object.__setattr__(
        value,
        "reconciliation_semantics_digest",
        semantics.content_digest,
    )
    object.__setattr__(value, "origin_set_digest", origin_set.content_digest)
    object.__setattr__(
        value,
        "required_coverage_set_digest",
        coverage_set.content_digest,
    )
    _register_accepted_provider_account_cut_authority(
        value,
        account_acquisition_authority=account_acquisition_authority,
        account_acquisition=account_acquisition,
        origin_set=origin_set,
        coverage_set=coverage_set,
        qualification_registry=qualification_registry,
    )
    return value
