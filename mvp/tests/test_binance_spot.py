from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import unittest
from uuid import uuid4

from mvp.autotrade_mvp.binance_spot import (
    BinanceSpotAdapterError,
    BinanceSpotDepthContinuityPolicy,
    BinanceSpotDepthBootstrapBuffer,
    BinanceSpotDepthCursor,
    BinanceSpotDepthRange,
    BINANCE_SPOT_DEPTH_POLICY_ID,
    binance_spot_depth_stream_policy,
    begin_binance_spot_depth_generation,
    register_binance_spot_depth_snapshot,
    apply_binance_spot_depth_event,
    BinanceSpotOrderIntent,
    BinanceSpotReferencePrice,
    BinanceSpotSymbolRules,
    coverage_evidence,
    parse_account_trades,
    parse_order_ack,
    prepare_order_request,
)
from mvp.autotrade_mvp.market_data import (
    MarketDataError,
    MarketNormalizer,
    RawMarketUpdate,
    _issue_qualified_book_range_admission,
)
from mvp.autotrade_mvp.instruments import InstrumentRegistry, InstrumentVersion
from mvp.autotrade_mvp.provider_core import (
    Surface,
    observe_authenticated_json_response,
    prepare_authenticated_read_query,
)
from mvp.autotrade_mvp.accounting import ScopedEconomicBook
from mvp.autotrade_mvp.fill_accounting import (
    ProjectedFillEvidence,
    build_provider_fill_financial_plan,
)
from mvp.autotrade_mvp.reservations import ReservationSnapshot
from mvp.autotrade_mvp.capabilities import (
    CapabilityClaim,
    EvidenceVerification,
    derive_capability_snapshot,
)
from mvp.tests.capability_test_support import fresh_test_admission


NOW = datetime(2026, 9, 24, 20, tzinfo=timezone.utc)
DEPTH_EVIDENCE = {
    "artifact_id": "33333333-3333-4333-8333-333333333333",
    "sha256": "sha256:" + "b" * 64,
    "observed_at": "2026-09-24T20:00:00.100000Z",
}


def depth_registry():
    registry = InstrumentRegistry()
    registry.add(
        InstrumentVersion(
            instrument_id="44444444-4444-4444-8444-444444444444",
            version=1,
            provider_id="BINANCE",
            venue_id="SPOT",
            provider_symbol="BTCUSDT",
            asset_class="CRYPTO_SPOT",
            base_currency="BTC",
            quote_currency="USDT",
            settlement_currency="USDT",
            quantity_unit="BTC",
            contract_multiplier=Decimal("1"),
            price_tick=Decimal("0.01"),
            quantity_step=Decimal("0.001"),
            minimum_quantity=Decimal("0.001"),
            maximum_quantity=Decimal("1000"),
            calendar_id="CONTINUOUS_24_7",
            timezone_id="UTC",
            effective_from=NOW - timedelta(days=1),
            status="ACTIVE",
        )
    )
    return registry


def depth_raw(
    kind,
    payload,
    *,
    sequence,
    available_ms=100,
    ingested_ms=200,
    generation=None,
):
    return RawMarketUpdate(
        provider_id="BINANCE",
        venue_id="SPOT",
        provider_symbol="BTCUSDT",
        kind=kind,
        source_event_at=NOW,
        available_at=NOW + timedelta(milliseconds=available_ms),
        ingested_at=NOW + timedelta(milliseconds=ingested_ms),
        availability_basis="PROVIDER_TIMESTAMP",
        source_sequence=sequence,
        stream_generation=generation,
        sequence_stream="book",
        revision=0,
        payload=payload,
        raw_evidence_ref=DEPTH_EVIDENCE,
    )


