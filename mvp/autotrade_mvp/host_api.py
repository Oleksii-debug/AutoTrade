"""Authenticated, versioned host command state for the AutoTrade simulation surface.

The UI is never a financial source of truth. Command acceptance is distinct
from operation completion, retries are idempotent, state versions prevent lost
updates, and event cursors are resumable with explicit gap detection.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from typing import Callable, Mapping
from uuid import NAMESPACE_URL, uuid5

from contracts.bindings.python.common_scalars import is_valid_common_scalar

from .host_actions import canonical_host_action


class EventGap(RuntimeError):
    """Raised when a client cursor predates retained host events."""


def scoped_host_operation_id(
    *,
    account_id: str,
    environment: str,
    command_id: str,
) -> str:
    """Derive the canonical operation identity for one scoped host command."""

    if not isinstance(account_id, str) or not account_id.strip():
        raise ValueError("account_id must be a non-empty string")
    normalized_account = account_id.strip()
    if not is_valid_common_scalar("Environment", environment):
        raise ValueError("environment must be a canonical Environment")
    if not isinstance(command_id, str) or not command_id:
        raise ValueError("command_id must be a non-empty string")
    scope_material = json.dumps(
        {
            "account_id": normalized_account,
            "environment": environment,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    scope_digest = sha256(scope_material).hexdigest()
    return str(
        uuid5(
            NAMESPACE_URL,
            "https://operations.autotrade.local/host/"
            f"{scope_digest}/{command_id}",
        )
    )


@dataclass(frozen=True)
class CommandResult:
    command_id: str
    status: str
    state_version: str
    reason_codes: tuple[str, ...] = ()
    field_errors: tuple[Mapping[str, object], ...] = ()
    operation_id: str | None = None
    current_value_ref: str | None = None


@dataclass(frozen=True)
class OperationResult:
    operation_id: str
    phase: str
    started_at: str
    updated_at: str
    state_version: str
    affected_refs: tuple[str, ...] = ()
    evidence: tuple[Mapping[str, object], ...] = ()
    remaining_uncertainty: tuple[str, ...] = ()


def command_result_payload(result: CommandResult) -> dict[str, object]:
    """Serialize exactly the canonical CommandResult contract."""

    payload: dict[str, object] = {
        "command_id": result.command_id,
        "status": result.status,
        "state_version": result.state_version,
        "reason_codes": list(result.reason_codes),
        "field_errors": [dict(item) for item in result.field_errors],
    }
    if result.operation_id is not None:
        payload["operation_id"] = result.operation_id
    if result.current_value_ref is not None:
        payload["current_value_ref"] = result.current_value_ref
    return payload


def operation_result_payload(result: OperationResult) -> dict[str, object]:
    """Serialize exactly the canonical OperationResult contract."""

    return {
        "operation_id": result.operation_id,
        "phase": result.phase,
        "started_at": result.started_at,
        "updated_at": result.updated_at,
        "affected_refs": list(result.affected_refs),
        "evidence": [dict(item) for item in result.evidence],
        "remaining_uncertainty": list(result.remaining_uncertainty),
    }


@dataclass(frozen=True)
class HostEvent:
    cursor: int
    kind: str
    state_version: int
    payload: Mapping[str, object]


class HostCommandStore:
    """Small in-memory analogue for canonical host/API command semantics."""

    TERMINAL_PHASES = {"SUCCEEDED", "FAILED", "CANCELLED"}
    UPDATE_PHASES = {"RUNNING", "WAITING_EXTERNAL", "UNKNOWN", *TERMINAL_PHASES}

    def __init__(
        self,
        *,
        account_id: str,
        environment: str,
        session_validator: Callable[[str, str, str, str], bool],
        request_origin_provider: Callable[[], str],
        max_events: int = 100,
        now: Callable[[], str] | None = None,
    ) -> None:
        if not isinstance(account_id, str) or not account_id.strip():
            raise ValueError("account_id must be a non-empty string")
        if not is_valid_common_scalar("Environment", environment):
            raise ValueError("environment must be a canonical Environment")
        if not callable(session_validator):
            raise TypeError("session_validator must be callable")
        if not callable(request_origin_provider):
            raise TypeError("request_origin_provider must be callable")
        if max_events < 1:
            raise ValueError("max_events must be positive")
        self.account_id = account_id.strip()
        self.environment = environment
        self._session_validator = session_validator
        self._request_origin_provider = request_origin_provider
        self._max_events = max_events
        self._now = now or (
            lambda: datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        )
        self.state_version = 0
        self.cursor = 0
        self._events: list[HostEvent] = []
        self._idempotency: dict[
            tuple[str, str, str], tuple[str, CommandResult]
        ] = {}
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

    @staticmethod
    def _normalize_refs(values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not isinstance(item, str) or not item.strip() for item in values):
            raise ValueError("affected_refs must contain non-empty strings")
        return tuple(values)

    @staticmethod
    def _normalize_evidence(
        values: tuple[Mapping[str, object], ...],
    ) -> tuple[Mapping[str, object], ...]:
        if any(not isinstance(item, Mapping) for item in values):
            raise ValueError("operation evidence must contain objects")
        return tuple(dict(item) for item in values)

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

        digest = self._digest(command)
        scope_key = (actor, environment, idempotency_key)
        previous = self._idempotency.get(scope_key)
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
            self._idempotency[scope_key] = (digest, result)
            self._commands[command_id] = digest
            return result

        operation_id = scoped_host_operation_id(
            account_id=self.account_id,
            environment=self.environment,
            command_id=command_id,
        )
        self.state_version += 1
        now = self._now()
        operation = OperationResult(
            operation_id=operation_id,
            phase="QUEUED",
            started_at=now,
            updated_at=now,
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
                "account_id": account_id,
                "environment": environment,
                **operation_result_payload(operation),
            },
        )
        result = CommandResult(
            command_id=command_id,
            status="ACCEPTED",
            state_version=str(self.state_version),
            operation_id=operation_id,
        )
        self._idempotency[scope_key] = (digest, result)
        self._commands[command_id] = digest
        return result

    def update_operation(
        self,
        operation_id: str,
        phase: str,
        *,
        remaining_uncertainty: tuple[str, ...] = (),
        affected_refs: tuple[str, ...] | None = None,
        evidence: tuple[Mapping[str, object], ...] | None = None,
    ) -> OperationResult:
        current = self._operations.get(operation_id)
        if current is None:
            raise KeyError("Unknown operation")
        if phase not in self.UPDATE_PHASES:
            raise ValueError("Unsupported operation phase")
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
            started_at=current.started_at,
            updated_at=self._now(),
            state_version=str(self.state_version),
            affected_refs=normalized_refs,
            evidence=normalized_evidence,
            remaining_uncertainty=normalized_uncertainty,
        )
        self._operations[operation_id] = updated
        self._emit(
            "OPERATION_UPDATED",
            operation_result_payload(updated),
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
            "account_id": self.account_id,
            "environment": self.environment,
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
