from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from mvp.autotrade_mvp import runtime_target_host_plan_bound_qualification as plan_bound
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.runtime_load_plan import declare_runtime_event_plan
from mvp.autotrade_mvp.runtime_target_host_composed_qualification import (
    RuntimeTargetHostCompositionError,
)
from mvp.tests.test_runtime_target_host_chronology_bound_qualification import (
    RuntimeTargetHostChronologyBoundTests,
)
from mvp.tests.test_runtime_target_host_plan_bound_qualification import (
    RELEASE_ID,
    RELEASE_SHA,
    _expected_event,
    _spec,
)


class RuntimeTargetHostPlanBoundAuthoritySealTests(unittest.TestCase):
    def test_production_path_ignores_rebound_composed_verifier(self) -> None:
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            spec = _spec()
            plan = declare_runtime_event_plan(
                store,
                plan_id="sealed-plan",
                spec=spec,
                expected_events=(_expected_event(),),
            )
            forged = Mock(
                return_value=RuntimeTargetHostChronologyBoundTests._qualification()
            )

            with patch.object(
                plan_bound,
                "verify_composed_runtime_target_host_qualification",
                forged,
            ), self.assertRaises((TypeError, RuntimeTargetHostCompositionError)):
                plan_bound.verify_declared_plan_runtime_target_host_qualification(
                    object(),
                    evidence_store=object(),
                    evidence_root=directory,
                    journal_store=store,
                    plan_id=plan.plan_id,
                    spec=spec,
                    expected_release_artifact_id=RELEASE_ID,
                    expected_release_artifact_sha256=RELEASE_SHA,
                    campaign_plan=object(),
                    campaign_cut=object(),
                    measurement=object(),
                )

            forged.assert_not_called()

    def test_detached_acceptance_survives_verifier_owned_post_pass_mutation(self) -> None:
        retained = RuntimeTargetHostChronologyBoundTests._qualification()
        detached = plan_bound._PRODUCTION_ACCEPTANCE_SNAPSHOTTER(retained)

        self.assertIsNot(detached, retained)
        self.assertIsNot(detached.qualification, retained.qualification)
        self.assertEqual(detached.qualification.source_sha, retained.qualification.source_sha)
        self.assertEqual(
            detached.target_host_measurement_digest,
            retained.target_host_measurement_digest,
        )

        object.__setattr__(retained.qualification, "source_sha", "f" * 40)
        object.__setattr__(
            retained,
            "target_host_measurement_digest",
            "sha256:" + "0" * 64,
        )

        self.assertEqual(detached.qualification.source_sha, "a" * 40)
        self.assertEqual(
            detached.target_host_measurement_digest,
            "sha256:" + "3" * 64,
        )

    def test_snapshot_rejects_post_pass_mutable_map_substitution(self) -> None:
        retained = RuntimeTargetHostChronologyBoundTests._qualification()
        object.__setattr__(
            retained.qualification,
            "evidence_sha256_by_kind",
            {"RUNTIME_TARGET_HOST_BINDING": "sha256:" + "5" * 64},
        )

        with self.assertRaisesRegex(
            RuntimeTargetHostCompositionError,
            "constructor-owned immutable mapping state",
        ):
            plan_bound._PRODUCTION_ACCEPTANCE_SNAPSHOTTER(retained)

    def test_chronology_free_verifier_returns_detached_composed_acceptance(self) -> None:
        retained = RuntimeTargetHostChronologyBoundTests._qualification()
        composed = Mock(return_value=retained)
        verifier = plan_bound._build_chronology_free_verifier(
            journal_store_type=plan_bound.JournalStore,
            budget_spec_type=plan_bound.RuntimeBudgetSpec,
            campaign_plan_type=plan_bound.RuntimeCampaignPlan,
            campaign_cut_type=plan_bound.RuntimeCampaignCut,
            measurement_type=plan_bound.TargetHostMeasurementArtifact,
            composition_error_type=plan_bound.RuntimeTargetHostCompositionError,
            copy_value=plan_bound.copy,
            require_store_authority=plan_bound.require_exact_journal_store_authority,
            store_authority_scope=plan_bound.journal_store_authority_scope,
            plan_loader=plan_bound.load_declared_runtime_event_plan,
            composed_verifier=composed,
            acceptance_snapshotter=plan_bound._PRODUCTION_ACCEPTANCE_SNAPSHOTTER,
        )

        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            spec = _spec()
            plan = declare_runtime_event_plan(
                store,
                plan_id="snapshot-plan",
                spec=spec,
                expected_events=(_expected_event(),),
            )
            result = verifier(
                object(),
                evidence_store=object(),
                evidence_root=directory,
                journal_store=store,
                plan_id=plan.plan_id,
                spec=spec,
                expected_release_artifact_id=RELEASE_ID,
                expected_release_artifact_sha256=RELEASE_SHA,
                campaign_plan=object(),
                campaign_cut=object(),
                measurement=object(),
            )

        self.assertIsNot(result, retained)
        self.assertIsNot(result.qualification, retained.qualification)
        object.__setattr__(retained.qualification, "source_sha", "f" * 40)
        self.assertEqual(result.qualification.source_sha, "a" * 40)
        composed.assert_called_once()


if __name__ == "__main__":
    unittest.main()
