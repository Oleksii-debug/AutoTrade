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
from typing import Any, Callable, Mapping
from weakref import WeakKeyDictionary, ref

from . import durable_financial_request_binding as _registry_module
from . import financial_binding_dispatch as _dispatch_module
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


def _method_authority(
    owner: type,
    name: str,
) -> tuple[str, Callable[..., Any], object]:
    function = owner.__dict__.get(name)
    code = getattr(function, "__code__", None)
    if not callable(function) or code is None:
        raise RuntimeError(f"{owner.__name__}.{name} executable authority is unavailable")
    return name, function, code


def _function_authority(
    module: object,
    name: str,
) -> tuple[str, Callable[..., Any], object]:
    function = getattr(module, name, None)
    code = getattr(function, "__code__", None)
    if not callable(function) or code is None:
        raise RuntimeError(f"{name} executable authority is unavailable")
    return name, function, code


_REGISTRY_TYPE = DurableFinancialRequestBindingRegistry
_REGISTRY_METHOD_AUTHORITIES = tuple(
    _method_authority(_REGISTRY_TYPE, name)
    for name in (
        "__init__",
        "_require_store",
        "_current_sequence",
        "_authority",
        "_load_payload",
        "resolve",
    )
)
_REGISTRY_RESOLVE = _REGISTRY_TYPE.__dict__["resolve"]
_REGISTRY_FUNCTION_AUTHORITIES = tuple(
    _function_authority(_registry_module, name)
    for name in (
        "_text",
        "_store_identity_digest",
        "_material_from_payload",
        "_binding_payload",
        "_risk_intent_axis",
        "_reservation_scope_digest",
        "_reservation_cut_digest",
        "_reservation_authority",
        "_admission_journal_sequence",
        "_simulation_account_cut",
        "_simulation_qualification_identity",
        "_validate_material_against_admission",
        "journal_store_identity_digest",
    )
)
_REGISTRY_OBJECT_AUTHORITIES = {
    name: getattr(_registry_module, name)
    for name in (
        "JournalStore",
        "DurableReservationBook",
        "ProviderFinancialScope",
        "_CANONICAL_APPEND_EVENT",
        "_CANONICAL_LOAD_EVENTS",
        "_CANONICAL_CURRENT_SEQUENCE",
        "_CANONICAL_STORE_IDENTITY",
        "_CANONICAL_PAYLOAD_DIGEST",
        "_AUTHORITY_TYPE",
        "_ADMISSION_TYPE",
        "_MATERIAL_TYPE",
    )
}
_LOCAL_FUNCTION_AUTHORITIES = tuple(
    (name, function, function.__code__)
    for name, function in (
        ("_exact_utf8_text", _exact_utf8_text),
        ("_exact_text", _exact_text),
        ("_exact_json_value", _exact_json_value),
        ("_detached_object", _detached_object),
        ("_json_digest", _json_digest),
        ("_identity", _identity),
    )
)
_DISPATCH_FUNCTION_AUTHORITIES = tuple(
    _function_authority(_dispatch_module, name)
    for name in ("_text", "financial_submission_scope")
)
_DISPATCH_OBJECT_AUTHORITIES = {
    "_FINANCIAL_SCOPE_SCHEMA": getattr(_dispatch_module, "_FINANCIAL_SCOPE_SCHEMA"),
    "FinancialRequestBindingMaterial": getattr(
        _dispatch_module,
        "FinancialRequestBindingMaterial",
    ),
}
_CANONICAL_JSON_AUTHORITY = canonical_json
_CANONICAL_JSON_CODE = getattr(_CANONICAL_JSON_AUTHORITY, "__code__", None)
if _CANONICAL_JSON_CODE is None:
    raise RuntimeError("canonical_json executable authority is unavailable")
_SHA256_AUTHORITY = sha256


