from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp.production_host import (
    _MAX_CONFIG_BYTES,
    load_production_host_config,
    parse_production_host_config,
)


class ProductionHostConfigParsingTests(unittest.TestCase):
    @staticmethod
    def _payload(journal_path: Path, **overrides: object) -> bytes:
        config: dict[str, object] = {
            "journal_path": str(journal_path),
            "account_id": "paper-account",
            "environment": "PAPER",
            "host_id": "host-a",
            "bind_host": "127.0.0.1",
            "bind_port": 8765,
            "public_origin": "http://127.0.0.1:8765",
        }
        config.update(overrides)
        return json.dumps(config, separators=(",", ":")).encode("utf-8")

    def test_exact_config_snapshot_parses_without_defaults_or_retargeting(self):
        with TemporaryDirectory() as directory:
            journal = Path(directory).resolve() / "journal.sqlite3"
            config = parse_production_host_config(self._payload(journal))
            self.assertEqual(config.journal_path, journal)
            self.assertEqual(config.account_id, "paper-account")
            self.assertEqual(config.environment, "PAPER")
            self.assertEqual(config.host_id, "host-a")
            self.assertEqual(config.bind_port, 8765)

    def test_missing_unknown_duplicate_non_object_and_non_utf8_fail_closed(self):
        with TemporaryDirectory() as directory:
            journal = Path(directory).resolve() / "journal.sqlite3"
            parsed = json.loads(self._payload(journal))
            del parsed["account_id"]
            with self.assertRaisesRegex(ValueError, "missing fields: account_id"):
                parse_production_host_config(json.dumps(parsed).encode())
            with self.assertRaisesRegex(ValueError, "unknown fields: default_account"):
                parse_production_host_config(self._payload(journal, default_account="x"))
            duplicate = self._payload(journal).decode().replace(
                '"account_id":"paper-account"',
                '"account_id":"paper-account","account_id":"other"',
            ).encode()
            with self.assertRaisesRegex(ValueError, "duplicate .* account_id"):
                parse_production_host_config(duplicate)
        with self.assertRaisesRegex(ValueError, "one JSON object"):
            parse_production_host_config(b"[]")
        with self.assertRaisesRegex(ValueError, "UTF-8 JSON"):
            parse_production_host_config(b"\xff")

    def test_non_finite_and_excessive_depth_fail_closed(self):
        with TemporaryDirectory() as directory:
            journal = Path(directory).resolve() / "journal.sqlite3"
            payload = self._payload(journal).decode().replace("8765", "NaN", 1).encode()
            with self.assertRaisesRegex(ValueError, "non-finite"):
                parse_production_host_config(payload)
            deep_value = "x"
            for _ in range(20):
                deep_value = [deep_value]
            with self.assertRaisesRegex(ValueError, "maximum JSON depth"):
                parse_production_host_config(self._payload(journal, account_id=deep_value))

    def test_relative_journal_and_boolean_port_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "journal_path must be absolute"):
            parse_production_host_config(self._payload(Path("state/journal.sqlite3")))
        with TemporaryDirectory() as directory:
            journal = Path(directory).resolve() / "journal.sqlite3"
            with self.assertRaisesRegex(TypeError, "bind_port must be an integer"):
                parse_production_host_config(self._payload(journal, bind_port=True))

    def test_loader_reads_only_limit_plus_one_and_enforces_exact_size_cap(self):
        with self.assertRaisesRegex(ValueError, "config path must be absolute"):
            load_production_host_config(Path("host.json"))

        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            journal = root / "journal.sqlite3"
            config_path = root / "host.json"
            payload = self._payload(journal)
            exact = payload + b" " * (_MAX_CONFIG_BYTES - len(payload))
            config_path.write_bytes(exact)
            loaded = load_production_host_config(config_path)
            self.assertEqual(loaded.journal_path, journal)

            config_path.write_bytes(exact + b" ")
            with self.assertRaisesRegex(ValueError, "maximum size"):
                load_production_host_config(config_path)

    def test_loader_never_materializes_whole_file(self):
        class BoundedReader:
            def __init__(self):
                self.requested = None

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def read(self, size):
                self.requested = size
                return b"x" * size

        reader = BoundedReader()
        path = Path("/synthetic/host.json")
        with (
            patch.object(Path, "resolve", return_value=path),
            patch.object(Path, "is_file", return_value=True),
            patch.object(Path, "open", return_value=reader),
            patch.object(Path, "read_bytes", side_effect=AssertionError("whole-file read forbidden")),
        ):
            with self.assertRaisesRegex(ValueError, "maximum size"):
                load_production_host_config(path)
        self.assertEqual(reader.requested, _MAX_CONFIG_BYTES + 1)


if __name__ == "__main__":
    unittest.main()
