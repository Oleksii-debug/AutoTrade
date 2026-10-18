"""Context-scoped strategy applicability over canonical scientific gate decisions.

This module implements the product rule that strategies are tools, not dogma.
It does not score returns, prove economic edge, promote a model, or grant trading
authority.  It only composes already-issued scientific GateDecision objects into
a comparable asset/regime/horizon view and fails closed when that comparison is
not complete.
"""
from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import datetime, timezone
from hashlib import sha256
import json
import re
from types import MappingProxyType
from typing import Mapping
from uuid import UUID

from ..strategies.deterministic import StrategyDescriptor
from .gates import GateDecision


_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_MAPPING_PROXY_TYPE = type(MappingProxyType({}))
_DECISION_STATUSES = frozenset(
    {"ONE_ELIGIBLE", "MULTIPLE_ELIGIBLE", "NO_ELIGIBLE", "INCONCLUSIVE"}
)


def _text(value: str, *, name: str) -> str:
    if type(value) is not str or value != value.strip() or not value:
        raise ValueError(f"{name} must be exact non-empty text")
    return value


def _digest(value: str, *, name: str) -> str:
    text = _text(value, name=name)
    if _SHA256.fullmatch(text) is None:
        raise ValueError(f"{name} must be canonical sha256:<64 lowercase hex>")
    return text


def _git_sha(value: str, *, name: str) -> str:
    text = _text(value, name=name)
    if _GIT_SHA.fullmatch(text) is None:
        raise ValueError(f"{name} must be a lowercase 40-character Git SHA")
    return text


def _uuid(value: str, *, name: str) -> str:
    text = _text(value, name=name)
    try:
        normalized = str(UUID(text))
    except (ValueError, AttributeError, TypeError) as error:
        raise ValueError(f"{name} must be a canonical UUID") from error
    if normalized != text:
        raise ValueError(f"{name} must be a canonical UUID")
    return text


def _utc_instant(value: str, *, name: str) -> str:
    text = _text(value, name=name)
    if not text.endswith("Z"):
        raise ValueError(f"{name} must be an exact UTC instant ending in Z")
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as error:
        raise ValueError(f"{name} must be an ISO-8601 UTC instant") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must include UTC")
    normalized = parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if normalized != text:
        raise ValueError(f"{name} must use canonical UTC ISO-8601 form")
    return text


def _canonical_json(value) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _detach_strategy(value: StrategyDescriptor) -> StrategyDescriptor:
    """Revalidate one exact frozen descriptor before trusting authority fields."""

    if type(value) is not StrategyDescriptor:
        raise TypeError("strategy must be exact StrategyDescriptor")
    state = object.__getattribute__(value, "__dict__")
    expected = {field.name for field in fields(StrategyDescriptor)}
    if type(state) is not dict or set(state) != expected:
        raise TypeError("StrategyDescriptor has unexpected state fields")

    text_fields = (
        "strategy_id",
        "family",
        "feature_schema",
        "decision_schedule",
        "proposal_semantics",
        "resource_profile",
        "source_license",
        "evaluation_protocol_sha256",
        "artifact_sha256",
    )
    for name in text_fields:
        if type(state[name]) is not str:
            raise TypeError(f"strategy {name} must be exact text")
    for name in ("version", "minimum_history", "horizon_seconds"):
        if type(state[name]) is not int:
            raise TypeError(f"strategy {name} must be exact integer")
    for name in ("market_requirements", "supported_regimes"):
        value_tuple = state[name]
        if type(value_tuple) is not tuple or any(type(item) is not str for item in value_tuple):
            raise TypeError(f"strategy {name} must be an exact text tuple")
    bounds = state["parameter_bounds"]
    if type(bounds) is not tuple:
        raise TypeError("strategy parameter_bounds must be exact tuple")
    for bound in bounds:
        if (
            type(bound) is not tuple
            or len(bound) != 3
            or any(type(item) is not str for item in bound)
        ):
            raise TypeError("strategy parameter_bounds contains unsafe state")
    return StrategyDescriptor(**state)


