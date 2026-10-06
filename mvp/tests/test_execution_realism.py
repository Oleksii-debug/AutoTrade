from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, localcontext
import unittest

from mvp.autotrade_mvp.instruments import (
    InstrumentRegistry,
    InstrumentVersion,
    TradingCalendar,
)
from mvp.autotrade_mvp.execution_realism import (
    ExecutionModel,
    ExecutionPriceGrid,
    ExecutionPriceProjectionPolicy,
    ExecutionRealismError,
    LiquidityObservation,
    SimulatedOrder,
    simulate_execution,
)


CALIBRATION = "a" * 64
INSTRUMENT_BINDING = "c" * 64
INSTRUMENT_ID = "11111111-1111-4111-8111-111111111111"
INSTRUMENT_REF = f"{INSTRUMENT_ID}@1"
OTHER_INSTRUMENT_ID = "33333333-3333-4333-8333-333333333333"
METADATA_ARTIFACT_ID = "22222222-2222-4222-8222-222222222222"
METADATA_SHA256 = "d" * 64


def canonical_instrument(
    *,
    instrument_id=INSTRUMENT_ID,
    provider_symbol="ABC",
    price_tick="0.01",
    evidence_sha256=METADATA_SHA256,
):
    return InstrumentVersion(
        instrument_id=instrument_id,
        version=1,
        provider_id="simulated",
        venue_id="simulated-venue",
        provider_symbol=provider_symbol,
        asset_class="CASH_EQUITY",
        base_currency=provider_symbol,
        quote_currency="USD",
        settlement_currency="USD",
        quantity_unit="share",
        contract_multiplier="1",
        price_tick=price_tick,
        quantity_step="1",
        minimum_quantity="1",
        calendar_id="CONTINUOUS_24_7",
        timezone_id="UTC",
        effective_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
        metadata_evidence=(
            {
                "artifact_id": METADATA_ARTIFACT_ID,
                "sha256": f"sha256:{evidence_sha256}",
                "observed_at": "2025-12-30T10:00:00Z",
            },
        ),
    )


def price_authority(
    *,
    instrument_id=INSTRUMENT_ID,
    provider_symbol="ABC",
    price_tick="0.01",
    evidence_sha256=METADATA_SHA256,
):
    instrument = canonical_instrument(
        instrument_id=instrument_id,
        provider_symbol=provider_symbol,
        price_tick=price_tick,
        evidence_sha256=evidence_sha256,
    )
    registry = InstrumentRegistry(
        calendars=(TradingCalendar.continuous_24_7(),),
        versions=(instrument,),
    )
    instrument_ref = f"{instrument.instrument_id}@{instrument.version}"
    return (
        ExecutionPriceProjectionPolicy.from_instrument(instrument),
        ExecutionPriceGrid.from_registry(registry, instrument_ref),
    )


def model(**overrides):
    default_projection, default_grid = price_authority()
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
        price_projection=default_projection,
        price_grid=default_grid,
    )
    values.update(overrides)
    return ExecutionModel.create(**values)


def order(**overrides):
    values = dict(
        order_id="sim-1",
        instrument_version=INSTRUMENT_REF,
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
        instrument_version=INSTRUMENT_REF,
        market_time="2026-09-24T10:00:00.200000Z",
        available_at="2026-09-24T10:00:00.250000Z",
        available_volume="100",
        bid="99",
        ask="101",
    )
    values.update(overrides)
    return LiquidityObservation.create(**values)


