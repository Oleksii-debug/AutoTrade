"""Deterministic zero-model proposal path for research and simulation.

The baseline is intentionally simple and makes no profitability claim. It only
consumes observations evidenced as available by the decision cutoff.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
import json
import re
from typing import Iterable
from uuid import UUID

from autotrade_numeric import (
    ExactDecimalError,
    as_fraction,
    bounded_fraction,
    is_exact_decimal_multiple,
    round_fraction_to_quantum,
    parse_bounded_exact_decimal,
    parse_bounded_json_integer_token,
)


def _decimal(value, *, name: str) -> Decimal:
    # Preserve the exact built-in fence before invoking the shared pre-construction
    # resource validator: virtual subclass methods cannot influence authority.
    if type(value) not in (Decimal, str, int):
        raise TypeError(f"{name} must use exact built-in Decimal, string or integer input")
    try:
        result = parse_bounded_exact_decimal(value)
    except ExactDecimalError as error:
        raise ValueError(f"{name} must be a finite bounded decimal") from error
    # Downstream arithmetic has its own independent rational resource budget.
    as_fraction(result)
    return result


def _time(value: datetime, *, name: str) -> datetime:
    if type(value) is not datetime or value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")
    # Even an exact datetime can contain a caller-supplied tzinfo subclass.
    # Seal the nested graph before astimezone() can invoke virtual offsets.
    if type(value.tzinfo) is not timezone:
        raise ValueError(f"{name} must use a built-in timezone")
    return datetime.astimezone(value, timezone.utc)


def _text(value: str, *, name: str) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{name} is required")
    return value.strip()


_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")


def _digest(value: str, *, name: str) -> str:
    text = _text(value, name=name)
    if _SHA256.fullmatch(text) is None:
        raise ValueError(f"{name} must be canonical sha256:<64 lowercase hex>")
    return text


def _canonical_json(value) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _read_strict_strategy_json(value: str) -> object:
    # Shared parser for restored strategy state and its registered run receipt.
    # Duplicate evidence keys, exotic numbers and costly integers cannot
    # acquire authority through Python's permissive default JSON decoder.
    def unique_keys(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("strategy evidence contains duplicate JSON keys")
            result[key] = item
        return result

    def deny_noninteger_numeric(_token):
        raise ValueError("strategy evidence contains forbidden JSON numeric token")

    if type(value) is not str:
        raise TypeError("strategy JSON must be an exact string")
    # Normalize recursive decoder exhaustion at the existing evidence
    # boundary without retaining its raw parser exception/context.
    structural_failure = False
    try:
        return json.loads(
            value,
            object_pairs_hook=unique_keys,
            parse_int=parse_bounded_json_integer_token,
            parse_float=deny_noninteger_numeric,
            parse_constant=deny_noninteger_numeric,
        )
    except RecursionError:
        structural_failure = True
    if structural_failure:
        raise ValueError("strategy evidence exceeds structural JSON limits")


@dataclass(frozen=True)
class StrategyDescriptor:
    """Immutable identity for one deterministic strategy configuration."""

    strategy_id: str
    version: int
    family: str
    feature_schema: str
    market_requirements: tuple[str, ...]
    minimum_history: int
    horizon_seconds: int
    decision_schedule: str
    proposal_semantics: str
    parameter_bounds: tuple[tuple[str, str, str], ...]
    resource_profile: str
    supported_regimes: tuple[str, ...]
    source_license: str
    evaluation_protocol_sha256: str
    artifact_sha256: str

    def __post_init__(self) -> None:
        for name in (
            "strategy_id",
            "family",
            "feature_schema",
            "decision_schedule",
            "proposal_semantics",
            "resource_profile",
            "source_license",
        ):
            object.__setattr__(self, name, _text(getattr(self, name), name=name))
        for name in ("version", "minimum_history", "horizon_seconds"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        for name in ("market_requirements", "supported_regimes"):
            value = getattr(self, name)
            if type(value) is not tuple:
                raise ValueError(f"{name} must be a tuple")
            normalized = tuple(_text(item, name=f"{name} item") for item in value)
            if not normalized or len(set(normalized)) != len(normalized):
                raise ValueError(f"{name} must contain unique non-empty values")
            object.__setattr__(self, name, normalized)
        if type(self.parameter_bounds) is not tuple or not self.parameter_bounds:
            raise ValueError("parameter_bounds must be a non-empty tuple")
        normalized_bounds = []
        names = set()
        for item in self.parameter_bounds:
            if type(item) is not tuple or len(item) != 3:
                raise ValueError("parameter bound must be (name, minimum, maximum)")
            parameter = _text(item[0], name="parameter name")
            minimum = _decimal(item[1], name=f"{parameter} minimum")
            maximum = _decimal(item[2], name=f"{parameter} maximum")
            if parameter in names:
                raise ValueError("parameter_bounds contains duplicate names")
            if minimum > maximum:
                raise ValueError("parameter minimum cannot exceed maximum")
            names.add(parameter)
            normalized_bounds.append((parameter, str(minimum), str(maximum)))
        object.__setattr__(self, "parameter_bounds", tuple(normalized_bounds))
        object.__setattr__(
            self,
            "evaluation_protocol_sha256",
            _digest(
                self.evaluation_protocol_sha256,
                name="evaluation_protocol_sha256",
            ),
        )
        object.__setattr__(
            self,
            "artifact_sha256",
            _digest(self.artifact_sha256, name="artifact_sha256"),
        )

    def canonical_document(self) -> dict[str, object]:
        return {
            "strategy_id": self.strategy_id,
            "version": self.version,
            "family": self.family,
            "feature_schema": self.feature_schema,
            "market_requirements": list(self.market_requirements),
            "minimum_history": self.minimum_history,
            "horizon_seconds": self.horizon_seconds,
            "decision_schedule": self.decision_schedule,
            "proposal_semantics": self.proposal_semantics,
            "parameter_bounds": [
                {"name": name, "minimum": minimum, "maximum": maximum}
                for name, minimum, maximum in self.parameter_bounds
            ],
            "resource_profile": self.resource_profile,
            "supported_regimes": list(self.supported_regimes),
            "source_license": self.source_license,
            "evaluation_protocol_sha256": self.evaluation_protocol_sha256,
            "artifact_sha256": self.artifact_sha256,
        }

    @property
    def fingerprint(self) -> str:
        payload = _canonical_json(self.canonical_document()).encode("utf-8")
        return "sha256:" + sha256(payload).hexdigest()



def _strategy_descriptor_from_document(payload: object) -> StrategyDescriptor:
    """Decode one exact canonical descriptor document without coercive containers."""

    expected = {
        "strategy_id",
        "version",
        "family",
        "feature_schema",
        "market_requirements",
        "minimum_history",
        "horizon_seconds",
        "decision_schedule",
        "proposal_semantics",
        "parameter_bounds",
        "resource_profile",
        "supported_regimes",
        "source_license",
        "evaluation_protocol_sha256",
        "artifact_sha256",
    }
    if type(payload) is not dict or set(payload) != expected:
        raise ValueError("strategy descriptor snapshot is invalid")
    if type(payload["market_requirements"]) is not list:
        raise ValueError("strategy descriptor market_requirements must be an array")
    if type(payload["supported_regimes"]) is not list:
        raise ValueError("strategy descriptor supported_regimes must be an array")
    if type(payload["parameter_bounds"]) is not list:
        raise ValueError("strategy descriptor parameter_bounds must be an array")
    bounds: list[tuple[str, str, str]] = []
    for item in payload["parameter_bounds"]:
        if type(item) is not dict or set(item) != {"name", "minimum", "maximum"}:
            raise ValueError("strategy descriptor parameter bound is invalid")
        bounds.append((item["name"], item["minimum"], item["maximum"]))
    try:
        return StrategyDescriptor(
            strategy_id=payload["strategy_id"],
            version=payload["version"],
            family=payload["family"],
            feature_schema=payload["feature_schema"],
            market_requirements=tuple(payload["market_requirements"]),
            minimum_history=payload["minimum_history"],
            horizon_seconds=payload["horizon_seconds"],
            decision_schedule=payload["decision_schedule"],
            proposal_semantics=payload["proposal_semantics"],
            parameter_bounds=tuple(bounds),
            resource_profile=payload["resource_profile"],
            supported_regimes=tuple(payload["supported_regimes"]),
            source_license=payload["source_license"],
            evaluation_protocol_sha256=payload["evaluation_protocol_sha256"],
            artifact_sha256=payload["artifact_sha256"],
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("strategy descriptor snapshot is invalid") from error


def _readmit_strategy_descriptor(value: StrategyDescriptor) -> StrategyDescriptor:
    """Detach and revalidate the exact registered strategy descriptor."""

    if type(value) is not StrategyDescriptor:
        raise TypeError("descriptor must be canonical StrategyDescriptor")
    return StrategyDescriptor(
        strategy_id=value.strategy_id,
        version=value.version,
        family=value.family,
        feature_schema=value.feature_schema,
        market_requirements=value.market_requirements,
        minimum_history=value.minimum_history,
        horizon_seconds=value.horizon_seconds,
        decision_schedule=value.decision_schedule,
        proposal_semantics=value.proposal_semantics,
        parameter_bounds=value.parameter_bounds,
        resource_profile=value.resource_profile,
        supported_regimes=value.supported_regimes,
        source_license=value.source_license,
        evaluation_protocol_sha256=value.evaluation_protocol_sha256,
        artifact_sha256=value.artifact_sha256,
    )


@dataclass(frozen=True)
class CausalObservation:
    event_id: str
    symbol: str
    available_at: datetime
    price: Decimal

    def __post_init__(self) -> None:
        event_id = _text(self.event_id, name="event_id")
        symbol = _text(self.symbol, name="symbol")
        available_at = _time(self.available_at, name="available_at")
        price = _decimal(self.price, name="price")
        if price <= 0:
            raise ValueError("price must be positive")
        object.__setattr__(self, "event_id", event_id)
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "available_at", available_at)
        object.__setattr__(self, "price", price)

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


def _readmit_causal_observation(value: CausalObservation) -> CausalObservation:
    """Reconstruct one exact observation before an authority-sensitive use."""

    if type(value) is not CausalObservation:
        raise TypeError("observation must be CausalObservation")
    return CausalObservation(
        event_id=value.event_id,
        symbol=value.symbol,
        available_at=value.available_at,
        price=value.price,
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
    information_cutoff: datetime | None = None
    horizon_seconds: int | None = None
    expiry: datetime | None = None
    strategy_version: str | None = None
    strategy_fingerprint: str | None = None
    strategy_configuration_fingerprint: str | None = None

    def __post_init__(self) -> None:
        symbol = _text(self.symbol, name="symbol")
        action = _text(self.action, name="action").upper()
        if action not in {"BUY", "SELL", "HOLD"}:
            raise ValueError("unsupported deterministic proposal action")
        quantity = _decimal(self.quantity, name="quantity")
        if quantity < 0:
            raise ValueError("quantity must be non-negative")
        if action == "HOLD" and quantity != 0:
            raise ValueError("HOLD proposal quantity must be zero")
        if action in {"BUY", "SELL"} and quantity <= 0:
            raise ValueError("BUY/SELL proposal quantity must be positive")
        decision_time = _time(self.decision_time, name="decision_time")
        if type(self.evidence_event_ids) is not tuple:
            raise ValueError("evidence_event_ids must be a tuple")
        evidence = tuple(
            _text(value, name="evidence_event_id")
            for value in self.evidence_event_ids
        )
        if len(set(evidence)) != len(evidence):
            raise ValueError("evidence_event_ids contains duplicates")
        if type(self.model_calls) is not int:
            raise ValueError("model_calls must be integer zero")
        if self.model_calls != 0:
            raise ValueError("deterministic proposal cannot contain model calls")
        edge = _text(self.economic_edge_claim, name="economic_edge_claim").upper()
        if edge != "UNPROVEN":
            raise ValueError(
                "deterministic proposal cannot claim proven economic edge"
            )
        reason = _text(self.reason, name="reason")
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "action", action)
        object.__setattr__(self, "quantity", quantity)
        object.__setattr__(self, "decision_time", decision_time)
        object.__setattr__(self, "evidence_event_ids", evidence)
        object.__setattr__(self, "economic_edge_claim", edge)
        object.__setattr__(self, "reason", reason)
        if self.information_cutoff is not None:
            object.__setattr__(
                self,
                "information_cutoff",
                _time(self.information_cutoff, name="information_cutoff"),
            )
        if self.expiry is not None:
            object.__setattr__(self, "expiry", _time(self.expiry, name="expiry"))
        if self.horizon_seconds is not None:
            if (
                type(self.horizon_seconds) is not int
                or self.horizon_seconds <= 0
            ):
                raise ValueError("horizon_seconds must be a positive integer")
            if self.expiry is None or self.information_cutoff is None:
                raise ValueError(
                    "horizon metadata requires information_cutoff and expiry"
                )
            if self.information_cutoff > self.decision_time:
                raise ValueError("information_cutoff cannot be after decision_time")
            expected_expiry = self.information_cutoff + timedelta(
                seconds=self.horizon_seconds
            )
            if self.expiry != expected_expiry:
                raise ValueError(
                    "expiry must equal information_cutoff plus horizon_seconds"
                )
        if self.strategy_fingerprint is not None:
            object.__setattr__(
                self,
                "strategy_fingerprint",
                _digest(
                    self.strategy_fingerprint,
                    name="strategy_fingerprint",
                ),
            )
        if self.strategy_configuration_fingerprint is not None:
            object.__setattr__(
                self,
                "strategy_configuration_fingerprint",
                _digest(
                    self.strategy_configuration_fingerprint,
                    name="strategy_configuration_fingerprint",
                ),
            )
        identity = (
            self.strategy_version,
            self.strategy_fingerprint,
            self.strategy_configuration_fingerprint,
        )
        if any(value is not None for value in identity):
            if not all(value is not None for value in identity):
                raise ValueError(
                    "registered strategy identity must include version, descriptor fingerprint "
                    "and configuration fingerprint"
                )
            object.__setattr__(
                self,
                "strategy_version",
                _text(self.strategy_version, name="strategy_version"),
            )
            if (
                self.information_cutoff is None
                or self.horizon_seconds is None
                or self.expiry is None
            ):
                raise ValueError(
                    "registered strategy identity requires cutoff, horizon and expiry"
                )


def _readmit_deterministic_proposal(
    value: DeterministicProposal,
) -> DeterministicProposal:
    """Re-run proposal invariants and detach caller-owned object identity."""

    if type(value) is not DeterministicProposal:
        raise TypeError("proposal must be DeterministicProposal")
    return DeterministicProposal(
        symbol=value.symbol,
        action=value.action,
        quantity=value.quantity,
        decision_time=value.decision_time,
        evidence_event_ids=value.evidence_event_ids,
        model_calls=value.model_calls,
        economic_edge_claim=value.economic_edge_claim,
        reason=value.reason,
        information_cutoff=value.information_cutoff,
        horizon_seconds=value.horizon_seconds,
        expiry=value.expiry,
        strategy_version=value.strategy_version,
        strategy_fingerprint=value.strategy_fingerprint,
        strategy_configuration_fingerprint=value.strategy_configuration_fingerprint,
    )


def _parse_utc_text(value: str, *, name: str) -> datetime:
    text = _text(value, name=name)
    if not text.endswith("Z"):
        raise ValueError(f"{name} must be canonical UTC text ending in Z")
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as error:
        raise ValueError(f"{name} must be canonical UTC text") from error
    return _time(parsed, name=name)


def _proposal_document(proposal: DeterministicProposal) -> dict[str, object]:
    proposal = _readmit_deterministic_proposal(proposal)
    return {
        "symbol": proposal.symbol,
        "action": proposal.action,
        "quantity": str(proposal.quantity),
        "decision_time": _utc_text(proposal.decision_time),
        "evidence_event_ids": list(proposal.evidence_event_ids),
        "model_calls": proposal.model_calls,
        "economic_edge_claim": proposal.economic_edge_claim,
        "reason": proposal.reason,
        "information_cutoff": (
            None
            if proposal.information_cutoff is None
            else _utc_text(proposal.information_cutoff)
        ),
        "horizon_seconds": proposal.horizon_seconds,
        "expiry": None if proposal.expiry is None else _utc_text(proposal.expiry),
        "strategy_version": proposal.strategy_version,
        "strategy_fingerprint": proposal.strategy_fingerprint,
        "strategy_configuration_fingerprint": (
            proposal.strategy_configuration_fingerprint
        ),
    }


def _proposal_from_document(payload: object) -> DeterministicProposal:
    expected = {
        "symbol", "action", "quantity", "decision_time", "evidence_event_ids",
        "model_calls", "economic_edge_claim", "reason", "information_cutoff",
        "horizon_seconds", "expiry", "strategy_version", "strategy_fingerprint",
        "strategy_configuration_fingerprint",
    }
    if not isinstance(payload, dict) or set(payload) != expected:
        raise ValueError("registered run proposal document is invalid")
    evidence = payload["evidence_event_ids"]
    if not isinstance(evidence, list):
        raise ValueError("registered run proposal evidence must be an array")
    cutoff = payload["information_cutoff"]
    expiry = payload["expiry"]
    return DeterministicProposal(
        symbol=payload["symbol"],
        action=payload["action"],
        quantity=payload["quantity"],
        decision_time=_parse_utc_text(payload["decision_time"], name="decision_time"),
        evidence_event_ids=tuple(evidence),
        model_calls=payload["model_calls"],
        economic_edge_claim=payload["economic_edge_claim"],
        reason=payload["reason"],
        information_cutoff=(
            None if cutoff is None
            else _parse_utc_text(cutoff, name="information_cutoff")
        ),
        horizon_seconds=payload["horizon_seconds"],
        expiry=None if expiry is None else _parse_utc_text(expiry, name="expiry"),
        strategy_version=payload["strategy_version"],
        strategy_fingerprint=payload["strategy_fingerprint"],
        strategy_configuration_fingerprint=payload[
            "strategy_configuration_fingerprint"
        ],
    )


@dataclass(frozen=True)
class RegisteredStrategyRunReceipt:
    """Replay-verifiable proof of one registered deterministic strategy run."""

    strategy_snapshot: str
    instrument_version: str
    symbol: str
    decision_time: datetime
    observations: tuple[CausalObservation, ...]
    proposal: DeterministicProposal

    def __post_init__(self) -> None:
        if type(self.strategy_snapshot) is not str or not self.strategy_snapshot:
            raise ValueError("strategy_snapshot is required")
        strategy = _restore_threshold_strategy_snapshot(self.strategy_snapshot)
        if strategy.snapshot() != self.strategy_snapshot:
            raise ValueError("strategy_snapshot must use canonical snapshot bytes")
        if strategy.descriptor is None:
            raise ValueError("registered run requires a registered strategy descriptor")
        if strategy._observations_by_id or any(strategy._history.values()):
            raise ValueError("registered run strategy snapshot must be pristine")
        instrument = _text(self.instrument_version, name="instrument_version")
        symbol = _text(self.symbol, name="symbol")
        decision = _time(self.decision_time, name="decision_time")
        if type(self.observations) is not tuple:
            raise ValueError("registered run observations must be a tuple")
        observations = tuple(
            _readmit_causal_observation(observation)
            for observation in self.observations
        )
        for observation in observations:
            if observation.available_at > decision:
                raise ValueError(
                    "registered run observation is not available at decision_time"
                )
        event_ids = tuple(item.event_id for item in observations)
        if len(set(event_ids)) != len(event_ids):
            raise ValueError("registered run observations contain duplicate event_id")
        proposal = _readmit_deterministic_proposal(self.proposal)
        descriptor = strategy.descriptor
        if proposal.symbol != symbol or proposal.decision_time != decision:
            raise ValueError("registered run proposal identity does not match receipt")
        if proposal.strategy_fingerprint != descriptor.fingerprint:
            raise ValueError(
                "registered run descriptor fingerprint does not match proposal"
            )
        if (
            proposal.strategy_configuration_fingerprint
            != strategy.configuration_fingerprint
        ):
            raise ValueError(
                "registered run configuration fingerprint does not match proposal"
            )
        if proposal.strategy_version != f"{descriptor.strategy_id}@{descriptor.version}":
            raise ValueError("registered run strategy version does not match proposal")
        if proposal.information_cutoff != decision:
            raise ValueError(
                "registered run information_cutoff does not match decision_time"
            )
        if proposal.horizon_seconds != descriptor.horizon_seconds:
            raise ValueError("registered run horizon does not match descriptor")
        if proposal.expiry != decision + timedelta(seconds=descriptor.horizon_seconds):
            raise ValueError("registered run expiry does not match descriptor horizon")
        object.__setattr__(self, "instrument_version", instrument)
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "decision_time", decision)
        object.__setattr__(self, "observations", observations)
        object.__setattr__(self, "proposal", proposal)

    def canonical_document(self) -> dict[str, object]:
        return {
            "schema_version": "1.0.0",
            "strategy_snapshot": self.strategy_snapshot,
            "instrument_version": self.instrument_version,
            "symbol": self.symbol,
            "decision_time": _utc_text(self.decision_time),
            "observations": [
                {
                    "event_id": item.event_id,
                    "symbol": item.symbol,
                    "available_at": _utc_text(item.available_at),
                    "price": str(item.price),
                }
                for item in self.observations
            ],
            "proposal": _proposal_document(self.proposal),
        }

    @property
    def fingerprint(self) -> str:
        return "sha256:" + sha256(
            _canonical_json(self.canonical_document()).encode("utf-8")
        ).hexdigest()

    def to_json(self) -> str:
        return _canonical_json(self.canonical_document())

    @classmethod
    def from_json(cls, value: str) -> "RegisteredStrategyRunReceipt":
        try:
            payload = _read_strict_strategy_json(value)
        except (TypeError, ValueError) as error:
            raise ValueError("registered run receipt is invalid JSON") from error
        expected = {
            "schema_version", "strategy_snapshot", "instrument_version", "symbol",
            "decision_time", "observations", "proposal",
        }
        if (
            not isinstance(payload, dict)
            or set(payload) != expected
            or payload.get("schema_version") != "1.0.0"
            or not isinstance(payload.get("observations"), list)
        ):
            raise ValueError("registered run receipt structure is invalid")
        observations = []
        for item in payload["observations"]:
            if not isinstance(item, dict) or set(item) != {
                "event_id", "symbol", "available_at", "price"
            }:
                raise ValueError("registered run observation document is invalid")
            observations.append(
                CausalObservation.create(
                    event_id=item["event_id"],
                    symbol=item["symbol"],
                    available_at=_parse_utc_text(
                        item["available_at"], name="observation available_at"
                    ),
                    price=item["price"],
                )
            )
        return cls(
            strategy_snapshot=payload["strategy_snapshot"],
            instrument_version=payload["instrument_version"],
            symbol=payload["symbol"],
            decision_time=_parse_utc_text(
                payload["decision_time"], name="decision_time"
            ),
            observations=tuple(observations),
            proposal=_proposal_from_document(payload["proposal"]),
        )


def _readmit_registered_strategy_run_receipt(
    value: RegisteredStrategyRunReceipt,
) -> RegisteredStrategyRunReceipt:
    """Detach a receipt and recursively re-run its exact value invariants."""

    if type(value) is not RegisteredStrategyRunReceipt:
        raise TypeError("receipt must be RegisteredStrategyRunReceipt")
    return RegisteredStrategyRunReceipt(
        strategy_snapshot=value.strategy_snapshot,
        instrument_version=value.instrument_version,
        symbol=value.symbol,
        decision_time=value.decision_time,
        observations=tuple(
            _readmit_causal_observation(observation)
            for observation in value.observations
        ),
        proposal=_readmit_deterministic_proposal(value.proposal),
    )


def verify_registered_strategy_run(
    proposal: DeterministicProposal,
    receipt: RegisteredStrategyRunReceipt,
) -> str:
    """Replay one receipt from pristine state and return its verified digest."""

    proposal = _readmit_deterministic_proposal(proposal)
    receipt = _readmit_registered_strategy_run_receipt(receipt)
    if receipt.proposal != proposal:
        raise ValueError(
            "registered run receipt proposal does not match supplied proposal"
        )
    strategy = _restore_threshold_strategy_snapshot(receipt.strategy_snapshot)
    replayed = run_baseline(
        strategy,
        receipt.observations,
        decision_time=receipt.decision_time,
        symbol=receipt.symbol,
    )
    if replayed != proposal:
        raise ValueError("registered run receipt does not replay to supplied proposal")
    return receipt.fingerprint


@dataclass(frozen=True)
class StrategyEconomicsBinding:
    """Frozen ex-ante economics/capacity evidence for one deterministic proposal cut.

    This is research evidence only. It cannot authorize execution, change risk
    limits, or use realized post-arrival liquidity. Every evidence timestamp must
    already be available at the proposal information cutoff.
    """

    strategy_fingerprint: str
    strategy_configuration_fingerprint: str
    instrument_version: str
    information_cutoff: datetime
    decision_time: datetime
    horizon_seconds: int
    expiry: datetime
    available_at: datetime
    input_manifest_refs: tuple[str, ...]
    gross_return_distribution_sha256: str
    after_cost_return_distribution_sha256: str
    after_cost_lower_bound: Decimal
    execution_model_fingerprint: str
    execution_calibration_sha256: str
    execution_fidelity: str
    capacity_assessment_sha256: str
    max_feasible_quantity: Decimal
    lot_size: Decimal
    registered_run_receipt_sha256: str | None = None
    required_evidence_dimensions: tuple[str, ...] = ()
    dimension_evidence: tuple[tuple[str, str], ...] = ()
    status: str = "INCONCLUSIVE"

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "strategy_fingerprint",
            _digest(self.strategy_fingerprint, name="strategy_fingerprint"),
        )
        object.__setattr__(
            self,
            "strategy_configuration_fingerprint",
            _digest(
                self.strategy_configuration_fingerprint,
                name="strategy_configuration_fingerprint",
            ),
        )
        object.__setattr__(
            self,
            "instrument_version",
            _text(self.instrument_version, name="instrument_version"),
        )
        cutoff = _time(self.information_cutoff, name="information_cutoff")
        decision = _time(self.decision_time, name="decision_time")
        expiry = _time(self.expiry, name="expiry")
        available = _time(self.available_at, name="available_at")
        if cutoff > decision:
            raise ValueError("information_cutoff cannot be after decision_time")
        if available > cutoff:
            raise ValueError(
                "economics evidence is not causally available at information_cutoff"
            )
        if (
            type(self.horizon_seconds) is not int
            or self.horizon_seconds <= 0
        ):
            raise ValueError("horizon_seconds must be a positive integer")
        if expiry != cutoff + timedelta(seconds=self.horizon_seconds):
            raise ValueError(
                "economics expiry must equal information_cutoff plus horizon_seconds"
            )
        object.__setattr__(self, "information_cutoff", cutoff)
        object.__setattr__(self, "decision_time", decision)
        object.__setattr__(self, "expiry", expiry)
        object.__setattr__(self, "available_at", available)

        if type(self.input_manifest_refs) is not tuple:
            raise ValueError("input_manifest_refs must be a tuple")
        manifests = tuple(
            _digest(value, name="input_manifest_ref")
            for value in self.input_manifest_refs
        )
        if not manifests or len(set(manifests)) != len(manifests):
            raise ValueError(
                "input_manifest_refs must contain unique immutable evidence"
            )
        object.__setattr__(self, "input_manifest_refs", manifests)
        for name in (
            "gross_return_distribution_sha256",
            "after_cost_return_distribution_sha256",
            "execution_model_fingerprint",
            "execution_calibration_sha256",
            "capacity_assessment_sha256",
        ):
            object.__setattr__(
                self,
                name,
                _digest(getattr(self, name), name=name),
            )
        object.__setattr__(
            self,
            "execution_fidelity",
            _text(self.execution_fidelity, name="execution_fidelity").upper(),
        )
        if self.registered_run_receipt_sha256 is not None:
            object.__setattr__(
                self,
                "registered_run_receipt_sha256",
                _digest(
                    self.registered_run_receipt_sha256,
                    name="registered_run_receipt_sha256",
                ),
            )

        lower_bound = _decimal(
            self.after_cost_lower_bound,
            name="after_cost_lower_bound",
        )
        capacity = _decimal(
            self.max_feasible_quantity,
            name="max_feasible_quantity",
        )
        lot_size = _decimal(self.lot_size, name="lot_size")
        if capacity < 0:
            raise ValueError("max_feasible_quantity cannot be negative")
        if lot_size <= 0:
            raise ValueError("lot_size must be positive")
        object.__setattr__(self, "after_cost_lower_bound", lower_bound)
        object.__setattr__(self, "max_feasible_quantity", capacity)
        object.__setattr__(self, "lot_size", lot_size)

        if type(self.required_evidence_dimensions) is not tuple:
            raise ValueError("required_evidence_dimensions must be a tuple")
        required = tuple(
            _text(value, name="required_evidence_dimension").upper()
            for value in self.required_evidence_dimensions
        )
        if len(set(required)) != len(required):
            raise ValueError("required_evidence_dimensions contains duplicates")
        object.__setattr__(self, "required_evidence_dimensions", required)

        if type(self.dimension_evidence) is not tuple:
            raise ValueError("dimension_evidence must be a tuple")
        evidence: list[tuple[str, str]] = []
        evidence_names: set[str] = set()
        for item in self.dimension_evidence:
            if type(item) is not tuple or len(item) != 2:
                raise ValueError(
                    "dimension_evidence entries must be (dimension, sha256)"
                )
            dimension = _text(item[0], name="dimension_evidence dimension").upper()
            if dimension in evidence_names:
                raise ValueError("dimension_evidence contains duplicate dimensions")
            evidence_names.add(dimension)
            evidence.append(
                (
                    dimension,
                    _digest(item[1], name=f"{dimension} evidence"),
                )
            )
        object.__setattr__(
            self,
            "dimension_evidence",
            tuple(sorted(evidence)),
        )

        status = _text(self.status, name="status").upper()
        if status not in {"QUALIFIED", "INCONCLUSIVE", "NO_TRADE"}:
            raise ValueError("unsupported economics binding status")
        missing = set(required) - evidence_names
        if status == "QUALIFIED" and missing:
            raise ValueError(
                "QUALIFIED economics binding is missing required evidence dimensions"
            )
        if status == "QUALIFIED" and lower_bound <= 0:
            raise ValueError(
                "QUALIFIED economics binding requires positive after-cost lower bound"
            )
        object.__setattr__(self, "status", status)

    @property
    def missing_dimensions(self) -> tuple[str, ...]:
        evidenced = {dimension for dimension, _digest_value in self.dimension_evidence}
        return tuple(
            sorted(set(self.required_evidence_dimensions) - evidenced)
        )

    def canonical_document(self) -> dict[str, object]:
        return {
            "schema_version": "1.0.0",
            "strategy_fingerprint": self.strategy_fingerprint,
            "strategy_configuration_fingerprint": (
                self.strategy_configuration_fingerprint
            ),
            "instrument_version": self.instrument_version,
            "information_cutoff": _utc_text(self.information_cutoff),
            "decision_time": _utc_text(self.decision_time),
            "horizon_seconds": self.horizon_seconds,
            "expiry": _utc_text(self.expiry),
            "available_at": _utc_text(self.available_at),
            "input_manifest_refs": list(self.input_manifest_refs),
            "gross_return_distribution_sha256": (
                self.gross_return_distribution_sha256
            ),
            "after_cost_return_distribution_sha256": (
                self.after_cost_return_distribution_sha256
            ),
            "after_cost_lower_bound": str(self.after_cost_lower_bound),
            "execution_model_fingerprint": self.execution_model_fingerprint,
            "execution_calibration_sha256": self.execution_calibration_sha256,
            "execution_fidelity": self.execution_fidelity,
            "capacity_assessment_sha256": self.capacity_assessment_sha256,
            "max_feasible_quantity": str(self.max_feasible_quantity),
            "lot_size": str(self.lot_size),
            "registered_run_receipt_sha256": self.registered_run_receipt_sha256,
            "required_evidence_dimensions": list(
                self.required_evidence_dimensions
            ),
            "dimension_evidence": [
                {"dimension": dimension, "sha256": digest}
                for dimension, digest in self.dimension_evidence
            ],
            "missing_dimensions": list(self.missing_dimensions),
            "status": self.status,
        }

    @property
    def fingerprint(self) -> str:
        return "sha256:" + sha256(
            _canonical_json(self.canonical_document()).encode("utf-8")
        ).hexdigest()


def _readmit_strategy_economics_binding(
    value: StrategyEconomicsBinding,
) -> StrategyEconomicsBinding:
    """Reconstruct public research economics before it can affect exposure."""

    if type(value) is not StrategyEconomicsBinding:
        raise TypeError("economics must be StrategyEconomicsBinding")
    return StrategyEconomicsBinding(
        strategy_fingerprint=value.strategy_fingerprint,
        strategy_configuration_fingerprint=value.strategy_configuration_fingerprint,
        instrument_version=value.instrument_version,
        information_cutoff=value.information_cutoff,
        decision_time=value.decision_time,
        horizon_seconds=value.horizon_seconds,
        expiry=value.expiry,
        available_at=value.available_at,
        input_manifest_refs=value.input_manifest_refs,
        gross_return_distribution_sha256=value.gross_return_distribution_sha256,
        after_cost_return_distribution_sha256=value.after_cost_return_distribution_sha256,
        after_cost_lower_bound=value.after_cost_lower_bound,
        execution_model_fingerprint=value.execution_model_fingerprint,
        execution_calibration_sha256=value.execution_calibration_sha256,
        execution_fidelity=value.execution_fidelity,
        capacity_assessment_sha256=value.capacity_assessment_sha256,
        max_feasible_quantity=value.max_feasible_quantity,
        lot_size=value.lot_size,
        registered_run_receipt_sha256=value.registered_run_receipt_sha256,
        required_evidence_dimensions=value.required_evidence_dimensions,
        dimension_evidence=value.dimension_evidence,
        status=value.status,
    )


def _require_registered_economics_join(
    receipt: RegisteredStrategyRunReceipt,
    economics: StrategyEconomicsBinding,
) -> None:
    """Require one economics evidence chain to name the exact registered run."""

    receipt = _readmit_registered_strategy_run_receipt(receipt)
    economics = _readmit_strategy_economics_binding(economics)
    receipt_digest = verify_registered_strategy_run(receipt.proposal, receipt)
    if economics.registered_run_receipt_sha256 != receipt_digest:
        raise ValueError(
            "economics evidence is not bound to the exact registered strategy run"
        )


@dataclass(frozen=True)
class EconomicsBoundProposal:
    """Gross deterministic signal plus a non-expansive ex-ante economics gate."""

    gross_proposal: DeterministicProposal
    economics: StrategyEconomicsBinding
    instrument_version: str
    action: str
    quantity: Decimal
    reason: str
    registered_run_receipt: RegisteredStrategyRunReceipt | None = None

    def __post_init__(self) -> None:
        if type(self.gross_proposal) is not DeterministicProposal:
            raise TypeError("gross_proposal must be DeterministicProposal")
        if type(self.economics) is not StrategyEconomicsBinding:
            raise TypeError("economics must be StrategyEconomicsBinding")
        instrument = _text(self.instrument_version, name="instrument_version")
        receipt = self.registered_run_receipt
        action = _text(self.action, name="action").upper()
        if action not in {"BUY", "SELL", "HOLD"}:
            raise ValueError("unsupported economics-bound proposal action")
        quantity = _decimal(self.quantity, name="quantity")
        if quantity < 0:
            raise ValueError("economics-bound quantity must be non-negative")
        if action == "HOLD" and quantity != 0:
            raise ValueError("HOLD economics-bound proposal quantity must be zero")
        if action in {"BUY", "SELL"} and quantity <= 0:
            raise ValueError("BUY/SELL economics-bound proposal quantity must be positive")
        gross = self.gross_proposal
        economics = self.economics
        if gross.action != "HOLD" and receipt is None:
            raise ValueError(
                "exposure-bearing registered proposal requires a verified run receipt"
            )
        if receipt is not None:
            verify_registered_strategy_run(gross, receipt)
            if receipt.instrument_version != instrument:
                raise ValueError(
                    "registered run instrument_version does not match economics binding"
                )
            if action != "HOLD":
                _require_registered_economics_join(receipt, economics)
        if gross.strategy_fingerprint != economics.strategy_fingerprint:
            raise ValueError("economics strategy fingerprint does not match proposal")
        if (
            gross.strategy_configuration_fingerprint
            != economics.strategy_configuration_fingerprint
        ):
            raise ValueError(
                "economics strategy configuration fingerprint does not match proposal"
            )
        if economics.instrument_version != instrument:
            raise ValueError("economics instrument_version does not match proposal")
        if economics.information_cutoff != gross.information_cutoff:
            raise ValueError("economics information_cutoff does not match proposal")
        if economics.decision_time != gross.decision_time:
            raise ValueError("economics decision_time does not match proposal")
        if economics.horizon_seconds != gross.horizon_seconds:
            raise ValueError("economics horizon does not match proposal")
        if economics.expiry != gross.expiry:
            raise ValueError("economics expiry does not match proposal")
        if action != "HOLD":
            if action != gross.action:
                raise ValueError("economics-bound action cannot change gross direction")
            if quantity > gross.quantity:
                raise ValueError("economics binding cannot increase proposal exposure")
            if quantity > economics.max_feasible_quantity:
                raise ValueError(
                    "economics-bound quantity exceeds frozen capacity"
                )
            if not is_exact_decimal_multiple(quantity, economics.lot_size):
                raise ValueError(
                    "economics-bound quantity must be an executable lot multiple"
                )
            if economics.status != "QUALIFIED":
                raise ValueError(
                    "non-HOLD economics-bound proposal requires QUALIFIED economics"
                )
        object.__setattr__(self, "instrument_version", instrument)
        object.__setattr__(self, "action", action)
        object.__setattr__(self, "quantity", quantity)
        object.__setattr__(self, "reason", _text(self.reason, name="reason"))

    @property
    def fingerprint(self) -> str:
        gross = self.gross_proposal
        payload = {
            "schema_version": "1.0.0",
            "strategy_version": gross.strategy_version,
            "strategy_fingerprint": gross.strategy_fingerprint,
            "strategy_configuration_fingerprint": (
                gross.strategy_configuration_fingerprint
            ),
            "symbol": gross.symbol,
            "gross_action": gross.action,
            "gross_quantity": str(gross.quantity),
            "decision_time": _utc_text(gross.decision_time),
            "information_cutoff": _utc_text(gross.information_cutoff),
            "horizon_seconds": gross.horizon_seconds,
            "expiry": _utc_text(gross.expiry),
            "evidence_event_ids": list(gross.evidence_event_ids),
            "economics_binding_sha256": self.economics.fingerprint,
            "instrument_version": self.instrument_version,
            "registered_run_receipt_sha256": (
                None if self.registered_run_receipt is None
                else self.registered_run_receipt.fingerprint
            ),
            "effective_action": self.action,
            "effective_quantity": str(self.quantity),
            "reason": self.reason,
        }
        return "sha256:" + sha256(
            _canonical_json(payload).encode("utf-8")
        ).hexdigest()


def bind_strategy_economics(
    proposal: DeterministicProposal,
    economics: StrategyEconomicsBinding,
    *,
    instrument_version: str,
    registered_run_receipt: RegisteredStrategyRunReceipt | None = None,
) -> EconomicsBoundProposal:
    """Bind frozen decision-time economics without expanding the gross signal."""

    proposal = _readmit_deterministic_proposal(proposal)
    economics = _readmit_strategy_economics_binding(economics)
    if registered_run_receipt is not None:
        registered_run_receipt = _readmit_registered_strategy_run_receipt(
            registered_run_receipt
        )
    instrument = _text(instrument_version, name="instrument_version")
    if (
        proposal.information_cutoff is None
        or proposal.horizon_seconds is None
        or proposal.expiry is None
        or proposal.strategy_fingerprint is None
        or proposal.strategy_configuration_fingerprint is None
    ):
        raise ValueError("proposal lacks registered strategy/horizon metadata")
    if proposal.action != "HOLD" and registered_run_receipt is None:
        raise ValueError(
            "exposure-bearing registered proposal requires a verified run receipt"
        )
    if registered_run_receipt is not None:
        verify_registered_strategy_run(proposal, registered_run_receipt)
        if registered_run_receipt.instrument_version != instrument:
            raise ValueError(
                "registered run instrument_version does not match economics binding"
            )
        if proposal.action != "HOLD" and economics.status == "QUALIFIED":
            _require_registered_economics_join(
                registered_run_receipt,
                economics,
            )
    if economics.strategy_fingerprint != proposal.strategy_fingerprint:
        raise ValueError("economics strategy fingerprint does not match proposal")
    if (
        economics.strategy_configuration_fingerprint
        != proposal.strategy_configuration_fingerprint
    ):
        raise ValueError(
            "economics strategy configuration fingerprint does not match proposal"
        )
    if economics.instrument_version != instrument:
        raise ValueError("economics instrument_version does not match proposal")
    if economics.information_cutoff != proposal.information_cutoff:
        raise ValueError("economics information_cutoff does not match proposal")
    if economics.decision_time != proposal.decision_time:
        raise ValueError("economics decision_time does not match proposal")
    if economics.horizon_seconds != proposal.horizon_seconds:
        raise ValueError("economics horizon does not match proposal")
    if economics.expiry != proposal.expiry:
        raise ValueError("economics expiry does not match proposal")

    if proposal.action == "HOLD":
        return EconomicsBoundProposal(
            gross_proposal=proposal,
            economics=economics,
            instrument_version=instrument,
            action="HOLD",
            quantity=Decimal("0"),
            reason=proposal.reason,
            registered_run_receipt=registered_run_receipt,
        )

    if economics.status != "QUALIFIED":
        missing = (
            ", ".join(economics.missing_dimensions)
            if economics.missing_dimensions
            else economics.status
        )
        return EconomicsBoundProposal(
            gross_proposal=proposal,
            economics=economics,
            instrument_version=instrument,
            action="HOLD",
            quantity=Decimal("0"),
            reason=f"decision-time economics {economics.status}: {missing}",
            registered_run_receipt=registered_run_receipt,
        )

    capped = min(proposal.quantity, economics.max_feasible_quantity)
    # Exact non-expansive floor to the accepted instrument lot. The neutral
    # runtime avoids ambient Decimal division/multiplication and rejects
    # out-of-budget arithmetic instead of rounding an exposure upward.
    quantity = round_fraction_to_quantum(
        as_fraction(capped), economics.lot_size, mode="FLOOR"
    )
    if quantity <= 0:
        return EconomicsBoundProposal(
            gross_proposal=proposal,
            economics=economics,
            instrument_version=instrument,
            action="HOLD",
            quantity=Decimal("0"),
            reason="decision-time capacity is below one executable lot",
            registered_run_receipt=registered_run_receipt,
        )
    if quantity > proposal.quantity:
        raise ValueError("economics binding cannot increase proposal exposure")
    return EconomicsBoundProposal(
        gross_proposal=proposal,
        economics=economics,
        instrument_version=instrument,
        action=proposal.action,
        quantity=quantity,
        reason=(
            proposal.reason
            if quantity == proposal.quantity
            else "gross signal retained with quantity reduced by frozen ex-ante capacity"
        ),
        registered_run_receipt=registered_run_receipt,
    )


class NoTradeBaseline:
    """Deterministic null baseline that can never propose financial exposure."""

    def __init__(self, *, descriptor: StrategyDescriptor):
        descriptor = _readmit_strategy_descriptor(descriptor)
        if descriptor.family != "NO_TRADE_CONTROL":
            raise ValueError("no-trade descriptor family must be NO_TRADE_CONTROL")
        self.descriptor = descriptor

    def propose(
        self,
        *,
        symbol: str,
        decision_time: datetime,
        evidence_event_ids: tuple[str, ...] = (),
    ) -> DeterministicProposal:
        descriptor = _readmit_strategy_descriptor(self.descriptor)
        if descriptor.family != "NO_TRADE_CONTROL":
            raise ValueError("no-trade descriptor family must be NO_TRADE_CONTROL")
        name = _text(symbol, name="symbol")
        cutoff = _time(decision_time, name="decision_time")
        if type(evidence_event_ids) is not tuple:
            raise ValueError("evidence_event_ids must be a tuple")
        evidence = tuple(
            _text(value, name="evidence_event_id")
            for value in evidence_event_ids
        )
        if len(set(evidence)) != len(evidence):
            raise ValueError("evidence_event_ids contains duplicates")
        return DeterministicProposal(
            symbol=name,
            action="HOLD",
            quantity=Decimal("0"),
            decision_time=cutoff,
            evidence_event_ids=evidence,
            model_calls=0,
            economic_edge_claim="UNPROVEN",
            reason="registered no-trade control baseline",
            information_cutoff=cutoff,
            horizon_seconds=descriptor.horizon_seconds,
            expiry=cutoff + timedelta(seconds=descriptor.horizon_seconds),
            strategy_version=(
                f"{descriptor.strategy_id}@{descriptor.version}"
            ),
            strategy_fingerprint=descriptor.fingerprint,
            strategy_configuration_fingerprint=descriptor.fingerprint,
        )


class ReturnThresholdBaseline:
    """A bounded research baseline, not a qualified trading strategy."""

    def __init__(
        self,
        *,
        lookback: int,
        threshold,
        proposal_quantity,
        descriptor: StrategyDescriptor | None = None,
    ):
        if type(lookback) is not int or lookback < 2:
            raise ValueError("lookback must be an integer >= 2")
        self.lookback = lookback
        self.threshold = _decimal(threshold, name="threshold")
        if self.threshold < 0:
            raise ValueError("threshold must be non-negative")
        self.proposal_quantity = _decimal(proposal_quantity, name="proposal_quantity")
        if self.proposal_quantity <= 0:
            raise ValueError("proposal_quantity must be positive")
        if descriptor is not None:
            descriptor = _readmit_strategy_descriptor(descriptor)
            expected_family = _threshold_family_for_type(type(self))
            if descriptor.family != expected_family:
                raise ValueError(
                    f"descriptor family must be {expected_family} "
                    "for this strategy implementation"
                )
            if descriptor.minimum_history != lookback:
                raise ValueError("descriptor minimum_history must equal lookback")
            bounds = {name: (Decimal(minimum), Decimal(maximum)) for name, minimum, maximum in descriptor.parameter_bounds}
            for parameter, value in (
                ("threshold", self.threshold),
                ("proposal_quantity", self.proposal_quantity),
            ):
                if parameter not in bounds:
                    raise ValueError(f"descriptor lacks {parameter} parameter bounds")
                minimum, maximum = bounds[parameter]
                if value < minimum or value > maximum:
                    raise ValueError(f"{parameter} is outside descriptor bounds")
        self.descriptor = descriptor
        self._history: dict[str, list[CausalObservation]] = {}
        self._observations_by_id: dict[str, CausalObservation] = {}

    @property
    def configuration_fingerprint(self) -> str | None:
        descriptor = _validate_threshold_strategy_configuration(self)
        if descriptor is None:
            return None
        body = {
            "descriptor_fingerprint": descriptor.fingerprint,
            "parameters": {
                "lookback": self.lookback,
                "threshold": str(self.threshold),
                "proposal_quantity": str(self.proposal_quantity),
            },
        }
        return "sha256:" + sha256(
            _canonical_json(body).encode("utf-8")
        ).hexdigest()

    def ingest(self, observation: CausalObservation, *, simulation_time: datetime) -> bool:
        _validate_threshold_strategy_configuration(self)
        if type(self._history) is not dict or type(self._observations_by_id) is not dict:
            raise ValueError("strategy state containers are invalid")
        observation = _readmit_causal_observation(observation)
        cutoff = _time(simulation_time, name="simulation_time")
        if observation.available_at > cutoff:
            raise ValueError("observation is not causally available at simulation_time")
        existing = self._observations_by_id.get(observation.event_id)
        if existing is not None:
            existing = _readmit_causal_observation(existing)
            if existing != observation:
                raise ValueError("event_id already exists with different observation content")
            return False
        history = self._history.setdefault(observation.symbol, [])
        if type(history) is not list:
            raise ValueError("strategy symbol history must be a list")
        if history:
            last_retained = _readmit_causal_observation(history[-1])
            if last_retained.symbol != observation.symbol:
                raise ValueError("retained history symbol does not match history key")
            if observation.available_at < last_retained.available_at:
                raise ValueError(
                    "observations must be ingested in non-decreasing availability order"
                )
        history.append(observation)
        if len(history) > self.lookback:
            del history[:-self.lookback]
        self._observations_by_id[observation.event_id] = observation
        return True

    def propose(self, *, symbol: str, decision_time: datetime) -> DeterministicProposal:
        descriptor = _validate_threshold_strategy_configuration(self)
        name = _text(symbol, name="symbol")
        state_history, _state_seen = _readmit_threshold_strategy_state(
            self,
            retained_symbol=name,
            include_all_seen=False,
        )
        cutoff = _time(decision_time, name="decision_time")
        history = state_history.get(name, ())
        eligible = [item for item in history if item.available_at <= cutoff]
        horizon_seconds = (
            descriptor.horizon_seconds if descriptor is not None else None
        )
        expiry = (
            cutoff + timedelta(seconds=horizon_seconds)
            if horizon_seconds is not None
            else None
        )
        strategy_version = (
            f"{descriptor.strategy_id}@{descriptor.version}"
            if descriptor is not None
            else None
        )
        strategy_fingerprint = (
            descriptor.fingerprint if descriptor is not None else None
        )
        strategy_configuration_fingerprint = self.configuration_fingerprint
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
                information_cutoff=cutoff,
                horizon_seconds=horizon_seconds,
                expiry=expiry,
                strategy_version=strategy_version,
                strategy_fingerprint=strategy_fingerprint,
                strategy_configuration_fingerprint=(
                    strategy_configuration_fingerprint
                ),
            )
        window = eligible[-self.lookback:]
        action, reason = _threshold_signal(
            type(self),
            window,
            self.threshold,
        )
        quantity = (
            self.proposal_quantity
            if action in {"BUY", "SELL"}
            else Decimal("0")
        )
        return DeterministicProposal(
            symbol=name,
            action=action,
            quantity=quantity,
            decision_time=cutoff,
            evidence_event_ids=tuple(item.event_id for item in window),
            model_calls=0,
            economic_edge_claim="UNPROVEN",
            reason=reason,
            information_cutoff=cutoff,
            horizon_seconds=horizon_seconds,
            expiry=expiry,
            strategy_version=strategy_version,
            strategy_fingerprint=strategy_fingerprint,
            strategy_configuration_fingerprint=(
                strategy_configuration_fingerprint
            ),
        )

    def snapshot(self) -> str:
        descriptor = _validate_threshold_strategy_configuration(self)
        state_history, state_seen = _readmit_threshold_strategy_state(self)
        payload = {
            "schema_version": 6,
            "strategy_family": _threshold_family_for_type(type(self)),
            "lookback": self.lookback,
            "threshold": str(self.threshold),
            "proposal_quantity": str(self.proposal_quantity),
            "descriptor": (
                None
                if descriptor is None
                else descriptor.canonical_document()
            ),
            "descriptor_fingerprint": (
                None
                if descriptor is None
                else descriptor.fingerprint
            ),
            "configuration_fingerprint": self.configuration_fingerprint,
            "seen_events": {
                event_id: {
                    "symbol": item.symbol,
                    "available_at": item.available_at.isoformat(),
                    "price": str(item.price),
                }
                for event_id, item in sorted(state_seen.items())
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
                for symbol, rows in sorted(state_history.items())
            },
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    @classmethod
    def restore(cls, snapshot: str) -> "ReturnThresholdBaseline":
        if cls not in (
            ReturnThresholdBaseline,
            MeanReversionThresholdBaseline,
            BreakoutThresholdBaseline,
        ):
            raise TypeError(
                "strategy must be ReturnThresholdBaseline, "
                "MeanReversionThresholdBaseline or BreakoutThresholdBaseline"
            )
        try:
            payload = _read_strict_strategy_json(snapshot)
        except (TypeError, ValueError) as error:
            raise ValueError("strategy snapshot is invalid") from error
        if (
            type(payload) is not dict
            or type(payload.get("schema_version")) is not int
            or payload["schema_version"] not in {1, 2, 3, 4, 5, 6}
        ):
            raise ValueError("unsupported strategy snapshot")
        version = payload["schema_version"]
        if version < 6 and cls is not ReturnThresholdBaseline:
            raise ValueError(
                "legacy strategy snapshot can only restore ReturnThresholdBaseline"
            )
        if version == 6:
            family = _text(payload.get("strategy_family"), name="strategy_family")
            if family != _threshold_family_for_type(cls):
                raise ValueError(
                    "strategy snapshot family does not match restore implementation"
                )
        expected = {"schema_version", "lookback", "threshold", "proposal_quantity", "history"}
        if version in {2, 3, 4, 5, 6}:
            expected.add("seen_events")
        if version in {3, 4, 5, 6}:
            expected.add("descriptor")
        if version in {4, 5, 6}:
            expected.add("descriptor_fingerprint")
        if version in {5, 6}:
            expected.add("configuration_fingerprint")
        if version == 6:
            expected.add("strategy_family")
        if set(payload) != expected or not isinstance(payload["history"], dict):
            raise ValueError("strategy snapshot structure is invalid")
        if version in {2, 3, 4, 5, 6} and not isinstance(payload["seen_events"], dict):
            raise ValueError("strategy seen-event snapshot is invalid")
        descriptor = None
        if version in {3, 4, 5, 6} and payload["descriptor"] is not None:
            descriptor = _strategy_descriptor_from_document(payload["descriptor"])
        if version in {4, 5, 6}:
            declared_fingerprint = payload["descriptor_fingerprint"]
            if descriptor is None:
                if declared_fingerprint is not None:
                    raise ValueError(
                        "descriptor fingerprint exists without descriptor"
                    )
            else:
                try:
                    normalized_fingerprint = _digest(
                        declared_fingerprint,
                        name="descriptor_fingerprint",
                    )
                except (TypeError, ValueError) as error:
                    raise ValueError(
                        "strategy descriptor fingerprint is invalid"
                    ) from error
                if normalized_fingerprint != descriptor.fingerprint:
                    raise ValueError(
                        "strategy descriptor fingerprint does not match snapshot"
                    )
        strategy = cls(
            lookback=payload["lookback"],
            threshold=payload["threshold"],
            proposal_quantity=payload["proposal_quantity"],
            descriptor=descriptor,
        )
        if version in {5, 6}:
            declared_configuration = payload["configuration_fingerprint"]
            if strategy.configuration_fingerprint is None:
                if declared_configuration is not None:
                    raise ValueError(
                        "configuration fingerprint exists without descriptor"
                    )
            else:
                try:
                    normalized_configuration = _digest(
                        declared_configuration,
                        name="configuration_fingerprint",
                    )
                except (TypeError, ValueError) as error:
                    raise ValueError(
                        "strategy configuration fingerprint is invalid"
                    ) from error
                if normalized_configuration != strategy.configuration_fingerprint:
                    raise ValueError(
                        "strategy configuration fingerprint does not match snapshot"
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

        if version in {2, 3, 4, 5, 6}:
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
                existing = strategy._observations_by_id.get(event_id)
                if existing is not None and existing != observation:
                    raise ValueError(
                        "seen-event snapshot conflicts with retained history"
                    )
                strategy._observations_by_id[event_id] = observation
                declared_seen.add(event_id)

            retained_ids = {
                item.event_id
                for rows in strategy._history.values()
                for item in rows
            }
            if not retained_ids.issubset(declared_seen):
                raise ValueError("seen-event snapshot is missing retained history")
        return strategy


class MeanReversionThresholdBaseline(ReturnThresholdBaseline):
    """Transparent exact-rational mean-reversion research control."""


class BreakoutThresholdBaseline(ReturnThresholdBaseline):
    """Transparent exact-rational causal breakout research control."""


def _threshold_family_for_type(strategy_type: type) -> str:
    if strategy_type is ReturnThresholdBaseline:
        return "DETERMINISTIC_RETURN_THRESHOLD"
    if strategy_type is MeanReversionThresholdBaseline:
        return "DETERMINISTIC_MEAN_REVERSION_THRESHOLD"
    if strategy_type is BreakoutThresholdBaseline:
        return "DETERMINISTIC_BREAKOUT_THRESHOLD"
    raise TypeError(
        "strategy must be ReturnThresholdBaseline, "
        "MeanReversionThresholdBaseline or BreakoutThresholdBaseline"
    )



def _validate_threshold_strategy_configuration(
    strategy: ReturnThresholdBaseline,
) -> StrategyDescriptor | None:
    _threshold_family_for_type(type(strategy))
    if type(strategy.lookback) is not int or strategy.lookback < 2:
        raise ValueError("strategy lookback must remain an integer >= 2")
    if type(strategy.threshold) is not Decimal:
        raise ValueError("strategy threshold must remain a canonical Decimal")
    threshold = _decimal(strategy.threshold, name="strategy threshold")
    if threshold < 0:
        raise ValueError("strategy threshold must remain non-negative")
    if type(strategy.proposal_quantity) is not Decimal:
        raise ValueError(
            "strategy proposal_quantity must remain a canonical Decimal"
        )
    quantity = _decimal(
        strategy.proposal_quantity,
        name="strategy proposal_quantity",
    )
    if quantity <= 0:
        raise ValueError("strategy proposal_quantity must remain positive")

    if strategy.descriptor is None:
        return None
    descriptor = _readmit_strategy_descriptor(strategy.descriptor)
    expected_family = _threshold_family_for_type(type(strategy))
    if descriptor.family != expected_family:
        raise ValueError(
            f"descriptor family must be {expected_family} "
            "for this strategy implementation"
        )
    if descriptor.minimum_history != strategy.lookback:
        raise ValueError("descriptor minimum_history must equal lookback")
    bounds = {
        name: (
            _decimal(minimum, name=f"{name} minimum"),
            _decimal(maximum, name=f"{name} maximum"),
        )
        for name, minimum, maximum in descriptor.parameter_bounds
    }
    for parameter, value in (
        ("threshold", threshold),
        ("proposal_quantity", quantity),
    ):
        if parameter not in bounds:
            raise ValueError(f"descriptor lacks {parameter} parameter bounds")
        minimum, maximum = bounds[parameter]
        if value < minimum or value > maximum:
            raise ValueError(f"{parameter} is outside descriptor bounds")
    return descriptor


def _readmit_threshold_strategy_state(
    strategy: ReturnThresholdBaseline,
    *,
    retained_symbol: str | None = None,
    include_all_seen: bool = True,
) -> tuple[
    dict[str, tuple[CausalObservation, ...]],
    dict[str, CausalObservation],
]:
    if type(strategy._history) is not dict or type(strategy._observations_by_id) is not dict:
        raise ValueError("strategy state containers are invalid")
    if type(include_all_seen) is not bool:
        raise TypeError("include_all_seen must be a built-in bool")
    if retained_symbol is not None:
        retained_symbol = _text(retained_symbol, name="retained_symbol")

    seen: dict[str, CausalObservation] = {}
    if include_all_seen:
        for event_id, raw_observation in strategy._observations_by_id.items():
            canonical_event_id = _text(event_id, name="seen event_id")
            observation = _readmit_causal_observation(raw_observation)
            if canonical_event_id != event_id or observation.event_id != event_id:
                raise ValueError("seen-event key does not match observation event_id")
            seen[event_id] = observation

    history: dict[str, tuple[CausalObservation, ...]] = {}
    retained_ids: set[str] = set()
    items = (
        ((retained_symbol, strategy._history.get(retained_symbol, [])),)
        if retained_symbol is not None
        else strategy._history.items()
    )
    for symbol, raw_rows in items:
        canonical_symbol = _text(symbol, name="history symbol")
        if canonical_symbol != symbol:
            raise ValueError("history symbol key is not canonical")
        if type(raw_rows) is not list:
            raise ValueError("strategy symbol history must be a list")
        if len(raw_rows) > strategy.lookback:
            raise ValueError("strategy retained history exceeds configured lookback")
        rows: list[CausalObservation] = []
        previous_time: datetime | None = None
        for raw_observation in raw_rows:
            observation = _readmit_causal_observation(raw_observation)
            if observation.symbol != symbol:
                raise ValueError("retained history symbol does not match history key")
            if observation.event_id in retained_ids:
                raise ValueError("retained history contains duplicate event_id")
            retained_ids.add(observation.event_id)
            seen_observation = seen.get(observation.event_id)
            if seen_observation is None:
                raw_seen = strategy._observations_by_id.get(observation.event_id)
                if raw_seen is not None:
                    seen_observation = _readmit_causal_observation(raw_seen)
                    if seen_observation.event_id != observation.event_id:
                        raise ValueError(
                            "seen-event key does not match observation event_id"
                        )
                    if include_all_seen:
                        seen[observation.event_id] = seen_observation
            if seen_observation is None or seen_observation != observation:
                raise ValueError(
                    "retained history is not backed by identical seen-event state"
                )
            if previous_time is not None and observation.available_at < previous_time:
                raise ValueError("retained history availability order is invalid")
            previous_time = observation.available_at
            rows.append(observation)
        history[symbol] = tuple(rows)
    return history, seen


def _threshold_signal(
    strategy_type: type,
    window: list[CausalObservation],
    threshold_value: Decimal,
) -> tuple[str, str]:
    """Return one exact zero-model gross signal; never economic qualification."""

    if type(window) is not list or len(window) < 2:
        raise ValueError("threshold strategy window must contain at least two observations")
    threshold = as_fraction(threshold_value)
    first = as_fraction(window[0].price)
    last = as_fraction(window[-1].price)

    if strategy_type in (
        ReturnThresholdBaseline,
        MeanReversionThresholdBaseline,
    ):
        change = bounded_fraction((last - first) / first)
        if strategy_type is ReturnThresholdBaseline:
            if change > threshold:
                return "BUY", "registered deterministic return threshold exceeded"
            if change < -threshold:
                return "SELL", "registered deterministic negative return threshold exceeded"
            return "HOLD", "registered deterministic threshold not exceeded"
        if change > threshold:
            return "SELL", "registered deterministic mean-reversion upper threshold exceeded"
        if change < -threshold:
            return "BUY", "registered deterministic mean-reversion lower threshold exceeded"
        return "HOLD", "registered deterministic mean-reversion threshold not exceeded"

    if strategy_type is BreakoutThresholdBaseline:
        prior_prices = [as_fraction(item.price) for item in window[:-1]]
        prior_high = max(prior_prices)
        prior_low = min(prior_prices)
        one = as_fraction(Decimal("1"))
        upper = bounded_fraction(prior_high * bounded_fraction(one + threshold))
        lower = bounded_fraction(prior_low * bounded_fraction(one - threshold))
        if last > upper:
            return "BUY", "registered deterministic upside breakout threshold exceeded"
        if last < lower:
            return "SELL", "registered deterministic downside breakout threshold exceeded"
        return "HOLD", "registered deterministic breakout threshold not exceeded"

    raise TypeError(
        "strategy must be ReturnThresholdBaseline, "
        "MeanReversionThresholdBaseline or BreakoutThresholdBaseline"
    )


def _restore_threshold_strategy_snapshot(snapshot: str) -> ReturnThresholdBaseline:
    """Restore the exact strategy family encoded by one canonical snapshot."""

    try:
        payload = _read_strict_strategy_json(snapshot)
    except (TypeError, ValueError) as error:
        raise ValueError("strategy snapshot is invalid") from error
    if type(payload) is not dict or type(payload.get("schema_version")) is not int:
        raise ValueError("unsupported strategy snapshot")
    version = payload["schema_version"]
    if version in {1, 2, 3, 4, 5}:
        strategy_type = ReturnThresholdBaseline
    elif version == 6:
        family = _text(payload.get("strategy_family"), name="strategy_family")
        mapping = {
            "DETERMINISTIC_RETURN_THRESHOLD": ReturnThresholdBaseline,
            "DETERMINISTIC_MEAN_REVERSION_THRESHOLD": MeanReversionThresholdBaseline,
            "DETERMINISTIC_BREAKOUT_THRESHOLD": BreakoutThresholdBaseline,
        }
        strategy_type = mapping.get(family)
        if strategy_type is None:
            raise ValueError("unsupported deterministic threshold strategy family")
    else:
        raise ValueError("unsupported strategy snapshot")
    return strategy_type.restore(snapshot)


def run_baseline(
    strategy: ReturnThresholdBaseline,
    observations: Iterable[CausalObservation],
    *,
    decision_time: datetime,
    symbol: str,
) -> DeterministicProposal:
    _threshold_family_for_type(type(strategy))
    if type(observations) not in (list, tuple):
        raise TypeError("observations must be an exact built-in list or tuple")
    cutoff = _time(decision_time, name="decision_time")
    detached = tuple(
        _readmit_causal_observation(observation)
        for observation in observations
    )
    for observation in detached:
        strategy.ingest(observation, simulation_time=cutoff)
    return strategy.propose(symbol=symbol, decision_time=cutoff)


def run_registered_baseline(
    strategy: ReturnThresholdBaseline,
    observations: Iterable[CausalObservation],
    *,
    decision_time: datetime,
    symbol: str,
    instrument_version: str,
) -> tuple[DeterministicProposal, RegisteredStrategyRunReceipt]:
    """Run a registered strategy from pristine state and mint replay evidence."""

    _threshold_family_for_type(type(strategy))
    descriptor = _validate_threshold_strategy_configuration(strategy)
    if descriptor is None:
        raise ValueError("registered runner requires a registered strategy descriptor")
    if type(strategy._history) is not dict or type(strategy._observations_by_id) is not dict:
        raise ValueError("strategy state containers are invalid")
    if strategy._observations_by_id or strategy._history:
        raise ValueError("registered runner requires pristine strategy state")
    pristine_snapshot = strategy.snapshot()
    if type(observations) not in (list, tuple):
        raise TypeError("observations must be an exact built-in list or tuple")
    materialized = tuple(
        _readmit_causal_observation(observation)
        for observation in observations
    )
    event_ids = tuple(item.event_id for item in materialized)
    if len(set(event_ids)) != len(event_ids):
        raise ValueError("registered runner observations contain duplicate event_id")
    runner = _restore_threshold_strategy_snapshot(pristine_snapshot)
    proposal = run_baseline(
        runner,
        materialized,
        decision_time=decision_time,
        symbol=symbol,
    )
    receipt = RegisteredStrategyRunReceipt(
        strategy_snapshot=pristine_snapshot,
        instrument_version=instrument_version,
        symbol=symbol,
        decision_time=decision_time,
        observations=materialized,
        proposal=proposal,
    )
    verify_registered_strategy_run(proposal, receipt)
    return proposal, receipt



def _utc_text(value: datetime) -> str:
    normalized = _time(value, name="timestamp")
    return normalized.isoformat().replace("+00:00", "Z")


def to_decision_proposal(
    proposal: DeterministicProposal,
    *,
    proposal_id: str,
    instrument_version: str,
    economics_binding: StrategyEconomicsBinding,
    exit_policy_ref: str,
    compute_cost_currency: str,
    registered_run_receipt: RegisteredStrategyRunReceipt | None = None,
    counterarguments: tuple[str, ...] = (),
) -> dict[str, object]:
    """Project a registered zero-model result through frozen ex-ante economics.

    Gross strategy evidence remains visible and immutable. The economics binding
    may only preserve/reduce exposure or convert it to NO_TRADE; it never grants
    financial authority and never consumes realized post-arrival liquidity.
    """

    proposal = _readmit_deterministic_proposal(proposal)
    economics_binding = _readmit_strategy_economics_binding(economics_binding)
    if registered_run_receipt is not None:
        registered_run_receipt = _readmit_registered_strategy_run_receipt(
            registered_run_receipt
        )
    if (
        proposal.information_cutoff is None
        or proposal.horizon_seconds is None
        or proposal.expiry is None
        or proposal.strategy_version is None
        or proposal.strategy_fingerprint is None
        or proposal.strategy_configuration_fingerprint is None
    ):
        raise ValueError("proposal lacks registered strategy/horizon metadata")
    try:
        normalized_proposal_id = str(UUID(_text(proposal_id, name="proposal_id")))
    except (ValueError, AttributeError) as error:
        raise ValueError("proposal_id must be a canonical UUID") from error
    if normalized_proposal_id != proposal_id:
        raise ValueError("proposal_id must be a canonical UUID")
    instrument = _text(instrument_version, name="instrument_version")
    exit_ref = _text(exit_policy_ref, name="exit_policy_ref")
    currency = _text(compute_cost_currency, name="compute_cost_currency")
    if isinstance(counterarguments, (str, bytes)) or not isinstance(
        counterarguments,
        tuple,
    ):
        raise ValueError("counterarguments must be a tuple")
    normalized_counterarguments = tuple(
        _text(value, name="counterargument")
        for value in counterarguments
    )
    if len(set(normalized_counterarguments)) != len(normalized_counterarguments):
        raise ValueError("counterarguments contains duplicates")

    bound = bind_strategy_economics(
        proposal,
        economics_binding,
        instrument_version=instrument,
        registered_run_receipt=registered_run_receipt,
    )
    proposal = bound.gross_proposal
    economics_binding = bound.economics
    registered_run_receipt = bound.registered_run_receipt
    no_trade = bound.action == "HOLD"
    body: dict[str, object] = {
        "proposal_id": normalized_proposal_id,
        "strategy_version": proposal.strategy_version,
        "decision_at": _utc_text(proposal.decision_time),
        "information_cutoff": _utc_text(proposal.information_cutoff),
        "input_manifest_refs": list(economics_binding.input_manifest_refs),
        "thesis": proposal.reason,
        "candidate_instruments": [] if no_trade else [instrument],
        "horizon": f"PT{proposal.horizon_seconds}S",
        "expected_return_distribution_ref": (
            economics_binding.after_cost_return_distribution_sha256
        ),
        "confidence_basis": {
            "economic_edge_claim": proposal.economic_edge_claim,
            "strategy_fingerprint": proposal.strategy_fingerprint,
            "strategy_configuration_fingerprint": (
                proposal.strategy_configuration_fingerprint
            ),
            "model_calls": proposal.model_calls,
            "gross_action": proposal.action,
            "gross_quantity": str(proposal.quantity),
            "effective_action": bound.action,
            "effective_quantity": str(bound.quantity),
            "strategy_economics_binding_sha256": economics_binding.fingerprint,
            "economics_bound_proposal_sha256": bound.fingerprint,
            "registered_run_receipt_sha256": (
                None if registered_run_receipt is None
                else registered_run_receipt.fingerprint
            ),
            "gross_return_distribution_ref": (
                economics_binding.gross_return_distribution_sha256
            ),
            "after_cost_lower_bound": str(
                economics_binding.after_cost_lower_bound
            ),
            "economics_status": economics_binding.status,
            "missing_economic_dimensions": list(
                economics_binding.missing_dimensions
            ),
            "execution_model_fingerprint": (
                economics_binding.execution_model_fingerprint
            ),
            "execution_calibration_sha256": (
                economics_binding.execution_calibration_sha256
            ),
            "execution_fidelity": economics_binding.execution_fidelity,
            "capacity_assessment_sha256": (
                economics_binding.capacity_assessment_sha256
            ),
            "max_feasible_quantity": str(
                economics_binding.max_feasible_quantity
            ),
        },
        "counterarguments": list(normalized_counterarguments),
        "exit_policy_ref": exit_ref,
        "expiry": _utc_text(proposal.expiry),
        "estimated_compute_cost": {
            "amount": "0",
            "currency": currency,
        },
    }
    if no_trade:
        body["NO_TRADE_reason"] = bound.reason
    return body
