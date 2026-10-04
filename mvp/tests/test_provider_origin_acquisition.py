from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import mvp.autotrade_mvp.provider_origin as provider_origin_module
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.provider_account_acquisition import (
    DurableProviderAccountAcquisitionAuthority,
)
from mvp.autotrade_mvp.provider_domain import ProviderFinancialScope
from mvp.autotrade_mvp.provider_origin import (
    ProviderOriginError,
    ProviderOriginJournal,
    _TEST_ONLY_PROVIDER_ORIGIN_RECORD_TOKEN,
    require_provider_origin_response_binding_authority,
)
from mvp.tests.test_provider_origin import ProviderOriginJournalTests
from mvp.tests.test_provider_selection import NOW


class ProviderOriginAcquisitionBindingTests(unittest.TestCase):
    def _fixture(self, directory: str):
        origin_tests = ProviderOriginJournalTests(
            methodName="test_exact_qualified_origin_survives_restart_without_requery"
        )
        self.addCleanup(origin_tests.doCleanups)
        (
            _route_fixture,
            journal,
            _capabilities,
            _qualifications,
            _route,
            q1,
            _harness,
            binding,
        ) = origin_tests._route_fixture(directory)
        origin = ProviderOriginJournalTests._origin(journal, directory)
        authority = DurableProviderAccountAcquisitionAuthority(journal)
        acquisition = authority.issue_serialized(
            provider_scope=q1.scope.provider_scope,
            account_id=binding.query_binding.account_id,
            acquisition_request_id="provider-origin-account-read-1",
            committed_at=NOW,
        )
        return journal, origin, authority, acquisition, q1, binding

    def test_prepared_binds_exact_current_serialized_acquisition(self):
        with TemporaryDirectory() as directory:
            journal, origin, authority, acquisition, _q1, binding = self._fixture(directory)
            attempt_id = origin.prepare_direct(
                binding,
                recorded_at=NOW,
                account_acquisition_authority=authority,
                account_acquisition=acquisition,
            )
            prepared = JournalStore.load_events(
                journal, "qualified_authenticated_provider_read", attempt_id
            )[0]
            self.assertEqual(
                set(prepared["payload"]),
                {
                    "origin_kind",
                    "qualified_query",
                    "transport_identity",
                    "network_policy_identity",
                },
            )
            binding_events = JournalStore.load_events(
                journal,
                "provider_origin_account_acquisition_binding",
                attempt_id,
            )
            self.assertEqual(len(binding_events), 1)
            snapshot = binding_events[0]["payload"]["account_acquisition"]
            self.assertEqual(snapshot["acquisition_id"], acquisition.acquisition_id)
            self.assertEqual(snapshot["acquisition_generation"], acquisition.acquisition_generation)
            self.assertEqual(snapshot["acquisition_journal_sequence_cut"], acquisition.acquisition_journal_sequence_cut)
            self.assertEqual(snapshot["issued_journal_sequence"], acquisition.issued_journal_sequence)
            self.assertEqual(snapshot["provider_scope_digest"], acquisition.provider_scope.content_digest)

    def test_binding_survives_restart_and_is_part_of_origin_identity(self):
        with TemporaryDirectory() as directory:
            journal, origin, authority, acquisition, _q1, binding = self._fixture(directory)
            attempt_id = origin.prepare_direct(
                binding,
                recorded_at=NOW,
                account_acquisition_authority=authority,
                account_acquisition=acquisition,
            )
            recorded = origin._record_provider_origin(
                attempt_id,
                binding,
                http_status=200,
                response_bytes=b'{"retCode":0,"result":{"list":[]}}',
                observed_at=NOW,
                _origin_token=_TEST_ONLY_PROVIDER_ORIGIN_RECORD_TOKEN,
            )
            self.assertEqual(recorded.account_acquisition_id, acquisition.acquisition_id)
            restarted = ProviderOriginJournalTests._origin(JournalStore(journal.path), directory)
            recovered = restarted.load_response_binding(attempt_id, binding)
            self.assertEqual(recovered.account_acquisition_id, acquisition.acquisition_id)
            self.assertEqual(recovered.account_acquisition_scope_digest, acquisition.provider_scope.content_digest)
            self.assertEqual(recovered.origin_ref, recorded.origin_ref)

    def test_superseded_acquisition_cannot_prepare_new_origin(self):
        with TemporaryDirectory() as directory:
            journal, origin, authority, first, _q1, binding = self._fixture(directory)
            authority.issue_serialized(
                provider_scope=first.provider_scope,
                account_id=first.account_id,
                acquisition_request_id="provider-origin-account-read-2",
                committed_at=NOW,
            )
            before = journal.current_journal_sequence()
            with self.assertRaisesRegex(ProviderOriginError, "not exact current authority"):
                origin.prepare_direct(
                    binding,
                    recorded_at=NOW,
                    account_acquisition_authority=authority,
                    account_acquisition=first,
                )
            self.assertEqual(journal.current_journal_sequence(), before)

    def test_cross_store_acquisition_is_rejected_before_prepared(self):
        with TemporaryDirectory() as directory:
            journal, origin, _authority, acquisition, _q1, binding = self._fixture(directory)
            foreign = DurableProviderAccountAcquisitionAuthority(
                JournalStore(Path(directory) / "foreign.sqlite3")
            )
            before = journal.current_journal_sequence()
            with self.assertRaisesRegex(ProviderOriginError, "share one JournalStore"):
                origin.prepare_direct(
                    binding,
                    recorded_at=NOW,
                    account_acquisition_authority=foreign,
                    account_acquisition=acquisition,
                )
            self.assertEqual(journal.current_journal_sequence(), before)

    def test_provider_environment_mismatch_is_rejected_before_prepared(self):
        with TemporaryDirectory() as directory:
            journal, origin, authority, _acquisition, q1, binding = self._fixture(directory)
            wrong_scope = ProviderFinancialScope(
                provider_id=q1.scope.provider_scope.provider_id,
                runtime_environment=q1.scope.provider_scope.runtime_environment,
                provider_environment="DEMO",
                entity_policy_id=q1.scope.provider_scope.entity_policy_id,
            )
            wrong = authority.issue_serialized(
                provider_scope=wrong_scope,
                account_id=binding.query_binding.account_id,
                acquisition_request_id="wrong-provider-environment",
                committed_at=NOW,
            )
            before = journal.current_journal_sequence()
            with self.assertRaisesRegex(ProviderOriginError, "scope differs from qualified read"):
                origin.prepare_direct(
                    binding,
                    recorded_at=NOW,
                    account_acquisition_authority=authority,
                    account_acquisition=wrong,
                )
            self.assertEqual(journal.current_journal_sequence(), before)

    def test_partial_acquisition_inputs_fail_closed(self):
        with TemporaryDirectory() as directory:
            journal, origin, authority, acquisition, _q1, binding = self._fixture(directory)
            before = journal.current_journal_sequence()
            with self.assertRaisesRegex(ProviderOriginError, "exact SerializedProviderAccountAcquisition"):
                origin.prepare_direct(
                    binding, recorded_at=NOW,
                    account_acquisition_authority=authority, account_acquisition=None,
                )
            with self.assertRaisesRegex(ProviderOriginError, "exact DurableProviderAccountAcquisitionAuthority"):
                origin.prepare_direct(
                    binding, recorded_at=NOW,
                    account_acquisition_authority=None, account_acquisition=acquisition,
                )
            self.assertEqual(journal.current_journal_sequence(), before)

    def test_hidden_binding_authority_detects_post_load_acquisition_mutation(self):
        with TemporaryDirectory() as directory:
            _journal, origin, authority, acquisition, _q1, binding = self._fixture(directory)
            attempt_id = origin.prepare_direct(
                binding, recorded_at=NOW,
                account_acquisition_authority=authority, account_acquisition=acquisition,
            )
            response = origin._record_provider_origin(
                attempt_id, binding, http_status=200,
                response_bytes=b'{"retCode":0,"result":{"list":[]}}',
                observed_at=NOW, _origin_token=_TEST_ONLY_PROVIDER_ORIGIN_RECORD_TOKEN,
            )
            require_provider_origin_response_binding_authority(response)
            object.__setattr__(
                response, "account_acquisition_id",
                "provider-account-acquisition:sha256:" + "0" * 64,
            )
            with self.assertRaisesRegex(ProviderOriginError, "changed after durable journal load"):
                require_provider_origin_response_binding_authority(response)

    def test_cross_store_content_identical_acquisition_cannot_consume_origin(self):
        with TemporaryDirectory() as first_directory, TemporaryDirectory() as second_directory:
            (
                _first_journal,
                first_origin,
                first_authority,
                first_acquisition,
                _first_q,
                first_binding,
            ) = self._fixture(first_directory)
            (
                _second_journal,
                _second_origin,
                second_authority,
                second_acquisition,
                _second_q,
                _second_binding,
            ) = self._fixture(second_directory)
            self.assertEqual(
                first_acquisition.acquisition_id,
                second_acquisition.acquisition_id,
            )
            attempt_id = first_origin.prepare_direct(
                first_binding,
                recorded_at=NOW,
                account_acquisition_authority=first_authority,
                account_acquisition=first_acquisition,
            )
            response = first_origin._record_provider_origin(
                attempt_id,
                first_binding,
                http_status=200,
                response_bytes=b'{"retCode":0,"result":{"list":[]}}',
                observed_at=NOW,
                _origin_token=_TEST_ONLY_PROVIDER_ORIGIN_RECORD_TOKEN,
            )
            from mvp.autotrade_mvp.provider_origin import (
                require_current_provider_origin_account_acquisition,
            )
            with self.assertRaisesRegex(
                ProviderOriginError, "same exact JournalStore generation"
            ):
                require_current_provider_origin_account_acquisition(
                    response_binding=response,
                    account_acquisition_authority=second_authority,
                    account_acquisition=second_acquisition,
                )

    def test_current_acquisition_consumer_fence_accepts_exact_bound_response(self):
        with TemporaryDirectory() as directory:
            _journal, origin, authority, acquisition, _q1, binding = self._fixture(directory)
            attempt_id = origin.prepare_direct(
                binding, recorded_at=NOW,
                account_acquisition_authority=authority, account_acquisition=acquisition,
            )
            response = origin._record_provider_origin(
                attempt_id, binding, http_status=200,
                response_bytes=b'{"retCode":0,"result":{"list":[]}}',
                observed_at=NOW, _origin_token=_TEST_ONLY_PROVIDER_ORIGIN_RECORD_TOKEN,
            )
            from mvp.autotrade_mvp.provider_origin import (
                require_current_provider_origin_account_acquisition,
            )
            self.assertEqual(
                require_current_provider_origin_account_acquisition(
                    response_binding=response,
                    account_acquisition_authority=authority,
                    account_acquisition=acquisition,
                ),
                acquisition,
            )

    def test_supersession_invalidates_bound_response_at_consumer_fence(self):
        with TemporaryDirectory() as directory:
            _journal, origin, authority, acquisition, _q1, binding = self._fixture(directory)
            attempt_id = origin.prepare_direct(
                binding, recorded_at=NOW,
                account_acquisition_authority=authority, account_acquisition=acquisition,
            )
            response = origin._record_provider_origin(
                attempt_id, binding, http_status=200,
                response_bytes=b'{"retCode":0,"result":{"list":[]}}',
                observed_at=NOW, _origin_token=_TEST_ONLY_PROVIDER_ORIGIN_RECORD_TOKEN,
            )
            authority.issue_serialized(
                provider_scope=acquisition.provider_scope,
                account_id=acquisition.account_id,
                acquisition_request_id="provider-origin-account-read-superseding",
                committed_at=NOW,
            )
            from mvp.autotrade_mvp.provider_origin import (
                require_current_provider_origin_account_acquisition,
            )
            with self.assertRaisesRegex(ProviderOriginError, "no longer current"):
                require_current_provider_origin_account_acquisition(
                    response_binding=response,
                    account_acquisition_authority=authority,
                    account_acquisition=acquisition,
                )

    def test_unbound_response_cannot_cross_account_consumer_fence(self):
        with TemporaryDirectory() as directory:
            _journal, origin, authority, acquisition, _q1, binding = self._fixture(directory)
            attempt_id = origin.prepare_direct(binding, recorded_at=NOW)
            response = origin._record_provider_origin(
                attempt_id, binding, http_status=200,
                response_bytes=b'{"retCode":0,"result":{"list":[]}}',
                observed_at=NOW, _origin_token=_TEST_ONLY_PROVIDER_ORIGIN_RECORD_TOKEN,
            )
            from mvp.autotrade_mvp.provider_origin import (
                require_current_provider_origin_account_acquisition,
            )
            with self.assertRaisesRegex(ProviderOriginError, "not bound"):
                require_current_provider_origin_account_acquisition(
                    response_binding=response,
                    account_acquisition_authority=authority,
                    account_acquisition=acquisition,
                )

    def test_late_binding_cannot_upgrade_historical_unbound_origin(self):
        with TemporaryDirectory() as directory:
            journal, origin, authority, acquisition, _q1, binding = self._fixture(directory)
            attempt_id = origin.prepare_direct(binding, recorded_at=NOW)
            original = origin._record_provider_origin(
                attempt_id, binding, http_status=200,
                response_bytes=b'{"retCode":0,"result":{"list":[]}}',
                observed_at=NOW, _origin_token=_TEST_ONLY_PROVIDER_ORIGIN_RECORD_TOKEN,
            )
            self.assertIsNone(original.account_acquisition_id)
            provider_origin_module._append_provider_origin_account_acquisition_binding(
                journal,
                attempt_id=attempt_id,
                query_binding=binding,
                account_acquisition=provider_origin_module._account_acquisition_snapshot(
                    authority=authority,
                    acquisition=acquisition,
                    store=journal,
                    query_binding=binding,
                ),
                committed_at=NOW.isoformat().replace("+00:00", "Z"),
            )
            with self.assertRaisesRegex(
                ProviderOriginError, "must precede Prepared"
            ):
                origin.load_response_binding(attempt_id, binding)

    def test_generic_origin_remains_explicitly_unbound(self):
        with TemporaryDirectory() as directory:
            journal, origin, _authority, _acquisition, _q1, binding = self._fixture(directory)
            attempt_id = origin.prepare_direct(binding, recorded_at=NOW)
            prepared = JournalStore.load_events(
                journal, "qualified_authenticated_provider_read", attempt_id
            )[0]
            self.assertNotIn("account_acquisition", prepared["payload"])
            self.assertEqual(
                JournalStore.load_events(
                    journal,
                    "provider_origin_account_acquisition_binding",
                    attempt_id,
                ),
                [],
            )
            response = origin._record_provider_origin(
                attempt_id, binding, http_status=200,
                response_bytes=b'{"retCode":0,"result":{"list":[]}}',
                observed_at=NOW, _origin_token=_TEST_ONLY_PROVIDER_ORIGIN_RECORD_TOKEN,
            )
            self.assertIsNone(response.account_acquisition_id)
            self.assertIsNone(response.account_acquisition_scope_digest)


if __name__ == "__main__":
    unittest.main()
