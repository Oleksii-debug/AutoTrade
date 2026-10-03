"""Fail-closed public authority facade for WP-48 trusted chronology cuts.

The implementation remains byte-for-byte in ``_trusted_chronology_cut_impl``.
This facade closes composition hazards around terminal chronology authority:
caller-owned horizons are never standalone authority, and recovery/runtime
currentness is re-read after signed evidence verification before a horizon can
be used by a terminal consumer.
"""

from __future__ import annotations

import dis
import sys
from types import FunctionType, ModuleType

from . import _trusted_chronology_cut_impl as _impl
from .recovery import OwnerFence, RecoveryController


_original_require_current_trusted_chronology_cut = (
    _impl.require_current_trusted_chronology_cut
)
_original_require_chronology_horizon = _impl.require_chronology_horizon
_original_verify_canonical_qualification_attestation = (
    _impl.verify_canonical_qualification_attestation
)
_original_trusted_authenticated_reader = _impl.trusted_authenticated_reader
_original_parse_challenge_bound_measurement = _impl.parse_challenge_bound_measurement
_original_parse_signed_qualification_attestation = (
    _impl.parse_signed_qualification_attestation
)


_IMPL_SEAL_EXCLUDED_NAMES = frozenset(
    {
        "_build_callback_authority_wrappers",
        "_build_external_function_graph_guard",
        "_build_guarded_parser",
        "_build_post_verification_currentness",
        "_build_current_cut_with_horizon",
        "_build_impl_namespace_guard",
        "_build_named_namespace_guard",
        "_build_test_current_cut_verifier",
        "require_current_trusted_chronology_cut",
        "require_chronology_horizon",
    }
)


def _capture_type_namespace(value: type) -> tuple[frozenset[str], tuple[tuple[str, object], ...]]:
    namespace = value.__dict__
    return frozenset(namespace), tuple(namespace.items())


def _require_type_namespace(
    name: str,
    expected_type: type,
    expected_names: frozenset[str],
    expected_members: tuple[tuple[str, object], ...],
) -> None:
    current_namespace = expected_type.__dict__
    if frozenset(current_namespace) != expected_names:
        raise RuntimeError(
            "trusted chronology implementation type authority changed: " + name
        )
    for member_name, expected_member in expected_members:
        if current_namespace.get(member_name) is not expected_member:
            raise RuntimeError(
                "trusted chronology implementation type authority changed: "
                + name
                + "."
                + member_name
            )


def _build_named_namespace_guard(
    namespace: dict[str, object],
    *,
    names: frozenset[str],
    label: str,
):
    """Seal an explicit transitive globals subset and its authority types."""

    if type(namespace) is not dict:
        raise TypeError("authority namespace must be exact dict")
    if type(names) is not frozenset or not names:
        raise TypeError("authority names must be a non-empty exact frozenset")
    if type(label) is not str or not label:
        raise TypeError("authority label must be exact non-empty text")
    captured = tuple((name, namespace.get(name)) for name in sorted(names))
    if any(value is None for _name, value in captured):
        raise RuntimeError(label + " authority namespace is incomplete")
    captured_types = tuple(
        (name, value, *_capture_type_namespace(value))
        for name, value in captured
        if isinstance(value, type)
    )

    def require_named_namespace_sealed() -> None:
        for name, expected in captured:
            if namespace.get(name) is not expected:
                raise RuntimeError(label + " authority changed: " + name)
        for name, expected_type, expected_names, expected_members in captured_types:
            _require_type_namespace(
                label + "." + name,
                expected_type,
                expected_names,
                expected_members,
            )

    return require_named_namespace_sealed


