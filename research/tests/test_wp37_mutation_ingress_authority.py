from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from research.autotrade_research.memory.episodes import ExperienceMemory


BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def memory(path: Path) -> ExperienceMemory:
    return ExperienceMemory(
        path,
        correction_evidence_resolver=lambda _ref: BASE,
    )


def payload():
    return {
        "evidence_refs": ["artifact:abc"],
        "intended_action": {"side": "HOLD"},
        "actual_execution": {"fills": []},
        "outcome": {"label": "flat"},
        "costs": {"USD": "0"},
    }


class TrapText(str):
    strip_calls = 0

    def strip(self, *args, **kwargs):
        type(self).strip_calls += 1
        raise AssertionError("caller text callback executed")


class TrapDateTime(datetime):
    astimezone_calls = 0

    def astimezone(self, *args, **kwargs):
        type(self).astimezone_calls += 1
        raise AssertionError("caller datetime callback executed")


class TrapDict(dict):
    items_calls = 0

    def items(self):
        type(self).items_calls += 1
        raise AssertionError("caller mapping callback executed")


class TrapList(list):
    iter_calls = 0

    def __iter__(self):
        type(self).iter_calls += 1
        raise AssertionError("caller list callback executed")


class MutationIngressAuthorityTests(unittest.TestCase):
    def setUp(self):
        TrapText.strip_calls = 0
        TrapDateTime.astimezone_calls = 0
        TrapDict.items_calls = 0
        TrapList.iter_calls = 0

    def test_executable_scalar_subclasses_fail_before_callbacks(self):
        with TemporaryDirectory() as directory:
            store = memory(Path(directory) / "memory.sqlite3")
            hostile_time = TrapDateTime(
                2026, 1, 1, tzinfo=timezone.utc
            )
            with self.assertRaisesRegex(TypeError, "exact built-in datetime"):
                store.append_episode(
                    decision_time=hostile_time,
                    information_cutoff=BASE,
                    task="research",
                    regime="calm",
                    instrument_family="equity",
                    permission_class="research",
                    payload=payload(),
                )
            self.assertEqual(TrapDateTime.astimezone_calls, 0)

            with self.assertRaisesRegex(TypeError, "exact built-in text"):
                store.append_episode(
                    decision_time=BASE,
                    information_cutoff=BASE,
                    task=TrapText("research"),
                    regime="calm",
                    instrument_family="equity",
                    permission_class="research",
                    payload=payload(),
                )
            self.assertEqual(TrapText.strip_calls, 0)

    def test_top_level_mapping_subclass_is_rejected_before_iteration(self):
        with TemporaryDirectory() as directory:
            store = memory(Path(directory) / "memory.sqlite3")
            hostile = TrapDict(payload())
            with self.assertRaisesRegex(TypeError, "exact built-in object"):
                store.append_episode(
                    decision_time=BASE,
                    information_cutoff=BASE,
                    task="research",
                    regime="calm",
                    instrument_family="equity",
                    permission_class="research",
                    payload=hostile,
                )
            self.assertEqual(TrapDict.items_calls, 0)

    def test_nested_executable_containers_fail_before_virtual_iteration(self):
        with TemporaryDirectory() as directory:
            store = memory(Path(directory) / "memory.sqlite3")
            nested_mapping = payload()
            nested_mapping["outcome"] = TrapDict({"label": "flat"})
            with self.assertRaisesRegex(TypeError, "unsupported executable"):
                store.append_episode(
                    decision_time=BASE,
                    information_cutoff=BASE,
                    task="research",
                    regime="calm",
                    instrument_family="equity",
                    permission_class="research",
                    payload=nested_mapping,
                )
            self.assertEqual(TrapDict.items_calls, 0)

            nested_list = payload()
            nested_list["evidence_refs"] = TrapList(["artifact:abc"])
            with self.assertRaisesRegex(TypeError, "unsupported executable"):
                store.append_episode(
                    decision_time=BASE,
                    information_cutoff=BASE,
                    task="research",
                    regime="calm",
                    instrument_family="equity",
                    permission_class="research",
                    payload=nested_list,
                )
            self.assertEqual(TrapList.iter_calls, 0)

    def test_correction_payload_is_detached_by_the_same_boundary(self):
        with TemporaryDirectory() as directory:
            store = memory(Path(directory) / "memory.sqlite3")
            episode_id, inserted = store.append_episode(
                decision_time=BASE,
                information_cutoff=BASE,
                task="research",
                regime="calm",
                instrument_family="equity",
                permission_class="research",
                payload=payload(),
            )
            self.assertTrue(inserted)

            hostile = {
                "supersedes_fields": ["outcome"],
                "outcome": TrapDict({"label": "corrected"}),
                "evidence_ref": "artifact:correction",
            }
            with self.assertRaisesRegex(TypeError, "unsupported executable"):
                store.append_correction(episode_id, payload=hostile)
            self.assertEqual(TrapDict.items_calls, 0)

    def test_builtin_payload_still_round_trips_canonically(self):
        with TemporaryDirectory() as directory:
            store = memory(Path(directory) / "memory.sqlite3")
            episode_id, inserted = store.append_episode(
                decision_time=BASE,
                information_cutoff=BASE,
                task=" research ",
                regime=" calm ",
                instrument_family=" equity ",
                permission_class=" research ",
                payload=payload(),
            )
            self.assertTrue(inserted)
            rows = store.retrieve(
                information_cutoff=datetime(2030, 1, 1, tzinfo=timezone.utc),
                granted_permissions={"research"},
            )
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["episode_id"], episode_id)
            self.assertEqual(rows[0]["payload"]["outcome"]["label"], "flat")


if __name__ == "__main__":
    unittest.main()
