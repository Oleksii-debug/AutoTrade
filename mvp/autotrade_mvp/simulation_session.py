"""One network-free, journal-backed canonical simulation episode.

This deliberately runs one episode per directory. A durable send without a
completed reconciliation cannot be reconstructed from a fresh simulated
provider instance, so restart leaves it UNKNOWN instead of sending again.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from .accounting import book_equity_fill, book_external_cash_flow
from .authority import AuthoritativeRiskSnapshot, AuthorityPolicy, AuthorityService
from .dispatch import GuardedDispatcher, stable_client_order_id
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


def _uuid(kind: str, episode_id: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"autotrade-canonical-simulation:{kind}:{episode_id}"))


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


def _event(store: JournalStore, kind: str, episode_id: str, payload: dict, now: str) -> dict:
    envelope = {
        "event_id": _uuid(kind, episode_id),
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
        "correlation_id": _uuid("correlation", episode_id),
        "causation_id": None,
        "payload": payload,
        "payload_hash": payload_digest(payload),
        "evidence_refs": [],
    }
    store.append_event(envelope)
    return envelope


def _owner_payload(
    *, episode_id: str, input_hash: str, decision: str, evidence_time: str
) -> dict[str, object]:
    return {
        "schema_version": "1.0.0",
        "bootstrap_contract": _BOOTSTRAP_CONTRACT,
        "episode_id": episode_id,
        "input_hash": input_hash,
        "decision": decision,
        "environment": ENVIRONMENT,
        "evidence_time": evidence_time,
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
    owners: list[dict], *, episode_id: str, input_hash: str, decision: str
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
    }
    if set(payload) != expected_keys:
        raise ValueError("durable simulation owner payload schema is invalid")
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
) -> None:
    events = [*economic_events, *reconciliation_events]
    expected = set(range(2, 2 + len(events)))
    observed = {event.get("journal_sequence") for event in events}
    if observed != expected:
        raise ValueError("canonical simulation bootstrap prefix is not contiguous")
    if JournalStore.current_journal_sequence(store) != 1 + len(events):
        raise ValueError("state directory contains foreign durable journal authority")


def _deliver_event(store: JournalStore, event_id: str, *, topic: str) -> None:
    state = JournalStore.outbox_delivery_state(
        store, event_id, topic=topic
    )
    if state["delivered"]:
        return
    JournalStore.mark_outbox_delivered(
        store,
        state["outbox_id"],
        expected_envelope_hash=state["envelope_hash"],
    )


def _risk_policy() -> RiskPolicy:
    return RiskPolicy.create(
        max_abs_position="10", max_single_notional="1000",
        max_gross_leverage="2", max_net_leverage="2",
        max_daily_loss="500", max_drawdown_fraction="0.20",
        max_data_age_seconds="5", max_fx_age_seconds="60",
        min_margin_headroom="0.20", max_stress_loss="500",
    )


def _risk_context(price: Decimal) -> RiskContext:
    return RiskContext.create(
        state_version=1, equity=str(INITIAL_CASH), positions={},
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
    decision = MovingAverageStrategy().decide(values, Decimal("1"))
    if decision.side == "SELL":
        raise ValueError("this long-only simulation session supports BUY/HOLD prices")
    buy_requirements = None
    if decision.side == "BUY":
        amount = exact_multiply(decision.quantity, decision.price)
        required = exact_add(amount, exact_multiply(amount, FEE_RATE))
        buy_requirements = (amount, required)
    input_payload = {"episode_id": episode_id, "prices": [str(v) for v in values]}
    input_hash = payload_digest(input_payload)
    root = Path(state_dir)
    root.mkdir(parents=True, exist_ok=True)
    with ResourceLock(root / ".canonical-simulation.lock"):
        return _run_locked(
            root, episode_id=episode_id, input_hash=input_hash,
            decision=decision, now=now, fault_after_send=fault_after_send,
            buy_requirements=buy_requirements,
        )


def _run_locked(root: Path, *, episode_id: str, input_hash: str,
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
        if (
            started.get("event_type") != "SimulationSessionStarted"
            or type(payload) is not dict
            or set(payload) != {"input_hash", "decision", "episode_id", "environment"}
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
            economic = DurableProviderEconomicBook(
                store, provider_id=PROVIDER, account_id=ACCOUNT,
                environment=ENVIRONMENT,
            )
            if (result["cash"] != str(economic.cash("USD"))
                    or result["position"] != str(economic.position(INSTRUMENT))):
                raise ValueError("completed simulation does not match durable economics")
            result["resumed"] = True
            result["new_outbound_requests"] = 0
            return result
        return {
            "status": "UNKNOWN", "environment": ENVIRONMENT,
            "episode_id": episode_id, "reason": "incomplete_send_requires_reconciliation",
            "reconciled": False, "resumed": True, "new_outbound_requests": 0,
        }

    future = (datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
              + timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
    provider = SimulatedProvider(
        account_id=ACCOUNT, initial_cash=str(INITIAL_CASH), fee_rate=str(FEE_RATE),
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
    _require_prestart_cut(
        store,
        economic_events=economic_events,
        reconciliation_events=reconciliation_events,
    )
    if economic_events:
        plan = economic.prepare_batch_mutation((seed,), committed_at=timestamp)
        if len(economic_events) != 1 or not plan.already_committed:
            raise ValueError("canonical simulation seed bootstrap is invalid")
    else:
        economic.append(seed)
        economic_events = JournalStore.load_events(
            store, "economic_book", economic.book_id
        )
        if len(economic_events) != 1:
            raise RuntimeError("canonical simulation seed bootstrap was not persisted")
    _deliver_event(
        store,
        economic_events[0]["event_id"],
        topic="autotrade.economic.events",
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
    _require_prestart_cut(
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
        )
        reconciliation_events = [availability]
    _deliver_event(
        store,
        availability["event_id"],
        topic="autotrade.reconciliation.events",
    )
    _require_prestart_cut(
        store,
        economic_events=economic_events,
        reconciliation_events=reconciliation_events,
    )
    _event(store, "SimulationSessionStarted", episode_id, {
        "input_hash": input_hash, "decision": decision.side,
        "episode_id": episode_id, "environment": ENVIRONMENT,
    }, timestamp)
    if decision.side == "HOLD":
        result = {
            "status": "HOLD", "decision": "HOLD", "environment": ENVIRONMENT,
            "episode_id": episode_id, "cash": str(economic.cash("USD")),
            "position": str(economic.position(INSTRUMENT)), "reconciled": True,
            "order_id": None, "fill_id": None,
            "reconciliation_event_id": availability["event_id"],
            "new_outbound_requests": 0,
        }
        _event(store, "SimulationSessionCompleted", episode_id, result, timestamp)
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
            "position": str(economic.position(INSTRUMENT)), "reconciled": True,
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
    if dispatch.status != "SENT":
        return {
            "status": "UNKNOWN" if dispatch.status == "UNKNOWN" else "BLOCKED",
            "decision": "BUY", "environment": ENVIRONMENT,
            "episode_id": episode_id, "reason": dispatch.reason,
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
        "reconciled": True, "order_id": dispatch.client_order_id,
        "fill_id": fill["provider_execution_id"],
        "reconciliation_event_id": checkpoint["event_id"],
        "new_outbound_requests": provider.outbound_request_count,
    }
    _event(store, "SimulationSessionCompleted", episode_id, result, timestamp)
    return {**result, "resumed": False}
