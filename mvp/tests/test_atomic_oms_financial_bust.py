from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.accounting import AccountingConflict
from mvp.autotrade_mvp import _provider_activity_accounting_impl as accounting_impl
from mvp.autotrade_mvp.durable_order_projection import DurableOrderBookProjection
from mvp.autotrade_mvp.durable_reservations import DurableReservationBook
from mvp.autotrade_mvp.fill_accounting import (
    ProjectedFillEvidence,
    build_provider_fill_bust_transaction,
)
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.provider_activity_accounting import (
    DurableProviderEconomicBook,
    commit_provider_fill_bust_with_economic_reversal,
    commit_provider_fill_with_reservation_consumption,
)
from mvp.autotrade_mvp.reconciliation import ProviderFillEvidence


PROVIDER = "SIMULATED"
ACCOUNT = "atomic-bust-account"
ENVIRONMENT = "SIMULATION"
WHEN = "2026-10-03T19:00:00Z"
BUST_REVISION = "provider-revision-bust-2"


def books(store: JournalStore):
    return (
        DurableOrderBookProjection(
            store,
            provider_id=PROVIDER,
            account_id=ACCOUNT,
            environment=ENVIRONMENT,
            host_id="atomic-bust-host",
            owner_epoch="1",
        ),
        DurableProviderEconomicBook(
            store,
            provider_id=PROVIDER,
            account_id=ACCOUNT,
            environment=ENVIRONMENT,
        ),
        DurableReservationBook(
            store,
            environment=ENVIRONMENT,
            account_id=ACCOUNT,
        ),
    )


def evidence():
    projected = ProjectedFillEvidence.create(
        fill_id="fill-1",
        provider_execution_id="provider-execution-1",
        intent_id="intent-1",
        client_order_id="order-1",
        side="BUY",
        quantity="1",
        price="100",
    )
    provider = ProviderFillEvidence.create(
        provider_id=PROVIDER,
        account_id=ACCOUNT,
        environment=ENVIRONMENT,
        provider_execution_id="provider-execution-1",
        client_order_id="order-1",
        instrument="ABC",
        quantity="1",
        price="100",
        fee_amount="0",
        fee_currency="USD",
        trade_time=WHEN,
        side="BUY",
    )
    return projected, provider


def seed(
    orders: DurableOrderBookProjection,
    economics: DurableProviderEconomicBook,
    reservations: DurableReservationBook,
):
    reservations.reserve(
        command_id="reserve-1",
        idempotency_key="reserve-1",
        reservation_id="reservation-1",
        intent_id="intent-1",
        requirements={"CASH:USD": "120"},
        available={"CASH:USD": "1000"},
    )
    orders.create_order(
        event_key="create-1",
        client_order_id="order-1",
        instrument="ABC",
        side="BUY",
        requested_quantity="1",
        committed_at=WHEN,
        parent_intent_id="intent-1",
    )
    projected, provider = evidence()
    inserted = commit_provider_fill_with_reservation_consumption(
        economics,
        reservations,
        command_id="provider-fill-1",
        idempotency_key="provider-fill-1",
        reservation_id="reservation-1",
        projected_fill=projected,
        provider_fill=provider,
        expected_instrument="ABC",
        settlement_currency="USD",
        observed_at=WHEN,
        committed_at=WHEN,
        order_book=orders,
        order_event_key="fill-1",
    )
    if not inserted:
        raise AssertionError("seed provider fill must insert")
    return projected, provider


def atomic_bust(
    orders: DurableOrderBookProjection,
    economics: DurableProviderEconomicBook,
    projected: ProjectedFillEvidence,
    provider: ProviderFillEvidence,
):
    return commit_provider_fill_bust_with_economic_reversal(
        economics,
        orders,
        command_id="provider-bust-1",
        idempotency_key="provider-bust-1",
        projected_fill=projected,
        provider_fill=provider,
        expected_instrument="ABC",
        settlement_currency="USD",
        bust_provider_revision=BUST_REVISION,
        bust_observed_at=WHEN,
        order_event_key="bust-1",
        committed_at=WHEN,
    )


