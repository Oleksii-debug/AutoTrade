"""Journal-backed host command API projection.

This adapter reuses :class:`JournalStore` as the single durable authority for
host command acceptance and resumable UI events.  It does not create a second
persistence system and it never treats command acceptance as financial
completion.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable, Mapping
from uuid import NAMESPACE_URL, uuid5

from contracts.bindings.python.common_scalars import is_valid_common_scalar

from .host_actions import canonical_host_action
from .host_action_payloads import canonical_authority_action_payload
from .host_api import (
    CommandResult,
    EventGap,
    HostEvent,
    OperationResult,
    command_result_payload,
)
from .persistence import JournalStore, payload_digest


class JournalBackedHostCommandStore:
    """Durable host API semantics over the canonical journal."""

    AGGREGATE_TYPE = "HOST_CONTROL"
    AGGREGATE_ID = "host"
    TERMINAL_PHASES = {"SUCCEEDED", "FAILED", "CANCELLED"}
    UPDATE_PHASES = {"RUNNING", "WAITING_EXTERNAL", "UNKNOWN", *TERMINAL_PHASES}

    def __init__(
        self,
        journal: JournalStore,
        *,
        account_id: str,
        environment: str,
        session_validator: Callable[[str, str, str, str], bool],
        request_origin_provider: Callable[[], str],
        max_events: int = 100,
        now: Callable[[], str] | None = None,
    ) -> None:
        if not isinstance(journal, JournalStore):
            raise TypeError("journal must be a JournalStore")
        if not isinstance(account_id, str) or not account_id:
            raise ValueError("account_id must be a non-empty string")
        if not is_valid_common_scalar("Environment", environment):
            raise ValueError("environment must be a canonical Environment")
        if not callable(session_validator):
            raise TypeError("session_validator must be callable")
        if not callable(request_origin_provider):
            raise TypeError("request_origin_provider must be callable")
        if not isinstance(max_events, int) or isinstance(max_events, bool) or max_events < 1:
            raise ValueError("max_events must be positive")
        self._journal = journal
        self.account_id = account_id
        self.environment = environment
        self._session_validator = session_validator
        self._request_origin_provider = request_origin_provider
        self._max_events = max_events
        self._now = now or (
            lambda: datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        )

    @staticmethod
    def _required_text(command: Mapping[str, object], field: str) -> str:
        value = command.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field} must be a non-empty string")
        return value.strip()

    @staticmethod
    def _command_result(value: Mapping[str, object]) -> CommandResult:
        return CommandResult(
            command_id=str(value["command_id"]),
            status=str(value["status"]),
            state_version=str(value["state_version"]),
            reason_codes=tuple(str(x) for x in value.get("reason_codes", ())),
            field_errors=tuple(
                dict(x)
                for x in value.get("field_errors", ())
                if isinstance(x, Mapping)
            ),
            operation_id=(
                None
                if value.get("operation_id") is None
                else str(value["operation_id"])
            ),
            current_value_ref=(
                None
                if value.get("current_value_ref") is None
                else str(value["current_value_ref"])
            ),
        )

    @staticmethod
    def _result_dict(result: CommandResult) -> dict[str, object]:
        return command_result_payload(result)

    @staticmethod
    def _normalize_refs(values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not isinstance(item, str) or not item.strip() for item in values):
            raise ValueError("affected_refs must contain non-empty strings")
        return tuple(values)

    @staticmethod
    def _replay_text_array(
        payload: Mapping[str, object],
        field: str,
        *,
        default: tuple[str, ...] | None = None,
    ) -> tuple[str, ...]:
        if field not in payload:
            if default is None:
                raise ValueError(f"Host journal {field} must be an array")
            return default
        value = payload[field]
        if not isinstance(value, list):
            raise ValueError(f"Host journal {field} must be an array")
        if any(not isinstance(item, str) or not item.strip() for item in value):
            raise ValueError(
                f"Host journal {field} must contain non-empty strings"
            )
        return tuple(value)

    @staticmethod
    def _replay_evidence(
        payload: Mapping[str, object],
        *,
        default: tuple[Mapping[str, object], ...] | None = None,
    ) -> tuple[Mapping[str, object], ...]:
        if "evidence" not in payload:
            if default is None:
                raise ValueError("Host journal operation evidence must be an array")
            return default
        value = payload["evidence"]
        if not isinstance(value, list) or any(
            not isinstance(item, Mapping) for item in value
        ):
            raise ValueError("Host journal operation evidence must contain objects")
        return tuple(dict(item) for item in value)

    @staticmethod
    def _normalize_evidence(
        values: tuple[Mapping[str, object], ...],
    ) -> tuple[Mapping[str, object], ...]:
        if any(not isinstance(item, Mapping) for item in values):
            raise ValueError("operation evidence must contain objects")
        return tuple(dict(item) for item in values)

    def _events(self) -> list[dict[str, object]]:
        return self._journal.load_events(self.AGGREGATE_TYPE, self.AGGREGATE_ID)

    @property
    def journal(self) -> JournalStore:
        return self._journal

    @property
    def state_version(self) -> int:
        events = self._events()
        return int(events[-1]["aggregate_version"]) if events else 0

    @property
    def cursor(self) -> int:
        return self.state_version

    def _event_envelope(
        self,
        *,
        event_id: str,
        event_type: str,
        aggregate_version: int,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        body = dict(payload)
        return {
            "event_id": event_id,
            "event_type": event_type,
            "aggregate_type": self.AGGREGATE_TYPE,
            "aggregate_id": self.AGGREGATE_ID,
            "aggregate_version": str(aggregate_version),
            "payload": body,
            "payload_hash": payload_digest(body),
            "committed_at": self._now(),
        }

    @staticmethod
    def _conflict_reason(error: ValueError) -> str:
        message = str(error)
        if "idempotency_key" in message:
            return "idempotency_key_conflict"
        if "command_id" in message:
            return "command_id_conflict"
        if "aggregate_version" in message:
            return "stale_state_version"
        raise error

    def submit(self, command: Mapping[str, object]) -> CommandResult:
        if not isinstance(command, Mapping):
            raise TypeError("command must be a mapping")
        command_id = self._required_text(command, "command_id")
        idempotency_key = self._required_text(command, "idempotency_key")
        actor = self._required_text(command, "actor")
        session = self._required_text(command, "session")
        account_id = self._required_text(command, "account_id")
        environment = self._required_text(command, "environment")
        if not is_valid_common_scalar("Environment", environment):
            raise ValueError("environment must be a canonical Environment")
        if account_id != self.account_id or environment != self.environment:
            raise ValueError("command scope does not match active host account/environment")
        action = canonical_host_action(command.get("action"))
        expected_raw = self._required_text(command, "expected_state_version")
        if "payload" not in command or not isinstance(command["payload"], dict):
            raise ValueError("payload must be an object")
        if not expected_raw.isdigit():
            raise ValueError("expected_state_version must be a sequence")
        request_origin = self._request_origin_provider()
        if not isinstance(request_origin, str) or not request_origin.strip():
            raise PermissionError("Current request origin is unavailable")
        if not self._session_validator(session, actor, request_origin.strip(), action):
            raise PermissionError(
                "Session is not authorized for actor, request origin, and action"
            )
        action_payload = canonical_authority_action_payload(
            action,
            command["payload"],
            account_id=account_id,
            environment=environment,
        )

        current = self.state_version
        expected = int(expected_raw)
        if expected != current:
            conflict = CommandResult(
                command_id=command_id,
                status="CONFLICT",
                state_version=str(current),
                reason_codes=("stale_state_version",),
            )
            try:
                stored, _ = self._journal.record_command(
                    command_id=command_id,
                    actor=actor,
                    environment=environment,
                    idempotency_key=idempotency_key,
                    request=dict(command),
                    result=self._result_dict(conflict),
                    state_version=current,
                )
            except ValueError as error:
                return CommandResult(
                    command_id=command_id,
                    status="CONFLICT",
                    state_version=str(current),
                    reason_codes=(self._conflict_reason(error),),
                )
            return self._command_result(stored)

        operation_id = str(
            uuid5(NAMESPACE_URL, f"https://operations.autotrade.local/{command_id}")
        )
        next_version = current + 1
        result = CommandResult(
            command_id=command_id,
            status="ACCEPTED",
            state_version=str(next_version),
            operation_id=operation_id,
        )
        event_id = str(
            uuid5(NAMESPACE_URL, f"https://events.autotrade.local/command/{command_id}")
        )
        operation_time = self._now()
        envelope = self._event_envelope(
            event_id=event_id,
            event_type="COMMAND_ACCEPTED",
            aggregate_version=next_version,
            payload={
                "command_id": command_id,
                "operation_id": operation_id,
                "action": action,
                "actor": actor,
                "account_id": account_id,
                "environment": environment,
                "action_payload": action_payload,
                "action_payload_hash": payload_digest(action_payload),
                "phase": "QUEUED",
                "started_at": operation_time,
                "updated_at": operation_time,
                "affected_refs": [],
                "evidence": [],
                "remaining_uncertainty": ["financial_outcome_not_completed"],
            },
        )
        try:
            stored, inserted, _ = self._journal.commit_command(
                command_id=command_id,
                actor=actor,
                environment=environment,
                idempotency_key=idempotency_key,
                request=dict(command),
                result=self._result_dict(result),
                state_version=next_version,
                events=[(envelope, "ui.host-events")],
            )
        except ValueError as error:
            return CommandResult(
                command_id=command_id,
                status="CONFLICT",
                state_version=str(self.state_version),
                reason_codes=(self._conflict_reason(error),),
            )

        returned = self._command_result(stored)
        if inserted:
            return returned

        # A retry that reached the journal after the state advanced returns the
        # original durable result, not a newly fabricated stale-state conflict.
        return returned

    def _accepted_command_event(
        self,
        event: Mapping[str, object],
        *,
        require_action_payload: bool,
    ) -> dict[str, object]:
        payload = event.get("payload")
        if not isinstance(payload, Mapping):
            raise ValueError("Host journal event payload must be an object")
        account_id = self._required_text(payload, "account_id")
        environment = self._required_text(payload, "environment")
        if account_id != self.account_id or environment != self.environment:
            raise ValueError(
                "Host journal command scope does not match active host account/environment"
            )
        action = canonical_host_action(payload.get("action"))
        raw_action_payload = payload.get("action_payload")
        raw_action_payload_hash = payload.get("action_payload_hash")
        if raw_action_payload is None and raw_action_payload_hash is None:
            if require_action_payload:
                raise ValueError(
                    "Accepted host command lacks durable canonical action payload"
                )
            action_payload = None
            action_payload_hash = None
        else:
            if not isinstance(raw_action_payload, Mapping):
                raise ValueError(
                    "Host journal action_payload must be an object"
                )
            if not isinstance(raw_action_payload_hash, str):
                raise ValueError(
                    "Host journal action_payload_hash must be text"
                )
            action_payload = canonical_authority_action_payload(
                action,
                raw_action_payload,
                account_id=account_id,
                environment=environment,
            )
            if dict(raw_action_payload) != action_payload:
                raise ValueError(
                    "Host journal action payload is not canonical"
                )
            action_payload_hash = payload_digest(action_payload)
            if action_payload_hash != raw_action_payload_hash:
                raise ValueError(
                    "Host journal action payload integrity failure"
                )
        return {
            "command_id": self._required_text(payload, "command_id"),
            "operation_id": self._required_text(payload, "operation_id"),
            "action": action,
            "actor": self._required_text(payload, "actor"),
            "account_id": account_id,
            "environment": environment,
            "started_at": str(payload.get("started_at") or event["committed_at"]),
            "action_payload": action_payload,
            "action_payload_hash": action_payload_hash,
        }

    def get_accepted_command(self, operation_id: str) -> dict[str, object]:
        if not isinstance(operation_id, str) or not operation_id.strip():
            raise ValueError("operation_id must be a non-empty string")
        matches = []
        for event in self._events():
            if event["event_type"] != "COMMAND_ACCEPTED":
                continue
            accepted = self._accepted_command_event(
                event,
                require_action_payload=False,
            )
            if accepted["operation_id"] == operation_id:
                matches.append((event, accepted))
        if len(matches) != 1:
            if not matches:
                raise KeyError("Unknown accepted operation")
            raise ValueError(
                "Host journal contains duplicate COMMAND_ACCEPTED operation identity"
            )
        event, _ = matches[0]
        return self._accepted_command_event(
            event,
            require_action_payload=True,
        )

    def _operation_projection(self) -> dict[str, OperationResult]:
        operations: dict[str, OperationResult] = {}
        for event in self._events():
            payload = event["payload"]
            if not isinstance(payload, Mapping):
                raise ValueError("Host journal event payload must be an object")
            if event["event_type"] == "COMMAND_ACCEPTED":
                accepted = self._accepted_command_event(
                    event,
                    require_action_payload=False,
                )
                operation_id = str(accepted["operation_id"])
                if operation_id in operations:
                    raise ValueError(
                        "Host journal contains duplicate COMMAND_ACCEPTED operation identity"
                    )
                phase = str(payload.get("phase", "QUEUED"))
                if phase != "QUEUED":
                    raise ValueError(
                        "COMMAND_ACCEPTED must create a QUEUED operation"
                    )
                uncertainty = self._replay_text_array(
                    payload,
                    "remaining_uncertainty",
                )
                if not uncertainty:
                    raise ValueError(
                        "Queued operation must preserve financial uncertainty"
                    )
                started_at = str(payload.get("started_at") or event["committed_at"])
                updated_at = str(payload.get("updated_at") or started_at)
                affected_refs = self._replay_text_array(payload, "affected_refs")
                evidence = self._replay_evidence(payload)
                operations[operation_id] = OperationResult(
                    operation_id=operation_id,
                    phase=phase,
                    started_at=started_at,
                    updated_at=updated_at,
                    state_version=str(event["aggregate_version"]),
                    affected_refs=affected_refs,
                    evidence=evidence,
                    remaining_uncertainty=uncertainty,
                )
            elif event["event_type"] == "OPERATION_UPDATED":
                operation_id = self._required_text(payload, "operation_id")
                current = operations.get(operation_id)
                if current is None:
                    raise ValueError(
                        "OPERATION_UPDATED cannot precede COMMAND_ACCEPTED"
                    )
                phase = self._required_text(payload, "phase")
                if phase not in self.UPDATE_PHASES:
                    raise ValueError("Host journal contains unsupported operation phase")
                uncertainty = self._replay_text_array(
                    payload,
                    "remaining_uncertainty",
                )
                if current.phase in self.TERMINAL_PHASES:
                    raise ValueError(
                        "Host journal rewrites a terminal operation"
                    )
                if current.phase == "UNKNOWN" and phase not in self.TERMINAL_PHASES:
                    raise ValueError(
                        "UNKNOWN journal operation can only resolve terminally"
                    )
                if phase == "UNKNOWN" and not uncertainty:
                    raise ValueError(
                        "UNKNOWN journal operation must preserve uncertainty"
                    )
                if phase in self.TERMINAL_PHASES and uncertainty:
                    raise ValueError(
                        "Terminal journal operation cannot retain uncertainty"
                    )
                affected_refs = self._replay_text_array(
                    payload,
                    "affected_refs",
                    default=current.affected_refs,
                )
                evidence = self._replay_evidence(
                    payload,
                    default=current.evidence,
                )
                operations[operation_id] = OperationResult(
                    operation_id=operation_id,
                    phase=phase,
                    started_at=current.started_at,
                    updated_at=str(payload.get("updated_at") or event["committed_at"]),
                    state_version=str(event["aggregate_version"]),
                    affected_refs=affected_refs,
                    evidence=evidence,
                    remaining_uncertainty=uncertainty,
                )
        return operations

    def update_operation(
        self,
        operation_id: str,
        phase: str,
        *,
        remaining_uncertainty: tuple[str, ...] = (),
        affected_refs: tuple[str, ...] | None = None,
        evidence: tuple[Mapping[str, object], ...] | None = None,
    ) -> OperationResult:
        if not isinstance(operation_id, str) or not operation_id.strip():
            raise ValueError("operation_id must be a non-empty string")
        if phase not in self.UPDATE_PHASES:
            raise ValueError("Unsupported operation phase")
        operations = self._operation_projection()
        current = operations.get(operation_id)
        if current is None:
            raise KeyError("Unknown operation")

        if any(
            not isinstance(item, str) or not item.strip()
            for item in remaining_uncertainty
        ):
            raise ValueError("remaining_uncertainty must contain non-empty strings")
        normalized_uncertainty = tuple(remaining_uncertainty)
        normalized_refs = (
            current.affected_refs
            if affected_refs is None
            else self._normalize_refs(affected_refs)
        )
        normalized_evidence = (
            current.evidence
            if evidence is None
            else self._normalize_evidence(evidence)
        )
        if (
            current.phase == phase
            and current.remaining_uncertainty == normalized_uncertainty
            and current.affected_refs == normalized_refs
            and current.evidence == normalized_evidence
        ):
            return current
        if current.phase in self.TERMINAL_PHASES:
            raise ValueError("Terminal operation cannot transition again")
        if current.phase == "UNKNOWN" and phase not in self.TERMINAL_PHASES:
            raise ValueError("UNKNOWN operation can only resolve to a terminal outcome")
        if phase == "UNKNOWN" and not normalized_uncertainty:
            raise ValueError("UNKNOWN operation must preserve remaining uncertainty")
        if phase in self.TERMINAL_PHASES and normalized_uncertainty:
            raise ValueError("Terminal operation cannot retain unresolved uncertainty")

        next_version = self.state_version + 1
        updated_at = self._now()
        payload = {
            "operation_id": operation_id,
            "phase": phase,
            "started_at": current.started_at,
            "updated_at": updated_at,
            "affected_refs": list(normalized_refs),
            "evidence": [dict(item) for item in normalized_evidence],
            "remaining_uncertainty": list(normalized_uncertainty),
        }
        event_id = str(
            uuid5(
                NAMESPACE_URL,
                f"https://events.autotrade.local/operation/{operation_id}/{next_version}/{phase}",
            )
        )
        envelope = self._event_envelope(
            event_id=event_id,
            event_type="OPERATION_UPDATED",
            aggregate_version=next_version,
            payload=payload,
        )
        self._journal.append_event(envelope, outbox_topic="ui.host-events")
        return OperationResult(
            operation_id=operation_id,
            phase=phase,
            started_at=current.started_at,
            updated_at=updated_at,
            state_version=str(next_version),
            affected_refs=normalized_refs,
            evidence=normalized_evidence,
            remaining_uncertainty=normalized_uncertainty,
        )

    def get_operation(self, operation_id: str) -> OperationResult:
        try:
            return self._operation_projection()[operation_id]
        except KeyError as error:
            raise KeyError("Unknown operation") from error

    def snapshot(self) -> dict[str, object]:
        operations = self._operation_projection()
        return {
            "state_version": str(self.state_version),
            "event_cursor": str(self.cursor),
            "account_id": self.account_id,
            "environment": self.environment,
            "operations": {
                operation_id: operation.phase
                for operation_id, operation in operations.items()
            },
        }

    def events_after(self, after: str | int) -> tuple[HostEvent, ...]:
        try:
            cursor = int(after)
        except (TypeError, ValueError) as error:
            raise ValueError("Cursor must be an integer sequence") from error
        if cursor < 0:
            raise ValueError("Cursor must be non-negative")
        current = self.state_version
        if cursor > current:
            raise ValueError("Cursor is ahead of host state")

        durable = self._events()
        visible = durable[-self._max_events :]
        if visible:
            oldest = int(visible[0]["aggregate_version"])
            if cursor < oldest - 1:
                raise EventGap("Event cursor gap requires a fresh state snapshot")
        return tuple(
            HostEvent(
                cursor=int(event["aggregate_version"]),
                kind=str(event["event_type"]),
                state_version=int(event["aggregate_version"]),
                payload=dict(event["payload"]),
            )
            for event in visible
            if int(event["aggregate_version"]) > cursor
        )
