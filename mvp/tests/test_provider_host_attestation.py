from __future__ import annotations

import base64
from copy import deepcopy
from hashlib import sha256
import sys
import unittest

import mvp.autotrade_mvp.provider_host_attestation as attestation
from mvp.autotrade_mvp.provider_host_attestation import (
    HostProviderAttestationError,
    HostProviderAttestationUnavailable,
    canonical_host_material,
    verify_host_observed_attestation,
    verify_host_prepared_attestation,
    verify_host_sender_fence_attestation,
)


_P = int(
    "FFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFF",
    16,
)
_A = _P - 3
_N = int(
    "FFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551",
    16,
)
_G = (
    int(
        "6B17D1F2E12C4247F8BCE6E563A440F277037D812DEB33A0F4A13945D898C296",
        16,
    ),
    int(
        "4FE342E2FE1A7F9B8EE7EB4A7C0F9E162BCE33576B315ECECBB6406837BF51F5",
        16,
    ),
)


def _add(left, right):
    if left is None:
        return right
    if right is None:
        return left
    x1, y1 = left
    x2, y2 = right
    if x1 == x2 and (y1 + y2) % _P == 0:
        return None
    if left == right:
        slope = ((3 * x1 * x1 + _A) * pow(2 * y1, -1, _P)) % _P
    else:
        slope = ((y2 - y1) * pow(x2 - x1, -1, _P)) % _P
    x3 = (slope * slope - x1 - x2) % _P
    return x3, (slope * (x1 - x3) - y1) % _P


def _multiply(scalar, point):
    result = None
    current = point
    while scalar:
        if scalar & 1:
            result = _add(result, current)
        current = _add(current, current)
        scalar >>= 1
    return result


def _sign(material: bytes, *, nonce: int) -> str:
    # Test-only deterministic ECDSA with private scalar d=1.  Production signing
    # remains exclusively in AutoTrade.Host and never enters Python.
    x, _y = _multiply(nonce, _G)
    r = x % _N
    z = int.from_bytes(sha256(material).digest(), "big")
    s = (pow(nonce, -1, _N) * (z + r)) % _N
    if r == 0 or s == 0:
        raise AssertionError("invalid test nonce")
    return base64.b64encode(
        r.to_bytes(32, "big") + s.to_bytes(32, "big")
    ).decode("ascii")


