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
    ExactDecimalError,
    as_fraction, bounded_fraction, canonical_decimal_text, exact_abs, exact_add,
    exact_multiply, exact_subtract, exact_sum, parse_bounded_exact_decimal,
    round_fraction_to_quantum, terminating_decimal,
)
from .persistence import JournalStore, payload_digest


MONEY_QUANTUM = Decimal("0.00000001")
CHECKPOINT_SCHEMA_VERSION = 6


def _exact_decimal(value: Decimal | str | int, *, name: str) -> Decimal:
    if type(value) not in {Decimal, str, int}:
        raise TypeError(f"{name} must use an exact Decimal, string or integer")
    return parse_bounded_exact_decimal(value)


def _money(
    value: Decimal | str | int,
    *,
    _quantum: Decimal = MONEY_QUANTUM,
    _parse=parse_bounded_exact_decimal,
    _fraction=as_fraction,
    _round=round_fraction_to_quantum,
) -> Decimal:
    """Quantize money through process-frozen exact numeric dependencies."""

    if type(value) not in {Decimal, str, int}:
        raise TypeError("money must use an exact Decimal, string or integer")
    return _round(
        _fraction(_parse(value)),
        _quantum,
        mode="HALF_EVEN",
    )


def _checkpoint_decimal(value: object, *, name: str) -> Decimal:
    """Parse one exact decimal emitted by the durable checkpoint.

    Checkpoints are JSON and canonical writers emit these financial scalars as
    strings.  Reject alternate JSON scalar types and numerically unbounded text
    before any recovered value can participate in risk or ledger arithmetic.
    """

    if type(value) is not str:
        raise ValueError(f"Corrupt checkpoint {name}: expected decimal text")
    try:
        return parse_bounded_exact_decimal(value)
    except (ExactDecimalError, TypeError, ValueError) as error:
        raise ValueError(f"Corrupt checkpoint {name}: invalid exact decimal") from error


def _financial_configuration(
    *,
    symbol: str,
    initial_cash: Decimal,
    order_quantity: Decimal,
    max_abs_position: Decimal,
    max_notional: Decimal,
    fee_rate: Decimal,
) -> dict[str, object]:
    """Return the exact effective financial configuration for one durable run."""

    return {
        "symbol": symbol,
        "initial_cash": str(initial_cash),
        "order_quantity": str(order_quantity),
        "max_abs_position": str(max_abs_position),
        "max_notional": str(max_notional),
        "fee_rate": canonical_decimal_text(fee_rate),
        "checkpoint_schema_version": CHECKPOINT_SCHEMA_VERSION,
        "money_quantum": str(MONEY_QUANTUM),
        "strategy": {
            "kind": "MOVING_AVERAGE",
            "fast": 2,
            "slow": 3,
        },
    }


def _require_checkpoint_configuration(
    state: dict,
    expected: dict[str, object],
) -> None:
    configuration = state.get("financial_configuration")
    digest = state.get("financial_configuration_hash")
    if type(configuration) is not dict or type(digest) is not str:
        raise ValueError("Checkpoint lacks exact financial configuration identity")
    try:
        observed_digest = _stable_hash(configuration)
        expected_digest = _stable_hash(expected)
    except (TypeError, ValueError) as error:
        raise ValueError("Corrupt checkpoint financial configuration") from error
    if digest != observed_digest:
        raise ValueError("Checkpoint financial configuration digest mismatch")
    if configuration != expected or digest != expected_digest:
        raise ValueError("Checkpoint financial configuration changed")
    if state.get("schema_version") != configuration.get("checkpoint_schema_version"):
        raise ValueError("Checkpoint schema does not match financial configuration")
    if (
        type(state.get("symbol")) is not str
        or state["symbol"] != configuration.get("symbol")
    ):
        raise ValueError("Checkpoint symbol does not match financial configuration")
    if (
        type(state.get("initial_cash")) is not str
        or state["initial_cash"] != configuration.get("initial_cash")
    ):
        raise ValueError("Checkpoint initial cash does not match financial configuration")


def handle_market_data(prices: Iterable[float | str | Decimal]) -> list[Decimal]:
    normalized: list[Decimal] = []
    for value in prices:
        if type(value) is float:
            presentation: object = str(value)
        elif type(value) in (str, int, Decimal):
            presentation = value
        else:
            raise ValueError("Prices must be exact float, str, int or Decimal values")
        try:
            numeric = parse_bounded_exact_decimal(presentation)
        except (ValueError, ArithmeticError, TypeError) as error:
            raise ValueError("Prices must be finite and positive") from error
        if not numeric.is_finite() or numeric <= 0:
            raise ValueError("Prices must be finite and positive")
        normalized_price = _money(numeric)
        if normalized_price <= 0:
            raise ValueError("Price is smaller than supported precision")
        normalized.append(normalized_price)
    if not normalized:
        raise ValueError("At least one price is required")
    return normalized


def handle_strategy(prices: list[Decimal], quantity: Decimal) -> Decision:
    return MovingAverageStrategy().decide(prices, quantity)


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
    return _money(exact_add(ledger.cash, exact_multiply(ledger.position, last_price)))


def handle_restart_recovery(state_dir: str | Path, initial_cash: Decimal) -> tuple[dict, bool]:
    root = Path(state_dir)
    checkpoint_path = root / "checkpoint.json"
    return _read_state(checkpoint_path, initial_cash)


def handle_learning_evidence(evidence: dict, evidence_path: Path, evidence_ids: set, evidence_records: dict) -> bool:
    return _append_evidence(evidence_path, evidence)


def _utc_z(value: str) -> str:
    if type(value) is not str or not value:
        raise ValueError("Timestamp must be exact ISO-8601 text")
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as error:
        raise ValueError("Timestamp must be valid ISO-8601 text") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Timestamp must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _event_uuid(kind: str, key: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"https://events.autotrade.local/{kind}/{key}"))


