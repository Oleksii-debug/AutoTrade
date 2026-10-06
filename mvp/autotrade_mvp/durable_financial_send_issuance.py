"""Resolve one restart-safe durable request binding before send-authority mint.

This module is a narrow composition seam. It does not create a dispatcher,
transport, retry state machine, provider authority, or new financial admission.
The durable admitted-request registry remains the restart-safe owner of exact
request content; ``FinancialSendAuthorityIssuer`` remains the product authority
that revalidates admission/risk/provider scope and mints the existing sealed
send capability.

The public mint path deliberately has no caller-supplied ``binding`` argument.
It resolves the one durable binding for the admission from the same exact
JournalStore used by the production issuer, then forwards that material into the
already-canonical issuer. PAPER/LIVE therefore remains fail-closed when the
registry cannot prove accepted account/Q/instrument authority.
"""

from __future__ import annotations

from typing import Any, Callable

from . import durable_financial_request_binding as _registry_module
from .durable_financial_request_binding import DurableFinancialRequestBindingRegistry
from .financial_request_binding import FinancialRequestBindingMaterial
from .financial_send_authority import FinancialSendAuthority, FinancialSendAuthorityIssuer


class DurableFinancialSendIssuanceError(PermissionError):
    """Raised when durable binding ownership cannot safely enter authority mint."""


