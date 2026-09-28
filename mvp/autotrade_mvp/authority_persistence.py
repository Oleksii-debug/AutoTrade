"""Durable journal adapter for the canonical AuthorityService."""

from __future__ import annotations

from research.autotrade_research.artifacts.store import ArtifactStore

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
_NEW_EXPOSURE_BLOCK_FIELDS = {
    "account_id",
    "environment",
    "command_id",
    "reason",
    "blocked_at",
}
_SNAPSHOT_COMMAND_ACTOR = "autotrade-authority-snapshot"
_CANONICAL_AUTHORITY_TYPE = "authority_state"
_CANONICAL_AUTHORITY_ID = "canonical"


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


def _indexed_new_exposure_blocks(
    state: dict,
) -> dict[tuple[str, str], dict[str, str]]:
    # AuthorityService.restore() deliberately treats the field as optional for
    # historical schema-v1 snapshots.  Keep that compatibility while requiring
    # every present entry to have the exact canonical exported shape.
    values = state.get("new_exposure_blocks", [])
    if not isinstance(values, list):
        raise ValueError("authority state new_exposure_blocks must be a list")
    indexed: dict[tuple[str, str], dict[str, str]] = {}
    for item in values:
        if not isinstance(item, dict) or set(item) != _NEW_EXPOSURE_BLOCK_FIELDS:
            raise ValueError(
                "authority state new_exposure_blocks entry has invalid structure"
            )
        normalized: dict[str, str] = {}
        for field in _NEW_EXPOSURE_BLOCK_FIELDS:
            value = item.get(field)
            if not isinstance(value, str) or not value.strip() or value != value.strip():
                raise ValueError(
                    f"authority state new_exposure_blocks entry has invalid {field}"
                )
            normalized[field] = value
        scope = (normalized["account_id"], normalized["environment"])
        if scope in indexed:
            raise ValueError(
                "authority state new_exposure_blocks contains duplicate scope"
            )
        indexed[scope] = normalized
    return indexed


def _canonical_authority_state(
    store: JournalStore,
    *,
    evidence_artifact_store: ArtifactStore | None = None,
) -> dict | None:
    """Project the full canonical authority aggregate using canonical replay.

    ``None`` is an explicit legacy boundary: historical snapshot-only installs
    may have no ``authority_state/canonical`` aggregate at all. Once at least one
    canonical transition exists, snapshots must equal the exact state rebuilt by
    ``AuthorityService`` itself.

    Financial admissions are not given a weaker replay mode here. CASH evidence
    is revalidated entirely from the JournalStore. BORROW evidence additionally
    requires the same trusted ``ArtifactStore`` used by canonical financial
    authority; absence of that cryptographic evidence dependency fails closed.
    """

    events = store.load_events(_CANONICAL_AUTHORITY_TYPE, _CANONICAL_AUTHORITY_ID)
    if not events:
        return None

    try:
        projection = AuthorityService(
            store,
            evidence_artifact_store=evidence_artifact_store,
        )
    except Exception as error:
        raise ValueError(
            "canonical authority journal cannot be projected for snapshot proof"
        ) from error
    return projection.export_state()


def _assert_new_exposure_blocks_match(
    candidate_state: dict,
    canonical_state: dict,
) -> None:
    candidate = _indexed_new_exposure_blocks(candidate_state)
    canonical = _indexed_new_exposure_blocks(canonical_state)
    if candidate == canonical:
        return

    missing = sorted(set(canonical) - set(candidate))
    extra = sorted(set(candidate) - set(canonical))
    if missing:
        raise ValueError(
            "authority snapshot is stale: canonical new-exposure block is missing"
        )
    if extra:
        raise ValueError(
            "authority snapshot contains new-exposure block without canonical authority"
        )
    raise ValueError(
        "authority snapshot rewrites canonical new-exposure block identity"
    )


def _assert_monotonic_authority_state(previous: dict, candidate: dict) -> None:
    """Reject snapshots that forget or rewrite any durable snapshot fact."""
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

    # Active block transitions are not append-only; their exact equality to
    # canonical block/restore replay is checked separately.
    _indexed_new_exposure_blocks(previous)
    _indexed_new_exposure_blocks(candidate)

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


def _assert_authority_state_matches_canonical(
    state: dict,
    *,
    store: JournalStore,
    evidence_artifact_store: ArtifactStore | None = None,
) -> None:
    """Bind a snapshot to the canonical authority aggregate at the read cut.

    Snapshot-only historical installs remain readable while the canonical
    aggregate is genuinely absent. In that legacy mode active new-exposure
    blocks are still forbidden because block/restore has always been canonical
    journal authority. As soon as any canonical authority event exists, the
    complete snapshot must equal the full canonical projection; comparing only
    with the prior snapshot is insufficient when a canonical mutation occurred
    without an intermediate snapshot.
    """

    try:
        candidate = AuthorityService.restore(state).export_state()
    except Exception as error:
        raise ValueError("authority snapshot state is invalid") from error

    canonical = _canonical_authority_state(
        store,
        evidence_artifact_store=evidence_artifact_store,
    )
    if canonical is None:
        if _indexed_new_exposure_blocks(candidate):
            raise ValueError(
                "authority snapshot contains new-exposure block without canonical authority"
            )
        return

    # Preserve precise safety diagnostics for the emergency block surface.
    _assert_new_exposure_blocks_match(candidate, canonical)

    # Treat canonical state as the minimum immutable lineage: this yields the
    # existing stale/removal/rewrite diagnostics for every durable collection.
    _assert_monotonic_authority_state(canonical, candidate)
    if candidate != canonical:
        raise ValueError(
            "authority snapshot contains facts without canonical authority"
        )


