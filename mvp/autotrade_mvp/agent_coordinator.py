from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Iterable


@dataclass(frozen=True, slots=True)
class AgentRole:
    role_id: str
    deterministic: bool
    can_propose_execution: bool = False

    def __post_init__(self) -> None:
        if not self.role_id.strip():
            raise ValueError("role_id is required")


@dataclass(frozen=True, slots=True)
class AgentTask:
    task_id: str
    role_id: str
    input_version: str
    depends_on: tuple[str, ...] = ()
    deadline_utc: datetime | None = None
    budget: Decimal = Decimal("0")
    required: bool = True

    def __post_init__(self) -> None:
        if not self.task_id.strip() or not self.role_id.strip() or not self.input_version.strip():
            raise ValueError("task_id, role_id and input_version are required")
        if self.budget < 0:
            raise ValueError("budget cannot be negative")
        if self.deadline_utc is not None and self.deadline_utc.tzinfo is None:
            raise ValueError("deadline must be timezone-aware")


@dataclass(frozen=True, slots=True)
class PlanStep:
    task_id: str
    role_id: str
    input_version: str
    budget: Decimal
    deadline_utc: datetime | None
    can_propose_execution: bool


@dataclass(frozen=True, slots=True)
class AgentPlan:
    waves: tuple[tuple[PlanStep, ...], ...]
    total_budget: Decimal


def _dedupe_roles(roles: Iterable[AgentRole]) -> dict[str, AgentRole]:
    result: dict[str, AgentRole] = {}
    for role in roles:
        if role.role_id in result:
            raise ValueError(f"duplicate role: {role.role_id}")
        result[role.role_id] = role
    return result


def _dedupe_tasks(tasks: Iterable[AgentTask]) -> dict[str, AgentTask]:
    result: dict[str, AgentTask] = {}
    for task in tasks:
        if task.task_id in result:
            raise ValueError(f"duplicate task: {task.task_id}")
        result[task.task_id] = task
    return result


def build_agent_plan(
    *,
    roles: Iterable[AgentRole],
    tasks: Iterable[AgentTask],
    maximum_budget: Decimal,
    now_utc: datetime | None = None,
) -> AgentPlan:
    """Build a deterministic dependency plan without executing models or trades.

    All tasks in a plan operate on an explicit immutable input version. Roles may
    produce structured execution proposals, but this coordinator never grants
    authority and never sends financial requests.
    """
    if maximum_budget < 0:
        raise ValueError("maximum_budget cannot be negative")
    now = datetime.now(timezone.utc) if now_utc is None else now_utc
    if now.tzinfo is None:
        raise ValueError("now_utc must be timezone-aware")

    role_map = _dedupe_roles(roles)
    task_map = _dedupe_tasks(tasks)

    for task in task_map.values():
        if task.role_id not in role_map:
            raise ValueError(f"unknown role for task {task.task_id}: {task.role_id}")
        unknown = set(task.depends_on) - set(task_map)
        if unknown:
            raise ValueError(f"unknown dependencies for {task.task_id}: {sorted(unknown)}")
        if task.task_id in task.depends_on:
            raise ValueError(f"task cannot depend on itself: {task.task_id}")
        if task.deadline_utc is not None and task.deadline_utc <= now:
            if task.required:
                raise ValueError(f"required task deadline expired: {task.task_id}")

    total = sum((task.budget for task in task_map.values()), Decimal("0"))
    if total > maximum_budget:
        raise ValueError("agent plan exceeds hard budget")

    indegree = {task_id: 0 for task_id in task_map}
    children: dict[str, list[str]] = {task_id: [] for task_id in task_map}
    for task in task_map.values():
        for parent in task.depends_on:
            indegree[task.task_id] += 1
            children[parent].append(task.task_id)

    ready = sorted(task_id for task_id, count in indegree.items() if count == 0)
    waves: list[tuple[PlanStep, ...]] = []
    visited = 0

    while ready:
        current = tuple(ready)
        wave: list[PlanStep] = []
        next_ready: list[str] = []
        for task_id in current:
            task = task_map[task_id]
            role = role_map[task.role_id]
            wave.append(
                PlanStep(
                    task_id=task.task_id,
                    role_id=task.role_id,
                    input_version=task.input_version,
                    budget=task.budget,
                    deadline_utc=task.deadline_utc,
                    can_propose_execution=role.can_propose_execution,
                )
            )
            visited += 1
            for child in sorted(children[task_id]):
                indegree[child] -= 1
                if indegree[child] == 0:
                    next_ready.append(child)
        waves.append(tuple(wave))
        ready = sorted(next_ready)

    if visited != len(task_map):
        raise ValueError("agent task dependency graph contains a cycle")

    return AgentPlan(tuple(waves), total)


@dataclass(frozen=True, slots=True)
class SpecialistResult:
    task_id: str
    input_version: str
    evidence_ids: tuple[str, ...]
    claim: str | None
    confidence: Decimal | None
    execution_proposal: dict | None = None

    def __post_init__(self) -> None:
        if not self.task_id.strip() or not self.input_version.strip():
            raise ValueError("task_id and input_version are required")
        if self.confidence is not None and not (Decimal("0") <= self.confidence <= Decimal("1")):
            raise ValueError("confidence must be within [0,1]")


@dataclass(frozen=True, slots=True)
class AggregationResult:
    status: str
    disagreements: tuple[str, ...]
    missing_evidence_tasks: tuple[str, ...]
    execution_proposals: tuple[dict, ...]


def aggregate_specialist_results(
    *,
    plan: AgentPlan,
    results: Iterable[SpecialistResult],
) -> AggregationResult:
    """Aggregate evidence without majority-vote authority semantics."""
    expected: dict[str, PlanStep] = {
        step.task_id: step for wave in plan.waves for step in wave
    }
    received: dict[str, SpecialistResult] = {}
    for result in results:
        if result.task_id in received:
            raise ValueError(f"duplicate specialist result: {result.task_id}")
        step = expected.get(result.task_id)
        if step is None:
            raise ValueError(f"result for task outside plan: {result.task_id}")
        if result.input_version != step.input_version:
            raise ValueError(f"input version mismatch for {result.task_id}")
        if result.execution_proposal is not None and not step.can_propose_execution:
            raise ValueError(f"role cannot produce execution proposal: {result.task_id}")
        received[result.task_id] = result

    missing = tuple(sorted(set(expected) - set(received)))
    missing_evidence = tuple(
        sorted(task_id for task_id, result in received.items() if not result.evidence_ids)
    )

    claims: dict[str, list[str]] = {}
    for result in received.values():
        if result.claim is not None:
            claims.setdefault(result.input_version, []).append(result.claim)
    disagreements = tuple(
        sorted(
            version
            for version, values in claims.items()
            if len(set(values)) > 1
        )
    )
    proposals = tuple(
        result.execution_proposal
        for result in received.values()
        if result.execution_proposal is not None
    )

    if missing:
        status = "INCOMPLETE"
    elif missing_evidence:
        status = "MISSING_EVIDENCE"
    elif disagreements:
        status = "DISAGREEMENT"
    else:
        status = "COMPLETE"

    return AggregationResult(
        status=status,
        disagreements=disagreements,
        missing_evidence_tasks=missing_evidence,
        execution_proposals=proposals,
    )
