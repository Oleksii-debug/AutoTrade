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
import re

from autotrade_numeric import (
    canonical_decimal_text, exact_subtract, exact_sum, parse_bounded_exact_decimal,
    MAX_INTEGER_DIGITS, MAX_SCALE,
)

from .durable_reservations import DurableReservationBook
from .accounting import book_external_cash_flow, canonical_transaction
from .dispatch import stable_client_order_id, submission_attempt_aggregate_id
from .persistence import JournalStore
from .provider_activity_accounting import DurableProviderEconomicBook
from .reconciliation_journal import load_latest_reconciliation_checkpoint
from .simulation_session import ACCOUNT, ENVIRONMENT, INITIAL_CASH, INSTRUMENT, INSTRUMENT_ID, PROVIDER, _uuid
from research.autotrade_research.artifacts.resource_lock import ResourceLock, ResourceLockBusyError


_ECONOMIC_UNITS = frozenset({
    ("CASH:USD", "USD"), ("EXTERNAL_EQUITY:USD", "USD"),
    ("CLEARING:USD", "USD"), ("FEE_EXPENSE:USD", "USD"),
    (f"POSITION:{INSTRUMENT}", INSTRUMENT),
    (f"CLEARING:{INSTRUMENT}", INSTRUMENT),
})


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


def _require_complete_reconciliation(event: dict, *, cash, position, fill_id) -> None:
    """Check the whole bounded fixture, rather than a self-asserted complete flag."""
    evidence = event["payload"]
    if (event["aggregate_type"] != "account_reconciliation"
            or event.get("environment") != ENVIRONMENT
            or type(evidence) is not dict):
        raise ValueError("simulation reconciliation owner differs")
    _scope(evidence)
    if (evidence.get("complete") is not True
            or evidence.get("snapshot_consistent") is not True
            or evidence.get("activity_coverage_complete") is not True
            or evidence.get("checkpoint_owner") != {"host_id": "local-simulation", "owner_epoch": "1"}):
        raise ValueError("simulation reconciliation is incomplete")
    observed_at = evidence.get("observed_at")
    if (type(observed_at) is not str or not observed_at
            or event.get("observed_at") != observed_at
            or event.get("committed_at") != observed_at
            or event.get("host_id") != "local-simulation"
            or event.get("owner_epoch") != "1"
            or evidence.get("snapshot") != {
                "mode": "ATOMIC", "query_started_at": observed_at, "query_completed_at": observed_at,
            }):
        raise ValueError("simulation reconciliation snapshot identity differs")
    empty_lists = (
        "reasons", "blocking_resources", "unexpected_execution_ids",
        "missing_local_execution_ids", "unexpected_provider_fill_bindings",
        "unexpected_working_provider_order_ids", "missing_local_working_client_order_ids",
        "unexpected_provider_activity_ids", "missing_local_provider_activity_ids",
        "manual_or_external_activity_ids", "submission_resolutions",
        "matched_working_client_order_ids", "matched_provider_activity_ids",
    )
    if any(evidence.get(name) != [] for name in empty_lists):
        raise ValueError("simulation reconciliation has unexpected or unresolved activity")
    for name in ("cash_differences", "position_differences", "borrow_differences"):
        amounts = evidence.get(name)
        permitted = {"USD"} if name == "cash_differences" else {INSTRUMENT} if name == "position_differences" else set()
        if (type(amounts) is not dict or set(amounts) - permitted
                or any(_decimal(value) != 0 for value in amounts.values())):
            raise ValueError("simulation reconciliation differences remain")
    if evidence.get("matched_execution_ids") != ([] if fill_id is None else [fill_id]):
        raise ValueError("simulation reconciled executions differ")
    provider_cash = evidence.get("provider_cash")
    provider_positions = evidence.get("provider_positions")
    if (type(provider_cash) is not dict or set(provider_cash) != {"USD"}
            or _decimal(provider_cash["USD"]) != cash
            or type(provider_positions) is not dict
            or set(provider_positions) - {INSTRUMENT}
            or _decimal(provider_positions.get(INSTRUMENT, "0")) != position):
        raise ValueError("simulation reconciliation units or amounts differ")


