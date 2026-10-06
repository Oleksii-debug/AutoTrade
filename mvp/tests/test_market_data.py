from collections import UserDict
from dataclasses import replace
from datetime import datetime, timedelta, timezone, tzinfo
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, localcontext
import unittest
from uuid import UUID

from mvp.autotrade_mvp.binance_spot import BINANCE_SPOT_DEPTH_POLICY_ID
from mvp.autotrade_mvp.instruments import (
    InstrumentRegistry,
    InstrumentVersion,
    OffsetTransition,
    TradingCalendar,
    WeeklySession,
)
from mvp.autotrade_mvp.market_data import (
    BookStreamPolicyBinding,
    _issue_qualified_book_range_admission,
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
TEST_ADAPTER_V1 = "autotrade.market-test@1"
TEST_ADAPTER_V2 = "autotrade.market-test@2"


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


class CallbackDatetime(datetime):
    calls = 0

    def astimezone(self, tz=None):
        type(self).calls += 1
        raise AssertionError("caller datetime code must not execute")

    def utcoffset(self):
        type(self).calls += 1
        raise AssertionError("caller datetime code must not execute")


class CallbackString(str):
    calls = 0

    def strip(self, chars=None):
        type(self).calls += 1
        raise AssertionError("caller string code must not execute")


class CallbackRegistry(InstrumentRegistry):
    calls = 0

    def resolve(self, *args, **kwargs):
        type(self).calls += 1
        raise AssertionError("caller registry code must not execute")


class CallbackTimedelta(timedelta):
    calls = 0

    def __le__(self, other):
        type(self).calls += 1
        raise AssertionError("caller timedelta code must not execute")


class CallbackDict(dict):
    calls = 0

    def _fail(self):
        type(self).calls += 1
        raise AssertionError("caller mapping code must not execute")

    def __len__(self):
        return self._fail()

    def __iter__(self):
        return self._fail()

    def __getitem__(self, key):
        return self._fail()

    def items(self):
        return self._fail()


class CallbackList(list):
    calls = 0

    def _fail(self):
        type(self).calls += 1
        raise AssertionError("caller sequence code must not execute")

    def __len__(self):
        return self._fail()

    def __iter__(self):
        return self._fail()

    def __getitem__(self, key):
        return self._fail()


class CallbackUserDict(UserDict):
    calls = 0

    def _fail(self):
        type(self).calls += 1
        raise AssertionError("caller UserDict code must not execute")

    def __len__(self):
        return self._fail()

    def __iter__(self):
        return self._fail()

    def __getitem__(self, key):
        return self._fail()

    def items(self):
        return self._fail()


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
    generation=None,
    evidence=None,
    adapter_version=TEST_ADAPTER_V1,
):
    available = available or (source + timedelta(milliseconds=100))
    ingested = ingested or (available + timedelta(milliseconds=100))
    return RawMarketUpdate(
        provider_id="provider-a",
        venue_id="venue-a",
        provider_symbol="ABC-USD",
        adapter_version=adapter_version,
        kind=kind,
        source_event_at=source,
        available_at=available,
        ingested_at=ingested,
        availability_basis="PROVIDER_TIMESTAMP",
        source_sequence=sequence,
        stream_generation=generation,
        sequence_stream=stream,
        revision=revision,
        payload=payload,
        raw_evidence_ref=EVIDENCE if evidence is None else evidence,
    )


def provider_raw(kind, payload, **kwargs):
    kwargs.setdefault("stream", "book")
    kwargs.setdefault("generation", 1)
    return raw(kind, payload, **kwargs)


TEST_BOOK_POLICY_ID = BINANCE_SPOT_DEPTH_POLICY_ID


def provider_book_policy(*, policy_id=TEST_BOOK_POLICY_ID):
    return BookStreamPolicyBinding(
        provider_id="provider-a",
        venue_id="venue-a",
        stream="book",
        policy_id=policy_id,
    )


def begin_provider_policy(
    normalizer,
    *,
    generation=1,
    policy_id=TEST_BOOK_POLICY_ID,
):
    normalizer.begin_provider_book_generation(
        provider_id="provider-a",
        venue_id="venue-a",
        provider_symbol="ABC-USD",
        stream="book",
        policy_id=policy_id,
        generation=generation,
    )



