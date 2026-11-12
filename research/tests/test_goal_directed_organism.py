from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest

from research.autotrade_research.agents.goal_directed import (
    PROFILE_IDS,
    AttentionObservation,
    allocate_attention,
    create_workstream,
    decide_attention,
    evaluate_objective,
    organism_manifest,
    profile_for,
    workstream_from_document,
)
from tools.create_goal_directed_organism import build_manifest
from tools.run_goal_directed_attention import _triangular_shadow_summary, run_cycle


NOW = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)


def spec(profile_id: str, *, paid: str = "0"):
    return create_workstream(
        session_id=profile_id.lower().replace("_", "-"),
        profile_id=profile_id,
        symbols=("BTC_USDT",),
        created_at=NOW,
        paid_model_budget_usd=paid,
    )


def observation(*, price: str = "100", scheduled_delta: int = 3600,
                age_seconds: int = 0, manual: bool = False):
    return AttentionObservation(
        available_at=NOW - timedelta(seconds=age_seconds),
        reference_price=Decimal("100"),
        current_price=Decimal(price),
        next_scheduled_review=NOW + timedelta(seconds=scheduled_delta),
        take_profit_fraction=Decimal("0.10"),
        stop_loss_fraction=Decimal("0.05"),
        manual_wake=manual,
    )


