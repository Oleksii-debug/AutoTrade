"""Provider-free orchestration over the canonical financial authorities.

The existing single-episode entrypoint remains available. The autonomous ZERO
entrypoint runs a frozen observation stream and restores completed simulator
cuts. Unfinished sends remain UNKNOWN and are never spontaneously retried.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
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
from .durable_settlement import (
    DurableSettlementBook,
    SETTLEMENT_EVIDENCE_MEDIA_TYPE,
    settlement_completion_evidence_metadata,
    settlement_completion_evidence_receipt,
    settlement_rule_evidence_metadata,
    settlement_rule_evidence_receipt,
)
from .exact_decimal import (
    canonical_decimal_text, exact_add, exact_multiply,
    parse_bounded_exact_decimal,
)
from .persistence import JournalStore, canonical_json, payload_digest
from .pipeline import MovingAverageStrategy
from .provider_activity_accounting import (
    DurableProviderEconomicBook,
    commit_economic_batch_with_reservation_consumption,
    commit_order_fill_with_reservation_consumption,
)
from .reconciliation import (
    ProviderFillEvidence,
    ResourceAvailabilityEvidence,
    SnapshotConsistencyEvidence,
    reconcile_account,
)
from .reconciliation_journal import (
    load_latest_reconciliation_checkpoint,
    reconciliation_payload,
    record_reconciliation_checkpoint,
)
from .risk import RiskContext, RiskIntent, RiskPolicy
from .settlement import (
    SettlementAccountScope,
    SettlementEvidence,
    SettlementObligation,
    SettlementRuleBinding,
    equity_cash_obligation_from_transaction,
)
from .simulated_provider import SimulatedProvider
from .simulation_runtime_checkpoint import autonomous_protocol_digest
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

_SIMULATION_PROTOCOL_VERSION = "canonical-simulation@4"
_SIMULATION_EPOCH = "2026-09-30T12:00:00Z"
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
_SIMULATION_SETTLEMENT_RULE_ID = "canonical-simulator-equity-cash-same-day"
_SIMULATION_SETTLEMENT_RULE_VERSION = "1"
_SIMULATION_SETTLEMENT_EVIDENCE_DELAY_SECONDS = 3


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
        "settlement_policy": {
            "rule_id": _SIMULATION_SETTLEMENT_RULE_ID,
            "rule_version": _SIMULATION_SETTLEMENT_RULE_VERSION,
            "contractual_cycle": "SAME_DAY",
            "provider_evidence_delay_seconds": _SIMULATION_SETTLEMENT_EVIDENCE_DELAY_SECONDS,
        },
        "risk_policy": _canonical_risk_policy_document(),
        "authority_protocols": {
            "admission": _ADMISSION_PROTOCOL,
            "reservation": _RESERVATION_PROTOCOL,
            "reconciliation": _RECONCILIATION_PROTOCOL,
        },
        "clock": {"kind": "DETERMINISTIC_UTC", "default_epoch": _SIMULATION_EPOCH},
        "fault_injection_mode": (
            "AFTER_ACCEPT_RESPONSE_LOST" if fault_after_send else "NONE"
        ),
    }


def _now(value: str | None) -> str:
    if value is None:
        value = _SIMULATION_EPOCH
    if type(value) is not str:
        raise TypeError("now must be exact timestamp text")
    try:
        point = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("now must be an ISO timestamp with timezone") from error
    if point.tzinfo is None:
        raise ValueError("now must include timezone")
    return point.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _prices(values: list[str]) -> list[Decimal]:
    if type(values) is not list:
        raise TypeError("prices must be an exact built-in list")
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
) -> tuple[str, str, str, str]:
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
    blocked_at = blocked.get("committed_at")
    if (
        type(blocked_at) is not str
        or _now(blocked_at) != blocked_at
        or blocked.get("occurred_at") != blocked_at
        or blocked.get("observed_at") != blocked_at
    ):
        raise ValueError("durable blocked submission timestamp is invalid")
    return attempt_id, expected_client_order_id, reason.strip(), blocked_at


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


def _risk_rejected_projection(
    store: JournalStore, *, episode_id: str, completed: bool,
) -> tuple[dict[str, object], dict[str, object]]:
    """Recover a recorded zero-wire rejection, never grant new risk authority."""
    cut = JournalStore.whole_store_state_cut(store)
    if (JournalStore.load_events_by_aggregate_type(store, "submission_attempt")
            or JournalStore.load_events_by_aggregate_type(store, "reservation_book")):
        raise ValueError("RISK_REJECTED cannot contain submission or reservation exposure")
    # Replay the canonical authority's historical evidence checks; no resolver,
    # new admission, current-risk evaluation or provider instance is involved.
    authority = AuthorityService(store)
    state = AuthorityService.export_state(authority)
    admissions = state["admissions"]
    if (len(admissions) != 1 or len(state["policies"]) != 1
            or state["epoch"] != 1 or state["revocations"]
            or state["confirmations"] or state["used_confirmations"]
            or state["new_exposure_blocks"]):
        raise ValueError("RISK_REJECTED requires exact canonical authority history")
    admission = admissions[0]
    expected = {
        "admission_id": _uuid("admission", episode_id),
        "policy_id": _uuid("policy", episode_id),
        "intent_id": _uuid("intent", episode_id),
        "financial_command_id": _uuid("financial-command", episode_id),
        "account_id": ACCOUNT, "environment": ENVIRONMENT,
        "instrument": {"instrument_id": INSTRUMENT_ID, "version": 1},
        "action": "ORDER.SUBMIT", "outcome": "REJECTED",
        "reservation_id": None,
    }
    if any(admission.get(key) != value for key, value in expected.items()):
        raise ValueError("RISK_REJECTED durable admission identity differs")
    AuthorityService.historical_admission(authority, admission["admission_id"])
    authority_events = JournalStore.load_events(store, "authority_state", "canonical")
    risks = JournalStore.load_events_by_aggregate_type(store, "risk_decision")
    if (len(authority_events) != 2 or len(risks) != 1
            or [e.get("event_type") for e in authority_events]
            != ["AuthorityPolicyRegistered", "AuthorityAdmissionRecorded"]
            or [e.get("journal_sequence") for e in authority_events] != [5, 7]
            or risks[0].get("journal_sequence") != 6
            or risks[0].get("aggregate_id") != admission.get("risk_decision_id")
            or risks[0].get("payload", {}).get("verdict") != "REJECT"):
        raise ValueError("RISK_REJECTED durable chronology differs")
    economic = DurableProviderEconomicBook(
        store, provider_id=PROVIDER, account_id=ACCOUNT, environment=ENVIRONMENT,
    )
    seed = book_external_cash_flow(
        transaction_id=_uuid("seed-transaction", episode_id),
        cause_event_id=_uuid("seed-cause", episode_id),
        currency="USD", amount=str(INITIAL_CASH),
    )
    if economic.transactions != (seed,):
        raise ValueError("RISK_REJECTED requires exact seed economics")
    checkpoint = _require_initial_reconciliation_checkpoint(store)
    if checkpoint.get("journal_sequence") != 3:
        raise ValueError("RISK_REJECTED initial reconciliation chronology differs")
    expected_counts = {
        "events": 8 if completed else 7, "outbox": 2, "command_dedupe": 2,
        "projection_checkpoints": 0, "global_projection_checkpoints": 0,
    }
    if (cut.get("journal_sequence") != expected_counts["events"]
            or cut.get("counts") != expected_counts
            or JournalStore.whole_store_state_cut(store) != cut):
        raise ValueError("RISK_REJECTED terminal durable state is not exact")
    result = {
        "status": "RISK_REJECTED", "decision": "BUY", "environment": ENVIRONMENT,
        "episode_id": episode_id, "cash": str(economic.cash("USD")),
        "position": str(economic.position(INSTRUMENT)), "reconciled": True,
        "order_id": None, "fill_id": None,
        "reconciliation_event_id": checkpoint["event_id"], "new_outbound_requests": 0,
    }
    result.update(_started_identity(store, episode_id=episode_id))
    return result, cut


def _zero_wire_blocked_projection(
    store: JournalStore,
    root: Path,
    *,
    episode_id: str,
    required_reservation_state: str,
) -> tuple[dict[str, object], DurableReservationBook, str, str, str]:
    (
        attempt_id,
        client_order_id,
        reason,
        blocked_at,
    ) = _require_zero_wire_blocked_submission(
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
    return result, reservations, attempt_id, client_order_id, blocked_at


def _finalize_zero_wire_blocked(
    store: JournalStore,
    root: Path,
    *,
    episode_id: str,
    timestamp: str,
    resumed: bool,
) -> dict[str, object]:
    # The caller timestamp is compatibility-only here. The durable
    # SubmissionBlocked event owns the terminal chronology on every restart.
    _now(timestamp)
    validation_start_cut = JournalStore.whole_store_state_cut(store)
    (
        result,
        reservations,
        attempt_id,
        client_order_id,
        terminal_timestamp,
    ) = _zero_wire_blocked_projection(
        store,
        root,
        episode_id=episode_id,
        required_reservation_state="WORKING",
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
        committed_at=terminal_timestamp,
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
        terminal_timestamp,
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


def _recover_existing_buy_attempt(
    store: JournalStore,
    *,
    episode_id: str,
    decision,
    prepared_request_time: str,
    recovery_now: str,
):
    """Advance only the durable recovery automaton for an existing BUY attempt.

    Re-entering GuardedDispatcher with the exact original attempt/request is safe:
    its existing-attempt path validates immutable submission identity and returns
    before authority_check or transport_send. This advances Prepared/Sending
    crash states without opening a second outbound-send path.
    """

    if decision.side != "BUY":
        raise ValueError("submission recovery requires BUY decision")
    quantity_text = canonical_decimal_text(decision.quantity)
    price_text = canonical_decimal_text(decision.price)
    intent_id = _uuid("intent", episode_id)
    intent_hash = payload_digest({
        "episode_id": episode_id,
        "side": "BUY",
        "quantity": quantity_text,
        "price": price_text,
        "instrument": INSTRUMENT,
    })
    attempt_id = _uuid("attempt", episode_id)

    def _must_not_be_called(*_args, **_kwargs):
        raise AssertionError(
            "existing submission recovery attempted fresh authority or wire I/O"
        )

    return GuardedDispatcher(
        store,
        environment=ENVIRONMENT,
        account_id=ACCOUNT,
        owner_token="canonical-simulation-owner",
    ).dispatch(
        attempt_id=attempt_id,
        intent_id=intent_id,
        intent_hash=intent_hash,
        provider="simulated",
        request={
            "attempt_id": attempt_id,
            "instrument_version": INSTRUMENT,
            "side": "BUY",
            "quantity": quantity_text,
            "price": price_text,
            "now": prepared_request_time,
        },
        now=recovery_now,
        authority_check=_must_not_be_called,
        transport_send=_must_not_be_called,
    )


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
            trade_time=fill["trade_time"], side=fill["side"],
            evidence_refs=(f"simulated:fill:{fill['provider_execution_id']}",),
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
    if type(episode_id) is not str:
        raise TypeError("episode_id must be exact canonical text")
    if not episode_id or episode_id != episode_id.strip():
        raise ValueError("episode_id must be canonical nonempty text")
    if type(fault_after_send) is not bool:
        raise TypeError("fault_after_send must be boolean")
    if now is not None:
        now = _now(now)
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
                expected, _, _, _, _ = _zero_wire_blocked_projection(
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
            elif result.get("status") == "RISK_REJECTED":
                expected, _ = _risk_rejected_projection(
                    store, episode_id=episode_id, completed=True,
                )
                if result != expected:
                    raise ValueError("completed RISK_REJECTED differs from durable zero-wire facts")
            elif result.get("status") == "FILL_RECONCILED_ORDER_UNCONFIRMED":
                artifacts = ArtifactStore(root / "artifacts")
                settlements = DurableSettlementBook(
                    store,
                    provider_id=PROVIDER,
                    account_id=ACCOUNT,
                    environment=ENVIRONMENT,
                    provider_environment=ENVIRONMENT,
                    evidence_artifact_root=root / "artifacts",
                    evidence_artifact_store=artifacts,
                )
                obligations = settlements.obligations
                if (
                    len(obligations) != 1
                    or obligations[0].cause_event_id != result.get("fill_id")
                    or obligations[0].amount >= 0
                ):
                    raise ValueError(
                        "completed BUY session does not match durable settlement provenance"
                    )
                projected = settlements.project(economic)
                if str(projected.available_to_spend("USD")) != result["cash"]:
                    raise ValueError(
                        "completed BUY session settlement capital differs from economics"
                    )
            else:
                raise ValueError("unsupported completed simulation outcome")
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
            authority_events = JournalStore.load_events(store, "authority_state", "canonical")
            if any(e.get("event_type") == "AuthorityAdmissionRecorded"
                   and e.get("payload", {}).get("outcome") == "REJECTED"
                   for e in authority_events):
                result, terminal_cut = _risk_rejected_projection(
                    store, episode_id=episode_id, completed=False,
                )
                _event(store, "SimulationSessionCompleted", episode_id, result,
                       timestamp, expected_cut=terminal_cut)
                return {**result, "resumed": True}
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
            if attempt_events:
                attempt_types = [
                    event.get("event_type") for event in attempt_events
                ]
                if attempt_types in (
                    ["SubmissionPrepared"],
                    ["SubmissionPrepared", "SubmissionSending"],
                ):
                    recovery = _recover_existing_buy_attempt(
                        store,
                        episode_id=episode_id,
                        decision=decision,
                        prepared_request_time=timestamp,
                        recovery_now=timestamp if now is None else _now(now),
                    )
                    if recovery.status == "BLOCKED":
                        return _finalize_zero_wire_blocked(
                            store,
                            root,
                            episode_id=episode_id,
                            timestamp=timestamp,
                            resumed=True,
                        )
                    if recovery.status == "IN_PROGRESS":
                        # Dispatcher lease activity is an internal recovery
                        # phase, not a new product-level simulation status.
                        return {
                            "status": "UNKNOWN",
                            "environment": ENVIRONMENT,
                            "episode_id": episode_id,
                            "reason": recovery.reason,
                            "protocol_identity": protocol_identity,
                            "input_hash": input_hash,
                            "source_build_identity": source_build_identity,
                            "reconciled": False,
                            "resumed": True,
                            "new_outbound_requests": 0,
                        }
                    if recovery.status != "UNKNOWN":
                        raise ValueError(
                            "incomplete submission recovery produced invalid status"
                        )
                    return {
                        "status": "UNKNOWN",
                        "environment": ENVIRONMENT,
                        "episode_id": episode_id,
                        "reason": recovery.reason,
                        "protocol_identity": protocol_identity,
                        "input_hash": input_hash,
                        "source_build_identity": source_build_identity,
                        "reconciled": False,
                        "resumed": True,
                        "new_outbound_requests": 0,
                    }
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
        seed_at = (datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                   - timedelta(microseconds=1)).isoformat().replace("+00:00", "Z")
        plan = economic.prepare_batch_mutation((seed,), committed_at=seed_at)
        if len(economic_events) != 1 or not plan.already_committed:
            raise ValueError("canonical simulation seed bootstrap is invalid")
    else:
        seed_at = (datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                   - timedelta(microseconds=1)).isoformat().replace("+00:00", "Z")
        economic.append(
            seed,
            committed_at=seed_at,
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

    artifacts = ArtifactStore(root / "artifacts")
    settlements = DurableSettlementBook(
        store,
        provider_id=PROVIDER,
        account_id=ACCOUNT,
        environment=ENVIRONMENT,
        provider_environment=ENVIRONMENT,
        evidence_artifact_root=root / "artifacts",
        evidence_artifact_store=artifacts,
    )
    authority = AuthorityService(
        store,
        risk_authority_resolver=resolve_risk,
        settlement_book=settlements,
        economic_book=economic,
    )
    policy_id = _uuid("policy", episode_id)
    authority.register_policy(AuthorityPolicy.create(
        policy_id=policy_id, account_id=ACCOUNT, environments={ENVIRONMENT},
        instruments={(INSTRUMENT_ID, 1)}, actions={"ORDER.SUBMIT"},
        max_notional="1000", expires_at=future, autonomous=True,
        protection_only=False, version=1,
    ), simulation_time=timestamp)
    reservations = DurableReservationBook(
        store, environment=ENVIRONMENT, account_id=ACCOUNT,
        resolution_artifact_store=artifacts,
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
        result, terminal_cut = _risk_rejected_projection(
            store, episode_id=episode_id, completed=False,
        )
        _event(store, "SimulationSessionCompleted", episode_id, result, timestamp,
               expected_cut=terminal_cut)
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
    fill_transaction = book_equity_fill(
        transaction_id=_uuid("fill-transaction", episode_id),
        cause_event_id=fill["provider_execution_id"],
        instrument=fill["instrument_version"],
        settlement_currency="USD",
        side=fill["side"],
        quantity=fill["last_quantity"]["value"],
        price=fill["last_price"],
        fee=fee["amount"],
        fee_currency=fee["currency"],
        economic_effective_at=fill["trade_time"],
        economic_order_key=f"provider:{PROVIDER}:execution:{fill['provider_execution_id']}",
        observed_at=fill["receipt_time"],
    )
    trade_point = datetime.fromisoformat(
        fill["trade_time"].replace("Z", "+00:00")
    ).astimezone(timezone.utc)
    settlement_date = date.fromisoformat(fill["settlement_date"])
    settlement_rule = _simulation_settlement_rule(
        artifacts,
        trade_date=trade_point.date(),
        settlement_date=settlement_date,
    )
    settlement_obligation = equity_cash_obligation_from_transaction(
        fill_transaction,
        obligation_id=_uuid("settlement-obligation", episode_id),
        instrument=INSTRUMENT,
        settlement_currency="USD",
        settlement_date=settlement_date,
        rule_binding=settlement_rule,
    )
    commit_economic_batch_with_reservation_consumption(
        economic, reservations,
        command_id=_uuid("financial-fill-command", episode_id),
        idempotency_key=_uuid("financial-fill-command", episode_id),
        reservation_id=_uuid("reservation", episode_id),
        usage={"CASH:USD": required_text},
        transactions=(fill_transaction,),
        committed_at=timestamp,
        settlement_book=settlements,
        settlement_obligations=(settlement_obligation,),
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


# Multi-episode orchestration shares the existing financial authorities and OMS.
# It owns no ledger, risk engine, strategy, transport or allocation algorithm.
_LOOP_AGGREGATE = "canonical_autonomous_simulation"
_LOOP_PROTOCOL = "provider-free-zero-loop-v7"


def _loop_event(
    store,
    run_id,
    kind,
    key,
    payload,
    now,
    *,
    expected_journal_sequence=None,
):
    cut = (
        store.current_journal_sequence()
        if expected_journal_sequence is None
        else expected_journal_sequence
    )
    if kind == "AutonomousEpisodeCompleted":
        # Deliver existing publications before freezing the completion preimage.
        from .simulation_runtime_checkpoint import (
            COMPLETION_RECEIPT_FIELD,
            deliver_autonomous_owned_publications,
            prepare_autonomous_completion_receipt,
        )
        # Never acknowledge foreign host/financial publications as ZERO output.
        deliver_autonomous_owned_publications(store, run_id=run_id)
        first = store.load_events(_LOOP_AGGREGATE, run_id)[0]
        protocol = first["payload"]["protocol"]
        receipt = prepare_autonomous_completion_receipt(
            Path(store.store_identity.canonical_path).parent, store,
            protocol=protocol, result=payload, event_id=_uuid(kind, f"{run_id}:{key}"),
        )
        if receipt["prior_cut"]["journal_sequence"] != cut:
            raise ValueError("journal sequence changed before completion checkpoint")
        payload = {**payload, COMPLETION_RECEIPT_FIELD: receipt}
    event = {
        "event_id": _uuid(kind, f"{run_id}:{key}"),
        "event_type": kind, "schema_version": "1.0.0",
        "aggregate_type": _LOOP_AGGREGATE, "aggregate_id": run_id,
        "aggregate_version": str(store.next_aggregate_version(_LOOP_AGGREGATE, run_id)),
        "host_id": "local-simulation", "owner_epoch": "1", "environment": ENVIRONMENT,
        "occurred_at": now, "observed_at": now, "committed_at": now,
        "correlation_id": _uuid("loop-correlation", run_id), "causation_id": None,
        "payload": payload, "payload_hash": payload_digest(payload), "evidence_refs": [],
    }
    # Conserve the financial cut even when an unrelated writer races the loop.
    store.commit_command(command_id=event["event_id"], actor="canonical-autonomous-simulation",
        environment=ENVIRONMENT, idempotency_key=event["event_id"], request=payload,
        result={"event_id": event["event_id"]}, state_version=int(event["aggregate_version"]),
        events=[(event, "autotrade.simulation.events")], expected_journal_sequence=cut)
    return event



def _loop_instrument(effective_from):
    from .instruments import InstrumentRegistry, InstrumentVersion
    version = InstrumentVersion(instrument_id=INSTRUMENT_ID, version=1, provider_id=PROVIDER,
        venue_id="internal-simulation", provider_symbol="CANONICAL-SIM", asset_class="CASH_EQUITY",
        base_currency="CANONICAL-SIM", quote_currency="USD", settlement_currency="USD", quantity_unit="SHARE",
        contract_multiplier=Decimal("1"), price_tick=Decimal("0.01"), quantity_step=Decimal("1"),
        minimum_quantity=Decimal("1"),
        maximum_quantity=Decimal("10"), calendar_id="CONTINUOUS_24_7",
        timezone_id="UTC", effective_from=effective_from)
    return InstrumentRegistry(versions=(version,))


def _simulation_settlement_scope() -> SettlementAccountScope:
    return SettlementAccountScope(
        provider_id=PROVIDER,
        account_id=ACCOUNT,
        environment=ENVIRONMENT,
        provider_environment=ENVIRONMENT,
    )


def _simulation_settlement_rule(
    artifacts: ArtifactStore,
    *,
    trade_date: date,
    settlement_date: date,
) -> SettlementRuleBinding:
    """Publish and bind the simulator's explicit contractual cash-settlement rule."""

    if settlement_date != trade_date:
        raise ValueError(
            "canonical simulation same-day settlement policy conflicts with provider fill"
        )

    source_ref = (
        f"simulation:settlement-rule:{_SIMULATION_SETTLEMENT_RULE_ID}:"
        f"{_SIMULATION_SETTLEMENT_RULE_VERSION}"
    )
    scope = _simulation_settlement_scope()
    provisional = SettlementRuleBinding(
        rule_id=_SIMULATION_SETTLEMENT_RULE_ID,
        rule_version=_SIMULATION_SETTLEMENT_RULE_VERSION,
        scope=scope,
        instrument_version=INSTRUMENT,
        settlement_currency="USD",
        effective_from=date(1970, 1, 1),
        effective_to=None,
        evidence_refs=(source_ref,),
    )
    receipt = settlement_rule_evidence_receipt(
        provisional,
        trade_date=trade_date,
        expected_settlement_date=settlement_date,
    )
    receipt_text = canonical_json(receipt)
    artifact_id = str(
        uuid5(
            NAMESPACE_URL,
            "https://evidence.autotrade.local/canonical-simulation/"
            "settlement-rule/" + receipt_text,
        )
    )
    manifest = artifacts.publish_bytes(
        artifact_id=artifact_id,
        data=receipt_text.encode("utf-8"),
        media_type=SETTLEMENT_EVIDENCE_MEDIA_TYPE,
        rights={"storage": True, "export": False},
        source_refs=[source_ref],
        metadata=settlement_rule_evidence_metadata(
            provisional,
            trade_date=trade_date,
            expected_settlement_date=settlement_date,
        ),
    )
    return SettlementRuleBinding(
        rule_id=provisional.rule_id,
        rule_version=provisional.rule_version,
        scope=scope,
        instrument_version=provisional.instrument_version,
        settlement_currency=provisional.settlement_currency,
        effective_from=provisional.effective_from,
        effective_to=provisional.effective_to,
        evidence_refs=(
            source_ref,
            f"artifact:{artifact_id}@{manifest['sha256']}",
        ),
    )


