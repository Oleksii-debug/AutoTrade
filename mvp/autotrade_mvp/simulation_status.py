"""Operator projections of the existing canonical simulation authorities.

Reading never submits, reconciles, releases a reservation or writes an event.
An open position has no current valuation: the session retains an input hash,
not a market mark. Cash reconciliation alone cannot supply portfolio P&L.
"""

from __future__ import annotations

from pathlib import Path
from decimal import Context, Decimal, localcontext, InvalidOperation, DivisionByZero, Overflow, Inexact, Rounded
from contextlib import closing
import sqlite3

from autotrade_numeric import (
    canonical_decimal_text, exact_subtract, exact_sum, parse_bounded_exact_decimal,
    MAX_INTEGER_DIGITS, MAX_SCALE,
)

from .durable_reservations import DurableReservationBook
from .persistence import JournalStore
from .provider_activity_accounting import DurableProviderEconomicBook
from .simulation_session import ACCOUNT, ENVIRONMENT, INITIAL_CASH, INSTRUMENT, PROVIDER
from research.autotrade_research.artifacts.resource_lock import ResourceLock, ResourceLockBusyError


class SimulationStateChanging(ValueError):
    """A writer advanced the journal while the operator projection was read."""


def _decimal(value: object):
    if type(value) is not str:
        raise ValueError("simulation amounts must be exact decimal strings")
    return parse_bounded_exact_decimal(value)


def _scope(payload: dict) -> None:
    if (payload.get("provider_id") != PROVIDER
            or payload.get("account_id") != ACCOUNT
            or payload.get("environment") != ENVIRONMENT):
        raise ValueError("simulation evidence scope differs")


def _existing_store(path: Path) -> JournalStore:
    # Status must not create a missing database or upgrade an old database.
    # Reuse the canonical store for all event integrity and financial replay.
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        versions = [row[0] for row in connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        )]
    if versions != list(range(1, JournalStore.SCHEMA_VERSION + 1)):
        raise ValueError("simulation journal requires an explicit schema migration")
    return JournalStore(path)


def _balance(book: DurableProviderEconomicBook, account: str, unit: str):
    # Older canonical book projections still use ambient Decimal sums. Read
    # the same replayed immutable postings through the shared exact helper.
    return exact_sum(posting.signed_amount for transaction in book.transactions
                     for posting in transaction.postings
                     if posting.ledger_account == account and posting.asset_or_currency == unit)


def inspect_canonical_simulation(state_dir: str | Path, *, history_limit: int = 0) -> dict | None:
    """Return one bounded, coherent operator read; None means no journal exists.

    This local SIMULATION projection does not issue financial or provider truth.
    A journal takes precedence over legacy files, including when it is corrupt.
    """
    # Pending canonical accounting/reservation successors eliminate their own
    # ambient arithmetic. Until then, give those existing replay consumers an
    # exact, bounded read context. Preflight below bounds at most 10^7 postings;
    # any rounding fails closed instead of changing the displayed state.
    context = Context(prec=MAX_INTEGER_DIGITS + MAX_SCALE + 7,
                      Emin=-MAX_SCALE - 7, Emax=MAX_INTEGER_DIGITS + 7,
                      traps=[InvalidOperation, DivisionByZero, Overflow, Inexact, Rounded])
    if type(history_limit) is not int or not 0 <= history_limit <= 1000:
        raise ValueError("history_limit must be between 0 and 1000")
    root = Path(state_dir)
    path = root / "journal.sqlite3"
    if not path.exists() and not path.is_symlink():
        return None
    try:
        with ResourceLock(root / ".canonical-simulation.lock"), localcontext(context):
            return _inspect(root, history_limit=history_limit)
    except ResourceLockBusyError as error:
        raise SimulationStateChanging("simulation writer is active") from error


