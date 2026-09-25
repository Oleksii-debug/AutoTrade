import base64
from hashlib import sha256
from tempfile import TemporaryDirectory
import unittest
from uuid import NAMESPACE_URL, uuid5

from research.autotrade_research.artifacts.store import ArtifactStore

from mvp.autotrade_mvp.qualification_attestation import (
    EvidenceArtifactRef,
    QualificationAttestation,
    QualificationScope,
    QualificationTrustError,
    QualificationTrustPolicy,
    SignedQualificationAttestation,
    TrustRoot,
    parse_qualification_trust_policy,
    parse_signed_qualification_attestation,
    qualification_trust_policy_payload,
    verify_qualification_attestation,
)


SOURCE = "a" * 40
OTHER_SOURCE = "b" * 40
RELEASE_A = str(uuid5(NAMESPACE_URL, "release-a"))
RELEASE_B = str(uuid5(NAMESPACE_URL, "release-b"))
RELEASE_A_SHA = "sha256:" + "c" * 64
RELEASE_B_SHA = "sha256:" + "d" * 64
EVIDENCE_ID = str(uuid5(NAMESPACE_URL, "qualification-evidence"))
EVIDENCE = b"immutable qualification evidence"
EVIDENCE_SHA = "sha256:" + sha256(EVIDENCE).hexdigest()

# Non-production test key. Runtime policy contains only the public half.
_RSA_N = int(
    "ae5f6165c50e6af720388e7649c53e3ec1c539b5d6cfea06c10d9895b362fc9f"
    "ae4d7afc9c4496d4ef3716fd49f8f9321f81e9794be8861f078cd25c702e57a6"
    "f135cb6e6cc7ee562c5aee6a52eae29a4b61fd6db7a0e0272525888588247d3b"
    "86f94992b9fe6471da90a6db05bd1108702adadba98e0130a903e1fa3ee58211b"
    "61c036a81442fb25824cc4b90dd8ae2eeee4112d01f80c5b44898f03bf3e7d278"
    "6906aa2ea7d3065074c5a6ab72f20926fa34edef80433f64ab60f57e91e22a700"
    "f09f442796f17b142331d8b6764c8ccc9545320f11a2f52512915a3085e41beaf"
    "2e3437e1a2f91cd674c49f165fa296a50d1692d78ea3e4bc0fcc9b021f53",
    16,
)
_RSA_D = int(
    "43e0b631df1923336af40924ebc79fd8df262eb665d60eb42d5f6507d54a51abb"
    "936c90adfabe589234baf23cf315f940ee6cbe35f54b72d0a0bdbf186ebcb4c1d"
    "b682a7cc29b1d212b71cfaffa716a9d8715f2d601f7c5250a8013275d23a7bbb2"
    "97c65e5082dc29241dfe9ff9c5f2e89376d75b7d5a309f5a920c500c9e7ace61"
    "f85eefc7665f3dff9bab2e28da848e0ea0c5c32a90043fdaf056c3a21431d4aa2"
    "9eaed6a2d70bbbaff3b78e1bdd9bbd4a617e3f16d5da358864c538408994097358"
    "54c4cecdadc8f082693679b823270b8d2402102916cb2b94bb92615d480b184a8d"
    "cd479ac90fd9c18ee4e341b9bee6238f231b672d8715c649e28cdde9",
    16,
)
_DER_SHA256 = bytes.fromhex("3031300d060960864801650304020105000420")


def sign(attestation):
    digest_info = _DER_SHA256 + sha256(attestation.canonical_bytes()).digest()
    width = (_RSA_N.bit_length() + 7) // 8
    encoded = (
        b"\x00\x01"
        + b"\xff" * (width - len(digest_info) - 3)
        + b"\x00"
        + digest_info
    )
    signature = pow(
        int.from_bytes(encoded, "big"), _RSA_D, _RSA_N
    ).to_bytes(width, "big")
    return base64.b64encode(signature).decode("ascii")


def root(*, revoked_at=None, scopes=None):
    return TrustRoot(
        producer_id="qualifier.release.service",
        verifier_id="autotrade.trust.verifier",
        public_modulus_hex=format(_RSA_N, "x"),
        public_exponent=65537,
        allowed_scopes=scopes
        or (
            QualificationScope("RELEASE", "FREEZE"),
            QualificationScope("SCIENCE", "ECONOMIC_EDGE"),
        ),
        valid_from="2026-09-01T00:00:00Z",
        revoked_at=revoked_at,
    )


def policy(value):
    return QualificationTrustPolicy(
        policy_version="2026.09",
        roots=(value,),
    )


def publish(store, *, source=SOURCE):
    return store.publish_bytes(
        artifact_id=EVIDENCE_ID,
        data=EVIDENCE,
        media_type="application/vnd.autotrade.qualification-evidence",
        rights={"storage": True, "export": False},
        source_refs=[f"git:{source}"],
        metadata={"evidence_kind": "QUALIFICATION_RUN"},
    )


