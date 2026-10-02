from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import NAMESPACE_URL, uuid5

from autotrade_runtime.artifacts import ArtifactIntegrityError, ArtifactStore

from mvp.autotrade_mvp import futures_journal as journal_module
from mvp.autotrade_mvp.futures import (
    FuturesContract,
    FuturesError,
    FuturesSettlementEvidence,
    FuturesSettlementScope,
    VariationMarginState,
)
from mvp.autotrade_mvp.futures_journal import (
    commit_linear_variation_margin,
    provider_settlement_evidence_metadata,
    provider_settlement_evidence_receipt,
    restore_linear_variation_margin,
    variation_margin_aggregate_id,
)
from mvp.autotrade_mvp.instruments import InstrumentVersion
from mvp.autotrade_mvp.persistence import JournalStore, canonical_json, payload_digest


def utc(day: int, hour: int = 0):
    return datetime(2026, 9, day, hour, tzinfo=timezone.utc)


class FuturesJournalAuthorityTests(unittest.TestCase):
    def _contract(self):
        version = InstrumentVersion(
            instrument_id="44444444-4444-4444-8444-444444444444",
            version=1,
            provider_id="TEST_CLEARER",
            venue_id="TEST_VENUE",
            provider_symbol="TEST-FUT",
            asset_class="FUTURE",
            base_currency="TEST",
            quote_currency="USD",
            settlement_currency="USD",
            quantity_unit="CONTRACT",
            contract_multiplier=Decimal("10"),
            price_tick=Decimal("0.01"),
            quantity_step=Decimal("1"),
            minimum_quantity=Decimal("1"),
            calendar_id="CONTINUOUS_24_7",
            timezone_id="UTC",
            effective_from=utc(1),
            payoff="LINEAR",
            underlying_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa@1",
            expiry=utc(30, 21),
            last_trade_at=utc(30, 20),
            delivery_cutoff=utc(30, 20),
            settlement_method="CASH",
            margin_model_id="TEST_FUTURES_MARGIN_V1",
        )
        return FuturesContract.from_instrument_version(version)

    @staticmethod
    def _scope():
        return FuturesSettlementScope(
            source_id="clearing:settlements",
            provider_id="TEST_CLEARER",
            account_id="acct-1",
            environment="PAPER",
        )

    def _opening(self):
        return VariationMarginState(
            contract=self._contract(),
            signed_contracts=Decimal("2"),
            last_settlement_price=Decimal("100"),
            settlement_scope=self._scope(),
        )

    def _settlement(self):
        contract = self._contract()
        version = contract.canonical_instrument
        self.assertIsNotNone(version)
        return FuturesSettlementEvidence(
            settlement_id="period-1",
            observation_id="period-1:r0",
            supersedes_observation_id=None,
            instrument_id=version.instrument_id,
            instrument_version=version.version,
            scope=self._scope(),
            effective_at=utc(25),
            sequence=1,
            revision=0,
            settlement_price=Decimal("105"),
            price_currency=contract.quote_currency,
            settlement_currency=contract.settlement_currency,
        )

    def _bind(self, artifacts: ArtifactStore, settlement: FuturesSettlementEvidence):
        receipt = provider_settlement_evidence_receipt(settlement)
        artifact_id = str(
            uuid5(
                NAMESPACE_URL,
                "autotrade-futures-settlement:" + canonical_json(receipt),
            )
        )
        manifest = ArtifactStore.publish_bytes(
            artifacts,
            artifact_id=artifact_id,
            data=canonical_json(receipt).encode("utf-8"),
            media_type="application/vnd.autotrade.futures-settlement-evidence+json",
            rights={"storage": True, "export": False},
            metadata=provider_settlement_evidence_metadata(settlement),
        )
        return replace(
            settlement,
            evidence_ref=f"artifact:{artifact_id}@{manifest['sha256']}",
        )

    def test_restore_rejects_journal_store_subclass_before_state_processing(self):
        class ForgedStore(JournalStore):
            pass

        forged = object.__new__(ForgedStore)
        with self.assertRaisesRegex(TypeError, "exact JournalStore"):
            restore_linear_variation_margin(
                forged,
                object(),
                evidence_artifact_store=object(),
                evidence_artifact_root="irrelevant",
            )

    def test_settlement_reader_rejects_artifact_store_subclass_before_override(self):
        calls = []

        class ForgedArtifactStore(ArtifactStore):
            def read_authenticated_snapshot(self, _artifact_id):
                calls.append("called")
                return {}, b"{}"

        with TemporaryDirectory() as directory:
            forged = ForgedArtifactStore(Path(directory) / "forged")
            with self.assertRaisesRegex(
                FuturesError,
                "canonical ArtifactStore publication input",
            ):
                journal_module._settlement_evidence_reader(forged.root, forged)
        self.assertEqual(calls, [])

    def test_bound_reader_ignores_poisoned_publication_store_methods(self):
        with TemporaryDirectory() as directory:
            artifacts = ArtifactStore(Path(directory) / "artifacts")
            settlement = self._bind(artifacts, self._settlement())
            reader = journal_module._settlement_evidence_reader(
                artifacts.root,
                artifacts,
            )
            with (
                patch.object(
                    artifacts,
                    "load_manifest",
                    side_effect=AssertionError("legacy split manifest read used"),
                ),
                patch.object(
                    artifacts,
                    "read_bytes",
                    side_effect=AssertionError("legacy split object read used"),
                ),
                patch.object(
                    artifacts,
                    "read_authenticated_snapshot",
                    side_effect=AssertionError("publication store regained read authority"),
                ),
            ):
                reference = journal_module._verify_provider_settlement_evidence(
                    settlement,
                    reader,
                )
            self.assertEqual(reference, settlement.evidence_ref)

    def test_reader_failure_is_fail_closed(self):
        settlement = replace(
            self._settlement(),
            evidence_ref=(
                f"artifact:{uuid5(NAMESPACE_URL, 'reader-failure')}@sha256:"
                + "0" * 64
            ),
        )

        def failed_reader(_artifact_id):
            raise ArtifactIntegrityError("snapshot changed")

        with self.assertRaisesRegex(
            FuturesError,
            "provider evidence verification failed",
        ):
            journal_module._verify_provider_settlement_evidence(
                settlement,
                failed_reader,
            )

    def test_unrelated_journal_advance_after_evidence_validation_blocks_commit(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            artifacts = ArtifactStore(Path(directory) / "artifacts")
            opening = self._opening()
            settlement = self._bind(artifacts, self._settlement())
            original_verify = journal_module._verify_provider_settlement_evidence
            advanced = False

            def verify_and_advance(evidence, evidence_reader):
                nonlocal advanced
                result = original_verify(evidence, evidence_reader)
                if not advanced:
                    advanced = True
                    payload = {"kind": "concurrent-durable-fact"}
                    JournalStore.append_event(
                        store,
                        {
                            "event_id": "concurrent-durable-fact-1",
                            "event_type": "ConcurrentDurableFact",
                            "aggregate_type": "test_concurrent_fact",
                            "aggregate_id": "global-cut",
                            "aggregate_version": "1",
                            "payload": payload,
                            "payload_hash": payload_digest(payload),
                            "committed_at": "2026-09-25T00:00:00Z",
                        },
                    )
                return result

            with patch.object(
                journal_module,
                "_verify_provider_settlement_evidence",
                side_effect=verify_and_advance,
            ):
                with self.assertRaisesRegex(
                    ValueError,
                    "journal sequence changed after financial evidence validation",
                ):
                    commit_linear_variation_margin(
                        store,
                        opening,
                        settlement,
                        evidence_artifact_store=artifacts,
                        evidence_artifact_root=artifacts.root,
                    )

            self.assertEqual(
                JournalStore.load_events(
                    store,
                    "FUTURES_VARIATION_MARGIN",
                    variation_margin_aggregate_id(opening),
                ),
                [],
            )

    def test_successful_insert_uses_one_aggregate_read_cut(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            artifacts = ArtifactStore(Path(directory) / "artifacts")
            opening = self._opening()
            settlement = self._bind(artifacts, self._settlement())

            with patch.object(
                JournalStore,
                "load_events",
                wraps=JournalStore.load_events,
            ) as load_events:
                state, delta, transaction, inserted = (
                    commit_linear_variation_margin(
                        store,
                        opening,
                        settlement,
                        evidence_artifact_store=artifacts,
                        evidence_artifact_root=artifacts.root,
                    )
                )

            self.assertTrue(inserted)
            self.assertEqual(delta, Decimal("100"))
            self.assertIsNotNone(transaction)
            self.assertEqual(len(state.settlement_history), 1)
            self.assertEqual(load_events.call_count, 1)



if __name__ == "__main__":
    unittest.main()
