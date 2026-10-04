from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from autotrade_research.science.registry import (
    ProtocolViolation,
    ScientificRegistry,
)


UTILITY_REF = (
    "artifact:11111111-1111-4111-8111-111111111111@sha256:" + "1" * 64
)
COST_REF = (
    "artifact:22222222-2222-4222-8222-222222222222@sha256:" + "2" * 64
)


def protocol_payload() -> dict:
    return {
        "hypothesis": "component adds after-cost value",
        "strategy": "matched causal ablation",
        "features": ["base", "component"],
        "search_space": {"component": ["enabled", "ablated"]},
        "train_period": {"start": "2026-01-01", "end": "2026-01-02"},
        "validation_period": {"start": "2026-01-04", "end": "2026-01-05"},
        "test_period": {"start": "2026-01-07", "end": "2026-01-08"},
        "forward_period": {"start": "2026-01-10", "end": "2026-01-11"},
        "labels": ["net_value"],
        "horizons": ["1d"],
        "purge_embargo": {"purge": "1d", "embargo": "1d"},
        "universe": ["TEST"],
        "cost_fill_model": "canonical-cost-v1",
        "baselines": ["ablated"],
        "primary_metrics": ["net_incremental_value"],
        "secondary_metrics": ["latency"],
        "trial_budget": 2,
        "stopping_rules": {"maximum_trials": 2},
        "statistical_estimator": "matched-lower-bound",
        "multiplicity_treatment": "pre-registered-single-comparison",
        "minimum_practical_effect": "0",
        "risk_constraints": {"authority_expansion": False},
        "retention_tolerances": {"negative_results": "retain"},
        "promotion_rule": "qualified-only",
    }


def decision_policy(
    *,
    minimum_pairs: int = 2,
    lower: str = "0",
    multiplier: str = "2",
) -> dict:
    return {
        "schema_version": "1.0.0",
        "minimum_pairs": minimum_pairs,
        "required_lower_bound": lower,
        "uncertainty_multiplier": multiplier,
        "decision_rule": "exact-rational-d2-sample-variance-v1",
    }


def value_policy() -> dict:
    return {
        "schema_version": "1.0.0",
        "value_unit": "USD",
        "utility_projection_ref": UTILITY_REF,
        "cost_projection_ref": COST_REF,
        "fx_valuation_ref": None,
    }


