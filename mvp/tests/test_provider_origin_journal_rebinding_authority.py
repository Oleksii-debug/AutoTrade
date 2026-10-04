from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from autotrade_runtime.artifacts import ArtifactStore

from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.provider_origin import ProviderOriginJournal
from mvp.tests.test_provider_route_reads import ProviderRouteReadTests
from mvp.tests.test_provider_selection import NOW


class ProviderOriginJournalRebindingAuthorityTests(unittest.TestCase):
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

    def test_public_journal_append_rebind_is_never_used_for_prepared_authority(self) -> None:
        with TemporaryDirectory() as directory:
            journal, binding = self._qualified_binding(directory)
            origin = ProviderOriginJournal(
                journal,
                response_store=ArtifactStore(
                    Path(directory) / "provider-origin-artifacts"
                ),
            )
            installed_append = JournalStore.append_event
            calls: list[str] = []

            def rebound_append(store, event, *args, **kwargs):
                calls.append(str(event.get("event_type")))
                return installed_append(store, event, *args, **kwargs)

            with patch.object(JournalStore, "append_event", new=rebound_append):
                attempt_id = origin.prepare(
                    binding,
                    transport_identity=(
                        "BybitV5AuthenticatedReadTransport:direct-v1"
                    ),
                    network_policy_identity="sha256:" + "1" * 64,
                    recorded_at=NOW,
                )

            self.assertEqual(
                calls,
                [],
                "provider-origin durability must not dispatch through a rebound public JournalStore method",
            )
            events = JournalStore.load_events(
                journal,
                "qualified_authenticated_provider_read",
                attempt_id,
            )
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["event_type"], "AuthenticatedReadPrepared")


if __name__ == "__main__":
    unittest.main()
