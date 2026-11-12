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
from tools.run_goal_directed_attention import (
    _flow_research_summary,
    _triangular_shadow_summary,
    run_cycle,
)


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
                        "nwÓ­í¢G§²ÚîÆ­yÖ÷CÖÖ&¶WBÀ¢æ÷sÔäõr²F–ÖVFVÇF†Ö–ÆÆ—6V6öæG3Ó3’À¢ÆV&æ–æuöfÆVWEö6†V6·ö–çCÖfÆVWBÀ¢f÷'v&E÷6†F÷uö6†V6·ö–çC×6†F÷rÀ¢&öÆÆ–æuöÇ†ö6†V6·ö–çC×&öÆÆ–ærÀ¢÷÷'GVæ—G•ö6†V6·ö–çCÖ÷÷'GVæ—G’À¢G&FUöfÆ÷uö6†V6·ö–çC×G&FUöfÆ÷rÀ¢fÆ÷u÷&W6V&6…ö6†V6·ö–çCÖfÆ÷u÷&W6V&6‚À¢Ö¶W%÷6†F÷uö6†V6·ö–çCÖÖ¶W"À¢VæöÖ¶W%÷6†F÷uö6†V6·ö–çCÖÖ¶W%öVæÀ¢G&–æwVÆ%÷6†F÷uö6†V6·ö–çC×G&–ævÆRÀ¢¢Wf–FVæ6RÒVç&–6†VE²&÷&væ—6ÕöWf–FVæ6R%Ğ¢6VÆbæ76W'DWVÂ†Wf–FVæ6U²&ÆV&æ–æuöfÆVWB%Õ²&6ö×ÆWFVEöW—6öFW2%ÒÂ"¢6VÆbæ76W'DWVÂ†Wf–FVæ6U²&ÆV&æ–æuöfÆVWB%Õ²'†6Uö6÷VçG2%ÒÂ²$UdÅTD”är#¢Ò¢6VÆbæ76W'DWVÂ†Wf–FVæ6U²&f÷'v&E÷6†F÷r%Õ²&æWE÷æÂ%ÒÂ#ã""¢6VÆbæ76W'DWVÂ†Wf–FVæ6U²&f÷'v&E÷6†F÷r%Õ²'VçF—G’%ÒÂ"Ó""¢6VÆbæ76W'DfÇ6R†Wf–FVæ6U²&f÷'v&E÷6†F÷r%Õ²'G&F–æuöWF†÷&—G’%Ò¢Ç†ÒWf–FVæ6U²'&öÆÆ–æuöæF—fUöÇ†%Ğ¢6VÆbæ76W'DWVÂ†Ç†²&7–6ÆUö6÷VçB%ÒÂ¢6VÆbæ76W'DWVÂ†Ç†²'76VEöÖöFVÅö6÷VçB%ÒÂ¢6VÆbæ76W'DWVÂ†Ç†²&Ö&¶WG2%Õ³Õ²&ÖöFVÇ2%Õ³Õ²'6†÷'B%ÒÂ¢6VÆbæ76W'DWVÂ€¢Ç†²&Ö&¶WG2%Õ³Õ²&ÖöFVÇ2%Õ³Õ²&æWE÷æÅögFW%ö6÷7G5÷V÷FR%ÒÂ"Óbã‚ ¢¢6VÆbæ76W'DfÇ6R†Ç†²&WFöÖF–5÷&öÖ÷F–öâ%Ò¢÷÷'GVæ—G•÷7VÖÖ'’ÒWf–FVæ6U²&÷÷'GVæ—G•÷67&VVâ%Ğ¢6VÆbæ76W'DWVÂ†÷÷'GVæ—G•÷7VÖÖ'•²'6VÆV7FVEöÖ&¶WEö6÷VçB%ÒÂ"¢6VÆbæ76W'DWVÂ†÷÷'GVæ—G•÷7VÖÖ'•²'VÆ–f–VEöÖ&¶WEö6÷VçB%ÒÂ¢6VÆbæ76W'DWVÂ†÷÷'GVæ—G•÷7VÖÖ'•²'&V¦V7FVEöFFö6÷VçB%ÒÂ¢6VÆbæ76W'DfÇ6R†÷÷'GVæ—G•÷7VÖÖ'•²&Ö&¶WG2%Õ³Õ²'&÷7V7F—fU÷VÆ–f–6F–öâ%Ò¢6VÆbæ76W'DfÇ6R†÷÷'GVæ—G•÷7VÖÖ'•²&WFöÖF–5÷&öÖ÷F–öâ%Ò¢G&FUöfÆ÷u÷7VÖÖ'’ÒWf–FVæ6U²'G&FUöfÆ÷u÷67&VVâ%Ğ¢6VÆbæ76W'DWVÂ‡G&FUöfÆ÷u÷7VÖÖ'•²&Ö–æ–×VÕö'V6¶WG2%ÒÂ#c¢6VÆbæ76W'DWVÂ‡G&FUöfÆ÷u÷7VÖÖ'•²&Ö&¶WEö6÷VçB%ÒÂ"¢6VÆbæ76W'DWVÂ‡G&FUöfÆ÷u÷7VÖÖ'•²'VÆ–f–VEöÖ&¶WEö6÷VçB%ÒÂ¢6VÆbæ76W'DWVÂ€¢G&FUöfÆ÷u÷7VÖÖ'•²&Ö&¶WG2%Õ³Õ²&f–Æ&ÆUö6W6Åö'V6¶WG2%ÒÂ3#¢¢6VÆbæ76W'DfÇ6R‡G&FUöfÆ÷u÷7VÖÖ'•²&WFöÖF–5÷&öÖ÷F–öâ%Ò¢6VÆbæ76W'DfÇ6R‡G&FUöfÆ÷u÷7VÖÖ'•²'G&F–æuöWF†÷&—G’%Ò¢fÆ÷u÷&W6V&6…÷7VÖÖ'’ÒWf–FVæ6U²&fÆ÷u÷&W6V&6…÷67&VVâ%Ğ¢6VÆbæ76W'DWVÂ†fÆ÷u÷&W6V&6…÷7VÖÖ'•²&7–6ÆUö6÷VçB%ÒÂ2¢6VÆbæ76W'DWVÂ†fÆ÷u÷&W6V&6…÷7VÖÖ'•²&Æ7Eö7–6ÆU÷6V6öæG2%ÒÂ#Rã#R"¢6VÆbæ76W'DWVÂ€¢fÆ÷u÷&W6V&6…÷7VÖÖ'•²&Æ7E÷&W7VÇB%Õ²'&W7VÇE÷6†#Sb%ÒÂ&"¢c@¢¢6VÆbæ76W'DWVÂ€¢fÆ÷u÷&W6V&6…÷7VÖÖ'•²&Æ7E÷&W7VÇB%Õ²&—6öÆFVEöW'&÷%ö6÷VçB%ÒÂ ¢¢6VÆbæ76W'Dæ÷D–â€¢&Wf–FVæ6U÷F‚"ÂfÆ÷u÷&W6V&6…÷7VÖÖ'•²&Æ7E÷&W7VÇB%Ğ¢¢6VÆbæ76W'DfÇ6R†fÆ÷u÷&W6V&6…÷7VÖÖ'•²&WFöÖF–5÷&öÖ÷F–öâ%Ò¢6VÆbæ76W'DfÇ6R†fÆ÷u÷&W6V&6…÷7VÖÖ'•²'G&F–æuöWF†÷&—G’%Ò¢Ö¶W%÷7VÖÖ'’ÒWf–FVæ6U²&Ö¶W%÷6†F÷r%Ğ¢6VÆbæ76W'DWVÂ†Ö¶W%÷7VÖÖ'•²'6WGFÆVEö7–6ÆW2%ÒÂ"¢6VÆbæ76W'DWVÂ†Ö¶W%÷7VÖÖ'•²'÷6—F—fUö7–6ÆW2%ÒÂ¢6VÆbæ76W'DWVÂ†Ö¶W%÷7VÖÖ'•²&æWE÷æÅ÷V÷FR%ÒÂ"ÓãR"¢6VÆbæ76W'DWVÂ†Ö¶W%÷7VÖÖ'•²'&W6V&6…÷†6R%ÒÂ%$U4T$4…ôd”ÅDU$TB"¢6VÆbæ76W'DWVÂ†Ö¶W%÷7VÖÖ'•²'vfUö7–6ÆW2%ÒÂ¢6VÆbæ76W'DWVÂ€¢Ö¶W%÷7VÖÖ'•²&Æ7E÷V÷FUöFÖ—76–öâ%Õ²'&V6öâ%ÒÂ$D•$T5D”ôäÅôdÄõr ¢¢6VÆbæ76W'EG'VR†Ö¶W%÷7VÖÖ'•²&÷Vå÷V÷FR%Õ²&&–Eöf–ÆÆVB%Ò¢6VÆbæ76W'DWVÂ€¢Ö¶W%÷7VÖÖ'•²&Æ7E÷6WGFÆVB%Õ²&÷WF6öÖR%ÒÂ$$”EôôäÅ•ôdõ$4TEôU„•B ¢¢6VÆbæ76W'DfÇ6R†Ö¶W%÷7VÖÖ'•²&WFöÖF–5÷&öÖ÷F–öâ%Ò¢Væ÷7VÖÖ'’ÒWf–FVæ6U²&VæöÖ¶W%÷6†F÷r%Ğ¢6VÆbæ76W'DWVÂ†Væ÷7VÖÖ'•²&Ö&¶WB%ÒÂ$TäõU4EB"¢6VÆbæ76W'DWVÂ†Væ÷7VÖÖ'•²'&W6V&6…÷†6R%ÒÂ$D•44õdU%’"¢6VÆbæ76W'DWVÂ†Væ÷7VÖÖ'•²&æWE÷æÅ÷V÷FR%ÒÂ#"¢G&–ævÆU÷7VÖÖ'’ÒWf–FVæ6U²'7F&ÆUöf–E÷G&–æwVÆ%÷6†F÷r%Ğ¢6VÆbæ76W'DWVÂ‡G&–ævÆU÷7VÖÖ'•²&ÆFW7EöÖ&¶WEö6÷VçB%ÒÂ’¢6VÆbæ76W'DWVÂ‡G&–ævÆU÷7VÖÖ'•²&7–6ÆU÷&W7VÇEö6÷VçB%ÒÂ‚¢6VÆbæ76W'DWVÂ‡G&–ævÆU÷7VÖÖ'•²&÷÷'GVæ—G•ö6÷VçB%ÒÂ¢6VÆbæ76W'DWVÂ€¢G&–ævÆU÷7VÖÖ'•²&&W7E÷&W7VÇB%Õ²'F‚%ÒÀ¢²%U4EB"Â%U4D2"Â%U4B"Â%U4EB%ÒÀ¢¢6VÆbæ76W'DWVÂ€¢G&–ævÆU÷7VÖÖ'•²&&W7E÷&W7VÇB%Õ²&æWEögFW%÷&W6W'fUö'2%ÒÀ¢"Ó3ã“c#33s33"À¢¢6VÆbæ76W'DfÇ6R‡G&–ævÆU÷7VÖÖ'•²&WFöÖF–5÷&öÖ÷F–öâ%Ò ¢FVbFW7E÷G&–æwVÆ%÷6†F÷u÷&ö¦V7F–öåöf–Ç5ö6Æ÷6VEööåöWF†÷&—G•ö÷%öÖÆf÷&ÖVE÷&W7VÇB‡6VÆb“ ¢v—F‚FV×f–ÆRåFV×÷&'”F—&V7F÷'’‚’2F—&V7F÷'“ ¢6†V6·ö–çBÒF‚†F—&V7F÷'’’ò'G&–ævÆRæ§6öâ ¢&6RÒ°¢'66†VÖ#¢&WF÷G&FR×v†—FV&—B×G&–æwVÆ"×6†F÷r×7WW'f—6÷"×c"À¢'†6R#¢%%Tää”är"Â'–EöÖöFVÅö6ÆÇ2#¢À¢'&öf—EöwV&çFVVB#¢fÇ6RÀ¢&æWGv÷&µö÷&FW'2#¢fÇ6RÂ'G&F–æuöWF†÷&—G’#¢fÇ6RÀ¢&ÆFW7EöÖ&¶WEö6÷VçB#¢’Â&7–6ÆU÷&W7VÇEö6÷VçB#¢‚À¢&÷÷'GVæ—G•ö6÷VçB#¢Â&æ÷F–öæÂ#¢#"À¢&–×7E÷&W6W'fUö'2#¢#""À¢&&W7E÷&W7VÇB#¢æöæRÀ¢Ğ¢6†V6·ö–çBçw&—FU÷FW‡B†§6öâæGV×2‡²¢¦&6RÂ'G&F–æuöWF†÷&—G’#¢G'VWÒ’À¢Væ6öF–æsÒ'WFbÓ‚"¢v—F‚6VÆbæ76W'E&—6W5&VvW‚…fÇVTW'&÷"Â&WF†÷&—G’÷"66†VÖ"“ ¢÷G&–æwVÆ%÷6†F÷u÷7VÖÖ'’†6†V6·ö–çB¢6†V6·ö–çBçw&—FU÷FW‡B†§6öâæGV×2‡°¢¢¦&6RÀ¢&&W7E÷&W7VÇB#¢°¢'F‚#¢²%U4EB"Â%U4D2%ÒÀ¢&æWEögFW%÷F¶W%öfVW5ö'2#¢#"À¢&æWEögFW%÷&W6W'fUö'2#¢#ãR"À¢'VÆ–f–VEöf÷%÷6†F÷uöö'6W'fF–öâ#¢G'VRÀ¢'&W7VÇE÷6†#Sb#¢&"¢cBÀ¢ÒÀ¢Ò’ÂVæ6öF–æsÒ'WFbÓ‚"¢v—F‚6VÆbæ76W'E&—6W5&VvW‚…fÇVTW'&÷"Â&&W7B&W7VÇB—2ÖÆf÷&ÖVB"“ ¢÷G&–æwVÆ%÷6†F÷u÷7VÖÖ'’†6†V6·ö–çB ¢FVbFW7EöfÆ÷u÷&W6V&6…÷&ö¦V7F–öåöf–Ç5ö6Æ÷6VEööåöWF†÷&—G•ö÷%÷&W7VÇB‡6VÆb“ ¢v—F‚FV×f–ÆRåFV×÷&'”F—&V7F÷'’‚’2F—&V7F÷'“ ¢6†V6·ö–çBÒF‚†F—&V7F÷'’’ò&fÆ÷r×&W6V&6‚æ§6öâ ¢&6RÒ°¢'66†VÖ#¢&WF÷G&FR×v†—FV&—BÖfÆ÷r×&W6V&6‚×7WW'f—6÷"×c"À¢'7FGW2#¢%%Tää”är"Â&7–6ÆUö6÷VçB#¢À¢&Æ7Eö7–6ÆU÷6V6öæG2#¢"ãRÂ&Æ7EöW'&÷%ö6Æ72#¢æöæRÀ¢'WFFVEöB#¢äõræ—6öf÷&ÖB‚’À¢&æW‡E÷'VåöB#¢„äõr²F–ÖVFVÇF†Ö–çWFW3Ó3’’æ—6öf÷&ÖB‚’À¢&Æ7E÷&W7VÇB#¢°¢'&W7VÇE÷6†#Sb#¢&"¢cBÀ¢'föÆF–Æ—G•÷VÆ–f–VEö6÷VçB#¢À¢&ÆVEöÆu÷VÆ–f–VEö6÷VçB#¢À¢&—6öÆFVEöW'&÷%ö6÷VçB#¢À¢ÒÀ¢&WFöÖF–5÷&öÖ÷F–öâ#¢fÇ6RÂ&æWGv÷&µ÷&WVW7G2#¢À¢'–EöÖöFVÅö6ÆÇ2#¢Â&æWGv÷&µö÷&FW'2#¢fÇ6RÀ¢'G&F–æuöWF†÷&—G’#¢fÇ6RÀ¢Ğ¢6†V6·ö–çBçw&—FU÷FW‡B†§6öâæGV×2‡²¢¦&6RÂ&æWGv÷&µö÷&FW'2#¢G'VWÒ’À¢Væ6öF–æsÒ'WFbÓ‚"¢v—F‚6VÆbæ76W'E&—6W5&VvW‚…fÇVTW'&÷"Â&WF†÷&—G’÷"66†VÖ"“ ¢öfÆ÷u÷&W6V&6…÷7VÖÖ'’†6†V6·ö–çB¢6†V6·ö–çBçw&—FU÷FW‡B†§6öâæGV×2‡°¢¢¦&6RÀ¢&Æ7E÷&W7VÇB#¢²¢¦&6U²&Æ7E÷&W7VÇB%ÒÂ'&W7VÇE÷6†#Sb#¢&&B'ÒÀ¢Ò’ÂVæ6öF–æsÒ'WFbÓ‚"¢v—F‚6VÆbæ76W'E&—6W5&VvW‚…fÇVTW'&÷"Â&Æ7B&W7VÇB—2ÖÆf÷&ÖVB"“ ¢öfÆ÷u÷&W6V&6…÷7VÖÖ'’†6†V6·ö–çB¢6†V6·ö–çBçw&—FU÷FW‡B†§6öâæGV×2‡²¢¦&6RÂ&Æ7E÷&W7VÇB#¢æöæWÒ’À¢Væ6öF–æsÒ'WFbÓ‚"¢v—F‚6VÆbæ76W'E&—6W5&VvW‚…fÇVTW'&÷"Â&Æ6·2&W7VÇB"“ ¢öfÆ÷u÷&W6V&6…÷7VÖÖ'’†6†V6·ö–çB ¢FVbFW7EöGFVçF–öå÷'VææW%÷&V&6W5÷&VfW&Væ6UögFW%÷v¶R‡6VÆb“ ¢v—F‚FV×f–ÆRåFV×÷&'”F—&V7F÷'’‚’2F—&V7F÷'“ ¢&ö÷BÒF‚†F—&V7F÷'’¢÷&væ—6ÒÒ&ö÷Bò&÷&væ—6Ò ¢Öæ–fW7BÒ'V–ÆEöÖæ–fW7B€¢&ö÷CÖ÷&væ—6ÒÀ¢7–Ö&öÇ3Ò‚$%D5õU4EB"Â’À¢7&VFVEöCÔäõrÀ¢¢Ö&¶WBÒ&ö÷Bò&ÆFW7BÖÖ&¶WBæ§6öâ ¢6æ6†÷BÒ°¢'66†VÖ#¢&WF÷G&FR×v†—FV&—B×V&Æ–2×7WW'f—6÷"×c"À¢&Ö&¶WB#¢$%D5õU4EB"À¢'fVçVR#¢%t„•DT$•Eõ5õB"À¢&&–E÷&–6R#¢#“2"À¢&&–E÷VçF—G’#¢#"À¢&6µ÷&–6R#¢#“R"À¢&6µ÷VçF—G’#¢#""À¢'6÷W&6UöWfVçEöB#¢äõræ—6öf÷&ÖB‚’À¢&f–Æ&ÆUöB#¢äõræ—6öf÷&ÖB‚’À¢'V&Æ—6†VEöB#¢äõræ—6öf÷&ÖB‚’À¢'WFFUö–B#¢À¢'7G&VÕövVæW&F–öâ#¢À¢'&uöWf–FVæ6U÷&Vb#¢°¢&'F–f7Eö–B#¢&öæR"À¢'6†#Sb#¢'6†#Sc¢"²#"¢cBÀ¢'6÷W&6U÷W&’#¢'w73¢ò÷w72çv†—FV&—Bæ6öÒ÷w26&ööµF–6¶W""À¢&ö'6W'fVEöB#¢äõræ—6öf÷&ÖB‚’ç&WÆ6R‚"³£"Â%¢"’À¢'&–v‡G5ö–B#¢'v†—FV&—B×V&Æ–2×vV'6ö6¶WBÖ–çFW&æÂ×c"À¢ÒÀ¢'V&Æ–5öFFööæÇ’#¢G'VRÀ¢'G&F–æuöWF†÷&—G’#¢fÇ6RÀ¢Ğ¢Ö&¶WBçw&—FU÷FW‡B†§6öâæGV×2‡6æ6†÷B’ÂVæ6öF–æsÒ'WFbÓ‚"¢&6VÆ–æRÒÖæ–fW7E²'v÷&·7G&V×2%Õ³Ğ¢7FFU÷F‚Ò÷&væ—6Òò&6VÆ–æU²&ÖVÖ÷'•öæÖW76R%Òò&GFVçF–öâ×7FFRæ§6öâ ¢7FFU÷F‚çw&—FU÷FW‡B†§6öâæGV×2‡°¢'&VfW&Væ6U÷&–6R#¢#"À¢&æW‡E÷66†VGVÆVE÷&Wf–Wr#¢„äõr²F–ÖVFVÇF††÷W'3Ó’’æ—6öf÷&ÖB‚’À¢Ò’ÂVæ6öF–æsÒ'WFbÓ‚"¢f—'7BÒ'Våö7–6ÆR€¢÷&væ—6Õ÷&ö÷CÖ÷&væ—6ÒÀ¢Ö&¶WE÷6æ6†÷CÖÖ&¶WBÀ¢æ÷sÔäõr²F–ÖVFVÇF†Ö–ÆÆ—6V6öæG3Ó#’À¢¢&6VÆ–æU÷7FFRÒæW‡B€¢&÷rf÷"&÷r–âf—'7E²'v÷&·7G&V×2%Ò–b&÷u²'&öf–ÆUö–B%ÒÓÒ$$4TÄ”äR ¢¢6VÆbæ76W'DWVÂ†&6VÆ–æU÷7FFU²'&V6öâ%ÒÂ'7F÷ÖÆ÷72&÷VæF'’&V6†VB"¢6VÆbæ76W'DWVÂ†&6VÆ–æU÷7FFU²'&VfW&Væ6U÷&–6R%ÒÂ#“B" ¢6V6öæBÒ'Våö7–6ÆR€¢÷&væ—6Õ÷&ö÷CÖ÷&væ—6ÒÀ¢Ö&¶WE÷6æ6†÷CÖÖ&¶WBÀ¢æ÷sÔäõr²F–ÖVFVÇF‡6V6öæG3Ó’À¢¢&6VÆ–æU÷7FFRÒæW‡B€¢&÷rf÷"&÷r–â6V6öæE²'v÷&·7G&V×2%Ò–b&÷u²'&öf–ÆUö–B%ÒÓÒ$$4TÄ”äR ¢¢6VÆbæ76W'Dæ÷DWVÂ†&6VÆ–æU÷7FFU²'&V6öâ%ÒÂ'7F÷ÖÆ÷72&÷VæF'’&V6†VB" ¢FVbFW7EöGFVçF–öå÷'VææW%÷&V¦V7G5öÖæ–fW7E÷F×W&–ær‡6VÆb“ ¢v—F‚FV×f–ÆRåFV×÷&'”F—&V7F÷'’‚’2F—&V7F÷'“ ¢&ö÷BÒF‚†F—&V7F÷'’¢'V–ÆEöÖæ–fW7B‡&ö÷C×&ö÷BÂ7–Ö&öÇ3Ò‚$%D5õU4EB"Â’Â7&VFVEöCÔäõr¢Öæ–fW7E÷F‚Ò&ö÷Bò&÷&væ—6Òæ§6öâ ¢Öæ–fW7BÒ§6öâæÆöG2†Öæ–fW7E÷F‚ç&VE÷FW‡B†Væ6öF–æsÒ'WFbÓ‚"’¢Öæ–fW7E²'v÷&·7G&V×2%Õ³Õ²&Ö…÷÷6—F–öåög&7F–öâ%ÒÒ#ãS ¢Öæ–fW7E÷F‚çw&—FU÷FW‡B†§6öâæGV×2†Öæ–fW7B’ÂVæ6öF–æsÒ'WFbÓ‚"¢v—F‚6VÆbæ76W'E&—6W5&VvW‚…fÇVTW'&÷"Â&F–vW7B"“ ¢'Våö7–6ÆR€¢÷&væ—6Õ÷&ö÷C×&ö÷BÀ¢Ö&¶WE÷6æ6†÷C×&ö÷Bò&æWfW"×&VBæ§6öâ"À¢æ÷sÔäõrÀ¢ ¢FVbFW7E÷W'6—7FVE÷&öf–ÆU÷F×W&–æuö—5÷&V¦V7FVB‡6VÆb“ ¢Fö7VÖVçBÒ7V2‚%D$tUEó#R"’æFö7VÖVçB‚¢Fö7VÖVçE²'&öf–ÆR%Õ²'F&vWE÷&WGW&åög&7F–öâ%ÒÒ#ã#B ¢v—F‚6VÆbæ76W'E&—6W5&VvW‚…fÇVTW'&÷"Â&ÖöF–f–VB"“ ¢v÷&·7G&VÕög&öÕöFö7VÖVçB†Fö7VÖVçB  ¦–bõöæÖUõòÓÒ%õöÖ–åõò# ¢Væ—GFW7BæÖ–â‚     