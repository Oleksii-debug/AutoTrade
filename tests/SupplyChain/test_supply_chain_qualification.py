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
    QualificationTrustPolicy,
    SignedQualificationAttestation,
    TrustRoot,
)

from mvp.autotrade_mvp.supply_chain_qualification import (
    ComponentEvidence,
    ModelDataRightsEvidence,
    SupplyChainEvidence,
    qualify_supply_chain,
)


R = "1" * 40
DATA_A = b"supply-chain-artifact-a"
DATA_B = b"supply-chain-artifact-b"
H = "sha256:" + sha256(DATA_A).hexdigest()
H2 = "sha256:" + sha256(DATA_B).hexdigest()
DATA_BY_HASH = {H: DATA_A, H2: DATA_B}


def artifact_id(label):
    return str(uuid5(NAMESPACE_URL, "supply-chain-test:" + label))


SBOM_ID = artifact_id("sbom")
PROVENANCE_ID = artifact_id("provenance")
LOCK_ID = artifact_id("dependency-lock")
COMPONENT_ID = artifact_id("component:example")
RIGHTS_ID = artifact_id("rights:model-local")
EXCEPTION_ID = artifact_id("exception:risk-acceptance-17")
EXCEPTION_ID_2 = artifact_id("exception:risk-acceptance-18")


def component(**overrides):
    values = dict(
        component_id="pkg:pypi/example@1.0",
        artifact_id=COMPONENT_ID,
        version="1.0",
        declared_artifact_hash=H,
        observed_artifact_hash=H,
        source_revision="tag:v1.0",
        license_status="APPROVED",
        distribution_rights="APPROVED",
        advisory_status="CLEAR",
        notice_required=True,
        notice_present=True,
        reviewed_for_release_sha=R,
    )
    values.update(overrides)
    return ComponentEvidence(**values)


def rights(**overrides):
    artifact_label = overrides.pop("artifact_id", None)
    values = dict(
        artifact_id=RIGHTS_ID if artifact_label is None else artifact_id(artifact_label),
        artifact_hash=H,
        use_scope="redistribute",
        rights_status="APPROVED",
        reviewed_for_release_sha=R,
    )
    values.update(overrides)
    return ModelDataRightsEvidence(**values)


def evidence(comp=None, model_rights=None, **overrides):
    comp = component() if comp is None else comp
    values = dict(
        release_commit_sha=R,
        built_from_commit_sha=R,
        sbom_artifact_id=SBOM_ID,
        sbom_hash=H,
        provenance_artifact_id=PROVENANCE_ID,
        provenance_hash=H,
        dependency_lock_artifact_id=LOCK_ID,
        dependency_lock_hash=H,
        sbom_reviewed_for_release_sha=R,
        provenance_reviewed_for_release_sha=R,
        dependency_lock_reviewed_for_release_sha=R,
        distributed_component_ids=(comp.component_id,),
        sbom_component_ids=(comp.component_id,),
        components=(comp,),
        model_data_rights=(rights(),) if model_rights is None else tuple(model_rights),
    )
    values.update(overrides)
    return SupplyChainEvidence(**values)


def _publish(
    store,
    *,
    artifact_id_value,
    artifact_hash,
    media_type,
    release_sha,
    metadata,
):
    data = DATA_BY_HASH[artifact_hash]
    store.publish_bytes(
        artifact_id=artifact_id_value,
        data=data,
        media_type=media_type,
        rights={"storage": True, "export": False},
        source_refs=[f"git:{release_sha}"],
        metadata=metadata,
    )


