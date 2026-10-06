"""Durable quantitative RiskPolicy selection authority.

This module owns *selection identity*, not risk arithmetic.  It persists validated
:class:`RiskPolicy` values in the canonical JournalStore, scopes them to the exact
provider/account/runtime/provider-environment/entity-policy/instrument-family
cut, and records explicit activation.  Historical resolution is reconstructed
from immutable journal events at an exact global journal sequence.

AuthorityService consumes the registry-issued result at its exact financial
cut when a durable policy scope is selected. Provider-free orchestration uses
that composition; PAPER/LIVE product-owned issuer qualification remains separate.
"""
from __future__ import annotations

from dataclasses import InitVar, dataclass, field, fields
from datetime import datetime, timezone
from hashlib import sha256
import re
import threading
from typing import Mapping
import weakref

from .exact_decimal import (
    ExactDecimalError,
    canonical_decimal_text,
    parse_canonical_decimal_text,
)
from .persistence import (
    JournalStore,
    canonical_json,
    journal_store_authority_scope,
    payload_digest,
    require_exact_journal_store_authority,
)
from .risk import RISK_ENVIRONMENTS, RiskPolicy
from .store_identity import JournalStoreIdentity, require_exact_journal_store_identity


_RESOLVED_POLICY_AUTHORITY_TOKEN = object()


class RiskPolicyAuthorityError(ValueError):
    """Raised when durable quantitative-policy authority is invalid or ambiguous."""


_AGGREGATE_TYPE = "risk_policy_registry"
_REGISTER_EVENT = "RiskPolicyRegistered.v1"
_ACTIVATE_EVENT = "RiskPolicyActivated.v1"
_ACTIVATE_EVENT_V2 = "RiskPolicyActivated.v2"
_SCHEMA_VERSION = "1.0.0"
_ACTIVATE_V2_SCHEMA_VERSION = "2.0.0"
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_ACTIVATION_EVENT_ID_RE = re.compile(r"^risk-policy-activate(?:-v2)?:[0-9a-f]{64}$")
_ACTIVATION_REQUEST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def _canonical_journal_authority_snapshot(
    store: JournalStore,
) -> JournalStoreIdentity:
    """Consume the persistence-owned exact physical journal authority."""
    return require_exact_journal_store_authority(
        store,
        subject="current risk policy journal",
    )

def _journal_store_identity_payload(
    identity: JournalStoreIdentity,
) -> dict[str, object]:
    exact = require_exact_journal_store_identity(
        identity,
        subject="resolved risk policy journal identity",
    )
    if exact.identity_source == "windows_by_handle":
        # Windows path spelling is not physical JournalStore identity.  Reopen
        # continuity is carried by the retained HANDLE volume/file-index tuple.
        return {
            "schema_version": "1.0.0",
            "identity_source": exact.identity_source,
            "windows_volume_serial": exact.windows_volume_serial,
            "windows_file_index_high": exact.windows_file_index_high,
            "windows_file_index_low": exact.windows_file_index_low,
        }
    return {
        "schema_version": "1.0.0",
        "identity_source": exact.identity_source,
        "canonical_path": exact.canonical_path,
        "filesystem_device": exact.filesystem_device,
        "filesystem_inode": exact.filesystem_inode,
    }

def journal_store_identity_digest(identity: JournalStoreIdentity) -> str:
    return payload_digest(_journal_store_identity_payload(identity))


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
_SCOPE_FIELDS = (
    "provider_id",
    "account_id",
    "environment",
    "provider_environment",
    "entity_policy_id",
    "instrument_family",
)
_SCOPE_KEYS = frozenset(_SCOPE_FIELDS)
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
    """Exact scope for one quantitative-policy authority aggregate."""

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
        values = _require_canonical_scope(self)
        return dict(zip(_SCOPE_FIELDS, values, strict=True))

    @property
    def aggregate_id(self) -> str:
        digest = sha256(canonical_json(self.payload()).encode("utf-8")).hexdigest()
        return "risk-policy-scope:" + digest


