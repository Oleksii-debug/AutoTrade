from hashlib import sha256
import unittest
from unittest.mock import patch

import mvp.autotrade_mvp.release_candidate as release_candidate_module
import mvp.autotrade_mvp.supply_chain_qualification as supply_chain_module
from mvp.autotrade_mvp.release_candidate import ReleaseArtifactEvidence
from mvp.autotrade_mvp.supply_chain_qualification import (
    ComponentEvidence,
    ModelDataRightsEvidence,
    SupplyChainEvidence,
)


SOURCE = "1" * 40
OTHER_SOURCE = "2" * 40


def _digest(char: str) -> str:
    return "sha256:" + char * 64


def _component(
    component_id: str,
    artifact_id: str,
    digest_char: str,
) -> ComponentEvidence:
    digest = _digest(digest_char)
    return ComponentEvidence(
        component_id=component_id,
        artifact_id=artifact_id,
        version="1.0.0",
        declared_artifact_hash=digest,
        observed_artifact_hash=digest,
        source_revision="git:" + SOURCE,
        license_status="APPROVED",
        distribution_rights="APPROVED",
        advisory_status="CLEAR",
        notice_required=False,
        notice_present=True,
        reviewed_for_release_sha=SOURCE,
    )


def _rights(
    artifact_id: str,
    digest_char: str,
    use_scope: str,
) -> ModelDataRightsEvidence:
    return ModelDataRightsEvidence(
        artifact_id=artifact_id,
        artifact_hash=_digest(digest_char),
        use_scope=use_scope,
        rights_status="APPROVED",
        reviewed_for_release_sha=SOURCE,
    )


def _evidence(
    *,
    distributed_component_ids,
    sbom_component_ids,
    components,
    rights,
) -> SupplyChainEvidence:
    return SupplyChainEvidence(
        release_commit_sha=SOURCE,
        built_from_commit_sha=SOURCE,
        sbom_artifact_id="11111111-1111-4111-8111-111111111111",
        sbom_hash=_digest("a"),
        provenance_artifact_id="22222222-2222-4222-8222-222222222222",
        provenance_hash=_digest("b"),
        dependency_lock_artifact_id="33333333-3333-4333-8333-333333333333",
        dependency_lock_hash=_digest("c"),
        sbom_reviewed_for_release_sha=SOURCE,
        provenance_reviewed_for_release_sha=SOURCE,
        dependency_lock_reviewed_for_release_sha=SOURCE,
        distributed_component_ids=distributed_component_ids,
        sbom_component_ids=sbom_component_ids,
        components=components,
        model_data_rights=rights,
        release_artifact_id="44444444-4444-4444-8444-444444444444",
        release_artifact_sha256=_digest("d"),
    )


