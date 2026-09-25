from datetime import datetime, timezone
from decimal import Decimal
from fractions import Fraction
from tempfile import TemporaryDirectory
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
    restore_inverse_variation_margin,
    restore_linear_variation_margin,
)
from mvp.autotrade_mvp.instruments import InstrumentVersion
from mvp.autotrade_mvp.persistence import JournalStore


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
            underlying_id="TEST" if payoff == "LINEAR" else "BTC",
            expiry=utc(30, 21),
            last_trade_at=utc(30, 20),
            delivery_cutoff=utc(30, 20),
            settlement_method="CASH",
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
            store = JournalStore(path)
            state, delta, transaction, inserted = commit_linear_variation_margin(
                store, opening, settlement
            )
            self.assertTrue(inserted)
            self.assertEqual(delta, Decimal("100"))
            self.assertIsNotNone(transaction)
            events = store.load_events("FUTURES_VARIATION_MARGIN", self._aggregate_id(store, opening))
            self.assertEqual(len(events), 1)
            self.assertEqual(
                events[0]["payload"]["transaction"]["postings"][0]["signed_amount"],
                "100",
            )

            reopened = JournalStore(path)
            rebuilt = restore_linear_variation_margin(reopened, opening)
            self.assertEqual(rebuilt, state)

            retried, retry_delta, retry_transaction, retry_inserted = (
                commit_linear_variation_margin(reopened, opening, settlement)
            )
            self.assertFalse(retry_inserted)
            self.assertEqual(retried, state)
            self.assertEqual(retry_delta, Decimal("0"))
            self.assertIsNone(retry_transaction)
            self.assertEqual(
                len(
                    reopened.load_events(
                        "FUTURES_VARIATION_MARGIN",
                        self._aggregate_id(reopened, opening),
                    )
                ),
                1,
            )

            correction = self._settlement(
                contract,
                "period-1",
                "106",
                sequence=1,
                revision=1,
            )
            corrected, correction_delta, correction_tx, correction_inserted = (
                commit_linear_variation_margin(
                    reopened, opening, correction
                )
            )
            self.assertTrue(correction_inserted)
            self.assertEqual(correction_delta, Decimal("20"))
            self.assertIsNotNone(correction_tx)
            self.assertEqual(
                corrected.cumulative_variation_margin,
                Decimal("120"),
            )
            self.assertEqual(
                restore_linear_variation_margin(JournalStore(path), opening),
                corrected,
            )
            events = reopened.load_events(
                "FUTURES_VARIATION_MARGIN",
                self._aggregate_id(reopened, opening),
            )
            self.assertEqual(len(events), 2)
            self.assertEqual(
                events[-1]["payload"]["transaction"]["postings"][0]["signed_amount"],
                "20",
            )

    def _aggregate_id(self, store, opening):
        # The durable adapter deliberately keeps aggregate identity private.
        # Discovering it from the exact first event avoids reimplementing its
        # identity grammar in the test.
        with store._connect() as connection:
            rows = connection.execute(
                "SELECT DISTINCT aggregate_id FROM events WHERE aggregate_type = ?",
                ("FUTURES_VARIATION_MARGIN",),
            ).fetchall()
        if rows:
            self.assertEqual(len(rows), 1)
            return rows[0][0]
        # Before the first commit there is no aggregate row; callers only use
        # this helper after a successful commit.
        self.fail("durable futures aggregate is missing")

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
            store = JournalStore(f"{directory}/journal.sqlite3")
            commit_linear_variation_margin(store, opening, first)
            aggregate_id = self._aggregate_id(store, opening)
            with self.assertRaisesRegex(FuturesError, "conflicts"):
                commit_linear_variation_margin(store, opening, conflicting)
            self.assertEqual(
                len(store.load_events("FUTURES_VARIATION_MARGIN", aggregate_id)),
                1,
            )
            self.assertEqual(
                restore_linear_variation_margin(store, opening).last_settlement_price,
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
            store = JournalStore(path)
            state, exact, settled_cash, transaction, inserted = (
                commit_inverse_variation_margin(
                    store,
                    opening,
                    first,
                    settlement_quantum=Decimal("0.00000001"),
                )
            )
            self.assertTrue(inserted)
            self.assertEqual(exact, Fraction(1, 1100))
            self.assertEqual(settled_cash, Decimal("0.00090909"))
            self.assertIsNotNone(transaction)
            self.assertEqual(
                restore_inverse_variation_margin(JournalStore(path), opening),
                state,
            )

            retried, retry_exact, retry_cash, retry_tx, retry_inserted = (
                commit_inverse_variation_margin(
                    JournalStore(path),
                    opening,
                    first,
                    settlement_quantum=Decimal("0.00000001"),
                )
            )
            self.assertFalse(retry_inserted)
            self.assertEqual(retried, state)
            self.assertEqual(retry_exact, Fraction(0, 1))
            self.assertEqual(retry_cash, Decimal("0"))
            self.assertIsNone(retry_tx)

            correction = self._settlement(
                contract,
                "inverse-period",
                "12000",
                sequence=1,
                revision=1,
            )
            corrected, correction_exact, _, correction_tx, correction_inserted = (
                commit_inverse_variation_margin(
                    JournalStore(path),
                    opening,
                    correction,
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
            self.assertEqual(
                restore_inverse_variation_margin(JournalStore(path), opening),
                corrected,
            )


if __name__ == "__main__":
    unittest.main()