class MarketNormalizationTests(unittest.TestCase):
    def test_adapter_build_is_part_of_normalized_and_contract_identity(self):
        first_update = raw(
            "TRADE",
            {"price": "100.01", "quantity": "1.250", "side": "buy"},
            sequence=77,
            adapter_version=TEST_ADAPTER_V1,
        )
        first = MarketNormalizer(registry()).normalize(first_update)
        replay = MarketNormalizer(registry()).normalize(first_update)
        other_build = MarketNormalizer(registry()).normalize(
            raw(
                "TRADE",
                {"price": "100.01", "quantity": "1.250", "side": "buy"},
                sequence=77,
                adapter_version=TEST_ADAPTER_V2,
            )
        )

        self.assertEqual(first.event_id, replay.event_id)
        self.assertEqual(first.adapter_version, TEST_ADAPTER_V1)
        self.assertEqual(first.to_contract_dict()["adapter_version"], TEST_ADAPTER_V1)
        self.assertNotEqual(first.event_id, other_build.event_id)

    def test_adapter_build_token_is_exact_and_bounded(self):
        for invalid in (
            "",
            " trailing ",
            "contains space",
            "x" * 129,
        ):
            with self.subTest(adapter_version=invalid):
                with self.assertRaisesRegex(MarketDataError, "adapter_version"):
                    raw(
                        "TRADE",
                        {"price": "100.01", "quantity": "1"},
                        adapter_version=invalid,
                    )

    def test_adapter_build_change_requires_new_stream_generation_before_book_mutation(self):
        normalizer = MarketNormalizer(registry())
        first = normalizer.normalize(
            raw(
                "BOOK_SNAPSHOT",
                {"bids": [["99.99", "1"]], "asks": [["100.01", "1"]]},
                sequence=1,
                stream="book",
                generation=1,
                adapter_version=TEST_ADAPTER_V1,
            )
        )
        self.assertEqual(first.adapter_version, TEST_ADAPTER_V1)
        self.assertEqual(
            normalizer.book_state(
                provider_id="provider-a",
                venue_id="venue-a",
                provider_symbol="ABC-USD",
                stream="book",
            ),
            "READY",
        )

        with self.assertRaisesRegex(
            SequenceConflict,
            "adapter_version changed within active stream generation",
        ):
            normalizer.normalize(
                raw(
                    "BOOK_DELTA",
                    {"bids": [["99.98", "1"]], "asks": []},
                    sequence=2,
                    stream="book",
                    generation=1,
                    adapter_version=TEST_ADAPTER_V2,
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
        next_generation = normalizer.normalize(
            raw(
                "BOOK_SNAPSHOT",
                {"bids": [["99.97", "1"]], "asks": [["100.02", "1"]]},
                sequence=1,
                stream="book",
                generation=2,
                adapter_version=TEST_ADAPTER_V2,
            )
        )
        self.assertEqual(next_generation.adapter_version, TEST_ADAPTER_V2)
        self.assertEqual(
            normalizer.book_state(
                provider_id="provider-a",
                venue_id="venue-a",
                provider_symbol="ABC-USD",
                stream="book",
            ),
            "READY",
        )

    def test_correction_and_replay_cannot_cross_adapter_build_silently(self):
        normalizer = MarketNormalizer(registry())
        first = normalizer.normalize(
            raw(
                "TRADE",
                {"price": "100.01", "quantity": "1", "side": "buy"},
                sequence=88,
                revision=0,
                adapter_version=TEST_ADAPTER_V1,
            )
        )
        correction = normalizer.normalize(
            raw(
                "TRADE",
                {"price": "100.02", "quantity": "1", "side": "buy"},
                sequence=88,
                revision=1,
                available=at() + timedelta(seconds=1),
                ingested=at() + timedelta(seconds=2),
                adapter_version=TEST_ADAPTER_V1,
            )
        )
        self.assertEqual(first.adapter_version, TEST_ADAPTER_V1)
        self.assertEqual(correction.adapter_version, TEST_ADAPTER_V1)
        self.assertIn("CORRECTION", correction.quality_flags)

        with self.assertRaisesRegex(
            SequenceConflict,
            "adapter_version changed within active stream generation",
        ):
            normalizer.normalize(
                raw(
                    "TRADE",
                    {"price": "100.03", "quantity": "1", "side": "buy"},
                    sequence=88,
                    revision=2,
                    available=at() + timedelta(seconds=3),
                    ingested=at() + timedelta(seconds=4),
                    adapter_version=TEST_ADAPTER_V2,
                )
            )

        accepted = normalizer.normalize(
            raw(
                "TRADE",
                {"price": "100.03", "quantity": "1", "side": "buy"},
                sequence=88,
                revision=2,
                available=at() + timedelta(seconds=3),
                ingested=at() + timedelta(seconds=4),
                adapter_version=TEST_ADAPTER_V1,
            )
        )
        self.assertEqual(accepted.adapter_version, TEST_ADAPTER_V1)
        self.assertIn("CORRECTION", accepted.quality_flags)

    def test_market_payload_rejects_executable_containers_before_callbacks(self):
        CallbackDict.calls = 0
        CallbackList.calls = 0

        with self.assertRaisesRegex(
            MarketDataError,
            "payload must use an exact dict",
        ):
            raw(
                "TRADE",
                CallbackDict(
                    {"price": "100.01", "quantity": "1", "side": "buy"}
                ),
            )

        with self.assertRaisesRegex(
            MarketDataError,
            "unsupported value type CallbackList",
        ):
            raw(
                "BOOK_SNAPSHOT",
                {
                    "bids": CallbackList([["99.99", "1"]]),
                    "asks": [],
                },
                stream="book",
            )

        self.assertEqual(CallbackDict.calls, 0)
        self.assertEqual(CallbackList.calls, 0)

        CallbackUserDict.calls = 0
        hostile_user_dict = CallbackUserDict(
            {"price": "100.01", "quantity": "1", "side": "buy"}
        )
        with self.assertRaisesRegex(
            MarketDataError,
            "payload must use an exact dict",
        ):
            raw("TRADE", hostile_user_dict)
        self.assertEqual(CallbackUserDict.calls, 0)

        exact_user_dict = UserDict(
            {"price": "100.01", "quantity": "1", "side": "buy"}
        )
        admitted = raw("TRADE", exact_user_dict)
        exact_user_dict["price"] = "999.99"
        self.assertEqual(admitted.payload["price"], "100.01")

    def test_raw_evidence_rejects_executable_mapping_before_callbacks(self):
        CallbackDict.calls = 0
        hostile = CallbackDict(EVIDENCE)
        with self.assertRaisesRegex(
            MarketDataError,
            "raw_evidence_ref: payload must use an exact dict",
        ):
            raw(
                "TRADE",
                {"price": "100.01", "quantity": "1", "side": "buy"},
                evidence=hostile,
            )
        self.assertEqual(CallbackDict.calls, 0)

    def test_normalize_revalidates_tampered_payload_and_evidence_without_callbacks(self):
        CallbackDict.calls = 0
        normalizer = MarketNormalizer(registry())

        payload_update = raw(
            "TRADE",
            {"price": "100.01", "quantity": "1", "side": "buy"},
        )
        object.__setattr__(
            payload_update,
            "payload",
            CallbackDict(
                {"price": "100.02", "quantity": "1", "side": "buy"}
            ),
        )
        with self.assertRaisesRegex(
            MarketDataError,
            "payload must use an exact dict",
        ):
            normalizer.normalize(payload_update)

        evidence_update = raw(
            "TRADE",
            {"price": "100.01", "quantity": "1", "side": "buy"},
            sequence=2,
        )
        object.__setattr__(
            evidence_update,
            "raw_evidence_ref",
            CallbackDict(EVIDENCE),
        )
        with self.assertRaisesRegex(
            MarketDataError,
            "raw_evidence_ref: payload must use an exact dict",
        ):
            normalizer.normalize(evidence_update)

        self.assertEqual(CallbackDict.calls, 0)

    def test_normalize_rejects_tampered_frozen_snapshot_state_without_callbacks(self):
        CallbackList.calls = 0
        normalizer = MarketNormalizer(registry())

        payload_update = raw(
            "TRADE",
            {"price": "100.01", "quantity": "1", "side": "buy"},
        )
        object.__setattr__(
            payload_update.payload,
            "_items",
            CallbackList([("price", "100.02")]),
        )
        with self.assertRaisesRegex(
            MarketDataError,
            "frozen mapping state is invalid",
        ):
            normalizer.normalize(payload_update)

        evidence_update = raw(
            "TRADE",
            {"price": "100.01", "quantity": "1", "side": "buy"},
            sequence=3,
        )
        object.__setattr__(
            evidence_update.raw_evidence_ref,
            "_items",
            CallbackList([("artifact_id", EVIDENCE["artifact_id"])]),
        )
        with self.assertRaisesRegex(
            MarketDataError,
            "raw_evidence_ref: payload frozen mapping state is invalid",
        ):
            normalizer.normalize(evidence_update)

        self.assertEqual(CallbackList.calls, 0)

    def test_normalizer_rejects_registry_subclass_before_use(self):
        CallbackRegistry.calls = 0
        with self.assertRaisesRegex(TypeError, "exact InstrumentRegistry"):
            MarketNormalizer(CallbackRegistry())
        self.assertEqual(CallbackRegistry.calls, 0)

    def test_normalizer_rejects_timedelta_subclass_without_comparison(self):
        CallbackTimedelta.calls = 0
        with self.assertRaisesRegex(MarketDataError, "exact positive timedelta"):
            MarketNormalizer(
                registry(),
                max_available_age=CallbackTimedelta(seconds=5),
            )
        self.assertEqual(CallbackTimedelta.calls, 0)

        with self.assertRaisesRegex(MarketDataError, "exact positive timedelta"):
            MarketNormalizer(
                registry(),
                max_book_age=CallbackTimedelta(seconds=5),
            )
        self.assertEqual(CallbackTimedelta.calls, 0)

    def test_admission_rejects_custom_timezone_without_callbacks(self):
        hostile = CallbackTimezone()
        source = datetime(2026, 9, 24, 16, tzinfo=hostile)
        with self.assertRaisesRegex(MarketDataError, "built-in timezone"):
            raw(
                "TRADE",
                {"price": "100.01", "quantity": "1", "side": "buy"},
                source=source,
                available=at(second=1),
                ingested=at(second=2),
            )
        self.assertEqual(hostile.calls, 0)

    def test_admission_rejects_datetime_subclass_without_callbacks(self):
        CallbackDatetime.calls = 0
        source = CallbackDatetime(2026, 9, 24, 16, tzinfo=timezone.utc)
        with self.assertRaisesRegex(MarketDataError, "exact timezone-aware datetime"):
            raw(
                "TRADE",
                {"price": "100.01", "quantity": "1", "side": "buy"},
                source=source,
                available=at(second=1),
                ingested=at(second=2),
            )
        self.assertEqual(CallbackDatetime.calls, 0)

    def test_funding_payload_rejects_custom_timezone_without_callbacks(self):
        hostile = CallbackTimezone()
        next_funding = datetime(2026, 9, 24, 17, tzinfo=hostile)
        with self.assertRaisesRegex(MarketDataError, "built-in timezone"):
            raw(
                "FUNDING",
                {"rate": "0.0001", "next_funding_at": next_funding},
            )
        self.assertEqual(hostile.calls, 0)

    def test_builtin_fixed_offset_timezone_remains_supported(self):
        plus_two = timezone(timedelta(hours=2))
        source = datetime(2026, 9, 24, 18, tzinfo=plus_two)
        normalizer = MarketNormalizer(registry())
        event = normalizer.normalize(
            raw(
                "TRADE",
                {"price": "100.01", "quantity": "1", "side": "buy"},
                source=source,
                available=datetime(2026, 9, 24, 18, 0, 1, tzinfo=plus_two),
                ingested=datetime(2026, 9, 24, 18, 0, 2, tzinfo=plus_two),
            )
        )
        self.assertEqual(
            event.to_contract_dict()["source_event_at"],
            "2026-09-24T16:00:00Z",
        )

    def test_book_lookup_rejects_string_subclass_without_callbacks(self):
        CallbackString.calls = 0
        normalizer = MarketNormalizer(registry())
        with self.assertRaisesRegex(MarketDataError, "exact string"):
            normalizer.book_state(
                provider_id=CallbackString("provider-a"),
                venue_id="venue-a",
                provider_symbol="ABC-USD",
            )
        self.assertEqual(CallbackString.calls, 0)

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

    def test_high_significance_market_identity_is_context_independent(self):
        observed = []
        price = "12345678901234567890.12"
        correction_price = "12345678901234567890.13"
        for precision in (6, 10, 28, 80):
            for rounding in (ROUND_FLOOR, ROUND_CEILING):
                with self.subTest(precision=precision, rounding=rounding):
                    with localcontext() as context:
                        context.prec = precision
                        context.rounding = rounding
                        normalizer = MarketNormalizer(registry())
                        first = normalizer.normalize(
                            raw(
                                "TRADE",
                                {"price": price, "quantity": "1.250", "side": "buy"},
                                sequence=77,
                            )
                        )
                        duplicate = normalizer.normalize(
                            raw(
                                "TRADE",
                                {"price": price, "quantity": "1.250", "side": "buy"},
                                sequence=77,
                                ingested=at() + timedelta(seconds=1),
                            )
                        )
                        correction = normalizer.normalize(
                            raw(
                                "TRADE",
                                {
                                    "price": correction_price,
                                    "quantity": "1.250",
                                    "side": "buy",
                                },
                                sequence=77,
                                revision=1,
                                available=at() + timedelta(seconds=1),
                                ingested=at() + timedelta(seconds=2),
                            )
                        )
                        observed.append(
                            (
                                first.payload_json,
                                first.event_id,
                                first.quality_flags,
                                duplicate.payload_json,
                                duplicate.quality_flags,
                                correction.payload_json,
                                correction.event_id,
                                correction.quality_flags,
                            )
                        )

        self.assertTrue(all(item == observed[0] for item in observed))
        self.assertEqual(
            observed[0][0],
            '{"price":"12345678901234567890.12","quantity":"1.25","side":"BUY"}',
        )
        self.assertIn("DUPLICATE", observed[0][4])
        self.assertIn("CORRECTION", observed[0][7])

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

        with self.assertRaisesRegex(SequenceConflict, "changed causal content"):
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

    def test_historical_snapshot_correction_blocks_execution_until_new_snapshot(self):
        normalizer = MarketNormalizer(registry())
        normalizer.normalize(
            raw(
                "BOOK_SNAPSHOT",
                {"bids": [["99.99", "1"]], "asks": [["100.01", "1"]]},
                sequence=10,
                revision=0,
                stream="book",
            )
        )
        normalizer.normalize(
            raw(
                "BOOK_DELTA",
                {"bids": [["99.98", "1"]], "asks": []},
                sequence=11,
                revision=0,
                stream="book",
            )
        )
        normalizer.normalize(
            raw(
                "BOOK_DELTA",
                {"bids": [], "asks": [["100.02", "1"]]},
                sequence=12,
                revision=0,
                stream="book",
            )
        )
        normalizer.require_executable_book(
            as_of=at() + timedelta(seconds=1),
            provider_id="provider-a",
            venue_id="venue-a",
            provider_symbol="ABC-USD",
            stream="book",
        )

        corrected = normalizer.normalize(
            raw(
                "BOOK_SNAPSHOT",
                {"bids": [["99.95", "2"]], "asks": [["100.05", "2"]]},
                sequence=10,
                revision=1,
                stream="book",
                available=at() + timedelta(seconds=3),
                ingested=at() + timedelta(seconds=4),
            )
        )
        self.assertIn("CORRECTION", corrected.quality_flags)
        self.assertIn("HISTORICAL_BOOK_CORRECTION", corrected.quality_flags)
        self.assertIn("BOOK_UNUSABLE", corrected.quality_flags)
        self.assertEqual(
            normalizer.book_state(
                provider_id="provider-a",
                venue_id="venue-a",
                provider_symbol="ABC-USD",
                stream="book",
            ),
            "GAPPED",
        )
        with self.assertRaisesRegex(MarketDataError, "new risk is blocked"):
            normalizer.require_executable_book(
                provider_id="provider-a",
                venue_id="venue-a",
                provider_symbol="ABC-USD",
                stream="book",
            )

        recovery = normalizer.normalize(
            raw(
                "BOOK_SNAPSHOT",
                {"bids": [["99.96", "2"]], "asks": [["100.03", "2"]]},
                sequence=20,
                stream="book",
                available=at() + timedelta(seconds=5),
                ingested=at() + timedelta(seconds=6),
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

    def test_historical_book_correction_blocks_execution_until_new_snapshot(self):
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
                revision=0,
                stream="book",
            )
        )
        normalizer.normalize(
            raw(
                "BOOK_DELTA",
                {"bids": [], "asks": [["100.02", "1"]]},
                sequence=12,
                revision=0,
                stream="book",
            )
        )
        normalizer.require_executable_book(
            as_of=at() + timedelta(seconds=1),
            provider_id="provider-a",
            venue_id="venue-a",
            provider_symbol="ABC-USD",
            stream="book",
        )

        corrected = normalizer.normalize(
            raw(
                "BOOK_DELTA",
                {"bids": [["99.97", "2"]], "asks": []},
                sequence=11,
                revision=1,
                stream="book",
                available=at() + timedelta(seconds=3),
                ingested=at() + timedelta(seconds=4),
            )
        )
        self.assertIn("CORRECTION", corrected.quality_flags)
        self.assertIn("HISTORICAL_BOOK_CORRECTION", corrected.quality_flags)
        self.assertIn("BOOK_UNUSABLE", corrected.quality_flags)
        self.assertEqual(
            normalizer.book_state(
                provider_id="provider-a",
                venue_id="venue-a",
                provider_symbol="ABC-USD",
                stream="book",
            ),
            "GAPPED",
        )
        with self.assertRaisesRegex(MarketDataError, "new risk is blocked"):
            normalizer.require_executable_book(
                provider_id="provider-a",
                venue_id="venue-a",
                provider_symbol="ABC-USD",
                stream="book",
            )

        recovery = normalizer.normalize(
            raw(
                "BOOK_SNAPSHOT",
                {"bids": [["99.96", "2"]], "asks": [["100.03", "2"]]},
                sequence=20,
                stream="book",
                available=at() + timedelta(seconds=5),
                ingested=at() + timedelta(seconds=6),
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
            as_of=at() + timedelta(seconds=7),
            provider_id="provider-a",
            venue_id="venue-a",
            provider_symbol="ABC-USD",
            stream="book",
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
            as_of=at() + timedelta(seconds=1),
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
            as_of=at() + timedelta(seconds=1),
            provider_id="provider-a",
            venue_id="venue-a",
            provider_symbol="ABC-USD",
            stream="book",
        )

    def test_book_causal_range_and_checksum_are_preserved(self):
        normalizer = MarketNormalizer(registry())
        snapshot = normalizer.normalize(
            raw(
                "BOOK_SNAPSHOT",
                {
                    "bids": [["99.99", "1"]],
                    "asks": [["100.01", "1"]],
                    "snapshot_id": "snapshot-10",
                    "first_sequence": 9,
                    "last_sequence": 10,
                    "checksum": "crc32:1234",
                },
                sequence=10,
                stream="book",
            )
        )
        self.assertEqual(snapshot.payload["snapshot_id"], "snapshot-10")
        self.assertEqual(snapshot.payload["first_sequence"], "9")
        self.assertEqual(snapshot.payload["last_sequence"], "10")
        self.assertEqual(snapshot.payload["checksum"], "crc32:1234")

        delta = normalizer.normalize(
            raw(
                "BOOK_DELTA",
                {
                    "bids": [["99.98", "1"]],
                    "asks": [],
                    "first_sequence": 11,
                    "last_sequence": 11,
                    "previous_sequence": 10,
                    "checksum": "crc32:5678",
                },
                sequence=11,
                stream="book",
            )
        )
        self.assertEqual(delta.payload["first_sequence"], "11")
        self.assertEqual(delta.payload["last_sequence"], "11")
        self.assertEqual(delta.payload["previous_sequence"], "10")
        self.assertEqual(delta.payload["checksum"], "crc32:5678")

    def test_book_checksum_change_reuses_sequence_as_causal_conflict(self):
        normalizer = MarketNormalizer(registry())
        normalizer.normalize(
            raw(
                "BOOK_SNAPSHOT",
                {
                    "bids": [["99.99", "1"]],
                    "asks": [["100.01", "1"]],
                    "first_sequence": 10,
                    "last_sequence": 10,
                    "checksum": "crc32:aaaa",
                },
                sequence=10,
                stream="book",
            )
        )
        with self.assertRaisesRegex(SequenceConflict, "changed causal content"):
            normalizer.normalize(
                raw(
                    "BOOK_SNAPSHOT",
                    {
                        "bids": [["99.99", "1"]],
                        "asks": [["100.01", "1"]],
                        "first_sequence": 10,
                        "last_sequence": 10,
                        "checksum": "crc32:bbbb",
                    },
                    sequence=10,
                    stream="book",
                )
            )

    def test_book_previous_sequence_change_is_not_a_duplicate(self):
        normalizer = MarketNormalizer(registry())
        normalizer.normalize(
            raw(
                "BOOK_SNAPSHOT",
                {
                    "bids": [["99.99", "1"]],
                    "asks": [["100.01", "1"]],
                    "first_sequence": 10,
                    "last_sequence": 10,
                },
                sequence=10,
                stream="book",
            )
        )
        normalizer.normalize(
            raw(
                "BOOK_DELTA",
                {
                    "bids": [["99.98", "1"]],
                    "asks": [],
                    "first_sequence": 11,
                    "last_sequence": 11,
                    "previous_sequence": 10,
                },
                sequence=11,
                stream="book",
            )
        )
        with self.assertRaisesRegex(SequenceConflict, "changed causal content"):
            normalizer.normalize(
                raw(
                    "BOOK_DELTA",
                    {
                        "bids": [["99.98", "1"]],
                        "asks": [],
                        "first_sequence": 11,
                        "last_sequence": 11,
                        "previous_sequence": 9,
                    },
                    sequence=11,
                    stream="book",
                )
            )

    def test_book_causal_sequences_fail_closed_before_state_mutation(self):
        invalid_payloads = (
            {
                "bids": [["99.99", "1"]],
                "asks": [["100.01", "1"]],
                "first_sequence": True,
                "last_sequence": 10,
            },
            {
                "bids": [["99.99", "1"]],
                "asks": [["100.01", "1"]],
                "first_sequence": 11,
                "last_sequence": 10,
            },
            {
                "bids": [["99.99", "1"]],
                "asks": [["100.01", "1"]],
                "snapshot_id": "   ",
            },
            {
                "bids": [["99.99", "1"]],
                "asks": [["100.01", "1"]],
                "checksum": "   ",
            },
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                normalizer = MarketNormalizer(registry())
                with self.assertRaises(MarketDataError):
                    normalizer.normalize(
                        raw(
                            "BOOK_SNAPSHOT",
                            payload,
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
                    "UNINITIALIZED",
                )

    def test_book_causal_text_uses_bounded_utf8_resource_envelope(self):
        normalizer = MarketNormalizer(registry())
        accepted = normalizer.normalize(
            raw(
                "BOOK_SNAPSHOT",
                {
                    "bids": [["99.99", "1"]],
                    "asks": [["100.01", "1"]],
                    "snapshot_id": "s" * 1024,
                    "checksum": "é" * 512,
                },
                sequence=10,
                stream="book",
            )
        )
        self.assertEqual(accepted.payload["snapshot_id"], "s" * 1024)
        self.assertEqual(accepted.payload["checksum"], "é" * 512)

        oversized_values = (
            ("snapshot_id", "s" * 1025),
            ("checksum", "é" * 513),
        )
        for field, value in oversized_values:
            with self.subTest(field=field):
                rejected = MarketNormalizer(registry())
                with self.assertRaisesRegex(MarketDataError, "resource envelope"):
                    rejected.normalize(
                        raw(
                            "BOOK_SNAPSHOT",
                            {
                                "bids": [["99.99", "1"]],
                                "asks": [["100.01", "1"]],
                                field: value,
                            },
                            sequence=10,
                            stream="book",
                        )
                    )
                self.assertEqual(
                    rejected.book_state(
                        provider_id="provider-a",
                        venue_id="venue-a",
                        provider_symbol="ABC-USD",
                        stream="book",
                    ),
                    "UNINITIALIZED",
                )

    def test_registered_provider_book_policy_blocks_snapshot_until_policy_composition(self):
        normalizer = MarketNormalizer(
            registry(),
            book_stream_policies=(
                provider_book_policy(policy_id=TEST_BOOK_POLICY_ID),
            ),
        )
        begin_provider_policy(normalizer)
        snapshot = normalizer.normalize(
            provider_raw(
                "BOOK_SNAPSHOT",
                {"bids": [["99.99", "1"]], "asks": [["100.01", "1"]]},
                sequence=100,
                stream="book",
            )
        )
        self.assertIn("BOOK_PROVIDER_CONTINUITY_PENDING", snapshot.quality_flags)
        self.assertNotIn("BOOK_RANGE_CONTINUITY_UNVERIFIED", snapshot.quality_flags)
        self.assertIn("BOOK_UNUSABLE", snapshot.quality_flags)
        self.assertEqual(
            normalizer.book_state(
                provider_id="provider-a",
                venue_id="venue-a",
                provider_symbol="ABC-USD",
                stream="book",
            ),
            "UNINITIALIZED",
        )
        with self.assertRaisesRegex(MarketDataError, "new risk is blocked"):
            normalizer.executable_book(
                as_of=at() + timedelta(seconds=1),
                provider_id="provider-a",
                venue_id="venue-a",
                provider_symbol="ABC-USD",
                stream="book",
            )

        ranged = normalizer.normalize(
            provider_raw(
                "BOOK_DELTA",
                {
                    "bids": [["99.98", "1"]],
                    "asks": [],
                    "first_sequence": 101,
                    "last_sequence": 105,
                },
                sequence=105,
                stream="book",
            )
        )
        self.assertIn("BOOK_PROVIDER_CONTINUITY_PENDING", ranged.quality_flags)
        self.assertIn("BOOK_RANGE_CONTINUITY_UNVERIFIED", ranged.quality_flags)
        self.assertNotIn("SEQUENCE_GAP", ranged.quality_flags)
        self.assertNotIn("OUT_OF_ORDER", ranged.quality_flags)

    def test_provider_snapshot_registration_rejects_unqualified_snapshot_continuity(self):
        policy_id = TEST_BOOK_POLICY_ID
        normalizer = MarketNormalizer(
            registry(),
            book_stream_policies=(
                provider_book_policy(policy_id=policy_id),
            ),
        )
        begin_provider_policy(normalizer, policy_id=policy_id)
        snapshot = normalizer.normalize(
            provider_raw(
                "BOOK_SNAPSHOT",
                {
                    "bids": [["99.99", "1"]],
                    "asks": [["100.01", "1"]],
                    "checksum": "unverified-snapshot-checksum",
                },
                sequence=100,
                stream="book",
            )
        )
        self.assertIn(
            "BOOK_RANGE_CONTINUITY_UNVERIFIED",
            snapshot.quality_flags,
        )

        with self.assertRaisesRegex(
            MarketDataError,
            "snapshot provider continuity is not qualified",
        ):
            normalizer.register_provider_book_snapshot(
                snapshot,
                provider_id="provider-a",
                venue_id="venue-a",
                provider_symbol="ABC-USD",
                stream="book",
                policy_id=policy_id,
                cursor_sequence=100,
            )

        self.assertEqual(
            normalizer.book_state(
                provider_id="provider-a",
                venue_id="venue-a",
                provider_symbol="ABC-USD",
                stream="book",
            ),
            "UNINITIALIZED",
        )
        with self.assertRaisesRegex(
            MarketDataError,
            "provider book cursor is unavailable",
        ):
            normalizer.provider_book_cursor(
                provider_id="provider-a",
                venue_id="venue-a",
                provider_symbol="ABC-USD",
                stream="book",
            )

        begin_provider_policy(
            normalizer,
            generation=2,
            policy_id=policy_id,
        )
        clean_snapshot = normalizer.normalize(
            provider_raw(
                "BOOK_SNAPSHOT",
                {"bids": [["99.99", "1"]], "asks": [["100.01", "1"]]},
                sequence=200,
                stream="book",
                generation=2,
            )
        )
        normalizer.register_provider_book_snapshot(
            clean_snapshot,
            provider_id="provider-a",
            venue_id="venue-a",
            provider_symbol="ABC-USD",
            stream="book",
            policy_id=policy_id,
            cursor_sequence=200,
        )
        self.assertEqual(
            normalizer.book_state(
                provider_id="provider-a",
                venue_id="venue-a",
                provider_symbol="ABC-USD",
                stream="book",
            ),
            "BOOTSTRAPPING",
        )

    def test_qualified_provider_range_bootstraps_materialized_book(self):
        policy_id = TEST_BOOK_POLICY_ID
        normalizer = MarketNormalizer(
            registry(),
            max_book_age=timedelta(seconds=5),
            book_stream_policies=(
                provider_book_policy(policy_id=policy_id),
            ),
        )
        begin_provider_policy(normalizer)
        snapshot = normalizer.normalize(
            provider_raw(
                "BOOK_SNAPSHOT",
                {"bids": [["99.99", "1"]], "asks": [["100.01", "1"]]},
                sequence=100,
                stream="book",
            )
        )
        normalizer.register_provider_book_snapshot(
            snapshot,
            provider_id="provider-a",
            venue_id="venue-a",
            provider_symbol="ABC-USD",
            stream="book",
            policy_id=policy_id,
            cursor_sequence=100,
        )
        self.assertEqual(
            normalizer.book_state(
                provider_id="provider-a",
                venue_id="venue-a",
                provider_symbol="ABC-USD",
                stream="book",
            ),
            "BOOTSTRAPPING",
        )
        with self.assertRaisesRegex(MarketDataError, "new risk is blocked"):
            normalizer.executable_book(
                as_of=at() + timedelta(seconds=1),
                provider_id="provider-a",
                venue_id="venue-a",
                provider_symbol="ABC-USD",
                stream="book",
            )

        delta = normalizer.normalize(
            provider_raw(
                "BOOK_DELTA",
                {
                    "bids": [["100.00", "2"]],
                    "asks": [],
                    "first_sequence": 101,
                    "last_sequence": 105,
                },
                sequence=105,
                stream="book",
                available=at() + timedelta(seconds=1),
                ingested=at() + timedelta(seconds=1, milliseconds=100),
            )
        )
        self.assertEqual(
            normalizer.book_state(
                provider_id="provider-a",
                venue_id="venue-a",
                provider_symbol="ABC-USD",
                stream="book",
            ),
            "BOOTSTRAPPING",
        )
        normalizer.apply_qualified_book_range(
            delta,
            _issue_qualified_book_range_admission(
                policy_id=policy_id,
                event_id=delta.event_id,
                disposition="APPLY",
                prior_sequence=100,
                first_sequence=101,
                last_sequence=105,
                next_sequence=105,
            ),
            provider_id="provider-a",
            venue_id="venue-a",
            provider_symbol="ABC-USD",
            stream="book",
        )
        view = normalizer.executable_book(
            as_of=at() + timedelta(seconds=2),
            provider_id="provider-a",
            venue_id="venue-a",
            provider_symbol="ABC-USD",
            stream="book",
        )
        self.assertEqual(view["bids"][0], {"price": "100", "quantity": "2"})
        self.assertEqual(view["asks"][0], {"price": "100.01", "quantity": "1"})

    def test_provider_snapshot_registration_rejects_same_id_event_substitution(self):
        policy_id = TEST_BOOK_POLICY_ID
        normalizer = MarketNormalizer(
            registry(),
            book_stream_policies=(
                provider_book_policy(policy_id=policy_id),
            ),
        )
        begin_provider_policy(normalizer, policy_id=policy_id)
        snapshot = normalizer.normalize(
            provider_raw(
                "BOOK_SNAPSHOT",
                {"bids": [["99.99", "1"]], "asks": [["100.01", "1"]]},
                sequence=100,
                stream="book",
            )
        )

        substitutions = (
            replace(
                snapshot,
                payload_json=(
                    '{"asks":[{"price":"100.01","quantity":"1"}],'
                    '"bids":[{"price":"99.50","quantity":"9"}]}'
                ),
            ),
            replace(
                snapshot,
                available_at=snapshot.available_at + timedelta(microseconds=1),
            ),
        )
        for substituted in substitutions:
            with self.subTest(event=substituted.to_contract_dict()), self.assertRaisesRegex(
                MarketDataError,
                "differs from retained normalized identity",
            ):
                normalizer.register_provider_book_snapshot(
                    substituted,
                    provider_id="provider-a",
                    venue_id="venue-a",
                    provider_symbol="ABC-USD",
                    policy_id=policy_id,
                    cursor_sequence=100,
                )
            self.assertEqual(
                normalizer.book_state(
                    provider_id="provider-a",
                    venue_id="venue-a",
                    provider_symbol="ABC-USD",
                ),
                "UNINITIALIZED",
            )

        normalizer.register_provider_book_snapshot(
            snapshot,
            provider_id="provider-a",
            venue_id="venue-a",
            provider_symbol="ABC-USD",
            policy_id=policy_id,
            cursor_sequence=100,
        )
        self.assertEqual(
            normalizer.book_state(
                provider_id="provider-a",
                venue_id="venue-a",
                provider_symbol="ABC-USD",
            ),
            "BOOTSTRAPPING",
        )

    def test_provider_snapshot_registration_cannot_replace_active_baseline(self):
        policy_id = TEST_BOOK_POLICY_ID
        normalizer = MarketNormalizer(
            registry(),
            max_book_age=timedelta(seconds=5),
            book_stream_policies=(
                provider_book_policy(policy_id=policy_id),
            ),
        )
        begin_provider_policy(normalizer, policy_id=policy_id)
        initial = normalizer.normalize(
            provider_raw(
                "BOOK_SNAPSHOT",
                {"bids": [["99.99", "1"]], "asks": [["100.01", "1"]]},
                sequence=100,
            )
        )
        normalizer.register_provider_book_snapshot(
            initial,
            provider_id="provider-a",
            venue_id="venue-a",
            provider_symbol="ABC-USD",
            policy_id=policy_id,
            cursor_sequence=100,
        )
        self.assertEqual(
            normalizer.book_state(
                provider_id="provider-a",
                venue_id="venue-a",
                provider_symbol="ABC-USD",
            ),
            "BOOTSTRAPPING",
        )
        self.assertEqual(
            normalizer.provider_book_cursor(
                provider_id="provider-a",
                venue_id="venue-a",
                provider_symbol="ABC-USD",
            ),
            100,
        )

        replacement = normalizer.normalize(
            provider_raw(
                "BOOK_SNAPSHOT",
                {"bids": [["90.00", "1"]], "asks": [["90.01", "1"]]},
                sequence=90,
            )
        )
        with self.assertRaisesRegex(
            MarketDataError,
            "requires UNINITIALIZED stream",
        ):
            normalizer.register_provider_book_snapshot(
                replacement,
                provider_id="provider-a",
                venue_id="venue-a",
                provider_symbol="ABC-USD",
                policy_id=policy_id,
                cursor_sequence=90,
            )
        self.assertEqual(
            normalizer.provider_book_cursor(
                provider_id="provider-a",
                venue_id="venue-a",
                provider_symbol="ABC-USD",
            ),
            100,
        )
        self.assertEqual(
            normalizer.book_state(
                provider_id="provider-a",
                venue_id="venue-a",
                provider_symbol="ABC-USD",
            ),
            "BOOTSTRAPPING",
        )

        delta = normalizer.normalize(
            provider_raw(
                "BOOK_DELTA",
                {
                    "bids": [["100.00", "2"]],
                    "asks": [],
                    "first_sequence": 101,
                    "last_sequence": 101,
                },
                sequence=101,
                available=at() + timedelta(seconds=1),
                ingested=at() + timedelta(seconds=1, milliseconds=100),
            )
        )
        normalizer.apply_qualified_book_range(
            delta,
            provider_id="provider-a",
            venue_id="venue-a",
            provider_symbol="ABC-USD",
        )
        self.assertEqual(
            normalizer.book_state(
                provider_id="provider-a",
                venue_id="venue-a",
                provider_symbol="ABC-USD",
            ),
            "READY",
        )
        self.assertEqual(
            normalizer.provider_book_cursor(
                provider_id="provider-a",
                venue_id="venue-a",
                provider_symbol="ABC-USD",
            ),
            101,
        )

        ready_replacement = normalizer.normalize(
            provider_raw(
                "BOOK_SNAPSHOT",
                {"bids": [["80.00", "1"]], "asks": [["80.01", "1"]]},
                sequence=80,
                available=at() + timedelta(seconds=2),
                ingested=at() + timedelta(seconds=2, milliseconds=100),
            )
        )
        with self.assertRaisesRegex(
            MarketDataError,
            "requires UNINITIALIZED stream",
        ):
            normalizer.register_provider_book_snapshot(
                ready_replacement,
                provider_id="provider-a",
                venue_id="venue-a",
                provider_symbol="ABC-USD",
                policy_id=policy_id,
                cursor_sequence=80,
            )
        self.assertEqual(
            normalizer.provider_book_cursor(
                provider_id="provider-a",
                venue_id="venue-a",
                provider_symbol="ABC-USD",
            ),
            101,
        )
        self.assertEqual(
            normalizer.executable_book(
                as_of=at() + timedelta(seconds=2),
                provider_id="provider-a",
                venue_id="venue-a",
                provider_symbol="ABC-USD",
            )["bids"][0],
            {"price": "100", "quantity": "2"},
        )

        begin_provider_policy(
            normalizer,
            generation=2,
            policy_id=policy_id,
        )
        fresh = normalizer.normalize(
            provider_raw(
                "BOOK_SNAPSHOT",
                {"bids": [["79.99", "1"]], "asks": [["80.01", "1"]]},
                sequence=1,
                generation=2,
                available=at() + timedelta(seconds=3),
                ingested=at() + timedelta(seconds=3, milliseconds=100),
                evidence={
                    **EVIDENCE,
                    "observed_at": "2026-09-24T16:00:03.050000Z",
                },
            )
        )
        normalizer.register_provider_book_snapshot(
            fresh,
            provider_id="provider-a",
            venue_id="venue-a",
            provider_symbol="ABC-USD",
            policy_id=policy_id,
            cursor_sequence=1,
        )
        self.assertEqual(
            normalizer.provider_book_cursor(
                provider_id="provider-a",
                venue_id="venue-a",
                provider_symbol="ABC-USD",
            ),
            1,
        )
        self.assertEqual(
            normalizer.book_state(
                provider_id="provider-a",
                venue_id="venue-a",
                provider_symbol="ABC-USD",
            ),
            "BOOTSTRAPPING",
        )

    def test_qualified_provider_range_discard_and_gap_are_state_safe(self):
        policy_id = TEST_BOOK_POLICY_ID
        normalizer = MarketNormalizer(
            registry(),
            book_stream_policies=(
                provider_book_policy(policy_id=policy_id),
            ),
        )
        begin_provider_policy(normalizer)
        snapshot = normalizer.normalize(
            provider_raw(
                "BOOK_SNAPSHOT",
                {"bids": [["99.99", "1"]], "asks": [["100.01", "1"]]},
                sequence=100,
                stream="book",
            )
        )
        normalizer.register_provider_book_snapshot(
            snapshot,
            provider_id="provider-a",
            venue_id="venue-a",
            provider_symbol="ABC-USD",
            policy_id=policy_id,
            cursor_sequence=100,
        )
        old = normalizer.normalize(
            provider_raw(
                "BOOK_DELTA",
                {
                    "bids": [["99.50", "1"]],
                    "asks": [],
                    "first_sequence": 90,
                    "last_sequence": 100,
                },
                sequence=100,
                stream="book",
            )
        )
        self.assertIn("BOOK_PROVIDER_CONTINUITY_PENDING", old.quality_flags)
        self.assertNotIn("DUPLICATE", old.quality_flags)
        normalizer.apply_qualified_book_range(
            old,
            _issue_qualified_book_range_admission(
                policy_id=policy_id,
                event_id=old.event_id,
                disposition="DISCARD",
                prior_sequence=100,
                first_sequence=90,
                last_sequence=100,
                next_sequence=None,
            ),
            provider_id="provider-a",
            venue_id="venue-a",
            provider_symbol="ABC-USD",
        )
        self.assertEqual(
            normalizer.book_state(
                provider_id="provider-a",
                venue_id="venue-a",
                provider_symbol="ABC-USD",
            ),
            "BOOTSTRAPPING",
        )

        old_correction = normalizer.normalize(
            provider_raw(
                "BOOK_DELTA",
                {
                    "bids": [["99.40", "2"]],
                    "asks": [],
                    "first_sequence": 90,
                    "last_sequence": 100,
                },
                sequence=100,
                stream="book",
                revision=1,
                available=at() + timedelta(seconds=1),
                ingested=at() + timedelta(seconds=1, milliseconds=100),
            )
        )
        self.assertIn("CORRECTION", old_correction.quality_flags)
        discarded_correction = normalizer.apply_qualified_book_range(
            old_correction,
            provider_id="provider-a",
            venue_id="venue-a",
            provider_symbol="ABC-USD",
        )
        self.assertEqual(discarded_correction.disposition, "DISCARD")
        self.assertEqual(
            normalizer.book_state(
                provider_id="provider-a",
                venue_id="venue-a",
                provider_symbol="ABC-USD",
            ),
            "BOOTSTRAPPING",
        )
        self.assertEqual(
            normalizer.provider_book_cursor(
                provider_id="provider-a",
                venue_id="venue-a",
                provider_symbol="ABC-USD",
            ),
            100,
        )

        gap = normalizer.normalize(
            provider_raw(
                "BOOK_DELTA",
                {
                    "bids": [["99.60", "1"]],
                    "asks": [],
                    "first_sequence": 110,
                    "last_sequence": 115,
                },
                sequence=115,
                stream="book",
            )
        )
        normalizer.apply_qualified_book_range(
            gap,
            _issue_qualified_book_range_admission(
                policy_id=policy_id,
                event_id=gap.event_id,
                disposition="GAP",
                prior_sequence=100,
                first_sequence=110,
                last_sequence=115,
                next_sequence=None,
            ),
            provider_id="provider-a",
            venue_id="venue-a",
            provider_symbol="ABC-USD",
        )
        self.assertEqual(
            normalizer.book_state(
                provider_id="provider-a",
                venue_id="venue-a",
                provider_symbol="ABC-USD",
            ),
            "GAPPED",
        )

    def test_historical_provider_delta_correction_invalidates_ready_book(self):
        policy_id = TEST_BOOK_POLICY_ID
        normalizer = MarketNormalizer(
            registry(),
            max_book_age=timedelta(seconds=5),
            book_stream_policies=(
                provider_book_policy(policy_id=policy_id),
            ),
        )
        begin_provider_policy(normalizer, policy_id=policy_id)
        snapshot = normalizer.normalize(
            provider_raw(
                "BOOK_SNAPSHOT",
                {"bids": [["99.99", "1"]], "asks": [["100.01", "1"]]},
                sequence=100,
                stream="book",
            )
        )
        normalizer.register_provider_book_snapshot(
            snapshot,
            provider_id="provider-a",
            venue_id="venue-a",
            provider_symbol="ABC-USD",
            stream="book",
            policy_id=policy_id,
            cursor_sequence=100,
        )

        first = normalizer.normalize(
            provider_raw(
                "BOOK_DELTA",
                {
                    "bids": [["100.00", "2"]],
                    "asks": [],
                    "first_sequence": 101,
                    "last_sequence": 105,
                },
                sequence=105,
                stream="book",
            )
        )
        normalizer.apply_qualified_book_range(
            first,
            provider_id="provider-a",
            venue_id="venue-a",
            provider_symbol="ABC-USD",
            stream="book",
        )
        second = normalizer.normalize(
            provider_raw(
                "BOOK_DELTA",
                {
                    "bids": [["99.98", "1"]],
                    "asks": [],
                    "first_sequence": 106,
                    "last_sequence": 110,
                },
                sequence=110,
                stream="book",
            )
        )
        normalizer.apply_qualified_book_range(
            second,
            provider_id="provider-a",
            venue_id="venue-a",
            provider_symbol="ABC-USD",
            stream="book",
        )
        self.assertEqual(
            normalizer.provider_book_cursor(
                provider_id="provider-a",
                venue_id="venue-a",
                provider_symbol="ABC-USD",
                stream="book",
            ),
            110,
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

        correction = normalizer.normalize(
            provider_raw(
                "BOOK_DELTA",
                {
                    "bids": [["100.00", "3"]],
                    "asks": [],
                    "first_sequence": 101,
                    "last_sequence": 105,
                },
                sequence=105,
                stream="book",
                revision=1,
                available=at() + timedelta(seconds=1),
                ingested=at() + timedelta(seconds=1, milliseconds=100),
            )
        )
        self.assertIn("CORRECTION", correction.quality_flags)

        with self.assertRaisesRegex(
            MarketDataError,
            "historical provider range correction requires book rebuild",
        ):
            normalizer.apply_qualified_book_range(
                correction,
                provider_id="provider-a",
                venue_id="venue-a",
                provider_symbol="ABC-USD",
                stream="book",
            )

        self.assertEqual(
            normalizer.book_state(
                provider_id="provider-a",
                venue_id="venue-a",
                provider_symbol="ABC-USD",
                stream="book",
            ),
            "GAPPED",
        )
        with self.assertRaisesRegex(
            MarketDataError,
            "provider book cursor is unavailable",
        ):
            normalizer.provider_book_cursor(
                provider_id="provider-a",
                venue_id="venue-a",
                provider_symbol="ABC-USD",
                stream="book",
            )
        with self.assertRaisesRegex(MarketDataError, "new risk is blocked"):
            normalizer.executable_book(
                as_of=at() + timedelta(seconds=2),
                provider_id="provider-a",
                venue_id="venue-a",
                provider_symbol="ABC-USD",
                stream="book",
            )

    def test_caller_forged_apply_cannot_override_registered_gap(self):
        policy_id = TEST_BOOK_POLICY_ID
        normalizer = MarketNormalizer(
            registry(),
            book_stream_policies=(
                provider_book_policy(policy_id=policy_id),
            ),
        )
        begin_provider_policy(normalizer, policy_id=policy_id)
        snapshot = normalizer.normalize(
            provider_raw(
                "BOOK_SNAPSHOT",
                {"bids": [["99.99", "1"]], "asks": [["100.01", "1"]]},
                sequence=100,
                stream="book",
            )
        )
        normalizer.register_provider_book_snapshot(
            snapshot,
            provider_id="provider-a",
            venue_id="venue-a",
            provider_symbol="ABC-USD",
            policy_id=policy_id,
            cursor_sequence=100,
        )
        gap = normalizer.normalize(
            provider_raw(
                "BOOK_DELTA",
                {
                    "bids": [["99.98", "2"]],
                    "asks": [],
                    "first_sequence": 102,
                    "last_sequence": 105,
                },
                sequence=105,
                stream="book",
            )
        )
        forged_apply = _issue_qualified_book_range_admission(
            policy_id=policy_id,
            event_id=gap.event_id,
            disposition="APPLY",
            prior_sequence=100,
            first_sequence=102,
            last_sequence=105,
            next_sequence=105,
        )
        with self.assertRaisesRegex(
            MarketDataError,
            "differs from registered provider policy",
        ):
            normalizer.apply_qualified_book_range(
                gap,
                forged_apply,
                provider_id="provider-a",
                venue_id="venue-a",
                provider_symbol="ABC-USD",
            )
        self.assertEqual(
            normalizer.book_state(
                provider_id="provider-a",
                venue_id="venue-a",
                provider_symbol="ABC-USD",
            ),
            "BOOTSTRAPPING",
        )
        authoritative = normalizer.apply_qualified_book_range(
            gap,
            provider_id="provider-a",
            venue_id="venue-a",
            provider_symbol="ABC-USD",
        )
        self.assertEqual(authoritative.disposition, "GAP")
        self.assertEqual(
            normalizer.book_state(
                provider_id="provider-a",
                venue_id="venue-a",
                provider_symbol="ABC-USD",
            ),
            "GAPPED",
        )

    def test_provider_range_rejects_same_id_event_content_substitution(self):
        policy_id = TEST_BOOK_POLICY_ID
        normalizer = MarketNormalizer(
            registry(),
            book_stream_policies=(
                provider_book_policy(policy_id=policy_id),
            ),
        )
        begin_provider_policy(normalizer, policy_id=policy_id)
        snapshot = normalizer.normalize(
            provider_raw(
                "BOOK_SNAPSHOT",
                {"bids": [["99.99", "1"]], "asks": [["100.01", "1"]]},
                sequence=100,
                stream="book",
            )
        )
        normalizer.register_provider_book_snapshot(
            snapshot,
            provider_id="provider-a",
            venue_id="venue-a",
            provider_symbol="ABC-USD",
            policy_id=policy_id,
            cursor_sequence=100,
        )
        delta = normalizer.normalize(
            provider_raw(
                "BOOK_DELTA",
                {
                    "bids": [["100.00", "2"]],
                    "asks": [],
                    "first_sequence": 101,
                    "last_sequence": 105,
                },
                sequence=105,
                stream="book",
            )
        )
        substituted = replace(
            delta,
            payload_json=(
                '{"asks":[],"bids":[{"price":"99.50","quantity":"9"}],'
                '"first_sequence":"101","last_sequence":"105"}'
            ),
        )
        with self.assertRaisesRegex(
            MarketDataError,
            "differs from retained normalized identity",
        ):
            normalizer.apply_qualified_book_range(
                substituted,
                provider_id="provider-a",
                venue_id="venue-a",
                provider_symbol="ABC-USD",
            )
        self.assertEqual(
            normalizer.book_state(
                provider_id="provider-a",
                venue_id="venue-a",
                provider_symbol="ABC-USD",
            ),
            "BOOTSTRAPPING",
        )

    def test_provider_book_event_identity_retention_is_bounded_and_eviction_fails_closed(self):
        policy_id = TEST_BOOK_POLICY_ID
        normalizer = MarketNormalizer(
            registry(),
            max_retained_book_events_per_stream=2,
            book_stream_policies=(
                provider_book_policy(policy_id=policy_id),
            ),
        )
        begin_provider_policy(normalizer, policy_id=policy_id)
        snapshot = normalizer.normalize(
            provider_raw(
                "BOOK_SNAPSHOT",
                {"bids": [["99.99", "1"]], "asks": [["100.01", "1"]]},
                sequence=100,
                stream="book",
            )
        )
        normalizer.register_provider_book_snapshot(
            snapshot,
            provider_id="provider-a",
            venue_id="venue-a",
            provider_symbol="ABC-USD",
            policy_id=policy_id,
            cursor_sequence=100,
        )
        first = normalizer.normalize(
            provider_raw(
                "BOOK_DELTA",
                {
                    "bids": [["99.98", "1"]],
                    "asks": [],
                    "first_sequence": 101,
                    "last_sequence": 101,
                },
                sequence=101,
                stream="book",
            )
        )
        normalizer.normalize(
            provider_raw(
                "BOOK_DELTA",
                {
                    "bids": [["99.97", "1"]],
                    "asks": [],
                    "first_sequence": 102,
                    "last_sequence": 102,
                },
                sequence=102,
                stream="book",
            )
        )
        normalizer.normalize(
            provider_raw(
                "BOOK_DELTA",
                {
                    "bids": [["99.96", "1"]],
                    "asks": [],
                    "first_sequence": 103,
                    "last_sequence": 103,
                },
                sequence=103,
                stream="book",
            )
        )

        self.assertEqual(len(normalizer._book_event_keys), 2)
        self.assertEqual(len(normalizer._book_event_contracts), 2)
        with self.assertRaisesRegex(
            MarketDataError,
            "book event does not belong to this stream",
        ):
            normalizer.apply_qualified_book_range(
                first,
                provider_id="provider-a",
                venue_id="venue-a",
                provider_symbol="ABC-USD",
            )

    def test_provider_book_causal_state_is_bounded_and_evicted_sequence_fails_closed(self):
        policy_id = TEST_BOOK_POLICY_ID
        normalizer = MarketNormalizer(
            registry(),
            max_retained_book_events_per_stream=3,
            book_stream_policies=(
                provider_book_policy(policy_id=policy_id),
            ),
        )
        begin_provider_policy(normalizer, policy_id=policy_id)
        for sequence in range(100, 150):
            normalizer.normalize(
                provider_raw(
                    "BOOK_DELTA",
                    {
                        "bids": [["99.98", "1"]],
                        "asks": [],
                        "first_sequence": sequence,
                        "last_sequence": sequence,
                    },
                    sequence=sequence,
                )
            )

        self.assertLessEqual(len(normalizer._seen_sequence_ids), 3)
        self.assertLessEqual(len(normalizer._last_revision), 3)
        self.assertLessEqual(len(normalizer._last_revision_available_at), 3)
        self.assertLessEqual(len(normalizer._sequence_source_identity), 3)
        self.assertLessEqual(len(normalizer._seen_revision), 3)
        self.assertLessEqual(
            len(normalizer._provider_book_sequence_refcounts),
            3,
        )
        self.assertEqual(
            [len(order) for order in normalizer._provider_book_causal_order.values()],
            [3],
        )
        with self.assertRaisesRegex(
            SequenceConflict,
            "outside the retained causal window",
        ):
            normalizer.normalize(
                provider_raw(
                    "BOOK_DELTA",
                    {
                        "bids": [["99.98", "1"]],
                        "asks": [],
                        "first_sequence": 100,
                        "last_sequence": 100,
                    },
                    sequence=100,
                )
            )

    def test_provider_book_evicted_revision_fails_closed_while_sequence_is_retained(self):
        policy_id = TEST_BOOK_POLICY_ID
        normalizer = MarketNormalizer(
            registry(),
            max_retained_book_events_per_stream=2,
            book_stream_policies=(
                provider_book_policy(policy_id=policy_id),
            ),
        )
        begin_provider_policy(normalizer, policy_id=policy_id)
        normalizer.normalize(
            provider_raw(
                "BOOK_DELTA",
                {
                    "bids": [["99.98", "1"]],
                    "asks": [],
                    "first_sequence": 200,
                    "last_sequence": 200,
                },
                sequence=200,
                revision=0,
            )
        )
        normalizer.normalize(
            provider_raw(
                "BOOK_DELTA",
                {
                    "bids": [["99.98", "2"]],
                    "asks": [],
                    "first_sequence": 200,
                    "last_sequence": 200,
                },
                sequence=200,
                revision=1,
            )
        )
        normalizer.normalize(
            provider_raw(
                "BOOK_DELTA",
                {
                    "bids": [["99.97", "1"]],
                    "asks": [],
                    "first_sequence": 201,
                    "last_sequence": 201,
                },
                sequence=201,
                revision=0,
            )
        )

        self.assertEqual(len(normalizer._seen_revision), 2)
        self.assertEqual(
            sorted(normalizer._provider_book_evicted_revision_watermark.values()),
            [0],
        )
        with self.assertRaisesRegex(
            SequenceConflict,
            "revision is outside the retained causal window",
        ):
            normalizer.normalize(
                provider_raw(
                    "BOOK_DELTA",
                    {
                        "bids": [["99.98", "1"]],
                        "asks": [],
                        "first_sequence": 200,
                        "last_sequence": 200,
                    },
                    sequence=200,
                    revision=0,
                )
            )

    def test_provider_generation_advance_releases_causal_detail_but_keeps_old_frames_fenced(self):
        policy_id = TEST_BOOK_POLICY_ID
        normalizer = MarketNormalizer(
            registry(),
            max_retained_book_events_per_stream=2,
            book_stream_policies=(
                provider_book_policy(policy_id=policy_id),
            ),
        )
        begin_provider_policy(normalizer, policy_id=policy_id)
        for sequence in (100, 101, 102):
            normalizer.normalize(
                provider_raw(
                    "BOOK_DELTA",
                    {
                        "bids": [["99.98", "1"]],
                        "asks": [],
                        "first_sequence": sequence,
                        "last_sequence": sequence,
                    },
                    sequence=sequence,
                )
            )
        self.assertTrue(normalizer._seen_sequence_ids)
        self.assertTrue(normalizer._provider_book_causal_order)
        self.assertTrue(normalizer._provider_book_evicted_sequence_watermark)

        begin_provider_policy(
            normalizer,
            generation=2,
            policy_id=policy_id,
        )
        self.assertEqual(normalizer._seen_sequence_ids, set())
        self.assertEqual(normalizer._last_revision, {})
        self.assertEqual(normalizer._last_revision_available_at, {})
        self.assertEqual(normalizer._sequence_source_identity, {})
        self.assertEqual(normalizer._seen_revision, {})
        self.assertEqual(normalizer._provider_book_causal_order, {})
        self.assertEqual(normalizer._provider_book_sequence_refcounts, {})
        self.assertEqual(
            normalizer._provider_book_evicted_sequence_watermark,
            {},
        )
        self.assertEqual(
            normalizer._provider_book_evicted_revision_watermark,
            {},
        )
        self.assertEqual(normalizer._book_event_keys, {})
        self.assertEqual(normalizer._book_event_contracts, {})

        with self.assertRaisesRegex(
            MarketDataError,
            "superseded stream generation",
        ):
            normalizer.normalize(
                provider_raw(
                    "BOOK_DELTA",
                    {
                        "bids": [["99.98", "1"]],
                        "asks": [],
                        "first_sequence": 103,
                        "last_sequence": 103,
                    },
                    sequence=103,
                    generation=1,
                )
            )
        fresh = normalizer.normalize(
            provider_raw(
                "BOOK_DELTA",
                {
                    "bids": [["99.98", "1"]],
                    "asks": [],
                    "first_sequence": 1,
                    "last_sequence": 1,
                },
                sequence=1,
                generation=2,
            )
        )
        self.assertNotIn("OUT_OF_ORDER", fresh.quality_flags)
        self.assertNotIn("SEQUENCE_GAP", fresh.quality_flags)

    def test_provider_book_causal_bound_does_not_prune_generic_sequence_authority(self):
        policy_id = TEST_BOOK_POLICY_ID
        normalizer = MarketNormalizer(
            registry(),
            max_retained_book_events_per_stream=2,
            book_stream_policies=(
                provider_book_policy(policy_id=policy_id),
            ),
        )
        begin_provider_policy(normalizer, policy_id=policy_id)
        for sequence in range(1, 6):
            normalizer.normalize(
                raw(
                    "TRADE",
                    {"price": "100", "quantity": "1"},
                    sequence=sequence,
                    stream="trades",
                )
            )
        for sequence in range(100, 105):
            normalizer.normalize(
                provider_raw(
                    "BOOK_DELTA",
                    {
                        "bids": [["99.98", "1"]],
                        "asks": [],
                        "first_sequence": sequence,
                        "last_sequence": sequence,
                    },
                    sequence=sequence,
                )
            )

        generic_sequences = {
            identity
            for identity in normalizer._seen_sequence_ids
            if identity[3] == "trades"
        }
        self.assertEqual(len(generic_sequences), 5)
        provider_sequences = {
            identity
            for identity in normalizer._seen_sequence_ids
            if identity[3] == "book:BOOK_DELTA"
        }
        self.assertLessEqual(len(provider_sequences), 2)

    def test_provider_generation_transition_releases_retained_event_identity(self):
        policy_id = TEST_BOOK_POLICY_ID
        normalizer = MarketNormalizer(
            registry(),
            book_stream_policies=(
                provider_book_policy(policy_id=policy_id),
            ),
        )
        begin_provider_policy(normalizer, policy_id=policy_id)
        normalizer.normalize(
            provider_raw(
                "BOOK_DELTA",
                {
                    "bids": [["99.98", "1"]],
                    "asks": [],
                    "first_sequence": 101,
                    "last_sequence": 101,
                },
                sequence=101,
                stream="book",
            )
        )
        self.assertEqual(len(normalizer._book_event_keys), 1)
        normalizer.invalidate_provider_book_stream(
            provider_id="provider-a",
            venue_id="venue-a",
            provider_symbol="ABC-USD",
            policy_id=policy_id,
        )
        self.assertEqual(normalizer._book_event_keys, {})
        self.assertEqual(normalizer._book_event_contracts, {})

    def test_invalidated_provider_generation_requires_explicit_advance_before_rebootstrap(self):
        policy_id = TEST_BOOK_POLICY_ID
        normalizer = MarketNormalizer(
            registry(),
            book_stream_policies=(
                provider_book_policy(policy_id=policy_id),
            ),
        )
        begin_provider_policy(normalizer, policy_id=policy_id)
        initial = normalizer.normalize(
            provider_raw(
                "BOOK_SNAPSHOT",
                {"bids": [["99.99", "1"]], "asks": [["100.01", "1"]]},
                sequence=100,
            )
        )
        normalizer.register_provider_book_snapshot(
            initial,
            provider_id="provider-a",
            venue_id="venue-a",
            provider_symbol="ABC-USD",
            policy_id=policy_id,
            cursor_sequence=100,
        )
        normalizer.invalidate_provider_book_stream(
            provider_id="provider-a",
            venue_id="venue-a",
            provider_symbol="ABC-USD",
            policy_id=policy_id,
        )
        self.assertEqual(
            normalizer.book_state(
                provider_id="provider-a",
                venue_id="venue-a",
                provider_symbol="ABC-USD",
            ),
            "UNINITIALIZED",
        )

        same_generation = normalizer.normalize(
            provider_raw(
                "BOOK_SNAPSHOT",
                {"bids": [["100.00", "1"]], "asks": [["100.02", "1"]]},
                sequence=101,
            )
        )
        with self.assertRaisesRegex(
            MarketDataError,
            "requires a newer generation",
        ):
            normalizer.register_provider_book_snapshot(
                same_generation,
                provider_id="provider-a",
                venue_id="venue-a",
                provider_symbol="ABC-USD",
                policy_id=policy_id,
                cursor_sequence=101,
            )

        begin_provider_policy(
            normalizer,
            generation=2,
            policy_id=policy_id,
        )
        fresh = normalizer.normalize(
            provider_raw(
                "BOOK_SNAPSHOT",
                {"bids": [["100.00", "1"]], "asks": [["100.02", "1"]]},
                sequence=1,
                generation=2,
            )
        )
        normalizer.register_provider_book_snapshot(
            fresh,
            provider_id="provider-a",
            venue_id="venue-a",
            provider_symbol="ABC-USD",
            policy_id=policy_id,
            cursor_sequence=1,
        )
        self.assertEqual(
            normalizer.provider_book_cursor(
                provider_id="provider-a",
                venue_id="venue-a",
                provider_symbol="ABC-USD",
            ),
            1,
        )
        self.assertEqual(
            normalizer.book_state(
                provider_id="provider-a",
                venue_id="venue-a",
                provider_symbol="ABC-USD",
            ),
            "BOOTSTRAPPING",
        )

    def test_qualified_range_admission_is_event_policy_and_cursor_bound(self):
        policy_id = TEST_BOOK_POLICY_ID
        normalizer = MarketNormalizer(
            registry(),
            book_stream_policies=(
                provider_book_policy(policy_id=policy_id),
            ),
        )
        begin_provider_policy(normalizer)
        snapshot = normalizer.normalize(
            provider_raw(
                "BOOK_SNAPSHOT",
                {"bids": [["99.99", "1"]], "asks": [["100.01", "1"]]},
                sequence=100,
                stream="book",
            )
        )
        normalizer.register_provider_book_snapshot(
            snapshot,
            provider_id="provider-a",
            venue_id="venue-a",
            provider_symbol="ABC-USD",
            policy_id=policy_id,
            cursor_sequence=100,
        )
        delta = normalizer.normalize(
            provider_raw(
                "BOOK_DELTA",
                {
                    "bids": [["99.98", "2"]],
                    "asks": [],
                    "first_sequence": 101,
                    "last_sequence": 105,
                },
                sequence=105,
                stream="book",
            )
        )
        for admission in (
            _issue_qualified_book_range_admission(
                policy_id="wrong-policy",
                event_id=delta.event_id,
                disposition="APPLY",
                prior_sequence=100,
                first_sequence=101,
                last_sequence=105,
                next_sequence=105,
            ),
            _issue_qualified_book_range_admission(
                policy_id=policy_id,
                event_id=snapshot.event_id,
                disposition="APPLY",
                prior_sequence=100,
                first_sequence=101,
                last_sequence=105,
                next_sequence=105,
            ),
            _issue_qualified_book_range_admission(
                policy_id=policy_id,
                event_id=delta.event_id,
                disposition="APPLY",
                prior_sequence=99,
                first_sequence=101,
                last_sequence=105,
                next_sequence=105,
            ),
            _issue_qualified_book_range_admission(
                policy_id=policy_id,
                event_id=delta.event_id,
                disposition="APPLY",
                prior_sequence=100,
                first_sequence=102,
                last_sequence=105,
                next_sequence=105,
            ),
        ):
            with self.subTest(admission=admission), self.assertRaises(MarketDataError):
                normalizer.apply_qualified_book_range(
                    delta,
                    admission,
                    provider_id="provider-a",
                    venue_id="venue-a",
                    provider_symbol="ABC-USD",
                )
        self.assertEqual(
            normalizer.book_state(
                provider_id="provider-a",
                venue_id="venue-a",
                provider_symbol="ABC-USD",
            ),
            "BOOTSTRAPPING",
        )

    def test_provider_bound_book_requires_generation_and_exact_stream_selection(self):
        policy_id = TEST_BOOK_POLICY_ID
        normalizer = MarketNormalizer(
            registry(),
            book_stream_policies=(
                provider_book_policy(policy_id=policy_id),
            ),
        )
        with self.assertRaisesRegex(MarketDataError, "active generation"):
            normalizer.normalize(
                raw(
                    "BOOK_SNAPSHOT",
                    {"bids": [["99.99", "1"]], "asks": [["100.01", "1"]]},
                    sequence=100,
                    stream="book",
                    generation=1,
                )
            )
        begin_provider_policy(normalizer, policy_id=policy_id)
        for bad_stream in (None, "depth"):
            with self.subTest(stream=bad_stream), self.assertRaisesRegex(
                MarketDataError,
                "bound sequence_stream",
            ):
                normalizer.normalize(
                    raw(
                        "BOOK_SNAPSHOT",
                        {"bids": [["99.99", "1"]], "asks": [["100.01", "1"]]},
                        sequence=100,
                        stream=bad_stream,
                        generation=1,
                    )
                )

    def test_book_stream_policy_binding_is_exact_and_unique(self):
        binding = provider_book_policy()
        with self.assertRaisesRegex(MarketDataError, "duplicate"):
            MarketNormalizer(
                registry(),
                book_stream_policies=(binding, binding),
            )
        with self.assertRaisesRegex(MarketDataError, "exact tuple"):
            MarketNormalizer(
                registry(),
                book_stream_policies=[binding],
            )
        with self.assertRaisesRegex(MarketDataError, "canonical provider policy"):
            MarketNormalizer(
                registry(),
                book_stream_policies=(
                    provider_book_policy(policy_id="CALLER_ALWAYS_APPLY"),
                ),
            )
        with self.assertRaises(TypeError):
            BookStreamPolicyBinding(
                provider_id="provider-a",
                venue_id="venue-a",
                stream="book",
                policy_id=TEST_BOOK_POLICY_ID,
                range_evaluator=lambda **_: _issue_qualified_book_range_admission(
                    policy_id=TEST_BOOK_POLICY_ID,
                    event_id="11111111-1111-4111-8111-111111111111",
                    disposition="APPLY",
                    prior_sequence=100,
                    first_sequence=200,
                    last_sequence=205,
                    next_sequence=205,
                ),
            )

    def test_ranged_book_stays_non_executable_without_qualified_policy(self):
        normalizer = MarketNormalizer(registry())
        snapshot = normalizer.normalize(
            raw(
                "BOOK_SNAPSHOT",
                {
                    "bids": [["99.99", "1"]],
                    "asks": [["100.01", "1"]],
                    "first_sequence": 100,
                    "last_sequence": 100,
                },
                sequence=100,
                stream="book",
            )
        )
        self.assertIn(
            "BOOK_RANGE_CONTINUITY_UNVERIFIED",
            snapshot.quality_flags,
        )
        self.assertIn("BOOK_UNUSABLE", snapshot.quality_flags)
        self.assertEqual(
            normalizer.book_state(
                provider_id="provider-a",
                venue_id="venue-a",
                provider_symbol="ABC-USD",
                stream="book",
            ),
            "UNVERIFIED",
        )
        with self.assertRaisesRegex(MarketDataError, "new risk is blocked"):
            normalizer.require_executable_book(
                as_of=at() + timedelta(seconds=1),
                provider_id="provider-a",
                venue_id="venue-a",
                provider_symbol="ABC-USD",
                stream="book",
            )

        ranged_delta = normalizer.normalize(
            raw(
                "BOOK_DELTA",
                {
                    "bids": [["99.98", "1"]],
                    "asks": [],
                    "first_sequence": 101,
                    "last_sequence": 105,
                },
                sequence=105,
                stream="book",
            )
        )
        self.assertIn(
            "BOOK_RANGE_CONTINUITY_UNVERIFIED",
            ranged_delta.quality_flags,
        )
        self.assertNotIn("SEQUENCE_GAP", ranged_delta.quality_flags)
        self.assertNotIn("OUT_OF_ORDER", ranged_delta.quality_flags)
        self.assertIn("BOOK_UNUSABLE", ranged_delta.quality_flags)
        self.assertEqual(
            normalizer.book_state(
                provider_id="provider-a",
                venue_id="venue-a",
                provider_symbol="ABC-USD",
                stream="book",
            ),
            "UNVERIFIED",
        )

    def test_stale_contiguous_delta_gaps_book_until_fresh_snapshot(self):
        normalizer = MarketNormalizer(
            registry(),
            max_available_age=timedelta(seconds=5),
            max_book_age=timedelta(seconds=5),
        )
        normalizer.normalize(
            raw(
                "BOOK_SNAPSHOT",
                {"bids": [["99.99", "1"]], "asks": [["100.01", "1"]]},
                sequence=10,
                stream="book",
            )
        )
        normalizer.require_executable_book(
            as_of=at() + timedelta(seconds=1),
            provider_id="provider-a",
            venue_id="venue-a",
            provider_symbol="ABC-USD",
            stream="book",
        )

        stale = normalizer.normalize(
            raw(
                "BOOK_DELTA",
                {"bids": [["99.98", "1"]], "asks": []},
                sequence=11,
                stream="book",
                available=at() + timedelta(seconds=1),
                ingested=at() + timedelta(seconds=7),
            )
        )
        self.assertIn("STALE", stale.quality_flags)
        self.assertIn("BOOK_UNUSABLE", stale.quality_flags)
        self.assertEqual(
            normalizer.book_state(
                provider_id="provider-a",
                venue_id="venue-a",
                provider_symbol="ABC-USD",
                stream="book",
            ),
            "GAPPED",
        )
        with self.assertRaisesRegex(MarketDataError, "new risk is blocked"):
            normalizer.require_executable_book(
                as_of=at() + timedelta(seconds=8),
                provider_id="provider-a",
                venue_id="venue-a",
                provider_symbol="ABC-USD",
                stream="book",
            )

        later_delta = normalizer.normalize(
            raw(
                "BOOK_DELTA",
                {"bids": [["99.97", "1"]], "asks": []},
                sequence=12,
                stream="book",
                available=at() + timedelta(seconds=8),
                ingested=at() + timedelta(seconds=8, milliseconds=100),
            )
        )
        self.assertIn("BOOK_UNUSABLE", later_delta.quality_flags)
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
                available=at() + timedelta(seconds=9),
                ingested=at() + timedelta(seconds=9, milliseconds=100),
            )
        )
        self.assertNotIn("BOOK_UNUSABLE", recovery.quality_flags)
        normalizer.require_executable_book(
            as_of=at() + timedelta(seconds=10),
            provider_id="provider-a",
            venue_id="venue-a",
            provider_symbol="ABC-USD",
            stream="book",
        )

    def test_ready_book_requires_explicit_fresh_decision_time(self):
        normalizer = MarketNormalizer(
            registry(),
            max_book_age=timedelta(seconds=5),
        )
        snapshot = normalizer.normalize(
            raw(
                "BOOK_SNAPSHOT",
                {"bids": [["99.99", "1"]], "asks": [["100.01", "1"]]},
                sequence=10,
                stream="book",
            )
        )
        with self.assertRaisesRegex(MarketDataError, "as_of is required"):
            normalizer.require_executable_book(
                provider_id="provider-a",
                venue_id="venue-a",
                provider_symbol="ABC-USD",
                stream="book",
            )
        normalizer.require_executable_book(
            as_of=at() + timedelta(seconds=5),
            provider_id="provider-a",
            venue_id="venue-a",
            provider_symbol="ABC-USD",
            stream="book",
        )
        with self.assertRaisesRegex(MarketDataError, "book data is stale"):
            normalizer.require_executable_book(
                as_of=at() + timedelta(seconds=6),
                provider_id="provider-a",
                venue_id="venue-a",
                provider_symbol="ABC-USD",
                stream="book",
            )
        with self.assertRaisesRegex(MarketDataError, "precedes"):
            normalizer.require_executable_book(
                as_of=snapshot.available_at - timedelta(microseconds=1),
                provider_id="provider-a",
                venue_id="venue-a",
                provider_symbol="ABC-USD",
                stream="book",
            )

    def test_fresh_delta_advances_book_freshness_but_duplicate_does_not(self):
        normalizer = MarketNormalizer(
            registry(),
            max_book_age=timedelta(seconds=5),
        )
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
                available=at() + timedelta(seconds=4),
                ingested=at() + timedelta(seconds=4, milliseconds=100),
            )
        )
        normalizer.require_executable_book(
            as_of=at() + timedelta(seconds=8),
            provider_id="provider-a",
            venue_id="venue-a",
            provider_symbol="ABC-USD",
            stream="book",
        )

        duplicate = normalizer.normalize(
            raw(
                "BOOK_DELTA",
                {"bids": [["99.98", "1"]], "asks": []},
                sequence=11,
                stream="book",
                available=at() + timedelta(seconds=4),
                ingested=at() + timedelta(seconds=4, milliseconds=200),
            )
        )
        self.assertIn("DUPLICATE", duplicate.quality_flags)
        with self.assertRaisesRegex(MarketDataError, "book data is stale"):
            normalizer.require_executable_book(
                as_of=at() + timedelta(seconds=10),
                provider_id="provider-a",
                venue_id="venue-a",
                provider_symbol="ABC-USD",
                stream="book",
            )

    def test_materialized_book_applies_delta_and_returns_sorted_detached_view(self):
        normalizer = MarketNormalizer(registry(), max_book_age=timedelta(seconds=5))
        normalizer.normalize(
            raw(
                "BOOK_SNAPSHOT",
                {
                    "bids": [["99.99", "1"], ["99.98", "2"]],
                    "asks": [["100.01", "1"], ["100.02", "2"]],
                },
                sequence=10,
                stream="book",
            )
        )
        normalizer.normalize(
            raw(
                "BOOK_DELTA",
                {
                    "bids": [["99.99", "0"], ["100.00", "3"]],
                    "asks": [["100.02", "0"], ["100.03", "4"]],
                },
                sequence=11,
                stream="book",
                available=at() + timedelta(seconds=1),
                ingested=at() + timedelta(seconds=1, milliseconds=100),
            )
        )
        view = normalizer.executable_book(
            as_of=at() + timedelta(seconds=2),
            provider_id="provider-a",
            venue_id="venue-a",
            provider_symbol="ABC-USD",
            stream="book",
        )
        self.assertEqual(
            view,
            {
                "bids": [
                    {"price": "100", "quantity": "3"},
                    {"price": "99.98", "quantity": "2"},
                ],
                "asks": [
                    {"price": "100.01", "quantity": "1"},
                    {"price": "100.03", "quantity": "4"},
                ],
            },
        )
        view["bids"].clear()
        self.assertEqual(
            normalizer.executable_book(
                as_of=at() + timedelta(seconds=2),
                provider_id="provider-a",
                venue_id="venue-a",
                provider_symbol="ABC-USD",
                stream="book",
            )["bids"][0]["price"],
            "100",
        )

    def test_delta_created_cross_revokes_executable_book_until_new_snapshot(self):
        normalizer = MarketNormalizer(registry())
        normalizer.normalize(
            raw(
                "BOOK_SNAPSHOT",
                {"bids": [["99.99", "1"]], "asks": [["100.01", "1"]]},
                sequence=10,
                stream="book",
            )
        )
        crossed = normalizer.normalize(
            raw(
                "BOOK_DELTA",
                {"bids": [["100.02", "1"]], "asks": []},
                sequence=11,
                stream="book",
                available=at() + timedelta(seconds=1),
                ingested=at() + timedelta(seconds=1, milliseconds=100),
            )
        )
        self.assertIn("BOOK_CROSSED", crossed.quality_flags)
        self.assertIn("BOOK_UNUSABLE", crossed.quality_flags)
        self.assertEqual(
            normalizer.book_state(
                provider_id="provider-a",
                venue_id="venue-a",
                provider_symbol="ABC-USD",
                stream="book",
            ),
            "GAPPED",
        )
        with self.assertRaisesRegex(MarketDataError, "new risk is blocked"):
            normalizer.executable_book(
                as_of=at() + timedelta(seconds=2),
                provider_id="provider-a",
                venue_id="venue-a",
                provider_symbol="ABC-USD",
                stream="book",
            )

        normalizer.normalize(
            raw(
                "BOOK_SNAPSHOT",
                {"bids": [["99.95", "2"]], "asks": [["100.05", "2"]]},
                sequence=20,
                stream="book",
                available=at() + timedelta(seconds=3),
                ingested=at() + timedelta(seconds=3, milliseconds=100),
            )
        )
        recovered = normalizer.executable_book(
            as_of=at() + timedelta(seconds=4),
            provider_id="provider-a",
            venue_id="venue-a",
            provider_symbol="ABC-USD",
            stream="book",
        )
        self.assertEqual(recovered["bids"][0]["price"], "99.95")
        self.assertEqual(recovered["asks"][0]["price"], "100.05")

    def test_latest_book_delta_correction_requires_rebuild(self):
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
                revision=0,
                stream="book",
            )
        )
        corrected = normalizer.normalize(
            raw(
                "BOOK_DELTA",
                {"bids": [["99.97", "2"]], "asks": []},
                sequence=11,
                revision=1,
                stream="book",
                available=at() + timedelta(seconds=1),
                ingested=at() + timedelta(seconds=2),
            )
        )
        self.assertIn("CORRECTION", corrected.quality_flags)
        self.assertIn("BOOK_CORRECTION_REBUILD_REQUIRED", corrected.quality_flags)
        self.assertIn("BOOK_UNUSABLE", corrected.quality_flags)
        self.assertEqual(
            normalizer.book_state(
                provider_id="provider-a",
                venue_id="venue-a",
                provider_symbol="ABC-USD",
                stream="book",
            ),
            "GAPPED",
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
                adapter_version=base.adapter_version,
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
                adapter_version=base.adapter_version,
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
                adapter_version=base.adapter_version,
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
        with self.assertRaisesRegex(MarketDataError, "unsupported value type float"):
            normalizer.normalize(raw("TRADE", {"price": 100.1, "quantity": "1"}))

    def test_stale_sequenced_snapshot_cannot_establish_executable_book(self):
        normalizer = MarketNormalizer(
            registry(),
            max_available_age=timedelta(seconds=5),
        )
        event = normalizer.normalize(
            raw(
                "BOOK_SNAPSHOT",
                {"bids": [["99.99", "1"]], "asks": [["100.01", "1"]]},
                sequence=10,
                stream="book",
                available=at(),
                ingested=at() + timedelta(seconds=6),
            )
        )
        self.assertIn("STALE", event.quality_flags)
        self.assertIn("BOOK_UNUSABLE", event.quality_flags)
        self.assertEqual(
            normalizer.book_state(
                provider_id="provider-a",
                venue_id="venue-a",
                provider_symbol="ABC-USD",
                stream="book",
            ),
            "GAPPED",
        )
        with self.assertRaisesRegex(MarketDataError, "new risk is blocked"):
            normalizer.require_executable_book(
                provider_id="provider-a",
                venue_id="venue-a",
                provider_symbol="ABC-USD",
                stream="book",
            )

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
                adapter_version=TEST_ADAPTER_V1,
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
                evidence={
                    **EVIDENCE,
                    "observed_at": "2026-07-01T12:00:01Z",
                },
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
                evidence={
                    **EVIDENCE,
                    "observed_at": "2026-09-26T12:00:01Z",
                },
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

    def test_funding_string_timestamp_must_be_a_real_utc_instant(self):
        normalizer = MarketNormalizer(registry())
        with self.assertRaisesRegex(MarketDataError, "UTC instant"):
            normalizer.normalize(
                raw(
                    "FUNDING",
                    {"rate": "0.001", "next_funding_at": "garbageZ"},
                )
            )

        with self.assertRaisesRegex(MarketDataError, "UTC instant"):
            normalizer.normalize(
                raw(
                    "FUNDING",
                    {"rate": "0.001", "next_funding_at": "2026-09-25Z"},
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





    def test_book_depth_resource_envelope_is_exact_and_fail_closed(self):
        for invalid in (True, 0, -1, 1.5):
            with self.subTest(max_book_levels_per_side=invalid):
                with self.assertRaisesRegex(MarketDataError, "max_book_levels_per_side"):
                    MarketNormalizer(registry(), max_book_levels_per_side=invalid)

        normalizer = MarketNormalizer(registry(), max_book_levels_per_side=2)
        normalizer.normalize(
            raw(
                "BOOK_SNAPSHOT",
                {
                    "bids": [["99.99", "1"], ["99.98", "1"]],
                    "asks": [["100.01", "1"]],
                },
                sequence=10,
                stream="book",
            )
        )
        exceeded = normalizer.normalize(
            raw(
                "BOOK_DELTA",
                {"bids": [["99.97", "1"]], "asks": []},
                sequence=11,
                stream="book",
                available=at() + timedelta(seconds=1),
                ingested=at() + timedelta(seconds=2),
            )
        )
        self.assertIn("BOOK_RESOURCE_LIMIT", exceeded.quality_flags)
        self.assertIn("BOOK_UNUSABLE", exceeded.quality_flags)
        self.assertEqual(
            normalizer.book_state(
                provider_id="provider-a",
                venue_id="venue-a",
                provider_symbol="ABC-USD",
                stream="book",
            ),
            "GAPPED",
        )
        with self.assertRaisesRegex(MarketDataError, "new risk is blocked"):
            normalizer.require_executable_book(
                provider_id="provider-a",
                venue_id="venue-a",
                provider_symbol="ABC-USD",
                stream="book",
                as_of=at() + timedelta(seconds=2),
            )

    def test_invalid_new_book_payload_revokes_previous_ready_authority(self):
        normalizer = MarketNormalizer(registry(), max_book_levels_per_side=2)
        normalizer.normalize(
            raw(
                "BOOK_SNAPSHOT",
                {"bids": [["99.99", "1"]], "asks": [["100.01", "1"]]},
                sequence=10,
                stream="book",
            )
        )
        with self.assertRaisesRegex(MarketDataError, "book snapshot is crossed"):
            normalizer.normalize(
                raw(
                    "BOOK_SNAPSHOT",
                    {"bids": [["100.02", "1"]], "asks": [["100.01", "1"]]},
                    sequence=20,
                    stream="book",
                    available=at() + timedelta(seconds=1),
                    ingested=at() + timedelta(seconds=2),
                )
            )
        self.assertEqual(
            normalizer.book_state(
                provider_id="provider-a",
                venue_id="venue-a",
                provider_symbol="ABC-USD",
                stream="book",
            ),
            "GAPPED",
        )
        with self.assertRaisesRegex(MarketDataError, "book-depth resource envelope"):
            normalizer.normalize(
                raw(
                    "BOOK_SNAPSHOT",
                    {
                        "bids": [["99.99", "1"], ["99.98", "1"], ["99.97", "1"]],
                        "asks": [["100.01", "1"]],
                    },
                    sequence=30,
                    stream="book",
                    available=at() + timedelta(seconds=2),
                    ingested=at() + timedelta(seconds=3),
                )
            )
        self.assertEqual(
            normalizer.book_state(
                provider_id="provider-a",
                venue_id="venue-a",
                provider_symbol="ABC-USD",
                stream="book",
            ),
            "GAPPED",
        )

    def test_stream_generation_is_exact_and_part_of_contract_and_event_identity(self):
        first = MarketNormalizer(registry()).normalize(
            raw(
                "TRADE",
                {"price": "100", "quantity": "1"},
                sequence=7,
                stream="trades",
                generation=1,
            )
        )
        second = MarketNormalizer(registry()).normalize(
            raw(
                "TRADE",
                {"price": "100", "quantity": "1"},
                sequence=7,
                stream="trades",
                generation=2,
            )
        )
        self.assertEqual(first.stream_generation, 1)
        self.assertEqual(first.to_contract_dict()["stream_generation"], "1")
        self.assertNotEqual(first.event_id, second.event_id)
        with self.assertRaisesRegex(MarketDataError, "stream_generation"):
            raw(
                "TRADE",
                {"price": "100", "quantity": "1"},
                sequence=7,
                stream="trades",
                generation=True,
            )

    def test_book_generation_boundary_requires_new_snapshot_and_fences_old_frames(self):
        normalizer = MarketNormalizer(registry(), max_book_age=timedelta(seconds=5))
        normalizer.normalize(
            raw(
                "BOOK_SNAPSHOT",
                {"bids": [["99.99", "1"]], "asks": [["100.01", "1"]]},
                sequence=100,
                stream="book",
                generation=1,
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

        first_new_generation_frame = normalizer.normalize(
            raw(
                "BOOK_DELTA",
                {"bids": [["99.98", "2"]], "asks": []},
                sequence=1,
                stream="book",
                generation=2,
            )
        )
        self.assertIn("BOOK_UNUSABLE", first_new_generation_frame.quality_flags)
        self.assertEqual(
            normalizer.book_state(
                provider_id="provider-a",
                venue_id="venue-a",
                provider_symbol="ABC-USD",
                stream="book",
            ),
            "GAPPED",
        )

        new_snapshot = normalizer.normalize(
            raw(
                "BOOK_SNAPSHOT",
                {"bids": [["100.00", "3"]], "asks": [["100.02", "4"]]},
                sequence=2,
                stream="book",
                generation=2,
            )
        )
        self.assertNotIn("BOOK_UNUSABLE", new_snapshot.quality_flags)
        expected = normalizer.executable_book(
            as_of=at() + timedelta(seconds=1),
            provider_id="provider-a",
            venue_id="venue-a",
            provider_symbol="ABC-USD",
            stream="book",
        )
        self.assertEqual(expected["bids"][0], {"price": "100", "quantity": "3"})
        self.assertEqual(expected["asks"][0], {"price": "100.02", "quantity": "4"})

        with self.assertRaisesRegex(
            MarketDataError,
            "superseded stream generation",
        ):
            normalizer.normalize(
                raw(
                    "BOOK_SNAPSHOT",
                    {"bids": [["1", "9"]], "asks": [["2", "9"]]},
                    sequence=101,
                    stream="book",
                    generation=1,
                )
            )
        with self.assertRaisesRegex(
            MarketDataError,
            "requires the active stream generation",
        ):
            normalizer.normalize(
                raw(
                    "BOOK_SNAPSHOT",
                    {"bids": [["1", "9"]], "asks": [["2", "9"]]},
                    sequence=102,
                    stream="book",
                )
            )

        self.assertEqual(
            normalizer.executable_book(
                as_of=at() + timedelta(seconds=1),
                provider_id="provider-a",
                venue_id="venue-a",
                provider_symbol="ABC-USD",
                stream="book",
            ),
            expected,
        )

    def test_scalar_sequence_chronology_resets_by_explicit_stream_generation(self):
        normalizer = MarketNormalizer(registry())
        normalizer.normalize(
            raw(
                "TRADE",
                {"price": "100", "quantity": "1"},
                sequence=100,
                stream="trades",
                generation=1,
            )
        )
        reset = normalizer.normalize(
            raw(
                "TRADE",
                {"price": "100", "quantity": "1"},
                sequence=1,
                source=at() + timedelta(seconds=1),
                stream="trades",
                generation=2,
                evidence={
                    **EVIDENCE,
                    "observed_at": "2026-09-24T16:00:01.100000Z",
                },
            )
        )
        self.assertNotIn("OUT_OF_ORDER", reset.quality_flags)
        self.assertNotIn("SEQUENCE_GAP", reset.quality_flags)


if __name__ == "__main__":
    unittest.main()
