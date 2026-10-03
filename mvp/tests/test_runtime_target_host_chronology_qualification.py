from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from mvp.autotrade_mvp.qualification_attestation import AcceptedQualificationAttestation
from mvp.autotrade_mvp.trusted_chronology import ChronologyScope
import mvp.autotrade_mvp.runtime_target_host_chronology_qualification as terminal


SOURCE_SHA = "a" * 40
RELEASE_ID = "5c9e8f48-4ce4-4d58-97e6-af20aac7ffec"
RELEASE_SHA = "sha256:" + "b" * 64
ATTESTATION_ID = "97743286-2c7a-49a4-b0fb-eceff6967cc5"
ATTESTATION_DIGEST = "sha256:" + "c" * 64


def _accepted():
    return SimpleNamespace(
        source_sha=SOURCE_SHA,
        release_artifact_id=RELEASE_ID,
        release_artifact_sha256=RELEASE_SHA,
        attestation_id=ATTESTATION_ID,
        attestation_digest=ATTESTATION_DIGEST,
    )


def _canonical(*, digest=ATTESTATION_DIGEST):
    value = object.__new__(AcceptedQualificationAttestation)
    for name, field_value in (
        ("attestation_id", ATTESTATION_ID),
        ("attestation_digest", digest),
        ("started_at", "2026-10-03T14:00:00Z"),
        ("completed_at", "2026-10-03T14:01:00Z"),
        ("signed_at", "2026-10-03T14:01:01Z"),
    ):
        object.__setattr__(value, name, field_value)
    return value