def _require_canonical_scope(value: object) -> tuple[str, ...]:
    """Return one callback-safe canonical scope snapshot for authority use."""

    if type(value) is not RiskPolicyScope:
        raise TypeError("scope must be exact RiskPolicyScope")
    state = vars(value)
    state_names = tuple(state)
    if any(type(name) is not str for name in state_names):
        raise RiskPolicyAuthorityError("risk policy scope state keys must be exact str")
    if frozenset(state_names) != _SCOPE_KEYS:
        raise RiskPolicyAuthorityError("risk policy scope state shape is non-canonical")
    raw = tuple(state[name] for name in _SCOPE_FIELDS)
    if any(type(item) is not str for item in raw):
        raise RiskPolicyAuthorityError("risk policy scope fields must be exact text")
    canonical = RiskPolicyScope(*raw)
    canonical_state = vars(canonical)
    canonical_values = tuple(canonical_state[name] for name in _SCOPE_FIELDS)
    if raw != canonical_values:
        raise RiskPolicyAuthorityError("risk policy scope is not canonical")
    return raw


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
        canonical_scope = RiskPolicyScope(*_require_canonical_scope(self.scope))
        object.__setattr__(self, "scope", canonical_scope)

    def payload(self) -> dict[str, object]:
        return {
            "policy_id": self.policy_id,
            "version": self.version,
            "content_digest": self.content_digest,
            "scope": self.scope.payload(),
        }


@dataclass(frozen=True, slots=True, weakref_slot=True)
class ResolvedRiskPolicy:
    """One immutable policy plus the durable events that establish its authority."""

    identity: RiskPolicyIdentity
    policy: RiskPolicy
    registration_event_id: str
    registration_journal_sequence: int
    activation_event_id: str
    activation_journal_sequence: int
    resolved_journal_sequence_cut: int
    journal_store_identity_digest: str
    _authority_token: InitVar[object | None] = None
    _authority_digest: str = field(init=False, repr=False, compare=False)

    def __post_init__(self, _authority_token: object | None) -> None:
        if _authority_token is not _RESOLVED_POLICY_AUTHORITY_TOKEN:
            raise RiskPolicyAuthorityError(
                "ResolvedRiskPolicy must be issued by DurableRiskPolicyRegistry"
            )
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
        object.__setattr__(
            self,
            "journal_store_identity_digest",
            _digest(
                self.journal_store_identity_digest,
                name="journal_store_identity_digest",
            ),
        )
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
        object.__setattr__(
            self,
            "_authority_digest",
            _resolved_policy_authority_digest(self),
        )

    @property
    def evidence_payload(self) -> dict[str, object]:
        return {
            "identity": self.identity.payload(),
            "registration_event_id": self.registration_event_id,
            "registration_journal_sequence": self.registration_journal_sequence,
            "activation_event_id": self.activation_event_id,
            "activation_journal_sequence": self.activation_journal_sequence,
            "resolved_journal_sequence_cut": self.resolved_journal_sequence_cut,
            "journal_store_identity_digest": self.journal_store_identity_digest,
        }


def _canonical_resolved_policy_identity(
    value: object,
) -> RiskPolicyIdentity:
    if type(value) is not RiskPolicyIdentity:
        raise RiskPolicyAuthorityError(
            "resolved RiskPolicy identity must be exact RiskPolicyIdentity"
        )
    raw = vars(value)
    if any(type(key) is not str for key in raw):
        raise RiskPolicyAuthorityError(
            "resolved RiskPolicy identity state keys must be exact strings"
        )
    if frozenset(raw) != _IDENTITY_KEYS:
        raise RiskPolicyAuthorityError(
            "resolved RiskPolicy identity state shape is not canonical"
        )
    return RiskPolicyIdentity(
        policy_id=_text(raw["policy_id"], name="policy_id"),
        version=_positive_int(raw["version"], name="version"),
        content_digest=_digest(raw["content_digest"], name="content_digest"),
        scope=RiskPolicyScope(*_require_canonical_scope(raw["scope"])),
    )


