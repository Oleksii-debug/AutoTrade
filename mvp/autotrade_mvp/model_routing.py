"""Policy-bounded model routing and exact cost reservation for AutoTrade.

This module deliberately performs no model/network call and grants no financial
authority. It chooses an eligible model (or deterministic fallback/NO_TRADE)
under explicit user policy, measured evidence, deadline and exact Decimal
budget constraints. Trading authority remains outside this layer.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import re
from typing import Iterable, Mapping, Sequence


_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_MODES = frozenset({"ZERO_LLM", "LOCAL_ONLY", "FIXED", "ALLOWLIST", "DYNAMIC"})
_LOCATIONS = frozenset({"LOCAL", "REMOTE"})
_FALLBACKS = frozenset({"DETERMINISTIC", "NO_TRADE"})


class ModelRoutingError(ValueError):
    """Raised when routing policy/evidence is malformed or contradictory."""


class ModelBudgetConflict(ModelRoutingError):
    """Raised when an immutable budget identity is reused inconsistently."""


def _text(value: str, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ModelRoutingError(f"{name} is required")
    return value.strip()


def _hash(value: str, *, name: str) -> str:
    result = _text(value, name=name).lower()
    if _SHA256.fullmatch(result) is None:
        raise ModelRoutingError(f"{name} must be canonical sha256:<64 lowercase hex>")
    return result


def _decimal(value, *, name: str, nonnegative: bool = True) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise ModelRoutingError(f"{name} must use exact Decimal/string/integer input")
    try:
        result = value if isinstance(value, Decimal) else Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ModelRoutingError(f"{name} must be a finite decimal") from error
    if not result.is_finite():
        raise ModelRoutingError(f"{name} must be a finite decimal")
    if nonnegative and result < 0:
        raise ModelRoutingError(f"{name} cannot be negative")
    return result


def _positive_int(value: int, *, name: str, allow_zero: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ModelRoutingError(f"{name} must be an integer")
    minimum = 0 if allow_zero else 1
    if value < minimum:
        raise ModelRoutingError(f"{name} must be >= {minimum}")
    return value


def _set(values: Iterable[str], *, name: str, upper: bool = False) -> frozenset[str]:
    if isinstance(values, (str, bytes)):
        raise ModelRoutingError(f"{name} must be a collection")
    result = frozenset(
        (_text(value, name=name).upper() if upper else _text(value, name=name))
        for value in values
    )
    return result


@dataclass(frozen=True, slots=True)
class ModelDescriptor:
    model_id: str
    provider: str
    model_name: str
    revision: str | None
    location: str
    supported_task_schemas: frozenset[str]
    modalities: frozenset[str]
    tool_permissions: frozenset[str]
    privacy_region: str | None
    license_id: str
    deterministic_limitations: tuple[str, ...]
    max_context_tokens: int
    max_output_tokens: int
    expected_latency_ms: int
    input_token_price: Decimal
    output_token_price: Decimal
    price_evidence_sha256: str
    measured_quality: Decimal
    quality_evidence_sha256: str

    @classmethod
    def create(
        cls,
        *,
        model_id: str,
        provider: str,
        model_name: str,
        revision: str | None,
        location: str,
        supported_task_schemas: Iterable[str],
        modalities: Iterable[str] = ("TEXT",),
        tool_permissions: Iterable[str] = (),
        privacy_region: str | None = None,
        license_id: str,
        deterministic_limitations: Sequence[str] = (),
        max_context_tokens: int,
        max_output_tokens: int,
        expected_latency_ms: int,
        input_token_price,
        output_token_price,
        price_evidence_sha256: str,
        measured_quality,
        quality_evidence_sha256: str,
    ) -> "ModelDescriptor":
        loc = _text(location, name="location").upper()
        if loc not in _LOCATIONS:
            raise ModelRoutingError("location must be LOCAL or REMOTE")
        schemas = _set(supported_task_schemas, name="supported_task_schemas")
        if not schemas:
            raise ModelRoutingError("supported_task_schemas cannot be empty")
        modalities_set = _set(modalities, name="modalities", upper=True)
        if not modalities_set:
            raise ModelRoutingError("modalities cannot be empty")
        tools = _set(tool_permissions, name="tool_permissions")
        normalized_region = None
        if privacy_region is not None:
            normalized_region = _text(privacy_region, name="privacy_region").upper()
        if loc == "REMOTE" and normalized_region is None:
            raise ModelRoutingError("remote model requires an explicit privacy_region")
        limitations = tuple(
            _text(value, name="deterministic_limitation")
            for value in deterministic_limitations
        )
        if len(limitations) != len(set(limitations)):
            raise ModelRoutingError("deterministic_limitations contains duplicates")
        quality = _decimal(measured_quality, name="measured_quality")
        if quality > 1:
            raise ModelRoutingError("measured_quality must be within [0, 1]")
        normalized_revision = None
        if revision is not None:
            normalized_revision = _text(revision, name="revision")
        return cls(
            model_id=_text(model_id, name="model_id"),
            provider=_text(provider, name="provider"),
            model_name=_text(model_name, name="model_name"),
            revision=normalized_revision,
            location=loc,
            supported_task_schemas=schemas,
            modalities=modalities_set,
            tool_permissions=tools,
            privacy_region=normalized_region,
            license_id=_text(license_id, name="license_id"),
            deterministic_limitations=limitations,
            max_context_tokens=_positive_int(max_context_tokens, name="max_context_tokens"),
            max_output_tokens=_positive_int(max_output_tokens, name="max_output_tokens"),
            expected_latency_ms=_positive_int(
                expected_latency_ms, name="expected_latency_ms", allow_zero=True
            ),
            input_token_price=_decimal(input_token_price, name="input_token_price"),
            output_token_price=_decimal(output_token_price, name="output_token_price"),
            price_evidence_sha256=_hash(
                price_evidence_sha256, name="price_evidence_sha256"
            ),
            measured_quality=quality,
            quality_evidence_sha256=_hash(
                quality_evidence_sha256, name="quality_evidence_sha256"
            ),
        )

    @property
    def reproducibility_limitations(self) -> tuple[str, ...]:
        limitations = list(self.deterministic_limitations)
        if self.revision is None:
            limitations.append("UNKNOWN_MODEL_REVISION")
        return tuple(limitations)


@dataclass(frozen=True, slots=True)
class ModelRoutingPolicy:
    policy_id: str
    mode: str
    allowed_model_ids: frozenset[str]
    allowed_remote_regions: frozenset[str]
    max_total_cost: Decimal
    max_input_tokens: int
    max_output_tokens: int
    deadline_ms: int
    fallback: str
    deterministic_fallback_evidence_sha256: str | None

    @classmethod
    def create(
        cls,
        *,
        policy_id: str,
        mode: str,
        allowed_model_ids: Iterable[str] = (),
        allowed_remote_regions: Iterable[str] = (),
        max_total_cost,
        max_input_tokens: int,
        max_output_tokens: int,
        deadline_ms: int,
        fallback: str = "NO_TRADE",
        deterministic_fallback_evidence_sha256: str | None = None,
    ) -> "ModelRoutingPolicy":
        normalized_mode = _text(mode, name="mode").upper()
        if normalized_mode not in _MODES:
            raise ModelRoutingError("unsupported routing mode")
        allowed = _set(allowed_model_ids, name="allowed_model_ids")
        if normalized_mode == "ZERO_LLM" and allowed:
            raise ModelRoutingError("ZERO_LLM cannot contain allowed models")
        if normalized_mode == "FIXED" and len(allowed) != 1:
            raise ModelRoutingError("FIXED requires exactly one allowed model")
        if normalized_mode in {"LOCAL_ONLY", "ALLOWLIST", "DYNAMIC"} and not allowed:
            raise ModelRoutingError(f"{normalized_mode} requires an explicit model allowlist")
        regions = _set(
            allowed_remote_regions,
            name="allowed_remote_regions",
            upper=True,
        )
        normalized_fallback = _text(fallback, name="fallback").upper()
        if normalized_fallback not in _FALLBACKS:
            raise ModelRoutingError("fallback must be DETERMINISTIC or NO_TRADE")
        fallback_evidence = None
        if deterministic_fallback_evidence_sha256 is not None:
            fallback_evidence = _hash(
                deterministic_fallback_evidence_sha256,
                name="deterministic_fallback_evidence_sha256",
            )
        if normalized_fallback == "DETERMINISTIC" and fallback_evidence is None:
            raise ModelRoutingError(
                "DETERMINISTIC fallback requires immutable qualification evidence"
            )
        if normalized_fallback == "NO_TRADE" and fallback_evidence is not None:
            raise ModelRoutingError(
                "NO_TRADE cannot carry deterministic fallback qualification evidence"
            )
        return cls(
            policy_id=_text(policy_id, name="policy_id"),
            mode=normalized_mode,
            allowed_model_ids=allowed,
            allowed_remote_regions=regions,
            max_total_cost=_decimal(max_total_cost, name="max_total_cost"),
            max_input_tokens=_positive_int(max_input_tokens, name="max_input_tokens"),
            max_output_tokens=_positive_int(max_output_tokens, name="max_output_tokens"),
            deadline_ms=_positive_int(deadline_ms, name="deadline_ms", allow_zero=True),
            fallback=normalized_fallback,
            deterministic_fallback_evidence_sha256=fallback_evidence,
        )


@dataclass(frozen=True, slots=True)
class ModelTask:
    task_id: str
    task_schema: str
    input_hash: str
    input_tokens: int
    max_output_tokens: int
    required_modalities: frozenset[str]
    required_tools: frozenset[str]

    @classmethod
    def create(
        cls,
        *,
        task_id: str,
        task_schema: str,
        input_hash: str,
        input_tokens: int,
        max_output_tokens: int,
        required_modalities: Iterable[str] = ("TEXT",),
        required_tools: Iterable[str] = (),
    ) -> "ModelTask":
        return cls(
            task_id=_text(task_id, name="task_id"),
            task_schema=_text(task_schema, name="task_schema"),
            input_hash=_hash(input_hash, name="input_hash"),
            input_tokens=_positive_int(input_tokens, name="input_tokens", allow_zero=True),
            max_output_tokens=_positive_int(
                max_output_tokens, name="max_output_tokens", allow_zero=True
            ),
            required_modalities=_set(
                required_modalities, name="required_modalities", upper=True
            ),
            required_tools=_set(required_tools, name="required_tools"),
        )


@dataclass(frozen=True, slots=True)
class BudgetSnapshot:
    ceiling: Decimal
    reserved: Decimal
    estimated_unbilled: Decimal
    incurred: Decimal

    @property
    def committed(self) -> Decimal:
        return self.reserved + self.estimated_unbilled + self.incurred

    @property
    def available(self) -> Decimal:
        return max(Decimal("0"), self.ceiling - self.committed)

    @property
    def over_budget(self) -> bool:
        return self.committed > self.ceiling


class ModelBudgetLedger:
    """In-memory exact budget projection.

    Durable persistence is intentionally delegated to the canonical journal in
    WP-05/WP-06; this class defines the invariant and never performs a model call.
    """

    def __init__(self, *, ceiling) -> None:
        self._ceiling = _decimal(ceiling, name="ceiling")
        self._reservations: dict[str, Decimal] = {}
        self._unbilled: dict[str, Decimal] = {}
        self._incurred: dict[str, Decimal] = {}

    def snapshot(self) -> BudgetSnapshot:
        return BudgetSnapshot(
            ceiling=self._ceiling,
            reserved=sum(self._reservations.values(), Decimal("0")),
            estimated_unbilled=sum(self._unbilled.values(), Decimal("0")),
            incurred=sum(self._incurred.values(), Decimal("0")),
        )

    def reserve(self, reservation_id: str, amount) -> bool:
        key = _text(reservation_id, name="reservation_id")
        cost = _decimal(amount, name="reservation_amount")
        existing = self._reservations.get(key)
        if existing is not None:
            if existing != cost:
                raise ModelBudgetConflict(
                    "reservation_id already exists with a different amount"
                )
            return False
        if key in self._unbilled or key in self._incurred:
            raise ModelBudgetConflict("reservation_id has already crossed the call boundary")
        if cost > self.snapshot().available:
            raise ModelRoutingError("model budget ceiling would be exceeded")
        self._reservations[key] = cost
        return True

    def release(self, reservation_id: str) -> bool:
        key = _text(reservation_id, name="reservation_id")
        if key in self._unbilled or key in self._incurred:
            raise ModelBudgetConflict("cannot release cost after the call boundary")
        return self._reservations.pop(key, None) is not None

    def mark_unbilled(self, reservation_id: str, *, estimated_cost=None) -> None:
        key = _text(reservation_id, name="reservation_id")
        reserved = self._reservations.pop(key, None)
        if reserved is None:
            raise ModelBudgetConflict("unknown active reservation")
        estimate = reserved if estimated_cost is None else _decimal(
            estimated_cost, name="estimated_unbilled_cost"
        )
        self._unbilled[key] = estimate

    def record_billing(self, reservation_id: str, *, billed_cost) -> None:
        key = _text(reservation_id, name="reservation_id")
        amount = _decimal(billed_cost, name="billed_cost")
        if key in self._incurred:
            if self._incurred[key] != amount:
                raise ModelBudgetConflict(
                    "billing identity already exists with a different amount"
                )
            return
        if key in self._reservations:
            self._reservations.pop(key)
        elif key in self._unbilled:
            self._unbilled.pop(key)
        else:
            raise ModelBudgetConflict("billing has no matching reserved/unbilled identity")
        # Actual provider billing is observed truth. Record it even if it exceeds
        # the estimate; overrun remains visible and blocks future reservations.
        self._incurred[key] = amount


@dataclass(frozen=True, slots=True)
class ModelRoutingDecision:
    outcome: str
    reason: str
    policy_id: str
    task_id: str
    reservation_id: str | None
    model_id: str | None
    provider: str | None
    model_name: str | None
    revision: str | None
    estimated_cost: Decimal
    expected_latency_ms: int | None
    price_evidence_sha256: str | None
    quality_evidence_sha256: str | None
    reproducibility_limitations: tuple[str, ...]
    fallback_evidence_sha256: str | None

    @property
    def authorizes_trading(self) -> bool:
        return False


def _estimated_cost(model: ModelDescriptor, task: ModelTask) -> Decimal:
    return (
        Decimal(task.input_tokens) * model.input_token_price
        + Decimal(task.max_output_tokens) * model.output_token_price
    )


def _fallback(policy: ModelRoutingPolicy, task: ModelTask, reason: str) -> ModelRoutingDecision:
    return ModelRoutingDecision(
        outcome=policy.fallback,
        reason=reason,
        policy_id=policy.policy_id,
        task_id=task.task_id,
        reservation_id=None,
        model_id=None,
        provider=None,
        model_name=None,
        revision=None,
        estimated_cost=Decimal("0"),
        expected_latency_ms=None,
        price_evidence_sha256=None,
        quality_evidence_sha256=None,
        reproducibility_limitations=(),
        fallback_evidence_sha256=policy.deterministic_fallback_evidence_sha256,
    )


def route_model(
    *,
    policy: ModelRoutingPolicy,
    task: ModelTask,
    models: Sequence[ModelDescriptor],
    budget: ModelBudgetLedger,
) -> ModelRoutingDecision:
    """Select and reserve one qualified model, otherwise return bounded fallback."""

    if not isinstance(policy, ModelRoutingPolicy):
        raise TypeError("policy must be ModelRoutingPolicy")
    if not isinstance(task, ModelTask):
        raise TypeError("task must be ModelTask")
    if not isinstance(budget, ModelBudgetLedger):
        raise TypeError("budget must be ModelBudgetLedger")
    if isinstance(models, (str, bytes)) or not isinstance(models, Sequence):
        raise TypeError("models must be a sequence")

    if task.input_tokens > policy.max_input_tokens:
        return _fallback(policy, task, "policy_input_token_limit")
    if task.max_output_tokens > policy.max_output_tokens:
        return _fallback(policy, task, "policy_output_token_limit")
    if policy.mode == "ZERO_LLM":
        return _fallback(policy, task, "zero_llm_policy")

    by_id: dict[str, ModelDescriptor] = {}
    for model in models:
        if not isinstance(model, ModelDescriptor):
            raise TypeError("models must contain ModelDescriptor values")
        if model.model_id in by_id:
            raise ModelRoutingError("duplicate model_id")
        by_id[model.model_id] = model

    eligible: list[tuple[ModelDescriptor, Decimal]] = []
    for model_id in sorted(policy.allowed_model_ids):
        model = by_id.get(model_id)
        if model is None:
            continue
        if policy.mode == "LOCAL_ONLY" and model.location != "LOCAL":
            continue
        if (
            model.location == "REMOTE"
            and model.privacy_region not in policy.allowed_remote_regions
        ):
            continue
        if task.task_schema not in model.supported_task_schemas:
            continue
        if not task.required_modalities <= model.modalities:
            continue
        if not task.required_tools <= model.tool_permissions:
            continue
        if task.input_tokens + task.max_output_tokens > model.max_context_tokens:
            continue
        if task.max_output_tokens > model.max_output_tokens:
            continue
        if model.expected_latency_ms > policy.deadline_ms:
            continue
        cost = _estimated_cost(model, task)
        if cost > policy.max_total_cost:
            continue
        if cost > budget.snapshot().available:
            continue
        eligible.append((model, cost))

    if not eligible:
        return _fallback(policy, task, "no_eligible_model")

    # Quality values are measured evidence, never model self-ratings. Within all
    # hard constraints choose highest measured quality, then lower reserved cost,
    # lower expected latency and stable model_id for deterministic ties.
    eligible.sort(
        key=lambda pair: (
            -pair[0].measured_quality,
            pair[1],
            pair[0].expected_latency_ms,
            pair[0].model_id,
        )
    )
    chosen, cost = eligible[0]
    reservation_material = (
        f"{policy.policy_id}|{task.task_id}|{task.input_hash}|{chosen.model_id}|{cost}"
    )
    reservation_id = "model:" + sha256(reservation_material.encode("utf-8")).hexdigest()
    budget.reserve(reservation_id, cost)

    return ModelRoutingDecision(
        outcome="MODEL",
        reason="qualified_model_reserved",
        policy_id=policy.policy_id,
        task_id=task.task_id,
        reservation_id=reservation_id,
        model_id=chosen.model_id,
        provider=chosen.provider,
        model_name=chosen.model_name,
        revision=chosen.revision,
        estimated_cost=cost,
        expected_latency_ms=chosen.expected_latency_ms,
        price_evidence_sha256=chosen.price_evidence_sha256,
        quality_evidence_sha256=chosen.quality_evidence_sha256,
        reproducibility_limitations=chosen.reproducibility_limitations,
        fallback_evidence_sha256=None,
    )