def _require_completed_send(events: list[dict], *, episode_id: str, order_id: str) -> None:
    """An economic fill must belong to this episode's durable submission."""
    intent_id = _uuid("intent", episode_id)
    attempt_id = _uuid("attempt", episode_id)
    client_id = stable_client_order_id("simulated", intent_id, environment=ENVIRONMENT, account_id=ACCOUNT)
    aggregate_id = submission_attempt_aggregate_id(environment=ENVIRONMENT, account_id=ACCOUNT, attempt_id=attempt_id)
    submissions = [event for event in events if event["aggregate_type"] == "submission_attempt"]
    if (order_id != client_id or len(submissions) != 3
            or [event["event_type"] for event in submissions] != ["SubmissionPrepared", "SubmissionSending", "SubmissionSent"]
            or any(event["aggregate_id"] != aggregate_id
                   or event["aggregate_version"] != index
                   or event.get("environment") != ENVIRONMENT
                   or event.get("host_id") != "local-mvp"
                   or event.get("owner_epoch") != "1"
                   or type(event["payload"]) is not dict
                   or event["payload"].get("client_order_id") != client_id
                   for index, event in enumerate(submissions, 1))):
        raise ValueError("simulation fill submission chronology differs")
    prepared = submissions[0]["payload"]
    sending = submissions[1]["payload"]
    response = submissions[-1]["payload"].get("response")
    if (prepared.get("intent_id") != intent_id or prepared.get("attempt_id") != attempt_id
            or prepared.get("provider") != "simulated"
            or prepared.get("environment") != ENVIRONMENT or prepared.get("account_id") != ACCOUNT
            or prepared.get("owner_token") != "canonical-simulation-owner"
            or prepared.get("owner_epoch") != 1
            or sending.get("owner_token") != "canonical-simulation-owner"
            or sending.get("owner_epoch") != 1
            or sending.get("reason") != "final_send_barrier_passed"
            or type(response) is not dict or response.get("outcome") != "ACKNOWLEDGED"
            or response.get("attempt_id") != attempt_id or response.get("client_order_id") != client_id):
        raise ValueError("simulation fill submission identity differs")


