from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

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
    record_reconciliation_checkpoint,
)
from mvp.autotrade_mvp.risk import RiskContext, RiskIntent, RiskPolicy


INSTRUMENT_ID = "11111111-1111-4111-8111-111111111111"
PROVIDER_ID = "TEST_PROVIDER"
ACCOUNT_ID = "paper-availability"
ENVIRONMENT = "PAPER"
NOW = "2026-09-24T18:01:00Z"


def _policy():
    return AuthorityPolicy.create(
        policy_id="availability-policy",
        account_id=ACCOUNT_ID,
        environments={ENVIRONMENT},
        instruments={(INSTRUMENT_ID, 1)},
        actions={"ORDER.SUBMIT"},
        max_notional="1000",
        valid_from="2026-09-24T00:00:00Z",
        expires_at="2026-09-25T00:00:00Z",
        autonomous=True,
        protection_only=False,
    )


def _risk_context():
    return RiskContext.create(
        state_version=1,
        equity="1000",
        positions={},
        marks={"ABC": "100"},
        reserved_position_delta={},
        daily_pnl="0",
        drawdown_fraction="0",
        market_data_age_seconds="1",
        fx_age_seconds={"USD": "1"},
        margin_headroom="1",
        capability_allowed=True,
        borrow_available=True,
        stress_scenarios=({"ABC": "-0.10"},),
    )


def _risk_policy():
    return RiskPolicy.create(
        max_abs_position="10",
        max_single_notional="1000",
        max_gross_leverage="2",
        max_net_leverage="2",
        max_daily_loss="500",
        max_drawdown_fraction="0.20",
        max_data_age_seconds="5",
        max_fx_age_seconds="60",
        min_margin_headroom="0.20",
        max_stress_loss="500",
    )


def _checkpoint(
    store,
    *,
    cash="1000",
    available_cash=None,
    observed_at="2026-09-24T18:00:30Z",
    reconciliation_id="availability-authority",
    snapshot_id="availability-snapshot",
    force_incomplete=False,
):
    if available_cash is None:
        available_cash = cash
    result = reconcile_account(
        provider_id=PROVIDER_ID,
        account_id=ACCOUNT_ID,
        environment=ENVIRONMENT,
        local_cash={"USD": cash},
        provider_cash={"USD": cash},
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
            snapshot_id=snapshot_id,
            query_started_at="2026-09-24T18:00:00Z",
            query_completed_at="2026-09-24T18:00:30Z",
            valid_until="2026-09-24T18:02:00Z",
            available_resources={"CASH:USD": available_cash},
            provider_as_of="2026-09-24T18:00:30Z",
            evidence_refs=(f"provider:{snapshot_id}",),
        ),
    )
    if force_incomplete:
        result = replace(
            result,
            complete=False,
            snapshot_consistent=False,
            blocking_resources=("ACCOUNT",),
            reasons=("forced-incomplete-provider-truth",),
        )
    return record_reconciliation_checkpoint(
        store,
        reconciliation_id=reconciliation_id,
        result=result,
        observed_at=observed_at,
        host_id="availability-test-host",
        owner_epoch="1",
    )


def _admit(authority, reservations, checkpoint, **overrides):
    values = dict(
        command_id="availability-command",
        idempotency_key="availability-command",
        admission_id="availability-admission",
        policy_id="availability-policy",
        intent_id="availability-intent",
        intent_hash="sha256:" + "a" * 64,
        account_id=ACCOUNT_ID,
        environment=ENVIRONMENT,
        instrument_id=INSTRUMENT_ID,
        instrument_version=1,
        action="ORDER.SUBMIT",
        notional="100",
        capability_snapshot_id="availability-capability-1",
        risk_intent=RiskIntent.create(
            symbol="ABC",
            side="BUY",
            quantity="1",
            price="100",
            expected_state_version=1,
        ),
        risk_context=_risk_context(),
        risk_policy=_risk_policy(),
        risk_valid_until="2026-09-24T18:05:00Z",
        reservation_book=reservations,
        reservation_id="availability-reservation",
        reservation_requirements={"CASH:USD": "100"},
        reservation_available={"CASH:USD": "1000"},
        reservation_checkpoint_event_id=checkpoint["event_id"],
        reservation_provider_id=PROVIDER_ID,
        reservation_max_age_seconds="60",
        now=NOW,
    )
    values.update(overrides)
    return authority.admit(**values)


