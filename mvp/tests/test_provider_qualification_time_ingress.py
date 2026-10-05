from __future__ import annotations

from datetime import datetime, timedelta, timezone, tzinfo
import unittest

from mvp.autotrade_mvp.durable_provider_qualification import (
    DurableProviderQualificationRegistry,
    ProviderQualificationError,
    _point,
)
from mvp.autotrade_mvp.provider_domain import ProviderFinancialScope
from mvp.autotrade_mvp.provider_qualification_current_scope import (
    ProviderQualificationCurrentScope,
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


def _scope() -> ProviderQualificationCurrentScope:
    return ProviderQualificationCurrentScope(
        provider_scope=ProviderFinancialScope(
            provider_id="BYBIT",
            runtime_environment="PAPER",
            provider_environment="TESTNET",
            entity_policy_id="POLICY",
        ),
        product_family="SPOT",
        adapter_source_git_sha="0" * 40,
        packaged_artifact_digest="sha256:" + "0" * 64,
        protocol_id="provider-q",
        protocol_version="1",
    )


class ProviderQualificationTimeIngressTests(unittest.TestCase):
    def test_exact_datetime_with_executable_timezone_is_rejected_without_callback(self):
        hostile = _ExecutableTimezone()
        value = datetime(2026, 10, 5, 18, 0, tzinfo=hostile)

        with self.assertRaises(ProviderQualificationError):
            _point(value, name="at")

        self.assertEqual(hostile.calls, 0)

    def test_public_current_rejects_executable_timezone_before_history_access(self):
        hostile = _ExecutableTimezone()
        value = datetime(2026, 10, 5, 18, 0, tzinfo=hostile)
        registry = object.__new__(DurableProviderQualificationRegistry)
        history_calls = 0

        def fail_history(*, journal_sequence_cut=None):
            nonlocal history_calls
            history_calls += 1
            raise AssertionError("durable history read before time ingress rejection")

        registry._history = fail_history

        with self.assertRaises(ProviderQualificationError):
            registry.current(scope=_scope(), at=value)

        self.assertEqual(hostile.calls, 0)
        self.assertEqual(history_calls, 0)

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
