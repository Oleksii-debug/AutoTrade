"""One network-free, journal-backed canonical simulation episode.

This deliberately runs one episode per directory. A durable send without a
completed reconciliation cannot be reconstructed from a fresh simulated
provider instance, so restart leaves it UNKNOWN instead of sending again.

The session identity binds an externally asserted exact Git source SHA and one
content-derived protocol/configuration digest. That makes replay incompatible
across different builds or behavior-affecting canonical configuration. The
source SHA is an identity claim, not installed-source authentication; release
and qualification tooling own that independent trust boundary.

One durable ``SimulationSessionOwned`` event is the first event in an empty
journal. It prevents canonical simulation from adopting unrelated durable state
and gives restart a proof boundary before bootstrap mutation. Zero-wire terminal
states are reconstructed only from canonical submission chronology; possible-send
states remain UNKNOWN and are never blindly retried.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
import re
from uuid import NAMESPACE_URL, uuid5

from .accounting import book_equity_fill, book_external_cash_flow
from .authority import AuthoritativeRiskSnapshot, AuthorityPolicy, AuthorityService
from .dispatch import (
    GuardedDispatcher,
    stable_client_order_id,
    submission_attempt_aggregate_id,
)
from .durable_reservations import DurableReservationBook
from .exact_decimal import canonical_decimal_text, exact_add, exact_multiply
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
from .reconciliation_journal import (
    load_latest_reconciliation_checkpoint_for_scope,
    record_reconciliation_checkpoint,
)
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
_SOURCE_SHA = re.compile(r"^[0-9a-f]{40}$")

CANONICAL_SIMULATION_PROTOCOL_VERSION = "2.0.0"
_STRATEGY_CONFIG = {
    "strategy_id": "moving-average",
    "strategy_version": "1.0.0",
    "fast": 2,
    "slow": 3,
    "order_quantity": "1",
}
_RISK_POLICY_CONFIG = {
    "policy_id": "canonical-simulation-risk",
    "policy_version": "1.0.0",
    "max_abs_position": "10",
    "max_single_notional": "1000",
    "max_gross_leverage": "2",
    "max_net_leverage": "2",
    "max_daily_loss": "500",
    "max_drawdown_fraction": "0.20",
    "max_data_age_seconds": "5",
    "max_fx_age_seconds": "60",
    "min_margin_headroom": "0.20",
    "max_stress_loss": "500",
}
_RISK_CONTEXT_CONFIG = {
    "context_version": "1.0.0",
    "state_version": 1,
    "daily_pnl": "0",
    "drawdown_fraction": "0",
    "market_data_age_seconds": "1",
    "fx_age_seconds": {"USD": "1"},
    "margin_headroom": "1",
    "capability_allowed": True,
    "borrow_available": True,
    "stress_move": "-0.25",
}
_AUTHORITY_POLICY_CONFIG = {
    "policy_schema_version": "1.0.0",
    "action": "ORDER.SUBMIT",
    "max_notional": "1000",
    "autonomous": True,
    "protection_only": False,
    "version": 1,
}
_COMPONENT_CONFIG = {
    "dispatch": "GuardedDispatcher/v1",
    "reservation": "DurableReservationBook/v1",
    "economic_book": "DurableProviderEconomicBook/v1",
    "economic_commit": "commit_economic_batch_with_reservation_consumption/v1",
    "reconciliation": "reconcile_account/v1",
    "reconciliation_checkpoint": "record_reconciliation_checkpoint/v1",
    "simulated_provider": "SimulatedProvider/v1",
}


def _canonical_source_sha(value: str) -> str:
    if not isinstance(value, str) or _SOURCE_SHA.fullmatch(value) is None:
        raise ValueError("source_sha must be an exact 40-character lowercase Git SHA")
    return value


def _protocol_identity(source_sha: str) -> tuple[dict[str, object], str]:
    normalized_sha = _canonical_source_sha(source_sha)
    payload: dict[str, object] = {
        "schema_version": "1.0.0",
        "protocol_version": CANONICAL_SIMULATION_PROTOCOL_VERSION,
        "source_sha": normalized_sha,
        "financial_scope": {
            "account_id": ACCOUNT,
            "environment": ENVIRONMENT,
            "provider_id": PROVIDER,
            "instrument": INSTRUMENT,
            "instrument_id": INSTRUMENT_ID,
            "instrument_version": 1,
            "initial_cash": canonical_decimal_text(INITIAL_CASH),
            "fee_rate": canonical_decimal_text(FEE_RATE),
            "currency": "USD",
        },
        "strategy": dict(_STRATEGY_CONFIG),
        "risk_policy": dict(_RISK_POLICY_CONFIG),
        "risk_context": {
            **_RISK_CONTEXT_CONFIG,
            "stress_instrument": INSTRUMENT,
            "equity": canonical_decimal_text(INITIAL_CASH),
        },
        "authority_policy": dict(_AUTHORITY_POLICY_CONFIG),
        "components": dict(_COMPONENT_CONFIG),
    }
    return payload, payload_digest(payload)


def _session_identity(
    values: list[Decimal], *, episode_id: str, source_sha: str
) -> tuple[str, str, str, dict[str, object]]:
    protocol, protocol_id = _protocol_identity(source_sha)
    input_hash = payload_digest({
        "schema_version": "1.0.0",
        "prices": [canonical_decimal_text(value) for value in values],
    })
    session_id = payload_digest({
        "schema_version": "1.0.0",
        "episode_id": episode_id,
        "input_hash": input_hash,
        "protocol_id": protocol_id,
    })
    return input_hash, session_id, protocol_id, protocol


def _identity_payload(
    *, episode_id: str, input_hash: str, session_id: str, source_sha: str,
    protocol_id: str, protocol: dict[str, object],
) -> dict[str, object]:
    return {
        "episode_id": episode_id,
        "session_id": session_id,
        "input_hash": input_hash,
        "source_sha": source_sha,
        "protocol_version": CANONICAL_SIMULATION_PROTOCOL_VERSION,
        "protocol_id": protocol_id,
        "protocol": protocol,
        "environment": ENVIRONMENT,
    }


def _require_identity(
    payload: object, *, episode_id: str, input_hash: str, session_id: str,
    source_sha: str, protocol_id: str, protocol: dict[str, object],
) -> None:
    if not isinstance(payload, dict):
        raise ValueError("durable simulation identity payload is invalid")
    if (
        payload.get("source_sha") != source_sha
        or payload.get("protocol_version") != CANONICAL_SIMULATION_PROTOCOL_VERSION
        or payload.get("protocol_id") != protocol_id
        or payload.get("protocol") != protocol
    ):
        raise ValueError(
            "durable simulation protocol/build identity is incompatible with this runtime"
        )
    if (
        payload.get("episode_id") != episode_id
        or payload.get("input_hash") != input_hash
        or payload.get("session_id") != session_id
        or payload.get("environment") != ENVIRONMENT
    ):
        raise ValueError("state directory belongs to another simulation input")


def _uuid(kind: str, session_id: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"autotrade-canonical-simulation:{kind}:{session_id}"))


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
        if not isinstance(raw, str):
            raise TypeError("prices must be decimal strings")
        try:
            value = Decimal(raw)
        except InvalidOperation as error:
            raise ValueError("prices must be finite positive decimals") from error
        if not value.is_finite() or value <= 0:
            raise ValueError("prices must be finite positive decimals")
        canonical_decimal_text(value)
        parsed.append(value)
    return parsed


def _event(store: JournalStore, kind: str, session_id: str, payload: dict, now: str) -> dict:
    envelope = {
        "event_id": _uuid(kind, session_id),
        "event_type": kind,
        "schema_version": "1.0.0",
        "aggregate_type": "canonical_simulation_session",
        "aggregate_id": _AGGREGATE,
        "aggregate_version": str(store.next_aggregate_version("canonical_simulation_session", _AGGREGATE)),
        "host_id": "local-simulation",
        "owner_epoch": "1",
        "environment": ENVIRONMENT,
        "occurred_at": now,
        "observed_at": now,
        "committed_at": now,
        "correlation_id": _uuid("correlation", session_id),
        "causation_id": None,
        "payload": payload,
        "payload_hash": payload_digest(payload),
        "evidence_refs": [],
    }
    store.append_event(envelope)
    persisted = store.get_event(envelope["event_id"])
    if persisted is None:
        raise RuntimeError("canonical simulation event was not persisted")
    return persisted


def _deliver_event(store: JournalStore, event_id: str) -> None:
    for item in store.pending_outbox():
        if item["event_id"] == event_id:
            store.mark_outbox_delivered(
                item["outbox_id"], expected_envelope_hash=item["envelope_hash"]
            )
            return
    raise ValueError("expected simulator bootstrap outbox event is missing")


def _risk_policy() -> RiskPolicy:
    return RiskPolicy.create(
        max_abs_position=_RISK_POLICY_CONFIG["max_abs_position"],
        max_single_notional=_RISK_POLICY_CONFIG["max_single_notional"],
        max_gross_leverage=_RISK_POLICY_CONFIG["max_gross_leverage"],
        max_net_leverage=_RISK_POLICY_CONFIG["max_net_leverage"],
        max_daily_loss=_RISK_POLICY_CONFIG["max_daily_loss"],
        max_drawdown_fraction=_RISK_POLICY_CONFIG["max_drawdown_fraction"],
        max_data_age_seconds=_RISK_POLICY_CONFIG["max_data_age_seconds"],
        max_fx_age_seconds=_RISK_POLICY_CONFIG["max_fx_age_seconds"],
        min_margin_headroom=_RISK_POLICY_CONFIG["min_margin_headroom"],
        max_stress_loss=_RISK_POLICY_CONFIG["max_stress_loss"],
    )


def _risk_context(price: Decimal) -> RiskContext:
    return RiskContext.create(
        state_version=_RISK_CONTEXT_CONFIG["state_version"],
        equity=canonical_decimal_text(INITIAL_CASH), positions={},
        marks={INSTRUMENT: canonical_decimal_text(price)}, reserved_position_delta={},
        daily_pnl=_RISK_CONTEXT_CONFIG["daily_pnl"],
        drawdown_fraction=_RISK_CONTEXT_CONFIG["drawdown_fraction"],
        market_data_age_seconds=_RISK_CONTEXT_CONFIG["market_data_age_seconds"],
        fx_age_seconds=dict(_RISK_CONTEXT_CONFIG["fx_age_seconds"]),
        margin_headroom=_RISK_CONTEXT_CONFIG["margin_headroom"],
        capability_allowed=_RISK_CONTEXT_CONFIG["capability_allowed"],
        borrow_available=_RISK_CONTEXT_CONFIG["borrow_available"],
        stress_scenarios=({INSTRUMENT: _RISK_CONTEXT_CONFIG["stress_move"]},),
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


def _submission_events(store: JournalStore, session_id: str) -> list[dict]:
    aggregate_id = submission_attempt_aggregate_id(
        environment=ENVIRONMENT,
        account_id=ACCOUNT,
        attempt_id=_uuid("attempt", session_id),
    )
    return store.load_events("submission_attempt", aggregate_id)


def _checkpoint_for_result(store: JournalStore, result: dict[str, object]) -> dict:
    checkpoint_id = result.get("reconciliation_event_id")
    if not isinstance(checkpoint_id, str) or not checkpoint_id:
        raise ValueError("completed simulation has no reconciliation checkpoint identity")
    checkpoint = store.get_event(checkpoint_id)
    if checkpoint is None or checkpoint.get("event_type") != "AccountReconciled":
        raise ValueError("completed simulation reconciliation checkpoint is unavailable")
    payload = checkpoint.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("completed simulation reconciliation payload is invalid")
    if (
        payload.get("provider_id") != PROVIDER
        or payload.get("account_id") != ACCOUNT
        or payload.get("environment") != ENVIRONMENT
        or payload.get("complete") is not True
    ):
        raise ValueError("completed simulation reconciliation scope is inconsistent")
    return checkpoint


def _validate_completed_result(
    store: JournalStore,
    root: Path,
    result: dict[str, object],
    *,
    episode_id: str,
    input_hash: str,
    session_id: str,
    source_sha: str,
    protocol_id: str,
) -> None:
    expected_identity = {
        "episode_id": episode_id,
        "input_hash": input_hash,
        "session_id": session_id,
        "source_sha": source_sha,
        "protocol_version": CANONICAL_SIMULATION_PROTOCOL_VERSION,
        "protocol_id": protocol_id,
    }
    if any(result.get(key) != value for key, value in expected_identity.items()):
        raise ValueError("completed simulation provenance is incompatible")

    economic = DurableProviderEconomicBook(
        store, provider_id=PROVIDER, account_id=ACCOUNT, environment=ENVIRONMENT,
    )
    if (
        result.get("cash") != str(economic.cash("USD"))
        or result.get("position") != str(economic.position(INSTRUMENT))
    ):
        raise ValueError("completed simulation does not match durable economics")

    checkpoint = _checkpoint_for_result(store, result)
    checkpoint_payload = checkpoint["payload"]
    submissions = _submission_events(store, session_id)
    submission_types = [event.get("event_type") for event in submissions]
    status = result.get("status")

    if status in {"HOLD", "RISK_REJECTED"}:
        if submissions or result.get("order_id") is not None or result.get("fill_id") is not None:
            raise ValueError("zero-wire completed simulation conflicts with submission evidence")
        return

    if status == "BLOCKED":
        evidence = result.get("zero_wire_evidence")
        expected_types = {
            "NO_SUBMISSION": [],
            "SUBMISSION_PREPARED": ["SubmissionPrepared"],
            "SUBMISSION_BLOCKED": ["SubmissionPrepared", "SubmissionBlocked"],
        }.get(evidence)
        if expected_types is None or submission_types != expected_types:
            raise ValueError("BLOCKED simulation lacks exact zero-wire dispatch evidence")
        if result.get("fill_id") is not None:
            raise ValueError("BLOCKED simulation cannot carry a fill identity")
        if submissions:
            prepared_payload = submissions[0].get("payload", {})
            client_order_id = prepared_payload.get("client_order_id")
            if result.get("order_id") != client_order_id:
                raise ValueError("BLOCKED simulation order identity conflicts with dispatch")
            reservations = DurableReservationBook(
                store, environment=ENVIRONMENT, account_id=ACCOUNT,
                resolution_artifact_store=ArtifactStore(root / "artifacts"),
                resolution_artifact_root=root / "artifacts",
            )
            reservation_id = _uuid("reservation", session_id)
            retained = [item for item in reservations.active() if item.reservation_id == reservation_id]
            if len(retained) != 1 or retained[0].state != "WORKING":
                raise ValueError("BLOCKED simulation reservation disposition is inconsistent")
        if checkpoint_payload.get("matched_execution_ids") not in ([], ()):
            raise ValueError("BLOCKED simulation cannot have reconciled execution ids")
        return

    if status == "FILL_RECONCILED_ORDER_UNCONFIRMED":
        if submission_types != ["SubmissionPrepared", "SubmissionSending", "SubmissionSent"]:
            raise ValueError("completed fill lacks exact sent submission chronology")
        prepared_payload = submissions[0].get("payload", {})
        if result.get("order_id") != prepared_payload.get("client_order_id"):
            raise ValueError("completed fill order identity conflicts with dispatch")
        fill_id = result.get("fill_id")
        if not isinstance(fill_id, str) or not fill_id:
            raise ValueError("completed fill identity is required")
        matched = checkpoint_payload.get("matched_execution_ids")
        if not isinstance(matched, list) or fill_id not in matched:
            raise ValueError("completed fill identity is not proven by reconciliation")
        if result.get("reconciled") is not True:
            raise ValueError("completed fill must be reconciled")
        return

    raise ValueError("unsupported completed simulation status")


def _common_result(
    *, episode_id: str, session_id: str, source_sha: str,
    protocol_id: str, input_hash: str,
) -> dict[str, object]:
    return {
        "episode_id": episode_id,
        "session_id": session_id,
        "source_sha": source_sha,
        "protocol_version": CANONICAL_SIMULATION_PROTOCOL_VERSION,
        "protocol_id": protocol_id,
        "input_hash": input_hash,
    }


def _project_incomplete_started(
    store: JournalStore,
    root: Path,
    started_payload: dict[str, object],
    *,
    episode_id: str,
    input_hash: str,
    session_id: str,
    source_sha: str,
    protocol_id: str,
    timestamp: str,
) -> dict[str, object]:
    common = _common_result(
        episode_id=episode_id, session_id=session_id, source_sha=source_sha,
        protocol_id=protocol_id, input_hash=input_hash,
    )
    submissions = _submission_events(store, session_id)
    types = [event.get("event_type") for event in submissions]
    if "SubmissionSending" in types or "SubmissionSent" in types or "SubmissionUnknown" in types:
        return {
            **common,
            "status": "UNKNOWN", "environment": ENVIRONMENT,
            "decision": started_payload.get("decision"),
            "reason": "durable_send_boundary_requires_reconciliation",
            "reconciled": False, "resumed": True,
            "new_outbound_requests": 0,
        }

    if types not in ([], ["SubmissionPrepared"], ["SubmissionPrepared", "SubmissionBlocked"]):
        raise ValueError("incomplete simulation has unsupported submission chronology")

    checkpoint = load_latest_reconciliation_checkpoint_for_scope(
        store, provider_id=PROVIDER, account_id=ACCOUNT, environment=ENVIRONMENT,
    )
    if checkpoint is None:
        raise ValueError("zero-wire recovery has no reconciliation checkpoint")
    economic = DurableProviderEconomicBook(
        store, provider_id=PROVIDER, account_id=ACCOUNT, environment=ENVIRONMENT,
    )
    if not types:
        evidence = "NO_SUBMISSION"
        order_id = None
    elif types == ["SubmissionPrepared"]:
        evidence = "SUBMISSION_PREPARED"
        order_id = submissions[0]["payload"].get("client_order_id")
    else:
        evidence = "SUBMISSION_BLOCKED"
        order_id = submissions[0]["payload"].get("client_order_id")
    result = {
        **common,
        "status": "BLOCKED",
        "decision": started_payload.get("decision"),
        "environment": ENVIRONMENT,
        "reason": "durable_zero_wire_recovery",
        "zero_wire_evidence": evidence,
        "cash": str(economic.cash("USD")),
        "position": str(economic.position(INSTRUMENT)),
        "reconciled": False,
        "order_id": order_id,
        "fill_id": None,
        "reconciliation_event_id": checkpoint["event_id"],
        "new_outbound_requests": 0,
    }
    _event(store, "SimulationSessionCompleted", session_id, result, timestamp)
    _validate_completed_result(
        store, root, result, episode_id=episode_id, input_hash=input_hash,
        session_id=session_id, source_sha=source_sha, protocol_id=protocol_id,
    )
    return {**result, "resumed": True}


def run_canonical_simulation(
    prices: list[str], state_dir: str | Path, *, episode_id: str, source_sha: str,
    now: str | None = None, fault_after_send: bool = False,
) -> dict[str, object]:
    """Run one BUY/HOLD episode; a restarted ambiguous send is never retried."""
    if not isinstance(episode_id, str) or not episode_id.strip():
        raise ValueError("episode_id is required")
    if type(fault_after_send) is not bool:
        raise TypeError("fault_after_send must be boolean")
    values = _prices(prices)
    input_hash, session_id, protocol_id, protocol = _session_identity(
        values, episode_id=episode_id, source_sha=source_sha
    )
    strategy = MovingAverageStrategy(
        fast=_STRATEGY_CONFIG["fast"], slow=_STRATEGY_CONFIG["slow"]
    )
    decision = strategy.decide(values, Decimal(_STRATEGY_CONFIG["order_quantity"]))
    if decision.side == "SELL":
        raise ValueError("this long-only simulation session supports BUY/HOLD prices")
    root = Path(state_dir)
    root.mkdir(parents=True, exist_ok=True)
    with ResourceLock(root / ".canonical-simulation.lock"):
        return _run_locked(
            root, episode_id=episode_id, input_hash=input_hash,
            session_id=session_id, source_sha=source_sha,
            protocol=protocol, protocol_id=protocol_id,
            decision=decision, now=now, fault_after_send=fault_after_send,
        )


def _run_locked(root: Path, *, episode_id: str, input_hash: str,
                session_id: str, source_sha: str, protocol: dict[str, object],
                protocol_id: str, decision, now: str | None,
                fault_after_send: bool) -> dict[str, object]:
    store = JournalStore(root / "journal.sqlite3")
    timestamp = _now(now)
    identity = _identity_payload(
        episode_id=episode_id, input_hash=input_hash, session_id=session_id,
        source_sha=source_sha, protocol_id=protocol_id, protocol=protocol,
    )
    prior = store.load_events("canonical_simulation_session", _AGGREGATE)
    if not prior:
        if store.current_journal_sequence() != 0:
            raise ValueError(
                "state directory contains foreign durable journal without canonical simulation ownership"
            )
        owner = _event(
            store,
            "SimulationSessionOwned",
            session_id,
            {"schema_version": "1.0.0", **identity},
            timestamp,
        )
        prior = [owner]
    owner = prior[0]
    if owner.get("event_type") != "SimulationSessionOwned":
        raise ValueError("durable simulation ownership marker is missing or invalid")
    _require_identity(
        owner.get("payload"), episode_id=episode_id, input_hash=input_hash,
        session_id=session_id, source_sha=source_sha,
        protocol_id=protocol_id, protocol=protocol,
    )

    if len(prior) == 1:
        owner_sequence = owner.get("journal_sequence")
        if type(owner_sequence) is not int or owner_sequence <= 0:
            raise ValueError("durable simulation ownership has no journal sequence")
        events_after_owner = store.load_events_after_journal_sequence(owner_sequence)
        if events_after_owner:
            return {
                **_common_result(
                    episode_id=episode_id, session_id=session_id,
                    source_sha=source_sha, protocol_id=protocol_id,
                    input_hash=input_hash,
                ),
                "status": "BLOCKED", "environment": ENVIRONMENT,
                "decision": decision.side,
                "reason": "owned_bootstrap_incomplete_pre_send",
                "reconciled": False, "resumed": True,
                "new_outbound_requests": 0,
            }
    else:
        if len(prior) > 3:
            raise ValueError("canonical simulation session has unsupported durable chronology")
        started = prior[1]
        if started.get("event_type") != "SimulationSessionStarted":
            raise ValueError("durable simulation start marker is invalid")
        _require_identity(
            started.get("payload"), episode_id=episode_id, input_hash=input_hash,
            session_id=session_id, source_sha=source_sha,
            protocol_id=protocol_id, protocol=protocol,
        )
        if len(prior) == 3:
            completed = prior[2]
            if completed.get("event_type") != "SimulationSessionCompleted":
                raise ValueError("durable simulation terminal marker is invalid")
            result = dict(completed["payload"])
            _validate_completed_result(
                store, root, result, episode_id=episode_id, input_hash=input_hash,
                session_id=session_id, source_sha=source_sha, protocol_id=protocol_id,
            )
            result["resumed"] = True
            result["new_outbound_requests"] = 0
            return result
        return _project_incomplete_started(
            store, root, started["payload"], episode_id=episode_id,
            input_hash=input_hash, session_id=session_id, source_sha=source_sha,
            protocol_id=protocol_id, timestamp=timestamp,
        )

    future = (datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
              + timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
    provider = SimulatedProvider(
        account_id=ACCOUNT, initial_cash=canonical_decimal_text(INITIAL_CASH),
        fee_rate=canonical_decimal_text(FEE_RATE),
        transport_faults=({stable_client_order_id(
            "simulated", _uuid("intent", session_id),
            environment=ENVIRONMENT, account_id=ACCOUNT,
        ): "AFTER_ACCEPT_RESPONSE_LOST"}
                          if fault_after_send else None),
    )
    economic = DurableProviderEconomicBook(
        store, provider_id=PROVIDER, account_id=ACCOUNT, environment=ENVIRONMENT,
    )
    if store.load_events("economic_book", economic.book_id):
        # Ownership exists, but bootstrap has already mutated durable state and
        # no session start marker proves which remaining step was reached. The
        # chronology is pre-send, so fail safe without fabricating possible-send
        # UNKNOWN or mutating the partial bootstrap further.
        return {
            **_common_result(
                episode_id=episode_id, session_id=session_id,
                source_sha=source_sha, protocol_id=protocol_id,
                input_hash=input_hash,
            ),
            "status": "BLOCKED", "environment": ENVIRONMENT,
            "decision": decision.side,
            "reason": "owned_bootstrap_incomplete_pre_send",
            "reconciled": False, "resumed": True,
            "new_outbound_requests": 0,
        }
    economic.append(book_external_cash_flow(
        transaction_id=_uuid("seed-transaction", session_id),
        cause_event_id=_uuid("seed-cause", session_id),
        currency="USD", amount=canonical_decimal_text(INITIAL_CASH),
    ))
    bootstrap = store.load_events("economic_book", economic.book_id)
    _deliver_event(store, bootstrap[-1]["event_id"])
    admission_reconciliation, snapshot = _reconcile(provider, economic, timestamp)
    if not admission_reconciliation.complete or admission_reconciliation.blocks_new_risk:
        raise ValueError("initial simulated provider reconciliation failed")
    availability = record_reconciliation_checkpoint(
        store, reconciliation_id="canonical-simulation-admission",
        result=admission_reconciliation, observed_at=timestamp,
        host_id="local-simulation", owner_epoch="1",
    )
    _deliver_event(store, availability["event_id"])
    _event(store, "SimulationSessionStarted", session_id, {
        "schema_version": "2.0.0",
        **identity,
        "decision": decision.side,
    }, timestamp)
    common_result = _common_result(
        episode_id=episode_id, session_id=session_id, source_sha=source_sha,
        protocol_id=protocol_id, input_hash=input_hash,
    )
    if decision.side == "HOLD":
        result = {
            **common_result,
            "status": "HOLD", "decision": "HOLD", "environment": ENVIRONMENT,
            "cash": str(economic.cash("USD")),
            "position": str(economic.position(INSTRUMENT)), "reconciled": True,
            "order_id": None, "fill_id": None,
            "reconciliation_event_id": availability["event_id"],
            "new_outbound_requests": 0,
        }
        _event(store, "SimulationSessionCompleted", session_id, result, timestamp)
        _validate_completed_result(
            store, root, result, episode_id=episode_id, input_hash=input_hash,
            session_id=session_id, source_sha=source_sha, protocol_id=protocol_id,
        )
        return {**result, "resumed": False}

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
            evidence_refs={name: f"simulated:{name.lower()}:{session_id}" for name in (
                "PORTFOLIO", "MARKET", "MARGIN", "POLICY", "RECONCILIATION",
                "CAPABILITY", "BORROW", "STRESS", "FX", "FACTORS", "LIQUIDITY",
                "LIQUIDATION", "SETTLEMENT", "OPTION_LIFECYCLE", "FUTURES_LIFECYCLE",
            )},
        )

    authority = AuthorityService(store, risk_authority_resolver=resolve_risk)
    policy_id = _uuid("policy", session_id)
    authority.register_policy(AuthorityPolicy.create(
        policy_id=policy_id, account_id=ACCOUNT, environments={ENVIRONMENT},
        instruments={(INSTRUMENT_ID, 1)}, actions={_AUTHORITY_POLICY_CONFIG["action"]},
        max_notional=_AUTHORITY_POLICY_CONFIG["max_notional"], expires_at=future,
        autonomous=_AUTHORITY_POLICY_CONFIG["autonomous"],
        protection_only=_AUTHORITY_POLICY_CONFIG["protection_only"],
        version=_AUTHORITY_POLICY_CONFIG["version"],
    ))
    reservations = DurableReservationBook(
        store, environment=ENVIRONMENT, account_id=ACCOUNT,
        resolution_artifact_store=ArtifactStore(root / "artifacts"),
        resolution_artifact_root=root / "artifacts",
    )
    amount = exact_multiply(decision.quantity, decision.price)
    required = exact_add(amount, exact_multiply(amount, FEE_RATE))
    quantity_text = canonical_decimal_text(decision.quantity)
    price_text = canonical_decimal_text(decision.price)
    amount_text = canonical_decimal_text(amount)
    required_text = canonical_decimal_text(required)
    intent_id = _uuid("intent", session_id)
    intent_hash = payload_digest({
        "session_id": session_id, "protocol_id": protocol_id,
        "side": "BUY", "quantity": quantity_text,
        "price": price_text, "instrument": INSTRUMENT,
    })
    admission_id = _uuid("admission", session_id)
    admission = authority.admit(
        command_id=_uuid("financial-command", session_id),
        idempotency_key=_uuid("financial-command", session_id),
        admission_id=admission_id, policy_id=policy_id,
        intent_id=intent_id, intent_hash=intent_hash, account_id=ACCOUNT,
        environment=ENVIRONMENT, instrument_id=INSTRUMENT_ID,
        instrument_version=1, action=_AUTHORITY_POLICY_CONFIG["action"],
        notional=amount_text,
        capability_snapshot_id="simulated-capability-v1",
        risk_intent=RiskIntent.create(
            symbol=INSTRUMENT, side="BUY", quantity=quantity_text,
            price=price_text, expected_state_version=_RISK_CONTEXT_CONFIG["state_version"],
        ),
        risk_context=context, risk_policy=policy, risk_valid_until=future,
        reservation_book=reservations,
        reservation_id=_uuid("reservation", session_id),
        reservation_requirements={"CASH:USD": required_text},
        reservation_available={"CASH:USD": snapshot["balances"][0]["available"]},
        reservation_checkpoint_event_id=availability["event_id"],
        reservation_provider_id=PROVIDER, reservation_max_age_seconds="60",
        now=timestamp,
    )
    if admission.outcome != "ADMITTED":
        result = {
            **common_result,
            "status": "RISK_REJECTED", "decision": "BUY", "environment": ENVIRONMENT,
            "cash": str(economic.cash("USD")),
            "position": str(economic.position(INSTRUMENT)), "reconciled": True,
            "order_id": None, "fill_id": None,
            "reconciliation_event_id": availability["event_id"],
            "new_outbound_requests": 0,
        }
        _event(store, "SimulationSessionCompleted", session_id, result, timestamp)
        _validate_completed_result(
            store, root, result, episode_id=episode_id, input_hash=input_hash,
            session_id=session_id, source_sha=source_sha, protocol_id=protocol_id,
        )
        return {**result, "resumed": False}

    def final_check(candidate_hash, current_time):
        return authority.dispatch_allowed(
            admission_id, intent_hash=candidate_hash, account_id=ACCOUNT,
            environment=ENVIRONMENT, instrument_id=INSTRUMENT_ID,
            instrument_version=1, action=_AUTHORITY_POLICY_CONFIG["action"], now=current_time,
            capability_snapshot_id="simulated-capability-v1",
        )

    attempt_id = _uuid("attempt", session_id)
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
    if dispatch.status != "SENT":
        if dispatch.status == "BLOCKED":
            submission_types = [event["event_type"] for event in _submission_events(store, session_id)]
            if submission_types != ["SubmissionPrepared", "SubmissionBlocked"]:
                raise ValueError("guarded BLOCKED result lacks exact zero-wire chronology")
            result = {
                **common_result,
                "status": "BLOCKED", "decision": "BUY", "environment": ENVIRONMENT,
                "reason": dispatch.reason,
                "zero_wire_evidence": "SUBMISSION_BLOCKED",
                "cash": str(economic.cash("USD")),
                "position": str(economic.position(INSTRUMENT)),
                "reconciled": False,
                "order_id": dispatch.client_order_id,
                "fill_id": None,
                "reconciliation_event_id": availability["event_id"],
                "new_outbound_requests": provider.outbound_request_count,
            }
            if result["new_outbound_requests"] != 0:
                raise ValueError("BLOCKED dispatch cannot have outbound provider requests")
            _event(store, "SimulationSessionCompleted", session_id, result, timestamp)
            _validate_completed_result(
                store, root, result, episode_id=episode_id, input_hash=input_hash,
                session_id=session_id, source_sha=source_sha, protocol_id=protocol_id,
            )
            return {**result, "resumed": False}
        return {
            **common_result,
            "status": "UNKNOWN",
            "decision": "BUY", "environment": ENVIRONMENT,
            "reason": dispatch.reason,
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
        command_id=_uuid("financial-fill-command", session_id),
        idempotency_key=_uuid("financial-fill-command", session_id),
        reservation_id=_uuid("reservation", session_id),
        usage={"CASH:USD": required_text},
        transactions=(book_equity_fill(
            transaction_id=_uuid("fill-transaction", session_id),
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
        **common_result,
        "status": "FILL_RECONCILED_ORDER_UNCONFIRMED", "decision": "BUY",
        "environment": ENVIRONMENT,
        "cash": str(economic.cash("USD")),
        "position": str(economic.position(INSTRUMENT)),
        "reconciled": True, "order_id": dispatch.client_order_id,
        "fill_id": fill["provider_execution_id"],
        "reconciliation_event_id": checkpoint["event_id"],
        "new_outbound_requests": provider.outbound_request_count,
    }
    _event(store, "SimulationSessionCompleted", session_id, result, timestamp)
    _validate_completed_result(
        store, root, result, episode_id=episode_id, input_hash=input_hash,
        session_id=session_id, source_sha=source_sha, protocol_id=protocol_id,
    )
    return {**result, "resumed": False}