def _exact_text(value: object, *, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise DurableFinancialSendIssuanceError(
            f"{name} must be exact canonical non-empty text"
        )
    return value


def _method_authority(owner: type, name: str) -> tuple[str, Callable[..., Any], object]:
    function = owner.__dict__.get(name)
    code = getattr(function, "__code__", None)
    if not callable(function) or code is None:
        raise RuntimeError(f"{owner.__name__}.{name} executable authority is unavailable")
    return name, function, code


def _function_authority(module: object, name: str) -> tuple[str, Callable[..., Any], object]:
    function = getattr(module, name, None)
    code = getattr(function, "__code__", None)
    if not callable(function) or code is None:
        raise RuntimeError(f"durable binding {name} executable authority is unavailable")
    return name, function, code


_REGISTRY_TYPE = DurableFinancialRequestBindingRegistry
_REGISTRY_METHOD_AUTHORITIES = tuple(
    _method_authority(_REGISTRY_TYPE, name)
    for name in (
        "_require_store",
        "_current_sequence",
        "_authority",
        "_load_payload",
        "resolve",
    )
)
_REGISTRY_REQUIRE_STORE = _REGISTRY_TYPE.__dict__["_require_store"]
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

_ISSUER_TYPE = FinancialSendAuthorityIssuer
_ISSUER_REQUIRE_CURRENT_NAME, _ISSUER_REQUIRE_CURRENT, _ISSUER_REQUIRE_CURRENT_CODE = (
    _method_authority(_ISSUER_TYPE, "_require_current")
)
_ISSUER_ISSUE_NAME, _ISSUER_ISSUE, _ISSUER_ISSUE_CODE = _method_authority(
    _ISSUER_TYPE, "issue"
)
_ISSUER_RUNTIME_DESCRIPTOR = _ISSUER_TYPE.__dict__.get("runtime")
if (
    type(_ISSUER_RUNTIME_DESCRIPTOR) is not property
    or _ISSUER_RUNTIME_DESCRIPTOR.fget is None
    or getattr(_ISSUER_RUNTIME_DESCRIPTOR.fget, "__code__", None) is None
):
    raise RuntimeError("FinancialSendAuthorityIssuer.runtime authority is unavailable")
_ISSUER_RUNTIME_GETTER = _ISSUER_RUNTIME_DESCRIPTOR.fget
_ISSUER_RUNTIME_GETTER_CODE = _ISSUER_RUNTIME_GETTER.__code__


def _require_executable_authority() -> None:
    if DurableFinancialRequestBindingRegistry is not _REGISTRY_TYPE:
        raise DurableFinancialSendIssuanceError(
            "durable binding registry type authority changed"
        )
    for name, function, code in _REGISTRY_METHOD_AUTHORITIES:
        current = _REGISTRY_TYPE.__dict__.get(name)
        if current is not function or getattr(function, "__code__", None) is not code:
            raise DurableFinancialSendIssuanceError(
                f"durable binding registry {name} executable authority changed"
            )
    for name, function, code in _REGISTRY_FUNCTION_AUTHORITIES:
        current = getattr(_registry_module, name, None)
        if current is not function or getattr(function, "__code__", None) is not code:
            raise DurableFinancialSendIssuanceError(
                f"durable binding {name} executable authority changed"
            )
    for name, authority in _REGISTRY_OBJECT_AUTHORITIES.items():
        if getattr(_registry_module, name, None) is not authority:
            raise DurableFinancialSendIssuanceError(
                f"durable binding {name} authority changed"
            )

    if FinancialSendAuthorityIssuer is not _ISSUER_TYPE:
        raise DurableFinancialSendIssuanceError(
            "financial send issuer type authority changed"
        )
    if (
        _ISSUER_TYPE.__dict__.get(_ISSUER_REQUIRE_CURRENT_NAME)
        is not _ISSUER_REQUIRE_CURRENT
        or _ISSUER_REQUIRE_CURRENT.__code__ is not _ISSUER_REQUIRE_CURRENT_CODE
    ):
        raise DurableFinancialSendIssuanceError(
            "financial send issuer currentness executable authority changed"
        )
    if (
        _ISSUER_TYPE.__dict__.get(_ISSUER_ISSUE_NAME) is not _ISSUER_ISSUE
        or _ISSUER_ISSUE.__code__ is not _ISSUER_ISSUE_CODE
    ):
        raise DurableFinancialSendIssuanceError(
            "financial send issuer mint executable authority changed"
        )
    current_runtime = _ISSUER_TYPE.__dict__.get("runtime")
    if (
        current_runtime is not _ISSUER_RUNTIME_DESCRIPTOR
        or type(current_runtime) is not property
        or current_runtime.fget is not _ISSUER_RUNTIME_GETTER
        or _ISSUER_RUNTIME_GETTER.__code__ is not _ISSUER_RUNTIME_GETTER_CODE
    ):
        raise DurableFinancialSendIssuanceError(
            "financial send issuer runtime executable authority changed"
        )


def _issue_with_authorities(
    issuer: FinancialSendAuthorityIssuer,
    registry: DurableFinancialRequestBindingRegistry,
    *,
    admission_id: str,
    intent_id: str,
    intent_hash: str,
    action: str,
    require_store: Callable[[DurableFinancialRequestBindingRegistry], Any],
    runtime_getter: Callable[[FinancialSendAuthorityIssuer], Any],
    resolve: Callable[[DurableFinancialRequestBindingRegistry, str], object],
    issue: Callable[..., FinancialSendAuthority],
) -> FinancialSendAuthority:
    """Internal composition helper with injectable already-pinned authorities."""

    if type(issuer) is not _ISSUER_TYPE:
        raise TypeError("issuer must be exact FinancialSendAuthorityIssuer")
    if type(registry) is not _REGISTRY_TYPE:
        raise TypeError("registry must be exact DurableFinancialRequestBindingRegistry")
    aid = _exact_text(admission_id, name="admission_id")
    iid = _exact_text(intent_id, name="intent_id")
    ihash = _exact_text(intent_hash, name="intent_hash")
    canonical_action = _exact_text(action, name="action")

    store = require_store(registry)
    runtime = runtime_getter(issuer)
    if getattr(runtime, "journal", None) is not store:
        raise DurableFinancialSendIssuanceError(
            "durable request binding and financial send issuer must share one exact JournalStore"
        )

    material = resolve(registry, aid)
    if type(material) is not FinancialRequestBindingMaterial:
        raise DurableFinancialSendIssuanceError(
            "durable request binding did not resolve exact FinancialRequestBindingMaterial"
        )
    return issue(
        issuer,
        admission_id=aid,
        intent_id=iid,
        intent_hash=ihash,
        action=canonical_action,
        binding=material,
    )


def issue_persisted_financial_send_authority(
    issuer: FinancialSendAuthorityIssuer,
    registry: DurableFinancialRequestBindingRegistry,
    *,
    admission_id: str,
    intent_id: str,
    intent_hash: str,
    action: str,
) -> FinancialSendAuthority:
    """Mint from the restart-safe binding owned by the shared durable journal."""

    if type(issuer) is not _ISSUER_TYPE:
        raise TypeError("issuer must be exact FinancialSendAuthorityIssuer")
    if type(registry) is not _REGISTRY_TYPE:
        raise TypeError("registry must be exact DurableFinancialRequestBindingRegistry")

    # Detach scalar authority inputs before any registry/issuer executable runs.
    aid = _exact_text(admission_id, name="admission_id")
    iid = _exact_text(intent_id, name="intent_id")
    ihash = _exact_text(intent_hash, name="intent_hash")
    canonical_action = _exact_text(action, name="action")
    _require_executable_authority()

    return _issue_with_authorities(
        issuer,
        registry,
        admission_id=aid,
        intent_id=iid,
        intent_hash=ihash,
        action=canonical_action,
        require_store=_REGISTRY_REQUIRE_STORE,
        runtime_getter=_ISSUER_RUNTIME_GETTER,
        resolve=_REGISTRY_RESOLVE,
        issue=_ISSUER_ISSUE,
    )
