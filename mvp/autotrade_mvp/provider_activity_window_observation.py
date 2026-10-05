"""Qualified complete-window Bybit activity scans for one client order id.

This module validates the full exact cursor chain for
/v5/account/transaction-log.  It requires one source-signed exact-current Q,
an explicit bounded window covering the submission instant, invariant query
scope across every page, exact cursor continuity, successful exact response
observations, and inspection of every row's orderLinkId.

The result still is not PROVEN_ABSENT: provider consistency-horizon authority
and the other required reconciliation surfaces remain separate prerequisites.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import weakref

from .durable_provider_qualification import DurableProviderQualificationRegistry
from .persistence import canonical_json
from .provider_activity_window_semantics import (
    _require_qualified_activity_window_semantics_authority,
    require_qualified_activity_window_semantics,
)
from .provider_core import Surface
from .provider_route_reads import (
    QualifiedProviderReadQueryBinding,
    QualifiedProviderResponseObservation,
    _require_qualified_provider_read_binding_authority,
    _require_qualified_provider_response_authority,
)
from .provider_selection import SelectedProviderRoute


_ENDPOINT = "/v5/account/transaction-log"
_CATEGORIES = frozenset({"spot", "linear", "inverse", "option"})
_MAX_WINDOW_MS = 7 * 24 * 60 * 60 * 1000
_BASE_QUERY_KEYS = frozenset(
    {"accountType", "category", "startTime", "endTime", "limit"}
)


class ProviderActivityWindowObservationError(ValueError):
    pass


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ProviderActivityWindowObservationError(
            f"{name} must be canonical non-empty text"
        )
    return value


def _client_order_id(value: object) -> str:
    text = _text(value, "client_order_id")
    if len(text) > 36 or any(
        not (character.isascii() and (character.isalnum() or character in "_-"))
        for character in text
    ):
        raise ProviderActivityWindowObservationError(
            "Bybit client_order_id must be 1-36 ASCII letters, numbers, dashes or underscores"
        )
    return text


def _millis(value: object, name: str) -> int:
    text = _text(value, name)
    if not text.isascii() or not text.isdigit():
        raise ProviderActivityWindowObservationError(
            f"{name} must be canonical non-negative millisecond integer text"
        )
    parsed = int(text, 10)
    if str(parsed) != text:
        raise ProviderActivityWindowObservationError(
            f"{name} must be canonical non-negative millisecond integer text"
        )
    return parsed


def _instant(value: datetime, name: str) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise ProviderActivityWindowObservationError(
            f"{name} must be exact timezone-aware datetime"
        )
    return value.astimezone(timezone.utc)


def _millis_utc_text(value: int) -> str:
    seconds, remainder = divmod(value, 1000)
    return (
        datetime.fromtimestamp(seconds, tz=timezone.utc)
        .replace(microsecond=remainder * 1000)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


@dataclass(frozen=True, slots=True, weakref_slot=True, init=False)
class QualifiedActivityWindowNoMatchObservation:
    provider_id: str
    account_id: str
    environment: str
    surface: str
    endpoint: str
    client_order_id: str
    category: str
    coverage_start: str
    coverage_end: str
    qualification_id: str
    parser_identity: str
    route_semantics_digest: str
    activity_window_semantics_ref: str
    page_count: int
    query_digests: tuple[str, ...]
    provider_response_refs: tuple[str, ...]
    final_observed_at: str
    authority_journal_sequence_cut: int

    def __init__(self, *_args, **_kwargs) -> None:
        raise ProviderActivityWindowObservationError(
            "qualified activity no-match observations must come from complete provider page-chain authority"
        )

    @property
    def evidence_ref(self) -> str:
        _require_qualified_activity_window_no_match_authority(self)
        material = {
            "provider_id": self.provider_id,
            "account_id": self.account_id,
            "environment": self.environment,
            "surface": self.surface,
            "endpoint": self.endpoint,
            "client_order_id": self.client_order_id,
            "category": self.category,
            "coverage_start": self.coverage_start,
            "coverage_end": self.coverage_end,
            "qualification_id": self.qualification_id,
            "parser_identity": self.parser_identity,
            "route_semantics_digest": self.route_semantics_digest,
            "activity_window_semantics_ref": self.activity_window_semantics_ref,
            "page_count": self.page_count,
            "query_digests": list(self.query_digests),
            "provider_response_refs": list(self.provider_response_refs),
            "final_observed_at": self.final_observed_at,
            "authority_journal_sequence_cut": self.authority_journal_sequence_cut,
        }
        return "qualified-activity-window-no-match:sha256:" + sha256(
            canonical_json(material).encode("utf-8")
        ).hexdigest()


def _install_activity_window_no_match_authority():
    states: dict[int, tuple[weakref.ReferenceType, tuple[object, ...]]] = {}
    fields = (
        "provider_id",
        "account_id",
        "environment",
        "surface",
        "endpoint",
        "client_order_id",
        "category",
        "coverage_start",
        "coverage_end",
        "qualification_id",
        "parser_identity",
        "route_semantics_digest",
        "activity_window_semantics_ref",
        "page_count",
        "query_digests",
        "provider_response_refs",
        "final_observed_at",
        "authority_journal_sequence_cut",
    )

    def prune() -> None:
        for object_id, (value_ref, _snapshot) in tuple(states.items()):
            if value_ref() is None:
                states.pop(object_id, None)

    def issue(**material: object) -> QualifiedActivityWindowNoMatchObservation:
        prune()
        value = object.__new__(QualifiedActivityWindowNoMatchObservation)
        for field_name in fields:
            object.__setattr__(value, field_name, material[field_name])
        states[id(value)] = (
            weakref.ref(value),
            tuple(material[field_name] for field_name in fields),
        )
        return value

    def require(value: object) -> None:
        if type(value) is not QualifiedActivityWindowNoMatchObservation:
            raise ProviderActivityWindowObservationError(
                "activity no-match authority requires exact sealed value"
            )
        prune()
        state = states.get(id(value))
        if state is None or state[0]() is not value:
            raise ProviderActivityWindowObservationError(
                "activity no-match construction authority is unavailable"
            )
        current = tuple(getattr(value, field_name) for field_name in fields)
        if current != state[1]:
            raise ProviderActivityWindowObservationError(
                "activity no-match observation changed after page-chain authorization"
            )

    return issue, require


(
    _issue_qualified_activity_window_no_match,
    _require_qualified_activity_window_no_match_authority,
) = _install_activity_window_no_match_authority()
del _install_activity_window_no_match_authority


def _require_page_envelope(
    response: QualifiedProviderResponseObservation,
    *,
    target_client_order_id: str,
    category: str,
    coverage_start_ms: int,
    coverage_end_ms: int,
) -> str:
    observation = response.observation
    if observation.http_status != 200:
        raise ProviderActivityWindowObservationError(
            "Bybit activity page requires exact HTTP 200"
        )
    payload = observation.payload
    if type(payload) is not dict:
        raise ProviderActivityWindowObservationError(
            "Bybit activity page response must be an object"
        )
    if type(payload.get("retCode")) is not int or payload.get("retCode") != 0:
        raise ProviderActivityWindowObservationError(
            "Bybit activity page requires exact successful retCode"
        )
    result = payload.get("result")
    if type(result) is not dict:
        raise ProviderActivityWindowObservationError(
            "Bybit activity page requires result object"
        )
    rows = result.get("list")
    if type(rows) is not list:
        raise ProviderActivityWindowObservationError(
            "Bybit activity page requires result.list array"
        )
    cursor = result.get("nextPageCursor")
    if type(cursor) is not str:
        raise ProviderActivityWindowObservationError(
            "Bybit activity page requires string nextPageCursor"
        )
    for row in rows:
        if type(row) is not dict:
            raise ProviderActivityWindowObservationError(
                "Bybit activity page contains non-object row"
            )
        order_link_id = row.get("orderLinkId")
        if type(order_link_id) is not str:
            raise ProviderActivityWindowObservationError(
                "Bybit activity row lacks string orderLinkId"
            )
        if order_link_id == target_client_order_id:
            raise ProviderActivityWindowObservationError(
                "Bybit activity window contains target client order identity"
            )
        row_category = row.get("category")
        if type(row_category) is not str or row_category != category:
            raise ProviderActivityWindowObservationError(
                "Bybit activity row category differs from qualified window"
            )
        transaction_time = _millis(row.get("transactionTime"), "transactionTime")
        if not coverage_start_ms <= transaction_time <= coverage_end_ms:
            raise ProviderActivityWindowObservationError(
                "Bybit activity row lies outside qualified window"
            )
    return cursor


def qualify_complete_bybit_activity_no_match(
    *,
    route: SelectedProviderRoute,
    pages: tuple[QualifiedProviderResponseObservation, ...],
    client_order_id: str,
    submission_at: datetime,
    qualification_registry: DurableProviderQualificationRegistry,
    at: datetime,
) -> QualifiedActivityWindowNoMatchObservation:
    """Validate every transaction-log page and issue one sealed no-match fact."""

    if type(route) is not SelectedProviderRoute:
        raise TypeError("route must be exact SelectedProviderRoute")
    if type(pages) is not tuple or not pages:
        raise ProviderActivityWindowObservationError(
            "activity window requires a non-empty exact tuple of qualified pages"
        )
    if any(type(page) is not QualifiedProviderResponseObservation for page in pages):
        raise TypeError("activity pages must be exact QualifiedProviderResponseObservation values")
    if type(qualification_registry) is not DurableProviderQualificationRegistry:
        raise TypeError(
            "qualification_registry must be exact DurableProviderQualificationRegistry"
        )
    target_id = _client_order_id(client_order_id)
    submitted = _instant(submission_at, "submission_at")
    _instant(at, "at")

    first = pages[0]
    try:
        _require_qualified_provider_response_authority(first)
        _require_qualified_provider_read_binding_authority(first.query_binding)
    except Exception as error:
        raise ProviderActivityWindowObservationError(
            "first activity page lacks canonical qualified provider authority"
        ) from error
    first_binding = first.query_binding
    semantics = require_qualified_activity_window_semantics(
        route=route,
        read_binding=first_binding,
        qualification_registry=qualification_registry,
        at=at,
    )
    _require_qualified_activity_window_semantics_authority(semantics)

    base = first_binding.query_binding
    first_query = dict(base.query)
    if set(first_query) != _BASE_QUERY_KEYS:
        raise ProviderActivityWindowObservationError(
            "first Bybit activity page requires exact unfiltered bounded-window query"
        )
    if first_query.get("accountType") != "UNIFIED":
        raise ProviderActivityWindowObservationError(
            "Bybit activity negative proof requires UNIFIED accountType"
        )
    category = first_query.get("category")
    if category not in _CATEGORIES:
        raise ProviderActivityWindowObservationError(
            "Bybit activity negative proof requires exact lowercase category"
        )
    if first_query.get("limit") != "50":
        raise ProviderActivityWindowObservationError(
            "Bybit activity negative proof requires maximum canonical page limit 50"
        )
    coverage_start_ms = _millis(first_query.get("startTime"), "startTime")
    coverage_end_ms = _millis(first_query.get("endTime"), "endTime")
    if coverage_end_ms < coverage_start_ms:
        raise ProviderActivityWindowObservationError(
            "Bybit activity endTime must not precede startTime"
        )
    if coverage_end_ms - coverage_start_ms > _MAX_WINDOW_MS:
        raise ProviderActivityWindowObservationError(
            "Bybit activity negative-proof window must not exceed seven days"
        )
    submitted_ms = int(submitted.timestamp() * 1000)
    if not coverage_start_ms <= submitted_ms <= coverage_end_ms:
        raise ProviderActivityWindowObservationError(
            "Bybit activity negative-proof window does not cover submission instant"
        )

    base_query = dict(first_query)
    query_digests: list[str] = []
    response_refs: list[str] = []
    seen_cursors: set[str] = set()
    expected_cursor: str | None = None
    final_observed_at = ""

    for index, page in enumerate(pages):
        try:
            _require_qualified_provider_response_authority(page)
            _require_qualified_provider_read_binding_authority(page.query_binding)
        except Exception as error:
            raise ProviderActivityWindowObservationError(
                "activity page lacks canonical qualified provider authority"
            ) from error
        binding = page.query_binding
        page_base = binding.query_binding
        if (
            binding.qualification_id != semantics.qualification_id
            or binding.route_semantics_digest != semantics.route_semantics_digest
            or binding.parser_identity != semantics.parser_identity
            or binding.endpoint_rule_digest != first_binding.endpoint_rule_digest
            or binding.qualified_route_rule_digest != first_binding.qualified_route_rule_digest
            or page_base.provider_id != semantics.provider_id
            or page_base.account_id != semantics.account_id
            or page_base.environment != semantics.environment
            or page_base.endpoint != _ENDPOINT
            or page_base.surface is not Surface.AUTHENTICATED_READ
            or page_base.permission_scope != "ACCOUNT.READ"
        ):
            raise ProviderActivityWindowObservationError(
                "activity page authority differs from exact signed window scope"
            )
        expected_query = dict(base_query)
        if index > 0:
            if expected_cursor is None or expected_cursor == "":
                raise ProviderActivityWindowObservationError(
                    "activity page exists after terminal cursor"
                )
            expected_query["cursor"] = expected_cursor
        if dict(page_base.query) != expected_query:
            raise ProviderActivityWindowObservationError(
                "activity page query drifted from exact cursor chain"
            )

        next_cursor = _require_page_envelope(
            page,
            target_client_order_id=target_id,
            category=category,
            coverage_start_ms=coverage_start_ms,
            coverage_end_ms=coverage_end_ms,
        )
        if next_cursor:
            if next_cursor in seen_cursors:
                raise ProviderActivityWindowObservationError(
                    "Bybit activity pagination cursor cycle detected"
                )
            seen_cursors.add(next_cursor)
        if index < len(pages) - 1 and not next_cursor:
            raise ProviderActivityWindowObservationError(
                "Bybit activity page chain stops before supplied next page"
            )
        if index == len(pages) - 1 and next_cursor:
            raise ProviderActivityWindowObservationError(
                "Bybit activity page chain is incomplete"
            )
        expected_cursor = next_cursor
        query_digests.append(binding.query_digest)
        response_refs.append(page.evidence_ref)
        final_observed_at = page.observation.observed_at

    value = _issue_qualified_activity_window_no_match(
        provider_id=semantics.provider_id,
        account_id=semantics.account_id,
        environment=semantics.environment,
        surface="ACTIVITIES",
        endpoint=_ENDPOINT,
        client_order_id=target_id,
        category=category,
        coverage_start=_millis_utc_text(coverage_start_ms),
        coverage_end=_millis_utc_text(coverage_end_ms),
        qualification_id=semantics.qualification_id,
        parser_identity=semantics.parser_identity,
        route_semantics_digest=semantics.route_semantics_digest,
        activity_window_semantics_ref=semantics.evidence_ref,
        page_count=len(pages),
        query_digests=tuple(query_digests),
        provider_response_refs=tuple(response_refs),
        final_observed_at=final_observed_at,
        authority_journal_sequence_cut=semantics.authority_journal_sequence_cut,
    )
    _require_qualified_activity_window_no_match_authority(value)
    return value
