from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest

from research.autotrade_research.features.causal import (
    CausalFold,
    FeaturePoint,
    SourceValue,
    causal_cross_market_point,
    fit_fold_normalizer,
    fit_normalizer,
    make_forward_label,
    require_universe_members,
    rolling_return,
    training_row,
    training_rows_for_fold,
)


BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def source(i, value, *, symbol="AAA", delay=0, revision="rev-1"):
    event = BASE + timedelta(days=i)
    return SourceValue.create(
        observation_id=f"{symbol}-{i}-{revision}",
        symbol=symbol,
        event_time=event,
        available_at=event + timedelta(days=delay),
        value=value,
        source_revision=revision,
    )


class CausalFeatureTests(unittest.TestCase):
    def test_future_available_value_is_not_used(self):
        rows = [source(0, "100"), source(1, "110", delay=2)]
        with self.assertRaises(ValueError):
            rolling_return(rows, symbol="AAA", decision_time=BASE + timedelta(days=1), count=2)

    def test_rolling_feature_keeps_source_revisions(self):
        point = rolling_return(
            [source(0, "100", revision="r1"), source(1, "110", revision="r2")],
            symbol="AAA",
            decision_time=BASE + timedelta(days=1),
            count=2,
        )
        self.assertEqual(point.value, Decimal("0.1"))
        self.assertEqual(point.source_revisions, ("r1", "r2"))

    def test_normalizer_rejects_future_fit_input(self):
        points = [
            FeaturePoint("AAA", BASE, Decimal("1"), ("a",), ("r1",), "x"),
            FeaturePoint("AAA", BASE + timedelta(days=1), Decimal("2"), ("b",), ("r1",), "x"),
        ]
        with self.assertRaises(ValueError):
            fit_normalizer(points, fit_cutoff=BASE)

    def test_normalizer_has_reproducible_provenance(self):
        points = [
            FeaturePoint("AAA", BASE, Decimal("1"), ("a",), ("r1",), "x"),
            FeaturePoint("AAA", BASE + timedelta(days=1), Decimal("3"), ("b",), ("r2",), "x"),
        ]
        first = fit_normalizer(points, fit_cutoff=BASE + timedelta(days=1))
        second = fit_normalizer(points, fit_cutoff=BASE + timedelta(days=1))
        self.assertEqual(first.provenance_hash, second.provenance_hash)
        self.assertEqual(first.transform("3"), Decimal("1"))

    def test_delayed_label_cannot_enter_training_early(self):
        feature = rolling_return(
            [source(0, "100"), source(1, "101")],
            symbol="AAA",
            decision_time=BASE + timedelta(days=1),
            count=2,
        )
        future = source(2, "102", delay=3)
        label = make_forward_label(
            symbol="AAA",
            anchor_time=BASE + timedelta(days=1),
            anchor_value="101",
            future=future,
        )
        with self.assertRaises(ValueError):
            training_row(
                feature=feature,
                label=label,
                training_cutoff=BASE + timedelta(days=3),
            )
        x, y = training_row(
            feature=feature,
            label=label,
            training_cutoff=BASE + timedelta(days=5),
        )
        self.assertEqual(x, feature.value)
        self.assertEqual(y, label.value)

    def test_missing_asset_fails_explicitly(self):
        with self.assertRaises(ValueError):
            require_universe_members(
                ["AAA", "BBB"],
                [source(0, "100", symbol="AAA")],
                decision_time=BASE,
            )

    def test_latest_causally_available_revision_is_selected(self):
        first = source(0, "100", symbol="AAA", revision="r1")
        revised = SourceValue.create(
            observation_id="AAA-revised",
            symbol="AAA",
            event_time=BASE,
            available_at=BASE + timedelta(hours=1),
            value="101",
            source_revision="r2",
        )
        selected = require_universe_members(
            ["AAA"],
            [first, revised],
            decision_time=BASE + timedelta(hours=1),
        )
        self.assertEqual(selected["AAA"].source_revision, "r2")
        self.assertEqual(selected["AAA"].value, Decimal("101"))



