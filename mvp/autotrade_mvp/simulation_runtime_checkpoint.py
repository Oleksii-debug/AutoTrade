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
import hmac
import json
import os
from pathlib import Path
from tempfile import mkstemp
import stat
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
_AUTHORITY_PREFIX = "autotrade-autonomous-runtime"
_VERIFIER_ID = "autotrade-autonomous-runtime-verifier-v1"
_AUTHORITY_KEY_NAME = ".autonomous-runtime-authority.key"
_SEAL_DOMAIN = b"AutoTrade/autonomous-runtime-checkpoint/v1\x00"
_COMPLETION_DOMAIN = b"AutoTrade/autonomous-completion-checkpoint/v1\x00"
COMPLETION_RECEIPT_FIELD = "runtime_checkpoint_completion_receipt"
_COMPONENT_AGGREGATE_TYPES = (
    "account_reconciliation",
    "authority_state",
    "economic_book",
    "order_projection_book",
    "provider_activity",
    "reservation_book",
    "risk_decision",
    "risk_policy_registry",
    "settlement_book",
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


def autonomous_protocol_digest(protocol: Mapping[str, object]) -> str:
    """Hash deterministic experiment inputs, separately from local signing identity.

    The product key fingerprint remains in the durable start and signed runtime
    authority identity. Fresh identical experiments must not change their public
    decisions because their private checkpoint keys were generated independently.
    """
    if type(protocol) is not dict:
        raise TypeError("autonomous protocol must be an exact dictionary")
    return payload_digest({
        key: value for key, value in protocol.items()
        if key != "runtime_authority_key_sha256"
    })


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
    protocol_digest = autonomous_protocol_digest(protocol)
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


def _runtime_scope_snapshot(
    store: JournalStore,
    *,
    run_id: str,
    completion_event_id: str | None = None,
) -> tuple[
    dict[str, object],
    list[dict[str, object]],
    dict[str, list[dict[str, object]]],
]:
    """Capture only ZERO-owned durable runtime authority.

    Host control/UI events share the physical journal but are not simulation,
    portfolio, risk, OMS, reconciliation, settlement, or provider authority.
    They must therefore not invalidate a ZERO replay checkpoint after they have
    been independently delivered. Relevant ZERO authority is read twice by the
    caller so a concurrent financial/runtime mutation still fails closed.
    """

    loop_events = [
        _event_identity(event)
        for event in JournalStore.load_events(
            store,
            "canonical_autonomous_simulation",
            run_id,
        )
    ]
    if completion_event_id is not None:
        if (
            type(completion_event_id) is not str
            or not completion_event_id
            or not loop_events
            or loop_events[-1].get("event_id") != completion_event_id
        ):
            raise AutonomousRuntimeCheckpointError(
                "completion checkpoint is not the terminal ZERO loop event"
            )
        loop_events = loop_events[:-1]

    authority_events = {
        aggregate_type: _aggregate_events(store, aggregate_type)
        for aggregate_type in _COMPONENT_AGGREGATE_TYPES
    }
    cut = {
        "schema": "autonomous-runtime-scope-cut.v1",
        "run_id": run_id,
        "loop_event_count": len(loop_events),
        "authority_event_counts": {
            aggregate_type: len(events)
            for aggregate_type, events in authority_events.items()
        },
        "state_digest": "sha256:" + _digest(
            {
                "loop_events": loop_events,
                "authority_events": authority_events,
            }
        ),
    }
    return cut, loop_events, authority_events


def _autonomous_publication_owned(
    item: Mapping[str, object],
    *,
    run_id: str,
) -> bool:
    """Return whether one pending publication belongs to ZERO runtime authority."""

    envelope = item.get("payload")
    if type(envelope) is not dict:
        raise AutonomousRuntimeCheckpointError(
            "pending outbox payload is not a canonical event envelope"
        )
    aggregate_type = envelope.get("aggregate_type")
    aggregate_id = envelope.get("aggregate_id")
    if aggregate_type == "canonical_autonomous_simulation":
        return aggregate_id == run_id

    event_payload = envelope.get("payload")
    event_payload = event_payload if type(event_payload) is dict else {}
    environment = envelope.get("environment", event_payload.get("environment"))
    host_id = envelope.get("host_id", event_payload.get("host_id"))
    return (
        aggregate_type in _COMPONENT_AGGREGATE_TYPES
        and environment == "SIMULATION"
        and host_id == "local-simulation"
    )


def _autonomous_owned_pending_publications(
    store: JournalStore,
    *,
    run_id: str,
) -> tuple[dict[str, object], ...]:
    pending = JournalStore.pending_outbox(store, limit=1000)
    count = JournalStore.pending_outbox_count(store)
    if len(pending) == 1000 and count > len(pending):
        raise AutonomousRuntimeCheckpointError(
            "runtime checkpoint publication ownership scan exceeded bounded outbox window"
        )
    if len(pending) != count:
        raise AutonomousRuntimeCheckpointError(
            "runtime checkpoint pending outbox changed during ownership preflight"
        )
    return tuple(
        item
        for item in pending
        if _autonomous_publication_owned(item, run_id=run_id)
    )


def deliver_autonomous_owned_publications(
    store: JournalStore,
    *,
    run_id: str,
) -> None:
    """Acknowledge only publications owned by the closed ZERO authority scope."""

    for item in _autonomous_owned_pending_publications(store, run_id=run_id):
        JournalStore.mark_outbox_delivered(
            store,
            item["outbox_id"],
            expected_envelope_hash=item["envelope_hash"],
        )


def _require_checkpoint_outbox_state(
    store: JournalStore,
    *,
    run_id: str,
    completion_event_id: str | None,
) -> None:
    """Require ZERO-owned publication completion; ignore foreign Host/UI backlog."""

    pending = _autonomous_owned_pending_publications(store, run_id=run_id)
    if not pending:
        return
    if (
        completion_event_id is not None
        and len(pending) == 1
        and pending[0].get("event_id") == completion_event_id
        and pending[0].get("topic") == "autotrade.simulation.events"
    ):
        return
    raise AutonomousRuntimeCheckpointError(
        "runtime checkpoint requires all ZERO-owned publications delivered"
    )


def _stable_runtime_components(
    store: JournalStore,
    *,
    protocol: Mapping[str, object],
    completed: Sequence[Mapping[str, object]],
    completion_preimage: str | None = None,
) -> tuple[str, dict[str, str]]:
    if type(store) is not JournalStore:
        raise TypeError("runtime checkpoint requires the canonical JournalStore")
    if type(completed) not in {list, tuple}:
        raise TypeError("completed autonomous episodes must be a list or tuple")

    run_id, _build_sha, protocol_digest = _protocol_identity(protocol)
    identity = store.store_identity
    store_identity = {
        "canonical_path": identity.canonical_path,
        "filesystem_device": identity.filesystem_device,
        "filesystem_inode": identity.filesystem_inode,
        "identity_source": identity.identity_source,
        "windows_volume_serial": identity.windows_volume_serial,
        "windows_file_index_high": identity.windows_file_index_high,
        "windows_file_index_low": identity.windows_file_index_low,
    }
    _require_checkpoint_outbox_state(
        store,
        run_id=run_id,
        completion_event_id=completion_preimage,
    )
    captured_cut, loop_events, authority_events = _runtime_scope_snapshot(
        store,
        run_id=run_id,
        completion_event_id=completion_preimage,
    )

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
        # The terminal completion receipt is attached atomically to the
        # durable episode after its runtime preimage was captured. Its seal
        # proves publication of that episode; it is not part of strategy
        # state. Excluding it keeps the persisted cut stable on reopen.
        completed_values.append({
            key: value for key, value in item.items()
            if key != COMPLETION_RECEIPT_FIELD
        })

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
        "cut": captured_cut,
        "journal_store_identity": store_identity,
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
                "settlement": authority_events["settlement_book"],
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

    after_cut, _after_loop, _after_authority = _runtime_scope_snapshot(
        store,
        run_id=run_id,
        completion_event_id=completion_preimage,
    )
    _require_checkpoint_outbox_state(
        store,
        run_id=run_id,
        completion_event_id=completion_preimage,
    )
    if after_cut != captured_cut:
        raise AutonomousRuntimeCheckpointError(
            "ZERO-owned runtime state changed while checkpoint cut was captured"
        )
    cut_id = "sha256:" + _digest(
        {
            "kind": "autonomous-runtime-scope-cut-v1",
            "cut": captured_cut,
            "components": components,
        }
    )
    return cut_id, components


def _runtime_leaf(root: str | Path, name: str) -> Path:
    candidate = Path(root).expanduser() / name
    if os.name == "nt":
        from autotrade_foundation.local_filesystem import (
            freeze_local_filesystem_path,
            require_qualified_local_filesystem_path,
        )

        candidate = Path(freeze_local_filesystem_path(candidate))
        require_qualified_local_filesystem_path(candidate)
        return candidate
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    # Canonicalize the containing namespace, not the authority-bearing leaf.
    # Resolving the final component here would dereference an existing symlink
    # before _read_runtime_regular_file() can enforce lstat/O_NOFOLLOW.
    parent = candidate.parent.resolve(strict=False)
    return parent / candidate.name


def _authority_key_path(root: str | Path) -> Path:
    return _runtime_leaf(root, _AUTHORITY_KEY_NAME)


def _read_runtime_regular_file(
    path: Path,
    *,
    subject: str,
    maximum_bytes: int,
) -> bytes:
    if type(maximum_bytes) is not int or maximum_bytes < 1:
        raise TypeError("maximum_bytes must be a positive exact integer")

    if os.name == "nt":
        from autotrade_foundation.windows_namespace import (
            retain_windows_parent_namespace,
            retain_windows_regular_file,
        )

        try:
            with retain_windows_parent_namespace(path, create=False) as authority:
                with retain_windows_regular_file(
                    authority,
                    target_name=path.name,
                    subject=subject,
                ) as descriptor:
                    chunks = bytearray()
                    while len(chunks) <= maximum_bytes:
                        chunk = os.read(
                            descriptor,
                            min(65536, maximum_bytes + 1 - len(chunks)),
                        )
                        if not chunk:
                            break
                        chunks.extend(chunk)
        except (OSError, RuntimeError) as error:
            raise AutonomousRuntimeCheckpointError(
                f"{subject} is unavailable or unsafe"
            ) from error
        if len(chunks) > maximum_bytes:
            raise AutonomousRuntimeCheckpointError(
                f"{subject} exceeds its bounded size"
            )
        return bytes(chunks)

    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = None
    try:
        before = os.lstat(path)
        if (
            stat.S_ISLNK(before.st_mode)
            or not stat.S_ISREG(before.st_mode)
            or int(before.st_nlink) != 1
        ):
            raise AutonomousRuntimeCheckpointError(
                f"{subject} must have one ordinary pathname"
            )
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or int(opened.st_nlink) != 1
            or int(before.st_dev) != int(opened.st_dev)
            or int(before.st_ino) != int(opened.st_ino)
        ):
            raise AutonomousRuntimeCheckpointError(
                f"{subject} identity changed while opening"
            )
        chunks = bytearray()
        while len(chunks) <= maximum_bytes:
            chunk = os.read(
                descriptor,
                min(65536, maximum_bytes + 1 - len(chunks)),
            )
            if not chunk:
                break
            chunks.extend(chunk)
    except OSError as error:
        raise AutonomousRuntimeCheckpointError(
            f"{subject} is unavailable or unsafe"
        ) from error
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
    if len(chunks) > maximum_bytes:
        raise AutonomousRuntimeCheckpointError(
            f"{subject} exceeds its bounded size"
        )
    return bytes(chunks)


