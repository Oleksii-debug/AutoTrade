"""Reconciliation regression suite with WP-20 negative-authority expectation.

The pre-existing suite is retained byte-for-byte in
``_test_reconciliation_legacy``.  Only the single test that previously treated
caller-authored coverage booleans as terminal PROVEN_ABSENT authority is
replaced here; all other unittest cases run unchanged.
"""
from __future__ import annotations

from mvp.tests import _test_reconciliation_legacy as _legacy_tests

for _name in dir(_legacy_tests):
    if not _name.startswith("__"):
        globals()[_name] = getattr(_legacy_tests, _name)


def _test_complete_window_requires_issuer_protected_absence_authority(self):
    unknown = UnknownSubmission.create(
        attempt_id="a1",
        intent_id="intent-unknown",
        provider_id="TEST_PROVIDER",
        account_id="test-account",
        environment="PAPER",
        client_order_id="missing-order",
        started_at="2026-09-24T18:00:00Z",
    )
    result = self.base(
        unknown_submissions=[unknown],
        searched_client_order_ids=["missing-order"],
        absence_coverage=absence_coverage(),
    )
    resolution = result.submission_resolutions[0]
    self.assertEqual(resolution.outcome, "UNKNOWN")
    self.assertEqual(
        resolution.evidence_reason,
        "issuer_protected_absence_authority_required",
    )
    self.assertFalse(result.complete)
    self.assertTrue(result.blocks_new_risk)
    self.assertIn("ACCOUNT", result.blocking_resources)
    self.assertIn(
        "issuer-protected absence authority is required before UNKNOWN may be released",
        result.reasons,
    )


# Replace only the obsolete unsafe expectation.  The rest of the original
# ReconciliationTests class remains the exact retained suite.
ReconciliationTests.test_complete_window_plus_explicit_lookup_can_prove_absence = (
    _test_complete_window_requires_issuer_protected_absence_authority
)