def _snapshot_command_identity(authority_id: str, event_id: str) -> str:
    digest = payload_digest(
        {"authority_id": authority_id, "event_id": event_id}
    ).split(":", 1)[1]
    return f"authority-snapshot-{digest}"


def persist_authority_snapshot(
    store: JournalStore,
    service: AuthorityService,
    *,
    authority_id: str,
    event_id: str,
    committed_at: str,
):
    """Persist complete authority state without creating another authority.

    New snapshots capture one global journal cut, prove their complete state
    against the canonical authority aggregate observed at that cut, and commit
    with the same cut as a compare-and-append fence. A canonical or unrelated
    journal event between proof and append invalidates the transaction.

    Historical immutable event-id retries keep their original envelope
    semantics and are never reinterpreted against later canonical state.
    """
    if not isinstance(store, JournalStore):
        raise TypeError("store must be JournalStore")
    if not isinstance(service, AuthorityService):
        raise TypeError("service must be AuthorityService")
    if service.store is not None and service.store is not store:
        raise ValueError("authority service and snapshot store must share one JournalStore")
    if not isinstance(authority_id, str) or not authority_id.strip():
        raise ValueError("authority_id is required")
    if not isinstance(event_id, str) or not event_id.strip():
        raise ValueError("event_id is required")
    if not isinstance(committed_at, str) or not committed_at.strip():
        raise ValueError("committed_at is required")

    authority_id = authority_id.strip()
    event_id = event_id.strip()
    state = service.export_state()
    existing = store.get_event(event_id)
    checked_previous_version: int | None = None
    journal_cut: int | None = None

    # An immutable event-id replay is a lost-reply retry, not a new authority
    # publication. Let JournalStore compare the reconstructed envelope exactly;
    # only a genuinely new event must extend current canonical authority.
    if existing is None:
        journal_cut = store.current_journal_sequence()
        existing_events = store.load_events(AUTHORITY_AGGREGATE_TYPE, authority_id)
        if existing_events:
            previous_event = existing_events[-1]
            checked_previous_version = previous_event.get("aggregate_version")
            if (
                isinstance(checked_previous_version, bool)
                or not isinstance(checked_previous_version, int)
                or checked_previous_version < 1
            ):
                raise ValueError("latest authority aggregate version is invalid")
            previous_payload = previous_event.get("payload")
            previous_state = (
                previous_payload.get("state")
                if isinstance(previous_payload, dict)
                else None
            )
            if not isinstance(previous_state, dict):
                raise ValueError("latest authority journal payload state is invalid")
            _assert_monotonic_authority_state(previous_state, state)

        _assert_authority_state_matches_canonical(
            state,
            store=store,
            evidence_artifact_store=service.evidence_artifact_store,
        )

    payload = {
        "authority_id": authority_id,
        "state": state,
    }
    aggregate_version = (
        existing["aggregate_version"]
        if existing is not None
        else (1 if checked_previous_version is None else checked_previous_version + 1)
    )
    envelope = {
        "event_id": event_id,
        "event_type": AUTHORITY_EVENT_TYPE,
        "aggregate_type": AUTHORITY_AGGREGATE_TYPE,
        "aggregate_id": authority_id,
        "aggregate_version": str(aggregate_version),
        "payload": payload,
        "payload_hash": payload_digest(payload),
        "committed_at": (
            existing["committed_at"] if existing is not None else committed_at.strip()
        ),
    }

    if existing is not None:
        return store.append_event(envelope)

    assert journal_cut is not None
    command_identity = _snapshot_command_identity(authority_id, event_id)
    try:
        _result, inserted, appended = store.commit_command(
            command_id=command_identity,
            actor=_SNAPSHOT_COMMAND_ACTOR,
            environment="REPLAY",
            idempotency_key=command_identity,
            request={"snapshot_event": envelope},
            result={
                "event_id": event_id,
                "aggregate_version": aggregate_version,
            },
            state_version=aggregate_version,
            events=[(envelope, None)],
            expected_journal_sequence=journal_cut,
        )
    except ValueError as error:
        if "journal sequence changed after financial evidence validation" in str(error):
            raise ValueError(
                "authority snapshot aggregate_version/journal sequence changed before commit"
            ) from error
        raise
    if not inserted or len(appended) != 1:
        raise ValueError(
            "authority snapshot transaction did not append the exact new event"
        )
    return appended[0]


def restore_authority_snapshot(
    store: JournalStore,
    *,
    authority_id: str,
    evidence_artifact_store: ArtifactStore | None = None,
) -> AuthorityService:
    """Restore latest durable authority state; missing/corrupt state fails closed.

    ``evidence_artifact_store`` is required when canonical financial history
    contains BORROW evidence because replay reuses the full canonical financial
    validator. CASH-only histories need no artifact store.
    """
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
    restored = AuthorityService.restore(latest)
    _assert_authority_state_matches_canonical(
        restored.export_state(),
        store=store,
        evidence_artifact_store=evidence_artifact_store,
    )
    return restored
