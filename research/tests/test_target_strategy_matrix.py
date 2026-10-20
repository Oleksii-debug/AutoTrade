from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from research.autotrade_research.agents.goal_directed import (
    PROFILE_IDS,
    create_workstream,
    organism_manifest,
)
from research.autotrade_research.strategies.target_matrix import (
    LifecyclePolicy,
    build_strategy_plan,
    default_candidates,
    evaluate_candidate_matrix,
    new_lifecycle,
    pause_reasons,
    read_checkpoint,
    select_training_candidate,
    transition_lifecycle,
    validate_strategy_plan,
    write_checkpoint,
)
from tools.run_target_strategy_lifecycle import _run_candidate


BASE = datetime(2024, 1, 1, tzinfo=timezone.utc)
TRAIN = "sha256:" + "1" * 64
EVAL = "sha256:" + "2" * 64


def streams():
    return tuple(
        create_workstream(
            session_id=profile.lower().replace("_", "-"),
            profile_id=profile,
            symbols=("BTCUSDT",),
            created_at=BASE,
            max_session_loss_fraction="0.05",
            max_position_fraction="0.25",
            paid_model_budget_usd="0",
        )
        for profile in PROFILE_IDS
    )


def candidate_report(candidate_id: str, *, pnl: str = "300", dataset: str = EVAL):
    return {
        "dataset_identity_sha256": dataset.removeprefix("sha256:"),
        "historical_start_utc": BASE.isoformat().replace("+00:00", "Z"),
        "adverse_half_spread_bps": "5",
        "registered_strategy_candidate": {"candidate_id": candidate_id},
        "simulation": {
            "status": "PAUSED",
            "reconciled": True,
            "completed_episodes": 24,
        },
        "comparative_report": {
            "starting_cash_usd": "1000",
            "agent_mark_pnl_usd": pnl,
            "agent_max_mark_drawdown_fraction": "0.02",
            "agent_gross_mark_turnover_proxy_fraction": "1.25",
            "scenario_fee_rate": "0.001",
            "adverse_slippage_bps": "10",
            "missed_fill_metrics": {
                "execution_intents": 8,
                "missed_fills": 0,
                "non_order_decisions": 16,
                "reconciled_fills": 8,
            },
            "causal_regime_analysis": {
                "status": "CAUSAL_TIME_SLICE_NOT_CAUSAL_EFFECT_OR_PROFIT_CLAIM",
                "summary": {
                    "RANGE_QUIET": {
                        "cost_adjusted_mark_pnl_usd": pnl,
                        "episodes": 24,
                        "nonpositive_episode_returns": 10,
                        "positive_episode_returns": 14,
                    },
                },
            },
            "asof_execution_open_utc": (
                BASE + timedelta(days=1)
            ).isoformat().replace("+00:00", "Z"),
        },
    }


