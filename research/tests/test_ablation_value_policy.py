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
    "artifact:11111111-1111-4111-8111-111111111111@sha256:"
    + "1" * 64
)
COST_REF = (
    "artifact:22222222-2222-4222-8222-222222222222@sha256:"
    + "2" * 64
)
FX_REF = (
    "artifact:33333333-3333-4333-8333-333333333333@sha256:"
    + "3" * 64
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


def dimensional_policy(*, unit: str = "USD", fx_ref: str | None = None) -> dict:
    return {
        "schema_version": "1.0.0",
        "value_unit": unit,
        "utility_projection_ref": UTILITY_REF,
        "cost_projection_ref": COST_REF,
        "fx_valuation_ref": fx_ref,
    }


class AblationValuePolicyTests(unittest.TestCase):
    def test_terminal_policy_is_protocol_hash_bound(self):
        with TemporaryDirectory() as directory:
            registry = ScientificRegistry(Path(directory) / "science.sqlite3")
            payload = protocol_payload()
            payload["ablation_value_policy"] = dimensional_policy(
                fx_ref=FX_REF,
            )
            registration = registry.register_protocol(
                payload,
                protocol_id="44444444-4444-4444-8444-444444444444",
            )

            policy = registry.ablation_value_policy(registration.protocol_id)

            self.assertEqual(policy.protocol_id, registration.protocol_id)
            self.assertEqual(policy.protocol_hash, registration.protocol_hash)
            self.assertEqual(policy.schema_version, "1.0.0")
            self.assertEqual(policy.value_unit, "USD")
            self.assertEqual(policy.utility_projection_ref, UTILITY_REF)
            self.assertEqual(policy.cost_projection_ref, COST_REF)
            self.assertEqual(policy.fx_valuation_ref, FX_REF)

    def test_legacy_protocol_remains_registrable_but_has_no_terminal_dimension(self):
        with TemporaryDirectory() as directory:
            registry = ScientificRegistry(Path(directory) / "science.sqlite3")
            registration = registry.register_protocol(
                protocol_payload(),
                protocol_id="55555555-5555-4555-8555-555555555555",
            )
            with self.assertRaisesRegex(
                ProtocolViolation,
                "no ablation_value_policy",
            ):
                registry.ablation_value_policy(registration.protocol_id)

    def test_value_unit_changes_protocol_identity(self):
        with TemporaryDirectory() as directory:
            registry = ScientificRegistry(Path(directory) / "science.sqlite3")
            usd = protocol_payload()
            usd["ablation_value_policy"] = dimensional_policy(unit="USD")
            eur = deepcopy(usd)
            eur["ablation_value_policy"]["value_unit"] = "EUR"

            usd_registration = registry.register_protocol(
                usd,
                protocol_id="66666666-6666-4666-8666-666666666666",
            )
            eur_registration = registry.register_protocol(
                eur,
                protocol_id="77777777-7777-4777-8777-777777777777",
            )

            self.assertNotEqual(
                usd_registration.protocol_hash,
                eur_registration.protocol_hash,
            )

    def test_malformed_dimension_or_projection_identity_fails_closed(self):
        bad_policies = [
            {**dimensional_policy(), "value_unit": "usd"},
            {**dimensional_policy(), "value_unit": ""},
            {**dimensional_policy(), "schema_version": "2.0.0"},
            {**dimensional_policy(), "utility_projection_ref": "artifact:caller"},
            {**dimensional_policy(), "cost_projection_ref": "sha256:" + "2" * 64},
            {**dimensional_policy(), "fx_valuation_ref": "artifact:caller"},
            {**dimensional_policy(), "extra": "caller-selected"},
        ]
        for index, policy in enumerate(bad_policies):
            with self.subTest(index=index), TemporaryDirectory() as directory:
                registry = ScientificRegistry(Path(directory) / "science.sqlite3")
                payload = protocol_payload()
                payload["ablation_value_policy"] = policy
                registration = registry.register_protocol(
                    payload,
                    protocol_id=f"88888888-8888-4888-8{index:03d}-888888888888",
                )
                with self.assertRaises((ProtocolViolation, ValueError)):
                    registry.ablation_value_policy(registration.protocol_id)


if __name__ == "__main__":
    unittest.main()
