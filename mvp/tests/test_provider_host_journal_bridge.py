from __future__ import annotations

import base64
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import mvp.autotrade_mvp.provider_host_attestation as attestation
import mvp.autotrade_mvp.provider_origin as provider_origin
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.provider_origin_journal_identity import (
    canonical_provider_origin_journal_identity,
)
from mvp.autotrade_mvp.provider_origin import (
    HostAuthenticatedReadExpectedScope,
    HostAuthenticatedReadJournalBridge,
    ProviderOriginError,
)
from mvp.tests.test_provider_host_attestation import _G, _sign
from mvp.tests.test_provider_transport import authenticated_read_binding


_PREPARED_COMMIT = datetime(
    2026, 9, 25, 10, 0, 0, 150000, tzinfo=timezone.utc
)
_OBSERVED_COMMIT = datetime(
    2026, 9, 25, 10, 0, 0, 250000, tzinfo=timezone.utc
)


def _fixture(query):
    spki = (
        attestation._P256_SPKI_PREFIX
        + _G[0].to_bytes(32, "big")
        + _G[1].to_bytes(32, "big")
    )
    spki_b64 = base64.b64encode(spki).decode("ascii")
    key_sha = "sha256:" + sha256(spki).hexdigest()
    started = "2026-09-25T09:59:59.0000000Z"
    issuer_id = "provider-issuer:11111111111111111111111111111111"
    session_material = attestation.canonical_host_material(
        attestation._SESSION_SCHEMA,
        issuer_id,
        started,
        spki_b64,
        key_sha,
    )
    session_identity = (
        "provider-issuer-session:sha256:"
        + sha256(session_material).hexdigest()
    )
    session = {
        "schema": attestation._SESSION_SCHEMA,
        "issuer_instance_id": issuer_id,
        "started_at_utc": started,
        "public_key_spki_base64": spki_b64,
        "public_key_sha256": key_sha,
        "session_identity": session_identity,
    }

    pins = HostAuthenticatedReadExpectedScope(
        data_entitlement="ACCOUNT",
        endpoint_rule_identity="sha256:" + "2" * 64,
        credential_handle_id="credential-handle-1",
        credential_generation=7,
        qualification_id="qualification-1",
        qualification_build_id="qualification-build-1",
        adapter_build_identity="adapter-build-1",
        network_policy_identity="sha256:" + "3" * 64,
        transport_identity="provider-transport:https-v1",
    )
    subject = {
        "provider_id": query.provider_id,
        "account_id": query.account_id,
        "entity_id": query.entity_id,
        "runtime_environment": query.environment,
        "provider_environment": query.provider_environment,
        "endpoint": query.endpoint,
        "surface": query.surface.value,
        "permission_scope": query.permission_scope,
        "data_entitlement": pins.data_entitlement,
        "instrument_version": query.instrument_version,
        "query_digest": query.query_digest,
        "endpoint_rule_identity": pins.endpoint_rule_identity,
        "credential_handle_id": pins.credential_handle_id,
        "credential_generation": pins.credential_generation,
        "capability_id": query.capability_snapshot_id,
        "qualification_id": pins.qualification_id,
        "qualification_build_id": pins.qualification_build_id,
        "adapter_build_identity": pins.adapter_build_identity,
        "network_policy_identity": pins.network_policy_identity,
        "transport_identity": pins.transport_identity,
    }
    generation = 1
    attempt_id = "provider-read:" + "a" * 32
    prepared = "2026-09-25T10:00:00.1000000Z"
    attempt_material = attestation.canonical_host_material(
        attestation._READ_ATTEMPT_SCHEMA,
        session_identity,
        subject["provider_id"],
        subject["account_id"],
        subject["entity_id"],
        subject["runtime_environment"],
        subject["provider_environment"],
        subject["endpoint"],
        subject["surface"],
        subject["permission_scope"],
        subject["data_entitlement"],
        subject["instrument_version"],
        subject["query_digest"],
        subject["endpoint_rule_identity"],
        subject["credential_handle_id"],
        str(subject["credential_generation"]),
        subject["capability_id"],
        subject["qualification_id"],
        subject["qualification_build_id"],
        subject["adapter_build_identity"],
        subject["network_policy_identity"],
        subject["transport_identity"],
        str(generation),
        attempt_id,
        prepared,
    )
    attempt = {
        "schema": attestation._READ_ATTEMPT_SCHEMA,
        "issuer_session_identity": session_identity,
        "subject": subject,
        "read_generation": generation,
        "read_attempt_id": attempt_id,
        "prepared_at_utc": prepared,
        "binding_sha256": "sha256:" + sha256(attempt_material).hexdigest(),
        "signature_base64": _sign(attempt_material, nonce=2),
    }
    prepared_envelope = {
        "schema": attestation._PREPARED_ENVELOPE_SCHEMA,
        "issuer_session": session,
        "attempt": attempt,
        "query": dict(query.query),
    }

    response = b'{"balances":[],"ok":true}'
    observed = "2026-09-25T10:00:00.2000000Z"
    receipt_material = attestation.canonical_host_material(
        attestation._READ_RECEIPT_SCHEMA,
        session_identity,
        attempt["binding_sha256"],
        attempt_id,
        str(generation),
        "200",
        "sha256:" + sha256(response).hexdigest(),
        str(len(response)),
        observed,
    )
    provider_receipt = {
        "schema": attestation._READ_RECEIPT_SCHEMA,
        "issuer_session_identity": session_identity,
        "read_attempt_binding_sha256": attempt["binding_sha256"],
        "read_attempt_id": attempt_id,
        "read_generation": generation,
        "http_status": 200,
        "response_sha256": "sha256:" + sha256(response).hexdigest(),
        "response_length": len(response),
        "observed_at_utc": observed,
        "receipt_sha256": "sha256:" + sha256(receipt_material).hexdigest(),
        "signature_base64": _sign(receipt_material, nonce=3),
    }
    return (
        prepared_envelope,
        provider_receipt,
        session_identity,
        key_sha,
        pins,
        response,
    )