def _canonical_key_identity(value: object) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or value != value.lower()
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise AutonomousRuntimeCheckpointError(
            "runtime checkpoint authority key identity is invalid"
        )
    return value


def _issue_autonomous_runtime_authority_key(root: str | Path) -> str:
    """Issue new product-owned signing state; pre-existing state is rejected."""

    path = _authority_key_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    key = os.urandom(32)

    if os.name == "nt":
        from autotrade_foundation.windows_namespace import (
            publish_windows_regular_bytes,
            retain_windows_parent_namespace,
            serialize_windows_directory_publication,
        )

        try:
            with retain_windows_parent_namespace(path, create=True) as authority:
                with serialize_windows_directory_publication(
                    authority,
                    lock_name=".autonomous-runtime-authority.lock",
                ):
                    publish_windows_regular_bytes(
                        authority,
                        target_name=path.name,
                        data=key,
                        replace=False,
                    )
        except FileExistsError as error:
            raise AutonomousRuntimeCheckpointError(
                "runtime checkpoint authority key pre-exists an unowned session"
            ) from error
        except (OSError, RuntimeError) as error:
            raise AutonomousRuntimeCheckpointError(
                "runtime checkpoint authority key could not be issued"
            ) from error
        return sha256(key).hexdigest()

    create_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_CLOEXEC"):
        create_flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        create_flags |= os.O_NOFOLLOW
    descriptor = None
    try:
        descriptor = os.open(path, create_flags, 0o600)
        view = memoryview(key)
        written = 0
        while written < len(view):
            count = os.write(descriptor, view[written:])
            if count <= 0:
                raise OSError("runtime authority key write made no progress")
            written += count
        os.fsync(descriptor)
    except FileExistsError as error:
        raise AutonomousRuntimeCheckpointError(
            "runtime checkpoint authority key pre-exists an unowned session"
        ) from error
    except OSError as error:
        raise AutonomousRuntimeCheckpointError(
            "runtime checkpoint authority key could not be issued"
        ) from error
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
    try:
        directory_fd = os.open(
            path.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
    except OSError:
        directory_fd = None
    if directory_fd is not None:
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    return sha256(key).hexdigest()


def _require_autonomous_runtime_authority_key(
    root: str | Path,
    expected_identity: object,
) -> bytes:
    expected = _canonical_key_identity(expected_identity)
    path = _authority_key_path(root)

    if os.name != "nt":
        try:
            observed = os.lstat(path)
        except OSError as error:
            raise AutonomousRuntimeCheckpointError(
                "runtime checkpoint authority key is unavailable"
            ) from error
        if (
            stat.S_ISLNK(observed.st_mode)
            or not stat.S_ISREG(observed.st_mode)
            or int(observed.st_nlink) != 1
        ):
            raise AutonomousRuntimeCheckpointError(
                "runtime checkpoint authority key must have one ordinary pathname"
            )
        if stat.S_IMODE(observed.st_mode) & 0o077:
            raise AutonomousRuntimeCheckpointError(
                "runtime checkpoint authority key permissions are too broad"
            )

    key = _read_runtime_regular_file(
        path,
        subject="runtime checkpoint authority key",
        maximum_bytes=32,
    )
    if len(key) != 32:
        raise AutonomousRuntimeCheckpointError(
            "runtime checkpoint authority key has invalid length"
        )
    if not hmac.compare_digest(sha256(key).hexdigest(), expected):
        raise AutonomousRuntimeCheckpointError(
            "runtime checkpoint authority key does not match durable session authority"
        )
    return key


def _verifier(authority_id: str, key: bytes) -> RuntimeStateVerifier:
    if type(key) is not bytes or len(key) != 32:
        raise TypeError("runtime checkpoint authority key must be 32 bytes")
    with _VERIFIER_LOCK:
        existing = _VERIFIERS.get(authority_id)
        if existing is not None:
            return existing

        def verify_signature(material: bytes, signature: str) -> bool:
            if type(material) is not bytes or type(signature) is not str:
                return False
            expected = hmac.new(
                key,
                _SEAL_DOMAIN + material,
                sha256,
            ).hexdigest()
            return hmac.compare_digest(expected, signature)

        verifier = RuntimeStateVerifier.select_product_trust(
            authority_id=authority_id,
            verifier_id=_VERIFIER_ID,
            verify_signature=verify_signature,
        )
        _VERIFIERS[authority_id] = verifier
        return verifier


def _authority_id(
    protocol: Mapping[str, object],
    authority_key_identity: object,
) -> str:
    run_id, _build_sha, protocol_digest = _protocol_identity(protocol)
    key_identity = _canonical_key_identity(authority_key_identity)
    return (
        f"{_AUTHORITY_PREFIX}:{run_id}:{protocol_digest}:key-sha256:{key_identity}"
    )


def _authority(
    *,
    root: str | Path,
    store: JournalStore,
    protocol: Mapping[str, object],
    authority_key_identity: object,
    completed: Sequence[Mapping[str, object]],
    replay: CausalReplay,
) -> tuple[RuntimeStateAuthority, RuntimeStateVerifier]:
    key_identity = _canonical_key_identity(authority_key_identity)
    key = _require_autonomous_runtime_authority_key(root, key_identity)
    authority_id = _authority_id(protocol, key_identity)

    def sign_material(material: bytes) -> str:
        if type(material) is not bytes:
            raise TypeError("runtime checkpoint signing material must be bytes")
        return hmac.new(
            key,
            _SEAL_DOMAIN + material,
            sha256,
        ).hexdigest()

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
            signer=sign_material,
            cut_resolver=resolve_cut,
        ),
        _verifier(authority_id, key),
    )


