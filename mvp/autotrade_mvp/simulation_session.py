"""One network-free, journal-backed canonical simulation episode.

This deliberately runs one episode per directory. A durable send without a
completed reconciliation cannot be reconstructed from a fresh simulated
provider instance, so restart leaves it UNKNOWN instead of sending again.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
import sys
from uuid import NAMESPACE_URL, uuid5

from .accounting import book_equity_fill, book_external_cash_flow
from .authority import AuthoritativeRiskSnapshot, AuthorityPolicy, AuthorityService
from .dispatch import (
    GuardedDispatcher,
    stable_client_order_id,
    submission_attempt_aggregate_id,
)
from .durable_reservations import DurableReservationBook
from .exact_decimal import (
    canonical_decimal_text, exact_add, exact_multiply,
    parse_bounded_exact_decimal,
)
from .persistence import JournalStore, payload_digest
from .pipeline import MovingAverageStrategy
from .provider_activity_accounting import (
    DurableProviderEconomicBook,
    commit_economic_batch_with_reservation_consumption,
)
from .reconciliation import (
    ProviderFillEvidence,
    ResourceAvailabilityEvidence,
    SnapshotConsistencyEvidence,
    reconcile_account,
)
from .reconciliation_journal import reconciliation_payload, record_reconciliation_checkpoint
from .risk import RiskContext, RiskIntent, RiskPolicy
from .simulated_provider import SimulatedProvider
from research.autotrade_research.artifacts.resource_lock import ResourceLock
from research.autotrade_research.artifacts.store import ArtifactStore


ACCOUNT = "canonical-sim-account"
INSTRUMENT = "CANONICAL-SIM@1"
INSTRUMENT_ID = "f752464b-1f5d-41c3-b55c-4af4c123a3db"
ENVIRONMENT = "SIMULATION"
PROVIDER = "SIMULATED"
INITIAL_CASH = Decimal("1000")
FEE_RATE = Decimal("0.001")
_AGGREGATE = "single-episode"
_OWNER_AGGREGATE_TYPE = "canonical_simulation_store_owner"
_OWNER_AGGREGATE_ID = "canonical"
_BOOTSTRAP_CONTRACT = "canonical-simulation-bootstrap-v1"

_SIMULATION_PROTOCOL_VERSION = "canonical-simulation@2"
_STRATEGY_ID = "moving-average"
_STRATEGY_VERSION = "1"
_STRATEGY_FAST = 2
_STRATEGY_SLOW = 3
_STRATEGY_QUANTITY = Decimal("1")
_RISK_POLICY_SPEC = (
    ("max_abs_position", "10"),
    ("max_single_notional", "1000"),
    ("max_gross_leverage", "2"),
    ("max_net_leverage", "2"),
    ("max_daily_loss", "500"),
    ("max_drawdown_fraction", "0.20"),
    ("max_data_age_seconds", "5"),
    ("max_fx_age_seconds", "60"),
    ("min_margin_headroom", "0.20"),
    ("max_stress_loss", "500"),
)
_RESERVATION_PROTOCOL = "durable-reservations@1"
_ADMISSION_PROTOCOL = "authority-admission@1"
_ECONOMIC_PROTOCOL = "provider-economic-book@1"
_RECONCILIATION_PROTOCOL = "account-reconciliation@1"
_PROVIDER_PROTOCOL = "simulated-provider@1"


def _uuid(kind: str, episode_id: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"autotrade-canonical-simulation:{kind}:{episode_id}"))


def _python_source_tree_digest(root: Path) -> str:
    """Content identity for the exact Python source tree used by the simulator."""

    files = tuple(sorted(path for path in root.rglob("*.py") if path.is_file()))
    if not files:
        raise RuntimeError("canonical simulation source tree is unavailable")
    digest = sha256()
    for path in files:
        try:
            relative = path.relative_to(root).as_posix().encode("utf-8")
            source = path.read_text(encoding="utf-8")
            content = (
                source.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
            )
        except (OSError, UnicodeError, ValueError) as error:
            raise RuntimeError(
                "canonical simulation source identity cannot be established"
            ) from error
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return "sha256:" + digest.hexdigest()


def _module_source_root(component) -> Path:
    module = sys.modules.get(component.__module__)
    raw_path = getattr(module, "__file__", None)
    if not isinstance(raw_path, str) or not raw_path:
        raise RuntimeError("canonical simulation dependency source is unavailable")
    path = Path(raw_path).resolve()
    if path.suffix in {".pyc", ".pyo"}:
        source = path.with_suffix(".py")
        if source.is_file():
            path = source
    return path.parent


def _simulation_build_identity() -> str:
    """Bind restart to the exact executable source packages, not only inputs."""

    document = {
        "mvp_source_tree": _python_source_tree_digest(Path(__file__).resolve().parent),
        "shared_numeric_source_tree": _python_source_tree_digest(
            _module_source_root(parse_bounded_exact_decimal)
        ),
        "research_artifact_source_tree": _python_source_tree_digest(
            _module_source_root(ArtifactStore)
        ),
    }
    return payload_digest(document)


def _canonical_risk_policy_document() -> dict[str, str]:
    return {
        name: canonical_decimal_text(parse_bounded_exact_decimal(value))
        for name, value in _RISK_POLICY_SPEC
    }


def _simulation_protocol_document(*, fault_after_send: bool) -> dict[str, object]:
    """Frozen behavior identity required before a durable session may be reused."""

    return {
        "protocol_version": _SIMULATION_PROTOCOL_VERSION,
        "source_build_identity": _simulation_build_identity(),
        "strategy": {
            "id": _STRATEGY_ID,
            "version": _STRATEGY_VERSION,
            "fast": _STRATEGY_FAST,
            "slow": _STRATEGY_SLOW,
            "quantity": canonical_decimal_text(_STRATEGY_QUANTITY),
        },
        "financial_scope": {
            "account_id": ACCOUNT,
            "provider_id": PROVIDER,
            "environment": ENVIRONMENT,
            "instrument_version": INSTRUMENT,
            "instrument_id": INSTRUMENT_ID,
        },
        "economics": {
            "initial_cash": canonical_decimal_text(INITIAL_CASH),
            "fee_rate": canonical_decimal_text(FEE_RATE),
            "provider_protocol": _PROVIDER_PROTOCOL,
            "economic_protocol": _ECONOMIC_PROTOCOL,
        },
        "risk_policy": _canonical_risk_policy_document(),
        "authority_protocols": {
            "admission": _ADMISSION_PROTOCOL,
            "reservation": _RESERVATION_PROTOCOL,
            "reconciliation": _RECONCILIATION_PROTOCOL,
        },
        "fault_injection_mode": (
            "AFTER_ACCEPT_RESPONSE_LOST" if fault_after_send else "NONE"
        ),
    }


def _now(value: str | None) -> str:
    if value is None:
        point = datetime.now(timezone.utc)
    else:
        try:
            point = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (AttributeError, ValueError) as error:
            raise ValueError("now must be an ISO timestamp with timezone") from error
        if point.tzinfo is None:
            raise ValueError("now must include timezone")
    return point.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _prices(values: list[str]) -> list[Decimal]:
    if not values:
        raise ValueError("at least one simulated price is required")
    parsed = []
    for raw in values:
        if type(raw) is not str:
            raise TypeError("prices must be decimal strings")
        value = parse_bounded_exact_decimal(raw.strip())
        if value <= 0:
            raise ValueError("prices must be finite positive decimals")
        parsed.append(value)
    return parsed


def _event_envelope(
    store: JournalStore,
    kind: str,
    episode_id: str,
    payload: dict,
    now: str,
    *,
    aggregate_version: int | None = None,
) -> dict:
    version = (
        store.next_aggregate_version("canonical_simulation_session", _AGGREGATE)
        if aggregate_version is None
        else aggregate_version
    )
    if type(version) is not int or version < 1:
        raise ValueError("session aggregate version must be a positive integer")
    return {
        "event_id": _uuid(kind, episode_id),
        "event_type": kind,
        "schema_version": "1.0.0",
        "aggregate_type": "canonical_simulation_session",
        "aggregate_id": _AGGREGATE,
        "aggregate_version": str(version),
        "host_id": "local-simulation",
        "owner_epoch": "1",
        "environment": ENVIRONMENT,
        "occurred_at": now,
        "observed_at": now,
        "committed_at": now,
        "correlation_id": _uuid("correlation", episode_id),
        "causation_id": None,
        "payload": payload,
        "payload_hash": payload_digest(payload),
        "evidence_refs": [],
    }


def _event(
    store: JournalStore,
    kind: str,
    episode_id: str,
    payload: dict,
    now: str,
    *,
    expected_cut: dict[str, object] | None = None,
) -> dict:
    envelope = _event_envelope(store, kind, episode_id, payload, now)
    kwargs = {}
    if expected_cut is not None:
        kwargs = {
            "expected_journal_sequence": expected_cut["journal_sequence"],
            "expected_whole_store_counts": expected_cut["counts"],
        }
    store.append_event(envelope, **kwargs)
    return envelope


def _owner_payload(
    *, episode_id: str, input_hash: str, decision: str, evidence_time: str,
    protocol_identity: str, source_build_identity: str
) -> dict[str, object]:
    return {
        "schema_version": "1.0.0",
        "bootstrap_contract": _BOOTSTRAP_CONTRACT,
        "episode_id": episode_id,
        "input_hash": input_hash,
        "decision": decision,
        "environment": ENVIRONMENT,
        "evidence_time": evidence_time,
        "protocol_identity": protocol_identity,
        "source_build_identity": source_build_identity,
    }


def _claim_owner(
    store: JournalStore, *, episode_id: str, payload: dict[str, object], now: str
) -> dict:
    envelope = {
        "event_id": _uuid("SimulationSessionOwned", episode_id),
        "event_type": "SimulationSessionOwned",
        "schema_version": "1.0.0",
        "aggregate_type": _OWNER_AGGREGATE_TYPE,
        "aggregate_id": _OWNER_AGGREGATE_ID,
        "aggregate_version": "1",
        "host_id": "local-simulation",
        "owner_epoch": "1",
        "environment": ENVIRONMENT,
        "occurred_at": now,
        "observed_at": now,
        "committed_at": now,
        "correlation_id": _uuid("correlation", episode_id),
        "causation_id": None,
        "payload": payload,
        "payload_hash": payload_digest(payload),
        "evidence_refs": [],
    }
    JournalStore.claim_first_event(store, envelope)
    persisted = JournalStore.get_event(store, envelope["event_id"])
    if persisted is None:
        raise RuntimeError("canonical simulation ownership event was not persisted")
    return persisted


def _validate_owner(
    owners: list[dict], *, episode_id: str, input_hash: str, decision: str,
    protocol_identity: str, source_build_identity: str
) -> str:
    if len(owners) != 1:
        raise ValueError("durable simulation ownership chronology is invalid")
    owner = owners[0]
    if (
        owner.get("event_type") != "SimulationSessionOwned"
        or owner.get("aggregate_version") != 1
        or owner.get("journal_sequence") != 1
    ):
        raise ValueError("durable simulation owner is not the first store authority")
    payload = owner.get("payload")
    if type(payload) is not dict:
        raise ValueError("durable simulation owner payload is invalid")
    expected_keys = {
        "schema_version",
        "bootstrap_contract",
        "episode_id",
        "input_hash",
        "decision",
        "environment",
        "evidence_time",
        "protocol_identity",
        "source_build_identity",
    }
    if set(payload) != expected_keys:
        raise ValueError("durable simulation owner payload schema is invalid")
    if (payload.get("protocol_identity") != protocol_identity
        or payload.get("source_build_identity") != source_build_identity):
        raise ValueError("state directory belongs to incompatible simulation protocol/configuration")
    evidence_time = payload.get("evidence_time")
    if (
        payload.get("schema_version") != "1.0.0"
        or payload.get("bootstrap_contract") != _BOOTSTRAP_CONTRACT
        or payload.get("episode_id") != episode_id
        or payload.get("input_hash") != input_hash
        or payload.get("decision") != decision
        or payload.get("environment") != ENVIRONMENT
        or type(evidence_time) is not str
        or _now(evidence_time) != evidence_time
    ):
        raise ValueError("state directory belongs to another simulation input")
    return evidence_time


def _require_prestart_cut(
    store: JournalStore,
    *,
    economic_events: list[dict],
    reconciliation_events: list[dict],
) -> dict[str, object]:
    events = [*economic_events, *reconciliation_events]
    expected = set(range(2, 2 + len(events)))
    observed = {event.get("journal_sequence") for event in events}
    if observed != expected:
        raise ValueError("canonical simulation bootstrap prefix is not contiguous")
    expected_counts = {
        "events": 1 + len(events),
        "outbox": len(events),
        "command_dedupe": 1 if economic_events else 0,
        "projection_checkpoints": 0,
        "global_projection_checkpoints": 0,
    }
    cut = JournalStore.whole_store_state_cut(store)
    if cut.get("journal_sequence") != 1 + len(events):
        raise ValueError("state directory contains foreign durable journal authority")
    if cut.get("counts") != expected_counts:
        raise ValueError(
            "canonical simulation bootstrap durable state is not exact"
        )
    return cut


def _deliver_event(
    store: JournalStore,
    event_id: str,
    *,
    topic: str,
    expected_cut: dict[str, object],
) -> None:
    state = JournalStore.outbox_delivery_state(
        store, event_id, topic=topic
    )
    if state["delivered"]:
        return
    JournalStore.mark_outbox_delivered(
        store,
        state["outbox_id"],
        expected_envelope_hash=state["envelope_hash"],
        expected_journal_sequence=expected_cut["journal_sequence"],
        expected_whole_store_counts=expected_cut["counts"],
    )
    if not JournalStore.outbox_delivery_state(
        store, event_id, topic=topic
    )["delivered"]:
        raise RuntimeError("bootstrap outbox delivery did not become durable")


def _require_zero_wire_blocked_submission(
    store: JournalStore,
    *,
    episode_id: str,
) -> tuple[str, str, str]:
    attempt_id = _uuid("attempt", episode_id)
    aggregate_id = submission_attempt_aggregate_id(
        environment=ENVIRONMENT,
        account_id=ACCOUNT,
        attempt_id=attempt_id,
    )
    events = JournalStore.load_events(
        store,
        "submission_attempt",
        aggregate_id,
    )
    if (
        len(events) != 2
        or [event.get("event_type") for event in events]
        != ["SubmissionPrepared", "SubmissionBlocked"]
        or [event.get("aggregate_version") for event in events] != [1, 2]
    ):
        raise ValueError(
            "zero-wire BLOCKED requires exact Prepared -> Blocked chronology"
        )
    prepared, blocked = events
    prepared_payload = prepared.get("payload")
    blocked_payload = blocked.get("payload")
    if type(prepared_payload) is not dict or type(blocked_payload) is not dict:
        raise ValueError("durable blocked submission payload is invalid")
    intent_id = _uuid("intent", episode_id)
    expected_client_order_id = stable_client_order_id(
        "simulated",
        intent_id,
        environment=ENVIRONMENT,
        account_id=ACCOUNT,
    )
    reason = blocked_payload.get("reason")
    if (
        prepared_payload.get("attempt_id") != attempt_id
        or prepared_payload.get("intent_id") != intent_id
        or prepared_payload.get("provider") != "simulated"
        or prepared_payload.get("environment") != ENVIRONMENT
        or prepared_payload.get("account_id") != ACCOUNT
        or prepared_payload.get("client_order_id") != expected_client_order_id
        or blocked_payload.get("client_order_id") != expected_client_order_id
        or type(reason) is not str
        or not reason.strip()
    ):
        raise ValueError("durable blocked submission scope is invalid")
    return attempt_id, expected_client_order_id, reason.strip()


def _require_initial_reconciliation_checkpoint(store: JournalStore) -> dict:
    checkpoints = JournalStore.load_events_by_aggregate_type(
        store,
        "account_reconciliation",
    )
    if len(checkpoints) != 1:
        raise ValueError(
            "zero-wire terminal projection requires one admission reconciliation"
        )
    checkpoint = checkpoints[0]
    payload = checkpoint.get("payload")
    if (
        checkpoint.get("event_type") != "AccountReconciled"
        or checkpoint.get("aggregate_version") != 1
        or type(payload) is not dict
        or payload.get("provider_id") != PROVIDER
        or payload.get("account_id") != ACCOUNT
        or payload.get("environment") != ENVIRONMENT
        or payload.get("complete") is not True
        or payload.get("snapshot_consistent") is not True
        or payload.get("blocking_resources") != []
    ):
        raise ValueError(
            "zero-wire terminal projection has invalid reconciliation authority"
        )
    return checkpoint


def _started_identity(store: JournalStore, *, episode_id: str) -> dict[str, str]:
    events = JournalStore.load_events(store, "canonical_simulation_session", _AGGREGATE)
    if not events or events[0].get("event_type") != "SimulationSessionStarted":
        raise ValueError("terminal projection requires durable Started identity")
    payload = events[0].get("payload")
    keys = ("input_hash", "protocol_identity", "source_build_identity")
    if type(payload) is not dict or payload.get("episode_id") != episode_id or any(type(payload.get(k)) is not str for k in keys):
        raise ValueError("terminal projection has invalid Started identity")
    return {k: payload[k] for k in keys}


def _hold_projection(
    store: JournalStore,
    *,
    episode_id: str,
    completed: bool,
) -> tuple[dict[str, object], dict[str, object]]:
    if JournalStore.load_events_by_aggregate_type(store, "submission_attempt"):
        raise ValueError("HOLD terminal projection cannot contain submission attempts")
    if JournalStore.load_events_by_aggregate_type(store, "reservation_book"):
        raise ValueError("HOLD terminal projection cannot contain reservations")
    economic = DurableProviderEconomicBook(
        store,
        provider_id=PROVIDER,
        account_id=ACCOUNT,
        environment=ENVIRONMENT,
    )
    economic_events = JournalStore.load_events(
        store,
        "economic_book",
        economic.book_id,
    )
    if len(economic_events) != 1:
        raise ValueError("HOLD terminal projection requires exact seed economics")
    checkpoint = _require_initial_reconciliation_checkpoint(store)
    expected_counts = {
        "events": 5 if completed else 4,
        "outbox": 2,
        "command_dedupe": 1,
        "projection_checkpoints": 0,
        "global_projection_checkpoints": 0,
    }
    cut = JournalStore.whole_store_state_cut(store)
    if (
        cut.get("journal_sequence") != expected_counts["events"]
        or cut.get("counts") != expected_counts
    ):
        raise ValueError("HOLD terminal durable state is not exact")
    result = {
        "status": "HOLD",
        "decision": "HOLD",
        "environment": ENVIRONMENT,
        "episode_id": episode_id,
        "cash": str(economic.cash("USD")),
        "position": str(economic.position(INSTRUMENT)),
        "reconciled": True,
        "order_id": None,
        "fill_id": None,
        "reconciliation_event_id": checkpoint["event_id"],
        "new_outbound_requests": 0,
    }
    result.update(_started_identity(store, episode_id=episode_id))
    return result, cut


def _zero_wire_blocked_projection(
    store: JournalStore,
    root: Path,
    *,
    episode_id: str,
    required_reservation_state: str,
) -> tuple[dict[str, object], DurableReservationBook, str, str]:
    attempt_id, client_order_id, reason = _require_zero_wire_blocked_submission(
        store,
        episode_id=episode_id,
    )
    checkpoint = _require_initial_reconciliation_checkpoint(store)
    economic = DurableProviderEconomicBook(
        store,
        provider_id=PROVIDER,
        account_id=ACCOUNT,
        environment=ENVIRONMENT,
    )
    economic_events = JournalStore.load_events(
        store,
        "economic_book",
        economic.book_id,
    )
    if len(economic_events) != 1:
        raise ValueError(
            "zero-wire terminal projection requires exact seed economics only"
        )
    reservations = DurableReservationBook(
        store,
        environment=ENVIRONMENT,
        account_id=ACCOUNT,
    )
    reservation = reservations.get(_uuid("reservation", episode_id))
    if reservation.state != required_reservation_state:
        raise ValueError(
            "zero-wire terminal reservation disposition is inconsistent"
        )
    if any(amount != 0 for amount in reservation.consumed.values()):
        raise ValueError(
            "zero-wire BLOCKED cannot coexist with consumed reservation exposure"
        )
    result = {
        "status": "BLOCKED",
        "decision": "BUY",
        "environment": ENVIRONMENT,
        "episode_id": episode_id,
        "reason": reason,
        "cash": str(economic.cash("USD")),
        "position": str(economic.position(INSTRUMENT)),
        "reconciled": True,
        "order_id": client_order_id,
        "fill_id": None,
        "reconciliation_event_id": checkpoint["event_id"],
        "new_outbound_requests": 0,
    }
    result.update(_started_identity(store, episode_id=episode_id))
    return result, reservations, attempt_id, client_order_id


def _finalize_zero_wire_blocked(
    store: JournalStore,
    root: Path,
    *,
    episode_id: str,
    timestamp: str,
    resumed: bool,
) -> dict[str, object]:
    validation_start_cut = JournalStore.whole_store_state_cut(store)
    result, reservations, attempt_id, client_order_id = (
        _zero_wire_blocked_projection(
            store,
            root,
            episode_id=episode_id,
            required_reservation_state="WORKING",
        )
    )
    terminal_cut = JournalStore.whole_store_state_cut(store)
    if terminal_cut != validation_start_cut:
        raise ValueError(
            "whole-store state changed while validating zero-wire terminal facts"
        )
    terminal_plan = reservations.prepare_zero_wire_blocked_terminal_mutation(
        event_key=_uuid("blocked-terminal-reservation-event", episode_id),
        idempotency_key=_uuid("blocked-terminal-reservation-idempotency", episode_id),
        reservation_id=_uuid("reservation", episode_id),
        provider=PROVIDER,
        attempt_id=attempt_id,
        client_order_id=client_order_id,
        committed_at=timestamp,
    )
    if terminal_plan.already_committed or terminal_plan.envelope is None:
        raise ValueError(
            "zero-wire reservation terminal already exists without session terminal"
        )
    if (
        JournalStore.next_aggregate_version(
            store,
            "canonical_simulation_session",
            _AGGREGATE,
        )
        != 2
    ):
        raise ValueError("zero-wire session terminal aggregate version is invalid")
    completed = _event_envelope(
        store,
        "SimulationSessionCompleted",
        episode_id,
        result,
        timestamp,
        aggregate_version=2,
    )
    command_id = _uuid("blocked-terminal-command", episode_id)
    JournalStore.commit_command(
        store,
        command_id=command_id,
        actor="canonical-simulation",
        environment=ENVIRONMENT,
        idempotency_key=command_id,
        request={
            "episode_id": episode_id,
            "attempt_id": attempt_id,
            "reservation_id": _uuid("reservation", episode_id),
            "blocked_reason": result["reason"],
        },
        result=result,
        state_version=2,
        events=[
            (terminal_plan.envelope, None),
            (completed, None),
        ],
        expected_journal_sequence=terminal_cut["journal_sequence"],
        expected_whole_store_counts=terminal_cut["counts"],
    )
    reservations.refresh()
    terminal = reservations.get(_uuid("reservation", episode_id))
    if terminal.state != "REJECTED" or reservations.active():
        raise RuntimeError(
            "zero-wire terminal commit did not release reservation authority"
        )
    return {**result, "resumed": resumed}


def _risk_policy() -> RiskPolicy:
    return RiskPolicy.create(**dict(_RISK_POLICY_SPEC))


def _risk_context(price: Decimal) -> RiskContext:
    return RiskContext.create(
        state_version=1, equity=canonical_decimal_text(INITIAL_CASH), positions={},
        marks={INSTRUMENT: canonical_decimal_text(price)}, reserved_position_delta={},
        daily_pnl="0", drawdown_fraction="0", market_data_age_seconds="1",
        fx_age_seconds={"USD": "1"}, margin_headroom="1",
        capability_allowed=True, borrow_available=True,
        stress_scenarios=({INSTRUMENT: "-0.25"},),
    )


def _reconcile(provider: SimulatedProvider, economic: DurableProviderEconomicBook,
               now: str, *, fill: dict | None = None, client_order_id: str | None = None):
    snapshot = provider.account_snapshot(now=now)
    fills = ()
    ids = ()
    if fill is not None:
        fee = fill["fees"][0]
        fills = (ProviderFillEvidence.create(
            provider_id=PROVIDER, account_id=ACCOUNT, environment=ENVIRONMENT,
            provider_execution_id=fill["provider_execution_id"],
            client_order_id=client_order_id, instrument=fill["instrument_version"],
            quantity=fill["last_quantity"]["value"], price=fill["last_price"],
            fee_amount=fee["amount"], fee_currency=fee["currency"],
            trade_time=fill["trade_time"],
        ),)
        ids = (fill["provider_execution_id"],)
    result = reconcile_account(
        provider_id=PROVIDER, account_id=ACCOUNT, environment=ENVIRONMENT,
        local_cash={"USD": economic.cash("USD")},
        provider_cash={"USD": snapshot["balances"][0]["total"]},
        local_positions=({INSTRUMENT: economic.position(INSTRUMENT)} if fill else {}),
        provider_positions={item["instrument_version"]: item["quantity"]["value"]
                            for item in snapshot["positions"]},
        local_execution_ids=ids, provider_fills=fills,
        snapshot_consistency=SnapshotConsistencyEvidence(
            provider_id=PROVIDER, account_id=ACCOUNT, environment=ENVIRONMENT,
            mode="ATOMIC", query_started_at=now, query_completed_at=now,
        ),
        coverage_start=now, coverage_end=now, pagination_complete=True,
        provider_activity_provider_id=PROVIDER,
        provider_activity_account_id=ACCOUNT,
        resource_availability=(ResourceAvailabilityEvidence(
            provider_id=PROVIDER, account_id=ACCOUNT, environment=ENVIRONMENT,
            snapshot_id=snapshot["snapshot_id"], query_started_at=now,
            query_completed_at=now,
            valid_until=(datetime.fromisoformat(now.replace("Z", "+00:00"))
                         + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
            available_resources={"CASH:USD": snapshot["balances"][0]["available"]},
            provider_as_of=snapshot["provider_as_of"],
            evidence_refs=(f"simulated:provider-snapshot:{snapshot['snapshot_id']}",),
        ) if fill is None else None),
    )
    return result, snapshot


def run_canonical_simulation(
    prices: list[str], state_dir: str | Path, *, episode_id: str,
    now: str | None = None, fault_after_send: bool = False,
) -> dict[str, object]:
    """Run one BUY/HOLD episode; a restarted ambiguous send is never retried."""
    if not isinstance(episode_id, str) or not episode_id.strip():
        raise ValueError("episode_id is required")
    if type(fault_after_send) is not bool:
        raise TypeError("fault_after_send must be boolean")
    values = _prices(prices)
    decision = MovingAverageStrategy(
        fast=_STRATEGY_FAST, slow=_STRATEGY_SLOW
    ).decide(values, _STRATEGY_QUANTITY)
    if decision.side == "SELL":
        raise ValueError("this long-only simulation session supports BUY/HOLD prices")
    buy_requirements = None
    if decision.side == "BUY":
        amount = exact_multiply(decision.quantity, decision.price)
        required = exact_add(amount, exact_multiply(amount, FEE_RATE))
        buy_requirements = (amount, required)
    protocol_document = _simulation_protocol_document(
        fault_after_send=fault_after_send
    )
    protocol_identity = payload_digest(protocol_document)
    source_build_identity = protocol_document["source_build_identity"]
    input_payload = {
        "episode_id": episode_id,
        "prices": [canonical_decimal_text(value) for value in values],
        "protocol_identity": protocol_identity,
    }
    input_hash = payload_digest(input_payload)
    root = Path(state_dir)
    root.mkdir(parents=True, exist_ok=True)
    with ResourceLock(root / ".canonical-simulation.lock"):
        return _run_locked(
            root, episode_id=episode_id, input_hash=input_hash,
            protocol_identity=protocol_identity,
            source_build_identity=source_build_identity,
            decision=decision, now=now, fault_after_send=fault_after_send,
            buy_requirements=buy_requirements,
        )


def _run_locked(root: Path, *, episode_id: str, input_hash: str,
                protocol_identity: str, source_build_identity: str,
                decision, now: str | None, fault_after_send: bool,
                buy_requirements: tuple[Decimal, Decimal] | None) -> dict[str, object]:
    store = JournalStore(root / "journal.sqlite3")
    owners = JournalStore.load_events(store, _OWNER_AGGREGATE_TYPE, _OWNER_AGGREGATE_ID)
    prior = JournalStore.load_events(store, "canonical_simulation_session", _AGGREGATE)
    resumed_from_owner = bool(owners)

    if owners:
        timestamp = _validate_owner(
            owners,
            episode_id=episode_id,
            input_hash=input_hash,
            decision=decision.side,
            protocol_identity=protocol_identity,
            source_build_identity=source_build_identity,
        )
    else:
        if prior:
            raise ValueError(
                "durable simulation session exists without first-store ownership"
            )
        timestamp = _now(now)
        owner_payload = _owner_payload(
            episode_id=episode_id,
            input_hash=input_hash,
            decision=decision.side,
            evidence_time=timestamp,
            protocol_identity=protocol_identity,
            source_build_identity=source_build_identity,
        )
        try:
            _claim_owner(
                store,
                episode_id=episode_id,
                payload=owner_payload,
                now=timestamp,
            )
        except ValueError as error:
            if "durable business state" in str(error):
                raise ValueError(
                    "state directory contains foreign durable business authority"
                ) from error
            raise

    if prior:
        if len(prior) > 2:
            raise ValueError("canonical simulation session chronology is invalid")
        started = prior[0]
        payload = started.get("payload")
        if (type(payload) is not dict
            or payload.get("protocol_identity") != protocol_identity
            or payload.get("source_build_identity") != source_build_identity):
            raise ValueError("state directory belongs to incompatible simulation protocol/configuration")
        if (
            started.get("event_type") != "SimulationSessionStarted"
            or type(payload) is not dict
            or set(payload) != {"input_hash", "decision", "episode_id", "environment", "protocol_identity", "protocol_version", "source_build_identity"}
            or payload.get("input_hash") != input_hash
            or payload.get("decision") != decision.side
            or payload.get("episode_id") != episode_id
            or payload.get("environment") != ENVIRONMENT
        ):
            raise ValueError("state directory belongs to another simulation input")
        if len(prior) == 2:
            if prior[1].get("event_type") != "SimulationSessionCompleted":
                raise ValueError("canonical simulation session chronology is invalid")
            result = dict(prior[1]["payload"])
            if (result.get("protocol_identity") != protocol_identity
                or result.get("input_hash") != input_hash
                or result.get("source_build_identity") != source_build_identity):
                raise ValueError("completed simulation protocol/input identity does not match session")
            economic = DurableProviderEconomicBook(
                store, provider_id=PROVIDER, account_id=ACCOUNT,
                environment=ENVIRONMENT,
            )
            if (result["cash"] != str(economic.cash("USD"))
                    or result["position"] != str(economic.position(INSTRUMENT))):
                raise ValueError("completed simulation does not match durable economics")
            if result.get("status") == "BLOCKED":
                expected, _, _, _ = _zero_wire_blocked_projection(
                    store,
                    root,
                    episode_id=episode_id,
                    required_reservation_state="REJECTED",
                )
                if result != expected:
                    raise ValueError(
                        "completed BLOCKED session does not match durable zero-wire facts"
                    )
            elif result.get("status") == "HOLD":
                expected, _completed_cut = _hold_projection(
                    store,
                    episode_id=episode_id,
                    completed=True,
                )
                if result != expected:
                    raise ValueError(
                        "completed HOLD session does not match durable zero-wire facts"
                    )
            result["resumed"] = True
            result["new_outbound_requests"] = 0
            return result
        if decision.side == "HOLD":
            result, terminal_cut = _hold_projection(
                store,
                episode_id=episode_id,
                completed=False,
            )
            _event(
                store,
                "SimulationSessionCompleted",
                episode_id,
                result,
                timestamp,
                expected_cut=terminal_cut,
            )
            return {**result, "resumed": True}
        if decision.side == "BUY":
            attempt_id = _uuid("attempt", episode_id)
            attempt_events = JournalStore.load_events(
                store,
                "submission_attempt",
                submission_attempt_aggregate_id(
                    environment=ENVIRONMENT,
                    account_id=ACCOUNT,
                    attempt_id=attempt_id,
                ),
            )
            if (
                len(attempt_events) == 2
                and [event.get("event_type") for event in attempt_events]
                == ["SubmissionPrepared", "SubmissionBlocked"]
            ):
                return _finalize_zero_wire_blocked(
                    store,
                    root,
                    episode_id=episode_id,
                    timestamp=timestamp,
                    resumed=True,
                )
        return {
            "status": "UNKNOWN", "environment": ENVIRONMENT,
            "episode_id": episode_id, "reason": "incomplete_send_requires_reconciliation",
            "protocol_identity": protocol_identity, "input_hash": input_hash,
            "source_build_identity": source_build_identity,
            "reconciled": False, "resumed": True, "new_outbound_requests": 0,
        }

    future = (datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
              + timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
    provider = SimulatedProvider(
        account_id=ACCOUNT, initial_cash=canonical_decimal_text(INITIAL_CASH),
        fee_rate=canonical_decimal_text(FEE_RATE),
        transport_faults=({stable_client_order_id(
            "simulated", _uuid("intent", episode_id),
            environment=ENVIRONMENT, account_id=ACCOUNT,
        ): "AFTER_ACCEPT_RESPONSE_LOST"}
                          if fault_after_send else None),
    )
    economic = DurableProviderEconomicBook(
        store, provider_id=PROVIDER, account_id=ACCOUNT, environment=ENVIRONMENT,
    )
    seed = book_external_cash_flow(
        transaction_id=_uuid("seed-transaction", episode_id),
        cause_event_id=_uuid("seed-cause", episode_id),
        currency="USD", amount=str(INITIAL_CASH),
    )
    economic_events = JournalStore.load_events(store, "economic_book", economic.book_id)
    reconciliation_events = JournalStore.load_events_by_aggregate_type(
        store, "account_reconciliation"
    )
    bootstrap_cut = _require_prestart_cut(
        store,
        economic_events=economic_events,
        reconciliation_events=reconciliation_events,
    )
    if economic_events:
        plan = economic.prepare_batch_mutation((seed,), committed_at=timestamp)
        if len(economic_events) != 1 or not plan.already_committed:
            raise ValueError("canonical simulation seed bootstrap is invalid")
    else:
        economic.append(
            seed,
            expected_journal_sequence=bootstrap_cut["journal_sequence"],
            expected_whole_store_counts=bootstrap_cut["counts"],
        )
        economic_events = JournalStore.load_events(
            store, "economic_book", economic.book_id
        )
        if len(economic_events) != 1:
            raise RuntimeError("canonical simulation seed bootstrap was not persisted")
    bootstrap_cut = _require_prestart_cut(
        store,
        economic_events=economic_events,
        reconciliation_events=reconciliation_events,
    )
    _deliver_event(
        store,
        economic_events[0]["event_id"],
        topic="autotrade.economic.events",
        expected_cut=bootstrap_cut,
    )

    admission_reconciliation, snapshot = _reconcile(provider, economic, timestamp)
    if not admission_reconciliation.complete or admission_reconciliation.blocks_new_risk:
        raise ValueError("initial simulated provider reconciliation failed")
    expected_checkpoint_payload = reconciliation_payload(
        admission_reconciliation, observed_at=timestamp
    )
    expected_checkpoint_payload["checkpoint_owner"] = {
        "host_id": "local-simulation",
        "owner_epoch": "1",
    }
    reconciliation_events = JournalStore.load_events_by_aggregate_type(
        store, "account_reconciliation"
    )
    bootstrap_cut = _require_prestart_cut(
        store,
        economic_events=economic_events,
        reconciliation_events=reconciliation_events,
    )
    if reconciliation_events:
        if (
            len(reconciliation_events) != 1
            or reconciliation_events[0].get("event_type") != "AccountReconciled"
            or reconciliation_events[0].get("aggregate_version") != 1
            or reconciliation_events[0].get("payload") != expected_checkpoint_payload
        ):
            raise ValueError("canonical simulation reconciliation bootstrap is invalid")
        availability = reconciliation_events[0]
    else:
        availability = record_reconciliation_checkpoint(
            store, reconciliation_id="canonical-simulation-admission",
            result=admission_reconciliation, observed_at=timestamp,
            host_id="local-simulation", owner_epoch="1",
            expected_journal_sequence=bootstrap_cut["journal_sequence"],
            expected_whole_store_counts=bootstrap_cut["counts"],
        )
        reconciliation_events = [availability]
    bootstrap_cut = _require_prestart_cut(
        store,
        economic_events=economic_events,
        reconciliation_events=reconciliation_events,
    )
    _deliver_event(
        store,
        availability["event_id"],
        topic="autotrade.reconciliation.events",
        expected_cut=bootstrap_cut,
    )
    bootstrap_cut = _require_prestart_cut(
        store,
        economic_events=economic_events,
        reconciliation_events=reconciliation_events,
    )
    _event(store, "SimulationSessionStarted", episode_id, {
        "input_hash": input_hash, "decision": decision.side,
        "episode_id": episode_id, "environment": ENVIRONMENT,
        "protocol_identity": protocol_identity,
        "protocol_version": _SIMULATION_PROTOCOL_VERSION,
        "source_build_identity": source_build_identity,
    }, timestamp, expected_cut=bootstrap_cut)
    if decision.side == "HOLD":
        result, terminal_cut = _hold_projection(
            store,
            episode_id=episode_id,
            completed=False,
        )
        _event(
            store,
            "SimulationSessionCompleted",
            episode_id,
            result,
            timestamp,
            expected_cut=terminal_cut,
        )
        return {**result, "resumed": resumed_from_owner}

    policy = _risk_policy()
    context = _risk_context(decision.price)
    def resolve_risk(request):
        return AuthoritativeRiskSnapshot(
            context=context, risk_policy=policy,
            account_id=request.account_id, environment=request.environment,
            provider_id=request.provider_id,
            instrument_version=request.instrument_version,
            capability_snapshot_id=request.capability_snapshot_id,
            reconciliation_checkpoint_event_id=request.reconciliation_checkpoint_event_id,
            journal_sequence_cut=request.journal_sequence_cut,
            reservation_version=request.reservation_version,
            reservation_state_digest=request.reservation_state_digest,
            authority_policy_id=request.authority_policy_id,
            authority_policy_version=request.authority_policy_version,
            evaluated_at=request.evaluated_at, valid_until=future,
            evidence_refs={name: f"simulated:{name.lower()}:{episode_id}" for name in (
                "PORTFOLIO", "MARKET", "MARGIN", "POLICY", "RECONCILIATION",
                "CAPABILITY", "BORROW", "STRESS", "FX", "FACTORS", "LIQUIDITY",
                "LIQUIDATION", "SETTLEMENT", "OPTION_LIFECYCLE", "FUTURES_LIFECYCLE",
            )},
        )

    authority = AuthorityService(store, risk_authority_resolver=resolve_risk)
    policy_id = _uuid("policy", episode_id)
    authority.register_policy(AuthorityPolicy.create(
        policy_id=policy_id, account_id=ACCOUNT, environments={ENVIRONMENT},
        instruments={(INSTRUMENT_ID, 1)}, actions={"ORDER.SUBMIT"},
        max_notional="1000", expires_at=future, autonomous=True,
        protection_only=False, version=1,
    ))
    reservations = DurableReservationBook(
        store, environment=ENVIRONMENT, account_id=ACCOUNT,
        resolution_artifact_store=ArtifactStore(root / "artifacts"),
        resolution_artifact_root=root / "artifacts",
    )
    amount, required = buy_requirements
    quantity_text = canonical_decimal_text(decision.quantity)
    price_text = canonical_decimal_text(decision.price)
    amount_text = canonical_decimal_text(amount)
    required_text = canonical_decimal_text(required)
    intent_id = _uuid("intent", episode_id)
    intent_hash = payload_digest({
        "episode_id": episode_id, "side": "BUY", "quantity": quantity_text,
        "price": price_text, "instrument": INSTRUMENT,
    })
    admission_id = _uuid("admission", episode_id)
    admission = authority.admit(
        command_id=_uuid("financial-command", episode_id),
        idempotency_key=_uuid("financial-command", episode_id),
        admission_id=admission_id, policy_id=policy_id,
        intent_id=intent_id, intent_hash=intent_hash, account_id=ACCOUNT,
        environment=ENVIRONMENT, instrument_id=INSTRUMENT_ID,
        instrument_version=1, action="ORDER.SUBMIT", notional=amount_text,
        capability_snapshot_id="simulated-capability-v1",
        risk_intent=RiskIntent.create(
            symbol=INSTRUMENT, side="BUY", quantity=quantity_text,
            price=price_text, expected_state_version=1,
        ),
        risk_context=context, risk_policy=policy, risk_valid_until=future,
        reservation_book=reservations,
        reservation_id=_uuid("reservation", episode_id),
        reservation_requirements={"CASH:USD": required_text},
        reservation_available={"CASH:USD": snapshot["balances"][0]["available"]},
        reservation_checkpoint_event_id=availability["event_id"],
        reservation_provider_id=PROVIDER, reservation_max_age_seconds="60",
        now=timestamp,
    )
    if admission.outcome != "ADMITTED":
        result = {
            "status": "RISK_REJECTED", "decision": "BUY", "environment": ENVIRONMENT,
            "episode_id": episode_id, "cash": str(economic.cash("USD")),
            "position": str(economic.position(INSTRUMENT)),
            "protocol_identity": protocol_identity, "input_hash": input_hash,
            "source_build_identity": source_build_identity, "reconciled": True,
            "order_id": None, "fill_id": None,
            "reconciliation_event_id": availability["event_id"],
            "new_outbound_requests": 0,
        }
        _event(store, "SimulationSessionCompleted", episode_id, result, timestamp)
        return {**result, "resumed": resumed_from_owner}

    def final_check(candidate_hash, current_time):
        return authority.dispatch_allowed(
            admission_id, intent_hash=candidate_hash, account_id=ACCOUNT,
            environment=ENVIRONMENT, instrument_id=INSTRUMENT_ID,
            instrument_version=1, action="ORDER.SUBMIT", now=current_time,
            capability_snapshot_id="simulated-capability-v1",
        )

    attempt_id = _uuid("attempt", episode_id)
    dispatch = GuardedDispatcher(
        store, environment=ENVIRONMENT, account_id=ACCOUNT,
        owner_token="canonical-simulation-owner",
    ).dispatch(
        attempt_id=attempt_id, intent_id=intent_id, intent_hash=intent_hash,
        provider="simulated", request={
            "attempt_id": attempt_id, "instrument_version": INSTRUMENT,
            "side": "BUY", "quantity": quantity_text,
            "price": price_text, "now": timestamp,
        },
        now=timestamp, authority_check=final_check,
        transport_send=provider.transport_send,
    )
    if dispatch.status == "BLOCKED":
        if provider.outbound_request_count != 0:
            raise RuntimeError(
                "durable BLOCKED chronology conflicts with provider outbound activity"
            )
        return _finalize_zero_wire_blocked(
            store,
            root,
            episode_id=episode_id,
            timestamp=timestamp,
            resumed=False,
        )
    if dispatch.status != "SENT":
        return {
            "status": "UNKNOWN",
            "decision": "BUY", "environment": ENVIRONMENT,
            "episode_id": episode_id, "reason": dispatch.reason,
            "protocol_identity": protocol_identity, "input_hash": input_hash,
            "source_build_identity": source_build_identity,
            "reconciled": False, "resumed": False,
            "new_outbound_requests": provider.outbound_request_count,
        }
    if dispatch.response.get("outcome") != "ACKNOWLEDGED":
        raise ValueError("simulated send did not acknowledge; reconciliation required")
    fills = provider.activity_fills()
    if len(fills) != 1:
        raise ValueError("acknowledgement is not a fill; reconciliation required")
    fill = fills[0]
    fee = fill["fees"][0]
    commit_economic_batch_with_reservation_consumption(
        economic, reservations,
        command_id=_uuid("financial-fill-command", episode_id),
        idempotency_key=_uuid("financial-fill-command", episode_id),
        reservation_id=_uuid("reservation", episode_id),
        usage={"CASH:USD": required_text},
        transactions=(book_equity_fill(
            transaction_id=_uuid("fill-transaction", episode_id),
            cause_event_id=fill["provider_execution_id"],
            instrument=fill["instrument_version"], settlement_currency="USD",
            side=fill["side"], quantity=fill["last_quantity"]["value"],
            price=fill["last_price"], fee=fee["amount"],
            fee_currency=fee["currency"],
        ),), committed_at=timestamp,
    )
    reconciled, _ = _reconcile(
        provider, economic, timestamp, fill=fill, client_order_id=dispatch.client_order_id,
    )
    if not reconciled.complete or reconciled.blocks_new_risk:
        raise ValueError("simulated fill failed account reconciliation")
    checkpoint = record_reconciliation_checkpoint(
        store, reconciliation_id="canonical-simulation-fill",
        result=reconciled, observed_at=timestamp,
        host_id="local-simulation", owner_epoch="1",
    )
    result = {
        "status": "FILL_RECONCILED_ORDER_UNCONFIRMED", "decision": "BUY",
        "environment": ENVIRONMENT, "episode_id": episode_id,
        "cash": str(economic.cash("USD")),
        "position": str(economic.position(INSTRUMENT)),
        "protocol_identity": protocol_identity, "input_hash": input_hash,
        "source_build_identity": source_build_identity,
        "reconciled": True, "order_id": dispatch.client_order_id,
        "fill_id": fill["provider_execution_id"],
        "reconciliation_event_id": checkpoint["event_id"],
        "new_outbound_requests": provider.outbound_request_count,
    }
    _event(store, "SimulationSessionCompleted", episode_id, result, timestamp)
    return {**result, "resumed": False}