def _build_impl_namespace_guard(
    namespace: dict[str, object],
    *,
    excluded_names: frozenset[str] = _IMPL_SEAL_EXCLUDED_NAMES,
):
    """Reject rebinding or executable mutation of captured implementation authority."""

    if type(namespace) is not dict:
        raise TypeError("implementation namespace must be exact dict")
    if type(excluded_names) is not frozenset:
        raise TypeError("excluded_names must be exact frozenset")
    captured = tuple(
        (name, value)
        for name, value in namespace.items()
        if type(name) is str
        and not name.startswith("__")
        and name not in excluded_names
    )
    captured_types = tuple(
        (name, value, *_capture_type_namespace(value))
        for name, value in captured
        if isinstance(value, type)
    )
    captured_functions = tuple(
        (
            name,
            value,
            value.__code__,
            value.__defaults__,
            None if value.__kwdefaults__ is None else dict(value.__kwdefaults__),
            tuple(
                (cell, cell.cell_contents)
                for cell in (value.__closure__ or ())
            ),
        )
        for name, value in captured
        if type(value) is FunctionType
    )

    def require_impl_namespace_sealed() -> None:
        for name, expected in captured:
            if namespace.get(name) is not expected:
                raise RuntimeError(
                    "trusted chronology implementation authority changed: " + name
                )
        for name, expected_type, expected_names, expected_members in captured_types:
            _require_type_namespace(
                name,
                expected_type,
                expected_names,
                expected_members,
            )
        for (
            name,
            expected_function,
            expected_code,
            expected_defaults,
            expected_kwdefaults,
            expected_closure,
        ) in captured_functions:
            if expected_function.__code__ is not expected_code:
                raise RuntimeError(
                    "trusted chronology implementation executable changed: " + name
                )
            if expected_function.__defaults__ is not expected_defaults:
                raise RuntimeError(
                    "trusted chronology implementation defaults changed: " + name
                )
            current_kwdefaults = expected_function.__kwdefaults__
            if expected_kwdefaults is None:
                if current_kwdefaults is not None:
                    raise RuntimeError(
                        "trusted chronology implementation defaults changed: " + name
                    )
            elif (
                type(current_kwdefaults) is not dict
                or current_kwdefaults != expected_kwdefaults
            ):
                raise RuntimeError(
                    "trusted chronology implementation defaults changed: " + name
                )
            current_closure = expected_function.__closure__ or ()
            if len(current_closure) != len(expected_closure):
                raise RuntimeError(
                    "trusted chronology implementation closure changed: " + name
                )
            for current_cell, (expected_cell, expected_value) in zip(
                current_closure,
                expected_closure,
            ):
                if (
                    current_cell is not expected_cell
                    or current_cell.cell_contents is not expected_value
                ):
                    raise RuntimeError(
                        "trusted chronology implementation closure changed: " + name
                    )

    return require_impl_namespace_sealed


