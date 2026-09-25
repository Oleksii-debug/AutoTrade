from decimal import Decimal
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.authority import (
    AuthorityConflict,
    AuthorityPolicy,
    AuthorityService,
)
from mvp.autotrade_mvp.durable_reservations import DurableReservationBook
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.reconciliation import (
    ResourceAvailabilityEvidence,
    SnapshotConsistencyEvidence,
    reconcile_account,
)
from mvp.autotrade_mvp.reconciliation_journal import (
    load_account_resource_availability_evidence,
    record_reconciliation_checkpoint,
)
from mvp.autotrade_mvp.reservations import InsufficientAvailable
from mvp.autotrade_mvp.risk import RiskContext, RiskIntent, RiskPolicy
from mvp.autotrade_mvp.securities_borrow import (
    BorrowAvailabilityEvidence,
    borrow_resource_key,
    incremental_short_borrow_quantity,
)


INSTRUMENT_ID = "11111111-1111-4111-8111-111111111111"
PROVIDER_ID = "TEST_PROVIDER"
ACCOUNT_ID = "paper-borrow-authority"
ENVIRONMENT = "PAPER"
NOW = "2026-09-25T05:01:00Z"


def _borrow_key():
    return borrow_resource_key(
        provider_id=PROVIDER_ID,
        account_id=ACCOUNT_ID,
        environment=ENVIRONMENT,
        instrument_id=INSTRUMENT_ID,
        instrument_version=1,
    )


def _borrow_evidence(capacity="100"):
    return BorrowAvailabilityEvidence(
        provider_id=PROVIDER_ID,
        account_id=ACCOUNT_ID,
        environment=ENVIRONMENT,
        instrument_id=INSTRUMENT_ID,
        instrument_version=1,
        locate_id="locate-authority-1",
        provider_revision="borrow-snapshot-r1",
        capacity_quantity=capacity,
        hard_to_borrow=True,
        observed_at="2026-09-25T05:00:30Z",
        effective_at="2026-09-25T05:00:00Z",
        expires_at="2026-09-25T05:05:00Z",
        evidence_ref="provider:borrow-snapshot-r1",
        indicative_rate="0.01",
    )


def _resource_evidence(*, capacity="100", cash="10000"):
    borrow = _borrow_evidence(capacity)
    key = borrow.resource_key
    return ResourceAvailabilityEvidence(
        provider_id=PROVIDER_ID,
        account_id=ACCOUNT_ID,
        environment=ENVIRONMENT,
        snapshot_id="account-resource-cut-1",
        query_started_at="2026-09-25T05:00:00Z",
        query_completed_at="2026-09-25T05:00:30Z",
        valid_until="2026-09-25T05:04:00Z",
        provider_as_of="2026-09-25T05:00:30Z",
        available_resources={
            key: capacity,
            "CASH:USD": cash,
        },
        evidence_refs=(
            "provider:account-resource-cut-1",
            borrow.evidence_ref,
        ),
        resource_details={key: borrow.resource_detail()},
    )


def _snapshot():
    return SnapshotConsistencyEvidence(
        provider_id=PROVIDER_ID,
        account_id=ACCOUNT_ID,
        environment=ENVIRONMENT,
        mode="ATOMIC",
        query_started_at="2026-09-25T05:00:00Z",
        query_completed_at="2026-09-25T05:00:30Z",
    )


def _result(*, local_borrow="40", provider_borrow="40", recalled=False):
    key = _borrow_key()
    return reconcile_account(
        provider_id=PROVIDER_ID,
        account_id=ACCOUNT_ID,
        environment=ENVIRONMENT,
        local_cash={"USD": "10000"},
        provider_cash={"USD": "10000"},
        local_positions={"ABC": "-40"},
        provider_positions={"ABC": "-40"},
        local_execution_ids=(),
        provider_fills=(),
        snapshot_consistency=_snapshot(),
        coverage_start="2026-09-25T05:00:00Z",
        coverage_end="2026-09-25T05:01:00Z",
        pagination_complete=True,
        resource_availability=_resource_evidence(),
        local_borrowed_resources={key: local_borrow},
        provider_borrowed_resources={key: provider_borrow},
        active_borrow_recall_resources=((key,) if recalled else ()),
    )


