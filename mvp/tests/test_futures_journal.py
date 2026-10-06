from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, localcontext
from fractions import Fraction
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import NAMESPACE_URL, uuid5
import json
import sqlite3
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp import futures_journal as futures_journal_module
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
from mvp.autotrade_mvp.settlement_convention import SettlementConvention
from mvp.autotrade_mvp.persistence import (\n    JournalStore,\n    _event_envelope_digest,\n    canonical_json,\n    payload_digest,\n)
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
            settlement_convention=(SettlementConvention(
                provider_id="TEST_CLEARER", instrument_id="55555555-5555-4555-8555-555555555555",
                instrument_version=1, settlement_currency="BTC", quantum="0.00000001",
                rounding="HALF_EVEN", evidence_artifact_id="00000000-0000-0000-0000-000000000303",
                evidence_sha256="sha256:" + sha256(b"test inverse contract economics v1").hexdigest(),
            ) if payoff == "INVERSE" else None),
            metadata_evidence=({"artifact_id":"00000000-0000-0000-0000-000000000303",
                "sha256":"sha256:" + sha256(b"test inverse contract economics v1").hexdigest(), "observed_at":"2026-09-01T00:00:00Z"},)
                if payoff == "INVERSE" else (),

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
        if settlement.settlement_currency == "BTC":
            version = self._version(payoff="INVERSE")
            artifacts.publish_bytes(
                artifact_id=version.settlement_convention.evidence_artifact_id,
                data=b"test inverse contract economics v1",
                media_type="application/vnd.autotrade.instrument-metadata+json",
                rights={"storage":True,"export":False},
                metadata={"kind":"instrument-metadata",
                    "instrument_version_binding":InstrumentVersion.metadata_evidence_binding(version)},
            )
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

    def test_inverse_durable_detachment_preserves_terminal_policy_authority(self):
        contract = self._contract(payoff="INVERSE")
        detached = futures_journal_module._detached_durable_contract(contract)
        policy = futures_journal_module.inverse_settlement_convention(detached)
        self.assertEqual(policy.quantum, "0.00000001")
        self.assertEqual(policy.rounding, "HALF_EVEN")

        object.__setattr__(
            contract.canonical_instrument.settlement_convention,
            "quantum",
            "1",
        )
        with self.assertRaisesRegex(
            FuturesError,
            "settlement convention changed after contract admission",
        ):
            futures_journal_module._detached_durable_contract(contract)

    def test_provider_settlement_receipt_is_context_independent(self):
        contract = self._contract()
        observed = []
        for precision in (6, 10, 28, 80):
            for rounding in (ROUND_FLOOR, ROUND_CEILING):
                with self.subTest(precision=precision, rounding=rounding):
                    with localcontext() as context:
                        context.prec = precision
                        context.rounding = rounding
                        settlement = self._settlement(
                            contract,
                            "high-significance",
                            "12345678901234567890.123456789",
                            sequence=19,
                        )
                        receipt = provider_settlement_evidence_receipt(settlement)
                        observed.append(canonical_json(receipt))
        self.assertTrue(all(payload == observed[0] for payload in observed))
        self.assertIn(
            '"settlement_price":"12345678901234567890.123456789"',
            observed[0],
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
                evidence_artifact_root=Path(directory) / "artifacts",
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
                evidence_artifact_root=Path(directory) / "artifacts",
            )
            self.assertEqual(rebuilt, state)

            retried, retry_delta, retry_transaction, retry_inserted = (
                commit_linear_variation_margin(
                    reopened,
                    opening,
                    settlement,
                    evidence_artifact_store=artifacts,
                    evidence_artifact_root=Path(directory) / "artifacts",
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
                    evidence_artifact_root=Path(directory) / "artifacts",
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
                    evidence_artifact_root=Path(directory) / "artifacts",
                ),
                corrected,
            )
            rebuilt_book = rebuild_variation_margin_book(
                final_store,
                opening,
                evidence_artifact_store=artifacts,
                evidence_artifact_root=Path(directory) / "artifacts",
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
                evidence_artifact_root=Path(directory) / "artifacts",
            )
            aggregate_id = variation_margin_aggregate_id(opening)
            with self.assertRaisesRegex(FuturesError, "conflicts"):
                commit_linear_variation_margin(
                    store,
                    opening,
                    conflicting,
                    evidence_artifact_store=artifacts,
                    evidence_artifact_root=Path(directory) / "artifacts",
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
                    evidence_artifact_root=Path(directory) / "artifacts",
                ).last_settlement_price,
                Decimal("105"),
            )

    def test_inverse_replay_requires_durable_settlement_convention_identity(self):
        contract = self._contract(payoff="INVERSE")
        opening = InverseVariationMarginState(
            contract=contract,
            signed_contracts=Decimal("100"),
            last_settlement_price=Decimal("10000"),
            settlement_scope=self._scope(),
        )
        settlement = self._settlement(contract, "inverse-convention-id", "11000", sequence=1)

        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            artifact_root = Path(directory) / "artifacts"
            artifacts = ArtifactStore(artifact_root)
            settlement = self._bind_provider_evidence(artifacts, settlement)
            store = JournalStore(path)
            commit_inverse_variation_margin(
                store,
                opening,
                settlement,
                evidence_artifact_store=artifacts,
                evidence_artifact_root=artifact_root,
            )
            aggregate_id = variation_margin_aggregate_id(opening)
            connection = sqlite3.connect(path)
            try:
                row = connection.execute(
                    """
                    SELECT event_id, payload_json, payload_hash,
                           envelope_json, envelope_hash
                    FROM events
                    WHERE aggregate_type = ? AND aggregate_id = ?
                    ORDER BY aggregate_version
                    """,
                    ("FUTURES_VARIATION_MARGIN", aggregate_id),
                ).fetchone()
                self.assertIsNotNone(row)
                payload = json.loads(row[1])
                payload.pop("settlement_convention_id", None)
                payload_json = canonical_json(payload)
                payload_hash = payload_digest(payload)
                envelope = json.loads(row[3])
                envelope["payload"] = payload
                envelope["payload_hash"] = payload_hash
                envelope_json = canonical_json(envelope)
                connection.execute(
                    """
                    UPDATE events
                    SET payload_json = ?, payload_hash = ?,
                        envelope_json = ?, envelope_hash = ?
                    WHERE event_id = ?
                    """,
                    (
                        payload_json,
                        payload_hash,
                        envelope_json,
                        _event_envelope_digest(envelope_json),
                        row[0],
                    ),
                )
                connection.commit()
            finally:
                connection.close()

            with self.assertRaisesRegex(
                FuturesError,
                "durable inverse settlement economics do not reproduce",
            ):
                restore_inverse_variation_margin(
                    JournalStore(path),
                    opening,
                    evidence_artifact_store=artifacts,
                    evidence_artifact_root=artifact_root,
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
                    evidence_artifact_root=Path(directory) / "artifacts",
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
                    evidence_artifact_root=Path(directory) / "artifacts",
                ),
                state,
            )

            retried, retry_exact, retry_cash, retry_tx, retry_inserted = (
                commit_inverse_variation_margin(
                    JournalStore(path),
                    opening,
                    first,
                    evidence_artifact_store=artifacts,
                    evidence_artifact_root=Path(directory) / "artifacts",
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
                    evidence_artifact_root=Path(directory) / "artifacts",
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
                    evidence_artifact_root=Path(directory) / "artifacts",
                ),
                corrected,
            )
            rebuilt_book = rebuild_variation_margin_book(
                final_store,
                opening,
                evidence_artifact_store=artifacts,
                evidence_artifact_root=Path(directory) / "artifacts",
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
                    evidence_artifact_root=Path(directory) / "artifacts",
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
                    evidence_artifact_root=Path(directory) / "artifacts",
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


    def test_linear_commit_fails_closed_if_global_journal_cut_moves(self):
        contract = self._contract()
        opening = VariationMarginState(
            contract=contract,
            signed_contracts=Decimal("1"),
            last_settlement_price=Decimal("100"),
            settlement_scope=self._scope(),
        )
        settlement = self._settlement(contract, "cut-race-linear", "105", sequence=1)

        with TemporaryDirectory() as directory:
            artifact_root = Path(directory) / "artifacts"
            artifacts = ArtifactStore(artifact_root)
            settlement = self._bind_provider_evidence(artifacts, settlement)
            store = JournalStore(f"{directory}/journal.sqlite3")
            original_commit = JournalStore.commit_command

            def raced_commit(instance, **kwargs):
                payload = {"source": "unrelated-writer"}
                JournalStore.append_event(
                    instance,
                    {
                        "event_id": "unrelated-linear-cut-race",
                        "event_type": "UnrelatedCommitted",
                        "aggregate_type": "UNRELATED_TEST",
                        "aggregate_id": "linear",
                        "aggregate_version": "1",
                        "committed_at": utc(1).isoformat(),
                        "payload": payload,
                        "payload_hash": payload_digest(payload),
                    },
                )
                return original_commit(instance, **kwargs)

            with patch.object(JournalStore, "commit_command", new=raced_commit):
                with self.assertRaisesRegex(
                    FuturesError,
                    "journal cut changed before commit",
                ):
                    commit_linear_variation_margin(
                        store,
                        opening,
                        settlement,
                        evidence_artifact_root=artifact_root,
                        evidence_artifact_store=artifacts,
                    )

            self.assertEqual(
                store.load_events(
                    "FUTURES_VARIATION_MARGIN",
                    variation_margin_aggregate_id(opening),
                ),
                [],
            )
            self.assertEqual(store.current_journal_sequence(), 1)

    def test_inverse_commit_fails_closed_if_global_journal_cut_moves(self):
        contract = self._contract(payoff="INVERSE")
        opening = InverseVariationMarginState(
            contract=contract,
            signed_contracts=Decimal("100"),
            last_settlement_price=Decimal("10000"),
            settlement_scope=self._scope(),
        )
        settlement = self._settlement(
            contract,
            "cut-race-inverse",
            "11000",
            sequence=1,
        )

        with TemporaryDirectory() as directory:
            artifact_root = Path(directory) / "artifacts"
            artifacts = ArtifactStore(artifact_root)
            settlement = self._bind_provider_evidence(artifacts, settlement)
            store = JournalStore(f"{directory}/journal.sqlite3")
            original_commit = JournalStore.commit_command

            def raced_commit(instance, **kwargs):
                payload = {"source": "unrelated-writer"}
                JournalStore.append_event(
                    instance,
                    {
                        "event_id": "unrelated-inverse-cut-race",
                        "event_type": "UnrelatedCommitted",
                        "aggregate_type": "UNRELATED_TEST",
                        "aggregate_id": "inverse",
                        "aggregate_version": "1",
                        "committed_at": utc(1).isoformat(),
                        "payload": payload,
                        "payload_hash": payload_digest(payload),
                    },
                )
                return original_commit(instance, **kwargs)

            with patch.object(JournalStore, "commit_command", new=raced_commit):
                with self.assertRaisesRegex(
                    FuturesError,
                    "journal cut changed before commit",
                ):
                    commit_inverse_variation_margin(
                        store,
                        opening,
                        settlement,
                        evidence_artifact_root=artifact_root,
                        evidence_artifact_store=artifacts,
                    )

            self.assertEqual(
                store.load_events(
                    "FUTURES_VARIATION_MARGIN",
                    variation_margin_aggregate_id(opening),
                ),
                [],
            )
            self.assertEqual(store.current_journal_sequence(), 1)


    def test_rebuild_projects_the_single_verified_event_tuple(self):
        contract = self._contract()
        opening = VariationMarginState(
            contract=contract,
            signed_contracts=Decimal("1"),
            last_settlement_price=Decimal("100"),
            settlement_scope=self._scope(),
        )
        settlement = self._settlement(contract, "single-rebuild-cut", "105", sequence=1)

        with TemporaryDirectory() as directory:
            artifact_root = Path(directory) / "artifacts"
            artifacts = ArtifactStore(artifact_root)
            settlement = self._bind_provider_evidence(artifacts, settlement)
            store = JournalStore(f"{directory}/journal.sqlite3")
            commit_linear_variation_margin(
                store,
                opening,
                settlement,
                evidence_artifact_root=artifact_root,
                evidence_artifact_store=artifacts,
            )

            original_load = JournalStore.load_events
            calls = []

            def counted_load(instance, aggregate_type, aggregate_id):
                calls.append((aggregate_type, aggregate_id))
                return original_load(instance, aggregate_type, aggregate_id)

            with patch.object(JournalStore, "load_events", new=counted_load):
                book = rebuild_variation_margin_book(
                    store,
                    opening,
                    evidence_artifact_root=artifact_root,
                    evidence_artifact_store=artifacts,
                )

            self.assertEqual(len(calls), 1)
            self.assertEqual(book.cash("USD"), Decimal("50"))

    def test_correction_retains_one_reader_generation_for_history_and_new_evidence(self):
        contract = self._contract()
        opening = VariationMarginState(
            contract=contract,
            signed_contracts=Decimal("1"),
            last_settlement_price=Decimal("100"),
            settlement_scope=self._scope(),
        )
        first = self._settlement(contract, "reader-generation", "105", sequence=1)
        correction = self._settlement(
            contract,
            "reader-generation",
            "106",
            sequence=1,
            revision=1,
        )

        with TemporaryDirectory() as directory:
            artifact_root = Path(directory) / "artifacts"
            artifacts = ArtifactStore(artifact_root)
            first = self._bind_provider_evidence(artifacts, first)
            correction = self._bind_provider_evidence(artifacts, correction)
            store = JournalStore(f"{directory}/journal.sqlite3")
            commit_linear_variation_margin(
                store,
                opening,
                first,
                evidence_artifact_root=artifact_root,
                evidence_artifact_store=artifacts,
            )

            original_factory = futures_journal_module.trusted_authenticated_reader
            factory_calls = []
            read_calls = []

            def counted_factory(*args, **kwargs):
                factory_calls.append((args, kwargs))
                reader = original_factory(*args, **kwargs)

                def counted_reader(artifact_id):
                    read_calls.append(artifact_id)
                    return reader(artifact_id)

                return counted_reader

            with patch.object(
                futures_journal_module,
                "trusted_authenticated_reader",
                new=counted_factory,
            ):
                corrected, delta, _transaction, inserted = (
                    commit_linear_variation_margin(
                        store,
                        opening,
                        correction,
                        evidence_artifact_root=artifact_root,
                        evidence_artifact_store=artifacts,
                    )
                )

            self.assertTrue(inserted)
            self.assertEqual(delta, Decimal("10"))
            self.assertEqual(corrected.last_settlement_price, Decimal("106"))
            self.assertEqual(len(factory_calls), 1)
            self.assertEqual(len(read_calls), 2)


    def test_durable_replay_reseals_post_construction_quantity_grid(self):
        linear = VariationMarginState(
            contract=self._contract(),
            signed_contracts=Decimal("1"),
            last_settlement_price=Decimal("100"),
            settlement_scope=self._scope(),
        )
        inverse = InverseVariationMarginState(
            contract=self._contract(payoff="INVERSE"),
            signed_contracts=Decimal("100"),
            last_settlement_price=Decimal("10000"),
            settlement_scope=self._scope(),
        )
        object.__setattr__(linear, "signed_contracts", Decimal("1.5"))
        object.__setattr__(inverse, "signed_contracts", Decimal("100.5"))

        with TemporaryDirectory() as directory:
            artifact_root = Path(directory) / "artifacts"
            ArtifactStore(artifact_root)
            store = JournalStore(f"{directory}/journal.sqlite3")

            with self.assertRaisesRegex(
                FuturesError,
                "signed_contracts must be an exact multiple",
            ):
                restore_linear_variation_margin(
                    store,
                    linear,
                    evidence_artifact_root=artifact_root,
                )

            with self.assertRaisesRegex(
                FuturesError,
                "signed_contracts must be an exact multiple",
            ):
                restore_inverse_variation_margin(
                    store,
                    inverse,
                    evidence_artifact_root=artifact_root,
                )


    def test_durable_scope_rejects_hostile_scope_before_field_reads(self):
        callbacks = []

        class HostileScope(FuturesSettlementScope):
            def __getattribute__(self, name):
                if name in {"provider_id", "account_id", "environment", "source_id"}:
                    callbacks.append(name)
                    raise AssertionError("hostile settlement scope attribute executed")
                return object.__getattribute__(self, name)

        opening = VariationMarginState(
            contract=self._contract(),
            signed_contracts=Decimal("1"),
            last_settlement_price=Decimal("100"),
            settlement_scope=self._scope(),
        )
        base_scope = opening.settlement_scope
        hostile_scope = object.__new__(HostileScope)
        for name, value in object.__getattribute__(base_scope, "__dict__").items():
            object.__setattr__(hostile_scope, name, value)
        object.__setattr__(opening, "settlement_scope", hostile_scope)

        with self.assertRaisesRegex(
            FuturesError,
            "exact FuturesSettlementScope",
        ):
            variation_margin_aggregate_id(opening)
        self.assertEqual(callbacks, [])


    def test_durable_contract_reseal_rejects_hostile_scalar_without_callback(self):
        callbacks = []

        class HostileText(str):
            def strip(self, *args, **kwargs):
                callbacks.append("strip")
                raise AssertionError("hostile contract text executed")

            def __eq__(self, other):
                callbacks.append("eq")
                raise AssertionError("hostile contract equality executed")

        contract = self._contract()
        opening = VariationMarginState(
            contract=contract,
            signed_contracts=Decimal("1"),
            last_settlement_price=Decimal("100"),
            settlement_scope=self._scope(),
        )
        object.__setattr__(contract, "instrument", HostileText(contract.instrument))

        with self.assertRaisesRegex(
            FuturesError,
            "contract conflicts with canonical InstrumentVersion",
        ):
            variation_margin_aggregate_id(opening)
        self.assertEqual(callbacks, [])


    def test_durable_scope_rejects_post_construction_contract_drift(self):
        contract = self._contract()
        opening = VariationMarginState(
            contract=contract,
            signed_contracts=Decimal("1"),
            last_settlement_price=Decimal("100"),
            settlement_scope=self._scope(),
        )
        original_multiplier = contract.multiplier
        try:
            object.__setattr__(contract, "multiplier", Decimal("999"))
            with self.assertRaisesRegex(
                FuturesError,
                "contract conflicts with canonical InstrumentVersion",
            ):
                variation_margin_aggregate_id(opening)
        finally:
            object.__setattr__(contract, "multiplier", original_multiplier)


    def test_durable_evidence_reseals_post_construction_field_types(self):
        callbacks = []

        class HostileText(str):
            def strip(self, *args, **kwargs):
                callbacks.append("strip")
                raise AssertionError("hostile settlement text executed")

            def __eq__(self, other):
                callbacks.append("eq")
                raise AssertionError("hostile settlement equality executed")

        contract = self._contract()
        opening = VariationMarginState(
            contract=contract,
            signed_contracts=Decimal("1"),
            last_settlement_price=Decimal("100"),
            settlement_scope=self._scope(),
        )
        evidence = self._settlement(contract, "mutated-evidence", "105", sequence=1)
        object.__setattr__(
            evidence,
            "settlement_id",
            HostileText(evidence.settlement_id),
        )

        with self.assertRaisesRegex(FuturesError, "settlement_id must be exact"):
            provider_settlement_evidence_receipt(evidence)
        self.assertEqual(callbacks, [])

        with self.assertRaisesRegex(FuturesError, "settlement_id must be exact"):
            provider_settlement_evidence_metadata(evidence)
        self.assertEqual(callbacks, [])

        with TemporaryDirectory() as directory:
            artifact_root = Path(directory) / "artifacts"
            ArtifactStore(artifact_root)
            store = JournalStore(f"{directory}/journal.sqlite3")
            with self.assertRaisesRegex(FuturesError, "settlement_id must be exact"):
                commit_linear_variation_margin(
                    store,
                    opening,
                    evidence,
                    evidence_artifact_root=artifact_root,
                )
        self.assertEqual(callbacks, [])


    def test_durable_evidence_boundaries_reject_hostile_subclass_before_reads(self):
        callbacks = []

        class HostileSettlementEvidence(FuturesSettlementEvidence):
            def __getattribute__(self, name):
                if name in {
                    "evidence_ref",
                    "scope",
                    "settlement_id",
                    "observation_id",
                    "instrument_id",
                    "instrument_version",
                    "effective_at",
                    "sequence",
                    "revision",
                    "settlement_price",
                    "price_currency",
                    "settlement_currency",
                    "supersedes_observation_id",
                }:
                    callbacks.append(name)
                    raise AssertionError("hostile settlement evidence attribute executed")
                return object.__getattribute__(self, name)

        contract = self._contract()
        opening = VariationMarginState(
            contract=contract,
            signed_contracts=Decimal("1"),
            last_settlement_price=Decimal("100"),
            settlement_scope=self._scope(),
        )
        base = self._settlement(contract, "hostile-evidence", "105", sequence=1)
        hostile = object.__new__(HostileSettlementEvidence)
        for name, value in object.__getattribute__(base, "__dict__").items():
            object.__setattr__(hostile, name, value)

        with self.assertRaisesRegex(TypeError, "exact FuturesSettlementEvidence"):
            provider_settlement_evidence_receipt(hostile)
        self.assertEqual(callbacks, [])

        with self.assertRaisesRegex(TypeError, "exact FuturesSettlementEvidence"):
            provider_settlement_evidence_metadata(hostile)
        self.assertEqual(callbacks, [])

        with TemporaryDirectory() as directory:
            artifact_root = Path(directory) / "artifacts"
            ArtifactStore(artifact_root)
            store = JournalStore(f"{directory}/journal.sqlite3")
            with self.assertRaisesRegex(TypeError, "exact FuturesSettlementEvidence"):
                commit_linear_variation_margin(
                    store,
                    opening,
                    hostile,
                    evidence_artifact_root=artifact_root,
                )
        self.assertEqual(callbacks, [])


    def test_durable_entrypoints_reject_hostile_state_subclasses_before_reads(self):
        callbacks = []

        class HostileLinearState(VariationMarginState):
            def __getattribute__(self, name):
                if name in {
                    "contract",
                    "settlement_scope",
                    "settlement_history",
                    "signed_contracts",
                    "last_settlement_price",
                    "cumulative_variation_margin",
                }:
                    callbacks.append(("linear", name))
                    raise AssertionError("hostile linear state attribute executed")
                return object.__getattribute__(self, name)

        class HostileInverseState(InverseVariationMarginState):
            def __getattribute__(self, name):
                if name in {
                    "contract",
                    "settlement_scope",
                    "settlement_history",
                    "signed_contracts",
                    "last_settlement_price",
                    "cumulative_variation_margin",
                }:
                    callbacks.append(("inverse", name))
                    raise AssertionError("hostile inverse state attribute executed")
                return object.__getattribute__(self, name)

        linear = VariationMarginState(
            contract=self._contract(),
            signed_contracts=Decimal("1"),
            last_settlement_price=Decimal("100"),
            settlement_scope=self._scope(),
        )
        inverse = InverseVariationMarginState(
            contract=self._contract(payoff="INVERSE"),
            signed_contracts=Decimal("100"),
            last_settlement_price=Decimal("10000"),
            settlement_scope=self._scope(),
        )

        def hostile_copy(base, cls):
            hostile = object.__new__(cls)
            for name, value in object.__getattribute__(base, "__dict__").items():
                object.__setattr__(hostile, name, value)
            return hostile

        hostile_linear = hostile_copy(linear, HostileLinearState)
        hostile_inverse = hostile_copy(inverse, HostileInverseState)
        unused_root = Path("unused-hostile-state-artifacts")

        linear_calls = (
            lambda: variation_margin_aggregate_id(hostile_linear),
            lambda: restore_linear_variation_margin(
                None,
                hostile_linear,
                evidence_artifact_root=unused_root,
            ),
            lambda: rebuild_variation_margin_book(
                None,
                hostile_linear,
                evidence_artifact_root=unused_root,
            ),
            lambda: commit_linear_variation_margin(
                None,
                hostile_linear,
                None,
                evidence_artifact_root=unused_root,
            ),
        )
        inverse_calls = (
            lambda: variation_margin_aggregate_id(hostile_inverse),
            lambda: restore_inverse_variation_margin(
                None,
                hostile_inverse,
                evidence_artifact_root=unused_root,
            ),
            lambda: rebuild_variation_margin_book(
                None,
                hostile_inverse,
                evidence_artifact_root=unused_root,
            ),
            lambda: commit_inverse_variation_margin(
                None,
                hostile_inverse,
                None,
                evidence_artifact_root=unused_root,
            ),
        )

        for call in linear_calls + inverse_calls:
            with self.subTest(call=call):
                with self.assertRaisesRegex(TypeError, "exact"):
                    call()
                self.assertEqual(callbacks, [])


if __name__ == "__main__":
    unittest.main()