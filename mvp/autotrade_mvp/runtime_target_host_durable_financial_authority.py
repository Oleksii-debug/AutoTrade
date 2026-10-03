"""Captured release-bound durable-financial authority for terminal WP-65.

The public durable-financial facade remains an explicit focused-test seam. Product
composed qualification uses the verifier constructed here so rebinding measurement
snapshotting, delivered-release validation, parent target-host evidence collection,
error classes, lower durable mechanics, or their authority-bearing dependency
graphs after import cannot retarget terminal PASS.

This module does not create provider truth, chronology, signer policy, release
authority, trading authority, profitability, or economic edge.
"""

from __future__ import annotations

from types import FunctionType
from uuid import UUID

from .performance_qualification import RuntimeBudgetError, RuntimeBudgetSpec
from .persistence import (
    JournalStore,
    journal_store_authority_scope,
    require_exact_journal_store_authority,
)
from .runtime_load_measurement import load_declared_financial_latency_samples
from .runtime_load_plan import load_declared_runtime_event_plan
from .runtime_load_qualification import RuntimeCampaignCut, RuntimeCampaignPlan
from .runtime_target_host_durable_financial import (
    DurableTargetHostFinancialBinding,
    RuntimeTargetHostDurableFinancialError,
    bind_durable_financial_latency_to_target_host_measurement,
)
from .runtime_target_host_measurement import (
    RuntimeTargetHostMeasurementError,
    TargetHostMeasurementArtifact,
    snapshot_target_host_measurement,
)
from .runtime_target_host_measurement_authority import (
    collect_release_bound_target_host_evidence,
)


def _build_module_authority_guard(*, root, error_type, label: str):
    """Freeze one callable's same-module executable dependency graph.

    Capturing a function object alone does not freeze names resolved through its
    module globals. This guard snapshots the root code object, every same-module
    function reachable through executable globals, executable methods on
    same-module classes, and every global binding those functions resolve.
    Runtime qualification fails closed if any captured edge is rebound.
    """

    if type(root) is not FunctionType:
        raise TypeError("root must be an exact Python function")
    if type(label) is not str or not label:
        raise TypeError("label must be non-empty exact text")

    module_name = root.__module__
    missing = object()
    function_states = []
    global_bindings = []
    class_bindings = []
    seen_functions = set()
    seen_classes = set()
    seen_globals = set()
    seen_class_bindings = set()

    def visit_function(function):
        if type(function) is not FunctionType or function.__module__ != module_name:
            return
        identity = id(function)
        if identity in seen_functions:
            return
        seen_functions.add(identity)

        kwdefaults = function.__kwdefaults__
        frozen_kwdefaults = None if kwdefaults is None else dict(kwdefaults)
        function_states.append(
            (
                function,
                function.__code__,
                function.__defaults__,
                frozen_kwdefaults,
            )
        )

        namespace = function.__globals__
        for name in function.__code__.co_names:
            if name not in namespace:
                continue
            expected = namespace[name]
            key = (id(namespace), name)
            if key not in seen_globals:
                seen_globals.add(key)
                global_bindings.append((namespace, name, expected))
            if type(expected) is FunctionType and expected.__module__ == module_name:
                visit_function(expected)
            elif isinstance(expected, type) and expected.__module__ == module_name:
                visit_class(expected)

    def visit_class(cls):
        identity = id(cls)
        if identity in seen_classes:
            return
        seen_classes.add(identity)

        for name, raw in cls.__dict__.items():
            functions = ()
            if type(raw) is FunctionType:
                functions = (raw,)
            elif isinstance(raw, staticmethod):
                functions = (raw.__func__,)
            elif isinstance(raw, classmethod):
                functions = (raw.__func__,)
            elif isinstance(raw, property):
                functions = tuple(
                    function
                    for function in (raw.fget, raw.fset, raw.fdel)
                    if function is not None
                )
            else:
                continue

            key = (id(cls), name)
            if key not in seen_class_bindings:
                seen_class_bindings.add(key)
                class_bindings.append((cls, name, raw))
            for function in functions:
                visit_function(function)

    visit_function(root)

    frozen_function_states = tuple(function_states)
    frozen_global_bindings = tuple(global_bindings)
    frozen_class_bindings = tuple(class_bindings)

    def require_intact():
        for function, code, defaults, kwdefaults in frozen_function_states:
            if function.__code__ is not code or function.__defaults__ is not defaults:
                raise error_type(f"{label} sealed executable changed")
            current_kwdefaults = function.__kwdefaults__
            if kwdefaults is None:
                if current_kwdefaults is not None:
                    raise error_type(f"{label} sealed defaults changed")
            elif type(current_kwdefaults) is not dict or current_kwdefaults != kwdefaults:
                raise error_type(f"{label} sealed defaults changed")

        for namespace, name, expected in frozen_global_bindings:
            if namespace.get(name, missing) is not expected:
                raise error_type(f"{label} sealed dependency changed: {name}")

        for cls, name, expected in frozen_class_bindings:
            if cls.__dict__.get(name, missing) is not expected:
                raise error_type(
                    f"{label} sealed class executable changed: "
                    f"{cls.__name__}.{name}"
                )

    return require_intact


