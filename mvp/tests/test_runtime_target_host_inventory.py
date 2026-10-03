from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from autotrade_runtime.artifacts import ArtifactStore
from mvp.autotrade_mvp.runtime_load_campaign import _sha256_identity
from mvp.autotrade_mvp.runtime_target_host_inventory import (
    COLLECTOR_ID,
    COLLECTOR_VERSION,
    RAW_EVIDENCE_KIND,
    RuntimeTargetHostInventory,
    RuntimeTargetHostInventoryError,
    collect_runtime_target_host_inventory,
    host_identity_fingerprint,
    publish_runtime_target_host_inventory,
)


IDENTITY = {
    "system": "Windows",
    "release": "11",
    "machine": "AMD64",
    "python_implementation": "CPython",
    "python_version": "3.12.11",
    "cpu_count": 8,
}
SOURCE_SHA = "a" * 40
ARTIFACT_ID = "00000000-0000-4000-8000-000000000065"


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

    def test_parser_enforces_bounded_json_domain(self) -> None:
        adversarial = (
            b"[" + (b" " * 1_000_001) + b"]",
            (b"[" * 129) + b"0" + (b"]" * 129),
            b'{"x":' + (b"9" * 641) + b"}",
        )
        for raw in adversarial:
            with self.subTest(size=len(raw)):
                with self.assertRaisesRegex(
                    RuntimeTargetHostInventoryError,
                    "bounded JSON domain",
                ):
                    RuntimeTargetHostInventory.parse(raw)

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

    def test_publication_retains_exact_authenticated_raw_bytes(self) -> None:
        expected = host_identity_fingerprint(IDENTITY)
        with TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory) / "evidence")
            with patch(
                "mvp.autotrade_mvp.runtime_target_host_inventory.capture_runtime_host_identity",
                return_value=dict(IDENTITY),
            ):
                published = publish_runtime_target_host_inventory(
                    store,
                    artifact_id=ARTIFACT_ID,
                    expected_source_sha=SOURCE_SHA,
                    expected_host_fingerprint=expected,
                )

            manifest, raw = store.read_authenticated_snapshot(ARTIFACT_ID)
            retained = RuntimeTargetHostInventory.parse(raw)
            self.assertEqual(published.artifact_id, ARTIFACT_ID)
            self.assertEqual(published.payload_sha256, manifest["sha256"])
            self.assertEqual(published.payload_sha256, retained.payload_sha256)
            self.assertEqual(published.host_fingerprint, expected)
            self.assertEqual(published.collector_id, COLLECTOR_ID)
            self.assertEqual(published.collector_version, COLLECTOR_VERSION)
            self.assertEqual(dict(retained.host_identity), IDENTITY)
            self.assertEqual(manifest["media_type"], "application/json")
            self.assertEqual(manifest["source_refs"], [f"git:{SOURCE_SHA}"])
            self.assertEqual(
                manifest["metadata"],
                {
                    "evidence_kind": RAW_EVIDENCE_KIND,
                    "collector_id": COLLECTOR_ID,
                    "collector_version": COLLECTOR_VERSION,
                    "host_fingerprint": expected,
                },
            )
            self.assertNotIn("created_at", retained.canonical_payload())

    def test_publication_is_idempotent_for_exact_inventory(self) -> None:
        expected = host_identity_fingerprint(IDENTITY)
        with TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory) / "evidence")
            with patch(
                "mvp.autotrade_mvp.runtime_target_host_inventory.capture_runtime_host_identity",
                return_value=dict(IDENTITY),
            ):
                first = publish_runtime_target_host_inventory(
                    store,
                    artifact_id=ARTIFACT_ID,
                    expected_source_sha=SOURCE_SHA,
                    expected_host_fingerprint=expected,
                )
                second = publish_runtime_target_host_inventory(
                    store,
                    artifact_id=ARTIFACT_ID,
                    expected_source_sha=SOURCE_SHA,
                    expected_host_fingerprint=expected,
                )
        self.assertEqual(first, second)

    def test_noncanonical_artifact_id_fails_before_capture_or_publication(self) -> None:
        expected = host_identity_fingerprint(IDENTITY)
        noncanonical = "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"
        with TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory) / "evidence")
            with patch(
                "mvp.autotrade_mvp.runtime_target_host_inventory.capture_runtime_host_identity"
            ) as capture:
                with self.assertRaisesRegex(
                    RuntimeTargetHostInventoryError,
                    "artifact_id must be a canonical UUID",
                ):
                    publish_runtime_target_host_inventory(
                        store,
                        artifact_id=noncanonical,
                        expected_source_sha=SOURCE_SHA,
                        expected_host_fingerprint=expected,
                    )
            capture.assert_not_called()
            self.assertEqual(list(store.manifests.iterdir()), [])

    def test_publication_rejects_noncanonical_source_or_store_type(self) -> None:
        expected = host_identity_fingerprint(IDENTITY)
        with self.assertRaisesRegex(TypeError, "exact ArtifactStore"):
            publish_runtime_target_host_inventory(
                object(),
                artifact_id=ARTIFACT_ID,
                expected_source_sha=SOURCE_SHA,
                expected_host_fingerprint=expected,
            )
        with TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory) / "evidence")
            with self.assertRaisesRegex(
                RuntimeTargetHostInventoryError,
                "lowercase 40-character Git SHA",
            ):
                publish_runtime_target_host_inventory(
                    store,
                    artifact_id=ARTIFACT_ID,
                    expected_source_sha="A" * 40,
                    expected_host_fingerprint=expected,
                )


if __name__ == "__main__":
    unittest.main()
