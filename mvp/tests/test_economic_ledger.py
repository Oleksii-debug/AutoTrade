from decimal import Decimal
import unittest

from mvp.autotrade_mvp.economic_ledger import (
    EconomicLedger,
    LedgerEntry,
    LedgerTransaction,
    transaction,
)


EVIDENCE = "sha256:" + "a" * 64
CORRECTION = "sha256:" + "b" * 64


def entry(book, unit, amount):
    return LedgerEntry(book=book, unit=unit, amount=Decimal(amount))


class EconomicLedgerTests(unittest.TestCase):
    def test_binary_float_is_rejected_at_authoritative_posting_boundary(self):
        with self.assertRaisesRegex(TypeError, "Decimal"):
            LedgerEntry(book="cash:settled", unit="USD", amount=1.5)

    def test_transaction_must_balance_each_unit_independently(self):
        with self.assertRaisesRegex(ValueError, "USD=1"):
            transaction(
                transaction_id="t1",
                event_id="fill-1",
                environment="PAPER",
                account_id="acct",
                evidence_digest=EVIDENCE,
                entries=(
                    entry("cash:settled", "USD", "-200"),
                    entry("clearing:cash", "USD", "201"),
                    entry("inventory:SPY", "SPY:share", "2"),
                    entry("clearing:inventory", "SPY:share", "-2"),
                ),
            )

    def test_fill_postings_preserve_cash_and_inventory_units(self):
        ledger = EconomicLedger()
        fill = transaction(
            transaction_id="fill-tx-1",
            event_id="provider-fill-1",
            environment="PAPER",
            account_id="acct",
            evidence_digest=EVIDENCE,
            entries=(
                entry("cash:settled", "USD", "-200"),
                entry("clearing:cash", "USD", "200"),
                entry("inventory:SPY", "SPY:share", "2"),
                entry("clearing:inventory", "SPY:share", "-2"),
            ),
        )
        self.assertTrue(ledger.append(fill))
        self.assertEqual(
            ledger.balance(
                environment="PAPER",
                account_id="acct",
                book="cash:settled",
                unit="USD",
            ),
            Decimal("-200"),
        )
        self.assertEqual(
            ledger.balance(
                environment="PAPER",
                account_id="acct",
                book="inventory:SPY",
                unit="SPY:share",
            ),
            Decimal("2"),
        )

    def test_duplicate_transaction_is_idempotent_but_conflict_fails_closed(self):
        ledger = EconomicLedger()
        first = transaction(
            transaction_id="t1",
            event_id="fill-1",
            environment="PAPER",
            account_id="acct",
            evidence_digest=EVIDENCE,
            entries=(
                entry("cash", "USD", "-10"),
                entry("clearing", "USD", "10"),
            ),
        )
        self.assertTrue(ledger.append(first))
        self.assertFalse(ledger.append(first))
        conflict = LedgerTransaction(
            **{
                **first.__dict__,
                "event_id": "different-event",
            }
        )
        with self.assertRaisesRegex(ValueError, "identity conflict"):
            ledger.append(conflict)

    def test_reversal_is_append_only_and_exact(self):
        ledger = EconomicLedger()
        original = transaction(
            transaction_id="t1",
            event_id="fill-1",
            environment="PAPER",
            account_id="acct",
            evidence_digest=EVIDENCE,
            entries=(
                entry("cash", "USD", "-200"),
                entry("clearing", "USD", "200"),
            ),
        )
        ledger.append(original)
        reversal = ledger.reverse(
            transaction_id="t1",
            reversal_id="t1-reversal",
            event_id="provider-bust-1",
            evidence_digest=CORRECTION,
        )
        self.assertEqual(reversal.reversal_of, "t1")
        self.assertEqual(ledger.projection(), {})
        self.assertEqual(ledger.transactions, (original, reversal))

    def test_second_or_inexact_reversal_is_rejected(self):
        ledger = EconomicLedger()
        original = transaction(
            transaction_id="t1",
            event_id="fill-1",
            environment="LIVE",
            account_id="acct",
            evidence_digest=EVIDENCE,
            entries=(entry("cash", "USD", "-2"), entry("clearing", "USD", "2")),
        )
        ledger.append(original)
        bad = LedgerTransaction(
            transaction_id="bad-reversal",
            event_id="bust",
            environment="LIVE",
            account_id="acct",
            evidence_digest=CORRECTION,
            reversal_of="t1",
            entries=(entry("cash", "USD", "1"), entry("clearing", "USD", "-1")),
        )
        with self.assertRaisesRegex(ValueError, "exactly negate"):
            ledger.append(bad)
        ledger.reverse(
            transaction_id="t1",
            reversal_id="good-reversal",
            event_id="bust",
            evidence_digest=CORRECTION,
        )
        with self.assertRaisesRegex(ValueError, "already has a reversal"):
            ledger.append(
                LedgerTransaction(
                    transaction_id="third",
                    event_id="another-bust",
                    environment="LIVE",
                    account_id="acct",
                    evidence_digest=CORRECTION,
                    reversal_of="t1",
                    entries=(
                        entry("cash", "USD", "2"),
                        entry("clearing", "USD", "-2"),
                    ),
                )
            )

    def test_correction_reverses_original_then_posts_replacement(self):
        ledger = EconomicLedger()
        original = transaction(
            transaction_id="fill-original",
            event_id="fill-provider-1",
            environment="PAPER",
            account_id="acct",
            evidence_digest=EVIDENCE,
            entries=(
                entry("cash", "USD", "-200"),
                entry("clearing", "USD", "200"),
                entry("inventory:XYZ", "XYZ:share", "2"),
                entry("clearing:inventory", "XYZ:share", "-2"),
            ),
        )
        ledger.append(original)
        ledger.reverse(
            transaction_id="fill-original",
            reversal_id="fill-original-reversal",
            event_id="correction-1",
            evidence_digest=CORRECTION,
        )
        corrected = transaction(
            transaction_id="fill-corrected",
            event_id="correction-1",
            environment="PAPER",
            account_id="acct",
            evidence_digest=CORRECTION,
            entries=(
                entry("cash", "USD", "-202"),
                entry("clearing", "USD", "202"),
                entry("inventory:XYZ", "XYZ:share", "2"),
                entry("clearing:inventory", "XYZ:share", "-2"),
            ),
        )
        ledger.append(corrected)
        self.assertEqual(
            ledger.balance(
                environment="PAPER",
                account_id="acct",
                book="cash",
                unit="USD",
            ),
            Decimal("-202"),
        )
        self.assertEqual(
            ledger.balance(
                environment="PAPER",
                account_id="acct",
                book="inventory:XYZ",
                unit="XYZ:share",
            ),
            Decimal("2"),
        )

    def test_external_cash_flow_is_not_strategy_pnl(self):
        ledger = EconomicLedger()
        deposit = transaction(
            transaction_id="deposit-1",
            event_id="provider-deposit-1",
            environment="LIVE",
            account_id="acct",
            evidence_digest=EVIDENCE,
            entries=(
                entry("cash:settled", "USD", "500"),
                entry("capital:external-flow", "USD", "-500"),
            ),
        )
        ledger.append(deposit)
        self.assertEqual(
            ledger.balance(
                environment="LIVE",
                account_id="acct",
                book="pnl:realized",
                unit="USD",
            ),
            Decimal("0"),
        )
        self.assertEqual(
            ledger.balance(
                environment="LIVE",
                account_id="acct",
                book="capital:external-flow",
                unit="USD",
            ),
            Decimal("-500"),
        )

    def test_fees_and_rebates_are_exact_and_can_have_either_sign(self):
        ledger = EconomicLedger()
        fee = transaction(
            transaction_id="fee-1",
            event_id="fill-1-fee",
            environment="PAPER",
            account_id="acct",
            evidence_digest=EVIDENCE,
            entries=(
                entry("cash:settled", "USD", "-1.50"),
                entry("expense:fees", "USD", "1.50"),
            ),
        )
        rebate = transaction(
            transaction_id="rebate-1",
            event_id="fill-2-rebate",
            environment="PAPER",
            account_id="acct",
            evidence_digest=CORRECTION,
            entries=(
                entry("cash:settled", "USD", "0.25"),
                entry("expense:fees", "USD", "-0.25"),
            ),
        )
        ledger.append(fee)
        ledger.append(rebate)
        self.assertEqual(
            ledger.balance(
                environment="PAPER",
                account_id="acct",
                book="expense:fees",
                unit="USD",
            ),
            Decimal("1.25"),
        )

    def test_account_and_environment_balances_never_coalesce(self):
        ledger = EconomicLedger()
        for environment, account, amount in (
            ("PAPER", "a", "10"),
            ("LIVE", "a", "20"),
            ("LIVE", "b", "30"),
        ):
            ledger.append(
                transaction(
                    transaction_id=f"{environment}-{account}",
                    event_id=f"event-{environment}-{account}",
                    environment=environment,
                    account_id=account,
                    evidence_digest=EVIDENCE,
                    entries=(
                        entry("cash", "USD", amount),
                        entry("capital:external-flow", "USD", f"-{amount}"),
                    ),
                )
            )
        self.assertEqual(
            ledger.balance(environment="PAPER", account_id="a", book="cash", unit="USD"),
            Decimal("10"),
        )
        self.assertEqual(
            ledger.balance(environment="LIVE", account_id="a", book="cash", unit="USD"),
            Decimal("20"),
        )
        self.assertEqual(
            ledger.balance(environment="LIVE", account_id="b", book="cash", unit="USD"),
            Decimal("30"),
        )

    def test_audit_digest_is_deterministic_and_order_sensitive(self):
        def make_ledger(order):
            ledger = EconomicLedger()
            for tx_id, amount in order:
                ledger.append(
                    transaction(
                        transaction_id=tx_id,
                        event_id=tx_id,
                        environment="PAPER",
                        account_id="acct",
                        evidence_digest=EVIDENCE,
                        entries=(
                            entry("cash", "USD", amount),
                            entry("clearing", "USD", f"-{amount}"),
                        ),
                    )
                )
            return ledger

        first = make_ledger((("a", "1"), ("b", "2")))
        identical = make_ledger((("a", "1"), ("b", "2")))
        reordered = make_ledger((("b", "2"), ("a", "1")))
        self.assertEqual(first.audit_digest(), identical.audit_digest())
        self.assertNotEqual(first.audit_digest(), reordered.audit_digest())


if __name__ == "__main__":
    unittest.main()
