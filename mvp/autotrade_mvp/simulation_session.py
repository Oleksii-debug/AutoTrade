from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from research.autotrade_research.io import ResourceLock

from .accounting import FillAccountingAuthority, economic_state_from_journal
from .authority import (
    AuthorityContext,
    EquityOrderAdmission,
    RiskAuthority,
    RiskLimits,
    RiskSnapshot,
)
from .provider_economic import DurableProviderEconomicBook
from .persistence import JournalStore, payload_digest
from .reconciliation import (
    ProviderFillEvidence,
    ResourceAvailabilityEvidence,
    SnapshotConsistencyEvidence,
    reconcile_account,
)
from .reservations import ReservationLedger
from .simulation import (
    FEE_RATE,
    INITIAL_CASH,
    MovingAverageStrategy,
    SimulatedProvider,
    canonical_decimal_text,
    exact_add,
    exact_multiply,
)

PROVIDER = "SIMULATED"
ACCOUNT = "simulated-account"
ENVIRONMENT = "SIMULATION"
INSTRUMENT = "SIM:XYZ:SPOT"


def _prices(values: list[str]) -> list[Decimal]:
    if not isinstance(values, list) or not values:
        raise ValueError("prices must be a non-empty list")
    result: list[Decimal] = []
    for value in values:
        if type(value) is not str or not value.strip():
            raise TypeError("prices must contain canonical decimal strings")
        result.append(Decimal(value))
    return result


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _risk_snapshot(price: Decimal) -> RiskSnapshot:
    return RiskSnapshot(
        state_version=1, equity=str(INITIAL_CASH), positions={},
        marks={INSTRUMENT: canonical_decimal_text(price)}, reserved_position_delta={},
        daily_pnl="0", drawdown_fraction="0", market_data_age_seconds="1",
        fx_age_seconds={"USD": "1"}, margin_headroom="1",
        capability_allowed=True, borrow_available=True,
        stress_scenarios=({INSTRUMENT: "-0.25"},),
    )


def _reconcile(
    provider: SimulatedProvider,
    economic: DurableProviderEconomicBook,
    now: str,
    *,
    provider_fill: ProviderFillEvidence | None = None,
):
    snapshot = provider.account_snapshot(now=now)
    fills = () if provider_fill is None else (provider_fill,)
    ids = () if provider_fill is None else (provider_fill.provider_execution_id,)
    result = reconcile_account(
        provider_id=PROVIDER, account_id=ACCOUNT, environment=ENVIRONMENT,
        local_cash={"USD": economic.cash("USD")},
        provider_cash={"USD": snapshot["balances"][0]["total"]},
        local_positions=(
            {INSTRUMENT: economic.position(INSTRUMENT)}
            if provider_fill is not None
            else {}
        ),
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
        ) if provider_fill is None else None),
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


