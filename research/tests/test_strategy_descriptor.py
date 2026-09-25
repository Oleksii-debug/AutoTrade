from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest

from research.autotrade_research.strategies.descriptor import (
    ArtifactHash,
    ParameterBound,
    StrategyDescriptor,
    describe_return_threshold,
    propose_from_snapshot,
)
from research.autotrade_research.strategies.deterministic import (
    CausalObservation,
    ReturnThresholdBaseline,
)


BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)
SOURCE_DIGEST = "a" * 64
PROTOCOL_DIGEST = "b" * 64


def build_strategy():
    strategy = ReturnThresholdBaseline(
        lookback=2,
        threshold="0.01",
        proposal_quantity="2",
    )
    for i, price in enumerate(("100", "102")):
        item = CausalObservation.create(
            event_id=f"event-{i}",
            symbol="AAA",
            available_at=BASE + timedelta(minutes=i),
            price=price,
        )
        strategy.ingest(item, simulation_time=item.available_at)
    return strategy


def descriptor_for(strategy):
    return describe_return_threshold(
        strategy,
        strategy_version="1.0.0",
        feature_schema="causal-close-v1",
        instrument_requirements=("positive-price", "stable-symbol-identity"),
        market_requirements=("causal-availability-time",),
        horizon="registered downstream outcome horizon",
        decision_schedule="event-driven after causally available observation",
        supported_regimes=("UNQUALIFIED_ALL",),
        source="AutoTrade first-party deterministic baseline",
        license_status="FIRST_PARTY_RIGHTS_REQUIRE_RELEASE_QUALIFICATION",
        evaluation_protocol="WP-33 zero-model baseline; WP-36 owns economic gates",
        artifact_hashes=(
            ArtifactHash("source", SOURCE_DIGEST),
            ArtifactHash("protocol", PROTOCOL_DIGEST),
        ),
    )


class StrategyDescriptorTests(unittest.TestCase):
    def test_descriptor_is_canonical_and_zero_model_only(self):
        strategy = build_strategy()
        descriptor = descriptor_for(strategy)

        self.assertEqual(descriptor.model_policy, "ZERO_MODEL_ONLY")
        self.assertEqual(descriptor.minimum_history, 2)
        self.assertEqual(
            {item.name for item in descriptor.parameter_bounds},
            {"lookback", "proposal_quantity", "threshold"},
        )
        self.assertEqual(descriptor.digest(), descriptor.digest())
        self.assertNotIn("UNPROVEN", descriptor.canonical_json())

    def test_descriptor_rejects_float_money_or_parameter_values(self):
        with self.assertRaises(TypeError):
            ParameterBound(
                name="threshold",
                minimum="0",
                maximum="1",
                value=0.1,
            )

    def test_descriptor_rejects_unbounded_or_duplicate_metadata(self):
        with self.assertRaises(ValueError):
            StrategyDescriptor(
                strategy_id="x",
                strategy_version="1",
                feature_schema="f",
                instrument_requirements=("same", "same"),
                market_requirements=("m",),
                minimum_history=2,
                horizon="h",
                decision_schedule="d",
                position_semantics="p",
                exit_semantics="e",
                parameter_bounds=(ParameterBound("x", "0", "1", "0.5"),),
                resource_profile="r",
                supported_regimes=("r",),
                source="s",
                license_status="l",
                evaluation_protocol="p",
                artifact_hashes=(ArtifactHash("source", SOURCE_DIGEST),),
            )

    def test_artifact_hash_is_strict(self):
        with self.assertRaises(ValueError):
            ArtifactHash("source", "not-a-sha256")

    def test_pure_proposal_matches_registered_snapshot(self):
        strategy = build_strategy()
        descriptor = descriptor_for(strategy)
        proposal = propose_from_snapshot(
            descriptor=descriptor,
            snapshot=strategy.snapshot(),
            symbol="AAA",
            decision_time=BASE + timedelta(minutes=1),
        )
        self.assertEqual(proposal.action, "BUY")
        self.assertEqual(proposal.quantity, Decimal("2"))
        self.assertEqual(proposal.model_calls, 0)
        self.assertEqual(proposal.economic_edge_claim, "UNPROVEN")

    def test_descriptor_snapshot_mismatch_fails_closed(self):
        strategy = build_strategy()
        descriptor = describe_return_threshold(
            ReturnThresholdBaseline(
                lookback=3,
                threshold="0.01",
                proposal_quantity="2",
            ),
            strategy_version="1.0.0",
            feature_schema="causal-close-v1",
            instrument_requirements=("positive-price",),
            market_requirements=("causal-availability-time",),
            horizon="registered downstream outcome horizon",
            decision_schedule="event-driven",
            supported_regimes=("UNQUALIFIED_ALL",),
            source="AutoTrade",
            license_status="UNQUALIFIED",
            evaluation_protocol="WP-33",
            artifact_hashes=(ArtifactHash("source", SOURCE_DIGEST),),
        )
        with self.assertRaisesRegex(ValueError, "lookback"):
            propose_from_snapshot(
                descriptor=descriptor,
                snapshot=strategy.snapshot(),
                symbol="AAA",
                decision_time=BASE + timedelta(minutes=1),
            )


if __name__ == "__main__":
    unittest.main()
