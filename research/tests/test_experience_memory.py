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