def _simulation_settlement_completion(
    artifacts: ArtifactStore,
    obligation: SettlementObligation,
    *,
    observed_at: datetime,
) -> SettlementEvidence:
    """Materialize one deterministic SIMULATION-only provider settlement fact."""

    source_ref = f"simulation:provider-settlement:{obligation.cause_event_id}"
    provisional = SettlementEvidence(
        obligation_id=obligation.obligation_id,
        evidence_ref=source_ref,
        observed_at=observed_at,
    )
    scope = _simulation_settlement_scope()
    receipt = settlement_completion_evidence_receipt(
        scope=scope,
        obligation=obligation,
        evidence=provisional,
    )
    receipt_text = canonical_json(receipt)
    artifact_id = str(
        uuid5(
            NAMESPACE_URL,
            "https://evidence.autotrade.local/canonical-simulation/"
            "settlement-completion/" + receipt_text,
        )
    )
    manifest = artifacts.publish_bytes(
        artifact_id=artifact_id,
        data=receipt_text.encode("utf-8"),
        media_type=SETTLEMENT_EVIDENCE_MEDIA_TYPE,
        rights={"storage": True, "export": False},
        source_refs=[source_ref],
        metadata=settlement_completion_evidence_metadata(
            scope=scope,
            obligation=obligation,
            evidence=provisional,
        ),
    )
    return SettlementEvidence(
        obligation_id=obligation.obligation_id,
        evidence_ref=f"artifact:{artifact_id}@{manifest['sha256']}",
        observed_at=observed_at,
    )



