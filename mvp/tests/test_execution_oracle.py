from dataclasses import replace
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, localcontext
import unittest

from mvp.autotrade_mvp.execution_oracle import (
    ExecutionOracleError,
    assert_conservative_execution,
)
from mvp.autotrade_mvp.execution_realism import (
    ExecutionModel,
    ExecutionPriceProjectionPolicy,
    ExecutionRealismError,
    LiquidityObservation,
    SimulatedExecution,
    SimulatedOrder,
    simulate_execution,
)


CALIBRATION = "a" * 64
INSTRUMENT_BINDING = "c" * 64


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
        scenario_cost_multiplier="1",
        price_projection=ExecutionPriceProjectionPolicy(
            policy_id="ADVERSE_INSTRUMENT_TICK",
            policy_version="1",
            instrument_version="ABC@v1",
            price_quantum="0.01",
            instrument_metadata_binding=INSTRUMENT_BINDING,
        ),
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


def observation(**overrides):
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


class ExecutionOracleTests(unittest.TestCase):
    def test_oracle_rejects_domain_subclasses_before_economic_checks(self):
        class DerivedOrder(SimulatedOrder):
            pass

        class DerivedObservation(LiquidityObservation):
            pass

        class DerivedModel(ExecutionModel):
            pass

        class DerivedExecution(SimulatedExecution):
            pass

        exact_order = order()
        exact_observation = observation()
        exact_model = model()
        exact_result = simulate_execution(
            exact_order,
            exact_observation,
            exact_model,
        )
        derived_order = DerivedOrder(**exact_order.__dict__)
        derived_observation = DerivedObservation(**exact_observation.__dict__)
        derived_model = DerivedModel(**exact_model.__dict__)
        derived_result = DerivedExecution(**exact_result.__dict__)

        with self.assertRaisesRegex(TypeError, "exact SimulatedOrder"):
            assert_conservative_execution(
                order=derived_order,
                observation=exact_observation,
                model=exact_model,
                result=exact_result,
            )
        with self.assertRaisesRegex(TypeError, "exact LiquidityObservation"):
            assert_conservative_execution(
                order=exact_order,
                observation=derived_observation,
                model=exact_model,
                result=exact_result,
            )
        with self.assertRaisesRegex(TypeError, "exact ExecutionModel"):
            assert_conservative_execution(
                order=exact_order,
                observation=exact_observation,
                model=derived_model,
                result=exact_result,
            )
        with self.assertRaisesRegex(TypeError, "exact SimulatedExecution"):
            assert_conservative_execution(
                order=exact_order,
                observation=exact_observation,
                model=exact_model,
                result=derived_result,
            )

    def test_oracle_revalidates_exact_objects_after_frozen_mutation(self):
        class HostileDecimal(Decimal):
            compare_calls = 0

            def __lt__(self, other):
                type(self).compare_calls += 1
                raise AssertionError("hostile Decimal comparison executed")

            def __gt__(self, other):
                type(self).compare_calls += 1
                raise AssertionError("hostile Decimal comparison executed")

        class HostileText(str):
            equality_calls = 0

            def __eq__(self, other):
                type(self).equality_calls += 1
                raise AssertionError("hostile text equality executed")

        o, q, m = order(), observation(), model()
        mutated_result = simulate_execution(o, q, m)
        object.__setattr__(
            mutated_result,
            "filled_quantity",
            HostileDecimal("1"),
        )
        with self.assertRaisesRegex(TypeError, "filled_quantity must be exact Decimal"):
            assert_conservative_execution(
                order=o,
                observation=q,
                model=m,
                result=mutated_result,
            )
        self.assertEqual(HostileDecimal.compare_calls, 0)

        mutated_text_result = simulate_execution(o, q, m)
        object.__setattr__(
            mutated_text_result,
            "model_fingerprint",
            HostileText(mutated_text_result.model_fingerprint),
        )
        with self.assertRaisesRegex(TypeError, "model_fingerprint must be exact text"):
            assert_conservative_execution(
                order=o,
                observation=q,
                model=m,
                result=mutated_text_result,
            )
        self.assertEqual(HostileText.equality_calls, 0)

        injected_result = simulate_execution(o, q, m)
        object.__setattr__(injected_result, "shadow_authority", "forged")
        with self.assertRaisesRegex(TypeError, "unexpected state fields"):
            assert_conservative_execution(
                order=o,
                observation=q,
                model=m,
                result=injected_result,
            )

        class HostileStateField(str):
            armed = False
            equality_calls = 0

            def __eq__(self, other):
                type(self).equality_calls += 1
                if type(self).armed:
                    raise AssertionError("hostile result state-field equality executed")
                return super().__eq__(other)

            __hash__ = str.__hash__

        hostile_key_result = simulate_execution(o, q, m)
        hostile_state = dict(hostile_key_result.__dict__)
        filled_quantity = hostile_state.pop("filled_quantity")
        hostile_state[HostileStateField("filled_quantity")] = filled_quantity
        object.__setattr__(hostile_key_result, "__dict__", hostile_state)
        HostileStateField.armed = True
        with self.assertRaisesRegex(TypeError, "non-canonical state field names"):
            assert_conservative_execution(
                order=o,
                observation=q,
                model=m,
                result=hostile_key_result,
            )
        self.assertEqual(HostileStateField.equality_calls, 0)

        mutated_order = order()
        object.__setattr__(mutated_order, "quantity", Decimal("-1"))
        with self.assertRaisesRegex(ExecutionRealismError, "quantity must be positive"):
            assert_conservative_execution(
                order=mutated_order,
                observation=q,
                model=m,
                result=simulate_execution(o, q, m),
            )

    def test_oracle_rejects_forged_arrival_evidence(self):
        o, q, m = order(), observation(), model()
        result = simulate_execution(o, q, m)
        forged = replace(result, arrival_at="2026-09-24T10:00:00Z")
        with self.assertRaisesRegex(ExecutionOracleError, "independently derived arrival"):
            assert_conservative_execution(
                order=o,
                observation=q,
                model=m,
                result=forged,
            )

    def test_existing_conservative_market_fill_passes_independent_oracle(self):
        o = order()
        q = observation()
        m = model()
        result = simulate_execution(o, q, m)
        assert_conservative_execution(order=o, observation=q, model=m, result=result)

    def test_oracle_requires_market_projection_authority_before_result_checks(self):
        o, q, valid_model = order(), observation(), model()
        result = simulate_execution(o, q, valid_model)
        with self.assertRaisesRegex(
            ExecutionOracleError,
            "requires authoritative price projection policy",
        ):
            assert_conservative_execution(
                order=o,
                observation=q,
                model=model(price_projection=None),
                result=result,
            )

    def test_oracle_market_projection_is_invariant_to_ambient_decimal_context(self):
        o = order(quantity="10")
        q = observation(available_volume="30")
        m = model(max_participation="0.5")
        with localcontext() as context:
            context.prec = 80
            result = simulate_execution(o, q, m)

        for precision, rounding in (
            (6, ROUND_FLOOR),
            (6, ROUND_CEILING),
            (10, ROUND_FLOOR),
            (28, ROUND_CEILING),
            (80, ROUND_CEILING),
        ):
            with self.subTest(precision=precision, rounding=rounding):
                with localcontext() as context:
                    context.prec = precision
                    context.rounding = rounding
                    assert_conservative_execution(
                        order=o,
                        observation=q,
                        model=m,
                        result=result,
                    )

    def test_oracle_rejects_off_grid_market_reference(self):
        o, q, m = order(), observation(), model()
        result = simulate_execution(o, q, m)
        with self.assertRaisesRegex(
            ExecutionOracleError,
            "market reference price is not aligned to authoritative price quantum",
        ):
            assert_conservative_execution(
                order=o,
                observation=observation(ask="101.005"),
                model=m,
                result=result,
            )

    def test_oracle_rejects_market_price_that_differs_from_adverse_tick_bound(self):
        o, q, m = order(), observation(), model()
        result = simulate_execution(o, q, m)
        forged = replace(result, fill_price=result.fill_price + Decimal("0.01"))
        with self.assertRaisesRegex(
            ExecutionOracleError,
            "independently projected adverse tick bound",
        ):
            assert_conservative_execution(
                order=o,
                observation=q,
                model=m,
                result=forged,
            )

    def test_oracle_limit_arithmetic_is_invariant_to_ambient_decimal_context(self):
        o = order(
            order_type="LIMIT",
            quantity="9999999999999999999.99",
            lot_size="0.01",
            limit_price="102.12345678901234567890123456789",
        )
        q = observation(
            available_volume="12345678901234567890.12",
            ask="101",
        )
        m = model(
            max_participation="0.123456789012345678",
            fee_rate="0.001234567890123456789",
        )
        with localcontext() as context:
            context.prec = 80
            result = simulate_execution(o, q, m)

        for precision, rounding in (
            (6, ROUND_FLOOR),
            (6, ROUND_CEILING),
            (80, ROUND_CEILING),
        ):
            with self.subTest(precision=precision, rounding=rounding):
                with localcontext() as context:
                    context.prec = precision
                    context.rounding = rounding
                    assert_conservative_execution(
                        order=o,
                        observation=q,
                        model=m,
                        result=result,
                    )

    def test_oracle_rejects_limit_fill_without_executable_price_evidence(self):
        o = order(order_type="LIMIT", limit_price="100")
        q = observation(ask="101")
        m = model()
        no_fill = simulate_execution(o, q, m)
        self.assertEqual(no_fill.status, "NO_FILL")
        forged = replace(
            no_fill,
            status="FILLED",
            filled_quantity=Decimal("10"),
            fill_price=Decimal("100"),
            fee=Decimal("1"),
            trade_time=q.market_time,
        )
        with self.assertRaisesRegex(
            ExecutionOracleError,
            "independently executable price evidence",
        ):
            assert_conservative_execution(order=o, observation=q, model=m, result=forged)

    def test_oracle_rejects_limit_fill_better_than_frozen_limit(self):
        o = order(order_type="LIMIT", limit_price="102")
        q = observation(ask="101")
        m = model()
        result = simulate_execution(o, q, m)
        self.assertEqual(result.fill_price, Decimal("102"))
        forged = replace(result, fill_price=Decimal("101"))
        with self.assertRaisesRegex(
            ExecutionOracleError,
            "frozen order limit",
        ):
            assert_conservative_execution(order=o, observation=q, model=m, result=forged)

    def test_oracle_rejects_same_observation_stop_limit_fill_before_prior_trigger(self):
        o = order(
            order_type="STOP_LIMIT",
            limit_price="102",
            stop_price="100",
        )
        q = observation(ask="101")
        m = model()
        waiting = simulate_execution(o, q, m)
        self.assertEqual(waiting.status, "NO_FILL")
        self.assertTrue(waiting.triggered)
        forged = replace(
            waiting,
            status="FILLED",
            filled_quantity=Decimal("10"),
            fill_price=Decimal("102"),
            fee=Decimal("1.02"),
            trade_time=q.market_time,
        )
        with self.assertRaisesRegex(
            ExecutionOracleError,
            "previously triggered order",
        ):
            assert_conservative_execution(order=o, observation=q, model=m, result=forged)

    def test_oracle_accepts_pretriggered_stop_limit_with_executable_limit(self):
        o = order(
            order_type="STOP_LIMIT",
            limit_price="102",
            stop_price="100",
            already_triggered=True,
        )
        q = observation(ask="101")
        m = model()
        result = simulate_execution(o, q, m)
        self.assertIn(result.status, {"FILLED", "PARTIAL"})
        self.assertTrue(result.triggered)
        assert_conservative_execution(order=o, observation=q, model=m, result=result)

    def test_oracle_rejects_trigger_state_regression_after_prior_stop_trigger(self):
        o = order(
            order_type="STOP_LIMIT",
            limit_price="102",
            stop_price="100",
            already_triggered=True,
        )
        q = observation(ask="101")
        m = model()
        result = simulate_execution(o, q, m)
        self.assertIn(result.status, {"FILLED", "PARTIAL"})
        self.assertTrue(result.triggered)
        forged = replace(result, triggered=False)
        with self.assertRaisesRegex(
            ExecutionOracleError,
            "triggered state cannot regress",
        ):
            assert_conservative_execution(
                order=o,
                observation=q,
                model=m,
                result=forged,
            )

    def test_oracle_rejects_forged_stop_trigger_without_price_evidence(self):
        o = order(
            order_type="STOP_LIMIT",
            limit_price="102",
            stop_price="105",
        )
        q = observation(ask="101")
        m = model()
        result = simulate_execution(o, q, m)
        self.assertFalse(result.triggered)
        forged = replace(result, triggered=True)
        with self.assertRaisesRegex(
            ExecutionOracleError,
            "independently observed stop evidence",
        ):
            assert_conservative_execution(order=o, observation=q, model=m, result=forged)

    def test_oracle_rejects_stop_trigger_from_same_or_earlier_liquidity(self):
        o = order(
            order_type="STOP_LIMIT",
            limit_price="102",
            stop_price="100",
        )
        q = observation(
            market_time="2026-09-24T10:00:00.100000Z",
            available_at="2026-09-24T10:00:00.150000Z",
            ask="101",
        )
        m = model(latency_ms=100)
        waiting = simulate_execution(o, q, m)
        self.assertFalse(waiting.triggered)
        forged = replace(waiting, triggered=True)
        with self.assertRaisesRegex(
            ExecutionOracleError,
            "same or earlier liquidity",
        ):
            assert_conservative_execution(order=o, observation=q, model=m, result=forged)

    def test_oracle_rejects_stop_trigger_from_bar_started_before_arrival(self):
        o = order(
            order_type="STOP_LIMIT",
            limit_price="102",
            stop_price="100",
        )
        q = observation(
            market_time="2026-09-24T10:01:00Z",
            available_at="2026-09-24T10:01:01Z",
            interval_start="2026-09-24T10:00:00Z",
            bar_low="99",
            bar_high="101",
        )
        m = model(data_fidelity="BAR", latency_ms=100)
        waiting = simulate_execution(o, q, m)
        self.assertFalse(waiting.triggered)
        forged = replace(waiting, triggered=True)
        with self.assertRaisesRegex(
            ExecutionOracleError,
            "BAR evidence from before venue arrival",
        ):
            assert_conservative_execution(order=o, observation=q, model=m, result=forged)

    def test_oracle_accepts_observed_stop_trigger_with_zero_fill_capacity(self):
        o = order(
            order_type="STOP_LIMIT",
            limit_price="102",
            stop_price="100",
            lot_size="5",
            quantity="10",
        )
        q = observation(
            ask="101",
            available_volume="0",
        )
        m = model()
        result = simulate_execution(o, q, m)
        self.assertEqual(result.filled_quantity, Decimal("0"))
        self.assertTrue(result.triggered)
        assert_conservative_execution(
            order=o,
            observation=q,
            model=m,
            result=result,
        )
        with self.assertRaisesRegex(
            ExecutionOracleError,
            "observed stop trigger cannot be omitted",
        ):
            assert_conservative_execution(
                order=o,
                observation=q,
                model=m,
                result=replace(result, triggered=False),
            )

    def test_oracle_accepts_observed_stop_trigger_without_same_observation_fill(self):
        o = order(
            order_type="STOP_LIMIT",
            limit_price="102",
            stop_price="100",
        )
        q = observation(ask="101")
        m = model()
        result = simulate_execution(o, q, m)
        self.assertTrue(result.triggered)
        self.assertEqual(result.filled_quantity, Decimal("0"))
        assert_conservative_execution(order=o, observation=q, model=m, result=result)
        with self.assertRaisesRegex(
            ExecutionOracleError,
            "observed stop trigger cannot be omitted",
        ):
            assert_conservative_execution(
                order=o,
                observation=q,
                model=m,
                result=replace(result, triggered=False),
            )

    def test_oracle_requires_waiting_status_before_venue_arrival(self):
        o = order()
        q = observation(
            market_time="2026-09-24T10:00:00.100000Z",
            available_at="2026-09-24T10:00:00.150000Z",
        )
        m = model(latency_ms=100)
        waiting = simulate_execution(o, q, m)
        self.assertEqual(waiting.status, "WAITING_FOR_LATENCY")
        with self.assertRaisesRegex(
            ExecutionOracleError,
            "pre-arrival result must remain WAITING_FOR_LATENCY",
        ):
            assert_conservative_execution(
                order=o,
                observation=q,
                model=m,
                result=replace(waiting, status="NO_FILL"),
            )

    def test_oracle_accepts_pre_arrival_bar_waiting_without_interval_start(self):
        o = order()
        q = LiquidityObservation.create(
            instrument_version="ABC@v1",
            market_time="2026-09-24T10:00:00.100000Z",
            available_at="2026-09-24T10:00:00.150000Z",
            available_volume="100",
            bar_low="90",
            bar_high="110",
        )
        m = model(data_fidelity="BAR", latency_ms=100)
        waiting = simulate_execution(o, q, m)
        self.assertEqual(waiting.status, "WAITING_FOR_LATENCY")
        assert_conservative_execution(
            order=o,
            observation=q,
            model=m,
            result=waiting,
        )

    def test_oracle_rejects_waiting_status_after_venue_arrival(self):
        o = order(order_type="LIMIT", limit_price="90")
        q = observation()
        m = model()
        no_fill = simulate_execution(o, q, m)
        self.assertEqual(no_fill.status, "NO_FILL")
        with self.assertRaisesRegex(
            ExecutionOracleError,
            "post-arrival result cannot claim WAITING_FOR_LATENCY",
        ):
            assert_conservative_execution(
                order=o,
                observation=q,
                model=m,
                result=replace(no_fill, status="WAITING_FOR_LATENCY"),
            )

    def test_oracle_requires_ambiguous_status_for_bar_overlapping_arrival(self):
        o = order(submitted_at="2026-09-24T10:00:30Z")
        q = LiquidityObservation.create(
            instrument_version="ABC@v1",
            market_time="2026-09-24T10:01:00Z",
            available_at="2026-09-24T10:01:01Z",
            available_volume="100",
            interval_start="2026-09-24T10:00:00Z",
            bar_low="90",
            bar_high="110",
        )
        m = model(data_fidelity="BAR", latency_ms=100)
        ambiguous = simulate_execution(o, q, m)
        self.assertEqual(ambiguous.status, "AMBIGUOUS_NO_FILL")
        with self.assertRaisesRegex(
            ExecutionOracleError,
            "BAR causal ambiguity must remain AMBIGUOUS_NO_FILL",
        ):
            assert_conservative_execution(
                order=o,
                observation=q,
                model=m,
                result=replace(ambiguous, status="NO_FILL"),
            )

    def test_oracle_requires_ambiguous_status_for_same_bar_stop_limit_ordering(self):
        o = order(
            order_type="STOP_LIMIT",
            stop_price="105",
            limit_price="103",
        )
        q = LiquidityObservation.create(
            instrument_version="ABC@v1",
            market_time="2026-09-24T10:01:00Z",
            available_at="2026-09-24T10:01:01Z",
            available_volume="100",
            interval_start="2026-09-24T10:00:30Z",
            bar_low="100",
            bar_high="110",
        )
        m = model(data_fidelity="BAR", latency_ms=0)
        ambiguous = simulate_execution(o, q, m)
        self.assertEqual(ambiguous.status, "AMBIGUOUS_NO_FILL")
        self.assertTrue(ambiguous.triggered)
        with self.assertRaisesRegex(
            ExecutionOracleError,
            "BAR causal ambiguity must remain AMBIGUOUS_NO_FILL",
        ):
            assert_conservative_execution(
                order=o,
                observation=q,
                model=m,
                result=replace(ambiguous, status="NO_FILL"),
            )

    def test_oracle_rejects_bar_ambiguity_without_causal_ambiguity(self):
        o = order(order_type="LIMIT", limit_price="90")
        q = LiquidityObservation.create(
            instrument_version="ABC@v1",
            market_time="2026-09-24T10:01:00Z",
            available_at="2026-09-24T10:01:01Z",
            available_volume="100",
            interval_start="2026-09-24T10:00:30Z",
            bar_low="100",
            bar_high="110",
        )
        m = model(data_fidelity="BAR", latency_ms=0)
        no_fill = simulate_execution(o, q, m)
        self.assertEqual(no_fill.status, "NO_FILL")
        with self.assertRaisesRegex(
            ExecutionOracleError,
            "AMBIGUOUS_NO_FILL lacks BAR causal ambiguity",
        ):
            assert_conservative_execution(
                order=o,
                observation=q,
                model=m,
                result=replace(no_fill, status="AMBIGUOUS_NO_FILL"),
            )

    def test_oracle_rejects_ambiguous_status_outside_bar_fidelity(self):
        o = order(order_type="LIMIT", limit_price="90")
        q = observation()
        m = model()
        no_fill = simulate_execution(o, q, m)
        self.assertEqual(no_fill.status, "NO_FILL")
        with self.assertRaisesRegex(
            ExecutionOracleError,
            "AMBIGUOUS_NO_FILL requires BAR causal ambiguity",
        ):
            assert_conservative_execution(
                order=o,
                observation=q,
                model=m,
                result=replace(no_fill, status="AMBIGUOUS_NO_FILL"),
            )

    def test_oracle_rejects_status_quantity_contradictions(self):
        o, q, m = order(), observation(), model()
        full = simulate_execution(o, q, m)
        self.assertEqual(full.status, "FILLED")
        with self.assertRaisesRegex(
            ExecutionOracleError,
            "positive fill status must match exact execution completeness",
        ):
            assert_conservative_execution(
                order=o,
                observation=q,
                model=m,
                result=replace(full, status="NO_FILL"),
            )
        with self.assertRaisesRegex(
            ExecutionOracleError,
            "positive fill status must match exact execution completeness",
        ):
            assert_conservative_execution(
                order=o,
                observation=q,
                model=m,
                result=replace(full, status="PARTIAL"),
            )

        partial_order = order(quantity="20")
        partial_observation = observation(available_volume="20")
        partial = simulate_execution(partial_order, partial_observation, m)
        self.assertEqual(partial.status, "PARTIAL")
        with self.assertRaisesRegex(
            ExecutionOracleError,
            "positive fill status must match exact execution completeness",
        ):
            assert_conservative_execution(
                order=partial_order,
                observation=partial_observation,
                model=m,
                result=replace(partial, status="FILLED"),
            )

        no_fill_order = order(order_type="LIMIT", limit_price="90")
        no_fill = simulate_execution(no_fill_order, q, m)
        self.assertEqual(no_fill.filled_quantity, Decimal("0"))
        with self.assertRaisesRegex(
            ExecutionOracleError,
            "zero fill cannot claim FILLED or PARTIAL status",
        ):
            assert_conservative_execution(
                order=no_fill_order,
                observation=q,
                model=m,
                result=replace(no_fill, status="FILLED"),
            )

    def test_oracle_rejects_quantity_above_participation_capacity(self):
        o = order(quantity="20")
        q = observation(available_volume="20")
        m = model(max_participation="0.25")
        result = simulate_execution(o, q, m)
        bad = replace(result, filled_quantity=Decimal("6"))
        with self.assertRaisesRegex(ExecutionOracleError, "participation capacity"):
            assert_conservative_execution(order=o, observation=q, model=m, result=bad)

    def test_oracle_rejects_market_buy_better_than_ask(self):
        o, q, m = order(), observation(), model()
        result = simulate_execution(o, q, m)
        bad = replace(result, fill_price=Decimal("100"))
        with self.assertRaisesRegex(ExecutionOracleError, "more favorable"):
            assert_conservative_execution(order=o, observation=q, model=m, result=bad)

    def test_oracle_rejects_market_sell_better_than_bid(self):
        o = order(side="SELL")
        q = observation()
        m = model()
        result = simulate_execution(o, q, m)
        bad = replace(result, fill_price=Decimal("100"))
        with self.assertRaisesRegex(ExecutionOracleError, "more favorable"):
            assert_conservative_execution(order=o, observation=q, model=m, result=bad)

    def test_oracle_rejects_fee_below_model_minimum(self):
        o, q = order(quantity="1"), observation()
        m = model(fee_rate="0", minimum_fee="2.50")
        result = simulate_execution(o, q, m)
        bad = replace(result, fee=Decimal("2.49"))
        with self.assertRaisesRegex(ExecutionOracleError, "fee is below"):
            assert_conservative_execution(order=o, observation=q, model=m, result=bad)

    def test_oracle_rejects_filled_result_at_or_before_arrival(self):
        o = order()
        q = observation(
            market_time="2026-09-24T10:00:00.100000Z",
            available_at="2026-09-24T10:00:00.150000Z",
        )
        m = model(latency_ms=100)
        waiting = simulate_execution(o, q, m)
        forged = replace(
            waiting,
            status="PARTIAL",
            filled_quantity=Decimal("1"),
            fill_price=Decimal("101"),
            fee=Decimal("0.101"),
            trade_time=q.market_time,
        )
        with self.assertRaisesRegex(ExecutionOracleError, "same or earlier"):
            assert_conservative_execution(order=o, observation=q, model=m, result=forged)

    def test_oracle_rejects_bar_interval_underway_at_arrival(self):
        o = order(submitted_at="2026-09-24T10:00:00Z")
        q = observation(
            market_time="2026-09-24T10:01:00Z",
            available_at="2026-09-24T10:01:01Z",
            interval_start="2026-09-24T10:00:00Z",
            bar_low="90",
            bar_high="110",
        )
        m = model(data_fidelity="BAR", latency_ms=100)
        waiting = simulate_execution(o, q, m)
        forged = replace(
            waiting,
            status="PARTIAL",
            filled_quantity=Decimal("1"),
            fill_price=Decimal("110"),
            fee=Decimal("0.110"),
            trade_time=q.market_time,
        )
        with self.assertRaisesRegex(
            ExecutionOracleError,
            "before venue arrival",
        ):
            assert_conservative_execution(
                order=o,
                observation=q,
                model=m,
                result=forged,
            )

    def test_oracle_rejects_bar_interval_start_one_tick_before_arrival(self):
        o = order(submitted_at="2026-09-24T10:00:00Z")
        q = observation(
            market_time="2026-09-24T10:01:00Z",
            available_at="2026-09-24T10:01:01Z",
            interval_start="2026-09-24T10:00:00.099999Z",
            bar_low="90",
            bar_high="110",
        )
        m = model(data_fidelity="BAR", latency_ms=100)
        waiting = simulate_execution(o, q, m)
        forged = replace(
            waiting,
            status="PARTIAL",
            filled_quantity=Decimal("1"),
            fill_price=Decimal("110"),
            fee=Decimal("0.110"),
            trade_time=q.market_time,
        )
        with self.assertRaisesRegex(ExecutionOracleError, "before venue arrival"):
            assert_conservative_execution(
                order=o,
                observation=q,
                model=m,
                result=forged,
            )

    def test_oracle_accepts_bar_interval_start_exactly_at_arrival(self):
        o = order(submitted_at="2026-09-24T10:00:00Z")
        q = observation(
            market_time="2026-09-24T10:01:00Z",
            available_at="2026-09-24T10:01:01Z",
            interval_start="2026-09-24T10:00:00.100000Z",
            bar_low="90",
            bar_high="110",
        )
        m = model(data_fidelity="BAR", latency_ms=100)
        result = simulate_execution(o, q, m)
        self.assertIn(result.status, {"FILLED", "PARTIAL"})
        assert_conservative_execution(
            order=o,
            observation=q,
            model=m,
            result=result,
        )

    def test_oracle_rejects_zero_fill_with_economic_posting_fields(self):
        o = order(order_type="LIMIT", limit_price="90")
        q, m = observation(), model()
        result = simulate_execution(o, q, m)
        bad = replace(result, fee=Decimal("1"))
        with self.assertRaisesRegex(ExecutionOracleError, "zero fill cannot carry fee"):
            assert_conservative_execution(order=o, observation=q, model=m, result=bad)

    def test_oracle_rejects_stale_model_fingerprint(self):
        o, q = order(), observation()
        m = model(slippage_bps="5")
        result = simulate_execution(o, q, m)
        changed = model(slippage_bps="6")
        with self.assertRaisesRegex(ExecutionOracleError, "model fingerprint"):
            assert_conservative_execution(
                order=o,
                observation=q,
                model=changed,
                result=result,
            )


if __name__ == "__main__":
    unittest.main()
