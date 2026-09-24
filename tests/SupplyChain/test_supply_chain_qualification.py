import unittest

from mvp.autotrade_mvp.supply_chain_qualification import (
    ComponentEvidence,
    ModelDataRightsEvidence,
    SupplyChainEvidence,
    qualify_supply_chain,
)


R = "1" * 40
H = "sha256:" + "a" * 64
H2 = "sha256:" + "b" * 64


def component(**overrides):
    values = dict(
        component_id="pkg:pypi/example@1.0",
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
    values = dict(
        artifact_id="model:local",
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
        sbom_hash=H,
        provenance_hash=H,
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


class SupplyChainQualificationTests(unittest.TestCase):
    def test_exact_release_complete_evidence_passes_without_release_authority(self):
        result = qualify_supply_chain(evidence())
        self.assertEqual(result.status, "PASS")
        self.assertFalse(result.release_authority)

    def test_unlicensed_component_blocks_release_qualification(self):
        result = qualify_supply_chain(evidence(component(license_status="BLOCKED")))
        self.assertEqual(result.status, "FAIL")
        self.assertIn("SUPPLY_CHAIN.LICENSE_BLOCKED:pkg:pypi/example@1.0", result.reason_codes)

    def test_changed_artifact_hash_blocks(self):
        result = qualify_supply_chain(evidence(component(observed_artifact_hash=H2)))
        self.assertEqual(result.status, "FAIL")
        self.assertIn("SUPPLY_CHAIN.ARTIFACT_HASH_MISMATCH:pkg:pypi/example@1.0", result.reason_codes)

    def test_missing_notice_blocks(self):
        result = qualify_supply_chain(evidence(component(notice_present=False)))
        self.assertEqual(result.status, "FAIL")
        self.assertIn("SUPPLY_CHAIN.MISSING_NOTICE:pkg:pypi/example@1.0", result.reason_codes)

    def test_blocking_advisory_blocks_and_unknown_is_inconclusive(self):
        blocked = qualify_supply_chain(evidence(component(advisory_status="BLOCKED")))
        unknown = qualify_supply_chain(evidence(component(advisory_status="UNKNOWN")))
        self.assertEqual(blocked.status, "FAIL")
        self.assertEqual(unknown.status, "INCONCLUSIVE")

    def test_architecture_time_review_cannot_approve_another_release(self):
        result = qualify_supply_chain(evidence(component(reviewed_for_release_sha="2" * 40)))
        self.assertEqual(result.status, "FAIL")
        self.assertIn("SUPPLY_CHAIN.STALE_REVIEW:pkg:pypi/example@1.0", result.reason_codes)

    def test_build_sha_must_equal_release_sha(self):
        result = qualify_supply_chain(evidence(built_from_commit_sha="3" * 40))
        self.assertEqual(result.status, "FAIL")
        self.assertIn("SUPPLY_CHAIN.BUILD_SHA_MISMATCH", result.reason_codes)

    def test_sbom_must_cover_exact_distributed_inventory(self):
        result = qualify_supply_chain(evidence(distributed_component_ids=("pkg:pypi/other@1.0",)))
        self.assertEqual(result.status, "FAIL")
        self.assertIn("SUPPLY_CHAIN.SBOM_INVENTORY_MISMATCH", result.reason_codes)

    def test_parsed_sbom_inventory_is_required_not_just_component_evidence(self):
        result = qualify_supply_chain(
            evidence(sbom_component_ids=("pkg:pypi/other@1.0",))
        )
        self.assertEqual(result.status, "FAIL")
        self.assertIn("SUPPLY_CHAIN.SBOM_INVENTORY_MISMATCH", result.reason_codes)

    def test_top_level_supply_chain_evidence_is_bound_to_exact_release(self):
        result = qualify_supply_chain(
            evidence(sbom_reviewed_for_release_sha="2" * 40)
        )
        self.assertEqual(result.status, "FAIL")
        self.assertIn("SUPPLY_CHAIN.STALE_SBOM_REVIEW", result.reason_codes)

    def test_missing_or_unknown_model_data_rights_cannot_pass(self):
        missing = qualify_supply_chain(evidence(model_rights=()))
        unknown = qualify_supply_chain(evidence(model_rights=(rights(rights_status="UNKNOWN"),)))
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
                sbom_hash=H,
                provenance_hash=H,
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
        baseline = qualify_supply_chain(evidence())
        changed = qualify_supply_chain(
            evidence(comp=component(source_revision="commit:abcdef"))
        )
        self.assertEqual(baseline.status, changed.status)
        self.assertNotEqual(baseline.qualification_id, changed.qualification_id)

    def test_equivalent_rights_order_has_stable_identity(self):
        first = rights(artifact_id="model:a")
        second = rights(artifact_id="data:b", use_scope="train")
        left = qualify_supply_chain(evidence(model_rights=(first, second)))
        right = qualify_supply_chain(evidence(model_rights=(second, first)))
        self.assertEqual(left.status, right.status)
        self.assertEqual(left.qualification_id, right.qualification_id)


if __name__ == "__main__":
    unittest.main()