def _detach_gate(value: GateDecision) -> GateDecision:
    """Copy one canonical gate decision without dispatching through hostile inputs."""

    if type(value) is not GateDecision:
        raise TypeError("gate_decision must be exact GateDecision")
    state = object.__getattribute__(value, "__dict__")
    expected = {field.name for field in fields(GateDecision)}
    if type(state) is not dict or set(state) != expected:
        raise TypeError("GateDecision has unexpected state fields")
    if type(state["status"]) is not str:
        raise TypeError("GateDecision status must be exact text")
    reasons = state["reasons"]
    if type(reasons) is not tuple or any(type(reason) is not str for reason in reasons):
        raise TypeError("GateDecision reasons must be exact text tuple")
    checks = state["checks"]
    provenance = state["provenance"]
    if type(checks) is not _MAPPING_PROXY_TYPE:
        raise TypeError("GateDecision checks must retain immutable canonical storage")
    if type(provenance) is not _MAPPING_PROXY_TYPE:
        raise TypeError("GateDecision provenance must retain immutable canonical storage")
    for key, item in checks.items():
        if type(key) is not str or type(item) is not str:
            raise TypeError("GateDecision checks contain unsafe state")
    for key, item in provenance.items():
        if type(key) is not str or type(item) is not str:
            raise TypeError("GateDecision provenance contains unsafe state")
    return GateDecision(
        status=state["status"],
        reasons=reasons,
        checks=dict(checks),
        provenance=dict(provenance),
    )


def _gate_provenance(decision: GateDecision) -> tuple[str, str, str]:
    provenance = decision.provenance
    bundle_id = provenance.get("evidence_bundle_id")
    graph_digest = provenance.get("evidence_graph_digest")
    source_sha = provenance.get("review_source_sha")
    if type(bundle_id) is not str or not bundle_id:
        raise ValueError("gate decision lacks verified evidence_bundle_id provenance")
    return (
        _uuid(bundle_id, name="gate evidence_bundle_id"),
        _digest(graph_digest, name="gate evidence_graph_digest"),
        _git_sha(source_sha, name="gate review_source_sha"),
    )


