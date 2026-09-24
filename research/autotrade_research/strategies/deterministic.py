"""Deterministic zero-model proposal path for research and simulation.

The baseline is intentionally simple and makes no profitability claim. It only
consumes observations evidenced as available by the decision cutoff.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
from typing import Iterable


def _decimal(value, *, name: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError(f"{name} must use Decimal, string or integer input")
    try:
        result = value if isinstance(value, Decimal) else Decimal(value)
    except (InvalidOperation, ValueError, TypeError) as error:
        raise ValueError(f"{name} must be a finite decimal") from error
    if not result.is_finite():
        raise ValueError(f"{name} must be a finite decimal")
    return result


def _time(value: datetime, *, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _text(value: str, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    return value.strip()


@dataclass(frozen=True)
class CausalObservation:
    event_id: str
    symbol: str
    available_at: datetime
    price: Decimal

    @classmethod
    def create(cls, *, event_id: str, symbol: str, available_at: datetime, price) -> "CausalObservation":
        value = _decimal(price, name="price")
        if value <= 0:
            raise ValueError("price must be positive")
        return cls(
            event_id=_text(event_id, name="event_id"),
            symbol=_text(symbol, name="symbol"),
            available_at=_time(available_at, name="available_at"),
            price=value,
        )


@dataclass(frozen=True)
class DeterministicProposal:
    symbol: str
    action: str
    quantity: Decimal
    decision_time: datetime
    evidence_event_ids: tuple[str, ...]
    model_calls: int
    economic_edge_claim: str
    reason: str


class ReturnThresholdBaseline:
    """A bounded research baseline, not a qualified trading strategy."""

    def __init__(self, *, lookback: int, threshold, proposal_quantity):
        if not isinstance(lookback, int) or isinstance(lookback, bool) or lookback < 2:
            raise ValueError("lookback must be an integer >= 2")
        self.lookback = lookback
        self.threshold = _decimal(threshold, name="threshold")
        if self.threshold < 0:
            raise ValueError("threshold must be non-negative")
        self.proposal_quantity = _decimal(proposal_quantity, name="proposal_quantity")
        if self.proposal_quantity <= 0:
            raise ValueError("proposal_quantity must be positive")
        self._history: dict[str, list[CausalObservation]] = {}
        self._seen_events: dict[str, CausalObservation] = {}

    def ingest(self, observation: CausalObservation, *, simulation_time: datetime) -> bool:
        cutoff = _time(simulation_time, name="simulation_time")
        if observation.available_at > cutoff:
            raise ValueError("observation is not causally available at simulation_time")
        previous = self._seen_events.get(observation.event_id)
        if previous is not None:
            if previous != observation:
                raise ValueError("event_id conflicts with previously ingested observation")
            return False
        history = self._history.setdefault(observation.symbol, [])
        if history and observation.available_at < history[-1].available_at:
            raise ValueError("observations must be ingested in non-decreasing availability order")
        history.append(observation)
        if len(history) > self.lookback:
            del history[:-self.lookback]
        self._seen_events[observation.event_id] = observation
        return True

    def propose(self, *, symbol: str, decision_time: datetime) -> DeterministicProposal:
        name = _text(symbol, name="symbol")
        cutoff = _time(decision_time, name="decision_time")
        history = self._history.get(name, [])
        eligible = [item for item in history if item.available_at <= cutoff]
        if len(eligible) < self.lookback:
            return DeterministicProposal(
                symbol=name,
                action="HOLD",
                quantity=Decimal("0"),
                decision_time=cutoff,
                evidence_event_ids=tuple(item.event_id for item in eligible),
                model_calls=0,
                economic_edge_claim="UNPROVEN",
                reason="insufficient causal history",
            )
        window = eligible[-self.lookback:]
        first = window[0].price
        last = window[-1].price
        change = (last / first) - Decimal("1")
        if change > self.threshold:
            action = "BUY"
            quantity = self.proposal_quantity
            reason = "registered deterministic return threshold exceeded"
        elif change < -self.threshold:
            action = "SELL"
            quantity = self.proposal_quantity
            reason = "registered deterministic negative return threshold exceeded"
        else:
            action = "HOLD"
            quantity = Decimal("0")
            reason = "registered deterministic threshold not exceeded"
        return DeterministicProposal(
            symbol=name,
            action=action,
            quantity=quantity,
            decision_time=cutoff,
            evidence_event_ids=tuple(item.event_id for item in window),
            model_calls=0,
            economic_edge_claim="UNPROVEN",
            reason=reason,
        )

    def snapshot(self) -> str:
        payload = {
            "schema_version": 2,
            "lookback": self.lookback,
            "threshold": str(self.threshold),
            "proposal_quantity": str(self.proposal_quantity),
            "seen_events": {
                event_id: {
                    "symbol": item.symbol,
                    "available_at": item.available_at.isoformat(),
                    "price": str(item.price),
                }
                for event_id, item in sorted(self._seen_events.items())
            },
            "history": {
                symbol: [
                    {
                        "event_id": item.event_id,
                        "available_at": item.available_at.isoformat(),
                        "price": str(item.price),
                    }
                    for item in rows
                ]
                for symbol, rows in sorted(self._history.items())
            },
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    @classmethod
    def restore(cls, snapshot: str) -> "ReturnThresholdBaseline":
        try:
            payload = json.loads(snapshot)
        except (TypeError, json.JSONDecodeError) as error:
            raise ValueError("strategy snapshot is invalid") from error
        if not isinstance(payload, dict) or payload.get("schema_version") not in {1, 2}:
            raise ValueError("unsupported strategy snapshot")
        version = payload["schema_version"]
        expected = {"schema_version", "lookback", "threshold", "proposal_quantity", "history"}
        if version == 2:
            expected.add("seen_events")
        if set(payload) != expected or not isinstance(payload["history"], dict):
            raise ValueError("strategy snapshot structure is invalid")
        if version == 2 and not isinstance(payload["seen_events"], dict):
            raise ValueError("strategy seen-event snapshot is invalid")
        strategy = cls(
            lookback=payload["lookback"],
            threshold=payload["threshold"],
            proposal_quantity=payload["proposal_quantity"],
        )
        for symbol, rows in sorted(payload["history"].items()):
            if not isinstance(rows, list):
                raise ValueError("strategy history is invalid")
            for row in rows:
                if not isinstance(row, dict) or set(row) != {"event_id", "available_at", "price"}:
                    raise ValueError("strategy observation snapshot is invalid")
                try:
                    available_at = datetime.fromisoformat(row["available_at"])
                except (TypeError, ValueError) as error:
                    raise ValueError("snapshot timestamp is invalid") from error
                observation = CausalObservation.create(
                    event_id=row["event_id"],
                    symbol=symbol,
                    available_at=available_at,
                    price=row["price"],
                )
                strategy.ingest(observation, simulation_time=observation.available_at)

        if version == 2:
            declared_seen: set[str] = set()
            for event_id, row in sorted(payload["seen_events"].items()):
                if not isinstance(row, dict) or set(row) != {"symbol", "available_at", "price"}:
                    raise ValueError("strategy seen-event snapshot is invalid")
                try:
                    available_at = datetime.fromisoformat(row["available_at"])
                except (TypeError, ValueError) as error:
                    raise ValueError("snapshot timestamp is invalid") from error
                observation = CausalObservation.create(
                    event_id=event_id,
                    symbol=row["symbol"],
                    available_at=available_at,
                    price=row["price"],
                )
                existing = strategy._seen_events.get(event_id)
                if existing is not None and existing != observation:
                    raise ValueError("seen-event snapshot conflicts with retained history")
                strategy._seen_events[event_id] = observation
                declared_seen.add(event_id)
            retained_ids = {
                item.event_id
                for rows in strategy._history.values()
                for item in rows
            }
            if not retained_ids.issubset(declared_seen):
                raise ValueError("seen-event snapshot is missing retained history")
        return strategy


def run_baseline(
    strategy: ReturnThresholdBaseline,
    observations: Iterable[CausalObservation],
    *,
    decision_time: datetime,
    symbol: str,
) -> DeterministicProposal:
    cutoff = _time(decision_time, name="decision_time")
    for observation in observations:
        strategy.ingest(observation, simulation_time=cutoff)
    return strategy.propose(symbol=symbol, decision_time=cutoff)
