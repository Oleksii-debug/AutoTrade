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


def _exact_utf8_text(value: object, *, name: str) -> str:
    if type(value) is not str:
        raise TypeError(f"{name} must be exact text")
    try:
        str.encode(value, "utf-8", "strict")
    except UnicodeEncodeError as error:
        raise AdmittedFinancialRequestAuthorityError(
            f"{name} must be valid canonical UTF-8 text"
        ) from error
    return value


def _exact_text(value: object, *, name: str) -> str:
    text = _exact_utf8_text(value, name=name)
    if not text or text != text.strip():
        raise AdmittedFinancialRequestAuthorityError(
            f"{name} must be exact canonical non-empty text"
        )
    return text


def _exact_json_value(value: object, *, name: str) -> object:
    """Detach one exact, acyclic JSON-domain value without caller execution."""

    def detach(current: object, current_name: str, ancestors: set[int]) -> object:
        if current is None or type(current) in {int, bool}:
            return current
        if type(current) is str:
            return _exact_utf8_text(current, name=current_name)
        if type(current) is list:
            marker = id(current)
            if marker in ancestors:
                raise AdmittedFinancialRequestAuthorityError(
                    f"{current_name} must be an acyclic JSON value"
                )
            ancestors.add(marker)
            try:
                return [
                    detach(item, f"{current_name}[{index}]", ancestors)
                    for index, item in enumerate(list.copy(current))
                ]
            finally:
                ancestors.remove(marker)
        if type(current) is dict:
            if any(type(key) is not str for key in current):
                raise TypeError(f"{current_name} keys must be exact strings")
            marker = id(current)
            if marker in ancestors:
                raise AdmittedFinancialRequestAuthorityError(
                    f"{current_name} must be an acyclic JSON value"
                )
            detached = dict.copy(current)
            for key in detached:
                _exact_utf8_text(key, name=f"{current_name} key")
            ancestors.add(marker)
            try:
                return {
                    key: detach(item, f"{current_name}.{key}", ancestors)
                    for key, item in detached.items()
                }
            finally:
                ancestors.remove(marker)
        raise TypeError(f"{current_name} must contain exact JSON-domain values")

    return detach(value, name, set())

def _detached_object(value: object, *, name: str) -> dict[str, Any]:
    """Snapshot an exact built-in JSON object before financial comparison."""

    if type(value) is not dict:
        raise TypeError(f"{name} must be an exact dict")
    try:
        detached = _exact_json_value(value, name=name)
    except RecursionError as error:
        raise AdmittedFinancialRequestAuthorityError(
            f"{name} JSON material is too deeply nested"
        ) from error
    if type(detached) is not dict:
        raise TypeError(f"{name} must be an exact dict")
    return detached


def _json_digest(value: dict[str, Any]) -> str:
    if type(value) is not dict:
        raise TypeError("digest value must be an exact dict")
    try:
        encoded = canonical_json(value).encode("utf-8")
    except (TypeError, ValueError, OverflowError, UnicodeError, RecursionError) as error:
        raise AdmittedFinancialRequestAuthorityError(
            "financial authority JSON material cannot be canonically digested"
        ) from error
    return "sha256:" + sha256(encoded).hexdigest()


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
            if type(self) is not AdmittedFinancialRequestAuthorityIssuer:
                raise TypeError(
                    "issuer must be exact AdmittedFinancialRequestAuthorityIssuer"
                )
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
            if type(self) is not AdmittedFinancialRequestAuthorityIssuer:
                raise TypeError(
                    "issuer must be exact AdmittedFinancialRequestAuthorityIssuer"
                )
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
            if type(self) is not AdmittedFinancialRequestAuthorityIssuer:
                raise TypeError(
                    "issuer must be exact AdmittedFinancialRequestAuthorityIssuer"
                )
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
            if type(self) is not AdmittedFinancialRequestAuthorityIssuer:
                raise TypeError(
                    "issuer must be exact AdmittedFinancialRequestAuthorityIssuer"
                )
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
