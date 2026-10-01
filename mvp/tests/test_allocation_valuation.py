from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, localcontext
import unittest

from mvp.autotrade_mvp.allocation_valuation import (
    AllocationValuationError,
    normalize_allocation_valuation,
)
from mvp.autotrade_mvp.fx_valuation import FxRoundingPolicy


class AllocationValuationBoundaryTests(unittest.TestCase):
    DECISION_TIME = "2026-09-25T18:30:00Z"

    def test_numeric_ingress_is_bounded_before_valuation_identity(self):
        class HostileMoney(Decimal):
            def is_finite(self):
                raise AssertionError("caller numeric callback executed")

        for value in ("1e999999999", "9" * 257, Decimal("1e-257")):
            with self.subTest(value=repr(value)), self.assertRaises(AllocationValuationError):
                self.normalize(self.market(), self.valuation(), source_price=value)
        with self.assertRaises(TypeError):
            self.normalize(self.market(), self.valuation(), source_price=HostileMoney("10"))

    def market(
        self,
        *,
        asset_class="CASH_EQUITY",
        payoff="LINEAR",
        multiplier="1",
        quote_currency="USD",
        settlement_currency=None,
    ):
        return {
            "instrument_version": "instrument:test:v1",
            "capability_snapshot_id": "capability:test:v1",
            "asset_class": asset_class,
            "payoff": payoff,
            "quantity_unit": "CONTRACT" if asset_class in {"FUTURE", "PERPETUAL"} else "SHARE",
            "contract_multiplier": multiplier,
            "quote_currency": quote_currency,
            "settlement_currency": settlement_currency or quote_currency,
        }

    def valuation(
        self,
        *,
        asset_class="CASH_EQUITY",
        payoff="LINEAR",
        multiplier="1",
        quote_currency="USD",
        settlement_currency=None,
        source_price="10",
        portfolio_base_currency="USD",
        unit_base_notional="10",
        fx_rate="1",
        fx_rate_numerator=None,
        fx_rate_denominator=None,
        fx_source_id="IDENTITY",
        fx_quote=None,
        fx_evidence_sha256=None,
        cost_rate_components=None,
        cost_evidence_refs=None,
    ):
        payload = {
            "symbol": "AAA",
            "instrument_version": "instrument:test:v1",
            "capability_snapshot_id": "capability:test:v1",
            "asset_class": asset_class,
            "payoff": payoff,
            "quantity_unit": "CONTRACT" if asset_class in {"FUTURE", "PERPETUAL"} else "SHARE",
            "contract_multiplier": multiplier,
            "quote_currency": quote_currency,
            "settlement_currency": settlement_currency or quote_currency,
            "source_price": source_price,
            "portfolio_base_currency": portfolio_base_currency,
            "fx_rate": fx_rate,
            "fx_source_id": fx_source_id,
            "unit_base_notional": unit_base_notional,
            "capital_requirement_rate": "1",
            "min_notional_base": "0",
            "fee_floor_base": "0",
            "max_executable_notional_base": "10000",
            "payoff_identity": f"{asset_class.lower()}:{payoff.lower()}:v1",
            "cost_rate_components": cost_rate_components or {
                "execution": "0.001",
                "financing": "0",
                "funding": "0",
                "borrow": "0",
                "fx": "0",
            },
            "cost_evidence_refs": cost_evidence_refs or {
                "execution": "execution:test:v1",
                "financing": "financing:none:test:v1",
                "funding": "funding:none:test:v1",
                "borrow": "borrow:none:test:v1",
                "fx": "fx:identity:test:v1",
            },
        }
        if fx_rate_numerator is not None:
            payload["fx_rate_numerator"] = fx_rate_numerator
        if fx_rate_denominator is not None:
            payload["fx_rate_denominator"] = fx_rate_denominator
        if fx_quote is not None:
            payload["fx_quote"] = fx_quote
        if fx_evidence_sha256 is not None:
            payload["fx_evidence_sha256"] = fx_evidence_sha256
        return payload

    def normalize(self, market, valuation, *, source_price="10", base="USD", cost_rate="0.001"):
        return normalize_allocation_valuation(
            symbol="AAA",
            market_payload=market,
            valuation_payload=valuation,
            source_price=source_price,
            expected_cost_rate=cost_rate,
            expected_capital_requirement_rate="1",
            expected_min_notional_base="0",
            expected_fee_floor_base="0",
            expected_max_executable_notional_base="10000",
            decision_time=self.DECISION_TIME,
            portfolio_base_currency=base,
        )

    def test_single_currency_cash_equity_is_numerically_unchanged(self):
        result = self.normalize(self.market(), self.valuation())
        self.assertEqual(result.unit_base_notional, 10)
        self.assertEqual(result.fx_rate, 1)
        self.assertEqual((result.fx_rate_numerator, result.fx_rate_denominator), (1, 1))
        self.assertEqual(result.fx_source_id, "IDENTITY")
        self.assertEqual(result.portfolio_base_currency, "USD")

    def test_high_significance_notional_and_cost_sum_ignore_ambient_context(self):
        source_price = "12345678901234567890.123456789"
        cost_components = {
            "execution": "0.1234567890123456789012345678",
            "financing": "0.0000000000000000000000000001",
            "funding": "0",
            "borrow": "0",
            "fx": "0",
        }
        expected_cost = "0.1234567890123456789012345679"
        observed = []
        for precision in (6, 10, 28, 80):
            for rounding in (ROUND_FLOOR, ROUND_CEILING):
                with localcontext() as context:
                    context.prec = precision
                    context.rounding = rounding
                    result = self.normalize(
                        self.market(),
                        self.valuation(
                            source_price=source_price,
                            unit_base_notional=source_price,
                            cost_rate_components=cost_components,
                        ),
                        source_price=source_price,
                        cost_rate=expected_cost,
                    )
                    observed.append(result.unit_base_notional)
        self.assertTrue(all(value == Decimal(source_price) for value in observed))

    def test_cross_currency_requires_fresh_exact_fx_evidence(self):
        digest = "sha256:" + "a" * 64
        quote = {
            "base_currency": "EUR",
            "quote_currency": "USD",
            "bid": "1.10",
            "ask": "1.11",
            "available_at": "2026-09-25T18:29:30Z",
            "source_id": "fx:eurusd:venue:v7",
            "evidence_sha256": digest,
            "max_age_seconds": 60,
            "haircut": "0",
        }
        result = self.normalize(
            self.market(quote_currency="EUR"),
            self.valuation(
                quote_currency="EUR",
                portfolio_base_currency="USD",
                unit_base_notional="11.00",
                fx_rate="1.10",
                fx_source_id="fx:eurusd:venue:v7",
                fx_quote=quote,
                fx_evidence_sha256=digest,
                cost_evidence_refs={
                    "execution": "execution:test:v1",
                    "financing": "financing:none:test:v1",
                    "funding": "funding:none:test:v1",
                    "borrow": "borrow:none:test:v1",
                    "fx": "fx:eurusd:venue:v7",
                },
            ),
        )
        self.assertEqual(result.unit_base_notional, 11)
        self.assertEqual((result.fx_rate_numerator, result.fx_rate_denominator), (11, 10))
        self.assertEqual(result.fx_evidence_sha256, digest)

        missing = self.valuation(
            quote_currency="EUR",
            portfolio_base_currency="USD",
            unit_base_notional="11",
            fx_rate="1.10",
            fx_source_id="fx:eurusd:venue:v7",
        )
        with self.assertRaisesRegex(AllocationValuationError, "fx_quote"):
            self.normalize(self.market(quote_currency="EUR"), missing)

        stale_quote = {**quote, "available_at": "2026-09-25T18:20:00Z"}
        stale = self.valuation(
            quote_currency="EUR",
            portfolio_base_currency="USD",
            unit_base_notional="11",
            fx_rate="1.10",
            fx_source_id="fx:eurusd:venue:v7",
            fx_quote=stale_quote,
            fx_evidence_sha256=digest,
        )
        with self.assertRaisesRegex(AllocationValuationError, "not allocatable: STALE"):
            self.normalize(self.market(quote_currency="EUR"), stale)

    def test_inverse_fx_exact_identity_survives_allocation_without_fabricated_decimal_rate(self):
        digest = "sha256:" + "b" * 64
        quote = {
            "base_currency": "EUR",
            "quote_currency": "USD",
            "bid": "1.1000",
            "ask": "1.1002",
            "available_at": "2026-09-25T18:29:30Z",
            "source_id": "fx:eurusd:venue:v8",
            "evidence_sha256": digest,
            "max_age_seconds": 60,
            "haircut": "0",
        }
        observed = []
        for precision in (6, 10, 28, 80):
            for rounding in (ROUND_FLOOR, ROUND_CEILING):
                with localcontext() as context:
                    context.prec = precision
                    context.rounding = rounding
                    result = self.normalize(
                        self.market(quote_currency="USD"),
                        self.valuation(
                            quote_currency="USD",
                            portfolio_base_currency="EUR",
                            source_price="110.02",
                            unit_base_notional="100",
                            fx_rate=None,
                            fx_rate_numerator=5000,
                            fx_rate_denominator=5501,
                            fx_source_id="fx:eurusd:venue:v8",
                            fx_quote=quote,
                            fx_evidence_sha256=digest,
                            cost_evidence_refs={
                                "execution": "execution:test:v1",
                                "financing": "financing:none:test:v1",
                                "funding": "funding:none:test:v1",
                                "borrow": "borrow:none:test:v1",
                                "fx": "fx:eurusd:venue:v8",
                            },
                        ),
                        source_price="110.02",
                        base="EUR",
                    )
                    observed.append(
                        (
                            result.unit_base_notional,
                            result.fx_rate,
                            result.fx_rate_numerator,
                            result.fx_rate_denominator,
                            result.fx_source_id,
                            result.fx_evidence_sha256,
                        )
                    )
        expected = (Decimal("100"), None, 5000, 5501, "fx:eurusd:venue:v8", digest)
        self.assertTrue(all(value == expected for value in observed))

    def test_inverse_fx_can_bind_rounding_policy_even_when_unit_amount_terminates(self):
        digest = "sha256:" + "b" * 64
        quote = {
            "base_currency": "EUR",
            "quote_currency": "USD",
            "bid": "1.1000",
            "ask": "1.1002",
            "available_at": "2026-09-25T18:29:30Z",
            "source_id": "fx:eurusd:venue:v8",
            "evidence_sha256": digest,
            "max_age_seconds": 60,
            "haircut": "0",
        }
        policy = FxRoundingPolicy(
            reporting_currency="EUR",
            quantum=Decimal("0.01"),
        )
        valuation = self.valuation(
            quote_currency="USD",
            portfolio_base_currency="EUR",
            source_price="110.02",
            unit_base_notional="100",
            fx_rate=None,
            fx_rate_numerator=5000,
            fx_rate_denominator=5501,
            fx_source_id="fx:eurusd:venue:v8",
            fx_quote=quote,
            fx_evidence_sha256=digest,
            cost_evidence_refs={
                "execution": "execution:test:v1",
                "financing": "financing:none:test:v1",
                "funding": "funding:none:test:v1",
                "borrow": "borrow:none:test:v1",
                "fx": "fx:eurusd:venue:v8",
            },
        )
        valuation["fx_rounding_policy_id"] = policy.policy_id
        valuation["fx_rounding_quantum"] = "0.01"

        result = self.normalize(
            self.market(quote_currency="USD"),
            valuation,
            source_price="110.02",
            base="EUR",
        )

        self.assertEqual(result.unit_base_notional, Decimal("100"))
        self.assertIsNone(result.fx_rate)
        self.assertEqual(
            (result.fx_rate_numerator, result.fx_rate_denominator),
            (5000, 5501),
        )
        self.assertEqual(result.fx_rounding_policy_id, policy.policy_id)
        self.assertEqual(result.fx_rounding_quantum, Decimal("0.01"))

        bad = dict(valuation)
        bad["fx_rounding_policy_id"] = "fx-rounding:sha256:" + "0" * 64
        with self.assertRaisesRegex(
            AllocationValuationError,
            "rounding policy identity mismatch",
        ):
            self.normalize(
                self.market(quote_currency="USD"),
                bad,
                source_price="110.02",
                base="EUR",
            )

    def test_inverse_fx_rejects_caller_fabricated_rational_identity(self):
        digest = "sha256:" + "b" * 64
        quote = {
            "base_currency": "EUR",
            "quote_currency": "USD",
            "bid": "1.1000",
            "ask": "1.1002",
            "available_at": "2026-09-25T18:29:30Z",
            "source_id": "fx:eurusd:venue:v8",
            "evidence_sha256": digest,
            "max_age_seconds": 60,
            "haircut": "0",
        }
        with self.assertRaisesRegex(AllocationValuationError, "exact FX rate identity mismatch"):
            self.normalize(
                self.market(quote_currency="USD"),
                self.valuation(
                    quote_currency="USD",
                    portfolio_base_currency="EUR",
                    source_price="110.02",
                    unit_base_notional="100",
                    fx_rate=None,
                    fx_rate_numerator=1,
                    fx_rate_denominator=1,
                    fx_source_id="fx:eurusd:venue:v8",
                    fx_quote=quote,
                    fx_evidence_sha256=digest,
                ),
                source_price="110.02",
                base="EUR",
            )

    def test_linear_future_includes_exact_contract_multiplier(self):
        market = self.market(asset_class="FUTURE", multiplier="50")
        valuation = self.valuation(
            asset_class="FUTURE",
            multiplier="50",
            source_price="100",
            unit_base_notional="5000",
        )
        result = self.normalize(
            market,
            valuation,
            source_price="100",
        )
        self.assertEqual(result.unit_base_notional, 5000)
        self.assertEqual(result.contract_multiplier, 50)

    def test_inverse_contract_is_never_silently_treated_as_linear(self):
        with self.assertRaisesRegex(AllocationValuationError, "canonical payoff boundary"):
            self.normalize(
                self.market(asset_class="FUTURE", payoff="INVERSE", multiplier="100"),
                self.valuation(
                    asset_class="FUTURE",
                    payoff="INVERSE",
                    multiplier="100",
                    unit_base_notional="1000",
                ),
            )

    def test_option_requires_canonical_nonlinear_economic_boundary(self):
        with self.assertRaisesRegex(AllocationValuationError, "nonlinear payoff boundary"):
            self.normalize(
                self.market(asset_class="OPTION", multiplier="100"),
                self.valuation(
                    asset_class="OPTION",
                    multiplier="100",
                    unit_base_notional="1000",
                ),
            )

    def test_cost_components_are_explicit_and_binary_float_is_rejected(self):
        with self.assertRaisesRegex(AllocationValuationError, "cost_rate_components"):
            bad_components = self.valuation(
                cost_rate_components={
                    "execution": "0.001",
                    "financing": "0",
                    "funding": "0",
                    "borrow": "0",
                }
            )
            self.normalize(self.market(), bad_components)

        with self.assertRaises(TypeError):
            self.normalize(self.market(), self.valuation(), source_price=10.0)

        bad_digest_quote = {
            "base_currency": "EUR",
            "quote_currency": "USD",
            "bid": "1.10",
            "ask": "1.11",
            "available_at": "2026-09-25T18:29:30Z",
            "source_id": "fx:eurusd:venue:v7",
            "evidence_sha256": "a" * 64,
            "max_age_seconds": 60,
            "haircut": "0",
        }
        with self.assertRaisesRegex(AllocationValuationError, "canonical sha256"):
            self.normalize(
                self.market(quote_currency="EUR"),
                self.valuation(
                    quote_currency="EUR",
                    portfolio_base_currency="USD",
                    unit_base_notional="11",
                    fx_rate="1.10",
                    fx_source_id="fx:eurusd:venue:v7",
                    fx_quote=bad_digest_quote,
                    fx_evidence_sha256="a" * 64,
                ),
            )


if __name__ == "__main__":
    unittest.main()
