from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import NAMESPACE_URL, uuid5

from research.autotrade_research.artifacts.store import ArtifactStore

from mvp.autotrade_mvp.accounting import AccountingConflict
from mvp.autotrade_mvp.durable_order_projection import DurableOrderBookProjection
from mvp.autotrade_mvp.durable_reservations import DurableReservationBook
from mvp.autotrade_mvp.durable_settlement import (
    DurableSettlementBook,
    SETTLEMENT_EVIDENCE_MEDIA_TYPE,
    settlement_rule_evidence_metadata,
    settlement_rule_evidence_receipt,
)
from mvp.autotrade_mvp.fill_accounting import (
    ProjectedFillEvidence,
    build_provider_fill_correction_transactions,
    build_provider_fill_financial_plan,
)
from mvp.autotrade_mvp.persistence import JournalStore, canonical_json
from mvp.autotrade_mvp.provider_activity_accounting import (
    DurableProviderEconomicBook,
    commit_provider_fill_correction_with_settlement_replacement,
    commit_provider_fill_with_reservation_consumption,
)
from mvp.autotrade_mvp.reconciliation import ProviderFillEvidence
from mvp.autotrade_mvp.settlement import (
    SettlementAccountScope,
    SettlementRuleBinding,
    equity_cash_obligation_from_transaction,
)


PROVIDER = "SIMULATED"
ACCOUNT = "atomic-oms-correction-account"
ENVIRONMENT = "SIMULATION"
INITIAL_AT = "2026-10-03T19:00:00Z"
CORRECTION_AT = "2026-10-03T19:10:00Z"


def artifact_store_for(store: JournalStore) -> ArtifactStore:
    return ArtifactStore(store.path.parent / "settlement-evidence")


def books(store: JournalStore, *, environment: str = ENVIRONMENT):
    return (
        DurableOrderBookProjection(
            store,
            provider_id=PROVIDER,
            account_id=ACCOUNT,
            environment=environment,
            host_id="atomic-correction-host",
            owner_epoch="1",
        ),
        DurableProviderEconomicBook(
            store,
            provider_id=PROVIDER,
            account_id=ACCOUNT,
            environment=environment,
        ),
        DurableReservationBook(
            store,
            environment=environment,
            account_id=ACCOUNT,
        ),
        DurableSettlementBook(
            store,
            provider_id=PROVIDER,
            account_id=ACCOUNT,
            environment=environment,
            evidence_artifact_store=artifact_store_for(store),
        ),
    )


def settlement_obligation(
    store: JournalStore,
    transaction,
    *,
    environment: str = ENVIRONMENT,
    obligation_id: str,
):
    rule = SettlementRuleBinding(
        rule_id="atomic-correction-equity-cash",
        rule_version="1",
        scope=SettlementAccountScope(
            provider_id=PROVIDER,
            account_id=ACCOUNT,
            environment=environment,
        ),
        instrument_version="ABC",
        settlement_currency="USD",
        effective_from=date(2026, 10, 1),
        effective_to=None,
        evidence_refs=("instrument:ABC", "rule:atomic-correction:1"),
    )
    receipt = settlement_rule_evidence_receipt(
        rule,
        trade_date=date(2026, 10, 3),
        expected_settlement_date=date(2026, 10, 5),
    )
    artifact_id = str(
        uuid5(
            NAMESPACE_URL,
            "https://evidence.autotrade.local/atomic-oms-correction/"
            + canonical_json(receipt),
        )
    )
    manifest = artifact_store_for(store).publish_bytes(
        artifact_id=artifact_id,
        data=canonical_json(receipt).encode("utf-8"),
        media_type=SETTLEMENT_EVIDENCE_MEDIA_TYPE,
        rights={"storage": True, "export": False},
        source_refs=["provider-doc:atomic-correction-rule"],
        metadata=settlement_rule_evidence_metadata(
            rule,
            trade_date=date(2026, 10, 3),
            expected_settlement_date=date(2026, 10, 5),
        ),
    )
    bound_rule = replace(
        rule,
        evidence_refs=(
            *rule.evidence_refs,
            f"artifact:{artifact_id}@{manifest['sha256']}",
        ),
    )
    return equity_cash_obligation_from_transaction(
        transaction,
        obligation_id=obligation_id,
        instrument="ABC",
        settlement_currency="USD",
        settlement_date=date(2026, 10, 5),
        rule_binding=bound_rule,
    )


def projected_initial():
    return ProjectedFillEvidence.create(
        fill_id="fill-1",
        provider_execution_id="provider-execution-1",
        intent_id="intent-1",
        client_order_id="order-1",
        side="BUY",
        quantity="1",
        price="100",
    )


def provider_initial(*, environment: str = ENVIRONMENT):
    return ProviderFillEvidence.create(
        provider_id=PROVIDER,
        account_id=ACCOUNT,
        environment=environment,
        provider_execution_id="provider-execution-1",
        client_order_id="order-1",
        instrument="ABC",
        quantity="1",
        price="100",
        fee_amount="0",
        fee_currency="USD",
        trade_time=INITIAL_AT,
        side="BUY",
    )