def _build_external_function_graph_guard(*, root, label: str):
    """Freeze one imported parser's reachable executable/class/module graph."""

    if type(root) is not FunctionType:
        raise TypeError("external parser root must be exact Python function")
    if type(label) is not str or not label:
        raise TypeError("external parser label must be exact non-empty text")

    module_name = root.__module__
    missing = object()
    function_states: list[tuple[object, ...]] = []
    global_bindings: list[tuple[dict[str, object], str, object]] = []
    closure_bindings: list[tuple[object, object]] = []
    class_bindings: list[tuple[type, str, object]] = []
    module_attribute_bindings: list[tuple[ModuleType, str, object]] = []
    module_attribute_function_states: list[tuple[FunctionType, object, object, object]] = []
    external_type_states: list[tuple[object, ...]] = []
    seen_functions: set[int] = set()
    seen_classes: set[int] = set()
    seen_globals: set[tuple[int, str]] = set()
    seen_closures: set[int] = set()
    seen_class_bindings: set[tuple[int, str]] = set()
    seen_module_attributes: set[tuple[int, str]] = set()
    seen_module_attribute_functions: set[int] = set()
    seen_external_types: set[int] = set()

    def executable_members(raw: object) -> tuple[FunctionType, ...]:
        if type(raw) is FunctionType:
            return (raw,)
        if isinstance(raw, staticmethod):
            return (raw.__func__,)
        if isinstance(raw, classmethod):
            return (raw.__func__,)
        if isinstance(raw, property):
            return tuple(
                function
                for function in (raw.fget, raw.fset, raw.fdel)
                if type(function) is FunctionType
            )
        return ()

    def capture_external_type(cls: type) -> None:
        identity = id(cls)
        if identity in seen_external_types:
            return
        seen_external_types.add(identity)
        namespace = cls.__dict__
        names = frozenset(namespace)
        members = tuple(namespace.items())
        executable_states = []
        for member_name, raw in members:
            for function in executable_members(raw):
                dependency_namespace = function.__globals__
                for dependency_name in function.__code__.co_names:
                    if dependency_name not in dependency_namespace:
                        continue
                    expected_dependency = dependency_namespace[dependency_name]
                    dependency_key = (id(dependency_namespace), dependency_name)
                    if dependency_key not in seen_globals:
                        seen_globals.add(dependency_key)
                        global_bindings.append(
                            (
                                dependency_namespace,
                                dependency_name,
                                expected_dependency,
                            )
                        )
                for cell in function.__closure__ or ():
                    cell_identity = id(cell)
                    if cell_identity in seen_closures:
                        continue
                    seen_closures.add(cell_identity)
                    try:
                        expected_cell_value = cell.cell_contents
                    except ValueError:
                        expected_cell_value = missing
                    closure_bindings.append((cell, expected_cell_value))
                executable_states.append(
                    (
                        member_name,
                        function,
                        function.__code__,
                        function.__defaults__,
                        None
                        if function.__kwdefaults__ is None
                        else dict(function.__kwdefaults__),
                    )
                )
        external_type_states.append(
            (cls, names, members, tuple(executable_states))
        )

    def capture_direct_module_attributes(function: FunctionType) -> None:
        namespace = function.__globals__
        instructions = tuple(dis.get_instructions(function))
        for index, instruction in enumerate(instructions[:-1]):
            if instruction.opname not in {"LOAD_GLOBAL", "LOAD_NAME"}:
                continue
            base = namespace.get(instruction.argval, missing)
            if type(base) is not ModuleType:
                continue
            next_instruction = instructions[index + 1]
            if next_instruction.opname not in {"LOAD_ATTR", "LOAD_METHOD"}:
                continue
            attribute_name = next_instruction.argval
            if type(attribute_name) is not str or attribute_name not in base.__dict__:
                continue
            expected = base.__dict__[attribute_name]
            key = (id(base), attribute_name)
            if key not in seen_module_attributes:
                seen_module_attributes.add(key)
                module_attribute_bindings.append((base, attribute_name, expected))
            if type(expected) is FunctionType and id(expected) not in seen_module_attribute_functions:
                seen_module_attribute_functions.add(id(expected))
                module_attribute_function_states.append(
                    (
                        expected,
                        expected.__code__,
                        expected.__defaults__,
                        None
                        if expected.__kwdefaults__ is None
                        else dict(expected.__kwdefaults__),
                    )
                )
            elif isinstance(expected, type):
                capture_external_type(expected)

    def visit_function(function) -> None:
        if type(function) is not FunctionType or function.__module__ != module_name:
            return
        identity = id(function)
        if identity in seen_functions:
            return
        seen_functions.add(identity)
        function_states.append(
            (
                function,
                function.__code__,
                function.__defaults__,
                None
                if function.__kwdefaults__ is None
                else dict(function.__kwdefaults__),
            )
        )
        capture_direct_module_attributes(function)
        for cell in function.__closure__ or ():
            cell_identity = id(cell)
            if cell_identity in seen_closures:
                continue
            seen_closures.add(cell_identity)
            try:
                expected = cell.cell_contents
            except ValueError:
                expected = missing
            closure_bindings.append((cell, expected))

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
            elif isinstance(expected, type):
                if expected.__module__ == module_name:
                    visit_class(expected)
                else:
                    capture_external_type(expected)

    def visit_class(cls: type) -> None:
        identity = id(cls)
        if identity in seen_classes:
            return
        seen_classes.add(identity)
        for name, raw in cls.__dict__.items():
            functions = executable_members(raw)
            if not functions:
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
    frozen_closure_bindings = tuple(closure_bindings)
    frozen_class_bindings = tuple(class_bindings)
    frozen_module_attribute_bindings = tuple(module_attribute_bindings)
    frozen_module_attribute_function_states = tuple(module_attribute_function_states)
    frozen_external_type_states = tuple(external_type_states)

    def require_external_graph_sealed() -> None:
        for function, code, defaults, kwdefaults in frozen_function_states:
            if function.__code__ is not code:
                raise RuntimeError(label + " executable changed")
            if function.__defaults__ is not defaults:
                raise RuntimeError(label + " defaults changed")
            current_kwdefaults = function.__kwdefaults__
            if kwdefaults is None:
                if current_kwdefaults is not None:
                    raise RuntimeError(label + " defaults changed")
            elif type(current_kwdefaults) is not dict or current_kwdefaults != kwdefaults:
                raise RuntimeError(label + " defaults changed")
        for namespace, name, expected in frozen_global_bindings:
            if namespace.get(name, missing) is not expected:
                raise RuntimeError(label + " dependency changed: " + name)
        for cell, expected in frozen_closure_bindings:
            try:
                current = cell.cell_contents
            except ValueError:
                current = missing
            if current is not expected:
                raise RuntimeError(label + " closure changed")
        for cls, name, expected in frozen_class_bindings:
            if cls.__dict__.get(name, missing) is not expected:
                raise RuntimeError(
                    label + " class executable changed: " + cls.__name__ + "." + name
                )
        for module, attribute_name, expected in frozen_module_attribute_bindings:
            if module.__dict__.get(attribute_name, missing) is not expected:
                raise RuntimeError(
                    label
                    + " module attribute changed: "
                    + module.__name__
                    + "."
                    + attribute_name
                )
        for function, code, defaults, kwdefaults in frozen_module_attribute_function_states:
            if function.__code__ is not code or function.__defaults__ is not defaults:
                raise RuntimeError(label + " module function executable changed")
            current_kwdefaults = function.__kwdefaults__
            if kwdefaults is None:
                if current_kwdefaults is not None:
                    raise RuntimeError(label + " module function defaults changed")
            elif type(current_kwdefaults) is not dict or current_kwdefaults != kwdefaults:
                raise RuntimeError(label + " module function defaults changed")
        for cls, expected_names, expected_members, executable_states in frozen_external_type_states:
            current_namespace = cls.__dict__
            class_label = cls.__module__ + "." + cls.__name__
            if frozenset(current_namespace) != expected_names:
                raise RuntimeError(
                    label + " external class namespace changed: " + class_label
                )
            for member_name, expected_member in expected_members:
                if current_namespace.get(member_name, missing) is not expected_member:
                    raise RuntimeError(
                        label
                        + " external class member changed: "
                        + class_label
                        + "."
                        + member_name
                    )
            for member_name, function, code, defaults, kwdefaults in executable_states:
                if function.__code__ is not code or function.__defaults__ is not defaults:
                    raise RuntimeError(
                        label
                        + " external class executable changed: "
                        + class_label
                        + "."
                        + member_name
                    )
                current_kwdefaults = function.__kwdefaults__
                if kwdefaults is None:
                    if current_kwdefaults is not None:
                        raise RuntimeError(
                            label
                            + " external class defaults changed: "
                            + class_label
                            + "."
                            + member_name
                        )
                elif type(current_kwdefaults) is not dict or current_kwdefaults != kwdefaults:
                    raise RuntimeError(
                        label
                        + " external class defaults changed: "
                        + class_label
                        + "."
                        + member_name
                    )

    return require_external_graph_sealed


