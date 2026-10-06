from datetime import timedelta
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from autotrade_runtime.artifacts import ArtifactStore

from mvp.autotrade_mvp.bybit_v5 import (
    BYBIT_EXECUTION_PARSER_CONTRACT_DIGEST,
    BYBIT_EXECUTION_PARSER_IDENTITY,
    BYBIT_OPTION_DELIVERY_PARSER_CONTRACT_DIGEST,
    BYBIT_OPTION_DELIVERY_PARSER_IDENTITY,
)
from mvp.autotrade_mvp.durable_capabilities import DurableCapabilityRegistry
from mvp.autotrade_mvp.persistence import JournalStore, canonical_json
import mvp.autotrade_mvp.provider_core as provider_core_module
import mvp.autotrade_mvp.provider_route_reads as provider_route_reads_module

from mvp.autotrade_mvp.provider_core import (
    ProviderCoreError,
    Surface,
    observe_authenticated_json_response,
)
from mvp.autotrade_mvp.provider_route_reads import (
    ProviderRouteReadError,
    QualifiedProviderReadQueryBinding,
    QualifiedProviderResponseObservation,
    observe_qualified_provider_json_response,
    prepare_qualified_provider_read,
    qualified_read_parser_semantic_claim,
    qualified_read_route_semantic_claim,
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


def _qualification_with_route_semantics(semantics):
    raw = canonical_json(dict(sorted(semantics.items())))
    digest = "sha256:" + sha256(raw.encode("utf-8")).hexdigest()
    qualification_id = "provider-qualification:sha256:" + "d" * 64
    return SimpleNamespace(
        route_semantics_json=raw,
        identity=SimpleNamespace(
            route_semantics_digest=digest,
            content_digest=qualification_id,
        ),
        qualification_id=qualification_id,
    )


class ProviderRouteReadTests(unittest.TestCase):
    def setup_route(self, directory: str, *, include_read_rule=True):
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
        q1, receipt1, protocol1 = accepted_spot_q(
            ordinal=50,
            include_read_rule=include_read_rule,
        )
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
            permission_scope="ACCOUNT.READ",
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
            self.assertTrue(binding.endpoint_rule_digest.startswith("sha256:"))
            self.assertTrue(binding.qualified_route_rule_digest.startswith("sha256:"))
            self.assertEqual(binding.data_entitlement, "BALANCES")
            self.assertEqual(binding.accepted_success_statuses, (200,))
            self.assertEqual(binding.parser_identity, "BYBIT_ORDER_V5_JSON_V1")
            self.assertEqual(len(binding.query_digest), 71)

    def test_execution_read_requires_endpoint_specific_parser_q_claim(self):
        rule_key, rule_digest = qualified_read_route_semantic_claim(
            provider_id="BYBIT",
            endpoint="/v5/execution/list",
            surface=Surface.AUTHENTICATED_READ,
            permission_scope="ORDER.READ",
        )
        parser_key, parser_contract_digest = qualified_read_parser_semantic_claim(
            provider_id="BYBIT",
            endpoint="/v5/execution/list",
            surface=Surface.AUTHENTICATED_READ,
            permission_scope="ORDER.READ",
        )
        self.assertEqual(
            parser_contract_digest,
            BYBIT_EXECUTION_PARSER_CONTRACT_DIGEST,
        )
        self.assertTrue(parser_key.startswith("READ_PARSER:"))
        self.assertNotEqual(parser_key, rule_key)

        global_parser_only = _qualification_with_route_semantics({
            "PARSER_IDENTITY": "BYBIT_ORDER_V5_JSON_V1",
            rule_key: rule_digest,
        })
        with self.assertRaisesRegex(
            ProviderRouteReadError,
            "does not cover exact authenticated-read parser contract",
        ):
            provider_route_reads_module._qualified_read_rule(
                qualification=global_parser_only,
                provider_id="BYBIT",
                endpoint="/v5/execution/list",
                surface=Surface.AUTHENTICATED_READ,
                permission_scope="ORDER.READ",
            )

        stale_endpoint_parser = _qualification_with_route_semantics({
            "PARSER_IDENTITY": "BYBIT_ORDER_V5_JSON_V1",
            parser_key: "sha256:" + "0" * 64,
            rule_key: rule_digest,
        })
        with self.assertRaisesRegex(
            ProviderRouteReadError,
            "does not cover exact authenticated-read parser contract",
        ):
            provider_route_reads_module._qualified_read_rule(
                qualification=stale_endpoint_parser,
                provider_id="BYBIT",
                endpoint="/v5/execution/list",
                surface=Surface.AUTHENTICATED_READ,
                permission_scope="ORDER.READ",
            )

        exact = _qualification_with_route_semantics({
            "PARSER_IDENTITY": "BYBIT_ORDER_V5_JSON_V1",
            parser_key: parser_contract_digest,
            rule_key: rule_digest,
        })
        (
            _semantics_digest,
            returned_rule_digest,
            qualified_rule_digest,
            entitlement,
            success_statuses,
            returned_parser_identity,
        ) = provider_route_reads_module._qualified_read_rule(
            qualification=exact,
            provider_id="BYBIT",
            endpoint="/v5/execution/list",
            surface=Surface.AUTHENTICATED_READ,
            permission_scope="ORDER.READ",
        )
        self.assertEqual(returned_rule_digest, rule_digest)
        self.assertTrue(qualified_rule_digest.startswith("sha256:"))
        self.assertEqual(entitlement, "EXECUTIONS")
        self.assertEqual(success_statuses, (200,))
        self.assertEqual(
            returned_parser_identity,
            BYBIT_EXECUTION_PARSER_IDENTITY,
        )

    def test_delivery_read_requires_endpoint_specific_parser_q_claim(self):
        rule_key, rule_digest = qualified_read_route_semantic_claim(
            provider_id="BYBIT",
            endpoint="/v5/asset/delivery-record",
            surface=Surface.ACTIVITIES,
            permission_scope="ACCOUNT.READ",
        )
        parser_key, parser_contract_digest = qualified_read_parser_semantic_claim(
            provider_id="BYBIT",
            endpoint="/v5/asset/delivery-record",
            surface=Surface.ACTIVITIES,
            permission_scope="ACCOUNT.READ",
        )
        self.assertEqual(
            parser_contract_digest,
            BYBIT_OPTION_DELIVERY_PARSER_CONTRACT_DIGEST,
        )
        self.assertTrue(parser_key.startswith("READ_PARSER:"))
        self.assertNotEqual(parser_key, rule_key)

        global_parser_only = _qualification_with_route_semantics({
            "PARSER_IDENTITY": "BYBIT_ORDER_V5_JSON_V1",
            rule_key: rule_digest,
        })
        with self.assertRaisesRegex(
            ProviderRouteReadError,
            "does not cover exact authenticated-read parser contract",
        ):
            provider_route_reads_module._qualified_read_rule(
                qualification=global_parser_only,
                provider_id="BYBIT",
                endpoint="/v5/asset/delivery-record",
                surface=Surface.ACTIVITIES,
                permission_scope="ACCOUNT.READ",
            )

        stale_endpoint_parser = _qualification_with_route_semantics({
            "PARSER_IDENTITY": "BYBIT_ORDER_V5_JSON_V1",
            parser_key: (
                "sha256:"
                "b26269b85ed6ae54339092502a678ddaf8b046ce65cddb0e4553aceabd2a94e7"
            ),
            rule_key: rule_digest,
        })
        with self.assertRaisesRegex(
            ProviderRouteReadError,
            "does not cover exact authenticated-read parser contract",
        ):
            provider_route_reads_module._qualified_read_rule(
                qualification=stale_endpoint_parser,
                provider_id="BYBIT",
                endpoint="/v5/asset/delivery-record",
                surface=Surface.ACTIVITIES,
                permission_scope="ACCOUNT.READ",
            )

        exact = _qualification_with_route_semantics({
            "PARSER_IDENTITY": "BYBIT_ORDER_V5_JSON_V1",
            parser_key: parser_contract_digest,
            rule_key: rule_digest,
        })
        (
            _semantics_digest,
            returned_rule_digest,
            qualified_rule_digest,
            entitlement,
            success_statuses,
            returned_parser_identity,
        ) = provider_route_reads_module._qualified_read_rule(
            qualification=exact,
            provider_id="BYBIT",
            endpoint="/v5/asset/delivery-record",
            surface=Surface.ACTIVITIES,
            permission_scope="ACCOUNT.READ",
        )
        self.assertEqual(returned_rule_digest, rule_digest)
        self.assertTrue(qualified_rule_digest.startswith("sha256:"))
        self.assertEqual(entitlement, "ACTIVITIES")
        self.assertEqual(success_statuses, (200,))
        self.assertEqual(
            returned_parser_identity,
            BYBIT_OPTION_DELIVERY_PARSER_IDENTITY,
        )

    def test_parser_claim_helper_rejects_legacy_endpoint_without_source_parser(self):
        with self.assertRaisesRegex(
            ProviderRouteReadError,
            "no source-owned endpoint parser contract",
        ):
            qualified_read_parser_semantic_claim(
                provider_id="BYBIT",
                endpoint="/v5/account/wallet-balance",
                surface=Surface.AUTHENTICATED_READ,
                permission_scope="ACCOUNT.READ",
            )

    def test_qualified_read_and_response_constructors_are_sealed(self):
        with TemporaryDirectory() as directory:
            _journal, capabilities, qualifications, route, _q1, _harness = self.setup_route(directory)
            binding = self.prepare(route, capabilities, qualifications)
            with self.assertRaisesRegex(ProviderRouteReadError, "canonical route authority"):
                QualifiedProviderReadQueryBinding(
                    query_binding=binding.query_binding,
                    qualification_id=binding.qualification_id,
                    route_semantics_digest=binding.route_semantics_digest,
                    endpoint_rule_digest=binding.endpoint_rule_digest,
                    qualified_route_rule_digest=binding.qualified_route_rule_digest,
                    data_entitlement=binding.data_entitlement,
                    accepted_success_statuses=binding.accepted_success_statuses,
                    parser_identity=binding.parser_identity,
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
            self.assertEqual(observation.provider_environment, "TESTNET")
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

    def test_changed_q_parser_semantics_change_exact_qualified_read_identity(self):
        with TemporaryDirectory() as directory:
            _journal, capabilities, qualifications, route, q1, harness = self.setup_route(directory)
            first = self.prepare(route, capabilities, qualifications)

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

            selection = select_provider(
                route_request(),
                [candidate()],
                at=NOW,
                capability_registry=capabilities,
                qualification_registry=qualifications,
            )
            self.assertEqual(selection.status, "SELECTED_UNAMBIGUOUS")
            self.assertIsNotNone(selection.selected)
            second = self.prepare(
                selection.selected,
                capabilities,
                qualifications,
                at=NOW,
            )
            self.assertEqual(
                first.query_binding.query_digest,
                second.query_binding.query_digest,
            )
            self.assertNotEqual(first.qualification_id, second.qualification_id)
            self.assertNotEqual(
                first.route_semantics_digest,
                second.route_semantics_digest,
            )
            self.assertEqual(
                first.endpoint_rule_digest,
                second.endpoint_rule_digest,
            )
            self.assertNotEqual(
                first.qualified_route_rule_digest,
                second.qualified_route_rule_digest,
            )
            self.assertEqual(first.data_entitlement, second.data_entitlement)
            self.assertEqual(
                first.accepted_success_statuses,
                second.accepted_success_statuses,
            )
            self.assertNotEqual(first.parser_identity, second.parser_identity)

            raw = b'{"retCode":0,"result":{"equity":"10.25"}}'
            first_response = observe_qualified_provider_json_response(
                query_binding=first,
                http_status=200,
                response_bytes=raw,
                observed_at=NOW + timedelta(seconds=1),
            )
            second_response = observe_qualified_provider_json_response(
                query_binding=second,
                http_status=200,
                response_bytes=raw,
                observed_at=NOW + timedelta(seconds=1),
            )
            self.assertEqual(
                first_response.observation.evidence_ref,
                second_response.observation.evidence_ref,
            )
            self.assertNotEqual(
                first_response.evidence_ref,
                second_response.evidence_ref,
            )
            self.assertTrue(
                first_response.evidence_ref.startswith(
                    "qualified-provider-read:sha256:"
                )
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

    def test_q_without_exact_endpoint_rule_cannot_authorize_provider_read(self):
        with TemporaryDirectory() as directory:
            _journal, capabilities, qualifications, route, _q1, _harness = self.setup_route(
                directory,
                include_read_rule=False,
            )
            with self.assertRaisesRegex(
                ProviderRouteReadError,
                "does not cover exact authenticated-read endpoint rule",
            ):
                self.prepare(route, capabilities, qualifications)

    def test_wallet_q_claim_cannot_authorize_distinct_orders_entitlement(self):
        with TemporaryDirectory() as directory:
            _journal, capabilities, qualifications, route, _q1, _harness = self.setup_route(directory)
            with self.assertRaisesRegex(
                ProviderRouteReadError,
                "does not cover exact authenticated-read endpoint rule",
            ):
                prepare_qualified_provider_read(
                    route,
                    capabilities,
                    qualifications,
                    surface=Surface.AUTHENTICATED_READ,
                    endpoint="/v5/order/realtime",
                    query={},
                    at=NOW,
                    permission_scope="ORDER.READ",
                )

    def test_canonical_endpoint_policy_rejects_wrong_permission_before_binding(self):
        with TemporaryDirectory() as directory:
            _journal, capabilities, qualifications, route, _q1, _harness = self.setup_route(directory)
            with self.assertRaisesRegex(
                ProviderRouteReadError,
                "permission differs from canonical provider policy",
            ):
                prepare_qualified_provider_read(
                    route,
                    capabilities,
                    qualifications,
                    surface=Surface.AUTHENTICATED_READ,
                    endpoint="/v5/account/wallet-balance",
                    query={"accountType": "UNIFIED"},
                    at=NOW,
                    permission_scope="ORDER.READ",
                )

    def test_response_status_must_match_qualified_endpoint_contract(self):
        with TemporaryDirectory() as directory:
            _journal, capabilities, qualifications, route, _q1, _harness = self.setup_route(directory)
            binding = self.prepare(route, capabilities, qualifications)
            with self.assertRaisesRegex(
                ProviderRouteReadError,
                "outside qualified endpoint contract",
            ):
                observe_qualified_provider_json_response(
                    query_binding=binding,
                    http_status=201,
                    response_bytes=b'{"retCode":0}',
                    observed_at=NOW + timedelta(seconds=1),
                )

    def test_module_tokens_are_not_provider_read_minting_authority(self):
        self.assertFalse(hasattr(provider_core_module, "_PREPARED_READ_TOKEN"))
        self.assertFalse(hasattr(provider_core_module, "_OBSERVED_RESPONSE_TOKEN"))
        self.assertFalse(
            hasattr(
                provider_core_module,
                "_register_authenticated_read_query_binding_authority",
            )
        )
        self.assertFalse(
            hasattr(
                provider_core_module,
                "_register_provider_response_observation_authority",
            )
        )
        self.assertFalse(
            hasattr(provider_core_module, "_prepare_authenticated_read_query_impl")
        )
        self.assertFalse(
            hasattr(provider_core_module, "_observe_authenticated_json_response_impl")
        )
        import mvp.autotrade_mvp.provider_route_reads as read_module
        self.assertFalse(hasattr(read_module, "_QUERY_TOKEN"))
        self.assertFalse(hasattr(read_module, "_RESPONSE_TOKEN"))
        self.assertFalse(
            hasattr(
                read_module,
                "_register_qualified_provider_read_binding_authority",
            )
        )
        self.assertFalse(
            hasattr(
                read_module,
                "_register_qualified_provider_response_authority",
            )
        )
        self.assertFalse(
            hasattr(read_module, "_prepare_qualified_provider_read_impl")
        )
        self.assertFalse(
            hasattr(read_module, "_observe_qualified_provider_json_response_impl")
        )

    def test_object_new_forged_read_bindings_have_no_construction_authority(self):
        forged = object.__new__(QualifiedProviderReadQueryBinding)
        object.__setattr__(
            forged,
            "qualification_id",
            "provider-qualification:sha256:" + "0" * 64,
        )
        with self.assertRaisesRegex(
            ProviderRouteReadError,
            "construction authority is unavailable",
        ):
            _ = forged.query_digest

    def test_neutral_read_binding_mutation_cannot_relabel_exact_response(self):
        with TemporaryDirectory() as directory:
            _journal, capabilities, qualifications, route, _q1, _harness = self.setup_route(directory)
            qualified = self.prepare(route, capabilities, qualifications)
            neutral = qualified.query_binding
            object.__setattr__(neutral, "endpoint", "/v5/order/realtime")
            object.__setattr__(neutral, "permission_scope", "ORDER.READ")
            object.__setattr__(neutral, "query_digest", "sha256:" + "0" * 64)
            with self.assertRaisesRegex(ProviderCoreError, "binding changed after preparation"):
                observe_authenticated_json_response(
                    query_binding=neutral,
                    http_status=200,
                    response_bytes=b'{"retCode":0}',
                    observed_at=NOW + timedelta(seconds=1),
                )

    def test_qualified_read_mutation_cannot_relabel_q_or_widen_success_status(self):
        with TemporaryDirectory() as directory:
            _journal, capabilities, qualifications, route, _q1, _harness = self.setup_route(directory)
            binding = self.prepare(route, capabilities, qualifications)
            object.__setattr__(
                binding,
                "qualification_id",
                "provider-qualification:sha256:" + "0" * 64,
            )
            object.__setattr__(binding, "accepted_success_statuses", (201,))
            with self.assertRaisesRegex(
                ProviderRouteReadError,
                "binding changed after route authority preparation",
            ):
                observe_qualified_provider_json_response(
                    query_binding=binding,
                    http_status=201,
                    response_bytes=b'{"retCode":0}',
                    observed_at=NOW + timedelta(seconds=1),
                )

    def test_qualified_response_mutation_cannot_relabel_exact_observation(self):
        with TemporaryDirectory() as directory:
            _journal, capabilities, qualifications, route, _q1, _harness = self.setup_route(directory)
            binding = self.prepare(route, capabilities, qualifications)
            response = observe_qualified_provider_json_response(
                query_binding=binding,
                http_status=200,
                response_bytes=b'{"retCode":0,"result":{"equity":"10.25"}}',
                observed_at=NOW + timedelta(seconds=1),
            )
            object.__setattr__(
                response.observation,
                "evidence_ref",
                "provider-read:sha256:" + "0" * 64,
            )
            with self.assertRaisesRegex(
                ProviderCoreError,
                "provider response changed after exact-byte observation",
            ):
                _ = response.evidence_ref

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
