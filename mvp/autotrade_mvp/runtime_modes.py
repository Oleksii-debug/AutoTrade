"""Durable product runtime modes over the canonical journal.

The mode is an admission boundary, not a second trading engine.  Existing
simulation, risk, OMS, accounting, learning and diagnostics remain authoritative;
this module only records which of those existing paths may be entered.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from uuid import NAMESPACE_URL, UUID, uuid5

from .persistence import JournalStore, payload_digest


RUNTIME_MODES = frozenset({
    "RUNNING", "PAUSED", "RESEARCH_ONLY", "RECOVERY", "STOPPED",
})
RUNTIME_MODE_ACTION = "SET_RUNTIME_MODE"
_AGGREGATE_TYPE = "product_runtime_mode"
_AGGREGATE_ID = "canonical"
_INITIAL_COMMAND_ID = str(uuid5(NAMESPACE_URL, "autotrade:product-runtime-mode:initial"))


@dataclass(frozen=True)
class RuntimeModeResult:
    affected_refs: tuple[str, ...]
    evidence: tuple[Mapping[str, object], ...]


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError(name + " must be canonical non-empty text")
    return value


def _mode(value: object) -> str:
    value = _text(value, "runtime mode")
    if value not in RUNTIME_MODES:
        raise ValueError("runtime mode is not supported")
    return value


def _events(journal: JournalStore) -> list[dict]:
    if not isinstance(journal, JournalStore):
        raise TypeError("journal must be a JournalStore")
    events = journal.load_events(_AGGREGATE_TYPE, _AGGREGATE_ID)
    for version, event in enumerate(events, start=1):
        payload = event.get("payload")
        if (
            event.get("aggregate_version") != version
            or event.get("aggregate_type") != _AGGREGATE_TYPE
            or event.get("aggregate_id") != _AGGREGATE_ID
            or event.get("payload_hash") != payload_digest(payload)
            or type(payload) is not dict
            or set(payload) != {
                "schema_version", "command_id", "previous_mode", "mode",
                "transitioned_at", "checkpoint",
            }
            or payload.get("schema_version") != 1
        ):
            raise ValueError("runtime mode journal is malformed")
        _mode(payload.get("mode"))
        previous = payload.get("previous_mode")
        if previous is not None:
            _mode(previous)
        if version == 1 and previous is not None:
            raise ValueError("initial runtime mode cannot name a predecessor")
        if version > 1 and previous != events[version - 2]["payload"]["mode"]:
            raise ValueError("runtime mode history is not contiguous")
        command_id = _text(payload.get("command_id"), "runtime mode command_id")
        if str(UUID(command_id)) != command_id:
            raise ValueError("runtime mode command_id is not canonical")
        _text(payload.get("transitioned_at"), "runtime mode transitioned_at")
        checkpoint = payload.get("checkpoint")
        if type(checkpoint) is not dict or set(checkpoint) != {
            "journal_sequence_before", "simulation_status", "simulation_replay_verified",
            "learning_status", "learning_state_sha256", "learning_episodes",
            "strategy_event_count", "strategy_state_sha256", "diagnostics_preserved",
            "memory_preserved", "strategies_preserved", "new_trade_decisions_allowed",
            "order_submission_allowed", "research_allowed",
        }:
            raise ValueError("runtime mode checkpoint is malformed")
    return events


def current_runtime_mode(journal: JournalStore) -> dict[str, object]:
    events = _events(journal)
    if not events:
        return {
            "mode": None,
            "version": 0,
            "transitioned_at": None,
            "checkpoint": None,
        }
    payload = events[-1]["payload"]
    return {
        "mode": payload["mode"],
        "version": len(events),
        "transitioned_at": payload["transitioned_at"],
        "checkpoint": dict(payload["checkpoint"]),
    }


def _checkpoint(journal: JournalStore, target: str) -> dict[str, object]:
    root = Path(journal.path).parent
    try:
        from .simulation_status import inspect_canonical_simulation
        inspected = inspect_canonical_simulation(root)
        simulation = inspected.get("status", {})
        simulation_status = simulation.get("session_status", "UNKNOWN")
        replay_verified = simulation.get("replay_verified") is True
    except (OSError, TypeError, ValueError):
        simulation_status = "UNKNOWN"
        replay_verified = False

    from .live_learning_status import read_whitebit_live_learning_status
    learning = read_whitebit_live_learning_status(root.parent / "whitebit-live-zero-learning")
    learning_status = str(learning.get("status", "CORRUPT"))
    episodes = learning.get("episodes")
    if type(episodes) is not int:
        episodes = None
    learning_hash = payload_digest(learning)

    strategy_events = journal.load_events_by_aggregate_type("simulation_agent_decision")
    strategy_payloads = [event.get("payload") for event in strategy_events]
    return {
        "journal_sequence_before": str(journal.current_journal_sequence()),
        "simulation_status": simulation_status,
        "simulation_replay_verified": replay_verified,
        "learning_status": learning_status,
        "learning_state_sha256": learning_hash,
        "learning_episodes": episodes,
        "strategy_event_count": len(strategy_events),
        "strategy_state_sha256": payload_digest(strategy_payloads),
        "diagnostics_preserved": True,
        "memory_preserved": True,
        "strategies_preserved": True,
        "new_trade_decisions_allowed": target == "RUNNING",
        "order_submission_allowed": target == "RUNNING",
        "research_allowed": target in {"RUNNING", "PAUSED", "RESEARCH_ONLY"},
    }


def _event_evidence(event: Mapping[str, object]) -> dict[str, object]:
    return {
        "kind": "product-runtime-mode-transition",
        "event_id": event["event_id"],
        "aggregate_type": _AGGREGATE_TYPE,
        "aggregate_id": _AGGREGATE_ID,
        "aggregate_version": str(event["aggregate_version"]),
        "payload_hash": event["payload_hash"],
        "journal_sequence": str(event["journal_sequence"]),
    }


def _result(event: Mapping[str, object]) -> RuntimeModeResult:
    return RuntimeModeResult(
        ("product-runtime-mode:canonical",),
        (_event_evidence(event),),
    )


def ensure_runtime_mode(
    journal: JournalStore,
    *,
    account_id: str,
    environment: str,
    transitioned_at: str,
) -> dict[str, object]:
    del account_id, environment
    events = _events(journal)
    if events:
        return current_runtime_mode(journal)
    payload = {
        "schema_version": 1,
        "command_id": _INITIAL_COMMAND_ID,
        "previous_mode": None,
        # Preserve the pre-mode product's admitted simulation behavior on
        # upgrade. Operators can immediately select a stricter durable mode.
        "mode": "RUNNING",
        "transitioned_at": _text(transitioned_at, "transitioned_at"),
        "checkpoint": _checkpoint(journal, "RUNNING"),
    }
    event = {
        "event_id": _INITIAL_COMMAND_ID,
        "event_type": "ProductRuntimeModeInitialized",
        "aggregate_type": _AGGREGATE_TYPE,
        "aggregate_id": _AGGREGATE_ID,
        "aggregate_version": "1",
        "payload": payload,
        "payload_hash": payload_digest(payload),
        "committed_at": payload["transitioned_at"],
    }
    journal.append_event(event)
    return current_runtime_mode(journal)


def canonical_runtime_mode_payload(
    journal: JournalStore,
    raw: Mapping[str, object],
    command_id: str,
    account_id: str,
    environment: str,
) -> dict[str, object]:
    if type(raw) is not dict or set(raw) != {"mode"}:
        raise ValueError("SET_RUNTIME_MODE requires exactly one mode")
    state = current_runtime_mode(journal)
    if state["mode"] is None:
        raise ValueError("product runtime mode is not initialized")
    command_id = _text(command_id, "command_id")
    if str(UUID(command_id)) != command_id:
        raise ValueError("command_id is not canonical")
    return {
        "schema_version": 1,
        "command_id": command_id,
        "account_id": _text(account_id, "account_id"),
        "environment": _text(environment, "environment"),
        "expected_mode": state["mode"],
        "expected_mode_version": state["version"],
        "mode": _mode(raw["mode"]),
    }


def validate_runtime_mode_payload(
    payload: object,
    digest: object,
    account_id: str,
    environment: str,
) -> dict[str, object]:
    if type(payload) is not dict or set(payload) != {
        "schema_version", "command_id", "account_id", "environment",
        "expected_mode", "expected_mode_version", "mode",
    } or payload_digest(payload) != digest:
        raise ValueError("persisted runtime mode payload differs")
    if payload.get("schema_version") != 1:
        raise ValueError("runtime mode payload schema differs")
    if payload.get("account_id") != account_id or payload.get("environment") != environment:
        raise ValueError("runtime mode payload scope differs")
    _mode(payload.get("expected_mode"))
    _mode(payload.get("mode"))
    if type(payload.get("expected_mode_version")) is not int or payload["expected_mode_version"] < 1:
        raise ValueError("runtime mode payload version differs")
    command_id = _text(payload.get("command_id"), "command_id")
    if str(UUID(command_id)) != command_id:
        raise ValueError("runtime mode command_id is not canonical")
    return dict(payload)


def resolve_runtime_mode_action(
    journal: JournalStore, payload: Mapping[str, object], accepted_at: str,
) -> RuntimeModeResult | None:
    event_id = str(uuid5(NAMESPACE_URL, "autotrade:product-runtime-mode:" + str(payload["command_id"])))
    matches = [event for event in _events(journal) if event.get("event_id") == event_id]
    if not matches:
        return None
    if len(matches) != 1:
        raise ValueError("runtime mode transition is duplicated")
    event = matches[0]
    body = event["payload"]
    if (
        body.get("command_id") != payload.get("command_id")
        or body.get("previous_mode") != payload.get("expected_mode")
        or body.get("mode") != payload.get("mode")
        or body.get("transitioned_at") != accepted_at
        or event.get("aggregate_version") != payload.get("expected_mode_version") + 1
    ):
        raise ValueError("runtime mode transition conflicts with accepted command")
    return _result(event)


def execute_runtime_mode_action(
    journal: JournalStore, payload: Mapping[str, object], accepted_at: str,
) -> RuntimeModeResult:
    resolved = resolve_runtime_mode_action(journal, payload, accepted_at)
    if resolved is not None:
        return resolved
    state = current_runtime_mode(journal)
    if (
        state["mode"] != payload["expected_mode"]
        or state["version"] != payload["expected_mode_version"]
    ):
        raise ValueError("runtime mode changed after command acceptance")
    target = str(payload["mode"])
    checkpoint = _checkpoint(journal, target)
    if target == "RUNNING" and state["mode"] == "RECOVERY" and (
        checkpoint["simulation_status"] == "UNKNOWN"
        or checkpoint["simulation_replay_verified"] is not True
    ):
        raise ValueError("runtime recovery must reconcile before RUNNING")
    event_id = str(uuid5(NAMESPACE_URL, "autotrade:product-runtime-mode:" + str(payload["command_id"])))
    body = {
        "schema_version": 1,
        "command_id": payload["command_id"],
        "previous_mode": state["mode"],
        "mode": target,
        "transitioned_at": _text(accepted_at, "accepted_at"),
        "checkpoint": checkpoint,
    }
    event = {
        "event_id": event_id,
        "event_type": "ProductRuntimeModeChanged",
        "aggregate_type": _AGGREGATE_TYPE,
        "aggregate_id": _AGGREGATE_ID,
        "aggregate_version": str(state["version"] + 1),
        "payload": body,
        "payload_hash": payload_digest(body),
        "committed_at": body["transitioned_at"],
    }
    journal.append_event(event)
    committed = next(item for item in _events(journal) if item["event_id"] == event_id)
    return _result(committed)


def assert_runtime_action_allowed(journal: JournalStore, action: str) -> None:
    state = current_runtime_mode(journal)
    mode = state["mode"]
    if mode is None:  # Legacy/non-product harnesses have no product-mode authority.
        return
    if action == "START_SIMULATION" and mode != "RUNNING":
        raise ValueError("new trade decisions require RUNNING mode")


def assert_order_submission_allowed(journal: JournalStore) -> None:
    state = current_runtime_mode(journal)
    if state["mode"] != "RUNNING":
        raise PermissionError("order submission requires RUNNING mode")
