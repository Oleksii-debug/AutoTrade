from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import NAMESPACE_URL, uuid5

from autotrade_research.artifacts import ArtifactStore
from mvp.autotrade_mvp.execution_oracle import ExecutionOracleError
from mvp.autotrade_mvp.instruments import (
    InstrumentRegistry,
    InstrumentVersion,
    TradingCalendar,
)
from mvp.autotrade_mvp.execution_qualification import (
    ExecutionModelQualification,
    ExecutionQualificationError,
    simulate_qualified_execution,
    validate_execution_qualification,
)
from mvp.autotrade_mvp.execution_realism import (
    ExecutionModel,
    ExecutionPriceGrid,
    ExecutionPriceProjectionPolicy,
    LiquidityObservation,
    SimulatedOrder,
    simulate_execution,
)


CALIBRATION = "a" * 64
INSTRUMENT_ID = "11111111-1111-4111-8111-111111111111"
INSTRUMENT_REF = f"{INSTRUMENT_ID}@1"
PROTOCOL = "b" * 64
EVIDENCE_BYTES = b"frozen execution qualification evidence v1"
EVIDENCE = sha256(EVIDENCE_BYTES).hexdigest()
ARTIFACT_ID = str(uuid5(NAMESPACE_URL, "autotrade:wp13:execution-evidence"))
INSTRUMENT_METADATA_ARTIFACT_ID = str(
    uuid5(NAMESPACE_URL, "autotrade:wp13:instrument-metadata")
)
INSTRUMENT_METADATA_BYTES = (
    b'{"instrument_version":"' + INSTRUMENT_REF.encode("ascii")
    + b'","price_tick":"0.01"}'
)
INSTRUMENT_METADATA_SHA256 = sha256(INSTRUMENT_METADATA_BYTES).hexdigest()
INSTRUMENT_METADATA_OBSERVED_AT = "2025-12-30T10:00:00Z"


class _FixedArtifactClock(datetime):
    @classmethod
    def now(cls, tz=None):
        value = datetime(2025, 12, 30, 12, 0, 0, tzinfo=timezone.utc)
        return value if tz is None else value.astimezone(tz)


class _LateArtifactClock(datetime):
    @classmethod
    def now(cls, tz=None):
        value = datetime(2026, 9, 25, 12, 0, 0, tzinfo=timezone.utc)
        return value if tz is None else value.astimezone(tz)


def instrument(*, price_tick="0.01", metadata_evidence=None):
    return InstrumentVersion(
        instrument_id=INSTRUMENT_ID,
        version=1,
        provider_id="simulated",
        venue_id="simulated-venue",
        provider_symbol="ABC",
        asset_class="CASH_EQUITY",
        base_currency="ABC",
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
        effective_to=datetime(2026, 12, 31, tzinfo=timezone.utc),
        metadata_evidence=(
            ({
                "artifact_id": INSTRUMENT_METADATA_ARTIFACT_ID,
                "sha256": f"sha256:{INSTRUMENT_METADATA_SHA256}",
                "observed_at": INSTRUMENT_METADATA_OBSERVED_AT,
            },)
            if metadata_evidence is None
            else metadata_evidence
        ),
    )


