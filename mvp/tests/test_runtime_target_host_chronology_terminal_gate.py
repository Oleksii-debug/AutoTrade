from __future__ import annotations

import unittest
from unittest.mock import Mock, patch
from uuid import NAMESPACE_URL, uuid5

import mvp.autotrade_mvp.runtime_target_host_chronology_bound_qualification as bound
import mvp.autotrade_mvp.runtime_target_host_plan_bound_qualification as plan_bound
from mvp.autotrade_mvp.qualification_attestation import (
    EvidenceArtifactRef,
    QualificationAttestation,
    SignedQualificationAttestation,
)
from mvp.autotrade_mvp.runtime_target_host_composed_qualification import (
    RuntimeTargetHostCompositionError,
)
from mvp.autotrade_mvp.runtime_target_host_plan_bound_qualification import (
    verify_declared_plan_runtime_target_host_qualification,
)


SOURCE_SHA = "a" * 40
RELEASE_ID = str(uuid5(NAMESPACE_URL, "chronology-terminal-release"))
RELEASE_SHA = "sha256:" + "b" * 64
ROOT_ID = "sha256:" + "c" * 64


def _receipt() -> SignedQualificationAttestation:
    ref = EvidenceArtifactRef(
        artifact_id=str(uuid5(NAMESPACE_URL, "chronology-terminal-evidence")),
        sha256="sha256:" + "d" * 64,
        media_type="application/json",
        evidence_kind="RUNTIME_TARGET_HOST_BINDING",
        source_sha=SOURCE_SHA,
    )
    attestation = QualificationAttestation(
        attestation_id=str(uuid5(NAMESPACE_URL, "chronology-terminal-attestation")),
        source_sha=SOURCE_SHA,
        domain="PERFORMANCE",
        gate="RUNTIME_TARGET_HOST",
        package_id="WP-65",
        protocol_id="runtime-target-host-v1",
        protocol_version="1.0.0",
        requirement_ids=("target-host-pressure-budget",),
        evidence_refs=(ref,),
        producer_id="independent.qualifier",
        verifier_id="autotrade.qualifier",
        trust_root_id=ROOT_ID,
        runner_id="runner-1",
        harness_version="1.0.0",
        started_at="2026-10-03T13:59:00Z",
        completed_at="2026-10-03T14:00:01Z",
        signed_at="2026-10-03T14:00:02Z",
        result="PASS",
        release_artifact_id=RELEASE_ID,
        release_artifact_sha256=RELEASE_SHA,
    )
    return SignedQualificationAttestation(attestation=attestation, signature_b64="AA==")


class RuntimeTargetHostChronologyTerminalGateTests(unittest.TestCase):
    @staticmethod
    def _call(verifier, **extra):
        return verifier(
            _receipt(),
            evidence_store=object(),
            evidence_root="evidence-root",
            journal_store=object(),
            plan_id="plan-1",
            spec=object(),
            expected_release_artifact_id=RELEASE_ID,
            expected_release_artifact_sha256=RELEASE_SHA,
            campaign_plan=object(),
            campaign_cut=object(),
            measurement=object(),
            **extra,
        )

    def test_canonical_signed_receipt_cannot_use_legacy_chronology_free_terminal_path(self):
        with self.assertRaisesRegex(
            RuntimeTargetHostCompositionError,
            "accepted RELEASE_RUNTIME chronology authority",
        ):
            self._call(verify_declared_plan_runtime_target_host_qualification)

    def test_partial_chronology_authority_fails_before_any_terminal_dispatch(self):
        forged_terminal = Mock(return_value=object())
        with patch.object(
            bound,
            "verify_chronology_bound_runtime_target_host_qualification",
            forged_terminal,
        ), self.assertRaisesRegex(
            RuntimeTargetHostCompositionError,
            "complete RELEASE_RUNTIME chronology authority",
        ):
            self._call(
                verify_declared_plan_runtime_target_host_qualification,
                recovery=object(),
            )
        forged_terminal.assert_not_called()

    def test_factory_dispatches_complete_chronology_only_to_captured_terminal(self):
        accepted = object()
        captured_terminal = Mock(return_value=accepted)
        lower = Mock(side_effect=AssertionError("chronology-free verifier ran"))
        verifier = bound._build_product_dispatcher(
            terminal_verifier=captured_terminal,
            lower_verifier=lower,
        )

        result = self._call(
            verifier,
            recovery=object(),
            runtime=object(),
            chronology_cut=object(),
        )

        self.assertIs(result, accepted)
        captured_terminal.assert_called_once()
        lower.assert_not_called()
        kwargs = captured_terminal.call_args.kwargs
        self.assertEqual(kwargs["plan_id"], "plan-1")
        self.assertEqual(kwargs["expected_release_artifact_id"], RELEASE_ID)
        self.assertEqual(kwargs["expected_release_artifact_sha256"], RELEASE_SHA)

    def test_factory_captures_terminal_and_type_authority_before_rebinding(self):
        accepted = object()
        captured_terminal = Mock(return_value=accepted)
        lower = Mock(side_effect=AssertionError("chronology-free verifier ran"))
        verifier = bound._build_product_dispatcher(
            terminal_verifier=captured_terminal,
            lower_verifier=lower,
        )

        class ForgedReceipt:
            pass

        class ForgedCompositionError(Exception):
            pass

        forged_public_terminal = Mock(
            side_effect=AssertionError("rebound public terminal verifier ran")
        )
        with (
            patch.object(plan_bound, "SignedQualificationAttestation", ForgedReceipt),
            patch.object(
                plan_bound,
                "RuntimeTargetHostCompositionError",
                ForgedCompositionError,
            ),
            patch.object(
                bound,
                "verify_chronology_bound_runtime_target_host_qualification",
                forged_public_terminal,
            ),
        ):
            result = self._call(
                verifier,
                recovery=object(),
                runtime=object(),
                chronology_cut=object(),
            )

        self.assertIs(result, accepted)
        captured_terminal.assert_called_once()
        lower.assert_not_called()
        forged_public_terminal.assert_not_called()

    def test_production_dispatch_ignores_rebound_public_terminal_symbol(self):
        forged_terminal = Mock(
            side_effect=AssertionError("rebound public terminal verifier ran")
        )
        with patch.object(
            bound,
            "verify_chronology_bound_runtime_target_host_qualification",
            forged_terminal,
        ):
            with self.assertRaises((TypeError, AttributeError, PermissionError, ValueError)):
                self._call(
                    verify_declared_plan_runtime_target_host_qualification,
                    recovery=object(),
                    runtime=object(),
                    chronology_cut=object(),
                )
        forged_terminal.assert_not_called()


if __name__ == "__main__":
    unittest.main()