class CausalFoldTests(unittest.TestCase):
    def point(self, day, value, *, name="x", symbol="AAA"):
        return FeaturePoint(
            symbol,
            BASE + timedelta(days=day),
            Decimal(value),
            (f"{symbol}-{day}",),
            ("r1",),
            name,
        )

    def fold(self, *, purge_seconds=0):
        return CausalFold.create(
            fold_id="fold-1",
            train_start=BASE,
            train_end=BASE + timedelta(days=3),
            validation_start=BASE + timedelta(days=4),
            validation_end=BASE + timedelta(days=6),
            purge_seconds=purge_seconds,
        )

    def test_full_dataset_input_cannot_change_training_fit(self):
        fold = self.fold()
        training = [
            self.point(0, "1"),
            self.point(1, "3"),
            self.point(2, "5"),
            self.point(3, "7"),
        ]
        future_extremes = [
            self.point(4, "1000000"),
            self.point(5, "-1000000"),
            self.point(6, "999999"),
        ]
        first = fit_fold_normalizer(
            training,
            fold=fold,
            feature_name="x",
        )
        second = fit_fold_normalizer(
            training + future_extremes,
            fold=fold,
            feature_name="x",
        )
        self.assertEqual(first.normalizer.mean, second.normalizer.mean)
        self.assertEqual(first.normalizer.scale, second.normalizer.scale)
        self.assertEqual(
            first.normalizer.provenance_hash,
            second.normalizer.provenance_hash,
        )
        self.assertEqual(second.training_point_count, 4)

    def test_purge_tail_is_excluded_from_normalizer_fit(self):
        fold = self.fold(purge_seconds=2 * 24 * 60 * 60)
        points = [
            self.point(0, "1"),
            self.point(1, "3"),
            self.point(2, "1000"),
            self.point(3, "2000"),
        ]
        fitted = fit_fold_normalizer(
            points,
            fold=fold,
            feature_name="x",
        )
        self.assertEqual(fitted.training_point_count, 3)
        self.assertNotIn("AAA-3", fitted.normalizer.fit_input_ids)

    def test_normalizer_is_bound_to_exact_fold_and_feature(self):
        fold = self.fold()
        fitted = fit_fold_normalizer(
            [self.point(0, "1"), self.point(1, "3")],
            fold=fold,
            feature_name="x",
        )
        validation = self.point(4, "5")
        self.assertIsInstance(
            fitted.transform_validation(validation, fold=fold),
            Decimal,
        )
        other = CausalFold.create(
            fold_id="fold-2",
            train_start=BASE,
            train_end=BASE + timedelta(days=3),
            validation_start=BASE + timedelta(days=4),
            validation_end=BASE + timedelta(days=6),
            purge_seconds=0,
        )
        with self.assertRaisesRegex(ValueError, "different fold"):
            fitted.transform_validation(validation, fold=other)
        with self.assertRaisesRegex(ValueError, "feature_name"):
            fitted.transform_validation(
                self.point(4, "5", name="other"),
                fold=fold,
            )

    def test_validation_point_outside_frozen_window_is_rejected(self):
        fold = self.fold()
        fitted = fit_fold_normalizer(
            [self.point(0, "1"), self.point(1, "3")],
            fold=fold,
            feature_name="x",
        )
        with self.assertRaisesRegex(ValueError, "outside"):
            fitted.transform_validation(self.point(3, "5"), fold=fold)

    def test_training_rows_exclude_labels_not_available_before_fold_cutoff(self):
        fold = self.fold(purge_seconds=24 * 60 * 60)
        feature_a = self.point(0, "1")
        label_a = make_forward_label(
            symbol="AAA",
            anchor_time=feature_a.decision_time,
            anchor_value="100",
            future=source(1, "101"),
        )
        feature_b = self.point(2, "2")
        late = SourceValue.create(
            observation_id="late-label",
            symbol="AAA",
            event_time=BASE + timedelta(days=3),
            available_at=BASE + timedelta(days=4),
            value="105",
            source_revision="r1",
        )
        label_b = make_forward_label(
            symbol="AAA",
            anchor_time=feature_b.decision_time,
            anchor_value="100",
            future=late,
        )
        rows = training_rows_for_fold(
            [(feature_a, label_a), (feature_b, label_b)],
            fold=fold,
        )
        self.assertEqual(rows, ((Decimal("1"), Decimal("0.01")),))

    def test_fold_windows_cannot_overlap(self):
        with self.assertRaisesRegex(ValueError, "must not overlap"):
            CausalFold.create(
                fold_id="bad",
                train_start=BASE,
                train_end=BASE + timedelta(days=4),
                validation_start=BASE + timedelta(days=4),
                validation_end=BASE + timedelta(days=5),
                purge_seconds=0,
            )

    def test_cross_market_point_rejects_future_component(self):
        with self.assertRaisesRegex(ValueError, "missing required universe"):
            causal_cross_market_point(
                [
                    source(0, "100", symbol="AAA"),
                    source(1, "200", symbol="BBB"),
                ],
                required_symbols=["AAA", "BBB"],
                decision_time=BASE,
                max_age_seconds=86400,
            )

    def test_cross_market_point_rejects_stale_component(self):
        with self.assertRaisesRegex(ValueError, "stale"):
            causal_cross_market_point(
                [
                    source(0, "100", symbol="AAA"),
                    source(1, "200", symbol="BBB"),
                ],
                required_symbols=["AAA", "BBB"],
                decision_time=BASE + timedelta(days=1),
                max_age_seconds=60,
            )

    def test_cross_market_provenance_changes_with_revision(self):
        first = causal_cross_market_point(
            [
                source(0, "100", symbol="AAA", revision="r1"),
                source(0, "200", symbol="BBB", revision="r1"),
            ],
            required_symbols=["AAA", "BBB"],
            decision_time=BASE,
            max_age_seconds=0,
        )
        second = causal_cross_market_point(
            [
                source(0, "101", symbol="AAA", revision="r2"),
                source(0, "200", symbol="BBB", revision="r1"),
            ],
            required_symbols=["AAA", "BBB"],
            decision_time=BASE,
            max_age_seconds=0,
        )
        self.assertNotEqual(first.provenance_hash, second.provenance_hash)


if __name__ == "__main__":
    unittest.main()