def _resolved_policy_authority_digest(value: ResolvedRiskPolicy) -> str:
    identity = _canonical_resolved_policy_identity(value.identity)
    if type(value.policy) is not RiskPolicy:
        raise RiskPolicyAuthorityError(
            "resolved RiskPolicy content must be exact RiskPolicy"
        )
    registration_event_id = _text(
        value.registration_event_id,
        name="registration_event_id",
    )
    activation_event_id = _text(
        value.activation_event_id,
        name="activation_event_id",
    )
    registration_sequence = _positive_int(
        value.registration_journal_sequence,
        name="registration_journal_sequence",
    )
    activation_sequence = _positive_int(
        value.activation_journal_sequence,
        name="activation_journal_sequence",
    )
    cut = value.resolved_journal_sequence_cut
    if type(cut) is not int or cut < 0:
        raise RiskPolicyAuthorityError(
            "resolved_journal_sequence_cut must be a non-negative integer"
        )
    if registration_sequence > activation_sequence or activation_sequence > cut:
        raise RiskPolicyAuthorityError(
            "resolved RiskPolicy chronology is not canonical"
        )
    policy_digest = risk_policy_digest(value.policy)
    if policy_digest != identity.content_digest:
        raise RiskPolicyAuthorityError(
            "resolved policy content digest mismatch"
        )
    return payload_digest(
        {
            "schema_version": "1.0.0",
            "identity": identity.payload(),
            "policy_digest": policy_digest,
            "registration_event_id": registration_event_id,
            "registration_journal_sequence": str(registration_sequence),
            "activation_event_id": activation_event_id,
            "activation_journal_sequence": str(activation_sequence),
            "resolved_journal_sequence_cut": str(cut),
            "journal_store_identity_digest": _digest(
                value.journal_store_identity_digest,
                name="journal_store_identity_digest",
            ),
        }
    )


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
    """Return callback-safe canonical content for one validated RiskPolicy."""

    if type(policy) is not RiskPolicy:
        raise TypeError("policy must be exact RiskPolicy")
    _require_complete_policy_schema()
    state = vars(policy)
    state_names = tuple(state)
    if any(type(name) is not str for name in state_names):
        raise RiskPolicyAuthorityError("RiskPolicy state keys must be exact str")
    if frozenset(state_names) != _POLICY_VALUE_FIELDS:
        raise RiskPolicyAuthorityError("RiskPolicy state shape is non-canonical")

    payload: dict[str, object] = {"schema_version": _SCHEMA_VERSION}
    for name in _DECIMAL_FIELDS:
        payload[name] = _decimal_text(state[name], name=name)

    labels = state["required_stress_scenario_labels"]
    if labels is not None:
        if type(labels) is not tuple or any(type(value) is not str for value in labels):
            raise RiskPolicyAuthorityError(
                "RiskPolicy.required_stress_scenario_labels is not canonical"
            )
        payload["required_stress_scenario_labels"] = list(labels)
    else:
        payload["required_stress_scenario_labels"] = None

    digests = state["required_stress_scenario_digests"]
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

    tail_digest = state["required_tail_scenario_set_digest"]
    if tail_digest is not None:
        _digest(tail_digest, name="required_tail_scenario_set_digest")
    payload["required_tail_scenario_set_digest"] = tail_digest

    actions = state["allowed_actions"]
    if actions is not None:
        if type(actions) is not tuple or any(type(value) is not str for value in actions):
            raise RiskPolicyAuthorityError("RiskPolicy.allowed_actions is not canonical")
        payload["allowed_actions"] = list(actions)
    else:
        payload["allowed_actions"] = None

    for name in ("require_settlement_evidence", "require_option_exercise_evidence"):
        value = state[name]
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


def canonical_risk_policy(policy: RiskPolicy) -> RiskPolicy:
    """Return one detached exact-base RiskPolicy after canonical content validation."""

    return _policy_from_payload(risk_policy_payload(policy))


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
    expected_scope_values = _require_canonical_scope(expected_scope)
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
    if _require_canonical_scope(identity.scope) != expected_scope_values:
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


# Compatibility/diagnostic surface only. Financial composition authority is
# deliberately NOT stored here: callers can import and mutate module globals.
_RISK_POLICY_REGISTRY_BINDINGS: dict[
    int,
    tuple[weakref.ReferenceType, weakref.ReferenceType, JournalStoreIdentity],
] = {}


