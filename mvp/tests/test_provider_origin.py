from datetime import timedelta
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.provider_origin import (
    AuthenticatedReadResponseBinding,
    ProviderOriginError,
    ProviderOriginJournal,
    _PROVIDER_ORIGIN_RECORD_TOKEN,
    observe_provider_origin_json_response,
)
from mvp.tests.test_provider_transport import (
    READ_NOW,
    authenticated_read_binding,
)


class ProviderOriginJournalTests(unittest.TestCase):
    def test_provider_origin_survives_restart_without_network_requery(self):
        query = authenticated_read_binding()
        body = b'{"balances":[{"asset":"USDT","free":"100.00"}]}'
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            journal = ProviderOriginJournal(JournalStore(path))
            attempt_id = journal.prepare(
                query,
                transport_identity="UrllibJsonWireClient:v1",
                network_policy_identity="sha256:" + "1" * 64,
                recorded_at=READ_NOW,
            )
            binding = journal._record_provider_origin(
                attempt_id,
                query,
                http_status=200,
                response_bytes=body,
                observed_at=READ_NOW + timedelta(seconds=1),
                _origin_token=_PROVIDER_ORIGIN_RECORD_TOKEN,
            )
            self.assertEqual(binding.response_bytes, body)
            self.assertEqual(binding.http_status, 200)
            self.assertEqual(binding.transport_identity, "UrllibJsonWireClient:v1")
            self.assertEqual(binding.network_policy_identity, "sha256:" + "1" * 64)
            self.assertTrue(binding.origin_ref.startswith("provider-origin:sha256:"))
            self.assertGreater(binding.journal_sequence, 0)

            restarted = ProviderOriginJournal(JournalStore(path))
            recovered = restarted.load_response_binding(attempt_id, query)
            self.assertEqual(recovered, binding)
            self.assertEqual(recovered.response_bytes, body)
            observation = observe_provider_origin_json_response(
                response_binding=recovered,
                query_binding=query,
                accepted_success_statuses=frozenset({200}),
            )
            self.assertEqual(observation.origin_ref, binding.origin_ref)
            self.assertEqual(observation.provider_id, "BINANCE")
            self.assertEqual(observation.account_id, "acct-1")
            self.assertEqual(observation.environment, "PAPER")
            self.assertEqual(observation.payload["balances"][0]["free"], "100.00")

    def test_caller_cannot_self_assert_provider_origin_token(self):
        query = authenticated_read_binding()
        with TemporaryDirectory() as directory:
            journal = ProviderOriginJournal(
                JournalStore(f"{directory}/journal.sqlite3")
            )
            attempt_id = journal.prepare(
                query,
                transport_identity="UrllibJsonWireClient:v1",
                network_policy_identity="sha256:" + "2" * 64,
                recorded_at=READ_NOW,
            )
            with self.assertRaisesRegex(
                ProviderOriginError, "may only be issued by provider transport"
            ):
                journal._record_provider_origin(
                    attempt_id,
                    query,
                    http_status=200,
                    response_bytes=b'{"ok":true}',
                    observed_at=READ_NOW + timedelta(seconds=1),
                    _origin_token=object(),
                )
            events = JournalStore.load_events(
                journal._store,
                "authenticated_provider_read",
                attempt_id,
            )
            self.assertEqual([event["event_type"] for event in events], [
                "AuthenticatedReadPrepared"
            ])

    def test_prepared_only_attempt_cannot_become_response_binding(self):
        query = authenticated_read_binding()
        with TemporaryDirectory() as directory:
            journal = ProviderOriginJournal(
                JournalStore(f"{directory}/journal.sqlite3")
            )
            attempt_id = journal.prepare(
                query,
                transport_identity="UrllibJsonWireClient:v1",
                network_policy_identity="sha256:" + "3" * 64,
                recorded_at=READ_NOW,
            )
            with self.assertRaisesRegex(ProviderOriginError, "incomplete"):
                journal.load_response_binding(attempt_id, query)

    def test_exact_query_binding_is_revalidated_at_recovery(self):
        query = authenticated_read_binding()
        with TemporaryDirectory() as directory:
            journal = ProviderOriginJournal(
                JournalStore(f"{directory}/journal.sqlite3")
            )
            attempt_id = journal.prepare(
                query,
                transport_identity="UrllibJsonWireClient:v1",
                network_policy_identity="sha256:" + "4" * 64,
                recorded_at=READ_NOW,
            )
            journal._record_provider_origin(
                attempt_id,
                query,
                http_status=200,
                response_bytes=b'{"ok":true}',
                observed_at=READ_NOW + timedelta(seconds=1),
                _origin_token=_PROVIDER_ORIGIN_RECORD_TOKEN,
            )
            other = authenticated_read_binding(
                query={"omitZeroBalances": "false"}
            )
            with self.assertRaisesRegex(ProviderOriginError, "does not match"):
                journal.load_response_binding(attempt_id, other)

            object.__setattr__(query, "endpoint", "/api/v3/openOrders")
            with self.assertRaisesRegex(ProviderOriginError, "digest conflicts"):
                journal.load_response_binding(attempt_id, query)

    def test_second_conflicting_observation_cannot_replace_first(self):
        query = authenticated_read_binding()
        with TemporaryDirectory() as directory:
            journal = ProviderOriginJournal(
                JournalStore(f"{directory}/journal.sqlite3")
            )
            attempt_id = journal.prepare(
                query,
                transport_identity="UrllibJsonWireClient:v1",
                network_policy_identity="sha256:" + "5" * 64,
                recorded_at=READ_NOW,
            )
            first = journal._record_provider_origin(
                attempt_id,
                query,
                http_status=200,
                response_bytes=b'{"value":1}',
                observed_at=READ_NOW + timedelta(seconds=1),
                _origin_token=_PROVIDER_ORIGIN_RECORD_TOKEN,
            )
            with self.assertRaisesRegex(
                ProviderOriginError, "requires one exact durable Prepared event"
            ):
                journal._record_provider_origin(
                    attempt_id,
                    query,
                    http_status=200,
                    response_bytes=b'{"value":2}',
                    observed_at=READ_NOW + timedelta(seconds=2),
                    _origin_token=_PROVIDER_ORIGIN_RECORD_TOKEN,
                )
            recovered = journal.load_response_binding(
                attempt_id, authenticated_read_binding()
            )
            self.assertEqual(recovered.response_sha256, first.response_sha256)
            self.assertEqual(recovered.response_bytes, b'{"value":1}')

    def test_non_success_provider_origin_is_durable_but_not_financial_observation(self):
        query = authenticated_read_binding()
        with TemporaryDirectory() as directory:
            journal = ProviderOriginJournal(
                JournalStore(f"{directory}/journal.sqlite3")
            )
            attempt_id = journal.prepare(
                query,
                transport_identity="UrllibJsonWireClient:v1",
                network_policy_identity="sha256:" + "6" * 64,
                recorded_at=READ_NOW,
            )
            binding = journal._record_provider_origin(
                attempt_id,
                query,
                http_status=429,
                response_bytes=b'{"code":-1003,"msg":"too many requests"}',
                observed_at=READ_NOW + timedelta(seconds=1),
                _origin_token=_PROVIDER_ORIGIN_RECORD_TOKEN,
            )
            self.assertEqual(binding.http_status, 429)
            with self.assertRaisesRegex(ProviderOriginError, "endpoint policy"):
                observe_provider_origin_json_response(
                    response_binding=binding,
                    query_binding=query,
                    accepted_success_statuses=frozenset({200}),
                )

    def test_endpoint_policy_status_contract_is_exact(self):
        query = authenticated_read_binding()
        with TemporaryDirectory() as directory:
            journal = ProviderOriginJournal(
                JournalStore(f"{directory}/journal.sqlite3")
            )
            attempt_id = journal.prepare(
                query,
                transport_identity="UrllibJsonWireClient:v1",
                network_policy_identity="sha256:" + "7" * 64,
                recorded_at=READ_NOW,
            )
            binding = journal._record_provider_origin(
                attempt_id,
                query,
                http_status=201,
                response_bytes=b'{"ok":true}',
                observed_at=READ_NOW + timedelta(seconds=1),
                _origin_token=_PROVIDER_ORIGIN_RECORD_TOKEN,
            )
            with self.assertRaisesRegex(ProviderOriginError, "endpoint policy"):
                observe_provider_origin_json_response(
                    response_binding=binding,
                    query_binding=query,
                    accepted_success_statuses=frozenset({200}),
                )
            observation = observe_provider_origin_json_response(
                response_binding=binding,
                query_binding=query,
                accepted_success_statuses=frozenset({200, 201}),
            )
            self.assertEqual(observation.response_binding.http_status, 201)

    def test_response_binding_cannot_be_publicly_constructed(self):
        with self.assertRaisesRegex(
            ProviderOriginError, "must come from durable origin journal"
        ):
            AuthenticatedReadResponseBinding(
                attempt_id="provider-read:" + "a" * 32,
                provider_id="BINANCE",
                account_id="acct-1",
                environment="PAPER",
                capability_snapshot_id="cap-1",
                query_digest="sha256:" + "a" * 64,
                endpoint="/api/v3/account",
                prepared_event_id="prepared",
                observed_event_id="observed",
                observed_at="2026-09-25T10:00:01Z",
                http_status=200,
                response_sha256="sha256:" + "b" * 64,
                response_bytes=b"{}",
                transport_identity="UrllibJsonWireClient:v1",
                network_policy_identity="sha256:" + "d" * 64,
                origin_ref="provider-origin:sha256:" + "c" * 64,
                journal_sequence=2,
            )


if __name__ == "__main__":
    unittest.main()
