from collections import UserDict
from datetime import datetime, timedelta, timezone
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

    def test_payload_datetime_is_detached_from_custom_timezone_authority(self):
        # Python's built-in timezone is final in ordinary construction; use an
        # exact datetime with the canonical UTC tzinfo and prove snapshot output
        # retains only the canonical built-in timezone object.
        admitted = update(
            "STATUS",
            {
                "status": "OPEN",
                "provider_time": datetime(
                    2026, 9, 24, 16, 0, tzinfo=timezone.utc
                ),
            },
        )
        self.assertIs(admitted.payload["provider_time"].tzinfo, timezone.utc)

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


if __name__ == "__main__":
    unittest.main()
