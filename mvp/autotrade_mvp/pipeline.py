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
    as_fraction, bounded_fraction, exact_abs, exact_add, exact_multiply,
    exact_subtract, exact_sum, parse_bounded_exact_decimal,
    round_fraction_to_quantum, terminating_decimal,
)
from .persistence import JournalStore, payload_digest


MONEY_QUANTUM = Decimal("0.00000001")
_CHECKPOINT_SCHEMA_VERSION = 2
_RUN_CONFIGURATION_SCHEMA_VERSION = 1
_STRATEGY_ID = "MOVING_AVERAGE"
_STRATEGY_VERSION = 1
_STRATEGY_FAST = 2
_STRATEGY_SLOW = 3


def _exact_decimal(value: Decimal | str | int, *, name: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError(f"{name} must use Decimal, string or integer input")
    return parse_bounded_exact_decimal(value)


def _money(
    value: Decimal | str | int,
    *,
    _quantum: Decimal = MONEY_QUANTUM,
    _parse=parse_bounded_exact_decimal,
    _fraction=as_fraction,
    _round=round_fraction_to_quantum,
) -> Decimal:
    """Quantize money through the process-frozen exact numeric dependencies."""

    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError("money must use Decimal, string or integer input")
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


def _atomic_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _run_configuration(
    *,
    symbol: str,
    initial_cash: Decimal,
    order_quantity: Decimal,
    max_abs_position: Decimal,
    max_notional: Decimal,
    fee_rate: Decimal,
    _configuration_schema_version: int = _RUN_CONFIGURATION_SCHEMA_VERSION,
    _checkpoint_schema_version: int = _CHECKPOINT_SCHEMA_VERSION,
    _money_quantum: Decimal = MONEY_QUANTUM,
    _strategy_id: str = _STRATEGY_ID,
    _strategy_version: int = _STRATEGY_VERSION,
    _strategy_fast: int = _STRATEGY_FAST,
    _strategy_slow: int = _STRATEGY_SLOW,
) -> dict[str, object]:
    return {
        "schema_version": _configuration_schema_version,
        "checkpoint_schema_version": _checkpoint_schema_version,
        "symbol": symbol,
        "initial_cash": str(initial_cash),
        "order_quantity": str(order_quantity),
        "max_abs_position": str(max_abs_position),
        "max_notional": str(max_notional),
        "fee_rate": str(fee_rate),
        "money_quantum": str(_money_quantum),
        "strategy": {
            "id": _strategy_id,
            "version": _strategy_version,
            "fast": _strategy_fast,
            "slow": _strategy_slow,
        },
    }


def _configuration_digest(configuration: dict[str, object]) -> str:
    return _stable_hash(configuration)


def _has_durable_run_state(root: Path) -> bool:
    if (
        (root / "checkpoint.json").exists()
        or (root / "learning-evidence.jsonl").exists()
        or (root / "journal.sqlite3").exists()
    ):
        return True
    intents = root / "order-intents"
    return intents.is_dir() and any(intents.glob("*.json"))


def _require_run_configuration(
    root: Path,
    expected: dict[str, object],
    *,
    _schema_version: int = _RUN_CONFIGURATION_SCHEMA_VERSION,
) -> str:
    path = root / "run-configuration.json"
    expected_digest = _configuration_digest(expected)
    if not path.exists():
        if _has_durable_run_state(root):
            raise ValueError(
                "Legacy durable run state lacks configuration identity; "
                "explicit migration or a new state directory is required"
            )
        _atomic_json(
            path,
            {
                "schema_version": _schema_version,
                "configuration_digest": expected_digest,
                "configuration": expected,
            },
        )
        return expected_digest

    try:
        envelope = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("Corrupt run configuration") from error
    if type(envelope) is not dict:
        raise ValueError("Corrupt run configuration")
    if type(envelope.get("schema_version")) is not int or (
        envelope["schema_version"] != _schema_version
    ):
        raise ValueError("Unsupported or corrupt run configuration schema")
    configuration = envelope.get("configuration")
    stored_digest = envelope.get("configuration_digest")
    if type(configuration) is not dict or type(stored_digest) is not str:
        raise ValueError("Corrupt run configuration")
    if len(stored_digest) != 64 or any(
        character not in "0123456789abcdef" for character in stored_digest
    ):
        raise ValueError("Corrupt run configuration digest")
    if _configuration_digest(configuration) != stored_digest:
        raise ValueError("Corrupt run configuration digest")
    if stored_digest != expected_digest:
        raise ValueError(
            "Run configuration is incompatible with existing durable state"
        )
    return stored_digest


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


def _checkpoint_text(value: object, *, name: str) -> str:
    if type(value) is not str or not value or value != str.strip(value):
        raise ValueError(f"Corrupt checkpoint {name}: expected canonical text")
    return value


def _restore_checkpoint_fill(
    map_key: object,
    value: object,
    *,
    expected_symbol: str,
) -> Fill:
    """Re-admit one durable simulated fill before it becomes financial truth."""

    client_key = _checkpoint_text(map_key, name="fill map key")
    if type(value) is not dict:
        raise ValueError("Corrupt checkpoint fill: expected exact object")
    required = {
        "fill_id",
        "client_order_id",
        "symbol",
        "side",
        "quantity",
        "price",
        "fee",
    }
    if set(dict.keys(value)) != required:
        raise ValueError("Corrupt checkpoint fill: unexpected schema")
    fill_id = _checkpoint_text(value["fill_id"], name="fill id")
    client_order_id = _checkpoint_text(
        value["client_order_id"], name="fill client_order_id"
    )
    symbol = _checkpoint_text(value["symbol"], name="fill symbol")
    side = _checkpoint_text(value["side"], name="fill side")
    if client_key != client_order_id:
        raise ValueError(
            "Corrupt checkpoint fill: map key does not match client_order_id"
        )
    intent_suffix = client_order_id.removeprefix("intent-")
    if (
        len(intent_suffix) != 20
        or client_order_id != "intent-" + intent_suffix
        or any(character not in "0123456789abcdef" for character in intent_suffix)
    ):
        raise ValueError(
            "Corrupt checkpoint fill: client_order_id is not canonical simulated identity"
        )
    if symbol != expected_symbol:
        raise ValueError("Corrupt checkpoint fill: symbol does not match run scope")
    if side not in {"BUY", "SELL"}:
        raise ValueError("Corrupt checkpoint fill: side must be BUY or SELL")
    expected_fill_id = (
        "fill-" + sha256(client_order_id.encode("utf-8")).hexdigest()[:20]
    )
    if fill_id != expected_fill_id:
        raise ValueError(
            "Corrupt checkpoint fill: fill_id does not match simulated identity"
        )
    quantity = _checkpoint_decimal(value["quantity"], name="fill quantity")
    price = _checkpoint_decimal(value["price"], name="fill price")
    fee = _checkpoint_decimal(value["fee"], name="fill fee")
    if quantity <= 0:
        raise ValueError("Corrupt checkpoint fill quantity: must be positive")
    if price <= 0:
        raise ValueError("Corrupt checkpoint fill price: must be positive")
    if fee < 0:
        raise ValueError("Corrupt checkpoint fill fee: must be non-negative")
    return Fill(
        fill_id=fill_id,
        client_order_id=client_order_id,
        symbol=symbol,
        side=side,
        quantity=quantity,
        price=price,
        fee=fee,
    )


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
    def __init__(
        self,
        fast: int = _STRATEGY_FAST,
        slow: int = _STRATEGY_SLOW,
    ):
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


def _read_state(
    path: Path,
    initial_cash: Decimal,
    *,
    _checkpoint_schema_version: int = _CHECKPOINT_SCHEMA_VERSION,
) -> tuple[dict, bool]:
    if not path.exists():
        return {"initial_cash": str(initial_cash), "postings": [], "fills": {}, "evidence_ids": []}, False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("Corrupt checkpoint JSON") from error
    if not isinstance(data, dict):
        raise ValueError("Corrupt checkpoint structure")
    schema_version = data.get("schema_version")
    if schema_version == 1:
        raise ValueError(
            "Legacy checkpoint lacks configuration identity; "
            "explicit migration or a new state directory is required"
        )
    if type(schema_version) is not int or schema_version != _checkpoint_schema_version:
        raise ValueError("Unsupported or corrupt checkpoint schema")
    if type(data.get("configuration_digest")) is not str:
        raise ValueError("Corrupt checkpoint configuration identity")
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


def _restore_evidence_graph(
    state: dict,
    evidence_path: Path,
) -> tuple[set[str], dict[str, dict]]:
    """Validate recovered learning evidence before any new durable mutation.

    A crash may leave the append-only JSONL row missing after the checkpoint
    commit, and legacy checkpoints may omit the record map. Existing JSONL rows
    may therefore hydrate only missing checkpoint records. Duplicate identities,
    foreign rows, or checkpoint/JSONL disagreement are corruption and must fail
    before a new checkpoint, evidence row, or journal event is written.
    """

    raw_ids = state.get("evidence_ids", [])
    if type(raw_ids) is not list:
        raise ValueError("Corrupt checkpoint evidence IDs")
    ordered_ids = tuple(
        _checkpoint_text(value, name="evidence id")
        for value in raw_ids
    )
    if len(set(ordered_ids)) != len(ordered_ids):
        raise ValueError("Corrupt checkpoint evidence IDs: duplicates")
    expected_ids = set(ordered_ids)

    raw_records = state.get("evidence_records", {})
    if type(raw_records) is not dict:
        raise ValueError("Corrupt checkpoint evidence records")
    records: dict[str, dict] = {}
    for key, value in dict.items(raw_records):
        evidence_id = _checkpoint_text(key, name="evidence record key")
        if type(value) is not dict:
            raise ValueError("Corrupt checkpoint evidence record")
        record_id = _checkpoint_text(
            value.get("evidence_id"),
            name="evidence record id",
        )
        if record_id != evidence_id:
            raise ValueError("Corrupt checkpoint evidence record identity")
        records[evidence_id] = value
    if not set(records).issubset(expected_ids):
        raise ValueError("Corrupt checkpoint evidence records do not match IDs")

    observed: dict[str, dict] = {}
    if evidence_path.exists():
        try:
            for line in evidence_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    raise ValueError("Corrupt learning evidence")
                row = json.loads(line)
                if type(row) is not dict:
                    raise ValueError("Corrupt learning evidence")
                evidence_id = _checkpoint_text(
                    row.get("evidence_id"),
                    name="learning evidence id",
                )
                if evidence_id in observed:
                    raise ValueError("Duplicate learning evidence ID")
                observed[evidence_id] = row
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError("Corrupt learning evidence") from error

    if not set(observed).issubset(expected_ids):
        raise ValueError("Learning evidence conflicts with checkpoint")
    for evidence_id, row in observed.items():
        checkpoint_row = records.get(evidence_id)
        if checkpoint_row is not None and checkpoint_row != row:
            raise ValueError("Learning evidence conflicts with checkpoint")
        records[evidence_id] = row

    return expected_ids, records


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


def _require_restored_fill_intent(root: Path, fill: Fill) -> None:
    """Cross-bind a recovered fill to the durable pre-execution intent."""

    intent_path = root / "order-intents" / f"{fill.client_order_id}.json"
    try:
        persisted = json.loads(intent_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(
            "Corrupt checkpoint fill: durable order intent is unavailable"
        ) from error
    expected = {
        "client_order_id": fill.client_order_id,
        "symbol": fill.symbol,
        "side": fill.side,
        "quantity": str(fill.quantity),
        "price": str(fill.price),
    }
    if type(persisted) is not dict or persisted != expected:
        raise ValueError(
            "Corrupt checkpoint fill: durable order intent does not match fill"
        )


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
        if row is None or type(row) is not dict:
            raise ValueError("Fill and economic ledger do not reconcile")
        posting_fill_id = _checkpoint_text(
            row.get("fill_id"), name="posting fill_id"
        )
        posting_fee = _checkpoint_decimal(row.get("fee"), name="posting fee")
        if (
            posting_fill_id != fill.fill_id
            or posting_fee != fill.fee
            or _checkpoint_decimal(
                row.get("position_delta"), name="position_delta"
            ) != signed
            or _money(
                _checkpoint_decimal(row.get("cash_delta"), name="cash_delta")
            ) != expected_cash
        ):
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

    if type(symbol) is not str or not symbol or symbol != str.strip(symbol):
        raise ValueError("A canonical simulated symbol is required")
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
    configuration = _run_configuration(
        symbol=symbol,
        initial_cash=starting_cash,
        order_quantity=quantity,
        max_abs_position=position_limit,
        max_notional=notional_limit,
        fee_rate=rate,
    )
    configuration_digest = _require_run_configuration(root, configuration)
    state, resumed = handle_restart_recovery(state_dir, starting_cash)
    if resumed:
        checkpoint_configuration_digest = _checkpoint_text(
            state.get("configuration_digest"),
            name="configuration digest",
        )
        if checkpoint_configuration_digest != configuration_digest:
            raise ValueError(
                "Checkpoint configuration identity does not match run configuration"
            )
        stored_symbol = _checkpoint_text(
            state.get("symbol"), name="run symbol"
        )
        if stored_symbol != symbol:
            raise ValueError("Checkpoint belongs to another symbol")
        stored_initial_cash = _checkpoint_decimal(
            state.get("initial_cash"),
            name="initial_cash",
        )
        if stored_initial_cash != starting_cash:
            raise ValueError(
                "Checkpoint initial cash conflicts with run configuration"
            )
    evidence_ids, evidence_records = _restore_evidence_graph(
        state,
        evidence_path,
    )
    postings = state.get("postings", [])
    fills = state.get("fills", {})
    if type(postings) is not list or type(fills) is not dict:
        raise ValueError("Corrupt checkpoint ledger or fills")
    ledger = EconomicLedger(
        _checkpoint_decimal(state["initial_cash"], name="initial_cash"),
        list(postings),
    )
    restored_fills = {
        key: _restore_checkpoint_fill(
            key,
            value,
            expected_symbol=symbol,
        )
        for key, value in dict.items(fills)
    }
    for restored_fill in restored_fills.values():
        _require_restored_fill_intent(root, restored_fill)
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
    evidence_id = "evidence-" + _stable_hash({
        "symbol": symbol, "input": [str(x) for x in normalized],
        "intent": intent.client_order_id if intent else None,
    })[:20]
    # "already_filled" is a replay-only operational observation. The durable
    # evidence describes the original causal admission that created the fill,
    # so replay must reconstruct the same semantic risk outcome instead of
    # permitting an arbitrary persisted label to bypass equality checks.
    evidence_risk_outcome = (
        "admitted" if risk_reason == "already_filled" else risk_reason
    )
    fresh_evidence = {
        "schema_version": 1,
        "evidence_id": evidence_id,
        "input_hash": _stable_hash([str(item) for item in normalized]),
        "decision": decision.side,
        "decision_reason": decision.reason,
        "risk_outcome": evidence_risk_outcome,
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
    # Replay may legitimately report the operational risk outcome as
    # "already_filled", while the first durable evidence keeps the original
    # admission result. Everything that describes the decision or financial
    # result must still match the freshly reconstructed durable state.
    replay_bound_fields = (
        "schema_version",
        "evidence_id",
        "input_hash",
        "decision",
        "decision_reason",
        "risk_outcome",
        "order_id",
        "fill_id",
        "cash",
        "position",
        "equity",
        "reconciled",
    )
    if any(
        evidence.get(field) != fresh_evidence[field]
        for field in replay_bound_fields
    ):
        raise ValueError("Checkpoint evidence conflicts with this episode")
    evidence_ids.add(evidence["evidence_id"])
    evidence_records[evidence_id] = evidence
    checkpoint = {
        "schema_version": configuration["checkpoint_schema_version"],
        "configuration_digest": configuration_digest,
        "symbol": symbol,
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
    episodes: Iterable[Iterable[float | str | Decimal]],
    state_dir: str | Path,
    **kwargs,
) -> list[RunResult]:
    """Run a deterministic sequence through the same durable portfolio."""
    return [run_vertical_slice(episode, state_dir, **kwargs) for episode in episodes]
