from __future__ import annotations

from contextlib import contextmanager
from io import BytesIO
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.provider_origin import (
    ProviderOriginError,
    _require_direct_terminal_after_prepared_sequence,
)
from mvp.autotrade_mvp.provider_route_reads import (
    issue_terminal_qualified_provider_read_authority,
    terminal_qualified_provider_read_authority_snapshot,
)
from mvp.autotrade_mvp.provider_transport import (
    BYBIT_V5_ENDPOINT_POLICIES,
    BybitV5AuthenticatedReadTransport,
    UrllibJsonWireClient,
    direct_authenticated_read_execution_receipt_snapshot,
    provider_observation_direct_execution_material,
)
from mvp.autotrade_mvp.windows_secrets import PersistentCredentialHandle
from mvp.tests.test_provider_origin import ProviderOriginJournalTests
from mvp.tests.test_provider_selection import NOW


class ProviderOriginRecordAuthorityTests(unittest.TestCase):
    def test_terminal_cut_cannot_precede_prepared_sequence(self):
        prepared = {"journal_sequence": 7}
        with self.assertRaisesRegex(
            ProviderOriginError,
            "predates durable Prepared",
        ):
            _require_direct_terminal_after_prepared_sequence(
                prepared_event=prepared,
                terminal_cut=6,
            )
        _require_direct_terminal_after_prepared_sequence(
            prepared_event=prepared,
            terminal_cut=7,
        )

    def test_prepared_must_precede_direct_wire_even_for_canonical_receipt(self):
        """A real receipt minted before Prepared must never be relabelled as origin."""

        class Resolver:
            @contextmanager
            def lease_for_execution(self, *_args, **_kwargs):
                yield (
                    '{"api_key":"SYNTHETIC-KEY",'
                    '"api_secret":"SYNTHETIC-SECRET"}'
                )

        with TemporaryDirectory() as directory:
            fixture = ProviderOriginJournalTests(
                methodName="test_exact_qualified_origin_survives_restart_without_requery"
            )
            self.addCleanup(fixture.doCleanups)
            (
                _route_fixture,
                journal,
                capabilities,
                qualifications,
                route,
                _q1,
                _harness,
                binding,
            ) = fixture._route_fixture(directory)
            origin = fixture._origin(journal, directory)
            body = b'{"retCode":0,"result":{"list":[{"coin":"USDT","equity":"12.00"}]}}'

            class Stream(BytesIO):
                status = 200

            client = UrllibJsonWireClient(max_response_bytes=1024)
            client._opener.open = lambda *_args, **_kwargs: Stream(body)
            base = binding.query_binding
            transport = BybitV5AuthenticatedReadTransport(
                policy=BYBIT_V5_ENDPOINT_POLICIES["TESTNET"],
                provider_environment="TESTNET",
                account_id=base.account_id,
                capability_snapshot_id=base.capability_snapshot_id,
                capability_registry=capabilities,
                secret_resolver=Resolver(),
                credential_handle=PersistentCredentialHandle(
                    handle_id="provider-origin-prepared-ordering",
                    account_id=base.account_id,
                    provider="BYBIT",
                    environment=base.environment,
                    provider_environment="TESTNET",
                    purpose="READ",
                    generation=1,
                ),
                session_token="provider-origin-prepared-ordering-session",
                origin="https://localhost",
                execution_identity="provider-origin-prepared-ordering-host",
                clock_millis=lambda: 1_700_000_000_777,
                clock_utc=lambda: NOW,
                wire_client=client,
            )

            provider_observation = transport(
                base,
                terminal_authority_factory=lambda received: (
                    issue_terminal_qualified_provider_read_authority(
                        route,
                        capabilities,
                        qualifications,
                        binding,
                        at=NOW,
                    )
                ),
            )
            receipt, exact_bytes = provider_observation_direct_execution_material(
                provider_observation
            )
            self.assertEqual(exact_bytes, body)
            receipt_snapshot = direct_authenticated_read_execution_receipt_snapshot(
                receipt
            )
            terminal_snapshot = terminal_qualified_provider_read_authority_snapshot(
                receipt_snapshot["terminal_authority"]
            )

            attempt_id = origin.prepare_direct(binding, recorded_at=NOW)
            prepared = journal.load_events(
                "qualified_authenticated_provider_read",
                attempt_id,
            )[0]
            self.assertLess(
                terminal_snapshot["journal_sequence_cut"],
                prepared["journal_sequence"],
            )

            for record in (
                origin.record_direct_provider_origin_observation,
                origin._record_provider_origin,
            ):
                with self.assertRaisesRegex(
                    ProviderOriginError,
                    "durable Prepared|canonical execute",
                ):
                    record(
                        attempt_id,
                        binding,
                        provider_observation=provider_observation,
                    )


    def test_valid_post_prepared_receipt_still_requires_canonical_execute_authority(self):
        """A chronologically valid direct receipt cannot bypass the execute fence."""

        class Resolver:
            @contextmanager
            def lease_for_execution(self, *_args, **_kwargs):
                yield (
                    '{"api_key":"SYNTHETIC-KEY",'
                    '"api_secret":"SYNTHETIC-SECRET"}'
                )

        with TemporaryDirectory() as directory:
            fixture = ProviderOriginJournalTests(
                methodName="test_exact_qualified_origin_survives_restart_without_requery"
            )
            self.addCleanup(fixture.doCleanups)
            (
                _route_fixture,
                journal,
                capabilities,
                qualifications,
                route,
                _q1,
                _harness,
                binding,
            ) = fixture._route_fixture(directory)
            origin = fixture._origin(journal, directory)
            body = b'{"retCode":0,"result":{"list":[{"coin":"USDT","equity":"12.00"}]}}'

            class Stream(BytesIO):
                status = 200

            client = UrllibJsonWireClient(max_response_bytes=1024)
            client._opener.open = lambda *_args, **_kwargs: Stream(body)
            base = binding.query_binding
            transport = BybitV5AuthenticatedReadTransport(
                policy=BYBIT_V5_ENDPOINT_POLICIES["TESTNET"],
                provider_environment="TESTNET",
                account_id=base.account_id,
                capability_snapshot_id=base.capability_snapshot_id,
                capability_registry=capabilities,
                secret_resolver=Resolver(),
                credential_handle=PersistentCredentialHandle(
                    handle_id="provider-origin-canonical-execute-fence",
                    account_id=base.account_id,
                    provider="BYBIT",
                    environment=base.environment,
                    provider_environment="TESTNET",
                    purpose="READ",
                    generation=1,
                ),
                session_token="provider-origin-canonical-execute-fence-session",
                origin="https://localhost",
                execution_identity="provider-origin-canonical-execute-fence-host",
                clock_millis=lambda: 1_700_000_000_888,
                clock_utc=lambda: NOW,
                wire_client=client,
            )

            attempt_id = origin.prepare_direct(binding, recorded_at=NOW)
            prepared = journal.load_events(
                "qualified_authenticated_provider_read",
                attempt_id,
            )[0]
            provider_observation = transport(
                base,
                terminal_authority_factory=lambda received: (
                    issue_terminal_qualified_provider_read_authority(
                        route,
                        capabilities,
                        qualifications,
                        binding,
                        at=NOW,
                    )
                ),
            )
            receipt, exact_bytes = provider_observation_direct_execution_material(
                provider_observation
            )
            self.assertEqual(exact_bytes, body)
            terminal_snapshot = terminal_qualified_provider_read_authority_snapshot(
                direct_authenticated_read_execution_receipt_snapshot(receipt)[
                    "terminal_authority"
                ]
            )
            self.assertGreaterEqual(
                terminal_snapshot["journal_sequence_cut"],
                prepared["journal_sequence"],
            )

            for record in (
                origin.record_direct_provider_origin_observation,
                origin._record_provider_origin,
            ):
                with self.assertRaisesRegex(
                    ProviderOriginError,
                    "canonical execute",
                ):
                    record(
                        attempt_id,
                        binding,
                        provider_observation=provider_observation,
                    )

            events = journal.load_events(
                "qualified_authenticated_provider_read",
                attempt_id,
            )
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["event_type"], "AuthenticatedReadPrepared")


if __name__ == "__main__":
    unittest.main()
