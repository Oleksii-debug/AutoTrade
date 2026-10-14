from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from research.autotrade_research.memory.episodes import ExperienceMemory


BASE = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)


def _payload():
    return {
        "evidence_refs": ["artifact:permission-scope"],
        "intended_action": {"side": "HOLD"},
        "actual_execution": {"fills": []},
        "outcome": {"label": "flat"},
        "costs": {"USD": "0"},
    }


class PermissionSnapshotAuthorityTests(unittest.TestCase):
    def _store_with_private_episode(self, path: Path):
        store = ExperienceMemory(path)
        with patch(
            "research.autotrade_research.memory.episodes._utc_now",
            return_value=BASE,
        ):
            episode_id, inserted = store.append_episode(
                decision_time=BASE,
                information_cutoff=BASE,
                task="research",
                regime="calm",
                instrument_family="equity",
                permission_class="account-private",
                payload=_payload(),
            )
        self.assertTrue(inserted)
        return store, episode_id

    def test_population_snapshot_rejects_polymorphic_set_before_iteration(self):
        class AlternatingPermissions(set):
            def __init__(self):
                super().__init__({"research"})
                self.iter_calls = 0

            def __iter__(self):
                self.iter_calls += 1
                if self.iter_calls == 1:
                    return iter(("account-private",))
                return iter(("research",))

        with TemporaryDirectory() as directory:
            store, _episode_id = self._store_with_private_episode(
                Path(directory) / "memory.sqlite3"
            )
            permissions = AlternatingPermissions()

            with self.assertRaisesRegex(
                TypeError,
                "exact built-in set",
            ):
                store.coverage_population_snapshot(
                    causal_cutoff=BASE + timedelta(seconds=1),
                    granted_permissions=permissions,
                    task="research",
                    instrument_family="equity",
                )

            self.assertEqual(
                permissions.iter_calls,
                0,
                "hostile permission iteration must not execute before rejection",
            )

    def test_all_memory_read_boundaries_reject_set_subclass_without_iteration(self):
        class HostilePermissions(set):
            def __init__(self):
                super().__init__({"account-private"})
                self.iter_calls = 0

            def __iter__(self):
                self.iter_calls += 1
                raise AssertionError("set subclass iteration must not execute")

        with TemporaryDirectory() as directory:
            store, episode_id = self._store_with_private_episode(
                Path(directory) / "memory.sqlite3"
            )
            calls = (
                lambda p: store.retrieve(
                    information_cutoff=BASE + timedelta(seconds=1),
                    granted_permissions=p,
                ),
                lambda p: store.source_episode(
                    episode_id,
                    information_cutoff=BASE + timedelta(seconds=1),
                    granted_permissions=p,
                ),
                lambda p: store.coverage_population(
                    causal_cutoff=BASE + timedelta(seconds=1),
                    granted_permissions=p,
                ),
                lambda p: store.coverage_population_snapshot(
                    causal_cutoff=BASE + timedelta(seconds=1),
                    granted_permissions=p,
                ),
            )

            for call in calls:
                permissions = HostilePermissions()
                with self.subTest(call=call), self.assertRaisesRegex(
                    TypeError,
                    "exact built-in set",
                ):
                    call(permissions)
                self.assertEqual(permissions.iter_calls, 0)

    def test_permission_string_subclass_is_rejected_before_strip_callback(self):
        class HostilePermission(str):
            strip_calls = 0

            def strip(self, *_args, **_kwargs):
                type(self).strip_calls += 1
                raise AssertionError("polymorphic strip must not execute")

        with TemporaryDirectory() as directory:
            store, episode_id = self._store_with_private_episode(
                Path(directory) / "memory.sqlite3"
            )
            permission = HostilePermission("account-private")
            calls = (
                lambda: store.retrieve(
                    information_cutoff=BASE + timedelta(seconds=1),
                    granted_permissions={permission},
                ),
                lambda: store.source_episode(
                    episode_id,
                    information_cutoff=BASE + timedelta(seconds=1),
                    granted_permissions={permission},
                ),
                lambda: store.coverage_population(
                    causal_cutoff=BASE + timedelta(seconds=1),
                    granted_permissions={permission},
                ),
                lambda: store.coverage_population_snapshot(
                    causal_cutoff=BASE + timedelta(seconds=1),
                    granted_permissions={permission},
                ),
            )

            for call in calls:
                with self.subTest(call=call), self.assertRaisesRegex(
                    TypeError,
                    "exact built-in text",
                ):
                    call()
            self.assertEqual(HostilePermission.strip_calls, 0)

    def test_exact_permission_scope_is_bound_to_selected_private_population(self):
        with TemporaryDirectory() as directory:
            store, episode_id = self._store_with_private_episode(
                Path(directory) / "memory.sqlite3"
            )

            snapshot = store.coverage_population_snapshot(
                causal_cutoff=BASE + timedelta(seconds=1),
                granted_permissions={"account-private"},
                task="research",
                instrument_family="equity",
            )

            self.assertEqual(snapshot.permission_classes, ("account-private",))
            self.assertEqual(snapshot.eligible_count, 1)
            self.assertEqual(snapshot.rows[0]["episode_id"], episode_id)
            snapshot.verify_integrity()


if __name__ == "__main__":
    unittest.main()
