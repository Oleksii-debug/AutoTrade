from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import test_ablation_utility_rule_provenance as utility_fixture

from autotrade_research.evaluation.ablation_owned_operand_preflight import (
    ResolvedAblationOwnedOperandPreflight,
    resolve_ablation_owned_operand_preflight,
    reverify_ablation_owned_operand_preflight,
)
from autotrade_research.evaluation.utility_projection import (
    build_utility_projection_rule,
)
from autotrade_research.memory.episodes import MemoryIntegrityError


COST_DESCRIPTOR_ID = "55555555-5555-4555-8555-555555555555"
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


class AblationOwnedOperandPreflightTests(unittest.TestCase):
    def _build_fixture(
        self,
        root: Path,
        *,
        components: list[str] | None = None,
        cost_unit: str = "USD",
        source_owned_utility_rule: bool = True,
    ):
        descriptor = {
            "schema_version": 1,
            "projection_kind": "COST",
            "value_unit": cost_unit,
            "owner_authority": "CANONICAL_ABLATION_COST_COMPOSITE",
            "rule_id": "complete-after-cost-attribution-v1",
            "cost_components": REQUIRED_COMPONENTS if components is None else components,
        }
        raw = json.dumps(
            descriptor,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        digest = "sha256:" + sha256(raw).hexdigest()
        cost_ref = f"artifact:{COST_DESCRIPTOR_ID}@{digest}"
        original_cost_ref = utility_fixture.COST_DESCRIPTOR_REF
        original_utility_id = utility_fixture.UTILITY_DESCRIPTOR_ID
        utility_fixture.COST_DESCRIPTOR_REF = cost_ref
        if source_owned_utility_rule:
            utility_fixture.UTILITY_DESCRIPTOR_ID = build_utility_projection_rule(
                "USD"
            ).artifact_id
        try:
            helper = utility_fixture.AblationUtilityRuleProvenanceTests(
                "test_registered_rule_binds_to_reverified_fact_without_numeric_score"
            )
            fixture = helper._build_fixture(root)
        finally:
            utility_fixture.COST_DESCRIPTOR_REF = original_cost_ref
            utility_fixture.UTILITY_DESCRIPTOR_ID = original_utility_id
        (
            _science,
            _memory,
            artifacts,
            registration,
            pair,
            refs,
            _facts,
            authority,
            _cutoff,
        ) = fixture
        manifest = artifacts.publish_bytes(
            artifact_id=COST_DESCRIPTOR_ID,
            data=raw,
            media_type="application/vnd.autotrade.ablation-value-projection+json",
            rights={"storage": True, "export": False},
            source_refs=["scientific-registry:cost-rule"],
        )
        self.assertEqual(manifest["sha256"], digest)
        return registration, pair, refs, authority

    def test_composes_owned_precursors_but_remains_explicitly_preterminal(self):
        with TemporaryDirectory() as directory:
            registration, pair, refs, authority = self._build_fixture(Path(directory))

            resolved = resolve_ablation_owned_operand_preflight(
                authority,
                [pair],
                outcome_refs=list(refs),
            )

            self.assertIsInstance(resolved, ResolvedAblationOwnedOperandPreflight)
            self.assertEqual(resolved.protocol_digest, registration.protocol_hash)
            self.assertEqual(resolved.value_unit, "USD")
            self.assertEqual(resolved.cost_components, tuple(REQUIRED_COMPONENTS))
            self.assertFalse(resolved.terminal_numeric_operands)
            self.assertEqual(
                resolved.missing_terminal_evidence,
                (
                    "utility_numeric_projection",
                    "registered_cost_component_attribution",
                    "registered_cost_component_projection",
                    "complete_cost_composite",
                ),
            )
            self.assertEqual(
                resolved.blocking_reason,
                "canonical_terminal_numeric_operand_evidence_unavailable",
            )
            self.assertTrue(resolved.bundle_digest.startswith("sha256:"))
            self.assertFalse(hasattr(resolved, "utility"))
            self.assertFalse(hasattr(resolved, "cost"))
            self.assertFalse(hasattr(resolved, "verdict"))
            resolved.verify_integrity()

    def test_caller_selected_utility_rule_identity_never_forms_preterminal_bundle(self):
        with TemporaryDirectory() as directory:
            _registration, pair, refs, authority = self._build_fixture(
                Path(directory),
                source_owned_utility_rule=False,
            )

            with self.assertRaisesRegex(
                MemoryIntegrityError,
                "utility projection rule identity is not source-owned",
            ):
                resolve_ablation_owned_operand_preflight(
                    authority,
                    [pair],
                    outcome_refs=list(refs),
                )

    def test_reverification_reconstructs_identical_owned_preflight(self):
        with TemporaryDirectory() as directory:
            _registration, pair, refs, authority = self._build_fixture(Path(directory))
            evidence = resolve_ablation_owned_operand_preflight(
                authority,
                [pair],
                outcome_refs=list(refs),
            )

            replayed = reverify_ablation_owned_operand_preflight(
                authority,
                [pair],
                outcome_refs=list(refs),
                evidence=evidence,
            )

            self.assertEqual(replayed, evidence)

    def test_instance_shadowed_verify_integrity_cannot_retarget_reverification(self):
        calls: list[str] = []
        with TemporaryDirectory() as directory:
            _registration, pair, refs, authority = self._build_fixture(Path(directory))
            evidence = resolve_ablation_owned_operand_preflight(
                authority,
                [pair],
                outcome_refs=list(refs),
            )
            object.__setattr__(
                evidence,
                "verify_integrity",
                lambda: calls.append("hostile"),
            )

            replayed = reverify_ablation_owned_operand_preflight(
                authority,
                [pair],
                outcome_refs=list(refs),
                evidence=evidence,
            )

            self.assertEqual(calls, [])
            self.assertEqual(replayed.bundle_digest, evidence.bundle_digest)

    def test_bundle_digest_tamper_fails_before_reverification(self):
        with TemporaryDirectory() as directory:
            _registration, pair, refs, authority = self._build_fixture(Path(directory))
            evidence = resolve_ablation_owned_operand_preflight(
                authority,
                [pair],
                outcome_refs=list(refs),
            )
            object.__setattr__(evidence, "bundle_digest", "sha256:" + "f" * 64)

            with self.assertRaisesRegex(
                MemoryIntegrityError,
                "preflight digest does not match canonical material",
            ):
                reverify_ablation_owned_operand_preflight(
                    authority,
                    [pair],
                    outcome_refs=list(refs),
                    evidence=evidence,
                )

    def test_missing_terminal_evidence_cannot_be_narrowed_by_mutation(self):
        with TemporaryDirectory() as directory:
            _registration, pair, refs, authority = self._build_fixture(Path(directory))
            evidence = resolve_ablation_owned_operand_preflight(
                authority,
                [pair],
                outcome_refs=list(refs),
            )
            object.__setattr__(
                evidence,
                "missing_terminal_evidence",
                ("complete_cost_composite",),
            )

            with self.assertRaisesRegex(
                MemoryIntegrityError,
                "preflight digest does not match canonical material",
            ):
                reverify_ablation_owned_operand_preflight(
                    authority,
                    [pair],
                    outcome_refs=list(refs),
                    evidence=evidence,
                )

    def test_incomplete_registered_cost_components_never_form_preterminal_bundle(self):
        with TemporaryDirectory() as directory:
            _registration, pair, refs, authority = self._build_fixture(
                Path(directory),
                components=REQUIRED_COMPONENTS[:-1],
            )

            with self.assertRaisesRegex(
                MemoryIntegrityError,
                "component coverage is incomplete",
            ):
                resolve_ablation_owned_operand_preflight(
                    authority,
                    [pair],
                    outcome_refs=list(refs),
                )

    def test_utility_and_cost_rules_must_share_registered_value_unit(self):
        with TemporaryDirectory() as directory:
            _registration, pair, refs, authority = self._build_fixture(
                Path(directory),
                cost_unit="EUR",
            )

            with self.assertRaisesRegex(
                MemoryIntegrityError,
                "cost projection value unit mismatch",
            ):
                resolve_ablation_owned_operand_preflight(
                    authority,
                    [pair],
                    outcome_refs=list(refs),
                )


if __name__ == "__main__":
    unittest.main()
