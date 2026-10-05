"""Settle qualified provider negative observations after source-signed horizons.

The inputs to this module are already sealed exact provider observations.  This
layer adds the missing temporal authority: the exact current Q must sign a
minimum consistency horizon for the exact endpoint/read rule/parser, and the
provider observation must occur after that delay from the ambiguous send.

For bounded history/activity windows, the requested window must also have ended
no later than the provider observation.  This prevents a request whose endTime
lies in the future from masquerading as complete negative coverage.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import weakref

from .durable_provider_qualification import DurableProviderQualificationRegistry
from .persistence import canonical_json
from .provider_activity_window_observation import (
    QualifiedActivityWindowNoMatchObservation,
    _require_qualified_activity_window_no_match_authority,
)
from .provider_negative_result_horizon import (
    _require_qualified_negative_result_horizon_authority,
    require_qualified_negative_result_horizon,
)
from .provider_negative_result_observation import (
    QualifiedEmptyIdentityObservation,
    _require_qualified_empty_identity_observation_authority,
)
from .provider_negative_result_query_scope import (
    CanonicalNegativeResultQueryScope,
    _require_negative_result_query_scope_authority,
)
from .provider_route_reads import (
    QualifiedProviderReadQueryBinding,
    _require_qualified_provider_read_binding_authority,
)
from .provider_selection import SelectedProviderRoute


class ProviderNegativeResultSettlementError(ValueError):
    pass


def _instant(value: object, name: str) -> datetime:
    if type(value) is datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ProviderNegativeResultSettlementError(
                f"{name} must include timezone"
            )
        return value.astimezone(timezone.utc)
    if type(value) is str and value and value == value.strip():
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise ProviderNegativeResultSettlementError(
                f"{name} must be an exact ISO timestamp"
            ) from error
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ProviderNegativeResultSettlementError(
                f"{name} must include timezone"
            )
        return parsed.astimezone(timezone.utc)
    raise ProviderNegativeResultSettlementError(
        f"{name} must be timezone-aware datetime or canonical ISO text"
    )


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, slots=True, weakref_slot=True, init=False)
class SettledNegativeSurfaceObservation:
    provider_id: str
    account_id: str
    environment: str
    surface: str
    endpoint: str
    client_order_id: str
    qualification_id: str
    parser_identity: str
    route_semantics_digest: str
    observation_ref: str
    horizon_ref: str
    minimum_horizon_ms: int
    submission_at: str
    observed_at: str
    coverage_start: str
    coverage_end: str
    authority_journal_sequence_cut: int

    def __init__(self, *_args, **_kwargs) -> None:
        raise ProviderNegativeResultSettlementError(
            "settled negative surfaces must come from signed provider horizon authority"
        )

    @property
    def evidence_ref(self) -> str:
        _require_settled_negative_surface_authority(self)
        material = {
            "provider_id": self.provider_id,
            "account_id": self.account_id,
            "environment": self.environment,
            "surface": self.surface,
            "endpoint": self.endpoint,
            "client_order_id": self.client_order_id,
            "qualification_id": self.qualification_id,
            "parser_identity": self.parser_identity,
            "route_semantics_digest": self.route_semantics_digest,
            "observation_ref": self.observation_ref,
            "horizon_ref": self.horizon_ref,
            "minimum_horizon_ms": self.minimum_horizon_ms,
            "submission_at": self.submission_at,
            "observed_at": self.observed_at,
            "coverage_start": self.coverage_start,
            "coverage_end": self.coverage_end,
            "authority_journal_sequence_cut": self.authority_journal_sequence_cut,
        }
        return "settled-negative-provider-surface:sha256:" + sha256(
            canonical_json(material).encode("utf-8")
        ).hexdigest()


def _install_settled_surface_authority():
    states: dict[int, tuple[weakref.ReferenceType, tuple[object, ...]]] = {}
    fields = (
        "provider_id",
        "account_id",
        "environment",
        "surface",
        "endpoint",
        "client_order_id",
        "qualification_id",
        "parser_identity",
        "route_semantics_digest",
        "observation_ref",
        "horizon_ref",
        "minimum_horizon_ms",
        "submission_at",
        "observed_at",
        "coverage_start",
        "coverage_end",
        "authority_journal_sequence_cut",
    )

    def prune() -> None:
        for object_id, (value_ref, _snapshot) in tuple(states.items()):
            if value_ref() is None:
                states.pop(object_id, None)

    def issue(**material: object) -> SettledNegativeSurfaceObservation:
        prune()
        value = object.__new__(SettledNegativeSurfaceObservation)
        for field_name in fields:
            object.__setattr__(value, field_name, material[field_name])
        states[id(value)] = (
            weakref.ref(value),
            tuple(material[field_name] for field_name in fields),
        )
        return value

    def require(value: object) -> None:
        if type(value) is not SettledNegativeSurfaceObservation:
            raise ProviderNegativeResultSettlementError(
                "settled negative surface authority requires exact sealed value"
            )
        prune()
        state = states.get(id(value))
        if state is None or state[0]() is not value:
            raise ProviderNegativeResultSettlementError(
                "settled negative surface construction authority is unavailable"
            )
        current = tuple(getattr(value, field_name) for field_name in fields)
        if current != state[1]:
            raise ProviderNegativeResultSettlementError(
                "settled negative surface changed after horizon authorization"
            )

    return issue, require


(
    _issue_settled_negative_surface,
    _require_settled_negative_surface_authority,
) = _install_settled_surface_authority()
del _install_settled_surface_authority


def _require_elapsed_horizon(
    *,
    submission_at: datetime,
    observed_at: datetime,
    minimum_horizon_ms: int,
) -> None:
    if observed_at < submission_at:
        raise ProviderNegativeResultSettlementError(
            "provider negative observation predates ambiguous submission"
        )
    earliest = submission_at + timedelta(milliseconds=minimum_horizon_ms)
    if observed_at < earliest:
        raise ProviderNegativeResultSettlementError(
            "provider negative observation has not satisfied signed consistency horizon"
        )


def settle_direct_negative_surface(
    *,
    route: SelectedProviderRoute,
    read_binding: QualifiedProviderReadQueryBinding,
    query_scope: CanonicalNegativeResultQueryScope,
    observation: QualifiedEmptyIdentityObservation,
    qualification_registry: DurableProviderQualificationRegistry,
    submission_at: datetime,
    at: datetime,
) -> SettledNegativeSurfaceObservation:
    """Settle one direct Bybit OPEN_ORDERS/ORDER_HISTORY/EXECUTIONS negative."""

    if type(route) is not SelectedProviderRoute:
        raise TypeError("route must be exact SelectedProviderRoute")
    if type(read_binding) is not QualifiedProviderReadQueryBinding:
        raise TypeError("read_binding must be exact QualifiedProviderReadQueryBinding")
    if type(query_scope) is not CanonicalNegativeResultQueryScope:
        raise TypeError("query_scope must be exact CanonicalNegativeResultQueryScope")
    if type(observation) is not QualifiedEmptyIdentityObservation:
        raise TypeError("observation must be exact QualifiedEmptyIdentityObservation")
    try:
        _require_qualified_provider_read_binding_authority(read_binding)
        _require_negative_result_query_scope_authority(query_scope)
        _require_qualified_empty_identity_observation_authority(observation)
    except Exception as error:
        raise ProviderNegativeResultSettlementError(
            "direct negative settlement prerequisite authority is unavailable"
        ) from error

    if (
        observation.qualified_query_digest != read_binding.query_digest
        or observation.query_scope_ref != query_scope.evidence_ref
        or observation.provider_id != query_scope.provider_id
        or observation.account_id != query_scope.account_id
        or observation.environment != query_scope.environment
        or observation.endpoint != query_scope.endpoint
        or observation.client_order_id != query_scope.client_order_id
        or read_binding.qualification_id != observation.qualification_id
    ):
        raise ProviderNegativeResultSettlementError(
            "direct negative observation does not match exact read/query authority"
        )

    horizon = require_qualified_negative_result_horizon(
        route=route,
        read_binding=read_binding,
        qualification_registry=qualification_registry,
        at=at,
    )
    _require_qualified_negative_result_horizon_authority(horizon)
    if (
        horizon.qualification_id != observation.qualification_id
        or horizon.provider_id != observation.provider_id
        or horizon.account_id != observation.account_id
        or horizon.environment != observation.environment
        or horizon.endpoint != observation.endpoint
        or horizon.parser_identity != observation.parser_identity
        or horizon.route_semantics_digest != observation.route_semantics_digest
    ):
        raise ProviderNegativeResultSettlementError(
            "signed horizon does not match direct negative observation authority"
        )

    submitted = _instant(submission_at, "submission_at")
    observed = _instant(observation.observed_at, "observation.observed_at")
    _require_elapsed_horizon(
        submission_at=submitted,
        observed_at=observed,
        minimum_horizon_ms=horizon.minimum_horizon_ms,
    )

    coverage_start = submitted
    coverage_end = observed
    if query_scope.coverage_start_ms is not None:
        if query_scope.coverage_end_ms is None:
            raise ProviderNegativeResultSettlementError(
                "bounded direct query has incomplete temporal scope"
            )
        coverage_start = datetime.fromtimestamp(
            query_scope.coverage_start_ms / 1000,
            tz=timezone.utc,
        )
        coverage_end = datetime.fromtimestamp(
            query_scope.coverage_end_ms / 1000,
            tz=timezone.utc,
        )
        if not coverage_start <= submitted <= coverage_end:
            raise ProviderNegativeResultSettlementError(
                "bounded direct query does not cover ambiguous submission"
            )
        if coverage_end > observed:
            raise ProviderNegativeResultSettlementError(
                "bounded direct negative query ends after provider observation"
            )

    value = _issue_settled_negative_surface(
        provider_id=observation.provider_id,
        account_id=observation.account_id,
        environment=observation.environment,
        surface=observation.surface,
        endpoint=observation.endpoint,
        client_order_id=observation.client_order_id,
        qualification_id=observation.qualification_id,
        parser_identity=observation.parser_identity,
        route_semantics_digest=observation.route_semantics_digest,
        observation_ref=observation.evidence_ref,
        horizon_ref=horizon.evidence_ref,
        minimum_horizon_ms=horizon.minimum_horizon_ms,
        submission_at=_utc_text(submitted),
        observed_at=_utc_text(observed),
        coverage_start=_utc_text(coverage_start),
        coverage_end=_utc_text(coverage_end),
        authority_journal_sequence_cut=horizon.authority_journal_sequence_cut,
    )
    _require_settled_negative_surface_authority(value)
    return value


def settle_activity_negative_surface(
    *,
    route: SelectedProviderRoute,
    representative_read_binding: QualifiedProviderReadQueryBinding,
    observation: QualifiedActivityWindowNoMatchObservation,
    qualification_registry: DurableProviderQualificationRegistry,
    submission_at: datetime,
    at: datetime,
) -> SettledNegativeSurfaceObservation:
    """Settle a complete ACTIVITIES page-chain after its signed Q horizon."""

    if type(route) is not SelectedProviderRoute:
        raise TypeError("route must be exact SelectedProviderRoute")
    if type(representative_read_binding) is not QualifiedProviderReadQueryBinding:
        raise TypeError(
            "representative_read_binding must be exact QualifiedProviderReadQueryBinding"
        )
    if type(observation) is not QualifiedActivityWindowNoMatchObservation:
        raise TypeError(
            "observation must be exact QualifiedActivityWindowNoMatchObservation"
        )
    try:
        _require_qualified_provider_read_binding_authority(representative_read_binding)
        _require_qualified_activity_window_no_match_authority(observation)
    except Exception as error:
        raise ProviderNegativeResultSettlementError(
            "activity negative settlement prerequisite authority is unavailable"
        ) from error
    if (
        not observation.query_digests
        or observation.query_digests[0] != representative_read_binding.query_digest
        or representative_read_binding.qualification_id != observation.qualification_id
    ):
        raise ProviderNegativeResultSettlementError(
            "activity no-match observation does not match representative exact read"
        )

    horizon = require_qualified_negative_result_horizon(
        route=route,
        read_binding=representative_read_binding,
        qualification_registry=qualification_registry,
        at=at,
    )
    _require_qualified_negative_result_horizon_authority(horizon)
    if (
        horizon.qualification_id != observation.qualification_id
        or horizon.provider_id != observation.provider_id
        or horizon.account_id != observation.account_id
        or horizon.environment != observation.environment
        or horizon.endpoint != observation.endpoint
        or horizon.parser_identity != observation.parser_identity
        or horizon.route_semantics_digest != observation.route_semantics_digest
    ):
        raise ProviderNegativeResultSettlementError(
            "signed horizon does not match activity no-match observation authority"
        )

    submitted = _instant(submission_at, "submission_at")
    observed = _instant(observation.final_observed_at, "observation.final_observed_at")
    coverage_start = _instant(observation.coverage_start, "observation.coverage_start")
    coverage_end = _instant(observation.coverage_end, "observation.coverage_end")
    if not coverage_start <= submitted <= coverage_end:
        raise ProviderNegativeResultSettlementError(
            "activity window does not cover ambiguous submission"
        )
    if coverage_end > observed:
        raise ProviderNegativeResultSettlementError(
            "activity negative window ends after final provider observation"
        )
    _require_elapsed_horizon(
        submission_at=submitted,
        observed_at=observed,
        minimum_horizon_ms=horizon.minimum_horizon_ms,
    )

    value = _issue_settled_negative_surface(
        provider_id=observation.provider_id,
        account_id=observation.account_id,
        environment=observation.environment,
        surface=observation.surface,
        endpoint=observation.endpoint,
        client_order_id=observation.client_order_id,
        qualification_id=observation.qualification_id,
        parser_identity=observation.parser_identity,
        route_semantics_digest=observation.route_semantics_digest,
        observation_ref=observation.evidence_ref,
        horizon_ref=horizon.evidence_ref,
        minimum_horizon_ms=horizon.minimum_horizon_ms,
        submission_at=_utc_text(submitted),
        observed_at=_utc_text(observed),
        coverage_start=_utc_text(coverage_start),
        coverage_end=_utc_text(coverage_end),
        authority_journal_sequence_cut=horizon.authority_journal_sequence_cut,
    )
    _require_settled_negative_surface_authority(value)
    return value
