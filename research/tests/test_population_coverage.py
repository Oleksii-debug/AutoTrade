from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from research.autotrade_research.learning.population_coverage import (
    build_population_coverage,
)
from research.autotrade_research.learning.retention import (
    RegimeMetric,
    RetentionPolicy,
    evaluate_population_bound_retention,
)
from research.autotrade_research.memory.episodes import ExperienceMemory


H1 = "sha256:" + "1" * 64
H2 = "sha256:" + "2" * 64
H3 = "sha256:" + "3" * 64
DECISION = datetime(2026, 9, 25, 12, tzinfo=timezone.utc)
CUTOFF = datetime(2030, 1, 1, tzinfo=timezone.utc)


def episode_payload(outcome_class, *, side="BUY", label_mature=True, label="observed"):
    return {
        "evidence_refs": ["artifact:evidence"],
        "intended_action": {"side": side},
        "actual_execution": {"fills": []},
        "outcome": {
            "class": outcome_class,
            "label": label,
            "label_mature": label_mature,
            "reconciliation_state": "RECONCILED" if label_mature else "PENDING",
        },
        "costs": {"total": "0"},
    }


def retention_policy():
    return RetentionPolicy.create(
        protected_regimes=[],
        recent_regimes=["calm"],
        max_protected_degradation="0",
        max_recent_degradation="0",
        min_recent_improvement="0",
        min_observations_per_regime=1,
        require_complete_labels=True,
        independent_science_gate_passed=True,
        risk_gate_passed=True,
    )


