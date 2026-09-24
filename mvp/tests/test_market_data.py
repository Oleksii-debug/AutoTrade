from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest
from uuid import UUID

from mvp.autotrade_mvp.instruments import (
    InstrumentRegistry,
    InstrumentVersion,
    OffsetTransition,
    TradingCalendar,
    WeeklySession,
)
from mvp.autotrade_mvp.market_data import (
    MarketDataError,
    MarketNormalizer,
    RawMarketUpdate,
    SequenceConflict,
)


IID = "11111111-1111-4111-8111-111111111111"
EVIDENCE = {
    "artifact_id": "22222222-2222-4222-8222-222222222222",
    "sha256": "sha256:" + "a" * 64,
    "observed_at": "2026-09-24T16:00:00Z",
}


def at(month=9, day=24, hour=16, minute=0, second=0):
    return datetime(2026, month, day, hour, minute, second, tzinfo=timezone.utc)


def instrument(
    *,
    version=1,
    effective_from=at(1, 1, 0),
    status="ACTIVE",
    calendar_id="CONTINUOUS_24_7",
    timezone_id="UTC",
):
    return InstrumentVersion(
        instrument_id=IID,
        version=version,
        provider_id="provider-a",
        venue_id="venue-a",
        provider_symbol="ABC-USD",
        asset_class="CRYPTO_SPOT",
        base_currency="ABC",
        quote_currency="USD",
        settlement_currency="USD",
        quantity_unit="ABC",
        contract_multiplier=Decimal("1"),
        price_tick=Decimal("0.01"),
        quantity_step=Decimal("0.001"),
        minimum_quantity=Decimal("0.001"),
        maximum_quantity=Decimal("1000"),
        calendar_id=calendar_id,
        timezone_id=timezone_id,
        effective_from=effective_from,
        status=status,
    )


def registry():
    value = InstrumentRegistry()
    value.add(instrument())
    return value


def raw(
    kind,
    payload,
    *,
    sequence=1,
    source=at(),
    available=None,
    ingested=None,
    revision=0,
    stream=None,
):
    available = available or (source + timedelta(milliseconds=100))
    ingested = ingested or (available + timedelta(milliseconds=100))
    return RawMarketUpdate(
        provider_id="provider-a",
        venue_id="venue-a",
        provider_symbol="ABC-USD",
        kind=kind,
        source_event_at=source,
        available_at=available,
        ingested_at=ingested,
        availability_basis="PROVIDER_TIMESTAMP",
        source_sequence=sequence,
        sequence_stream=stream,
        revision=revision,
        payload=payload,
        raw_evidence_ref=EVIDENCE,
    )


