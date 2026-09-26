"""Canonical secret-free payloads for privileged host authority commands."""

from __future__ import annotations

from typing import Mapping

from .authority import AuthorityPolicy
from .host_actions import canonical_host_action


def _text(value: object, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{name} must be canonical non-empty text")
    return value


def _object(value: object, *, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    if any(not isinstance(key, str) for key in value):
        raise ValueError(f"{name} keys must be text")
    return value


def _keys(
    value: Mapping[str, object],
    *,
    required: frozenset[str],
    optional: frozenset[str] = frozenset(),
    name: str,
) -> None:
    keys = frozenset(value)
    missing = required - keys
    unknown = keys - required - optional
    if missing:
        raise ValueError(f"{name} is missing required fields: {sorted(missing)}")
    if unknown:
        raise ValueError(f"{name} contains unsupported fields: {sorted(unknown)}")


def _policy_payload(policy: AuthorityPolicy) -> dict[str, object]:
    return {
        "policy_id": policy.policy_id,
        "account_id": policy.account_id,
        "environments": sorted(policy.environments),
        "instruments": [
            {
                "instrument_id": instrument.instrument_id,
                "version": instrument.version,
            }
            for instrument in sorted(policy.instruments)
        ],
        "actions": sorted(policy.actions),
        "max_notional": format(policy.max_notional, "f").rstrip("0").rstrip(".")
        if "." in format(policy.max_notional, "f")
        else format(policy.max_notional, "f"),
        "expires_at": policy.expires_at,
        "autonomous": policy.autonomous,
        "valid_from": policy.valid_from,
        "protection_only": policy.protection_only,
        "version": policy.version,
    }


def _authority_policy(
    value: object,
    *,
    account_id: str,
    environment: str,
) -> AuthorityPolicy:
    item = _object(value, name="SET_AUTHORITY policy")
    expected = frozenset(
        {
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
    )
    _keys(item, required=expected, name="SET_AUTHORITY policy")
    instruments = item["instruments"]
    if not isinstance(instruments, list):
        raise ValueError("SET_AUTHORITY policy instruments must be an array")
    normalized_instruments = []
    for instrument in instruments:
        ref = _object(instrument, name="SET_AUTHORITY instrument")
        _keys(
            ref,
            required=frozenset({"instrument_id", "version"}),
            name="SET_AUTHORITY instrument",
        )
        normalized_instruments.append(
            (ref["instrument_id"], ref["version"])
        )
    environments = item["environments"]
    actions = item["actions"]
    if not isinstance(environments, list) or not isinstance(actions, list):
        raise ValueError("SET_AUTHORITY environments and actions must be arrays")
    policy = AuthorityPolicy.create(
        policy_id=item["policy_id"],
        account_id=item["account_id"],
        environments=environments,
        instruments=normalized_instruments,
        actions=actions,
        max_notional=item["max_notional"],
        expires_at=item["expires_at"],
        autonomous=item["autonomous"],
        valid_from=item["valid_from"],
        protection_only=item["protection_only"],
        version=item["version"],
    )
    if policy.account_id != account_id:
        raise ValueError("SET_AUTHORITY policy account does not match host scope")
    if policy.environments != frozenset({environment}):
        raise ValueError(
            "SET_AUTHORITY policy must be scoped to the active host environment"
        )
    return policy


def canonical_authority_action_payload(
    action: object,
    payload: object,
    *,
    account_id: str,
    environment: str,
) -> dict[str, object]:
    """Return the only action payload that may enter durable host events.

    Session/bearer/authentication data is deliberately not accepted here.
    """

    normalized_action = canonical_host_action(action)
    normalized_account = _text(account_id, name="account_id")
    normalized_environment = _text(environment, name="environment")
    item = _object(payload, name=f"{normalized_action} payload")

    if normalized_action == "BLOCK_NEW_EXPOSURE":
        _keys(
            item,
            required=frozenset(),
            name="BLOCK_NEW_EXPOSURE payload",
        )
        return {"reason": "operator_requested_block_new_exposure"}

    if normalized_action == "REVOKE_AUTHORITY":
        _keys(
            item,
            required=frozenset({"policy_id"}),
            name="REVOKE_AUTHORITY payload",
        )
        return {
            "policy_id": _text(
                item["policy_id"],
                name="REVOKE_AUTHORITY policy_id",
            ),
            "reason": "operator_requested_authority_revocation",
        }

    if normalized_action == "SET_AUTHORITY":
        _keys(
            item,
            required=frozenset({"policy"}),
            name="SET_AUTHORITY payload",
        )
        policy = _authority_policy(
            item["policy"],
            account_id=normalized_account,
            environment=normalized_environment,
        )
        return {"policy": _policy_payload(policy)}

    raise ValueError("Unsupported canonical host authority action")


def authority_policy_from_action_payload(
    payload: Mapping[str, object],
    *,
    account_id: str,
    environment: str,
) -> AuthorityPolicy:
    normalized = canonical_authority_action_payload(
        "SET_AUTHORITY",
        payload,
        account_id=account_id,
        environment=environment,
    )
    return _authority_policy(
        normalized["policy"],
        account_id=account_id,
        environment=environment,
    )
