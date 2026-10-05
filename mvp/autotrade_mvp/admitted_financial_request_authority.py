"""Issuer-bound authority for one exact durable admitted provider request.

This module deliberately does **not** grant provider qualification or wire-send
permission.  It seals the exact request identity already owned by
``DurableFinancialRequestBindingRegistry`` into a process capability that cannot
be caller-constructed or retargeted.  Product composition may later require this
value together with current provider qualification, account/risk truth, recovery
ownership, credentials and transport authority before an irreversible byte.

The issuer accepts only an admission id.  Financial material, account cuts,
qualification identity, provider scope and request/submission digests are always
reconstructed from the durable canonical registry on the issuer's exact
``JournalStore`` generation.  Consumption resolves that durable binding again,
so the capability is not a detached replacement for the existing authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from threading import RLock
from typing import Any, Mapping
from weakref import WeakKeyDictionary, ref

from .durable_financial_request_binding import (
    DurableFinancialRequestBindingError,
    DurableFinancialRequestBindingRegistry,
)
from .financial_binding_dispatch import financial_submission_scope
from .financial_request_binding import FinancialRequestBindingMaterial
from .persistence import JournalStore, canonical_json


class AdmittedFinancialRequestAuthorityError(PermissionError):
    """Raised when admitted request authority is absent, forged or retargeted."""


@dataclass(frozen=True, slots=True)
class AdmittedFinancialRequestIdentity:
    """Non-authorizing diagnostic identity of one issued request capability."""

    admission_id: str
    binding_id: str
    risk_snapshot_id: str
    risk_decision_id: str
    account_cut_id: str
    qualification_identity_digest: str
    capability_snapshot_id: str
    provider_scope_digest: str
    request_sha256: str
    submission_scope_digest: str


def _exact_text(value: object, *, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise AdmittedFinancialRequestAuthorityError(
            f"{name} must be exact canonical non-empty text"
        )
    return value


def _exact_json_value(value: object, *, name: str) -> object:
    """Detach exact JSON-domain values without caller-polymorphic execution."""

    if value is None or type(value) in {str, int, bool}:
        return value
    if type(value) is list:
        return [
            _exact_json_value(item, name=f"{name}[{index}]")
            for index, item in enumerate(list.copy(value))
        ]
    if type(value) is dict:
        if any(type(key) is not str for key in value):
            raise TypeError(f"{name} keys must be exact strings")
        detached = dict.copy(value)
        return {
            key: _exact_json_value(item, name=f"{name}.{key}")
            for key, item in detached.items()
        }
    raise TypeError(f"{name} must contain exact JSON-domain values")


def _detached_object(value: object, *, name: str) -> dict[str, Any]:
    """Snapshot an exact built-in JSON object before financial comparison."""

    if type(value) is not dict:
        raise TypeError(f"{name} must be an exact dict")
    detached = _exact_json_value(value, name=name)
    if type(detached) is not dict:
        raise TypeError(f"{name} must be an exact dict")
    return detached


def _json_digest(value: dict[str, Any]) -> str:
    if type(value) is not dict:
        raise TypeError("digest value must be an exact dict")
    return "sha256:" + sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _identity(
    admission_id: str,
    material: FinancialRequestBindingMaterial,
) -> AdmittedFinancialRequestIdentity:
    return AdmittedFinancialRequestIdentity(
        admission_id=admission_id,
        binding_id=material.binding_id,
        risk_snapshot_id=material.risk_snapshot_id,
        risk_decision_id=material.risk_decision_id,
        account_cut_id=material.account_cut_id,
        qualification_identity_digest=material.qualification_identity_digest,
        capability_snapshot_id=material.capability_snapshot_id,
        provider_scope_digest=material.provider_scope_digest,
        request_sha256=material.request_sha256,
        submission_scope_digest=material.submission_scope_digest,
    )


def _install_authority_types():
    """Keep minting/binding state outside module- and instance-writable data."""

    issuer_states: WeakKeyDictionary[object, DurableFinancialRequestBindingRegistry] = (
        WeakKeyDictionary()
    )
    authority_states: WeakKeyDictionary[
        object,
        tuple[object, str, str],
    ] = WeakKeyDictionary()
    lock = RLock()

    class AdmittedFinancialRequestAuthority:
        __slots__ = ("__weakref__",)

        def __new__(cls, *_args, **_kwargs):
            raise AdmittedFinancialRequestAuthorityError(
                "admitted financial request authority must be issued"
            )

        def __copy__(self):
            raise AdmittedFinancialRequestAuthorityError(
                "admitted financial request authority cannot be copied"
            )

        def __deepcopy__(self, _memo):
            raise AdmittedFinancialRequestAuthorityError(
                "admitted financial request authority cannot be copied"
            )

        def __reduce__(self):
            raise AdmittedFinancialRequestAuthorityError(
                "admitted financial request authority cannot be serialized"
            )

        def __reduce_ex__(self, _protocol):
            raise AdmittedFinancialRequestAuthorityError(
                "admitted financial request authority cannot be serialized"
            )

    class AdmittedFinancialRequestAuthorityIssuer:
        __slots__ = ("__weakref__",)

        def __init__(self, store: JournalStore) -> None:
            if type(store) is not JournalStore:
                raise TypeError("store must be exact JournalStore")
            registry = DurableFinancialRequestBindingRegistry(store)
            with lock:
                if self in issuer_states:
                    raise AdmittedFinancialRequestAuthorityError(
                        "admitted request issuer is already initialized"
                    )
                issuer_states[self] = registry

        def _registry(self) -> DurableFinancialRequestBindingRegistry:
            with lock:
                registry = issuer_states.get(self)
            if registry is None:
                raise AdmittedFinancialRequestAuthorityError(
                    "admitted request issuer authority is unavailable"
                )
            return registry

        def _resolved(
            self,
            authority: AdmittedFinancialRequestAuthority,
        ) -> tuple[str, FinancialRequestBindingMaterial]:
            if type(authority) is not AdmittedFinancialRequestAuthority:
                raise AdmittedFinancialRequestAuthorityError(
                    "admitted financial request authority has invalid exact type"
                )
            with lock:
                state = authority_states.get(authority)
            if state is None:
                raise AdmittedFinancialRequestAuthorityError(
                    "admitted financial request authority was not issued"
                )
            issuer_ref, admission_id, binding_id = state
            if issuer_ref() is not self:
                raise AdmittedFinancialRequestAuthorityError(
                    "admitted financial request authority belongs to another issuer"
                )
            try:
                material = self._registry().resolve(admission_id)
            except DurableFinancialRequestBindingError as error:
                raise AdmittedFinancialRequestAuthorityError(
                    "durable admitted request authority cannot be revalidated"
                ) from error
            if material.binding_id != binding_id:
                raise AdmittedFinancialRequestAuthorityError(
                    "durable admitted request binding changed"
                )
            return admission_id, material

        def issue(self, admission_id: str) -> AdmittedFinancialRequestAuthority:
            aid = _exact_text(admission_id, name="admission_id")
            try:
                material = self._registry().resolve(aid)
            except DurableFinancialRequestBindingError as error:
                raise AdmittedFinancialRequestAuthorityError(
                    "durable admitted request authority is unavailable"
                ) from error
            authority = object.__new__(AdmittedFinancialRequestAuthority)
            with lock:
                authority_states[authority] = (ref(self), aid, material.binding_id)
            return authority

        def identity(
            self,
            authority: AdmittedFinancialRequestAuthority,
        ) -> AdmittedFinancialRequestIdentity:
            admission_id, material = self._resolved(authority)
            return _identity(admission_id, material)

        def require_exact_request(
            self,
            authority: AdmittedFinancialRequestAuthority,
            *,
            request: Mapping[str, Any],
            submission_scope: Mapping[str, Any],
        ) -> FinancialRequestBindingMaterial:
            """Revalidate one issued authority against exact request/scope bytes."""

            admission_id, material = self._resolved(authority)
            detached_request = _detached_object(request, name="request")
            if _json_digest(detached_request) != material.request_sha256:
                raise AdmittedFinancialRequestAuthorityError(
                    "request digest differs from durable admitted authority"
                )

            detached_scope = _detached_object(
                submission_scope,
                name="submission_scope",
            )
            expected_scope = financial_submission_scope(
                admission_id=admission_id,
                material=material,
            )
            if detached_scope != expected_scope:
                raise AdmittedFinancialRequestAuthorityError(
                    "submission scope differs from durable admitted authority"
                )
            if _json_digest(detached_scope) != material.submission_scope_digest:
                raise AdmittedFinancialRequestAuthorityError(
                    "submission scope digest differs from durable admitted authority"
                )
            return material

    AdmittedFinancialRequestAuthority.__name__ = "AdmittedFinancialRequestAuthority"
    AdmittedFinancialRequestAuthority.__qualname__ = "AdmittedFinancialRequestAuthority"
    AdmittedFinancialRequestAuthorityIssuer.__name__ = (
        "AdmittedFinancialRequestAuthorityIssuer"
    )
    AdmittedFinancialRequestAuthorityIssuer.__qualname__ = (
        "AdmittedFinancialRequestAuthorityIssuer"
    )
    return AdmittedFinancialRequestAuthority, AdmittedFinancialRequestAuthorityIssuer


(
    AdmittedFinancialRequestAuthority,
    AdmittedFinancialRequestAuthorityIssuer,
) = _install_authority_types()
del _install_authority_types