class HostAuthenticatedReadJournalBridgeTests(unittest.TestCase):
    def _commit_prepared(self, bridge, query, fixture):
        prepared, _provider_receipt, session_id, key_sha, pins, _response = fixture
        return bridge.commit_prepared(
            prepared,
            query_binding=query,
            expected_session_identity=session_id,
            expected_public_key_sha256=key_sha,
            expected_scope=pins,
            committed_at=_PREPARED_COMMIT,
        )

    def _commit_observed(
        self,
        bridge,
        query,
        fixture,
        prepared_receipt,
        *,
        response_override=None,
    ):
        (
            prepared,
            provider_receipt,
            session_id,
            key_sha,
            pins,
            response,
        ) = fixture
        return bridge.commit_observed(
            prepared,
            provider_receipt,
            provider_origin._host_prepared_receipt_payload(prepared_receipt),
            response if response_override is None else response_override,
            query_binding=query,
            expected_session_identity=session_id,
            expected_public_key_sha256=key_sha,
            expected_scope=pins,
            committed_at=_OBSERVED_COMMIT,
        )

    def test_writer_and_verifier_share_one_canonical_journal_identity(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            bridge = HostAuthenticatedReadJournalBridge(store)
            self.assertEqual(
                bridge.journal_identity,
                canonical_provider_origin_journal_identity(store),
            )

    def test_restart_replays_same_host_attempt_and_canonical_journal_cut(self):
        query = authenticated_read_binding()
        fixture = _fixture(query)
        prepared = fixture[0]
        with TemporaryDirectory() as directory, patch.object(
            attestation,
            "_verify_p256_sha256_p1363",
            return_value=None,
        ):
            path = f"{directory}/journal.sqlite3"
            first_store = JournalStore(path)
            first = HostAuthenticatedReadJournalBridge(first_store)
            prepared_receipt = self._commit_prepared(first, query, fixture)
            self.assertEqual(
                prepared_receipt.read_attempt_id,
                prepared["attempt"]["read_attempt_id"],
            )
            self.assertEqual(prepared_receipt.journal_sequence, 1)
            first_journal_identity = first.journal_identity

            # Simulate process restart by constructing a fresh JournalStore and
            # bridge over the same backing database before Observed.
            restarted_store = JournalStore(path)
            restarted = HostAuthenticatedReadJournalBridge(restarted_store)
            self.assertEqual(restarted.journal_identity, first_journal_identity)
            observed_receipt = self._commit_observed(
                restarted,
                query,
                fixture,
                prepared_receipt,
            )
            self.assertEqual(observed_receipt.prepared_journal_sequence, 1)
            self.assertEqual(observed_receipt.observed_journal_sequence, 2)
            self.assertEqual(
                observed_receipt.journal_identity,
                first_journal_identity,
            )

            replay_store = JournalStore(path)
            replay = HostAuthenticatedReadJournalBridge(replay_store)
            verified = replay.load_verified_observed(
                prepared["attempt"]["read_attempt_id"],
                query_binding=query,
                expected_session_identity=fixture[2],
                expected_public_key_sha256=fixture[3],
                expected_scope=fixture[4],
            )
            self.assertEqual(verified.response_bytes, fixture[5])
            self.assertEqual(verified.receipt.http_status, 200)
            self.assertEqual(
                verified.prepared_durability.receipt_identity,
                prepared_receipt.receipt_identity,
            )
            self.assertEqual(
                verified.observed_durability.receipt_identity,
                observed_receipt.receipt_identity,
            )

    def test_prepared_retry_is_idempotent_but_observed_attempt_cannot_reenter(self):
        query = authenticated_read_binding()
        fixture = _fixture(query)
        with TemporaryDirectory() as directory, patch.object(
            attestation,
            "_verify_p256_sha256_p1363",
            return_value=None,
        ):
            store = JournalStore(f"{directory}/journal.sqlite3")
            bridge = HostAuthenticatedReadJournalBridge(store)
            first = self._commit_prepared(bridge, query, fixture)
            second = self._commit_prepared(bridge, query, fixture)
            self.assertEqual(second, first)

            first_observed = self._commit_observed(
                bridge,
                query,
                fixture,
                first,
            )
            second_observed = self._commit_observed(
                bridge,
                query,
                fixture,
                first,
            )
            self.assertEqual(second_observed, first_observed)

            with self.assertRaisesRegex(
                ProviderOriginError,
                "already Observed",
            ):
                self._commit_prepared(bridge, query, fixture)

    def test_wrong_independent_host_key_pin_is_zero_mutation(self):
        query = authenticated_read_binding()
        fixture = _fixture(query)
        prepared = fixture[0]
        with TemporaryDirectory() as directory, patch.object(
            attestation,
            "_verify_p256_sha256_p1363",
            return_value=None,
        ):
            store = JournalStore(f"{directory}/journal.sqlite3")
            bridge = HostAuthenticatedReadJournalBridge(store)
            with self.assertRaisesRegex(
                ProviderOriginError,
                "Prepared attestation verification failed",
            ):
                bridge.commit_prepared(
                    prepared,
                    query_binding=query,
                    expected_session_identity=fixture[2],
                    expected_public_key_sha256="sha256:" + "f" * 64,
                    expected_scope=fixture[4],
                    committed_at=_PREPARED_COMMIT,
                )
            self.assertEqual(
                JournalStore.load_events(
                    store,
                    "authenticated_provider_read",
                    prepared["attempt"]["read_attempt_id"],
                ),
                [],
            )

    def test_signed_subject_scope_mismatch_is_zero_mutation(self):
        query = authenticated_read_binding()
        fixture = _fixture(query)
        pins = fixture[4]
        changed = HostAuthenticatedReadExpectedScope(
            data_entitlement=pins.data_entitlement,
            endpoint_rule_identity=pins.endpoint_rule_identity,
            credential_handle_id=pins.credential_handle_id,
            credential_generation=pins.credential_generation,
            qualification_id=pins.qualification_id,
            qualification_build_id=pins.qualification_build_id,
            adapter_build_identity=pins.adapter_build_identity,
            network_policy_identity=pins.network_policy_identity,
            transport_identity="provider-transport:https-v2",
        )
        with TemporaryDirectory() as directory, patch.object(
            attestation,
            "_verify_p256_sha256_p1363",
            return_value=None,
        ):
            store = JournalStore(f"{directory}/journal.sqlite3")
            bridge = HostAuthenticatedReadJournalBridge(store)
            with self.assertRaisesRegex(
                ProviderOriginError,
                "conflicts with independently selected scope",
            ):
                bridge.commit_prepared(
                    fixture[0],
                    query_binding=query,
                    expected_session_identity=fixture[2],
                    expected_public_key_sha256=fixture[3],
                    expected_scope=changed,
                    committed_at=_PREPARED_COMMIT,
                )
            self.assertEqual(
                JournalStore.load_events(
                    store,
                    "authenticated_provider_read",
                    fixture[0]["attempt"]["read_attempt_id"],
                ),
                [],
            )

    def test_verified_prepared_input_is_detached_before_journal_write(self):
        query = authenticated_read_binding()
        fixture = _fixture(query)
        mutable_prepared = deepcopy(fixture[0])
        canonical_prepared = deepcopy(fixture[0])
        with TemporaryDirectory() as directory, patch.object(
            attestation,
            "_verify_p256_sha256_p1363",
            return_value=None,
        ):
            verified = attestation.verify_host_prepared_attestation(
                canonical_prepared,
                expected_session_identity=fixture[2],
                expected_public_key_sha256=fixture[3],
                expected_query=canonical_prepared["query"],
            )

            def mutate_after_verification(value, **_kwargs):
                value["attempt"]["subject"]["transport_identity"] = (
                    "provider-transport:attacker"
                )
                return verified

            store = JournalStore(f"{directory}/journal.sqlite3")
            bridge = HostAuthenticatedReadJournalBridge(store)
            with patch.object(
                attestation,
                "verify_host_prepared_attestation",
                side_effect=mutate_after_verification,
            ):
                bridge.commit_prepared(
                    mutable_prepared,
                    query_binding=query,
                    expected_session_identity=fixture[2],
                    expected_public_key_sha256=fixture[3],
                    expected_scope=fixture[4],
                    committed_at=_PREPARED_COMMIT,
                )

            events = JournalStore.load_events(
                store,
                "authenticated_provider_read",
                canonical_prepared["attempt"]["read_attempt_id"],
            )
            stored = events[0]["payload"]["host_prepared_attestation"]
            self.assertEqual(
                stored["attempt"]["subject"]["transport_identity"],
                fixture[4].transport_identity,
            )
            self.assertEqual(stored, canonical_prepared)
            self.assertNotEqual(stored, mutable_prepared)

    def test_verified_provider_receipt_is_detached_before_observed_write(self):
        query = authenticated_read_binding()
        fixture = _fixture(query)
        mutable_receipt = deepcopy(fixture[1])
        canonical_receipt = deepcopy(fixture[1])
        with TemporaryDirectory() as directory, patch.object(
            attestation,
            "_verify_p256_sha256_p1363",
            return_value=None,
        ):
            store = JournalStore(f"{directory}/journal.sqlite3")
            bridge = HostAuthenticatedReadJournalBridge(store)
            prepared_receipt = self._commit_prepared(bridge, query, fixture)
            verified_prepared = attestation.verify_host_prepared_attestation(
                fixture[0],
                expected_session_identity=fixture[2],
                expected_public_key_sha256=fixture[3],
                expected_query=fixture[0]["query"],
            )
            parsed_receipt = attestation._parse_receipt(
                canonical_receipt,
                prepared=verified_prepared,
                response_bytes=fixture[5],
            )

            def mutate_after_parse(value, **_kwargs):
                value["http_status"] = 599
                value["response_sha256"] = "sha256:" + "f" * 64
                return parsed_receipt

            with patch.object(
                attestation,
                "_parse_receipt",
                side_effect=mutate_after_parse,
            ):
                bridge.commit_observed(
                    fixture[0],
                    mutable_receipt,
                    provider_origin._host_prepared_receipt_payload(
                        prepared_receipt
                    ),
                    fixture[5],
                    query_binding=query,
                    expected_session_identity=fixture[2],
                    expected_public_key_sha256=fixture[3],
                    expected_scope=fixture[4],
                    committed_at=_OBSERVED_COMMIT,
                )

            events = JournalStore.load_events(
                store,
                "authenticated_provider_read",
                fixture[0]["attempt"]["read_attempt_id"],
            )
            stored = events[1]["payload"]["host_provider_receipt"]
            self.assertEqual(stored, canonical_receipt)
            self.assertNotEqual(stored, mutable_receipt)

    def test_response_tamper_cannot_append_observed_event(self):
        query = authenticated_read_binding()
        fixture = _fixture(query)
        with TemporaryDirectory() as directory, patch.object(
            attestation,
            "_verify_p256_sha256_p1363",
            return_value=None,
        ):
            store = JournalStore(f"{directory}/journal.sqlite3")
            bridge = HostAuthenticatedReadJournalBridge(store)
            prepared_receipt = self._commit_prepared(bridge, query, fixture)
            with self.assertRaisesRegex(
                ProviderOriginError,
                "Observed attestation component verification failed",
            ):
                self._commit_observed(
                    bridge,
                    query,
                    fixture,
                    prepared_receipt,
                    response_override=b'{"balances":[{"attacker":true}]}',
                )
            events = JournalStore.load_events(
                store,
                "authenticated_provider_read",
                fixture[0]["attempt"]["read_attempt_id"],
            )
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["event_type"], "AuthenticatedReadPrepared")

    def test_prepared_receipt_from_different_journal_is_rejected(self):
        query = authenticated_read_binding()
        fixture = _fixture(query)
        with TemporaryDirectory() as left, TemporaryDirectory() as right, patch.object(
            attestation,
            "_verify_p256_sha256_p1363",
            return_value=None,
        ):
            left_bridge = HostAuthenticatedReadJournalBridge(
                JournalStore(f"{left}/journal.sqlite3")
            )
            prepared_receipt = self._commit_prepared(
                left_bridge,
                query,
                fixture,
            )
            right_store = JournalStore(f"{right}/journal.sqlite3")
            right_bridge = HostAuthenticatedReadJournalBridge(right_store)
            with self.assertRaisesRegex(
                ProviderOriginError,
                "Observed attestation component verification failed",
            ):
                self._commit_observed(
                    right_bridge,
                    query,
                    fixture,
                    prepared_receipt,
                )
            self.assertEqual(
                JournalStore.load_events(
                    right_store,
                    "authenticated_provider_read",
                    fixture[0]["attempt"]["read_attempt_id"],
                ),
                [],
            )

    @unittest.skipIf(sys.platform == "win32", "non-Windows fail-closed contract")
    def test_non_windows_bridge_never_writes_before_cng_verification(self):
        query = authenticated_read_binding()
        fixture = _fixture(query)
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            bridge = HostAuthenticatedReadJournalBridge(store)
            with self.assertRaisesRegex(
                ProviderOriginError,
                "Prepared attestation verification failed",
            ):
                self._commit_prepared(bridge, query, fixture)
            self.assertEqual(
                JournalStore.load_events(
                    store,
                    "authenticated_provider_read",
                    fixture[0]["attempt"]["read_attempt_id"],
                ),
                [],
            )

    @unittest.skipUnless(sys.platform == "win32", "requires Windows CNG")
    def test_windows_cng_bridge_round_trip_without_crypto_patch(self):
        query = authenticated_read_binding()
        fixture = _fixture(query)
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            bridge = HostAuthenticatedReadJournalBridge(JournalStore(path))
            prepared_receipt = self._commit_prepared(
                bridge,
                query,
                fixture,
            )
            observed_receipt = self._commit_observed(
                bridge,
                query,
                fixture,
                prepared_receipt,
            )
            replay = HostAuthenticatedReadJournalBridge(JournalStore(path))
            verified = replay.load_verified_observed(
                fixture[0]["attempt"]["read_attempt_id"],
                query_binding=query,
                expected_session_identity=fixture[2],
                expected_public_key_sha256=fixture[3],
                expected_scope=fixture[4],
            )
            self.assertEqual(verified.response_bytes, fixture[5])
            self.assertEqual(
                verified.observed_durability.receipt_identity,
                observed_receipt.receipt_identity,
            )


if __name__ == "__main__":
    unittest.main()
