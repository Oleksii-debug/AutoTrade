import base64
from hashlib import sha256
import json
from tempfile import TemporaryDirectory
import unittest
from uuid import NAMESPACE_URL, uuid5

from research.autotrade_research.artifacts.store import ArtifactStore

from mvp.autotrade_mvp.qualification_attestation import (
    EvidenceArtifactRef,
    QualificationAttestation,
    QualificationScope,
    QualificationTrustPolicy,
    SignedQualificationAttestation,
    TrustRoot,
)
from mvp.autotrade_mvp.release_candidate import (
    ReleaseArtifactEvidence,
    ReleaseCandidateDecision,
    ReleaseCandidateError,
    ReleaseCandidateInput,
    freeze_release_candidate,
)


SOURCE = "3cae63fac37820611cddd38128a91185cb271fff"
OTHER_SOURCE = "1" * 40
BASELINE = "sha256:" + "a" * 64
CONTRACTS = "sha256:" + "b" * 64
TEST_ARTIFACT_ID = "a1111111-1111-4111-8111-111111111111"
RELEASE_MEDIA_TYPE = "application/vnd.autotrade.release-artifact"
RELEASE_EVIDENCE_KIND = "AUTOTRADE_RELEASE_EVIDENCE_V1"
_ARTIFACT_BYTES = {}

# Non-production RSA fixture shared only by this integration test.
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

REQUIRED_ROLES = (
    "HOST",
    "WEB",
    "DESKTOP",
    "WINDOWS_PACKAGE",
    "SBOM",
    "DEPENDENCY_RIGHTS",
    "ACCESSIBILITY",
    "API_COMPATIBILITY",
    "CLEAN_INSTALL",
    "LICENSE_NOTICES",
    "RELEASE_QUALIFICATION",
)


def artifact(
    role,
    *,
    source_sha=SOURCE,
    signature_status=None,
    evidence_status="PASS",
    digest_char="c",
):
    if signature_status is None:
        signature_status = (
            "VERIFIED"
            if role in {"HOST", "WEB", "DESKTOP", "WINDOWS_PACKAGE"}
            else "NOT_APPLICABLE"
        )
    identity = "|".join(
        (role, source_sha, signature_status, evidence_status, digest_char)
    )
    artifact_id = str(uuid5(NAMESPACE_URL, "release-test:" + identity))
    data = ("release-artifact:" + identity).encode("utf-8")
    _ARTIFACT_BYTES[artifact_id] = data
    return ReleaseArtifactEvidence.create(
        role=role,
        artifact_id=artifact_id,
        artifact_sha256="sha256:" + sha256(data).hexdigest(),
        source_sha=source_sha,
        signature_status=signature_status,
        evidence_status=evidence_status,
    )


def _trust_root():
    return TrustRoot(
        producer_id="qualifier.release.service",
        verifier_id="autotrade.trust.verifier",
        public_modulus_hex=format(_RSA_N, "x"),
        public_exponent=65537,
        allowed_scopes=(QualificationScope("RELEASE", "FREEZE"),),
        valid_from="2026-09-01T00:00:00Z",
    )


def _sign(value):
    digest_info = _DER_SHA256 + sha256(value.canonical_bytes()).digest()
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


def _qualification(candidate, trust_root, *, artifacts=None, result="PASS"):
    evidence = tuple(
        EvidenceArtifactRef(
            artifact_id=item.artifact_id,
            sha256=item.artifact_sha256,
            media_type=RELEASE_MEDIA_TYPE,
            evidence_kind=RELEASE_EVIDENCE_KIND,
            source_sha=item.source_sha,
        )
        for item in (candidate.artifacts if artifacts is None else artifacts)
    )
    windows = next(
        item for item in candidate.artifacts if item.role == "WINDOWS_PACKAGE"
    )
    value = QualificationAttestation(
        attestation_id=str(
            uuid5(NAMESPACE_URL, "wp54:" + candidate.release_id + ":" + result)
        ),
        source_sha=candidate.source_sha,
        domain="RELEASE",
        gate="FREEZE",
        package_id="WP-54",
        protocol_id="release-freeze-v1",
        protocol_version="1.0.0",
        requirement_ids=("release-candidate-freeze",),
        evidence_refs=evidence,
        producer_id=trust_root.producer_id,
        verifier_id=trust_root.verifier_id,
        trust_root_id=trust_root.root_id,
        runner_id="release-qualification-runner",
        harness_version="1.0.0",
        started_at="2026-09-25T02:00:00Z",
        completed_at="2026-09-25T02:10:00Z",
        signed_at="2026-09-25T02:11:00Z",
        result=result,
        unresolved_limits=() if result == "PASS" else ("qualification incomplete",),
        release_artifact_id=windows.artifact_id,
        release_artifact_sha256=windows.artifact_sha256,
    )
    return SignedQualificationAttestation(value, _sign(value))


