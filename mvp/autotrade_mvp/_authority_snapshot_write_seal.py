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
        # Preserve the original writer's argument validation/error surface.  The
        # preflight uses class-owned JournalStore methods so a subclass cannot
        # manufacture the existence of canonical authority through overrides.
        if not isinstance(store, JournalStore):
            return original_persist(
                store,
                service,
                authority_id=authority_id,
                event_id=event_id,
                committed_at=committed_at,
            )

        existing = JournalStore.get_event(store, event_id)
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