def _build_release_bound_durable_financial_authority(
    *,
    measurement_type,
    measurement_snapshotter,
    uuid_type,
    durable_error_type,
    parent_collector,
    parent_error_types,
    durable_binder,
    measurement_dependency_guard=None,
    parent_dependency_guard=None,
    durable_dependency_guard=None,
    durable_external_dependency_guards=(),
):
    """Build one release-bound binder with immutable dependency captures."""

    if type(durable_external_dependency_guards) is not tuple or any(
        not callable(guard) for guard in durable_external_dependency_guards
    ):
        raise TypeError("durable_external_dependency_guards must be exact tuple of callables")

    def canonical_uuid(value: object, *, name: str) -> str:
        if type(value) is not str or not value or value != value.strip():
            raise durable_error_type(f"{name} must be a canonical UUID")
        try:
            canonical = str(uuid_type(value))
        except (ValueError, TypeError, AttributeError) as error:
            raise durable_error_type(f"{name} must be a canonical UUID") from error
        if canonical != value:
            raise durable_error_type(f"{name} must be a canonical UUID")
        return canonical

    def canonical_digest(value: object, *, name: str) -> str:
        if (
            type(value) is not str
            or len(value) != 71
            or not value.startswith("sha256:")
            or value != value.lower()
            or any(char not in "0123456789abcdef" for char in value[7:])
        ):
            raise durable_error_type(f"{name} must be canonical sha256:<64 hex>")
        return value

    def require_durable_graphs() -> None:
        if durable_dependency_guard is not None:
            durable_dependency_guard()
        for guard in durable_external_dependency_guards:
            guard()

    def bind(
        *,
        store: JournalStore,
        spec: RuntimeBudgetSpec,
        campaign_plan: RuntimeCampaignPlan,
        campaign_cut: RuntimeCampaignCut,
        declared_plan_id: str,
        measurement: TargetHostMeasurementArtifact,
        expected_release_artifact_id: str,
        expected_release_artifact_sha256: str,
    ) -> DurableTargetHostFinancialBinding:
        if type(measurement) is not measurement_type:
            raise TypeError("measurement must be exact TargetHostMeasurementArtifact")
        if measurement_dependency_guard is not None:
            measurement_dependency_guard()
        measurement_authority = measurement_snapshotter(measurement)
        frozen_release_artifact_id = canonical_uuid(
            expected_release_artifact_id,
            name="expected_release_artifact_id",
        )
        frozen_release_artifact_sha256 = canonical_digest(
            expected_release_artifact_sha256,
            name="expected_release_artifact_sha256",
        )
        if measurement_authority.release_artifact_id != frozen_release_artifact_id:
            raise durable_error_type(
                "target-host measurement belongs to another delivered release artifact"
            )
        if (
            measurement_authority.release_artifact_sha256
            != frozen_release_artifact_sha256
        ):
            raise durable_error_type(
                "target-host measurement belongs to another delivered release digest"
            )

        if parent_dependency_guard is not None:
            parent_dependency_guard()
        try:
            parent_collector(
                journal=store,
                spec=spec,
                plan=campaign_plan,
                cut=campaign_cut,
                measurement=measurement_authority,
                expected_release_artifact_id=frozen_release_artifact_id,
                expected_release_artifact_sha256=frozen_release_artifact_sha256,
            )
        except parent_error_types as error:
            raise durable_error_type(str(error)) from error

        # The low-level durable binder snapshots the measurement again. The parent
        # evidence collector runs between the first snapshot and that nested use,
        # so a callback/side effect there must not be able to retarget the
        # measurement module's executable graph after the first guard passed.
        if measurement_dependency_guard is not None:
            measurement_dependency_guard()
        require_durable_graphs()
        result = durable_binder(
            store,
            spec,
            declared_plan_id=declared_plan_id,
            measurement=measurement_authority,
        )
        # Persistent mutation during durable projection must not survive into a
        # terminal accepted result even when the root callable identity is stable.
        require_durable_graphs()
        return result

    return bind


