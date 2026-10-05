"""Public reconciliation authority facade.

The historical implementation is retained byte-for-byte in
``_reconciliation_legacy_impl`` so this safety cut does not rewrite unrelated
cash/position/borrow/activity reconciliation logic.  Public callers keep the
same API and value types, but caller-authored diagnostic coverage/snapshot
booleans are no longer permitted to release an UNKNOWN submission as
``PROVEN_ABSENT``.

Terminal negative resolution remains fail-closed until WP-20 can compose its
issuer-protected accepted account cut and semantic exclusion with WP-48 trusted
chronology.  Positive provider facts (executions/working orders) are unchanged.
"""
from __future__ import annotations

from dataclasses import replace as _replace
from functools import wraps as _wraps

from . import _reconciliation_legacy_impl as _legacy

# Re-export the existing public surface without duplicating 2k+ lines of
# unrelated reconciliation code.  Restore the original public module identity
# on values defined by the retained implementation so pickle/introspection
# compatibility is preserved across this safety migration.
for _name in dir(_legacy):
    if not _name.startswith("__"):
        _value = getattr(_legacy, _name)
        globals()[_name] = _value
        if getattr(_value, "__module__", None) == _legacy.__name__:
            try:
                _value.__module__ = __name__
            except (AttributeError, TypeError):
                pass

_LEGACY_RECONCILE_ACCOUNT = _legacy.reconcile_account
_ISSUER_REQUIRED_REASON = "issuer_protected_absence_authority_required"
_ISSUER_REQUIRED_MESSAGE = (
    "issuer-protected absence authority is required before UNKNOWN may be released"
)


@_wraps(_LEGACY_RECONCILE_ACCOUNT)
def reconcile_account(*args, **kwargs):
    """Run reconciliation while treating legacy negative evidence as diagnostic.

    The retained implementation may derive a legacy ``PROVEN_ABSENT`` from
    ``SnapshotConsistencyEvidence`` and ``CoverageSurfaceEvidence`` booleans.
    Those are public value objects and therefore cannot be terminal financial
    authority under WP-20/#697.  Any such legacy verdict is downgraded to
    ``UNKNOWN`` at the public boundary; provider-positive outcomes are preserved.
    """

    result = _LEGACY_RECONCILE_ACCOUNT(*args, **kwargs)
    if not any(
        resolution.outcome == "PROVEN_ABSENT"
        for resolution in result.submission_resolutions
    ):
        return result

    resolutions = tuple(
        (
            _replace(
                resolution,
                outcome="UNKNOWN",
                evidence_reason=_ISSUER_REQUIRED_REASON,
            )
            if resolution.outcome == "PROVEN_ABSENT"
            else resolution
        )
        for resolution in result.submission_resolutions
    )
    blocking = tuple(sorted(set(result.blocking_resources) | {"ACCOUNT"}))
    reasons = result.reasons
    if _ISSUER_REQUIRED_MESSAGE not in reasons:
        reasons = (*reasons, _ISSUER_REQUIRED_MESSAGE)
    return _replace(
        result,
        complete=False,
        submission_resolutions=resolutions,
        blocking_resources=blocking,
        reasons=reasons,
    )


# Once the public facade is imported, even an accidental later import of the
# internal retained module receives the fail-closed entrypoint.  The underscore
# module is implementation storage, not a supported financial-authority API.
_legacy.reconcile_account = reconcile_account
reconcile_account.__module__ = __name__