class TargetStrategyPlanTests(unittest.TestCase):
    def test_four_controls_emit_distinct_causal_plans(self):
        candidates = default_candidates(proposal_quantity="1")
        prices = ["100", "101", "103", "104"]
        cuts = [BASE + timedelta(hours=index) for index in range(1, 5)]
        plans = {
            item.family: build_strategy_plan(
                item,
                symbol="BTCUSDT",
                interval="1h",
                interval_seconds=3600,
                dataset_identity_sha256=TRAIN,
                signal_prices=prices,
                available_at=cuts,
            )
            for item in candidates
        }
        self.assertEqual(plans["NO_TRADE_CONTROL"]["side_plan"], ["HOLD"] * 4)
        self.assertEqual(
            plans["DETERMINISTIC_RETURN_THRESHOLD"]["side_plan"][-2:],
            ["BUY", "BUY"],
        )
        self.assertEqual(
            plans["DETERMINISTIC_MEAN_REVERSION_THRESHOLD"]["side_plan"][-2:],
            ["SELL", "SELL"],
        )
        self.assertEqual(
            plans["DETERMINISTIC_BREAKOUT_THRESHOLD"]["side_plan"][-1],
            "BUY",
        )
        for plan in plans.values():
            self.assertEqual(plan["model_calls"], 0)
            self.assertFalse(plan["network_orders"])
            self.assertEqual(
                validate_strategy_plan(
                    plan,
                    dataset_identity_sha256=TRAIN,
                    symbol="BTCUSDT",
                    interval="1h",
                    observation_count=4,
                ),
                plan,
            )

    def test_plan_tamper_or_cross_dataset_reuse_fails_closed(self):
        plan = build_strategy_plan(
            default_candidates(proposal_quantity="1")[1],
            symbol="BTCUSDT",
            interval="1h",
            interval_seconds=3600,
            dataset_identity_sha256=TRAIN,
            signal_prices=["100", "101", "103"],
            available_at=[BASE + timedelta(hours=i) for i in range(1, 4)],
        )
        tampered = json.loads(json.dumps(plan))
        tampered["side_plan"][-1] = "SELL"
        with self.assertRaisesRegex(ValueError, "digest"):
            validate_strategy_plan(
                tampered,
                dataset_identity_sha256=TRAIN,
                symbol="BTCUSDT",
                interval="1h",
                observation_count=3,
            )
        with self.assertRaisesRegex(ValueError, "another causal dataset"):
            validate_strategy_plan(
                plan,
                dataset_identity_sha256=EVAL,
                symbol="BTCUSDT",
                interval="1h",
                observation_count=3,
            )

    def test_rehashed_future_informed_plan_fails_causal_reconstruction(self):
        prices = ["100", "101", "103", "102"]
        cuts = [BASE + timedelta(hours=i) for i in range(1, 5)]
        plan = build_strategy_plan(
            default_candidates(proposal_quantity="1")[1],
            symbol="BTCUSDT",
            interval="1h",
            interval_seconds=3600,
            dataset_identity_sha256=TRAIN,
            signal_prices=prices,
            available_at=cuts,
        )
        forged = json.loads(json.dumps(plan))
        forged["side_plan"][0] = "BUY"
        body = {key: value for key, value in forged.items() if key != "plan_sha256"}
        forged["plan_sha256"] = self.digest(body)
        with self.assertRaisesRegex(ValueError, "causal reconstruction"):
            validate_strategy_plan(
                forged,
                dataset_identity_sha256=TRAIN,
                symbol="BTCUSDT",
                interval="1h",
                observation_count=4,
                signal_prices=prices,
                available_at=cuts,
                interval_seconds=3600,
            )

    @staticmethod
    def digest(value):
        from research.autotrade_research.strategies import target_matrix
        return target_matrix._hash(value)