_PRODUCTION_MEASUREMENT_SNAPSHOT_GUARD = _build_module_authority_guard(
    root=snapshot_target_host_measurement,
    error_type=RuntimeTargetHostDurableFinancialError,
    label="target-host measurement snapshotter",
)
_PRODUCTION_PARENT_EVIDENCE_GUARD = _build_module_authority_guard(
    root=collect_release_bound_target_host_evidence,
    error_type=RuntimeTargetHostDurableFinancialError,
    label="release-bound target-host evidence collector",
)
_PRODUCTION_DURABLE_BINDER_GUARD = _build_module_authority_guard(
    root=bind_durable_financial_latency_to_target_host_measurement,
    error_type=RuntimeTargetHostDurableFinancialError,
    label="durable-financial binder",
)
_PRODUCTION_JOURNAL_STORE_AUTHORITY_GUARD = _build_module_authority_guard(
    root=require_exact_journal_store_authority,
    error_type=RuntimeTargetHostDurableFinancialError,
    label="durable-financial JournalStore authority",
)
_PRODUCTION_JOURNAL_STORE_SCOPE_GUARD = _build_module_authority_guard(
    root=journal_store_authority_scope.__wrapped__,
    error_type=RuntimeTargetHostDurableFinancialError,
    label="durable-financial JournalStore scope",
)
_PRODUCTION_DECLARED_PLAN_LOADER_GUARD = _build_module_authority_guard(
    root=load_declared_runtime_event_plan,
    error_type=RuntimeTargetHostDurableFinancialError,
    label="durable-financial declared-plan loader",
)
_PRODUCTION_DURABLE_SAMPLE_LOADER_GUARD = _build_module_authority_guard(
    root=load_declared_financial_latency_samples,
    error_type=RuntimeTargetHostDurableFinancialError,
    label="durable-financial sample loader",
)
_PRODUCTION_DURABLE_EXTERNAL_DEPENDENCY_GUARDS = (
    _PRODUCTION_JOURNAL_STORE_AUTHORITY_GUARD,
    _PRODUCTION_JOURNAL_STORE_SCOPE_GUARD,
    _PRODUCTION_DECLARED_PLAN_LOADER_GUARD,
    _PRODUCTION_DURABLE_SAMPLE_LOADER_GUARD,
)


bind_sealed_release_bound_durable_financial_latency_to_target_host_measurement = (
    _build_release_bound_durable_financial_authority(
        measurement_type=TargetHostMeasurementArtifact,
        measurement_snapshotter=snapshot_target_host_measurement,
        uuid_type=UUID,
        durable_error_type=RuntimeTargetHostDurableFinancialError,
        parent_collector=collect_release_bound_target_host_evidence,
        parent_error_types=(RuntimeTargetHostMeasurementError, RuntimeBudgetError),
        durable_binder=bind_durable_financial_latency_to_target_host_measurement,
        measurement_dependency_guard=_PRODUCTION_MEASUREMENT_SNAPSHOT_GUARD,
        parent_dependency_guard=_PRODUCTION_PARENT_EVIDENCE_GUARD,
        durable_dependency_guard=_PRODUCTION_DURABLE_BINDER_GUARD,
        durable_external_dependency_guards=_PRODUCTION_DURABLE_EXTERNAL_DEPENDENCY_GUARDS,
    )
)
