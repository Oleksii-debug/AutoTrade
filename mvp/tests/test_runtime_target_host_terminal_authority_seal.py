from __future__ import annotations

import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

import mvp.autotrade_mvp.runtime_target_host_chronology_bound_qualification as bound
import mvp.autotrade_mvp.runtime_target_host_plan_bound_qualification as plan_bound
from mvp.tests.test_runtime_target_host_chronology_bound_qualification import (
    RuntimeTargetHostChronologyBoundTests,
)


class RuntimeTargetHostTerminalAuthoritySealTests(unittest.TestCase):
    @staticmethod
    def _terminal_call(verifier):
        return verifier(
            object(),
            evidence_store=object(),
            evidence_root="evidence-root",
            journal_store=object(),
            recovery=object(),
            runtime=object(),
            chronology_cut=object(),
            plan_id="plan-1",
            spec=object(),
            expected_release_artifact_id="release-id",
            expected_release_artifact_sha256="sha256:" + "a" * 64,
            campaign_plan=object(),
            campaign_cut=object(),
            measurement=object(),
        )

    def test_production_verifier_ignores_rebound_trust_dependencies(self):
        production = bound.verify_chronology_bound_runtime_target_host_qualification
        forged_receipt_snapshot = Mock(return_value=object())
        forged_current_cut = Mock(return_value=object())
        forged_plan_verifier = Mock(return_value=object())
        forged_pre_binding = Mock()
        forged_cross_binding = Mock()
        forged_terminal_snapshot = Mock(return_value=object())

        with (
            patch.object(bound, "_snapshot_signed_receipt", forged_receipt_snapshot),
            patch.object(bound, "require_current_trusted_chronology_cut", forged_current_cut),
            patch.object(
                bound,
                "verify_declared_plan_runtime_target_host_qualification",
                forged_plan_verifier,
            ),
            patch.object(bound, "_require_pre_binding", forged_pre_binding),
            patch.object(bound, "_require_cross_binding", forged_cross_binding),
            patch.object(
                bound,
                "_snapshot_terminal_qualification",
                forged_terminal_snapshot,
            ),
        ):
            with self.assertRaises(TypeError):
                self._terminal_call(production)

        forged_receipt_snapshot.assert_not_called()
        forged_current_cut.assert_not_called()
        forged_plan_verifier.assert_not_called()
        forged_pre_binding.assert_not_called()
        forged_cross_binding.assert_not_called()
        forged_terminal_snapshot.assert_not_called()

    def test_product_dispatcher_ignores_rebound_terminal_entry(self):
        product = plan_bound.verify_declared_plan_runtime_target_host_qualification
        forged_terminal = Mock(return_value="forged-terminal-pass")

        with patch.object(
            bound,
            "verify_chronology_bound_runtime_target_host_qualification",
            forged_terminal,
        ):
            with self.assertRaises(TypeError):
                self._terminal_call(product)

        forged_terminal.assert_not_called()

    def test_terminal_snapshot_ignores_rebound_scalar_validator(self):
        snapshot = bound._snapshot_terminal_qualification
        qualification = RuntimeTargetHostChronologyBoundTests._qualification()
        forged_exact_text = Mock(return_value="forged")

        with patch.object(bound, "_exact_text", forged_exact_text):
            detached = snapshot(qualification)

        forged_exact_text.assert_not_called()
        self.assertEqual(
            detached.qualification.source_sha,
            qualification.qualification.source_sha,
        )
        self.assertEqual(
            detached.target_host_measurement_digest,
            qualification.target_host_measurement_digest,
        )

    def test_direct_plan_bound_import_installs_sealed_dispatcher_before_return(self):
        script = (
            "import mvp.autotrade_mvp.runtime_target_host_plan_bound_qualification as p;"
            "f=p.verify_declared_plan_runtime_target_host_qualification;"
            "assert f.__module__.endswith('runtime_target_host_chronology_bound_qualification'),"
            " f.__module__"
        )
        completed = subprocess.run(
            [sys.executable, "-c", script],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            completed.returncode,
            0,
            msg=completed.stdout + completed.stderr,
        )


if __name__ == "__main__":
    unittest.main()