def _autonomous_fill_financials(artifacts, fill, key):
    """Use identical economics and settlement identities during execution/recovery."""
    fill_id = fill["provider_execution_id"]
    fill_transaction = book_equity_fill(
        transaction_id=_uuid("loop-fill-transaction", key),
        cause_event_id=fill_id,
        instrument=INSTRUMENT,
        settlement_currency="USD",
        side=fill["side"],
        quantity=fill["last_quantity"]["value"],
        price=fill["last_price"],
        fee=fill["fees"][0]["amount"],
        fee_currency="USD",
        economic_effective_at=fill["trade_time"],
        economic_order_key=f"provider:{PROVIDER}:execution:{fill_id}",
        observed_at=fill["receipt_time"],
    )
    trade_point = datetime.fromisoformat(
        fill["trade_time"].replace("Z", "+00:00")
    ).astimezone(timezone.utc)
    settlement_date = date.fromisoformat(fill["settlement_date"])
    settlement_rule = _simulation_settlement_rule(
        artifacts,
        trade_date=trade_point.date(),
        settlement_date=settlement_date,
    )
    settlement_obligation = equity_cash_obligation_from_transaction(
        fill_transaction,
        obligation_id=_uuid("loop-settlement-obligation", key),
        instrument=INSTRUMENT,
        settlement_currency="USD",
        settlement_date=settlement_date,
        rule_binding=settlement_rule,
    )
    return fill_transaction, settlement_obligation


def _retained_autonomous_fills(observed, protocol):
    partials = protocol["execution_profile"] == "TWO_EQUAL_PARTIALS"
    field = "fills" if partials else "fill"
    if set(observed) != {"episode", "protocol_digest", "provider_state", field}:
        raise ValueError("retained fill observation identity differs")
    fills = observed[field] if partials else [observed[field]]
    if type(fills) is not list or len(fills) != (2 if partials else 1) or any(type(f) is not dict for f in fills):
        raise ValueError("retained fill observation identity differs")
    return tuple(fills)


def _commit_autonomous_fill_batch(orders, economic, reservations, settlements, artifacts,
                                fills, key, order_id, reservation_id, timestamp, *,
                                expected_journal_sequence=None):
    """The canonical atomic writer owns each partial execution and its exact usage."""
    for index, fill in enumerate(fills, 1):
        fill_key = key if len(fills) == 1 else f"{key}:part:{index}"
        transaction, obligation = _autonomous_fill_financials(artifacts, fill, fill_key)
        amount = exact_multiply(parse_bounded_exact_decimal(fill["last_quantity"]["value"]),
                                parse_bounded_exact_decimal(fill["last_price"]))
        fee = exact_multiply(amount, FEE_RATE)
        usage = exact_add(amount, fee) if fill["side"] == "BUY" else fee
        commit_order_fill_with_reservation_consumption(
            orders, economic, reservations,
            order_event_key=f"{fill_key}:fill", client_order_id=order_id,
            fill_id=fill["provider_execution_id"], provider_execution_id=fill["provider_execution_id"],
            quantity=fill["last_quantity"]["value"], price=fill["last_price"],
            order_evidence_refs=fill["evidence"], command_id=_uuid("loop-fill-command", fill_key),
            idempotency_key=_uuid("loop-fill-command", fill_key), reservation_id=reservation_id,
            usage={"CASH:USD": usage}, transactions=(transaction,), committed_at=timestamp,
            settlement_book=settlements, settlement_obligations=(obligation,),
            expected_journal_sequence=expected_journal_sequence,
        )
        if expected_journal_sequence is not None:
            expected_journal_sequence = orders.store.current_journal_sequence()

def _apply_due_simulation_settlements(
    settlements: DurableSettlementBook,
    artifacts: ArtifactStore,
    provider: SimulatedProvider,
    *,
    point: datetime,
    committed_at: str,
) -> tuple[str, ...]:
    """Apply only provider settlement evidence that is causally available by point."""

    fills = {
        item["provider_execution_id"]: item
        for item in provider.activity_fills()
    }
    already_settled = settlements.settled_obligation_evidence
    applied: list[str] = []
    for obligation in settlements.obligations:
        if obligation.obligation_id in already_settled:
            continue
        fill = fills.get(obligation.cause_event_id)
        if fill is None:
            raise ValueError(
                "settlement obligation lacks its canonical simulated provider fill"
            )
        trade_point = datetime.fromisoformat(
            fill["trade_time"].replace("Z", "+00:00")
        ).astimezone(timezone.utc)
        observed_at = trade_point - timedelta(microseconds=1) + timedelta(
            seconds=_SIMULATION_SETTLEMENT_EVIDENCE_DELAY_SECONDS
        )
        if observed_at > point:
            continue
        evidence = _simulation_settlement_completion(
            artifacts,
            obligation,
            observed_at=observed_at,
        )
        command_id = _uuid(
            "loop-settlement-completion",
            obligation.obligation_id,
        )
        settlements.apply_settlement(
            evidence,
            as_of=point.date(),
            command_id=command_id,
            idempotency_key=command_id,
            committed_at=committed_at,
        )
        applied.append(obligation.obligation_id)
    return tuple(applied)


