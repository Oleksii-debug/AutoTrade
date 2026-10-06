from datetime import datetime, timedelta
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from autotrade_research.artifacts.store import ArtifactStore
from autotrade_research.evaluation.ablation import AblationQualificationAuthority
from autotrade_research.evaluation.ablation_cost_rule_provenance import (
    ResolvedAblationCostRuleProvenance,
    resolve_ablation_cost_rule_provenance,
    reverify_ablation_cost_rule_provenance,
)
from autotrade_research.memory.episodes import ExperienceMemory, MemoryIntegrityError
from autotrade_research.science.registry import ScientificRegistry


SOURCE_REVISION = "8" * 40
COST_DESCRIPTOR_ID = "55555555-5555-4555-8555-555555555555"
UTILITY_DESCRIPTOR_REF = (
    "artifact:44444444-4444-4444-8444-444444444444@sha256:" + "4" * 64
)
REQUIRED_COMPONENTS = [
    "commission",
    "spread",
    "slippage",
    "financing",
    "funding",
    "borrow",
    "market_data",
    "model_compute",
    "infrastructure",
    "tax_estimate",
]


def base_protocol() -> dict:
    return {
        "hypothesis": "component adds after-cost value",
        "strategy": "matched causal ablation",
        "features": ["base", "agent"],
        "search_space": {"agent": ["enabled", "ablated"]},
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
        "trial_budget": 1,
        "stopping_rules": {"maximum_trials": 1},
        "statistical_estimator": "matched-lower-bound",
        "multiplicity_treatment": "pre-registered-single-comparison",
        "minimum_practical_effect": "0",
        "risk_constraints": {"authority_expansion": False},
        "retention_tolerances": {"negative_results": "retain"},
        "promotion_rule": "qualified-only",
    }