def _build_guarded_parser(*, parser, require_parser_authority):
    """Dispatch one retained parser only while its external graph remains sealed."""

    if type(parser) is not FunctionType:
        raise TypeError("parser must be exact Python function")
    if not callable(require_parser_authority):
        raise TypeError("require_parser_authority must be callable")

    def guarded_parser(*args, **kwargs):
        require_parser_authority()
        result = parser(*args, **kwargs)
        require_parser_authority()
        return result

    return guarded_parser


def _build_callback_authority_wrappers(
    *,
    canonical_verifier,
    authenticated_reader_factory,
    require_callback_authority,
):
    """Guard every callback return before chronology code consumes mutable globals."""

    if not callable(canonical_verifier):
        raise TypeError("canonical_verifier must be callable")
    if not callable(authenticated_reader_factory):
        raise TypeError("authenticated_reader_factory must be callable")
    if not callable(require_callback_authority):
        raise TypeError("require_callback_authority must be callable")

    def guarded_canonical_verifier(*args, **kwargs):
        result = canonical_verifier(*args, **kwargs)
        require_callback_authority()
        return result

    def guarded_authenticated_reader_factory(*args, **kwargs):
        reader = authenticated_reader_factory(*args, **kwargs)
        require_callback_authority()
        if not callable(reader):
            raise TypeError("trusted authenticated reader factory returned non-callable")

        def guarded_reader(*reader_args, **reader_kwargs):
            result = reader(*reader_args, **reader_kwargs)
            require_callback_authority()
            return result

        return guarded_reader

    return guarded_canonical_verifier, guarded_authenticated_reader_factory


