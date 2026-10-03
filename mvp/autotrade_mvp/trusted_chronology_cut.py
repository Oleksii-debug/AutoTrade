"""Fail-closed public authority facade for WP-48 trusted chronology cuts.

The implementation remains byte-for-byte in ``_trusted_chronology_cut_impl``.
This facade closes composition hazards around terminal chronology authority:
caller-owned horizons are never standalone authority, and recovery/runtime
currentness is re-read after signed evidence verification before a horizon can
be used by a terminal consumer.
"""

from __future__ import annotations

import sys

from . import _trusted_chronology_cut_impl as _impl
from .recovery import OwnerFence, RecoveryController


_original_require_current_trusted_chronology_cut = (
    _impl.require_current_trusted_chronology_cut
)
_original_require_chronology_horizon = _impl.require_chronology_horizon


# The compatibility facade intentionally publishes a small number of replacements
# onto the implementation module below. Everything else in the implementation
# namespace is part of the captured authority graph used by the original verifier
# and horizon checker. Snapshot identity now, before publishing those replacements,
# so later pre-call or callback-time rebinding fails closed instead of redirecting
# a function object whose ``__globals__`` still points at the mutable impl module.
_IMPL_SEAL_EXCLUDED_NAMES = frozenset(
    {
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
    """Reject module rebinding and shallow mutation of captured authority types."""

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

    return require_impl_namespace_sealed


_require_impl_namespace_sealed = _build_impl_namespace_guard(_impl.__dict__)
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
    """Build a focused verifier after test doubles are installed.

    Production authority never calls this helper. Legacy chronology tests must
    inject signer/evidence doubles without rebinding the already-sealed production
    verifier. Capturing a fresh namespace guard here treats the explicit test
    doubles as that verifier's baseline while still detecting any rebinding or
    authority-type namespace mutation that occurs from inside a verifier callback.
    """

    return _build_current_cut_with_horizon(
        require_current_cut=_original_require_current_trusted_chronology_cut,
        require_post_currentness=_require_post_verification_currentness,
        require_horizon=_original_require_chronology_horizon,
        require_impl_namespace_sealed=_build_impl_namespace_guard(_impl.__dict__),
    )


def _reject_standalone_chronology_horizon(*_args: object, **_kwargs: object) -> None:
    """Prevent caller-owned cuts from becoming standalone horizon authority."""

    raise PermissionError(
        "standalone chronology horizon is not authority; "
        "use require_current_trusted_chronology_cut with claimed_instants"
    )


_impl._build_post_verification_currentness = _build_post_verification_currentness
_impl._build_current_cut_with_horizon = _build_current_cut_with_horizon
_impl._build_impl_namespace_guard = _build_impl_namespace_guard
_impl._build_named_namespace_guard = _build_named_namespace_guard
_impl._build_test_current_cut_verifier = _build_test_current_cut_verifier
_impl.require_current_trusted_chronology_cut = (
    _require_current_trusted_chronology_cut_with_horizon
)
_impl.require_chronology_horizon = _reject_standalone_chronology_horizon

# Preserve the implementation module object so existing exact-head tests that
# deliberately patch private verifier/time seams continue to exercise the real
# implementation rather than a second copy of module globals. Production terminal
# currentness above no longer dereferences those patched globals after construction.
sys.modules[__name__] = _impl