def _expected_identity(
    protocol: Mapping[str, object],
) -> tuple[str, str]:
    _run_id, build_sha, protocol_digest = _protocol_identity(protocol)
    return build_sha, f"autonomous-simulation:{protocol_digest}"


def checkpoint_path(root: str | Path) -> Path:
    if not isinstance(root, (str, Path)):
        raise TypeError("runtime checkpoint root must be a path")
    return _runtime_leaf(root, _CHECKPOINT_NAME)


def build_autonomous_runtime_checkpoint(
    root: str | Path,
    store: JournalStore,
    *,
    protocol: Mapping[str, object],
    authority_key_identity: object,
    completed: Sequence[Mapping[str, object]],
) -> CompositeReplayCheckpoint:
    replay = _replay(protocol, completed_episodes=len(completed))
    authority, verifier = _authority(
        root=root,
        store=store,
        protocol=protocol,
        authority_key_identity=authority_key_identity,
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
    authority_key_identity: object,
    completed: Sequence[Mapping[str, object]],
) -> CompositeReplayCheckpoint:
    if not completed:
        raise AutonomousRuntimeCheckpointError(
            "runtime checkpoint requires at least one completed episode"
        )
    checkpoint = build_autonomous_runtime_checkpoint(
        root,
        store,
        protocol=protocol,
        authority_key_identity=authority_key_identity,
        completed=completed,
    )
    if os.environ.get("AUTOTRADE_TEST_DIAGNOSTIC") == "1" and len(completed) == 1:
        scope, _, _ = _runtime_scope_snapshot(store, run_id=protocol["run_id"])
        identity = store.store_identity
        diagnostic = {
            "scope": scope["state_digest"],
            "identity": sha256(str(identity).encode("utf-8")).hexdigest(),
            "identity_fields": {
                field: sha256(str(getattr(identity, field)).encode("utf-8")).hexdigest()
                for field in (
                    "canonical_path", "filesystem_device", "filesystem_inode",
                    "identity_source", "windows_volume_serial", "windows_file_index_high",
                    "windows_file_index_low",
                )
            },
            "cut": checkpoint.runtime_cut_id,
        }
        (Path(root).parent / "bootstrap-checkpoint-diagnostic.json").write_text(
            json.dumps(diagnostic, sort_keys=True), encoding="utf-8"
        )
    destination = checkpoint_path(root)
    data = checkpoint.to_canonical_json().encode("utf-8")
    if os.name == "nt":
        from autotrade_foundation.windows_namespace import (
            publish_windows_regular_bytes,
            retain_windows_parent_namespace,
            serialize_windows_directory_publication,
        )

        try:
            with retain_windows_parent_namespace(destination, create=True) as authority:
                with serialize_windows_directory_publication(
                    authority,
                    lock_name=".autonomous-runtime-checkpoint.lock",
                ):
                    publish_windows_regular_bytes(
                        authority,
                        target_name=destination.name,
                        data=data,
                        replace=True,
                    )
        except (OSError, RuntimeError) as error:
            raise AutonomousRuntimeCheckpointError(
                "autonomous runtime checkpoint could not be persisted"
            ) from error
        return checkpoint

    descriptor: int | None = None
    temporary: Path | None = None
    try:
        descriptor, raw_temporary = mkstemp(
            prefix=".autonomous-runtime-checkpoint-",
            suffix=".tmp",
            dir=destination.parent,
        )
        temporary = Path(raw_temporary)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        temporary = None
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
        raise AutonomousRuntimeCheckpointError(
            "autonomous runtime checkpoint could not be persisted"
        ) from error
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
    return checkpoint