def qualify(value, *, omit_artifact_ids=(), corrupt_artifact_id=None):
    with TemporaryDirectory() as directory:
        store = ArtifactStore(directory)
        entries = [
            (
                value.sbom_artifact_id,
                value.sbom_hash,
                "application/vnd.autotrade.sbom",
                {"evidence_kind": "SBOM", "release_sha": value.release_commit_sha},
            ),
            (
                value.provenance_artifact_id,
                value.provenance_hash,
                "application/vnd.autotrade.provenance",
                {
                    "evidence_kind": "PROVENANCE",
                    "release_sha": value.release_commit_sha,
                },
            ),
            (
                value.dependency_lock_artifact_id,
                value.dependency_lock_hash,
                "application/vnd.autotrade.dependency-lock",
                {
                    "evidence_kind": "DEPENDENCY_LOCK",
                    "release_sha": value.release_commit_sha,
                },
            ),
        ]
        for item in value.components:
            entries.append(
                (
                    item.artifact_id,
                    item.observed_artifact_hash,
                    "application/vnd.autotrade.distributed-component",
                    {
                        "evidence_kind": "DISTRIBUTED_COMPONENT",
                        "component_id": item.component_id,
                        "version": item.version,
                        "release_sha": value.release_commit_sha,
                    },
                )
            )
            if item.advisory_status == "ALLOWLISTED":
                entries.append(
                    (
                        item.advisory_exception_id,
                        item.advisory_exception_hash,
                        "application/vnd.autotrade.advisory-exception",
                        {
                            "evidence_kind": "ADVISORY_EXCEPTION",
                            "component_id": item.component_id,
                            "release_sha": value.release_commit_sha,
                        },
                    )
                )
        for item in value.model_data_rights:
            entries.append(
                (
                    item.artifact_id,
                    item.artifact_hash,
                    "application/vnd.autotrade.rights-evidence",
                    {
                        "evidence_kind": "MODEL_DATA_RIGHTS",
                        "use_scope": item.use_scope,
                        "release_sha": value.release_commit_sha,
                    },
                )
            )

        for aid, digest, media_type, metadata in entries:
            if aid in omit_artifact_ids:
                continue
            _publish(
                store,
                artifact_id_value=aid,
                artifact_hash=digest,
                media_type=media_type,
                release_sha=value.release_commit_sha,
                metadata=metadata,
            )

        if corrupt_artifact_id is not None:
            manifest = store.load_manifest(corrupt_artifact_id)
            digest = manifest["sha256"].removeprefix("sha256:")
            (store.objects / digest[:2] / digest).write_bytes(b"corrupt")

        return qualify_supply_chain(value, evidence_store=store)



# Non-production RSA fixture shared only by this focused trust-integration test.
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


def _sign_attestation(value):
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


def _trust_root():
    return TrustRoot(
        producer_id="qualifier.supply-chain.service",
        verifier_id="autotrade.trust.verifier",
        public_modulus_hex=format(_RSA_N, "x"),
        public_exponent=65537,
        allowed_scopes=(QualificationScope("SUPPLY_CHAIN", "RELEASE"),),
        valid_from="2026-09-01T00:00:00Z",
    )


def _trust_policy(root):
    return QualificationTrustPolicy(
        policy_version="2026.09",
        roots=(root,),
    )


def _evidence_refs(value):
    refs = [
        EvidenceArtifactRef(
            value.sbom_artifact_id,
            value.sbom_hash,
            "application/vnd.autotrade.sbom",
            "SBOM",
            value.release_commit_sha,
        ),
        EvidenceArtifactRef(
            value.provenance_artifact_id,
            value.provenance_hash,
            "application/vnd.autotrade.provenance",
            "PROVENANCE",
            value.release_commit_sha,
        ),
        EvidenceArtifactRef(
            value.dependency_lock_artifact_id,
            value.dependency_lock_hash,
            "application/vnd.autotrade.dependency-lock",
            "DEPENDENCY_LOCK",
            value.release_commit_sha,
        ),
    ]
    for item in value.components:
        refs.append(
            EvidenceArtifactRef(
                item.artifact_id,
                item.observed_artifact_hash,
                "application/vnd.autotrade.distributed-component",
                "DISTRIBUTED_COMPONENT",
                value.release_commit_sha,
            )
        )
        if item.advisory_status == "ALLOWLISTED":
            refs.append(
                EvidenceArtifactRef(
                    item.advisory_exception_id,
                    item.advisory_exception_hash,
                    "application/vnd.autotrade.advisory-exception",
                    "ADVISORY_EXCEPTION",
                    value.release_commit_sha,
                )
            )
    for item in value.model_data_rights:
        refs.append(
            EvidenceArtifactRef(
                item.artifact_id,
                item.artifact_hash,
                "application/vnd.autotrade.rights-evidence",
                "MODEL_DATA_RIGHTS",
                value.release_commit_sha,
            )
        )
    return tuple(refs)