def freeze_with_integrity_store(
    candidate,
    *,
    omit_roles=(),
    corrupt_role=None,
    with_attestation=False,
    receipt_override=None,
    policy_override=None,
):
    with TemporaryDirectory() as directory:
        store = ArtifactStore(directory)
        for item in candidate.artifacts:
            if item.role in omit_roles:
                continue
            data = _ARTIFACT_BYTES[item.artifact_id]
            store.publish_bytes(
                artifact_id=item.artifact_id,
                data=data,
                media_type=RELEASE_MEDIA_TYPE,
                rights={"storage": True, "export": False},
                source_refs=[f"git:{item.source_sha}"],
                metadata={
                    "evidence_kind": RELEASE_EVIDENCE_KIND,
                    "role": item.role,
                    "source_sha": item.source_sha,
                    "signature_status": item.signature_status,
                    "evidence_status": item.evidence_status,
                },
            )
        if corrupt_role is not None:
            item = next(
                artifact for artifact in candidate.artifacts
                if artifact.role == corrupt_role
            )
            digest = item.artifact_sha256.removeprefix("sha256:")
            object_path = store.objects / digest[:2] / digest
            object_path.write_bytes(b"corrupt")
        if not with_attestation and receipt_override is None:
            return freeze_release_candidate(candidate, evidence_store=store)
        trust_root = _trust_root()
        trust_policy = (
            policy_override
            if policy_override is not None
            else QualificationTrustPolicy(
                policy_version="2026.09",
                roots=(trust_root,),
            )
        )
        receipt = (
            receipt_override
            if receipt_override is not None
            else _qualification(candidate, trust_root)
        )
        return freeze_release_candidate(
            candidate,
            evidence_store=store,
            qualification_receipt=receipt,
            qualification_policy=trust_policy,
            expected_policy_id=trust_policy.policy_id,
            expected_policy_version=trust_policy.policy_version,
        )

