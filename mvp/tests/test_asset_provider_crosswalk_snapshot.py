from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import NAMESPACE_URL, uuid5

from research.autotrade_research.artifacts.store import (
    ArtifactIntegrityError,
    ArtifactStore,
)

from mvp.autotrade_mvp.asset_provider_crosswalk import (
    LifecycleEvidence,
    advertised_lifecycle_keys,
    lifecycle_evidence_bytes,
    lifecycle_evidence_payload,
    qualify_asset_provider_crosswalk,
    required_cases,
)


SOURCE = "1" * 40
ADAPTER = "2" * 40


def _complete_evidence(key):
    artifact_id = str(
        uuid5(
            NAMESPACE_URL,
            "autotrade:wp61:snapshot:"
            + key.provider_id
            + ":"
            + key.product_family
            + ":"
            + key.lifecycle.value,
        )
    )
    provisional = LifecycleEvidence(
        key=key,
        source_sha=SOURCE,
        adapter_sha=ADAPTER,
        cases=required_cases(key.lifecycle),
        reconciliation_complete=True,
        economic_units_exact=True,
        artifact_id=artifact_id,
        artifact_sha256="sha256:" + "0" * 64,
    )
    return replace(
        provisional,
        artifact_sha256="sha256:"
        + sha256(lifecycle_evidence_bytes(provisional)).hexdigest(),
    )


def _adapter_map():
    return {
        (key.provider_id, key.product_family): ADAPTER
        for key in advertised_lifecycle_keys()
    }


def _publish(store: ArtifactStore, item: LifecycleEvidence) -> None:
    store.publish_bytes(
        artifact_id=item.artifact_id,
        data=lifecycle_evidence_bytes(item),
        media_type="application/vnd.autotrade.asset-provider-lifecycle",
        rights={"storage": True, "export": False},
        source_refs=[f"git:{item.source_sha}"],
        metadata=lifecycle_evidence_payload(item),
    )


class AssetProviderCrosswalkSnapshotTests(unittest.TestCase):
    def test_evidence_consumer_uses_exactly_one_authenticated_snapshot(self):
        key = advertised_lifecycle_keys()[0]
        item = _complete_evidence(key)
        with TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            _publish(store, item)
            original_snapshot = store.read_authenticated_snapshot
            with (
                patch.object(
                    store,
                    "read_authenticated_snapshot",
                    wraps=original_snapshot,
                ) as snapshot,
                patch.object(
                    store,
                    "load_manifest",
                    side_effect=AssertionError("split manifest read is forbidden"),
                ),
                patch.object(
                    store,
                    "read_bytes",
                    side_effect=AssertionError("split payload read is forbidden"),
                ),
            ):
                verdict = qualify_asset_provider_crosswalk(
                    [item],
                    exact_source_sha=SOURCE,
                    exact_adapter_shas=_adapter_map(),
                    evidence_store=store,
                )

        snapshot.assert_called_once_with(item.artifact_id)
        self.assertNotIn(key, verdict.invalid_keys)
        self.assertFalse(verdict.trading_authority_granted)

    def test_authenticated_snapshot_integrity_failure_invalidates_evidence(self):
        key = advertised_lifecycle_keys()[0]
        item = _complete_evidence(key)
        with TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            with (
                patch.object(
                    store,
                    "read_authenticated_snapshot",
                    side_effect=ArtifactIntegrityError("snapshot changed during read"),
                ) as snapshot,
                patch.object(
                    store,
                    "load_manifest",
                    side_effect=AssertionError("split manifest fallback is forbidden"),
                ),
                patch.object(
                    store,
                    "read_bytes",
                    side_effect=AssertionError("split payload fallback is forbidden"),
                ),
            ):
                verdict = qualify_asset_provider_crosswalk(
                    [item],
                    exact_source_sha=SOURCE,
                    exact_adapter_shas=_adapter_map(),
                    evidence_store=store,
                )

        snapshot.assert_called_once_with(item.artifact_id)
        self.assertIn(key, verdict.invalid_keys)
        self.assertEqual(verdict.status, "INCOMPLETE")
        self.assertFalse(verdict.trading_authority_granted)

    def test_same_payload_with_replaced_manifest_claims_is_invalid(self):
        key = advertised_lifecycle_keys()[0]
        item = _complete_evidence(key)
        with TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            _publish(store, item)
            manifest, data = store.read_authenticated_snapshot(item.artifact_id)
            replaced_manifest = dict(manifest)
            replaced_manifest["source_refs"] = ["git:" + "f" * 40]
            with patch.object(
                store,
                "read_authenticated_snapshot",
                return_value=(replaced_manifest, data),
            ):
                verdict = qualify_asset_provider_crosswalk(
                    [item],
                    exact_source_sha=SOURCE,
                    exact_adapter_shas=_adapter_map(),
                    evidence_store=store,
                )

        self.assertIn(key, verdict.invalid_keys)
        self.assertEqual(verdict.status, "INCOMPLETE")
        self.assertFalse(verdict.trading_authority_granted)


if __name__ == "__main__":
    unittest.main()
