from __future__ import annotations

from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from autotrade_runtime.artifacts import ArtifactStore

from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp import provider_origin as provider_origin_module
from mvp.autotrade_mvp.provider_origin import ProviderOriginError, ProviderOriginJournal
from mvp.tests.test_provider_route_reads import ProviderRouteReadTests


class HostileTimezone(tzinfo):
    def __init__(self) -> None:
        self.calls: list[str] = []

    def utcoffset(self, _dt):
        self.calls.append("utcoffset")
        return timedelta(0)

    def dst(self, _dt):
        self.calls.append("dst")
        return timedelta(0)

    def tzname(self, _dt):
        self.calls.append("tzname")
        return "UTC"


class ProviderOriginTimezoneAuthorityTests(unittest.TestCase):
    def _qualified_binding(self, directory: str):
        fixture = ProviderRouteReadTests(
            methodName="test_prepared_read_binds_exact_current_q_c_and_rule_identity"
        )
        self.addCleanup(fixture.doCleanups)
        journal, capabilities, qualifications, route, _q1, _harness = (
            fixture.setup_route(directory)
        )
        binding = fixture.prepare(route, capabilities, qualifications)
        return journal, binding

    def test_utc_text_accepts_exact_stdlib_fixed_offsets(self) -> None:
        values = (
            datetime(2026, 10, 5, 20, 0, tzinfo=timezone.utc),
            datetime(
                2026,
                10,
                5,
                22,
                0,
                tzinfo=timezone(timedelta(hours=2)),
            ),
        )
        for value in values:
            with self.subTest(value=value):
                self.assertEqual(
                    provider_origin_module._utc_text(value, name="at"),
                    "2026-10-05T20:00:00Z",
                )

    def test_prepare_rejects_custom_timezone_before_callback_or_journal_mutation(self) -> None:
        with TemporaryDirectory() as directory:
            journal, binding = self._qualified_binding(directory)
            origin = ProviderOriginJournal(
                journal,
                response_store=ArtifactStore(
                    Path(directory) / "provider-origin-artifacts"
                ),
            )
            hostile = HostileTimezone()
            recorded_at = datetime(
                2026,
                10,
                4,
                20,
                0,
                0,
                tzinfo=hostile,
            )
            before = JournalStore.current_journal_sequence(journal)

            error = None
            try:
                origin.prepare(
                    binding,
                    transport_identity=(
                        "BybitV5AuthenticatedReadTransport:direct-v1"
                    ),
                    network_policy_identity="sha256:" + "1" * 64,
                    recorded_at=recorded_at,
                )
            except ProviderOriginError as caught:
                error = caught

            self.assertIsNotNone(
                error,
                "custom timezone must be rejected rather than normalized",
            )
            self.assertEqual(
                hostile.calls,
                [],
                "provider-origin admission must not execute caller tzinfo callbacks",
            )
            self.assertEqual(
                JournalStore.current_journal_sequence(journal),
                before,
                "rejected timestamp must not mutate provider-origin JournalStore",
            )


if __name__ == "__main__":
    unittest.main()