_measurement_parser_guard = _build_external_function_graph_guard(
    root=_original_parse_challenge_bound_measurement,
    label="trusted chronology measurement parser",
)
_signed_receipt_parser_guard = _build_external_function_graph_guard(
    root=_original_parse_signed_qualification_attestation,
    label="trusted chronology signed receipt parser",
)
_callback_guard_exclusions = _IMPL_SEAL_EXCLUDED_NAMES | frozenset(
    {
        "verify_canonical_qualification_attestation",
        "trusted_authenticated_reader",
        "parse_challenge_bound_measurement",
        "parse_signed_qualification_attestation",
    }
)
_callback_impl_guard = _build_impl_namespace_guard(
    _impl.__dict__,
    excluded_names=_callback_guard_exclusions,
)


def _require_callback_authority_sealed() -> None:
    _callback_impl_guard()
    _measurement_parser_guard()
    _signed_receipt_parser_guard()


(
    _guarded_verify_canonical_qualification_attestation,
    _guarded_trusted_authenticated_reader,
) = _build_callback_authority_wrappers(
    canonical_verifier=_original_verify_canonical_qualification_attestation,
    authenticated_reader_factory=_original_trusted_authenticated_reader,
    require_callback_authority=_require_callback_authority_sealed,
)
_guarded_parse_challenge_bound_measurement = _build_guarded_parser(
    parser=_original_parse_challenge_bound_measurement,
    require_parser_authority=_measurement_parser_guard,
)
_guarded_parse_signed_qualification_attestation = _build_guarded_parser(
    parser=_original_parse_signed_qualification_attestation,
    require_parser_authority=_signed_receipt_parser_guard,
)
_impl.verify_canonical_qualification_attestation = (
    _guarded_verify_canonical_qualification_attestation
)
_impl.trusted_authenticated_reader = _guarded_trusted_authenticated_reader
_impl.parse_challenge_bound_measurement = _guarded_parse_challenge_bound_measurement
_impl.parse_signed_qualification_attestation = (
    _guarded_parse_signed_qualification_attestation
)
_callback_impl_guard = _build_impl_namespace_guard(_impl.__dict__)
_require_impl_namespace_sealed = _callback_impl_guard
_require_recovery_owner_globals_sealed = _build_named_namespace_guard(
    RecoveryController.durable_owner_chain.__globals__,
    names=frozenset(
        {
            "JournalStore",
            "OwnerFence",
            "durable_clock_incident_generation",
            "payload_digest",
            "require_exact_journal_store_authority",
            "require_exact_journal_store_identity",
        }
    ),
    label="trusted chronology recovery",
)
_require_runtime_occurrence_globals_sealed = _build_named_namespace_guard(
    _impl.require_current_production_host_runtime_occurrence.__globals__,
    names=frozenset(
        {
            "JournalStore",
            "ProductionHostConfig",
            "ProductionHostRuntimeOccurrence",
            "_RUNTIME_OCCURRENCE_AGGREGATE_TYPE",
            "_RUNTIME_OCCURRENCE_EVENT_TYPE",
            "_RUNTIME_OCCURRENCE_PAYLOAD_FIELDS",
            "_RUNTIME_OCCURRENCE_SCHEMA_VERSION",
            "_load_production_host_runtime_occurrences",
            "_readmit_production_host_config",
            "_runtime_occurrence_from_event",
            "_runtime_occurrence_from_scoped_event",
            "journal_store_authority_scope",
            "require_exact_journal_store_authority",
        }
    ),
    label="trusted chronology production runtime",
)


