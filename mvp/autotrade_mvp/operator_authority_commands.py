"""Durable operator authority commands over the canonical AuthorityService.

Host acceptance stores a secret-free normalized action payload and the exact
authority cut it was derived from. Execution revalidates that cut and delegates
all financial-authority mutation to AuthorityService.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .authority import (
    AuthorityConflict,
    AuthorityPolicy,
    AuthorityService,
    InstrumentVersionIdentity,
)
from .host_actions import canonical_host_action
from .persistence import JournalStore, payload_digest


AUTHORITY_TYPE = "authority_state"
AUTHORITY_ID = "canonical"
PAYLOAD_SCHEMA = 1
REASON_CODES = frozenset({"OPERATOR_REQUEST", "EMERGENCY_STOP", "POLICY_REVIEW"})


class OperatorAuthorityConflict(ValueError):
    """The accepted command can no longer be applied safely."""


@dataclass(frozen=True)
class AuthorityExecutionResult:
    affected_refs: tuple[str, ...]
    evidence: tuple[Mapping[str, object], ...]


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(name + " must be a non-empty string")
    return value.strip()


def _seq(value: object, name: str) -> int:
    if (
        not isinstance(value, str)
        or not value.isdigit()
        or (len(value) > 1 and value.startswith("0"))
    ):
        raise ValueError(name + " must be a canonical integer sequence string")
    return int(value)


def _keys(
    value: Mapping[str, object],
    required: set[str],
    optional: set[str] | None = None,
) -> None:
    optional = optional or set()
    missing = required - set(value)
    unknown = set(value) - required - optional
    if missing:
        raise ValueError("missing fields: " + ", ".join(sorted(missing)))
    if unknown:
        raise ValueError("unsupported fields: " + ", ".join(sorted(unknown)))


def _policy_payload(policy: AuthorityPolicy) -> dict[str, object]:
    amount = (
        "0"
        if policy.max_notional == 0
        else format(policy.max_notional.normalize(), "f")
    )
    return {
        "policy_id": policy.policy_id,
        "account_id": policy.account_id,
        "environments": sorted(policy.environments),
        "instruments": [
            {"instrument_id": item.instrument_id, "version": item.version}
            for item in sorted(policy.instruments)
        ],
        "actions": sorted(policy.actions),
        "max_notional": amount,
        "expires_at": policy.expires_at,
        "autonomous": policy.autonomous,
        "valid_from": policy.valid_from,
        "protection_only": policy.protection_only,
        "version": policy.version,
    }


def _policy_from_mapping(value: Mapping[str, object]) -> AuthorityPolicy:
    required = {
        "policy_id",
        "account_id",
        "environments",
        "instruments",
        "actions",
        "max_notional",
        "expires_at",
        "autonomous",
        "valid_from",
        "protection_only",
        "version",
    }
    _keys(value, required)
    raw_instruments = value.get("instruments")
    if not isinstance(raw_instruments, list):
        raise ValueError("policy instruments must be an array")
    instruments: list[InstrumentVersionIdentity] = []
    for raw in raw_instruments:
        if not isinstance(raw, Mapping):
            raise ValueError("policy instrument must be an object")
        _keys(raw, {"instrument_id", "version"})
        instruments.append(
            InstrumentVersionIdentity(raw.get("instrument_id"), raw.get("version"))
        )
    policy = AuthorityPolicy.create(
        policy_id=value.get("policy_id"),
        account_id=value.get("account_id"),
        environments=value.get("environments"),
        instruments=instruments,
        actions=value.get("actions"),
        max_notional=value.get("max_notional"),
        expires_at=value.get("expires_at"),
        autonomous=value.get("autonomous"),
        valid_from=value.get("valid_from"),
        protection_only=value.get("protection_only"),
        version=value.get("version"),
    )
    if _policy_payload(policy) != dict(value):
        raise ValueError("policy payload is not canonical")
    return policy


def _events(journal: JournalStore) -> list[dict[str, Any]]:
    events = journal.load_events(AUTHORITY_TYPE, AUTHORITY_ID)
    versions = [int(item["aggregate_version"]) for item in events]
    if versions != list(range(1, len(events) + 1)):
        raise OperatorAuthorityConflict("authority event versions are not contiguous")
    return events


def _state(journal: JournalStore) -> tuple[AuthorityService, dict, list, int]:
    service = AuthorityService(journal)
    state = service.export_state()
    events = _events(journal)
    return service, state, events, len(events)


def _active_targets(
    state: Mapping[str, object],
    account_id: str,
    environment: str,
    include_protection: bool,
) -> list[dict[str, object]]:
    policies = state.get("policies")
    revocations = state.get("revocations")
    if not isinstance(policies, list) or not isinstance(revocations, list):
        raise OperatorAuthorityConflict("authority snapshot is malformed")
    revoked = {
        _text(item.get("policy_id"), "revoked policy_id")
        for item in revocations
        if isinstance(item, Mapping)
    }
    if len(revoked) != len(revocations):
        raise OperatorAuthorityConflict("authority revocation snapshot is malformed")

    result: list[dict[str, object]] = []
    for raw in policies:
        if not isinstance(raw, Mapping):
            raise OperatorAuthorityConflict("authority policy snapshot is malformed")
        policy = _policy_from_mapping(raw)
        if policy.policy_id in revoked:
            continue
        if policy.account_id != account_id or environment not in policy.environments:
            continue
        if policy.protection_only and not include_protection:
            continue
        canonical = _policy_payload(policy)
        result.append(
            {
                "policy_id": policy.policy_id,
                "policy_version": policy.version,
                "policy_hash": payload_digest(canonical),
            }
        )
    return sorted(result, key=lambda item: str(item["policy_id"]))


def canonical_operator_payload(
    journal: JournalStore,
    action: object,
    raw_payload: Mapping[str, object],
    command_id: str,
    account_id: str,
    environment: str,
) -> dict[str, object]:
    """Create the immutable secret-free payload stored with COMMAND_ACCEPTED."""

    if not isinstance(journal, JournalStore):
        raise TypeError("journal must be a JournalStore")
    if not isinstance(raw_payload, Mapping):
        raise ValueError("payload must be an object")
    action_name = canonical_host_action(action)
    command = _text(command_id, "command_id")
    account = _text(account_id, "account_id")
    env = _text(environment, "environment")
    _service, state, _authority_events, version = _state(journal)
    epoch = state.get("epoch")
    if type(epoch) is not int or epoch < 0:
        raise OperatorAuthorityConflict("authority epoch is malformed")

    base: dict[str, object] = {
        "schema_version": PAYLOAD_SCHEMA,
        "command_id": command,
        "account_id": account,
        "environment": env,
        "expected_authority_epoch": str(epoch),
        "expected_authority_version": str(version),
    }

    if action_name == "SET_AUTHORITY":
        required = {
            "policy_id",
            "environments",
            "instruments",
            "actions",
            "max_notional",
            "expires_at",
            "autonomous",
            "valid_from",
            "protection_only",
            "version",
        }
        _keys(raw_payload, required)
        raw_instruments = raw_payload.get("instruments")
        if not isinstance(raw_instruments, list):
            raise ValueError("SET_AUTHORITY instruments must be an array")
        instruments: list[InstrumentVersionIdentity] = []
        for raw in raw_instruments:
            if not isinstance(raw, Mapping):
                raise ValueError("SET_AUTHORITY instrument must be an object")
            _keys(raw, {"instrument_id", "version"})
            instruments.append(
                InstrumentVersionIdentity(raw.get("instrument_id"), raw.get("version"))
            )
        policy = AuthorityPolicy.create(
            policy_id=raw_payload.get("policy_id"),
            account_id=account,
            environments=raw_payload.get("environments"),
            instruments=instruments,
            actions=raw_payload.get("actions"),
            max_notional=raw_payload.get("max_notional"),
            expires_at=raw_payload.get("expires_at"),
            autonomous=raw_payload.get("autonomous"),
            valid_from=raw_payload.get("valid_from"),
            protection_only=raw_payload.get("protection_only"),
            version=raw_payload.get("version"),
        )
        if policy.environments != frozenset({env}):
            raise ValueError(
                "SET_AUTHORITY environments must exactly match host environment"
            )
        canonical = _policy_payload(policy)
        current = state.get("policies")
        revocations = state.get("revocations")
        if not isinstance(current, list) or not isinstance(revocations, list):
            raise OperatorAuthorityConflict("authority policy snapshot is malformed")
        if any(
            isinstance(item, Mapping)
            and item.get("policy_id") == policy.policy_id
            for item in revocations
        ):
            raise OperatorAuthorityConflict(
                "revoked policy_id cannot be reactivated; use a new policy identity"
            )
        for existing in current:
            if (
                isinstance(existing, Mapping)
                and existing.get("policy_id") == policy.policy_id
                and dict(existing) != canonical
            ):
                raise OperatorAuthorityConflict(
                    "policy_id already has different durable content"
                )
        base["policy"] = canonical
        base["policy_hash"] = payload_digest(canonical)
        return base

    _keys(raw_payload, set(), {"reason_code"})
    reason_code = _text(
        raw_payload.get("reason_code", "OPERATOR_REQUEST"),
        "reason_code",
    )
    if reason_code not in REASON_CODES:
        raise ValueError("reason_code must be a canonical operator reason")
    base["reason_code"] = reason_code

    if action_name == "RESTORE_NEW_EXPOSURE":
        raw_blocks = state.get("new_exposure_blocks")
        if not isinstance(raw_blocks, list):
            raise OperatorAuthorityConflict(
                "authority new-exposure block snapshot is malformed"
            )
        matching = [
            item
            for item in raw_blocks
            if isinstance(item, Mapping)
            and item.get("account_id") == account
            and item.get("environment") == env
        ]
        if len(matching) != 1:
            raise OperatorAuthorityConflict(
                "RESTORE_NEW_EXPOSURE requires exactly one active scope block"
            )
        active = matching[0]
        base["expected_block"] = {
            "command_id": _text(
                active.get("command_id"),
                "blocked command_id",
            ),
            "reason": _text(active.get("reason"), "blocked reason"),
            "blocked_at": _text(
                active.get("blocked_at"),
                "blocked blocked_at",
            ),
        }
        return base

    base["target_policies"] = _active_targets(
        state,
        account,
        env,
        action_name == "REVOKE_AUTHORITY",
    )
    return base


def validate_persisted_payload(
    action: object,
    payload: object,
    payload_hash: object,
    account_id: str,
    environment: str,
) -> dict[str, object]:
    action_name = canonical_host_action(action)
    account = _text(account_id, "account_id")
    env = _text(environment, "environment")
    if not isinstance(payload, Mapping):
        raise ValueError("persisted authority payload must be an object")
    value = dict(payload)
    if payload_digest(value) != payload_hash:
        raise ValueError("persisted authority payload hash mismatch")
    if value.get("schema_version") != PAYLOAD_SCHEMA:
        raise ValueError("unsupported persisted authority payload schema")
    _seq(value.get("expected_authority_epoch"), "expected_authority_epoch")
    _seq(value.get("expected_authority_version"), "expected_authority_version")

    command_id = _text(value.get("command_id"), "command_id")
    payload_account = _text(value.get("account_id"), "account_id")
    payload_environment = _text(value.get("environment"), "environment")
    if payload_account != account or payload_environment != env:
        raise ValueError("persisted authority payload scope mismatch")
    if command_id != value.get("command_id"):
        raise ValueError("persisted authority command identity is not canonical")

    common = {
        "schema_version",
        "command_id",
        "account_id",
        "environment",
        "expected_authority_epoch",
        "expected_authority_version",
    }
    if action_name == "SET_AUTHORITY":
        _keys(value, common | {"policy", "policy_hash"})
        policy_raw = value.get("policy")
        if not isinstance(policy_raw, Mapping):
            raise ValueError("persisted SET_AUTHORITY policy must be an object")
        policy = _policy_from_mapping(policy_raw)
        if policy.account_id != account or policy.environments != frozenset({env}):
            raise ValueError("persisted SET_AUTHORITY scope mismatch")
        if value.get("policy_hash") != payload_digest(dict(policy_raw)):
            raise ValueError("persisted SET_AUTHORITY policy hash mismatch")
        return value

    if action_name == "RESTORE_NEW_EXPOSURE":
        _keys(value, common | {"reason_code", "expected_block"})
        reason_code = _text(value.get("reason_code"), "reason_code")
        if reason_code not in REASON_CODES:
            raise ValueError("persisted reason_code is not canonical")
        expected_block = value.get("expected_block")
        if not isinstance(expected_block, Mapping):
            raise ValueError("expected_block must be an object")
        _keys(
            expected_block,
            {"command_id", "reason", "blocked_at"},
        )
        _text(expected_block.get("command_id"), "blocked command_id")
        _text(expected_block.get("reason"), "blocked reason")
        _text(expected_block.get("blocked_at"), "blocked blocked_at")
        return value

    _keys(value, common | {"reason_code", "target_policies"})
    reason_code = _text(value.get("reason_code"), "reason_code")
    if reason_code not in REASON_CODES:
        raise ValueError("persisted reason_code is not canonical")
    targets = value.get("target_policies")
    if not isinstance(targets, list):
        raise ValueError("target_policies must be an array")
    ids: list[str] = []
    for target in targets:
        if not isinstance(target, Mapping):
            raise ValueError("authority target must be an object")
        _keys(target, {"policy_id", "policy_version", "policy_hash"})
        policy_id = _text(target.get("policy_id"), "policy_id")
        version = target.get("policy_version")
        digest = target.get("policy_hash")
        if type(version) is not int or version < 1:
            raise ValueError("policy_version must be positive")
        if not isinstance(digest, str) or not digest.startswith("sha256:"):
            raise ValueError("policy_hash is invalid")
        ids.append(policy_id)
    if ids != sorted(set(ids)):
        raise ValueError("target_policies must be unique and sorted")
    return value


def _find(
    events: list[dict[str, Any]],
    event_type: str,
    policy_id: str,
) -> dict[str, Any] | None:
    matches = [
        event
        for event in events
        if event.get("event_type") == event_type
        and isinstance(event.get("payload"), Mapping)
        and event["payload"].get("policy_id") == policy_id
    ]
    if len(matches) > 1:
        raise OperatorAuthorityConflict(
            "duplicate authority event for policy " + policy_id
        )
    return matches[0] if matches else None


def _find_new_exposure_block(
    events: list[dict[str, Any]],
    command_id: str,
    account_id: str,
    environment: str,
) -> dict[str, Any] | None:
    matches = [
        event
        for event in events
        if event.get("event_type") == "AuthorityNewExposureBlocked"
        and isinstance(event.get("payload"), Mapping)
        and event["payload"].get("command_id") == command_id
        and event["payload"].get("account_id") == account_id
        and event["payload"].get("environment") == environment
    ]
    if len(matches) > 1:
        raise OperatorAuthorityConflict(
            "duplicate new-exposure block for command " + command_id
        )
    return matches[0] if matches else None


def _preexisting_scope_block(
    events: list[dict[str, Any]],
    payload: Mapping[str, object],
    expected_version: int,
) -> dict[str, Any] | None:
    account_id = _text(payload.get("account_id"), "account_id")
    environment = _text(payload.get("environment"), "environment")
    active: dict[str, Any] | None = None
    for event in events:
        version = int(event["aggregate_version"])
        if version > expected_version:
            break
        raw = event.get("payload")
        if not isinstance(raw, Mapping):
            continue
        if (
            raw.get("account_id") != account_id
            or raw.get("environment") != environment
        ):
            continue
        if event.get("event_type") == "AuthorityNewExposureBlocked":
            if active is not None:
                raise OperatorAuthorityConflict(
                    "multiple active durable new-exposure blocks exist "
                    "for one authority scope"
                )
            _text(raw.get("command_id"), "block command_id")
            _text(raw.get("reason"), "block reason")
            _text(raw.get("blocked_at"), "block blocked_at")
            active = event
        elif event.get("event_type") == "AuthorityNewExposureRestored":
            if active is None:
                raise OperatorAuthorityConflict(
                    "new-exposure restore history has no active block"
                )
            active_payload = active.get("payload")
            assert isinstance(active_payload, Mapping)
            if (
                raw.get("blocked_command_id")
                != active_payload.get("command_id")
                or raw.get("blocked_reason")
                != active_payload.get("reason")
                or raw.get("blocked_at")
                != active_payload.get("blocked_at")
            ):
                raise OperatorAuthorityConflict(
                    "new-exposure restore history targets a different block"
                )
            active = None
    return active


def _validated_new_exposure_block(
    events: list[dict[str, Any]],
    payload: Mapping[str, object],
    accepted_at: str,
    expected_version: int,
) -> dict[str, Any] | None:
    command_id = _text(payload.get("command_id"), "command_id")
    account_id = _text(payload.get("account_id"), "account_id")
    environment = _text(payload.get("environment"), "environment")
    event = _find_new_exposure_block(
        events,
        command_id,
        account_id,
        environment,
    )
    if event is None:
        return None
    reason = "host_operator_command:BLOCK_NEW_EXPOSURE:" + _text(
        payload.get("reason_code"), "reason_code"
    )
    expected_payload = {
        "command_id": command_id,
        "account_id": _text(payload.get("account_id"), "account_id"),
        "environment": _text(payload.get("environment"), "environment"),
        "reason": reason,
        "blocked_at": accepted_at,
    }
    if (
        event.get("payload") != expected_payload
        or int(event["aggregate_version"]) <= expected_version
    ):
        raise OperatorAuthorityConflict(
            "new-exposure block event does not match accepted command"
        )
    return event


def _find_new_exposure_restore(
    events: list[dict[str, Any]],
    command_id: str,
    account_id: str,
    environment: str,
) -> dict[str, Any] | None:
    matches = [
        event
        for event in events
        if event.get("event_type") == "AuthorityNewExposureRestored"
        and isinstance(event.get("payload"), Mapping)
        and event["payload"].get("command_id") == command_id
        and event["payload"].get("account_id") == account_id
        and event["payload"].get("environment") == environment
    ]
    if len(matches) > 1:
        raise OperatorAuthorityConflict(
            "duplicate new-exposure restore for command " + command_id
        )
    return matches[0] if matches else None


def _validated_new_exposure_restore(
    events: list[dict[str, Any]],
    payload: Mapping[str, object],
    accepted_at: str,
    expected_version: int,
) -> dict[str, Any] | None:
    command_id = _text(payload.get("command_id"), "command_id")
    account_id = _text(payload.get("account_id"), "account_id")
    environment = _text(payload.get("environment"), "environment")
    event = _find_new_exposure_restore(
        events,
        command_id,
        account_id,
        environment,
    )
    if event is None:
        return None
    expected_block = payload.get("expected_block")
    assert isinstance(expected_block, Mapping)
    reason = "host_operator_command:RESTORE_NEW_EXPOSURE:" + _text(
        payload.get("reason_code"), "reason_code"
    )
    expected_payload = {
        "command_id": command_id,
        "account_id": account_id,
        "environment": environment,
        "reason": reason,
        "restored_at": accepted_at,
        "blocked_command_id": _text(
            expected_block.get("command_id"),
            "blocked command_id",
        ),
        "blocked_reason": _text(
            expected_block.get("reason"),
            "blocked reason",
        ),
        "blocked_at": _text(
            expected_block.get("blocked_at"),
            "blocked blocked_at",
        ),
    }
    if (
        event.get("payload") != expected_payload
        or int(event["aggregate_version"]) <= expected_version
    ):
        raise OperatorAuthorityConflict(
            "new-exposure restore event does not match accepted command"
        )
    return event


def _new_exposure_block_ref(payload: Mapping[str, object]) -> str:
    return (
        "authority-new-exposure-block:"
        + _text(payload.get("account_id"), "account_id")
        + ":"
        + _text(payload.get("environment"), "environment")
    )


def _evidence(event: Mapping[str, object]) -> dict[str, object]:
    value: dict[str, object] = {
        "kind": "canonical-authority-event",
        "event_id": _text(event.get("event_id"), "authority event_id"),
        "event_type": _text(event.get("event_type"), "authority event_type"),
        "aggregate_type": AUTHORITY_TYPE,
        "aggregate_id": AUTHORITY_ID,
        "aggregate_version": str(event.get("aggregate_version")),
        "payload_hash": _text(event.get("payload_hash"), "authority payload_hash"),
    }
    sequence = event.get("journal_sequence")
    if sequence is not None:
        if type(sequence) is not int or sequence < 1:
            raise OperatorAuthorityConflict("authority journal sequence is malformed")
        value["journal_sequence"] = str(sequence)
    return value


def _cut_evidence(
    events: list[dict[str, Any]],
    expected_version: int,
    expected_epoch: int,
) -> dict[str, object]:
    prefix = events[:expected_version]
    if len(prefix) != expected_version:
        raise OperatorAuthorityConflict("accepted authority cut no longer exists")
    if prefix:
        value = _evidence(prefix[-1])
        value["kind"] = "canonical-authority-state-cut"
        value["authority_epoch"] = str(expected_epoch)
        return value
    return {
        "kind": "canonical-authority-state-cut",
        "aggregate_type": AUTHORITY_TYPE,
        "aggregate_id": AUTHORITY_ID,
        "aggregate_version": "0",
        "authority_epoch": str(expected_epoch),
    }


def _resolved(
    journal: JournalStore,
    action: str,
    payload: Mapping[str, object],
    accepted_at: str,
) -> AuthorityExecutionResult | None:
    events = _events(journal)
    expected_version = _seq(
        payload.get("expected_authority_version"),
        "expected_authority_version",
    )
    expected_epoch = _seq(
        payload.get("expected_authority_epoch"),
        "expected_authority_epoch",
    )

    if action == "SET_AUTHORITY":
        raw_policy = payload.get("policy")
        assert isinstance(raw_policy, Mapping)
        policy = _policy_from_mapping(raw_policy)
        event = _find(events, "AuthorityPolicyRegistered", policy.policy_id)
        if event is None:
            return None
        if event.get("payload") != dict(raw_policy):
            raise OperatorAuthorityConflict("SET_AUTHORITY event content mismatch")
        return AuthorityExecutionResult(
            (("authority-policy:" + policy.policy_id),),
            (_evidence(event),),
        )

    if action == "RESTORE_NEW_EXPOSURE":
        restored = _validated_new_exposure_restore(
            events,
            payload,
            accepted_at,
            expected_version,
        )
        if restored is None:
            return None
        return AuthorityExecutionResult(
            (_new_exposure_block_ref(payload),),
            (_evidence(restored),),
        )

    targets = payload.get("target_policies")
    assert isinstance(targets, list)
    reason = "host_operator_command:" + action + ":" + _text(
        payload.get("reason_code"), "reason_code"
    )
    affected: list[str] = []
    evidence: list[Mapping[str, object]] = []

    if action == "BLOCK_NEW_EXPOSURE":
        block = _validated_new_exposure_block(
            events,
            payload,
            accepted_at,
            expected_version,
        )
        if block is None:
            block = _preexisting_scope_block(
                events,
                payload,
                expected_version,
            )
        if block is None:
            return None
        affected.append(_new_exposure_block_ref(payload))
        evidence.append(_evidence(block))
    elif not targets:
        return AuthorityExecutionResult(
            ("authority-state:canonical",),
            (_cut_evidence(events, expected_version, expected_epoch),),
        )
    for target in targets:
        assert isinstance(target, Mapping)
        policy_id = str(target["policy_id"])
        registered = _find(events, "AuthorityPolicyRegistered", policy_id)
        if (
            registered is None
            or not isinstance(registered.get("payload"), Mapping)
            or registered["payload"].get("version") != target["policy_version"]
            or payload_digest(dict(registered["payload"])) != target["policy_hash"]
        ):
            raise OperatorAuthorityConflict(
                "authority target registration mismatch: " + policy_id
            )
        revoked = _find(events, "AuthorityPolicyRevoked", policy_id)
        if revoked is None:
            return None
        raw = revoked.get("payload")
        if (
            not isinstance(raw, Mapping)
            or raw.get("reason") != reason
            or raw.get("revoked_at") != accepted_at
            or int(revoked["aggregate_version"]) <= expected_version
        ):
            raise OperatorAuthorityConflict(
                "authority target revocation mismatch: " + policy_id
            )
        affected.append("authority-policy:" + policy_id)
        evidence.append(_evidence(revoked))
    return AuthorityExecutionResult(tuple(affected), tuple(evidence))


def execute_operator_authority_action(
    journal: JournalStore,
    action: object,
    action_payload: object,
    action_payload_hash: object,
    account_id: str,
    environment: str,
    accepted_at: str,
) -> AuthorityExecutionResult:
    """Execute or idempotently resume one accepted authority action."""

    action_name = canonical_host_action(action)
    accepted = _text(accepted_at, "accepted_at")
    payload = validate_persisted_payload(
        action_name,
        action_payload,
        action_payload_hash,
        account_id,
        environment,
    )
    resolved = _resolved(journal, action_name, payload, accepted)
    if resolved is not None:
        return resolved

    expected_version = _seq(
        payload["expected_authority_version"],
        "expected_authority_version",
    )
    expected_epoch = _seq(
        payload["expected_authority_epoch"],
        "expected_authority_epoch",
    )
    service, state, events, current_version = _state(journal)

    if action_name == "SET_AUTHORITY":
        if state.get("epoch") != expected_epoch or current_version != expected_version:
            raise OperatorAuthorityConflict("authority changed after acceptance")
        raw_policy = payload["policy"]
        assert isinstance(raw_policy, Mapping)
        try:
            service.register_policy(_policy_from_mapping(raw_policy))
        except AuthorityConflict as error:
            raise OperatorAuthorityConflict(
                "AuthorityService rejected SET_AUTHORITY"
            ) from error
    elif action_name == "RESTORE_NEW_EXPOSURE":
        if state.get("epoch") != expected_epoch or current_version != expected_version:
            raise OperatorAuthorityConflict("authority changed after acceptance")
        expected_block = payload.get("expected_block")
        assert isinstance(expected_block, Mapping)
        raw_blocks = state.get("new_exposure_blocks")
        if not isinstance(raw_blocks, list):
            raise OperatorAuthorityConflict(
                "authority new-exposure block snapshot is malformed"
            )
        active = [
            item
            for item in raw_blocks
            if isinstance(item, Mapping)
            and item.get("account_id") == payload.get("account_id")
            and item.get("environment") == payload.get("environment")
        ]
        expected_active = {
            "account_id": payload.get("account_id"),
            "environment": payload.get("environment"),
            "command_id": expected_block.get("command_id"),
            "reason": expected_block.get("reason"),
            "blocked_at": expected_block.get("blocked_at"),
        }
        if len(active) != 1 or dict(active[0]) != expected_active:
            raise OperatorAuthorityConflict(
                "accepted restore no longer matches the active new-exposure block"
            )
        reason = (
            "host_operator_command:RESTORE_NEW_EXPOSURE:"
            + _text(payload["reason_code"], "reason_code")
        )
        try:
            service.restore_new_exposure(
                account_id=_text(payload["account_id"], "account_id"),
                environment=_text(payload["environment"], "environment"),
                reason=reason,
                restored_at=accepted,
                command_id=_text(payload["command_id"], "command_id"),
                expected_block_command_id=_text(
                    expected_block["command_id"],
                    "blocked command_id",
                ),
                expected_block_reason=_text(
                    expected_block["reason"],
                    "blocked reason",
                ),
                expected_blocked_at=_text(
                    expected_block["blocked_at"],
                    "blocked blocked_at",
                ),
            )
        except AuthorityConflict as error:
            raise OperatorAuthorityConflict(
                "AuthorityService rejected new-exposure restore"
            ) from error
    else:
        targets = payload["target_policies"]
        assert isinstance(targets, list)
        reason = (
            "host_operator_command:"
            + action_name
            + ":"
            + _text(payload["reason_code"], "reason_code")
        )
        exact_done = 0
        missing: list[str] = []
        target_ids = {str(item["policy_id"]) for item in targets}
        policies = state.get("policies")
        if not isinstance(policies, list):
            raise OperatorAuthorityConflict("authority policy snapshot is malformed")
        by_id = {
            str(item.get("policy_id")): item
            for item in policies
            if isinstance(item, Mapping)
        }
        for target in targets:
            assert isinstance(target, Mapping)
            policy_id = str(target["policy_id"])
            policy = by_id.get(policy_id)
            if (
                policy is None
                or policy.get("version") != target["policy_version"]
                or payload_digest(dict(policy)) != target["policy_hash"]
            ):
                raise OperatorAuthorityConflict(
                    "authority target changed: " + policy_id
                )
            revoked = _find(events, "AuthorityPolicyRevoked", policy_id)
            if revoked is None:
                missing.append(policy_id)
                continue
            raw = revoked.get("payload")
            if (
                not isinstance(raw, Mapping)
                or raw.get("reason") != reason
                or raw.get("revoked_at") != accepted
                or int(revoked["aggregate_version"]) <= expected_version
            ):
                raise OperatorAuthorityConflict(
                    "authority target was revoked differently: " + policy_id
                )
            exact_done += 1

        block_event = None
        preexisting_block = None
        block_done = 0
        if action_name == "BLOCK_NEW_EXPOSURE":
            block_event = _validated_new_exposure_block(
                events,
                payload,
                accepted,
                expected_version,
            )
            preexisting_block = _preexisting_scope_block(
                events,
                payload,
                expected_version,
            )
            block_done = 1 if block_event is not None else 0

        post_cut = [
            event
            for event in events
            if int(event["aggregate_version"]) > expected_version
        ]
        if len(post_cut) != exact_done + block_done:
            raise OperatorAuthorityConflict(
                "authority changed outside accepted operation"
            )
        for event in post_cut:
            raw = event.get("payload")
            is_revocation = (
                event.get("event_type") == "AuthorityPolicyRevoked"
                and isinstance(raw, Mapping)
                and raw.get("policy_id") in target_ids
                and raw.get("reason") == reason
                and raw.get("revoked_at") == accepted
            )
            is_block = block_event is not None and event.get("event_id") == block_event.get(
                "event_id"
            )
            if not is_revocation and not is_block:
                raise OperatorAuthorityConflict(
                    "authority changed outside accepted operation"
                )
        if (
            state.get("epoch") != expected_epoch + exact_done
            or current_version != expected_version + exact_done + block_done
        ):
            raise OperatorAuthorityConflict(
                "authority cut changed outside accepted operation"
            )

        if (
            action_name == "BLOCK_NEW_EXPOSURE"
            and block_event is None
            and preexisting_block is None
        ):
            try:
                service.block_new_exposure(
                    account_id=_text(payload["account_id"], "account_id"),
                    environment=_text(payload["environment"], "environment"),
                    reason=reason,
                    blocked_at=accepted,
                    command_id=_text(payload["command_id"], "command_id"),
                )
            except AuthorityConflict as error:
                raise OperatorAuthorityConflict(
                    "AuthorityService rejected new-exposure block"
                ) from error

        for policy_id in missing:
            try:
                service.revoke_policy(
                    policy_id,
                    reason=reason,
                    revoked_at=accepted,
                )
            except AuthorityConflict as error:
                raise OperatorAuthorityConflict(
                    "AuthorityService rejected revocation: " + policy_id
                ) from error

    resolved = _resolved(journal, action_name, payload, accepted)
    if resolved is None:
        raise OperatorAuthorityConflict(
            "accepted authority action has no canonical completion evidence"
        )
    return resolved


def observed_authority_operation_effects(
    journal: JournalStore,
    action: object,
    action_payload: object,
    action_payload_hash: object,
    account_id: str,
    environment: str,
    accepted_at: str,
) -> AuthorityExecutionResult:
    """Return only durable authority effects attributable to this accepted action."""

    action_name = canonical_host_action(action)
    accepted = _text(accepted_at, "accepted_at")
    payload = validate_persisted_payload(
        action_name,
        action_payload,
        action_payload_hash,
        account_id,
        environment,
    )
    events = _events(journal)
    expected_version = _seq(
        payload["expected_authority_version"],
        "expected_authority_version",
    )

    if action_name == "SET_AUTHORITY":
        raw_policy = payload.get("policy")
        assert isinstance(raw_policy, Mapping)
        policy = _policy_from_mapping(raw_policy)
        event = _find(events, "AuthorityPolicyRegistered", policy.policy_id)
        if (
            event is None
            or event.get("payload") != dict(raw_policy)
            or int(event["aggregate_version"]) <= expected_version
        ):
            return AuthorityExecutionResult((), ())
        return AuthorityExecutionResult(
            ("authority-policy:" + policy.policy_id,),
            (_evidence(event),),
        )

    if action_name == "RESTORE_NEW_EXPOSURE":
        restored = _validated_new_exposure_restore(
            events,
            payload,
            accepted,
            expected_version,
        )
        if restored is None:
            return AuthorityExecutionResult((), ())
        return AuthorityExecutionResult(
            (_new_exposure_block_ref(payload),),
            (_evidence(restored),),
        )

    targets = payload.get("target_policies")
    assert isinstance(targets, list)
    reason = "host_operator_command:" + action_name + ":" + _text(
        payload.get("reason_code"), "reason_code"
    )
    affected: list[str] = []
    evidence: list[Mapping[str, object]] = []
    if action_name == "BLOCK_NEW_EXPOSURE":
        block = _validated_new_exposure_block(
            events,
            payload,
            accepted,
            expected_version,
        )
        if block is not None:
            affected.append(_new_exposure_block_ref(payload))
            evidence.append(_evidence(block))
    for target in targets:
        assert isinstance(target, Mapping)
        policy_id = str(target["policy_id"])
        registered = _find(events, "AuthorityPolicyRegistered", policy_id)
        if (
            registered is None
            or not isinstance(registered.get("payload"), Mapping)
            or registered["payload"].get("version") != target["policy_version"]
            or payload_digest(dict(registered["payload"])) != target["policy_hash"]
        ):
            continue
        revoked = _find(events, "AuthorityPolicyRevoked", policy_id)
        if revoked is None:
            continue
        raw = revoked.get("payload")
        if (
            isinstance(raw, Mapping)
            and raw.get("reason") == reason
            and raw.get("revoked_at") == accepted
            and int(revoked["aggregate_version"]) > expected_version
        ):
            affected.append("authority-policy:" + policy_id)
            evidence.append(_evidence(revoked))
    return AuthorityExecutionResult(tuple(affected), tuple(evidence))


def validate_authority_success_evidence(
    journal: JournalStore,
    action: object,
    action_payload: object,
    action_payload_hash: object,
    account_id: str,
    environment: str,
    accepted_at: str,
    affected_refs: tuple[str, ...],
    evidence: tuple[Mapping[str, object], ...],
) -> None:
    """Verify terminal success from canonical authority events, read-only."""

    action_name = canonical_host_action(action)
    payload = validate_persisted_payload(
        action_name,
        action_payload,
        action_payload_hash,
        account_id,
        environment,
    )
    result = _resolved(
        journal,
        action_name,
        payload,
        _text(accepted_at, "accepted_at"),
    )
    if result is None:
        raise ValueError("terminal authority operation has no canonical outcome")
    if tuple(affected_refs) != result.affected_refs:
        raise ValueError("terminal authority affected_refs do not match journal")
    if tuple(dict(item) for item in evidence) != tuple(
        dict(item) for item in result.evidence
    ):
        raise ValueError("terminal authority evidence does not match journal")
