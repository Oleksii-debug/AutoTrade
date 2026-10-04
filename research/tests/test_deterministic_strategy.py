from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import unittest

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

from research.autotrade_research.strategies.deterministic import (
    BreakoutThresholdBaseline,
    CausalObservation,
    DeterministicProposal,
    MeanReversionThresholdBaseline,
    NoTradeBaseline,
    ReturnThresholdBaseline,
    StrategyComparisonSnapshot,
    StrategyDescriptor,
    StrategyEconomicsBinding,
    bind_strategy_economics,
    compare_deterministic_strategies,
    run_baseline,
    to_decision_proposal,
)


BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[2]


def obs(i, price, *, available=None):
    return CausalObservation.create(
        event_id=f"event-{i}",
        symbol="AAA",
        available_at=available or BASE + timedelta(minutes=i),
        price=price,
    )


def economics_binding(
    proposal,
    *,
    instrument_version,
    available_at=None,
    after_cost_lower_bound="0.01",
    max_feasible_quantity="2",
    lot_size="1",
    required_evidence_dimensions=(),
    dimension_evidence=(),
    status="QUALIFIED",
    input_manifest_refs=("sha256:" + "c" * 64,),
):
    return StrategyEconomicsBinding(
        strategy_fingerprint=proposal.strategy_fingerprint,
        strategy_configuration_fingerprint=(
            proposal.strategy_configuration_fingerprint
        ),
        instrument_version=instrument_version,
        information_cutoff=proposal.information_cutoff,
        decision_time=proposal.decision_time,
        horizon_seconds=proposal.horizon_seconds,
        expiry=proposal.expiry,
        available_at=available_at or proposal.information_cutoff,
        input_manifest_refs=input_manifest_refs,
        gross_return_distribution_sha256="sha256:" + "d" * 64,
        after_cost_return_distribution_sha256="sha256:" + "e" * 64,
        after_cost_lower_bound=after_cost_lower_bound,
        execution_model_fingerprint="sha256:" + "f" * 64,
        execution_calibration_sha256="sha256:" + "1" * 64,
        execution_fidelity="FROZEN_EX_ANTE",
        capacity_assessment_sha256="sha256:" + "2" * 64,
        max_feasible_quantity=max_feasible_quantity,
        lot_size=lot_size,
        required_evidence_dimensions=required_evidence_dimensions,
        dimension_evidence=dimension_evidence,
        status=status,
    )