@dataclass(frozen=True)
class StrategyToolCell:
    """One exact strategy evaluation in one product-relevant context."""

    strategy: StrategyDescriptor
    asset_class: str
    regime_id: str
    evaluation_cut: str
    gate_decision: GateDecision

    def __post_init__(self) -> None:
        strategy = _detach_strategy(self.strategy)
        asset_class = _text(self.asset_class, name="asset_class")
        regime_id = _text(self.regime_id, name="regime_id")
        evaluation_cut = _utc_instant(self.evaluation_cut, name="evaluation_cut")
        gate = _detach_gate(self.gate_decision)
        _gate_provenance(gate)
        if regime_id not in strategy.supported_regimes:
            raise ValueError("regime_id is outside the strategy descriptor's supported_regimes")
        object.__setattr__(self, "strategy", strategy)
        object.__setattr__(self, "asset_class", asset_class)
        object.__setattr__(self, "regime_id", regime_id)
        object.__setattr__(self, "evaluation_cut", evaluation_cut)
        object.__setattr__(self, "gate_decision", gate)

    @property
    def context_key(self) -> tuple[str, str, int]:
        return (self.asset_class, self.regime_id, self.strategy.horizon_seconds)

    @property
    def candidate_key(self) -> tuple[str, int, str]:
        return (
            self.strategy.strategy_id,
            self.strategy.version,
            self.strategy.fingerprint,
        )

    @property
    def cell_digest(self) -> str:
        bundle_id, graph_digest, source_sha = _gate_provenance(self.gate_decision)
        payload = {
            "asset_class": self.asset_class,
            "candidate": {
                "strategy_id": self.strategy.strategy_id,
                "version": self.strategy.version,
                "fingerprint": self.strategy.fingerprint,
                "family": self.strategy.family,
                "artifact_sha256": self.strategy.artifact_sha256,
                "evaluation_protocol_sha256": self.strategy.evaluation_protocol_sha256,
            },
            "evaluation_cut": self.evaluation_cut,
            "evidence": {
                "bundle_id": bundle_id,
                "graph_digest": graph_digest,
                "review_source_sha": source_sha,
            },
            "gate": {
                "status": self.gate_decision.status,
                "reasons": list(self.gate_decision.reasons),
                "checks": dict(self.gate_decision.checks),
                "provenance": dict(self.gate_decision.provenance),
            },
            "horizon_seconds": self.strategy.horizon_seconds,
            "regime_id": self.regime_id,
        }
        return "sha256:" + sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class StrategyContextDecision:
    """Fail-closed context view; never a financial authorization."""

    status: str
    asset_class: str
    regime_id: str
    horizon_seconds: int
    eligible_fingerprints: tuple[str, ...]
    failed_fingerprints: tuple[str, ...]
    inconclusive_fingerprints: tuple[str, ...]
    matrix_digest: str
    context_digest: str
    grants_trading_authority: bool = False
    economic_edge_status: str = "NOT_ESTABLISHED"

    def __post_init__(self) -> None:
        if type(self.status) is not str or self.status not in _DECISION_STATUSES:
            raise ValueError("unsupported strategy context decision status")
        _text(self.asset_class, name="asset_class")
        _text(self.regime_id, name="regime_id")
        if type(self.horizon_seconds) is not int or self.horizon_seconds <= 0:
            raise ValueError("horizon_seconds must be a positive exact integer")
        for name in (
            "eligible_fingerprints",
            "failed_fingerprints",
            "inconclusive_fingerprints",
        ):
            values = getattr(self, name)
            if type(values) is not tuple or any(type(item) is not str for item in values):
                raise TypeError(f"{name} must be an exact text tuple")
            if tuple(sorted(values)) != values or len(set(values)) != len(values):
                raise ValueError(f"{name} must be unique and sorted")
            for value in values:
                _digest(value, name=f"{name} item")
        sets = (
            set(self.eligible_fingerprints),
            set(self.failed_fingerprints),
            set(self.inconclusive_fingerprints),
        )
        if sets[0] & sets[1] or sets[0] & sets[2] or sets[1] & sets[2]:
            raise ValueError("strategy context classifications must be disjoint")
        _digest(self.matrix_digest, name="matrix_digest")
        _digest(self.context_digest, name="context_digest")
        if self.grants_trading_authority is not False:
            raise ValueError("strategy context decisions cannot grant trading authority")
        if self.economic_edge_status != "NOT_ESTABLISHED":
            raise ValueError("strategy context decisions cannot claim economic edge")


