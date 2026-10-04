from decimal import Decimal
import unittest

from mvp.autotrade_mvp.execution_realism import (
    ExecutionModel,
    ExecutionRealismError,
    LiquidityObservation,
    SimulatedOrder,
    simulate_execution,
)


CALIBRATION = "a" * 64


def model(**overrides):
    values = dict(
        model_version="exec-realism-v1",
        calibration_sha256=CALIBRATION,
        data_fidelity="TOP_OF_BOOK",
        scenario="BASE",
        latency_ms=100,
        fee_rate="0.001",
        minimum_fee="0",
        max_participation="0.25",
        slippage_bps="5",
        impact_bps_at_max_participation="10",
        bar_half_spread_bps="0",
        scenario_cost_multiplier="1",
    )
    values.update(overrides)
    return ExecutionModel.create(**values)


def order(**overrides):
    values = dict(
        order_id="sim-1",
        instrument_version="ABC@v1",
        side="BUY",
        order_type="MARKET",
        quantity="10",
        submitted_at="2026-09-24T10:00:00Z",
        lot_size="1",
    )
    values.update(overrides)
    return SimulatedOrder.create(**values)


def top(**overrides):
    values = dict(
        instrument_version="ABC@v1",
        market_time="2026-09-24T10:00:00.200000Z",
        available_at="2026-09-24T10:00:00.250000Z",
        available_volume="100",
        bid="99",
        ask="101",
    )
    values.update(overrides)
    return LiquidityObservation.create(**values)


