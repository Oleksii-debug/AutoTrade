"""Sealed financial authority for one exact provider request.

This module closes the product-level gap between durable financial admission and
an irreversible provider send without creating another dispatcher.  The
existing AuthorityService remains the dynamic financial policy/risk authority;
the existing HostBoundFinancialDispatcher remains the durable send-state owner.
This module only mints an issuer-bound capability for one immutable
FinancialRequestBindingMaterial and turns it back into the legacy two-argument
dispatch guard *inside* trusted composition.

Provider qualification remains an upstream authority.  A qualification digest
is retained in FinancialRequestBindingMaterial but this module does not upgrade
or manufacture qualification evidence.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

from .authority import AuthorityService
from .dispatch import DispatchOutcome
from .financial_request_binding import FinancialRequestBindingMaterial
from .persistence import JournalStore, payload_digest
from .production_bybit import ProductionBybitOrderSender
from .production_financial_host import FinancialProductionHostRuntime


class FinancialSendAuthorityError(PermissionError):
    """Raised when an exact financial send capability is absent or retargeted."""


_ISSUER_FACTORY_TOKEN = object()
_CAPABILITY_FACTORY_TOKEN = object()
_BOUND_BYBIT_FACTORY_TOKEN = object()


def _mapping_digest(value: Mapping[str, Any], *, name: str) -> str:
    if not isinstance(value, Mapping):
        raise TypeError(f"{name} must be a mapping")
    # payload_digest uses the same canonical-json contract as GuardedDispatcher.
    return payload_digest(dict(value))


def _exact_text(value: object, *, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise FinancialSendAuthorityError(f"{name} must be exact non-empty text")
    return value


def _risk_payload(
    journal: JournalStore,
    binding: FinancialRequestBindingMaterial,
) -> Mapping[str, Any]:
    events = journal.load_events("risk_decision", binding.risk_decision_id)
    if len(events) != 1:
        raise FinancialSendAuthorityError(
            "financial binding risk decision is not one durable canonical event"
        )
    payload = events[0].get("payload")
    if not isinstance(payload, Mapping):
        raise FinancialSendAuthorityError("durable risk decision payload is malformed")
    return payload


def _require_binding_matches_durable_admission(
    *,
    service: AuthorityService,
    journal: JournalStore,
    admission_id: str,
    intent_hash: str,
    action: str,
    binding: FinancialRequestBindingMaterial,
) -> Mapping[str, Any]:
    admission = service.historical_admission(admission_id)
    if not isinstance(admission, Mapping) or admission.get("outcome") != "ADMITTED":
        raise FinancialSendAuthorityError("financial send requires an admitted durable record")

    instrument = admission.get("instrument")
    if not isinstance(instrument, Mapping):
        raise FinancialSendAuthorityError("durable admission instrument is malformed")

    expected = (
        _exact_text(intent_hash, name="intent_hash"),
        binding.account_id,
        binding.runtime_environment,
        binding.instrument_id,
        binding.instrument_version,
        _exact_text(action, name="action").upper(),
        binding.risk_decision_id,
        binding.reservation_id,
        binding.capability_snapshot_id,
    )
    actual = (
        admission.get("intent_hash"),
        admission.get("account_id"),
        admission.get("environment"),
        instrument.get("instrument_id"),
        instrument.get("version"),
        admission.get("action"),
        admission.get("risk_decision_id"),
        admission.get("reservation_id"),
        admission.get("capability_snapshot_id"),
    )
    if actual != expected:
        raise FinancialSendAuthorityError(
            "financial request binding differs from durable admission scope"
        )

    risk_payload = _risk_payload(journal, binding)
    if risk_payload.get("journal_sequence_cut") != binding.admitted_journal_sequence_cut:
        raise FinancialSendAuthorityError(
            "financial request binding journal cut differs from durable risk decision"
        )
    snapshot = risk_payload.get("authoritative_risk_snapshot")
    if not isinstance(snapshot, Mapping):
        raise FinancialSendAuthorityError(
            "durable risk decision lacks authoritative risk snapshot"
        )
    if snapshot.get("snapshot_id") != binding.risk_snapshot_id:
        raise FinancialSendAuthorityError(
            "financial request binding risk snapshot differs from durable decision"
        )

    durable_intent = risk_payload.get("risk_intent")
    if not isinstance(durable_intent, Mapping):
        raise FinancialSendAuthorityError("durable risk decision lacks canonical risk intent")
    durable_side = durable_intent.get("side")
    durable_quantity = durable_intent.get("quantity")
    durable_price = durable_intent.get("price")
    if durable_side != binding.side:
        raise FinancialSendAuthorityError("financial binding side differs from admitted risk")
    if durable_quantity != binding.quantity:
        raise FinancialSendAuthorityError("financial binding quantity differs from admitted risk")
    if durable_price != binding.price:
        raise FinancialSendAuthorityError("financial binding price differs from admitted risk")

    return admission


def require_exact_bybit_financial_request(
    binding: FinancialRequestBindingMaterial,
    request: Mapping[str, Any],
    submission_scope: Mapping[str, Any] | None,
    *,
    provider_environment: str,
) -> None:
    """Prove one Bybit dispatcher request is the exact financial binding.

    This is intentionally pure and public so provider-composition tests can
    exercise the terminal equality rule without minting live authority.
    """

    if type(binding) is not FinancialRequestBindingMaterial:
        raise TypeError("binding must be exact FinancialRequestBindingMaterial")
    if binding.provider_id != "BYBIT":
        raise FinancialSendAuthorityError("financial binding belongs to another provider")
    provider_env = _exact_text(
        provider_environment,
        name="provider_environment",
    ).upper()
    if provider_env != binding.provider_environment:
        raise FinancialSendAuthorityError(
            "Bybit provider environment differs from financial binding"
        )
    if not isinstance(request, Mapping):
        raise TypeError("request must be a mapping")
    if _mapping_digest(request, name="request") != binding.request_sha256:
        raise FinancialSendAuthorityError(
            "dispatcher request digest differs from financial binding"
        )

    exact_projection = (
        request.get("endpoint"),
        request.get("account_id"),
        request.get("environment"),
        request.get("provider_environment"),
        request.get("capability_snapshot_id"),
        request.get("body_sha256"),
    )
    expected_projection = (
        binding.endpoint,
        binding.account_id,
        binding.runtime_environment,
        binding.provider_environment,
        binding.capability_snapshot_id,
        binding.body_sha256,
    )
    if exact_projection != expected_projection:
        raise FinancialSendAuthorityError(
            "dispatcher provider projection differs from financial binding"
        )

    body = request.get("body")
    if not isinstance(body, Mapping):
        raise FinancialSendAuthorityError("Bybit dispatcher request body is missing")
    if _mapping_digest(body, name="request.body") != binding.body_sha256:
        raise FinancialSendAuthorityError("Bybit body digest differs from financial binding")

    expected_side = "Buy" if binding.side == "BUY" else "Sell"
    expected_order_type = "Market" if binding.order_type == "MARKET" else "Limit"
    if body.get("orderLinkId") != binding.client_order_id:
        raise FinancialSendAuthorityError("Bybit client order id differs from financial binding")
    if body.get("side") != expected_side:
        raise FinancialSendAuthorityError("Bybit side differs from financial binding")
    if body.get("qty") != binding.quantity:
        raise FinancialSendAuthorityError("Bybit quantity differs from financial binding")
    if body.get("orderType") != expected_order_type:
        raise FinancialSendAuthorityError("Bybit order type differs from financial binding")
    if body.get("timeInForce") != binding.time_in_force:
        raise FinancialSendAuthorityError("Bybit TIF differs from financial binding")
    if binding.order_type == "LIMIT":
        if body.get("price") != binding.price:
            raise FinancialSendAuthorityError("Bybit limit price differs from financial binding")
    elif "price" in body:
        raise FinancialSendAuthorityError("Bybit market request carries an unbound price")
    body_reduce_only = body.get("reduceOnly", False)
    if type(body_reduce_only) is not bool or body_reduce_only != binding.reduce_only:
        raise FinancialSendAuthorityError("Bybit reduce-only differs from financial binding")

    scope = {} if submission_scope is None else submission_scope
    if _mapping_digest(scope, name="submission_scope") != binding.submission_scope_digest:
        raise FinancialSendAuthorityError(
            "dispatcher submission scope differs from financial binding"
        )


class FinancialSendAuthority:
    """One issuer-bound, admission-bound, exact-request send capability."""

    __slots__ = (
        "__issuer_identity",
        "__binding",
        "__admission_id",
        "__intent_id",
        "__intent_hash",
        "__action",
    )

    def __init_subclass__(cls, **_kwargs) -> None:
        raise TypeError("FinancialSendAuthority is sealed")

    def __init__(
        self,
        *,
        issuer_identity: object,
        binding: FinancialRequestBindingMaterial,
        admission_id: str,
        intent_id: str,
        intent_hash: str,
        action: str,
        _factory_token: object = None,
    ) -> None:
        if _factory_token is not _CAPABILITY_FACTORY_TOKEN:
            raise FinancialSendAuthorityError(
                "FinancialSendAuthority must be minted by canonical issuer"
            )
        if type(binding) is not FinancialRequestBindingMaterial:
            raise TypeError("binding must be exact FinancialRequestBindingMaterial")
        self.__issuer_identity = issuer_identity
        self.__binding = binding
        self.__admission_id = _exact_text(admission_id, name="admission_id")
        self.__intent_id = _exact_text(intent_id, name="intent_id")
        self.__intent_hash = _exact_text(intent_hash, name="intent_hash")
        self.__action = _exact_text(action, name="action").upper()

    @property
    def binding(self) -> FinancialRequestBindingMaterial:
        return self.__binding

    @property
    def binding_id(self) -> str:
        return self.__binding.binding_id

    @property
    def admission_id(self) -> str:
        return self.__admission_id

    @property
    def intent_id(self) -> str:
        return self.__intent_id

    @property
    def intent_hash(self) -> str:
        return self.__intent_hash

    @property
    def action(self) -> str:
        return self.__action

    def _require_issuer(self, issuer_identity: object) -> None:
        if issuer_identity is not self.__issuer_identity:
            raise FinancialSendAuthorityError(
                "financial send capability belongs to another authority issuer"
            )

class FinancialSendAuthorityIssuer:
    """Product-owned bridge from AuthorityService to exact send capabilities."""

    __slots__ = (
        "__service",
        "__runtime",
        "__journal",
        "__store_identity",
        "__dispatcher",
        "__issuer_identity",
        "__dispatch_guard_function",
        "__dispatch_guard_code",
        "__historical_function",
        "__historical_code",
    )

    def __init_subclass__(cls, **_kwargs) -> None:
        raise TypeError("FinancialSendAuthorityIssuer is sealed")

    def __init__(
        self,
        service: AuthorityService,
        runtime: FinancialProductionHostRuntime,
        *,
        _factory_token: object = None,
    ) -> None:
        if _factory_token is not _ISSUER_FACTORY_TOKEN:
            raise FinancialSendAuthorityError(
                "FinancialSendAuthorityIssuer must come from canonical composition"
            )
        if type(service) is not AuthorityService:
            raise TypeError("service must be exact AuthorityService")
        if type(runtime) is not FinancialProductionHostRuntime:
            raise TypeError("runtime must be exact FinancialProductionHostRuntime")
        journal = runtime.journal
        if type(journal) is not JournalStore or service.store is not journal:
            raise FinancialSendAuthorityError(
                "AuthorityService and production host must share one exact JournalStore"
            )
        if runtime.store_identity != journal.store_identity:
            raise FinancialSendAuthorityError("production journal generation is inconsistent")
        dispatcher = runtime.financial_dispatcher
        dispatch_guard_function = AuthorityService.dispatch_guard
        historical_function = AuthorityService.historical_admission
        self.__service = service
        self.__runtime = runtime
        self.__journal = journal
        self.__store_identity = runtime.store_identity
        self.__dispatcher = dispatcher
        self.__issuer_identity = object()
        self.__dispatch_guard_function = dispatch_guard_function
        self.__dispatch_guard_code = dispatch_guard_function.__code__
        self.__historical_function = historical_function
        self.__historical_code = historical_function.__code__

    def _require_current(self) -> None:
        if type(self.__service) is not AuthorityService:
            raise FinancialSendAuthorityError("financial authority service type changed")
        if self.__service.store is not self.__journal:
            raise FinancialSendAuthorityError("financial authority journal changed")
        if self.__runtime.journal is not self.__journal:
            raise FinancialSendAuthorityError("production host journal changed")
        if (
            self.__runtime.store_identity != self.__store_identity
            or self.__journal.store_identity != self.__store_identity
        ):
            raise FinancialSendAuthorityError("production journal generation changed")
        if self.__runtime.financial_dispatcher is not self.__dispatcher:
            raise FinancialSendAuthorityError("production financial dispatcher changed")
        if AuthorityService.dispatch_guard is not self.__dispatch_guard_function:
            raise FinancialSendAuthorityError("AuthorityService dispatch guard changed")
        if self.__dispatch_guard_function.__code__ is not self.__dispatch_guard_code:
            raise FinancialSendAuthorityError("AuthorityService dispatch guard code changed")
        if AuthorityService.historical_admission is not self.__historical_function:
            raise FinancialSendAuthorityError("AuthorityService historical authority changed")
        if self.__historical_function.__code__ is not self.__historical_code:
            raise FinancialSendAuthorityError("AuthorityService historical code changed")

    @property
    def runtime(self) -> FinancialProductionHostRuntime:
        self._require_current()
        return self.__runtime

    def issue(
        self,
        *,
        admission_id: str,
        intent_id: str,
        intent_hash: str,
        action: str,
        binding: FinancialRequestBindingMaterial,
    ) -> FinancialSendAuthority:
        self._require_current()
        if type(binding) is not FinancialRequestBindingMaterial:
            raise TypeError("binding must be exact FinancialRequestBindingMaterial")
        admission = _require_binding_matches_durable_admission(
            service=self.__service,
            journal=self.__journal,
            admission_id=_exact_text(admission_id, name="admission_id"),
            intent_hash=_exact_text(intent_hash, name="intent_hash"),
            action=_exact_text(action, name="action"),
            binding=binding,
        )
        canonical_intent_id = _exact_text(intent_id, name="intent_id")
        if admission.get("intent_id") != canonical_intent_id:
            raise FinancialSendAuthorityError(
                "financial send intent id differs from durable admission"
            )
        return FinancialSendAuthority(
            issuer_identity=self.__issuer_identity,
            binding=binding,
            admission_id=admission_id,
            intent_id=canonical_intent_id,
            intent_hash=intent_hash,
            action=action,
            _factory_token=_CAPABILITY_FACTORY_TOKEN,
        )

    def _require_capability(self, authority: FinancialSendAuthority) -> None:
        self._require_current()
        if type(authority) is not FinancialSendAuthority:
            raise FinancialSendAuthorityError(
                "send requires exact FinancialSendAuthority capability"
            )
        authority._require_issuer(self.__issuer_identity)

    def _dispatch_guard_for(
        self,
        authority: FinancialSendAuthority,
    ) -> Callable[[str, str], tuple[bool, str]]:
        """Re-derive the terminal guard from canonical durable authority."""

        self._require_capability(authority)
        binding = authority.binding
        admission = _require_binding_matches_durable_admission(
            service=self.__service,
            journal=self.__journal,
            admission_id=authority.admission_id,
            intent_hash=authority.intent_hash,
            action=authority.action,
            binding=binding,
        )
        if admission.get("intent_id") != authority.intent_id:
            raise FinancialSendAuthorityError(
                "financial send intent id differs from durable admission"
            )
        return self.__dispatch_guard_function(
            self.__service,
            authority.admission_id,
            account_id=binding.account_id,
            environment=binding.runtime_environment,
            instrument_id=binding.instrument_id,
            instrument_version=binding.instrument_version,
            action=authority.action,
            capability_snapshot_id=binding.capability_snapshot_id,
        )


def build_financial_send_authority_issuer(
    service: AuthorityService,
    runtime: FinancialProductionHostRuntime,
) -> FinancialSendAuthorityIssuer:
    """Bind one AuthorityService generation to one current production host."""

    return FinancialSendAuthorityIssuer(
        service,
        runtime,
        _factory_token=_ISSUER_FACTORY_TOKEN,
    )


class FinanciallyBoundBybitOrderSender:
    """Canonical Bybit product sender requiring one sealed financial capability."""

    __slots__ = ("__sender", "__issuer", "__runtime", "__provider_environment")

    def __init_subclass__(cls, **_kwargs) -> None:
        raise TypeError("FinanciallyBoundBybitOrderSender is sealed")

    def __init__(
        self,
        sender: ProductionBybitOrderSender,
        issuer: FinancialSendAuthorityIssuer,
        *,
        _factory_token: object = None,
    ) -> None:
        if _factory_token is not _BOUND_BYBIT_FACTORY_TOKEN:
            raise FinancialSendAuthorityError(
                "FinanciallyBoundBybitOrderSender must come from canonical composition"
            )
        if type(sender) is not ProductionBybitOrderSender:
            raise TypeError("sender must be exact ProductionBybitOrderSender")
        if type(issuer) is not FinancialSendAuthorityIssuer:
            raise TypeError("issuer must be exact FinancialSendAuthorityIssuer")
        runtime = issuer.runtime
        sender_runtime = getattr(sender, "_ProductionBybitOrderSender__runtime", None)
        if sender_runtime is not runtime:
            raise FinancialSendAuthorityError(
                "Bybit sender and financial issuer belong to different production hosts"
            )
        self.__sender = sender
        self.__issuer = issuer
        self.__runtime = runtime
        self.__provider_environment = sender.provider_environment

    @property
    def provider_environment(self) -> str:
        return self.__provider_environment

    def dispatch(
        self,
        *,
        authority: FinancialSendAuthority,
        attempt_id: str,
        intent_id: str,
        intent_hash: str,
        request: Mapping[str, Any],
        now: str,
        client_id_max_length: int = 36,
        client_id_format: str = "TOKEN",
        final_barrier_clock: Callable[[], str] | None = None,
        submission_scope: Mapping[str, Any] | None = None,
    ) -> DispatchOutcome:
        issuer = self.__issuer
        authority_check = issuer._dispatch_guard_for(authority)
        if issuer.runtime is not self.__runtime:
            raise FinancialSendAuthorityError("financial issuer production host changed")
        if authority.intent_id != intent_id or authority.intent_hash != intent_hash:
            raise FinancialSendAuthorityError(
                "dispatch intent differs from sealed financial authority"
            )
        require_exact_bybit_financial_request(
            authority.binding,
            request,
            submission_scope,
            provider_environment=self.__provider_environment,
        )
        return self.__sender.dispatch(
            attempt_id=attempt_id,
            intent_id=intent_id,
            intent_hash=intent_hash,
            request=request,
            now=now,
            authority_check=authority_check,
            client_id_max_length=client_id_max_length,
            client_id_format=client_id_format,
            final_barrier_clock=final_barrier_clock,
            submission_scope=submission_scope,
        )


def bind_financial_bybit_order_sender(
    sender: ProductionBybitOrderSender,
    issuer: FinancialSendAuthorityIssuer,
) -> FinanciallyBoundBybitOrderSender:
    """Make the product-facing Bybit sender reject caller-supplied callbacks."""

    return FinanciallyBoundBybitOrderSender(
        sender,
        issuer,
        _factory_token=_BOUND_BYBIT_FACTORY_TOKEN,
    )