class CandidateMatrixTests(unittest.TestCase):
    def test_after_cost_matrix_is_target_conditioned_and_never_promotes(self):
        workstreams = streams()
        manifest = organism_manifest(workstreams)
        candidates = default_candidates(proposal_quantity="1")
        reports = {
            item.candidate_id: candidate_report(item.candidate_id)
            for item in candidates
        }
        matrix = evaluate_candidate_matrix(
            workstreams=workstreams,
            manifest_sha256=manifest["manifest_sha256"],
            training_dataset_sha256=TRAIN,
            evaluation_dataset_sha256=EVAL,
            candidates=candidates,
            evaluation_reports=reports,
            evaluation_report_sha256={key: self.digest(value)
                                      for key, value in reports.items()},
            evaluation_started_at=BASE,
            evaluation_ended_at=BASE + timedelta(days=1),
            evaluation_window_seconds=86400,
            minimum_evaluation_episodes=24,
            incumbent_candidate_id="no-trade-control",
            selected_candidate_id="return-3-002",
        )
        by_id = {row["candidate"]["candidate_id"]: row for row in matrix["candidates"]}
        momentum = by_id["return-3-002"]
        objectives = {row["profile_id"]: row for row in momentum["objectives"]}
        self.assertTrue(objectives["TARGET_25"]["qualification_passed"])
        self.assertFalse(objectives["TARGET_50"]["qualification_passed"])
        self.assertFalse(objectives["OPPORTUNITY_RESEARCH"]["candidate_allowed"])
        self.assertIn("return-3-002", matrix["qualified_candidate_ids"])
        self.assertEqual(
            momentum["post_experiment_analysis"]["starting_capital"]["amount"],
            "1000",
        )
        self.assertEqual(
            momentum["post_experiment_analysis"]["reconciled_trade_count"]
            ["reconciled_trade_decisions"],
            8,
        )
        self.assertEqual(
            momentum["post_experiment_analysis"]["turnover"]
            ["reference_open_proxy_notional_usd"],
            "1250.00",
        )
        self.assertEqual(
            momentum["post_experiment_analysis"]["modeled_cost_attribution"]
            ["fees"]["status"],
            "UNAVAILABLE_EVIDENCE_GAP",
        )
        self.assertIsNone(
            momentum["post_experiment_analysis"]["gross_pnl_before_modeled_costs"]
            ["amount_usd"]
        )
        self.assertEqual(
            momentum["post_experiment_analysis"]["causal_regime_summary"]
            ["regimes"]["RANGE_QUIET"]["episodes"],
            24,
        )
        self.assertEqual(
            momentum["post_experiment_analysis"]["versus_no_trade"]
            ["candidate_minus_no_trade_net_pnl_after_costs_usd"],
            "0",
        )
        self.assertFalse(matrix["automatic_champion_promotion"])
        self.assertFalse(matrix["promotion_authority_invoked"])
        self.assertFalse(matrix["network_orders"])

    def test_absent_cost_detail_is_explicit_and_malformed_counts_fail_closed(self):
        candidates = default_candidates(proposal_quantity="1")
        reports = {
            item.candidate_id: candidate_report(item.candidate_id)
            for item in candidates
        }
        for report in reports.values():
            comparative = report["comparative_report"]
            comparative.pop("missed_fill_metrics")
            comparative.pop("agent_gross_mark_turnover_proxy_fraction")
            comparative.pop("causal_regime_analysis")
        common = dict(
            workstreams=streams(),
            manifest_sha256=organism_manifest(streams())["manifest_sha256"],
            training_dataset_sha256=TRAIN,
            evaluation_dataset_sha256=EVAL,
            candidates=candidates,
            evaluation_reports=reports,
            evaluation_report_sha256={key: self.digest(value)
                                      for key, value in reports.items()},
            evaluation_started_at=BASE,
            evaluation_ended_at=BASE + timedelta(days=1),
            evaluation_window_seconds=86400,
            minimum_evaluation_episodes=24,
            incumbent_candidate_id="no-trade-control",
            selected_candidate_id="return-3-002",
        )
        matrix = evaluate_candidate_matrix(**common)
        analysis = matrix["candidates"][0]["post_experiment_analysis"]
        self.assertEqual(
            analysis["reconciled_trade_count"]["status"],
            "UNAVAILABLE_EVIDENCE_GAP",
        )
        self.assertEqual(
            analysis["turnover"]["status"], "UNAVAILABLE_EVIDENCE_GAP"
        )
        self.assertEqual(
            analysis["causal_regime_summary"]["status"],
            "UNAVAILABLE_EVIDENCE_GAP",
        )

        broken = candidate_report("no-trade-control")
        broken["comparative_report"]["missed_fill_metrics"]["reconciled_fills"] = 7
        reports["no-trade-control"] = broken
        common["evaluation_reports"] = reports
        common["evaluation_report_sha256"] = {
            key: self.digest(value) for key, value in reports.items()
        }
        with self.assertRaisesRegex(ValueError, "do not reconcile"):
            evaluate_candidate_matrix(**common)

    def test_same_train_eval_cut_and_report_tamper_are_rejected(self):
        candidates = default_candidates(proposal_quantity="1")
        reports = {item.candidate_id: candidate_report(item.candidate_id)
                   for item in candidates}
        common = dict(
            workstreams=streams(), manifest_sha256=organism_manifest(streams())["manifest_sha256"],
            candidates=candidates, evaluation_reports=reports,
            evaluation_report_sha256={key: self.digest(value)
                                      for key, value in reports.items()},
            evaluation_started_at=BASE,
            evaluation_ended_at=BASE + timedelta(days=1),
            evaluation_window_seconds=86400,
            minimum_evaluation_episodes=24,
            incumbent_candidate_id="no-trade-control",
            selected_candidate_id="return-3-002",
        )
        with self.assertRaisesRegex(ValueError, "must differ"):
            evaluate_candidate_matrix(
                training_dataset_sha256=EVAL,
                evaluation_dataset_sha256=EVAL,
                **common,
            )
        common["evaluation_report_sha256"] = dict(common["evaluation_report_sha256"])
        common["evaluation_report_sha256"]["return-3-002"] = "sha256:" + "9" * 64
        with self.assertRaisesRegex(ValueError, "digest differs"):
            evaluate_candidate_matrix(
                training_dataset_sha256=TRAIN,
                evaluation_dataset_sha256=EVAL,
                **common,
            )

    def test_monthly_measurement_cannot_satisfy_one_day_target(self):
        candidates = default_candidates(proposal_quantity="1")
        reports = {item.candidate_id: candidate_report(item.candidate_id)
                   for item in candidates}
        for report in reports.values():
            report["comparative_report"]["asof_execution_open_utc"] = (
                BASE + timedelta(days=30)
            ).isoformat().replace("+00:00", "Z")
        matrix = evaluate_candidate_matrix(
            workstreams=streams(),
            manifest_sha256=organism_manifest(streams())["manifest_sha256"],
            training_dataset_sha256=TRAIN,
            evaluation_dataset_sha256=EVAL,
            candidates=candidates,
            evaluation_reports=reports,
            evaluation_report_sha256={key: self.digest(value)
                                      for key, value in reports.items()},
            evaluation_started_at=BASE,
            evaluation_ended_at=BASE + timedelta(days=30),
            evaluation_window_seconds=86400,
            minimum_evaluation_episodes=24,
            incumbent_candidate_id="no-trade-control",
            selected_candidate_id="return-3-002",
        )
        momentum = next(
            row for row in matrix["candidates"]
            if row["candidate"]["candidate_id"] == "return-3-002"
        )
        target = next(
            row for row in momentum["objectives"]
            if row["profile_id"] == "TARGET_25"
        )
        self.assertEqual(target["objective"]["status"], "INCONCLUSIVE")
        self.assertFalse(target["horizon_exact_match"])
        self.assertFalse(target["qualification_passed"])

    def test_training_selection_is_frozen_before_evaluation(self):
        candidates = default_candidates(proposal_quantity="1")
        training = {
            item.candidate_id: candidate_report(
                item.candidate_id,
                dataset=TRAIN,
                pnl={
                    "no-trade-control": "0",
                    "return-3-002": "10",
                    "mean-reversion-3-002": "25",
                    "breakout-4-002": "20",
                }[item.candidate_id],
            )
            for item in candidates
        }
        selection = select_training_candidate(
            candidates=candidates,
            training_reports=training,
            training_dataset_sha256=TRAIN,
            incumbent_candidate_id="no-trade-control",
            max_drawdown_fraction="0.05",
        )
        self.assertEqual(selection["selected_candidate_id"], "mean-reversion-3-002")
        self.assertFalse(selection["independent_evaluation_opened"])
        self.assertFalse(selection["automatic_champion_promotion"])

    @staticmethod
    def digest(value):
        from research.autotrade_research.strategies import target_matrix
        return target_matrix._hash(value)


