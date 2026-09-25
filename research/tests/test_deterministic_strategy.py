from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import unittest

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

from research.autotrade_research.strategies.deterministic import (
    CausalObservation,
    DeterministicProposal,
    NoTradeBaseline,
    ReturnThresholdBaseline,
    StrategyDescriptor,
    StrategyEconomicsBinding,
    bind_strategy_economics,
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
    status="QUALIFIED",
    max_quantity="2",
    gross_lower_bound="0.03",
    after_cost_lower_bound="0.01",
    required_dimensions=(),
    missing_dimensions=(),
    fx_evidence=None,
    borrow_evidence=None,
    funding_evidence=None,
    instrument_version="instrument:aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa@7",
):
    return StrategyEconomicsBinding(
        strategy_source_sha256="sha256:" + "a" * 64,
        strategy_fingerprint=proposal.strategy_fingerprint,
        strategy_configuration_fingerprint=(
            proposal.strategy_configuration_fingerprint
        ),
        input_manifest_refs=("sha256:" + "c" * 64,),
        instrument_version=instrument_version,
        information_cutoff=proposal.information_cutoff,
        decision_time=proposal.decision_time,
        horizon_seconds=proposal.horizon_seconds,
        expiry=proposal.expiry,
        gross_return_distribution_ref="artifact:gross-return-distribution:1",
        execution_model_fingerprint="sha256:" + "d" * 64,
        execution_calibration_sha256="sha256:" + "e" * 64,
        execution_scenario_fidelity="decision-time-conservative-v1",
        capacity_assessment_sha256="sha256:" + "f" * 64,
        max_feasible_quantity=max_quantity,
        after_cost_distribution_ref="artifact:after-cost-return-distribution:1",
        gross_lower_bound=gross_lower_bound,
        after_cost_lower_bound=after_cost_lower_bound,
        required_dimensions=required_dimensions,
        missing_dimensions=missing_dimensions,
        fx_evidence_sha256=fx_evidence,
        borrow_evidence_sha256=borrow_evidence,
        funding_evidence_sha256=funding_evidence,
        status=status,
        reason_codes=(
            ("AFTER_COST_QUALIFIED",)
            if status == "QUALIFIED"
            else ("MISSING_OR_NONPOSITIVE_ECONOMICS",)
        ),
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
            economics_binding=economics_binding(proposal),
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
            economics_binding=economics_binding(proposal),
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
                economics_binding=economics_binding(
                    proposal,
                    instrument_version="instrument:v1",
                ),
                exit_policy_ref="exit-policy:v1",
                compute_cost_currency="USD",
            )
        with self.assertRaisesRegex(ValueError, "immutable evidence"):
            StrategyEconomicsBinding(
                **{
                    **economics_binding(proposal).__dict__,
                    "input_manifest_refs": (),
                }
            )
        with self.assertRaisesRegex(ValueError, "canonical sha256"):
            StrategyEconomicsBinding(
                **{
                    **economics_binding(proposal).__dict__,
                    "input_manifest_refs": ("not-a-digest",),
                }
            )


    def test_after_cost_nonpositive_preserves_gross_signal_but_fails_to_no_trade(self):
        descriptor = self.descriptor()
        strategy = ReturnThresholdBaseline(
            lookback=2,
            threshold="0.01",
            proposal_quantity="2",
            descriptor=descriptor,
        )
        gross = run_baseline(
            strategy,
            [obs(0, "100"), obs(1, "104")],
            decision_time=BASE + timedelta(minutes=1),
            symbol="AAA",
        )
        self.assertEqual((gross.action, gross.quantity), ("BUY", Decimal("2")))
        binding = economics_binding(
            gross,
            status="NO_TRADE",
            gross_lower_bound="0.02",
            after_cost_lower_bound="0",
        )
        bound = bind_strategy_economics(gross, binding)
        self.assertEqual((bound.effective_action, bound.effective_quantity), ("HOLD", Decimal("0")))
        self.assertEqual((gross.action, gross.quantity), ("BUY", Decimal("2")))
        body = to_decision_proposal(
            gross,
            proposal_id="12345678-1234-5678-9234-567812345678",
            economics_binding=binding,
            exit_policy_ref="exit-policy:v1",
            compute_cost_currency="USD",
        )
        self.assertEqual(body["candidate_instruments"], [])
        self.assertEqual(
            body["expected_return_distribution_ref"],
            "artifact:after-cost-return-distribution:1",
        )
        self.assertEqual(
            body["confidence_basis"]["gross_signal_action"],
            "BUY",
        )

    def test_frozen_capacity_can_only_reduce_quantity_and_is_reproducible(self):
        descriptor = self.descriptor()
        strategy = ReturnThresholdBaseline(
            lookback=2,
            threshold="0.01",
            proposal_quantity="2",
            descriptor=descriptor,
        )
        for item in (obs(0, "100"), obs(1, "103")):
            strategy.ingest(item, simulation_time=BASE + timedelta(minutes=1))
        proposal = strategy.propose(
            symbol="AAA",
            decision_time=BASE + timedelta(minutes=1),
        )
        binding = economics_binding(
            proposal,
            max_quantity="0.75",
        )
        first = bind_strategy_economics(proposal, binding)
        restored = ReturnThresholdBaseline.restore(strategy.snapshot())
        replayed = restored.propose(
            symbol="AAA",
            decision_time=BASE + timedelta(minutes=1),
        )
        second = bind_strategy_economics(replayed, binding)
        self.assertEqual(first.effective_quantity, Decimal("0.75"))
        self.assertEqual(first.evaluation_sha256, second.evaluation_sha256)
        self.assertEqual(binding.binding_sha256, second.economics.binding_sha256)

    def test_missing_required_fx_borrow_or_funding_cannot_be_qualified(self):
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
        for dimension, field in (
            ("FX", "fx_evidence_sha256"),
            ("BORROW", "borrow_evidence_sha256"),
            ("FUNDING", "funding_evidence_sha256"),
        ):
            with self.subTest(dimension=dimension), self.assertRaisesRegex(
                ValueError,
                "QUALIFIED economics",
            ):
                kwargs = {
                    "required_dimensions": (dimension,),
                    "missing_dimensions": (dimension,),
                }
                economics_binding(proposal, **kwargs)

            inconclusive = economics_binding(
                proposal,
                status="INCONCLUSIVE",
                required_dimensions=(dimension,),
                missing_dimensions=(dimension,),
            )
            bound = bind_strategy_economics(proposal, inconclusive)
            self.assertEqual(bound.effective_action, "HOLD")
            self.assertEqual(bound.effective_quantity, Decimal("0"))

    def test_binding_identity_changes_when_posthoc_economics_evidence_changes(self):
        descriptor = self.descriptor()
        proposal = ReturnThresholdBaseline(
            lookback=2,
            threshold="0.01",
            proposal_quantity="1",
            descriptor=descriptor,
        ).propose(symbol="AAA", decision_time=BASE)
        first = economics_binding(proposal)
        second = StrategyEconomicsBinding(
            **{
                **first.__dict__,
                "execution_calibration_sha256": "sha256:" + "9" * 64,
            }
        )
        self.assertNotEqual(first.binding_sha256, second.binding_sha256)

    def test_projection_rejects_binding_from_other_strategy_or_time_cut(self):
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
        foreign = StrategyEconomicsBinding(
            **{
                **economics_binding(proposal).__dict__,
                "strategy_configuration_fingerprint": "sha256:" + "8" * 64,
            }
        )
        with self.assertRaisesRegex(ValueError, "does not match exact"):
            to_decision_proposal(
                proposal,
                proposal_id="12345678-1234-5678-9234-567812345678",
                economics_binding=foreign,
                exit_policy_ref="exit-policy:v1",
                compute_cost_currency="USD",
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
            economics_binding=economics_binding(
                proposal,
                status="NO_TRADE",
                max_quantity="0",
                gross_lower_bound="0",
                after_cost_lower_bound="0",
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


if __name__ == "__main__":
    unittest.main()
