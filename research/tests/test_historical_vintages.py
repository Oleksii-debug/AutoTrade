from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier, Lock, Thread
import unittest
from uuid import uuid4

from autotrade_research.data.vintages import (
    HistoricalConflict,
    HistoricalDataError,
    HistoricalVintageRegistry,
    explicit_missingness,
    point_in_time_market_events,
    point_in_time_universe,
    validate_multiplicative_adjustment,
)


def digest(text: str) -> str:
    return "sha256:" + sha256(text.encode("utf-8")).hexdigest()


def evidence(observed_at: str) -> dict:
    return {
        "artifact_id": str(uuid4()),
        "sha256": digest(observed_at),
        "observed_at": observed_at,
        "rights_id": "research-rights",
    }


class HistoricalVintageTests(unittest.TestCase):
    def test_point_in_time_view_hides_future_revision(self):
        event_id = str(uuid4())
        base = {
            "event_id": event_id,
            "instrument_version": "instrument:v1",
            "kind": "BAR",
            "source_event_at": "2026-01-01T10:00:00Z",
            "availability_basis": "provider-history",
            "payload": {"close": "100"},
            "quality_flags": [],
            "raw_evidence_ref": evidence("2026-01-01T10:01:00Z"),
        }
        first = {
            **base,
            "available_at": "2026-01-01T10:01:00Z",
            "ingested_at": "2026-01-01T10:02:00Z",
            "revision": "1",
        }
        correction = {
            **base,
            "available_at": "2026-01-02T09:00:00Z",
            "ingested_at": "2026-01-02T09:01:00Z",
            "revision": "2",
            "payload": {"close": "101"},
        }

        early = point_in_time_market_events(
            [first, correction],
            datetime(2026, 1, 1, 12, tzinfo=timezone.utc),
        )
        late = point_in_time_market_events(
            [first, correction],
            datetime(2026, 1, 3, tzinfo=timezone.utc),
        )
        self.assertEqual(early[0]["revision"], "1")
        self.assertEqual(early[0]["payload"]["close"], "100")
        self.assertEqual(late[0]["revision"], "2")
        self.assertEqual(late[0]["payload"]["close"], "101")

    def test_point_in_time_view_orders_by_evidenced_availability(self):
        earlier_source = {
            "event_id": str(uuid4()),
            "instrument_version": "instrument:v1",
            "kind": "TRADE",
            "source_event_at": "2026-01-01T10:00:00Z",
            "available_at": "2026-01-01T10:05:00Z",
            "ingested_at": "2026-01-01T10:05:01Z",
            "revision": "1",
            "availability_basis": "provider-history",
            "payload": {"price": "100"},
            "quality_flags": [],
            "raw_evidence_ref": evidence("2026-01-01T10:05:01Z"),
        }
        later_source_but_earlier_available = {
            "event_id": str(uuid4()),
            "instrument_version": "instrument:v1",
            "kind": "TRADE",
            "source_event_at": "2026-01-01T10:01:00Z",
            "available_at": "2026-01-01T10:02:00Z",
            "ingested_at": "2026-01-01T10:02:01Z",
            "revision": "1",
            "availability_basis": "provider-history",
            "payload": {"price": "101"},
            "quality_flags": [],
            "raw_evidence_ref": evidence("2026-01-01T10:02:01Z"),
        }
        view = point_in_time_market_events(
            [earlier_source, later_source_but_earlier_available],
            datetime(2026, 1, 1, 11, tzinfo=timezone.utc),
        )
        self.assertEqual(
            [row["event_id"] for row in view],
            [
                later_source_but_earlier_available["event_id"],
                earlier_source["event_id"],
            ],
        )

    def test_visible_revision_cannot_change_source_identity(self):
        event_id = str(uuid4())
        common = {
            "event_id": event_id,
            "instrument_version": "instrument:v1",
            "kind": "TRADE",
            "availability_basis": "provider",
            "quality_flags": [],
        }
        first = {
            **common,
            "source_event_at": "2026-01-01T10:00:00Z",
            "available_at": "2026-01-01T10:01:00Z",
            "ingested_at": "2026-01-01T10:01:01Z",
            "revision": "1",
            "payload": {"price": "10"},
            "raw_evidence_ref": evidence("2026-01-01T10:01:01Z"),
        }
        changed = {
            **common,
            "source_event_at": "2026-01-01T10:00:01Z",
            "available_at": "2026-01-01T10:02:00Z",
            "ingested_at": "2026-01-01T10:02:01Z",
            "revision": "2",
            "payload": {"price": "11"},
            "raw_evidence_ref": evidence("2026-01-01T10:02:01Z"),
        }
        with self.assertRaisesRegex(HistoricalConflict, "source identity"):
            point_in_time_market_events(
                [first, changed],
                datetime(2026, 1, 1, 12, tzinfo=timezone.utc),
            )

    def test_future_revision_does_not_contaminate_earlier_causal_view(self):
        event_id = str(uuid4())
        first = {
            "event_id": event_id,
            "instrument_version": "instrument:v1",
            "kind": "TRADE",
            "source_event_at": "2026-01-01T10:00:00Z",
            "available_at": "2026-01-01T10:01:00Z",
            "ingested_at": "2026-01-01T10:01:01Z",
            "revision": "1",
            "availability_basis": "provider",
            "payload": {"price": "10"},
            "quality_flags": [],
            "raw_evidence_ref": evidence("2026-01-01T10:01:01Z"),
        }
        future_changed_identity = {
            **first,
            "instrument_version": "instrument:future-drift",
            "available_at": "2026-01-03T10:00:00Z",
            "ingested_at": "2026-01-03T10:00:01Z",
            "revision": "2",
            "payload": {"price": "11"},
            "raw_evidence_ref": evidence("2026-01-03T10:00:01Z"),
        }
        view = point_in_time_market_events(
            [first, future_changed_identity],
            datetime(2026, 1, 2, tzinfo=timezone.utc),
        )
        self.assertEqual(len(view), 1)
        self.assertEqual(view[0]["revision"], "1")

    def test_conflicting_same_revision_is_rejected(self):
        event_id = str(uuid4())
        common = {
            "event_id": event_id,
            "instrument_version": "instrument:v1",
            "kind": "TRADE",
            "source_event_at": "2026-01-01T10:00:00Z",
            "available_at": "2026-01-01T10:00:01Z",
            "ingested_at": "2026-01-01T10:00:02Z",
            "revision": "1",
            "availability_basis": "provider",
            "quality_flags": [],
            "raw_evidence_ref": evidence("2026-01-01T10:00:01Z"),
        }
        with self.assertRaises(HistoricalConflict):
            point_in_time_market_events(
                [{**common, "payload": {"price": "10"}}, {**common, "payload": {"price": "11"}}],
                datetime(2026, 1, 2, tzinfo=timezone.utc),
            )

    def test_visible_revision_cannot_backdate_availability(self):
        event_id = str(uuid4())
        common = {
            "event_id": event_id,
            "instrument_version": "instrument:v1",
            "kind": "TRADE",
            "source_event_at": "2026-01-01T10:00:00Z",
            "availability_basis": "provider",
            "quality_flags": [],
        }
        first = {
            **common,
            "available_at": "2026-01-01T10:05:00Z",
            "ingested_at": "2026-01-01T10:06:00Z",
            "revision": "1",
            "payload": {"price": "10"},
            "raw_evidence_ref": evidence("2026-01-01T10:06:00Z"),
        }
        backdated = {
            **common,
            "available_at": "2026-01-01T10:04:00Z",
            "ingested_at": "2026-01-01T10:07:00Z",
            "revision": "2",
            "payload": {"price": "11"},
            "raw_evidence_ref": evidence("2026-01-01T10:07:00Z"),
        }
        with self.assertRaisesRegex(HistoricalConflict, "backdate"):
            point_in_time_market_events(
                [first, backdated],
                datetime(2026, 1, 1, 12, tzinfo=timezone.utc),
            )

    def test_raw_evidence_cannot_predate_source_event(self):
        row = {
            "event_id": str(uuid4()),
            "instrument_version": "instrument:v1",
            "kind": "TRADE",
            "source_event_at": "2026-01-01T10:00:02Z",
            "available_at": "2026-01-01T10:00:03Z",
            "ingested_at": "2026-01-01T10:00:04Z",
            "revision": "1",
            "availability_basis": "provider",
            "quality_flags": [],
            "payload": {"price": "10"},
            "raw_evidence_ref": evidence("2026-01-01T10:00:01Z"),
        }
        with self.assertRaisesRegex(
            HistoricalDataError,
            "observed before source_event_at",
        ):
            point_in_time_market_events(
                [row],
                datetime(2026, 1, 2, tzinfo=timezone.utc),
            )

    def test_raw_evidence_cannot_postdate_event_ingestion(self):
        row = {
            "event_id": str(uuid4()),
            "instrument_version": "instrument:v1",
            "kind": "TRADE",
            "source_event_at": "2026-01-01T10:00:00Z",
            "available_at": "2026-01-01T10:00:01Z",
            "ingested_at": "2026-01-01T10:00:02Z",
            "revision": "1",
            "availability_basis": "provider",
            "quality_flags": [],
            "payload": {"price": "10"},
            "raw_evidence_ref": evidence("2026-01-01T10:00:03Z"),
        }
        with self.assertRaisesRegex(HistoricalDataError, "observed after event ingestion"):
            point_in_time_market_events(
                [row],
                datetime(2026, 1, 2, tzinfo=timezone.utc),
            )

    def test_market_event_availability_cannot_predate_source_event(self):
        row = {
            "event_id": str(uuid4()),
            "instrument_version": "instrument:v1",
            "kind": "TRADE",
            "source_event_at": "2026-01-01T10:00:02Z",
            "available_at": "2026-01-01T10:00:01Z",
            "ingested_at": "2026-01-01T10:00:03Z",
            "revision": "1",
            "availability_basis": "provider",
            "quality_flags": [],
            "payload": {"price": "10"},
            "raw_evidence_ref": evidence("2026-01-01T10:00:03Z"),
        }
        with self.assertRaisesRegex(HistoricalDataError, "available_at cannot precede source_event_at"):
            point_in_time_market_events(
                [row],
                datetime(2026, 1, 2, tzinfo=timezone.utc),
            )

    def test_point_in_time_universe_retains_delisted_asset(self):
        delisted_id = str(uuid4())
        future_id = str(uuid4())
        rows = [
            {
                "instrument_id": delisted_id,
                "version": "2",
                "provider_symbol": "OLD",
                "effective_from": "2025-12-01T00:00:00Z",
                "status": "DELISTED",
                "metadata_evidence": [evidence("2025-12-01T01:00:00Z")],
            },
            {
                "instrument_id": future_id,
                "version": "1",
                "provider_symbol": "NEW",
                "effective_from": "2025-12-01T00:00:00Z",
                "status": "ACTIVE",
                "metadata_evidence": [evidence("2026-02-01T00:00:00Z")],
            },
        ]
        view = point_in_time_universe(rows, datetime(2026, 1, 1, tzinfo=timezone.utc))
        self.assertEqual(len(view), 1)
        self.assertEqual(view[0]["instrument_id"], delisted_id)
        self.assertEqual(view[0]["status"], "DELISTED")

    def test_missingness_is_explicit_and_never_imputed(self):
        report = explicit_missingness(["10:00", "10:01", "10:02"], ["10:00", "10:02"])
        self.assertEqual(report.missing_keys, ("10:01",))
        self.assertEqual(report.invented_count, 0)
        self.assertEqual(report.expected_count, 3)
        self.assertEqual(report.observed_count, 2)

    def test_raw_and_adjusted_series_require_exact_consistency(self):
        validate_multiplicative_adjustment(
            {"d1": Decimal("100"), "d2": Decimal("50")},
            {"d1": Decimal("50"), "d2": Decimal("25")},
            {"d1": Decimal("0.5"), "d2": Decimal("0.5")},
        )
        with self.assertRaises(HistoricalDataError):
            validate_multiplicative_adjustment(
                {"d1": "100"},
                {"d1": "49.99"},
                {"d1": "0.5"},
            )
        with self.assertRaises(HistoricalDataError):
            validate_multiplicative_adjustment(
                {"d1": 100.0},
                {"d1": "50"},
                {"d1": "0.5"},
            )

    def _manifest(self, dataset_id: str, version: int, content: str) -> dict:
        return {
            "dataset_id": dataset_id,
            "version": str(version),
            "content_hashes": [digest(content)],
            "instrument_universe_version": "universe:2026-01-01",
            "calendar_version": "calendar:2026a",
            "coverage": {"from": "2025-01-01T00:00:00Z", "to": "2026-01-01T00:00:00Z"},
            "availability_policy": {
                "point_in_time": True,
                "no_future_leakage": True,
                "cutoff": "2026-01-01T00:00:00Z",
                "basis": "evidenced-first-availability",
            },
            "revision_policy": {
                "append_only": True,
                "replace_prior_vintages": False,
            },
            "normalization_version": "market-normalization:v1",
            "adjustment_policy": {
                "raw_retained": True,
                "adjusted_available": True,
                "method": "explicit-factors",
            },
            "rights": {
                "storage": True,
                "research_use": True,
                "redistribution": False,
                "basis": "first-party-test-fixture",
            },
            "missingness_report": explicit_missingness(
                ["slot-1", "slot-2"], ["slot-1"]
            ).to_dict(),
            "source_evidence": [evidence("2026-01-01T00:00:00Z")],
            "created_at": "2026-01-01T00:00:01Z",
        }

    def test_manifest_cannot_claim_creation_before_source_evidence(self):
        with TemporaryDirectory() as directory:
            registry = HistoricalVintageRegistry(Path(directory))
            manifest = self._manifest(str(uuid4()), 1, "chronology")
            manifest["source_evidence"] = [evidence("2026-01-01T00:00:02Z")]
            manifest["created_at"] = "2026-01-01T00:00:01Z"
            with self.assertRaisesRegex(HistoricalDataError, "created_at cannot precede"):
                registry.commit(manifest)

    def test_registry_is_append_only_and_old_vintage_digest_stays_stable(self):
        with TemporaryDirectory() as directory:
            registry = HistoricalVintageRegistry(Path(directory))
            dataset_id = str(uuid4())
            first = self._manifest(dataset_id, 1, "first")
            first_digest = registry.commit(first)
            self.assertEqual(registry.commit(first), first_digest)

            changed = self._manifest(dataset_id, 1, "changed")
            with self.assertRaises(HistoricalConflict):
                registry.commit(changed)

            second = self._manifest(dataset_id, 2, "second")
            registry.commit(second)
            self.assertEqual(registry.digest(dataset_id, 1), first_digest)
            self.assertEqual(registry.load(dataset_id, 1)["content_hashes"], [digest("first")])
            self.assertEqual(registry.load(dataset_id, 2)["content_hashes"], [digest("second")])

    def test_concurrent_conflicting_writers_cannot_replace_same_vintage(self):
        with TemporaryDirectory() as directory:
            registry = HistoricalVintageRegistry(Path(directory))
            dataset_id = str(uuid4())
            manifests = (
                self._manifest(dataset_id, 1, "writer-a"),
                self._manifest(dataset_id, 1, "writer-b"),
            )
            barrier = Barrier(2)
            result_lock = Lock()
            outcomes = []

            def writer(manifest):
                barrier.wait()
                try:
                    outcome = ("ok", registry.commit(manifest))
                except HistoricalConflict as error:
                    outcome = ("conflict", str(error))
                with result_lock:
                    outcomes.append(outcome)

            threads = [Thread(target=writer, args=(manifest,)) for manifest in manifests]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)
                self.assertFalse(thread.is_alive())

            self.assertEqual(sorted(kind for kind, _ in outcomes), ["conflict", "ok"])
            stored = registry.load(dataset_id, 1)
            self.assertIn(
                stored["content_hashes"],
                ([digest("writer-a")], [digest("writer-b")]),
            )
            stored_digest = registry.digest(dataset_id, 1)
            self.assertEqual(
                stored_digest,
                next(value for kind, value in outcomes if kind == "ok"),
            )

    def test_manifest_refuses_rights_or_missingness_that_weaken_reproducibility(self):
        with TemporaryDirectory() as directory:
            registry = HistoricalVintageRegistry(Path(directory))
            dataset_id = str(uuid4())
            no_rights = self._manifest(dataset_id, 1, "x")
            no_rights["rights"]["research_use"] = False
            with self.assertRaises(HistoricalDataError):
                registry.commit(no_rights)

            invented = self._manifest(dataset_id, 2, "y")
            invented["missingness_report"]["invented_count"] = 1
            with self.assertRaises(HistoricalDataError):
                registry.commit(invented)


if __name__ == "__main__":
    unittest.main()
