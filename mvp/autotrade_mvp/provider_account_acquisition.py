"""Durable serialized provider-account acquisition authority.

WP-20 uses this authority only for providers whose qualified reconciliation
semantics do not expose a provider-native comparable snapshot generation.
Issuance is journal-backed and globally CAS-bound: a serialized acquisition
generation is committed against the exact durable journal cut observed before
provider reads are allowed to begin.

This module does not perform provider I/O, interpret balances/positions, issue a
provider-account cut, qualify a provider, or authorize PAPER/LIVE trading.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import re
import threading
import weakref

from .persistence import (
    JournalStore,
    canonical_json,
    journal_store_authority_scope,
    payload_digest,
    require_exact_journal_store_authority,
)
from .provider_account_cut import ProviderAccountCutIdentity
from .provider_domain import ProviderFinancialScope
from .store_identity import JournalStoreIdentity, require_exact_journal_store_identity


_SCHEMA_VERSION = "1.0.0"
_OPERATION = "START_SERIALIZED_ACQUISITION"
_EVENT_TYPE = "ProviderAccountAcquisitionStarted.v1"
_AGGREGATE_TYPE = "provider_account_acquisition"
_ACQUISITION_MODE = "SERIALIZED_ACQUISITION_GENERATION"
_REQUEST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_ACQUISITION_ID_RE = re.compile(
    r"^provider-account-acquisition:sha256:[0-9a-f]{64}$"
)
_EVENT_ID_RE = re.compile(
    r"^provider-account-acquisition-start:sha256:[0-9a-f]{64}$"
)

# Retain the installed persistence callables used after caller-visible objects
# have been accepted. Public class rebinding must not silently redirect durable
# financial authority.
_CURRENT_JOURNAL_SEQUENCE = JournalStore.current_journal_sequence
_LOAD_EVENTS = JournalStore.load_events
_COMMIT_COMMAND = JournalStore.commit_command

# Provider/account identity is also financial authority.  Keep exact installed
# value types and pure canonicalization helpers so later public module/class
# rebinding cannot redirect durable acquisition identity after composition.
_CANONICAL_JSON = canonical_json
_PAYLOAD_DIGEST = payload_digest
_SHA256 = sha256
_PROVIDER_FINANCIAL_SCOPE_TYPE = ProviderFinancialScope
_PROVIDER_SCOPE_PAYLOAD = ProviderFinancialScope.payload
_PROVIDER_ACCOUNT_CUT_TYPE = ProviderAccountCutIdentity
_DATETIME_TYPE = datetime
_TIMEZONE_TYPE = timezone
_UTC = timezone.utc
_OBJECT_NEW = object.__new__
_OBJECT_SETATTR = object.__setattr__
_SCOPE_TOKEN_RE = re.compile(r"^[A-Z0-9][A-Z0-9_.:-]{0,127}$")
_SCOPE_RUNTIME_ENVIRONMENTS = frozenset({"SIMULATION", "PAPER", "LIVE"})


class ProviderAccountAcquisitionError(ValueError):
    """Raised when serialized provider-account acquisition authority is invalid."""


def _text(value: object, *, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ProviderAccountAcquisitionError(
            f"{name} must be canonical non-empty text"
        )
    return value


def _request_id(value: object) -> str:
    if type(value) is not str or _REQUEST_RE.fullmatch(value) is None:
        raise ProviderAccountAcquisitionError(
            "acquisition_request_id must be canonical bounded ASCII intent"
        )
    return value


def _positive_int(value: object, *, name: str) -> int:
    if type(value) is not int or value < 1:
        raise ProviderAccountAcquisitionError(
            f"{name} must be a positive exact integer"
        )
    return value


def _nonnegative_int(value: object, *, name: str) -> int:
    if type(value) is not int or value < 0:
        raise ProviderAccountAcquisitionError(
            f"{name} must be a non-negative exact integer"
        )
    return value


def _utc_text(value: object, *, name: str) -> str:
    # Exact datetime alone is insufficient: its tzinfo can still be a
    # caller-controlled subclass whose callbacks run during conversion.
    if (
        type(value) is not _DATETIME_TYPE
        or type(value.tzinfo) is not _TIMEZONE_TYPE
    ):
        raise ProviderAccountAcquisitionError(
            f"{name} must be exact datetime with datetime.timezone"
        )
    return _DATETIME_TYPE.astimezone(value, _UTC).isoformat().replace(
        "+00:00", "Z"
    )


def _scope_token_exact(value: object, *, name: str) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or value != value.upper()
        or _SCOPE_TOKEN_RE.fullmatch(value) is None
    ):
        raise ProviderAccountAcquisitionError(
            "provider_scope is not canonical at use time"
        )
    if name == "runtime_environment" and value not in _SCOPE_RUNTIME_ENVIRONMENTS:
        raise ProviderAccountAcquisitionError(
            "provider_scope is not canonical at use time"
        )
    return value


def _new_scope_exact(
    *,
    provider_id: object,
    runtime_environment: object,
    provider_environment: object,
    entity_policy_id: object,
) -> ProviderFinancialScope:
    candidate = _OBJECT_NEW(_PROVIDER_FINANCIAL_SCOPE_TYPE)
    _OBJECT_SETATTR(
        candidate,
        "provider_id",
        _scope_token_exact(provider_id, name="provider_id"),
    )
    _OBJECT_SETATTR(
        candidate,
        "runtime_environment",
        _scope_token_exact(runtime_environment, name="runtime_environment"),
    )
    _OBJECT_SETATTR(
        candidate,
        "provider_environment",
        _scope_token_exact(provider_environment, name="provider_environment"),
    )
    _OBJECT_SETATTR(
        candidate,
        "entity_policy_id",
        _scope_token_exact(entity_policy_id, name="entity_policy_id"),
    )
    return candidate


def _canonical_scope(value: object) -> ProviderFinancialScope:
    if type(value) is not _PROVIDER_FINANCIAL_SCOPE_TYPE:
        raise ProviderAccountAcquisitionError(
            "provider_scope must be exact ProviderFinancialScope"
        )
    # Copy only exact inert slots into a fresh exact value.  Do not invoke
    # caller-visible ProviderFinancialScope lifecycle/equality methods after
    # authority composition; those class attributes can be rebound at runtime.
    return _new_scope_exact(
        provider_id=value.provider_id,
        runtime_environment=value.runtime_environment,
        provider_environment=value.provider_environment,
        entity_policy_id=value.entity_policy_id,
    )


def _scope_payload_exact(value: object) -> dict[str, str]:
    return _PROVIDER_SCOPE_PAYLOAD(_canonical_scope(value))


def _scope_digest_exact(value: object) -> str:
    payload = _scope_payload_exact(value)
    digest = _SHA256(_CANONICAL_JSON(payload).encode("utf-8")).hexdigest()
    return "provider-financial-scope:sha256:" + digest


def _scope_from_payload(value: object) -> ProviderFinancialScope:
    if type(value) is not dict:
        raise ProviderAccountAcquisitionError(
            "provider acquisition scope payload must be an exact object"
        )
    expected = {
        "schema_version",
        "provider_id",
        "runtime_environment",
        "provider_environment",
        "entity_policy_id",
    }
    if set(value) != expected or any(type(key) is not str for key in value):
        raise ProviderAccountAcquisitionError(
            "provider acquisition scope payload shape is invalid"
        )
    if value["schema_version"] != "1.0.0":
        raise ProviderAccountAcquisitionError(
            "provider acquisition scope schema is unsupported"
        )
    return _new_scope_exact(
        provider_id=value["provider_id"],
        runtime_environment=value["runtime_environment"],
        provider_environment=value["provider_environment"],
        entity_policy_id=value["entity_policy_id"],
    )


def _aggregate_id(scope: ProviderFinancialScope, account_id: str) -> str:
    material = {
        "provider_scope_digest": _scope_digest_exact(scope),
        "account_id": account_id,
    }
    return "provider-account-acquisition:" + _SHA256(
        _CANONICAL_JSON(material).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class SerializedProviderAccountAcquisition:
    """One journal-issued same-scope serialized acquisition generation."""

    provider_scope: ProviderFinancialScope
    account_id: str
    acquisition_request_id: str
    acquisition_generation: int
    acquisition_journal_sequence_cut: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "provider_scope", _canonical_scope(self.provider_scope)
        )
        object.__setattr__(
            self, "account_id", _text(self.account_id, name="account_id")
        )
        object.__setattr__(
            self,
            "acquisition_request_id",
            _request_id(self.acquisition_request_id),
        )
        object.__setattr__(
            self,
            "acquisition_generation",
            _positive_int(
                self.acquisition_generation,
                name="acquisition_generation",
            ),
        )
        object.__setattr__(
            self,
            "acquisition_journal_sequence_cut",
            _nonnegative_int(
                self.acquisition_journal_sequence_cut,
                name="acquisition_journal_sequence_cut",
            ),
        )

    @property
    def aggregate_id(self) -> str:
        return _aggregate_id(self.provider_scope, self.account_id)

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "provider_scope": _scope_payload_exact(self.provider_scope),
            "provider_scope_digest": _scope_digest_exact(self.provider_scope),
            "account_id": self.account_id,
            "acquisition_mode": _ACQUISITION_MODE,
            "acquisition_request_id": self.acquisition_request_id,
            "acquisition_generation": self.acquisition_generation,
            "acquisition_journal_sequence_cut":
                self.acquisition_journal_sequence_cut,
        }

    @property
    def acquisition_id(self) -> str:
        digest = _SHA256(
            _CANONICAL_JSON(self.identity_payload()).encode("utf-8")
        ).hexdigest()
        return "provider-account-acquisition:sha256:" + digest

    def event_payload(self) -> dict[str, object]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "operation": _OPERATION,
            "identity": self.identity_payload(),
            "acquisition_id": self.acquisition_id,
        }

    @property
    def event_id(self) -> str:
        digest = _SHA256(
            _CANONICAL_JSON(self.event_payload()).encode("utf-8")
        ).hexdigest()
        return "provider-account-acquisition-start:sha256:" + digest

    @property
    def issued_journal_sequence(self) -> int:
        return self.acquisition_journal_sequence_cut + 1


def _same_acquisition(
    left: SerializedProviderAccountAcquisition,
    right: SerializedProviderAccountAcquisition,
) -> bool:
    if (
        type(left) is not SerializedProviderAccountAcquisition
        or type(right) is not SerializedProviderAccountAcquisition
    ):
        return False
    return (
        _scope_payload_exact(left.provider_scope)
        == _scope_payload_exact(right.provider_scope)
        and _text(left.account_id, name="account_id")
        == _text(right.account_id, name="account_id")
        and _request_id(left.acquisition_request_id)
        == _request_id(right.acquisition_request_id)
        and _positive_int(
            left.acquisition_generation,
            name="acquisition_generation",
        )
        == _positive_int(
            right.acquisition_generation,
            name="acquisition_generation",
        )
        and _nonnegative_int(
            left.acquisition_journal_sequence_cut,
            name="acquisition_journal_sequence_cut",
        )
        == _nonnegative_int(
            right.acquisition_journal_sequence_cut,
            name="acquisition_journal_sequence_cut",
        )
    )


def _acquisition_from_payload(value: object) -> SerializedProviderAccountAcquisition:
    if type(value) is not dict:
        raise ProviderAccountAcquisitionError(
            "provider acquisition event payload must be an exact object"
        )
    if set(value) != {
        "schema_version", "operation", "identity", "acquisition_id"
    } or any(type(key) is not str for key in value):
        raise ProviderAccountAcquisitionError(
            "provider acquisition event payload shape is invalid"
        )
    if value["schema_version"] != _SCHEMA_VERSION or value["operation"] != _OPERATION:
        raise ProviderAccountAcquisitionError(
            "provider acquisition event payload is unsupported"
        )
    identity = value["identity"]
    if type(identity) is not dict:
        raise ProviderAccountAcquisitionError(
            "provider acquisition identity must be an exact object"
        )
    expected_identity_keys = {
        "schema_version",
        "provider_scope",
        "provider_scope_digest",
        "account_id",
        "acquisition_mode",
        "acquisition_request_id",
        "acquisition_generation",
        "acquisition_journal_sequence_cut",
    }
    if set(identity) != expected_identity_keys or any(
        type(key) is not str for key in identity
    ):
        raise ProviderAccountAcquisitionError(
            "provider acquisition identity shape is invalid"
        )
    if identity["schema_version"] != _SCHEMA_VERSION:
        raise ProviderAccountAcquisitionError(
            "provider acquisition identity schema is unsupported"
        )
    if identity["acquisition_mode"] != _ACQUISITION_MODE:
        raise ProviderAccountAcquisitionError(
            "provider acquisition mode is not serialized"
        )
    scope = _scope_from_payload(identity["provider_scope"])
    if identity["provider_scope_digest"] != _scope_digest_exact(scope):
        raise ProviderAccountAcquisitionError(
            "provider acquisition scope digest mismatch"
        )
    acquisition = SerializedProviderAccountAcquisition(
        provider_scope=scope,
        account_id=identity["account_id"],
        acquisition_request_id=identity["acquisition_request_id"],
        acquisition_generation=identity["acquisition_generation"],
        acquisition_journal_sequence_cut=identity[
            "acquisition_journal_sequence_cut"
        ],
    )
    acquisition_id = value["acquisition_id"]
    if (
        type(acquisition_id) is not str
        or _ACQUISITION_ID_RE.fullmatch(acquisition_id) is None
        or acquisition_id != acquisition.acquisition_id
    ):
        raise ProviderAccountAcquisitionError(
            "provider acquisition content identity mismatch"
        )
    return acquisition


@dataclass(frozen=True, slots=True)
class _ReplayState:
    acquisitions: tuple[SerializedProviderAccountAcquisition, ...]
    request_index: dict[str, SerializedProviderAccountAcquisition]
    aggregate_version: int


_BINDINGS: dict[
    int,
    tuple[weakref.ReferenceType, weakref.ReferenceType, JournalStoreIdentity],
] = {}
_BINDINGS_LOCK = threading.RLock()


class DurableProviderAccountAcquisitionAuthority:
    """Issue and revalidate serialized provider-account acquisition generations."""

    __slots__ = ("store", "_journal_store_identity", "__weakref__")

    def __init_subclass__(cls, **_kwargs) -> None:
        raise TypeError(
            "DurableProviderAccountAcquisitionAuthority cannot be subclassed"
        )

    def __init__(self, store: JournalStore) -> None:
        if type(self) is not DurableProviderAccountAcquisitionAuthority:
            raise TypeError(
                "authority must be exact DurableProviderAccountAcquisitionAuthority"
            )
        authority_id = id(self)
        with _BINDINGS_LOCK:
            existing = _BINDINGS.get(authority_id)
            if existing is not None:
                existing_authority = existing[0]()
                if existing_authority is self:
                    raise ProviderAccountAcquisitionError(
                        "provider account acquisition authority is already initialized"
                    )
                if existing_authority is not None:
                    raise ProviderAccountAcquisitionError(
                        "provider account acquisition authority identity collision"
                    )
                _BINDINGS.pop(authority_id, None)
            selected = require_exact_journal_store_authority(
                store,
                subject="provider account acquisition journal",
            )
            visible = require_exact_journal_store_identity(
                selected,
                subject="visible provider account acquisition journal identity",
            )
            module_owned = require_exact_journal_store_identity(
                selected,
                subject="module-owned provider account acquisition journal identity",
            )
            self.store = store
            self._journal_store_identity = visible
            _BINDINGS[authority_id] = (
                weakref.ref(self),
                weakref.ref(store),
                module_owned,
            )

    def _journal_authority(self) -> tuple[JournalStore, JournalStoreIdentity]:
        if type(self) is not DurableProviderAccountAcquisitionAuthority:
            raise TypeError(
                "authority must be exact DurableProviderAccountAcquisitionAuthority"
            )
        with _BINDINGS_LOCK:
            binding = _BINDINGS.get(id(self))
        if binding is None or binding[0]() is not self:
            raise ProviderAccountAcquisitionError(
                "provider account acquisition composition is unavailable"
            )
        selected_store = binding[1]()
        if selected_store is None:
            raise ProviderAccountAcquisitionError(
                "provider account acquisition journal is unavailable"
            )
        expected = require_exact_journal_store_identity(
            binding[2],
            subject="module-owned provider account acquisition journal identity",
        )
        if self.store is not selected_store:
            raise ProviderAccountAcquisitionError(
                "provider account acquisition composition changed"
            )
        visible = require_exact_journal_store_identity(
            self._journal_store_identity,
            subject="visible provider account acquisition journal identity",
        )
        if visible != expected:
            raise ProviderAccountAcquisitionError(
                "provider account acquisition journal identity changed"
            )
        current = require_exact_journal_store_authority(
            selected_store,
            subject="provider account acquisition journal",
        )
        if current != expected:
            raise ProviderAccountAcquisitionError(
                "provider account acquisition journal authority changed"
            )
        return selected_store, expected

    def _replay(
        self,
        *,
        provider_scope: ProviderFinancialScope,
        account_id: str,
    ) -> _ReplayState:
        scope = _canonical_scope(provider_scope)
        account = _text(account_id, name="account_id")
        aggregate_id = _aggregate_id(scope, account)
        store, expected_store_identity = self._journal_authority()
        with journal_store_authority_scope(store, expected_store_identity):
            events = _LOAD_EVENTS(store, _AGGREGATE_TYPE, aggregate_id)

        acquisitions: list[SerializedProviderAccountAcquisition] = []
        request_index: dict[str, SerializedProviderAccountAcquisition] = {}
        expected_version = 1
        previous_journal_sequence = 0
        for event in events:
            if event.get("event_type") != _EVENT_TYPE:
                raise ProviderAccountAcquisitionError(
                    "unsupported provider account acquisition event"
                )
            if event.get("aggregate_type") != _AGGREGATE_TYPE:
                raise ProviderAccountAcquisitionError(
                    "provider acquisition aggregate type mismatch"
                )
            if event.get("aggregate_id") != aggregate_id:
                raise ProviderAccountAcquisitionError(
                    "provider acquisition aggregate id mismatch"
                )
            aggregate_version = event.get("aggregate_version")
            if aggregate_version != expected_version:
                raise ProviderAccountAcquisitionError(
                    "provider acquisition generation sequence is not contiguous"
                )
            if _PAYLOAD_DIGEST(event.get("payload")) != event.get("payload_hash"):
                raise ProviderAccountAcquisitionError(
                    "provider acquisition payload integrity failure"
                )
            acquisition = _acquisition_from_payload(event.get("payload"))
            if (
                _scope_payload_exact(acquisition.provider_scope)
                != _scope_payload_exact(scope)
                or acquisition.account_id != account
            ):
                raise ProviderAccountAcquisitionError(
                    "provider acquisition durable scope mismatch"
                )
            if acquisition.acquisition_generation != aggregate_version:
                raise ProviderAccountAcquisitionError(
                    "provider acquisition generation does not match aggregate version"
                )
            journal_sequence = event.get("journal_sequence")
            if type(journal_sequence) is not int or journal_sequence < 1:
                raise ProviderAccountAcquisitionError(
                    "provider acquisition lacks durable journal sequence"
                )
            if journal_sequence <= previous_journal_sequence:
                raise ProviderAccountAcquisitionError(
                    "provider acquisition journal sequence did not advance"
                )
            if journal_sequence != acquisition.issued_journal_sequence:
                raise ProviderAccountAcquisitionError(
                    "provider acquisition journal cut is not the exact pre-issuance cut"
                )
            if event.get("event_id") != acquisition.event_id:
                raise ProviderAccountAcquisitionError(
                    "provider acquisition event identity mismatch"
                )
            if (
                type(event.get("event_id")) is not str
                or _EVENT_ID_RE.fullmatch(event["event_id"]) is None
            ):
                raise ProviderAccountAcquisitionError(
                    "provider acquisition event id is non-canonical"
                )
            request_id = acquisition.acquisition_request_id
            if request_id in request_index:
                raise ProviderAccountAcquisitionError(
                    "duplicate durable acquisition_request_id in provider scope"
                )
            request_index[request_id] = acquisition
            acquisitions.append(acquisition)
            expected_version += 1
            previous_journal_sequence = journal_sequence

        return _ReplayState(
            acquisitions=tuple(acquisitions),
            request_index=request_index,
            aggregate_version=expected_version - 1,
        )

    def issue_serialized(
        self,
        *,
        provider_scope: ProviderFinancialScope,
        account_id: str,
        acquisition_request_id: str,
        committed_at: datetime,
    ) -> SerializedProviderAccountAcquisition:
        """Durably issue one serialized acquisition generation before provider I/O."""

        scope = _canonical_scope(provider_scope)
        account = _text(account_id, name="account_id")
        request_id = _request_id(acquisition_request_id)
        committed_at_text = _utc_text(committed_at, name="committed_at")

        state = self._replay(provider_scope=scope, account_id=account)
        previous = state.request_index.get(request_id)
        if previous is not None:
            if state.acquisitions and _same_acquisition(
                state.acquisitions[-1],
                previous,
            ):
                return previous
            raise ProviderAccountAcquisitionError(
                "acquisition_request_id belongs to a superseded acquisition"
            )

        store, expected_store_identity = self._journal_authority()
        with journal_store_authority_scope(store, expected_store_identity):
            journal_cut = _CURRENT_JOURNAL_SEQUENCE(store)

        acquisition = SerializedProviderAccountAcquisition(
            provider_scope=scope,
            account_id=account,
            acquisition_request_id=request_id,
            acquisition_generation=state.aggregate_version + 1,
            acquisition_journal_sequence_cut=journal_cut,
        )
        payload = acquisition.event_payload()
        envelope = {
            "event_id": acquisition.event_id,
            "event_type": _EVENT_TYPE,
            "schema_version": _SCHEMA_VERSION,
            "aggregate_type": _AGGREGATE_TYPE,
            "aggregate_id": acquisition.aggregate_id,
            "aggregate_version": str(acquisition.acquisition_generation),
            "host_id": "provider-account-acquisition",
            "owner_epoch": acquisition.acquisition_id,
            "environment": scope.runtime_environment,
            "occurred_at": committed_at_text,
            "observed_at": committed_at_text,
            "committed_at": committed_at_text,
            "correlation_id": acquisition.event_id,
            "causation_id": None,
            "payload": payload,
            "payload_hash": _PAYLOAD_DIGEST(payload),
            "evidence_refs": [],
        }
        command_material = {
            "operation": _OPERATION,
            "aggregate_id": acquisition.aggregate_id,
            "acquisition_request_id": request_id,
        }
        command_digest = _SHA256(
            _CANONICAL_JSON(command_material).encode("utf-8")
        ).hexdigest()
        command_id = "provider-account-acquisition-command:" + command_digest
        idempotency_key = "provider-account-acquisition:" + command_digest

        try:
            with journal_store_authority_scope(store, expected_store_identity):
                _COMMIT_COMMAND(
                    store,
                    command_id=command_id,
                    actor="provider-account-acquisition",
                    environment=scope.runtime_environment,
                    idempotency_key=idempotency_key,
                    request=command_material,
                    result=payload,
                    state_version=acquisition.acquisition_generation,
                    events=[(envelope, None)],
                    expected_journal_sequence=journal_cut,
                )
        except ValueError as error:
            current = self._replay(provider_scope=scope, account_id=account)
            persisted = current.request_index.get(request_id)
            if persisted is not None and _same_acquisition(
                current.acquisitions[-1],
                persisted,
            ):
                return persisted
            raise ProviderAccountAcquisitionError(
                "provider account acquisition changed concurrently; retry with "
                "a fresh acquisition_request_id only if the prior request was not persisted"
            ) from error

        current = self._replay(provider_scope=scope, account_id=account)
        persisted = current.request_index.get(request_id)
        if (
            persisted is None
            or not _same_acquisition(persisted, acquisition)
            or not _same_acquisition(current.acquisitions[-1], acquisition)
        ):
            raise ProviderAccountAcquisitionError(
                "provider account acquisition did not become durable current authority"
            )
        return acquisition

    def resolve_current(
        self,
        *,
        provider_scope: ProviderFinancialScope,
        account_id: str,
    ) -> SerializedProviderAccountAcquisition:
        state = self._replay(
            provider_scope=provider_scope,
            account_id=account_id,
        )
        if not state.acquisitions:
            raise ProviderAccountAcquisitionError(
                "no serialized provider account acquisition exists for scope"
            )
        return state.acquisitions[-1]

    def require_account_cut_acquisition(
        self,
        account_cut: ProviderAccountCutIdentity,
    ) -> SerializedProviderAccountAcquisition:
        """Bind a candidate account cut to the exact current serialized acquisition.

        This verifies only the acquisition/currentness dimension. It deliberately
        does not make the account cut accepted provider truth: provider-origin,
        consistency, coverage, qualification and financial projection issuers
        remain separate WP-20 prerequisites.
        """

        if type(account_cut) is not _PROVIDER_ACCOUNT_CUT_TYPE:
            raise ProviderAccountAcquisitionError(
                "account_cut must be exact ProviderAccountCutIdentity"
            )
        # This consumer verifies only the acquisition/currentness projection.
        # Copy and validate the exact inert fields it needs instead of
        # re-running caller-visible ProviderAccountCutIdentity lifecycle hooks.
        cut_scope = _canonical_scope(account_cut.provider_scope)
        cut_account = _text(account_cut.account_id, name="account_id")
        cut_mode = _text(account_cut.acquisition_mode, name="acquisition_mode")
        cut_acquisition_id = _text(
            account_cut.acquisition_id,
            name="acquisition_id",
        )
        cut_generation = _positive_int(
            account_cut.acquisition_generation,
            name="acquisition_generation",
        )
        cut_journal_sequence_cut = _nonnegative_int(
            account_cut.acquisition_journal_sequence_cut,
            name="acquisition_journal_sequence_cut",
        )
        if cut_mode != _ACQUISITION_MODE:
            raise ProviderAccountAcquisitionError(
                "account cut does not use serialized acquisition authority"
            )
        current = self.resolve_current(
            provider_scope=cut_scope,
            account_id=cut_account,
        )
        if cut_acquisition_id != current.acquisition_id:
            raise ProviderAccountAcquisitionError(
                "account cut acquisition_id is not current durable acquisition"
            )
        if cut_generation != current.acquisition_generation:
            raise ProviderAccountAcquisitionError(
                "account cut acquisition_generation is not current"
            )
        if cut_journal_sequence_cut != current.acquisition_journal_sequence_cut:
            raise ProviderAccountAcquisitionError(
                "account cut journal cut does not match current acquisition"
            )
        return current

    def require_current(
        self,
        acquisition: SerializedProviderAccountAcquisition,
    ) -> SerializedProviderAccountAcquisition:
        if type(acquisition) is not SerializedProviderAccountAcquisition:
            raise ProviderAccountAcquisitionError(
                "acquisition must be exact SerializedProviderAccountAcquisition"
            )
        acquisition_scope = _canonical_scope(acquisition.provider_scope)
        acquisition_account = _text(acquisition.account_id, name="account_id")
        _request_id(acquisition.acquisition_request_id)
        _positive_int(
            acquisition.acquisition_generation,
            name="acquisition_generation",
        )
        _nonnegative_int(
            acquisition.acquisition_journal_sequence_cut,
            name="acquisition_journal_sequence_cut",
        )
        current = self.resolve_current(
            provider_scope=acquisition_scope,
            account_id=acquisition_account,
        )
        if not _same_acquisition(current, acquisition):
            raise ProviderAccountAcquisitionError(
                "serialized provider account acquisition is superseded or forged"
            )
        return current
