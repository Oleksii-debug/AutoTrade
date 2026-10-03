from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, localcontext
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from autotrade_research.artifacts import ArtifactStore
from autotrade_research.data.vintages import (
    HistoricalConflict,
    HistoricalDataError,
    HistoricalVintageRegistry,
    canonical_market_event_population_bytes,
    explicit_missingness,
    market_event_population_digest,
)
from autotrade_research.features.authoritative import (
    AuthoritativeFoldNormalizer,
    HistoricalFeatureInputSpec,
    fit_authoritative_fold_normalizer,
    resolve_authoritative_feature_points,
)
from autotrade_research.features.causal import (
    CausalFold,
    FeaturePoint,
    FoldNormalizer,
    fit_normalizer,
)


BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _digest(text: str) -> str:
    return "sha256:" + sha256(text.encode("utf-8")).hexdigest()


def _uuid(prefix: int, value: int) -> str:
    return f"{prefix:08x}-0000-4000-8000-{value:012x}"


def event(
    day: int,
    value: str,
    *,
    revision: int = 1,
    event_id: str | None = None,
    known_at: datetime | None = None,
) -> dict:
    source_at = BASE + timedelta(days=day)
    available = known_at or (source_at + timedelta(minutes=1))
    ingested = available + timedelta(minutes=1)
    identity = event_id or _uuid(1, day + 1)
    return {
        "event_id": identity,
        "instrument_version": "instrument:AAA:v1",
        "kind": "BAR",
        "source_event_at": _iso(source_at),
        "available_at": _iso(available),
        "ingested_at": _iso(ingested),
        "revision": str(revision),
        "availability_basis": "provider-history",
        "quality_flags": [],
        "payload": {"close": value},
        "raw_evidence_ref": {
            "artifact_id": _uuid(2 + revision, day + 1),
            "sha256": _digest(
                f"{identity}:{revision}:{_iso(ingested)}:{value}"
            ),
            "observed_at": _iso(ingested),
            "rights_id": "research-fixture",
        },
    }


class AuthoritativeFoldPopulationTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.registry = HistoricalVintageRegistry(root / "vintages")
        self.artifacts = ArtifactStore(root / "artifacts")
        self.dataset_id = _uuid(10, 1)
        self.spec = HistoricalFeatureInputSpec(
            spec_id="bar-close-return:v1",
            payload_value_field="close",
            window_count=2,
            event_kinds=("BAR",),
        )
        self.fold = CausalFold.create(
            fold_id="fold-authoritative-1",
            train_start=BASE,
            train_end=BASE + timedelta(days=3, hours=12),
            validation_start=BASE + timedelta(days=4),
            validation_end=BASE + timedelta(days=7),
            purge_seconds=0,
        )

    def _manifest(self, content_hashes, *, content_refs=None, version=1):
        manifest = {
            "dataset_id": self.dataset_id,
            "version": str(version),
            "content_hashes": list(content_hashes),
            "instrument_universe_version": "universe:2026-01",
            "calendar_version": "calendar:2026-a",
            "coverage": {
                "from": _iso(BASE),
                "to": _iso(BASE + timedelta(days=7)),
            },
            "availability_policy": {
                "point_in_time": True,
                "no_future_leakage": True,
                "cutoff": _iso(BASE + timedelta(days=8)),
                "basis": "evidenced-first-availability",
            },
            "revision_policy": {
                "append_only": True,
                "replace_prior_vintages": False,
            },
            "normalization_version": "market-normalization:v1",
            "adjustment_policy": {
                "raw_retained": True,
                "adjusted_available": False,
                "method": "none",
            },
            "rights": {
                "storage": True,
                "research_use": True,
                "redistribution": False,
                "basis": "first-party-test-fixture",
            },
            "missingness_report": explicit_missingness(
                ["expected-series"], ["expected-series"]
            ).to_dict(),
            "source_evidence": [
                {
                    "artifact_id": _uuid(20, version),
                    "sha256": _digest(f"manifest-source:{version}"),
                    "observed_at": _iso(BASE + timedelta(days=8)),
                    "rights_id": "research-fixture",
                }
            ],
            "created_at": _iso(BASE + timedelta(days=8, seconds=1)),
        }
        if content_refs is not None:
            manifest["content_refs"] = list(content_refs)
        return manifest

    @staticmethod
    def _base_events(*, through=6):
        values = ("100", "102", "105", "103", "107", "109", "111")
        return [event(day, values[day]) for day in range(through + 1)]

    def _register(self, rows, *, version=1):
        artifact_id = _uuid(30, version)
        artifact_manifest = self.artifacts.publish_bytes(
            artifact_id=artifact_id,
            data=canonical_market_event_population_bytes(rows),
            media_type="application/vnd.autotrade.market-event-population+json",
            rights={
                "storage": True,
                "export": False,
                "rights_id": "research-fixture",
            },
            source_refs=[f"dataset:{self.dataset_id}:v{version}"],
            metadata={
                "role": "market_event_population",
                "dataset_id": self.dataset_id,
                "dataset_version": version,
            },
        )
        content_ref = {
            "ordinal": 1,
            "role": "market_event_population",
            "artifact_id": artifact_id,
            "sha256": artifact_manifest["sha256"],
            "manifest_hash": artifact_manifest["manifest_hash"],
            "rights_id": "research-fixture",
        }
        return self.registry.commit(
            self._manifest(
                [artifact_manifest["sha256"]],
                content_refs=[content_ref],
                version=version,
            )
        )

    def _validation_point(self, rows, manifest_digest, *, version=1):
        points = resolve_authoritative_feature_points(
            registry=self.registry,
            dataset_id=self.dataset_id,
            dataset_version=version,
            manifest_digest=manifest_digest,
            artifact_store=self.artifacts,
            events=rows,
            cutoff=BASE + timedelta(days=4, minutes=2),
            spec=self.spec,
        )
        candidates = [
            point
            for point in points
            if self.fold.validation_start
            <= point.decision_time
            <= self.fold.validation_end
        ]
        self.assertEqual(len(candidates), 1)
        return candidates[0]

    def test_exact_registered_population_fits_and_validates(self):
        rows = self._base_events()
        manifest_digest = self._register(rows)
        fitted = fit_authoritative_fold_normalizer(
            registry=self.registry,
            dataset_id=self.dataset_id,
            dataset_version=1,
            manifest_digest=manifest_digest,
            artifact_store=self.artifacts,
            events=rows,
            fold=self.fold,
            spec=self.spec,
            replay_common_cut_fingerprint="a" * 64,
        )
        point = self._validation_point(rows, manifest_digest)
        value = fitted.transform_validation(
            point,
            fold=self.fold,
            registry=self.registry,
            artifact_store=self.artifacts,
            events=rows,
            spec=self.spec,
            replay_common_cut_fingerprint="a" * 64,
        )
        self.assertIsInstance(value, Decimal)
        self.assertEqual(fitted.fold_normalizer.training_point_count, 3)
        self.assertTrue(fitted.fingerprint.startswith("sha256:"))

    def test_artifact_store_is_sufficient_without_caller_population(self):
        rows = self._base_events()
        manifest_digest = self._register(rows)
        fitted = fit_authoritative_fold_normalizer(
            registry=self.registry,
            dataset_id=self.dataset_id,
            dataset_version=1,
            manifest_digest=manifest_digest,
            artifact_store=self.artifacts,
            fold=self.fold,
            spec=self.spec,
        )
        points = resolve_authoritative_feature_points(
            registry=self.registry,
            dataset_id=self.dataset_id,
            dataset_version=1,
            manifest_digest=manifest_digest,
            artifact_store=self.artifacts,
            cutoff=BASE + timedelta(days=4, minutes=2),
            spec=self.spec,
        )
        point = next(
            candidate
            for candidate in points
            if self.fold.validation_start
            <= candidate.decision_time
            <= self.fold.validation_end
        )
        transformed = fitted.transform_validation(
            point,
            fold=self.fold,
            registry=self.registry,
            artifact_store=self.artifacts,
            spec=self.spec,
        )
        self.assertIsInstance(transformed, Decimal)

    def test_same_ids_and_revisions_with_altered_value_are_not_authority(self):
        rows = self._base_events()
        manifest_digest = self._register(rows)
        forged = [dict(row) for row in rows]
        forged[2] = {
            **forged[2],
            "payload": {"close": "999999"},
        }
        with self.assertRaisesRegex(
            HistoricalConflict,
            "caller market event cache differs",
        ):
            fit_authoritative_fold_normalizer(
                registry=self.registry,
                dataset_id=self.dataset_id,
                dataset_version=1,
                manifest_digest=manifest_digest,
                artifact_store=self.artifacts,
                events=forged,
                fold=self.fold,
                spec=self.spec,
            )

    def test_manifest_digest_mismatch_fails_before_fit(self):
        rows = self._base_events()
        self._register(rows)
        with self.assertRaisesRegex(
            HistoricalConflict,
            "manifest digest differs",
        ):
            fit_authoritative_fold_normalizer(
                registry=self.registry,
                dataset_id=self.dataset_id,
                dataset_version=1,
                manifest_digest="sha256:" + "0" * 64,
                artifact_store=self.artifacts,
                events=rows,
                fold=self.fold,
                spec=self.spec,
            )

    def test_authoritative_resolver_rejects_legacy_unbound_content_hash(self):
        rows = self._base_events()
        dangling_digest = market_event_population_digest(rows)
        manifest_digest = self.registry.commit(self._manifest([dangling_digest]))
        with self.assertRaisesRegex(
            HistoricalDataError,
            "lacks authoritative content references",
        ):
            fit_authoritative_fold_normalizer(
                registry=self.registry,
                dataset_id=self.dataset_id,
                dataset_version=1,
                manifest_digest=manifest_digest,
                artifact_store=self.artifacts,
                events=rows,
                fold=self.fold,
                spec=self.spec,
            )

    def test_manifest_rejects_content_ref_digest_different_from_hash_list(self):
        rows = self._base_events()
        digest = market_event_population_digest(rows)
        content_ref = {
            "ordinal": 1,
            "role": "market_event_population",
            "artifact_id": _uuid(30, 1),
            "sha256": "sha256:" + "0" * 64,
            "manifest_hash": "sha256:" + "1" * 64,
            "rights_id": "research-fixture",
        }
        with self.assertRaisesRegex(
            HistoricalDataError,
            "exactly match ordered content_refs",
        ):
            self.registry.commit(
                self._manifest([digest], content_refs=[content_ref])
            )

    def test_artifact_manifest_hash_is_bound_by_dataset_vintage(self):
        rows = self._base_events()
        artifact_id = _uuid(31, 1)
        artifact_manifest = self.artifacts.publish_bytes(
            artifact_id=artifact_id,
            data=canonical_market_event_population_bytes(rows),
            media_type="application/vnd.autotrade.market-event-population+json",
            rights={
                "storage": True,
                "export": False,
                "rights_id": "research-fixture",
            },
        )
        content_ref = {
            "ordinal": 1,
            "role": "market_event_population",
            "artifact_id": artifact_id,
            "sha256": artifact_manifest["sha256"],
            "manifest_hash": "sha256:" + "0" * 64,
            "rights_id": "research-fixture",
        }
        manifest_digest = self.registry.commit(
            self._manifest(
                [artifact_manifest["sha256"]],
                content_refs=[content_ref],
            )
        )
        with self.assertRaisesRegex(
            HistoricalConflict,
            "artifact manifest identity differs",
        ):
            fit_authoritative_fold_normalizer(
                registry=self.registry,
                dataset_id=self.dataset_id,
                dataset_version=1,
                manifest_digest=manifest_digest,
                artifact_store=self.artifacts,
                events=rows,
                fold=self.fold,
                spec=self.spec,
            )

    def test_artifact_rights_identity_is_bound_by_dataset_vintage(self):
        rows = self._base_events()
        artifact_id = _uuid(32, 1)
        artifact_manifest = self.artifacts.publish_bytes(
            artifact_id=artifact_id,
            data=canonical_market_event_population_bytes(rows),
            media_type="application/vnd.autotrade.market-event-population+json",
            rights={
                "storage": True,
                "export": False,
                "rights_id": "research-fixture",
            },
        )
        content_ref = {
            "ordinal": 1,
            "role": "market_event_population",
            "artifact_id": artifact_id,
            "sha256": artifact_manifest["sha256"],
            "manifest_hash": artifact_manifest["manifest_hash"],
            "rights_id": "different-rights",
        }
        manifest_digest = self.registry.commit(
            self._manifest(
                [artifact_manifest["sha256"]],
                content_refs=[content_ref],
            )
        )
        with self.assertRaisesRegex(
            HistoricalConflict,
            "rights identity differs",
        ):
            fit_authoritative_fold_normalizer(
                registry=self.registry,
                dataset_id=self.dataset_id,
                dataset_version=1,
                manifest_digest=manifest_digest,
                artifact_store=self.artifacts,
                events=rows,
                fold=self.fold,
                spec=self.spec,
            )

    def test_caller_forged_validation_feature_is_rejected(self):
        rows = self._base_events()
        manifest_digest = self._register(rows)
        fitted = fit_authoritative_fold_normalizer(
            registry=self.registry,
            dataset_id=self.dataset_id,
            dataset_version=1,
            manifest_digest=manifest_digest,
            artifact_store=self.artifacts,
            events=rows,
            fold=self.fold,
            spec=self.spec,
        )
        point = self._validation_point(rows, manifest_digest)
        forged = FeaturePoint(
            symbol=point.symbol,
            decision_time=point.decision_time,
            value=point.value + Decimal("1"),
            input_ids=point.input_ids,
            source_revisions=point.source_revisions,
            feature_name=point.feature_name,
        )
        with self.assertRaisesRegex(
            ValueError,
            "caller validation point differs",
        ):
            fitted.transform_validation(
                forged,
                fold=self.fold,
                registry=self.registry,
                artifact_store=self.artifacts,
                events=rows,
                spec=self.spec,
            )

    def test_future_dataset_values_cannot_change_training_fit(self):
        training_only = self._base_events(through=3)
        full = self._base_events(through=6)
        full[4] = event(4, "999999999")
        full[5] = event(5, "-999999999")
        full[6] = event(6, "777777777")
        first_manifest = self._register(training_only, version=1)
        second_manifest = self._register(full, version=2)

        first = fit_authoritative_fold_normalizer(
            registry=self.registry,
            dataset_id=self.dataset_id,
            dataset_version=1,
            manifest_digest=first_manifest,
            artifact_store=self.artifacts,
            events=training_only,
            fold=self.fold,
            spec=self.spec,
        )
        second = fit_authoritative_fold_normalizer(
            registry=self.registry,
            dataset_id=self.dataset_id,
            dataset_version=2,
            manifest_digest=second_manifest,
            artifact_store=self.artifacts,
            events=full,
            fold=self.fold,
            spec=self.spec,
        )
        self.assertEqual(first.fold_normalizer, second.fold_normalizer)
        self.assertNotEqual(
            first.training_population_fingerprint,
            second.training_population_fingerprint,
        )

    def test_late_correction_is_invisible_to_training_fit(self):
        baseline = self._base_events(through=3)
        original = baseline[2]
        late = event(
            2,
            "500",
            revision=2,
            event_id=original["event_id"],
            known_at=BASE + timedelta(days=5),
        )
        with_late = baseline + [late]
        first_manifest = self._register(baseline, version=1)
        second_manifest = self._register(with_late, version=2)

        first = fit_authoritative_fold_normalizer(
            registry=self.registry,
            dataset_id=self.dataset_id,
            dataset_version=1,
            manifest_digest=first_manifest,
            artifact_store=self.artifacts,
            events=baseline,
            fold=self.fold,
            spec=self.spec,
        )
        second = fit_authoritative_fold_normalizer(
            registry=self.registry,
            dataset_id=self.dataset_id,
            dataset_version=2,
            manifest_digest=second_manifest,
            artifact_store=self.artifacts,
            events=with_late,
            fold=self.fold,
            spec=self.spec,
        )
        self.assertEqual(first.fold_normalizer, second.fold_normalizer)
        self.assertNotEqual(
            first.training_population_fingerprint,
            second.training_population_fingerprint,
        )

    def test_correction_known_before_cutoff_changes_training_fit(self):
        baseline = self._base_events(through=3)
        original = baseline[2]
        early = event(
            2,
            "500",
            revision=2,
            event_id=original["event_id"],
            known_at=BASE + timedelta(days=3, hours=1),
        )
        with_early = baseline + [early]
        first_manifest = self._register(baseline, version=1)
        second_manifest = self._register(with_early, version=2)

        first = fit_authoritative_fold_normalizer(
            registry=self.registry,
            dataset_id=self.dataset_id,
            dataset_version=1,
            manifest_digest=first_manifest,
            artifact_store=self.artifacts,
            events=baseline,
            fold=self.fold,
            spec=self.spec,
        )
        second = fit_authoritative_fold_normalizer(
            registry=self.registry,
            dataset_id=self.dataset_id,
            dataset_version=2,
            manifest_digest=second_manifest,
            artifact_store=self.artifacts,
            events=with_early,
            fold=self.fold,
            spec=self.spec,
        )
        self.assertNotEqual(first.fold_normalizer, second.fold_normalizer)
        self.assertNotEqual(
            first.training_population_fingerprint,
            second.training_population_fingerprint,
        )

    def test_pre_cut_correction_preserves_what_was_known_before_correction(self):
        baseline = self._base_events(through=3)
        original = baseline[2]
        correction_known_at = BASE + timedelta(days=3, hours=1)
        correction = event(
            2,
            "500",
            revision=2,
            event_id=original["event_id"],
            known_at=correction_known_at,
        )
        rows = baseline + [correction]
        manifest_digest = self._register(rows)
        points = resolve_authoritative_feature_points(
            registry=self.registry,
            dataset_id=self.dataset_id,
            dataset_version=1,
            manifest_digest=manifest_digest,
            artifact_store=self.artifacts,
            events=rows,
            cutoff=self.fold.training_information_cutoff,
            spec=self.spec,
        )

        before_correction = next(
            point
            for point in points
            if point.decision_time == BASE + timedelta(days=3, minutes=2)
        )
        after_correction = next(
            point
            for point in points
            if point.decision_time == correction_known_at + timedelta(minutes=1)
        )
        original_id = f"{original['event_id']}@r1"
        correction_id = f"{original['event_id']}@r2"
        self.assertIn(original_id, before_correction.input_ids)
        self.assertNotIn(correction_id, before_correction.input_ids)
        self.assertIn(correction_id, after_correction.input_ids)

    def test_full_dataset_fitted_normalizer_cannot_masquerade_as_fold_fit(self):
        rows = self._base_events()
        manifest_digest = self._register(rows)
        legitimate = fit_authoritative_fold_normalizer(
            registry=self.registry,
            dataset_id=self.dataset_id,
            dataset_version=1,
            manifest_digest=manifest_digest,
            artifact_store=self.artifacts,
            events=rows,
            fold=self.fold,
            spec=self.spec,
        )
        all_points = resolve_authoritative_feature_points(
            registry=self.registry,
            dataset_id=self.dataset_id,
            dataset_version=1,
            manifest_digest=manifest_digest,
            artifact_store=self.artifacts,
            events=rows,
            cutoff=BASE + timedelta(days=6, minutes=2),
            spec=self.spec,
        )
        leaked = fit_normalizer(
            all_points,
            fit_cutoff=BASE + timedelta(days=6, minutes=2),
        )
        forged_fold_fit = FoldNormalizer(
            fold_id=legitimate.fold_normalizer.fold_id,
            fold_fingerprint=legitimate.fold_normalizer.fold_fingerprint,
            feature_name=legitimate.fold_normalizer.feature_name,
            normalizer=leaked,
            training_point_count=len(all_points),
        )
        forged = AuthoritativeFoldNormalizer(
            dataset_id=legitimate.dataset_id,
            dataset_version=legitimate.dataset_version,
            manifest_digest=legitimate.manifest_digest,
            training_population_fingerprint=legitimate.training_population_fingerprint,
            feature_spec_fingerprint=legitimate.feature_spec_fingerprint,
            replay_common_cut_fingerprint=None,
            fold_normalizer=forged_fold_fit,
        )
        point = self._validation_point(rows, manifest_digest)
        with self.assertRaisesRegex(
            ValueError,
            "stored fold normalizer differs",
        ):
            forged.transform_validation(
                point,
                fold=self.fold,
                registry=self.registry,
                artifact_store=self.artifacts,
                events=rows,
                spec=self.spec,
            )

    def test_replay_common_cut_must_match_at_validation(self):
        rows = self._base_events()
        manifest_digest = self._register(rows)
        fitted = fit_authoritative_fold_normalizer(
            registry=self.registry,
            dataset_id=self.dataset_id,
            dataset_version=1,
            manifest_digest=manifest_digest,
            artifact_store=self.artifacts,
            events=rows,
            fold=self.fold,
            spec=self.spec,
            replay_common_cut_fingerprint="a" * 64,
        )
        point = self._validation_point(rows, manifest_digest)
        with self.assertRaisesRegex(ValueError, "replay common-cut"):
            fitted.transform_validation(
                point,
                fold=self.fold,
                registry=self.registry,
                artifact_store=self.artifacts,
                events=rows,
                spec=self.spec,
                replay_common_cut_fingerprint="b" * 64,
            )

    def test_authoritative_fit_and_transform_ignore_ambient_decimal_context(self):
        rows = self._base_events()
        manifest_digest = self._register(rows)
        with localcontext() as context:
            context.prec = 6
            context.rounding = ROUND_FLOOR
            first = fit_authoritative_fold_normalizer(
                registry=self.registry,
                dataset_id=self.dataset_id,
                dataset_version=1,
                manifest_digest=manifest_digest,
                artifact_store=self.artifacts,
                events=rows,
                fold=self.fold,
                spec=self.spec,
            )
            point = self._validation_point(rows, manifest_digest)
            transformed_first = first.transform_validation(
                point,
                fold=self.fold,
                registry=self.registry,
                artifact_store=self.artifacts,
                events=rows,
                spec=self.spec,
            )

        with localcontext() as context:
            context.prec = 37
            context.rounding = ROUND_CEILING
            second = fit_authoritative_fold_normalizer(
                registry=self.registry,
                dataset_id=self.dataset_id,
                dataset_version=1,
                manifest_digest=manifest_digest,
                artifact_store=self.artifacts,
                events=rows,
                fold=self.fold,
                spec=self.spec,
            )
            transformed_second = second.transform_validation(
                point,
                fold=self.fold,
                registry=self.registry,
                artifact_store=self.artifacts,
                events=rows,
                spec=self.spec,
            )

        self.assertEqual(first, second)
        self.assertEqual(first.fingerprint, second.fingerprint)
        self.assertEqual(transformed_first, transformed_second)

    def test_simultaneous_distinct_events_use_canonical_provider_order(self):
        first = event(2, "105", event_id=_uuid(1, 200))
        second = event(2, "106", event_id=_uuid(1, 201))
        first["source_sequence"] = "40"
        first["stream_generation"] = "2"
        second["source_sequence"] = "41"
        second["stream_generation"] = "2"
        second["raw_evidence_ref"]["artifact_id"] = _uuid(3, 201)
        rows = [event(0, "100"), event(1, "102"), first, second]
        manifest_digest = self._register(rows)

        points = resolve_authoritative_feature_points(
            registry=self.registry,
            dataset_id=self.dataset_id,
            dataset_version=1,
            manifest_digest=manifest_digest,
            artifact_store=self.artifacts,
            events=rows,
            cutoff=BASE + timedelta(days=2, minutes=2),
            spec=self.spec,
        )
        simultaneous = [
            point
            for point in points
            if point.decision_time == BASE + timedelta(days=2, minutes=2)
        ]
        self.assertEqual(len(simultaneous), 1)
        point = simultaneous[0]
        self.assertEqual(
            point.input_ids,
            (f"{first['event_id']}@r1", f"{second['event_id']}@r1"),
        )
        with localcontext() as context:
            context.prec = 50
            expected = (Decimal("106") / Decimal("105")) - Decimal("1")
        self.assertEqual(point.value, expected)

    def test_simultaneous_distinct_events_without_sequence_fail_closed(self):
        first = event(2, "105", event_id=_uuid(1, 210))
        second = event(2, "106", event_id=_uuid(1, 211))
        second["raw_evidence_ref"]["artifact_id"] = _uuid(3, 211)
        rows = [event(0, "100"), event(1, "102"), first, second]
        manifest_digest = self._register(rows)

        with self.assertRaisesRegex(
            ValueError,
            "simultaneous distinct market events require source_sequence",
        ):
            resolve_authoritative_feature_points(
                registry=self.registry,
                dataset_id=self.dataset_id,
                dataset_version=1,
                manifest_digest=manifest_digest,
                artifact_store=self.artifacts,
                events=rows,
                cutoff=BASE + timedelta(days=2, minutes=2),
                spec=self.spec,
            )

    def test_identical_authority_reproduces_exact_fit_identity(self):
        rows = self._base_events()
        manifest_digest = self._register(rows)
        first = fit_authoritative_fold_normalizer(
            registry=self.registry,
            dataset_id=self.dataset_id,
            dataset_version=1,
            manifest_digest=manifest_digest,
            artifact_store=self.artifacts,
            events=rows,
            fold=self.fold,
            spec=self.spec,
            replay_common_cut_fingerprint="c" * 64,
        )
        second = fit_authoritative_fold_normalizer(
            registry=self.registry,
            dataset_id=self.dataset_id,
            dataset_version=1,
            manifest_digest=manifest_digest,
            artifact_store=self.artifacts,
            events=list(reversed(rows)),
            fold=self.fold,
            spec=self.spec,
            replay_common_cut_fingerprint="c" * 64,
        )
        self.assertEqual(first, second)
        self.assertEqual(first.fingerprint, second.fingerprint)


if __name__ == "__main__":
    unittest.main()
