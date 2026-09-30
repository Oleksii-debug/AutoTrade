"""Durable quantitative RiskPolicy selection authority.

This module owns *selection identity*, not risk arithmetic.  It persists validated
:class:`RiskPolicy` values in the canonical JournalStore, scopes them to the exact
provider/account/runtime/provider-environment/entity-policy/instrument-family
cut, and records explicit activation.  Historical resolution is reconstructed
from immutable journal events at an exact global journal sequence.

It intentionally does not modify AuthorityService yet.  The production risk
composition in #987 can consume this registry once the provider-domain/persistence
integration lineage is on main and bind the returned identity into
RiskAuthorityRequest / AuthoritativeRiskSnapshot.
"""
from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import datetime, timezone
from hashlib import sha256
import re
from typing import Mapping

from .exact_decimal import (
    ExactDecimalError,
    canonical_decimal_text,
    parse_canonical_decimal_text,
)
from .durable_event_taxonomy import RISK_POLICY_REGISTRY
from .persistence import JournalStore, canonical_json, payload_digest
from .risk import RISK_ENVIRONMENTS, RiskPolicy


class RiskPolicyAuthorityError(ValueError):
    """Raised when durable quantitative-policy authority is invalid or ambiguous."""


_AGGREGATE_TYPE = RISK_POLICY_REGISTRY.aggregate_type
_REGISTER_EVENT = "RiskPolicyRegistered.v1"
_ACTIVATE_EVENT = "RiskPolicyActivated.v1"
_ACTIVATE_EVENT_V2 = "RiskPolicyActivated.v2"
_SCHEMA_VERSION = "1.0.0"
_ACTIVATE_V2_SCHEMA_VERSION = "2.0.0"
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_ACTIVATION_EVENT_ID_RE = re.compile(r"^risk-policy-activate(?:-v2)?:[0-9a-f]{64}$")
_ACTIVATION_REQUEST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")

_DECIMAL_FIELDS = (
    "max_abs_position",
    "max_single_notional",
    "max_gross_leverage",
    "max_net_leverage",
    "max_daily_loss",
    "max_drawdown_fraction",
    "max_data_age_seconds",
    "max_fx_age_seconds",
    "min_margin_headroom",
    "max_stress_loss",
    "max_expected_shortfall",
    "expected_shortfall_tail_fraction",
    "min_liquidation_headroom",
    "max_asset_concentration_fraction",
    "max_venue_concentration_fraction",
    "max_order_participation_fraction",
    "max_abs_factor_exposure",
    "max_spread_fraction",
    "max_slippage_fraction",
    "max_clock_age_seconds",
    "min_futures_delivery_headroom_seconds",
)
_POLICY_VALUE_FIELDS = frozenset(
    {
        *_DECIMAL_FIELDS,
        "required_stress_scenario_labels",
        "required_stress_scenario_digests",
        "required_tail_scenario_set_digest",
        "allowed_actions",
        "require_settlement_evidence",
        "require_option_exercise_evidence",
    }
)
_POLICY_KEYS = frozenset({"schema_version", *_POLICY_VALUE_FIELDS})
_SCOPE_KEYS = frozenset(
    {
        "provider_id",
        "account_id",
        "environment",
        "provider_environment",
        "entity_policy_id",
        "instrument_family",
    }
)
_IDENTITY_KEYS = frozenset({"policy_id", "version", "content_digest", "scope"})
_REGISTER_KEYS = frozenset({"schema_version", "operation", "identity", "policy"})
_ACTIVATE_KEYS = frozenset({"schema_version", "operation", "identity"})
_ACTIVATE_V2_KEYS = frozenset({
    "schema_version", "operation", "identity", "activation_request_id",
    "expected_previous_activation_event_id",
})


def _text(value: object, *, name: str, upper: bool = False) -> str:
    if type(value) is not str or not value.strip():
        raise RiskPolicyAuthorityError(f"{name} must be non-empty text")
    normalized = value.strip()
    return normalized.upper() if upper else normalized


def _request_id(value: object) -> str:
    """A bounded, exact operator intent: no implicit strip or normalization."""
    if (
        type(value) is not str
        or len(value) > 128
        or _ACTIVATION_REQUEST_RE.fullmatch(value) is None
    ):
        raise RiskPolicyAuthorityError(
            "activation_request_id must be canonical non-empty ASCII intent"
        )
    return value


