"""Replay-derived capacity consistency evidence for strategy economics.

This module does not create a second allocator, select the product capacity
policy, or authorize trading. It snapshots one research proposal plus caller-
presented allocation inputs, reruns the existing canonical allocator, and issues
a tamper-evident digest for the resulting non-expansive capacity calculation.
Terminal composition may verify replay consistency, but the independent
product-selected capacity owner remains unresolved.
"""

from __future__ import annotations

from dataclasses import InitVar, dataclass, replace
from decimal import Decimal
from hashlib import sha256
import json
from threading import Lock
import weakref

from mvp.autotrade_mvp.allocation import (
    AllocationCandidate,
    AllocationPolicy,
    AllocationResult,
    StressScenarioEvidence,
    allocate_targets,
)
from mvp.autotrade_mvp.exact_decimal import (
    ExactDecimalError,
    canonical_decimal_text,
    exact_abs,
    exact_multiply,
    exact_subtract,
)
from research.autotrade_research.strategies.deterministic import DeterministicProposal


class StrategyCapacityAuthorityError(ValueError):
    """Capacity evidence is malformed, detached, or not canonically issued."""


_ALLOCATE_TARGETS = allocate_targets
_ISSUE_TOKEN = object()
_ISSUED_LOCK = Lock()
_ISSUED: dict[
    int,
    tuple[
        weakref.ReferenceType["AllocationCapacityEvidence"],
        tuple[object, ...],
    ],
] = {}


