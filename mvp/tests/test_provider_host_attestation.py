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
    observed_envelope = {
        "schema": attestation._OBSERVED_ENVELOPE_SCHEMA,
        "issuer_session": session,
        "attempt": attempt,
        "receipt": receipt,
        "query": dict(prepared_envelope["query"]),
        "response_base64": base64.b64encode(response).decode("ascii"),
    }
    return (
        prepared_envelope,
        observed_envelope,
        session_identity,
        key_sha,
        response,
    )


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
            )

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
        )
        self.assertEqual(verified_observed.response_bytes, response)
        self.assertEqual(verified_observed.receipt.http_status, 200)
        self.assertEqual(
            verified_observed.receipt.read_attempt_binding_sha256,
            verified_observed.prepared.attempt.binding_sha256,
        )

    @unittest.skipUnless(sys.platform == "win32", "requires Windows CNG")
    def test_windows_cng_rejects_signed_response_byte_tamper(self):
        _prepared, observed, session_id, key_sha, _response = _fixture()
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
            )


if __name__ == "__main__":
    unittest.main()
