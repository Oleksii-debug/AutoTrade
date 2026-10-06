from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import UUID

from autotrade_research.artifacts.store import ArtifactStore
from autotrade_research.evaluation.ablation import (
    AblationOutcome,
    AblationOutcomeArtifactRef,
    AblationPair,
    AblationQualificationAuthority,
    CausalInputEvidence,
)
from autotrade_research.evaluation.ablation_operand_provenance import (
    resolve_ablation_utility_fact_provenance,
)
from autotrade_research.evaluation.ablation_utility_rule_provenance import (
    ResolvedAblationUtilityRuleProvenance,
    resolve_ablation_utility_rule_provenance,
    reverify_ablation_utility_rule_provenance,
)
from autotrade_research.memory.episodes import ExperienceMemory, MemoryIntegrityError
from autotrade_research.memory.reconciled_outcome import resolve_reconciled_outcome_fact
from autotrade_research.science.registry import ScientificRegistry


SOURCE_REVISION = "8" * 40
INPUT_DIGEST = "sha256:" + "4" * 64
COST_DIGEST = "sha256:" + "5" * 64
UNIT_ID = "33333333-3333-4333-8333-333333333333"
UTILITY_DESCRIPTOR_ID = "44444444-4444-4444-8444-444444444444"
COST_DESCRIPTOR_REF = (
    "artifact:55555555-5555-4555-8555-555555555555@sha256:" + "6" * 64
)


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


def matched_pair(cutoff: datetime) -> AblationPair:
    causal = CausalInputEvidence(
        evidence_id="base-input",
        content_digest=INPUT_DIGEST,
        component_id="base",
        available_utc=cutoff,
    )

    def outcome(variant: str, utility: str, cost: str, components: tuple[str, ...]):
        return AblationOutcome(
            case_id="case-rule",
            input_fingerprint="sha256:" + "7" * 64,
            variant=variant,
            utility=Decimal(utility),
            cost=Decimal(cost),
            elapsed_ms=20,
            deadline_ms=100,
            components=components,
            input_cutoff_utc=cutoff,
            decision_utc=cutoff,
            outcome_available_utc=cutoff + timedelta(minutes=5),
            population_unit_id=UNIT_ID,
            input_evidence=(causal,),
        )

    return AblationPair(
        "agent",
        outcome("FULL", "2", "1", ("base", "agent")),
        outcome("ABLATED", "1", "0", ("base",)),
    )


