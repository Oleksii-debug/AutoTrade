from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
import unittest

from research.autotrade_research.learning.waves import (
    CandidateEvaluation,
    CandidateWave,
    EvidencePopulation,
    LearningWavePolicy,
    MarketWaveSnapshot,
    evaluate_pause,
    resolve_candidate,
)


BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def digest(label: str) -> str:
    return "sha256:" + sha256(label.encode("utf-8")).hexdigest()


def policy(**overrides) -> LearningWavePolicy:
    values = dict(
        policy_id="wave-policy-v1",
        max_market_seconds=3600,
        max_trades=20,
        min_evidence_events=50,
        max_drawdown="0.05",
        pause_on_regime_change=True,
        promotion_mode="CONFIRMATION",
    )
    values.update(overrides)
    return LearningWavePolicy(**values)


def snapshot(**overrides) -> MarketWaveSnapshot:
    values = dict(
        wave_id="wave-0001",
        segment_id="segment-0001",
        champion_artifact_hash=digest("champion"),
        source_cut_hash=digest("paused-cut"),
        segment_started_at=BASE,
        observed_at=BASE + timedelta(minutes=10),
        trades_since_pause=3,
        evidence_events_since_pause=8,
        drawdown_since_pause=Decimal("0.01"),
        previous_regime_id="calm",
        current_regime_id="calm",
    )
    values.update(overrides)
    return MarketWaveSnapshot(**values)


def population(
    name: str,
    *,
    causal_cut: str,
    available_at: datetime,
    observations: tuple[str, ...],
) -> EvidencePopulation:
    return EvidencePopulation(
        population_id=name,
        root_hash=digest(f"population:{name}"),
        causal_cut_hash=causal_cut,
        available_at=available_at,
        observation_ids=observations,
    )


def paused_decision(*, promotion_mode="CONFIRMATION"):
    p = policy(promotion_mode=promotion_mode)
    snap = snapshot(trades_since_pause=p.max_trades)
    decision = evaluate_pause(p, snap)
    return p, snap, decision


def candidate_wave(*, promotion_mode="CONFIRMATION") -> CandidateWave:
    p, snap, decision = paused_decision(promotion_mode=promotion_mode)
    training = population(
        "train",
        causal_cut=snap.source_cut_hash,
        available_at=BASE + timedelta(minutes=10),
        observations=("obs-1", "obs-2", "obs-3"),
    )
    validation = population(
        "validation",
        causal_cut=digest("validation-cut"),
        available_at=BASE + timedelta(minutes=20),
        observations=("obs-4", "obs-5"),
    )
    return CandidateWave.from_pause(
        pause=decision,
        policy=p,
        champion_artifact_hash=snap.champion_artifact_hash,
        candidate_id="candidate-0001",
        candidate_artifact_hash=digest("candidate"),
        candidate_created_at=BASE + timedelta(minutes=15),
        error_analysis_hash=digest("error-analysis"),
        change_summary="Correct observed errors without changing hard risk",
        training_population=training,
        validation_population=validation,
        validation_opened_at=BASE + timedelta(minutes=20),
    )


def evaluation(
    *,
    status="PASS",
    science=True,
    retention=True,
    risk=True,
    candidate_hash=None,
) -> CandidateEvaluation:
    return CandidateEvaluation(
        candidate_artifact_hash=candidate_hash or digest("candidate"),
        evaluation_status=status,
        evaluation_hash=digest(
            f"evaluation:{status}:{science}:{retention}:{risk}"
        ),
        science_gate_passed=science,
        retention_gate_passed=retention,
        risk_gate_passed=risk,
    )