def _risk_policy_registry_binding_operations():
    """Return closure-owned one-shot registry composition operations.

    Callback-free weak references are deliberate. WeakKeyDictionary installs a
    caller-discoverable removal callback on its key weakref; invoking that
    callback manually can erase a live composition binding. Key by id instead,
    retain weak refs with no callbacks, and prove referent identity at every use.
    """

    bindings: dict[
        int,
        tuple[weakref.ReferenceType, weakref.ReferenceType, JournalStoreIdentity],
    ] = {}
    lock = threading.RLock()

    def bind(
        registry: "DurableRiskPolicyRegistry",
        store: JournalStore,
    ) -> JournalStoreIdentity:
        registry_id = id(registry)
        with lock:
            existing = bindings.get(registry_id)
            if existing is not None:
                existing_registry = existing[0]()
                if existing_registry is registry:
                    raise RiskPolicyAuthorityError(
                        "risk policy registry composition is already initialized"
                    )
                if existing_registry is not None:
                    raise RiskPolicyAuthorityError(
                        "risk policy registry binding identity collision"
                    )
                # Python reused the id of a genuinely dead registry.
                bindings.pop(registry_id, None)

            selected_identity = _canonical_journal_authority_snapshot(store)
            visible_identity = require_exact_journal_store_identity(
                selected_identity,
                subject="selected risk policy journal identity",
            )
            module_identity = require_exact_journal_store_identity(
                selected_identity,
                subject="module-owned risk policy journal identity",
            )
            bindings[registry_id] = (
                weakref.ref(registry),
                weakref.ref(store),
                module_identity,
            )
            return visible_identity

    def resolve(
        registry: "DurableRiskPolicyRegistry",
    ) -> tuple[
        weakref.ReferenceType,
        weakref.ReferenceType,
        JournalStoreIdentity,
    ] | None:
        with lock:
            return bindings.get(id(registry))

    return bind, resolve


(
    _bind_risk_policy_registry,
    _resolve_risk_policy_registry_binding,
) = _risk_policy_registry_binding_operations()