def projected_correction():
    return ProjectedFillEvidence.create(
        fill_id="fill-correction-1",
        provider_execution_id="provider-execution-1",
        intent_id="intent-1",
        client_order_id="order-1",
        side="BUY",
        quantity="1.1",
        price="100",
        provider_revision="revision-2",
        correction_of="fill-1",
    )


def provider_correction(*, environment: str = ENVIRONMENT):
    return ProviderFillEvidence.create(
        provider_id=PROVIDER,
        account_id=ACCOUNT,
        environment=environment,
        provider_execution_id="provider-execution-1",
        client_order_id="order-1",
        instrument="ABC",
        quantity="1.1",
        price="100",
        fee_amount="0",
        fee_currency="USD",
        trade_time=INITIAL_AT,
        side="BUY",
    )


def seed_initial(
    orders: DurableOrderBookProjection,
    economics: DurableProviderEconomicBook,
    reservations: DurableReservationBook,
    settlements: DurableSettlementBook,
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
        requested_quantity="2",
        committed_at=INITIAL_AT,
        parent_intent_id="intent-1",
    )
    projected = projected_initial()
    provider = provider_initial()
    plan = build_provider_fill_financial_plan(
        book=economics,
        provider_id=PROVIDER,
        projected_fill=projected,
        provider_fill=provider,
        expected_instrument="ABC",
        settlement_currency="USD",
        reservation_snapshot=reservations.get("reservation-1"),
        observed_at=INITIAL_AT,
    )
    obligation = settlement_obligation(
        economics.store,
        plan.transaction,
        obligation_id="initial-settlement",
    )
    inserted = commit_provider_fill_with_reservation_consumption(
        economics,
        reservations,
        command_id="initial-fill",
        idempotency_key="initial-fill",
        reservation_id="reservation-1",
        projected_fill=projected,
        provider_fill=provider,
        expected_instrument="ABC",
        settlement_currency="USD",
        observed_at=INITIAL_AT,
        committed_at=INITIAL_AT,
        settlement_book=settlements,
        settlement_obligations=(obligation,),
        order_book=orders,
        order_event_key="fill-1",
    )
    if not inserted:
        raise AssertionError("initial fill must be freshly committed")
    return projected, provider


def correction_obligation(
    economics: DurableProviderEconomicBook,
    *,
    original_projected,
    original_provider,
    corrected_projected,
    corrected_provider,
):
    _reversal, replacement = build_provider_fill_correction_transactions(
        book=economics,
        provider_id=PROVIDER,
        original_projected_fill=original_projected,
        original_provider_fill=original_provider,
        corrected_projected_fill=corrected_projected,
        corrected_provider_fill=corrected_provider,
        expected_instrument="ABC",
        settlement_currency="USD",
        correction_observed_at=CORRECTION_AT,
    )
    return settlement_obligation(
        economics.store,
        replacement,
        obligation_id="corrected-settlement",
    )


def atomic_correction(
    orders: DurableOrderBookProjection,
    economics: DurableProviderEconomicBook,
    reservations: DurableReservationBook,
    settlements: DurableSettlementBook,
    *,
    with_order: bool = True,
):
    original_projected = projected_initial()
    original_provider = provider_initial()
    corrected_projected = projected_correction()
    corrected_provider = provider_correction()
    obligation = correction_obligation(
        economics,
        original_projected=original_projected,
        original_provider=original_provider,
        corrected_projected=corrected_projected,
        corrected_provider=corrected_provider,
    )
    kwargs = dict(
        reservation_book=reservations,
        reservation_id="reservation-1",
        command_id="correction-1",
        idempotency_key="correction-1",
        original_projected_fill=original_projected,
        original_provider_fill=original_provider,
        corrected_projected_fill=corrected_projected,
        corrected_provider_fill=corrected_provider,
        expected_instrument="ABC",
        settlement_currency="USD",
        correction_observed_at=CORRECTION_AT,
        settlement_obligations=(obligation,),
        committed_at=CORRECTION_AT,
    )
    if with_order:
        kwargs.update(
            order_book=orders,
            order_event_key="correct-fill-1",
        )
    return commit_provider_fill_correction_with_settlement_replacement(
        economics,
        settlements,
        **kwargs,
    )