class LearningWavePauseTests(unittest.TestCase):
    def test_continue_when_no_registered_trigger_is_reached(self):
        result = evaluate_pause(policy(), snapshot())
        self.assertFalse(result.should_pause)
        self.assertEqual(result.reasons, ())

    def test_time_trigger_pauses(self):
        result = evaluate_pause(
            policy(),
            snapshot(observed_at=BASE + timedelta(hours=1)),
        )
        self.assertTrue(result.should_pause)
        self.assertIn("LEARNING_WAVE.TIME_THRESHOLD", result.reasons)

    def test_trade_trigger_pauses(self):
        result = evaluate_pause(policy(), snapshot(trades_since_pause=20))
        self.assertIn("LEARNING_WAVE.TRADE_THRESHOLD", result.reasons)

    def test_evidence_trigger_pauses(self):
        result = evaluate_pause(
            policy(),
            snapshot(evidence_events_since_pause=50),
        )
        self.assertIn("LEARNING_WAVE.EVIDENCE_THRESHOLD", result.reasons)

    def test_regime_change_trigger_pauses(self):
        result = evaluate_pause(
            policy(),
            snapshot(current_regime_id="stress"),
        )
        self.assertIn("LEARNING_WAVE.REGIME_CHANGE", result.reasons)

    def test_drawdown_trigger_pauses(self):
        result = evaluate_pause(
            policy(),
            snapshot(drawdown_since_pause=Decimal("0.05")),
        )
        self.assertIn("LEARNING_WAVE.DRAWDOWN_THRESHOLD", result.reasons)

    def test_multiple_pause_reasons_are_preserved_in_stable_order(self):
        result = evaluate_pause(
            policy(),
            snapshot(
                observed_at=BASE + timedelta(hours=2),
                trades_since_pause=25,
                evidence_events_since_pause=60,
                current_regime_id="stress",
                drawdown_since_pause=Decimal("0.08"),
            ),
        )
        self.assertEqual(
            result.reasons,
            (
                "LEARNING_WAVE.TIME_THRESHOLD",
                "LEARNING_WAVE.TRADE_THRESHOLD",
                "LEARNING_WAVE.EVIDENCE_THRESHOLD",
                "LEARNING_WAVE.REGIME_CHANGE",
                "LEARNING_WAVE.DRAWDOWN_THRESHOLD",
            ),
        )

    def test_pause_hash_is_deterministic(self):
        first = evaluate_pause(policy(), snapshot(trades_since_pause=20))
        second = evaluate_pause(policy(), snapshot(trades_since_pause=20))
        self.assertEqual(first.decision_hash, second.decision_hash)

    def test_same_policy_id_with_changed_threshold_has_different_authority_hash(self):
        first = policy(max_trades=20)
        second = policy(max_trades=21)
        self.assertNotEqual(first.policy_hash, second.policy_hash)
        first_decision = evaluate_pause(first, snapshot(trades_since_pause=25))
        second_decision = evaluate_pause(second, snapshot(trades_since_pause=25))
        self.assertNotEqual(first_decision.decision_hash, second_decision.decision_hash)

    def test_no_hidden_universal_pause_threshold_is_allowed(self):
        with self.assertRaisesRegex(ValueError, "at least one"):
            LearningWavePolicy(policy_id="empty")

    def test_noncanonical_float_drawdown_is_rejected(self):
        with self.assertRaisesRegex(TypeError, "exact decimal"):
            snapshot(drawdown_since_pause=0.01)

    def test_backward_market_clock_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "cannot precede"):
            snapshot(observed_at=BASE - timedelta(seconds=1))


