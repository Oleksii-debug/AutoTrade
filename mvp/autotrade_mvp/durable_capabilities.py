"""Durable capability history with fail-closed restart admission.

Capability derivation remains owned by capabilities.py.  This adapter persists
already-derived immutable snapshots in the canonical JournalStore so historical
causal queries survive restart.  A persisted VERIFIED snapshot is deliberately
*not* sufficient for new financial admission after process restart: the current
process must add a freshly derived snapshot before require_verified() succeeds.
"""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from typing import Any, Mapping
from uuid import UUID
from .capabilities import (
    CapabilityError,
    CapabilityRegistry,
    CapabilitySnapshot,
    SOURCES,
    _DERIVED_SNAPSHOT_TOKEN,
    _freeze_evidence,
)
from .persistence import (
    JournalStore,
    canonical_json,
    journal_store_authority_scope,
    payload_digest,
    require_exact_journal_store_authority,
)


_AGGREGATE_TYPE = "capability_history"
_EVENT_TYPE_V1 = "CapabilitySnapshotObserved.v1"
_EVENT_TYPE = "CapabilitySnapshotObserved.v2"
_RETIREMENT_ID = "capability-history-v2"


def _identity_id(snapshot: CapabilitySnapshot) -> str:
    material = canonical_json(list(snapshot.identity)).encode("utf-8")
    return "capability:" + sha256(material).hexdigest()


def _legacy_identity_id_from_raw(raw: Mapping[str, Any]) -> str:
    material = canonical_json(
        [
            raw["provider_id"],
            raw["account_id"],
            raw["entity_id"],
            raw["environment"],
            raw["instrument_version"],
        ]
    ).encode("utf-8")
    return "capability:" + sha256(material).hexdigest()


def _payload(snapshot: CapabilitySnapshot) -> dict[str, Any]:
    return {
        "schema_version": "2.0.0",
        "snapshot": snapshot.to_contract_dict(),
        "sources": sorted(snapshot.sources),
    }


