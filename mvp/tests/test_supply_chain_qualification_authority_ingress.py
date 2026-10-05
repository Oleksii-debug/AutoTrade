import unittest

import mvp.autotrade_mvp.supply_chain_qualification as supply_module
from mvp.autotrade_mvp.qualification_attestation import (
    SignedQualificationAttestation,
)
from mvp.autotrade_mvp.supply_chain_qualification import (
    ComponentEvidence,
    ModelDataRightsEvidence,
    SupplyChainEvidence,
    SupplyChainQualification,
    qualify_supply_chain,
)
from mvp.tests.test_qualification_attestation import (
    attestation,
    root as attestation_root,
    sign,
)


_RELEASE_SHA = "a" * 40
_HASH_A = "sha256:" + "1" * 64
_HASH_B = "sha256:" + "2" * 64
_HASH_C = "sha256:" + "3" * 64
_HASH_D = "sha256:" + "4" * 64
_HASH_E = "sha256:" + "5" * 64


def component(**overrides):
    values = {
        "component_id": "core",
        "artifact_id": "00000000-0000-0000-0000-000000000001",
        "version": "1.0.0",
        "declared_artifact_hash": _HASH_A,
        "observed_artifact_hash": _HASH_A,
        "source_revision": "core-v1",
        "license_status": "APPROVED",
        "distribution_rights": "APPROVED",
        "advisory_status": "CLEAR",
        "notice_required": False,
        "notice_present": False,
        "reviewed_for_release_sha": _RELEASE_SHA,
    }
    values.update(overrides)
    return ComponentEvidence(**values)


def rights(**overrides):
    values = {
        "artifact_id": "00000000-0000-0000-0000-000000000005",
        "artifact_hash": _HASH_E,
        "use_scope": "runtime",
        "rights_status": "APPROVED",
        "reviewed_for_release_sha": _RELEASE_SHA,
    }
    values.update(overrides)
    return ModelDataRightsEvidence(**values)


def evidence(**overrides):
    values = {
        "release_commit_sha": _RELEASE_SHA,
        "built_from_commit_sha": _RELEASE_SHA,
        "sbom_artifact_id": "00000000-0000-0000-0000-000000000002",
        "sbom_hash": _HASH_B,
        "provenance_artifact_id": "00000000-0000-0000-0000-000000000003",
        "provenance_hash": _HASH_C,
        "dependency_lock_artifact_id": "00000000-0000-0000-0000-000000000004",
        "dependency_lock_hash": _HASH_D,
        "sbom_reviewed_for_release_sha": _RELEASE_SHA,
        "provenance_reviewed_for_release_sha": _RELEASE_SHA,
        "dependency_lock_reviewed_for_release_sha": _RELEASE_SHA,
        "distributed_component_ids": ("core",),
        "sbom_component_ids": ("core",),
        "components": (component(),),
        "model_data_rights": (rights(),),
    }
    values.update(overrides)
    return SupplyChainEvidence(**values)