def _build_post_verification_currentness(
    *,
    recovery_type,
    owner_fence_type,
    durable_owner_chain,
    require_recovery_owner_globals_sealed,
    require_runtime_occurrence_globals_sealed,
    chronology_scope_type,
    production_runtime_type,
    runtime_occurrence_type,
    selected_store_identity,
    require_journal_authority,
    require_current_runtime_occurrence,
    owner_account,
):
    """Capture mutable post-callback authority dependencies before any verifier runs."""

    def require_post_verification_currentness(
        durable: _impl.TrustedChronologyCut,
        *,
        kwargs: dict[str, object],
    ) -> None:
        recovery = kwargs["recovery"]
        if type(recovery) is not recovery_type:
            raise TypeError("recovery must be exact RecoveryController")

        require_recovery_owner_globals_sealed()
        store = kwargs["store"]
        selected_identity = selected_store_identity(store)
        if recovery.durable_owner_store_identity != selected_identity:
            raise PermissionError("trusted chronology recovery JournalStore changed")
        if recovery.clock_trusted is not True:
            raise PermissionError("trusted chronology clock health is no longer trusted")
        if recovery.clock_incident_generation != durable.clock_incident_generation:
            raise PermissionError("trusted chronology clock incident generation changed")
        if recovery.owner_scope != durable.owner_scope:
            raise PermissionError("trusted chronology durable recovery owner scope changed")

        require_recovery_owner_globals_sealed()
        owner_chain = durable_owner_chain(recovery)
        require_recovery_owner_globals_sealed()
        if type(owner_chain) is not tuple or not owner_chain:
            raise PermissionError("trusted chronology durable recovery owner is unavailable")
        if not all(type(owner) is owner_fence_type for owner in owner_chain):
            raise PermissionError("trusted chronology durable recovery owner is non-canonical")
        latest_owner = owner_chain[-1]
        if (
            latest_owner.owner_id != durable.owner_id
            or latest_owner.epoch != durable.owner_epoch
        ):
            raise PermissionError("trusted chronology durable recovery owner changed")

        runtime = kwargs.get("runtime")
        if durable.scope is chronology_scope_type.SOURCE_QUALIFICATION:
            if runtime is not None:
                raise PermissionError(
                    "SOURCE_QUALIFICATION accepted cut cannot carry production runtime"
                )
            return

        if type(runtime) is not production_runtime_type:
            raise TypeError("RELEASE_RUNTIME requires exact ProductionHostRuntime")
        require_runtime_occurrence_globals_sealed()
        runtime_identity = require_journal_authority(
            runtime.journal,
            subject="trusted chronology production runtime JournalStore",
        )
        if runtime_identity != selected_identity:
            raise PermissionError(
                "production runtime does not share trusted chronology JournalStore"
            )
        occurrence = runtime.runtime_occurrence
        require_runtime_occurrence_globals_sealed()
        if type(occurrence) is not runtime_occurrence_type:
            raise TypeError("production runtime occurrence is not canonical")
        occurrence = require_current_runtime_occurrence(
            journal=store,
            occurrence=occurrence,
        )
        require_runtime_occurrence_globals_sealed()
        if (
            occurrence.host_id != durable.runtime_host_id
            or occurrence.runtime_occurrence_id != durable.runtime_occurrence_id
            or occurrence.aggregate_version != durable.runtime_occurrence_version
            or occurrence.journal_sequence != durable.runtime_occurrence_journal_sequence
        ):
            raise PermissionError(
                "trusted chronology production runtime occurrence changed"
            )
        if occurrence.environment != durable.runtime_environment:
            raise PermissionError(
                "trusted chronology production runtime environment changed"
            )
        if occurrence.account_id != owner_account(durable.owner_scope):
            raise PermissionError(
                "trusted chronology production runtime account changed"
            )

    return require_post_verification_currentness


