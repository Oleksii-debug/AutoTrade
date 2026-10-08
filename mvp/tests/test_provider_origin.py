import base64
from datetime import datetime, timedelta, timezone, tzinfo
from hashlib import sha256
from tempfile import TemporaryDirectory
import unittest

import mvp.autotrade_mvp.provider_origin as provider_origin_module
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.provider_core import Surface, prepare_authenticated_read_query
from mvp.autotrade_mvp.provider_origin import (
    AuthenticatedReadResponseBinding,
    ProviderOriginError,
    ProviderOriginJournal,
    observe_provider_origin_json_response,
    observe_test_injected_json_response,
)
from mvp.tests.test_bybit_v5 import READ_AT as BYBIT_READ_AT, read_capability
from mvp.tests.test_provider_transport import (
    READ_NOW,
    authenticated_read_binding,
)


class _HostileTimezone(tzinfo):
    def __init__(self) -> None:
        self.calls = 0

    def utcoffset(self, _dt):
        self.calls += 1
        raise AssertionError("hostile tzinfo callback executed")

    def dst(self, _dt):
        self.calls += 1
        raise AssertionError("hostile tzinfo callback executed")

    def tzname(self, _dt):
        self.calls += 1
        raise AssertionError("hostile tzinfo callback executed")


class ProviderOriginJournalTests(unittest.TestCase):
    def test_prepare_rejects_tzinfo_subclass_before_callback_or_journal_mutation(self):
        query = authenticated_read_binding()
        hostile_tz = _HostileTimezone()
        hostile_time = datetime(
            READ_NOW.year,
            READ_NOW.month,
            READ_NOW.day,
            READ_NOW.hour,
            READ_NOW.minute,
            READ_NOW.second,
            READ_NOW.microsecond,
            tzinfo=hostile_tz,
        )
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            journal = ProviderOriginJournal(store)
            with self.assertRaisesRegex(
                ProviderOriginError,
                "exact datetime.timezone tzinfo",
            ):
                journal.prepare(
                    query,
                    transport_identity="UrllibJsonWireClient:v1",
                    network_policy_identity="sha256:" + "f" * 64,
                    recorded_at=hostile_time,
                )
            self.assertEqual(hostile_tz.calls, 0)
            self.assertEqual(
                JournalStore.load_events(
                    store,
                    "authenticated_provider_read",
                    "provider-read:does-not-exist",
                ),
                [],
            )

    def test_observed_at_rejects_tzinfo_subclass_before_callback_or_observed_event(self):
        query = authenticated_read_binding()
        hostile_tz = _HostileTimezone()
        hostile_time = datetime(
            READ_NOW.year,
            READ_NOW.month,
            READ_NOW.day,
            READ_NOW.hour,
            READ_NOW.minute,
            READ_NOW.second + 1,
            READ_NOW.microsecond,
            tzinfo=hostile_tz,
        )
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            journal = ProviderOriginJournal(store)
            attempt_id = journal.prepare(
                query,
                transport_identity="UrllibJsonWireClient:v1",
                network_policy_identity="sha256:" + "d" * 64,
                recorded_at=READ_NOW,
            )
            with self.assertRaisesRegex(
                ProviderOriginError,
                "exact datetime.timezone tzinfo",
            ):
                journal._record_test_injected_response(
                    attempt_id,
                    query,
                    http_status=200,
                    response_bytes=b'{"ok":true}',
                    observed_at=hostile_time,
                )
            self.assertEqual(hostile_tz.calls, 0)
            events = JournalStore.load_events(
                store,
                "authenticated_provider_read",
                attempt_id,
            )
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["event_type"], "AuthenticatedReadPrepared")

    def test_prepare_accepts_exact_fixed_offset_timezone_and_normalizes_to_utc(self):
        query = authenticated_read_binding()
        fixed = READ_NOW.astimezone(timezone(timedelta(hours=2)))
        self.assertIs(type(fixed), datetime)
        self.assertIs(type(fixed.tzinfo), timezone)
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            journal = ProviderOriginJournal(store)
            attempt_id = journal.prepare(
                query,
                transport_identity="UrllibJsonWireClient:v1",
                network_policy_identity="sha256:" + "e" * 64,
                recorded_at=fixed,
            )
            events = JournalStore.load_events(
                store,
                "authenticated_provider_read",
                attempt_id,
            )
            self.assertEqual(len(events), 1)
            self.assertEqual(
                events[0]["committed_at"],
                READ_NOW.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            )

    def test_test_injected_replay_survives_restart_without_network_requery(self):
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
            binding = journal._record_test_injected_response(
                attempt_id,
                query,
                http_status=200,
                response_bytes=body,
                observed_at=READ_NOW + timedelta(seconds=1),
            )
            self.assertEqual(binding.response_bytes, body)
            self.assertEqual(binding.http_status, 200)
            self.assertEqual(binding.provider_environment, "PAPER")
            self.assertEqual(binding.transport_identity, "UrllibJsonWireClient:v1")
            self.assertEqual(binding.network_policy_identity, "sha256:" + "1" * 64)
            self.assertEqual(binding.evidence_class, "TEST_INJECTED")
            self.assertTrue(binding.origin_ref.startswith("provider-test:sha256:"))
            self.assertGreater(binding.journal_sequence, 0)

            restarted = ProviderOriginJournal(JournalStore(path))
            recovered = restarted.load_response_binding(attempt_id, query)
            self.assertEqual(recovered, binding)
            self.assertEqual(recovered.response_bytes, body)
            with self.assertRaisesRegex(
                ProviderOriginError,
                "independently authenticated provider-wire origin",
            ):
                observe_provider_origin_json_response(
                    response_binding=recovered,
                    query_binding=query,
                    accepted_success_statuses=frozenset({200}),
                )
            observation = observe_test_injected_json_response(
                response_binding=recovered,
                query_binding=query,
                accepted_success_statuses=frozenset({200}),
            )
            self.assertEqual(observation.provider_id, "BINANCE")
            self.assertEqual(observation.account_id, "acct-1")
            self.assertEqual(observation.environment, "PAPER")
            self.assertEqual(observation.payload["balances"][0]["free"], "100.00")

    def test_mutating_test_binding_cannot_promote_provider_origin(self):
        query = authenticated_read_binding()
        with TemporaryDirectory() as directory:
            journal = ProviderOriginJournal(
                JournalStore(f"{directory}/journal.sqlite3")
            )
            attempt_id = journal.prepare(
                query,
                transport_identity="UrllibJsonWireClient:v1",
                network_policy_identity="sha256:" + "a" * 64,
                recorded_at=READ_NOW,
            )
            binding = journal._record_test_injected_response(
                attempt_id,
                query,
                http_status=200,
                response_bytes=b'{"ok":true}',
                observed_at=READ_NOW + timedelta(seconds=1),
            )
            object.__setattr__(binding, "evidence_class", "PROVIDER_ORIGIN")
            object.__setattr__(
                binding,
                "origin_ref",
                "provider-origin:sha256:" + "b" * 64,
            )
            with self.assertRaisesRegex(
                ProviderOriginError,
                "in-process response binding",
            ):
                binding.require_provider_origin()

    def test_in_process_caller_cannot_issue_provider_origin(self):
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
                ProviderOriginError, "independent external provider-wire issuer"
            ):
                journal._record_provider_origin(
                    attempt_id,
                    query,
                    http_status=200,
                    response_bytes=b'{"ok":true}',
                    observed_at=READ_NOW + timedelta(seconds=1),
                )
            events = JournalStore.load_events(
                journal._store,
                "authenticated_provider_read",
                attempt_id,
            )
            self.assertEqual(
                [event["event_type"] for event in events],
                ["AuthenticatedReadPrepared"],
            )

    def test_rebound_in_process_predicate_cannot_promote_provider_origin(self):
        query = authenticated_read_binding()
        body = b'{"ok":true}'
        with TemporaryDirectory() as directory:
            journal = ProviderOriginJournal(
                JournalStore(f"{directory}/journal.sqlite3")
            )
            attempt_id = journal.prepare(
                query,
                transport_identity="UrllibJsonWireClient:v1",
                network_policy_identity="sha256:" + "c" * 64,
                recorded_at=READ_NOW,
            )
            binding = journal._record_test_injected_response(
                attempt_id,
                query,
                http_status=200,
                response_bytes=body,
                observed_at=READ_NOW + timedelta(seconds=1),
            )
            neutral = observe_test_injected_json_response(
                response_binding=binding,
                query_binding=query,
                accepted_success_statuses=frozenset({200}),
            )

            original = AuthenticatedReadResponseBinding.require_provider_origin
            AuthenticatedReadResponseBinding.require_provider_origin = lambda _self: None
            try:
                with self.assertRaisesRegex(
                    ProviderOriginError,
                    "independently authenticated provider-wire issuer",
                ):
                    observe_provider_origin_json_response(
                        response_binding=binding,
                        query_binding=query,
                        accepted_success_statuses=frozenset({200}),
                    )
                with self.assertRaisesRegex(
                    ProviderOriginError,
                    "independently authenticated provider-wire issuer",
                ):
                    provider_origin_module.ProviderOriginObservation(
                        response_binding=binding,
                        observation=neutral,
                        _observation_token=provider_origin_module._OBSERVATION_TOKEN,
                    )
            finally:
                AuthenticatedReadResponseBinding.require_provider_origin = original

    def test_self_consistent_unissued_query_binding_cannot_enter_origin_journal(self):
        query = authenticated_read_binding()
        forged = object.__new__(type(query))
        for name, value in vars(query).items():
            object.__setattr__(forged, name, value)
        self.assertEqual(vars(forged), vars(query))

        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            journal = ProviderOriginJournal(store)
            with self.assertRaisesRegex(
                ProviderOriginError,
                "lacks canonical preparation authority",
            ):
                journal.prepare(
                    forged,
                    transport_identity="UrllibJsonWireClient:v1",
                    network_policy_identity="sha256:" + "c" * 64,
                    recorded_at=READ_NOW,
                )
            self.assertEqual(
                JournalStore.load_events(
                    store,
                    "authenticated_provider_read",
                    "provider-read:forged",
                ),
                [],
            )

    def test_rebound_query_authority_alias_cannot_admit_forged_binding(self):
        query = authenticated_read_binding()
        forged = object.__new__(type(query))
        for name, value in vars(query).items():
            object.__setattr__(forged, name, value)

        original = provider_origin_module._require_authenticated_read_query_binding_authority
        provider_origin_module._require_authenticated_read_query_binding_authority = (
            lambda _value: None
        )
        try:
            with TemporaryDirectory() as directory:
                journal = ProviderOriginJournal(
                    JournalStore(f"{directory}/journal.sqlite3")
                )
                with self.assertRaisesRegex(
                    ProviderOriginError,
                    "lacks canonical preparation authority",
                ):
                    journal.prepare(
                        forged,
                        transport_identity="UrllibJsonWireClient:v1",
                        network_policy_identity="sha256:" + "b" * 64,
                        recorded_at=READ_NOW,
                    )
        finally:
            provider_origin_module._require_authenticated_read_query_binding_authority = original

    def test_provider_origin_journal_io_ignores_rebound_journalstore_alias(self):
        query = authenticated_read_binding()
        with TemporaryDirectory() as directory:
            real_store = JournalStore(f"{directory}/journal.sqlite3")
            journal = ProviderOriginJournal(real_store)

            class FakeJournalStore:
                calls = 0

                @staticmethod
                def append_event(*_args, **_kwargs):
                    FakeJournalStore.calls += 1
                    raise AssertionError("rebound append_event executed")

                @staticmethod
                def load_events(*_args, **_kwargs):
                    FakeJournalStore.calls += 1
                    raise AssertionError("rebound load_events executed")

            original = provider_origin_module.JournalStore
            provider_origin_module.JournalStore = FakeJournalStore
            try:
                attempt_id = journal.prepare(
                    query,
                    transport_identity="UrllibJsonWireClient:v1",
                    network_policy_identity="sha256:" + "a" * 64,
                    recorded_at=READ_NOW,
                )
                binding = journal._record_test_injected_response(
                    attempt_id,
                    query,
                    http_status=200,
                    response_bytes=b'{"ok":true}',
                    observed_at=READ_NOW + timedelta(seconds=1),
                )
            finally:
                provider_origin_module.JournalStore = original

            self.assertEqual(FakeJournalStore.calls, 0)
            self.assertEqual(binding.evidence_class, "TEST_INJECTED")
            self.assertEqual(binding.response_bytes, b'{"ok":true}')
            events = JournalStore.load_events(
                real_store,
                "authenticated_provider_read",
                attempt_id,
            )
            self.assertEqual(
                [event["event_type"] for event in events],
                ["AuthenticatedReadPrepared", "AuthenticatedReadObserved"],
            )

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
            journal._record_test_injected_response(
                attempt_id,
                query,
                http_status=200,
                response_bytes=b'{"ok":true}',
                observed_at=READ_NOW + timedelta(seconds=1),
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
            first = journal._record_test_injected_response(
                attempt_id,
                query,
                http_status=200,
                response_bytes=b'{"value":1}',
                observed_at=READ_NOW + timedelta(seconds=1),
            )
            with self.assertRaisesRegex(
                ProviderOriginError, "requires one exact durable Prepared event"
            ):
                journal._record_test_injected_response(
                    attempt_id,
                    query,
                    http_status=200,
                    response_bytes=b'{"value":2}',
                    observed_at=READ_NOW + timedelta(seconds=2),
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
            binding = journal._record_test_injected_response(
                attempt_id,
                query,
                http_status=429,
                response_bytes=b'{"code":-1003,"msg":"too many requests"}',
                observed_at=READ_NOW + timedelta(seconds=1),
            )
            self.assertEqual(binding.http_status, 429)
            with self.assertRaisesRegex(ProviderOriginError, "endpoint policy"):
                observe_test_injected_json_response(
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
            binding = journal._record_test_injected_response(
                attempt_id,
                query,
                http_status=201,
                response_bytes=b'{"ok":true}',
                observed_at=READ_NOW + timedelta(seconds=1),
            )
            with self.assertRaisesRegex(ProviderOriginError, "endpoint policy"):
                observe_test_injected_json_response(
                    response_binding=binding,
                    query_binding=query,
                    accepted_success_statuses=frozenset({200}),
                )
            observation = observe_test_injected_json_response(
                response_binding=binding,
                query_binding=query,
                accepted_success_statuses=frozenset({200, 201}),
            )
            self.assertEqual(observation.response_binding.http_status, 201)

    def test_bybit_testnet_and_demo_origin_identity_cannot_cross_replay(self):
        kwargs = dict(
            surface=Surface.AUTHENTICATED_READ,
            endpoint="/v5/execution/list",
            query={"category": "spot", "limit": "100"},
            at=BYBIT_READ_AT,
            permission_scope="ORDER.READ",
        )
        testnet_query = prepare_authenticated_read_query(
            capability=read_capability(provider_environment="TESTNET"),
            provider_environment="TESTNET",
            **kwargs,
        )
        demo_query = prepare_authenticated_read_query(
            capability=read_capability(provider_environment="DEMO"),
            provider_environment="DEMO",
            **kwargs,
        )
        self.assertEqual(testnet_query.environment, "PAPER")
        self.assertEqual(demo_query.environment, "PAPER")
        self.assertEqual(testnet_query.provider_environment, "TESTNET")
        self.assertEqual(demo_query.provider_environment, "DEMO")
        self.assertNotEqual(testnet_query.query_digest, demo_query.query_digest)

        body = b'{"list":[{"symbol":"BTCUSDT","execQty":"1"}]}'
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            journal = ProviderOriginJournal(JournalStore(path))
            testnet_attempt = journal.prepare(
                testnet_query,
                transport_identity="UrllibJsonWireClient:v1",
                network_policy_identity="sha256:" + "8" * 64,
                recorded_at=BYBIT_READ_AT,
            )
            testnet_binding = journal._record_test_injected_response(
                testnet_attempt,
                testnet_query,
                http_status=200,
                response_bytes=body,
                observed_at=BYBIT_READ_AT + timedelta(seconds=1),
            )
            self.assertEqual(testnet_binding.provider_environment, "TESTNET")
            with self.assertRaisesRegex(ProviderOriginError, "does not match exact query binding"):
                journal.load_response_binding(testnet_attempt, demo_query)

            demo_attempt = journal.prepare(
                demo_query,
                transport_identity="UrllibJsonWireClient:v1",
                network_policy_identity="sha256:" + "8" * 64,
                recorded_at=BYBIT_READ_AT,
            )
            demo_binding = journal._record_test_injected_response(
                demo_attempt,
                demo_query,
                http_status=200,
                response_bytes=body,
                observed_at=BYBIT_READ_AT + timedelta(seconds=1),
            )
            self.assertEqual(demo_binding.provider_environment, "DEMO")
            self.assertNotEqual(testnet_binding.origin_ref, demo_binding.origin_ref)

            restarted = ProviderOriginJournal(JournalStore(path))
            self.assertEqual(
                restarted.load_response_binding(testnet_attempt, testnet_query).provider_environment,
                "TESTNET",
            )
            self.assertEqual(
                restarted.load_response_binding(demo_attempt, demo_query).provider_environment,
                "DEMO",
            )

    def test_forged_provider_origin_event_cannot_cross_restart_loader(self):
        query = authenticated_read_binding()
        body = b'{"ok":true}'
        with TemporaryDirectory() as directory:
            journal = ProviderOriginJournal(
                JournalStore(f"{directory}/journal.sqlite3")
            )
            attempt_id = journal.prepare(
                query,
                transport_identity="UrllibJsonWireClient:v1",
                network_policy_identity="sha256:" + "9" * 64,
                recorded_at=READ_NOW,
            )
            prepared = JournalStore.load_events(
                journal._store,
                "authenticated_provider_read",
                attempt_id,
            )[0]
            observed_at = (
                READ_NOW + timedelta(seconds=1)
            ).isoformat().replace("+00:00", "Z")
            observed_payload = {
                "origin_kind": "PROVIDER_ORIGIN",
                "prepared_event_id": prepared["event_id"],
                "query_digest": query.query_digest,
                "transport_identity": "UrllibJsonWireClient:v1",
                "network_policy_identity": "sha256:" + "9" * 64,
                "http_status": 200,
                "response_sha256": "sha256:" + sha256(body).hexdigest(),
                "response_base64": base64.b64encode(body).decode("ascii"),
                "observed_at": observed_at,
            }
            JournalStore.append_event(
                journal._store,
                journal._event(
                    event_id=attempt_id + ":observed",
                    event_type="AuthenticatedReadObserved",
                    attempt_id=attempt_id,
                    aggregate_version=2,
                    payload=observed_payload,
                    committed_at=observed_at,
                ),
            )
            with self.assertRaisesRegex(
                ProviderOriginError,
                "external provider-wire attestation verifier",
            ):
                journal.load_response_binding(attempt_id, query)

    def test_response_binding_cannot_be_publicly_constructed(self):
        with self.assertRaisesRegex(
            ProviderOriginError, "must come from durable origin journal"
        ):
            AuthenticatedReadResponseBinding(
                attempt_id="provider-read:" + "a" * 32,
                provider_id="BINANCE",
                account_id="acct-1",
                environment="PAPER",
                provider_environment="PAPER",
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
                evidence_class="PROVIDER_ORIGIN",
                origin_ref="provider-origin:sha256:" + "c" * 64,
                journal_sequence=2,
            )


if __name__ == "__main__":
    unittest.main()
