from datetime import timedelta
from hashlib import sha256
from tempfile import TemporaryDirectory
import unittest

from autotrade_runtime.artifacts import ArtifactStore

import mvp.autotrade_mvp.provider_origin as provider_origin_module
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.provider_account_acquisition import (
    DurableProviderAccountAcquisitionAuthority,
)
from mvp.autotrade_mvp.provider_account_origin_set import (
    ProviderAccountOriginBindingSet,
    ProviderAccountOriginSetError,
    issue_provider_account_origin_set,
    require_provider_account_origin_set_authority,
)
from mvp.tests.test_provider_origin import ProviderOriginJournalTests
from mvp.tests.test_provider_selection import NOW


class ProviderAccountOriginSetTests(unittest.TestCase):
    def _fixture(self, directory: str):
        origin_tests = ProviderOriginJournalTests(
            methodName="test_direct_wire_claim_is_single_use_and_restart_verifiable"
        )
        self.addCleanup(origin_tests.doCleanups)
        (
            _route_fixture,
            journal,
            _capabilities,
            qualifications,
            _route,
            q1,
            _harness,
            binding,
        ) = origin_tests._route_fixture(directory)
        origin = ProviderOriginJournalTests._origin(journal, directory)
        acquisition_authority = DurableProviderAccountAcquisitionAuthority(journal)
        acquisition = acquisition_authority.issue_serialized(
            provider_scope=q1.scope.provider_scope,
            account_id=binding.query_binding.account_id,
            acquisition_request_id="origin-set-acquisition-1",
            committed_at=NOW,
        )
        return (
            origin_tests,
            journal,
            origin,
            qualifications,
            acquisition_authority,
            acquisition,
            binding,
        )

    def _direct_binding(self, fixture, directory: str, *, marker: str):
        (
            _origin_tests,
            journal,
            origin,
            _qualifications,
            acquisition_authority,
            acquisition,
            binding,
        ) = fixture
        body = b'{"retCode":0,"result":{"list":[]}}'
        attempt_id = origin.prepare_direct(
            binding,
            recorded_at=NOW,
            account_acquisition_authority=acquisition_authority,
            account_acquisition=acquisition,
        )
        prepared = JournalStore.load_events(
            journal, "qualified_authenticated_provider_read", attempt_id
        )[0]
        snapshot = provider_origin_module._qualified_query_snapshot(binding)
        observed_at = NOW.isoformat().replace("+00:00", "Z")
        response_sha256 = "sha256:" + sha256(body).hexdigest()
        wire_request_sha256 = "sha256:" + sha256(
            (attempt_id + "|" + marker).encode("utf-8")
        ).hexdigest()
        wire_semantics = (
            provider_origin_module.qualified_authenticated_read_expected_wire_semantics_digest(
                binding.query_binding,
                provider_environment=binding.provider_environment,
            )
        )
        terminal_cut = prepared["journal_sequence"]
        artifact_id = provider_origin_module._response_artifact_id(
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
            "wire_request_semantics_sha256": wire_semantics,
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
            wire_request_semantics_sha256=wire_semantics,
            terminal_authority_journal_sequence_cut=terminal_cut,
            terminal_authority_verified_at=observed_at,
        )
        return origin.recover_response_binding(attempt_id, binding)

    def test_constructor_is_sealed(self):
        with self.assertRaisesRegex(
            ProviderAccountOriginSetError, "canonical issuer"
        ):
            ProviderAccountOriginBindingSet()

    def test_issue_seals_direct_current_acquisition_and_current_q(self):
        with TemporaryDirectory() as directory:
            fixture = self._fixture(directory)
            (
                _tests,
                _journal,
                _origin,
                qualifications,
                acquisition_authority,
                acquisition,
                _binding,
            ) = fixture
            response = self._direct_binding(fixture, directory, marker="one")
            issued = issue_provider_account_origin_set(
                qualification_registry=qualifications,
                account_acquisition_authority=acquisition_authority,
                account_acquisition=acquisition,
                response_bindings=(response,),
                at=NOW,
            )
            self.assertEqual(issued.acquisition_id, acquisition.acquisition_id)
            self.assertEqual(
                issued.provider_scope_digest,
                acquisition.provider_scope.content_digest,
            )
            self.assertEqual(issued.qualification_id, response.qualification_id)
            self.assertEqual(issued.entries[0]["origin_ref"], response.origin_ref)
            self.assertTrue(issued.content_digest.startswith("sha256:"))
            require_provider_account_origin_set_authority(issued)

    def test_test_injected_origin_cannot_enter_set(self):
        with TemporaryDirectory() as directory:
            fixture = self._fixture(directory)
            (
                _tests,
                _journal,
                origin,
                qualifications,
                acquisition_authority,
                acquisition,
                binding,
            ) = fixture
            attempt_id = origin.prepare_direct(
                binding,
                recorded_at=NOW,
                account_acquisition_authority=acquisition_authority,
                account_acquisition=acquisition,
            )
            response = origin._record_provider_origin(
                attempt_id,
                binding,
                http_status=200,
                response_bytes=b'{"retCode":0,"result":{"list":[]}}',
                observed_at=NOW,
                _origin_token=provider_origin_module._TEST_ONLY_PROVIDER_ORIGIN_RECORD_TOKEN,
            )
            with self.assertRaisesRegex(
                ProviderAccountOriginSetError, "DIRECT_PROVIDER_WIRE"
            ):
                issue_provider_account_origin_set(
                    qualification_registry=qualifications,
                    account_acquisition_authority=acquisition_authority,
                    account_acquisition=acquisition,
                    response_bindings=(response,),
                    at=NOW,
                )

    def test_superseded_acquisition_invalidates_origin_set_issuance(self):
        with TemporaryDirectory() as directory:
            fixture = self._fixture(directory)
            (
                _tests,
                _journal,
                _origin,
                qualifications,
                acquisition_authority,
                acquisition,
                _binding,
            ) = fixture
            response = self._direct_binding(fixture, directory, marker="before-super")
            acquisition_authority.issue_serialized(
                provider_scope=acquisition.provider_scope,
                account_id=acquisition.account_id,
                acquisition_request_id="origin-set-acquisition-2",
                committed_at=NOW,
            )
            with self.assertRaisesRegex(
                ProviderAccountOriginSetError, "not exact current authority"
            ):
                issue_provider_account_origin_set(
                    qualification_registry=qualifications,
                    account_acquisition_authority=acquisition_authority,
                    account_acquisition=acquisition,
                    response_bindings=(response,),
                    at=NOW,
                )

    def test_duplicate_origin_is_rejected(self):
        with TemporaryDirectory() as directory:
            fixture = self._fixture(directory)
            (
                _tests, _journal, _origin, qualifications,
                acquisition_authority, acquisition, _binding,
            ) = fixture
            response = self._direct_binding(fixture, directory, marker="dup")
            with self.assertRaisesRegex(
                ProviderAccountOriginSetError, "duplicate origin_ref"
            ):
                issue_provider_account_origin_set(
                    qualification_registry=qualifications,
                    account_acquisition_authority=acquisition_authority,
                    account_acquisition=acquisition,
                    response_bindings=(response, response),
                    at=NOW,
                )

    def test_cross_store_q_is_rejected(self):
        with TemporaryDirectory() as directory:
            fixture = self._fixture(directory)
            (
                tests, _journal, _origin, _qualifications,
                acquisition_authority, acquisition, _binding,
            ) = fixture
            response = self._direct_binding(fixture, directory, marker="foreign-q")
            with TemporaryDirectory() as foreign_directory:
                foreign_q = tests._route_fixture(foreign_directory)[3]
                self.assertIsNot(
                    foreign_q.store, acquisition_authority.store
                )
                with self.assertRaisesRegex(
                    ProviderAccountOriginSetError, "share one JournalStore"
                ):
                    issue_provider_account_origin_set(
                        qualification_registry=foreign_q,
                        account_acquisition_authority=acquisition_authority,
                        account_acquisition=acquisition,
                        response_bindings=(response,),
                        at=NOW,
                    )
    def test_empty_and_list_inputs_fail_closed(self):
        with TemporaryDirectory() as directory:
            fixture = self._fixture(directory)
            (
                _tests, _journal, _origin, qualifications,
                acquisition_authority, acquisition, _binding,
            ) = fixture
            for value in ((), []):
                with self.assertRaisesRegex(
                    ProviderAccountOriginSetError, "non-empty exact tuple"
                ):
                    issue_provider_account_origin_set(
                        qualification_registry=qualifications,
                        account_acquisition_authority=acquisition_authority,
                        account_acquisition=acquisition,
                        response_bindings=value,
                        at=NOW,
                    )

    def test_post_issue_mutation_is_detected(self):
        with TemporaryDirectory() as directory:
            fixture = self._fixture(directory)
            (
                _tests, _journal, _origin, qualifications,
                acquisition_authority, acquisition, _binding,
            ) = fixture
            response = self._direct_binding(fixture, directory, marker="mutation")
            issued = issue_provider_account_origin_set(
                qualification_registry=qualifications,
                account_acquisition_authority=acquisition_authority,
                account_acquisition=acquisition,
                response_bindings=(response,),
                at=NOW,
            )
            object.__setattr__(
                issued, "account_id", issued.account_id + "-forged"
            )
            with self.assertRaisesRegex(
                ProviderAccountOriginSetError, "changed after canonical issuance"
            ):
                require_provider_account_origin_set_authority(issued)


if __name__ == "__main__":
    unittest.main()