class AblationDecisionPolicyTests(unittest.TestCase):
    def test_decision_and_value_policies_share_one_protocol_identity(self):
        with TemporaryDirectory() as directory:
            registry = ScientificRegistry(Path(directory) / "science.sqlite3")
            payload = protocol_payload()
            payload["ablation_decision_policy"] = decision_policy()
            payload["ablation_value_policy"] = value_policy()

            registration = registry.register_protocol(
                payload,
                protocol_id="11111111-1111-4111-8111-111111111111",
            )
            decision = registry.ablation_decision_policy(registration.protocol_id)
            value = registry.ablation_value_policy(registration.protocol_id)

            self.assertEqual(decision.protocol_hash, registration.protocol_hash)
            self.assertEqual(value.protocol_hash, registration.protocol_hash)
            self.assertEqual(decision.minimum_pairs, 2)
            self.assertEqual(decision.required_lower_bound, "0")
            self.assertEqual(decision.uncertainty_multiplier, "2")
            self.assertEqual(
                decision.decision_rule,
                "exact-rational-d2-sample-variance-v1",
            )

    def test_policy_change_changes_protocol_and_policy_identity(self):
        with TemporaryDirectory() as directory:
            registry = ScientificRegistry(Path(directory) / "science.sqlite3")
            first = protocol_payload()
            first["ablation_decision_policy"] = decision_policy()
            second = deepcopy(first)
            second["ablation_decision_policy"]["uncertainty_multiplier"] = "3"

            first_registration = registry.register_protocol(
                first,
                protocol_id="22222222-2222-4222-8222-222222222222",
            )
            second_registration = registry.register_protocol(
                second,
                protocol_id="33333333-3333-4333-8333-333333333333",
            )

            first_policy = registry.ablation_decision_policy(
                first_registration.protocol_id
            )
            second_policy = registry.ablation_decision_policy(
                second_registration.protocol_id
            )
            self.assertNotEqual(
                first_registration.protocol_hash,
                second_registration.protocol_hash,
            )
            self.assertNotEqual(first_policy.policy_digest, second_policy.policy_digest)

    def test_legacy_protocol_cannot_supply_terminal_decision_policy(self):
        with TemporaryDirectory() as directory:
            registry = ScientificRegistry(Path(directory) / "science.sqlite3")
            registration = registry.register_protocol(
                protocol_payload(),
                protocol_id="44444444-4444-4444-8444-444444444444",
            )
            with self.assertRaisesRegex(
                ProtocolViolation,
                "lacks ablation_decision_policy",
            ):
                registry.ablation_decision_policy(registration.protocol_id)

    def test_required_lower_bound_must_equal_registered_practical_effect(self):
        with TemporaryDirectory() as directory:
            registry = ScientificRegistry(Path(directory) / "science.sqlite3")
            payload = protocol_payload()
            payload["ablation_decision_policy"] = decision_policy(lower="1")
            with self.assertRaisesRegex(
                ProtocolViolation,
                "required_lower_bound must equal minimum_practical_effect",
            ):
                registry.register_protocol(
                    payload,
                    protocol_id="55555555-5555-4555-8555-555555555555",
                )

    def test_noncanonical_or_unbounded_decimal_policy_fails_closed(self):
        invalid = [
            decision_policy(lower="0.0"),
            decision_policy(lower="1E0"),
            decision_policy(multiplier="2.0"),
            decision_policy(multiplier="-1"),
            decision_policy(lower="1E+100000000"),
            decision_policy(lower="1E-100000000"),
        ]
        for index, policy in enumerate(invalid):
            with self.subTest(index=index), TemporaryDirectory() as directory:
                registry = ScientificRegistry(Path(directory) / "science.sqlite3")
                payload = protocol_payload()
                payload["ablation_decision_policy"] = policy
                with self.assertRaises(ProtocolViolation):
                    registry.register_protocol(
                        payload,
                        protocol_id=f"60000000-0000-4000-8000-00000000000{index}",
                    )

    def test_policy_shape_and_integer_semantics_fail_closed(self):
        invalid = [
            {**decision_policy(), "schema_version": "2.0.0"},
            {**decision_policy(), "minimum_pairs": 1},
            {**decision_policy(), "minimum_pairs": True},
            {**decision_policy(), "extra": "post-hoc"},
            {**decision_policy(), "decision_rule": ""},
        ]
        for index, policy in enumerate(invalid):
            with self.subTest(index=index), TemporaryDirectory() as directory:
                registry = ScientificRegistry(Path(directory) / "science.sqlite3")
                payload = protocol_payload()
                payload["ablation_decision_policy"] = policy
                with self.assertRaises(ProtocolViolation):
                    registry.register_protocol(
                        payload,
                        protocol_id=f"70000000-0000-4000-8000-00000000000{index}",
                    )

    def test_policy_subclasses_are_rejected_before_callbacks_or_hashing(self):
        calls: list[str] = []

        class HostilePolicy(dict):
            def keys(self):
                calls.append("keys")
                return super().keys()

            def __iter__(self):
                calls.append("iter")
                return super().__iter__()

            def get(self, key, default=None):
                calls.append("get")
                return super().get(key, default)

        class HostileText(str):
            def strip(self, *args, **kwargs):
                calls.append("strip")
                return super().strip(*args, **kwargs)

            def __eq__(self, other):
                calls.append("eq")
                return super().__eq__(other)

            __hash__ = str.__hash__

        class HostileKey(str):
            def __hash__(self):
                calls.append("hash")
                return str.__hash__(self)

        hostile_key_policy = decision_policy()
        value = hostile_key_policy.pop("decision_rule")
        hostile_key_policy[HostileKey("decision_rule")] = value

        hostile_values = [
            HostilePolicy(decision_policy()),
            {**decision_policy(), "schema_version": HostileText("1.0.0")},
            {**decision_policy(), "required_lower_bound": HostileText("0")},
            {**decision_policy(), "uncertainty_multiplier": HostileText("2")},
            {
                **decision_policy(),
                "decision_rule": HostileText(
                    "exact-rational-d2-sample-variance-v1"
                ),
            },
            hostile_key_policy,
        ]

        for index, policy in enumerate(hostile_values):
            with self.subTest(index=index), TemporaryDirectory() as directory:
                registry = ScientificRegistry(Path(directory) / "science.sqlite3")
                payload = protocol_payload()
                payload["ablation_decision_policy"] = policy
                calls.clear()
                with self.assertRaises(ProtocolViolation):
                    registry.register_protocol(
                        payload,
                        protocol_id=f"80000000-0000-4000-8{index:03d}-000000000000",
                    )
                self.assertEqual(calls, [])

    def test_minimum_practical_effect_subclass_fails_before_callbacks(self):
        calls: list[str] = []

        class HostileText(str):
            def strip(self, *args, **kwargs):
                calls.append("strip")
                return super().strip(*args, **kwargs)

            def __eq__(self, other):
                calls.append("eq")
                return super().__eq__(other)

            __hash__ = str.__hash__

        with TemporaryDirectory() as directory:
            registry = ScientificRegistry(Path(directory) / "science.sqlite3")
            payload = protocol_payload()
            payload["minimum_practical_effect"] = HostileText("0")
            payload["ablation_decision_policy"] = decision_policy()

            calls.clear()
            with self.assertRaises(ProtocolViolation):
                registry.register_protocol(
                    payload,
                    protocol_id="88888888-8888-4888-8888-888888888888",
                )
            self.assertEqual(calls, [])

    def test_invalid_policy_does_not_poison_append_only_protocol_id(self):
        with TemporaryDirectory() as directory:
            registry = ScientificRegistry(Path(directory) / "science.sqlite3")
            protocol_id = "99999999-9999-4999-8999-999999999999"
            invalid = protocol_payload()
            invalid["ablation_decision_policy"] = decision_policy(lower="1")

            with self.assertRaises(ProtocolViolation):
                registry.register_protocol(invalid, protocol_id=protocol_id)

            valid = protocol_payload()
            valid["ablation_decision_policy"] = decision_policy()
            registration = registry.register_protocol(valid, protocol_id=protocol_id)
            policy = registry.ablation_decision_policy(registration.protocol_id)
            self.assertEqual(policy.required_lower_bound, "0")


if __name__ == "__main__":
    unittest.main()
