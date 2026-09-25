from datetime import date, datetime, timezone
from decimal import Decimal
import unittest

from mvp.autotrade_mvp.accounting import (
    EconomicBook,
    book_equity_fill,
    book_external_cash_flow,
    reverse_transaction,
)
from mvp.autotrade_mvp.reservations import ReservationBook
from mvp.autotrade_mvp.settlement import (
    BuyingPowerEvidence,
    SettlementAccountScope,
    SettlementBook,
    SettlementCheckpoint,
    SettlementConflict,
    SettlementEvidence,
    SettlementObligation,
    SettlementRuleBinding,
    equity_cash_obligation,
    equity_cash_obligation_from_transaction,
)


def evidence(
    obligation_id: str,
    ref: str,
    *,
    day: int = 25,
    hour: int = 12,
) -> SettlementEvidence:
    return SettlementEvidence(
        obligation_id=obligation_id,
        evidence_ref=ref,
        observed_at=datetime(2026, 9, day, hour, tzinfo=timezone.utc),
    )


class SettlementBookTests(unittest.TestCase):
    def test_obligation_constructor_canonicalizes_financial_fields(self):
        obligation = SettlementObligation(
            " obligation-1 ",
            " fill-1 ",
            " usd ",
            "10.50",
            date(2026, 9, 24),
            date(2026, 9, 25),
        )
        self.assertEqual(obligation.obligation_id, "obligation-1")
        self.assertEqual(obligation.cause_event_id, "fill-1")
        self.assertEqual(obligation.currency, "USD")
        self.assertIsInstance(obligation.amount, Decimal)
        self.assertEqual(obligation.amount, Decimal("10.50"))

        book = SettlementBook(obligations=(obligation,))
        self.assertEqual(
            book.snapshot("usd").unsettled_receivable,
            Decimal("10.50"),
        )

    def test_obligation_rejects_non_date_temporal_fields(self):
        with self.assertRaises(TypeError):
            SettlementObligation(
                "x",
                "event",
                "USD",
                Decimal("1"),
                "2026-09-24",
                date(2026, 9, 25),
            )

    def test_initial_settled_cash_rejects_duplicate_normalized_currency_codes(self):
        with self.assertRaisesRegex(
            SettlementConflict,
            "duplicate normalized currency codes",
        ):
            SettlementBook(
                settled_cash={
                    "USD": "100",
                    " usd ": "999",
                }
            )

    def test_unsettled_sale_proceeds_are_not_spendable(self):
        book = SettlementBook(settled_cash={"USD": "100"})
        sale = equity_cash_obligation(
            obligation_id="sale-1",
            cause_event_id="fill-1",
            settlement_currency="USD",
            side="SELL",
            quantity="2",
            price="50",
            fee="1",
            trade_date=date(2026, 9, 24),
            settlement_date=date(2026, 9, 26),
        )
        book.add(sale)
        snapshot = book.snapshot("USD")
        self.assertEqual(snapshot.settled_cash, Decimal("100"))
        self.assertEqual(snapshot.unsettled_receivable, Decimal("99"))
        self.assertEqual(snapshot.unsettled_payable, Decimal("0"))
        self.assertEqual(snapshot.economic_cash, Decimal("199"))
        self.assertEqual(book.available_to_spend("USD"), Decimal("100"))

    def test_buy_creates_unsettled_payable_and_settles_exactly_once(self):
        book = SettlementBook(settled_cash={"USD": "1000"})
        buy = equity_cash_obligation(
            obligation_id="buy-1",
            cause_event_id="fill-1",
            settlement_currency="USD",
            side="BUY",
            quantity="2",
            price="100",
            fee="1",
            trade_date=date(2026, 9, 24),
            settlement_date=date(2026, 9, 25),
        )
        self.assertTrue(book.add(buy))
        before = book.snapshot("USD")
        self.assertEqual(before.unsettled_payable, Decimal("201"))
        self.assertEqual(before.economic_cash, Decimal("799"))
        self.assertEqual(book.available_to_spend("USD"), Decimal("799"))
        with self.assertRaisesRegex(SettlementConflict, "before contractual"):
            book.settle("buy-1", as_of=date(2026, 9, 24), settlement_evidence=evidence("buy-1", "provider:cash:buy-1", day=24))
        self.assertTrue(book.settle("buy-1", as_of=date(2026, 9, 25), settlement_evidence=evidence("buy-1", "provider:cash:buy-1", day=25)))
        self.assertFalse(book.settle("buy-1", as_of=date(2026, 9, 26), settlement_evidence=evidence("buy-1", "provider:cash:buy-1", day=25)))
        after = book.snapshot("USD")
        self.assertEqual(after.settled_cash, Decimal("799"))
        self.assertEqual(after.unsettled_payable, Decimal("0"))

    def test_settle_due_orders_deterministically(self):
        book = SettlementBook(settled_cash={"USD": "0"})
        for obligation in (
            SettlementObligation("b", "e2", "USD", Decimal("3"), date(2026, 9, 20), date(2026, 9, 22)),
            SettlementObligation("a", "e1", "USD", Decimal("2"), date(2026, 9, 20), date(2026, 9, 22)),
            SettlementObligation("c", "e3", "USD", Decimal("5"), date(2026, 9, 20), date(2026, 9, 23)),
        ):
            book.add(obligation)
        self.assertEqual(
            book.settle_due(
                as_of=date(2026, 9, 22),
                settlement_evidence={
                    "a": evidence("a", "provider:cash:a", day=22),
                    "b": evidence("b", "provider:cash:b", day=22),
                },
            ),
            ("a", "b"),
        )
        self.assertEqual(book.snapshot("USD").settled_cash, Decimal("5"))
        self.assertEqual(book.snapshot("USD").unsettled_receivable, Decimal("5"))

    def test_duplicate_identity_is_idempotent_but_changed_content_conflicts(self):
        original = SettlementObligation(
            "x", "event", "EUR", Decimal("10"), date(2026, 9, 20), date(2026, 9, 22)
        )
        book = SettlementBook()
        self.assertTrue(book.add(original))
        self.assertFalse(book.add(original))
        changed = SettlementObligation(
            "x", "event", "EUR", Decimal("11"), date(2026, 9, 20), date(2026, 9, 22)
        )
        with self.assertRaises(SettlementConflict):
            book.add(changed)

    def test_same_economic_cause_component_cannot_duplicate_but_distinct_components_can(self):
        first = SettlementObligation(
            "obligation-a",
            " provider-fill-1 ",
            "USD",
            Decimal("-200"),
            date(2026, 9, 24),
            date(2026, 9, 26),
            "PRINCIPAL",
        )
        duplicate_component = SettlementObligation(
            "obligation-b",
            "provider-fill-1",
            "USD",
            Decimal("-200"),
            date(2026, 9, 24),
            date(2026, 9, 26),
            "PRINCIPAL",
        )
        third_currency_fee = SettlementObligation(
            "obligation-fee",
            "provider-fill-1",
            "BNB",
            Decimal("-0.01"),
            date(2026, 9, 24),
            date(2026, 9, 26),
            "FEE",
        )
        book = SettlementBook(settled_cash={"USD": "1000", "BNB": "1"})
        self.assertTrue(book.add(first))
        with self.assertRaisesRegex(SettlementConflict, "component_id"):
            book.add(duplicate_component)
        self.assertTrue(book.add(third_currency_fee))
        self.assertEqual(book.snapshot("USD").unsettled_payable, Decimal("200"))
        self.assertEqual(book.snapshot("BNB").unsettled_payable, Decimal("0.01"))
        self.assertEqual(book.available_to_spend("USD"), Decimal("800"))
        self.assertEqual(book.available_to_spend("BNB"), Decimal("0.99"))

    def test_restart_constructor_rejects_duplicate_economic_cause_components(self):
        obligations = (
            SettlementObligation(
                "a", "same-fill", "USD", Decimal("-10"),
                date(2026, 9, 24), date(2026, 9, 25), "PRINCIPAL",
            ),
            SettlementObligation(
                "b", "same-fill", "USD", Decimal("-10"),
                date(2026, 9, 24), date(2026, 9, 25), "PRINCIPAL",
            ),
        )
        with self.assertRaisesRegex(SettlementConflict, "component_id"):
            SettlementBook(obligations=obligations)

    def test_reserve_reduces_only_settled_spendable_cash(self):
        book = SettlementBook(settled_cash={"USD": "100"})
        book.add(SettlementObligation(
            "sale", "event", "USD", Decimal("500"), date(2026, 9, 20), date(2026, 9, 23)
        ))
        self.assertEqual(book.available_to_spend("USD", reserve="30"), Decimal("70"))
        self.assertEqual(book.available_to_spend("USD", reserve="130"), Decimal("0"))

    def test_multi_currency_obligations_do_not_cross_net(self):
        book = SettlementBook(settled_cash={"USD": "100", "EUR": "50"})
        book.add(SettlementObligation(
            "usd-r", "u1", "USD", Decimal("20"), date(2026, 9, 20), date(2026, 9, 22)
        ))
        book.add(SettlementObligation(
            "eur-p", "e1", "EUR", Decimal("-10"), date(2026, 9, 20), date(2026, 9, 22)
        ))
        self.assertEqual(book.snapshot("USD").economic_cash, Decimal("120"))
        self.assertEqual(book.snapshot("EUR").economic_cash, Decimal("40"))

    def test_invalid_dates_zero_amount_float_and_negative_reserve_fail_closed(self):
        with self.assertRaises(ValueError):
            SettlementObligation(
                "x", "e", "USD", Decimal("1"), date(2026, 9, 23), date(2026, 9, 22)
            )
        with self.assertRaises(ValueError):
            SettlementObligation(
                "x", "e", "USD", Decimal("0"), date(2026, 9, 22), date(2026, 9, 22)
            )
        with self.assertRaises(TypeError):
            equity_cash_obligation(
                obligation_id="x",
                cause_event_id="e",
                settlement_currency="USD",
                side="BUY",
                quantity=1,
                price=100.1,
                trade_date=date(2026, 9, 22),
                settlement_date=date(2026, 9, 22),
            )
        with self.assertRaises(ValueError):
            SettlementBook(settled_cash={"USD": "10"}).available_to_spend("USD", reserve="-1")

    def test_fee_is_applied_in_settlement_currency_without_hidden_conversion(self):
        buy = equity_cash_obligation(
            obligation_id="buy",
            cause_event_id="fill",
            settlement_currency="EUR",
            side="BUY",
            quantity="1.5",
            price="20",
            fee="0.25",
            trade_date=date(2026, 9, 24),
            settlement_date=date(2026, 9, 25),
        )
        sell = equity_cash_obligation(
            obligation_id="sell",
            cause_event_id="fill2",
            settlement_currency="EUR",
            side="SELL",
            quantity="1.5",
            price="20",
            fee="0.25",
            trade_date=date(2026, 9, 24),
            settlement_date=date(2026, 9, 25),
        )
        self.assertEqual(buy.amount, Decimal("-30.25"))
        self.assertEqual(sell.amount, Decimal("29.75"))


    def test_payable_and_explicit_reserve_are_both_conservative(self):
        book = SettlementBook(settled_cash={"USD": "1000"})
        book.add(SettlementObligation(
            "buy", "fill-buy", "USD", Decimal("-201"),
            date(2026, 9, 24), date(2026, 9, 26),
        ))
        book.add(SettlementObligation(
            "sale", "fill-sale", "USD", Decimal("500"),
            date(2026, 9, 24), date(2026, 9, 26),
        ))
        self.assertEqual(book.available_to_spend("USD"), Decimal("799"))
        self.assertEqual(book.available_to_spend("USD", reserve="99"), Decimal("700"))

    def test_payables_do_not_cross_net_between_currencies(self):
        book = SettlementBook(settled_cash={"USD": "1000", "EUR": "100"})
        book.add(SettlementObligation(
            "usd-buy", "fill-usd", "USD", Decimal("-201"),
            date(2026, 9, 24), date(2026, 9, 26),
        ))
        book.add(SettlementObligation(
            "eur-sale", "fill-eur", "EUR", Decimal("500"),
            date(2026, 9, 24), date(2026, 9, 26),
        ))
        self.assertEqual(book.available_to_spend("USD"), Decimal("799"))
        self.assertEqual(book.available_to_spend("EUR"), Decimal("100"))

    def test_restart_reconstruction_preserves_settlement_projection(self):
        obligation = SettlementObligation(
            "buy", "fill", "USD", Decimal("-201"),
            date(2026, 9, 24), date(2026, 9, 25),
        )
        book = SettlementBook(settled_cash={"USD": "1000"}, obligations=(obligation,))
        self.assertTrue(
            book.settle(
                "buy",
                as_of=date(2026, 9, 25),
                settlement_evidence=evidence("buy", "provider:cash:buy", day=25),
            )
        )
        snapshot = book.snapshot("USD")

        restored = SettlementBook(
            settled_cash={"USD": snapshot.settled_cash},
            obligations=(obligation,),
            settled_obligation_evidence={"buy": evidence("buy", "provider:cash:buy", day=25)},
        )
        self.assertTrue(restored.is_settled("buy"))
        self.assertEqual(restored.snapshot("USD"), snapshot)
        self.assertEqual(restored.available_to_spend("USD"), Decimal("799"))
        self.assertFalse(
            restored.settle(
                "buy",
                as_of=date(2026, 9, 26),
                settlement_evidence=evidence("buy", "provider:cash:buy", day=25),
            )
        )

    def test_restart_rejects_unknown_settled_identity(self):
        with self.assertRaisesRegex(SettlementConflict, "unknown obligation"):
            SettlementBook(
                settled_cash={"USD": "100"},
                settled_obligation_evidence={"missing": evidence("missing", "provider:cash:missing", day=25)},
            )


    def test_restart_rejects_duplicate_normalized_settlement_evidence_identity(self):
        obligation = SettlementObligation(
            "cash-1", "fill-1", "USD", Decimal("10"),
            date(2026, 9, 20), date(2026, 9, 22),
        )
        with self.assertRaisesRegex(
            SettlementConflict,
            "duplicate normalized obligation ids",
        ):
            SettlementBook(
                settled_cash={"USD": "10"},
                obligations=(obligation,),
                settled_obligation_evidence={
                    "cash-1": evidence("cash-1", "provider:statement:1", day=22),
                    " cash-1 ": evidence("cash-1", "provider:statement:2", day=22),
                },
            )

    def test_reservation_to_settlement_handoff_never_double_locks_same_fill(self):
        reservations = ReservationBook()
        settlement = SettlementBook(settled_cash={"USD": "1000"})
        reservations.reserve(
            reservation_id="reservation-buy-1",
            intent_id="intent-buy-1",
            requirements={"CASH:USD": "201"},
            available={"CASH:USD": "1000"},
        )

        before_fill_reserve = reservations.total_reserved("CASH:USD")
        self.assertEqual(before_fill_reserve, Decimal("201"))
        self.assertEqual(
            settlement.available_to_spend("USD", reserve=before_fill_reserve),
            Decimal("799"),
        )
        self.assertEqual(settlement.snapshot("USD").unsettled_payable, Decimal("0"))

        reservations.consume("reservation-buy-1", {"CASH:USD": "201"})
        filled = reservations.mark_terminal(
            "reservation-buy-1",
            outcome="FILLED",
            resolution_evidence="provider-execution-fill-1",
        )
        self.assertEqual(filled.state, "FILLED")
        self.assertEqual(reservations.total_reserved("CASH:USD"), Decimal("0"))

        settlement.add(
            equity_cash_obligation(
                obligation_id="settlement-buy-1",
                cause_event_id="provider-execution-fill-1",
                settlement_currency="USD",
                side="BUY",
                quantity="2",
                price="100",
                fee="1",
                trade_date=date(2026, 9, 24),
                settlement_date=date(2026, 9, 26),
            )
        )
        after_fill_reserve = reservations.total_reserved("CASH:USD")
        self.assertEqual(after_fill_reserve, Decimal("0"))
        self.assertEqual(settlement.snapshot("USD").unsettled_payable, Decimal("201"))
        self.assertEqual(
            settlement.available_to_spend("USD", reserve=after_fill_reserve),
            Decimal("799"),
        )
        self.assertNotEqual(
            settlement.available_to_spend("USD", reserve=after_fill_reserve),
            Decimal("598"),
        )

    def test_working_or_unknown_reservation_does_not_create_settlement_payable(self):
        reservations = ReservationBook()
        settlement = SettlementBook(settled_cash={"USD": "1000"})
        reservations.reserve(
            reservation_id="reservation-unknown",
            intent_id="intent-unknown",
            requirements={"CASH:USD": "201"},
            available={"CASH:USD": "1000"},
        )
        reservations.mark_unknown("reservation-unknown")

        reserve = reservations.total_reserved("CASH:USD")
        self.assertEqual(reserve, Decimal("201"))
        self.assertEqual(settlement.snapshot("USD").unsettled_payable, Decimal("0"))
        self.assertEqual(
            settlement.available_to_spend("USD", reserve=reserve),
            Decimal("799"),
        )


    def test_due_date_without_provider_evidence_does_not_make_receivable_spendable(self):
        book = SettlementBook(settled_cash={"USD": "100"})
        book.add(
            SettlementObligation(
                "sale-due",
                "fill-due",
                "USD",
                Decimal("50"),
                date(2026, 9, 20),
                date(2026, 9, 22),
            )
        )
        self.assertEqual(
            book.settle_due(
                as_of=date(2026, 9, 25),
                settlement_evidence={},
            ),
            (),
        )
        self.assertEqual(book.snapshot("USD").unsettled_receivable, Decimal("50"))
        self.assertEqual(book.available_to_spend("USD"), Decimal("100"))

    def test_settle_due_rejects_unknown_or_malformed_evidence_identity(self):
        book = SettlementBook(settled_cash={"USD": "0"})
        book.add(
            SettlementObligation(
                "cash-1",
                "fill-1",
                "USD",
                Decimal("10"),
                date(2026, 9, 20),
                date(2026, 9, 22),
            )
        )
        with self.assertRaisesRegex(SettlementConflict, "unknown obligation"):
            book.settle_due(
                as_of=date(2026, 9, 22),
                settlement_evidence={"cash-typo": evidence("cash-typo", "provider:statement:1", day=22)},
            )
        with self.assertRaises(ValueError):
            book.settle_due(
                as_of=date(2026, 9, 22),
                settlement_evidence={"": evidence("", "provider:statement:1", day=22)},
            )
        with self.assertRaises(ValueError):
            book.settle_due(
                as_of=date(2026, 9, 22),
                settlement_evidence={"cash-1": evidence("cash-1", " ", day=22)},
            )

    def test_settlement_retry_with_different_evidence_fails_closed(self):
        book = SettlementBook(settled_cash={"USD": "0"})
        book.add(
            SettlementObligation(
                "cash-1",
                "fill-1",
                "USD",
                Decimal("10"),
                date(2026, 9, 20),
                date(2026, 9, 22),
            )
        )
        self.assertTrue(
            book.settle(
                "cash-1",
                as_of=date(2026, 9, 22),
                settlement_evidence=evidence("cash-1", "provider:statement:1", day=22),
            )
        )
        with self.assertRaisesRegex(SettlementConflict, "different settlement evidence"):
            book.settle(
                "cash-1",
                as_of=date(2026, 9, 22),
                settlement_evidence=evidence("cash-1", "provider:statement:2", day=22),
            )


    def test_history_replay_derives_cash_from_initial_balance_and_evidence(self):
        obligation = SettlementObligation(
            "buy-history",
            "fill-history",
            "USD",
            Decimal("-201"),
            date(2026, 9, 24),
            date(2026, 9, 25),
        )
        running = SettlementBook(
            settled_cash={"USD": "1000"},
            obligations=(obligation,),
        )
        self.assertTrue(
            running.settle(
                "buy-history",
                as_of=date(2026, 9, 25),
                settlement_evidence=evidence("buy-history", "provider:statement:history", day=25),
            )
        )
        self.assertEqual(running.snapshot("USD").settled_cash, Decimal("799"))

        restored = SettlementBook.from_history(
            checkpoint=SettlementCheckpoint.create(
                checkpoint_id="opening-balance",
                settled_cash={"USD": "1000"},
                settled_obligation_evidence={},
            ),
            obligations=running.obligations,
            settled_obligation_evidence=running.settled_obligation_evidence,
        )
        self.assertEqual(restored.snapshot("USD").settled_cash, Decimal("799"))
        self.assertEqual(restored.available_to_spend("USD"), Decimal("799"))
        self.assertEqual(
            restored.settled_obligation_evidence,
            {"buy-history": evidence("buy-history", "provider:statement:history", day=25)},
        )
        self.assertFalse(
            restored.settle(
                "buy-history",
                as_of=date(2026, 9, 26),
                settlement_evidence=evidence("buy-history", "provider:statement:history", day=25),
            )
        )

    def test_history_replay_rejects_duplicate_normalized_evidence_ids(self):
        obligation = SettlementObligation(
            "cash-1",
            "fill-1",
            "USD",
            Decimal("10"),
            date(2026, 9, 20),
            date(2026, 9, 22),
        )
        with self.assertRaisesRegex(
            SettlementConflict,
            "duplicate normalized obligation ids",
        ):
            SettlementBook.from_history(
                checkpoint=SettlementCheckpoint.create(
                    checkpoint_id="opening",
                    settled_cash={"USD": "0"},
                    settled_obligation_evidence={},
                ),
                obligations=(obligation,),
                settled_obligation_evidence={
                    "cash-1": evidence("cash-1", "provider:statement:1", day=22),
                    " cash-1 ": evidence("cash-1", "provider:statement:1", day=22),
                },
            )

    def test_delayed_settlement_evidence_is_not_backdated_or_spendable_early(self):
        obligation = SettlementObligation(
            "sale-delayed",
            "fill-delayed",
            "USD",
            Decimal("50"),
            date(2026, 9, 20),
            date(2026, 9, 22),
        )
        book = SettlementBook(
            settled_cash={"USD": "100"},
            obligations=(obligation,),
        )
        delayed = evidence(
            "sale-delayed",
            "provider:statement:delayed",
            day=24,
            hour=15,
        )
        self.assertEqual(
            book.settle_due(
                as_of=date(2026, 9, 23),
                settlement_evidence={"sale-delayed": delayed},
            ),
            (),
        )
        self.assertEqual(book.available_to_spend("USD"), Decimal("100"))
        self.assertEqual(
            book.settle_due(
                as_of=date(2026, 9, 24),
                settlement_evidence={"sale-delayed": delayed},
            ),
            ("sale-delayed",),
        )
        self.assertEqual(book.available_to_spend("USD"), Decimal("150"))
        self.assertEqual(
            book.settled_obligation_evidence["sale-delayed"].observed_at,
            datetime(2026, 9, 24, 15, tzinfo=timezone.utc),
        )

    def test_settlement_evidence_requires_timezone_and_matching_identity(self):
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            SettlementEvidence(
                obligation_id="x",
                evidence_ref="provider:x",
                observed_at=datetime(2026, 9, 22, 12),
            )

        obligation = SettlementObligation(
            "x",
            "fill-x",
            "USD",
            Decimal("10"),
            date(2026, 9, 20),
            date(2026, 9, 22),
        )
        book = SettlementBook(obligations=(obligation,))
        with self.assertRaisesRegex(SettlementConflict, "does not match"):
            book.settle(
                "x",
                as_of=date(2026, 9, 22),
                settlement_evidence=evidence("other", "provider:x", day=22),
            )

    def test_checkpoint_plus_full_history_does_not_double_apply_settlement(self):
        obligation = SettlementObligation(
            "buy-checkpoint",
            "fill-checkpoint",
            "USD",
            Decimal("-201"),
            date(2026, 9, 24),
            date(2026, 9, 25),
        )
        running = SettlementBook(
            settled_cash={"USD": "1000"},
            obligations=(obligation,),
        )
        running.settle(
            "buy-checkpoint",
            as_of=date(2026, 9, 25),
            settlement_evidence=evidence("buy-checkpoint", "provider:statement:checkpoint", day=25),
        )
        checkpoint = running.checkpoint("after-buy")
        full_history = running.settled_obligation_evidence

        restored = SettlementBook.from_history(
            checkpoint=checkpoint,
            obligations=running.obligations,
            settled_obligation_evidence=full_history,
        )
        self.assertEqual(restored.snapshot("USD").settled_cash, Decimal("799"))
        self.assertFalse(
            restored.settle(
                "buy-checkpoint",
                as_of=date(2026, 9, 26),
                settlement_evidence=evidence("buy-checkpoint", "provider:statement:checkpoint", day=25),
            )
        )

    def test_checkpoint_replays_only_strict_suffix(self):
        first = SettlementObligation(
            "a",
            "fill-a",
            "USD",
            Decimal("-100"),
            date(2026, 9, 20),
            date(2026, 9, 22),
        )
        second = SettlementObligation(
            "b",
            "fill-b",
            "USD",
            Decimal("50"),
            date(2026, 9, 21),
            date(2026, 9, 23),
        )
        prefix = SettlementBook(
            settled_cash={"USD": "1000"},
            obligations=(first, second),
        )
        prefix.settle(
            "a",
            as_of=date(2026, 9, 22),
            settlement_evidence=evidence("a", "provider:a", day=22),
        )
        checkpoint = prefix.checkpoint("after-a")

        restored = SettlementBook.from_history(
            checkpoint=checkpoint,
            obligations=(first, second),
            settled_obligation_evidence={
                "a": evidence("a", "provider:a", day=22),
                "b": evidence("b", "provider:b", day=23),
            },
        )
        self.assertEqual(restored.snapshot("USD").settled_cash, Decimal("950"))
        self.assertTrue(restored.is_settled("a"))
        self.assertTrue(restored.is_settled("b"))

    def test_checkpoint_history_identity_mismatch_fails_before_cash_mutation(self):
        obligation = SettlementObligation(
            "a",
            "fill-a",
            "USD",
            Decimal("-100"),
            date(2026, 9, 20),
            date(2026, 9, 22),
        )
        checkpoint = SettlementCheckpoint.create(
            checkpoint_id="after-a",
            settled_cash={"USD": "900"},
            settled_obligation_evidence={"a": evidence("a", "provider:a", day=22)},
        )
        with self.assertRaisesRegex(SettlementConflict, "conflicts"):
            SettlementBook.from_history(
                checkpoint=checkpoint,
                obligations=(obligation,),
                settled_obligation_evidence={"a": evidence("a", "provider:changed", day=22)},
            )

    def test_settlement_temporal_boundaries_require_exact_date_values(self):
        obligation = SettlementObligation(
            "cash-date",
            "fill-date",
            "USD",
            Decimal("10"),
            date(2026, 9, 20),
            date(2026, 9, 22),
        )
        book = SettlementBook(obligations=(obligation,))
        with self.assertRaisesRegex(TypeError, "as_of"):
            book.settle(
                "cash-date",
                as_of="2026-09-22",
                settlement_evidence=evidence("cash-date", "provider:statement:date", day=22),
            )
        with self.assertRaisesRegex(TypeError, "as_of"):
            book.settle_due(
                as_of="2026-09-22",
                settlement_evidence={"cash-date": evidence("cash-date", "provider:statement:date", day=22)},
            )



