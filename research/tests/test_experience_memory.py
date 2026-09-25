from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from research.autotrade_research.memory.episodes import ExperienceMemory, MemoryConflict


BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def payload(outcome="flat"):
    return {
        "evidence_refs": ["artifact:abc"],
        "intended_action": {"side": "HOLD"},
        "actual_execution": {"fills": []},
        "outcome": {"label": outcome},
        "costs": {"USD": "0"},
    }


class ExperienceMemoryTests(unittest.TestCase):
    def test_duplicate_episode_is_idempotent(self):
        with TemporaryDirectory() as directory:
            store = ExperienceMemory(Path(directory) / "memory.sqlite3")
            identifier = "00000000-0000-0000-0000-000000000001"
            first, inserted = store.append_episode(
                episode_id=identifier,
                decision_time=BASE,
                information_cutoff=BASE,
                task="research",
                regime="calm",
                instrument_family="equity",
                permission_class="research",
                payload=payload(),
            )
            second, inserted_again = store.append_episode(
                episode_id=identifier,
                decision_time=BASE,
                information_cutoff=BASE,
                task="research",
                regime="calm",
                instrument_family="equity",
                permission_class="research",
                payload=payload(),
            )
            self.assertTrue(inserted)
            self.assertFalse(inserted_again)
            self.assertEqual(first, second)

    def test_episode_identity_uses_canonical_text_fields(self):
        with TemporaryDirectory() as directory:
            store = ExperienceMemory(Path(directory) / "memory.sqlite3")
            identifier = "00000000-0000-0000-0000-000000000077"
            first, inserted = store.append_episode(
                episode_id=identifier,
                decision_time=BASE,
                information_cutoff=BASE,
                task=" research ",
                regime=" calm ",
                instrument_family=" equity ",
                permission_class=" research ",
                payload=payload("flat"),
            )
            second, inserted_again = store.append_episode(
                episode_id=identifier,
                decision_time=BASE,
                information_cutoff=BASE,
                task="research",
                regime="calm",
                instrument_family="equity",
                permission_class="research",
                payload=payload("flat"),
            )
            self.assertEqual(first, second)
            self.assertTrue(inserted)
            self.assertFalse(inserted_again)
            retrieved = store.retrieve(
                information_cutoff=BASE,
                granted_permissions={"research"},
            )
            self.assertEqual(retrieved[0]["task"], "research")
            self.assertEqual(retrieved[0]["regime"], "calm")

    def test_episode_identity_cannot_be_destructively_upserted(self):
        with TemporaryDirectory() as directory:
            store = ExperienceMemory(Path(directory) / "memory.sqlite3")
            identifier = "00000000-0000-0000-0000-000000000001"
            kwargs = dict(
                episode_id=identifier,
                decision_time=BASE,
                information_cutoff=BASE,
                task="research",
                regime="calm",
                instrument_family="equity",
                permission_class="research",
            )
            store.append_episode(payload=payload(), **kwargs)
            with self.assertRaises(MemoryConflict):
                store.append_episode(payload=payload("changed"), **kwargs)

    def test_correction_preserves_old_truth_and_appends_lineage(self):
        with TemporaryDirectory() as directory:
            store = ExperienceMemory(Path(directory) / "memory.sqlite3")
            episode, _ = store.append_episode(
                decision_time=BASE,
                information_cutoff=BASE,
                task="research",
                regime="calm",
                instrument_family="equity",
                permission_class="research",
                payload=payload("pending"),
            )
            store.append_correction(
                episode,
                available_at=BASE,
                payload={
                    "supersedes_fields": ["outcome"],
                    "outcome": {"label": "reconciled"},
                    "evidence_ref": "artifact:correction",
                },
            )
            source = store.source_episode(episode)
            self.assertEqual(source["payload"]["outcome"]["label"], "pending")
            retrieved = store.retrieve(
                information_cutoff=BASE,
                granted_permissions={"research"},
            )
            self.assertEqual(retrieved[0]["payload"]["outcome"]["label"], "pending")
            self.assertEqual(retrieved[0]["corrections"][0]["outcome"]["label"], "reconciled")


    def test_correction_supersedes_fields_are_semantically_bound(self):
        with TemporaryDirectory() as directory:
            store = ExperienceMemory(Path(directory) / "memory.sqlite3")
            episode, _ = store.append_episode(
                decision_time=BASE,
                information_cutoff=BASE,
                task="research",
                regime="calm",
                instrument_family="equity",
                permission_class="research",
                payload=payload("pending"),
            )
            invalid_payloads = (
                {"supersedes_fields": [], "evidence_ref": "artifact:none"},
                {
                    "supersedes_fields": ["outcome", "outcome"],
                    "outcome": {"label": "duplicate"},
                    "evidence_ref": "artifact:duplicate",
                },
                {
                    "supersedes_fields": ["outcome"],
                    "evidence_ref": "artifact:missing-field",
                },
            )
            for candidate in invalid_payloads:
                with self.subTest(candidate=candidate), self.assertRaises(ValueError):
                    store.append_correction(
                        episode,
                        available_at=BASE,
                        payload=candidate,
                    )

    def test_correction_cannot_claim_to_supersede_absent_source_field(self):
        with TemporaryDirectory() as directory:
            store = ExperienceMemory(Path(directory) / "memory.sqlite3")
            episode, _ = store.append_episode(
                decision_time=BASE,
                information_cutoff=BASE,
                task="research",
                regime="calm",
                instrument_family="equity",
                permission_class="research",
                payload=payload("pending"),
            )
            with self.assertRaisesRegex(
                MemoryConflict,
                "absent from source episode",
            ):
                store.append_correction(
                    episode,
                    available_at=BASE,
                    payload={
                        "supersedes_fields": ["invented_field"],
                        "invented_field": {"value": "must-not-enter-history"},
                        "evidence_ref": "artifact:invalid-correction",
                    },
                )

    def test_correction_cannot_introduce_unsuperseded_historical_field(self):
        with TemporaryDirectory() as directory:
            store = ExperienceMemory(Path(directory) / "memory.sqlite3")
            episode, _ = store.append_episode(
                decision_time=BASE,
                information_cutoff=BASE,
                task="research",
                regime="calm",
                instrument_family="equity",
                permission_class="research",
                payload=payload("pending"),
            )
            with self.assertRaisesRegex(
                MemoryConflict,
                "cannot introduce fields absent from source episode",
            ):
                store.append_correction(
                    episode,
                    available_at=BASE,
                    payload={
                        "supersedes_fields": ["outcome"],
                        "outcome": {"label": "reconciled"},
                        "future_signal": {"score": "1"},
                        "evidence_ref": "artifact:correction",
                    },
                )

    def test_correction_retry_without_explicit_availability_is_idempotent(self):
        with TemporaryDirectory() as directory:
            store = ExperienceMemory(Path(directory) / "memory.sqlite3")
            episode, _ = store.append_episode(
                decision_time=BASE,
                information_cutoff=BASE,
                task="research",
                regime="calm",
                instrument_family="equity",
                permission_class="research",
                payload=payload("pending"),
            )
            correction_id = "00000000-0000-0000-0000-000000000099"
            correction = {
                "supersedes_fields": ["outcome"],
                "outcome": {"label": "late"},
                "evidence_ref": "artifact:retry",
            }
            first, inserted = store.append_correction(
                episode,
                correction_id=correction_id,
                payload=correction,
            )
            second, inserted_again = store.append_correction(
                episode,
                correction_id=correction_id,
                payload=correction,
            )
            self.assertTrue(inserted)
            self.assertFalse(inserted_again)
            self.assertEqual(first, second)

    def test_legacy_correction_hash_remains_idempotent_after_schema_migration(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "memory.sqlite3"
            store = ExperienceMemory(path)
            episode, _ = store.append_episode(
                decision_time=BASE,
                information_cutoff=BASE,
                task="research",
                regime="calm",
                instrument_family="equity",
                permission_class="research",
                payload=payload("pending"),
            )
            correction_id = "00000000-0000-0000-0000-000000000098"
            correction = {
                "supersedes_fields": ["outcome"],
                "outcome": {"label": "legacy"},
                "evidence_ref": "artifact:legacy",
            }
            store.append_correction(
                episode,
                correction_id=correction_id,
                available_at=BASE,
                payload=correction,
            )
            from research.autotrade_research.memory.episodes import _hash
            with store._connect() as con:
                con.execute(
                    "UPDATE corrections SET correction_hash=?, available_at=NULL WHERE correction_id=?",
                    (_hash(correction), correction_id),
                )

            reopened = ExperienceMemory(path)
            same_id, inserted_again = reopened.append_correction(
                episode,
                correction_id=correction_id,
                payload=correction,
            )
            self.assertEqual(same_id, correction_id)
            self.assertFalse(inserted_again)

    def test_correction_cannot_be_backdated_before_episode_decision(self):
        with TemporaryDirectory() as directory:
            store = ExperienceMemory(Path(directory) / "memory.sqlite3")
            episode, _ = store.append_episode(
                decision_time=BASE,
                information_cutoff=BASE,
                task="research",
                regime="calm",
                instrument_family="equity",
                permission_class="research",
                payload=payload("pending"),
            )
            with self.assertRaisesRegex(
                MemoryConflict,
                "cannot precede episode decision_time",
            ):
                store.append_correction(
                    episode,
                    available_at=BASE - timedelta(seconds=1),
                    payload={
                        "supersedes_fields": ["outcome"],
                        "outcome": {"label": "impossible-early-truth"},
                        "evidence_ref": "artifact:backdated",
                    },
                )

    def test_future_correction_is_not_visible_before_its_evidenced_availability(self):
        with TemporaryDirectory() as directory:
            store = ExperienceMemory(Path(directory) / "memory.sqlite3")
            episode, _ = store.append_episode(
                decision_time=BASE,
                information_cutoff=BASE,
                task="research",
                regime="calm",
                instrument_family="equity",
                permission_class="research",
                payload=payload("pending"),
            )
            correction_time = BASE + timedelta(days=2)
            store.append_correction(
                episode,
                available_at=correction_time,
                payload={
                    "supersedes_fields": ["outcome"],
                    "outcome": {"label": "reconciled-later"},
                    "evidence_ref": "artifact:later-correction",
                },
            )

            before = store.retrieve(
                information_cutoff=BASE + timedelta(days=1),
                granted_permissions={"research"},
            )
            self.assertEqual(before[0]["corrections"], [])

            at_availability = store.retrieve(
                information_cutoff=correction_time,
                granted_permissions={"research"},
            )
            self.assertEqual(
                at_availability[0]["corrections"][0]["outcome"]["label"],
                "reconciled-later",
            )

    def test_default_correction_availability_is_append_time_not_historical_episode_time(self):
        with TemporaryDirectory() as directory:
            store = ExperienceMemory(Path(directory) / "memory.sqlite3")
            episode, _ = store.append_episode(
                decision_time=BASE,
                information_cutoff=BASE,
                task="research",
                regime="calm",
                instrument_family="equity",
                permission_class="research",
                payload=payload("pending"),
            )
            store.append_correction(
                episode,
                payload={
                    "supersedes_fields": ["outcome"],
                    "outcome": {"label": "late-observation"},
                    "evidence_ref": "artifact:late",
                },
            )
            historical = store.retrieve(
                information_cutoff=BASE,
                granted_permissions={"research"},
            )
            self.assertEqual(historical[0]["corrections"], [])

    def test_tombstone_hides_episode_but_keeps_auditable_record(self):
        with TemporaryDirectory() as directory:
            store = ExperienceMemory(Path(directory) / "memory.sqlite3")
            episode, _ = store.append_episode(
                decision_time=BASE,
                information_cutoff=BASE,
                task="research",
                regime="calm",
                instrument_family="equity",
                permission_class="research",
                payload=payload(),
            )
            store.tombstone(episode, reason="source rights revoked")
            self.assertEqual(
                store.retrieve(information_cutoff=BASE, granted_permissions={"research"}),
                (),
            )
            audit = store.retrieve(
                information_cutoff=BASE,
                granted_permissions={"research"},
                include_tombstoned=True,
            )
            self.assertEqual(audit[0]["tombstones"][0]["reason"], "source rights revoked")

    def test_permission_filter_blocks_sensitive_episode(self):
        with TemporaryDirectory() as directory:
            store = ExperienceMemory(Path(directory) / "memory.sqlite3")
            store.append_episode(
                decision_time=BASE,
                information_cutoff=BASE,
                task="account-review",
                regime="calm",
                instrument_family="equity",
                permission_class="account-private",
                payload=payload(),
            )
            self.assertEqual(
                store.retrieve(information_cutoff=BASE, granted_permissions={"research"}),
                (),
            )
            allowed = store.retrieve(
                information_cutoff=BASE,
                granted_permissions={"account-private"},
            )
            self.assertEqual(len(allowed), 1)

    def test_information_cutoff_prevents_future_memory_leakage(self):
        with TemporaryDirectory() as directory:
            store = ExperienceMemory(Path(directory) / "memory.sqlite3")
            future = BASE + timedelta(days=1)
            store.append_episode(
                decision_time=future,
                information_cutoff=future,
                task="research",
                regime="calm",
                instrument_family="equity",
                permission_class="research",
                payload=payload(),
            )
            self.assertEqual(
                store.retrieve(information_cutoff=BASE, granted_permissions={"research"}),
                (),
            )

    def test_future_decision_with_old_information_cutoff_is_not_retrieved_early(self):
        with TemporaryDirectory() as directory:
            store = ExperienceMemory(Path(directory) / "memory.sqlite3")
            future = BASE + timedelta(days=2)
            store.append_episode(
                decision_time=future,
                information_cutoff=BASE,
                task="research",
                regime="calm",
                instrument_family="equity",
                permission_class="research",
                payload=payload(),
            )
            self.assertEqual(
                store.retrieve(
                    information_cutoff=BASE + timedelta(days=1),
                    granted_permissions={"research"},
                ),
                (),
            )

    def test_reopen_preserves_episode(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "memory.sqlite3"
            first = ExperienceMemory(path)
            episode, _ = first.append_episode(
                decision_time=BASE,
                information_cutoff=BASE,
                task="research",
                regime="calm",
                instrument_family="equity",
                permission_class="research",
                payload=payload(),
            )
            second = ExperienceMemory(path)
            self.assertEqual(second.source_episode(episode)["episode_id"], episode)


if __name__ == "__main__":
    unittest.main()
