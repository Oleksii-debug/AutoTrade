from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.accounting import AccountingConflict, JournalTransaction
from mvp.autotrade_mvp import _provider_activity_accounting_impl as accounting_impl
from mvp.autotrade_mvp.durable_order_projection import DurableOrderBookProjection
from mvp.autotrade_mvp.durable_reservations import DurableReservationBook
from mvp.autotrade_mvp.durable_settlement import DurableSettlementBook
from mvp.autotrade_mvp.fill_accounting import (
    ProjectedFillEvidence,
    ProviderFillFinancialPlan,
    build_provider_fill_bust_transaction,
)
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.provider_activity_accounting import (
    DurableProviderEconomicBook,
    commit_provider_fill_bust_with_economic_reversal,
    commit_provider_fill_with_reservation_consumption,
)
from mvp.autotrade_mvp.reconciliation import ProviderFillEvidence
from mvp.autotrade_mvp.reservations import (
    POST_BUST_HOLD_STATE,
    ReservationConflict,
)
from research.autotrade_research.artifacts.store import ArtifactStore


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
    reservations: DurableReservationBook,
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
        reservation_book=reservations,
        reservation_id="reservation-1",
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
            Decimal("0"),
        )
        self.assertEqual(
            reservations.get("reservation-1").remaining["CASH:USD"],
            Decimal("120"),
        )
        self.assertEqual(
            reservations.total_reserved("CASH:USD"),
            Decimal("120"),
        )

    def test_reservation_bound_bust_cannot_omit_reservation_authority(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            orders, economics, reservations = books(store)
            projected, provider = seed(orders, economics, reservations)

            with self.assertRaisesRegex(
                AccountingConflict,
                "reservation-bound fill bust requires reservation authority",
            ):
                commit_provider_fill_bust_with_economic_reversal(
                    economics,
                    orders,
                    command_id="provider-bust-without-reservation",
                    idempotency_key="provider-bust-without-reservation",
                    projected_fill=projected,
                    provider_fill=provider,
                    expected_instrument="ABC",
                    settlement_currency="USD",
                    bust_provider_revision=BUST_REVISION,
                    bust_observed_at=WHEN,
                    order_event_key="bust-without-reservation",
                    committed_at=WHEN,
                )

            self.assertEqual(
                orders.order("order-1").snapshot().filled_quantity,
                Decimal("1"),
            )
            self.assertEqual(economics.position("ABC"), Decimal("1"))
            reservation = reservations.get("reservation-1")
            self.assertEqual(
                reservation.consumed["CASH:USD"],
                Decimal("100"),
            )
            self.assertEqual(
                reservation.remaining["CASH:USD"],
                Decimal("0"),
            )

    def test_late_bust_after_terminal_filled_reconstitutes_hold_atomically(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            orders, economics, reservations = books(store)
            projected, provider = seed(orders, economics, reservations)

            # The atomic final fill already wrote the durable FILLED cut.
            terminal = reservations.get("reservation-1")
            self.assertEqual(terminal.state, "FILLED")
            self.assertEqual(
                terminal.consumed["CASH:USD"], Decimal("100")
            )
            self.assertEqual(
                terminal.remaining["CASH:USD"], Decimal("0")
            )

            self.assertTrue(
                atomic_bust(
                    orders,
                    economics,
                    reservations,
                    projected,
                    provider,
                )
            )

            reopened = JournalStore(path)
            ro, re, rr = books(reopened)
            order = ro.order("order-1").snapshot()
            self.assertEqual(order.filled_quantity, Decimal("0"))
            self.assertEqual(order.fill_count, 0)
            self.assertEqual(order.observation_count, 2)
            self.assertEqual(re.position("ABC"), Decimal("0"))
            self.assertEqual(len(re.transactions), 2)
            reservation = rr.get("reservation-1")
            self.assertEqual(reservation.state, POST_BUST_HOLD_STATE)
            self.assertIsNone(reservation.resolution_evidence)
            self.assertEqual(
                reservation.consumed["CASH:USD"], Decimal("0")
            )
            # Terminalization had released the unused 20 buffer as well as
            # the 100 fill usage. The bust must restore the full original
            # worst-case hold, not merely add the busted usage to zero.
            self.assertEqual(
                reservation.remaining["CASH:USD"], Decimal("120")
            )
            self.assertEqual(
                rr.total_reserved("CASH:USD"), Decimal("120")
            )

            self.assertFalse(
                atomic_bust(ro, re, rr, projected, provider)
            )
            retry = rr.get("reservation-1")
            self.assertEqual(retry, reservation)
            self.assertEqual(
                rr.total_reserved("CASH:USD"), Decimal("120")
            )

    def test_provider_fill_facade_rejects_polymorphic_evidence_before_field_access(self):
        class HostileProjectedFill(ProjectedFillEvidence):
            field_reads = 0

            def __getattribute__(self, name):
                if name not in {"field_reads", "__class__"}:
                    type(self).field_reads += 1
                    raise AssertionError("hostile initial projected-fill field access")
                return super().__getattribute__(name)

        class HostileProviderFill(ProviderFillEvidence):
            field_reads = 0

            def __getattribute__(self, name):
                if name not in {"field_reads", "__class__"}:
                    type(self).field_reads += 1
                    raise AssertionError("hostile initial provider-fill field access")
                return super().__getattribute__(name)

        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            _orders, economics, reservations = books(store)
            projected, provider = evidence()

            with self.assertRaisesRegex(
                TypeError,
                "projected_fill must be exact ProjectedFillEvidence",
            ):
                commit_provider_fill_with_reservation_consumption(
                    economics,
                    reservations,
                    command_id="hostile-initial-projected",
                    idempotency_key="hostile-initial-projected",
                    reservation_id="reservation-never-read",
                    projected_fill=object.__new__(HostileProjectedFill),
                    provider_fill=provider,
                    expected_instrument="ABC",
                    settlement_currency="USD",
                )
            self.assertEqual(HostileProjectedFill.field_reads, 0)

            with self.assertRaisesRegex(
                TypeError,
                "provider_fill must be exact ProviderFillEvidence",
            ):
                commit_provider_fill_with_reservation_consumption(
                    economics,
                    reservations,
                    command_id="hostile-initial-provider",
                    idempotency_key="hostile-initial-provider",
                    reservation_id="reservation-never-read",
                    projected_fill=projected,
                    provider_fill=object.__new__(HostileProviderFill),
                    expected_instrument="ABC",
                    settlement_currency="USD",
                )
            self.assertEqual(HostileProviderFill.field_reads, 0)
            self.assertEqual(economics.transactions, ())

    def test_provider_fill_facade_rejects_polymorphic_text_before_callbacks(self):
        touched = []

        class HostileText(str):
            def strip(self, *args, **kwargs):
                touched.append("strip")
                raise AssertionError("hostile provider-fill text callback")

            def upper(self, *args, **kwargs):
                touched.append("upper")
                raise AssertionError("hostile provider-fill text callback")

        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            _orders, economics, reservations = books(store)
            projected, provider = evidence()

            with self.assertRaisesRegex(ValueError, "expected_instrument"):
                commit_provider_fill_with_reservation_consumption(
                    economics,
                    reservations,
                    command_id="hostile-initial-text",
                    idempotency_key="hostile-initial-text",
                    reservation_id="reservation-never-read",
                    projected_fill=projected,
                    provider_fill=provider,
                    expected_instrument=HostileText("ABC"),
                    settlement_currency="USD",
                )
            self.assertEqual(touched, [])
            self.assertEqual(economics.transactions, ())

    def test_provider_fill_correction_facade_rejects_polymorphic_evidence_first(self):
        class HostileProjectedFill(ProjectedFillEvidence):
            field_reads = 0

            def __getattribute__(self, name):
                if name not in {"field_reads", "__class__"}:
                    type(self).field_reads += 1
                    raise AssertionError("hostile correction projected-fill field access")
                return super().__getattribute__(name)

        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            _orders, economics, reservations = books(store)
            _projected, provider = evidence()

            with self.assertRaisesRegex(
                TypeError,
                "original_projected_fill must be exact ProjectedFillEvidence",
            ):
                accounting_impl.commit_provider_fill_correction_with_settlement_replacement(
                    economics,
                    None,
                    reservation_book=reservations,
                    reservation_id="reservation-never-read",
                    command_id="hostile-correction",
                    idempotency_key="hostile-correction",
                    original_projected_fill=object.__new__(HostileProjectedFill),
                    original_provider_fill=provider,
                    corrected_projected_fill=_projected,
                    corrected_provider_fill=provider,
                    expected_instrument="ABC",
                    settlement_currency="USD",
                    correction_observed_at=WHEN,
                    settlement_obligations=(),
                )
            self.assertEqual(HostileProjectedFill.field_reads, 0)
            self.assertEqual(economics.transactions, ())

    def test_correction_barrier_rejects_polymorphic_transaction_before_field_access(self):
        class HostileTransaction(JournalTransaction):
            field_reads = 0

            def __getattribute__(self, name):
                if name not in {"field_reads", "__class__"}:
                    type(self).field_reads += 1
                    raise AssertionError("hostile correction transaction field access")
                return super().__getattribute__(name)

        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = JournalStore(root / "journal.sqlite3")
            _orders, economics, _reservations = books(store)
            evidence_root = root / "settlement-evidence"
            settlement = DurableSettlementBook(
                store,
                provider_id=PROVIDER,
                account_id=ACCOUNT,
                environment=ENVIRONMENT,
                evidence_artifact_root=evidence_root,
                evidence_artifact_store=ArtifactStore(evidence_root),
            )
            hostile = object.__new__(HostileTransaction)

            with self.assertRaisesRegex(
                TypeError,
                "reversal must be an exact JournalTransaction",
            ):
                accounting_impl.commit_economic_correction_with_settlement_replacement(
                    economics,
                    settlement,
                    command_id="hostile-correction-transaction",
                    idempotency_key="hostile-correction-transaction",
                    reversal=hostile,
                    replacement=None,
                    settlement_obligations=(),
                )
            self.assertEqual(HostileTransaction.field_reads, 0)
            self.assertEqual(economics.transactions, ())

    def test_correction_binding_helper_rejects_polymorphic_transaction_first(self):
        class HostileTransaction(JournalTransaction):
            field_reads = 0

            def __getattribute__(self, name):
                if name not in {"field_reads", "__class__"}:
                    type(self).field_reads += 1
                    raise AssertionError("hostile correction binding transaction read")
                return super().__getattribute__(name)

        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            _orders, economics, reservations = books(store)
            projected, provider = evidence()
            hostile = object.__new__(HostileTransaction)

            with self.assertRaisesRegex(
                TypeError,
                "replacement must be an exact JournalTransaction",
            ):
                accounting_impl._prepare_provider_fill_correction_binding(
                    economics,
                    reservations,
                    reservation_id="reservation-never-read",
                    original_projected_fill=projected,
                    original_provider_fill=provider,
                    corrected_projected_fill=projected,
                    corrected_provider_fill=provider,
                    replacement=hostile,
                    asset_family="CASH_EQUITY",
                    committed_at=WHEN,
                )
            self.assertEqual(HostileTransaction.field_reads, 0)
            self.assertEqual(economics.transactions, ())

    def test_atomic_barrier_rejects_polymorphic_prepared_binding_before_field_access(self):
        class HostileBinding(accounting_impl.PreparedProviderFillBinding):
            field_reads = 0

            def __getattribute__(self, name):
                if name not in {"field_reads", "__class__"}:
                    type(self).field_reads += 1
                    raise AssertionError("hostile prepared binding field access")
                return super().__getattribute__(name)

        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            _orders, economics, reservations = books(store)
            hostile = object.__new__(HostileBinding)

            with self.assertRaisesRegex(
                TypeError,
                "exact PreparedProviderFillBinding",
            ):
                accounting_impl.commit_economic_batch_with_reservation_consumption(
                    economics,
                    reservations,
                    command_id="hostile-prepared-binding",
                    idempotency_key="hostile-prepared-binding",
                    reservation_id="reservation-never-read",
                    usage={},
                    transactions=(),
                    provider_fill_binding=hostile,
                )
            self.assertEqual(HostileBinding.field_reads, 0)
            self.assertEqual(economics.transactions, ())

    def test_provider_fill_binding_rejects_polymorphic_plan_before_field_access(self):
        class HostilePlan(ProviderFillFinancialPlan):
            field_reads = 0

            def __getattribute__(self, name):
                if name not in {"field_reads", "__class__"}:
                    type(self).field_reads += 1
                    raise AssertionError("hostile provider-fill plan field access")
                return super().__getattribute__(name)

        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            _orders, economics, _reservations = books(store)
            projected, provider = evidence()
            hostile = object.__new__(HostilePlan)

            with self.assertRaisesRegex(
                TypeError,
                "plan must be exact ProviderFillFinancialPlan",
            ):
                accounting_impl._prepare_provider_fill_binding(
                    economics,
                    plan=hostile,
                    projected_fill=projected,
                    provider_fill=provider,
                    committed_at=WHEN,
                )
            self.assertEqual(HostilePlan.field_reads, 0)
            self.assertEqual(economics.transactions, ())

    def test_fill_bust_rejects_polymorphic_fill_evidence_before_field_access(self):
        class HostileProjectedFill(ProjectedFillEvidence):
            field_reads = 0

            def __getattribute__(self, name):
                if name not in {"field_reads", "__class__"}:
                    type(self).field_reads += 1
                    raise AssertionError("hostile projected-fill field access")
                return super().__getattribute__(name)

        class HostileProviderFill(ProviderFillEvidence):
            field_reads = 0

            def __getattribute__(self, name):
                if name not in {"field_reads", "__class__"}:
                    type(self).field_reads += 1
                    raise AssertionError("hostile provider-fill field access")
                return super().__getattribute__(name)

        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            orders, economics, reservations = books(store)
            projected, provider = seed(orders, economics, reservations)

            hostile_projected = object.__new__(HostileProjectedFill)
            with self.assertRaisesRegex(
                TypeError,
                "projected_fill must be ProjectedFillEvidence",
            ):
                commit_provider_fill_bust_with_economic_reversal(
                    economics,
                    orders,
                    command_id="provider-bust-hostile-projected",
                    idempotency_key="provider-bust-hostile-projected",
                    projected_fill=hostile_projected,
                    provider_fill=provider,
                    expected_instrument="ABC",
                    settlement_currency="USD",
                    bust_provider_revision=BUST_REVISION,
                    bust_observed_at=WHEN,
                    order_event_key="bust-hostile-projected",
                    reservation_book=reservations,
                    reservation_id="reservation-1",
                    committed_at=WHEN,
                )
            self.assertEqual(HostileProjectedFill.field_reads, 0)

            hostile_provider = object.__new__(HostileProviderFill)
            with self.assertRaisesRegex(
                TypeError,
                "provider_fill must be ProviderFillEvidence",
            ):
                commit_provider_fill_bust_with_economic_reversal(
                    economics,
                    orders,
                    command_id="provider-bust-hostile-provider",
                    idempotency_key="provider-bust-hostile-provider",
                    projected_fill=projected,
                    provider_fill=hostile_provider,
                    expected_instrument="ABC",
                    settlement_currency="USD",
                    bust_provider_revision=BUST_REVISION,
                    bust_observed_at=WHEN,
                    order_event_key="bust-hostile-provider",
                    reservation_book=reservations,
                    reservation_id="reservation-1",
                    committed_at=WHEN,
                )
            self.assertEqual(HostileProviderFill.field_reads, 0)

            self.assertEqual(
                orders.order("order-1").snapshot().filled_quantity,
                Decimal("1"),
            )
            self.assertEqual(economics.position("ABC"), Decimal("1"))
            self.assertEqual(len(economics.transactions), 1)
            reservation = reservations.get("reservation-1")
            self.assertEqual(
                reservation.consumed["CASH:USD"],
                Decimal("100"),
            )
            self.assertEqual(
                reservation.remaining["CASH:USD"],
                Decimal("0"),
            )

    def test_fresh_bust_is_atomic_restart_safe_and_idempotent(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            orders, economics, reservations = books(store)
            projected, provider = seed(orders, economics, reservations)

            self.assertTrue(
                atomic_bust(orders, economics, reservations, projected, provider)
            )
            self.assert_busted(orders, economics, reservations)

            reopened = JournalStore(path)
            ro, re, rr = books(reopened)
            self.assertFalse(atomic_bust(ro, re, rr, projected, provider))
            self.assert_busted(ro, re, rr)

    def test_exact_retry_survives_unrelated_later_reservation_event(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            orders, economics, reservations = books(store)
            projected, provider = seed(orders, economics, reservations)

            self.assertTrue(
                atomic_bust(
                    orders,
                    economics,
                    reservations,
                    projected,
                    provider,
                )
            )
            reservations.reserve(
                command_id="later-reservation-command",
                idempotency_key="later-reservation-idempotency",
                reservation_id="later-reservation",
                intent_id="later-intent",
                requirements={"CASH:EUR": "3"},
                available={"CASH:EUR": "3"},
            )

            ro, re, rr = books(JournalStore(path))
            self.assertFalse(
                atomic_bust(ro, re, rr, projected, provider)
            )
            self.assert_busted(ro, re, rr)
            self.assertEqual(
                rr.get("later-reservation").remaining["CASH:EUR"],
                Decimal("3"),
            )

    def test_bust_restores_only_one_partial_fill_usage(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            orders, economics, reservations = books(store)
            reservations.reserve(
                command_id="reserve-partials",
                idempotency_key="reserve-partials",
                reservation_id="reservation-1",
                intent_id="intent-1",
                requirements={"CASH:USD": "200"},
                available={"CASH:USD": "1000"},
            )
            orders.create_order(
                event_key="create-partials",
                client_order_id="order-1",
                instrument="ABC",
                side="BUY",
                requested_quantity="2",
                committed_at=WHEN,
            )

            def fill(fill_id, execution_id, quantity, command_id):
                projected = ProjectedFillEvidence.create(
                    fill_id=fill_id,
                    provider_execution_id=execution_id,
                    intent_id="intent-1",
                    client_order_id="order-1",
                    side="BUY",
                    quantity=quantity,
                    price="100",
                )
                provider = ProviderFillEvidence.create(
                    provider_id=PROVIDER,
                    account_id=ACCOUNT,
                    environment=ENVIRONMENT,
                    provider_execution_id=execution_id,
                    client_order_id="order-1",
                    instrument="ABC",
                    quantity=quantity,
                    price="100",
                    fee_amount="0",
                    fee_currency="USD",
                    trade_time=WHEN,
                    side="BUY",
                )
                self.assertTrue(
                    commit_provider_fill_with_reservation_consumption(
                        economics,
                        reservations,
                        command_id=command_id,
                        idempotency_key=command_id,
                        reservation_id="reservation-1",
                        projected_fill=projected,
                        provider_fill=provider,
                        expected_instrument="ABC",
                        settlement_currency="USD",
                        observed_at=WHEN,
                        committed_at=WHEN,
                        order_book=orders,
                        order_event_key=fill_id,
                    )
                )
                return projected, provider

            first_projected, first_provider = fill(
                "fill-partial-a",
                "provider-execution-partial-a",
                "0.4",
                "provider-fill-partial-a",
            )
            fill(
                "fill-partial-b",
                "provider-execution-partial-b",
                "0.6",
                "provider-fill-partial-b",
            )
            before = reservations.get("reservation-1")
            self.assertEqual(before.consumed["CASH:USD"], Decimal("100"))
            self.assertEqual(before.remaining["CASH:USD"], Decimal("100"))

            self.assertTrue(
                atomic_bust(
                    orders,
                    economics,
                    reservations,
                    first_projected,
                    first_provider,
                )
            )

            after = reservations.get("reservation-1")
            self.assertEqual(after.consumed["CASH:USD"], Decimal("60"))
            self.assertEqual(after.remaining["CASH:USD"], Decimal("140"))
            self.assertEqual(
                reservations.total_reserved("CASH:USD"),
                Decimal("140"),
            )
            order = orders.order("order-1").snapshot()
            self.assertEqual(order.filled_quantity, Decimal("0.6"))
            self.assertEqual(order.fill_count, 1)
            self.assertEqual(economics.position("ABC"), Decimal("0.6"))

            # A later fill on the same reservation is valid forward progress.
            # Exact retry of the older bust must resolve its historical command
            # authority without requiring the current reservation snapshot to
            # equal the old post-bust snapshot and without restoring twice.
            fill(
                "fill-partial-c",
                "provider-execution-partial-c",
                "0.2",
                "provider-fill-partial-c",
            )
            before_retry = reservations.get("reservation-1")
            self.assertEqual(before_retry.consumed["CASH:USD"], Decimal("80"))
            self.assertEqual(before_retry.remaining["CASH:USD"], Decimal("120"))
            self.assertFalse(
                atomic_bust(
                    orders,
                    economics,
                    reservations,
                    first_projected,
                    first_provider,
                )
            )
            after_retry = reservations.get("reservation-1")
            self.assertEqual(after_retry, before_retry)
            self.assertEqual(
                orders.order("order-1").snapshot().filled_quantity,
                Decimal("0.8"),
            )
            self.assertEqual(economics.position("ABC"), Decimal("0.8"))

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
                    atomic_bust(orders, economics, reservations, projected, provider)
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
                    atomic_bust(orders, economics, reservations, projected, provider)
            finally:
                JournalStore.commit_command = original

            ro, re, rr = books(JournalStore(path))
            self.assertFalse(atomic_bust(ro, re, rr, projected, provider))
            self.assert_busted(ro, re, rr)

    def test_replay_rejects_command_effect_from_wrong_aggregate_identity(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            orders, economics, reservations = books(store)
            projected, provider = seed(orders, economics, reservations)
            self.assertTrue(atomic_bust(orders, economics, reservations, projected, provider))

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
                    atomic_bust(orders, economics, reservations, projected, provider)
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
                atomic_bust(orders, economics, reservations, projected, provider)
            )
            self.assert_busted(orders, economics, reservations)
            self.assertFalse(
                atomic_bust(orders, economics, reservations, projected, provider)
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
                "missing reservation restoration",
            ):
                atomic_bust(orders, economics, reservations, projected, provider)

            self.assertEqual(
                orders.order("order-1").snapshot().filled_quantity,
                Decimal("0"),
            )
            self.assertEqual(economics.position("ABC"), Decimal("0"))
            self.assertEqual(
                reservations.get("reservation-1").consumed["CASH:USD"],
                Decimal("100"),
            )
            self.assertEqual(
                reservations.get("reservation-1").remaining["CASH:USD"],
                Decimal("0"),
            )

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
                    atomic_bust(orders, economics, reservations, projected, provider)
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

    def test_reservation_writer_after_cut_fences_atomic_bust(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            orders, economics, reservations = books(store)
            projected, provider = seed(orders, economics, reservations)
            original_prepare = (
                DurableReservationBook.prepare_restore_consumption_mutation
            )
            injected = False

            def race_after_reservation_cut(selected_book, **kwargs):
                nonlocal injected
                plan = original_prepare(selected_book, **kwargs)
                if selected_book is reservations and not injected:
                    injected = True
                    reservations.reserve(
                        command_id="reservation-race-command",
                        idempotency_key="reservation-race-idempotency",
                        reservation_id="reservation-race",
                        intent_id="intent-race",
                        requirements={"CASH:EUR": "1"},
                        available={"CASH:EUR": "1"},
                    )
                return plan

            DurableReservationBook.prepare_restore_consumption_mutation = (
                race_after_reservation_cut
            )
            try:
                with self.assertRaisesRegex(
                    ValueError,
                    "journal sequence changed after financial evidence validation",
                ):
                    atomic_bust(
                        orders,
                        economics,
                        reservations,
                        projected,
                        provider,
                    )
            finally:
                DurableReservationBook.prepare_restore_consumption_mutation = (
                    original_prepare
                )

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
            self.assertEqual(
                rr.get("reservation-1").remaining["CASH:USD"],
                Decimal("0"),
            )
            self.assertEqual(
                rr.get("reservation-race").remaining["CASH:EUR"],
                Decimal("1"),
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
                    atomic_bust(orders, economics, reservations, projected, provider)
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
                    atomic_bust(orders, economics, reservations, projected, provider)
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
                atomic_bust(orders, economics, reservations, projected, provider)

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

    def test_fill_bust_rejects_hostile_command_text_before_callback_or_mutation(self):
        class HostileText(str):
            strip_calls = 0

            def strip(self, *args, **kwargs):
                type(self).strip_calls += 1
                raise AssertionError("hostile strip callback executed")

        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            orders, economics, reservations = books(store)
            projected, provider = seed(orders, economics, reservations)

            with self.assertRaisesRegex(ValueError, "command_id is required"):
                commit_provider_fill_bust_with_economic_reversal(
                    economics,
                    orders,
                    command_id=HostileText("atomic-bust"),
                    idempotency_key="atomic-bust",
                    projected_fill=projected,
                    provider_fill=provider,
                    expected_instrument="ABC",
                    settlement_currency="USD",
                    bust_provider_revision=BUST_REVISION,
                    bust_observed_at=WHEN,
                    order_event_key="bust-1",
                    reservation_book=reservations,
                    reservation_id="reservation-1",
                    committed_at=WHEN,
                )

            self.assertEqual(HostileText.strip_calls, 0)
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
            self.assertEqual(
                reservations.get("reservation-1").remaining["CASH:USD"],
                Decimal("0"),
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
                atomic_bust(orders, economics, reservations, projected, provider)

            self.assertEqual(economics.position("ABC"), Decimal("1"))
            self.assertEqual(
                reservations.get("reservation-1").consumed["CASH:USD"],
                Decimal("100"),
            )


if __name__ == "__main__":
    unittest.main()