def _install_authority_types():
    """Keep minting/binding state outside module- and instance-writable data."""

    module_globals = globals()
    registry_module = _registry_module
    dispatch_module = _dispatch_module
    registry_type = _REGISTRY_TYPE
    registry_resolve = _REGISTRY_RESOLVE
    registry_method_authorities = _REGISTRY_METHOD_AUTHORITIES
    registry_function_authorities = _REGISTRY_FUNCTION_AUTHORITIES
    registry_object_authorities = dict(_REGISTRY_OBJECT_AUTHORITIES)
    local_function_authorities = _LOCAL_FUNCTION_AUTHORITIES
    dispatch_function_authorities = _DISPATCH_FUNCTION_AUTHORITIES
    dispatch_object_authorities = dict(_DISPATCH_OBJECT_AUTHORITIES)
    canonical_json_authority = _CANONICAL_JSON_AUTHORITY
    canonical_json_code = _CANONICAL_JSON_CODE
    sha256_authority = _SHA256_AUTHORITY
    exact_text = _exact_text
    detached_object = _detached_object
    json_digest = _json_digest
    identity_builder = _identity
    scope_builder = financial_submission_scope
    material_type = FinancialRequestBindingMaterial
    journal_store_type = JournalStore
    registry_error_type = DurableFinancialRequestBindingError

    issuer_states: WeakKeyDictionary[object, DurableFinancialRequestBindingRegistry] = (
        WeakKeyDictionary()
    )
    authority_states: WeakKeyDictionary[
        object,
        tuple[object, str, str],
    ] = WeakKeyDictionary()
    lock = RLock()

    def require_executable_authority() -> None:
        if module_globals.get("DurableFinancialRequestBindingRegistry") is not registry_type:
            raise AdmittedFinancialRequestAuthorityError(
                "durable binding registry type authority changed"
            )
        if getattr(registry_module, "DurableFinancialRequestBindingRegistry", None) is not registry_type:
            raise AdmittedFinancialRequestAuthorityError(
                "durable binding registry module authority changed"
            )
        for name, function, code in registry_method_authorities:
            current = registry_type.__dict__.get(name)
            if current is not function or getattr(function, "__code__", None) is not code:
                raise AdmittedFinancialRequestAuthorityError(
                    f"durable binding registry {name} executable authority changed"
                )
        for name, function, code in registry_function_authorities:
            current = getattr(registry_module, name, None)
            if current is not function or getattr(function, "__code__", None) is not code:
                raise AdmittedFinancialRequestAuthorityError(
                    f"durable binding {name} executable authority changed"
                )
        for name, authority in registry_object_authorities.items():
            if getattr(registry_module, name, None) is not authority:
                raise AdmittedFinancialRequestAuthorityError(
                    f"durable binding {name} authority changed"
                )

        for name, function, code in local_function_authorities:
            current = module_globals.get(name)
            if current is not function or getattr(function, "__code__", None) is not code:
                raise AdmittedFinancialRequestAuthorityError(
                    f"admitted request {name} executable authority changed"
                )
        if (
            module_globals.get("canonical_json") is not canonical_json_authority
            or getattr(canonical_json_authority, "__code__", None)
            is not canonical_json_code
            or module_globals.get("sha256") is not sha256_authority
        ):
            raise AdmittedFinancialRequestAuthorityError(
                "admitted request digest executable authority changed"
            )

        for name, function, code in dispatch_function_authorities:
            current = getattr(dispatch_module, name, None)
            if current is not function or getattr(function, "__code__", None) is not code:
                raise AdmittedFinancialRequestAuthorityError(
                    f"financial submission scope {name} executable authority changed"
                )
        for name, authority in dispatch_object_authorities.items():
            if getattr(dispatch_module, name, None) is not authority:
                raise AdmittedFinancialRequestAuthorityError(
                    f"financial submission scope {name} authority changed"
                )
        if module_globals.get("financial_submission_scope") is not scope_builder:
            raise AdmittedFinancialRequestAuthorityError(
                "financial submission scope authority changed"
            )
        if module_globals.get("FinancialRequestBindingMaterial") is not material_type:
            raise AdmittedFinancialRequestAuthorityError(
                "financial request material type authority changed"
            )
        if module_globals.get("JournalStore") is not journal_store_type:
            raise AdmittedFinancialRequestAuthorityError(
                "JournalStore type authority changed"
            )

    def registry_for(
        issuer: object,
    ) -> DurableFinancialRequestBindingRegistry:
        with lock:
            registry = issuer_states.get(issuer)
        if registry is None or type(registry) is not registry_type:
            raise AdmittedFinancialRequestAuthorityError(
                "admitted request issuer authority is unavailable"
            )
        return registry

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
            if type(store) is not journal_store_type:
                raise TypeError("store must be exact JournalStore")
            require_executable_authority()
            registry = registry_type(store)
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
            require_executable_authority()
            return registry_for(self)

        def _resolved(
            self,
            authority: AdmittedFinancialRequestAuthority,
        ) -> tuple[str, FinancialRequestBindingMaterial]:
            if type(self) is not AdmittedFinancialRequestAuthorityIssuer:
                raise TypeError(
                    "issuer must be exact AdmittedFinancialRequestAuthorityIssuer"
                )
            require_executable_authority()
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
                material = registry_resolve(registry_for(self), admission_id)
            except registry_error_type as error:
                raise AdmittedFinancialRequestAuthorityError(
                    "durable admitted request authority cannot be revalidated"
                ) from error
            if type(material) is not material_type:
                raise AdmittedFinancialRequestAuthorityError(
                    "durable admitted request did not resolve exact material"
                )
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
            require_executable_authority()
            aid = exact_text(admission_id, name="admission_id")
            try:
                material = registry_resolve(registry_for(self), aid)
            except registry_error_type as error:
                raise AdmittedFinancialRequestAuthorityError(
                    "durable admitted request authority is unavailable"
                ) from error
            if type(material) is not material_type:
                raise AdmittedFinancialRequestAuthorityError(
                    "durable admitted request did not resolve exact material"
                )
            authority = object.__new__(AdmittedFinancialRequestAuthority)
            with lock:
                authority_states[authority] = (ref(self), aid, material.binding_id)
            return authority

        def identity(
            self,
            authority: AdmittedFinancialRequestAuthority,
        ) -> AdmittedFinancialRequestIdentity:
            admission_id, material = self._resolved(authority)
            return identity_builder(admission_id, material)

        def require_exact_request(
            self,
            authority: AdmittedFinancialRequestAuthority,
            *,
            request: Mapping[str, Any],
            submission_scope: Mapping[str, Any],
        ) -> FinancialRequestBindingMaterial:
            """Revalidate one issued authority against exact request/scope bytes."""

            admission_id, material = self._resolved(authority)
            detached_request = detached_object(request, name="request")
            if json_digest(detached_request) != material.request_sha256:
                raise AdmittedFinancialRequestAuthorityError(
                    "request digest differs from durable admitted authority"
                )

            detached_scope = detached_object(
                submission_scope,
                name="submission_scope",
            )
            expected_scope = scope_builder(
                admission_id=admission_id,
                material=material,
            )
            if detached_scope != expected_scope:
                raise AdmittedFinancialRequestAuthorityError(
                    "submission scope differs from durable admitted authority"
                )
            if json_digest(detached_scope) != material.submission_scope_digest:
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