def _legacy_text(value: object, *, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise CapabilityError(f"legacy capability {name} is not canonical")
    return value


def _legacy_time(value: object, *, name: str) -> datetime:
    text = _legacy_text(value, name=name)
    if not text.endswith("Z"):
        raise CapabilityError(f"legacy capability {name} must be canonical UTC text")
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00").astimezone(timezone.utc)
    except ValueError as error:
        raise CapabilityError(f"legacy capability {name} is malformed") from error
    if parsed.isoformat().replace("+00:00", "Z") != text:
        raise CapabilityError(f"legacy capability {name} is not canonical UTC text")
    return parsed


_V1_SNAPSHOT_FIELDS = {
    "snapshot_id", "provider_id", "account_id", "entity_id", "environment",
    "instrument_version", "observed_at", "expires_at",
    "supported_order_types", "time_in_force", "permission_scopes",
    "position_mode", "native_protection", "rate_limit_policy_id",
    "data_entitlements", "evidence", "status",
}


def _validate_ambiguous_legacy_bybit(
    raw: Mapping[str, Any],
    sources: object,
) -> datetime:
    if set(raw) != _V1_SNAPSHOT_FIELDS:
        raise CapabilityError("durable capability snapshot fields are malformed")
    try:
        canonical_snapshot_id = str(UUID(raw["snapshot_id"]))
    except (ValueError, TypeError, AttributeError) as error:
        raise CapabilityError("legacy capability snapshot_id is malformed") from error
    if canonical_snapshot_id != raw["snapshot_id"]:
        raise CapabilityError("legacy capability snapshot_id is not canonical")
    for name in (
        "provider_id", "account_id", "entity_id", "environment",
        "instrument_version", "position_mode", "rate_limit_policy_id", "status",
    ):
        _legacy_text(raw[name], name=name)
    if raw["provider_id"].upper() != "BYBIT":
        raise CapabilityError("legacy ambiguous capability must be BYBIT")
    if raw["environment"] not in {"REPLAY", "SIMULATION", "PAPER", "LIVE"}:
        raise CapabilityError("legacy capability environment is unsupported")
    if raw["status"] not in {"VERIFIED", "UNKNOWN", "CONFLICTED", "EXPIRED"}:
        raise CapabilityError("legacy capability status is unsupported")
    observed = _legacy_time(raw["observed_at"], name="observed_at")
    expires = _legacy_time(raw["expires_at"], name="expires_at")
    if expires < observed:
        raise CapabilityError("legacy capability expires_at precedes observed_at")
    for name in (
        "supported_order_types", "time_in_force", "permission_scopes",
        "native_protection", "data_entitlements",
    ):
        values = raw[name]
        if (
            type(values) is not list
            or any(
                type(item) is not str or not item or item != item.strip()
                for item in values
            )
            or values != sorted(set(values))
        ):
            raise CapabilityError(f"legacy capability {name} is not canonical")
    if (
        type(sources) is not list
        or any(
            type(item) is not str or not item or item != item.strip()
            for item in sources
        )
        or sources != sorted(set(sources))
        or not set(sources).issubset(SOURCES)
    ):
        raise CapabilityError("durable capability sources are not canonical")
    evidence = raw["evidence"]
    if type(evidence) is not list:
        raise CapabilityError("legacy capability evidence is not an array")
    if [dict(_freeze_evidence(item)) for item in evidence] != evidence:
        raise CapabilityError("legacy capability evidence is not canonical")
    if raw["status"] == "VERIFIED":
        if set(sources) != SOURCES or len(evidence) < len(SOURCES):
            raise CapabilityError("legacy VERIFIED capability lacks canonical evidence")
        if (
            not raw["supported_order_types"]
            or not raw["time_in_force"]
            or not raw["permission_scopes"]
        ):
            raise CapabilityError("legacy VERIFIED capability is not executable")
    return observed


def _rehydrate(payload: dict[str, Any]) -> CapabilitySnapshot | None:
    if not isinstance(payload, dict):
        raise CapabilityError("unsupported durable capability payload")
    schema_version = payload.get("schema_version")
    if schema_version not in {"1.0.0", "2.0.0"}:
        raise CapabilityError("unsupported durable capability payload")
    raw = payload.get("snapshot")
    sources = payload.get("sources")
    if not isinstance(raw, dict) or not isinstance(sources, list):
        raise CapabilityError("durable capability payload is malformed")
    required = (
        _V1_SNAPSHOT_FIELDS
        if schema_version == "1.0.0"
        else _V1_SNAPSHOT_FIELDS | {"provider_environment"}
    )
    if set(raw) != required:
        raise CapabilityError("durable capability snapshot fields are malformed")
    if (
        schema_version == "1.0.0"
        and type(raw.get("provider_id")) is str
        and raw["provider_id"].upper() == "BYBIT"
    ):
        _validate_ambiguous_legacy_bybit(raw, sources)
        return None
    try:
        observed_at = datetime.fromisoformat(
            str(raw["observed_at"]).replace("Z", "+00:00")
        )
        expires_at = datetime.fromisoformat(
            str(raw["expires_at"]).replace("Z", "+00:00")
        )
    except ValueError as error:
        raise CapabilityError("durable capability timestamps are malformed") from error
    snapshot = CapabilitySnapshot(
        snapshot_id=raw["snapshot_id"],
        provider_id=raw["provider_id"],
        account_id=raw["account_id"],
        entity_id=raw["entity_id"],
        environment=raw["environment"],
        provider_environment=raw.get("provider_environment"),
        instrument_version=raw["instrument_version"],
        observed_at=observed_at,
        expires_at=expires_at,
        supported_order_types=frozenset(raw["supported_order_types"]),
        time_in_force=frozenset(raw["time_in_force"]),
        permission_scopes=frozenset(raw["permission_scopes"]),
        position_mode=raw["position_mode"],
        native_protection=frozenset(raw["native_protection"]),
        rate_limit_policy_id=raw["rate_limit_policy_id"],
        data_entitlements=frozenset(raw["data_entitlements"]),
        evidence=tuple(raw["evidence"]),
        status=raw["status"],
        sources=frozenset(sources),
        _verification_token=_DERIVED_SNAPSHOT_TOKEN,
    )
    projected = snapshot.to_contract_dict()
    if schema_version == "1.0.0":
        projected = dict(projected)
        projected.pop("provider_environment")
    if projected != raw:
        raise CapabilityError("durable capability snapshot is not canonical")
    if sorted(snapshot.sources) != sources:
        raise CapabilityError("durable capability sources are not canonical")
    return snapshot


def _ambiguous_legacy_bybit_identity(
    payload: Mapping[str, Any],
) -> tuple[tuple[str, str, str, str, str], datetime] | None:
    if payload.get("schema_version") != "1.0.0":
        return None
    raw = payload.get("snapshot")
    sources = payload.get("sources")
    if (
        not isinstance(raw, dict)
        or type(raw.get("provider_id")) is not str
        or raw["provider_id"].upper() != "BYBIT"
    ):
        return None
    observed = _validate_ambiguous_legacy_bybit(raw, sources)
    return (
        (
            raw["provider_id"], raw["account_id"], raw["entity_id"],
            raw["environment"], raw["instrument_version"],
        ),
        observed,
    )


class DurableCapabilityRegistry:
    """Journal-backed history plus process-local refresh fencing."""

    def __init__(self, store: JournalStore) -> None:
        store_identity = require_exact_journal_store_authority(
            store,
            subject="durable-capability JournalStore",
        )
        self.store = store
        self._store_identity = store_identity
        # Constructing the v2 registry is the product-generation cutover. Fence
        # old v1 writers before exposing any v2 read/write authority.
        self._journal_operation(
            JournalStore.retire_event_type,
            _EVENT_TYPE_V1,
            retirement_id=_RETIREMENT_ID,
        )
        self._session_verified: dict[str, CapabilitySnapshot] = {}

    def _journal_operation(self, operation, /, *args, **kwargs):
        store = self.store
        expected = self._store_identity
        current = require_exact_journal_store_authority(
            store,
            subject="durable-capability JournalStore",
        )
        if current != expected:
            raise CapabilityError("durable-capability JournalStore changed")
        with journal_store_authority_scope(store, expected):
            return operation(store, *args, **kwargs)

    def _validated_history_cut(
        self,
    ) -> tuple[
        CapabilityRegistry,
        dict[str, int],
        dict[tuple[str, str, str, str, str], datetime],
    ]:
        """Replay one durable semantic cut and retain per-aggregate versions.

        The returned version map is part of the semantic decision cut. Writers
        must publish against it rather than perform a later fresh version read,
        otherwise a concurrent same-identity refresh can make a stale semantic
        decision look like a valid newer event.
        """

        registry = CapabilityRegistry()
        events = self._journal_operation(
            JournalStore.load_events_by_aggregate_type,
            _AGGREGATE_TYPE,
        )
        seen_versions: dict[str, int] = {}
        ambiguous_bybit_latest: dict[
            tuple[str, str, str, str, str], datetime
        ] = {}
        for event in events:
            if event["aggregate_type"] != _AGGREGATE_TYPE:
                raise CapabilityError("capability event uses wrong aggregate type")
            if event["event_type"] not in {_EVENT_TYPE_V1, _EVENT_TYPE}:
                raise CapabilityError("unsupported durable capability event type")
            aggregate_id = event["aggregate_id"]
            expected = seen_versions.get(aggregate_id, 0) + 1
            if event["aggregate_version"] != expected:
                raise CapabilityError("durable capability aggregate version gap")
            seen_versions[aggregate_id] = expected
            if payload_digest(event["payload"]) != event["payload_hash"]:
                raise CapabilityError("durable capability payload integrity failure")
            payload = event["payload"]
            ambiguous = _ambiguous_legacy_bybit_identity(payload)
            snapshot = _rehydrate(payload)
            if ambiguous is not None:
                raw = payload["snapshot"]
                if _legacy_identity_id_from_raw(raw) != aggregate_id:
                    raise CapabilityError(
                        "durable legacy capability aggregate identity mismatch"
                    )
                legacy_identity, observed = ambiguous
                previous = ambiguous_bybit_latest.get(legacy_identity)
                if previous is None or observed > previous:
                    ambiguous_bybit_latest[legacy_identity] = observed
                continue
            if snapshot is None:
                raise CapabilityError(
                    "durable capability replay produced no canonical snapshot"
                )
            expected_identity = (
                _legacy_identity_id_from_raw(payload["snapshot"])
                if payload.get("schema_version") == "1.0.0"
                else _identity_id(snapshot)
            )
            if expected_identity != aggregate_id:
                raise CapabilityError("durable capability aggregate identity mismatch")
            registry.add(snapshot)
        return registry, seen_versions, ambiguous_bybit_latest

    def _history(self) -> CapabilityRegistry:
        registry, _versions, _ambiguous = self._validated_history_cut()
        return registry

    def add(self, snapshot: CapabilitySnapshot) -> bool:
        if type(snapshot) is not CapabilitySnapshot:
            raise TypeError("snapshot must be exact CapabilitySnapshot")
        # Registry construction already committed the v1 writer fence. Replay
        # the durable post-cutover history used for this semantic decision.
        registry, versions_at_cut, ambiguous_bybit_latest = (
            self._validated_history_cut()
        )
        if snapshot.provider_id == "BYBIT":
            legacy_key = (
                snapshot.provider_id,
                snapshot.account_id,
                snapshot.entity_id,
                snapshot.environment,
                snapshot.instrument_version,
            )
            legacy_observed = ambiguous_bybit_latest.get(legacy_key)
            if legacy_observed is not None and snapshot.observed_at <= legacy_observed:
                raise CapabilityError(
                    "BYBIT v2 requalification must advance beyond ambiguous legacy history"
                )
        try:
            existing = registry.latest(
                provider_id=snapshot.provider_id,
                account_id=snapshot.account_id,
                entity_id=snapshot.entity_id,
                environment=snapshot.environment,
                provider_environment=snapshot.provider_environment,
                instrument_version=snapshot.instrument_version,
                at=snapshot.observed_at,
            )
        except CapabilityError:
            existing = None
        if existing is not None and existing.observed_at == snapshot.observed_at:
            if existing != snapshot:
                raise CapabilityError(
                    "snapshot observed_at conflicts with durable capability history"
                )
            if snapshot.status == "VERIFIED":
                # Exact replay is not a fresh current-process derivation.
                return False
            return False

        # Canonical in-memory registry owns ordering/content semantics.
        registry.add(snapshot)
        aggregate_id = _identity_id(snapshot)
        version = versions_at_cut.get(aggregate_id, 0) + 1
        payload = _payload(snapshot)
        envelope = {
            "event_id": f"capability-snapshot:{snapshot.snapshot_id}",
            "event_type": _EVENT_TYPE,
            "aggregate_type": _AGGREGATE_TYPE,
            "aggregate_id": aggregate_id,
            "aggregate_version": str(version),
            "payload": payload,
            "payload_hash": payload_digest(payload),
            "committed_at": snapshot.observed_at.isoformat(),
        }
        try:
            result = self._journal_operation(JournalStore.append_event, envelope)
        except ValueError as error:
            # A concurrent refresh won. Re-read and accept only exact replay.
            current = self._history()
            try:
                persisted = current.latest(
                    provider_id=snapshot.provider_id,
                    account_id=snapshot.account_id,
                    entity_id=snapshot.entity_id,
                    environment=snapshot.environment,
                    provider_environment=snapshot.provider_environment,
                    instrument_version=snapshot.instrument_version,
                    at=snapshot.observed_at,
                )
            except CapabilityError:
                raise CapabilityError(
                    "capability history changed concurrently; refresh required"
                ) from error
            if persisted == snapshot:
                return False
            raise CapabilityError(
                "capability history changed concurrently; refresh required"
            ) from error

        if result.inserted and snapshot.status == "VERIFIED":
            self._session_verified[snapshot.snapshot_id] = snapshot
        return result.inserted

    def latest(self, **kwargs) -> CapabilitySnapshot:
        return self._history().latest(**kwargs)

    def require_verified(self, **kwargs) -> CapabilitySnapshot:
        snapshot = self._history().require_verified(**kwargs)
        fresh = self._session_verified.get(snapshot.snapshot_id)
        if fresh is None or fresh != snapshot:
            raise CapabilityError(
                "capability requires fresh current-process verification after restart"
            )
        return fresh