class AblationUtilityRuleProvenanceTests(unittest.TestCase):
    def _publish_utility_descriptor(
        self,
        store: ArtifactStore,
        *,
        rule_id: str = "reconciled-outcome-net-value-v1",
        owner: str = "CANONICAL_RECONCILED_OUTCOME",
        value_unit: str = "USD",
    ) -> str:
        payload = {
            "schema_version": 1,
            "projection_kind": "UTILITY",
            "value_unit": value_unit,
            "owner_authority": owner,
            "rule_id": rule_id,
            "cost_components": [],
        }
        raw = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        manifest = store.publish_bytes(
            artifact_id=UTILITY_DESCRIPTOR_ID,
            data=raw,
            media_type="application/vnd.autotrade.ablation-value-projection+json",
            rights={"storage": True, "export": False},
            source_refs=["scientific-registry:utility-rule"],
        )
        return f"artifact:{UTILITY_DESCRIPTOR_ID}@{manifest['sha256']}"

    def _build_fixture(
        self,
        root: Path,
        *,
        rule_id: str = "reconciled-outcome-net-value-v1",
        owner: str = "CANONICAL_RECONCILED_OUTCOME",
        descriptor_unit: str = "USD",
        policy_unit: str = "USD",
        fx_ref: str | None = None,
    ):
        artifacts = ArtifactStore(root / "artifacts")
        utility_ref = self._publish_utility_descriptor(
            artifacts,
            rule_id=rule_id,
            owner=owner,
            value_unit=descriptor_unit,
        )
        science = ScientificRegistry(root / "science.sqlite3")
        payload = base_protocol()
        payload["ablation_value_policy"] = {
            "schema_version": "1.0.0",
            "value_unit": policy_unit,
            "utility_projection_ref": utility_ref,
            "cost_projection_ref": COST_DESCRIPTOR_REF,
            "fx_valuation_ref": fx_ref,
        }
        registration = science.register_protocol(
            payload,
            protocol_id="66666666-6666-4666-8666-666666666666",
        )
        registered_at = datetime.fromisoformat(registration.created_at)
        cutoff = registered_at + timedelta(minutes=1)
        evaluation_cutoff = cutoff + timedelta(minutes=20)
        pair = matched_pair(cutoff)
        memory = ExperienceMemory(root / "memory.sqlite3")
        memory.append_episode(
            episode_id=UNIT_ID,
            decision_time=cutoff,
            information_cutoff=cutoff,
            task="ablation-rule",
            regime="test",
            instrument_family="EQUITY",
            permission_class="RESEARCH",
            payload={
                "evidence_refs": ["artifact:source"],
                "intended_action": {"case_id": "case-rule", "side": "HOLD"},
                "actual_execution": {"fills": []},
                "outcome": {
                    "class": "POSITIVE",
                    "label": "observed",
                    "label_mature": True,
                    "reconciliation_state": "RECONCILED",
                },
                "costs": {"USD": "0"},
            },
        )
        fact = resolve_reconciled_outcome_fact(
            memory,
            episode_id=UNIT_ID,
            causal_cutoff=evaluation_cutoff,
            granted_permissions={"RESEARCH"},
            task="ablation-rule",
            instrument_family="EQUITY",
        )
        population = ExperienceMemory.coverage_population_snapshot(
            memory,
            causal_cutoff=evaluation_cutoff,
            granted_permissions={"RESEARCH"},
            task="ablation-rule",
            instrument_family="EQUITY",
        )
        authority = AblationQualificationAuthority(
            scientific_registry=science,
            experience_memory=memory,
            artifact_store=artifacts,
            protocol_id=registration.protocol_id,
            protocol_hash=registration.protocol_hash,
            source_revision=SOURCE_REVISION,
            causal_cutoff=evaluation_cutoff,
            granted_permissions={"RESEARCH"},
            task="ablation-rule",
            instrument_family="EQUITY",
        )
        refs = []
        for index, item in enumerate((pair.full, pair.ablated), start=1):
            artifact_id = str(
                UUID(int=0x77777777777740008000000000000010 + index)
            )
            outcome_payload = {
                "schema_version": 1,
                "case_id": item.case_id,
                "variant": item.variant,
                "population_unit_id": item.population_unit_id,
                "utility": "999999",
                "cost": "888888",
                "outcome_available_utc": (
                    item.outcome_available_utc.isoformat().replace("+00:00", "Z")
                ),
                "source_revision": SOURCE_REVISION,
                "protocol_id": registration.protocol_id,
                "protocol_hash": registration.protocol_hash,
                "population_root": population.root_hash,
                "utility_evidence_digest": fact.evidence_digest,
                "cost_evidence_digest": COST_DIGEST,
                "superseded_at_utc": None,
            }
            raw = json.dumps(
                outcome_payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
            manifest = artifacts.publish_bytes(
                artifact_id=artifact_id,
                data=raw,
                media_type="application/vnd.autotrade.ablation-outcome+json",
                rights={"storage": True, "export": False},
                source_refs=[f"protocol:{registration.protocol_id}"],
            )
            refs.append(
                AblationOutcomeArtifactRef(
                    artifact_id=artifact_id,
                    sha256=manifest["sha256"],
                )
            )
        facts = resolve_ablation_utility_fact_provenance(
            authority,
            [pair],
            outcome_refs=refs,
        )
        return (
            science,
            memory,
            artifacts,
            registration,
            pair,
            tuple(refs),
            facts,
            authority,
            evaluation_cutoff,
        )

    def test_registered_rule_binds_to_reverified_fact_without_numeric_score(self):
        with TemporaryDirectory() as directory:
            (
                _science,
                _memory,
                _artifacts,
                registration,
                pair,
                refs,
                facts,
                authority,
                _cutoff,
            ) = self._build_fixture(Path(directory))

            resolved = resolve_ablation_utility_rule_provenance(
                authority,
                [pair],
                outcome_refs=list(refs),
                utility_facts=facts,
            )

            self.assertEqual(resolved.protocol_digest, registration.protocol_hash)
            self.assertEqual(resolved.value_unit, "USD")
            self.assertEqual(
                resolved.owner_authority,
                "CANONICAL_RECONCILED_OUTCOME",
            )
            self.assertEqual(resolved.rule_id, "reconciled-outcome-net-value-v1")
            self.assertEqual(
                resolved.utility_fact_provenance_digest,
                facts.provenance_digest,
            )
            self.assertTrue(resolved.binding_digest.startswith("sha256:"))
            self.assertFalse(hasattr(resolved, "utility"))
            self.assertFalse(hasattr(resolved, "score"))

    def test_unsupported_rule_descriptor_fails_closed(self):
        with TemporaryDirectory() as directory:
            *_, pair, refs, facts, authority, _cutoff = self._build_fixture(
                Path(directory),
                rule_id="caller-selected-score-v9",
            )
            with self.assertRaisesRegex(
                MemoryIntegrityError,
                "projection rule is unsupported",
            ):
                resolve_ablation_utility_rule_provenance(
                    authority,
                    [pair],
                    outcome_refs=list(refs),
                    utility_facts=facts,
                )

    def test_wrong_owner_descriptor_fails_closed(self):
        with TemporaryDirectory() as directory:
            *_, pair, refs, facts, authority, _cutoff = self._build_fixture(
                Path(directory),
                owner="CALLER_OUTCOME",
            )
            with self.assertRaisesRegex(
                MemoryIntegrityError,
                "owner authority mismatch",
            ):
                resolve_ablation_utility_rule_provenance(
                    authority,
                    [pair],
                    outcome_refs=list(refs),
                    utility_facts=facts,
                )

    def test_descriptor_value_unit_must_equal_registered_policy(self):
        with TemporaryDirectory() as directory:
            *_, pair, refs, facts, authority, _cutoff = self._build_fixture(
                Path(directory),
                descriptor_unit="EUR",
                policy_unit="USD",
            )
            with self.assertRaisesRegex(
                MemoryIntegrityError,
                "value unit mismatch",
            ):
                resolve_ablation_utility_rule_provenance(
                    authority,
                    [pair],
                    outcome_refs=list(refs),
                    utility_facts=facts,
                )

    def test_fx_dependency_is_bound_but_not_misrepresented_as_resolved(self):
        fx_ref = (
            "artifact:88888888-8888-4888-8888-888888888888@sha256:" + "9" * 64
        )
        with TemporaryDirectory() as directory:
            *_, pair, refs, facts, authority, _cutoff = self._build_fixture(
                Path(directory),
                fx_ref=fx_ref,
            )
            resolved = resolve_ablation_utility_rule_provenance(
                authority,
                [pair],
                outcome_refs=list(refs),
                utility_facts=facts,
            )
            self.assertEqual(resolved.fx_valuation_ref, fx_ref)
            self.assertFalse(hasattr(resolved, "fx_value"))

    def test_binding_digest_tamper_fails_before_reverification(self):
        with TemporaryDirectory() as directory:
            *_, pair, refs, facts, authority, _cutoff = self._build_fixture(
                Path(directory)
            )
            evidence = resolve_ablation_utility_rule_provenance(
                authority,
                [pair],
                outcome_refs=list(refs),
                utility_facts=facts,
            )
            object.__setattr__(evidence, "binding_digest", "sha256:" + "f" * 64)
            with self.assertRaisesRegex(
                MemoryIntegrityError,
                "rule provenance digest does not match",
            ):
                reverify_ablation_utility_rule_provenance(
                    authority,
                    [pair],
                    outcome_refs=list(refs),
                    utility_facts=facts,
                    evidence=evidence,
                )

    def test_value_policy_class_rebinding_fails_before_hostile_callback(self):
        calls: list[str] = []
        with TemporaryDirectory() as directory:
            *_, pair, refs, facts, authority, _cutoff = self._build_fixture(
                Path(directory)
            )
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
                    resolve_ablation_utility_rule_provenance(
                        authority,
                        [pair],
                        outcome_refs=list(refs),
                        utility_facts=facts,
                    )
            finally:
                setattr(ScientificRegistry, "ablation_value_policy", original)
            self.assertEqual(calls, [])

    def test_restart_reverifies_identical_rule_and_fact_binding(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (
                _science,
                _memory,
                _artifacts,
                registration,
                pair,
                refs,
                facts,
                authority,
                cutoff,
            ) = self._build_fixture(root)
            before = resolve_ablation_utility_rule_provenance(
                authority,
                [pair],
                outcome_refs=list(refs),
                utility_facts=facts,
            )

            reopened_science = ScientificRegistry(root / "science.sqlite3")
            reopened_memory = ExperienceMemory(root / "memory.sqlite3")
            reopened_artifacts = ArtifactStore(root / "artifacts")
            reopened_authority = AblationQualificationAuthority(
                scientific_registry=reopened_science,
                experience_memory=reopened_memory,
                artifact_store=reopened_artifacts,
                protocol_id=registration.protocol_id,
                protocol_hash=registration.protocol_hash,
                source_revision=SOURCE_REVISION,
                causal_cutoff=cutoff,
                granted_permissions={"RESEARCH"},
                task="ablation-rule",
                instrument_family="EQUITY",
            )
            after = reverify_ablation_utility_rule_provenance(
                reopened_authority,
                [pair],
                outcome_refs=list(refs),
                utility_facts=facts,
                evidence=before,
            )
            self.assertEqual(after, before)
            self.assertIsInstance(before, ResolvedAblationUtilityRuleProvenance)


if __name__ == "__main__":
    unittest.main()