class ExecutionRealismTests(unittest.TestCase):
    def test_already_triggered_is_reserved_for_stop_limit_orders(self):
        for order_type, kwargs in (
            ("MARKET", {}),
            ("LIMIT", {"limit_price": "102"}),
        ):
            with self.subTest(order_type=order_type):
                with self.assertRaisesRegex(
                    ExecutionRealismError,
                    "only valid for STOP_LIMIT",
                ):
                    order(order_type=order_type, already_triggered=True, **kwargs)

    def test_execution_scalar_ingress_rejects_hostile_subclasses_without_callbacks(self):
        class HostileDecimal(Decimal):
            finite_calls = 0

            def is_finite(self):
                type(self).finite_calls += 1
                raise AssertionError("hostile decimal callback executed")

        class HostileText(str):
            strip_calls = 0

            def strip(self, *args, **kwargs):
                type(self).strip_calls += 1
                raise AssertionError("hostile text callback executed")

        class HostileInt(int):
            compare_calls = 0

            def __lt__(self, other):
                type(self).compare_calls += 1
                raise AssertionError("hostile latency comparison executed")

        with self.assertRaisesRegex(ExecutionRealismError, "finite decimal"):
            model(fee_rate=HostileDecimal("0.001"))
        with self.assertRaisesRegex(ExecutionRealismError, "finite decimal"):
            ExecutionPriceProjectionPolicy(
                policy_id="ADVERSE_INSTRUMENT_TICK",
                policy_version="1",
                instrument_version=INSTRUMENT_REF,
                price_quantum=HostileDecimal("0.01"),
                instrument_metadata_binding=INSTRUMENT_BINDING,
            )
        self.assertEqual(HostileDecimal.finite_calls, 0)

        with self.assertRaisesRegex(ExecutionRealismError, "order_id is required"):
            order(order_id=HostileText("sim-1"))
        with self.assertRaisesRegex(
            ExecutionRealismError,
            "projection instrument_metadata_binding is required",
        ):
            ExecutionPriceProjectionPolicy(
                policy_id="ADVERSE_INSTRUMENT_TICK",
                policy_version="1",
                instrument_version=INSTRUMENT_REF,
                price_quantum="0.01",
                instrument_metadata_binding=HostileText("c" * 64),
            )
        self.assertEqual(HostileText.strip_calls, 0)

        with self.assertRaisesRegex(ExecutionRealismError, "latency_ms must be"):
            model(latency_ms=HostileInt(100))
        self.assertEqual(HostileInt.compare_calls, 0)

    def test_execution_scalar_ingress_enforces_shared_decimal_resource_envelope(self):
        with self.assertRaisesRegex(ExecutionRealismError, "finite decimal"):
            model(fee_rate="9" * 257)

    def test_simulation_rejects_domain_subclasses_before_execution_logic(self):
        class DerivedOrder(SimulatedOrder):
            pass

        class DerivedObservation(LiquidityObservation):
            pass

        class DerivedModel(ExecutionModel):
            pass

        exact_order = order()
        exact_observation = top()
        exact_model = model()
        derived_order = DerivedOrder(**exact_order.__dict__)
        derived_observation = DerivedObservation(**exact_observation.__dict__)
        derived_model = DerivedModel(**exact_model.__dict__)

        with self.assertRaisesRegex(TypeError, "exact SimulatedOrder"):
            simulate_execution(derived_order, exact_observation, exact_model)
        with self.assertRaisesRegex(TypeError, "exact LiquidityObservation"):
            simulate_execution(exact_order, derived_observation, exact_model)
        with self.assertRaisesRegex(TypeError, "exact ExecutionModel"):
            simulate_execution(exact_order, exact_observation, derived_model)

    def test_simulation_revalidates_exact_objects_after_frozen_mutation(self):
        exact_observation = top()
        exact_model = model()

        mutated_order = order()
        object.__setattr__(mutated_order, "quantity", Decimal("-1"))
        with self.assertRaisesRegex(ExecutionRealismError, "quantity must be positive"):
            simulate_execution(mutated_order, exact_observation, exact_model)

        mutated_observation = top()
        object.__setattr__(
            mutated_observation,
            "available_at",
            "2026-09-24T09:59:59Z",
        )
        with self.assertRaisesRegex(
            ExecutionRealismError,
            "available_at cannot precede market_time",
        ):
            simulate_execution(order(), mutated_observation, exact_model)

        mutated_model = model()
        object.__setattr__(mutated_model, "latency_ms", -1)
        with self.assertRaisesRegex(ExecutionRealismError, "latency_ms must be"):
            simulate_execution(order(), exact_observation, mutated_model)

        nested_authority_model = model()
        object.__setattr__(
            nested_authority_model.price_projection,
            "instrument_version",
            "XYZ@v1",
        )
        with self.assertRaisesRegex(
            ExecutionRealismError,
            "projection instrument_version must match",
        ):
            simulate_execution(order(), exact_observation, nested_authority_model)

        injected_order = order()
        object.__setattr__(injected_order, "shadow_authority", "forged")
        with self.assertRaisesRegex(TypeError, "unexpected state fields"):
            simulate_execution(injected_order, exact_observation, exact_model)

        class HostileStateField(str):
            armed = False
            equality_calls = 0

            def __eq__(self, other):
                type(self).equality_calls += 1
                if type(self).armed:
                    raise AssertionError("hostile state-field equality executed")
                return super().__eq__(other)

            __hash__ = str.__hash__

        hostile_key_order = order()
        hostile_state = dict(hostile_key_order.__dict__)
        quantity = hostile_state.pop("quantity")
        hostile_state[HostileStateField("quantity")] = quantity
        object.__setattr__(hostile_key_order, "__dict__", hostile_state)
        HostileStateField.armed = True
        with self.assertRaisesRegex(TypeError, "non-canonical state field names"):
            simulate_execution(hostile_key_order, exact_observation, exact_model)
        self.assertEqual(HostileStateField.equality_calls, 0)

    def test_cross_instrument_liquidity_cannot_execute_order(self):
        with self.assertRaisesRegex(
            ExecutionRealismError,
            "instrument_version must exactly match",
        ):
            simulate_execution(
                order(instrument_version=INSTRUMENT_REF),
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
        # Exact raw projection is 101.0909; BUY rounds adversely to the next 0.01 tick.
        expected = Decimal("101.10")
        self.assertEqual(result.status, "FILLED")
        self.assertEqual(result.fill_price, expected)
        self.assertEqual(result.fee, Decimal("1.011"))

    def test_market_projection_is_invariant_to_ambient_decimal_context(self):
        def execute(*, side, precision, rounding):
            with localcontext() as context:
                context.prec = precision
                context.rounding = rounding
                return simulate_execution(
                    order(side=side, quantity="10"),
                    top(available_volume="30"),
                    model(max_participation="0.5"),
                )

        for side in ("BUY", "SELL"):
            low_floor = execute(side=side, precision=6, rounding=ROUND_FLOOR)
            low_ceiling = execute(side=side, precision=6, rounding=ROUND_CEILING)
            high_precision = execute(side=side, precision=80, rounding=ROUND_CEILING)
            self.assertEqual(low_floor, low_ceiling)
            self.assertEqual(low_floor, high_precision)
            self.assertEqual(low_floor.fill_price % Decimal("0.01"), Decimal("0"))
            if side == "BUY":
                self.assertGreaterEqual(low_floor.fill_price, Decimal("101"))
            else:
                self.assertLessEqual(low_floor.fill_price, Decimal("99"))

    def test_market_execution_rejects_off_grid_reference_before_projection(self):
        with self.assertRaisesRegex(
            ExecutionRealismError,
            "market reference price is not aligned to authoritative price quantum",
        ):
            simulate_execution(order(), top(ask="101.005"), model())

    def test_market_execution_fails_closed_without_matching_projection_authority(self):
        for observation in (
            top(),
            top(market_time="2026-09-24T10:00:00.050000Z"),
            top(available_volume="0"),
        ):
            with self.subTest(observation=observation):
                with self.assertRaisesRegex(
                    ExecutionRealismError,
                    "requires authoritative price projection policy",
                ):
                    simulate_execution(
                        order(),
                        observation,
                        model(price_projection=None),
                    )

        mismatched, mismatched_grid = price_authority(
            instrument_id=OTHER_INSTRUMENT_ID,
            provider_symbol="XYZ",
        )
        with self.assertRaisesRegex(
            ExecutionRealismError,
            "projection instrument_version must match",
        ):
            simulate_execution(
                order(),
                top(),
                model(
                    price_projection=mismatched,
                    price_grid=mismatched_grid,
                ),
            )

    def test_projection_policy_and_grid_are_bound_to_canonical_registry(self):
        instrument = canonical_instrument(price_tick="0.05")
        registry = InstrumentRegistry(
            calendars=(TradingCalendar.continuous_24_7(),),
            versions=(instrument,),
        )
        policy = ExecutionPriceProjectionPolicy.from_instrument(instrument)
        grid = ExecutionPriceGrid.from_registry(registry, INSTRUMENT_REF)
        self.assertEqual(policy.instrument_version, INSTRUMENT_REF)
        self.assertEqual(policy.price_quantum, Decimal("0.05"))
        self.assertEqual(grid.instrument_version, INSTRUMENT_REF)
        self.assertEqual(grid.price_quantum, Decimal("0.05"))
        self.assertEqual(
            grid.instrument_metadata_binding,
            instrument.metadata_evidence_binding().removeprefix("sha256:"),
        )

    def test_market_execution_requires_registry_issued_price_grid(self):
        with self.assertRaisesRegex(
            ExecutionRealismError,
            "requires canonical InstrumentRegistry price grid",
        ):
            simulate_execution(
                order(),
                top(),
                model(price_grid=None),
            )
        with self.assertRaisesRegex(
            TypeError,
            "must be issued by the canonical InstrumentRegistry",
        ):
            ExecutionPriceGrid(
                instrument_version=INSTRUMENT_REF,
                price_quantum=Decimal("0.01"),
                projection_policy_id="ADVERSE_INSTRUMENT_TICK",
                projection_policy_version="1",
                instrument_metadata_binding=INSTRUMENT_BINDING,
                source_evidence_binding=INSTRUMENT_BINDING,
            )

    def test_price_grid_issuance_ignores_instance_shadowed_registry_exact(self):
        instrument = canonical_instrument()
        registry = InstrumentRegistry(
            calendars=(TradingCalendar.continuous_24_7(),),
            versions=(instrument,),
        )
        forged_instrument = canonical_instrument(price_tick="100")
        forged_calls = 0

        def forged_exact(_instrument_version):
            nonlocal forged_calls
            forged_calls += 1
            return forged_instrument

        registry.exact = forged_exact
        grid = ExecutionPriceGrid.from_registry(registry, INSTRUMENT_REF)

        self.assertEqual(forged_calls, 0)
        self.assertEqual(grid.instrument_version, INSTRUMENT_REF)
        self.assertEqual(grid.price_quantum, Decimal("0.01"))
        self.assertEqual(
            grid.instrument_metadata_binding,
            instrument.metadata_evidence_binding(),
        )

    def test_price_grid_issuance_requires_exact_registry_and_metadata_evidence(self):
        with self.assertRaisesRegex(TypeError, "registry must be exact InstrumentRegistry"):
            ExecutionPriceGrid.from_registry(object(), INSTRUMENT_REF)

        unbound = replace(canonical_instrument(), metadata_evidence=())
        registry = InstrumentRegistry(
            calendars=(TradingCalendar.continuous_24_7(),),
            versions=(unbound,),
        )
        with self.assertRaisesRegex(
            ExecutionRealismError,
            "requires metadata evidence",
        ):
            ExecutionPriceGrid.from_registry(registry, INSTRUMENT_REF)

    def test_registry_price_grid_detects_post_issuance_mutation(self):
        exact_model = model()
        object.__setattr__(
            exact_model.price_grid,
            "price_quantum",
            Decimal("0.05"),
        )
        with self.assertRaisesRegex(
            ExecutionRealismError,
            "content changed after issuance",
        ):
            simulate_execution(order(), top(), exact_model)

    def test_projection_policy_changes_model_fingerprint(self):
        base = model()
        changed_projection, changed_grid = price_authority(price_tick="0.05")
        changed = model(
            price_projection=changed_projection,
            price_grid=changed_grid,
        )
        self.assertNotEqual(base.fingerprint, changed.fingerprint)

    def test_price_grid_source_evidence_changes_model_fingerprint(self):
        base = model()
        changed_projection, changed_grid = price_authority(
            evidence_sha256="e" * 64,
        )
        changed = model(
            price_projection=changed_projection,
            price_grid=changed_grid,
        )
        self.assertEqual(
            base.price_projection.instrument_metadata_binding,
            changed.price_projection.instrument_metadata_binding,
        )
        self.assertNotEqual(
            base.price_grid.source_evidence_binding,
            changed.price_grid.source_evidence_binding,
        )
        self.assertNotEqual(base.fingerprint, changed.fingerprint)

    def test_limit_execution_is_invariant_to_ambient_decimal_context(self):
        def execute(*, precision, rounding):
            with localcontext() as context:
                context.prec = precision
                context.rounding = rounding
                return simulate_execution(
                    order(
                        order_type="LIMIT",
                        quantity="9999999999999999999.99",
                        lot_size="0.01",
                        limit_price="102.12345678901234567890123456789",
                    ),
                    top(
                        available_volume="12345678901234567890.12",
                        ask="101",
                    ),
                    model(
                        max_participation="0.123456789012345678",
                        fee_rate="0.001234567890123456789",
                    ),
                )

        low_floor = execute(precision=6, rounding=ROUND_FLOOR)
        low_ceiling = execute(precision=6, rounding=ROUND_CEILING)
        high_precision = execute(precision=80, rounding=ROUND_CEILING)
        self.assertEqual(low_floor, low_ceiling)
        self.assertEqual(low_floor, high_precision)
        self.assertEqual(low_floor.status, "PARTIAL")
        self.assertGreater(low_floor.filled_quantity, Decimal("0"))

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

    def test_stop_trigger_is_recorded_even_when_fill_capacity_is_below_one_lot(self):
        result = simulate_execution(
            order(
                order_type="STOP_LIMIT",
                stop_price="100",
                limit_price="102",
                lot_size="5",
                quantity="10",
            ),
            top(
                ask="101",
                available_volume="0",
            ),
            model(),
        )
        self.assertEqual(result.status, "NO_FILL")
        self.assertEqual(result.filled_quantity, Decimal("0"))
        self.assertTrue(result.triggered)
        self.assertIn("stop triggered", result.reason)
        self.assertIn("below one lot", result.reason)


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
                instrument_version=INSTRUMENT_REF,
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
                instrument_version=INSTRUMENT_REF,
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
                instrument_version=INSTRUMENT_REF,
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
        # Raw adverse SELL projection is 89.865; FLOOR to the 0.01 tick is worse.
        self.assertEqual(result.fill_price, Decimal("89.86"))
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
                instrument_version=INSTRUMENT_REF,
                side="BUY",
                order_type="MARKET",
                quantity=Decimal("1.5"),
                submitted_at="2026-09-24T10:00:00Z",
                lot_size=Decimal("1"),
            )
        with self.assertRaises(ExecutionRealismError):
            LiquidityObservation(
                instrument_version=INSTRUMENT_REF,
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
                    instrument_version=INSTRUMENT_REF,
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
                instrument_version=INSTRUMENT_REF,
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
                instrument_version=INSTRUMENT_REF,
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
                instrument_version=INSTRUMENT_REF,
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
                    instrument_version=INSTRUMENT_REF,
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