def run_autonomous_simulation(
    prices: list[str], state_dir: str | Path, *, run_id: str,
    now: str, stop_after_episodes: int | None = None,
    fault_at_episode: int | None = None, emergency_at_episode: int | None = None,
    execution_profile: str = "IMMEDIATE",
    target_quantity: str = "1",
) -> dict[str, object]:
    """Run/resume a frozen price stream using the canonical SIMULATION authorities.

    Each new observation drives another autonomous decision; repeated BUY signals
    are targets, never repeated incremental exposure. An unfinished episode
    without a retained exact internal fill remains UNKNOWN and blocks continuation
    without a spontaneous resend. A durably observed internal fill may finish only
    through historical admission/send evidence and canonical atomic OMS+finance
    recovery. Completed episodes restore the simulator from its journal snapshot
    and economics from DurableProviderEconomicBook. All evidence remains
    simulation-only.
    """
    from .zero_network import deny_python_network
    from .risk_policy_authority import canonical_risk_policy, risk_policy_digest

    if type(run_id) is not str or not run_id or run_id != run_id.strip():
        raise ValueError("run_id must be canonical nonempty text")
    if type(prices) is not list or not 1 <= len(prices) <= 10000:
        raise ValueError("price stream must contain 1 to 10000 observations")
    values = _prices(prices)
    timestamp = _now(now)
    if type(execution_profile) is not str or execution_profile not in {"IMMEDIATE", "TWO_EQUAL_PARTIALS"}:
        raise ValueError("unsupported frozen simulation execution profile")
    instrument_registry = _loop_instrument(datetime.fromisoformat(timestamp.replace("Z", "+00:00")))
    instrument = instrument_registry.require_tradable(INSTRUMENT_ID, datetime.fromisoformat(timestamp.replace("Z", "+00:00")))
    if type(target_quantity) is not str:
        raise TypeError("frozen target quantity must be exact decimal text")
    target = parse_bounded_exact_decimal(target_quantity)
    instrument.validate_quantity(target)
    if execution_profile == "TWO_EQUAL_PARTIALS":
        instrument.validate_quantity(exact_multiply(target, Decimal("0.5")))
    for price in values:
        instrument.validate_price(price)
    for name, value in (("stop_after_episodes", stop_after_episodes),
                        ("fault_at_episode", fault_at_episode),
                        ("emergency_at_episode", emergency_at_episode)):
        if value is not None and (type(value) is not int or not 1 <= value <= len(values)):
            raise ValueError(f"{name} must be an exact episode index within the stream")
    # Freeze quantitative content, not merely a reusable policy label. Resolve
    # once so preflight and journal registration cannot select different limits.
    selected_policy = canonical_risk_policy(_risk_policy())
    protocol = {
        "protocol": _LOOP_PROTOCOL,
        "execution_profile": execution_profile,
        "target_quantity": canonical_decimal_text(target),
        "clock_order": "SETTLEMENT_AT_EVENT_TIME_THEN_DECISION_PLUS_1US", "run_id": run_id,
        "source_build_identity": _simulation_build_identity(),
        "account": ACCOUNT, "provider": PROVIDER, "environment": ENVIRONMENT,
        # Bind the canonical ZERO financial scope before its durable start event.
        # The runtime checkpoint never infers financial ownership from aliases.
        "financial_scope": {
            "account_id": ACCOUNT, "provider_id": PROVIDER,
            "environment": ENVIRONMENT,
            "instrument_id": INSTRUMENT_ID, "instrument_version": INSTRUMENT,
        },
        "strategy_parameters": {"fast": 2, "slow": 3},
        "prices": [canonical_decimal_text(v) for v in values], "start_time": timestamp,
        "risk_policy": "canonical-provider-free-risk-v1",
        "risk_policy_digest": risk_policy_digest(selected_policy),
        "strategy": "moving-average-2-3-long-only-target-" + canonical_decimal_text(target),
        "fee_rate": canonical_decimal_text(FEE_RATE), "initial_cash": canonical_decimal_text(INITIAL_CASH),
        "settlement_policy": {
            "rule_id": _SIMULATION_SETTLEMENT_RULE_ID,
            "rule_version": _SIMULATION_SETTLEMENT_RULE_VERSION,
            "contractual_cycle": "SAME_DAY",
            "provider_evidence_delay_seconds": _SIMULATION_SETTLEMENT_EVIDENCE_DELAY_SECONDS,
        },
        "fault_at_episode": fault_at_episode, "emergency_at_episode": emergency_at_episode,
        "instrument": instrument.to_contract_dict(),
    }
    root = Path(state_dir)
    if any((root / name).exists() for name in ("checkpoint.json", "learning-evidence.jsonl")):
        raise ValueError("legacy state requires a separate autonomous simulation directory")
    root.mkdir(parents=True, exist_ok=True)
    with deny_python_network(), ResourceLock(root / ".canonical-simulation.lock"):
        return _run_autonomous_locked(root, values, protocol, stop_after_episodes, selected_policy)


def _autonomous_reconciliation(
    store: JournalStore,
    provider: SimulatedProvider,
    economic: DurableProviderEconomicBook,
    protocol: dict[str, object],
    timestamp: str,
    key: str,
    *,
    expected_journal_sequence: int | None = None,
):
    """Reconcile one retained simulator state against canonical economics."""

    snapshot = provider.account_snapshot(now=timestamp)
    fills = tuple(
        ProviderFillEvidence.create(
            provider_id=PROVIDER,
            account_id=ACCOUNT,
            environment=ENVIRONMENT,
            provider_execution_id=fill["provider_execution_id"],
            client_order_id=next(
                order.client_order_id
                for order in provider.orders.values()
                if order.provider_order_id == fill["order_ref"]
            ),
            instrument=fill["instrument_version"],
            quantity=fill["last_quantity"]["value"],
            price=fill["last_price"],
            fee_amount=fill["fees"][0]["amount"],
            fee_currency=fill["fees"][0]["currency"],
            trade_time=fill["trade_time"],
            side=fill["side"],
            evidence_refs=(
                f"simulated:fill:{fill['provider_execution_id']}",
            ),
        )
        for fill in provider.activity_fills()
    )
    result = reconcile_account(
        provider_id=PROVIDER,
        account_id=ACCOUNT,
        environment=ENVIRONMENT,
        local_cash={"USD": economic.cash("USD")},
        provider_cash={"USD": snapshot["balances"][0]["total"]},
        local_positions={INSTRUMENT: economic.position(INSTRUMENT)},
        provider_positions={
            item["instrument_version"]: item["quantity"]["value"]
            for item in snapshot["positions"]
        },
        local_execution_ids=tuple(
            fill.provider_execution_id for fill in fills
        ),
        provider_fills=fills,
        snapshot_consistency=SnapshotConsistencyEvidence(
            provider_id=PROVIDER,
            account_id=ACCOUNT,
            environment=ENVIRONMENT,
            mode="ATOMIC",
            query_started_at=timestamp,
            query_completed_at=timestamp,
        ),
        coverage_start=protocol["start_time"],
        coverage_end=timestamp,
        pagination_complete=True,
        provider_activity_provider_id=PROVIDER,
        provider_activity_account_id=ACCOUNT,
        resource_availability=ResourceAvailabilityEvidence(
            provider_id=PROVIDER,
            account_id=ACCOUNT,
            environment=ENVIRONMENT,
            snapshot_id=snapshot["snapshot_id"],
            query_started_at=timestamp,
            query_completed_at=timestamp,
            valid_until=(
                datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                + timedelta(seconds=60)
            )
            .isoformat()
            .replace("+00:00", "Z"),
            available_resources={
                "CASH:USD": snapshot["balances"][0]["available"],
                f"POSITION:{INSTRUMENT}": canonical_decimal_text(
                    economic.position(INSTRUMENT)
                ),
            },
            provider_as_of=snapshot["provider_as_of"],
            evidence_refs=(
                f"simulated:provider-snapshot:{snapshot['snapshot_id']}",
            ),
        ),
    )
    if not result.complete or result.blocks_new_risk:
        raise ValueError("complete simulated economics did not reconcile")
    expected_checkpoint_payload = reconciliation_payload(
        result,
        observed_at=timestamp,
    )
    expected_checkpoint_payload["checkpoint_owner"] = {
        "host_id": "local-simulation",
        "owner_epoch": "1",
    }
    existing_checkpoint = load_latest_reconciliation_checkpoint(
        store,
        reconciliation_id=key,
        provider_id=PROVIDER,
        account_id=ACCOUNT,
        environment=ENVIRONMENT,
    )
    if (
        existing_checkpoint is not None
        and existing_checkpoint.get("payload")
        != expected_checkpoint_payload
    ):
        raise ValueError(
            "existing autonomous reconciliation checkpoint conflicts"
        )
    checkpoint = record_reconciliation_checkpoint(
        store,
        reconciliation_id=key,
        result=result,
        observed_at=timestamp,
        host_id="local-simulation",
        owner_epoch="1",
        expected_journal_sequence=expected_journal_sequence,
    )
    _deliver_event(
        store,
        checkpoint["event_id"],
        topic="autotrade.reconciliation.events",
        expected_cut=JournalStore.whole_store_state_cut(store),
    )
    return checkpoint, snapshot