def _inspect(state_dir: str | Path, *, history_limit: int) -> dict | None:
    if type(history_limit) is not int or not 0 <= history_limit <= 1000:
        raise ValueError("history_limit must be between 0 and 1000")
    path = Path(state_dir) / "journal.sqlite3"
    if not path.exists() and not path.is_symlink():
        return None
    if not path.is_file() or path.is_symlink():
        raise ValueError("simulation journal must be a regular file")
    store = _existing_store(path)
    cut = store.current_journal_sequence()
    if cut > 100000:
        raise ValueError("single-episode simulation journal exceeds the operator read bound")
    events = store.load_events_after_journal_sequence(0, limit=max(cut, 1))
    if len(events) != cut:
        raise SimulationStateChanging("journal changed during operator read")
    if events and all(event["aggregate_type"] == "simulation_portfolio"
                      and event["event_type"] == "SimulationEpisodeRecorded"
                      and event.get("environment") == ENVIRONMENT
                      for event in events):
        if store.current_journal_sequence() != cut:
            raise SimulationStateChanging("journal changed during operator read")
        return None
    for event in events:
        if event["aggregate_type"] == "reservation_book":
            payload = event["payload"]
            if (type(payload) is not dict or payload.get("account_id") != ACCOUNT
                    or payload.get("environment") != ENVIRONMENT):
                raise ValueError("simulation reservation scope differs")
            snapshot = payload.get("snapshot")
            if type(snapshot) is not dict:
                raise ValueError("simulation reservation snapshot is malformed")
            for name in ("original", "remaining", "consumed"):
                amounts = snapshot.get(name)
                if type(amounts) is not dict or len(amounts) > 10:
                    raise ValueError("simulation reservation amount map is malformed")
                for amount in amounts.values():
                    _decimal(amount)
        if event["aggregate_type"] != "economic_book":
            continue
        payload = event["payload"]
        if type(payload) is not dict:
            raise ValueError("simulation economic payload is malformed")
        _scope(payload)
        transactions = payload.get("transactions")
        if type(transactions) is not list or not 1 <= len(transactions) <= 10:
            raise ValueError("single-episode simulation transaction bound differs")
        for transaction in transactions:
            if type(transaction) is not dict:
                raise ValueError("simulation transaction is malformed")
            postings = transaction.get("postings")
            if type(postings) is not list or not 2 <= len(postings) <= 10:
                raise ValueError("single-episode simulation posting bound differs")
            for posting in postings:
                if type(posting) is not dict:
                    raise ValueError("simulation posting is malformed")
                _decimal(posting.get("signed_amount"))
    sessions = [event for event in events
                if event["aggregate_type"] == "canonical_simulation_session"]
    if sessions:
        if (len(sessions) not in {1, 2}
                or any(event["aggregate_id"] != "single-episode"
                       or event["aggregate_version"] != index
                       or event.get("environment") != ENVIRONMENT
                       for index, event in enumerate(sessions, 1))
                or sessions[0]["event_type"] != "SimulationSessionStarted"):
            raise ValueError("simulation session chronology is invalid")
        started = sessions[0]["payload"]
        if type(started) is not dict:
            raise ValueError("simulation session payload must be an object")
        episode_id = started.get("episode_id")
        if (type(episode_id) is not str or not episode_id.strip()
                or started.get("environment") != ENVIRONMENT
                or started.get("decision") not in {"BUY", "HOLD"}
                or type(started.get("input_hash")) is not str):
            raise ValueError("simulation session identity is invalid")
    else:
        episode_id = None
        started = {}

    book = DurableProviderEconomicBook(
        store, provider_id=PROVIDER, account_id=ACCOUNT, environment=ENVIRONMENT,
    )
    if not book.transactions:
        raise ValueError("journal has no canonical simulation economic state")
    cash = _balance(book, "CASH:USD", "USD")
    position = _balance(book, f"POSITION:{INSTRUMENT}", INSTRUMENT)
    initial_cash = exact_subtract(Decimal("0"), _balance(book, "EXTERNAL_EQUITY:USD", "USD"))
    if initial_cash != INITIAL_CASH:
        raise ValueError("simulation starting capital differs from the canonical fixture")
    reservations = DurableReservationBook(store, environment=ENVIRONMENT, account_id=ACCOUNT)
    active = reservations.active()
    status = {
        "status": "needs_recovery", "state_format": "canonical_journal",
        "environment": ENVIRONMENT, "episode_id": episode_id,
        "symbol": INSTRUMENT, "currency": "USD", "quantity_unit": "share",
        "initial_cash": canonical_decimal_text(initial_cash),
        "cash": canonical_decimal_text(cash), "position": canonical_decimal_text(position),
        "journal_sequence": str(cut), "evidence_count": cut,
        "decision": started.get("decision"), "reconciled": False,
        "replay_verified": True, "session_status": "UNKNOWN",
        "new_outbound_requests": 0, "fills": {},
        "reason": ("incomplete_send_requires_reconciliation" if sessions
                   else "orphaned_durable_state_requires_reconciliation"),
        "active_reservations": [
            {"reservation_id": item.reservation_id, "state": item.state,
             "remaining": {key: canonical_decimal_text(value)
                           for key, value in sorted(item.remaining.items())},
             "consumed": {key: canonical_decimal_text(value)
                          for key, value in sorted(item.consumed.items())}}
            for item in active
        ],
        "economic_edge_claim": "UNPROVEN_SIMULATION_ONLY",
    }
    report = None
    if len(sessions) == 2:
        completed = sessions[1]
        result = completed["payload"]
        if type(result) is not dict:
            raise ValueError("completed simulation payload must be an object")
        if (completed["event_type"] != "SimulationSessionCompleted"
                or result.get("episode_id") != episode_id
                or result.get("environment") != ENVIRONMENT
                or result.get("decision") != started["decision"]
                or result.get("reconciled") is not True
                or _decimal(result.get("cash")) != cash
                or _decimal(result.get("position")) != position):
            raise ValueError("completed simulation differs from durable economics")
        session_status = result.get("status")
        if session_status not in {"HOLD", "RISK_REJECTED", "FILL_RECONCILED_ORDER_UNCONFIRMED"}:
            raise ValueError("unsupported completed simulation outcome")
        reconciliation = next((event for event in events
                               if event["event_id"] == result.get("reconciliation_event_id")), None)
        if (reconciliation is None or reconciliation["event_type"] != "AccountReconciled"
                or reconciliation["journal_sequence"] >= completed["journal_sequence"]):
            raise ValueError("simulation reconciliation evidence is missing")
        evidence = reconciliation["payload"]
        if (type(evidence) is not dict or type(evidence.get("provider_cash")) is not dict
                or type(evidence.get("provider_positions")) is not dict):
            raise ValueError("simulation reconciliation payload is malformed")
        _scope(evidence)
        if (evidence.get("complete") is not True
                or evidence.get("snapshot_consistent") is not True
                or evidence.get("reasons") != []
                or evidence.get("blocking_resources") != []
                or _decimal(evidence.get("provider_cash", {}).get("USD")) != cash
                or _decimal(evidence.get("provider_positions", {}).get(INSTRUMENT, "0")) != position):
            raise ValueError("simulation reconciliation is incomplete or differs")
        fill_id = result.get("fill_id")
        is_fill = session_status == "FILL_RECONCILED_ORDER_UNCONFIRMED"
        if is_fill:
            if (result["decision"] != "BUY" or position <= 0
                    or type(fill_id) is not str or not fill_id
                    or type(result.get("order_id")) is not str or not result["order_id"]
                    or fill_id not in evidence.get("matched_execution_ids", [])
                    or not any(tx.cause_event_id == fill_id for tx in book.transactions)):
                raise ValueError("completed simulation fill is not durably evidenced")
            status["fills"] = {fill_id: {"instrument": INSTRUMENT,
                                        "quantity": canonical_decimal_text(position)}}
        elif (fill_id is not None or result.get("order_id") is not None or position != 0
              or (session_status == "HOLD" and result["decision"] != "HOLD")
              or (session_status == "RISK_REJECTED" and result["decision"] != "BUY")):
            raise ValueError("non-fill simulation contains unexpected exposure")
        status.update(
            status="awaiting_order_reconciliation" if is_fill or active else "completed",
            session_status=session_status, reconciled=True,
            reason="order_terminal_state_unconfirmed" if is_fill or active else None,
            reconciliation_event_id=reconciliation["event_id"],
        )
        fees = _balance(book, "FEE_EXPENSE:USD", "USD")
        turnover = _balance(book, "CLEARING:USD", "USD")
        if fees < 0 or turnover < 0:
            raise ValueError("single-episode simulated fees or turnover are invalid")
        # No retained market mark exists. In particular, neither a user price
        # nor the execution's cost basis is silently substituted for one.
        valued = position == 0
        report = {
            "environment": ENVIRONMENT, "currency": "USD",
            "initial_equity": canonical_decimal_text(initial_cash),
            "cash": canonical_decimal_text(cash),
            "final_equity": canonical_decimal_text(cash) if valued else None,
            "net_pnl": canonical_decimal_text(exact_subtract(cash, initial_cash)) if valued else None,
            "total_fees": canonical_decimal_text(fees),
            "turnover": canonical_decimal_text(turnover),
            "ending_position": canonical_decimal_text(position),
            "trade_count": len(status["fills"]), "reconciled": True,
            "valuation_status": "CASH_ONLY" if valued else "MARK_UNAVAILABLE",
            "economic_edge_claim": "UNPROVEN_SIMULATION_ONLY",
            "journal_sequence": str(cut),
        }
        if completed["journal_sequence"] != cut:
            # This entrypoint owns one bounded episode. Later financial/host
            # activity cannot inherit the historical completed checkpoint.
            status.update(status="needs_recovery", reconciled=False,
                          reason="journal_advanced_after_session_completion")
            report = None
    if history_limit:
        status["history"] = [
            {"journal_sequence": str(event["journal_sequence"]),
             "event_id": event["event_id"], "event_type": event["event_type"],
             "committed_at": event["committed_at"]}
            for event in events[-history_limit:]
        ]
    if store.current_journal_sequence() != cut:
        raise SimulationStateChanging("journal changed during operator read")
    return {"status": status, "economic_report": report}
