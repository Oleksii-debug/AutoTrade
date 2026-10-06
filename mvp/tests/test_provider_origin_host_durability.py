from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
from tempfile import TemporaryDirectory
import unittest

import mvp.autotrade_mvp.provider_origin_host_durability as durability_module
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.provider_core import Surface, prepare_authenticated_read_query
from mvp.autotrade_mvp.provider_host_attestation import (
    HostAuthenticatedReadAttempt,
    HostAuthenticatedReadReceipt,
    HostAuthenticatedReadSubject,
    HostIssuerSession,
    VerifiedHostPreparedAttestation,
)
from mvp.autotrade_mvp.provider_origin_host_bridge import (
    HostProviderOriginPins,
    canonical_bybit_authenticated_read_rule_identity,
)
from mvp.autotrade_mvp.provider_origin_host_durability import (
    ProviderOriginHostDurabilityError,
    _commit_host_observed_impl,
    _commit_host_prepared_impl,
    commit_bybit_host_prepared_to_canonical_journal,
)
from mvp.tests.test_bybit_v5 import READ_AT, read_capability


class ProviderOriginHostDurabilityTests(unittest.TestCase):
    def _query(self):
        capability = read_capability(
            account_id="acct-1",
            environment="PAPER",
            instrument_version="BTCUSDT@v1",
            provider_environment="TESTNET",
            permission_scopes=("ORDER.READ",),
            at=READ_AT,
        )
        return prepare_authenticated_read_query(
            capability=capability,
            surface=Surface.AUTHENTICATED_READ,
            endpoint="/v5/execution/list",
            query={"category": "linear"},
            at=READ_AT,
            permission_scope="ORDER.READ",
        )

    @staticmethod
    def _pins():
        return HostProviderOriginPins(
            credential_handle_id="credential-1",
            credential_generation=1,
            qualification_id="qualification-1",
            qualification_build_id="qualification-build-1",
            adapter_build_identity="adapter-build-1",
            network_policy_identity="sha256:" + "3" * 64,
            transport_identity="UrllibJsonWireClient:v1",
        )

    def _verified_prepared(self, query, *, prepared_at="2026-09-24T20:00:01.0000000Z"):
        pins = self._pins()
        rule_identity, rule = canonical_bybit_authenticated_read_rule_identity(query)
        subject = HostAuthenticatedReadSubject(
            provider_id=query.provider_id,
            account_id=query.account_id,
            entity_id=query.entity_id,
            runtime_environment=query.environment,
            provider_environment=query.provider_environment,
            endpoint=query.endpoint,
            surface=query.surface.value,
            permission_scope=query.permission_scope,
            data_entitlement=rule.data_entitlement,
            instrument_version=query.instrument_version,
            query_digest=query.query_digest,
            endpoint_rule_identity=rule_identity,
            credential_handle_id=pins.credential_handle_id,
            credential_generation=pins.credential_generation,
            capability_id=query.capability_snapshot_id,
            qualification_id=pins.qualification_id,
            qualification_build_id=pins.qualification_build_id,
            adapter_build_identity=pins.adapter_build_identity,
            network_policy_identity=pins.network_policy_identity,
            transport_identity=pins.transport_identity,
        )
        session = HostIssuerSession(
            issuer_instance_id="provider-issuer:" + "1" * 32,
            started_at_utc="2026-09-24T19:59:59.0000000Z",
            public_key_spki_base64="test-only",
            public_key_sha256="sha256:" + "a" * 64,
            session_identity="provider-issuer-session:sha256:" + "b" * 64,
            public_key_spki=b"test-only",
        )
        attempt = HostAuthenticatedReadAttempt(
            issuer_session_identity=session.session_identity,
            subject=subject,
            read_generation=1,
            read_attempt_id="provider-read:" + "1" * 32,
            prepared_at_utc=prepared_at,
            binding_sha256="sha256:" + "c" * 64,
            signature_base64="test-only",
        )
        return VerifiedHostPreparedAttestation(
            issuer_session=session,
            attempt=attempt,
            query=query.query,
        )

    @staticmethod
    def _receipt(verified, response, *, observed_at="2026-09-24T20:00:02.0000000Z"):
        return HostAuthenticatedReadReceipt(
            issuer_session_identity=verified.issuer_session.session_identity,
            read_attempt_binding_sha256=verified.attempt.binding_sha256,
            read_attempt_id=verified.attempt.read_attempt_id,
            read_generation=verified.attempt.read_generation,
            http_status=200,
            response_sha256="sha256:" + sha256(response).hexdigest(),
            response_length=len(response),
            observed_at_utc=observed_at,
            receipt_sha256="sha256:" + "d" * 64,
            signature_base64="test-only",
        )

    def test_verified_prepared_commits_one_exact_idempotent_row_and_receipt(self):
        query = self._query()
        verified = self._verified_prepared(query)
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            kwargs = dict(
                query_binding=query,
                store=store,
                pins=self._pins(),
                expected_session_identity=verified.issuer_session.session_identity,
                expected_public_key_sha256=verified.issuer_session.public_key_sha256,
                verify_prepared=lambda *_args, **_kwargs: verified,
            )
            first = _commit_host_prepared_impl({}, **kwargs)
            second = _commit_host_prepared_impl({}, **kwargs)
            self.assertEqual(first, second)
            events = JournalStore.load_events(
                store,
                "authenticated_provider_read",
                verified.attempt.read_attempt_id,
            )
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["event_type"], "AuthenticatedReadPrepared")
            self.assertEqual(first.prepared_event_id, events[0]["event_id"])
            self.assertEqual(first.journal_sequence, events[0]["journal_sequence"])
            self.assertTrue(first.journal_identity.startswith("sha256:"))

    def test_prepared_before_canonical_query_time_fails_before_mutation(self):
        query = self._query()
        verified = self._verified_prepared(
            query,
            prepared_at="2026-09-24T19:59:59.9999999Z",
        )
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            with self.assertRaisesRegex(
                ProviderOriginHostDurabilityError,
                "predates canonical query preparation",
            ):
                _commit_host_prepared_impl(
                    {},
                    query_binding=query,
                    store=store,
                    pins=self._pins(),
                    expected_session_identity=verified.issuer_session.session_identity,
                    expected_public_key_sha256=verified.issuer_session.public_key_sha256,
                    verify_prepared=lambda *_args, **_kwargs: verified,
                )
            self.assertEqual(
                JournalStore.load_events(
                    store,
                    "authenticated_provider_read",
                    verified.attempt.read_attempt_id,
                ),
                [],
            )

    def test_submicrosecond_host_time_rounds_up_never_backwards(self):
        python_text, host_text = durability_module._journal_utc_from_host(
            "2026-09-24T20:00:01.0000001Z"
        )
        self.assertEqual(python_text, "2026-09-24T20:00:01.000001Z")
        self.assertEqual(host_text, "2026-09-24T20:00:01.0000010Z")

    def test_verified_response_commits_origin_row_and_is_restart_idempotent(self):
        query = self._query()
        verified = self._verified_prepared(query)
        response = b'{"retCode":0,"result":{"list":[]}}'
        receipt = self._receipt(verified, response)
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            store = JournalStore(path)
            kwargs = dict(
                query_binding=query,
                pins=self._pins(),
                expected_session_identity=verified.issuer_session.session_identity,
                expected_public_key_sha256=verified.issuer_session.public_key_sha256,
                verify_prepared=lambda *_args, **_kwargs: verified,
                verify_receipt=lambda *_args, **_kwargs: receipt,
            )
            first = _commit_host_observed_impl(
                {},
                {},
                response,
                store=store,
                **kwargs,
            )
            restarted = JournalStore(path)
            second = _commit_host_observed_impl(
                {},
                {},
                response,
                store=restarted,
                **kwargs,
            )
            self.assertEqual(first, second)
            events = JournalStore.load_events(
                restarted,
                "authenticated_provider_read",
                verified.attempt.read_attempt_id,
            )
            self.assertEqual(len(events), 2)
            self.assertEqual(
                [event["event_type"] for event in events],
                ["AuthenticatedReadPrepared", "AuthenticatedReadObserved"],
            )
            self.assertEqual(events[1]["payload"]["origin_kind"], "PROVIDER_ORIGIN")
            self.assertEqual(events[1]["payload"]["response_sha256"], receipt.response_sha256)
            self.assertEqual(first.observed_event_id, events[1]["event_id"])
            self.assertGreater(
                first.observed_journal_sequence,
                first.prepared_journal_sequence,
            )

    def test_conflicting_signed_response_cannot_replace_observed_row(self):
        query = self._query()
        verified = self._verified_prepared(query)
        first_bytes = b'{"retCode":0,"result":{"list":[]}}'
        first_receipt = self._receipt(verified, first_bytes)
        second_bytes = b'{"retCode":0,"result":{"list":[1]}}'
        second_receipt = replace(
            first_receipt,
            response_sha256="sha256:" + sha256(second_bytes).hexdigest(),
            response_length=len(second_bytes),
            receipt_sha256="sha256:" + "e" * 64,
        )
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            common = dict(
                query_binding=query,
                store=store,
                pins=self._pins(),
                expected_session_identity=verified.issuer_session.session_identity,
                expected_public_key_sha256=verified.issuer_session.public_key_sha256,
                verify_prepared=lambda *_args, **_kwargs: verified,
            )
            _commit_host_observed_impl(
                {},
                {},
                first_bytes,
                verify_receipt=lambda *_args, **_kwargs: first_receipt,
                **common,
            )
            with self.assertRaisesRegex(
                ProviderOriginHostDurabilityError,
                "conflicts with signed Host response",
            ):
                _commit_host_observed_impl(
                    {},
                    {},
                    second_bytes,
                    verify_receipt=lambda *_args, **_kwargs: second_receipt,
                    **common,
                )
            events = JournalStore.load_events(
                store,
                "authenticated_provider_read",
                verified.attempt.read_attempt_id,
            )
            self.assertEqual(len(events), 2)
            self.assertEqual(
                events[1]["payload"]["response_sha256"],
                first_receipt.response_sha256,
            )

    def test_public_prepared_commit_retains_captured_cng_verifier(self):
        query = self._query()
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            calls = []
            original = durability_module.verify_bybit_host_prepared_against_binding
            durability_module.verify_bybit_host_prepared_against_binding = (
                lambda *_args, **_kwargs: calls.append("hostile")
            )
            try:
                with self.assertRaises(ProviderOriginHostDurabilityError):
                    commit_bybit_host_prepared_to_canonical_journal(
                        {},
                        query_binding=query,
                        store=store,
                        pins=self._pins(),
                        expected_session_identity=(
                            "provider-issuer-session:sha256:" + "1" * 64
                        ),
                        expected_public_key_sha256="sha256:" + "2" * 64,
                    )
            finally:
                durability_module.verify_bybit_host_prepared_against_binding = original
            self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
