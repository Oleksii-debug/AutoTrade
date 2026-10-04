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
    # caller-controlled subclass whose callbacks run during astimezone().
    if type(value) is not datetime or type(value.tzinfo) is not timezone:
        raise ProviderAccountAcquisitionError(
            f"{name} must be exact datetime with datetime.timezone"
        )
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_scope(value: object) -> ProviderFinancialScope:
    if type(value) is not ProviderFinancialScope:
        raise ProviderAccountAcquisitionError(
            "provider_scope must be exact ProviderFinancialScope"
        )
    state = value.payload()
    candidate = ProviderFinancialScope(
        provider_id=state["provider_id"],
        runtime_environment=state["runtime_environment"],
        provider_environment=state["provider_environment"],
        entity_policy_id=state["entity_policy_id"],
    )
    if candidate != value or candidate.content_digest != value.content_digest:
        raise ProviderAccountAcquisitionError(
            "provider_scope is not canonical at use time"
        )
    return candidate


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
    return ProviderFinancialScope(
        provider_id=value["provider_id"],
        runtime_environment=value["runtime_environment"],
        provider_environment=value["provider_environment"],
        entity_policy_id=value["entity_policy_id"],
    )


def _aggregate_id(scope: ProviderFinancialScope, account_id: str) -> str:
    material = {
        "provider_scope_digest": scope.content_digest,
        "account_id": account_id,
    }
    return "provider-account-acquisition:" + sha256(
        canonical_json(material).encode("utf-8")
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
            "provider_scope": self.provider_scope.payload(),
            "provider_scope_digest": self.provider_scope.content_digest,
            "account_id": self.account_id,
            "acquisition_mode": _ACQUISITION_MODE,
            "acquisition_request_id": self.acquisition_request_id,
            "acquisition_generation": self.acquisition_generation,
            "acquisition_journal_sequence_cut":
                self.acquisition_journal_sequence_cut,
        }

    @property
    def acquisition_id(self) -> str:
        digest = sha256(
            canonical_json(self.identity_payload()).encode("utf-8")
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
        digest = sha256(
            canonical_json(self.event_payload()).encode("utf-8")
        ).hexdigest()
        return "provider-account-acquisition-start:sha256:" + digest

    @property
    def issued_journal_sequence(self) -> int:
        return self.acquisition_journal_sequence_cut + 1


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
    if identity["provider_scope_digest"] != scope.content_digest:
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


def _build_provider_account_acquisition_binding_methods():
    """Keep exact JournalStore selection outside caller-mutable module state."""

    bindings: dict[
        int,
        tuple[weakref.ReferenceType, weakref.ReferenceType, JournalStoreIdentity],
    ] = {}
    lock = threading.RLock()

    def registered(
        value: object,
    ) -> tuple[weakref.ReferenceType, JournalStoreIdentity] | None:
        object_id = id(value)
        entry = bindings.get(object_id)
        if entry is None:
            return None
        value_ref, store_ref, identity = entry
        current = value_ref()
        if current is value:
            return store_ref, identity
        if current is None:
            bindings.pop(object_id, None)
            return None
        raise ProviderAccountAcquisitionError(
            "provider account acquisition authority identity collision"
        )

    def initialize(value: object, store: JournalStore) -> None:
        if type(value) is not DurableProviderAccountAcquisitionAuthority:
            raise TypeError(
                "authority must be exact DurableProviderAccountAcquisitionAuthority"
            )
        if type(store) is not JournalStore:
            raise TypeError("store must be exact JournalStore")
        with lock:
            if registered(value) is not None:
                raise ProviderAccountAcquisitionError(
                    "provider account acquisition authority is already initialized"
                )
            selected = require_exact_journal_store_authority(
                store,
                subject="provider account acquisition journal",
            )
            visible = require_exact_journal_store_identity(
                selected,
                subject="visible provider account acquisition journal identity",
            )
            retained = require_exact_journal_store_identity(
                selected,
                subject="retained provider account acquisition journal identity",
            )
            object.__setattr__(value, "store", store)
            object.__setattr__(value, "_journal_store_identity", visible)
            bindings[id(value)] = (
                weakref.ref(value),
                weakref.ref(store),
                retained,
            )

    def require(
        value: object,
    ) -> tuple[JournalStore, JournalStoreIdentity]:
        if type(value) is not DurableProviderAccountAcquisitionAuthority:
            raise TypeError(
                "authority must be exact DurableProviderAccountAcquisitionAuthority"
            )
        with lock:
            binding = registered(value)
        if binding is None:
            raise ProviderAccountAcquisitionError(
                "provider account acquisition composition is unavailable"
            )
        store_ref, retained_identity = binding
        selected_store = store_ref()
        if selected_store is None:
            raise ProviderAccountAcquisitionError(
                "provider account acquisition journal is unavailable"
            )
        expected = require_exact_journal_store_identity(
            retained_identity,
            subject="retained provider account acquisition journal identity",
        )
        if object.__getattribute__(value, "store") is not selected_store:
            raise ProviderAccountAcquisitionError(
                "provider account acquisition composition changed"
            )
        visible = require_exact_journal_store_identity(
            object.__getattribute__(value, "_journal_store_identity"),
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

    return initialize, require


class DurableProviderAccountAcquisitionAuthority:
    """Issue and revalidate serialized provider-account acquisition generations."""

    __slots__ = ("store", "_journal_store_identity", "__weakref__")

    def __init_subclass__(cls, **_kwargs) -> None:
        raise TypeError(
            "DurableProviderAccountAcquisitionAuthority cannot be subclassed"
        )

    __init__, _journal_authority = (
        _build_provider_account_acquisition_binding_methods()
    )

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
            if payload_digest(event.get("payload")) != event.get("payload_hash"):
                raise ProviderAccountAcquisitionError(
                    "provider acquisition payload integrity failure"
                )
            acquisition = _acquisition_from_payload(event.get("payload"))
            if acquisition.provider_scope != scope or acquisition.account_id != account:
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
            if state.acquisitions and state.acquisitions[-1] == previous:
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
            "payload_hash": payload_digest(payload),
            "evidence_refs": [],
        }
        command_material = {
            "operation": _OPERATION,
            "aggregate_id": acquisition.aggregate_id,
            "acquisition_request_id": request_id,
        }
        command_digest = sha256(
            canonical_json(command_material).encode("utf-8")
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
            if persisted is not None and current.acquisitions[-1] == persisted:
                return persisted
            raise ProviderAccountAcquisitionError(
                "provider account acquisition changed concurrently; retry with "
                "a fresh acquisition_request_id only if the prior request was not persisted"
            ) from error

        current = self._replay(provider_scope=scope, account_id=account)
        persisted = current.request_index.get(request_id)
        if persisted != acquisition or current.acquisitions[-1] != acquisition:
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

        if type(account_cut) is not ProviderAccountCutIdentity:
            raise ProviderAccountAcquisitionError(
                "account_cut must be exact ProviderAccountCutIdentity"
            )
        # Reconstruct the frozen value so post-construction mutation cannot
        # smuggle a non-canonical nested identity through this consumer fence.
        canonical_cut = ProviderAccountCutIdentity(
            provider_scope=account_cut.provider_scope,
            account_id=account_cut.account_id,
            acquisition_mode=account_cut.acquisition_mode,
            acquisition_id=account_cut.acquisition_id,
            acquisition_generation=account_cut.acquisition_generation,
            acquisition_journal_sequence_cut=
                account_cut.acquisition_journal_sequence_cut,
            qualification_identity_digest=
                account_cut.qualification_identity_digest,
            consistency_method_id=account_cut.consistency_method_id,
            consistency_method_version=
                account_cut.consistency_method_version,
            origin_binding_set_digest=account_cut.origin_binding_set_digest,
            stream_binding_set_digest=account_cut.stream_binding_set_digest,
            backfill_binding_set_digest=account_cut.backfill_binding_set_digest,
            coverage_window_digest=account_cut.coverage_window_digest,
            provider_native_generation_token=
                account_cut.provider_native_generation_token,
        )
        if canonical_cut.acquisition_mode != _ACQUISITION_MODE:
            raise ProviderAccountAcquisitionError(
                "account cut does not use serialized acquisition authority"
            )
        current = self.resolve_current(
            provider_scope=canonical_cut.provider_scope,
            account_id=canonical_cut.account_id,
        )
        if canonical_cut.acquisition_id != current.acquisition_id:
            raise ProviderAccountAcquisitionError(
                "account cut acquisition_id is not current durable acquisition"
            )
        if (
            canonical_cut.acquisition_generation
            != current.acquisition_generation
        ):
            raise ProviderAccountAcquisitionError(
                "account cut acquisition_generation is not current"
            )
        if (
            canonical_cut.acquisition_journal_sequence_cut
            != current.acquisition_journal_sequence_cut
        ):
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
        canonical = SerializedProviderAccountAcquisition(
            provider_scope=acquisition.provider_scope,
            account_id=acquisition.account_id,
            acquisition_request_id=acquisition.acquisition_request_id,
            acquisition_generation=acquisition.acquisition_generation,
            acquisition_journal_sequence_cut=
                acquisition.acquisition_journal_sequence_cut,
        )
        current = self.resolve_current(
            provider_scope=canonical.provider_scope,
            account_id=canonical.account_id,
        )
        if current != canonical:
            raise ProviderAccountAcquisitionError(
                "serialized provider account acquisition is superseded or forged"
            )
        return current