class PopulationCoverageTests(unittest.TestCase):
    def _store_with_negative_and_no_trade(self, directory):
        store = ExperienceMemory(Path(directory) / "memory.sqlite3")
        negative, _ = store.append_episode(
            episode_id="11111111-1111-4111-8111-111111111111",
            decision_time=DECISION,
            information_cutoff=DECISION,
            task="research",
            regime="calm",
            instrument_family="equity",
            permission_class="research",
            payload=episode_payload("NEGATIVE", side="SELL", label="loss"),
        )
        no_trade, _ = store.append_episode(
            episode_id="22222222-2222-4222-8222-222222222222",
            decision_time=DECISION,
            information_cutoff=DECISION,
            task="research",
            regime="calm",
            instrument_family="equity",
            permission_class="research",
            payload=episode_payload("NULL", side="NO_TRADE", label="no-trade"),
        )
        return store, negative, no_trade

    def _manifest(self, store, included, exclusions=None):
        population = store.coverage_population(
            causal_cutoff=CUTOFF,
            granted_permissions={"research"},
            task="research",
            instrument_family="equity",
        )
        return build_population_coverage(
            population,
            candidate_hash=H1,
            frozen_protocol_hash=H2,
            input_snapshot_hash=H3,
            causal_cutoff=CUTOFF,
            permission_classes=["research"],
            included_episode_ids=included,
            exclusions=exclusions or {},
            task="research",
            instrument_family="equity",
        )

    def test_negative_and_no_trade_are_first_class_population_evidence(self):
        with TemporaryDirectory() as directory:
            store, negative, no_trade = self._store_with_negative_and_no_trade(directory)
            manifest = self._manifest(store, [negative, no_trade])
            eligible = {kind: count for kind, count, _digest in manifest.eligible_outcomes}
            self.assertEqual(eligible["NEGATIVE"], 1)
            self.assertEqual(eligible["NULL"], 1)
            self.assertEqual(manifest.eligible_no_trade_count, 1)
            self.assertEqual(manifest.included_no_trade_count, 1)
            self.assertTrue(manifest.complete)
            self.assertTrue(manifest.digest.startswith("sha256:"))

    def test_silent_omission_of_eligible_negative_or_no_trade_fails(self):
        with TemporaryDirectory() as directory:
            store, negative, no_trade = self._store_with_negative_and_no_trade(directory)
            with self.assertRaisesRegex(ValueError, "not fully accounted"):
                self._manifest(store, [no_trade])
            with self.assertRaisesRegex(ValueError, "not fully accounted"):
                self._manifest(store, [negative])

    def test_explicit_exclusion_is_visible_and_digest_bound(self):
        with TemporaryDirectory() as directory:
            store, negative, no_trade = self._store_with_negative_and_no_trade(directory)
            manifest = self._manifest(
                store,
                [negative],
                {no_trade: "FROZEN_PROTOCOL_EXCLUSION:NO_TRADE_CONTROL"},
            )
            self.assertEqual(
                manifest.exclusions,
                ((no_trade, "FROZEN_PROTOCOL_EXCLUSION:NO_TRADE_CONTROL"),),
            )
            self.assertEqual(manifest.included_no_trade_count, 0)
            changed = self._manifest(
                store,
                [negative],
                {no_trade: "FROZEN_PROTOCOL_EXCLUSION:OTHER"},
            )
            self.assertNotEqual(manifest.digest, changed.digest)

    def test_late_correction_cannot_rewrite_historical_population_digest(self):
        with TemporaryDirectory() as directory:
            store, negative, no_trade = self._store_with_negative_and_no_trade(directory)
            before = self._manifest(store, [negative, no_trade])
            store.append_correction(
                negative,
                correction_id="33333333-3333-4333-8333-333333333333",
                available_at=datetime(2031, 1, 1, tzinfo=timezone.utc),
                payload={
                    "supersedes_fields": ["outcome"],
                    "outcome": {
                        "class": "POSITIVE",
                        "label": "late-reversal",
                        "label_mature": True,
                        "reconciliation_state": "RECONCILED",
                    },
                    "evidence_ref": "artifact:late-correction",
                },
            )
            after = self._manifest(store, [negative, no_trade])
            self.assertEqual(before.digest, after.digest)
            self.assertEqual(before.episode_digests, after.episode_digests)

    def test_correction_available_before_cutoff_changes_manifest_identity(self):
        with TemporaryDirectory() as directory:
            store, negative, no_trade = self._store_with_negative_and_no_trade(directory)
            before = self._manifest(store, [negative, no_trade])
            store.append_correction(
                negative,
                correction_id="44444444-4444-4444-8444-444444444444",
                available_at=datetime(2029, 1, 1, tzinfo=timezone.utc),
                payload={
                    "supersedes_fields": ["outcome"],
                    "outcome": {
                        "class": "POSITIVE",
                        "label": "reconciled",
                        "label_mature": True,
                        "reconciliation_state": "RECONCILED",
                    },
                    "evidence_ref": "artifact:correction",
                },
            )
            after = self._manifest(store, [negative, no_trade])
            self.assertNotEqual(before.digest, after.digest)
            eligible = {kind: count for kind, count, _digest in after.eligible_outcomes}
            self.assertEqual(eligible["POSITIVE"], 1)
            self.assertEqual(eligible["NEGATIVE"], 0)

    def test_manifest_direct_tamper_is_rejected(self):
        with TemporaryDirectory() as directory:
            store, negative, no_trade = self._store_with_negative_and_no_trade(directory)
            manifest = self._manifest(store, [negative, no_trade])
            with self.assertRaisesRegex(ValueError, "digest does not match"):
                replace(manifest, included_no_trade_count=0)

    def test_retention_observation_counts_must_match_covered_population(self):
        with TemporaryDirectory() as directory:
            store, negative, no_trade = self._store_with_negative_and_no_trade(directory)
            manifest = self._manifest(store, [negative, no_trade])
            good = {
                "calm": RegimeMetric.create(
                    regime="calm",
                    champion_net_score=Decimal("0.10"),
                    candidate_net_score=Decimal("0.11"),
                    observations=2,
                    label_complete=True,
                )
            }
            result = evaluate_population_bound_retention(
                good,
                retention_policy(),
                manifest,
            )
            self.assertEqual(result.status, "PASS")
            self.assertTrue(result.promotable)
            self.assertEqual(result.population_coverage_digest, manifest.digest)

            forged_count = {
                "calm": RegimeMetric.create(
                    regime="calm",
                    champion_net_score=Decimal("0.10"),
                    candidate_net_score=Decimal("0.11"),
                    observations=1,
                    label_complete=True,
                )
            }
            rejected = evaluate_population_bound_retention(
                forged_count,
                retention_policy(),
                manifest,
            )
            self.assertEqual(rejected.status, "INCONCLUSIVE")
            self.assertFalse(rejected.promotable)
            self.assertIn(
                "population observation count mismatch for regime calm",
                rejected.reasons,
            )

    def test_unresolved_label_prevents_population_bound_retention_pass(self):
        with TemporaryDirectory() as directory:
            store = ExperienceMemory(Path(directory) / "memory.sqlite3")
            pending, _ = store.append_episode(
                episode_id="55555555-5555-4555-8555-555555555555",
                decision_time=DECISION,
                information_cutoff=DECISION,
                task="research",
                regime="calm",
                instrument_family="equity",
                permission_class="research",
                payload=episode_payload(
                    "PENDING",
                    side="BUY",
                    label_mature=False,
                    label="pending",
                ),
            )
            manifest = self._manifest(store, [pending])
            metrics = {
                "calm": RegimeMetric.create(
                    regime="calm",
                    champion_net_score="0.10",
                    candidate_net_score="0.11",
                    observations=1,
                    label_complete=False,
                )
            }
            result = evaluate_population_bound_retention(
                metrics,
                retention_policy(),
                manifest,
            )
            self.assertEqual(result.status, "INCONCLUSIVE")
            self.assertFalse(result.promotable)


if __name__ == "__main__":
    unittest.main()
