from datetime import date
from decimal import (
    Decimal,
    ROUND_CEILING,
    ROUND_FLOOR,
    ROUND_HALF_EVEN,
    localcontext,
)
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import NAMESPACE_URL, uuid5

from mvp.autotrade_mvp.accounting import (
    AccountingConflict,
    book_equity_fill,
)
from mvp.autotrade_mvp.durable_reservations import DurableReservationBook
from mvp.autotrade_mvp.durable_settlement import (
    DurableSettlementBook,
    SETTLEMENT_EVIDENCE_MEDIA_TYPE,
    settlement_rule_evidence_metadata,
    settlement_rule_evidence_receipt,
)
from mvp.tests._journal_store_patch import patch_journal_store_method
from mvp.autotrade_mvp.persistence import JournalStore, canonical_json, payload_digest
from mvp.autotrade_mvp.fill_accounting import (
    ProjectedFillEvidence,
    build_provider_fill_correction_transactions,
    build_provider_fill_financial_plan,
)
from mvp.autotrade_mvp.provider_activity_accounting import (
    DurableProviderEconomicBook,
    commit_economic_batch_with_reservation_consumption,
    commit_economic_correction_with_settlement_replacement,
    commit_provider_fill_correction_with_settlement_replacement,
    commit_provider_fill_with_reservation_consumption,
)
from mvp.autotrade_mvp._provider_activity_accounting_impl import (
    _cash_leg_totals,
    _cash_outflow_usage,
    _exact_usage_increase,
    _projected_fill_binding_payload,
    _provider_fill_binding_payload,
    _usage_payload,
)
from mvp.autotrade_mvp.reconciliation import ProviderFillEvidence
from mvp.autotrade_mvp.reservations import ReservationConflict
from research.autotrade_research.artifacts.store import ArtifactStore

from mvp.autotrade_mvp.settlement import (
    SettlementAccountScope,
    SettlementRuleBinding,
    equity_cash_obligation_from_transaction,
)


PROVIDER = "PROVIDER-A"
ACCOUNT = "acct-1"
ENVIRONMENT = "PAPER"


def reservation_book(store: JournalStore) -> DurableReservationBook:
    return DurableReservationBook(
        store,
        environment=ENVIRONMENT,
        account_id=ACCOUNT,
    )


def economic_book(store: JournalStore) -> DurableProviderEconomicBook:
    return DurableProviderEconomicBook(
        store,
        provider_id=PROVIDER,
        account_id=ACCOUNT,
        environment=ENVIRONMENT,
    )


def artifact_store_for(store: JournalStore) -> ArtifactStore:
    return ArtifactStore(store.path.parent / "settlement-evidence")


def settlement_book(store: JournalStore) -> DurableSettlementBook:
    return DurableSettlementBook(
        store,
        provider_id=PROVIDER,
        account_id=ACCOUNT,
        environment=ENVIRONMENT,
        evidence_artifact_store=artifact_store_for(store),
    )


def settlement_obligation(
    store: JournalStore,
    transaction,
    *,
    obligation_id: str = "settlement-economic-fill-1",
):
    rule = SettlementRuleBinding(
        rule_id="test-equity-cash",
        rule_version="1",
        scope=SettlementAccountScope(
            provider_id=PROVIDER,
            account_id=ACCOUNT,
            environment=ENVIRONMENT,
        ),
        instrument_version="ABC",
        settlement_currency="USD",
        effective_from=date(2026, 9, 1),
        effective_to=None,
        evidence_refs=("instrument:ABC", "rule:test-equity-cash:1"),
    )
    receipt = settlement_rule_evidence_receipt(
        rule,
        trade_date=date(2026, 9, 25),
        expected_settlement_date=date(2026, 9, 26),
    )
    artifact_id = str(
        uuid5(
            NAMESPACE_URL,
            "https://evidence.autotrade.local/atomic-settlement-rule/"
            + canonical_json(receipt),
        )
    )
    manifest = artifact_store_for(store).publish_bytes(
        artifact_id=artifact_id,
        data=canonical_json(receipt).encode("utf-8"),
        media_type=SETTLEMENT_EVIDENCE_MEDIA_TYPE,
        rights={"storage": True, "export": False},
        source_refs=["provider-doc:test-settlement-rule"],
        metadata=settlement_rule_evidence_metadata(
            rule,
            trade_date=date(2026, 9, 25),
            expected_settlement_date=date(2026, 9, 26),
        ),
    )
    rule = replace(
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
        settlement_date=date(2026, 9, 26),
        rule_binding=rule,
    )


def reserve(book: DurableReservationBook) -> None:
    book.reserve(
        command_id="reserve-command",
        idempotency_key="reserve-idempotency",
        reservation_id="reservation-1",
        intent_id="intent-1",
        requirements={"CASH:USD": "120"},
        available={"CASH:USD": "1000"},
    )


def fill_transaction(*, price: str = "100"):
    return book_equity_fill(
        transaction_id="economic-fill-1",
        cause_event_id="provider-execution-1",
        instrument="ABC",
        settlement_currency="USD",
        side="BUY",
        quantity="1",
        price=price,
        economic_effective_at="2026-09-25T09:00:00Z",
        economic_order_key="provider:PROVIDER-A:execution:provider-execution-1",
        observed_at="2026-09-25T09:00:01Z",
    )


def commit_fill(
    economics: DurableProviderEconomicBook,
    reservations: DurableReservationBook,
    *,
    usage: str = "100",
    transaction=None,
    settlements: DurableSettlementBook | None = None,
):
    economic_transaction = fill_transaction() if transaction is None else transaction
    kwargs = {}
    if settlements is not None:
        kwargs = {
            "settlement_book": settlements,
            "settlement_obligations": (
                settlement_obligation(settlements.store, economic_transaction),
            ),
        }
    return commit_economic_batch_with_reservation_consumption(
        economics,
        reservations,
        command_id="fill-financial-command-1",
        idempotency_key="fill-financial-idempotency-1",
        reservation_id="reservation-1",
        usage={"CASH:USD": usage},
        transactions=(economic_transaction,),
        committed_at="2026-09-25T09:00:02Z",
        **kwargs,
    )


