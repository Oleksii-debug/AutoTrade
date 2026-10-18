"""Causal Plan-10 adapter for the canonical quantitative risk context.

The adapter owns no financial state and issues no orders.  It reconstructs the
current historical risk cut from completed canonical simulation episodes, then
uses :func:`mvp.autotrade_mvp.risk.evaluate_risk` before the next historical
episode may add exposure.  Consequently restart state comes from the durable
journal and no future market observation is needed to rebuild the cut.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Mapping, Sequence

from mvp.autotrade_mvp.exact_decimal import (
    as_fraction,
    canonical_decimal_text,
    exact_add,
    exact_multiply,
    exact_subtract,
    parse_bounded_exact_decimal,
    round_fraction_to_quantum,
)
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.risk import (
    RiskContext,
    RiskDecision,
    RiskIntent,
    RiskPolicy,
    evaluate_risk,
)
from mvp.autotrade_mvp.risk_policy_authority import (
    DurableRiskPolicyRegistry,
    RiskPolicyScope,
)
from mvp.autotrade_mvp.simulation_session import (
    ACCOUNT,
    ENVIRONMENT,
    INSTRUMENT,
    PROVIDER,
)


_LOOP_AGGREGATE = "canonical_autonomous_simulation"
_DRAWDOWN_QUANTUM = Decimal("0.000000000000000001")


def _utc(value: datetime, *, name: str) -> datetime:
    if type(value) is not datetime or value.tzinfo is None:
        raise TypeError(f"{name} must be an exact timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _dec(value, *, name: str) -> Decimal:
    if isinstance(value, (bool, float)):
        raise TypeError(f"{name} must use exact Decimal input")
    try:
        return parse_bounded_exact_decimal(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a bounded exact Decimal") from exc


@dataclass(frozen=True)
class HistoricalRiskCut:
    """The exact, causally derived pre-decision cut for one episode."""

    episode: int
    observed_at: datetime
    cash: Decimal
    position: Decimal
    equity: Decimal
    daily_start_equity: Decimal
    peak_equity: Decimal
    context: RiskContext


def causal_historical_risk_cut(
    *,
    completed: Sequence[Mapping[str, object]],
    execution_prices: Sequence[object],
    execution_times: Sequence[datetime],
    current_episode: int,
    initial_cash,
) -> HistoricalRiskCut:
    """Derive one risk cut from the completed prefix and current observation.

    ``current_episode`` is one-based.  A caller cannot skip or replay an
    episode, and only prices/times through that episode are inspected.
    Completed equity is independently recomputed against the corresponding
    historical mark so a corrupt restart image fails closed.
    """

    if type(current_episode) is not int or current_episode != len(completed) + 1:
        raise ValueError("historical risk cut must immediately follow the completed prefix")
    if current_episode < 1 or current_episode > len(execution_prices):
        raise ValueError("historical risk episode is outside the execution stream")
    if len(execution_times) != len(execution_prices):
        raise ValueError("historical execution times and prices must have equal length")

    starting = _dec(initial_cash, name="initial_cash")
    if starting <= 0:
        raise ValueError("initial_cash must be positive")
    current_time = _utc(execution_times[current_episode - 1], name="execution time")
    current_price = _dec(execution_prices[current_episode - 1], name="execution price")
    if current_price <= 0:
        raise ValueError("execution price must be positive")

    prior_equities: list[Decimal] = []
    prior_times: list[datetime] = []
    cash = starting
    position = Decimal("0")
    for index, item in enumerate(completed):
        if type(item) is not dict:
            raise TypeError("completed historical decisions must be exact mappings")
        episode = index + 1
        if item.get("episode") != episode or item.get("reconciled") is not True:
            raise ValueError("completed historical decision prefix is incomplete")
        point = _utc(execution_times[index], name="completed execution time")
        if index and point <= prior_times[-1]:
            raise ValueError("completed historical execution time is not increasing")
        if point >= current_time:
            raise ValueError("completed historical decision is not before the current cut")
        mark = _dec(execution_prices[index], name="completed execution price")
        cash = _dec(item.get("cash"), name="completed cash")
        position = _dec(item.get("position"), name="completed position")
        reported_equity = _dec(item.get("equity"), name="completed equity")
        recomputed = exact_add(cash, exact_multiply(position, mark))
        if reported_equity != recomputed:
            raise ValueError("completed historical equity conflicts with cash/position/mark")
        prior_times.append(point)
        prior_equities.append(reported_equity)

    equity = exact_add(cash, exact_multiply(position, current_price))
    peak = max((starting, equity, *prior_equities))
    drawdown = round_fraction_to_quantum(
        as_fraction(exact_subtract(peak, equity)) / as_fraction(peak),
        _DRAWDOWN_QUANTUM,
        mode="CEILING",
    )
    daily_start = starting
    for point, prior_equity in zip(prior_times, prior_equities):
        if point.date() < current_time.date():
            daily_start = prior_equity
        else:
            break
    daily_pnl = exact_subtract(equity, daily_start)
    context = RiskContext.create(
        state_version=current_episode,
        equity=equity,
        positions={INSTRUMENT: position},
        marks={INSTRUMENT: current_price},
        reserved_position_delta={},
        daily_pnl=daily_pnl,
        drawdown_fraction=drawdown,
        market_data_age_seconds="0",
        fx_age_seconds={"USD": "0"},
        margin_headroom="1",
        capability_allowed=True,
        borrow_available=True,
        stress_scenarios=({INSTRUMENT: "-0.25"},),
        instrument_types={INSTRUMENT: "EQUITY"},
    )
    return HistoricalRiskCut(
        episode=current_episode,
        observed_at=current_time,
        cash=cash,
        position=position,
        equity=equity,
        daily_start_equity=daily_start,
        peak_equity=peak,
        context=context,
    )


def evaluate_historical_risk(
    cut: HistoricalRiskCut,
    policy: RiskPolicy,
    *,
    side: str,
    quantity,
    execution_price,
) -> RiskDecision:
    """Evaluate a historical order through the canonical risk engine."""

    if type(cut) is not HistoricalRiskCut:
        raise TypeError("cut must be an exact HistoricalRiskCut")
    amount = _dec(quantity, name="risk quantity")
    price = _dec(execution_price, name="risk execution price")
    if amount <= 0 or price <= 0:
        raise ValueError("historical risk order quantity and price must be positive")
    normalized_side = side.upper() if type(side) is str else side
    return evaluate_risk(
        RiskIntent.create(
            symbol=INSTRUMENT,
            side=normalized_side,
            quantity=amount,
            price=price,
            expected_state_version=cut.episode,
            reduce_only=normalized_side == "SELL",
            action="REDUCE" if normalized_side == "SELL" else "TRADE",
            instrument_type="EQUITY",
        ),
        cut.context,
        policy,
    )


class HistoricalRiskGate:
    """``should_pause`` adapter for the historical-only ZERO entrypoint.

    The canonical loop invokes this before exposing each next observation to
    financial admission.  Any malformed/tampered state raises and therefore
    fails closed; a canonical risk rejection pauses before an order is built.
    """

    def __init__(
        self,
        *,
        state_dir: str | Path,
        run_id: str,
        execution_prices: Sequence[object],
        signal_prices: Sequence[object],
        execution_times: Sequence[datetime],
        initial_cash,
        target_quantity,
    ) -> None:
        self._root = Path(state_dir)
        self._run_id = run_id
        self._prices = tuple(execution_prices)
        self._signals = tuple(signal_prices)
        self._times = tuple(execution_times)
        self._initial_cash = _dec(initial_cash, name="initial_cash")
        self._target = _dec(target_quantity, name="target_quantity")
        self._store: JournalStore | None = None
        self._registry: DurableRiskPolicyRegistry | None = None
        self.last_cut: HistoricalRiskCut | None = None
        self.last_decision: RiskDecision | None = None
        self.blocked_rules: tuple[str, ...] = ()

    def _completed(self, store: JournalStore) -> list[dict]:
        events = store.load_events(_LOOP_AGGREGATE, self._run_id)
        if not events or events[0].get("event_type") != "AutonomousSimulationStarted":
            raise ValueError("historical risk gate lacks canonical simulation start")
        protocol = events[0]["payload"].get("protocol")
        if type(protocol) is not dict:
            raise ValueError("historical risk gate protocol is malformed")
        if tuple(protocol.get("prices", ())) != tuple(
            canonical_decimal_text(_dec(v, name="execution price")) for v in self._prices
        ):
            raise ValueError("historical risk execution stream differs from durable protocol")
        durable_signals = tuple(protocol.get("signal_prices", protocol.get("prices", ())))
        if durable_signals != tuple(
            canonical_decimal_text(_dec(v, name="signal price")) for v in self._signals
        ):
            raise ValueError("historical risk signal stream differs from durable protocol")
        completed = [event["payload"] for event in events
                     if event["event_type"] == "AutonomousEpisodeCompleted"]
        return completed

    def __call__(self) -> bool:
        if self._store is None:
            self._store = JournalStore(self._root / "journal.sqlite3")
            self._registry = DurableRiskPolicyRegistry(self._store)
        store = self._store
        completed = self._completed(store)
        episode = len(completed) + 1
        if episode > len(self._prices):
            return False
        cut = causal_historical_risk_cut(
            completed=completed,
            execution_prices=self._prices,
            execution_times=self._times,
            current_episode=episode,
            initial_cash=self._initial_cash,
        )
        self.last_cut = cut
        scope = RiskPolicyScope(
            PROVIDER, ACCOUNT, ENVIRONMENT, ENVIRONMENT,
            "internal-simulator-v1", "CASH_EQUITY",
        )
        if self._registry is None:  # defensive type narrowing; initialized above
            raise RuntimeError("historical risk registry is unavailable")
        policy = self._registry.resolve_current(scope).policy
        decision = evaluate_historical_risk(
            cut,
            policy,
            # This is a no-send canary for an exposure-increasing action.  The
            # real strategy/order remains solely inside the canonical loop.
            # Historical loss/drawdown STOP is deliberately conservative and
            # pauses before either a new order or a future observation.
            side="BUY",
            quantity=self._target,
            execution_price=self._prices[episode - 1],
        )
        self.last_decision = decision
        self.blocked_rules = tuple(
            rule.rule for rule in decision.rules
            if not rule.passed and rule.rule in {"daily_loss", "drawdown"}
        )
        return bool(self.blocked_rules)

    def _restore_last_completed_predecision(self) -> None:
        """Rebuild the last evaluated cut for a no-op bounded restart report."""
        if self.last_cut is not None:
            return
        journal = self._root / "journal.sqlite3"
        if journal.is_symlink() or not journal.is_file():
            return
        if self._store is None:
            self._store = JournalStore(journal)
            self._registry = DurableRiskPolicyRegistry(self._store)
        completed = self._completed(self._store)
        if not completed:
            return
        episode = len(completed)
        cut = causal_historical_risk_cut(
            completed=completed[:-1],
            execution_prices=self._prices,
            execution_times=self._times,
            current_episode=episode,
            initial_cash=self._initial_cash,
        )
        scope = RiskPolicyScope(
            PROVIDER, ACCOUNT, ENVIRONMENT, ENVIRONMENT,
            "internal-simulator-v1", "CASH_EQUITY",
        )
        if self._registry is None:  # defensive type narrowing
            raise RuntimeError("historical risk registry is unavailable")
        decision = evaluate_historical_risk(
            cut,
            self._registry.resolve_current(scope).policy,
            side="BUY",
            quantity=self._target,
            execution_price=self._prices[episode - 1],
        )
        self.last_cut = cut
        self.last_decision = decision
        self.blocked_rules = tuple(
            rule.rule for rule in decision.rules
            if not rule.passed and rule.rule in {"daily_loss", "drawdown"}
        )

    def report(self) -> dict[str, object]:
        self._restore_last_completed_predecision()
        cut = self.last_cut
        return {
            "status": "BLOCKED" if self.blocked_rules else "CLEAR",
            "blocked_rules": list(self.blocked_rules),
            "through_predecision_episode": None if cut is None else cut.episode,
            "daily_pnl_usd": (
                None if cut is None else canonical_decimal_text(cut.context.daily_pnl)
            ),
            "drawdown_fraction": (
                None if cut is None
                else canonical_decimal_text(cut.context.drawdown_fraction)
            ),
            "equity_usd": None if cut is None else canonical_decimal_text(cut.equity),
            "causal_state_source": "CANONICAL_COMPLETED_EPISODE_JOURNAL_PREFIX",
        }