class GoalDirectedOrganismTests(unittest.TestCase):
    def test_six_profiles_have_exact_targets_and_no_authority(self):
        profiles = {name: profile_for(name) for name in PROFILE_IDS}
        self.assertEqual(profiles["TARGET_25"].target_return_fraction, Decimal("0.25"))
        self.assertEqual(profiles["TARGET_50"].target_return_fraction, Decimal("0.50"))
        self.assertEqual(profiles["TARGET_100"].target_return_fraction, Decimal("1.00"))
        manifest = organism_manifest(tuple(spec(name) for name in PROFILE_IDS))
        self.assertFalse(manifest["network_orders"])
        self.assertFalse(manifest["trading_authority"])

    def test_target_pressure_cannot_expand_hard_risk(self):
        with self.assertRaisesRegex(ValueError, "10%"):
            create_workstream(
                session_id="target-100",
                profile_id="TARGET_100",
                symbols=("BTC_USDT",),
                created_at=NOW,
                max_session_loss_fraction="0.11",
            )

    def test_memories_are_isolated_but_authorities_shared(self):
        first = spec("TARGET_25")
        second = spec("TARGET_50")
        manifest = organism_manifest((first, second))
        self.assertNotEqual(first.memory_namespace, second.memory_namespace)
        self.assertEqual(
            manifest["architecture"],
            "ONE_ORGANISM_SHARED_AUTHORITIES_ISOLATED_WORKSTREAM_MEMORY",
        )

    def test_passive_position_uses_zero_model_without_trigger(self):
        decision = decide_attention(
            spec("EVENT_DRIVEN_POSITION"), observation(), now=NOW
        )
        self.assertEqual(decision.phase, "PASSIVE_MONITORING")
        self.assertFalse(decision.deep_analysis_requested)
        self.assertFalse(decision.paid_model_eligible)

    def test_take_profit_and_stop_loss_wake_analysis(self):
        take = decide_attention(spec("EVENT_DRIVEN_POSITION"), observation(price="111"), now=NOW)
        stop = decide_attention(spec("EVENT_DRIVEN_POSITION"), observation(price="94"), now=NOW)
        self.assertEqual(take.reason, "take-profit boundary reached")
        self.assertEqual(stop.reason, "stop-loss boundary reached")
        self.assertGreater(stop.priority, take.priority)

    def test_stale_market_wakes_recovery_without_paid_model(self):
        decision = decide_attention(
            spec("TARGET_100", paid="1"),
            observation(age_seconds=121),
            now=NOW,
        )
        self.assertEqual(decision.phase, "RECOVERY")
        self.assertFalse(decision.paid_model_eligible)

    def test_future_market_observation_is_rejected(self):
        future = AttentionObservation(
            available_at=NOW + timedelta(seconds=1),
            reference_price=Decimal("100"),
            current_price=Decimal("100"),
            next_scheduled_review=NOW + timedelta(hours=1),
            take_profit_fraction=Decimal("0.1"),
            stop_loss_fraction=Decimal("0.05"),
        )
        with self.assertRaisesRegex(ValueError, "future"):
            decide_attention(spec("BASELINE"), future, now=NOW)

    def test_one_attention_budget_prevents_duplicate_paid_thought(self):
        high = decide_attention(
            spec("TARGET_100", paid="1"), observation(price="94"), now=NOW
        )
        lower = decide_attention(
            spec("TARGET_25", paid="1"), observation(price="111"), now=NOW
        )
        grants = allocate_attention((lower, high), max_deep_analyses=1, max_paid_model_calls=1)
        by_id = {item.decision.session_id: item for item in grants}
        self.assertTrue(by_id["target-100"].deep_analysis_granted)
        self.assertTrue(by_id["target-100"].paid_model_granted)
        self.assertFalse(by_id["target-25"].deep_analysis_granted)

    def test_target_is_measured_after_costs_not_assumed(self):
        result = evaluate_objective(
            spec("TARGET_25"),
            initial_equity="1000",
            final_equity_after_costs="1249",
            max_drawdown_fraction="0.04",
            horizon_complete=True,
        )
        self.assertEqual(result["status"], "TARGET_MISSED")
        self.assertEqual(result["net_return_after_costs_fraction"], "0.249")
        self.assertFalse(result["profitability_proven"])

    def test_risk_breach_dominates_a_reached_target(self):
        result = evaluate_objective(
            spec("TARGET_25"),
            initial_equity="1000",
            final_equity_after_costs="1300",
            max_drawdown_fraction="0.051",
            horizon_complete=True,
        )
        self.assertEqual(result["status"], "RISK_LIMIT_BREACHED")

    def test_opportunity_research_is_inert_and_free(self):
        item = spec("OPPORTUNITY_RESEARCH", paid="99")
        self.assertEqual(item.max_position_fraction, Decimal("0.01"))
        self.assertEqual(item.paid_model_budget_usd, Decimal("0"))
        self.assertTrue(item.profile.opportunity_research_only)

    def test_persistent_builder_creates_one_manifest_and_six_memories(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = build_manifest(
                root=root,
                symbols=("BTC_USDT", "ETH_USDT"),
                created_at=NOW,
            )
            self.assertTrue((root / "organism.json").is_file())
            self.assertEqual(len(manifest["workstreams"]), 6)
            for row in manifest["workstreams"]:
                self.assertEqual(row["paid_model_budget_usd"], "0")
                self.assertTrue((root / row["memory_namespace"] / "workstream.json").is_file())

    def test_attention_runner_reuses_one_snapshot_without_model_or_orders(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            organism = root / "organism"
            build_manifest(
                root=organism,
                symbols=("BTC_USDT",),
                created_at=NOW,
            )
            market = root / "latest-market.json"
            market.write_text(json.dumps({
                "schema": "autotrade-whitebit-public-supervisor-v1",
                "market": "BTC_USDT",
                "venue": "WHITEBIT_SPOT",
                "bid_price": "9999",
                "bid_quantity": "1",
                "ask_price": "10001",
                "ask_quantity": "2",
                "source_event_at": (NOW - timedelta(milliseconds=20)).isoformat(),
                "available_at": NOW.isoformat(),
                "published_at": (NOW + timedelta(milliseconds=10)).isoformat(),
                "update_id": 1,
                "stream_generation": 1,
                "raw_evidence_ref": {
                    "artifact_id": "one",
                    "sha256": "sha256:" + "1" * 64,
                    "source_uri": "wss://wss.whitebit.com/ws#bookTicker",
                    "observed_at": NOW.isoformat().replace("+00:00", "Z"),
                    "rights_id": "whitebit-public-websocket-internal-v1",
                },
                "public_data_only": True,
                "trading_authority": False,
            }), encoding="utf-8")
            report = run_cycle(
                organism_root=organism,
                market_snapshot=market,
                now=NOW + timedelta(milliseconds=20),
            )
            self.assertEqual(report["shared_snapshot_reuse_count"], 6)
            self.assertEqual(report["paid_model_calls"], 0)
            self.assertFalse(report["network_orders"])
            self.assertEqual(
                report["organism_evidence"]["learning_fleet"]["status"],
                "NOT_AVAILABLE",
            )

            fleet = root / "fleet.json"
            fleet.write_text(json.dumps({
                "schema": "autotrade-learning-fleet-diagnostics-v1",
                "status": "RUNNING",
                "updated_at": NOW.isoformat(),
                "snapshot_sha256": "sha256:" + "2" * 64,
                "learners": [{
                    "id": "btc",
                    "episode_count": 12,
                    "model_version_count": 2,
                    "network_orders": False,
                    "trading_authority": False,
                    "learning_cycle": {
                        "phase": "EVALUATING",
                        "candidate_version": 1,
                        "champion_promotion": False,
                        "network_orders": False,
                        "trading_authority": False,
                    },
                }],
                "network_orders": False,
                "trading_authority": False,
            }), encoding="utf-8")
            shadow = root / "shadow.json"
            shadow.write_text(json.dumps({
                "schema": "autotrade-whitebit-trend-forward-shadow-v1",
                "status": "RUNNING",
                "updated_at": NOW.isoformat(),
                "market": "SOL_USDT",
                "completed_live_bars": 2,
                "current_equity": "1001.2",
                "net_pnl": "1.2",
                "return_fraction": "0.0012",
                "max_drawdown_fraction": "0.01",
                "quantity": "-12",
                "holding_bars": 2,
                "closed_cycles": 0,
                "last_action": "HOLD_SHORT",
                "qualification_file_sha256": "sha256:" + "3" * 64,
                "network_orders": False,
                "trading_authority": False,
            }), encoding="utf-8")
            rolling = root / "rolling.json"
            rolling.write_text(json.dumps({
                "schema": "autotrade-whitebit-rolling-native-alpha-v1",
                "status": "RUNNING", "updated_at": NOW.isoformat(),
                "cycle_count": 1, "cycles_today": 1,
                "next_run_at": (NOW + timedelta(hours=6)).isoformat(),
                "last_result": {"markets": {"SOL_USDT": {
                    "evaluation": {
                        "dataset_identity_sha256": "4" * 64,
                        "models": {"XGBOOST": {
                            "executed_trade_count": 1, "long_trade_count": 0,
                            "short_trade_count": 1,
                            "net_pnl_after_costs_quote": "-6.8",
                            "research_gate_passed": False,
                            "rejection_reasons": ["NO_POSITIVE_RETURN_AFTER_COSTS"],
                        }},
                    },
                }}},
                "network_orders": False, "trading_authority": False,
                "automatic_promotion": False,
            }), encoding="utf-8")
            opportunity = root / "opportunity.json"
            opportunity.write_text(json.dumps({
                "schema": "autotrade-whitebit-opportunity-supervisor-v1",
                "status": "RUNNING", "updated_at": NOW.isoformat(),
                "cycle_count": 2,
                "next_run_at": (NOW + timedelta(days=1)).isoformat(),
                "last_result": {
                    "selected_market_count": 2,
                    "qualified_market_count": 0,
                    "paid_model_calls": 0,
                    "markets": {
                        "XRP_USDT": {
                            "status": "SCREENED", "rank": 1,
                            "quote_volume_24h": "2000000",
                            "screen": {
                                "prospective_qualification": False,
                                "train_survivor_count": 0,
                                "development_survivor_count": 0,
                                "final_consumed": False,
                                "final_rejection_reasons": ["NO_TRAIN_SURVIVOR"],
                                "network_orders": False,
                                "trading_authority": False,
                            },
                        },
                        "HYPE_USDT": {
                            "status": "REJECTED_DATA", "rank": 2,
                            "quote_volume_24h": "1000000",
                        },
                    },
                    "network_orders": False,
                    "trading_authority": False,
                },
                "network_orders": False, "trading_authority": False,
                "automatic_promotion": False,
            }), encoding="utf-8")
            trade_flow = root / "trade-flow.json"
            trade_flow.write_text(json.dumps({
                "schema": "autotrade-whitebit-trade-flow-supervisor-v1",
                "phase": "RUNNING",
                "checkpointed_at": NOW.isoformat(),
                "minimum_buckets": 2160,
                "paid_model_calls": 0,
                "results": {
                    "BTC_USDT": {
                        "status": "WAITING_FOR_CAUSAL_BUCKETS",
                        "available_causal_buckets": 321,
                        "prospective_qualification": False,
                        "final_consumed": False,
                        "result_sha256": "5" * 64,
                    },
                    "SOL_USDT": {
                        "status": "REJECTED",
                        "available_causal_buckets": 2160,
                        "prospective_qualification": False,
                        "final_consumed": False,
                        "result_sha256": "6" * 64,
                    },
                },
                "network_orders": False,
                "trading_authority": False,
            }), encoding="utf-8")
            maker = root / "maker.json"
            maker.write_text(json.dumps({
                "schema": "autotrade-whitebit-maker-shadow-v1",
                "phase": "RUNNING", "checkpointed_at": NOW.isoformat(),
                "market": "PEPE_USDT", "settled_cycles": 2,
                "positive_cycles": 1, "net_pnl_quote": "-0.15",
                "research_phase": "RESEARCH_FILTERED", "wave_cycles": 1,
                "wave_positive_cycles": 0, "wave_net_pnl_quote": "-0.25",
                "last_quote_admission": {
                    "admitted": False, "reason": "DIRECTIONAL_FLOW",
                    "network_orders": False, "trading_authority": False,
                },
                "open_quote": {
                    "status": "OPEN", "age_seconds": 12,
                    "bid_filled": True, "ask_filled": False,
                    "quote_sha256": "7" * 64,
                    "network_orders": False, "trading_authority": False,
                },
                "last_settled": {
                    "outcome": "BID_ONLY_FORCED_EXIT",
                    "net_pnl_quote": "-0.25", "result_sha256": "8" * 64,
                    "network_orders": False, "trading_authority": False,
                },
                "profit_guaranteed": False, "paid_model_calls": 0,
                "network_orders": False, "trading_authority": False,
            }), encoding="utf-8")
            maker_ena = root / "maker-ena.json"
            maker_ena.write_text(json.dumps({
                "schema": "autotrade-whitebit-maker-shadow-v1",
                "phase": "RUNNING", "checkpointed_at": NOW.isoformat(),
                "market": "ENA_USDT", "settled_cycles": 0,
                "positive_cycles": 0, "net_pnl_quote": "0",
                "research_phase": "DISCOVERY", "wave_cycles": 0,
                "wave_positive_cycles": 0, "wave_net_pnl_quote": "0",
                "open_quote": None, "last_settled": None,
                "profit_guaranteed": False, "paid_model_calls": 0,
                "network_orders": False, "trading_authority": False,
            }), encoding="utf-8")
            triangle = root / "triangle.json"
            triangle.write_text(json.dumps({
                "schema": "autotrade-whitebit-triangular-shadow-supervisor-v1",
                "phase": "RUNNING", "checkpointed_at": NOW.isoformat(),
                "latest_market_count": 9, "cycle_result_count": 8,
                "opportunity_count": 0, "notional": "100",
                "impact_reserve_bps": "2",
                "best_result": {
                    "path": ["USDT", "USDC", "USD", "USDT"],
                    "net_after_taker_fees_bps": "-29.9612337330",
                    "net_after_reserve_bps": "-31.9612337330",
                    "qualified_for_shadow_observation": False,
                    "result_sha256": "9" * 64,
                },
                "profit_guaranteed": False, "paid_model_calls": 0,
                "network_orders": False, "trading_authority": False,
            }), encoding="utf-8")
            enriched = run_cycle(
                organism_root=organism,
                market_snapshot=market,
                now=NOW + timedelta(milliseconds=30),
                learning_fleet_checkpoint=fleet,
                forward_shadow_checkpoint=shadow,
                rolling_alpha_checkpoint=rolling,
                opportunity_checkpoint=opportunity,
                trade_flow_checkpoint=trade_flow,
                maker_shadow_checkpoint=maker,
                ena_maker_shadow_checkpoint=maker_ena,
                triangular_shadow_checkpoint=triangle,
            )
            evidence = enriched["organism_evidence"]
            self.assertEqual(evidence["learning_fleet"]["completed_episodes"], 12)
            self.assertEqual(evidence["learning_fleet"]["phase_counts"], {"EVALUATING": 1})
            self.assertEqual(evidence["forward_shadow"]["net_pnl"], "1.2")
            self.assertEqual(evidence["forward_shadow"]["quantity"], "-12")
            self.assertFalse(evidence["forward_shadow"]["trading_authority"])
            alpha = evidence["rolling_native_alpha"]
            self.assertEqual(alpha["cycle_count"], 1)
            self.assertEqual(alpha["passed_model_count"], 0)
            self.assertEqual(alpha["markets"][0]["models"][0]["short"], 1)
            self.assertEqual(
                alpha["markets"][0]["models"][0]["net_pnl_after_costs_quote"], "-6.8"
            )
            self.assertFalse(alpha["automatic_promotion"])
            opportunity_summary = evidence["opportunity_screen"]
            self.assertEqual(opportunity_summary["selected_market_count"], 2)
            self.assertEqual(opportunity_summary["qualified_market_count"], 0)
            self.assertEqual(opportunity_summary["rejected_data_count"], 1)
            self.assertFalse(opportunity_summary["markets"][1]["prospective_qualification"])
            self.assertFalse(opportunity_summary["automatic_promotion"])
            trade_flow_summary = evidence["trade_flow_screen"]
            self.assertEqual(trade_flow_summary["minimum_buckets"], 2160)
            self.assertEqual(trade_flow_summary["market_count"], 2)
            self.assertEqual(trade_flow_summary["qualified_market_count"], 0)
            self.assertEqual(
                trade_flow_summary["markets"][0]["available_causal_buckets"], 321
            )
            self.assertFalse(trade_flow_summary["automatic_promotion"])
            self.assertFalse(trade_flow_summary["trading_authority"])
            maker_summary = evidence["maker_shadow"]
            self.assertEqual(maker_summary["settled_cycles"], 2)
            self.assertEqual(maker_summary["positive_cycles"], 1)
            self.assertEqual(maker_summary["net_pnl_quote"], "-0.15")
            self.assertEqual(maker_summary["research_phase"], "RESEARCH_FILTERED")
            self.assertEqual(maker_summary["wave_cycles"], 1)
            self.assertEqual(
                maker_summary["last_quote_admission"]["reason"], "DIRECTIONAL_FLOW"
            )
            self.assertTrue(maker_summary["open_quote"]["bid_filled"])
            self.assertEqual(
                maker_summary["last_settled"]["outcome"], "BID_ONLY_FORCED_EXIT"
            )
            self.assertFalse(maker_summary["automatic_promotion"])
            ena_summary = evidence["ena_maker_shadow"]
            self.assertEqual(ena_summary["market"], "ENA_USDT")
            self.assertEqual(ena_summary["research_phase"], "DISCOVERY")
            self.assertEqual(ena_summary["net_pnl_quote"], "0")
            triangle_summary = evidence["stable_fiat_triangular_shadow"]
            self.assertEqual(triangle_summary["latest_market_count"], 9)
            self.assertEqual(triangle_summary["cycle_result_count"], 8)
            self.assertEqual(triangle_summary["opportunity_count"], 0)
            self.assertEqual(
                triangle_summary["best_result"]["path"],
                ["USDT", "USDC", "USD", "USDT"],
            )
            self.assertEqual(
                triangle_summary["best_result"]["net_after_reserve_bps"],
                "-31.9612337330",
            )
            self.assertFalse(triangle_summary["automatic_promotion"])

    def test_triangular_shadow_projection_fails_closed_on_authority_or_malformed_result(self):
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = Path(directory) / "triangle.json"
            base = {
                "schema": "autotrade-whitebit-triangular-shadow-supervisor-v1",
                "phase": "RUNNING", "paid_model_calls": 0,
                "profit_guaranteed": False,
                "network_orders": False, "trading_authority": False,
                "latest_market_count": 9, "cycle_result_count": 8,
                "opportunity_count": 0, "notional": "100",
                "impact_reserve_bps": "2",
                "best_result": None,
            }
            checkpoint.write_text(json.dumps({**base, "trading_authority": True}),
                                  encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "authority or schema"):
                _triangular_shadow_summary(checkpoint)
            checkpoint.write_text(json.dumps({
                **base,
                "best_result": {
                    "path": ["USDT", "USDC"],
                    "net_after_taker_fees_bps": "1",
                    "net_after_reserve_bps": "0.5",
                    "qualified_for_shadow_observation": True,
                    "result_sha256": "a" * 64,
                },
            }), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "best result is malformed"):
                _triangular_shadow_summary(checkpoint)

    def test_attention_runner_rebases_reference_after_wake(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            organism = root / "organism"
            manifest = build_manifest(
                root=organism,
                symbols=("BTC_USDT",),
                created_at=NOW,
            )
            market = root / "latest-market.json"
            snapshot = {
                "schema": "autotrade-whitebit-public-supervisor-v1",
                "market": "BTC_USDT",
                "venue": "WHITEBIT_SPOT",
                "bid_price": "93",
                "bid_quantity": "1",
                "ask_price": "95",
                "ask_quantity": "2",
                "source_event_at": NOW.isoformat(),
                "available_at": NOW.isoformat(),
                "published_at": NOW.isoformat(),
                "update_id": 1,
                "stream_generation": 1,
                "raw_evidence_ref": {
                    "artifact_id": "one",
                    "sha256": "sha256:" + "1" * 64,
                    "source_uri": "wss://wss.whitebit.com/ws#bookTicker",
                    "observed_at": NOW.isoformat().replace("+00:00", "Z"),
                    "rights_id": "whitebit-public-websocket-internal-v1",
                },
                "public_data_only": True,
                "trading_authority": False,
            }
            market.write_text(json.dumps(snapshot), encoding="utf-8")
            baseline = manifest["workstreams"][0]
            state_path = organism / baseline["memory_namespace"] / "attention-state.json"
            state_path.write_text(json.dumps({
                "reference_price": "100",
                "next_scheduled_review": (NOW + timedelta(hours=1)).isoformat(),
            }), encoding="utf-8")
            first = run_cycle(
                organism_root=organism,
                market_snapshot=market,
                now=NOW + timedelta(milliseconds=20),
            )
            baseline_state = next(
                row for row in first["workstreams"] if row["profile_id"] == "BASELINE"
            )
            self.assertEqual(baseline_state["reason"], "stop-loss boundary reached")
            self.assertEqual(baseline_state["reference_price"], "94")

            second = run_cycle(
                organism_root=organism,
                market_snapshot=market,
                now=NOW + timedelta(seconds=1),
            )
            baseline_state = next(
                row for row in second["workstreams"] if row["profile_id"] == "BASELINE"
            )
            self.assertNotEqual(baseline_state["reason"], "stop-loss boundary reached")

    def test_attention_runner_rejects_manifest_tampering(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            build_manifest(root=root, symbols=("BTC_USDT",), created_at=NOW)
            manifest_path = root / "organism.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["workstreams"][0]["max_position_fraction"] = "0.50"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "digest"):
                run_cycle(
                    organism_root=root,
                    market_snapshot=root / "never-read.json",
                    now=NOW,
                )

    def test_persisted_profile_tampering_is_rejected(self):
        document = spec("TARGET_25").document()
        document["profile"]["target_return_fraction"] = "0.24"
        with self.assertRaisesRegex(ValueError, "modified"):
            workstream_from_document(document)


if __name__ == "__main__":
    unittest.main()