def execution_observation(
    rows,
    *,
    account_id="paper-1",
    environment="PAPER",
    instrument_version="BTCUSDT:v1",
    surface=Surface.ACTIVITIES,
):
    query = prepare_authenticated_read_query(
        capability=capability(
            account_id=account_id,
            environment=environment,
            instrument_version=instrument_version,
        ),
        surface=surface,
        endpoint="/api/v3/myTrades",
        query={"symbol": "BTCUSDT"},
        at=NOW,
        permission_scope="TRADE.READ",
    )
    raw = json.dumps(
        rows,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return observe_authenticated_json_response(
        query_binding=query,
        http_status=200,
        response_bytes=raw,
        observed_at=NOW,
    )

def capability(
    *,
    order_types=("LIMIT", "MARKET"),
    tif=("GTC", "IOC", "FOK", "NONE"),
    account_id="account-1",
    environment="PAPER",
    instrument_version="BTCUSDT:v1",
):
    observed_at = NOW - timedelta(hours=1)
    claims = tuple(
        CapabilityClaim(
            source=source,
            provider_id="BINANCE",
            account_id=account_id,
            entity_id="global",
            environment=environment,
            instrument_version=instrument_version,
            observed_at=observed_at,
            expires_at=NOW + timedelta(hours=1),
            supported_order_types=frozenset(order_types),
            time_in_force=frozenset(tif),
            permission_scopes=frozenset({"ORDER_WRITE", "ORDER.READ", "TRADE.READ"}),
            position_mode="NET",
            native_protection=frozenset(),
            rate_limit_policy_id="binance-spot-foundation",
            data_entitlements=frozenset({"ORDERS", "TRADES"}),
            evidence_ref={
                "artifact_id": str(uuid4()),
                "sha256": "sha256:" + "b" * 64,
                "observed_at": observed_at.isoformat().replace("+00:00", "Z"),
                "source_uri": "https://developers.binance.com/en/docs/products/spot",
            },
        )
        for source in ("DOCUMENTED", "API", "ACCOUNT", "INSTRUMENT")
    )
    return fresh_test_admission(derive_capability_snapshot(
        snapshot_id=str(uuid4()),
        claims=claims,
        observed_at=observed_at,
        evidence_verifier=lambda _claim: EvidenceVerification(valid=True),
    ))


def symbol_rules(
    *,
    instrument_version="BTCUSDT:v1",
    symbol="BTCUSDT",
    min_qty="0.001",
    max_qty="100",
    step_size="0.001",
    min_price="0.01",
    max_price="1000000",
    tick_size="0.01",
    min_notional="5",
    apply_to_market=True,
    avg_price_mins=5,
):
    return BinanceSpotSymbolRules.from_exchange_info(
        instrument_version=instrument_version,
        symbol_payload={
            "symbol": symbol,
            "filters": [
                {
                    "filterType": "PRICE_FILTER",
                    "minPrice": min_price,
                    "maxPrice": max_price,
                    "tickSize": tick_size,
                },
                {
                    "filterType": "LOT_SIZE",
                    "minQty": min_qty,
                    "maxQty": max_qty,
                    "stepSize": step_size,
                },
                {
                    "filterType": "MARKET_LOT_SIZE",
                    "minQty": min_qty,
                    "maxQty": max_qty,
                    "stepSize": step_size,
                },
                {
                    "filterType": "MIN_NOTIONAL",
                    "minNotional": min_notional,
                    "applyToMarket": apply_to_market,
                    "avgPriceMins": avg_price_mins,
                },
            ],
        },
    )


def reference_price(
    *,
    instrument_version="BTCUSDT:v1",
    symbol="BTCUSDT",
    price="40000",
    observed_at=NOW - timedelta(seconds=1),
    avg_price_mins=5,
):
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    delta = observed_at - epoch
    timestamp_ms = (
        delta.days * 86_400_000
        + delta.seconds * 1_000
        + delta.microseconds // 1_000
    )
    if avg_price_mins == 0:
        return BinanceSpotReferencePrice.from_last_trade_payload(
            instrument_version=instrument_version,
            symbol=symbol,
            payload={
                "price": price,
                "time": timestamp_ms,
            },
        )
    return BinanceSpotReferencePrice.from_average_price_payload(
        instrument_version=instrument_version,
        symbol=symbol,
        payload={
            "mins": avg_price_mins,
            "price": price,
            "closeTime": timestamp_ms,
        },
    )


def provider_reference(
    *,
    instrument_version="BTCUSDT:v1",
    symbol="BTCUSDT",
    price="40000",
    observed_at=NOW - timedelta(seconds=1),
):
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    delta = observed_at - epoch
    timestamp_ms = (
        delta.days * 86_400_000
        + delta.seconds * 1_000
        + delta.microseconds // 1_000
    )
    return BinanceSpotReferencePrice.from_reference_price_payload(
        instrument_version=instrument_version,
        symbol=symbol,
        payload={
            "symbol": symbol,
            "referencePrice": price,
            "timestamp": timestamp_ms,
        },
    )


class BinanceSpotDepthContinuityTests(unittest.TestCase):
    def depth_event(self, *, first=101, final=105, symbol="BTCUSDT"):
        return BinanceSpotDepthRange.from_diff_depth_payload(
            {
                "e": "depthUpdate",
                "E": 1672515782136,
                "s": symbol,
                "U": first,
                "u": final,
                "b": [["100", "1"]],
                "a": [["101", "2"]],
            }
        )

    def test_diff_depth_range_parser_preserves_exact_provider_range(self):
        event = self.depth_event()
        self.assertEqual(event.symbol, "BTCUSDT")
        self.assertEqual(event.first_update_id, 101)
        self.assertEqual(event.final_update_id, 105)

    def test_depth_range_parser_rejects_coercion_and_reversed_ranges(self):
        class IntSubclass(int):
            pass

        invalid = (
            {"e": "depthUpdate", "s": "BTCUSDT", "U": True, "u": 105},
            {"e": "depthUpdate", "s": "BTCUSDT", "U": IntSubclass(101), "u": 105},
            {"e": "depthUpdate", "s": "btcusdt", "U": 101, "u": 105},
            {"e": "depthUpdate", "s": "BTCUSDT", "U": 106, "u": 105},
        )
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(
                BinanceSpotAdapterError
            ):
                BinanceSpotDepthRange.from_diff_depth_payload(payload)

    def test_snapshot_cursor_binds_symbol_and_exact_update_id(self):
        cursor = BinanceSpotDepthCursor.from_snapshot(
            symbol="BTCUSDT",
            payload={"lastUpdateId": 100, "bids": [], "asks": []},
        )
        self.assertEqual((cursor.symbol, cursor.update_id), ("BTCUSDT", 100))
        for value in (True, -1, "100"):
            with self.subTest(value=value), self.assertRaises(
                BinanceSpotAdapterError
            ):
                BinanceSpotDepthCursor.from_snapshot(
                    symbol="BTCUSDT",
                    payload={"lastUpdateId": value},
                )

    def test_bootstrap_discards_old_accepts_covering_range_and_detects_gap(self):
        snapshot = BinanceSpotDepthCursor.from_snapshot(
            symbol="BTCUSDT",
            payload={"lastUpdateId": 100},
        )
        old = self.depth_event(first=95, final=100)
        covering = self.depth_event(first=101, final=105)
        gap = self.depth_event(first=102, final=105)
        self.assertEqual(
            BinanceSpotDepthContinuityPolicy.bootstrap(
                snapshot=snapshot,
                event=old,
            ).disposition,
            "DISCARD",
        )
        accepted = BinanceSpotDepthContinuityPolicy.bootstrap(
            snapshot=snapshot,
            event=covering,
        )
        self.assertEqual(accepted.disposition, "APPLY")
        self.assertEqual(accepted.next_update_id, 105)
        self.assertEqual(
            BinanceSpotDepthContinuityPolicy.bootstrap(
                snapshot=snapshot,
                event=gap,
            ).disposition,
            "GAP",
        )

    def test_subsequent_ranges_allow_overlap_and_advance_to_final_id(self):
        local = BinanceSpotDepthCursor(symbol="BTCUSDT", update_id=105)
        contiguous = self.depth_event(first=106, final=110)
        first = BinanceSpotDepthContinuityPolicy.advance(
            local=local,
            event=contiguous,
        )
        self.assertEqual((first.disposition, first.next_update_id), ("APPLY", 110))
        second = BinanceSpotDepthContinuityPolicy.advance(
            local=BinanceSpotDepthCursor(symbol="BTCUSDT", update_id=110),
            event=self.depth_event(first=108, final=112),
        )
        self.assertEqual((second.disposition, second.next_update_id), ("APPLY", 112))
        self.assertEqual(
            BinanceSpotDepthContinuityPolicy.advance(
                local=local,
                event=self.depth_event(first=100, final=104),
            ).disposition,
            "DISCARD",
        )
        self.assertEqual(
            BinanceSpotDepthContinuityPolicy.advance(
                local=local,
                event=self.depth_event(first=107, final=110),
            ).disposition,
            "GAP",
        )

    def test_cross_symbol_depth_range_cannot_use_foreign_cursor(self):
        snapshot = BinanceSpotDepthCursor.from_snapshot(
            symbol="BTCUSDT",
            payload={"lastUpdateId": 100},
        )
        with self.assertRaisesRegex(
            BinanceSpotAdapterError,
            "does not match local depth cursor",
        ):
            BinanceSpotDepthContinuityPolicy.bootstrap(
                snapshot=snapshot,
                event=self.depth_event(symbol="ETHUSDT"),
            )


    def test_bootstrap_buffer_replays_old_then_covering_range_after_snapshot(self):
        normalizer = MarketNormalizer(
            depth_registry(),
            book_stream_policies=(
                binance_spot_depth_stream_policy(
                    provider_id="BINANCE",
                    venue_id="SPOT",
                ),
            ),
        )
        buffer = begin_binance_spot_depth_generation(
            normalizer,
            provider_id="BINANCE",
            venue_id="SPOT",
            provider_symbol="BTCUSDT",
            generation=7,
        )

        old_range = self.depth_event(first=95, final=100)
        old_event = normalizer.normalize(
            depth_raw(
                "BOOK_DELTA",
                {
                    "bids": [["99.98", "1"]],
                    "asks": [],
                    "first_sequence": 95,
                    "last_sequence": 100,
                },
                sequence=100,
            
                generation=7,
            )
        )
        buffer.buffer(old_event, old_range, generation=7)

        covering_range = self.depth_event(first=101, final=105)
        covering_event = normalizer.normalize(
            depth_raw(
                "BOOK_DELTA",
                {
                    "bids": [["100.00", "2"]],
                    "asks": [],
                    "first_sequence": 101,
                    "last_sequence": 105,
                },
                sequence=105,
                available_ms=300,
                ingested_ms=400,
            
                generation=7,
            )
        )
        buffer.buffer(covering_event, covering_range, generation=7)

        snapshot_payload = {
            "lastUpdateId": 100,
            "bids": [["99.99", "1"]],
            "asks": [["100.01", "1"]],
        }
        snapshot_event = normalizer.normalize(
            depth_raw(
                "BOOK_SNAPSHOT",
                {
                    "bids": snapshot_payload["bids"],
                    "asks": snapshot_payload["asks"],
                },
                sequence=100,
                available_ms=500,
                ingested_ms=600,
            
                generation=7,
            )
        )
        decisions = buffer.install_snapshot_and_replay(
            normalizer,
            snapshot_event,
            BinanceSpotDepthCursor.from_snapshot(
                symbol="BTCUSDT",
                payload=snapshot_payload,
            ),
            generation=7,
            provider_id="BINANCE",
            venue_id="SPOT",
            provider_symbol="BTCUSDT",
        )

        self.assertEqual(
            [(item.disposition, item.next_update_id) for item in decisions],
            [("DISCARD", None), ("APPLY", 105)],
        )
        self.assertEqual(buffer.buffered_count, 0)
        self.assertEqual(
            normalizer.provider_book_cursor(
                provider_id="BINANCE",
                venue_id="SPOT",
                provider_symbol="BTCUSDT",
            ),
            105,
        )
        with self.assertRaisesRegex(
            MarketDataError,
            "precedes the accepted book availability cut",
        ):
            normalizer.executable_book(
                as_of=NOW + timedelta(milliseconds=400),
                provider_id="BINANCE",
                venue_id="SPOT",
                provider_symbol="BTCUSDT",
            )
        view = normalizer.executable_book(
            as_of=NOW + timedelta(milliseconds=500),
            provider_id="BINANCE",
            venue_id="SPOT",
            provider_symbol="BTCUSDT",
        )
        self.assertEqual(view["bids"][0], {"price": "100", "quantity": "2"})

    def test_bootstrap_buffer_is_bounded_idempotent_and_fails_before_overflow_mutation(self):
        normalizer = MarketNormalizer(
            depth_registry(),
            book_stream_policies=(
                binance_spot_depth_stream_policy(
                    provider_id="BINANCE",
                    venue_id="SPOT",
                ),
            ),
        )
        buffer = begin_binance_spot_depth_generation(
            normalizer,
            provider_id="BINANCE",
            venue_id="SPOT",
            provider_symbol="BTCUSDT",
            generation=3,
            max_events=1,
        )
        first_range = self.depth_event(first=101, final=105)
        first_event = normalizer.normalize(
            depth_raw(
                "BOOK_DELTA",
                {
                    "bids": [["100.00", "1"]],
                    "asks": [],
                    "first_sequence": 101,
                    "last_sequence": 105,
                },
                sequence=105,
            
                generation=3,
            )
        )
        buffer.buffer(first_event, first_range, generation=3)
        buffer.buffer(first_event, first_range, generation=3)
        self.assertEqual(buffer.buffered_count, 1)

        second_range = self.depth_event(first=106, final=110)
        second_event = normalizer.normalize(
            depth_raw(
                "BOOK_DELTA",
                {
                    "bids": [["100.00", "2"]],
                    "asks": [],
                    "first_sequence": 106,
                    "last_sequence": 110,
                },
                sequence=110,
                available_ms=300,
                ingested_ms=400,
            
                generation=3,
            )
        )
        with self.assertRaisesRegex(
            BinanceSpotAdapterError,
            "supported event envelope",
        ):
            buffer.buffer(second_event, second_range, generation=3)
        self.assertEqual(buffer.buffered_count, 1)

    def test_reconnect_invalidates_old_generation_and_discards_buffered_events(self):
        normalizer = MarketNormalizer(
            depth_registry(),
            book_stream_policies=(
                binance_spot_depth_stream_policy(
                    provider_id="BINANCE",
                    venue_id="SPOT",
                ),
            ),
        )
        old = begin_binance_spot_depth_generation(
            normalizer,
            provider_id="BINANCE",
            venue_id="SPOT",
            provider_symbol="BTCUSDT",
            generation=11,
        )
        depth_range = self.depth_event(first=101, final=105)
        event = normalizer.normalize(
            depth_raw(
                "BOOK_DELTA",
                {
                    "bids": [["100.00", "1"]],
                    "asks": [],
                    "first_sequence": 101,
                    "last_sequence": 105,
                },
                sequence=105,
            
                generation=11,
            )
        )
        old.buffer(event, depth_range, generation=11)
        new = old.reconnect(
            normalizer,
            generation=12,
            provider_id="BINANCE",
            venue_id="SPOT",
            provider_symbol="BTCUSDT",
        )
        self.assertEqual(new.generation, 12)
        self.assertEqual(new.buffered_count, 0)
        self.assertEqual(old.buffered_count, 0)
        with self.assertRaisesRegex(BinanceSpotAdapterError, "sealed"):
            old.buffer(event, depth_range, generation=11)
        with self.assertRaisesRegex(BinanceSpotAdapterError, "generation"):
            new.buffer(event, depth_range, generation=11)
        with self.assertRaisesRegex(BinanceSpotAdapterError, "strictly increase"):
            new.reconnect(
                normalizer,
                generation=12,
                provider_id="BINANCE",
                venue_id="SPOT",
                provider_symbol="BTCUSDT",
            )

    def test_bootstrap_buffer_gap_stops_replay_and_leaves_book_non_executable(self):
        normalizer = MarketNormalizer(
            depth_registry(),
            book_stream_policies=(
                binance_spot_depth_stream_policy(
                    provider_id="BINANCE",
                    venue_id="SPOT",
                ),
            ),
        )
        buffer = begin_binance_spot_depth_generation(
            normalizer,
            provider_id="BINANCE",
            venue_id="SPOT",
            provider_symbol="BTCUSDT",
            generation=20,
        )
        gap_range = self.depth_event(first=102, final=105)
        gap_event = normalizer.normalize(
            depth_raw(
                "BOOK_DELTA",
                {
                    "bids": [["100.00", "2"]],
                    "asks": [],
                    "first_sequence": 102,
                    "last_sequence": 105,
                },
                sequence=105,
            
                generation=20,
            )
        )
        buffer.buffer(gap_event, gap_range, generation=20)

        snapshot_payload = {
            "lastUpdateId": 100,
            "bids": [["99.99", "1"]],
            "asks": [["100.01", "1"]],
        }
        snapshot_event = normalizer.normalize(
            depth_raw(
                "BOOK_SNAPSHOT",
                {
                    "bids": snapshot_payload["bids"],
                    "asks": snapshot_payload["asks"],
                },
                sequence=100,
                available_ms=300,
                ingested_ms=400,
            
                generation=20,
            )
        )
        decisions = buffer.install_snapshot_and_replay(
            normalizer,
            snapshot_event,
            BinanceSpotDepthCursor.from_snapshot(
                symbol="BTCUSDT",
                payload=snapshot_payload,
            ),
            generation=20,
            provider_id="BINANCE",
            venue_id="SPOT",
            provider_symbol="BTCUSDT",
        )
        self.assertEqual(
            [(item.disposition, item.next_update_id) for item in decisions],
            [("GAP", None)],
        )
        self.assertEqual(
            normalizer.book_state(
                provider_id="BINANCE",
                venue_id="SPOT",
                provider_symbol="BTCUSDT",
            ),
            "GAPPED",
        )
        with self.assertRaisesRegex(MarketDataError, "new risk is blocked"):
            normalizer.require_executable_book(
                provider_id="BINANCE",
                venue_id="SPOT",
                provider_symbol="BTCUSDT",
                as_of=NOW + timedelta(seconds=1),
            )


    def test_bootstrap_replay_exception_revokes_partially_ready_book(self):
        normalizer = MarketNormalizer(
            depth_registry(),
            book_stream_policies=(
                binance_spot_depth_stream_policy(
                    provider_id="BINANCE",
                    venue_id="SPOT",
                    stream="book",
                ),
                binance_spot_depth_stream_policy(
                    provider_id="BINANCE",
                    venue_id="SPOT",
                    stream="shadow-book",
                ),
            ),
        )
        buffer = begin_binance_spot_depth_generation(
            normalizer,
            provider_id="BINANCE",
            venue_id="SPOT",
            provider_symbol="BTCUSDT",
            generation=25,
            stream="book",
        )
        normalizer.begin_provider_book_generation(
            provider_id="BINANCE",
            venue_id="SPOT",
            provider_symbol="BTCUSDT",
            generation=25,
            stream="shadow-book",
            policy_id=BINANCE_SPOT_DEPTH_POLICY_ID,
        )

        valid_range = self.depth_event(first=101, final=105)
        valid_event = normalizer.normalize(
            depth_raw(
                "BOOK_DELTA",
                {
                    "bids": [["100.00", "2"]],
                    "asks": [],
                    "first_sequence": 101,
                    "last_sequence": 105,
                },
                sequence=105,
                generation=25,
            )
        )
        buffer.buffer(valid_event, valid_range, generation=25)

        foreign_range = self.depth_event(first=106, final=110)
        foreign_event = normalizer.normalize(
            RawMarketUpdate(
                provider_id="BINANCE",
                venue_id="SPOT",
                provider_symbol="BTCUSDT",
                kind="BOOK_DELTA",
                source_event_at=NOW,
                available_at=NOW + timedelta(milliseconds=300),
                ingested_at=NOW + timedelta(milliseconds=400),
                availability_basis="PROVIDER_TIMESTAMP",
                source_sequence=110,
                stream_generation=25,
                sequence_stream="shadow-book",
                revision=0,
                payload={
                    "bids": [["100.00", "3"]],
                    "asks": [],
                    "first_sequence": 106,
                    "last_sequence": 110,
                },
                raw_evidence_ref=DEPTH_EVIDENCE,
            )
        )
        buffer.buffer(foreign_event, foreign_range, generation=25)

        snapshot_payload = {
            "lastUpdateId": 100,
            "bids": [["99.99", "1"]],
            "asks": [["100.01", "1"]],
        }
        snapshot_event = normalizer.normalize(
            depth_raw(
                "BOOK_SNAPSHOT",
                {
                    "bids": snapshot_payload["bids"],
                    "asks": snapshot_payload["asks"],
                },
                sequence=100,
                generation=25,
            )
        )
        with self.assertRaisesRegex(
            MarketDataError,
            "does not belong to this stream",
        ):
            buffer.install_snapshot_and_replay(
                normalizer,
                snapshot_event,
                BinanceSpotDepthCursor.from_snapshot(
                    symbol="BTCUSDT",
                    payload=snapshot_payload,
                ),
                generation=25,
                provider_id="BINANCE",
                venue_id="SPOT",
                provider_symbol="BTCUSDT",
                stream="book",
            )

        self.assertEqual(buffer.buffered_count, 0)
        self.assertEqual(
            normalizer.book_state(
                provider_id="BINANCE",
                venue_id="SPOT",
                provider_symbol="BTCUSDT",
                stream="book",
            ),
            "UNINITIALIZED",
        )
        with self.assertRaisesRegex(MarketDataError, "new risk is blocked"):
            normalizer.require_executable_book(
                provider_id="BINANCE",
                venue_id="SPOT",
                provider_symbol="BTCUSDT",
                stream="book",
                as_of=NOW + timedelta(seconds=1),
            )


    def test_new_generation_revokes_previously_ready_book_before_new_snapshot(self):
        normalizer = MarketNormalizer(
            depth_registry(),
            book_stream_policies=(
                binance_spot_depth_stream_policy(
                    provider_id="BINANCE",
                    venue_id="SPOT",
                ),
            ),
        )
        first = begin_binance_spot_depth_generation(
            normalizer,
            provider_id="BINANCE",
            venue_id="SPOT",
            provider_symbol="BTCUSDT",
            generation=30,
        )
        snapshot_payload = {
            "lastUpdateId": 100,
            "bids": [["99.99", "1"]],
            "asks": [["100.01", "1"]],
        }
        snapshot_event = normalizer.normalize(
            depth_raw(
                "BOOK_SNAPSHOT",
                {
                    "bids": snapshot_payload["bids"],
                    "asks": snapshot_payload["asks"],
                },
                sequence=100,
            
                generation=30,
            )
        )
        range_event = self.depth_event(first=101, final=105)
        delta_event = normalizer.normalize(
            depth_raw(
                "BOOK_DELTA",
                {
                    "bids": [["100.00", "2"]],
                    "asks": [],
                    "first_sequence": 101,
                    "last_sequence": 105,
                },
                sequence=105,
                available_ms=300,
                ingested_ms=400,
            
                generation=30,
            )
        )
        first.buffer(delta_event, range_event, generation=30)
        first.install_snapshot_and_replay(
            normalizer,
            snapshot_event,
            BinanceSpotDepthCursor.from_snapshot(
                symbol="BTCUSDT",
                payload=snapshot_payload,
            ),
            generation=30,
            provider_id="BINANCE",
            venue_id="SPOT",
            provider_symbol="BTCUSDT",
        )
        normalizer.require_executable_book(
            provider_id="BINANCE",
            venue_id="SPOT",
            provider_symbol="BTCUSDT",
            as_of=NOW + timedelta(seconds=1),
        )

        with self.assertRaises(BinanceSpotAdapterError):
            begin_binance_spot_depth_generation(
                normalizer,
                provider_id="BINANCE",
                venue_id="SPOT",
                provider_symbol="BTCUSDT",
                generation=True,
            )
        with self.assertRaisesRegex(
            BinanceSpotAdapterError,
            "max_events",
        ):
            begin_binance_spot_depth_generation(
                normalizer,
                provider_id="BINANCE",
                venue_id="SPOT",
                provider_symbol="BTCUSDT",
                generation=31,
                max_events=0,
            )
        normalizer.require_executable_book(
            provider_id="BINANCE",
            venue_id="SPOT",
            provider_symbol="BTCUSDT",
            as_of=NOW + timedelta(seconds=1),
        )

        second = begin_binance_spot_depth_generation(
            normalizer,
            provider_id="BINANCE",
            venue_id="SPOT",
            provider_symbol="BTCUSDT",
            generation=31,
        )
        self.assertEqual(second.generation, 31)
        self.assertEqual(
            normalizer.book_state(
                provider_id="BINANCE",
                venue_id="SPOT",
                provider_symbol="BTCUSDT",
            ),
            "UNINITIALIZED",
        )
        with self.assertRaisesRegex(MarketDataError, "new risk is blocked"):
            normalizer.require_executable_book(
                provider_id="BINANCE",
                venue_id="SPOT",
                provider_symbol="BTCUSDT",
                as_of=NOW + timedelta(seconds=1),
            )
        with self.assertRaisesRegex(MarketDataError, "cursor is unavailable"):
            normalizer.provider_book_cursor(
                provider_id="BINANCE",
                venue_id="SPOT",
                provider_symbol="BTCUSDT",
            )


class BinanceSpotFoundationTests(unittest.TestCase):
    def depth_event(self, *, first=101, final=105, symbol="BTCUSDT"):
        return BinanceSpotDepthRange.from_diff_depth_payload(
            {
                "e": "depthUpdate",
                "E": 1672515782136,
                "s": symbol,
                "U": first,
                "u": final,
                "b": [["100", "1"]],
                "a": [["101", "2"]],
            }
        )

    def test_limit_request_preserves_exact_strings_and_requests_ack_only(self):
        intent = BinanceSpotOrderIntent.create(
            instrument_version="BTCUSDT:v1",
            symbol="BTCUSDT",
            side="BUY",
            order_type="LIMIT",
            quantity="0.0100",
            price="40000.2500",
            time_in_force="GTC",
        )
        request = prepare_order_request(
            intent,
            client_order_id="at-order-1",
            capability=capability(),
            symbol_rules=symbol_rules(),
            at=NOW,
        )
        self.assertEqual(request.endpoint, "/api/v3/order")
        self.assertEqual(request.body["quantity"], "0.0100")
        self.assertEqual(request.body["price"], "40000.2500")
        self.assertEqual(request.body["newOrderRespType"], "ACK")
        self.assertRegex(request.filter_source_sha256, r"^sha256:[0-9a-f]{64}$")
        self.assertNotIn("timestamp", request.body)
        self.assertNotIn("signature", request.body)

    def test_market_uses_base_quantity_and_rejects_price_or_time_in_force(self):
        intent = BinanceSpotOrderIntent.create(
            instrument_version="BTCUSDT:v1",
            symbol="BTCUSDT",
            side="SELL",
            order_type="MARKET",
            quantity="0.5",
        )
        reference = reference_price()
        request = prepare_order_request(
            intent,
            client_order_id="at-market-1",
            capability=capability(),
            symbol_rules=symbol_rules(),
            at=NOW,
            reference_price_observation=provider_reference(price=None),
            market_reference=reference,
            maximum_market_reference_age_seconds=30,
        )
        self.assertEqual(request.body["quantity"], "0.5")
        self.assertNotIn("quoteOrderQty", request.body)
        self.assertNotIn("timeInForce", request.body)
        self.assertEqual(
            request.market_reference_source_sha256,
            reference.source_sha256,
        )
        self.assertEqual(request.market_reference_kind, "AVERAGE")
        self.assertEqual(request.market_reference_window_minutes, 5)
        with self.assertRaises(BinanceSpotAdapterError):
            BinanceSpotOrderIntent.create(
                instrument_version="BTCUSDT:v1",
                symbol="BTCUSDT",
                side="SELL",
                order_type="MARKET",
                quantity="0.5",
                price="40000",
            )

    def test_binary_float_economics_fail_closed(self):
        with self.assertRaises(BinanceSpotAdapterError):
            BinanceSpotOrderIntent.create(
                instrument_version="BTCUSDT:v1",
                symbol="BTCUSDT",
                side="BUY",
                order_type="MARKET",
                quantity=0.1,
            )

    def test_exact_capability_evidence_controls_admission(self):
        intent = BinanceSpotOrderIntent.create(
            instrument_version="BTCUSDT:v1",
            symbol="BTCUSDT",
            side="BUY",
            order_type="LIMIT",
            quantity="1",
            price="100",
        )
        with self.assertRaisesRegex(BinanceSpotAdapterError, "capability"):
            prepare_order_request(
                intent,
                client_order_id="at-order-2",
                capability=capability(order_types=("MARKET",)),
                symbol_rules=symbol_rules(),
                at=NOW,
            )

    def test_zero_price_tick_disables_tick_rule_without_disabling_bounds(self):
        rules = symbol_rules(
            min_price="1",
            max_price="100",
            tick_size="0",
            min_notional="0.001",
        )
        intent = BinanceSpotOrderIntent.create(
            instrument_version="BTCUSDT:v1",
            symbol="BTCUSDT",
            side="BUY",
            order_type="LIMIT",
            quantity="0.001",
            price="1.23456789",
            time_in_force="GTC",
        )
        request = prepare_order_request(
            intent,
            client_order_id="at-filter-disabled-tick",
            capability=capability(),
            symbol_rules=rules,
            at=NOW,
        )
        self.assertEqual(request.body["price"], "1.23456789")

    def test_exchange_info_filters_reject_invalid_limit_before_send(self):
        intent = BinanceSpotOrderIntent.create(
            instrument_version="BTCUSDT:v1",
            symbol="BTCUSDT",
            side="BUY",
            order_type="LIMIT",
            quantity="0.0105",
            price="40000.255",
            time_in_force="GTC",
        )
        with self.assertRaisesRegex(BinanceSpotAdapterError, "quantity"):
            prepare_order_request(
                intent,
                client_order_id="at-filter-1",
                capability=capability(),
                symbol_rules=symbol_rules(),
                at=NOW,
            )

    def test_market_notional_filter_requires_causal_reference_price(self):
        intent = BinanceSpotOrderIntent.create(
            instrument_version="BTCUSDT:v1",
            symbol="BTCUSDT",
            side="BUY",
            order_type="MARKET",
            quantity="0.001",
        )
        with self.assertRaisesRegex(
            BinanceSpotAdapterError,
            "provider reference-price observation evidence",
        ):
            prepare_order_request(
                intent,
                client_order_id="at-filter-2",
                capability=capability(),
                symbol_rules=symbol_rules(min_notional="50"),
                at=NOW,
            )
        with self.assertRaisesRegex(BinanceSpotAdapterError, "notional"):
            prepare_order_request(
                intent,
                client_order_id="at-filter-3",
                capability=capability(),
                symbol_rules=symbol_rules(min_notional="50"),
                at=NOW,
                reference_price_observation=provider_reference(price=None),
                market_reference=reference_price(price="40000"),
                maximum_market_reference_age_seconds=30,
            )

    def test_market_reference_must_be_provider_evidence_and_fresh(self):
        intent = BinanceSpotOrderIntent.create(
            instrument_version="BTCUSDT:v1",
            symbol="BTCUSDT",
            side="BUY",
            order_type="MARKET",
            quantity="0.01",
        )
        rules = symbol_rules(min_notional="5")

        with self.assertRaisesRegex(
            BinanceSpotAdapterError,
            "provider reference-price evidence",
        ):
            rules.validate(
                intent,
                at=NOW,
                reference_price_observation=provider_reference(price=None),
                market_reference="40000",
                maximum_market_reference_age_seconds=30,
            )

        with self.assertRaisesRegex(BinanceSpotAdapterError, "stale"):
            prepare_order_request(
                intent,
                client_order_id="at-filter-stale-reference",
                capability=capability(),
                symbol_rules=rules,
                at=NOW,
                reference_price_observation=provider_reference(
                    price=None,
                    observed_at=NOW - timedelta(seconds=31),
                ),
                market_reference=reference_price(),
                maximum_market_reference_age_seconds=30,
            )

        with self.assertRaisesRegex(BinanceSpotAdapterError, "from the future"):
            prepare_order_request(
                intent,
                client_order_id="at-filter-future-reference",
                capability=capability(),
                symbol_rules=rules,
                at=NOW,
                reference_price_observation=provider_reference(
                    price=None,
                    observed_at=NOW + timedelta(seconds=1),
                ),
                market_reference=reference_price(),
                maximum_market_reference_age_seconds=30,
            )

    def test_market_reference_is_bound_to_exact_symbol_and_instrument(self):
        intent = BinanceSpotOrderIntent.create(
            instrument_version="BTCUSDT:v1",
            symbol="BTCUSDT",
            side="BUY",
            order_type="MARKET",
            quantity="0.01",
        )
        with self.assertRaisesRegex(BinanceSpotAdapterError, "instrument version"):
            prepare_order_request(
                intent,
                client_order_id="at-filter-reference-instrument",
                capability=capability(),
                symbol_rules=symbol_rules(),
                at=NOW,
                reference_price_observation=provider_reference(
                    instrument_version="BTCUSDT:v2",
                    price=None,
                ),
                market_reference=reference_price(),
                maximum_market_reference_age_seconds=30,
            )
        with self.assertRaisesRegex(BinanceSpotAdapterError, "symbol"):
            prepare_order_request(
                intent,
                client_order_id="at-filter-reference-symbol",
                capability=capability(),
                symbol_rules=symbol_rules(),
                at=NOW,
                reference_price_observation=provider_reference(
                    symbol="ETHUSDT",
                    price=None,
                ),
                market_reference=reference_price(),
                maximum_market_reference_age_seconds=30,
            )

    def test_market_notional_reference_window_must_match_exchange_info(self):
        intent = BinanceSpotOrderIntent.create(
            instrument_version="BTCUSDT:v1",
            symbol="BTCUSDT",
            side="BUY",
            order_type="MARKET",
            quantity="1",
        )
        rules = symbol_rules(min_notional="50", avg_price_mins=5)

        with self.assertRaisesRegex(BinanceSpotAdapterError, "averaging window"):
            prepare_order_request(
                intent,
                client_order_id="at-last-cannot-sub-average",
                capability=capability(),
                symbol_rules=rules,
                at=NOW,
                reference_price_observation=provider_reference(price=None),
                market_reference=reference_price(
                    price="60",
                    avg_price_mins=0,
                ),
                maximum_market_reference_age_seconds=30,
            )

        with self.assertRaisesRegex(BinanceSpotAdapterError, "notional"):
            prepare_order_request(
                intent,
                client_order_id="at-filter-required-window-average",
                capability=capability(),
                symbol_rules=rules,
                at=NOW,
                reference_price_observation=provider_reference(price=None),
                market_reference=reference_price(
                    price="40",
                    avg_price_mins=5,
                ),
                maximum_market_reference_age_seconds=30,
            )

        accepted = prepare_order_request(
            intent,
            client_order_id="at-filter-required-window-pass",
            capability=capability(),
            symbol_rules=rules,
            at=NOW,
            reference_price_observation=provider_reference(price=None),
            market_reference=reference_price(
                price="60",
                avg_price_mins=5,
            ),
            maximum_market_reference_age_seconds=30,
        )
        self.assertEqual(accepted.market_reference_kind, "AVERAGE")
        self.assertEqual(accepted.market_reference_window_minutes, 5)

    def test_zero_avg_price_mins_requires_last_price_evidence(self):
        intent = BinanceSpotOrderIntent.create(
            instrument_version="BTCUSDT:v1",
            symbol="BTCUSDT",
            side="BUY",
            order_type="MARKET",
            quantity="1",
        )
        rules = symbol_rules(min_notional="50", avg_price_mins=0)

        with self.assertRaisesRegex(BinanceSpotAdapterError, "averaging window"):
            prepare_order_request(
                intent,
                client_order_id="at-average-cannot-sub-last",
                capability=capability(),
                symbol_rules=rules,
                at=NOW,
                reference_price_observation=provider_reference(price=None),
                market_reference=reference_price(
                    price="60",
                    avg_price_mins=5,
                ),
                maximum_market_reference_age_seconds=30,
            )

        accepted = prepare_order_request(
            intent,
            client_order_id="at-filter-last-price-pass",
            capability=capability(),
            symbol_rules=rules,
            at=NOW,
            reference_price_observation=provider_reference(price=None),
            market_reference=reference_price(
                price="60",
                avg_price_mins=0,
            ),
            maximum_market_reference_age_seconds=30,
        )
        self.assertEqual(accepted.market_reference_kind, "LAST")
        self.assertEqual(accepted.market_reference_window_minutes, 0)

    def test_provider_reference_price_precedes_average_fallback(self):
        intent = BinanceSpotOrderIntent.create(
            instrument_version="BTCUSDT:v1",
            symbol="BTCUSDT",
            side="BUY",
            order_type="MARKET",
            quantity="1",
        )
        rules = symbol_rules(min_notional="50", avg_price_mins=5)

        with self.assertRaisesRegex(BinanceSpotAdapterError, "notional"):
            prepare_order_request(
                intent,
                client_order_id="at-provider-reference-wins-reject",
                capability=capability(),
                symbol_rules=rules,
                at=NOW,
                reference_price_observation=provider_reference(price="40"),
                market_reference=reference_price(
                    price="60",
                    avg_price_mins=5,
                ),
                maximum_market_reference_age_seconds=30,
            )

        provider = provider_reference(price="60")
        accepted = prepare_order_request(
            intent,
            client_order_id="at-provider-reference-wins-accept",
            capability=capability(),
            symbol_rules=rules,
            at=NOW,
            reference_price_observation=provider,
            market_reference=reference_price(
                price="40",
                avg_price_mins=5,
            ),
            maximum_market_reference_age_seconds=30,
        )
        self.assertEqual(accepted.market_reference_kind, "REFERENCE")
        self.assertIsNone(accepted.market_reference_window_minutes)
        self.assertEqual(
            accepted.reference_price_observation_source_sha256,
            provider.source_sha256,
        )
        self.assertEqual(
            accepted.market_reference_source_sha256,
            provider.source_sha256,
        )

    def test_null_provider_reference_permits_exact_avg_price_fallback_only(self):
        intent = BinanceSpotOrderIntent.create(
            instrument_version="BTCUSDT:v1",
            symbol="BTCUSDT",
            side="BUY",
            order_type="MARKET",
            quantity="1",
        )
        rules = symbol_rules(min_notional="50", avg_price_mins=5)
        provider = provider_reference(price=None)
        fallback = reference_price(price="60", avg_price_mins=5)

        accepted = prepare_order_request(
            intent,
            client_order_id="at-null-reference-fallback",
            capability=capability(),
            symbol_rules=rules,
            at=NOW,
            reference_price_observation=provider,
            market_reference=fallback,
            maximum_market_reference_age_seconds=30,
        )
        self.assertEqual(accepted.market_reference_kind, "AVERAGE")
        self.assertEqual(accepted.market_reference_window_minutes, 5)
        self.assertEqual(
            accepted.reference_price_observation_source_sha256,
            provider.source_sha256,
        )
        self.assertEqual(
            accepted.market_reference_source_sha256,
            fallback.source_sha256,
        )
        self.assertNotEqual(provider.source_sha256, fallback.source_sha256)

        with self.assertRaisesRegex(
            BinanceSpotAdapterError,
            "provider reference-price observation",
        ):
            prepare_order_request(
                intent,
                client_order_id="at-missing-reference-observation",
                capability=capability(),
                symbol_rules=rules,
                at=NOW,
                market_reference=fallback,
                maximum_market_reference_age_seconds=30,
            )

    def test_reference_price_endpoint_parser_binds_nullable_price_symbol_and_time(self):
        missing = provider_reference(price=None)
        self.assertEqual(missing.reference_kind, "REFERENCE")
        self.assertIsNone(missing.price)
        self.assertIsNone(missing.averaging_window_minutes)

        present = provider_reference(price="60000.25")
        self.assertEqual(present.price, Decimal("60000.25"))
        self.assertEqual(present.reference_kind, "REFERENCE")
        self.assertIsNone(present.averaging_window_minutes)

        epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
        timestamp_ms = int((NOW - epoch).total_seconds() * 1000)
        with self.assertRaisesRegex(
            BinanceSpotAdapterError,
            "does not match requested symbol",
        ):
            BinanceSpotReferencePrice.from_reference_price_payload(
                instrument_version="BTCUSDT:v1",
                symbol="BTCUSDT",
                payload={
                    "symbol": "ETHUSDT",
                    "referencePrice": "60000",
                    "timestamp": timestamp_ms,
                },
            )
        with self.assertRaisesRegex(
            BinanceSpotAdapterError,
            "fields are not canonical",
        ):
            BinanceSpotReferencePrice.from_reference_price_payload(
                instrument_version="BTCUSDT:v1",
                symbol="BTCUSDT",
                payload={
                    "symbol": "BTCUSDT",
                    "referencePrice": None,
                    "timestamp": timestamp_ms,
                    "unexpected": True,
                },
            )

    def test_exchange_info_avg_price_mins_is_strictly_validated(self):
        for invalid in (True, -1, "5"):
            with self.subTest(avg_price_mins=invalid):
                with self.assertRaisesRegex(
                    BinanceSpotAdapterError,
                    "avgPriceMins",
                ):
                    symbol_rules(avg_price_mins=invalid)

    def test_reference_price_cannot_be_forged_by_direct_construction(self):
        parsed = reference_price()
        with self.assertRaisesRegex(
            BinanceSpotAdapterError,
            "canonical provider payload parsing",
        ):
            BinanceSpotReferencePrice(
                instrument_version=parsed.instrument_version,
                symbol=parsed.symbol,
                price=parsed.price,
                observed_at=parsed.observed_at,
                source_sha256=parsed.source_sha256,
                reference_kind=parsed.reference_kind,
                averaging_window_minutes=parsed.averaging_window_minutes,
            )

    def test_exchange_info_rules_are_bound_to_exact_instrument(self):
        intent = BinanceSpotOrderIntent.create(
            instrument_version="BTCUSDT:v2",
            symbol="BTCUSDT",
            side="BUY",
            order_type="LIMIT",
            quantity="0.01",
            price="40000.25",
            time_in_force="GTC",
        )
        with self.assertRaisesRegex(BinanceSpotAdapterError, "instrument version"):
            prepare_order_request(
                intent,
                client_order_id="at-filter-4",
                capability=capability(instrument_version="BTCUSDT:v2"),
                symbol_rules=symbol_rules(instrument_version="BTCUSDT:v1"),
                at=NOW,
            )

    def test_exchange_info_market_flags_must_be_real_booleans(self):
        with self.assertRaisesRegex(BinanceSpotAdapterError, "boolean"):
            symbol_rules(apply_to_market="false")

    def test_exchange_info_rules_cannot_be_forged_by_direct_construction(self):
        parsed = symbol_rules()
        with self.assertRaisesRegex(
            BinanceSpotAdapterError,
            "canonical provider payload parsing",
        ):
            BinanceSpotSymbolRules(**parsed.__dict__)

    def test_depth_policy_admission_binds_exact_range_cursor_and_event(self):
        cursor = BinanceSpotDepthCursor(symbol="BTCUSDT", update_id=100)
        event = BinanceSpotDepthRange.from_diff_depth_payload(
            {
                "e": "depthUpdate",
                "s": "BTCUSDT",
                "U": 101,
                "u": 105,
            }
        )
        admission = BinanceSpotDepthContinuityPolicy.admission(
            event_id="11111111-1111-4111-8111-111111111111",
            cursor=cursor,
            event=event,
            bootstrap=True,
        )
        self.assertEqual(admission.disposition, "APPLY")
        self.assertEqual(admission.prior_sequence, 100)
        self.assertEqual(admission.first_sequence, 101)
        self.assertEqual(admission.last_sequence, 105)
        self.assertEqual(admission.next_sequence, 105)
        self.assertEqual(admission.policy_id, BINANCE_SPOT_DEPTH_POLICY_ID)

    def test_depth_policy_composes_bootstrap_and_apply_with_market_authority(self):
        normalizer = MarketNormalizer(
            depth_registry(),
            book_stream_policies=(
                binance_spot_depth_stream_policy(
                    provider_id="BINANCE",
                    venue_id="SPOT",
                ),
            ),
        )
        begin_binance_spot_depth_generation(
            normalizer,
            provider_id="BINANCE",
            venue_id="SPOT",
            provider_symbol="BTCUSDT",
            generation=40,
        )
        snapshot_payload = {
            "lastUpdateId": 100,
            "bids": [["99.99", "1"]],
            "asks": [["100.01", "1"]],
        }
        snapshot_cursor = BinanceSpotDepthCursor.from_snapshot(
            symbol="BTCUSDT",
            payload=snapshot_payload,
        )
        snapshot_event = normalizer.normalize(
            depth_raw(
                "BOOK_SNAPSHOT",
                {
                    "bids": snapshot_payload["bids"],
                    "asks": snapshot_payload["asks"],
                },
                sequence=100,
            
                generation=40,
            )
        )
        register_binance_spot_depth_snapshot(
            normalizer,
            snapshot_event,
            snapshot_cursor,
            provider_id="BINANCE",
            venue_id="SPOT",
            provider_symbol="BTCUSDT",
        )
        self.assertEqual(
            normalizer.book_state(
                provider_id="BINANCE",
                venue_id="SPOT",
                provider_symbol="BTCUSDT",
            ),
            "BOOTSTRAPPING",
        )

        provider_delta = {
            "e": "depthUpdate",
            "s": "BTCUSDT",
            "U": 101,
            "u": 105,
        }
        depth_range = BinanceSpotDepthRange.from_diff_depth_payload(provider_delta)
        delta_event = normalizer.normalize(
            depth_raw(
                "BOOK_DELTA",
                {
                    "bids": [["100.00", "2"]],
                    "asks": [],
                    "first_sequence": provider_delta["U"],
                    "last_sequence": provider_delta["u"],
                },
                sequence=provider_delta["u"],
                available_ms=300,
                ingested_ms=400,
            
                generation=40,
            )
        )
        decision = apply_binance_spot_depth_event(
            normalizer,
            delta_event,
            depth_range,
            provider_id="BINANCE",
            venue_id="SPOT",
            provider_symbol="BTCUSDT",
        )
        self.assertEqual(decision.disposition, "APPLY")
        self.assertEqual(decision.next_update_id, 105)
        self.assertEqual(
            normalizer.provider_book_cursor(
                provider_id="BINANCE",
                venue_id="SPOT",
                provider_symbol="BTCUSDT",
            ),
            105,
        )
        view = normalizer.executable_book(
            as_of=NOW + timedelta(seconds=1),
            provider_id="BINANCE",
            venue_id="SPOT",
            provider_symbol="BTCUSDT",
        )
        self.assertEqual(view["bids"][0], {"price": "100", "quantity": "2"})
        self.assertEqual(view["asks"][0], {"price": "100.01", "quantity": "1"})

    def test_depth_policy_exposes_explicit_coordinator_stream_binding(self):
        binding = binance_spot_depth_stream_policy(
            provider_id="BINANCE",
            venue_id="SPOT",
        )
        self.assertEqual(binding.provider_id, "BINANCE")
        self.assertEqual(binding.venue_id, "SPOT")
        self.assertEqual(binding.stream, "book")
        self.assertEqual(binding.policy_id, BINANCE_SPOT_DEPTH_POLICY_ID)

    def test_bootstrap_buffer_rejects_cross_generation_normalized_delta_and_snapshot(self):
        normalizer = MarketNormalizer(
            depth_registry(),
            book_stream_policies=(
                binance_spot_depth_stream_policy(
                    provider_id="BINANCE",
                    venue_id="SPOT",
                ),
            ),
        )
        begin_binance_spot_depth_generation(
            normalizer,
            provider_id="BINANCE",
            venue_id="SPOT",
            provider_symbol="BTCUSDT",
            generation=7,
        )
        with self.assertRaisesRegex(
            MarketDataError,
            "superseded stream generation",
        ):
            normalizer.normalize(
                depth_raw(
                    "BOOK_DELTA",
                    {
                        "bids": [["100.00", "2"]],
                        "asks": [],
                        "first_sequence": 101,
                        "last_sequence": 105,
                    },
                    sequence=105,
                    generation=6,
                )
            )
        with self.assertRaisesRegex(
            MarketDataError,
            "superseded stream generation",
        ):
            normalizer.normalize(
                depth_raw(
                    "BOOK_SNAPSHOT",
                    {"bids": [["99.99", "1"]], "asks": [["100.01", "1"]]},
                    sequence=100,
                    generation=6,
                )
            )


    def test_depth_sequence_identity_can_reuse_lower_cursor_after_reconnect_generation(self):
        normalizer = MarketNormalizer(
            depth_registry(),
            book_stream_policies=(
                binance_spot_depth_stream_policy(
                    provider_id="BINANCE",
                    venue_id="SPOT",
                ),
            ),
        )
        begin_binance_spot_depth_generation(
            normalizer,
            provider_id="BINANCE",
            venue_id="SPOT",
            provider_symbol="BTCUSDT",
            generation=1,
        )
        first = normalizer.normalize(
            depth_raw(
                "BOOK_DELTA",
                {
                    "bids": [["100.00", "1"]],
                    "asks": [],
                    "first_sequence": 101,
                    "last_sequence": 105,
                },
                sequence=105,
                generation=1,
            )
        )
        begin_binance_spot_depth_generation(
            normalizer,
            provider_id="BINANCE",
            venue_id="SPOT",
            provider_symbol="BTCUSDT",
            generation=2,
        )
        second = normalizer.normalize(
            depth_raw(
                "BOOK_DELTA",
                {
                    "bids": [["100.00", "1"]],
                    "asks": [],
                    "first_sequence": 1,
                    "last_sequence": 5,
                },
                sequence=5,
                available_ms=300,
                ingested_ms=400,
                generation=2,
            )
        )
        self.assertNotEqual(first.event_id, second.event_id)
        self.assertNotIn("OUT_OF_ORDER", second.quality_flags)
        self.assertNotIn("SEQUENCE_GAP", second.quality_flags)

    def test_ack_is_never_promoted_to_fill(self):
        result = parse_order_ack(
            attempt_id=str(uuid4()),
            client_order_id="at-ack-1",
            response={
                "symbol": "BTCUSDT",
                "orderId": 42,
                "orderListId": -1,
                "clientOrderId": "at-ack-1",
                "transactTime": 1790272800123,
            },
        )
        self.assertEqual(result["outcome"], "ACKNOWLEDGED")
        self.assertEqual(result["retry_disposition"], "NEVER")
        self.assertNotIn("fill", result)
        self.assertNotIn("executed_quantity", result)

    def test_ack_symbol_identity_must_be_canonical_uppercase(self):
        with self.assertRaisesRegex(BinanceSpotAdapterError, "canonical uppercase"):
            parse_order_ack(
                attempt_id=str(uuid4()),
                client_order_id="at-ack-lower-symbol",
                response={
                    "symbol": "btcusdt",
                    "orderId": 42,
                    "clientOrderId": "at-ack-lower-symbol",
                    "transactTime": 1790272800123,
                },
            )

    def test_account_trade_id_is_economic_identity_and_duplicates_are_idempotent(self):
        rows = [
            {
                "symbol": "BTCUSDT",
                "id": 7,
                "orderId": 42,
                "price": "100.25",
                "qty": "0.2",
                "commission": "0.001",
                "commissionAsset": "BNB",
                "isBuyer": True,
                "time": 1790272800123,
            }
        ]
        observation = execution_observation([rows[0], dict(rows[0])])
        fills = parse_account_trades(
            observation,
            instrument_versions={"BTCUSDT": "BTCUSDT:v1"},
            client_ids_by_order_id={42: "at-ack-1"},
        )
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0].provider_execution_id, "BINANCE-SPOT:BTCUSDT:7")
        self.assertEqual(fills[0].client_order_id, "at-ack-1")
        self.assertEqual(fills[0].account_id, "paper-1")
        self.assertEqual(fills[0].environment, "PAPER")
        self.assertEqual(fills[0].evidence_refs, (observation.evidence_ref,))
        self.assertEqual(fills[0].quantity, Decimal("0.2"))
        self.assertEqual(fills[0].fee_currency, "BNB")
        self.assertEqual(fills[0].side, "BUY")
        self.assertIsNone(fills[0].position_side)

    def test_account_trade_requires_provider_evidenced_direction(self):
        base = {
            "symbol": "BTCUSDT",
            "id": 8,
            "orderId": 43,
            "price": "101.25",
            "qty": "0.1",
            "commission": "0.001",
            "commissionAsset": "BNB",
            "time": 1790272801123,
        }
        for value in (None, "true", 1):
            with self.subTest(isBuyer=value):
                row = dict(base)
                if value is not None:
                    row["isBuyer"] = value
                with self.assertRaisesRegex(
                    BinanceSpotAdapterError,
                    "isBuyer must be provider-evidenced boolean",
                ):
                    parse_account_trades(
                        execution_observation([row]),
                        instrument_versions={"BTCUSDT": "BTCUSDT:v1"},
                    )

        sell = dict(base, isBuyer=False)
        fills = parse_account_trades(
            execution_observation([sell]),
            instrument_versions={"BTCUSDT": "BTCUSDT:v1"},
        )
        self.assertEqual(fills[0].side, "SELL")
        self.assertIsNone(fills[0].position_side)

    def test_client_order_identity_map_is_fully_validated_before_fill_mapping(self):
        row = {
            "symbol": "BTCUSDT",
            "id": 8,
            "orderId": 43,
            "price": "101.25",
            "qty": "0.1",
            "commission": "0.001",
            "commissionAsset": "BNB",
            "isBuyer": True,
            "time": 1790272801123,
        }
        observation = execution_observation([row])

        with self.assertRaisesRegex(BinanceSpotAdapterError, "must be a mapping"):
            parse_account_trades(
                observation,
                instrument_versions={"BTCUSDT": "BTCUSDT:v1"},
                client_ids_by_order_id=[],
            )

        for bad_map in (
            {"43": "at-spot-fill"},
            {True: "at-spot-fill"},
            {-1: "at-spot-fill"},
        ):
            with self.subTest(bad_map=bad_map), self.assertRaisesRegex(
                BinanceSpotAdapterError,
                "non-negative integer order ids",
            ):
                parse_account_trades(
                    observation,
                    instrument_versions={"BTCUSDT": "BTCUSDT:v1"},
                    client_ids_by_order_id=bad_map,
                )

        with self.assertRaisesRegex(BinanceSpotAdapterError, "client_order_id"):
            parse_account_trades(
                observation,
                instrument_versions={"BTCUSDT": "BTCUSDT:v1"},
                client_ids_by_order_id={99: "bad client id with spaces"},
            )

        with self.assertRaisesRegex(BinanceSpotAdapterError, "multiple provider order ids"):
            parse_account_trades(
                observation,
                instrument_versions={"BTCUSDT": "BTCUSDT:v1"},
                client_ids_by_order_id={
                    43: "at-spot-same-client",
                    44: "at-spot-same-client",
                },
            )

    def test_instrument_identity_map_is_fully_validated_before_fill_mapping(self):
        row = {
            "symbol": "BTCUSDT",
            "id": 8,
            "orderId": 43,
            "price": "101.25",
            "qty": "0.1",
            "commission": "0.001",
            "commissionAsset": "BNB",
            "isBuyer": True,
            "time": 1790272801123,
        }
        observation = execution_observation([row])

        with self.assertRaisesRegex(BinanceSpotAdapterError, "canonical uppercase"):
            parse_account_trades(
                observation,
                instrument_versions={
                    "BTCUSDT": "BTCUSDT:v1",
                    "ethusdt": "ETHUSDT:v1",
                },
            )

        with self.assertRaisesRegex(BinanceSpotAdapterError, "instrument_version"):
            parse_account_trades(
                observation,
                instrument_versions={
                    "BTCUSDT": "BTCUSDT:v1",
                    "ETHUSDT": "",
                },
            )

        with self.assertRaisesRegex(BinanceSpotAdapterError, "duplicate normalized"):
            parse_account_trades(
                observation,
                instrument_versions={
                    "BTCUSDT": "BTCUSDT:v1",
                    " BTCUSDT ": "BTCUSDT:v2",
                },
            )

    def test_spot_fill_remains_compatible_with_cash_equity_financial_plan(self):
        row = {
            "symbol": "BTCUSDT",
            "id": 9,
            "orderId": 44,
            "price": "101.25",
            "qty": "0.1",
            "commission": "0",
            "commissionAsset": "BNB",
            "isBuyer": True,
            "time": 1790272802123,
        }
        provider_fill = parse_account_trades(
            execution_observation([row]),
            instrument_versions={"BTCUSDT": "BTCUSDT:v1"},
        )[0]
        self.assertIsNone(provider_fill.position_side)
        projected = ProjectedFillEvidence.create(
            fill_id="spot-fill-9",
            provider_execution_id=provider_fill.provider_execution_id,
            intent_id="spot-intent-9",
            client_order_id=provider_fill.client_order_id,
            side=provider_fill.side,
            quantity=provider_fill.quantity,
            price=provider_fill.price,
        )
        reservation = ReservationSnapshot(
            reservation_id="spot-reservation-9",
            intent_id="spot-intent-9",
            original={"CASH:USDT": Decimal("20")},
            remaining={"CASH:USDT": Decimal("20")},
            consumed={"CASH:USDT": Decimal("0")},
            state="WORKING",
        )
        plan = build_provider_fill_financial_plan(
            book=ScopedEconomicBook(environment="PAPER", account_id="paper-1"),
            provider_id="BINANCE",
            projected_fill=projected,
            provider_fill=provider_fill,
            expected_instrument="BTCUSDT:v1",
            settlement_currency="USDT",
            reservation_snapshot=reservation,
        )
        self.assertEqual(plan.usage["CASH:USDT"], Decimal("10.125"))

    def test_execution_parser_rejects_wrong_authenticated_read_surface(self):
        row = {
            "symbol": "BTCUSDT",
            "id": 7,
            "orderId": 42,
            "price": "100.25",
            "qty": "0.2",
            "commission": "0.001",
            "commissionAsset": "BNB",
            "isBuyer": True,
            "time": 1790272800123,
        }
        wrong_surface = execution_observation(
            [row],
            surface=Surface.AUTHENTICATED_READ,
        )
        with self.assertRaisesRegex(BinanceSpotAdapterError, "scope"):
            parse_account_trades(
                wrong_surface,
                instrument_versions={"BTCUSDT": "BTCUSDT:v1"},
            )

    def test_conflicting_duplicate_trade_id_fails_closed(self):
        first = {
            "symbol": "BTCUSDT",
            "id": 7,
            "orderId": 42,
            "price": "100.25",
            "qty": "0.2",
            "commission": "0.001",
            "commissionAsset": "BNB",
            "isBuyer": True,
            "time": 1790272800123,
        }
        changed = dict(first, qty="0.3")
        with self.assertRaisesRegex(BinanceSpotAdapterError, "conflicting"):
            parse_account_trades(
                execution_observation([first, changed]),
                instrument_versions={"BTCUSDT": "BTCUSDT:v1"},
            )

    def test_absence_semantics_are_never_assumed_from_empty_surface(self):
        evidence = coverage_evidence(
            account_id="paper-1",
            environment="PAPER",
            surface="ORDER_HISTORY",
            coverage_start="2026-09-24T17:00:00Z",
            coverage_end="2026-09-24T19:00:00Z",
            pagination_complete=True,
            consistency_horizon_satisfied=True,
        )
        self.assertFalse(evidence.provider_semantics_exclude_execution)
        with self.assertRaisesRegex(
            BinanceSpotAdapterError,
            "cannot self-assert provider exclusion semantics",
        ):
            coverage_evidence(
                account_id="paper-1",
                environment="PAPER",
                surface="ORDER_HISTORY",
                coverage_start="2026-09-24T17:00:00Z",
                coverage_end="2026-09-24T19:00:00Z",
                pagination_complete=True,
                consistency_horizon_satisfied=True,
                qualified_exclusion_semantics=True,
            )



    def test_exact_diff_depth_replay_at_current_cursor_is_idempotent(self):
        normalizer = MarketNormalizer(
            depth_registry(),
            book_stream_policies=(
                binance_spot_depth_stream_policy(
                    provider_id="BINANCE",
                    venue_id="SPOT",
                ),
            ),
        )
        begin_binance_spot_depth_generation(
            normalizer,
            provider_id="BINANCE",
            venue_id="SPOT",
            provider_symbol="BTCUSDT",
            generation=70,
        )
        snapshot_event = normalizer.normalize(
            depth_raw(
                "BOOK_SNAPSHOT",
                {"bids": [["99.99", "1"]], "asks": [["100.01", "1"]]},
                sequence=100,
            
                generation=70,
            )
        )
        register_binance_spot_depth_snapshot(
            normalizer,
            snapshot_event,
            BinanceSpotDepthCursor(symbol="BTCUSDT", update_id=100),
            provider_id="BINANCE",
            venue_id="SPOT",
            provider_symbol="BTCUSDT",
        )
        payload = {
            "bids": [["100.00", "2"]],
            "asks": [],
            "first_sequence": 101,
            "last_sequence": 105,
        }
        depth_range = BinanceSpotDepthRange.from_diff_depth_payload(
            {"e": "depthUpdate", "s": "BTCUSDT", "U": 101, "u": 105}
        )
        first = normalizer.normalize(
            depth_raw(
                "BOOK_DELTA",
                payload,
                sequence=105,
                available_ms=300,
                ingested_ms=400,
            
                generation=70,
            )
        )
        apply_binance_spot_depth_event(
            normalizer,
            first,
            depth_range,
            provider_id="BINANCE",
            venue_id="SPOT",
            provider_symbol="BTCUSDT",
        )
        first_view = normalizer.executable_book(
            provider_id="BINANCE",
            venue_id="SPOT",
            provider_symbol="BTCUSDT",
            as_of=NOW + timedelta(seconds=1),
        )

        replay = normalizer.normalize(
            depth_raw(
                "BOOK_DELTA",
                payload,
                sequence=105,
                available_ms=300,
                ingested_ms=400,
            
                generation=70,
            )
        )
        self.assertIn("DUPLICATE", replay.quality_flags)
        replay_decision = apply_binance_spot_depth_event(
            normalizer,
            replay,
            depth_range,
            provider_id="BINANCE",
            venue_id="SPOT",
            provider_symbol="BTCUSDT",
        )
        self.assertEqual(replay_decision.disposition, "APPLY")
        self.assertEqual(
            normalizer.provider_book_cursor(
                provider_id="BINANCE",
                venue_id="SPOT",
                provider_symbol="BTCUSDT",
            ),
            105,
        )
        self.assertEqual(
            normalizer.book_state(
                provider_id="BINANCE",
                venue_id="SPOT",
                provider_symbol="BTCUSDT",
            ),
            "READY",
        )
        self.assertEqual(
            normalizer.executable_book(
                provider_id="BINANCE",
                venue_id="SPOT",
                provider_symbol="BTCUSDT",
                as_of=NOW + timedelta(seconds=1),
            ),
            first_view,
        )


    def test_stale_generation_malformed_frame_cannot_revoke_current_ready_book(self):
        normalizer = MarketNormalizer(
            depth_registry(),
            book_stream_policies=(
                binance_spot_depth_stream_policy(
                    provider_id="BINANCE",
                    venue_id="SPOT",
                ),
            ),
        )
        first = begin_binance_spot_depth_generation(
            normalizer,
            provider_id="BINANCE",
            venue_id="SPOT",
            provider_symbol="BTCUSDT",
            generation=80,
        )
        first.reconnect(
            normalizer,
            generation=81,
            provider_id="BINANCE",
            venue_id="SPOT",
            provider_symbol="BTCUSDT",
        )
        snapshot_event = normalizer.normalize(
            depth_raw(
                "BOOK_SNAPSHOT",
                {"bids": [["99.99", "1"]], "asks": [["100.01", "1"]]},
                sequence=100,
                generation=81,
                available_ms=500,
                ingested_ms=600,
            )
        )
        register_binance_spot_depth_snapshot(
            normalizer,
            snapshot_event,
            BinanceSpotDepthCursor(symbol="BTCUSDT", update_id=100),
            provider_id="BINANCE",
            venue_id="SPOT",
            provider_symbol="BTCUSDT",
        )
        delta_range = self.depth_event(first=101, final=105)
        delta_event = normalizer.normalize(
            depth_raw(
                "BOOK_DELTA",
                {
                    "bids": [["100.00", "2"]],
                    "asks": [],
                    "first_sequence": 101,
                    "last_sequence": 105,
                },
                sequence=105,
                generation=81,
                available_ms=700,
                ingested_ms=800,
            )
        )
        apply_binance_spot_depth_event(
            normalizer,
            delta_event,
            delta_range,
            provider_id="BINANCE",
            venue_id="SPOT",
            provider_symbol="BTCUSDT",
        )
        before = normalizer.executable_book(
            provider_id="BINANCE",
            venue_id="SPOT",
            provider_symbol="BTCUSDT",
            as_of=NOW + timedelta(seconds=1),
        )

        stale = depth_raw(
            "BOOK_DELTA",
            {
                "bids": [["100.02", "1"], ["bad-price", "1"]],
                "asks": [],
                "first_sequence": 106,
                "last_sequence": 110,
            },
            sequence=110,
            generation=80,
            available_ms=900,
            ingested_ms=950,
        )
        with self.assertRaisesRegex(
            MarketDataError,
            "superseded stream generation",
        ):
            normalizer.normalize(stale)

        self.assertEqual(
            normalizer.provider_book_generation(
                provider_id="BINANCE",
                venue_id="SPOT",
                provider_symbol="BTCUSDT",
            ),
            81,
        )
        self.assertEqual(
            normalizer.book_state(
                provider_id="BINANCE",
                venue_id="SPOT",
                provider_symbol="BTCUSDT",
            ),
            "READY",
        )
        self.assertEqual(
            normalizer.executable_book(
                provider_id="BINANCE",
                venue_id="SPOT",
                provider_symbol="BTCUSDT",
                as_of=NOW + timedelta(seconds=1),
            ),
            before,
        )


    def test_importable_forged_apply_cannot_override_binance_gap_policy(self):
        normalizer = MarketNormalizer(
            depth_registry(),
            book_stream_policies=(
                binance_spot_depth_stream_policy(
                    provider_id="BINANCE",
                    venue_id="SPOT",
                ),
            ),
        )
        begin_binance_spot_depth_generation(
            normalizer,
            provider_id="BINANCE",
            venue_id="SPOT",
            provider_symbol="BTCUSDT",
            generation=44,
        )
        snapshot_payload = {
            "lastUpdateId": 100,
            "bids": [["99.99", "1"]],
            "asks": [["100.01", "1"]],
        }
        snapshot_event = normalizer.normalize(
            depth_raw(
                "BOOK_SNAPSHOT",
                {
                    "bids": snapshot_payload["bids"],
                    "asks": snapshot_payload["asks"],
                },
                sequence=100,
                generation=44,
            )
        )
        register_binance_spot_depth_snapshot(
            normalizer,
            snapshot_event,
            BinanceSpotDepthCursor.from_snapshot(
                symbol="BTCUSDT",
                payload=snapshot_payload,
            ),
            provider_id="BINANCE",
            venue_id="SPOT",
            provider_symbol="BTCUSDT",
        )
        gap_range = self.depth_event(first=102, final=105)
        gap_event = normalizer.normalize(
            depth_raw(
                "BOOK_DELTA",
                {
                    "bids": [["100.00", "2"]],
                    "asks": [],
                    "first_sequence": 102,
                    "last_sequence": 105,
                },
                sequence=105,
                generation=44,
            )
        )
        forged_apply = _issue_qualified_book_range_admission(
            policy_id=BINANCE_SPOT_DEPTH_POLICY_ID,
            event_id=gap_event.event_id,
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
                gap_event,
                forged_apply,
                provider_id="BINANCE",
                venue_id="SPOT",
                provider_symbol="BTCUSDT",
            )
        self.assertEqual(
            normalizer.book_state(
                provider_id="BINANCE",
                venue_id="SPOT",
                provider_symbol="BTCUSDT",
            ),
            "BOOTSTRAPPING",
        )
        decision = apply_binance_spot_depth_event(
            normalizer,
            gap_event,
            gap_range,
            provider_id="BINANCE",
            venue_id="SPOT",
            provider_symbol="BTCUSDT",
        )
        self.assertEqual(
            (decision.disposition, decision.next_update_id),
            ("GAP", None),
        )
        self.assertEqual(
            normalizer.book_state(
                provider_id="BINANCE",
                venue_id="SPOT",
                provider_symbol="BTCUSDT",
            ),
            "GAPPED",
        )

    def test_qualified_range_preview_is_non_mutating_before_commit(self):
        normalizer = MarketNormalizer(
            depth_registry(),
            book_stream_policies=(
                binance_spot_depth_stream_policy(
                    provider_id="BINANCE",
                    venue_id="SPOT",
                ),
            ),
        )
        buffer = begin_binance_spot_depth_generation(
            normalizer,
            provider_id="BINANCE",
            venue_id="SPOT",
            provider_symbol="BTCUSDT",
            generation=21,
        )
        snapshot_payload = {
            "lastUpdateId": 100,
            "bids": [["99.99", "1"]],
            "asks": [["100.01", "1"]],
        }
        snapshot_event = normalizer.normalize(
            depth_raw(
                "BOOK_SNAPSHOT",
                {
                    "bids": snapshot_payload["bids"],
                    "asks": snapshot_payload["asks"],
                },
                sequence=100,
                generation=21,
            )
        )
        buffer.install_snapshot_and_replay(
            normalizer,
            snapshot_event,
            BinanceSpotDepthCursor.from_snapshot(
                symbol="BTCUSDT",
                payload=snapshot_payload,
            ),
            generation=21,
            provider_id="BINANCE",
            venue_id="SPOT",
            provider_symbol="BTCUSDT",
        )
        depth_range = self.depth_event(first=101, final=105)
        delta = normalizer.normalize(
            depth_raw(
                "BOOK_DELTA",
                {
                    "bids": [["100.00", "2"]],
                    "asks": [],
                    "first_sequence": 101,
                    "last_sequence": 105,
                },
                sequence=105,
                available_ms=300,
                ingested_ms=400,
                generation=21,
            )
        )
        admission = BinanceSpotDepthContinuityPolicy.admission(
            event_id=delta.event_id,
            cursor=BinanceSpotDepthCursor(symbol="BTCUSDT", update_id=100),
            event=depth_range,
            bootstrap=True,
        )
        candidate = normalizer.preview_qualified_book_range(
            delta,
            admission,
            provider_id="BINANCE",
            venue_id="SPOT",
            provider_symbol="BTCUSDT",
        )
        self.assertEqual(candidate["bids"][0], {"price": "100", "quantity": "2"})
        self.assertEqual(
            normalizer.provider_book_cursor(
                provider_id="BINANCE",
                venue_id="SPOT",
                provider_symbol="BTCUSDT",
            ),
            100,
        )
        self.assertEqual(
            normalizer.book_state(
                provider_id="BINANCE",
                venue_id="SPOT",
                provider_symbol="BTCUSDT",
            ),
            "BOOTSTRAPPING",
        )

if __name__ == "__main__":
    unittest.main()
