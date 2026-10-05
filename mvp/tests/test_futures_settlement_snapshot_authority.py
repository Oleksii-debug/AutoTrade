from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
import inspect
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import NAMESPACE_URL, uuid5

from mvp.autotrade_mvp.futures import (
    FuturesError,
    FuturesSettlementEvidence,
    FuturesSettlementScope,
)
from mvp.autotrade_mvp.futures_journal import (
    _trusted_settlement_evidence_reader,
    _verify_provider_settlement_evidence,
    commit_inverse_variation_margin,
    commit_linear_variation_margin,
    provider_settlement_evidence_metadata,
    provider_settlement_evidence_receipt,
)
from mvp.autotrade_mvp.persistence import canonical_json
from research.autotrade_research.artifacts import (
    ArtifactIntegrityError,
    ArtifactStore,
)


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
    def test_public_financial_paths_require_explicit_authoritative_root(self):
        for function in (
            commit_linear_variation_margin,
            commit_inverse_variation_margin,
        ):
            parameter = inspect.signature(function).parameters[
                "evidence_artifact_root"
            ]
            self.assertIs(parameter.default, inspect.Parameter.empty)

    def test_caller_publication_store_cannot_select_a_foreign_root(self):
        with TemporaryDirectory() as directory:
            trusted_root = Path(directory) / "trusted"
            trusted_store = ArtifactStore(trusted_root)
            bind(trusted_store, settlement())

            attacker_root = Path(directory) / "attacker"
            attacker_store = ArtifactStore(attacker_root)
            bind(attacker_store, settlement())

            with self.assertRaisesRegex(
                FuturesError,
                "trusted settlement evidence authority is unavailable",
            ):
                _trusted_settlement_evidence_reader(
                    trusted_root,
                    attacker_store,
                )

    def test_private_reader_ignores_poisoned_publication_store_read_methods(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "artifacts"
            store = ArtifactStore(root)
            evidence, artifact_id = bind(store, settlement())

            def forbidden(*_args, **_kwargs):
                raise AssertionError(
                    "caller publication-store reads must not be financial authority"
                )

            store.read_authenticated_snapshot = forbidden
            store.load_manifest = forbidden
            store.read_bytes = forbidden

            reader = _trusted_settlement_evidence_reader(root, store)
            canonical_ref = _verify_provider_settlement_evidence(evidence, reader)

            self.assertEqual(canonical_ref, evidence.evidence_ref)
            self.assertIn(artifact_id, canonical_ref)

    def test_verifier_consumes_exactly_one_authenticated_snapshot(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "artifacts"
            store = ArtifactStore(root)
            evidence, artifact_id = bind(store, settlement())
            reader = _trusted_settlement_evidence_reader(root, store)
            calls = []

            def counted(requested_artifact_id):
                calls.append(requested_artifact_id)
                return reader(requested_artifact_id)

            self.assertEqual(
                _verify_provider_settlement_evidence(evidence, counted),
                evidence.evidence_ref,
            )
            self.assertEqual(calls, [artifact_id])

    def test_authenticated_snapshot_failure_is_fail_closed(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "artifacts"
            store = ArtifactStore(root)
            evidence, _ = bind(store, settlement())

            def failing_reader(_artifact_id):
                raise ArtifactIntegrityError("simulated storage corruption")

            with self.assertRaisesRegex(
                FuturesError,
                "settlement provider evidence verification failed",
            ):
                _verify_provider_settlement_evidence(
                    evidence,
                    failing_reader,
                )

    def test_canonical_bytes_and_scope_are_rechecked_after_authenticated_read(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "artifacts"
            store = ArtifactStore(root)
            evidence, artifact_id = bind(store, settlement())
            reader = _trusted_settlement_evidence_reader(root, store)
            manifest, raw = reader(artifact_id)

            forged_manifest = dict(manifest)
            forged_manifest["metadata"] = dict(manifest["metadata"])
            forged_manifest["metadata"]["account_id"] = "other-account"

            def forged_reader(_artifact_id):
                return forged_manifest, raw

            with self.assertRaisesRegex(
                FuturesError,
                "verification failed",
            ):
                _verify_provider_settlement_evidence(
                    evidence,
                    forged_reader,
                )


if __name__ == "__main__":
    unittest.main()