def _recover_autonomous_zero_wire_completion(
    store: JournalStore,
    root: Path,
    protocol: dict[str, object],
    active: dict[str, object],
    prior_state: dict[str, object],
) -> None:
    """Complete a durably started episode that provably cannot have sent.

    HOLD/NO_TRADE episodes never enter the risk/admission/order/dispatcher
    branch. Recovery therefore reuses the frozen Started decision, but only
    while canonical economics, OMS, reservations and submission history prove
    that no financial/send-side mutation appeared after that decision.
    """

    from .durable_order_projection import DurableOrderBookProjection

    expected_fields = {
        "episode",
        "decision",
        "financial_cut",
        "protocol_digest",
        "allocation_status",
    }
    if set(active) != expected_fields:
        raise ValueError("zero-wire autonomous start payload is malformed")
    episode = active["episode"]
    decision = active["decision"]
    if type(episode) is not int or not 1 <= episode <= len(protocol["prices"]):
        raise ValueError("zero-wire autonomous episode is invalid")
    if decision not in {"HOLD", "NO_TRADE"}:
        raise ValueError("autonomous episode is not proven zero-wire")
    protocol_digest = autonomous_protocol_digest(protocol)
    if active["protocol_digest"] != protocol_digest:
        raise ValueError("zero-wire autonomous protocol identity differs")
    financial_cut = active["financial_cut"]
    if (
        type(financial_cut) is not int
        or financial_cut < 0
        or financial_cut >= store.current_journal_sequence()
    ):
        raise ValueError("zero-wire autonomous financial cut is invalid")

    proof_cut = store.current_journal_sequence()
    run_id = protocol["run_id"]
    key = f"{run_id}:{episode}"
    timestamp = (
        datetime.fromisoformat(protocol["start_time"].replace("Z", "+00:00"))
        + timedelta(seconds=episode - 1, microseconds=1)
    ).isoformat().replace("+00:00", "Z")

    # The Started cut is the last admissible pre-completion mutation point for
    # a zero-wire episode. A prior exact after-checkpoint is the only additional
    # event that may already exist when a response/crash happened after
    # reconciliation but before AutonomousEpisodeCompleted.
    existing_after = load_latest_reconciliation_checkpoint(
        store,
        reconciliation_id=f"{key}:after",
        provider_id=PROVIDER,
        account_id=ACCOUNT,
        environment=ENVIRONMENT,
    )
    allowed_event_ids = {
        _uuid("AutonomousEpisodeStarted", f"{run_id}:{episode}")
    }
    if existing_after is not None:
        allowed_event_ids.add(existing_after["event_id"])
    post_cut = store.load_events_after_journal_sequence(
        financial_cut,
        limit=max(proof_cut - financial_cut, 1),
    )
    if (
        not post_cut
        or post_cut[0].get("event_id")
        != _uuid("AutonomousEpisodeStarted", f"{run_id}:{episode}")
        or post_cut[0].get("payload") != active
        or any(event.get("event_id") not in allowed_event_ids for event in post_cut)
        or len({event.get("event_id") for event in post_cut}) != len(post_cut)
    ):
        raise ValueError(
            "zero-wire autonomous cut changed after start"
        )

    provider = SimulatedProvider.from_state(prior_state)
    economic = DurableProviderEconomicBook(
        store,
        provider_id=PROVIDER,
        account_id=ACCOUNT,
        environment=ENVIRONMENT,
    )
    if (
        economic.cash("USD") != provider.cash
        or economic.position(INSTRUMENT)
        != provider.positions.get(INSTRUMENT, Decimal("0"))
    ):
        raise ValueError(
            "zero-wire autonomous economics changed after start"
        )
    expected_causes = {
        _uuid("loop-seed-cause", run_id),
        *(
            fill["provider_execution_id"]
            for fill in provider.activity_fills()
        ),
    }
    if (
        len(economic.transactions) != len(expected_causes)
        or {
            transaction.cause_event_id
            for transaction in economic.transactions
        }
        != expected_causes
    ):
        raise ValueError(
            "zero-wire autonomous economic history changed after start"
        )

    artifacts = ArtifactStore(root / "artifacts")
    reservations = DurableReservationBook(
        store,
        environment=ENVIRONMENT,
        account_id=ACCOUNT,
        resolution_artifact_store=artifacts,
        resolution_artifact_root=root / "artifacts",
    )
    if (
        any(record.state == "UNKNOWN" or any(value > 0 for value in record.remaining.values())
            for record in reservations.active())
        or reservations.version != 2 * len(provider.activity_fills())
    ):
        raise ValueError(
            "zero-wire autonomous reservation history changed after start"
        )
    orders = DurableOrderBookProjection(
        store,
        provider_id=PROVIDER,
        account_id=ACCOUNT,
        environment=ENVIRONMENT,
        host_id="local-simulation",
        owner_epoch="1",
    )
    if (
        len(orders.snapshots) != len(provider.orders)
        or any(order.state != "FILLED" for order in orders.snapshots)
        or {
            order.client_order_id
            for order in orders.snapshots
        }
        != set(provider.orders)
    ):
        raise ValueError(
            "zero-wire autonomous OMS history changed after start"
        )
    attempt_id = _uuid("loop-attempt", key)
    if store.load_events(
        "submission_attempt",
        submission_attempt_aggregate_id(
            environment=ENVIRONMENT,
            account_id=ACCOUNT,
            attempt_id=attempt_id,
        ),
    ):
        raise ValueError(
            "zero-wire autonomous episode has submission attempt evidence"
        )
    if store.current_journal_sequence() != proof_cut:
        raise ValueError(
            "zero-wire autonomous journal changed while validating recovery"
        )

    checkpoint, _ = _autonomous_reconciliation(
        store,
        provider,
        economic,
        protocol,
        timestamp,
        f"{key}:after",
        expected_journal_sequence=proof_cut,
    )
    price = parse_bounded_exact_decimal(protocol["prices"][episode - 1])
    equity = exact_add(
        economic.cash("USD"),
        exact_multiply(economic.position(INSTRUMENT), price),
    )
    emergency_at = protocol["emergency_at_episode"]
    emergency = (
        emergency_at is not None
        and episode >= emergency_at
    )
    result = {
        "episode": episode,
        "status": decision,
        "decision": decision,
        "cash": canonical_decimal_text(economic.cash("USD")),
        "position": canonical_decimal_text(
            economic.position(INSTRUMENT)
        ),
        "equity": canonical_decimal_text(equity),
        "reconciled": True,
        "order_id": None,
        "fill_id": None,
        "reconciliation_event_id": checkpoint["event_id"],
        "protocol_digest": protocol_digest,
        "provider_state": provider.export_state(),
        "emergency": emergency,
    }
    _loop_event(
        store,
        run_id,
        "AutonomousEpisodeCompleted",
        str(episode),
        result,
        timestamp,
        expected_journal_sequence=checkpoint["journal_sequence"],
    )
    for item in store.pending_outbox(limit=1000):
        store.mark_outbox_delivered(
            item["outbox_id"],
            expected_envelope_hash=item["envelope_hash"],
        )


