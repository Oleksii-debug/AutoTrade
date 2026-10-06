from __future__ import annotations

import base64
from datetime import timedelta
from dataclasses import replace
from hashlib import sha256
from tempfile import TemporaryDirectory
import unittest

import mvp.autotrade_mvp.provider_origin_host_bridge as bridge_module
import mvp.autotrade_mvp.provider_host_attestation as host_attestation
from mvp.autotrade_mvp.persistence import (
    JournalStore,
    require_exact_journal_store_authority,
)
from mvp.autotrade_mvp.provider_core import (
    Surface,
    prepare_authenticated_read_query,
)
from mvp.autotrade_mvp.provider_host_attestation import (
    HostAuthenticatedReadAttempt,
    HostAuthenticatedReadReceipt,
    HostAuthenticatedReadSubject,
    HostIssuerSession,
    HostObservedDurabilityReceipt,
    HostPreparedDurabilityReceipt,
    VerifiedHostObservedAttestation,
    VerifiedHostPreparedAttestation,
)
from mvp.autotrade_mvp.provider_origin import (
    ProviderOriginJournal,
    _OBSERVED_EVENT,
    _PENDING_KIND,
    _PREPARED_EVENT,
    _PROVIDER_ORIGIN_KIND,
    _append_origin_event,
    _query_snapshot,
)
from mvp.autotrade_mvp.provider_origin_host_bridge import (
    HostProviderOriginPins,
    ProviderOriginHostBridgeError,
    canonical_bybit_authenticated_read_rule_identity,
    canonical_provider_origin_journal_identity,
    verify_bybit_host_observed_against_canonical_journal,
)
from mvp.tests.test_bybit_v5 import READ_AT, read_capability


