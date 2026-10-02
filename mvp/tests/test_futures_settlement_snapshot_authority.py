from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import NAMESPACE_URL, uuid5

from autotrade_runtime.artifacts.store import (
    ArtifactIntegrityError,
    ArtifactStore,
)
from mvp.autotrade_mvp.futures import (
    FuturesError,
    FuturesSettlementEvidence,
    FuturesSettlementScope,
)
from mvp.autotrade_mvp.futures_journal import (
    _verify_provider_settlement_evidence,
    provider_settlement_evidence_metadata,
    provider_settlement_evidence_receipt,
)
from mvp.autotrade_mvp.persistence import canonical_json


NOW = datetime(2026, 9, 25, tzinfo=timezone.utc)


def settlement() -> FuturesSettlementEvidence:
    return FuturesSettlementEvidence(
        settlement_id="period-1",
        observation_id="period-1:r0",
        supersedes_observation_id=None,
        instrument_id="44444444-4444-4444-8444-444444444444",
        instrument_version=1,
        scope=FuturesSettlementScope(
            source_id="clearing:settlements",
            provider_id="TEST_CLEARER",
            account_id="acct-1",
            environment="PAPER",
        ),
        effective_at=NOW,
        sequence=1,
        revision=0,
        settlement_price=Decimal("105.25"),
        price_currency="USD",
        settlement_currency="USD",
    )


def bind(store: ArtifactStore, evidence: FuturesSettlementEvidence):
    receipt = provider_settlement_evidence_receipt(evidence)
    artifact_id = str(
        uuid5(
            NAMESPACE_URL,
            "autotrade-futures-settlement:" + canonical_json(receipt),
        )
    )
    manifest = store.publish_bytes(
        artifact_id=artifact_id,
        data=canonical_json(receipt).encode("utf-8"),
        media_type="application/vnd.autotrade.futures-settlement-evidence+json",
        rights={"storage": True, "export": False},
        metadata=provider_settlement_evidence_metadata(evidence),
    )
    return replace(
        evidence,
        evidence_ref=f"artifact:{artifact_id}@{manifest['sha256']}",
    ), artifact_id


class FuturesSettlementSnapshotAuthorityTests(unittest.TestCase):
    def test_artifact_store_subclass_is_rejected_before_override_dispatch(self):
        class ForgedArtifactStore(ArtifactStore):
            snapshot_called = False
            manifest_called = False
            bytes_called = False

            def read_authenticated_snapshot(self, artifact_id):
                self.snapshot_called = True
                raise AssertionError("subclass snapshot override must not be invoked")

            def load_manifest(self, artifact_id):
                self.manifest_called = True
                raise AssertionError("subclass manifest override must not be invoked")

            def read_bytes(self, artifact_id):
                self.bytes_called = True
                raise AssertionError("subclass bytes override must not be invoked")

        with TemporaryDirectory() as directory:
            forged = ForgedArtifactStore(Path(directory) / "artifacts")
            with self.assertRaisesRegex(
                FuturesError,
                "requires trusted ArtifactStore",
            ):
                _verify_provider_settlement_evidence(settlement(), forged)
            self.assertFalse(forged.snapshot_called)
            self.assertFalse(forged.manifest_called)
            self.assertFalse(forged.bytes_called)

    def test_canonical_snapshot_ignores_poisoned_instance_read_methods(self):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory) / "artifacts")
            evidence, artifact_id = bind(store, settlement())

            def forbidden(*_args, **_kwargs):
                raise AssertionError("consumer must not virtual-dispatch instance read methods")

            store.read_authenticated_snapshot = forbidden
            store.load_manifest = forbidden
            store.read_bytes = forbidden

            canonical_ref = _verify_provider_settlement_evidence(evidence, store)
            self.assertEqual(canonical_ref, evidence.evidence_ref)
            self.assertIn(artifact_id, canonical_ref)

    def test_one_canonical_authenticated_snapshot_is_used_exactly_once(self):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory) / "artifacts")
            evidence, artifact_id = bind(store, settlement())
            original = ArtifactStore.read_authenticated_snapshot
            calls: list[tuple[ArtifactStore, str]] = []

            def counted(instance: ArtifactStore, requested_artifact_id: str):
                calls.append((instance, requested_artifact_id))
                return original(instance, requested_artifact_id)

            with patch.object(
                ArtifactStore,
                "read_authenticated_snapshot",
                new=counted,
            ):
                _verify_provider_settlement_evidence(evidence, store)

            self.assertEqual(calls, [(store, artifact_id)])

    def test_snapshot_oserror_is_normalized_fail_closed(self):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory) / "artifacts")
            evidence, _ = bind(store, settlement())
            with patch.object(
                ArtifactStore,
                "read_authenticated_snapshot",
                side_effect=OSError("simulated storage race"),
            ):
                with self.assertRaisesRegex(
                    FuturesError,
                    "settlement provider evidence verification failed",
                ):
                    _verify_provider_settlement_evidence(evidence, store)

    def test_snapshot_integrity_failure_is_normalized_fail_closed(self):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory) / "artifacts")
            evidence, _ = bind(store, settlement())
            with patch.object(
                ArtifactStore,
                "read_authenticated_snapshot",
                side_effect=ArtifactIntegrityError("simulated corruption"),
            ):
                with self.assertRaisesRegex(
                    FuturesError,
                    "settlement provider evidence verification failed",
                ):
                    _verify_provider_settlement_evidence(evidence, store)


if __name__ == "__main__":
    unittest.main()