_require_post_verification_currentness = _build_post_verification_currentness(
    recovery_type=RecoveryController,
    owner_fence_type=OwnerFence,
    durable_owner_chain=RecoveryController.durable_owner_chain,
    require_recovery_owner_globals_sealed=_require_recovery_owner_globals_sealed,
    require_runtime_occurrence_globals_sealed=_require_runtime_occurrence_globals_sealed,
    chronology_scope_type=_impl.ChronologyScope,
    production_runtime_type=_impl.ProductionHostRuntime,
    runtime_occurrence_type=_impl.ProductionHostRuntimeOccurrence,
    selected_store_identity=_impl._selected_store_identity,
    require_journal_authority=_impl.require_exact_journal_store_authority,
    require_current_runtime_occurrence=_impl.require_current_production_host_runtime_occurrence,
    owner_account=_impl._owner_account,
)


def _build_current_cut_with_horizon(
    *,
    require_current_cut,
    require_post_currentness,
    require_horizon,
    require_impl_namespace_sealed,
):
    """Capture terminal current-cut composition before callback-driven rebinding."""

    def require_current_trusted_chronology_cut_with_horizon(
        *,
        claimed_instants: tuple[str, ...] = (),
        **kwargs: object,
    ):
        if type(claimed_instants) is not tuple:
            raise TypeError("claimed_instants must be exact tuple")
        require_impl_namespace_sealed()
        durable = require_current_cut(**kwargs)
        require_impl_namespace_sealed()
        require_post_currentness(durable, kwargs=kwargs)
        require_impl_namespace_sealed()
        require_horizon(durable, *claimed_instants)
        require_impl_namespace_sealed()
        return durable

    return require_current_trusted_chronology_cut_with_horizon


_require_current_trusted_chronology_cut_with_horizon = _build_current_cut_with_horizon(
    require_current_cut=_original_require_current_trusted_chronology_cut,
    require_post_currentness=_require_post_verification_currentness,
    require_horizon=_original_require_chronology_horizon,
    require_impl_namespace_sealed=_require_impl_namespace_sealed,
)


def _build_test_current_cut_verifier():
    """Build a focused verifier after test doubles are installed."""

    return _build_current_cut_with_horizon(
        require_current_cut=_original_require_current_trusted_chronology_cut,
        require_post_currentness=_require_post_verification_currentness,
        require_horizon=_original_require_chronology_horizon,
        require_impl_namespace_sealed=_build_impl_namespace_guard(_impl.__dict__),
    )


def _reject_standalone_chronology_horizon(*_args: object, **_kwargs: object) -> None:
    raise PermissionError(
        "standalone chronology horizon is not authority; "
        "use require_current_trusted_chronology_cut with claimed_instants"
    )


_impl._build_callback_authority_wrappers = _build_callback_authority_wrappers
_impl._build_external_function_graph_guard = _build_external_function_graph_guard
_impl._build_guarded_parser = _build_guarded_parser
_impl._build_post_verification_currentness = _build_post_verification_currentness
_impl._build_current_cut_with_horizon = _build_current_cut_with_horizon
_impl._build_impl_namespace_guard = _build_impl_namespace_guard
_impl._build_named_namespace_guard = _build_named_namespace_guard
_impl._build_test_current_cut_verifier = _build_test_current_cut_verifier
_impl.require_current_trusted_chronology_cut = (
    _require_current_trusted_chronology_cut_with_horizon
)
_impl.require_chronology_horizon = _reject_standalone_chronology_horizon

sys.modules[__name__] = _impl