class DurableRiskPolicyRegistry:
    """Append-only quantitative-policy registration and activation authority."""

    __slots__ = ("_journal_store_identity", "store", "__weakref__")

    def __init_subclass__(cls, **_kwargs) -> None:
        raise TypeError("DurableRiskPolicyRegistry cannot be subclassed")

    def __init__(self, store: JournalStore) -> None:
        if type(self) is not DurableRiskPolicyRegistry:
            raise TypeError("registry must be exact DurableRiskPolicyRegistry")
        # Python permits explicit re-entry into __init__ on an existing object.
        # The closure-owned binding rejects re-entry before inspecting a replacement
        # store, and caller-visible fields below remain diagnostics only.
        visible_identity = _bind_risk_policy_registry(self, store)
        self._journal_store_identity = visible_identity
        self.store = store

    def _journal_store_authority(self) -> tuple[JournalStore, JournalStoreIdentity]:
        if type(self) is not DurableRiskPolicyRegistry:
            raise TypeError("registry must be exact DurableRiskPolicyRegistry")
        binding = _resolve_risk_policy_registry_binding(self)
        if binding is None:
            raise RiskPolicyAuthorityError(
                "risk policy registry lacks original journal composition"
            )
        selected_registry_ref, selected_store_ref, module_identity = binding
        if selected_registry_ref() is not self:
            raise RiskPolicyAuthorityError(
                "risk policy registry binding identity changed"
            )
        selected_store = selected_store_ref()
        if selected_store is None:
            raise RiskPolicyAuthorityError(
                "risk policy registry original journal is unavailable"
            )
        expected = require_exact_journal_store_identity(
            module_identity,
            subject="module-owned risk policy journal identity",
        )
        if self.store is not selected_store:
            raise RiskPolicyAuthorityError(
                "risk policy registry composition changed"
            )
        visible_identity = require_exact_journal_store_identity(
            self._journal_store_identity,
            subject="selected risk policy journal identity",
        )
        if visible_identity != expected:
            raise RiskPolicyAuthorityError(
                "risk policy registry composition changed"
            )
        identity = _canonical_journal_authority_snapshot(selected_store)
        if identity != expected:
            raise RiskPolicyAuthorityError(
                "risk policy journal authority changed"
            )
        return selected_store, expected

    def _replay(
        self,
        scope: RiskPolicyScope,
        *,
        journal_sequence_cut: int,
    ) -> _ReplayState:
        scope = RiskPolicyScope(*_require_canonical_scope(scope))
        if type(journal_sequence_cut) is not int or journal_sequence_cut < 0:
            raise RiskPolicyAuthorityError(
                "journal_sequence_cut must be a non-negative integer"
            )
        store, expected_store_identity = (
            DurableRiskPolicyRegistry._journal_store_authority(self)
        )
        with journal_store_authority_scope(store, expected_store_identity):
            current = JournalStore.current_journal_sequence(store)
        if journal_sequence_cut > current:
            raise RiskPolicyAuthorityError(
                "journal_sequence_cut cannot be newer than the durable journal"
            )

        with journal_store_authority_scope(store, expected_store_identity):
            events = JournalStore.load_events(
                store,
                _AGGREGATE_TYPE,
                scope.aggregate_id,
            )
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
        store, expected_store_identity = (
            DurableRiskPolicyRegistry._journal_store_authority(self)
        )
        with journal_store_authority_scope(store, expected_store_identity):
            cut = JournalStore.current_journal_sequence(store)
        return cut, DurableRiskPolicyRegistry._replay(self, scope, journal_sequence_cut=cut)

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

        scope = RiskPolicyScope(*_require_canonical_scope(scope))
        if type(policy) is not RiskPolicy:
            raise TypeError("policy must be exact RiskPolicy")
        policy_payload = risk_policy_payload(policy)
        canonical_policy = _policy_from_payload(policy_payload)
        policy_id = _text(policy_id, name="policy_id")
        version = _positive_int(version, name="version")
        committed_at_text = _utc_text(committed_at, name="committed_at")
        content_digest = "sha256:" + sha256(
            canonical_json(policy_payload).encode("utf-8")
        ).hexdigest()
        identity = RiskPolicyIdentity(
            policy_id=policy_id,
            version=version,
            content_digest=content_digest,
            scope=scope,
        )

        _cut, state = DurableRiskPolicyRegistry._current_state(self, scope)
        key = (policy_id, version)
        existing = state.registered.get(key)
        if existing is not None:
            if existing[0] == identity and existing[1] == canonical_policy:
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
            "policy": policy_payload,
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
            store, expected_store_identity = (
                DurableRiskPolicyRegistry._journal_store_authority(self)
            )
            with journal_store_authority_scope(store, expected_store_identity):
                return JournalStore.append_event(store, envelope).inserted
        except ValueError as error:
            _cut, current = DurableRiskPolicyRegistry._current_state(self, scope)
            persisted = current.registered.get(key)
            if persisted is not None and persisted[0] == identity and persisted[1] == canonical_policy:
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
        scope = RiskPolicyScope(*_require_canonical_scope(scope))
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

        _cut, state = DurableRiskPolicyRegistry._current_state(self, scope)
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
            store, expected_store_identity = (
                DurableRiskPolicyRegistry._journal_store_authority(self)
            )
            with journal_store_authority_scope(store, expected_store_identity):
                result = JournalStore.append_event(store, envelope)
            if result.inserted:
                return True
        except ValueError as error:
            # A stale same-scope aggregate CAS or duplicate event ID never
            # implies success just because another request selected the same key.
            _cut, current = DurableRiskPolicyRegistry._current_state(self, scope)
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
        _cut, current = DurableRiskPolicyRegistry._current_state(self, scope)
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

        scope = RiskPolicyScope(*_require_canonical_scope(scope))
        store, expected_store_identity = (
            DurableRiskPolicyRegistry._journal_store_authority(self)
        )
        with journal_store_authority_scope(store, expected_store_identity):
            current = JournalStore.current_journal_sequence(store)
        cut = current if journal_sequence_cut is None else journal_sequence_cut
        if type(cut) is not int or cut < 0:
            raise RiskPolicyAuthorityError(
                "journal_sequence_cut must be a non-negative integer"
            )
        if cut > current:
            raise RiskPolicyAuthorityError(
                "journal_sequence_cut cannot be newer than the durable journal"
            )
        state = DurableRiskPolicyRegistry._replay(self, scope, journal_sequence_cut=cut)
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
        resolved = ResolvedRiskPolicy(
            identity=identity,
            policy=policy,
            registration_event_id=registration_event_id,
            registration_journal_sequence=registration_sequence,
            activation_event_id=state.activation_event_id,
            activation_journal_sequence=state.activation_sequence,
            resolved_journal_sequence_cut=cut,
            journal_store_identity_digest=journal_store_identity_digest(
                expected_store_identity
            ),
            _authority_token=_RESOLVED_POLICY_AUTHORITY_TOKEN,
        )
        return resolved