def model(**overrides):
    selected_instrument = instrument()
    registry = InstrumentRegistry(
        calendars=(TradingCalendar.continuous_24_7(),),
        versions=(selected_instrument,),
    )
    values = dict(
        model_version="exec-realism-v1",
        calibration_sha256=CALIBRATION,
        data_fidelity="TOP_OF_BOOK",
        scenario="BASE",
        latency_ms=0,
        fee_rate="0.001",
        minimum_fee="0",
        max_participation="0.25",
        slippage_bps="5",
        impact_bps_at_max_participation="10",
        bar_half_spread_bps="0",
        scenario_cost_multiplier="1",
        price_projection=ExecutionPriceProjectionPolicy.from_instrument(
            selected_instrument
        ),
        price_grid=ExecutionPriceGrid.from_registry(
            registry,
            INSTRUMENT_REF,
        ),
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


def observation(**overrides):
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


def qualification(exec_model, **overrides):
    values = dict(
        qualification_id="q-1",
        asset_class="CASH_EQUITY",
        data_fidelity=exec_model.data_fidelity,
        scenario=exec_model.scenario,
        purpose="REPLAY",
        model_fingerprint=exec_model.fingerprint,
        calibration_sha256=exec_model.calibration_sha256,
        protocol_sha256=PROTOCOL,
        evidence_artifact_id=ARTIFACT_ID,
        evidence_sha256=EVIDENCE,
        instrument_version=INSTRUMENT_REF,
    )
    values.update(overrides)
    return ExecutionModelQualification(**values)


class ExecutionQualificationTests(unittest.TestCase):
    def setUp(self):
        self._temp = TemporaryDirectory()
        self.store = ArtifactStore(Path(self._temp.name) / "artifacts")
        with patch(
            "autotrade_research.artifacts._crash_atomic_publication.datetime",
            _FixedArtifactClock,
        ):
            self.store.publish_bytes(
                artifact_id=INSTRUMENT_METADATA_ARTIFACT_ID,
                data=INSTRUMENT_METADATA_BYTES,
                media_type="application/vnd.autotrade.instrument-metadata+json",
                rights={"storage": True, "export": False},
                source_refs=["provider:simulated:instrument:ABC"],
                metadata={
                    "kind": "instrument-metadata",
                    "instrument_version_binding": instrument().metadata_evidence_binding(),
                },
            )
            self.store.publish_bytes(
                artifact_id=ARTIFACT_ID,
                data=EVIDENCE_BYTES,
                media_type="application/json",
                rights={"storage": True, "export": False},
                source_refs=["protocol:wp13"],
                metadata={"kind": "execution-qualification-evidence"},
            )

    def tearDown(self):
        self._temp.cleanup()

    def validation_kwargs(self, exec_model, **overrides):
        values = dict(
            model=exec_model,
            qualification=qualification(exec_model),
            instrument=instrument(),
            asset_class="CASH_EQUITY",
            instrument_version=INSTRUMENT_REF,
            protocol_sha256=PROTOCOL,
            artifact_store=self.store,
            evidence_artifact_id=ARTIFACT_ID,
            purpose="REPLAY",
        )
        values.update(overrides)
        return values

    def test_qualified_wrapper_rejects_polymorphic_inputs_before_authority_reads(self):
        class OrderAlias(SimulatedOrder):
            pass

        class ModelAlias(ExecutionModel):
            pass

        base_order = order()
        aliased_order = OrderAlias(**base_order.__dict__)
        with self.assertRaisesRegex(TypeError, "order must be exact SimulatedOrder"):
            simulate_qualified_execution(
                order=aliased_order,
                observation=observation(),
                model=model(),
                qualification=qualification(model()),
                instrument=instrument(),
                asset_class="CASH_EQUITY",
                protocol_sha256=PROTOCOL,
                artifact_store=self.store,
                evidence_artifact_id=ARTIFACT_ID,
            )

        base_model = model()
        aliased_model = ModelAlias(**base_model.__dict__)
        with self.assertRaisesRegex(TypeError, "model must be exact ExecutionModel"):
            validate_execution_qualification(
                **self.validation_kwargs(aliased_model)
            )

    def test_qualification_text_ingress_rejects_hostile_subclasses_without_callbacks(self):
        class HostileText(str):
            strip_calls = 0

            def strip(self, *args, **kwargs):
                type(self).strip_calls += 1
                raise AssertionError("hostile qualification text callback executed")

        exec_model = model()
        with self.assertRaisesRegex(
            ExecutionQualificationError,
            "asset_class is required",
        ):
            validate_execution_qualification(
                **self.validation_kwargs(
                    exec_model,
                    asset_class=HostileText("EQUITY"),
                )
            )
        self.assertEqual(HostileText.strip_calls, 0)

        mutated = qualification(exec_model)
        object.__setattr__(mutated, "purpose", HostileText("REPLAY"))
        with self.assertRaisesRegex(
            TypeError,
            "qualification.purpose must be exact str",
        ):
            validate_execution_qualification(
                **self.validation_kwargs(
                    exec_model,
                    qualification=mutated,
                )
            )
        self.assertEqual(HostileText.strip_calls, 0)

    def test_exact_qualified_model_can_execute_simulation(self):
        exec_model = model()
        result = simulate_qualified_execution(
            order=order(),
            observation=observation(),
            model=exec_model,
            qualification=qualification(exec_model),
            instrument=instrument(),
            asset_class="CASH_EQUITY",
            protocol_sha256=PROTOCOL,
            artifact_store=self.store,
            evidence_artifact_id=ARTIFACT_ID,
            purpose="REPLAY",
        )
        self.assertEqual(result.status, "FILLED")
        self.assertGreater(result.fill_price, Decimal("101"))

    def test_qualified_order_lot_size_cannot_undercut_instrument_quantity_step(self):
        exec_model = model()
        with self.assertRaisesRegex(
            ExecutionQualificationError,
            "instrument_quantity_step",
        ):
            simulate_qualified_execution(
                order=order(lot_size="0.1"),
                observation=observation(),
                model=exec_model,
                qualification=qualification(exec_model),
                instrument=instrument(),
                asset_class="CASH_EQUITY",
                protocol_sha256=PROTOCOL,
                artifact_store=self.store,
                evidence_artifact_id=ARTIFACT_ID,
                purpose="REPLAY",
            )

    def test_qualified_limit_price_must_match_instrument_price_tick(self):
        exec_model = model()
        with self.assertRaisesRegex(
            ExecutionQualificationError,
            "instrument_limit_price_rules",
        ):
            simulate_qualified_execution(
                order=order(
                    order_type="LIMIT",
                    limit_price="101.005",
                ),
                observation=observation(),
                model=exec_model,
                qualification=qualification(exec_model),
                instrument=instrument(),
                asset_class="CASH_EQUITY",
                protocol_sha256=PROTOCOL,
                artifact_store=self.store,
                evidence_artifact_id=ARTIFACT_ID,
                purpose="REPLAY",
            )

    def test_qualified_liquidity_price_must_match_instrument_price_tick(self):
        exec_model = model()
        with self.assertRaisesRegex(
            ExecutionQualificationError,
            "instrument_ask_rules",
        ):
            simulate_qualified_execution(
                order=order(
                    order_type="LIMIT",
                    limit_price="102",
                ),
                observation=observation(ask="101.005"),
                model=exec_model,
                qualification=qualification(exec_model),
                instrument=instrument(),
                asset_class="CASH_EQUITY",
                protocol_sha256=PROTOCOL,
                artifact_store=self.store,
                evidence_artifact_id=ARTIFACT_ID,
                purpose="REPLAY",
            )

    def test_qualified_order_cannot_precede_instrument_effective_interval(self):
        exec_model = model()
        with self.assertRaisesRegex(
            ExecutionQualificationError,
            "instrument_effective_at_order",
        ):
            simulate_qualified_execution(
                order=order(submitted_at="2025-12-31T23:59:59Z"),
                observation=observation(
                    market_time="2026-01-01T00:00:00.200000Z",
                    available_at="2026-01-01T00:00:00.250000Z",
                ),
                model=exec_model,
                qualification=qualification(exec_model),
                instrument=instrument(),
                asset_class="CASH_EQUITY",
                protocol_sha256=PROTOCOL,
                artifact_store=self.store,
                evidence_artifact_id=ARTIFACT_ID,
                purpose="REPLAY",
            )

    def test_qualified_market_observation_cannot_outlive_instrument_version(self):
        exec_model = model()
        with self.assertRaisesRegex(
            ExecutionQualificationError,
            "instrument_effective_at_market",
        ):
            simulate_qualified_execution(
                order=order(),
                observation=observation(
                    market_time="2027-01-01T00:00:00.200000Z",
                    available_at="2027-01-01T00:00:00.250000Z",
                ),
                model=exec_model,
                qualification=qualification(exec_model),
                instrument=instrument(),
                asset_class="CASH_EQUITY",
                protocol_sha256=PROTOCOL,
                artifact_store=self.store,
                evidence_artifact_id=ARTIFACT_ID,
                purpose="REPLAY",
            )

    def test_qualification_rejects_instrument_without_authenticated_metadata(self):
        exec_model = model()
        unbound = replace(instrument(), metadata_evidence=())
        with self.assertRaisesRegex(
            ExecutionQualificationError,
            "requires authenticated instrument metadata evidence",
        ):
            validate_execution_qualification(
                **self.validation_kwargs(
                    exec_model,
                    instrument=unbound,
                )
            )

    def test_qualified_replay_rejects_metadata_committed_after_order_cut(self):
        exec_model = model()
        with TemporaryDirectory() as directory:
            late_store = ArtifactStore(Path(directory) / "artifacts")
            with patch(
                "autotrade_research.artifacts._crash_atomic_publication.datetime",
                _LateArtifactClock,
            ):
                late_store.publish_bytes(
                    artifact_id=INSTRUMENT_METADATA_ARTIFACT_ID,
                    data=INSTRUMENT_METADATA_BYTES,
                    media_type="application/vnd.autotrade.instrument-metadata+json",
                    rights={"storage": True, "export": False},
                    source_refs=["provider:simulated:instrument:ABC"],
                    metadata={
                        "kind": "instrument-metadata",
                        "instrument_version_binding": instrument().metadata_evidence_binding(),
                    },
                )
                late_store.publish_bytes(
                    artifact_id=ARTIFACT_ID,
                    data=EVIDENCE_BYTES,
                    media_type="application/json",
                    rights={"storage": True, "export": False},
                    source_refs=["protocol:wp13"],
                    metadata={"kind": "execution-qualification-evidence"},
                )
            with self.assertRaisesRegex(
                ExecutionQualificationError,
                "requires authenticated instrument metadata evidence",
            ):
                simulate_qualified_execution(
                    order=order(),
                    observation=observation(),
                    model=exec_model,
                    qualification=qualification(exec_model),
                    instrument=instrument(),
                    asset_class="CASH_EQUITY",
                    protocol_sha256=PROTOCOL,
                    artifact_store=late_store,
                    evidence_artifact_id=ARTIFACT_ID,
                    purpose="REPLAY",
                )

    def test_cost_assumption_change_invalidates_qualification(self):
        qualified = model(slippage_bps="5")
        changed = model(slippage_bps="6")
        with self.assertRaisesRegex(ExecutionQualificationError, "model_fingerprint"):
            validate_execution_qualification(
                **self.validation_kwargs(
                    changed,
                    qualification=qualification(qualified),
                )
            )

    def test_calibration_change_invalidates_qualification(self):
        exec_model = model(calibration_sha256="d" * 64)
        stale = qualification(exec_model, calibration_sha256="a" * 64)
        with self.assertRaisesRegex(
            ExecutionQualificationError,
            "calibration_sha256",
        ):
            validate_execution_qualification(
                **self.validation_kwargs(exec_model, qualification=stale)
            )

    def test_asset_class_is_a_qualification_dimension(self):
        exec_model = model()
        with self.assertRaisesRegex(ExecutionQualificationError, "asset_class"):
            validate_execution_qualification(
                **self.validation_kwargs(
                    exec_model,
                    qualification=qualification(exec_model, asset_class="CRYPTO_SPOT"),
                )
            )

    def test_caller_asset_class_must_match_canonical_instrument(self):
        exec_model = model()
        with self.assertRaisesRegex(
            ExecutionQualificationError,
            "instrument_asset_class",
        ):
            validate_execution_qualification(
                **self.validation_kwargs(
                    exec_model,
                    asset_class="CRYPTO_SPOT",
                    qualification=qualification(
                        exec_model,
                        asset_class="CRYPTO_SPOT",
                    ),
                )
            )

    def test_data_fidelity_is_a_qualification_dimension(self):
        qualified_model = model(data_fidelity="TOP_OF_BOOK")
        different_model = model(data_fidelity="BOOK")
        with self.assertRaisesRegex(
            ExecutionQualificationError,
            "data_fidelity",
        ):
            validate_execution_qualification(
                **self.validation_kwargs(
                    different_model,
                    qualification=qualification(qualified_model),
                )
            )

    def test_protocol_digest_must_match_frozen_experiment(self):
        exec_model = model()
        with self.assertRaisesRegex(
            ExecutionQualificationError,
            "protocol_sha256",
        ):
            validate_execution_qualification(
                **self.validation_kwargs(
                    exec_model,
                    protocol_sha256="f" * 64,
                )
            )

    def test_expected_digest_must_match_resolved_immutable_artifact(self):
        exec_model = model()
        stale = qualification(exec_model, evidence_sha256="d" * 64)
        with self.assertRaisesRegex(
            ExecutionQualificationError,
            "evidence_sha256",
        ):
            validate_execution_qualification(
                **self.validation_kwargs(exec_model, qualification=stale)
            )

    def test_different_resolved_bytes_fail_even_when_frozen_digest_is_unchanged(self):
        exec_model = model()
        with TemporaryDirectory() as directory:
            tampered_store = ArtifactStore(Path(directory) / "artifacts")
            tampered_store.publish_bytes(
                artifact_id=ARTIFACT_ID,
                data=b"different immutable evidence bytes",
                media_type="application/json",
                rights={"storage": True, "export": False},
            )
            with self.assertRaisesRegex(
                ExecutionQualificationError,
                "evidence_sha256",
            ):
                validate_execution_qualification(
                    **self.validation_kwargs(
                        exec_model,
                        artifact_store=tampered_store,
                    )
                )

    def test_missing_evidence_artifact_fails_closed(self):
        exec_model = model()
        with TemporaryDirectory() as directory:
            empty_store = ArtifactStore(Path(directory) / "artifacts")
            with self.assertRaisesRegex(
                ExecutionQualificationError,
                "cannot be verified",
            ):
                validate_execution_qualification(
                    **self.validation_kwargs(
                        exec_model,
                        artifact_store=empty_store,
                    )
                )

    def test_artifact_id_alias_cannot_rebind_same_bytes(self):
        exec_model = model()
        alias_id = str(uuid5(NAMESPACE_URL, "autotrade:wp13:alias"))
        self.store.publish_bytes(
            artifact_id=alias_id,
            data=EVIDENCE_BYTES,
            media_type="application/json",
            rights={"storage": True, "export": False},
        )
        with self.assertRaisesRegex(
            ExecutionQualificationError,
            "evidence_artifact_id",
        ):
            validate_execution_qualification(
                **self.validation_kwargs(
                    exec_model,
                    evidence_artifact_id=alias_id,
                )
            )

    def test_qualified_projection_must_match_canonical_instrument_price_tick(self):
        exec_model = model()
        with self.assertRaisesRegex(
            ExecutionQualificationError,
            "price_projection_authority",
        ):
            validate_execution_qualification(
                **self.validation_kwargs(
                    exec_model,
                    instrument=instrument(price_tick="0.05"),
                )
            )

    def test_qualification_requires_exact_canonical_instrument_type(self):
        exec_model = model()
        with self.assertRaisesRegex(TypeError, "instrument must be exact InstrumentVersion"):
            validate_execution_qualification(
                **self.validation_kwargs(exec_model, instrument=object())
            )

    def test_instrument_specific_qualification_cannot_cross_instrument(self):
        exec_model = model()
        with self.assertRaisesRegex(
            ExecutionQualificationError,
            "instrument_version",
        ):
            validate_execution_qualification(
                **self.validation_kwargs(
                    exec_model,
                    qualification=qualification(
                        exec_model,
                        instrument_version=INSTRUMENT_REF,
                    ),
                    instrument_version="22222222-2222-4222-8222-222222222222@2",
                )
            )

    def test_optimistic_model_cannot_be_qualified_for_promotion(self):
        exec_model = model(
            scenario="OPTIMISTIC",
            scenario_cost_multiplier="0.5",
        )
        with self.assertRaisesRegex(
            ExecutionQualificationError,
            "OPTIMISTIC scenario cannot qualify promotion evidence",
        ):
            qualification(exec_model, purpose="PROMOTION")

    def test_research_qualification_cannot_be_reused_for_promotion(self):
        exec_model = model()
        with self.assertRaisesRegex(ExecutionQualificationError, "purpose"):
            validate_execution_qualification(
                **self.validation_kwargs(
                    exec_model,
                    qualification=qualification(exec_model, purpose="RESEARCH"),
                    purpose="PROMOTION",
                )
            )

    def test_unknown_asset_class_fails_closed(self):
        exec_model = model()
        with self.assertRaisesRegex(
            ExecutionQualificationError,
            "unsupported asset_class",
        ):
            qualification(exec_model, asset_class="MYSTERY")

    def test_invalid_evidence_digest_fails_closed(self):
        exec_model = model()
        with self.assertRaisesRegex(
            ExecutionQualificationError,
            "evidence_sha256 must be a SHA-256 digest",
        ):
            qualification(exec_model, evidence_sha256="not-a-digest")

    def test_invalid_evidence_artifact_id_fails_closed(self):
        exec_model = model()
        with self.assertRaisesRegex(
            ExecutionQualificationError,
            "evidence_artifact_id must be a UUID",
        ):
            qualification(exec_model, evidence_artifact_id="not-a-uuid")

    def test_qualified_path_rejects_omitted_observed_stop_trigger(self):
        exec_model = model()
        simulated_order = order(
            order_type="STOP_LIMIT",
            limit_price="102",
            stop_price="100",
        )
        liquidity = observation(ask="101")
        valid = simulate_execution(simulated_order, liquidity, exec_model)
        self.assertEqual(valid.filled_quantity, Decimal("0"))
        self.assertTrue(valid.triggered)
        forged = replace(valid, triggered=False)
        with patch(
            "mvp.autotrade_mvp.execution_qualification.simulate_execution",
            return_value=forged,
        ):
            with self.assertRaisesRegex(
                ExecutionOracleError,
                "observed stop trigger cannot be omitted",
            ):
                simulate_qualified_execution(
                    order=simulated_order,
                    observation=liquidity,
                    model=exec_model,
                    qualification=qualification(exec_model),
                    instrument=instrument(),
                    asset_class="CASH_EQUITY",
                    protocol_sha256=PROTOCOL,
                    artifact_store=self.store,
                    evidence_artifact_id=ARTIFACT_ID,
                    purpose="REPLAY",
                )

    def test_qualified_path_rejects_stop_trigger_state_regression(self):
        exec_model = model()
        simulated_order = order(
            order_type="STOP_LIMIT",
            limit_price="102",
            stop_price="100",
            already_triggered=True,
        )
        liquidity = observation(ask="101")
        valid = simulate_execution(simulated_order, liquidity, exec_model)
        self.assertIn(valid.status, {"FILLED", "PARTIAL"})
        self.assertTrue(valid.triggered)
        forged = replace(valid, triggered=False)
        with patch(
            "mvp.autotrade_mvp.execution_qualification.simulate_execution",
            return_value=forged,
        ):
            with self.assertRaisesRegex(
                ExecutionOracleError,
                "triggered state cannot regress",
            ):
                simulate_qualified_execution(
                    order=simulated_order,
                    observation=liquidity,
                    model=exec_model,
                    qualification=qualification(exec_model),
                    instrument=instrument(),
                    asset_class="CASH_EQUITY",
                    protocol_sha256=PROTOCOL,
                    artifact_store=self.store,
                    evidence_artifact_id=ARTIFACT_ID,
                    purpose="REPLAY",
                )

    def test_qualified_path_rejects_forged_limit_fill_without_liquidity(self):
        exec_model = model()
        simulated_order = order(order_type="LIMIT", limit_price="100")
        liquidity = observation(ask="101")
        no_fill = simulate_execution(simulated_order, liquidity, exec_model)
        self.assertEqual(no_fill.status, "NO_FILL")
        forged = replace(
            no_fill,
            status="FILLED",
            filled_quantity=Decimal("10"),
            fill_price=Decimal("100"),
            fee=Decimal("1"),
            trade_time=liquidity.market_time,
        )
        with patch(
            "mvp.autotrade_mvp.execution_qualification.simulate_execution",
            return_value=forged,
        ):
            with self.assertRaisesRegex(
                ExecutionOracleError,
                "independently executable price evidence",
            ):
                simulate_qualified_execution(
                    order=simulated_order,
                    observation=liquidity,
                    model=exec_model,
                    qualification=qualification(exec_model),
                    instrument=instrument(),
                    asset_class="CASH_EQUITY",
                    protocol_sha256=PROTOCOL,
                    artifact_store=self.store,
                    evidence_artifact_id=ARTIFACT_ID,
                    purpose="REPLAY",
                )

    def test_qualified_path_requires_independent_oracle(self):
        exec_model = model()
        valid = simulate_qualified_execution(
            order=order(),
            observation=observation(),
            model=exec_model,
            qualification=qualification(exec_model),
            instrument=instrument(),
            asset_class="CASH_EQUITY",
            protocol_sha256=PROTOCOL,
            artifact_store=self.store,
            evidence_artifact_id=ARTIFACT_ID,
            purpose="REPLAY",
        )
        self.assertEqual(valid.status, "FILLED")
        forged = replace(valid, fill_price=Decimal("100"))
        with patch(
            "mvp.autotrade_mvp.execution_qualification.simulate_execution",
            return_value=forged,
        ):
            with self.assertRaisesRegex(ExecutionOracleError, "more favorable"):
                simulate_qualified_execution(
                    order=order(),
                    observation=observation(),
                    model=exec_model,
                    qualification=qualification(exec_model),
                    instrument=instrument(),
                    asset_class="CASH_EQUITY",
                    protocol_sha256=PROTOCOL,
                    artifact_store=self.store,
                    evidence_artifact_id=ARTIFACT_ID,
                    purpose="REPLAY",
                )


if __name__ == "__main__":
    unittest.main()