def _signed_review(value, *, refs=None, result="PASS"):
    root = _trust_root()
    attestation = QualificationAttestation(
        attestation_id=artifact_id("wp64-independent-review"),
        source_sha=value.release_commit_sha,
        domain="SUPPLY_CHAIN",
        gate="RELEASE",
        package_id="WP-64",
        protocol_id="supply-chain-review-v1",
        protocol_version="1.0.0",
        requirement_ids=("independent-supply-chain-review",),
        evidence_refs=_evidence_refs(value) if refs is None else tuple(refs),
        producer_id=root.producer_id,
        verifier_id=root.verifier_id,
        trust_root_id=root.root_id,
        runner_id="supply-chain-qualifier-1",
        harness_version="1.0.0",
        started_at="2026-09-25T20:00:00Z",
        completed_at="2026-09-25T20:05:00Z",
        signed_at="2026-09-25T20:06:00Z",
        result=result,
        unresolved_limits=() if result == "PASS" else ("review incomplete",),
    )
    return (
        SignedQualificationAttestation(attestation, _sign_attestation(attestation)),
        _trust_policy(root),
    )


def qualify_signed(value, *, receipt=None, policy=None):
    with TemporaryDirectory() as directory:
        store = ArtifactStore(directory)
        entries = [
            (
                value.sbom_artifact_id,
                value.sbom_hash,
                "application/vnd.autotrade.sbom",
                {"evidence_kind": "SBOM", "release_sha": value.release_commit_sha},
            ),
            (
                value.provenance_artifact_id,
                value.provenance_hash,
                "application/vnd.autotrade.provenance",
                {
                    "evidence_kind": "PROVENANCE",
                    "release_sha": value.release_commit_sha,
                },
            ),
            (
                value.dependency_lock_artifact_id,
                value.dependency_lock_hash,
                "application/vnd.autotrade.dependency-lock",
                {
                    "evidence_kind": "DEPENDENCY_LOCK",
                    "release_sha": value.release_commit_sha,
                },
            ),
        ]
        for item in value.components:
            entries.append(
                (
                    item.artifact_id,
                    item.observed_artifact_hash,
                    "application/vnd.autotrade.distributed-component",
                    {
                        "evidence_kind": "DISTRIBUTED_COMPONENT",
                        "component_id": item.component_id,
                        "version": item.version,
                        "release_sha": value.release_commit_sha,
                    },
                )
            )
        for item in value.model_data_rights:
            entries.append(
                (
                    item.artifact_id,
                    item.artifact_hash,
                    "application/vnd.autotrade.rights-evidence",
                    {
                        "evidence_kind": "MODEL_DATA_RIGHTS",
                        "use_scope": item.use_scope,
                        "release_sha": value.release_commit_sha,
                    },
                )
            )
        for aid, digest, media_type, metadata in entries:
            _publish(
                store,
                artifact_id_value=aid,
                artifact_hash=digest,
                media_type=media_type,
                release_sha=value.release_commit_sha,
                metadata=metadata,
            )
        if receipt is None or policy is None:
            receipt, policy = _signed_review(value)
        return qualify_supply_chain(
            value,
            evidence_store=store,
            trust_receipt=receipt,
            trust_policy=policy,
            expected_trust_policy_id=policy.policy_id,
            expected_trust_policy_version=policy.policy_version,
        )

