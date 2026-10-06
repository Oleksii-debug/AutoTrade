from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

from autotrade_research.artifacts.store import ArtifactStore
from autotrade_research.evaluation.utility_projection import (
    UTILITY_OWNER_AUTHORITY,
    UTILITY_PROJECTION_KIND,
    UTILITY_PROJECTION_MEDIA_TYPE,
    UTILITY_RULE_ID,
    UtilityProjectionRule,
    UtilityProjectionRuleError,
    build_utility_projection_rule,
    publish_utility_projection_rule,
    require_source_owned_utility_projection_rule,
)


class UtilityProjectionRuleTests(unittest.TestCase):
    def test_rule_is_deterministic_source_owned_descriptor_without_operands(self):
        first = build_utility_projection_rule("USD")
        second = build_utility_projection_rule("USD")

        self.assertEqual(first, second)
        self.assertEqual(first.immutable_reference, second.immutable_reference)
        payload = json.loads(first.payload.decode("utf-8"))
        self.assertEqual(
            payload,
            {
                "cost_components": [],
                "owner_authority": UTILITY_OWNER_AUTHORITY,
                "projection_kind": UTILITY_PROJECTION_KIND,
                "rule_id": UTILITY_RULE_ID,
                "schema_version": 1,
                "value_unit": "USD",
            },
        )
        self.assertNotIn("utility", payload)
        self.assertNotIn("cost", payload)
        self.assertNotIn("score", payload)

    def test_value_unit_is_part_of_rule_identity(self):
        usd = build_utility_projection_rule("USD")
        basis_points = build_utility_projection_rule("BPS")

        self.assertNotEqual(usd.artifact_id, basis_points.artifact_id)
        self.assertNotEqual(usd.sha256, basis_points.sha256)
        self.assertNotEqual(usd.immutable_reference, basis_points.immutable_reference)

    def test_noncanonical_or_executable_text_subclass_is_rejected(self):
        class HostileText(str):
            def strip(self):
                raise AssertionError("virtual strip must not execute")

            def upper(self):
                raise AssertionError("virtual upper must not execute")

        for value in ("usd", " USD", "USD ", "", "USD/JPY", HostileText("USD")):
            with self.subTest(value=repr(value)):
                with self.assertRaises(UtilityProjectionRuleError):
                    build_utility_projection_rule(value)

    def test_direct_descriptor_forgery_is_rejected(self):
        canonical = build_utility_projection_rule("USD")
        with self.assertRaises(UtilityProjectionRuleError):
            UtilityProjectionRule(
                value_unit="USD",
                artifact_id=str(uuid4()),
                sha256=canonical.sha256,
                payload=canonical.payload,
            )
        with self.assertRaises(UtilityProjectionRuleError):
            UtilityProjectionRule(
                value_unit="USD",
                artifact_id=canonical.artifact_id,
                sha256="sha256:" + ("0" * 64),
                payload=canonical.payload,
            )
        with self.assertRaises(UtilityProjectionRuleError):
            UtilityProjectionRule(
                value_unit="USD",
                artifact_id=canonical.artifact_id,
                sha256=canonical.sha256,
                payload=b"{}",
            )

    def test_publication_is_idempotent_and_resolves_exact_installed_rule(self):
        with TemporaryDirectory() as root:
            store = ArtifactStore(Path(root) / "artifacts")
            first = publish_utility_projection_rule(store, value_unit="USD")
            second = publish_utility_projection_rule(store, value_unit="USD")

            self.assertEqual(first, second)
            resolved = require_source_owned_utility_projection_rule(
                store,
                reference=first.immutable_reference,
                value_unit="USD",
            )
            self.assertEqual(resolved, first)
            manifest, data = ArtifactStore.read_authenticated_snapshot(
                store,
                first.artifact_id,
            )
            self.assertEqual(manifest["media_type"], UTILITY_PROJECTION_MEDIA_TYPE)
            self.assertEqual(manifest["sha256"], first.sha256)
            self.assertEqual(data, first.payload)

    def test_caller_selected_descriptor_reference_cannot_select_semantics(self):
        with TemporaryDirectory() as root:
            store = ArtifactStore(Path(root) / "artifacts")
            forged_id = str(uuid4())
            forged_payload = json.dumps(
                {
                    "cost_components": [],
                    "owner_authority": UTILITY_OWNER_AUTHORITY,
                    "projection_kind": UTILITY_PROJECTION_KIND,
                    "rule_id": "caller-selected-easy-score-v1",
                    "schema_version": 1,
                    "value_unit": "USD",
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            manifest = store.publish_bytes(
                artifact_id=forged_id,
                data=forged_payload,
                media_type=UTILITY_PROJECTION_MEDIA_TYPE,
                rights={"storage": True, "export": True},
            )
            forged_ref = f"artifact:{forged_id}@{manifest['sha256']}"

            with self.assertRaisesRegex(
                UtilityProjectionRuleError,
                "does not name the installed source rule",
            ):
                require_source_owned_utility_projection_rule(
                    store,
                    reference=forged_ref,
                    value_unit="USD",
                )

    def test_writable_store_cannot_redefine_bytes_at_canonical_identity(self):
        expected = build_utility_projection_rule("USD")
        with TemporaryDirectory() as root:
            store = ArtifactStore(Path(root) / "artifacts")
            forged_payload = expected.payload.replace(
                UTILITY_RULE_ID.encode("utf-8"),
                b"caller-selected-easy-score-v1",
            )
            store.publish_bytes(
                artifact_id=expected.artifact_id,
                data=forged_payload,
                media_type=UTILITY_PROJECTION_MEDIA_TYPE,
                rights={"storage": True, "export": True},
            )

            with self.assertRaisesRegex(
                UtilityProjectionRuleError,
                "manifest digest does not match source rule",
            ):
                require_source_owned_utility_projection_rule(
                    store,
                    reference=expected.immutable_reference,
                    value_unit="USD",
                )

    def test_artifact_store_subclasses_are_not_rule_authority(self):
        class ForgedStore(ArtifactStore):
            pass

        with TemporaryDirectory() as root:
            forged = ForgedStore(Path(root) / "artifacts")
            rule = build_utility_projection_rule("USD")
            with self.assertRaises(TypeError):
                publish_utility_projection_rule(forged, value_unit="USD")
            with self.assertRaises(TypeError):
                require_source_owned_utility_projection_rule(
                    forged,
                    reference=rule.immutable_reference,
                    value_unit="USD",
                )


if __name__ == "__main__":
    unittest.main()