class DeterministicStrategyTests(unittest.TestCase):
    def test_direct_observation_cannot_bypass_exact_causal_invariants(self):
        with self.assertRaises(TypeError):
            CausalObservation(
                event_id="direct-float",
                symbol="AAA",
                available_at=BASE,
                price=100.1,
            )
        with self.assertRaises(ValueError):
            CausalObservation(
                event_id="direct-naive",
                symbol="AAA",
                available_at=datetime(2026, 1, 1),
                price=Decimal("100"),
            )
        with self.assertRaises(ValueError):
            CausalObservation(
                event_id="direct-zero",
                symbol="AAA",
                available_at=BASE,
                price=Decimal("0"),
            )

        normalized = CausalObservation(
            event_id=" event-direct ",
            symbol=" AAA ",
            available_at=BASE.astimezone(timezone(timedelta(hours=2))),
            price="100.00",
        )
        self.assertEqual(normalized.event_id, "event-direct")
        self.assertEqual(normalized.symbol, "AAA")
        self.assertEqual(normalized.available_at, BASE)
        self.assertEqual(normalized.price, Decimal("100.00"))

    def test_future_observation_is_rejected(self):
        strategy = ReturnThresholdBaseline(lookback=2, threshold="0.01", proposal_quantity="1")
        with self.assertRaises(ValueError):
            strategy.ingest(obs(1, "101"), simulation_time=BASE)

    def test_zero_model_path_never_claims_edge(self):
        strategy = ReturnThresholdBaseline(lookback=2, threshold="0.01", proposal_quantity="2")
        proposal = run_baseline(
            strategy,
            [obs(0, "100"), obs(1, "102")],
            decision_time=BASE + timedelta(minutes=1),
            symbol="AAA",
        )
        self.assertEqual(proposal.action, "BUY")
        self.assertEqual(proposal.quantity, Decimal("2"))
        self.assertEqual(proposal.model_calls, 0)
        self.assertEqual(proposal.economic_edge_claim, "UNPROVEN")

    def test_insufficient_history_fails_to_hold_not_fabricated_signal(self):
        strategy = ReturnThresholdBaseline(lookback=3, threshold="0.01", proposal_quantity="1")
        proposal = run_baseline(
            strategy,
            [obs(0, "100"), obs(1, "110")],
            decision_time=BASE + timedelta(minutes=1),
            symbol="AAA",
        )
        self.assertEqual(proposal.action, "HOLD")
        self.assertEqual(proposal.quantity, Decimal("0"))

    def test_snapshot_resume_matches_uninterrupted_state(self):
        uninterrupted = ReturnThresholdBaseline(lookback=3, threshold="0.01", proposal_quantity="1")
        for item in [obs(0, "100"), obs(1, "101")]:
            uninterrupted.ingest(item, simulation_time=item.available_at)
        restored = ReturnThresholdBaseline.restore(uninterrupted.snapshot())

        final = obs(2, "103")
        uninterrupted.ingest(final, simulation_time=final.available_at)
        restored.ingest(final, simulation_time=final.available_at)

        a = uninterrupted.propose(symbol="AAA", decision_time=final.available_at)
        b = restored.propose(symbol="AAA", decision_time=final.available_at)
        self.assertEqual(a, b)

    def test_duplicate_event_is_idempotent(self):
        strategy = ReturnThresholdBaseline(lookback=2, threshold="0.01", proposal_quantity="1")
        item = obs(0, "100")
        self.assertTrue(strategy.ingest(item, simulation_time=item.available_at))
        self.assertFalse(strategy.ingest(item, simulation_time=item.available_at))

    def test_duplicate_event_id_with_changed_price_fails_closed(self):
        strategy = ReturnThresholdBaseline(lookback=2, threshold="0.01", proposal_quantity="1")
        original = obs(0, "100")
        conflicting = CausalObservation.create(
            event_id=original.event_id,
            symbol=original.symbol,
            available_at=original.available_at,
            price="101",
        )
        strategy.ingest(original, simulation_time=original.available_at)
        with self.assertRaisesRegex(ValueError, "different observation content"):
            strategy.ingest(conflicting, simulation_time=conflicting.available_at)

    def test_duplicate_event_id_cannot_move_between_symbols(self):
        strategy = ReturnThresholdBaseline(lookback=2, threshold="0.01", proposal_quantity="1")
        original = obs(0, "100")
        conflicting = CausalObservation.create(
            event_id=original.event_id,
            symbol="BBB",
            available_at=original.available_at,
            price=original.price,
        )
        strategy.ingest(original, simulation_time=original.available_at)
        with self.assertRaisesRegex(ValueError, "different observation content"):
            strategy.ingest(conflicting, simulation_time=conflicting.available_at)


    def test_snapshot_remembers_evicted_event_for_restart_idempotency(self):
        strategy = ReturnThresholdBaseline(lookback=2, threshold="0.01", proposal_quantity="1")
        first, second, third = obs(0, "100"), obs(1, "101"), obs(2, "102")
        for item in (first, second, third):
            strategy.ingest(item, simulation_time=item.available_at)

        restored = ReturnThresholdBaseline.restore(strategy.snapshot())
        self.assertFalse(restored.ingest(first, simulation_time=third.available_at))
        proposal = restored.propose(symbol="AAA", decision_time=third.available_at)
        self.assertEqual(proposal.evidence_event_ids, ("event-1", "event-2"))

    def test_snapshot_remembers_evicted_event_content_and_rejects_conflict(self):
        strategy = ReturnThresholdBaseline(lookback=2, threshold="0.01", proposal_quantity="1")
        first, second, third = obs(0, "100"), obs(1, "101"), obs(2, "102")
        for item in (first, second, third):
            strategy.ingest(item, simulation_time=item.available_at)

        restored = ReturnThresholdBaseline.restore(strategy.snapshot())
        conflicting = CausalObservation.create(
            event_id=first.event_id,
            symbol=first.symbol,
            available_at=first.available_at,
            price="999",
        )
        with self.assertRaisesRegex(ValueError, "different observation content"):
            restored.ingest(conflicting, simulation_time=third.available_at)

    def test_snapshot_rejects_seen_event_that_conflicts_with_retained_history(self):
        strategy = ReturnThresholdBaseline(lookback=2, threshold="0.01", proposal_quantity="1")
        first, second = obs(0, "100"), obs(1, "101")
        for item in (first, second):
            strategy.ingest(item, simulation_time=item.available_at)
        payload = json.loads(strategy.snapshot())
        payload["seen_events"][first.event_id]["price"] = "999"

        with self.assertRaisesRegex(ValueError, "conflicts with retained history"):
            ReturnThresholdBaseline.restore(
                json.dumps(payload, sort_keys=True, separators=(",", ":"))
            )

    def test_snapshot_rejects_missing_seen_event_for_retained_history(self):
        strategy = ReturnThresholdBaseline(lookback=2, threshold="0.01", proposal_quantity="1")
        first, second = obs(0, "100"), obs(1, "101")
        for item in (first, second):
            strategy.ingest(item, simulation_time=item.available_at)
        payload = json.loads(strategy.snapshot())
        del payload["seen_events"][first.event_id]

        with self.assertRaisesRegex(ValueError, "missing retained history"):
            ReturnThresholdBaseline.restore(
                json.dumps(payload, sort_keys=True, separators=(",", ":"))
            )

    def test_schema_v1_snapshot_remains_readable(self):
        snapshot_v1 = json.dumps(
            {
                "schema_version": 1,
                "lookback": 2,
                "threshold": "0.01",
                "proposal_quantity": "1",
                "history": {
                    "AAA": [
                        {
                            "event_id": "event-0",
                            "available_at": BASE.isoformat(),
                            "price": "100",
                        },
                        {
                            "event_id": "event-1",
                            "available_at": (BASE + timedelta(minutes=1)).isoformat(),
                            "price": "101",
                        },
                    ]
                },
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        restored = ReturnThresholdBaseline.restore(snapshot_v1)
        proposal = restored.propose(
            symbol="AAA",
            decision_time=BASE + timedelta(minutes=1),
        )
        self.assertEqual(proposal.evidence_event_ids, ("event-0", "event-1"))

    def test_out_of_order_availability_is_rejected(self):
        strategy = ReturnThresholdBaseline(lookback=2, threshold="0.01", proposal_quantity="1")
        later = obs(2, "102")
        earlier = obs(1, "101")
        strategy.ingest(later, simulation_time=later.available_at)
        with self.assertRaises(ValueError):
            strategy.ingest(earlier, simulation_time=later.available_at)


    def descriptor(self, **overrides):
        values = dict(
            strategy_id="return-threshold-baseline",
            version=1,
            family="DETERMINISTIC_RETURN_THRESHOLD",
            feature_schema="price-only-v1",
            market_requirements=("CAUSAL_PRICE",),
            minimum_history=2,
            horizon_seconds=3600,
            decision_schedule="ON_REGISTERED_CUTOFF",
            proposal_semantics="BUY_SELL_HOLD_RESEARCH_PROPOSAL",
            parameter_bounds=(
                ("threshold", "0", "0.10"),
                ("proposal_quantity", "0.0001", "100"),
            ),
            resource_profile="CPU_LIGHT_ZERO_MODEL",
            supported_regimes=("UNSPECIFIED",),
            source_license="FIRST_PARTY",
            evaluation_protocol_sha256="sha256:" + "a" * 64,
            artifact_sha256="sha256:" + "b" * 64,
        )
        values.update(overrides)
        return StrategyDescriptor(**values)

    def test_descriptor_is_deterministic_versioned_identity(self):
        descriptor = self.descriptor()
        same = self.descriptor()
        changed = self.descriptor(version=2)
        self.assertEqual(descriptor.fingerprint, same.fingerprint)
        self.assertNotEqual(descriptor.fingerprint, changed.fingerprint)
        self.assertTrue(descriptor.fingerprint.startswith("sha256:"))
        self.assertEqual(
            descriptor.canonical_document()["minimum_history"],
            2,
        )

    def test_descriptor_rejects_noncanonical_evidence_and_invalid_bounds(self):
        with self.assertRaisesRegex(ValueError, "canonical sha256"):
            self.descriptor(evaluation_protocol_sha256="A" * 64)
        with self.assertRaisesRegex(ValueError, "minimum cannot exceed"):
            self.descriptor(
                parameter_bounds=(
                    ("threshold", "0.2", "0.1"),
                    ("proposal_quantity", "1", "2"),
                )
            )
        with self.assertRaisesRegex(ValueError, "duplicate"):
            self.descriptor(
                parameter_bounds=(
                    ("threshold", "0", "1"),
                    ("threshold", "0", "2"),
                )
            )

    def test_strategy_configuration_must_fit_registered_descriptor(self):
        descriptor = self.descriptor()
        strategy = ReturnThresholdBaseline(
            lookback=2,
            threshold="0.01",
            proposal_quantity="2",
            descriptor=descriptor,
        )
        self.assertEqual(strategy.descriptor, descriptor)
        with self.assertRaisesRegex(ValueError, "minimum_history"):
            ReturnThresholdBaseline(
                lookback=3,
                threshold="0.01",
                proposal_quantity="2",
                descriptor=descriptor,
            )
        with self.assertRaisesRegex(ValueError, "outside descriptor bounds"):
            ReturnThresholdBaseline(
                lookback=2,
                threshold="0.20",
                proposal_quantity="2",
                descriptor=descriptor,
            )

    def test_registered_proposal_binds_cutoff_horizon_expiry_and_strategy(self):
        descriptor = self.descriptor(horizon_seconds=7200)
        strategy = ReturnThresholdBaseline(
            lookback=2,
            threshold="0.01",
            proposal_quantity="2",
            descriptor=descriptor,
        )
        decision_time = BASE + timedelta(minutes=1)
        proposal = run_baseline(
            strategy,
            [obs(0, "100"), obs(1, "102")],
            decision_time=decision_time,
            symbol="AAA",
        )
        self.assertEqual(proposal.information_cutoff, decision_time)
        self.assertEqual(proposal.horizon_seconds, 7200)
        self.assertEqual(
            proposal.expiry,
            decision_time + timedelta(seconds=7200),
        )
        self.assertEqual(
            proposal.strategy_version,
            "return-threshold-baseline@1",
        )
        self.assertEqual(
            proposal.strategy_fingerprint,
            descriptor.fingerprint,
        )
        self.assertEqual(proposal.model_calls, 0)
        self.assertEqual(proposal.economic_edge_claim, "UNPROVEN")

    def test_no_trade_proposal_preserves_registered_identity(self):
        descriptor = self.descriptor(minimum_history=3)
        descriptor = StrategyDescriptor(
            **{
                **descriptor.__dict__,
                "minimum_history": 3,
            }
        )
        strategy = ReturnThresholdBaseline(
            lookback=3,
            threshold="0.01",
            proposal_quantity="1",
            descriptor=descriptor,
        )
        proposal = run_baseline(
            strategy,
            [obs(0, "100"), obs(1, "101")],
            decision_time=BASE + timedelta(minutes=1),
            symbol="AAA",
        )
        self.assertEqual(proposal.action, "HOLD")
        self.assertEqual(proposal.economic_edge_claim, "UNPROVEN")
        self.assertEqual(proposal.strategy_fingerprint, descriptor.fingerprint)
        self.assertIsNotNone(proposal.expiry)

    def test_descriptor_survives_snapshot_restart_exactly(self):
        descriptor = self.descriptor()
        strategy = ReturnThresholdBaseline(
            lookback=2,
            threshold="0.01",
            proposal_quantity="2",
            descriptor=descriptor,
        )
        for item in (obs(0, "100"), obs(1, "102")):
            strategy.ingest(item, simulation_time=item.available_at)
        snapshot = strategy.snapshot()
        restored = ReturnThresholdBaseline.restore(snapshot)
        self.assertIsNotNone(restored.descriptor)
        self.assertEqual(
            restored.descriptor.fingerprint,
            descriptor.fingerprint,
        )
        before = strategy.propose(
            symbol="AAA",
            decision_time=BASE + timedelta(minutes=1),
        )
        after = restored.propose(
            symbol="AAA",
            decision_time=BASE + timedelta(minutes=1),
        )
        self.assertEqual(before, after)

    def test_snapshot_descriptor_tamper_fails_closed(self):
        descriptor = self.descriptor()
        strategy = ReturnThresholdBaseline(
            lookback=2,
            threshold="0.01",
            proposal_quantity="2",
            descriptor=descriptor,
        )
        payload = json.loads(strategy.snapshot())
        payload["descriptor"]["minimum_history"] = 3
        with self.assertRaisesRegex(ValueError, "fingerprint does not match"):
            ReturnThresholdBaseline.restore(
                json.dumps(payload, sort_keys=True, separators=(",", ":"))
            )


    def test_registered_result_projects_to_canonical_decision_shape(self):
        descriptor = self.descriptor()
        strategy = ReturnThresholdBaseline(
            lookback=2,
            threshold="0.01",
            proposal_quantity="2",
            descriptor=descriptor,
        )
        proposal = run_baseline(
            strategy,
            [obs(0, "100"), obs(1, "102")],
            decision_time=BASE + timedelta(minutes=1),
            symbol="AAA",
        )
        body = to_decision_proposal(
            proposal,
            proposal_id="12345678-1234-5678-9234-567812345678",
            instrument_version="instrument:aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa@7",
            economics_binding=economics_binding(
                proposal,
                instrument_version="instrument:aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa@7",
            ),
            exit_policy_ref="exit-policy:registered-v1",
            compute_cost_currency="USD",
            counterarguments=("economic edge remains unproven",),
        )
        self.assertEqual(
            set(body),
            {
                "proposal_id",
                "strategy_version",
                "decision_at",
                "information_cutoff",
                "input_manifest_refs",
                "thesis",
                "candidate_instruments",
                "horizon",
                "expected_return_distribution_ref",
                "confidence_basis",
                "counterarguments",
                "exit_policy_ref",
                "expiry",
                "estimated_compute_cost",
            },
        )
        self.assertEqual(body["strategy_version"], "return-threshold-baseline@1")
        self.assertEqual(body["horizon"], "PT3600S")
        self.assertEqual(body["estimated_compute_cost"], {"amount": "0", "currency": "USD"})
        self.assertEqual(body["confidence_basis"]["economic_edge_claim"], "UNPROVEN")
        self.assertEqual(body["confidence_basis"]["model_calls"], 0)
        self.assertNotIn("NO_TRADE_reason", body)

        schemas = {
            path.name: json.loads(path.read_text(encoding="utf-8"))
            for path in (ROOT / "contracts" / "jsonschema").glob("*.json")
        }
        registry = Registry().with_resources(
            [
                (schema["$id"], Resource.from_contents(schema))
                for schema in schemas.values()
            ]
        )
        decision_schema = schemas["decision.schema.json"]
        Draft202012Validator(
            {
                "$ref": (
                    decision_schema["$id"]
                    + "#/$defs/DecisionProposal"
                )
            },
            registry=registry,
            format_checker=FormatChecker(),
        ).validate(body)

    def test_hold_projects_to_no_trade_without_executable_candidate(self):
        descriptor = self.descriptor()
        strategy = ReturnThresholdBaseline(
            lookback=2,
            threshold="0.10",
            proposal_quantity="2",
            descriptor=descriptor,
        )
        proposal = run_baseline(
            strategy,
            [obs(0, "100"), obs(1, "101")],
            decision_time=BASE + timedelta(minutes=1),
            symbol="AAA",
        )
        body = to_decision_proposal(
            proposal,
            proposal_id="12345678-1234-5678-9234-567812345678",
            instrument_version="instrument:aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa@7",
            economics_binding=economics_binding(
                proposal,
                instrument_version="instrument:aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa@7",
            ),
            exit_policy_ref="exit-policy:registered-v1",
            compute_cost_currency="USD",
        )
        self.assertEqual(body["candidate_instruments"], [])
        self.assertEqual(body["NO_TRADE_reason"], proposal.reason)
        self.assertEqual(body["estimated_compute_cost"]["amount"], "0")

    def test_projection_refuses_unregistered_or_unproven_inputs(self):
        unregistered = ReturnThresholdBaseline(
            lookback=2,
            threshold="0.01",
            proposal_quantity="1",
        ).propose(symbol="AAA", decision_time=BASE)
        with self.assertRaisesRegex(ValueError, "registered strategy"):
            to_decision_proposal(
                unregistered,
                proposal_id="12345678-1234-5678-9234-567812345678",
                instrument_version="instrument:v1",
                economics_binding=None,
                exit_policy_ref="exit-policy:v1",
                compute_cost_currency="USD",
            )

        descriptor = self.descriptor()
        strategy = ReturnThresholdBaseline(
            lookback=2,
            threshold="0.01",
            proposal_quantity="1",
            descriptor=descriptor,
        )
        proposal = run_baseline(
            strategy,
            [obs(0, "100"), obs(1, "102")],
            decision_time=BASE + timedelta(minutes=1),
            symbol="AAA",
        )
        with self.assertRaisesRegex(ValueError, "canonical UUID"):
            to_decision_proposal(
                proposal,
                proposal_id="abcdefab-1234-5678-9234-567812345678".upper(),
                instrument_version="instrument:v1",
                economics_binding=economics_binding(
                    proposal,
                    instrument_version="instrument:v1",
                ),
                exit_policy_ref="exit-policy:v1",
                compute_cost_currency="USD",
            )
        with self.assertRaisesRegex(ValueError, "immutable evidence"):
            economics_binding(
                proposal,
                instrument_version="instrument:v1",
                input_manifest_refs=(),
            )
        with self.assertRaisesRegex(ValueError, "canonical sha256"):
            economics_binding(
                proposal,
                instrument_version="instrument:v1",
                input_manifest_refs=("not-a-digest",),
            )


    def test_no_trade_control_is_registered_zero_model_comparator(self):
        descriptor = self.descriptor(
            strategy_id="no-trade-control",
            family="NO_TRADE_CONTROL",
            minimum_history=1,
            horizon_seconds=86400,
            parameter_bounds=(("dummy", "0", "0"),),
        )
        baseline = NoTradeBaseline(descriptor=descriptor)
        proposal = baseline.propose(
            symbol="AAA",
            decision_time=BASE,
            evidence_event_ids=("market-cutoff-1",),
        )
        self.assertEqual(proposal.action, "HOLD")
        self.assertEqual(proposal.quantity, Decimal("0"))
        self.assertEqual(proposal.model_calls, 0)
        self.assertEqual(proposal.economic_edge_claim, "UNPROVEN")
        self.assertEqual(proposal.evidence_event_ids, ("market-cutoff-1",))
        self.assertEqual(proposal.horizon_seconds, 86400)
        self.assertEqual(proposal.expiry, BASE + timedelta(days=1))
        self.assertEqual(
            proposal.strategy_version,
            "no-trade-control@1",
        )

        body = to_decision_proposal(
            proposal,
            proposal_id="12345678-1234-5678-9234-567812345678",
            instrument_version="instrument:not-executable-for-hold",
            economics_binding=economics_binding(
                proposal,
                instrument_version="instrument:not-executable-for-hold",
            ),
            exit_policy_ref="exit-policy:no-position",
            compute_cost_currency="USD",
        )
        self.assertEqual(body["candidate_instruments"], [])
        self.assertEqual(
            body["NO_TRADE_reason"],
            "registered no-trade control baseline",
        )

    def test_no_trade_control_rejects_mislabeled_descriptor_and_duplicate_evidence(self):
        with self.assertRaisesRegex(ValueError, "NO_TRADE_CONTROL"):
            NoTradeBaseline(descriptor=self.descriptor())
        descriptor = self.descriptor(
            strategy_id="no-trade-control",
            family="NO_TRADE_CONTROL",
            minimum_history=1,
            parameter_bounds=(("dummy", "0", "0"),),
        )
        baseline = NoTradeBaseline(descriptor=descriptor)
        with self.assertRaisesRegex(ValueError, "duplicates"):
            baseline.propose(
                symbol="AAA",
                decision_time=BASE,
                evidence_event_ids=("event-1", "event-1"),
            )


    def test_snapshot_fingerprint_detects_semantically_valid_descriptor_tamper(self):
        descriptor = self.descriptor()
        strategy = ReturnThresholdBaseline(
            lookback=2,
            threshold="0.01",
            proposal_quantity="2",
            descriptor=descriptor,
        )
        payload = json.loads(strategy.snapshot())
        payload["descriptor"]["horizon_seconds"] = 7200
        with self.assertRaisesRegex(ValueError, "fingerprint does not match"):
            ReturnThresholdBaseline.restore(
                json.dumps(payload, sort_keys=True, separators=(",", ":"))
            )

    def test_legacy_v3_descriptor_snapshot_remains_readable(self):
        descriptor = self.descriptor()
        strategy = ReturnThresholdBaseline(
            lookback=2,
            threshold="0.01",
            proposal_quantity="2",
            descriptor=descriptor,
        )
        payload = json.loads(strategy.snapshot())
        payload["schema_version"] = 3
        del payload["descriptor_fingerprint"]
        del payload["configuration_fingerprint"]
        del payload["strategy_family"]
        restored = ReturnThresholdBaseline.restore(
            json.dumps(payload, sort_keys=True, separators=(",", ":"))
        )
        self.assertEqual(restored.descriptor.fingerprint, descriptor.fingerprint)


    def test_direct_proposal_cannot_bypass_zero_model_or_edge_invariants(self):
        base = dict(
            symbol="AAA",
            action="HOLD",
            quantity=Decimal("0"),
            decision_time=BASE,
            evidence_event_ids=(),
            model_calls=0,
            economic_edge_claim="UNPROVEN",
            reason="control",
        )
        with self.assertRaisesRegex(ValueError, "model calls"):
            DeterministicProposal(**{**base, "model_calls": 1})
        with self.assertRaisesRegex(ValueError, "economic edge"):
            DeterministicProposal(
                **{**base, "economic_edge_claim": "PROVEN"}
            )
        with self.assertRaisesRegex(ValueError, "HOLD.*zero"):
            DeterministicProposal(
                **{**base, "quantity": Decimal("1")}
            )
        with self.assertRaisesRegex(ValueError, "BUY/SELL.*positive"):
            DeterministicProposal(
                **{
                    **base,
                    "action": "BUY",
                    "quantity": Decimal("0"),
                }
            )
        with self.assertRaises(TypeError):
            DeterministicProposal(
                **{
                    **base,
                    "action": "BUY",
                    "quantity": 1.5,
                }
            )
        with self.assertRaisesRegex(ValueError, "unsupported"):
            DeterministicProposal(**{**base, "action": "EXECUTE_NOW"})

    def test_direct_proposal_rejects_inconsistent_horizon_timing(self):
        base = dict(
            symbol="AAA",
            action="HOLD",
            quantity=Decimal("0"),
            decision_time=BASE,
            evidence_event_ids=(),
            model_calls=0,
            economic_edge_claim="UNPROVEN",
            reason="control",
            information_cutoff=BASE,
            horizon_seconds=3600,
            expiry=BASE + timedelta(hours=1),
        )
        with self.assertRaisesRegex(ValueError, "plus horizon_seconds"):
            DeterministicProposal(
                **{**base, "expiry": BASE + timedelta(minutes=30)}
            )
        with self.assertRaisesRegex(ValueError, "cannot be after decision_time"):
            DeterministicProposal(
                **{
                    **base,
                    "information_cutoff": BASE + timedelta(seconds=1),
                    "expiry": BASE + timedelta(seconds=3601),
                }
            )

    def test_direct_proposal_rejects_duplicate_evidence_and_naive_time(self):
        base = dict(
            symbol="AAA",
            action="HOLD",
            quantity=Decimal("0"),
            decision_time=BASE,
            evidence_event_ids=(),
            model_calls=0,
            economic_edge_claim="UNPROVEN",
            reason="control",
        )
        with self.assertRaisesRegex(ValueError, "duplicates"):
            DeterministicProposal(
                **{
                    **base,
                    "evidence_event_ids": ("event-1", "event-1"),
                }
            )
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            DeterministicProposal(
                **{
                    **base,
                    "decision_time": datetime(2026, 1, 1),
                }
            )


    def test_configuration_fingerprint_distinguishes_runtime_parameters(self):
        descriptor = self.descriptor()
        lower = ReturnThresholdBaseline(
            lookback=2,
            threshold="0.01",
            proposal_quantity="2",
            descriptor=descriptor,
        )
        higher = ReturnThresholdBaseline(
            lookback=2,
            threshold="0.02",
            proposal_quantity="2",
            descriptor=descriptor,
        )
        self.assertEqual(
            lower.descriptor.fingerprint,
            higher.descriptor.fingerprint,
        )
        self.assertNotEqual(
            lower.configuration_fingerprint,
            higher.configuration_fingerprint,
        )
        proposal = lower.propose(symbol="AAA", decision_time=BASE)
        self.assertEqual(
            proposal.strategy_configuration_fingerprint,
            lower.configuration_fingerprint,
        )

    def test_snapshot_detects_parameter_tamper_inside_registered_bounds(self):
        descriptor = self.descriptor()
        strategy = ReturnThresholdBaseline(
            lookback=2,
            threshold="0.01",
            proposal_quantity="2",
            descriptor=descriptor,
        )
        payload = json.loads(strategy.snapshot())
        payload["threshold"] = "0.02"
        with self.assertRaisesRegex(
            ValueError,
            "configuration fingerprint does not match",
        ):
            ReturnThresholdBaseline.restore(
                json.dumps(payload, sort_keys=True, separators=(",", ":"))
            )

    def test_legacy_v4_snapshot_remains_readable_without_configuration_fingerprint(self):
        descriptor = self.descriptor()
        strategy = ReturnThresholdBaseline(
            lookback=2,
            threshold="0.01",
            proposal_quantity="2",
            descriptor=descriptor,
        )
        payload = json.loads(strategy.snapshot())
        payload["schema_version"] = 4
        del payload["configuration_fingerprint"]
        del payload["strategy_family"]
        restored = ReturnThresholdBaseline.restore(
            json.dumps(payload, sort_keys=True, separators=(",", ":"))
        )
        self.assertEqual(
            restored.configuration_fingerprint,
            strategy.configuration_fingerprint,
        )

    def test_direct_proposal_rejects_partial_registered_strategy_identity(self):
        descriptor = self.descriptor()
        with self.assertRaisesRegex(
            ValueError,
            "registered strategy identity must include",
        ):
            DeterministicProposal(
                symbol="AAA",
                action="HOLD",
                quantity=Decimal("0"),
                decision_time=BASE,
                evidence_event_ids=(),
                model_calls=0,
                economic_edge_claim="UNPROVEN",
                reason="manual but incomplete",
                information_cutoff=BASE,
                horizon_seconds=3600,
                expiry=BASE + timedelta(hours=1),
                strategy_version="return-threshold-baseline@1",
                strategy_fingerprint=descriptor.fingerprint,
                strategy_configuration_fingerprint=None,
            )

    def test_direct_registered_identity_requires_horizon_bundle(self):
        descriptor = self.descriptor()
        with self.assertRaisesRegex(
            ValueError,
            "requires cutoff, horizon and expiry",
        ):
            DeterministicProposal(
                symbol="AAA",
                action="HOLD",
                quantity=Decimal("0"),
                decision_time=BASE,
                evidence_event_ids=(),
                model_calls=0,
                economic_edge_claim="UNPROVEN",
                reason="manual but incomplete",
                strategy_version="return-threshold-baseline@1",
                strategy_fingerprint=descriptor.fingerprint,
                strategy_configuration_fingerprint=descriptor.fingerprint,
            )



    def test_positive_gross_signal_with_nonpositive_after_cost_bound_cannot_qualify(self):
        descriptor = self.descriptor()
        strategy = ReturnThresholdBaseline(
            lookback=2,
            threshold="0.01",
            proposal_quantity="2",
            descriptor=descriptor,
        )
        proposal = run_baseline(
            strategy,
            [obs(0, "100"), obs(1, "102")],
            decision_time=BASE + timedelta(minutes=1),
            symbol="AAA",
        )
        with self.assertRaisesRegex(ValueError, "positive after-cost lower bound"):
            economics_binding(
                proposal,
                instrument_version="instrument:aaa@1",
                after_cost_lower_bound="0",
                status="QUALIFIED",
            )

        inconclusive = economics_binding(
            proposal,
            instrument_version="instrument:aaa@1",
            after_cost_lower_bound="0",
            status="INCONCLUSIVE",
        )
        bound = bind_strategy_economics(
            proposal,
            inconclusive,
            instrument_version="instrument:aaa@1",
        )
        self.assertEqual(bound.action, "HOLD")
        self.assertEqual(bound.quantity, Decimal("0"))
        self.assertEqual(proposal.action, "BUY")
        self.assertEqual(proposal.quantity, Decimal("2"))

    def test_frozen_ex_ante_capacity_can_only_reduce_exposure(self):
        descriptor = self.descriptor()
        strategy = ReturnThresholdBaseline(
            lookback=2,
            threshold="0.01",
            proposal_quantity="2",
            descriptor=descriptor,
        )
        proposal = run_baseline(
            strategy,
            [obs(0, "100"), obs(1, "102")],
            decision_time=BASE + timedelta(minutes=1),
            symbol="AAA",
        )
        economics = economics_binding(
            proposal,
            instrument_version="instrument:aaa@1",
            max_feasible_quantity="1.4",
            lot_size="0.5",
        )
        bound = bind_strategy_economics(
            proposal,
            economics,
            instrument_version="instrument:aaa@1",
        )
        self.assertEqual(bound.action, "BUY")
        self.assertEqual(bound.quantity, Decimal("1.0"))
        self.assertLessEqual(bound.quantity, proposal.quantity)
        self.assertEqual(
            bound.fingerprint,
            bind_strategy_economics(
                proposal,
                economics,
                instrument_version="instrument:aaa@1",
            ).fingerprint,
        )

    def test_future_economics_evidence_cannot_resize_prior_proposal(self):
        descriptor = self.descriptor()
        strategy = ReturnThresholdBaseline(
            lookback=2,
            threshold="0.01",
            proposal_quantity="2",
            descriptor=descriptor,
        )
        proposal = run_baseline(
            strategy,
            [obs(0, "100"), obs(1, "102")],
            decision_time=BASE + timedelta(minutes=1),
            symbol="AAA",
        )
        with self.assertRaisesRegex(ValueError, "not causally available"):
            economics_binding(
                proposal,
                instrument_version="instrument:aaa@1",
                available_at=proposal.information_cutoff + timedelta(seconds=1),
                max_feasible_quantity="1000000",
            )

    def test_missing_required_borrow_or_fx_evidence_fails_closed(self):
        descriptor = self.descriptor()
        strategy = ReturnThresholdBaseline(
            lookback=2,
            threshold="0.01",
            proposal_quantity="2",
            descriptor=descriptor,
        )
        proposal = run_baseline(
            strategy,
            [obs(0, "102"), obs(1, "100")],
            decision_time=BASE + timedelta(minutes=1),
            symbol="AAA",
        )
        self.assertEqual(proposal.action, "SELL")
        with self.assertRaisesRegex(ValueError, "missing required evidence"):
            economics_binding(
                proposal,
                instrument_version="instrument:aaa@1",
                required_evidence_dimensions=("BORROW", "FX"),
                dimension_evidence=(
                    ("FX", "sha256:" + "3" * 64),
                ),
                status="QUALIFIED",
            )
        economics = economics_binding(
            proposal,
            instrument_version="instrument:aaa@1",
            required_evidence_dimensions=("BORROW", "FX"),
            dimension_evidence=(("FX", "sha256:" + "3" * 64),),
            status="INCONCLUSIVE",
        )
        self.assertEqual(economics.missing_dimensions, ("BORROW",))
        bound = bind_strategy_economics(
            proposal,
            economics,
            instrument_version="instrument:aaa@1",
        )
        self.assertEqual(bound.action, "HOLD")

    def test_decision_projection_uses_after_cost_distribution_and_frozen_binding(self):
        descriptor = self.descriptor()
        strategy = ReturnThresholdBaseline(
            lookback=2,
            threshold="0.01",
            proposal_quantity="2",
            descriptor=descriptor,
        )
        proposal = run_baseline(
            strategy,
            [obs(0, "100"), obs(1, "102")],
            decision_time=BASE + timedelta(minutes=1),
            symbol="AAA",
        )
        economics = economics_binding(
            proposal,
            instrument_version="instrument:aaa@1",
            max_feasible_quantity="1",
        )
        body = to_decision_proposal(
            proposal,
            proposal_id="12345678-1234-5678-9234-567812345678",
            instrument_version="instrument:aaa@1",
            economics_binding=economics,
            exit_policy_ref="exit-policy:v1",
            compute_cost_currency="USD",
        )
        self.assertEqual(
            body["expected_return_distribution_ref"],
            economics.after_cost_return_distribution_sha256,
        )
        confidence = body["confidence_basis"]
        self.assertEqual(
            confidence["gross_return_distribution_ref"],
            economics.gross_return_distribution_sha256,
        )
        self.assertEqual(confidence["gross_quantity"], "2")
        self.assertEqual(confidence["effective_quantity"], "1")
        self.assertEqual(
            confidence["strategy_economics_binding_sha256"],
            economics.fingerprint,
        )


    def test_ingest_rejects_post_construction_invalid_observation(self):
        strategy = ReturnThresholdBaseline(
            lookback=2,
            threshold="0.01",
            proposal_quantity="1",
        )
        item = obs(0, "100")
        object.__setattr__(item, "price", Decimal("0"))

        with self.assertRaisesRegex(ValueError, "price must be positive"):
            strategy.ingest(item, simulation_time=BASE)

        self.assertEqual(strategy._history, {})
        self.assertEqual(strategy._observations_by_id, {})

    def test_ingest_detaches_observation_from_later_caller_mutation(self):
        strategy = ReturnThresholdBaseline(
            lookback=2,
            threshold="0.01",
            proposal_quantity="1",
        )
        first = obs(0, "100")
        second = obs(1, "102")
        strategy.ingest(first, simulation_time=first.available_at)
        object.__setattr__(first, "price", Decimal("999"))
        strategy.ingest(second, simulation_time=second.available_at)

        proposal = strategy.propose(symbol="AAA", decision_time=second.available_at)
        self.assertEqual(proposal.action, "BUY")
        self.assertEqual(strategy._history["AAA"][0].price, Decimal("100"))

    def test_bind_revalidates_post_construction_economics_status_mutation(self):
        descriptor = self.descriptor()
        strategy = ReturnThresholdBaseline(
            lookback=2,
            threshold="0.01",
            proposal_quantity="2",
            descriptor=descriptor,
        )
        proposal = run_baseline(
            strategy,
            [obs(0, "100"), obs(1, "102")],
            decision_time=BASE + timedelta(minutes=1),
            symbol="AAA",
        )
        economics = economics_binding(
            proposal,
            instrument_version="instrument:aaa@1",
            after_cost_lower_bound="0",
            required_evidence_dimensions=("BORROW",),
            dimension_evidence=(),
            status="INCONCLUSIVE",
        )
        object.__setattr__(economics, "status", "QUALIFIED")

        with self.assertRaisesRegex(ValueError, "missing required evidence"):
            bind_strategy_economics(
                proposal,
                economics,
                instrument_version="instrument:aaa@1",
            )

    def test_bind_revalidates_mutated_proposal_and_detaches_valid_inputs(self):
        descriptor = self.descriptor()
        strategy = ReturnThresholdBaseline(
            lookback=2,
            threshold="0.01",
            proposal_quantity="2",
            descriptor=descriptor,
        )
        proposal = run_baseline(
            strategy,
            [obs(0, "100"), obs(1, "102")],
            decision_time=BASE + timedelta(minutes=1),
            symbol="AAA",
        )
        economics = economics_binding(
            proposal,
            instrument_version="instrument:aaa@1",
            max_feasible_quantity="1",
        )
        bound = bind_strategy_economics(
            proposal,
            economics,
            instrument_version="instrument:aaa@1",
        )
        self.assertIsNot(bound.gross_proposal, proposal)
        self.assertIsNot(bound.economics, economics)
        object.__setattr__(economics, "max_feasible_quantity", Decimal("100"))
        self.assertEqual(bound.economics.max_feasible_quantity, Decimal("1"))
        self.assertEqual(bound.quantity, Decimal("1"))

        object.__setattr__(proposal, "action", "HOLD")
        with self.assertRaisesRegex(ValueError, "HOLD proposal quantity must be zero"):
            bind_strategy_economics(
                proposal,
                economics_binding(
                    bound.gross_proposal,
                    instrument_version="instrument:aaa@1",
                ),
                instrument_version="instrument:aaa@1",
            )



    def test_strategy_detaches_descriptor_from_later_caller_mutation(self):
        descriptor = self.descriptor(horizon_seconds=3600)
        strategy = ReturnThresholdBaseline(
            lookback=2,
            threshold="0.01",
            proposal_quantity="2",
            descriptor=descriptor,
        )
        original_fingerprint = strategy.descriptor.fingerprint
        object.__setattr__(descriptor, "horizon_seconds", 1)
        object.__setattr__(descriptor, "strategy_id", "retargeted")

        proposal = run_baseline(
            strategy,
            [obs(0, "100"), obs(1, "102")],
            decision_time=BASE + timedelta(minutes=1),
            symbol="AAA",
        )
        self.assertEqual(strategy.descriptor.fingerprint, original_fingerprint)
        self.assertEqual(proposal.horizon_seconds, 3600)
        self.assertEqual(
            proposal.strategy_version,
            "return-threshold-baseline@1",
        )

    def test_no_trade_baseline_detaches_descriptor_from_caller(self):
        descriptor = self.descriptor(
            strategy_id="no-trade-control",
            family="NO_TRADE_CONTROL",
            minimum_history=1,
            parameter_bounds=(("dummy", "0", "0"),),
        )
        baseline = NoTradeBaseline(descriptor=descriptor)
        original_fingerprint = baseline.descriptor.fingerprint
        object.__setattr__(descriptor, "family", "DETERMINISTIC_RETURN_THRESHOLD")

        proposal = baseline.propose(symbol="AAA", decision_time=BASE)
        self.assertEqual(baseline.descriptor.fingerprint, original_fingerprint)
        self.assertEqual(proposal.action, "HOLD")

    def test_decision_projection_uses_readmitted_canonical_objects(self):
        descriptor = self.descriptor()
        strategy = ReturnThresholdBaseline(
            lookback=2,
            threshold="0.01",
            proposal_quantity="2",
            descriptor=descriptor,
        )
        proposal = run_baseline(
            strategy,
            [obs(0, "100"), obs(1, "102")],
            decision_time=BASE + timedelta(minutes=1),
            symbol="AAA",
        )
        economics = economics_binding(
            proposal,
            instrument_version="instrument:aaa@1",
            max_feasible_quantity="1.0",
        )
        # Semantically equivalent lexical mutation must be canonicalized by
        # readmission and must not leak caller-owned bytes into the projection.
        object.__setattr__(economics, "instrument_version", " instrument:aaa@1 ")
        body = to_decision_proposal(
            proposal,
            proposal_id="12345678-1234-5678-9234-567812345678",
            instrument_version="instrument:aaa@1",
            economics_binding=economics,
            exit_policy_ref="exit-policy:v1",
            compute_cost_currency="USD",
        )
        canonical = economics_binding(
            proposal,
            instrument_version="instrument:aaa@1",
            max_feasible_quantity="1.0",
        )
        self.assertEqual(
            body["confidence_basis"]["strategy_economics_binding_sha256"],
            canonical.fingerprint,
        )
        self.assertEqual(
            body["expected_return_distribution_ref"],
            canonical.after_cost_return_distribution_sha256,
        )



    def test_strategy_descriptor_rejects_tuple_subclass_before_iteration(self):
        calls = []

        class HostileTuple(tuple):
            def __iter__(self):
                calls.append("iter")
                return super().__iter__()

        with self.assertRaisesRegex(ValueError, "market_requirements must be a tuple"):
            self.descriptor(market_requirements=HostileTuple(("CAUSAL_PRICE",)))
        self.assertEqual(calls, [])

    def test_economics_rejects_tuple_subclass_before_iteration(self):
        descriptor = self.descriptor()
        strategy = ReturnThresholdBaseline(
            lookback=2,
            threshold="0.01",
            proposal_quantity="2",
            descriptor=descriptor,
        )
        proposal = run_baseline(
            strategy,
            [obs(0, "100"), obs(1, "102")],
            decision_time=BASE + timedelta(minutes=1),
            symbol="AAA",
        )
        calls = []

        class HostileTuple(tuple):
            def __iter__(self):
                calls.append("iter")
                return super().__iter__()

        with self.assertRaisesRegex(ValueError, "input_manifest_refs must be a tuple"):
            economics_binding(
                proposal,
                instrument_version="instrument:aaa@1",
                input_manifest_refs=HostileTuple(("sha256:" + "c" * 64,)),
            )
        self.assertEqual(calls, [])

    def test_run_baseline_rejects_strategy_subclass_before_virtual_dispatch(self):
        calls = []

        class HostileStrategy(ReturnThresholdBaseline):
            def ingest(self, observation, *, simulation_time):
                calls.append("ingest")
                return True

            def propose(self, *, symbol, decision_time):
                calls.append("propose")
                raise AssertionError("virtual dispatch reached")

        strategy = HostileStrategy(
            lookback=2,
            threshold="0.01",
            proposal_quantity="1",
        )
        with self.assertRaisesRegex(TypeError, "strategy must be ReturnThresholdBaseline"):
            run_baseline(
                strategy,
                [obs(0, "100"), obs(1, "102")],
                decision_time=BASE + timedelta(minutes=1),
                symbol="AAA",
            )
        self.assertEqual(calls, [])



    def test_numeric_ingress_rejects_oversized_text_before_decimal_construction(self):
        hostile = "9" * 260
        with self.assertRaisesRegex(ValueError, "bounded finite decimal"):
            ReturnThresholdBaseline(
                lookback=2,
                threshold=hostile,
                proposal_quantity="1",
            )

    def test_numeric_ingress_rejects_extreme_exponent_and_large_integer(self):
        for hostile in ("1e999999999999999999999999", 10 ** 256):
            with self.subTest(hostile_type=type(hostile).__name__):
                with self.assertRaisesRegex(ValueError, "bounded finite decimal"):
                    ReturnThresholdBaseline(
                        lookback=2,
                        threshold=hostile,
                        proposal_quantity="1",
                    )

    def test_numeric_ingress_preserves_supported_domain_presentation(self):
        strategy = ReturnThresholdBaseline(
            lookback=2,
            threshold="+1.00e-2",
            proposal_quantity="2.0",
        )
        self.assertEqual(strategy.threshold, Decimal("0.0100"))
        self.assertEqual(strategy.proposal_quantity, Decimal("2.0"))


    def threshold_descriptor(self, *, family, strategy_id, minimum_history=3):
        return self.descriptor(
            strategy_id=strategy_id,
            family=family,
            minimum_history=minimum_history,
            parameter_bounds=(
                ("threshold", "0", "0.10"),
                ("proposal_quantity", "0.0001", "100"),
            ),
        )

    def test_threshold_implementation_rejects_mislabeled_strategy_family(self):
        wrong = self.threshold_descriptor(
            family="DETERMINISTIC_MEAN_REVERSION_THRESHOLD",
            strategy_id="mislabeled",
            minimum_history=2,
        )
        with self.assertRaisesRegex(ValueError, "descriptor family"):
            ReturnThresholdBaseline(
                lookback=2,
                threshold="0.01",
                proposal_quantity="1",
                descriptor=wrong,
            )

        trend = self.descriptor()
        with self.assertRaisesRegex(ValueError, "descriptor family"):
            MeanReversionThresholdBaseline(
                lookback=2,
                threshold="0.01",
                proposal_quantity="1",
                descriptor=trend,
            )

    def test_trend_and_mean_reversion_are_distinct_transparent_controls(self):
        trend_descriptor = self.threshold_descriptor(
            family="DETERMINISTIC_RETURN_THRESHOLD",
            strategy_id="trend-control",
            minimum_history=2,
        )
        mean_descriptor = self.threshold_descriptor(
            family="DETERMINISTIC_MEAN_REVERSION_THRESHOLD",
            strategy_id="mean-reversion-control",
            minimum_history=2,
        )
        rising = [obs(0, "100"), obs(1, "102")]
        trend = run_baseline(
            ReturnThresholdBaseline(
                lookback=2,
                threshold="0.01",
                proposal_quantity="1",
                descriptor=trend_descriptor,
            ),
            rising,
            decision_time=BASE + timedelta(minutes=1),
            symbol="AAA",
        )
        mean = run_baseline(
            MeanReversionThresholdBaseline(
                lookback=2,
                threshold="0.01",
                proposal_quantity="1",
                descriptor=mean_descriptor,
            ),
            rising,
            decision_time=BASE + timedelta(minutes=1),
            symbol="AAA",
        )
        self.assertEqual(trend.action, "BUY")
        self.assertEqual(mean.action, "SELL")
        for proposal in (trend, mean):
            self.assertEqual(proposal.model_calls, 0)
            self.assertEqual(proposal.economic_edge_claim, "UNPROVEN")

    def test_breakout_control_uses_only_prior_causal_window(self):
        descriptor = self.threshold_descriptor(
            family="DETERMINISTIC_BREAKOUT_THRESHOLD",
            strategy_id="breakout-control",
        )
        proposal = run_baseline(
            BreakoutThresholdBaseline(
                lookback=3,
                threshold="0.01",
                proposal_quantity="2",
                descriptor=descriptor,
            ),
            [obs(0, "100"), obs(1, "101"), obs(2, "103")],
            decision_time=BASE + timedelta(minutes=2),
            symbol="AAA",
        )
        self.assertEqual(proposal.action, "BUY")
        self.assertEqual(proposal.quantity, Decimal("2"))
        self.assertEqual(
            proposal.evidence_event_ids,
            ("event-0", "event-1", "event-2"),
        )
        self.assertIn("breakout", proposal.reason)

    def test_family_specific_snapshot_restart_preserves_exact_semantics(self):
        descriptor = self.threshold_descriptor(
            family="DETERMINISTIC_MEAN_REVERSION_THRESHOLD",
            strategy_id="mean-reversion-control",
            minimum_history=2,
        )
        strategy = MeanReversionThresholdBaseline(
            lookback=2,
            threshold="0.01",
            proposal_quantity="1",
            descriptor=descriptor,
        )
        for item in (obs(0, "100"), obs(1, "102")):
            strategy.ingest(item, simulation_time=item.available_at)
        restored = MeanReversionThresholdBaseline.restore(strategy.snapshot())
        self.assertEqual(
            restored.propose(
                symbol="AAA",
                decision_time=BASE + timedelta(minutes=1),
            ),
            strategy.propose(
                symbol="AAA",
                decision_time=BASE + timedelta(minutes=1),
            ),
        )
        with self.assertRaisesRegex(ValueError, "family does not match"):
            ReturnThresholdBaseline.restore(strategy.snapshot())

    def test_legacy_snapshot_cannot_be_reinterpreted_as_new_strategy_family(self):
        trend = ReturnThresholdBaseline(
            lookback=2,
            threshold="0.01",
            proposal_quantity="1",
        )
        payload = json.loads(trend.snapshot())
        payload["schema_version"] = 5
        del payload["strategy_family"]
        legacy = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        ReturnThresholdBaseline.restore(legacy)
        with self.assertRaisesRegex(ValueError, "legacy strategy snapshot"):
            MeanReversionThresholdBaseline.restore(legacy)
        with self.assertRaisesRegex(ValueError, "legacy strategy snapshot"):
            BreakoutThresholdBaseline.restore(legacy)

    def make_comparison_strategies(self):
        no_trade_descriptor = self.descriptor(
            strategy_id="no-trade-control",
            family="NO_TRADE_CONTROL",
            minimum_history=1,
            parameter_bounds=(("dummy", "0", "0"),),
        )
        return (
            ReturnThresholdBaseline(
                lookback=3,
                threshold="0.01",
                proposal_quantity="1",
                descriptor=self.threshold_descriptor(
                    family="DETERMINISTIC_RETURN_THRESHOLD",
                    strategy_id="trend-control",
                ),
            ),
            MeanReversionThresholdBaseline(
                lookback=3,
                threshold="0.01",
                proposal_quantity="1",
                descriptor=self.threshold_descriptor(
                    family="DETERMINISTIC_MEAN_REVERSION_THRESHOLD",
                    strategy_id="mean-reversion-control",
                ),
            ),
            BreakoutThresholdBaseline(
                lookback=3,
                threshold="0.01",
                proposal_quantity="1",
                descriptor=self.threshold_descriptor(
                    family="DETERMINISTIC_BREAKOUT_THRESHOLD",
                    strategy_id="breakout-control",
                ),
            ),
            NoTradeBaseline(descriptor=no_trade_descriptor),
        )

    def test_comparison_exposes_candidates_without_declaring_a_winner(self):
        comparison = compare_deterministic_strategies(
            self.make_comparison_strategies(),
            (obs(0, "100"), obs(1, "101"), obs(2, "103")),
            decision_time=BASE + timedelta(minutes=2),
            symbol="AAA",
        )
        self.assertIsInstance(comparison, StrategyComparisonSnapshot)
        self.assertEqual(len(comparison.proposals), 4)
        self.assertEqual(comparison.selection_status, "NOT_ESTABLISHED")
        self.assertEqual(comparison.economic_edge_status, "NOT_ESTABLISHED")
        self.assertFalse(comparison.grants_trading_authority)
        self.assertEqual(comparison.model_calls, 0)
        self.assertEqual(
            {proposal.action for proposal in comparison.proposals},
            {"BUY", "SELL", "HOLD"},
        )
        self.assertTrue(
            all(
                proposal.economic_edge_claim == "UNPROVEN"
                for proposal in comparison.proposals
            )
        )
        document = comparison.canonical_document()
        self.assertNotIn("winner", document)
        self.assertNotIn("selected_strategy", document)
        self.assertEqual(
            document["selection_status"],
            "NOT_ESTABLISHED",
        )

    def test_comparison_fingerprint_is_invariant_to_candidate_input_order(self):
        observations = (obs(0, "100"), obs(1, "101"), obs(2, "103"))
        first = compare_deterministic_strategies(
            self.make_comparison_strategies(),
            observations,
            decision_time=BASE + timedelta(minutes=2),
            symbol="AAA",
        )
        second_strategies = tuple(reversed(self.make_comparison_strategies()))
        second = compare_deterministic_strategies(
            second_strategies,
            observations,
            decision_time=BASE + timedelta(minutes=2),
            symbol="AAA",
        )
        self.assertEqual(first.fingerprint, second.fingerprint)
        self.assertEqual(first.canonical_document(), second.canonical_document())

    def test_comparison_does_not_mutate_caller_strategy_state(self):
        strategies = self.make_comparison_strategies()
        threshold_strategies = strategies[:3]
        compare_deterministic_strategies(
            strategies,
            (obs(0, "100"), obs(1, "101"), obs(2, "103")),
            decision_time=BASE + timedelta(minutes=2),
            symbol="AAA",
        )
        for strategy in threshold_strategies:
            self.assertEqual(strategy._history, {})
            self.assertEqual(strategy._observations_by_id, {})

    def test_comparison_rejects_future_evidence_before_strategy_mutation(self):
        strategies = self.make_comparison_strategies()
        with self.assertRaisesRegex(ValueError, "not causally available"):
            compare_deterministic_strategies(
                strategies,
                (
                    obs(0, "100"),
                    obs(
                        1,
                        "101",
                        available=BASE + timedelta(minutes=5),
                    ),
                ),
                decision_time=BASE + timedelta(minutes=1),
                symbol="AAA",
            )
        for strategy in strategies[:3]:
            self.assertEqual(strategy._history, {})

    def test_comparison_rejects_duplicate_candidate_configuration_before_run(self):
        descriptor = self.threshold_descriptor(
            family="DETERMINISTIC_RETURN_THRESHOLD",
            strategy_id="trend-control",
            minimum_history=2,
        )
        first = ReturnThresholdBaseline(
            lookback=2,
            threshold="0.01",
            proposal_quantity="1",
            descriptor=descriptor,
        )
        duplicate = ReturnThresholdBaseline(
            lookback=2,
            threshold="0.01",
            proposal_quantity="1",
            descriptor=descriptor,
        )
        with self.assertRaisesRegex(ValueError, "duplicate candidate"):
            compare_deterministic_strategies(
                (first, duplicate),
                (obs(0, "100"), obs(1, "102")),
                decision_time=BASE + timedelta(minutes=1),
                symbol="AAA",
            )
        self.assertEqual(first._history, {})
        self.assertEqual(duplicate._history, {})

    def test_comparison_requires_multiple_registered_candidates(self):
        strategy = self.make_comparison_strategies()[0]
        with self.assertRaisesRegex(ValueError, "at least two"):
            compare_deterministic_strategies(
                (strategy,),
                (obs(0, "100"),),
                decision_time=BASE,
                symbol="AAA",
            )
        unregistered = ReturnThresholdBaseline(
            lookback=2,
            threshold="0.01",
            proposal_quantity="1",
        )
        with self.assertRaisesRegex(ValueError, "must be registered"):
            compare_deterministic_strategies(
                (strategy, unregistered),
                (obs(0, "100"), obs(1, "102")),
                decision_time=BASE + timedelta(minutes=1),
                symbol="AAA",
            )

    def test_comparison_rejects_hostile_tuple_subclasses_before_iteration(self):
        calls = []

        class HostileTuple(tuple):
            def __iter__(self):
                calls.append("iter")
                return super().__iter__()

        strategies = HostileTuple(self.make_comparison_strategies())
        with self.assertRaisesRegex(TypeError, "strategies must"):
            compare_deterministic_strategies(
                strategies,
                (),
                decision_time=BASE,
                symbol="AAA",
            )
        self.assertEqual(calls, [])

        observations = HostileTuple((obs(0, "100"),))
        with self.assertRaisesRegex(TypeError, "observations must"):
            compare_deterministic_strategies(
                self.make_comparison_strategies()[:2],
                observations,
                decision_time=BASE,
                symbol="AAA",
            )
        self.assertEqual(calls, [])

    def test_comparison_fingerprint_revalidates_nested_proposals(self):
        comparison = compare_deterministic_strategies(
            self.make_comparison_strategies()[:2],
            (obs(0, "100"), obs(1, "102"), obs(2, "103")),
            decision_time=BASE + timedelta(minutes=2),
            symbol="AAA",
        )
        object.__setattr__(comparison.proposals[0], "economic_edge_claim", "PROVEN")
        with self.assertRaisesRegex(ValueError, "economic edge"):
            _ = comparison.fingerprint



    def test_use_boundary_rejects_post_construction_threshold_mutation(self):
        descriptor = self.descriptor()
        strategy = ReturnThresholdBaseline(
            lookback=2,
            threshold="0.01",
            proposal_quantity="1",
            descriptor=descriptor,
        )
        object.__setattr__(strategy, "threshold", Decimal("-0.5"))
        with self.assertRaisesRegex(ValueError, "non-negative"):
            strategy.propose(symbol="AAA", decision_time=BASE)
        with self.assertRaisesRegex(ValueError, "non-negative"):
            strategy.snapshot()

    def test_use_boundary_rejects_post_construction_descriptor_relabeling(self):
        descriptor = self.descriptor()
        strategy = ReturnThresholdBaseline(
            lookback=2,
            threshold="0.01",
            proposal_quantity="1",
            descriptor=descriptor,
        )
        object.__setattr__(
            strategy.descriptor,
            "family",
            "DETERMINISTIC_MEAN_REVERSION_THRESHOLD",
        )
        with self.assertRaisesRegex(ValueError, "descriptor family"):
            strategy.propose(symbol="AAA", decision_time=BASE)

    def test_propose_rejects_corrupted_internal_history_before_signal(self):
        strategy = ReturnThresholdBaseline(
            lookback=2,
            threshold="0.01",
            proposal_quantity="1",
        )
        first = obs(0, "100")
        second = obs(1, "102")
        strategy.ingest(first, simulation_time=first.available_at)
        strategy.ingest(second, simulation_time=second.available_at)
        object.__setattr__(strategy._history["AAA"][0], "price", Decimal("0"))
        with self.assertRaisesRegex(ValueError, "price must be positive"):
            strategy.propose(
                symbol="AAA",
                decision_time=BASE + timedelta(minutes=1),
            )

    def test_propose_requires_history_to_match_seen_event_state(self):
        strategy = ReturnThresholdBaseline(
            lookback=2,
            threshold="0.01",
            proposal_quantity="1",
        )
        first = obs(0, "100")
        strategy.ingest(first, simulation_time=first.available_at)
        strategy._observations_by_id[first.event_id] = CausalObservation.create(
            event_id=first.event_id,
            symbol="AAA",
            available_at=first.available_at,
            price="999",
        )
        with self.assertRaisesRegex(ValueError, "identical seen-event state"):
            strategy.propose(symbol="AAA", decision_time=BASE)

    def test_no_trade_control_revalidates_descriptor_at_use_boundary(self):
        descriptor = self.descriptor(
            strategy_id="no-trade-control",
            family="NO_TRADE_CONTROL",
            minimum_history=1,
            parameter_bounds=(("dummy", "0", "0"),),
        )
        baseline = NoTradeBaseline(descriptor=descriptor)
        object.__setattr__(baseline.descriptor, "family", "FORGED_FAMILY")
        with self.assertRaisesRegex(ValueError, "NO_TRADE_CONTROL"):
            baseline.propose(symbol="AAA", decision_time=BASE)


if __name__ == "__main__":
    unittest.main()
