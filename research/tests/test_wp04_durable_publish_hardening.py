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
    atomic_write_bytes,
    atomic_write_bytes_with_sha256_sidecar,
    atomic_write_json,
    atomic_write_stream,
    atomic_write_stream_with_sha256_sidecar,
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

    def test_atomic_write_bytes_preserves_exact_binary_payload(self):
        with TemporaryDirectory() as directory:
            destination = Path(directory) / "artifact.bin"
            payload = bytes(range(256)) + b"\x00\xffAutoTrade"
            atomic_write_bytes(destination, payload)

            self.assertEqual(destination.read_bytes(), payload)
            self.assertEqual(
                list(destination.parent.glob(f".{destination.name}.*.tmp")),
                [],
            )

    def test_atomic_write_stream_failure_preserves_destination_and_removes_temp(self):
        with TemporaryDirectory() as directory:
            destination = Path(directory) / "artifact.bin"
            destination.write_bytes(b"stable")

            def fail_after_partial_write(handle):
                handle.write(b"partial")
                raise RuntimeError("simulated stream writer failure")

            with self.assertRaisesRegex(
                RuntimeError,
                "simulated stream writer failure",
            ):
                atomic_write_stream(destination, fail_after_partial_write)

            self.assertEqual(destination.read_bytes(), b"stable")
            self.assertEqual(
                list(destination.parent.glob(f".{destination.name}.*.tmp")),
                [],
            )

    def test_sha256_pair_restores_old_sidecar_when_primary_replace_fails(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            destination = root / "artifact.bin"
            sidecar = root / "artifact.bin.sha256"
            destination.write_bytes(b"old-primary")
            sidecar.write_bytes(b"old-digest\n")
            original_replace = durable_publish.os.replace
            injected = False

            def replace_with_primary_failure(source, target):
                nonlocal injected
                if Path(target) == destination and not injected:
                    injected = True
                    raise OSError("simulated primary replace failure")
                return original_replace(source, target)

            with patch.object(
                durable_publish.os,
                "replace",
                side_effect=replace_with_primary_failure,
            ):
                with self.assertRaisesRegex(
                    OSError,
                    "simulated primary replace failure",
                ):
                    atomic_write_bytes_with_sha256_sidecar(
                        destination,
                        sidecar,
                        b"new-primary",
                    )

            self.assertTrue(injected)
            self.assertEqual(destination.read_bytes(), b"old-primary")
            self.assertEqual(sidecar.read_bytes(), b"old-digest\n")
            self.assertEqual(
                list(root.glob(".*.tmp")),
                [],
            )

    def test_sha256_pair_hashes_owned_staged_bytes_before_publication(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            destination = root / "artifact.bin"
            sidecar = root / "artifact.bin.sha256"
            destination.write_bytes(b"old-primary")
            expected = b"canonical-produced-bytes"
            original_replace = durable_publish.os.replace
            interfered = False

            def replace_with_noncooperating_interference(source, target):
                nonlocal interfered
                result = original_replace(source, target)
                if Path(target) == sidecar and not interfered:
                    destination.write_bytes(b"noncooperating-replacement")
                    interfered = True
                return result

            with patch.object(
                durable_publish.os,
                "replace",
                side_effect=replace_with_noncooperating_interference,
            ):
                digest = atomic_write_stream_with_sha256_sidecar(
                    destination,
                    sidecar,
                    lambda handle: handle.write(expected),
                )

            self.assertTrue(interfered)
            self.assertEqual(destination.read_bytes(), expected)
            self.assertEqual(
                sidecar.read_text(encoding="utf-8"),
                f"{digest}  {destination.name}\n",
            )
            self.assertEqual(
                digest,
                __import__("hashlib").sha256(expected).hexdigest(),
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

    def test_final_destination_symlink_is_rejected_without_touching_target(self):
        if not hasattr(os, "symlink"):
            self.skipTest("symlinks unavailable on this platform")
        with TemporaryDirectory() as directory:
            root = Path(directory)
            external_target = root / "external.json"
            external_target.write_text('{"external":true}\n', encoding="utf-8")
            destination = root / "artifact.json"
            try:
                os.symlink(external_target, destination)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"symlink creation unavailable on this platform: {exc}")

            with self.assertRaisesRegex(
                DurablePublishLockError,
                "regular non-symlink",
            ):
                atomic_write_json(destination, {"replacement": True})

            self.assertEqual(
                json.loads(external_target.read_text(encoding="utf-8")),
                {"external": True},
            )
            self.assertTrue(destination.is_symlink())

    def test_final_destination_hardlink_is_rejected_without_touching_alias(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            external_target = root / "external.json"
            external_target.write_text('{"external":true}\n', encoding="utf-8")
            destination = root / "artifact.json"
            try:
                os.link(external_target, destination)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"hard links unavailable on this platform: {exc}")

            with self.assertRaisesRegex(
                DurablePublishLockError,
                "hard-link aliases",
            ):
                atomic_write_json(destination, {"replacement": True})

            self.assertEqual(
                json.loads(external_target.read_text(encoding="utf-8")),
                {"external": True},
            )
            self.assertEqual(
                json.loads(destination.read_text(encoding="utf-8")),
                {"external": True},
            )

    def test_parent_directory_alias_uses_same_canonical_sidecar_identity(self):
        if not hasattr(os, "symlink"):
            self.skipTest("symlinks unavailable on this platform")
        with TemporaryDirectory() as directory:
            root = Path(directory)
            real_parent = root / "real"
            real_parent.mkdir()
            alias_parent = root / "alias"
            try:
                os.symlink(real_parent, alias_parent, target_is_directory=True)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"directory symlink unavailable on this platform: {exc}")

            real_destination = real_parent / "artifact.json"
            alias_destination = alias_parent / "artifact.json"
            with durable_path_lock(real_destination):
                with durable_path_lock(alias_destination):
                    self.assertFalse(real_destination.exists())

            self.assertTrue((real_parent / ".artifact.json.lock").exists())
            self.assertFalse((root / ".artifact.json.lock").exists())

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
