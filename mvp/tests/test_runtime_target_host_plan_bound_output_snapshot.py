from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from types import MappingProxyType
import unittest
from unittest.mock import Mock

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


class RuntimeTargetHostPlanBoundOutputSnapshotTests(unittest.TestCase):
    def test_snapshot_detaches_composed_and_nested_acceptance(self) -> None:
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

    def test_snapshot_rejects_mutable_mapping_substitution(self) -> None:
        retained = RuntimeTargetHostChronologyBoundTests._qualification()
        object.__setattr__(
            retained.qualification,
            "evidence_sha256_by_kind",
            {"RUNTIME_TARGET_HOST_BINDING": "sha256:" + "5" * 64},
        )

        with self.assertRaisesRegex(
            RuntimeTargetHostCompositionError,
            "exact immutable mapping state",
        ):
            plan_bound._PRODUCTION_ACCEPTANCE_SNAPSHOTTER(retained)

    def test_snapshot_rejects_post_pass_noncanonical_authority_text(self) -> None:
        retained = RuntimeTargetHostChronologyBoundTests._qualification()
        object.__setattr__(retained.qualification, "source_sha", "F" * 40)

        with self.assertRaisesRegex(
            RuntimeTargetHostCompositionError,
            "lowercase 40-character Git SHA",
        ):
            plan_bound._PRODUCTION_ACCEPTANCE_SNAPSHOTTER(retained)

        retained = RuntimeTargetHostChronologyBoundTests._qualification()
        object.__setattr__(retained.qualification, "attestation_digest", "forged")
        with self.assertRaisesRegex(
            RuntimeTargetHostCompositionError,
            "canonical sha256 text",
        ):
            plan_bound._PRODUCTION_ACCEPTANCE_SNAPSHOTTER(retained)

    def test_snapshot_rejects_noncanonical_nested_acceptance_type(self) -> None:
        retained = RuntimeTargetHostChronologyBoundTests._qualification()
        object.__setattr__(retained, "qualification", object())

        with self.assertRaisesRegex(
            RuntimeTargetHostCompositionError,
            "non-canonical signed acceptance",
        ):
            plan_bound._PRODUCTION_ACCEPTANCE_SNAPSHOTTER(retained)

    def test_snapshot_detaches_external_mapping_proxy_backing(self) -> None:
        retained = RuntimeTargetHostChronologyBoundTests._qualification()
        backing = {
            "RUNTIME_TARGET_HOST_BINDING": "sha256:" + "5" * 64,
        }
        object.__setattr__(
            retained.qualification,
            "evidence_sha256_by_kind",
            MappingProxyType(backing),
        )

        detached = plan_bound._PRODUCTION_ACCEPTANCE_SNAPSHOTTER(retained)
        backing["RUNTIME_TARGET_HOST_BINDING"] = "sha256:" + "6" * 64

        self.assertEqual(
            detached.qualification.evidence_sha256_by_kind[
                "RUNTIME_TARGET_HOST_BINDING"
            ],
            "sha256:" + "5" * 64,
        )

    def test_verifier_snapshots_before_post_verification_plan_revalidation(self) -> None:
        retained = RuntimeTargetHostChronologyBoundTests._qualification()
        composed = Mock(return_value=retained)
        original_loader = plan_bound.load_declared_runtime_event_plan
        load_count = 0

        def mutating_loader(current_store, *, plan_id, spec):
            nonlocal load_count
            load_count += 1
            if load_count == 2:
                object.__setattr__(retained.qualification, "source_sha", "f" * 40)
            return original_loader(current_store, plan_id=plan_id, spec=spec)

        verifier = plan_bound._build_chronology_free_verifier(
            load_declared_plan=mutating_loader,
            verify_composed=composed,
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

        self.assertEqual(load_count, 2)
        self.assertIsNot(result, retained)
        self.assertIsNot(result.qualification, retained.qualification)
        self.assertEqual(result.qualification.source_sha, "a" * 40)
        self.assertEqual(retained.qualification.source_sha, "f" * 40)
        composed.assert_called_once()

    def test_snapshot_rejects_mutable_projection_map_substitution(self) -> None:
        retained = RuntimeTargetHostChronologyBoundTests._qualification()
        object.__setattr__(retained, "projection_sha256_by_kind", {})

        with self.assertRaisesRegex(
            RuntimeTargetHostCompositionError,
            "exact immutable mapping state",
        ):
            plan_bound._PRODUCTION_ACCEPTANCE_SNAPSHOTTER(retained)


if __name__ == "__main__":
    unittest.main()