def _policy():
    return AuthorityPolicy.create(
        policy_id="borrow-policy",
        account_id=ACCOUNT_ID,
        environments={ENVIRONMENT},
        instruments={(INSTRUMENT_ID, 1)},
        actions={"ORDER.SUBMIT"},
        max_notional="5000",
        valid_from="2026-09-25T00:00:00Z",
        expires_at="2026-09-26T00:00:00Z",
        autonomous=True,
        protection_only=False,
    )


def _risk_policy():
    return RiskPolicy.create(
        max_abs_position="200",
        max_single_notional="5000",
        max_gross_leverage="5",
        max_net_leverage="5",
        max_daily_loss="5000",
        max_drawdown_fraction="0.50",
        max_data_age_seconds="10",
        max_fx_age_seconds="60",
        min_margin_headroom="0.10",
        max_stress_loss="5000",
    )


def _risk_context(*, reserved="-30", borrow_available=True):
    return RiskContext.create(
        state_version=1,
        equity="10000",
        positions={"ABC": "-40"},
        marks={"ABC": "10"},
        reserved_position_delta=(
            {} if reserved is None else {"ABC": reserved}
        ),
        daily_pnl="0",
        drawdown_fraction="0",
        market_data_age_seconds="1",
        fx_age_seconds={"USD": "1"},
        margin_headroom="1",
        capability_allowed=True,
        borrow_available=borrow_available,
        stress_scenarios=({"ABC": "0.10"},),
    )


def _checkpoint(store, *, recalled=False, local_borrow="40", provider_borrow="40"):
    result = _result(
        recalled=recalled,
        local_borrow=local_borrow,
        provider_borrow=provider_borrow,
    )
    return record_reconciliation_checkpoint(
        store,
        reconciliation_id=(
            "borrow-recalled" if recalled else "borrow-clean"
        )
        + f"-{local_borrow}-{provider_borrow}",
        result=result,
        observed_at="2026-09-25T05:00:30Z",
        host_id="borrow-test-host",
        owner_epoch="1",
    )


def _authority(store):
    service = AuthorityService(store)
    service.register_policy(_policy())
    return service


def _admit_short(
    authority,
    reservations,
    checkpoint,
    *,
    suffix,
    reserved="-30",
    quantity="20",
    requirement="20",
):
    key = _borrow_key()
    return authority.admit(
        command_id=f"borrow-command-{suffix}",
        idempotency_key=f"borrow-idem-{suffix}",
        admission_id=f"borrow-admission-{suffix}",
        policy_id="borrow-policy",
        intent_id=f"borrow-intent-{suffix}",
        intent_hash="sha256:" + ("a" * 64),
        account_id=ACCOUNT_ID,
        environment=ENVIRONMENT,
        instrument_id=INSTRUMENT_ID,
        instrument_version=1,
        action="ORDER.SUBMIT",
        notional=str(Decimal(quantity) * Decimal("10")),
        capability_snapshot_id="borrow-capability-1",
        risk_intent=RiskIntent.create(
            symbol="ABC",
            side="SELL",
            quantity=quantity,
            price="10",
            expected_state_version=1,
            instrument_type="EQUITY",
        ),
        risk_context=_risk_context(reserved=reserved),
        risk_policy=_risk_policy(),
        risk_valid_until="2026-09-25T05:03:00Z",
        reservation_book=reservations,
        reservation_id=f"borrow-reservation-{suffix}",
        reservation_requirements={key: requirement},
        reservation_available={key: "100"},
        reservation_checkpoint_event_id=checkpoint["event_id"],
        reservation_provider_id=PROVIDER_ID,
        reservation_max_age_seconds="60",
        now=NOW,
    )


