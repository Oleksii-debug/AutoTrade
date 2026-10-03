from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp.runtime_load_campaign import _sha256_identity
from mvp.autotrade_mvp.runtime_target_host_inventory import (
    RuntimeTargetHostInventory,
    RuntimeTargetHostInventoryError,
    collect_runtime_target_host_inventory,
    host_identity_fingerprint,
)


IDENTITY = {
    "system": "Windows",
    "release": "11",
    "machine": "AMD64",
    "python_implementation": "CPython",
    "python_version": "3.12.11",
    "cpu_count": 8,
}


class RuntimeTargetHostInventoryTests(unittest.TestCase):
    def test_fingerprint_matches_existing_wp65_campaign_identity_semantics(self) -> None:
        self.assertEqual(
            host_identity_fingerprint(IDENTITY),
            _sha256_identity(IDENTITY),
        )

    def test_collects_predeclared_host_and_round_trips_canonical_bytes(self) -> None:
        expected = host_identity_fingerprint(IDENTITY)
        with patch(
            "mvp.autotrade_mvp.runtime_target_host_inventory.capture_runtime_host_identity",
            return_value=dict(IDENTITY),
        ):
            inventory = collect_runtime_target_host_inventory(
                expected_host_fingerprint=expected,
            )

        raw = inventory.canonical_bytes()
        parsed = RuntimeTargetHostInventory.parse(raw)
        self.assertEqual(dict(parsed.host_identity), IDENTITY)
        self.assertEqual(parsed.host_fingerprint, expected)
        self.assertEqual(parsed.canonical_bytes(), raw)
        self.assertTrue(parsed.payload_sha256.startswith("sha256:"))
        self.assertEqual(len(parsed.payload_sha256), 71)

    def test_collector_rejects_different_predeclared_host(self) -> None:
        wrong = host_identity_fingerprint({**IDENTITY, "cpu_count": 16})
        with patch(
            "mvp.autotrade_mvp.runtime_target_host_inventory.capture_runtime_host_identity",
            return_value=dict(IDENTITY),
        ):
            with self.assertRaisesRegex(
                RuntimeTargetHostInventoryError,
                "does not match predeclared host fingerprint",
            ):
                collect_runtime_target_host_inventory(
                    expected_host_fingerprint=wrong,
                )

    def test_host_identity_schema_is_closed_and_exact_typed(self) -> None:
        with self.assertRaisesRegex(
            RuntimeTargetHostInventoryError,
            "fields do not match",
        ):
            host_identity_fingerprint({**IDENTITY, "hostname": "mutable-name"})

        with self.assertRaisesRegex(
            RuntimeTargetHostInventoryError,
            "cpu_count must be a positive integer",
        ):
            host_identity_fingerprint({**IDENTITY, "cpu_count": True})

        with self.assertRaisesRegex(
            RuntimeTargetHostInventoryError,
            "canonical non-empty text",
        ):
            host_identity_fingerprint({**IDENTITY, "machine": ""})

    def test_parser_rejects_duplicate_keys_and_noncanonical_json(self) -> None:
        expected = host_identity_fingerprint(IDENTITY)
        inventory = RuntimeTargetHostInventory(
            host_identity=IDENTITY,
            host_fingerprint=expected,
        )
        payload = inventory.canonical_payload()
        noncanonical = json.dumps(payload, sort_keys=False).encode("utf-8")
        self.assertNotEqual(noncanonical, inventory.canonical_bytes())
        with self.assertRaisesRegex(
            RuntimeTargetHostInventoryError,
            "bytes are not canonical JSON",
        ):
            RuntimeTargetHostInventory.parse(noncanonical)

        duplicate = (
            b'{"collector_id":"autotrade-runtime-target-host-inventory",'
            b'"collector_id":"autotrade-runtime-target-host-inventory",'
            b'"collector_version":"1.0.0","evidence_type":'
            b'"AUTOTRADE_RUNTIME_TARGET_HOST_INVENTORY",'
            b'"host_fingerprint":"' + expected.encode("ascii") + b'",'
            b'"host_identity":{"cpu_count":8,"machine":"AMD64",'
            b'"python_implementation":"CPython","python_version":"3.12.11",'
            b'"release":"11","system":"Windows"},'
            b'"schema_version":"1.0.0"}'
        )
        with self.assertRaisesRegex(
            RuntimeTargetHostInventoryError,
            "duplicate JSON key",
        ):
            RuntimeTargetHostInventory.parse(duplicate)

    def test_parser_rejects_fingerprint_substitution(self) -> None:
        inventory = RuntimeTargetHostInventory(
            host_identity=IDENTITY,
            host_fingerprint=host_identity_fingerprint(IDENTITY),
        )
        payload = inventory.canonical_payload()
        payload["host_fingerprint"] = "sha256:" + ("0" * 64)
        tampered = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        with self.assertRaisesRegex(
            RuntimeTargetHostInventoryError,
            "fingerprint does not match captured identity",
        ):
            RuntimeTargetHostInventory.parse(tampered)


if __name__ == "__main__":
    unittest.main()
