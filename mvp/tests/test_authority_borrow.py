from decimal import Decimal
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.authority import (
    AuthorityConflict,
    AuthorityPolicy,
    AuthorityService,
)
from mvp.autotrade_mvp.borrow import (
    BorrowLifecycleJournal,
    BorrowLoanEvidence,
    BorrowLocateEvidence,
    BorrowRecallEvidence,
    BorrowResourceIdentity,
)
from mvp.autotrade_mvp.durable_reservations import DurableReservationBook
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.reservations import InsufficientAvailable
from mvp.autotrade_mvp.reconciliation import (
    ResourceAvailabilityEvidence,
    SnapshotConsistencyEvidence,
    reconcile_account,
)
from mvp.autotrade_mvp.reconciliation_journal import (
    record_reconciliation_checkpoint,
)
from mvp.autotrade_mvp.risk import RiskContext, RiskIntent, RiskPolicy


INSTRUMENT_ID = "11111111-1111-4111-8111-111111111111"
PROVIDER_ID = "TEST_PROVIDER"
ACCOUNT_ID = "paper-borrow"
ENVIRONMENT = "PAPER"
NOW = "2026-09-24T18:01:00Z"


def policy():
    return AuthorityPolicy.create(
        policy_id="borrow-policy",
        account_id=ACCOUNT_ID,
        environments={ENVIRONMENT},
        instruments={(INSTRUMENT_ID, 1)},
        actions={"ORDER.SUBMIT"},
        max_notional="5000",
        valid_from="2026-09-24T00:00:00Z",
        expires_at="2026-09-25T00:00:00Z",
        autonomous=True,
        protection_only=False,
    )


def risk_policy():
    return RiskPolicy.create(
        max_abs_position="200",
        max_single_notional="10000",
        max_gross_leverage="10",
        max_net_leverage="10",
        max_daily_loss="5000",
        max_drawdown_fraction="0.50",
        max_data_age_seconds="5",
        max_fx_age_seconds="60",
        min_margin_headroom="0.10",
        max_stress_loss="5000",
    )


def risk_context(*, position="0", reserved="0", borrow_available=True):
    return RiskContext.create(
        state_version=1,
        equity="10000",
        positions=(
            {} if Decimal(position) == 0 else {"ABC": position}
        ),
        marks={"ABC": "100"},
        reserved_position_delta=(
            {} if Decimal(reserved) == 0 else {"ABC": reserved}
        ),
        daily_pnl="0",
        drawdown_fraction="0",
        market_data_age_seconds="1",
        fx_age_seconds={"USD": "1"},
        margin_headroom="1",
        capability_allowed=True,
        borrow_available=borrow_available,
        stress_scenarios=({"ABC": "-0.10"},),
    )


def short_intent(*, quantity="2", price="100"):
    return RiskIntent.create(
        symbol="ABC",
        side="SELL",
        quantity=quantity,
        price=price,
        expected_state_version=1,
        instrument_type="EQUITY",
    )


def checkpoint(store):
    result = reconcile_account(
        provider_id=PROVIDER_ID,
        account_id=ACCOUNT_ID,
        environment=ENVIRONMENT,
        local_cash={"USD": "10000"},
        provider_cash={"USD": "10000"},
        local_positions={},
        provider_positions={},
        local_execution_ids=(),
        provider_fills=(),
        snapshot_consistency=SnapshotConsistencyEvidence(
            provider_id=PROVIDER_ID,
            account_id=ACCOUNT_ID,
            environment=ENVIRONMENT,
            mode="ATOMIC",
            query_started_at="2026-09-24T18:00:00Z",
            query_completed_at="2026-09-24T18:00:30Z",
        ),
        coverage_start="2026-09-24T18:00:00Z",
        coverage_end=NOW,
        pagination_complete=True,
        provider_activity_provider_id=PROVIDER_ID,
        provider_activity_account_id=ACCOUNT_ID,
        resource_availability=ResourceAvailabilityEvidence(
            provider_id=PROVIDER_ID,
            account_id=ACCOUNT_ID,
            environment=ENVIRONMENT,
            snapshot_id="borrow-account-capacity-1",
            query_started_at="2026-09-24T18:00:00Z",
            query_completed_at="2026-09-24T18:00:30Z",
            valid_until="2026-09-24T18:05:00Z",
            available_resources={"CASH:USD": "10000"},
            evidence_refs=("provider:account-capacity-1",),
        ),
    )
    return record_reconciliation_checkpoint(
        store,
        reconciliation_id="borrow-authority",
        result=result,
        observed_at="2026-09-24T18:00:30Z",
        host_id="borrow-test-host",
        owner_epoch="1",
    )


