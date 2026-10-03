from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

import test_runtime_target_host_chronology_bound_qualification as _cases
import mvp.autotrade_mvp.runtime_target_host_chronology_bound_qualification as bound


class RuntimeTargetHostComposedHorizonTests(unittest.TestCase):
    def test_each_currentness_check_carries_signed_terminal_horizon(self):
        cases = _cases.RuntimeTargetHostChronologyBoundTests
        chronology = cases._chronology()
        measurement = cases._measurement()
        qualification = cases._qualification()
        current = Mock(side_effect=[chronology, chronology])
        observer = Mock()

        with (
            patch.object(bound, "snapshot_target_host_measurement", return_value=measurement),
            patch.object(bound, "require_current_trusted_chronology_cut", current),
            patch.object(bound, "require_chronology_horizon", observer),
            patch.object(
                bound,
                "verify_declared_plan_runtime_target_host_qualification",
                return_value=qualification,
            ),
        ):
            result = bound.verify_chronology_bound_runtime_target_host_qualification(
                cases._receipt(),
                evidence_store=object(),
                evidence_root="evidence-root",
                journal_store=object(),
                recovery=object(),
                runtime=object(),
                chronology_cut=chronology,
                plan_id="plan-1",
                spec=object(),
                expected_release_artifact_id=_cases.RELEASE_ID,
                expected_release_artifact_sha256=_cases.RELEASE_SHA,
                campaign_plan=object(),
                campaign_cut=object(),
                measurement=object(),
            )

        self.assertEqual(result.chronology_cut, chronology)
        self.assertEqual(current.call_count, 2)
        expected_horizon = (
            "2026-10-03T14:00:01Z",
            "2026-10-03T14:00:02Z",
        )
        for current_call in current.call_args_list:
            self.assertEqual(current_call.kwargs["claimed_instants"], expected_horizon)
        self.assertEqual(observer.call_count, 2)


if __name__ == "__main__":
    unittest.main()