def _recover_autonomous_observed_fill(
    store: JournalStore,
    root: Path,
    protocol: dict[str, object],
    active: dict[str, object],
    observed: dict[str, object],
    prior_state: dict[str, object],
) -> None:
    """Finish a retained internal fill without new risk admission or wire I/O.

    Recovery requires one durable simulator state/fill observation, the exact
    historical AuthorityService admission, and a completed dispatcher attempt.
    Missing observations or ambiguous sends remain UNKNOWN. The canonical
    atomic OMS+financial writer owns all recovery mutation.
    """

    from .durable_order_projection import DurableOrderBookProjection
    from .exact_decimal import exact_abs, exact_subtract

    run_id = protocol["run_id"]
    proof_cut = store.current_journal_sequence()
    episode = active["episode"]
    if type(episode) is not int or episode < 1:
        raise ValueError("retained fill episode is invalid")
    key = f"{run_id}:{episode}"
    protocol_digest = autonomous_protocol_digest(protocol)
    retained_fills = _retained_autonomous_fills(observed, protocol)
    if (
        observed["episode"] != episode
        or observed["protocol_digest"] != protocol_digest
        or active.get("protocol_digest") != protocol_digest
        or type(observed["provider_state"]) is not dict
    ):
        raise ValueError("retained fill observation identity differs")

    before = SimulatedProvider.from_state(prior_state)
    provider = SimulatedProvider.from_state(observed["provider_state"])
    decision = active.get("decision")
    if decision not in {"BUY", "REDUCE"}:
        raise ValueError("retained fill lacks a trade decision")
    side = "BUY" if decision == "BUY" else "SELL"
    target = parse_bounded_exact_decimal(protocol["target_quantity"]) if side == "BUY" else Decimal("0")
    prior_position = before.positions.get(INSTRUMENT, Decimal("0"))
    delta = exact_subtract(target, prior_position)
    quantity = exact_abs(delta)
    price = parse_bounded_exact_decimal(protocol["prices"][episode - 1])
    timestamp = (
        datetime.fromisoformat(protocol["start_time"].replace("Z", "+00:00"))
        + timedelta(seconds=episode - 1, microseconds=1)
    ).isoformat().replace("+00:00", "Z")
    intent_id = _uuid("loop-intent", key)
    attempt_id = _uuid("loop-attempt", key)
    order_id = stable_client_order_id(
        "simulated",
        intent_id,
        environment=ENVIRONMENT,
        account_id=ACCOUNT,
    )
    order = provider.orders.get(order_id)
    if order is None or quantity <= 0 or ((side == "BUY") != (delta > 0)):
        raise ValueError("retained fill order/target differs")

    fills = provider.activity_fills()
    selected = [
        fill for fill in fills if fill["order_ref"] == order.provider_order_id
    ]
    if (
        tuple(selected) != retained_fills
        or tuple(
            fill
            for fill in fills
            if fill["order_ref"] != order.provider_order_id
        )
        != before.activity_fills()
        or set(provider.orders) != set(before.orders) | {order_id}
        or any(
            provider.orders[name] != original
            for name, original in before.orders.items()
        )
        or provider.outbound_request_count
        != before.outbound_request_count + 1
    ):
        raise ValueError("retained simulator history differs")

    part_quantity = exact_multiply(quantity, Decimal("0.5")) if len(selected) == 2 else quantity
    for index, fill in enumerate(selected, 1):
        fee = exact_multiply(exact_multiply(part_quantity, price), FEE_RATE)
        if (
            fill["instrument_version"] != INSTRUMENT or fill["side"] != side
            or parse_bounded_exact_decimal(fill["last_quantity"]["value"]) != part_quantity
            or parse_bounded_exact_decimal(fill["last_price"]) != price
            or fill["trade_time"] != timestamp
            or fill["fees"] != [{"amount": canonical_decimal_text(fee), "currency": "USD"}]
            or (len(selected) == 2 and fill["provider_execution_id"] !=
                _uuid("loop-partial-execution", f"{key}:part:{index}"))
        ):
            raise ValueError("retained fill economics differ from frozen request")

    intent_hash = payload_digest(
        {
            "protocol_digest": protocol_digest,
            "episode": episode,
            "side": side,
            "quantity": canonical_decimal_text(quantity),
            "price": canonical_decimal_text(price),
            "instrument": INSTRUMENT,
            "allocation": {
                "target": canonical_decimal_text(target),
            },
        }
    )
    admission_id = _uuid("loop-admission", key)
    reservation_id = _uuid("loop-reservation", key)
    admission = AuthorityService(store).historical_admission(admission_id)
    expected_admission = {
        "outcome": "ADMITTED",
        "admission_id": admission_id,
        "intent_id": intent_id,
        "intent_hash": intent_hash,
        "reservation_id": reservation_id,
        "account_id": ACCOUNT,
        "environment": ENVIRONMENT,
        "action": "ORDER.SUBMIT",
        "instrument": {"instrument_id": INSTRUMENT_ID, "version": 1},
        "notional": canonical_decimal_text(
            exact_multiply(quantity, price)
        ),
        "financial_command_id": _uuid("loop-financial-command", key),
    }
    if any(
        admission.get(name) != value
        for name, value in expected_admission.items()
    ):
        raise ValueError("retained fill historical admission differs")

    def forbidden(*_args, **_kwargs):
        raise AssertionError(
            "observed-fill recovery cannot issue authority or send"
        )

    request = {
        "attempt_id": attempt_id,
        "instrument_version": INSTRUMENT,
        "side": side,
        "quantity": canonical_decimal_text(quantity),
        "price": canonical_decimal_text(price),
        "now": timestamp,
    }
    if len(selected) == 2:
        request["fill_immediately"] = False
    replay = GuardedDispatcher(
        store,
        environment=ENVIRONMENT,
        account_id=ACCOUNT,
        owner_token="canonical-simulation-owner",
    ).dispatch(
        attempt_id=attempt_id,
        intent_id=intent_id,
        intent_hash=intent_hash,
        provider="simulated",
        request=request,
        now=timestamp,
        authority_check=forbidden,
        transport_send=forbidden,
    )
    if (
        replay.status != "SENT"
        or replay.response.get("provider_order_id")
        != order.provider_order_id
    ):
        raise ValueError(
            "retained fill lacks exact completed send evidence"
        )

    economic = DurableProviderEconomicBook(
        store,
        provider_id=PROVIDER,
        account_id=ACCOUNT,
        environment=ENVIRONMENT,
    )
    financial_state = (
        economic.cash("USD"),
        economic.position(INSTRUMENT),
    )
    before_financial = (
        before.cash,
        prior_position,
    )
    after_financial = (
        provider.cash,
        provider.positions.get(INSTRUMENT, Decimal("0")),
    )
    expected_causes = {
        _uuid("loop-seed-cause", run_id),
        *(
            prior_fill["provider_execution_id"]
            for prior_fill in before.activity_fills()
        ),
    }
    recorded_causes = {transaction.cause_event_id for transaction in economic.transactions}
    projected_cash, projected_position = before_financial
    valid_financial_state = financial_state == before_financial
    valid_prefix = (financial_state == before_financial and recorded_causes == expected_causes
                    and len(economic.transactions) == len(expected_causes))
    for fill in selected:
        fill_quantity = parse_bounded_exact_decimal(fill["last_quantity"]["value"])
        notional = exact_multiply(fill_quantity, price)
        fee = exact_multiply(notional, FEE_RATE)
        signed_quantity = fill_quantity if side == "BUY" else exact_subtract(Decimal("0"), fill_quantity)
        cash_delta = exact_subtract(Decimal("0"), exact_add(notional, fee)) if side == "BUY" else exact_subtract(notional, fee)
        projected_cash = exact_add(projected_cash, cash_delta)
        projected_position = exact_add(projected_position, signed_quantity)
        valid_financial_state = valid_financial_state or financial_state == (projected_cash, projected_position)
        expected_causes.add(fill["provider_execution_id"])
        valid_prefix = valid_prefix or (
            financial_state == (projected_cash, projected_position)
            and recorded_causes == expected_causes and len(economic.transactions) == len(expected_causes))
    if (projected_cash, projected_position) != after_financial or not valid_financial_state:
        raise ValueError("retained fill conflicts with canonical economic state")
    if not valid_prefix:
        raise ValueError("retained fill conflicts with canonical economic history")

    artifacts = ArtifactStore(root / "artifacts")
    settlements = DurableSettlementBook(
        store, provider_id=PROVIDER, account_id=ACCOUNT,
        environment=ENVIRONMENT, provider_environment=ENVIRONMENT,
        evidence_artifact_root=root / "artifacts", evidence_artifact_store=artifacts,
    )
    reservations = DurableReservationBook(
        store,
        environment=ENVIRONMENT,
        account_id=ACCOUNT,
        resolution_artifact_store=artifacts,
        resolution_artifact_root=root / "artifacts",
    )
    orders = DurableOrderBookProjection(
        store,
        provider_id=PROVIDER,
        account_id=ACCOUNT,
        environment=ENVIRONMENT,
        host_id="local-simulation",
        owner_epoch="1",
    )
    if store.current_journal_sequence() != proof_cut:
        raise ValueError(
            "journal changed while validating retained fill"
        )

    fill_id = fill["provider_execution_id"]
    _commit_autonomous_fill_batch(orders, economic, reservations, settlements, artifacts,
        tuple(selected), key, order_id, reservation_id, timestamp, expected_journal_sequence=proof_cut)
    if (
        economic.cash("USD"),
        economic.position(INSTRUMENT),
    ) != after_financial or orders.order(order_id).state != "FILLED":
        raise ValueError(
            "retained fill recovery did not reconcile financial owners"
        )

    checkpoint, _ = _autonomous_reconciliation(
        store,
        provider,
        economic,
        protocol,
        timestamp,
        f"{key}:after",
    )
    equity = exact_add(
        economic.cash("USD"),
        exact_multiply(economic.position(INSTRUMENT), price),
    )
    result = {
        "episode": episode,
        "status": "FILLED",
        "decision": decision,
        "cash": canonical_decimal_text(economic.cash("USD")),
        "position": canonical_decimal_text(
            economic.position(INSTRUMENT)
        ),
        "equity": canonical_decimal_text(equity),
        "reconciled": True,
        "order_id": order_id,
        "fill_id": fill_id,
        "reconciliation_event_id": checkpoint["event_id"],
        "protocol_digest": protocol_digest,
        "provider_state": provider.export_state(),
        "emergency": False,
    }
    _loop_event(
        store,
        run_id,
        "AutonomousEpisodeCompleted",
        str(episode),
        result,
        timestamp,
    )
    for item in store.pending_outbox(limit=1000):
        store.mark_outbox_delivered(
            item["outbox_id"],
            expected_envelope_hash=item["envelope_hash"],
        )