class EconomicSettlementCapitalTests(unittest.TestCase):
    def setUp(self):
        self.scope = SettlementAccountScope(
            provider_id="TEST_PROVIDER",
            account_id="cash-1",
            environment="PAPER",
        )

    def rule(
        self,
        *,
        version="1",
        effective_from=date(2026, 9, 1),
        effective_to=None,
    ):
        return SettlementRuleBinding(
            rule_id="cash-equity-settlement",
            rule_version=version,
            scope=self.scope,
            instrument_version=f"ABC@{version}",
            settlement_currency="USD",
            effective_from=effective_from,
            effective_to=effective_to,
            evidence_refs=(f"instrument:ABC@{version}", f"rule:{version}"),
        )

    def funded_book(self):
        book = EconomicBook()
        book.append(
            book_external_cash_flow(
                transaction_id="deposit",
                cause_event_id="deposit:1",
                currency="USD",
                amount="1000",
            )
        )
        return book

    def fill(self, *, transaction_id, side, at="2026-09-24T14:00:00Z"):
        return book_equity_fill(
            transaction_id=transaction_id,
            cause_event_id=f"event:{transaction_id}",
            instrument="ABC",
            settlement_currency="USD",
            side=side,
            quantity="1",
            price="100",
            economic_effective_at=at,
            economic_order_key=transaction_id,
            observed_at=at,
        )

    def bound(self, transaction, *, obligation_id, settlement_day=25, rule=None):
        return equity_cash_obligation_from_transaction(
            transaction,
            obligation_id=obligation_id,
            instrument="ABC",
            settlement_currency="USD",
            settlement_date=date(2026, 9, settlement_day),
            rule_binding=rule or self.rule(),
        )

    def test_sell_is_economic_cash_but_not_available_before_settlement(self):
        book = self.funded_book()
        sold = self.fill(transaction_id="sell-1", side="SELL")
        book.append(sold)
        obligation = self.bound(sold, obligation_id="ob-sell-1")

        settlement = SettlementBook.from_economic_book(
            economic_book=book,
            obligations=(obligation,),
        )
        snapshot = settlement.snapshot("USD")
        self.assertEqual(book.cash("USD"), Decimal("1100"))
        self.assertEqual(snapshot.settled_cash, Decimal("1000"))
        self.assertEqual(snapshot.unsettled_receivable, Decimal("100"))
        capital = settlement.available_capital(
            scope=self.scope,
            currency="USD",
            as_of=datetime(2026, 9, 24, 18, tzinfo=timezone.utc),
        )
        self.assertEqual(capital.available_cash, Decimal("1000"))
        self.assertEqual(capital.available_capital, Decimal("1000"))
        self.assertFalse(capital.blocks_new_risk)

    def test_provider_settlement_releases_sell_proceeds_once_across_restart(self):
        book = self.funded_book()
        sold = self.fill(transaction_id="sell-restart", side="SELL")
        book.append(sold)
        obligation = self.bound(sold, obligation_id="ob-sell-restart")
        settled = evidence(
            "ob-sell-restart",
            "provider:settlement:sell-restart",
            day=25,
            hour=15,
        )

        rebuilt = SettlementBook.from_economic_book(
            economic_book=book,
            obligations=(obligation,),
            settled_obligation_evidence={"ob-sell-restart": settled},
        )
        self.assertEqual(rebuilt.available_to_spend("USD"), Decimal("1100"))
        checkpoint = rebuilt.checkpoint("after-settlement")
        restored = SettlementBook.from_history(
            checkpoint=checkpoint,
            obligations=rebuilt.obligations,
            settled_obligation_evidence=rebuilt.settled_obligation_evidence,
        )
        self.assertEqual(restored.available_to_spend("USD"), Decimal("1100"))
        self.assertFalse(
            restored.settle(
                "ob-sell-restart",
                as_of=date(2026, 9, 26),
                settlement_evidence=settled,
            )
        )
        self.assertEqual(restored.available_to_spend("USD"), Decimal("1100"))

    def test_buy_creates_payable_and_consumes_availability_immediately(self):
        book = self.funded_book()
        bought = self.fill(transaction_id="buy-1", side="BUY")
        book.append(bought)
        obligation = self.bound(bought, obligation_id="ob-buy-1")
        settlement = SettlementBook.from_economic_book(
            economic_book=book,
            obligations=(obligation,),
        )
        snapshot = settlement.snapshot("USD")
        self.assertEqual(book.cash("USD"), Decimal("900"))
        self.assertEqual(snapshot.settled_cash, Decimal("1000"))
        self.assertEqual(snapshot.unsettled_payable, Decimal("100"))
        self.assertEqual(settlement.available_to_spend("USD"), Decimal("900"))

    def test_margin_buying_power_is_separate_evidence_not_settled_cash(self):
        book = self.funded_book()
        bought = self.fill(transaction_id="buy-margin", side="BUY")
        book.append(bought)
        settlement = SettlementBook.from_economic_book(
            economic_book=book,
            obligations=(self.bound(bought, obligation_id="ob-margin"),),
        )
        buying_power = BuyingPowerEvidence(
            evidence_id="bp-1",
            scope=self.scope,
            currency="USD",
            additional_credit="250",
            observed_at=datetime(2026, 9, 24, 15, tzinfo=timezone.utc),
            valid_until=datetime(2026, 9, 24, 17, tzinfo=timezone.utc),
            evidence_refs=("provider:buying-power:1",),
        )
        capital = settlement.available_capital(
            scope=self.scope,
            currency="USD",
            as_of=datetime(2026, 9, 24, 16, tzinfo=timezone.utc),
            buying_power_evidence=buying_power,
            require_buying_power_evidence=True,
        )
        self.assertEqual(capital.settled_cash, Decimal("1000"))
        self.assertEqual(capital.available_cash, Decimal("900"))
        self.assertEqual(capital.additional_buying_power, Decimal("250"))
        self.assertEqual(capital.available_capital, Decimal("1150"))

        stale = settlement.available_capital(
            scope=self.scope,
            currency="USD",
            as_of=datetime(2026, 9, 24, 17, tzinfo=timezone.utc),
            buying_power_evidence=buying_power,
            require_buying_power_evidence=True,
        )
        self.assertEqual(stale.additional_buying_power, Decimal("0"))
        self.assertTrue(stale.blocks_new_risk)

    def test_rule_versions_apply_only_to_trades_in_their_effective_window(self):
        old_rule = self.rule(
            version="1",
            effective_from=date(2026, 9, 1),
            effective_to=date(2026, 9, 25),
        )
        new_rule = self.rule(
            version="2",
            effective_from=date(2026, 9, 25),
        )
        old_fill = self.fill(
            transaction_id="old-rule",
            side="SELL",
            at="2026-09-24T14:00:00Z",
        )
        new_fill = self.fill(
            transaction_id="new-rule",
            side="SELL",
            at="2026-09-25T14:00:00Z",
        )
        old_obligation = self.bound(
            old_fill, obligation_id="old-ob", rule=old_rule
        )
        new_obligation = self.bound(
            new_fill, obligation_id="new-ob", rule=new_rule, settlement_day=28
        )
        self.assertNotEqual(
            old_obligation.rule_binding.digest,
            new_obligation.rule_binding.digest,
        )
        with self.assertRaisesRegex(SettlementConflict, "not effective"):
            self.bound(
                old_fill,
                obligation_id="wrong-ob",
                rule=new_rule,
                settlement_day=28,
            )

    def test_overdue_missing_provider_settlement_is_unknown_and_blocks_risk(self):
        book = self.funded_book()
        sold = self.fill(transaction_id="sell-late", side="SELL")
        book.append(sold)
        settlement = SettlementBook.from_economic_book(
            economic_book=book,
            obligations=(self.bound(sold, obligation_id="ob-late"),),
        )
        capital = settlement.available_capital(
            scope=self.scope,
            currency="USD",
            as_of=datetime(2026, 9, 26, 9, tzinfo=timezone.utc),
        )
        self.assertEqual(capital.overdue_obligation_ids, ("ob-late",))
        self.assertTrue(capital.blocks_new_risk)
        self.assertEqual(capital.available_cash, Decimal("1000"))

    def test_reservation_authority_uses_available_cash_not_trade_date_cash(self):
        book = self.funded_book()
        sold = self.fill(transaction_id="sell-reservation", side="SELL")
        book.append(sold)
        settlement = SettlementBook.from_economic_book(
            economic_book=book,
            obligations=(self.bound(sold, obligation_id="ob-reservation"),),
        )
        capital = settlement.available_capital(
            scope=self.scope,
            currency="USD",
            as_of=datetime(2026, 9, 24, 18, tzinfo=timezone.utc),
        )
        reservations = ReservationBook()
        reservations.reserve_from_capital(
            reservation_id="reserve-1000",
            intent_id="intent-1000",
            requirements={"CASH:USD": "1000"},
            capital=capital,
        )
        with self.assertRaisesRegex(Exception, "Insufficient"):
            reservations.reserve_from_capital(
                reservation_id="reserve-extra",
                intent_id="intent-extra",
                requirements={"CASH:USD": "1"},
                capital=capital,
            )
        self.assertEqual(book.cash("USD"), Decimal("1100"))
        self.assertEqual(capital.available_cash, Decimal("1000"))

    def test_overdue_unknown_capital_is_rejected_by_reservation_authority(self):
        book = self.funded_book()
        sold = self.fill(transaction_id="sell-block", side="SELL")
        book.append(sold)
        settlement = SettlementBook.from_economic_book(
            economic_book=book,
            obligations=(self.bound(sold, obligation_id="ob-block"),),
        )
        capital = settlement.available_capital(
            scope=self.scope,
            currency="USD",
            as_of=datetime(2026, 9, 26, 9, tzinfo=timezone.utc),
        )
        self.assertTrue(capital.blocks_new_risk)
        with self.assertRaisesRegex(Exception, "blocks new risk"):
            ReservationBook().reserve_from_capital(
                reservation_id="blocked",
                intent_id="blocked-intent",
                requirements={"CASH:USD": "1"},
                capital=capital,
            )

    def test_bust_before_or_after_settlement_cannot_double_release_capital(self):
        for settled_first in (False, True):
            with self.subTest(settled_first=settled_first):
                book = self.funded_book()
                sold = self.fill(
                    transaction_id=f"sell-bust-{settled_first}",
                    side="SELL",
                )
                book.append(sold)
                ob = self.bound(
                    sold,
                    obligation_id=f"ob-bust-{settled_first}",
                )
                history = {}
                if settled_first:
                    history[ob.obligation_id] = evidence(
                        ob.obligation_id,
                        f"provider:settlement:{settled_first}",
                        day=25,
                        hour=15,
                    )
                book.append(
                    reverse_transaction(
                        sold,
                        transaction_id=f"bust-{settled_first}",
                        cause_event_id=f"bust-event-{settled_first}",
                        observed_at="2026-09-25T16:00:00Z",
                    )
                )
                rebuilt = SettlementBook.from_economic_book(
                    economic_book=book,
                    obligations=(ob,),
                    settled_obligation_evidence=history,
                )
                self.assertEqual(book.cash("USD"), Decimal("1000"))
                self.assertEqual(rebuilt.available_to_spend("USD"), Decimal("1000"))
                self.assertEqual(rebuilt.obligations, ())


if __name__ == "__main__":
    unittest.main()
