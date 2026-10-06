"""Executable current-schema provider-free target-host runner for WP-65.

This runner is deliberately a composition layer over the durable authorities that
already exist in the current product.  It does not resurrect the historical
``RuntimeCampaignPlan``/``RuntimeCampaignCut`` stack and it does not invent a
terminal PASS.

The operator must predeclare the financial and research plans and must establish
the target-host campaign authority before this function is called.  The runner
then:

* proves the exact research plan existed before the target-host authority cut;
* snapshots exact operation mappings from the durable plan identities;
* captures and retains target-host inventory before caller work executes;
* invokes the canonical durable research/financial measurement issuers;
* collects the current raw target-host measurement artifact;
* retains that artifact byte-for-byte in the neutral ArtifactStore; and
* retains a nonterminal run receipt that binds the exact research plan to the
  authority and retained artifacts.

The retained receipt explicitly carries ``terminal_qualification_eligible=false``
and ``resource_evidence_status=NOT_COLLECTED``.  Provider/exchange source-clock
freshness, resource telemetry, independent chronology/signing, PAPER/LIVE
provider qualification, release acceptance and economic edge remain separate
unfinished authorities.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
from types import FunctionType, MethodType
from typing import Callable
from uuid import UUID

from autotrade_runtime.artifacts import ArtifactStore

from .performance_qualification import RuntimeBudgetSpec
from .persistence import JournalStore, require_exact_journal_store_authority
from .runtime_load_measurement import measure_declared_financial_operation
from .runtime_load_plan import load_declared_runtime_event_plan
from .runtime_load_research_measurement import (
    DeclaredResearchInterferencePlan,
    _read_research_plan,
    measure_declared_research_interference,
)
from .runtime_target_host_campaign_authority import (
    RuntimeTargetHostCampaignAuthority,
    load_runtime_target_host_campaign_authority,
)
from .runtime_target_host_inventory import (
    PublishedRuntimeTargetHostInventory,
    publish_runtime_target_host_inventory,
)
from .runtime_target_host_measurement import (
    RuntimeTargetHostMeasurementArtifact,
    collect_runtime_target_host_measurement,
)
from .runtime_target_host_measurement_publication import (
    PublishedRuntimeTargetHostMeasurement,
    publish_runtime_target_host_measurement,
)


_SCHEMA_VERSION = "1.0.0"
_EVIDENCE_TYPE = "AUTOTRADE_TARGET_HOST_RUN_RECEIPT_CURRENT"
_RUN_EVIDENCE_KIND = "RUNTIME_TARGET_HOST_RUN_RECEIPT_CURRENT"
_JSON_MEDIA_TYPE = "application/json"
_RESOURCE_EVIDENCE_STATUS = "NOT_COLLECTED"


class RuntimeTargetHostRunnerError(ValueError):
    """Raised when the current-schema target-host run cannot proceed safely."""


def _text(value: object, *, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise RuntimeTargetHostRunnerError(f"{name} must be canonical non-empty text")
    return value


def _uuid(value: object, *, name: str) -> str:
    value = _text(value, name=name)
    try:
        canonical = str(UUID(value))
    except (TypeError, ValueError, AttributeError) as error:
        raise RuntimeTargetHostRunnerError(f"{name} must be a canonical UUID") from error
    if canonical != value:
        raise RuntimeTargetHostRunnerError(f"{name} must be a canonical UUID")
    return value


def _snapshot_spec(value: RuntimeBudgetSpec) -> RuntimeBudgetSpec:
    if type(value) is not RuntimeBudgetSpec:
        raise TypeError("spec must be exact RuntimeBudgetSpec")
    return RuntimeBudgetSpec(
        scenario_id=value.scenario_id,
        release_sha=value.release_sha,
        configuration_hash=value.configuration_hash,
        host_fingerprint=value.host_fingerprint,
        strategy_horizon_us=value.strategy_horizon_us,
        max_p95_financial_latency_us=value.max_p95_financial_latency_us,
        max_financial_staleness_us=value.max_financial_staleness_us,
        max_research_interference_us=value.max_research_interference_us,
        min_financial_samples=value.min_financial_samples,
        min_research_samples=value.min_research_samples,
    )


def _require_callback(value: object, *, name: str) -> Callable[[], object]:
    if type(value) is not FunctionType and type(value) is not MethodType:
        raise RuntimeTargetHostRunnerError(
            f"{name} must be an exact Python function or bound method"
        )
    return value


def _snapshot_operation_map(
    value: object,
    *,
    expected_ids: tuple[str, ...],
    name: str,
) -> tuple[tuple[str, Callable[[], object]], ...]:
    if type(value) is not dict:
        raise RuntimeTargetHostRunnerError(f"{name} must be an exact dict")
    if any(type(key) is not str for key in value):
        raise RuntimeTargetHostRunnerError(f"{name} keys must be exact strings")
    if set(value) != set(expected_ids):
        raise RuntimeTargetHostRunnerError(
            f"{name} keys must exactly match the durable pre-run plan"
        )
    return tuple(
        (
            identity,
            _require_callback(value[identity], name=f"{name}[{identity}]"),
        )
        for identity in expected_ids
    )


def _snapshot_kwdefaults(value: object) -> tuple[tuple[str, object], ...] | None:
    if value is None:
        return None
    if type(value) is not dict or any(type(key) is not str for key in value):
        raise RuntimeTargetHostRunnerError(
            "callable keyword defaults must be an exact string-keyed dict"
        )
    return tuple((key, value[key]) for key in sorted(value))


@dataclass(frozen=True, slots=True)
class _CallableAuthority:
    target: object
    functions: tuple[
        tuple[
            FunctionType,
            object,
            object,
            object,
            tuple[tuple[str, object], ...] | None,
        ],
        ...,
    ]
    globals: tuple[tuple[dict[str, object], str, object], ...]
    closures: tuple[tuple[object, bool, object | None], ...]


@dataclass(frozen=True, slots=True)
class _ArtifactStoreAuthority:
    store: ArtifactStore
    paths: tuple[tuple[str, Path, Path], ...]


def _capture_artifact_store_authority(
    store: ArtifactStore,
) -> _ArtifactStoreAuthority:
    if type(store) is not ArtifactStore:
        raise TypeError("evidence_store must be exact ArtifactStore")
    paths: list[tuple[str, Path, Path]] = []
    concrete_path_type = type(Path())
    for name in ("root", "objects", "manifests", "staging", "lock_path"):
        value = getattr(store, name, None)
        if type(value) is not concrete_path_type:
            raise RuntimeTargetHostRunnerError(
                f"ArtifactStore {name} must remain an exact pathlib path"
            )
        try:
            resolved = value.resolve(strict=False)
        except OSError as error:
            raise RuntimeTargetHostRunnerError(
                f"ArtifactStore {name} authority cannot be resolved"
            ) from error
        paths.append((name, value, resolved))
    return _ArtifactStoreAuthority(store=store, paths=tuple(paths))


def _require_artifact_store_authority(
    store: ArtifactStore,
    state: _ArtifactStoreAuthority,
) -> None:
    if store is not state.store:
        raise RuntimeTargetHostRunnerError(
            "ArtifactStore identity changed during target-host run"
        )
    for name, expected, expected_resolved in state.paths:
        current = getattr(store, name, None)
        if type(current) is not type(expected) or current != expected:
            raise RuntimeTargetHostRunnerError(
                f"ArtifactStore {name} authority changed during target-host run"
            )
        try:
            current_resolved = current.resolve(strict=False)
        except OSError as error:
            raise RuntimeTargetHostRunnerError(
                f"ArtifactStore {name} authority cannot be resolved during target-host run"
            ) from error
        if current_resolved != expected_resolved:
            raise RuntimeTargetHostRunnerError(
                f"ArtifactStore {name} resolved authority changed during target-host run"
            )


def _capture_callable_authority(value: object) -> _CallableAuthority:
    """Snapshot first-party executable dependencies crossed after callbacks."""

    target = value.__func__ if type(value) is MethodType else value
    if type(target) is not FunctionType:
        return _CallableAuthority(target, (), (), ())

    functions: list[
        tuple[
            FunctionType,
            object,
            object,
            object,
            tuple[tuple[str, object], ...] | None,
        ]
    ] = []
    globals_: list[tuple[dict[str, object], str, object]] = []
    closures: list[tuple[object, bool, object | None]] = []
    seen: set[int] = set()

    def capture(function: FunctionType) -> None:
        if id(function) in seen:
            return
        seen.add(id(function))
        functions.append(
            (
                function,
                function.__code__,
                function.__defaults__,
                function.__kwdefaults__,
                _snapshot_kwdefaults(function.__kwdefaults__),
            )
        )
        namespace = function.__globals__
        for dependency_name in function.__code__.co_names:
            if dependency_name not in namespace:
                continue
            expected = namespace[dependency_name]
            globals_.append((namespace, dependency_name, expected))
            dependency = expected.__func__ if type(expected) is MethodType else expected
            if (
                type(dependency) is FunctionType
                and (
                    dependency.__module__ == "mvp.autotrade_mvp"
                    or dependency.__module__.startswith("mvp.autotrade_mvp.")
                )
            ):
                capture(dependency)
        for cell in function.__closure__ or ():
            try:
                expected = cell.cell_contents
            except ValueError:
                closures.append((cell, False, None))
                continue
            closures.append((cell, True, expected))
            dependency = expected.__func__ if type(expected) is MethodType else expected
            if (
                type(dependency) is FunctionType
                and (
                    dependency.__module__ == "mvp.autotrade_mvp"
                    or dependency.__module__.startswith("mvp.autotrade_mvp.")
                )
            ):
                capture(dependency)

    capture(target)
    return _CallableAuthority(
        target=target,
        functions=tuple(functions),
        globals=tuple(globals_),
        closures=tuple(closures),
    )


def _require_callable_authority(
    value: object,
    state: _CallableAuthority,
    *,
    name: str,
) -> None:
    target = value.__func__ if type(value) is MethodType else value
    if target is not state.target:
        raise RuntimeTargetHostRunnerError(
            f"{name} callable authority changed during target-host run"
        )
    for function, code, defaults, kwdefaults, kwdefault_items in state.functions:
        current_kwdefaults = function.__kwdefaults__
        if (
            function.__code__ is not code
            or function.__defaults__ is not defaults
            or current_kwdefaults is not kwdefaults
        ):
            raise RuntimeTargetHostRunnerError(
                f"{name} executable authority changed during target-host run"
            )
        if kwdefault_items is not None:
            if type(current_kwdefaults) is not dict:
                raise RuntimeTargetHostRunnerError(
                    f"{name} keyword-default authority changed during target-host run"
                )
            if tuple(sorted(current_kwdefaults)) != tuple(
                key for key, _expected in kwdefault_items
            ) or any(
                current_kwdefaults[key] is not expected
                for key, expected in kwdefault_items
            ):
                raise RuntimeTargetHostRunnerError(
                    f"{name} keyword-default authority changed during target-host run"
                )
    missing = object()
    for namespace, dependency_name, expected in state.globals:
        if namespace.get(dependency_name, missing) is not expected:
            raise RuntimeTargetHostRunnerError(
                f"{name} dependency changed during target-host run: {dependency_name}"
            )
    for cell, had_value, expected in state.closures:
        try:
            current = cell.cell_contents
        except ValueError:
            if had_value:
                raise RuntimeTargetHostRunnerError(
                    f"{name} closure authority changed during target-host run"
                )
            continue
        if not had_value or current is not expected:
            raise RuntimeTargetHostRunnerError(
                f"{name} closure authority changed during target-host run"
            )


def _canonical_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


@dataclass(frozen=True, slots=True)
class RuntimeTargetHostRunReceipt:
    authority_id: str
    authority_digest: str
    authority_journal_sequence: int
    financial_plan_id: str
    financial_plan_digest: str
    research_plan_id: str
    research_plan_digest: str
    research_plan_declared_journal_sequence: int
    source_sha: str
    release_artifact_id: str
    release_artifact_sha256: str
    inventory_artifact_id: str
    inventory_payload_sha256: str
    measurement_artifact_id: str
    measurement_payload_sha256: str
    scenario_id: str
    spec_digest: str
    host_fingerprint: str
    resource_evidence_status: str = _RESOURCE_EVIDENCE_STATUS
    terminal_qualification_eligible: bool = False
    schema_version: str = _SCHEMA_VERSION
    evidence_type: str = _EVIDENCE_TYPE

    def canonical_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "evidence_type": self.evidence_type,
            "authority_id": self.authority_id,
            "authority_digest": self.authority_digest,
            "authority_journal_sequence": self.authority_journal_sequence,
            "financial_plan_id": self.financial_plan_id,
            "financial_plan_digest": self.financial_plan_digest,
            "research_plan_id": self.research_plan_id,
            "research_plan_digest": self.research_plan_digest,
            "research_plan_declared_journal_sequence": (
                self.research_plan_declared_journal_sequence
            ),
            "source_sha": self.source_sha,
            "release_artifact_id": self.release_artifact_id,
            "release_artifact_sha256": self.release_artifact_sha256,
            "inventory_artifact_id": self.inventory_artifact_id,
            "inventory_payload_sha256": self.inventory_payload_sha256,
            "measurement_artifact_id": self.measurement_artifact_id,
            "measurement_payload_sha256": self.measurement_payload_sha256,
            "scenario_id": self.scenario_id,
            "spec_digest": self.spec_digest,
            "host_fingerprint": self.host_fingerprint,
            "resource_evidence_status": self.resource_evidence_status,
            "terminal_qualification_eligible": self.terminal_qualification_eligible,
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_bytes(self.canonical_payload())

    @property
    def digest(self) -> str:
        return "sha256:" + sha256(self.canonical_bytes()).hexdigest()


@dataclass(frozen=True, slots=True)
class RuntimeTargetHostRunResult:
    authority: RuntimeTargetHostCampaignAuthority
    research_plan: DeclaredResearchInterferencePlan
    measurement: RuntimeTargetHostMeasurementArtifact
    published_inventory: PublishedRuntimeTargetHostInventory
    published_measurement: PublishedRuntimeTargetHostMeasurement
    run_receipt: RuntimeTargetHostRunReceipt
    run_receipt_artifact_id: str
    run_receipt_payload_sha256: str

    @property
    def terminal_qualification_eligible(self) -> bool:
        return False


def _publish_run_receipt(
    evidence_store: ArtifactStore,
    *,
    artifact_id: str,
    receipt: RuntimeTargetHostRunReceipt,
    publish_bytes: Callable[..., dict[str, object]],
) -> str:
    raw = receipt.canonical_bytes()
    digest = receipt.digest
    manifest = publish_bytes(
        evidence_store,
        artifact_id=artifact_id,
        data=raw,
        media_type=_JSON_MEDIA_TYPE,
        rights={"storage": True, "export": False},
        source_refs=[f"git:{receipt.source_sha}"],
        metadata={
            "evidence_kind": _RUN_EVIDENCE_KIND,
            "schema_version": receipt.schema_version,
            "authority_id": receipt.authority_id,
            "authority_digest": receipt.authority_digest,
            "research_plan_id": receipt.research_plan_id,
            "research_plan_digest": receipt.research_plan_digest,
            "measurement_artifact_id": receipt.measurement_artifact_id,
            "measurement_payload_sha256": receipt.measurement_payload_sha256,
            "resource_evidence_status": receipt.resource_evidence_status,
            "terminal_qualification_eligible": False,
        },
    )
    if (
        manifest.get("artifact_id") != artifact_id
        or manifest.get("sha256") != digest
        or manifest.get("media_type") != _JSON_MEDIA_TYPE
    ):
        raise RuntimeTargetHostRunnerError(
            "retained target-host run receipt changed during publication"
        )
    return digest


def run_declared_target_host_campaign(
    *,
    journal: JournalStore,
    evidence_store: ArtifactStore,
    spec: RuntimeBudgetSpec,
    authority_id: str,
    research_plan_id: str,
    financial_operations: dict[str, Callable[[], object]],
    research_operations: dict[str, Callable[[], object]],
    inventory_artifact_id: str,
    measurement_artifact_id: str,
    run_receipt_artifact_id: str,
) -> RuntimeTargetHostRunResult:
    """Execute one fully predeclared provider-free target-host measurement run.

    This function intentionally returns and retains nonterminal evidence only.
    Any callback failure or authority mutation aborts the run.  Existing durable
    measurement issuers remain responsible for binding each callback to its exact
    predeclared JournalStore event/sample identity.
    """

    spec = _snapshot_spec(spec)
    authority_id = _text(authority_id, name="authority_id")
    research_plan_id = _text(research_plan_id, name="research_plan_id")
    inventory_artifact_id = _uuid(
        inventory_artifact_id,
        name="inventory_artifact_id",
    )
    measurement_artifact_id = _uuid(
        measurement_artifact_id,
        name="measurement_artifact_id",
    )
    run_receipt_artifact_id = _uuid(
        run_receipt_artifact_id,
        name="run_receipt_artifact_id",
    )
    if len(
        {
            inventory_artifact_id,
            measurement_artifact_id,
            run_receipt_artifact_id,
        }
    ) != 3:
        raise RuntimeTargetHostRunnerError(
            "runner evidence artifact IDs must be pairwise distinct"
        )
    if type(evidence_store) is not ArtifactStore:
        raise TypeError("evidence_store must be exact ArtifactStore")
    require_exact_journal_store_authority(
        journal,
        subject="target-host runner JournalStore",
    )

    authority = load_runtime_target_host_campaign_authority(
        journal,
        spec,
        authority_id=authority_id,
    )
    financial_plan = load_declared_runtime_event_plan(
        journal,
        plan_id=authority.financial_plan_id,
        spec=spec,
    )
    research_plan = _read_research_plan(
        journal,
        spec,
        plan_id=research_plan_id,
    )
    if (
        research_plan.financial_plan_id != financial_plan.plan_id
        or research_plan.financial_plan_digest != financial_plan.digest
    ):
        raise RuntimeTargetHostRunnerError(
            "research plan is not bound to the target-host financial plan"
        )
    if research_plan.declared_journal_sequence >= authority.declared_journal_sequence:
        raise RuntimeTargetHostRunnerError(
            "research plan must be durably declared before target-host campaign authority"
        )

    financial_snapshot = tuple(
        (identity, operation, _capture_callable_authority(operation))
        for identity, operation in _snapshot_operation_map(
            financial_operations,
            expected_ids=financial_plan.expected_event_ids,
            name="financial_operations",
        )
    )
    research_snapshot = tuple(
        (identity, operation, _capture_callable_authority(operation))
        for identity, operation in _snapshot_operation_map(
            research_operations,
            expected_ids=research_plan.expected_sample_ids,
            name="research_operations",
        )
    )

    store_authority = _capture_artifact_store_authority(evidence_store)
    measure_financial = measure_declared_financial_operation
    measure_research = measure_declared_research_interference
    collect_measurement = collect_runtime_target_host_measurement
    publish_measurement = publish_runtime_target_host_measurement
    read_snapshot = ArtifactStore.read_authenticated_snapshot
    publish_bytes = ArtifactStore.publish_bytes
    protected = (
        ("financial measurement issuer", measure_financial, _capture_callable_authority(measure_financial)),
        ("research measurement issuer", measure_research, _capture_callable_authority(measure_research)),
        ("target-host measurement collector", collect_measurement, _capture_callable_authority(collect_measurement)),
        ("target-host measurement publisher", publish_measurement, _capture_callable_authority(publish_measurement)),
        ("ArtifactStore authenticated reader", read_snapshot, _capture_callable_authority(read_snapshot)),
        ("ArtifactStore publisher", publish_bytes, _capture_callable_authority(publish_bytes)),
    )

    def require_runner_authority() -> None:
        _require_artifact_store_authority(evidence_store, store_authority)
        if ArtifactStore.read_authenticated_snapshot is not read_snapshot:
            raise RuntimeTargetHostRunnerError(
                "ArtifactStore authenticated reader authority changed during target-host run"
            )
        if ArtifactStore.publish_bytes is not publish_bytes:
            raise RuntimeTargetHostRunnerError(
                "ArtifactStore publisher authority changed during target-host run"
            )
        for name, function, state in protected:
            _require_callable_authority(function, state, name=name)

    require_runner_authority()
    published_inventory = publish_runtime_target_host_inventory(
        evidence_store,
        artifact_id=inventory_artifact_id,
        expected_source_sha=authority.source_sha,
        expected_host_fingerprint=authority.host_fingerprint,
    )

    for sample_id, operation, operation_authority in research_snapshot:
        require_runner_authority()
        _require_callable_authority(
            operation,
            operation_authority,
            name=f"research operation {sample_id}",
        )
        measure_research(
            journal,
            spec,
            plan_id=research_plan.plan_id,
            sample_id=sample_id,
            operation=operation,
        )
        _require_callable_authority(
            operation,
            operation_authority,
            name=f"research operation {sample_id}",
        )
        require_runner_authority()

    for event_id, operation, operation_authority in financial_snapshot:
        require_runner_authority()
        _require_callable_authority(
            operation,
            operation_authority,
            name=f"financial operation {event_id}",
        )
        measure_financial(
            journal,
            spec,
            plan_id=financial_plan.plan_id,
            event_id=event_id,
            operation=operation,
        )
        _require_callable_authority(
            operation,
            operation_authority,
            name=f"financial operation {event_id}",
        )
        require_runner_authority()

    measurement = collect_measurement(
        journal,
        spec,
        authority_id=authority.authority_id,
        research_plan_id=research_plan.plan_id,
    )
    require_runner_authority()
    published_measurement = publish_measurement(
        evidence_store,
        artifact_id=measurement_artifact_id,
        artifact=measurement,
    )

    inventory_manifest, _inventory_raw = read_snapshot(evidence_store, inventory_artifact_id)
    if inventory_manifest.get("sha256") != published_inventory.payload_sha256:
        raise RuntimeTargetHostRunnerError(
            "retained target-host inventory changed during campaign"
        )
    measurement_manifest, measurement_raw = read_snapshot(evidence_store, measurement_artifact_id)
    if (
        measurement_manifest.get("sha256") != published_measurement.payload_sha256
        or measurement_raw != measurement.canonical_bytes()
    ):
        raise RuntimeTargetHostRunnerError(
            "retained target-host measurement changed during campaign"
        )

    receipt = RuntimeTargetHostRunReceipt(
        authority_id=authority.authority_id,
        authority_digest=authority.digest,
        authority_journal_sequence=authority.declared_journal_sequence,
        financial_plan_id=financial_plan.plan_id,
        financial_plan_digest=financial_plan.digest,
        research_plan_id=research_plan.plan_id,
        research_plan_digest=research_plan.digest,
        research_plan_declared_journal_sequence=research_plan.declared_journal_sequence,
        source_sha=authority.source_sha,
        release_artifact_id=authority.release_artifact_id,
        release_artifact_sha256=authority.release_artifact_sha256,
        inventory_artifact_id=published_inventory.artifact_id,
        inventory_payload_sha256=published_inventory.payload_sha256,
        measurement_artifact_id=published_measurement.artifact_id,
        measurement_payload_sha256=published_measurement.payload_sha256,
        scenario_id=authority.scenario_id,
        spec_digest=authority.spec_digest,
        host_fingerprint=authority.host_fingerprint,
    )
    require_runner_authority()
    receipt_digest = _publish_run_receipt(
        evidence_store,
        artifact_id=run_receipt_artifact_id,
        receipt=receipt,
        publish_bytes=publish_bytes,
    )
    _receipt_manifest, receipt_raw = read_snapshot(evidence_store, run_receipt_artifact_id)
    if receipt_raw != receipt.canonical_bytes():
        raise RuntimeTargetHostRunnerError(
            "retained target-host run receipt bytes changed during publication"
        )

    return RuntimeTargetHostRunResult(
        authority=authority,
        research_plan=research_plan,
        measurement=measurement,
        published_inventory=published_inventory,
        published_measurement=published_measurement,
        run_receipt=receipt,
        run_receipt_artifact_id=run_receipt_artifact_id,
        run_receipt_payload_sha256=receipt_digest,
    )
