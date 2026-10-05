"""Source-signed semantics for paginated provider activity absence scans.

Transaction-log reads are not directly selectable by client order id.  A
negative result therefore means something only when the exact current provider
qualification signs the stronger contract used here: every page of an explicit
bounded window is parsed, nextPageCursor is followed to exhaustion, and every
returned row's orderLinkId is inspected for the target identity.

This module proves only that Q authorizes those semantics for one exact read
rule/parser.  It does not inspect pages, prove a consistency horizon, or mint
PROVEN_ABSENT.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import weakref

from .durable_provider_qualification import DurableProviderQualificationRegistry
from .persistence import canonical_json
from .provider_core import Surface
from .provider_qualification_authority import ProviderQualificationError
from .provider_route_reads import (
    ProviderRouteReadError,
    QualifiedProviderReadQueryBinding,
    _journal_cut,
    _point,
    _q_scope,
    _require_qualified_provider_read_binding_authority,
    _route_semantics,
    qualified_read_route_semantic_claim,
)
from .provider_selection import SelectedProviderRoute


_ACTIVITY_WINDOW_RULE_PREFIX = "PAGINATED_ACTIVITY_NEGATIVE_RULE:"
_ACTIVITY_WINDOW_SEMANTICS_V1 = (
    "COMPLETE_WINDOW_ALL_CURSOR_PAGES_ORDER_LINK_ID_NO_MATCH_V1"
)
_BYBIT_TRANSACTION_LOG_ENDPOINT = "/v5/account/transaction-log"


class ProviderActivityWindowSemanticsError(ValueError):
    pass


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ProviderActivityWindowSemanticsError(
            f"{name} must be canonical non-empty text"
        )
    return value


def qualified_activity_window_route_semantic_claim(
    *,
    provider_id: str,
    endpoint: str,
    surface: Surface,
    permission_scope: str,
    parser_identity: str,
) -> tuple[str, str]:
    """Return the source-Q claim for one complete paginated activity window."""

    provider = _text(provider_id, "provider_id").upper()
    path = _text(endpoint, "endpoint")
    parser = _text(parser_identity, "parser_identity")
    permission = _text(permission_scope, "permission_scope")
    if type(surface) is not Surface:
        raise TypeError("surface must be exact Surface")
    if provider != "BYBIT" or path != _BYBIT_TRANSACTION_LOG_ENDPOINT:
        raise ProviderActivityWindowSemanticsError(
            "activity-window negative semantics are currently defined only for Bybit transaction log"
        )
    try:
        _read_key, read_rule_digest = qualified_read_route_semantic_claim(
            provider_id=provider,
            endpoint=path,
            surface=surface,
            permission_scope=permission,
        )
    except ProviderRouteReadError as error:
        raise ProviderActivityWindowSemanticsError(
            "activity-window semantics require a canonical qualified read endpoint"
        ) from error

    locator = {"provider_id": provider, "endpoint": path}
    claim_key = _ACTIVITY_WINDOW_RULE_PREFIX + sha256(
        canonical_json(locator).encode("utf-8")
    ).hexdigest()
    policy = {
        "provider_id": provider,
        "endpoint": path,
        "surface": surface.value,
        "permission_scope": permission,
        "read_rule_digest": read_rule_digest,
        "parser_identity": parser,
        "semantics": _ACTIVITY_WINDOW_SEMANTICS_V1,
        "result_list_field": "list",
        "request_cursor_field": "cursor",
        "response_cursor_field": "nextPageCursor",
        "target_identity_field": "orderLinkId",
        "event_time_field": "transactionTime",
    }
    return claim_key, "sha256:" + sha256(
        canonical_json(policy).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True, slots=True, weakref_slot=True, init=False)
class QualifiedActivityWindowSemantics:
    qualification_id: str
    provider_id: str
    account_id: str
    environment: str
    endpoint: str
    parser_identity: str
    route_semantics_digest: str
    activity_window_rule_key: str
    activity_window_rule_digest: str
    semantics_version: str
    authority_journal_sequence_cut: int

    def __init__(self, *_args, **_kwargs) -> None:
        raise ProviderActivityWindowSemanticsError(
            "activity-window semantics must come from exact current qualification authority"
        )

    @property
    def evidence_ref(self) -> str:
        _require_qualified_activity_window_semantics_authority(self)
        material = {
            "qualification_id": self.qualification_id,
            "provider_id": self.provider_id,
            "account_id": self.account_id,
            "environment": self.environment,
            "endpoint": self.endpoint,
            "parser_identity": self.parser_identity,
            "route_semantics_digest": self.route_semantics_digest,
            "activity_window_rule_key": self.activity_window_rule_key,
            "activity_window_rule_digest": self.activity_window_rule_digest,
            "semantics_version": self.semantics_version,
            "authority_journal_sequence_cut": self.authority_journal_sequence_cut,
        }
        return "qualified-activity-window-semantics:sha256:" + sha256(
            canonical_json(material).encode("utf-8")
        ).hexdigest()


def _install_activity_window_semantics_authority():
    states: dict[int, tuple[weakref.ReferenceType, tuple[object, ...]]] = {}
    fields = (
        "qualification_id",
        "provider_id",
        "account_id",
        "environment",
        "endpoint",
        "parser_identity",
        "route_semantics_digest",
        "activity_window_rule_key",
        "activity_window_rule_digest",
        "semantics_version",
        "authority_journal_sequence_cut",
    )

    def prune() -> None:
        for object_id, (value_ref, _snapshot) in tuple(states.items()):
            if value_ref() is None:
                states.pop(object_id, None)

    def issue(**material: object) -> QualifiedActivityWindowSemantics:
        prune()
        value = object.__new__(QualifiedActivityWindowSemantics)
        for field_name in fields:
            object.__setattr__(value, field_name, material[field_name])
        states[id(value)] = (
            weakref.ref(value),
            tuple(material[field_name] for field_name in fields),
        )
        return value

    def require(value: object) -> None:
        if type(value) is not QualifiedActivityWindowSemantics:
            raise ProviderActivityWindowSemanticsError(
                "activity-window semantics authority requires exact sealed value"
            )
        prune()
        state = states.get(id(value))
        if state is None or state[0]() is not value:
            raise ProviderActivityWindowSemanticsError(
                "activity-window semantics construction authority is unavailable"
            )
        current = tuple(getattr(value, field_name) for field_name in fields)
        if current != state[1]:
            raise ProviderActivityWindowSemanticsError(
                "activity-window semantics changed after qualification authorization"
            )

    return issue, require


(
    _issue_qualified_activity_window_semantics,
    _require_qualified_activity_window_semantics_authority,
) = _install_activity_window_semantics_authority()
del _install_activity_window_semantics_authority


def require_qualified_activity_window_semantics(
    *,
    route: SelectedProviderRoute,
    read_binding: QualifiedProviderReadQueryBinding,
    qualification_registry: DurableProviderQualificationRegistry,
    at: datetime,
) -> QualifiedActivityWindowSemantics:
    """Revalidate exact-current Q and require signed activity-window semantics."""

    if type(route) is not SelectedProviderRoute:
        raise TypeError("route must be exact SelectedProviderRoute")
    if type(read_binding) is not QualifiedProviderReadQueryBinding:
        raise TypeError("read_binding must be exact QualifiedProviderReadQueryBinding")
    if type(qualification_registry) is not DurableProviderQualificationRegistry:
        raise TypeError(
            "qualification_registry must be exact DurableProviderQualificationRegistry"
        )
    try:
        _require_qualified_provider_read_binding_authority(read_binding)
        point = _point(at)
        cut = _journal_cut(qualification_registry.store)
    except ProviderRouteReadError as error:
        raise ProviderActivityWindowSemanticsError(
            "qualified read authority is unavailable for activity-window semantics"
        ) from error

    if read_binding.qualification_id != route.qualification_id:
        raise ProviderActivityWindowSemanticsError(
            "qualified activity read and selected route bind different qualifications"
        )
    base = read_binding.query_binding
    candidate = route.candidate
    if (
        base.provider_id != "BYBIT"
        or base.provider_id != candidate.provider_id
        or base.account_id != candidate.account_id
        or base.environment != route.capability.environment
        or base.endpoint != _BYBIT_TRANSACTION_LOG_ENDPOINT
        or base.surface is not Surface.AUTHENTICATED_READ
        or base.permission_scope != "ACCOUNT.READ"
        or read_binding.provider_environment != candidate.provider_environment
        or read_binding.adapter_code_sha != candidate.adapter_code_sha
        or read_binding.packaged_artifact_digest != candidate.packaged_artifact_digest
    ):
        raise ProviderActivityWindowSemanticsError(
            "qualified activity read does not match canonical Bybit route scope"
        )

    try:
        current_q = qualification_registry.require_exact_current(
            scope=_q_scope(route),
            at=point,
            expected_qualification_id=route.qualification_id,
            journal_sequence_cut=cut,
        )
    except ProviderQualificationError as error:
        raise ProviderActivityWindowSemanticsError(
            "selected route qualification is not exact current for activity-window semantics"
        ) from error
    if current_q.journal_sequence_cut != cut:
        raise ProviderActivityWindowSemanticsError(
            "qualification authority did not honor activity-window journal cut"
        )
    try:
        if _journal_cut(qualification_registry.store) != cut:
            raise ProviderActivityWindowSemanticsError(
                "qualification authority changed during activity-window authorization"
            )
        semantics, semantics_digest = _route_semantics(current_q.qualification)
    except ProviderRouteReadError as error:
        raise ProviderActivityWindowSemanticsError(
            "current qualification route semantics are invalid"
        ) from error
    if semantics_digest != read_binding.route_semantics_digest:
        raise ProviderActivityWindowSemanticsError(
            "activity read route semantics are not exact current Q semantics"
        )
    if semantics.get("PARSER_IDENTITY") != read_binding.parser_identity:
        raise ProviderActivityWindowSemanticsError(
            "activity read parser identity is not exact current Q parser identity"
        )

    claim_key, claim_digest = qualified_activity_window_route_semantic_claim(
        provider_id=base.provider_id,
        endpoint=base.endpoint,
        surface=base.surface,
        permission_scope=base.permission_scope,
        parser_identity=read_binding.parser_identity,
    )
    if semantics.get(claim_key) != claim_digest:
        raise ProviderActivityWindowSemanticsError(
            "provider qualification does not authorize complete paginated activity-window semantics"
        )

    value = _issue_qualified_activity_window_semantics(
        qualification_id=read_binding.qualification_id,
        provider_id=base.provider_id,
        account_id=base.account_id,
        environment=base.environment,
        endpoint=base.endpoint,
        parser_identity=read_binding.parser_identity,
        route_semantics_digest=read_binding.route_semantics_digest,
        activity_window_rule_key=claim_key,
        activity_window_rule_digest=claim_digest,
        semantics_version=_ACTIVITY_WINDOW_SEMANTICS_V1,
        authority_journal_sequence_cut=cut,
    )
    _require_qualified_activity_window_semantics_authority(value)
    return value
