from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import NAMESPACE_URL, uuid5
import sqlite3
import unittest

from mvp.autotrade_mvp.futures import (
    FuturesSettlementEvidence,
    FuturesSettlementScope,
    InverseVariationMarginState,
    VariationMarginState,
    FuturesContract,
    FuturesError,
    inverse_futures_pnl_exact,
)
from mvp.autotrade_mvp.futures_journal import (
    commit_inverse_variation_margin,
    commit_linear_variation_margin,
    rebuild_variation_margin_book,
    provider_settlement_evidence_metadata,
    provider_settlement_evidence_receipt,
    restore_inverse_variation_margin,
    restore_linear_variation_margin,
    variation_margin_aggregate_id,
)
from mvp.autotrade_mvp.instruments import InstrumentVersion
from mvp.autotrade_mvp.persistence import JournalStore, canonical_json
from research.autotrade_research.artifacts.store import ArtifactStore


def utc(day: int, hour: int = 0):
    return datetime(2026, 9, day, hour, tzinfo=timezone.utc)


class DurableFuturesVariationMarginTests(unittest.TestCase):
    def _version(self, *, payoff="LINEAR"):
        return InstrumentVersion(
            instrument_id=(
                "44444444-4444-4444-8444-444444444444"
                if payoff == "LINEAR"
                else "55555555-5555-4555-8555-555555555555"
            ),
            version=1,
            provider_id="TEST_CLEARER",
            venue_id="TEST_VENUE",
            provider_symbol="TEST-FUT" if payoff == "LINEAR" else "BTC-USD-INVERSE",
            asset_class="FUTURE",
            base_currency="TEST" if payoff == "LINEAR" else "BTC",
            quote_currency="USD",
            settlement_currency="USD" if payoff == "LINEAR" else "BTC",
            quantity_unit="CONTRACT",
            contract_multiplier=Decimal("10" if payoff == "LINEAR" else "1"),
            price_tick=Decimal("0.01"),
            quantity_step=Decimal("1"),
            minimum_quantity=Decimal("1"),
            calendar_id="CONTINUOUS_24_7",
            timezone_id="UTC",
            effective_from=utc(1),
            payoff=payoff,
            underlying_id=(
                "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa@1"
                if payoff == "LINEAR"
                else "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb@1"
            ),
            expiry=utc(30, 21),
            last_trade_at=utc(30, 20),
            delivery_cutoff=utc(30, 20),
            settlement_method="CASH",
            margin_model_id="TEST_FUTURES_MARGIN_V1",
        )

    def _contract(self, *, payoff="LINEAR"):
        return FuturesContract.from_instrument_version(self._version(payoff=payoff))

    def _scope(self):
        return FuturesSettlementScope(
            source_id="clearing:settlements",
            provider_id="TEST_CLEARER",
            account_id="acct-1",
            environment="PAPER",
        )

    def _settlement(
        self,
        contract,
        settlement_id,
        price,
        *,
        sequence=1,
        revision=0,
        effective_at=None,
        observation_id=None,
        supersedes=None,
    ):
        version = contract.canonical_instrument
        self.assertIsNotNone(version)
        observation = observation_id or f"{settlement_id}:r{revision}"
        if revision > 0 and supersedes is None:
            supersedes = f"{settlement_id}:r{revision - 1}"
        return FuturesSettlementEvidence(
            settlement_id=settlement_id,
            observation_id=observation,
            supersedes_observation_id=supersedes,
            instrument_id=version.instrument_id,
            instrument_version=version.version,
            scope=self._scope(),
            effective_at=effective_at or utc(25),
            sequence=sequence,
            revision=revision,
            settlement_price=Decimal(str(price)),
            price_currency=contract.quote_currency,
            settlement_currency=contract.settlement_currency,
        )

    def _bind_provider_evidence(self, artifacts, settlement):
        receipt = provider_settlement_evidence_receipt(settlement)
        artifact_id = str(
            uuid5(
                NAMESPACE_URL,
                "autotrade-futures-settlement:" + canonical_json(receipt),
            )
        )
        manifest = artifacts.publish_bytes(
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

    def test_linear_commit_restart_retry_and_correction_are_exactly_once(self):
        contract = self._contract()
        opening = VariationMarginState(
            contract=contract,
            signed_contracts=Decimal("2"),
            last_settlement_price=Decimal("100"),
            settlement_scope=self._scope(),
        )
        settlement = self._settlement(contract, "period-1", "105", sequence=1)

        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            artifacts = ArtifactStore(Path(directory) / "artifacts")
            settlement = self._bind_provider_evidence(artifacts, settlement)
            store = JournalStore(path)
            state, delta, transaction, inserted = commit_linear_variation_margin(
                store,
                opening,
                settlement,
                evidence_artifact_store=artifacts,
            )
            self.assertTrue(inserted)
            self.assertEqual(delta, Decimal("100"))
            self.assertIsNotNone(transaction)
            events = store.load_events("FUTURES_VARIATION_MARGIN", variation_margin_aggregate_id(opening))
            self.assertEqual(len(events), 1)
            self.assertEqual(
                events[0]["payload"]["transaction"]["postings"][0]["signed_amount"],
                "100",
            )

            reopened = JournalStore(path)
            rebuilt = restore_linear_variation_margin(
                reopened,
                opening,
                evidence_artifact_store=artifacts,
            )
            self.assertEqual(rebuilt, state)

            retried, retry_delta, retry_transaction, retry_inserted = (
                commit_linear_variation_margin(
                    reopened,
                    opening,
                    settlement,
                    evidence_artifact_store=artifacts,
                )
            )
            self.assertFalse(retry_inserted)
            self.assertEqual(retried, state)
            self.assertEqual(retry_delta, Decimal("0"))
            self.assertIsNone(retry_transaction)
            self.assertEqual(
                len(
                    reopened.load_events(
                        "FUTURES_VARIATION_MARGIN",
                        variation_margin_aggregate_id(opening),
                    )
                ),
                1,
            )

            correction = self._bind_provider_evidence(
                artifacts,
                self._settlement(
                    contract,
                    "period-1",
                    "106",
                    sequence=1,
                    revision=1,
                ),
            )
            corrected, correction_delta, correction_tx, correction_inserted = (
                commit_linear_variation_margin(
                    reopened,
                    opening,
                    correction,
                    evidence_artifact_store=artifacts,
                )
            )
            self.assertTrue(correction_inserted)
            self.assertEqual(correction_delta, Decimal("20"))
            self.assertIsNotNone(correction_tx)
            self.assertEqual(
                corrected.cumulative_variation_margin,
                Decimal("120"),
            )
            final_store = JournalStore(path)
            self.assertEqual(
                restore_linear_variation_margin(
                    final_store,
                    opening,
                    evidence_artifact_store=artifacts,
                ),
                corrected,
            )
            rebuilt_book = rebuild_variation_margin_book(
                final_store,
                opening,
                evidence_artifact_store=artifacts,
            )
            self.assertEqual(rebuilt_book.cash("USD"), Decimal("120"))
            self.assertEqual(
                rebuilt_book.balance("FUTURES_VARIATION_PNL:USD", "USD"),
                Decimal("-120"),
            )
            events = reopened.load_events(
                "FUTURES_VARIATION_MARGIN",
                variation_margin_aggregate_id(opening),
            )
            self.assertEqual(len(events), 2)
            self.assertEqual(
                events[-1]["payload"]["transaction"]["postings"][0]["signed_amount"],
                "20",
            )

    def test_conflicting_same_observation_is_rejected_without_new_event(self):
        contract = self._contract()
        opening = VariationMarginState(
            contract=contract,
            signed_contracts=Decimal("1"),
            last_settlement_price=Decimal("100"),
            settlement_scope=self._scope(),
        )
        first = self._settlement(contract, "period-1", "105")
        conflicting = self._settlement(
            contract,
            "period-1",
            "106",
            observation_id="period-1:r0",
        )

        with TemporaryDirectory() as directory:
            artifacts = ArtifactStore(Path(directory) / "artifacts")
            first = self._bind_provider_evidence(artifacts, first)
            conflicting = self._bind_provider_evidence(artifacts, conflicting)
            store = JournalStore(f"{directory}/journal.sqlite3")
            commit_linear_variation_margin(
                store,
                opening,
                first,
                evidence_artifact_store=artifacts,
            )
            aggregate_id = variation_margin_aggregate_id(opening)
            with self.assertRaisesRegex(FuturesError, "conflicts"):
                commit_linear_variation_margin(
                    store,
                    opening,
                    conflicting,
                    evidence_artifact_store=artifacts,
                )
            self.assertEqual(
                len(store.load_events("FUTURES_VARIATION_MARGIN", aggregate_id)),
                1,
            )
            self.assertEqual(
                restore_linear_variation_margin(
                    store,
                    opening,
                    evidence_artifact_store=artifacts,
                ).last_settlement_price,
                Decimal("105"),
            )

    def test_inverse_restart_retry_and_correction_preserve_exact_fraction(self):
        contract = self._contract(payoff="INVERSE")
        opening = InverseVariationMarginState(
            contract=contract,
            signed_contracts=Decimal("100"),
            last_settlement_price=Decimal("10000"),
            settlement_scope=self._scope(),
        )
        first = self._settlement(contract, "inverse-period", "11000", sequence=1)

        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            artifacts = ArtifactStore(Path(directory) / "artifacts")
            first = self._bind_provider_evidence(artifacts, first)
            store = JournalStore(path)
            state, exact, settled_cash, transaction, inserted = (
                commit_inverse_variation_margin(
                    store,
                    opening,
                    first,
                    evidence_artifact_store=artifacts,
                    settlement_quantum=Decimal("0.00000001"),
                )
            )
            self.assertTrue(inserted)
            self.assertEqual(exact, Fraction(1, 1100))
            self.assertEqual(settled_cash, Decimal("0.00090909"))
            self.assertIsNotNone(transaction)
            self.assertEqual(
                restore_inverse_variation_margin(
                    JournalStore(path),
                    opening,
                    evidence_artifact_store=artifacts,
                ),
                state,
            )

            retried, retry_exact, retry_cash, retry_tx, retry_inserted = (
                commit_inverse_variation_margin(
                    JournalStore(path),
                    opening,
                    first,
                    evidence_artifact_store=artifacts,
                    settlement_quantum=Decimal("0.00000001"),
                )
            )
            self.assertFalse(retry_inserted)
            self.assertEqual(retried, state)
            self.assertEqual(retry_exact, Fraction(0, 1))
            self.assertEqual(retry_cash, Decimal("0"))
            self.assertIsNone(retry_tx)

            correction = self._bind_provider_evidence(
                artifacts,
                self._settlement(
                    contract,
                    "inverse-period",
                    "12000",
                    sequence=1,
                    revision=1,
                ),
            )
            corrected, correction_exact, _, correction_tx, correction_inserted = (
                commit_inverse_variation_margin(
                    JournalStore(path),
                    opening,
                    correction,
                    evidence_artifact_store=artifacts,
                    settlement_quantum=Decimal("0.00000001"),
                )
            )
            self.assertTrue(correction_inserted)
            self.assertIsNotNone(correction_tx)
            self.assertEqual(
                corrected.cumulative_variation_margin,
                inverse_futures_pnl_exact(
                    signed_contracts=100,
                    contract_quote_value=1,
                    entry_price=10000,
                    exit_price=12000,
                ),
            )
            self.assertEqual(
                correction_exact,
                inverse_futures_pnl_exact(
                    signed_contracts=100,
                    contract_quote_value=1,
                    entry_price=11000,
                    exit_price=12000,
                ),
            )
            final_store = JournalStore(path)
            self.assertEqual(
                restore_inverse_variation_margin(
                    final_store,
                    opening,
                    evidence_artifact_store=artifacts,
                ),
                corrected,
            )
            rebuilt_book = rebuild_variation_margin_book(
                final_store,
                opening,
                evidence_artifact_store=artifacts,
            )
            self.assertEqual(
                rebuilt_book.cash("BTC"),
                sum(
                    (
                        Decimal(item["payload"]["settled_cash_delta"])
                        for item in final_store.load_events(
                            "FUTURES_VARIATION_MARGIN",
                            variation_margin_aggregate_id(opening),
                        )
                    ),
                    Decimal("0"),
                ),
            )

    def test_altered_provider_economics_under_same_artifact_is_rejected_before_commit(self):
        contract = self._contract()
        opening = VariationMarginState(
            contract=contract,
            signed_contracts=Decimal("1"),
            last_settlement_price=Decimal("100"),
            settlement_scope=self._scope(),
        )
        with TemporaryDirectory() as directory:
            artifacts = ArtifactStore(Path(directory) / "artifacts")
            original = self._bind_provider_evidence(
                artifacts,
                self._settlement(contract, "provenance-period", "105"),
            )
            altered = replace(original, settlement_price=Decimal("106"))
            store = JournalStore(f"{directory}/journal.sqlite3")
            aggregate_id = variation_margin_aggregate_id(opening)

            with self.assertRaisesRegex(
                FuturesError,
                "evidence (manifest metadata|does not match supplied economics)",
            ):
                commit_linear_variation_margin(
                    store,
                    opening,
                    altered,
                    evidence_artifact_store=artifacts,
                )

            self.assertEqual(
                store.load_events("FUTURES_VARIATION_MARGIN", aggregate_id),
                [],
            )
            connection = sqlite3.connect(store.path)
            try:
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM command_dedupe WHERE actor = ?",
                        ("autotrade-futures-settlement",),
                    ).fetchone()[0],
                    0,
                )
            finally:
                connection.close()

    def test_sqlite_failure_rolls_back_command_and_settlement_event_together(self):
        contract = self._contract()
        opening = VariationMarginState(
            contract=contract,
            signed_contracts=Decimal("1"),
            last_settlement_price=Decimal("100"),
            settlement_scope=self._scope(),
        )
        settlement = self._settlement(contract, "rollback-period", "105")
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            artifacts = ArtifactStore(Path(directory) / "artifacts")
            settlement = self._bind_provider_evidence(artifacts, settlement)
            store = JournalStore(path)
            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    """
                    CREATE TRIGGER fail_futures_event
                    BEFORE INSERT ON events
                    WHEN NEW.aggregate_type = 'FUTURES_VARIATION_MARGIN'
                    BEGIN
                        SELECT RAISE(ABORT, 'injected futures commit failure');
                    END
                    """
                )
                connection.commit()
            finally:
                connection.close()

            with self.assertRaisesRegex(Exception, "injected futures commit failure"):
                commit_linear_variation_margin(
                    store,
                    opening,
                    settlement,
                    evidence_artifact_store=artifacts,
                )

            aggregate_id = variation_margin_aggregate_id(opening)
            self.assertEqual(
                store.load_events("FUTURES_VARIATION_MARGIN", aggregate_id),
                [],
            )
            connection = sqlite3.connect(path)
            try:
                command_count = connection.execute(
                    "SELECT COUNT(*) FROM command_dedupe WHERE actor = ?",
                    ("autotrade-futures-settlement",),
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(command_count, 0)


if __name__ == "__main__":
    unittest.main()