def _predecessor_id(value: object) -> str | None:
    if value is None:
        return None
    if (
        type(value) is not str
        or _ACTIVATION_EVENT_ID_RE.fullmatch(value) is None
    ):
        raise RiskPolicyAuthorityError(
            "expected_previous_activation_event_id must be a canonical activation event id"
        )
    return value


def _positive_int(value: object, *, name: str) -> int:
    if type(value) is not int or value < 1:
        raise RiskPolicyAuthorityError(f"{name} must be a positive integer")
    return value


def _digest(value: object, *, name: str) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise RiskPolicyAuthorityError(
            f"{name} must be canonical lowercase sha256:<64-hex>"
        )
    return value


def _utc_text(value: datetime, *, name: str) -> str:
    # Durable financial-authority timestamps must not dispatch through
    # caller-controlled datetime/tzinfo subclasses while normalizing.
    if type(value) is not datetime or type(value.tzinfo) is not timezone:
        raise RiskPolicyAuthorityError(
            f"{name} must be exact datetime with datetime.timezone"
        )
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _strict_mapping(
    value: object,
    *,
    name: str,
    keys: frozenset[str],
) -> Mapping[str, object]:
    if type(value) is not dict:
        raise RiskPolicyAuthorityError(f"{name} must be a JSON object")
    actual = frozenset(value)
    if actual != keys:
        raise RiskPolicyAuthorityError(
            f"{name} fields mismatch: missing={sorted(keys - actual)} "
            f"extra={sorted(actual - keys)}"
        )
    return value


@dataclass(frozen=True, order=True)
class RiskPolicyScope:
    """Exact scope for one quantitative-policy authority aggregate.

    ``provider_environment`` and ``entity_policy_id`` remain separate from the
    runtime environment.  This deliberately does not infer provider-local
    semantics (for example TESTNET vs DEMO); the canonical provider-domain owner
    supplies those identities and this registry preserves exact equality.
    """

    provider_id: str
    account_id: str
    environment: str
    provider_environment: str
    entity_policy_id: str
    instrument_family: str

    def __post_init__(self) -> None:
        provider_id = _text(self.provider_id, name="provider_id", upper=True)
        account_id = _text(self.account_id, name="account_id")
        environment = _text(self.environment, name="environment", upper=True)
        if environment not in RISK_ENVIRONMENTS:
            raise RiskPolicyAuthorityError("environment is unsupported")
        provider_environment = _text(
            self.provider_environment,
            name="provider_environment",
            upper=True,
        )
        entity_policy_id = _text(self.entity_policy_id, name="entity_policy_id")
        instrument_family = _text(
            self.instrument_family,
            name="instrument_family",
            upper=True,
        )
        object.__setattr__(self, "provider_id", provider_id)
        object.__setattr__(self, "account_id", account_id)
        object.__setattr__(self, "environment", environment)
        object.__setattr__(self, "provider_environment", provider_environment)
        object.__setattr__(self, "entity_policy_id", entity_policy_id)
        object.__setattr__(self, "instrument_family", instrument_family)

    def payload(self) -> dict[str, str]:
        return {
            "provider_id": self.provider_id,
            "account_id": self.account_id,
            "environment": self.environment,
            "provider_environment": self.provider_environment,
            "entity_policy_id": self.entity_policy_id,
            "instrument_family": self.instrument_family,
        }

    @property
    def aggregate_id(self) -> str:
        digest = sha256(canonical_json(self.payload()).encode("utf-8")).hexdigest()
        return "risk-policy-scope:" + digest


@dataclass(frozen=True)
class RiskPolicyIdentity:
    policy_id: str
    version: int
    content_digest: str
    scope: RiskPolicyScope

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "policy_id",
            _text(self.policy_id, name="policy_id"),
        )
        object.__setattr__(
            self,
            "version",
            _positive_int(self.version, name="version"),
        )
        object.__setattr__(
            self,
            "content_digest",
            _digest(self.content_digest, name="content_digest"),
        )
        if type(self.scope) is not RiskPolicyScope:
            raise TypeError("scope must be RiskPolicyScope")

    def payload(self) -> dict[str, object]:
        return {
            "policy_id": self.policy_id,
            "version": self.version,
            "content_digest": self.content_digest,
            "scope": self.scope.payload(),
        }


