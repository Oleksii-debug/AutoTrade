from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from autotrade_runtime.artifacts import ArtifactStore

from mvp.autotrade_mvp.durable_capabilities import DurableCapabilityRegistry
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.provider_core import Surface
from mvp.autotrade_mvp.provider_route_reads import (
    ProviderRouteReadError,
    QualifiedProviderReadQueryBinding,
    QualifiedProviderResponseObservation,
    observe_qualified_provider_json_response,
    prepare_qualified_provider_read,
)
from mvp.autotrade_mvp.provider_selection import select_provider
from mvp.tests.provider_qualification_test_support import (
    ExactQualificationProjectionHarness,
)
from mvp.tests.test_durable_capabilities import verified
from mvp.tests.test_provider_route_dispatch import successor_spot_q
from mvp.tests.test_provider_selection import (
    NOW,
    accepted_spot_q,
    candidate,
    request as route_request,
)


class ProviderRouteReadTests(unittest.TestCase):
    def setup_route(self, directory: str):
        journal = JournalStore(Path(directory) / "journal.sqlite3")
        capabilities = DurableCapabilityRegistry(journal)
        capabilities.add(
            verified(
                "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                NOW - timedelta(minutes=1),
                provider_id="BYBIT",
                provider_environment="TESTNET",
            )
        )
        evidence_root = Path(directory) / "evidence"
        harness = ExactQualificationProjectionHarness().start()
        self.addCleanup(harness.stop)
        qualifications = harness.registry(
            journal,
            evidence_store=ArtifactStore(evidence_root),
            evidence_root=evidence_root,
        )
        q1, receipt1, protocol1 = accepted_spot_q(ordinal=50)
        harness.register(
            protocol_key=protocol1.key,
            record=q1,
            receipt=receipt1,
        )
        qualifications._append_accepted(
            protocol_key=protocol1.key,
            record=q1,
            receipt=receipt1,
        )
        selection = select_provider(
            route_request(),
            [candidate()],
            at=NOW,
            capability_registry=capabilities,
            qualification_registry=qualifications,
        )
        self.assertEqual(selection.status, "SELECTED_UNAMBIGUOUS")
        self.assertIsNotNone(selection.selected)
        return journal, capabilities, qualifications, selection.selected, q1, harness

    def prepare(self, route, capabilities, qualifications, *, at=NOW):
        return prepare_qualified_provider_read(
            route,
            capabilities,
            qualifications,
            surface=Surface.AUTHENTICATED_READ,
            endpoint="/v5/account/wallet-balance",
            query={"accountType": "UNIFIED"},
            at=at,
            permission_scope="ORDER.READ",
        )

    def test_prepared_read_binds_exact_current_q_c_and_rule_identity(self):
        with TemporaryDirectory() as directory:
            _journal, capabilities, qualifications, route, q1, _harness = self.setup_route(directory)
            binding = self.prepare(route, capabilities, qualifications)
            self.assertEqual(binding.qualification_id, q1.qualification_id)
            self.assertEqual(
                binding.query_binding.capability_snapshot_id,
                route.capability_snapshot_id,
            )
            self.assertEqual(binding.provider_environment, "TESTNET")
            self.assertTrue(binding.route_semantics_digest.startswith("sha256:"))
            self.assertEqual(len(binding.query_digest), 71)

    def test_qualified_read_and_response_constructors_are_sealed(self):
        with TemporaryDirectory() as directory:
            _journal, capabilities, qualifications, route, _q1, _harness = self.setup_route(directory)
            binding = self.prepare(route, capabilities, qualifications)
            with self.assertRaisesRegex(ProviderRouteReadError, "canonical route authority"):
                QualifiedProviderReadQueryBinding(
                    query_binding=binding.query_binding,
                    qualification_id=binding.qualification_id,
                    route_semantics_digest=binding.route_semantics_digest,
                    authority_journal_sequence_cut=binding.authority_journal_sequence_cut,
                    provider_environment=binding.provider_environment,
                    adapter_code_sha=binding.adapter_code_sha,
                    packaged_artifact_digest=binding.packaged_artifact_digest,
                )
            observation = observe_qualified_provider_json_response(
                query_binding=binding,
                http_status=200,
                response_bytes=b'{"retCode":0}',
                observed_at=NOW + timedelta(seconds=1),
            )
            with self.assertRaisesRegex(ProviderRouteReadError, "exact observed bytes"):
                QualifiedProviderResponseObservation(
                    observation=observation.observation,
                    query_binding=binding,
                )

    def test_response_retains_q1_if_q2_supersedes_after_request_was_sent(self):
        with TemporaryDirectory() as directory:
            _journal, capabilities, qualifications, route, q1, harness = self.setup_route(directory)
            binding = self.prepare(route, capabilities, qualifications)
            q2, receipt2, protocol2 = successor_spot_q(
                old_qualification_id=q1.qualification_id,
                ordinal=51,
            )
            harness.register(
                protocol_key=protocol2.key,
                record=q2,
                receipt=receipt2,
            )
            qualifications._append_accepted(
                protocol_key=protocol2.key,
                record=q2,
                receipt=receipt2,
            )
            qualifications._append_supersession(
                old_id=q1.qualification_id,
                new_id=q2.qualification_id,
            )

            observation = observe_qualified_provider_json_response(
                query_binding=binding,
                http_status=200,
                response_bytes=b'{"retCode":0,"result":{"equity":"10.25"}}',
                observed_at=NOW + timedelta(seconds=1),
            )
            self.assertEqual(observation.qualification_id, q1.qualification_id)
            self.assertNotEqual(observation.qualification_id, q2.qualification_id)
            self.assertEqual(
                observation.route_semantics_digest,
                binding.route_semantics_digest,
            )

            with self.assertRaisesRegex(ProviderRouteReadError, "qualification is not exact current"):
                self.prepare(
                    route,
                    capabilities,
                    qualifications,
                    at=NOW + timedelta(seconds=2),
                )

    def test_new_current_capability_invalidates_old_selected_route_for_new_read(self):
        with TemporaryDirectory() as directory:
            _journal, capabilities, qualifications, route, _q1, _harness = self.setup_route(directory)
            capabilities.add(
                verified(
                    "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                    NOW + timedelta(minutes=1),
                    provider_id="BYBIT",
                    provider_environment="TESTNET",
                )
            )
            with self.assertRaisesRegex(ProviderRouteReadError, "capability was superseded"):
                self.prepare(
                    route,
                    capabilities,
                    qualifications,
                    at=NOW + timedelta(minutes=2),
                )

    def test_q_registry_and_capability_registry_must_share_exact_store_instance(self):
        with TemporaryDirectory() as directory:
            journal, capabilities, qualifications, route, _q1, harness = self.setup_route(directory)
            other = harness.registry(
                JournalStore(Path(directory) / "journal.sqlite3"),
                evidence_store=qualifications.evidence_store,
                evidence_root=qualifications.evidence_root,
            )
            self.assertIsNot(other.store, journal)
            with self.assertRaisesRegex(ProviderRouteReadError, "share one JournalStore"):
                self.prepare(route, capabilities, other)


if __name__ == "__main__":
    unittest.main()