def _install_resolved_policy_issuance_authority():
    """Install a closure-owned issuance capability on the registry resolver.

    The mutable binding table and the registrar are deliberately not module
    globals. Importing the constructor token or recomputing the caller-visible
    diagnostic digest therefore cannot register a caller-constructed result.
    """

    lock = threading.RLock()
    bindings: dict[int, tuple[weakref.ReferenceType, str]] = {}
    original_resolve = DurableRiskPolicyRegistry.resolve_current

    def bind_issued(value: ResolvedRiskPolicy) -> ResolvedRiskPolicy:
        if type(value) is not ResolvedRiskPolicy:
            raise TypeError("resolved policy must be exact ResolvedRiskPolicy")
        issued_digest = _resolved_policy_authority_digest(value)
        if value._authority_digest != issued_digest:
            raise RiskPolicyAuthorityError(
                "resolved RiskPolicy authority changed before registry issuance"
            )
        key = id(value)
        with lock:
            existing = bindings.get(key)
            if existing is not None:
                referent = existing[0]()
                if referent is value:
                    if existing[1] != issued_digest:
                        raise RiskPolicyAuthorityError(
                            "resolved RiskPolicy issuance binding changed"
                        )
                    return value
                if referent is not None:
                    raise RiskPolicyAuthorityError(
                        "resolved RiskPolicy issuance identity collision"
                    )
                bindings.pop(key, None)
            # Callback-free weakrefs prevent caller-invoked removal callbacks
            # from erasing issuance truth for a live result.
            bindings[key] = (weakref.ref(value), issued_digest)
        return value

    def sealed_resolve_current(
        self,
        scope: RiskPolicyScope,
        *,
        journal_sequence_cut: int | None = None,
    ) -> ResolvedRiskPolicy:
        resolved = original_resolve(
            self,
            scope,
            journal_sequence_cut=journal_sequence_cut,
        )
        return bind_issued(resolved)

    def require_issued(value: object) -> ResolvedRiskPolicy:
        if type(value) is not ResolvedRiskPolicy:
            raise TypeError("resolved policy must be exact ResolvedRiskPolicy")
        key = id(value)
        with lock:
            binding = bindings.get(key)
        if binding is None or binding[0]() is not value:
            raise RiskPolicyAuthorityError(
                "resolved RiskPolicy was not issued by DurableRiskPolicyRegistry"
            )
        issued_digest = binding[1]
        try:
            current_digest = _resolved_policy_authority_digest(value)
        except (RiskPolicyAuthorityError, TypeError, ValueError) as error:
            raise RiskPolicyAuthorityError(
                "resolved RiskPolicy authority changed after registry issuance"
            ) from error
        if value._authority_digest != issued_digest or current_digest != issued_digest:
            raise RiskPolicyAuthorityError(
                "resolved RiskPolicy authority changed after registry issuance"
            )
        return value

    DurableRiskPolicyRegistry.resolve_current = sealed_resolve_current
    return require_issued


require_registry_issued_resolved_policy = (
    _install_resolved_policy_issuance_authority()
)
del _install_resolved_policy_issuance_authority
