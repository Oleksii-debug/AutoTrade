"""Canonical query scopes eligible for provider negative-result proof.

A signed provider semantic claim is not enough to prove absence if the request
itself is broad, filtered incorrectly, paginated, or does not cover the send
instant.  This module classifies only exact canonical query bindings.  It does
not inspect responses and cannot mint reconciliation absence authority.

Bybit transaction-log is intentionally excluded from direct identity scope:
its request contract has no orderLinkId/orderId selector, so absence there must
come from a separate complete-window + pagination + consistency-horizon proof.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import weakref

from .persistence import canonical_json
from .provider_core import (
    AuthenticatedReadQueryBinding,
    Surface,
    _require_authenticated_read_query_binding_authority,
)


_BYBIT_EXACT_ID_ENDPOINTS = frozenset(
    {
        "/v5/order/realtime",
        "/v5/order/history",
        "/v5/execution/list",
    }
)
_BYBIT_CATEGORIES = frozenset({"spot", "linear", "inverse", "option"})
_ORDER_IDENTITY_FIELD = "orderLinkId"
_MAX_HISTORY_WINDOW_MS = 7 * 24 * 60 * 60 * 1000


class ProviderNegativeResultQueryScopeError(ValueError):
    pass


def _canonical_text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ProviderNegativeResultQueryScopeError(
            f"{name} must be canonical non-empty text"
        )
    return value


def _instant(value: datetime, name: str) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise ProviderNegativeResultQueryScopeError(
            f"{name} must be exact timezone-aware datetime"
        )
    return value.astimezone(timezone.utc)


def _canonical_millis(value: object, name: str) -> int:
    text = _canonical_text(value, name)
    if not text.isascii() or not text.isdigit():
        raise ProviderNegativeResultQueryScopeError(
            f"{name} must be canonical non-negative millisecond integer text"
        )
    parsed = int(text, 10)
    if str(parsed) != text:
        raise ProviderNegativeResultQueryScopeError(
            f"{name} must be canonical non-negative millisecond integer text"
        )
    return parsed


def _client_order_id(value: object) -> str:
    text = _canonical_text(value, "client_order_id")
    if len(text) > 36 or any(
        not (character.isascii() and (character.isalnum() or character in "_-"))
        for character in text
    ):
        raise ProviderNegativeResultQueryScopeError(
            "Bybit client_order_id must be 1-36 ASCII letters, numbers, dashes or underscores"
        )
    return text


@dataclass(frozen=True, slots=True, weakref_slot=True, init=False)
class CanonicalNegativeResultQueryScope:
    provider_id: str
    account_id: str
    environment: str
    endpoint: str
    surface: Surface
    permission_scope: str
    query_digest: str
    client_order_id: str
    category: str
    submission_at: str
    coverage_start_ms: int | None
    coverage_end_ms: int | None
    selector_semantics: str

    def __init__(self, *_args, **_kwargs) -> None:
        raise ProviderNegativeResultQueryScopeError(
            "negative-result query scope must come from canonical authenticated-read authority"
        )

    @property
    def evidence_ref(self) -> str:
        _require_negative_result_query_scope_authority(self)
        material = {
            "provider_id": self.provider_id,
            "account_id": self.account_id,
            "environment": self.environment,
            "endpoint": self.endpoint,
            "surface": self.surface.value,
            "permission_scope": self.permission_scope,
            "query_digest": self.query_digest,
            "client_order_id": self.client_order_id,
            "category": self.category,
            "submission_at": self.submission_at,
            "coverage_start_ms": self.coverage_start_ms,
            "coverage_end_ms": self.coverage_end_ms,
            "selector_semantics": self.selector_semantics,
        }
        return "canonical-negative-result-query:sha256:" + sha256(
            canonical_json(material).encode("utf-8")
        ).hexdigest()


def _install_query_scope_authority():
    states: dict[int, tuple[weakref.ReferenceType, tuple[object, ...]]] = {}
    fields = (
        "provider_id",
        "account_id",
        "environment",
        "endpoint",
        "surface",
        "permission_scope",
        "query_digest",
        "client_order_id",
        "category",
        "submission_at",
        "coverage_start_ms",
        "coverage_end_ms",
        "selector_semantics",
    )

    def prune() -> None:
        for object_id, (value_ref, _snapshot) in tuple(states.items()):
            if value_ref() is None:
                states.pop(object_id, None)

    def issue(**material: object) -> CanonicalNegativeResultQueryScope:
        prune()
        value = object.__new__(CanonicalNegativeResultQueryScope)
        for field_name in fields:
            object.__setattr__(value, field_name, material[field_name])
        states[id(value)] = (
            weakref.ref(value),
            tuple(material[field_name] for field_name in fields),
        )
        return value

    def require(value: object) -> None:
        if type(value) is not CanonicalNegativeResultQueryScope:
            raise ProviderNegativeResultQueryScopeError(
                "negative-result query authority requires exact sealed value"
            )
        prune()
        state = states.get(id(value))
        if state is None or state[0]() is not value:
            raise ProviderNegativeResultQueryScopeError(
                "negative-result query construction authority is unavailable"
            )
        current = tuple(getattr(value, field_name) for field_name in fields)
        if current != state[1] or type(value.surface) is not Surface:
            raise ProviderNegativeResultQueryScopeError(
                "negative-result query scope changed after authorization"
            )

    return issue, require


(
    _issue_negative_result_query_scope,
    _require_negative_result_query_scope_authority,
) = _install_query_scope_authority()
del _install_query_scope_authority


def require_canonical_negative_result_query_scope(
    *,
    query_binding: AuthenticatedReadQueryBinding,
    client_order_id: str,
    submission_at: datetime,
) -> CanonicalNegativeResultQueryScope:
    """Require an exact Bybit order identity query suitable for negative proof."""

    if type(query_binding) is not AuthenticatedReadQueryBinding:
        raise TypeError("query_binding must be exact AuthenticatedReadQueryBinding")
    try:
        _require_authenticated_read_query_binding_authority(query_binding)
    except Exception as error:
        raise ProviderNegativeResultQueryScopeError(
            "authenticated-read construction authority is unavailable"
        ) from error

    target_id = _client_order_id(client_order_id)
    submitted = _instant(submission_at, "submission_at")
    if query_binding.provider_id != "BYBIT":
        raise ProviderNegativeResultQueryScopeError(
            "direct negative-result query scope is currently qualified only for BYBIT"
        )
    if query_binding.endpoint == "/v5/account/transaction-log":
        raise ProviderNegativeResultQueryScopeError(
            "Bybit transaction-log is not directly identity-selectable; complete-window pagination authority is required"
        )
    if query_binding.endpoint not in _BYBIT_EXACT_ID_ENDPOINTS:
        raise ProviderNegativeResultQueryScopeError(
            "Bybit endpoint is not eligible for direct identity negative-result proof"
        )
    if query_binding.surface is not Surface.AUTHENTICATED_READ:
        raise ProviderNegativeResultQueryScopeError(
            "Bybit direct negative-result proof requires AUTHENTICATED_READ surface"
        )
    if query_binding.permission_scope != "ORDER.READ":
        raise ProviderNegativeResultQueryScopeError(
            "Bybit direct negative-result proof requires ORDER.READ permission"
        )

    query = dict(query_binding.query)
    category = query.get("category")
    if category not in _BYBIT_CATEGORIES:
        raise ProviderNegativeResultQueryScopeError(
            "Bybit negative-result query requires exact lowercase product category"
        )
    if query.get(_ORDER_IDENTITY_FIELD) != target_id:
        raise ProviderNegativeResultQueryScopeError(
            "Bybit negative-result query is not bound to target client order identity"
        )

    coverage_start_ms: int | None = None
    coverage_end_ms: int | None = None
    if query_binding.endpoint in {"/v5/order/realtime", "/v5/execution/list"}:
        if set(query) != {"category", _ORDER_IDENTITY_FIELD}:
            raise ProviderNegativeResultQueryScopeError(
                "Bybit direct identity query contains filters or pagination outside canonical negative-proof scope"
            )
        selector_semantics = "EXACT_CLIENT_ORDER_ID"
    else:
        required = {"category", _ORDER_IDENTITY_FIELD, "startTime", "endTime"}
        if set(query) != required:
            raise ProviderNegativeResultQueryScopeError(
                "Bybit order-history negative proof requires exact identity plus explicit bounded time window"
            )
        coverage_start_ms = _canonical_millis(query["startTime"], "startTime")
        coverage_end_ms = _canonical_millis(query["endTime"], "endTime")
        if coverage_end_ms < coverage_start_ms:
            raise ProviderNegativeResultQueryScopeError(
                "Bybit order-history endTime must not precede startTime"
            )
        if coverage_end_ms - coverage_start_ms > _MAX_HISTORY_WINDOW_MS:
            raise ProviderNegativeResultQueryScopeError(
                "Bybit order-history negative-proof window must not exceed seven days"
            )
        submitted_ms = int(submitted.timestamp() * 1000)
        if not coverage_start_ms <= submitted_ms <= coverage_end_ms:
            raise ProviderNegativeResultQueryScopeError(
                "Bybit order-history negative-proof window does not cover submission instant"
            )
        selector_semantics = "EXACT_CLIENT_ORDER_ID_WITH_SUBMISSION_WINDOW"

    value = _issue_negative_result_query_scope(
        provider_id=query_binding.provider_id,
        account_id=query_binding.account_id,
        environment=query_binding.environment,
        endpoint=query_binding.endpoint,
        surface=query_binding.surface,
        permission_scope=query_binding.permission_scope,
        query_digest=query_binding.query_digest,
        client_order_id=target_id,
        category=category,
        submission_at=submitted.isoformat().replace("+00:00", "Z"),
        coverage_start_ms=coverage_start_ms,
        coverage_end_ms=coverage_end_ms,
        selector_semantics=selector_semantics,
    )
    _require_negative_result_query_scope_authority(value)
    return value