def _text(value: object, *, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise StrategyCapacityAuthorityError(f"{name} must be exact non-empty text")
    return value


def _digest_text(value: object, *, name: str) -> str:
    text = _text(value, name=name)
    if (
        len(text) != 71
        or not text.startswith("sha256:")
        or text.lower() != text
        or any(character not in "0123456789abcdef" for character in text[7:])
    ):
        raise StrategyCapacityAuthorityError(
            f"{name} must be canonical sha256:<64 lowercase hex>"
        )
    return text


def _decimal_text(value: Decimal) -> str:
    try:
        return canonical_decimal_text(value)
    except ExactDecimalError as error:
        raise StrategyCapacityAuthorityError(
            "capacity evidence exceeds exact-decimal resource envelope"
        ) from error


def _utc_text(value) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _snapshot_proposal(value: object) -> DeterministicProposal:
    if type(value) is not DeterministicProposal:
        raise TypeError("proposal must be exact DeterministicProposal")
    try:
        proposal = replace(value)
    except (TypeError, ValueError) as error:
        raise StrategyCapacityAuthorityError(
            "proposal cannot be reconstructed canonically"
        ) from error
    if (
        proposal.strategy_fingerprint is None
        or proposal.strategy_configuration_fingerprint is None
        or proposal.information_cutoff is None
        or proposal.horizon_seconds is None
        or proposal.expiry is None
    ):
        raise StrategyCapacityAuthorityError(
            "capacity evidence requires registered strategy and horizon identity"
        )
    return proposal


def _snapshot_candidate(value: object) -> AllocationCandidate:
    if type(value) is not AllocationCandidate:
        raise TypeError("candidate must be exact AllocationCandidate")
    try:
        return replace(value)
    except (TypeError, ValueError) as error:
        raise StrategyCapacityAuthorityError(
            "allocation candidate cannot be reconstructed canonically"
        ) from error


def _snapshot_policy(value: object) -> AllocationPolicy:
    if type(value) is not AllocationPolicy:
        raise TypeError("policy must be exact AllocationPolicy")
    try:
        return replace(value)
    except (TypeError, ValueError) as error:
        raise StrategyCapacityAuthorityError(
            "allocation policy cannot be reconstructed canonically"
        ) from error


def _snapshot_stress(values: object) -> tuple[StressScenarioEvidence, ...]:
    if type(values) is not tuple:
        raise TypeError("stress_evidence must be an exact tuple")
    result: list[StressScenarioEvidence] = []
    for value in values:
        if type(value) is not StressScenarioEvidence:
            raise TypeError(
                "stress_evidence must contain exact StressScenarioEvidence"
            )
        try:
            result.append(replace(value))
        except (TypeError, ValueError) as error:
            raise StrategyCapacityAuthorityError(
                "stress evidence cannot be reconstructed canonically"
            ) from error
    return tuple(result)


def _candidate_document(value: AllocationCandidate) -> dict[str, object]:
    return {
        "symbol": value.symbol,
        "desired_notional": _decimal_text(value.desired_notional),
        "price": _decimal_text(value.price),
        "lot_size": _decimal_text(value.lot_size),
        "cost_rate": _decimal_text(value.cost_rate),
        "capital_requirement_rate": _decimal_text(value.capital_requirement_rate),
        "min_notional": _decimal_text(value.min_notional),
        "fee_floor": _decimal_text(value.fee_floor),
        "max_executable_notional": (
            None
            if value.max_executable_notional is None
            else _decimal_text(value.max_executable_notional)
        ),
        "current_quantity": _decimal_text(value.current_quantity),
        "turnover_cost_rate": (
            None
            if value.turnover_cost_rate is None
            else _decimal_text(value.turnover_cost_rate)
        ),
        "holding_cost_rate": (
            None
            if value.holding_cost_rate is None
            else _decimal_text(value.holding_cost_rate)
        ),
    }


def _policy_document(value: AllocationPolicy) -> dict[str, object]:
    return {
        "cash_available": _decimal_text(value.cash_available),
        "max_gross_notional": _decimal_text(value.max_gross_notional),
        "max_net_notional": _decimal_text(value.max_net_notional),
        "max_symbol_notional": _decimal_text(value.max_symbol_notional),
        "max_total_cost": _decimal_text(value.max_total_cost),
        "max_stress_loss": _decimal_text(value.max_stress_loss),
        "stress_loss_penalty_rate": _decimal_text(value.stress_loss_penalty_rate),
        "max_turnover_notional": (
            None
            if value.max_turnover_notional is None
            else _decimal_text(value.max_turnover_notional)
        ),
        "minimum_cash_reserve": _decimal_text(value.minimum_cash_reserve),
        "max_iterations": value.max_iterations,
        "min_scale_tolerance": _decimal_text(value.min_scale_tolerance),
        "require_adverse_stress_evidence": value.require_adverse_stress_evidence,
        "require_fresh_stress_evidence": value.require_fresh_stress_evidence,
        "max_execution_states": value.max_execution_states,
    }


def _stress_document(
    values: tuple[StressScenarioEvidence, ...],
) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for value in sorted(values, key=lambda item: item.name):
        result.append(
            {
                "name": value.name,
                "shocks": {
                    key: _decimal_text(amount)
                    for key, amount in sorted(value.shocks.items())
                },
                "observed_at": value.observed_at,
                "valid_until": value.valid_until,
                "source_ref": value.source_ref,
            }
        )
    return result


def _result_document(value: AllocationResult) -> dict[str, object]:
    return {
        "status": value.status,
        "scale": _decimal_text(value.scale),
        "targets": [
            {
                "symbol": item.symbol,
                "quantity": _decimal_text(item.quantity),
                "notional": _decimal_text(item.notional),
                "estimated_cost": _decimal_text(item.estimated_cost),
                "turnover_notional": _decimal_text(item.turnover_notional),
            }
            for item in value.targets
        ],
        "gross_notional": _decimal_text(value.gross_notional),
        "net_notional": _decimal_text(value.net_notional),
        "estimated_cost": _decimal_text(value.estimated_cost),
        "worst_stress_loss": (
            None
            if value.worst_stress_loss is None
            else _decimal_text(value.worst_stress_loss)
        ),
        "cash_required": _decimal_text(value.cash_required),
        "reason": value.reason,
        "turnover_notional": _decimal_text(value.turnover_notional),
    }


@dataclass(frozen=True, slots=True, weakref_slot=True)
class AllocationCapacityEvidence:
    """Module-issued replay-consistency proof for one flat-baseline cut."""

    digest: str
    instrument_version: str
    strategy_fingerprint: str
    strategy_configuration_fingerprint: str
    symbol: str
    action: str
    decision_time: str
    information_cutoff: str
    horizon_seconds: int
    proposal_quantity: str
    max_feasible_quantity: str
    lot_size: str
    allocation_status: str
    allocation_reason: str
    _token: InitVar[object | None] = None

    def __post_init__(self, _token: object | None) -> None:
        if _token is not _ISSUE_TOKEN:
            raise StrategyCapacityAuthorityError(
                "capacity evidence must be issued canonically"
            )
        _digest_text(self.digest, name="capacity evidence digest")
        for name in (
            "instrument_version",
            "strategy_fingerprint",
            "strategy_configuration_fingerprint",
            "symbol",
            "action",
            "decision_time",
            "information_cutoff",
            "proposal_quantity",
            "max_feasible_quantity",
            "lot_size",
            "allocation_status",
            "allocation_reason",
        ):
            _text(getattr(self, name), name=name)
        _digest_text(self.strategy_fingerprint, name="strategy_fingerprint")
        _digest_text(
            self.strategy_configuration_fingerprint,
            name="strategy_configuration_fingerprint",
        )
        if type(self.horizon_seconds) is not int or self.horizon_seconds <= 0:
            raise StrategyCapacityAuthorityError(
                "horizon_seconds must be a positive integer"
            )


def _issued_seal(value: AllocationCapacityEvidence) -> tuple[object, ...]:
    return (
        value.digest,
        value.instrument_version,
        value.strategy_fingerprint,
        value.strategy_configuration_fingerprint,
        value.symbol,
        value.action,
        value.decision_time,
        value.information_cutoff,
        value.horizon_seconds,
        value.proposal_quantity,
        value.max_feasible_quantity,
        value.lot_size,
        value.allocation_status,
        value.allocation_reason,
    )


def _register_issued(
    value: AllocationCapacityEvidence,
) -> AllocationCapacityEvidence:
    identity = id(value)
    seal = _issued_seal(value)

    def cleanup(reference) -> None:
        with _ISSUED_LOCK:
            current = _ISSUED.get(identity)
            if current is not None and current[0] is reference:
                _ISSUED.pop(identity, None)

    reference = weakref.ref(value, cleanup)
    with _ISSUED_LOCK:
        _ISSUED[identity] = (reference, seal)
    return value


def require_allocation_capacity_evidence(
    value: object,
) -> AllocationCapacityEvidence:
    if type(value) is not AllocationCapacityEvidence:
        raise TypeError("value must be exact AllocationCapacityEvidence")
    with _ISSUED_LOCK:
        row = _ISSUED.get(id(value))
        if row is None or row[0]() is not value or row[1] != _issued_seal(value):
            raise StrategyCapacityAuthorityError(
                "capacity evidence is unissued or changed"
            )
    return value


def issue_allocation_capacity_evidence(
    proposal: DeterministicProposal,
    *,
    instrument_version: str,
    candidate: AllocationCandidate,
    policy: AllocationPolicy,
    stress_evidence: tuple[StressScenarioEvidence, ...] = (),
) -> AllocationCapacityEvidence:
    """Replay canonical allocation and issue one non-expansive capacity digest.

    The digest proves exact replay of the presented inputs; it does not prove
    that the presented AllocationPolicy is the product-selected capacity owner.
    This boundary intentionally supports flat research baselines only. Existing
    portfolio holdings require separate portfolio/current-position authority and
    are not silently reinterpreted as strategy capacity.
    """

    proposal = _snapshot_proposal(proposal)
    candidate = _snapshot_candidate(candidate)
    policy = _snapshot_policy(policy)
    stress = _snapshot_stress(stress_evidence)
    instrument = _text(instrument_version, name="instrument_version")

    if candidate.symbol != proposal.symbol:
        raise StrategyCapacityAuthorityError(
            "allocation candidate symbol does not match strategy proposal"
        )
    if candidate.current_quantity != 0:
        raise StrategyCapacityAuthorityError(
            "research capacity evidence requires a flat current position"
        )

    try:
        requested_notional = exact_multiply(proposal.quantity, candidate.price)
        if proposal.action == "SELL":
            requested_notional = exact_subtract(Decimal("0"), requested_notional)
        elif proposal.action == "HOLD":
            requested_notional = Decimal("0")
    except ExactDecimalError as error:
        raise StrategyCapacityAuthorityError(
            "proposal capacity notional exceeds exact-decimal resource envelope"
        ) from error

    if candidate.desired_notional != requested_notional:
        raise StrategyCapacityAuthorityError(
            "allocation candidate does not represent the exact proposal quantity"
        )

    decision_time = _utc_text(proposal.decision_time)
    result = _ALLOCATE_TARGETS(
        (candidate,),
        policy,
        stress_evidence=stress,
        decision_time=decision_time,
    )
    if type(result) is not AllocationResult:
        raise StrategyCapacityAuthorityError(
            "canonical allocator returned an unexpected result type"
        )
    if len(result.targets) > 1:
        raise StrategyCapacityAuthorityError(
            "single-proposal capacity replay returned multiple targets"
        )

    max_quantity = Decimal("0")
    if result.status == "ALLOCATED":
        if len(result.targets) != 1:
            raise StrategyCapacityAuthorityError(
                "allocated capacity replay must contain one target"
            )
        target = result.targets[0]
        if target.symbol != proposal.symbol:
            raise StrategyCapacityAuthorityError(
                "capacity replay target symbol changed"
            )
        try:
            max_quantity = exact_abs(target.quantity)
        except ExactDecimalError as error:
            raise StrategyCapacityAuthorityError(
                "capacity quantity exceeds exact-decimal resource envelope"
            ) from error
        if max_quantity > proposal.quantity:
            raise StrategyCapacityAuthorityError(
                "capacity evidence cannot expand strategy proposal exposure"
            )

    payload = {
        "schema_version": "wp33-allocation-capacity.v1",
        "proposal": {
            "strategy_fingerprint": proposal.strategy_fingerprint,
            "strategy_configuration_fingerprint": (
                proposal.strategy_configuration_fingerprint
            ),
            "instrument_version": instrument,
            "symbol": proposal.symbol,
            "action": proposal.action,
            "quantity": _decimal_text(proposal.quantity),
            "decision_time": decision_time,
            "information_cutoff": _utc_text(proposal.information_cutoff),
            "horizon_seconds": proposal.horizon_seconds,
            "expiry": _utc_text(proposal.expiry),
            "evidence_event_ids": list(proposal.evidence_event_ids),
        },
        "candidate": _candidate_document(candidate),
        "policy": _policy_document(policy),
        "stress_evidence": _stress_document(stress),
        "allocation_result": _result_document(result),
        "max_feasible_quantity": _decimal_text(max_quantity),
    }
    rendered = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    digest = "sha256:" + sha256(rendered).hexdigest()

    evidence = AllocationCapacityEvidence(
        digest=digest,
        instrument_version=instrument,
        strategy_fingerprint=proposal.strategy_fingerprint,
        strategy_configuration_fingerprint=(
            proposal.strategy_configuration_fingerprint
        ),
        symbol=proposal.symbol,
        action=proposal.action,
        decision_time=decision_time,
        information_cutoff=_utc_text(proposal.information_cutoff),
        horizon_seconds=proposal.horizon_seconds,
        proposal_quantity=_decimal_text(proposal.quantity),
        max_feasible_quantity=_decimal_text(max_quantity),
        lot_size=_decimal_text(candidate.lot_size),
        allocation_status=result.status,
        allocation_reason=result.reason,
        _token=_ISSUE_TOKEN,
    )
    return _register_issued(evidence)