def _fixture():
    spki = (
        attestation._P256_SPKI_PREFIX
        + _G[0].to_bytes(32, "big")
        + _G[1].to_bytes(32, "big")
    )
    spki_b64 = base64.b64encode(spki).decode("ascii")
    key_sha = "sha256:" + sha256(spki).hexdigest()
    started = "2026-10-02T11:00:00.0000000Z"
    issuer_id = "provider-issuer:11111111111111111111111111111111"
    session_material = canonical_host_material(
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
    subject = {
        "provider_id": "BYBIT",
        "account_id": "acct-1",
        "entity_id": "entity-1",
        "runtime_environment": "PAPER",
        "provider_environment": "TESTNET",
        "endpoint": "/v5/account/wallet-balance",
        "surface": "AUTHENTICATED_READ",
        "permission_scope": "ACCOUNT_READ",
        "data_entitlement": "ACCOUNT_BALANCES",
        "instrument_version": "instrument-version-1",
        "query_digest": "sha256:" + "1" * 64,
        "endpoint_rule_identity": "sha256:" + "2" * 64,
        "credential_handle_id": "credential-handle-1",
        "credential_generation": 7,
        "capability_id": "capability-1",
        "qualification_id": "qualification-1",
        "qualification_build_id": "qualification-build-1",
        "adapter_build_identity": "adapter-build-1",
        "network_policy_identity": "sha256:" + "3" * 64,
        "transport_identity": "provider-transport:https-v1",
    }
    generation = 1
    attempt_id = "provider-read:" + "a" * 32
    prepared = "2026-10-02T11:00:00.1000000Z"
    attempt_material = canonical_host_material(
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
        "query": {"category": "linear", "symbol": "BTCUSDT"},
    }

    response = b'{"retCode":0,"result":{"coin":[]}}'
    observed = "2026-10-02T11:00:00.2000000Z"
    receipt_fields = (
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
    receipt_material = canonical_host_material(*receipt_fields)
    receipt = {
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
    journal_identity = "sha256:" + "4" * 64
    prepared_committed = "2026-10-02T11:00:00.1500000Z"
    prepared_receipt_identity = attestation._content_identity(
        "provider-read-durable-prepared",
        canonical_host_material(
            attestation._PREPARED_DURABILITY_SCHEMA,
            session_identity,
            attempt_id,
            attempt["binding_sha256"],
            subject["query_digest"],
            journal_identity,
            attempt_id + ":prepared",
            "1",
            prepared_committed,
        ),
    )
    durable_prepared = {
        "schema": attestation._PREPARED_DURABILITY_SCHEMA,
        "issuer_session_identity": session_identity,
        "read_attempt_id": attempt_id,
        "read_attempt_binding_sha256": attempt["binding_sha256"],
        "query_digest": subject["query_digest"],
        "journal_identity": journal_identity,
        "prepared_event_id": attempt_id + ":prepared",
        "journal_sequence": 1,
        "committed_at_utc": prepared_committed,
        "receipt_identity": prepared_receipt_identity,
    }
    observed_committed = "2026-10-02T11:00:00.2500000Z"
    observed_receipt_identity = attestation._content_identity(
        "provider-read-durable-observed",
        canonical_host_material(
            attestation._OBSERVED_DURABILITY_SCHEMA,
            session_identity,
            attempt_id,
            attempt["binding_sha256"],
            receipt["receipt_sha256"],
            receipt["response_sha256"],
            "200",
            observed,
            journal_identity,
            prepared_receipt_identity,
            attempt_id + ":prepared",
            "1",
            attempt_id + ":observed",
            "2",
            observed_committed,
        ),
    )
    durable_observed = {
        "schema": attestation._OBSERVED_DURABILITY_SCHEMA,
        "issuer_session_identity": session_identity,
        "read_attempt_id": attempt_id,
        "read_attempt_binding_sha256": attempt["binding_sha256"],
        "provider_receipt_sha256": receipt["receipt_sha256"],
        "response_sha256": receipt["response_sha256"],
        "http_status": 200,
        "observed_at_utc": observed,
        "journal_identity": journal_identity,
        "prepared_receipt_identity": prepared_receipt_identity,
        "prepared_event_id": attempt_id + ":prepared",
        "prepared_journal_sequence": 1,
        "observed_event_id": attempt_id + ":observed",
        "observed_journal_sequence": 2,
        "committed_at_utc": observed_committed,
        "receipt_identity": observed_receipt_identity,
    }
    observed_envelope = {
        "schema": attestation._OBSERVED_ENVELOPE_SCHEMA,
        "issuer_session": session,
        "attempt": attempt,
        "receipt": receipt,
        "query": dict(prepared_envelope["query"]),
        "response_base64": base64.b64encode(response).decode("ascii"),
        "durable_prepared": durable_prepared,
        "durable_observed": durable_observed,
    }
    return (
        prepared_envelope,
        observed_envelope,
        session_identity,
        key_sha,
        response,
    )



def _fence_fixture():
    prepared, _observed, session_id, key_sha, _response = _fixture()
    session = deepcopy(prepared["issuer_session"])
    runtime = "PAPER"
    account = "paper-account"
    owner_scope = runtime + ":" + account
    lease_scope_id = "hf-" + sha256(
        (
            attestation._HOST_LIFETIME_FENCE_DOMAIN
            + "\0"
            + "BYBIT"
            + "\0"
            + "TESTNET"
            + "\0"
            + runtime
            + "\0"
            + account
        ).encode("utf-8")
    ).hexdigest()
    lease_acquired = "2026-10-02T11:00:00.3000000Z"
    fenced_at = "2026-10-02T11:00:00.4000000Z"
    receipt_fields = (
        attestation._HOST_SENDER_FENCE_SCHEMA,
        session_id,
        attestation._HOST_SENDER_FENCE_METHOD,
        lease_scope_id,
        "sha256:" + "4" * 64,
        lease_acquired,
        owner_scope,
        "BYBIT",
        account,
        runtime,
        "TESTNET",
        "credential-handle-7",
        "7",
        "sha256:" + "5" * 64,
        "source-owner",
        "1",
        "restored-owner",
        "2",
        fenced_at,
    )
    material = canonical_host_material(*receipt_fields)
    receipt = {
        "schema": attestation._HOST_SENDER_FENCE_SCHEMA,
        "issuer_session_identity": session_id,
        "fence_method": attestation._HOST_SENDER_FENCE_METHOD,
        "lease_scope_id": lease_scope_id,
        "lease_owner_record_sha256": "sha256:" + "4" * 64,
        "lease_acquired_at_utc": lease_acquired,
        "owner_scope": owner_scope,
        "provider_id": "BYBIT",
        "account_id": account,
        "runtime_environment": runtime,
        "provider_environment": "TESTNET",
        "credential_handle_id": "credential-handle-7",
        "credential_generation": 7,
        "backup_manifest_sha256": "sha256:" + "5" * 64,
        "old_owner_id": "source-owner",
        "old_owner_epoch": 1,
        "new_owner_id": "restored-owner",
        "new_owner_epoch": 2,
        "fenced_at_utc": fenced_at,
        "receipt_sha256": "sha256:" + sha256(material).hexdigest(),
        "signature_base64": _sign(material, nonce=5),
    }
    envelope = {
        "schema": attestation._HOST_SENDER_FENCE_ENVELOPE_SCHEMA,
        "issuer_session": session,
        "receipt": receipt,
    }
    expected = {
        "expected_session_identity": session_id,
        "expected_public_key_sha256": key_sha,
        "expected_backup_manifest_sha256": receipt["backup_manifest_sha256"],
        "expected_owner_scope": owner_scope,
        "expected_provider_id": "BYBIT",
        "expected_account_id": account,
        "expected_runtime_environment": runtime,
        "expected_provider_environment": "TESTNET",
        "expected_credential_handle_id": "credential-handle-7",
        "expected_credential_generation": 7,
        "expected_old_owner_id": "source-owner",
        "expected_old_owner_epoch": 1,
        "expected_new_owner_id": "restored-owner",
        "expected_new_owner_epoch": 2,
    }
    return envelope, expected


class ProviderHostAttestationTests(unittest.TestCase):
    def test_self_describing_session_cannot_replace_independent_pin(self):
        prepared, _observed, session_id, key_sha, _response = _fixture()
        with self.assertRaisesRegex(
            HostProviderAttestationError,
            "independently pinned",
        ):
            verify_host_prepared_attestation(
                prepared,
                expected_session_identity=session_id,
                expected_public_key_sha256="sha256:" + "f" * 64,
                expected_query=prepared["query"],
                expected_journal_identity=observed["durable_prepared"]["journal_identity"],
            )

    def test_attempt_binding_tamper_fails_before_platform_crypto(self):
        prepared, _observed, session_id, key_sha, _response = _fixture()
        changed = deepcopy(prepared)
        changed["attempt"]["subject"]["account_id"] = "attacker-account"
        with self.assertRaisesRegex(
            HostProviderAttestationError,
            "binding digest",
        ):
            verify_host_prepared_attestation(
                changed,
                expected_session_identity=session_id,
                expected_public_key_sha256=key_sha,
                expected_query=prepared["query"],
                expected_journal_identity=observed["durable_prepared"]["journal_identity"],
            )

    def test_noncanonical_or_extra_fields_are_rejected(self):
        prepared, _observed, session_id, key_sha, _response = _fixture()
        changed = deepcopy(prepared)
        changed["attempt"]["caller_authorized"] = True
        with self.assertRaisesRegex(
            HostProviderAttestationError,
            "non-canonical shape",
        ):
            verify_host_prepared_attestation(
                changed,
                expected_session_identity=session_id,
                expected_public_key_sha256=key_sha,
                expected_query=prepared["query"],
                expected_journal_identity=observed["durable_prepared"]["journal_identity"],
            )

    def test_serialized_query_cannot_replace_independently_pinned_query(self):
        prepared, _observed, session_id, key_sha, _response = _fixture()
        changed = deepcopy(prepared)
        changed["query"]["symbol"] = "ETHUSDT"
        with self.assertRaisesRegex(
            HostProviderAttestationError,
            "independently pinned query",
        ):
            verify_host_prepared_attestation(
                changed,
                expected_session_identity=session_id,
                expected_public_key_sha256=key_sha,
                expected_query=prepared["query"],
                expected_journal_identity=observed["durable_prepared"]["journal_identity"],
            )

    def test_sender_fence_transition_must_match_independent_restore_context(self):
        envelope, expected = _fence_fixture()
        changed = deepcopy(envelope)
        changed["receipt"]["provider_environment"] = "DEMO"
        with self.assertRaisesRegex(
            HostProviderAttestationError,
            "independently resolved transition",
        ):
            verify_host_sender_fence_attestation(changed, **expected)

    def test_sender_fence_lease_scope_is_recomputed_from_account_runtime(self):
        envelope, expected = _fence_fixture()
        changed = deepcopy(envelope)
        changed["receipt"]["lease_scope_id"] = "hf-" + "f" * 64
        with self.assertRaisesRegex(
            HostProviderAttestationError,
            "lease identity is inconsistent",
        ):
            verify_host_sender_fence_attestation(changed, **expected)

    def test_sender_fence_requires_consecutive_owner_epoch(self):
        envelope, expected = _fence_fixture()
        changed = deepcopy(envelope)
        changed["receipt"]["new_owner_epoch"] = 3
        changed_expected = dict(expected)
        changed_expected["expected_new_owner_epoch"] = 3
        with self.assertRaisesRegex(
            HostProviderAttestationError,
            "expected sender-fence owner transition is not consecutive",
        ):
            verify_host_sender_fence_attestation(changed, **changed_expected)

    def test_sender_fence_self_describing_session_cannot_replace_pin(self):
        envelope, expected = _fence_fixture()
        changed_expected = dict(expected)
        changed_expected["expected_public_key_sha256"] = "sha256:" + "f" * 64
        with self.assertRaisesRegex(
            HostProviderAttestationError,
            "independently pinned authority",
        ):
            verify_host_sender_fence_attestation(envelope, **changed_expected)

    @unittest.skipIf(sys.platform == "win32", "non-Windows fail-closed contract")
    def test_non_windows_sender_fence_never_grants_host_signature_authority(self):
        envelope, expected = _fence_fixture()
        with self.assertRaisesRegex(
            HostProviderAttestationUnavailable,
            "Windows CNG",
        ):
            verify_host_sender_fence_attestation(envelope, **expected)

    @unittest.skipIf(sys.platform == "win32", "non-Windows fail-closed contract")
    def test_non_windows_never_grants_host_signature_authority(self):
        prepared, _observed, session_id, key_sha, _response = _fixture()
        with self.assertRaisesRegex(
            HostProviderAttestationUnavailable,
            "Windows CNG",
        ):
            verify_host_prepared_attestation(
                prepared,
                expected_session_identity=session_id,
                expected_public_key_sha256=key_sha,
                expected_query=prepared["query"],
                expected_journal_identity=observed["durable_prepared"]["journal_identity"],
            )

    @unittest.skipUnless(sys.platform == "win32", "requires Windows CNG")
    def test_windows_cng_verifies_host_compatible_attempt_and_receipt(self):
        prepared, observed, session_id, key_sha, response = _fixture()
        verified_prepared = verify_host_prepared_attestation(
            prepared,
            expected_session_identity=session_id,
            expected_public_key_sha256=key_sha,
            expected_query=prepared["query"],
        )
        self.assertEqual(verified_prepared.attempt.subject.provider_id, "BYBIT")
        self.assertEqual(verified_prepared.attempt.subject.credential_generation, 7)
        self.assertEqual(
            dict(verified_prepared.query),
            {"category": "linear", "symbol": "BTCUSDT"},
        )

        verified_observed = verify_host_observed_attestation(
            observed,
            expected_session_identity=session_id,
            expected_public_key_sha256=key_sha,
            expected_query=observed["query"],
            expected_journal_identity=observed["durable_prepared"]["journal_identity"],
        )
        self.assertEqual(verified_observed.response_bytes, response)
        self.assertEqual(verified_observed.receipt.http_status, 200)
        self.assertEqual(
            verified_observed.receipt.read_attempt_binding_sha256,
            verified_observed.prepared.attempt.binding_sha256,
        )
        self.assertEqual(
            verified_observed.prepared_durability.journal_identity,
            observed["durable_prepared"]["journal_identity"],
        )
        self.assertEqual(
            verified_observed.observed_durability.prepared_receipt_identity,
            verified_observed.prepared_durability.receipt_identity,
        )

        wrong_journal = "sha256:" + "5" * 64
        with self.assertRaisesRegex(
            HostProviderAttestationError,
            "durable Prepared receipt conflicts",
        ):
            verify_host_observed_attestation(
                observed,
                expected_session_identity=session_id,
                expected_public_key_sha256=key_sha,
                expected_query=observed["query"],
                expected_journal_identity=wrong_journal,
            )

    @unittest.skipUnless(sys.platform == "win32", "requires Windows CNG")
    def test_windows_cng_verifies_same_host_sender_fence(self):
        envelope, expected = _fence_fixture()
        verified = verify_host_sender_fence_attestation(envelope, **expected)
        self.assertEqual(
            verified.receipt.fence_method,
            "SAME_HOST_EXCLUSIVE_LEASE_HANDOFF",
        )
        self.assertEqual(verified.receipt.provider_environment, "TESTNET")
        self.assertEqual(verified.receipt.credential_generation, 7)
        self.assertEqual(verified.receipt.new_owner_epoch, 2)

    @unittest.skipUnless(sys.platform == "win32", "requires Windows CNG")
    def test_windows_cng_rejects_signed_response_byte_tamper(self):
        prepared, observed, session_id, key_sha, _response = _fixture()
        changed = deepcopy(observed)
        changed["response_base64"] = base64.b64encode(
            b'{"retCode":0,"result":{"coin":[1]}}'
        ).decode("ascii")
        with self.assertRaisesRegex(
            HostProviderAttestationError,
            "exact response bytes",
        ):
            verify_host_observed_attestation(
                changed,
                expected_session_identity=session_id,
                expected_public_key_sha256=key_sha,
                expected_query=prepared["query"],
                expected_journal_identity=observed["durable_prepared"]["journal_identity"],
            )


if __name__ == "__main__":
    unittest.main()
