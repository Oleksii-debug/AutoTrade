from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import NAMESPACE_URL, uuid5

from autotrade_runtime.artifacts import (
    ArtifactIntegrityError,
    ArtifactStore,
)
from autotrade_runtime.resource_lock import ResourceLockError
from mvp.autotrade_mvp.futures import (
    FuturesError,
    FuturesSettlementEvidence,
    FuturesSettlementScope,
)
from mvp.autotrade_mvp.futures_journal import (
    _settlement_evidence_reader,
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
    def test_artifact_store_subclass_is_rejected_before_reader_issuance(self):
        class ForgedArtifactStore(ArtifactStore):
            snapshot_called = False

            def read_authenticated_snapshot(self, artifact_id):
                self.snapshot_called = True
                raise AssertionError("subclass snapshot override must not be invoked")

        with TemporaryDirectory() as directory:
            forged = ForgedArtifactStore(Path(directory) / "artifacts")
            with self.assertRaisesRegex(
                FuturesError,
                "canonical ArtifactStore publication input",
            ):
                _settlement_evidence_reader(forged.root, forged)
            self.assertFalse(forged.snapshot_called)

    def test_foreign_publication_store_cannot_match_selected_root(self):
        with TemporaryDirectory() as directory:
            authoritative = ArtifactStore(Path(directory) / "authoritative")
            foreign = ArtifactStore(Path(directory) / "foreign")
            with self.assertRaisesRegex(
                FuturesError,
                "root authority is invalid",
            ):
                _settlement_evidence_reader(authoritative.root, foreign)

    def test_settlement_evidence_subclass_is_rejected_before_virtual_dispatch(self):
        class ForgedSettlement(FuturesSettlementEvidence):
            def __getattribute__(self, name):
                if name == "evidence_ref":
                    raise AssertionError("subclass evidence_ref dispatch must not run")
                return super().__getattribute__(name)

        base = settlement()
        forged = ForgedSettlement(**base.__dict__)
        with self.assertRaisesRegex(TypeError, "evidence must be FuturesSettlementEvidence"):
            provider_settlement_evidence_receipt(forged)

        def forbidden_reader(_artifact_id):
            raise AssertionError("reader must not run for forged settlement evidence")

        with self.assertRaisesRegex(TypeError, "evidence must be FuturesSettlementEvidence"):
            _verify_provider_settlement_evidence(forged, forbidden_reader)

    def test_settlement_scope_subclass_is_rejected(self):
        class ForgedScope(FuturesSettlementScope):
            pass

        with self.assertRaisesRegex(FuturesError, "settlement scope is required"):
            FuturesSettlementEvidence(
                settlement_id="period-1",
                observation_id="period-1:r0",
                supersedes_observation_id=None,
                instrument_id="44444444-4444-4444-8444-444444444444",
                instrument_version=1,
                scope=ForgedScope(
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

    def test_bound_reader_ignores_post_issuance_publication_store_poison(self):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory) / "artifacts")
            evidence, artifact_id = bind(store, settlement())
            reader = _settlement_evidence_reader(store.root, store)

            def forbidden(*_args, **_kwargs):
                raise AssertionError("publication store must not regain read authority")

            store.root = Path(directory) / "redirected"
            store.read_authenticated_snapshot = forbidden
            store.load_manifest = forbidden
            store.read_bytes = forbidden
            store._manifest_path = forbidden
            store._read_verified_object_bytes = forbidden

            canonical_ref = _verify_provider_settlement_evidence(evidence, reader)
            self.assertEqual(canonical_ref, evidence.evidence_ref)
            self.assertIn(artifact_id, canonical_ref)

    def test_one_bound_authenticated_snapshot_is_consumed_exactly_once(self):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory) / "artifacts")
            evidence, artifact_id = bind(store, settlement())
            reader = _settlement_evidence_reader(store.root, store)
            calls: list[str] = []

            def counted(requested_artifact_id: str):
                calls.append(requested_artifact_id)
                return reader(requested_artifact_id)

            _verify_provider_settlement_evidence(evidence, counted)
            self.assertEqual(calls, [artifact_id])

    def test_reader_failures_are_normalized_fail_closed(self):
        for label, error in (
            ("oserror", OSError("simulated storage race")),
            ("integrity", ArtifactIntegrityError("simulated corruption")),
            ("resource-lock", ResourceLockError("simulated authority lock failure")),
        ):
            with self.subTest(label=label):
                def failing(_artifact_id, error=error):
                    raise error

                with self.assertRaisesRegex(
                    FuturesError,
                    "settlement provider evidence verification failed",
                ):
                    _verify_provider_settlement_evidence(settlement(), failing)

    def test_reader_issuance_resource_lock_failure_is_normalized(self):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory) / "artifacts")
            with patch(
                "mvp.autotrade_mvp.futures_journal.trusted_authenticated_reader",
                side_effect=ResourceLockError("simulated authority lock failure"),
            ):
                with self.assertRaisesRegex(
                    FuturesError,
                    "root authority is invalid",
                ):
                    _settlement_evidence_reader(store.root, store)

    def test_held_snapshot_bytes_are_independently_rehashed(self):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory) / "artifacts")
            evidence, artifact_id = bind(store, settlement())
            reader = _settlement_evidence_reader(store.root, store)
            manifest, raw = reader(artifact_id)

            def corrupted(_artifact_id):
                return manifest, raw + b"tampered"

            with self.assertRaisesRegex(
                FuturesError,
                "settlement provider evidence verification failed",
            ):
                _verify_provider_settlement_evidence(evidence, corrupted)


if __name__ == "__main__":
    unittest.main()
