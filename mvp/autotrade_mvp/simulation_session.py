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
from .reconciliation_journal import record_reconciliation_checkpoint
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
            content = path.read_bytes()
        except (OSError, ValueError) as error:
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


def _deliver_event(store: JournalStore, event_id: str) -> None:
    for item in store.pending_outbox():
        if item["event_id"] == event_id:
            store.mark_outbox_delivered(
                item["outbox_id"], expected_envelope_hash=item["envelope_hash"]
            )
            return
    raise ValueError("expected simulator bootstrap outbox event is missing")


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
    prior = store.load_events("canonical_simulation_session", _AGGREGATE)
    if prior:
        started = prior[0]
        if started["event_type"] != "SimulationSessionStarted":
            raise ValueError("simulation session journal has an invalid first event")
        if started["payload"].get("protocol_identity") != protocol_identity:
            raise ValueError(
                "state directory belongs to incompatible simulation protocol/configuration"
            )
        if started["payload"].get("input_hash") != input_hash:
            raise ValueError("state directory belongs to another simulation input")
        if len(prior) == 2 and prior[1]["event_type"] == "SimulationSessionCompleted":
            completed = prior[1]
            if (
                completed["payload"].get("protocol_identity") != protocol_identity
                or completed["payload"].get("input_hash") != input_hash
            ):
                raise ValueError(
                    "completed simulation protocol/input identity does not match session"
                )
            result = dict(completed["payload"])
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
            "protocol_identity": protocol_identity, "input_hash": input_hash,
            "source_build_identity": source_build_identity,
            "reconciled": False, "resumed": True, "new_outbound_requests": 0,
        }

    timestamp = _now(now)
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
    if store.load_events("economic_book", economic.book_id):
        # A prior process may have died between bootstrap and the session marker.
        # Its send state cannot be inferred from a fresh simulated provider.
        return {
            "status": "UNKNOWN", "environment": ENVIRONMENT,
            "episode_id": episode_id, "reason": "orphaned_durable_state_requires_reconciliation",
            "protocol_identity": protocol_identity, "input_hash": input_hash,
            "source_build_identity": source_build_identity,
            "reconciled": False, "resumed": True, "new_outbound_requests": 0,
        }
    economic.append(book_external_cash_flow(
        transaction_id=_uuid("seed-transaction", episode_id),
        cause_event_id=_uuid("seed-cause", episode_id),
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
    _event(store, "SimulationSessionStarted", episode_id, {
        "input_hash": input_hash,
        "protocol_identity": protocol_identity,
        "protocol_version": _SIMULATION_PROTOCOL_VERSION,
        "source_build_identity": source_build_identity,
        "decision": decision.side,
        "episode_id": episode_id,
        "environment": ENVIRONMENT,
    }, timestamp)
    if decision.side == "HOLD":
        result = {
            "status": "HOLD", "decision": "HOLD", "environment": ENVIRONMENT,
            "episode_id": episode_id, "cash": str(economic.cash("USD")),
            "position": str(economic.position(INSTRUMENT)),
            "protocol_identity": protocol_identity, "input_hash": input_hash,
            "source_build_identity": source_build_identity, "reconciled": True,
            "order_id": None, "fill_id": None,
            "reconciliation_event_id": availability["event_id"],
            "new_outbound_requests": 0,
        }
        _event(store, "SimulationSessionCompleted", episode_id, result, timestamp)
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
        return {**result, "resumed": False}

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