class CandidateWaveIsolationTests(unittest.TestCase):
    def test_candidate_requires_actual_pause(self):
        p = policy()
        snap = snapshot()
        decision = evaluate_pause(p, snap)
        train = population(
            "train",
            causal_cut=snap.source_cut_hash,
            available_at=BASE,
            observations=("obs-1",),
        )
        validation = population(
            "validation",
            causal_cut=digest("validation-cut"),
            available_at=BASE,
            observations=("obs-2",),
        )
        with self.assertRaisesRegex(ValueError, "real pause"):
            CandidateWave.from_pause(
                pause=decision,
                policy=p,
                champion_artifact_hash=snap.champion_artifact_hash,
                candidate_id="candidate",
                candidate_artifact_hash=digest("candidate"),
                candidate_created_at=BASE + timedelta(minutes=1),
                error_analysis_hash=digest("error-analysis"),
                change_summary="Correct observed errors without changing hard risk",
                training_population=train,
                validation_population=validation,
                validation_opened_at=BASE + timedelta(minutes=1),
            )

    def test_pause_policy_cannot_be_swapped_before_candidate_handoff(self):
        p, snap, decision = paused_decision(promotion_mode="CONFIRMATION")
        swapped = policy(promotion_mode="AUTO")
        training = population(
            "train",
            causal_cut=snap.source_cut_hash,
            available_at=BASE,
            observations=("obs-1",),
        )
        validation = population(
            "validation",
            causal_cut=digest("validation-cut"),
            available_at=BASE,
            observations=("obs-2",),
        )
        with self.assertRaisesRegex(ValueError, "exact pause decision"):
            CandidateWave.from_pause(
                pause=decision,
                policy=swapped,
                champion_artifact_hash=snap.champion_artifact_hash,
                candidate_id="candidate",
                candidate_artifact_hash=digest("candidate"),
                candidate_created_at=BASE + timedelta(minutes=1),
                error_analysis_hash=digest("error-analysis"),
                change_summary="Correct observed errors without changing hard risk",
                training_population=training,
                validation_population=validation,
                validation_opened_at=BASE + timedelta(minutes=1),
            )
        self.assertEqual(p.promotion_mode, "CONFIRMATION")

    def test_champion_cannot_be_swapped_after_pause(self):
        p, snap, decision = paused_decision()
        training = population(
            "train",
            causal_cut=snap.source_cut_hash,
            available_at=BASE,
            observations=("obs-1",),
        )
        validation = population(
            "validation",
            causal_cut=digest("validation-cut"),
            available_at=BASE,
            observations=("obs-2",),
        )
        with self.assertRaisesRegex(ValueError, "champion does not match"):
            CandidateWave.from_pause(
                pause=decision,
                policy=p,
                champion_artifact_hash=digest("different-champion"),
                candidate_id="candidate",
                candidate_artifact_hash=digest("candidate"),
                candidate_created_at=BASE + timedelta(minutes=1),
                error_analysis_hash=digest("error-analysis"),
                change_summary="Correct observed errors without changing hard risk",
                training_population=training,
                validation_population=validation,
                validation_opened_at=BASE + timedelta(minutes=1),
            )

    def test_training_population_must_bind_paused_causal_cut(self):
        p, snap, decision = paused_decision()
        training = population(
            "train",
            causal_cut=digest("wrong-cut"),
            available_at=BASE,
            observations=("obs-1",),
        )
        validation = population(
            "validation",
            causal_cut=digest("validation-cut"),
            available_at=BASE,
            observations=("obs-2",),
        )
        with self.assertRaisesRegex(ValueError, "paused causal cut"):
            CandidateWave.from_pause(
                pause=decision,
                policy=p,
                champion_artifact_hash=snap.champion_artifact_hash,
                candidate_id="candidate",
                candidate_artifact_hash=digest("candidate"),
                candidate_created_at=BASE + timedelta(minutes=1),
                error_analysis_hash=digest("error-analysis"),
                change_summary="Correct observed errors without changing hard risk",
                training_population=training,
                validation_population=validation,
                validation_opened_at=BASE + timedelta(minutes=1),
            )

    def test_training_and_validation_observations_must_be_disjoint(self):
        p, snap, decision = paused_decision()
        training = population(
            "train",
            causal_cut=snap.source_cut_hash,
            available_at=BASE,
            observations=("shared", "train-only"),
        )
        validation = population(
            "validation",
            causal_cut=digest("validation-cut"),
            available_at=BASE,
            observations=("shared", "validation-only"),
        )
        with self.assertRaisesRegex(ValueError, "disjoint"):
            CandidateWave.from_pause(
                pause=decision,
                policy=p,
                champion_artifact_hash=snap.champion_artifact_hash,
                candidate_id="candidate",
                candidate_artifact_hash=digest("candidate"),
                candidate_created_at=BASE + timedelta(minutes=1),
                error_analysis_hash=digest("error-analysis"),
                change_summary="Correct observed errors without changing hard risk",
                training_population=training,
                validation_population=validation,
                validation_opened_at=BASE + timedelta(minutes=1),
            )

    def test_validation_must_use_distinct_causal_cut(self):
        p, snap, decision = paused_decision()
        training = population(
            "train",
            causal_cut=snap.source_cut_hash,
            available_at=BASE,
            observations=("train",),
        )
        validation = population(
            "validation",
            causal_cut=snap.source_cut_hash,
            available_at=BASE,
            observations=("validation",),
        )
        with self.assertRaisesRegex(ValueError, "independently identified causal cut"):
            CandidateWave.from_pause(
                pause=decision,
                policy=p,
                champion_artifact_hash=snap.champion_artifact_hash,
                candidate_id="candidate",
                candidate_artifact_hash=digest("candidate"),
                candidate_created_at=BASE + timedelta(minutes=1),
                error_analysis_hash=digest("error-analysis"),
                change_summary="Correct observed errors without changing hard risk",
                training_population=training,
                validation_population=validation,
                validation_opened_at=BASE + timedelta(minutes=1),
            )

    def test_candidate_cannot_use_training_data_that_arrived_after_creation(self):
        p, snap, decision = paused_decision()
        training = population(
            "train",
            causal_cut=snap.source_cut_hash,
            available_at=BASE + timedelta(minutes=20),
            observations=("train",),
        )
        validation = population(
            "validation",
            causal_cut=digest("validation-cut"),
            available_at=BASE + timedelta(minutes=30),
            observations=("validation",),
        )
        with self.assertRaisesRegex(ValueError, "after candidate creation"):
            CandidateWave.from_pause(
                pause=decision,
                policy=p,
                champion_artifact_hash=snap.champion_artifact_hash,
                candidate_id="candidate",
                candidate_artifact_hash=digest("candidate"),
                candidate_created_at=BASE + timedelta(minutes=15),
                error_analysis_hash=digest("error-analysis"),
                change_summary="Correct observed errors without changing hard risk",
                training_population=training,
                validation_population=validation,
                validation_opened_at=BASE + timedelta(minutes=16),
            )

    def test_validation_cannot_open_before_candidate_is_fixed(self):
        p, snap, decision = paused_decision()
        training = population(
            "train",
            causal_cut=snap.source_cut_hash,
            available_at=BASE,
            observations=("train",),
        )
        validation = population(
            "validation",
            causal_cut=digest("validation-cut"),
            available_at=BASE,
            observations=("validation",),
        )
        with self.assertRaisesRegex(ValueError, "before candidate creation"):
            CandidateWave.from_pause(
                pause=decision,
                policy=p,
                champion_artifact_hash=snap.champion_artifact_hash,
                candidate_id="candidate",
                candidate_artifact_hash=digest("candidate"),
                candidate_created_at=BASE + timedelta(minutes=15),
                error_analysis_hash=digest("error-analysis"),
                change_summary="Correct observed errors without changing hard risk",
                training_population=training,
                validation_population=validation,
                validation_opened_at=BASE + timedelta(minutes=14),
            )

    def test_validation_population_cannot_open_before_its_evidence_is_available(self):
        p, snap, decision = paused_decision()
        training = population(
            "train",
            causal_cut=snap.source_cut_hash,
            available_at=BASE,
            observations=("train",),
        )
        validation = population(
            "validation",
            causal_cut=digest("validation-cut"),
            available_at=BASE + timedelta(minutes=30),
            observations=("validation",),
        )
        with self.assertRaisesRegex(ValueError, "before it is available"):
            CandidateWave.from_pause(
                pause=decision,
                policy=p,
                champion_artifact_hash=snap.champion_artifact_hash,
                candidate_id="candidate",
                candidate_artifact_hash=digest("candidate"),
                candidate_created_at=BASE + timedelta(minutes=15),
                error_analysis_hash=digest("error-analysis"),
                change_summary="Correct observed errors without changing hard risk",
                training_population=training,
                validation_population=validation,
                validation_opened_at=BASE + timedelta(minutes=20),
            )

    def test_candidate_artifact_must_differ_from_champion(self):
        p, snap, decision = paused_decision()
        training = population(
            "train",
            causal_cut=snap.source_cut_hash,
            available_at=BASE,
            observations=("train",),
        )
        validation = population(
            "validation",
            causal_cut=digest("validation-cut"),
            available_at=BASE,
            observations=("validation",),
        )
        with self.assertRaisesRegex(ValueError, "differ from current champion"):
            CandidateWave.from_pause(
                pause=decision,
                policy=p,
                champion_artifact_hash=snap.champion_artifact_hash,
                candidate_id="candidate",
                candidate_artifact_hash=snap.champion_artifact_hash,
                candidate_created_at=BASE + timedelta(minutes=1),
                error_analysis_hash=digest("error-analysis"),
                change_summary="Correct observed errors without changing hard risk",
                training_population=training,
                validation_population=validation,
                validation_opened_at=BASE + timedelta(minutes=2),
            )

    def test_population_identity_is_immutable_unique_tuple(self):
        with self.assertRaisesRegex(TypeError, "immutable tuple"):
            EvidencePopulation(
                population_id="bad",
                root_hash=digest("bad"),
                causal_cut_hash=digest("cut"),
                available_at=BASE,
                observation_ids=["obs-1"],
            )
        with self.assertRaisesRegex(ValueError, "unique"):
            population(
                "duplicate",
                causal_cut=digest("cut"),
                available_at=BASE,
                observations=("obs-1", "obs-1"),
            )