class SupplyChainQualificationTests(unittest.TestCase):

    def test_valid_independent_signed_review_can_close_wp64_trust_gate(self):
        result = qualify_signed(evidence())
        self.assertEqual(result.status, "PASS")
        self.assertFalse(result.release_authority)
        self.assertIn(("independent_evidence_trust", "PASS"), result.checks)
        self.assertNotIn(
            "SUPPLY_CHAIN.TRUST_ANCHOR_UNAVAILABLE",
            result.reason_codes,
        )

    def test_signed_review_must_cover_exact_supply_chain_evidence_set(self):
        value = evidence()
        refs = _evidence_refs(value)[:-1]
        receipt, policy = _signed_review(value, refs=refs)
        result = qualify_signed(value, receipt=receipt, policy=policy)
        self.assertEqual(result.status, "FAIL")
        self.assertIn(
            "SUPPLY_CHAIN.TRUST_EVIDENCE_SET_MISMATCH",
            result.reason_codes,
        )

    def test_invalid_signature_is_terminal_trust_failure_not_self_approval(self):
        value = evidence()
        receipt, policy = _signed_review(value)
        forged = SignedQualificationAttestation(
            receipt.attestation,
            base64.b64encode(b"x" * 256).decode("ascii"),
        )
        result = qualify_signed(value, receipt=forged, policy=policy)
        self.assertEqual(result.status, "FAIL")
        self.assertIn(
            "SUPPLY_CHAIN.TRUST_ATTESTATION_INVALID",
            result.reason_codes,
        )

    def test_inconclusive_independent_review_cannot_produce_supply_chain_pass(self):
        value = evidence()
        receipt, policy = _signed_review(value, result="INCONCLUSIVE")
        result = qualify_signed(value, receipt=receipt, policy=policy)
        self.assertEqual(result.status, "INCONCLUSIVE")
        self.assertIn(
            "SUPPLY_CHAIN.INDEPENDENT_REVIEW_INCONCLUSIVE",
            result.reason_codes,
        )

    def test_self_asserted_release_hashes_are_inconclusive_without_external_evidence(self):
        result = qualify_supply_chain(evidence())
        self.assertEqual(result.status, "INCONCLUSIVE")
        self.assertFalse(result.release_authority)
        self.assertIn("SUPPLY_CHAIN.EVIDENCE_STORE_MISSING", result.reason_codes)

    def test_arbitrary_callback_cannot_self_approve_supply_chain(self):
        with self.assertRaisesRegex(TypeError, "evidence_store must be ArtifactStore"):
            qualify_supply_chain(
                evidence(),
                evidence_store=lambda _evidence: True,
            )

    def test_missing_or_corrupt_exact_artifact_fails_closed(self):
        value = evidence()
        missing = qualify(value, omit_artifact_ids={value.sbom_artifact_id})
        self.assertEqual(missing.status, "INCONCLUSIVE")
        self.assertIn(
            "SUPPLY_CHAIN.IMMUTABLE_ARTIFACT_UNVERIFIED:sbom",
            missing.reason_codes,
        )

        corrupt = qualify(value, corrupt_artifact_id=value.sbom_artifact_id)
        self.assertEqual(corrupt.status, "INCONCLUSIVE")
        self.assertIn(
            "SUPPLY_CHAIN.IMMUTABLE_ARTIFACT_UNVERIFIED:sbom",
            corrupt.reason_codes,
        )

    def test_caller_populated_store_proves_integrity_but_not_independent_trust(self):
        result = qualify(evidence())
        self.assertEqual(result.status, "INCONCLUSIVE")
        self.assertFalse(result.release_authority)
        self.assertIn(
            ("immutable_evidence_bundle", "PASS"),
            result.checks,
        )
        self.assertIn(
            ("independent_evidence_trust", "INCONCLUSIVE"),
            result.checks,
        )
        self.assertIn(
            "SUPPLY_CHAIN.TRUST_ANCHOR_UNAVAILABLE",
            result.reason_codes,
        )

    def test_unlicensed_component_blocks_release_qualification(self):
        result = qualify(evidence(component(license_status="BLOCKED")))
        self.assertEqual(result.status, "FAIL")
        self.assertIn("SUPPLY_CHAIN.LICENSE_BLOCKED:pkg:pypi/example@1.0", result.reason_codes)

    def test_changed_artifact_hash_blocks(self):
        result = qualify(evidence(component(observed_artifact_hash=H2)))
        self.assertEqual(result.status, "FAIL")
        self.assertIn("SUPPLY_CHAIN.ARTIFACT_HASH_MISMATCH:pkg:pypi/example@1.0", result.reason_codes)

    def test_missing_notice_blocks(self):
        result = qualify(evidence(component(notice_present=False)))
        self.assertEqual(result.status, "FAIL")
        self.assertIn("SUPPLY_CHAIN.MISSING_NOTICE:pkg:pypi/example@1.0", result.reason_codes)

    def test_blocking_advisory_blocks_and_unknown_is_inconclusive(self):
        blocked = qualify(evidence(component(advisory_status="BLOCKED")))
        unknown = qualify(evidence(component(advisory_status="UNKNOWN")))
        self.assertEqual(blocked.status, "FAIL")
        self.assertEqual(unknown.status, "INCONCLUSIVE")

    def test_allowlisted_advisory_requires_immutable_exception_evidence(self):
        with self.assertRaisesRegex(ValueError, "advisory_exception_id"):
            component(advisory_status="ALLOWLISTED")
        with self.assertRaisesRegex(ValueError, "advisory_exception_hash"):
            component(
                advisory_status="ALLOWLISTED",
                advisory_exception_id=EXCEPTION_ID,
            )
        with self.assertRaisesRegex(ValueError, "must be a UUID"):
            component(
                advisory_status="ALLOWLISTED",
                advisory_exception_id=" " + EXCEPTION_ID + " ",
                advisory_exception_hash=H2,
            )

        allowlisted = component(
            advisory_status="ALLOWLISTED",
            advisory_exception_id=EXCEPTION_ID,
            advisory_exception_hash=H2,
        )
        result = qualify(evidence(comp=allowlisted))
        self.assertEqual(result.status, "INCONCLUSIVE")
        self.assertIn(
            "SUPPLY_CHAIN.TRUST_ANCHOR_UNAVAILABLE",
            result.reason_codes,
        )

        changed = component(
            advisory_status="ALLOWLISTED",
            advisory_exception_id=EXCEPTION_ID_2,
            advisory_exception_hash=H2,
        )
        self.assertNotEqual(
            result.qualification_id,
            qualify(evidence(comp=changed)).qualification_id,
        )

    def test_non_allowlisted_advisory_rejects_stale_exception_fields(self):
        with self.assertRaisesRegex(ValueError, "only for ALLOWLISTED"):
            component(
                advisory_status="CLEAR",
                advisory_exception_id=EXCEPTION_ID,
                advisory_exception_hash=H2,
            )

    def test_architecture_time_review_cannot_approve_another_release(self):
        result = qualify(evidence(component(reviewed_for_release_sha="2" * 40)))
        self.assertEqual(result.status, "FAIL")
        self.assertIn("SUPPLY_CHAIN.STALE_REVIEW:pkg:pypi/example@1.0", result.reason_codes)

    def test_build_sha_must_equal_release_sha(self):
        result = qualify(evidence(built_from_commit_sha="3" * 40))
        self.assertEqual(result.status, "FAIL")
        self.assertIn("SUPPLY_CHAIN.BUILD_SHA_MISMATCH", result.reason_codes)

    def test_sbom_must_cover_exact_distributed_inventory(self):
        result = qualify(evidence(distributed_component_ids=("pkg:pypi/other@1.0",)))
        self.assertEqual(result.status, "FAIL")
        self.assertIn("SUPPLY_CHAIN.SBOM_INVENTORY_MISMATCH", result.reason_codes)

    def test_parsed_sbom_inventory_is_required_not_just_component_evidence(self):
        result = qualify(
            evidence(sbom_component_ids=("pkg:pypi/other@1.0",))
        )
        self.assertEqual(result.status, "FAIL")
        self.assertIn("SUPPLY_CHAIN.SBOM_INVENTORY_MISMATCH", result.reason_codes)

    def test_top_level_supply_chain_evidence_is_bound_to_exact_release(self):
        result = qualify(
            evidence(sbom_reviewed_for_release_sha="2" * 40)
        )
        self.assertEqual(result.status, "FAIL")
        self.assertIn("SUPPLY_CHAIN.STALE_SBOM_REVIEW", result.reason_codes)

    def test_missing_or_unknown_model_data_rights_cannot_pass(self):
        missing = qualify(evidence(model_rights=()))
        unknown = qualify(evidence(model_rights=(rights(rights_status="UNKNOWN"),)))
        self.assertEqual(missing.status, "INCONCLUSIVE")
        self.assertEqual(unknown.status, "INCONCLUSIVE")

    def test_hash_and_commit_identities_are_strictly_canonical(self):
        with self.assertRaises(ValueError):
            evidence(sbom_hash="sha256:" + "A" * 64)
        with self.assertRaises(ValueError):
            evidence(release_commit_sha="A" * 40)

    def test_notice_flags_require_actual_booleans(self):
        with self.assertRaises(TypeError):
            component(notice_required=1)
        with self.assertRaises(TypeError):
            component(notice_present="yes")

    def test_supply_chain_collections_are_typed_and_stable(self):
        with self.assertRaises(TypeError):
            SupplyChainEvidence(
                release_commit_sha=R,
                built_from_commit_sha=R,
                sbom_artifact_id=SBOM_ID,
                sbom_hash=H,
                provenance_artifact_id=PROVENANCE_ID,
                provenance_hash=H,
                dependency_lock_artifact_id=LOCK_ID,
                dependency_lock_hash=H,
                sbom_reviewed_for_release_sha=R,
                provenance_reviewed_for_release_sha=R,
                dependency_lock_reviewed_for_release_sha=R,
                distributed_component_ids=["pkg:pypi/example@1.0"],
                sbom_component_ids=("pkg:pypi/example@1.0",),
                components=(component(),),
                model_data_rights=(rights(),),
            )
        with self.assertRaises(TypeError):
            evidence(model_data_rights=(object(),))

    def test_qualification_identity_binds_exact_component_evidence(self):
        baseline = qualify(evidence())
        changed = qualify(
            evidence(comp=component(source_revision="commit:abcdef"))
        )
        self.assertEqual(baseline.status, changed.status)
        self.assertNotEqual(baseline.qualification_id, changed.qualification_id)

    def test_equivalent_rights_order_has_stable_identity(self):
        first = rights(artifact_id="model:a")
        second = rights(artifact_id="data:b", use_scope="train")
        left = qualify(evidence(model_rights=(first, second)))
        right = qualify(evidence(model_rights=(second, first)))
        self.assertEqual(left.status, right.status)
        self.assertEqual(left.checks, right.checks)
        self.assertEqual(left.reason_codes, right.reason_codes)
        self.assertEqual(left.qualification_id, right.qualification_id)

    def test_equivalent_component_and_inventory_order_has_stable_identity(self):
        first = component(
            component_id="pkg:pypi/a@1.0",
            artifact_id=artifact_id("component:a"),
        )
        second = component(
            component_id="pkg:pypi/b@1.0",
            artifact_id=artifact_id("component:b"),
        )
        left = qualify(
            evidence(
                comp=first,
                components=(first, second),
                distributed_component_ids=(first.component_id, second.component_id),
                sbom_component_ids=(first.component_id, second.component_id),
            )
        )
        right = qualify(
            evidence(
                comp=second,
                components=(second, first),
                distributed_component_ids=(second.component_id, first.component_id),
                sbom_component_ids=(second.component_id, first.component_id),
            )
        )
        self.assertEqual(left.status, right.status)
        self.assertEqual(left.qualification_id, right.qualification_id)


if __name__ == "__main__":
    unittest.main()