@dataclass(frozen=True)
class ResolvedRiskPolicy:
    """One immutable policy plus the durable events that establish its authority."""

    identity: RiskPolicyIdentity
    policy: RiskPolicy
    registration_event_id: str
    registration_journal_sequence: int
    activation_event_id: str
    activation_journal_sequence: int
    resolved_journal_sequence_cut: int

    def __post_init__(self) -> None:
        if type(self.identity) is not RiskPolicyIdentity:
            raise TypeError("identity must be RiskPolicyIdentity")
        if type(self.policy) is not RiskPolicy:
            raise TypeError("policy must be RiskPolicy")
        for name in ("registration_event_id", "activation_event_id"):
            _text(getattr(self, name), name=name)
        for name in (
            "registration_journal_sequence",
            "activation_journal_sequence",
        ):
            _positive_int(getattr(self, name), name=name)
        if (
            type(self.resolved_journal_sequence_cut) is not int
            or self.resolved_journal_sequence_cut < 0
        ):
            raise RiskPolicyAuthorityError(
                "resolved_journal_sequence_cut must be a non-negative integer"
            )
        if self.registration_journal_sequence > self.activation_journal_sequence:
            raise RiskPolicyAuthorityError("policy activation predates registration")
        if self.activation_journal_sequence > self.resolved_journal_sequence_cut:
            raise RiskPolicyAuthorityError("policy activation is newer than resolved cut")
        if risk_policy_digest(self.policy) != self.identity.content_digest:
            raise RiskPolicyAuthorityError("resolved policy content digest mismatch")

    @property
    def evidence_payload(self) -> dict[str, object]:
        return {
            "identity": self.identity.payload(),
            "registration_event_id": self.registration_event_id,
            "registration_journal_sequence": self.registration_journal_sequence,
            "activation_event_id": self.activation_event_id,
            "activation_journal_sequence": self.activation_journal_sequence,
            "resolved_journal_sequence_cut": self.resolved_journal_sequence_cut,
        }


def _decimal_text(value: object, *, name: str) -> str | None:
    if value is None:
        return None
    try:
        return canonical_decimal_text(value)  # exact-decimal TCB rejects subclasses
    except (ExactDecimalError, TypeError) as error:
        raise RiskPolicyAuthorityError(
            f"RiskPolicy.{name} is not a bounded exact Decimal"
        ) from error


def _require_complete_policy_schema() -> None:
    """Fail closed if RiskPolicy evolves without durable identity coverage."""

    runtime_fields = frozenset(field.name for field in fields(RiskPolicy))
    if runtime_fields != _POLICY_VALUE_FIELDS:
        raise RiskPolicyAuthorityError(
            "RiskPolicy schema changed without durable policy identity coverage: "
            f"missing={sorted(runtime_fields - _POLICY_VALUE_FIELDS)} "
            f"stale={sorted(_POLICY_VALUE_FIELDS - runtime_fields)}"
        )


def risk_policy_payload(policy: RiskPolicy) -> dict[str, object]:
    """Return context-independent canonical content for one validated RiskPolicy."""

    if type(policy) is not RiskPolicy:
        raise TypeError("policy must be exact RiskPolicy")
    _require_complete_policy_schema()
    payload: dict[str, object] = {"schema_version": _SCHEMA_VERSION}
    for name in _DECIMAL_FIELDS:
        payload[name] = _decimal_text(getattr(policy, name), name=name)

    labels = policy.required_stress_scenario_labels
    if labels is not None:
        if type(labels) is not tuple or any(type(value) is not str for value in labels):
            raise RiskPolicyAuthorityError(
                "RiskPolicy.required_stress_scenario_labels is not canonical"
            )
        payload["required_stress_scenario_labels"] = list(labels)
    else:
        payload["required_stress_scenario_labels"] = None

    digests = policy.required_stress_scenario_digests
    if digests is not None:
        if (
            type(digests) is not tuple
            or any(
                type(item) is not tuple
                or len(item) != 2
                or type(item[0]) is not str
                or type(item[1]) is not str
                for item in digests
            )
        ):
            raise RiskPolicyAuthorityError(
                "RiskPolicy.required_stress_scenario_digests is not canonical"
            )
        payload["required_stress_scenario_digests"] = [list(item) for item in digests]
    else:
        payload["required_stress_scenario_digests"] = None

    tail_digest = policy.required_tail_scenario_set_digest
    if tail_digest is not None:
        _digest(tail_digest, name="required_tail_scenario_set_digest")
    payload["required_tail_scenario_set_digest"] = tail_digest

    actions = policy.allowed_actions
    if actions is not None:
        if type(actions) is not tuple or any(type(value) is not str for value in actions):
            raise RiskPolicyAuthorityError("RiskPolicy.allowed_actions is not canonical")
        payload["allowed_actions"] = list(actions)
    else:
        payload["allowed_actions"] = None

    for name in ("require_settlement_evidence", "require_option_exercise_evidence"):
        value = getattr(policy, name)
        if type(value) is not bool:
            raise RiskPolicyAuthorityError(f"RiskPolicy.{name} must be boolean")
        payload[name] = value
    return payload