def _require_recorded_admission(events: list[dict], *, episode_id: str, outcome: str) -> dict:
    """Display a recorded outcome only for the same intent and risk reference.

    This is historical SIMULATION attribution. It never evaluates risk or
    grants current admission, dispatch or retry authority.
    """
    admissions = [event for event in events if event["event_type"] == "AuthorityAdmissionRecorded"]
    if len(admissions) != 1:
        raise ValueError("simulation admission evidence differs")
    admission = admissions[0]
    payload = admission["payload"]
    if (admission["aggregate_type"] != "authority_state" or admission["aggregate_id"] != "canonical"
            or type(payload) is not dict or payload.get("outcome") != outcome
            or payload.get("environment") != ENVIRONMENT or payload.get("account_id") != ACCOUNT
            or payload.get("action") != "ORDER.SUBMIT"
            or payload.get("instrument") != {"instrument_id": INSTRUMENT_ID, "version": 1}
            or payload.get("admission_id") != _uuid("admission", episode_id)
            or payload.get("intent_id") != _uuid("intent", episode_id)
            or payload.get("financial_command_id") != _uuid("financial-command", episode_id)):
        raise ValueError("simulation admission identity differs")
    risks = [event for event in events if event["event_type"] == "RiskDecisionRecorded"]
    if (len(risks) != 1 or risks[0]["aggregate_type"] != "risk_decision"
            or risks[0]["aggregate_id"] != payload.get("risk_decision_id")
            or type(risks[0]["payload"]) is not dict
            or risks[0]["payload"].get("decision_id") != payload.get("risk_decision_id")
            or risks[0]["payload"].get("intent_hash") != payload.get("intent_hash")
            or risks[0]["payload"].get("verdict") != ("ALLOW" if outcome == "ADMITTED" else "REJECT")
            or not risks[0]["journal_sequence"] < admission["journal_sequence"]):
        raise ValueError("simulation recorded risk attribution differs")
    return admission


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
                    or event.get("environment") not in {None, ENVIRONMENT}
                    or payload.get("environment") != ENVIRONMENT):
                raise ValueError("simulation reservation scope differs")
            snapshot = payload.get("snapshot")
            if type(snapshot) is not dict:
                raise ValueError("simulation reservation snapshot is malformed")
            for name in ("original", "remaining", "consumed"):
                amounts = snapshot.get(name)
                if type(amounts) is not dict or len(amounts) > 10:
                    raise ValueError("simulation reservation amount map is malformed")
                if set(amounts) != {"CASH:USD"}:
                    raise ValueError("single-episode simulation reservation resources differ")
                for amount in amounts.values():
                    _decimal(amount)
        if event["aggregate_type"] != "economic_book":
            continue
        payload = event["payload"]
        if type(payload) is not dict:
            raise ValueError("simulation economic payload is malformed")
        if event.get("environment") not in {None, ENVIRONMENT}:
            raise ValueError("simulation economic envelope scope differs")
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
                if (posting.get("ledger_account"), posting.get("asset_or_currency")) not in _ECONOMIC_UNITS:
                    raise ValueError("single-episode simulation economic units differ")
    if any(event.get("event_type") == "AccountReconciled" and
           event.get("aggregate_type") != "account_reconciliation" for event in events):
        raise ValueError("simulation reconciliation has an invalid aggregate owner")
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
                or type(started.get("input_hash")) is not str
                or re.fullmatch(r"sha256:[0-9a-f]{64}", started["input_hash"]) is None
                or sessions[0]["event_id"] != _uuid("SimulationSessionStarted", episode_id)):
            raise ValueError("simulation session identity is invalid")
    else:
        episode_id = None
        started = {}

    book = DurableProviderEconomicBook(
        store, provider_id=PROVIDER, account_id=ACCOUNT, environment=ENVIRONMENT,
    )
    if not book.transactions:
        raise ValueError("journal has no canonical simulation economic state")
    if any(event["aggregate_id"] != book.book_id for event in events
           if event["aggregate_type"] == "economic_book"):
        raise ValueError("simulation journal contains another economic book")
    cash = _balance(book, "CASH:USD", "USD")
    position = _balance(book, f"POSITION:{INSTRUMENT}", INSTRUMENT)
    initial_cash = exact_subtract(Decimal("0"), _balance(book, "EXTERNAL_EQUITY:USD", "USD"))
    if initial_cash != INITIAL_CASH:
        raise ValueError("simulation starting capital differs from the canonical fixture")
    reservations = DurableReservationBook(store, environment=ENVIRONMENT, account_id=ACCOUNT)
    if any(event["aggregate_id"] != reservations.scope_id for event in events
           if event["aggregate_type"] == "reservation_book"):
        raise ValueError("simulation journal contains another reservation book")
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
                or completed["event_id"] != _uuid("SimulationSessionCompleted", episode_id)
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
        fill_id = result.get("fill_id")
        is_fill = session_status == "FILL_RECONCILED_ORDER_UNCONFIRMED"
        seed = book_external_cash_flow(
            transaction_id=_uuid("seed-transaction", episode_id),
            cause_event_id=_uuid("seed-cause", episode_id), currency="USD", amount=INITIAL_CASH,
        )
        if (len(book.transactions) != (2 if is_fill else 1)
                or canonical_transaction(book.transactions[0]) != canonical_transaction(seed)):
            raise ValueError("completed simulation economic history differs")
        reconciliation = next((event for event in events
                               if event["event_id"] == result.get("reconciliation_event_id")), None)
        if (reconciliation is None or reconciliation["event_type"] != "AccountReconciled"
                or reconciliation["journal_sequence"] >= completed["journal_sequence"]):
            raise ValueError("simulation reconciliation evidence is missing")
        selected = load_latest_reconciliation_checkpoint(
            store, reconciliation_id="canonical-simulation-fill" if is_fill else "canonical-simulation-admission",
            provider_id=PROVIDER, account_id=ACCOUNT, environment=ENVIRONMENT,
        )
        if selected != reconciliation:
            raise ValueError("simulation reconciliation is not the canonical checkpoint")
        evidence = reconciliation["payload"]
        if (type(evidence) is not dict or type(evidence.get("provider_cash")) is not dict
                or type(evidence.get("provider_positions")) is not dict):
            raise ValueError("simulation reconciliation payload is malformed")
        _require_complete_reconciliation(reconciliation, cash=cash, position=position, fill_id=fill_id)
        if is_fill:
            if (result["decision"] != "BUY" or position <= 0
                    or type(fill_id) is not str or not fill_id
                    or type(result.get("order_id")) is not str or not result["order_id"]
                    or book.transactions[1].cause_event_id != fill_id
                    or book.transactions[1].transaction_id != _uuid("fill-transaction", episode_id)
                    or book.transactions[1].reverses_transaction_id is not None
                    or book.transactions[1].corrects_transaction_id is not None
                    or reconciliation["journal_sequence"] <= max(event["journal_sequence"] for event in events
                        if event["aggregate_type"] == "economic_book")):
                raise ValueError("completed simulation fill is not durably evidenced")
            _require_completed_send(events, episode_id=episode_id, order_id=result["order_id"])
            admission = _require_recorded_admission(events, episode_id=episode_id, outcome="ADMITTED")
            prepared = next(event for event in events if event["event_type"] == "SubmissionPrepared")
            if (prepared["payload"].get("intent_hash") != admission["payload"].get("intent_hash")
                    or prepared["journal_sequence"] <= admission["journal_sequence"]):
                raise ValueError("simulation send does not belong to the admitted intent")
            if (len(active) != 1 or active[0].reservation_id != _uuid("reservation", episode_id)
                    or active[0].intent_id != _uuid("intent", episode_id)
                    or active[0].state != "WORKING" or active[0].remaining != {"CASH:USD": Decimal("0")}):
                raise ValueError("completed simulation reservation differs")
            status["fills"] = {fill_id: {"instrument": INSTRUMENT,
                                        "quantity": canonical_decimal_text(position)}}
        elif (fill_id is not None or result.get("order_id") is not None or position != 0 or active
              or any(event["aggregate_type"] == "submission_attempt" for event in events)
              or (session_status == "HOLD" and result["decision"] != "HOLD")
              or (session_status == "RISK_REJECTED" and result["decision"] != "BUY")):
            raise ValueError("non-fill simulation contains unexpected exposure")
        elif session_status == "RISK_REJECTED":
            _require_recorded_admission(events, episode_id=episode_id, outcome="REJECTED")
        elif any(event["event_type"] in {"AuthorityAdmissionRecorded", "RiskDecisionRecorded"}
                 for event in events):
            raise ValueError("completed HOLD contains an unexpected financial admission")
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