def borrow_resource():
    return BorrowResourceIdentity(
        provider_id=PROVIDER_ID,
        account_id=ACCOUNT_ID,
        environment=ENVIRONMENT,
        instrument_id=INSTRUMENT_ID,
        instrument_version=1,
    )


def record_locate(store, *, available="10"):
    resource = borrow_resource()
    journal = BorrowLifecycleJournal(store, resource)
    journal.record_locate(
        BorrowLocateEvidence(
            resource=resource,
            locate_id="locate-1",
            provider_revision="locate-rev-1",
            availability_state="AVAILABLE",
            available_quantity=available,
            observed_at="2026-09-24T18:00:20Z",
            effective_at="2026-09-24T18:00:00Z",
            valid_until="2026-09-24T18:04:00Z",
            evidence_refs=("provider:locate-1:rev-1",),
        )
    )
    return journal


def record_loan(store, *, borrowed):
    resource = borrow_resource()
    journal = BorrowLifecycleJournal(store, resource)
    journal.record_loan(
        BorrowLoanEvidence(
            resource=resource,
            provider_revision="loan-rev-1",
            borrowed_quantity=borrowed,
            observed_at="2026-09-24T18:00:20Z",
            effective_at="2026-09-24T18:00:00Z",
            valid_until="2026-09-24T18:04:00Z",
            evidence_refs=("provider:loan-rev-1",),
        )
    )
    return journal


def admit(
    authority,
    reservations,
    account_checkpoint,
    *,
    context=None,
    intent=None,
    requirements=None,
    available=None,
    command_id="borrow-command",
    admission_id="borrow-admission",
    intent_id="borrow-intent",
    reservation_id="borrow-reservation",
):
    return authority.admit(
        command_id=command_id,
        idempotency_key=command_id,
        admission_id=admission_id,
        policy_id="borrow-policy",
        intent_id=intent_id,
        intent_hash="sha256:" + "b" * 64,
        account_id=ACCOUNT_ID,
        environment=ENVIRONMENT,
        instrument_id=INSTRUMENT_ID,
        instrument_version=1,
        action="ORDER.SUBMIT",
        notional="200",
        capability_snapshot_id="borrow-capability-1",
        risk_intent=intent or short_intent(),
        risk_context=context or risk_context(),
        risk_policy=risk_policy(),
        risk_valid_until="2026-09-24T18:03:30Z",
        reservation_book=reservations,
        reservation_id=reservation_id,
        reservation_requirements=requirements or {"CASH:USD": "200"},
        reservation_available=available or {"CASH:USD": "10000"},
        reservation_checkpoint_event_id=account_checkpoint["event_id"],
        reservation_provider_id=PROVIDER_ID,
        reservation_max_age_seconds="60",
        now=NOW,
    )


