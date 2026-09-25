"""Deterministic zero-model proposal path for research and simulation.

The baseline is intentionally simple and makes no profitability claim. It only
consumes observations evidenced as available by the decision cutoff.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
import re
from typing import Iterable
from uuid import UUID


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
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        for name in ("market_requirements", "supported_regimes"):
            value = getattr(self, name)
            if isinstance(value, (str, bytes)) or not isinstance(value, tuple):
                raise ValueError(f"{name} must be a tuple")
            normalized = tuple(_text(item, name=f"{name} item") for item in value)
            if not normalized or len(set(normalized)) != len(normalized):
                raise ValueError(f"{name} must contain unique non-empty values")
            object.__setattr__(self, name, normalized)
        if not isinstance(self.parameter_bounds, tuple) or not self.parameter_bounds:
            raise ValueError("parameter_bounds must be a non-empty tuple")
        normalized_bounds = []
        names = set()
        for item in self.parameter_bounds:
            if not isinstance(item, tuple) or len(item) != 3:
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
        if (
            isinstance(self.evidence_event_ids, (str, bytes))
            or not isinstance(self.evidence_event_ids, tuple)
        ):
            raise ValueError("evidence_event_ids must be a tuple")
        evidence = tuple(
            _text(value, name="evidence_event_id")
            for value in self.evidence_event_ids
        )
        if len(set(evidence)) != len(evidence):
            raise ValueError("evidence_event_ids contains duplicates")
        if isinstance(self.model_calls, bool) or not isinstance(self.model_calls, int):
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
                isinstance(self.horizon_seconds, bool)
                or not isinstance(self.horizon_seconds, int)
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


class NoTradeBaseline:
    """Deterministic null baseline that can never propose financial exposure."""

    def __init__(self, *, descriptor: StrategyDescriptor):
        if not isinstance(descriptor, StrategyDescriptor):
            raise TypeError("descriptor must be StrategyDescriptor")
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
        name = _text(symbol, name="symbol")
        cutoff = _time(decision_time, name="decision_time")
        if isinstance(evidence_event_ids, (str, bytes)) or not isinstance(
            evidence_event_ids,
            tuple,
        ):
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
            horizon_seconds=self.descriptor.horizon_seconds,
            expiry=cutoff + timedelta(seconds=self.descriptor.horizon_seconds),
            strategy_version=(
                f"{self.descriptor.strategy_id}@{self.descriptor.version}"
            ),
            strategy_fingerprint=self.descriptor.fingerprint,
            strategy_configuration_fingerprint=self.descriptor.fingerprint,
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
        if not isinstance(lookback, int) or isinstance(lookback, bool) or lookback < 2:
            raise ValueError("lookback must be an integer >= 2")
        self.lookback = lookback
        self.threshold = _decimal(threshold, name="threshold")
        if self.threshold < 0:
            raise ValueError("threshold must be non-negative")
        self.proposal_quantity = _decimal(proposal_quantity, name="proposal_quantity")
        if self.proposal_quantity <= 0:
            raise ValueError("proposal_quantity must be positive")
        if descriptor is not None:
            if not isinstance(descriptor, StrategyDescriptor):
                raise TypeError("descriptor must be StrategyDescriptor or None")
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
        if self.descriptor is None:
            return None
        body = {
            "descriptor_fingerprint": self.descriptor.fingerprint,
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
        cutoff = _time(simulation_time, name="simulation_time")
        if observation.available_at > cutoff:
            raise ValueError("observation is not causally available at simulation_time")
        existing = self._observations_by_id.get(observation.event_id)
        if existing is not None:
            if existing != observation:
                raise ValueError("event_id already exists with different observation content")
            return False
        history = self._history.setdefault(observation.symbol, [])
        if history and observation.available_at < history[-1].available_at:
            raise ValueError("observations must be ingested in non-decreasing availability order")
        history.append(observation)
        if len(history) > self.lookback:
            del history[:-self.lookback]
        self._observations_by_id[observation.event_id] = observation
        return True

    def propose(self, *, symbol: str, decision_time: datetime) -> DeterministicProposal:
        name = _text(symbol, name="symbol")
        cutoff = _time(decision_time, name="decision_time")
        history = self._history.get(name, [])
        eligible = [item for item in history if item.available_at <= cutoff]
        horizon_seconds = (
            self.descriptor.horizon_seconds if self.descriptor is not None else None
        )
        expiry = (
            cutoff + timedelta(seconds=horizon_seconds)
            if horizon_seconds is not None
            else None
        )
        strategy_version = (
            f"{self.descriptor.strategy_id}@{self.descriptor.version}"
            if self.descriptor is not None
            else None
        )
        strategy_fingerprint = (
            self.descriptor.fingerprint if self.descriptor is not None else None
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
        payload = {
            "schema_version": 5,
            "lookback": self.lookback,
            "threshold": str(self.threshold),
            "proposal_quantity": str(self.proposal_quantity),
            "descriptor": (
                None
                if self.descriptor is None
                else self.descriptor.canonical_document()
            ),
            "descriptor_fingerprint": (
                None
                if self.descriptor is None
                else self.descriptor.fingerprint
            ),
            "configuration_fingerprint": self.configuration_fingerprint,
            "seen_events": {
                event_id: {
                    "symbol": item.symbol,
                    "available_at": item.available_at.isoformat(),
                    "price": str(item.price),
                }
                for event_id, item in sorted(self._observations_by_id.items())
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
        if not isinstance(payload, dict) or payload.get("schema_version") not in {1, 2, 3, 4, 5}:
            raise ValueError("unsupported strategy snapshot")
        version = payload["schema_version"]
        expected = {"schema_version", "lookback", "threshold", "proposal_quantity", "history"}
        if version in {2, 3, 4, 5}:
            expected.add("seen_events")
        if version in {3, 4, 5}:
            expected.add("descriptor")
        if version in {4, 5}:
            expected.add("descriptor_fingerprint")
        if version == 5:
            expected.add("configuration_fingerprint")
        if set(payload) != expected or not isinstance(payload["history"], dict):
            raise ValueError("strategy snapshot structure is invalid")
        if version in {2, 3, 4, 5} and not isinstance(payload["seen_events"], dict):
            raise ValueError("strategy seen-event snapshot is invalid")
        descriptor = None
        if version in {3, 4, 5} and payload["descriptor"] is not None:
            raw_descriptor = payload["descriptor"]
            if not isinstance(raw_descriptor, dict):
                raise ValueError("strategy descriptor snapshot is invalid")
            try:
                descriptor = StrategyDescriptor(
                    strategy_id=raw_descriptor["strategy_id"],
                    version=raw_descriptor["version"],
                    family=raw_descriptor["family"],
                    feature_schema=raw_descriptor["feature_schema"],
                    market_requirements=tuple(raw_descriptor["market_requirements"]),
                    minimum_history=raw_descriptor["minimum_history"],
                    horizon_seconds=raw_descriptor["horizon_seconds"],
                    decision_schedule=raw_descriptor["decision_schedule"],
                    proposal_semantics=raw_descriptor["proposal_semantics"],
                    parameter_bounds=tuple(
                        (
                            item["name"],
                            item["minimum"],
                            item["maximum"],
                        )
                        for item in raw_descriptor["parameter_bounds"]
                    ),
                    resource_profile=raw_descriptor["resource_profile"],
                    supported_regimes=tuple(raw_descriptor["supported_regimes"]),
                    source_license=raw_descriptor["source_license"],
                    evaluation_protocol_sha256=raw_descriptor[
                        "evaluation_protocol_sha256"
                    ],
                    artifact_sha256=raw_descriptor["artifact_sha256"],
                )
            except (KeyError, TypeError, ValueError) as error:
                raise ValueError("strategy descriptor snapshot is invalid") from error
        if version in {4, 5}:
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
        if version == 5:
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

        if version in {2, 3, 4, 5}:
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



def _utc_text(value: datetime) -> str:
    normalized = _time(value, name="timestamp")
    return normalized.isoformat().replace("+00:00", "Z")


def to_decision_proposal(
    proposal: DeterministicProposal,
    *,
    proposal_id: str,
    instrument_version: str,
    input_manifest_refs: tuple[str, ...],
    expected_return_distribution_ref: str,
    exit_policy_ref: str,
    compute_cost_currency: str,
    counterarguments: tuple[str, ...] = (),
) -> dict[str, object]:
    """Project a registered zero-model result into canonical DecisionProposal shape.

    This adapter does not invent an expected-return distribution, exit policy,
    instrument identity or input manifest. Those remain explicit external
    evidence. A HOLD result becomes a canonical NO_TRADE proposal and never
    gains an executable candidate instrument from its display symbol.
    """

    if not isinstance(proposal, DeterministicProposal):
        raise TypeError("proposal must be DeterministicProposal")
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
    distribution_ref = _text(
        expected_return_distribution_ref,
        name="expected_return_distribution_ref",
    )
    exit_ref = _text(exit_policy_ref, name="exit_policy_ref")
    currency = _text(compute_cost_currency, name="compute_cost_currency")
    if isinstance(input_manifest_refs, (str, bytes)) or not isinstance(
        input_manifest_refs,
        tuple,
    ):
        raise ValueError("input_manifest_refs must be a tuple")
    manifests = tuple(
        _digest(value, name="input_manifest_ref")
        for value in input_manifest_refs
    )
    if not manifests or len(set(manifests)) != len(manifests):
        raise ValueError(
            "input_manifest_refs must contain unique immutable evidence"
        )
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

    no_trade = proposal.action == "HOLD"
    body: dict[str, object] = {
        "proposal_id": normalized_proposal_id,
        "strategy_version": proposal.strategy_version,
        "decision_at": _utc_text(proposal.decision_time),
        "information_cutoff": _utc_text(proposal.information_cutoff),
        "input_manifest_refs": list(manifests),
        "thesis": proposal.reason,
        "candidate_instruments": [] if no_trade else [instrument],
        "horizon": f"PT{proposal.horizon_seconds}S",
        "expected_return_distribution_ref": distribution_ref,
        "confidence_basis": {
            "economic_edge_claim": proposal.economic_edge_claim,
            "strategy_fingerprint": proposal.strategy_fingerprint,
            "strategy_configuration_fingerprint": (
                proposal.strategy_configuration_fingerprint
            ),
            "model_calls": proposal.model_calls,
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
        body["NO_TRADE_reason"] = proposal.reason
    return body
