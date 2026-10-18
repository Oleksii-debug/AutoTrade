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
from tools.run_goal_directed_attention import run_cycle


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
