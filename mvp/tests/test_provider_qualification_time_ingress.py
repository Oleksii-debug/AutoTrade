from __future__ import annotations

from datetime import datetime, timedelta, timezone, tzinfo
import unittest

from mvp.autotrade_mvp.durable_provider_qualification import (
    ProviderQualificationError,
    _point,
)


class _ExecutableTimezone(tzinfo):
    def __init__(self) -> None:
        self.calls = 0

    def utcoffset(self, _dt):
        self.calls += 1
        raise AssertionError("caller timezone executed at provider-Q ingress")

    def dst(self, _dt):
        raise AssertionError("caller timezone dst executed")

    def tzname(self, _dt):
        raise AssertionError("caller timezone tzname executed")


class ProviderQualificationTimeIngressTests(unittest.TestCase):
    def test_exact_datetime_with_executable_timezone_is_rejected_without_callback(self):
        hostile = _ExecutableTimezone()
        value = datetime(2026, 10, 5, 18, 0, tzinfo=hostile)

        with self.assertRaises(ProviderQualificationError):
            _point(value, name="at")

        self.assertEqual(hostile.calls, 0)

    def test_exact_builtin_timezones_remain_supported_and_normalize_to_utc(self):
        values = (
            (datetime(2026, 10, 5, 16, 0, tzinfo=timezone.utc), 16),
            (
                datetime(
                    2026,
                    10,
                    5,
                    18,
                    0,
                    tzinfo=timezone(timedelta(hours=2)),
                ),
                16,
            ),
        )

        for value, expected_hour in values:
            with self.subTest(value=value):
                normalized = _point(value, name="at")
                self.assertIs(normalized.tzinfo, timezone.utc)
                self.assertEqual(normalized.hour, expected_hour)


if __name__ == "__main__":
    unittest.main()
