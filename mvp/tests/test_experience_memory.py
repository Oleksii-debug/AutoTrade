from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.experience_memory import ExperienceMemoryStore


class ExperienceMemoryStoreTests(unittest.TestCase):
    def test_negative_and_null_outcomes_are_first_class(self):
        with TemporaryDirectory() as directory:
            store = ExperienceMemoryStore(f"{directory}/memory.sqlite3")
            store.append(
                episode_id="e-neg", decision_id="d1", evidence_cutoff="2026-09-24T10:00:00+00:00",
                evidence_refs=("event:1",),
                action_ref="a1", execution_ref="x1", outcome_class="NEGATIVE",
                outcome_payload={"pnl": "-2.5"}, source_version="v1",
            )
            store.append(
                episode_id="e-null", decision_id="d2", evidence_cutoff="2026-09-24T10:00:00+00:00",
                evidence_refs=("event:1",),
                action_ref=None, execution_ref=None, outcome_class="NULL",
                outcome_payload={"reason": "no-trade"}, source_version="v1",
            )
            counts = store.outcome_counts()
            self.assertEqual(counts["NEGATIVE"], 1)
            self.assertEqual(counts["NULL"], 1)

    def test_identity_and_reference_whitespace_canonicalizes_to_one_episode(self):
        with TemporaryDirectory() as directory:
            store = ExperienceMemoryStore(f"{directory}/memory.sqlite3")
            inserted = store.append(
                episode_id=" e1 ",
                decision_id=" d1 ",
                evidence_cutoff="2026-09-24T10:00:00+00:00",
                evidence_refs=(" event:1 ",),
                action_ref=" action:1 ",
                execution_ref=" execution:1 ",
                outcome_class="NULL",
                outcome_payload={"reason": "no-trade"},
                source_version=" v1 ",
            )
            self.assertTrue(inserted)
            replayed = store.append(
                episode_id="e1",
                decision_id="d1",
                evidence_cutoff="2026-09-24T10:00:00+00:00",
                evidence_refs=("event:1",),
                action_ref="action:1",
                execution_ref="execution:1",
                outcome_class="NULL",
                outcome_payload={"reason": "no-trade"},
                source_version="v1",
            )
            self.assertFalse(replayed)
            episode = store.get(" e1 ")
            self.assertEqual(episode.episode_id, "e1")
            self.assertEqual(episode.decision_id, "d1")
            self.assertEqual(episode.action_ref, "action:1")
            self.assertEqual(episode.execution_ref, "execution:1")
            self.assertEqual(episode.source_version, "v1")

    def test_exact_duplicate_is_idempotent_but_conflict_fails_closed(self):
        with TemporaryDirectory() as directory:
            store = ExperienceMemoryStore(f"{directory}/memory.sqlite3")
            args = dict(
                episode_id="e1", decision_id="d1", evidence_cutoff="2026-09-24T10:00:00+00:00",
                evidence_refs=("event:1",),
                action_ref=None, execution_ref=None, outcome_class="UNKNOWN",
                outcome_payload={"why": "pending reconciliation"}, source_version="v1",
            )
            self.assertTrue(store.append(**args))
            self.assertFalse(store.append(**args))
            changed = dict(args)
            changed["outcome_class"] = "POSITIVE"
            with self.assertRaisesRegex(ValueError, "conflicts"):
                store.append(**changed)

    def test_correction_appends_successor_without_overwriting_prior_truth(self):
        with TemporaryDirectory() as directory:
            store = ExperienceMemoryStore(f"{directory}/memory.sqlite3")
            store.append(
                episode_id="e1", decision_id="d1", evidence_cutoff="2026-09-24T10:00:00+00:00",
                evidence_refs=("event:1",),
                action_ref="a", execution_ref="x", outcome_class="UNKNOWN",
                outcome_payload={"state": "unreconciled"}, source_version="v1",
            )
            store.append(
                episode_id="e2", decision_id="d1", evidence_cutoff="2026-09-24T10:00:00+00:00",
                evidence_refs=("event:1",),
                action_ref="a", execution_ref="x", outcome_class="NEGATIVE",
                outcome_payload={"pnl": "-1"}, source_version="v2",
                supersedes_episode_id="e1",
            )
            prior = store.get("e1")
            current = store.get("e2")
            self.assertEqual(prior.outcome_class, "UNKNOWN")
            self.assertEqual(prior.superseded_by, "e2")
            self.assertEqual(current.outcome_class, "NEGATIVE")
            self.assertEqual([x.episode_id for x in store.retrieve()], ["e2"])

    def test_correction_cannot_cross_decision_lineage(self):
        with TemporaryDirectory() as directory:
            store = ExperienceMemoryStore(f"{directory}/memory.sqlite3")
            store.append(
                episode_id="e1", decision_id="d1", evidence_cutoff="2026-09-24T10:00:00+00:00",
                evidence_refs=("event:1",),
                action_ref=None, execution_ref=None, outcome_class="NULL",
                outcome_payload={}, source_version="v1",
            )
            with self.assertRaisesRegex(ValueError, "decision lineage"):
                store.append(
                    episode_id="e2", decision_id="d2", evidence_cutoff="2026-09-24T10:00:00+00:00", evidence_refs=("event:1",),
                    action_ref=None, execution_ref=None, outcome_class="NULL",
                    outcome_payload={}, source_version="v2",
                    supersedes_episode_id="e1",
                )

    def test_tombstone_hides_from_default_retrieval_but_preserves_truth(self):
        with TemporaryDirectory() as directory:
            store = ExperienceMemoryStore(f"{directory}/memory.sqlite3")
            store.append(
                episode_id="e1", decision_id="d1", evidence_cutoff="2026-09-24T10:00:00+00:00",
                evidence_refs=("event:1",),
                action_ref=None, execution_ref=None, outcome_class="NULL",
                outcome_payload={"source": "licensed"}, source_version="v1",
            )
            self.assertTrue(store.tombstone("e1", reason="retention expired"))
            self.assertFalse(store.tombstone("e1", reason="retention expired"))
            self.assertEqual(store.retrieve(), [])
            preserved = store.retrieve(include_tombstoned=True)
            self.assertEqual(len(preserved), 1)
            self.assertTrue(preserved[0].tombstoned)

    def test_retrieval_respects_information_cutoff(self):
        with TemporaryDirectory() as directory:
            store = ExperienceMemoryStore(f"{directory}/memory.sqlite3")
            store.append(
                episode_id="early", decision_id="d1",
                evidence_cutoff="2026-09-24T10:00:00+00:00",
                evidence_refs=("event:early",), action_ref=None, execution_ref=None,
                outcome_class="NULL", outcome_payload={}, source_version="v1",
            )
            store.append(
                episode_id="late", decision_id="d2",
                evidence_cutoff="2026-09-24T12:00:00+00:00",
                evidence_refs=("event:late",), action_ref=None, execution_ref=None,
                outcome_class="NULL", outcome_payload={}, source_version="v1",
            )
            visible = store.retrieve(
                information_cutoff="2026-09-24T11:00:00+00:00"
            )
            self.assertEqual([item.episode_id for item in visible], ["early"])

    def test_future_correction_does_not_hide_truth_at_historical_cutoff(self):
        with TemporaryDirectory() as directory:
            store = ExperienceMemoryStore(f"{directory}/memory.sqlite3")
            store.append(
                episode_id="e1", decision_id="d1",
                evidence_cutoff="2026-09-24T10:00:00+00:00",
                evidence_refs=("event:1",), action_ref="a", execution_ref="x",
                outcome_class="UNKNOWN", outcome_payload={"state": "pending"},
                source_version="v1",
            )
            store.append(
                episode_id="e2", decision_id="d1",
                evidence_cutoff="2026-09-24T12:00:00+00:00",
                evidence_refs=("event:2",), action_ref="a", execution_ref="x",
                outcome_class="NEGATIVE", outcome_payload={"pnl": "-1"},
                source_version="v2", supersedes_episode_id="e1",
            )

            historical = store.retrieve(
                information_cutoff="2026-09-24T11:00:00+00:00"
            )
            self.assertEqual([item.episode_id for item in historical], ["e1"])

            after_correction = store.retrieve(
                information_cutoff="2026-09-24T13:00:00+00:00"
            )
            self.assertEqual([item.episode_id for item in after_correction], ["e2"])

    def test_cutoff_and_evidence_refs_fail_closed(self):
        with TemporaryDirectory() as directory:
            store = ExperienceMemoryStore(f"{directory}/memory.sqlite3")
            with self.assertRaisesRegex(ValueError, "timezone-aware"):
                store.append(
                    episode_id="bad-time", decision_id="d", evidence_cutoff="2026-09-24T10:00:00",
                    evidence_refs=("event:1",), action_ref=None, execution_ref=None,
                    outcome_class="NULL", outcome_payload={}, source_version="v1",
                )
            with self.assertRaisesRegex(ValueError, "non-empty tuple"):
                store.append(
                    episode_id="bad-refs", decision_id="d",
                    evidence_cutoff="2026-09-24T10:00:00+00:00",
                    evidence_refs=(), action_ref=None, execution_ref=None,
                    outcome_class="NULL", outcome_payload={}, source_version="v1",
                )

    def test_non_finite_payload_is_rejected(self):
        with TemporaryDirectory() as directory:
            store = ExperienceMemoryStore(f"{directory}/memory.sqlite3")
            with self.assertRaises(ValueError):
                store.append(
                    episode_id="bad", decision_id="d", evidence_cutoff="2026-09-24T10:00:00+00:00", evidence_refs=("event:1",),
                    action_ref=None, execution_ref=None, outcome_class="POSITIVE",
                    outcome_payload={"score": float("nan")}, source_version="v1",
                )


if __name__ == "__main__":
    unittest.main()
