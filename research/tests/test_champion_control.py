from datetime import datetime, timedelta, timezone, tzinfo
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from research.autotrade_research.learning.champion import (
    CandidateApproval,
    ChampionRegistry,
    OnlineEnvelope,
    ParameterBound,
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
        "trial_log_hash": trial_state["trial_log_hash"],
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


def online_envelope(candidate="candidate-a", *, interval=60, max_cost="2"):
    return OnlineEnvelope.create(
        envelope_id="online-envelope-v1",
        champion_artifact_hash=digest(candidate),
        authority_scope_id="paper-scope",
        parameter_bounds=(
            ParameterBound.create(name="threshold", minimum="0.1", maximum="0.9"),
            ParameterBound.create(name="weight", minimum="0", maximum="1"),
        ),
        minimum_update_interval_seconds=interval,
        maximum_update_cost=max_cost,
        eligible_label_refs=("label:reconciled-outcome",),
    )


class _NoOffsetTZ(tzinfo):
    def utcoffset(self, dt):
        return None

    def dst(self, dt):
        return None


class ChampionRegistryTests(unittest.TestCase):
    def test_promotion_with_unused_trial_budget_requires_registered_stop_evidence(self):
        with TemporaryDirectory() as directory:
            science = ScientificRegistry(Path(directory) / "science.sqlite3")
            value = protocol()
            value["trial_budget"] = 2
            value["stopping_rules"] = "stop after invariant failure"
            registered = science.register_protocol(value)
            candidate = "candidate-early-stop"
            artifact = digest(candidate)
            science.record_trial(
                registered.protocol_id,
                status="COMPLETED",
                payload={
                    "candidate_id": candidate,
                    "artifact_hash": artifact,
                },
            )
            trial_state = science.completeness(registered.protocol_id)
            valid_until = BASE + timedelta(days=1)
            base_result = {
                "candidate_id": candidate,
                "artifact_hash": artifact,
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
                "trial_log_hash": trial_state["trial_log_hash"],
            }
            locked = science.register_evaluation(
                registered.protocol_id,
                holdout_id="holdout-early-stop",
                result=base_result,
            )
            approval_value = CandidateApproval.create(
                candidate_id=candidate,
                artifact_hash=artifact,
                evidence_id="evidence:early-stop",
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
            with self.assertRaisesRegex(
                ProtocolViolation,
                "trial-budget exhaustion",
            ):
                registry.promote(
                    approval_value,
                    expected_generation=0,
                    now=BASE,
                    open_position_count=0,
                    existing_position_policy=None,
                )

    def test_locked_evaluation_trial_log_hash_detects_trial_log_tampering(self):
        with TemporaryDirectory() as directory:
            science = ScientificRegistry(Path(directory) / "science.sqlite3")
            value = protocol()
            value["trial_budget"] = 2
            registered = science.register_protocol(value)
            candidate = "candidate-log-bound"
            artifact = digest(candidate)
            science.record_trial(
                registered.protocol_id,
                status="COMPLETED",
                payload={
                    "candidate_id": candidate,
                    "artifact_hash": artifact,
                },
                trial_id="00000000-0000-0000-0000-000000000021",
            )
            science.record_trial(
                registered.protocol_id,
                status="FAILED",
                payload={"reason": "fit-failed"},
                trial_id="00000000-0000-0000-0000-000000000022",
            )
            trial_state = science.completeness(registered.protocol_id)
            valid_until = BASE + timedelta(days=1)
            result = {
                "candidate_id": candidate,
                "artifact_hash": artifact,
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
                "trial_log_hash": trial_state["trial_log_hash"],
            }
            locked = science.register_evaluation(
                registered.protocol_id,
                holdout_id="holdout-log-bound",
                result=result,
            )
            # Simulate storage corruption/tampering that keeps trial count and
            # statuses unchanged. Promotion must still detect the changed log.
            with science._connect() as con:
                con.execute(
                    "UPDATE trials SET payload_hash=? WHERE trial_id=?",
                    (
                        digest("tampered-trial-payload"),
                        "00000000-0000-0000-0000-000000000022",
                    ),
                )
            approval_value = CandidateApproval.create(
                candidate_id=candidate,
                artifact_hash=artifact,
                evidence_id="evidence:log-bound",
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
            with self.assertRaisesRegex(
                ProtocolViolation,
                "trial_log_hash",
            ):
                registry.promote(
                    approval_value,
                    expected_generation=0,
                    now=BASE,
                    open_position_count=0,
                    existing_position_policy=None,
                )

    def test_candidate_evidence_time_requires_effective_utc_offset(self):
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            CandidateApproval.create(
                candidate_id="candidate-a",
                artifact_hash=digest("candidate-a"),
                evidence_id="evidence:candidate-a",
                evidence_valid_until=datetime(
                    2026, 9, 25, 0, 0, tzinfo=_NoOffsetTZ()
                ),
                evaluation_status="PASS",
                retention_passed=True,
                risk_passed=True,
                authority_scope_id="scope-a",
                protocol_id="protocol-a",
                protocol_hash=digest("protocol-a"),
                evaluation_id="evaluation-a",
                evaluation_result_hash=digest("evaluation-a"),
            )

    def test_direct_candidate_approval_cannot_bypass_identity_or_boolean_guards(self):
        common = dict(
            candidate_id="candidate-a",
            artifact_hash=digest("candidate-a"),
            evidence_id="evidence:candidate-a",
            evidence_valid_until=BASE + timedelta(days=1),
            evaluation_status="PASS",
            retention_passed=True,
            risk_passed=True,
            authority_scope_id="paper-scope",
            protocol_id="protocol-1",
            protocol_hash=digest("protocol"),
            evaluation_id="evaluation-1",
            evaluation_result_hash=digest("evaluation"),
        )
        with self.assertRaisesRegex(ValueError, "artifact_hash"):
            CandidateApproval(**{**common, "artifact_hash": "not-a-digest"})
        with self.assertRaisesRegex(TypeError, "boolean"):
            CandidateApproval(**{**common, "retention_passed": 1})
        with self.assertRaisesRegex(ValueError, "evaluation_status"):
            CandidateApproval(**{**common, "evaluation_status": "APPROVED"})

    def test_direct_parameter_bound_cannot_bypass_exact_range_guards(self):
        with self.assertRaisesRegex(TypeError, "exact decimal"):
            ParameterBound(name="threshold", minimum=0.1, maximum="0.9")
        with self.assertRaisesRegex(ValueError, "minimum"):
            ParameterBound(name="threshold", minimum="1", maximum="0")

    def test_direct_online_envelope_cannot_forge_content_hash(self):
        bound = ParameterBound.create(
            name="threshold",
            minimum="0.1",
            maximum="0.9",
        )
        with self.assertRaisesRegex(ValueError, "envelope_hash"):
            OnlineEnvelope(
                envelope_id="forged-envelope",
                champion_artifact_hash=digest("candidate-a"),
                authority_scope_id="paper-scope",
                parameter_bounds=(bound,),
                minimum_update_interval_seconds=60,
                maximum_update_cost="1",
                eligible_label_refs=("label:reconciled-outcome",),
                envelope_hash=digest("unrelated-content"),
            )

    def test_direct_online_envelope_rejects_duplicate_parameter_names(self):
        first = ParameterBound.create(
            name="threshold",
            minimum="0.1",
            maximum="0.9",
        )
        second = ParameterBound.create(
            name="threshold",
            minimum="0.2",
            maximum="0.8",
        )
        with self.assertRaisesRegex(ValueError, "parameter names"):
            OnlineEnvelope(
                envelope_id="duplicate-bounds",
                champion_artifact_hash=digest("candidate-a"),
                authority_scope_id="paper-scope",
                parameter_bounds=(first, second),
                minimum_update_interval_seconds=60,
                maximum_update_cost="1",
                eligible_label_refs=("label:reconciled-outcome",),
                envelope_hash=digest("cannot-be-valid"),
            )

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

    def test_holdout_peek_after_locked_evaluation_invalidates_promotion(self):
        with TemporaryDirectory() as directory:
            science = ScientificRegistry(Path(directory) / "science.sqlite3")
            registry = ChampionRegistry(
                Path(directory) / "champion.sqlite3",
                scientific_registry=science,
            )
            candidate = approval(science)
            evidence = science.locked_evaluation(candidate.evaluation_id)
            science.record_holdout_access(
                candidate.protocol_id,
                holdout_id=evidence.holdout_id,
                purpose="post-evaluation manual inspection",
            )
            with self.assertRaisesRegex(
                ProtocolViolation,
                "remain untouched",
            ):
                registry.promote(
                    candidate,
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


    def test_promotion_retry_after_response_loss_is_idempotent(self):
        with TemporaryDirectory() as directory:
            science = ScientificRegistry(Path(directory) / "science.sqlite3")
            registry = ChampionRegistry(
                Path(directory) / "champion.sqlite3",
                scientific_registry=science,
            )
            candidate = approval(science)
            first = registry.promote(
                candidate,
                expected_generation=0,
                now=BASE,
                open_position_count=0,
                existing_position_policy=None,
            )
            retry = registry.promote(
                candidate,
                expected_generation=0,
                now=BASE + timedelta(seconds=1),
                open_position_count=0,
                existing_position_policy=None,
            )
            self.assertEqual(retry, first)
            self.assertEqual(len(registry.history()), 1)

    def test_same_generation_retry_with_changed_request_conflicts(self):
        with TemporaryDirectory() as directory:
            science = ScientificRegistry(Path(directory) / "science.sqlite3")
            registry = ChampionRegistry(
                Path(directory) / "champion.sqlite3",
                scientific_registry=science,
            )
            candidate = approval(science)
            registry.promote(
                candidate,
                expected_generation=0,
                now=BASE,
                open_position_count=0,
                existing_position_policy=None,
            )
            with self.assertRaises(PromotionConflict):
                registry.promote(
                    candidate,
                    expected_generation=0,
                    now=BASE + timedelta(seconds=1),
                    open_position_count=0,
                    existing_position_policy="changed-policy",
                )

    def test_rollback_retry_after_response_loss_is_idempotent(self):
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
            registry.promote(
                approval(science, "candidate-b"),
                expected_generation=1,
                now=BASE,
                open_position_count=0,
                existing_position_policy=None,
            )
            first = registry.rollback(
                target_generation=1,
                expected_generation=2,
                now=BASE,
                open_position_count=1,
                existing_position_policy="manage-under-original-exit-owner",
            )
            retry = registry.rollback(
                target_generation=1,
                expected_generation=2,
                now=BASE + timedelta(seconds=1),
                open_position_count=1,
                existing_position_policy="manage-under-original-exit-owner",
            )
            self.assertEqual(retry, first)
            self.assertEqual(
                [row["action"] for row in registry.history()],
                ["PROMOTE", "PROMOTE", "ROLLBACK"],
            )

    def test_online_update_inside_envelope_is_recorded_without_route_change(self):
        with TemporaryDirectory() as directory:
            science = ScientificRegistry(Path(directory) / "science.sqlite3")
            registry = ChampionRegistry(
                Path(directory) / "champion.sqlite3",
                scientific_registry=science,
            )
            state = registry.promote(
                approval(science),
                expected_generation=0,
                now=BASE,
                open_position_count=0,
                existing_position_policy=None,
            )
            row = registry.record_online_update(
                envelope=online_envelope(),
                update_id="update-1",
                expected_generation=state.generation,
                updates={"threshold": "0.4"},
                label_refs=("label:reconciled-outcome",),
                evidence_refs=("episode:1",),
                actual_update_cost="1.25",
                now=BASE + timedelta(minutes=1),
                drift_gate_passed=True,
                stop_condition_triggered=False,
            )
            self.assertEqual(row["routing_generation"], 1)
            self.assertEqual(registry.state(), state)

    def test_online_update_outside_parameter_range_requires_new_candidate(self):
        with TemporaryDirectory() as directory:
            science = ScientificRegistry(Path(directory) / "science.sqlite3")
            registry = ChampionRegistry(
                Path(directory) / "champion.sqlite3",
                scientific_registry=science,
            )
            state = registry.promote(
                approval(science),
                expected_generation=0,
                now=BASE,
                open_position_count=0,
                existing_position_policy=None,
            )
            with self.assertRaisesRegex(ValueError, "outside the approved online range"):
                registry.record_online_update(
                    envelope=online_envelope(),
                    update_id="outside-range",
                    expected_generation=state.generation,
                    updates={"threshold": "0.95"},
                    label_refs=("label:reconciled-outcome",),
                    evidence_refs=("episode:2",),
                    actual_update_cost="1",
                    now=BASE + timedelta(minutes=1),
                    drift_gate_passed=True,
                    stop_condition_triggered=False,
                )

    def test_record_online_update_revalidates_direct_envelope_and_bounds(self):
        with TemporaryDirectory() as directory:
            science = ScientificRegistry(Path(directory) / "science.sqlite3")
            registry = ChampionRegistry(
                Path(directory) / "champion.sqlite3",
                scientific_registry=science,
            )
            state = registry.promote(
                approval(science),
                expected_generation=0,
                now=BASE,
                open_position_count=0,
                existing_position_policy=None,
            )
            forged_bound = ParameterBound(
                name="threshold",
                minimum=Decimal("0.9"),
                maximum=Decimal("0.1"),
            )
            forged = OnlineEnvelope(
                envelope_id="forged-envelope",
                champion_artifact_hash=digest("candidate-a"),
                authority_scope_id="paper-scope",
                parameter_bounds=(forged_bound,),
                minimum_update_interval_seconds=0,
                maximum_update_cost=Decimal("2"),
                eligible_label_refs=("label:reconciled-outcome",),
                envelope_hash="sha256:" + "0" * 64,
            )
            with self.assertRaisesRegex(ValueError, "minimum cannot exceed maximum"):
                registry.record_online_update(
                    envelope=forged,
                    update_id="forged-update",
                    expected_generation=state.generation,
                    updates={"threshold": "0.5"},
                    label_refs=("label:reconciled-outcome",),
                    evidence_refs=("episode:forged",),
                    actual_update_cost="1",
                    now=BASE + timedelta(minutes=1),
                    drift_gate_passed=True,
                    stop_condition_triggered=False,
                )

    def test_record_online_update_ignores_forged_envelope_hash_and_uses_canonical_content(self):
        with TemporaryDirectory() as directory:
            science = ScientificRegistry(Path(directory) / "science.sqlite3")
            registry = ChampionRegistry(
                Path(directory) / "champion.sqlite3",
                scientific_registry=science,
            )
            state = registry.promote(
                approval(science),
                expected_generation=0,
                now=BASE,
                open_position_count=0,
                existing_position_policy=None,
            )
            canonical = online_envelope()
            forged = OnlineEnvelope(
                **{
                    **canonical.__dict__,
                    "envelope_hash": "sha256:" + "0" * 64,
                }
            )
            row = registry.record_online_update(
                envelope=forged,
                update_id="canonicalized-envelope-hash",
                expected_generation=state.generation,
                updates={"threshold": "0.4"},
                label_refs=("label:reconciled-outcome",),
                evidence_refs=("episode:canonicalized",),
                actual_update_cost="1",
                now=BASE + timedelta(minutes=1),
                drift_gate_passed=True,
                stop_condition_triggered=False,
            )
            self.assertEqual(row["envelope_hash"], canonical.envelope_hash)
            self.assertNotEqual(row["envelope_hash"], forged.envelope_hash)

    def test_online_update_unknown_parameter_is_blocked(self):
        envelope = online_envelope()
        with self.assertRaisesRegex(ValueError, "outside the approved online envelope"):
            envelope.normalize_updates({"unregistered_parameter": "0.2"})

    def test_online_update_uses_exact_decimal_not_float(self):
        envelope = online_envelope()
        with self.assertRaises(TypeError):
            envelope.normalize_updates({"threshold": 0.4})

    def test_online_update_respects_label_and_resource_envelope(self):
        with TemporaryDirectory() as directory:
            science = ScientificRegistry(Path(directory) / "science.sqlite3")
            registry = ChampionRegistry(
                Path(directory) / "champion.sqlite3",
                scientific_registry=science,
            )
            state = registry.promote(
                approval(science),
                expected_generation=0,
                now=BASE,
                open_position_count=0,
                existing_position_policy=None,
            )
            with self.assertRaisesRegex(ValueError, "labels outside"):
                registry.record_online_update(
                    envelope=online_envelope(),
                    update_id="bad-label",
                    expected_generation=state.generation,
                    updates={"threshold": "0.4"},
                    label_refs=("label:future-or-unapproved",),
                    evidence_refs=("episode:3",),
                    actual_update_cost="1",
                    now=BASE + timedelta(minutes=1),
                    drift_gate_passed=True,
                    stop_condition_triggered=False,
                )
            with self.assertRaisesRegex(ValueError, "resource budget"):
                registry.record_online_update(
                    envelope=online_envelope(max_cost="1"),
                    update_id="too-expensive",
                    expected_generation=state.generation,
                    updates={"threshold": "0.4"},
                    label_refs=("label:reconciled-outcome",),
                    evidence_refs=("episode:4",),
                    actual_update_cost="1.01",
                    now=BASE + timedelta(minutes=1),
                    drift_gate_passed=True,
                    stop_condition_triggered=False,
                )

    def test_online_update_drift_and_stop_gates_fail_closed(self):
        with TemporaryDirectory() as directory:
            science = ScientificRegistry(Path(directory) / "science.sqlite3")
            registry = ChampionRegistry(
                Path(directory) / "champion.sqlite3",
                scientific_registry=science,
            )
            state = registry.promote(
                approval(science),
                expected_generation=0,
                now=BASE,
                open_position_count=0,
                existing_position_policy=None,
            )
            common = dict(
                envelope=online_envelope(),
                expected_generation=state.generation,
                updates={"threshold": "0.4"},
                label_refs=("label:reconciled-outcome",),
                evidence_refs=("episode:5",),
                actual_update_cost="1",
                now=BASE + timedelta(minutes=1),
            )
            with self.assertRaisesRegex(ValueError, "drift gate"):
                registry.record_online_update(
                    update_id="drift-failed",
                    drift_gate_passed=False,
                    stop_condition_triggered=False,
                    **common,
                )
            with self.assertRaisesRegex(ValueError, "stop condition"):
                registry.record_online_update(
                    update_id="stop-triggered",
                    drift_gate_passed=True,
                    stop_condition_triggered=True,
                    **common,
                )

    def test_online_update_frequency_is_serialized_and_enforced(self):
        with TemporaryDirectory() as directory:
            science = ScientificRegistry(Path(directory) / "science.sqlite3")
            registry = ChampionRegistry(
                Path(directory) / "champion.sqlite3",
                scientific_registry=science,
            )
            state = registry.promote(
                approval(science),
                expected_generation=0,
                now=BASE,
                open_position_count=0,
                existing_position_policy=None,
            )
            envelope = online_envelope(interval=60)
            def apply(update_id, when):
                return registry.record_online_update(
                    envelope=envelope,
                    update_id=update_id,
                    expected_generation=state.generation,
                    updates={"threshold": "0.4"},
                    label_refs=("label:reconciled-outcome",),
                    evidence_refs=("episode:frequency",),
                    actual_update_cost="1",
                    now=when,
                    drift_gate_passed=True,
                    stop_condition_triggered=False,
                )
            apply("frequency-1", BASE + timedelta(minutes=1))
            with self.assertRaisesRegex(ValueError, "update frequency"):
                apply("frequency-too-soon", BASE + timedelta(seconds=90))
            apply("frequency-2", BASE + timedelta(minutes=2))

    def test_online_update_retry_is_idempotent_but_changed_payload_conflicts(self):
        with TemporaryDirectory() as directory:
            science = ScientificRegistry(Path(directory) / "science.sqlite3")
            registry = ChampionRegistry(
                Path(directory) / "champion.sqlite3",
                scientific_registry=science,
            )
            state = registry.promote(
                approval(science),
                expected_generation=0,
                now=BASE,
                open_position_count=0,
                existing_position_policy=None,
            )
            kwargs = dict(
                envelope=online_envelope(),
                update_id="retry-update",
                expected_generation=state.generation,
                updates={"threshold": "0.4"},
                label_refs=("label:reconciled-outcome",),
                evidence_refs=("episode:retry",),
                actual_update_cost="1",
                drift_gate_passed=True,
                stop_condition_triggered=False,
            )
            first = registry.record_online_update(
                now=BASE + timedelta(minutes=1),
                **kwargs,
            )
            retry = registry.record_online_update(
                now=BASE + timedelta(minutes=5),
                **kwargs,
            )
            self.assertEqual(retry["request_fingerprint"], first["request_fingerprint"])
            with self.assertRaises(PromotionConflict):
                registry.record_online_update(
                    envelope=kwargs["envelope"],
                    update_id="retry-update",
                    expected_generation=state.generation,
                    updates={"threshold": "0.5"},
                    label_refs=kwargs["label_refs"],
                    evidence_refs=kwargs["evidence_refs"],
                    actual_update_cost="1",
                    now=BASE + timedelta(minutes=5),
                    drift_gate_passed=True,
                    stop_condition_triggered=False,
                )

    def test_online_envelope_is_bound_to_exact_active_champion_generation(self):
        with TemporaryDirectory() as directory:
            science = ScientificRegistry(Path(directory) / "science.sqlite3")
            registry = ChampionRegistry(
                Path(directory) / "champion.sqlite3",
                scientific_registry=science,
            )
            first = registry.promote(
                approval(science, "candidate-a"),
                expected_generation=0,
                now=BASE,
                open_position_count=0,
                existing_position_policy=None,
            )
            registry.promote(
                approval(science, "candidate-b"),
                expected_generation=first.generation,
                now=BASE,
                open_position_count=0,
                existing_position_policy=None,
            )
            with self.assertRaises(PromotionConflict):
                registry.record_online_update(
                    envelope=online_envelope("candidate-a"),
                    update_id="stale-envelope",
                    expected_generation=first.generation,
                    updates={"threshold": "0.4"},
                    label_refs=("label:reconciled-outcome",),
                    evidence_refs=("episode:stale",),
                    actual_update_cost="1",
                    now=BASE + timedelta(minutes=1),
                    drift_gate_passed=True,
                    stop_condition_triggered=False,
                )


    def test_same_envelope_id_cannot_change_immutable_content(self):
        with TemporaryDirectory() as directory:
            science = ScientificRegistry(Path(directory) / "science.sqlite3")
            registry = ChampionRegistry(
                Path(directory) / "champion.sqlite3",
                scientific_registry=science,
            )
            state = registry.promote(
                approval(science),
                expected_generation=0,
                now=BASE,
                open_position_count=0,
                existing_position_policy=None,
            )
            registry.record_online_update(
                envelope=online_envelope(interval=0),
                update_id="envelope-first",
                expected_generation=state.generation,
                updates={"threshold": "0.4"},
                label_refs=("label:reconciled-outcome",),
                evidence_refs=("episode:envelope-first",),
                actual_update_cost="1",
                now=BASE + timedelta(minutes=1),
                drift_gate_passed=True,
                stop_condition_triggered=False,
            )
            changed = OnlineEnvelope.create(
                envelope_id="online-envelope-v1",
                champion_artifact_hash=digest("candidate-a"),
                authority_scope_id="paper-scope",
                parameter_bounds=(
                    ParameterBound.create(
                        name="threshold",
                        minimum="0.2",
                        maximum="0.8",
                    ),
                ),
                minimum_update_interval_seconds=0,
                maximum_update_cost="2",
                eligible_label_refs=("label:reconciled-outcome",),
            )
            with self.assertRaisesRegex(ValueError, "immutable content"):
                registry.record_online_update(
                    envelope=changed,
                    update_id="envelope-changed",
                    expected_generation=state.generation,
                    updates={"threshold": "0.4"},
                    label_refs=("label:reconciled-outcome",),
                    evidence_refs=("episode:envelope-changed",),
                    actual_update_cost="1",
                    now=BASE + timedelta(minutes=2),
                    drift_gate_passed=True,
                    stop_condition_triggered=False,
                )

    def test_duplicate_label_or_evidence_refs_fail_closed(self):
        with TemporaryDirectory() as directory:
            science = ScientificRegistry(Path(directory) / "science.sqlite3")
            registry = ChampionRegistry(
                Path(directory) / "champion.sqlite3",
                scientific_registry=science,
            )
            state = registry.promote(
                approval(science),
                expected_generation=0,
                now=BASE,
                open_position_count=0,
                existing_position_policy=None,
            )
            common = dict(
                envelope=online_envelope(),
                expected_generation=state.generation,
                updates={"threshold": "0.4"},
                actual_update_cost="1",
                now=BASE + timedelta(minutes=1),
                drift_gate_passed=True,
                stop_condition_triggered=False,
            )
            with self.assertRaisesRegex(ValueError, "label references must be unique"):
                registry.record_online_update(
                    update_id="duplicate-label",
                    label_refs=(
                        "label:reconciled-outcome",
                        "label:reconciled-outcome",
                    ),
                    evidence_refs=("episode:unique",),
                    **common,
                )
            with self.assertRaisesRegex(ValueError, "evidence references must be unique"):
                registry.record_online_update(
                    update_id="duplicate-evidence",
                    label_refs=("label:reconciled-outcome",),
                    evidence_refs=("episode:dup", "episode:dup"),
                    **common,
                )

if __name__ == "__main__":
    unittest.main()
