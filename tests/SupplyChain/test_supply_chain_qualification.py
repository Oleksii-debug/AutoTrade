from hashlib import sha256
from tempfile import TemporaryDirectory
import unittest
from uuid import NAMESPACE_URL, uuid5

from research.autotrade_research.artifacts.store import ArtifactStore

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


class SupplyChainQualificationTests(unittest.TestCase):
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
