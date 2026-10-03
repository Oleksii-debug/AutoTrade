from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, localcontext
from types import MappingProxyType
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
        fx_rounding_policy_id=None,
        fx_rounding_quantum=None,
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
        if fx_rounding_policy_id is not None:
            payload["fx_rounding_policy_id"] = fx_rounding_policy_id
        if fx_rounding_quantum is not None:
            payload["fx_rounding_quantum"] = fx_rounding_quantum
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
        self.assertEqual(result.unit_base_liability_notional, Decimal("11.1"))
        self.assertEqual((result.fx_rate_numerator, result.fx_rate_denominator), (11, 10))
        self.assertEqual(
            (
                result.fx_liability_rate_numerator,
                result.fx_liability_rate_denominator,
            ),
            (111, 100),
        )
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
        rounding_policy = FxRoundingPolicy(
            reporting_currency="EUR",
            quantum=Decimal("0.01"),
        )
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
                            fx_rounding_policy_id=rounding_policy.policy_id,
                            fx_rounding_quantum=rounding_policy.quantum,
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
                            result.unit_base_liability_notional,
                            result.fx_rate,
                            result.fx_rate_numerator,
                            result.fx_rate_denominator,
                            result.fx_liability_rate_numerator,
                            result.fx_liability_rate_denominator,
                            result.fx_source_id,
                            result.fx_evidence_sha256,
                        )
                    )
        expected = (
            Decimal("100"),
            Decimal("100.02"),
            None,
            5000,
            5501,
            10,
            11,
            "fx:eurusd:venue:v8",
            digest,
        )
        self.assertTrue(all(value == expected for value in observed))

    def test_inverse_fx_rejects_rounded_unit_price_for_quantity_feasibility(self):
        digest = "sha256:" + "d" * 64
        quote = {
            "base_currency": "EUR",
            "quote_currency": "USD",
            "bid": "3",
            "ask": "3",
            "available_at": "2026-09-25T18:29:30Z",
            "source_id": "fx:eurusd:venue:one-third",
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
            source_price="1",
            unit_base_notional="0.33",
            fx_rate=None,
            fx_rate_numerator=1,
            fx_rate_denominator=3,
            fx_source_id="fx:eurusd:venue:one-third",
            fx_quote=quote,
            fx_evidence_sha256=digest,
            cost_evidence_refs={
                "execution": "execution:test:v1",
                "financing": "financing:none:test:v1",
                "funding": "funding:none:test:v1",
                "borrow": "borrow:none:test:v1",
                "fx": "fx:eurusd:venue:one-third",
            },
        )
        valuation["fx_rounding_policy_id"] = policy.policy_id
        valuation["fx_rounding_quantum"] = "0.01"

        with self.assertRaisesRegex(
            AllocationValuationError,
            "unit FX conversion must terminate exactly",
        ):
            self.normalize(
                self.market(quote_currency="USD"),
                valuation,
                source_price="1",
                base="EUR",
            )

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
        self.assertEqual(
            (
                result.fx_liability_rate_numerator,
                result.fx_liability_rate_denominator,
            ),
            (10, 11),
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

    def test_inverse_fx_liability_side_requires_rounding_policy_when_asset_unit_terminates(self):
        digest = "sha256:" + "c" * 64
        quote = {
            "base_currency": "EUR",
            "quote_currency": "USD",
            "bid": "1.10",
            "ask": "1.20",
            "available_at": "2026-09-25T18:29:30Z",
            "source_id": "fx:eurusd:venue:side-boundary",
            "evidence_sha256": digest,
            "max_age_seconds": 60,
            "haircut": "0",
        }
        with self.assertRaisesRegex(
            AllocationValuationError,
            "liability FX valuation is not allocatable: ROUNDING_POLICY_REQUIRED",
        ):
            self.normalize(
                self.market(quote_currency="USD"),
                self.valuation(
                    quote_currency="USD",
                    portfolio_base_currency="EUR",
                    source_price="6",
                    unit_base_notional="5",
                    fx_rate=None,
                    fx_rate_numerator=5,
                    fx_rate_denominator=6,
                    fx_source_id="fx:eurusd:venue:side-boundary",
                    fx_quote=quote,
                    fx_evidence_sha256=digest,
                    cost_evidence_refs={
                        "execution": "execution:test:v1",
                        "financing": "financing:none:test:v1",
                        "funding": "funding:none:test:v1",
                        "borrow": "borrow:none:test:v1",
                        "fx": "fx:eurusd:venue:side-boundary",
                    },
                ),
                source_price="6",
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
        policy = FxRoundingPolicy(
            reporting_currency="EUR",
            quantum=Decimal("0.01"),
        )
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
                    fx_rounding_policy_id=policy.policy_id,
                    fx_rounding_quantum=policy.quantum,
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


    def test_text_subclass_is_rejected_before_string_dispatch(self):
        touched = []

        class HostileText(str):
            def strip(self, *args, **kwargs):
                touched.append("strip")
                raise AssertionError("hostile text strip")

            def upper(self, *args, **kwargs):
                touched.append("upper")
                raise AssertionError("hostile text upper")

        with self.assertRaisesRegex(AllocationValuationError, "allocation symbol"):
            normalize_allocation_valuation(
                symbol=HostileText("AAA"),
                market_payload=self.market(),
                valuation_payload=self.valuation(),
                source_price="10",
                expected_cost_rate="0.001",
                expected_capital_requirement_rate="1",
                expected_min_notional_base="0",
                expected_fee_floor_base="0",
                expected_max_executable_notional_base="10000",
                decision_time=self.DECISION_TIME,
                portfolio_base_currency="USD",
            )

        self.assertEqual(touched, [])

    def test_mapping_subclasses_are_rejected_before_callbacks(self):
        touched = []

        class HostileDict(dict):
            def __iter__(self):
                touched.append("iter")
                raise AssertionError("hostile mapping iteration")

            def get(self, *args, **kwargs):
                touched.append("get")
                raise AssertionError("hostile mapping get")

            def __contains__(self, key):
                touched.append("contains")
                raise AssertionError("hostile mapping contains")

        for market, valuation in (
            (HostileDict(self.market()), self.valuation()),
            (self.market(), HostileDict(self.valuation())),
        ):
            touched.clear()
            with self.subTest(
                market_type=type(market).__name__,
                valuation_type=type(valuation).__name__,
            ):
                with self.assertRaisesRegex(
                    AllocationValuationError, "exact dict or mappingproxy"
                ):
                    self.normalize(market, valuation)
                self.assertEqual(touched, [])

    def test_nonexact_mapping_key_is_rejected_before_key_dispatch(self):
        touched = []

        class HostileKey(str):
            def __eq__(self, other):
                touched.append("eq")
                raise AssertionError("hostile key equality")

            def strip(self, *args, **kwargs):
                touched.append("strip")
                raise AssertionError("hostile key strip")

            __hash__ = str.__hash__

        market = self.market()
        market[HostileKey("attacker")] = "value"
        touched.clear()

        with self.assertRaisesRegex(AllocationValuationError, "keys must be exact"):
            self.normalize(market, self.valuation())

        self.assertEqual(touched, [])

    def test_nested_cost_mapping_subclass_is_rejected_before_callbacks(self):
        touched = []

        class HostileCosts(dict):
            def __iter__(self):
                touched.append("iter")
                raise AssertionError("hostile cost iteration")

            def __getitem__(self, key):
                touched.append("getitem")
                raise AssertionError("hostile cost lookup")

        valuation = self.valuation()
        valuation["cost_rate_components"] = HostileCosts(
            {
                "execution": "0.001",
                "financing": "0",
                "funding": "0",
                "borrow": "0",
                "fx": "0",
            }
        )
        touched.clear()

        with self.assertRaisesRegex(
            AllocationValuationError, "exact dict or mappingproxy"
        ):
            self.normalize(self.market(), valuation)

        self.assertEqual(touched, [])

    def test_fx_max_age_integer_subclass_is_rejected_before_comparison(self):
        touched = []

        class HostileInt(int):
            def __le__(self, other):
                touched.append("le")
                raise AssertionError("hostile int comparison")

            def __eq__(self, other):
                touched.append("eq")
                raise AssertionError("hostile int equality")

        digest = "sha256:" + "a" * 64
        quote = {
            "base_currency": "EUR",
            "quote_currency": "USD",
            "bid": "1.10",
            "ask": "1.11",
            "available_at": "2026-09-25T18:29:30Z",
            "source_id": "fx:eurusd:venue:v7",
            "evidence_sha256": digest,
            "max_age_seconds": HostileInt(60),
            "haircut": "0",
        }
        valuation = self.valuation(
            quote_currency="EUR",
            portfolio_base_currency="USD",
            unit_base_notional="11.00",
            fx_rate="1.10",
            fx_source_id="fx:eurusd:venue:v7",
            fx_quote=quote,
            fx_evidence_sha256=digest,
        )
        touched.clear()

        with self.assertRaisesRegex(AllocationValuationError, "positive integer"):
            self.normalize(self.market(quote_currency="EUR"), valuation)

        self.assertEqual(touched, [])


    def test_canonical_mappingproxy_payloads_remain_accepted(self):
        market = MappingProxyType(self.market())
        valuation_data = self.valuation()
        valuation_data["cost_rate_components"] = MappingProxyType(
            dict(valuation_data["cost_rate_components"])
        )
        valuation_data["cost_evidence_refs"] = MappingProxyType(
            dict(valuation_data["cost_evidence_refs"])
        )
        valuation = MappingProxyType(valuation_data)

        result = self.normalize(market, valuation)

        self.assertEqual(result.symbol, "AAA")
        self.assertEqual(result.unit_base_notional, Decimal("10"))


if __name__ == "__main__":
    unittest.main()
