from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, localcontext
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from autotrade_research.data.vintages import (
    HistoricalConflict,
    HistoricalVintageRegistry,
    explicit_missingness,
    market_event_population_digest,
)
from autotrade_research.features.authoritative import (
    HistoricalFeatureInputSpec,
    fit_authoritative_fold_normalizer,
    resolve_authoritative_feature_points,
)
from autotrade_research.features.causal import CausalFold, FeaturePoint


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
        self.registry = HistoricalVintageRegistry(Path(self.directory.name))
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

    def _manifest(self, content_hashes, *, version=1):
        return {
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

    @staticmethod
    def _base_events(*, through=6):
        values = ("100", "102", "105", "103", "107", "109", "111")
        return [event(day, values[day]) for day in range(through + 1)]

    def _register(self, populations):
        digests = [market_event_population_digest(rows) for rows in populations]
        manifest_digest = self.registry.commit(self._manifest(digests))
        return manifest_digest

    def _validation_point(self, rows, manifest_digest):
        points = resolve_authoritative_feature_points(
            registry=self.registry,
            dataset_id=self.dataset_id,
            dataset_version=1,
            manifest_digest=manifest_digest,
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
        manifest_digest = self._register([rows])
        fitted = fit_authoritative_fold_normalizer(
            registry=self.registry,
            dataset_id=self.dataset_id,
            dataset_version=1,
            manifest_digest=manifest_digest,
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
            events=rows,
            spec=self.spec,
            replay_common_cut_fingerprint="a" * 64,
        )
        self.assertIsInstance(value, Decimal)
        self.assertEqual(fitted.fold_normalizer.training_point_count, 3)
        self.assertTrue(fitted.fingerprint.startswith("sha256:"))

    def test_same_ids_and_revisions_with_altered_value_are_not_authority(self):
        rows = self._base_events()
        manifest_digest = self._register([rows])
        forged = [dict(row) for row in rows]
        forged[2] = {
            **forged[2],
            "payload": {"close": "999999"},
        }
        with self.assertRaisesRegex(
            HistoricalConflict,
            "population digest is not registered",
        ):
            fit_authoritative_fold_normalizer(
                registry=self.registry,
                dataset_id=self.dataset_id,
                dataset_version=1,
                manifest_digest=manifest_digest,
                events=forged,
                fold=self.fold,
                spec=self.spec,
            )

    def test_manifest_digest_mismatch_fails_before_fit(self):
        rows = self._base_events()
        self._register([rows])
        with self.assertRaisesRegex(
            HistoricalConflict,
            "manifest digest differs",
        ):
            fit_authoritative_fold_normalizer(
                registry=self.registry,
                dataset_id=self.dataset_id,
                dataset_version=1,
                manifest_digest="sha256:" + "0" * 64,
                events=rows,
                fold=self.fold,
                spec=self.spec,
            )

    def test_caller_forged_validation_feature_is_rejected(self):
        rows = self._base_events()
        manifest_digest = self._register([rows])
        fitted = fit_authoritative_fold_normalizer(
            registry=self.registry,
            dataset_id=self.dataset_id,
            dataset_version=1,
            manifest_digest=manifest_digest,
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
                events=rows,
                spec=self.spec,
            )

    def test_future_dataset_values_cannot_change_training_fit(self):
        training_only = self._base_events(through=3)
        full = self._base_events(through=6)
        full[4] = event(4, "999999999")
        full[5] = event(5, "-999999999")
        full[6] = event(6, "777777777")
        manifest_digest = self._register([training_only, full])

        first = fit_authoritative_fold_normalizer(
            registry=self.registry,
            dataset_id=self.dataset_id,
            dataset_version=1,
            manifest_digest=manifest_digest,
            events=training_only,
            fold=self.fold,
            spec=self.spec,
        )
        second = fit_authoritative_fold_normalizer(
            registry=self.registry,
            dataset_id=self.dataset_id,
            dataset_version=1,
            manifest_digest=manifest_digest,
            events=full,
            fold=self.fold,
            spec=self.spec,
        )
        self.assertEqual(
            first.training_population_fingerprint,
            second.training_population_fingerprint,
        )
        self.assertEqual(first.fold_normalizer, second.fold_normalizer)
        self.assertEqual(first.fingerprint, second.fingerprint)

    def test_late_correction_is_invisible_to_training_identity(self):
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
        manifest_digest = self._register([baseline, with_late])

        first = fit_authoritative_fold_normalizer(
            registry=self.registry,
            dataset_id=self.dataset_id,
            dataset_version=1,
            manifest_digest=manifest_digest,
            events=baseline,
            fold=self.fold,
            spec=self.spec,
        )
        second = fit_authoritative_fold_normalizer(
            registry=self.registry,
            dataset_id=self.dataset_id,
            dataset_version=1,
            manifest_digest=manifest_digest,
            events=with_late,
            fold=self.fold,
            spec=self.spec,
        )
        self.assertEqual(
            first.training_population_fingerprint,
            second.training_population_fingerprint,
        )
        self.assertEqual(first.fold_normalizer, second.fold_normalizer)
        self.assertEqual(first.fingerprint, second.fingerprint)

    def test_correction_known_before_cutoff_changes_training_identity(self):
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
        manifest_digest = self._register([baseline, with_early])

        first = fit_authoritative_fold_normalizer(
            registry=self.registry,
            dataset_id=self.dataset_id,
            dataset_version=1,
            manifest_digest=manifest_digest,
            events=baseline,
            fold=self.fold,
            spec=self.spec,
        )
        second = fit_authoritative_fold_normalizer(
            registry=self.registry,
            dataset_id=self.dataset_id,
            dataset_version=1,
            manifest_digest=manifest_digest,
            events=with_early,
            fold=self.fold,
            spec=self.spec,
        )
        self.assertNotEqual(
            first.training_population_fingerprint,
            second.training_population_fingerprint,
        )
        self.assertNotEqual(first.fold_normalizer, second.fold_normalizer)
        self.assertNotEqual(first.fingerprint, second.fingerprint)

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
        manifest_digest = self._register([rows])
        points = resolve_authoritative_feature_points(
            registry=self.registry,
            dataset_id=self.dataset_id,
            dataset_version=1,
            manifest_digest=manifest_digest,
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

    def test_replay_common_cut_must_match_at_validation(self):
        rows = self._base_events()
        manifest_digest = self._register([rows])
        fitted = fit_authoritative_fold_normalizer(
            registry=self.registry,
            dataset_id=self.dataset_id,
            dataset_version=1,
            manifest_digest=manifest_digest,
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
                events=rows,
                spec=self.spec,
                replay_common_cut_fingerprint="b" * 64,
            )

    def test_authoritative_fit_and_transform_ignore_ambient_decimal_context(self):
        rows = self._base_events()
        manifest_digest = self._register([rows])
        with localcontext() as context:
            context.prec = 6
            context.rounding = ROUND_FLOOR
            first = fit_authoritative_fold_normalizer(
                registry=self.registry,
                dataset_id=self.dataset_id,
                dataset_version=1,
                manifest_digest=manifest_digest,
                events=rows,
                fold=self.fold,
                spec=self.spec,
            )
            point = self._validation_point(rows, manifest_digest)
            transformed_first = first.transform_validation(
                point,
                fold=self.fold,
                registry=self.registry,
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
                events=rows,
                fold=self.fold,
                spec=self.spec,
            )
            transformed_second = second.transform_validation(
                point,
                fold=self.fold,
                registry=self.registry,
                events=rows,
                spec=self.spec,
            )

        self.assertEqual(first, second)
        self.assertEqual(first.fingerprint, second.fingerprint)
        self.assertEqual(transformed_first, transformed_second)

    def test_identical_authority_reproduces_exact_fit_identity(self):
        rows = self._base_events()
        manifest_digest = self._register([rows])
        first = fit_authoritative_fold_normalizer(
            registry=self.registry,
            dataset_id=self.dataset_id,
            dataset_version=1,
            manifest_digest=manifest_digest,
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
            events=list(reversed(rows)),
            fold=self.fold,
            spec=self.spec,
            replay_common_cut_fingerprint="c" * 64,
        )
        self.assertEqual(first, second)
        self.assertEqual(first.fingerprint, second.fingerprint)


if __name__ == "__main__":
    unittest.main()
