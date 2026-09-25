import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch

from autotrade_research.artifacts import durable_publish
from autotrade_research.artifacts.durable_publish import (
    DurablePublishLockError,
    atomic_write_json,
    durable_path_lock,
)


class DurablePublishHardeningTests(unittest.TestCase):
    def test_serialization_failure_preserves_destination_and_removes_temp(self):
        with TemporaryDirectory() as directory:
            destination = Path(directory) / "artifact.json"
            atomic_write_json(destination, {"stable": True})
            stable_bytes = destination.read_bytes()

            with self.assertRaises(TypeError):
                atomic_write_json(destination, {"invalid": object()})

            self.assertEqual(destination.read_bytes(), stable_bytes)
            self.assertEqual(
                list(destination.parent.glob(f".{destination.name}.*.tmp")),
                [],
            )

    def test_nonfinite_json_never_reaches_replace(self):
        with TemporaryDirectory() as directory:
            destination = Path(directory) / "artifact.json"
            atomic_write_json(destination, {"stable": True})
            stable_bytes = destination.read_bytes()

            for invalid in (float("nan"), float("inf"), float("-inf")):
                with self.subTest(value=repr(invalid)):
                    with patch.object(
                        durable_publish.os,
                        "replace",
                        wraps=durable_publish.os.replace,
                    ) as replace:
                        with self.assertRaises(ValueError):
                            atomic_write_json(
                                destination,
                                {"nested": {"invalid": invalid}},
                            )
                        replace.assert_not_called()
                    self.assertEqual(destination.read_bytes(), stable_bytes)
                    self.assertEqual(
                        list(destination.parent.glob(f".{destination.name}.*.tmp")),
                        [],
                    )

    def test_dangling_sidecar_symlink_is_rejected_without_creating_target(self):
        if not hasattr(os, "symlink"):
            self.skipTest("symlinks unavailable on this platform")
        with TemporaryDirectory() as directory:
            root = Path(directory)
            destination = root / "artifact.json"
            destination.write_text('{"stable":true}\n', encoding="utf-8")
            lock_path = root / ".artifact.json.lock"
            external_target = root / "outside.lock"
            try:
                os.symlink(external_target, lock_path)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"symlink creation unavailable on this platform: {exc}")

            with self.assertRaises(DurablePublishLockError):
                atomic_write_json(destination, {"new": True})

            self.assertFalse(
                external_target.exists(),
                "publication lock followed dangling alias and created target",
            )
            self.assertEqual(
                json.loads(destination.read_text(encoding="utf-8")),
                {"stable": True},
            )

    def test_nested_same_thread_lock_is_reentrant(self):
        with TemporaryDirectory() as directory:
            destination = Path(directory) / "artifact.json"
            with durable_path_lock(destination):
                with durable_path_lock(destination):
                    self.assertFalse(destination.exists())

    def test_primary_body_error_survives_release_failure(self):
        with TemporaryDirectory() as directory:
            destination = Path(directory) / "artifact.json"
            with patch(
                "autotrade_research.artifacts.resource_lock.ResourceLock._unlock_handle",
                side_effect=OSError("simulated unlock failure"),
            ):
                with self.assertRaisesRegex(ValueError, "primary publication failure") as caught:
                    with durable_path_lock(destination):
                        raise ValueError("primary publication failure")

            notes = getattr(caught.exception, "__notes__", ())
            self.assertTrue(
                any(
                    "publication lock release also failed" in note
                    and "simulated unlock failure" in note
                    for note in notes
                ),
                f"release evidence missing from primary exception notes: {notes!r}",
            )

    def test_concurrent_publishers_expose_only_complete_payload(self):
        with TemporaryDirectory() as directory:
            destination = Path(directory) / "artifact.json"
            payloads = [
                {"writer": "a", "items": list(range(40))},
                {"writer": "b", "items": list(range(40, 80))},
                {"writer": "c", "items": list(range(80, 120))},
            ]
            errors = []

            def writer(payload):
                try:
                    atomic_write_json(destination, payload)
                except BaseException as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=writer, args=(payload,)) for payload in payloads]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(10)

            self.assertTrue(all(not thread.is_alive() for thread in threads))
            self.assertEqual(errors, [])
            self.assertIn(
                json.loads(destination.read_text(encoding="utf-8")),
                payloads,
            )
            self.assertEqual(
                list(destination.parent.glob(f".{destination.name}.*.tmp")),
                [],
            )


if __name__ == "__main__":
    unittest.main()
