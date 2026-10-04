from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
import json
from tempfile import TemporaryDirectory
import unittest

from research.autotrade_research.artifacts.store import ArtifactStore
from research.autotrade_research.learning.champion import CandidateApproval
from research.autotrade_research.learning.waves import (
    CandidateWave,
    EvidencePopulation,
    LearningWavePolicy,
    MarketWaveSnapshot,
    evaluate_pause,
    publish_wave_resolution,
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
        error_analysis_at=BASE + timedelta(minutes=12),
        change_summary="Correct observed errors without changing hard risk",
        training_population=training,
        validation_population=validation,
        validation_opened_at=BASE + timedelta(minutes=20),
    )


def approval(
    *,
    status="PASS",
    retention=True,
    risk=True,
    candidate_id="candidate-0001",
    artifact_hash=None,
    valid_until=None,
) -> CandidateApproval:
    return CandidateApproval.create(
        candidate_id=candidate_id,
        artifact_hash=artifact_hash or digest("candidate"),
        evidence_id="evidence:candidate-0001",
        evidence_valid_until=valid_until or (BASE + timedelta(days=1)),
        evaluation_status=status,
        retention_passed=retention,
        risk_passed=risk,
        authority_scope_id="paper-scope",
        protocol_id="protocol-v1",
        protocol_hash=digest("protocol"),
        evaluation_id="evaluation-v1",
        evaluation_result_hash=digest(
            f"evaluation:{status}:{retention}:{risk}:{candidate_id}"
        ),
    )


RESOLUTION_TIME = BASE + timedelta(minutes=30)


