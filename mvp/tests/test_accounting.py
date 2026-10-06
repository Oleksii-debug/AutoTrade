from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN, localcontext
import sys
import gc
import unittest
import weakref

import mvp.autotrade_mvp.accounting as accounting_module
from mvp.autotrade_mvp.accounting import (
    AccountingConflict,
    EconomicBook,
    JournalTransaction,
    Posting,
    ScopedEconomicBook,
    book_equity_fill,
    book_external_cash_flow,
    book_fx_exchange,
    posting,
    project_equity_position,
    reverse_transaction,
    transaction_digest,
    validate_transaction,
)
from mvp.autotrade_mvp.economics import cash_round_trip
from mvp.autotrade_mvp.exact_decimal import MAX_INTEGER_DIGITS


class AccountingFoundationTests(unittest.TestCase):
    def test_collected_scoped_book_releases_bound_economic_book_without_successor(self):
        scoped = ScopedEconomicBook(
            environment="PAPER",
            account_id="acct-1",
        )
        inner = object.__getattribute__(scoped, "__dict__")["_book"]
        scoped_ref = weakref.ref(scoped)
        inner_ref = weakref.ref(inner)
        self.assertTrue(
            all(ref.__callback__ is None for ref in weakref.getweakrefs(scoped))
        )
        self.assertTrue(
            all(ref.__callback__ is None for ref in weakref.getweakrefs(inner))
        )

        del scoped
        del inner
        gc.collect()

        self.assertIsNone(scoped_ref())
        self.assertIsNone(inner_ref())

    def test_cash_equity_round_trip_matches_independent_oracle(self):
        book = EconomicBook()
        book.append(book_external_cash_flow(
            transaction_id="deposit-1",
            cause_event_id="cash-1",
            currency="USD",
            amount="1000",
        ))
        book.append(book_equity_fill(
            transaction_id="buy-1",
            cause_event_id="fill-1",
            instrument="ABC",
            settlement_currency="USD",
            side="BUY",
            quantity="2",
            price="100",
            fee="1",
        ))
        book.append(book_equity_fill(
            transaction_id="sell-1",
            cause_event_id="fill-2",
            instrument="ABC",
            settlement_currency="USD",
            side="SELL",
            quantity="1",
            price="110",
            fee="0.50",
        ))
        oracle = cash_round_trip(
            start_cash="1000",
            buy_quantity="2",
            buy_price="100",
            buy_fee="1",
            sell_quantity="1",
            sell_price="110",
            sell_fee="0.50",
            mark_price="105",
        )
        self.assertEqual(book.cash("USD"), oracle.cash)
        self.assertEqual(book.position("ABC"), oracle.position)
        self.assertEqual(book.fee_expense("USD"), oracle.fees)

    def test_third_currency_fee_and_rebate_are_not_silently_converted(self):
        book = EconomicBook()
        book.append(book_external_cash_flow(
            transaction_id="usd",
            cause_event_id="cash-usd",
            currency="USD",
            amount="1000",
        ))
        book.append(book_external_cash_flow(
            transaction_id="eur",
            cause_event_id="cash-eur",
            currency="EUR",
            amount="10",
        ))
        book.append(book_equity_fill(
            transaction_id="buy",
            cause_event_id="fill-buy",
            instrument="ABC",
            settlement_currency="USD",
            side="BUY",
            quantity="1",
            price="100",
            fee="2",
            fee_currency="EUR",
        ))
        book.append(book_equity_fill(
            transaction_id="sell",
            cause_event_id="fill-sell",
            instrument="ABC",
            settlement_currency="USD",
            side="SELL",
            quantity="1",
            price="100",
            fee="-0.50",
            fee_currency="EUR",
        ))
        self.assertEqual(book.cash("USD"), Decimal("1000"))
        self.assertEqual(book.cash("EUR"), Decimal("8.50"))
        self.assertEqual(book.fee_expense("EUR"), Decimal("1.50"))

    def test_fx_exchange_balances_each_currency_separately(self):
        book = EconomicBook()
        book.append(book_external_cash_flow(
            transaction_id="seed",
            cause_event_id="cash",
            currency="USD",
            amount="1000",
        ))
        trade = book_fx_exchange(
            transaction_id="fx-1",
            cause_event_id="fx-fill",
            sold_currency="USD",
            sold_amount="110",
            bought_currency="EUR",
            bought_amount="100",
        )
        book.append(trade)
        self.assertEqual(book.cash("USD"), Decimal("890"))
        self.assertEqual(book.cash("EUR"), Decimal("100"))
        for asset in {"USD", "EUR"}:
            self.assertEqual(
                sum(p.signed_amount for p in trade.postings if p.asset_or_currency == asset),
                Decimal("0"),
            )

    def test_exact_reversal_restores_balances(self):
        book = EconomicBook()
        book.append(book_external_cash_flow(
            transaction_id="seed",
            cause_event_id="cash",
            currency="USD",
            amount="1000",
        ))
        fill = book_equity_fill(
            transaction_id="fill",
            cause_event_id="fill-event",
            instrument="ABC",
            settlement_currency="USD",
            side="BUY",
            quantity="2",
            price="100",
            fee="1",
        )
        book.append(fill)
        book.append(reverse_transaction(
            fill,
            transaction_id="fill-reversal",
            cause_event_id="correction",
        ))
        self.assertEqual(book.cash("USD"), Decimal("1000"))
        self.assertEqual(book.position("ABC"), Decimal("0"))
        self.assertEqual(book.fee_expense("USD"), Decimal("0"))

    def test_same_transaction_cannot_be_reversed_twice(self):
        book = EconomicBook()
        original = book_external_cash_flow(
            transaction_id="cash-1",
            cause_event_id="deposit",
            currency="USD",
            amount="100",
        )
        book.append(original)
        book.append(reverse_transaction(
            original,
            transaction_id="reverse-1",
            cause_event_id="correction-1",
        ))
        with self.assertRaises(AccountingConflict):
            book.append(reverse_transaction(
                original,
                transaction_id="reverse-2",
                cause_event_id="correction-2",
            ))

    def test_duplicate_transaction_is_idempotent_but_changed_content_conflicts(self):
        book = EconomicBook()
        original = book_external_cash_flow(
            transaction_id="cash-1",
            cause_event_id="deposit",
            currency="USD",
            amount="100",
        )
        self.assertTrue(book.append(original))
        self.assertFalse(book.append(original))
        changed = book_external_cash_flow(
            transaction_id="cash-1",
            cause_event_id="deposit",
            currency="USD",
            amount="101",
        )
        with self.assertRaises(AccountingConflict):
            book.append(changed)

    def test_same_economic_cause_cannot_be_booked_under_two_transaction_ids(self):
        book = EconomicBook()
        first = book_external_cash_flow(
            transaction_id="cash-1",
            cause_event_id="provider-activity-1",
            currency="USD",
            amount="100",
        )
        duplicate_cause = book_external_cash_flow(
            transaction_id="cash-2",
            cause_event_id="provider-activity-1",
            currency="USD",
            amount="100",
        )
        self.assertTrue(book.append(first))
        with self.assertRaisesRegex(AccountingConflict, "cause_event_id"):
            book.append(duplicate_cause)
        self.assertEqual(book.cash("USD"), Decimal("100"))
        self.assertEqual(len(book.transactions), 1)

    def test_same_fill_cause_cannot_double_position_or_fee(self):
        book = EconomicBook()
        first = book_equity_fill(
            transaction_id="fill-posting-1",
            cause_event_id="provider-execution-abc",
            instrument="ABC",
            settlement_currency="USD",
            side="BUY",
            quantity="2",
            price="100",
            fee="1",
        )
        duplicate = book_equity_fill(
            transaction_id="fill-posting-2",
            cause_event_id="provider-execution-abc",
            instrument="ABC",
            settlement_currency="USD",
            side="BUY",
            quantity="2",
            price="100",
            fee="1",
        )
        book.append(first)
        with self.assertRaises(AccountingConflict):
            book.append(duplicate)
        self.assertEqual(book.position("ABC"), Decimal("2"))
        self.assertEqual(book.fee_expense("USD"), Decimal("1"))

    def test_reversal_requires_its_own_distinct_cause_event(self):
        book = EconomicBook()
        original = book_external_cash_flow(
            transaction_id="cash-1",
            cause_event_id="provider-cash-event",
            currency="USD",
            amount="100",
        )
        book.append(original)
        reversal = reverse_transaction(
            original,
            transaction_id="cash-reversal",
            cause_event_id="provider-cash-event",
        )
        with self.assertRaisesRegex(AccountingConflict, "cause_event_id"):
            book.append(reversal)
        self.assertEqual(book.cash("USD"), Decimal("100"))

    def test_constructor_detects_duplicate_cause_history(self):
        first = book_external_cash_flow(
            transaction_id="cash-1",
            cause_event_id="same-cause",
            currency="USD",
            amount="100",
        )
        second = book_external_cash_flow(
            transaction_id="cash-2",
            cause_event_id="same-cause",
            currency="USD",
            amount="-100",
        )
        with self.assertRaisesRegex(AccountingConflict, "cause_event_id"):
            EconomicBook([first, second])

    def test_canonical_cause_identity_cannot_be_bypassed_with_whitespace(self):
        book = EconomicBook()
        first = JournalTransaction(
            transaction_id="raw-1",
            cause_event_id=" provider-execution-abc ",
            postings=(
                posting("CASH:USD", "USD", "100"),
                posting("EXTERNAL_EQUITY:USD", "USD", "-100"),
            ),
        )
        second = JournalTransaction(
            transaction_id="raw-2",
            cause_event_id="provider-execution-abc",
            postings=(
                posting("CASH:USD", "USD", "100"),
                posting("EXTERNAL_EQUITY:USD", "USD", "-100"),
            ),
        )
        self.assertTrue(book.append(first))
        with self.assertRaisesRegex(AccountingConflict, "cause_event_id"):
            book.append(second)
        self.assertEqual(book.cash("USD"), Decimal("100"))

    def test_canonical_transaction_identity_cannot_be_split_with_whitespace(self):
        book = EconomicBook()
        first = JournalTransaction(
            transaction_id=" tx-1 ",
            cause_event_id="cause-a",
            postings=(
                posting("CASH:USD", "USD", "100"),
                posting("EXTERNAL_EQUITY:USD", "USD", "-100"),
            ),
        )
        changed = JournalTransaction(
            transaction_id="tx-1",
            cause_event_id="cause-b",
            postings=(
                posting("CASH:USD", "USD", "200"),
                posting("EXTERNAL_EQUITY:USD", "USD", "-200"),
            ),
        )
        self.assertTrue(book.append(first))
        with self.assertRaisesRegex(AccountingConflict, "transaction_id"):
            book.append(changed)
        self.assertEqual(book.cash("USD"), Decimal("100"))

    def test_audit_identity_and_balance_projection_share_canonical_posting_names(self):
        raw = JournalTransaction(
            transaction_id=" tx-whitespace ",
            cause_event_id=" source-event ",
            postings=(
                Posting(" CASH:USD ", " USD ", Decimal("100")),
                Posting(" CLEARING:USD ", " USD ", Decimal("-100")),
            ),
        )
        book = EconomicBook()
        self.assertTrue(book.append(raw))

        self.assertEqual(book.cash("USD"), Decimal("100"))
        stored = book.transactions[0]
        self.assertEqual(stored.transaction_id, "tx-whitespace")
        self.assertEqual(stored.cause_event_id, "source-event")
        self.assertEqual(stored.postings[0].ledger_account, "CASH:USD")
        self.assertEqual(stored.postings[0].asset_or_currency, "USD")
        self.assertEqual(book.audit_digest(), EconomicBook(book.transactions).audit_digest())

    def test_transaction_digest_is_stable_across_equivalent_decimal_scales(self):
        first = JournalTransaction(
            transaction_id="tx",
            cause_event_id="cause",
            postings=(
                posting("CASH:USD", "USD", Decimal("1.0")),
                posting("CLEARING:USD", "USD", Decimal("-1.00")),
            ),
        )
        second = JournalTransaction(
            transaction_id="tx",
            cause_event_id="cause",
            postings=(
                posting("CASH:USD", "USD", Decimal("1.000")),
                posting("CLEARING:USD", "USD", Decimal("-1")),
            ),
        )
        self.assertEqual(transaction_digest(first), transaction_digest(second))

    def test_transaction_digest_changes_with_economic_or_lineage_content(self):
        original = book_external_cash_flow(
            transaction_id="cash-1",
            cause_event_id="deposit",
            currency="USD",
            amount="100",
        )
        changed_amount = book_external_cash_flow(
            transaction_id="cash-1",
            cause_event_id="deposit",
            currency="USD",
            amount="101",
        )
        changed_cause = book_external_cash_flow(
            transaction_id="cash-1",
            cause_event_id="deposit-2",
            currency="USD",
            amount="100",
        )
        self.assertNotEqual(
            transaction_digest(original),
            transaction_digest(changed_amount),
        )
        self.assertNotEqual(
            transaction_digest(original),
            transaction_digest(changed_cause),
        )

    def test_book_audit_digest_is_restart_reproducible_and_order_sensitive(self):
        first_tx = book_external_cash_flow(
            transaction_id="cash-1",
            cause_event_id="deposit-1",
            currency="USD",
            amount="100",
        )
        second_tx = book_external_cash_flow(
            transaction_id="cash-2",
            cause_event_id="deposit-2",
            currency="EUR",
            amount="50",
        )
        original = EconomicBook((first_tx, second_tx))
        restarted = EconomicBook(original.transactions)
        reordered = EconomicBook((second_tx, first_tx))
        self.assertEqual(original.audit_digest(), restarted.audit_digest())
        self.assertNotEqual(original.audit_digest(), reordered.audit_digest())

    def test_audit_digest_is_not_a_financial_balance_or_authority(self):
        book = EconomicBook()
        book.append(book_external_cash_flow(
            transaction_id="cash-1",
            cause_event_id="deposit",
            currency="USD",
            amount="100",
        ))
        digest = book.audit_digest()
        self.assertRegex(digest, r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(book.cash("USD"), Decimal("100"))

    def test_audit_digest_cache_detects_direct_transaction_list_mutation(self):
        book = EconomicBook((book_external_cash_flow(
            transaction_id="cash-1",
            cause_event_id="deposit-1",
            currency="USD",
            amount="100",
        ),))
        before = book.audit_digest()
        book._transactions.append(book_external_cash_flow(
            transaction_id="cash-2",
            cause_event_id="deposit-2",
            currency="USD",
            amount="50",
        ))
        self.assertNotEqual(book.audit_digest(), before)

    def test_audit_digest_cache_detects_frozen_transaction_tampering(self):
        book = EconomicBook((book_external_cash_flow(
            transaction_id="cash-1",
            cause_event_id="deposit-1",
            currency="USD",
            amount="100",
        ),))
        before = book.audit_digest()
        object.__setattr__(book._transactions[0], "transaction_id", "tampered")
        self.assertNotEqual(book.audit_digest(), before)

    def test_audit_digest_cache_fails_closed_if_transaction_changes_during_digest(self):
        book = EconomicBook((book_external_cash_flow(
            transaction_id="cash-1",
            cause_event_id="deposit-1",
            currency="USD",
            amount="100",
        ),))
        transaction = book._transactions[0]

        closure = {
            name: cell.cell_contents
            for name, cell in zip(
                EconomicBook.audit_digest.__code__.co_freevars,
                EconomicBook.audit_digest.__closure__ or (),
            )
        }
        cached_digest = closure["digest_transaction"]
        target_code = cached_digest.__code__
        mutated = False

        def trace(frame, event, _arg):
            nonlocal mutated
            if (
                not mutated
                and event == "line"
                and frame.f_code is target_code
                and frame.f_locals.get("transaction") is transaction
                and "fingerprint" in frame.f_locals
                and "digest" in frame.f_locals
            ):
                object.__setattr__(
                    transaction,
                    "transaction_id",
                    "cash-raced-during-digest",
                )
                mutated = True
            return trace

        previous_trace = sys.gettrace()
        sys.settrace(trace)
        try:
            with self.assertRaisesRegex(
                AccountingConflict,
                "transaction changed during digest computation",
            ):
                book.audit_digest()
        finally:
            sys.settrace(previous_trace)

        self.assertTrue(mutated)

    def test_audit_digest_rejects_hostile_transaction_container_without_callback(self):
        class HostileList(list):
            iterated = False

            def __iter__(self):
                self.iterated = True
                raise AssertionError("hostile iterator callback")

        book = EconomicBook((book_external_cash_flow(
            transaction_id="cash-1",
            cause_event_id="deposit-1",
            currency="USD",
            amount="100",
        ),))
        hostile = HostileList(book._transactions)
        book._transactions = hostile
        with self.assertRaisesRegex(TypeError, "exact list"):
            book.audit_digest()
        self.assertFalse(hostile.iterated)

    def test_audit_digest_cache_cannot_be_retargeted_through_module_state(self):
        book = EconomicBook((book_external_cash_flow(
            transaction_id="cash-1",
            cause_event_id="deposit-1",
            currency="USD",
            amount="100",
        ),))
        expected = book.audit_digest()
        transaction = book._transactions[0]
        fingerprint = accounting_module._transaction_digest_fingerprint(transaction)
        self.assertIsNotNone(fingerprint)
        forged = "sha256:" + "0" * 64

        # A module-global memoization dictionary would become a second mutable
        # authority over the audit result. A same-named hostile module binding
        # must be irrelevant to the cache selected by product construction.
        self.assertFalse(hasattr(accounting_module, "_transaction_digest_cache"))
        self.assertFalse(hasattr(accounting_module, "_cached_transaction_digest"))
        original_payload_digest = accounting_module.payload_digest
        original_transaction_digest = accounting_module.transaction_digest
        accounting_module._transaction_digest_cache = {fingerprint: forged}
        accounting_module._transaction_digest_cache_lock = object()
        accounting_module._cached_transaction_digest = lambda _transaction: forged
        accounting_module.payload_digest = lambda _payload: forged
        accounting_module.transaction_digest = lambda _transaction: forged
        try:
            self.assertEqual(book.audit_digest(), expected)
            self.assertNotEqual(book.audit_digest(), forged)

            # Exercise a cache miss after the hostile module rebinding. The
            # selected memoizer must retain the original transaction digest
            # function, and that function must retain the original payload
            # digest primitive rather than resolving the rebound global.
            second_transaction = book_external_cash_flow(
                transaction_id="cash-2",
                cause_event_id="deposit-2",
                currency="USD",
                amount="50",
            )
            second_expected = original_payload_digest(
                {
                    "schema_version": "1.0.0",
                    "transactions": [
                        {
                            "transaction_id": "cash-2",
                            "digest": original_transaction_digest(second_transaction),
                        }
                    ],
                }
            )
            second_book = EconomicBook((second_transaction,))
            self.assertEqual(second_book.audit_digest(), second_expected)
            self.assertNotEqual(second_book.audit_digest(), forged)
        finally:
            accounting_module.payload_digest = original_payload_digest
            accounting_module.transaction_digest = original_transaction_digest
            del accounting_module._transaction_digest_cache
            del accounting_module._transaction_digest_cache_lock
            del accounting_module._cached_transaction_digest

    def test_audit_digest_cache_miss_fails_closed_on_rebound_canonicalizer_dependencies(self):
        first = book_external_cash_flow(
            transaction_id="cash-1",
            cause_event_id="deposit-1",
            currency="USD",
            amount="100",
        )
        second = book_external_cash_flow(
            transaction_id="cash-2",
            cause_event_id="deposit-2",
            currency="USD",
            amount="50",
        )
        book = EconomicBook((first,))
        book.audit_digest()
        book.append(second)

        def hostile_helper(*_args, **_kwargs):
            raise AssertionError("rebound helper must not execute")

        for attribute in ("_name", "parse_bounded_exact_decimal"):
            with self.subTest(attribute=attribute):
                original = getattr(accounting_module, attribute)
                setattr(accounting_module, attribute, hostile_helper)
                try:
                    with self.assertRaisesRegex(
                        AccountingConflict,
                        "digest authority dependency was rebound",
                    ):
                        book.audit_digest()
                finally:
                    setattr(accounting_module, attribute, original)

    def test_audit_digest_rejects_hostile_transaction_subclass_before_attribute_callback(self):
        exact = book_external_cash_flow(
            transaction_id="cash-1",
            cause_event_id="deposit-1",
            currency="USD",
            amount="100",
        )

        class HostileTransaction(JournalTransaction):
            touched = False

            def __getattribute__(self, name):
                if name == "transaction_id":
                    type(self).touched = True
                    raise AssertionError("hostile transaction attribute callback")
                return super().__getattribute__(name)

        hostile = HostileTransaction(
            transaction_id=exact.transaction_id,
            cause_event_id=exact.cause_event_id,
            postings=exact.postings,
            reverses_transaction_id=exact.reverses_transaction_id,
            economic_effective_at=exact.economic_effective_at,
            economic_order_key=exact.economic_order_key,
            observed_at=exact.observed_at,
            corrects_transaction_id=exact.corrects_transaction_id,
        )
        book = EconomicBook((exact,))
        book._transactions[0] = hostile

        with self.assertRaisesRegex(TypeError, "exact JournalTransaction values"):
            book.audit_digest()
        self.assertFalse(HostileTransaction.touched)

    def test_scoped_economic_book_constructor_ignores_rebound_module_authorities(self):
        self.assertFalse(
            hasattr(accounting_module, "_bind_scoped_economic_book_owner")
        )
        self.assertFalse(
            hasattr(accounting_module, "_require_scoped_economic_book_owner")
        )

        original_class = accounting_module.ScopedEconomicBook
        original_book_type = accounting_module.EconomicBook
        original_name = accounting_module._name
        original_weakref = accounting_module.weakref
        original_environments = ScopedEconomicBook._ENVIRONMENTS
        hostile_calls = []

        class HostileBook:
            def __init__(self, _transactions=()):
                hostile_calls.append("book")

        seed = book_external_cash_flow(
            transaction_id="seed-constructor",
            cause_event_id="seed-constructor-cause",
            currency="USD",
            amount="100",
        )
        accounting_module.ScopedEconomicBook = object
        accounting_module.EconomicBook = HostileBook
        accounting_module._bind_scoped_economic_book_owner = (
            lambda *_args: hostile_calls.append("bind")
        )
        accounting_module._require_scoped_economic_book_owner = (
            lambda _value: (
                hostile_calls.append("require")
                or ("LIVE", "forged-account", EconomicBook())
            )
        )
        accounting_module._name = lambda *_args, **_kwargs: (
            hostile_calls.append("name") or "FORGED"
        )
        accounting_module.weakref = object()
        ScopedEconomicBook._ENVIRONMENTS = frozenset({"FORGED"})
        try:
            scoped = ScopedEconomicBook(
                environment="paper",
                account_id=" acct-1 ",
            )
            self.assertEqual(hostile_calls, [])
            self.assertEqual(scoped.environment, "PAPER")
            self.assertEqual(scoped.account_id, "acct-1")
            self.assertIs(
                type(object.__getattribute__(scoped, "__dict__")["_book"]),
                original_book_type,
            )

            # _name is a broader EconomicBook normalization/projection
            # dependency, not ScopedEconomicBook construction authority. Restore
            # it before exercising the retained canonical book, while keeping
            # every scoped-owner/class/book/weakref decoy in place.
            accounting_module._name = original_name
            self.assertTrue(scoped.append(seed))
            self.assertEqual(scoped.cash("USD"), Decimal("100"))
            self.assertEqual(hostile_calls, [])

            with self.assertRaisesRegex(ValueError, "unsupported environment"):
                ScopedEconomicBook(
                    environment="FORGED",
                    account_id="acct-forged",
                )
        finally:
            accounting_module.ScopedEconomicBook = original_class
            accounting_module.EconomicBook = original_book_type
            accounting_module._name = original_name
            accounting_module.weakref = original_weakref
            ScopedEconomicBook._ENVIRONMENTS = original_environments
            del accounting_module._bind_scoped_economic_book_owner
            del accounting_module._require_scoped_economic_book_owner

    def test_scoped_economic_book_rejects_post_construction_owner_retargeting(self):
        scoped = ScopedEconomicBook(
            environment="PAPER",
            account_id="acct-1",
            transactions=(
                book_external_cash_flow(
                    transaction_id="seed",
                    cause_event_id="seed-cause",
                    currency="USD",
                    amount="100",
                ),
            ),
        )
        expected = scoped.audit_digest()

        self.assertFalse(
            hasattr(accounting_module, "_require_scoped_economic_book_owner")
        )
        original_payload_digest = accounting_module.payload_digest
        accounting_module._require_scoped_economic_book_owner = (
            lambda _value: ("LIVE", "forged-account", EconomicBook())
        )
        accounting_module.payload_digest = lambda _payload: "sha256:" + "0" * 64
        try:
            # Facade methods retain the verifier and digest primitive selected
            # during module construction.
            self.assertEqual(scoped.audit_digest(), expected)
            self.assertEqual(scoped.cash("USD"), Decimal("100"))
        finally:
            del accounting_module._require_scoped_economic_book_owner
            accounting_module.payload_digest = original_payload_digest

        object.__setattr__(scoped, "environment", "LIVE")
        with self.assertRaisesRegex(AccountingConflict, "owner changed"):
            scoped.audit_digest()
        with self.assertRaisesRegex(AccountingConflict, "owner changed"):
            scoped.cash("USD")
        object.__setattr__(scoped, "environment", "PAPER")

        replacement = EconomicBook(
            (
                book_external_cash_flow(
                    transaction_id="replacement",
                    cause_event_id="replacement-cause",
                    currency="USD",
                    amount="999",
                ),
            )
        )
        object.__setattr__(scoped, "_book", replacement)
        with self.assertRaisesRegex(AccountingConflict, "owner changed"):
            scoped.audit_digest()
        with self.assertRaisesRegex(AccountingConflict, "owner changed"):
            scoped.append(
                book_external_cash_flow(
                    transaction_id="should-not-append",
                    cause_event_id="should-not-append-cause",
                    currency="USD",
                    amount="1",
                )
            )
        self.assertEqual(replacement.cash("USD"), Decimal("999"))

    def test_scoped_economic_book_owner_writer_is_not_public_authority(self):
        self.assertFalse(
            hasattr(accounting_module, "_bind_scoped_economic_book_owner")
        )
        self.assertFalse(
            hasattr(accounting_module, "_require_scoped_economic_book_owner")
        )

        forged = object.__new__(ScopedEconomicBook)
        object.__setattr__(forged, "environment", "PAPER")
        object.__setattr__(forged, "account_id", "acct-forged")
        object.__setattr__(forged, "_book", EconomicBook())

        with self.assertRaisesRegex(AccountingConflict, "owner is unavailable"):
            forged.cash("USD")
        with self.assertRaisesRegex(AccountingConflict, "owner is unavailable"):
            forged.audit_digest()

    def test_scoped_economic_book_reinit_rejected_before_state_mutation(self):
        scoped = ScopedEconomicBook(
            environment="PAPER",
            account_id="acct-reinit",
            transactions=(
                book_external_cash_flow(
                    transaction_id="reinit-seed",
                    cause_event_id="reinit-seed-cause",
                    currency="USD",
                    amount="40",
                ),
            ),
        )
        before_digest = scoped.audit_digest()
        before_book = object.__getattribute__(scoped, "_book")

        with self.assertRaisesRegex(
            AccountingConflict,
            "owner is already initialized",
        ):
            ScopedEconomicBook.__init__(
                scoped,
                environment="LIVE",
                account_id="attacker-account",
                transactions=(
                    book_external_cash_flow(
                        transaction_id="attacker-seed",
                        cause_event_id="attacker-seed-cause",
                        currency="USD",
                        amount="999",
                    ),
                ),
            )

        self.assertEqual(scoped.environment, "PAPER")
        self.assertEqual(scoped.account_id, "acct-reinit")
        self.assertIs(object.__getattribute__(scoped, "_book"), before_book)
        self.assertEqual(scoped.cash("USD"), Decimal("40"))
        self.assertEqual(scoped.audit_digest(), before_digest)

    def test_scoped_economic_book_builtin_dispatch_is_frozen(self):
        touched = []

        def hostile_builtin(*_args, **_kwargs):
            touched.append("builtin")
            raise AssertionError("rebound builtin must not execute")

        class HostileObject:
            @staticmethod
            def __setattr__(*args, **kwargs):
                touched.append("setattr")
                raise AssertionError("rebound object.__setattr__ must not execute")

            @staticmethod
            def __getattribute__(*args, **kwargs):
                touched.append("getattribute")
                raise AssertionError(
                    "rebound object.__getattribute__ must not execute"
                )

        injected = {
            "type": hostile_builtin,
            "id": hostile_builtin,
            "object": HostileObject,
            "tuple": hostile_builtin,
            "str": object,
            "TypeError": hostile_builtin,
            "ValueError": hostile_builtin,
        }
        for name in injected:
            self.assertFalse(hasattr(accounting_module, name))
        for name, value in injected.items():
            setattr(accounting_module, name, value)
        try:
            scoped = ScopedEconomicBook(
                environment=" paper ",
                account_id=" acct-builtins ",
            )
            self.assertEqual(scoped.environment, "PAPER")
            self.assertEqual(scoped.account_id, "acct-builtins")
            self.assertEqual(scoped.cash("USD"), Decimal("0"))
            self.assertEqual(touched, [])

            with self.assertRaises(ValueError):
                ScopedEconomicBook(
                    environment=" ",
                    account_id="acct-invalid",
                )
            self.assertEqual(touched, [])
        finally:
            for name in injected:
                delattr(accounting_module, name)

    def test_scoped_economic_book_subclass_rejected_before_virtual_dispatch(self):
        touched = []

        class HostileScopedBook(ScopedEconomicBook):
            def __getattribute__(self, name):
                if name != "__class__":
                    touched.append(("get", name))
                    raise AssertionError("hostile scoped-book attribute dispatch")
                return super().__getattribute__(name)

            def __setattr__(self, name, value):
                touched.append(("set", name))
                raise AssertionError("hostile scoped-book attribute dispatch")

        with self.assertRaisesRegex(TypeError, "exact ScopedEconomicBook"):
            HostileScopedBook(
                environment="PAPER",
                account_id="acct-hostile",
            )

        self.assertEqual(touched, [])

    def test_unbalanced_transaction_is_rejected(self):
        transaction = JournalTransaction(
            transaction_id="bad",
            cause_event_id="bad-event",
            postings=(
                posting("CASH:USD", "USD", "10"),
                posting("OTHER:USD", "USD", "-9"),
            ),
        )
        with self.assertRaises(ValueError):
            validate_transaction(transaction)
        with self.assertRaises(ValueError):
            EconomicBook().append(transaction)

    def test_binary_float_inputs_are_rejected(self):
        with self.assertRaises(TypeError):
            book_equity_fill(
                transaction_id="fill",
                cause_event_id="event",
                instrument="ABC",
                settlement_currency="USD",
                side="BUY",
                quantity=1,
                price=100.1,
            )


    def test_atomic_batch_is_all_or_nothing_and_exact_retry_is_idempotent(self):
        book = EconomicBook()
        usd = book_external_cash_flow(
            transaction_id="batch-usd",
            cause_event_id="batch-cash-usd",
            currency="USD",
            amount="10",
        )
        eur = book_external_cash_flow(
            transaction_id="batch-eur",
            cause_event_id="batch-cash-eur",
            currency="EUR",
            amount="5",
        )

        self.assertTrue(book.append_batch((usd, eur)))
        committed_digest = book.audit_digest()
        self.assertFalse(book.append_batch((usd, eur)))
        self.assertEqual(book.audit_digest(), committed_digest)
        self.assertEqual(len(book.transactions), 2)

        gbp = book_external_cash_flow(
            transaction_id="batch-gbp",
            cause_event_id="batch-cash-gbp",
            currency="GBP",
            amount="7",
        )
        conflicting_eur = book_external_cash_flow(
            transaction_id="batch-eur",
            cause_event_id="batch-cash-eur-conflict",
            currency="EUR",
            amount="6",
        )
        before = book.transactions
        before_digest = book.audit_digest()
        with self.assertRaises(AccountingConflict):
            book.append_batch((gbp, conflicting_eur))
        self.assertEqual(book.transactions, before)
        self.assertEqual(book.audit_digest(), before_digest)
        self.assertEqual(book.cash("GBP"), Decimal("0"))


    def test_corrected_fill_restates_fifo_at_original_economic_time(self):
        book = EconomicBook()
        original = book_equity_fill(
            transaction_id="buy-original",
            cause_event_id="fill-buy-original",
            instrument="ABC",
            settlement_currency="USD",
            side="BUY",
            quantity="2",
            price="100",
            economic_effective_at="2026-01-01T10:00:00Z",
            economic_order_key="fill-1",
        )
        later_sale = book_equity_fill(
            transaction_id="sell-later",
            cause_event_id="fill-sell-later",
            instrument="ABC",
            settlement_currency="USD",
            side="SELL",
            quantity="1",
            price="110",
            economic_effective_at="2026-01-01T11:00:00Z",
            economic_order_key="fill-2",
        )
        book.append(original)
        book.append(later_sale)
        replacement = book_equity_fill(
            transaction_id="buy-corrected",
            cause_event_id="fill-buy-corrected",
            instrument="ABC",
            settlement_currency="USD",
            side="BUY",
            quantity="2",
            price="101",
            economic_effective_at=original.economic_effective_at,
            economic_order_key=original.economic_order_key,
            observed_at="2026-01-02T12:00:00Z",
            corrects_transaction_id=original.transaction_id,
        )
        self.assertTrue(
            book.append_batch(
                (
                    reverse_transaction(
                        original,
                        transaction_id="buy-original-reversal",
                        cause_event_id="fill-buy-correction-reversal",
                        observed_at="2026-01-02T12:00:00Z",
                    ),
                    replacement,
                )
            )
        )

        projected = project_equity_position(
            book,
            instrument="ABC",
            settlement_currency="USD",
        )
        self.assertEqual(projected.quantity, Decimal("1"))
        self.assertEqual(projected.open_cost_basis, Decimal("101"))
        self.assertEqual(projected.realized_pnl, Decimal("9"))
        self.assertEqual(projected.lots[0].transaction_id, "buy-corrected")

        restarted = EconomicBook(book.transactions)
        self.assertEqual(
            project_equity_position(
                restarted,
                instrument="ABC",
                settlement_currency="USD",
            ),
            projected,
        )

    def test_corrected_fifo_fails_closed_without_explicit_replacement_lineage(self):
        book = EconomicBook()
        original = book_equity_fill(
            transaction_id="buy-lineage",
            cause_event_id="fill-buy-lineage",
            instrument="ABC",
            settlement_currency="USD",
            side="BUY",
            quantity="1",
            price="100",
            economic_effective_at="2026-01-01T10:00:00Z",
            economic_order_key="provider:A:execution:lineage",
        )
        book.append(original)
        reversal = reverse_transaction(
            original,
            transaction_id="buy-lineage-reversal",
            cause_event_id="fill-buy-lineage-reversal",
            observed_at="2026-01-02T10:00:00Z",
        )
        replacement = book_equity_fill(
            transaction_id="buy-lineage-unbound",
            cause_event_id="fill-buy-lineage-unbound",
            instrument="ABC",
            settlement_currency="USD",
            side="BUY",
            quantity="1",
            price="101",
            economic_effective_at=original.economic_effective_at,
            economic_order_key=original.economic_order_key,
        )
        book.append_batch((reversal, replacement))
        with self.assertRaisesRegex(AccountingConflict, "replacement lineage"):
            project_equity_position(
                book,
                instrument="ABC",
                settlement_currency="USD",
            )

    def test_corrected_fifo_fails_closed_when_active_fill_lacks_order_evidence(self):
        book = EconomicBook()
        original = book_equity_fill(
            transaction_id="buy-original",
            cause_event_id="fill-buy-original",
            instrument="ABC",
            settlement_currency="USD",
            side="BUY",
            quantity="2",
            price="100",
            economic_effective_at="2026-01-01T10:00:00Z",
            economic_order_key="fill-1",
        )
        book.append(original)
        book.append(
            book_equity_fill(
                transaction_id="sell-without-order-proof",
                cause_event_id="fill-sell-unordered",
                instrument="ABC",
                settlement_currency="USD",
                side="SELL",
                quantity="1",
                price="110",
            )
        )
        replacement = book_equity_fill(
            transaction_id="buy-corrected",
            cause_event_id="fill-buy-corrected",
            instrument="ABC",
            settlement_currency="USD",
            side="BUY",
            quantity="2",
            price="101",
            economic_effective_at=original.economic_effective_at,
            economic_order_key=original.economic_order_key,
            observed_at="2026-01-02T12:00:00Z",
            corrects_transaction_id=original.transaction_id,
        )
        book.append_batch(
            (
                reverse_transaction(
                    original,
                    transaction_id="buy-original-reversal",
                    cause_event_id="fill-buy-correction-reversal",
                    observed_at="2026-01-02T12:00:00Z",
                ),
                replacement,
            )
        )
        with self.assertRaisesRegex(
            AccountingConflict,
            "economic effective-time",
        ):
            project_equity_position(
                book,
                instrument="ABC",
                settlement_currency="USD",
            )



class AccountingExactAuthorityTests(unittest.TestCase):
    _ROUNDINGS = (ROUND_FLOOR, ROUND_CEILING, ROUND_HALF_EVEN)
    _PRECISIONS = (6, 10, 28, 80)

    def test_unbalanced_tiny_residual_is_rejected_in_every_decimal_context(self):
        transaction = JournalTransaction(
            transaction_id="hostile-context-unbalanced",
            cause_event_id="hostile-context-unbalanced-cause",
            postings=(
                posting("A", "USD", "1e30"),
                posting("B", "USD", "1e-30"),
                posting("C", "USD", "-1e30"),
            ),
        )

        for precision in self._PRECISIONS:
            for rounding in self._ROUNDINGS:
                with self.subTest(precision=precision, rounding=rounding):
                    with localcontext() as context:
                        context.prec = precision
                        context.rounding = rounding
                        with self.assertRaisesRegex(ValueError, "not balanced"):
                            validate_transaction(transaction)

    def test_high_significance_reversal_is_context_invariant(self):
        original = JournalTransaction(
            transaction_id="high-significance-original",
            cause_event_id="high-significance-cause",
            postings=(
                posting("CASH:USD", "USD", "12345678901234567890.123456789"),
                posting("CLEARING:USD", "USD", "-12345678901234567890.123456789"),
            ),
        )
        expected = (
            Decimal("-12345678901234567890.123456789"),
            Decimal("12345678901234567890.123456789"),
        )
        digests = set()

        for precision in self._PRECISIONS:
            for rounding in self._ROUNDINGS:
                with self.subTest(precision=precision, rounding=rounding):
                    with localcontext() as context:
                        context.prec = precision
                        context.rounding = rounding
                        reversal = reverse_transaction(
                            original,
                            transaction_id="high-significance-reversal",
                            cause_event_id="high-significance-reversal-cause",
                        )
                        self.assertEqual(
                            tuple(item.signed_amount for item in reversal.postings),
                            expected,
                        )
                        digests.add(transaction_digest(reversal))

        self.assertEqual(len(digests), 1)

    def test_balance_projection_preserves_tiny_residual_in_every_decimal_context(self):
        transactions = (
            JournalTransaction(
                transaction_id="balance-large-in",
                cause_event_id="balance-large-in-cause",
                postings=(
                    posting("TARGET", "USD", "1e30"),
                    posting("OFFSET", "USD", "-1e30"),
                ),
            ),
            JournalTransaction(
                transaction_id="balance-tiny-in",
                cause_event_id="balance-tiny-in-cause",
                postings=(
                    posting("TARGET", "USD", "1e-30"),
                    posting("OFFSET", "USD", "-1e-30"),
                ),
            ),
            JournalTransaction(
                transaction_id="balance-large-out",
                cause_event_id="balance-large-out-cause",
                postings=(
                    posting("TARGET", "USD", "-1e30"),
                    posting("OFFSET", "USD", "1e30"),
                ),
            ),
        )

        for precision in self._PRECISIONS:
            for rounding in self._ROUNDINGS:
                with self.subTest(precision=precision, rounding=rounding):
                    with localcontext() as context:
                        context.prec = precision
                        context.rounding = rounding
                        book = EconomicBook(transactions)
                        self.assertEqual(
                            book.balance("TARGET", "USD"),
                            Decimal("1e-30"),
                        )

    def test_oversized_durable_split_ratio_is_rejected_before_decimal_parse(self):
        oversized = "1" * (MAX_INTEGER_DIGITS + 1)

        for numerator, denominator in ((oversized, "1"), ("1", oversized)):
            with self.subTest(
                numerator_length=len(numerator),
                denominator_length=len(denominator),
            ):
                transaction = JournalTransaction(
                    transaction_id=(
                        "oversized-split-ratio-"
                        + ("numerator" if numerator == oversized else "denominator")
                    ),
                    cause_event_id=(
                        "oversized-split-ratio-cause-"
                        + ("numerator" if numerator == oversized else "denominator")
                    ),
                    postings=(
                        posting("POSITION:ABC", "ABC", "1"),
                        posting(
                            "CORPORATE_ACTION_SPLIT_CLEARING:ABC:"
                            + numerator
                            + ":"
                            + denominator,
                            "ABC",
                            "-1",
                        ),
                    ),
                )
                book = EconomicBook((transaction,))
                before = book.transactions

                with self.assertRaisesRegex(
                    AccountingConflict,
                    "ratio identity is not canonical",
                ):
                    project_equity_position(
                        book,
                        instrument="ABC",
                        settlement_currency="USD",
                    )

                self.assertEqual(book.transactions, before)




class AccountingSemanticGraphAuthorityTests(unittest.TestCase):
    def test_transaction_subclass_is_rejected_before_attribute_dispatch(self):
        touched = []

        class HostileTransaction(JournalTransaction):
            def __getattribute__(self, name):
                if name not in {"__class__"}:
                    touched.append(name)
                    raise AssertionError("hostile transaction attribute dispatch")
                return super().__getattribute__(name)

        hostile = object.__new__(HostileTransaction)
        book = EconomicBook()
        before = book.transactions

        with self.assertRaisesRegex(TypeError, "exact JournalTransaction"):
            book.append(hostile)

        self.assertEqual(touched, [])
        self.assertEqual(book.transactions, before)

    def test_posting_subclass_is_rejected_before_attribute_dispatch(self):
        touched = []

        class HostilePosting(Posting):
            def __getattribute__(self, name):
                if name not in {"__class__"}:
                    touched.append(name)
                    raise AssertionError("hostile posting attribute dispatch")
                return super().__getattribute__(name)

        hostile = object.__new__(HostilePosting)
        transaction = JournalTransaction(
            transaction_id="hostile-posting",
            cause_event_id="hostile-posting-cause",
            postings=(hostile, posting("OFFSET", "USD", "0")),
        )
        book = EconomicBook()
        before = book.transactions

        with self.assertRaisesRegex(TypeError, "exact Posting"):
            book.append(transaction)

        self.assertEqual(touched, [])
        self.assertEqual(book.transactions, before)

    def test_string_subclass_is_rejected_before_virtual_strip(self):
        touched = []

        class HostileText(str):
            def strip(self, *args, **kwargs):
                touched.append("strip")
                raise AssertionError("hostile string strip dispatch")

        transaction = JournalTransaction(
            transaction_id=HostileText("hostile-text"),
            cause_event_id="hostile-text-cause",
            postings=(
                posting("A", "USD", "1"),
                posting("B", "USD", "-1"),
            ),
        )
        book = EconomicBook()
        before = book.transactions

        with self.assertRaisesRegex(ValueError, "transaction_id"):
            book.append(transaction)

        self.assertEqual(touched, [])
        self.assertEqual(book.transactions, before)

    def test_decimal_subclass_is_rejected_before_virtual_decimal_dispatch(self):
        touched = []

        class HostileDecimal(Decimal):
            def is_finite(self):
                touched.append("is_finite")
                raise AssertionError("hostile Decimal dispatch")

            def as_tuple(self):
                touched.append("as_tuple")
                raise AssertionError("hostile Decimal dispatch")

        transaction = JournalTransaction(
            transaction_id="hostile-decimal",
            cause_event_id="hostile-decimal-cause",
            postings=(
                Posting("A", "USD", HostileDecimal("1")),
                posting("B", "USD", "-1"),
            ),
        )
        book = EconomicBook()
        before = book.transactions

        with self.assertRaisesRegex(TypeError, "exact built-in Decimal"):
            book.append(transaction)

        self.assertEqual(touched, [])
        self.assertEqual(book.transactions, before)





class AccountingBoundedIngressTests(unittest.TestCase):
    def test_at_limit_string_and_integer_are_admitted_exactly(self):
        text = "9" * MAX_INTEGER_DIGITS
        self.assertEqual(posting("A", "USD", text).signed_amount, Decimal(text))

        integer = 10 ** (MAX_INTEGER_DIGITS - 1)
        self.assertEqual(posting("A", "USD", integer).signed_amount, Decimal(integer))

    def test_one_over_string_rejects_before_book_mutation(self):
        oversized = "9" * (MAX_INTEGER_DIGITS + 1)
        transaction = JournalTransaction(
            transaction_id="bounded-ingress-string",
            cause_event_id="bounded-ingress-string-cause",
            postings=(
                Posting("A", "USD", oversized),
                Posting("B", "USD", "-1"),
            ),
        )
        book = EconomicBook()
        before = book.transactions

        with self.assertRaisesRegex(ValueError, "resource envelope"):
            book.append(transaction)

        self.assertEqual(book.transactions, before)

    def test_one_over_integer_rejects_before_book_mutation(self):
        oversized = 10 ** MAX_INTEGER_DIGITS
        transaction = JournalTransaction(
            transaction_id="bounded-ingress-int",
            cause_event_id="bounded-ingress-int-cause",
            postings=(
                Posting("A", "USD", oversized),
                Posting("B", "USD", "-1"),
            ),
        )
        book = EconomicBook()
        before = book.transactions

        with self.assertRaisesRegex(ValueError, "resource envelope"):
            book.append(transaction)

        self.assertEqual(book.transactions, before)

    def test_numeric_subclasses_fail_before_virtual_dispatch(self):
        touched = []

        class HostileNumericText(str):
            def __len__(self):
                touched.append("len")
                raise AssertionError("numeric string subclass dispatched")

            def startswith(self, *args, **kwargs):
                touched.append("startswith")
                raise AssertionError("numeric string subclass dispatched")

        class HostileInt(int):
            def bit_length(self):
                touched.append("bit_length")
                raise AssertionError("integer subclass dispatched")

        for value in (HostileNumericText("1"), HostileInt(1)):
            with self.subTest(kind=type(value).__name__):
                with self.assertRaisesRegex(TypeError, "exact built-in Decimal"):
                    posting("A", "USD", value)

        self.assertEqual(touched, [])


if __name__ == "__main__":
    unittest.main()
