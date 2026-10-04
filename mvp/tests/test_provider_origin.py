from __future__ import annotations

from contextlib import contextmanager
from datetime import timedelta
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import unittest

from autotrade_runtime.artifacts import ArtifactStore

import mvp.autotrade_mvp.provider_origin as provider_origin_module
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.provider_core import Surface
from mvp.autotrade_mvp.provider_origin import (
    AuthenticatedReadResponseBinding,
    ProviderOriginError,
    ProviderOriginJournal,
    _TEST_ONLY_PROVIDER_ORIGIN_RECORD_TOKEN,
    _response_artifact_id,
    execute_direct_provider_origin_read,
    observe_provider_origin_json_response,
)
from mvp.autotrade_mvp.provider_transport import (
    BYBIT_V5_ENDPOINT_POLICIES,
    BybitV5AuthenticatedReadTransport,
    UrllibJsonWireClient,
)
from mvp.autotrade_mvp.provider_route_reads import prepare_qualified_provider_read
from mvp.autotrade_mvp.windows_secrets import PersistentCredentialHandle
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

    @staticmethod
    def _seed_direct_claim_artifact(
        origin: ProviderOriginJournal,
        journal: JournalStore,
        binding,
        body: bytes,
        *,
        request_marker: str,
        terminal_cut_delta: int = 0,
        canonical_prepared: bool = True,
    ):
        """Seed only the durable recovery state; this is not provider-wire proof."""

        if canonical_prepared:
            attempt_id = origin.prepare_direct(binding, recorded_at=NOW)
        else:
            attempt_id = origin.prepare(
                binding,
                transport_identity="InjectedAuthenticatedReadTransport:test-only",
                network_policy_identity="sha256:" + "9" * 64,
                recorded_at=NOW,
            )
        prepared = JournalStore.load_events(
            journal,
            "qualified_authenticated_provider_read",
            attempt_id,
        )[0]
        snapshot = provider_origin_module._qualified_query_snapshot(binding)
        observed_at = NOW.isoformat().replace("+00:00", "Z")
        response_sha256 = "sha256:" + sha256(body).hexdigest()
        wire_request_sha256 = "sha256:" + sha256(
            (attempt_id + "|" + request_marker).encode("utf-8")
        ).hexdigest()
        wire_request_semantics_sha256 = (
            provider_origin_module.qualified_authenticated_read_expected_wire_semantics_digest(
                binding.query_binding,
                provider_environment=binding.provider_environment,
            )
        )
        terminal_cut = prepared["journal_sequence"] + terminal_cut_delta
        artifact_id = _response_artifact_id(
            attempt_id=attempt_id,
            qualified_query_digest=snapshot["qualified_query_digest"],
            response_sha256=response_sha256,
        )
        metadata = {
            "evidence_kind": "QUALIFIED_PROVIDER_ORIGIN_RESPONSE",
            "attempt_id": attempt_id,
            "prepared_subject_digest": prepared["payload_hash"],
            "qualified_query_digest": snapshot["qualified_query_digest"],
            "qualification_id": snapshot["qualification_id"],
            "endpoint_rule_digest": snapshot["endpoint_rule_digest"],
            "qualified_route_rule_digest": snapshot["qualified_route_rule_digest"],
            "data_entitlement": snapshot["data_entitlement"],
            "parser_identity": snapshot["parser_identity"],
            "provider_environment": snapshot["provider_environment"],
            "execution_class": "DIRECT_PROVIDER_WIRE",
            "wire_request_sha256": wire_request_sha256,
            "wire_request_semantics_sha256": wire_request_semantics_sha256,
            "terminal_authority_journal_sequence_cut": terminal_cut,
            "terminal_authority_verified_at": observed_at,
        }
        ArtifactStore.publish_bytes(
            origin._response_store,
            artifact_id=artifact_id,
            data=body,
            media_type="application/octet-stream",
            rights={
                "storage": True,
                "export": False,
                "rights_id": "qualified-provider-origin-response:v1",
            },
            source_refs=[],
            metadata=metadata,
        )
        provider_origin_module._claim_direct_wire_execution(
            journal,
            attempt_id=attempt_id,
            qualified_query_digest=snapshot["qualified_query_digest"],
            qualification_id=snapshot["qualification_id"],
            http_status=200,
            response_sha256=response_sha256,
            observed_at=observed_at,
            wire_request_sha256=wire_request_sha256,
            wire_request_semantics_sha256=wire_request_semantics_sha256,
            terminal_authority_journal_sequence_cut=terminal_cut,
            terminal_authority_verified_at=observed_at,
        )
        return attempt_id, artifact_id

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
            self.assertEqual(recovered.execution_class, "TEST_INJECTED")
            with self.assertRaisesRegex(
                ProviderOriginError,
                "DIRECT_PROVIDER_WIRE",
            ):
                observe_provider_origin_json_response(
                    response_binding=recovered,
                    query_binding=binding,
                )

    def test_execute_direct_origin_rejects_injected_wire_before_prepared(self):
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
            transport = object.__new__(BybitV5AuthenticatedReadTransport)
            transport.wire_client = object()

            with patch.object(
                origin,
                "prepare_direct",
                wraps=origin.prepare_direct,
            ) as prepare_direct:
                with self.assertRaisesRegex(
                    ProviderOriginError,
                    "canonical direct wire client",
                ):
                    execute_direct_provider_origin_read(
                        origin=origin,
                        route=route,
                        capability_registry=capabilities,
                        qualification_registry=qualifications,
                        query_binding=binding,
                        transport=transport,
                    )
                prepare_direct.assert_not_called()

    def test_execute_direct_origin_rejects_replaced_opener_before_prepared(self):
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
            client = UrllibJsonWireClient(max_response_bytes=1024)
            client._opener = object()
            transport = object.__new__(BybitV5AuthenticatedReadTransport)
            transport.wire_client = client

            with patch.object(
                origin,
                "prepare_direct",
                wraps=origin.prepare_direct,
            ) as prepare_direct:
                with self.assertRaisesRegex(
                    ProviderOriginError,
                    "direct network authority is unavailable",
                ):
                    execute_direct_provider_origin_read(
                        origin=origin,
                        route=route,
                        capability_registry=capabilities,
                        qualification_registry=qualifications,
                        query_binding=binding,
                        transport=transport,
                    )
                prepare_direct.assert_not_called()

    def test_provider_origin_causal_chronology_rejects_terminal_time_before_prepared(self):
        with self.assertRaisesRegex(
            ProviderOriginError,
            "causal chronology",
        ):
            provider_origin_module._require_provider_origin_causal_chronology(
                prepared_at=NOW.isoformat().replace("+00:00", "Z"),
                terminal_verified_at=(NOW - timedelta(seconds=1)).isoformat().replace(
                    "+00:00", "Z"
                ),
                observed_at=NOW.isoformat().replace("+00:00", "Z"),
            )

    def test_provider_origin_causal_chronology_rejects_terminal_time_after_observation(self):
        with self.assertRaisesRegex(
            ProviderOriginError,
            "causal chronology",
        ):
            provider_origin_module._require_provider_origin_causal_chronology(
                prepared_at=NOW.isoformat().replace("+00:00", "Z"),
                terminal_verified_at=(NOW + timedelta(seconds=2)).isoformat().replace(
                    "+00:00", "Z"
                ),
                observed_at=(NOW + timedelta(seconds=1)).isoformat().replace(
                    "+00:00", "Z"
                ),
            )

    def test_provider_origin_causal_chronology_accepts_equal_boundary_times(self):
        point = NOW.isoformat().replace("+00:00", "Z")
        provider_origin_module._require_provider_origin_causal_chronology(
            prepared_at=point,
            terminal_verified_at=point,
            observed_at=point,
        )

    def test_direct_origin_rejects_wrong_qualification_registry_type_before_prepare(self):
        with TemporaryDirectory() as directory:
            (
                _fixture,
                journal,
                capabilities,
                _qualifications,
                route,
                _q1,
                _harness,
                binding,
            ) = self._route_fixture(directory)
            origin = self._origin(journal, directory)
            transport = object.__new__(BybitV5AuthenticatedReadTransport)
            transport.wire_client = object()
            with patch.object(
                origin,
                "prepare_direct",
                wraps=origin.prepare_direct,
            ) as prepare_direct:
                with self.assertRaisesRegex(
                    TypeError,
                    "qualification_registry must be exact DurableProviderQualificationRegistry",
                ):
                    execute_direct_provider_origin_read(
                        origin=origin,
                        route=route,
                        capability_registry=capabilities,
                        qualification_registry=object(),
                        query_binding=binding,
                        transport=transport,
                    )
                prepare_direct.assert_not_called()

    def test_direct_origin_rejects_c_q_store_split_before_prepare_or_transport(self):
        with TemporaryDirectory() as directory:
            (
                _fixture,
                journal,
                capabilities,
                _qualifications,
                route,
                _q1,
                harness,
                binding,
            ) = self._route_fixture(directory)
            origin = self._origin(journal, directory)
            foreign_store = JournalStore(Path(directory) / "foreign-q-journal.sqlite3")
            foreign_evidence = Path(directory) / "foreign-q-evidence"
            foreign_qualifications = harness.registry(
                foreign_store,
                evidence_store=ArtifactStore(foreign_evidence),
                evidence_root=foreign_evidence,
            )
            transport = object.__new__(BybitV5AuthenticatedReadTransport)
            transport.wire_client = object()

            with patch.object(
                origin,
                "prepare_direct",
                wraps=origin.prepare_direct,
            ) as prepare_direct, patch.object(
                BybitV5AuthenticatedReadTransport,
                "__call__",
                autospec=True,
            ) as transport_call:
                with self.assertRaisesRegex(
                    ProviderOriginError,
                    "C/Q authorities must share one JournalStore",
                ):
                    execute_direct_provider_origin_read(
                        origin=origin,
                        route=route,
                        capability_registry=capabilities,
                        qualification_registry=foreign_qualifications,
                        query_binding=binding,
                        transport=transport,
                    )
                prepare_direct.assert_not_called()
                transport_call.assert_not_called()
            self.assertEqual(
                foreign_store.whole_store_state_cut()["journal_sequence"],
                0,
            )

    def test_direct_origin_rejects_cross_store_authority_before_prepare_or_transport(self):
        with TemporaryDirectory() as directory:
            (
                _fixture,
                _journal,
                capabilities,
                qualifications,
                route,
                _q1,
                _harness,
                binding,
            ) = self._route_fixture(directory)
            foreign_store = JournalStore(Path(directory) / "foreign-journal.sqlite3")
            origin = self._origin(foreign_store, directory)
            transport = object.__new__(BybitV5AuthenticatedReadTransport)
            transport.wire_client = object()

            with patch.object(
                origin,
                "prepare_direct",
                wraps=origin.prepare_direct,
            ) as prepare_direct, patch.object(
                BybitV5AuthenticatedReadTransport,
                "__call__",
                autospec=True,
            ) as transport_call:
                with self.assertRaisesRegex(
                    ProviderOriginError,
                    "share exact C/Q JournalStore",
                ):
                    execute_direct_provider_origin_read(
                        origin=origin,
                        route=route,
                        capability_registry=capabilities,
                        qualification_registry=qualifications,
                        query_binding=binding,
                        transport=transport,
                    )
                prepare_direct.assert_not_called()
                transport_call.assert_not_called()
            self.assertEqual(
                foreign_store.whole_store_state_cut()["journal_sequence"],
                0,
            )

    def test_instance_shadowed_opener_cannot_be_promoted_to_direct_provider_origin(self):
        class Resolver:
            @contextmanager
            def lease_for_execution(self, *_args, **_kwargs):
                yield (
                    '{"api_key":"SYNTHETIC-KEY",'
                    '"api_secret":"SYNTHETIC-SECRET"}'
                )

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

            class Stream(BytesIO):
                status = 200

            client = UrllibJsonWireClient(max_response_bytes=1024)
            client._opener.open = lambda *_args, **_kwargs: Stream(b'{"retCode":0}')
            base = binding.query_binding
            transport = BybitV5AuthenticatedReadTransport(
                policy=BYBIT_V5_ENDPOINT_POLICIES["TESTNET"],
                provider_environment="TESTNET",
                account_id=base.account_id,
                capability_snapshot_id=base.capability_snapshot_id,
                capability_registry=capabilities,
                secret_resolver=Resolver(),
                credential_handle=PersistentCredentialHandle(
                    handle_id="provider-origin-bybit-read",
                    account_id=base.account_id,
                    provider="BYBIT",
                    environment=base.environment,
                    provider_environment="TESTNET",
                    purpose="READ",
                    generation=1,
                ),
                session_token="provider-origin-session",
                origin="https://localhost",
                execution_identity="provider-origin-test-host",
                clock_millis=lambda: 1_700_000_000_000,
                clock_utc=lambda: NOW,
                wire_client=client,
            )
            with patch.object(
                origin,
                "prepare_direct",
                wraps=origin.prepare_direct,
            ) as prepare_direct:
                with self.assertRaisesRegex(
                    ProviderOriginError,
                    "direct network authority is unavailable",
                ):
                    execute_direct_provider_origin_read(
                        origin=origin,
                        route=route,
                        capability_registry=capabilities,
                        qualification_registry=qualifications,
                        query_binding=binding,
                        transport=transport,
                    )
                prepare_direct.assert_not_called()

    def test_prepared_claim_artifact_recovery_is_zero_requery_state_machine_only(self):
        """Synthetic durable state tests recovery only; it is not provider execution evidence."""

        with TemporaryDirectory() as directory:
            (
                _fixture,
                journal,
                _capabilities,
                _qualifications,
                _route,
                _q1,
                _harness,
                binding,
            ) = self._route_fixture(directory)
            origin = self._origin(journal, directory)
            body = b'{"retCode":0,"result":{"list":[{"coin":"USDT","equity":"10.25"}]}}'
            attempt_id, _artifact_id = self._seed_direct_claim_artifact(
                origin,
                journal,
                binding,
                body,
                request_marker="zero-requery-state-machine",
            )

            restarted = self._origin(JournalStore(journal.path), directory)
            recovered = restarted.recover_response_binding(attempt_id, binding)
            self.assertEqual(recovered.response_bytes, body)
            self.assertEqual(
                [
                    event["event_type"]
                    for event in JournalStore.load_events(
                        journal,
                        "qualified_authenticated_provider_read",
                        attempt_id,
                    )
                ],
                [
                    "AuthenticatedReadPrepared",
                    "AuthenticatedReadRetained",
                    "AuthenticatedReadObserved",
                ],
            )

    def test_prepared_claim_recovery_rejects_terminal_cut_before_prepared(self):
        """A durable claim cannot relabel a wire cut that predates Prepared."""

        with TemporaryDirectory() as directory:
            (
                _fixture,
                journal,
                _capabilities,
                _qualifications,
                _route,
                _q1,
                _harness,
                binding,
            ) = self._route_fixture(directory)
            origin = self._origin(journal, directory)
            body = b'{"retCode":0,"result":{"list":[]}}'
            attempt_id, _artifact_id = self._seed_direct_claim_artifact(
                origin,
                journal,
                binding,
                body,
                request_marker="terminal-before-prepared",
                terminal_cut_delta=-1,
            )

            restarted = self._origin(JournalStore(journal.path), directory)
            with self.assertRaisesRegex(
                ProviderOriginError,
                "predates durable Prepared",
            ):
                restarted.recover_response_binding(attempt_id, binding)
            self.assertEqual(
                [
                    event["event_type"]
                    for event in JournalStore.load_events(
                        journal,
                        "qualified_authenticated_provider_read",
                        attempt_id,
                    )
                ],
                ["AuthenticatedReadPrepared"],
            )

    def test_prepared_claim_recovery_rejects_noncanonical_prepared_network(self):
        with TemporaryDirectory() as directory:
            (
                _fixture,
                journal,
                _capabilities,
                _qualifications,
                _route,
                _q1,
                _harness,
                binding,
            ) = self._route_fixture(directory)
            origin = self._origin(journal, directory)
            body = b'{"retCode":0,"result":{"list":[]}}'
            attempt_id, _artifact_id = self._seed_direct_claim_artifact(
                origin,
                journal,
                binding,
                body,
                request_marker="noncanonical-prepared-network",
                canonical_prepared=False,
            )

            restarted = self._origin(JournalStore(journal.path), directory)
            with self.assertRaisesRegex(
                ProviderOriginError,
                "canonical Prepared network authority",
            ):
                restarted.recover_response_binding(attempt_id, binding)
            self.assertEqual(
                [
                    event["event_type"]
                    for event in JournalStore.load_events(
                        journal,
                        "qualified_authenticated_provider_read",
                        attempt_id,
                    )
                ],
                ["AuthenticatedReadPrepared"],
            )

    def test_prepared_claim_missing_artifact_fails_closed_until_exact_bytes_restored(self):
        """Recovery durability is tested without pretending a patched socket was real wire."""

        with TemporaryDirectory() as directory:
            (
                _fixture,
                journal,
                _capabilities,
                _qualifications,
                _route,
                _q1,
                _harness,
                binding,
            ) = self._route_fixture(directory)
            origin = self._origin(journal, directory)
            body = b'{"retCode":0,"result":{"list":[{"coin":"USDT","equity":"11.50"}]}}'
            attempt_id, artifact_id = self._seed_direct_claim_artifact(
                origin,
                journal,
                binding,
                body,
                request_marker="missing-artifact-recovery",
            )

            response_store = origin._response_store
            manifest = response_store.load_manifest(artifact_id)
            object_path = response_store._object_path(
                manifest["sha256"].removeprefix("sha256:")
            )
            exact_bytes = object_path.read_bytes()
            object_path.unlink()

            missing_artifact_restart = self._origin(
                JournalStore(journal.path),
                directory,
            )
            with self.assertRaisesRegex(
                ProviderOriginError,
                "recovery provider response artifact is unavailable",
            ):
                missing_artifact_restart.recover_response_binding(
                    attempt_id,
                    binding,
                )
            self.assertEqual(
                [
                    event["event_type"]
                    for event in JournalStore.load_events(
                        journal,
                        "qualified_authenticated_provider_read",
                        attempt_id,
                    )
                ],
                ["AuthenticatedReadPrepared"],
            )

            object_path.write_bytes(exact_bytes)
            restarted = self._origin(JournalStore(journal.path), directory)
            recovered = restarted.recover_response_binding(attempt_id, binding)
            self.assertEqual(recovered.response_bytes, body)
            self.assertEqual(
                [
                    event["event_type"]
                    for event in JournalStore.load_events(
                        journal,
                        "qualified_authenticated_provider_read",
                        attempt_id,
                    )
                ],
                [
                    "AuthenticatedReadPrepared",
                    "AuthenticatedReadRetained",
                    "AuthenticatedReadObserved",
                ],
            )

    def test_imported_observation_token_cannot_promote_test_injected_origin(self):
        with TemporaryDirectory() as directory:
            _fixture, journal, *_rest, binding = self._route_fixture(directory)
            origin = self._origin(journal, directory)
            attempt_id = origin.prepare(
                binding,
                transport_identity="BybitV5AuthenticatedReadTransport:direct-v1",
                network_policy_identity="sha256:" + "8" * 64,
                recorded_at=NOW,
            )
            recorded = self._record(origin, attempt_id, binding)
            qualified = provider_origin_module.observe_qualified_provider_json_response(
                query_binding=binding,
                http_status=recorded.http_status,
                response_bytes=recorded.response_bytes,
                observed_at=NOW,
            )
            with self.assertRaisesRegex(
                ProviderOriginError,
                "DIRECT_PROVIDER_WIRE",
            ):
                provider_origin_module.ProviderOriginObservation(
                    response_binding=recorded,
                    qualified_observation=qualified,
                    _observation_token=provider_origin_module._OBSERVATION_TOKEN,
                )

    def test_direct_wire_claim_is_single_use_and_restart_verifiable(self):
        with TemporaryDirectory() as directory:
            journal = JournalStore(Path(directory) / "journal.sqlite3")
            kwargs = {
                "attempt_id": "provider-read:" + "a" * 32,
                "qualified_query_digest": "sha256:" + "1" * 64,
                "qualification_id": "provider-qualification:sha256:" + "2" * 64,
                "response_sha256": "sha256:" + "3" * 64,
                "observed_at": NOW.isoformat().replace("+00:00", "Z"),
                "wire_request_sha256": "sha256:" + "4" * 64,
                "wire_request_semantics_sha256": "sha256:" + "5" * 64,
                "terminal_authority_journal_sequence_cut": 0,
                "terminal_authority_verified_at": NOW.isoformat().replace("+00:00", "Z"),
            }
            provider_origin_module._claim_direct_wire_execution(journal, **kwargs)
            provider_origin_module._require_direct_wire_execution_claim(journal, **kwargs)
            restarted = JournalStore(journal.path)
            provider_origin_module._require_direct_wire_execution_claim(restarted, **kwargs)
            conflicting = dict(kwargs)
            conflicting["attempt_id"] = "provider-read:" + "b" * 32
            with self.assertRaisesRegex(
                ProviderOriginError,
                "already claimed by another attempt",
            ):
                provider_origin_module._claim_direct_wire_execution(
                    restarted,
                    **conflicting,
                )

    def test_direct_wire_claim_rejects_future_terminal_cut_before_append(self):
        with TemporaryDirectory() as directory:
            journal = JournalStore(Path(directory) / "journal.sqlite3")
            kwargs = {
                "attempt_id": "provider-read:" + "c" * 32,
                "qualified_query_digest": "sha256:" + "1" * 64,
                "qualification_id": "provider-qualification:sha256:" + "2" * 64,
                "http_status": 200,
                "response_sha256": "sha256:" + "3" * 64,
                "observed_at": NOW.isoformat().replace("+00:00", "Z"),
                "wire_request_sha256": "sha256:" + "4" * 64,
                "wire_request_semantics_sha256": "sha256:" + "5" * 64,
                "terminal_authority_journal_sequence_cut": 1,
                "terminal_authority_verified_at": NOW.isoformat().replace("+00:00", "Z"),
            }
            with self.assertRaisesRegex(
                ProviderOriginError,
                "ahead of durable journal",
            ):
                provider_origin_module._claim_direct_wire_execution(
                    journal,
                    **kwargs,
                )
            self.assertEqual(
                JournalStore.load_events_by_aggregate_type(
                    journal,
                    "qualified_authenticated_provider_wire_execution",
                ),
                [],
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

    def test_imported_module_token_cannot_mint_direct_provider_origin(self):
        with TemporaryDirectory() as directory:
            _fixture, _journal, *_rest, binding = self._route_fixture(directory)
            raw = b'{"retCode":0,"result":{"list":[]}}'
            base = binding.query_binding
            forged = AuthenticatedReadResponseBinding(
                attempt_id="provider-read:" + "f" * 32,
                provider_id=base.provider_id,
                account_id=base.account_id,
                environment=base.environment,
                provider_environment=binding.provider_environment,
                capability_snapshot_id=base.capability_snapshot_id,
                qualification_id=binding.qualification_id,
                endpoint=base.endpoint,
                qualified_query_digest=binding.query_digest,
                endpoint_rule_digest=binding.endpoint_rule_digest,
                qualified_route_rule_digest=binding.qualified_route_rule_digest,
                data_entitlement=binding.data_entitlement,
                parser_identity=binding.parser_identity,
                transport_identity=provider_origin_module.direct_authenticated_read_transport_identity(),
                network_policy_identity=provider_origin_module.direct_authenticated_read_network_policy_identity(),
                http_status=200,
                observed_at=NOW.isoformat().replace("+00:00", "Z"),
                response_sha256="sha256:" + sha256(raw).hexdigest(),
                response_artifact_id="00000000-0000-0000-0000-000000000000",
                response_bytes=raw,
                origin_ref="provider-origin:sha256:" + "0" * 64,
                journal_sequence=3,
                execution_class="DIRECT_PROVIDER_WIRE",
                wire_request_sha256="sha256:" + "1" * 64,
                wire_request_semantics_sha256="sha256:" + "2" * 64,
                terminal_authority_journal_sequence_cut=1,
                terminal_authority_verified_at=NOW.isoformat().replace("+00:00", "Z"),
                _binding_token=provider_origin_module._BINDING_TOKEN,
            )
            with self.assertRaisesRegex(
                ProviderOriginError,
                "construction authority is unavailable",
            ):
                observe_provider_origin_json_response(
                    response_binding=forged,
                    query_binding=binding,
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
                execution_class="TEST_INJECTED",
                wire_request_sha256="sha256:" + "1" * 64,
                wire_request_semantics_sha256="sha256:" + "2" * 64,
                terminal_authority_journal_sequence_cut=1,
                terminal_authority_verified_at="2026-10-04T08:00:00Z",
            )


if __name__ == "__main__":
    unittest.main()