class AuthorityBorrowTests(unittest.TestCase):
    def make_runtime(self, directory):
        store = JournalStore(f"{directory}/journal.sqlite3")
        authority = AuthorityService(store)
        authority.register_policy(policy())
        reservations = DurableReservationBook(
            store,
            environment=ENVIRONMENT,
            account_id=ACCOUNT_ID,
        )
        return store, authority, reservations, checkpoint(store)

    def test_caller_true_cannot_replace_missing_provider_borrow_evidence(self):
        with TemporaryDirectory() as directory:
            store, authority, reservations, account_checkpoint = self.make_runtime(
                directory
            )
            result = admit(
                authority,
                reservations,
                account_checkpoint,
                context=risk_context(borrow_available=True),
            )
            self.assertEqual(result.outcome, "REJECTED")
            self.assertEqual(reservations.version, 0)
            risk_event = store.load_events(
                "risk_decision",
                result.risk_decision_id,
            )[0]
            short_check = next(
                item
                for item in risk_event["payload"]["checks"]
                if item["rule_id"] == "short_borrow"
            )
            self.assertFalse(short_check["passed"])

    def test_provider_evidence_overrides_caller_false_and_reserves_borrow_atomically(self):
        with TemporaryDirectory() as directory:
            store, authority, reservations, account_checkpoint = self.make_runtime(
                directory
            )
            record_locate(store, available="10")
            result = admit(
                authority,
                reservations,
                account_checkpoint,
                context=risk_context(borrow_available=False),
            )
            self.assertEqual(result.outcome, "ADMITTED")
            resource = borrow_resource().resource_key
            self.assertEqual(
                reservations.total_reserved(resource),
                Decimal("2"),
            )
            self.assertEqual(
                reservations.total_reserved("CASH:USD"),
                Decimal("200"),
            )
            risk_event = store.load_events(
                "risk_decision",
                result.risk_decision_id,
            )[0]
            self.assertEqual(
                risk_event["payload"]["reservation_requirements"][resource],
                "2",
            )
            evidence = risk_event["payload"][
                "reservation_availability_evidence"
            ]
            self.assertEqual(
                evidence["availability"][resource],
                "10",
            )
            self.assertEqual(
                evidence["borrow_authority"]["resource"]["resource_key"],
                resource,
            )

    def test_caller_cannot_inject_borrow_requirement_or_capacity(self):
        with TemporaryDirectory() as directory:
            store, authority, reservations, account_checkpoint = self.make_runtime(
                directory
            )
            record_locate(store)
            resource = borrow_resource().resource_key
            with self.assertRaisesRegex(
                AuthorityConflict,
                "derived by financial authority",
            ):
                admit(
                    authority,
                    reservations,
                    account_checkpoint,
                    requirements={
                        "CASH:USD": "200",
                        resource: "1",
                    },
                )
            with self.assertRaisesRegex(
                AuthorityConflict,
                "derived by financial authority",
            ):
                admit(
                    authority,
                    reservations,
                    account_checkpoint,
                    available={
                        "CASH:USD": "10000",
                        resource: "999",
                    },
                )

    def test_insufficient_borrow_capacity_rejects_without_reservation(self):
        with TemporaryDirectory() as directory:
            store, authority, reservations, account_checkpoint = self.make_runtime(
                directory
            )
            record_locate(store, available="1")
            result = admit(
                authority,
                reservations,
                account_checkpoint,
            )
            self.assertEqual(result.outcome, "REJECTED")
            self.assertEqual(reservations.version, 0)

    def test_existing_short_requires_matching_provider_loan_truth(self):
        with TemporaryDirectory() as directory:
            store, authority, reservations, account_checkpoint = self.make_runtime(
                directory
            )
            record_locate(store, available="10")
            record_loan(store, borrowed="4")
            result = admit(
                authority,
                reservations,
                account_checkpoint,
                context=risk_context(position="-5"),
                intent=short_intent(quantity="1"),
            )
            self.assertEqual(result.outcome, "REJECTED")
            self.assertEqual(reservations.version, 0)

    def test_restart_retry_reuses_bound_borrow_cut(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            store = JournalStore(path)
            authority = AuthorityService(store)
            authority.register_policy(policy())
            reservations = DurableReservationBook(
                store,
                environment=ENVIRONMENT,
                account_id=ACCOUNT_ID,
            )
            account_checkpoint = checkpoint(store)
            record_locate(store, available="10")

            first = admit(
                authority,
                reservations,
                account_checkpoint,
                context=risk_context(borrow_available=False),
            )
            self.assertEqual(first.outcome, "ADMITTED")

            restarted_store = JournalStore(path)
            restarted_authority = AuthorityService(restarted_store)
            restarted_reservations = DurableReservationBook(
                restarted_store,
                environment=ENVIRONMENT,
                account_id=ACCOUNT_ID,
            )
            replay = admit(
                restarted_authority,
                restarted_reservations,
                account_checkpoint,
                context=risk_context(borrow_available=True),
            )
            self.assertEqual(replay, first)
            self.assertEqual(
                restarted_reservations.total_reserved(
                    borrow_resource().resource_key
                ),
                Decimal("2"),
            )

    def test_working_unknown_reservations_compete_atomically_for_borrow_capacity(self):
        with TemporaryDirectory() as directory:
            store, authority, reservations, account_checkpoint = self.make_runtime(
                directory
            )
            record_locate(store, available="60")
            record_loan(store, borrowed="40")
            borrow_key = borrow_resource().resource_key

            reservations.reserve(
                command_id="existing-short-command",
                idempotency_key="existing-short-idem",
                reservation_id="existing-short-reservation",
                intent_id="existing-short-intent",
                requirements={borrow_key: "30"},
                available={borrow_key: "60"},
            )
            self.assertEqual(
                reservations.total_reserved(borrow_key),
                Decimal("30"),
            )

            first = admit(
                authority,
                reservations,
                account_checkpoint,
                context=risk_context(
                    position="-40",
                    reserved="-30",
                    borrow_available=False,
                ),
                intent=short_intent(quantity="20", price="10"),
                command_id="borrow-command-first",
                admission_id="borrow-admission-first",
                intent_id="borrow-intent-first",
                reservation_id="borrow-reservation-first",
            )
            self.assertEqual(first.outcome, "ADMITTED")
            self.assertEqual(
                reservations.total_reserved(borrow_key),
                Decimal("50"),
            )

            with self.assertRaises(InsufficientAvailable):
                admit(
                    authority,
                    reservations,
                    account_checkpoint,
                    context=risk_context(
                        position="-40",
                        reserved="-50",
                        borrow_available=False,
                    ),
                    intent=short_intent(quantity="20", price="10"),
                    command_id="borrow-command-second",
                    admission_id="borrow-admission-second",
                    intent_id="borrow-intent-second",
                    reservation_id="borrow-reservation-second",
                )
            self.assertEqual(
                reservations.total_reserved(borrow_key),
                Decimal("50"),
            )

    def test_recall_after_admission_blocks_final_dispatch(self):
        with TemporaryDirectory() as directory:
            store, authority, reservations, account_checkpoint = self.make_runtime(
                directory
            )
            journal = record_locate(store, available="10")
            result = admit(
                authority,
                reservations,
                account_checkpoint,
            )
            self.assertEqual(result.outcome, "ADMITTED")

            resource = borrow_resource()
            journal.record_recall(
                BorrowRecallEvidence(
                    resource=resource,
                    recall_id="recall-1",
                    provider_revision="recall-rev-1",
                    recalled_quantity="1",
                    observed_at="2026-09-24T18:01:30Z",
                    effective_at="2026-09-24T18:01:15Z",
                    evidence_refs=("provider:recall-1:rev-1",),
                )
            )
            allowed, reason = authority.dispatch_allowed(
                result.admission_id,
                intent_hash=result.intent_hash,
                account_id=ACCOUNT_ID,
                environment=ENVIRONMENT,
                instrument_id=INSTRUMENT_ID,
                instrument_version=1,
                action="ORDER.SUBMIT",
                now="2026-09-24T18:02:00Z",
                capability_snapshot_id="borrow-capability-1",
            )
            self.assertFalse(allowed)
            self.assertEqual(reason, "borrow_provider_state_changed")


if __name__ == "__main__":
    unittest.main()
