"""Durable journal adapter for the canonical AuthorityService."""

from __future__ import annotations

from mvp.autotrade_mvp.authority import AuthorityService
from mvp.autotrade_mvp.persistence import JournalStore, payload_digest


AUTHORITY_AGGREGATE_TYPE = "financial-authority"
AUTHORITY_EVENT_TYPE = "AuthorityStateSnapshotted.v1"


def persist_authority_snapshot(
    store: JournalStore,
    service: AuthorityService,
    *,
    authority_id: str,
    event_id: str,
    committed_at: str,
):
    """Persist the complete authority state without creating another authority."""
    if not isinstance(store, JournalStore):
        raise TypeError("store must be JournalStore")
    if not isinstance(service, AuthorityService):
        raise TypeError("service must be AuthorityService")
    if not isinstance(authority_id, str) or not authority_id.strip():
        raise ValueError("authority_id is required")
    if not isinstance(event_id, str) or not event_id.strip():
        raise ValueError("event_id is required")
    if not isinstance(committed_at, str) or not committed_at.strip():
        raise ValueError("committed_at is required")

    authority_id = authority_id.strip()
    event_id = event_id.strip()
    payload = {
        "authority_id": authority_id,
        "state": service.export_state(),
    }
    existing = store.get_event(event_id)
    aggregate_version = (
        existing["aggregate_version"]
        if existing is not None
        else store.next_aggregate_version(AUTHORITY_AGGREGATE_TYPE, authority_id)
    )
    envelope = {
        "event_id": event_id,
        "event_type": AUTHORITY_EVENT_TYPE,
        "aggregate_type": AUTHORITY_AGGREGATE_TYPE,
        "aggregate_id": authority_id,
        "aggregate_version": aggregate_version,
        "payload": payload,
        "payload_hash": payload_digest(payload),
        "committed_at": committed_at.strip(),
    }
    return store.append_event(envelope)


def restore_authority_snapshot(
    store: JournalStore,
    *,
    authority_id: str,
) -> AuthorityService:
    """Restore latest durable authority state; missing/corrupt state fails closed."""
    if not isinstance(store, JournalStore):
        raise TypeError("store must be JournalStore")
    if not isinstance(authority_id, str) or not authority_id.strip():
        raise ValueError("authority_id is required")
    authority_id = authority_id.strip()
    events = store.load_events(AUTHORITY_AGGREGATE_TYPE, authority_id)
    if not events:
        raise ValueError("authority durable state is missing")

    expected_version = 1
    latest = None
    for event in events:
        if event["aggregate_version"] != expected_version:
            raise ValueError("authority journal version gap")
        expected_version += 1
        if event["event_type"] != AUTHORITY_EVENT_TYPE:
            raise ValueError("unexpected authority journal event")
        if payload_digest(event["payload"]) != event["payload_hash"]:
            raise ValueError("authority journal payload integrity failure")
        payload = event["payload"]
        if (
            not isinstance(payload, dict)
            or payload.get("authority_id") != authority_id
            or not isinstance(payload.get("state"), dict)
        ):
            raise ValueError("authority journal payload scope is invalid")
        latest = payload["state"]

    if latest is None:
        raise ValueError("authority durable state is missing")
    return AuthorityService.restore(latest)
