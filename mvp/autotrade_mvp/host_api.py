"""Authenticated, versioned host command state for the AutoTrade simulation surface.

The UI is never a financial source of truth.  Command acceptance is distinct
from operation completion, retries are idempotent, state versions prevent lost
updates, and event cursors are resumable with explicit gap detection.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from typing import Callable, Mapping
from uuid import NAMESPACE_URL, uuid5


class EventGap(RuntimeError):
    """Raised when a client cursor predates retained host events."""


@dataclass(frozen=True)
class CommandResult:
    command_id: str
    status: str
    state_version: str
    reason_codes: tuple[str, ...] = ()
    operation_id: str | None = None


@dataclass(frozen=True)
class OperationResult:
    operation_id: str
    phase: str
    state_version: str
    remaining_uncertainty: tuple[str, ...] = ()


@dataclass(frozen=True)
class HostEvent:
    cursor: int
    kind: str
    state_version: int
    payload: Mapping[str, object]


class HostCommandStore:
    """Small durable-state analogue for host/API command semantics."""

    TERMINAL_PHASES = {"SUCCEEDED", "FAILED", "CANCELLED"}
    UPDATE_PHASES = {"RUNNING", "WAITING_EXTERNAL", "UNKNOWN", *TERMINAL_PHASES}

    def __init__(
        self,
        *,
        session_validator: Callable[[str, str], bool],
        max_events: int = 100,
    ) -> None:
        if max_events < 1:
            raise ValueError("max_events must be positive")
        self._session_validator = session_validator
        self._max_events = max_events
        self.state_version = 0
        self.cursor = 0
        self._events: list[HostEvent] = []
        self._idempotency: dict[str, tuple[str, CommandResult]] = {}
        self._commands: dict[str, str] = {}
        self._operations: dict[str, OperationResult] = {}

    @staticmethod
    def _digest(command: Mapping[str, object]) -> str:
        encoded = json.dumps(
            command,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        return sha256(encoded).hexdigest()

    @staticmethod
    def _required_text(command: Mapping[str, object], field: str) -> str:
        value = command.get(field)
        if not isinstance(value, str) or not value:
            raise ValueError(f"{field} must be a non-empty string")
        return value

    def _emit(self, kind: str, payload: Mapping[str, object]) -> HostEvent:
        self.cursor += 1
        event = HostEvent(
            cursor=self.cursor,
            kind=kind,
            state_version=self.state_version,
            payload=dict(payload),
        )
        self._events.append(event)
        if len(self._events) > self._max_events:
            self._events = self._events[-self._max_events :]
        return event

    def submit(self, command: Mapping[str, object]) -> CommandResult:
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

        digest = self._digest(command)
        previous = self._idempotency.get(idempotency_key)
        if previous is not None:
            previous_digest, previous_result = previous
            if previous_digest != digest:
                return CommandResult(
                    command_id=command_id,
                    status="CONFLICT",
                    state_version=str(self.state_version),
                    reason_codes=("idempotency_key_conflict",),
                )
            return previous_result

        previous_command_digest = self._commands.get(command_id)
        if previous_command_digest is not None and previous_command_digest != digest:
            return CommandResult(
                command_id=command_id,
                status="CONFLICT",
                state_version=str(self.state_version),
                reason_codes=("command_id_conflict",),
            )

        expected = int(expected_raw)
        if expected != self.state_version:
            result = CommandResult(
                command_id=command_id,
                status="CONFLICT",
                state_version=str(self.state_version),
                reason_codes=("stale_state_version",),
            )
            self._idempotency[idempotency_key] = (digest, result)
            self._commands[command_id] = digest
            return result

        operation_id = str(
            uuid5(NAMESPACE_URL, f"https://operations.autotrade.local/{command_id}")
        )
        self.state_version += 1
        operation = OperationResult(
            operation_id=operation_id,
            phase="QUEUED",
            state_version=str(self.state_version),
            remaining_uncertainty=("financial_outcome_not_completed",),
        )
        self._operations[operation_id] = operation
        self._emit(
            "COMMAND_ACCEPTED",
            {
                "command_id": command_id,
                "operation_id": operation_id,
                "action": action,
                "actor": actor,
            },
        )
        result = CommandResult(
            command_id=command_id,
            status="ACCEPTED",
            state_version=str(self.state_version),
            operation_id=operation_id,
        )
        self._idempotency[idempotency_key] = (digest, result)
        self._commands[command_id] = digest
        return result

    def update_operation(
        self,
        operation_id: str,
        phase: str,
        *,
        remaining_uncertainty: tuple[str, ...] = (),
    ) -> OperationResult:
        current = self._operations.get(operation_id)
        if current is None:
            raise KeyError("Unknown operation")
        if phase not in self.UPDATE_PHASES:
            raise ValueError("Unsupported operation phase")
        normalized_uncertainty = tuple(str(x) for x in remaining_uncertainty)
        if current.phase in self.TERMINAL_PHASES:
            raise ValueError("Terminal operation cannot transition again")
        if current.phase == "UNKNOWN" and phase not in self.TERMINAL_PHASES:
            raise ValueError("UNKNOWN operation can only resolve to a terminal outcome")
        if phase == "UNKNOWN" and not normalized_uncertainty:
            raise ValueError("UNKNOWN operation must preserve remaining uncertainty")
        if phase in self.TERMINAL_PHASES and normalized_uncertainty:
            raise ValueError("Terminal operation cannot retain unresolved uncertainty")
        self.state_version += 1
        updated = OperationResult(
            operation_id=operation_id,
            phase=phase,
            state_version=str(self.state_version),
            remaining_uncertainty=normalized_uncertainty,
        )
        self._operations[operation_id] = updated
        self._emit(
            "OPERATION_UPDATED",
            {
                "operation_id": operation_id,
                "phase": phase,
                "remaining_uncertainty": list(normalized_uncertainty),
            },
        )
        return updated

    def get_operation(self, operation_id: str) -> OperationResult:
        try:
            return self._operations[operation_id]
        except KeyError as error:
            raise KeyError("Unknown operation") from error

    def snapshot(self) -> dict[str, object]:
        return {
            "state_version": str(self.state_version),
            "event_cursor": str(self.cursor),
            "operations": {
                operation_id: operation.phase
                for operation_id, operation in self._operations.items()
            },
        }

    def events_after(self, after: str | int) -> tuple[HostEvent, ...]:
        try:
            cursor = int(after)
        except (TypeError, ValueError) as error:
            raise ValueError("Cursor must be an integer sequence") from error
        if cursor < 0:
            raise ValueError("Cursor must be non-negative")
        if cursor > self.cursor:
            raise ValueError("Cursor is ahead of host state")
        if self._events:
            oldest = self._events[0].cursor
            if cursor < oldest - 1:
                raise EventGap("Event cursor gap requires a fresh state snapshot")
        return tuple(event for event in self._events if event.cursor > cursor)