class RuntimeTargetHostChronologyQualificationTests(unittest.TestCase):
    def _call(self):
        return terminal.verify_declared_plan_runtime_target_host_qualification_with_release_chronology(
            self.receipt,
            evidence_store=self.evidence_store,
            evidence_root="/evidence",
            journal_store=self.journal_store,
            plan_id="plan-1",
            spec=self.spec,
            expected_release_artifact_id=RELEASE_ID,
            expected_release_artifact_sha256=RELEASE_SHA,
            chronology_cut=self.chronology_cut,
            recovery=self.recovery,
            runtime=self.runtime,
        )

    def setUp(self):
        self.receipt = object()
        self.evidence_store = object()
        self.journal_store = object()
        self.spec = object()
        self.chronology_cut = object()
        self.recovery = object()
        self.runtime = object()
        self.accepted = _accepted()
        self.canonical = _canonical()
        self.durable_cut = object()

    def test_terminal_acceptance_binds_same_receipt_release_runtime_and_horizon(self):
        with (
            patch.object(
                terminal,
                "verify_declared_plan_runtime_target_host_qualification",
                return_value=self.accepted,
            ) as plan_verifier,
            patch.object(
                terminal,
                "verify_canonical_qualification_attestation",
                return_value=self.canonical,
            ) as canonical_verifier,
            patch.object(
                terminal,
                "require_current_trusted_chronology_cut",
                return_value=self.durable_cut,
            ) as chronology_verifier,
            patch.object(terminal, "require_chronology_horizon") as horizon,
        ):
            observed = self._call()

        self.assertIs(observed, self.accepted)
        plan_verifier.assert_called_once_with(
            self.receipt,
            evidence_store=self.evidence_store,
            evidence_root="/evidence",
            journal_store=self.journal_store,
            plan_id="plan-1",
            spec=self.spec,
            expected_release_artifact_id=RELEASE_ID,
            expected_release_artifact_sha256=RELEASE_SHA,
        )
        canonical_verifier.assert_called_once_with(
            self.receipt,
            evidence_store=self.evidence_store,
            evidence_root="/evidence",
            expected_source_sha=SOURCE_SHA,
            expected_domain=terminal.DOMAIN,
            expected_gate=terminal.GATE,
            expected_package_id=terminal.PACKAGE_ID,
            expected_protocol_id=terminal.PROTOCOL_ID,
            expected_protocol_version=terminal.PROTOCOL_VERSION,
            expected_requirement_id=terminal.REQUIREMENT_ID,
            expected_release_artifact_id=RELEASE_ID,
            expected_release_artifact_sha256=RELEASE_SHA,
        )
        chronology_verifier.assert_called_once_with(
            store=self.journal_store,
            recovery=self.recovery,
            cut=self.chronology_cut,
            evidence_store=self.evidence_store,
            evidence_root="/evidence",
            expected_source_sha=SOURCE_SHA,
            expected_scope=ChronologyScope.RELEASE_RUNTIME,
            expected_release_artifact_id=RELEASE_ID,
            expected_release_artifact_sha256=RELEASE_SHA,
            runtime=self.runtime,
        )
        horizon.assert_called_once_with(
            self.durable_cut,
            "2026-10-03T14:00:00Z",
            "2026-10-03T14:01:00Z",
            "2026-10-03T14:01:01Z",
        )

    def test_different_canonical_receipt_cannot_borrow_wp65_acceptance(self):
        different = _canonical(digest="sha256:" + "d" * 64)
        with (
            patch.object(
                terminal,
                "verify_declared_plan_runtime_target_host_qualification",
                return_value=self.accepted,
            ),
            patch.object(
                terminal,
                "verify_canonical_qualification_attestation",
                return_value=different,
            ),
            patch.object(
                terminal,
                "require_current_trusted_chronology_cut",
            ) as chronology_verifier,
        ):
            with self.assertRaisesRegex(
                terminal.RuntimeTargetHostChronologyError,
                "differs from accepted WP-65",
            ):
                self._call()
        chronology_verifier.assert_not_called()

    def test_noncanonical_second_verifier_result_fails_before_chronology(self):
        with (
            patch.object(
                terminal,
                "verify_declared_plan_runtime_target_host_qualification",
                return_value=self.accepted,
            ),
            patch.object(
                terminal,
                "verify_canonical_qualification_attestation",
                return_value=SimpleNamespace(
                    attestation_id=ATTESTATION_ID,
                    attestation_digest=ATTESTATION_DIGEST,
                ),
            ),
            patch.object(
                terminal,
                "require_current_trusted_chronology_cut",
            ) as chronology_verifier,
        ):
            with self.assertRaisesRegex(
                terminal.RuntimeTargetHostChronologyError,
                "non-canonical WP-65 acceptance",
            ):
                self._call()
        chronology_verifier.assert_not_called()

    def test_stale_or_wrong_runtime_chronology_fails_closed(self):
        with (
            patch.object(
                terminal,
                "verify_declared_plan_runtime_target_host_qualification",
                return_value=self.accepted,
            ),
            patch.object(
                terminal,
                "verify_canonical_qualification_attestation",
                return_value=self.canonical,
            ),
            patch.object(
                terminal,
                "require_current_trusted_chronology_cut",
                side_effect=PermissionError("runtime occurrence is no longer current"),
            ),
            patch.object(terminal, "require_chronology_horizon") as horizon,
        ):
            with self.assertRaisesRegex(PermissionError, "no longer current"):
                self._call()
        horizon.assert_not_called()

    def test_signed_wp65_times_outside_cut_horizon_fail_closed(self):
        with (
            patch.object(
                terminal,
                "verify_declared_plan_runtime_target_host_qualification",
                return_value=self.accepted,
            ),
            patch.object(
                terminal,
                "verify_canonical_qualification_attestation",
                return_value=self.canonical,
            ),
            patch.object(
                terminal,
                "require_current_trusted_chronology_cut",
                return_value=self.durable_cut,
            ),
            patch.object(
                terminal,
                "require_chronology_horizon",
                side_effect=PermissionError("claimed terminal evidence horizon is not covered"),
            ),
        ):
            with self.assertRaisesRegex(PermissionError, "not covered"):
                self._call()


if __name__ == "__main__":
    unittest.main()