def handle_journal_event(
    root: Path,
    symbol: str,
    evidence: dict,
    financial_configuration_hash: str,
) -> None:
    timestamp = _utc_z(evidence["recorded_at"])
    episode_sequence = evidence.get("episode_sequence")
    if type(episode_sequence) is not int or episode_sequence < 1:
        raise ValueError("Simulation episode sequence must be a positive exact integer")
    store = JournalStore(root / "journal.sqlite3")
    event_id = _event_uuid("simulation-episode", evidence["evidence_id"])
    existing = store.get_event(event_id)
    aggregate_version = (
        existing["aggregate_version"]
        if existing is not None
        else store.next_aggregate_version("simulation_portfolio", symbol)
    )
    if aggregate_version != episode_sequence:
        raise ValueError(
            "Simulation journal aggregate version conflicts with episode sequence"
        )
    payload = {
        "evidence_id": evidence["evidence_id"],
        "episode_sequence": episode_sequence,
        "input_hash": evidence["input_hash"],
        "decision": evidence["decision"],
        "decision_reason": evidence["decision_reason"],
        "risk_outcome": evidence["risk_outcome"],
        "order_id": evidence["order_id"],
        "fill_id": evidence["fill_id"],
        "cash": evidence["cash"],
        "position": evidence["position"],
        "equity": evidence["equity"],
        "valuation_price": evidence["valuation_price"],
        "reconciled": evidence["reconciled"],
        "financial_configuration_hash": financial_configuration_hash,
    }
    envelope = {
        "event_id": event_id,
        "event_type": "SimulationEpisodeRecorded",
        "schema_version": "2.0.0",
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


def _simulation_intent_id(
    *,
    symbol: str,
    side: str,
    quantity: Decimal,
    price: Decimal,
    input_hash: str,
    financial_configuration_hash: str,
) -> str:
    """Derive the durable simulated intent identity from exact causal inputs."""

    payload = {
        "symbol": symbol,
        "side": side,
        "quantity": str(quantity),
        "price": str(price),
        "input_hash": input_hash,
        "financial_configuration_hash": financial_configuration_hash,
    }
    return "intent-" + _stable_hash(payload)[:20]


def _best_effort_fsync_directory(directory: Path) -> None:
    """Best-effort metadata flush after rename without post-commit failure."""

    if os.name == "nt":
        return
    try:
        descriptor = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        try:
            os.fsync(descriptor)
        except OSError:
            pass
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass


def _atomic_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    serialized = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(serialized)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    _best_effort_fsync_directory(path.parent)


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
        signed = (
            decision.quantity
            if decision.side == "BUY"
            else exact_subtract(Decimal("0"), decision.quantity)
        )
        if exact_abs(exact_add(current_position, signed)) > self.max_abs_position:
            return False, "max_position"
        notional = exact_multiply(decision.quantity, decision.price)
        if notional > self.max_notional:
            return False, "max_notional"
        if decision.side == "BUY":
            cash_required = exact_multiply(
                notional,
                exact_add(Decimal("1"), fee_rate),
            )
            if cash_required > current_cash:
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
        fill = Fill(
            fill_id=fill_id,
            client_order_id=intent.client_order_id,
            symbol=intent.symbol,
            side=intent.side,
            quantity=intent.quantity,
            price=intent.price,
            fee=_money(exact_multiply(intent.quantity, intent.price, fee_rate)),
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
        signed_quantity = (
            fill.quantity
            if fill.side == "BUY"
            else exact_subtract(Decimal("0"), fill.quantity)
        )
        cash_delta = exact_subtract(
            exact_subtract(
                Decimal("0"),
                exact_multiply(signed_quantity, fill.price),
            ),
            fill.fee,
        )
        self.postings.append(
            {
                "fill_id": fill.fill_id,
                "cash_delta": str(_money(cash_delta)),
                "position_delta": str(signed_quantity),
                "fee": str(fill.fee),
            }
        )
        return True

    @property
    def cash(self) -> Decimal:
        return _money(
            exact_sum(
                (
                    _checkpoint_decimal(row["cash_delta"], name="cash_delta")
                    for row in self.postings
                ),
                start=self.initial_cash,
            )
        )

    @property
    def position(self) -> Decimal:
        return exact_sum(
            (
                _checkpoint_decimal(row["position_delta"], name="position_delta")
                for row in self.postings
            ),
            start=Decimal("0"),
        )


def _require_no_pending_atomic_writes(root: Path) -> None:
    """Fail closed when a prior atomic replacement stopped before commit."""

    checkpoint_temp = root / "checkpoint.json.tmp"
    intents = root / "order-intents"
    pending_intent = intents.is_dir() and any(intents.glob("*.json.tmp"))
    if checkpoint_temp.exists() or pending_intent:
        raise ValueError(
            "Interrupted durable atomic write requires explicit recovery"
        )


def _has_residual_durable_state(root: Path) -> bool:
    """Reject rebinding an existing run directory after its checkpoint is lost."""

    if (root / "learning-evidence.jsonl").exists():
        return True
    if any(root.glob("journal.sqlite3*")):
        return True
    if (root / "checkpoint.json.tmp").exists():
        return True
    intents = root / "order-intents"
    return intents.is_dir() and (
        any(intents.glob("*.json"))
        or any(intents.glob("*.json.tmp"))
    )


def _initial_checkpoint(financial_configuration: dict[str, object]) -> dict[str, object]:
    """Persist run economics before the first durable intent/provider mutation."""

    return {
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "symbol": financial_configuration["symbol"],
        "financial_configuration": financial_configuration,
        "financial_configuration_hash": _stable_hash(financial_configuration),
        "initial_cash": financial_configuration["initial_cash"],
        "postings": [],
        "fills": {},
        "evidence_ids": [],
        "evidence_records": {},
    }


def _strict_json_loads(text: str) -> object:
    """Decode durable JSON while rejecting ambiguous duplicate object keys."""

    if type(text) is not str:
        raise TypeError("durable JSON input must be exact text")

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result

    return json.loads(text, object_pairs_hook=unique_object)


def _read_state(path: Path, initial_cash: Decimal) -> tuple[dict, bool]:
    if not path.exists():
        return {"initial_cash": str(initial_cash), "postings": [], "fills": {}, "evidence_ids": []}, False
    try:
        data = _strict_json_loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as error:
        raise ValueError("Corrupt checkpoint JSON") from error
    if not isinstance(data, dict):
        raise ValueError("Corrupt checkpoint structure")
    schema_version = data.get("schema_version")
    if schema_version == 1:
        raise ValueError(
            "Legacy checkpoint schema 1 lacks exact financial configuration identity"
        )
    if schema_version != CHECKPOINT_SCHEMA_VERSION:
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
            recorded = _strict_json_loads(line)
            if type(recorded) is not dict or type(recorded.get("evidence_id")) is not str:
                raise ValueError("Corrupt learning evidence")
            if recorded["evidence_id"] == evidence_id:
                if found is not None:
                    raise ValueError("Duplicate learning evidence ID")
                found = recorded
    except (ValueError, KeyError, TypeError) as error:
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


_REPLAY_EVIDENCE_FIELDS = frozenset(
    {
        "schema_version",
        "evidence_id",
        "episode_sequence",
        "input_hash",
        "decision",
        "decision_reason",
        "risk_outcome",
        "order_id",
        "fill_id",
        "cash",
        "position",
        "equity",
        "valuation_price",
        "reconciled",
        "financial_configuration_hash",
        "recorded_at",
    }
)


def _replay_evidence_semantics_valid(record: dict) -> bool:
    """Require evidence labels to match the fixed deterministic strategy/risk path."""

    decision = record.get("decision")
    reason = record.get("decision_reason")
    risk_outcome = record.get("risk_outcome")
    has_order = record.get("order_id") is not None
    has_fill = record.get("fill_id") is not None
    if has_order != has_fill:
        return False
    if decision == "BUY":
        if reason != "fast_above_slow":
            return False
    elif decision == "SELL":
        if reason != "fast_below_slow":
            return False
    elif decision == "HOLD":
        if reason not in {"insufficient_history", "averages_equal"}:
            return False
    else:
        return False
    if has_order:
        return decision in {"BUY", "SELL"} and risk_outcome == "admitted"
    if decision == "HOLD":
        return risk_outcome == "hold"
    return risk_outcome in {"max_position", "max_notional", "insufficient_cash"}


def _require_replay_evidence_record(
    record: object,
    *,
    evidence_id: str,
    financial_configuration_hash: str,
) -> tuple[int, datetime]:
    """Authenticate one checkpoint evidence row before any replay repair write."""

    if (
        type(record) is not dict
        or set(record) != _REPLAY_EVIDENCE_FIELDS
        or record.get("schema_version") != 2
        or record.get("evidence_id") != evidence_id
        or type(record.get("episode_sequence")) is not int
        or record["episode_sequence"] < 1
        or type(record.get("input_hash")) is not str
        or len(record["input_hash"]) != 64
        or any(char not in "0123456789abcdef" for char in record["input_hash"])
        or record.get("decision") not in {"BUY", "SELL", "HOLD"}
        or type(record.get("decision_reason")) is not str
        or type(record.get("risk_outcome")) is not str
        or (
            record.get("order_id") is not None
            and type(record.get("order_id")) is not str
        )
        or (
            record.get("fill_id") is not None
            and type(record.get("fill_id")) is not str
        )
        or (record.get("order_id") is None) != (record.get("fill_id") is None)
        or type(record.get("cash")) is not str
        or type(record.get("position")) is not str
        or type(record.get("equity")) is not str
        or type(record.get("valuation_price")) is not str
        or record.get("reconciled") is not True
        or record.get("financial_configuration_hash")
        != financial_configuration_hash
    ):
        raise ValueError("Corrupt checkpoint replay evidence")
    if not _replay_evidence_semantics_valid(record):
        raise ValueError("Corrupt checkpoint replay evidence semantics")
    financial_values: dict[str, Decimal] = {}
    try:
        for field in ("cash", "position", "equity", "valuation_price"):
            value = _checkpoint_decimal(
                record[field],
                name=f"evidence {field}",
            )
            if str(value) != record[field]:
                raise ValueError("non-canonical evidence decimal")
            financial_values[field] = value
        timestamp = datetime.fromisoformat(
            _utc_z(record["recorded_at"]).replace("Z", "+00:00")
        )
    except (KeyError, TypeError, ValueError, ArithmeticError) as error:
        raise ValueError("Corrupt checkpoint replay evidence") from error
    valuation_price = financial_values["valuation_price"]
    if valuation_price <= 0:
        raise ValueError("Checkpoint replay evidence valuation price is invalid")
    expected_equity = _money(
        exact_add(
            financial_values["cash"],
            exact_multiply(financial_values["position"], valuation_price),
        )
    )
    if financial_values["equity"] != expected_equity:
        raise ValueError(
            "Checkpoint replay evidence equity does not match valuation"
        )
    return record["episode_sequence"], timestamp


def _require_replay_risk_outcome(
    record: dict,
    *,
    order_quantity: Decimal,
    fee_rate: Decimal,
    max_abs_position: Decimal,
    max_notional: Decimal,
    ledger: EconomicLedger,
) -> bool:
    """Re-execute the bound risk gate for every non-HOLD replay episode."""

    decision_side = record["decision"]
    if decision_side == "HOLD":
        if record["risk_outcome"] != "hold":
            raise ValueError(
                "Checkpoint replay HOLD outcome does not match bound risk admission"
            )
        return False

    valuation_price = _checkpoint_decimal(
        record["valuation_price"],
        name="evidence valuation_price",
    )
    admitted, replay_reason = handle_risk(
        Decision(
            side=decision_side,
            quantity=order_quantity,
            price=valuation_price,
            reason=record["decision_reason"],
        ),
        ledger.position,
        ledger.cash,
        fee_rate,
        max_abs_position,
        max_notional,
    )
    has_order = record["order_id"] is not None
    if has_order:
        if (
            not admitted
            or replay_reason != "admitted"
            or record["risk_outcome"] != "admitted"
        ):
            raise ValueError(
                "Checkpoint replay fill violates bound risk admission"
            )
        return True
    if admitted or record["risk_outcome"] != replay_reason:
        raise ValueError(
            "Checkpoint replay risk rejection does not match bound risk admission"
        )
    return False


def _require_checkpoint_evidence_financial_state(
    state: dict,
    *,
    symbol: str,
    financial_configuration_hash: str,
    order_quantity: Decimal,
    fee_rate: Decimal,
    max_abs_position: Decimal,
    max_notional: Decimal,
    restored_fills: dict[str, Fill],
    ledger: EconomicLedger,
) -> None:
    """Bind checkpoint evidence to the already authenticated final financial state."""

    ids = state.get("evidence_ids", [])
    records = state.get("evidence_records", {})
    if type(ids) is not list or type(records) is not dict:
        raise ValueError("Corrupt checkpoint replay authority")
    if len(ids) != len(set(ids)) or set(ids) != set(records):
        raise ValueError("Corrupt checkpoint replay evidence identity")
    if not ids:
        if restored_fills or ledger.postings:
            raise ValueError("Checkpoint financial state lacks replay evidence")
        return

    observed_order_ids: set[str] = set()
    evidence_by_order_id: dict[str, dict] = {}
    ordered: list[tuple[int, datetime, str, dict]] = []
    for evidence_id in ids:
        if type(evidence_id) is not str or not evidence_id:
            raise ValueError("Corrupt checkpoint replay evidence identity")
        record = records.get(evidence_id)
        episode_sequence, timestamp = _require_replay_evidence_record(
            record,
            evidence_id=evidence_id,
            financial_configuration_hash=financial_configuration_hash,
        )
        order_id = record["order_id"]
        if order_id is not None:
            if order_id in observed_order_ids:
                raise ValueError("Checkpoint replay reuses one durable order identity")
            fill = restored_fills.get(order_id)
            if fill is None or fill.fill_id != record["fill_id"]:
                raise ValueError("Checkpoint replay evidence conflicts with restored fill")
            if (
                record["decision"] != fill.side
                or order_id
                != _simulation_intent_id(
                    symbol=symbol,
                    side=fill.side,
                    quantity=fill.quantity,
                    price=fill.price,
                    input_hash=record["input_hash"],
                    financial_configuration_hash=financial_configuration_hash,
                )
            ):
                raise ValueError(
                    "Checkpoint replay intent identity is not causally bound"
                )
            evidence_by_order_id[order_id] = record
            observed_order_ids.add(order_id)
        ordered.append((episode_sequence, timestamp, evidence_id, record))

    ordered_by_sequence = sorted(ordered, key=lambda item: item[0])
    if [item[0] for item in ordered_by_sequence] != list(
        range(1, len(ordered_by_sequence) + 1)
    ):
        raise ValueError("Checkpoint replay episode sequence is not contiguous")

    if observed_order_ids != set(restored_fills):
        raise ValueError("Checkpoint replay evidence does not cover restored fills")

    fills_by_id = {fill.fill_id: fill for fill in restored_fills.values()}
    if len(fills_by_id) != len(restored_fills):
        raise ValueError("Checkpoint replay reuses one durable fill identity")
    risk_replay_ledger = EconomicLedger(ledger.initial_cash)
    for _sequence, _timestamp, _evidence_id, record in ordered_by_sequence:
        should_fill = _require_replay_risk_outcome(
            record,
            order_quantity=order_quantity,
            fee_rate=fee_rate,
            max_abs_position=max_abs_position,
            max_notional=max_notional,
            ledger=risk_replay_ledger,
        )
        order_id = record["order_id"]
        if not should_fill:
            if order_id is not None:
                raise ValueError(
                    "Checkpoint replay fill violates bound risk admission"
                )
            continue
        if order_id is None:
            raise ValueError(
                "Checkpoint replay admitted episode lacks durable fill authority"
            )
        fill = restored_fills.get(order_id)
        if fill is None:
            raise ValueError("Checkpoint fill lacks replay evidence authority")
        valuation_price = _checkpoint_decimal(
            record["valuation_price"],
            name="evidence valuation_price",
        )
        if fill.quantity != order_quantity or fill.price != valuation_price:
            raise ValueError(
                "Checkpoint replay fill conflicts with bound strategy inputs"
            )
        if not risk_replay_ledger.apply_fill(fill):
            raise ValueError("Checkpoint replay duplicates one fill")
    if risk_replay_ledger.postings != ledger.postings:
        raise ValueError(
            "Checkpoint posting chronology conflicts with durable replay prefix"
        )
    if (
        risk_replay_ledger.cash != ledger.cash
        or risk_replay_ledger.position != ledger.position
    ):
        raise ValueError("Checkpoint replay financial path does not match final state")

    latest_record = ordered_by_sequence[-1][3]
    if (
        latest_record["cash"] != str(ledger.cash)
        or latest_record["position"] != str(ledger.position)
    ):
        raise ValueError(
            "Checkpoint replay evidence does not match final financial state"
        )


def _repair_interrupted_replay(
    root: Path,
    state: dict,
    *,
    symbol: str,
    financial_configuration_hash: str,
) -> None:
    """Repair only the latest incomplete replay tail before new financial work."""

    ids = state.get("evidence_ids", [])
    records = state.get("evidence_records", {})
    if type(ids) is not list or type(records) is not dict:
        raise ValueError("Corrupt checkpoint replay authority")
    if any(type(evidence_id) is not str or not evidence_id for evidence_id in ids):
        raise ValueError("Corrupt checkpoint replay evidence identity")
    if len(ids) != len(set(ids)) or set(ids) != set(records):
        raise ValueError("Corrupt checkpoint replay evidence identity")
    if not ids:
        evidence_path = root / "learning-evidence.jsonl"
        if evidence_path.exists() or any(root.glob("journal.sqlite3*")):
            raise ValueError(
                "Durable replay state exists without checkpoint evidence authority"
            )
        return

    ordered: list[tuple[int, datetime, str, dict]] = []
    for evidence_id in ids:
        record = records.get(evidence_id)
        if type(evidence_id) is not str or not evidence_id:
            raise ValueError("Corrupt checkpoint replay evidence identity")
        episode_sequence, timestamp = _require_replay_evidence_record(
            record,
            evidence_id=evidence_id,
            financial_configuration_hash=financial_configuration_hash,
        )
        ordered.append((episode_sequence, timestamp, evidence_id, record))

    ordered_by_sequence = sorted(ordered, key=lambda item: item[0])
    if [item[0] for item in ordered_by_sequence] != list(
        range(1, len(ordered_by_sequence) + 1)
    ):
        raise ValueError("Checkpoint replay episode sequence is not contiguous")
    canonical_evidence_order = [item[2] for item in ordered_by_sequence]
    _sequence, _instant, latest_id, latest_record = ordered_by_sequence[-1]
    expected_ids = set(ids)

    evidence_path = root / "learning-evidence.jsonl"
    existing_evidence_ids: set[str] = set()
    existing_evidence_order: list[str] = []
    if evidence_path.is_file():
        try:
            for line in evidence_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    raise ValueError(
                        "Corrupt learning evidence before replay repair"
                    )
                recorded = _strict_json_loads(line)
                evidence_id = (
                    recorded.get("evidence_id")
                    if type(recorded) is dict
                    else None
                )
                if (
                    type(evidence_id) is not str
                    or not evidence_id
                    or evidence_id in existing_evidence_ids
                ):
                    raise ValueError(
                        "Corrupt learning evidence before replay repair"
                    )
                expected_record = records.get(evidence_id)
                if type(expected_record) is not dict or recorded != expected_record:
                    raise ValueError(
                        "Learning evidence conflicts with checkpoint before replay repair"
                    )
                existing_evidence_ids.add(evidence_id)
                existing_evidence_order.append(evidence_id)
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise ValueError(
                "Corrupt learning evidence before replay repair"
            ) from error
    missing_evidence = expected_ids - existing_evidence_ids
    if missing_evidence - {latest_id}:
        raise ValueError("Historical learning evidence is incomplete before latest episode")
    if existing_evidence_order != canonical_evidence_order[:len(existing_evidence_order)]:
        raise ValueError(
            "Learning evidence chronology conflicts with checkpoint episode sequence"
        )

    journal_path = root / "journal.sqlite3"
    journal_ids: set[str] = set()
    journal_order: list[str] = []
    if journal_path.is_file():
        store = JournalStore(journal_path)
        replay_events = store.load_events("simulation_portfolio", symbol)
        existing_event_count = len(replay_events)
        expected_existing_counts = {
            "events": existing_event_count,
            "outbox": existing_event_count,
            "command_dedupe": 0,
            "projection_checkpoints": 0,
            "global_projection_checkpoints": 0,
        }
        initial_store_cut = store.whole_store_state_cut()
        if (
            initial_store_cut.get("counts") != expected_existing_counts
            or initial_store_cut.get("journal_sequence") != existing_event_count
        ):
            raise ValueError(
                "Unexpected durable journal state before replay repair"
            )
        for expected_aggregate_version, event in enumerate(replay_events, start=1):
            if type(event) is not dict:
                raise ValueError("Corrupt simulation journal replay event")
            if (
                event.get("aggregate_version") != expected_aggregate_version
                or event.get("journal_sequence") != expected_aggregate_version
                or event.get("event_type") != "SimulationEpisodeRecorded"
                or event.get("aggregate_type") != "simulation_portfolio"
                or event.get("aggregate_id") != symbol
                or event.get("host_id") != "local-mvp"
                or event.get("owner_epoch") != "1"
                or event.get("environment") != "SIMULATION"
                or event.get("schema_version") != "2.0.0"
                or event.get("causation_id") is not None
                or event.get("evidence_refs") != []
            ):
                raise ValueError("Corrupt simulation journal replay event")
            payload = event.get("payload")
            evidence_id = (
                payload.get("evidence_id")
                if type(payload) is dict
                else None
            )
            if type(evidence_id) is not str or evidence_id in journal_ids:
                raise ValueError("Corrupt simulation journal evidence identity")
            record = records.get(evidence_id)
            if type(record) is not dict:
                raise ValueError("Corrupt simulation journal evidence identity")
            if record.get("episode_sequence") != expected_aggregate_version:
                raise ValueError("Corrupt simulation journal episode sequence")
            expected_payload = {
                "evidence_id": evidence_id,
                "episode_sequence": record["episode_sequence"],
                "input_hash": record.get("input_hash"),
                "decision": record.get("decision"),
                "decision_reason": record.get("decision_reason"),
                "risk_outcome": record.get("risk_outcome"),
                "order_id": record.get("order_id"),
                "fill_id": record.get("fill_id"),
                "cash": record.get("cash"),
                "position": record.get("position"),
                "equity": record.get("equity"),
                "valuation_price": record.get("valuation_price"),
                "reconciled": record.get("reconciled"),
                "financial_configuration_hash": financial_configuration_hash,
            }
            try:
                expected_timestamp = _utc_z(record["recorded_at"])
            except (KeyError, TypeError, ValueError) as error:
                raise ValueError("Corrupt simulation journal replay chronology") from error
            if (
                payload != expected_payload
                or event.get("event_id")
                != _event_uuid("simulation-episode", evidence_id)
                or event.get("correlation_id")
                != _event_uuid("correlation", evidence_id)
                or event.get("occurred_at") != expected_timestamp
                or event.get("observed_at") != expected_timestamp
                or event.get("committed_at") != expected_timestamp
            ):
                raise ValueError("Corrupt simulation journal replay event")
            store.outbox_delivery_state(
                event["event_id"],
                topic="autotrade.simulation.events",
            )
            journal_ids.add(evidence_id)
            journal_order.append(evidence_id)
        if store.whole_store_state_cut() != initial_store_cut:
            raise ValueError(
                "Durable journal state changed during replay preflight"
            )
    if journal_ids - expected_ids:
        raise ValueError("Simulation journal contains unknown replay evidence")
    if journal_order != canonical_evidence_order[:len(journal_order)]:
        raise ValueError(
            "Simulation journal chronology conflicts with checkpoint episode sequence"
        )
    missing_journal = expected_ids - journal_ids
    if missing_journal - {latest_id}:
        raise ValueError("Historical simulation journal is incomplete before latest episode")

    postings = state.get("postings", [])
    if type(postings) is not list:
        raise ValueError("Corrupt checkpoint posting chronology")
    posting_fill_ids: list[str] = []
    for posting in postings:
        if (
            type(posting) is not dict
            or type(posting.get("fill_id")) is not str
            or not posting["fill_id"]
        ):
            raise ValueError("Corrupt checkpoint posting chronology")
        posting_fill_ids.append(posting["fill_id"])
    if len(posting_fill_ids) != len(set(posting_fill_ids)):
        raise ValueError("Corrupt checkpoint posting chronology")

    latest_fill_id = latest_record["fill_id"]

    def require_durable_fill_prefix(
        evidence_order: list[str],
        missing_ids: set[str],
    ) -> None:
        observed_fill_ids = [
            records[evidence_id]["fill_id"]
            for evidence_id in evidence_order
            if records[evidence_id]["fill_id"] is not None
        ]
        expected_fill_ids = posting_fill_ids
        if latest_id in missing_ids and latest_fill_id is not None:
            if not posting_fill_ids or posting_fill_ids[-1] != latest_fill_id:
                raise ValueError(
                    "Checkpoint posting chronology conflicts with durable replay prefix"
                )
            expected_fill_ids = posting_fill_ids[:-1]
        if observed_fill_ids != expected_fill_ids:
            raise ValueError(
                "Checkpoint posting chronology conflicts with durable replay prefix"
            )

    require_durable_fill_prefix(existing_evidence_order, missing_evidence)
    require_durable_fill_prefix(journal_order, missing_journal)

    for _sequence, _timestamp, _evidence_id, record in ordered_by_sequence:
        _append_evidence(evidence_path, record)
    if latest_id in missing_journal:
        handle_journal_event(
            root,
            symbol,
            latest_record,
            financial_configuration_hash,
        )

    if not verify_replay(root):
        raise ValueError("Durable replay state is invalid before resume")


def _persist_intent(path: Path, intent: OrderIntent) -> None:
    payload = {**asdict(intent), "quantity": str(intent.quantity), "price": str(intent.price)}
    if path.exists():
        try:
            existing = _strict_json_loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as error:
            raise ValueError("Corrupt durable order intent") from error
        if existing != payload:
            raise ValueError("Durable order intent conflicts with this decision")
        return
    _atomic_json(path, payload)


def _restore_simulated_fills(
    state: dict,
    root: Path,
    *,
    symbol: str,
    fee_rate: Decimal,
) -> dict[str, Fill]:
    """Reconstruct simulated fills only from exact checkpoint + intent authority."""

    restored: dict[str, Fill] = {}
    raw_fills = state.get("fills", {})
    if type(raw_fills) is not dict:
        raise ValueError("Corrupt checkpoint fills")
    fill_fields = {
        "fill_id",
        "client_order_id",
        "symbol",
        "side",
        "quantity",
        "price",
        "fee",
    }
    for key, value in raw_fills.items():
        if type(key) is not str or type(value) is not dict or set(value) != fill_fields:
            raise ValueError("Corrupt checkpoint fill structure")
        client_order_id = value["client_order_id"]
        fill_id = value["fill_id"]
        fill_symbol = value["symbol"]
        side = value["side"]
        if (
            type(client_order_id) is not str
            or len(client_order_id) != 27
            or not client_order_id.startswith("intent-")
            or any(char not in "0123456789abcdef" for char in client_order_id[7:])
            or key != client_order_id
        ):
            raise ValueError("Corrupt checkpoint fill client order identity")
        expected_fill_id = (
            "fill-" + sha256(client_order_id.encode("utf-8")).hexdigest()[:20]
        )
        if type(fill_id) is not str or fill_id != expected_fill_id:
            raise ValueError("Corrupt checkpoint fill identity")
        if type(fill_symbol) is not str or fill_symbol != symbol:
            raise ValueError("Corrupt checkpoint fill symbol")
        if type(side) is not str or side not in {"BUY", "SELL"}:
            raise ValueError("Corrupt checkpoint fill side")

        quantity = _checkpoint_decimal(value["quantity"], name="fill quantity")
        price = _checkpoint_decimal(value["price"], name="fill price")
        fee = _checkpoint_decimal(value["fee"], name="fill fee")
        if quantity <= 0 or price <= 0 or fee < 0:
            raise ValueError("Corrupt checkpoint fill financial scalar")
        expected_fee = _money(exact_multiply(quantity, price, fee_rate))
        if fee != expected_fee:
            raise ValueError("Corrupt checkpoint fill fee does not match bound fee rate")

        intent_path = root / "order-intents" / f"{client_order_id}.json"
        try:
            intent_payload = _strict_json_loads(intent_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as error:
            raise ValueError("Durable order intent missing for checkpoint fill") from error
        expected_intent_payload = {
            "client_order_id": client_order_id,
            "symbol": symbol,
            "side": side,
            "quantity": str(quantity),
            "price": str(price),
        }
        if type(intent_payload) is not dict or intent_payload != expected_intent_payload:
            raise ValueError("Checkpoint fill conflicts with durable order intent")

        restored[client_order_id] = Fill(
            fill_id=fill_id,
            client_order_id=client_order_id,
            symbol=symbol,
            side=side,
            quantity=quantity,
            price=price,
            fee=fee,
        )

    intents_root = root / "order-intents"
    try:
        durable_intent_files = (
            {path.name for path in intents_root.glob("*.json")}
            if intents_root.is_dir()
            else set()
        )
    except OSError as error:
        raise ValueError("Cannot enumerate durable order intents") from error
    expected_intent_files = {
        f"{client_order_id}.json" for client_order_id in restored
    }
    if durable_intent_files != expected_intent_files:
        raise ValueError(
            "Durable order intents do not match checkpoint fills"
        )
    return restored


def _reconcile(provider: SimulatedProvider, ledger: EconomicLedger) -> bool:
    postings = {row["fill_id"]: row for row in ledger.postings}
    if len(postings) != len(ledger.postings) or len(postings) != len(provider.fills):
        raise ValueError("Fill and ledger counts do not reconcile")
    for fill in provider.fills.values():
        row = postings.get(fill.fill_id)
        signed = (
            fill.quantity
            if fill.side == "BUY"
            else exact_subtract(Decimal("0"), fill.quantity)
        )
        expected_cash = _money(
            exact_subtract(
                exact_subtract(
                    Decimal("0"),
                    exact_multiply(signed, fill.price),
                ),
                fill.fee,
            )
        )
        if (
            row is None
            or _checkpoint_decimal(row["position_delta"], name="position_delta") != signed
            or _money(_checkpoint_decimal(row["cash_delta"], name="cash_delta")) != expected_cash
        ):
            raise ValueError("Fill and economic ledger do not reconcile")
    return True


def verify_replay(state_dir: str | Path) -> bool:
    """Verify checkpoint, evidence and durable journal share one financial config root."""
    root = Path(state_dir)
    checkpoint_path = root / "checkpoint.json"
    evidence_path = root / "learning-evidence.jsonl"
    journal_path = root / "journal.sqlite3"
    if not checkpoint_path.is_file() or not evidence_path.is_file() or not journal_path.is_file():
        return False
    try:
        checkpoint = _strict_json_loads(checkpoint_path.read_text(encoding="utf-8"))
        checkpoint_fields = {
            "schema_version",
            "symbol",
            "financial_configuration",
            "financial_configuration_hash",
            "initial_cash",
            "postings",
            "fills",
            "evidence_ids",
            "evidence_records",
        }
        if type(checkpoint) is not dict or set(checkpoint) != checkpoint_fields:
            return False
        if checkpoint.get("schema_version") != CHECKPOINT_SCHEMA_VERSION:
            return False
        configuration = checkpoint["financial_configuration"]
        configuration_hash = checkpoint["financial_configuration_hash"]
        if type(configuration) is not dict or type(configuration_hash) is not str:
            return False
        if configuration_hash != _stable_hash(configuration):
            return False
        symbol = configuration.get("symbol")
        if type(symbol) is not str or not symbol or symbol != symbol.strip():
            return False
        try:
            initial_cash = _money(
                _checkpoint_decimal(
                    configuration.get("initial_cash"),
                    name="financial configuration initial cash",
                )
            )
            order_quantity = _money(
                _checkpoint_decimal(
                    configuration.get("order_quantity"),
                    name="financial configuration order quantity",
                )
            )
            max_abs_position = _money(
                _checkpoint_decimal(
                    configuration.get("max_abs_position"),
                    name="financial configuration max position",
                )
            )
            max_notional = _money(
                _checkpoint_decimal(
                    configuration.get("max_notional"),
                    name="financial configuration max notional",
                )
            )
            fee_rate = _checkpoint_decimal(
                configuration.get("fee_rate"),
                name="financial configuration fee rate",
            )
        except (ValueError, TypeError, ArithmeticError):
            return False
        if (
            initial_cash <= 0
            or order_quantity <= 0
            or max_abs_position <= 0
            or max_notional <= 0
            or not fee_rate.is_finite()
            or fee_rate < 0
            or fee_rate >= 1
        ):
            return False
        expected_configuration = _financial_configuration(
            symbol=symbol,
            initial_cash=initial_cash,
            order_quantity=order_quantity,
            max_abs_position=max_abs_position,
            max_notional=max_notional,
            fee_rate=fee_rate,
        )
        if configuration != expected_configuration:
            return False
        if checkpoint.get("symbol") != symbol:
            return False
        if checkpoint.get("initial_cash") != configuration.get("initial_cash"):
            return False
        records = checkpoint["evidence_records"]
        ids = checkpoint["evidence_ids"]
        evidence_fields = frozenset(
            {
                "schema_version",
                "evidence_id",
                "episode_sequence",
                "input_hash",
                "decision",
                "decision_reason",
                "risk_outcome",
                "order_id",
                "fill_id",
                "cash",
                "position",
                "equity",
                "valuation_price",
                "reconciled",
                "financial_configuration_hash",
                "recorded_at",
            }
        )
        rows = [
            _strict_json_loads(line)
            for line in evidence_path.read_text(encoding="utf-8").splitlines()
        ]
        if any(
            type(row) is not dict
            or set(row) != evidence_fields
            or row["schema_version"] != 2
            or type(row["episode_sequence"]) is not int
            or row["episode_sequence"] < 1
            or type(row["evidence_id"]) is not str
            or not row["evidence_id"]
            or type(row["input_hash"]) is not str
            or len(row["input_hash"]) != 64
            or row["decision"] not in {"BUY", "SELL", "HOLD"}
            or type(row["decision_reason"]) is not str
            or type(row["risk_outcome"]) is not str
            or row["order_id"] is not None and type(row["order_id"]) is not str
            or row["fill_id"] is not None and type(row["fill_id"]) is not str
            or type(row["cash"]) is not str
            or type(row["position"]) is not str
            or type(row["equity"]) is not str
            or type(row["valuation_price"]) is not str
            or type(row["reconciled"]) is not bool
            or row["financial_configuration_hash"] != configuration_hash
            for row in rows
        ):
            return False
        if [row["episode_sequence"] for row in rows] != list(
            range(1, len(rows) + 1)
        ):
            return False
        observed = {row["evidence_id"]: row for row in rows}
        if not isinstance(records, dict) or not isinstance(ids, list):
            return False
        if len(ids) != len(set(ids)):
            return False
        if not (len(ids) == len(records) == len(observed) == len(rows)):
            return False
        if set(ids) != set(records) or set(ids) != set(observed):
            return False
        if any(
            type(records[key]) is not dict
            or set(records[key]) != evidence_fields
            or records[key]["financial_configuration_hash"] != configuration_hash
            or observed[key] != records[key]
            for key in ids
        ):
            return False
        for row in rows:
            if any(char not in "0123456789abcdef" for char in row["input_hash"]):
                return False
            if not _replay_evidence_semantics_valid(row):
                return False
            if not row["reconciled"]:
                return False
            if (row["order_id"] is None) != (row["fill_id"] is None):
                return False
            financial_values: dict[str, Decimal] = {}
            for field in ("cash", "position", "equity", "valuation_price"):
                value = _checkpoint_decimal(row[field], name=f"evidence {field}")
                if str(value) != row[field]:
                    return False
                financial_values[field] = value
            valuation_price = financial_values["valuation_price"]
            if valuation_price <= 0:
                return False
            if financial_values["equity"] != _money(
                exact_add(
                    financial_values["cash"],
                    exact_multiply(financial_values["position"], valuation_price),
                )
            ):
                return False

        checkpoint_ledger = EconomicLedger(initial_cash, list(checkpoint["postings"]))
        restored_fills = _restore_simulated_fills(
            checkpoint,
            root,
            symbol=symbol,
            fee_rate=fee_rate,
        )
        for row in rows:
            order_id = row["order_id"]
            if order_id is None:
                continue
            fill = restored_fills.get(order_id)
            if (
                fill is None
                or row["fill_id"] != fill.fill_id
                or row["decision"] != fill.side
                or order_id
                != _simulation_intent_id(
                    symbol=symbol,
                    side=fill.side,
                    quantity=fill.quantity,
                    price=fill.price,
                    input_hash=row["input_hash"],
                    financial_configuration_hash=configuration_hash,
                )
            ):
                return False

        checkpoint_provider = SimulatedProvider(restored_fills)
        _reconcile(checkpoint_provider, checkpoint_ledger)

        store = JournalStore(journal_path)
        expected_counts = {
            "events": len(ids),
            "outbox": len(ids),
            "command_dedupe": 0,
            "projection_checkpoints": 0,
            "global_projection_checkpoints": 0,
        }
        initial_store_cut = store.whole_store_state_cut()
        if (
            initial_store_cut.get("counts") != expected_counts
            or initial_store_cut.get("journal_sequence") != len(ids)
        ):
            return False
        events = store.load_events("simulation_portfolio", symbol)
        if not events:
            return False
        journal_evidence_ids = set()
        replayed_order_ids = set()
        replayed_fill_ids = set()
        replay_ledger = EconomicLedger(initial_cash)
        if len(events) != len(ids):
            return False
        for expected_aggregate_version, event in enumerate(events, start=1):
            if type(event) is not dict:
                return False
            if (
                event.get("aggregate_version") != expected_aggregate_version
                or event.get("journal_sequence") != expected_aggregate_version
                or event.get("event_type") != "SimulationEpisodeRecorded"
                or event.get("aggregate_type") != "simulation_portfolio"
                or event.get("aggregate_id") != symbol
                or event.get("host_id") != "local-mvp"
                or event.get("owner_epoch") != "1"
                or event.get("environment") != "SIMULATION"
                or event.get("schema_version") != "2.0.0"
                or event.get("causation_id") is not None
                or event.get("evidence_refs") != []
            ):
                return False
            payload = event.get("payload")
            if type(payload) is not dict:
                return False
            evidence_id = payload.get("evidence_id")
            if type(evidence_id) is not str or evidence_id in journal_evidence_ids:
                return False
            journal_evidence_ids.add(evidence_id)
            record = observed.get(evidence_id)
            if type(record) is not dict:
                return False
            if record["episode_sequence"] != expected_aggregate_version:
                return False
            expected_payload = {
                "evidence_id": evidence_id,
                "episode_sequence": record["episode_sequence"],
                "input_hash": record["input_hash"],
                "decision": record["decision"],
                "decision_reason": record["decision_reason"],
                "risk_outcome": record["risk_outcome"],
                "order_id": record["order_id"],
                "fill_id": record["fill_id"],
                "cash": record["cash"],
                "position": record["position"],
                "equity": record["equity"],
                "valuation_price": record["valuation_price"],
                "reconciled": record["reconciled"],
                "financial_configuration_hash": configuration_hash,
            }
            if payload != expected_payload:
                return False
            order_id = record["order_id"]
            fill_id = record["fill_id"]
            should_fill = _require_replay_risk_outcome(
                record,
                order_quantity=order_quantity,
                fee_rate=fee_rate,
                max_abs_position=max_abs_position,
                max_notional=max_notional,
                ledger=replay_ledger,
            )
            if order_id is not None:
                if not should_fill:
                    return False
                fill = restored_fills.get(order_id)
                if (
                    fill is None
                    or fill.fill_id != fill_id
                    or order_id in replayed_order_ids
                    or fill_id in replayed_fill_ids
                ):
                    return False
                valuation_price = _checkpoint_decimal(
                    record["valuation_price"],
                    name="evidence valuation_price",
                )
                if (
                    fill.quantity != order_quantity
                    or fill.price != valuation_price
                    or not replay_ledger.apply_fill(fill)
                ):
                    return False
                replayed_order_ids.add(order_id)
                replayed_fill_ids.add(fill_id)
            elif should_fill:
                return False
            if (
                record["cash"] != str(replay_ledger.cash)
                or record["position"] != str(replay_ledger.position)
            ):
                return False
            if event.get("event_id") != _event_uuid("simulation-episode", evidence_id):
                return False
            expected_timestamp = _utc_z(record["recorded_at"])
            if (
                event.get("occurred_at") != expected_timestamp
                or event.get("observed_at") != expected_timestamp
                or event.get("committed_at") != expected_timestamp
                or event.get("correlation_id")
                != _event_uuid("correlation", evidence_id)
            ):
                return False
            store.outbox_delivery_state(
                event["event_id"],
                topic="autotrade.simulation.events",
            )
        if journal_evidence_ids != set(ids):
            return False
        if replayed_order_ids != set(restored_fills):
            return False
        if replay_ledger.postings != checkpoint_ledger.postings:
            return False
        if (
            replay_ledger.cash != checkpoint_ledger.cash
            or replay_ledger.position != checkpoint_ledger.position
        ):
            return False
        if store.whole_store_state_cut() != initial_store_cut:
            return False
        return True
    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
        return False


def run_vertical_slice(
    prices: Iterable[float | str | Decimal],
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

    if type(symbol) is not str:
        raise TypeError("symbol must be exact text")
    if not symbol or symbol != symbol.strip():
        raise ValueError("A simulated symbol must be non-empty canonical text")
    starting_cash = _money(initial_cash)
    quantity = _money(order_quantity)
    position_limit = _money(max_abs_position)
    notional_limit = _money(max_notional)
    rate = _exact_decimal(fee_rate, name="fee_rate")
    if starting_cash <= 0 or quantity <= 0 or position_limit <= 0 or notional_limit <= 0:
        raise ValueError("Cash, order quantity and risk limits must be positive")
    if not rate.is_finite() or rate < 0 or rate >= 1:
        raise ValueError("Fee rate must be finite and between zero and one")
    root = Path(state_dir)
    checkpoint_path = root / "checkpoint.json"
    evidence_path = root / "learning-evidence.jsonl"
    _require_no_pending_atomic_writes(root)
    financial_configuration = _financial_configuration(
        symbol=symbol,
        initial_cash=starting_cash,
        order_quantity=quantity,
        max_abs_position=position_limit,
        max_notional=notional_limit,
        fee_rate=rate,
    )
    financial_configuration_hash = _stable_hash(financial_configuration)
    state, resumed = handle_restart_recovery(state_dir, starting_cash)
    if resumed:
        _require_checkpoint_configuration(state, financial_configuration)
        # Tail repair is itself a durable mutation. Authenticate the checkpoint's
        # financial state and its intent-backed simulated fills before repairing
        # any missing evidence/journal tail, so corrupt economics cannot cause a
        # partial replay write before the resume is rejected.
        preflight_ledger = EconomicLedger(
            _checkpoint_decimal(state["initial_cash"], name="initial_cash"),
            list(state.get("postings", [])),
        )
        preflight_fills = _restore_simulated_fills(
            state,
            root,
            symbol=symbol,
            fee_rate=rate,
        )
        _reconcile(SimulatedProvider(preflight_fills), preflight_ledger)
        _require_checkpoint_evidence_financial_state(
            state,
            symbol=symbol,
            financial_configuration_hash=financial_configuration_hash,
            order_quantity=quantity,
            fee_rate=rate,
            max_abs_position=position_limit,
            max_notional=notional_limit,
            restored_fills=preflight_fills,
            ledger=preflight_ledger,
        )
        _repair_interrupted_replay(
            root,
            state,
            symbol=symbol,
            financial_configuration_hash=financial_configuration_hash,
        )
    elif _has_residual_durable_state(root):
        raise ValueError(
            "Durable state exists without exact financial configuration identity"
        )
    ledger = EconomicLedger(
        _checkpoint_decimal(state["initial_cash"], name="initial_cash"),
        list(state.get("postings", [])),
    )
    restored_fills = _restore_simulated_fills(
        state,
        root,
        symbol=symbol,
        fee_rate=rate,
    )
    provider = SimulatedProvider(restored_fills)
    _reconcile(provider, ledger)
    normalized = handle_market_data(prices)
    input_hash = _stable_hash([str(item) for item in normalized])
    decision = handle_strategy(normalized, quantity)
    intent = None
    fill = None
    risk_reason = "hold"
    if decision.side != "HOLD":
        intent = OrderIntent(
            client_order_id=_simulation_intent_id(
                symbol=symbol,
                side=decision.side,
                quantity=decision.quantity,
                price=decision.price,
                input_hash=input_hash,
                financial_configuration_hash=financial_configuration_hash,
            ),
            symbol=symbol,
            side=decision.side,
            quantity=decision.quantity,
            price=decision.price,
        )
        if intent.client_order_id in provider.fills:
            # A restored fill proves this exact durable intent was admitted in
            # the original episode. Replaying the same deterministic episode
            # must preserve that recorded admission fact rather than minting a
            # new evidence outcome merely because the fill is already durable.
            admitted, risk_reason = True, "admitted"
        else:
            admitted, risk_reason = handle_risk(decision, ledger.position, ledger.cash, rate, position_limit, notional_limit)
        if admitted:
            if not resumed and not checkpoint_path.exists():
                _atomic_json(
                    checkpoint_path,
                    _initial_checkpoint(financial_configuration),
                )
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
        "financial_configuration_hash": financial_configuration_hash,
    })[:20]
    recorded_evidence = _find_evidence(evidence_path, evidence_id)
    checkpoint_evidence = evidence_records.get(evidence_id)
    sequence_source = (
        checkpoint_evidence
        if checkpoint_evidence is not None
        else recorded_evidence
    )
    if sequence_source is None:
        episode_sequence = len(evidence_records) + 1
    elif (
        type(sequence_source) is dict
        and type(sequence_source.get("episode_sequence")) is int
        and sequence_source["episode_sequence"] >= 1
    ):
        episode_sequence = sequence_source["episode_sequence"]
    else:
        raise ValueError("Checkpoint evidence lacks exact episode sequence")
    fresh_evidence = {
        "schema_version": 2,
        "evidence_id": evidence_id,
        "episode_sequence": episode_sequence,
        "input_hash": input_hash,
        "decision": decision.side,
        "decision_reason": decision.reason,
        "risk_outcome": risk_reason,
        "order_id": intent.client_order_id if intent else None,
        "fill_id": fill.fill_id if fill else None,
        "cash": str(ledger.cash),
        "position": str(ledger.position),
        "equity": str(equity),
        "valuation_price": str(last_price),
        "reconciled": reconciled,
        "financial_configuration_hash": financial_configuration_hash,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }
    if checkpoint_evidence is not None:
        evidence = checkpoint_evidence
    elif recorded_evidence is not None:
        # Adopt an evidence row left durable by an older interrupted build.
        evidence = recorded_evidence
    else:
        evidence = fresh_evidence
    if type(evidence) is not dict:
        raise ValueError("Checkpoint evidence conflicts with this episode")
    comparable_evidence = {
        key: value for key, value in evidence.items() if key != "recorded_at"
    }
    comparable_fresh = {
        key: value for key, value in fresh_evidence.items() if key != "recorded_at"
    }
    if comparable_evidence != comparable_fresh:
        raise ValueError("Checkpoint evidence conflicts with this episode")
    evidence_ids.add(evidence["evidence_id"])
    evidence_records[evidence_id] = evidence
    checkpoint = {
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "symbol": symbol,
        "financial_configuration": financial_configuration,
        "financial_configuration_hash": financial_configuration_hash,
        "initial_cash": str(ledger.initial_cash),
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
    handle_journal_event(
        root,
        symbol,
        evidence,
        financial_configuration_hash,
    )
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
    episodes: Iterable[Iterable[float | str | Decimal]],
    state_dir: str | Path,
    **kwargs,
) -> list[RunResult]:
    """Run a deterministic sequence through the same durable portfolio."""
    return [run_vertical_slice(episode, state_dir, **kwargs) for episode in episodes]