class SecuritiesBorrowAuthorityTests(unittest.TestCase):
    def test_incremental_short_quantity_handles_crossing_flat_and_reserved_orders(self):
        self.assertEqual(
            incremental_short_borrow_quantity(
                side="SELL",
                quantity="20",
                current_position="-40",
                reserved_position_delta="-30",
            ),
            Decimal("20"),
        )
        self.assertEqual(
            incremental_short_borrow_quantity(
                side="SELL",
                quantity="20",
                current_position="10",
                reserved_position_delta="0",
            ),
            Decimal("10"),
        )
        self.assertEqual(
            incremental_short_borrow_quantity(
                side="BUY",
                quantity="10",
                current_position="-40",
                reserved_position_delta="-30",
            ),
            Decimal("0"),
        )

    def test_capacity_100_existing_40_reserved_30_allows_20_then_rejects_next_20(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            authority = _authority(store)
            checkpoint = _checkpoint(store)
            reservations = DurableReservationBook(
                store,
                environment=ENVIRONMENT,
                account_id=ACCOUNT_ID,
            )
            key = _borrow_key()

            reservations.reserve(
                command_id="existing-short-command",
                idempotency_key="existing-short-idem",
                reservation_id="existing-short-reservation",
                intent_id="existing-short-intent",
                requirements={key: "30"},
                available={key: "60"},
            )
            self.assertEqual(reservations.total_reserved(key), Decimal("30"))

            first = _admit_short(
                authority,
                reservations,
                checkpoint,
                suffix="first",
                reserved="-30",
            )
            self.assertEqual(first.outcome, "ADMITTED")
            self.assertEqual(reservations.total_reserved(key), Decimal("50"))

            risk_event = store.load_events(
                "risk_decision",
                first.risk_decision_id,
            )[0]
            adjustment = risk_event["payload"][
                "reservation_availability_evidence"
            ]["borrow_capacity_adjustments"][key]
            self.assertEqual(adjustment["total_capacity"], "100")
            self.assertEqual(
                adjustment["current_borrowed_quantity"],
                "40",
            )
            self.assertEqual(adjustment["reservable_capacity"], "60")
            self.assertEqual(adjustment["required_increment"], "20")

            with self.assertRaises(InsufficientAvailable):
                _admit_short(
                    authority,
                    reservations,
                    checkpoint,
                    suffix="second",
                    reserved="-50",
                )
            self.assertEqual(reservations.total_reserved(key), Decimal("50"))

    def test_equity_short_cannot_use_boolean_borrow_without_exact_resource(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            authority = _authority(store)
            checkpoint = _checkpoint(store)
            reservations = DurableReservationBook(
                store,
                environment=ENVIRONMENT,
                account_id=ACCOUNT_ID,
            )

            with self.assertRaisesRegex(
                AuthorityConflict,
                "exact scoped borrow reservation",
            ):
                authority.admit(
                    command_id="missing-borrow-command",
                    idempotency_key="missing-borrow-idem",
                    admission_id="missing-borrow-admission",
                    policy_id="borrow-policy",
                    intent_id="missing-borrow-intent",
                    intent_hash="sha256:" + ("b" * 64),
                    account_id=ACCOUNT_ID,
                    environment=ENVIRONMENT,
                    instrument_id=INSTRUMENT_ID,
                    instrument_version=1,
                    action="ORDER.SUBMIT",
                    notional="200",
                    capability_snapshot_id="borrow-capability-1",
                    risk_intent=RiskIntent.create(
                        symbol="ABC",
                        side="SELL",
                        quantity="20",
                        price="10",
                        expected_state_version=1,
                        instrument_type="EQUITY",
                    ),
                    risk_context=_risk_context(reserved=None),
                    risk_policy=_risk_policy(),
                    risk_valid_until="2026-09-25T05:03:00Z",
                    reservation_book=reservations,
                    reservation_id="missing-borrow-reservation",
                    reservation_requirements={"CASH:USD": "1"},
                    reservation_available={"CASH:USD": "10000"},
                    reservation_checkpoint_event_id=checkpoint["event_id"],
                    reservation_provider_id=PROVIDER_ID,
                    reservation_max_age_seconds="60",
                    now=NOW,
                )
            self.assertEqual(reservations.version, 0)

    def test_active_recall_blocks_new_short_but_not_cash_funded_cover(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            authority = _authority(store)
            checkpoint = _checkpoint(store, recalled=True)
            reservations = DurableReservationBook(
                store,
                environment=ENVIRONMENT,
                account_id=ACCOUNT_ID,
            )
            key = _borrow_key()

            with self.assertRaisesRegex(ValueError, "blocked"):
                _admit_short(
                    authority,
                    reservations,
                    checkpoint,
                    suffix="recalled-short",
                    reserved=None,
                )
            self.assertEqual(reservations.total_reserved(key), Decimal("0"))

            cover = authority.admit(
                command_id="cover-command",
                idempotency_key="cover-idem",
                admission_id="cover-admission",
                policy_id="borrow-policy",
                intent_id="cover-intent",
                intent_hash="sha256:" + ("c" * 64),
                account_id=ACCOUNT_ID,
                environment=ENVIRONMENT,
                instrument_id=INSTRUMENT_ID,
                instrument_version=1,
                action="ORDER.SUBMIT",
                notional="100",
                capability_snapshot_id="borrow-capability-1",
                risk_intent=RiskIntent.create(
                    symbol="ABC",
                    side="BUY",
                    quantity="10",
                    price="10",
                    expected_state_version=1,
                    instrument_type="EQUITY",
                ),
                risk_context=_risk_context(
                    reserved=None,
                    borrow_available=False,
                ),
                risk_policy=_risk_policy(),
                risk_valid_until="2026-09-25T05:03:00Z",
                reservation_book=reservations,
                reservation_id="cover-reservation",
                reservation_requirements={"CASH:USD": "100"},
                reservation_available={"CASH:USD": "10000"},
                reservation_checkpoint_event_id=checkpoint["event_id"],
                reservation_provider_id=PROVIDER_ID,
                reservation_max_age_seconds="60",
                now=NOW,
                risk_reducing=True,
            )
            self.assertEqual(cover.outcome, "ADMITTED")
            self.assertEqual(
                reservations.total_reserved("CASH:USD"),
                Decimal("100"),
            )

    def test_provider_local_borrow_mismatch_is_durable_scoped_blocker(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            result = _result(local_borrow="40", provider_borrow="39")
            key = _borrow_key()
            self.assertEqual(
                result.borrow_differences[key],
                Decimal("-1"),
            )
            self.assertIn(key, result.blocking_resources)
            self.assertTrue(result.complete)

            checkpoint = record_reconciliation_checkpoint(
                store,
                reconciliation_id="borrow-mismatch",
                result=result,
                observed_at="2026-09-25T05:00:30Z",
                host_id="borrow-test-host",
                owner_epoch="1",
            )
            self.assertEqual(
                checkpoint["payload"]["borrow_differences"],
                {key: "-1"},
            )
            with self.assertRaisesRegex(ValueError, "blocked"):
                load_account_resource_availability_evidence(
                    store,
                    checkpoint_event_id=checkpoint["event_id"],
                    provider_id=PROVIDER_ID,
                    account_id=ACCOUNT_ID,
                    environment=ENVIRONMENT,
                    resources=(key,),
                    now=NOW,
                    max_age_seconds="60",
                )

    def test_borrow_detail_scope_and_capacity_are_revalidated_before_checkpoint(self):
        borrow = _borrow_evidence()
        key = borrow.resource_key
        bad = borrow.resource_detail()
        bad["account_id"] = "wrong-account"
        with self.assertRaisesRegex(ValueError, "identity|scope"):
            ResourceAvailabilityEvidence(
                provider_id=PROVIDER_ID,
                account_id=ACCOUNT_ID,
                environment=ENVIRONMENT,
                snapshot_id="bad-borrow-cut",
                query_started_at="2026-09-25T05:00:00Z",
                query_completed_at="2026-09-25T05:00:30Z",
                valid_until="2026-09-25T05:04:00Z",
                available_resources={key: "100"},
                resource_details={key: bad},
            )

        bad_amount = borrow.resource_detail()
        bad_amount["capacity_quantity"] = "99"
        with self.assertRaisesRegex(ValueError, "capacity differs"):
            ResourceAvailabilityEvidence(
                provider_id=PROVIDER_ID,
                account_id=ACCOUNT_ID,
                environment=ENVIRONMENT,
                snapshot_id="bad-borrow-amount",
                query_started_at="2026-09-25T05:00:00Z",
                query_completed_at="2026-09-25T05:00:30Z",
                valid_until="2026-09-25T05:04:00Z",
                available_resources={key: "100"},
                resource_details={key: bad_amount},
            )


if __name__ == "__main__":
    unittest.main()
