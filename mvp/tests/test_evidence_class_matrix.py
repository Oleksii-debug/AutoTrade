"""Plan-7 Section-6 evidence-class matrix: negative/promotion/replay contracts."""
import json
from dataclasses import replace
import unittest

from mvp.autotrade_mvp.evidence_class_matrix import (
    EVIDENCE_CLASSES, EvidenceMatrixError, EvidenceReference, PLAN9_OWNED,
    REQUIRED_EVIDENCE, SOURCE, REPLAY, SIMULATION, PAPER, LIVE,
    TARGET_HOST_PHYSICAL, NVDA_PHYSICAL, SIGNED_RELEASE,
    capability_requirements, inspect_capability_evidence, matrix_fingerprint,
)

SHA = "a" * 40
OTHER_SHA = "b" * 40
DIGEST = "sha256:" + "c" * 64


def ref(capability, evidence_class, *, source_sha=SHA, claimed_status="PASS",
        evidence_ref="artifact:1", issuer="untrusted-test"):
    return EvidenceReference(
        capability=capability, evidence_class=evidence_class,
        source_sha=source_sha, content_digest=DIGEST,
        evidence_ref=evidence_ref, issuer=issuer, claimed_status=claimed_status,
    )


class EvidenceClassMatrixTests(unittest.TestCase):
    def test_inventory_is_deterministic_versioned_and_machine_readable(self):
        a = capability_requirements()
        b = capability_requirements()
        self.assertEqual(a, b)
        self.assertEqual(a["schema"], "autotrade.plan7.evidence-class-matrix.v1")
        self.assertEqual(a["scope"], "DIAGNOSTIC_ONLY")
        self.assertTrue(a["plan9_is_final_release_authority"])
        self.assertEqual(set(a["requirements"]), set(REQUIRED_EVIDENCE))
        self.assertIn("sha256:", matrix_fingerprint())
        self.assertEqual(matrix_fingerprint(), matrix_fingerprint())
        json.dumps(a, allow_nan=False)

    def test_exact_class_missing_by_default(self):
        for capability, classes in REQUIRED_EVIDENCE.items():
            with self.subTest(capability=capability):
                result = inspect_capability_evidence(capability, source_sha=SHA, references=())
                self.assertEqual(result.required_classes, classes)
                self.assertEqual(result.missing_classes, classes)
                self.assertEqual(result.unverified_classes, ())
                self.assertEqual(result.status, "INCONCLUSIVE")
                self.assertEqual(result.plan9_owned, capability in PLAN9_OWNED)
                self.assertFalse(result.machine_readable()["final_release_eligible"])
                self.assertFalse(result.machine_readable()["authorizes_trading"])

    def test_wrong_class_cannot_qualify_provider_paper_live_nvda_or_signed_release(self):
        targets = (
            ("provider_account_qualification", "REAL_PROVIDER"),
            ("paper_order_qualification", PAPER),
            ("live_order_qualification", LIVE),
            ("physical_nvda_acceptance", NVDA_PHYSICAL),
            ("delivered_signed_release", SIGNED_RELEASE),
            ("target_host_performance", TARGET_HOST_PHYSICAL),
        )
        for capability, expected in targets:
            with self.subTest(capability=capability):
                wrong = tuple(
                    ref(capability, kind, evidence_ref="artifact:" + kind)
                    for kind in (SOURCE, SIMULATION, REPLAY) if kind != expected
                )
                result = inspect_capability_evidence(capability, source_sha=SHA, references=wrong)
                self.assertEqual(result.missing_classes, (expected,))
                self.assertEqual(result.unverified_classes, ())
                self.assertEqual(result.rejected_wrong_class_count, len(wrong))
                self.assertEqual(result.status, "INCONCLUSIVE")

    def test_distinct_paper_never_counts_as_live_and_live_never_counts_as_paper(self):
        p = inspect_capability_evidence(
            "live_order_qualification", source_sha=SHA,
            references=(ref("live_order_qualification", PAPER),),
        )
        l = inspect_capability_evidence(
            "paper_order_qualification", source_sha=SHA,
            references=(ref("paper_order_qualification", LIVE),),
        )
        self.assertEqual(p.missing_classes, (LIVE,))
        self.assertEqual(l.missing_classes, (PAPER,))

    def test_self_issued_pass_is_explicitly_unverified(self):
        for capability, classes in REQUIRED_EVIDENCE.items():
            references = tuple(
                ref(capability, kind, evidence_ref="artifact:" + kind,
                    issuer="self-proclaimed-independent", claimed_status="PASS")
                for kind in classes
            )
            result = inspect_capability_evidence(capability, source_sha=SHA, references=references)
            self.assertEqual(result.missing_classes, ())
            self.assertEqual(result.unverified_classes, classes)
            self.assertEqual(result.status, "INCONCLUSIVE")
            self.assertFalse(result.machine_readable()["final_release_eligible"])

    def test_stale_git_source_identity_is_not_replayed_as_current(self):
        result = inspect_capability_evidence(
            "scientific_gate_engineering", source_sha=SHA,
            references=(ref("scientific_gate_engineering", SOURCE, source_sha=OTHER_SHA),),
        )
        self.assertEqual(result.missing_classes, (SOURCE, REPLAY))
        self.assertEqual(result.rejected_cross_source_classes, (SOURCE,))

    def test_fail_evidence_remains_fail(self):
        result = inspect_capability_evidence(
            "performance_load_harness", source_sha=SHA,
            references=(ref("performance_load_harness", SOURCE, claimed_status="FAIL"),),
        )
        self.assertEqual(result.status, "FAIL")
        self.assertEqual(result.missing_classes, (SIMULATION, REPLAY))
        self.assertFalse(result.machine_readable()["authorizes_trading"])

    def test_duplicate_reference_and_forged_field_rejected(self):
        r = ref("scientific_gate_engineering", SOURCE)
        with self.assertRaisesRegex(EvidenceMatrixError, "duplicate"):
            inspect_capability_evidence("scientific_gate_engineering", source_sha=SHA, references=(r, r))
        with self.assertRaises(EvidenceMatrixError):
            replace(r, source_sha="INVALID")
        with self.assertRaises(EvidenceMatrixError):
            replace(r, content_digest="sha256:abc")
        with self.assertRaises(EvidenceMatrixError):
            replace(r, evidence_class="PHYSICAL")
        with self.assertRaises(EvidenceMatrixError):
            replace(r, claimed_status="VERIFIED")
        with self.assertRaises(EvidenceMatrixError):
            replace(r, issuer="  issuer ")
        with self.assertRaises(EvidenceMatrixError):
            inspect_capability_evidence("scientific_gate_engineering", source_sha="notsha", references=())
        with self.assertRaises(EvidenceMatrixError):
            inspect_capability_evidence("unknown", source_sha=SHA, references=())

    def test_other_capability_receipts_cannot_be_laundered(self):
        r = ref("scientific_gate_engineering", SOURCE)
        result = inspect_capability_evidence(
            "strategy_economics_harness", source_sha=SHA, references=(r,),
        )
        self.assertEqual(result.missing_classes, (SOURCE, SIMULATION, REPLAY))
        self.assertEqual(result.unverified_classes, ())

    def test_restart_reconstruction_is_deterministic_and_no_implicit_trust_cache(self):
        reference = ref("ablation_component_engineering", SOURCE)
        first = inspect_capability_evidence("ablation_component_engineering", source_sha=SHA, references=(reference,))
        second = inspect_capability_evidence("ablation_component_engineering", source_sha=SHA, references=(reference,))
        self.assertEqual(first.machine_readable(), second.machine_readable())
        self.assertEqual(first.status, "INCONCLUSIVE")
        self.assertEqual(second.unverified_classes, (SOURCE,))

    def test_class_registry_is_closed_and_cannot_be_mutated_through_public_view(self):
        self.assertEqual(set(EVIDENCE_CLASSES), set((
            SOURCE, SIMULATION, REPLAY, TARGET_HOST_PHYSICAL,
            "REAL_PROVIDER", PAPER, LIVE, NVDA_PHYSICAL, SIGNED_RELEASE,
        )))
        with self.assertRaises(TypeError):
            REQUIRED_EVIDENCE["unauthorized"] = (SOURCE,)
