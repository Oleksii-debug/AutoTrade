"""Fail-closed public authority facade for WP-48 trusted chronology cuts.

The implementation remains byte-for-byte in ``_trusted_chronology_cut_impl``.
This facade closes one composition hazard: a chronology horizon must never be
accepted from a caller-constructed ``TrustedChronologyCut`` without durable
current-cut revalidation in the same authority-bearing operation.
"""

from __future__ import annotations

import sys

from . import _trusted_chronology_cut_impl as _impl


_original_require_current_trusted_chronology_cut = (
    _impl.require_current_trusted_chronology_cut
)
_original_require_chronology_horizon = _impl.require_chronology_horizon


def _require_current_trusted_chronology_cut_with_horizon(
    *,
    claimed_instants: tuple[str, ...] = (),
    **kwargs: object,
):
    """Reverify durable authority, then evaluate its conservative UTC horizon."""

    if type(claimed_instants) is not tuple:
        raise TypeError("claimed_instants must be exact tuple")
    durable = _original_require_current_trusted_chronology_cut(**kwargs)
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
