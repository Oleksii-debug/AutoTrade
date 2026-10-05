from __future__ import annotations

from datetime import datetime, timedelta, timezone, tzinfo
import unittest

from mvp.autotrade_mvp import provider_account_accepted_cut as accepted_cut
from mvp.autotrade_mvp import provider_account_coverage_set as coverage_set
from mvp.autotrade_mvp import provider_account_currentness as currentness
from mvp.autotrade_mvp import provider_account_empty_exclusion as empty_exclusion


class _ExecutableTimezone(tzinfo):
    def __init__(self) -> None:
        self.calls = 0

    def utcoffset(self, _dt):
        self.calls += 1
        raise AssertionError("caller tzinfo executed before authority validation")

    def dst(self, _dt):
        raise AssertionError("caller tzinfo dst executed")

    def tzname(self, _dt):
        raise AssertionError("caller tzinfo tzname executed")


class ProviderAccountInertTimeIngressTests(unittest.TestCase):
    def test_wp20_currentness_issuers_reject_executable_nested_tzinfo_without_calling_it(self):
        boundaries = (
            (currentness._at, currentness.ProviderAccountCurrentnessError),
            (coverage_set._at, coverage_set.ProviderAccountCoverageSetError),
            (empty_exclusion._at, empty_exclusion.ProviderAccountEmptyExclusionError),
            (accepted_cut._at, accepted_cut.AcceptedProviderAccountCutError),
        )
        for validator, error_type in boundaries:
            with self.subTest(boundary=validator.__module__):
                hostile = _ExecutableTimezone()
                value = datetime(2026, 10, 5, 16, 0, tzinfo=hostile)
                with self.assertRaises(error_type):
                    validator(value)
                self.assertEqual(hostile.calls, 0)

    def test_exact_stdlib_fixed_offset_timezones_remain_accepted(self):
        values = (
            datetime(2026, 10, 5, 16, 0, tzinfo=timezone.utc),
            datetime(
                2026,
                10,
                5,
                18,
                0,
                tzinfo=timezone(timedelta(hours=2)),
            ),
        )
        for value in values:
            self.assertIs(currentness._at(value), value)
            self.assertIs(coverage_set._at(value), value)
            self.assertIs(empty_exclusion._at(value), value)
            self.assertIs(accepted_cut._at(value), value)


if __name__ == "__main__":
    unittest.main()