def evidence_ref():
    return EvidenceArtifactRef(
        artifact_id=EVIDENCE_ID,
        sha256=EVIDENCE_SHA,
        media_type="application/vnd.autotrade.qualification-evidence",
        evidence_kind="QUALIFICATION_RUN",
        source_sha=SOURCE,
    )


def attestation(trust_root, **overrides):
    values = dict(
        attestation_id=str(uuid5(NAMESPACE_URL, "release-attestation")),
        source_sha=SOURCE,
        domain="RELEASE",
        gate="FREEZE",
        package_id="WP-54",
        protocol_id="release-freeze-v1",
        protocol_version="1.0.0",
        requirement_ids=(
            "release-candidate-freeze",
            "signed-windows-artifacts",
        ),
        evidence_refs=(evidence_ref(),),
        producer_id=trust_root.producer_id,
        verifier_id=trust_root.verifier_id,
        trust_root_id=trust_root.root_id,
        runner_id="qualification-runner-1",
        harness_version="1.0.0",
        started_at="2026-09-25T02:00:00Z",
        completed_at="2026-09-25T02:10:00Z",
        signed_at="2026-09-25T02:11:00Z",
        result="PASS",
        release_artifact_id=RELEASE_A,
        release_artifact_sha256=RELEASE_A_SHA,
    )
    values.update(overrides)
    return QualificationAttestation(**values)


def verify(receipt, store, trust_policy, **overrides):
    values = dict(
        expected_policy_id=trust_policy.policy_id,
        expected_policy_version=trust_policy.policy_version,
        expected_source_sha=SOURCE,
        expected_domain="RELEASE",
        expected_gate="FREEZE",
        expected_package_id="WP-54",
        expected_protocol_id="release-freeze-v1",
        expected_protocol_version="1.0.0",
        expected_requirement_id="release-candidate-freeze",
        expected_release_artifact_id=RELEASE_A,
        expected_release_artifact_sha256=RELEASE_A_SHA,
    )
    values.update(overrides)
    return verify_qualification_attestation(
        receipt,
        policy=trust_policy,
        evidence_store=store,
        **values,
    )