def resolve(wave, approval_value=None, *, at=RESOLUTION_TIME):
    return resolve_candidate(
        wave,
        approval_value or approval(),
        resolved_at=at,
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
                candidate_created_at=BASE + timedelta(minutes=15),
                error_analysis_hash=digest("error-analysis"),
                error_analysis_at=BASE + timedelta(minutes=12),
                change_summary="Correct observed errors without changing hard risk",
                training_population=train,
                validation_population=validation,
                validation_opened_at=BASE + timedelta(minutes=16),
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
                candidate_created_at=BASE + timedelta(minutes=15),
                error_analysis_hash=digest("error-analysis"),
                error_analysis_at=BASE + timedelta(minutes=12),
                change_summary="Correct observed errors without changing hard risk",
                training_population=training,
                validation_population=validation,
                validation_opened_at=BASE + timedelta(minutes=16),
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
                candidate_created_at=BASE + timedelta(minutes=15),
                error_analysis_hash=digest("error-analysis"),
                error_analysis_at=BASE + timedelta(minutes=12),
                change_summary="Correct observed errors without changing hard risk",
                training_population=training,
                validation_population=validation,
                validation_opened_at=BASE + timedelta(minutes=16),
            )

    def test_candidate_cannot_be_created_before_pause(self):
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
        with self.assertRaisesRegex(ValueError, "before the learning-wave pause"):
            CandidateWave.from_pause(
                pause=decision,
                policy=p,
                champion_artifact_hash=snap.champion_artifact_hash,
                candidate_id="candidate",
                candidate_artifact_hash=digest("candidate"),
                candidate_created_at=BASE + timedelta(minutes=9),
                error_analysis_hash=digest("error-analysis"),
                error_analysis_at=BASE + timedelta(minutes=9),
                change_summary="Premature candidate",
                training_population=training,
                validation_population=validation,
                validation_opened_at=BASE + timedelta(minutes=16),
            )

    def test_error_analysis_cannot_precede_pause(self):
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
        with self.assertRaisesRegex(ValueError, "error analysis cannot precede"):
            CandidateWave.from_pause(
                pause=decision,
                policy=p,
                champion_artifact_hash=snap.champion_artifact_hash,
                candidate_id="candidate",
                candidate_artifact_hash=digest("candidate"),
                candidate_created_at=BASE + timedelta(minutes=15),
                error_analysis_hash=digest("error-analysis"),
                error_analysis_at=BASE + timedelta(minutes=9),
                change_summary="Analysis happened too early",
                training_population=training,
                validation_population=validation,
                validation_opened_at=BASE + timedelta(minutes=16),
            )

    def test_candidate_cannot_precede_error_analysis_completion(self):
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
        with self.assertRaisesRegex(ValueError, "before error analysis completes"):
            CandidateWave.from_pause(
                pause=decision,
                policy=p,
                champion_artifact_hash=snap.champion_artifact_hash,
                candidate_id="candidate",
                candidate_artifact_hash=digest("candidate"),
                candidate_created_at=BASE + timedelta(minutes=15),
                error_analysis_hash=digest("error-analysis"),
                error_analysis_at=BASE + timedelta(minutes=16),
                change_summary="Candidate fixed too early",
                training_population=training,
                validation_population=validation,
                validation_opened_at=BASE + timedelta(minutes=17),
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
                candidate_created_at=BASE + timedelta(minutes=15),
                error_analysis_hash=digest("error-analysis"),
                error_analysis_at=BASE + timedelta(minutes=12),
                change_summary="Correct observed errors without changing hard risk",
                training_population=training,
                validation_population=validation,
                validation_opened_at=BASE + timedelta(minutes=16),
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
                candidate_created_at=BASE + timedelta(minutes=15),
                error_analysis_hash=digest("error-analysis"),
                error_analysis_at=BASE + timedelta(minutes=12),
                change_summary="Correct observed errors without changing hard risk",
                training_population=training,
                validation_population=validation,
                validation_opened_at=BASE + timedelta(minutes=16),
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
                candidate_created_at=BASE + timedelta(minutes=15),
                error_analysis_hash=digest("error-analysis"),
                error_analysis_at=BASE + timedelta(minutes=12),
                change_summary="Correct observed errors without changing hard risk",
                training_population=training,
                validation_population=validation,
                validation_opened_at=BASE + timedelta(minutes=16),
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
                error_analysis_at=BASE + timedelta(minutes=12),
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
                error_analysis_at=BASE + timedelta(minutes=12),
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
                error_analysis_at=BASE + timedelta(minutes=12),
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
                candidate_created_at=BASE + timedelta(minutes=15),
                error_analysis_hash=digest("error-analysis"),
                error_analysis_at=BASE + timedelta(minutes=12),
                change_summary="Correct observed errors without changing hard risk",
                training_population=training,
                validation_population=validation,
                validation_opened_at=BASE + timedelta(minutes=16),
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
    def test_pass_under_auto_policy_hands_off_to_canonical_promotion_authority(self):
        result = resolve(candidate_wave(promotion_mode="AUTO"))
        self.assertEqual(result.action, "HANDOFF_TO_AUTO_PROMOTION_CHECK")
        self.assertFalse(result.grants_trading_authority)

    def test_pass_under_confirmation_policy_waits_for_confirmation(self):
        result = resolve(candidate_wave())
        self.assertEqual(result.action, "AWAITING_CONFIRMATION")
        self.assertFalse(result.grants_trading_authority)

    def test_failed_canonical_approval_rejects_candidate(self):
        result = resolve(
            candidate_wave(),
            approval(status="FAIL"),
        )
        self.assertEqual(result.action, "REJECTED")
        self.assertIn("LEARNING_WAVE.EVALUATION_FAILED", result.reasons)

    def test_inconclusive_approval_preserves_champion_and_continues_validation(self):
        result = resolve(
            candidate_wave(),
            approval(status="INCONCLUSIVE"),
        )
        self.assertEqual(result.action, "CONTINUE_VALIDATION")

    def test_pass_claim_cannot_bypass_retention_gate(self):
        result = resolve(
            candidate_wave(promotion_mode="AUTO"),
            approval(retention=False),
        )
        self.assertEqual(result.action, "REJECTED")
        self.assertIn("LEARNING_WAVE.RETENTION_GATE_FAILED", result.reasons)

    def test_pass_claim_cannot_bypass_risk_gate(self):
        result = resolve(
            candidate_wave(promotion_mode="AUTO"),
            approval(risk=False),
        )
        self.assertEqual(result.action, "REJECTED")
        self.assertIn("LEARNING_WAVE.RISK_GATE_FAILED", result.reasons)

    def test_bound_policy_is_revalidated_at_resolution_use(self):
        wave = candidate_wave(promotion_mode="CONFIRMATION")
        object.__setattr__(wave.policy, "promotion_mode", "AUTO")
        with self.assertRaisesRegex(ValueError, "policy changed"):
            resolve(wave)

    def test_inconclusive_status_cannot_hide_failed_hard_gate(self):
        result = resolve(
            candidate_wave(promotion_mode="AUTO"),
            approval(status="INCONCLUSIVE", risk=False),
        )
        self.assertEqual(result.action, "REJECTED")
        self.assertIn("LEARNING_WAVE.RISK_GATE_FAILED", result.reasons)
        self.assertNotIn("LEARNING_WAVE.EVALUATION_INCONCLUSIVE", result.reasons)

    def test_approval_must_bind_exact_candidate_identity(self):
        with self.assertRaisesRegex(ValueError, "candidate identity"):
            resolve(
                candidate_wave(),
                approval(candidate_id="candidate-different"),
            )

    def test_approval_must_bind_exact_candidate_artifact(self):
        with self.assertRaisesRegex(ValueError, "candidate artifact"):
            resolve(
                candidate_wave(),
                approval(artifact_hash=digest("different-candidate")),
            )

    def test_expired_approval_is_rejected_before_handoff(self):
        result = resolve(
            candidate_wave(promotion_mode="AUTO"),
            approval(valid_until=RESOLUTION_TIME),
        )
        self.assertEqual(result.action, "REJECTED")
        self.assertIn("LEARNING_WAVE.APPROVAL_EVIDENCE_EXPIRED", result.reasons)

    def test_resolution_hash_is_deterministic(self):
        wave = candidate_wave(promotion_mode="AUTO")
        first = resolve(wave)
        second = resolve(wave)
        self.assertEqual(first.resolution_hash, second.resolution_hash)


class LearningWavePersistenceTests(unittest.TestCase):
    def test_resolution_is_retained_as_immutable_canonical_artifact(self):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            wave = candidate_wave(promotion_mode="CONFIRMATION")
            approval_value = approval()
            resolution, manifest = publish_wave_resolution(
                store,
                wave,
                approval_value,
                resolved_at=RESOLUTION_TIME,
                rights={"storage": True, "export": False},
            )
            payload = json.loads(store.read_bytes(manifest["artifact_id"]).decode("utf-8"))

            self.assertEqual(payload["artifact_kind"], "LEARNING_WAVE_RESOLUTION")
            self.assertEqual(payload["wave_id"], wave.wave_id)
            self.assertEqual(payload["policy"]["policy_hash"], wave.policy_hash)
            self.assertEqual(payload["policy"]["max_trades"], 20)
            self.assertEqual(
                payload["pause_reasons"],
                ["LEARNING_WAVE.TRADE_THRESHOLD"],
            )
            self.assertEqual(payload["resolved_at"], RESOLUTION_TIME.isoformat())
            self.assertEqual(payload["candidate_artifact_hash"], wave.candidate_artifact_hash)
            self.assertEqual(
                payload["resolution"]["resolution_hash"],
                resolution.resolution_hash,
            )
            self.assertEqual(
                payload["resolution"]["action"],
                "AWAITING_CONFIRMATION",
            )
            self.assertFalse(payload["resolution"]["grants_trading_authority"])
            self.assertEqual(payload["training_population"]["observation_count"], 3)
            self.assertEqual(payload["validation_population"]["observation_count"], 2)
            self.assertEqual(
                manifest["metadata"]["artifact_kind"],
                "LEARNING_WAVE_RESOLUTION",
            )
            self.assertFalse(manifest["metadata"]["grants_trading_authority"])

    def test_same_resolution_publication_is_idempotent(self):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            wave = candidate_wave(promotion_mode="AUTO")
            approval_value = approval()
            first_resolution, first_manifest = publish_wave_resolution(
                store,
                wave,
                approval_value,
                resolved_at=RESOLUTION_TIME,
                rights={"storage": True, "export": False},
            )
            second_resolution, second_manifest = publish_wave_resolution(
                store,
                wave,
                approval_value,
                resolved_at=RESOLUTION_TIME,
                rights={"storage": True, "export": False},
            )
            self.assertEqual(first_resolution, second_resolution)
            self.assertEqual(first_manifest["artifact_id"], second_manifest["artifact_id"])
            self.assertEqual(first_manifest["sha256"], second_manifest["sha256"])

    def test_rejected_candidate_retains_failure_reason_separately(self):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            wave = candidate_wave(promotion_mode="AUTO")
            passing_resolution, passing_manifest = publish_wave_resolution(
                store,
                wave,
                approval(),
                resolved_at=RESOLUTION_TIME,
                rights={"storage": True, "export": False},
            )
            rejected_resolution, rejected_manifest = publish_wave_resolution(
                store,
                wave,
                approval(risk=False),
                resolved_at=RESOLUTION_TIME,
                rights={"storage": True, "export": False},
            )
            rejected = json.loads(
                store.read_bytes(rejected_manifest["artifact_id"]).decode("utf-8")
            )
            self.assertNotEqual(
                passing_resolution.resolution_hash,
                rejected_resolution.resolution_hash,
            )
            self.assertNotEqual(
                passing_manifest["artifact_id"],
                rejected_manifest["artifact_id"],
            )
            self.assertIn(
                "LEARNING_WAVE.RISK_GATE_FAILED",
                rejected["resolution"]["reasons"],
            )
            self.assertEqual(rejected["resolution"]["action"], "REJECTED")


if __name__ == "__main__":
    unittest.main()
