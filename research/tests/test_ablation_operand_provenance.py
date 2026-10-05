from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import UUID

import autotrade_research.evaluation.ablation as ablation_module
from autotrade_research.artifacts.store import ArtifactStore
from autotrade_research.evaluation.ablation import (
    AblationOutcome,
    AblationOutcomeArtifactRef,
    AblationPair,
    AblationQualificationAuthority,
    CausalInputEvidence,
)
from autotrade_research.evaluation.ablation_operand_provenance import (
    ResolvedAblationUtilityFactProvenance,
    resolve_ablation_utility_fact_provenance,
    reverify_ablation_utility_fact_provenance,
)
from autotrade_research.memory.episodes import ExperienceMemory, MemoryIntegrityError
from autotrade_research.memory.reconciled_outcome import resolve_reconciled_outcome_fact
from autotrade_research.science.registry import ProtocolViolation, ScientificRegistry


SOURCE_REVISION = "9" * 40
INPUT_DIGEST = "sha256:" + "1" * 64
COST_DIGEST = "sha256:" + "2" * 64
UNIT_ID = "22222222-2222-4222-8222-222222222222"


def protocol_payload() -> dict:
    return {
        "hypothesis": "agent adds after-cost value",
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


def pair_for_cut(cutoff: datetime) -> AblationPair:
    evidence = CausalInputEvidence(
        evidence_id="base-input",
        content_digest=INPUT_DIGEST,
        component_id="base",
        available_utc=cutoff,
    )

    def outcome(variant: str, utility: str, cost: str, components: tuple[str, ...]):
        return AblationOutcome(
            case_id="case-63",
            input_fingerprint="sha256:" + "3" * 64,
            variant=variant,
            utility=Decimal(utility),
            cost=Decimal(cost),
            elapsed_ms=50,
            deadline_ms=100,
            components=components,
            input_cutoff_utc=cutoff,
            decision_utc=cutoff,
            outcome_available_utc=cutoff + timedelta(minutes=5),
            population_unit_id=UNIT_ID,
            input_evidence=(evidence,),
        )

    return AblationPair(
        "agent",
        outcome("FULL", "2", "1", ("base", "agent")),
        outcome("ABLATED", "1", "0", ("base",)),
    )


class AblationOperandProvenanceTests(unittest.TestCase):
    def _build_fixture(self, root: Path):
        science = ScientificRegistry(root / "science.sqlite3")
        memory = ExperienceMemory(root / "memory.sqlite3")
        artifacts = ArtifactStore(root / "artifacts")
        registration = science.register_protocol(
            protocol_payload(),
            protocol_id="11111111-1111-4111-8111-111111111111",
        )
        registered_at = datetime.fromisoformat(registration.created_at)
        self.assertIs(registered_at.tzinfo, timezone.utc)
        cutoff = registered_at + timedelta(minutes=1)
        evaluation_cutoff = cutoff + timedelta(minutes=20)
        matched = pair_for_cut(cutoff)
        memory.append_episode(
            episode_id=UNIT_ID,
            decision_time=matched.full.decision_utc,
            information_cutoff=matched.full.input_cutoff_utc,
            task="ablation-qualification",
            regime="test",
            instrument_family="EQUITY",
            permission_class="RESEARCH",
            payload={
                "evidence_refs": ["artifact:source"],
                "intended_action": {"case_id": "case-63", "side": "BUY"},
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
            task="ablation-qualification",
            instrument_family="EQUITY",
        )
        population = ExperienceMemory.coverage_population_snapshot(
            memory,
            causal_cutoff=evaluation_cutoff,
            granted_permissions={"RESEARCH"},
            task="ablation-qualification",
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
            task="ablation-qualification",
            instrument_family="EQUITY",
        )
        return (
            science,
            memory,
            artifacts,
            registration,
            matched,
            fact,
            population,
            authority,
            evaluation_cutoff,
        )

    def _publish_refs(
        self,
        artifacts: ArtifactStore,
        registration,
        matched: AblationPair,
        population,
        *,
        utility_digest: str,
        full_utility: str = "999999",
        ablated_utility: str = "-999999",
    ) -> tuple[AblationOutcomeArtifactRef, ...]:
        refs = []
        for index, (item, utility) in enumerate(
            (
                (matched.full, full_utility),
                (matched.ablated, ablated_utility),
            ),
            start=1,
        ):
            artifact_id = str(
                UUID(int=0x77777777777740008000000000000000 + index)
            )
            payload = {
                "schema_version": 1,
                "case_id": item.case_id,
                "variant": item.variant,
                "population_unit_id": item.population_unit_id,
                "utility": utility,
                "cost": "888888",
                "outcome_available_utc": (
                    item.outcome_available_utc.isoformat().replace("+00:00", "Z")
                ),
                "source_revision": SOURCE_REVISION,
                "protocol_id": registration.protocol_id,
                "protocol_hash": registration.protocol_hash,
                "population_root": population.root_hash,
                "utility_evidence_digest": utility_digest,
                "cost_evidence_digest": COST_DIGEST,
                "superseded_at_utc": None,
            }
            raw = json.dumps(
                payload,
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
        return tuple(refs)

    def test_real_memory_fact_digest_binds_without_numeric_economic_authority(self):
        with TemporaryDirectory() as directory:
            (
                _science,
                _memory,
                artifacts,
                registration,
                matched,
                fact,
                population,
                authority,
                _evaluation_cutoff,
            ) = self._build_fixture(Path(directory))
            refs = self._publish_refs(
                artifacts,
                registration,
                matched,
                population,
                utility_digest=fact.evidence_digest,
            )

            resolved = resolve_ablation_utility_fact_provenance(
                authority,
                [matched],
                outcome_refs=list(refs),
            )

            self.assertEqual(resolved.population_digest, population.root_hash)
            self.assertEqual(len(resolved.bound_outcomes), 2)
            self.assertEqual(
                tuple(
                    (item.case_id, item.variant)
                    for item in resolved.bound_outcomes
                ),
                (("case-63", "ABLATED"), ("case-63", "FULL")),
            )
            self.assertEqual(
                {item.reconciled_fact.evidence_digest for item in resolved.bound_outcomes},
                {fact.evidence_digest},
            )
            self.assertTrue(resolved.provenance_digest.startswith("sha256:"))
            self.assertTrue(all(not hasattr(item, "utility") for item in resolved.bound_outcomes))
            self.assertTrue(all(not hasattr(item, "cost") for item in resolved.bound_outcomes))

    def test_placeholder_utility_digest_fails_closed(self):
        with TemporaryDirectory() as directory:
            (
                _science,
                _memory,
                artifacts,
                registration,
                matched,
                _fact,
                population,
                authority,
                _evaluation_cutoff,
            ) = self._build_fixture(Path(directory))
            refs = self._publish_refs(
                artifacts,
                registration,
                matched,
                population,
                utility_digest="sha256:" + "f" * 64,
            )

            with self.assertRaisesRegex(
                MemoryIntegrityError,
                "utility evidence digest does not match",
            ):
                resolve_ablation_utility_fact_provenance(
                    authority,
                    [matched],
                    outcome_refs=list(refs),
                )

    def test_artifact_numeric_values_do_not_select_reconciled_fact(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (
                _science,
                _memory,
                artifacts,
                registration,
                matched,
                fact,
                population,
                authority,
                _evaluation_cutoff,
            ) = self._build_fixture(root)
            first_refs = self._publish_refs(
                artifacts,
                registration,
                matched,
                population,
                utility_digest=fact.evidence_digest,
                full_utility="1000000000",
                ablated_utility="-1000000000",
            )
            first = resolve_ablation_utility_fact_provenance(
                authority,
                [matched],
                outcome_refs=list(first_refs),
            )

            other_artifacts = ArtifactStore(root / "other-artifacts")
            other_authority = AblationQualificationAuthority(
                scientific_registry=authority.scientific_registry,
                experience_memory=authority.experience_memory,
                artifact_store=other_artifacts,
                protocol_id=authority.protocol_id,
                protocol_hash=authority.protocol_hash,
                source_revision=authority.source_revision,
                causal_cutoff=authority.causal_cutoff,
                granted_permissions=set(authority.granted_permissions),
                task=authority.task,
                instrument_family=authority.instrument_family,
            )
            second_refs = self._publish_refs(
                other_artifacts,
                registration,
                matched,
                population,
                utility_digest=fact.evidence_digest,
                full_utility="-1",
                ablated_utility="1",
            )
            second = resolve_ablation_utility_fact_provenance(
                other_authority,
                [matched],
                outcome_refs=list(second_refs),
            )

            self.assertEqual(
                tuple(item.reconciled_fact for item in first.bound_outcomes),
                tuple(item.reconciled_fact for item in second.bound_outcomes),
            )
            self.assertNotEqual(first.provenance_digest, second.provenance_digest)

    def test_reordered_outcome_refs_have_identical_canonical_provenance(self):
        with TemporaryDirectory() as directory:
            (
                _science,
                _memory,
                artifacts,
                registration,
                matched,
                fact,
                population,
                authority,
                _evaluation_cutoff,
            ) = self._build_fixture(Path(directory))
            refs = self._publish_refs(
                artifacts,
                registration,
                matched,
                population,
                utility_digest=fact.evidence_digest,
            )
            forward = resolve_ablation_utility_fact_provenance(
                authority,
                [matched],
                outcome_refs=list(refs),
            )
            reverse = resolve_ablation_utility_fact_provenance(
                authority,
                [matched],
                outcome_refs=list(reversed(refs)),
            )
            self.assertEqual(reverse, forward)
            self.assertEqual(reverse.provenance_digest, forward.provenance_digest)

    def test_instance_shadowed_authority_resolve_is_not_executed(self):
        calls: list[str] = []
        with TemporaryDirectory() as directory:
            (
                _science,
                _memory,
                artifacts,
                registration,
                matched,
                fact,
                population,
                authority,
                _evaluation_cutoff,
            ) = self._build_fixture(Path(directory))
            refs = self._publish_refs(
                artifacts,
                registration,
                matched,
                population,
                utility_digest=fact.evidence_digest,
            )

            def hostile(*args, **kwargs):
                calls.append("resolve")
                raise AssertionError("instance resolve shadow executed")

            authority.resolve = hostile
            authority.resolve_population = hostile
            resolved = resolve_ablation_utility_fact_provenance(
                authority,
                [matched],
                outcome_refs=list(refs),
            )
            self.assertEqual(len(resolved.bound_outcomes), 2)
            self.assertEqual(calls, [])

    def test_module_global_context_decoy_cannot_retarget_composition(self):
        calls: list[str] = []
        with TemporaryDirectory() as directory:
            (
                _science,
                _memory,
                artifacts,
                registration,
                matched,
                fact,
                population,
                authority,
                _evaluation_cutoff,
            ) = self._build_fixture(Path(directory))
            refs = self._publish_refs(
                artifacts,
                registration,
                matched,
                population,
                utility_digest=fact.evidence_digest,
            )

            original = ablation_module._registered_policy_context

            def hostile(*args, **kwargs):
                calls.append("context")
                raise AssertionError("module-global decoy context executed")

            ablation_module._registered_policy_context = hostile
            try:
                resolved = resolve_ablation_utility_fact_provenance(
                    authority,
                    [matched],
                    outcome_refs=list(refs),
                )
            finally:
                ablation_module._registered_policy_context = original
            self.assertEqual(len(resolved.bound_outcomes), 2)
            self.assertEqual(calls, [])

    def test_visible_authority_memory_retarget_fails_before_attacker_read(self):
        calls: list[str] = []
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (
                _science,
                _memory,
                artifacts,
                registration,
                matched,
                fact,
                population,
                authority,
                _evaluation_cutoff,
            ) = self._build_fixture(root)
            refs = self._publish_refs(
                artifacts,
                registration,
                matched,
                population,
                utility_digest=fact.evidence_digest,
            )
            attacker = ExperienceMemory(root / "attacker.sqlite3")

            def hostile(*args, **kwargs):
                calls.append("coverage")
                raise AssertionError("attacker memory executed")

            attacker.coverage_population_snapshot = hostile
            object.__setattr__(authority, "experience_memory", attacker)
            with self.assertRaisesRegex(
                ProtocolViolation,
                "memory binding changed after issuance",
            ):
                resolve_ablation_utility_fact_provenance(
                    authority,
                    [matched],
                    outcome_refs=list(refs),
                )
            self.assertEqual(calls, [])

    def test_provenance_digest_tamper_fails_before_reverification_reads(self):
        with TemporaryDirectory() as directory:
            (
                _science,
                _memory,
                artifacts,
                registration,
                matched,
                fact,
                population,
                authority,
                _evaluation_cutoff,
            ) = self._build_fixture(Path(directory))
            refs = self._publish_refs(
                artifacts,
                registration,
                matched,
                population,
                utility_digest=fact.evidence_digest,
            )
            evidence = resolve_ablation_utility_fact_provenance(
                authority,
                [matched],
                outcome_refs=list(refs),
            )
            object.__setattr__(evidence, "provenance_digest", "sha256:" + "f" * 64)
            with self.assertRaisesRegex(
                MemoryIntegrityError,
                "provenance digest does not match",
            ):
                reverify_ablation_utility_fact_provenance(
                    authority,
                    [matched],
                    outcome_refs=list(refs),
                    evidence=evidence,
                )

    def test_restart_reverifies_same_population_and_reconciled_fact(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (
                science,
                memory,
                artifacts,
                registration,
                matched,
                fact,
                population,
                authority,
                evaluation_cutoff,
            ) = self._build_fixture(root)
            refs = self._publish_refs(
                artifacts,
                registration,
                matched,
                population,
                utility_digest=fact.evidence_digest,
            )
            before = resolve_ablation_utility_fact_provenance(
                authority,
                [matched],
                outcome_refs=list(refs),
            )

            reopened_science = ScientificRegistry(root / "science.sqlite3")
            reopened_memory = ExperienceMemory(root / "memory.sqlite3")
            reopened_artifacts = ArtifactStore(root / "artifacts")
            after_authority = AblationQualificationAuthority(
                scientific_registry=reopened_science,
                experience_memory=reopened_memory,
                artifact_store=reopened_artifacts,
                protocol_id=registration.protocol_id,
                protocol_hash=registration.protocol_hash,
                source_revision=SOURCE_REVISION,
                causal_cutoff=evaluation_cutoff,
                granted_permissions={"RESEARCH"},
                task="ablation-qualification",
                instrument_family="EQUITY",
            )
            after = reverify_ablation_utility_fact_provenance(
                after_authority,
                [matched],
                outcome_refs=list(refs),
                evidence=before,
            )

            self.assertEqual(after, before)
            self.assertEqual(after.population_digest, before.population_digest)
            self.assertEqual(after.coverage_digest, before.coverage_digest)
            self.assertEqual(after.provenance_digest, before.provenance_digest)
            self.assertEqual(
                tuple(item.reconciled_fact for item in after.bound_outcomes),
                tuple(item.reconciled_fact for item in before.bound_outcomes),
            )
            self.assertIsNot(reopened_memory, memory)
            self.assertIsNot(reopened_science, science)
            self.assertIsInstance(before, ResolvedAblationUtilityFactProvenance)


if __name__ == "__main__":
    unittest.main()