def prepare_autonomous_completion_receipt(root, store, *, protocol, result, event_id):
    """Authorize exactly one terminal publication from the existing runtime cut.

    The receipt is committed inside the same command as EpisodeCompleted. It
    permits reconstructing a sidecar lost between that durable commit and file
    publication, without signing an arbitrary later journal generation.
    """
    prior = [event["payload"] for event in JournalStore.load_events(
        store, "canonical_autonomous_simulation", protocol["run_id"]
    ) if event["event_type"] == "AutonomousEpisodeCompleted"]
    prior = [{key: value for key, value in item.items()
              if key != COMPLETION_RECEIPT_FIELD} for item in prior]
    if type(result) is not dict or result.get("episode") != len(prior) + 1:
        raise AutonomousRuntimeCheckpointError("completion checkpoint chronology is invalid")
    cut_id, components = _stable_runtime_components(store, protocol=protocol, completed=prior)
    cut = JournalStore.whole_store_state_cut(store)
    key_identity = protocol["runtime_authority_key_sha256"]
    key = _require_autonomous_runtime_authority_key(root, key_identity)
    material = {
        "schema": "autonomous-completion-checkpoint.v1",
        "event_id": event_id,
        "protocol_digest": autonomous_protocol_digest(protocol),
        "authority_key_identity": key_identity,
        "prior_cut": cut,
        "prior_runtime_cut_id": cut_id,
        "prior_components": components,
        "result_digest": _digest(result),
    }
    # Capture is read-only; a racing writer invalidates the final command CAS.
    if JournalStore.whole_store_state_cut(store) != cut:
        raise AutonomousRuntimeCheckpointError("completion checkpoint cut changed")
    return {**material, "seal": hmac.new(key, _COMPLETION_DOMAIN + _canonical_bytes(material), sha256).hexdigest()}


