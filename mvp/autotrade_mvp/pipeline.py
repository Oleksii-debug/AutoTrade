"""Deterministic, network-free AutoTrade MVP vertical slice.

This module deliberately cannot place a live order.  It proves the complete
market -> decision -> risk -> durable intent -> simulated fill -> ledger ->
reconciliation -> portfolio -> restart -> evidence path with standard-library
types so it can run on a clean Windows machine.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal
from fractions import Fraction
from hashlib import sha256
import json
import os
from pathlib import Path
from typing import Iterable
from uuid import NAMESPACE_URL, uuid5

from .exact_decimal import (
    as_fraction, bounded_fraction, canonical_decimal_text,
    parse_bounded_exact_decimal, round_fraction_to_quantum, terminating_decimal,
)
from .persistence import JournalStore, payload_digest


MONEY_QUANTUM = Decimal("0.00000001")
CHECKPOINT_SCHEMA_VERSION = 2
CHECKPOINT_CONFIGURATION_VERSION = 1
SIMULATION_STRATEGY_ID = "moving_average_v1"
SIMULATION_STRATEGY_FAST = 2
SIMULATION_STRATEGY_SLOW = 3


def _exact_decimal(value: Decimal | str | int, *, name: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError(f"{name} must use Decimal, string or integer input")
    return parse_bounded_exact_decimal(value)


def _money_fraction(value: Fraction) -> Decimal:
    return round_fraction_to_quantum(
        bounded_fraction(value), MONEY_QUANTUM, mode="HALF_EVEN",
    )


def _money(value: Decimal | str | int) -> Decimal:
    return _money_fraction(as_fraction(_exact_decimal(value, name="money")))


def _bounded_product(*values: Decimal) -> Fraction:
    result = Fraction(1, 1)
    for value in values:
        result = bounded_fraction(result * as_fraction(value))
    return result


def _bounded_sum(values: Iterable[Decimal]) -> Fraction:
    result = Fraction(0, 1)
    for value in values:
        result = bounded_fraction(result + as_fraction(value))
    return result


def handle_market_data(prices: Iterable[float | int | str | Decimal]) -> list[Decimal]:
    normalized: list[Decimal] = []
    for value in prices:
        try:
            # Preserve the legacy float-facing simulation seam, but perform a
            # bounded textual admission before Decimal construction.  Every
            # other scalar goes through the shared exact authority directly.
            numeric = (
                parse_bounded_exact_decimal(repr(value))
                if type(value) is float
                else _exact_decimal(value, name="price")
            )
        except (TypeError, ValueError, ArithmeticError) as error:
            raise ValueError("Prices must be finite and positive") from error
        if numeric <= 0:
            raise ValueError("Prices must be finite and positive")
        normalized_price = _money(numeric)
        if normalized_price <= 0:
            raise ValueError("Price is smaller than supported precision")
        normalized.append(normalized_price)
    if not normalized:
        raise ValueError("At least one price is required")
    return normalized


def handle_strategy(prices: list[Decimal], quantity: Decimal) -> Decision:
    return MovingAverageStrategy(
        SIMULATION_STRATEGY_FAST, SIMULATION_STRATEGY_SLOW
    ).decide(prices, quantity)


def handle_risk(decision: Decision, current_position: Decimal, current_cash: Decimal, fee_rate: Decimal, max_abs_position: Decimal, max_notional: Decimal) -> tuple[bool, str]:
    return RiskGate(max_abs_position, max_notional).admit(decision, current_position, current_cash, fee_rate)


def handle_durable_order_intent(intent: OrderIntent, root: Path) -> None:
    _persist_intent(root / "order-intents" / f"{intent.client_order_id}.json", intent)


def handle_simulated_provider(intent: OrderIntent, fee_rate: Decimal, provider: SimulatedProvider) -> Fill:
    return provider.execute(intent, fee_rate)


def handle_economic_ledger(fill: Fill, ledger: EconomicLedger) -> bool:
    return ledger.apply_fill(fill)


def handle_reconciliation(provider: SimulatedProvider, ledger: EconomicLedger) -> bool:
    return _reconcile(provider, ledger)


def handle_portfolio(ledger: EconomicLedger, last_price: Decimal) -> Decimal:
    equity = bounded_fraction(
        as_fraction(ledger.cash) + _bounded_product(ledger.position, last_price)
    )
    return _money_fraction(equity)


def handle_restart_recovery(state_dir: str | Path, initial_cash: Decimal) -> tuple[dict, bool]:
    root = Path(state_dir)
    checkpoint_path = root / "checkpoint.json"
    return _read_state(checkpoint_path, initial_cash)


def handle_learning_evidence(evidence: dict, evidence_path: Path, evidence_ids: set, evidence_records: dict) -> bool:
    return _append_evidence(evidence_path, evidence)


def _utc_z(value: str) -> str:
    if value.endswith("Z"):
        return value
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("Timestamp must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _event_uuid(kind: str, key: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"https://events.autotrade.local/{kind}/{key}"))


def handle_journal_event(root: Path, symbol: str, evidence: dict) -> None:
    store = JournalStore(root / "journal.sqlite3")
    event_id = _event_uuid("simulation-episode", evidence["evidence_id"])
    existing = store.get_event(event_id)
    aggregate_version = (
        existing["aggregate_version"]
        if existing is not None
        else store.next_aggregate_version("simulation_portfolio", symbol)
    )
    timestamp = _utc_z(evidence["recorded_at"])
    payload = {
        "evidence_id": evidence["evidence_id"],
        "input_hash": evidence["input_hash"],
        "decision": evidence["decision"],
        "decision_reason": evidence["decision_reason"],
        "risk_outcome": evidence["risk_outcome"],
        "order_id": evidence["order_id"],
        "fill_id": evidence["fill_id"],
        "cash": evidence["cash"],
        "position": evidence["position"],
        "equity": evidence["equity"],
        "reconciled": evidence["reconciled"],
    }
    envelope = {
        "event_id": event_id,
        "event_type": "SimulationEpisodeRecorded",
        "schema_version": "1.0.0",
        "aggregate_type": "simulation_portfolio",
        "aggregate_id": symbol,
        "aggregate_version": str(aggregate_version),
        "host_id": "local-mvp",
        "owner_epoch": "1",
        "environment": "SIMULATION",
        "occurred_at": timestamp,
        "observed_at": timestamp,
        "committed_at": timestamp,
        "correlation_id": _event_uuid("correlation", evidence["evidence_id"]),
        "causation_id": None,
        "payload": payload,
        "payload_hash": payload_digest(payload),
        "evidence_refs": [],
    }
    store.append_event(envelope, outbox_topic="autotrade.simulation.events")


def _stable_hash(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return sha256(encoded).hexdigest()


def _checkpoint_configuration(
    *,
    symbol: str,
    initial_cash: Decimal,
    order_quantity: Decimal,
    max_abs_position: Decimal,
    max_notional: Decimal,
    fee_rate: Decimal,
) -> dict:
    return {
        "configuration_version": CHECKPOINT_CONFIGURATION_VERSION,
        "checkpoint_schema_version": CHECKPOINT_SCHEMA_VERSION,
        "symbol": symbol,
        "initial_cash": canonical_decimal_text(initial_cash),
        "order_quantity": canonical_decimal_text(order_quantity),
        "max_abs_position": canonical_decimal_text(max_abs_position),
        "max_notional": canonical_decimal_text(max_notional),
        "fee_rate": canonical_decimal_text(fee_rate),
        "strategy": {
            "id": SIMULATION_STRATEGY_ID,
            "fast": SIMULATION_STRATEGY_FAST,
            "slow": SIMULATION_STRATEGY_SLOW,
        },
    }


def _validate_checkpoint_configuration(
    state: dict,
    expected_configuration: dict,
    expected_digest: str,
) -> None:
    stored_configuration = state.get("checkpoint_configuration")
    stored_digest = state.get("checkpoint_configuration_digest")
    if type(stored_configuration) is not dict or type(stored_digest) is not str:
        raise ValueError("Checkpoint configuration identity is missing or corrupt")
    if _stable_hash(stored_configuration) != stored_digest:
        raise ValueError("Checkpoint configuration identity is corrupt")
    if stored_configuration != expected_configuration or stored_digest != expected_digest:
        raise ValueError("Checkpoint financial configuration is incompatible with this resume")


def _atomic_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


@dataclass(frozen=True)
class Decision:
    side: str
    quantity: Decimal
    price: Decimal
    reason: str


@dataclass(frozen=True)
class OrderIntent:
    client_order_id: str
    symbol: str
    side: str
    quantity: Decimal
    price: Decimal


@dataclass(frozen=True)
class Fill:
    fill_id: str
    client_order_id: str
    symbol: str
    side: str
    quantity: Decimal
    price: Decimal
    fee: Decimal


@dataclass(frozen=True)
class RunResult:
    status: str
    decision: str
    order_id: str | None
    fill_id: str | None
    cash: Decimal
    position: Decimal
    equity: Decimal
    reconciled: bool
    evidence_count: int
    resumed: bool


class MovingAverageStrategy:
    def __init__(self, fast: int = 2, slow: int = 3):
        if fast < 1 or slow <= fast:
            raise ValueError("Require 1 <= fast < slow")
        self.fast, self.slow = fast, slow

    def decide(self, prices: list[Decimal], quantity: Decimal) -> Decision:
        # This public seam is shared by the legacy vertical slice and canonical
        # simulation. Detach each semantically consumed scalar through the
        # shared exact authority before it can escape in a Decision. Validation
        # is limited to the bounded strategy window, not an irrelevant prefix.
        if not prices:
            raise ValueError("At least one price is required")
        admitted_quantity = terminating_decimal(as_fraction(quantity))
        if len(prices) < self.slow:
            admitted_price = terminating_decimal(as_fraction(prices[-1]))
            return Decision(
                "HOLD", Decimal("0"), admitted_price, "insufficient_history"
            )

        admitted_window = tuple(
            terminating_decimal(as_fraction(value))
            for value in prices[-self.slow :]
        )
        # Valid prices can have a sum wider than the Decimal input envelope.
        # The averages remain valid; retain shared bounded rational sums until
        # comparison instead of forcing the transient total back to Decimal.
        fast_total = Fraction(0)
        for value in admitted_window[-self.fast :]:
            fast_total = bounded_fraction(fast_total + as_fraction(value))
        slow_total = Fraction(0)
        for value in admitted_window:
            slow_total = bounded_fraction(slow_total + as_fraction(value))
        fast_scaled = bounded_fraction(fast_total * self.slow)
        slow_scaled = bounded_fraction(slow_total * self.fast)
        admitted_price = admitted_window[-1]
        if fast_scaled > slow_scaled:
            return Decision(
                "BUY", admitted_quantity, admitted_price, "fast_above_slow"
            )
        if fast_scaled < slow_scaled:
            return Decision(
                "SELL", admitted_quantity, admitted_price, "fast_below_slow"
            )
        return Decision("HOLD", Decimal("0"), admitted_price, "averages_equal")


class RiskGate:
    def __init__(self, max_abs_position: Decimal, max_notional: Decimal):
        self.max_abs_position = max_abs_position
        self.max_notional = max_notional

    def admit(self, decision: Decision, current_position: Decimal, current_cash: Decimal,
              fee_rate: Decimal) -> tuple[bool, str]:
        quantity = as_fraction(decision.quantity)
        signed = quantity if decision.side == "BUY" else -quantity
        next_position = bounded_fraction(as_fraction(current_position) + signed)
        if abs(next_position) > as_fraction(self.max_abs_position):
            return False, "max_position"
        notional = bounded_fraction(quantity * as_fraction(decision.price))
        if notional > as_fraction(self.max_notional):
            return False, "max_notional"
        if decision.side == "BUY":
            fee_multiplier = bounded_fraction(Fraction(1, 1) + as_fraction(fee_rate))
            cash_required = bounded_fraction(notional * fee_multiplier)
            if cash_required > as_fraction(current_cash):
                return False, "insufficient_cash"
        return True, "admitted"


class SimulatedProvider:
    def __init__(self, fills: dict[str, Fill] | None = None):
        self.fills = fills or {}

    def execute(self, intent: OrderIntent, fee_rate: Decimal) -> Fill:
        previous = self.fills.get(intent.client_order_id)
        if previous is not None:
            return previous
        fill_id = "fill-" + sha256(intent.client_order_id.encode("utf-8")).hexdigest()[:20]
        fee = _money_fraction(_bounded_product(intent.quantity, intent.price, fee_rate))
        fill = Fill(
            fill_id=fill_id,
            client_order_id=intent.client_order_id,
            symbol=intent.symbol,
            side=intent.side,
            quantity=intent.quantity,
            price=intent.price,
            fee=fee,
        )
        self.fills[intent.client_order_id] = fill
        return fill


class EconomicLedger:
    def __init__(self, initial_cash: Decimal, postings: list[dict] | None = None):
        self.initial_cash = initial_cash
        self.postings = postings or []

    def apply_fill(self, fill: Fill) -> bool:
        if any(row["fill_id"] == fill.fill_id for row in self.postings):
            return False
        signed_fraction = (
            as_fraction(fill.quantity)
            if fill.side == "BUY"
            else -as_fraction(fill.quantity)
        )
        signed_quantity = terminating_decimal(bounded_fraction(signed_fraction))
        cash_delta = bounded_fraction(
            -(signed_fraction * as_fraction(fill.price)) - as_fraction(fill.fee)
        )
        self.postings.append(
            {
                "fill_id": fill.fill_id,
                "cash_delta": str(_money_fraction(cash_delta)),
                "position_delta": str(signed_quantity),
                "fee": str(fill.fee),
            }
        )
        return True

    @property
    def cash(self) -> Decimal:
        deltas = (
            _exact_decimal(row["cash_delta"], name="checkpoint cash_delta")
            for row in self.postings
        )
        total = bounded_fraction(as_fraction(self.initial_cash) + _bounded_sum(deltas))
        return _money_fraction(total)

    @property
    def position(self) -> Decimal:
        deltas = (
            _exact_decimal(row["position_delta"], name="checkpoint position_delta")
            for row in self.postings
        )
        return terminating_decimal(_bounded_sum(deltas))


def _read_state(path: Path, initial_cash: Decimal) -> tuple[dict, bool]:
    if not path.exists():
        return {"initial_cash": str(initial_cash), "postings": [], "fills": {}, "evidence_ids": []}, False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("Corrupt checkpoint JSON") from error
    if type(data) is not dict:
        raise ValueError("Corrupt checkpoint structure")
    schema_version = data.get("schema_version")
    if type(schema_version) is int and schema_version == 1:
        raise ValueError("Legacy checkpoint requires explicit migration before resume")
    if type(schema_version) is not int or schema_version != CHECKPOINT_SCHEMA_VERSION:
        raise ValueError("Unsupported or corrupt checkpoint schema")
    if not isinstance(data.get("postings"), list) or not isinstance(data.get("fills"), dict):
        raise ValueError("Corrupt checkpoint ledger or fills")
    if not isinstance(data.get("evidence_ids"), list):
        raise ValueError("Corrupt checkpoint evidence IDs")
    if "evidence_records" in data and not isinstance(data["evidence_records"], dict):
        raise ValueError("Corrupt checkpoint evidence records")
    return data, True


def _find_evidence(path: Path, evidence_id: str) -> dict | None:
    if not path.exists():
        return None
    found = None
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            recorded = json.loads(line)
            if recorded["evidence_id"] == evidence_id:
                if found is not None:
                    raise ValueError("Duplicate learning evidence ID")
                found = recorded
    except (json.JSONDecodeError, KeyError) as error:
        raise ValueError("Corrupt learning evidence") from error
    return found


def _append_evidence(path: Path, evidence: dict) -> bool:
    evidence_id = evidence["evidence_id"]
    path.parent.mkdir(parents=True, exist_ok=True)
    recorded = _find_evidence(path, evidence_id)
    if recorded is not None:
        comparable = {key: value for key, value in recorded.items() if key != "recorded_at"}
        expected = {key: value for key, value in evidence.items() if key != "recorded_at"}
        if comparable != expected:
            raise ValueError("Learning evidence conflicts with checkpoint")
        return False
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(evidence, sort_keys=True, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return True


def _persist_intent(path: Path, intent: OrderIntent) -> None:
    payload = {**asdict(intent), "quantity": str(intent.quantity), "price": str(intent.price)}
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError("Corrupt durable order intent") from error
        if existing != payload:
            raise ValueError("Durable order intent conflicts with this decision")
        return
    _atomic_json(path, payload)


def _reconcile(provider: SimulatedProvider, ledger: EconomicLedger) -> bool:
    postings = {row["fill_id"]: row for row in ledger.postings}
    if len(postings) != len(ledger.postings) or len(postings) != len(provider.fills):
        raise ValueError("Fill and ledger counts do not reconcile")
    for fill in provider.fills.values():
        row = postings.get(fill.fill_id)
        signed_fraction = (
            as_fraction(fill.quantity)
            if fill.side == "BUY"
            else -as_fraction(fill.quantity)
        )
        signed = terminating_decimal(bounded_fraction(signed_fraction))
        expected_cash = _money_fraction(
            bounded_fraction(-(signed_fraction * as_fraction(fill.price)) - as_fraction(fill.fee))
        )
        if row is None:
            raise ValueError("Fill and economic ledger do not reconcile")
        row_position = _exact_decimal(row["position_delta"], name="checkpoint position_delta")
        row_cash = _exact_decimal(row["cash_delta"], name="checkpoint cash_delta")
        if row_position != signed or row_cash != expected_cash:
            raise ValueError("Fill and economic ledger do not reconcile")
    return True


def verify_replay(state_dir: str | Path) -> bool:
    """Verify that every saved decision has exactly one matching evidence row."""
    root = Path(state_dir)
    checkpoint_path = root / "checkpoint.json"
    evidence_path = root / "learning-evidence.jsonl"
    if not checkpoint_path.is_file() or not evidence_path.is_file():
        return False
    try:
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        records = checkpoint["evidence_records"]
        ids = checkpoint["evidence_ids"]
        rows = [json.loads(line) for line in evidence_path.read_text(encoding="utf-8").splitlines()]
        observed = {row["evidence_id"]: row for row in rows}
    except (OSError, ValueError, KeyError, TypeError):
        return False
    return (isinstance(records, dict) and isinstance(ids, list)
            and len(ids) == len(set(ids)) == len(rows) == len(observed)
            and set(ids) == set(records) == set(observed)
            and all(observed[key] == records[key] for key in ids))


def run_vertical_slice(
    prices: Iterable[float | int | str | Decimal],
    state_dir: str | Path,
    *,
    symbol: str = "SIM",
    initial_cash: Decimal | str = Decimal("10000"),
    order_quantity: Decimal | str = Decimal("1"),
    max_abs_position: Decimal | str = Decimal("10"),
    max_notional: Decimal | str = Decimal("5000"),
    fee_rate: Decimal | str = Decimal("0.001"),
) -> RunResult:
    """Run or resume one safe simulated end-to-end trading episode."""

    if not symbol or not symbol.strip():
        raise ValueError("A simulated symbol is required")
    starting_cash = _money(initial_cash)
    quantity = _money(order_quantity)
    position_limit = _money(max_abs_position)
    notional_limit = _money(max_notional)
    rate = _exact_decimal(fee_rate, name="fee_rate")
    if starting_cash <= 0 or quantity <= 0 or position_limit <= 0 or notional_limit <= 0:
        raise ValueError("Cash, order quantity and risk limits must be positive")
    if rate < 0 or rate >= 1:
        raise ValueError("Fee rate must be finite and between zero and one")
    checkpoint_configuration = _checkpoint_configuration(
        symbol=symbol,
        initial_cash=starting_cash,
        order_quantity=quantity,
        max_abs_position=position_limit,
        max_notional=notional_limit,
        fee_rate=rate,
    )
    checkpoint_configuration_digest = _stable_hash(checkpoint_configuration)
    root = Path(state_dir)
    checkpoint_path = root / "checkpoint.json"
    evidence_path = root / "learning-evidence.jsonl"
    state, resumed = handle_restart_recovery(state_dir, starting_cash)
    if resumed:
        _validate_checkpoint_configuration(
            state, checkpoint_configuration, checkpoint_configuration_digest
        )
        if state.get("symbol", symbol) != symbol:
            raise ValueError("Checkpoint belongs to another symbol")
    else:
        state = {
            "schema_version": CHECKPOINT_SCHEMA_VERSION,
            "checkpoint_configuration": checkpoint_configuration,
            "checkpoint_configuration_digest": checkpoint_configuration_digest,
            "symbol": symbol,
            "initial_cash": canonical_decimal_text(starting_cash),
            "postings": [],
            "fills": {},
            "evidence_ids": [],
            "evidence_records": {},
        }
        # Persist the exact financial/runtime configuration before any durable
        # order intent, fill, ledger, evidence or journal mutation can occur.
        _atomic_json(checkpoint_path, state)
    try:
        restored_initial_cash = _exact_decimal(
            state["initial_cash"], name="checkpoint initial_cash"
        )
        restored_fills = {
            key: Fill(
                fill_id=value["fill_id"],
                client_order_id=value["client_order_id"],
                symbol=value["symbol"],
                side=value["side"],
                quantity=_exact_decimal(value["quantity"], name="checkpoint fill quantity"),
                price=_exact_decimal(value["price"], name="checkpoint fill price"),
                fee=_exact_decimal(value["fee"], name="checkpoint fill fee"),
            )
            for key, value in state.get("fills", {}).items()
        }
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("Corrupt checkpoint financial scalar") from error
    if (
        resumed
        and canonical_decimal_text(restored_initial_cash)
        != checkpoint_configuration["initial_cash"]
    ):
        raise ValueError("Checkpoint initial cash conflicts with configuration identity")
    ledger = EconomicLedger(restored_initial_cash, list(state.get("postings", [])))
    provider = SimulatedProvider(restored_fills)
    _reconcile(provider, ledger)
    normalized = handle_market_data(prices)
    decision = handle_strategy(normalized, quantity)
    intent = None
    fill = None
    risk_reason = "hold"
    if decision.side != "HOLD":
        intent_payload = {
            "symbol": symbol,
            "side": decision.side,
            "quantity": str(decision.quantity),
            "price": str(decision.price),
            "input_hash": _stable_hash([str(item) for item in normalized]),
            "checkpoint_configuration_digest": checkpoint_configuration_digest,
        }
        intent = OrderIntent(
            client_order_id="intent-" + _stable_hash(intent_payload)[:20], symbol=symbol, side=decision.side,
            quantity=decision.quantity, price=decision.price,
        )
        if intent.client_order_id in provider.fills:
            admitted, risk_reason = True, "already_filled"
        else:
            admitted, risk_reason = handle_risk(decision, ledger.position, ledger.cash, rate, position_limit, notional_limit)
        if admitted:
            handle_durable_order_intent(intent, root)
            fill = handle_simulated_provider(intent, rate, provider)
            handle_economic_ledger(fill, ledger)
        else:
            intent = None

    last_price = normalized[-1]
    reconciled = handle_reconciliation(provider, ledger)
    equity = handle_portfolio(ledger, last_price)
    evidence_ids = set(state.get("evidence_ids", []))
    evidence_records = dict(state.get("evidence_records", {}))
    evidence_id = "evidence-" + _stable_hash({
        "symbol": symbol,
        "input": [str(x) for x in normalized],
        "intent": intent.client_order_id if intent else None,
        "checkpoint_configuration_digest": checkpoint_configuration_digest,
    })[:20]
    fresh_evidence = {
        "schema_version": 1,
        "evidence_id": evidence_id,
        "checkpoint_configuration_digest": checkpoint_configuration_digest,
        "input_hash": _stable_hash([str(item) for item in normalized]),
        "decision": decision.side,
        "decision_reason": decision.reason,
        "risk_outcome": risk_reason,
        "order_id": intent.client_order_id if intent else None,
        "fill_id": fill.fill_id if fill else None,
        "cash": str(ledger.cash),
        "position": str(ledger.position),
        "equity": str(equity),
        "reconciled": reconciled,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }
    recorded_evidence = _find_evidence(evidence_path, evidence_id)
    if evidence_id in evidence_records:
        evidence = evidence_records[evidence_id]
    elif recorded_evidence is not None:
        # Adopt an evidence row left durable by an older interrupted build.
        evidence = recorded_evidence
    else:
        evidence = fresh_evidence
    if (evidence.get("input_hash") != fresh_evidence["input_hash"]
            or evidence.get("checkpoint_configuration_digest")
            != checkpoint_configuration_digest
            or evidence.get("order_id") != fresh_evidence["order_id"]
            or evidence.get("fill_id") != fresh_evidence["fill_id"]):
        raise ValueError("Checkpoint evidence conflicts with this episode")
    evidence_ids.add(evidence["evidence_id"])
    evidence_records[evidence_id] = evidence
    checkpoint = {
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "checkpoint_configuration": checkpoint_configuration,
        "checkpoint_configuration_digest": checkpoint_configuration_digest,
        "symbol": symbol,
        "initial_cash": canonical_decimal_text(ledger.initial_cash),
        "postings": ledger.postings,
        "fills": {key: {**asdict(value), "quantity": str(value.quantity), "price": str(value.price), "fee": str(value.fee)} for key, value in provider.fills.items()},
        "evidence_ids": sorted(evidence_ids),
        "evidence_records": evidence_records,
    }
    _atomic_json(checkpoint_path, checkpoint)
    # The checkpoint is committed before the append-only evidence row. If the
    # process stops here, replay repairs the missing row on the next run.
    handle_learning_evidence(evidence, evidence_path, evidence_ids, evidence_records)
    # Journal/outbox comes after checkpoint and append-only evidence. An
    # interrupted write is repaired by deterministic replay on the next run.
    handle_journal_event(root, symbol, evidence)
    if not verify_replay(root):
        raise ValueError("Learning evidence does not replay against checkpoint")
    return RunResult(
        status="filled" if fill else ("risk_rejected" if decision.side != "HOLD" else "hold"),
        decision=decision.side,
        order_id=intent.client_order_id if intent else None,
        fill_id=fill.fill_id if fill else None,
        cash=ledger.cash,
        position=ledger.position,
        equity=equity,
        reconciled=reconciled,
        evidence_count=len(evidence_ids),
        resumed=resumed,
    )


def run_multi_episode(
    episodes: Iterable[Iterable[float | int | str | Decimal]],
    state_dir: str | Path,
    **kwargs,
) -> list[RunResult]:
    """Run a deterministic sequence through the same durable portfolio."""
    return [run_vertical_slice(episode, state_dir, **kwargs) for episode in episodes]