class AtomicOmsFinancialCorrectionTests(unittest.TestCase):
    def assert_corrected(self, orders, economics, reservations, settlements):
        snapshot = orders.order("order-1").snapshot()
        self.assertEqual(snapshot.filled_quantity, Decimal("1.1"))
        self.assertEqual(snapshot.fill_count, 1)
        self.assertEqual(economics.position("ABC"), Decimal("1.1"))
        self.assertEqual(len(economics.transactions), 3)
        reservation = reservations.get("reservation-1")
        self.assertEqual(reservation.consumed["CASH:USD"], Decimal("110"))
        self.assertEqual(reservation.remaining["CASH:USD"], Decimal("10"))
        self.assertEqual(len(settlements.obligations), 2)

    def test_restart_retry_replays_one_atomic_correction(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            orders, economics, reservations, settlements = books(store)
            seed_initial(orders, economics, reservations, settlements)

            self.assertTrue(
                atomic_correction(orders, economics, reservations, settlements)
            )
            self.assert_corrected(orders, economics, reservations, settlements)

            ro, re, rr, rs = books(JournalStore(path))
            self.assertFalse(atomic_correction(ro, re, rr, rs))
            self.assert_corrected(ro, re, rr, rs)

    def test_precommit_failure_leaves_every_correction_projection_unchanged(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            orders, economics, reservations, settlements = books(store)
            seed_initial(orders, economics, reservations, settlements)
            before_order = orders.order("order-1").snapshot()
            before_transactions = economics.transactions
            before_reservation = reservations.get("reservation-1")
            before_obligations = settlements.obligations
            original = JournalStore.commit_command

            def fail(selected_store, **kwargs):
                if selected_store is store:
                    raise RuntimeError("injected atomic correction failure")
                return original(selected_store, **kwargs)

            JournalStore.commit_command = fail
            try:
                with self.assertRaisesRegex(
                    RuntimeError,
                    "atomic correction failure",
                ):
                    atomic_correction(
                        orders,
                        economics,
                        reservations,
                        settlements,
                    )
            finally:
                JournalStore.commit_command = original

            ro, re, rr, rs = books(JournalStore(path))
            self.assertEqual(ro.order("order-1").snapshot(), before_order)
            self.assertEqual(re.transactions, before_transactions)
            self.assertEqual(rr.get("reservation-1"), before_reservation)
            self.assertEqual(rs.obligations, before_obligations)

    def test_ack_loss_after_correction_is_exactly_once_after_restart(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            orders, economics, reservations, settlements = books(store)
            seed_initial(orders, economics, reservations, settlements)
            original = JournalStore.commit_command
            injected = False

            def lose_ack(selected_store, **kwargs):
                nonlocal injected
                result = original(selected_store, **kwargs)
                if (
                    selected_store is store
                    and result[1]
                    and not injected
                    and kwargs.get("actor")
                    == "atomic-settlement-correction-integration"
                ):
                    injected = True
                    raise RuntimeError("injected correction acknowledgement loss")
                return result

            JournalStore.commit_command = lose_ack
            try:
                with self.assertRaisesRegex(
                    RuntimeError,
                    "correction acknowledgement loss",
                ):
                    atomic_correction(
                        orders,
                        economics,
                        reservations,
                        settlements,
                    )
            finally:
                JournalStore.commit_command = original

            ro, re, rr, rs = books(JournalStore(path))
            self.assertFalse(atomic_correction(ro, re, rr, rs))
            self.assert_corrected(ro, re, rr, rs)

    def test_oms_only_correction_recovers_missing_finances(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            orders, economics, reservations, settlements = books(store)
            seed_initial(orders, economics, reservations, settlements)
            corrected = projected_correction()
            self.assertTrue(
                orders.correct_fill(
                    event_key="correct-fill-1",
                    client_order_id="order-1",
                    fill_id=corrected.correction_of,
                    quantity=corrected.quantity,
                    price=corrected.price,
                    provider_revision=corrected.provider_revision,
                    correction_fill_id=corrected.fill_id,
                    committed_at=CORRECTION_AT,
                ).inserted
            )

            self.assertTrue(
                atomic_correction(orders, economics, reservations, settlements)
            )
            self.assert_corrected(orders, economics, reservations, settlements)
            self.assertFalse(
                atomic_correction(orders, economics, reservations, settlements)
            )

    def test_finance_only_correction_cannot_be_relabelled_atomic(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            orders, economics, reservations, settlements = books(store)
            seed_initial(orders, economics, reservations, settlements)

            self.assertTrue(
                atomic_correction(
                    orders,
                    economics,
                    reservations,
                    settlements,
                    with_order=False,
                )
            )
            self.assertEqual(
                orders.order("order-1").snapshot().filled_quantity,
                Decimal("1"),
            )

            with self.assertRaisesRegex(
                AccountingConflict,
                "financial correction is committed without the matching OMS correction",
            ):
                atomic_correction(
                    orders,
                    economics,
                    reservations,
                    settlements,
                )

    def test_paper_provider_correction_requires_atomic_oms_before_finance(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            _orders, economics, reservations, settlements = books(
                store,
                environment="PAPER",
            )
            with self.assertRaisesRegex(
                AccountingConflict,
                "require atomic canonical order correction",
            ):
                commit_provider_fill_correction_with_settlement_replacement(
                    economics,
                    settlements,
                    reservation_book=reservations,
                    reservation_id="reservation-1",
                    command_id="paper-correction",
                    idempotency_key="paper-correction",
                    original_projected_fill=projected_initial(),
                    original_provider_fill=provider_initial(
                        environment="PAPER"
                    ),
                    corrected_projected_fill=projected_correction(),
                    corrected_provider_fill=provider_correction(
                        environment="PAPER"
                    ),
                    expected_instrument="ABC",
                    settlement_currency="USD",
                    correction_observed_at=CORRECTION_AT,
                    settlement_obligations=(),
                    committed_at=CORRECTION_AT,
                )


if __name__ == "__main__":
    unittest.main()
