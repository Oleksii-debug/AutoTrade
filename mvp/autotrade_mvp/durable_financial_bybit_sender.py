"""Product-facing Bybit dispatch bound to restart-safe financial request authority.

This module does not own a transport, dispatcher, provider route, risk engine, or
request-binding registry.  It composes the existing financially-bound Bybit
sender with the durable mint adapter so the product dispatch surface cannot
accept a caller-minted ``FinancialSendAuthority`` or a caller-selected
``FinancialRequestBindingMaterial``.
"""

from __future__ import annotations

from typing import Any, Mapping

from .dispatch import DispatchOutcome
from .durable_financial_request_binding import DurableFinancialRequestBindingRegistry
from .durable_financial_send_issuance import issue_persisted_financial_send_authority
from .financial_send_authority import (
    FinancialSendAuthorityIssuer,
    FinanciallyBoundBybitOrderSender,
)


class DurableFinancialBybitSenderError(PermissionError):
    """The product Bybit sender lost its persisted financial authority binding."""


_FACTORY_TOKEN = object()
_ISSUER_TYPE = FinancialSendAuthorityIssuer
_REGISTRY_TYPE = DurableFinancialRequestBindingRegistry
_SENDER_TYPE = FinanciallyBoundBybitOrderSender
_MINT_PERSISTED_AUTHORITY = issue_persisted_financial_send_authority
_MINT_PERSISTED_AUTHORITY_CODE = _MINT_PERSISTED_AUTHORITY.__code__
_SENDER_DISPATCH_FUNCTION = _SENDER_TYPE.__dict__.get("dispatch")
_SENDER_DISPATCH_CODE = getattr(_SENDER_DISPATCH_FUNCTION, "__code__", None)
if not callable(_SENDER_DISPATCH_FUNCTION) or _SENDER_DISPATCH_CODE is None:
    raise RuntimeError("financially-bound Bybit dispatch authority is unavailable")


def _require_module_authority() -> None:
    """Check only retained identities; execute no registry/issuer callback here."""

    if FinancialSendAuthorityIssuer is not _ISSUER_TYPE:
        raise DurableFinancialBybitSenderError("financial issuer type authority changed")
    if DurableFinancialRequestBindingRegistry is not _REGISTRY_TYPE:
        raise DurableFinancialBybitSenderError(
            "durable binding registry type authority changed"
        )
    if FinanciallyBoundBybitOrderSender is not _SENDER_TYPE:
        raise DurableFinancialBybitSenderError("financial Bybit sender type authority changed")
    if (
        issue_persisted_financial_send_authority is not _MINT_PERSISTED_AUTHORITY
        or _MINT_PERSISTED_AUTHORITY.__code__ is not _MINT_PERSISTED_AUTHORITY_CODE
    ):
        raise DurableFinancialBybitSenderError(
            "persisted financial authority mint executable changed"
        )
    if (
        _SENDER_TYPE.__dict__.get("dispatch") is not _SENDER_DISPATCH_FUNCTION
        or _SENDER_DISPATCH_FUNCTION.__code__ is not _SENDER_DISPATCH_CODE
    ):
        raise DurableFinancialBybitSenderError(
            "financial Bybit dispatch executable authority changed"
        )


def _structural_authorities(
    sender: FinanciallyBoundBybitOrderSender,
    issuer: FinancialSendAuthorityIssuer,
    registry: DurableFinancialRequestBindingRegistry,
) -> tuple[object, object]:
    """Read canonical private object identities without executing mutable methods."""

    runtime = object.__getattribute__(
        issuer,
        "_FinancialSendAuthorityIssuer__runtime",
    )
    issuer_store = object.__getattribute__(
        issuer,
        "_FinancialSendAuthorityIssuer__journal",
    )
    registry_store = object.__getattribute__(registry, "_store")
    if registry_store is not issuer_store:
        raise DurableFinancialBybitSenderError(
            "durable financial binding and product sender must share one exact JournalStore"
        )
    if object.__getattribute__(
        sender,
        "_FinanciallyBoundBybitOrderSender__issuer",
    ) is not issuer:
        raise DurableFinancialBybitSenderError(
            "financial Bybit sender belongs to another issuer"
        )
    if object.__getattribute__(
        sender,
        "_FinanciallyBoundBybitOrderSender__runtime",
    ) is not runtime:
        raise DurableFinancialBybitSenderError(
            "financial Bybit sender belongs to another production host"
        )
    return runtime, issuer_store


