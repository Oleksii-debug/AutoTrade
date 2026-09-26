from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from uuid import NAMESPACE_URL, uuid5

from research.autotrade_research.artifacts.store import ArtifactStore

from mvp.autotrade_mvp.qualification_attestation import (
    EvidenceArtifactRef,
    QualificationAttestation,
    QualificationScope,
    SignedQualificationAttestation,
)
from mvp.tests.test_nvda_qualification_gate import (
    REQUIREMENTS as NVDA_REQUIREMENTS,
    complete_evidence as complete_nvda_evidence,
    write_release_bundle,
)
from mvp.tests.test_qualification_attestation import (
    policy as fixture_policy,
    root as fixture_root,
    sign as fixture_sign,
)
from tools.check_product_completion import (
    EXPECTED_GATE_NAMES,
    EXPECTED_PACKAGE_IDS,
    EXPECTED_SECTION_IDS,
    ProductCompletionError,
    WholeProductEvidenceContext,
    evaluate_completion,
)


ROOT = Path(__file__).resolve().parents[2]
SPEC = (ROOT / "docs/product/PRODUCT_SPEC_CANONICAL.txt").read_text(encoding="utf-8")
SHA = "a" * 40
EVIDENCE_MEDIA_TYPE = "application/vnd.autotrade.whole-product-evidence"
EVIDENCE_KIND = "WHOLE_PRODUCT_QUALIFICATION"


def complete_bank():
    return {
        "schema_version": "1.0.0",
        "packages": [
            {"id": package_id, "proposed_status": "DONE"}
            for package_id in EXPECTED_PACKAGE_IDS
        ],
    }


def complete_evidence():
    records = [
        {
            "kind": "PRODUCT_SECTION",
            "requirement_id": section,
            "source_sha": SHA,
            "status": "PASS",
            "evidence_ref": f"artifact://whole-product/{section.lower()}",
        }
        for section in EXPECTED_SECTION_IDS
    ]
    records.extend(
        {
            "kind": "WORK_PACKAGE",
            "requirement_id": package,
            "source_sha": SHA,
            "status": "PASS",
            "evidence_ref": f"artifact://whole-product/{package.lower()}",
        }
        for package in EXPECTED_PACKAGE_IDS
    )
    return records


def complete_qualification(evidence=None):
    return {
        "schema_version": "2.0.0",
        "source_sha": SHA,
        "overall_status": "FULL_PRODUCT_QUALIFIED",
        "gates": {name: "QUALIFIED" for name in EXPECTED_GATE_NAMES},
        "whole_product_evidence": complete_evidence() if evidence is None else evidence,
    }


def verified_evidence(store, trust_root):
    records = []
    requirements = [
        ("PRODUCT_SECTION", section) for section in EXPECTED_SECTION_IDS
    ] + [
        ("WORK_PACKAGE", package) for package in EXPECTED_PACKAGE_IDS
    ]
    for kind, requirement_id in requirements:
        artifact_id = str(
            uuid5(
                NAMESPACE_URL,
                f"whole-product-artifact:{kind}:{requirement_id}",
            )
        )
        payload = f"verified:{kind}:{requirement_id}".encode("utf-8")
        manifest = store.publish_bytes(
            artifact_id=artifact_id,
            data=payload,
            media_type=EVIDENCE_MEDIA_TYPE,
            rights={"storage": True, "export": False},
            source_refs=[f"git:{SHA}"],
            metadata={"evidence_kind": EVIDENCE_KIND},
        )
        evidence_ref = EvidenceArtifactRef(
            artifact_id=artifact_id,
            sha256=manifest["sha256"],
            media_type=EVIDENCE_MEDIA_TYPE,
            evidence_kind=EVIDENCE_KIND,
            source_sha=SHA,
        )
        attestation = QualificationAttestation(
            attestation_id=str(
                uuid5(
                    NAMESPACE_URL,
                    f"whole-product-attestation:{kind}:{requirement_id}",
                )
            ),
            source_sha=SHA,
            domain="WHOLE_PRODUCT",
            gate="COMPLETION",
            package_id="WP-60",
            protocol_id="whole-product-completion-v1",
            protocol_version="1.0.0",
            requirement_ids=(requirement_id,),
            evidence_refs=(evidence_ref,),
            producer_id=trust_root.producer_id,
            verifier_id=trust_root.verifier_id,
            trust_root_id=trust_root.root_id,
            runner_id="whole-product-test-runner",
            harness_version="1.0.0",
            started_at="2026-09-25T02:00:00Z",
            completed_at="2026-09-25T02:10:00Z",
            signed_at="2026-09-25T02:11:00Z",
            result="PASS",
        )
        receipt = SignedQualificationAttestation(
            attestation=attestation,
            signature_b64=fixture_sign(attestation),
        )
        records.append(
            {
                "kind": kind,
                "requirement_id": requirement_id,
                "source_sha": SHA,
                "status": "PASS",
                "evidence_ref": attestation.attestation_id,
                "receipt": {
                    "attestation": attestation.canonical_payload(),
                    "signature_b64": receipt.signature_b64,
                },
            }
        )
    return records


