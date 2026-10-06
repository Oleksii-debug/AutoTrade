from collections import UserDict
from datetime import datetime, timedelta, timezone, tzinfo
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, localcontext
import unittest

from mvp.autotrade_mvp.instruments import InstrumentRegistry, InstrumentVersion
from mvp.autotrade_mvp.market_data import (
    MarketDataError,
    MarketNormalizer,
    RawMarketUpdate,
)


IID = "11111111-1111-4111-8111-111111111111"
EVIDENCE = {
    "artifact_id": "22222222-2222-4222-8222-222222222222",
    "sha256": "sha256:" + "a" * 64,
    "observed_at": "2026-09-24T16:00:00Z",
}
TEST_ADAPTER_VERSION = "autotrade.market-causal-test@1"


class CallbackTimezone(tzinfo):
    def __init__(self):
        self.calls = 0

    def _fail(self):
        self.calls += 1
        raise AssertionError("caller timezone code must not execute")

    def utcoffset(self, dt):
        return self._fail()

    def dst(self, dt):
        return self._fail()

    def tzname(self, dt):
        return self._fail()


def at(second: int = 0) -> datetime:
    return datetime(2026, 9, 24, 16, 0, second, tzinfo=timezone.utc)


def registry() -> InstrumentRegistry:
    value = InstrumentRegistry()
    value.add(
        InstrumentVersion(
            instrument_id=IID,
            version=1,
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
            calendar_id="CONTINUOUS_24_7",
            timezone_id="UTC",
            effective_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
            status="ACTIVE",
        )
    )
    return value


def update(
    kind: str,
    payload,
    *,
    evidence=None,
    source=None,
    available=None,
    ingested=None,
) -> RawMarketUpdate:
    source = source or at()
    available = available or (source + timedelta(milliseconds=100))
    ingested = ingested or (available + timedelta(milliseconds=100))
    return RawMarketUpdate(
        provider_id="provider-a",
        venue_id="venue-a",
        provider_symbol="ABC-USD",
        adapter_version=TEST_ADAPTER_VERSION,
        kind=kind,
        source_event_at=source,
        available_at=available,
        ingested_at=ingested,
        availability_basis="PROVIDER_TIMESTAMP",
        source_sequence=1,
        sequence_stream="book" if kind.startswith("BOOK_") else None,
        revision=0,
        payload=payload,
        raw_evidence_ref=EVIDENCE if evidence is None else evidence,
    )


