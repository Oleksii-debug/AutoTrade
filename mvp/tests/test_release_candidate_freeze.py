import base64
from hashlib import sha256
import json
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import NAMESPACE_URL, uuid5

from research.autotrade_research.artifacts.store import ArtifactStore

import mvp.autotrade_mvp.release_candidate as release_candidate_module
import mvp.autotrade_mvp.supply_chain_qualification as supply_chain_module
from mvp.autotrade_mvp.qualification_attestation import (
    EvidenceArtifactRef,
    QualificationAttestation,
    QualificationScope,
    QualificationTrustPolicy,
    SignedQualificationAttestation,
    TrustRoot,
    verify_qualification_attestation,
)
from mvp.autotrade_mvp.release_candidate import (
    ReleaseArtifactEvidence,
    ReleaseCandidateDecision,
    ReleaseCandidateError,
    ReleaseCandidateInput,
    freeze_release_candidate,
)
from mvp.autotrade_mvp.supply_chain_qualification import (
    ComponentEvidence,
    ModelDataRightsEvidence,
    SupplyChainEvidence,
    supply_chain_subject_requirement,
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
        allowed_scopes=(
            QualificationScope("RELEASE", "FREEZE"),
            QualificationScope("SUPPLY_CHAIN", "RELEASE"),
        ),
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
        requirement_ids=(
            "release-candidate-freeze",
            release_candidate_module.release_candidate_subject_requirement(candidate),
        ),
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


def _publish_supply_artifact(
    store,
    *,
    label,
    media_type,
    metadata,
):
    artifact_id = str(uuid5(NAMESPACE_URL, "wp64-artifact:" + label))
    data = ("wp64-artifact:" + label).encode("utf-8")
    digest = "sha256:" + sha256(data).hexdigest()
    store.publish_bytes(
        artifact_id=artifact_id,
        data=data,
        media_type=media_type,
        rights={"storage": True, "export": False},
        source_refs=[f"git:{SOURCE}"],
        metadata=metadata,
    )
    return artifact_id, digest


def _supply_chain_fixture(candidate, store, *, release_artifact=None):
    windows = (
        release_artifact
        if release_artifact is not None
        else next(
            item for item in candidate.artifacts
            if item.role == "WINDOWS_PACKAGE"
        )
    )
    sbom_id, sbom_hash = _publish_supply_artifact(
        store,
        label="sbom",
        media_type=supply_chain_module._SBOM_MEDIA_TYPE,
        metadata={"evidence_kind": "SBOM", "release_sha": SOURCE},
    )
    provenance_id, provenance_hash = _publish_supply_artifact(
        store,
        label="provenance",
        media_type=supply_chain_module._PROVENANCE_MEDIA_TYPE,
        metadata={"evidence_kind": "PROVENANCE", "release_sha": SOURCE},
    )
    lock_id, lock_hash = _publish_supply_artifact(
        store,
        label="dependency-lock",
        media_type=supply_chain_module._DEPENDENCY_LOCK_MEDIA_TYPE,
        metadata={"evidence_kind": "DEPENDENCY_LOCK", "release_sha": SOURCE},
    )
    component_id = "autotrade-core"
    component_version = "1.0.0"
    component_artifact_id, component_hash = _publish_supply_artifact(
        store,
        label="component",
        media_type=supply_chain_module._COMPONENT_MEDIA_TYPE,
        metadata={
            "evidence_kind": "DISTRIBUTED_COMPONENT",
            "component_id": component_id,
            "version": component_version,
            "release_sha": SOURCE,
        },
    )
    rights_scope = "distribution"
    rights_id, rights_hash = _publish_supply_artifact(
        store,
        label="rights",
        media_type=supply_chain_module._RIGHTS_MEDIA_TYPE,
        metadata={
            "evidence_kind": "MODEL_DATA_RIGHTS",
            "use_scope": rights_scope,
            "release_sha": SOURCE,
        },
    )
    component = ComponentEvidence(
        component_id=component_id,
        artifact_id=component_artifact_id,
        version=component_version,
        declared_artifact_hash=component_hash,
        observed_artifact_hash=component_hash,
        source_revision="git:" + SOURCE,
        license_status="APPROVED",
        distribution_rights="APPROVED",
        advisory_status="CLEAR",
        notice_required=False,
        notice_present=True,
        reviewed_for_release_sha=SOURCE,
    )
    rights = ModelDataRightsEvidence(
        artifact_id=rights_id,
        artifact_hash=rights_hash,
        use_scope=rights_scope,
        rights_status="APPROVED",
        reviewed_for_release_sha=SOURCE,
    )
    return SupplyChainEvidence(
        release_commit_sha=SOURCE,
        built_from_commit_sha=SOURCE,
        sbom_artifact_id=sbom_id,
        sbom_hash=sbom_hash,
        provenance_artifact_id=provenance_id,
        provenance_hash=provenance_hash,
        dependency_lock_artifact_id=lock_id,
        dependency_lock_hash=lock_hash,
        sbom_reviewed_for_release_sha=SOURCE,
        provenance_reviewed_for_release_sha=SOURCE,
        dependency_lock_reviewed_for_release_sha=SOURCE,
        distributed_component_ids=(component_id,),
        sbom_component_ids=(component_id,),
        components=(component,),
        model_data_rights=(rights,),
        release_artifact_id=windows.artifact_id,
        release_artifact_sha256=windows.artifact_sha256,
    )


def _supply_chain_receipt(evidence, trust_root):
    refs = [
        EvidenceArtifactRef(
            artifact_id=evidence.sbom_artifact_id,
            sha256=evidence.sbom_hash,
            media_type=supply_chain_module._SBOM_MEDIA_TYPE,
            evidence_kind="SBOM",
            source_sha=SOURCE,
        ),
        EvidenceArtifactRef(
            artifact_id=evidence.provenance_artifact_id,
            sha256=evidence.provenance_hash,
            media_type=supply_chain_module._PROVENANCE_MEDIA_TYPE,
            evidence_kind="PROVENANCE",
            source_sha=SOURCE,
        ),
        EvidenceArtifactRef(
            artifact_id=evidence.dependency_lock_artifact_id,
            sha256=evidence.dependency_lock_hash,
            media_type=supply_chain_module._DEPENDENCY_LOCK_MEDIA_TYPE,
            evidence_kind="DEPENDENCY_LOCK",
            source_sha=SOURCE,
        ),
    ]
    refs.extend(
        EvidenceArtifactRef(
            artifact_id=item.artifact_id,
            sha256=item.observed_artifact_hash,
            media_type=supply_chain_module._COMPONENT_MEDIA_TYPE,
            evidence_kind="DISTRIBUTED_COMPONENT",
            source_sha=SOURCE,
        )
        for item in evidence.components
    )
    refs.extend(
        EvidenceArtifactRef(
            artifact_id=item.artifact_id,
            sha256=item.artifact_hash,
            media_type=supply_chain_module._RIGHTS_MEDIA_TYPE,
            evidence_kind="MODEL_DATA_RIGHTS",
            source_sha=SOURCE,
        )
        for item in evidence.model_data_rights
    )
    value = QualificationAttestation(
        attestation_id=str(
            uuid5(
                NAMESPACE_URL,
                "wp64:"
                + evidence.release_artifact_id
                + ":"
                + evidence.release_artifact_sha256,
            )
        ),
        source_sha=SOURCE,
        domain="SUPPLY_CHAIN",
        gate="RELEASE",
        package_id="WP-64",
        protocol_id="supply-chain-review-v1",
        protocol_version="1.0.0",
        requirement_ids=(
            "independent-supply-chain-review",
            supply_chain_subject_requirement(evidence),
        ),
        evidence_refs=tuple(refs),
        producer_id=trust_root.producer_id,
        verifier_id=trust_root.verifier_id,
        trust_root_id=trust_root.root_id,
        runner_id="supply-chain-review-runner",
        harness_version="1.0.0",
        started_at="2026-09-25T01:00:00Z",
        completed_at="2026-09-25T01:10:00Z",
        signed_at="2026-09-25T01:11:00Z",
        result="PASS",
        unresolved_limits=(),
        release_artifact_id=evidence.release_artifact_id,
        release_artifact_sha256=evidence.release_artifact_sha256,
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
    before_canonical_verify=None,
    supply_chain_evidence_override=None,
    supply_chain_receipt_override=None,
    supply_chain_release_artifact_override=None,
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
            return freeze_release_candidate(
                candidate,
                evidence_store=store,
                evidence_root=directory,
            )
        trust_root = _trust_root()
        supply_chain_evidence = (
            supply_chain_evidence_override
            if supply_chain_evidence_override is not None
            else _supply_chain_fixture(
                candidate,
                store,
                release_artifact=supply_chain_release_artifact_override,
            )
        )
        supply_chain_receipt = (
            supply_chain_receipt_override
            if supply_chain_receipt_override is not None
            else _supply_chain_receipt(supply_chain_evidence, trust_root)
        )
        canonical_policy = QualificationTrustPolicy(
            policy_version="2026.09",
            roots=(trust_root,),
        )
        caller_policy = (
            policy_override
            if policy_override is not None
            else canonical_policy
        )
        receipt = (
            receipt_override
            if receipt_override is not None
            else _qualification(candidate, trust_root)
        )

        def canonical_verify(receipt_arg, **kwargs):
            if before_canonical_verify is not None:
                before_canonical_verify()
            return verify_qualification_attestation(
                receipt_arg,
                policy=canonical_policy,
                expected_policy_id=canonical_policy.policy_id,
                expected_policy_version=canonical_policy.policy_version,
                **kwargs,
            )

        with patch.object(
            release_candidate_module,
            "verify_canonical_qualification_attestation",
            side_effect=canonical_verify,
        ), patch.object(
            supply_chain_module,
            "verify_canonical_qualification_attestation",
            side_effect=canonical_verify,
        ):
            return freeze_release_candidate(
                candidate,
                evidence_store=store,
                evidence_root=directory,
                qualification_receipt=receipt,
                supply_chain_evidence=supply_chain_evidence,
                supply_chain_receipt=supply_chain_receipt,
                qualification_policy=caller_policy,
                expected_policy_id=caller_policy.policy_id,
                expected_policy_version=caller_policy.policy_version,
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


    def test_freeze_uses_one_exact_detached_candidate_graph_across_callbacks(self):
        phase = {"mutated": False}

        class HostileArtifact(ReleaseArtifactEvidence):
            def __getattribute__(self, name):
                if phase["mutated"]:
                    if name == "evidence_status":
                        return "FAIL"
                    if name == "signature_status":
                        return "INVALID"
                return super().__getattribute__(name)

        class HostileCandidate(ReleaseCandidateInput):
            def __getattribute__(self, name):
                if phase["mutated"]:
                    if name == "source_sha":
                        return OTHER_SOURCE
                    if name == "unresolved_blockers":
                        return ("late-caller-blocker",)
                return super().__getattribute__(name)

        base = self.candidate()
        first = base.artifacts[0]
        hostile_artifact = HostileArtifact(
            role=first.role,
            artifact_id=first.artifact_id,
            artifact_sha256=first.artifact_sha256,
            source_sha=first.source_sha,
            signature_status=first.signature_status,
            evidence_status=first.evidence_status,
        )
        hostile_artifacts = (hostile_artifact,) + base.artifacts[1:]
        hostile_candidate = HostileCandidate(
            release_id=base.release_id,
            source_sha=base.source_sha,
            baseline_hash=base.baseline_hash,
            schema_contract_hash=base.schema_contract_hash,
            artifacts=hostile_artifacts,
            unresolved_blockers=base.unresolved_blockers,
        )

        decision = freeze_with_integrity_store(
            hostile_candidate,
            with_attestation=True,
            before_canonical_verify=lambda: phase.__setitem__("mutated", True),
        )

        self.assertTrue(phase["mutated"])
        self.assertEqual(decision.status, "FROZEN")
        manifest = json.loads(decision.manifest_json)
        self.assertEqual(manifest["source_sha"], SOURCE)
        by_role = {item["role"]: item for item in manifest["artifacts"]}
        self.assertEqual(by_role[first.role]["evidence_status"], "PASS")
        self.assertEqual(by_role[first.role]["signature_status"], first.signature_status)
        self.assertNotIn("late-caller-blocker", decision.reasons)

    def test_freeze_rejects_nonexact_terminal_container_views_before_callbacks(self):
        class HostileCandidate(ReleaseCandidateInput):
            def __getattribute__(self, name):
                value = super().__getattribute__(name)
                if name == "artifacts":
                    return list(value)
                return value

        base = self.candidate()
        hostile_candidate = HostileCandidate(
            release_id=base.release_id,
            source_sha=base.source_sha,
            baseline_hash=base.baseline_hash,
            schema_contract_hash=base.schema_contract_hash,
            artifacts=base.artifacts,
            unresolved_blockers=base.unresolved_blockers,
        )

        with self.assertRaisesRegex(
            ReleaseCandidateError,
            "candidate.artifacts must be an exact tuple",
        ):
            freeze_release_candidate(hostile_candidate)

    def test_caller_selected_trust_policy_cannot_freeze_release(self):
        candidate = self.candidate()
        canonical_root = _trust_root()
        hostile_root = TrustRoot(
            producer_id="candidate.self",
            verifier_id=canonical_root.verifier_id,
            public_modulus_hex=canonical_root.public_modulus_hex,
            public_exponent=canonical_root.public_exponent,
            allowed_scopes=canonical_root.allowed_scopes,
            valid_from=canonical_root.valid_from,
        )
        hostile_policy = QualificationTrustPolicy(
            policy_version="2026.09",
            roots=(hostile_root,),
        )
        hostile_receipt = _qualification(candidate, hostile_root)
        decision = freeze_with_integrity_store(
            candidate,
            receipt_override=hostile_receipt,
            policy_override=hostile_policy,
        )
        self.assertEqual(decision.status, "BLOCKED")
        self.assertIn("independent_evidence_trust_invalid", decision.reasons)

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

    def test_wp64_review_for_package_a_cannot_freeze_package_b(self):
        candidate_a = self.candidate()
        windows_a = next(
            item for item in candidate_a.artifacts
            if item.role == "WINDOWS_PACKAGE"
        )
        windows_b = artifact("WINDOWS_PACKAGE", digest_char="e")
        candidate_b = self.candidate(
            artifacts=tuple(
                windows_b if item.role == "WINDOWS_PACKAGE" else item
                for item in candidate_a.artifacts
            )
        )

        wrong = freeze_with_integrity_store(
            candidate_b,
            with_attestation=True,
            supply_chain_release_artifact_override=windows_a,
        )
        self.assertEqual(wrong.status, "BLOCKED")
        self.assertIn(
            "supply_chain_release_artifact_mismatch",
            wrong.reasons,
        )

        correct = freeze_with_integrity_store(
            candidate_b,
            with_attestation=True,
        )
        self.assertEqual(correct.status, "FROZEN")
        manifest = json.loads(correct.manifest_json)
        self.assertEqual(
            manifest["supply_chain"]["release_artifact_id"],
            windows_b.artifact_id,
        )
        self.assertEqual(
            manifest["supply_chain"]["release_artifact_sha256"],
            windows_b.artifact_sha256,
        )

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

    def test_direct_frozen_decision_cannot_replay_verified_manifest_as_authority(self):
        decision = freeze_with_integrity_store(
            self.candidate(),
            with_attestation=True,
        )

        with self.assertRaisesRegex(
            ReleaseCandidateError,
            "requires canonical qualification verification context",
        ):
            ReleaseCandidateDecision(
                status="FROZEN",
                reasons=(),
                manifest_json=decision.manifest_json,
                manifest_sha256=decision.manifest_sha256,
                qualification_attestation_id=decision.qualification_attestation_id,
                qualification_attestation_digest=decision.qualification_attestation_digest,
                qualification_policy_id=decision.qualification_policy_id,
                qualification_trust_root_id=decision.qualification_trust_root_id,
            )

    def test_frozen_rehydration_reverifies_canonical_signature_and_evidence(self):
        candidate = self.candidate()
        with TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            for item in candidate.artifacts:
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
            trust_root = _trust_root()
            canonical_policy = QualificationTrustPolicy(
                policy_version="2026.09",
                roots=(trust_root,),
            )
            receipt = _qualification(candidate, trust_root)

            def canonical_verify(receipt_arg, **kwargs):
                return verify_qualification_attestation(
                    receipt_arg,
                    policy=canonical_policy,
                    expected_policy_id=canonical_policy.policy_id,
                    expected_policy_version=canonical_policy.policy_version,
                    **kwargs,
                )

            with patch.object(
                release_candidate_module,
                "verify_canonical_qualification_attestation",
                side_effect=canonical_verify,
            ):
                original = freeze_release_candidate(
                    candidate,
                    evidence_store=store,
                    evidence_root=directory,
                    qualification_receipt=receipt,
                )
                rehydrated = ReleaseCandidateDecision(
                    status="FROZEN",
                    reasons=(),
                    manifest_json=original.manifest_json,
                    manifest_sha256=original.manifest_sha256,
                    qualification_attestation_id=original.qualification_attestation_id,
                    qualification_attestation_digest=original.qualification_attestation_digest,
                    qualification_policy_id=original.qualification_policy_id,
                    qualification_trust_root_id=original.qualification_trust_root_id,
                    _verification_store=store,
                    _verification_root=directory,
                )
                self.assertEqual(rehydrated, original)

                body = json.loads(original.manifest_json)
                encoded_signature = body["qualification"]["receipt"]["signature_b64"]
                signature_bytes = base64.b64decode(encoded_signature)
                body["qualification"]["receipt"]["signature_b64"] = base64.b64encode(
                    b"\\x00" * len(signature_bytes)
                ).decode("ascii")
                forged_json = json.dumps(
                    body,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                )
                forged_sha = (
                    "sha256:" + sha256(forged_json.encode("utf-8")).hexdigest()
                )
                with self.assertRaisesRegex(
                    ReleaseCandidateError,
                    "qualification receipt is not canonically verified",
                ):
                    ReleaseCandidateDecision(
                        status="FROZEN",
                        reasons=(),
                        manifest_json=forged_json,
                        manifest_sha256=forged_sha,
                        qualification_attestation_id=(
                            original.qualification_attestation_id
                        ),
                        qualification_attestation_digest=(
                            original.qualification_attestation_digest
                        ),
                        qualification_policy_id=original.qualification_policy_id,
                        qualification_trust_root_id=(
                            original.qualification_trust_root_id
                        ),
                        _verification_store=store,
                        _verification_root=directory,
                    )

    def test_structural_rehydration_rejects_changed_signed_artifact_binding(self):
        decision = freeze_with_integrity_store(
            self.candidate(),
            with_attestation=True,
        )
        body = json.loads(decision.manifest_json)
        body["artifacts"][0]["artifact_id"] = str(
            uuid5(NAMESPACE_URL, "wp54:forged-artifact-binding")
        )
        forged_json = json.dumps(
            body,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        forged_sha = (
            "sha256:" + sha256(forged_json.encode("utf-8")).hexdigest()
        )

        with self.assertRaisesRegex(
            ReleaseCandidateError,
            "does not cover exact artifact set",
        ):
            ReleaseCandidateDecision(
                status="FROZEN",
                reasons=(),
                manifest_json=forged_json,
                manifest_sha256=forged_sha,
                qualification_attestation_id=decision.qualification_attestation_id,
                qualification_attestation_digest=decision.qualification_attestation_digest,
                qualification_policy_id=decision.qualification_policy_id,
                qualification_trust_root_id=decision.qualification_trust_root_id,
            )

    def test_structural_rehydration_rejects_changed_signed_role_binding(self):
        decision = freeze_with_integrity_store(
            self.candidate(),
            with_attestation=True,
        )
        body = json.loads(decision.manifest_json)
        by_role = {item["role"]: item for item in body["artifacts"]}
        sbom = by_role["SBOM"]
        notices = by_role["LICENSE_NOTICES"]
        sbom["role"], notices["role"] = notices["role"], sbom["role"]
        body["artifacts"].sort(key=lambda item: item["role"])
        forged_json = json.dumps(
            body,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        forged_sha = (
            "sha256:" + sha256(forged_json.encode("utf-8")).hexdigest()
        )

        with self.assertRaisesRegex(
            ReleaseCandidateError,
            "does not cover exact artifact set",
        ):
            ReleaseCandidateDecision(
                status="FROZEN",
                reasons=(),
                manifest_json=forged_json,
                manifest_sha256=forged_sha,
                qualification_attestation_id=decision.qualification_attestation_id,
                qualification_attestation_digest=decision.qualification_attestation_digest,
                qualification_policy_id=decision.qualification_policy_id,
                qualification_trust_root_id=decision.qualification_trust_root_id,
            )

    def test_direct_frozen_decision_rejects_noncanonical_artifact_manifest(self):
        decision = freeze_with_integrity_store(
            self.candidate(),
            with_attestation=True,
        )
        body = json.loads(decision.manifest_json)
        body["artifacts"].append(
            {
                **body["artifacts"][0],
                "role": "ARBITRARY_EXTENSION",
            }
        )
        forged_json = json.dumps(
            body,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        forged_sha = "sha256:" + sha256(forged_json.encode("utf-8")).hexdigest()

        with self.assertRaisesRegex(
            ReleaseCandidateError,
            "artifact role set is not canonical",
        ):
            ReleaseCandidateDecision(
                status="FROZEN",
                reasons=(),
                manifest_json=forged_json,
                manifest_sha256=forged_sha,
                qualification_attestation_id=decision.qualification_attestation_id,
                qualification_attestation_digest=decision.qualification_attestation_digest,
                qualification_policy_id=decision.qualification_policy_id,
                qualification_trust_root_id=decision.qualification_trust_root_id,
            )

        body = json.loads(decision.manifest_json)
        body["artifacts"].reverse()
        reordered_json = json.dumps(
            body,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        reordered_sha = (
            "sha256:" + sha256(reordered_json.encode("utf-8")).hexdigest()
        )
        with self.assertRaisesRegex(
            ReleaseCandidateError,
            "canonical role order",
        ):
            ReleaseCandidateDecision(
                status="FROZEN",
                reasons=(),
                manifest_json=reordered_json,
                manifest_sha256=reordered_sha,
                qualification_attestation_id=decision.qualification_attestation_id,
                qualification_attestation_digest=decision.qualification_attestation_digest,
                qualification_policy_id=decision.qualification_policy_id,
                qualification_trust_root_id=decision.qualification_trust_root_id,
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
