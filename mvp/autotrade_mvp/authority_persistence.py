"""Durable journal adapter for the canonical AuthorityService."""

from __future__ import annotations

from mvp.autotrade_mvp.authority import AuthorityService, _instant
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
_NEW_EXPOSURE_RESTORE_FIELDS = {
    "command_id",
    "account_id",
    "environment",
    "reason",
    "restored_at",
    "blocked_command_id",
    "blocked_reason",
    "blocked_at",
}
_SUPPORTED_ENVIRONMENTS = {"SIMULATION", "PAPER", "LIVE"}
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
    # historical schema-v1 snapshots; the snapshot fence preserves that read
    # compatibility while requiring every present entry to be canonical.
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
            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    f"authority state new_exposure_blocks entry has invalid {field}"
                )
            # Exported authority state is already canonical. Reject whitespace or
            # case rewriting instead of silently normalizing authority identity.
            if value != value.strip():
                raise ValueError(
                    f"authority state new_exposure_blocks entry has non-canonical {field}"
                )
            normalized[field] = value
        environment = normalized["environment"]
        if environment not in _SUPPORTED_ENVIRONMENTS:
            raise ValueError(
                "authority state new_exposure_blocks entry has unsupported environment"
            )
        _instant(normalized["blocked_at"], name="blocked_at")
        scope = (normalized["account_id"], environment)
        if scope in indexed:
            raise ValueError(
                "authority state new_exposure_blocks contains duplicate scope"
            )
        indexed[scope] = normalized
    return indexed


def _canonical_event_text(payload: dict, field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(
            f"canonical new-exposure authority has invalid {field}"
        )
    return value


def _canonical_new_exposure_blocks(
    store: JournalStore,
) -> dict[tuple[str, str], dict[str, str]]:
    """Replay only canonical block/restore transitions from authority_state.

    Full AuthorityService journal replay also verifies durable financial
    admissions and may need external artifact evidence. Snapshot publication
    must not acquire that unrelated availability dependency merely to prove the
    emergency no-new-risk state. The source remains the same canonical
    authority_state/canonical journal; this focused replay reproduces its exact
    block/restore transition rules and validates aggregate order.
    """

    active: dict[tuple[str, str], dict[str, str]] = {}
    expected_version = 1
    try:
        events = store.load_events(_CANONICAL_AUTHORITY_TYPE, _CANONICAL_AUTHORITY_ID)
        for event in events:
            if event.get("aggregate_version") != expected_version:
                raise ValueError(
                    "canonical authority journal version sequence is invalid"
                )
            expected_version += 1
            event_type = event.get("event_type")
            if event_type not in {
                "AuthorityNewExposureBlocked",
                "AuthorityNewExposureRestored",
            }:
                continue
            payload = event.get("payload")
            if not isinstance(payload, dict):
                raise ValueError(
                    "canonical new-exposure authority payload is malformed"
                )

            if event_type == "AuthorityNewExposureBlocked":
                if set(payload) != _NEW_EXPOSURE_BLOCK_FIELDS:
                    raise ValueError(
                        "canonical new-exposure block payload has invalid structure"
                    )
                account_id = _canonical_event_text(payload, "account_id")
                environment = _canonical_event_text(payload, "environment")
                if environment not in _SUPPORTED_ENVIRONMENTS:
                    raise ValueError(
                        "canonical new-exposure block environment is unsupported"
                    )
                block = {
                    "account_id": account_id,
                    "environment": environment,
                    "command_id": _canonical_event_text(payload, "command_id"),
                    "reason": _canonical_event_text(payload, "reason"),
                    "blocked_at": _canonical_event_text(payload, "blocked_at"),
                }
                _instant(block["blocked_at"], name="blocked_at")
                scope = (account_id, environment)
                existing = active.get(scope)
                if existing is not None and existing != block:
                    raise ValueError(
                        "canonical new-exposure block history conflicts"
                    )
                active[scope] = block
                continue

            if set(payload) != _NEW_EXPOSURE_RESTORE_FIELDS:
                raise ValueError(
                    "canonical new-exposure restore payload has invalid structure"
                )
            account_id = _canonical_event_text(payload, "account_id")
            environment = _canonical_event_text(payload, "environment")
            if environment not in _SUPPORTED_ENVIRONMENTS:
                raise ValueError(
                    "canonical new-exposure restore environment is unsupported"
                )
            _canonical_event_text(payload, "command_id")
            _canonical_event_text(payload, "reason")
            restored_at = _canonical_event_text(payload, "restored_at")
            _instant(restored_at, name="restored_at")
            expected_block = {
                "account_id": account_id,
                "environment": environment,
                "command_id": _canonical_event_text(payload, "blocked_command_id"),
                "reason": _canonical_event_text(payload, "blocked_reason"),
                "blocked_at": _canonical_event_text(payload, "blocked_at"),
            }
            _instant(expected_block["blocked_at"], name="blocked_at")
            scope = (account_id, environment)
            if active.get(scope) != expected_block:
                raise ValueError(
                    "canonical new-exposure restore does not match active block"
                )
            del active[scope]
    except Exception as error:
        if isinstance(error, ValueError) and str(error).startswith("canonical "):
            raise
        raise ValueError(
            "canonical new-exposure authority cannot be replayed for snapshot proof"
        ) from error
    return active


def _assert_new_exposure_blocks_match_canonical(
    state: dict,
    *,
    store: JournalStore,
) -> None:
    """Require snapshot block state to equal durable canonical block/restore replay.

    This is stronger than treating the active list as append-only. A legitimate
    AuthorityNewExposureRestored transition is accepted because canonical replay
    removes the exact active block; a stale omission, forged addition/rewrite, or
    resurrection after restore cannot equal replayed authority.
    """

    candidate = _indexed_new_exposure_blocks(state)
    canonical = _canonical_new_exposure_blocks(store)
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

    # Validate historical block state even though active block transitions are
    # authorized by canonical block/restore replay rather than append-only rules.
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

    New snapshots are committed against the same global journal cut used to
    prove canonical new-exposure block state. This closes the check-to-append
    race: a concurrent block/restore or any other journal event invalidates the
    cut inside JournalStore's write transaction and the snapshot is not stored.
    Historical immutable event-id retries keep their original envelope semantics.
    """
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
    existing = store.get_event(event_id)
    checked_previous_version: int | None = None
    journal_cut: int | None = None

    # An immutable event-id replay is a lost-reply retry, not a new authority
    # publication. Let JournalStore compare the reconstructed envelope exactly;
    # only a genuinely new event must extend the latest durable snapshot.
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

        # Every new snapshot, including the first and candidates whose active
        # list is unchanged from the previous snapshot, must represent the
        # current canonical block/restore authority exactly.
        _assert_new_exposure_blocks_match_canonical(state, store=store)

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
        # Lost-reply retries for an existing immutable event must reproduce the
        # original envelope byte-for-byte; caller wall-clock drift is not a
        # new financial fact.
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
    restored = AuthorityService.restore(latest)
    # Snapshot restart must not silently resurrect eligibility while canonical
    # block/restore authority says otherwise. A lagging snapshot therefore fails
    # closed until a matching snapshot is durably published.
    _assert_new_exposure_blocks_match_canonical(
        restored.export_state(),
        store=store,
    )
    return restored