def risk_policy_digest(policy: RiskPolicy) -> str:
    material = canonical_json(risk_policy_payload(policy)).encode("utf-8")
    return "sha256:" + sha256(material).hexdigest()


def _policy_from_payload(value: object) -> RiskPolicy:
    payload = _strict_mapping(value, name="risk policy", keys=_POLICY_KEYS)
    if payload["schema_version"] != _SCHEMA_VERSION:
        raise RiskPolicyAuthorityError("unsupported risk policy schema_version")

    raw_digests = payload["required_stress_scenario_digests"]
    digests = None
    if raw_digests is not None:
        if type(raw_digests) is not list:
            raise RiskPolicyAuthorityError(
                "required_stress_scenario_digests must be an array or null"
            )
        digest_map: dict[str, str] = {}
        for item in raw_digests:
            if (
                type(item) is not list
                or len(item) != 2
                or type(item[0]) is not str
                or type(item[1]) is not str
                or item[0] in digest_map
            ):
                raise RiskPolicyAuthorityError(
                    "required_stress_scenario_digests is malformed"
                )
            digest_map[item[0]] = item[1]
        digests = digest_map

    raw_labels = payload["required_stress_scenario_labels"]
    if raw_labels is not None and (
        type(raw_labels) is not list
        or any(type(item) is not str for item in raw_labels)
    ):
        raise RiskPolicyAuthorityError(
            "required_stress_scenario_labels must be an array or null"
        )
    raw_actions = payload["allowed_actions"]
    if raw_actions is not None and (
        type(raw_actions) is not list
        or any(type(item) is not str for item in raw_actions)
    ):
        raise RiskPolicyAuthorityError("allowed_actions must be an array or null")

    values: dict[str, object] = {}
    for name in _DECIMAL_FIELDS:
        raw_value = payload[name]
        if raw_value is None:
            values[name] = None
            continue
        if type(raw_value) is not str:
            raise RiskPolicyAuthorityError(
                f"durable RiskPolicy.{name} must be canonical Decimal text"
            )
        try:
            values[name] = parse_canonical_decimal_text(raw_value)
        except ExactDecimalError as error:
            raise RiskPolicyAuthorityError(
                f"durable RiskPolicy.{name} is not canonical bounded Decimal text"
            ) from error

    try:
        policy = RiskPolicy.create(
            **values,
            required_stress_scenario_labels=raw_labels,
            required_stress_scenario_digests=digests,
            required_tail_scenario_set_digest=payload[
                "required_tail_scenario_set_digest"
            ],
            allowed_actions=raw_actions,
            require_settlement_evidence=payload["require_settlement_evidence"],
            require_option_exercise_evidence=payload[
                "require_option_exercise_evidence"
            ],
        )
    except (TypeError, ValueError) as error:
        raise RiskPolicyAuthorityError("durable RiskPolicy content is invalid") from error
    if risk_policy_payload(policy) != dict(payload):
        raise RiskPolicyAuthorityError("durable RiskPolicy content is not canonical")
    return policy


def _scope_from_payload(value: object) -> RiskPolicyScope:
    payload = _strict_mapping(value, name="risk policy scope", keys=_SCOPE_KEYS)
    scope = RiskPolicyScope(**payload)
    if scope.payload() != dict(payload):
        raise RiskPolicyAuthorityError("risk policy scope is not canonical")
    return scope


def _identity_from_payload(
    value: object,
    *,
    expected_scope: RiskPolicyScope,
) -> RiskPolicyIdentity:
    payload = _strict_mapping(value, name="risk policy identity", keys=_IDENTITY_KEYS)
    scope = _scope_from_payload(payload["scope"])
    identity = RiskPolicyIdentity(
        policy_id=payload["policy_id"],
        version=payload["version"],
        content_digest=payload["content_digest"],
        scope=scope,
    )
    if identity.payload() != dict(payload):
        raise RiskPolicyAuthorityError("risk policy identity is not canonical")
    if identity.scope != expected_scope:
        raise RiskPolicyAuthorityError("risk policy event scope mismatch")
    return identity