def repair_autonomous_completion_checkpoint(root, store, *, protocol, completed):
    """Recover the one authorized terminal sidecar publication, never newer state."""
    if not completed:
        return
    path = checkpoint_path(root)
    try:
        document = _read_runtime_regular_file(path, subject="autonomous runtime checkpoint",
                                             maximum_bytes=1024 * 1024).decode("utf-8")
    except AutonomousRuntimeCheckpointError:
        # Only an absent ordinary leaf is recoverable. Symlinks, hard links,
        # nonlocal namespaces and unreadable/corrupt files remain hard failures.
        try:
            os.lstat(path)
        except FileNotFoundError:
            document = None
        else:
            raise AutonomousRuntimeCheckpointError(
                "completed autonomous state is missing or has unsafe runtime checkpoint evidence"
            )
    key_identity = protocol["runtime_authority_key_sha256"]
    key = _require_autonomous_runtime_authority_key(root, key_identity)
    if document is not None:
        try:
            existing = CompositeReplayCheckpoint.from_canonical_json(document)
            verifier = _verifier(_authority_id(protocol, key_identity), key)
            RuntimeStateVerifier.verify_checkpoint_binding(verifier, existing)
        except (ReplayError, TypeError, ValueError) as error:
            raise AutonomousRuntimeCheckpointError(
                "persisted autonomous runtime checkpoint authority seal is invalid"
            ) from error
        if (existing.build_sha, existing.protocol_ref, existing.runtime_authority_id) != (
                *_expected_identity(protocol), _authority_id(protocol, key_identity)):
            raise AutonomousRuntimeCheckpointError("completion checkpoint identity differs")
        if existing.replay.cursor == len(completed):
            return
        if existing.replay.cursor != len(completed) - 1:
            raise AutonomousRuntimeCheckpointError("completion checkpoint prefix is stale")
    events = JournalStore.load_events(store, "canonical_autonomous_simulation", protocol["run_id"])
    terminal = events[-1]
    if terminal["event_type"] != "AutonomousEpisodeCompleted":
        raise AutonomousRuntimeCheckpointError("completion checkpoint lacks terminal evidence")
    payload = terminal["payload"]
    receipt = payload.get(COMPLETION_RECEIPT_FIELD)
    fields = {"schema", "event_id", "protocol_digest", "authority_key_identity", "prior_cut",
              "prior_runtime_cut_id", "prior_components", "result_digest", "seal"}
    if type(receipt) is not dict or set(receipt) != fields:
        raise AutonomousRuntimeCheckpointError("completion checkpoint receipt is missing or malformed")
    material = {name: value for name, value in receipt.items() if name != "seal"}
    seal = receipt["seal"]
    if type(seal) is not str or not hmac.compare_digest(
            seal, hmac.new(key, _COMPLETION_DOMAIN + _canonical_bytes(material), sha256).hexdigest()):
        raise AutonomousRuntimeCheckpointError("completion checkpoint receipt seal is invalid")
    result = {name: value for name, value in payload.items() if name != COMPLETION_RECEIPT_FIELD}
    if (receipt["schema"] != "autonomous-completion-checkpoint.v1"
            or receipt["event_id"] != terminal["event_id"]
            or receipt["protocol_digest"] != autonomous_protocol_digest(protocol)
            or receipt["authority_key_identity"] != key_identity
            or receipt["result_digest"] != _digest(result)
            or result != completed[-1]):
        raise AutonomousRuntimeCheckpointError("completion checkpoint receipt identity differs")
    prior_cut = receipt["prior_cut"]
    if (
        type(prior_cut) is not dict
        or type(prior_cut.get("journal_sequence")) is not int
        or prior_cut["journal_sequence"] < 0
        or type(prior_cut.get("counts")) is not dict
    ):
        raise AutonomousRuntimeCheckpointError(
            "completion checkpoint prior journal cut is malformed"
        )
    expected_terminal_sequence = prior_cut["journal_sequence"] + 1
    if terminal.get("journal_sequence") != expected_terminal_sequence:
        raise AutonomousRuntimeCheckpointError(
            "completion checkpoint terminal chronology differs"
        )
    batch = JournalStore.load_command_event_batch(
        store,
        command_id=terminal["event_id"],
        actor="canonical-autonomous-simulation",
        environment="SIMULATION",
        idempotency_key=terminal["event_id"],
        request=payload,
    )
    if (
        type(batch) is not dict
        or len(batch.get("events", ())) != 1
        or batch["events"][0].get("event_id") != terminal["event_id"]
        or batch["events"][0].get("journal_sequence") != expected_terminal_sequence
    ):
        raise AutonomousRuntimeCheckpointError(
            "completion checkpoint terminal command authority differs"
        )
    cut_id, components = _stable_runtime_components(
        store,
        protocol=protocol,
        completed=completed[:-1],
        completion_preimage=terminal["event_id"],
    )
    if cut_id != receipt["prior_runtime_cut_id"] or components != receipt["prior_components"]:
        raise AutonomousRuntimeCheckpointError("completion checkpoint preimage does not match current authorities")
    state = JournalStore.outbox_delivery_state(
        store,
        terminal["event_id"],
        topic="autotrade.simulation.events",
    )
    if state is None:
        raise AutonomousRuntimeCheckpointError(
            "completion checkpoint terminal publication is missing"
        )
    if not state["delivered"]:
        JournalStore.mark_outbox_delivered(
            store,
            state["outbox_id"],
            expected_envelope_hash=state["envelope_hash"],
        )
        state = JournalStore.outbox_delivery_state(
            store,
            terminal["event_id"],
            topic="autotrade.simulation.events",
        )
    if state is None or not state["delivered"]:
        raise AutonomousRuntimeCheckpointError(
            "completion checkpoint terminal publication remains undelivered"
        )
    persist_autonomous_runtime_checkpoint(root, store, protocol=protocol,
        authority_key_identity=key_identity, completed=completed)