def _run_autonomous_locked(root, values, protocol, stop_after_episodes, selected_policy):
    from .allocation import AllocationCandidate, AllocationPolicy, StressScenarioEvidence, allocate_targets
    from .durable_order_projection import DurableOrderBookProjection
    from .exact_decimal import exact_abs, exact_subtract, exact_sum, as_fraction, round_fraction_to_quantum
    from .risk_policy_authority import DurableRiskPolicyRegistry, RiskPolicyScope
    from .valuation_authority import DurableValuationBook, diagnostic_mark_observation, evaluate_valuation_freshness

    store = JournalStore(root / "journal.sqlite3")
    run_id = protocol["run_id"]
    events = store.load_events(_LOOP_AGGREGATE, run_id)
    from .simulation_runtime_checkpoint import (
        _issue_autonomous_runtime_authority_key,
        _require_autonomous_runtime_authority_key,
    )
    protocol = dict(protocol)
    if not events:
        key_identity = _issue_autonomous_runtime_authority_key(root)
        protocol["runtime_authority_key_sha256"] = key_identity
    else:
        first = events[0]
        if (
            first["event_type"] != "AutonomousSimulationStarted"
            or type(first.get("payload")) is not dict
            or type(first["payload"].get("protocol")) is not dict
        ):
            raise ValueError("autonomous simulation start authority is invalid")
        key_identity = first["payload"]["protocol"].get(
            "runtime_authority_key_sha256"
        )
        _require_autonomous_runtime_authority_key(root, key_identity)
        protocol["runtime_authority_key_sha256"] = key_identity
    protocol_digest = autonomous_protocol_digest(protocol)
    started_at = datetime.fromisoformat(protocol["start_time"].replace("Z", "+00:00"))
    instruments = _loop_instrument(started_at)
    if instruments.require_tradable(INSTRUMENT_ID, started_at).to_contract_dict() != protocol["instrument"]:
        raise ValueError("frozen instrument identity differs")
    if not events:
        if store.current_journal_sequence() != 0:
            raise ValueError("autonomous simulation requires an empty or matching owned journal")
        provider = SimulatedProvider(account_id=ACCOUNT, initial_cash=str(INITIAL_CASH), fee_rate=str(FEE_RATE))
        _loop_event(store, run_id, "AutonomousSimulationStarted", "start", {
            "protocol_digest": protocol_digest, "protocol": protocol,
            "provider_state": provider.export_state(),
        }, protocol["start_time"])
        events = store.load_events(_LOOP_AGGREGATE, run_id)
    if (
        events[0]["event_type"] != "AutonomousSimulationStarted"
        or events[0]["payload"]["protocol_digest"] != protocol_digest
        or events[0]["payload"]["protocol"] != protocol
    ):
        raise ValueError("autonomous simulation protocol/input identity changed")
    completed = []
    active = None
    observed = None
    for event in events[1:]:
        payload = event["payload"]
        if event["event_type"] == "AutonomousEpisodeStarted":
            if active is not None or payload["episode"] != len(completed) + 1:
                raise ValueError("autonomous episode chronology conflicts")
            active = payload
            observed = None
        elif event["event_type"] == "AutonomousEpisodeFillObserved":
            if (
                active is None
                or observed is not None
                or payload["episode"] != active["episode"]
            ):
                raise ValueError(
                    "autonomous fill observation chronology conflicts"
                )
            observed = payload
        elif event["event_type"] == "AutonomousEpisodeCompleted":
            if active is None or payload["episode"] != active["episode"]:
                raise ValueError("autonomous completion lacks matching start")
            completed.append({k: v for k, v in payload.items()
                              if k != "runtime_checkpoint_completion_receipt"})
            active = None
            observed = None
        else:
            raise ValueError("unsupported autonomous simulation event")
    if active is not None:
        prior_state = (
            completed[-1]["provider_state"]
            if completed
            else events[0]["payload"]["provider_state"]
        )
        if observed is not None:
            _recover_autonomous_observed_fill(
                store,
                root,
                protocol,
                active,
                observed,
                prior_state,
            )
            recovered_completed = [{k: v for k, v in event["payload"].items()
                                    if k != "runtime_checkpoint_completion_receipt"}
                                   for event in store.load_events(_LOOP_AGGREGATE, run_id)
                                   if event["event_type"] == "AutonomousEpisodeCompleted"]
            from .simulation_runtime_checkpoint import persist_autonomous_runtime_checkpoint
            persist_autonomous_runtime_checkpoint(
                root, store, protocol=protocol, authority_key_identity=key_identity,
                completed=recovered_completed,
            )
            return _run_autonomous_locked(
                root,
                values,
                protocol,
                stop_after_episodes,
                selected_policy,
            )
        if active.get("decision") in {"HOLD", "NO_TRADE"}:
            _recover_autonomous_zero_wire_completion(
                store,
                root,
                protocol,
                active,
                prior_state,
            )
            recovered_completed = [{k: v for k, v in event["payload"].items()
                                    if k != "runtime_checkpoint_completion_receipt"}
                                   for event in store.load_events(_LOOP_AGGREGATE, run_id)
                                   if event["event_type"] == "AutonomousEpisodeCompleted"]
            from .simulation_runtime_checkpoint import persist_autonomous_runtime_checkpoint
            persist_autonomous_runtime_checkpoint(
                root, store, protocol=protocol, authority_key_identity=key_identity,
                completed=recovered_completed,
            )
            return _run_autonomous_locked(
                root,
                values,
                protocol,
                stop_after_episodes,
                selected_policy,
            )
        return {"status": "UNKNOWN", "environment": ENVIRONMENT, "mode": "ZERO",
                "run_id": run_id, "completed_episodes": len(completed),
                "unresolved_episode": active["episode"], "new_outbound_requests": 0,
                "reason": "unfinished_episode_requires_reconciliation", "resumed": True}
    if completed:
        from .simulation_runtime_checkpoint import repair_autonomous_completion_checkpoint
        repair_autonomous_completion_checkpoint(root, store, protocol=protocol, completed=completed)
    if completed:
        # Missing, malformed or causally stale checkpoint bytes are rejected
        # before rebuilding any provider projection.  Full cross-authority
        # equality is checked below after owner-specific recovery diagnostics.
        from .simulation_runtime_checkpoint import preflight_autonomous_runtime_checkpoint
        preflight_autonomous_runtime_checkpoint(
            root,
            protocol=protocol,
            authority_key_identity=key_identity,
            completed_episodes=len(completed),
        )
    state = completed[-1]["provider_state"] if completed else events[0]["payload"]["provider_state"]
    provider = SimulatedProvider.from_state(state)
    economic = DurableProviderEconomicBook(store, provider_id=PROVIDER, account_id=ACCOUNT, environment=ENVIRONMENT)
    economic.append(book_external_cash_flow(
        transaction_id=_uuid("loop-seed-transaction", run_id), cause_event_id=_uuid("loop-seed-cause", run_id),
        currency="USD", amount=protocol["initial_cash"]), committed_at=protocol["start_time"])
    if provider.cash != economic.cash("USD") or provider.positions.get(INSTRUMENT, Decimal("0")) != economic.position(INSTRUMENT):
        raise ValueError("simulator snapshot conflicts with canonical economic state")
    artifacts = ArtifactStore(root / "artifacts")
    settlements = DurableSettlementBook(
        store,
        provider_id=PROVIDER,
        account_id=ACCOUNT,
        environment=ENVIRONMENT,
        provider_environment=ENVIRONMENT,
        evidence_artifact_root=root / "artifacts",
        evidence_artifact_store=artifacts,
    )
    reservations = DurableReservationBook(store, environment=ENVIRONMENT, account_id=ACCOUNT,
        resolution_artifact_store=artifacts, resolution_artifact_root=root / "artifacts")
    orders = DurableOrderBookProjection(store, provider_id=PROVIDER, account_id=ACCOUNT,
        environment=ENVIRONMENT, host_id="local-simulation", owner_epoch="1")
    if completed:
        # Preserve the owning authorities' more specific fail-closed diagnoses
        # before applying the cross-authority common-cut gate.  These reads do
        # not expose another market event or permit a send.
        if any(
            record.state == "UNKNOWN"
            or any(value > 0 for value in record.remaining.values())
            for record in reservations.active()
        ):
            raise ValueError("pending/UNKNOWN reservations block a new financial cut")
        if any(order.state != "FILLED" for order in orders.snapshots):
            raise ValueError("pending/UNKNOWN OMS obligations block a new financial cut")
        from .simulation_runtime_checkpoint import verify_autonomous_runtime_checkpoint
        verify_autonomous_runtime_checkpoint(
            root,
            store,
            protocol=protocol,
            authority_key_identity=key_identity,
            completed=completed,
        )
    scope = RiskPolicyScope(PROVIDER, ACCOUNT, ENVIRONMENT, ENVIRONMENT, "internal-simulator-v1", "CASH_EQUITY")
    registry = DurableRiskPolicyRegistry(store)
    policy = selected_policy
    registry.register(scope=scope, policy_id="canonical-provider-free-risk-v1", version=1,
        policy=policy, committed_at=started_at)
    if not completed:
        registry.activate(scope=scope, policy_id="canonical-provider-free-risk-v1", version=1, committed_at=started_at)
    valuations = DurableValuationBook(store)
    count_before = provider.outbound_request_count
    previous_equities = [parse_bounded_exact_decimal(item["equity"]) for item in completed]
    end = len(values) if stop_after_episodes is None else stop_after_episodes

    def reconciliation_at(timestamp, key):
        return _autonomous_reconciliation(
            store,
            provider,
            economic,
            protocol,
            timestamp,
            key,
        )

    authority = None
    for index in range(len(completed), end):
        episode = index + 1
        key = f"{run_id}:{episode}"
        point = started_at + timedelta(seconds=index)
        instrument = instruments.require_tradable(INSTRUMENT_ID, point)
        instrument.validate_price(values[index])
        timestamp = (point + timedelta(microseconds=1)).isoformat().replace("+00:00", "Z")
        future = (point + timedelta(seconds=60)).isoformat().replace("+00:00", "Z")
        _apply_due_simulation_settlements(
            settlements,
            artifacts,
            provider,
            point=point,
            committed_at=point.isoformat().replace("+00:00", "Z"),
        )
        if any(record.state == "UNKNOWN" or any(value > 0 for value in record.remaining.values())
               for record in reservations.active()):
            raise ValueError("pending/UNKNOWN reservations block a new financial cut")
        if any(order.state != "FILLED" for order in orders.snapshots):
            raise ValueError("pending/UNKNOWN OMS obligations block a new financial cut")
        checkpoint, account_snapshot = reconciliation_at(timestamp, f"{key}:before")
        predecessor = None if index == 0 else valuations.resolve_at(
            journal_sequence_cut=store.current_journal_sequence(), as_of=point, kind="MARK",
            provider_id=PROVIDER, account_id=ACCOUNT, environment=ENVIRONMENT,
            provider_environment=ENVIRONMENT, route_policy_id=scope.entity_policy_id,
            instrument_version=INSTRUMENT, require_production=False).observation_id
        mark = diagnostic_mark_observation(provider_id=PROVIDER, account_id=ACCOUNT,
            environment=ENVIRONMENT, provider_environment=ENVIRONMENT, route_policy_id=scope.entity_policy_id,
            capability_snapshot_id="simulated-capability-v1", provider_qualification_id="internal-simulation-only",
            adapter_build_id="internal-simulator-v1", source_revision=protocol_digest,
            source_event_at=point, available_at=point, observed_at=point,
            freshness_rule_id="canonical-provider-free-risk-v1", origin_binding_id=f"simulated:{key}",
            query_digest=payload_digest({"episode": episode}), response_sha256=payload_digest({"price": str(values[index])}),
            instrument_version=INSTRUMENT, mark=values[index], supersedes_observation_id=predecessor)
        valuations.record(mark)
        position = economic.position(INSTRUMENT)
        cash = economic.cash("USD")
        available_cash = settlements.project(economic).available_to_spend("USD")
        price = mark.mark
        equity = exact_add(cash, exact_multiply(position, price))
        peak = max([INITIAL_CASH, equity, *previous_equities])
        drawdown = round_fraction_to_quantum(as_fraction(exact_subtract(peak, equity)) / as_fraction(peak),
                                             Decimal("0.000000000000000001"), mode="CEILING")
        frozen_target = parse_bounded_exact_decimal(protocol["target_quantity"])
        proposal = MovingAverageStrategy(**protocol["strategy_parameters"]).decide(values[:episode], frozen_target)
        target_quantity = frozen_target if proposal.side == "BUY" else Decimal("0") if proposal.side == "SELL" else position
        decision = "BUY" if target_quantity > position else "REDUCE" if target_quantity < position else "HOLD" if proposal.side == "HOLD" else "NO_TRADE"
        emergency = protocol["emergency_at_episode"] is not None and episode >= protocol["emergency_at_episode"]
        if emergency:
            decision = "NO_TRADE"
            target_quantity = position
        allocation = allocate_targets((AllocationCandidate.create(symbol=INSTRUMENT,
            desired_notional=exact_multiply(target_quantity, price), price=price, lot_size=instrument.quantity_step,
            current_quantity=position, cost_rate=FEE_RATE, turnover_cost_rate=FEE_RATE,
            holding_cost_rate="0", max_executable_notional="1000"),),
            AllocationPolicy.create(cash_available=exact_subtract(available_cash, reservations.total_reserved("CASH:USD")),
                max_gross_notional="1000", max_net_notional="1000", max_symbol_notional="1000",
                max_total_cost="10", max_stress_loss="500", max_turnover_notional="1000"),
            stress_evidence=(StressScenarioEvidence.create(name="adverse-quarter", shocks={INSTRUMENT: "-0.25"},
                observed_at=timestamp, valid_until=future, source_ref="simulation:fixed-stress-v1"),), decision_time=timestamp)
        quantity = exact_subtract(allocation.targets[0].quantity, position) if allocation.targets else Decimal("0")
        if allocation.status != "ALLOCATED" or quantity == 0:
            decision = "HOLD" if decision == "HOLD" else "NO_TRADE"
        _loop_event(store, run_id, "AutonomousEpisodeStarted", str(episode), {
            "episode": episode, "decision": decision, "financial_cut": store.current_journal_sequence(),
            "protocol_digest": protocol_digest, "allocation_status": allocation.status,
        }, timestamp)
        status = decision
        order_id = fill_id = None
        if quantity != 0 and allocation.status == "ALLOCATED":
            side = "BUY" if quantity > 0 else "SELL"
            instrument.validate_quantity(exact_abs(quantity))
            amount = exact_multiply(exact_abs(quantity), price)
            required = exact_add(amount, exact_multiply(amount, FEE_RATE)) if side == "BUY" else exact_multiply(amount, FEE_RATE)
            # REDUCE never spends unfilled sale proceeds. Canonical hard risk
            # verifies owned inventory; the sequential OMS fence holds every
            # unresolved order, while the reservation covers its cash fee.
            resource = "CASH:USD"
            context = RiskContext.create(state_version=episode, equity=equity,
                positions={INSTRUMENT: position}, marks={INSTRUMENT: price}, reserved_position_delta={},
                daily_pnl=exact_subtract(equity, INITIAL_CASH), drawdown_fraction=drawdown,
                market_data_age_seconds="0", fx_age_seconds={"USD": "0"}, margin_headroom="1",
                capability_allowed=True, borrow_available=True, stress_scenarios=({INSTRUMENT: "-0.25"},),
                instrument_types={INSTRUMENT: "EQUITY"})

            def resolve_risk(request):
                if economic.cash("USD") != cash or economic.position(INSTRUMENT) != position:
                    raise ValueError("canonical economics changed during risk composition")
                resolved = request.resolved_risk_policy
                selected = valuations.resolve_at(journal_sequence_cut=request.journal_sequence_cut,
                    as_of=point, kind="MARK", provider_id=PROVIDER, account_id=ACCOUNT, environment=ENVIRONMENT,
                    provider_environment=ENVIRONMENT, route_policy_id=scope.entity_policy_id,
                    instrument_version=INSTRUMENT, require_production=False)
                fresh = evaluate_valuation_freshness(selected, resolved, as_of=point,
                    journal_sequence_cut=request.journal_sequence_cut)
                refs = {name: f"simulated:{name.lower()}:{key}" for name in
                        ("PORTFOLIO", "MARGIN", "RECONCILIATION", "CAPABILITY", "BORROW", "STRESS")}
                refs.update(POLICY=resolved.registration_event_id, MARKET=selected.observation_id + ":" + fresh.evidence_digest)
                return AuthoritativeRiskSnapshot(context=context, risk_policy=resolved.policy,
                    resolved_risk_policy=resolved, provider_environment=request.provider_environment,
                    entity_policy_id=request.entity_policy_id, instrument_family=request.instrument_family,
                    account_id=request.account_id, environment=request.environment, provider_id=request.provider_id,
                    instrument_version=request.instrument_version, capability_snapshot_id=request.capability_snapshot_id,
                    reconciliation_checkpoint_event_id=request.reconciliation_checkpoint_event_id,
                    journal_sequence_cut=request.journal_sequence_cut, reservation_version=request.reservation_version,
                    reservation_state_digest=request.reservation_state_digest, authority_policy_id=request.authority_policy_id,
                    authority_policy_version=request.authority_policy_version, evaluated_at=request.evaluated_at,
                    valid_until=future, evidence_refs=refs)

            if authority is None:
                authority = AuthorityService(
                    store,
                    risk_policy_scope=scope,
                    risk_authority_resolver=resolve_risk,
                    settlement_book=settlements,
                    economic_book=economic,
                )
            else:
                # Resolver injection is already a SIMULATION-only composition
                # seam; keep the sealed store/capital authority unchanged.
                authority.risk_authority_resolver = resolve_risk
            policy_id = _uuid("loop-authority-policy", key)
            authority.register_policy(AuthorityPolicy.create(policy_id=policy_id, account_id=ACCOUNT,
                environments={ENVIRONMENT}, instruments={(INSTRUMENT_ID, 1)}, actions={"ORDER.SUBMIT"},
                max_notional="1000", expires_at=future, autonomous=True, protection_only=False, version=1),
                simulation_time=timestamp)
            intent_id = _uuid("loop-intent", key)
            intent_hash = payload_digest({"protocol_digest": protocol_digest, "episode": episode,
                "side": side, "quantity": canonical_decimal_text(exact_abs(quantity)), "price": canonical_decimal_text(price),
                "instrument": INSTRUMENT, "allocation": {"target": canonical_decimal_text(allocation.targets[0].quantity)}})
            admission_id = _uuid("loop-admission", key)
            reservation_id = _uuid("loop-reservation", key)
            admission = authority.admit(command_id=_uuid("loop-financial-command", key),
                idempotency_key=_uuid("loop-financial-command", key), admission_id=admission_id, policy_id=policy_id,
                intent_id=intent_id, intent_hash=intent_hash, account_id=ACCOUNT, environment=ENVIRONMENT,
                instrument_id=INSTRUMENT_ID, instrument_version=1, action="ORDER.SUBMIT", notional=amount,
                capability_snapshot_id="simulated-capability-v1", risk_intent=RiskIntent.create(symbol=INSTRUMENT,
                    side=side, quantity=exact_abs(quantity), price=price, expected_state_version=episode,
                    instrument_type="EQUITY", reduce_only=side == "SELL", action="REDUCE" if side == "SELL" else "TRADE"),
                risk_context=context, risk_policy=policy, risk_valid_until=future, reservation_book=reservations,
                reservation_id=reservation_id, reservation_requirements={resource: required},
                reservation_available={resource: account_snapshot["balances"][0]["available"]},
                reservation_checkpoint_event_id=checkpoint["event_id"], reservation_provider_id=PROVIDER,
                reservation_max_age_seconds="60", now=timestamp)
            if admission.outcome != "ADMITTED":
                status = "RISK_REJECTED"
            else:
                attempt_id = _uuid("loop-attempt", key)
                order_id = stable_client_order_id("simulated", intent_id, environment=ENVIRONMENT, account_id=ACCOUNT)
                prepared = orders.create_order(event_key=f"{key}:intent", client_order_id=order_id,
                    instrument=INSTRUMENT, side=side, requested_quantity=exact_abs(quantity), committed_at=timestamp)
                _deliver_event(store, prepared.event_id,
                    topic="autotrade.order-projection.events",
                    expected_cut=JournalStore.whole_store_state_cut(store))
                request = {"attempt_id": attempt_id, "instrument_version": INSTRUMENT, "side": side,
                    "quantity": canonical_decimal_text(exact_abs(quantity)), "price": canonical_decimal_text(price), "now": timestamp}
                if protocol["execution_profile"] == "TWO_EQUAL_PARTIALS":
                    request["fill_immediately"] = False
                frozen_request = dict(request)

                def final_check(candidate_hash, current_time):
                    if request != frozen_request:
                        return False, "financial_request_changed"
                    return authority.dispatch_allowed(admission_id, intent_hash=candidate_hash, account_id=ACCOUNT,
                        environment=ENVIRONMENT, instrument_id=INSTRUMENT_ID, instrument_version=1,
                        action="ORDER.SUBMIT", now=current_time, capability_snapshot_id="simulated-capability-v1")

                if protocol["fault_at_episode"] == episode:
                    provider._transport_faults[order_id] = "AFTER_ACCEPT_RESPONSE_LOST"
                dispatched = GuardedDispatcher(store, environment=ENVIRONMENT, account_id=ACCOUNT,
                    owner_token="canonical-simulation-owner").dispatch(attempt_id=attempt_id, intent_id=intent_id,
                    intent_hash=intent_hash, provider="simulated", request=request, now=timestamp,
                    authority_check=final_check, transport_send=provider.transport_send)
                orders.sync_submission_attempt(attempt_id=attempt_id)
                if dispatched.status != "SENT":
                    reservations.mark_unknown(command_id=_uuid("loop-unknown", key), idempotency_key=_uuid("loop-unknown", key),
                                              reservation_id=reservation_id)
                    return {"status": "UNKNOWN", "environment": ENVIRONMENT, "mode": "ZERO", "run_id": run_id,
                            "completed_episodes": len(completed), "unresolved_episode": episode,
                            "new_outbound_requests": provider.outbound_request_count - count_before, "resumed": bool(completed)}
                # Acknowledgement has only changed OMS acceptance. Fill history owns quantity.
                if protocol["execution_profile"] == "TWO_EQUAL_PARTIALS":
                    part_quantity = exact_multiply(exact_abs(quantity), Decimal("0.5"))
                    instrument.validate_quantity(part_quantity)
                    for part in (1, 2):
                        provider.record_fill(client_order_id=order_id,
                            provider_execution_id=_uuid("loop-partial-execution", f"{key}:part:{part}"),
                            quantity=part_quantity, now=timestamp, price=price)
                fresh_fills = [f for f in provider.activity_fills() if f["order_ref"] == dispatched.response["provider_order_id"]]
                if len(fresh_fills) != (2 if protocol["execution_profile"] == "TWO_EQUAL_PARTIALS" else 1):
                    raise ValueError("ACK is not fill evidence")
                fill = fresh_fills[-1]
                fill_id = fill["provider_execution_id"]
                _loop_event(
                    store,
                    run_id,
                    "AutonomousEpisodeFillObserved",
                    str(episode),
                    {
                        "episode": episode,
                        "protocol_digest": protocol_digest,
                        "provider_state": provider.export_state(),
                        **({"fills": fresh_fills} if len(fresh_fills) == 2 else {"fill": fill}),
                    },
                    timestamp,
                )
                _commit_autonomous_fill_batch(orders, economic, reservations, settlements, artifacts,
                    tuple(fresh_fills), key, order_id, reservation_id, timestamp)
                if orders.order(order_id).state != "FILLED":
                    raise ValueError("canonical OMS has not confirmed complete fill")
                status = "FILLED"
        after_checkpoint, _ = reconciliation_at(timestamp, f"{key}:after")
        equity = exact_add(economic.cash("USD"), exact_multiply(economic.position(INSTRUMENT), price))
        result = {"episode": episode, "status": status, "decision": decision,
            "cash": canonical_decimal_text(economic.cash("USD")), "position": canonical_decimal_text(economic.position(INSTRUMENT)),
            "equity": canonical_decimal_text(equity), "reconciled": True, "order_id": order_id, "fill_id": fill_id,
            "reconciliation_event_id": after_checkpoint["event_id"], "protocol_digest": protocol_digest,
            "provider_state": provider.export_state(), "emergency": emergency}
        _loop_event(store, run_id, "AutonomousEpisodeCompleted", str(episode), result, timestamp)
        for item in store.pending_outbox(limit=1000):
            store.mark_outbox_delivered(item["outbox_id"], expected_envelope_hash=item["envelope_hash"])
        completed.append(result)
        # Persist only after the durable episode and every publication in this
        # terminal cut are complete.  A crash before this point leaves the
        # prior checkpoint and therefore fails closed on restart.
        from .simulation_runtime_checkpoint import persist_autonomous_runtime_checkpoint
        persist_autonomous_runtime_checkpoint(
            root,
            store,
            protocol=protocol,
            authority_key_identity=key_identity,
            completed=completed,
        )
        previous_equities.append(equity)
    return {"status": "COMPLETED" if len(completed) == len(values) else "PAUSED", "environment": ENVIRONMENT,
        "mode": "ZERO", "run_id": run_id, "protocol_digest": protocol_digest,
        "completed_episodes": len(completed), "cash": canonical_decimal_text(economic.cash("USD")),
        "position": canonical_decimal_text(economic.position(INSTRUMENT)), "reconciled": True,
        "new_outbound_requests": provider.outbound_request_count - count_before,
        "resumed": count_before > 0 or len(events) > 1,
        "decisions": [{k: v for k, v in item.items() if k != "provider_state"} for item in completed],
        "economic_edge_status": "INCONCLUSIVE"}
