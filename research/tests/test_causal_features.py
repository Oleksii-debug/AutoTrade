from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest

from research.autotrade_research.features.causal import (
    FeaturePoint,
    SourceValue,
    fit_normalizer,
    make_forward_label,
    require_universe_members,
    rolling_return,
    training_row,
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

    def test_delayed_revision_of_old_event_does_not_reverse_feature_time(self):
        original = source(0, "100", revision="r1")
        newer = source(1, "110", revision="r1")
        corrected_old = SourceValue.create(
            observation_id="AAA-0-r2",
            symbol="AAA",
            event_time=BASE,
            available_at=BASE + timedelta(days=2),
            value="101",
            source_revision="r2",
        )
        point = rolling_return(
            [original, newer, corrected_old],
            symbol="AAA",
            decision_time=BASE + timedelta(days=2),
            count=2,
        )
        self.assertEqual(point.input_ids, ("AAA-0-r2", "AAA-1-r1"))
        self.assertEqual(point.value, (Decimal("110") / Decimal("101")) - Decimal("1"))

    def test_universe_latest_prefers_later_event_time_over_late_old_revision(self):
        later_market_event = source(1, "110", revision="r1")
        corrected_old = SourceValue.create(
            observation_id="AAA-0-r9",
            symbol="AAA",
            event_time=BASE,
            available_at=BASE + timedelta(days=2),
            value="999",
            source_revision="r9",
        )
        selected = require_universe_members(
            ["AAA"],
            [later_market_event, corrected_old],
            decision_time=BASE + timedelta(days=2),
        )
        self.assertEqual(selected["AAA"].observation_id, "AAA-1-r1")
        self.assertEqual(selected["AAA"].value, Decimal("110"))

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


if __name__ == "__main__":
    unittest.main()
