import hashlib
import unittest
from decimal import Decimal

from mvp.autotrade_mvp.reconciliation import reconcile_account

from mvp.autotrade_mvp.kraken_spot_stream import (
    KRAKEN_SPOT_EXECUTIONS_SUBSCRIPTION,
    KrakenSpotExecutionsSubscriptionBinding,
    KrakenSpotExecutionStreamRecovery,
    KrakenSpotStreamError,
    parse_execution_frame,
    parse_executions_subscription_ack,
)


def frame_bytes(
    *,
    frame_type="snapshot",
    sequence=1,
    reports=None,
    channel="executions",
):
    if reports is None:
        reports = [
            {
                "order_id": "O-1",
                "cl_ord_id": "client-1",
                "exec_type": "new",
                "order_status": "new",
            }
        ]
    normalized_reports = []
    for report in reports:
        normalized = dict(report)
        normalized.setdefault(
            "timestamp",
            "2026-10-04T05:00:00.123456Z",
        )
        if normalized.get("exec_type") == "trade":
            normalized.setdefault("symbol", "BTC/USD")
            normalized.setdefault("side", "buy")
            normalized.setdefault("last_qty", 1)
            normalized.setdefault("last_price", 25000)
            normalized.setdefault("cost", 25000)
            normalized.setdefault("trade_id", 1)
            normalized.setdefault("margin_borrow", False)
            normalized.setdefault(
                "fees",
                [{"asset": "USD", "qty": 1}],
            )
            normalized.setdefault(
                "timestamp",
                "2026-10-04T05:00:00.123456Z",
            )
        normalized_reports.append(normalized)
    import json

    return json.dumps(
        {
            "channel": channel,
            "type": frame_type,
            "data": normalized_reports,
            "sequence": sequence,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def ack_bytes(
    *,
    success=True,
    channel="executions",
    snap_orders=True,
    snap_trades=False,
    req_id=7,
):
    import json

    payload = {
        "method": "subscribe",
        "req_id": req_id,
        "success": success,
        "result": {
            "channel": channel,
            "snap_orders": snap_orders,
            "snap_trades": snap_trades,
        },
    }
    if not success:
        payload["error"] = "subscription rejected"
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def subscription_binding(
    *,
    account_id="spot-live-1",
    connection_generation=1,
    req_id=7,
):
    return KrakenSpotExecutionsSubscriptionBinding.create(
        account_id=account_id,
        connection_generation=connection_generation,
        req_id=req_id,
    )


class KrakenSpotExecutionFrameTests(unittest.TestCase):
    def provider_fills_from_admitted_frame(
        self,
        frame,
        *,
        instrument_versions,
        fee_currency_by_symbol=None,
    ):
        if frame.frame_type != "update" or frame.sequence < 1:
            raise AssertionError("test helper requires an update with positive sequence")
        recovery = KrakenSpotExecutionStreamRecovery(
            account_id=frame.account_id,
            environment=frame.environment,
        )
        generation = recovery.begin_connection()
        self.assertEqual(generation, frame.connection_generation)
        binding = KrakenSpotExecutionsSubscriptionBinding.create(
            account_id=frame.account_id,
            connection_generation=generation,
            req_id=7,
            environment=frame.environment,
        )
        recovery.apply_subscription_ack(
            parse_executions_subscription_ack(
                ack_bytes(req_id=7),
                subscription_binding=binding,
            )
        )
        recovery.apply_frame(
            parse_execution_frame(
                frame_bytes(
                    frame_type="snapshot",
                    sequence=frame.sequence - 1,
                    reports=[
                        {
                            "order_id": "O-ADMISSION-SNAPSHOT",
                            "exec_type": "new",
                            "order_status": "new",
                        }
                    ],
                ),
                account_id=frame.account_id,
                connection_generation=generation,
                environment=frame.environment,
            )
        )
        recovery.apply_frame(frame)
        return recovery.buffered_provider_fills(
            instrument_versions=instrument_versions,
            fee_currency_by_symbol=(
                {"BTC/USD": "USD"}
                if fee_currency_by_symbol is None
                else fee_currency_by_symbol
            ),
        )

    def test_authority_ingress_rejects_polymorphic_scalars_before_callbacks(self):
        touched: list[str] = []

        class HostileText(str):
            def strip(self, *args, **kwargs):
                touched.append("strip")
                raise AssertionError("hostile text normalization")

        class HostileInt(int):
            def __lt__(self, other):
                touched.append("lt")
                raise AssertionError("hostile integer comparison")

        with self.assertRaisesRegex(KrakenSpotStreamError, "account_id"):
            KrakenSpotExecutionsSubscriptionBinding.create(
                account_id=HostileText("spot-live-1"),
                connection_generation=1,
                req_id=7,
            )
        with self.assertRaisesRegex(KrakenSpotStreamError, "connection_generation"):
            KrakenSpotExecutionsSubscriptionBinding.create(
                account_id="spot-live-1",
                connection_generation=HostileInt(1),
                req_id=7,
            )
        self.assertEqual(touched, [])

    def test_fill_bridge_rejects_polymorphic_mapping_before_callbacks(self):
        touched: list[str] = []

        class HostileDict(dict):
            def items(self):
                touched.append("items")
                raise AssertionError("hostile mapping iteration")

            def get(self, *args, **kwargs):
                touched.append("get")
                raise AssertionError("hostile mapping lookup")

        frame = parse_execution_frame(
            frame_bytes(
                frame_type="update",
                sequence=43,
                reports=[
                    {
                        "order_id": "O-MAP-GUARD",
                        "cl_ord_id": "client-map-guard",
                        "exec_id": "E-MAP-GUARD",
                        "exec_type": "trade",
                        "order_status": "partially_filled",
                    }
                ],
            ),
            account_id="spot-live-1",
            connection_generation=1,
        )

        with self.assertRaisesRegex(TypeError, "instrument_versions must be an exact dict"):
            self.provider_fills_from_admitted_frame(
                frame,
                instrument_versions=HostileDict(
                    {"BTC/USD": "CRYPTO:BTC-USD:v1"}
                ),
                fee_currency_by_symbol={"BTC/USD": "USD"},
            )
        with self.assertRaisesRegex(TypeError, "fee_currency_by_symbol must be an exact dict"):
            self.provider_fills_from_admitted_frame(
                frame,
                instrument_versions={"BTC/USD": "CRYPTO:BTC-USD:v1"},
                fee_currency_by_symbol=HostileDict({"BTC/USD": "USD"}),
            )
        self.assertEqual(touched, [])

    def test_subscription_profile_requires_open_order_snapshot_without_trade_snapshot(self):
        self.assertEqual(
            dict(KRAKEN_SPOT_EXECUTIONS_SUBSCRIPTION),
            {
                "channel": "executions",
                "snap_orders": True,
                "snap_trades": False,
                "order_status": True,
            },
        )
        self.assertNotIn("token", KRAKEN_SPOT_EXECUTIONS_SUBSCRIPTION)

    def test_exact_frame_binds_scope_sequence_identity_and_raw_hash(self):
        raw = frame_bytes(
            frame_type="update",
            sequence=42,
            reports=[
                {
                    "order_id": "O-1",
                    "cl_ord_id": "client-1",
                    "exec_id": "E-1",
                    "exec_type": "trade",
                    "order_status": "partially_filled",
                }
            ],
        )
        frame = parse_execution_frame(
            raw,
            account_id="spot-live-1",
            connection_generation=1,
        )

        self.assertEqual(frame.account_id, "spot-live-1")
        self.assertEqual(frame.environment, "LIVE")
        self.assertEqual(frame.frame_type, "update")
        self.assertEqual(frame.sequence, 42)
        self.assertEqual(frame.response_bytes, raw)
        self.assertEqual(
            frame.evidence_ref,
            "provider-stream:sha256:" + hashlib.sha256(raw).hexdigest(),
        )
        self.assertEqual(len(frame.reports), 1)
        report = frame.reports[0]
        self.assertEqual(report.order_id, "O-1")
        self.assertEqual(report.client_order_id, "client-1")
        self.assertEqual(report.exec_id, "E-1")
        self.assertEqual(report.exec_type, "trade")
        self.assertEqual(report.order_status, "partially_filled")
        self.assertEqual(report.symbol, "BTC/USD")
        self.assertEqual(report.side, "buy")
        self.assertEqual(report.last_qty, Decimal("1"))
        self.assertEqual(report.last_price, Decimal("25000"))
        self.assertEqual(report.cost, Decimal("25000"))
        self.assertEqual(report.trade_id, 1)
        self.assertFalse(report.margin_borrow)
        self.assertEqual(len(report.fees), 1)
        self.assertTrue(report.fees_reported)
        self.assertTrue(report.trade_economics_complete)
        self.assertEqual(report.fees[0].asset, "USD")
        self.assertEqual(report.fees[0].quantity, Decimal("1"))
        self.assertEqual(report.event_time, "2026-10-04T05:00:00.123456Z")

    def test_trade_frame_retains_exact_decimal_economics_from_provider_bytes(self):
        raw = (
            b'{"channel":"executions","type":"update","data":['
            b'{"order_id":"O-EXACT","cl_ord_id":"client-exact",'
            b'"exec_id":"E-EXACT","exec_type":"trade",'
            b'"order_status":"partially_filled","symbol":"BTC/USD",'
            b'"side":"sell","last_qty":0.00000001,'
            b'"last_price":12345.67890123,"cost":0.0001234567890123,'
            b'"fees":[{"asset":"USD","qty":0.00000123}],'
            b'"timestamp":"2026-10-04T05:00:00.123456Z","trade_id":42,'
            b'"margin_borrow":false}],'
            b'"sequence":2}'
        )
        report = parse_execution_frame(
            raw,
            account_id="spot-live-1",
            connection_generation=1,
        ).reports[0]

        self.assertEqual(report.exec_id, "E-EXACT")
        self.assertEqual(report.symbol, "BTC/USD")
        self.assertEqual(report.side, "sell")
        self.assertEqual(report.last_qty, Decimal("0.00000001"))
        self.assertEqual(report.last_price, Decimal("12345.67890123"))
        self.assertEqual(report.cost, Decimal("0.0001234567890123"))
        self.assertEqual(report.trade_id, 42)
        self.assertFalse(report.margin_borrow)
        self.assertEqual(report.fees[0].asset, "USD")
        self.assertEqual(report.fees[0].quantity, Decimal("0.00000123"))
        self.assertEqual(report.event_time, "2026-10-04T05:00:00.123456Z")
        self.assertEqual(report.trade_id, 42)

    def test_trade_frame_preserves_incomplete_economics_for_reconciliation(self):
        raw = (
            b'{"channel":"executions","type":"update","data":['
            b'{"order_id":"O-INCOMPLETE","exec_id":"E-INCOMPLETE",'
            b'"exec_type":"trade","order_status":"partially_filled",'
            b'"symbol":"BTC/USD","side":"buy","last_qty":1,'
            b'"last_price":25000,"cost":25000,"trade_id":7,"margin_borrow":false,'
            b'"timestamp":"2026-10-04T05:00:00Z"}],'
            b'"sequence":3}'
        )
        frame = parse_execution_frame(
            raw,
            account_id="spot-live-1",
            connection_generation=1,
        )
        report = frame.reports[0]
        self.assertEqual(report.exec_id, "E-INCOMPLETE")
        self.assertEqual(report.fees, ())
        self.assertFalse(report.fees_reported)
        self.assertFalse(report.trade_economics_complete)
        self.assertEqual(frame.response_bytes, raw)

    def test_trade_frame_rejects_string_encoded_numeric_fields(self):
        raw = (
            b'{"channel":"executions","type":"update","data":['
            b'{"order_id":"O-STRING","exec_id":"E-STRING","exec_type":"trade",'
            b'"order_status":"partially_filled","symbol":"BTC/USD","side":"buy",'
            b'"last_qty":"1.0","last_price":25000,"cost":25000,"trade_id":8,'
            b'"margin_borrow":false,"fees":[{"asset":"USD","qty":1}],'
            b'"timestamp":"2026-10-04T05:00:00Z"}],"sequence":4}'
        )
        with self.assertRaisesRegex(
            KrakenSpotStreamError,
            "last_qty must be an exact JSON number",
        ):
            parse_execution_frame(
                raw,
                account_id="spot-live-1",
                connection_generation=1,
            )

    def test_trade_frame_marks_missing_conditional_fields_incomplete(self):
        cases = (
            (
                {
                    "order_id": "O-COST",
                    "exec_id": "E-COST",
                    "exec_type": "trade",
                    "order_status": "partially_filled",
                    "cost": None,
                },
                "cost",
            ),
        )
        for report_input, missing_field in cases:
            with self.subTest(missing_field=missing_field):
                report = parse_execution_frame(
                    frame_bytes(reports=[report_input]),
                    account_id="spot-live-1",
                    connection_generation=1,
                ).reports[0]
                self.assertIsNone(getattr(report, missing_field))
                self.assertFalse(report.trade_economics_complete)

    def test_trade_id_is_optional_for_canonical_provider_fill_bridge(self):
        frame = parse_execution_frame(
            frame_bytes(
                frame_type="update",
                sequence=6,
                reports=[
                    {
                        "order_id": "O-OPTIONAL-TRADE-ID",
                        "cl_ord_id": "client-optional-trade-id",
                        "exec_id": "E-OPTIONAL-TRADE-ID",
                        "exec_type": "trade",
                        "order_status": "partially_filled",
                        "trade_id": None,
                    }
                ],
            ),
            account_id="spot-live-1",
            connection_generation=1,
        )
        report = frame.reports[0]
        self.assertIsNone(report.trade_id)
        self.assertTrue(report.trade_economics_complete)

        fill = self.provider_fills_from_admitted_frame(
            frame,
            instrument_versions={"BTC/USD": "CRYPTO:BTC-USD:v1"},
            fee_currency_by_symbol={"BTC/USD": "USD"},
        )[0]
        self.assertEqual(fill.provider_execution_id, "E-OPTIONAL-TRADE-ID")
        self.assertEqual(fill.client_order_id, "client-optional-trade-id")
        self.assertEqual(fill.quantity, Decimal("1"))
        self.assertEqual(fill.price, Decimal("25000"))

    def test_trade_frame_rejects_non_boolean_margin_flag_when_present(self):
        raw = (
            b'{"channel":"executions","type":"update","data":[{"order_id":"O-MARGIN",'
            b'"exec_id":"E-MARGIN","exec_type":"trade","order_status":"partially_filled",'
            b'"symbol":"BTC/USD","side":"buy","last_qty":1,"last_price":25000,"cost":25000,'
            b'"fees":[{"asset":"USD","qty":1}],"timestamp":"2026-10-04T05:00:00Z",'
            b'"trade_id":10,"margin_borrow":1}],"sequence":7}'
        )
        with self.assertRaisesRegex(
            KrakenSpotStreamError,
            "margin_borrow must be an exact boolean",
        ):
            parse_execution_frame(
                raw,
                account_id="spot-live-1",
                connection_generation=1,
            )

    def test_non_trade_report_cannot_carry_trade_only_economics(self):
        raw = frame_bytes(
            reports=[
                {
                    "order_id": "O-STATUS",
                    "exec_type": "status",
                    "order_status": "new",
                    "exec_id": "E-FORGED",
                }
            ],
        )
        with self.assertRaisesRegex(
            KrakenSpotStreamError,
            "non-trade report contains trade-only economics",
        ):
            parse_execution_frame(
                raw,
                account_id="spot-live-1",
                connection_generation=1,
            )

    def test_trade_frame_retains_optional_external_execution_identity(self):
        raw = frame_bytes(
            reports=[
                {
                    "order_id": "O-EXT",
                    "exec_id": "E-EXT",
                    "ext_exec_id": "00000000-0000-0000-0000-000000000042",
                    "exec_type": "trade",
                    "order_status": "partially_filled",
                }
            ],
        )
        report = parse_execution_frame(
            raw,
            account_id="spot-live-1",
            connection_generation=1,
        ).reports[0]
        self.assertEqual(
            report.ext_exec_id,
            "00000000-0000-0000-0000-000000000042",
        )

    def test_non_trade_tolerates_null_conditional_trade_fields(self):
        for field_name in (
            "cost",
            "ext_exec_id",
            "fees",
            "last_price",
            "last_qty",
            "margin_borrow",
            "trade_id",
        ):
            with self.subTest(field_name=field_name):
                report = parse_execution_frame(
                    frame_bytes(
                        reports=[
                            {
                                "order_id": "O-STATUS-NULL",
                                "exec_type": "status",
                                "order_status": "new",
                                field_name: None,
                            }
                        ],
                    ),
                    account_id="spot-live-1",
                    connection_generation=1,
                ).reports[0]
                self.assertEqual(report.exec_type, "status")

    def test_every_execution_report_requires_timestamp(self):
        raw = (
            b'{"channel":"executions","type":"update","data":['
            b'{"order_id":"O-NO-TIME","exec_type":"status","order_status":"new"}],'
            b'"sequence":8}'
        )
        with self.assertRaisesRegex(
            KrakenSpotStreamError,
            "lacks timestamp",
        ):
            parse_execution_frame(
                raw,
                account_id="spot-live-1",
                connection_generation=1,
            )

    def test_explicit_empty_fee_array_is_reported_not_missing(self):
        report = parse_execution_frame(
            frame_bytes(
                reports=[
                    {
                        "order_id": "O-ZERO-FEE",
                        "exec_id": "E-ZERO-FEE",
                        "exec_type": "trade",
                        "order_status": "partially_filled",
                        "fees": [],
                    }
                ],
            ),
            account_id="spot-live-1",
            connection_generation=1,
        ).reports[0]
        self.assertEqual(report.fees, ())
        self.assertTrue(report.fees_reported)
        self.assertTrue(report.trade_economics_complete)

    def test_trade_timestamp_requires_rfc3339_lexical_form(self):
        invalid = (
            "2026-10-04 05:00:00+00:00",
            "2026-10-04T05:00:00",
            "2026-10-04T05:00:00z",
        )
        for timestamp in invalid:
            with self.subTest(timestamp=timestamp):
                raw = frame_bytes(
                    reports=[
                        {
                            "order_id": "O-TIME",
                            "exec_id": "E-TIME",
                            "exec_type": "trade",
                            "order_status": "partially_filled",
                            "timestamp": timestamp,
                        }
                    ],
                )
                with self.assertRaisesRegex(
                    KrakenSpotStreamError,
                    "timestamp must be an RFC3339 timestamp",
                ):
                    parse_execution_frame(
                        raw,
                        account_id="spot-live-1",
                        connection_generation=1,
                    )

    def test_trade_timestamp_accepts_rfc3339_offset(self):
        raw = frame_bytes(
            reports=[
                {
                    "order_id": "O-OFFSET",
                    "exec_id": "E-OFFSET",
                    "exec_type": "trade",
                    "order_status": "partially_filled",
                    "timestamp": "2026-10-04T07:00:00+02:00",
                }
            ],
        )
        report = parse_execution_frame(
            raw,
            account_id="spot-live-1",
            connection_generation=1,
        ).reports[0]
        self.assertEqual(
            report.event_time,
            "2026-10-04T07:00:00+02:00",
        )


    def test_trade_frame_maps_to_existing_canonical_provider_fill_evidence(self):
        frame = parse_execution_frame(
            frame_bytes(
                frame_type="update",
                sequence=44,
                reports=[
                    {
                        "order_id": "O-BRIDGE",
                        "cl_ord_id": "client-bridge",
                        "exec_id": "E-BRIDGE",
                        "exec_type": "trade",
                        "order_status": "partially_filled",
                        "symbol": "BTC/USD",
                        "side": "sell",
                        "last_qty": 1,
                        "last_price": 40000,
                        "cost": 40000,
                        "fees": [
                            {"asset": "USD", "qty": 1},
                            {"asset": "USD", "qty": 2},
                        ],
                        "timestamp": "2026-10-04T05:00:00.123456Z",
                        "trade_id": 404,
                        "margin_borrow": False,
                    }
                ],
            ),
            account_id="spot-live-1",
            connection_generation=1,
        )

        fills = self.provider_fills_from_admitted_frame(
            frame,
            instrument_versions={"BTC/USD": "CRYPTO:BTC-USD:v1"},
        )

        self.assertEqual(len(fills), 1)
        fill = fills[0]
        self.assertEqual(fill.provider_id, "KRAKEN")
        self.assertEqual(fill.account_id, "spot-live-1")
        self.assertEqual(fill.environment, "LIVE")
        self.assertEqual(fill.provider_execution_id, "E-BRIDGE")
        self.assertEqual(fill.client_order_id, "client-bridge")
        self.assertEqual(fill.instrument, "CRYPTO:BTC-USD:v1")
        self.assertEqual(fill.side, "SELL")
        self.assertEqual(fill.quantity, Decimal("1"))
        self.assertEqual(fill.price, Decimal("40000"))
        self.assertEqual(fill.fee_amount, Decimal("3"))
        self.assertEqual(fill.fee_currency, "USD")
        self.assertEqual(fill.trade_time, "2026-10-04T05:00:00.123456Z")
        self.assertEqual(fill.evidence_refs, (frame.evidence_ref,))

    def test_provider_fill_bridge_rejects_conflicting_normalized_authority_maps(self):
        frame = parse_execution_frame(
            frame_bytes(
                frame_type="update",
                sequence=45,
                reports=[
                    {
                        "order_id": "O-MAP-CONFLICT",
                        "cl_ord_id": "client-map-conflict",
                        "exec_id": "E-MAP-CONFLICT",
                        "exec_type": "trade",
                        "order_status": "partially_filled",
                    }
                ],
            ),
            account_id="spot-live-1",
            connection_generation=1,
        )

        with self.assertRaisesRegex(
            KrakenSpotStreamError,
            "instrument_versions contains conflicting symbol mappings",
        ):
            self.provider_fills_from_admitted_frame(
                frame,
                instrument_versions={
                    "BTC/USD": "CRYPTO:BTC-USD:v1",
                    " BTC/USD ": "CRYPTO:OTHER:v1",
                },
                fee_currency_by_symbol={"BTC/USD": "USD"},
            )

        with self.assertRaisesRegex(
            KrakenSpotStreamError,
            "fee_currency_by_symbol contains conflicting symbol mappings",
        ):
            self.provider_fills_from_admitted_frame(
                frame,
                instrument_versions={"BTC/USD": "CRYPTO:BTC-USD:v1"},
                fee_currency_by_symbol={
                    "BTC/USD": "USD",
                    " BTC/USD ": "EUR",
                },
            )

    def test_provider_fill_bridge_ignores_status_only_reports(self):
        frame = parse_execution_frame(
            frame_bytes(
                frame_type="update",
                sequence=45,
                reports=[
                    {
                        "order_id": "O-STATUS-ONLY",
                        "exec_type": "status",
                        "order_status": "new",
                        "symbol": "BTC/USD",
                        "side": "buy",
                    }
                ],
            ),
            account_id="spot-live-1",
            connection_generation=1,
        )
        self.assertEqual(
            self.provider_fills_from_admitted_frame(
                frame,
                instrument_versions={"BTC/USD": "CRYPTO:BTC-USD:v1"},
            ),
            (),
        )

    def test_provider_fill_bridge_rejects_unmapped_symbol_and_mixed_fee_assets(self):
        base = {
            "order_id": "O-BRIDGE-FAIL",
            "cl_ord_id": "client-bridge-fail",
            "exec_id": "E-BRIDGE-FAIL",
            "exec_type": "trade",
            "order_status": "partially_filled",
            "symbol": "BTC/USD",
            "side": "buy",
        }
        frame = parse_execution_frame(
            frame_bytes(frame_type="update", sequence=46, reports=[base]),
            account_id="spot-live-1",
            connection_generation=1,
        )
        with self.assertRaisesRegex(
            KrakenSpotStreamError,
            "unmapped Kraken executions symbol",
        ):
            self.provider_fills_from_admitted_frame(
                frame,
                instrument_versions={},
            )

        mixed = dict(base)
        mixed["fees"] = [
            {"asset": "USD", "qty": 1},
            {"asset": "EUR", "qty": 1},
        ]
        mixed_frame = parse_execution_frame(
            frame_bytes(frame_type="update", sequence=47, reports=[mixed]),
            account_id="spot-live-1",
            connection_generation=1,
        )
        with self.assertRaisesRegex(
            KrakenSpotStreamError,
            "fee asset disagrees with evidenced fee currency",
        ):
            self.provider_fills_from_admitted_frame(
                mixed_frame,
                instrument_versions={"BTC/USD": "CRYPTO:BTC-USD:v1"},
            )


    def test_provider_fill_bridge_uses_explicit_fee_currency_for_zero_fee_trade(self):
        frame = parse_execution_frame(
            frame_bytes(
                frame_type="update",
                sequence=48,
                reports=[
                    {
                        "order_id": "O-ZERO-FEE-BRIDGE",
                        "cl_ord_id": "client-zero-fee",
                        "exec_id": "E-ZERO-FEE-BRIDGE",
                        "exec_type": "trade",
                        "order_status": "partially_filled",
                        "fees": [],
                    }
                ],
            ),
            account_id="spot-live-1",
            connection_generation=1,
        )
        fill = self.provider_fills_from_admitted_frame(
            frame,
            instrument_versions={"BTC/USD": "CRYPTO:BTC-USD:v1"},
            fee_currency_by_symbol={"BTC/USD": "USD"},
        )[0]
        self.assertEqual(fill.fee_amount, Decimal("0"))
        self.assertEqual(fill.fee_currency, "USD")

        with self.assertRaisesRegex(
            KrakenSpotStreamError,
            "missing evidenced fee currency",
        ):
            self.provider_fills_from_admitted_frame(
                frame,
                instrument_versions={"BTC/USD": "CRYPTO:BTC-USD:v1"},
                fee_currency_by_symbol={},
            )


    def test_provider_fill_bridge_defers_execution_identity_to_canonical_reconciliation(self):
        frame = parse_execution_frame(
            frame_bytes(
                frame_type="update",
                sequence=49,
                reports=[
                    {
                        "order_id": "O-CANONICAL-ID",
                        "cl_ord_id": "client-canonical-id",
                        "exec_id": "E-CANONICAL-ID",
                        "exec_type": "trade",
                        "order_status": "partially_filled",
                    }
                ],
            ),
            account_id="spot-live-1",
            connection_generation=1,
        )
        fill = self.provider_fills_from_admitted_frame(
            frame,
            instrument_versions={"BTC/USD": "CRYPTO:BTC-USD:v1"},
        )[0]

        result = reconcile_account(
            provider_id="KRAKEN",
            account_id="spot-live-1",
            environment="LIVE",
            local_cash={},
            provider_cash={},
            local_positions={},
            provider_positions={},
            local_execution_ids=(),
            provider_fills=(fill, fill),
            coverage_start="2026-10-04T04:59:00Z",
            coverage_end="2026-10-04T05:01:00Z",
            pagination_complete=False,
        )

        self.assertEqual(
            result.unexpected_execution_ids,
            ("E-CANONICAL-ID",),
        )

    def test_canonical_reconciliation_rejects_changed_economics_for_same_stream_exec_id(self):
        common = {
            "order_id": "O-CANONICAL-CONFLICT",
            "cl_ord_id": "client-canonical-conflict",
            "exec_id": "E-CANONICAL-CONFLICT",
            "exec_type": "trade",
            "order_status": "partially_filled",
        }
        first = self.provider_fills_from_admitted_frame(
            parse_execution_frame(
                frame_bytes(
                    frame_type="update",
                    sequence=50,
                    reports=[common],
                ),
                account_id="spot-live-1",
                connection_generation=1,
            ),
            instrument_versions={"BTC/USD": "CRYPTO:BTC-USD:v1"},
        )[0]
        changed = dict(common)
        changed["last_price"] = 25001
        changed["cost"] = 25001
        second = self.provider_fills_from_admitted_frame(
            parse_execution_frame(
                frame_bytes(
                    frame_type="update",
                    sequence=51,
                    reports=[changed],
                ),
                account_id="spot-live-1",
                connection_generation=1,
            ),
            instrument_versions={"BTC/USD": "CRYPTO:BTC-USD:v1"},
        )[0]

        with self.assertRaisesRegex(
            ValueError,
            "provider execution id has conflicting observations",
        ):
            reconcile_account(
                provider_id="KRAKEN",
                account_id="spot-live-1",
                environment="LIVE",
                local_cash={},
                provider_cash={},
                local_positions={},
                provider_positions={},
                local_execution_ids=(),
                provider_fills=(first, second),
                coverage_start="2026-10-04T04:59:00Z",
                coverage_end="2026-10-04T05:01:00Z",
                pagination_complete=False,
            )


    def test_provider_fill_bridge_requires_proven_cash_semantics(self):
        for margin_value in (None, True):
            with self.subTest(margin_borrow=margin_value):
                frame = parse_execution_frame(
                    frame_bytes(
                        frame_type="update",
                        sequence=52,
                        reports=[
                            {
                                "order_id": "O-MARGIN-BRIDGE",
                                "cl_ord_id": "client-margin-bridge",
                                "exec_id": "E-MARGIN-BRIDGE",
                                "exec_type": "trade",
                                "order_status": "partially_filled",
                                "margin_borrow": margin_value,
                            }
                        ],
                    ),
                    account_id="spot-live-1",
                    connection_generation=1,
                )
                with self.assertRaisesRegex(
                    KrakenSpotStreamError,
                    "margin_borrow=false is provider-evidenced",
                ):
                    self.provider_fills_from_admitted_frame(
                        frame,
                        instrument_versions={"BTC/USD": "CRYPTO:BTC-USD:v1"},
                    )

    def test_provider_fill_bridge_rejects_cost_not_exactly_represented_by_qty_and_price(self):
        frame = parse_execution_frame(
            frame_bytes(
                frame_type="update",
                sequence=53,
                reports=[
                    {
                        "order_id": "O-COST-BRIDGE",
                        "cl_ord_id": "client-cost-bridge",
                        "exec_id": "E-COST-BRIDGE",
                        "exec_type": "trade",
                        "order_status": "partially_filled",
                        "last_qty": 2,
                        "last_price": 25000,
                        "cost": 49999,
                    }
                ],
            ),
            account_id="spot-live-1",
            connection_generation=1,
        )
        with self.assertRaisesRegex(
            KrakenSpotStreamError,
            r"cost differs from exact last_qty \* last_price",
        ):
            self.provider_fills_from_admitted_frame(
                frame,
                instrument_versions={"BTC/USD": "CRYPTO:BTC-USD:v1"},
            )


    def test_empty_fill_batch_still_validates_and_detaches_authority_maps(self):
        touched: list[str] = []

        class HostileDict(dict):
            def items(self):
                touched.append("items")
                raise AssertionError("hostile mapping iteration")

            def copy(self):
                touched.append("copy")
                raise AssertionError("hostile mapping copy")

        recovery = KrakenSpotExecutionStreamRecovery(account_id="spot-live-1")
        generation = recovery.begin_connection()
        binding = KrakenSpotExecutionsSubscriptionBinding.create(
            account_id="spot-live-1",
            connection_generation=generation,
            req_id=7,
        )
        recovery.apply_subscription_ack(
            parse_executions_subscription_ack(
                ack_bytes(req_id=7),
                subscription_binding=binding,
            )
        )
        recovery.apply_frame(
            parse_execution_frame(
                frame_bytes(
                    frame_type="snapshot",
                    sequence=1,
                    reports=[
                        {
                            "order_id": "O-EMPTY-BATCH",
                            "exec_type": "new",
                            "order_status": "new",
                        }
                    ],
                ),
                account_id="spot-live-1",
                connection_generation=generation,
            )
        )

        with self.assertRaisesRegex(TypeError, "instrument_versions must be an exact dict"):
            recovery.buffered_provider_fills(
                instrument_versions=HostileDict(
                    {"BTC/USD": "CRYPTO:BTC-USD:v1"}
                ),
                fee_currency_by_symbol={"BTC/USD": "USD"},
            )
        with self.assertRaisesRegex(TypeError, "fee_currency_by_symbol must be an exact dict"):
            recovery.buffered_provider_fills(
                instrument_versions={"BTC/USD": "CRYPTO:BTC-USD:v1"},
                fee_currency_by_symbol=HostileDict({"BTC/USD": "USD"}),
            )
        self.assertEqual(touched, [])

        self.assertEqual(
            recovery.buffered_provider_fills(
                instrument_versions={"BTC/USD": "CRYPTO:BTC-USD:v1"},
                fee_currency_by_symbol={"BTC/USD": "USD"},
            ),
            (),
        )

    def test_provider_fill_extraction_requires_recovery_admission(self):
        recovery = KrakenSpotExecutionStreamRecovery(
            account_id="spot-live-1",
        )
        recovery.begin_connection()
        with self.assertRaisesRegex(
            KrakenSpotStreamError,
            "require gap-free stream recovery",
        ):
            recovery.buffered_provider_fills(
                instrument_versions={"BTC/USD": "CRYPTO:BTC-USD:v1"},
                fee_currency_by_symbol={"BTC/USD": "USD"},
            )

    def test_sequence_gap_clears_fill_buffer_and_blocks_extraction(self):
        recovery = KrakenSpotExecutionStreamRecovery(
            account_id="spot-live-1",
        )
        generation = recovery.begin_connection()
        binding = KrakenSpotExecutionsSubscriptionBinding.create(
            account_id="spot-live-1",
            connection_generation=generation,
            req_id=7,
        )
        recovery.apply_subscription_ack(
            parse_executions_subscription_ack(
                ack_bytes(req_id=7),
                subscription_binding=binding,
            )
        )
        recovery.apply_frame(
            parse_execution_frame(
                frame_bytes(
                    frame_type="snapshot",
                    sequence=1,
                    reports=[
                        {
                            "order_id": "O-GAP-SNAPSHOT",
                            "exec_type": "new",
                            "order_status": "new",
                        }
                    ],
                ),
                account_id="spot-live-1",
                connection_generation=generation,
            )
        )
        recovery.apply_frame(
            parse_execution_frame(
                frame_bytes(
                    frame_type="update",
                    sequence=2,
                    reports=[
                        {
                            "order_id": "O-GAP-TRADE",
                            "exec_id": "E-GAP-TRADE",
                            "exec_type": "trade",
                            "order_status": "partially_filled",
                        }
                    ],
                ),
                account_id="spot-live-1",
                connection_generation=generation,
            )
        )
        self.assertEqual(len(recovery.evidence().buffered_update_evidence_refs), 1)
        recovery.apply_frame(
            parse_execution_frame(
                frame_bytes(
                    frame_type="update",
                    sequence=4,
                    reports=[
                        {
                            "order_id": "O-GAP-LATE",
                            "exec_type": "status",
                            "order_status": "new",
                        }
                    ],
                ),
                account_id="spot-live-1",
                connection_generation=generation,
            )
        )
        self.assertEqual(recovery.evidence().buffered_update_evidence_refs, ())
        with self.assertRaisesRegex(
            KrakenSpotStreamError,
            "require gap-free stream recovery",
        ):
            recovery.buffered_provider_fills(
                instrument_versions={"BTC/USD": "CRYPTO:BTC-USD:v1"},
                fee_currency_by_symbol={"BTC/USD": "USD"},
            )


    def test_admitted_fill_evidence_is_carried_into_required_rest_crosscheck(self):
        frame = parse_execution_frame(
            frame_bytes(
                frame_type="update",
                sequence=60,
                reports=[
                    {
                        "order_id": "O-HANDOFF",
                        "cl_ord_id": "client-handoff",
                        "exec_id": "E-HANDOFF",
                        "exec_type": "trade",
                        "order_status": "partially_filled",
                    }
                ],
            ),
            account_id="spot-live-1",
            connection_generation=1,
        )
        recovery = KrakenSpotExecutionStreamRecovery(
            account_id="spot-live-1",
        )
        generation = recovery.begin_connection()
        binding = KrakenSpotExecutionsSubscriptionBinding.create(
            account_id="spot-live-1",
            connection_generation=generation,
            req_id=7,
        )
        recovery.apply_subscription_ack(
            parse_executions_subscription_ack(
                ack_bytes(req_id=7),
                subscription_binding=binding,
            )
        )
        recovery.apply_frame(
            parse_execution_frame(
                frame_bytes(
                    frame_type="snapshot",
                    sequence=59,
                    reports=[
                        {
                            "order_id": "O-HANDOFF-SNAPSHOT",
                            "exec_type": "new",
                            "order_status": "new",
                        }
                    ],
                ),
                account_id="spot-live-1",
                connection_generation=generation,
            )
        )
        recovery.apply_frame(frame)

        fills = recovery.buffered_provider_fills(
            instrument_versions={"BTC/USD": "CRYPTO:BTC-USD:v1"},
            fee_currency_by_symbol={"BTC/USD": "USD"},
        )
        plan = recovery.rest_crosscheck_plan()
        evidence = recovery.evidence()

        self.assertEqual(len(fills), 1)
        self.assertIn(fills[0].evidence_refs[0], plan.evidence_refs)
        self.assertIn(fills[0].evidence_refs[0], evidence.buffered_update_evidence_refs)
        self.assertFalse(plan.trading_ready)
        self.assertFalse(evidence.trading_ready)
        self.assertEqual(
            evidence.phase,
            KrakenSpotExecutionStreamRecovery.REST_RECONCILIATION_REQUIRED,
        )

    def test_json_numeric_tokens_use_shared_exact_resource_envelope(self):
        huge_integer = b"9" * 1000
        raw_sequence = (
            b'{"channel":"executions","type":"snapshot","data":[],"sequence":'
            + huge_integer
            + b"}"
        )
        huge_exponent = (
            b'{"channel":"executions","type":"update","data":['
            b'{"order_id":"O-HUGE","exec_id":"E-HUGE","exec_type":"trade",'
            b'"order_status":"partially_filled","symbol":"BTC/USD","side":"buy",'
            b'"last_qty":1e999999,"last_price":25000,'
            b'"fees":[{"asset":"USD","qty":1}],'
            b'"timestamp":"2026-10-04T05:00:00Z"}],"sequence":1}'
        )
        for raw in (raw_sequence, huge_exponent):
            with self.subTest(raw_prefix=raw[:80]), self.assertRaisesRegex(
                KrakenSpotStreamError,
                "numeric token exceeds the exact resource envelope",
            ):
                parse_execution_frame(
                    raw,
                    account_id="spot-live-1",
                    connection_generation=1,
                )

    def test_byte_distinct_frames_have_distinct_evidence(self):
        compact = (
            b'{"channel":"executions","type":"snapshot","data":[],"sequence":1}'
        )
        spaced = (
            b'{"channel": "executions", "type":"snapshot","data":[],"sequence":1}'
        )
        first = parse_execution_frame(
            compact,
            account_id="acct",
            connection_generation=1,
        )
        second = parse_execution_frame(
            spaced,
            account_id="acct",
            connection_generation=1,
        )
        self.assertNotEqual(first.evidence_ref, second.evidence_ref)
        self.assertNotEqual(first.response_bytes, second.response_bytes)

    def test_parser_rejects_noncanonical_or_ambiguous_frames(self):
        cases = (
            (
                b'{"channel":"executions","channel":"executions","type":"snapshot","data":[],"sequence":1}',
                "duplicate JSON object field",
            ),
            (
                b'{"channel":"executions","type":"snapshot","data":[],"sequence":1,"token":"secret"}',
                "fields are not canonical",
            ),
            (
                frame_bytes(channel="balances"),
                "channel must be executions",
            ),
            (
                frame_bytes(frame_type="heartbeat"),
                "type must be snapshot or update",
            ),
            (
                b'{"channel":"executions","type":"snapshot","data":[],"sequence":true}',
                "sequence must be a non-negative integer",
            ),
            (
                b'{"channel":"executions","type":"snapshot","data":{},"sequence":1}',
                "data must be an array",
            ),
            (
                frame_bytes(reports=[{"exec_type": "new", "order_status": "new"}]),
                "lacks order_id",
            ),
            (
                frame_bytes(reports=[{"order_id": "O-1", "order_status": "new"}]),
                "lacks exec_type",
            ),
            (
                frame_bytes(
                    reports=[
                        {
                            "order_id": "O-1",
                            "exec_type": "mystery",
                            "order_status": "new",
                        }
                    ]
                ),
                "exec_type is unsupported",
            ),
            (
                frame_bytes(
                    reports=[
                        {
                            "order_id": "O-1",
                            "exec_type": "status",
                            "order_status": "unknown",
                        }
                    ]
                ),
                "order_status is unsupported",
            ),
        )
        for raw, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(
                KrakenSpotStreamError,
                message,
            ):
                parse_execution_frame(
                    raw,
                    account_id="acct",
                    connection_generation=1,
                )

    def test_subscription_ack_binds_canonical_request_profile_and_exact_bytes(self):
        binding = subscription_binding()
        raw = ack_bytes(req_id=binding.req_id)
        acknowledgement = parse_executions_subscription_ack(
            raw,
            subscription_binding=binding,
        )
        self.assertEqual(acknowledgement.account_id, "spot-live-1")
        self.assertEqual(acknowledgement.environment, "LIVE")
        self.assertIs(acknowledgement.subscription_binding, binding)
        self.assertEqual(
            acknowledgement.subscription_binding.evidence_ref,
            binding.evidence_ref,
        )
        self.assertEqual(
            acknowledgement.evidence_ref,
            "provider-stream:sha256:" + hashlib.sha256(raw).hexdigest(),
        )
        self.assertNotIn(b"token", acknowledgement.response_bytes.lower())
        self.assertNotIn("token", binding.evidence_ref)

    def test_subscription_ack_retains_rate_ceiling_and_schema_warnings(self):
        binding = subscription_binding()
        raw = (
            b'{"method":"subscribe","req_id":7,"success":true,"result":{'
            b'"channel":"executions","snap_orders":true,"snap_trades":false,'
            b'"maxratecount":180,"warnings":["field deprecation scheduled"]}}'
        )
        acknowledgement = parse_executions_subscription_ack(
            raw,
            subscription_binding=binding,
        )
        self.assertEqual(acknowledgement.maxratecount, 180)
        self.assertEqual(
            acknowledgement.warnings,
            ("field deprecation scheduled",),
        )

    def test_subscription_ack_rejects_malformed_rate_ceiling_or_warnings(self):
        binding = subscription_binding()
        cases = (
            (
                b'{"method":"subscribe","req_id":7,"success":true,"result":{'
                b'"channel":"executions","snap_orders":true,"snap_trades":false,'
                b'"maxratecount":true}}',
                "maxratecount",
            ),
            (
                b'{"method":"subscribe","req_id":7,"success":true,"result":{'
                b'"channel":"executions","snap_orders":true,"snap_trades":false,'
                b'"warnings":"deprecated"}}',
                "warnings",
            ),
        )
        for raw, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(
                KrakenSpotStreamError,
                message,
            ):
                parse_executions_subscription_ack(
                    raw,
                    subscription_binding=binding,
                )

    def test_subscription_binding_rejects_noncanonical_profile(self):
        valid = subscription_binding()
        with self.assertRaisesRegex(
            KrakenSpotStreamError,
            "profile is not canonical",
        ):
            KrakenSpotExecutionsSubscriptionBinding(
                account_id=valid.account_id,
                environment=valid.environment,
                connection_generation=valid.connection_generation,
                req_id=valid.req_id,
                profile_items=(
                    ("channel", "executions"),
                    ("order_status", False),
                    ("snap_orders", True),
                    ("snap_trades", False),
                ),
                evidence_ref=valid.evidence_ref,
            )

    def test_subscription_ack_rejects_wrong_profile_failed_or_wrong_request(self):
        binding = subscription_binding()
        cases = (
            (ack_bytes(success=False), "was not accepted"),
            (ack_bytes(channel="balances"), "channel must be executions"),
            (ack_bytes(snap_orders=False), "snap_orders=true"),
            (ack_bytes(snap_trades=True), "snap_trades=false"),
            (ack_bytes(req_id=binding.req_id + 1), "req_id does not match"),
        )
        for raw, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(
                KrakenSpotStreamError,
                message,
            ):
                parse_executions_subscription_ack(
                    raw,
                    subscription_binding=binding,
                )

    def test_parser_is_live_only_and_scope_text_is_canonical(self):
        raw = frame_bytes()
        with self.assertRaisesRegex(KrakenSpotStreamError, "permits LIVE only"):
            parse_execution_frame(
                raw,
                account_id="acct",
                connection_generation=1,
                environment="PAPER",
            )
        with self.assertRaisesRegex(KrakenSpotStreamError, "canonical text"):
            parse_execution_frame(
                raw,
                account_id=" acct ",
                connection_generation=1,
            )


class KrakenSpotExecutionStreamRecoveryTests(unittest.TestCase):
    def make_recovery(self, **kwargs):
        return KrakenSpotExecutionStreamRecovery(
            account_id="spot-live-1",
            **kwargs,
        )

    def acknowledge(
        self,
        recovery,
        *,
        account_id="spot-live-1",
        connection_generation=None,
        req_id=7,
    ):
        if connection_generation is None:
            connection_generation = recovery.connection_generation
        binding = subscription_binding(
            account_id=account_id,
            connection_generation=connection_generation,
            req_id=req_id,
        )
        acknowledgement = parse_executions_subscription_ack(
            ack_bytes(req_id=req_id),
            subscription_binding=binding,
        )
        recovery.apply_subscription_ack(acknowledgement)
        return acknowledgement

    def parse(
        self,
        *,
        recovery,
        frame_type,
        sequence,
        reports=None,
        account_id="spot-live-1",
        connection_generation=None,
    ):
        if connection_generation is None:
            connection_generation = recovery.connection_generation
        return parse_execution_frame(
            frame_bytes(
                frame_type=frame_type,
                sequence=sequence,
                reports=reports,
            ),
            account_id=account_id,
            connection_generation=connection_generation,
        )

    def test_recovery_handoff_retains_subscription_rate_and_warning_evidence(self):
        recovery = self.make_recovery()
        generation = recovery.begin_connection()
        binding = subscription_binding(
            connection_generation=generation,
            req_id=77,
        )
        raw = (
            b'{"method":"subscribe","req_id":77,"success":true,"result":{'
            b'"channel":"executions","snap_orders":true,"snap_trades":false,'
            b'"maxratecount":240,"warnings":["schema change pending"]}}'
        )
        acknowledgement = parse_executions_subscription_ack(
            raw,
            subscription_binding=binding,
        )
        recovery.apply_subscription_ack(acknowledgement)
        evidence = recovery.evidence()
        self.assertEqual(evidence.subscription_maxratecount, 240)
        self.assertEqual(
            evidence.subscription_warnings,
            ("schema change pending",),
        )
        recovery.disconnect()
        reset = recovery.evidence()
        self.assertIsNone(reset.subscription_maxratecount)
        self.assertEqual(reset.subscription_warnings, ())

    def test_generation_requires_fresh_snapshot_and_never_grants_ready(self):
        recovery = self.make_recovery()
        generation = recovery.begin_connection()
        self.assertEqual(generation, 1)
        self.assertEqual(
            recovery.evidence().phase,
            recovery.AWAITING_SUBSCRIPTION_ACK,
        )
        self.assertEqual(
            recovery.evidence().recovery_reason,
            "fresh_subscription_required",
        )

        with self.assertRaisesRegex(
            KrakenSpotStreamError,
            "requires subscription acknowledgement",
        ):
            recovery.apply_frame(
                self.parse(recovery=recovery, frame_type="update", sequence=1)
            )

        acknowledgement = self.acknowledge(recovery)
        self.assertEqual(
            recovery.evidence().phase,
            recovery.AWAITING_SNAPSHOT,
        )
        self.assertEqual(
            recovery.evidence().subscription_binding_evidence_ref,
            acknowledgement.subscription_binding.evidence_ref,
        )
        self.assertEqual(
            recovery.evidence().subscription_ack_evidence_ref,
            acknowledgement.evidence_ref,
        )
        self.assertEqual(
            recovery.evidence().recovery_reason,
            "fresh_snapshot_required",
        )
        with self.assertRaisesRegex(
            KrakenSpotStreamError,
            "requires snapshot before updates",
        ):
            recovery.apply_frame(
                self.parse(recovery=recovery, frame_type="update", sequence=1)
            )

        snapshot = self.parse(recovery=recovery, 
            frame_type="snapshot",
            sequence=7,
            reports=[
                {
                    "order_id": "O-2",
                    "exec_type": "new",
                    "order_status": "new",
                },
                {
                    "order_id": "O-1",
                    "exec_type": "status",
                    "order_status": "partially_filled",
                },
            ],
        )
        recovery.apply_frame(snapshot)
        evidence = recovery.evidence()

        self.assertEqual(
            evidence.phase,
            recovery.REST_RECONCILIATION_REQUIRED,
        )
        self.assertEqual(evidence.snapshot_sequence, 7)
        self.assertEqual(evidence.last_sequence, 7)
        self.assertEqual(evidence.snapshot_evidence_ref, snapshot.evidence_ref)
        self.assertEqual(
            evidence.provisional_snapshot_order_ids,
            ("O-1", "O-2"),
        )
        self.assertEqual(evidence.buffered_update_evidence_refs, ())
        self.assertEqual(
            evidence.recovery_reason,
            "snapshot_requires_rest_crosscheck",
        )
        self.assertFalse(evidence.trading_ready)

    def test_snapshot_rejects_trade_events_when_snap_trades_is_false(self):
        recovery = self.make_recovery()
        recovery.begin_connection()
        self.acknowledge(recovery)
        trade_snapshot = self.parse(
            recovery=recovery,
            frame_type="snapshot",
            sequence=1,
            reports=[
                {
                    "order_id": "O-TRADE-SNAPSHOT",
                    "exec_id": "E-TRADE-SNAPSHOT",
                    "exec_type": "trade",
                    "order_status": "partially_filled",
                }
            ],
        )
        before = recovery.evidence()
        with self.assertRaisesRegex(
            KrakenSpotStreamError,
            "snap_trades=false snapshot contains trade events",
        ):
            recovery.apply_frame(trade_snapshot)
        self.assertEqual(recovery.evidence(), before)

    def test_snapshot_rejects_terminal_orders_under_snap_orders_contract(self):
        recovery = self.make_recovery()
        recovery.begin_connection()
        self.acknowledge(recovery)
        terminal = self.parse(recovery=recovery, 
            frame_type="snapshot",
            sequence=1,
            reports=[
                {
                    "order_id": "O-DONE",
                    "exec_type": "filled",
                    "order_status": "filled",
                }
            ],
        )
        with self.assertRaisesRegex(
            KrakenSpotStreamError,
            "snapshot contains terminal orders",
        ):
            recovery.apply_frame(terminal)

    def test_contiguous_updates_are_buffered_as_exact_reconciliation_evidence(self):
        recovery = self.make_recovery()
        recovery.begin_connection()
        self.acknowledge(recovery)
        recovery.apply_frame(
            self.parse(recovery=recovery, frame_type="snapshot", sequence=10)
        )
        update_11 = self.parse(recovery=recovery, 
            frame_type="update",
            sequence=11,
            reports=[
                {
                    "order_id": "O-1",
                    "exec_type": "trade",
                    "order_status": "partially_filled",
                    "exec_id": "E-1",
                }
            ],
        )
        update_12 = self.parse(recovery=recovery, 
            frame_type="update",
            sequence=12,
            reports=[
                {
                    "order_id": "O-1",
                    "exec_type": "filled",
                    "order_status": "filled",
                }
            ],
        )
        recovery.apply_frame(update_11)
        recovery.apply_frame(update_12)
        evidence = recovery.evidence()

        self.assertEqual(
            evidence.phase,
            recovery.REST_RECONCILIATION_REQUIRED,
        )
        self.assertEqual(evidence.last_sequence, 12)
        self.assertEqual(
            evidence.buffered_update_evidence_refs,
            (update_11.evidence_ref, update_12.evidence_ref),
        )
        self.assertFalse(evidence.trading_ready)

    def test_duplicate_or_forward_sequence_gap_fails_closed_and_discards_projection(self):
        for observed in (10, 13):
            with self.subTest(observed=observed):
                recovery = self.make_recovery()
                recovery.begin_connection()
                self.acknowledge(recovery)
                recovery.apply_frame(
                    self.parse(recovery=recovery, frame_type="snapshot", sequence=10)
                )
                gap = self.parse(recovery=recovery, 
                    frame_type="update",
                    sequence=observed,
                )
                recovery.apply_frame(gap)
                evidence = recovery.evidence()

                self.assertEqual(
                    evidence.phase,
                    recovery.GAP_RECONCILIATION_REQUIRED,
                )
                self.assertEqual(evidence.gap_expected_sequence, 11)
                self.assertEqual(evidence.gap_observed_sequence, observed)
                self.assertEqual(
                    evidence.gap_evidence_ref,
                    gap.evidence_ref,
                )
                self.assertEqual(evidence.recovery_reason, "sequence_gap")
                self.assertEqual(
                    evidence.provisional_snapshot_order_ids,
                    (),
                )
                self.assertEqual(
                    evidence.buffered_update_evidence_refs,
                    (),
                )
                self.assertFalse(evidence.trading_ready)
                with self.assertRaisesRegex(
                    KrakenSpotStreamError,
                    "gap requires a fresh connection",
                ):
                    recovery.apply_frame(
                        self.parse(recovery=recovery, frame_type="update", sequence=11)
                    )

    def test_reconnect_discards_stale_generation_and_requires_new_snapshot(self):
        recovery = self.make_recovery()
        self.assertEqual(recovery.begin_connection(), 1)
        self.acknowledge(recovery)
        recovery.apply_frame(
            self.parse(recovery=recovery, frame_type="snapshot", sequence=20)
        )
        recovery.apply_frame(
            self.parse(recovery=recovery, frame_type="update", sequence=21)
        )

        self.assertEqual(recovery.begin_connection(), 2)
        reset = recovery.evidence()
        self.assertEqual(
            reset.phase,
            recovery.AWAITING_SUBSCRIPTION_ACK,
        )
        self.assertIsNone(reset.snapshot_sequence)
        self.assertIsNone(reset.last_sequence)
        self.assertIsNone(reset.snapshot_evidence_ref)
        self.assertEqual(reset.buffered_update_evidence_refs, ())
        self.assertEqual(reset.provisional_snapshot_order_ids, ())
        self.assertEqual(
            reset.recovery_reason,
            "fresh_subscription_required",
        )

        with self.assertRaisesRegex(
            KrakenSpotStreamError,
            "requires subscription acknowledgement",
        ):
            recovery.apply_frame(
                self.parse(recovery=recovery, frame_type="update", sequence=22)
            )
        self.acknowledge(recovery)
        with self.assertRaisesRegex(
            KrakenSpotStreamError,
            "requires snapshot before updates",
        ):
            recovery.apply_frame(
                self.parse(recovery=recovery, frame_type="update", sequence=22)
            )
        fresh = self.parse(recovery=recovery, 
            frame_type="snapshot",
            sequence=1,
            reports=[],
            connection_generation=2,
        )
        recovery.apply_frame(fresh)
        self.assertEqual(
            recovery.evidence().phase,
            recovery.REST_RECONCILIATION_REQUIRED,
        )
        self.assertEqual(recovery.evidence().connection_generation, 2)

    def test_reconnect_rejects_stale_generation_ack_snapshot_and_expected_update(self):
        recovery = self.make_recovery()
        self.assertEqual(recovery.begin_connection(), 1)

        stale_binding = subscription_binding(connection_generation=1, req_id=91)
        stale_ack = parse_executions_subscription_ack(
            ack_bytes(req_id=91),
            subscription_binding=stale_binding,
        )
        stale_snapshot = self.parse(recovery=recovery, 
            frame_type="snapshot",
            sequence=10,
            connection_generation=1,
        )
        stale_update = self.parse(recovery=recovery, 
            frame_type="update",
            sequence=11,
            reports=[
                {
                    "order_id": "O-STALE",
                    "exec_type": "trade",
                    "order_status": "partially_filled",
                    "exec_id": "E-STALE",
                }
            ],
            connection_generation=1,
        )

        self.assertEqual(recovery.begin_connection(), 2)
        before = recovery.evidence()
        with self.assertRaisesRegex(
            KrakenSpotStreamError,
            "acknowledgement generation mismatch",
        ):
            recovery.apply_subscription_ack(stale_ack)
        self.assertEqual(recovery.evidence(), before)

        self.acknowledge(recovery, req_id=92)
        awaiting_snapshot = recovery.evidence()
        with self.assertRaisesRegex(
            KrakenSpotStreamError,
            "connection generation mismatch",
        ):
            recovery.apply_frame(stale_snapshot)
        self.assertEqual(recovery.evidence(), awaiting_snapshot)

        fresh_snapshot = self.parse(recovery=recovery, 
            frame_type="snapshot",
            sequence=10,
            connection_generation=2,
        )
        recovery.apply_frame(fresh_snapshot)
        before_expected_stale_update = recovery.evidence()
        self.assertEqual(before_expected_stale_update.last_sequence, 10)
        with self.assertRaisesRegex(
            KrakenSpotStreamError,
            "connection generation mismatch",
        ):
            recovery.apply_frame(stale_update)
        self.assertEqual(
            recovery.evidence(),
            before_expected_stale_update,
        )

    def test_disconnect_invalidates_all_stream_local_state(self):
        recovery = self.make_recovery()
        recovery.begin_connection()
        self.acknowledge(recovery)
        recovery.apply_frame(
            self.parse(recovery=recovery, frame_type="snapshot", sequence=2)
        )
        recovery.disconnect()
        evidence = recovery.evidence()
        self.assertEqual(evidence.phase, recovery.DISCONNECTED)
        self.assertIsNone(evidence.last_sequence)
        self.assertEqual(evidence.provisional_snapshot_order_ids, ())
        self.assertIsNone(evidence.recovery_reason)
        with self.assertRaisesRegex(
            KrakenSpotStreamError,
            "while disconnected",
        ):
            recovery.apply_frame(
                self.parse(recovery=recovery, frame_type="snapshot", sequence=3)
            )

    def test_cross_account_subscription_ack_is_rejected_before_state_mutation(self):
        recovery = self.make_recovery()
        recovery.begin_connection()
        with self.assertRaisesRegex(
            KrakenSpotStreamError,
            "acknowledgement scope mismatch",
        ):
            self.acknowledge(recovery, account_id="other-account")
        evidence = recovery.evidence()
        self.assertEqual(
            evidence.phase,
            recovery.AWAITING_SUBSCRIPTION_ACK,
        )
        self.assertIsNone(evidence.subscription_binding_evidence_ref)
        self.assertIsNone(evidence.subscription_ack_evidence_ref)

    def test_cross_account_frame_is_rejected_before_state_mutation(self):
        recovery = self.make_recovery()
        recovery.begin_connection()
        self.acknowledge(recovery)
        wrong = self.parse(recovery=recovery, 
            frame_type="snapshot",
            sequence=1,
            account_id="other-account",
        )
        with self.assertRaisesRegex(
            KrakenSpotStreamError,
            "account/environment scope mismatch",
        ):
            recovery.apply_frame(wrong)
        evidence = recovery.evidence()
        self.assertEqual(evidence.phase, recovery.AWAITING_SNAPSHOT)
        self.assertIsNone(evidence.last_sequence)

    def test_rest_crosscheck_plan_chunks_query_orders_and_never_grants_ready(self):
        recovery = self.make_recovery()
        recovery.begin_connection()
        acknowledgement = self.acknowledge(recovery)
        reports = [
            {
                "order_id": f"O-{index:03d}",
                "exec_type": "new",
                "order_status": "new",
            }
            for index in range(51)
        ]
        snapshot = self.parse(recovery=recovery, 
            frame_type="snapshot",
            sequence=5,
            reports=reports,
        )
        recovery.apply_frame(snapshot)

        plan = recovery.rest_crosscheck_plan()
        self.assertEqual(
            plan.required_endpoints,
            (
                "/0/private/OpenOrders",
                "/0/private/ClosedOrders",
                "/0/private/TradesHistory",
                "/0/private/QueryOrders",
            ),
        )
        self.assertEqual(len(plan.query_order_chunks), 2)
        self.assertEqual(len(plan.query_order_chunks[0]), 50)
        self.assertEqual(len(plan.query_order_chunks[1]), 1)
        self.assertEqual(
            tuple(
                order_id
                for chunk in plan.query_order_chunks
                for order_id in chunk
            ),
            tuple(f"O-{index:03d}" for index in range(51)),
        )
        self.assertEqual(
            plan.evidence_refs,
            (
                acknowledgement.subscription_binding.evidence_ref,
                acknowledgement.evidence_ref,
                snapshot.evidence_ref,
            ),
        )
        self.assertEqual(
            plan.recovery_reason,
            "snapshot_requires_rest_crosscheck",
        )
        self.assertFalse(plan.trading_ready)

    def test_gap_crosscheck_plan_retains_known_ids_and_exact_evidence(self):
        recovery = self.make_recovery()
        recovery.begin_connection()
        acknowledgement = self.acknowledge(recovery)
        snapshot = self.parse(recovery=recovery, 
            frame_type="snapshot",
            sequence=10,
            reports=[
                {
                    "order_id": "O-1",
                    "exec_type": "new",
                    "order_status": "new",
                }
            ],
        )
        update = self.parse(recovery=recovery, 
            frame_type="update",
            sequence=11,
            reports=[
                {
                    "order_id": "O-2",
                    "exec_type": "new",
                    "order_status": "new",
                }
            ],
        )
        gap = self.parse(recovery=recovery, 
            frame_type="update",
            sequence=13,
            reports=[
                {
                    "order_id": "O-3",
                    "exec_type": "new",
                    "order_status": "new",
                }
            ],
        )
        recovery.apply_frame(snapshot)
        recovery.apply_frame(update)
        recovery.apply_frame(gap)

        plan = recovery.rest_crosscheck_plan()
        self.assertEqual(plan.recovery_reason, "sequence_gap")
        self.assertEqual(
            plan.query_order_chunks,
            (("O-1", "O-2", "O-3"),),
        )
        self.assertEqual(
            plan.evidence_refs,
            (
                acknowledgement.subscription_binding.evidence_ref,
                acknowledgement.evidence_ref,
                snapshot.evidence_ref,
                update.evidence_ref,
                gap.evidence_ref,
            ),
        )
        self.assertFalse(plan.trading_ready)

    def test_rest_crosscheck_plan_is_unavailable_before_snapshot_evidence(self):
        recovery = self.make_recovery()
        recovery.begin_connection()
        with self.assertRaisesRegex(
            KrakenSpotStreamError,
            "requires stream recovery evidence",
        ):
            recovery.rest_crosscheck_plan()
        self.acknowledge(recovery)
        with self.assertRaisesRegex(
            KrakenSpotStreamError,
            "requires stream recovery evidence",
        ):
            recovery.rest_crosscheck_plan()

    def test_buffer_bound_fails_closed_instead_of_dropping_updates(self):
        recovery = self.make_recovery(max_buffered_updates=1)
        recovery.begin_connection()
        self.acknowledge(recovery)
        recovery.apply_frame(
            self.parse(recovery=recovery, frame_type="snapshot", sequence=100)
        )
        recovery.apply_frame(
            self.parse(recovery=recovery, frame_type="update", sequence=101)
        )
        overflow = self.parse(recovery=recovery, frame_type="update", sequence=102)
        recovery.apply_frame(overflow)
        evidence = recovery.evidence()
        self.assertEqual(
            evidence.phase,
            recovery.GAP_RECONCILIATION_REQUIRED,
        )
        self.assertEqual(evidence.gap_expected_sequence, 102)
        self.assertEqual(evidence.gap_observed_sequence, 102)
        self.assertEqual(evidence.gap_evidence_ref, overflow.evidence_ref)
        self.assertEqual(evidence.buffered_update_evidence_refs, ())
        self.assertEqual(evidence.recovery_reason, "buffer_exhausted")
        self.assertFalse(evidence.trading_ready)


if __name__ == "__main__":
    unittest.main()
