from __future__ import annotations

from tempfile import TemporaryDirectory
from unittest.mock import patch
import unittest

from autotrade_runtime.artifacts import ArtifactStore

from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.provider_origin import _TEST_ONLY_PROVIDER_ORIGIN_RECORD_TOKEN
from mvp.tests.test_provider_origin import ProviderOriginJournalTests
from mvp.tests.test_provider_selection import NOW


class ProviderOriginStoreDispatchAuthorityTests(unittest.TestCase):
    def _fixture(self, directory: str):
        fixture = ProviderOriginJournalTests(
            methodName="test_exact_qualified_origin_survives_restart_without_requery"
        )
        self.addCleanup(fixture.doCleanups)
        journal, capabilities, qualifications, route, _q1, _harness, binding = (
            fixture._route_fixture(directory)
        )[1:]
        origin = fixture._origin(journal, directory)
        return fixture, journal, capabilities, qualifications, route, binding, origin

    def test_post_composition_journal_append_rebinding_cannot_replace_prepare_authority(self):
        with TemporaryDirectory() as directory:
            _fixture, journal, _capabilities, _qualifications, _route, binding, origin = (
                self._fixture(directory)
            )

            def hostile_append(*args, **kwargs):
                raise AssertionError("post-composition JournalStore.append_event rebinding was used")

            with patch.object(JournalStore, "append_event", hostile_append):
                attempt_id = origin.prepare(
                    binding,
                    transport_identity="test-injected-transport",
                    network_policy_identity="sha256:" + "1" * 64,
                    recorded_at=NOW,
                )

            self.assertEqual(
                len(JournalStore.load_events(
                    journal,
                    "qualified_authenticated_provider_read",
                    attempt_id,
                )),
                1,
            )

    def test_post_composition_journal_load_rebinding_cannot_replace_read_authority(self):
        with TemporaryDirectory() as directory:
            _fixture, _journal, _capabilities, _qualifications, _route, binding, origin = (
                self._fixture(directory)
            )
            attempt_id = origin.prepare(
                binding,
                transport_identity="test-injected-transport",
                network_policy_identity="sha256:" + "2" * 64,
                recorded_at=NOW,
            )
            expected = origin._record_provider_origin(
                attempt_id,
                binding,
                http_status=200,
                response_bytes=b'{"retCode":0,"result":{"list":[]}}',
                observed_at=NOW,
                _origin_token=_TEST_ONLY_PROVIDER_ORIGIN_RECORD_TOKEN,
            )

            def hostile_load(*args, **kwargs):
                raise AssertionError("post-composition JournalStore.load_events rebinding was used")

            with patch.object(JournalStore, "load_events", hostile_load):
                recovered = origin.load_response_binding(attempt_id, binding)

            self.assertEqual(recovered, expected)

    def test_post_composition_artifact_publish_rebinding_cannot_replace_retention_authority(self):
        with TemporaryDirectory() as directory:
            _fixture, _journal, _capabilities, _qualifications, _route, binding, origin = (
                self._fixture(directory)
            )
            attempt_id = origin.prepare(
                binding,
                transport_identity="test-injected-transport",
                network_policy_identity="sha256:" + "3" * 64,
                recorded_at=NOW,
            )

            def hostile_publish(*args, **kwargs):
                raise AssertionError("post-composition ArtifactStore.publish_bytes rebinding was used")

            with patch.object(ArtifactStore, "publish_bytes", hostile_publish):
                recorded = origin._record_provider_origin(
                    attempt_id,
                    binding,
                    http_status=200,
                    response_bytes=b'{"retCode":0,"result":{"list":[]}}',
                    observed_at=NOW,
                    _origin_token=_TEST_ONLY_PROVIDER_ORIGIN_RECORD_TOKEN,
                )

            self.assertEqual(recorded.http_status, 200)
            self.assertTrue(recorded.response_bytes)

    def test_post_composition_artifact_read_rebinding_cannot_replace_recovery_authority(self):
        with TemporaryDirectory() as directory:
            _fixture, _journal, _capabilities, _qualifications, _route, binding, origin = (
                self._fixture(directory)
            )
            attempt_id = origin.prepare(
                binding,
                transport_identity="test-injected-transport",
                network_policy_identity="sha256:" + "4" * 64,
                recorded_at=NOW,
            )
            expected = origin._record_provider_origin(
                attempt_id,
                binding,
                http_status=200,
                response_bytes=b'{"retCode":0,"result":{"list":[]}}',
                observed_at=NOW,
                _origin_token=_TEST_ONLY_PROVIDER_ORIGIN_RECORD_TOKEN,
            )

            def hostile_read(*args, **kwargs):
                raise AssertionError(
                    "post-composition ArtifactStore.read_authenticated_snapshot rebinding was used"
                )

            with patch.object(ArtifactStore, "read_authenticated_snapshot", hostile_read):
                recovered = origin.load_response_binding(attempt_id, binding)

            self.assertEqual(recovered, expected)


if __name__ == "__main__":
    unittest.main()
