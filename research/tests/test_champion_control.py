from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from research.autotrade_research.learning.champion import (
    CandidateApproval,
    ChampionRegistry,
    PromotionConflict,
)
from autotrade_research.science.registry import (
    ProtocolViolation,
    ScientificRegistry,
)

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def digest(value: str) -> str:
    return "sha256:" + sha256(value.encode("utf-8")).hexdigest()


def protocol():
    return {
        "hypothesis": "candidate improves registered net outcome",
        "strategy": "candidate",
        "features": ["registered"],
        "search_space": {"variant": ["fixed"]},
        "train_period": {
            "start": "2025-01-01T00:00:00Z",
            "end": "2025-12-31T23:59:59Z",
        },
        "validation_period": {
            "start": "2026-01-02T00:00:00Z",
            "end": "2026-03-31T23:59:59Z",
        },
        "test_period": {
            "start": "2026-04-02T00:00:00Z",
            "end": "2026-06-30T23:59:59Z",
        },
        "forward_period": {
            "start": "2026-07-02T00:00:00Z",
            "end": "2026-09-30T23:59:59Z",
        },
        "labels": ["net_return"],
        "horizons": [86400],
        "purge_embargo": {
            "purge_seconds": 86400,
            "embargo_seconds": 86400,
        },
        "universe": ["AAA"],
        "cost_fill_model": "base-v1",
        "baselines": ["cash"],
        "primary_metrics": ["net_advantage"],
        "secondary_metrics": ["drawdown"],
        "trial_budget": 1,
        "stopping_rules": "one registered trial",
        "statistical_estimator": "dependence-aware",
        "multiplicity_treatment": "registered",
        "minimum_practical_effect": "0.001",
        "risk_constraints": {"max_drawdown": "0.10"},
        "retention_tolerances": {"prior_regime_loss": "0.02"},
        "promotion_rule": "all registered gates",
    }


def approval(
    science,
    candidate="candidate-a",
    valid_days=1,
    status="PASS",
    *,
    contaminate=False,
    record_trial=True,
):
    registered = science.register_protocol(protocol())
    valid_until = BASE + timedelta(days=valid_days)
    if record_trial:
        science.record_trial(
            registered.protocol_id,
            status="COMPLETED",
            payload={
                "candidate_id": candidate,
                "artifact_hash": digest(candidate),
            },
        )
    trial_state = science.completeness(registered.protocol_id)
    result = {
        "candidate_id": candidate,
        "artifact_hash": digest(candidate),
        "evaluation_status": status,
        "retention_passed": True,
        "risk_passed": True,
        "authority_scope_id": "paper-scope",
        "evidence_valid_until": valid_until.isoformat(),
        "reproducible": True,
        "causal_audit_passed": True,
        "financial_invariants_passed": True,
        "trial_log_complete": True,
        "recorded_trial_count": trial_state["recorded_trials"],
        "trial_budget": trial_state["trial_budget"],
    }
    if contaminate:
        science.record_holdout_access(
            registered.protocol_id,
            holdout_id=f"holdout-{candidate}",
            purpose="manual peek",
        )
    locked = science.register_evaluation(
        registered.protocol_id,
        holdout_id=f"holdout-{candidate}",
        result=result,
    )
    return CandidateApproval.create(
        candidate_id=candidate,
        artifact_hash=digest(candidate),
        evidence_id=f"evidence:{candidate}",
        evidence_valid_until=valid_until,
        evaluation_status=status,
        retention_passed=True,
        risk_passed=True,
        authority_scope_id="paper-scope",
        protocol_id=registered.protocol_id,
        protocol_hash=registered.protocol_hash,
        evaluation_id=locked["evaluation_id"],
        evaluation_result_hash=locked["result_hash"],
    )