class ExecutionRealismTests(unittest.TestCase):
    def test_cross_instrument_liquidity_cannot_execute_order(self):
        with self.assertRaisesRegex(
            ExecutionRealismError,
            "instrument_version must exactly match",
        ):
            simulate_execution(
                order(instrument_version="ABC@v1"),
                top(instrument_version="XYZ@v9"),
                model(),
            )

    def test_liquidity_identity_is_required_and_canonical(self):
        with self.assertRaisesRegex(ExecutionRealismError, "instrument_version is required"):
            top(instrument_version="")

    def test_order_cannot_fill_against_same_or_earlier_liquidity(self):
        result = simulate_execution(
            order(),
            top(
                market_time="2026-09-24T10:00:00.100000Z",
                available_at="2026-09-24T10:00:00.100000Z",
            ),
            model(latency_ms=100),
        )
        self.assertEqual(result.status, "WAITING_FOR_LATENCY")
        self.assertEqual(result.filled_quantity, Decimal("0"))
        self.assertIsNone(result.trade_time)

    def test_execution_result_retains_event_and_availability_time(self):
        observation = top(
            market_time="2026-09-24T10:00:00.200000Z",
            available_at="2026-09-24T10:00:00.900000Z",
        )
        result = simulate_execution(order(), observation, model())
        self.assertEqual(result.trade_time, observation.market_time)
        self.assertEqual(result.evidence_available_at, observation.available_at)
        self.assertGreater(result.evidence_available_at, result.trade_time)

    def test_latency_pushes_arrival_past_otherwise_future_quote(self):
        result = simulate_execution(
            order(),
            top(market_time="2026-09-24T10:00:00.200000Z"),
            model(latency_ms=250),
        )
        self.assertEqual(result.status, "WAITING_FOR_LATENCY")
        self.assertEqual(result.arrival_at, "2026-09-24T10:00:00.250000+00:00".replace("+00:00", "Z"))

    def test_market_buy_uses_ask_plus_adverse_slippage_and_impact(self):
        result = simulate_execution(order(), top(), model())
        # Quantity 10 / volume 100 = 10% participation, i.e. 40% of the
        # configured 25% max. Impact = 4 bps; slippage = 5 bps.
        expected = Decimal("101") * (Decimal("1") + Decimal("9") / Decimal("10000"))
        self.assertEqual(result.status, "FILLED")
        self.assertEqual(result.fill_price, expected)
        self.assertEqual(result.fee, Decimal("10") * expected * Decimal("0.001"))

    def test_available_volume_and_participation_create_partial_fill(self):
        result = simulate_execution(
            order(quantity="20"),
            top(available_volume="20"),
            model(max_participation="0.25"),
        )
        self.assertEqual(result.status, "PARTIAL")
        self.assertEqual(result.filled_quantity, Decimal("5"))

    def test_order_quantity_must_match_lot_quantum(self):
        with self.assertRaisesRegex(ExecutionRealismError, "multiple of lot_size"):
            order(quantity="1.5", lot_size="1")

    def test_capacity_is_rounded_down_to_lot_and_never_rounded_up(self):
        result = simulate_execution(
            order(quantity="10", lot_size="2"),
            top(available_volume="15"),
            model(max_participation="0.25"),
        )
        self.assertEqual(result.status, "PARTIAL")
        self.assertEqual(result.filled_quantity, Decimal("2"))

    def test_capacity_below_one_lot_fails_to_no_fill(self):
        result = simulate_execution(
            order(quantity="10", lot_size="5"),
            top(available_volume="10"),
            model(max_participation="0.25"),
        )
        self.assertEqual(result.status, "NO_FILL")
        self.assertEqual(result.filled_quantity, Decimal("0"))

    def test_limit_fill_does_not_assume_price_improvement(self):
        result = simulate_execution(
            order(order_type="LIMIT", limit_price="102"),
            top(ask="100"),
            model(slippage_bps="500", impact_bps_at_max_participation="500"),
        )
        self.assertEqual(result.status, "FILLED")
        self.assertEqual(result.fill_price, Decimal("102"))

    def test_unexecutable_limit_is_no_fill(self):
        result = simulate_execution(
            order(order_type="LIMIT", limit_price="100"),
            top(ask="101"),
            model(),
        )
        self.assertEqual(result.status, "NO_FILL")

    def test_negative_execution_paths_preserve_evidence_availability(self):
        cases = (
            (
                order(order_type="STOP_LIMIT", stop_price="110", limit_price="100"),
                top(),
                "NO_FILL",
            ),
            (
                order(order_type="LIMIT", limit_price="100"),
                top(ask="101"),
                "NO_FILL",
            ),
        )
        for simulated_order, observation, expected_status in cases:
            with self.subTest(expected_status=expected_status):
                result = simulate_execution(simulated_order, observation, model())
                self.assertEqual(result.status, expected_status)
                self.assertEqual(
                    result.evidence_available_at,
                    observation.available_at,
                )
                self.assertIsNone(result.trade_time)

    def test_bar_stop_limit_does_not_choose_favorable_same_bar_ordering(self):
        result = simulate_execution(
            order(
                order_type="STOP_LIMIT",
                stop_price="105",
                limit_price="103",
            ),
            LiquidityObservation.create(
                instrument_version="ABC@v1",
                market_time="2026-09-24T10:01:00Z",
                available_at="2026-09-24T10:01:01Z",
                available_volume="100",
                interval_start="2026-09-24T10:00:30Z",
                bar_low="100",
                bar_high="110",
            ),
            model(data_fidelity="BAR", latency_ms=0),
        )
        self.assertEqual(result.status, "AMBIGUOUS_NO_FILL")
        self.assertTrue(result.triggered)
        self.assertEqual(result.filled_quantity, Decimal("0"))

    def test_top_of_book_stop_limit_cannot_reuse_trigger_event_as_fill(self):
        first = simulate_execution(
            order(
                order_type="STOP_LIMIT",
                stop_price="100",
                limit_price="102",
            ),
            top(bid="100", ask="101"),
            model(data_fidelity="TOP_OF_BOOK", latency_ms=0),
        )
        self.assertEqual(first.status, "NO_FILL")
        self.assertTrue(first.triggered)
        self.assertEqual(first.filled_quantity, Decimal("0"))
        self.assertIn("wait for later liquidity", first.reason)

        later = simulate_execution(
            order(
                order_type="STOP_LIMIT",
                stop_price="100",
                limit_price="102",
                already_triggered=True,
            ),
            top(
                market_time="2026-09-24T10:00:00.300000Z",
                available_at="2026-09-24T10:00:00.350000Z",
                bid="100",
                ask="101",
            ),
            model(data_fidelity="TOP_OF_BOOK", latency_ms=0),
        )
        self.assertEqual(later.status, "FILLED")
        self.assertEqual(later.fill_price, Decimal("102"))

    def test_already_triggered_stop_limit_can_fill_only_on_later_bar(self):
        result = simulate_execution(
            order(
                order_type="STOP_LIMIT",
                stop_price="105",
                limit_price="103",
                already_triggered=True,
                submitted_at="2026-09-24T10:00:00Z",
            ),
            LiquidityObservation.create(
                instrument_version="ABC@v1",
                market_time="2026-09-24T10:02:00Z",
                available_at="2026-09-24T10:02:01Z",
                available_volume="100",
                interval_start="2026-09-24T10:01:00Z",
                bar_low="101",
                bar_high="104",
            ),
            model(data_fidelity="BAR", latency_ms=0),
        )
        self.assertEqual(result.status, "FILLED")
        self.assertEqual(result.fill_price, Decimal("103"))

    def test_bar_market_uses_adverse_extreme_plus_configured_costs(self):
        result = simulate_execution(
            order(side="SELL"),
            LiquidityObservation.create(
                instrument_version="ABC@v1",
                market_time="2026-09-24T10:01:00Z",
                available_at="2026-09-24T10:01:01Z",
                available_volume="100",
                interval_start="2026-09-24T10:00:30Z",
                bar_low="90",
                bar_high="110",
            ),
            model(
                data_fidelity="BAR",
                latency_ms=0,
                slippage_bps="10",
                impact_bps_at_max_participation="0",
                bar_half_spread_bps="5",
            ),
        )
        self.assertEqual(result.fill_price, Decimal("90") * (Decimal("1") - Decimal("15") / Decimal("10000")))
        self.assertIn("intrabar queue", " ".join(result.warnings))

    def test_base_scenario_cannot_hide_optimistic_cost_multiplier(self):
        with self.assertRaisesRegex(
            ExecutionRealismError,
            "BASE scenario_cost_multiplier cannot be below 1",
        ):
            model(scenario="BASE", scenario_cost_multiplier="0.5")

    def test_adverse_multiplier_increases_market_execution_cost(self):
        base = simulate_execution(order(), top(), model())
        adverse = simulate_execution(
            order(),
            top(),
            model(scenario="ADVERSE", scenario_cost_multiplier="2"),
        )
        self.assertGreater(adverse.fill_price, base.fill_price)

    def test_optimistic_scenario_is_explicitly_not_promotion_evidence(self):
        result = simulate_execution(
            order(),
            top(),
            model(scenario="OPTIMISTIC", scenario_cost_multiplier="0.5"),
        )
        self.assertTrue(result.optimistic_only)
        self.assertIn("not sufficient for promotion", " ".join(result.warnings))

    def test_minimum_fee_is_applied_without_float_money(self):
        result = simulate_execution(
            order(quantity="1"),
            top(),
            model(fee_rate="0", minimum_fee="2.50"),
        )
        self.assertEqual(result.fee, Decimal("2.50"))

    def test_binary_float_financial_inputs_are_rejected(self):
        with self.assertRaises(TypeError):
            model(fee_rate=0.001)
        with self.assertRaises(TypeError):
            order(quantity=1.0)
        with self.assertRaises(TypeError):
            top(available_volume=100.0)

    def test_direct_construction_cannot_bypass_execution_model_invariants(self):
        with self.assertRaises(TypeError):
            ExecutionModel(
                model_version="v1",
                calibration_sha256=CALIBRATION,
                data_fidelity="TOP_OF_BOOK",
                scenario="BASE",
                latency_ms=0,
                fee_rate=0.001,
                minimum_fee=Decimal("0"),
                max_participation=Decimal("0.1"),
                slippage_bps=Decimal("0"),
                impact_bps_at_max_participation=Decimal("0"),
                bar_half_spread_bps=Decimal("0"),
                scenario_cost_multiplier=Decimal("1"),
            )
        with self.assertRaises(ExecutionRealismError):
            SimulatedOrder(
                order_id="direct-order",
                instrument_version="ABC@v1",
                side="BUY",
                order_type="MARKET",
                quantity=Decimal("1.5"),
                submitted_at="2026-09-24T10:00:00Z",
                lot_size=Decimal("1"),
            )
        with self.assertRaises(ExecutionRealismError):
            LiquidityObservation(
                instrument_version="ABC@v1",
                market_time="2026-09-24T10:00:01Z",
                available_at="2026-09-24T10:00:00Z",
                available_volume=Decimal("1"),
                bid=Decimal("100"),
                ask=Decimal("101"),
            )

    def test_invalid_calibration_digest_is_rejected(self):
        with self.assertRaisesRegex(ExecutionRealismError, "SHA-256"):
            model(calibration_sha256="not-a-digest")

    def test_model_fingerprint_changes_when_cost_assumption_changes(self):
        one = model(slippage_bps="5")
        two = model(slippage_bps="6")
        self.assertNotEqual(one.fingerprint, two.fingerprint)

    def test_top_of_book_requires_bid_and_ask(self):
        with self.assertRaisesRegex(ExecutionRealismError, "bid and ask"):
            simulate_execution(
                order(),
                LiquidityObservation.create(
                    instrument_version="ABC@v1",
                    market_time="2026-09-24T10:01:00Z",
                    available_at="2026-09-24T10:01:01Z",
                    available_volume="100",
                ),
                model(),
            )


    def test_bar_cannot_use_volume_from_interval_already_underway_at_arrival(self):
        result = simulate_execution(
            order(submitted_at="2026-09-24T10:00:30Z"),
            LiquidityObservation.create(
                instrument_version="ABC@v1",
                market_time="2026-09-24T10:01:00Z",
                available_at="2026-09-24T10:01:01Z",
                available_volume="1000",
                interval_start="2026-09-24T10:00:00Z",
                bar_low="90",
                bar_high="110",
            ),
            model(data_fidelity="BAR", latency_ms=100),
        )
        self.assertEqual(result.status, "AMBIGUOUS_NO_FILL")
        self.assertEqual(result.filled_quantity, Decimal("0"))
        self.assertIn("before venue arrival", result.reason)

    def test_bar_interval_start_one_tick_before_arrival_is_ambiguous(self):
        result = simulate_execution(
            order(submitted_at="2026-09-24T10:00:00Z"),
            LiquidityObservation.create(
                instrument_version="ABC@v1",
                market_time="2026-09-24T10:01:00Z",
                available_at="2026-09-24T10:01:01Z",
                available_volume="1000",
                interval_start="2026-09-24T10:00:00.099999Z",
                bar_low="90",
                bar_high="110",
            ),
            model(data_fidelity="BAR", latency_ms=100),
        )
        self.assertEqual(result.status, "AMBIGUOUS_NO_FILL")
        self.assertEqual(result.filled_quantity, Decimal("0"))

    def test_bar_interval_start_exactly_at_arrival_is_causally_eligible(self):
        result = simulate_execution(
            order(submitted_at="2026-09-24T10:00:00Z"),
            LiquidityObservation.create(
                instrument_version="ABC@v1",
                market_time="2026-09-24T10:01:00Z",
                available_at="2026-09-24T10:01:01Z",
                available_volume="1000",
                interval_start="2026-09-24T10:00:00.100000Z",
                bar_low="90",
                bar_high="110",
            ),
            model(data_fidelity="BAR", latency_ms=100),
        )
        self.assertIn(result.status, {"FILLED", "PARTIAL"})
        self.assertGreater(result.filled_quantity, Decimal("0"))

    def test_bar_requires_interval_start_to_bound_causal_volume(self):
        with self.assertRaisesRegex(
            ExecutionRealismError, "requires interval_start"
        ):
            simulate_execution(
                order(),
                LiquidityObservation.create(
                    instrument_version="ABC@v1",
                    market_time="2026-09-24T10:01:00Z",
                    available_at="2026-09-24T10:01:01Z",
                    available_volume="100",
                    bar_low="90",
                    bar_high="110",
                ),
                model(data_fidelity="BAR", latency_ms=0),
            )


if __name__ == "__main__":
    unittest.main()
