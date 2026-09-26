"""Execute durable host authority commands through the canonical AuthorityService."""

from __future__ import annotations

from typing import Mapping

from .authority import AuthorityConflict, AuthorityService
from .durable_host_api import JournalBackedHostCommandStore
from .host_action_payloads import authority_policy_from_action_payload


class AuthorityCommandExecutor:
    """Restart-safe bridge from accepted host operations to financial authority."""

    def __init__(
        self,
        host: JournalBackedHostCommandStore,
        authority: AuthorityService,
    ):
        if not isinstance(host, JournalBackedHostCommandStore):
            raise TypeError("host must be JournalBackedHostCommandStore")
        if not isinstance(authority, AuthorityService):
            raise TypeError("authority must be AuthorityService")
        if authority.store is None:
            raise ValueError("authority command execution requires durable AuthorityService")
        if authority.store.path.resolve() != host.journal.path.resolve():
            raise ValueError("host and authority must use the same JournalStore path")
        self.host = host
        self.authority = authority

    @staticmethod
    def _evidence(event: Mapping[str, object]) -> dict[str, object]:
        return {
            "artifact_id": str(event["event_id"]),
            "sha256": str(event["payload_hash"]),
            "observed_at": str(event["committed_at"]),
        }

    def _authority_event(
        self,
        event_type: str,
        *,
        field: str,
        value: str,
    ) -> Mapping[str, object]:
        assert self.authority.store is not None
        matches = [
            event
            for event in self.authority.store.load_events(
                "authority_state",
                "canonical",
            )
            if event["event_type"] == event_type
            and isinstance(event.get("payload"), Mapping)
            and event["payload"].get(field) == value
        ]
        if len(matches) != 1:
            raise AuthorityConflict(
                f"canonical authority event {event_type} is missing or ambiguous"
            )
        return matches[0]

    def execute(self, operation_id: str):
        operation = self.host.get_operation(operation_id)
        if operation.phase in self.host.TERMINAL_PHASES:
            return operation

        accepted = self.host.get_accepted_command(operation_id)
        action = accepted["action"]
        account_id = accepted["account_id"]
        environment = accepted["environment"]
        action_payload = accepted["action_payload"]
        accepted_at = accepted["started_at"]

        if operation.phase != "RUNNING":
            operation = self.host.update_operation(
                operation_id,
                "RUNNING",
                remaining_uncertainty=("authority_mutation_pending",),
            )

        try:
            if action == "SET_AUTHORITY":
                policy = authority_policy_from_action_payload(
                    action_payload,
                    account_id=account_id,
                    environment=environment,
                )
                self.authority.register_policy(policy)
                event = self._authority_event(
                    "AuthorityPolicyRegistered",
                    field="policy_id",
                    value=policy.policy_id,
                )
                affected_refs = (f"authority-policy:{policy.policy_id}",)
                evidence = (self._evidence(event),)

            elif action == "REVOKE_AUTHORITY":
                policy_id = str(action_payload["policy_id"])
                policy = self.authority.policy(policy_id)
                if (
                    policy.account_id != account_id
                    or environment not in policy.environments
                ):
                    raise ValueError(
                        "REVOKE_AUTHORITY policy does not match accepted host scope"
                    )
                self.authority.revoke_policy(
                    policy_id,
                    reason=str(action_payload["reason"]),
                    revoked_at=accepted_at,
                )
                event = self._authority_event(
                    "AuthorityPolicyRevoked",
                    field="policy_id",
                    value=policy_id,
                )
                affected_refs = (f"authority-policy:{policy_id}",)
                evidence = (self._evidence(event),)

            elif action == "BLOCK_NEW_EXPOSURE":
                self.authority.block_new_exposure(
                    account_id=account_id,
                    environment=environment,
                    reason=str(action_payload["reason"]),
                    blocked_at=accepted_at,
                    command_id=str(accepted["command_id"]),
                )
                event = self._authority_event(
                    "AuthorityNewExposureBlocked",
                    field="command_id",
                    value=str(accepted["command_id"]),
                )
                affected_refs = (
                    f"new-exposure-block:{environment}:{account_id}",
                )
                evidence = (self._evidence(event),)

            else:
                raise ValueError("unsupported durable authority action")
        except AuthorityConflict:
            return self.host.update_operation(
                operation_id,
                "WAITING_EXTERNAL",
                remaining_uncertainty=("authority_state_reload_required",),
            )
        except (KeyError, TypeError, ValueError):
            return self.host.update_operation(
                operation_id,
                "FAILED",
                remaining_uncertainty=(),
            )

        return self.host.update_operation(
            operation_id,
            "SUCCEEDED",
            remaining_uncertainty=(),
            affected_refs=affected_refs,
            evidence=evidence,
        )
