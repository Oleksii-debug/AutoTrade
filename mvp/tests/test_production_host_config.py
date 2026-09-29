from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.production_host import (
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

    def test_exact_config_snapshot_parses_without_defaults_or_retargeting(self) -> None:
        with TemporaryDirectory() as directory:
            journal = Path(directory).resolve() / "journal.sqlite3"
            config = parse_production_host_config(self._payload(journal))

            self.assertEqual(config.journal_path, journal)
            self.assertEqual(config.account_id, "paper-account")
            self.assertEqual(config.environment, "PAPER")
            self.assertEqual(config.host_id, "host-a")
            self.assertEqual(config.bind_host, "127.0.0.1")
            self.assertEqual(config.bind_port, 8765)
            self.assertEqual(config.public_origin, "http://127.0.0.1:8765")

    def test_missing_required_field_fails_closed(self) -> None:
        with TemporaryDirectory() as directory:
            journal = Path(directory).resolve() / "journal.sqlite3"
            parsed = json.loads(self._payload(journal))
            del parsed["account_id"]

            with self.assertRaisesRegex(ValueError, "missing fields: account_id"):
                parse_production_host_config(
                    json.dumps(parsed, separators=(",", ":")).encode("utf-8")
                )

    def test_unknown_field_fails_closed(self) -> None:
        with TemporaryDirectory() as directory:
            journal = Path(directory).resolve() / "journal.sqlite3"
            with self.assertRaisesRegex(ValueError, "unknown fields: default_account"):
                parse_production_host_config(
                    self._payload(journal, default_account="live-account")
                )

    def test_duplicate_field_fails_closed(self) -> None:
        with TemporaryDirectory() as directory:
            journal = str(Path(directory).resolve() / "journal.sqlite3")
            payload = (
                "{"
                f'"journal_path":{json.dumps(journal)},'
                '"account_id":"paper-account",'
                '"account_id":"other-account",'
                '"environment":"PAPER",'
                '"host_id":"host-a",'
                '"bind_host":"127.0.0.1",'
                '"bind_port":8765,'
                '"public_origin":"http://127.0.0.1:8765"'
                "}"
            ).encode("utf-8")

            with self.assertRaisesRegex(ValueError, "duplicate .* account_id"):
                parse_production_host_config(payload)

    def test_non_object_and_non_utf8_inputs_fail_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be one JSON object"):
            parse_production_host_config(b"[]")
        with self.assertRaisesRegex(ValueError, "UTF-8 JSON"):
            parse_production_host_config(b"\xff")

    def test_relative_journal_and_boolean_port_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "journal_path must be absolute"):
            parse_production_host_config(
                self._payload(Path("state/journal.sqlite3"))
            )

        with TemporaryDirectory() as directory:
            journal = Path(directory).resolve() / "journal.sqlite3"
            with self.assertRaisesRegex(TypeError, "bind_port must be an integer"):
                parse_production_host_config(self._payload(journal, bind_port=True))

    def test_loader_requires_explicit_absolute_file_and_reads_valid_snapshot(self) -> None:
        with self.assertRaisesRegex(ValueError, "config path must be absolute"):
            load_production_host_config(Path("host.json"))

        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            journal = root / "journal.sqlite3"
            config_path = root / "host.json"
            config_path.write_bytes(self._payload(journal))

            loaded = load_production_host_config(config_path)
            self.assertEqual(loaded.journal_path, journal)
            self.assertEqual(loaded.account_id, "paper-account")
            self.assertEqual(loaded.environment, "PAPER")


if __name__ == "__main__":
    unittest.main()