@dataclass(frozen=True)
class StrategyToolMatrix:
    """Comparable, immutable strategy evidence cells for one scientific cut."""

    cells: tuple[StrategyToolCell, ...]

    def __post_init__(self) -> None:
        if type(self.cells) is not tuple or not self.cells:
            raise ValueError("cells must be a non-empty exact tuple")

        detached: list[StrategyToolCell] = []
        seen: set[tuple[str, str, int, str]] = set()
        version_identities: dict[tuple[str, int], str] = {}
        protocol = None
        source_sha = None
        evaluation_cut = None

        for raw_cell in self.cells:
            if type(raw_cell) is not StrategyToolCell:
                raise TypeError("cells must contain exact StrategyToolCell values")
            state = object.__getattribute__(raw_cell, "__dict__")
            expected = {field.name for field in fields(StrategyToolCell)}
            if type(state) is not dict or set(state) != expected:
                raise TypeError("StrategyToolCell has unexpected state fields")
            cell = StrategyToolCell(
                strategy=state["strategy"],
                asset_class=state["asset_class"],
                regime_id=state["regime_id"],
                evaluation_cut=state["evaluation_cut"],
                gate_decision=state["gate_decision"],
            )
            key = (
                cell.strategy.fingerprint,
                cell.asset_class,
                cell.regime_id,
                cell.strategy.horizon_seconds,
            )
            if key in seen:
                raise ValueError("duplicate strategy/context evaluation cell")
            seen.add(key)
            version_key = (cell.strategy.strategy_id, cell.strategy.version)
            prior_fingerprint = version_identities.get(version_key)
            if prior_fingerprint is not None and prior_fingerprint != cell.strategy.fingerprint:
                raise ValueError(
                    "one strategy_id/version cannot identify multiple strategy fingerprints"
                )
            version_identities[version_key] = cell.strategy.fingerprint
            _, _, cell_source_sha = _gate_provenance(cell.gate_decision)
            cell_protocol = cell.strategy.evaluation_protocol_sha256
            if protocol is None:
                protocol = cell_protocol
                source_sha = cell_source_sha
                evaluation_cut = cell.evaluation_cut
            elif (
                cell_protocol != protocol
                or cell_source_sha != source_sha
                or cell.evaluation_cut != evaluation_cut
            ):
                raise ValueError(
                    "strategy cells are not comparable on protocol, source SHA and evaluation cut"
                )
            detached.append(cell)

        detached.sort(
            key=lambda cell: (
                cell.asset_class,
                cell.regime_id,
                cell.strategy.horizon_seconds,
                cell.strategy.strategy_id,
                cell.strategy.version,
                cell.strategy.fingerprint,
            )
        )
        object.__setattr__(self, "cells", tuple(detached))

    @property
    def matrix_digest(self) -> str:
        payload = {"cell_digests": [cell.cell_digest for cell in self.cells]}
        return "sha256:" + sha256(_canonical_json(payload).encode("utf-8")).hexdigest()

    def decision_for_context(
        self,
        *,
        asset_class: str,
        regime_id: str,
        horizon_seconds: int,
    ) -> StrategyContextDecision:
        asset = _text(asset_class, name="asset_class")
        regime = _text(regime_id, name="regime_id")
        if type(horizon_seconds) is not int or horizon_seconds <= 0:
            raise ValueError("horizon_seconds must be a positive exact integer")

        matching = tuple(
            cell
            for cell in self.cells
            if cell.asset_class == asset
            and cell.regime_id == regime
            and cell.strategy.horizon_seconds == horizon_seconds
        )

        eligible = tuple(
            sorted(
                cell.strategy.fingerprint
                for cell in matching
                if cell.gate_decision.status == "PASS"
            )
        )
        failed = tuple(
            sorted(
                cell.strategy.fingerprint
                for cell in matching
                if cell.gate_decision.status == "FAIL"
            )
        )
        inconclusive = tuple(
            sorted(
                cell.strategy.fingerprint
                for cell in matching
                if cell.gate_decision.status == "INCONCLUSIVE"
            )
        )

        if not matching or inconclusive:
            status = "INCONCLUSIVE"
        elif not eligible:
            status = "NO_ELIGIBLE"
        elif len(eligible) == 1:
            status = "ONE_ELIGIBLE"
        else:
            status = "MULTIPLE_ELIGIBLE"

        context_payload = {
            "asset_class": asset,
            "regime_id": regime,
            "horizon_seconds": horizon_seconds,
            "matrix_digest": self.matrix_digest,
            "status": status,
            "eligible_fingerprints": list(eligible),
            "failed_fingerprints": list(failed),
            "inconclusive_fingerprints": list(inconclusive),
            "grants_trading_authority": False,
            "economic_edge_status": "NOT_ESTABLISHED",
        }
        context_digest = "sha256:" + sha256(
            _canonical_json(context_payload).encode("utf-8")
        ).hexdigest()
        return StrategyContextDecision(
            status=status,
            asset_class=asset,
            regime_id=regime,
            horizon_seconds=horizon_seconds,
            eligible_fingerprints=eligible,
            failed_fingerprints=failed,
            inconclusive_fingerprints=inconclusive,
            matrix_digest=self.matrix_digest,
            context_digest=context_digest,
        )