class LifecycleTests(unittest.TestCase):
    def test_existing_learning_wave_policy_drives_bounded_pause(self):
        policy = LifecyclePolicy(24, 86400, Decimal("0.05"))
        reasons = pause_reasons(
            completed_episodes=24,
            market_seconds=86400,
            drawdown_fraction="0.05",
            policy=policy,
            failed=False,
        )
        self.assertEqual(
            reasons,
            (
                "LEARNING_WAVE.TIME_THRESHOLD",
                "LEARNING_WAVE.TRADE_THRESHOLD",
                "LEARNING_WAVE.DRAWDOWN_THRESHOLD",
            ),
        )
        self.assertEqual(policy.learning_wave_policy().promotion_mode, "CONFIRMATION")

    def test_durable_phase_machine_retains_incumbent_without_auto_promotion(self):
        policy = LifecyclePolicy(24, 86400, Decimal("0.05"))
        checkpoint = new_lifecycle(
            input_sha256=TRAIN,
            incumbent_candidate_id="no-trade-control",
            policy=policy,
            observed_at=BASE,
        )
        transitions = (
            ("AUTO_PAUSE_ON_WINDOW_OR_FAILURE", ("LEARNING_WAVE.TIME_THRESHOLD",), ()),
            ("ROOT_CAUSE_ANALYSIS", (), ()),
            ("TRAIN_CANDIDATES", (), ()),
            ("INDEPENDENT_EVALUATION", (), ()),
            ("RESUME_ONLY_IF_QUALIFIED", (), ("return-3-002",)),
        )
        for index, (phase, reasons, qualified) in enumerate(transitions, 1):
            checkpoint = transition_lifecycle(
                checkpoint,
                phase=phase,
                evidence_sha256="sha256:" + str(index) * 64,
                observed_at=BASE + timedelta(minutes=index),
                pause_reasons=reasons,
                qualified_candidate_ids=qualified,
            )
        self.assertEqual(checkpoint["phase"], "RESUME_ONLY_IF_QUALIFIED")
        self.assertEqual(checkpoint["resume_candidate_id"], "no-trade-control")
        self.assertFalse(checkpoint["automatic_champion_promotion"])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "checkpoint.json"
            write_checkpoint(path, checkpoint)
            self.assertEqual(read_checkpoint(path, input_sha256=TRAIN), checkpoint)
            value = json.loads(path.read_text(encoding="utf-8"))
            value["resume_candidate_id"] = "return-3-002"
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "digest differs"):
                read_checkpoint(path, input_sha256=TRAIN)

    def test_cached_candidate_report_must_bind_plan_dataset_and_episode_cut(self):
        candidate = default_candidates(proposal_quantity="1")[1]
        plan = {
            "candidate": candidate.document(),
            "plan_sha256": "sha256:" + "a" * 64,
        }
        report = candidate_report(candidate.candidate_id)
        report.update({
            "registered_strategy_plan_sha256": plan["plan_sha256"],
            "registered_strategy_candidate": candidate.document(),
        })
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            path = workspace / "reports" / "evaluation" / (
                candidate.candidate_id + ".json"
            )
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(report), encoding="utf-8")
            with patch(
                "tools.run_target_strategy_lifecycle.run_historical_zero",
                side_effect=AssertionError("valid cache must not rerun"),
            ):
                cached = _run_candidate(
                    data_root=workspace,
                    artifact_store=workspace,
                    workspace=workspace,
                    symbol="BTCUSDT",
                    interval="1h",
                    from_month="2024-02",
                    through_month="2024-02",
                    candidate_id=candidate.candidate_id,
                    plan_path=workspace / "unused-plan.json",
                    episode_limit=24,
                    split="evaluation",
                    expected_plan=plan,
                    expected_dataset_sha256=EVAL.removeprefix("sha256:"),
                )
            self.assertEqual(cached, report)
            report["registered_strategy_plan_sha256"] = "sha256:" + "b" * 64
            path.write_text(json.dumps(report), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "does not bind"):
                _run_candidate(
                    data_root=workspace,
                    artifact_store=workspace,
                    workspace=workspace,
                    symbol="BTCUSDT",
                    interval="1h",
                    from_month="2024-02",
                    through_month="2024-02",
                    candidate_id=candidate.candidate_id,
                    plan_path=workspace / "unused-plan.json",
                    episode_limit=24,
                    split="evaluation",
                    expected_plan=plan,
                    expected_dataset_sha256=EVAL.removeprefix("sha256:"),
                )


if __name__ == "__main__":
    unittest.main()