class AuthorityAccountAvailabilityTests(unittest.TestCase):
    def test_admission_uses_exact_reconciled_cash_and_survives_restart_retry(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            store = JournalStore(path)
            authority = AuthorityService(store)
            authority.register_policy(_policy())
            checkpoint = _checkpoint(store)
            reservations = DurableReservationBook(
                store,
                environment=ENVIRONMENT,
                account_id=ACCOUNT_ID,
            )

            first = _admit(authority, reservations, checkpoint)
            self.assertEqual(first.outcome, "ADMITTED")
            self.assertEqual(
                reservations.total_reserved("CASH:USD"),
                Decimal("100"),
            )

            risk_event = store.load_events(
                "risk_decision", first.risk_decision_id
            )[0]
            evidence = risk_event["payload"][
                "reservation_availability_evidence"
            ]
            self.assertEqual(
                evidence["checkpoint_event_id"],
                checkpoint["event_id"],
            )
            self.assertEqual(
                evidence["checkpoint_payload_hash"],
                checkpoint["payload_hash"],
            )
            self.assertEqual(
                evidence["scope_latest_checkpoint_event_id"],
                checkpoint["event_id"],
            )
            self.assertEqual(
                evidence["scope_latest_checkpoint_aggregate_id"],
                checkpoint["aggregate_id"],
            )
            self.assertEqual(
                evidence["scope_latest_checkpoint_aggregate_version"],
                checkpoint["aggregate_version"],
            )
            self.assertEqual(
                evidence["availability"],
                {"CASH:USD": "1000"},
            )
            self.assertEqual(
                evidence["resource_snapshot_id"],
                "availability-snapshot",
            )
            self.assertEqual(
                evidence["resource_evidence_refs"],
                ["provider:availability-snapshot"],
            )
            self.assertIsInstance(evidence["resource_evidence_refs"], list)

            restarted_store = JournalStore(path)
            restarted_authority = AuthorityService(restarted_store)
            restarted_reservations = DurableReservationBook(
                restarted_store,
                environment=ENVIRONMENT,
                account_id=ACCOUNT_ID,
            )
            replay = _admit(
                restarted_authority,
                restarted_reservations,
                checkpoint,
            )
            self.assertEqual(replay, first)
            self.assertEqual(
                restarted_reservations.total_reserved("CASH:USD"),
                Decimal("100"),
            )
            self.assertEqual(
                len(
                    [
                        item
                        for item in restarted_store.pending_outbox()
                        if item["topic"] == "financial.admission.ready"
                    ]
                ),
                1,
            )

    def test_new_admission_cannot_select_superseded_reconciliation_truth(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            authority = AuthorityService(store)
            authority.register_policy(_policy())
            older = _checkpoint(
                store,
                available_cash="1000",
                observed_at="2026-09-24T18:00:30Z",
                reconciliation_id="older-reconciliation",
                snapshot_id="availability-old",
            )
            _checkpoint(
                store,
                cash="50",
                available_cash="50",
                observed_at="2026-09-24T18:00:40Z",
                reconciliation_id="newer-reconciliation",
                snapshot_id="availability-new",
            )
            reservations = DurableReservationBook(
                store,
                environment=ENVIRONMENT,
                account_id=ACCOUNT_ID,
            )

            with self.assertRaisesRegex(AuthorityConflict, "superseded|availability"):
                _admit(authority, reservations, older)
            self.assertEqual(reservations.total_reserved("CASH:USD"), Decimal("0"))

    def test_newer_incomplete_truth_supersedes_older_complete_checkpoint(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            authority = AuthorityService(store)
            authority.register_policy(_policy())
            older = _checkpoint(
                store,
                available_cash="1000",
                observed_at="2026-09-24T18:00:30Z",
                reconciliation_id="complete-reconciliation",
                snapshot_id="availability-complete",
            )
            _checkpoint(
                store,
                available_cash="1000",
                observed_at="2026-09-24T18:00:40Z",
                reconciliation_id="incomplete-reconciliation",
                snapshot_id="availability-incomplete",
                force_incomplete=True,
            )
            reservations = DurableReservationBook(
                store,
                environment=ENVIRONMENT,
                account_id=ACCOUNT_ID,
            )

            with self.assertRaisesRegex(AuthorityConflict, "superseded|availability"):
                _admit(authority, reservations, older)
            self.assertEqual(reservations.total_reserved("CASH:USD"), Decimal("0"))

    def test_exact_retry_keeps_immutable_checkpoint_but_dispatch_rechecks_current_truth(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            store = JournalStore(path)
            authority = AuthorityService(store)
            authority.register_policy(_policy())
            older = _checkpoint(
                store,
                available_cash="1000",
                observed_at="2026-09-24T18:00:30Z",
                reconciliation_id="retry-reconciliation-a",
                snapshot_id="availability-retry-a",
            )
            reservations = DurableReservationBook(
                store,
                environment=ENVIRONMENT,
                account_id=ACCOUNT_ID,
            )
            first = _admit(authority, reservations, older)
            self.assertEqual(first.outcome, "ADMITTED")
            risk_event = store.load_events(
                "risk_decision",
                first.risk_decision_id,
            )[0]
            journal_cut = risk_event["payload"]["journal_sequence_cut"]
            self.assertIs(type(journal_cut), int)
            self.assertGreaterEqual(journal_cut, 0)
            self.assertGreater(risk_event["journal_sequence"], journal_cut)
            pending_before = len(store.pending_outbox())

            _checkpoint(
                store,
                cash="50",
                available_cash="50",
                observed_at="2026-09-24T18:00:40Z",
                reconciliation_id="retry-reconciliation-b",
                snapshot_id="availability-retry-b",
            )
            replay = _admit(authority, reservations, older)
            self.assertEqual(replay, first)
            self.assertEqual(len(store.pending_outbox()), pending_before + 1)

            restarted_store = JournalStore(path)
            restarted_authority = AuthorityService(restarted_store)
            restarted_reservations = DurableReservationBook(
                restarted_store,
                environment=ENVIRONMENT,
                account_id=ACCOUNT_ID,
            )
            restarted_replay = _admit(
                restarted_authority,
                restarted_reservations,
                older,
            )
            self.assertEqual(restarted_replay, first)
            self.assertEqual(
                restarted_reservations.total_reserved("CASH:USD"),
                Decimal("100"),
            )

    def test_transaction_a_rejects_reconciliation_race_after_latest_check(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            authority = AuthorityService(store)
            authority.register_policy(_policy())
            selected = _checkpoint(
                store,
                available_cash="1000",
                observed_at="2026-09-24T18:00:30Z",
                reconciliation_id="race-reconciliation-a",
                snapshot_id="availability-race-a",
            )
            reservations = DurableReservationBook(
                store,
                environment=ENVIRONMENT,
                account_id=ACCOUNT_ID,
            )
            original_commit = store.commit_command
            injected = False

            def commit_with_newer_truth(**kwargs):
                nonlocal injected
                if not injected:
                    injected = True
                    _checkpoint(
                        store,
                        cash="50",
                        available_cash="50",
                        observed_at="2026-09-24T18:00:40Z",
                        reconciliation_id="race-reconciliation-b",
                        snapshot_id="availability-race-b",
                    )
                return original_commit(**kwargs)

            store.commit_command = commit_with_newer_truth
            with self.assertRaisesRegex(ValueError, "journal sequence changed"):
                _admit(
                    authority,
                    reservations,
                    selected,
                    command_id="availability-command-race",
                    idempotency_key="availability-command-race",
                    admission_id="availability-admission-race",
                    intent_id="availability-intent-race",
                    intent_hash="sha256:" + "d" * 64,
                    reservation_id="availability-reservation-race",
                )

            self.assertTrue(injected)
            self.assertEqual(
                reservations.total_reserved("CASH:USD"),
                Decimal("0"),
            )
            self.assertFalse(
                any(
                    item["topic"] == "financial.admission.ready"
                    for item in store.pending_outbox()
                )
            )


    def test_decimal_scale_is_canonical_across_admission_restart_replay(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            store = JournalStore(path)
            authority = AuthorityService(store)
            authority.register_policy(_policy())
            checkpoint = _checkpoint(store)
            reservations = DurableReservationBook(
                store,
                environment=ENVIRONMENT,
                account_id=ACCOUNT_ID,
            )

            first = _admit(
                authority,
                reservations,
                checkpoint,
                notional=Decimal("100.00"),
                reservation_requirements={"CASH:USD": Decimal("100.00")},
                reservation_max_age_seconds=Decimal("60.00"),
            )
            self.assertEqual(first.outcome, "ADMITTED")
            risk_event = store.load_events(
                "risk_decision", first.risk_decision_id
            )[0]
            self.assertEqual(
                risk_event["payload"]["reservation_availability_evidence"][
                    "max_age_seconds"
                ],
                "60",
            )
            admission_event = next(
                item
                for item in store.load_events("authority_state", "canonical")
                if item["event_type"] == "AuthorityAdmissionRecorded"
            )
            self.assertEqual(admission_event["payload"]["notional"], "100")

            restarted_store = JournalStore(path)
            restarted_authority = AuthorityService(restarted_store)
            restarted_reservations = DurableReservationBook(
                restarted_store,
                environment=ENVIRONMENT,
                account_id=ACCOUNT_ID,
            )
            pending_before_replay = len(restarted_store.pending_outbox())
            replay = _admit(
                restarted_authority,
                restarted_reservations,
                checkpoint,
                notional=Decimal("100.0"),
                reservation_requirements={"CASH:USD": Decimal("100.0")},
                reservation_max_age_seconds=Decimal("60.0"),
            )
            self.assertEqual(replay, first)
            self.assertEqual(
                len(restarted_store.pending_outbox()),
                pending_before_replay,
            )

            with self.assertRaises(AuthorityConflict):
                _admit(
                    restarted_authority,
                    restarted_reservations,
                    checkpoint,
                    notional=Decimal("101"),
                    reservation_requirements={"CASH:USD": Decimal("101")},
                    reservation_max_age_seconds=Decimal("60"),
                )
            self.assertEqual(
                len(restarted_store.pending_outbox()),
                pending_before_replay,
            )


    def test_inflated_caller_availability_cannot_increase_reservation_capacity(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            authority = AuthorityService(store)
            authority.register_policy(_policy())
            checkpoint = _checkpoint(
                store,
                cash="1000",
                available_cash="100",
            )
            reservations = DurableReservationBook(
                store,
                environment=ENVIRONMENT,
                account_id=ACCOUNT_ID,
            )

            with self.assertRaisesRegex(
                AuthorityConflict,
                "authoritative reconciliation checkpoint",
            ):
                _admit(
                    authority,
                    reservations,
                    checkpoint,
                    reservation_available={"CASH:USD": "1000"},
                )
            self.assertEqual(reservations.version, 0)
            self.assertEqual(
                reservations.total_reserved("CASH:USD"),
                Decimal("0"),
            )

    def test_resource_availability_requires_provider_provenance(self):
        common = dict(
            provider_id=PROVIDER_ID,
            account_id=ACCOUNT_ID,
            environment=ENVIRONMENT,
            snapshot_id="availability-snapshot",
            query_started_at="2026-09-24T18:00:00Z",
            query_completed_at="2026-09-24T18:00:30Z",
            valid_until="2026-09-24T18:02:00Z",
            available_resources={"CASH:USD": "1000"},
        )
        with self.assertRaisesRegex(ValueError, "at least one evidence_ref"):
            ResourceAvailabilityEvidence(**common, evidence_refs=())
        with self.assertRaisesRegex(TypeError, "tuple of strings"):
            ResourceAvailabilityEvidence(
                **common,
                evidence_refs=["provider:availability-snapshot"],
            )
        with self.assertRaisesRegex(ValueError, "must be unique"):
            ResourceAvailabilityEvidence(
                **common,
                evidence_refs=("provider:same", " provider:same "),
            )

    def test_malformed_persisted_capacity_provenance_fails_before_reservation(self):
        cases = (
            ("empty", []),
            ("wrong-type", "provider:availability-snapshot"),
            ("duplicate", ["provider:same", " provider:same "]),
        )
        for label, bad_refs in cases:
            with self.subTest(label=label), TemporaryDirectory() as directory:
                store = JournalStore(f"{directory}/journal.sqlite3")
                authority = AuthorityService(store)
                authority.register_policy(_policy())
                checkpoint = _checkpoint(store)
                reservations = DurableReservationBook(
                    store,
                    environment=ENVIRONMENT,
                    account_id=ACCOUNT_ID,
                )
                tampered = deepcopy(checkpoint)
                tampered["payload"]["resource_availability"][
                    "evidence_refs"
                ] = bad_refs

                with patch.object(store, "get_event", return_value=tampered):
                    with self.assertRaisesRegex(
                        ValueError,
                        "resource availability evidence_refs",
                    ):
                        _admit(authority, reservations, checkpoint)

                self.assertEqual(reservations.version, 0)
                self.assertEqual(
                    reservations.total_reserved("CASH:USD"),
                    Decimal("0"),
                )

    def test_stale_or_cross_account_checkpoint_fails_closed(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            authority = AuthorityService(store)
            authority.register_policy(_policy())
            checkpoint = _checkpoint(store)
            reservations = DurableReservationBook(
                store,
                environment=ENVIRONMENT,
                account_id=ACCOUNT_ID,
            )

            with self.assertRaisesRegex(ValueError, "stale"):
                _admit(
                    authority,
                    reservations,
                    checkpoint,
                    reservation_max_age_seconds="1",
                )
            with self.assertRaisesRegex(ValueError, "scope mismatch"):
                _admit(
                    authority,
                    reservations,
                    checkpoint,
                    account_id="other-account",
                )
            self.assertEqual(reservations.version, 0)


if __name__ == "__main__":
    unittest.main()