class SupplyChainAuthorityIngressTests(unittest.TestCase):
    def test_git_sha_subclass_is_rejected_before_len_dispatch(self):
        touched: list[str] = []

        class HostileText(str):
            def __len__(self):
                touched.append("len")
                raise AssertionError("hostile supply-chain text length")

        with self.assertRaisesRegex(ValueError, "40-character lowercase"):
            evidence(release_commit_sha=HostileText(_RELEASE_SHA))
        self.assertEqual(touched, [])

    def test_status_subclass_is_rejected_before_membership_dispatch(self):
        touched: list[str] = []

        class HostileText(str):
            def __hash__(self):
                touched.append("hash")
                raise AssertionError("hostile supply-chain status hashing")

            def __eq__(self, other):
                touched.append("eq")
                raise AssertionError("hostile supply-chain status comparison")

        with self.assertRaisesRegex(ValueError, "license_status must be explicit"):
            component(license_status=HostileText("APPROVED"))
        self.assertEqual(touched, [])

    def test_tuple_subclass_is_rejected_before_iteration_dispatch(self):
        touched: list[str] = []

        class HostileTuple(tuple):
            def __iter__(self):
                touched.append("iter")
                raise AssertionError("hostile supply-chain inventory iteration")

        value = HostileTuple(("core",))
        with self.assertRaisesRegex(
            TypeError,
            "distributed_component_ids must be a tuple",
        ):
            evidence(distributed_component_ids=value)
        self.assertEqual(touched, [])

    def test_component_subclass_is_not_accepted_as_evidence_authority(self):
        class DerivedComponent(ComponentEvidence):
            pass

        exact = component()
        derived = DerivedComponent(
            component_id=exact.component_id,
            artifact_id=exact.artifact_id,
            version=exact.version,
            declared_artifact_hash=exact.declared_artifact_hash,
            observed_artifact_hash=exact.observed_artifact_hash,
            source_revision=exact.source_revision,
            license_status=exact.license_status,
            distribution_rights=exact.distribution_rights,
            advisory_status=exact.advisory_status,
            notice_required=exact.notice_required,
            notice_present=exact.notice_present,
            reviewed_for_release_sha=exact.reviewed_for_release_sha,
        )
        with self.assertRaisesRegex(
            TypeError,
            "components must be a tuple of ComponentEvidence",
        ):
            evidence(components=(derived,))

    def test_evidence_subclass_is_rejected_before_attribute_dispatch(self):
        touched: list[str] = []

        class DerivedEvidence(SupplyChainEvidence):
            def __getattribute__(self, name):
                if name == "release_commit_sha":
                    touched.append("release_commit_sha")
                return super().__getattribute__(name)

        exact = evidence()
        derived = DerivedEvidence(
            release_commit_sha=exact.release_commit_sha,
            built_from_commit_sha=exact.built_from_commit_sha,
            sbom_artifact_id=exact.sbom_artifact_id,
            sbom_hash=exact.sbom_hash,
            provenance_artifact_id=exact.provenance_artifact_id,
            provenance_hash=exact.provenance_hash,
            dependency_lock_artifact_id=exact.dependency_lock_artifact_id,
            dependency_lock_hash=exact.dependency_lock_hash,
            sbom_reviewed_for_release_sha=exact.sbom_reviewed_for_release_sha,
            provenance_reviewed_for_release_sha=exact.provenance_reviewed_for_release_sha,
            dependency_lock_reviewed_for_release_sha=(
                exact.dependency_lock_reviewed_for_release_sha
            ),
            distributed_component_ids=exact.distributed_component_ids,
            sbom_component_ids=exact.sbom_component_ids,
            components=exact.components,
            model_data_rights=exact.model_data_rights,
        )
        touched.clear()
        with self.assertRaisesRegex(TypeError, "evidence must be SupplyChainEvidence"):
            qualify_supply_chain(derived)
        self.assertEqual(touched, [])

    def test_receipt_subclass_is_rejected_before_trust_dispatch(self):
        trust_root = attestation_root()
        signed = attestation(trust_root)

        class DerivedReceipt(SignedQualificationAttestation):
            pass

        receipt = DerivedReceipt(signed, sign(signed))
        with self.assertRaisesRegex(
            TypeError,
            "trust_receipt must be SignedQualificationAttestation",
        ):
            qualify_supply_chain(evidence(), trust_receipt=receipt)

    def test_snapshot_mapping_subclass_is_rejected_before_get_dispatch(self):
        touched: list[str] = []

        class HostileManifest(dict):
            def get(self, *args, **kwargs):
                touched.append("get")
                raise AssertionError("hostile supply-chain manifest lookup")

        matched = supply_module._store_artifact_matches(
            lambda _artifact_id: (HostileManifest(), b"payload"),
            artifact_id="00000000-0000-0000-0000-000000000002",
            artifact_hash=_HASH_B,
            media_type="application/vnd.autotrade.sbom",
            release_sha=_RELEASE_SHA,
            metadata={},
        )
        self.assertFalse(matched)
        self.assertEqual(touched, [])

    def test_supply_chain_result_cannot_grant_release_authority(self):
        with self.assertRaisesRegex(
            ValueError,
            "never grants release authority",
        ):
            SupplyChainQualification(
                qualification_id="supply-forged",
                status="PASS",
                checks=(),
                reason_codes=(),
                release_authority=True,
            )

    def test_canonical_evidence_remains_non_authorizing(self):
        result = qualify_supply_chain(evidence())
        self.assertEqual(result.status, "INCONCLUSIVE")
        self.assertFalse(result.release_authority)


if __name__ == "__main__":
    unittest.main()
