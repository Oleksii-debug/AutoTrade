"""Durable capability history with fail-closed restart admission.

Capability derivation remains owned by capabilities.py. This adapter persists
already-derived immutable snapshots in the canonical JournalStore so historical
causal queries survive restart. A persisted VERIFIED snapshot is deliberately
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
    _instant,
)
from .persistence import JournalStore, canonical_json, payload_digest


_AGGREGATE_TYPE = "capability_history"
_EVENT_TYPE_V1 = "CapabilitySnapshotObserved.v1"
_EVENT_TYPE = "CapabilitySnapshotObserved.v2"


def _identity_id(snapshot: CapabilitySnapshot) -> str:
    material = canonical_json(list(snapshot.identity)).encode("utf-8")
    return "capability:" + sha256(material).hexdigest()


def _legacy_identity_id(snapshot: CapabilitySnapshot) -> str:
    material = canonical_json([
        snapshot.provider_id,
        snapshot.account_id,
        snapshot.entity_id,
        snapshot.environment,
        snapshot.instrument_version,
    ]).encode("utf-8")
    return "capability:" + sha256(material).hexdigest()


def _payload(snapshot: CapabilitySnapshot) -> dict[str, Any]:
    return {
        "schema_version": "2.0.0",
        "snapshot": snapshot.to_contract_dict(),
        "sources": sorted(snapshot.sources),
    }


def _rehydrate(payload: dict[str, Any]) -> CapabilitySnapshot:
    if not isinstance(payload, dict):
        raise CapabilityError("unsupported durable capability payload")
    schema_version = payload.get("schema_version")
    if schema_version not in {"1.0.0", "2.0.0"}:
        raise CapabilityError("unsupported durable capability payload")
    raw = payload.get("snapshot")
    sources = payload.get("sources")
    if not isinstance(raw, dict) or not isinstance(sources, list):
        raise CapabilityError("durable capability payload is malformed")
    v1_required = {
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
    required = (
        v1_required
        if schema_version == "1.0.0"
        else v1_required | {"provider_environment"}
    )
    if set(raw) != required:
        raise CapabilityError("durable capability snapshot fields are malformed")
    if (
        schema_version == "1.0.0"
        and str(raw.get("provider_id", "")).strip().upper() == "BYBIT"
    ):
        raise CapabilityError("legacy BYBIT capability lacks exact provider_environment")
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


class DurableCapabilityRegistry:
    """Journal-backed history plus process-local refresh fencing."""

    def __init__(self, store: JournalStore) -> None:
        if type(store) is not JournalStore:
            raise TypeError("store must be exact JournalStore")
        self.store = store
        self._session_verified: dict[str, CapabilitySnapshot] = {}

    def _resolved_cut(self, requested: int | None) -> int:
        if requested is not None and (
            type(requested) is not int or requested < 0
        ):
            raise ValueError(
                "journal_sequence_cut must be a non-negative exact integer or None"
            )
        cut = self.store.whole_store_state_cut()
        if type(cut) is not dict:
            raise CapabilityError("whole-store journal cut is non-canonical")
        observed = cut.get("journal_sequence")
        if type(observed) is not int or observed < 0:
            raise CapabilityError("whole-store journal cut lacks canonical sequence")
        if requested is None:
            return observed
        if requested > observed:
            raise CapabilityError("requested capability journal cut is in the future")
        return requested

    def _history_with_versions(
        self,
        *,
        journal_sequence_cut: int | None = None,
    ) -> tuple[CapabilityRegistry, dict[str, int]]:
        resolved_cut = self._resolved_cut(journal_sequence_cut)
        registry = CapabilityRegistry()
        events = self.store.load_events_by_aggregate_type(_AGGREGATE_TYPE)
        seen_versions: dict[str, int] = {}
        previous_sequence = 0
        for event in events:
            if type(event) is not dict:
                raise CapabilityError("capability journal event is not canonical")
            sequence = event.get("journal_sequence")
            if type(sequence) is not int or sequence < 1:
                raise CapabilityError("capability event lacks global journal sequence")
            if sequence > resolved_cut:
                continue
            if sequence <= previous_sequence:
                raise CapabilityError(
                    "capability journal sequence is not strictly increasing"
                )
            previous_sequence = sequence
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
            snapshot = _rehydrate(event["payload"])
            expected_identity = (
                _legacy_identity_id(snapshot)
                if event["payload"].get("schema_version") == "1.0.0"
                else _identity_id(snapshot)
            )
            if expected_identity != aggregate_id:
                raise CapabilityError("durable capability aggregate identity mismatch")
            registry.add(snapshot)
        return registry, seen_versions

    def _history(
        self,
        *,
        journal_sequence_cut: int | None = None,
    ) -> CapabilityRegistry:
        registry, _versions = self._history_with_versions(
            journal_sequence_cut=journal_sequence_cut
        )
        return registry

    def add(self, snapshot: CapabilitySnapshot) -> bool:
        if type(snapshot) is not CapabilitySnapshot:
            raise TypeError("snapshot must be exact CapabilitySnapshot")
        registry, seen_versions = self._history_with_versions()
        aggregate_id = _identity_id(snapshot)
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
            if (
                snapshot.status == "VERIFIED"
                and getattr(snapshot, "_can_admit", False)
            ):
                self._session_verified[snapshot.snapshot_id] = snapshot
            return False

        registry.add(snapshot)
        version = seen_versions.get(aggregate_id, 0) + 1
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
                if (
                    snapshot.status == "VERIFIED"
                    and getattr(snapshot, "_can_admit", False)
                ):
                    self._session_verified[snapshot.snapshot_id] = snapshot
                return False
            raise CapabilityError(
                "capability history changed concurrently; refresh required"
            ) from error

        if (
            result.inserted
            and snapshot.status == "VERIFIED"
            and getattr(snapshot, "_can_admit", False)
        ):
            self._session_verified[snapshot.snapshot_id] = snapshot
        return result.inserted

    def latest(self, **kwargs) -> CapabilitySnapshot:
        journal_sequence_cut = kwargs.pop("journal_sequence_cut", None)
        return self._history(
            journal_sequence_cut=journal_sequence_cut
        ).latest(**kwargs)

    def require_verified(self, **kwargs) -> CapabilitySnapshot:
        journal_sequence_cut = kwargs.pop("journal_sequence_cut", None)
        snapshot = self._history(
            journal_sequence_cut=journal_sequence_cut
        ).latest(**kwargs)
        point = _instant(kwargs["at"], "at")
        if type(snapshot) is not CapabilitySnapshot:
            raise CapabilityError("capability snapshot is not canonical")
        if snapshot.status != "VERIFIED":
            raise CapabilityError(f"capability status is {snapshot.status}")
        if point >= snapshot.expires_at:
            raise CapabilityError("capability snapshot is expired")
        fresh = self._session_verified.get(snapshot.snapshot_id)
        if (
            fresh is None
            or fresh != snapshot
            or type(fresh) is not CapabilitySnapshot
            or not getattr(fresh, "_can_admit", False)
        ):
            raise CapabilityError(
                "capability requires fresh current-process verification after restart"
            )
        return fresh