class ChampionRegistryTests(unittest.TestCase):
    def test_atomic_initial_promotion_changes_future_pointer(self):
        with TemporaryDirectory() as directory:
            science = ScientificRegistry(Path(directory) / "science.sqlite3")
            registry = ChampionRegistry(
                Path(directory) / "champion.sqlite3",
                scientific_registry=science,
            )
            state = registry.promote(
                approval(science), expected_generation=0, now=BASE,
                open_position_count=0, existing_position_policy=None,
            )
            self.assertEqual(state.generation, 1)
            self.assertEqual(state.champion_candidate_id, "candidate-a")

    def test_stale_generation_cannot_overwrite_new_champion(self):
        with TemporaryDirectory() as directory:
            science = ScientificRegistry(Path(directory) / "science.sqlite3")
            registry = ChampionRegistry(
                Path(directory) / "champion.sqlite3",
                scientific_registry=science,
            )
            registry.promote(approval(science), expected_generation=0, now=BASE, open_position_count=0, existing_position_policy=None)
            with self.assertRaises(PromotionConflict):
                registry.promote(approval(science, "candidate-b"), expected_generation=0, now=BASE, open_position_count=0, existing_position_policy=None)

    def test_expired_evidence_blocks_promotion(self):
        with TemporaryDirectory() as directory:
            science = ScientificRegistry(Path(directory) / "science.sqlite3")
            registry = ChampionRegistry(
                Path(directory) / "champion.sqlite3",
                scientific_registry=science,
            )
            with self.assertRaises(ValueError):
                registry.promote(
                    approval(science, valid_days=0), expected_generation=0,
                    now=BASE + timedelta(seconds=1),
                    open_position_count=0, existing_position_policy=None,
                )

    def test_evidence_expires_at_the_exact_deadline(self):
        with TemporaryDirectory() as directory:
            science = ScientificRegistry(Path(directory) / "science.sqlite3")
            registry = ChampionRegistry(
                Path(directory) / "champion.sqlite3",
                scientific_registry=science,
            )
            candidate = approval(science, valid_days=1)
            with self.assertRaisesRegex(ValueError, "expired"):
                registry.promote(
                    candidate,
                    expected_generation=0,
                    now=BASE + timedelta(days=1),
                    open_position_count=0,
                    existing_position_policy=None,
                )

    def test_candidate_approval_requires_canonical_hashes(self):
        with TemporaryDirectory() as directory:
            science = ScientificRegistry(Path(directory) / "science.sqlite3")
            legitimate = approval(science)
            with self.assertRaisesRegex(ValueError, "artifact_hash.*sha256"):
                CandidateApproval.create(
                    candidate_id=legitimate.candidate_id,
                    artifact_hash="sha256:not-a-real-digest",
                    evidence_id=legitimate.evidence_id,
                    evidence_valid_until=legitimate.evidence_valid_until,
                    evaluation_status=legitimate.evaluation_status,
                    retention_passed=legitimate.retention_passed,
                    risk_passed=legitimate.risk_passed,
                    authority_scope_id=legitimate.authority_scope_id,
                    protocol_id=legitimate.protocol_id,
                    protocol_hash=legitimate.protocol_hash,
                    evaluation_id=legitimate.evaluation_id,
                    evaluation_result_hash=legitimate.evaluation_result_hash,
                )

    def test_failed_evaluation_blocks_promotion(self):
        with TemporaryDirectory() as directory:
            science = ScientificRegistry(Path(directory) / "science.sqlite3")
            registry = ChampionRegistry(
                Path(directory) / "champion.sqlite3",
                scientific_registry=science,
            )
            with self.assertRaises(ValueError):
                registry.promote(
                    approval(science, status="FAIL"), expected_generation=0,
                    now=BASE, open_position_count=0, existing_position_policy=None,
                )

    def test_fabricated_result_hash_cannot_promote(self):
        with TemporaryDirectory() as directory:
            science = ScientificRegistry(Path(directory) / "science.sqlite3")
            registry = ChampionRegistry(
                Path(directory) / "champion.sqlite3",
                scientific_registry=science,
            )
            legitimate = approval(science)
            forged = CandidateApproval.create(
                candidate_id=legitimate.candidate_id,
                artifact_hash=legitimate.artifact_hash,
                evidence_id=legitimate.evidence_id,
                evidence_valid_until=legitimate.evidence_valid_until,
                evaluation_status=legitimate.evaluation_status,
                retention_passed=legitimate.retention_passed,
                risk_passed=legitimate.risk_passed,
                authority_scope_id=legitimate.authority_scope_id,
                protocol_id=legitimate.protocol_id,
                protocol_hash=legitimate.protocol_hash,
                evaluation_id=legitimate.evaluation_id,
                evaluation_result_hash="sha256:" + "f" * 64,
            )
            with self.assertRaises(ProtocolViolation):
                registry.promote(
                    forged,
                    expected_generation=0,
                    now=BASE,
                    open_position_count=0,
                    existing_position_policy=None,
                )

    def test_contaminated_holdout_cannot_promote_even_with_pass_text(self):
        with TemporaryDirectory() as directory:
            science = ScientificRegistry(Path(directory) / "science.sqlite3")
            registry = ChampionRegistry(
                Path(directory) / "champion.sqlite3",
                scientific_registry=science,
            )
            contaminated = approval(science, contaminate=True)
            with self.assertRaisesRegex(ProtocolViolation, "untouched"):
                registry.promote(
                    contaminated,
                    expected_generation=0,
                    now=BASE,
                    open_position_count=0,
                    existing_position_policy=None,
                )

    def test_self_asserted_trial_log_without_registered_trial_cannot_promote(self):
        with TemporaryDirectory() as directory:
            science = ScientificRegistry(Path(directory) / "science.sqlite3")
            registry = ChampionRegistry(
                Path(directory) / "champion.sqlite3",
                scientific_registry=science,
            )
            candidate = approval(science, record_trial=False)
            with self.assertRaisesRegex(ProtocolViolation, "registered trial"):
                registry.promote(
                    candidate,
                    expected_generation=0,
                    now=BASE,
                    open_position_count=0,
                    existing_position_policy=None,
                )

    def test_completed_trial_must_be_bound_to_exact_candidate_artifact(self):
        with TemporaryDirectory() as directory:
            science = ScientificRegistry(Path(directory) / "science.sqlite3")
            registered = science.register_protocol(protocol())
            science.record_trial(
                registered.protocol_id,
                status="COMPLETED",
                payload={
                    "candidate_id": "other-candidate",
                    "artifact_hash": digest("other-candidate"),
                },
            )
            valid_until = BASE + timedelta(days=1)
            trial_state = science.completeness(registered.protocol_id)
            result = {
                "candidate_id": "candidate-a",
                "artifact_hash": digest("candidate-a"),
                "evaluation_status": "PASS",
                "retention_passed": True,
                "risk_passed": True,
                "authority_scope_id": "paper-scope",
                "evidence_valid_until": valid_until.isoformat(),
                "reproducible": True,
                "causal_audit_passed": True,
                "financial_invariants_passed": True,
                "trial_log_complete": True,
                "recorded_trial_count": trial_state["recorded_trials"],
                "trial_budget": trial_state["trial_budget"],
            }
            locked = science.register_evaluation(
                registered.protocol_id,
                holdout_id="holdout-a",
                result=result,
            )
            candidate = CandidateApproval.create(
                candidate_id="candidate-a",
                artifact_hash=digest("candidate-a"),
                evidence_id="evidence:candidate-a",
                evidence_valid_until=valid_until,
                evaluation_status="PASS",
                retention_passed=True,
                risk_passed=True,
                authority_scope_id="paper-scope",
                protocol_id=registered.protocol_id,
                protocol_hash=registered.protocol_hash,
                evaluation_id=locked["evaluation_id"],
                evaluation_result_hash=locked["result_hash"],
            )
            registry = ChampionRegistry(
                Path(directory) / "champion.sqlite3",
                scientific_registry=science,
            )
            with self.assertRaisesRegex(ProtocolViolation, "bound to candidate_id"):
                registry.promote(
                    candidate,
                    expected_generation=0,
                    now=BASE,
                    open_position_count=0,
                    existing_position_policy=None,
                )

    def test_open_positions_require_explicit_policy(self):
        with TemporaryDirectory() as directory:
            science = ScientificRegistry(Path(directory) / "science.sqlite3")
            registry = ChampionRegistry(
                Path(directory) / "champion.sqlite3",
                scientific_registry=science,
            )
            with self.assertRaises(ValueError):
                registry.promote(
                    approval(science), expected_generation=0, now=BASE,
                    open_position_count=2, existing_position_policy=None,
                )

    def test_rollback_rejects_boolean_or_invalid_generation_inputs(self):
        with TemporaryDirectory() as directory:
            science = ScientificRegistry(Path(directory) / "science.sqlite3")
            registry = ChampionRegistry(
                Path(directory) / "champion.sqlite3",
                scientific_registry=science,
            )
            registry.promote(
                approval(science, "candidate-a"),
                expected_generation=0,
                now=BASE,
                open_position_count=0,
                existing_position_policy=None,
            )
            for kwargs in (
                {"target_generation": True, "expected_generation": 1, "open_position_count": 0},
                {"target_generation": 1, "expected_generation": True, "open_position_count": 0},
                {"target_generation": 1, "expected_generation": 1, "open_position_count": True},
                {"target_generation": 1, "expected_generation": -1, "open_position_count": 0},
            ):
                with self.subTest(kwargs=kwargs):
                    with self.assertRaises(ValueError):
                        registry.rollback(
                            now=BASE,
                            existing_position_policy=None,
                            **kwargs,
                        )

    def test_rollback_changes_future_pointer_without_erasing_history(self):
        with TemporaryDirectory() as directory:
            science = ScientificRegistry(Path(directory) / "science.sqlite3")
            registry = ChampionRegistry(
                Path(directory) / "champion.sqlite3",
                scientific_registry=science,
            )
            registry.promote(
                approval(science, "candidate-a"), expected_generation=0, now=BASE,
                open_position_count=0, existing_position_policy=None,
            )
            registry.promote(
                approval(science, "candidate-b"), expected_generation=1, now=BASE,
                open_position_count=0, existing_position_policy=None,
            )
            state = registry.rollback(
                target_generation=1, expected_generation=2, now=BASE,
                open_position_count=1,
                existing_position_policy="manage-under-original-exit-owner",
            )
            self.assertEqual(state.generation, 3)
            self.assertEqual(state.champion_candidate_id, "candidate-a")
            self.assertEqual(
                state.existing_position_policy,
                "manage-under-original-exit-owner",
            )
            self.assertEqual(
                [row["action"] for row in registry.history()],
                ["PROMOTE", "PROMOTE", "ROLLBACK"],
            )


if __name__ == "__main__":
    unittest.main()