class AblationCostRuleProvenanceTests(unittest.TestCase):
    def _build_fixture(
        self,
        root: Path,
        *,
        components: list[str] | None = None,
        owner: str = "CANONICAL_ABLATION_COST_COMPOSITE",
        rule_id: str = "complete-after-cost-attribution-v1",
        descriptor_unit: str = "USD",
        policy_unit: str = "USD",
        fx_ref: str | None = None,
    ):
        artifacts = ArtifactStore(root / "artifacts")
        payload = {
            "schema_version": 1,
            "projection_kind": "COST",
            "value_unit": descriptor_unit,
            "owner_authority": owner,
            "rule_id": rule_id,
            "cost_components": REQUIRED_COMPONENTS if components is None else components,
        }
        raw = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        manifest = artifacts.publish_bytes(
            artifact_id=COST_DESCRIPTOR_ID,
            data=raw,
            media_type="application/vnd.autotrade.ablation-value-projection+json",
            rights={"storage": True, "export": False},
            source_refs=["scientific-registry:cost-rule"],
        )
        cost_ref = f"artifact:{COST_DESCRIPTOR_ID}@{manifest['sha256']}"
        science = ScientificRegistry(root / "science.sqlite3")
        protocol = base_protocol()
        protocol["ablation_value_policy"] = {
            "schema_version": "1.0.0",
            "value_unit": policy_unit,
            "utility_projection_ref": UTILITY_DESCRIPTOR_REF,
            "cost_projection_ref": cost_ref,
            "fx_valuation_ref": fx_ref,
        }
        registration = science.register_protocol(
            protocol,
            protocol_id="66666666-6666-4666-8666-666666666666",
        )
        registered_at = datetime.fromisoformat(registration.created_at)
        memory = ExperienceMemory(root / "memory.sqlite3")
        authority = AblationQualificationAuthority(
            scientific_registry=science,
            experience_memory=memory,
            artifact_store=artifacts,
            protocol_id=registration.protocol_id,
            protocol_hash=registration.protocol_hash,
            source_revision=SOURCE_REVISION,
            causal_cutoff=registered_at + timedelta(minutes=1),
            granted_permissions={"RESEARCH"},
            task="ablation-cost-rule",
            instrument_family="EQUITY",
        )
        return registration, authority

    def test_registered_cost_rule_binds_complete_component_coverage_without_numeric_cost(self):
        with TemporaryDirectory() as directory:
            registration, authority = self._build_fixture(Path(directory))

            resolved = resolve_ablation_cost_rule_provenance(authority)

            self.assertIsInstance(resolved, ResolvedAblationCostRuleProvenance)
            self.assertEqual(resolved.protocol_digest, registration.protocol_hash)
            self.assertEqual(resolved.value_unit, "USD")
            self.assertEqual(
                resolved.owner_authority,
                "CANONICAL_ABLATION_COST_COMPOSITE",
            )
            self.assertEqual(resolved.rule_id, "complete-after-cost-attribution-v1")
            self.assertEqual(resolved.cost_components, tuple(REQUIRED_COMPONENTS))
            self.assertTrue(resolved.binding_digest.startswith("sha256:"))
            self.assertFalse(hasattr(resolved, "cost"))
            self.assertFalse(hasattr(resolved, "amount"))
            resolved.verify_integrity()

    def test_missing_required_component_fails_closed(self):
        with TemporaryDirectory() as directory:
            _registration, authority = self._build_fixture(
                Path(directory),
                components=REQUIRED_COMPONENTS[:-1],
            )
            with self.assertRaisesRegex(
                MemoryIntegrityError,
                "component coverage is incomplete",
            ):
                resolve_ablation_cost_rule_provenance(authority)

    def test_wrong_owner_and_rule_fail_closed(self):
        cases = (
            ({"owner": "CALLER_COST"}, "owner authority mismatch"),
            ({"rule_id": "caller-cost-v9"}, "projection rule is unsupported"),
        )
        for kwargs, message in cases:
            with self.subTest(kwargs=kwargs):
                with TemporaryDirectory() as directory:
                    _registration, authority = self._build_fixture(
                        Path(directory),
                        **kwargs,
                    )
                    with self.assertRaisesRegex(MemoryIntegrityError, message):
                        resolve_ablation_cost_rule_provenance(authority)

    def test_descriptor_value_unit_must_equal_registered_policy(self):
        with TemporaryDirectory() as directory:
            _registration, authority = self._build_fixture(
                Path(directory),
                descriptor_unit="EUR",
                policy_unit="USD",
            )
            with self.assertRaisesRegex(
                MemoryIntegrityError,
                "value unit mismatch",
            ):
                resolve_ablation_cost_rule_provenance(authority)

    def test_fx_dependency_is_bound_without_claiming_valuation(self):
        fx_ref = (
            "artifact:88888888-8888-4888-8888-888888888888@sha256:" + "9" * 64
        )
        with TemporaryDirectory() as directory:
            _registration, authority = self._build_fixture(
                Path(directory),
                fx_ref=fx_ref,
            )
            resolved = resolve_ablation_cost_rule_provenance(authority)
            self.assertEqual(resolved.fx_valuation_ref, fx_ref)
            self.assertFalse(hasattr(resolved, "fx_value"))

    def test_binding_tamper_fails_before_reverification(self):
        with TemporaryDirectory() as directory:
            _registration, authority = self._build_fixture(Path(directory))
            evidence = resolve_ablation_cost_rule_provenance(authority)
            object.__setattr__(evidence, "binding_digest", "sha256:" + "f" * 64)
            with self.assertRaisesRegex(
                MemoryIntegrityError,
                "cost rule provenance digest does not match",
            ):
                reverify_ablation_cost_rule_provenance(authority, evidence)

    def test_value_policy_class_rebinding_fails_before_hostile_callback(self):
        calls: list[str] = []
        with TemporaryDirectory() as directory:
            _registration, authority = self._build_fixture(Path(directory))
            original = ScientificRegistry.__dict__["ablation_value_policy"]

            def hostile(self, protocol_id):
                calls.append("policy")
                raise AssertionError("hostile value policy executed")

            setattr(ScientificRegistry, "ablation_value_policy", hostile)
            try:
                with self.assertRaisesRegex(
                    MemoryIntegrityError,
                    "value-policy executable changed",
                ):
                    resolve_ablation_cost_rule_provenance(authority)
            finally:
                setattr(ScientificRegistry, "ablation_value_policy", original)
            self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