def verified_nvda_fixture(store, trust_root, trust_policy, release_artifact):
    write_release_bundle(release_artifact, source_sha=SHA)
    evidence = complete_nvda_evidence()
    evidence["source_sha"] = SHA
    evidence["artifact_sha256"] = (
        "sha256:" + sha256(release_artifact.read_bytes()).hexdigest()
    )
    evidence_bytes = json.dumps(
        evidence,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    artifact_id = str(uuid5(NAMESPACE_URL, "whole-product-nvda-raw-evidence"))
    manifest = store.publish_bytes(
        artifact_id=artifact_id,
        data=evidence_bytes,
        media_type="application/vnd.autotrade.nvda-evidence+json",
        rights={"storage": True, "export": False},
        source_refs=[f"git:{SHA}"],
        metadata={"evidence_kind": "NVDA_REAL_RUN"},
    )
    evidence_ref = EvidenceArtifactRef(
        artifact_id=artifact_id,
        sha256=manifest["sha256"],
        media_type="application/vnd.autotrade.nvda-evidence+json",
        evidence_kind="NVDA_REAL_RUN",
        source_sha=SHA,
    )
    requirement_ids = tuple(
        sorted(item["id"] for item in NVDA_REQUIREMENTS["workflows"])
    )
    attestation = QualificationAttestation(
        attestation_id=str(uuid5(NAMESPACE_URL, "whole-product-nvda-attestation")),
        source_sha=SHA,
        domain="ACCESSIBILITY",
        gate="NVDA_RELEASE",
        package_id="WP-53",
        protocol_id="real-nvda-keyboard-v1",
        protocol_version="1.0.0",
        requirement_ids=requirement_ids,
        evidence_refs=(evidence_ref,),
        producer_id=trust_root.producer_id,
        verifier_id=trust_root.verifier_id,
        trust_root_id=trust_root.root_id,
        runner_id="nvda-test-runner",
        harness_version="1.0.0",
        started_at="2026-09-25T03:00:00Z",
        completed_at="2026-09-25T03:10:00Z",
        signed_at="2026-09-25T03:11:00Z",
        result="PASS",
        release_artifact_id=evidence["release_artifact_id"],
        release_artifact_sha256=evidence["artifact_sha256"],
    )
    receipt = SignedQualificationAttestation(
        attestation=attestation,
        signature_b64=fixture_sign(attestation),
    )
    status = {
        "qualified": True,
        "reason": "QUALIFIED_SIGNED_REAL_NVDA_RELEASE",
        "source_sha": SHA,
        "release_artifact_id": evidence["release_artifact_id"],
        "artifact_sha256": evidence["artifact_sha256"],
        "evidence_sha256": manifest["sha256"],
        "attestation_id": attestation.attestation_id,
        "attestation_digest": attestation.content_digest,
        "policy_id": trust_policy.policy_id,
        "trust_root_id": trust_root.root_id,
    }
    return status, receipt


def verified_completion_fixture(directory):
    store = ArtifactStore(directory)
    trust_root = fixture_root(
        scopes=(
            QualificationScope("WHOLE_PRODUCT", "COMPLETION"),
            QualificationScope("ACCESSIBILITY", "NVDA_RELEASE"),
        )
    )
    trust_policy = fixture_policy(trust_root)
    release_artifact = Path(directory) / "AutoTrade-release.zip"
    nvda_status, nvda_receipt = verified_nvda_fixture(
        store,
        trust_root,
        trust_policy,
        release_artifact,
    )
    context = WholeProductEvidenceContext(
        evidence_store=store,
        policy=trust_policy,
        expected_policy_id=trust_policy.policy_id,
        expected_policy_version=trust_policy.policy_version,
        nvda_receipt=nvda_receipt,
        nvda_requirements_json=json.dumps(
            NVDA_REQUIREMENTS,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ),
        nvda_release_artifact=release_artifact,
    )
    return (
        complete_qualification(verified_evidence(store, trust_root)),
        context,
        nvda_status,
    )


def nvda(*, qualified=True, source_sha=SHA):
    if not qualified:
        return {"qualified": False, "source_sha": source_sha}
    return {
        "qualified": True,
        "reason": "QUALIFIED_SIGNED_REAL_NVDA_RELEASE",
        "source_sha": source_sha,
        "release_artifact_id": "11111111-1111-4111-8111-111111111111",
        "artifact_sha256": "sha256:" + "1" * 64,
        "evidence_sha256": "sha256:" + "2" * 64,
        "attestation_id": "22222222-2222-4222-8222-222222222222",
        "attestation_digest": "sha256:" + "3" * 64,
        "policy_id": "sha256:" + "4" * 64,
        "trust_root_id": "sha256:" + "5" * 64,
    }


def evaluate(
    bank=None,
    qualification=None,
    nvda_status=None,
    source_sha=SHA,
    evidence_context=None,
):
    return evaluate_completion(
        bank or complete_bank(),
        qualification or complete_qualification(),
        nvda_status or nvda(),
        spec_text=SPEC,
        exact_source_sha=source_sha,
        evidence_context=evidence_context,
    )


class ProductCompletionGateTests(unittest.TestCase):
    def test_current_repository_cannot_be_misreported_as_complete(self):
        result = subprocess.run(
            [
                sys.executable,
                "tools/check_product_completion.py",
                "--require-complete",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 3, result.stderr)
        report = json.loads(result.stdout)
        self.assertFalse(report["complete"])
        self.assertEqual(report["product_section_count"], 40)
        self.assertEqual(report["package_count"], 65)
        self.assertGreater(len(report["incomplete_packages"]), 0)
        self.assertGreater(len(report["nonterminal_gates"]), 0)
        self.assertFalse(report["nvda_qualified"])

    def test_self_asserted_complete_matrix_cannot_report_complete(self):
        report = evaluate()
        self.assertFalse(report["complete"])
        self.assertTrue(
            all(
                item.endswith(":independent_verification")
                for item in report["nonpassing_evidence"]
            )
        )

    def test_only_independently_verified_exact_matrix_can_report_complete(self):
        with TemporaryDirectory() as directory:
            (
                qualification,
                evidence_context,
                nvda_status,
            ) = verified_completion_fixture(directory)
            report = evaluate(
                qualification=qualification,
                nvda_status=nvda_status,
                evidence_context=evidence_context,
            )
        self.assertTrue(report["complete"])
        self.assertEqual(report["blockers"], [])
        self.assertEqual(report["missing_section_evidence"], [])
        self.assertEqual(report["missing_package_evidence"], [])
        self.assertEqual(report["nonpassing_evidence"], [])
        self.assertTrue(report["nvda_source_matches"])
        self.assertTrue(report["qualification_source_matches"])

    def test_well_formed_but_unverified_nvda_identity_cannot_complete(self):
        with TemporaryDirectory() as directory:
            (
                qualification,
                evidence_context,
                valid_nvda,
            ) = verified_completion_fixture(directory)

            forged = nvda()
            report = evaluate(
                qualification=qualification,
                nvda_status=forged,
                evidence_context=evidence_context,
            )
            self.assertFalse(report["complete"])
            self.assertFalse(report["nvda_qualified"])

            forged = dict(valid_nvda)
            forged["attestation_id"] = "33333333-3333-4333-8333-333333333333"
            report = evaluate(
                qualification=qualification,
                nvda_status=forged,
                evidence_context=evidence_context,
            )
            self.assertFalse(report["nvda_qualified"])

            forged = dict(valid_nvda)
            forged["evidence_sha256"] = "sha256:" + "f" * 64
            report = evaluate(
                qualification=qualification,
                nvda_status=forged,
                evidence_context=evidence_context,
            )
            self.assertFalse(report["nvda_qualified"])

            forged = dict(valid_nvda)
            forged["artifact_sha256"] = "sha256:" + "e" * 64
            report = evaluate(
                qualification=qualification,
                nvda_status=forged,
                evidence_context=evidence_context,
            )
            self.assertFalse(report["nvda_qualified"])

    def test_product_spec_toc_cannot_mask_missing_or_renamed_body_section(self):
        marker = "28. Accessible desktop and web interfaces"
        prefix, separator, suffix = SPEC.rpartition(marker)
        self.assertTrue(separator)

        missing_body = prefix + suffix
        with self.assertRaisesRegex(
            ProductCompletionError,
            "table of contents and one 1..40 body heading sequence",
        ):
            evaluate_completion(
                complete_bank(),
                complete_qualification(),
                nvda(),
                spec_text=missing_body,
                exact_source_sha=SHA,
            )

        renamed_body = prefix + "28. Renamed inaccessible body section" + suffix
        with self.assertRaisesRegex(
            ProductCompletionError,
            "table of contents does not match body headings",
        ):
            evaluate_completion(
                complete_bank(),
                complete_qualification(),
                nvda(),
                spec_text=renamed_body,
                exact_source_sha=SHA,
            )

    def test_one_unfinished_package_blocks_whole_product_completion(self):
        bank = complete_bank()
        bank["packages"][31]["proposed_status"] = "IN_PROGRESS"
        report = evaluate(bank=bank)
        self.assertFalse(report["complete"])
        self.assertIn("WP-32", report["incomplete_packages"])

    def test_missing_required_gate_cannot_disappear_from_completion_scope(self):
        qualification = complete_qualification()
        qualification["gates"].pop("economic_edge")
        report = evaluate(qualification=qualification)
        self.assertFalse(report["complete"])
        self.assertIn("economic_edge", report["missing_required_gates"])

    def test_unknown_qualification_gate_is_protocol_error(self):
        qualification = complete_qualification()
        qualification["gates"]["future_unreviewed_gate"] = "QUALIFIED"
        with self.assertRaisesRegex(
            ProductCompletionError,
            "qualification gates contain unknown entries",
        ):
            evaluate(qualification=qualification)

    def test_unproven_economic_edge_or_release_blocks_completion(self):
        qualification = complete_qualification()
        qualification["gates"]["economic_edge"] = "UNPROVEN"
        qualification["gates"]["release"] = "NOT_STARTED"
        report = evaluate(qualification=qualification)
        self.assertFalse(report["complete"])
        self.assertEqual(report["nonterminal_gates"]["economic_edge"], "UNPROVEN")
        self.assertEqual(report["nonterminal_gates"]["release"], "NOT_STARTED")

    def test_missing_or_duplicate_work_package_is_protocol_error(self):
        missing = complete_bank()
        missing["packages"].pop()
        with self.assertRaisesRegex(ProductCompletionError, "exactly WP-01"):
            evaluate(bank=missing)

        duplicated = complete_bank()
        duplicated["packages"].append(dict(duplicated["packages"][0]))
        with self.assertRaisesRegex(ProductCompletionError, "duplicate"):
            evaluate(bank=duplicated)

    def test_each_product_section_and_package_needs_exact_source_pass_evidence(self):
        qualification = complete_qualification()
        qualification["whole_product_evidence"] = [
            item
            for item in qualification["whole_product_evidence"]
            if not (
                item["kind"] == "PRODUCT_SECTION"
                and item["requirement_id"] == "SECTION-07"
            )
        ]
        package = next(
            item
            for item in qualification["whole_product_evidence"]
            if item["kind"] == "WORK_PACKAGE" and item["requirement_id"] == "WP-33"
        )
        package["source_sha"] = "b" * 40
        report = evaluate(qualification=qualification)
        self.assertFalse(report["complete"])
        self.assertIn("SECTION-07", report["missing_section_evidence"])
        self.assertIn(
            "WORK_PACKAGE:WP-33:source_sha",
            report["nonpassing_evidence"],
        )

    def test_duplicate_or_unknown_requirement_evidence_is_protocol_error(self):
        qualification = complete_qualification()
        qualification["whole_product_evidence"].append(
            dict(qualification["whole_product_evidence"][0])
        )
        with self.assertRaisesRegex(ProductCompletionError, "duplicate"):
            evaluate(qualification=qualification)

        qualification = complete_qualification()
        qualification["whole_product_evidence"][0]["requirement_id"] = "SECTION-99"
        with self.assertRaisesRegex(ProductCompletionError, "unknown requirement"):
            evaluate(qualification=qualification)

    def test_nvda_must_be_real_terminal_evidence_on_same_source(self):
        report = evaluate(nvda_status={"qualified": "true", "source_sha": SHA})
        self.assertFalse(report["complete"])
        self.assertFalse(report["nvda_qualified"])

        report = evaluate(nvda_status=nvda(source_sha="b" * 40))
        self.assertFalse(report["complete"])
        self.assertFalse(report["nvda_source_matches"])

    def test_nvda_completion_requires_signed_release_identity(self):
        with TemporaryDirectory() as directory:
            (
                qualification,
                evidence_context,
                valid_status,
            ) = verified_completion_fixture(directory)
            required_fields = (
                "release_artifact_id",
                "artifact_sha256",
                "evidence_sha256",
                "attestation_id",
                "attestation_digest",
                "policy_id",
                "trust_root_id",
            )
            for field in required_fields:
                with self.subTest(field=field):
                    status = dict(valid_status)
                    status.pop(field)
                    report = evaluate(
                        qualification=qualification,
                        nvda_status=status,
                        evidence_context=evidence_context,
                    )
                    self.assertFalse(report["nvda_qualified"])

            status = dict(valid_status)
            status["reason"] = "QUALIFIED"
            report = evaluate(
                qualification=qualification,
                nvda_status=status,
                evidence_context=evidence_context,
            )
            self.assertFalse(report["nvda_qualified"])

    def test_missing_or_noncanonical_exact_source_sha_blocks_completion(self):
        report = evaluate(source_sha=None)
        self.assertFalse(report["complete"])
        self.assertIsNone(report["exact_source_sha"])
        self.assertTrue(
            any("exact source SHA" in blocker for blocker in report["blockers"])
        )

    def test_qualification_cannot_self_assert_or_mismatch_exact_source(self):
        qualification = complete_qualification()
        qualification["source_sha"] = "b" * 40
        report = evaluate(qualification=qualification, source_sha=SHA)
        self.assertFalse(report["complete"])
        self.assertFalse(report["qualification_source_matches"])
        self.assertIn(
            "qualification source SHA is missing, non-canonical, or not the exact source SHA",
            report["blockers"],
        )

        qualification["source_sha"] = "NOT_A_SHA"
        report = evaluate(qualification=qualification, source_sha=SHA)
        self.assertFalse(report["qualification_source_matches"])


if __name__ == "__main__":
    unittest.main()
