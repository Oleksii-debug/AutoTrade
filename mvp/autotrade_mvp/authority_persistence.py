"""Durable journal adapter for the canonical AuthorityService."""

from __future__ import annotations

from mvp.autotrade_mvp.authority import AuthorityService
from mvp.autotrade_mvp.persistence import JournalStore, payload_digest


AUTHORITY_AGGREGATE_TYPE = "financial-authority"
AUTHORITY_EVENT_TYPE = "AuthorityStateSnapshotted.v1"


_STATE_COLLECTION_KEYS = {
    "policies": "policy_id",
    "revocations": "policy_id",
    "confirmations": "confirmation_id",
    "admissions": "admission_id",
}


def _indexed_collection(state: dict, name: str, identity_key: str) -> dict[str, dict]:
    values = state.get(name)
    if not isinstance(values, list):
        raise ValueError(f"authority state {name} must be a list")
    indexed: dict[str, dict] = {}
    for item in values:
        if not isinstance(item, dict):
            raise ValueError(f"authority state {name} entry must be an object")
        identity = item.get(identity_key)
        if not isinstance(identity, str) or not identity.strip():
            raise ValueError(f"authority state {name} entry has invalid {identity_key}")
        if identity in indexed:
            raise ValueError(f"authority state {name} contains duplicate {identity_key}")
        indexed[identity] = item
    return indexed


def _assert_monotonic_authority_state(previous: dict, candidate: dict) -> None:
    """Reject snapshots that forget or rewrite any durable authority fact."""
    if not isinstance(previous, dict) or not isinstance(candidate, dict):
        raise ValueError("authority state must be an object")
    if previous.get("schema_version") != candidate.get("schema_version"):
        raise ValueError("authority state schema cannot regress or change in snapshot lineage")

    previous_epoch = previous.get("epoch")
    candidate_epoch = candidate.get("epoch")
    if (
        isinstance(previous_epoch, bool)
        or isinstance(candidate_epoch, bool)
        or not isinstance(previous_epoch, int)
        or not isinstance(candidate_epoch, int)
    ):
        raise ValueError("authority state epoch must be an integer")
    if candidate_epoch < previous_epoch:
        raise ValueError("authority snapshot is stale: epoch regressed")

    for name, identity_key in _STATE_COLLECTION_KEYS.items():
        old = _indexed_collection(previous, name, identity_key)
        new = _indexed_collection(candidate, name, identity_key)
        missing = set(old) - set(new)
        if missing:
            raise ValueError(
                f"authority snapshot is stale: {name} facts were removed"
            )
        for identity, old_item in old.items():
            if new[identity] != old_item:
                raise ValueError(
                    f"authority snapshot rewrites durable {name} fact {identity}"
                )

    old_used = previous.get("used_confirmations")
    new_used = candidate.get("used_confirmations")
    if not isinstance(old_used, list) or not isinstance(new_used, list):
        raise ValueError("authority state used_confirmations must be a list")
    if any(not isinstance(value, str) or not value.strip() for value in old_used + new_used):
        raise ValueError("authority state used_confirmations entries must be non-empty strings")
    if len(set(old_used)) != len(old_used) or len(set(new_used)) != len(new_used):
        raise ValueError("authority state used_confirmations must be unique")
    if not set(old_used).issubset(new_used):
        raise ValueError("authority snapshot is stale: used confirmation was forgotten")


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
    state = service.export_state()
    existing_events = store.load_events(AUTHORITY_AGGREGATE_TYPE, authority_id)
    if existing_events:
        previous_payload = existing_events[-1].get("payload")
        previous_state = (
            previous_payload.get("state")
            if isinstance(previous_payload, dict)
            else None
        )
        if not isinstance(previous_state, dict):
            raise ValueError("latest authority journal payload state is invalid")
        _assert_monotonic_authority_state(previous_state, state)

    payload = {
        "authority_id": authority_id,
        "state": state,
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
        "aggregate_version": str(aggregate_version),
        "payload": payload,
        "payload_hash": payload_digest(payload),
        # Lost-reply retries for an existing immutable event must reproduce the
        # original envelope byte-for-byte; caller wall-clock drift is not a
        # new financial fact.
        "committed_at": (
            existing["committed_at"] if existing is not None else committed_at.strip()
        ),
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
