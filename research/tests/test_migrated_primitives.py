import json
import multiprocessing
import tempfile
import unittest
from pathlib import Path

from autotrade_research.io.strict_json import (
    DuplicateJsonKeyError,
    InvalidJsonDomainError,
    NonStandardJsonConstantError,
    jsonl_bytes_are_blank,
    strict_json_loads,
)
from autotrade_research.artifacts.durable_publish import atomic_write_json, sha256_file
from autotrade_research.artifacts.resource_lock import ResourceLock, ResourceLockBusyError


def hold_lock(path: str, ready, release):
    with ResourceLock(path):
        ready.set()
        release.wait(10)


class MigrationTests(unittest.TestCase):
    def test_strict_json_rejects_duplicate(self):
        with self.assertRaises(DuplicateJsonKeyError):
            strict_json_loads('{"a":1,"a":2}')

    def test_strict_json_rejects_nonstandard(self):
        with self.assertRaises(NonStandardJsonConstantError):
            strict_json_loads('{"a":NaN}')

    def test_strict_json_rejects_huge_integer(self):
        with self.assertRaises(InvalidJsonDomainError):
            strict_json_loads('{"a":' + ('1' * 641) + '}')

    def test_strict_json_rejects_oversized_document_and_decoded_domain(self):
        with self.assertRaisesRegex(InvalidJsonDomainError, "document exceeds"):
            strict_json_loads('{"value":"' + ("x" * 1_000_000) + '"}')

        many_nodes = "[" + ",".join("0" for _ in range(100_001)) + "]"
        with self.assertRaisesRegex(InvalidJsonDomainError, "decoded domain exceeds"):
            strict_json_loads(many_nodes)

    def test_blank_jsonl_is_json_whitespace_only(self):
        self.assertTrue(jsonl_bytes_are_blank(b" \t\r\n"))
        self.assertFalse(jsonl_bytes_are_blank("\u00a0".encode()))

    def test_atomic_json_is_stable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.json"
            atomic_write_json(path, {"z": 2, "a": 1})
            self.assertEqual(json.loads(path.read_text()), {"a": 1, "z": 2})
            digest = sha256_file(path)
            atomic_write_json(path, {"a": 1, "z": 2})
            self.assertEqual(digest, sha256_file(path))

    def test_resource_lock_contends(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / ".lock")
            context = multiprocessing.get_context("spawn")
            ready = context.Event()
            release = context.Event()
            process = context.Process(target=hold_lock, args=(path, ready, release))
            process.start()
            self.assertTrue(ready.wait(10))
            try:
                with self.assertRaises(ResourceLockBusyError):
                    with ResourceLock(path):
                        pass
            finally:
                release.set()
                process.join(10)
            self.assertEqual(process.exitcode, 0)


if __name__ == "__main__":
    unittest.main()