class QualificationAttestationTests(unittest.TestCase):
    def test_git_sha256_identity_is_supported_without_case_aliases(self):
        trust_root = root()
        source_sha256 = "d" * 64
        ref = EvidenceArtifactRef(
            artifact_id=EVIDENCE_ID,
            sha256=EVIDENCE_SHA,
            media_type="application/vnd.autotrade.qualification-evidence",
            evidence_kind="QUALIFICATION_RUN",
            source_sha=source_sha256,
        )
        value = attestation(
            trust_root,
            source_sha=source_sha256,
            evidence_refs=(ref,),
        )
        self.assertEqual(value.source_sha, source_sha256)
        with self.assertRaisesRegex(QualificationTrustError, "lowercase 40- or 64"):
            attestation(
                trust_root,
                source_sha=source_sha256.upper(),
                evidence_refs=(ref,),
            )

    def test_valid_signed_receipt_resolves_exact_evidence(self):
        trust_root = root()
        trust_policy = policy(trust_root)
        value = attestation(trust_root)
        receipt = SignedQualificationAttestation(value, sign(value))
        with TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            publish(store)
            accepted = verify(receipt, store, trust_policy)
        self.assertEqual(accepted.result, "PASS")
        self.assertEqual(accepted.attestation_digest, value.content_digest)
        self.assertEqual(accepted.policy_id, trust_policy.policy_id)
        self.assertEqual(accepted.trust_root_id, trust_root.root_id)

    def test_candidate_supplied_policy_cannot_replace_pinned_policy(self):
        trust_root = root()
        pinned = policy(trust_root)
        candidate_policy = QualificationTrustPolicy(
            policy_version="candidate.1",
            roots=(trust_root,),
        )
        value = attestation(trust_root)
        receipt = SignedQualificationAttestation(value, sign(value))
        with TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            publish(store)
            with self.assertRaisesRegex(
                QualificationTrustError, "pinned policy"
            ):
                verify(
                    receipt,
                    store,
                    candidate_policy,
                    expected_policy_id=pinned.policy_id,
                    expected_policy_version=pinned.policy_version,
                )

    def test_attestation_cannot_be_replayed_across_packages(self):
        trust_root = root()
        trust_policy = policy(trust_root)
        value = attestation(trust_root)
        receipt = SignedQualificationAttestation(value, sign(value))
        with TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            publish(store)
            with self.assertRaisesRegex(
                QualificationTrustError, "package"
            ):
                verify(
                    receipt,
                    store,
                    trust_policy,
                    expected_package_id="WP-60",
                )

    def test_persisted_receipt_and_policy_round_trip_through_strict_ingress(self):
        trust_root = root()
        trust_policy = policy(trust_root)
        value = attestation(trust_root)
        serialized_receipt = {
            "attestation": value.canonical_payload(),
            "signature_b64": sign(value),
        }
        parsed_policy = parse_qualification_trust_policy(
            qualification_trust_policy_payload(trust_policy)
        )
        parsed_receipt = parse_signed_qualification_attestation(
            serialized_receipt
        )
        with TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            publish(store)
            accepted = verify(
                parsed_receipt,
                store,
                parsed_policy,
            )
        self.assertEqual(accepted.attestation_digest, value.content_digest)
        self.assertEqual(parsed_policy.policy_id, trust_policy.policy_id)

    def test_persisted_trust_ingress_rejects_unknown_fields_and_root_id_drift(self):
        trust_root = root()
        value = attestation(trust_root)
        receipt = {
            "attestation": {
                **value.canonical_payload(),
                "unexpected": "candidate-controlled",
            },
            "signature_b64": sign(value),
        }
        with self.assertRaisesRegex(
            QualificationTrustError,
            "fields mismatch",
        ):
            parse_signed_qualification_attestation(receipt)

        policy_payload = qualification_trust_policy_payload(policy(trust_root))
        policy_payload["roots"][0]["root_id"] = "sha256:" + "0" * 64
        with self.assertRaisesRegex(
            QualificationTrustError,
            "root_id mismatch",
        ):
            parse_qualification_trust_policy(policy_payload)

    def test_persisted_attestation_rejects_noncanonical_nested_evidence(self):
        trust_root = root()
        value = attestation(trust_root)
        payload = value.canonical_payload()
        payload["evidence_refs"][0]["extra"] = "not-signed-schema"
        with self.assertRaisesRegex(
            QualificationTrustError,
            "fields mismatch",
        ):
            parse_signed_qualification_attestation(
                {
                    "attestation": payload,
                    "signature_b64": sign(value),
                }
            )

    def test_self_published_pass_without_valid_signature_fails(self):
        trust_root = root()
        value = attestation(trust_root)
        forged = SignedQualificationAttestation(
            value,
            base64.b64encode(b"x" * 256).decode("ascii"),
        )
        with TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            publish(store)
            with self.assertRaisesRegex(
                QualificationTrustError, "signature"
            ):
                verify(forged, store, policy(trust_root))

    def test_release_a_receipt_cannot_qualify_release_b(self):
        trust_root = root()
        value = attestation(trust_root)
        receipt = SignedQualificationAttestation(value, sign(value))
        with TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            publish(store)
            with self.assertRaisesRegex(
                QualificationTrustError, "different release"
            ):
                verify(
                    receipt,
                    store,
                    policy(trust_root),
                    expected_release_artifact_id=RELEASE_B,
                    expected_release_artifact_sha256=RELEASE_B_SHA,
                )

    def test_cross_domain_receipt_cannot_satisfy_release_gate(self):
        trust_root = root()
        value = attestation(
            trust_root,
            domain="SCIENCE",
            gate="ECONOMIC_EDGE",
            package_id="WP-56",
            protocol_id="walk-forward-v1",
            requirement_ids=("economic-edge",),
            release_artifact_id=None,
            release_artifact_sha256=None,
        )
        receipt = SignedQualificationAttestation(value, sign(value))
        with TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            publish(store)
            with self.assertRaisesRegex(
                QualificationTrustError, "domain/gate"
            ):
                verify(receipt, store, policy(trust_root))

    def test_stale_protocol_and_uncovered_requirement_fail(self):
        trust_root = root()
        value = attestation(trust_root)
        receipt = SignedQualificationAttestation(value, sign(value))
        with TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            publish(store)
            with self.assertRaisesRegex(
                QualificationTrustError, "protocol"
            ):
                verify(
                    receipt,
                    store,
                    policy(trust_root),
                    expected_protocol_version="2.0.0",
                )
            with self.assertRaisesRegex(
                QualificationTrustError, "cover"
            ):
                verify(
                    receipt,
                    store,
                    policy(trust_root),
                    expected_requirement_id="nvda-real-run",
                )

    def test_missing_or_tampered_evidence_fails_even_with_signature(self):
        trust_root = root()
        value = attestation(trust_root)
        receipt = SignedQualificationAttestation(value, sign(value))
        with TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            with self.assertRaisesRegex(
                QualificationTrustError, "cannot be resolved"
            ):
                verify(receipt, store, policy(trust_root))
            manifest = publish(store)
            digest = manifest["sha256"].removeprefix("sha256:")
            store._object_path(digest).write_bytes(b"tampered")
            with self.assertRaisesRegex(
                QualificationTrustError, "cannot be resolved"
            ):
                verify(receipt, store, policy(trust_root))

    def test_evidence_source_binding_is_not_candidate_metadata(self):
        trust_root = root()
        value = attestation(trust_root)
        receipt = SignedQualificationAttestation(value, sign(value))
        with TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            publish(store, source=OTHER_SOURCE)
            with self.assertRaisesRegex(
                QualificationTrustError, "source SHA"
            ):
                verify(receipt, store, policy(trust_root))

    def test_revoked_root_cannot_authorize_backdated_or_new_terminal_receipts(self):
        trust_root = root(
            revoked_at="2026-09-25T03:00:00Z"
        )
        trust_policy = policy(trust_root)
        before = attestation(trust_root)
        after = attestation(
            trust_root,
            attestation_id=str(
                uuid5(NAMESPACE_URL, "post-revocation")
            ),
            started_at="2026-09-25T03:01:00Z",
            completed_at="2026-09-25T03:02:00Z",
            signed_at="2026-09-25T03:03:00Z",
        )
        backdated_after_compromise = attestation(
            trust_root,
            attestation_id=str(
                uuid5(NAMESPACE_URL, "backdated-after-compromise")
            ),
            started_at="2026-09-25T02:50:00Z",
            completed_at="2026-09-25T02:55:00Z",
            signed_at="2026-09-25T02:59:59Z",
        )
        with TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            publish(store)
            for value in (before, after, backdated_after_compromise):
                with self.subTest(attestation_id=value.attestation_id), self.assertRaisesRegex(
                    QualificationTrustError, "revoked trust root"
                ):
                    verify(
                        SignedQualificationAttestation(
                            value, sign(value)
                        ),
                        store,
                        trust_policy,
                    )

    def test_policy_identity_changes_on_revocation_but_root_id_does_not(self):
        active = root()
        revoked = root(
            revoked_at="2026-09-25T03:00:00Z"
        )
        self.assertEqual(active.root_id, revoked.root_id)
        self.assertNotEqual(
            policy(active).policy_id,
            policy(revoked).policy_id,
        )

    def test_pass_cannot_hide_unresolved_limits(self):
        trust_root = root()
        with self.assertRaisesRegex(
            QualificationTrustError, "PASS"
        ):
            attestation(
                trust_root,
                unresolved_limits=("real Windows run missing",),
            )

    def test_duplicate_evidence_or_requirements_are_rejected(self):
        trust_root = root()
        ref = evidence_ref()
        with self.assertRaisesRegex(
            QualificationTrustError, "identities"
        ):
            attestation(
                trust_root,
                evidence_refs=(ref, ref),
            )
        with self.assertRaisesRegex(
            QualificationTrustError, "unique"
        ):
            attestation(
                trust_root,
                requirement_ids=("same", "same"),
            )

    def test_altered_signed_payload_fails_signature(self):
        trust_root = root()
        original = attestation(trust_root)
        changed = attestation(
            trust_root,
            attestation_id=original.attestation_id,
            result="INCONCLUSIVE",
            unresolved_limits=("external reviewer missing",),
        )
        receipt = SignedQualificationAttestation(
            changed, sign(original)
        )
        with TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            publish(store)
            with self.assertRaisesRegex(
                QualificationTrustError, "signature"
            ):
                verify(receipt, store, policy(trust_root))

    def test_root_scope_and_producer_are_independent_authority(self):
        trust_root = root(
            scopes=(
                QualificationScope("RELEASE", "FREEZE"),
            )
        )
        recovery = attestation(
            trust_root,
            domain="RECOVERY",
            gate="RESTORE",
            package_id="WP-59",
            protocol_id="recovery-v1",
            requirement_ids=("restart-recovery",),
            release_artifact_id=None,
            release_artifact_sha256=None,
        )
        with TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            publish(store)
            with self.assertRaisesRegex(
                QualificationTrustError, "not authorized"
            ):
                verify_qualification_attestation(
                    SignedQualificationAttestation(
                        recovery, sign(recovery)
                    ),
                    policy=policy(trust_root),
                    evidence_store=store,
                    expected_policy_id=policy(trust_root).policy_id,
                    expected_policy_version=policy(trust_root).policy_version,
                    expected_source_sha=SOURCE,
                    expected_domain="RECOVERY",
                    expected_gate="RESTORE",
                    expected_package_id="WP-59",
                    expected_protocol_id="recovery-v1",
                    expected_protocol_version="1.0.0",
                    expected_requirement_id="restart-recovery",
                )

        forged = attestation(
            trust_root,
            producer_id="candidate.self",
        )
        with TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            publish(store)
            with self.assertRaisesRegex(
                QualificationTrustError, "producer"
            ):
                verify(
                    SignedQualificationAttestation(
                        forged, sign(forged)
                    ),
                    store,
                    policy(trust_root),
                )


if __name__ == "__main__":
    unittest.main()
