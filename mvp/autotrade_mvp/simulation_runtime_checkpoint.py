"""Product-owned whole-runtime checkpoint composition for autonomous ZERO simulation.

This module does not own financial state.  It binds the existing durable
JournalStore authorities and the frozen autonomous protocol into the generic
CompositeReplayCheckpoint envelope from replay.py.

The checkpoint is simulation/research evidence only.  It never grants provider,
PAPER, LIVE, release, or economic-edge authority.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import threading
from typing import Any, Mapping, Sequence

from .persistence import JournalStore, payload_digest
from .replay import (
    CausalReplay,
    CompositeReplayCheckpoint,
    ReplayError,
    ReplayEvent,
    RuntimeStateAuthority,
    RuntimeStateVerifier,
    resume_from_composite_checkpoint,
)


_CHECKPOINT_NAME = "autonomous-runtime-checkpoint.json"
_CHECKPOINT_TMP_NAME = ".autonomous-runtime-checkpoint.tmp"
_AUTHORITY_PREFIX = "autotrade-autonomous-runtime"
_VERIFIER_ID = "autotrade-autonomous-runtime-verifier-v1"
_SEAL_DOMAIN = b"AutoTrade/autonomous-runtime-checkpoint/v1\x00"
_COMPONENT_AGGREGATE_TYPES = (
    "account_reconciliation",
    "authority_state",
    "economic_book",
    "order_projection_book",
    "provider_activity",
    "reservation_book",
    "risk_decision",
    "risk_policy_registry",
    "submission_attempt",
    "valuation_observation",
)
_VERIFIER_LOCK = threading.RLock()
_VERIFIERS: dict[str, RuntimeStateVerifier] = {}


class AutonomousRuntimeCheckpointError(ValueError):
    """Raised when the autonomous runtime cut cannot be proven or restored."""


def _canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise AutonomousRuntimeCheckpointError(
            "autonomous runtime state is not canonical JSON"
        ) from error


def _digest(value: object) -> str:
    return sha256(_canonical_bytes(value)).hexdigest()


def _text(value: object, *, field: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise AutonomousRuntimeCheckpointError(
            f"{field} must be canonical non-empty text"
        )
    return value


def _utc(value: object, *, field: str) -> datetime:
    raw = _text(value, field=field)
    if not raw.endswith("Z"):
        raise AutonomousRuntimeCheckpointError(f"{field} must end in Z")
    try:
        parsed = datetime.fromisoformat(raw[:-1] + "+00:00")
    except ValueError as error:
        raise AutonomousRuntimeCheckpointError(
            f"{field} must be an ISO-8601 UTC timestamp"
        ) from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AutonomousRuntimeCheckpointError(
            f"{field} must include a UTC offset"
        )
    return parsed.astimezone(timezone.utc)


def _instant(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _protocol_identity(protocol: Mapping[str, object]) -> tuple[str, str, str]:
    if type(protocol) is not dict:
        raise TypeError("autonomous protocol must be an exact dictionary")
    run_id = _text(protocol.get("run_id"), field="run_id")
    source = _text(
        protocol.get("source_build_identity"),
        field="source_build_identity",
    )
    if not source.startswith("sha256:") or len(source) != 71:
        raise AutonomousRuntimeCheckpointError(
            "source_build_identity must be sha256:<64 lowercase hex>"
        )
    build_sha = source[7:]
    if any(character not in "0123456789abcdef" for character in build_sha):
        raise AutonomousRuntimeCheckpointError(
            "source_build_identity must be canonical lowercase hex"
        )
    protocol_digest = payload_digest(protocol)
    if (
        type(protocol_digest) is not str
        or not protocol_digest.startswith("sha256:")
        or len(protocol_digest) != 71
    ):
        raise AutonomousRuntimeCheckpointError(
            "canonical protocol digest is unavailable"
        )
    return run_id, build_sha, protocol_digest


def _events(protocol: Mapping[str, object]) -> tuple[ReplayEvent, ...]:
    prices = protocol.get("prices")
    if type(prices) is not list or not prices:
        raise AutonomousRuntimeCheckpointError(
            "autonomous protocol prices must be a non-empty exact list"
        )
    start = _utc(protocol.get("start_time"), field="start_time")
    source = _text(
        protocol.get("source_build_identity"),
        field="source_build_identity",
    )
    values: list[ReplayEvent] = []
    for index, raw_price in enumerate(prices):
        price = _text(raw_price, field=f"prices[{index}]")
        values.append(
            ReplayEvent(
                sequence=index,
                available_at=_instant(start + timedelta(seconds=index)),
                source_version=source,
                payload={"episode": index + 1, "price": price},
            )
        )
    return tuple(values)


def _replay(
    protocol: Mapping[str, object],
    *,
    completed_episodes: int,
) -> CausalReplay:
    events = _events(protocol)
    if (
        type(completed_episodes) is not int
        or completed_episodes < 0
        or completed_episodes > len(events)
    ):
        raise AutonomousRuntimeCheckpointError(
            "completed episode count is outside the frozen replay population"
        )
    first = _utc(events[0].available_at, field="first replay event")
    replay = CausalReplay(events, start_at=_instant(first - timedelta(microseconds=1)))
    if completed_episodes:
        visible = replay.advance_to(events[completed_episodes - 1].available_at)
        if len(visible) != completed_episodes:
            raise AutonomousRuntimeCheckpointError(
                "causal replay did not consume the exact completed prefix"
            )
    return replay


def _event_identity(event: Mapping[str, object]) -> dict[str, object]:
    if type(event) is not dict:
        raise AutonomousRuntimeCheckpointError(
            "durable journal event is not a canonical dictionary"
        )
    required = (
        "event_id",
        "event_type",
        "aggregate_type",
        "aggregate_id",
        "aggregate_version",
        "payload_hash",
        "committed_at",
    )
    result: dict[str, object] = {}
    for key in required:
        if key not in event:
            raise AutonomousRuntimeCheckpointError(
                f"durable journal event is missing {key}"
            )
        result[key] = event[key]
    if "journal_sequence" in event:
        result["journal_sequence"] = event["journal_sequence"]
    result["payload"] = event.get("payload")
    return result


def _aggregate_events(
    store: JournalStore,
    aggregate_type: str,
) -> list[dict[str, object]]:
    return [
        _event_identity(event)
        for event in JournalStore.load_events_by_aggregate_type(
            store,
            aggregate_type,
        )
    ]


def _stable_runtime_components(
    store: JournalStore,
    *,
    protocol: Mapping[str, object],
    completed: Sequence[Mapping[str, object]],
) -> tuple[str, dict[str, str]]:
    if type(store) is not JournalStore:
        raise TypeError("runtime checkpoint requires the canonical JournalStore")
    if type(completed) not in {list, tuple}:
        raise TypeError("completed autonomous episodes must be a list or tuple")

    run_id, _build_sha, protocol_digest = _protocol_identity(protocol)
    before = JournalStore.whole_store_state_cut(store)

    if JournalStore.pending_outbox_count(store) != 0:
        raise AutonomousRuntimeCheckpointError(
            "runtime checkpoint requires a fully delivered outbox cut"
        )

    loop_events = [
        _event_identity(event)
        for event in JournalStore.load_events(
            store,
            "canonical_autonomous_simulation",
            run_id,
        )
    ]
    authority_events = {
        aggregate_type: _aggregate_events(store, aggregate_type)
        for aggregate_type in _COMPONENT_AGGREGATE_TYPES
    }

    completed_values: list[dict[str, object]] = []
    for index, item in enumerate(completed):
        if type(item) is not dict:
            raise AutonomousRuntimeCheckpointError(
                "completed autonomous episode payload must be an exact dictionary"
            )
        if item.get("episode") != index + 1:
            raise AutonomousRuntimeCheckpointError(
                "completed autonomous episode chronology is not contiguous"
            )
        completed_values.append(dict(item))

    last = completed_values[-1] if completed_values else None
    financial_projection = (
        {
            "episode": last.get("episode"),
            "cash": last.get("cash"),
            "position": last.get("position"),
            "equity": last.get("equity"),
            "reconciled": last.get("reconciled"),
            "reconciliation_event_id": last.get("reconciliation_event_id"),
        }
        if last is not None
        else {
            "episode": 0,
            "cash": protocol.get("initial_cash"),
            "position": "0",
            "reconciled": True,
        }
    )
    provider_state = (
        last.get("provider_state")
        if last is not None
        else {
            "bootstrap": "simulated-provider-unstarted",
            "initial_cash": protocol.get("initial_cash"),
        }
    )

    common = {
        "cut": before,
        "run_id": run_id,
        "protocol_digest": protocol_digest,
        "completed_episodes": len(completed_values),
    }
    components = {
        "pending_event_queue": _digest(
            {
                **common,
                "remaining_prices": protocol["prices"][len(completed_values):],
                "next_episode": len(completed_values) + 1,
            }
        ),
        "rng_state": _digest(
            {
                **common,
                "rng_family": "NONE",
                "reason": "canonical autonomous ZERO loop is deterministic",
            }
        ),
        "strategy_state": _digest(
            {
                **common,
                "strategy": protocol.get("strategy"),
                "parameters": protocol.get("strategy_parameters"),
                "consumed_prices": protocol["prices"][:len(completed_values)],
                "completed_projection": [
                    {
                        key: value
                        for key, value in item.items()
                        if key != "provider_state"
                    }
                    for item in completed_values
                ],
            }
        ),
        "portfolio_accounting_state": _digest(
            {
                **common,
                "projection": financial_projection,
                "economic_events": authority_events["economic_book"],
                "provider_activity": authority_events["provider_activity"],
            }
        ),
        "execution_state": _digest(
            {
                **common,
                "orders": authority_events["order_projection_book"],
                "submissions": authority_events["submission_attempt"],
                "reservations": authority_events["reservation_book"],
                "authority": authority_events["authority_state"],
            }
        ),
        "accrual_state": _digest(
            {
                **common,
                "asset_class": protocol.get("instrument", {}).get("asset_class")
                if type(protocol.get("instrument")) is dict
                else None,
                "economics": {
                    "fee_rate": protocol.get("fee_rate"),
                    "financing": "NONE_IN_CANONICAL_CASH_EQUITY_ZERO_LOOP",
                },
            }
        ),
        "policy_state": _digest(
            {
                **common,
                "risk_policy": protocol.get("risk_policy"),
                "risk_policy_digest": protocol.get("risk_policy_digest"),
                "registrations": authority_events["risk_policy_registry"],
                "decisions": authority_events["risk_decision"],
            }
        ),
        "instrument_state": _digest(
            {
                **common,
                "instrument": protocol.get("instrument"),
                "valuations": authority_events["valuation_observation"],
            }
        ),
        "provider_state": _digest(
            {
                **common,
                "provider": protocol.get("provider"),
                "environment": protocol.get("environment"),
                "state": provider_state,
                "reconciliation": authority_events["account_reconciliation"],
            }
        ),
        "experiment_state": _digest(
            {
                **common,
                "source_build_identity": protocol.get("source_build_identity"),
                "loop_events": loop_events,
                "fault_at_episode": protocol.get("fault_at_episode"),
                "emergency_at_episode": protocol.get("emergency_at_episode"),
            }
        ),
    }

    after = JournalStore.whole_store_state_cut(store)
    if after != before:
        raise AutonomousRuntimeCheckpointError(
            "durable runtime state changed while checkpoint cut was captured"
        )
    cut_id = "sha256:" + _digest(
        {
            "kind": "autonomous-runtime-common-cut-v1",
            "cut": before,
            "components": components,
        }
    )
    return cut_id, components


def _sign_material(material: bytes) -> str:
    if type(material) is not bytes:
        raise TypeError("runtime checkpoint signing material must be bytes")
    return sha256(_SEAL_DOMAIN + material).hexdigest()


def _verify_material(material: bytes, signature: str) -> bool:
    if type(material) is not bytes or type(signature) is not str:
        return False
    return _sign_material(material) == signature


def _verifier(authority_id: str) -> RuntimeStateVerifier:
    with _VERIFIER_LOCK:
        existing = _VERIFIERS.get(authority_id)
        if existing is not None:
            return existing
        verifier = RuntimeStateVerifier.select_product_trust(
            authority_id=authority_id,
            verifier_id=_VERIFIER_ID,
            verify_signature=_verify_material,
        )
        _VERIFIERS[authority_id] = verifier
        return verifier


def _authority(
    *,
    store: JournalStore,
    protocol: Mapping[str, object],
    completed: Sequence[Mapping[str, object]],
    replay: CausalReplay,
) -> tuple[RuntimeStateAuthority, RuntimeStateVerifier]:
    run_id, _build_sha, protocol_digest = _protocol_identity(protocol)
    authority_id = f"{_AUTHORITY_PREFIX}:{run_id}:{protocol_digest}"

    def resolve_cut():
        cut_id, components = _stable_runtime_components(
            store,
            protocol=protocol,
            completed=completed,
        )
        return cut_id, replay.checkpoint(), components

    return (
        RuntimeStateAuthority(
            authority_id=authority_id,
            signer=_sign_material,
            cut_resolver=resolve_cut,
        ),
        _verifier(authority_id),
    )


def _expected_identity(
    protocol: Mapping[str, object],
) -> tuple[str, str]:
    _run_id, build_sha, protocol_digest = _protocol_identity(protocol)
    return build_sha, f"autonomous-simulation:{protocol_digest}"


def checkpoint_path(root: str | Path) -> Path:
    if not isinstance(root, (str, Path)):
        raise TypeError("runtime checkpoint root must be a path")
    return Path(root) / _CHECKPOINT_NAME


def build_autonomous_runtime_checkpoint(
    store: JournalStore,
    *,
    protocol: Mapping[str, object],
    completed: Sequence[Mapping[str, object]],
) -> CompositeReplayCheckpoint:
    replay = _replay(protocol, completed_episodes=len(completed))
    authority, verifier = _authority(
        store=store,
        protocol=protocol,
        completed=completed,
        replay=replay,
    )
    build_sha, protocol_ref = _expected_identity(protocol)
    return replay.composite_checkpoint(
        runtime_state_authority=authority,
        runtime_state_verifier=verifier,
        build_sha=build_sha,
        protocol_ref=protocol_ref,
    )


def persist_autonomous_runtime_checkpoint(
    root: str | Path,
    store: JournalStore,
    *,
    protocol: Mapping[str, object],
    completed: Sequence[Mapping[str, object]],
) -> CompositeReplayCheckpoint:
    if not completed:
        raise AutonomousRuntimeCheckpointError(
            "runtime checkpoint requires at least one completed episode"
        )
    checkpoint = build_autonomous_runtime_checkpoint(
        store,
        protocol=protocol,
        completed=completed,
    )
    destination = checkpoint_path(root)
    temporary = destination.with_name(_CHECKPOINT_TMP_NAME)
    data = checkpoint.to_canonical_json().encode("utf-8")
    try:
        with open(temporary, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        try:
            directory_fd = os.open(
                destination.parent,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
        except OSError:
            directory_fd = None
        if directory_fd is not None:
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    except OSError as error:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise AutonomousRuntimeCheckpointError(
            "autonomous runtime checkpoint could not be persisted"
        ) from error
    return checkpoint


def preflight_autonomous_runtime_checkpoint(
    root: str | Path,
    *,
    protocol: Mapping[str, object],
    completed_episodes: int,
) -> CompositeReplayCheckpoint:
    """Reject missing, malformed or causally stale checkpoint bytes before restore.

    This phase deliberately does not claim current financial/component equality;
    owner-specific projections are allowed to emit their more precise diagnoses
    before the later full common-cut verification.
    """

    if type(completed_episodes) is not int or completed_episodes < 1:
        raise AutonomousRuntimeCheckpointError(
            "runtime checkpoint preflight requires completed episodes"
        )
    path = checkpoint_path(root)
    try:
        document = path.read_text(encoding="utf-8")
    except OSError as error:
        raise AutonomousRuntimeCheckpointError(
            "completed autonomous state is missing its runtime checkpoint"
        ) from error
    try:
        checkpoint = CompositeReplayCheckpoint.from_canonical_json(document)
    except (ReplayError, TypeError, ValueError) as error:
        raise AutonomousRuntimeCheckpointError(
            "persisted autonomous runtime checkpoint is invalid"
        ) from error

    replay = _replay(protocol, completed_episodes=completed_episodes)
    build_sha, protocol_ref = _expected_identity(protocol)
    if (
        checkpoint.replay != replay.checkpoint()
        or checkpoint.build_sha != build_sha
        or checkpoint.protocol_ref != protocol_ref
    ):
        raise AutonomousRuntimeCheckpointError(
            "persisted autonomous runtime checkpoint identity differs from durable state"
        )
    return checkpoint


def verify_autonomous_runtime_checkpoint(
    root: str | Path,
    store: JournalStore,
    *,
    protocol: Mapping[str, object],
    completed: Sequence[Mapping[str, object]],
) -> CompositeReplayCheckpoint:
    if not completed:
        raise AutonomousRuntimeCheckpointError(
            "runtime checkpoint verification requires completed episodes"
        )
    checkpoint = preflight_autonomous_runtime_checkpoint(
        root,
        protocol=protocol,
        completed_episodes=len(completed),
    )
    replay = _replay(protocol, completed_episodes=len(completed))
    authority, verifier = _authority(
        store=store,
        protocol=protocol,
        completed=completed,
        replay=replay,
    )
    build_sha, protocol_ref = _expected_identity(protocol)
    try:
        resumed = resume_from_composite_checkpoint(
            _events(protocol),
            start_at=_instant(
                _utc(_events(protocol)[0].available_at, field="first replay event")
                - timedelta(microseconds=1)
            ),
            checkpoint=checkpoint,
            runtime_state_authority=authority,
            runtime_state_verifier=verifier,
            build_sha=build_sha,
            protocol_ref=protocol_ref,
        )
    except (ReplayError, TypeError, ValueError) as error:
        raise AutonomousRuntimeCheckpointError(
            "persisted autonomous runtime checkpoint does not match current authorities"
        ) from error
    if resumed.checkpoint() != replay.checkpoint():
        raise AutonomousRuntimeCheckpointError(
            "runtime checkpoint resume produced a different causal replay cut"
        )
    return checkpoint