class AtomicFillFinancialCommitTests(unittest.TestCase):
    def test_atomic_fill_rejects_polymorphic_or_shadowed_financial_authorities(self):
        class ForgedEconomicBook(DurableProviderEconomicBook):
            def prepare_batch_mutation(self, *_args, **_kwargs):
                raise AssertionError("economic subclass virtual dispatch must not run")

        class ForgedReservationBook(DurableReservationBook):
            def get(self, *_args, **_kwargs):
                raise AssertionError("reservation subclass get must not run")

            def prepare_consume_mutation(self, *_args, **_kwargs):
                raise AssertionError("reservation subclass prepare must not run")

        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            canonical_economics = economic_book(store)
            canonical_reservations = reservation_book(store)
            forged_economics = ForgedEconomicBook(
                store,
                provider_id=PROVIDER,
                account_id=ACCOUNT,
                environment=ENVIRONMENT,
            )
            forged_reservations = ForgedReservationBook(
                store,
                environment=ENVIRONMENT,
                account_id=ACCOUNT,
            )
            common = {
                "command_id": "hostile-fill-command",
                "idempotency_key": "hostile-fill-idem",
                "reservation_id": "reservation-1",
                "usage": {"CASH:USD": "1"},
                "transactions": (),
                "committed_at": "2026-09-25T09:00:02Z",
            }

            with self.assertRaisesRegex(
                TypeError,
                "canonical DurableProviderEconomicBook",
            ):
                commit_economic_batch_with_reservation_consumption(
                    forged_economics,
                    canonical_reservations,
                    **common,
                )
            with self.assertRaisesRegex(
                TypeError,
                "canonical DurableReservationBook",
            ):
                commit_economic_batch_with_reservation_consumption(
                    canonical_economics,
                    forged_reservations,
                    **common,
                )

            shadow_called = False

            def hostile_prepare(*_args, **_kwargs):
                nonlocal shadow_called
                shadow_called = True
                raise AssertionError("shadowed reservation method must not run")

            canonical_reservations.prepare_consume_mutation = hostile_prepare
            with self.assertRaisesRegex(TypeError, "reservation_book authority is shadowed"):
                commit_economic_batch_with_reservation_consumption(
                    canonical_economics,
                    canonical_reservations,
                    **common,
                )
            self.assertFalse(shadow_called)

    def test_atomic_correction_rejects_polymorphic_settlement_before_virtual_dispatch(self):
        class ForgedSettlementBook(DurableSettlementBook):
            def prepare_register_mutation(self, *_args, **_kwargs):
                raise AssertionError("settlement subclass virtual dispatch must not run")

        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            economics = economic_book(store)
            forged_settlement = ForgedSettlementBook(
                store,
                provider_id=PROVIDER,
                account_id=ACCOUNT,
                environment=ENVIRONMENT,
                evidence_artifact_store=artifact_store_for(store),
            )
            with self.assertRaisesRegex(
                TypeError,
                "canonical DurableSettlementBook",
            ):
                commit_economic_correction_with_settlement_replacement(
                    economics,
                    forged_settlement,
                    command_id="hostile-correction-command",
                    idempotency_key="hostile-correction-idem",
                    reversal=None,
                    replacement=None,
                    settlement_obligations=(),
                    committed_at="2026-09-25T09:00:02Z",
                )

    def test_atomic_fill_rejects_shadowed_journal_before_financial_preparation(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            reservations = reservation_book(store)
            economics = economic_book(store)
            called = False

            def hostile_commit(*_args, **_kwargs):
                nonlocal called
                called = True
                raise AssertionError("shadowed JournalStore commit must not run")

            store.commit_command = hostile_commit
            with self.assertRaisesRegex(TypeError, "JournalStore.*shadowed"):
                commit_economic_batch_with_reservation_consumption(
                    economics,
                    reservations,
                    command_id="shadowed-store-command",
                    idempotency_key="shadowed-store-idem",
                    reservation_id="reservation-1",
                    usage={"CASH:USD": "1"},
                    transactions=(),
                    committed_at="2026-09-25T09:00:02Z",
                )
            self.assertFalse(called)

    def test_fill_economics_and_reservation_consumption_restart_together(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            reservations = reservation_book(store)
            economics = economic_book(store)
            reserve(reservations)

            self.assertTrue(commit_fill(economics, reservations))
            snapshot = reservations.get("reservation-1")
            self.assertEqual(snapshot.consumed["CASH:USD"], Decimal("100"))
            self.assertEqual(snapshot.remaining["CASH:USD"], Decimal("20"))
            self.assertEqual(economics.position("ABC"), Decimal("1"))

            reopened_store = JournalStore(path)
            reopened_reservations = reservation_book(reopened_store)
            reopened_economics = economic_book(reopened_store)
            reopened_snapshot = reopened_reservations.get("reservation-1")
            self.assertEqual(
                reopened_snapshot.consumed["CASH:USD"],
                Decimal("100"),
            )
            self.assertEqual(
                reopened_snapshot.remaining["CASH:USD"],
                Decimal("20"),
            )
            self.assertEqual(reopened_economics.position("ABC"), Decimal("1"))
            self.assertEqual(len(reopened_economics.transactions), 1)

    def test_failure_before_shared_commit_leaves_neither_projection_mutated(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            reservations = reservation_book(store)
            economics = economic_book(store)
            reserve(reservations)

            original_commit = store.commit_command

            def fail_before_commit(**kwargs):
                raise RuntimeError("injected pre-commit failure")

            commit_patch = patch_journal_store_method(

                store, "commit_command", side_effect=fail_before_commit

            )

            commit_patch.start()
            try:
                with self.assertRaisesRegex(RuntimeError, "pre-commit"):
                    commit_fill(economics, reservations)
            finally:
                commit_patch.stop()

            reopened_store = JournalStore(path)
            reopened_reservations = reservation_book(reopened_store)
            reopened_economics = economic_book(reopened_store)
            snapshot = reopened_reservations.get("reservation-1")
            self.assertEqual(snapshot.consumed["CASH:USD"], Decimal("0"))
            self.assertEqual(snapshot.remaining["CASH:USD"], Decimal("120"))
            self.assertEqual(reopened_economics.transactions, ())

    def test_ack_loss_after_shared_commit_retries_without_duplicate(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            reservations = reservation_book(store)
            economics = economic_book(store)
            reserve(reservations)

            original_commit = store.commit_command
            injected = False

            def lose_ack_after_commit(**kwargs):
                nonlocal injected
                result = original_commit(**kwargs)
                if not injected and result[1]:
                    injected = True
                    raise RuntimeError("injected acknowledgement loss")
                return result

            commit_patch = patch_journal_store_method(

                store, "commit_command", side_effect=lose_ack_after_commit

            )

            commit_patch.start()
            try:
                with self.assertRaisesRegex(RuntimeError, "acknowledgement loss"):
                    commit_fill(economics, reservations)
            finally:
                commit_patch.stop()

            reopened_store = JournalStore(path)
            reopened_reservations = reservation_book(reopened_store)
            reopened_economics = economic_book(reopened_store)
            self.assertFalse(
                commit_fill(reopened_economics, reopened_reservations)
            )
            snapshot = reopened_reservations.get("reservation-1")
            self.assertEqual(snapshot.consumed["CASH:USD"], Decimal("100"))
            self.assertEqual(snapshot.remaining["CASH:USD"], Decimal("20"))
            self.assertEqual(reopened_economics.position("ABC"), Decimal("1"))
            self.assertEqual(len(reopened_economics.transactions), 1)

    def test_preexisting_economics_without_reservation_consumption_fails_closed(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            reservations = reservation_book(store)
            economics = economic_book(store)
            reserve(reservations)
            self.assertTrue(economics.append(fill_transaction()))

            with self.assertRaisesRegex(
                AccountingConflict,
                "partially committed",
            ):
                commit_fill(economics, reservations)

            snapshot = reservations.get("reservation-1")
            self.assertEqual(snapshot.consumed["CASH:USD"], Decimal("0"))
            self.assertEqual(snapshot.remaining["CASH:USD"], Decimal("120"))
            self.assertEqual(economics.position("ABC"), Decimal("1"))

    def test_exact_retry_cannot_change_reserved_usage(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            reservations = reservation_book(store)
            economics = economic_book(store)
            reserve(reservations)
            self.assertTrue(commit_fill(economics, reservations))

            with self.assertRaisesRegex(
                ReservationConflict,
                "different reservation request",
            ):
                commit_fill(economics, reservations, usage="101")

            snapshot = reservations.get("reservation-1")
            self.assertEqual(snapshot.consumed["CASH:USD"], Decimal("100"))
            self.assertEqual(snapshot.remaining["CASH:USD"], Decimal("20"))
            self.assertEqual(len(economics.transactions), 1)

    def test_preexisting_reservation_consumption_without_economics_fails_closed(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            reservations = reservation_book(store)
            economics = economic_book(store)
            reserve(reservations)

            component_key = (
                "atomic-fill-reservation:"
                + sha256(
                    canonical_json(
                        [
                            PROVIDER,
                            ACCOUNT,
                            ENVIRONMENT,
                            "fill-financial-idempotency-1",
                        ]
                    ).encode("utf-8")
                ).hexdigest()
            )
            # Use the public preparation seam to emulate a legacy/crashed
            # partial integration without mutating the economic aggregate.
            plan = reservations.prepare_consume_mutation(
                event_key="partial-reservation-event",
                idempotency_key=component_key,
                reservation_id="reservation-1",
                usage={"CASH:USD": "100"},
                committed_at="2026-09-25T09:00:02Z",
            )
            self.assertIsNotNone(plan.envelope)
            store.commit_command(
                command_id="partial-reservation-only",
                actor="test-partial-financial-state",
                environment=ENVIRONMENT,
                idempotency_key="partial-reservation-only",
                request=plan.request,
                result=plan.snapshot_payload,
                state_version=plan.aggregate_version,
                events=[(plan.envelope, None)],
            )
            reservations.refresh()

            with self.assertRaisesRegex(
                AccountingConflict,
                "partially committed",
            ):
                commit_fill(economics, reservations)

            snapshot = reservations.get("reservation-1")
            self.assertEqual(snapshot.consumed["CASH:USD"], Decimal("100"))
            self.assertEqual(snapshot.remaining["CASH:USD"], Decimal("20"))
            self.assertEqual(economics.transactions, ())

    def test_fill_settlement_provenance_commits_and_restarts_atomically(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            reservations = reservation_book(store)
            economics = economic_book(store)
            settlements = settlement_book(store)
            reserve(reservations)

            self.assertTrue(
                commit_fill(
                    economics,
                    reservations,
                    settlements=settlements,
                )
            )
            self.assertEqual(len(settlements.obligations), 1)
            self.assertEqual(
                settlements.obligations[0].source_transaction_id,
                "economic-fill-1",
            )

            reopened_store = JournalStore(path)
            reopened_reservations = reservation_book(reopened_store)
            reopened_economics = economic_book(reopened_store)
            reopened_settlements = settlement_book(reopened_store)
            self.assertEqual(
                reopened_reservations.get("reservation-1").consumed["CASH:USD"],
                Decimal("100"),
            )
            self.assertEqual(len(reopened_economics.transactions), 1)
            self.assertEqual(len(reopened_settlements.obligations), 1)
            projected = reopened_settlements.project(reopened_economics)
            self.assertEqual(
                projected.snapshot("USD").unsettled_payable,
                Decimal("100"),
            )

    def test_atomic_fill_ack_loss_does_not_duplicate_settlement_provenance(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            reservations = reservation_book(store)
            economics = economic_book(store)
            settlements = settlement_book(store)
            reserve(reservations)

            original_commit = store.commit_command
            injected = False

            def lose_ack_after_commit(**kwargs):
                nonlocal injected
                result = original_commit(**kwargs)
                if not injected and result[1]:
                    injected = True
                    raise RuntimeError("injected settlement acknowledgement loss")
                return result

            commit_patch = patch_journal_store_method(

                store, "commit_command", side_effect=lose_ack_after_commit

            )

            commit_patch.start()
            try:
                with self.assertRaisesRegex(RuntimeError, "acknowledgement loss"):
                    commit_fill(
                        economics,
                        reservations,
                        settlements=settlements,
                    )
            finally:
                commit_patch.stop()

            reopened_store = JournalStore(path)
            reopened_reservations = reservation_book(reopened_store)
            reopened_economics = economic_book(reopened_store)
            reopened_settlements = settlement_book(reopened_store)
            self.assertFalse(
                commit_fill(
                    reopened_economics,
                    reopened_reservations,
                    settlements=reopened_settlements,
                )
            )
            self.assertEqual(len(reopened_economics.transactions), 1)
            self.assertEqual(len(reopened_settlements.obligations), 1)
            self.assertEqual(
                reopened_reservations.get("reservation-1").consumed["CASH:USD"],
                Decimal("100"),
            )

    def test_preexisting_settlement_without_fill_pair_fails_closed(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            reservations = reservation_book(store)
            economics = economic_book(store)
            settlements = settlement_book(store)
            reserve(reservations)
            transaction = fill_transaction()
            obligation = settlement_obligation(store, transaction)
            self.assertTrue(
                settlements.register_obligations(
                    (obligation,),
                    command_id="legacy-settlement-only",
                    idempotency_key="legacy-settlement-only",
                    committed_at="2026-09-25T09:00:01Z",
                )
            )

            with self.assertRaisesRegex(
                AccountingConflict,
                "partially committed",
            ):
                commit_fill(
                    economics,
                    reservations,
                    transaction=transaction,
                    settlements=settlements,
                )

            self.assertEqual(economics.transactions, ())
            self.assertEqual(
                reservations.get("reservation-1").consumed["CASH:USD"],
                Decimal("0"),
            )
            self.assertEqual(len(settlements.obligations), 1)

    def test_failure_before_three_way_commit_leaves_settlement_unregistered(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            reservations = reservation_book(store)
            economics = economic_book(store)
            settlements = settlement_book(store)
            reserve(reservations)

            original_commit = store.commit_command

            def fail_before_commit(**kwargs):
                raise RuntimeError("injected three-way pre-commit failure")

            commit_patch = patch_journal_store_method(

                store, "commit_command", side_effect=fail_before_commit

            )

            commit_patch.start()
            try:
                with self.assertRaisesRegex(RuntimeError, "pre-commit"):
                    commit_fill(
                        economics,
                        reservations,
                        settlements=settlements,
                    )
            finally:
                commit_patch.stop()

            reopened_store = JournalStore(path)
            self.assertEqual(economic_book(reopened_store).transactions, ())
            self.assertEqual(
                reservation_book(reopened_store)
                .get("reservation-1")
                .consumed["CASH:USD"],
                Decimal("0"),
            )
            self.assertEqual(settlement_book(reopened_store).obligations, ())

    def test_settlement_coverage_must_include_every_fill_cash_currency(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            reservations = reservation_book(store)
            economics = economic_book(store)
            settlements = settlement_book(store)
            reserve(reservations)
            transaction = book_equity_fill(
                transaction_id="economic-fill-1",
                cause_event_id="provider-execution-1",
                instrument="ABC",
                settlement_currency="USD",
                side="BUY",
                quantity="1",
                price="100",
                fee="1",
                fee_currency="EUR",
                economic_effective_at="2026-09-25T09:00:00Z",
                economic_order_key="provider:PROVIDER-A:execution:provider-execution-1",
                observed_at="2026-09-25T09:00:01Z",
            )
            with self.assertRaisesRegex(
                AccountingConflict,
                "cover every atomic fill cash leg",
            ):
                commit_fill(
                    economics,
                    reservations,
                    transaction=transaction,
                    settlements=settlements,
                )
            self.assertEqual(economics.transactions, ())
            self.assertEqual(settlements.obligations, ())
            self.assertEqual(
                reservations.get("reservation-1").consumed["CASH:USD"],
                Decimal("0"),
            )

    def test_cross_scope_books_are_rejected_before_mutation(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            reservations = DurableReservationBook(
                store,
                environment=ENVIRONMENT,
                account_id="other-account",
            )
            economics = economic_book(store)
            reservations.reserve(
                command_id="reserve-other",
                idempotency_key="reserve-other",
                reservation_id="reservation-1",
                intent_id="intent-1",
                requirements={"CASH:USD": "120"},
                available={"CASH:USD": "1000"},
            )

            with self.assertRaisesRegex(ValueError, "account/environment"):
                commit_fill(economics, reservations)
            self.assertEqual(economics.transactions, ())
            self.assertEqual(
                reservations.get("reservation-1").remaining["CASH:USD"],
                Decimal("120"),
            )


class ExactFillFinancialArithmeticTests(unittest.TestCase):
    _CONTEXTS = (
        (6, ROUND_FLOOR),
        (10, ROUND_CEILING),
        (28, ROUND_HALF_EVEN),
        (80, ROUND_HALF_EVEN),
    )

    def test_provider_fill_binding_payload_identity_ignores_ambient_context(self):
        projected = ProjectedFillEvidence.create(
            fill_id="exact-context-fill",
            provider_execution_id="exact-context-execution",
            intent_id="intent-1",
            client_order_id="client-order-1",
            side="BUY",
            quantity="1000001",
            price="1.000001",
            provider_revision="revision-1",
        )
        provider = ProviderFillEvidence.create(
            provider_id=PROVIDER,
            account_id=ACCOUNT,
            environment=ENVIRONMENT,
            provider_execution_id="exact-context-execution",
            client_order_id="client-order-1",
            instrument="ABC",
            quantity="1000001",
            price="1.000001",
            fee_amount="0.0000001",
            fee_currency="USD",
            trade_time="2026-09-25T08:00:00Z",
            side="BUY",
            evidence_refs=("provider-fill:exact-context",),
        )
        observed = set()

        for precision, rounding in self._CONTEXTS:
            with self.subTest(precision=precision, rounding=rounding):
                with localcontext() as context:
                    context.prec = precision
                    context.rounding = rounding
                    projected_payload = _projected_fill_binding_payload(projected)
                    provider_payload = _provider_fill_binding_payload(provider)
                self.assertEqual(projected_payload["quantity"], "1000001")
                self.assertEqual(projected_payload["price"], "1.000001")
                self.assertEqual(provider_payload["quantity"], "1000001")
                self.assertEqual(provider_payload["price"], "1.000001")
                self.assertEqual(provider_payload["fee_amount"], "0.0000001")
                observed.add(
                    (
                        canonical_json(projected_payload),
                        canonical_json(provider_payload),
                    )
                )

        self.assertEqual(len(observed), 1)

    def test_cash_usage_high_water_and_settlement_aggregation_ignore_ambient_context(self):
        transaction = book_equity_fill(
            transaction_id="exact-context-fill",
            cause_event_id="exact-context-execution",
            instrument="ABC",
            settlement_currency="USD",
            side="BUY",
            quantity="1",
            price="100000",
            fee="0.0000001",
            fee_currency="USD",
            economic_effective_at="2026-09-25T08:00:00Z",
            economic_order_key="provider:PROVIDER-A:execution:exact-context-execution",
            observed_at="2026-09-25T08:00:01Z",
        )
        expected_usage = Decimal("100000.0000001")
        expected_cash_effect = Decimal("-100000.0000001")
        observed = set()

        for precision, rounding in self._CONTEXTS:
            with self.subTest(precision=precision, rounding=rounding):
                with localcontext() as context:
                    context.prec = precision
                    context.rounding = rounding
                    usage = _cash_outflow_usage(transaction)
                    cash_legs = _cash_leg_totals((transaction,))
                    additional = _exact_usage_increase(
                        expected_usage,
                        Decimal("100000"),
                    )
                    payload = _usage_payload({"CASH:USD": expected_usage})
                self.assertEqual(usage, {"CASH:USD": expected_usage})
                self.assertEqual(
                    cash_legs,
                    {("exact-context-fill", "USD"): expected_cash_effect},
                )
                self.assertEqual(additional, Decimal("0.0000001"))
                self.assertEqual(payload, {"CASH:USD": "100000.0000001"})
                observed.add(
                    (
                        tuple(usage.items()),
                        tuple(cash_legs.items()),
                        additional,
                        tuple(payload.items()),
                    )
                )

        self.assertEqual(len(observed), 1)


class EvidenceDerivedFillConsumptionTests(unittest.TestCase):
    def setUp(self):
        super().setUp()
        self._admission_patch = patch(
            "mvp.autotrade_mvp._provider_activity_accounting_impl."
            "_resolved_financial_admission_payload",
            side_effect=self._financial_admission_payload,
        )
        self._admission_patch.start()
        self.addCleanup(self._admission_patch.stop)

    def _financial_admission_payload(
        self,
        economic_book,
        *,
        admission_id,
        reservation_id,
        intent_id,
        expected_instrument,
    ):
        if admission_id != "admission-1":
            raise AccountingConflict("test admission selector changed")
        return {
            "schema_version": "1.0.0",
            "admission_id": "admission-1",
            "intent_id": intent_id,
            "reservation_id": reservation_id,
            "provider_id": economic_book.provider_id,
            "account_id": economic_book.account_id,
            "environment": economic_book.environment,
            "instrument_symbol": expected_instrument,
            "instrument": {
                "instrument_id": "11111111-1111-4111-8111-111111111111",
                "version": 1,
            },
            "action": "ORDER.SUBMIT",
            "risk_decision_id": "risk:sha256:" + "a" * 64,
            "financial_command_id": "financial-admission-command-1",
            "request_fingerprint": "b" * 64,
            "authority_event_id": "authority-admission-event-1",
            "authority_event_payload_hash": "sha256:" + "c" * 64,
            "authority_aggregate_version": 2,
            "authority_journal_sequence": 7,
        }

    def projected_fill(
        self,
        *,
        side="BUY",
        quantity="1",
        price="100",
        fill_id="fill-1",
        provider_execution_id="provider-execution-1",
        provider_revision=None,
        correction_of=None,
        position_side=None,
        position_effect=None,
    ):
        return ProjectedFillEvidence.create(
            fill_id=fill_id,
            provider_execution_id=provider_execution_id,
            intent_id="intent-1",
            client_order_id="client-order-1",
            side=side,
            quantity=quantity,
            price=price,
            position_side=position_side,
            position_effect=position_effect,
            provider_revision=provider_revision,
            correction_of=correction_of,
        )

    def provider_fill(
        self,
        *,
        side="BUY",
        quantity="1",
        price="100",
        fee_amount="0",
        fee_currency="USD",
        position_side=None,
        position_effect=None,
        provider_execution_id="provider-execution-1",
        evidence_refs=("provider-fill:test",),
    ):
        return ProviderFillEvidence.create(
            provider_id=PROVIDER,
            account_id=ACCOUNT,
            environment=ENVIRONMENT,
            provider_execution_id=provider_execution_id,
            client_order_id="client-order-1",
            instrument="ABC",
            quantity=quantity,
            price=price,
            fee_amount=fee_amount,
            fee_currency=fee_currency,
            trade_time="2026-09-25T09:00:00Z",
            side=side,
            position_side=position_side,
            position_effect=position_effect,
            evidence_refs=evidence_refs,
        )

    def commit_evidenced_fill(
        self,
        economics,
        reservations,
        *,
        projected=None,
        provider=None,
        asset_family="CASH_EQUITY",
        command_id="evidence-fill-command-1",
        idempotency_key="evidence-fill-idempotency-1",
    ):
        return commit_provider_fill_with_reservation_consumption(
            economics,
            reservations,
            command_id=command_id,
            idempotency_key=idempotency_key,
            reservation_id="reservation-1",
            admission_id="admission-1",
            projected_fill=(
                self.projected_fill() if projected is None else projected
            ),
            provider_fill=(
                self.provider_fill() if provider is None else provider
            ),
            expected_instrument="ABC",
            settlement_currency="USD",
            asset_family=asset_family,
            observed_at="2026-09-25T09:00:01Z",
            committed_at="2026-09-25T09:00:02Z",
        )

    def test_financial_plan_usage_and_digest_ignore_ambient_decimal_context(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            reservations = reservation_book(store)
            reservations.reserve(
                command_id="reserve-exact-context",
                idempotency_key="reserve-exact-context",
                reservation_id="reservation-1",
                intent_id="intent-1",
                requirements={"CASH:USD": "100001"},
                available={"CASH:USD": "200000"},
            )
            economics = economic_book(store)
            projected = self.projected_fill(price="100000")
            provider = self.provider_fill(
                price="100000",
                fee_amount="0.0000001",
            )
            snapshot = reservations.get("reservation-1")
            results = set()

            for precision, rounding in ExactFillFinancialArithmeticTests._CONTEXTS:
                with self.subTest(precision=precision, rounding=rounding):
                    with localcontext() as context:
                        context.prec = precision
                        context.rounding = rounding
                        plan = build_provider_fill_financial_plan(
                            book=economics,
                            provider_id=PROVIDER,
                            projected_fill=projected,
                            provider_fill=provider,
                            expected_instrument="ABC",
                            settlement_currency="USD",
                            reservation_snapshot=snapshot,
                            observed_at="2026-09-25T09:00:01Z",
                        )
                    self.assertEqual(
                        dict(plan.usage),
                        {"CASH:USD": Decimal("100000.0000001")},
                    )
                    results.add(
                        (
                            plan.plan_digest,
                            tuple(plan.usage_items),
                        )
                    )

            self.assertEqual(len(results), 1)

    def test_usage_is_derived_from_provider_fill_not_caller_input(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            reservations = reservation_book(store)
            economics = economic_book(store)
            reserve(reservations)

            self.assertTrue(self.commit_evidenced_fill(economics, reservations))
            snapshot = reservations.get("reservation-1")
            self.assertEqual(snapshot.consumed["CASH:USD"], Decimal("100"))
            self.assertEqual(snapshot.remaining["CASH:USD"], Decimal("20"))
            self.assertEqual(economics.position("ABC"), Decimal("1"))
            bindings = store.load_events_by_aggregate_type(
                "provider_fill_financial_binding"
            )
            self.assertEqual(len(bindings), 1)
            binding = bindings[0]["payload"]["request"]
            self.assertEqual(binding["reservation_id"], "reservation-1")
            self.assertEqual(binding["intent_id"], "intent-1")
            self.assertEqual(
                binding["provider_execution_id"], "provider-execution-1"
            )
            self.assertEqual(binding["fill_id"], "fill-1")
            self.assertEqual(binding["derived_usage"], {"CASH:USD": "100"})
            self.assertTrue(binding["reservation_cut_digest"].startswith("sha256:"))
            self.assertTrue(binding["plan_digest"].startswith("sha256:"))
            self.assertTrue(binding["transaction_digest"].startswith("sha256:"))
            self.assertTrue(binding["projected_fill_digest"].startswith("sha256:"))
            self.assertTrue(binding["provider_fill_digest"].startswith("sha256:"))
            self.assertEqual(
                binding["provider_fill"]["evidence_refs"],
                ["provider-fill:test"],
            )
            self.assertEqual(binding["schema_version"], "1.2.0")
            self.assertEqual(
                binding["financial_admission"]["admission_id"],
                "admission-1",
            )
            self.assertEqual(
                binding["financial_admission"]["reservation_id"],
                "reservation-1",
            )
            self.assertEqual(
                binding["financial_admission"]["intent_id"],
                "intent-1",
            )
            self.assertEqual(
                binding["financial_admission"]["provider_id"],
                PROVIDER,
            )
            self.assertEqual(
                binding["financial_admission"]["instrument_symbol"],
                "ABC",
            )
            self.assertEqual(
                binding["financial_admission_digest"],
                payload_digest(binding["financial_admission"]),
            )
            self.assertEqual(binding["projected_fill"]["position_side"], None)
            self.assertEqual(binding["projected_fill"]["position_effect"], None)
            self.assertEqual(binding["provider_fill"]["side"], "BUY")
            self.assertEqual(binding["provider_fill"]["position_side"], None)
            self.assertEqual(binding["provider_fill"]["position_effect"], None)
            self.assertEqual(binding["provider_fill"]["quantity"], "1")

    def test_provider_evidence_retargeting_conflicts_with_existing_fill_binding(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            reservations = reservation_book(store)
            economics = economic_book(store)
            reserve(reservations)

            self.commit_evidenced_fill(economics, reservations)
            changed_provider = self.provider_fill(
                evidence_refs=("provider-fill:retargeted",),
            )

            with self.assertRaisesRegex(
                AccountingConflict,
                "different financial binding",
            ):
                self.commit_evidenced_fill(
                    economics,
                    reservations,
                    provider=changed_provider,
                    command_id="evidence-fill-command-retarget",
                    idempotency_key="evidence-fill-idempotency-retarget",
                )

            self.assertEqual(len(economics.transactions), 1)
            self.assertEqual(
                len(
                    store.load_events_by_aggregate_type(
                        "provider_fill_financial_binding"
                    )
                ),
                1,
            )
            snapshot = reservations.get("reservation-1")
            self.assertEqual(snapshot.consumed["CASH:USD"], Decimal("100"))
            self.assertEqual(snapshot.remaining["CASH:USD"], Decimal("20"))

    def test_equivalent_decimal_exponents_share_reservation_cut_identity(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            reservations = reservation_book(store)
            economics = economic_book(store)
            reserve(reservations)

            durable_snapshot = reservations.get("reservation-1")
            equivalent_snapshot = replace(
                durable_snapshot,
                original={"CASH:USD": Decimal("120.0")},
                remaining={"CASH:USD": Decimal("120.00")},
                consumed={"CASH:USD": Decimal("0.000")},
            )
            projected = self.projected_fill(
                quantity="0.5",
                fill_id="fill-equivalent-cut",
                provider_execution_id="provider-execution-equivalent-cut",
            )
            provider = self.provider_fill(
                quantity="0.5",
                provider_execution_id="provider-execution-equivalent-cut",
            )
            plan = build_provider_fill_financial_plan(
                book=economics,
                provider_id=PROVIDER,
                projected_fill=projected,
                provider_fill=provider,
                expected_instrument="ABC",
                settlement_currency="USD",
                reservation_snapshot=equivalent_snapshot,
                observed_at="2026-09-25T09:00:01Z",
            )

            prepared = reservations.prepare_consume_mutation(
                event_key="equivalent-cut-event",
                idempotency_key="equivalent-cut-idempotency",
                reservation_id="reservation-1",
                usage=plan.usage,
                committed_at="2026-09-25T09:00:02Z",
                expected_snapshot_digest=plan.reservation_cut_digest,
            )
            self.assertFalse(prepared.already_committed)
            self.assertEqual(
                prepared.snapshot.consumed["CASH:USD"],
                Decimal("50"),
            )
            self.assertEqual(
                prepared.snapshot.remaining["CASH:USD"],
                Decimal("70"),
            )

    def test_stale_financial_plan_is_fenced_before_atomic_mutation(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            reservations = reservation_book(store)
            economics = economic_book(store)
            reserve(reservations)

            projected = self.projected_fill(
                quantity="0.5",
                fill_id="fill-stale",
                provider_execution_id="provider-execution-stale",
            )
            provider = self.provider_fill(
                quantity="0.5",
                provider_execution_id="provider-execution-stale",
            )
            plan = build_provider_fill_financial_plan(
                book=economics,
                provider_id=PROVIDER,
                projected_fill=projected,
                provider_fill=provider,
                expected_instrument="ABC",
                settlement_currency="USD",
                reservation_snapshot=reservations.get("reservation-1"),
                observed_at="2026-09-25T09:00:01Z",
            )

            reservations.consume(
                command_id="competing-consume-command",
                idempotency_key="competing-consume-idempotency",
                reservation_id="reservation-1",
                usage={"CASH:USD": "1"},
            )
            with self.assertRaisesRegex(
                ReservationConflict,
                "snapshot changed after provider fill plan derivation",
            ):
                commit_economic_batch_with_reservation_consumption(
                    economics,
                    reservations,
                    command_id="stale-plan-command",
                    idempotency_key="stale-plan-idempotency",
                    reservation_id="reservation-1",
                    usage=plan.usage,
                    transactions=(plan.transaction,),
                    reservation_expected_snapshot_digest=plan.reservation_cut_digest,
                    committed_at="2026-09-25T09:00:02Z",
                )

            self.assertEqual(economics.transactions, ())
            snapshot = reservations.get("reservation-1")
            self.assertEqual(snapshot.consumed["CASH:USD"], Decimal("1"))
            self.assertEqual(snapshot.remaining["CASH:USD"], Decimal("119"))

    def test_binding_is_not_left_behind_when_atomic_commit_fails(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            reservations = reservation_book(store)
            economics = economic_book(store)
            reserve(reservations)

            original_commit = store.commit_command

            def fail_before_commit(**kwargs):
                raise RuntimeError("injected provider-fill binding failure")

            commit_patch = patch_journal_store_method(

                store, "commit_command", side_effect=fail_before_commit

            )

            commit_patch.start()
            try:
                with self.assertRaisesRegex(
                    RuntimeError, "provider-fill binding failure"
                ):
                    self.commit_evidenced_fill(economics, reservations)
            finally:
                commit_patch.stop()

            self.assertEqual(
                store.load_events_by_aggregate_type(
                    "provider_fill_financial_binding"
                ),
                [],
            )
            self.assertEqual(economics.transactions, ())
            snapshot = reservations.get("reservation-1")
            self.assertEqual(snapshot.consumed["CASH:USD"], Decimal("0"))
            self.assertEqual(snapshot.remaining["CASH:USD"], Decimal("120"))

    def test_two_partial_fills_accumulate_exact_reservation_consumption(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            reservations = reservation_book(store)
            economics = economic_book(store)
            reserve(reservations)

            first_projected = self.projected_fill(
                quantity="0.4",
                fill_id="fill-partial-1",
                provider_execution_id="provider-execution-partial-1",
            )
            first_provider = self.provider_fill(
                quantity="0.4",
                provider_execution_id="provider-execution-partial-1",
            )
            second_projected = self.projected_fill(
                quantity="0.6",
                fill_id="fill-partial-2",
                provider_execution_id="provider-execution-partial-2",
            )
            second_provider = self.provider_fill(
                quantity="0.6",
                provider_execution_id="provider-execution-partial-2",
            )

            self.assertTrue(
                self.commit_evidenced_fill(
                    economics,
                    reservations,
                    projected=first_projected,
                    provider=first_provider,
                    command_id="partial-command-1",
                    idempotency_key="partial-idempotency-1",
                )
            )
            self.assertTrue(
                self.commit_evidenced_fill(
                    economics,
                    reservations,
                    projected=second_projected,
                    provider=second_provider,
                    command_id="partial-command-2",
                    idempotency_key="partial-idempotency-2",
                )
            )

            snapshot = reservations.get("reservation-1")
            self.assertEqual(snapshot.consumed["CASH:USD"], Decimal("100.0"))
            self.assertEqual(snapshot.remaining["CASH:USD"], Decimal("20.0"))
            self.assertEqual(economics.position("ABC"), Decimal("1.0"))
            self.assertEqual(len(economics.transactions), 2)
            bindings = store.load_events_by_aggregate_type(
                "provider_fill_financial_binding"
            )
            self.assertEqual(len(bindings), 2)
            self.assertEqual(
                {
                    event["payload"]["request"]["provider_execution_id"]
                    for event in bindings
                },
                {
                    "provider-execution-partial-1",
                    "provider-execution-partial-2",
                },
            )
            self.assertEqual(
                {
                    event["payload"]["request"]["derived_usage"]["CASH:USD"]
                    for event in bindings
                },
                {"40", "60"},
            )
            self.assertEqual(
                len({event["aggregate_id"] for event in bindings}),
                2,
            )

    def test_fill_cannot_consume_beyond_admitted_reservation(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            reservations = reservation_book(store)
            economics = economic_book(store)
            reserve(reservations)

            with self.assertRaisesRegex(
                AccountingConflict,
                "exceeds admitted reservation",
            ):
                self.commit_evidenced_fill(
                    economics,
                    reservations,
                    projected=self.projected_fill(quantity="2"),
                    provider=self.provider_fill(quantity="2"),
                )

            self.assertEqual(economics.transactions, ())
            snapshot = reservations.get("reservation-1")
            self.assertEqual(snapshot.consumed["CASH:USD"], Decimal("0"))
            self.assertEqual(snapshot.remaining["CASH:USD"], Decimal("120"))

    def test_positive_settlement_fee_consumes_same_reserved_cash(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            reservations = reservation_book(store)
            economics = economic_book(store)
            reserve(reservations)

            self.assertTrue(
                self.commit_evidenced_fill(
                    economics,
                    reservations,
                    provider=self.provider_fill(fee_amount="1"),
                )
            )
            snapshot = reservations.get("reservation-1")
            self.assertEqual(snapshot.consumed["CASH:USD"], Decimal("101"))
            self.assertEqual(snapshot.remaining["CASH:USD"], Decimal("19"))

    def test_third_currency_fee_requires_admitted_resource(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            reservations = reservation_book(store)
            economics = economic_book(store)
            reserve(reservations)

            with self.assertRaisesRegex(
                AccountingConflict,
                "unreserved resource CASH:EUR",
            ):
                self.commit_evidenced_fill(
                    economics,
                    reservations,
                    provider=self.provider_fill(
                        fee_amount="1",
                        fee_currency="EUR",
                    ),
                )
            self.assertEqual(economics.transactions, ())
            self.assertEqual(
                reservations.get("reservation-1").consumed["CASH:USD"],
                Decimal("0"),
            )

    def test_negative_fee_never_releases_or_creates_authority(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            reservations = reservation_book(store)
            economics = economic_book(store)
            reserve(reservations)

            self.assertTrue(
                self.commit_evidenced_fill(
                    economics,
                    reservations,
                    provider=self.provider_fill(fee_amount="-2"),
                )
            )
            snapshot = reservations.get("reservation-1")
            self.assertEqual(snapshot.consumed["CASH:USD"], Decimal("100"))
            self.assertEqual(snapshot.remaining["CASH:USD"], Decimal("20"))

    def test_provider_side_mismatch_fails_before_any_mutation(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            reservations = reservation_book(store)
            economics = economic_book(store)
            reserve(reservations)

            with self.assertRaisesRegex(
                AccountingConflict,
                "side does not match projection",
            ):
                self.commit_evidenced_fill(
                    economics,
                    reservations,
                    provider=self.provider_fill(side="SELL"),
                )
            self.assertEqual(economics.transactions, ())
            self.assertEqual(
                reservations.get("reservation-1").consumed["CASH:USD"],
                Decimal("0"),
            )

    def test_unsupported_sell_and_derivative_mapping_fail_closed(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            reservations = reservation_book(store)
            economics = economic_book(store)
            reserve(reservations)

            with self.assertRaisesRegex(
                AccountingConflict,
                "qualified only for BUY",
            ):
                self.commit_evidenced_fill(
                    economics,
                    reservations,
                    projected=self.projected_fill(side="SELL"),
                    provider=self.provider_fill(side="SELL"),
                )
            with self.assertRaisesRegex(
                AccountingConflict,
                "not qualified for this asset family",
            ):
                self.commit_evidenced_fill(
                    economics,
                    reservations,
                    asset_family="FUTURES",
                )
            self.assertEqual(economics.transactions, ())

    def test_cash_equity_position_effect_is_rejected_before_initial_mutation(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            reservations = reservation_book(store)
            economics = economic_book(store)
            reserve(reservations)

            with self.assertRaisesRegex(
                AccountingConflict,
                "derivative position identity",
            ):
                self.commit_evidenced_fill(
                    economics,
                    reservations,
                    projected=self.projected_fill(position_effect="OPEN"),
                    provider=self.provider_fill(position_effect="OPEN"),
                )

            snapshot = reservations.get("reservation-1")
            self.assertEqual(snapshot.consumed["CASH:USD"], Decimal("0"))
            self.assertEqual(snapshot.remaining["CASH:USD"], Decimal("120"))
            self.assertEqual(economics.transactions, ())
            self.assertEqual(
                store.load_events_by_aggregate_type(
                    "provider_fill_financial_binding"
                ),
                [],
            )

    def test_cash_equity_position_effect_is_rejected_before_correction_mutation(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            reservations = reservation_book(store)
            economics = economic_book(store)
            settlements = settlement_book(store)
            reserve(reservations)

            inserted, original_projected, original_provider = (
                self.commit_initial_fill_with_settlement(
                    economics,
                    reservations,
                    settlements,
                )
            )
            self.assertTrue(inserted)
            before_snapshot = reservations.get("reservation-1")
            before_transactions = economics.transactions
            before_settlements = settlements.obligations

            corrected_projected = self.projected_fill(
                fill_id="fill-position-effect-correction",
                provider_revision="provider-revision-position-effect",
                correction_of=original_projected.fill_id,
                position_effect="OPEN",
            )
            corrected_provider = self.provider_fill(position_effect="OPEN")

            with self.assertRaisesRegex(
                AccountingConflict,
                "derivative position identity",
            ):
                commit_provider_fill_correction_with_settlement_replacement(
                    economics,
                    settlements,
                    reservation_book=reservations,
                    reservation_id="reservation-1",
                    command_id="position-effect-correction-command",
                    idempotency_key="position-effect-correction-idempotency",
                    original_projected_fill=original_projected,
                    original_provider_fill=original_provider,
                    corrected_projected_fill=corrected_projected,
                    corrected_provider_fill=corrected_provider,
                    expected_instrument="ABC",
                    settlement_currency="USD",
                    correction_observed_at="2026-09-25T10:00:01Z",
                    settlement_obligations=(),
                    committed_at="2026-09-25T10:00:02Z",
                )

            self.assertEqual(reservations.get("reservation-1"), before_snapshot)
            self.assertEqual(economics.transactions, before_transactions)
            self.assertEqual(settlements.obligations, before_settlements)
            self.assertEqual(
                store.load_events_by_aggregate_type(
                    "provider_fill_reservation_correction_binding"
                ),
                [],
            )

    def test_same_caller_idempotency_rejects_changed_fill_plan(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            reservations = reservation_book(store)
            economics = economic_book(store)
            reserve(reservations)

            first_projected = self.projected_fill(
                quantity="0.4",
                fill_id="fill-idempotency-1",
                provider_execution_id="provider-execution-idempotency-1",
            )
            first_provider = self.provider_fill(
                quantity="0.4",
                provider_execution_id="provider-execution-idempotency-1",
            )
            self.assertTrue(
                self.commit_evidenced_fill(
                    economics,
                    reservations,
                    projected=first_projected,
                    provider=first_provider,
                    command_id="idempotency-command-1",
                    idempotency_key="shared-fill-idempotency",
                )
            )

            changed_projected = self.projected_fill(
                quantity="0.5",
                fill_id="fill-idempotency-2",
                provider_execution_id="provider-execution-idempotency-2",
            )
            changed_provider = self.provider_fill(
                quantity="0.5",
                provider_execution_id="provider-execution-idempotency-2",
            )
            with self.assertRaisesRegex(
                ReservationConflict,
                "idempotency_key was already used for a different reservation request",
            ):
                self.commit_evidenced_fill(
                    economics,
                    reservations,
                    projected=changed_projected,
                    provider=changed_provider,
                    command_id="idempotency-command-2",
                    idempotency_key="shared-fill-idempotency",
                )

            snapshot = reservations.get("reservation-1")
            self.assertEqual(snapshot.consumed["CASH:USD"], Decimal("40.0"))
            self.assertEqual(snapshot.remaining["CASH:USD"], Decimal("80.0"))
            self.assertEqual(economics.position("ABC"), Decimal("0.4"))
            self.assertEqual(len(economics.transactions), 1)

    def test_exact_retry_after_restart_is_idempotent_across_both_aggregates(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            reservations = reservation_book(store)
            economics = economic_book(store)
            reserve(reservations)

            self.assertTrue(self.commit_evidenced_fill(economics, reservations))

            reopened = JournalStore(path)
            reopened_reservations = reservation_book(reopened)
            reopened_economics = economic_book(reopened)
            self.assertFalse(
                self.commit_evidenced_fill(
                    reopened_economics,
                    reopened_reservations,
                )
            )
            snapshot = reopened_reservations.get("reservation-1")
            self.assertEqual(snapshot.consumed["CASH:USD"], Decimal("100"))
            self.assertEqual(snapshot.remaining["CASH:USD"], Decimal("20"))
            self.assertEqual(len(reopened_economics.transactions), 1)
            bindings = reopened.load_events_by_aggregate_type(
                "provider_fill_financial_binding"
            )
            self.assertEqual(len(bindings), 1)
            self.assertEqual(
                bindings[0]["payload"]["request"]["provider_execution_id"],
                "provider-execution-1",
            )


    def commit_initial_fill_with_settlement(
        self,
        economics,
        reservations,
        settlements,
        *,
        projected=None,
        provider=None,
    ):
        projected_fill = self.projected_fill() if projected is None else projected
        provider_fill = self.provider_fill() if provider is None else provider
        plan = build_provider_fill_financial_plan(
            book=economics,
            provider_id=PROVIDER,
            projected_fill=projected_fill,
            provider_fill=provider_fill,
            expected_instrument="ABC",
            settlement_currency="USD",
            reservation_snapshot=reservations.get("reservation-1"),
            observed_at="2026-09-25T09:00:01Z",
        )
        obligation = settlement_obligation(
            settlements.store,
            plan.transaction,
            obligation_id="settlement-initial-fill",
        )
        inserted = commit_provider_fill_with_reservation_consumption(
            economics,
            reservations,
            command_id="initial-settled-fill-command",
            idempotency_key="initial-settled-fill-idempotency",
            reservation_id="reservation-1",
            admission_id="admission-1",
            projected_fill=projected_fill,
            provider_fill=provider_fill,
            expected_instrument="ABC",
            settlement_currency="USD",
            observed_at="2026-09-25T09:00:01Z",
            committed_at="2026-09-25T09:00:02Z",
            settlement_book=settlements,
            settlement_obligations=(obligation,),
        )
        return inserted, projected_fill, provider_fill

    def correction_obligation(
        self,
        economics,
        settlements,
        *,
        original_projected,
        original_provider,
        corrected_projected,
        corrected_provider,
        correction_observed_at,
        obligation_id,
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
            correction_observed_at=correction_observed_at,
        )
        return settlement_obligation(
            settlements.store,
            replacement,
            obligation_id=obligation_id,
        )

    def test_generic_correction_cannot_bypass_provider_fill_reservation_authority(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            reservations = reservation_book(store)
            economics = economic_book(store)
            settlements = settlement_book(store)
            reserve(reservations)

            inserted, original_projected, original_provider = (
                self.commit_initial_fill_with_settlement(
                    economics,
                    reservations,
                    settlements,
                )
            )
            self.assertTrue(inserted)
            before_reservation = reservations.get("reservation-1")
            before_transactions = economics.transactions
            before_settlements = settlements.obligations
            self.assertEqual(before_reservation.consumed["CASH:USD"], Decimal("100"))

            corrected_projected = self.projected_fill(
                quantity="1.1",
                fill_id="fill-generic-correction-bypass",
                provider_revision="provider-revision-generic-bypass",
                correction_of=original_projected.fill_id,
            )
            corrected_provider = self.provider_fill(quantity="1.1")
            reversal, replacement = build_provider_fill_correction_transactions(
                book=economics,
                provider_id=PROVIDER,
                original_projected_fill=original_projected,
                original_provider_fill=original_provider,
                corrected_projected_fill=corrected_projected,
                corrected_provider_fill=corrected_provider,
                expected_instrument="ABC",
                settlement_currency="USD",
                correction_observed_at="2026-09-25T10:30:01Z",
            )
            obligation = settlement_obligation(
                store,
                replacement,
                obligation_id="settlement-generic-correction-bypass",
            )

            with self.assertRaisesRegex(
                AccountingConflict,
                "provider-fill-owned correction requires reservation-aware correction authority",
            ):
                commit_economic_correction_with_settlement_replacement(
                    economics,
                    settlements,
                    command_id="generic-correction-bypass-command",
                    idempotency_key="generic-correction-bypass-idempotency",
                    reversal=reversal,
                    replacement=replacement,
                    settlement_obligations=(obligation,),
                    committed_at="2026-09-25T10:30:02Z",
                )

            self.assertEqual(reservations.get("reservation-1"), before_reservation)
            self.assertEqual(economics.transactions, before_transactions)
            self.assertEqual(settlements.obligations, before_settlements)
            self.assertEqual(
                store.load_events_by_aggregate_type(
                    "provider_fill_reservation_correction_binding"
                ),
                [],
            )

            kwargs = dict(
                reservation_id="reservation-1",
                command_id="reservation-aware-correction-command",
                idempotency_key="reservation-aware-correction-idempotency",
                original_projected_fill=original_projected,
                original_provider_fill=original_provider,
                corrected_projected_fill=corrected_projected,
                corrected_provider_fill=corrected_provider,
                expected_instrument="ABC",
                settlement_currency="USD",
                correction_observed_at="2026-09-25T10:30:01Z",
                settlement_obligations=(obligation,),
                committed_at="2026-09-25T10:30:02Z",
            )
            self.assertTrue(
                commit_provider_fill_correction_with_settlement_replacement(
                    economics,
                    settlements,
                    reservation_book=reservations,
                    **kwargs,
                )
            )
            self.assertEqual(
                reservations.get("reservation-1").consumed["CASH:USD"],
                Decimal("110"),
            )
            self.assertEqual(
                len(
                    store.load_events_by_aggregate_type(
                        "provider_fill_reservation_correction_binding"
                    )
                ),
                1,
            )

            self.assertFalse(
                commit_provider_fill_correction_with_settlement_replacement(
                    economics,
                    settlements,
                    reservation_book=reservations,
                    **kwargs,
                )
            )
            self.assertEqual(
                reservations.get("reservation-1").consumed["CASH:USD"],
                Decimal("110"),
            )
            self.assertEqual(len(economics.transactions), 3)

    def test_correction_decrease_then_increase_consumes_only_high_water_delta(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            reservations = reservation_book(store)
            economics = economic_book(store)
            settlements = settlement_book(store)
            reserve(reservations)

            inserted, original_projected, original_provider = (
                self.commit_initial_fill_with_settlement(
                    economics,
                    reservations,
                    settlements,
                )
            )
            self.assertTrue(inserted)
            self.assertEqual(
                reservations.get("reservation-1").consumed["CASH:USD"],
                Decimal("100"),
            )

            down_projected = self.projected_fill(
                quantity="0.9",
                fill_id="fill-correction-down",
                provider_revision="provider-revision-2",
                correction_of=original_projected.fill_id,
            )
            down_provider = self.provider_fill(quantity="0.9")
            down_obligation = self.correction_obligation(
                economics,
                settlements,
                original_projected=original_projected,
                original_provider=original_provider,
                corrected_projected=down_projected,
                corrected_provider=down_provider,
                correction_observed_at="2026-09-25T10:00:01Z",
                obligation_id="settlement-correction-down",
            )
            self.assertTrue(
                commit_provider_fill_correction_with_settlement_replacement(
                    economics,
                    settlements,
                    reservation_book=reservations,
                    reservation_id="reservation-1",
                    command_id="correction-down-command",
                    idempotency_key="correction-down-idempotency",
                    original_projected_fill=original_projected,
                    original_provider_fill=original_provider,
                    corrected_projected_fill=down_projected,
                    corrected_provider_fill=down_provider,
                    expected_instrument="ABC",
                    settlement_currency="USD",
                    correction_observed_at="2026-09-25T10:00:01Z",
                    settlement_obligations=(down_obligation,),
                    committed_at="2026-09-25T10:00:02Z",
                )
            )
            after_down = reservations.get("reservation-1")
            self.assertEqual(after_down.consumed["CASH:USD"], Decimal("100"))
            self.assertEqual(after_down.remaining["CASH:USD"], Decimal("20"))

            up_projected = self.projected_fill(
                quantity="1.1",
                fill_id="fill-correction-up",
                provider_revision="provider-revision-3",
                correction_of=down_projected.fill_id,
            )
            up_provider = self.provider_fill(quantity="1.1")
            up_obligation = self.correction_obligation(
                economics,
                settlements,
                original_projected=down_projected,
                original_provider=down_provider,
                corrected_projected=up_projected,
                corrected_provider=up_provider,
                correction_observed_at="2026-09-25T11:00:01Z",
                obligation_id="settlement-correction-up",
            )
            self.assertTrue(
                commit_provider_fill_correction_with_settlement_replacement(
                    economics,
                    settlements,
                    reservation_book=reservations,
                    reservation_id="reservation-1",
                    command_id="correction-up-command",
                    idempotency_key="correction-up-idempotency",
                    original_projected_fill=down_projected,
                    original_provider_fill=down_provider,
                    corrected_projected_fill=up_projected,
                    corrected_provider_fill=up_provider,
                    expected_instrument="ABC",
                    settlement_currency="USD",
                    correction_observed_at="2026-09-25T11:00:01Z",
                    settlement_obligations=(up_obligation,),
                    committed_at="2026-09-25T11:00:02Z",
                )
            )
            after_up = reservations.get("reservation-1")
            self.assertEqual(after_up.consumed["CASH:USD"], Decimal("110"))
            self.assertEqual(after_up.remaining["CASH:USD"], Decimal("10"))

            bindings = store.load_events_by_aggregate_type(
                "provider_fill_reservation_correction_binding"
            )
            self.assertEqual(len(bindings), 2)
            self.assertEqual(
                bindings[0]["payload"]["request"]["additional_usage"],
                {},
            )
            self.assertEqual(
                bindings[1]["payload"]["request"]["additional_usage"],
                {"CASH:USD": "10"},
            )
            self.assertEqual(
                bindings[1]["payload"]["request"]["resulting_conservative_usage"],
                {"CASH:USD": "110"},
            )

    def test_increasing_correction_restart_retry_is_exactly_once(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            reservations = reservation_book(store)
            economics = economic_book(store)
            settlements = settlement_book(store)
            reserve(reservations)
            _, original_projected, original_provider = (
                self.commit_initial_fill_with_settlement(
                    economics,
                    reservations,
                    settlements,
                )
            )

            corrected_projected = self.projected_fill(
                quantity="1.1",
                fill_id="fill-correction-retry",
                provider_revision="provider-revision-retry",
                correction_of=original_projected.fill_id,
            )
            corrected_provider = self.provider_fill(quantity="1.1")
            obligation = self.correction_obligation(
                economics,
                settlements,
                original_projected=original_projected,
                original_provider=original_provider,
                corrected_projected=corrected_projected,
                corrected_provider=corrected_provider,
                correction_observed_at="2026-09-25T12:00:01Z",
                obligation_id="settlement-correction-retry",
            )
            kwargs = dict(
                reservation_id="reservation-1",
                command_id="correction-retry-command",
                idempotency_key="correction-retry-idempotency",
                original_projected_fill=original_projected,
                original_provider_fill=original_provider,
                corrected_projected_fill=corrected_projected,
                corrected_provider_fill=corrected_provider,
                expected_instrument="ABC",
                settlement_currency="USD",
                correction_observed_at="2026-09-25T12:00:01Z",
                settlement_obligations=(obligation,),
                committed_at="2026-09-25T12:00:02Z",
            )
            self.assertTrue(
                commit_provider_fill_correction_with_settlement_replacement(
                    economics,
                    settlements,
                    reservation_book=reservations,
                    **kwargs,
                )
            )

            reopened_store = JournalStore(path)
            reopened_reservations = reservation_book(reopened_store)
            reopened_economics = economic_book(reopened_store)
            reopened_settlements = settlement_book(reopened_store)
            self.assertFalse(
                commit_provider_fill_correction_with_settlement_replacement(
                    reopened_economics,
                    reopened_settlements,
                    reservation_book=reopened_reservations,
                    **kwargs,
                )
            )
            snapshot = reopened_reservations.get("reservation-1")
            self.assertEqual(snapshot.consumed["CASH:USD"], Decimal("110"))
            self.assertEqual(snapshot.remaining["CASH:USD"], Decimal("10"))
            self.assertEqual(
                len(
                    reopened_store.load_events_by_aggregate_type(
                        "provider_fill_reservation_correction_binding"
                    )
                ),
                1,
            )
            self.assertEqual(len(reopened_economics.transactions), 3)

    def test_correction_positive_fee_consumes_exact_additional_cash(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            reservations = reservation_book(store)
            economics = economic_book(store)
            settlements = settlement_book(store)
            reserve(reservations)
            _, original_projected, original_provider = (
                self.commit_initial_fill_with_settlement(
                    economics,
                    reservations,
                    settlements,
                )
            )

            corrected_projected = self.projected_fill(
                fill_id="fill-correction-fee",
                provider_revision="provider-revision-fee",
                correction_of=original_projected.fill_id,
            )
            corrected_provider = self.provider_fill(fee_amount="1")
            obligation = self.correction_obligation(
                economics,
                settlements,
                original_projected=original_projected,
                original_provider=original_provider,
                corrected_projected=corrected_projected,
                corrected_provider=corrected_provider,
                correction_observed_at="2026-09-25T12:30:01Z",
                obligation_id="settlement-correction-fee",
            )
            self.assertTrue(
                commit_provider_fill_correction_with_settlement_replacement(
                    economics,
                    settlements,
                    reservation_book=reservations,
                    reservation_id="reservation-1",
                    command_id="correction-fee-command",
                    idempotency_key="correction-fee-idempotency",
                    original_projected_fill=original_projected,
                    original_provider_fill=original_provider,
                    corrected_projected_fill=corrected_projected,
                    corrected_provider_fill=corrected_provider,
                    expected_instrument="ABC",
                    settlement_currency="USD",
                    correction_observed_at="2026-09-25T12:30:01Z",
                    settlement_obligations=(obligation,),
                    committed_at="2026-09-25T12:30:02Z",
                )
            )
            snapshot = reservations.get("reservation-1")
            self.assertEqual(snapshot.consumed["CASH:USD"], Decimal("101"))
            self.assertEqual(snapshot.remaining["CASH:USD"], Decimal("19"))
            binding = store.load_events_by_aggregate_type(
                "provider_fill_reservation_correction_binding"
            )[0]
            self.assertEqual(
                binding["payload"]["request"]["additional_usage"],
                {"CASH:USD": "1"},
            )

    def test_correction_third_currency_fee_requires_admitted_resource_before_mutation(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            reservations = reservation_book(store)
            economics = economic_book(store)
            settlements = settlement_book(store)
            reserve(reservations)
            _, original_projected, original_provider = (
                self.commit_initial_fill_with_settlement(
                    economics,
                    reservations,
                    settlements,
                )
            )

            corrected_projected = self.projected_fill(
                fill_id="fill-correction-eur-fee",
                provider_revision="provider-revision-eur-fee",
                correction_of=original_projected.fill_id,
            )
            corrected_provider = self.provider_fill(
                fee_amount="1",
                fee_currency="EUR",
            )
            with self.assertRaisesRegex(
                AccountingConflict,
                "unreserved resource CASH:EUR",
            ):
                commit_provider_fill_correction_with_settlement_replacement(
                    economics,
                    settlements,
                    reservation_book=reservations,
                    reservation_id="reservation-1",
                    command_id="correction-eur-fee-command",
                    idempotency_key="correction-eur-fee-idempotency",
                    original_projected_fill=original_projected,
                    original_provider_fill=original_provider,
                    corrected_projected_fill=corrected_projected,
                    corrected_provider_fill=corrected_provider,
                    expected_instrument="ABC",
                    settlement_currency="USD",
                    correction_observed_at="2026-09-25T12:45:01Z",
                    settlement_obligations=(),
                    committed_at="2026-09-25T12:45:02Z",
                )

            snapshot = reservations.get("reservation-1")
            self.assertEqual(snapshot.consumed["CASH:USD"], Decimal("100"))
            self.assertEqual(snapshot.remaining["CASH:USD"], Decimal("20"))
            self.assertEqual(len(economics.transactions), 1)
            self.assertEqual(
                store.load_events_by_aggregate_type(
                    "provider_fill_reservation_correction_binding"
                ),
                [],
            )

    def test_correction_additional_usage_cannot_exceed_current_remaining_capacity(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            reservations = reservation_book(store)
            economics = economic_book(store)
            settlements = settlement_book(store)
            reserve(reservations)
            _, original_projected, original_provider = (
                self.commit_initial_fill_with_settlement(
                    economics,
                    reservations,
                    settlements,
                )
            )
            reservations.consume(
                command_id="competing-capacity-command",
                idempotency_key="competing-capacity-idempotency",
                reservation_id="reservation-1",
                usage={"CASH:USD": "15"},
            )
            before = reservations.get("reservation-1")
            self.assertEqual(before.consumed["CASH:USD"], Decimal("115"))
            self.assertEqual(before.remaining["CASH:USD"], Decimal("5"))

            corrected_projected = self.projected_fill(
                quantity="1.1",
                fill_id="fill-correction-insufficient-remaining",
                provider_revision="provider-revision-insufficient-remaining",
                correction_of=original_projected.fill_id,
            )
            corrected_provider = self.provider_fill(quantity="1.1")
            obligation = self.correction_obligation(
                economics,
                settlements,
                original_projected=original_projected,
                original_provider=original_provider,
                corrected_projected=corrected_projected,
                corrected_provider=corrected_provider,
                correction_observed_at="2026-09-25T12:50:01Z",
                obligation_id="settlement-correction-insufficient-remaining",
            )

            with self.assertRaisesRegex(
                ReservationConflict,
                "Consumption exceeds remaining reservation",
            ):
                commit_provider_fill_correction_with_settlement_replacement(
                    economics,
                    settlements,
                    reservation_book=reservations,
                    reservation_id="reservation-1",
                    command_id="correction-insufficient-remaining-command",
                    idempotency_key="correction-insufficient-remaining-idempotency",
                    original_projected_fill=original_projected,
                    original_provider_fill=original_provider,
                    corrected_projected_fill=corrected_projected,
                    corrected_provider_fill=corrected_provider,
                    expected_instrument="ABC",
                    settlement_currency="USD",
                    correction_observed_at="2026-09-25T12:50:01Z",
                    settlement_obligations=(obligation,),
                    committed_at="2026-09-25T12:50:02Z",
                )

            after = reservations.get("reservation-1")
            self.assertEqual(after.consumed["CASH:USD"], Decimal("115"))
            self.assertEqual(after.remaining["CASH:USD"], Decimal("5"))
            self.assertEqual(len(economics.transactions), 1)
            self.assertEqual(
                store.load_events_by_aggregate_type(
                    "provider_fill_reservation_correction_binding"
                ),
                [],
            )

    def test_intervening_reservation_mutation_invalidates_correction_cut_atomically(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            reservations = reservation_book(store)
            economics = economic_book(store)
            settlements = settlement_book(store)
            reserve(reservations)
            _, original_projected, original_provider = (
                self.commit_initial_fill_with_settlement(
                    economics,
                    reservations,
                    settlements,
                )
            )

            corrected_projected = self.projected_fill(
                quantity="1.1",
                fill_id="fill-correction-stale-cut",
                provider_revision="provider-revision-stale-cut",
                correction_of=original_projected.fill_id,
            )
            corrected_provider = self.provider_fill(quantity="1.1")
            obligation = self.correction_obligation(
                economics,
                settlements,
                original_projected=original_projected,
                original_provider=original_provider,
                corrected_projected=corrected_projected,
                corrected_provider=corrected_provider,
                correction_observed_at="2026-09-25T12:55:01Z",
                obligation_id="settlement-correction-stale-cut",
            )

            original_prepare = DurableProviderEconomicBook.prepare_batch_mutation
            mutated = False

            def mutate_reservation_then_prepare(instance, *args, **kwargs):
                nonlocal mutated
                if instance is economics and not mutated:
                    mutated = True
                    reservations.consume(
                        command_id="intervening-reservation-command",
                        idempotency_key="intervening-reservation-idempotency",
                        reservation_id="reservation-1",
                        usage={"CASH:USD": "1"},
                    )
                return original_prepare(instance, *args, **kwargs)

            with patch.object(
                DurableProviderEconomicBook,
                "prepare_batch_mutation",
                new=mutate_reservation_then_prepare,
            ):
                with self.assertRaisesRegex(
                    ReservationConflict,
                    "snapshot changed after provider fill plan derivation",
                ):
                    commit_provider_fill_correction_with_settlement_replacement(
                        economics,
                        settlements,
                        reservation_book=reservations,
                        reservation_id="reservation-1",
                        command_id="correction-stale-cut-command",
                        idempotency_key="correction-stale-cut-idempotency",
                        original_projected_fill=original_projected,
                        original_provider_fill=original_provider,
                        corrected_projected_fill=corrected_projected,
                        corrected_provider_fill=corrected_provider,
                        expected_instrument="ABC",
                        settlement_currency="USD",
                        correction_observed_at="2026-09-25T12:55:01Z",
                        settlement_obligations=(obligation,),
                        committed_at="2026-09-25T12:55:02Z",
                    )

            snapshot = reservations.get("reservation-1")
            self.assertEqual(snapshot.consumed["CASH:USD"], Decimal("101"))
            self.assertEqual(snapshot.remaining["CASH:USD"], Decimal("19"))
            self.assertEqual(len(economics.transactions), 1)
            self.assertEqual(
                store.load_events_by_aggregate_type(
                    "provider_fill_reservation_correction_binding"
                ),
                [],
            )
            settlement_events = store.load_events_by_aggregate_type(
                "settlement_book"
            )
            self.assertEqual(len(settlement_events), 1)

    def test_next_correction_cannot_retarget_prior_provider_evidence_lineage(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            reservations = reservation_book(store)
            economics = economic_book(store)
            settlements = settlement_book(store)
            reserve(reservations)
            _, original_projected, original_provider = (
                self.commit_initial_fill_with_settlement(
                    economics,
                    reservations,
                    settlements,
                )
            )

            first_projected = self.projected_fill(
                quantity="0.9",
                fill_id="fill-correction-lineage-1",
                provider_revision="provider-revision-lineage-1",
                correction_of=original_projected.fill_id,
            )
            first_provider = self.provider_fill(
                quantity="0.9",
                evidence_refs=("provider-fill:lineage-1",),
            )
            first_obligation = self.correction_obligation(
                economics,
                settlements,
                original_projected=original_projected,
                original_provider=original_provider,
                corrected_projected=first_projected,
                corrected_provider=first_provider,
                correction_observed_at="2026-09-25T12:30:01Z",
                obligation_id="settlement-correction-lineage-1",
            )
            self.assertTrue(
                commit_provider_fill_correction_with_settlement_replacement(
                    economics,
                    settlements,
                    reservation_book=reservations,
                    reservation_id="reservation-1",
                    command_id="correction-lineage-1-command",
                    idempotency_key="correction-lineage-1-idempotency",
                    original_projected_fill=original_projected,
                    original_provider_fill=original_provider,
                    corrected_projected_fill=first_projected,
                    corrected_provider_fill=first_provider,
                    expected_instrument="ABC",
                    settlement_currency="USD",
                    correction_observed_at="2026-09-25T12:30:01Z",
                    settlement_obligations=(first_obligation,),
                    committed_at="2026-09-25T12:30:02Z",
                )
            )

            retargeted_prior_provider = self.provider_fill(
                quantity="0.9",
                evidence_refs=("provider-fill:retargeted-prior",),
            )
            second_projected = self.projected_fill(
                quantity="1.0",
                fill_id="fill-correction-lineage-2",
                provider_revision="provider-revision-lineage-2",
                correction_of=first_projected.fill_id,
            )
            second_provider = self.provider_fill(
                quantity="1.0",
                evidence_refs=("provider-fill:lineage-2",),
            )
            second_obligation = self.correction_obligation(
                economics,
                settlements,
                original_projected=first_projected,
                original_provider=retargeted_prior_provider,
                corrected_projected=second_projected,
                corrected_provider=second_provider,
                correction_observed_at="2026-09-25T12:40:01Z",
                obligation_id="settlement-correction-lineage-2",
            )

            with self.assertRaisesRegex(
                AccountingConflict,
                "original evidence does not match active lineage",
            ):
                commit_provider_fill_correction_with_settlement_replacement(
                    economics,
                    settlements,
                    reservation_book=reservations,
                    reservation_id="reservation-1",
                    command_id="correction-lineage-2-command",
                    idempotency_key="correction-lineage-2-idempotency",
                    original_projected_fill=first_projected,
                    original_provider_fill=retargeted_prior_provider,
                    corrected_projected_fill=second_projected,
                    corrected_provider_fill=second_provider,
                    expected_instrument="ABC",
                    settlement_currency="USD",
                    correction_observed_at="2026-09-25T12:40:01Z",
                    settlement_obligations=(second_obligation,),
                    committed_at="2026-09-25T12:40:02Z",
                )

            self.assertEqual(
                reservations.get("reservation-1").consumed["CASH:USD"],
                Decimal("100"),
            )
            self.assertEqual(
                len(
                    store.load_events_by_aggregate_type(
                        "provider_fill_reservation_correction_binding"
                    )
                ),
                1,
            )

    def test_correction_precommit_failure_leaves_all_financial_projections_unchanged(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            reservations = reservation_book(store)
            economics = economic_book(store)
            settlements = settlement_book(store)
            reserve(reservations)
            _, original_projected, original_provider = (
                self.commit_initial_fill_with_settlement(
                    economics,
                    reservations,
                    settlements,
                )
            )

            corrected_projected = self.projected_fill(
                quantity="1.1",
                fill_id="fill-correction-failure",
                provider_revision="provider-revision-failure",
                correction_of=original_projected.fill_id,
            )
            corrected_provider = self.provider_fill(quantity="1.1")
            obligation = self.correction_obligation(
                economics,
                settlements,
                original_projected=original_projected,
                original_provider=original_provider,
                corrected_projected=corrected_projected,
                corrected_provider=corrected_provider,
                correction_observed_at="2026-09-25T13:00:01Z",
                obligation_id="settlement-correction-failure",
            )

            original_commit = store.commit_command

            def fail_before_commit(**kwargs):
                raise RuntimeError("injected correction pre-commit failure")

            commit_patch = patch_journal_store_method(

                store, "commit_command", side_effect=fail_before_commit

            )

            commit_patch.start()
            try:
                with self.assertRaisesRegex(RuntimeError, "pre-commit failure"):
                    commit_provider_fill_correction_with_settlement_replacement(
                        economics,
                        settlements,
                        reservation_book=reservations,
                        reservation_id="reservation-1",
                        command_id="correction-failure-command",
                        idempotency_key="correction-failure-idempotency",
                        original_projected_fill=original_projected,
                        original_provider_fill=original_provider,
                        corrected_projected_fill=corrected_projected,
                        corrected_provider_fill=corrected_provider,
                        expected_instrument="ABC",
                        settlement_currency="USD",
                        correction_observed_at="2026-09-25T13:00:01Z",
                        settlement_obligations=(obligation,),
                        committed_at="2026-09-25T13:00:02Z",
                    )
            finally:
                commit_patch.stop()

            reopened = JournalStore(path)
            reopened_reservations = reservation_book(reopened)
            reopened_economics = economic_book(reopened)
            self.assertEqual(
                reopened_reservations.get("reservation-1").consumed["CASH:USD"],
                Decimal("100"),
            )
            self.assertEqual(len(reopened_economics.transactions), 1)
            self.assertEqual(
                reopened.load_events_by_aggregate_type(
                    "provider_fill_reservation_correction_binding"
                ),
                [],
            )



if __name__ == "__main__":
    unittest.main()