def preflight_autonomous_runtime_checkpoint(
    root: str | Path,
    *,
    protocol: Mapping[str, object],
    authority_key_identity: object,
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
        document = _read_runtime_regular_file(
            path,
            subject="autonomous runtime checkpoint",
            maximum_bytes=1024 * 1024,
        ).decode("utf-8")
    except UnicodeDecodeError as error:
        raise AutonomousRuntimeCheckpointError(
            "persisted autonomous runtime checkpoint is not UTF-8"
        ) from error
    except AutonomousRuntimeCheckpointError as error:
        raise AutonomousRuntimeCheckpointError(
            "completed autonomous state is missing or has unsafe runtime checkpoint evidence"
        ) from error
    try:
        checkpoint = CompositeReplayCheckpoint.from_canonical_json(document)
    except (ReplayError, TypeError, ValueError) as error:
        raise AutonomousRuntimeCheckpointError(
            "persisted autonomous runtime checkpoint is invalid"
        ) from error

    replay = _replay(protocol, completed_episodes=completed_episodes)
    build_sha, protocol_ref = _expected_identity(protocol)
    expected_authority_id = _authority_id(protocol, authority_key_identity)
    if (
        checkpoint.replay != replay.checkpoint()
        or checkpoint.build_sha != build_sha
        or checkpoint.protocol_ref != protocol_ref
        or checkpoint.runtime_authority_id != expected_authority_id
    ):
        raise AutonomousRuntimeCheckpointError(
            "persisted autonomous runtime checkpoint identity differs from durable state"
        )

    # Authenticate the persisted common-cut envelope before any provider or
    # financial projection is reconstructed. The plain checkpoint fingerprint
    # detects accidental corruption but is not an authority boundary: a writer
    # that can replace the JSON bytes can recompute it. The product-owned HMAC
    # key is available at this stage and verifies the exact replay, component,
    # cut, build and protocol binding without consulting mutable runtime state.
    key = _require_autonomous_runtime_authority_key(
        root,
        authority_key_identity,
    )
    verifier = _verifier(expected_authority_id, key)
    try:
        RuntimeStateVerifier.verify_checkpoint_binding(verifier, checkpoint)
    except (ReplayError, TypeError, ValueError) as error:
        raise AutonomousRuntimeCheckpointError(
            "persisted autonomous runtime checkpoint authority seal is invalid"
        ) from error
    return checkpoint


def verify_autonomous_runtime_checkpoint(
    root: str | Path,
    store: JournalStore,
    *,
    protocol: Mapping[str, object],
    authority_key_identity: object,
    completed: Sequence[Mapping[str, object]],
) -> CompositeReplayCheckpoint:
    if not completed:
        raise AutonomousRuntimeCheckpointError(
            "runtime checkpoint verification requires completed episodes"
        )
    checkpoint = preflight_autonomous_runtime_checkpoint(
        root,
        protocol=protocol,
        authority_key_identity=authority_key_identity,
        completed_episodes=len(completed),
    )
    replay = _replay(protocol, completed_episodes=len(completed))
    authority, verifier = _authority(
        root=root,
        store=store,
        protocol=protocol,
        authority_key_identity=authority_key_identity,
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
