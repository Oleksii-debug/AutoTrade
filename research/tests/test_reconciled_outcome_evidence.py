from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import autotrade_research.memory.reconciled_outcome as reconciled_module
from autotrade_research.memory.episodes import ExperienceMemory, MemoryIntegrityError
from autotrade_research.memory.reconciled_outcome import (
    ReconciledOutcomeFactEvidence,
    resolve_reconciled_outcome_fact,
    reverify_reconciled_outcome_fact,
)


BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)
EPISODE_ID = "00000000-0000-0000-0000-000000000063"


def payload(label: str = "pending") -> dict:
    return {
        "evidence_refs": ["artifact:wp63-outcome"],
        "intended_action": {"side": "HOLD"},
        "actual_execution": {"fills": []},
        "outcome": {"label": label, "net_value_source": "reconciled-fact-only"},
        "costs": {"USD": "0"},
    }


def memory(path: Path) -> ExperienceMemory:
    return ExperienceMemory(
        path,
        correction_evidence_resolver=lambda ref: {
            "artifact:reconciled": BASE + timedelta(days=2),
            "artifact:later": BASE + timedelta(days=4),
        }[ref],
    )


class ReconciledOutcomeEvidenceTests(unittest.TestCase):
    def _append_episode(self, store: ExperienceMemory) -> str:
        with patch(
            "autotrade_research.memory.episodes._utc_now",
            return_value=BASE,
        ):
            episode_id, inserted = store.append_episode(
                episode_id=EPISODE_ID,
                decision_time=BASE,
                information_cutoff=BASE,
                task="wp63-ablation",
                regime="historical",
                instrument_family="equity",
                permission_class="research",
                payload=payload(),
            )
        self.assertTrue(inserted)
        return episode_id

    def _resolve(
        self,
        store: ExperienceMemory,
        *,
        cutoff: datetime,
    ) -> ReconciledOutcomeFactEvidence:
        return resolve_reconciled_outcome_fact(
            store,
            episode_id=EPISODE_ID,
            causal_cutoff=cutoff,
            granted_permissions={"research"},
            task="wp63-ablation",
            instrument_family="equity",
        )

    def test_module_uses_canonical_standalone_package(self):
        self.assertEqual(
            reconciled_module.__package__,
            "autotrade_research.memory",
        )

    def test_fact_binds_exact_population_cut_and_contains_no_numeric_utility(self):
        with TemporaryDirectory() as directory:
            store = memory(Path(directory) / "memory.sqlite3")
            self._append_episode(store)

            evidence = self._resolve(store, cutoff=BASE + timedelta(days=1))

            self.assertEqual(evidence.episode_id, EPISODE_ID)
            self.assertEqual(evidence.effective_outcome["label"], "pending")
            self.assertEqual(evidence.correction_lineage, ())
            self.assertEqual(evidence.permission_classes, ("research",))
            self.assertTrue(evidence.population_root_hash.startswith("sha256:"))
            self.assertTrue(evidence.evidence_digest.startswith("sha256:"))
            self.assertFalse(hasattr(evidence, "utility"))
            self.assertFalse(hasattr(evidence, "score"))
            ReconciledOutcomeFactEvidence.verify_integrity(evidence)

    def test_post_cut_correction_cannot_rewrite_frozen_fact(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "memory.sqlite3"
            store = memory(path)
            self._append_episode(store)
            frozen = self._resolve(store, cutoff=BASE + timedelta(days=1))

            with patch(
                "autotrade_research.memory.episodes._utc_now",
                return_value=BASE + timedelta(seconds=1),
            ):
                store.append_correction(
                    EPISODE_ID,
                    available_at=BASE + timedelta(days=2),
                    payload={
                        "supersedes_fields": ["outcome"],
                        "outcome": {
                            "label": "reconciled",
                            "net_value_source": "reconciled-fact-only",
                        },
                        "evidence_ref": "artifact:reconciled",
                    },
                )

            reopened = memory(path)
            reverified = reverify_reconciled_outcome_fact(reopened, frozen)
            self.assertEqual(reverified.effective_outcome["label"], "pending")
            self.assertEqual(reverified.evidence_digest, frozen.evidence_digest)

            after = self._resolve(reopened, cutoff=BASE + timedelta(days=3))
            self.assertEqual(after.effective_outcome["label"], "reconciled")
            self.assertEqual(len(after.correction_lineage), 1)
            self.assertNotEqual(after.population_root_hash, frozen.population_root_hash)
            self.assertNotEqual(after.evidence_digest, frozen.evidence_digest)

    def test_reverification_detects_in_memory_object_mutation(self):
        with TemporaryDirectory() as directory:
            store = memory(Path(directory) / "memory.sqlite3")
            self._append_episode(store)
            evidence = self._resolve(store, cutoff=BASE + timedelta(days=1))

            object.__setattr__(
                evidence,
                "effective_outcome",
                {"label": "forged", "net_value_source": "caller"},
            )
            with self.assertRaisesRegex(
                MemoryIntegrityError,
                "evidence digest does not match",
            ):
                reverify_reconciled_outcome_fact(store, evidence)

    def test_constructor_digest_is_not_authority(self):
        with self.assertRaisesRegex(
            MemoryIntegrityError,
            "evidence digest does not match",
        ):
            ReconciledOutcomeFactEvidence(
                causal_cutoff=(BASE + timedelta(days=1)).isoformat(),
                permission_classes=("research",),
                task="wp63-ablation",
                instrument_family="equity",
                population_root_hash="sha256:" + "1" * 64,
                episode_id=EPISODE_ID,
                episode_hash="sha256:" + "2" * 64,
                effective_outcome={"label": "caller"},
                correction_lineage=(),
                evidence_digest="sha256:" + "3" * 64,
            )

    def test_restart_reverification_requires_same_canonical_memory(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "memory.sqlite3"
            store = memory(path)
            self._append_episode(store)
            evidence = self._resolve(store, cutoff=BASE + timedelta(days=1))

            reopened = memory(path)
            self.assertEqual(
                reverify_reconciled_outcome_fact(reopened, evidence),
                evidence,
            )

            other_path = Path(directory) / "other.sqlite3"
            other = memory(other_path)
            with patch(
                "autotrade_research.memory.episodes._utc_now",
                return_value=BASE,
            ):
                other.append_episode(
                    episode_id=EPISODE_ID,
                    decision_time=BASE,
                    information_cutoff=BASE,
                    task="wp63-ablation",
                    regime="historical",
                    instrument_family="equity",
                    permission_class="research",
                    payload=payload("different"),
                )
            with self.assertRaisesRegex(
                MemoryIntegrityError,
                "does not match canonical ExperienceMemory",
            ):
                reverify_reconciled_outcome_fact(other, evidence)

    def test_tombstoned_episode_cannot_issue_usable_outcome_fact(self):
        with TemporaryDirectory() as directory:
            store = memory(Path(directory) / "memory.sqlite3")
            self._append_episode(store)
            with patch(
                "autotrade_research.memory.episodes._utc_now",
                return_value=BASE + timedelta(days=1),
            ):
                store.tombstone(EPISODE_ID, reason="superseded source")

            with self.assertRaisesRegex(
                MemoryIntegrityError,
                "tombstoned episode cannot issue",
            ):
                self._resolve(store, cutoff=BASE + timedelta(days=2))

    def test_wrong_query_identity_cannot_reverify_evidence(self):
        with TemporaryDirectory() as directory:
            store = memory(Path(directory) / "memory.sqlite3")
            self._append_episode(store)
            evidence = self._resolve(store, cutoff=BASE + timedelta(days=1))

            object.__setattr__(evidence, "task", "another-task")
            with self.assertRaisesRegex(
                MemoryIntegrityError,
                "evidence digest does not match",
            ):
                reverify_reconciled_outcome_fact(store, evidence)

    def test_memory_subclass_is_rejected_before_virtual_population_read(self):
        calls: list[str] = []

        class HostileMemory(ExperienceMemory):
            def coverage_population_snapshot(self, *args, **kwargs):
                calls.append("coverage")
                raise AssertionError("hostile memory callback executed")

        with TemporaryDirectory() as directory:
            hostile = HostileMemory(Path(directory) / "memory.sqlite3")
            with self.assertRaisesRegex(TypeError, "exact ExperienceMemory"):
                resolve_reconciled_outcome_fact(
                    hostile,
                    episode_id=EPISODE_ID,
                    causal_cutoff=BASE + timedelta(days=1),
                    granted_permissions={"research"},
                )
        self.assertEqual(calls, [])

    def test_instance_shadowed_population_reader_is_not_authority(self):
        calls: list[str] = []
        with TemporaryDirectory() as directory:
            store = memory(Path(directory) / "memory.sqlite3")
            self._append_episode(store)

            def hostile(*args, **kwargs):
                calls.append("coverage")
                raise AssertionError("instance shadow executed")

            store.coverage_population_snapshot = hostile
            with self.assertRaisesRegex(
                MemoryIntegrityError,
                "shadows canonical executables",
            ):
                self._resolve(store, cutoff=BASE + timedelta(days=1))
            self.assertEqual(calls, [])

    def test_non_utc_cut_is_rejected_without_normalization(self):
        with TemporaryDirectory() as directory:
            store = memory(Path(directory) / "memory.sqlite3")
            self._append_episode(store)
            non_utc = datetime(
                2026,
                1,
                2,
                tzinfo=timezone(timedelta(hours=1)),
            )
            with self.assertRaisesRegex(ValueError, "exact UTC timezone"):
                self._resolve(store, cutoff=non_utc)

    def test_hostile_episode_text_is_rejected_before_text_callbacks(self):
        calls: list[str] = []

        class HostileText(str):
            def strip(self, *args, **kwargs):
                calls.append("strip")
                raise AssertionError("hostile strip executed")

        with TemporaryDirectory() as directory:
            store = memory(Path(directory) / "memory.sqlite3")
            with self.assertRaisesRegex(
                MemoryIntegrityError,
                "episode_id must be exact built-in text",
            ):
                resolve_reconciled_outcome_fact(
                    store,
                    episode_id=HostileText(EPISODE_ID),
                    causal_cutoff=BASE + timedelta(days=1),
                    granted_permissions={"research"},
                )
        self.assertEqual(calls, [])

    def test_hostile_outcome_mapping_is_rejected_before_mapping_callbacks(self):
        calls: list[str] = []

        class HostileDict(dict):
            def items(self):
                calls.append("items")
                raise AssertionError("hostile mapping callback executed")

        with self.assertRaisesRegex(
            MemoryIntegrityError,
            "non-canonical JSON value",
        ):
            ReconciledOutcomeFactEvidence(
                causal_cutoff=(BASE + timedelta(days=1)).isoformat(),
                permission_classes=("research",),
                task="wp63-ablation",
                instrument_family="equity",
                population_root_hash="sha256:" + "1" * 64,
                episode_id=EPISODE_ID,
                episode_hash="sha256:" + "2" * 64,
                effective_outcome=HostileDict(label="caller"),
                correction_lineage=(),
                evidence_digest="sha256:" + "3" * 64,
            )
        self.assertEqual(calls, [])

    def test_reverification_ignores_instance_shadowed_integrity_method(self):
        calls: list[str] = []
        with TemporaryDirectory() as directory:
            store = memory(Path(directory) / "memory.sqlite3")
            self._append_episode(store)
            evidence = self._resolve(store, cutoff=BASE + timedelta(days=1))

            def hostile():
                calls.append("verify")
                raise AssertionError("instance integrity shadow executed")

            object.__setattr__(evidence, "verify_integrity", hostile)
            self.assertEqual(
                reverify_reconciled_outcome_fact(store, evidence).episode_id,
                EPISODE_ID,
            )
            self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
