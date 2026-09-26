from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import NAMESPACE_URL, uuid5

from research.autotrade_research.artifacts.store import ArtifactStore

from tools import check_product_completion as completion_gate
from mvp.autotrade_mvp import qualification_attestation as qualification_trust

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


def _initialize_exact_source_test_repo(root: Path) -> tuple[str, Path]:
    requirements = root / "qualification" / "nvda" / "requirements.json"
    requirements.parent.mkdir(parents=True, exist_ok=True)
    requirements.write_text('{"schema_version":"test"}\n', encoding="utf-8")
    subprocess.run(
        ["git", "init"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    subprocess.run(
        ["git", "add", "."],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=AutoTrade Test",
            "-c",
            "user.email=autotrade-test@example.invalid",
            "commit",
            "-m",
            "initial exact-source fixture",
        ],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return head, requirements


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
    records.extend(
        {
            "kind": "QUALIFICATION_GATE",
            "requirement_id": gate,
            "source_sha": SHA,
            "status": "PASS",
            "evidence_ref": f"artifact://whole-product/gate/{gate}",
        }
        for gate in sorted(EXPECTED_GATE_NAMES)
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
    ] + [
        ("QUALIFICATION_GATE", gate) for gate in sorted(EXPECTED_GATE_NAMES)
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
            requirement_ids=(f"{kind}/{requirement_id}",),
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
        "schema_version": "1.0.0",
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
        trust_policy,
    )


def nvda(*, qualified=True, source_sha=SHA):
    if not qualified:
        return {
            "schema_version": "1.0.0",
            "qualified": False,
            "source_sha": source_sha,
        }
    return {
        "schema_version": "1.0.0",
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

    def test_terminal_completion_rejects_caller_selected_trust_policy(self):
        with TemporaryDirectory() as directory:
            policy_path = Path(directory) / "hostile-policy.json"
            policy_path.write_text("{}", encoding="utf-8")
            result = subprocess.run(
                [
                    sys.executable,
                    "tools/check_product_completion.py",
                    "--qualification-policy",
                    str(policy_path),
                    "--expected-policy-id",
                    "sha256:" + "1" * 64,
                    "--expected-policy-version",
                    "attacker-controlled",
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
            )
        self.assertEqual(result.returncode, 2)
        self.assertIn(
            "caller-selected qualification trust policy is forbidden",
            result.stderr,
        )

    def test_terminal_completion_rejects_caller_selected_canonical_inputs(self):
        with TemporaryDirectory() as directory:
            alternate = Path(directory) / "alternate.json"
            alternate.write_text("{}", encoding="utf-8")
            for flag, label in (
                ("--spec", "product spec"),
                ("--bank", "work-package bank"),
                ("--qualification", "qualification"),
                ("--nvda-status", "NVDA status"),
            ):
                with self.subTest(flag=flag):
                    result = subprocess.run(
                        [
                            sys.executable,
                            "tools/check_product_completion.py",
                            flag,
                            str(alternate),
                        ],
                        cwd=ROOT,
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(result.returncode, 2)
                    self.assertIn(
                        "caller-selected canonical completion inputs are forbidden",
                        result.stderr,
                    )
                    self.assertIn(label, result.stderr)

    def test_terminal_completion_binds_source_sha_to_checkout_head(self):
        result = subprocess.run(
            [
                sys.executable,
                "tools/check_product_completion.py",
                "--source-sha",
                "a" * 40,
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn(
            "exact source SHA does not match checkout HEAD",
            result.stderr,
        )

    def test_exact_source_rejects_untracked_canonical_trust_policy_injection(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source_sha, requirements = _initialize_exact_source_test_repo(root)
            trust_policy = (
                root
                / "mvp"
                / "autotrade_mvp"
                / "qualification_trust_policy.json"
            )
            trust_policy.parent.mkdir(parents=True, exist_ok=True)
            trust_policy.write_text('{"attacker":"controlled"}\n', encoding="utf-8")

            with patch.object(completion_gate, "ROOT", root):
                with self.assertRaisesRegex(
                    ProductCompletionError,
                    "canonical completion inputs differ from exact source checkout",
                ):
                    completion_gate._verify_exact_source_checkout(
                        source_sha,
                        canonical_paths=(trust_policy, requirements),
                    )

    def test_exact_source_ignores_inherited_git_work_tree_redirect(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "source"
            root.mkdir()
            source_sha, requirements = _initialize_exact_source_test_repo(root)
            trust_policy = (
                root
                / "mvp"
                / "autotrade_mvp"
                / "qualification_trust_policy.json"
            )

            clean_tree = Path(directory) / "decoy-work-tree"
            clean_requirements = (
                clean_tree / "qualification" / "nvda" / "requirements.json"
            )
            clean_requirements.parent.mkdir(parents=True)
            clean_requirements.write_text(
                requirements.read_text(encoding="utf-8"),
                encoding="utf-8",
            )

            requirements.write_text(
                '{"schema_version":"weakened-real-work-tree"}\n',
                encoding="utf-8",
            )

            with (
                patch.object(completion_gate, "ROOT", root),
                patch.dict(
                    completion_gate.os.environ,
                    {"GIT_WORK_TREE": str(clean_tree)},
                    clear=False,
                ),
                self.assertRaisesRegex(
                    ProductCompletionError,
                    "canonical completion inputs differ from exact source checkout",
                ),
            ):
                completion_gate._verify_exact_source_checkout(
                    source_sha,
                    canonical_paths=(trust_policy, requirements),
                )
    def test_exact_source_git_resolution_ignores_candidate_path(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "source"
            root.mkdir()
            attacker_bin = Path(directory) / "attacker-bin"
            attacker_bin.mkdir()
            candidate_git = attacker_bin / ("git.exe" if sys.platform == "win32" else "git")
            candidate_git.write_bytes(b"candidate-controlled executable")
            trusted_git = Path(directory) / "os-managed-git"
            trusted_git.write_bytes(b"independently selected executable")

            with (
                patch.object(
                    qualification_trust,
                    "_trusted_git_candidate_paths",
                    return_value=(trusted_git,),
                ),
                patch.dict(
                    completion_gate.os.environ,
                    {"PATH": str(attacker_bin)},
                    clear=False,
                ),
            ):
                resolved = completion_gate._trusted_git_executable(source_root=root)

            self.assertEqual(Path(resolved).resolve(), trusted_git.resolve())
            self.assertNotEqual(Path(resolved).resolve(), candidate_git.resolve())

    def test_exact_source_git_environment_drops_loader_and_config_authority(self):
        hostile = {
            "PATH": "/attacker/bin",
            "HOME": "/attacker/home",
            "XDG_CONFIG_HOME": "/attacker/config",
            "LD_PRELOAD": "/attacker/libinject.so",
            "LD_LIBRARY_PATH": "/attacker/lib",
            "DYLD_INSERT_LIBRARIES": "/attacker/libinject.dylib",
            "PYTHONPATH": "/attacker/python",
            "GIT_OBJECT_DIRECTORY": "/attacker/objects",
            "GIT_ALTERNATE_OBJECT_DIRECTORIES": "/attacker/alternates",
            "SYSTEMROOT": r"C:\\Windows",
        }
        with patch.dict(completion_gate.os.environ, hostile, clear=True):
            environment = completion_gate._trusted_git_environment()

        self.assertEqual(environment["SYSTEMROOT"], r"C:\\Windows")
        self.assertEqual(environment["GIT_CONFIG_NOSYSTEM"], "1")
        self.assertEqual(environment["GIT_CONFIG_GLOBAL"], completion_gate.os.devnull)
        self.assertEqual(environment["GIT_NO_REPLACE_OBJECTS"], "1")
        for key in (
            "PATH",
            "HOME",
            "XDG_CONFIG_HOME",
            "LD_PRELOAD",
            "LD_LIBRARY_PATH",
            "DYLD_INSERT_LIBRARIES",
            "PYTHONPATH",
            "GIT_OBJECT_DIRECTORY",
            "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        ):
            self.assertNotIn(key, environment)

    def test_exact_source_rejects_canonical_symlink_substitution(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _source_sha, requirements = _initialize_exact_source_test_repo(root)
            decoy = requirements.with_name("decoy-requirements.json")
            decoy.write_text(requirements.read_text(encoding="utf-8"), encoding="utf-8")
            subprocess.run(
                ["git", "add", decoy.relative_to(root).as_posix()],
                cwd=root,
                capture_output=True,
                text=True,
                check=True,
            )
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=AutoTrade Test",
                    "-c",
                    "user.email=autotrade-test@example.invalid",
                    "commit",
                    "-m",
                    "add clean decoy requirements",
                ],
                cwd=root,
                capture_output=True,
                text=True,
                check=True,
            )
            source_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            requirements.unlink()
            try:
                requirements.symlink_to(decoy.name)
            except OSError as error:
                self.skipTest(f"working-tree symlink unavailable: {error}")

            with (
                patch.object(completion_gate, "ROOT", root),
                self.assertRaisesRegex(
                    ProductCompletionError,
                    "canonical completion inputs differ from exact source checkout",
                ),
            ):
                completion_gate._verify_exact_source_checkout(
                    source_sha,
                    canonical_paths=(requirements,),
                )

    def test_exact_source_rejects_dirty_nvda_requirements(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source_sha, requirements = _initialize_exact_source_test_repo(root)
            trust_policy = (
                root
                / "mvp"
                / "autotrade_mvp"
                / "qualification_trust_policy.json"
            )
            requirements.write_text(
                '{"schema_version":"weakened-working-tree"}\n',
                encoding="utf-8",
            )

            with patch.object(completion_gate, "ROOT", root):
                with self.assertRaisesRegex(
                    ProductCompletionError,
                    "canonical completion inputs differ from exact source checkout",
                ):
                    completion_gate._verify_exact_source_checkout(
                        source_sha,
                        canonical_paths=(trust_policy, requirements),
                    )

    def test_only_independently_verified_exact_matrix_can_report_complete(self):
        with TemporaryDirectory() as directory:
            (
                qualification,
                evidence_context,
                nvda_status,
                trust_policy,
            ) = verified_completion_fixture(directory)
            with patch.object(
                qualification_trust,
                "load_canonical_qualification_trust_policy",
                return_value=trust_policy,
            ):
                report = evaluate(
                    qualification=qualification,
                    nvda_status=nvda_status,
                    evidence_context=evidence_context,
                )
        self.assertTrue(report["complete"])
        self.assertEqual(report["blockers"], [])
        self.assertEqual(report["missing_section_evidence"], [])
        self.assertEqual(report["missing_package_evidence"], [])
        self.assertEqual(report["missing_gate_evidence"], [])
        self.assertEqual(report["nonpassing_evidence"], [])
        self.assertTrue(report["nvda_source_matches"])
        self.assertTrue(report["qualification_source_matches"])

    def test_well_formed_but_unverified_nvda_identity_cannot_complete(self):
        with TemporaryDirectory() as directory:
            (
                qualification,
                evidence_context,
                valid_nvda,
                trust_policy,
            ) = verified_completion_fixture(directory)

            policy_patch = patch.object(
                qualification_trust,
                "load_canonical_qualification_trust_policy",
                return_value=trust_policy,
            )
            policy_patch.start()
            self.addCleanup(policy_patch.stop)

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

    def test_terminal_gate_requires_independently_verified_gate_evidence(self):
        qualification = complete_qualification()
        qualification["whole_product_evidence"] = [
            item
            for item in qualification["whole_product_evidence"]
            if not (
                item["kind"] == "QUALIFICATION_GATE"
                and item["requirement_id"] == "economic_edge"
            )
        ]
        report = evaluate(qualification=qualification)
        self.assertFalse(report["complete"])
        self.assertIn("economic_edge", report["missing_gate_evidence"])

    def test_whole_product_receipt_binds_requirement_kind(self):
        with TemporaryDirectory() as directory:
            (
                qualification,
                evidence_context,
                nvda_status,
                trust_policy,
            ) = verified_completion_fixture(directory)

            target = next(
                item
                for item in qualification["whole_product_evidence"]
                if item["kind"] == "WORK_PACKAGE"
                and item["requirement_id"] == "WP-01"
            )
            target["kind"] = "PRODUCT_SECTION"
            target["requirement_id"] = "SECTION-01"
            qualification["whole_product_evidence"] = [
                item
                for item in qualification["whole_product_evidence"]
                if not (
                    item is not target
                    and item["kind"] == "PRODUCT_SECTION"
                    and item["requirement_id"] == "SECTION-01"
                )
            ]

            with patch.object(
                qualification_trust,
                "load_canonical_qualification_trust_policy",
                return_value=trust_policy,
            ):
                report = evaluate(
                    qualification=qualification,
                    nvda_status=nvda_status,
                    evidence_context=evidence_context,
                )
        self.assertFalse(report["complete"])
        self.assertIn(
            "PRODUCT_SECTION:SECTION-01:independent_verification",
            report["nonpassing_evidence"],
        )
        self.assertIn("WP-01", report["missing_package_evidence"])

    def test_completion_protocol_rejects_schema_version_drift(self):
        bank = complete_bank()
        bank["schema_version"] = "9.0.0"
        with self.assertRaisesRegex(
            ProductCompletionError,
            "unsupported work-package bank schema_version",
        ):
            evaluate(bank=bank)

        qualification = complete_qualification()
        qualification["schema_version"] = "9.0.0"
        with self.assertRaisesRegex(
            ProductCompletionError,
            "unsupported qualification schema_version",
        ):
            evaluate(qualification=qualification)

        legacy_in_progress = complete_qualification()
        legacy_in_progress["schema_version"] = "1.0.0"
        legacy_in_progress["overall_status"] = (
            "IMPLEMENTATION_IN_PROGRESS_SIMULATED_VERTICAL_SLICE_AVAILABLE"
        )
        report = evaluate(qualification=legacy_in_progress)
        self.assertFalse(report["complete"])

        legacy_terminal = complete_qualification()
        legacy_terminal["schema_version"] = "1.0.0"
        with self.assertRaisesRegex(
            ProductCompletionError,
            "FULL_PRODUCT_QUALIFIED requires qualification schema_version",
        ):
            evaluate(qualification=legacy_terminal)

        with TemporaryDirectory() as directory:
            (
                qualification,
                evidence_context,
                status,
                trust_policy,
            ) = verified_completion_fixture(directory)
            status["schema_version"] = "9.0.0"
            with patch.object(
                qualification_trust,
                "load_canonical_qualification_trust_policy",
                return_value=trust_policy,
            ):
                report = evaluate(
                    qualification=qualification,
                    nvda_status=status,
                    evidence_context=evidence_context,
                )
        self.assertFalse(report["nvda_qualified"])

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
                trust_policy,
            ) = verified_completion_fixture(directory)
            policy_patch = patch.object(
                qualification_trust,
                "load_canonical_qualification_trust_policy",
                return_value=trust_policy,
            )
            policy_patch.start()
            self.addCleanup(policy_patch.stop)
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
