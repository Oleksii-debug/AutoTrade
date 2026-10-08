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


def _require_post_verification_currentness(
    durable: _impl.TrustedChronologyCut,
    *,
    kwargs: dict[str, object],
) -> None:
    """Re-read mutable durable/runtime fences after signed-evidence callbacks."""

    store = kwargs["store"]
    selected_identity = _impl._selected_store_identity(store)
    if (
        _impl.journal_store_identity_digest(selected_identity)
        != durable.store_identity_digest
    ):
        raise PermissionError(
            "trusted chronology JournalStore identity changed during verification"
        )

    recovery = kwargs["recovery"]
    if type(recovery) is not RecoveryController:
        raise TypeError("recovery must be exact RecoveryController")
    if recovery.durable_owner_store_identity != selected_identity:
        raise PermissionError(
            "trusted chronology recovery JournalStore changed during verification"
        )
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

    # Signature/evidence verification is an I/O callback boundary. Recheck the
    # live controller view as well as the durable chain so a local owner swap
    # cannot leave a current cut paired with an inconsistent RecoveryController.
    local_owner = recovery.owner
    if (
        type(local_owner) is not OwnerFence
        or local_owner.owner_id != durable.owner_id
        or local_owner.epoch != durable.owner_epoch
    ):
        raise PermissionError(
            "trusted chronology local recovery owner changed during verification"
        )

    runtime = kwargs.get("runtime")
    if durable.scope is _impl.ChronologyScope.SOURCE_QUALIFICATION:
        if runtime is not None:
            raise PermissionError(
                "SOURCE_QUALIFICATION accepted cut cannot carry production runtime"
            )
        return

    if type(runtime) is not _impl.ProductionHostRuntime:
        raise TypeError("RELEASE_RUNTIME requires exact ProductionHostRuntime")
    runtime_store = kwargs["store"]
    runtime_selected_identity = _impl._selected_store_identity(runtime_store)
    runtime_identity = _impl.require_exact_journal_store_authority(
        runtime.journal,
        subject="trusted chronology production runtime JournalStore",
    )
    # The native Windows opened-handle identity is authoritative, even when
    # the same journal has a different lexical path spelling. On POSIX the
    # existing canonical-path/device/inode match remains strict.
    if not _impl.same_journal_backing_object(runtime_identity, runtime_selected_identity):
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

    # Signed-evidence verification is an external callback boundary. Capture the
    # exact verifier/currentness/horizon callables and their code objects before
    # crossing it so callback-time module/global mutation cannot replace the
    # post-verification authority checks that make this cut current.
    current_verifier = _original_require_current_trusted_chronology_cut
    current_verifier_code = getattr(current_verifier, "__code__", None)
    post_verifier = _require_post_verification_currentness
    post_verifier_code = getattr(post_verifier, "__code__", None)
    horizon_verifier = _original_require_chronology_horizon
    horizon_verifier_code = getattr(horizon_verifier, "__code__", None)

    durable = current_verifier(**kwargs)

    if (
        _original_require_current_trusted_chronology_cut is not current_verifier
        or _require_post_verification_currentness is not post_verifier
        or _original_require_chronology_horizon is not horizon_verifier
        or (
            current_verifier_code is not None
            and getattr(current_verifier, "__code__", None)
            is not current_verifier_code
        )
        or (
            post_verifier_code is not None
            and getattr(post_verifier, "__code__", None)
            is not post_verifier_code
        )
        or (
            horizon_verifier_code is not None
            and getattr(horizon_verifier, "__code__", None)
            is not horizon_verifier_code
        )
    ):
        raise PermissionError(
            "trusted chronology verification authority changed during verification"
        )

    post_verifier(durable, kwargs=kwargs)

    if (
        _require_post_verification_currentness is not post_verifier
        or _original_require_chronology_horizon is not horizon_verifier
        or (
            post_verifier_code is not None
            and getattr(post_verifier, "__code__", None)
            is not post_verifier_code
        )
        or (
            horizon_verifier_code is not None
            and getattr(horizon_verifier, "__code__", None)
            is not horizon_verifier_code
        )
    ):
        raise PermissionError(
            "trusted chronology verification authority changed during verification"
        )

    horizon_verifier(durable, *claimed_instants)
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