def _run_locked(
    root: Path,
    *,
    episode_id: str,
    input_hash: str,
    decision,
    now: str | None,
    fault_after_send: bool,
    buy_requirements,
) -> dict[str, object]:
    timestamp = now or _utc_now()
    journal = JournalStore(root / "journal.sqlite3")
    provider = SimulatedProvider(root / "provider.sqlite3", starting_cash=INITIAL_CASH)
    reservations = ReservationLedger(root / "reservations.sqlite3")
    accounting = FillAccountingAuthority(journal)
    economic = DurableProviderEconomicBook(
        journal=journal,
        provider_id=PROVIDER,
        account_id=ACCOUNT,
        environment=ENVIRONMENT,
        initial_cash={"USD": INITIAL_CASH},
    )

    started_event_id = f"simulation:{episode_id}:started"
    if journal.event(started_event_id) is None:
        payload = {
            "episode_id": episode_id,
            "input_hash": input_hash,
            "decision_side": decision.side,
            "decision_quantity": canonical_decimal_text(decision.quantity),
            "decision_price": canonical_decimal_text(decision.price),
        }
        journal.append_event({
            "event_id": started_event_id,
            "event_type": "SimulationEpisodeStarted",
            "aggregate_type": "simulation_episode",
            "aggregate_id": episode_id,
            "aggregate_version": 1,
            "payload": payload,
            "payload_hash": payload_digest(payload),
            "committed_at": timestamp,
        })
    else:
        existing = journal.event(started_event_id)
        if existing is None or existing["payload"]["input_hash"] != input_hash:
            raise ValueError("episode_id already exists with different inputs")

    if decision.side == "HOLD":
        admission_reconciliation, snapshot = _reconcile(provider, economic, timestamp)
        return {
            "episode_id": episode_id,
            "input_hash": input_hash,
            "decision": asdict(decision),
            "provider_snapshot": snapshot,
            "reconciliation": asdict(admission_reconciliation),
            "reservation": None,
            "provider_execution": None,
            "economic_state": economic_state_from_journal(journal),
        }

    amount, required = buy_requirements
    authority = RiskAuthority(
        RiskLimits(
            max_order_notional="1000000",
            max_position_abs={INSTRUMENT: "1000000"},
            max_gross_exposure="1000000",
            max_net_exposure="1000000",
            min_margin_headroom="0",
            max_daily_loss="1000000",
            max_drawdown_fraction="1",
            max_market_data_age_seconds="60",
            max_fx_age_seconds="60",
            stress_max_loss="1000000",
        ),
        reservation_ledger=reservations,
    )
    snapshot = _risk_snapshot(decision.price)
    admission = authority.admit(
        EquityOrderAdmission(
            order_id=f"simulation:{episode_id}:order",
            account_id=ACCOUNT,
            instrument_version=INSTRUMENT,
            side="BUY",
            quantity=canonical_decimal_text(decision.quantity),
            limit_price=canonical_decimal_text(decision.price),
            currency="USD",
        ),
        snapshot=snapshot,
        context=AuthorityContext(
            account_id=ACCOUNT,
            environment=ENVIRONMENT,
            provider_id=PROVIDER,
        ),
        now=timestamp,
        available_cash={"USD": str(provider.available_cash("USD"))},
    )
    reservation = reservations.get(admission.reservation_id)
    if reservation is None:
        raise RuntimeError("simulation reservation was not persisted")

    provider_execution = provider.submit_market_buy(
        client_order_id=admission.order_id,
        instrument_version=INSTRUMENT,
        quantity=decision.quantity,
        price=decision.price,
        now=timestamp,
        fault_after_send=fault_after_send,
    )
    if fault_after_send:
        return {
            "episode_id": episode_id,
            "input_hash": input_hash,
            "decision": asdict(decision),
            "provider_snapshot": provider.account_snapshot(now=timestamp),
            "reconciliation": None,
            "reservation": reservation,
            "provider_execution": provider_execution,
            "economic_state": economic_state_from_journal(journal),
        }

    fill = provider.require_fill(provider_execution.provider_execution_id)
    provider_fill = ProviderFillEvidence(
        provider_id=PROVIDER,
        account_id=ACCOUNT,
        environment=ENVIRONMENT,
        provider_execution_id=provider_execution.provider_execution_id,
        client_order_id=admission.order_id,
        instrument_version=INSTRUMENT,
        side="BUY",
        quantity=canonical_decimal_text(fill.quantity),
        price=canonical_decimal_text(fill.price),
        fee_currency="USD",
        fee=canonical_decimal_text(fill.fee),
        filled_at=fill.filled_at,
    )
    accounting.record_provider_fill(
        provider_fill=provider_fill,
        admission=admission,
        reservation=reservation,
        committed_at=timestamp,
    )
    economic.apply_fill(provider_fill, committed_at=timestamp)
    admission_reconciliation, snapshot = _reconcile(
        provider, economic, timestamp, provider_fill=provider_fill)
    return {
        "episode_id": episode_id,
        "input_hash": input_hash,
        "decision": asdict(decision),
        "provider_snapshot": snapshot,
        "reconciliation": asdict(admission_reconciliation),
        "reservation": reservation,
        "provider_execution": provider_execution,
        "economic_state": economic_state_from_journal(journal),
    }


def _parse_cli() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="AutoTrade deterministic canonical simulation")
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--episode-id", required=True)
    parser.add_argument("--prices", required=True)
    parser.add_argument("--at")
    parser.add_argument("--fault-after-send", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = _parse_cli()
    result = run_canonical_simulation(
        args.prices.split(","), args.state_dir,
        episode_id=args.episode_id, now=args.at, fault_after_send=args.fault_after_send,
    )
    print(json.dumps(result, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
