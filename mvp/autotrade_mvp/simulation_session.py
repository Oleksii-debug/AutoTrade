"""Provider-free orchestration over the canonical financial authorities.

The existing single-episode entrypoint remains available. The autonomous ZERO
entrypoint runs a frozen observation stream and restores completed simulator
cuts. Unfinished sends remain UNKNOWN and are never spontaneously retried.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
import sys
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
    prior = store.load_events("canonical_simulation_session", _AGGREGATE)
    if prior:
        started = prior[0]
        if (started["event_type"] != "SimulationSessionStarted"
                or started["payload"].get("input_hash") != input_hash):
            raise ValueError("state directory belongs to another simulation input")
        if len(prior) == 2 and prior[1]["event_type"] == "SimulationSessionCompleted":
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

    timestamp = _now(now)
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
    if store.load_events("economic_book", economic.book_id):
        # A prior process may have died between bootstrap and the session marker.
        # Its send state cannot be inferred from a fresh simulated provider.
        return {
            "status": "UNKNOWN", "environment": ENVIRONMENT,
            "episode_id": episode_id, "reason": "orphaned_durable_state_requires_reconciliation",
            "reconciled": False, "resumed": True, "new_outbound_requests": 0,
        }
    economic.append(book_external_cash_flow(
        transaction_id=_uuid("seed-transaction", episode_id),
        cause_event_id=_uuid("seed-cause", episode_id),
        currency="USD", amount=str(INITIAL_CASH),
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
            "position": str(economic.position(INSTRUMENT)), "reconciled": True,
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


# Multi-episode orchestration shares the existing financial authorities and OMS.
# It owns no ledger, risk engine, strategy, transport or allocation algorithm.
_LOOP_AGGREGATE = "canonical_autonomous_simulation"
_LOOP_PROTOCOL = "provider-free-zero-loop-v3"


def _loop_event(store, run_id, kind, key, payload, now):
    cut = store.current_journal_sequence()
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
        minimum_quantity=Decimal("1"), maximum_quantity=Decimal("10"), calendar_id="CONTINUOUS_24_7",
        timezone_id="UTC", effective_from=effective_from)
    return InstrumentRegistry(versions=(version,))


def run_autonomous_simulation(
    prices: list[str], state_dir: str | Path, *, run_id: str,
    now: str, stop_after_episodes: int | None = None,
    fault_at_episode: int | None = None, emergency_at_episode: int | None = None,
) -> dict[str, object]:
    """Run/resume a frozen price stream using the canonical SIMULATION authorities.

    Each new observation drives another autonomous decision; repeated BUY signals
    are targets, never repeated incremental exposure. An unfinished episode is
    UNKNOWN and blocks continuation without a spontaneous resend. Completed
    episodes restore the simulator from its journal snapshot and economics from
    DurableProviderEconomicBook. All evidence remains simulation-only.
    """
    from .zero_network import deny_python_network
    from .risk_policy_authority import canonical_risk_policy, risk_policy_digest

    if type(run_id) is not str or not run_id or run_id != run_id.strip():
        raise ValueError("run_id must be canonical nonempty text")
    if type(prices) is not list or not 1 <= len(prices) <= 10000:
        raise ValueError("price stream must contain 1 to 10000 observations")
    values = _prices(prices)
    timestamp = _now(now)
    instrument_registry = _loop_instrument(datetime.fromisoformat(timestamp.replace("Z", "+00:00")))
    instrument = instrument_registry.require_tradable(INSTRUMENT_ID, datetime.fromisoformat(timestamp.replace("Z", "+00:00")))
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
        "protocol": _LOOP_PROTOCOL, "run_id": run_id,
        "source_build_identity": _simulation_build_identity(),
        "account": ACCOUNT, "provider": PROVIDER, "environment": ENVIRONMENT,
        "strategy_parameters": {"fast": 2, "slow": 3},
        "prices": [canonical_decimal_text(v) for v in values], "start_time": timestamp,
        "risk_policy": "canonical-provider-free-risk-v1",
        "risk_policy_digest": risk_policy_digest(selected_policy),
        "strategy": "moving-average-2-3-long-only-target-1",
        "fee_rate": canonical_decimal_text(FEE_RATE), "initial_cash": canonical_decimal_text(INITIAL_CASH),
        "fault_at_episode": fault_at_episode, "emergency_at_episode": emergency_at_episode,
        "instrument": instrument.to_contract_dict(),
    }
    root = Path(state_dir)
    if any((root / name).exists() for name in ("checkpoint.json", "learning-evidence.jsonl")):
        raise ValueError("legacy state requires a separate autonomous simulation directory")
    root.mkdir(parents=True, exist_ok=True)
    with deny_python_network(), ResourceLock(root / ".canonical-simulation.lock"):
        return _run_autonomous_locked(root, values, protocol, stop_after_episodes, selected_policy)


def _run_autonomous_locked(root, values, protocol, stop_after_episodes, selected_policy):
    from .allocation import AllocationCandidate, AllocationPolicy, StressScenarioEvidence, allocate_targets
    from .durable_order_projection import DurableOrderBookProjection
    from .exact_decimal import exact_abs, exact_subtract, exact_sum, as_fraction, round_fraction_to_quantum
    from .risk_policy_authority import DurableRiskPolicyRegistry, RiskPolicyScope
    from .valuation_authority import DurableValuationBook, diagnostic_mark_observation, evaluate_valuation_freshness

    store = JournalStore(root / "journal.sqlite3")
    run_id = protocol["run_id"]
    events = store.load_events(_LOOP_AGGREGATE, run_id)
    protocol_digest = payload_digest(protocol)
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
    if events[0]["event_type"] != "AutonomousSimulationStarted" or events[0]["payload"]["protocol_digest"] != protocol_digest:
        raise ValueError("autonomous simulation protocol/input identity changed")
    completed = []
    active = None
    for event in events[1:]:
        payload = event["payload"]
        if event["event_type"] == "AutonomousEpisodeStarted":
            if active is not None or payload["episode"] != len(completed) + 1:
                raise ValueError("autonomous episode chronology conflicts")
            active = payload
        elif event["event_type"] == "AutonomousEpisodeCompleted":
            if active is None or payload["episode"] != active["episode"]:
                raise ValueError("autonomous completion lacks matching start")
            completed.append(payload)
            active = None
        else:
            raise ValueError("unsupported autonomous simulation event")
    if active is not None:
        return {"status": "UNKNOWN", "environment": ENVIRONMENT, "mode": "ZERO",
                "run_id": run_id, "completed_episodes": len(completed),
                "unresolved_episode": active["episode"], "new_outbound_requests": 0,
                "reason": "unfinished_episode_requires_reconciliation", "resumed": True}
    state = completed[-1]["provider_state"] if completed else events[0]["payload"]["provider_state"]
    provider = SimulatedProvider.from_state(state)
    economic = DurableProviderEconomicBook(store, provider_id=PROVIDER, account_id=ACCOUNT, environment=ENVIRONMENT)
    economic.append(book_external_cash_flow(
        transaction_id=_uuid("loop-seed-transaction", run_id), cause_event_id=_uuid("loop-seed-cause", run_id),
        currency="USD", amount=protocol["initial_cash"]))
    if provider.cash != economic.cash("USD") or provider.positions.get(INSTRUMENT, Decimal("0")) != economic.position(INSTRUMENT):
        raise ValueError("simulator snapshot conflicts with canonical economic state")
    artifacts = ArtifactStore(root / "artifacts")
    reservations = DurableReservationBook(store, environment=ENVIRONMENT, account_id=ACCOUNT,
        resolution_artifact_store=artifacts, resolution_artifact_root=root / "artifacts")
    orders = DurableOrderBookProjection(store, provider_id=PROVIDER, account_id=ACCOUNT,
        environment=ENVIRONMENT, host_id="local-simulation", owner_epoch="1")
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
        # Every current simulator fill participates; an ACK alone contributes none.
        snapshot = provider.account_snapshot(now=timestamp)
        fills = tuple(ProviderFillEvidence.create(
            provider_id=PROVIDER, account_id=ACCOUNT, environment=ENVIRONMENT,
            provider_execution_id=f["provider_execution_id"],
            client_order_id=next(o.client_order_id for o in provider.orders.values() if o.provider_order_id == f["order_ref"]),
            instrument=f["instrument_version"], quantity=f["last_quantity"]["value"], price=f["last_price"],
            fee_amount=f["fees"][0]["amount"], fee_currency=f["fees"][0]["currency"], trade_time=f["trade_time"],
            side=f["side"], evidence_refs=(f"simulated:fill:{f['provider_execution_id']}",),
        ) for f in provider.activity_fills())
        result = reconcile_account(provider_id=PROVIDER, account_id=ACCOUNT, environment=ENVIRONMENT,
            local_cash={"USD": economic.cash("USD")}, provider_cash={"USD": snapshot["balances"][0]["total"]},
            local_positions={INSTRUMENT: economic.position(INSTRUMENT)},
            provider_positions={item["instrument_version"]: item["quantity"]["value"] for item in snapshot["positions"]},
            local_execution_ids=tuple(f.provider_execution_id for f in fills), provider_fills=fills,
            snapshot_consistency=SnapshotConsistencyEvidence(provider_id=PROVIDER, account_id=ACCOUNT,
                environment=ENVIRONMENT, mode="ATOMIC", query_started_at=timestamp, query_completed_at=timestamp),
            coverage_start=protocol["start_time"], coverage_end=timestamp, pagination_complete=True,
            provider_activity_provider_id=PROVIDER, provider_activity_account_id=ACCOUNT,
            resource_availability=ResourceAvailabilityEvidence(provider_id=PROVIDER, account_id=ACCOUNT,
                environment=ENVIRONMENT, snapshot_id=snapshot["snapshot_id"], query_started_at=timestamp,
                query_completed_at=timestamp, valid_until=(datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                    + timedelta(seconds=60)).isoformat().replace("+00:00", "Z"),
                available_resources={"CASH:USD": snapshot["balances"][0]["available"],
                    f"POSITION:{INSTRUMENT}": canonical_decimal_text(economic.position(INSTRUMENT))},
                provider_as_of=snapshot["provider_as_of"], evidence_refs=(f"simulated:provider-snapshot:{snapshot['snapshot_id']}",)))
        if not result.complete or result.blocks_new_risk:
            raise ValueError("complete simulated economics did not reconcile")
        checkpoint = record_reconciliation_checkpoint(store, reconciliation_id=key, result=result,
            observed_at=timestamp, host_id="local-simulation", owner_epoch="1")
        _deliver_event(store, checkpoint["event_id"])
        return checkpoint, snapshot

    for index in range(len(completed), end):
        episode = index + 1
        key = f"{run_id}:{episode}"
        point = started_at + timedelta(seconds=index)
        instrument = instruments.require_tradable(INSTRUMENT_ID, point)
        instrument.validate_price(values[index])
        timestamp = point.isoformat().replace("+00:00", "Z")
        future = (point + timedelta(seconds=60)).isoformat().replace("+00:00", "Z")
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
        price = mark.mark
        equity = exact_add(cash, exact_multiply(position, price))
        peak = max([INITIAL_CASH, equity, *previous_equities])
        drawdown = round_fraction_to_quantum(as_fraction(exact_subtract(peak, equity)) / as_fraction(peak),
                                             Decimal("0.000000000000000001"), mode="CEILING")
        proposal = MovingAverageStrategy(**protocol["strategy_parameters"]).decide(values[:episode], Decimal("1"))
        target_quantity = Decimal("1") if proposal.side == "BUY" else Decimal("0") if proposal.side == "SELL" else position
        decision = "BUY" if target_quantity > position else "REDUCE" if target_quantity < position else "HOLD" if proposal.side == "HOLD" else "NO_TRADE"
        emergency = protocol["emergency_at_episode"] is not None and episode >= protocol["emergency_at_episode"]
        if emergency:
            decision = "NO_TRADE"
            target_quantity = position
        allocation = allocate_targets((AllocationCandidate.create(symbol=INSTRUMENT,
            desired_notional=exact_multiply(target_quantity, price), price=price, lot_size=instrument.quantity_step,
            current_quantity=position, cost_rate=FEE_RATE, turnover_cost_rate=FEE_RATE,
            holding_cost_rate="0", max_executable_notional="1000"),),
            AllocationPolicy.create(cash_available=exact_subtract(cash, reservations.total_reserved("CASH:USD")),
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

            authority = AuthorityService(store, risk_policy_scope=scope, risk_authority_resolver=resolve_risk)
            policy_id = _uuid("loop-authority-policy", key)
            authority.register_policy(AuthorityPolicy.create(policy_id=policy_id, account_id=ACCOUNT,
                environments={ENVIRONMENT}, instruments={(INSTRUMENT_ID, 1)}, actions={"ORDER.SUBMIT"},
                max_notional="1000", expires_at=future, autonomous=True, protection_only=False, version=1))
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
                reservation_available={resource: cash},
                reservation_checkpoint_event_id=checkpoint["event_id"], reservation_provider_id=PROVIDER,
                reservation_max_age_seconds="60", now=timestamp)
            if admission.outcome != "ADMITTED":
                status = "RISK_REJECTED"
            else:
                attempt_id = _uuid("loop-attempt", key)
                order_id = stable_client_order_id("simulated", intent_id, environment=ENVIRONMENT, account_id=ACCOUNT)
                prepared = orders.create_order(event_key=f"{key}:intent", client_order_id=order_id,
                    instrument=INSTRUMENT, side=side, requested_quantity=exact_abs(quantity), committed_at=timestamp)
                _deliver_event(store, prepared.event_id)
                request = {"attempt_id": attempt_id, "instrument_version": INSTRUMENT, "side": side,
                    "quantity": canonical_decimal_text(exact_abs(quantity)), "price": canonical_decimal_text(price), "now": timestamp}
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
                fresh_fills = [f for f in provider.activity_fills() if f["order_ref"] == dispatched.response["provider_order_id"]]
                if len(fresh_fills) != 1:
                    raise ValueError("ACK is not fill evidence")
                fill = fresh_fills[0]
                fill_id = fill["provider_execution_id"]
                commit_economic_batch_with_reservation_consumption(economic, reservations,
                    command_id=_uuid("loop-fill-command", key), idempotency_key=_uuid("loop-fill-command", key),
                    reservation_id=reservation_id, usage={resource: required}, transactions=(book_equity_fill(
                        transaction_id=_uuid("loop-fill-transaction", key), cause_event_id=fill_id, instrument=INSTRUMENT,
                        settlement_currency="USD", side=fill["side"], quantity=fill["last_quantity"]["value"],
                        price=fill["last_price"], fee=fill["fees"][0]["amount"], fee_currency="USD"),), committed_at=timestamp,
                    order_projection=orders, order_fill={
                        "event_key": f"{key}:fill", "client_order_id": order_id, "fill_id": fill_id,
                        "provider_execution_id": fill_id, "quantity": fill["last_quantity"]["value"],
                        "price": fill["last_price"], "provider_revision": None, "evidence_refs": fill["evidence"],
                    })
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
        previous_equities.append(equity)
    return {"status": "COMPLETED" if len(completed) == len(values) else "PAUSED", "environment": ENVIRONMENT,
        "mode": "ZERO", "run_id": run_id, "protocol_digest": protocol_digest,
        "completed_episodes": len(completed), "cash": canonical_decimal_text(economic.cash("USD")),
        "position": canonical_decimal_text(economic.position(INSTRUMENT)), "reconciled": True,
        "new_outbound_requests": provider.outbound_request_count - count_before,
        "resumed": count_before > 0 or len(events) > 1,
        "decisions": [{k: v for k, v in item.items() if k != "provider_state"} for item in completed],
        "economic_edge_status": "INCONCLUSIVE"}
