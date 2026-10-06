from datetime import datetime, timezone
import unittest

from mvp.autotrade_mvp.bybit_v5 import coverage_evidence
from mvp.autotrade_mvp.provider_core import ProviderCoreError


class BybitAbsenceSemanticsAuthorityTests(unittest.TestCase):
    def _coverage(self, **overrides):
        values = dict(
            account_id="paper-1",
            environment="PAPER",
            surface="EXECUTIONS",
            coverage_start="2026-09-24T19:00:00Z",
            coverage_end="2026-09-24T21:00:00Z",
            pagination_complete=True,
            consistency_horizon_satisfied=True,
        )
        values.update(overrides)
        return coverage_evidence(**values)

    def test_caller_boolean_cannot_grant_provider_absence_authority(self):
        with self.assertRaisesRegex(
            ProviderCoreError,
            "qualification|authority|exclusion semantics",
        ):
            self._coverage(qualified_exclusion_semantics=True)

    def test_unqualified_coverage_remains_non_authoritative(self):
        coverage = self._coverage()
        self.assertFalse(coverage.provider_semantics_exclude_execution)
        self.assertFalse(
            coverage.proves_absence_for(
                datetime(2026, 9, 24, 20, tzinfo=timezone.utc)
            )
        )

    def test_false_compatibility_scalar_cannot_strengthen_coverage(self):
        coverage = self._coverage(qualified_exclusion_semantics=False)
        self.assertFalse(coverage.provider_semantics_exclude_execution)

    def test_non_boolean_compatibility_scalar_is_rejected(self):
        with self.assertRaisesRegex(ProviderCoreError, "must be boolean"):
            self._coverage(qualified_exclusion_semantics="qualified")

    def test_every_reconciliation_surface_remains_fail_closed_without_authority(self):
        for surface in (
            "OPEN_ORDERS",
            "ORDER_HISTORY",
            "EXECUTIONS",
            "ACTIVITIES",
        ):
            with self.subTest(surface=surface):
                coverage = self._coverage(surface=surface)
                self.assertFalse(coverage.provider_semantics_exclude_execution)
                self.assertFalse(
                    coverage.proves_absence_for(
                        datetime(2026, 9, 24, 20, tzinfo=timezone.utc)
                    )
                )


if __name__ == "__main__":
    unittest.main()