class MarketNormalizationTests(unittest.TestCase):
    def test_trade_normalizes_to_contract_without_binary_numbers(self):
        normalizer = MarketNormalizer(registry())
        event = normalizer.normalize(
            raw("TRADE", {"price": "100.01", "quantity": "1.250", "side": "buy"})
        )
        UUID(event.event_id)
        self.assertEqual(event.instrument_version, f"{IID}:1")
        self.assertEqual(event.payload, {"price": "100.01", "quantity": "1.25", "side": "BUY"})
        contract = event.to_contract_dict()
        self.assertEqual(contract["source_sequence"], "1")
        self.assertEqual(contract["revision"], "0")
        self.assertEqual(contract["quality_flags"], [])

    def test_duplicate_sequence_is_explicit_but_changed_content_conflicts(self):
        normalizer = MarketNormalizer(registry())
        first = normalizer.normalize(raw("TRADE", {"price": "100", "quantity": "1"}))
        second = normalizer.normalize(
            raw(
                "TRADE",
                {"price": "100", "quantity": "1"},
                ingested=at() + timedelta(seconds=1),
            )
        )
        self.assertNotEqual(first.event_id, second.event_id)
        self.assertNotIn("DUPLICATE", first.quality_flags)
        self.assertIn("DUPLICATE", second.quality_flags)

        with self.assertRaisesRegex(SequenceConflict, "different normalized content"):
            normalizer.normalize(
                raw(
                    "TRADE",
                    {"price": "101", "quantity": "1"},
                    ingested=at() + timedelta(seconds=2),
                )
            )

    def test_late_revision_preserves_sequence_as_immutable_correction(self):
        normalizer = MarketNormalizer(registry())
        original = normalizer.normalize(
            raw("TRADE", {"price": "100", "quantity": "1"}, sequence=7, revision=0)
        )
        corrected = normalizer.normalize(
            raw(
                "TRADE",
                {"price": "101", "quantity": "1"},
                sequence=7,
                revision=1,
                available=at() + timedelta(seconds=1),
                ingested=at() + timedelta(seconds=2),
            )
        )
        self.assertNotEqual(original.event_id, corrected.event_id)
        self.assertIn("CORRECTION", corrected.quality_flags)
        self.assertNotIn("DUPLICATE", corrected.quality_flags)
        self.assertEqual(corrected.payload["price"], "101")

    def test_revision_gap_and_out_of_order_revision_are_explicit(self):
        normalizer = MarketNormalizer(registry())
        normalizer.normalize(
            raw("TRADE", {"price": "100", "quantity": "1"}, sequence=9, revision=0)
        )
        rev3 = normalizer.normalize(
            raw(
                "TRADE",
                {"price": "103", "quantity": "1"},
                sequence=9,
                revision=3,
                available=at() + timedelta(seconds=3),
                ingested=at() + timedelta(seconds=4),
            )
        )
        late_rev2 = normalizer.normalize(
            raw(
                "TRADE",
                {"price": "102", "quantity": "1"},
                sequence=9,
                revision=2,
                available=at() + timedelta(seconds=2),
                ingested=at() + timedelta(seconds=5),
            )
        )
        self.assertTrue({"CORRECTION", "REVISION_GAP"} <= set(rev3.quality_flags))
        self.assertTrue(
            {"CORRECTION", "OUT_OF_ORDER_REVISION"} <= set(late_rev2.quality_flags)
        )

    def test_initial_nonzero_revision_is_flagged_without_inventing_base(self):
        normalizer = MarketNormalizer(registry())
        revised_only = normalizer.normalize(
            raw("TRADE", {"price": "100", "quantity": "1"}, sequence=11, revision=2)
        )
        self.assertIn("REVISION_BASE_MISSING", revised_only.quality_flags)

    def test_same_revision_cannot_change_causal_identity_with_same_payload(self):
        normalizer = MarketNormalizer(registry())
        normalizer.normalize(
            raw("TRADE", {"price": "100", "quantity": "1"}, sequence=15, revision=0)
        )
        with self.assertRaisesRegex(SequenceConflict, "causal content"):
            normalizer.normalize(
                raw(
                    "TRADE",
                    {"price": "100", "quantity": "1"},
                    sequence=15,
                    revision=0,
                    available=at() + timedelta(seconds=2),
                    ingested=at() + timedelta(seconds=3),
                )
            )

    def test_higher_revision_cannot_backdate_availability(self):
        normalizer = MarketNormalizer(registry())
        normalizer.normalize(
            raw(
                "TRADE",
                {"price": "100", "quantity": "1"},
                sequence=16,
                revision=0,
                available=at() + timedelta(seconds=2),
                ingested=at() + timedelta(seconds=3),
            )
        )
        with self.assertRaisesRegex(SequenceConflict, "backdate"):
            normalizer.normalize(
                raw(
                    "TRADE",
                    {"price": "101", "quantity": "1"},
                    sequence=16,
                    revision=1,
                    available=at() + timedelta(seconds=1),
                    ingested=at() + timedelta(seconds=4),
                )
            )

    def test_sequence_gap_and_late_out_of_order_are_preserved_as_quality(self):
        normalizer = MarketNormalizer(registry())
        normalizer.normalize(raw("TRADE", {"price": "100", "quantity": "1"}, sequence=1))
        gap = normalizer.normalize(raw("TRADE", {"price": "100", "quantity": "1"}, sequence=3))
        late = normalizer.normalize(raw("TRADE", {"price": "100", "quantity": "1"}, sequence=2))
        self.assertIn("SEQUENCE_GAP", gap.quality_flags)
        self.assertIn("OUT_OF_ORDER", late.quality_flags)

    def test_multi_level_book_depth_and_delta_zero_are_normalized(self):
        normalizer = MarketNormalizer(registry())
        snapshot = normalizer.normalize(
            raw(
                "BOOK_SNAPSHOT",
                {
                    "bids": [["99.99", "1"], ["99.98", "2"]],
                    "asks": [["100.01", "1.5"], ["100.02", "2.5"]],
                },
                stream="book",
            )
        )
        self.assertEqual(len(snapshot.payload["bids"]), 2)
        self.assertEqual(snapshot.payload["asks"][1]["quantity"], "2.5")

        delta = normalizer.normalize(
            raw(
                "BOOK_DELTA",
                {"bids": [["99.99", "0"]], "asks": []},
                sequence=2,
                stream="book",
            )
        )
        self.assertEqual(delta.payload["bids"][0]["quantity"], "0")

    def test_stale_snapshot_cannot_roll_back_book_sequence_or_recover_gap(self):
        normalizer = MarketNormalizer(registry())
        normalizer.normalize(
            raw(
                "BOOK_SNAPSHOT",
                {"bids": [["99.99", "1"]], "asks": [["100.01", "1"]]},
                sequence=10,
                stream="book",
            )
        )
        normalizer.normalize(
            raw(
                "BOOK_DELTA",
                {"bids": [["99.98", "1"]], "asks": []},
                sequence=11,
                stream="book",
            )
        )

        stale = normalizer.normalize(
            raw(
                "BOOK_SNAPSHOT",
                {"bids": [["99.90", "1"]], "asks": [["100.10", "1"]]},
                sequence=5,
                stream="book",
            )
        )
        self.assertIn("OUT_OF_ORDER", stale.quality_flags)
        self.assertIn("BOOK_UNUSABLE", stale.quality_flags)
        self.assertEqual(
            normalizer.book_state(
                provider_id="provider-a",
                venue_id="venue-a",
                provider_symbol="ABC-USD",
                stream="book",
            ),
            "READY",
        )

        gap = normalizer.normalize(
            raw(
                "BOOK_DELTA",
                {"bids": [["99.97", "1"]], "asks": []},
                sequence=13,
                stream="book",
            )
        )
        self.assertIn("SEQUENCE_GAP", gap.quality_flags)
        self.assertEqual(
            normalizer.book_state(
                provider_id="provider-a",
                venue_id="venue-a",
                provider_symbol="ABC-USD",
                stream="book",
            ),
            "GAPPED",
        )

        stale_after_gap = normalizer.normalize(
            raw(
                "BOOK_SNAPSHOT",
                {"bids": [["99.80", "1"]], "asks": [["100.20", "1"]]},
                sequence=6,
                stream="book",
            )
        )
        self.assertIn("OUT_OF_ORDER", stale_after_gap.quality_flags)
        self.assertIn("BOOK_UNUSABLE", stale_after_gap.quality_flags)
        self.assertEqual(
            normalizer.book_state(
                provider_id="provider-a",
                venue_id="venue-a",
                provider_symbol="ABC-USD",
                stream="book",
            ),
            "GAPPED",
        )

    def test_book_gap_blocks_new_risk_until_new_snapshot(self):
        normalizer = MarketNormalizer(registry())
        normalizer.normalize(
            raw(
                "BOOK_SNAPSHOT",
                {"bids": [["99.99", "1"]], "asks": [["100.01", "1"]]},
                sequence=10,
                stream="book",
            )
        )
        self.assertEqual(
            normalizer.book_state(
                provider_id="provider-a",
                venue_id="venue-a",
                provider_symbol="ABC-USD",
                stream="book",
            ),
            "READY",
        )
        normalizer.require_executable_book(
            provider_id="provider-a",
            venue_id="venue-a",
            provider_symbol="ABC-USD",
            stream="book",
        )

        gap = normalizer.normalize(
            raw(
                "BOOK_DELTA",
                {"bids": [["99.98", "1"]], "asks": []},
                sequence=12,
                stream="book",
            )
        )
        self.assertIn("SEQUENCE_GAP", gap.quality_flags)
        self.assertIn("BOOK_UNUSABLE", gap.quality_flags)
        with self.assertRaisesRegex(MarketDataError, "new risk is blocked"):
            normalizer.require_executable_book(
                provider_id="provider-a",
                venue_id="venue-a",
                provider_symbol="ABC-USD",
                stream="book",
            )

        contiguous_after_gap = normalizer.normalize(
            raw(
                "BOOK_DELTA",
                {"bids": [["99.97", "1"]], "asks": []},
                sequence=13,
                stream="book",
            )
        )
        self.assertIn("BOOK_UNUSABLE", contiguous_after_gap.quality_flags)
        self.assertEqual(
            normalizer.book_state(
                provider_id="provider-a",
                venue_id="venue-a",
                provider_symbol="ABC-USD",
                stream="book",
            ),
            "GAPPED",
        )

        recovery = normalizer.normalize(
            raw(
                "BOOK_SNAPSHOT",
                {"bids": [["99.96", "2"]], "asks": [["100.02", "2"]]},
                sequence=20,
                stream="book",
            )
        )
        self.assertNotIn("BOOK_UNUSABLE", recovery.quality_flags)
        self.assertEqual(
            normalizer.book_state(
                provider_id="provider-a",
                venue_id="venue-a",
                provider_symbol="ABC-USD",
                stream="book",
            ),
            "READY",
        )
        normalizer.require_executable_book(
            provider_id="provider-a",
            venue_id="venue-a",
            provider_symbol="ABC-USD",
            stream="book",
        )

    def test_book_without_sequence_never_becomes_executable(self):
        normalizer = MarketNormalizer(registry())
        event = normalizer.normalize(
            raw(
                "BOOK_SNAPSHOT",
                {"bids": [["99.99", "1"]], "asks": [["100.01", "1"]]},
                sequence=None,
                stream="book",
            )
        )
        self.assertIn("BOOK_SEQUENCE_UNVERIFIED", event.quality_flags)
        self.assertEqual(
            normalizer.book_state(
                provider_id="provider-a",
                venue_id="venue-a",
                provider_symbol="ABC-USD",
            ),
            "UNVERIFIED",
        )
        with self.assertRaisesRegex(MarketDataError, "new risk is blocked"):
            normalizer.require_executable_book(
                provider_id="provider-a",
                venue_id="venue-a",
                provider_symbol="ABC-USD",
            )

    def test_raw_evidence_ref_is_strict_contract_evidence(self):
        base = raw("TRADE", {"price": "100", "quantity": "1"})
        with self.assertRaisesRegex(MarketDataError, "missing required"):
            RawMarketUpdate(
                provider_id=base.provider_id,
                venue_id=base.venue_id,
                provider_symbol=base.provider_symbol,
                kind=base.kind,
                source_event_at=base.source_event_at,
                available_at=base.available_at,
                ingested_at=base.ingested_at,
                availability_basis=base.availability_basis,
                revision=base.revision,
                payload=base.payload,
                raw_evidence_ref={"artifact_id": EVIDENCE["artifact_id"]},
            )
        with self.assertRaisesRegex(MarketDataError, "sha256"):
            RawMarketUpdate(
                provider_id=base.provider_id,
                venue_id=base.venue_id,
                provider_symbol=base.provider_symbol,
                kind=base.kind,
                source_event_at=base.source_event_at,
                available_at=base.available_at,
                ingested_at=base.ingested_at,
                availability_basis=base.availability_basis,
                revision=base.revision,
                payload=base.payload,
                raw_evidence_ref={**EVIDENCE, "sha256": "bad"},
            )
        with self.assertRaisesRegex(MarketDataError, "unknown fields"):
            RawMarketUpdate(
                provider_id=base.provider_id,
                venue_id=base.venue_id,
                provider_symbol=base.provider_symbol,
                kind=base.kind,
                source_event_at=base.source_event_at,
                available_at=base.available_at,
                ingested_at=base.ingested_at,
                availability_basis=base.availability_basis,
                revision=base.revision,
                payload=base.payload,
                raw_evidence_ref={**EVIDENCE, "secret": "must-not-pass"},
            )

    def test_crossed_book_and_precision_edges_fail_closed(self):
        normalizer = MarketNormalizer(registry())
        with self.assertRaisesRegex(MarketDataError, "crossed"):
            normalizer.normalize(
                raw(
                    "BOOK_SNAPSHOT",
                    {"bids": [["100.02", "1"]], "asks": [["100.01", "1"]]},
                )
            )
        with self.assertRaisesRegex(MarketDataError, "price_tick"):
            normalizer.normalize(raw("TRADE", {"price": "100.005", "quantity": "1"}))
        with self.assertRaisesRegex(MarketDataError, "exact decimal"):
            normalizer.normalize(raw("TRADE", {"price": 100.1, "quantity": "1"}))

    def test_staleness_is_flagged_and_impossible_timestamp_order_is_rejected(self):
        normalizer = MarketNormalizer(registry(), max_available_age=timedelta(seconds=2))
        update = raw(
            "QUOTE",
            {
                "bid_price": "99.99",
                "bid_quantity": "1",
                "ask_price": "100.01",
                "ask_quantity": "1",
            },
            ingested=at() + timedelta(seconds=4),
        )
        event = normalizer.normalize(update)
        self.assertIn("STALE", event.quality_flags)

        with self.assertRaisesRegex(MarketDataError, "source_event_at"):
            RawMarketUpdate(
                provider_id="provider-a",
                venue_id="venue-a",
                provider_symbol="ABC-USD",
                kind="TRADE",
                source_event_at=at(),
                available_at=at() - timedelta(seconds=1),
                ingested_at=at(),
                availability_basis="PROVIDER_TIMESTAMP",
                revision=0,
                payload={"price": "100", "quantity": "1"},
                raw_evidence_ref=EVIDENCE,
            )

    def test_delisted_and_weekend_events_are_kept_with_quality_flag(self):
        delisted = InstrumentRegistry()
        delisted.add(instrument())
        delisted.add(instrument(version=2, effective_from=at(6, 1, 0), status="DELISTED"))
        event = MarketNormalizer(delisted).normalize(
            raw(
                "STATUS",
                {"status": "halted"},
                source=at(7, 1, 12),
                available=at(7, 1, 12, 0, 1),
                ingested=at(7, 1, 12, 0, 2),
            )
        )
        self.assertIn("NOT_TRADABLE_AT_EVENT_TIME", event.quality_flags)

        weekday = TradingCalendar(
            calendar_id="WEEKDAY_UTC",
            timezone_id="UTC",
            sessions=tuple(WeeklySession(day, 9 * 60, 17 * 60) for day in range(5)),
            transitions=(OffsetTransition(at(1, 1, 0), 0),),
        )
        weekday_registry = InstrumentRegistry(calendars=(weekday,))
        weekday_registry.add(
            instrument(calendar_id="WEEKDAY_UTC", timezone_id="UTC")
        )
        saturday = datetime(2026, 9, 26, 12, tzinfo=timezone.utc)
        weekend = MarketNormalizer(weekday_registry).normalize(
            raw(
                "STATUS",
                {"status": "closed"},
                source=saturday,
                available=saturday + timedelta(seconds=1),
                ingested=saturday + timedelta(seconds=2),
            )
        )
        self.assertIn("NOT_TRADABLE_AT_EVENT_TIME", weekend.quality_flags)

    def test_bar_ohlc_and_zero_volume_are_exact(self):
        normalizer = MarketNormalizer(registry())
        event = normalizer.normalize(
            raw(
                "BAR",
                {
                    "open": "100",
                    "high": "101",
                    "low": "99",
                    "close": "100.50",
                    "volume": "0",
                },
            )
        )
        self.assertEqual(event.payload["volume"], "0")
        with self.assertRaisesRegex(MarketDataError, "OHLC"):
            normalizer.normalize(
                raw(
                    "BAR",
                    {
                        "open": "100",
                        "high": "99",
                        "low": "98",
                        "close": "100",
                        "volume": "1",
                    },
                    sequence=2,
                )
            )

    def test_funding_timestamp_string_must_be_parseable_utc(self):
        normalizer = MarketNormalizer(registry())
        with self.assertRaisesRegex(MarketDataError, "ISO UTC instant"):
            normalizer.normalize(
                raw(
                    "FUNDING",
                    {"rate": "0.0001", "next_funding_at": "not-a-dateZ"},
                )
            )
        with self.assertRaisesRegex(MarketDataError, "UTC instant"):
            normalizer.normalize(
                raw(
                    "FUNDING",
                    {
                        "rate": "0.0001",
                        "next_funding_at": "2026-09-25T00:00:00+02:00",
                    },
                )
            )

    def test_funding_rate_can_be_negative_without_float_coercion(self):
        normalizer = MarketNormalizer(registry())
        event = normalizer.normalize(
            raw(
                "FUNDING",
                {"rate": "-0.000125", "next_funding_at": at() + timedelta(hours=8)},
            )
        )
        self.assertEqual(event.payload["rate"], "-0.000125")
        self.assertTrue(event.payload["next_funding_at"].endswith("Z"))


if __name__ == "__main__":
    unittest.main()