class AtomicOmsFinancialBustTests(unittest.TestCase):
    def assert_busted(self, orders, economics, reservations):
        snapshot = orders.order("order-1").snapshot()
        self.assertEqual(snapshot.filled_quantity, Decimal("0"))
        self.assertEqual(snapshot.fill_count, 0)
        self.assertEqual(snapshot.observation_count, 2)
        self.assertEqual(economics.position("ABC"), Decimal("0"))
        self.assertEqual(economics.cash("USD"), Decimal("0"))
        self.assertEqual(len(economics.transactions), 2)
        original, reversal = economics.transactions
        self.assertEqual(reversal.reverses_transaction_id, original.transaction_id)
        self.assertEqual(
            reservations.get("reservation-1").consumed["CASH:USD"],
            Decimal("100"),
        )
        self.assertEqual(
            reservations.get("reservation-1").remaining["CASH:USD"],
            Decimal("20"),
        )

    def test_fresh_bust_is_atomic_restart_safe_and_idempotent(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            orders, economics, reservations = books(store)
            projected, provider = seed(orders, economics, reservations)

            self.assertTrue(
                atomic_bust(orders, economics, projected, provider)
            )
            self.assert_busted(orders, economics, reservations)

            reopened = JournalStore(path)
            ro, re, rr = books(reopened)
            self.assertFalse(atomic_bust(ro, re, projected, provider))
            self.assert_busted(ro, re, rr)

    def test_precommit_failure_leaves_oms_and_economics_unbusted(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            orders, economics, reservations = books(store)
            projected, provider = seed(orders, economics, reservations)
            original = JournalStore.commit_command

            def fail(selected_store, **kwargs):
                if (
                    selected_store is store
                    and kwargs.get("actor")
                    == "atomic-fill-bust-financial-integration"
                ):
                    raise RuntimeError("injected atomic bust failure")
                return original(selected_store, **kwargs)

            JournalStore.commit_command = fail
            try:
                with self.assertRaisesRegex(
                    RuntimeError,
                    "atomic bust failure",
                ):
                    atomic_bust(orders, economics, projected, provider)
            finally:
                JournalStore.commit_command = original

            ro, re, rr = books(JournalStore(path))
            self.assertEqual(
                ro.order("order-1").snapshot().filled_quantity,
                Decimal("1"),
            )
            self.assertEqual(re.position("ABC"), Decimal("1"))
            self.assertEqual(len(re.transactions), 1)
            self.assertEqual(
                rr.get("reservation-1").consumed["CASH:USD"],
                Decimal("100"),
            )

    def test_acknowledgement_loss_retries_exactly_once(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            orders, economics, reservations = books(store)
            projected, provider = seed(orders, economics, reservations)
            original = JournalStore.commit_command
            injected = False

            def lose_ack(selected_store, **kwargs):
                nonlocal injected
                result = original(selected_store, **kwargs)
                if (
                    selected_store is store
                    and kwargs.get("actor")
                    == "atomic-fill-bust-financial-integration"
                    and result[1]
                    and not injected
                ):
                    injected = True
                    raise RuntimeError("injected bust acknowledgement loss")
                return result

            JournalStore.commit_command = lose_ack
            try:
                with self.assertRaisesRegex(
                    RuntimeError,
                    "bust acknowledgement loss",
                ):
                    atomic_bust(orders, economics, projected, provider)
            finally:
                JournalStore.commit_command = original

            ro, re, rr = books(JournalStore(path))
            self.assertFalse(atomic_bust(ro, re, projected, provider))
            self.assert_busted(ro, re, rr)

    def test_replay_rejects_command_effect_from_wrong_aggregate_identity(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            orders, economics, reservations = books(store)
            projected, provider = seed(orders, economics, reservations)
            self.assertTrue(atomic_bust(orders, economics, projected, provider))

            original_load = accounting_impl._economic_store_load_command_event_batch

            def wrong_aggregate(selected_book, **kwargs):
                authority = original_load(selected_book, **kwargs)
                if authority is None:
                    return None
                events = list(authority["events"])
                economic_index = next(
                    index
                    for index, event in enumerate(events)
                    if event.get("aggregate_type") == "economic_book"
                )
                forged = dict(events[economic_index])
                forged["aggregate_id"] = "wrong-economic-book"
                events[economic_index] = forged
                return {
                    **authority,
                    "events": tuple(events),
                }

            accounting_impl._economic_store_load_command_event_batch = wrong_aggregate
            try:
                with self.assertRaisesRegex(
                    AccountingConflict,
                    "unexpected durable effects",
                ):
                    atomic_bust(orders, economics, projected, provider)
            finally:
                accounting_impl._economic_store_load_command_event_batch = original_load

            self.assert_busted(orders, economics, reservations)

    def test_oms_only_legacy_bust_recovers_missing_reversal(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            orders, economics, reservations = books(store)
            projected, provider = seed(orders, economics, reservations)

            self.assertTrue(
                orders.bust_fill(
                    event_key="bust-1",
                    client_order_id="order-1",
                    fill_id="fill-1",
                    provider_revision=BUST_REVISION,
                    committed_at=WHEN,
                ).inserted
            )
            self.assertEqual(
                orders.order("order-1").snapshot().filled_quantity,
                Decimal("0"),
            )
            self.assertEqual(economics.position("ABC"), Decimal("1"))

            self.assertTrue(
                atomic_bust(orders, economics, projected, provider)
            )
            self.assert_busted(orders, economics, reservations)
            self.assertFalse(
                atomic_bust(orders, economics, projected, provider)
            )

    def test_fully_split_bust_state_is_not_relabelled_atomic(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            orders, economics, reservations = books(store)
            projected, provider = seed(orders, economics, reservations)

            reversal = build_provider_fill_bust_transaction(
                book=economics,
                provider_id=PROVIDER,
                projected_fill=projected,
                provider_fill=provider,
                expected_instrument="ABC",
                settlement_currency="USD",
                bust_provider_revision=BUST_REVISION,
                bust_observed_at=WHEN,
            )
            self.assertTrue(
                orders.bust_fill(
                    event_key="bust-1",
                    client_order_id="order-1",
                    fill_id="fill-1",
                    provider_revision=BUST_REVISION,
                    committed_at=WHEN,
                ).inserted
            )
            self.assertTrue(economics.append(reversal))

            with self.assertRaisesRegex(
                AccountingConflict,
                "exist without one atomic/recovery command authority",
            ):
                atomic_bust(orders, economics, projected, provider)

            self.assert_busted(orders, economics, reservations)

    def test_oms_only_recovery_fences_post_cut_journal_mutation(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            orders, economics, reservations = books(store)
            projected, provider = seed(orders, economics, reservations)
            orders.bust_fill(
                event_key="bust-1",
                client_order_id="order-1",
                fill_id="fill-1",
                provider_revision=BUST_REVISION,
                committed_at=WHEN,
            )
            original_prepare = DurableOrderBookProjection.prepare_bust_fill_mutation
            injected = False

            def race_after_cut(selected_book, **kwargs):
                nonlocal injected
                plan = original_prepare(selected_book, **kwargs)
                if selected_book is orders and not injected:
                    injected = True
                    orders.request_cancel(
                        event_key="post-bust-cancel",
                        client_order_id="order-1",
                        command_id="post-bust-cancel-command",
                        committed_at=WHEN,
                    )
                return plan

            DurableOrderBookProjection.prepare_bust_fill_mutation = race_after_cut
            try:
                with self.assertRaisesRegex(
                    ValueError,
                    "journal sequence changed after financial evidence validation",
                ):
                    atomic_bust(orders, economics, projected, provider)
            finally:
                DurableOrderBookProjection.prepare_bust_fill_mutation = original_prepare

            ro, re, rr = books(JournalStore(path))
            self.assertTrue(ro.order("order-1").cancel_requested)
            self.assertEqual(
                ro.order("order-1").snapshot().filled_quantity,
                Decimal("0"),
            )
            self.assertEqual(re.position("ABC"), Decimal("1"))
            self.assertEqual(len(re.transactions), 1)
            self.assertEqual(
                rr.get("reservation-1").consumed["CASH:USD"],
                Decimal("100"),
            )

    def test_settled_source_blocks_bust_without_settlement_compensation_authority(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            orders, economics, reservations = books(store)
            projected, provider = seed(orders, economics, reservations)
            source_id = economics.transactions[0].transaction_id
            original_load = accounting_impl._economic_store_load_events

            def with_settlement(selected_book, aggregate_type, aggregate_id):
                if aggregate_type != "settlement_book":
                    return original_load(selected_book, aggregate_type, aggregate_id)
                scope = {
                    "provider_id": PROVIDER,
                    "account_id": ACCOUNT,
                    "environment": ENVIRONMENT,
                }
                return [
                    {
                        "event_type": "SettlementObligationsRegistered",
                        "payload": {
                            "scope": scope,
                            "obligations": [
                                {
                                    "obligation_id": "settlement-1",
                                    "source_transaction_id": source_id,
                                }
                            ],
                        },
                    },
                    {
                        "event_type": "SettlementEvidenceApplied",
                        "payload": {
                            "scope": scope,
                            "evidence": {
                                "obligation_id": "settlement-1",
                            },
                        },
                    },
                ]

            accounting_impl._economic_store_load_events = with_settlement
            try:
                with self.assertRaisesRegex(
                    AccountingConflict,
                    "settled fill bust requires provider settlement compensation authority",
                ):
                    atomic_bust(orders, economics, projected, provider)
            finally:
                accounting_impl._economic_store_load_events = original_load

            self.assertEqual(
                orders.order("order-1").snapshot().filled_quantity,
                Decimal("1"),
            )
            self.assertEqual(economics.position("ABC"), Decimal("1"))
            self.assertEqual(len(economics.transactions), 1)
            self.assertEqual(
                reservations.get("reservation-1").consumed["CASH:USD"],
                Decimal("100"),
            )

    def test_unsettled_source_does_not_require_settlement_compensation(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            orders, economics, reservations = books(store)
            projected, provider = seed(orders, economics, reservations)
            source_id = economics.transactions[0].transaction_id
            original_load = accounting_impl._economic_store_load_events

            def with_unsettled_registration(selected_book, aggregate_type, aggregate_id):
                if aggregate_type != "settlement_book":
                    return original_load(selected_book, aggregate_type, aggregate_id)
                return [
                    {
                        "event_type": "SettlementObligationsRegistered",
                        "payload": {
                            "scope": {
                                "provider_id": PROVIDER,
                                "account_id": ACCOUNT,
                                "environment": ENVIRONMENT,
                            },
                            "obligations": [
                                {
                                    "obligation_id": "settlement-1",
                                    "source_transaction_id": source_id,
                                }
                            ],
                        },
                    }
                ]

            accounting_impl._economic_store_load_events = with_unsettled_registration
            try:
                self.assertTrue(
                    atomic_bust(orders, economics, projected, provider)
                )
            finally:
                accounting_impl._economic_store_load_events = original_load

            self.assert_busted(orders, economics, reservations)

    def test_finance_only_reversal_fails_closed(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            orders, economics, reservations = books(store)
            projected, provider = seed(orders, economics, reservations)
            reversal = build_provider_fill_bust_transaction(
                book=economics,
                provider_id=PROVIDER,
                projected_fill=projected,
                provider_fill=provider,
                expected_instrument="ABC",
                settlement_currency="USD",
                bust_provider_revision=BUST_REVISION,
                bust_observed_at=WHEN,
            )
            self.assertTrue(economics.append(reversal))

            with self.assertRaisesRegex(
                AccountingConflict,
                "economic fill reversal is committed without the matching OMS bust",
            ):
                atomic_bust(orders, economics, projected, provider)

            self.assertEqual(
                orders.order("order-1").snapshot().filled_quantity,
                Decimal("1"),
            )
            self.assertEqual(economics.position("ABC"), Decimal("0"))
            self.assertEqual(
                reservations.get("reservation-1").consumed["CASH:USD"],
                Decimal("100"),
            )

    def test_paper_bust_fails_closed_without_canonical_provider_bust_authority(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            orders = DurableOrderBookProjection(
                store,
                provider_id=PROVIDER,
                account_id=ACCOUNT,
                environment="PAPER",
                host_id="paper-bust-host",
                owner_epoch="1",
            )
            economics = DurableProviderEconomicBook(
                store,
                provider_id=PROVIDER,
                account_id=ACCOUNT,
                environment="PAPER",
            )
            projected = ProjectedFillEvidence.create(
                fill_id="fill-1",
                provider_execution_id="provider-execution-1",
                intent_id="intent-1",
                client_order_id="order-1",
                side="BUY",
                quantity="1",
                price="100",
            )
            provider = ProviderFillEvidence.create(
                provider_id=PROVIDER,
                account_id=ACCOUNT,
                environment="PAPER",
                provider_execution_id="provider-execution-1",
                client_order_id="order-1",
                instrument="ABC",
                quantity="1",
                price="100",
                fee_amount="0",
                fee_currency="USD",
                trade_time=WHEN,
                side="BUY",
            )

            with self.assertRaisesRegex(
                AccountingConflict,
                "requires canonical provider bust authority",
            ):
                commit_provider_fill_bust_with_economic_reversal(
                    economics,
                    orders,
                    command_id="paper-bust",
                    idempotency_key="paper-bust",
                    projected_fill=projected,
                    provider_fill=provider,
                    expected_instrument="ABC",
                    settlement_currency="USD",
                    bust_provider_revision=BUST_REVISION,
                    bust_observed_at=WHEN,
                    order_event_key="paper-bust-event",
                    committed_at=WHEN,
                )

            self.assertEqual(economics.transactions, ())
            self.assertEqual(
                store.load_events("order_projection_book", orders.aggregate_id),
                [],
            )

    def test_stale_provider_evidence_cannot_bust_newer_oms_revision(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            orders, economics, reservations = books(store)
            projected, provider = seed(orders, economics, reservations)
            orders.correct_fill(
                event_key="correct-oms-only",
                client_order_id="order-1",
                fill_id="fill-1",
                quantity="1",
                price="101",
                provider_revision="provider-revision-correction-1",
                committed_at=WHEN,
            )

            with self.assertRaisesRegex(
                AccountingConflict,
                "OMS state differs from active provider evidence",
            ):
                atomic_bust(orders, economics, projected, provider)

            self.assertEqual(economics.position("ABC"), Decimal("1"))
            self.assertEqual(
                reservations.get("reservation-1").consumed["CASH:USD"],
                Decimal("100"),
            )


if __name__ == "__main__":
    unittest.main()