class ReleaseCandidateFreezeTests(unittest.TestCase):
    def candidate(self, **overrides):
        values = dict(
            release_id="autotrade-rc-20260924-1",
            source_sha=SOURCE,
            baseline_hash=BASELINE,
            schema_contract_hash=CONTRACTS,
            artifacts=tuple(
                artifact(role, digest_char=hex(index + 3)[2:])
                for index, role in enumerate(REQUIRED_ROLES)
            ),
            unresolved_blockers=(),
        )
        values.update(overrides)
        return ReleaseCandidateInput.create(**values)

    def test_valid_independently_attested_candidate_freezes_and_binds_trust(self):
        candidate = self.candidate()
        decision = freeze_with_integrity_store(
            candidate,
            with_attestation=True,
        )
        self.assertEqual(decision.status, "FROZEN")
        self.assertIsNotNone(decision.qualification_attestation_id)
        self.assertIsNotNone(decision.qualification_attestation_digest)
        manifest = json.loads(decision.manifest_json)
        self.assertEqual(
            manifest["qualification"]["attestation_id"],
            decision.qualification_attestation_id,
        )
        self.assertEqual(
            manifest["qualification"]["policy_id"],
            decision.qualification_policy_id,
        )

    def test_attestation_must_cover_exact_candidate_artifact_set(self):
        candidate = self.candidate()
        trust_root = _trust_root()
        incomplete = _qualification(
            candidate,
            trust_root,
            artifacts=candidate.artifacts[:-1],
        )
        decision = freeze_with_integrity_store(
            candidate,
            receipt_override=incomplete,
            policy_override=QualificationTrustPolicy(
                policy_version="2026.09",
                roots=(trust_root,),
            ),
        )
        self.assertEqual(decision.status, "BLOCKED")
        self.assertIn("qualification_evidence_set_mismatch", decision.reasons)

    def test_attestation_for_different_release_package_cannot_freeze(self):
        candidate = self.candidate()
        trust_root = _trust_root()
        receipt = _qualification(candidate, trust_root)
        wrong_windows = next(
            item for item in candidate.artifacts if item.role == "WINDOWS_PACKAGE"
        )
        wrong = QualificationAttestation(
            **{
                **receipt.attestation.__dict__,
                "attestation_id": str(uuid5(NAMESPACE_URL, "wrong-release")),
                "release_artifact_sha256": "sha256:" + "0" * 64,
            }
        )
        signed_wrong = SignedQualificationAttestation(wrong, _sign(wrong))
        decision = freeze_with_integrity_store(
            candidate,
            receipt_override=signed_wrong,
            policy_override=QualificationTrustPolicy(
                policy_version="2026.09",
                roots=(trust_root,),
            ),
        )
        self.assertEqual(decision.status, "BLOCKED")
        self.assertIn("independent_evidence_trust_invalid", decision.reasons)

    def test_complete_self_populated_store_cannot_freeze_release(self):
        first = freeze_with_integrity_store(self.candidate())
        reordered = list(self.candidate().artifacts)
        reordered.reverse()
        second = freeze_with_integrity_store(
            self.candidate(artifacts=tuple(reordered))
        )
        self.assertEqual(first.status, "BLOCKED")
        self.assertIn(
            "independent_evidence_trust_unavailable",
            first.reasons,
        )
        self.assertIsNone(first.manifest_json)
        self.assertIsNone(first.manifest_sha256)
        self.assertEqual(first, second)

    def test_direct_frozen_decision_cannot_forge_release_authority(self):
        manifest = (
            '{"artifacts":[],"baseline_hash":"' + BASELINE
            + '","release_id":"forged-rc","schema_contract_hash":"' + CONTRACTS
            + '","source_sha":"' + SOURCE + '"}'
        )
        digest = "sha256:" + sha256(manifest.encode("utf-8")).hexdigest()
        with self.assertRaisesRegex(
            ReleaseCandidateError,
            "unsupported structure",
        ):
            ReleaseCandidateDecision(
                status="FROZEN",
                reasons=(),
                manifest_json=manifest,
                manifest_sha256=digest,
            )

        with self.assertRaisesRegex(
            ReleaseCandidateError,
            "digest does not match",
        ):
            ReleaseCandidateDecision(
                status="FROZEN",
                reasons=(),
                manifest_json=manifest,
                manifest_sha256="sha256:" + "0" * 64,
            )

    def test_missing_required_artifact_blocks_freeze(self):
        artifacts = tuple(
            item for item in self.candidate().artifacts if item.role != "SBOM"
        )
        decision = freeze_with_integrity_store(
            self.candidate(artifacts=artifacts)
        )
        self.assertEqual(decision.status, "BLOCKED")
        self.assertIn("missing_required_artifact:SBOM", decision.reasons)
        self.assertIsNone(decision.manifest_json)

    def test_artifact_from_another_source_sha_blocks_freeze(self):
        artifacts = list(self.candidate().artifacts)
        artifacts[0] = artifact("HOST", source_sha=OTHER_SOURCE)
        decision = freeze_with_integrity_store(
            self.candidate(artifacts=artifacts)
        )
        self.assertEqual(decision.status, "BLOCKED")
        self.assertIn("source_sha_mismatch:HOST", decision.reasons)

    def test_unsigned_host_web_or_desktop_blocks_freeze(self):
        for role in ("HOST", "WEB", "DESKTOP", "WINDOWS_PACKAGE"):
            with self.subTest(role=role):
                artifacts = [
                    (
                        artifact(role, signature_status="MISSING")
                        if item.role == role
                        else item
                    )
                    for item in self.candidate().artifacts
                ]
                decision = freeze_with_integrity_store(
                    self.candidate(artifacts=artifacts)
                )
                self.assertEqual(decision.status, "BLOCKED")
                self.assertIn(
                    f"signature_not_verified:{role}",
                    decision.reasons,
                )

    def test_windows_package_cannot_claim_signature_not_applicable(self):
        with self.assertRaisesRegex(ReleaseCandidateError, "NOT_APPLICABLE"):
            artifact("WINDOWS_PACKAGE", signature_status="NOT_APPLICABLE")

    def test_failed_accessibility_is_a_blocker(self):
        artifacts = [
            (
                artifact("ACCESSIBILITY", evidence_status="FAIL")
                if item.role == "ACCESSIBILITY"
                else item
            )
            for item in self.candidate().artifacts
        ]
        decision = freeze_with_integrity_store(
            self.candidate(artifacts=artifacts)
        )
        self.assertEqual(decision.status, "BLOCKED")
        self.assertIn("evidence_failed:ACCESSIBILITY", decision.reasons)

    def test_inconclusive_dependency_rights_is_not_treated_as_pass(self):
        artifacts = [
            (
                artifact("DEPENDENCY_RIGHTS", evidence_status="INCONCLUSIVE")
                if item.role == "DEPENDENCY_RIGHTS"
                else item
            )
            for item in self.candidate().artifacts
        ]
        decision = freeze_with_integrity_store(
            self.candidate(artifacts=artifacts)
        )
        self.assertEqual(decision.status, "BLOCKED")
        self.assertIn(
            "evidence_inconclusive:DEPENDENCY_RIGHTS",
            decision.reasons,
        )

    def test_release_qualification_is_mandatory_and_must_pass(self):
        artifacts = tuple(
            item
            for item in self.candidate().artifacts
            if item.role != "RELEASE_QUALIFICATION"
        )
        missing = freeze_with_integrity_store(
            self.candidate(artifacts=artifacts)
        )
        self.assertEqual(missing.status, "BLOCKED")
        self.assertIn(
            "missing_required_artifact:RELEASE_QUALIFICATION",
            missing.reasons,
        )

        inconclusive_artifacts = [
            (
                artifact(
                    "RELEASE_QUALIFICATION",
                    evidence_status="INCONCLUSIVE",
                )
                if item.role == "RELEASE_QUALIFICATION"
                else item
            )
            for item in self.candidate().artifacts
        ]
        inconclusive = freeze_with_integrity_store(
            self.candidate(artifacts=inconclusive_artifacts)
        )
        self.assertEqual(inconclusive.status, "BLOCKED")
        self.assertIn(
            "evidence_inconclusive:RELEASE_QUALIFICATION",
            inconclusive.reasons,
        )

    def test_unresolved_blocker_prevents_manifest_publication(self):
        decision = freeze_with_integrity_store(
            self.candidate(
                unresolved_blockers=("provider-paper-qualification",)
            )
        )
        self.assertEqual(decision.status, "BLOCKED")
        self.assertIn(
            "unresolved_blocker:provider-paper-qualification",
            decision.reasons,
        )
        self.assertIsNone(decision.manifest_sha256)

    def test_duplicate_role_is_malformed_input_not_silently_overwritten(self):
        artifacts = self.candidate().artifacts
        with self.assertRaisesRegex(ReleaseCandidateError, "duplicate"):
            self.candidate(artifacts=artifacts + (artifacts[0],))

    def test_unknown_artifact_role_is_rejected_before_attestation(self):
        extra = artifact(
            "ARBITRARY_EXTENSION",
            signature_status="NOT_APPLICABLE",
        )
        base_artifacts = self.candidate().artifacts

        with self.assertRaisesRegex(
            ReleaseCandidateError,
            "unsupported release artifact role: ARBITRARY_EXTENSION",
        ):
            self.candidate(artifacts=base_artifacts + (extra,))

        with self.assertRaisesRegex(
            ReleaseCandidateError,
            "unsupported release artifact role: ARBITRARY_EXTENSION",
        ):
            ReleaseCandidateInput(
                release_id="autotrade-rc-unknown-role",
                source_sha=SOURCE,
                baseline_hash=BASELINE,
                schema_contract_hash=CONTRACTS,
                artifacts=base_artifacts + (extra,),
                unresolved_blockers=(),
            )

    def test_binary_signature_cannot_be_not_applicable(self):
        with self.assertRaisesRegex(ReleaseCandidateError, "NOT_APPLICABLE"):
            artifact("HOST", signature_status="NOT_APPLICABLE")

    def test_direct_artifact_construction_cannot_bypass_digest_validation(self):
        with self.assertRaisesRegex(ReleaseCandidateError, "canonical sha256"):
            ReleaseArtifactEvidence(
                role="HOST",
                artifact_id=TEST_ARTIFACT_ID,
                artifact_sha256="not-a-digest",
                source_sha=SOURCE,
                signature_status="VERIFIED",
                evidence_status="PASS",
            )

    def test_direct_candidate_construction_cannot_bypass_source_validation(self):
        with self.assertRaisesRegex(ReleaseCandidateError, "Git object id"):
            ReleaseCandidateInput(
                release_id="unsafe-direct",
                source_sha="main",
                baseline_hash=BASELINE,
                schema_contract_hash=CONTRACTS,
                artifacts=self.candidate().artifacts,
                unresolved_blockers=(),
            )

    def test_noncanonical_uppercase_evidence_identity_is_rejected(self):
        with self.assertRaisesRegex(ReleaseCandidateError, "canonical lowercase"):
            artifact("HOST", source_sha=SOURCE.upper())
        with self.assertRaisesRegex(ReleaseCandidateError, "lowercase hex"):
            ReleaseArtifactEvidence.create(
                role="SBOM",
                artifact_id=TEST_ARTIFACT_ID,
                artifact_sha256=("sha256:" + "A" * 64),
                source_sha=SOURCE,
                signature_status="NOT_APPLICABLE",
                evidence_status="PASS",
            )

    def test_release_evidence_accepts_git_sha256_but_rejects_identity_aliases(self):
        git_sha256 = "d" * 64
        item = artifact("HOST", source_sha=git_sha256)
        self.assertEqual(item.source_sha, git_sha256)

        for invalid in (" " + SOURCE, SOURCE + " ", "D" * 64):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(
                ReleaseCandidateError,
                "canonical lowercase",
            ):
                artifact("HOST", source_sha=invalid)

        for invalid_id in (
            " " + TEST_ARTIFACT_ID,
            TEST_ARTIFACT_ID.upper(),
            TEST_ARTIFACT_ID.replace("-", ""),
        ):
            with self.subTest(invalid_id=invalid_id), self.assertRaisesRegex(
                ReleaseCandidateError,
                "canonical UUID",
            ):
                ReleaseArtifactEvidence.create(
                    role="SBOM",
                    artifact_id=invalid_id,
                    artifact_sha256="sha256:" + ("a" * 64),
                    source_sha=SOURCE,
                    signature_status="NOT_APPLICABLE",
                    evidence_status="PASS",
                )

    def test_changed_self_asserted_evidence_still_cannot_publish_manifest(self):
        original = freeze_with_integrity_store(self.candidate())
        artifacts = list(self.candidate().artifacts)
        index = next(i for i, item in enumerate(artifacts) if item.role == "SBOM")
        artifacts[index] = artifact("SBOM", digest_char="f")
        changed = freeze_with_integrity_store(
            self.candidate(artifacts=artifacts)
        )
        self.assertEqual(original.status, "BLOCKED")
        self.assertEqual(changed.status, "BLOCKED")
        self.assertIn(
            "independent_evidence_trust_unavailable",
            changed.reasons,
        )
        self.assertIsNone(original.manifest_sha256)
        self.assertIsNone(changed.manifest_sha256)


    def test_nonbinary_missing_or_invalid_signature_status_is_unresolved(self):
        for status in ("MISSING", "INVALID"):
            with self.subTest(status=status):
                artifacts = [
                    (
                        artifact("SBOM", signature_status=status)
                        if item.role == "SBOM"
                        else item
                    )
                    for item in self.candidate().artifacts
                ]
                decision = freeze_with_integrity_store(
                    self.candidate(artifacts=artifacts)
                )
                self.assertEqual(decision.status, "BLOCKED")
                self.assertIn(
                    "signature_status_unresolved:SBOM",
                    decision.reasons,
                )



    def test_freeze_without_store_reports_both_missing_integrity_and_trust(self):
        decision = freeze_release_candidate(self.candidate())
        self.assertEqual(decision.status, "BLOCKED")
        self.assertIn("evidence_store_missing", decision.reasons)
        self.assertIn(
            "independent_evidence_trust_unavailable",
            decision.reasons,
        )
        self.assertIsNone(decision.manifest_json)

    def test_missing_exact_artifact_blocks_independent_verification(self):
        decision = freeze_with_integrity_store(
            self.candidate(),
            omit_roles={"SBOM"},
        )
        self.assertEqual(decision.status, "BLOCKED")
        self.assertIn(
            "evidence_integrity_unverified:SBOM",
            decision.reasons,
        )

    def test_corrupt_artifact_object_fails_closed(self):
        decision = freeze_with_integrity_store(
            self.candidate(),
            corrupt_role="HOST",
        )
        self.assertEqual(decision.status, "BLOCKED")
        self.assertIn(
            "evidence_integrity_unverified:HOST",
            decision.reasons,
        )

    def test_arbitrary_callback_cannot_be_installed_as_release_authority(self):
        with self.assertRaisesRegex(TypeError, "evidence_store must be ArtifactStore"):
            freeze_release_candidate(
                self.candidate(),
                evidence_store=lambda _artifact: True,
            )

if __name__ == "__main__":
    unittest.main()
