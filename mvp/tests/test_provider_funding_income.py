"""Bybit funding-income semantics without synthetic DIRECT_PROVIDER_WIRE minting.

The pure row parser is intentionally non-authoritative.  Positive provider-origin
issuance is exercised only when a genuine ProviderOriginObservation exists; unit
tests must not monkey-patch the canonical direct network opener or seed a fake
wire claim and then relabel that state as provider execution.
"""

import json
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import patch

import mvp.autotrade_mvp.provider_funding_income as funding_income_module

from mvp.autotrade_mvp.provider_core import _decode_exact_json
from mvp.autotrade_mvp.provider_funding_income import (
    ProviderFundingIncomeError,
    ProviderFundingIncomeObservation,
    _parse_bybit_funding_income_rows,
    bybit_funding_income_observations,
)


class ProviderFundingIncomeParserTests(unittest.TestCase):
    @staticmethod
    def _query(**overrides):
        query = {
            "accountType": "UNIFIED",
            "category": "linear",
            "type": "SETTLEMENT",
        }
        query.update(overrides)
        return query

    @staticmethod
    def _payload(
        *,
        duplicate=False,
        ret_code=0,
        transaction_time="1672128000000",
        currency="USDT",
        category="linear",
        side="Buy",
        funding="-0.003676",
        fee="0.00000000",
        cash_flow="0",
        change="-0.003676",
        include_trade=False,
    ):
        settlement = {
            "transSubType": "",
            "id": "592324_XRPUSDT_161440249321",
            "symbol": "XRPUSDT",
            "side": side,
            "funding": funding,
            "orderLinkId": "",
            "orderId": "1672128000-8-592324-1-2",
            "fee": fee,
            "change": change,
            "cashFlow": cash_flow,
            "transactionTime": transaction_time,
            "type": "SETTLEMENT",
            "feeRate": "0.0001",
            "bonusChange": "",
            "size": "100",
            "qty": "100",
            "cashBalance": "5086.55825002",
            "currency": currency,
            "category": category,
            "tradePrice": "0.3676",
            "tradeId": "534c0003-4bf7-486f-aa02-78cee36825e4",
            "extraFees": "",
        }
        rows = [settlement]
        if include_trade:
            trade = dict(settlement)
            trade.update(
                {
                    "id": "trade-row",
                    "type": "TRADE",
                    "funding": "",
                    "transactionTime": "1672121182224",
                }
            )
            rows.append(trade)
        if duplicate:
            rows.append(dict(settlement))
        return {
            "retCode": ret_code,
            "retMsg": "OK" if type(ret_code) is int and ret_code == 0 else "ERROR",
            "result": {"nextPageCursor": "", "list": rows},
            "retExtInfo": {},
            "time": 1672132481405,
        }

    def _parse(
        self,
        *,
        payload=None,
        query=None,
        observed_at="2026-10-04T11:00:00Z",
    ):
        return _parse_bybit_funding_income_rows(
            self._payload() if payload is None else payload,
            self._query() if query is None else query,
            observed_at,
        )

    def test_parser_projects_one_funding_row(self):
        rows = self._parse()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.provider_transaction_id, "592324_XRPUSDT_161440249321")
        self.assertEqual(row.instrument_id, "XRPUSDT")
        self.assertEqual(row.product_category, "linear")
        self.assertEqual(row.settlement_currency, "USDT")
        self.assertEqual(row.side, "Buy")
        self.assertEqual(row.funding_amount, Decimal("-0.003676"))
        self.assertEqual(row.transaction_time_ms, "1672128000000")
        self.assertNotIsInstance(row, ProviderFundingIncomeObservation)

    def test_nonsettlement_row_escaped_qualified_type_fails_closed(self):
        with self.assertRaisesRegex(
            ProviderFundingIncomeError,
            "qualified transaction type",
        ):
            self._parse(payload=self._payload(include_trade=True))

    def test_parser_accepts_canonical_frozen_provider_payload_shape(self):
        payload = _decode_exact_json(
            json.dumps(
                self._payload(),
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        self.assertIsNot(type(payload), dict)
        self.assertIsInstance(payload["result"]["list"], tuple)
        rows = self._parse(payload=payload)
        self.assertEqual(len(rows), 1)
        self.assertEqual(
            rows[0].provider_transaction_id,
            "592324_XRPUSDT_161440249321",
        )

    def test_parser_preserves_exact_millisecond_without_float(self):
        rows = self._parse(
            payload=self._payload(transaction_time="1672128000001")
        )
        self.assertEqual(
            rows[0].provider_transaction_at.isoformat(),
            "2022-12-27T08:00:00.001000+00:00",
        )

    def test_epoch_conversion_does_not_dispatch_host_timestamp_authority(self):
        class HostileDateTime:
            calls = 0

            @classmethod
            def fromtimestamp(cls, *_args, **_kwargs):
                cls.calls += 1
                raise AssertionError("host time_t conversion executed")

        with patch(
            "mvp.autotrade_mvp.provider_funding_income.datetime",
            HostileDateTime,
        ):
            instant = funding_income_module._datetime_from_epoch_millis(
                1_672_128_000_001,
                name="transactionTime",
            )
        self.assertEqual(HostileDateTime.calls, 0)
        self.assertEqual(
            instant,
            datetime(2022, 12, 27, 8, 0, 0, 1000, tzinfo=timezone.utc),
        )

    def test_epoch_conversion_preserves_exact_utc_upper_boundary(self):
        self.assertEqual(
            funding_income_module._datetime_from_epoch_millis(
                253402300799999,
                name="transactionTime",
            ),
            datetime(9999, 12, 31, 23, 59, 59, 999000, tzinfo=timezone.utc),
        )
        with self.assertRaisesRegex(
            ProviderFundingIncomeError,
            "outside supported UTC range",
        ):
            funding_income_module._datetime_from_epoch_millis(
                253402300800000,
                name="transactionTime",
            )

    def test_epoch_millisecond_token_is_bounded_before_integer_materialization(self):
        with self.assertRaisesRegex(
            ProviderFundingIncomeError,
            "bounded epoch-millisecond",
        ):
            self._parse(
                payload=self._payload(transaction_time="9" * 257)
            )

    def test_transaction_after_response_observation_fails_closed(self):
        with self.assertRaisesRegex(
            ProviderFundingIncomeError,
            "after provider response observation",
        ):
            self._parse(
                payload=self._payload(transaction_time="1672128000001"),
                observed_at="2022-12-27T08:00:00Z",
            )

    def test_response_observation_time_must_be_canonical_utc(self):
        for observed_at in ("2026-10-04T11:00:00+00:00", "not-a-time"):
            with self.subTest(observed_at=observed_at), self.assertRaisesRegex(
                ProviderFundingIncomeError,
                "observed_at must be canonical UTC",
            ):
                self._parse(observed_at=observed_at)

    def test_noncanonical_leading_zero_transaction_time_fails_closed(self):
        with self.assertRaisesRegex(
            ProviderFundingIncomeError,
            "canonical epoch-millisecond",
        ):
            self._parse(
                payload=self._payload(transaction_time="01672128000000")
            )

    def test_unvalidated_semantic_query_filters_fail_closed(self):
        for filter_name, value in (
            ("baseCoin", "BTC"),
            ("transSubType", "movePosition"),
        ):
            with self.subTest(filter_name=filter_name), self.assertRaisesRegex(
                ProviderFundingIncomeError,
                f"filter {filter_name} is not supported",
            ):
                self._parse(query=self._query(**{filter_name: value}))

    def test_qualified_currency_scope_mismatch_fails_closed(self):
        with self.assertRaisesRegex(
            ProviderFundingIncomeError,
            "qualified currency scope",
        ):
            self._parse(
                payload=self._payload(currency="USDC"),
                query=self._query(currency="USDT"),
            )

    def test_lowercase_provider_currency_is_not_silently_canonicalized(self):
        with self.assertRaisesRegex(
            ProviderFundingIncomeError,
            "canonical uppercase ASCII",
        ):
            self._parse(payload=self._payload(currency="usdt"))

    def test_lowercase_qualified_currency_is_rejected(self):
        with self.assertRaisesRegex(
            ProviderFundingIncomeError,
            "qualified query currency",
        ):
            self._parse(query=self._query(currency="usdt"))

    def test_signed_funding_cash_is_not_inverted_by_position_side(self):
        for side in ("Buy", "Sell", "None"):
            with self.subTest(side=side):
                rows = self._parse(
                    payload=self._payload(
                        side=side,
                        funding="0.125",
                        fee="0",
                        cash_flow="0",
                        change="0.125",
                    )
                )
                self.assertEqual(rows[0].funding_amount, Decimal("0.125"))

    def test_session_cash_flow_is_not_folded_into_funding_amount(self):
        rows = self._parse(
            payload=self._payload(
                funding="-0.003",
                fee="0",
                cash_flow="5.25",
                change="5.247",
            )
        )
        row = rows[0]
        self.assertEqual(row.funding_amount, Decimal("-0.003"))
        self.assertFalse(hasattr(row, "cash_flow"))
        self.assertFalse(hasattr(row, "funding_rate"))

    def test_negative_fee_rebate_obeys_provider_change_equation(self):
        rows = self._parse(
            payload=self._payload(
                funding="0",
                fee="-0.10",
                cash_flow="0",
                change="0.10",
            )
        )
        self.assertEqual(rows[0].funding_amount, Decimal("0"))

    def test_actual_null_side_is_not_provider_none_side(self):
        with self.assertRaisesRegex(
            ProviderFundingIncomeError,
            "side must be exact non-empty text",
        ):
            self._parse(payload=self._payload(side=None))

    def test_funding_row_change_equation_must_reconcile_exactly(self):
        with self.assertRaisesRegex(
            ProviderFundingIncomeError,
            r"cashFlow \+ funding - fee",
        ):
            self._parse(payload=self._payload(change="-0.003675"))

    def test_change_equation_uses_exact_decimal_arithmetic(self):
        rows = self._parse(
            payload=self._payload(
                funding="0.0000000000000000003",
                fee="0.0000000000000000001",
                cash_flow="1000000000000000000.0000000000000000002",
                change="1000000000000000000.0000000000000000004",
            )
        )
        self.assertEqual(
            rows[0].funding_amount,
            Decimal("0.0000000000000000003"),
        )

    def test_boolean_ret_code_cannot_masquerade_as_success(self):
        with self.assertRaisesRegex(
            ProviderFundingIncomeError,
            "not successful",
        ):
            self._parse(payload=self._payload(ret_code=False))

    def test_non_success_provider_envelope_fails_closed(self):
        with self.assertRaisesRegex(
            ProviderFundingIncomeError,
            "not successful",
        ):
            self._parse(payload=self._payload(ret_code=10001))

    def test_settlement_row_wrong_category_fails_closed(self):
        with self.assertRaisesRegex(
            ProviderFundingIncomeError,
            "qualified derivative category",
        ):
            self._parse(payload=self._payload(category="inverse"))

    def test_invalid_side_fails_closed(self):
        with self.assertRaisesRegex(
            ProviderFundingIncomeError,
            "side is not canonical",
        ):
            self._parse(payload=self._payload(side="BUY"))

    def test_duplicate_provider_transaction_id_fails_closed(self):
        with self.assertRaisesRegex(
            ProviderFundingIncomeError,
            "duplicated",
        ):
            self._parse(payload=self._payload(duplicate=True))

    def test_duplicate_id_hidden_by_empty_funding_row_fails_closed(self):
        payload = self._payload(include_trade=False)
        funded = payload["result"]["list"][0]
        empty = dict(funded)
        empty["funding"] = ""
        empty["fee"] = "0"
        empty["cashFlow"] = "0"
        empty["change"] = "0"
        payload["result"]["list"] = [empty, funded]
        with self.assertRaisesRegex(
            ProviderFundingIncomeError,
            "duplicated",
        ):
            self._parse(payload=payload)

    def test_funding_row_before_qualified_start_time_fails_closed(self):
        with self.assertRaisesRegex(
            ProviderFundingIncomeError,
            "qualified startTime",
        ):
            self._parse(
                payload=self._payload(transaction_time="1672128000000"),
                query=self._query(startTime="1672128000001"),
            )

    def test_funding_row_after_qualified_end_time_fails_closed(self):
        with self.assertRaisesRegex(
            ProviderFundingIncomeError,
            "qualified endTime",
        ):
            self._parse(
                payload=self._payload(transaction_time="1672128000001"),
                query=self._query(endTime="1672128000000"),
            )

    def test_noncanonical_qualified_start_time_is_rejected(self):
        with self.assertRaisesRegex(
            ProviderFundingIncomeError,
            "qualified query startTime.*canonical",
        ):
            self._parse(query=self._query(startTime="01672128000000"))

    def test_start_only_window_enforces_bybit_24_hour_implicit_end(self):
        start = 1672128000000
        with self.assertRaisesRegex(
            ProviderFundingIncomeError,
            "qualified endTime",
        ):
            self._parse(
                payload=self._payload(
                    transaction_time=str(start + 24 * 60 * 60 * 1000 + 1)
                ),
                query=self._query(startTime=str(start)),
            )

    def test_end_only_window_enforces_bybit_24_hour_implicit_start(self):
        end = 1672214400000
        with self.assertRaisesRegex(
            ProviderFundingIncomeError,
            "qualified startTime",
        ):
            self._parse(
                payload=self._payload(
                    transaction_time=str(end - 24 * 60 * 60 * 1000 - 1)
                ),
                query=self._query(endTime=str(end)),
            )

    def test_qualified_time_window_over_seven_days_is_rejected(self):
        start = 1672128000000
        end = start + 7 * 24 * 60 * 60 * 1000 + 1
        with self.assertRaisesRegex(
            ProviderFundingIncomeError,
            "seven-day range",
        ):
            self._parse(
                query=self._query(
                    startTime=str(start),
                    endTime=str(end),
                )
            )

    def test_inverted_qualified_time_window_is_rejected(self):
        with self.assertRaisesRegex(
            ProviderFundingIncomeError,
            "endTime precedes startTime",
        ):
            self._parse(
                query=self._query(
                    startTime="1672128000001",
                    endTime="1672128000000",
                )
            )

    def test_empty_funding_session_settlement_is_not_projected_as_funding_cash(self):
        rows = self._parse(
            payload=self._payload(
                funding="",
                fee="0",
                cash_flow="7.5",
                change="7.5",
            )
        )
        self.assertEqual(rows, ())

    def test_missing_exact_decimal_component_fails_closed(self):
        payload = self._payload()
        del payload["result"]["list"][0]["fee"]
        with self.assertRaisesRegex(
            ProviderFundingIncomeError,
            "fee must be exact decimal text",
        ):
            self._parse(payload=payload)

    def test_float_decimal_component_fails_closed(self):
        payload = self._payload()
        payload["result"]["list"][0]["funding"] = -0.003676
        with self.assertRaisesRegex(
            ProviderFundingIncomeError,
            "funding must be exact decimal text",
        ):
            self._parse(payload=payload)

    def test_query_must_be_exact_narrow_settlement_domain(self):
        for query in (
            self._query(accountType="CONTRACT"),
            self._query(type="TRADE"),
            self._query(category="spot"),
        ):
            with self.subTest(query=query), self.assertRaisesRegex(
                ProviderFundingIncomeError,
                "not narrowed to one derivative settlement domain",
            ):
                self._parse(query=query)

    def test_malformed_response_shape_fails_closed(self):
        for mutation in ("list", "cursor"):
            payload = self._payload()
            if mutation == "list":
                payload["result"]["list"] = {}
            else:
                payload["result"]["nextPageCursor"] = None
            with self.subTest(mutation=mutation), self.assertRaisesRegex(
                ProviderFundingIncomeError,
                "result shape is invalid",
            ):
                self._parse(payload=payload)

    def test_authoritative_observation_constructor_remains_sealed(self):
        with self.assertRaisesRegex(
            ProviderFundingIncomeError,
            "must come from qualified provider-origin bytes",
        ):
            ProviderFundingIncomeObservation()

    def test_public_authority_entrypoint_rejects_non_origin_objects(self):
        with self.assertRaisesRegex(
            ProviderFundingIncomeError,
            "exact ProviderOriginObservation",
        ):
            bybit_funding_income_observations(object())


if __name__ == "__main__":
    unittest.main()
