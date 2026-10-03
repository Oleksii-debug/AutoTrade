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
from .recovery import RecoveryController


_original_require_current_trusted_chronology_cut = (
    _impl.require_current_trusted_chronology_cut
)
_original_require_chronology_horizon = _impl.require_chronology_horizon


def _require_post_verification_currentness(
    durable: _impl.TrustedChronologyCut,
    *,
    kwargs: dict[str, object],
) -> None:
    """Re-read mutable durable/runtime fences after signed-evidence callbacks."""

    recovery = kwargs["recovery"]
    if type(recovery) is not RecoveryController:
        raise TypeError("recovery must be exact RecoveryController")
    if recovery.clock_trusted is not True:
        raise PermissionError("trusted chronology clock health is no longer trusted")
    if recovery.clock_incident_generation != durable.clock_incident_generation:
        raise PermissionError("trusted chronology clock incident generation changed")
    if recovery.owner_scope != durable.owner_scope:
        raise PermissionError("trusted chronology durable recovery owner scope changed")

    owner_chain = RecoveryController.durable_owner_chain(recovery)
    if not owner_chain:
        raise PermissionError("trusted chronology durable recovery owner is unavailable")
    latest_owner = owner_chain[-1]
    if (
        latest_owner.owner_id != durable.owner_id
        or latest_owner.epoch != durable.owner_epoch
    ):
        raise PermissionError("trusted chronology durable recovery owner changed")

    runtime = kwargs.get("runtime")
    if durable.scope is _impl.ChronologyScope.SOURCE_QUALIFICATION:
        if runtime is not None:
            raise PermissionError(
                "SOURCE_QUALIFICATION accepted cut cannot carry production runtime"
            )
        return

    if type(runtime) is not _impl.ProductionHostRuntime:
        raise TypeError("RELEASE_RUNTIME requires exact ProductionHostRuntime")
    store = kwargs["store"]
    selected_identity = _impl._selected_store_identity(store)
    runtime_identity = _impl.require_exact_journal_store_authority(
        runtime.journal,
        subject="trusted chronology production runtime JournalStore",
    )
    if runtime_identity != selected_identity:
        raise PermissionError(
            "production runtime does not share trusted chronology JournalStore"
        )
    occurrence = runtime.runtime_occurrence
    if type(occurrence) is not _impl.ProductionHostRuntimeOccurrence:
        raise TypeError("production runtime occurrence is not canonical")
    occurrence = _impl.require_current_production_host_runtime_occurrence(
        journal=store,
        occurrence=occurrence,
    )
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
    if occurrence.account_id != _impl._owner_account(durable.owner_scope):
        raise PermissionError(
            "trusted chronology production runtime account changed"
        )


def _require_current_trusted_chronology_cut_with_horizon(
    *,
    claimed_instants: tuple[str, ...] = (),
    **kwargs: object,
):
    """Reverify durable authority, then evaluate its conservative UTC horizon."""

    if type(claimed_instants) is not tuple:
        raise TypeError("claimed_instants must be exact tuple")
    durable = _original_require_current_trusted_chronology_cut(**kwargs)
    _require_post_verification_currentness(durable, kwargs=kwargs)
    _original_require_chronology_horizon(durable, *claimed_instants)
    return durable


def _reject_standalone_chronology_horizon(*_args: object, **_kwargs: object) -> None:
    """Prevent caller-owned cuts from becoming standalone horizon authority."""

    raise PermissionError(
        "standalone chronology horizon is not authority; "
        "use require_current_trusted_chronology_cut with claimed_instants"
    )


_impl.require_current_trusted_chronology_cut = (
    _require_current_trusted_chronology_cut_with_horizon
)
_impl.require_chronology_horizon = _reject_standalone_chronology_horizon

# Preserve the implementation module object so existing exact-head tests that
# deliberately patch private verifier/time seams continue to exercise the real
# implementation rather than a second copy of module globals.
sys.modules[__name__] = _impl
