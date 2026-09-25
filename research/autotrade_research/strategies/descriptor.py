"""Versioned descriptors for deterministic zero-model strategies.

This module carries research metadata only. It grants no execution authority and
makes no economic-edge claim. Descriptor serialization is canonical so the same
bounded strategy definition can be hashed into experiment evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re

from .deterministic import DeterministicProposal, ReturnThresholdBaseline

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _text(value: str, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    return value.strip()


def _integer(value, *, name: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return value


def _decimal(value, *, name: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError(f"{name} must use Decimal, string or integer input")
    try:
        result = value if isinstance(value, Decimal) else Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError(f"{name} must be a finite decimal") from error
    if not result.is_finite():
        raise ValueError(f"{name} must be a finite decimal")
    return result


@dataclass(frozen=True)
class ParameterBound:
    name: str
    minimum: Decimal
    maximum: Decimal
    value: Decimal

    def __post_init__(self) -> None:
        name = _text(self.name, name="parameter name")
        minimum = _decimal(self.minimum, name=f"{name}.minimum")
        maximum = _decimal(self.maximum, name=f"{name}.maximum")
        value = _decimal(self.value, name=f"{name}.value")
        if minimum > maximum:
            raise ValueError(f"{name} minimum exceeds maximum")
        if value < minimum or value > maximum:
            raise ValueError(f"{name} value is outside registered bounds")
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "minimum", minimum)
        object.__setattr__(self, "maximum", maximum)
        object.__setattr__(self, "value", value)


@dataclass(frozen=True)
class ArtifactHash:
    role: str
    sha256: str

    def __post_init__(self) -> None:
        role = _text(self.role, name="artifact role")
        digest = _text(self.sha256, name="artifact sha256").lower()
        if not _SHA256.fullmatch(digest):
            raise ValueError("artifact sha256 must be exactly 64 lowercase hex characters")
        object.__setattr__(self, "role", role)
        object.__setattr__(self, "sha256", digest)


@dataclass(frozen=True)
class StrategyDescriptor:
    strategy_id: str
    strategy_version: str
    feature_schema: str
    instrument_requirements: tuple[str, ...]
    market_requirements: tuple[str, ...]
    minimum_history: int
    horizon: str
    decision_schedule: str
    position_semantics: str
    exit_semantics: str
    parameter_bounds: tuple[ParameterBound, ...]
    resource_profile: str
    supported_regimes: tuple[str, ...]
    source: str
    license_status: str
    evaluation_protocol: str
    artifact_hashes: tuple[ArtifactHash, ...]
    model_policy: str = "ZERO_MODEL_ONLY"
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("unsupported strategy descriptor schema_version")
        for field_name in (
            "strategy_id",
            "strategy_version",
            "feature_schema",
            "horizon",
            "decision_schedule",
            "position_semantics",
            "exit_semantics",
            "resource_profile",
            "source",
            "license_status",
            "evaluation_protocol",
        ):
            object.__setattr__(self, field_name, _text(getattr(self, field_name), name=field_name))
        if self.model_policy != "ZERO_MODEL_ONLY":
            raise ValueError("deterministic descriptor must use ZERO_MODEL_ONLY")
        object.__setattr__(self, "minimum_history", _integer(self.minimum_history, name="minimum_history", minimum=2))

        for field_name in ("instrument_requirements", "market_requirements", "supported_regimes"):
            values = getattr(self, field_name)
            if not isinstance(values, tuple) or not values:
                raise ValueError(f"{field_name} must be a non-empty tuple")
            normalized = tuple(_text(item, name=field_name) for item in values)
            if len(set(normalized)) != len(normalized):
                raise ValueError(f"{field_name} contains duplicates")
            object.__setattr__(self, field_name, normalized)

        if not isinstance(self.parameter_bounds, tuple) or not self.parameter_bounds:
            raise ValueError("parameter_bounds must be a non-empty tuple")
        names = [item.name for item in self.parameter_bounds]
        if len(set(names)) != len(names):
            raise ValueError("parameter_bounds contains duplicate names")

        if not isinstance(self.artifact_hashes, tuple) or not self.artifact_hashes:
            raise ValueError("artifact_hashes must be a non-empty tuple")
        roles = [item.role for item in self.artifact_hashes]
        if len(set(roles)) != len(roles):
            raise ValueError("artifact_hashes contains duplicate roles")

    def canonical_payload(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
            "feature_schema": self.feature_schema,
            "instrument_requirements": list(self.instrument_requirements),
            "market_requirements": list(self.market_requirements),
            "minimum_history": self.minimum_history,
            "horizon": self.horizon,
            "decision_schedule": self.decision_schedule,
            "position_semantics": self.position_semantics,
            "exit_semantics": self.exit_semantics,
            "parameter_bounds": [
                {
                    "name": item.name,
                    "minimum": str(item.minimum),
                    "maximum": str(item.maximum),
                    "value": str(item.value),
                }
                for item in sorted(self.parameter_bounds, key=lambda item: item.name)
            ],
            "resource_profile": self.resource_profile,
            "supported_regimes": list(self.supported_regimes),
            "source": self.source,
            "license_status": self.license_status,
            "evaluation_protocol": self.evaluation_protocol,
            "artifact_hashes": [
                {"role": item.role, "sha256": item.sha256}
                for item in sorted(self.artifact_hashes, key=lambda item: item.role)
            ],
            "model_policy": self.model_policy,
        }

    def canonical_json(self) -> str:
        return json.dumps(
            self.canonical_payload(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )

    def digest(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


def describe_return_threshold(
    strategy: ReturnThresholdBaseline,
    *,
    strategy_version: str,
    feature_schema: str,
    instrument_requirements: tuple[str, ...],
    market_requirements: tuple[str, ...],
    horizon: str,
    decision_schedule: str,
    supported_regimes: tuple[str, ...],
    source: str,
    license_status: str,
    evaluation_protocol: str,
    artifact_hashes: tuple[ArtifactHash, ...],
    threshold_bounds=("0", "1"),
    quantity_bounds=("0.00000001", "1000000000"),
) -> StrategyDescriptor:
    """Describe the exact bounded deterministic baseline without changing it."""

    if not isinstance(strategy, ReturnThresholdBaseline):
        raise TypeError("strategy must be ReturnThresholdBaseline")
    threshold_min, threshold_max = threshold_bounds
    quantity_min, quantity_max = quantity_bounds
    return StrategyDescriptor(
        strategy_id="return-threshold-baseline",
        strategy_version=strategy_version,
        feature_schema=feature_schema,
        instrument_requirements=instrument_requirements,
        market_requirements=market_requirements,
        minimum_history=strategy.lookback,
        horizon=horizon,
        decision_schedule=decision_schedule,
        position_semantics="BUY/SELL/HOLD proposal only; no execution authority",
        exit_semantics="exit behavior is not implied by this signal descriptor",
        parameter_bounds=(
            ParameterBound(
                name="lookback",
                minimum=Decimal("2"),
                maximum=Decimal("1000000"),
                value=Decimal(strategy.lookback),
            ),
            ParameterBound(
                name="proposal_quantity",
                minimum=quantity_min,
                maximum=quantity_max,
                value=strategy.proposal_quantity,
            ),
            ParameterBound(
                name="threshold",
                minimum=threshold_min,
                maximum=threshold_max,
                value=strategy.threshold,
            ),
        ),
        resource_profile="deterministic-cpu-memory-bounded; zero model calls",
        supported_regimes=supported_regimes,
        source=source,
        license_status=license_status,
        evaluation_protocol=evaluation_protocol,
        artifact_hashes=artifact_hashes,
    )


def propose_from_snapshot(
    *,
    descriptor: StrategyDescriptor,
    snapshot: str,
    symbol: str,
    decision_time: datetime,
) -> DeterministicProposal:
    """Pure proposal path over a serialized bounded state.

    The descriptor must match the strategy state parameters. This prevents a
    result from being attributed to a different registered configuration.
    """

    if not isinstance(descriptor, StrategyDescriptor):
        raise TypeError("descriptor must be StrategyDescriptor")
    strategy = ReturnThresholdBaseline.restore(snapshot)
    registered = {item.name: item.value for item in descriptor.parameter_bounds}
    required = {"lookback", "threshold", "proposal_quantity"}
    if set(registered) != required:
        raise ValueError("descriptor parameter set does not match return-threshold strategy")
    if registered["lookback"] != Decimal(strategy.lookback):
        raise ValueError("descriptor lookback does not match snapshot")
    if registered["threshold"] != strategy.threshold:
        raise ValueError("descriptor threshold does not match snapshot")
    if registered["proposal_quantity"] != strategy.proposal_quantity:
        raise ValueError("descriptor proposal_quantity does not match snapshot")
    return strategy.propose(symbol=symbol, decision_time=decision_time)