class CandidateResolutionTests(unittest.TestCase):
    def test_pass_under_auto_policy_is_only_eligible_for_external_promotion(self):
        result = resolve_candidate(
            candidate_wave(promotion_mode="AUTO"),
            evaluation(),
        )
        self.assertEqual(result.action, "ELIGIBLE_AUTO_PROMOTION")
        self.assertFalse(result.grants_trading_authority)

    def test_pass_under_confirmation_policy_waits_for_confirmation(self):
        result = resolve_candidate(candidate_wave(), evaluation())
        self.assertEqual(result.action, "AWAITING_CONFIRMATION")
        self.assertFalse(result.grants_trading_authority)

    def test_failed_evaluation_rejects_candidate(self):
        result = resolve_candidate(
            candidate_wave(),
            evaluation(status="FAIL"),
        )
        self.assertEqual(result.action, "REJECTED")

    def test_inconclusive_evaluation_preserves_champion_and_continues_validation(self):
        result = resolve_candidate(
            candidate_wave(),
            evaluation(status="INCONCLUSIVE"),
        )
        self.assertEqual(result.action, "CONTINUE_VALIDATION")

    def test_pass_claim_cannot_bypass_science_gate(self):
        result = resolve_candidate(
            candidate_wave(promotion_mode="AUTO"),
            evaluation(science=False),
        )
        self.assertEqual(result.action, "REJECTED")
        self.assertIn("LEARNING_WAVE.SCIENCE_GATE_FAILED", result.reasons)

    def test_pass_claim_cannot_bypass_retention_gate(self):
        result = resolve_candidate(
            candidate_wave(promotion_mode="AUTO"),
            evaluation(retention=False),
        )
        self.assertEqual(result.action, "REJECTED")
        self.assertIn("LEARNING_WAVE.RETENTION_GATE_FAILED", result.reasons)

    def test_pass_claim_cannot_bypass_risk_gate(self):
        result = resolve_candidate(
            candidate_wave(promotion_mode="AUTO"),
            evaluation(risk=False),
        )
        self.assertEqual(result.action, "REJECTED")
        self.assertIn("LEARNING_WAVE.RISK_GATE_FAILED", result.reasons)

    def test_bound_policy_is_revalidated_at_resolution_use(self):
        wave = candidate_wave(promotion_mode="CONFIRMATION")
        object.__setattr__(wave.policy, "promotion_mode", "AUTO")
        with self.assertRaisesRegex(ValueError, "policy changed"):
            resolve_candidate(wave, evaluation())

    def test_evaluation_must_bind_exact_candidate_artifact(self):
        with self.assertRaisesRegex(ValueError, "does not bind"):
            resolve_candidate(
                candidate_wave(),
                evaluation(candidate_hash=digest("different-candidate")),
            )

    def test_resolution_hash_is_deterministic(self):
        wave = candidate_wave(promotion_mode="AUTO")
        first = resolve_candidate(wave, evaluation())
        second = resolve_candidate(wave, evaluation())
        self.assertEqual(first.resolution_hash, second.resolution_hash)


if __name__ == "__main__":
    unittest.main()