class ReleaseEvidenceTrustRegressionTests(unittest.TestCase):
    def test_supply_chain_payload_is_order_independent_without_deduplication(self):
        component_a = _component(
            "component-a",
            "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "1",
        )
        component_b = _component(
            "component-b",
            "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
            "2",
        )
        rights_a = _rights(
            "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
            "3",
            "model-a",
        )
        rights_b = _rights(
            "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
            "4",
            "model-b",
        )

        forward = _evidence(
            distributed_component_ids=("component-a", "component-b"),
            sbom_component_ids=("component-a", "component-b"),
            components=(component_a, component_b),
            rights=(rights_a, rights_b),
        )
        reversed_order = _evidence(
            distributed_component_ids=("component-b", "component-a"),
            sbom_component_ids=("component-b", "component-a"),
            components=(component_b, component_a),
            rights=(rights_b, rights_a),
        )

        forward_payload = supply_chain_module._supply_chain_evidence_payload(
            forward
        )
        reversed_payload = supply_chain_module._supply_chain_evidence_payload(
            reversed_order
        )

        self.assertEqual(forward_payload, reversed_payload)
        self.assertEqual(
            forward_payload["distributed_component_ids"],
            ["component-a", "component-b"],
        )
        self.assertEqual(
            [item["component_id"] for item in forward_payload["components"]],
            ["component-a", "component-b"],
        )
        self.assertEqual(
            [item["artifact_id"] for item in forward_payload["model_data_rights"]],
            sorted((rights_a.artifact_id, rights_b.artifact_id)),
        )

        with self.assertRaises(ValueError):
            _evidence(
                distributed_component_ids=("component-a", "component-a"),
                sbom_component_ids=("component-a", "component-b"),
                components=(component_a, component_b),
                rights=(rights_a, rights_b),
            )

    def test_supply_chain_proof_parses_the_same_bytes_that_were_verified(self):
        verified_bytes = b"verified durable WP-64 proof"
        replacement_bytes = b"unverified replacement proof"
        artifact = ReleaseArtifactEvidence.create(
            role="DEPENDENCY_RIGHTS",
            artifact_id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
            artifact_sha256=(
                "sha256:" + sha256(verified_bytes).hexdigest()
            ),
            source_sha=SOURCE,
            signature_status="NOT_APPLICABLE",
            evidence_status="PASS",
        )
        manifest = {
            "manifest_hash": _digest("f"),
            "sha256": artifact.artifact_sha256,
            "media_type": supply_chain_module.SUPPLY_CHAIN_PROOF_MEDIA_TYPE,
            "source_refs": [f"git:{SOURCE}"],
            "metadata": {
                "evidence_kind": (
                    supply_chain_module.SUPPLY_CHAIN_PROOF_EVIDENCE_KIND
                ),
                "role": artifact.role,
                "source_sha": SOURCE,
                "signature_status": artifact.signature_status,
                "evidence_status": artifact.evidence_status,
            },
        }
        reads = 0

        def flipping_reader(_artifact_id):
            nonlocal reads
            reads += 1
            if reads == 1:
                return manifest, verified_bytes
            return manifest, replacement_bytes

        parsed_evidence = type(
            "ParsedEvidence",
            (),
            {"release_commit_sha": SOURCE},
        )()
        parsed = (parsed_evidence, object())
        with patch.object(
            release_candidate_module,
            "parse_supply_chain_proof_bytes",
            return_value=parsed,
        ) as parser:
            result = release_candidate_module._load_supply_chain_proof(
                flipping_reader,
                artifact,
            )

        self.assertEqual(result, parsed)
        self.assertEqual(reads, 1)
        parser.assert_called_once_with(verified_bytes)

    def test_supply_chain_proof_rejects_cross_source_signed_payload(self):
        verified_bytes = b"verified durable WP-64 proof for a different source"
        artifact = ReleaseArtifactEvidence.create(
            role="DEPENDENCY_RIGHTS",
            artifact_id="ffffffff-ffff-4fff-8fff-ffffffffffff",
            artifact_sha256=(
                "sha256:" + sha256(verified_bytes).hexdigest()
            ),
            source_sha=SOURCE,
            signature_status="NOT_APPLICABLE",
            evidence_status="PASS",
        )
        manifest = {
            "manifest_hash": _digest("e"),
            "sha256": artifact.artifact_sha256,
            "media_type": supply_chain_module.SUPPLY_CHAIN_PROOF_MEDIA_TYPE,
            "source_refs": [f"git:{SOURCE}"],
            "metadata": {
                "evidence_kind": (
                    supply_chain_module.SUPPLY_CHAIN_PROOF_EVIDENCE_KIND
                ),
                "role": artifact.role,
                "source_sha": SOURCE,
                "signature_status": artifact.signature_status,
                "evidence_status": artifact.evidence_status,
            },
        }
        wrong_source_evidence = type(
            "ParsedEvidence",
            (),
            {"release_commit_sha": OTHER_SOURCE},
        )()

        with patch.object(
            release_candidate_module,
            "parse_supply_chain_proof_bytes",
            return_value=(wrong_source_evidence, object()),
        ):
            with self.assertRaisesRegex(
                release_candidate_module.ReleaseCandidateError,
                "source SHA does not match dependency-rights artifact",
            ):
                release_candidate_module._load_supply_chain_proof(
                    lambda _artifact_id: (manifest, verified_bytes),
                    artifact,
                )


if __name__ == "__main__":
    unittest.main()