class CausalImmutableMarketPayloadTests(unittest.TestCase):

    def test_caller_scalar_subclasses_are_rejected_at_admission(self):
        class TextSubclass(str):
            pass

        class IntSubclass(int):
            pass

        class DecimalSubclass(Decimal):
            pass

        class DatetimeSubclass(datetime):
            pass

        cases = (
            ("payload-key", {TextSubclass("price"): "100.00", "quantity": "1.000"}),
            ("payload-text", {"price": TextSubclass("100.00"), "quantity": "1.000"}),
            ("payload-int", {"price": "100.00", "quantity": IntSubclass(1)}),
            ("payload-decimal", {"price": DecimalSubclass("100.00"), "quantity": "1.000"}),
            (
                "payload-datetime",
                {"status": "OPEN", "provider_time": DatetimeSubclass(
                    2026, 9, 24, 16, 0, tzinfo=timezone.utc
                )},
            ),
        )
        for name, payload in cases:
            with self.subTest(name=name), self.assertRaisesRegex(
                MarketDataError,
                "exact strings|unsupported value type",
            ):
                update("STATUS" if "datetime" in name else "TRADE", payload)

        with self.assertRaisesRegex(MarketDataError, "exact string"):
            RawMarketUpdate(
                provider_id=TextSubclass("provider-a"),
                venue_id="venue-a",
                provider_symbol="ABC-USD",
                adapter_version=TEST_ADAPTER_VERSION,
                kind="TRADE",
                source_event_at=at(),
                available_at=at(1),
                ingested_at=at(2),
                availability_basis="PROVIDER_TIMESTAMP",
                source_sequence=1,
                revision=0,
                payload={"price": "100.00", "quantity": "1.000"},
                raw_evidence_ref={
                    **EVIDENCE,
                    "observed_at": "2026-09-24T16:00:01Z",
                },
            )

        with self.assertRaisesRegex(MarketDataError, "exact non-negative integer"):
            RawMarketUpdate(
                provider_id="provider-a",
                venue_id="venue-a",
                provider_symbol="ABC-USD",
                adapter_version=TEST_ADAPTER_VERSION,
                kind="TRADE",
                source_event_at=at(),
                available_at=at(1),
                ingested_at=at(2),
                availability_basis="PROVIDER_TIMESTAMP",
                source_sequence=IntSubclass(1),
                revision=0,
                payload={"price": "100.00", "quantity": "1.000"},
                raw_evidence_ref={
                    **EVIDENCE,
                    "observed_at": "2026-09-24T16:00:01Z",
                },
            )

        with self.assertRaisesRegex(MarketDataError, "exact timezone-aware datetime"):
            RawMarketUpdate(
                provider_id="provider-a",
                venue_id="venue-a",
                provider_symbol="ABC-USD",
                adapter_version=TEST_ADAPTER_VERSION,
                kind="TRADE",
                source_event_at=DatetimeSubclass(
                    2026, 9, 24, 16, 0, tzinfo=timezone.utc
                ),
                available_at=at(1),
                ingested_at=at(2),
                availability_basis="PROVIDER_TIMESTAMP",
                source_sequence=1,
                revision=0,
                payload={"price": "100.00", "quantity": "1.000"},
                raw_evidence_ref={
                    **EVIDENCE,
                    "observed_at": "2026-09-24T16:00:01Z",
                },
            )

    def test_raw_evidence_text_subclass_cannot_enter_authority(self):
        class TextSubclass(str):
            def strip(self):
                raise AssertionError("caller text callback must not execute")

        evidence = {
            **EVIDENCE,
            "artifact_id": TextSubclass(EVIDENCE["artifact_id"]),
        }
        with self.assertRaisesRegex(MarketDataError, "exact string"):
            update(
                "TRADE",
                {"price": "100.00", "quantity": "1.000"},
                evidence=evidence,
            )

    def test_payload_datetime_rejects_custom_timezone_without_callbacks(self):
        hostile = CallbackTimezone()
        provider_time = datetime(2026, 9, 24, 16, 0, tzinfo=hostile)
        with self.assertRaisesRegex(MarketDataError, "exact built-in timezone"):
            update(
                "STATUS",
                {
                    "status": "OPEN",
                    "provider_time": provider_time,
                },
            )
        self.assertEqual(hostile.calls, 0)

    def test_payload_datetime_builtin_fixed_offset_is_detached_to_utc(self):
        plus_two = timezone(timedelta(hours=2))
        admitted = update(
            "STATUS",
            {
                "status": "OPEN",
                "provider_time": datetime(
                    2026, 9, 24, 18, 0, tzinfo=plus_two
                ),
            },
        )
        self.assertIs(admitted.payload["provider_time"].tzinfo, timezone.utc)
        self.assertEqual(
            admitted.payload["provider_time"],
            datetime(2026, 9, 24, 16, 0, tzinfo=timezone.utc),
        )

    def test_causal_boundary_and_decimal_context_preserve_event_identity(self):
        observed = []
        for precision in (6, 28, 80):
            for rounding in (ROUND_FLOOR, ROUND_CEILING):
                with localcontext() as context:
                    context.prec = precision
                    context.rounding = rounding
                    admitted = update(
                        "TRADE",
                        {"price": "100.00", "quantity": "1.000"},
                        source=at(),
                        available=at(1),
                        ingested=at(2),
                        evidence={
                            **EVIDENCE,
                            "observed_at": "2026-09-24T16:00:02Z",
                        },
                    )
                    event = MarketNormalizer(registry()).normalize(admitted)
                    observed.append((event.payload_json, event.event_id))
        self.assertTrue(all(item == observed[0] for item in observed))

    def test_raw_evidence_cannot_predate_source_event(self):
        with self.assertRaisesRegex(
            MarketDataError,
            "observed before source_event_at",
        ):
            update(
                "TRADE",
                {"price": "100.00", "quantity": "1.000"},
                source=at(1),
            )

    def test_raw_evidence_cannot_postdate_ingestion(self):
        with self.assertRaisesRegex(
            MarketDataError,
            "observed after ingested_at",
        ):
            update(
                "TRADE",
                {"price": "100.00", "quantity": "1.000"},
                source=at(),
                available=at(1),
                ingested=at(2),
                evidence={**EVIDENCE, "observed_at": "2026-09-24T16:00:03Z"},
            )

    def test_raw_evidence_at_ingestion_boundary_is_valid(self):
        admitted = update(
            "TRADE",
            {"price": "100.00", "quantity": "1.000"},
            source=at(),
            available=at(1),
            ingested=at(2),
            evidence={**EVIDENCE, "observed_at": "2026-09-24T16:00:02Z"},
        )
        event = MarketNormalizer(registry()).normalize(admitted)
        self.assertEqual(event.raw_evidence_ref["observed_at"], "2026-09-24T16:00:02Z")

    def test_nested_book_aliases_cannot_rewrite_admitted_market_truth(self):
        for kind in ("BOOK_SNAPSHOT", "BOOK_DELTA"):
            with self.subTest(kind=kind):
                bid = ["100.00", "1.000"]
                ask = ["100.01", "2.000"]
                tags = ["construction-time"]
                metadata = {"tags": tags}
                caller_payload = UserDict(
                    {
                        "bids": [bid],
                        "asks": [ask],
                        "metadata": metadata,
                    }
                )
                admitted = update(kind, caller_payload)

                # Poison every caller-owned alias after admission.
                bid[0] = "999.99"
                bid[1] = "999.000"
                ask[:] = ["0.01", "0.001"]
                tags[0] = "forged"
                metadata["new"] = ["forged"]
                caller_payload["bids"].append(["888.88", "8.000"])
                caller_payload["asks"] = [["0.02", "9.000"]]

                observed = []
                for precision in (6, 28, 80):
                    for rounding in (ROUND_FLOOR, ROUND_CEILING):
                        with localcontext() as context:
                            context.prec = precision
                            context.rounding = rounding
                            event = MarketNormalizer(registry()).normalize(admitted)
                            observed.append((event.payload_json, event.event_id))

                self.assertTrue(all(item == observed[0] for item in observed))
                self.assertEqual(
                    MarketNormalizer(registry()).normalize(admitted).payload,
                    {
                        "bids": [{"price": "100", "quantity": "1"}],
                        "asks": [{"price": "100.01", "quantity": "2"}],
                    },
                )
                self.assertEqual(
                    admitted.payload["metadata"]["tags"],
                    ("construction-time",),
                )
                self.assertNotIn("new", admitted.payload["metadata"])
                with self.assertRaises(TypeError):
                    admitted.payload["metadata"]["new"] = "forged"

    def test_normalize_rejects_subclass_that_skips_admission(self):
        class ForgedRawMarketUpdate(RawMarketUpdate):
            def __post_init__(self):
                pass

        forged = ForgedRawMarketUpdate(
            provider_id="provider-a",
            venue_id="venue-a",
            provider_symbol="ABC-USD",
            adapter_version=TEST_ADAPTER_VERSION,
            kind="BOOK_SNAPSHOT",
            source_event_at=at(),
            available_at=at(1),
            ingested_at=at(2),
            availability_basis="PROVIDER_TIMESTAMP",
            revision=0,
            payload={"bids": [["100.00", "1.000"]], "asks": [["100.01", "1.000"]]},
            raw_evidence_ref={**EVIDENCE, "observed_at": "2026-09-24T16:00:03Z"},
            source_sequence=1,
            sequence_stream="book",
        )
        normalizer = MarketNormalizer(registry())
        with self.assertRaisesRegex(MarketDataError, "exact RawMarketUpdate"):
            normalizer.normalize(forged)
        self.assertEqual(
            normalizer.book_state(
                provider_id="provider-a",
                venue_id="venue-a",
                provider_symbol="ABC-USD",
                stream="book",
            ),
            "UNINITIALIZED",
        )

    def test_normalize_readmits_exact_update_after_field_replacement(self):
        admitted = update(
            "BOOK_SNAPSHOT",
            {"bids": [["100.00", "1.000"]], "asks": [["100.01", "1.000"]]},
        )
        object.__setattr__(
            admitted,
            "raw_evidence_ref",
            {**EVIDENCE, "observed_at": "2026-09-24T16:00:03Z"},
        )
        normalizer = MarketNormalizer(registry())
        with self.assertRaisesRegex(MarketDataError, "observed after ingested_at"):
            normalizer.normalize(admitted)
        self.assertEqual(
            normalizer.book_state(
                provider_id="provider-a",
                venue_id="venue-a",
                provider_symbol="ABC-USD",
                stream="book",
            ),
            "UNINITIALIZED",
        )

    def test_raw_numeric_ingress_is_bounded_before_domain_validation(self):
        huge = "9" * 10000
        huge_int = 1 << 100000
        cases = (
            ("trade-price", update("TRADE", {"price": huge, "quantity": "1.000"})),
            ("quote-price", update("QUOTE", {
                "bid_price": huge,
                "bid_quantity": "1.000",
                "ask_price": "100.01",
                "ask_quantity": "1.000",
            })),
            ("book-price", update("BOOK_SNAPSHOT", {
                "bids": [[huge, "1.000"]],
                "asks": [["100.01", "1.000"]],
            })),
            ("trade-quantity", update("TRADE", {"price": "100.00", "quantity": huge})),
            ("book-quantity", update("BOOK_SNAPSHOT", {
                "bids": [["100.00", huge]],
                "asks": [["100.01", "1.000"]],
            })),
            ("bar-volume", update("BAR", {
                "open": "100.00",
                "high": "101.00",
                "low": "99.00",
                "close": "100.50",
                "volume": huge,
            })),
            ("funding-rate", update("FUNDING", {"rate": huge})),
            ("huge-int-price", update("MARK", {"price": huge_int})),
        )
        for name, raw in cases:
            with self.subTest(name=name), self.assertRaisesRegex(
                MarketDataError,
                "resource envelope",
            ):
                MarketNormalizer(registry()).normalize(raw)

        valid = (
            update("MARK", {"price": Decimal("100.00")}),
            update("MARK", {"price": "1.0000E+2"}),
            update("MARK", {"price": 100}),
        )
        normalized = [MarketNormalizer(registry()).normalize(raw) for raw in valid]
        self.assertEqual([event.payload["price"] for event in normalized], ["100"] * 3)

    def test_unsupported_or_recursive_nested_payload_fails_at_admission(self):
        with self.assertRaisesRegex(MarketDataError, "unsupported value type"):
            update(
                "STATUS",
                {"status": "OPEN", "opaque": object()},
            )

        recursive = []
        recursive.append(recursive)
        with self.assertRaisesRegex(MarketDataError, "reference cycle"):
            update(
                "STATUS",
                {"status": "OPEN", "recursive": recursive},
            )



    def test_raw_payload_snapshot_has_structural_resource_envelope(self):
        too_wide = {"status": "OPEN", "opaque": [None] * 20001}
        with self.assertRaisesRegex(
            MarketDataError,
            "container resource envelope",
        ):
            update("STATUS", too_wide)

        nested = None
        for _ in range(65):
            nested = [nested]
        with self.assertRaisesRegex(
            MarketDataError,
            "nesting resource envelope",
        ):
            update("STATUS", {"status": "OPEN", "opaque": nested})

    def test_book_depth_supported_width_fits_snapshot_structural_budget(self):
        bids = [
            [f"{100 - index / 100:.2f}", "1.000"]
            for index in range(10000)
        ]
        admitted = update(
            "BOOK_SNAPSHOT",
            {
                "bids": bids,
                "asks": [["100.01", "1.000"]],
            },
        )
        self.assertEqual(len(admitted.payload["bids"]), 10000)

if __name__ == "__main__":
    unittest.main()