def _event_id(prefix: str, payload: Mapping[str, object]) -> str:
    return prefix + ":" + sha256(canonical_json(payload).encode("utf-8")).hexdigest()


@dataclass
class _ReplayState:
    registered: dict[
        tuple[str, int], tuple[RiskPolicyIdentity, RiskPolicy, str, int]
    ]
    active_key: tuple[str, int] | None
    activation_event_id: str | None
    activation_sequence: int | None
    max_version_by_policy_id: dict[str, int]
    max_activated_version_by_policy_id: dict[str, int]
    aggregate_version_at_cut: int
    activated_keys: set[tuple[str, int]]
    activation_requests: dict[str, tuple[RiskPolicyIdentity, str | None, str, int]]


class DurableRiskPolicyRegistry:
    """Append-only quantitative-policy registration and activation authority."""

    def __init__(self, store: JournalStore) -> None:
        # JournalStore is financial state authority. Accepting subclasses here
        # would dispatch replay/write calls through caller-controlled overrides
        # and let a forged chronology impersonate durable SQLite state.
        # Keep this boundary exact until persistence issues a sealed capability.
        if type(store) is not JournalStore:
            raise TypeError("store must be exact JournalStore")
        self.store = store

    def _replay(
        self,
        scope: RiskPolicyScope,
        *,
        journal_sequence_cut: int,
    ) -> _ReplayState:
        if type(scope) is not RiskPolicyScope:
            raise TypeError("scope must be RiskPolicyScope")
        if type(journal_sequence_cut) is not int or journal_sequence_cut < 0:
            raise RiskPolicyAuthorityError(
                "journal_sequence_cut must be a non-negative integer"
            )
        current = self.store.current_journal_sequence()
        if journal_sequence_cut > current:
            raise RiskPolicyAuthorityError(
                "journal_sequence_cut cannot be newer than the durable journal"
            )

        events = self.store.load_events(_AGGREGATE_TYPE, scope.aggregate_id)
        registered: dict[
            tuple[str, int], tuple[RiskPolicyIdentity, RiskPolicy, str, int]
        ] = {}
        max_version_by_policy_id: dict[str, int] = {}
        max_activated_version_by_policy_id: dict[str, int] = {}
        active_key: tuple[str, int] | None = None
        activation_event_id: str | None = None
        activation_sequence: int | None = None
        aggregate_version_at_cut = 0
        activated_keys: set[tuple[str, int]] = set()
        activation_requests: dict[
            str, tuple[RiskPolicyIdentity, str | None, str, int]
        ] = {}
        expected_aggregate_version = 1

        for event in events:
            if event.get("aggregate_type") != _AGGREGATE_TYPE:
                raise RiskPolicyAuthorityError("risk policy event aggregate type mismatch")
            if event.get("aggregate_id") != scope.aggregate_id:
                raise RiskPolicyAuthorityError("risk policy event aggregate id mismatch")
            aggregate_version = event.get("aggregate_version")
            if aggregate_version != expected_aggregate_version:
                raise RiskPolicyAuthorityError("risk policy aggregate version gap")
            expected_aggregate_version += 1
            sequence = event.get("journal_sequence")
            if type(sequence) is not int or sequence < 1:
                raise RiskPolicyAuthorityError(
                    "risk policy event lacks durable journal sequence"
                )
            if payload_digest(event.get("payload")) != event.get("payload_hash"):
                raise RiskPolicyAuthorityError("risk policy event payload integrity failure")
            if sequence > journal_sequence_cut:
                continue
            aggregate_version_at_cut = aggregate_version

            event_type = event.get("event_type")
            if event_type == _REGISTER_EVENT:
                payload = _strict_mapping(
                    event.get("payload"),
                    name="risk policy registration",
                    keys=_REGISTER_KEYS,
                )
                if (
                    payload["schema_version"] != _SCHEMA_VERSION
                    or payload["operation"] != "REGISTER"
                ):
                    raise RiskPolicyAuthorityError(
                        "unsupported risk policy registration payload"
                    )
                identity = _identity_from_payload(
                    payload["identity"],
                    expected_scope=scope,
                )
                policy = _policy_from_payload(payload["policy"])
                if risk_policy_digest(policy) != identity.content_digest:
                    raise RiskPolicyAuthorityError(
                        "risk policy registration content digest mismatch"
                    )
                expected_event_id = _event_id("risk-policy-register", payload)
                if event.get("event_id") != expected_event_id:
                    raise RiskPolicyAuthorityError(
                        "risk policy registration event identity mismatch"
                    )
                key = (identity.policy_id, identity.version)
                if key in registered:
                    raise RiskPolicyAuthorityError(
                        "duplicate durable risk policy identity"
                    )
                previous = max_version_by_policy_id.get(identity.policy_id, 0)
                if identity.version <= previous:
                    raise RiskPolicyAuthorityError(
                        "risk policy versions are not monotonic"
                    )
                max_version_by_policy_id[identity.policy_id] = identity.version
                registered[key] = (
                    identity,
                    policy,
                    event["event_id"],
                    sequence,
                )
            elif event_type in (_ACTIVATE_EVENT, _ACTIVATE_EVENT_V2):
                is_v2 = event_type == _ACTIVATE_EVENT_V2
                payload = _strict_mapping(
                    event.get("payload"),
                    name="risk policy activation",
                    keys=_ACTIVATE_V2_KEYS if is_v2 else _ACTIVATE_KEYS,
                )
                if (
                    payload["schema_version"]
                    != (_ACTIVATE_V2_SCHEMA_VERSION if is_v2 else _SCHEMA_VERSION)
                    or payload["operation"] != "ACTIVATE"
                ):
                    raise RiskPolicyAuthorityError(
                        "unsupported risk policy activation payload"
                    )
                identity = _identity_from_payload(
                    payload["identity"],
                    expected_scope=scope,
                )
                expected_event_id = _event_id(
                    "risk-policy-activate-v2" if is_v2 else "risk-policy-activate",
                    payload,
                )
                if event.get("event_id") != expected_event_id:
                    raise RiskPolicyAuthorityError(
                        "risk policy activation event identity mismatch"
                    )
                request_id: str | None = None
                predecessor: str | None = None
                if is_v2:
                    request_id = _request_id(payload["activation_request_id"])
                    predecessor = _predecessor_id(
                        payload["expected_previous_activation_event_id"]
                    )
                    if request_id in activation_requests:
                        raise RiskPolicyAuthorityError(
                            "duplicate durable activation_request_id in policy scope"
                        )
                    if predecessor != activation_event_id:
                        raise RiskPolicyAuthorityError(
                            "risk policy activation predecessor mismatch"
                        )
                key = (identity.policy_id, identity.version)
                registered_value = registered.get(key)
                if registered_value is None or registered_value[0] != identity:
                    raise RiskPolicyAuthorityError(
                        "risk policy activation references unregistered identity"
                    )
                highest_activated = max_activated_version_by_policy_id.get(
                    identity.policy_id,
                    0,
                )
                if identity.version < highest_activated:
                    raise RiskPolicyAuthorityError(
                        "risk policy activation cannot roll back a policy lineage"
                    )
                max_activated_version_by_policy_id[identity.policy_id] = max(
                    highest_activated,
                    identity.version,
                )
                activated_keys.add(key)
                active_key = key
                activation_event_id = event["event_id"]
                activation_sequence = sequence
                if request_id is not None:
                    activation_requests[request_id] = (
                        identity, predecessor, activation_event_id, sequence
                    )
            else:
                raise RiskPolicyAuthorityError(
                    "unsupported durable risk policy event type"
                )

        return _ReplayState(
            registered=registered,
            active_key=active_key,
            activation_event_id=activation_event_id,
            activation_sequence=activation_sequence,
            max_version_by_policy_id=max_version_by_policy_id,
            max_activated_version_by_policy_id=max_activated_version_by_policy_id,
            aggregate_version_at_cut=aggregate_version_at_cut,
            activated_keys=activated_keys,
            activation_requests=activation_requests,
        )

    def _current_state(self, scope: RiskPolicyScope) -> tuple[int, _ReplayState]:
        cut = self.store.current_journal_sequence()
        return cut, self._replay(scope, journal_sequence_cut=cut)

    def register(
        self,
        *,
        scope: RiskPolicyScope,
        policy_id: str,
        version: int,
        policy: RiskPolicy,
        committed_at: datetime,
    ) -> bool:
        """Persist immutable policy content. Exact replay is idempotent."""

        if type(scope) is not RiskPolicyScope:
            raise TypeError("scope must be RiskPolicyScope")
        if type(policy) is not RiskPolicy:
            raise TypeError("policy must be exact RiskPolicy")
        policy_id = _text(policy_id, name="policy_id")
        version = _positive_int(version, name="version")
        committed_at_text = _utc_text(committed_at, name="committed_at")
        content_digest = risk_policy_digest(policy)
        identity = RiskPolicyIdentity(
            policy_id=policy_id,
            version=version,
            content_digest=content_digest,
            scope=scope,
        )

        _cut, state = self._current_state(scope)
        key = (policy_id, version)
        existing = state.registered.get(key)
        if existing is not None:
            if existing[0] == identity and existing[1] == policy:
                return False
            raise RiskPolicyAuthorityError(
                "risk policy identity already exists with different content"
            )
        if version <= state.max_version_by_policy_id.get(policy_id, 0):
            raise RiskPolicyAuthorityError("risk policy version must advance monotonically")

        payload = {
            "schema_version": _SCHEMA_VERSION,
            "operation": "REGISTER",
            "identity": identity.payload(),
            "policy": risk_policy_payload(policy),
        }
        envelope = {
            "event_id": _event_id("risk-policy-register", payload),
            "event_type": _REGISTER_EVENT,
            "aggregate_type": _AGGREGATE_TYPE,
            "aggregate_id": scope.aggregate_id,
            "aggregate_version": str(state.aggregate_version_at_cut + 1),
            "payload": payload,
            "payload_hash": payload_digest(payload),
            "committed_at": committed_at_text,
        }
        try:
            return self.store.append_event(envelope).inserted
        except ValueError as error:
            _cut, current = self._current_state(scope)
            persisted = current.registered.get(key)
            if persisted is not None and persisted[0] == identity and persisted[1] == policy:
                return False
            raise RiskPolicyAuthorityError(
                "risk policy registry changed concurrently; registration must retry"
            ) from error

    def activate(
        self,
        *,
        scope: RiskPolicyScope,
        policy_id: str,
        version: int,
        committed_at: datetime,
        activation_request_id: str | None = None,
        expected_previous_activation_event_id: str | None = None,
    ) -> bool:
        """Select a policy through a durable, predecessor-bound activation episode.

        A request ID is an operator-issued intent, not a fresh random retry key.
        Exact accepted retries return False only while that *same episode* is
        active. A superseded episode is never reissued after a lost response.
        Historical no-intent callers retain first-activation compatibility,
        but cannot reselect an identity after a policy detour.
        """
        if type(scope) is not RiskPolicyScope:
            raise TypeError("scope must be RiskPolicyScope")
        policy_id = _text(policy_id, name="policy_id")
        version = _positive_int(version, name="version")
        committed_at_text = _utc_text(committed_at, name="committed_at")
        if activation_request_id is not None:
            activation_request_id = _request_id(activation_request_id)
        expected_previous_activation_event_id = _predecessor_id(
            expected_previous_activation_event_id
        )
        if (
            activation_request_id is None
            and expected_previous_activation_event_id is not None
        ):
            raise RiskPolicyAuthorityError(
                "activation predecessor requires explicit activation_request_id"
            )

        _cut, state = self._current_state(scope)
        key = (policy_id, version)
        registered = state.registered.get(key)
        if registered is None:
            raise RiskPolicyAuthorityError(
                "risk policy must be durably registered before activation"
            )
        identity = registered[0]
        if activation_request_id is not None:
            previous = state.activation_requests.get(activation_request_id)
            if previous is not None:
                if (
                    previous[0] != identity
                    or previous[1] != expected_previous_activation_event_id
                ):
                    raise RiskPolicyAuthorityError(
                        "activation_request_id conflicts with persisted intent"
                    )
                if state.activation_event_id == previous[2]:
                    return False
                raise RiskPolicyAuthorityError(
                    "activation_request_id belongs to a superseded episode"
                )
            if expected_previous_activation_event_id != state.activation_event_id:
                raise RiskPolicyAuthorityError(
                    "risk policy activation predecessor is stale"
                )
        if state.active_key == key:
            # A new explicit intent must be durably recorded or rejected.
            # Reporting a no-op here would falsely acknowledge an unbound ID
            # that could later be reused under a different episode chronology.
            if activation_request_id is not None:
                raise RiskPolicyAuthorityError(
                    "fresh activation_request_id cannot select already-active policy"
                )
            return False
        # A monotonic downgrade violation is more specific than the legacy
        # no-intent fallback and must retain its established fail-closed verdict.
        highest_activated = state.max_activated_version_by_policy_id.get(policy_id, 0)
        if version < highest_activated:
            raise RiskPolicyAuthorityError(
                "risk policy activation cannot roll back a policy lineage"
            )
        if activation_request_id is None and key in state.activated_keys:
            raise RiskPolicyAuthorityError(
                "legacy activation cannot reselect a historical episode; "
                "explicit activation_request_id and predecessor required"
            )

        if activation_request_id is None:
            # Preserve v1 first-selection wire shape and historical read support.
            payload = {
                "schema_version": _SCHEMA_VERSION,
                "operation": "ACTIVATE",
                "identity": identity.payload(),
            }
            event_id = _event_id("risk-policy-activate", payload)
            event_type = _ACTIVATE_EVENT
        else:
            payload = {
                "schema_version": _ACTIVATE_V2_SCHEMA_VERSION,
                "operation": "ACTIVATE",
                "identity": identity.payload(),
                "activation_request_id": activation_request_id,
                "expected_previous_activation_event_id":
                    expected_previous_activation_event_id,
            }
            event_id = _event_id("risk-policy-activate-v2", payload)
            event_type = _ACTIVATE_EVENT_V2
        envelope = {
            "event_id": event_id,
            "event_type": event_type,
            "aggregate_type": _AGGREGATE_TYPE,
            "aggregate_id": scope.aggregate_id,
            "aggregate_version": str(state.aggregate_version_at_cut + 1),
            "payload": payload,
            "payload_hash": payload_digest(payload),
            "committed_at": committed_at_text,
        }
        try:
            result = self.store.append_event(envelope)
            if result.inserted:
                return True
        except ValueError as error:
            # A stale same-scope aggregate CAS or duplicate event ID never
            # implies success just because another request selected the same key.
            _cut, current = self._current_state(scope)
            if activation_request_id is not None:
                persisted = current.activation_requests.get(activation_request_id)
                if persisted is not None:
                    if (
                        persisted[0] != identity
                        or persisted[1] != expected_previous_activation_event_id
                    ):
                        raise RiskPolicyAuthorityError(
                            "activation_request_id conflicts with persisted intent"
                        ) from error
                    if current.activation_event_id == persisted[2]:
                        return False
                    raise RiskPolicyAuthorityError(
                        "activation_request_id belongs to a superseded episode"
                    ) from error
            elif (
                current.active_key == key
                and current.activation_event_id == event_id
            ):
                return False
            raise RiskPolicyAuthorityError(
                "risk policy registry changed concurrently; activation must retry "
                "with current predecessor and a new explicit request"
            ) from error

        # Defensive duplicate-result path for compatible JournalStore versions.
        _cut, current = self._current_state(scope)
        if activation_request_id is not None:
            persisted = current.activation_requests.get(activation_request_id)
            if persisted is not None:
                if (
                    persisted[0] != identity
                    or persisted[1] != expected_previous_activation_event_id
                ):
                    raise RiskPolicyAuthorityError(
                        "activation_request_id conflicts with persisted intent"
                    )
                if current.activation_event_id == persisted[2]:
                    return False
                raise RiskPolicyAuthorityError(
                    "activation_request_id belongs to a superseded episode"
                )
        elif current.active_key == key and current.activation_event_id == event_id:
            return False
        raise RiskPolicyAuthorityError(
            "risk policy activation duplicate result has no current exact episode"
        )

    def resolve_current(
        self,
        scope: RiskPolicyScope,
        *,
        journal_sequence_cut: int | None = None,
    ) -> ResolvedRiskPolicy:
        """Resolve the policy active at exactly ``journal_sequence_cut``.

        Later registrations/activations are intentionally ignored.  The full
        journal event remains immutable and integrity-checked; exact accepted
        admission retry is expected to use its persisted risk snapshot directly,
        not re-resolve mutable current policy.
        """

        if type(scope) is not RiskPolicyScope:
            raise TypeError("scope must be RiskPolicyScope")
        current = self.store.current_journal_sequence()
        cut = current if journal_sequence_cut is None else journal_sequence_cut
        state = self._replay(scope, journal_sequence_cut=cut)
        if (
            state.active_key is None
            or state.activation_event_id is None
            or state.activation_sequence is None
        ):
            raise RiskPolicyAuthorityError(
                "no unambiguous active quantitative RiskPolicy exists at requested cut"
            )
        identity, policy, registration_event_id, registration_sequence = state.registered[
            state.active_key
        ]
        return ResolvedRiskPolicy(
            identity=identity,
            policy=policy,
            registration_event_id=registration_event_id,
            registration_journal_sequence=registration_sequence,
            activation_event_id=state.activation_event_id,
            activation_journal_sequence=state.activation_sequence,
            resolved_journal_sequence_cut=cut,
        )
