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

from .host_api import CommandResult, EventGap, HostEvent, OperationResult
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
        session_validator: Callable[[str, str], bool],
        max_events: int = 100,
        now: Callable[[], str] | None = None,
    ) -> None:
        if not isinstance(journal, JournalStore):
            raise TypeError("journal must be a JournalStore")
        if not callable(session_validator):
            raise TypeError("session_validator must be callable")
        if not isinstance(max_events, int) or isinstance(max_events, bool) or max_events < 1:
            raise ValueError("max_events must be positive")
        self._journal = journal
        self._session_validator = session_validator
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
            operation_id=(
                None
                if value.get("operation_id") is None
                else str(value["operation_id"])
            ),
        )

    @staticmethod
    def _result_dict(result: CommandResult) -> dict[str, object]:
        return {
            "command_id": result.command_id,
            "status": result.status,
            "state_version": result.state_version,
            "reason_codes": list(result.reason_codes),
            "operation_id": result.operation_id,
        }

    def _events(self) -> list[dict[str, object]]:
        return self._journal.load_events(self.AGGREGATE_TYPE, self.AGGREGATE_ID)

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
            "aggregate_version": aggregate_version,
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
        raise error

    def submit(self, command: Mapping[str, object]) -> CommandResult:
        if not isinstance(command, Mapping):
            raise TypeError("command must be a mapping")
        command_id = self._required_text(command, "command_id")
        idempotency_key = self._required_text(command, "idempotency_key")
        actor = self._required_text(command, "actor")
        session = self._required_text(command, "session")
        action = self._required_text(command, "action")
        expected_raw = self._required_text(command, "expected_state_version")
        if "payload" not in command or not isinstance(command["payload"], dict):
            raise ValueError("payload must be an object")
        if not expected_raw.isdigit():
            raise ValueError("expected_state_version must be a sequence")
        if not self._session_validator(session, actor):
            raise PermissionError("Session is not authorized for actor")

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
        envelope = self._event_envelope(
            event_id=event_id,
            event_type="COMMAND_ACCEPTED",
            aggregate_version=next_version,
            payload={
                "command_id": command_id,
                "operation_id": operation_id,
                "action": action,
                "actor": actor,
                "phase": "QUEUED",
                "remaining_uncertainty": ["financial_outcome_not_completed"],
            },
        )
        try:
            stored, inserted, _ = self._journal.commit_command(
                command_id=command_id,
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

    def _operation_projection(self) -> dict[str, OperationResult]:
        operations: dict[str, OperationResult] = {}
        for event in self._events():
            payload = event["payload"]
            if event["event_type"] == "COMMAND_ACCEPTED":
                operation_id = str(payload["operation_id"])
                operations[operation_id] = OperationResult(
                    operation_id=operation_id,
                    phase=str(payload.get("phase", "QUEUED")),
                    state_version=str(event["aggregate_version"]),
                    remaining_uncertainty=tuple(
                        str(x)
                        for x in payload.get(
                            "remaining_uncertainty",
                            ["financial_outcome_not_completed"],
                        )
                    ),
                )
            elif event["event_type"] == "OPERATION_UPDATED":
                operation_id = str(payload["operation_id"])
                operations[operation_id] = OperationResult(
                    operation_id=operation_id,
                    phase=str(payload["phase"]),
                    state_version=str(event["aggregate_version"]),
                    remaining_uncertainty=tuple(
                        str(x)
                        for x in payload.get("remaining_uncertainty", ())
                    ),
                )
        return operations

    def update_operation(
        self,
        operation_id: str,
        phase: str,
        *,
        remaining_uncertainty: tuple[str, ...] = (),
    ) -> OperationResult:
        if not isinstance(operation_id, str) or not operation_id.strip():
            raise ValueError("operation_id must be a non-empty string")
        if phase not in self.UPDATE_PHASES:
            raise ValueError("Unsupported operation phase")
        operations = self._operation_projection()
        current = operations.get(operation_id)
        if current is None:
            raise KeyError("Unknown operation")

        normalized_uncertainty = tuple(str(x) for x in remaining_uncertainty)
        if (
            current.phase == phase
            and current.remaining_uncertainty == normalized_uncertainty
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
        payload = {
            "operation_id": operation_id,
            "phase": phase,
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
            state_version=str(next_version),
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
