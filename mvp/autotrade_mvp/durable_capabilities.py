"""Durable capability history with fail-closed restart admission.

Capability derivation remains owned by capabilities.py.  This adapter persists
already-derived immutable snapshots in the canonical JournalStore so historical
causal queries survive restart.  A persisted VERIFIED snapshot is deliberately
*not* sufficient for new financial admission after process restart: the current
process must add a freshly derived snapshot before require_verified() succeeds.
"""
from __future__ import annotations

from datetime import datetime
from hashlib import sha256
from typing import Any
from .capabilities import (
    CapabilityError,
    CapabilityRegistry,
    CapabilitySnapshot,
    _DERIVED_SNAPSHOT_TOKEN,
)
from .persistence import JournalStore, canonical_json, payload_digest


_AGGREGATE_TYPE = "capability_history"
_EVENT_TYPE = "CapabilitySnapshotObserved.v1"


def _identity_id(snapshot: CapabilitySnapshot) -> str:
    material = canonical_json(list(snapshot.identity)).encode("utf-8")
    return "capability:" + sha256(material).hexdigest()


def _payload(snapshot: CapabilitySnapshot) -> dict[str, Any]:
    return {
        "schema_version": "1.0.0",
        "snapshot": snapshot.to_contract_dict(),
        "sources": sorted(snapshot.sources),
    }


def _rehydrate(payload: dict[str, Any]) -> CapabilitySnapshot:
    if not isinstance(payload, dict) or payload.get("schema_version") != "1.0.0":
        raise CapabilityError("unsupported durable capability payload")
    raw = payload.get("snapshot")
    sources = payload.get("sources")
    if not isinstance(raw, dict) or not isinstance(sources, list):
        raise CapabilityError("durable capability payload is malformed")
    required = {
        "snapshot_id",
        "provider_id",
        "account_id",
        "entity_id",
        "environment",
        "instrument_version",
        "observed_at",
        "expires_at",
        "supported_order_types",
        "time_in_force",
        "permission_scopes",
        "position_mode",
        "native_protection",
        "rate_limit_policy_id",
        "data_entitlements",
        "evidence",
        "status",
    }
    if set(raw) != required:
        raise CapabilityError("durable capability snapshot fields are malformed")
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
    if snapshot.to_contract_dict() != raw:
        raise CapabilityError("durable capability snapshot is not canonical")
    if sorted(snapshot.sources) != sources:
        raise CapabilityError("durable capability sources are not canonical")
    return snapshot


class DurableCapabilityRegistry:
    """Journal-backed history plus process-local refresh fencing."""

    def __init__(self, store: JournalStore) -> None:
        if not isinstance(store, JournalStore):
            raise TypeError("store must be JournalStore")
        self.store = store
        self._session_verified: dict[str, CapabilitySnapshot] = {}

    def _history(self) -> CapabilityRegistry:
        registry = CapabilityRegistry()
        events = self.store.load_events_by_aggregate_type(_AGGREGATE_TYPE)
        seen_versions: dict[str, int] = {}
        for event in events:
            if event["aggregate_type"] != _AGGREGATE_TYPE:
                raise CapabilityError("capability event uses wrong aggregate type")
            if event["event_type"] != _EVENT_TYPE:
                raise CapabilityError("unsupported durable capability event type")
            aggregate_id = event["aggregate_id"]
            expected = seen_versions.get(aggregate_id, 0) + 1
            if event["aggregate_version"] != expected:
                raise CapabilityError("durable capability aggregate version gap")
            seen_versions[aggregate_id] = expected
            if payload_digest(event["payload"]) != event["payload_hash"]:
                raise CapabilityError("durable capability payload integrity failure")
            snapshot = _rehydrate(event["payload"])
            if _identity_id(snapshot) != aggregate_id:
                raise CapabilityError("durable capability aggregate identity mismatch")
            registry.add(snapshot)
        return registry

    def add(self, snapshot: CapabilitySnapshot) -> bool:
        if not isinstance(snapshot, CapabilitySnapshot):
            raise TypeError("snapshot must be CapabilitySnapshot")
        # Rebuild durable truth first so stale writers cannot append after a
        # newer refresh for the same identity.
        registry = self._history()
        try:
            existing = registry.latest(
                provider_id=snapshot.provider_id,
                account_id=snapshot.account_id,
                entity_id=snapshot.entity_id,
                environment=snapshot.environment,
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
        version = self.store.next_aggregate_version(
            _AGGREGATE_TYPE,
            aggregate_id,
        )
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
            result = self.store.append_event(envelope)
        except ValueError as error:
            # A concurrent refresh won. Re-read and accept only exact replay.
            current = self._history()
            try:
                persisted = current.latest(
                    provider_id=snapshot.provider_id,
                    account_id=snapshot.account_id,
                    entity_id=snapshot.entity_id,
                    environment=snapshot.environment,
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
