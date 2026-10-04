from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import unittest

from autotrade_runtime.artifacts import ArtifactStore

from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.provider_core import Surface
from mvp.autotrade_mvp.provider_origin import (
    AuthenticatedReadResponseBinding,
    ProviderOriginError,
    ProviderOriginJournal,
    _TEST_ONLY_PROVIDER_ORIGIN_RECORD_TOKEN,
    observe_provider_origin_json_response,
)
from mvp.autotrade_mvp.provider_route_reads import prepare_qualified_provider_read
from mvp.tests.test_provider_route_reads import ProviderRouteReadTests
from mvp.tests.test_provider_selection import NOW


class ProviderOriginJournalTests(unittest.TestCase):
    def _route_fixture(self, directory: str):
        fixture = ProviderRouteReadTests(
            methodName="test_prepared_read_binds_exact_current_q_c_and_rule_identity"
        )
        self.addCleanup(fixture.doCleanups)
        journal, capabilities, qualifications, route, q1, harness = fixture.setup_route(
            directory
        )
        binding = fixture.prepare(route, capabilities, qualifications)
        return fixture, journal, capabilities, qualifications, route, q1, harness, binding

    @staticmethod
    def _origin(journal: JournalStore, directory: str) -> ProviderOriginJournal:
        return ProviderOriginJournal(
            journal,
            response_store=ArtifactStore(Path(directory) / "provider-origin-artifacts"),
        )

    @staticmethod
    def _record(origin: ProviderOriginJournal, attempt_id: str, binding, body=None):
        return origin._record_provider_origin(
            attempt_id,
            binding,
            http_status=200,
            response_bytes=body
            or b'{"retCode":0,"result":{"list":[{"coin":"USDT","equity":"10.25"}]}}',
            observed_at=NOW,
            _origin_token=_TEST_ONLY_PROVIDER_ORIGIN_RECORD_TOKEN,
        )

    def test_exact_qualified_origin_survives_restart_without_requery(self):
        with TemporaryDirectory() as directory:
            (
                _fixture,
                journal,
                _capabilities,
                _qualifications,
                _route,
                q1,
                _harness,
                binding,
            ) = self._route_fixture(directory)
            origin = self._origin(journal, directory)
            attempt_id = origin.prepare(
                binding,
                transport_identity="BybitV5AuthenticatedReadTransport:direct-v1",
                network_policy_identity="sha256:" + "1" * 64,
                recorded_at=NOW,
            )
            recorded = self._record(origin, attempt_id, binding)
            self.assertEqual(recorded.qualification_id, q1.qualification_id)
            self.assertEqual(recorded.provider_environment, "TESTNET")
            self.assertEqual(recorded.data_entitlement, "BALANCES")
            self.assertEqual(
                recorded.qualified_route_rule_digest,
                binding.qualified_route_rule_digest,
            )
            self.assertTrue(recorded.origin_ref.startswith("provider-origin:sha256:"))

            restarted = self._origin(JournalStore(journal.path), directory)
            recovered = restarted.load_response_binding(attempt_id, binding)
            self.assertEqual(recovered, recorded)
            observation = observe_provider_origin_json_response(
                origin_journal=restarted,
                attempt_id=attempt_id,
                query_binding=binding,
            )
            self.assertEqual(observation.origin_ref, recorded.origin_ref)
            self.assertNotEqual(
                observation.origin_ref,
                observation.qualified_evidence_ref,
            )
            self.assertEqual(
                observation.qualified_observation.qualification_id,
                q1.qualification_id,
            )
            self.assertEqual(
                observation.payload["result"]["list"][0]["equity"],
                "10.25",
            )

    def test_mutated_loaded_binding_cannot_relabel_durable_response_bytes(self):
        with TemporaryDirectory() as directory:
            _fixture, journal, *_rest, binding = self._route_fixture(directory)
            origin = self._origin(journal, directory)
            attempt_id = origin.prepare(
                binding,
                transport_identity="BybitV5AuthenticatedReadTransport:direct-v1",
                network_policy_identity="sha256:" + "a" * 64,
                recorded_at=NOW,
            )
            recorded = self._record(
                origin,
                attempt_id,
                binding,
                body=b'{"retCode":0,"result":{"equity":"10.25"}}',
            )
            caller_copy = origin.load_response_binding(attempt_id, binding)
            forged = b'{"retCode":0,"result":{"equity":"999999.99"}}'
            object.__setattr__(caller_copy, "response_bytes", forged)
            object.__setattr__(
                caller_copy,
                "response_sha256",
                "sha256:" + sha256(forged).hexdigest(),
            )

            observation = observe_provider_origin_json_response(
                origin_journal=origin,
                attempt_id=attempt_id,
                query_binding=binding,
            )
            self.assertEqual(observation.origin_ref, recorded.origin_ref)
            self.assertEqual(
                observation.response_binding.response_sha256,
                recorded.response_sha256,
            )
            self.assertEqual(observation.payload["result"]["equity"], "10.25")
            self.assertNotEqual(
                observation.response_binding.response_sha256,
                caller_copy.response_sha256,
            )

    def test_journal_never_embeds_provider_response_bytes(self):
        with TemporaryDirectory() as directory:
            _fixture, journal, *_rest, binding = self._route_fixture(directory)
            origin = self._origin(journal, directory)
            attempt_id = origin.prepare(
                binding,
                transport_identity="BybitV5AuthenticatedReadTransport:direct-v1",
                network_policy_identity="sha256:" + "2" * 64,
                recorded_at=NOW,
            )
            body = b'{"retCode":0,"result":{"marker":"NEVER_IN_JOURNAL"}}'
            self._record(origin, attempt_id, binding, body=body)
            events = JournalStore.load_events(
                journal,
                "qualified_authenticated_provider_read",
                attempt_id,
            )
            self.assertEqual(
                [event["event_type"] for event in events],
                [
                    "AuthenticatedReadPrepared",
                    "AuthenticatedReadRetained",
                    "AuthenticatedReadObserved",
                ],
            )
            rendered = repr(events)
            self.assertNotIn("NEVER_IN_JOURNAL", rendered)
            self.assertNotIn("response_base64", rendered)

    def test_prepared_only_state_cannot_become_provider_origin(self):
        with TemporaryDirectory() as directory:
            _fixture, journal, *_rest, binding = self._route_fixture(directory)
            origin = self._origin(journal, directory)
            attempt_id = origin.prepare(
                binding,
                transport_identity="BybitV5AuthenticatedReadTransport:direct-v1",
                network_policy_identity="sha256:" + "3" * 64,
                recorded_at=NOW,
            )
            with self.assertRaisesRegex(ProviderOriginError, "incomplete"):
                origin.load_response_binding(attempt_id, binding)
            with self.assertRaisesRegex(
                ProviderOriginError,
                "Prepared \\+ Retained",
            ):
                origin.recover_response_binding(attempt_id, binding)

    def test_private_record_seam_rejects_caller_without_transport_receipt(self):
        with TemporaryDirectory() as directory:
            _fixture, journal, *_rest, binding = self._route_fixture(directory)
            origin = self._origin(journal, directory)
            attempt_id = origin.prepare(
                binding,
                transport_identity="BybitV5AuthenticatedReadTransport:direct-v1",
                network_policy_identity="sha256:" + "4" * 64,
                recorded_at=NOW,
            )
            with self.assertRaisesRegex(
                ProviderOriginError,
                "canonical transport execution receipt",
            ):
                origin._record_provider_origin(
                    attempt_id,
                    binding,
                    http_status=200,
                    response_bytes=b'{"retCode":0}',
                    observed_at=NOW,
                    _origin_token=object(),
                )
            events = JournalStore.load_events(
                journal,
                "qualified_authenticated_provider_read",
                attempt_id,
            )
            self.assertEqual(len(events), 1)

    def test_status_outside_exact_qualified_rule_cannot_be_recorded(self):
        with TemporaryDirectory() as directory:
            _fixture, journal, *_rest, binding = self._route_fixture(directory)
            origin = self._origin(journal, directory)
            attempt_id = origin.prepare(
                binding,
                transport_identity="BybitV5AuthenticatedReadTransport:direct-v1",
                network_policy_identity="sha256:" + "5" * 64,
                recorded_at=NOW,
            )
            with self.assertRaisesRegex(
                ProviderOriginError,
                "outside qualified endpoint contract",
            ):
                origin._record_provider_origin(
                    attempt_id,
                    binding,
                    http_status=201,
                    response_bytes=b'{"retCode":0}',
                    observed_at=NOW,
                    _origin_token=_TEST_ONLY_PROVIDER_ORIGIN_RECORD_TOKEN,
                )

    def test_distinct_qualified_query_cannot_relabel_durable_origin(self):
        with TemporaryDirectory() as directory:
            (
                _fixture,
                journal,
                capabilities,
                qualifications,
                route,
                _q1,
                _harness,
                binding,
            ) = self._route_fixture(directory)
            origin = self._origin(journal, directory)
            attempt_id = origin.prepare(
                binding,
                transport_identity="BybitV5AuthenticatedReadTransport:direct-v1",
                network_policy_identity="sha256:" + "6" * 64,
                recorded_at=NOW,
            )
            self._record(origin, attempt_id, binding)
            other = prepare_qualified_provider_read(
                route,
                capabilities,
                qualifications,
                surface=Surface.AUTHENTICATED_READ,
                endpoint="/v5/account/wallet-balance",
                query={"accountType": "UNIFIED", "coin": "USDT"},
                at=NOW,
                permission_scope="ACCOUNT.READ",
            )
            self.assertNotEqual(binding.query_digest, other.query_digest)
            with self.assertRaisesRegex(
                ProviderOriginError,
                "does not match exact qualified binding",
            ):
                origin.load_response_binding(attempt_id, other)

    def test_missing_retained_artifact_fails_closed_after_restart(self):
        with TemporaryDirectory() as directory:
            _fixture, journal, *_rest, binding = self._route_fixture(directory)
            origin = self._origin(journal, directory)
            attempt_id = origin.prepare(
                binding,
                transport_identity="BybitV5AuthenticatedReadTransport:direct-v1",
                network_policy_identity="sha256:" + "7" * 64,
                recorded_at=NOW,
            )
            recorded = self._record(origin, attempt_id, binding)
            response_store = origin._response_store
            manifest = response_store.load_manifest(recorded.response_artifact_id)
            object_path = response_store._object_path(
                manifest["sha256"].removeprefix("sha256:")
            )
            object_path.unlink()
            restarted = self._origin(JournalStore(journal.path), directory)
            with self.assertRaisesRegex(
                ProviderOriginError,
                "artifact is unavailable",
            ):
                restarted.load_response_binding(attempt_id, binding)

    def test_retained_crash_state_recovers_observed_without_provider_call(self):
        with TemporaryDirectory() as directory:
            _fixture, journal, *_rest, binding = self._route_fixture(directory)
            origin = self._origin(journal, directory)
            attempt_id = origin.prepare(
                binding,
                transport_identity="BybitV5AuthenticatedReadTransport:direct-v1",
                network_policy_identity="sha256:" + "8" * 64,
                recorded_at=NOW,
            )
            original_append = JournalStore.append_event

            def fail_observed(store, envelope):
                if envelope.get("event_type") == "AuthenticatedReadObserved":
                    raise RuntimeError("simulated crash before Observed commit")
                return original_append(store, envelope)

            with patch.object(JournalStore, "append_event", new=fail_observed):
                with self.assertRaisesRegex(RuntimeError, "simulated crash"):
                    self._record(origin, attempt_id, binding)

            events = JournalStore.load_events(
                journal,
                "qualified_authenticated_provider_read",
                attempt_id,
            )
            self.assertEqual(
                [event["event_type"] for event in events],
                ["AuthenticatedReadPrepared", "AuthenticatedReadRetained"],
            )
            restarted = self._origin(JournalStore(journal.path), directory)
            recovered = restarted.recover_response_binding(attempt_id, binding)
            self.assertTrue(recovered.origin_ref.startswith("provider-origin:sha256:"))
            events = JournalStore.load_events(
                restarted._store,
                "qualified_authenticated_provider_read",
                attempt_id,
            )
            self.assertEqual(events[-1]["event_type"], "AuthenticatedReadObserved")

    def test_second_conflicting_response_cannot_replace_first(self):
        with TemporaryDirectory() as directory:
            _fixture, journal, *_rest, binding = self._route_fixture(directory)
            origin = self._origin(journal, directory)
            attempt_id = origin.prepare(
                binding,
                transport_identity="BybitV5AuthenticatedReadTransport:direct-v1",
                network_policy_identity="sha256:" + "9" * 64,
                recorded_at=NOW,
            )
            first = self._record(origin, attempt_id, binding, body=b'{"retCode":0,"x":1}')
            with self.assertRaisesRegex(
                ProviderOriginError,
                "one exact durable Prepared event",
            ):
                self._record(origin, attempt_id, binding, body=b'{"retCode":0,"x":2}')
            self.assertEqual(
                origin.load_response_binding(attempt_id, binding).response_sha256,
                first.response_sha256,
            )

    def test_response_binding_constructor_is_sealed(self):
        with self.assertRaisesRegex(
            ProviderOriginError,
            "must come from durable journal",
        ):
            AuthenticatedReadResponseBinding(
                attempt_id="provider-read:" + "a" * 32,
                provider_id="BYBIT",
                account_id="acct-1",
                environment="PAPER",
                provider_environment="TESTNET",
                capability_snapshot_id="cap",
                qualification_id="provider-qualification:sha256:" + "a" * 64,
                endpoint="/v5/account/wallet-balance",
                qualified_query_digest="sha256:" + "b" * 64,
                endpoint_rule_digest="sha256:" + "c" * 64,
                qualified_route_rule_digest="sha256:" + "d" * 64,
                data_entitlement="BALANCES",
                parser_identity="BYBIT_ORDER_V5_JSON_V1",
                transport_identity="direct",
                network_policy_identity="sha256:" + "e" * 64,
                http_status=200,
                observed_at="2026-10-04T08:00:00Z",
                response_sha256="sha256:" + "f" * 64,
                response_artifact_id="00000000-0000-0000-0000-000000000000",
                response_bytes=b"{}",
                origin_ref="provider-origin:sha256:" + "0" * 64,
                journal_sequence=3,
            )


if __name__ == "__main__":
    unittest.main()