class DurableFinanciallyBoundBybitOrderSender:
    """Canonical product sender resolving durable binding before every send attempt."""

    __slots__ = (
        "__sender",
        "__issuer",
        "__registry",
        "__runtime",
        "__store",
        "__provider_environment",
        "__sender_dispatch",
    )

    def __init_subclass__(cls, **_kwargs) -> None:
        raise TypeError("DurableFinanciallyBoundBybitOrderSender is sealed")

    def __init__(
        self,
        sender: FinanciallyBoundBybitOrderSender,
        issuer: FinancialSendAuthorityIssuer,
        registry: DurableFinancialRequestBindingRegistry,
        *,
        _factory_token: object = None,
    ) -> None:
        if _factory_token is not _FACTORY_TOKEN:
            raise DurableFinancialBybitSenderError(
                "DurableFinanciallyBoundBybitOrderSender requires canonical composition"
            )
        if type(sender) is not _SENDER_TYPE:
            raise TypeError("sender must be exact FinanciallyBoundBybitOrderSender")
        if type(issuer) is not _ISSUER_TYPE:
            raise TypeError("issuer must be exact FinancialSendAuthorityIssuer")
        if type(registry) is not _REGISTRY_TYPE:
            raise TypeError(
                "registry must be exact DurableFinancialRequestBindingRegistry"
            )
        _require_module_authority()
        runtime, store = _structural_authorities(sender, issuer, registry)
        provider_environment = object.__getattribute__(
            sender,
            "_FinanciallyBoundBybitOrderSender__provider_environment",
        )
        if type(provider_environment) is not str or not provider_environment:
            raise DurableFinancialBybitSenderError(
                "financial Bybit provider environment is non-canonical"
            )
        sender_dispatch = _SENDER_DISPATCH_FUNCTION.__get__(sender, _SENDER_TYPE)
        self.__sender = sender
        self.__issuer = issuer
        self.__registry = registry
        self.__runtime = runtime
        self.__store = store
        self.__provider_environment = provider_environment
        self.__sender_dispatch = sender_dispatch

    @property
    def provider_environment(self) -> str:
        return self.__provider_environment

    def _require_current(self) -> None:
        """Perform structural checks only; #1713 owns executable/generation proof."""

        _require_module_authority()
        if type(self.__sender) is not _SENDER_TYPE:
            raise DurableFinancialBybitSenderError("financial Bybit sender type changed")
        if type(self.__issuer) is not _ISSUER_TYPE:
            raise DurableFinancialBybitSenderError("financial issuer type changed")
        if type(self.__registry) is not _REGISTRY_TYPE:
            raise DurableFinancialBybitSenderError("durable binding registry type changed")
        runtime, store = _structural_authorities(
            self.__sender,
            self.__issuer,
            self.__registry,
        )
        if runtime is not self.__runtime or store is not self.__store:
            raise DurableFinancialBybitSenderError(
                "durable financial product composition changed"
            )
        if object.__getattribute__(
            self.__sender,
            "_FinanciallyBoundBybitOrderSender__provider_environment",
        ) != self.__provider_environment:
            raise DurableFinancialBybitSenderError("financial Bybit provider scope changed")
        bound = self.__sender_dispatch
        if (
            getattr(bound, "__self__", None) is not self.__sender
            or getattr(bound, "__func__", None) is not _SENDER_DISPATCH_FUNCTION
        ):
            raise DurableFinancialBybitSenderError(
                "financial Bybit dispatch binding authority changed"
            )

    def dispatch(
        self,
        *,
        admission_id: str,
        action: str,
        attempt_id: str,
        intent_id: str,
        intent_hash: str,
        request: Mapping[str, Any],
        now: str,
        client_id_max_length: int = 36,
        client_id_format: str = "TOKEN",
        submission_scope: Mapping[str, Any] | None = None,
    ) -> DispatchOutcome:
        """Resolve the persisted admitted request, mint, then dispatch exact request."""

        self._require_current()
        # The #1713 adapter performs the executable-authority and durable store
        # generation checks before it executes any registry/issuer authority.
        authority = _MINT_PERSISTED_AUTHORITY(
            self.__issuer,
            self.__registry,
            admission_id=admission_id,
            intent_id=intent_id,
            intent_hash=intent_hash,
            action=action,
        )
        self._require_current()
        return self.__sender_dispatch(
            authority=authority,
            attempt_id=attempt_id,
            intent_id=intent_id,
            intent_hash=intent_hash,
            request=request,
            now=now,
            client_id_max_length=client_id_max_length,
            client_id_format=client_id_format,
            submission_scope=submission_scope,
        )


def bind_durable_financial_bybit_order_sender(
    sender: FinanciallyBoundBybitOrderSender,
    issuer: FinancialSendAuthorityIssuer,
    registry: DurableFinancialRequestBindingRegistry,
) -> DurableFinanciallyBoundBybitOrderSender:
    """Return the product sender whose public dispatch cannot accept detached authority."""

    return DurableFinanciallyBoundBybitOrderSender(
        sender,
        issuer,
        registry,
        _factory_token=_FACTORY_TOKEN,
    )