class ProviderOriginHostBridgeTests(unittest.TestCase):
    def _query_binding(self, *, endpoint="/v5/execution/list"):
        capability = read_capability(
            account_id="acct-1",
            environment="PAPER",
            instrument_version="instrument-version-1",
            provider_environment="TESTNET",
            permission_scopes=("ORDER.READ",),
            at=READ_AT,
        )
        return prepare_authenticated_read_query(
            capability=capability,
            surface=Surface.AUTHENTICATED_READ,
            endpoint=endpoint,
            query={"category": "linear"},
            at=READ_AT,
            permission_scope="ORDER.READ",
        )

    @staticmethod
    def _pins():
        return HostProviderOriginPins(
            credential_handle_id="credential-handle-1",
            credential_generation=7,
            qualification_id="qualification-1",
            qualification_build_id="qualification-build-1",
            adapter_build_identity="adapter-build-1",
            network_policy_identity="sha256:" + "3" * 64,
            transport_identity="UrllibJsonWireClient:v1",
        )

    def _seed_provider_origin_rows(
        self,
        store,
        query_binding,
        *,
        attempt_id,
        response=b'{"retCode":0,"result":{"list":[]}}',
    ):
        pins = self._pins()
        snapshot = _query_snapshot(query_binding)
        journal = ProviderOriginJournal(store)
        selected_store, identity = journal._require_store()
        prepared_at = (READ_AT + timedelta(seconds=1)).isoformat().replace(
            "+00:00", "Z"
        )
        prepared = ProviderOriginJournal._event(
            event_id=attempt_id + ":prepared",
            event_type=_PREPARED_EVENT,
            attempt_id=attempt_id,
            aggregate_version=1,
            payload={
                "origin_kind": _PENDING_KIND,
                "query": snapshot,
                "transport_identity": pins.transport_identity,
                "network_policy_identity": pins.network_policy_identity,
            },
            committed_at=prepared_at,
        )
        _append_origin_event(selected_store, identity, prepared)
        observed_at = (READ_AT + timedelta(seconds=2)).isoformat().replace(
            "+00:00", "Z"
        )
        observed = ProviderOriginJournal._event(
            event_id=attempt_id + ":observed",
            event_type=_OBSERVED_EVENT,
            attempt_id=attempt_id,
            aggregate_version=2,
            payload={
                "origin_kind": _PROVIDER_ORIGIN_KIND,
                "prepared_event_id": attempt_id + ":prepared",
                "query_digest": snapshot["query_digest"],
                "transport_identity": pins.transport_identity,
                "network_policy_identity": pins.network_policy_identity,
                "http_status": 200,
                "response_sha256": "sha256:" + sha256(response).hexdigest(),
                "response_base64": base64.b64encode(response).decode("ascii"),
                "observed_at": observed_at,
            },
            committed_at=observed_at,
        )
        _append_origin_event(selected_store, identity, observed)
        return response

    def _verified_observed(self, store, query_binding, *, attempt_id, response):
        pins = self._pins()
        rule_identity, rule = canonical_bybit_authenticated_read_rule_identity(
            query_binding
        )
        snapshot = _query_snapshot(query_binding)
        subject = HostAuthenticatedReadSubject(
            provider_id=snapshot["provider_id"],
            account_id=snapshot["account_id"],
            entity_id=snapshot["entity_id"],
            runtime_environment=snapshot["environment"],
            provider_environment=snapshot["provider_environment"],
            endpoint=snapshot["endpoint"],
            surface=snapshot["surface"],
            permission_scope=snapshot["permission_scope"],
            data_entitlement=rule.data_entitlement,
            instrument_version=snapshot["instrument_version"],
            query_digest=snapshot["query_digest"],
            endpoint_rule_identity=rule_identity,
            credential_handle_id=pins.credential_handle_id,
            credential_generation=pins.credential_generation,
            capability_id=snapshot["capability_snapshot_id"],
            qualification_id=pins.qualification_id,
            qualification_build_id=pins.qualification_build_id,
            adapter_build_identity=pins.adapter_build_identity,
            network_policy_identity=pins.network_policy_identity,
            transport_identity=pins.transport_identity,
        )
        session = HostIssuerSession(
            issuer_instance_id="provider-issuer:" + "1" * 32,
            started_at_utc="2026-09-24T20:00:00.0000000Z",
            public_key_spki_base64="test-only",
            public_key_sha256="sha256:" + "a" * 64,
            session_identity="provider-issuer-session:sha256:" + "b" * 64,
            public_key_spki=b"test-only",
        )
        attempt = HostAuthenticatedReadAttempt(
            issuer_session_identity=session.session_identity,
            subject=subject,
            read_generation=1,
            read_attempt_id=attempt_id,
            prepared_at_utc="2026-09-24T20:00:01.0000000Z",
            binding_sha256="sha256:" + "c" * 64,
            signature_base64="test-only",
        )
        prepared = VerifiedHostPreparedAttestation(
            issuer_session=session,
            attempt=attempt,
            query=query_binding.query,
        )
        response_sha = "sha256:" + sha256(response).hexdigest()
        receipt = HostAuthenticatedReadReceipt(
            issuer_session_identity=session.session_identity,
            read_attempt_binding_sha256=attempt.binding_sha256,
            read_attempt_id=attempt_id,
            read_generation=1,
            http_status=200,
            response_sha256=response_sha,
            response_length=len(response),
            observed_at_utc="2026-09-24T20:00:02.0000000Z",
            receipt_sha256="sha256:" + "d" * 64,
            signature_base64="test-only",
        )
        events = JournalStore.load_events(
            store,
            "authenticated_provider_read",
            attempt_id,
        )
        self.assertEqual(len(events), 2)
        prepared_event, observed_event = events
        journal_identity = canonical_provider_origin_journal_identity(store)

        prepared_receipt_identity = host_attestation._content_identity(
            "provider-read-durable-prepared",
            host_attestation.canonical_host_material(
                host_attestation._PREPARED_DURABILITY_SCHEMA,
                session.session_identity,
                attempt_id,
                attempt.binding_sha256,
                snapshot["query_digest"],
                journal_identity,
                prepared_event["event_id"],
                str(prepared_event["journal_sequence"]),
                "2026-09-24T20:00:01.0000000Z",
            ),
        )
        prepared_durability = HostPreparedDurabilityReceipt(
            issuer_session_identity=session.session_identity,
            read_attempt_id=attempt_id,
            read_attempt_binding_sha256=attempt.binding_sha256,
            query_digest=snapshot["query_digest"],
            journal_identity=journal_identity,
            prepared_event_id=prepared_event["event_id"],
            journal_sequence=prepared_event["journal_sequence"],
            committed_at_utc="2026-09-24T20:00:01.0000000Z",
            receipt_identity=prepared_receipt_identity,
        )
        observed_receipt_identity = host_attestation._content_identity(
            "provider-read-durable-observed",
            host_attestation.canonical_host_material(
                host_attestation._OBSERVED_DURABILITY_SCHEMA,
                session.session_identity,
                attempt_id,
                attempt.binding_sha256,
                receipt.receipt_sha256,
                response_sha,
                "200",
                receipt.observed_at_utc,
                journal_identity,
                prepared_receipt_identity,
                prepared_event["event_id"],
                str(prepared_event["journal_sequence"]),
                observed_event["event_id"],
                str(observed_event["journal_sequence"]),
                "2026-09-24T20:00:02.0000000Z",
            ),
        )
        observed_durability = HostObservedDurabilityReceipt(
            issuer_session_identity=session.session_identity,
            read_attempt_id=attempt_id,
            read_attempt_binding_sha256=attempt.binding_sha256,
            provider_receipt_sha256=receipt.receipt_sha256,
            response_sha256=response_sha,
            http_status=200,
            observed_at_utc=receipt.observed_at_utc,
            journal_identity=journal_identity,
            prepared_receipt_identity=prepared_receipt_identity,
            prepared_event_id=prepared_event["event_id"],
            prepared_journal_sequence=prepared_event["journal_sequence"],
            observed_event_id=observed_event["event_id"],
            observed_journal_sequence=observed_event["journal_sequence"],
            committed_at_utc="2026-09-24T20:00:02.0000000Z",
            receipt_identity=observed_receipt_identity,
        )
        return VerifiedHostObservedAttestation(
            prepared=prepared,
            receipt=receipt,
            response_bytes=response,
            prepared_durability=prepared_durability,
            observed_durability=observed_durability,
        )

    def test_journal_identity_is_stable_for_same_reopened_backing_generation(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            first = JournalStore(path)
            first_identity = canonical_provider_origin_journal_identity(first)
            second = JournalStore(path)
            second_identity = canonical_provider_origin_journal_identity(second)
            self.assertEqual(first_identity, second_identity)
            self.assertTrue(first_identity.startswith("sha256:"))
            self.assertEqual(len(first_identity), 71)

    def test_bybit_rule_identity_is_derived_from_existing_endpoint_registry(self):
        binding = self._query_binding()
        identity, rule = canonical_bybit_authenticated_read_rule_identity(binding)
        self.assertEqual(rule.permission_scope, "ORDER.READ")
        self.assertEqual(rule.data_entitlement, "EXECUTIONS")
        self.assertTrue(identity.startswith("sha256:"))
        self.assertEqual(len(identity), 71)

    def test_matching_signed_shape_requires_actual_matching_durable_rows(self):
        query = self._query_binding()
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            attempt_id = "provider-read:" + "1" * 32
            response = self._seed_provider_origin_rows(
                store,
                query,
                attempt_id=attempt_id,
            )
            verified = self._verified_observed(
                store,
                query,
                attempt_id=attempt_id,
                response=response,
            )

            result = bridge_module._verify_bybit_host_observed_against_journal_impl(
                {},
                query_binding=query,
                store=store,
                pins=self._pins(),
                expected_session_identity=verified.prepared.issuer_session.session_identity,
                expected_public_key_sha256=verified.prepared.issuer_session.public_key_sha256,
                verify_observed=lambda *_args, **_kwargs: verified,
            )
            self.assertIs(result, verified)

    def test_content_addressed_durability_receipt_without_rows_fails_closed(self):
        query = self._query_binding()
        with TemporaryDirectory() as directory:
            seeded = JournalStore(f"{directory}/seeded.sqlite3")
            attempt_id = "provider-read:" + "2" * 32
            response = self._seed_provider_origin_rows(
                seeded,
                query,
                attempt_id=attempt_id,
            )
            verified = self._verified_observed(
                seeded,
                query,
                attempt_id=attempt_id,
                response=response,
            )
            empty_store = JournalStore(f"{directory}/empty.sqlite3")
            with self.assertRaisesRegex(
                ProviderOriginHostBridgeError,
                "do not reference exact Prepared/Observed journal rows",
            ):
                bridge_module._verify_bybit_host_observed_against_journal_impl(
                    {},
                    query_binding=query,
                    store=empty_store,
                    pins=self._pins(),
                    expected_session_identity=verified.prepared.issuer_session.session_identity,
                    expected_public_key_sha256=verified.prepared.issuer_session.public_key_sha256,
                    verify_observed=lambda *_args, **_kwargs: verified,
                )

    def test_subject_entitlement_cannot_relabel_canonical_bybit_rule(self):
        query = self._query_binding()
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            attempt_id = "provider-read:" + "3" * 32
            response = self._seed_provider_origin_rows(
                store,
                query,
                attempt_id=attempt_id,
            )
            verified = self._verified_observed(
                store,
                query,
                attempt_id=attempt_id,
                response=response,
            )
            wrong_subject = replace(
                verified.prepared.attempt.subject,
                data_entitlement="BALANCES",
            )
            wrong_attempt = replace(
                verified.prepared.attempt,
                subject=wrong_subject,
            )
            wrong_prepared = VerifiedHostPreparedAttestation(
                issuer_session=verified.prepared.issuer_session,
                attempt=wrong_attempt,
                query=verified.prepared.query,
            )
            wrong = VerifiedHostObservedAttestation(
                prepared=wrong_prepared,
                receipt=verified.receipt,
                response_bytes=verified.response_bytes,
                prepared_durability=verified.prepared_durability,
                observed_durability=verified.observed_durability,
            )
            with self.assertRaisesRegex(
                ProviderOriginHostBridgeError,
                "subject mismatch: data_entitlement",
            ):
                bridge_module._verify_bybit_host_observed_against_journal_impl(
                    {},
                    query_binding=query,
                    store=store,
                    pins=self._pins(),
                    expected_session_identity=wrong.prepared.issuer_session.session_identity,
                    expected_public_key_sha256=wrong.prepared.issuer_session.public_key_sha256,
                    verify_observed=lambda *_args, **_kwargs: wrong,
                )

    def test_response_bytes_must_match_actual_durable_observed_payload(self):
        query = self._query_binding()
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            attempt_id = "provider-read:" + "4" * 32
            response = self._seed_provider_origin_rows(
                store,
                query,
                attempt_id=attempt_id,
            )
            verified = self._verified_observed(
                store,
                query,
                attempt_id=attempt_id,
                response=response,
            )
            changed = VerifiedHostObservedAttestation(
                prepared=verified.prepared,
                receipt=verified.receipt,
                response_bytes=b'{"retCode":0,"result":{"list":[1]}}',
                prepared_durability=verified.prepared_durability,
                observed_durability=verified.observed_durability,
            )
            with self.assertRaisesRegex(
                ProviderOriginHostBridgeError,
                "signed response bytes differ",
            ):
                bridge_module._verify_bybit_host_observed_against_journal_impl(
                    {},
                    query_binding=query,
                    store=store,
                    pins=self._pins(),
                    expected_session_identity=changed.prepared.issuer_session.session_identity,
                    expected_public_key_sha256=changed.prepared.issuer_session.public_key_sha256,
                    verify_observed=lambda *_args, **_kwargs: changed,
                )

    def test_public_observed_bridge_retains_captured_crypto_verifier(self):
        query = self._query_binding()
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            original = bridge_module.verify_host_observed_attestation
            calls = []
            bridge_module.verify_host_observed_attestation = (
                lambda *_args, **_kwargs: calls.append("hostile")
            )
            try:
                with self.assertRaises(ProviderOriginHostBridgeError):
                    verify_bybit_host_observed_against_canonical_journal(
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
                bridge_module.verify_host_observed_attestation = original
            self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
