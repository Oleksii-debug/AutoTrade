"""Fail-closed legacy boundary for authority snapshot publication.

Historical ``financial-authority`` snapshots remain readable and exact immutable
lost-reply retries remain replayable.  A new snapshot event, however, is only a
cache/checkpoint of already-existing canonical ``authority_state/canonical``
authority; snapshot-only state is never a second writable authority system.
"""
from __future__ import annotations

from . import authority_persistence as _persistence
from .persistence import JournalStore


def _install_authority_snapshot_write_seal() -> None:
    original_persist = _persistence.persist_authority_snapshot

    def sealed_persist_authority_snapshot(
        store: JournalStore,
        service,
        *,
        authority_id: str,
        event_id: str,
        committed_at: str,
    ):
        # Preserve the canonical writer's existing argument/composition error
        # surface.  Only an otherwise valid fresh publication reaches the new
        # legacy-read-only gate below.
        if (
            not isinstance(store, JournalStore)
            or not isinstance(service, _persistence.AuthorityService)
            or (service.store is not None and service.store is not store)
            or not isinstance(authority_id, str)
            or not authority_id.strip()
            or not isinstance(event_id, str)
            or not event_id.strip()
            or not isinstance(committed_at, str)
            or not committed_at.strip()
        ):
            return original_persist(
                store,
                service,
                authority_id=authority_id,
                event_id=event_id,
                committed_at=committed_at,
            )

        # Class-owned reads prevent a JournalStore subclass from manufacturing
        # canonical-history existence through method overrides at this fence.
        existing = JournalStore.get_event(store, event_id.strip())
        if existing is None:
            canonical = JournalStore.load_events(
                store,
                _persistence._CANONICAL_AUTHORITY_TYPE,
                _persistence._CANONICAL_AUTHORITY_ID,
            )
            if not canonical:
                raise ValueError(
                    "new authority snapshot publication requires canonical authority"
                )

        return original_persist(
            store,
            service,
            authority_id=authority_id,
            event_id=event_id,
            committed_at=committed_at,
        )

    _persistence.persist_authority_snapshot = sealed_persist_authority_snapshot


_install_authority_snapshot_write_seal()
del _install_authority_snapshot_write_seal
